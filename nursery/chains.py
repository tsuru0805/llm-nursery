# -*- coding: utf-8 -*-
"""连续剧事件链:3 天一条有状态的事件线。

scheduled_event kind='chain',chain_id='arc:<模板>'(night_cry combo 用 'combo',
语义不撞);幂等键=(child,模板,集数)→ arc:<模板>:ep<N>。全幂等:重复 tick
不重排不重发。

- plan_chains:确定性日抽签((child,模板,date) 种子)开播,**每模板一生一次**
  (连续剧重播必穿帮,choice 同款口径)。中签即把整季排进 scheduled_event
  (每天一集,傍晚 CHAIN_HOURS 时段;今晚首集时刻已过=顺延到明晚开播,
  不播断头首集)。
- fire_due_chain_eps:到点一集进 outbox(**复用 nursery.event kind**,不开新
  kind;title=当集正文 ≤130 字)。集序有闸:上一集没 fired 不播下一集;任何一集
  过宽限没播出(引擎断更)=整条剧作废(expired),绝不播断头剧。
  末集分支:上一集真 fire 时刻(fired_at)起 CHAIN_INTERVENE_WINDOW_H 小时内
  父母有介入动作(CHAIN_INTERVENE_KINDS,asks.settle 同款窗口判定口径)= good,
  否则 bad。真后果:分支动作账(state+psyche 规则;good 记在真实介入人头上=
  bond 同步走账,bad 记 system=零 bond)+末集进成长相册。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time

from . import child as child_mod
from . import config as cfg
from . import texts
from .child import tx


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def plan_chains(conn, child_id: str, now=None) -> int:
    """开播抽签+整季排班(幂等)。返回本次新排集数。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return 0
    stage = child_mod.stage_of(child, t)
    date = _local_date(t)
    created = 0
    with tx(conn):
        for name, tmpl in cfg.CHAIN_TEMPLATES.items():
            if stage not in tmpl["stages"]:
                continue
            if conn.execute(
                    "SELECT 1 FROM scheduled_event WHERE child_id=?"
                    " AND kind='chain' AND chain_id=? LIMIT 1",
                    (child_id, f"arc:{name}")).fetchone() is not None:
                continue   # 每模板一生一次(开播过=永不再抽)
            rng = random.Random(f"{child_id}:chain:{name}:{date}")
            if rng.random() > cfg.CHAIN_DAY_P:
                continue
            n = int(tmpl["episodes"])
            offsets = [rng.uniform(cfg.CHAIN_HOURS[0] * 3600,
                                   cfg.CHAIN_HOURS[1] * 3600) for _ in range(n)]
            base = _local_midnight(t)
            if base + offsets[0] <= t:
                base += 86400   # 今晚首集时刻已过:顺延明晚开播,不播断头首集
            for i in range(1, n + 1):
                due = base + (i - 1) * 86400 + offsets[i - 1]
                try:
                    conn.execute(
                        "INSERT INTO scheduled_event(child_id, kind, chain_id,"
                        " due_at, expires_at, catchup_policy, status,"
                        " payload_json, idempotency_key)"
                        " VALUES(?,?,?,?,?,'drop','pending',?,?)",
                        (child_id, "chain", f"arc:{name}", due,
                         due + cfg.CHAIN_EP_GRACE_H * 3600,
                         json.dumps({"template": name, "ep": i, "date": date}),
                         f"arc:{name}:ep{i}"))
                    created += 1
                except sqlite3.IntegrityError:
                    pass   # UNIQUE(child_id, idempotency_key) 已排过=幂等跳过
    return created


def _abort_chain(conn, child_id: str, chain_id: str) -> None:
    """断更弃剧:该链所有未播集(pending)一并作废,绝不播断头剧。"""
    conn.execute(
        "UPDATE scheduled_event SET status='expired' WHERE child_id=?"
        " AND kind='chain' AND chain_id=? AND status='pending'",
        (child_id, chain_id))


def _intervened(conn, child_id: str, since: float):
    """介入判定(asks.settle 同款口径):窗=上一集真 fire 起 WINDOW_H 小时,
    目标动词集内首个**照护人**动作(actor != system——引擎自动作不算
    「父母介入」;照护人是谁由接入层登记,引擎不钉名单)。
    返回行(actor 可取)或 None。"""
    kinds = cfg.CHAIN_INTERVENE_KINDS
    marks = ",".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT actor FROM action_log WHERE child_id=? AND kind IN ({marks})"
        " AND actor != 'system'"
        " AND effective_at>=? AND effective_at<=? ORDER BY effective_at LIMIT 1",
        (child_id, *kinds, since,
         since + cfg.CHAIN_INTERVENE_WINDOW_H * 3600)).fetchone()


