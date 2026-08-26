# -*- coding: utf-8 -*-
"""观察日志(见 AGENTS.md)。

「我不操作时,它也存在」——每天晚间从**真实状态与生成统计**派生 1-2 行旁观记录
("今天把「果果」说了 4 遍""有一句话试了几次,没说完整"),走 nursery.event 既有事件通道进围观台时间线;**不进成长相册**(item_kind=None),不改孩子内部任何状态。

与每日随机事件(config.DAILY_EVENTS 文案池)的本质区别:这里每一行都必须由
数据真实支撑,查不出=不发,绝不编。文案模板=texts 层;派生纯 SQL。
"""
from __future__ import annotations

import time
from collections import Counter

from . import config as cfg
from . import texts
from .chunks import _clean_runs


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _midnight(t: float) -> float:
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _obs_repeat(conn, child_id: str, day0: float, t: float):
    """今天把某个词说了 n 遍(≥2 句不同话里出现同一 2-4 字片段)。"""
    texts_today = [r["text"] for r in conn.execute(
        "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
        " AND created_at>=? AND created_at<=?", (child_id, day0, t))]
    if len(texts_today) < 2:
        return None
    seen: Counter = Counter()
    for txt in texts_today:
        subs = set()
        for run in _clean_runs(txt):
            for n in (2, 3, 4):
                for i in range(len(run) - n + 1):
                    subs.add(run[i:i + n])
        seen.update(subs)   # 每句最多记一次(要"多句都说",不是一句里叠)
    best = [(w, c) for w, c in seen.items() if c >= 2]
    if not best:
        return None
    # 词面兜底排序:同数据永远选同一个词(集合哈希序不稳,评审
    w, c = max(best, key=lambda x: (x[1], len(x[0]), x[0]))
    return texts.OBS_REPEAT.format(word=w, n=c)


def _obs_unfinished(conn, child_id: str, day0: float, t: float):
    """有话没说完整(今天护栏重采样耗尽的次数 ≥1)。"""
    n = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=? AND accepted=0"
        " AND rejection_reason='guard_exhausted' AND created_at>=? AND created_at<=?",
        (child_id, day0, t)).fetchone()[0]
    return texts.OBS_UNFINISHED if n >= 1 else None


def quiet_gap_seconds(conn, child_id: str, day0: float, t: float) -> float | None:
    """白天(07:00 起)最长无人互动间隔的**公共口径**(观察行与摩擦轴「被晾」
    共用同一判定,统计不重造)。返回间隔秒数;白天窗还没开/窗内有 fired 夜哭
    (他真闹过,不算被晾)=None。"""
    start = day0 + 7 * 3600
    if t <= start:
        return None
    stamps = [start] + [r["effective_at"] for r in conn.execute(
        "SELECT effective_at FROM action_log WHERE child_id=?"
        " AND effective_at>=? AND effective_at<=? ORDER BY effective_at",
        (child_id, start, t))] + [t]
    gap = max(b - a for a, b in zip(stamps, stamps[1:]))
    cried = conn.execute(
        "SELECT 1 FROM scheduled_event WHERE child_id=? AND kind='night_cry'"
        " AND status='fired' AND due_at>=? AND due_at<=? LIMIT 1",
        (child_id, start, t)).fetchone()  # 钉 kind:白天 fired 的 ask()不算哭
    if cried is not None:
        return None
    return gap


