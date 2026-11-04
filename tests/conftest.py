"""共用 fixture：临时库、临时数据目录、两种评测结果格式。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from helpers import ids16 as _ids16  # noqa: E402
from maldi_platform.db import init_db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture()
def data_dir(tmp_path):
    """造一个"数据集目录"，内容固定，便于断言哈希可复现。"""
    d = tmp_path / "dataset_v1"
    d.mkdir()
    (d / "spectra.csv").write_text("mz,intensity\n100,1.0\n101,2.0\n", encoding="utf-8")
    (d / "labels.csv").write_text("sample,group\nS1,case\nS2,control\n", encoding="utf-8")
    return d


@pytest.fixture()
def ids16():
    return _ids16()


@pytest.fixture()
def native_file(tmp_path):
    def _make(payload: dict[str, Any], name: str = "run.json") -> Path:
        p = tmp_path / name
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    return _make
