# -*- coding: utf-8 -*-
"""偷学抽样:孩子趁"睡觉"从照护人的聊天存档里偷学语料("语出惊人"的原料)。

语料源是一个外部 SQLite 库(env NURSERY_ARCHIVE_DB),约定最小 schema:

    CREATE TABLE windows (
        id     TEXT PRIMARY KEY,   -- 窗口 ID(任意唯一串)
        viewer TEXT NOT NULL,      -- 归属人(与 driver 的 persona 键一致)
        text   TEXT NOT NULL       -- 该窗口的对话原文
    );

把你自己的聊天记录导成这张表就能接上;不配 NURSERY_ARCHIVE_DB 则偷学整体停用。

只读硬闸(一条都不许松):
- `file:...?mode=ro` + uri=True + `PRAGMA query_only=ON`,绝不复用会建 schema 的写端
- 查询必须带 viewer=?(孩子只偷自己照护人的窗)
- 任何失败 fail closed:本轮不偷学,不改已有模型(异常上抛由调用方吞掉跳过)

抽出来的是 30-120 字**小片段**,本地库只存片段+ref(win_id@offset+len),
不复制整窗原文。同窗冷却由调用方按 source_ref 前缀去重(corpus content_hash 兜底)。
"""
from __future__ import annotations

import random
import sqlite3


def connect_archive(archive_path: str) -> sqlite3.Connection:
    """只读打开语料存档库。路径必填;打不开就 raise(调用方 fail closed)。"""
    if not archive_path:
        raise ValueError("archive_path 必填(env NURSERY_ARCHIVE_DB)")
    conn = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("SELECT 1 FROM windows LIMIT 1")  # 结构探活,坏库早炸
    return conn


def sample_fragments(archive: sqlite3.Connection, viewer: str, n: int,
                     rng: random.Random, *, min_len: int = 30, max_len: int = 120,
                     exclude_refs: set[str] | None = None,
                     pool_factor: int = 8) -> list[dict]:
    """从 viewer 自己的窗里随机抽 n 个片段。返回 [{ref, text}]。

    exclude_refs:已偷过的 source_ref 集合(同窗冷却:同一窗已出现即跳过)。
    """
    if not viewer:
        raise ValueError("viewer 必填(孩子只偷自己照护人的窗)")
    exclude_wins = {r.split("@", 1)[0] for r in (exclude_refs or set())}
    rows = archive.execute(
        "SELECT id, text FROM windows WHERE viewer=? ORDER BY RANDOM() LIMIT ?",
        (viewer, max(n * pool_factor, n))).fetchall()
    out: list[dict] = []
    for row in rows:
        if len(out) >= n:
            break
        if row["id"] in exclude_wins:
            continue
        body = row["text"].strip()
        if len(body) < min_len:
            continue
        frag_len = rng.randint(min_len, min(max_len, len(body)))
        offset = rng.randint(0, len(body) - frag_len)
        frag = body[offset:offset + frag_len].strip()
        if len(frag) < min_len:
            continue
        out.append({"ref": f"{row['id']}@{offset}+{frag_len}", "text": frag})
        exclude_wins.add(row["id"])
    return out
