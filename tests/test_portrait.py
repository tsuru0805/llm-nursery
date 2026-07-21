# -*- coding: utf-8 -*-
"""成长画像(纯派生读口)测试。"""
import json

import pytest

from nursery import child as child_mod
from nursery import chunks as chunks_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import driver
from nursery import portrait

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _legacy_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "portrait.db"))
    yield c
    c.close()


@pytest.fixture()
def lived(conn):
    """养了几天的孩子:语料+动作+夜哭+词块+里程碑都有一点。"""
    cid = child_mod.create_child(conn, "papa", name="囡", seed=42, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "抱抱,抱抱,要抱抱", actor="papa",
                          idempotency_key="c1", now=T0 + 60)
    child_mod.feed_corpus(conn, brain, cid, "妈妈说晚安", actor="mama",
                          speaker="mama", action_kind="mama_say",
                          idempotency_key="c2", now=T0 + 120)
    child_mod.apply_action(conn, cid, "mama", "mama_hug", idempotency_key="a1",
                           now=T0 + 180)
    # 一个已响应的主哭夜
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (cid, T0 + 300, T0 + 7200, json.dumps({"date": "2027-01-15"}), "n1"))
    child_mod.apply_action(conn, cid, "papa", "soothe", idempotency_key="a2",
                           now=T0 + 400)
    # 一个没人响应的主哭夜(已过期)
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (cid, T0 + 86400, T0 + 86400 + 3600, json.dumps({"date": "2027-01-16"}),
         "n2"))
    chunks_mod.rebuild_index(conn, cid, now=T0 + 500)
    conn.execute(
        "INSERT INTO growth_album(child_id, item_kind, title, note, created_at)"
        " VALUES(?, 'first_papa', '第一次叫了爸爸', '「爸爸」', ?)", (cid, T0 + 600))
    return cid, brain


def test_portrait_shape_and_facts(conn, lived):
    cid, brain = lived
    p = portrait.build_portrait(conn, brain, cid, now=T0 + 2 * DAY)
    assert p["name"] == "囡" and p["stage"] == "infant"
    assert set(p["axes"]) == set(cfg.PSYCHE_AXES)
    assert p["axes"]["anxiety"]["cn"] == "不安"
    assert set(p["bond"]["values"]) == {"papa", "mama"}
    assert p["bond"]["values"]["mama"]["attachment"] > cfg.BOND_BASELINE["attachment"]
    assert p["night"] == {"cries": 2, "responded": 1, "response_rate": 0.5}
    assert p["corpus"]["by_speaker"].get("mama") == 1
    assert p["language"]["family_chunks"]   # 词块索引已建
    assert any(f["kind"] == "first_papa" for f in p["firsts"])
    json.dumps(p, ensure_ascii=False)   # 全量可序列化


def test_portrait_fresh_child_safe(conn):
    cid = child_mod.create_child(conn, "papa", name="乙", seed=1, now=T0)
    p = portrait.build_portrait(conn, None, cid, now=T0 + 60)
    assert p["night"]["response_rate"] is None
    assert p["language"]["vocab"] is None
    assert p["corpus"]["total_chars"] == 0
    assert p["firsts"] == []


def test_portrait_no_labels(conn, lived):
    """画像只给事实与趋势,不打人格类型标签(刀7 纪律)。"""
    cid, brain = lived
    blob = json.dumps(portrait.build_portrait(conn, brain, cid, now=T0 + DAY),
                      ensure_ascii=False)
    for banned in ("高度照顾型", "独立型", "人格类型"):
        assert banned not in blob


def test_driver_portrait_json(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    out = driver.run("papa", ["portrait"], now=T0)
    assert json.loads(out) == {"ok": False, "error": "no_child"}
    driver.init_birth("papa", "囡", now=T0)
    driver.run("papa", ["feed", "第一句话"], now=T0 + 60)
    got = json.loads(driver.run("papa", ["portrait"], now=T0 + 120))
    assert got["ok"] is True
    assert got["portrait"]["name"] == "囡"
    assert got["portrait"]["corpus"]["total_chars"] > 0


def test_night_ledger_unified_and_edges(conn):
    """逐夜账单一口径:combo 不算独立夜/窗外响应不算/
    进行中的夜不计;画像与忽视账同源同结果。"""
    from nursery import events
    cid = child_mod.create_child(conn, "papa", name="丙", seed=3, now=T0)
    # 夜1:主哭+combo 都 fired,响应在窗内 → 1 夜已响应
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " status, payload_json, idempotency_key)"
        " VALUES(?,'night_cry',NULL,?,?,'fired',?,?)",
        (cid, T0 + 100, T0 + 3600, json.dumps({"date": "d1"}), "l1"))
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " status, payload_json, idempotency_key)"
        " VALUES(?,'night_cry','combo',?,?,'fired',?,?)",
        (cid, T0 + 700, T0 + 3600, json.dumps({"date": "d1"}), "l1c"))
    child_mod.apply_action(conn, cid, "papa", "soothe", idempotency_key="lr1",
                           now=T0 + 800)
    # 夜2:主哭 fired,响应在窗外(过期后)→ 1 夜未响应
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " status, payload_json, idempotency_key)"
        " VALUES(?,'night_cry',NULL,?,?,'fired',?,?)",
        (cid, T0 + 86400, T0 + 86400 + 3600, json.dumps({"date": "d2"}), "l2"))
    child_mod.apply_action(conn, cid, "papa", "feed", idempotency_key="lr2",
                           now=T0 + 86400 + 7200)
    # 夜3:进行中(未过期)→ 不计
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " status, payload_json, idempotency_key)"
        " VALUES(?,'night_cry',NULL,?,?,'fired',?,?)",
        (cid, T0 + 2 * 86400, T0 + 2 * 86400 + 9e5, json.dumps({"date": "d3"}),
         "l3"))
    t = T0 + 2 * 86400 + 100
    ledger = events.closed_cry_nights(conn, cid, t)
    assert [(n["date"], n["responded"]) for n in ledger] == \
        [("d1", True), ("d2", False)]
    p = portrait.build_portrait(conn, None, cid, now=t)
    assert p["night"] == {"cries": 2, "responded": 1, "response_rate": 0.5}
    hits = events.check_neglect(conn, cid, now=t)
    assert hits == 1   # 只有 d2 记忽视账(d1 已响应,d3 未完结)


def test_portrait_runaway_graduated_and_readonly(conn, lived):
    cid, brain = lived
    conn.execute("UPDATE child SET status='runaway', runaway_at=? WHERE child_id=?",
                 (T0 + DAY, cid))
    before = conn.total_changes
    p = portrait.build_portrait(conn, brain, cid, now=T0 + 2 * DAY)
    assert p["status"] == "runaway"
    assert conn.total_changes == before   # 纯读:零写入
    conn.execute("UPDATE child SET status='graduated', ending='reconciled'"
                 " WHERE child_id=?", (cid,))
    p2 = portrait.build_portrait(conn, brain, cid, now=T0 + 2 * DAY)
    assert p2["status"] == "graduated" and p2["ending"] == "reconciled"


def test_portrait_not_in_papa_whitelist():
    """portrait 不在任何阶段的照护指令白名单里(围观特权,孩子面零暴露)。"""
    for stage, allowed in driver.STAGE_ACTIONS.items():
        assert "portrait" not in allowed
        assert "mama" not in allowed
