# -*- coding: utf-8 -*-
"""幼儿期语言质感(家庭词块/场景标签/睡眠整理/首次记录)测试。

全部临时 db+假时钟;RULES_V2 钉 inf 隔离刀15 数值面(场景/词块不受 v2 闸,
是阶段闸:infant 词块概率=0)。夜窗判定用 time.mktime 构造本地时刻。
"""
import json
import random
import time

import pytest

from nursery import child as child_mod
from nursery import chunks as chunks_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import events

T0 = 1_800_000_000.0
DAY = 86400.0


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _legacy_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "chunks.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    cid = child_mod.create_child(conn, "papa", name="囡", seed=42, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    return cid, brain


def _feed(conn, brain, cid, text, t, **kw):
    return child_mod.feed_corpus(conn, brain, cid, text, actor="papa", now=t, **kw)


# ── 词块提取(纯函数) ──

def test_extract_chunks_frequency_and_absorption():
    rows = [("不要走,不要走,不要走呀", None, 1.0),
            ("抱抱,抱抱,要抱抱", None, 1.0),
            ("只出现一次的长句子在这里", None, 1.0)]
    got = {c["chunk"]: c for c in chunks_mod.extract_chunks(rows)}
    assert "不要走" in got            # 高频片段成块
    assert "抱抱" in got
    assert "不要" not in got          # 子串被"不要走"吸收
    assert "只出现一次的" not in got   # 频次不达标
    for ck in got:
        assert cfg.CHUNK_MIN_LEN <= len(ck) <= cfg.CHUNK_MAX_LEN


def test_extract_chunks_scene_aggregation():
    rows = [("睡吧睡吧,睡吧睡吧", "bedtime", 1.0),
            ("睡吧睡吧", "comfort", 1.0)]
    got = {c["chunk"]: c for c in chunks_mod.extract_chunks(rows)}
    sc = got["睡吧睡吧"]["scenes"]
    assert sc["bedtime"] > sc["comfort"]


def test_extract_chunks_cap(monkeypatch):
    monkeypatch.setattr(cfg, "CHUNK_TOP_MAX", 3)
    rows = [(f"词块{i}词块{i}词块{i}", None, 1.0) for i in range(10)]
    assert len(chunks_mod.extract_chunks(rows)) <= 3


# ── 场景标签自动派生 ──

def test_scene_derivation(conn, born):
    cid, brain = born
    noon = _local(2026, 7, 22, 12)
    night = _local(2026, 7, 23, 1)
    r1 = _feed(conn, brain, cid, "白天说的一句话", noon)
    r2 = _feed(conn, brain, cid, "教你认字的一句", noon + 60, action_kind="teach")
    r3 = _feed(conn, brain, cid, "偷听来的一句", noon + 120, source_kind="archive",
               source_ref="w:0", idempotency_key="s1")
    r4 = _feed(conn, brain, cid, "深夜低声说的一句", night)
    scenes = {r["id"]: r["scene"] for r in conn.execute(
        "SELECT id, scene FROM corpus_item WHERE child_id=?", (cid,))}
    assert scenes[r1["corpus_id"]] == "daily"
    assert scenes[r2["corpus_id"]] == "teaching"
    assert scenes[r3["corpus_id"]] == "overheard"
    assert scenes[r4["corpus_id"]] == "bedtime"


def test_scene_comfort_in_cry_window(conn, born):
    cid, brain = born
    t = _local(2026, 7, 22, 4, 50)
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at, status,"
        " payload_json, idempotency_key) VALUES(?,'night_cry',?,?,'fired',?,?)",
        (cid, t - 300, t + 3600, json.dumps({"date": "2026-07-22"}), "cw1"))
    r = _feed(conn, brain, cid, "夜哭时哄他的一句", t)
    sc = conn.execute("SELECT scene FROM corpus_item WHERE id=?",
                      (r["corpus_id"],)).fetchone()[0]
    assert sc == "comfort"


# ── 睡眠整理(重建闸) ──

