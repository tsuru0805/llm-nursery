# -*- coding: utf-8 -*-
"""需求事件 ask「他来找你」。

婴儿期无 ask——夜哭就是婴儿版 ask,不双轨。三段生命周期,全部幂等:

- plan_asks:当日确定性抽签((child_id,date) 种子,重复 tick 排出同一班表,
  幂等键兜底)→ scheduled_event(kind='ask')。计划时刻已在过去的点=fire 时
  直接 expired:没冒出来的 ask 不算漏接(没真发生过,同 closed_cry「只算真
  fired 的夜」口径)。
- fire_due_asks:到点触发→他真实开口(child_speak trigger='ask',失败=兜底稿)
  +场景模板包一句(texts.ASK_SCENES 朴素 v1)→outbox kind='nursery.ask',
  payload.target=papa/mama 由 接入层按 target 分路。
  outbox.expires_at=响应窗关点:窗关还没投出去的不再递(过时的招手不补播)。
- settle_asks:窗关后记账一次:窗内(**实际 fire 时刻起算**,不是计划 due_at——
  tick 迟到时他还没真出现,之前的动作不算接住)目标动词集(ASK_RESPONSE_KINDS)
  有动作=接住(actor=真实响应者——叔叔答了 papa 的 ask 也算接住,psyche 全额;
  bond 只认 BOND_ACTOR_TO_CG 在册的人,非在册 actor 记入主账);漏窗=独立微涨,
  不惩罚不扣。status 推进 fired→settled_hit/settled_miss,扫描面有界;账本身
  再叠 action_log 幂等键(askhit:/askmiss:),status 更新崩了重扫也不双记。

设计决定(评审记档):两条 ask 响应窗重叠时,同一个动作可以同时接住
两条——他来了两次,你在场的那一阵对两次都算数(刻意宽,不做动作独占)。
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
from .config import (ASK_ANSWERED_EFFECTS, ASK_HOURS, ASK_RESPONSE_KINDS,
                     ASK_STAGE_PLAN)


def derive_tattle(conn, child_id: str, target: str, t: float) -> str | None:
    """告状/吐槽稿:内容=当日 action_log 真账(零次也是真账,
    「都没陪我玩」由真实零计数派生,不编)。找妈妈=告爸爸的状(凶我了>
    没陪我玩>正面汇报);找爸爸=吐槽/汇报妈妈。返回带 {voice} 槽的完整
    场景模板(他真实模型的那句仍在场);查询异常由调用方兜回普通场景稿。"""
    day0 = _local_midnight(t)

    def _count(kinds, actor=None, exclude=()):
        marks = ",".join("?" for _ in kinds)
        if actor is not None:
            return conn.execute(
                f"SELECT COUNT(*) FROM action_log WHERE child_id=? AND actor=?"
                f" AND kind IN ({marks}) AND effective_at>=? AND effective_at<=?",
                (child_id, actor, *kinds, day0, t)).fetchone()[0]
        emarks = ",".join("?" for _ in exclude)
        return conn.execute(
            f"SELECT COUNT(*) FROM action_log WHERE child_id=?"
            f" AND actor NOT IN ({emarks})"
            f" AND kind IN ({marks}) AND effective_at>=? AND effective_at<=?",
            (child_id, *exclude, *kinds, day0, t)).fetchone()[0]

    if target == "mama":
        if _count(("discipline",), exclude=("system", "mama")) > 0:
            complaint = texts.TATTLE_MAMA_DISC
        else:
            n = _count(cfg.TATTLE_PLAY_KINDS, exclude=("system", "mama"))
            complaint = texts.TATTLE_MAMA_NOPLAY if n == 0 else \
                texts.TATTLE_MAMA_PRAISE.format(n=n)
    else:
        n = _count(cfg.TATTLE_TOUCH_KINDS, actor="mama")
        complaint = texts.TATTLE_PAPA_NOTOUCH if n == 0 else \
            texts.TATTLE_PAPA_PRAISE.format(n=n)
    return texts.TATTLE_SCENE.replace("{complaint}", complaint)


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def plan_asks(conn, child_id: str, now=None) -> int:
    """当日班表(确定性)。返回本次新排条数。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return 0
    stage = child_mod.stage_of(child, t)
    plan = ASK_STAGE_PLAN.get(stage)
    if not plan:
        return 0  # embryo/infant
    date = _local_date(t)
    rng = random.Random(f"{child_id}:ask:{date}")
    # 抽签顺序固定:day_p → 条数 → 每条(时刻,目标)。全部先抽完再入库,
    # 插入成败不影响序列=重复 tick 同班表。
    if rng.random() > plan["day_p"]:
        return 0  # 今天他自己玩得挺好
    n = rng.randint(*plan["n"])
    midnight = _local_midnight(t)
    slots = []
    for i in range(n):
        due = midnight + rng.uniform(ASK_HOURS[0] * 3600, ASK_HOURS[1] * 3600)
        target = "mama" if rng.random() < plan["mama_p"] else "papa"
        slots.append((due, target, f"ask:{date}:{i}"))
    created = 0
    with tx(conn):
        for due, target, idem in slots:
            try:
                conn.execute(
                    "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at,"
                    " expires_at, catchup_policy, status, payload_json,"
                    " idempotency_key) VALUES(?,?,NULL,?,?,'drop','pending',?,?)",
                    (child_id, "ask", due, due + plan["window_h"] * 3600,
                     json.dumps({"date": date, "target": target}), idem))
                created += 1
            except sqlite3.IntegrityError:
                pass  # UNIQUE(child_id, idempotency_key) 已排过=幂等跳过
    return created


