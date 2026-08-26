# -*- coding: utf-8 -*-
"""摩擦轴节律。

设计原则:冲突从正常生活长出来——摩擦轴 annoyance 独立于黑暗值。darkness 保持
虐待线语义(管教/忽视)一个字不动;annoyance 只从唠叨(child._apply_action_locked
内落账)与「被晾」(本文件,复用 observer quiet 公共口径)长出来,给台阶就消。

本文件管 tick 侧三件(全幂等,故障=空照旧,tick 不许炸):
- 被晾:白天最长无互动间隔 ≥ observer 同阈值(21 点后判一次/日)→ annoyance+
  (action_log kind='left_alone',actor=system;不在 PSYCHE/BOND/DARKNESS 任何
  规则表里=心理与关系账全零,摩擦就是摩擦)。
- 摔门:annoyance 过阈**确定性**触发,每日至多一次(outbox 幂等键 doorslam:{date})。
- 深夜彩蛋(设计原案):23 点后低概率(日种子确定性抽签)nursery.event
  「loading_family_memory.dump… {pct}%」,百分比从**真实语料量**派生,不编。

另出顶嘴拧话的取词口 recent_direct_anchors(child_speak snark 通道用):
父母最近 direct 语料 → 锚词,纯派生确定性,不耗 rng。
"""
from __future__ import annotations

import random
import time

from . import child as child_mod
from . import config as cfg
from . import texts
from .chunks import _clean_runs
from .events import _emit
from .observer import _midnight, quiet_gap_seconds


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def annoy_stage(child, t: float) -> str | None:
    """摩擦轴阶段闸(child 后半起生效):返回生效阶段名,不生效=None。
    child 前半不闹(懂事得早,ANNOY_CHILD_FROM_FRAC 判「后半」);teen 全程。
    child.py 唠叨/台阶账与本文件 tick 三件共用这一个闸,别各写各漂。"""
    stage = child_mod.stage_of(child, t)
    if stage not in cfg.ANNOY_STAGES:
        return None
    if stage == "child":
        lo, hi = 0.0, None
        for st, upper in child_mod.stage_schedule_for(child):
            if st == "child":
                hi = upper
                break
            lo = upper
        if hi is None:
            return None   # 该 policy 没有 child 段=不生效(fail closed)
        if child_mod.logical_age_days(child, t) < \
                lo + (hi - lo) * cfg.ANNOY_CHILD_FROM_FRAC:
            return None
    return stage


def recent_direct_anchors(conn, child_id: str) -> list | None:
    """顶嘴拧话取词:父母最近 direct 语料的「话芯」段 → 锚词列表(每个 ≤8 字,
    与 psyche 锚词同尺)。纯派生、确定性、零 rng 消耗;取不出=None(fail-open,
    调用方回落既有锚词路)。"""
    rows = conn.execute(
        "SELECT text FROM corpus_item WHERE child_id=? AND source_kind='direct'"
        " ORDER BY id DESC LIMIT ?", (child_id, cfg.SNARK_SOURCE_ROWS)).fetchall()
    anchors: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for run in _clean_runs(r["text"] or ""):
            w = run[:8]
            if w and w not in seen:
                seen.add(w)
                anchors.append(w)
            if len(anchors) >= cfg.SNARK_MAX_ANCHORS:
                return anchors
    return anchors or None


def _quiet_annoyance(conn, child_id: str, t: float,
                     stage: str = "teen") -> bool:
    """被晾:21 点后(observer 时段)判一次,当日幂等(action_log 键
    frictquiet:{date})。判定=observer.quiet_gap_seconds 公共口径,不重造统计。"""
    if time.localtime(t).tm_hour < cfg.OBSERVE_AFTER_H:
        return False
    date = _local_date(t)
    if conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND idempotency_key=?",
            (child_id, f"frictquiet:{date}")).fetchone() is not None:
        return False
    # 升级当日不翻旧账:窗起点不早于 v0.3 生效时刻(升级前那半天的
    # 「没人理」不算账;observer 观察行照旧,只有摩擦账收这个闸)
    v3 = child_mod._rules_v3_since(conn, child_id)
    gap = quiet_gap_seconds(conn, child_id, _midnight(t), t, not_before=v3)
    if gap is None or gap < cfg.OBSERVE_QUIET_GAP_H * 3600:
        return False
    child_mod.apply_action(
        conn, child_id, "system", "left_alone",
        idempotency_key=f"frictquiet:{date}",
        payload={"date": date, "gap_h": round(gap / 3600, 1)},
        extra_effects={"annoyance": cfg.ANNOY_QUIET_STEP *
                       cfg.ANNOY_STAGE_SCALE.get(stage, 1.0)}, now=t)
    return True


def _door_slam(conn, child_id: str, t: float, stage: str = "teen") -> bool:
    """摔门:annoyance ≥ 阈值确定性触发,每日至多一次(outbox 幂等)。"""
    st = child_mod.read_state(conn, child_id, now=t, persist=False)
    if st.get("annoyance", 0.0) < cfg.ANNOY_DOOR_AT:
        return False
    date = _local_date(t)
    return _emit(conn, child_id, kind="nursery.event", item_kind=None,
                 title=texts.DOOR_SLAM, note=None,
                 payload={"friction": "door_slam"},
                 idem=f"doorslam:{date}:{child_id}", t=t, expires_at=t + 86400)


def _night_egg(conn, child_id: str, t: float, stage: str = "teen") -> bool:
    """深夜彩蛋:23 点后,(child,date) 种子低概率抽签;百分比=真实语料总字数派生
    (SUM(char_count) % 100);零语料=不发(不编数)。幂等 per date。"""
    if time.localtime(t).tm_hour < cfg.NIGHT_EGG_HOUR:
        return False
    date = _local_date(t)
    # 幂等键前置(评审定案):当日已发就直接回,不再跑生命周期级
    # SUM 聚合——同晚重复 tick 的扫描量有界(每晚聚合至多一次)
    if conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=? LIMIT 1",
                    (f"nightegg:{date}:{child_id}",)).fetchone() is not None:
        return False
    rng = random.Random(f"{child_id}:nightegg:{date}")
    if rng.random() > cfg.NIGHT_EGG_P:
        return False
    total = conn.execute(
        "SELECT COALESCE(SUM(char_count),0) FROM corpus_item WHERE child_id=?",
        (child_id,)).fetchone()[0]
    if total <= 0:
        return False
    pct = total % 100
    return _emit(conn, child_id, kind="nursery.event", item_kind=None,
                 title=texts.NIGHT_EGG.format(pct=pct), note=None,
                 payload={"friction": "night_egg", "corpus_chars": total},
                 idem=f"nightegg:{date}:{child_id}", t=t, expires_at=t + 86400)


def tick_friction(conn, child_id: str, now: float | None = None) -> dict:
    """scheduler 每拍调(自守闸):只在 active + ANNOY_STAGES 生效。
    单件故障不拦别件(与 observer 同哲学)。"""
    t = child_mod._now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return {}
    stage = annoy_stage(child, t)
    if stage is None:
        return {}
    out: dict = {}
    # drama=青春期专属戏码(摔门/深夜彩蛋);child 后半只会闹别扭被晾,不摔门
    for key, fn, drama in (("quiet", _quiet_annoyance, False),
                           ("door_slam", _door_slam, True),
                           ("night_egg", _night_egg, True)):
        if drama and stage not in cfg.ANNOY_DRAMA_STAGES:
            continue
        try:
            if fn(conn, child_id, t, stage):
                out[key] = True
        except Exception:
            continue
    return out
