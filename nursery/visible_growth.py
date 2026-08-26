# -*- coding: utf-8 -*-
"""可见成长。

总纲③:内容优先真实语料/真实数据生成,写死文案只做骨架。三件全是**派生可见化**,
不落新表、不改孩子内部任何状态:

- 宝贝盒:chunk_index(家庭词块索引,派生数据)top N=「他的宝贝」。
  纯派生读口 treasure_list(driver status 直读);每阶段落一张 growth_album
  「他现在的宝贝」卡(幂等 per stage)。
- 小本子:psyche_decision 存量真实决策 → 每日 21 点后(observer 时段)至多
  一行小本子事件。只用**安全字段**(锚词+趋势方向词),绝不直贴 DS 原始输出全文,
  不贴裸数值。幂等 per date。
- 生日 set-piece 的 album 卡在 events.check_stage_transition(跃迁时刻挂卡,
  文案=texts.BIRTHDAY_*)。

查不出=不发(observer 同哲学);任何故障=空照旧,tick 不许炸。
"""
from __future__ import annotations

import json
import time

from . import child as child_mod
from . import config as cfg
from . import texts
from .events import _emit


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def treasure_list(conn, child_id: str, top_n: int | None = None) -> list:
    """「他的宝贝」:词块索引 top N(纯派生,索引空=空表)。
    排序钉 (weight DESC, chunk):同数据永远同一份宝贝清单。"""
    n = top_n or cfg.TREASURE_TOP_N
    return [r["chunk"] for r in conn.execute(
        "SELECT chunk FROM chunk_index WHERE child_id=?"
        " ORDER BY weight DESC, chunk LIMIT ?", (child_id, n))]


def _treasure_card(conn, child_id: str, child, stage: str, t: float) -> bool:
    """每阶段一张「他现在的宝贝」卡(album+outbox,幂等 per stage)。"""
    if stage not in cfg.TREASURE_STAGES:
        return False
    if conn.execute(
            "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=? LIMIT 1",
            (child_id, f"treasure_{stage}")).fetchone() is not None:
        return False
    words = treasure_list(conn, child_id)
    if len(words) < cfg.TREASURE_MIN_CHUNKS:
        return False   # 词太少不成盒,等索引长起来
    name = child["name"] or "孩子"
    from .config import STAGE_CN
    return _emit(conn, child_id, kind="nursery.event",
                 item_kind=f"treasure_{stage}",
                 title=texts.TREASURE_TITLE.format(name=name,
                                                   stage_cn=STAGE_CN[stage]),
                 note=texts.TREASURE_NOTE.format(
                     words="、".join(f"「{w}」" for w in words)),
                 payload={"treasures": words, "stage": stage},
                 idem=f"treasure:{stage}:{child_id}", t=t)


def _notebook(conn, child_id: str, t: float) -> bool:
    """小本子:最近一条 ok 且有锚词的心理决策(NOTEBOOK_WINDOW_H 内)→
    一行观察事件。只抄锚词(≤NOTEBOOK_MAX_WORDS 个)+不安趋势**方向词**;
    raw_json/裸数值永不出现。每日至多一行(幂等 notebook:{date})。"""
    if time.localtime(t).tm_hour < cfg.OBSERVE_AFTER_H:
        return False
    # 窗口双端夹在 SQL 里(评审阻断:只查「差值>窗」会把未来时间戳的
    # 脏行当新鲜——created_at BETWEEN 才挡得住时间穿越)
    row = conn.execute(
        "SELECT anchor_words_json, input_digest_json, created_at"
        " FROM psyche_decision WHERE child_id=? AND status='ok' AND no_action=0"
        " AND anchor_words_json IS NOT NULL AND created_at>=? AND created_at<=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (child_id, t - cfg.NOTEBOOK_WINDOW_H * 3600, t)).fetchone()
    if row is None:
        return False   # 窗内没有真决策=小本子没翻开,不编
    try:
        words = [str(w).strip() for w in
                 json.loads(row["anchor_words_json"] or "[]") if str(w).strip()]
    except ValueError:
        return False
    words = words[: cfg.NOTEBOOK_MAX_WORDS]
    if not words:
        return False
    mood = None
    try:
        digest = json.loads(row["input_digest_json"] or "{}")
        trend = (digest.get("trends") or {}).get("anxiety")
        mood = texts.NOTEBOOK_MOOD.get(trend)   # flat/缺失=不加话
    except ValueError:
        mood = None
    date = _local_date(t)
    return _emit(conn, child_id, kind="nursery.event", item_kind=None,
                 title=texts.NOTEBOOK_LINE.format(words="」「".join(words)),
                 note=mood,
                 payload={"notebook": words},
                 idem=f"notebook:{date}:{child_id}", t=t, expires_at=t + 86400)


def tick_growth(conn, child_id: str, now: float | None = None) -> dict:
    """scheduler 每拍调(自守闸):active 才跑;单件故障不拦别件。"""
    t = child_mod._now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return {}
    stage = child_mod.stage_of(child, t)
    out: dict = {}
    try:
        if _treasure_card(conn, child_id, child, stage, t):
            out["treasure"] = stage
    except Exception:
        pass
    try:
        if _notebook(conn, child_id, t):
            out["notebook"] = True
    except Exception:
        pass
    return out
