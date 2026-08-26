# -*- coding: utf-8 -*-
"""真实语料魔法。

设计原则:内容优先真实语料生成,写死文案只做兜底。四件里的三件在本文件
(送礼藏品卡挂在 events.maybe_daily_event 里,随每日事件同事务落卡):

1. **时空穿越提问**:从他偷学过的语料(corpus_item source_kind='archive')的
   source_ref 反查该窗的真实日期(经 sampler 只读硬闸,**不裸查全 archive**),
   生成「他突然问:那天你们去哪了」事件。
2. **温柔的误译**:随机挑一段偷学语料(不做情感判定,长度过滤即可),取锚词
   喂 speak 软偏置重新生成一句——与 maybe_surprise 同族,但**不动 SURPRISE_*
   配额逻辑**(配额治理归)。
3. **睡前故事复述**:昨晚(19:00-07:00)喂过 source_kind='book' 语料(story
   通道)=次日早上他用故事锚词复述一句;连续两晚同一故事(source_ref 相同)=
   「再讲一遍那个」事件。book 语料不存在=整件静默。
   频度闸=**真实讲故事行为本身**(不是抽签):book 语料只在真有人讲故事的晚上
   存在,复述最多每日一次(idem)——讲了故事必有回声是本件的设计点,天天讲
   天天复述不算刷屏(评审)。

纪律(与每日事件同族):确定性日抽签((child_id,date) 种子,重复 tick 同结果)、
outbox idempotency_key 幂等、低频、**fail-open——任何一件坏了绝不炸 tick**
(tick_magic 对每件单独吞异常)。日期窗判定用本地时(与 scheduler 同口径)。
"""
from __future__ import annotations

import os
import random
import time

from . import child as child_mod
from . import texts
from .config import (MAGIC_EVENT_HOURS, MISTRANSLATE_DAY_P, MISTRANSLATE_MIN_LEN,
                     MISTRANSLATE_STAGES, STORY_MORNING_H, STORY_NIGHT_START_H,
                     TIMETRAVEL_DAY_P, TIMETRAVEL_STAGES)
from .events import _album_has, _emit
from .sampler import connect_archive


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def _dup(conn, idem: str) -> bool:
    return conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=?",
                        (idem,)).fetchone() is not None


def _day_slot(child_id: str, tag: str, t: float, p: float):
    """确定性日抽签:今天有没有+几点发生(与 maybe_daily_event 同形制)。
    抽中且已到时刻 → 返回 (rng, date);否则 None。抽签顺序固定,重复 tick 同结果。"""
    date = _local_date(t)
    rng = random.Random(f"{child_id}:{tag}:{date}")
    if rng.random() > p:
        return None  # 今天没这一出
    happen_at = _local_midnight(t) + rng.uniform(MAGIC_EVENT_HOURS[0] * 3600,
                                                 MAGIC_EVENT_HOURS[1] * 3600)
    if t < happen_at:
        return None  # 还没到那一刻
    return rng, date


def _anchor_words(text: str, rng: random.Random, n: int = 3) -> list[str]:
    """从片段里抽 n 个 2-4 字锚词(speak 软偏置用;不做情感判定)。"""
    body = "".join(ch for ch in text if not ch.isspace())
    if len(body) < 4:
        return []
    out: list[str] = []
    for _ in range(n):
        ln = rng.randint(2, 4)
        if len(body) <= ln:
            break
        off = rng.randrange(0, len(body) - ln)
        out.append(body[off:off + ln])
    return out


# ────────────────────────── 件1:时空穿越提问 ──────────────────────────

