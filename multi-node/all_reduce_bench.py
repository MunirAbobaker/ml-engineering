#!/usr/bin/env python

# this version
# has been derived from @jeffra's gist: https://gist.github.com/jeffra/b5e80466b4c86be00ea3b6f130fb7a36
#    which in turn is derived from the logic in https://github.com/NVIDIA/nccl-tests
# with contributions from:
# 1. Indu Thangakrishnan https://github.com/indhub to handle timing correctly using cuda events
# 2. Ross Whiteman https://github.com/rwightman who suggested to gather results to print from rank 0 to avoid interleaving
#
# Important: when you finished running this benchmark you want to pay attention to the busbw result (not
# algobw) as explained here
#
# To run on 4 nodes:
#
# GPUS_PER_NODE=8
# NNODES=4
# MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
# MASTER_PORT=6000
# python -u -m torch.distributed.run \
#     --nproc_per_node $GPUS_PER_NODE \
#     --nnodes $NNODES \
#     --rdzv_endpoint $MASTER_ADDR:$MASTER_PORT \
#     --rdzv_backend c10d \
#     --max_restarts 0 \
#     --role `hostname -s`: \
#     --tee 3 \
#     all_reduce_bench.py
#
# note: adapt MASTER_ADDR to rank 0 hostname if it's not a SLURM environment where it's derived automatically
#
# Important: when you finished running this benchmark you want to pay attention to the busbw result
# (not algobw) as they are number of gpus-agnostic as explained here:
# https://github.com/NVIDIA/nccl-tests/blob/master/doc/PERFORMANCE.md#bandwidth
#
# e.g. example to run with salloc+srun
# salloc --partition=mypartition --nodes=4 --ntasks-per-node=1 --cpus-per-task=48 --gres=gpu:8 --time=1:00:00 bash
# srun --gres=gpu:8 --nodes=4 --tasks-per-node=1 python -u -m torch.distributed.run --nproc_per_node=8 --nnodes 4 --rdzv_endpoint $(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1):6000 --rdzv_backend c10d all_reduce_bench.py


import os
import socket
import torch
import torch.distributed as dist

TRIALS = 5

N = 500000
M = 2000

def timed_allreduce(mat, id, start_event, end_event):
    start_event.record()
    dist.all_reduce(mat)
    end_event.record()

    torch.cuda.synchronize()
    duration = start_event.elapsed_time(end_event) / 1000

    tput = ((M*N*4*2)/duration)*8 # *2 is for send + receive, *8 for gigabits/second
    size = M * N * 4 # 4 is fp32
    n = dist.get_world_size()
    # 2*(n-1)/n correction factor is explained here:
    # https://github.com/NVIDIA/nccl-tests/blob/master/doc/PERFORMANCE.md#allreduce
    busbw = (size / duration) * (2 * (n - 1) / n) * 8

    # gather all data on global-rank-0 and print the results from there to avoid interleaved prints
    data = [id, duration, tput, busbw]
    output = [None for _ in range(dist.get_world_size())] if dist.get_rank() == 0 else None
    dist.gather_object(data, output, dst=0)
    if dist.get_rank() == 0:
        for data in output:
            id, duration, tput, busbw = data
            print(f"{id}:\n",
                  f"duration: {duration:.4f} sec\n",
                  f"algo throughput: {tput/1e9:.4f} Gbps\n",
                  f"busbw: {busbw / 1e9:.4f}  Gbps"
    )

def run(local_rank):
    hostname = socket.gethostname()
    id = f"{hostname}:{local_rank}"
    global_rank = dist.get_rank()

    if global_rank == 0:
        print(f"{id} data size: {M*N*4/1e9} GB")
    mat = torch.rand(N, M, dtype=torch.float32).cuda(local_rank)

    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    for i in range(TRIALS):
        dist.barrier()
        if global_rank == 0:
            print(f"\n\n\n-----------trial-{i}----------------")
        timed_allreduce(mat, id, start_event, end_event)

def init_processes(local_rank, fn, backend='nccl'):
    torch.cuda.set_device(local_rank)
    dist.init_process_group(backend)
    fn(local_rank)


if __name__ == "__main__":
    rank = int(os.environ["LOCAL_RANK"])
    print("local_rank: %d" % rank)
    init_processes(local_rank=rank, fn=run)
