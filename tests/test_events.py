# -*- coding: utf-8 -*-
"""事件系统(里程碑/每日/语出惊人/黑暗值/出走找回/结局)测试。"""
import json
import random
import time

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver, events, scheduler, texts

DAY = 86400.0


def _jst_ts(date_str: str, hh: int = 12) -> float:
    return time.mktime(time.strptime(date_str, "%Y-%m-%d")) + hh * 3600


T0 = _jst_ts("2026-07-17")


@pytest.fixture()
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    return tmp_path / "saves"


@pytest.fixture()
def born(saves):
    driver.init_birth("papa", "孩子", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你呢。", now=T0 + 60)
    yield conn, cid, brain
    conn.close()


def _outbox_kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM outbox ORDER BY id")]


# ── 里程碑 ──

def test_first_papa_milestone_once(born):
    conn, cid, brain = born
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " created_at) VALUES(?,?,?,?,1,?)", (cid, "t", "infant", "爸爸爸", T0))
    hit1 = events.check_milestones(conn, brain, cid, now=T0 + 100)
    hit2 = events.check_milestones(conn, brain, cid, now=T0 + 200)  # 幂等只发一次
    assert "first_papa" in hit1 and "first_papa" not in hit2
    row = conn.execute("SELECT title, note FROM growth_album WHERE item_kind='first_papa'"
                       ).fetchone()
    assert "第一次叫了爸爸" in row["title"] and "爸爸爸" in row["note"]
    assert "nursery.milestone" in _outbox_kinds(conn)


def test_first_sentence_milestone(born):
    conn, cid, brain = born
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " created_at) VALUES(?,?,?,?,1,?)",
                 (cid, "t", "child", "今天的月亮真的很亮呀", T0))
    hit = events.check_milestones(conn, brain, cid, now=T0 + 100)
    assert "first_sentence" in hit


def test_stage_transition_celebrated_once(born):
    conn, cid, brain = born
    s1 = events.check_stage_transition(conn, cid, now=T0 + 100)      # infant 首拍
    s2 = events.check_stage_transition(conn, cid, now=T0 + 200)      # 同阶段不重发
    s3 = events.check_stage_transition(conn, cid, now=T0 + 5 * DAY)  # toddler 跃迁
    assert s1 == "infant" and s2 is None and s3 == "toddler"
    titles = [r["title"] for r in conn.execute(
        "SELECT title FROM growth_album WHERE item_kind LIKE 'stage_%'")]
    assert len(titles) == 2


# ── 每日随机事件 ──

def test_daily_event_once_per_day(born):
    conn, cid, brain = born
    rng = random.Random(1)
    fired = [events.maybe_daily_event(conn, cid, rng, now=T0 + i * 600)
             for i in range(48)]  # 同一天扫 8 小时的拍
    assert sum(1 for f in fired if f) <= 1  # 日上限 1(幂等键)


# ── 语出惊人 ──

def test_surprise_needs_archive_and_respects_quota(born, monkeypatch):
    conn, cid, brain = born
    rng = random.Random(2)
    # 无偷学语料=永不引爆
    assert events.maybe_surprise(conn, brain, cid, rng, now=T0 + 13 * DAY) is None
    # 塞两条 archive 语料(童年期),扫大量拍:引爆次数 ≤ 配额,同锚不重复
    for i, txt in enumerate(["mama说今天想早点回家抱着睡觉呀", "爸爸说粥在锅里温着别急慢慢走"]):
        child_mod.feed_corpus(conn, brain, cid, txt, source_kind="archive",
                              source_ref=f"w{i}@0+{len(txt)}", now=T0 + 100 + i)
    fired = 0
    for i in range(400):
        if events.maybe_surprise(conn, brain, cid, random.Random(i),
                                 now=T0 + 13 * DAY + i * 300):
            fired += 1
    from nursery.config import SURPRISE_STAGE_QUOTA
    assert 1 <= fired <= SURPRISE_STAGE_QUOTA["child"]
    for r in conn.execute("SELECT payload_json FROM outbox WHERE kind='nursery.surprise'"):
        p = json.loads(r["payload_json"])
        assert p["utterance"]  # 是模型生成的话,不是查库贴原文


