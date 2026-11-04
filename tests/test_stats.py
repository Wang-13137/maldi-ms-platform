"""统计函数测试：区间与小样本精确检验是这套中台的地基，边界必须钉死。"""
from __future__ import annotations

import pytest

from maldi_platform.stats import mcnemar_exact, paired_table, rate_with_ci, verdict, wilson_ci


def test_wilson_ci_matches_known_small_sample():
    """11/16 的 Wilson 区间应约为 [0.44, 0.86]，这是本项目 64 条实测里每个臂的真实规模。"""
    lo, hi = wilson_ci(11, 16)
    assert 0.43 < lo < 0.46
    assert 0.85 < hi < 0.88
    assert lo < 11 / 16 < hi


def test_wilson_ci_zero_success_has_zero_lower_bound():
    lo, hi = wilson_ci(0, 16)
    assert lo == 0.0
    assert 0.18 < hi < 0.21


def test_wilson_ci_all_success_has_one_upper_bound():
    lo, hi = wilson_ci(16, 16)
    assert hi == 1.0
    assert 0.78 < lo < 0.82


def test_wilson_ci_never_exceeds_unit_interval():
    """Wald 区间在这里会给出负数下界，Wilson 不会——这就是选它的原因。"""
    for n in (1, 2, 5, 16, 64):
        for k in range(n + 1):
            lo, hi = wilson_ci(k, n)
            assert 0.0 <= lo <= hi <= 1.0


def test_wilson_ci_rejects_invalid_input():
    with pytest.raises(ValueError):
        wilson_ci(1, 0)
    with pytest.raises(ValueError):
        wilson_ci(5, 4)


def test_mcnemar_exact_11_vs_0():
    """真实实测 A vs C：仅 A 过 11、仅 C 过 0，精确 p = 2/2^11 ≈ 0.000977。

    这个数比卡方近似（≈0.0003）大，小样本上近似会高估显著性。
    """
    assert mcnemar_exact(11, 0) == pytest.approx(2 / 2 ** 11, rel=1e-6)


def test_mcnemar_exact_3_vs_0():
    assert mcnemar_exact(3, 0) == pytest.approx(0.25, rel=1e-6)


def test_mcnemar_exact_balanced_discordant_is_one():
    assert mcnemar_exact(5, 5) == 1.0


def test_mcnemar_exact_no_discordant_pairs_is_one():
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_exact_symmetric():
    assert mcnemar_exact(7, 2) == mcnemar_exact(2, 7)


def test_mcnemar_exact_rejects_negative():
    with pytest.raises(ValueError):
        mcnemar_exact(-1, 0)


def test_paired_table_counts():
    a = {"x": True, "y": True, "z": False, "w": False}
    b = {"x": True, "y": False, "z": True, "w": False}
    t = paired_table(a, b)
    assert t == {"n_paired": 4, "a_only": 1, "b_only": 1, "both": 1, "neither": 1}


def test_paired_table_only_counts_shared_cases():
    """两次运行样本不完全一致时，只比较交集——不能把缺的 case 当失败。"""
    t = paired_table({"a": True, "b": False}, {"a": True})
    assert t["n_paired"] == 1
    assert t["both"] == 1


def test_rate_with_ci_shape():
    r = rate_with_ci(11, 16)
    assert r["n"] == 16 and r["k"] == 11
    assert r["rate"] == pytest.approx(0.6875)
    assert r["ci_low"] < r["rate"] < r["ci_high"]


def test_verdict_threshold():
    assert verdict(0.001) == "差异显著"
    assert verdict(0.07) == "差异不显著"
