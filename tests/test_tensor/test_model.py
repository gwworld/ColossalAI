from colossalai.tensor.colo_parameter import ColoParameter
from tests.components_to_test.registry import non_distributed_component_funcs

import colossalai
import pytest
import torch
import torch.multiprocessing as mp
from colossalai.testing import parameterize, rerun_if_address_is_in_use
from colossalai.utils.cuda import get_current_device
from colossalai.utils import free_port
from colossalai.utils import ColoInitContext
from colossalai.tensor import distspec, named_params_with_colotensor, TensorSpec, ComputePattern, \
    ParallelAction, ColoTensor, ColoOptimizer, DistSpecManager
from colossalai.context import ParallelMode
from colossalai.core import global_context as gpc
from functools import partial
from _utils import set_seed


def init_1d_row_linear(weight):
    spec = TensorSpec(
        distspec.shard(gpc.get_group(ParallelMode.PARALLEL_1D), [-1], [gpc.get_world_size(ParallelMode.PARALLEL_1D)]),
        ParallelAction(ComputePattern.TP1D))
    with DistSpecManager.no_grad():
        weight.set_spec(spec)


def init_1d_col_linear(weight, gather_out=True):
    spec = TensorSpec(
        distspec.shard(gpc.get_group(ParallelMode.PARALLEL_1D), [0], [gpc.get_world_size(ParallelMode.PARALLEL_1D)]),
        ParallelAction(ComputePattern.TP1D, gather_out=gather_out))
    with DistSpecManager.no_grad():
        weight.set_spec(spec)


def init_1d_row_embedding(weight):
    spec = TensorSpec(
        distspec.shard(gpc.get_group(ParallelMode.PARALLEL_1D), [0], [gpc.get_world_size(ParallelMode.PARALLEL_1D)]),
        ParallelAction(ComputePattern.TP1D))
    with DistSpecManager.no_grad():
        weight.set_spec(spec)


def init_1d_col_embedding(weight):
    spec = TensorSpec(
        distspec.shard(gpc.get_group(ParallelMode.PARALLEL_1D), [-1], [gpc.get_world_size(ParallelMode.PARALLEL_1D)]),
        ParallelAction(ComputePattern.TP1D))
    with DistSpecManager.no_grad():
        weight.set_spec(spec)


