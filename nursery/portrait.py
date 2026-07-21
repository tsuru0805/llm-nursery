# -*- coding: utf-8 -*-
"""成长画像(见 AGENTS.md)。

「它为什么长成这样」——从全程养育数据**纯派生**一份结构化画像:三轴与趋势/
对爸妈分账的四维关系/夜哭响应模式/语料构成(来源·声部·场景)/语言面(词汇量·
拒绝率·涌现率·家常词块)/关键第一次清单。**只读不写,零状态**;数值给事实与
趋势,不打人格标签(纪律)——把事实讲成话是告别信(灵魂件)与围观台的事。

消费方:
- driver `portrait` 指令(JSON 面,接入层/围观页按调用拉起;照护指令
  白名单不含=孩子面零暴露,这是给旁观者看的);
- 告别信/结局叙事生成以本画像为唯一事实源(只引用画像里有的,没有的不许编)。
"""
from __future__ import annotations

import json

from . import config as cfg
from .bond import bond_trends, read_bond
from .psyche import axis_trends, read_axes


def _night_stats(conn, child_id: str, t: float) -> dict:
    """逐夜口径=events.closed_cry_nights(与忽视账同源单一权威;只算已完结的
    fired 主哭夜,进行中的夜不计)。judge_ending 的粗比率=v1 判分口径,另账不混。"""
    from .events import closed_cry_nights
    nights = closed_cry_nights(conn, child_id, t)
    cries = len(nights)
    responded = sum(1 for n in nights if n["responded"])
    return {"cries": cries, "responded": responded,
            "response_rate": round(responded / cries, 2) if cries else None}


def _corpus_stats(conn, child_id: str) -> dict:
    total = conn.execute(
        "SELECT COALESCE(SUM(char_count),0) FROM corpus_item WHERE child_id=?",
        (child_id,)).fetchone()[0]
    by = {}
    for col in ("source_kind", "speaker", "scene"):
        by[col] = {str(r[0] if r[0] is not None else "legacy"): r[1]
                   for r in conn.execute(
                       f"SELECT {col}, COUNT(*) FROM corpus_item WHERE child_id=?"
                       f" GROUP BY {col}", (child_id,))}
    return {"total_chars": total, "by_source": by["source_kind"],
            "by_speaker": by["speaker"], "by_scene": by["scene"]}


def _language_stats(conn, brain, child_id: str) -> dict:
    utt = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=?", (child_id,)).fetchone()[0]
    accepted = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=? AND accepted=1",
        (child_id,)).fetchone()[0]
    refused = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=?"
        " AND rejection_reason='refused'", (child_id,)).fetchone()[0]
    novel = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=? AND accepted=1"
        " AND stage!='infant' AND LENGTH(text)>=6 AND max_source_overlap<=3",
        (child_id,)).fetchone()[0]
    chunks = [r["chunk"] for r in conn.execute(
        "SELECT chunk FROM chunk_index WHERE child_id=? ORDER BY weight DESC"
        " LIMIT 5", (child_id,))]
    return {"vocab": len(brain.model.vocab_by_freq()) if brain else None,
            "utterances": utt,
            "refusal_rate": round(refused / utt, 3) if utt else None,
            "novel_rate": round(novel / accepted, 3) if accepted else None,
            "family_chunks": chunks}


def _firsts(conn, child_id: str) -> list[dict]:
    """关键第一次清单(§八:形成这些特征的关键事件)。"""
    return [{"kind": r["item_kind"], "title": r["title"], "note": r["note"],
             "at": r["created_at"]}
            for r in conn.execute(
                "SELECT item_kind, title, note, created_at FROM growth_album"
                " WHERE child_id=? AND (item_kind LIKE 'first_%'"
                " OR item_kind LIKE 'appearance_%') ORDER BY id", (child_id,))]


def build_portrait(conn, brain, child_id: str, now: float | None = None) -> dict:
    """全量画像(纯读)。brain 可为 None(词汇量置 None,其余照出)。"""
    from . import child as child_mod
    t = child_mod._now(now)
    child = child_mod.get_child(conn, child_id)
    stage = child_mod.stage_of(child, t)
    state = child_mod.read_state(conn, child_id, now=t, persist=False) \
        if child["status"] != "embryo" else {}
    axes = read_axes(conn, child_id)
    atr = axis_trends(conn, child_id, t)
    meta = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM parenting_meta WHERE child_id=?"
        " AND key IN ('bond_initialized_from_history','bond_confidence')",
        (child_id,))}
    return {
        "generated_at": t,
        "name": child["name"], "stage": stage,
        "age_days": round(child_mod.logical_age_days(child, t), 1),
        "status": child["status"], "ending": child["ending"],
        "appearance": child["appearance"],
        "axes": {a: {"cn": cfg.PSYCHE_CN[a], "value": round(axes[a], 1),
                     "trend": atr[a]} for a in cfg.PSYCHE_AXES},
        "darkness": round(state.get("darkness", 0.0), 1) if state else None,
        "digest_load": round(state.get("digest_load", 0.0), 1) if state else None,
        "bond": {
            "dims_cn": dict(cfg.BOND_CN),
            "values": {cg: {d: round(v, 1) for d, v in dims.items()}
                       for cg, dims in read_bond(conn, child_id).items()},
            "trends": bond_trends(conn, child_id, t),
            "initialized_from_history":
                meta.get("bond_initialized_from_history") == "true",
            "confidence": meta.get("bond_confidence"),
        },
        "night": _night_stats(conn, child_id, t),
        "corpus": _corpus_stats(conn, child_id),
        "language": _language_stats(conn, brain, child_id),
        "firsts": _firsts(conn, child_id),
    }


def portrait_json(conn, brain, child_id: str, now: float | None = None) -> str:
    return json.dumps(build_portrait(conn, brain, child_id, now=now),
                      ensure_ascii=False)
