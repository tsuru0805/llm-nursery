# -*- coding: utf-8 -*-
"""v0.3:需求事件 ask「他来找你」。全部临时 db,假时钟注入。"""
import json

import pytest

from nursery import asks
from nursery import child as child_mod
from nursery import db as pdb
from nursery import scheduler

T0 = 1_800_000_000.0
DAY = 86400.0

CORPUS = """他抱着积木过来找爸爸,爸爸看看这个好不好。
妈妈说恐龙是很久很久以前的动物。
今天在外面看到了一只很大的狗狗。
把果果分给妈妈一半,分享是好孩子。
不怕不怕,爸爸在,妈妈也在。"""


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    """钉 v1 状态规则(同 test_nursery.py 口径):本文件只测 ask 机制本身。"""
    from nursery import config as _cfg
    monkeypatch.setattr(_cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_asks.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    """幼儿期(T0+5天)的孩子+已喂语料的大脑。"""
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


def _patch_plan(monkeypatch, **kw):
    """钉死抽签参数免概率抖动。默认:必发/两条/2h 窗/全找爸爸。"""
    plan = dict(day_p=1.0, n=(2, 2), window_h=2.0, mama_p=0.0)
    plan.update(kw)
    monkeypatch.setattr(asks, "ASK_STAGE_PLAN", {"toddler": plan})


NOW = T0 + 5 * DAY  # 幼儿期当中


def _rows(conn, cid):
    return conn.execute("SELECT * FROM scheduled_event WHERE child_id=?"
                        " AND kind='ask' ORDER BY due_at", (cid,)).fetchall()


# ── 排班 ──

def test_plan_deterministic_and_idempotent(kid, conn, monkeypatch):
    cid, _ = kid
    _patch_plan(monkeypatch)
    assert asks.plan_asks(conn, cid, now=NOW) == 2
    assert asks.plan_asks(conn, cid, now=NOW) == 0          # 幂等
    assert asks.plan_asks(conn, cid, now=NOW + 3600) == 0   # 同日再拍同班表
    rows = _rows(conn, cid)
    assert len(rows) == 2
    for r in rows:
        assert r["expires_at"] == pytest.approx(r["due_at"] + 2 * 3600)
        assert json.loads(r["payload_json"])["target"] == "papa"


def test_plan_skips_infant_and_inactive(kid, conn, monkeypatch):
    cid, _ = kid
    assert asks.plan_asks(conn, cid, now=T0 + DAY) == 0     # 婴儿期无 ask(真配置)
    _patch_plan(monkeypatch)
    conn.execute("UPDATE child SET status='runaway'")
    conn.commit()
    assert asks.plan_asks(conn, cid, now=NOW) == 0          # 出走不来找你


# ── 触发 ──

def test_fire_emits_outbox_with_real_voice(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch)
    asks.plan_asks(conn, cid, now=NOW)
    due = _rows(conn, cid)[0]["due_at"]
    fired = asks.fire_due_asks(conn, brain, cid, now=due + 60)
    assert len(fired) >= 1
    row = conn.execute("SELECT * FROM outbox WHERE kind='nursery.ask'"
                       " ORDER BY id").fetchone()
    p = json.loads(row["payload_json"])
    assert p["title"] == "孩子来找你了" and p["target"] == "papa"
    assert "「" in p["text"]
    assert p["window_until"] == str(int(row["expires_at"]))  # 全 str(接入层可做纯 str 硬校验)
    # 二次触发不重复(status 已 fired+outbox 幂等键)
    assert asks.fire_due_asks(conn, brain, cid, now=due + 120) == []


def test_fire_expired_never_surfaces(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch, window_h=1.0)
    asks.plan_asks(conn, cid, now=NOW)
    last = _rows(conn, cid)[-1]
    fired = asks.fire_due_asks(conn, brain, cid, now=last["expires_at"] + 3600)
    assert fired == []
    assert all(r["status"] == "expired" for r in _rows(conn, cid))
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='nursery.ask'"
                        ).fetchone()[0] == 0
    # 过期的不进 settle 账(没真发生过,不算漏接)
    out = asks.settle_asks(conn, cid, now=last["expires_at"] + 7200)
    assert out == {"hit": 0, "miss": 0}


# ── 窗关记账 ──

def _fire_first(conn, brain, cid):
    due = _rows(conn, cid)[0]["due_at"]
    asks.fire_due_asks(conn, brain, cid, now=due + 60)
    ev = _rows(conn, cid)[0]
    assert ev["status"] == "fired"
    return ev


def test_settle_hit_credits_real_responder(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1))
    asks.plan_asks(conn, cid, now=NOW)
    ev = _fire_first(conn, brain, cid)
    child_mod.apply_action(conn, cid, "papa", "talk", idempotency_key="r1",
                          now=ev["due_at"] + 600)
    out = asks.settle_asks(conn, cid, now=ev["expires_at"] + 60)
    assert out == {"hit": 1, "miss": 0}
    row = conn.execute("SELECT actor, payload_json FROM action_log"
                       " WHERE kind='ask_answered'").fetchone()
    assert row["actor"] == "papa"
    assert json.loads(row["payload_json"])["user_payload"]["answered_by"] == "papa"
    assert _rows(conn, cid)[0]["status"] == "settled_hit"
    # psyche/bond 走规则表:被接住=不安-自尊+;对答话人(papa)更黏
    assert conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE reason="
                        "'ask_answered'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='papa' AND reason='ask_answered'"
                        ).fetchone()[0] > 0
    # 崩后重扫不双记:status 手动打回 fired,账本幂等键挡住
    conn.execute("UPDATE scheduled_event SET status='fired' WHERE id=?", (ev["id"],))
    conn.commit()
    asks.settle_asks(conn, cid, now=ev["expires_at"] + 120)
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE kind='ask_answered'"
                        ).fetchone()[0] == 1


