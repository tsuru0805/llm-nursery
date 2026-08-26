# -*- coding: utf-8 -*-
"""v0.3:结局日交互——告别门/再等一天/亲口 farewell 才判。"""
import json

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver, events, texts

T0 = 1_800_000_000.0
DAY = 86400.0
T_GRAD = T0 + 38 * DAY   # 毕业线(36+1.5)之后


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    from nursery import config as _cfg
    monkeypatch.setattr(_cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    return tmp_path / "saves"


@pytest.fixture
def grown(saves):
    """毕业线后的成年孩子(高亲密,若自动判会落 reconciled)。"""
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    conn.execute("UPDATE child SET stage_policy_version=1")   # 断言按 v1 时间轴写
    conn.commit()
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你。",
                          now=T0 + 60)
    conn.execute("UPDATE child_state SET intimacy=85, darkness=10")
    conn.commit()
    yield conn, cid, brain
    conn.close()


def test_gate_opens_but_never_auto_judges(grown):
    conn, cid, brain = grown
    assert events.judge_ending(conn, brain, cid, now=T_GRAD) is None
    gate = conn.execute("SELECT title, note FROM growth_album WHERE"
                        " item_kind='farewell_gate'").fetchone()
    assert gate is not None and "收拾好了" in gate["title"]
    # 再过 30 天,反复 tick 也绝不开奖;门事件不重复
    for d in (1, 10, 30):
        assert events.judge_ending(conn, brain, cid, now=T_GRAD + d * DAY) is None
    child = child_mod.get_child(conn, cid)
    assert child["status"] == "active" and not child["ending"]
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='farewell_gate'").fetchone()[0] == 1


def test_stay_day_only_says_the_line(grown):
    conn, cid, brain = grown
    events.judge_ending(conn, brain, cid, now=T_GRAD)   # 开门
    child_mod.apply_action(conn, cid, "papa", "stay", idempotency_key="s1",
                          now=T_GRAD + 60)
    res = child_mod.child_speak(conn, brain, cid, trigger="talk",
                                now=T_GRAD + 3600)
    assert res.text == texts.STAY_LINE and res.accepted
    assert res.params.get("stay_day") is True
    row = conn.execute("SELECT text FROM utterance ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    assert row["text"] == texts.STAY_LINE                # 留痕照旧
    # 24h 窗过了=恢复正常生成(不是那句定稿)
    res2 = child_mod.child_speak(conn, brain, cid, trigger="talk",
                                 now=T_GRAD + 60 + 25 * 3600)
    assert res2.params.get("stay_day") is None


def test_farewell_action_unlocks_judgement(grown):
    conn, cid, brain = grown
    events.judge_ending(conn, brain, cid, now=T_GRAD)   # 开门
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="f1",
                          now=T_GRAD + 60)
    assert events.judge_ending(conn, brain, cid, now=T_GRAD + 120) == "reconciled"
    assert child_mod.get_child(conn, cid)["status"] == "graduated"


def test_driver_gate_guard_and_uncle_block(saves, grown, monkeypatch):
    conn, cid, brain = grown
    # 门没开(新档年龄不到)时 farewell 婉拒——用时间早于毕业线模拟
    out_early = driver.run("papa", ["farewell"], now=T0 + 37 * DAY)
    assert "还没到" in out_early
    events.judge_ending(conn, brain, cid, now=T_GRAD)   # 开门
    import pytest as _pt
    with _pt.raises(ValueError):   # 未登记 persona 进不了门
        driver.run("uncle", ["farewell"], now=T_GRAD + 60)
    out = driver.run("papa", ["stay"], now=T_GRAD + 120)
    assert texts.STAY_LINE in out


def test_farewell_before_gate_or_wrong_actor_invalid(grown):
    """门前的/非papa的 farewell 账不解锁判定(评审定案回归)。"""
    conn, cid, brain = grown
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="e1",
                          now=T_GRAD - DAY)          # 门还没开
    assert events.judge_ending(conn, brain, cid, now=T_GRAD) is None  # 本拍开门
    assert events.judge_ending(conn, brain, cid, now=T_GRAD + 60) is None
    child_mod.apply_action(conn, cid, "mama", "farewell", idempotency_key="e2",
                          now=T_GRAD + 120)          # 妈妈不能替他说再见
    assert events.judge_ending(conn, brain, cid, now=T_GRAD + 180) is None
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="e3",
                          now=T_GRAD + 240)
    assert events.judge_ending(conn, brain, cid, now=T_GRAD + 300) == "reconciled"


def test_stay_utterance_has_null_snapshot(grown):
    """定稿句不冒充模型输出:model_snapshot_id=NULL(评审定案回归)。"""
    conn, cid, brain = grown
    events.judge_ending(conn, brain, cid, now=T_GRAD)
    child_mod.apply_action(conn, cid, "papa", "stay", idempotency_key="s9",
                          now=T_GRAD + 60)
    child_mod.child_speak(conn, brain, cid, trigger="talk", now=T_GRAD + 120)
    row = conn.execute("SELECT model_snapshot_id, text FROM utterance"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["text"] == texts.STAY_LINE and row["model_snapshot_id"] is None


def test_stay_future_dirty_row_not_active_now(grown):
    """stay 窗带上界:未来时间戳的脏行不把「现在」拖进定稿句模式。"""
    conn, cid, brain = grown
    events.judge_ending(conn, brain, cid, now=T_GRAD)   # 开门
    child_mod.apply_action(conn, cid, "papa", "stay", idempotency_key="sfut",
                           now=T_GRAD + 10 * 3600)      # 「未来」的 stay
    res = child_mod.child_speak(conn, brain, cid, now=T_GRAD + 60)
    assert res.params.get("stay_day") is None            # 现在还没到那一天
