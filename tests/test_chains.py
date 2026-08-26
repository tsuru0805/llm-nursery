# -*- coding: utf-8 -*-
"""v0.3:连续剧事件链。全部临时 db,假时钟注入。"""
import json

import pytest

from nursery import chains
from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb

T0 = 1_800_000_000.0
DAY = 86400.0
NOW = T0 + 5 * DAY  # 幼儿期当中

CORPUS = """他抱着积木过来找爸爸,爸爸看看这个好不好。
妈妈说恐龙是很久很久以前的动物。
今天在外面看到了一只很大的狗狗。"""


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture(autouse=True)
def _friend_only(monkeypatch):
    """钉死:只留 friend 模板+必中签,免概率抖动。"""
    monkeypatch.setattr(cfg, "CHAIN_TEMPLATES",
                        {"friend": cfg.CHAIN_TEMPLATES["friend"]})
    monkeypatch.setattr(cfg, "CHAIN_DAY_P", 1.0)


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_chains.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


def _rows(conn, cid):
    return conn.execute("SELECT * FROM scheduled_event WHERE child_id=?"
                        " AND kind='chain' ORDER BY due_at", (cid,)).fetchall()


def _events_out(conn):
    return conn.execute("SELECT * FROM outbox WHERE kind='nursery.event'"
                        " ORDER BY id").fetchall()


MORNING = chains._local_midnight(NOW) + 8 * 3600   # 当日 08:00(窗前)


def _plan(conn, cid, now=MORNING):
    return chains.plan_chains(conn, cid, now=now)


# ── 排班 ──

def test_plan_schedules_whole_season_idempotent(kid, conn):
    cid, _ = kid
    assert _plan(conn, cid) == 3
    assert _plan(conn, cid) == 0                       # 幂等
    assert _plan(conn, cid, now=MORNING + DAY) == 0    # 一生一次,换天不重开
    rows = _rows(conn, cid)
    assert len(rows) == 3
    mid = chains._local_midnight(MORNING)
    for i, r in enumerate(rows, 1):
        assert r["chain_id"] == "arc:friend"
        assert r["idempotency_key"] == f"arc:friend:ep{i}"
        day0 = mid + (i - 1) * DAY
        assert day0 + cfg.CHAIN_HOURS[0] * 3600 <= r["due_at"] <= \
            day0 + cfg.CHAIN_HOURS[1] * 3600
        assert r["expires_at"] == pytest.approx(
            r["due_at"] + cfg.CHAIN_EP_GRACE_H * 3600)
        assert json.loads(r["payload_json"])["ep"] == i


def test_plan_late_evening_starts_tomorrow(kid, conn):
    """首集时刻已过=顺延明晚开播,不播断头首集。"""
    cid, _ = kid
    late = chains._local_midnight(NOW) + 22 * 3600    # 22:00,窗(18-21)已过
    assert chains.plan_chains(conn, cid, now=late) == 3
    first = _rows(conn, cid)[0]
    assert first["due_at"] > late
    assert first["due_at"] >= chains._local_midnight(NOW) + DAY + \
        cfg.CHAIN_HOURS[0] * 3600


def test_plan_gates(kid, conn):
    cid, _ = kid
    conn.execute("UPDATE child SET status='runaway'")
    conn.commit()
    assert _plan(conn, cid) == 0
    conn.execute("UPDATE child SET status='active'")
    conn.commit()
    # 阶段门:friend 只到 child;teen(T0+30d)不开这条
    assert chains.plan_chains(conn, cid, now=T0 + 30 * DAY) == 0


# ── 逐集触发+分支 ──

def _fire_ep(conn, brain, cid, i, offset=60.0):
    rows = _rows(conn, cid)
    due = rows[i - 1]["due_at"]
    fired = chains.fire_due_chain_eps(conn, brain, cid, now=due + offset)
    return fired, due