def test_settle_miss_no_punishment(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1))
    asks.plan_asks(conn, cid, now=NOW)
    ev = _fire_first(conn, brain, cid)
    before = child_mod.read_state(conn, cid, now=ev["expires_at"] + 60, persist=False)
    out = asks.settle_asks(conn, cid, now=ev["expires_at"] + 60)
    assert out == {"hit": 0, "miss": 1}
    assert _rows(conn, cid)[0]["status"] == "settled_miss"
    # 独立微涨,零 bond 账,状态零惩罚(mood/intimacy 不因 miss 扣)
    axes = {r["axis"]: r["delta"] for r in conn.execute(
        "SELECT axis, delta FROM psyche_axis_log WHERE reason='ask_missed'")}
    assert axes.get("independence", 0) > 0 and "anxiety" not in axes
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " reason='ask_missed'").fetchone()[0] == 0
    after = child_mod.read_state(conn, cid, now=ev["expires_at"] + 61, persist=False)
    assert after["intimacy"] >= before["intimacy"] - 0.01


def test_mama_target_only_mama_verbs_count(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1), mama_p=1.0)
    asks.plan_asks(conn, cid, now=NOW)
    ev = _fire_first(conn, brain, cid)
    assert json.loads(conn.execute(
        "SELECT payload_json FROM outbox WHERE kind='nursery.ask'"
        ).fetchone()["payload_json"])["target"] == "mama"
    # 爸爸的 talk 不算接住妈妈的 ask
    child_mod.apply_action(conn, cid, "papa", "talk", idempotency_key="r2",
                          now=ev["due_at"] + 300)
    assert asks.settle_asks(conn, cid, now=ev["expires_at"] + 60) == \
        {"hit": 0, "miss": 1}


def test_mama_verb_hits_mama_target(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1), mama_p=1.0)
    asks.plan_asks(conn, cid, now=NOW)
    ev = _fire_first(conn, brain, cid)
    child_mod.apply_action(conn, cid, "mama", "mama_touch", idempotency_key="r3",
                          now=ev["due_at"] + 300)
    assert asks.settle_asks(conn, cid, now=ev["expires_at"] + 60) == \
        {"hit": 1, "miss": 0}
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='mama' AND reason='ask_answered'"
                        ).fetchone()[0] > 0


# ── 夜哭触发器不吃 ask(回归:fire_due_events 加了 kind 过滤) ──

def test_night_cry_fire_ignores_ask_rows(kid, conn, monkeypatch):
    cid, brain = kid
    _patch_plan(monkeypatch)
    asks.plan_asks(conn, cid, now=NOW)
    due = _rows(conn, cid)[0]["due_at"]
    fired = scheduler.fire_due_events(conn, brain, cid, now=due + 60)
    assert fired == []                                      # 不把 ask 当哭处理
    assert _rows(conn, cid)[0]["status"] == "pending"       # 原样留给 ask 触发器
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='nursery.cry'"
                        ).fetchone()[0] == 0


def test_settle_counts_from_actual_fire_not_due(kid, conn, monkeypatch):
    """tick 迟到时,他还没真出现前的动作不算接住(评审定案)。"""
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1), window_h=2.0)
    asks.plan_asks(conn, cid, now=NOW)
    due = _rows(conn, cid)[0]["due_at"]
    # 计划点之后、真 fire 之前有过一次 talk
    child_mod.apply_action(conn, cid, "papa", "talk", idempotency_key="early",
                          now=due + 60)
    asks.fire_due_asks(conn, brain, cid, now=due + 1800)   # tick 晚了半小时
    ev = _rows(conn, cid)[0]
    assert json.loads(ev["payload_json"])["fired_at"] == pytest.approx(due + 1800)
    assert asks.settle_asks(conn, cid, now=ev["expires_at"] + 60) == \
        {"hit": 0, "miss": 1}


def test_observer_quiet_not_suppressed_by_ask(kid, conn, monkeypatch):
    """白天 fired 的 ask 不算「哭闹」——安静观察行照发(评审定案回归)。"""
    from nursery import observer
    cid, brain = kid
    _patch_plan(monkeypatch, n=(1, 1))
    asks.plan_asks(conn, cid, now=NOW)
    due = _rows(conn, cid)[0]["due_at"]
    asks.fire_due_asks(conn, brain, cid, now=due + 60)
    day0 = asks._local_midnight(NOW)
    line = observer._obs_quiet(conn, cid, day0, day0 + 22 * 3600)
    assert line is not None and "没人理他" in line
    # 同窗有 fired 夜哭时仍压掉(原语义不变)
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, status,"
        " idempotency_key) VALUES(?,?,?,?,?)",
        (cid, "night_cry", day0 + 8 * 3600, "fired", "nc-test"))
    conn.commit()
    assert observer._obs_quiet(conn, cid, day0, day0 + 22 * 3600) is None
