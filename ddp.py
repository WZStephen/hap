import config
import time
import sys
import datetime
import torch
import torch.fx
from torch.profiler import profile, record_function, ProfilerActivity
from contextlib import nullcontext
import numpy as np
import hap

from torch.nn.parallel import DistributedDataParallel as DDP

from utils import *

def run(global_rank, local_rank):
    import torch.distributed as dist
    dist.init_process_group('nccl', rank=global_rank)

    model = config.get_model(seed=39).cuda(local_rank)
    dmodel = DDP(model, device_ids=[local_rank])
    del model

    optimizer = torch.optim.Adam(dmodel.parameters(), lr=config.lr)
    train_data = config.get_data()[1]

    result_times = []
    strat_time = last_iter_time = time.time()
    total_loss = 0

    x, y = next(train_data)
    sharding_lengths = [ 1 ] * config.world_size
    # sharding_lengths = [ 3858755112937 ] * round(config.world_size / 8 * 2) + [ 2149250936815 ] * round(config.world_size / 8 * 6)
    sharding_lengths = [ s / sum(sharding_lengths) for s in sharding_lengths]
    hap.sharding_round(x.shape[0], sharding_lengths)
    print(sharding_lengths, flush=True)
    x = x.split(sharding_lengths, 0)[global_rank].cuda(local_rank)
    y = y.split(sharding_lengths, 0)[global_rank].cuda(local_rank)

    for iter in range(config.run_iter):
        optimizer.zero_grad()

        with torch.autocast(device_type="cuda") if config.fp16 else nullcontext() :
            loss = dmodel(x, y) * config.world_size # DDP averages the loss

        aggregated_loss = loss.detach().clone()
        dist.reduce(aggregated_loss, 0)
        if global_rank == 0:
            total_loss += aggregated_loss.cpu().numpy() / config.batch_size / config.seqlen
            if iter % config.log_iter == 0:
                print(f"loss (log ppl) {iter}: {total_loss / config.log_iter:.3f}, wall clock: {time.time() - strat_time:.3f}")
                total_loss = 0
        # dist.barrier(device_ids=[global_rank])

        loss.backward()
        torch.nn.utils.clip_grad_norm_(dmodel.parameters(), 0.5)
        # torch.cuda.synchronize()
        optimizer.step()
        # dist.barrier()
        if config.report_per_iter_time and local_rank == 0:
            iter_duration = time.time() - last_iter_time
            result_times.append(iter_duration)
            last_iter_time += iter_duration
            print("iter time: ", iter_duration)
            print("avg±std:", np.mean(result_times[-config.avg_iter:]), np.std(result_times[-config.avg_iter:]), flush=True)

    if not config.trace:
        return

    # x, y = next(train_data)
    # x = x.chunk(config.world_size, 0)[global_rank].cuda(local_rank)
    # y = y.chunk(config.world_size, 0)[global_rank].cuda(local_rank)
    with profile(
        activities = [ProfilerActivity.CPU, ProfilerActivity.CUDA],
        # record_shapes = True,
        # profile_memory = True,
        schedule = torch.profiler.schedule(wait=1, warmup=10, active=4)
    ) as prof:
        for _ in range(15):
            with record_function("forward"):
                with torch.autocast(device_type="cuda") if config.fp16 else nullcontext() :
                    loss = dmodel(x, y)
            with record_function("backward"):
                loss.backward()
                torch.cuda.synchronize()
            with record_function("update"):
                optimizer.step()
            dist.barrier()
            prof.step()

    if local_rank == 0:
        # print(prof.key_averages().table(sort_by="cuda_time_total"))
        prof.export_chrome_trace("trace.json")

if __name__ == '__main__':
    ranks = [ int(x) for x in sys.argv[1].split(',') ]

    # if torch.cuda.device_count() != len(ranks):
    #     print("forget to set CUDA_VISIBLE_DEVICES")
    #     raise SystemExit

    import os
    os.environ['MASTER_ADDR'] = str(config.master_addr)
    os.environ['MASTER_PORT'] = str(config.master_port)
    os.environ['WORLD_SIZE'] = str(config.world_size)

    import torch.multiprocessing as mp
    mp.set_start_method('spawn')

    for local_rank, global_rank in enumerate(ranks):
        mp.Process(target=run, args=(global_rank, local_rank)).start()

    for p in mp.active_children():
        p.join()
