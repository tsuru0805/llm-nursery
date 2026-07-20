# -*- coding: utf-8 -*-
"""妈妈通道(driver mama 命令族+MAMA_ACTION_EFFECTS+actor 落账)测试。

全部 env 注入临时目录(真实 saves/ 红线),假时钟。
"""
import json
import time

import pytest

from nursery import toolface as nursery_server
from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver
from nursery.config import ACTION_EFFECTS, MAMA_ACTION_EFFECTS

DAY = 86400.0
T0 = time.mktime(time.strptime("2026-07-20", "%Y-%m-%d")) + 12 * 3600


@pytest.fixture()
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    return tmp_path / "saves"


@pytest.fixture()
def born(saves):
    out = driver.init_birth("papa", "孩子", now=T0)
    assert out.startswith("born:")
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,妈妈在。\n乖,不哭不哭。",
                          now=T0 + 60)
    conn.close()
    return cid


def _mama(argv, now):
    return json.loads(driver.run("papa", ["mama", *argv], now=now))


# ── 配置面 ──

def test_mama_effects_registered_and_disjoint():
    """mama_* 四键齐全;与爸爸动作的 kind 零重名;刻意不命中夜哭响应过滤集。"""
    assert set(MAMA_ACTION_EFFECTS) == {"mama_hug", "mama_soothe", "mama_touch",
                                        "mama_say"}
    assert not set(MAMA_ACTION_EFFECTS) & set(ACTION_EFFECTS)
    # events/scheduler 的响应过滤=('feed','soothe','diaper'),mama 键绝不冒充
    assert not set(MAMA_ACTION_EFFECTS) & {"feed", "soothe", "diaper"}


# ── 动作:抱抱/哄哄/摸摸 ──

def test_mama_hug_json_and_actor(born):
    out = _mama(["hug"], T0 + 300)
    assert out["ok"] is True and out["action"] == "hug"
    assert "\n" not in driver.run("papa", ["mama", "touch"], now=T0 + 400)  # 单行 JSON
    conn = pdb.connect(driver._db_path("papa"))
    row = conn.execute("SELECT actor, kind, payload_json FROM action_log"
                       " WHERE kind='mama_hug'").fetchone()
    assert row is not None and row["actor"] == "mama"
    p = json.loads(row["payload_json"])
    assert p["effects"]["mood"] == MAMA_ACTION_EFFECTS["mama_hug"]["mood"]
    # 每次互动孩子真实回一句(utterance 落账)
    utt = conn.execute("SELECT trigger FROM utterance ORDER BY id DESC LIMIT 2"
                       ).fetchall()
    assert {r["trigger"] for r in utt} >= {"mama_hug"}
    conn.close()


def test_mama_touch_effects_applied(born):
    conn = pdb.connect(driver._db_path("papa"))
    before = child_mod.read_state(conn, born, now=T0 + 500)
    conn.close()
    out = _mama(["touch"], T0 + 500)
    assert out["ok"] is True
    conn = pdb.connect(driver._db_path("papa"))
    after = child_mod.read_state(conn, born, now=T0 + 500, persist=False)
    eff = MAMA_ACTION_EFFECTS["mama_touch"]
    assert after["mood"] == pytest.approx(before["mood"] + eff["mood"], abs=0.2)
    assert after["intimacy"] == pytest.approx(before["intimacy"] + eff["intimacy"],
                                              abs=0.2)
    conn.close()


def test_mama_action_idempotent_replay(born):
    """同 idempotency key 重放不重复生效(照既有动作纪律)。"""
    conn = pdb.connect(driver._db_path("papa"))
    a1 = child_mod.apply_action(conn, born, "mama", "mama_soothe",
                                idempotency_key="mama-1", now=T0 + 600)
    a2 = child_mod.apply_action(conn, born, "mama", "mama_soothe",
                                idempotency_key="mama-1", now=T0 + 900)
    assert a1 == a2
    n = conn.execute("SELECT COUNT(*) FROM action_log WHERE kind='mama_soothe'"
                     ).fetchone()[0]
    assert n == 1
    conn.close()


# ── 说给他听(真进语料) ──