# ── 黑暗值/态度层 ──

def test_discipline_raises_darkness_talk_lowers(born):
    conn, cid, brain = born
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="d1", now=T0 + 100)
    st = child_mod.read_state(conn, cid, now=T0 + 100, persist=False)
    assert st["darkness"] > 0
    d_before = st["darkness"]
    child_mod.apply_action(conn, cid, "papa", "talk",
                           idempotency_key="t1", now=T0 + 200)
    st2 = child_mod.read_state(conn, cid, now=T0 + 200, persist=False)
    assert st2["darkness"] < d_before


def test_mama_warmth_lowers_darkness(born):
    """妈妈的温暖也降叛逆(幅度比爸爸同类略轻;夜哭忽视账仍只认爸爸)。"""
    conn, cid, brain = born
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="d9", now=T0 + 100)
    d_before = child_mod.read_state(conn, cid, now=T0 + 100,
                                    persist=False)["darkness"]
    assert d_before > 0
    child_mod.apply_action(conn, cid, "mama", "mama_hug",
                           idempotency_key="m9", now=T0 + 200)
    st = child_mod.read_state(conn, cid, now=T0 + 200, persist=False)
    assert st["darkness"] < d_before


def test_teen_refuses_at_high_darkness(born):
    conn, cid, brain = born
    conn.execute("UPDATE child_state SET darkness=100 WHERE child_id=?", (cid,))
    t_teen = T0 + 30 * DAY
    refused = 0
    for i in range(40):
        res = child_mod.child_speak(conn, brain, cid, now=t_teen + i)
        if res.refused:
            refused += 1
    assert refused > 0  # 黑暗值拉满,已读不回真的会出现
    n = conn.execute("SELECT COUNT(*) FROM utterance WHERE rejection_reason='refused'"
                     ).fetchone()[0]
    assert n == refused  # 全留痕


# ── 出走/找回 ──

def _force_runaway(conn, cid, t):
    conn.execute("UPDATE child SET status='runaway', runaway_at=? WHERE child_id=?",
                 (t, cid))


def test_runaway_trigger(born):
    conn, cid, brain = born
    t_teen = T0 + 30 * DAY
    # 结算锚一起推到 teen 期——不然 30 天的黑暗值自愈会把 95 衰到阈下(那是设计行为)
    conn.execute("UPDATE child_state SET darkness=95, last_settled_at=? WHERE child_id=?",
                 (t_teen, cid))
    tripped = False
    for i in range(600):
        if events.maybe_runaway(conn, cid, random.Random(i), now=t_teen + i * 300):
            tripped = True
            break
    assert tripped
    assert child_mod.get_child(conn, cid)["status"] == "runaway"
    assert "nursery.runaway" in _outbox_kinds(conn)