def fire_due_asks(conn, brain: "child_mod.ChildBrain", child_id: str,
                  now=None) -> list[dict]:
    """领取到期 ask:过期即弃;到期的配场景稿+他真实开口,入 outbox。

    voice 生成在事务外(child_speak 自管事务),成功后回写 pending 的 outbox 行
    (fire_due_events 夜哭同款形制;speak 失败=payload 保留兜底稿)。
    """
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or "孩子"
    fired: list[dict] = []
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM scheduled_event WHERE child_id=? AND kind='ask'"
            " AND status='pending' AND due_at<=? ORDER BY due_at",
            (child_id, t)).fetchall()
        for ev in rows:
            if ev["expires_at"] is not None and t >= ev["expires_at"]:
                conn.execute("UPDATE scheduled_event SET status='expired'"
                             " WHERE id=?", (ev["id"],))
                continue
            meta = json.loads(ev["payload_json"] or "{}")
            stage = child_mod.stage_of(child, t)
            pool = texts.ASK_SCENES.get(stage) or texts.ASK_SCENES["child"]
            rng = random.Random(f"{child_id}:askev:{ev['id']}")
            scene_key, template = pool[rng.randrange(len(pool))]
            # 告状钩子:按事件种子概率把场景稿换成告状/吐槽(内容=真账)。
            # rng 抽取在池选之后=池选序列不变(确定性口径);派生失败照旧。
            if rng.random() < cfg.TATTLE_P:
                try:
                    tt = derive_tattle(conn, child_id,
                                       meta.get("target", "papa"), t)
                except Exception:
                    tt = None
                if tt:
                    scene_key, template = "tattle", tt
            # fired_at 落进 payload:settle 从他真出现的时刻起算(评审)
            meta["fired_at"] = t
            conn.execute("UPDATE scheduled_event SET status='fired',"
                         " attempt_count=attempt_count+1, payload_json=?"
                         " WHERE id=?",
                         (json.dumps(meta, ensure_ascii=False), ev["id"]))
            payload = {
                "kind": "nursery.ask",
                "title": texts.ASK_TITLE.format(name=name),
                "text": template.format(voice=texts.ASK_FALLBACK_VOICE),
                "voice": None, "scene": scene_key,
                "target": meta.get("target", "papa"), "stage": stage,
                # 全 str(接入层可做纯 str 硬校验);注入前过滤用
                "window_until": str(int(ev["expires_at"])), "ts": t,
                "source_event_id": f"ask:{child_id}:{ev['id']}",
                "_template": template,
            }
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind, payload_json,"
                " status, next_attempt_at, expires_at, idempotency_key)"
                " VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", "nursery.ask",
                 json.dumps(payload, ensure_ascii=False), t, ev["expires_at"],
                 payload["source_event_id"]))
            fired.append(payload)
    # 他真实开口(事务外);拿到后回写 pending 的 outbox 行
    for p in fired:
        template = p.pop("_template")
        try:
            res = child_mod.child_speak(conn, brain, child_id,
                                        trigger="ask", now=t)
            if res.accepted and res.text.strip():
                p["voice"] = res.text
                p["text"] = template.format(voice=res.text)
        except Exception:
            pass  # payload 已带兜底稿
        with tx(conn):
            conn.execute(
                "UPDATE outbox SET payload_json=? WHERE idempotency_key=?"
                " AND status='pending'",
                (json.dumps(p, ensure_ascii=False), p["source_event_id"]))
    return fired


def settle_asks(conn, child_id: str, now=None) -> dict:
    """窗关记账(fired→settled_hit/settled_miss)。返回 {hit, miss}。

    apply_action 自开顶层事务,本函数不得持事务调它;status 更新单独小事务,
    中途崩=下拍重扫,action_log 幂等键挡双记。
    """
    t = _now(now)
    hit = miss = 0
    rows = conn.execute(
        "SELECT * FROM scheduled_event WHERE child_id=? AND kind='ask'"
        " AND status='fired' AND expires_at IS NOT NULL AND expires_at<=?"
        " ORDER BY due_at", (child_id, t)).fetchall()
    for ev in rows:
        meta = json.loads(ev["payload_json"] or "{}")
        target = meta.get("target", "papa")
        since = meta.get("fired_at") or ev["due_at"]   # 真出现时刻起算(旧行兜底)
        kinds = ASK_RESPONSE_KINDS.get(target, ASK_RESPONSE_KINDS["papa"])
        marks = ",".join("?" for _ in kinds)
        resp = conn.execute(
            f"SELECT actor FROM action_log WHERE child_id=? AND kind IN ({marks})"
            " AND effective_at>=? AND effective_at<=? ORDER BY effective_at LIMIT 1",
            (child_id, *kinds, since, ev["expires_at"])).fetchone()
        if resp is not None:
            child_mod.apply_action(
                conn, child_id, resp["actor"], "ask_answered",
                idempotency_key=f"askhit:{ev['idempotency_key']}",
                payload={"ask": ev["idempotency_key"], "target": target,
                         "answered_by": resp["actor"]},
                extra_effects=dict(ASK_ANSWERED_EFFECTS), now=t)
            new_status = "settled_hit"
            hit += 1
        else:
            child_mod.apply_action(
                conn, child_id, "system", "ask_missed",
                idempotency_key=f"askmiss:{ev['idempotency_key']}",
                payload={"ask": ev["idempotency_key"], "target": target},
                extra_effects={}, now=t)
            new_status = "settled_miss"
            miss += 1
        with tx(conn):
            conn.execute("UPDATE scheduled_event SET status=? WHERE id=?",
                         (new_status, ev["id"]))
    return {"hit": hit, "miss": miss}
