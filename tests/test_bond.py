# -*- coding: utf-8 -*-
"""双照护人关系状态(亲近/安心/踏实/委屈)测试。"""
import json

import pytest

from nursery import bond as bond_mod
from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import psyche

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _legacy_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "bond.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    cid = child_mod.create_child(conn, "papa", name="囡", seed=42, now=T0)
    return cid


def _log(conn, cid, reason=None):
    q = "SELECT caregiver, dim, delta, reason FROM caregiver_bond_log WHERE child_id=?"
    a = [cid]
    if reason:
        q += " AND reason=?"
        a.append(reason)
    return conn.execute(q + " ORDER BY id", a).fetchall()


def test_separate_accounts_papa_vs_mama(conn, born):
    child_mod.apply_action(conn, born, "papa", "play", idempotency_key="p1", now=T0 + 60)
    child_mod.apply_action(conn, born, "mama", "mama_hug", idempotency_key="m1",
                           now=T0 + 120)
    b = bond_mod.read_bond(conn, born)
    assert b["papa"]["attachment"] > cfg.BOND_BASELINE["attachment"]
    assert b["mama"]["attachment"] > cfg.BOND_BASELINE["attachment"]
    # 爸爸的 play 不动妈妈的账,反之亦然
    papa_rows = {r["caregiver"] for r in _log(conn, born, reason="play")}
    mama_rows = {r["caregiver"] for r in _log(conn, born, reason="mama_hug")}
    assert papa_rows == {"papa"} and mama_rows == {"mama"}


def test_action_record_carries_bond(conn, born):
    child_mod.apply_action(conn, born, "papa", "soothe", idempotency_key="s1",
                           now=T0 + 60)
    rec = json.loads(conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='s1'",
        (born,)).fetchone()["payload_json"])
    assert rec["bond"]["attachment"] == pytest.approx(
        cfg.BOND_RULES["soothe"]["attachment"])


def test_neglect_hits_papa_account(conn, born):
    child_mod.apply_action(conn, born, "system", "neglect", idempotency_key="ng1",
                           payload={"date": "2027-01-15"},
                           extra_effects={"darkness": 6.0}, now=T0 + 60)
    b = bond_mod.read_bond(conn, born)
    assert b["papa"]["trust"] < cfg.BOND_BASELINE["trust"]
    assert b["papa"]["resentment"] > 0
    assert b["mama"]["trust"] == cfg.BOND_BASELINE["trust"]   # 妈妈账不动


def test_night_response_bonus_once_per_night(conn, born):
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (born, T0 + 100, T0 + 7200, json.dumps({"date": "2027-01-15"}), "nc1"))
    child_mod.apply_action(conn, born, "papa", "soothe", idempotency_key="ns1",
                           now=T0 + 600)
    child_mod.apply_action(conn, born, "papa", "feed", idempotency_key="ns2",
                           now=T0 + 900)
    night_rows = _log(conn, born, reason="night_response")
    trust_bonus = [r["delta"] for r in night_rows if r["dim"] == "trust"]
    assert trust_bonus == [pytest.approx(cfg.BOND_NIGHT_RESPONSE["trust"])]  # 每夜一次
    base = [r["delta"] for r in _log(conn, born, reason="soothe")
            if r["dim"] == "trust"]
    assert base == [pytest.approx(cfg.BOND_RULES["soothe"]["trust"])]


def test_saturated_night_bonus_not_regrantable(conn, born):
    """饱和夜:加成零增量不落流水,但占位已消费——同夜掉下来也不再领。"""
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (born, T0 + 100, T0 + 7200, json.dumps({"date": "2027-01-15"}), "nc2"))
    with child_mod.tx(conn):
        bond_mod.ensure_initialized_locked(conn, born, T0 + 10)
        bond_mod._bump_locked(conn, born, "papa",
                              {"trust": 100.0, "predictability": 100.0},
                              reason="sat", source_key=None, t=T0 + 20)
    child_mod.apply_action(conn, born, "papa", "soothe", idempotency_key="sn1",
                           now=T0 + 600)   # 饱和:加成零增量,占位已消费
    assert not _log(conn, born, reason="night_response")
    conn.execute("UPDATE caregiver_bond SET value=50 WHERE child_id=?"
                 " AND caregiver='papa' AND dim='trust'", (born,))
    child_mod.apply_action(conn, born, "papa", "feed", idempotency_key="sn2",
                           now=T0 + 900)   # 同夜再响应:不再领
    assert not _log(conn, born, reason="night_response")