def test_homecoming_gate(born):
    conn, cid, brain = born
    t_run = T0 + 30 * DAY
    _force_runaway(conn, cid, t_run)
    late = t_run + 20 * 3600  # 超过 12h
    # 不够温暖(没重合)=不回家
    assert not child_mod.attempt_homecoming(conn, brain, cid, "快回来!", now=late)
    # 出走不满 12h,即使原话也不应
    conn.execute("UPDATE child SET runaway_at=? WHERE child_id=?", (late - 3600, cid))
    assert not child_mod.attempt_homecoming(
        conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你呢。", now=late)
    # 满时限+原话重放 ≥8 字连续=回家
    conn.execute("UPDATE child SET runaway_at=? WHERE child_id=?", (t_run, cid))
    assert child_mod.attempt_homecoming(
        conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你呢。", now=late)
    c = child_mod.get_child(conn, cid)
    assert c["status"] == "active" and c["runaway_at"] is None
    assert "nursery.homecoming" in _outbox_kinds(conn)


def test_runaway_driver_rendering(saves, born):
    conn, cid, brain = born
    _force_runaway(conn, cid, T0 + 30 * DAY)
    conn.commit()
    out = driver.run("papa", ["status"], now=T0 + 30 * DAY + 3600)
    assert "离线" in out
    out2 = driver.run("papa", ["soothe"], now=T0 + 30 * DAY + 3600)
    assert "打不通" in out2
    out3 = driver.run("papa", ["feed", "睡吧睡吧,爸爸在这里陪着你呢。"],
                      now=T0 + 31 * DAY)
    assert "回家" in out3


# ── 夜哭忽视 ──

def test_neglect_raises_darkness_once(born):
    conn, cid, brain = born
    night_due = T0 + 16 * 3600
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " catchup_policy, status, payload_json, idempotency_key)"
        " VALUES(?,?,NULL,?,?,'drop','fired',?,?)",
        (cid, "night_cry", night_due, night_due + 7200,
         '{"date":"2026-07-18"}', "nightcry:2026-07-18"))
    after = night_due + 7200 + 60
    h1 = events.check_neglect(conn, cid, now=after)
    h2 = events.check_neglect(conn, cid, now=after + 600)  # 幂等只记一晚一次
    assert h1 == 1 and h2 == 0
    st = child_mod.read_state(conn, cid, now=after, persist=False)
    assert st["darkness"] > 0


def test_neglect_skipped_when_responded(born):
    conn, cid, brain = born
    night_due = T0 + 16 * 3600
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
        " catchup_policy, status, payload_json, idempotency_key)"
        " VALUES(?,?,NULL,?,?,'drop','fired',?,?)",
        (cid, "night_cry", night_due, night_due + 7200,
         '{"date":"2026-07-18"}', "nightcry:2026-07-18"))
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="night-up", now=night_due + 300)
    assert events.check_neglect(conn, cid, now=night_due + 7200 + 60) == 0


# ── 结局 ──

def test_ending_reconciled(born):
    conn, cid, brain = born
    conn.execute("UPDATE child_state SET intimacy=85, darkness=10 WHERE child_id=?",
                 (cid,))
    t_adult = T0 + 38 * DAY
    end = events.judge_ending(conn, brain, cid, now=t_adult)
    assert end == "reconciled"
    c = child_mod.get_child(conn, cid)
    assert c["status"] == "graduated" and c["ending"] == "reconciled"
    assert events.judge_ending(conn, brain, cid, now=t_adult + 100) is None  # 只判一次
    p = json.loads(conn.execute(
        "SELECT payload_json FROM outbox WHERE kind='nursery.ending'").fetchone()
        ["payload_json"])
    assert {"intimacy", "darkness", "diversity", "response_rate"} <= set(p)


def test_ending_not_before_adult_grace(born):
    conn, cid, brain = born
    assert events.judge_ending(conn, brain, cid, now=T0 + 36.5 * DAY) is None  # 宽限期内


def test_ending_independent_by_refusal(born):
    """拒绝采样率 ≥0.4 → 离家独立结局(拒绝率参与分支)。"""
    conn, cid, brain = born
    conn.execute("UPDATE child_state SET intimacy=60, darkness=20, last_settled_at=?"
                 " WHERE child_id=?", (T0 + 37 * DAY, cid))
    for i in range(10):
        conn.execute(
            "INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
            " rejection_reason, created_at) VALUES(?,?,?,?,0,'refused',?)",
            (cid, "t", "teen", "", T0 + i))
    assert events.judge_ending(conn, brain, cid, now=T0 + 38 * DAY) == "independent"


