import torch


def masked_mean(x: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    # x: [batch, time, dim] float; mask: [batch, time] bool
    out = []
    for b in range(x.shape[0]):
        rows = x[b][mask[b]]
        out.append(rows.mean(0) if len(rows) else torch.zeros(x.shape[2], dtype=x.dtype, device=x.device))
    return torch.stack(out)
