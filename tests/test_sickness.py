# -*- coding: utf-8 -*-
"""v0.3:生病 arc(M7)。全部临时 db,假时钟注入。

抽签概率钉 1.0 免抖动;onset/夜叫时刻是 (child,date) 种子内的均匀抽签,
断言只钉窗口边界(08-20 / 03-06)不钉具体时刻。
"""
import json
import time

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import events, sickness

DAY = 86400.0


def _jst(date_str: str, hh: int = 12, mm: int = 0) -> float:
    return time.mktime(time.strptime(date_str, "%Y-%m-%d")) + hh * 3600 + mm * 60


T0 = _jst("2026-07-17")

CORPUS = """睡吧睡吧,爸爸在这里陪着你呢,不怕不怕。
喝完奶奶就不难受了,乖乖的,妈妈也在。
外面下雨了,我们在家里,家里暖暖的。"""


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_sick.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    """幼儿期的孩子+已喂语料的大脑。"""
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


@pytest.fixture()
def sick_kid(conn, kid, monkeypatch):
    """第 5 天注定生病的孩子:病窗已排好(sickness+sick_cry 两件)。"""
    monkeypatch.setattr(sickness, "SICKNESS_DAY_P", 1.0)
    cid, brain = kid
    n = sickness.plan_sickness(conn, cid, now=T0 + 5 * DAY)   # 7-22 正午
    assert n == 2
    return cid, brain


def _sched(conn, cid, kind):
    return conn.execute("SELECT * FROM scheduled_event WHERE child_id=?"
                        " AND kind=? ORDER BY due_at", (cid, kind)).fetchall()