def maybe_timetravel(conn, child_id: str, now=None) -> str | None:
    """从偷学语料的 source_ref 窗反查真实日期 → 「那天你们去哪了」。
    低频确定性抽签;archive 打不开/窗查不到日期=本轮静默(fail-open 由调用方兜)。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return None
    if child_mod.stage_of(child, t) not in TIMETRAVEL_STAGES:
        return None
    slot = _day_slot(child_id, "tt", t, TIMETRAVEL_DAY_P)
    if slot is None:
        return None
    rng, date = slot
    idem = f"tt:{date}:{child_id}"
    if _dup(conn, idem):
        return None  # 今天已问过(幂等;也免得重复开 archive)
    # 只认他真偷学过的窗(ref=win_id@offset+len),别裸查全 archive
    wins: list[str] = []
    seen: set[str] = set()
    for r in conn.execute(
            "SELECT source_ref FROM corpus_item WHERE child_id=?"
            " AND source_kind='archive' AND source_ref IS NOT NULL ORDER BY id",
            (child_id,)):
        w = r["source_ref"].split("@", 1)[0]
        if w and w not in seen:
            seen.add(w)
            wins.append(w)
    if not wins:
        return None  # 还没偷学过=没有可穿越的日子
    win = wins[rng.randrange(len(wins))]
    archive = connect_archive(os.getenv("NURSERY_ARCHIVE_DB", ""))
    try:
        row = archive.execute("SELECT date FROM windows WHERE id=?",
                              (win,)).fetchone()
    finally:
        archive.close()
    if row is None or not row["date"]:
        return None
    try:
        lt = time.strptime(row["date"], "%Y-%m-%d")
    except ValueError:
        return None
    date_cn = f"{lt.tm_year}年{lt.tm_mon}月{lt.tm_mday}日"
    if _emit(conn, child_id, kind="nursery.event", item_kind=None,
             title=texts.MAGIC_TIMETRAVEL.format(date=date_cn), note=None,
             payload={"event": "timetravel", "win": win,
                      "archive_date": row["date"]},
             idem=idem, t=t, expires_at=t + 86400):
        return "timetravel"
    return None


# ────────────────────────── 件2:温柔的误译 ──────────────────────────

def maybe_mistranslate(conn, brain, child_id: str, now=None) -> str | None:
    """偷学语料取锚喂 speak 重新生成——他把听来的话复述成自己的道理。
    与 maybe_surprise 同族但不碰 surprise 配额;speak 不过护栏=今天作罢。"""
    from .decoder import speak
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return None
    stage = child_mod.stage_of(child, t)
    if stage not in MISTRANSLATE_STAGES:
        return None
    slot = _day_slot(child_id, "mt", t, MISTRANSLATE_DAY_P)
    if slot is None:
        return None
    rng, date = slot
    idem = f"mt:{date}:{child_id}"
    if _dup(conn, idem):
        return None
    rows = conn.execute(
        "SELECT source_ref, text FROM corpus_item WHERE child_id=?"
        " AND source_kind='archive' AND LENGTH(text)>=? ORDER BY id",
        (child_id, MISTRANSLATE_MIN_LEN)).fetchall()
    if not rows:
        return None
    row = rows[rng.randrange(len(rows))]
    anchors = _anchor_words(row["text"], rng)
    if not anchors:
        return None
    res = speak(brain.model, brain.guard, stage, rng, anchor_words=anchors)
    if not res.accepted or not res.text.strip():
        return None  # 确定性 rng:同日重试同结果,不会越试越歪
    if _emit(conn, child_id, kind="nursery.event", item_kind=None,
             title=texts.MAGIC_MISTRANSLATE.format(voice=res.text), note=None,
             payload={"event": "mistranslate", "utterance": res.text,
                      "source_ref": row["source_ref"]},
             idem=idem, t=t, expires_at=t + 86400):
        return "mistranslate"
    return None


# ────────────────────────── 件3:睡前故事复述 ──────────────────────────

def maybe_story_retell(conn, brain, child_id: str, now=None) -> str | None:
    """昨晚讲过故事(book 语料)=次日 07:00 后他复述一句;连续两晚同一故事
    (source_ref 相同且非空)=「再讲一遍那个」。book 语料不存在=整件静默。
    首次复述额外落一张相册卡(first_story_retell,一生一次)。"""
    from .decoder import speak
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return None
    if time.localtime(t).tm_hour < STORY_MORNING_H:
        return None  # 睡一觉才变成自己的
    date = _local_date(t)
    midnight = _local_midnight(t)
    # 昨晚窗=昨天 19:00 → 今天 07:00
    night0 = midnight - (24 - STORY_NIGHT_START_H) * 3600
    night1 = midnight + STORY_MORNING_H * 3600
    story = conn.execute(
        "SELECT source_ref, text FROM corpus_item WHERE child_id=?"
        " AND source_kind='book' AND acquired_at>=? AND acquired_at<?"
        " ORDER BY acquired_at DESC LIMIT 1",
        (child_id, night0, night1)).fetchone()
    if story is None:
        return None  # 昨晚没讲故事=静默
    # 连续两晚同一故事(source_ref 同;NULL 无从归并,不算同一故事)
    again = bool(story["source_ref"]) and conn.execute(
        "SELECT 1 FROM corpus_item WHERE child_id=? AND source_kind='book'"
        " AND source_ref=? AND acquired_at>=? AND acquired_at<? LIMIT 1",
        (child_id, story["source_ref"], night0 - 86400,
         night1 - 86400)).fetchone() is not None
    if again:
        idem, template, event = (f"storyagain:{date}:{child_id}",
                                 texts.MAGIC_STORY_AGAIN, "story_again")
    else:
        idem, template, event = (f"story:{date}:{child_id}",
                                 texts.MAGIC_STORY_RETELL, "story_retell")
    if _dup(conn, idem):
        return None
    rng = random.Random(f"{child_id}:story:{date}")
    anchors = _anchor_words(story["text"], rng)
    if not anchors:
        return None
    stage = child_mod.stage_of(child, t)
    res = speak(brain.model, brain.guard, stage, rng, anchor_words=anchors)
    if not res.accepted or not res.text.strip():
        return None
    # 可选进相册:第一次复述故事=一生一次的纪念件((child,item_kind) 幂等)
    item_kind = None if _album_has(conn, child_id, "first_story_retell") \
        else "first_story_retell"
    if _emit(conn, child_id, kind="nursery.event", item_kind=item_kind,
             title=template.format(voice=res.text), note=None,
             payload={"event": event, "utterance": res.text,
                      "source_ref": story["source_ref"]},
             idem=idem, t=t, expires_at=t + 86400):
        return event
    return None


# ────────────────────────── tick 入口 ──────────────────────────

def tick_magic(conn, brain, child_id: str, now=None) -> dict:
    """scheduler 每拍调。三件各自独立 fail-open:单件异常吞掉,绝不炸 tick
    (送礼藏品卡挂在 events.maybe_daily_event,不在此)。"""
    t = _now(now)
    out: dict = {}
    for name, fn in (
            ("timetravel", lambda: maybe_timetravel(conn, child_id, now=t)),
            ("mistranslate", lambda: maybe_mistranslate(conn, brain, child_id,
                                                        now=t)),
            ("story", lambda: maybe_story_retell(conn, brain, child_id, now=t))):
        try:
            r = fn()
        except Exception:
            r = None  # fail-open:archive 坏/库怪/模型空,都不许炸 tick
        if r:
            out[name] = r
    return out
