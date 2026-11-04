"""看板测试：能离线打开、内容真实、HTML 转义正确。"""

from __future__ import annotations

from helpers import make_native
from maldi_platform import datasets, evals, report


def _seed(conn, data_dir, ids16):
    ds = datasets.register_dataset(conn, "synth-uti", modality="maldi-spectrum", description="合成扰动数据")
    v = datasets.add_version(conn, ds, "v1", source=data_dir, n_samples=37, acquisition_batch="B1")
    datasets.add_field(conn, v, "group", role="label")
    datasets.add_split(conn, v, "train", 30, seed=0)
    suite = evals.create_suite(conn, "m9-agent", dataset_version_id=v, task_type="agent-perturbation")
    (a,) = evals.ingest_native(conn, suite, make_native("A_full", ids16[:11], ids16))
    (c,) = evals.ingest_native(conn, suite, make_native("C_no_tool", [], ids16))
    evals.gate_min_pass_rate(conn, a, 0.6)
    evals.gate_no_regression(conn, c, a)
    return ds, v, suite, a, c


def test_html_contains_core_facts(conn, data_dir, ids16):
    _seed(conn, data_dir, ids16)
    html = report.build_html(conn)
    assert "synth-uti" in html
    assert "m9-agent" in html
    assert "A_full" in html
    assert "C_no_tool" in html
    assert "68.8%" in html  # 11/16
    assert "0.0010" in html  # McNemar p（看板保留 4 位）
    assert "差异显著" in html


def test_html_marks_real_dataset(conn, data_dir):
    datasets.register_dataset(conn, "real37", classification="real", export_policy="restricted")
    html = report.build_html(conn)
    assert "真实数据·仅元数据" in html


def test_html_escapes_user_input(conn, data_dir, ids16):
    ds = datasets.register_dataset(conn, "<script>alert(1)</script>")
    datasets.add_version(conn, ds, "v1", source=data_dir)
    html = report.build_html(conn)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_has_no_external_assets(conn, data_dir, ids16):
    _seed(conn, data_dir, ids16)
    html = report.build_html(conn)
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html.lower()


def test_html_handles_empty_db(conn):
    html = report.build_html(conn)
    assert "还没有登记任何数据集" in html
    assert "还没有评测套件" in html


def test_write_report_creates_file(conn, data_dir, ids16, tmp_path):
    _seed(conn, data_dir, ids16)
    out = tmp_path / "nested" / "dashboard.html"
    path = report.write_report(conn, out, title="测试看板")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "测试看板" in text
    assert text.startswith("<!DOCTYPE html>")
