# -*- coding: utf-8 -*-
"""生病 arc:低频全期的「被需要」回归。

三段生命周期,复用夜哭的排班/expires/幂等纪律(kind 独立='sickness'/'sick_cry',
chain_id=f"sick:{date}"——不撞 night_cry 的 chain_id IS NULL/=='combo' 语义,
closed_cry_nights/忽视账/结局响应率全部不受影响):

- plan_sickness:确定性日抽签((child_id,date) 种子)+最小间隔闸 → 开一段
  SICKNESS_DURATION_H(2 天)的 sickness scheduled_event + 病窗次日凌晨
  03:00-06:00 一次 sick_cry(**婴儿期外也叫——这正是设计点**;当日 07:00 过期即弃)。
- fire_due_sickness:到点开窗=「他病了」事件(nursery.event);sick_cry 到点=
  夜哭同形叫醒(nursery.cry, detail='sick'),voice=他病中的真实声音
  (child_speak 在窗内自动吃解码扰动;失败=兜底哼唧)。
- settle_sickness:窗关(expires_at 到)=自动痊愈事件,status→settled。

病中效果(全部 fail-open):
- 解码扰动:child.child_speak 查 open_sickness_date(**单条 indexed 查询**,
  idx_sched_child_kind)→ decoder.speak(sick=True):温度升/句长缩/叠词回升。
- 照顾加成:psyche.apply_rules_locked 里 SICK_CARE_KINDS(feed/soothe+mama 三件)窗内
  落 SICK_CARE_BONUS(每病日每类一次,parenting_meta 占位,与夜哭响应同形制)。

全幂等:scheduled_event UNIQUE(child_id,idempotency_key)+outbox idem。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time

from . import child as child_mod
from . import texts
from .child import tx
from .config import (SICK_CRY_EXPIRES_H, SICK_CRY_HOURS, SICK_ONSET_HOURS,
                     SICKNESS_DAY_P, SICKNESS_DURATION_H, SICKNESS_MIN_GAP_DAYS)

FALLBACK_SICK_VOICE = "(哼……哼唧……)"


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def open_sickness_date(conn, child_id: str, t: float) -> str | None:
    """t 时刻病窗是否开着(已 fired 未过期);是则返回开窗 date。
    child_speak 热路径每次调:单条查询吃 idx_sched_child_kind,便宜;
    调用方必须 fail-open(查询炸了当没病)。"""
    row = conn.execute(
        "SELECT payload_json FROM scheduled_event WHERE child_id=?"
        " AND kind='sickness' AND status='fired' AND due_at<=?"
        " AND expires_at IS NOT NULL AND expires_at>? LIMIT 1",
        (child_id, t, t)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"] or "{}").get("date") or "open"
    except ValueError:
        return "open"   # 窗确实开着,date 读不出也别把病判没了


def plan_sickness(conn, child_id: str, now=None) -> int:
    """确定性抽签开病窗(每 10-14 天量级=最小间隔 7 天+日抽签 p=0.2)。
    同日重排幂等(idem 键);返回本次新排条数。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child["born_at"] is None:
        return 0
    date = _local_date(t)
    rng = random.Random(f"{child_id}:sick:{date}")
    last = conn.execute(
        "SELECT MAX(due_at) FROM scheduled_event WHERE child_id=?"
        " AND kind='sickness'", (child_id,)).fetchone()[0]
    if last is not None and t - last < SICKNESS_MIN_GAP_DAYS * 86400:
        return 0  # 刚病过,缓缓
    if rng.random() > SICKNESS_DAY_P:
        return 0  # 今天结实着呢
    midnight = _local_midnight(t)
    onset = midnight + rng.uniform(SICK_ONSET_HOURS[0] * 3600,
                                   SICK_ONSET_HOURS[1] * 3600)
    expires = onset + SICKNESS_DURATION_H * 3600
    chain = f"sick:{date}"
    # 病窗次日凌晨的夜叫(03:00-06:00,当日 07:00 过期;婴儿期外也叫=设计点)
    cry_night = midnight + 86400
    cry_due = cry_night + rng.uniform(SICK_CRY_HOURS[0] * 3600,
                                      SICK_CRY_HOURS[1] * 3600)
    cry_expires = min(cry_night + SICK_CRY_EXPIRES_H * 3600, expires)
    plan = [("sickness", onset, expires, f"sick:{date}", {"date": date})]
    if cry_due < cry_expires:
        plan.append(("sick_cry", cry_due, cry_expires, f"sick:{date}:cry",
                     {"date": _local_date(cry_due)}))
    created = 0
    with tx(conn):
        for kind, due_at, exp, idem, payload in plan:
            try:
                conn.execute(
                    "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at,"
                    " expires_at, catchup_policy, status, payload_json,"
                    " idempotency_key) VALUES(?,?,?,?,?,'drop','pending',?,?)",
                    (child_id, kind, chain, due_at, exp,
                     json.dumps(payload), idem))
                created += 1
            except sqlite3.IntegrityError:
                pass  # UNIQUE(child_id, idempotency_key) 已排过=幂等跳过
    return created


