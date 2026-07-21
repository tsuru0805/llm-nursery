# -*- coding: utf-8 -*-
"""家庭词块索引(见 AGENTS.md)。

从孩子的**真实语料**里提取高频短片段("抱抱""不要走"),供 decoder 整词起头——
模型本体零改动(词块只是 speak 的 seed 软通道),护栏/词汇解锁照跑。
chunk_index 是**派生数据**:任何时刻可全量重建,坏了删掉重建即可,不进备份关键面。

睡眠整理(consolidate_daily):每天 07:00 后首拍重建一次(=夜里把白天听的话
变成自己的);部署后 meta 缺行=当拍立即引导重建。全部纯标准库(§14 开源纪律)。
"""
from __future__ import annotations

import json
import re
import time

from . import config as cfg

# 词块里不许出现的字符:标点/空白/行界(只要"话芯",不要断句符)
_BAD_CHAR = re.compile(r"[\s\d\W]", re.UNICODE)

_META_KEY = "chunks_consolidated_date"


def _clean_runs(text: str) -> list[str]:
    """切成纯"话芯"连续段(标点/空白/数字处断开)。"""
    return [r for r in _BAD_CHAR.split(text) if len(r) >= cfg.CHUNK_MIN_LEN]


def extract_chunks(rows) -> list[dict]:
    """语料行 → 词块表。rows=可迭代 (text, scene, training_weight)。

    口径:窗口 CHUNK_MIN_LEN..CHUNK_MAX_LEN 的加权出现计数;达标
    (≥CHUNK_MIN_COUNT)后做子串吸收(长块次数 ≥ 短块×CHUNK_ABSORB_RATIO ⇒
    短块被吸收;短块计数恒≥长块),防"抱/抱抱/抱抱抱"三代同堂;
    取权重 top CHUNK_TOP_MAX。"""
    counts: dict[str, float] = {}
    scenes: dict[str, dict] = {}
    for text, scene, weight in rows:
        w = max(0.1, float(weight if weight is not None else 1.0))
        for run in _clean_runs(text or ""):
            for n in range(cfg.CHUNK_MIN_LEN, min(cfg.CHUNK_MAX_LEN, len(run)) + 1):
                for i in range(len(run) - n + 1):
                    ck = run[i:i + n]
                    counts[ck] = counts.get(ck, 0.0) + w
                    if scene:
                        sc = scenes.setdefault(ck, {})
                        sc[scene] = sc.get(scene, 0.0) + w
    kept = {ck: c for ck, c in counts.items() if c >= cfg.CHUNK_MIN_COUNT}
    # 子串吸收:按长度降序,已保留的长块吸收其所有达比例子串
    survivors: dict[str, float] = {}
    for ck in sorted(kept, key=lambda x: (-len(x), -kept[x])):
        absorbed = False
        for longer in survivors:
            if ck in longer and kept[ck] <= survivors[longer] / cfg.CHUNK_ABSORB_RATIO:
                absorbed = True
                break
        if not absorbed:
            survivors[ck] = kept[ck]
    top = sorted(survivors.items(), key=lambda kv: (-kv[1] * len(kv[0]), kv[0]))
    out = []
    for ck, c in top[: cfg.CHUNK_TOP_MAX]:
        out.append({"chunk": ck, "weight": c * len(ck),
                    "scenes": scenes.get(ck, {})})
    return out


def rebuild_index(conn, child_id: str, now: float | None = None) -> int:
    """全量重建 chunk_index(幂等;派生数据先删后插)。调用方管事务外壳——
    本函数自带顶层事务,禁止嵌套调用(与 child.tx 同纪律)。"""
    from .child import _now, tx
    t = _now(now)
    with tx(conn):
        # 读-算-替换同事务(评审:写锁内读,重建期间落进来的
        # 新语料要么在本次索引里,要么排队等锁——不会"漏了还标记当日已整理"
        rows = conn.execute(
            "SELECT text, scene, training_weight FROM corpus_item WHERE child_id=?"
            " ORDER BY id", (child_id,)).fetchall()
        chunks = extract_chunks((r["text"], r["scene"], r["training_weight"])
                                for r in rows)
        conn.execute("DELETE FROM chunk_index WHERE child_id=?", (child_id,))
        for c in chunks:
            conn.execute(
                "INSERT INTO chunk_index(child_id, chunk, weight, scenes_json,"
                " updated_at) VALUES(?,?,?,?,?)",
                (child_id, c["chunk"], c["weight"],
                 json.dumps(c["scenes"], ensure_ascii=False) if c["scenes"] else None,
                 t))
        conn.execute(
            "INSERT INTO parenting_meta(child_id, key, value, updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(child_id, key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (child_id, _META_KEY, time.strftime("%Y-%m-%d", time.localtime(t)), t))
    return len(chunks)


def consolidate_daily(conn, child_id: str, now: float | None = None) -> int | None:
    """睡眠整理入口(scheduler 每拍调,自守闸):
    - meta 缺行(部署后首拍)⇒ 立即引导重建;
    - 当日已整理 ⇒ 跳过(None);
    - 本地时刻 < CONSOLIDATE_AFTER_H(他还在睡/夜里)⇒ 跳过(None)。
    返回重建后的词块数,跳过=None。任何异常由调用方吞(tick 不许炸)。"""
    from .child import _now
    t = _now(now)
    row = conn.execute(
        "SELECT value FROM parenting_meta WHERE child_id=? AND key=?",
        (child_id, _META_KEY)).fetchone()
    today = time.strftime("%Y-%m-%d", time.localtime(t))
    if row is not None:
        if row["value"] == today:
            return None
        if time.localtime(t).tm_hour < cfg.CONSOLIDATE_AFTER_H:
            return None
    return rebuild_index(conn, child_id, now=t)


def pick_chunk(conn, child_id: str, rng, scene_hint: tuple | None = None) -> str | None:
    """按权重(场景命中 ×CHUNK_SCENE_BOOST)从索引抽一个词块;空索引=None。"""
    rows = conn.execute(
        "SELECT chunk, weight, scenes_json FROM chunk_index WHERE child_id=?"
        " ORDER BY weight DESC LIMIT ?", (child_id, cfg.CHUNK_PICK_POOL)).fetchall()
    if not rows:
        return None
    chunks, weights = [], []
    for r in rows:
        w = max(0.001, float(r["weight"]))
        if scene_hint:
            try:
                sc = json.loads(r["scenes_json"] or "{}")
            except ValueError:
                sc = {}
            if any(s in sc for s in scene_hint):
                w *= cfg.CHUNK_SCENE_BOOST
        chunks.append(r["chunk"])
        weights.append(w)
    return rng.choices(chunks, weights=weights, k=1)[0]