def test_consolidate_bootstrap_then_daily_gate(conn, born):
    cid, brain = born
    noon = _local(2026, 7, 22, 12)
    _feed(conn, brain, cid, "抱抱,抱抱,要抱抱", noon)
    # meta 缺行=部署后首拍:立即引导重建
    n = chunks_mod.consolidate_daily(conn, cid, now=noon + 60)
    assert n is not None and n >= 1
    # 当日再拍=跳过
    assert chunks_mod.consolidate_daily(conn, cid, now=noon + 3600) is None
    # 次日 07:00 前=还在睡,跳过
    next_5am = _local(2026, 7, 23, 5)
    assert chunks_mod.consolidate_daily(conn, cid, now=next_5am) is None
    # 次日 07:00 后=重建
    next_8am = _local(2026, 7, 23, 8)
    assert chunks_mod.consolidate_daily(conn, cid, now=next_8am) is not None


def test_rebuild_is_idempotent_full_replace(conn, born):
    cid, brain = born
    noon = _local(2026, 7, 22, 12)
    _feed(conn, brain, cid, "果果,果果,吃果果", noon)
    chunks_mod.rebuild_index(conn, cid, now=noon)
    n1 = conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?",
                      (cid,)).fetchone()[0]
    chunks_mod.rebuild_index(conn, cid, now=noon + 60)
    n2 = conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?",
                      (cid,)).fetchone()[0]
    assert n1 == n2 >= 1


# ── 词块起头(decoder/child_speak 集成) ──

def _grow_to_toddler(conn, brain, cid):
    t = T0 + 5 * DAY   # 4-12 天=toddler
    for i, s in enumerate(["抱抱,抱抱,要抱抱", "不要走,不要走",
                           "乖乖睡觉觉,乖乖睡觉觉"]):
        _feed(conn, brain, cid, s, t + i * 60)
    chunks_mod.rebuild_index(conn, cid, now=t + 600)
    return t + 900


def test_child_speak_chunk_seed(conn, born, monkeypatch):
    cid, brain = born
    t = _grow_to_toddler(conn, brain, cid)
    monkeypatch.setattr(cfg, "CHUNK_SEED_P", dict(cfg.CHUNK_SEED_P, toddler=1.0))
    res = child_mod.child_speak(conn, brain, cid, now=t)
    ck = res.params.get("chunk")
    assert ck, "toddler p=1 必须整词起头"
    assert res.text.startswith(ck) or not res.accepted
    row = conn.execute(
        "SELECT generation_params_json FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    assert json.loads(row[0]).get("chunk") == ck


def test_infant_never_chunks(conn, born):
    cid, brain = born
    _feed(conn, brain, cid, "抱抱,抱抱,要抱抱", T0 + 60)
    chunks_mod.rebuild_index(conn, cid, now=T0 + 120)
    res = child_mod.child_speak(conn, brain, cid, now=T0 + 180)   # infant
    assert "chunk" not in res.params


def test_empty_index_fail_open(conn, born, monkeypatch):
    cid, brain = born
    t = T0 + 5 * DAY
    _feed(conn, brain, cid, "随便一句话垫底", t)
    monkeypatch.setattr(cfg, "CHUNK_SEED_P", dict(cfg.CHUNK_SEED_P, toddler=1.0))
    res = child_mod.child_speak(conn, brain, cid, now=t + 60)   # 没建索引
    assert "chunk" not in res.params   # 空索引=照旧说话不炸


def test_pick_chunk_scene_boost(conn, born):
    cid, brain = born
    noon = _local(2026, 7, 22, 12)
    night = _local(2026, 7, 23, 1)
    _feed(conn, brain, cid, "玩球球,玩球球,玩球球", noon)           # daily
    _feed(conn, brain, cid, "睡觉觉,睡觉觉,睡觉觉", night)          # bedtime
    chunks_mod.rebuild_index(conn, cid, now=night + 60)
    hits = 0
    for i in range(40):
        ck = chunks_mod.pick_chunk(conn, cid, random.Random(i),
                                   scene_hint=("bedtime",))
        if ck and "睡" in ck:
            hits += 1
    assert hits > 20   # 场景加权应明显占优(权重×3)


# ── 首次记录里程碑 ──

def _utter(conn, cid, text, stage="toddler", overlap=10, params=None, t=T0):
    conn.execute(
        "INSERT INTO utterance(child_id, trigger, stage, text,"
        " generation_params_json, max_source_overlap, accepted, created_at)"
        " VALUES(?,?,?,?,?,?,1,?)",
        (cid, "manual", stage, text,
         json.dumps(params or {}, ensure_ascii=False), overlap, t))


def test_milestone_first_no_and_novel(conn, born):
    cid, brain = born
    _utter(conn, cid, "不要睡觉", t=T0 + 100)
    _utter(conn, cid, "月亮抱抱去上班", overlap=2, t=T0 + 200)   # 涌现:低重合长句
    hit = events.check_milestones(conn, brain, cid, now=T0 + 300)
    assert "first_no" in hit and "first_novel" in hit
    # 幂等:再查不重发
    assert not {"first_no", "first_novel"} & set(
        events.check_milestones(conn, brain, cid, now=T0 + 400))


def test_milestone_infant_babble_not_novel(conn, born):
    cid, brain = born
    _utter(conn, cid, "果果果妈妈妈", stage="infant", overlap=1, t=T0 + 100)
    hit = events.check_milestones(conn, brain, cid, now=T0 + 200)
    assert "first_novel" not in hit and "first_own_name" not in hit


def test_milestone_first_chunk_and_name(conn, born):
    cid, brain = born
    _utter(conn, cid, "抱抱不要走", params={"chunk": "抱抱"}, t=T0 + 100)
    _utter(conn, cid, "囡要果果", t=T0 + 200)
    hit = events.check_milestones(conn, brain, cid, now=T0 + 300)
    assert "first_chunk" in hit and "first_own_name" in hit


# ── 评审回归

def test_chunk_respects_vocab_unlock():
    """词块含未解锁字符=本次不整词(seed 不绕 vocab_ratio 闸)。"""
    from nursery.decoder import speak
    from nursery.guard import OverlapGuard
    from nursery.model import VariableOrderMarkov
    m = VariableOrderMarkov(2)
    m.feed("抱抱,抱抱,抱抱,乖乖,乖乖,乖乖")
    res = speak(m, OverlapGuard(), "toddler", random.Random(5), chunk="生僻")
    assert "chunk" not in res.params
    assert not res.text.startswith("生僻")
    # 全在解锁集的词块照常起头
    res2 = speak(m, OverlapGuard(), "toddler", random.Random(5), chunk="抱抱")
    assert res2.params.get("chunk") == "抱抱"


def test_rebuild_rejects_external_tx(conn, born):
    """rebuild 自带顶层事务纪律:外部事务内调用=直接 raise,不静默嵌套。"""
    cid, _ = born
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError):
            chunks_mod.rebuild_index(conn, cid, now=T0)
    finally:
        conn.rollback()


