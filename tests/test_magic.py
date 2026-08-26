# -*- coding: utf-8 -*-
"""v0.3:真实语料魔法四件。全部临时 db+假 archive,假时钟注入。

生成层(decoder.speak)另有专测;本文件用确定性桩钉住 speak 输出,专测机制:
抽签/幂等/fail-open/事件与相册落位(child_id 是 uuid,真采样跨运行不确定,
桩掉才测得稳——规则可测不靠运气)。
"""
import json
import random
import sqlite3
import time

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import events
from nursery import texts, magic
from nursery.decoder import SpeakResult

DAY = 86400.0


def _jst(date_str: str, hh: int = 12, mm: int = 0) -> float:
    return time.mktime(time.strptime(date_str, "%Y-%m-%d")) + hh * 3600 + mm * 60


T0 = _jst("2026-07-17")   # 出生:7-17 正午(childhood=7-29 起)

CORPUS = """他抱着积木过来找爸爸,爸爸看看这个好不好。
妈妈说恐龙是很久很久以前的动物,比房子还要大。
今天在外面看到了一只很大的狗狗,它冲我摇尾巴。
把果果分给妈妈一半,分享是好孩子才会做的事。
不怕不怕,爸爸在,妈妈也在,谁都不会走。"""


def _stub_speak(text: str = "恐龙也要回家吃饭的"):
    def fake(model, guard, stage, rng, **kw):
        return SpeakResult(text=text, retries=0, max_overlap=0, accepted=True,
                           stage=stage, params={})
    return fake


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_magic.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


@pytest.fixture()
def fake_archive(tmp_path, monkeypatch):
    p = str(tmp_path / "fake_archive.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE windows (id TEXT PRIMARY KEY, conv_uuid TEXT,"
              " win_index INT, text TEXT, date TEXT, viewer TEXT)")
    c.executemany("INSERT INTO windows VALUES(?,?,?,?,?,?)", [
        ("w1", "c1", 1, "妈妈说今天加班到很晚,爸爸说粥在锅里温着。" * 3,
         "2026-05-01", "papa"),
        ("w2", "c1", 2, "两个人在阳台看了很久的月亮,谁都没说话。" * 3,
         "2026-05-02", "papa"),
    ])
    c.commit()
    c.close()
    monkeypatch.setenv("NURSERY_ARCHIVE_DB", p)
    return p


def _feed_archive(conn, brain, cid, n=2):
    """喂 n 条偷学语料(ref 指向 fake_archive 的窗)。"""
    frags = ["妈妈说今天加班到很晚,爸爸说粥在锅里温着,路上慢慢走别急。",
             "两个人在阳台看了很久的月亮,谁都没说话,风把窗帘吹起来。"]
    for i in range(n):
        child_mod.feed_corpus(conn, brain, cid, frags[i],
                              source_kind="archive",
                              source_ref=f"w{i + 1}@0+{len(frags[i])}",
                              actor="system", action_kind="overhear",
                              idempotency_key=f"steal:w{i + 1}", now=T0 + 100 + i)


def _events(conn, event: str) -> list:
    out = []
    for r in conn.execute("SELECT payload_json FROM outbox"
                          " WHERE kind='nursery.event' ORDER BY id"):
        p = json.loads(r["payload_json"])
        if p.get("event") == event:
            out.append(p)
    return out


NOW_CHILD = T0 + 13 * DAY + 10 * 3600   # 童年期第 13 天 22:00(必过 9-21 事发窗)


# ── 件1:时空穿越提问 ──

