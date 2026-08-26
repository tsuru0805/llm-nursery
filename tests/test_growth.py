# -*- coding: utf-8 -*-
"""v0.3:可见成长——宝贝盒/孩子的小本子/生日 set-piece。

全部派生可见化:数据查不出=不发,不编;DS 原文/裸数值绝不出现在事件里。
"""
import json
import time

import pytest

from nursery import child as child_mod
from nursery import chunks
from nursery import config as cfg
from nursery import db as pdb
from nursery import events
from nursery import visible_growth as vg

T0 = 1_800_000_000.0
DAY = 86400.0

CORPUS = """恐龙恐龙,孩子最喜欢恐龙。
恐龙博物馆里有好多积木。
积木搭好了,积木别推倒。
果果洗好了,分你一半果果,果果甜。
抱抱,睡前要抱抱,抱抱才肯睡。"""


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_growth.db"))
    yield c
    c.close()


NOW = T0 + 6 * DAY   # toddler(4-12 天)


@pytest.fixture()
def kid(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


def _midnight(t):
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# ── 宝贝盒 ──

def test_treasure_list_derives_from_chunks(kid, conn):
    cid, _ = kid
    chunks.rebuild_index(conn, cid, now=NOW)
    ws = vg.treasure_list(conn, cid)
    assert ws and len(ws) <= cfg.TREASURE_TOP_N
    idx = {r["chunk"] for r in conn.execute(
        "SELECT chunk FROM chunk_index WHERE child_id=?", (cid,))}
    assert set(ws) <= idx   # 纯派生:宝贝全部来自词块索引


def test_treasure_card_once_per_stage(kid, conn):
    cid, _ = kid
    chunks.rebuild_index(conn, cid, now=NOW)
    out = vg.tick_growth(conn, cid, now=NOW)
    assert out.get("treasure") == "toddler"
    row = conn.execute("SELECT title, note FROM growth_album WHERE child_id=?"
                       " AND item_kind='treasure_toddler'", (cid,)).fetchone()
    assert row is not None and "宝贝" in row["title"]
    ws = vg.treasure_list(conn, cid)
    assert all(w in row["note"] for w in ws)   # 卡上的宝贝=真派生清单
    # 幂等 per stage
    assert "treasure" not in vg.tick_growth(conn, cid, now=NOW + 300)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='treasure_toddler'").fetchone()[0] == 1
    # 下一阶段再立新卡
    t_child = T0 + 14 * DAY
    out2 = vg.tick_growth(conn, cid, now=t_child)
    assert out2.get("treasure") == "child"


def test_treasure_needs_enough_chunks(kid, conn):
    cid, _ = kid   # 不重建索引=chunk_index 空
    assert "treasure" not in vg.tick_growth(conn, cid, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind LIKE 'treasure_%'").fetchone()[0] == 0


# ── 孩子的小本子 ──

def _decision(conn, cid, t, anchors=("抱抱", "恐龙"), anx="falling",
              status="ok", no_action=0):
    conn.execute(
        "INSERT INTO psyche_decision(child_id, stage, trigger, status, api_called,"
        " input_digest_json, behavior, anchor_words_json, no_action, created_at)"
        " VALUES(?,?,?,?,1,?,?,?,?,?)",
        (cid, "toddler", "tick", status,
         json.dumps({"trends": {"anxiety": anx, "independence": "flat",
                                "esteem": "flat"}}, ensure_ascii=False),
         "想找爸爸", json.dumps(list(anchors), ensure_ascii=False), no_action, t))


def test_notebook_from_decision_safe_fields(kid, conn):
    cid, _ = kid
    t21 = _midnight(NOW) + 21 * 3600 + 900
    _decision(conn, cid, t21 - 3600)
    out = vg.tick_growth(conn, cid, now=t21)
    assert out.get("notebook") is True
    date = time.strftime("%Y-%m-%d", time.localtime(t21))
    row = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                       (f"notebook:{date}:{cid}",)).fetchone()
    p = json.loads(row["payload_json"])
    assert "抱抱" in p["title"] and "恐龙" in p["title"]   # 锚词=安全字段
    assert p["note"] == "笔画松了些。"   # 方向词→旁观一笔
    blob = row["payload_json"]
    assert "想找爸爸" not in blob      # behavior/DS 原文不外露
    assert "falling" not in p["title"]  # 裸英文方向词不进文案
    # 同日至多一行
    assert "notebook" not in vg.tick_growth(conn, cid, now=t21 + 300)


def test_notebook_caps_words(kid, conn):
    cid, _ = kid
    t21 = _midnight(NOW) + 21 * 3600 + 900
    _decision(conn, cid, t21 - 60, anchors=("一", "二", "三", "四", "五"),
              anx="flat")
    vg.tick_growth(conn, cid, now=t21)
    date = time.strftime("%Y-%m-%d", time.localtime(t21))
    row = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                       (f"notebook:{date}:{cid}",)).fetchone()
    p = json.loads(row["payload_json"])
    assert p["notebook"] == ["一", "二", "三"]   # 只抄前 NOTEBOOK_MAX_WORDS 个
    assert "四" not in p["title"]
    assert p["note"] is None                     # flat=不加话