def test_psyche_saturated_night_marker_consumed(conn, born):
    """psyche 同款:不安已 0 时响应=占位消费,同夜不安回升也不重复领。"""
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (born, T0 + 100, T0 + 7200, json.dumps({"date": "2027-01-16"}), "nc3"))
    child_mod.apply_action(conn, born, "papa", "burp", idempotency_key="pn0",
                           now=T0 + 50)   # 窗前动作:建轴行
    conn.execute("UPDATE psyche_axis SET value=0 WHERE child_id=? AND axis='anxiety'",
                 (born,))
    child_mod.apply_action(conn, born, "papa", "soothe", idempotency_key="pn1",
                           now=T0 + 600)   # 不安=0:加成零增量,占位已消费
    conn.execute("UPDATE psyche_axis SET value=50 WHERE child_id=? AND axis='anxiety'",
                 (born,))
    child_mod.apply_action(conn, born, "papa", "feed", idempotency_key="pn2",
                           now=T0 + 900)   # 同夜再响应:不重复领
    bonus = conn.execute(
        "SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
        " AND reason='night_cry_responded'", (born,)).fetchone()[0]
    assert bonus == 0


def test_saturation_logs_real_delta(conn, born):
    """顶格夹取后流水记实际增量;满格再涨=不落行,趋势不虚报。"""
    with child_mod.tx(conn):
        bond_mod.ensure_initialized_locked(conn, born, T0 + 10)
        bond_mod._bump_locked(conn, born, "papa", {"resentment": 99.0},
                              reason="t", source_key=None, t=T0 + 20)
    with child_mod.tx(conn):
        got = bond_mod._bump_locked(conn, born, "papa", {"resentment": 5.0},
                                    reason="t2", source_key=None, t=T0 + 30)
    assert got["resentment"] == pytest.approx(1.0)   # 99→100 实际只涨 1
    with child_mod.tx(conn):
        got2 = bond_mod._bump_locked(conn, born, "papa", {"resentment": 5.0},
                                     reason="t3", source_key=None, t=T0 + 40)
    assert got2 == {}   # 满格=零增量不落行
    assert not _log(conn, born, reason="t3")


