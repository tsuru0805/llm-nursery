# -*- coding: utf-8 -*-
"""命名双模式:提前设名 / 出生后人机一起定名(name 指令)。"""
import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver, texts

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture()
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    return tmp_path / "saves"


@pytest.fixture()
def unnamed(saves):
    """无名出生(模式②入口)。"""
    driver.init_birth("papa", None, now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    yield conn, cid, brain
    conn.close()


def test_birth_with_preset_name_shows_named_opening(saves):
    """模式①:提前想好名字——出生输出机读行+具名开场文案。"""
    out = driver.init_birth("papa", "小小", now=T0)
    assert out.startswith("born:")
    assert texts.OPENING_NAMED_LINE.format(name="小小") in out


def test_birth_unnamed_opening_and_status_prompt(unnamed):
    """模式②:无名出生合法;status/help 都提示可以 name。"""
    conn, cid, brain = unnamed
    st = driver.run("papa", ["status"], now=T0 + 60)
    assert texts.NAME_PROMPT_LINE in st
    hp = driver.run("papa", ["help"], now=T0 + 60)
    assert "name" in hp


def test_name_bare_shows_rules_with_babble(unnamed):
    """空跑 name:零语料=瞎抓提示;喂过话=给出他常咿呀的音。"""
    conn, cid, brain = unnamed
    out = driver.run("papa", ["name"], now=T0 + 60)
    assert texts.NAME_NO_BABBLE_LINE in out
    child_mod.feed_corpus(conn, brain, cid, "星星月亮都出来了,星星在眨眼。",
                          now=T0 + 100)
    out2 = driver.run("papa", ["name"], now=T0 + 200)
    assert texts.NAME_NO_BABBLE_LINE not in out2
    assert "也许是个线索" in out2   # NAME_BABBLE_LINE 已带上他的音


def test_name_single_candidate_is_human_decision(unnamed):
    """单候选=人说了算,直接定;定名落 child.name+相册纪念,一生一次。"""
    conn, cid, brain = unnamed
    out = driver.run("papa", ["name", "小豆"], now=T0 + 300)
    assert texts.NAME_PICKED_SOLO.format(name="小豆") in out
    conn2 = pdb.connect(driver._db_path("papa"))
    c = conn2.execute("SELECT name FROM child").fetchone()
    assert c["name"] == "小豆"
    album = conn2.execute("SELECT title FROM growth_album WHERE item_kind='named'"
                          ).fetchone()
    assert album is not None and "小豆" in album["title"]
    out2 = driver.run("papa", ["name", "改名"], now=T0 + 400)
    assert texts.NAME_ALREADY.format(name="小豆") in out2
    conn2.close()


def test_name_multi_candidates_child_prefers_familiar(unnamed):
    """多候选=他自己挑:听过的字权重高;选择确定性可重放(同候选同结果)。"""
    conn, cid, brain = unnamed
    for i in range(3):
        child_mod.feed_corpus(conn, brain, cid, f"星星真好看呀,星星陪着你({i})。",
                              now=T0 + 100 + i)
    picked = child_mod.pick_name(conn, brain, cid, ["星星", "翙翙"], now=T0 + 500)
    assert picked["weights"]["星星"] > picked["weights"]["翙翙"]  # 熟悉度进权重
    assert picked["name"] in ("星星", "翙翙")
    again = child_mod.pick_name(conn, brain, cid, ["再来"], now=T0 + 600)
    assert again["already"] and again["name"] == picked["name"]  # 一生一次,重试拿既有结果


def test_name_too_long_rejected(unnamed):
    conn, cid, brain = unnamed
    out = driver.run("papa", ["name", "一个特别特别长的名字"], now=T0 + 60)
    assert out == texts.NAME_TOO_LONG.format(max_len=driver.MAX_NAME_LEN)


def test_name_concurrent_only_first_wins(unnamed):
    """两连接抢命名:锁内重读,只有先者生效;后者拿 already,named 纪念只一条。"""
    conn, cid, brain = unnamed
    conn2 = pdb.connect(driver._db_path("papa"))
    brain2 = child_mod.ChildBrain.load(conn2, cid)
    r2 = child_mod.pick_name(conn2, brain2, cid, ["先手"], now=T0 + 100)
    assert r2["name"] == "先手" and not r2["already"]
    r1 = child_mod.pick_name(conn, brain, cid, ["后手"], now=T0 + 200)  # 旧视图连接
    assert r1["already"] and r1["name"] == "先手"
    n = conn2.execute("SELECT COUNT(*) FROM growth_album WHERE item_kind='named'"
                      ).fetchone()[0]
    assert n == 1
    conn2.close()


def test_preset_name_gets_album_keepsake(saves):
    """模式①(出生带名)同样落 named 纪念——两条路都值得被记住。"""
    driver.init_birth("papa", "小小", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    row = conn.execute("SELECT title, note FROM growth_album WHERE item_kind='named'"
                       ).fetchone()
    assert row is not None and "小小" in row["title"]
    assert row["note"] == texts.MS_NAMED_NOTE_PRESET
    conn.close()


def test_pick_name_rejected_for_non_active(unnamed):
    """终态门禁与 driver 语义一致:非 active 不可命名(公开 API 同拒)。"""
    conn, cid, brain = unnamed
    conn.execute("UPDATE child SET status='runaway', runaway_at=? WHERE child_id=?",
                 (T0, cid))
    with pytest.raises(ValueError):
        child_mod.pick_name(conn, brain, cid, ["小豆"], now=T0 + 100)


def test_name_commit_not_masked_by_speak_failure(unnamed, monkeypatch):
    """定名已提交后,附加的回话失败=fail-open,不掩盖定名成功。"""
    conn, cid, brain = unnamed

    def boom(*a, **k):
        raise RuntimeError("speak-fail")

    monkeypatch.setattr(child_mod, "child_speak", boom)
    out = driver.run("papa", ["name", "小豆"], now=T0 + 100)
    assert texts.NAME_PICKED_SOLO.format(name="小豆") in out
