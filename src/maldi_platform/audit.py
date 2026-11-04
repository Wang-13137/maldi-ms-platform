"""审计日志：谁、在什么时候、对哪份数据做了什么、结果是成功还是被拒。

为什么数据平台必须有这张表：真实受试者数据能不能导出、被谁导出了，事后要能回答。
日志只追加（库层用触发器挡住 UPDATE / DELETE），因此它同时是"操作记录"和"证据"。

设计上的两个取舍：
- **不记录数据内容**，只记目标标识与结论，避免审计表本身变成泄露面；
- **写入失败不静默**：审计写不进去就说明库坏了，此时业务操作也不该被当成成功。
"""

from __future__ import annotations

import getpass
import os
import sqlite3
from typing import Any

from .db import now_iso

OUTCOMES = ("ok", "denied", "failed")

#: 操作者：CI 里用环境变量注入，本地默认当前系统用户。
#: 不追求强身份认证——这一版是单机工具，强身份应该由部署层的 SSO 提供，
#: 这里只保证"换人操作会留下不同的名字"，不做安全承诺。
ACTOR_ENV = "MALDI_PLATFORM_ACTOR"


class AuditError(RuntimeError):
    """审计写入失败。业务方必须当作失败处理，不能吞掉。"""


def resolve_actor(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get(ACTOR_ENV, "").strip()
    if env:
        return env
    try:
        return getpass.getuser() or "unknown"
    except Exception:  # pragma: no cover - 少数无用户环境（如精简容器）
        return "unknown"


def record(
    conn: sqlite3.Connection,
    action: str,
    *,
    target: str | None = None,
    outcome: str = "ok",
    detail: str | None = None,
    actor: str | None = None,
) -> int:
    """追加一条审计记录，返回其 id。``outcome`` 只允许 ok / denied / failed。"""
    if outcome not in OUTCOMES:
        raise AuditError(f"未知的审计结论：{outcome}（只允许 {', '.join(OUTCOMES)}）")
    try:
        cur = conn.execute(
            "INSERT INTO audit_log(ts, actor, action, target, outcome, detail) VALUES(?, ?, ?, ?, ?, ?)",
            (now_iso(), resolve_actor(actor), action, target, outcome, detail),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise AuditError(f"审计写入失败：{exc}") from exc
    return int(cur.lastrowid or 0)


def list_entries(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    action: str | None = None,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    """按时间倒序列出审计记录。"""
    sql = "SELECT * FROM audit_log"
    conds: list[str] = []
    params: list[Any] = []
    if action:
        conds.append("action = ?")
        params.append(action)
    if outcome:
        conds.append("outcome = ?")
        params.append(outcome)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    return int(row["n"])


__all__ = ["ACTOR_ENV", "OUTCOMES", "AuditError", "count", "list_entries", "record", "resolve_actor"]