def run_1d_hybrid_tp(model_name):
    # A simple net with two stacked nn.Linear
    get_components_func = non_distributed_component_funcs.get_callable(model_name)
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()
    rank = gpc.get_local_rank(ParallelMode.PARALLEL_1D)

    set_seed(1)
    with ColoInitContext(device=get_current_device()):
        model = model_builder(checkpoint=True)

    if rank == 0:
        model_torch = model_builder(checkpoint=True)
        model_torch = model_torch.cuda()
        colo_optimizer_torch = ColoOptimizer(dict(model_torch.named_parameters()), torch.optim.SGD, lr=0.1)

        # Make two models have the same init params
        for p1, p2 in zip(model.parameters(), model_torch.parameters()):
            p2.data.copy_(p1.data)

    if 'bert' == model_name:
        for name, p in model.named_parameters():
            if not isinstance(p, ColoTensor):
                continue
            # print(name)
            # num_class = type_vocab_size = 2 | (8, 2)
            if 'classifier' in name and 'weight' in name:
                init_1d_row_linear(p)
            # num_class = vocab_size = 30524 | (30524, 8)
            if 'word_embeddings' in name and 'weight' in name:
                init_1d_row_embedding(p)
            # num_class = seq_len = 512 | (512, 8)
            if 'position_embeddings' in name and 'weight' in name:
                init_1d_row_embedding(p)
            # num_class = type_vocab_size = 2 | (2, 8)
            if 'token_type_embeddings' in name and 'weight' in name:
                init_1d_col_embedding(p)
    elif "simple_net" == model_name:
        # A naive way to set spec for all weights in Linear
        for name, p in model.named_parameters():
            if not isinstance(p, ColoTensor):
                continue
            if 'embed' in name and 'weight' in name:
                init_1d_col_embedding(p)
            if 'proj1' in name and ('weight' in name or 'bias' in name):
                init_1d_col_linear(p)
            if 'proj2' in name and 'weight' in name:
                init_1d_row_linear(p)
            if 'classifier' in name and ('weight' in name or 'bias' in name):
                init_1d_col_linear(p, gather_out=False)

    model = model.cuda()
    colo_optimizer = ColoOptimizer(dict(model.named_parameters()), torch.optim.SGD, lr=0.1)
    for i, (data, label) in enumerate(train_dataloader):
        model.eval()
        colo_optimizer.zero_grad()
        if rank == 0:
            model_torch.eval()
            colo_optimizer_torch.zero_grad()

        data = data.to(get_current_device())
        label = label.to(get_current_device())

        torch.distributed.broadcast(data, 0, group=gpc.get_group(ParallelMode.PARALLEL_1D))
        torch.distributed.broadcast(label, 0, group=gpc.get_group(ParallelMode.PARALLEL_1D))
        # Bcast rank0 data to all processes
        if criterion:
            output = model(data)
            loss = criterion(output, label)
        else:
            output = model(data, label)
            loss = output

        # For reference
        if rank == 0:
            if criterion:
                output_torch = model_torch(data)
                loss_torch = criterion(output_torch, label)
            else:
                output_torch = model_torch(data, label)
                loss_torch = output_torch

        if rank == 0:
            with torch.no_grad():
                assert torch.allclose(loss, loss_torch, rtol=1e-2)

        loss.backward()
        colo_optimizer.step()

        if rank == 0:
            loss_torch.backward()
            colo_optimizer_torch.step()

            with torch.no_grad():
                # check param
                for p1, p2 in zip(model.parameters(), model_torch.parameters()):
                    if p1.size() == p2.size():
                        assert torch.allclose(p1, p2)
                    else:
                        # TODO(jzy) Only check 1D spec. Need to be replaced by new DistSpec.
                        if p1.size(-1) < p2.size(-1):    # col
                            world_size = p2.size(-1) // p1.size(-1)
                            split_p2 = torch.chunk(p2, world_size, dim=-1)[0]

                        elif p1.size(0) < p2.size(0):    # row
                            world_size = p2.size(0) // p1.size(0)
                            split_p2 = torch.chunk(p2, world_size, dim=0)[0]

                        assert torch.allclose(p1, split_p2)

        if i > 5:
            break


# Test the overrided parameters() and named_parameters() member functions
def test_model_parameters():
    # build a module with 2 Linear, 4 parameters in total.
    class Net(torch.nn.Module):

        def __init__(self):
            super().__init__()
            self.fcs = torch.nn.Sequential(torch.nn.Linear(2, 3), torch.nn.Linear(3, 2))
            self.extra_param = torch.nn.Parameter(torch.randn(2))

    with ColoInitContext(device=get_current_device()):
        model = Net()

    param_cnt = 0
    for name, p in model.named_parameters():
        param_cnt += 1
    assert param_cnt == 5

    for name, colo_p in model.named_parameters():
        assert colo_p.is_model_data()

    param_cnt = 0
    for name, p in model.named_parameters(recurse=False):
        param_cnt += 1
    assert param_cnt == 1

    param_cnt = 0
    for p in model.fcs[0].parameters(recurse=False):
        param_cnt += 1
    assert param_cnt == 2


def test_colo_optimizer():
    get_components_func = non_distributed_component_funcs.get_callable('simple_net')
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()
    set_seed(1)
    with ColoInitContext(lazy_memory_allocate=False, device=get_current_device()):
        model = model_builder(checkpoint=True)

    colo_optimizer = ColoOptimizer(dict(model.named_parameters()), torch.optim.SGD, lr=0.1)
    for i, (data, label) in enumerate(train_dataloader):
        colo_optimizer.zero_grad()
        data = data.to(get_current_device())
        label = label.to(get_current_device())

        # Bcast rank0 data to all processes
        if criterion:
            output = model(data)
            loss = criterion(output, label)
        else:
            output = model(data, label)
            loss = output

        loss.backward()
        colo_optimizer.step()

        if i > 5:
            break