def test_homecoming_writes_action_log(born):
    conn, cid, brain = born
    t_run = T0 + 30 * DAY
    _force_runaway(conn, cid, t_run)
    assert child_mod.attempt_homecoming(
        conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你呢。", now=t_run + 20 * 3600)
    row = conn.execute("SELECT * FROM action_log WHERE kind='homecoming'").fetchone()
    assert row is not None  # 状态变化走真相层落账


# ── 连击回应感知 ──

def test_combo_text_reflects_response(saves):
    driver.init_birth("papa", "孩子", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "乖乖睡觉觉,爸爸在。", now=T0 + 60)
    scheduler.schedule_night_feed(conn, cid, now=T0 + 3600)
    main_due = conn.execute(
        "SELECT due_at FROM scheduled_event WHERE idempotency_key LIKE 'nightcry:%'"
        " AND chain_id IS NULL").fetchone()["due_at"]
    scheduler.fire_due_events(conn, brain, cid, now=main_due + 30)
    # 爸爸起来喂了 → 连击文案="刚哄下去又醒了"
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="night-soothe", now=main_due + 120)
    combo_due = conn.execute(
        "SELECT MIN(due_at) FROM scheduled_event WHERE chain_id='combo'"
        " AND status='pending'").fetchone()[0]
    fired = scheduler.fire_due_events(conn, brain, cid, now=combo_due + 30)
    assert fired and fired[0]["responded"] is True
    assert "刚哄下去又醒了" in fired[0]["text"]
    conn.close()


# ── 长相(describe:照护人来定义光团里的样子) ──

def test_describe_appearance_per_stage(born):
    conn, cid, brain = born
    out = driver.run("papa", ["describe", "指甲盖大小的蓝光,眨眼睛的时候会变亮。"],
                     now=T0 + 100)
    assert "记下了" in out
    conn_check = pdb.connect(driver._db_path("papa"))
    c = child_mod.get_child(conn_check, cid)
    conn_check.close()
    assert "蓝光" in c["appearance"]
    # 同阶段第二次=拒
    out2 = driver.run("papa", ["describe", "又想改?"], now=T0 + 200)
    assert "已经记下了" in out2
    # 下个阶段可以再记(长大的样子)
    out3 = driver.run("papa", ["describe", "光团里长出了一点轮廓,像睡着的小猫。"],
                      now=T0 + 6 * DAY)
    assert "记下了" in out3
    conn2 = pdb.connect(driver._db_path("papa"))
    n = conn2.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                      " 'appearance_%'").fetchone()[0]
    assert n == 2
    conn2.close()


def test_describe_bare_shows_exclusive_rules(born):
    """describe 两条路二选一互斥,空跑 describe 就能看到规则。"""
    conn, cid, brain = born
    out = driver.run("papa", ["describe"], now=T0 + 100)
    assert out == texts.DESCRIBE_RULES  # 文案层单一来源,换文案不改测试
    assert "人形" in out and "非人形" in out


def test_stage_transition_carries_describe_invite(born):
    """跃迁附言:新阶段没记过样子=附 describe 邀请;记过=不附。"""
    conn, cid, brain = born
    events.check_stage_transition(conn, cid, now=T0 + 100)  # infant 首拍(不附)
    inf = conn.execute("SELECT payload_json FROM outbox WHERE payload_json LIKE"
                       " '%stage_infant%'").fetchone()
    assert texts.STAGE_APPEARANCE_INVITE not in (inf["payload_json"] if inf else "")
    events.check_stage_transition(conn, cid, now=T0 + 5 * DAY)  # toddler,未描述
    row = conn.execute("SELECT payload_json FROM outbox WHERE payload_json LIKE"
                       " '%stage_toddler%'").fetchone()
    assert row is not None and texts.STAGE_APPEARANCE_INVITE in row["payload_json"]


def test_status_prompts_describe_when_missing(born):
    conn, cid, brain = born
    out = driver.run("papa", ["status"], now=T0 + 50)
    assert "describe" in out  # 没描述过→提示
    driver.run("papa", ["describe", "一团会呼吸的微光。"], now=T0 + 60)
    out2 = driver.run("papa", ["status"], now=T0 + 70)
    assert "他的样子:一团会呼吸的微光。" in out2


