from typing import Optional, Tuple

import torch


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


def sdpa_attention_forward(
    module: torch.nn.Module,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attention_mask: Optional[torch.Tensor],
    dropout: float = 0.0,
    scaling: Optional[float] = None,
    is_causal: Optional[bool] = None,
    cache=None,
    cumulative_seqlens_q=None,
    cumulative_seqlens_k=None,
    **kwargs,
) -> Tuple[torch.Tensor, None]:
    key, value = cache.update(key, value, module.layer_idx, cumulative_seqlens_k, **kwargs)
    attention_mask_ = torch.full(
            [1, 1, query.shape[2],key.shape[2]+1], torch.finfo(query.dtype).min, device=query.device, dtype=query.dtype
    )
    attention_mask_[0,0, cumulative_seqlens_q[0], 0 : cumulative_seqlens_k[0]] = 0
    for i in range(1, len(cumulative_seqlens_k)):
        attention_mask_[..., cumulative_seqlens_q[i - 1] : cumulative_seqlens_q[i], cumulative_seqlens_k[i - 1] : cumulative_seqlens_k[i]] = 0
    attention_mask_[..., cumulative_seqlens_q[i]:, cumulative_seqlens_k[i]:] = 0

    if attention_mask.shape == attention_mask_.shape:
        attention_mask_.masked_fill_(attention_mask!=0, torch.finfo(query.dtype).min)
    if hasattr(module, "num_key_value_groups"):
        key = repeat_kv(key, module.num_key_value_groups)
        value = repeat_kv(value, module.num_key_value_groups)

    causal_mask = attention_mask_
    if attention_mask is not None:
        causal_mask = causal_mask[:, :, :, : key.shape[-2]]

    # SDPA with memory-efficient backend is bugged with non-contiguous inputs and custom attn_mask for some torch versions
    # Reference: https://github.com/pytorch/pytorch/issues/112577.
    query = query.contiguous()
    key = key.contiguous()
    value = value.contiguous()

    # We dispatch to SDPA's Flash Attention or Efficient kernels via this `is_causal` if statement instead of an inline conditional assignment
    # in SDPA to support both torch.compile's dynamic shapes and full graph options. An inline conditional prevents dynamic shapes from compiling.
    if is_causal is None:
        is_causal = causal_mask is None and query.shape[2] > 1 and False

    attn_output = torch.nn.functional.scaled_dot_product_attention(
        query,
        key,
        value,
        attn_mask=causal_mask,
        dropout_p=dropout,
        scale=scaling,
        is_causal=is_causal,
    )
    attn_output = attn_output.transpose(1, 2).contiguous()

    return attn_output, None
