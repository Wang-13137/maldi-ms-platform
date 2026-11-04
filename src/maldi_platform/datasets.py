"""数据中台：数据集登记、内容哈希、版本、字段与切分、导出门禁、血缘。

设计约束（与项目红线一致）：
1. **只登记元数据，不搬数据**。`storage_path` 是一个指针，中台不读数据内容、不做副本。
2. **真实数据默认不可导出**。`classification='real'` 的数据集，无论 `export_policy` 写什么，
   都只允许导出元数据（哈希、样本数、批次），payload 一律拒。
3. **哈希是版本主键**。内容变了哈希必变，同一 version 重复登记会被拒绝，避免"版本号相同、内容不同"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .db import now_iso

IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".idea", ".vscode", "node_modules"}

CLASSIFICATIONS = ("synthetic", "real")
EXPORT_POLICIES = ("open", "internal", "restricted")
EXPORT_TARGETS = ("metadata", "payload")


class DataError(RuntimeError):
    """数据中台的业务错误（重名、版本冲突、非法枚举）。"""


class ExportDeniedError(DataError):
    """导出被策略拒绝。异常信息要说清拒绝理由，方便审计。"""


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_path(path: str | Path) -> dict[str, Any]:
    """对文件或目录计算内容哈希。

    目录：按相对路径排序后逐文件取 sha256，再对整份清单取哈希；
    文件：清单只有一条。这样同内容不同拷贝顺序、不同机器都能得到同一哈希。

    返回 ``{"content_hash", "n_files", "files": [...]}``。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")

    if p.is_file():
        entries = [{"path": p.name, "sha256": _sha256_file(p), "size": p.stat().st_size}]
    else:
        files = [f for f in sorted(p.rglob("*")) if f.is_file() and not (set(f.parts) & IGNORE_DIRS)]
        entries = [
            {"path": f.relative_to(p).as_posix(), "sha256": _sha256_file(f), "size": f.stat().st_size}
            for f in files
        ]

    if not entries:
        raise DataError(f"目录为空，无法生成内容哈希：{p}")

    blob = json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "content_hash": hashlib.sha256(blob).hexdigest(),
        "n_files": len(entries),
        "files": entries,
    }