def _outbox(conn, kind):
    return [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT payload_json FROM outbox WHERE kind=? ORDER BY id", (kind,))]


def _sick_events(conn, event):
    return [p for p in _outbox(conn, "nursery.event") if p.get("event") == event]


# ── 排窗 ──

def test_plan_windows_and_idempotent(conn, sick_kid):
    cid, _ = sick_kid
    midnight = _jst("2026-07-22", 0)
    (sick,) = _sched(conn, cid, "sickness")
    assert midnight + 8 * 3600 <= sick["due_at"] <= midnight + 20 * 3600
    assert sick["expires_at"] == pytest.approx(sick["due_at"] + 48 * 3600)
    assert sick["chain_id"] == "sick:2026-07-22"   # 不撞 night_cry 的 NULL/combo 语义
    (cry,) = _sched(conn, cid, "sick_cry")
    night = midnight + DAY
    assert night + 3 * 3600 <= cry["due_at"] <= night + 6 * 3600   # 03:00-06:00 放宽窗
    assert cry["expires_at"] == night + 7 * 3600                   # 当日 07:00 过期即弃
    # 重排幂等
    assert sickness.plan_sickness(conn, cid, now=T0 + 5 * DAY + 3600) == 0


def test_plan_min_gap(conn, sick_kid):
    """刚病过=最小间隔内绝不再抽(p=1.0 也压不过间隔闸)。"""
    cid, _ = sick_kid
    assert sickness.plan_sickness(conn, cid, now=T0 + 6 * DAY) == 0
    assert sickness.plan_sickness(conn, cid, now=T0 + 8 * DAY) == 0
    assert len(_sched(conn, cid, "sickness")) == 1


def test_plan_deterministic_low_freq(conn, kid):
    """默认概率:同日重抽结果一致;间隔闸+抽签叠加=10-14 天量级。"""
    cid, _ = kid
    for i in range(3):
        t = T0 + 5 * DAY + i * 600
        a = sickness.plan_sickness(conn, cid, now=t)
        assert a == sickness.plan_sickness(conn, cid, now=t) or a > 0  # 幂等/确定


# ── 开窗/夜叫/痊愈 ──

def test_onset_fires_event_and_opens_window(conn, sick_kid):
    cid, brain = sick_kid
    t = _jst("2026-07-22", 23)   # 必在 onset(08-20)之后、窗内
    fired = sickness.fire_due_sickness(conn, brain, cid, now=t)
    assert any(p.get("event") == "sick_onset" for p in fired)
    evs = _sick_events(conn, "sick_onset")
    assert len(evs) == 1 and "孩子" in evs[0]["title"]
    assert sickness.open_sickness_date(conn, cid, t) == "2026-07-22"
    # 幂等:重扫不重发
    assert sickness.fire_due_sickness(conn, brain, cid, now=t + 60) == []
    # 窗外=没病
    assert sickness.open_sickness_date(conn, cid, t - DAY) is None


def test_sick_cry_fires_in_small_hours(conn, sick_kid):
    """病窗次日凌晨真叫人(幼儿期也叫=设计点),夜哭同形 payload。"""
    cid, brain = sick_kid
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    fired = sickness.fire_due_sickness(conn, brain, cid,
                                       now=_jst("2026-07-23", 6, 59))
    crys = [p for p in fired if p.get("kind") == "nursery.cry"]
    assert len(crys) == 1
    p = crys[0]
    assert p["detail"] == "sick" and p["chain"] == "sick:2026-07-22"
    assert p["voice"]   # 真实声音或兜底哼唧,绝不空着
    assert _outbox(conn, "nursery.cry")


def test_sick_cry_expires_not_replayed(conn, sick_kid):
    """07:00 过了没叫出去=过期即弃(夜里的难受不上午补播)。"""
    cid, brain = sick_kid
    fired = sickness.fire_due_sickness(conn, brain, cid,
                                       now=_jst("2026-07-23", 9))
    assert all(p.get("kind") != "nursery.cry" for p in fired)
    (cry,) = _sched(conn, cid, "sick_cry")
    assert cry["status"] == "expired"
    assert _outbox(conn, "nursery.cry") == []


def test_heal_on_window_close(conn, sick_kid):
    cid, brain = sick_kid
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    t_after = _jst("2026-07-25", 12)   # onset+48h 必已过
    assert sickness.settle_sickness(conn, cid, now=t_after) == 1
    evs = _sick_events(conn, "sick_recovered")
    assert len(evs) == 1 and "孩子" in evs[0]["title"]
    (sick,) = _sched(conn, cid, "sickness")
    assert sick["status"] == "settled"
    assert sickness.open_sickness_date(conn, cid, t_after) is None
    # 幂等
    assert sickness.settle_sickness(conn, cid, now=t_after + 60) == 0


def test_never_fired_window_expires_silently(conn, sick_kid):
    """调度停摆整个病程=过期作废,不补发「他病了」也不发痊愈。"""
    cid, brain = sick_kid
    fired = sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-26", 12))
    assert fired == []
    (sick,) = _sched(conn, cid, "sickness")
    assert sick["status"] == "expired"
    assert sickness.settle_sickness(conn, cid, now=_jst("2026-07-26", 13)) == 0
    assert _sick_events(conn, "sick_onset") == []
    assert _sick_events(conn, "sick_recovered") == []


# ── 病中效果 ──

def test_speak_perturbed_only_in_window(conn, sick_kid):
    cid, brain = sick_kid
    t_in = _jst("2026-07-23", 12)
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    child_mod.child_speak(conn, brain, cid, trigger="manual", now=t_in)
    p_in = json.loads(conn.execute(
        "SELECT generation_params_json FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()[0])
    assert p_in.get("sick") is True   # 温度升/句长缩/叠词回升已进 params 留痕
    t_out = _jst("2026-07-26", 12)
    sickness.settle_sickness(conn, cid, now=t_out)
    child_mod.child_speak(conn, brain, cid, trigger="manual", now=t_out + 60)
    p_out = json.loads(conn.execute(
        "SELECT generation_params_json FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()[0])
    assert "sick" not in p_out


def test_sick_care_bonus_once_per_day_per_kind(conn, sick_kid):
    cid, brain = sick_kid
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    t = _jst("2026-07-23", 12)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s1", now=t)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s2", now=t + 600)   # 同日第二次
    child_mod.apply_action(conn, cid, "papa", "feed",
                           idempotency_key="f1",
                           extra_effects={"nutrition": 5.0}, now=t + 1200)
    rows = conn.execute(
        "SELECT source_key FROM psyche_axis_log WHERE child_id=?"
        " AND reason='sick_care' ORDER BY id", (cid,)).fetchall()
    keys = {r["source_key"] for r in rows}
    assert keys == {"sickcare:2026-07-23:soothe", "sickcare:2026-07-23:feed"}
    # 次日窗内同类可再领一次(每病日每类一次)。时刻钉 07:00:onset 抽签在
    # 08-20 之间,expires=onset+48h ≥ 7-24 08:00,07:00 必仍在窗内(不赌抽签)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s3", now=_jst("2026-07-24", 7))
    keys2 = {r["source_key"] for r in conn.execute(
        "SELECT source_key FROM psyche_axis_log WHERE child_id=?"
        " AND reason='sick_care'", (cid,))}
    assert "sickcare:2026-07-24:soothe" in keys2


def test_no_sick_care_outside_window(conn, kid):
    cid, _ = kid
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s0", now=T0 + 3 * DAY)
    assert conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
                        " AND reason='sick_care'", (cid,)).fetchone()[0] == 0


# ── 账目隔离:不撞夜哭族 ──

def test_sick_rows_never_pollute_cry_accounting(conn, sick_kid):
    """sick_cry/sickness 绝不进 closed_cry_nights(忽视账/画像口径不受扰),
    也绝不进结局响应率**分母**(judge_ending 只数 kind='night_cry' 的 fired)。
    分子(全时段 feed/soothe/diaper 计数)=v1 粗口径本来的样子:病中照顾是
    真实照顾动作,照常计入;升精确口径=改判分语义,暂维持现状。"""
    cid, brain = sick_kid
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-23", 6, 59))
    assert events.closed_cry_nights(conn, cid, _jst("2026-07-30", 12)) == []
    assert events.check_neglect(conn, cid, now=_jst("2026-07-30", 12)) == 0
    # judge_ending 分母同款查询:sick 族 fired 不算主哭夜
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_event WHERE child_id=? AND status='fired'"
        " AND kind='night_cry'", (cid,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_event WHERE child_id=? AND status='fired'"
        " AND kind IN ('sickness','sick_cry')", (cid,)).fetchone()[0] == 2


# ── tick 面:fail-open ──

def test_tick_sickness_runs_and_fail_open(conn, sick_kid, monkeypatch):
    cid, brain = sick_kid
    out = sickness.tick_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    assert out.get("fired", 0) >= 1
    # 单段炸了不上抛
    monkeypatch.setattr(sickness, "plan_sickness",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    out2 = sickness.tick_sickness(conn, brain, cid, now=_jst("2026-07-23", 12))
    assert isinstance(out2, dict)


def test_sick_care_counts_mama_actions(conn, sick_kid):
    """0825:妈妈的照顾(mama_hug/mama_soothe/mama_touch)病窗内同吃 sick_care
    加成,每病日每类一次(与 feed/soothe 同形制)。"""
    cid, brain = sick_kid
    sickness.fire_due_sickness(conn, brain, cid, now=_jst("2026-07-22", 23))
    t = _jst("2026-07-23", 12)
    child_mod.apply_action(conn, cid, "mama", "mama_hug",
                           idempotency_key="mh1", now=t)
    child_mod.apply_action(conn, cid, "mama", "mama_hug",
                           idempotency_key="mh2", now=t + 600)   # 同日第二次不重领
    child_mod.apply_action(conn, cid, "mama", "mama_touch",
                           idempotency_key="mt1", now=t + 1200)
    keys = {r["source_key"] for r in conn.execute(
        "SELECT source_key FROM psyche_axis_log WHERE child_id=?"
        " AND reason='sick_care'", (cid,))}
    assert keys == {"sickcare:2026-07-23:mama_hug",
                    "sickcare:2026-07-23:mama_touch"}