def _obs_quiet(conn, child_id: str, day0: float, t: float):
    """白天最长无人互动间隔 ≥ 阈值。「没闹」必须有据:窗内存在 fired 哭闹即不发
    (不许编)。判定=quiet_gap_seconds 公共口径。"""
    gap = quiet_gap_seconds(conn, child_id, day0, t)
    if gap is None or gap < cfg.OBSERVE_QUIET_GAP_H * 3600:
        return None
    return texts.OBS_QUIET.format(hours=int(gap // 3600))


def _obs_diary(conn, child_id: str, day0: float, t: float):
    """日记上锁片段(,teen 限定):从他**当日 accepted utterance** 里取一句
    真话,遮一半字符——「他的日记你只能看到一角」。真数据不编,没说过话=不发。
    取句确定性(最长优先,平手取最新),同日重算同一句。"""
    from . import child as child_mod
    child = child_mod.get_child(conn, child_id)
    if child_mod.stage_of(child, t) != "teen":
        return None
    row = conn.execute(
        "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
        " AND created_at>=? AND created_at<=?"
        " ORDER BY LENGTH(text) DESC, id DESC LIMIT 1",
        (child_id, day0, t)).fetchone()
    if row is None or len(row["text"]) < 4:
        return None
    txt = row["text"]
    keep = max(2, len(txt) // 2)
    peek = txt[:keep] + "▓" * (len(txt) - keep)
    return texts.OBS_DIARY.format(peek=peek)


def _obs_new_chars(conn, child_id: str, day0: float, t: float):
    """今天语料带来的新字数(与既往语料字集差)。"""
    old = set("".join(r["text"] for r in conn.execute(
        "SELECT text FROM corpus_item WHERE child_id=? AND acquired_at<?",
        (child_id, day0))))
    today = set("".join(r["text"] for r in conn.execute(
        "SELECT text FROM corpus_item WHERE child_id=? AND acquired_at>=?"
        " AND acquired_at<=?", (child_id, day0, t))))
    fresh = len({c for c in today - old if not c.isspace()})
    if fresh < cfg.OBSERVE_NEW_CHARS_MIN:
        return None
    return texts.OBS_NEW_CHARS.format(n=fresh)


def _obs_stale_chunk(conn, child_id: str, day0: float, t: float):
    """很久没说某个家常词(词块 top10 里滚动 72h 没出现在他嘴里的;
    窗内他得真说过 ≥3 句,否则是没人逗他说话,不怪词)。窗口=[t-72h, t]
    双端夹取(评审。"""
    since = t - 3 * 86400
    said = [r["text"] for r in conn.execute(
        "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
        " AND created_at>=? AND created_at<=?", (child_id, since, t))]
    if len(said) < 3:
        return None
    blob = "\n".join(said)
    for r in conn.execute(
            "SELECT chunk FROM chunk_index WHERE child_id=?"
            " ORDER BY weight DESC LIMIT 10", (child_id,)):
        if r["chunk"] not in blob:
            return texts.OBS_STALE.format(word=r["chunk"])
    return None


def _obs_taught_word(conn, child_id: str, day0: float, t: float):
    """溯源闭环:他今天说的词,是昨天谁教的。判据全真——
    词=昨天亲口语料(direct/night_feed/book)与今天 accepted 话的公共 2-6 字片段,
    且至少含一个「昨天才第一次进语料」的字;查不出=不发,绝不编。"""
    y0 = day0 - 86400
    taught = conn.execute(
        "SELECT speaker, text FROM corpus_item WHERE child_id=?"
        " AND source_kind IN ('direct','night_feed','book')"
        " AND acquired_at>=? AND acquired_at<?", (child_id, y0, day0)).fetchall()
    if not taught:
        return None
    said_today = [r["text"] for r in conn.execute(
        "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
        " AND created_at>=? AND created_at<=?", (child_id, day0, t))]
    if not said_today:
        return None
    old_chars = set("".join(r["text"] for r in conn.execute(
        "SELECT text FROM corpus_item WHERE child_id=? AND acquired_at<?",
        (child_id, y0))))
    cands = []   # (len, word, speaker) 全收集后词面定序(与 _obs_repeat 同纪律)
    for row in taught:
        fresh = {c for c in row["text"] if c not in old_chars and not c.isspace()}
        if not fresh:
            continue
        for run in _clean_runs(row["text"]):
            for n in (2, 3, 4, 5, 6):
                for i in range(len(run) - n + 1):
                    w = run[i:i + n]
                    if fresh & set(w) and any(w in s for s in said_today):
                        cands.append((n, w, row["speaker"] or ""))
    if not cands:
        return None
    _, w, speaker = max(cands, key=lambda x: (x[0], x[1]))
    who = texts.ROLE_CN.get(speaker, "有人")   # 声部显示名(papa/mama)
    return texts.OBS_TAUGHT.format(word=w, who=who)


def _obs_asks(conn, child_id: str, day0: float, t: float):
    """今天他主动来找人的战报(,吃settle 真账;零条=不发)。"""
    rows = conn.execute(
        "SELECT status FROM scheduled_event WHERE child_id=? AND kind='ask'"
        " AND status IN ('settled_hit','settled_miss')"
        " AND expires_at>=? AND expires_at<=?", (child_id, day0, t)).fetchall()
    if not rows:
        return None
    hit = sum(1 for r in rows if r["status"] == "settled_hit")
    return texts.OBS_ASKS.format(n=len(rows), m=hit)


_CANDIDATES = (
    # diary()自身钉 teen——非 teen 恒 None 不占名额;teen 期招牌观察排最前
    ("diary", _obs_diary),
    ("taught", _obs_taught_word),   # :溯源点名——养成感的核心闭环
    ("repeat", _obs_repeat),
    ("unfinished", _obs_unfinished),
    ("asks", _obs_asks),            # :今天他来找过人的真账
    ("quiet", _obs_quiet),
    ("new_chars", _obs_new_chars),
    ("stale_chunk", _obs_stale_chunk),
)


def daily_observe(conn, child_id: str, now: float | None = None) -> list[str]:
    """晚间观察(scheduler 每拍调,自守闸):本地 OBSERVE_AFTER_H 点后,
    按优先序发当日还没发过的观察行,全天上限 OBSERVE_MAX_PER_DAY。
    幂等键 obs:{date}:{key};过了午夜=昨天的不补(与过期即弃同哲学)。"""
    from .child import _now, get_child
    from .events import _emit
    t = _now(now)
    if time.localtime(t).tm_hour < cfg.OBSERVE_AFTER_H:
        return []
    if get_child(conn, child_id)["status"] != "active":
        return []
    date = _local_date(t)
    already = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE child_id=? AND idempotency_key LIKE ?",
        (child_id, f"obs:{date}:%")).fetchone()[0]
    budget = cfg.OBSERVE_MAX_PER_DAY - already
    if budget <= 0:
        return []
    day0 = _midnight(t)
    out: list[str] = []
    for key, fn in _CANDIDATES:
        if budget <= 0:
            break
        try:
            line = fn(conn, child_id, day0, t)
        except Exception:
            continue   # 单条派生失败不拦别条,更不许炸 tick
        if not line:
            continue
        if _emit(conn, child_id, kind="nursery.event", item_kind=None,
                 title=line, note=None, payload={"observation": key},
                 idem=f"obs:{date}:{key}", t=t):
            out.append(key)
            budget -= 1
    return out
