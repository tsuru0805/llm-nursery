# -*- coding: utf-8 -*-
"""观察日志(真实统计派生旁观行)测试。临时 db+mktime 本地时刻。"""
import json
import time

import pytest

from nursery import child as child_mod
from nursery import chunks as chunks_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import observer

T0 = 1_800_000_000.0
DAY = 86400.0


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


EVE = _local(2026, 7, 22, 21, 30)     # 观察窗内
NOON = _local(2026, 7, 22, 12)


@pytest.fixture(autouse=True)
def _legacy_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))
    monkeypatch.setattr(cfg, "OBSERVE_MAX_PER_DAY", 5)   # 单项测试不互抢预算


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "obs.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    cid = child_mod.create_child(conn, "papa", name="囡", seed=42,
                                 now=EVE - 2 * DAY)
    return cid


def _utter(conn, cid, text, t, accepted=1, reason=None):
    conn.execute(
        "INSERT INTO utterance(child_id, trigger, stage, text, max_source_overlap,"
        " accepted, rejection_reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (cid, "manual", "toddler", text, 5, accepted, reason, t))


def _act(conn, cid, t, kind="play"):
    conn.execute(
        "INSERT INTO action_log(child_id, actor, kind, payload_json, effective_at,"
        " created_at, idempotency_key, state_version_before, state_version_after)"
        " VALUES(?,?,?,?,?,?,?,0,0)",
        (cid, "papa", kind, "{}", t, t, f"a:{t}", ))


def _obs_rows(conn, cid):
    return {json.loads(r["payload_json"])["observation"]: json.loads(r["payload_json"])
            for r in conn.execute(
                "SELECT payload_json FROM outbox WHERE child_id=?"
                " AND idempotency_key LIKE 'obs:%'", (cid,))}


def test_gate_before_evening(conn, born):
    _utter(conn, born, "果果好吃", NOON - 60)
    _utter(conn, born, "要果果", NOON - 30)
    assert observer.daily_observe(conn, born, now=NOON) == []


def test_repeat_observation(conn, born):
    _utter(conn, born, "果果好吃", EVE - 3600)
    _utter(conn, born, "还要果果", EVE - 1800)
    keys = observer.daily_observe(conn, born, now=EVE)
    assert "repeat" in keys
    rows = _obs_rows(conn, born)
    assert "果果" in conn.execute(
        "SELECT payload_json FROM outbox WHERE idempotency_key=?",
        (f"obs:{time.strftime('%Y-%m-%d', time.localtime(EVE))}:repeat",)
    ).fetchone()[0]
    assert rows["repeat"]["kind"] == "nursery.event"
    # 不进相册(named=建档命名纪念,非观察行)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE child_id=?"
                        " AND item_kind!='named'", (born,)).fetchone()[0] == 0


def test_unfinished_observation(conn, born):
    _utter(conn, born, "(咿呀……)", EVE - 600, accepted=0, reason="guard_exhausted")
    assert "unfinished" in observer.daily_observe(conn, born, now=EVE)


def test_quiet_observation_and_counterexample(conn, born):
    # 白天每 3 小时有互动=不安静
    for h in (8, 11, 14, 17, 20):
        _act(conn, born, _local(2026, 7, 22, h))
    assert "quiet" not in observer.daily_observe(conn, born, now=EVE)
    # 另一个孩子:全天没人理=安静一整天
    cid2 = child_mod.create_child(conn, "papa", name="乙", seed=1, now=EVE - DAY)
    assert "quiet" in observer.daily_observe(conn, cid2, now=EVE)


def test_new_chars_observation(conn, born):
    brain = child_mod.ChildBrain.load(conn, born)
    child_mod.feed_corpus(conn, brain, born, "星星月亮太阳云朵都在天上",
                          actor="papa", idempotency_key="nc", now=EVE - 3600)
    assert "new_chars" in observer.daily_observe(conn, born, now=EVE)


def test_stale_chunk_observation(conn, born):
    brain = child_mod.ChildBrain.load(conn, born)
    old = EVE - 5 * DAY
    child_mod.feed_corpus(conn, brain, born, "抱抱,抱抱,要抱抱",
                          actor="papa", idempotency_key="st", now=old)
    chunks_mod.rebuild_index(conn, born, now=old + 60)
    for i, s in enumerate(["星星在天上", "月亮出来了", "云朵飘走了"]):
        _utter(conn, born, s, EVE - 3600 - i * 60)
    keys = observer.daily_observe(conn, born, now=EVE)
    assert "stale_chunk" in keys


def test_daily_cap_and_idempotence(conn, born, monkeypatch):
    monkeypatch.setattr(cfg, "OBSERVE_MAX_PER_DAY", 1)
    _utter(conn, born, "果果好吃", EVE - 3600)
    _utter(conn, born, "还要果果", EVE - 1800)
    _utter(conn, born, "(咿呀)", EVE - 600, accepted=0, reason="guard_exhausted")
    first = observer.daily_observe(conn, born, now=EVE)
    assert len(first) == 1
    # 同晚再拍:当日预算已满,不再发
    assert observer.daily_observe(conn, born, now=EVE + 600) == []


def test_quiet_blocked_by_fired_event(conn, born):
    """窗内有 fired 哭闹事件=「没闹」没证据,不发。"""
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,'fired','{}','q1')",
        (born, _local(2026, 7, 22, 10)))
    assert "quiet" not in observer.daily_observe(conn, born, now=EVE)


def test_empty_model_not_unfinished(conn, born):
    """空模型零重试=no_model,不冒充「试了好几次」。"""
    brain = child_mod.ChildBrain.load(conn, born)
    res = child_mod.child_speak(conn, brain, born, now=EVE - 600)
    assert not res.accepted
    reason = conn.execute(
        "SELECT rejection_reason FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (born,)).fetchone()[0]
    assert reason == "no_model"
    assert "unfinished" not in observer.daily_observe(conn, born, now=EVE)


def test_stale_window_excludes_future(conn, born):
    """未来时间戳的话不算进滚动 72h 窗:
    窗内只有未来句时活动闸不满足,不发。"""
    brain = child_mod.ChildBrain.load(conn, born)
    old = EVE - 5 * DAY
    child_mod.feed_corpus(conn, brain, born, "抱抱,抱抱,要抱抱",
                          actor="papa", idempotency_key="fw", now=old)
    chunks_mod.rebuild_index(conn, born, now=old + 60)
    for i, s in enumerate(["星星在天上", "月亮出来了", "云朵飘走了"]):
        _utter(conn, born, s, EVE + DAY + i * 60)   # 全是未来时间戳
    assert "stale_chunk" not in observer.daily_observe(conn, born, now=EVE)


def test_inactive_child_silent(conn, born):
    conn.execute("UPDATE child SET status='runaway' WHERE child_id=?", (born,))
    _utter(conn, born, "果果好吃", EVE - 3600)
    _utter(conn, born, "还要果果", EVE - 1800)
    assert observer.daily_observe(conn, born, now=EVE) == []
