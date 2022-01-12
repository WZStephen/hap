import config

import os
import sys
import time
import torch
import torch.distributed as dist
import torch.nn as nn
from torch.profiler import profile, record_function, ProfilerActivity

from utils import *
import fmoe

class SwitchTransformerEncoderLayer(nn.Module):
    def __init__(self, global_rank):
        super().__init__()

        self.self_atten = torch.nn.MultiheadAttention(config.emsize, config.nheads, dropout=config.dropout)

        self.moe = fmoe.FMoETransformerMLP(
            num_expert=config.n_expert // config.world_size, # this is the number of experts on *each* worker
            d_model=config.emsize,
            d_hidden=config.nhid,
            expert_rank=global_rank,
            top_k=1,
            world_size=config.world_size,
        )

        self.norm1 = torch.nn.LayerNorm(config.emsize, eps=1e-5)
        self.norm2 = torch.nn.LayerNorm(config.emsize, eps=1e-5)
        self.dropout = torch.nn.Dropout(config.dropout)

    def forward(self, x):
        x = self.norm1(x + self._sa_block(x))
        x = self.norm2(x + self.moe(x))
        return x

    def _sa_block(self, x):
        x = self.self_atten(x, x, x, need_weights=False)[0]
        return self.dropout(x)

# class NaiveSwitchTransformerEncoderLayer(nn.Module):
#     def __init__(self):
#         super().__init__()

#         self.self_atten = torch.nn.MultiheadAttention(config.emsize, 4, dropout=config.dropout)

#         self.moe = fmoe.FMoE(
#             num_expert=config.n_expert // config.world_size, # this is the number of experts on *each* worker
#             d_model=config.emsize,
#             top_k=1,
#             world_size=world_size,

#             expert=lambda d: torch.nn.Sequential(
#                 # LogShape(),
#                 torch.nn.Linear(d, config.nhid),
#                 torch.nn.ReLU(),
#                 torch.nn.Linear(config.nhid, d),
#             ),
#         )

#         self.norm1 = torch.nn.LayerNorm(config.emsize, eps=1e-5)
#         self.norm2 = torch.nn.LayerNorm(config.emsize, eps=1e-5)
#         self.dropout = torch.nn.Dropout(config.dropout)

#     def forward(self, x):
#         x = self.norm1(x + self._sa_block(x))
#         x = self.norm2(x + self.moe(x))
#         return x

#     def _sa_block(self, x):
#         x = self.self_atten(x, x, x, need_weights=False)[0]
#         return self.dropout(x)

# class LogShape(torch.nn.Module):
#     def __init__(self) -> None:
#         super().__init__()
#     def forward(self, x):
#         print(x.shape)
#         return x

class MoE(torch.nn.Module):
    def __init__(self, global_rank) -> None:
        super().__init__()

        self.layers = torch.nn.ModuleList([
            torch.nn.TransformerEncoderLayer(config.emsize, config.nheads, config.nhid, config.dropout)
            if i % 2 == 0 else
            SwitchTransformerEncoderLayer(global_rank)
            # NaiveSwitchTransformerEncoderLayer()
            for i in range(config.nlayers)
        ])

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return torch.sum(x)

# from torch.profiler import profile, record_function, ProfilerActivity

# with profile(
#     activities= [ProfilerActivity.CPU, ProfilerActivity.CUDA],
#     schedule= torch.profiler.schedule(wait=1, warmup=1, active=4)
# ) as prof:
#     for _ in range(6):
#         with record_function("forward"):
#             loss = model(rand_input)
#         with record_function("backward"):
#             loss.backward()
#             torch.cuda.synchronize()
#         with record_function("update"):
#             optimizer.step()
#         dist.barrier()
#         prof.step()

# if rank == 0:
#     prof.export_chrome_trace("trace.json")

def run(global_rank, local_rank):
    import torch.distributed as dist
    dist.init_process_group('nccl', rank=global_rank)

    torch.manual_seed(0)
    # torch.use_deterministic_algorithms(True)

    model = MoE(global_rank).to(local_rank)
    model = fmoe.DistributedGroupedDataParallel(model)

    # optimizer = torch.optim.SGD(model.parameters(), lr=1e-6)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-6)
    test_input = torch.rand(config.batch_size, config.seqlen, config.emsize).cuda(local_rank) / 6
    test_input = test_input.chunk(config.world_size, 0)[global_rank]

    for iter in range(10):
        with measure_time(f"iteration {iter}") as wall_time:
            loss = model(test_input)
            aggregated_loss = loss.detach().clone()
            dist.reduce(aggregated_loss, 0)
            if global_rank == 0:
                print(f"loss {iter}:", aggregated_loss.cpu().numpy())
            # dist.barrier(device_ids=[rank])

            loss.backward()
            # torch.cuda.synchronize()
            optimizer.step()
            dist.barrier()
        if local_rank == 0:
            print(wall_time)

    with profile(
        activities = [ProfilerActivity.CPU, ProfilerActivity.CUDA],
        # record_shapes = True,
        # profile_memory = True,
        schedule = torch.profiler.schedule(wait=1, warmup=10, active=4)
    ) as prof:
        for _ in range(15):
            with record_function("forward"):
                loss = model(test_input)
            with record_function("backward"):
                loss.backward()
                torch.cuda.synchronize()
            with record_function("update"):
                optimizer.step()
            dist.barrier()
            prof.step()

    if global_rank == 0:
        # print(prof.key_averages().table(sort_by="cuda_time_total"))
        prof.export_chrome_trace("trace.json")

if __name__ == '__main__':
    ranks = [ int(x) for x in sys.argv[1].split(',') ]

    if torch.cuda.device_count() != len(ranks):
        print("forget to set CUDA_VISIBLE_DEVICES")
        raise SystemExit

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