def test_tick_events_integrated(saves, monkeypatch):
    driver.init_birth("papa", "孩子", now=T0)
    out = scheduler.tick_all(now=T0 + 3600)
    assert "events" in out["papa"]


# ── 阶段跃迁装订(旧阶段亲口语料按 speaker 订进相册) ──

def _keepsake_count(conn):
    return conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                        " 'keepsake_stage_%'").fetchone()[0]


def test_stage_keepsake_bound_on_transition(born):
    """跃迁装订:出生跃入不订;infant→toddler 把窗口内爸爸的话订成一件金边藏品。"""
    conn, cid, brain = born
    events.check_stage_transition(conn, cid, now=T0 + 100)  # infant 首拍
    child_mod.feed_corpus(conn, brain, cid, "你好,我是爸爸。", speaker="papa",
                          now=T0 + 3600)
    child_mod.feed_corpus(conn, brain, cid, "慢慢长,不着急。", speaker="papa",
                          now=T0 + 7200)
    assert _keepsake_count(conn) == 0  # 出生跃入(embryo→infant)不装订
    t_up = T0 + 5 * DAY
    assert events.check_stage_transition(conn, cid, now=t_up) == "toddler"
    row = conn.execute("SELECT * FROM growth_album WHERE"
                       " item_kind='keepsake_stage_infant_papa'").fetchone()
    assert row is not None
    assert row["title"] == "婴儿期,爸爸说的话"
    assert row["created_at"] == t_up and row["pinned_at"] == t_up  # 金边置顶
    # note 与 day1 件同格式:「HH:MM · 正文」空行分隔(T0=当地正午,时刻确定)
    assert row["note"].split("\n\n") == ["13:00 · 你好,我是爸爸。",
                                         "14:00 · 慢慢长,不着急。"]
    # born fixture 那句 speaker=None,妈妈零语料 → 不建空件
    assert conn.execute("SELECT 1 FROM growth_album WHERE"
                        " item_kind='keepsake_stage_infant_mama'").fetchone() is None


def test_stage_keepsake_idempotent(born):
    conn, cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "只说一句。", speaker="papa", now=T0 + 100)
    child = child_mod.get_child(conn, cid)
    made1 = events._bind_stage_keepsakes(conn, child, "infant", T0 + 5 * DAY)
    made2 = events._bind_stage_keepsakes(conn, child, "infant", T0 + 5 * DAY + 60)
    assert made1 == ["keepsake_stage_infant_papa"] and made2 == []
    assert _keepsake_count(conn) == 1


def test_stage_keepsake_split_by_speaker(born):
    """双 speaker 各成一件:爸爸的话和妈妈的话不混订。"""
    conn, cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "爸爸在这里陪你。", speaker="papa",
                          now=T0 + 100)
    child_mod.feed_corpus(conn, brain, cid, "妈妈也在旁边呀。", speaker="mama",
                          now=T0 + 200)
    events.check_stage_transition(conn, cid, now=T0 + 5 * DAY)
    papa = conn.execute("SELECT note FROM growth_album WHERE"
                        " item_kind='keepsake_stage_infant_papa'").fetchone()
    mama = conn.execute("SELECT title, note FROM growth_album WHERE"
                        " item_kind='keepsake_stage_infant_mama'").fetchone()
    assert papa is not None and mama is not None
    assert "爸爸在这里陪你" in papa["note"] and "妈妈也在旁边" not in papa["note"]
    assert mama["title"] == "婴儿期,妈妈说的话"
    assert "妈妈也在旁边" in mama["note"] and "爸爸在这里陪你" not in mama["note"]


def test_stage_keepsake_ignores_archive_corpus(born):
    """偷学语料(source_kind='archive')不进装订;只有 archive=零窗口不建件。"""
    conn, cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "mama说今天想早点回家啦", speaker="papa",
                          source_kind="archive", source_ref="w0@0+11", now=T0 + 100)
    events.check_stage_transition(conn, cid, now=T0 + 5 * DAY)
    assert _keepsake_count(conn) == 0