def test_timetravel_emits_real_archive_date(conn, kid, fake_archive, monkeypatch):
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.setattr(magic, "TIMETRAVEL_DAY_P", 1.0)
    assert magic.maybe_timetravel(conn, cid, now=NOW_CHILD) == "timetravel"
    evs = _events(conn, "timetravel")
    assert len(evs) == 1
    p = evs[0]
    assert p["archive_date"] in ("2026-05-01", "2026-05-02")
    assert p["win"] in ("w1", "w2")
    y, m, d = p["archive_date"].split("-")
    assert f"{int(y)}年{int(m)}月{int(d)}日" in p["title"]
    assert "那天你们去哪了" in p["title"]
    # 幂等:同日再问=不问
    assert magic.maybe_timetravel(conn, cid, now=NOW_CHILD + 600) is None
    assert len(_events(conn, "timetravel")) == 1


def test_timetravel_needs_stolen_corpus(conn, kid, fake_archive, monkeypatch):
    """没偷学过=没有可穿越的日子(不裸查全 archive)。"""
    cid, _ = kid
    monkeypatch.setattr(magic, "TIMETRAVEL_DAY_P", 1.0)
    assert magic.maybe_timetravel(conn, cid, now=NOW_CHILD) is None


def test_timetravel_stage_gate(conn, kid, fake_archive, monkeypatch):
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.setattr(magic, "TIMETRAVEL_DAY_P", 1.0)
    assert magic.maybe_timetravel(conn, cid, now=T0 + 5 * DAY + 10 * 3600) is None


def test_timetravel_low_freq_deterministic(conn, kid, fake_archive):
    """默认概率下 30 天量级只发少数几次,且同日重抽结果一致(确定性)。"""
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    hits = 0
    for i in range(10):   # 童年期 12-22 天,每天两拍
        t = T0 + (12.5 + i) * DAY + 9.6 * 3600
        a = magic.maybe_timetravel(conn, cid, now=t)
        b = magic.maybe_timetravel(conn, cid, now=t + 300)
        assert b is None  # 同日第二拍:或没抽中或已发过,绝不双发
        if a:
            hits += 1
    assert hits <= 5  # p=0.15/天:10 天期望 1.5 次,>5=抽签坏了(P≈6e-5,不赌运气)


# ── 件2:温柔的误译 ──

def test_mistranslate_regenerates_from_archive(conn, kid, monkeypatch):
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.setattr(magic, "MISTRANSLATE_DAY_P", 1.0)
    monkeypatch.setattr("nursery.decoder.speak", _stub_speak("月亮在锅里温着"))
    assert magic.maybe_mistranslate(conn, brain, cid, now=NOW_CHILD) == "mistranslate"
    evs = _events(conn, "mistranslate")
    assert len(evs) == 1
    assert evs[0]["utterance"] == "月亮在锅里温着"
    assert "「月亮在锅里温着」" in evs[0]["title"]
    assert "一本正经" in evs[0]["title"]
    assert evs[0]["source_ref"].startswith("w")   # 锚源留痕
    # 幂等
    assert magic.maybe_mistranslate(conn, brain, cid, now=NOW_CHILD + 600) is None


