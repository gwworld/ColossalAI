import torch
import torch.distributed as dist
from colossalai.core import global_context as gpc
from colossalai.context import ParallelMode
from functools import partial
from colossalai.zero.utils.zero_hook_v2 import ZeROHookV2
from colossalai.tensor import ChunkManager, use_param_op_hooks, TensorState

__all__ = ['ColoDDP', 'ColoDDPV2']


def free_storage(data: torch.Tensor) -> None:
    """Free underlying storage of a Tensor."""
    if data.storage().size() > 0:
        # Since we're modifying the Tensor's Storage directly, make sure the Tensor
        # is the sole occupant of the Storage.
        assert data.storage_offset() == 0
        data.storage().resize_(0)


class ColoDDP(torch.nn.Module):

    def __init__(self, module: torch.nn.Module) -> None:
        super().__init__()
        self.module = module
        self.comm_stream: torch.cuda.Stream = torch.cuda.Stream()
        self.dp_world_size = gpc.get_world_size(ParallelMode.DATA)
        for p in module.parameters():
            if p.requires_grad:
                p.register_hook(partial(self.grad_handle, p))

    def parameters(self, recurse: bool = True):
        return self.module.parameters(recurse)

    def named_parameters(self, prefix: str = '', recurse: bool = True):
        return self.module.named_parameters(prefix, recurse)

    def forward(self, *args, **kwargs):
        self.module.zero_grad(set_to_none=True)
        return self.module(*args, **kwargs)

    def backward(self, loss: torch.Tensor):
        loss.backward()
        torch.cuda.current_stream().wait_stream(self.comm_stream)
        for p in self.module.parameters():
            p.grad = p._saved_grad

    def grad_handle(self, p, grad):
        empty_grad = torch.empty_like(grad)
        free_storage(empty_grad)
        if self.dp_world_size > 1:
            grad = grad / self.dp_world_size
            self.comm_stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(self.comm_stream):
                dist.all_reduce(grad, group=gpc.get_group(ParallelMode.DATA))
                ColoDDP._save_grad(p, grad)
            grad.record_stream(self.comm_stream)
        else:
            ColoDDP._save_grad(p, grad)
        return empty_grad

    @staticmethod
    def _save_grad(p, grad):
        if hasattr(p, '_saved_grad'):
            p._saved_grad.add_(grad)
        else:
            p._saved_grad = grad

    def zero_grad(self, set_to_none: bool = False) -> None:
        self.module.zero_grad(set_to_none=True)
        for p in self.module.parameters():
            if getattr(p, '_saved_grad', None) is not None:
                if set_to_none:
                    p._saved_grad = None
                else:
                    if p._saved_grad.grad_fn is not None:
                        p._saved_grad.detach_()
                    else:
                        p._saved_grad.requires_grad_(False)
                    p._saved_grad.zero_()


class ColoDDPV2(ColoDDP):

    def __init__(self, module: torch.nn.Module, chunk_manager: ChunkManager) -> None:
        super().__init__(module)
        self.chunk_manager = chunk_manager
        self.param_op_hook = ZeROHookV2(chunk_manager)
        self.fp32_params = []
        # TODO: get param order and filter unused params
        for p in module.parameters():
            assert p.dtype == torch.half
            fp32_p = p.float()
            self.chunk_manager.append_tensor(p, 'fp16_param')
            self.chunk_manager.append_tensor(fp32_p, 'fp32_param')
            self.fp32_params.append(fp32_p)

    def forward(self, *args, **kwargs):
        self.module.zero_grad(set_to_none=True)
        for p, fp32_p in zip(self.module.parameters(), self.fp32_params):
            if not self.chunk_manager.is_chunk_free(p):
                self.chunk_manager.copy_tensor_to_chunk_slice(p, fp32_p)
        with use_param_op_hooks(self.param_op_hook):
            outputs = self.module(*args, **kwargs)
        self.chunk_manager.exec_lazy_release()
        return outputs

    def backward(self, loss: torch.Tensor):
        with self.param_op_hook.switch_to_backward(), use_param_op_hooks(self.param_op_hook):
            loss.backward()
        self.chunk_manager.exec_lazy_release()
        for p in self.module.parameters():
            if self.chunk_manager.is_chunk_free(p) or not p.requires_grad:
                p.grad = None
            else:
                p.grad = p.data

    def grad_handle(self, p, grad):
        empty_grad = torch.empty_like(grad)
        free_storage(empty_grad)
        with torch._C.DisableTorchFunction():
            self.chunk_manager.trans_tensor_state(p, TensorState.READY_FOR_REDUCE)
            if self.dp_world_size > 1:
                grad = grad / self.dp_world_size
            self.chunk_manager.copy_tensor_to_chunk_slice(p, grad)
            self.chunk_manager.reduce_chunk(p)
            self.chunk_manager.release_chunk(p)
        return empty_grad

    def zero_grad(self, set_to_none: bool = False) -> None:
        self.module.zero_grad(set_to_none=True)