def fire_due_chain_eps(conn, brain: "child_mod.ChildBrain", child_id: str,
                       now=None) -> list[dict]:
    """领取到期集,逐集小事务处理(apply_action 自开顶层事务,不能包大锁)。

    末集顺序:先落分支后果账(幂等键 arcfx:<模板>)再标 fired+outbox+相册——
    中途崩下拍续跑,后果账幂等键挡双记,不会「播了结局没落账」。
    """
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or "孩子"
    stage = child_mod.stage_of(child, t)
    fired: list[dict] = []
    rows = conn.execute(
        "SELECT * FROM scheduled_event WHERE child_id=? AND kind='chain'"
        " AND status='pending' AND due_at<=? ORDER BY due_at",
        (child_id, t)).fetchall()
    for ev in rows:
        meta = json.loads(ev["payload_json"] or "{}")
        tmpl_name = meta.get("template", "")
        ep = int(meta.get("ep", 0))
        tmpl = cfg.CHAIN_TEMPLATES.get(tmpl_name)
        episodes = texts.CHAIN_EPISODES.get(tmpl_name)
        chain_id = ev["chain_id"]
        if tmpl is None or episodes is None or ep <= 0:
            with tx(conn):
                _abort_chain(conn, child_id, chain_id)
            continue   # 模板被下架的旧行:整条安静作废,不炸 tick
        if ev["expires_at"] is not None and t >= ev["expires_at"]:
            with tx(conn):
                conn.execute("UPDATE scheduled_event SET status='expired'"
                             " WHERE id=?", (ev["id"],))
                _abort_chain(conn, child_id, chain_id)   # 断更=整条剧废弃
            continue
        prev = None
        if ep > 1:
            prev = conn.execute(
                "SELECT * FROM scheduled_event WHERE child_id=?"
                " AND idempotency_key=?",
                (child_id, f"arc:{tmpl_name}:ep{ep - 1}")).fetchone()
            if prev is None or prev["status"] == "expired":
                with tx(conn):
                    conn.execute("UPDATE scheduled_event SET status='expired'"
                                 " WHERE id=?", (ev["id"],))
                    _abort_chain(conn, child_id, chain_id)
                continue
            if prev["status"] != "fired":
                continue   # 上一集还没播(理论上同拍前行已处理),本拍先等
        # 末集:定分支+先落真后果账(actor=真实介入人/system)
        branch = None
        if ep == int(tmpl["episodes"]):
            prev_meta = json.loads(prev["payload_json"] or "{}") if prev else {}
            since = prev_meta.get("fired_at") or (prev["due_at"] if prev else t)
            resp = _intervened(conn, child_id, since)
            # 介入窗还开着(上一集播晚了):等窗关再判,不抢答 bad
            # (评审定案)。窗关点恒早于本集 expires:
            # since≤prev.due+GRACE,窗关≤prev.due+GRACE+WINDOW=+47h,
            # 本集 expires≥prev.due+21h+GRACE=+48h——等窗不会等成断更。
            # 分支一旦可判即确定(good=窗内首个父母动作定死;bad=窗已关),
            # 崩后重跑同分支,后果账/文案/相册不会分叉。
            if resp is None and \
                    t < since + cfg.CHAIN_INTERVENE_WINDOW_H * 3600:
                continue
            branch = "good" if resp is not None else "bad"
            fx = tmpl["branches"][branch]
            actor = resp["actor"] if resp is not None else "system"
            child_mod.apply_action(
                conn, child_id, actor, fx["kind"],
                idempotency_key=f"arcfx:{tmpl_name}",
                payload={"arc": tmpl_name, "branch": branch,
                         **({"intervened_by": actor} if resp is not None else {})},
                extra_effects=dict(fx.get("effects") or {}), now=t)
        text_tpl = episodes.get((ep, branch) if branch else ep)
        if text_tpl is None:
            text_tpl = episodes.get(ep, "")
        title = str(text_tpl).format(name=name)
        meta["fired_at"] = t
        if branch:
            meta["branch"] = branch
        idem_out = f"arc:{tmpl_name}:ep{ep}:{child_id}"
        with tx(conn):
            conn.execute("UPDATE scheduled_event SET status='fired',"
                         " attempt_count=attempt_count+1, payload_json=?"
                         " WHERE id=?",
                         (json.dumps(meta, ensure_ascii=False), ev["id"]))
            payload = {"kind": "nursery.event", "title": title, "note": None,
                       "ts": t, "source_event_id": idem_out,
                       "event": f"arc:{tmpl_name}:ep{ep}", "stage": stage}
            if branch:
                payload["branch"] = branch
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind,"
                " payload_json, status, next_attempt_at, expires_at,"
                " idempotency_key) VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", "nursery.event",
                 json.dumps(payload, ensure_ascii=False), t, ev["expires_at"],
                 idem_out))
            if branch and conn.execute(
                    "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=?"
                    " LIMIT 1", (child_id, f"arc_{tmpl_name}")).fetchone() is None:
                conn.execute(
                    "INSERT INTO growth_album(child_id, item_kind, title, note,"
                    " created_at) VALUES(?,?,?,?,?)",
                    (child_id, f"arc_{tmpl_name}",
                     texts.CHAIN_ALBUM_TITLE.get(tmpl_name, tmpl_name)
                     .format(name=name), title, t))
        fired.append(payload)
    return fired
