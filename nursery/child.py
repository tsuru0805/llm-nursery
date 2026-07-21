# -*- coding: utf-8 -*-
"""孩子的生命周期:出生/阶段推导/状态机/喂语料/说话。

事务纪律:
- 公开写入口(feed_corpus/child_speak/apply_action/save_snapshot/read_state(persist))
  一律**自己开顶层事务,禁止在外部事务内调用**(入口检测直接 raise)——嵌套语义
  (savepoint 半成品/brain 与 DB 分叉)从根上关掉;内部互调走 _locked 私有变体。
- brain 有内存副作用:事务失败标 stale,下次使用前自动重载(内存回滚替代)。
- catch-up 同时补模型与护栏索引;child_speak 锁内也 catch-up。
- 阶段=绝对时间推导;RNG 读-采样-落账同事务;坏快照新→旧回退。
- 状态结算固定 ≤1h 步长积分:分段与整段**近似**一致(误差<1,来自步长划分,
  不追求严格半群;需要精确口径时再上固定网格)。
- 时间一律 now 参数注入,默认 time.time()。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config as cfg
from .bond import apply_locked as _bond_apply_locked   # bond 只依赖 config
from .config import (ACTION_EFFECTS, ATTITUDE_REFUSE_MAX_P, DARKNESS_BY_ACTION,
                     DARKNESS_HEAL_PER_H, FATIGUE_DECAY_PER_H, HEALTH_DECAY_PER_H,
                     HEALTH_RECOVER_PER_H, HOMECOMING_OVERLAP, MAMA_ACTION_EFFECTS,
                     MAX_CHAR_ORDER, MOOD_REVERT_RATE, NUTRITION_DECAY_PER_H,
                     RUNAWAY_MIN_HOURS, SETTLE_CAP_H, STAGE_POLICY_VERSION,
                     STAGE_SCHEDULE_V1, STATE_BASELINE, TOKENIZER_VERSION)
from . import texts
from .decoder import SpeakResult, speak
from .guard import OverlapGuard, scrub_pii
from .model import VariableOrderMarkov
from .psyche import (_local_midnight, _open_night_cry_date,   # v2 情境复用(child→psyche 单向)
                     apply_rules_locked, latest_anchor_words)  # psyche 不回 import child 顶层


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


@contextmanager
def tx(conn: sqlite3.Connection):
    """顶层 BEGIN IMMEDIATE 事务。已在事务内=调用方用错了入口,直接 raise。"""
    if conn.in_transaction:
        raise RuntimeError("公开写入口不支持在外部事务内调用(用 _locked 内部变体)")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ────────────────────────── 生命周期 ──────────────────────────

def create_child(conn: sqlite3.Connection, caregiver_id: str, *,
                 name: str | None = None, status: str = "active",
                 seed: int | None = None, now: float | None = None) -> str:
    """建档。status='active' 即出生;'embryo' 为占位胚胎:无名/无出生时间/无状态行。"""
    t = _now(now)
    child_id = uuid.uuid4().hex[:12]
    seed = random.SystemRandom().randrange(2 ** 31) if seed is None else seed
    born_at = t if status == "active" else None
    with tx(conn):
        conn.execute(
            "INSERT INTO child(child_id, caregiver_id, name, status, born_at,"
            " total_paused_seconds, stage_policy_version, rng_seed, state_version,"
            " created_at, updated_at) VALUES(?,?,?,?,?,0,?,?,0,?,?)",
            (child_id, caregiver_id, name, status, born_at,
             STAGE_POLICY_VERSION, seed, t, t))
        if status == "active":
            conn.execute(
                "INSERT INTO child_state(child_id, mood, health, intimacy, nutrition,"
                " fatigue, last_settled_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (child_id, STATE_BASELINE["mood"], STATE_BASELINE["health"],
                 STATE_BASELINE["intimacy"], STATE_BASELINE["nutrition"],
                 STATE_BASELINE["fatigue"], t, t))
            if name:   # 预设名同样值一条纪念(与 name 指令定名同格式,同事务)
                conn.execute(
                    "INSERT INTO growth_album(child_id, item_kind, title, note,"
                    " created_at, pinned_at) VALUES(?,?,?,?,?,?)",
                    (child_id, "named", texts.MS_NAMED_TITLE.format(name=name),
                     texts.MS_NAMED_NOTE_PRESET, t, t))
    return child_id


def name_babble(brain: "ChildBrain", child_id: str, k: int = 2) -> str:
    """他最近总在咿呀的音:已听语料的高频字里确定性抽 k 个(零语料=空串)。

    给「一起起名」当提示用;种子含语料量,喂过新话后提示会变——他在长大。"""
    ranked = [c for c in brain.model.vocab_by_freq() if not c.isspace()][:12]
    if not ranked:
        return ""
    rng = random.Random(f"{child_id}:namebabble:{brain.model.total_chars}")
    return "".join(rng.sample(ranked, min(k, len(ranked))))


def pick_name(conn: sqlite3.Connection, brain: "ChildBrain", child_id: str,
              candidates: list[str], *, now: float | None = None) -> dict:
    """定名(一生一次)。单候选=人说了算;多候选=**他自己挑**——
    权重为候选各字在他已听语料里的 unigram 熟悉度(+1 平滑):
    越是听你说过的字,他越容易伸手去够。

    并发安全:状态检查/挑名/落库全在同一 BEGIN IMMEDIATE 内(锁内重读),
    两连接抢命名只有先者生效;已命名=返回 {"name": 现名, "already": True}
    不 raise(重试友好)。熟悉度按锁内 catch-up 后的全量语料算。
    确定性:以 (child_id, 去重候选序列) 为种子,重放同结果。仅 active 可命名。"""
    t = _now(now)
    brain._ensure_usable(conn, child_id)
    cands = list(dict.fromkeys(c.strip() for c in candidates if c.strip()))
    if not cands:
        raise ValueError("no_candidates")
    try:
        with tx(conn):
            child = get_child(conn, child_id)   # 锁内重读:并发命名只有先者生效
            if child["status"] != "active":
                raise ValueError(child["status"])
            if child["name"]:
                return {"name": child["name"], "already": True}
            brain._replay_after_cursor(conn)    # 熟悉度不吃旧视图
            uni = brain.model.counts[0].get("", {})
            weights = [float(sum(uni.get(ch, 0) for ch in c)) + 1.0 for c in cands]
            rng = random.Random(f"{child_id}:naming:{'|'.join(cands)}")
            chosen = cands[0] if len(cands) == 1 else \
                rng.choices(cands, weights=weights, k=1)[0]
            conn.execute(
                "UPDATE child SET name=?, updated_at=? WHERE child_id=?"
                " AND name IS NULL", (chosen, t, child_id))
            conn.execute(
                "INSERT INTO growth_album(child_id, item_kind, title, note,"
                " created_at, pinned_at) VALUES(?,?,?,?,?,?)",
                (child_id, "named", texts.MS_NAMED_TITLE.format(name=chosen),
                 texts.MS_NAMED_NOTE.format(candidates="、".join(cands)), t, t))
    except BaseException:
        brain.stale = True   # 锁内 catch-up 可能已推进内存而 DB 回滚
        raise
    return {"name": chosen, "already": False, "candidates": cands,
            "weights": dict(zip(cands, weights))}


def hatch_child(conn: sqlite3.Connection, child_id: str, *,
                name: str | None = None, now: float | None = None) -> str:
    """embryo 孵化转正:补名字/出生时间/状态行,status→active。已 active=幂等原样返回。"""
    t = _now(now)
    with tx(conn):
        child = get_child(conn, child_id)
        if child["status"] != "embryo":
            return child_id
        conn.execute(
            "UPDATE child SET status='active', name=COALESCE(?, name), born_at=?,"
            " updated_at=? WHERE child_id=?", (name, t, t, child_id))
        conn.execute(
            "INSERT OR IGNORE INTO child_state(child_id, mood, health, intimacy,"
            " nutrition, fatigue, last_settled_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (child_id, STATE_BASELINE["mood"], STATE_BASELINE["health"],
             STATE_BASELINE["intimacy"], STATE_BASELINE["nutrition"],
             STATE_BASELINE["fatigue"], t, t))
        if name:   # 孵化即定名的,同样落纪念(embryo 期本就无名,不会双记)
            conn.execute(
                "INSERT INTO growth_album(child_id, item_kind, title, note,"
                " created_at, pinned_at) VALUES(?,?,?,?,?,?)",
                (child_id, "named", texts.MS_NAMED_TITLE.format(name=name),
                 texts.MS_NAMED_NOTE_PRESET, t, t))
    return child_id


def get_child(conn: sqlite3.Connection, child_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM child WHERE child_id=?", (child_id,)).fetchone()
    if row is None:
        raise KeyError(f"child {child_id} 不存在")
    return row


def logical_age_days(child: sqlite3.Row, now: float | None = None) -> float:
    if child["born_at"] is None:
        return -1.0  # embryo
    t = _now(now)
    paused = child["total_paused_seconds"] or 0.0
    if child["paused_at"] is not None:
        paused += max(0.0, t - child["paused_at"])
    return max(0.0, (t - child["born_at"] - paused)) / 86400.0


def stage_of(child: sqlite3.Row, now: float | None = None) -> str:
    if child["status"] == "embryo":
        return "embryo"
    age = logical_age_days(child, now)
    for stage, upper in STAGE_SCHEDULE_V1:
        if age < upper:
            return stage
    return STAGE_SCHEDULE_V1[-1][0]


# ────────────────────────── 状态机(读时惰性结算,固定步长积分) ──────────────────────────

def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _digest_decay_rate(abs_t: float | None) -> float:
    """消化速率:夜窗(本地 23:00-07:00)=睡眠整理大幅回落;abs_t=None
    (直调 settle_state 没给起点)按白天速率兜底。"""
    if abs_t is None:
        return cfg.DIGEST_DECAY_PER_H
    hour = time.localtime(abs_t).tm_hour
    if hour >= cfg.DIGEST_NIGHT_START_H or hour < cfg.DIGEST_NIGHT_END_H:
        return cfg.DIGEST_NIGHT_DECAY_PER_H
    return cfg.DIGEST_DECAY_PER_H


def _settle_step(s: dict, dt: float, abs_t: float | None = None) -> None:
    """单步(dt≤1h)就地演化。abs_t=本步起点的绝对时刻(消化夜窗判定用)。"""
    s["fatigue"] = _clamp(s["fatigue"] - FATIGUE_DECAY_PER_H * dt)
    if s["nutrition"] < 15:
        s["health"] = _clamp(s["health"] - HEALTH_DECAY_PER_H * dt)
    elif s["nutrition"] > 30:
        s["health"] = _clamp(s["health"] + HEALTH_RECOVER_PER_H * dt)
    s["nutrition"] = _clamp(s["nutrition"] - NUTRITION_DECAY_PER_H * dt)
    base = STATE_BASELINE["mood"]
    s["mood"] = _clamp(base + (s["mood"] - base) * ((1 - MOOD_REVERT_RATE) ** dt))
    if "darkness" in s:
        s["darkness"] = _clamp(s["darkness"] - DARKNESS_HEAL_PER_H * dt)  # 缓慢自愈
    if "digest_load" in s:
        s["digest_load"] = _clamp(s["digest_load"] - _digest_decay_rate(abs_t) * dt)


def _secs_to_night_boundary(abs_t: float) -> float:
    """到下一个夜窗边界(本地 23:00 或 07:00)的秒数(>0)。"""
    lt = time.localtime(abs_t)
    sec_of_day = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (abs_t % 1.0)
    bounds = sorted({cfg.DIGEST_NIGHT_START_H * 3600, cfg.DIGEST_NIGHT_END_H * 3600})
    for b in bounds:
        if sec_of_day < b:
            return b - sec_of_day
    return 86400 - sec_of_day + bounds[0]


def settle_state(state: dict, hours: float, start: float | None = None) -> dict:
    """纯函数:h 小时自然演化,固定 ≤1h 步长积分。分段与整段近似一致(误差<1)。
    start=结算起点的绝对时刻(消化夜窗判定):步长额外贴夜窗边界切开,
    消化分量为分段常速率的精确积分——任意切分点整段/分段结果一致;
    None=消化按白天速率(旧签名兼容)。"""
    h = max(0.0, min(hours, SETTLE_CAP_H))
    s = dict(state)
    cursor = start
    guard = 0
    while h > 1e-9 and guard < SETTLE_CAP_H * 4:
        guard += 1
        dt = min(1.0, h)
        if cursor is not None:
            dt = min(dt, max(1e-6, _secs_to_night_boundary(cursor) / 3600.0))
        _settle_step(s, dt, abs_t=cursor)
        if cursor is not None:
            cursor += dt * 3600.0
        h -= dt
    return s


def _read_state_locked(conn: sqlite3.Connection, child_id: str, t: float,
                       persist: bool) -> dict:
    row = conn.execute("SELECT * FROM child_state WHERE child_id=?", (child_id,)).fetchone()
    if row is None:
        raise KeyError(f"child_state {child_id} 不存在(embryo 无状态)")
    state = {k: row[k] for k in ("mood", "health", "intimacy", "nutrition", "fatigue",
                                 "darkness", "digest_load")}
    hours = (t - row["last_settled_at"]) / 3600.0
    settled = settle_state(state, hours, start=row["last_settled_at"])
    if persist and hours > 0.01:
        conn.execute(
            "UPDATE child_state SET mood=?, health=?, intimacy=?, nutrition=?,"
            " fatigue=?, darkness=?, digest_load=?, last_settled_at=?, updated_at=?"
            " WHERE child_id=?",
            (settled["mood"], settled["health"], settled["intimacy"],
             settled["nutrition"], settled["fatigue"], settled["darkness"],
             settled["digest_load"], t, t, child_id))
    return settled


def read_state(conn: sqlite3.Connection, child_id: str, now: float | None = None,
               persist: bool = True) -> dict:
    """读状态(惰性结算)。persist=True 时读-结算-写同一写锁。"""
    t = _now(now)
    if not persist:
        return _read_state_locked(conn, child_id, t, persist=False)
    with tx(conn):
        return _read_state_locked(conn, child_id, t, persist=True)


def _action_effects(kind: str) -> dict:
    """动作基础效果表:主照护人的 ACTION_EFFECTS + 妈妈通道 MAMA_ACTION_EFFECTS。"""
    return ACTION_EFFECTS.get(kind) or MAMA_ACTION_EFFECTS.get(kind) or {}


def _rules_v2_since(conn: sqlite3.Connection, child_id: str) -> float:
    """v2 取舍规则对该孩子的生效时刻=max(全局配置, 老档升级 stamp)。
    新档无 stamp=纯看配置(默认 0=全程生效);老档升级当日,升级前的动作
    不进递减计数、不触发消化/情境化——「不追溯」是真承诺。"""
    row = conn.execute(
        "SELECT value FROM parenting_meta WHERE child_id=? AND key='rules_v2_since'",
        (child_id,)).fetchone()
    stamp = 0.0
    if row is not None:
        try:
            stamp = float(row["value"])
        except (TypeError, ValueError):
            stamp = 0.0
    return max(cfg.RULES_V2_SINCE, stamp)


def _daily_repeat_count(conn: sqlite3.Connection, child_id: str, kind: str,
                        t: float) -> int:
    """当日(本地零点起,且不早于 RULES_V2_SINCE)已落账的同类动作次数。
    幂等重放在上层早退不进这里,不会虚增;当前动作尚未插入,<=t 不含自己,
    同秒已提交的动作也计入。"""
    day0 = max(_local_midnight(t), _rules_v2_since(conn, child_id))
    return conn.execute(
        "SELECT COUNT(*) FROM action_log WHERE child_id=? AND kind=?"
        " AND effective_at>=? AND effective_at<=?",
        (child_id, kind, day0, t)).fetchone()[0]


def _apply_action_locked(conn: sqlite3.Connection, child_id: str, actor: str, kind: str,
                         *, idempotency_key: str, payload: dict | None,
                         extra_effects: dict | None, t: float) -> dict:
    dup = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key=?",
        (child_id, idempotency_key)).fetchone()
    if dup is not None:
        return json.loads(dup["payload_json"] or "{}").get("state_after", {})

    state = _read_state_locked(conn, child_id, t, persist=False)
    effects = dict(_action_effects(kind))
    for k, v in (extra_effects or {}).items():
        effects[k] = effects.get(k, 0.0) + v
    # 黑暗值:管教涨(亲密<30 翻倍),温暖动作降
    dk = DARKNESS_BY_ACTION.get(kind, 0.0)
    if kind == "discipline" and state["intimacy"] < 30:
        dk *= 2
    if dk:
        effects["darkness"] = effects.get("darkness", 0.0) + dk

    # 夜哭窗是否开着:v2 情境与关系账共用(date 给 bond 做每夜一次去重)
    night_date = _open_night_cry_date(conn, child_id, t)
    night_open = night_date is not None
    # ── 养成取舍 v2 情境(切换时刻前=全额老规则,不追溯)──
    factor, calm = 1.0, False
    if t >= _rules_v2_since(conn, child_id):
        if kind in cfg.DAILY_DECAY_KINDS and not (
                night_open and kind in cfg.PSYCHE_NIGHT_RESPONSE_KINDS):
            # 当日同类收益递减;夜哭窗口内的响应动作永远全额(夜奶体验不动)
            n = _daily_repeat_count(conn, child_id, kind, t)
            factor = max(cfg.DAILY_DECAY_FLOOR, cfg.DAILY_DECAY ** n)
        if kind in cfg.CALM_SOOTHE_KINDS and not night_open and                 state["mood"] >= cfg.CALM_SOOTHE_MOOD_MIN:
            calm = True   # 他本来就平静:心理账走依赖口径(psyche 层)
    if factor != 1.0:
        # 递减动作集不含营养/负荷键(feed/mama_say 走语料线,不在集内),整表同乘
        effects = {k: v * factor for k, v in effects.items()}
    after = dict(state)
    for k, v in effects.items():
        after[k] = _clamp(after.get(k, 0.0) + v)

    child = get_child(conn, child_id)
    ver = child["state_version"]
    # 心理程序层:确定性规则表落三轴账(同事务;重放动作在上面早退=同幂等,不双记)
    psy = apply_rules_locked(conn, child_id, kind, t, source_key=idempotency_key,
                             scale=factor, calm=calm)
    # 对"这个人"的关系账(同事务同幂等;actor 不在照护人表=零写入)
    bnd = _bond_apply_locked(conn, child_id, actor, kind, t,
                             source_key=idempotency_key, scale=factor,
                             night_date=night_date, calm=calm)
    record = {"action": kind, "effects": effects,
              "state_before": state, "state_after": after,
              "user_payload": payload or {}}
    if factor != 1.0:
        record["decay_factor"] = round(factor, 4)
    if calm:
        record["calm_soothe"] = True
    if bnd:
        record["bond"] = bnd
    if psy:
        record["psyche"] = psy
    conn.execute(
        "UPDATE child_state SET mood=?, health=?, intimacy=?, nutrition=?, fatigue=?,"
        " darkness=?, digest_load=?, last_settled_at=?, last_interaction_at=?,"
        " last_fed_at=CASE WHEN ?='feed' THEN ? ELSE last_fed_at END,"
        " updated_at=? WHERE child_id=?",
        (after["mood"], after["health"], after["intimacy"], after["nutrition"],
         after["fatigue"], after["darkness"], after["digest_load"],
         t, t, kind, t, t, child_id))
    conn.execute(
        "UPDATE child SET state_version=?, updated_at=? WHERE child_id=?",
        (ver + 1, t, child_id))
    conn.execute(
        "INSERT INTO action_log(child_id, actor, kind, payload_json, effective_at,"
        " created_at, idempotency_key, state_version_before, state_version_after)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (child_id, actor, kind, json.dumps(record, ensure_ascii=False), t, t,
         idempotency_key, ver, ver + 1))
    return after


def apply_action(conn: sqlite3.Connection, child_id: str, actor: str, kind: str, *,
                 idempotency_key: str, payload: dict | None = None,
                 extra_effects: dict | None = None,
                 now: float | None = None) -> dict:
    """动作落账(幂等,查重在写锁内)。重放同 key 返回既有结果不重复生效。"""
    if not _action_effects(kind) and extra_effects is None:
        raise ValueError(f"未知动作 {kind}")
    t = _now(now)
    with tx(conn):
        return _apply_action_locked(conn, child_id, actor, kind,
                                    idempotency_key=idempotency_key, payload=payload,
                                    extra_effects=extra_effects, t=t)


def _derive_scene(conn, child_id: str, source_kind: str, action_kind: str,
                  t: float) -> str:
    """场景标签自动派生:从动作上下文推,不用任何人手选。旧语料 NULL=legacy。"""
    if source_kind == "archive":
        return "overheard"
    if source_kind in ("night_feed", "book"):
        return "bedtime"
    if action_kind == "teach":
        return "teaching"
    if _open_night_cry_date(conn, child_id, t) is not None:
        return "comfort"
    hour = time.localtime(t).tm_hour
    if hour >= cfg.DIGEST_NIGHT_START_H or hour < cfg.DIGEST_NIGHT_END_H:
        return "bedtime"
    return "daily"


# ────────────────────────── 大脑装载/喂语料/说话 ──────────────────────────

class ChildBrain:
    """一个孩子的可采样大脑:有效快照 + 游标后语料增量重放 + 护栏索引。

    调用方按 caregiver 各持一份(禁模块级 CURRENT_* 全局)。
    事务失败后实例自动标 stale,再次使用前自动重载(内存回滚替代)。
    """

    def __init__(self, child_id: str):
        self.child_id = child_id
        self.model = VariableOrderMarkov(MAX_CHAR_ORDER)
        self.guard = OverlapGuard()
        self.trained_through = 0
        self.snapshot_id: int | None = None
        self.snapshot_cursor = -1   # snapshot_id 对应的训练游标(留痕一致性判据)
        self.stale = False

    # ── 装载 ──
    @classmethod
    def load(cls, conn: sqlite3.Connection, child_id: str) -> "ChildBrain":
        get_child(conn, child_id)  # 不存在早炸
        brain = cls(child_id)
        max_corpus = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM corpus_item WHERE child_id=?",
            (child_id,)).fetchone()[0]
        # 快照按新→旧回退到首个有效者;全坏=从零重放
        for snap in conn.execute(
                "SELECT * FROM model_snapshot WHERE child_id=? ORDER BY id DESC",
                (child_id,)):
            blob = snap["model_blob"]
            if VariableOrderMarkov.checksum(blob) != snap["checksum"]:
                continue
            if snap["trained_through_corpus_id"] > max_corpus:
                continue  # 游标越界(异库拷贝/回滚残留)不可信
            try:
                model = VariableOrderMarkov.from_blob(blob)
            except Exception:
                continue
            brain.model = model
            brain.trained_through = snap["trained_through_corpus_id"]
            brain.snapshot_id = snap["id"]
            brain.snapshot_cursor = snap["trained_through_corpus_id"]
            break
        # 快照不含护栏索引 → 模型补游标后语料(不带 guard),护栏全量重建
        brain._replay_after_cursor(conn, update_guard=False)
        for r in conn.execute(
                "SELECT text FROM corpus_item WHERE child_id=? ORDER BY id", (child_id,)):
            brain.guard.add_source(r["text"])
        return brain

    def _replay_after_cursor(self, conn: sqlite3.Connection, *,
                             update_guard: bool = True) -> int:
        """按 DB 游标补齐内存模型(catch-up)。update_guard=True 同步补护栏索引。

        load() 全量重建护栏时置 False 免重复;增量 catch-up(feed/speak 路径)必须 True。
        """
        rows = conn.execute(
            "SELECT id, text, training_weight FROM corpus_item"
            " WHERE child_id=? AND id>? ORDER BY id",
            (self.child_id, self.trained_through)).fetchall()
        for r in rows:
            self.model.feed(r["text"], weight=r["training_weight"])
            if update_guard:
                self.guard.add_source(r["text"])
            self.trained_through = r["id"]
        return len(rows)

    def reload(self, conn: sqlite3.Connection) -> "ChildBrain":
        fresh = ChildBrain.load(conn, self.child_id)
        self.__dict__.update(fresh.__dict__)
        return self

    def _ensure_usable(self, conn: sqlite3.Connection, child_id: str) -> None:
        if self.child_id != child_id:
            raise ValueError(f"brain({self.child_id}) 与 child({child_id}) 不匹配,禁止串训")
        if self.stale:
            self.reload(conn)
            self.stale = False

    def _save_snapshot_locked(self, conn: sqlite3.Connection, t: float) -> int:
        blob = self.model.to_blob()
        conn.execute("UPDATE model_snapshot SET is_active=0 WHERE child_id=?",
                     (self.child_id,))
        cur = conn.execute(
            "INSERT INTO model_snapshot(child_id, format_version, tokenizer_version,"
            " max_char_order, trained_through_corpus_id, model_blob, checksum,"
            " created_at, is_active) VALUES(?,?,?,?,?,?,?,?,1)",
            (self.child_id, 1, TOKENIZER_VERSION, self.model.max_order,
             self.trained_through, blob, VariableOrderMarkov.checksum(blob), t))
        self.snapshot_id = cur.lastrowid
        self.snapshot_cursor = self.trained_through
        return self.snapshot_id

    def save_snapshot(self, conn: sqlite3.Connection, now: float | None = None) -> int:
        t = _now(now)
        try:
            with tx(conn):
                return self._save_snapshot_locked(conn, t)
        except BaseException:
            self.stale = True  # snapshot_id 可能指向已回滚的行
            raise


def feed_corpus(conn: sqlite3.Connection, brain: ChildBrain, child_id: str, text: str, *,
                source_kind: str = "direct", speaker: str | None = None,
                source_ref: str | None = None, actor: str = "papa",
                action_kind: str = "feed", scene: str | None = None,
                idempotency_key: str | None = None, training_weight: float = 1.0,
                snapshot: bool = True, now: float | None = None) -> dict:
    """喂语料:PII 遮盖→(锁内)catch-up→去重→入库→模型增量→快照→营养动作账,单顶层事务。

    失败时 DB 全量回滚,brain 标 stale 下次自动重载。不支持在外部事务内调用。
    action_kind:动作账落的 kind,默认 'feed'(主照护人);妈妈通道用 'mama_say'
    ——避免妈妈的话在动作账里冒充主照护人的 feed(夜哭响应/结局响应率按 kind 过滤)。
    营养仍按语料多样性口径落账,与 kind 无关。
    """
    if not _action_effects(action_kind):
        raise ValueError(f"未知喂语料动作 {action_kind}")
    t = _now(now)
    brain._ensure_usable(conn, child_id)
    child = get_child(conn, child_id)
    if child["status"] == "embryo":
        raise ValueError("受精卵还不能喂语料(还没出生)")
    if child["status"] != "active":
        raise ValueError(f"{child['status']} 状态不能喂语料")
    clean, flags = scrub_pii(text)
    clean = clean.strip()
    if not clean:
        return {"fed": 0, "duplicate": False, "pii_flags": flags}
    h = hashlib.sha256(clean.encode("utf-8")).hexdigest()

    act_key = idempotency_key or f"{action_kind}:{h[:16]}"
    try:
        with tx(conn):
            brain._replay_after_cursor(conn)  # 锁内 catch-up(模型+护栏),游标不跳号
            dup = conn.execute(
                "SELECT id FROM corpus_item WHERE child_id=? AND content_hash=?",
                (child_id, h)).fetchone()
            if dup is not None:
                return {"fed": 0, "duplicate": True, "pii_flags": flags}
            # 动作幂等键先查:同 key 不同正文的重试若放行,语料/模型会提交而
            # 动作账早退=两本账分叉。视为重放,整体不生效。
            if conn.execute(
                    "SELECT 1 FROM action_log WHERE child_id=? AND idempotency_key=?",
                    (child_id, act_key)).fetchone() is not None:
                return {"fed": 0, "duplicate": True, "pii_flags": flags}

            # 营养:多样性口径(新字比例)——刷同一句话喂不胖
            known = set(brain.model.vocab_by_freq())
            fresh = len({c for c in clean if not c.isspace()} - known)
            nutrition_delta = min(12.0, len(clean) / 25.0 + fresh * 0.4)

            # ── 消化负荷:照护者语料进账;过载=吸收打折(语料照样入库,学得浅)──
            v2_since = _rules_v2_since(conn, child_id)
            digestible = t >= v2_since and \
                source_kind in cfg.DIGEST_SOURCE_KINDS
            st_now = _read_state_locked(conn, child_id, t, persist=False)
            overloaded = t >= v2_since and \
                st_now.get("digest_load", 0.0) >= cfg.DIGEST_OVERLOAD_AT
            eff_weight = training_weight * \
                (cfg.DIGEST_ABSORB_FACTOR if overloaded else 1.0)
            if overloaded:
                nutrition_delta *= cfg.DIGEST_ABSORB_FACTOR

            sc = scene or _derive_scene(conn, child_id, source_kind, action_kind, t)
            cur = conn.execute(
                "INSERT INTO corpus_item(child_id, source_kind, source_ref, speaker,"
                " text, content_hash, tokenizer_version, char_count, privacy_flags,"
                " training_weight, scene, acquired_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (child_id, source_kind, source_ref, speaker, clean, h,
                 TOKENIZER_VERSION, len(clean), json.dumps(flags) if flags else None,
                 eff_weight, sc, t))   # 落打折后权重:catch-up 重放与当场喂结果一致
            corpus_id = cur.lastrowid
            brain.model.feed(clean, weight=eff_weight)
            brain.guard.add_source(clean)
            brain.trained_through = corpus_id
            if snapshot:
                brain._save_snapshot_locked(conn, t)
            extra = {"nutrition": nutrition_delta -
                     _action_effects(action_kind).get("nutrition", 0.0)}
            if digestible:
                extra["digest_load"] = len(clean) * cfg.DIGEST_PER_CHAR
            after = _apply_action_locked(
                conn, child_id, actor, action_kind,
                idempotency_key=act_key,
                payload={"corpus_id": corpus_id, "chars": len(clean)},
                extra_effects=extra, t=t)
    except BaseException:
        brain.stale = True  # 内存已可能被 feed 污染,下次使用前重载
        raise
    return {"fed": len(clean), "duplicate": False, "pii_flags": flags,
            "corpus_id": corpus_id, "nutrition_delta": nutrition_delta,
            "digest_load": after.get("digest_load", 0.0), "overloaded": overloaded}


def child_speak(conn: sqlite3.Connection, brain: ChildBrain, child_id: str, *,
                trigger: str = "manual", recent_n: int = 20,
                now: float | None = None) -> SpeakResult:
    """让孩子说一句话:锁内 catch-up+RNG 读-采样-落账同事务;utterance 全留痕。"""
    t = _now(now)
    brain._ensure_usable(conn, child_id)
    try:
        with tx(conn):
            child = get_child(conn, child_id)
            if child["status"] == "embryo":
                raise ValueError("受精卵还不会说话")
            if child["status"] == "runaway":
                raise ValueError("runaway")  # 推理端离线,driver 渲染"打不通"
            if child["status"] == "graduated":
                raise ValueError("graduated")  # 已毕业,摇篮房不再出声
            stage = stage_of(child, t)
            brain._replay_after_cursor(conn)
            if brain.snapshot_id is None or brain.trained_through != brain.snapshot_cursor:
                # 当前模型游标 ≠ 快照游标(catch-up 或 load 走了旧快照+重放):
                # 同事务落新快照,utterance 留痕永远指向真实模型
                brain._save_snapshot_locked(conn, t)

            rng = random.Random()
            if child["rng_state"]:
                rng.setstate(_state_from_json(child["rng_state"]))
            else:
                rng.seed(child["rng_seed"])

            st_now = _read_state_locked(conn, child_id, t, persist=False)
            # 态度层:teen 期黑暗值 → 已读不回概率(听懂了,但他就是不)
            refuse_p = 0.0
            if stage == "teen":
                refuse_p = (st_now.get("darkness", 0.0) / 100.0) * ATTITUDE_REFUSE_MAX_P
            # 消化过载 → 出口碎化比例(超过阈值的部分线性到 1)
            overload = 0.0
            if t >= _rules_v2_since(conn, child_id):
                d = st_now.get("digest_load", 0.0)
                if d > cfg.DIGEST_OVERLOAD_AT:
                    overload = min(1.0, (d - cfg.DIGEST_OVERLOAD_AT) /
                                   max(1.0, 100.0 - cfg.DIGEST_OVERLOAD_AT))

            recent = [r["text"] for r in conn.execute(
                "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
                " ORDER BY id DESC LIMIT ?", (child_id, recent_n))]
            # 锚词接力:最近一次有效 psyche 决策 → 采样软偏置;
            # 无决策/超 TTL/心理层任何故障 = 零偏置照旧(fail-open,绝不拦嘴)
            try:
                anchors = latest_anchor_words(conn, child_id, t)
            except Exception:
                anchors = None
            # 家庭词块:按阶段概率整词起头(婴儿=0);场景倾向随触发源;
            # 索引空/任何故障=不起头照旧(fail-open)。确定性口径:同 rng_state+
            # 同索引内容 ⇒ 同结果;p=0 不抽签不耗 rng。
            chunk = None
            p_seed = cfg.CHUNK_SEED_P.get(stage, 0.0)
            if p_seed > 0 and rng.random() < p_seed:
                from .chunks import pick_chunk
                try:
                    chunk = pick_chunk(conn, child_id, rng,
                                       scene_hint=cfg.SPEAK_SCENE_HINT.get(trigger))
                except Exception:
                    chunk = None
            result = speak(brain.model, brain.guard, stage, rng,
                           recent_texts=recent, refuse_p=refuse_p,
                           anchor_words=anchors, overload=overload, chunk=chunk)

            conn.execute(
                "INSERT INTO utterance(child_id, trigger, model_snapshot_id, stage, text,"
                " generation_params_json, max_source_overlap, accepted, rejection_reason,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (child_id, trigger, brain.snapshot_id, stage, result.text,
                 json.dumps(result.params, ensure_ascii=False), result.max_overlap,
                 1 if result.accepted else 0,
                 None if result.accepted else
                 ("refused" if result.refused else
                  # 真试过(retries>0)才叫护栏耗尽;空模型零重试=no_model
                  ("guard_exhausted" if result.retries > 0 else "no_model")), t))
            conn.execute("UPDATE child SET rng_state=?, updated_at=? WHERE child_id=?",
                         (_state_to_json(rng.getstate()), t, child_id))
    except BaseException:
        brain.stale = True  # catch-up 可能已推进内存但 DB 回滚
        raise
    return result


def attempt_homecoming(conn: sqlite3.Connection, brain: ChildBrain, child_id: str,
                       text: str, now: float | None = None, *,
                       actor: str = "papa") -> bool:
    """离家出走的找回 gate:隔空喊话与「你教过他的话」连续重合 ≥HOMECOMING_OVERLAP
    (把对他说过的话原样再说一遍=他想起来了)。出走不满 RUNAWAY_MIN_HOURS 喊不应。"""
    t = _now(now)
    brain._ensure_usable(conn, child_id)
    child = get_child(conn, child_id)
    if child["status"] != "runaway":
        return False
    if child["runaway_at"] is not None and \
            t - child["runaway_at"] < RUNAWAY_MIN_HOURS * 3600:
        return False
    from .guard import OverlapGuard
    warm = OverlapGuard()
    for r in conn.execute(
            "SELECT text FROM corpus_item WHERE child_id=? AND source_kind IN"
            " ('direct','night_feed','book')", (child_id,)):
        warm.add_source(r["text"])
    if warm.max_overlap(text) < HOMECOMING_OVERLAP:
        return False
    with tx(conn):
        conn.execute("UPDATE child SET status='active', runaway_at=NULL, updated_at=?"
                     " WHERE child_id=?", (t, child_id))
        # 状态变化走 action 账(action_log=真相层,state_version 推进)
        _apply_action_locked(
            conn, child_id, actor, "homecoming",
            idempotency_key=f"hc:{child_id}:{int(t)}",
            payload={"call": text[:80]},
            extra_effects={"darkness": -30.0, "intimacy": +10.0, "mood": +8.0}, t=t)
        conn.execute(
            "INSERT OR IGNORE INTO outbox(child_id, target, kind, payload_json,"
            " status, next_attempt_at, idempotency_key) VALUES(?,?,?,?,'pending',?,?)",
            (child_id, "webhook", "nursery.homecoming",
             json.dumps({"kind": "nursery.homecoming", "ts": t,
                         "source_event_id": f"hc:{child_id}:{int(t)}"},
                        ensure_ascii=False), t, f"hc:{child_id}:{int(t)}"))
    return True


def _state_to_json(state) -> str:
    return json.dumps([state[0], list(state[1]), state[2]])


def _state_from_json(s: str):
    v = json.loads(s)
    return (v[0], tuple(v[1]), v[2])