def fire_due_sickness(conn, brain, child_id: str, now=None) -> list[dict]:
    """领取到期件:过期即弃;sickness 开窗=「他病了」事件,sick_cry=夜哭同形叫醒。
    status 更新与 outbox 插入同事务(fire_due_asks 同形制);sick_cry 的真实声音
    在事务外补(child_speak 自管事务,窗已 fired=扰动生效;失败=兜底哼唧)。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or "孩子"
    fired: list[dict] = []
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM scheduled_event WHERE child_id=?"
            " AND kind IN ('sickness','sick_cry') AND status='pending'"
            " AND due_at<=? ORDER BY due_at", (child_id, t)).fetchall()
        for ev in rows:
            if ev["expires_at"] is not None and t >= ev["expires_at"]:
                conn.execute("UPDATE scheduled_event SET status='expired'"
                             " WHERE id=?", (ev["id"],))
                continue
            conn.execute("UPDATE scheduled_event SET status='fired',"
                         " attempt_count=attempt_count+1 WHERE id=?", (ev["id"],))
            if ev["kind"] == "sickness":
                payload = {
                    "kind": "nursery.event", "event": "sick_onset",
                    "title": texts.SICK_ONSET.format(name=name), "note": None,
                    "ts": t,
                    # outbox.idempotency_key 全局 UNIQUE,键带 child_id
                    # (夜哭/ask 同口径)
                    "source_event_id": f"parentingsick:{child_id}:{ev['id']}:open",
                }
                out_expires = t + 86400
            else:
                payload = {
                    "kind": "nursery.cry", "detail": "sick",
                    "text": texts.SICK_CRY_TEXT, "chain": ev["chain_id"],
                    "responded": None, "voice": FALLBACK_SICK_VOICE, "ts": t,
                    "source_event_id": f"sickcry:{child_id}:{ev['id']}",
                }
                out_expires = ev["expires_at"]  # 夜里的难受不上午补播
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind,"
                " payload_json, status, next_attempt_at, expires_at,"
                " idempotency_key) VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", payload["kind"],
                 json.dumps(payload, ensure_ascii=False), t, out_expires,
                 payload["source_event_id"]))
            fired.append(payload)
    # 病中的真实声音(事务外;此刻窗已 fired,child_speak 自动吃解码扰动)
    for p in fired:
        if p["kind"] != "nursery.cry":
            continue
        try:
            res = child_mod.child_speak(conn, brain, child_id,
                                        trigger="sick_cry", now=t)
            if res.accepted and res.text.strip():
                p["voice"] = res.text
                with tx(conn):
                    conn.execute(
                        "UPDATE outbox SET payload_json=? WHERE idempotency_key=?"
                        " AND status='pending'",
                        (json.dumps(p, ensure_ascii=False),
                         p["source_event_id"]))
        except Exception:
            pass  # payload 已带兜底哼唧
    return fired


def settle_sickness(conn, child_id: str, now=None) -> int:
    """窗关自动痊愈:fired 且 expires_at 到 → settled+痊愈事件。返回痊愈条数。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or "孩子"
    healed = 0
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM scheduled_event WHERE child_id=? AND kind='sickness'"
            " AND status='fired' AND expires_at IS NOT NULL AND expires_at<=?"
            " ORDER BY due_at", (child_id, t)).fetchall()
        for ev in rows:
            conn.execute("UPDATE scheduled_event SET status='settled'"
                         " WHERE id=?", (ev["id"],))
            payload = {
                "kind": "nursery.event", "event": "sick_recovered",
                "title": texts.SICK_HEAL.format(name=name), "note": None,
                "ts": t,
                "source_event_id": f"parentingsick:{child_id}:{ev['id']}:heal",
            }
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind,"
                " payload_json, status, next_attempt_at, expires_at,"
                " idempotency_key) VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", "nursery.event",
                 json.dumps(payload, ensure_ascii=False), t, t + 86400,
                 payload["source_event_id"]))
            healed += 1
    return healed


def tick_sickness(conn, brain, child_id: str, now=None) -> dict:
    """scheduler 每拍调:排窗→触发→痊愈。三段各自独立 fail-open,绝不炸 tick。"""
    t = _now(now)
    out: dict = {}
    try:
        n = plan_sickness(conn, child_id, now=t)
        if n:
            out["planned"] = n
    except Exception:
        pass
    try:
        f = fire_due_sickness(conn, brain, child_id, now=t)
        if f:
            out["fired"] = len(f)
    except Exception:
        pass
    try:
        h = settle_sickness(conn, child_id, now=t)
        if h:
            out["healed"] = h
    except Exception:
        pass
    return out
