"""Rank fusion shared across the isolated management and AI services."""


def fuse_support_ranks(lexical: dict[str, float], dense: dict[str, float], *, threshold: float = 0.35) -> list[str]:
    sparse_rank = sorted(
        (key for key, score in lexical.items() if score >= 0.8), key=lambda key: lexical[key], reverse=True
    )
    dense_rank = sorted(
        (key for key in lexical if dense.get(key, -1) >= threshold), key=lambda key: dense[key], reverse=True
    )
    fused: dict[str, float] = {}
    for weight, ranking in ((1.0, sparse_rank), (1.3, dense_rank)):
        for rank, key in enumerate(ranking, 1):
            fused[key] = fused.get(key, 0.0) + weight / (60 + rank)
    return sorted(fused, key=lambda key: (fused[key], dense.get(key, -1), lexical[key]), reverse=True)
