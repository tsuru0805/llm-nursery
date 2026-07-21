# -*- coding: utf-8 -*-
"""孩子引擎三层+生命周期+喂语料 测试。全部临时 db,假时钟注入。"""
import json
import random

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery.config import STAGE_DECODE_V1
from nursery.decoder import speak
from nursery.guard import OverlapGuard, scrub_pii
from nursery.model import VariableOrderMarkov

T0 = 1_800_000_000.0  # 假时钟起点
DAY = 86400.0

CORPUS = """睡吧,睡吧,午睡着了就不哭了。
乖,不哭不哭,爸爸在。
喝奶奶了,慢一点,不着急。
爸爸在家,妈妈在家,你也在家,我们三个都有太阳。
从前有一只小兔子,它住在森林里,森林里有很多果果。
月亮出来了,星星也出来了,该睡觉了。
把果果分给妈妈一半,分享是好孩子。
不怕不怕,打雷是天上在敲鼓,爸爸在。"""


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_nursery.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    """一个已出生的孩子+已喂基础语料的大脑。"""
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=42, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed-corpus", now=T0 + 60)
    return cid, brain


# ── db 纪律 ──

def test_db_path_required():
    with pytest.raises(ValueError):
        pdb.connect("")


def test_db_v1_migrates_to_latest(tmp_path):
    """旧 v1 库(无 v2 expires_at/v3 darkness 等列)连接时逐版幂等迁移到最新。"""
    import re
    import sqlite3 as _sq
    p = str(tmp_path / "v1.db")
    raw = _sq.connect(p)
    v1_schema = pdb._SCHEMA
    for pat in (r"^\s*expires_at REAL,.*\n", r"^\s*darkness\s+REAL.*\n",
                r"^\s*digest_load REAL.*\n", r"^\s*scene\s+TEXT,.*\n",
                r"^\s*celebrated_stage TEXT,.*\n", r"^\s*runaway_at\s+REAL,.*\n",
                r"^\s*ending\s+TEXT,.*\n", r"^\s*appearance\s+TEXT,.*\n"):
        before = v1_schema
        v1_schema = re.sub(pat, "", v1_schema, flags=re.M)
        assert v1_schema != before, f"v1 造库没删掉列行: {pat}"
    raw.executescript(v1_schema)
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()
    conn = pdb.connect(p)   # 触发迁移
    assert "expires_at" in {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "darkness" in {r[1] for r in conn.execute("PRAGMA table_info(child_state)")}
    child_cols = {r[1] for r in conn.execute("PRAGMA table_info(child)")}
    assert {"celebrated_stage", "runaway_at", "ending"} <= child_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    conn.close()
    conn2 = pdb.connect(p)  # 再连=幂等,不重复 ALTER
    conn2.close()


# ── 模型/快照 ──

def test_model_snapshot_roundtrip():
    m = VariableOrderMarkov()
    m.feed(CORPUS)
    blob = m.to_blob()
    m2 = VariableOrderMarkov.from_blob(blob)
    assert m2.total_chars == m.total_chars
    assert m2.counts[1]["爸"] == m.counts[1]["爸"]
    assert VariableOrderMarkov.checksum(blob) == VariableOrderMarkov.checksum(m2.to_blob())


def test_model_feed_weight():
    m = VariableOrderMarkov()
    m.feed("爸爸", weight=3)
    assert m.counts[1]["爸"]["爸"] == 3


# ── 护栏/PII ──

def test_guard_blocks_verbatim():
    g = OverlapGuard()
    g.add_source(CORPUS)
    ok, ov = g.check("爸爸在家,妈妈在家,你也在家", 10)
    assert not ok and ov >= 10


def test_guard_allows_recombined():
    g = OverlapGuard()
    g.add_source(CORPUS)
    ok, ov = g.check("果果在森林,爸爸睡着了", 10)
    assert ok and ov < 10


def test_scrub_pii():
    text = "打电话 08012345678 邮箱 a@b.com 密钥 sk-abcdef123456789 好呀"
    clean, flags = scrub_pii(text)
    assert "08012345678" not in clean and "a@b.com" not in clean
    assert "sk-abcdef123456789" not in clean
    assert set(flags) >= {"phone", "email", "token"}
    assert "好呀" in clean  # 正常内容不动


# ── 阶段推导(绝对时间) ──

def test_stage_progression(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", now=T0)
    c = child_mod.get_child(conn, cid)
    assert child_mod.stage_of(c, T0 + 1 * DAY) == "infant"
    assert child_mod.stage_of(c, T0 + 5 * DAY) == "toddler"
    assert child_mod.stage_of(c, T0 + 15 * DAY) == "child"
    assert child_mod.stage_of(c, T0 + 30 * DAY) == "teen"
    assert child_mod.stage_of(c, T0 + 40 * DAY) == "adult"


def test_embryo_placeholder(conn):
    cid = child_mod.create_child(conn, "mama", status="embryo", now=T0)
    c = child_mod.get_child(conn, cid)
    assert c["name"] is None and c["born_at"] is None
    assert child_mod.stage_of(c, T0 + 100 * DAY) == "embryo"  # 不孵化不长大
    brain = child_mod.ChildBrain.load(conn, cid)
    with pytest.raises(ValueError):
        child_mod.child_speak(conn, brain, cid, now=T0)
    with pytest.raises(KeyError):
        child_mod.read_state(conn, cid, now=T0)  # embryo 无状态行


# ── 状态机 ──

def test_settle_decay(born, conn):
    cid, _ = born
    s1 = child_mod.read_state(conn, cid, now=T0 + 120)
    s2 = child_mod.read_state(conn, cid, now=T0 + 120 + 24 * 3600, persist=False)
    assert s2["nutrition"] < s1["nutrition"]  # 一天不喂,饿


def test_action_idempotent(born, conn):
    cid, _ = born
    a1 = child_mod.apply_action(conn, cid, "papa", "soothe",
                                idempotency_key="soothe-1", now=T0 + 200)
    a2 = child_mod.apply_action(conn, cid, "papa", "soothe",
                                idempotency_key="soothe-1", now=T0 + 400)
    assert a1 == a2  # 重放不重复生效
    n = conn.execute("SELECT COUNT(*) FROM action_log WHERE child_id=? AND kind='soothe'",
                     (cid,)).fetchone()[0]
    assert n == 1


# ── 喂语料 ──

def test_feed_dedup(born, conn):
    cid, brain = born
    r = child_mod.feed_corpus(conn, brain, cid, CORPUS, now=T0 + 300)
    assert r["duplicate"] is True and r["fed"] == 0


def test_feed_diversity_nutrition(born, conn):
    cid, brain = born
    r1 = child_mod.feed_corpus(conn, brain, cid, "今天有海和灯塔还有远方的船。", now=T0 + 300)
    r2 = child_mod.feed_corpus(conn, brain, cid, "今天有海和灯塔还有远方的船呀。", now=T0 + 400)
    assert r1["nutrition_delta"] > r2["nutrition_delta"]  # 新字多→更营养


def test_feed_scrubs_pii_before_storage(born, conn):
    cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "记住这个号码 090987654321 哦", now=T0 + 500)
    stored = conn.execute(
        "SELECT text, privacy_flags FROM corpus_item WHERE child_id=? ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()
    assert "090987654321" not in stored["text"]
    assert "phone" in json.loads(stored["privacy_flags"])


# ── 说话 ──

def test_speak_stages_shape(born, conn):
    cid, brain = born
    r = child_mod.child_speak(conn, brain, cid, trigger="test", now=T0 + 3600)
    p = STAGE_DECODE_V1["infant"]
    assert r.stage == "infant"
    if r.accepted:
        assert 1 <= len(r.text) <= p["max_len"] * 2  # 叠词可超 max_len 一倍以内
    row = conn.execute("SELECT * FROM utterance WHERE child_id=?", (cid,)).fetchone()
    assert row is not None and row["stage"] == "infant"


def test_speak_rng_persisted(born, conn):
    cid, brain = born
    r1 = child_mod.child_speak(conn, brain, cid, now=T0 + 3600)
    state1 = child_mod.get_child(conn, cid)["rng_state"]
    r2 = child_mod.child_speak(conn, brain, cid, now=T0 + 3700)
    state2 = child_mod.get_child(conn, cid)["rng_state"]
    assert state1 is not None and state1 != state2  # RNG 前进且持久化


def test_speak_guard_never_verbatim(born, conn):
    """童年期高阶采样也不许整段背语料(护栏兜底)。"""
    cid, brain = born
    rng = random.Random(7)
    for _ in range(20):
        res = speak(brain.model, brain.guard, "child", rng)
        if res.accepted:
            assert res.max_overlap < STAGE_DECODE_V1["child"]["overlap_limit"]


# ── 快照断点续训 ──

def test_brain_reload_from_snapshot(born, conn):
    cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "新的一句话,关于风和树叶。", now=T0 + 600)
    brain2 = child_mod.ChildBrain.load(conn, cid)
    assert brain2.trained_through == brain.trained_through
    assert brain2.model.total_chars == brain.model.total_chars


def test_brain_recovers_from_corrupt_snapshot(born, conn):
    cid, brain = born
    conn.execute("UPDATE model_snapshot SET checksum='bad' WHERE child_id=?", (cid,))
    brain2 = child_mod.ChildBrain.load(conn, cid)  # 坏快照→从零重放语料
    assert brain2.model.total_chars > 0
    assert brain2.trained_through == brain.trained_through


def test_brain_falls_back_to_older_valid_snapshot(born, conn):
    """最新快照坏、旧快照好 → 用旧快照+游标后重放。"""
    cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "又一句新话,风吹过山谷。", now=T0 + 700)
    latest = conn.execute(
        "SELECT id FROM model_snapshot WHERE child_id=? ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()["id"]
    conn.execute("UPDATE model_snapshot SET checksum='corrupt' WHERE id=?", (latest,))
    brain2 = child_mod.ChildBrain.load(conn, cid)
    assert brain2.snapshot_id is not None and brain2.snapshot_id != latest  # 用了旧的
    assert brain2.trained_through == brain.trained_through  # 余量重放补齐
    assert brain2.model.total_chars == brain.model.total_chars


# ── 补充:事务原子性/串训/竞态/半群/空模型 ──

def test_feed_atomic_on_failure(born, conn, monkeypatch):
    """快照阶段炸 → corpus/action 零残留,brain 标 stale 且重载后干净。"""
    cid, brain = born
    n_corpus = conn.execute("SELECT COUNT(*) FROM corpus_item").fetchone()[0]
    n_action = conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0]
    monkeypatch.setattr(child_mod.ChildBrain, "_save_snapshot_locked",
                        lambda self, c, t: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        child_mod.feed_corpus(conn, brain, cid, "这句不该留下任何痕迹。", now=T0 + 800)
    assert conn.execute("SELECT COUNT(*) FROM corpus_item").fetchone()[0] == n_corpus
    assert conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0] == n_action
    assert brain.stale is True
    monkeypatch.undo()
    r = child_mod.feed_corpus(conn, brain, cid, "这句会正常留下。", now=T0 + 900)
    assert r["fed"] > 0 and brain.stale is False  # stale 自动重载后继续可用


