# -*- coding: utf-8 -*-
"""v0.3:溯源闭环(教词点名)+ask 战报+告状/吐槽。临时 db 假时钟。"""
import json

import pytest

from nursery import asks
from nursery import child as child_mod
from nursery import db as pdb
from nursery import observer, texts

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    from nursery import config as _cfg
    monkeypatch.setattr(_cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "t23.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里。", actor="papa",
                          idempotency_key="seed", now=T0)
    return cid, brain


def _utter(conn, cid, text, t):
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " created_at) VALUES(?, 't', 'toddler', ?, 1, ?)", (cid, text, t))
    conn.commit()


# ── 溯源点名 ──

def test_taught_word_traces_to_speaker(kid, conn):
    cid, brain = kid
    day0 = observer._midnight(T0 + 6 * DAY)
    # 昨天妈妈教了带新字的话;今天他说出了「恐龙」
    child_mod.feed_corpus(conn, brain, cid, "恐龙是很久以前的动物。", actor="papa",
                          speaker="mama", idempotency_key="teach1",
                          now=day0 - 3600)
    _utter(conn, cid, "恐龙龙恐龙", day0 + 3600)
    line = observer._obs_taught_word(conn, cid, day0, day0 + 7200)
    assert line == texts.OBS_TAUGHT.format(word="恐龙", who="妈妈")


def test_taught_word_never_fabricates(kid, conn):
    cid, brain = kid
    day0 = observer._midnight(T0 + 6 * DAY)
    # 昨天教的话今天没说出来 → 不发
    child_mod.feed_corpus(conn, brain, cid, "犀牛有很大的角。", actor="papa",
                          idempotency_key="teach2", now=day0 - 3600)
    _utter(conn, cid, "睡吧睡吧", day0 + 3600)
    assert observer._obs_taught_word(conn, cid, day0, day0 + 7200) is None
    # 说了但词不含「昨天才第一次听到」的字 → 也不发(睡吧是老词)
    _utter(conn, cid, "睡吧睡吧睡吧", day0 + 3700)
    assert observer._obs_taught_word(conn, cid, day0, day0 + 7200) is None


def test_ask_tally_line(kid, conn, monkeypatch):
    cid, brain = kid
    plan = dict(day_p=1.0, n=(2, 2), window_h=1.0, mama_p=0.0)
    monkeypatch.setattr(asks, "ASK_STAGE_PLAN", {"toddler": plan})
    NOW = T0 + 5 * DAY
    asks.plan_asks(conn, cid, now=NOW)
    rows = conn.execute("SELECT * FROM scheduled_event WHERE kind='ask'"
                        " ORDER BY due_at").fetchall()
    asks.fire_due_asks(conn, brain, cid, now=rows[0]["due_at"] + 1)
    asks.fire_due_asks(conn, brain, cid, now=rows[1]["due_at"] + 1)
    child_mod.apply_action(conn, cid, "papa", "talk", idempotency_key="a1",
                          now=rows[0]["due_at"] + 60)
    last = max(r["expires_at"] for r in rows)
    asks.settle_asks(conn, cid, now=last + 1)
    day0 = observer._midnight(NOW)
    line = observer._obs_asks(conn, cid, day0, day0 + 86399)
    assert line is not None and "2 次" in line and "1 次" in line


def test_ask_tally_silent_when_none(kid, conn):
    cid, _ = kid
    day0 = observer._midnight(T0 + 5 * DAY)
    assert observer._obs_asks(conn, cid, day0, day0 + 86399) is None


# ── 告状/吐槽 ──

def test_tattle_mama_disc_beats_noplay(kid, conn):
    cid, _ = kid
    t = T0 + 5 * DAY + 12 * 3600
    child_mod.apply_action(conn, cid, "papa", "discipline",
                          idempotency_key="d1", now=t - 600)
    tt = asks.derive_tattle(conn, cid, "mama", t)
    assert texts.TATTLE_MAMA_DISC in tt and "{voice}" in tt


def test_tattle_mama_noplay_and_praise(kid, conn):
    cid, _ = kid
    t = T0 + 5 * DAY + 12 * 3600
    assert texts.TATTLE_MAMA_NOPLAY in asks.derive_tattle(conn, cid, "mama", t)
    child_mod.apply_action(conn, cid, "papa", "play", idempotency_key="p1",
                          now=t - 300)
    assert "1 回" in asks.derive_tattle(conn, cid, "mama", t)


def test_tattle_papa_about_mama(kid, conn):
    cid, _ = kid
    t = T0 + 5 * DAY + 12 * 3600
    assert texts.TATTLE_PAPA_NOTOUCH in asks.derive_tattle(conn, cid, "papa", t)
    child_mod.apply_action(conn, cid, "mama", "mama_touch", idempotency_key="m1",
                          now=t - 300)
    assert "1 下" in asks.derive_tattle(conn, cid, "papa", t)


def test_tattle_hook_in_fire(kid, conn, monkeypatch):
    """TATTLE_P=1 时 fire 出来的场景稿=告状稿,voice 槽仍被他真实的话填上。"""
    from nursery import config as _cfg
    cid, brain = kid
    monkeypatch.setattr(_cfg, "TATTLE_P", 1.0)
    plan = dict(day_p=1.0, n=(1, 1), window_h=2.0, mama_p=1.0)
    monkeypatch.setattr(asks, "ASK_STAGE_PLAN", {"toddler": plan})
    asks.plan_asks(conn, cid, now=T0 + 5 * DAY)
    due = conn.execute("SELECT due_at FROM scheduled_event WHERE kind='ask'"
                       ).fetchone()["due_at"]
    asks.fire_due_asks(conn, brain, cid, now=due + 1)
    p = json.loads(conn.execute(
        "SELECT payload_json FROM outbox WHERE kind='nursery.ask'"
        ).fetchone()["payload_json"])
    assert p["scene"] == "tattle" and "咬耳朵" in p["text"]
    assert "{voice}" not in p["text"]        # 槽已被填(真嘴或兜底稿)


def test_taught_word_null_speaker_fallback(kid, conn):
    """legacy/无 speaker 语料归因兜底=「有人」,不炸不冒名(评审)。"""
    cid, brain = kid
    day0 = observer._midnight(T0 + 6 * DAY)
    child_mod.feed_corpus(conn, brain, cid, "鲸鱼会喷水柱。", actor="papa",
                          speaker=None, idempotency_key="teach3",
                          now=day0 - 3600)
    _utter(conn, cid, "鲸鱼鲸鱼", day0 + 3600)
    line = observer._obs_taught_word(conn, cid, day0, day0 + 7200)
    assert line is not None and "有人昨天教他的" in line