def run_1d_row_tp(model_name: str):
    # A simple net with two stacked nn.Linear
    get_components_func = non_distributed_component_funcs.get_callable(model_name)
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()
    rank = gpc.get_local_rank(ParallelMode.PARALLEL_1D)

    set_seed(1)
    with ColoInitContext(device=get_current_device()):
        model = model_builder(checkpoint=True)

    set_seed(1)
    if rank == 0:
        model_torch = model_builder(checkpoint=True)
        model_torch = model_torch.cuda()
    # A naive way to set spec for all weights in Linear
    for name, p in model.named_parameters():
        if not isinstance(p, ColoTensor):
            continue
        if 'weight' in name and 'LayerNorm' not in name and 'ln' not in name and 'embed' not in name:
            init_1d_row_linear(p)
        if 'embed' in name and 'weight' in name:
            init_1d_row_embedding(p)

    model = model.cuda()

    for i, (data, label) in enumerate(train_dataloader):
        data = data.to(get_current_device())
        label = label.to(get_current_device())

        torch.distributed.broadcast(data, 0, group=gpc.get_group(ParallelMode.PARALLEL_1D))
        torch.distributed.broadcast(label, 0, group=gpc.get_group(ParallelMode.PARALLEL_1D))

        # Bcast rank0 data to all processes
        if criterion:
            output = model(data)
            loss = criterion(output, label)
        else:
            output = model(data, label)
            loss = output

        # For reference
        if rank == 0:
            if criterion:
                output_torch = model_torch(data)
                loss_torch = criterion(output_torch, label)
            else:
                output_torch = model_torch(data, label)
                loss_torch = output_torch

        if rank == 0:
            assert torch.allclose(loss, loss_torch, rtol=1e-2)

        loss.backward()

        if rank == 0:
            loss_torch.backward()
        if i > 5:
            break


def _run_pretrain_load():
    from _utils import check_equal
    from transformers import BertForMaskedLM
    set_seed(1)
    model_pretrained = BertForMaskedLM.from_pretrained('bert-base-uncased')
    with ColoInitContext(lazy_memory_allocate=False, device=get_current_device()):
        model = BertForMaskedLM.from_pretrained('bert-base-uncased')

    model_pretrained = model_pretrained.cuda()
    model = model.cuda()

    dict_pretrained = {}
    dict_col = {}
    c_ref = 0
    for name, param in model_pretrained.named_parameters():
        dict_pretrained[name] = param
        c_ref += 1
    c1 = 0
    c2 = 0
    for name, param in model.named_parameters():
        if isinstance(param, ColoParameter):
            c1 += 1
        else:
            c2 += 1
        dict_col[name] = param
    assert c_ref == c1
    assert c2 == 0
    if model_pretrained.cls.predictions.decoder.bias is model_pretrained.cls.predictions.bias:
        assert model.cls.predictions.decoder.bias is model.cls.predictions.bias

    for name, param in dict_pretrained.items():
        check_equal(param, dict_col[name])


def run_model_dist(rank, world_size, port):
    config = dict(parallel=dict(tensor=dict(mode="1d", size=world_size),))
    colossalai.launch(config=config, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    for name in ['simple_net']:
        run_1d_row_tp(name)
    for name in ['bert', 'simple_net']:
        run_1d_hybrid_tp(name)


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [1, 4])
# @parameterize('world_size', [1, 4])
@rerun_if_address_is_in_use()
def test_model(world_size):
    run_func = partial(run_model_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


def run_pretrain_load_dist(rank, world_size, port):
    config = dict(parallel=dict(tensor=dict(mode="1d", size=world_size),))
    colossalai.launch(config=config, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    _run_pretrain_load()


# The test case has to download huggingface pretrained models from the internet
# So we manually trigger the test.
@pytest.mark.dist
@pytest.mark.parametrize('world_size', [1, 4])
@rerun_if_address_is_in_use()
def _test_pretrain_load(world_size):
    run_func = partial(run_pretrain_load_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    # test_model_parameters()
    # test_colo_optimizer()
    # test_model(4)
    _test_pretrain_load(4)
