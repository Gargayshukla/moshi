# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, Future, Executor
from contextlib import contextmanager
from functools import wraps, lru_cache
import itertools
import hashlib
import json
import logging
from pathlib import Path
import typing as tp

import omegaconf
import torch

from .compile import torch_compile_lazy, no_compile  # noqa


logger = logging.getLogger(__name__)


def model_hash(model: torch.nn.Module) -> str:
    """Return a model hash. This should allow us to track regressions in model init
    from the logs of past experiments.
    """
    hasher = hashlib.sha1()
    for p in model.parameters():
        hasher.update(p.data.float().cpu().numpy().tobytes())
    return hasher.hexdigest()


def dict_from_config(cfg: omegaconf.DictConfig) -> dict:
    """Convenience function to map an omegaconf configuration to a dictionary.

    Args:
        cfg (omegaconf.DictConfig): Original configuration to map to dict.
    Returns:
        dict: Config as dictionary object.
    """
    dct = omegaconf.OmegaConf.to_container(cfg, resolve=True)
    assert isinstance(dct, dict)
    return dct


def random_subset(dataset, max_samples: int, seed: int = 42) -> torch.utils.data.Subset:
    if max_samples >= len(dataset):
        return dataset

    generator = torch.Generator().manual_seed(seed)
    perm = torch.randperm(len(dataset), generator=generator)
    return torch.utils.data.Subset(dataset, perm[:max_samples].tolist())


def get_loader(dataset, num_samples: tp.Optional[int], batch_size: int,
               num_workers: int, seed: int, **kwargs) -> torch.utils.data.DataLoader:
    """Convenience function to load dataset into a dataloader with optional subset sampling.

    Args:
        dataset: Dataset to load.
        num_samples (Optional[int]): Number of samples to limit subset size.
        batch_size (int): Batch size.
        num_workers (int): Number of workers for data loading.
        seed (int): Random seed.
    """
    if num_samples is not None:
        dataset = random_subset(dataset, num_samples, seed)

    dataloader = flashy.distrib.loader(
        dataset,
        batch_size=batch_size,
        num_workers=num_workers,
        **kwargs
    )
    return dataloader


def get_dataset_from_loader(dataloader):
    dataset = dataloader.dataset
    if isinstance(dataset, torch.utils.data.Subset):
        return dataset.dataset
    else:
        return dataset


@torch_compile_lazy
def cross_entropy(
        logits: torch.Tensor, targets: torch.Tensor, mask: torch.Tensor, dtype=torch.float32,
        logits_soft_clip: float | None = None) -> torch.Tensor:
    """Compute cross entropy between multi-codebook targets and model's logits.
    The cross entropy is computed per codebook to provide codebook-level cross entropy.
    Valid timesteps for each of the codebook are pulled from the mask, where invalid
    timesteps are set to 0.

    Args:
        logits (torch.Tensor): Model's logits of shape [B, K, T, card].
        targets (torch.Tensor): Target codes, of shape [B, K, T].
        mask (torch.Tensor): Mask for valid target codes, of shape [B, K, T].
        dtype (type): Data type of the output cross entropy.
        logits_soft_clip (float): Clipping value for the logits to avoid numerical instability.
            Recommended value: 30.0.
    Returns:
        ce (torch.Tensor): Cross entropy [B, K, T] with type dtype.
    """
    output_shape = targets.shape
    assert logits.shape[:-1] == targets.shape
    assert mask.shape == targets.shape
    logits = logits.view(-1, logits.shape[-1])
    targets = targets.reshape(-1)
    mask = mask.reshape(-1)

    safe_targets = torch.where(
        mask,
        targets,
        torch.zeros(1, device=targets.device, dtype=targets.dtype),
    )

    # Chunking the conversion to float32 to avoid OOMs.
    ce_chunks = []
    for logits_chunk, targets_chunk in zip(torch.chunk(logits, 4), torch.chunk(safe_targets, 4)):
        logits_chunk = logits_chunk.to(dtype)
        if logits_soft_clip is not None:
            logits_chunk = logits_soft_clip * torch.tanh(logits_chunk / logits_soft_clip)
        log_partition = torch.logsumexp(logits_chunk, dim=-1, keepdim=True)

        # For some reason, the PyTorch cross entropy is super slow with inputs with large cardinality (e.g. 32000)
        # so we reimplement the cross entropy ourselves...
        ce_chunks.append(log_partition - logits_chunk.gather(-1, targets_chunk[..., None]))
    ce = torch.cat(ce_chunks, dim=0)
    ce = ce[..., 0]
    ce = torch.where(mask, ce, torch.zeros(1, device=ce.device, dtype=ce.dtype))
    return ce.view(output_shape)


