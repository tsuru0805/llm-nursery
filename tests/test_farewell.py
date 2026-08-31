# -*- coding: utf-8 -*-
"""v0.4 毕业过渡:预告→成年日→他提出离开→告别窗→窗满他自己告别。

时间轴按默认档 policy v2:teen 上限 48=成年日。预告 45/46/47;成年日当晚
(≥20 点,或成年满 1 天兜底)开窗;窗 3 天(逻辑天,冻龄安全);判定=窗开后
任一非 system 的 farewell 落账;五分支判分口径与 v0.3 一致。
时刻锚用本地 mktime(20 点档判定走 time.localtime,固定 epoch 会随贡献者
时区漂——测试必须钉本地钟)。
"""
import json
import time

import pytest

from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import driver, events, texts

DAY = 86400.0
T0 = time.mktime((2030, 1, 1, 12, 0, 0, 0, 0, -1))   # 出生钉本地正午


def _day_at(n: int, hour: int) -> float:
    """出生后第 n 个本地日的 hour 点(n=48 即成年日当天)。"""
    lt = time.localtime(T0)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + int(n),
                        hour, 0, 0, 0, 0, -1))


@pytest.fixture
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    return tmp_path / "saves"


@pytest.fixture
def grown(saves):
    """teen 末尾的孩子(默认 v2 档;高亲密——判结局该落 reconciled)。"""
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你。",
                          now=T0 + 60)
    conn.execute("UPDATE child_state SET intimacy=85, darkness=10")
    conn.commit()
    yield conn, cid, brain
    conn.close()


def _open_window(conn, cid, t=None):
    """走真机制开窗:成年日白天先庆祝(stage_adult 落相册,双发闸要它 ≥2h 前),
    晚 21 点跑一拍 arc。返回开窗那拍的时刻。"""
    events.check_stage_transition(conn, cid, now=_day_at(48, 13))
    t = _day_at(48, 21) if t is None else t
    out = events.tick_farewell_arc(conn, cid, now=t)
    assert events.farewell_window(conn, cid) is not None, out
    return t


# ── ① 渐进预告 ──

def test_pre_farewell_sequence_and_idempotent(grown):
    conn, cid, brain = grown
    assert events.tick_farewell_arc(conn, cid, now=T0 + 44 * DAY) == {}
    for i, d in enumerate((45.2, 46.2, 47.2), 1):
        out = events.tick_farewell_arc(conn, cid, now=T0 + d * DAY)
        assert out.get(f"pre_{i}") is True
        assert events.tick_farewell_arc(conn, cid, now=T0 + d * DAY) == {}
    rows = conn.execute("SELECT payload_json FROM outbox WHERE"
                        " idempotency_key LIKE 'prefw:%'").fetchall()
    assert len(rows) == 3
    titles = [json.loads(r["payload_json"])["title"] for r in rows]
    assert any("整理东西" in s for s in titles)
    # 预告进相册(毕业叙事的一部分)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind"
                        " LIKE 'pre_farewell_%'").fetchone()[0] == 3


def test_pre_farewell_catchup_all_at_once(grown):
    """调度停摆几天:day 47.5 一拍补齐三条(补播语义,不漏)。"""
    conn, cid, brain = grown
    out = events.tick_farewell_arc(conn, cid, now=T0 + 47.5 * DAY)
    assert out.get("pre_1") and out.get("pre_2") and out.get("pre_3")


# ── ② 成年日与开窗 ──

def test_coming_of_age_title(grown):
    conn, cid, brain = grown
    assert events.check_stage_transition(conn, cid, now=T0 + 48.1 * DAY) == "adult"
    row = conn.execute("SELECT title FROM growth_album WHERE"
                       " item_kind='stage_adult'").fetchone()
    assert row["title"] == texts.COMING_OF_AGE_TITLE.format(name="囡")


def test_window_opens_evening_not_daytime(grown):
    conn, cid, brain = grown
    events.check_stage_transition(conn, cid, now=_day_at(48, 13))
    events.tick_farewell_arc(conn, cid, now=_day_at(48, 14))
    assert events.farewell_window(conn, cid) is None   # 白天不开(正常过成年日)
    out = events.tick_farewell_arc(conn, cid, now=_day_at(48, 21))
    assert out.get("window_opened") is True
    ann = conn.execute("SELECT note FROM growth_album WHERE"
                       " item_kind='leaving_announce'").fetchone()
    assert "我想出去住了" in ann["note"]
    # 重放幂等
    assert events.tick_farewell_arc(
        conn, cid, now=_day_at(48, 22)).get("window_opened") is None
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='leaving_announce'").fetchone()[0] == 1


def test_announce_not_same_beat_as_coming_of_age(grown):
    """双发闸:跃迁本身落在 20 点后时,生日会与「我想出去住了」不许同拍。"""
    conn, cid, brain = grown
    t_evening = _day_at(48, 21)
    events.check_stage_transition(conn, cid, now=t_evening)
    out = events.tick_farewell_arc(conn, cid, now=t_evening + 60)
    assert out.get("window_opened") is None
    out = events.tick_farewell_arc(conn, cid, now=t_evening + 2.5 * 3600)
    assert out.get("window_opened") is True


def test_window_opens_by_lag_fallback(grown):
    """20 点档全漏(调度空窗):成年满 1 天兜底开窗(T0 在正午→day49 13 点=49.04 天)。"""
    conn, cid, brain = grown
    out = events.tick_farewell_arc(conn, cid, now=_day_at(49, 13))
    assert out.get("window_opened") is True


# ── ③ 窗内小变化+静默 ──

