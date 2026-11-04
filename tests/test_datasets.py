"""数据中台测试：哈希可复现、版本不可覆盖、真实数据导不出 payload。"""
from __future__ import annotations

import shutil

import pytest

from maldi_platform import datasets
from maldi_platform.datasets import DataError, ExportDenied


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #
def test_register_and_list(conn):
    ds_id = datasets.register_dataset(conn, "synth-uti", modality="maldi-spectrum")
    rows = datasets.list_datasets(conn)
    assert ds_id == rows[0]["id"]
    assert rows[0]["name"] == "synth-uti"
    assert rows[0]["n_versions"] == 0


def test_duplicate_name_rejected(conn):
    datasets.register_dataset(conn, "dup")
    with pytest.raises(DataError):
        datasets.register_dataset(conn, "dup")


def test_blank_name_rejected(conn):
    with pytest.raises(DataError):
        datasets.register_dataset(conn, "   ")


def test_invalid_classification_rejected(conn):
    with pytest.raises(DataError):
        datasets.register_dataset(conn, "x", classification="unknown")


def test_real_with_open_policy_rejected_at_registration(conn):
    """真实数据不允许登记成 open——在入口拦住，别指望下游自觉。"""
    with pytest.raises(DataError):
        datasets.register_dataset(conn, "real-open", classification="real", export_policy="open")


# --------------------------------------------------------------------------- #
# 哈希与版本
# --------------------------------------------------------------------------- #
def test_version_hash_is_reproducible_across_copies(conn, data_dir, tmp_path):
    """同一份内容换个目录、换个顺序拷，哈希必须一样。"""
    copy = tmp_path / "another_copy"
    shutil.copytree(data_dir, copy)

    ds_id = datasets.register_dataset(conn, "d")
    v1 = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    info1 = datasets.describe_version(conn, v1)

    ds2 = datasets.register_dataset(conn, "d2")
    v2 = datasets.add_version(conn, ds2, "v1", source=copy)
    info2 = datasets.describe_version(conn, v2)

    assert info1["version"]["content_hash"] == info2["version"]["content_hash"]
    assert info1["version"]["n_files"] == 2


def test_hash_changes_when_content_changes(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v1 = datasets.add_version(conn, ds_id, "v1", source=data_dir)

    (data_dir / "spectra.csv").write_text("mz,intensity\n100,9.9\n", encoding="utf-8")
    v2 = datasets.add_version(conn, ds_id, "v2", source=data_dir)

    h1 = datasets.describe_version(conn, v1)["version"]["content_hash"]
    h2 = datasets.describe_version(conn, v2)["version"]["content_hash"]
    assert h1 != h2


def test_duplicate_version_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    datasets.add_version(conn, ds_id, "v1", source=data_dir)
    with pytest.raises(DataError):
        datasets.add_version(conn, ds_id, "v1", source=data_dir)


def test_version_of_missing_dataset_rejected(conn, data_dir):
    with pytest.raises(DataError):
        datasets.add_version(conn, 999, "v1", source=data_dir)


def test_external_version_without_source(conn):
    """没有本地文件时也能登记（外部数据），哈希降级为 external:<version> 而不是假的哈希。"""
    ds_id = datasets.register_dataset(conn, "ext")
    v = datasets.add_version(conn, ds_id, "2026-11-04", storage_path=r"D:\data\extern")
    info = datasets.describe_version(conn, v)
    assert info["version"]["content_hash"] == "external:2026-11-04"
    assert info["version"]["n_files"] == 0


def test_empty_dir_rejected(conn, tmp_path):
    ds_id = datasets.register_dataset(conn, "d")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DataError):
        datasets.add_version(conn, ds_id, "v1", source=empty)


def test_missing_path_rejected(conn, tmp_path):
    ds_id = datasets.register_dataset(conn, "d")
    with pytest.raises(FileNotFoundError):
        datasets.add_version(conn, ds_id, "v1", source=tmp_path / "nope")


def test_metadata_fields_and_splits(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d", modality="maldi-spectrum")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir, n_samples=37, acquisition_batch="B1")
    datasets.add_field(conn, v, "sample_id", role="id")
    datasets.add_field(conn, v, "group", role="label", dtype="category")
    datasets.add_field(conn, v, "peak_47", role="feature", unit="Da")
    datasets.add_split(conn, v, "train", 30, seed=0, split_hash="abc123")
    datasets.add_split(conn, v, "test", 7, seed=0, split_hash="def456")

    info = datasets.describe_version(conn, v)
    assert [f["name"] for f in info["fields"]] == ["sample_id", "group", "peak_47"]
    assert len(info["splits"]) == 2
    assert info["version"]["acquisition_batch"] == "B1"


def test_invalid_field_role_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    with pytest.raises(DataError):
        datasets.add_field(conn, v, "x", role="whatever")


def test_duplicate_field_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    datasets.add_field(conn, v, "group", role="label")
    with pytest.raises(DataError):
        datasets.add_field(conn, v, "group", role="feature")


def test_duplicate_split_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    datasets.add_split(conn, v, "train", 30)
    with pytest.raises(DataError):
        datasets.add_split(conn, v, "train", 31)


def test_negative_split_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    with pytest.raises(DataError):
        datasets.add_split(conn, v, "train", -1)


# --------------------------------------------------------------------------- #
# 导出门禁
# --------------------------------------------------------------------------- #
def test_metadata_export_allowed_even_for_real(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "real37", classification="real", export_policy="restricted")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir, n_samples=37)
    assert datasets.check_export(conn, v, "metadata") is True


def test_real_payload_export_denied(conn, data_dir):
    """红线：真实数据原始谱与特征矩阵不得出域，payload 一律拒。"""
    ds_id = datasets.register_dataset(conn, "real37", classification="real", export_policy="restricted")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir, n_samples=37)
    with pytest.raises(ExportDenied) as exc:
        datasets.check_export(conn, v, "payload")
    assert "真实" in str(exc.value)


def test_internal_policy_payload_denied(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "synth", export_policy="internal")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    with pytest.raises(ExportDenied):
        datasets.check_export(conn, v, "payload")


def test_synthetic_open_payload_allowed(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "synth", classification="synthetic", export_policy="open")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    assert datasets.check_export(conn, v, "payload") is True


def test_invalid_export_target_rejected(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "synth")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    with pytest.raises(DataError):
        datasets.check_export(conn, v, "everything")


def test_export_check_missing_version(conn):
    with pytest.raises(DataError):
        datasets.check_export(conn, 12345, "metadata")


# --------------------------------------------------------------------------- #
# 血缘
# --------------------------------------------------------------------------- #
def test_lineage_recorded_and_read_back(conn, data_dir):
    ds_id = datasets.register_dataset(conn, "d")
    v = datasets.add_version(conn, ds_id, "v1", source=data_dir)
    datasets.add_lineage(conn, src_kind="dataset_version", src_id=v, dst_kind="run", dst_id=7, relation="consumed_by")
    edges = datasets.lineage_of(conn, "run", 7)
    assert len(edges) == 1
    assert edges[0]["src_id"] == v
    assert edges[0]["relation"] == "consumed_by"


def test_get_dataset_by_name_and_id(conn):
    ds_id = datasets.register_dataset(conn, "by-name")
    assert datasets.get_dataset(conn, "by-name")["id"] == ds_id
    assert datasets.get_dataset(conn, ds_id)["name"] == "by-name"
    with pytest.raises(DataError):
        datasets.get_dataset(conn, "nope")