# --------------------------------------------------------------------------- #
# 数据集与版本
# --------------------------------------------------------------------------- #
def register_dataset(
    conn: sqlite3.Connection,
    name: str,
    *,
    classification: str = "synthetic",
    export_policy: str = "open",
    modality: str = "unknown",
    owner: str | None = None,
    description: str | None = None,
) -> int:
    """登记一个数据集（不含版本）。名字唯一，重复登记报错而不是静默覆盖。"""
    if not name.strip():
        raise DataError("数据集名字不能为空")
    if classification not in CLASSIFICATIONS:
        raise DataError(f"classification 必须是 {CLASSIFICATIONS}，收到 {classification!r}")
    if export_policy not in EXPORT_POLICIES:
        raise DataError(f"export_policy 必须是 {EXPORT_POLICIES}，收到 {export_policy!r}")
    # 真实数据不允许开放导出，登记阶段就拦住，避免下游误以为可下发
    if classification == "real" and export_policy == "open":
        raise DataError("真实数据不允许 export_policy='open'（只能 internal 或 restricted）")

    try:
        cur = conn.execute(
            "INSERT INTO datasets(name, modality, classification, export_policy, owner, description, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (name, modality, classification, export_policy, owner, description, now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        raise DataError(f"数据集已存在：{name}") from exc
    conn.commit()
    return int(cur.lastrowid or 0)


def add_version(
    conn: sqlite3.Connection,
    dataset_id: int,
    version: str,
    *,
    source: str | Path | None = None,
    storage_path: str | None = None,
    n_samples: int | None = None,
    acquisition_batch: str | None = None,
    notes: str | None = None,
) -> int:
    """登记一个数据版本。

    ``source`` 指向本地文件/目录，用于计算内容哈希（只读，不复制）；
    ``storage_path`` 是给人看的原始位置指针，通常与 ``source`` 相同或更粗略。
    两者都不填时视为"外部登记"，哈希记为 ``external:<version>``。
    """
    dataset = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if dataset is None:
        raise DataError(f"数据集不存在：id={dataset_id}")

    if source is not None:
        info = hash_path(source)
        content_hash = info["content_hash"]
        n_files = info["n_files"]
        if storage_path is None:
            storage_path = str(source)
    else:
        content_hash = f"external:{version}"
        n_files = 0

    try:
        cur = conn.execute(
            "INSERT INTO dataset_versions"
            "(dataset_id, version, content_hash, n_files, n_samples, acquisition_batch, storage_path, notes, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                version,
                content_hash,
                n_files,
                n_samples,
                acquisition_batch,
                storage_path,
                notes,
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DataError(f"版本已存在：dataset_id={dataset_id}, version={version}") from exc
    conn.commit()
    return int(cur.lastrowid or 0)


def add_field(
    conn: sqlite3.Connection,
    dataset_version_id: int,
    name: str,
    *,
    dtype: str | None = None,
    role: str | None = None,
    unit: str | None = None,
) -> int:
    """登记字段。role 限 feature/label/id/time/meta，便于下游自动识别标签列。"""
    if role is not None and role not in ("feature", "label", "id", "time", "meta"):
        raise DataError(f"role 非法：{role!r}")
    try:
        cur = conn.execute(
            "INSERT INTO dataset_fields(dataset_version_id, name, dtype, role, unit) VALUES(?, ?, ?, ?, ?)",
            (dataset_version_id, name, dtype, role, unit),
        )
    except sqlite3.IntegrityError as exc:
        raise DataError(f"字段已存在：version={dataset_version_id}, name={name}") from exc
    conn.commit()
    return int(cur.lastrowid or 0)


def add_split(
    conn: sqlite3.Connection,
    dataset_version_id: int,
    name: str,
    n_samples: int,
    *,
    seed: int | None = None,
    split_hash: str | None = None,
) -> int:
    """登记切分（train/val/test/fold0…）。同一版本内名字唯一。"""
    if n_samples < 0:
        raise DataError("n_samples 不能为负")
    try:
        cur = conn.execute(
            "INSERT INTO dataset_splits(dataset_version_id, name, n_samples, seed, split_hash)"
            " VALUES(?, ?, ?, ?, ?)",
            (dataset_version_id, name, n_samples, seed, split_hash),
        )
    except sqlite3.IntegrityError as exc:
        raise DataError(f"切分已存在：version={dataset_version_id}, name={name}") from exc
    conn.commit()
    return int(cur.lastrowid or 0)


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_datasets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.*, COUNT(v.id) AS n_versions
        FROM datasets d LEFT JOIN dataset_versions v ON v.dataset_id = d.id
        GROUP BY d.id ORDER BY d.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_dataset(conn: sqlite3.Connection, name_or_id: str | int) -> dict[str, Any]:
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        row = conn.execute("SELECT * FROM datasets WHERE id = ?", (int(name_or_id),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM datasets WHERE name = ?", (name_or_id,)).fetchone()
    if row is None:
        raise DataError(f"数据集不存在：{name_or_id}")
    return dict(row)


def list_versions(conn: sqlite3.Connection, dataset_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM dataset_versions WHERE dataset_id = ? ORDER BY id", (dataset_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def describe_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    """把一个版本的元数据装配成可直接进报告的结构。"""
    v = conn.execute("SELECT * FROM dataset_versions WHERE id = ?", (version_id,)).fetchone()
    if v is None:
        raise DataError(f"数据版本不存在：id={version_id}")
    d = conn.execute("SELECT * FROM datasets WHERE id = ?", (v["dataset_id"],)).fetchone()
    fields = conn.execute(
        "SELECT name, dtype, role, unit FROM dataset_fields WHERE dataset_version_id = ? ORDER BY id",
        (version_id,),
    ).fetchall()
    splits = conn.execute(
        "SELECT name, n_samples, seed, split_hash FROM dataset_splits WHERE dataset_version_id = ? ORDER BY id",
        (version_id,),
    ).fetchall()
    return {
        "version_id": version_id,
        "dataset": dict(d),
        "version": dict(v),
        "fields": [dict(r) for r in fields],
        "splits": [dict(r) for r in splits],
    }


# --------------------------------------------------------------------------- #
# 导出门禁
# --------------------------------------------------------------------------- #
def check_export(conn: sqlite3.Connection, version_id: int, target: str = "metadata") -> bool:
    """判断某数据版本是否允许以 ``target`` 形式导出（metadata / payload）。

    规则（从紧到松）：
    - ``target='metadata'``：任何数据集都允许（哈希、样本数、批次这些本身不敏感）。
    - ``target='payload'``：``real`` 一律拒绝；``internal`` / ``restricted`` 拒绝；仅 ``synthetic + open`` 放行。
    """
    if target not in EXPORT_TARGETS:
        raise DataError(f"target 必须是 {EXPORT_TARGETS}，收到 {target!r}")

    row = conn.execute(
        "SELECT d.classification, d.export_policy, d.name"
        " FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id WHERE v.id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise DataError(f"数据版本不存在：id={version_id}")

    if target == "metadata":
        return True

    if row["classification"] == "real":
        raise ExportDeniedError(
            f"拒绝导出真实数据 payload（dataset={row['name']}）：真实受试者数据只能登记元数据，"
            "原始谱与特征矩阵不得出域。"
        )
    if row["export_policy"] != "open":
        raise ExportDeniedError(
            f"拒绝导出 payload（dataset={row['name']}, export_policy={row['export_policy']}）："
            "策略非 open 的数据只允许内部使用。"
        )
    return True


def add_lineage(
    conn: sqlite3.Connection,
    *,
    src_kind: str,
    src_id: int,
    dst_kind: str,
    dst_id: int,
    relation: str,
) -> int:
    """记一条血缘边。评测运行引用数据版本时必须留痕，否则无法回答"这个结论出于哪份数据"。"""
    cur = conn.execute(
        "INSERT INTO lineage(src_kind, src_id, dst_kind, dst_id, relation, created_at) VALUES(?, ?, ?, ?, ?, ?)",
        (src_kind, src_id, dst_kind, dst_id, relation, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def lineage_of(conn: sqlite3.Connection, dst_kind: str, dst_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM lineage WHERE dst_kind = ? AND dst_id = ? ORDER BY id", (dst_kind, dst_id)
    ).fetchall()
    return [dict(r) for r in rows]


def iter_versions(conn: sqlite3.Connection) -> Iterable[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM dataset_versions ORDER BY id").fetchall()
    for r in rows:
        yield dict(r)


__all__ = [
    "DataError",
    "ExportDeniedError",
    "add_field",
    "add_lineage",
    "add_split",
    "add_version",
    "check_export",
    "describe_version",
    "get_dataset",
    "hash_path",
    "iter_versions",
    "lineage_of",
    "list_datasets",
    "list_versions",
    "register_dataset",
]