def multinomial(input: torch.Tensor, num_samples: int, replacement=False, *, generator=None):
    """torch.multinomial with arbitrary number of dimensions, and number of candidates on the last dimension.

    Args:
        input (torch.Tensor): The input tensor containing probabilities.
        num_samples (int): Number of samples to draw.
        replacement (bool): Whether to draw with replacement or not.
    Keywords args:
        generator (torch.Generator): A pseudorandom number generator for sampling.
    Returns:
        torch.Tensor: Last dimension contains num_samples indices
            sampled from the multinomial probability distribution
            located in the last dimension of tensor input.
    """
    input_ = input.reshape(-1, input.shape[-1])
    # TODO one day: the following leads to a sync point, which slows down a bit generation.
    output_ = torch.multinomial(input_, num_samples=num_samples, replacement=replacement, generator=generator)
    output = output_.reshape(*list(input.shape[:-1]), -1)
    return output


def sample_top_k(probs: torch.Tensor, k: int) -> torch.Tensor:
    """Sample next token from top K values along the last dimension of the input probs tensor.

    Args:
        probs (torch.Tensor): Input probabilities with token candidates on the last dimension.
        k (int): The k in “top-k”.
    Returns:
        torch.Tensor: Sampled tokens.
    """
    probs, indices = torch.topk(probs, k, dim=-1)
    next_token = multinomial(probs, num_samples=1)
    next_token = indices.gather(-1, next_token)
    return next_token


def sample_top_p(probs: torch.Tensor, p: float) -> torch.Tensor:
    """Sample next token from top P probabilities along the last dimension of the input probs tensor.

    Args:
        probs (torch.Tensor): Input probabilities with token candidates on the last dimension.
        p (int): The p in “top-p”.
    Returns:
        torch.Tensor: Sampled tokens.
    """
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    mask = probs_sum - probs_sort > p
    probs_sort *= (~mask).float()
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    next_token = multinomial(probs_sort, num_samples=1)
    next_token = torch.gather(probs_idx, -1, next_token)
    return next_token


class DummyPoolExecutor(Executor):
    """Dummy pool executor to use when we actually have only 1 worker.
    (e.g. instead of ProcessPoolExecutor).
    """
    def __init__(self) -> None:
        pass

    class DummyResult:
        def __init__(self, func, *args, **kwargs):
            self.func = func
            self.args = args
            self.kwargs = kwargs

        def result(self):
            return self.func(*self.args, **self.kwargs)

    def submit(self, func, *args, **kwargs):
        future = Future()
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            future.set_Exception(exc)
        else:
            future.set_result(result)
        return future

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        return


def get_pool_executor(num_workers: int, mp_context=None, use_threads: bool = False) -> Executor:
    """Convenience wrapper for easily switching between threads, process, or no pool at all."""
    if num_workers == 0:
        return DummyPoolExecutor()
    elif use_threads:
        return ThreadPoolExecutor(num_workers)
    else:
        return ProcessPoolExecutor(num_workers, mp_context)


def length_to_mask(lengths: torch.Tensor, max_len: tp.Optional[int] = None) -> torch.Tensor:
    """Utility function to convert a tensor of sequence lengths to a mask (useful when working on padded sequences).
    For example: [3, 5] => [[1, 1, 1, 0, 0], [1, 1, 1, 1, 1]]

    Args:
        lengths (torch.Tensor): tensor with lengths
        max_len (int): can set the max length manually. Defaults to None.
    Returns:
        torch.Tensor: mask with 0s where there is pad tokens else 1s
    """
    assert len(lengths.shape) == 1, "Length shape should be 1 dimensional."
    final_length = lengths.max().item() if not max_len else max_len
    final_length = max(final_length, 1)  # if all seqs are of len zero we don't want a zero-size tensor
    return torch.arange(final_length, device=lengths.device)[None, :] < lengths[:, None]


def hash_trick(word: str, vocab_size: int) -> int:
    """Hash trick to pair each word with an index

    Args:
        word (str): word we wish to convert to an index
        vocab_size (int): size of the vocabulary
    Returns:
        int: index of the word in the embedding LUT
    """
    hash = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
    return hash % vocab_size