def test_episodes_fire_in_order_good_branch(kid, conn):
    cid, brain = kid
    _plan(conn, cid)
    fired, due1 = _fire_ep(conn, brain, cid, 1)
    assert len(fired) == 1 and "朋友" in fired[0]["title"]
    assert "孩子" in fired[0]["title"]
    # 重复 tick 不重发
    assert chains.fire_due_chain_eps(conn, brain, cid, now=due1 + 120) == []
    assert len(_events_out(conn)) == 1
    fired2, due2 = _fire_ep(conn, brain, cid, 2)
    assert len(fired2) == 1 and "吵架" in fired2[0]["title"]
    # 介入:ep2 真 fire 后窗内谈心(asks.settle 同款口径)
    child_mod.apply_action(conn, cid, "papa", "talk", idempotency_key="iv1",
                          now=due2 + 3600)
    fired3, _ = _fire_ep(conn, brain, cid, 3)
    assert len(fired3) == 1 and fired3[0]["branch"] == "good"
    assert "和好" in fired3[0]["title"]
    # 真后果:好分支记在真实介入人头上(psyche+bond+state)
    row = conn.execute("SELECT actor, payload_json FROM action_log WHERE"
                       " kind='arc_friend_good'").fetchone()
    assert row["actor"] == "papa"
    assert json.loads(row["payload_json"])["user_payload"]["branch"] == "good"
    assert conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE"
                        " reason='arc_friend_good'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='papa' AND reason='arc_friend_good'"
                        ).fetchone()[0] > 0
    # 末集进成长相册
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='arc_friend'").fetchone()[0] == 1
    assert all(r["status"] == "fired" for r in _rows(conn, cid))
    # 末集重放不双记(outbox 幂等键+动作幂等键)
    assert chains.fire_due_chain_eps(conn, brain, cid,
                                     now=_rows(conn, cid)[2]["due_at"] + 240) == []
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE"
                        " kind='arc_friend_good'").fetchone()[0] == 1


def test_no_intervention_bad_branch_zero_bond(kid, conn):
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    _fire_ep(conn, brain, cid, 2)
    fired3, _ = _fire_ep(conn, brain, cid, 3)
    assert fired3[0]["branch"] == "bad"
    assert "记仇" in fired3[0]["title"] or "不跟" in fired3[0]["title"]
    row = conn.execute("SELECT actor FROM action_log WHERE"
                       " kind='arc_friend_bad'").fetchone()
    assert row["actor"] == "system"
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " reason='arc_friend_bad'").fetchone()[0] == 0
    axes = {r["axis"]: r["delta"] for r in conn.execute(
        "SELECT axis, delta FROM psyche_axis_log WHERE reason='arc_friend_bad'")}
    assert axes["anxiety"] > 0 and axes["independence"] > 0


def test_intervention_outside_window_is_bad(kid, conn):
    """窗外的动作不算介入(fired_at 起算 CHAIN_INTERVENE_WINDOW_H)。"""
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    _, due2 = _fire_ep(conn, brain, cid, 2)
    child_mod.apply_action(
        conn, cid, "papa", "talk", idempotency_key="late1",
        now=due2 + 60 + cfg.CHAIN_INTERVENE_WINDOW_H * 3600 + 600)
    fired3, _ = _fire_ep(conn, brain, cid, 3, offset=7 * 3600)
    assert fired3[0]["branch"] == "bad"


def test_mama_intervention_counts(kid, conn):
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    _, due2 = _fire_ep(conn, brain, cid, 2)
    child_mod.apply_action(conn, cid, "mama", "mama_say", idempotency_key="iv2",
                          now=due2 + 1800)
    fired3, _ = _fire_ep(conn, brain, cid, 3)
    assert fired3[0]["branch"] == "good"
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='mama' AND reason='arc_friend_good'"
                        ).fetchone()[0] > 0


def test_broken_serial_aborts_whole_chain(kid, conn):
    """断更(某集过宽限没播出)=整条剧废弃,绝不播断头剧。"""
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    rows = _rows(conn, cid)
    late = rows[1]["expires_at"] + 3600   # 引擎停摆:ep2 过宽限才回来
    assert chains.fire_due_chain_eps(conn, brain, cid, now=late) == []
    rows = _rows(conn, cid)
    assert rows[1]["status"] == "expired"
    assert rows[2]["status"] == "expired"     # 未播集连坐作废
    # 之后怎么 tick 都不再有这条剧的动静
    assert chains.fire_due_chain_eps(conn, brain, cid, now=late + DAY) == []
    assert len(_events_out(conn)) == 1        # 只有 ep1