def test_mama_say_feeds_corpus_as_wanwan(born):
    text = "小孩子,妈妈今天想你了,乖乖长大呀。"
    out = _mama(["say", text], T0 + 1000)
    assert out["ok"] is True and out["action"] == "say" and out["duplicate"] is False
    assert out["fed"] == len(text)
    conn = pdb.connect(driver._db_path("papa"))
    row = conn.execute("SELECT source_kind, speaker, text FROM corpus_item"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["source_kind"] == "direct" and row["speaker"] == "mama"
    assert row["text"] == text  # 原文入库(「妈妈声部」读口的数据源)
    # 动作账=mama_say(actor=mama);绝不落 kind='feed'——夜哭响应过滤
    # kind IN ('feed','soothe','diaper') 不许被妈妈的话冒充成爸爸起夜
    kinds = {r["kind"]: r["actor"] for r in conn.execute(
        "SELECT kind, actor FROM action_log WHERE actor='mama'")}
    assert kinds.get("mama_say") == "mama" and "feed" not in kinds
    # 营养走喂语料管线(多样性口径>0)
    p = json.loads(conn.execute(
        "SELECT payload_json FROM action_log WHERE kind='mama_say'"
        ).fetchone()["payload_json"])
    assert p["effects"]["nutrition"] > 0
    conn.close()


def test_mama_say_trains_model(born):
    """她的话真训练:模型语料量增长,孩子之后能吐出她教的字。"""
    conn = pdb.connect(driver._db_path("papa"))
    chars_before = child_mod.ChildBrain.load(conn, born).model.total_chars
    conn.close()
    _mama(["say", "月亮圆圆,照着我们家的小晶体。"], T0 + 1100)
    conn = pdb.connect(driver._db_path("papa"))
    brain = child_mod.ChildBrain.load(conn, born)
    assert brain.model.total_chars > chars_before
    assert brain.guard.max_overlap("照着我们家的小晶体") >= 8  # 护栏也认识她的原文
    conn.close()


def test_mama_say_duplicate(born):
    _mama(["say", "重复的一句话呀。"], T0 + 1200)
    out = _mama(["say", "重复的一句话呀。"], T0 + 1300)
    assert out["ok"] is True and out["duplicate"] is True


def test_status_shows_mama_words_to_papa(born):
    """status 给主照护人看妈妈的原话——整行精确断言锁住
    「只取最新两句+新在前」与全部异源排除。没说过则整行不出。"""
    before = driver.run("papa", ["status"], now=T0 + 900)
    assert "妈妈对他说过" not in before  # born 夹具那句 speaker=None,不算妈妈的
    _mama(["say", "第一句会被挤出去。"], T0 + 950)
    _mama(["say", "小孩子,妈妈回来啦。"], T0 + 1000)
    _mama(["say", "今天也要乖乖的哦。"], T0 + 1100)
    driver.run("papa", ["feed", "爸爸的话不该进妈妈那行。"], now=T0 + 1150)
    # 异源锁:她署名但非 direct(夜奶偷学面)绝不进这行
    conn = pdb.connect(driver._db_path("papa"))
    brain = child_mod.ChildBrain.load(conn, born)
    child_mod.feed_corpus(conn, brain, born, "夜里偷学来的一句。",
                          source_kind="night_feed", speaker="mama", now=T0 + 1160)
    conn.close()
    out = driver.run("papa", ["status"], now=T0 + 1200)
    mama_lines = [l for l in out.splitlines() if l.startswith("妈妈对他说过:")]
    assert mama_lines == ["妈妈对他说过:「今天也要乖乖的哦。」 / 「小孩子,妈妈回来啦。」"]


def test_mama_say_empty_and_too_long(born):
    assert _mama(["say"], T0 + 1400) == {"ok": False, "error": "empty_text"}
    out = _mama(["say", "长" * (driver.MAX_MAMA_SAY_LEN + 1)], T0 + 1500)
    assert out == {"ok": False, "error": "too_long"}
    conn = pdb.connect(driver._db_path("papa"))
    n = conn.execute("SELECT COUNT(*) FROM action_log WHERE actor='mama'"
                     ).fetchone()[0]
    assert n == 0  # 拒收路径零落账
    conn.close()


# ── 状态 gate ──

def test_mama_no_child(saves):
    assert _mama(["hug"], T0) == {"ok": False, "error": "no_child"}


def test_mama_unknown_subcmd(born):
    assert _mama(["kiss"], T0 + 100)["error"] == "unknown_subcmd"
    assert _mama([], T0 + 100)["error"] == "unknown_subcmd"


def test_mama_runaway_and_graduated(born):
    conn = pdb.connect(driver._db_path("papa"))
    with child_mod.tx(conn):
        conn.execute("UPDATE child SET status='runaway', runaway_at=?", (T0,))
    conn.close()
    assert _mama(["hug"], T0 + 100) == {"ok": False, "error": "runaway"}
    conn = pdb.connect(driver._db_path("papa"))
    with child_mod.tx(conn):
        conn.execute("UPDATE child SET status='graduated', runaway_at=NULL")
    conn.close()
    assert _mama(["soothe"], T0 + 200) == {"ok": False, "error": "graduated"}


def test_mama_action_persists_even_if_speak_fails(born, monkeypatch):
    """回话失败注入:动作账已提交(各自顶层事务),整口
    向上报错(接入层侧=500);重试=新毫秒键第二次落账——与爸爸的动作路径同纪律。"""
    def boom(*a, **k):
        raise RuntimeError("speak boom")
    monkeypatch.setattr(child_mod, "child_speak", boom)
    with pytest.raises(RuntimeError):
        driver.run("papa", ["mama", "hug"], now=T0 + 3000)
    conn = pdb.connect(driver._db_path("papa"))
    n = conn.execute("SELECT COUNT(*) FROM action_log WHERE kind='mama_hug'"
                     ).fetchone()[0]
    assert n == 1  # 拥抱不回滚:他被抱过了,只是没来得及吭声
    conn.close()


# ── 主照护人侧防冒充 ──

def test_mama_not_reachable_from_nursery(born):
    """接入面白名单无 mama:主照护人不能冒充妈妈(硬边界)。"""
    assert "mama" not in nursery_server._PUBLIC_CMDS
    out = nursery_server.nursery("papa", "mama hug")
    assert "没有这个指令" in out
    out2 = nursery_server.nursery("papa", "'mama' say 假妈妈")  # 引号绕过防线
    assert "没有这个指令" in out2


def test_papa_dispatch_untouched(born):
    """爸爸的既有指令面回归:feed/status 文本形态不变。"""
    out = driver.run("papa", ["feed", "爸爸的一句话,晒晒太阳。"], now=T0 + 2000)
    assert "喂下去了" in out and "{" not in out.splitlines()[0]
    st = driver.run("papa", ["status"], now=T0 + 2100)
    assert "孩子 · 婴儿期" in st