def test_window_daily_lines(grown):
    conn, cid, brain = grown
    _open_window(conn, cid)
    for n, day in enumerate((49, 50, 51), 1):
        out = events.tick_farewell_arc(conn, cid, now=_day_at(day, 10))
        assert out.get(f"window_{n}") is True
        row = conn.execute("SELECT payload_json FROM outbox WHERE"
                           " idempotency_key=?", (f"fwwin:{n}:{cid}",)).fetchone()
        assert json.loads(row["payload_json"])["title"] == \
            texts.FAREWELL_WINDOW_LINES[n - 1].format(name="囡")


def test_departure_window_quiet_gate(grown):
    conn, cid, brain = grown
    assert not events.in_departure_window(conn, cid, now=_day_at(48, 14))
    _open_window(conn, cid)
    assert events.in_departure_window(conn, cid, now=_day_at(49, 10))
    evs = events.tick_events(conn, brain, cid, now=_day_at(49, 15))
    assert "daily" not in evs   # 窗内不抽每日随机事件


# ── ④ farewell / stay 语义 ──

def test_farewell_judges_ending(grown):
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    assert events.judge_ending(conn, brain, cid, now=t + 60) is None
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="f1",
                           now=t + 120)
    assert events.judge_ending(conn, brain, cid, now=t + 180) == "reconciled"
    assert child_mod.get_child(conn, cid)["status"] == "graduated"


def test_farewell_before_window_does_not_count(grown):
    conn, cid, brain = grown
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="e0",
                           now=_day_at(48, 10))
    t = _open_window(conn, cid)
    assert events.judge_ending(conn, brain, cid, now=t + 60) is None


def test_system_farewell_never_judges(grown):
    """数据层闸:system 的 farewell 账不解锁判定(照护人/self 才算——
    NURSERY_PLAYERS 自定义 persona 无法静态枚举,故排除 system 而非枚举白名单)。"""
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    child_mod.apply_action(conn, cid, "system", "farewell",
                           idempotency_key="sfw", now=t + 60)
    assert events.judge_ending(conn, brain, cid, now=t + 120) is None


def test_mama_farewell_counts(saves, grown):
    """告别窗内第二照护人也能说(谁先说算谁的)。"""
    conn, cid, brain = grown
    r = json.loads(driver.run("papa", ["mama", "farewell"], now=_day_at(48, 14)))
    assert r == {"ok": False, "error": "not_yet"}
    _open_window(conn, cid)
    r = json.loads(driver.run("papa", ["mama", "stay"], now=_day_at(49, 10)))
    assert r["ok"] and "那就明天" in r["line"]
    r = json.loads(driver.run("papa", ["mama", "farewell"], now=_day_at(49, 11)))
    assert r["ok"] and r["ending"] == "reconciled"


def test_stay_repeats_and_speak_stays_normal(grown):
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    for i in range(3):   # 可多次用
        child_mod.apply_action(conn, cid, "papa", "stay",
                               idempotency_key=f"s{i}", now=t + 60 + i)
    res = child_mod.child_speak(conn, brain, cid, trigger="talk", now=t + 300)
    assert res.params.get("stay_day") is None   # 定稿句直出路已退役
    assert events.judge_ending(conn, brain, cid, now=t + 400) is None


def test_driver_farewell_stay_faces(saves, grown):
    conn, cid, brain = grown
    assert "还没提要走的事" in driver.run("papa", ["farewell"],
                                          now=_day_at(48, 14))
    _open_window(conn, cid)
    out = driver.run("papa", ["stay"], now=_day_at(49, 11))
    assert "那就明天" in out
    out = driver.run("papa", ["farewell"], now=_day_at(49, 12))
    assert "到了我会写信的" in out and "理解与原谅" in out


# ── ⑤ 窗满他自己告别 ──

def test_self_farewell_after_window(grown):
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    assert events.tick_farewell_arc(
        conn, cid, now=t + 2.9 * DAY).get("self_farewell") is None
    out = events.tick_farewell_arc(conn, cid, now=t + 3.1 * DAY)
    assert out.get("self_farewell") is True
    row = conn.execute("SELECT actor FROM action_log WHERE kind='farewell'"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor"] == "self"
    ev = conn.execute("SELECT note FROM growth_album WHERE"
                      " item_kind='self_farewell'").fetchone()
    assert ev["note"] == texts.SELF_FAREWELL_NOTE
    evs = events.tick_events(conn, brain, cid, now=t + 3.1 * DAY + 300)
    assert evs.get("ending") == "reconciled"
    assert child_mod.get_child(conn, cid)["status"] == "graduated"
    assert not events.in_departure_window(conn, cid, now=t + 3.2 * DAY)


def test_self_farewell_respects_pause(grown):
    """窗内冻龄:窗口时钟(逻辑天口径)跟着停,窗满顺延。"""
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    child_mod.pause_child(conn, cid, now=t + 1 * DAY)
    child_mod.resume_child(conn, cid, now=t + 5 * DAY)   # 冻了 4 天
    assert events.tick_farewell_arc(
        conn, cid, now=t + 3.5 * DAY).get("self_farewell") is None
    out = events.tick_farewell_arc(conn, cid, now=t + 7.2 * DAY)
    assert out.get("self_farewell") is True


def test_stay_does_not_extend_window(grown):
    conn, cid, brain = grown
    t = _open_window(conn, cid)
    child_mod.apply_action(conn, cid, "papa", "stay", idempotency_key="sx",
                           now=t + 2.9 * DAY)
    out = events.tick_farewell_arc(conn, cid, now=t + 3.1 * DAY)
    assert out.get("self_farewell") is True   # stay 不延总窗