def test_calm_soothe_extra_attachment(conn, born, monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", 0.0)
    conn.execute("UPDATE child_state SET mood=70, last_settled_at=?, updated_at=?"
                 " WHERE child_id=?", (T0 + 50, T0 + 50, born))
    child_mod.apply_action(conn, born, "papa", "soothe", idempotency_key="cs1",
                           now=T0 + 60)
    rows = _log(conn, born, reason="soothe")
    att = sum(r["delta"] for r in rows if r["dim"] == "attachment")
    assert att == pytest.approx(cfg.BOND_RULES["soothe"]["attachment"] +
                                cfg.BOND_CALM_SOOTHE["attachment"])


def test_repeat_decay_scales_bond(conn, born, monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", 0.0)
    for i in range(2):
        child_mod.apply_action(conn, born, "papa", "play",
                               idempotency_key=f"pd{i}", now=T0 + 100 + i * 60)
    rows = [r for r in _log(conn, born, reason="play") if r["dim"] == "attachment"]
    assert rows[0]["delta"] == pytest.approx(cfg.BOND_RULES["play"]["attachment"])
    assert rows[1]["delta"] == pytest.approx(
        cfg.BOND_RULES["play"]["attachment"] * cfg.DAILY_DECAY)


def test_init_from_history_estimation(conn, born):
    """已有既往账的孩子首次触达=半额估底+meta 标注,幂等一次。"""
    # 先造既往:直接插 action_log(绕过 bond 钩=模拟刀20 上线前的历史)
    for i, kind in enumerate(("feed", "soothe", "play")):
        conn.execute(
            "INSERT INTO action_log(child_id, actor, kind, payload_json,"
            " effective_at, created_at, idempotency_key, state_version_before,"
            " state_version_after) VALUES(?,?,?,?,?,?,?,0,0)",
            (born, "papa", kind, "{}", T0 + i, T0 + i, f"h{i}"))
    with child_mod.tx(conn):
        assert bond_mod.ensure_initialized_locked(conn, born, T0 + 100) is True
    b = bond_mod.read_bond(conn, born)
    expect = cfg.BOND_BASELINE["attachment"] + cfg.BOND_INIT_FACTOR * (
        cfg.BOND_RULES["feed"]["attachment"] +
        cfg.BOND_RULES["soothe"]["attachment"] +
        cfg.BOND_RULES["play"]["attachment"])
    assert b["papa"]["attachment"] == pytest.approx(expect)
    meta = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM parenting_meta WHERE child_id=?", (born,))}
    assert meta["bond_initialized_from_history"] == "true"
    assert meta["bond_confidence"] == "low"
    with child_mod.tx(conn):
        assert bond_mod.ensure_initialized_locked(conn, born, T0 + 200) is False


def test_trends_and_cn_lines(conn, born):
    for i in range(4):
        child_mod.apply_action(conn, born, "papa", "play",
                               idempotency_key=f"t{i}", now=T0 + 100 + i * 60)
    tr = bond_mod.bond_trends(conn, born, T0 + 1000)
    assert tr["papa"]["attachment"] == "rising"
    assert tr["mama"]["attachment"] == "flat"   # init 估底行不算趋势
    lines = bond_mod.trend_lines_cn(conn, born, T0 + 1000)
    assert any("对爸爸" in ln and "亲近在上升" in ln for ln in lines)
    assert any("对妈妈" in ln and "都平稳" in ln for ln in lines)


def test_psyche_input_carries_bond_lines(conn, born):
    child_mod.apply_action(conn, born, "mama", "mama_hug", idempotency_key="ph1",
                           now=T0 + 60)
    for i in range(3):
        child_mod.apply_action(conn, born, "mama", "mama_say",
                               idempotency_key=f"ps{i}", now=T0 + 120 + i * 30)
    child_row = child_mod.get_child(conn, born)
    digest, _ids, prompt = psyche.build_input(conn, born, child_row, "toddler",
                                              T0 + 1000)
    assert digest["bond_trends"]["mama"]["attachment"] == "rising"
    assert any("对妈妈" in ln for ln in digest["bond_lines"])
    assert digest["input_rev"] == 2
    assert "对妈妈" in prompt


def test_unknown_actor_zero_write(conn, born):
    child_mod.apply_action(conn, born, "system", "homecoming",
                           idempotency_key="uk1",
                           extra_effects={"mood": 1.0}, now=T0 + 60)
    # actor=system 且非 neglect ⇒ 关系账零写入
    assert all(r["reason"] == "init_from_history" for r in _log(conn, born)) or \
        not _log(conn, born)


def test_custom_persona_accrues_to_papa(conn, born):
    """NURSERY_PLAYERS 自定义 persona:关系账记主账 papa,不失账(评审回归)。"""
    child_mod.apply_action(conn, born, "grandma", "play", idempotency_key="cp1",
                           now=T0 + 60)
    b = bond_mod.read_bond(conn, born)
    assert b["papa"]["attachment"] > cfg.BOND_BASELINE["attachment"]
    rows = _log(conn, born, reason="play")
    assert {r["caregiver"] for r in rows} == {"papa"}


def test_db_v7_migrates_to_v8(tmp_path):
    import sqlite3 as _sq
    p = str(tmp_path / "v7.db")
    v7 = pdb._SCHEMA.split("CREATE TABLE IF NOT EXISTS caregiver_bond")[0]
    assert "caregiver_bond" not in v7
    raw = _sq.connect(p)
    raw.executescript(v7)
    raw.execute("PRAGMA user_version=7")
    raw.commit()
    raw.close()
    c = pdb.connect(p)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"caregiver_bond", "caregiver_bond_log"} <= tables
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    c.close()
    pdb.connect(p).close()