def test_mistranslate_needs_archive_and_stage(conn, kid, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr(magic, "MISTRANSLATE_DAY_P", 1.0)
    # 无偷学语料=静默
    assert magic.maybe_mistranslate(conn, brain, cid, now=NOW_CHILD) is None
    # 婴儿期=静默(还复述不了道理)
    _feed_archive(conn, brain, cid)
    assert magic.maybe_mistranslate(conn, brain, cid, now=T0 + 3600 * 20) is None


def test_mistranslate_speak_rejected_no_event(conn, kid, monkeypatch):
    """护栏全拒=今天作罢,不发兜底事件(内容优先真实生成,不硬凑)。"""
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.setattr(magic, "MISTRANSLATE_DAY_P", 1.0)

    def refuse(model, guard, stage, rng, **kw):
        return SpeakResult(text="", retries=30, max_overlap=0, accepted=False,
                           stage=stage, params={})
    monkeypatch.setattr("nursery.decoder.speak", refuse)
    assert magic.maybe_mistranslate(conn, brain, cid, now=NOW_CHILD) is None
    assert _events(conn, "mistranslate") == []


# ── 件3:睡前故事复述 ──

def _feed_book(conn, brain, cid, text, ref, t):
    child_mod.feed_corpus(conn, brain, cid, text, source_kind="book",
                          source_ref=ref, actor="papa", action_kind="feed",
                          idempotency_key=f"book:{t}", now=t)


def test_story_retell_next_morning(conn, kid, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr("nursery.decoder.speak", _stub_speak("小王子的玫瑰只有一朵"))
    _feed_book(conn, brain, cid, "小王子住在 B612 星球上,他有一朵玫瑰。",
               "book:小王子", T0 + 5 * DAY + 8 * 3600)      # 7-22 20:00 讲故事
    now = T0 + 5 * DAY + 20 * 3600                          # 7-23 08:00
    assert magic.maybe_story_retell(conn, brain, cid, now=now) == "story_retell"
    evs = _events(conn, "story_retell")
    assert len(evs) == 1 and "「小王子的玫瑰只有一朵」" in evs[0]["title"]
    assert evs[0]["source_ref"] == "book:小王子"
    # 首次复述=相册纪念件(一生一次)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='first_story_retell'").fetchone()[0] == 1
    # 幂等
    assert magic.maybe_story_retell(conn, brain, cid, now=now + 600) is None


def test_story_silent_without_book(conn, kid, monkeypatch):
    """book 语料不存在=整件静默(story 通道没喂过就当没这回事)。"""
    cid, brain = kid
    monkeypatch.setattr("nursery.decoder.speak", _stub_speak())
    assert magic.maybe_story_retell(conn, brain, cid,
                                    now=T0 + 5 * DAY + 20 * 3600) is None
    assert _events(conn, "story_retell") == []


def test_story_morning_gate(conn, kid, monkeypatch):
    """夜里不复述——睡一觉才变成自己的(07:00 前静默)。"""
    cid, brain = kid
    monkeypatch.setattr("nursery.decoder.speak", _stub_speak())
    _feed_book(conn, brain, cid, "月亮晚安,星星晚安,小床晚安。",
               "book:晚安月亮", T0 + 5 * DAY + 8 * 3600)
    assert magic.maybe_story_retell(conn, brain, cid,
                                    now=T0 + 5 * DAY + 15 * 3600) is None  # 03:00


def test_story_again_two_nights_same_book(conn, kid, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr("nursery.decoder.speak", _stub_speak("玫瑰要盖玻璃罩子"))
    _feed_book(conn, brain, cid, "小王子给玫瑰浇水,还给她盖上玻璃罩。",
               "book:小王子", T0 + 4 * DAY + 8 * 3600)      # 7-21 晚
    _feed_book(conn, brain, cid, "狐狸说,重要的东西眼睛是看不见的。",
               "book:小王子", T0 + 5 * DAY + 8 * 3600)      # 7-22 晚,同一本
    now = T0 + 5 * DAY + 20 * 3600                          # 7-23 08:00
    assert magic.maybe_story_retell(conn, brain, cid, now=now) == "story_again"
    evs = _events(conn, "story_again")
    assert len(evs) == 1 and "再讲一遍那个" in evs[0]["title"]


# ── 件4:送礼藏品卡(挂每日事件) ──

def _pin_gift_pool(monkeypatch):
    monkeypatch.setattr(events, "DAILY_EVENT_P", 1.0)
    monkeypatch.setitem(texts.DAILY_EVENTS, "child",
                        [("stone", "幼儿园回来,书包里多了一颗小石头,说是捡给你的。")])


def test_gift_event_lands_album_card_with_voice(conn, kid, monkeypatch):
    cid, brain = kid
    _pin_gift_pool(monkeypatch)
    monkeypatch.setattr(child_mod, "child_speak",
                        lambda *a, **k: SpeakResult(
                            text="给爸爸的", retries=0, max_overlap=0,
                            accepted=True, stage="child", params={}))
    rng = random.Random(1)
    assert events.maybe_daily_event(conn, cid, rng, now=NOW_CHILD,
                                    brain=brain) == "stone"
    date = time.strftime("%Y-%m-%d", time.localtime(NOW_CHILD))
    row = conn.execute("SELECT title, note FROM growth_album WHERE item_kind=?",
                       (f"gift_stone_{date}",)).fetchone()
    assert row is not None and "小石头" in row["title"]
    assert row["note"] == "递过来的时候他说:「给爸爸的」"
    # 幂等:同日重放不重卡不重 speak
    assert events.maybe_daily_event(conn, cid, rng, now=NOW_CHILD + 600,
                                    brain=brain) is None
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                        " 'gift_%'").fetchone()[0] == 1


def test_gift_without_brain_card_still_lands(conn, kid, monkeypatch):
    """speak 不可用=卡照落 note 空(fail-open:东西照样递到你手里)。"""
    cid, _ = kid
    _pin_gift_pool(monkeypatch)
    rng = random.Random(1)
    assert events.maybe_daily_event(conn, cid, rng, now=NOW_CHILD) == "stone"
    date = time.strftime("%Y-%m-%d", time.localtime(NOW_CHILD))
    row = conn.execute("SELECT note FROM growth_album WHERE item_kind=?",
                       (f"gift_stone_{date}",)).fetchone()
    assert row is not None and row["note"] is None


def test_non_gift_daily_event_no_card(conn, kid, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr(events, "DAILY_EVENT_P", 1.0)
    monkeypatch.setitem(texts.DAILY_EVENTS, "child",
                        [("drawing", "画了一张全家福,你的头发被涂成了蓝色。")])
    assert events.maybe_daily_event(conn, cid, random.Random(1), now=NOW_CHILD,
                                    brain=brain) == "drawing"
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                        " 'gift_%'").fetchone()[0] == 0


def test_gift_crash_replay_reuses_utterance(conn, kid, monkeypatch):
    """speak 提交后、_emit 前崩过(卡没落 outbox 没落)=重放重用当日 gift 话,
    不再耗一次 RNG 不留第二条孤儿 utterance(评审定案)。"""
    cid, brain = kid
    _pin_gift_pool(monkeypatch)
    # 模拟崩溃现场:当日已有一条 gift utterance,但事件/卡都没发出去
    child_mod.child_speak(conn, brain, cid, trigger="gift", now=NOW_CHILD - 60)
    n_before = conn.execute("SELECT COUNT(*) FROM utterance WHERE child_id=?"
                            " AND trigger='gift'", (cid,)).fetchone()[0]
    prev = conn.execute("SELECT text FROM utterance WHERE child_id=?"
                        " AND trigger='gift' AND accepted=1", (cid,)).fetchone()
    assert events.maybe_daily_event(conn, cid, random.Random(1), now=NOW_CHILD,
                                    brain=brain) == "stone"
    n_after = conn.execute("SELECT COUNT(*) FROM utterance WHERE child_id=?"
                           " AND trigger='gift'", (cid,)).fetchone()[0]
    assert n_after == n_before   # 没有再说一次
    date = time.strftime("%Y-%m-%d", time.localtime(NOW_CHILD))
    note = conn.execute("SELECT note FROM growth_album WHERE item_kind=?",
                        (f"gift_stone_{date}",)).fetchone()["note"]
    if prev is not None:   # 崩前那句真被护栏收了才有 note(收不了=note 空,同 fail-open)
        assert note == f"递过来的时候他说:「{prev['text']}」"


def test_gift_crash_replay_rejected_utterance_no_respeak(conn, kid, monkeypatch):
    """崩前那句被护栏拒了(rejected 也提交了行+推进了 RNG)=重放同样不再 speak,
    卡落地 note 空(评审)。"""
    cid, brain = kid
    _pin_gift_pool(monkeypatch)
    # 模拟崩溃现场:当日已有一条 rejected 的 gift utterance
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " rejection_reason, created_at) VALUES(?,?,?,?,0,'guard_exhausted',?)",
                 (cid, "gift", "child", "", NOW_CHILD - 60))

    def no_respeak(*a, **k):
        raise AssertionError("重放不许再 speak")
    monkeypatch.setattr(child_mod, "child_speak", no_respeak)
    assert events.maybe_daily_event(conn, cid, random.Random(1), now=NOW_CHILD,
                                    brain=brain) == "stone"
    date = time.strftime("%Y-%m-%d", time.localtime(NOW_CHILD))
    row = conn.execute("SELECT note FROM growth_album WHERE item_kind=?",
                       (f"gift_stone_{date}",)).fetchone()
    assert row is not None and row["note"] is None


def test_gift_real_album_insert_failure_rolls_back(conn, kid, monkeypatch):
    """真实 album INSERT 失败(SQLite trigger RAISE ABORT):事务回滚干净,
    退化普通事件照发,连接仍可写(评审)。"""
    cid, brain = kid
    _pin_gift_pool(monkeypatch)
    conn.execute("CREATE TRIGGER gift_block BEFORE INSERT ON growth_album"
                 " WHEN NEW.item_kind LIKE 'gift_%'"
                 " BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    monkeypatch.setattr(child_mod, "child_speak",
                        lambda *a, **k: SpeakResult(
                            text="给爸爸的", retries=0, max_overlap=0,
                            accepted=True, stage="child", params={}))
    assert events.maybe_daily_event(conn, cid, random.Random(1), now=NOW_CHILD,
                                    brain=brain) == "stone"
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                        " 'gift_%'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE"
                        " idempotency_key LIKE 'daily:%'").fetchone()[0] == 1
    conn.execute("DROP TRIGGER gift_block")
    # 连接没被半截事务卡死,后续照常可写
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="post-crash", now=NOW_CHILD + 60)