# TODO: Move to flashy?
def copy_state(state: tp.Any, device: tp.Union[torch.device, str] = 'cpu',
               dtype: tp.Optional[torch.dtype] = None) -> tp.Any:
    if isinstance(state, torch.Tensor):
        if dtype is None or not state.is_floating_point():
            dtype = state.dtype
        return state.detach().to(device=device, dtype=dtype, copy=True)
    elif isinstance(state, dict):
        return {k: copy_state(v, device, dtype) for k, v in state.items()}
    elif isinstance(state, list):
        return [copy_state(v, device, dtype) for v in state]


# TODO: Move to flashy?
@contextmanager
def swap_state(model, state, **kwargs):
    old_state = copy_state(model.state_dict())
    model.load_state_dict(state, **kwargs)
    try:
        yield
    finally:
        model.load_state_dict(old_state)


@contextmanager
def maybe_no_grad(use_no_grad: bool):
    """Selectively use torch.no_grad context manager."""
    if use_no_grad:
        with torch.no_grad():
            yield
    else:
        yield


@lru_cache(None)
def warn_once(logger, msg):
    """Warn about a given message only once."""
    logger.warning(msg)


def is_jsonable(x: tp.Any):
    """Check if an object can be serialized into a json:"""
    try:
        json.dumps(x)
        return True
    except (TypeError, OverflowError):
        return False


def load_clap_state_dict(clap_model, path: tp.Union[str, Path]):
    """Wrapper around state dict loading of CLAP model
    addressing compatibility issues between CLAP and AudioCraft
    HuggingFace transformer version.
    See: https://github.com/LAION-AI/CLAP/issues/118
    """
    from clap_module.factory import load_state_dict  # type: ignore
    pkg = load_state_dict(path)
    pkg.pop('text_branch.embeddings.position_ids', None)
    clap_model.model.load_state_dict(pkg)


class GradNormGetter:
    """Efficient way to obtain the gradient norm for various subparts of the model.

    Args:
        categories: dict mapping from the name of the category of weights, to the list of weights
            in that category.
        fsdp_used: if True, will do one step of all reduce at the end.
        """
    def __init__(self, categories: tp.Dict[str, tp.Iterator[torch.Tensor]],
                 fsdp_used: bool = True):
        self.fsdp_used = fsdp_used
        self._categories_for_tensor: tp.Dict[torch.Tensor, tp.List[str]] = defaultdict(list)
        self._categories = {category: idx for idx, category in enumerate(sorted(categories.keys()))}
        device = None
        for name, tensors in categories.items():
            for tensor in tensors:
                self._categories_for_tensor[tensor].append(name)
                device = tensor.device
        assert device is not None
        self.device = device

    def __call__(self) -> tp.Tuple[tp.Dict[str, torch.Tensor], tp.List[torch.Tensor]]:
        """
        Returns the L2 norm of the gradient per category of weights, along with the list of gradients.
        """
        grad_norm2 = torch.zeros(len(self._categories), device=self.device, dtype=torch.float32)
        grads = []
        params = []
        norms = []
        for param in self._categories_for_tensor.keys():
            if param.grad is not None:
                grads.append(param.grad.data)
                params.append(param)
        norms = torch._foreach_norm(grads)
        for param, norm in zip(params, norms):
            categories = self._categories_for_tensor[param]
            norm2 = norm.pow(2)
            for category in categories:
                category_index = self._categories[category]
                grad_norm2[category_index] += norm2
        if self.fsdp_used:
            torch.distributed.all_reduce(grad_norm2)
        grad_norm = grad_norm2.sqrt()
        grad_norms = {category: grad_norm[index] for category, index in self._categories.items()}
        return grad_norms, grads


def get_seed_from_string(seed_str: str, n_bytes: int = 8) -> int:
    """Get a seed from a string. This is useful for setting a seed from a string
    that is not a number, e.g. a model name.

    Args:
        seed_str (str): Seed string.
        n_bytes (int): Number of bytes to use for the seed.
    Returns:
        int: Seed.
    """
    return int(hashlib.sha1(seed_str.encode()).hexdigest()[:n_bytes * 2], 16)


def product_dict(**kwargs):
    keys = kwargs.keys()
    for instance in itertools.product(*kwargs.values()):
        yield dict(zip(keys, instance))
