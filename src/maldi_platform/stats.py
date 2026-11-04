"""统计工具：Wilson 置信区间、McNemar 精确检验、比率差值区间。

刻意不依赖 scipy：中台要能在客户现场/离线的机器上一条 ``pip install -e .`` 就跑起来，
而这里用到的分布只有二项分布，``math.comb`` 足够。
"""

from __future__ import annotations

import math

Z_95 = 1.959963984540054


def wilson_ci(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """二项比例的 Wilson 区间。

    小样本（n=16 这种）下比正态近似靠谱得多——本项目 64 条实测里每组只有 16 条，
    用 Wald 区间会出现 0% 的下界跑到负数、100% 的上界超过 1 的荒唐结果。
    """
    if n <= 0:
        raise ValueError("n 必须为正")
    if not 0 <= k <= n:
        raise ValueError("要求 0 <= k <= n")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return round(lo, 8), round(hi, 8)


def mcnemar_exact(b: int, c: int) -> float:
    """配对 McNemar 精确检验（双侧）。

    ``b``：A 通过而 B 不通过的样本数；``c``：A 不通过而 B 通过的样本数。
    原假设是两者错误率相同，即不一致格在 b+c 次里各占一半：
    双侧 p = 2 * P(X <= min(b, c))，X ~ Bin(b + c, 0.5)，上限 1。

    为什么不用卡方近似：b+c 常常只有个位数（本项目 A vs C 是 b=11, c=0），
    近似在这么小的格子里不可信，会给出偏小的 p。
    """
    if b < 0 or c < 0:
        raise ValueError("b/c 不能为负")
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    p_value: float = round(min(1.0, 2 * tail), 10)
    return p_value


def paired_table(a_passed: dict[str, bool], b_passed: dict[str, bool]) -> dict[str, int]:
    """按 case_id 取交集算 2x2 表：a_only / b_only / both / neither。"""
    shared = sorted(set(a_passed) & set(b_passed))
    a_only = sum(1 for k in shared if a_passed[k] and not b_passed[k])
    b_only = sum(1 for k in shared if b_passed[k] and not a_passed[k])
    both = sum(1 for k in shared if a_passed[k] and b_passed[k])
    neither = sum(1 for k in shared if not a_passed[k] and not b_passed[k])
    return {
        "n_paired": len(shared),
        "a_only": a_only,
        "b_only": b_only,
        "both": both,
        "neither": neither,
    }


def rate_with_ci(k: int, n: int) -> dict[str, float | int]:
    """把 k/n 打包成报告要的形状。"""
    lo, hi = wilson_ci(k, n)
    return {"n": n, "k": k, "rate": round(k / n, 6) if n else 0.0, "ci_low": lo, "ci_high": hi}


def verdict(p: float, alpha: float = 0.05) -> str:
    """只输出统计结论，不替用户做业务判断。"""
    if p < alpha:
        return "差异显著"
    return "差异不显著"


__all__ = ["Z_95", "mcnemar_exact", "paired_table", "rate_with_ci", "verdict", "wilson_ci"]
