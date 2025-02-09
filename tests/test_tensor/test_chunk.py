import torch
import colossalai
import pytest
import torch.multiprocessing as mp
from typing import List
from functools import partial
from colossalai.tensor import ChunkManager
from colossalai.testing import rerun_if_address_is_in_use, parameterize
from colossalai.utils import free_port
from colossalai.core import global_context as gpc
from colossalai.context import ParallelMode


def check_has_params(params: List[torch.Tensor], has_tensors: List[bool]):
    for p, has_tensor in zip(params, has_tensors):
        if has_tensor:
            assert p.storage().size() > 0
            assert p.device.type == 'cuda'
        else:
            assert p.storage().size() == 0


# HAS_TENSORS[use_chunk][use_zero]
HAS_TENSORS = {
    True: {
        True: [[True, True, False], [False, False, True]],
        False: [[True, True, True], [True, True, True]]
    },
    False: {
        True: [[True, False, True], [False, True, False]],
        False: [[True, True, True], [True, True, True]]
    }
}


@parameterize('use_chunk', [False, True])
@parameterize('use_zero', [False, True])
def run_chunk_zero(use_chunk, use_zero):
    rank = gpc.get_local_rank(ParallelMode.DATA)
    if rank == 0:
        print(f'use_chunk={use_chunk}, use_zero={use_zero}')
    params = [torch.rand(32, 32) for _ in range(3)]
    chunk_size = 2048 if use_chunk else None
    chunk_manager = ChunkManager(chunk_size, enable_distributed_storage=use_zero)
    for p in params:
        chunk_manager.append_tensor(p, 'param')
    check_has_params(params, HAS_TENSORS[use_chunk][use_zero][rank])
    for p in params:
        chunk_manager.access_chunk(p)
    check_has_params(params, [True, True, True])
    for p in params:
        chunk_manager.release_chunk(p)
    check_has_params(params, HAS_TENSORS[use_chunk][use_zero][rank])


def run_dist(rank, world_size, port):
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    run_chunk_zero()


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [2])
@rerun_if_address_is_in_use()
def test_chunk_mapping(world_size):
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_chunk_mapping(2)