def test_milestone_name_wildcard_not_glob(conn):
    """名字含 %/_ 不作通配符:普通话语不误触发,字面命中才算。"""
    cid = child_mod.create_child(conn, "papa", name="%", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    _utter(conn, cid, "普通的一句话", t=T0 + 100)
    hit = events.check_milestones(conn, brain, cid, now=T0 + 200)
    assert "first_own_name" not in hit
    _utter(conn, cid, "他说了一个%字", t=T0 + 300)
    hit = events.check_milestones(conn, brain, cid, now=T0 + 400)
    assert "first_own_name" in hit


def test_milestone_chunk_key_not_value(conn, born):
    """params 值里出现 'chunk' 字样不算;真 chunk 键才算(阻断3)。"""
    cid, brain = born
    _utter(conn, cid, "带干扰值的一句", params={"anchors": ["chunk"]}, t=T0 + 100)
    hit = events.check_milestones(conn, brain, cid, now=T0 + 200)
    assert "first_chunk" not in hit


# ── v6→v7 迁移 ──

def test_db_v6_migrates_to_v7(tmp_path):
    import re
    import sqlite3 as _sq
    p = str(tmp_path / "v6.db")
    v6 = pdb._SCHEMA.split("CREATE TABLE IF NOT EXISTS chunk_index")[0]
    v6 = re.sub(r"^\s*scene\s+TEXT,.*\n", "", v6, flags=re.M)
    assert "chunk_index" not in v6 and "scene" not in v6
    raw = _sq.connect(p)
    raw.executescript(v6)
    raw.execute("PRAGMA user_version=6")
    raw.commit()
    raw.close()
    c = pdb.connect(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(corpus_item)")}
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scene" in cols
    assert {"chunk_index", "parenting_meta"} <= tables
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    c.close()
    pdb.connect(p).close()   # 幂等