def test_gift_card_write_failure_degrades_to_plain_event(conn, kid, monkeypatch):
    """藏品卡写不动=退化成普通每日事件,绝不炸 tick(评审定案)。"""
    cid, brain = kid
    _pin_gift_pool(monkeypatch)
    real_emit = events._emit
    calls = []

    def flaky_emit(*a, **kw):
        calls.append(kw.get("item_kind"))
        if kw.get("item_kind") is not None:
            raise RuntimeError("album disk full")
        return real_emit(*a, **kw)
    monkeypatch.setattr(events, "_emit", flaky_emit)
    assert events.maybe_daily_event(conn, cid, random.Random(1),
                                    now=NOW_CHILD) == "stone"
    assert calls[0] is not None and calls[1] is None   # 先试卡,再退化
    # 事件发出去了,卡确实没有
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE"
                        " idempotency_key LIKE 'daily:%'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind LIKE"
                        " 'gift_%'").fetchone()[0] == 0


# ── tick 面:fail-open ──

def test_tick_magic_fail_open_without_archive(conn, kid, monkeypatch):
    """archive env 没配/打不开:tick_magic 静默返回,绝不上抛。"""
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.setattr(magic, "TIMETRAVEL_DAY_P", 1.0)
    out = magic.tick_magic(conn, brain, cid, now=NOW_CHILD)
    assert "timetravel" not in out   # 打不开=本轮没这一出,但没炸


def test_tick_magic_single_failure_isolated(conn, kid, monkeypatch):
    """单件炸了不连坐:mistranslate 桩炸,story 照常静默,整体不上抛。"""
    cid, brain = kid
    _feed_archive(conn, brain, cid)
    monkeypatch.setattr(magic, "MISTRANSLATE_DAY_P", 1.0)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("nursery.decoder.speak", boom)
    out = magic.tick_magic(conn, brain, cid, now=NOW_CHILD)
    assert "mistranslate" not in out