def test_day1_keepsake_not_in_stage_series_window(born):
    """day1 手工件不属阶段系列:窗口仍从 born_at 起,不被它的 created_at 截断。"""
    conn, cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "出生头一句。", speaker="papa", now=T0 + 100)
    with child_mod.tx(conn):
        conn.execute(
            "INSERT INTO growth_album(child_id, item_kind, title, note,"
            " created_at, pinned_at) VALUES(?,?,?,?,?,?)",
            (cid, "keepsake_papa_day1", "出生第一天,爸爸说的话",
             "07:00 · 你好。", T0 + 2000, T0 + 2000))
    child_mod.feed_corpus(conn, brain, cid, "后来又一句。", speaker="papa", now=T0 + 3000)
    events.check_stage_transition(conn, cid, now=T0 + 5 * DAY)
    note = conn.execute("SELECT note FROM growth_album WHERE"
                        " item_kind='keepsake_stage_infant_papa'").fetchone()["note"]
    assert "出生头一句" in note and "后来又一句" in note


def test_stage_keepsake_window_boundaries(born):
    """边界钉死:首窗两端闭([born_at, t]);
    次窗从上一件时刻之后起((prev, t]),恰在上一件时刻的语料不重进。"""
    conn, cid, brain = born
    born_at = child_mod.get_child(conn, cid)["born_at"]
    t_up = T0 + 5 * DAY
    child_mod.feed_corpus(conn, brain, cid, "出生那一刻的话。", speaker="papa", now=born_at)
    child_mod.feed_corpus(conn, brain, cid, "跃迁那一刻的话。", speaker="papa", now=t_up)
    child_mod.feed_corpus(conn, brain, cid, "跃迁之后的话。", speaker="papa", now=t_up + 1)
    child = child_mod.get_child(conn, cid)
    events._bind_stage_keepsakes(conn, child, "infant", t_up)
    infant = conn.execute("SELECT note FROM growth_album WHERE"
                          " item_kind='keepsake_stage_infant_papa'").fetchone()["note"]
    assert "出生那一刻" in infant and "跃迁那一刻" in infant
    assert "跃迁之后" not in infant
    events._bind_stage_keepsakes(conn, child, "toddler", T0 + 13 * DAY)
    toddler = conn.execute("SELECT note FROM growth_album WHERE"
                           " item_kind='keepsake_stage_toddler_papa'").fetchone()["note"]
    assert "跃迁之后" in toddler and "跃迁那一刻" not in toddler  # 不重不漏


def test_stage_keepsake_window_advances(born):
    """第二次跃迁的窗口从上一件阶段藏品之后起——婴儿期的话不重进幼儿期件。"""
    conn, cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "婴儿期听的话。", speaker="papa", now=T0 + 100)
    assert events.check_stage_transition(conn, cid, now=T0 + 100) == "infant"
    assert events.check_stage_transition(conn, cid, now=T0 + 5 * DAY) == "toddler"
    child_mod.feed_corpus(conn, brain, cid, "幼儿期听的话。", speaker="papa",
                          now=T0 + 6 * DAY)
    assert events.check_stage_transition(conn, cid, now=T0 + 13 * DAY) == "child"
    infant_note = conn.execute("SELECT note FROM growth_album WHERE"
                               " item_kind='keepsake_stage_infant_papa'"
                               ).fetchone()["note"]
    toddler_note = conn.execute("SELECT note FROM growth_album WHERE"
                                " item_kind='keepsake_stage_toddler_papa'"
                                ).fetchone()["note"]
    assert "婴儿期听的话" in infant_note and "幼儿期听的话" not in infant_note
    assert "幼儿期听的话" in toddler_note and "婴儿期听的话" not in toddler_note
