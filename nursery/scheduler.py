# -*- coding: utf-8 -*-
"""调度器:tick+夜奶链+偷学+outbox 投递。

驱动方式:外部定时器(cron/launchd)每 1-5 分钟调一次 `python -m nursery.driver --tick`
(短命进程,刻意不为随机哭闹动态生成定时任务)。业务时间全在 SQLite:
- 夜奶排班:婴儿期每晚一主哭(默认 04:40-05:10 随机,按自家作息改 NIGHT_CRY_WINDOW)
  +至多 2 连击拍(主哭后 8-18min 必发 / 25-50min 概率),expires 07:00 过期即弃不补播。
- 排班用 (child_id, date) 种子的确定性 RNG:重复 tick 排出同一班表,幂等键兜底。
- 偷学:每 tick 概率触发,每日上限;archive 打不开=fail closed 本轮跳过。
- outbox:至少一次投递;NURSERY_EVENT_URL 未配置=留 pending(开闸前常态,不炸)。
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
import urllib.request

from . import child as child_mod
from . import db as pdb
from . import texts
from .child import tx
from .sampler import connect_archive, sample_fragments

NIGHT_CRY_WINDOW = (4 * 3600 + 40 * 60, 5 * 3600 + 10 * 60)   # 当日 04:40-05:10(秒)
NIGHT_CRY_EXPIRES = 7 * 3600                                   # 当日 07:00
COMBO_1 = (8 * 60, 18 * 60)      # 主哭后 8-18min,必发
COMBO_2 = (25 * 60, 50 * 60)     # 主哭后 25-50min,概率
COMBO_2_P = 0.35
STEAL_P = 0.15                   # 每 tick 偷学概率
STEAL_DAILY_MAX = 3              # 每日偷学条数上限
STEAL_BATCH = 1

# 夜奶抽奖池(权重可调;文案在 texts.CRY_TEXT,键要对得上)
CRY_POOL = [("hungry", 50), ("diaper", 30), ("hold", 15), ("dream", 5)]


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def _pick_weighted(rng: random.Random, pool: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in pool)
    r = rng.uniform(0, total)
    acc = 0.0
    for name, w in pool:
        acc += w
        if r <= acc:
            return name
    return pool[-1][0]


def schedule_night_feed(conn, child_id: str, now: float | None = None) -> int:
    """给"明晨"排夜奶班(婴儿期)。确定性 RNG+幂等键,重复调用不重复排。返回新排数。"""
    t = child_mod._now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child_mod.stage_of(child, t) != "infant":
        return 0
    # 排"下一个凌晨"的班:当前时刻在今日 04:40 前排今晨,否则排明晨
    midnight = _local_midnight(t)
    if t - midnight >= NIGHT_CRY_WINDOW[0]:
        midnight += 86400
    date = _local_date(midnight + 1)
    rng = random.Random(f"{child_id}:{date}")
    due = midnight + rng.uniform(*NIGHT_CRY_WINDOW)
    expires = midnight + NIGHT_CRY_EXPIRES
    plan = [("night_cry", due, f"nightcry:{date}", None)]
    combo1_due = due + rng.uniform(*COMBO_1)
    plan.append(("night_cry", combo1_due, f"nightcry:{date}:c1", "combo"))
    if rng.random() < COMBO_2_P:
        plan.append(("night_cry", due + rng.uniform(*COMBO_2),
                     f"nightcry:{date}:c2", "combo"))
    created = 0
    with tx(conn):
        for kind, due_at, idem, chain in plan:
            if due_at >= expires:
                continue
            try:
                conn.execute(
                    "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at,"
                    " expires_at, catchup_policy, status, payload_json, idempotency_key)"
                    " VALUES(?,?,?,?,?,'drop','pending',?,?)",
                    (child_id, kind, chain, due_at, expires,
                     json.dumps({"date": date}), idem))
                created += 1
            except sqlite3.IntegrityError:
                pass  # UNIQUE(child_id, idempotency_key) 已排过=幂等跳过;其他错照炸
    return created


def fire_due_events(conn, brain: child_mod.ChildBrain, child_id: str,
                    now: float | None = None) -> list[dict]:
    """领取到期事件:过期即弃;到期的抽奖成事件+孩子的真实哭声,入 outbox。

    voice 生成在事务外(child_speak 自管事务),成功后回写 outbox.payload_json
    (此时行仍 pending 未投递,更新安全;speak 失败=payload 保留兜底哭声)。
    """
    t = child_mod._now(now)
    fired: list[dict] = []
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM scheduled_event WHERE child_id=? AND status='pending'"
            " AND due_at<=? ORDER BY due_at", (child_id, t)).fetchall()
        for ev in rows:
            if ev["expires_at"] is not None and t >= ev["expires_at"]:
                conn.execute("UPDATE scheduled_event SET status='expired' WHERE id=?",
                             (ev["id"],))
                continue
            rng = random.Random(f"{child_id}:ev:{ev['id']}")
            detail = _pick_weighted(rng, CRY_POOL)
            conn.execute("UPDATE scheduled_event SET status='fired', attempt_count="
                         "attempt_count+1 WHERE id=?", (ev["id"],))
            text = texts.CRY_TEXT[detail]
            responded = None
            if ev["chain_id"] == "combo":
                # 连击回应感知:主哭后照护人有没有起来管,文案不一样
                date = json.loads(ev["payload_json"] or "{}").get("date", "")
                main_ev = conn.execute(
                    "SELECT due_at FROM scheduled_event WHERE child_id=?"
                    " AND idempotency_key=?", (child_id, f"nightcry:{date}")).fetchone()
                responded = bool(main_ev and conn.execute(
                    "SELECT 1 FROM action_log WHERE child_id=? AND effective_at>=?"
                    " AND kind IN ('feed','soothe','diaper') LIMIT 1",
                    (child_id, main_ev["due_at"])).fetchone())
                text = (texts.CRY_COMBO_RESPONDED + texts.CRY_TEXT[detail]) \
                    if responded else texts.CRY_COMBO_IGNORED
            payload = {
                "kind": "nursery.cry", "detail": detail,
                "text": text, "chain": ev["chain_id"], "responded": responded,
                "voice": None if detail == "dream" else texts.FALLBACK_VOICE,
                "ts": t, "source_event_id": f"nursery:{child_id}:{ev['id']}",
            }
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind, payload_json,"
                " status, next_attempt_at, expires_at, idempotency_key)"
                " VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", "nursery.cry",
                 json.dumps(payload, ensure_ascii=False), t, ev["expires_at"],
                 payload["source_event_id"]))
            fired.append(payload)
    # 哭声=孩子的真实输出(婴儿期=真·哭声/咿呀);拿到后回写 pending 的 outbox 行
    for p in fired:
        if p["detail"] == "dream":
            continue
        try:
            res = child_mod.child_speak(conn, brain, child_id,
                                        trigger="night_cry", now=t)
            p["voice"] = res.text
            with tx(conn):
                conn.execute(
                    "UPDATE outbox SET payload_json=? WHERE idempotency_key=?"
                    " AND status='pending'",
                    (json.dumps(p, ensure_ascii=False), p["source_event_id"]))
        except Exception:
            pass  # payload 已带兜底哭声
    return fired


def steal_corpus(conn, brain: child_mod.ChildBrain, child_id: str, viewer: str,
                 now: float | None = None) -> int:
    """偷学:概率触发+每日上限;**任何环节失败=fail closed 本轮 0 条不上炸**
    (不只 connect,查询/喂养失败同样不许炸掉整次 tick;feed_corpus 失败
    自带 brain stale 标记,下次使用自动重载,安全吞)。"""
    t = child_mod._now(now)
    try:
        if child_mod.get_child(conn, child_id)["status"] != "active":
            return 0  # 出走的孩子不在家,偷不着家里的话
        rng = random.Random(f"{child_id}:steal:{int(t // 300)}")
        if rng.random() > STEAL_P:
            return 0
        today0 = _local_midnight(t)
        n_today = conn.execute(
            "SELECT COUNT(*) FROM corpus_item WHERE child_id=? AND"
            " source_kind='archive' AND acquired_at>=?",
            (child_id, today0)).fetchone()[0]
        if n_today >= STEAL_DAILY_MAX:
            return 0
        archive = connect_archive(os.getenv("NURSERY_ARCHIVE_DB", ""))
        try:
            seen = {r["source_ref"] for r in conn.execute(
                "SELECT source_ref FROM corpus_item WHERE child_id=?"
                " AND source_kind='archive' AND source_ref IS NOT NULL", (child_id,))}
            frags = sample_fragments(archive, viewer, STEAL_BATCH, rng,
                                     exclude_refs=seen)
        finally:
            archive.close()
        stolen = 0
        for f in frags:
            r = child_mod.feed_corpus(conn, brain, child_id, f["text"],
                                      source_kind="archive", source_ref=f["ref"],
                                      speaker="偷听", actor="system",
                                      idempotency_key=f"steal:{f['ref']}", now=t)
            if not r.get("duplicate") and r.get("fed"):
                stolen += 1
        return stolen
    except Exception:
        return 0  # fail closed


def deliver_outbox(conn, now: float | None = None, poster=None) -> dict:
    """至少一次投递。NURSERY_EVENT_URL 未配=留 pending(开闸前常态)。"""
    t = child_mod._now(now)
    with tx(conn):
        # 过期未投出的先弃(夜哭绝不上午补播)——必须在 no_url 早退之前:
        # 开闸前 URL 未配攒下的 pending,不清的话开闸瞬间会全喷出去
        conn.execute("UPDATE outbox SET status='dropped' WHERE status='pending'"
                     " AND expires_at IS NOT NULL AND expires_at<=?", (t,))
    url = os.getenv("NURSERY_EVENT_URL", "")
    token = os.getenv("NURSERY_EVENT_TOKEN", "")
    if not url:
        return {"delivered": 0, "skipped": "no_url"}
    if poster is None:
        def poster(payload: dict) -> bool:
            req = urllib.request.Request(
                url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {token}"} if token else {})})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=?"
        " ORDER BY id LIMIT 20", (t,)).fetchall()
    delivered = failed = 0
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["idempotency_key"] = row["idempotency_key"]
        try:
            ok = poster(payload)
        except Exception:
            ok = False
        with tx(conn):
            if ok:
                conn.execute("UPDATE outbox SET status='sent', attempt_count="
                             "attempt_count+1 WHERE id=?", (row["id"],))
                delivered += 1
            else:
                backoff = min(3600.0, 60.0 * (2 ** min(row["attempt_count"], 6)))
                conn.execute(
                    "UPDATE outbox SET attempt_count=attempt_count+1,"
                    " next_attempt_at=?, last_error='post_failed' WHERE id=?",
                    (t + backoff, row["id"]))
                failed += 1
    return {"delivered": delivered, "failed": failed}


def tick_one(db_path: str, viewer: str, now: float | None = None) -> dict:
    """单库一拍:排班→触发到期→偷学→投递。持库同款 flock(与 MCP 写并发互斥)。"""
    import fcntl
    t = child_mod._now(now)
    lock_path = os.path.join(os.path.dirname(db_path), ".lock")
    with open(lock_path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            conn = pdb.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT child_id FROM child WHERE status IN ('active','runaway')"
                    " LIMIT 1").fetchone()
                if row is None:
                    return {"skipped": "no_active_child"}
                cid = row["child_id"]
                brain = child_mod.ChildBrain.load(conn, cid)
                scheduled = schedule_night_feed(conn, cid, now=t)
                fired = fire_due_events(conn, brain, cid, now=t)
                stolen = steal_corpus(conn, brain, cid, viewer, now=t)
                # 睡眠整理:每日 07:00 后首拍重建词块索引;部署后首拍引导;
                # 任何故障=None 照旧(词块是派生数据,坏了下拍重来)
                try:
                    from .chunks import consolidate_daily
                    chunks_n = consolidate_daily(conn, cid, now=t)
                except Exception:
                    chunks_n = None
                # 观察日志:晚间从真实统计派生旁观行;故障=空照旧
                try:
                    from .observer import daily_observe
                    observed = daily_observe(conn, cid, now=t)
                except Exception:
                    observed = []
                from .events import tick_events
                evs = tick_events(conn, brain, cid, now=t)
                posted = deliver_outbox(conn, now=t)
                out = {"scheduled": scheduled, "fired": len(fired), "stolen": stolen,
                       "events": evs, "outbox": posted}
                if chunks_n is not None:
                    out["chunks"] = chunks_n
                if observed:
                    out["observed"] = observed
            finally:
                conn.close()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
    # DS 心理层:主 flock **外**、心理专用锁 **内** 跑——
    # 主 .lock 外=20s 网络绝不押整库进程锁,照护互动指令零等待;
    # .psyche.lock 内=闸门读-出网-落痕跨进程串行,非阻塞抢不到即本拍跳过,
    # 防相邻 tick TOCTOU 双出网/预算超发。
    # 阶段/节流/活动/预算闸都在 maybe_decide 内;fail-open——任何故障绝不炸整拍。
    psy = None
    try:
        plock_path = os.path.join(os.path.dirname(db_path), ".psyche.lock")
        with open(plock_path, "a") as plk:
            try:
                fcntl.flock(plk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return out   # 别的 tick 正在决策,本拍跳过
            try:
                conn = pdb.connect(db_path)
                try:
                    from .psyche import maybe_decide
                    psy = maybe_decide(conn, cid, trigger="tick", now=t)
                finally:
                    conn.close()
            finally:
                fcntl.flock(plk, fcntl.LOCK_UN)
    except Exception:
        psy = None
    if psy:
        out["psyche"] = psy.get("status")
    return out


def tick_all(now: float | None = None) -> dict:
    """全 caregiver 巡检(driver --tick 入口)。单库故障不阻断其他库。"""
    from .driver import current_players, resolve_saves_dir
    out = {}
    for persona, sub in current_players().items():  # 现读 env:.env 的 NURSERY_PLAYERS 生效
        db_path = os.path.join(resolve_saves_dir(), sub, "nursery.db")
        if not os.path.exists(db_path):
            out[sub] = {"skipped": "no_save"}
            continue
        try:
            out[sub] = tick_one(db_path, persona, now=now)
        except Exception as e:
            out[sub] = {"error": type(e).__name__}
    return out