def test_ep2_never_fires_before_ep1(kid, conn):
    """集序闸:上一集没播,下一集不播(哪怕行被人为搅乱)。"""
    cid, brain = kid
    _plan(conn, cid)
    rows = _rows(conn, cid)
    # 人为把 ep1 卡成 pending 且 ep2 已到点(构造:ep1 due 挪后)
    conn.execute("UPDATE scheduled_event SET due_at=? WHERE idempotency_key=?",
                 (rows[1]["due_at"] + 7200, "arc:friend:ep1"))
    conn.commit()
    fired = chains.fire_due_chain_eps(conn, brain, cid,
                                      now=rows[1]["due_at"] + 60)
    assert fired == []   # ep2 等 ep1,本拍不播
    assert _rows(conn, cid)[0]["status"] == "pending"


def test_system_action_never_flips_branch(kid, conn):
    """分支判定=「照护人是否介入」:actor=system 的动作不算转折点
    (引擎自动作拍不了这条剧的结局)。"""
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    _, due2 = _fire_ep(conn, brain, cid, 2)
    child_mod.apply_action(conn, cid, "system", "talk", idempotency_key="unc1",
                          now=due2 + 1800)
    fired3, _ = _fire_ep(conn, brain, cid, 3)
    assert fired3[0]["branch"] == "bad"
    assert conn.execute("SELECT actor FROM action_log WHERE"
                        " kind='arc_friend_bad'").fetchone()["actor"] == "system"


def test_late_ep2_final_waits_for_window(kid, conn):
    """上一集播晚了(仍在宽限内):介入窗未关时末集不抢答 bad,等窗关再判
    (评审定案);窗内父母来了照样 good。"""
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    rows = _rows(conn, cid)
    due2 = rows[1]["due_at"]
    late_fire = due2 + 9 * 3600          # ep2 迟播 9h(宽限 27h 内)
    chains.fire_due_chain_eps(conn, brain, cid, now=late_fire)
    assert _rows(conn, cid)[1]["status"] == "fired"
    window_end = late_fire + cfg.CHAIN_INTERVENE_WINDOW_H * 3600
    due3 = rows[2]["due_at"]
    assert due3 + 60 < window_end        # 构造成立:ep3 到点时窗还开着
    # 窗未关:ep3 到点也不播不判
    assert chains.fire_due_chain_eps(conn, brain, cid, now=due3 + 60) == []
    assert _rows(conn, cid)[2]["status"] == "pending"
    # 窗关后才判;全程无介入=bad(且没被当断更作废)
    assert window_end + 60 < rows[2]["expires_at"]   # 等窗不会等成断更
    fired3 = chains.fire_due_chain_eps(conn, brain, cid, now=window_end + 60)
    assert len(fired3) == 1 and fired3[0]["branch"] == "bad"


def test_late_ep2_intervention_in_open_window_is_good(kid, conn):
    cid, brain = kid
    _plan(conn, cid)
    _fire_ep(conn, brain, cid, 1)
    rows = _rows(conn, cid)
    due2 = rows[1]["due_at"]
    late_fire = due2 + 9 * 3600
    chains.fire_due_chain_eps(conn, brain, cid, now=late_fire)
    due3 = rows[2]["due_at"]
    # ep3 原定时刻之后、窗关之前,妈妈来了
    child_mod.apply_action(conn, cid, "mama", "mama_say", idempotency_key="lv1",
                          now=due3 + 3600)
    fired3 = chains.fire_due_chain_eps(conn, brain, cid, now=due3 + 7200)
    assert len(fired3) == 1 and fired3[0]["branch"] == "good"


# ── 调度整合 ──

def test_tick_one_carries_choices_and_chains(tmp_path, monkeypatch):
    """tick_one 一拍带上 choices/chains 计数(全幂等,不炸整拍)。"""
    from nursery import scheduler
    saves = tmp_path / "saves"
    (saves / "papa").mkdir(parents=True)
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(saves))
    db_path = str(saves / "papa" / "nursery.db")
    conn = pdb.connect(db_path)
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    conn.close()
    out = scheduler.tick_one(db_path, "papa", now=MORNING)
    assert "choices" in out and "chains" in out
    assert out["chains"]["planned"] == 3
    assert set(out["choices"]) >= {"planned", "fired", "auto", "miss"}