def test_notebook_needs_fresh_ok_decision(kid, conn):
    cid, _ = kid
    t21 = _midnight(NOW) + 21 * 3600 + 900
    assert "notebook" not in vg.tick_growth(conn, cid, now=t21)   # 零决策=不编
    _decision(conn, cid, t21 - (cfg.NOTEBOOK_WINDOW_H + 5) * 3600)  # 过期
    assert "notebook" not in vg.tick_growth(conn, cid, now=t21)
    _decision(conn, cid, t21 - 60, status="bad_json")             # 失败态不算
    assert "notebook" not in vg.tick_growth(conn, cid, now=t21)


def test_notebook_rejects_future_rows(kid, conn):
    """时间穿越挡住(评审定案):未来时间戳的脏行不算「新鲜」。"""
    cid, _ = kid
    t21 = _midnight(NOW) + 21 * 3600 + 900
    _decision(conn, cid, t21 + 3600, anchors=("未来",))   # created_at 在未来
    assert "notebook" not in vg.tick_growth(conn, cid, now=t21)
    # 窗内真行照常生效,且按 created_at 取最新——不被未来行盖住
    _decision(conn, cid, t21 - 60)
    assert vg.tick_growth(conn, cid, now=t21).get("notebook") is True
    date = time.strftime("%Y-%m-%d", time.localtime(t21))
    row = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                       (f"notebook:{date}:{cid}",)).fetchone()
    assert "未来" not in row["payload_json"]


def test_db_v9_migrates_to_v10_index(tmp_path):
    """v9 老库(无时间窗索引)开新版:索引补上+幂等重连(评审)。"""
    import sqlite3 as _sq
    p = str(tmp_path / "v9.db")
    ddl = ("CREATE INDEX IF NOT EXISTS idx_psyche_dec_child_time\n"
           "    ON psyche_decision(child_id, created_at, id)")
    assert ddl in pdb._SCHEMA, "v10 索引 DDL 形制变了,同步改本测试"
    v9 = pdb._SCHEMA.replace(ddl, "SELECT 1")
    assert "idx_psyche_dec_child_time" not in v9, "v9 造库没剥掉新索引"
    raw = _sq.connect(p)
    raw.executescript(v9)
    raw.execute("PRAGMA user_version=9")
    raw.commit()
    raw.close()
    c = pdb.connect(p)
    idx = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_psyche_dec_child_time" in idx
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    c.close()
    pdb.connect(p).close()   # 幂等


def test_notebook_query_uses_time_index(kid, conn):
    """小本子时间窗读口必须走 (child_id, created_at, id) 索引,
    不许 temp b-tree 全量排序(有界性,评审 二审阻断)。"""
    cid, _ = kid
    plan = " ".join(r["detail"] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT anchor_words_json FROM psyche_decision"
        " WHERE child_id=? AND status='ok' AND no_action=0"
        " AND anchor_words_json IS NOT NULL AND created_at>=? AND created_at<=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1", (cid, 0.0, NOW)))
    assert "idx_psyche_dec_child_time" in plan
    assert "TEMP B-TREE" not in plan


def test_notebook_waits_for_evening(kid, conn):
    cid, _ = kid
    t15 = _midnight(NOW) + 15 * 3600
    _decision(conn, cid, t15 - 60)
    assert "notebook" not in vg.tick_growth(conn, cid, now=t15)


# ── 生日 set-piece ──

def test_birthday_card_on_stage_transition(kid, conn):
    cid, _ = kid
    conn.execute("UPDATE child SET celebrated_stage='infant'")
    got = events.check_stage_transition(conn, cid, now=NOW)   # → toddler
    assert got == "toddler"
    row = conn.execute("SELECT title, note FROM growth_album WHERE child_id=?"
                       " AND item_kind='birthday_toddler'", (cid,)).fetchone()
    assert row is not None and "生日会" in row["title"]
    assert "家里人" in row["note"]        # 全家出席(通用稿)
    assert conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=?",
                        (f"bday:toddler:{cid}",)).fetchone() is not None
    # 幂等:重复检查不重发
    events.check_stage_transition(conn, cid, now=NOW + 300)
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind='birthday_toddler'").fetchone()[0] == 1


def test_no_birthday_for_infant(kid, conn):
    cid, _ = kid
    got = events.check_stage_transition(conn, cid, now=T0 + 3600)   # infant 庆祝
    assert got == "infant"
    assert conn.execute("SELECT COUNT(*) FROM growth_album WHERE"
                        " item_kind LIKE 'birthday_%'").fetchone()[0] == 0