def test_feed_rejects_wrong_child(born, conn):
    cid, brain = born
    other = child_mod.create_child(conn, "papa", name="别家孩子", now=T0)
    with pytest.raises(ValueError):
        child_mod.feed_corpus(conn, brain, other, "串训禁止", now=T0 + 100)


def test_stale_brain_catches_up(born, conn):
    """两个 brain 实例:A 喂过之后 B 再喂,B 锁内 catch-up 不跳号。"""
    cid, brain_a = born
    brain_b = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain_a, cid, "A 先喂的一句,含新词琥珀。", now=T0 + 1000)
    child_mod.feed_corpus(conn, brain_b, cid, "B 后喂的一句,含新词砂糖。", now=T0 + 1100)
    fresh = child_mod.ChildBrain.load(conn, cid)
    assert brain_b.trained_through == fresh.trained_through
    assert brain_b.model.total_chars == fresh.model.total_chars  # B 没漏 A 的语料


def test_embryo_feed_rejected(conn):
    cid = child_mod.create_child(conn, "mama", status="embryo", now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    with pytest.raises(ValueError):
        child_mod.feed_corpus(conn, brain, cid, "还不能喂", now=T0)
    assert conn.execute("SELECT COUNT(*) FROM corpus_item WHERE child_id=?",
                        (cid,)).fetchone()[0] == 0  # 零残留


def test_settle_segmented_consistency():
    """48h 一次结算 ≈ 24h×2 分段结算(固定步长半群性)。"""
    s0 = dict(mood=90.0, health=80.0, intimacy=50.0, nutrition=40.0, fatigue=60.0)
    once = child_mod.settle_state(s0, 48.0)
    twice = child_mod.settle_state(child_mod.settle_state(s0, 24.0), 24.0)
    for k in s0:
        assert abs(once[k] - twice[k]) < 1.0, (k, once[k], twice[k])


def test_speak_empty_model_bounded(conn):
    """出生未喂语料就说话:有界返回兜底,不死循环。"""
    cid = child_mod.create_child(conn, "papa", name="孩子", now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    r = child_mod.child_speak(conn, brain, cid, now=T0 + 10)
    assert r.accepted is False and r.text  # 兜底话


# ── 补充:护栏边界/PII 误伤 ──

def test_guard_short_source_verbatim():
    """短源行(<6字)整句照抄也要被算重合。"""
    g = OverlapGuard()
    g.add_source("要抱抱\n睡吧睡吧,午睡着了就不哭了")
    ok, ov = g.check("要抱抱", 3)
    assert not ok and ov == 3


def test_guard_shingle_ratio_layer():
    """4-gram 重合率层(SSOT 第三层)拦"长文改字近逐字背诵";低密重组放行=特性。"""
    long_line = "睡吧睡吧午睡着了就不哭了爸爸一直都在这里月亮出来了星星也出来了该睡觉了好孩子"
    g = OverlapGuard()
    g.add_source(long_line)
    # 40 字原文只改中间 1 字(LCS 断成两段各 <20,若 limit=20 则 LCS 层放行)→ ratio 层拦
    mutated = long_line[:18] + "喵" + long_line[19:]
    ok, _ = g.check(mutated, 20)
    assert not ok
    # 两段短拼接(重组,找不到原句)放行=特性
    g2 = OverlapGuard()
    g2.add_source("今天月亮很亮很圆\n照着我们午的小脸睡觉")
    ok2, _ = g2.check("月亮呀在山那边睡觉了", 10)
    assert ok2


def test_scrub_pii_date_not_mangled():
    clean, flags = scrub_pii("2026-07-17 我们去了公园,2026年5月20日 取的名字")
    assert "2026-07-17" in clean and "2026年5月20日" in clean
    assert "phone" not in flags and "longnum" not in flags


def test_scrub_pii_cjk_adjacent_token():
    clean, flags = scrub_pii("密钥是sk-abcdef12345678可别说出去")
    assert "sk-abcdef12345678" not in clean and "token" in flags


# ── 补充:嵌套禁用/护栏 catch-up/speak 新鲜度 ──

def test_public_entrypoints_reject_outer_transaction(born, conn):
    """公开写入口在外部事务内调用=用错了,直接 raise(嵌套语义从根关掉)。"""
    cid, brain = born
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError):
            child_mod.apply_action(conn, cid, "papa", "soothe",
                                   idempotency_key="in-tx", now=T0 + 50)
        with pytest.raises(RuntimeError):
            child_mod.feed_corpus(conn, brain, cid, "外部事务里不许喂", now=T0 + 50)
        with pytest.raises(RuntimeError):
            child_mod.child_speak(conn, brain, cid, now=T0 + 50)
    finally:
        conn.rollback()
    brain.stale = False  # 上面 feed/speak 失败会标 stale,清掉不影响后续夹具


def test_catchup_updates_guard(born, conn):
    """B brain catch-up 后,护栏必须认识 A 新喂的原文。"""
    cid, brain_a = born
    brain_b = child_mod.ChildBrain.load(conn, cid)
    secret = "这句只有A喂过的悄悄话足够长了吧"
    child_mod.feed_corpus(conn, brain_a, cid, secret, now=T0 + 1000)
    # B 喂任意一句触发锁内 catch-up
    child_mod.feed_corpus(conn, brain_b, cid, "B 自己的一句话。", now=T0 + 1100)
    ok, ov = brain_b.guard.check(secret, 10)
    assert not ok and ov == len(secret)


def test_speak_catches_up_before_talking(born, conn):
    """旧 brain 直接 speak:锁内 catch-up,不用旧模型旧护栏说话;
    且 utterance 留痕指向 catch-up 后的真实快照。"""
    cid, brain_a = born
    brain_b = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain_a, cid, "新鲜出炉的一句琥珀色的话。", now=T0 + 1000)
    before = brain_b.trained_through
    child_mod.child_speak(conn, brain_b, cid, now=T0 + 1200)
    assert brain_b.trained_through > before  # speak 路径补齐了游标
    utt = conn.execute(
        "SELECT model_snapshot_id FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    snap = conn.execute("SELECT trained_through_corpus_id FROM model_snapshot WHERE id=?",
                        (utt["model_snapshot_id"],)).fetchone()
    assert snap["trained_through_corpus_id"] == brain_b.trained_through


def test_speak_after_stale_snapshot_load_traceable(born, conn):
    """load 走旧快照+重放(增量为0的 speak 路径)也要落新快照留痕。"""
    cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "再喂一句撑出第二个快照。", now=T0 + 700)
    latest = conn.execute(
        "SELECT id FROM model_snapshot WHERE child_id=? ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()["id"]
    conn.execute("UPDATE model_snapshot SET checksum='corrupt' WHERE id=?", (latest,))
    brain2 = child_mod.ChildBrain.load(conn, cid)  # 旧快照+重放,游标>快照游标
    assert brain2.trained_through != brain2.snapshot_cursor
    child_mod.child_speak(conn, brain2, cid, now=T0 + 800)
    utt = conn.execute(
        "SELECT model_snapshot_id FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    snap = conn.execute("SELECT trained_through_corpus_id FROM model_snapshot WHERE id=?",
                        (utt["model_snapshot_id"],)).fetchone()
    assert snap["trained_through_corpus_id"] == brain2.trained_through
