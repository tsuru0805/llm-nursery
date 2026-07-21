# -*- coding: utf-8 -*-
"""养成取舍机制 v2(消化负荷/当日递减/情境化安抚)测试。

全部临时 db+假时钟;本文件默认把 RULES_V2_SINCE 钉到 0(v2 全程生效),
v1 语义回归=各存量文件的 _legacy_rules 钉 inf。夜窗测试用 time.mktime
构造本地时刻,不依赖机器时区常量。
"""
import json
import random
import time

import pytest

from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import driver
from nursery.config import STAGE_DECODE_V1
from nursery.decoder import speak
from nursery.model import VariableOrderMarkov

T0 = 1_800_000_000.0
DAY = 86400.0
_ORIG_SINCE = cfg.RULES_V2_SINCE   # 模块导入期取原值(autouse fixture 会打补丁)

CORPUS = """睡吧,睡吧,午睡着了就不哭了。
乖,不哭不哭,爸爸在。
喝奶奶了,慢一点,不着急。
爸爸叫大山,妈妈叫小溪,你叫囡,我们一家都有名字。
月亮出来了,星星也出来了,该睡觉了。"""


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _v2_rules(monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", 0.0)


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "tradeoff.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    cid = child_mod.create_child(conn, "papa", name="囡", seed=42, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed-corpus", now=T0 + 60)
    return cid, brain


def _set_state(conn, cid, t, **kw):
    """直捣状态行(测试专用):写值并把 last_settled_at 钉到 t,免自然衰减干扰。"""
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE child_state SET {sets}, last_settled_at=?, updated_at=?"
                 " WHERE child_id=?", (*kw.values(), t, t, cid))


def _axis_rows(conn, cid, reason=None):
    q = "SELECT axis, delta, reason FROM psyche_axis_log WHERE child_id=?"
    args = [cid]
    if reason:
        q += " AND reason=?"
        args.append(reason)
    return conn.execute(q + " ORDER BY id", args).fetchall()


# ── 消化负荷:结算衰减(昼/夜速率) ──

def test_digest_decay_day_vs_night():
    base = dict(mood=60.0, health=80.0, intimacy=20.0, nutrition=50.0,
                fatigue=20.0, darkness=0.0, digest_load=50.0)
    noon = _local(2026, 7, 22, 12)     # 12:00-14:00 全白天
    s_day = child_mod.settle_state(dict(base), 2.0, start=noon)
    assert s_day["digest_load"] == pytest.approx(
        50.0 - cfg.DIGEST_DECAY_PER_H * 2, abs=1e-6)
    night = _local(2026, 7, 23, 1)     # 01:00-03:00 全夜窗
    s_night = child_mod.settle_state(dict(base), 2.0, start=night)
    assert s_night["digest_load"] == pytest.approx(
        50.0 - cfg.DIGEST_NIGHT_DECAY_PER_H * 2, abs=1e-6)
    # 起点不给=白天速率兜底(旧签名兼容)
    s_flat = child_mod.settle_state(dict(base), 2.0)
    assert s_flat["digest_load"] == s_day["digest_load"]


def test_digest_night_boundary_switches_rate():
    """22:00-00:00 跨 23 点边界:1h 白天 + 1h 夜窗。"""
    base = dict(mood=60.0, health=80.0, intimacy=20.0, nutrition=50.0,
                fatigue=20.0, darkness=0.0, digest_load=50.0)
    t22 = _local(2026, 7, 22, 22)
    s = child_mod.settle_state(dict(base), 2.0, start=t22)
    expect = 50.0 - cfg.DIGEST_DECAY_PER_H - cfg.DIGEST_NIGHT_DECAY_PER_H
    assert s["digest_load"] == pytest.approx(expect, abs=1e-6)


# ── 消化负荷:喂语料进账/过载打折 ──

def test_feed_accumulates_digest(conn, born):
    cid, brain = born
    r = child_mod.feed_corpus(conn, brain, cid, "小果果要分给妈妈吃呀",
                              actor="papa", idempotency_key="d1", now=T0 + 120)
    assert r["digest_load"] > 0
    assert not r["overloaded"]
    st = child_mod.read_state(conn, cid, now=T0 + 120, persist=False)
    assert st["digest_load"] == pytest.approx(r["digest_load"], abs=1e-6)


def test_overload_halves_absorption(conn, born, monkeypatch):
    cid, brain = born
    t = T0 + 300
    _set_state(conn, cid, t, digest_load=cfg.DIGEST_OVERLOAD_AT + 5)
    text = "森林里有一只小狐狸住在树洞里"
    r = child_mod.feed_corpus(conn, brain, cid, text, actor="papa",
                              idempotency_key="ov1", now=t)
    assert r["overloaded"]
    w = conn.execute("SELECT training_weight FROM corpus_item WHERE id=?",
                     (r["corpus_id"],)).fetchone()[0]
    assert w == pytest.approx(cfg.DIGEST_ABSORB_FACTOR)
    # 营养同乘打折:重算一遍不打折口径做对照
    rec = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key=?",
        (cid, "ov1")).fetchone()
    eff = json.loads(rec["payload_json"])["effects"]["nutrition"]
    assert eff == pytest.approx(r["nutrition_delta"], abs=1e-6)
    # 过载喂进去的语料照样入库(不丢),只是学得浅
    assert conn.execute("SELECT text FROM corpus_item WHERE id=?",
                        (r["corpus_id"],)).fetchone()[0] == text


def test_steal_does_not_accrue_digest(conn, born):
    """偷学(archive)=被动听墙角,不进消化负荷账。"""
    cid, brain = born
    before = child_mod.read_state(conn, cid, now=T0 + 200, persist=False)["digest_load"]
    child_mod.feed_corpus(conn, brain, cid, "偷听来的一句悄悄话",
                          source_kind="archive", source_ref="w1:0",
                          speaker="偷听", actor="system",
                          idempotency_key="steal-1", now=T0 + 200)
    after = child_mod.read_state(conn, cid, now=T0 + 200, persist=False)["digest_load"]
    assert after == pytest.approx(before, abs=1e-6)


def test_replay_does_not_double_digest(conn, born):
    cid, brain = born
    r1 = child_mod.feed_corpus(conn, brain, cid, "重放测试的一句话",
                               actor="papa", idempotency_key="rp1", now=T0 + 400)
    st1 = child_mod.read_state(conn, cid, now=T0 + 400, persist=False)["digest_load"]
    # 同内容再喂=corpus 去重早退,digest 不再涨
    r2 = child_mod.feed_corpus(conn, brain, cid, "重放测试的一句话",
                               actor="papa", idempotency_key="rp2", now=T0 + 401)
    assert r2["duplicate"]
    st2 = child_mod.read_state(conn, cid, now=T0 + 401, persist=False)["digest_load"]
    assert st2 == pytest.approx(st1, abs=1e-3)
    assert r1["corpus_id"] is not None


# ── 出口碎化 ──

def test_speak_overload_params(conn, born):
    cid, brain = born
    t = T0 + 500
    _set_state(conn, cid, t, digest_load=100.0)
    res = child_mod.child_speak(conn, brain, cid, now=t)
    assert res.params.get("overload") == pytest.approx(1.0)
    base = STAGE_DECODE_V1[res.stage]
    assert res.params["max_len"] < base["max_len"]
    assert res.params["temperature"] > base["temperature"]
    row = conn.execute(
        "SELECT generation_params_json FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    assert json.loads(row[0]).get("overload") == pytest.approx(1.0)


def test_speak_no_overload_below_threshold(conn, born):
    cid, brain = born
    t = T0 + 500
    _set_state(conn, cid, t, digest_load=cfg.DIGEST_OVERLOAD_AT - 1)
    res = child_mod.child_speak(conn, brain, cid, now=t)
    assert "overload" not in res.params


def test_decoder_overload_direct():
    m = VariableOrderMarkov(2)
    m.feed("爸爸抱抱,不哭不哭,乖乖睡觉觉。")
    from nursery.guard import OverlapGuard
    g = OverlapGuard()
    res = speak(m, g, "infant", random.Random(7), overload=1.0)
    assert res.params.get("overload") == pytest.approx(1.0)
    assert res.params["max_len"] >= res.params["min_len"]


# ── 当日同类递减 ──

def test_daily_repeat_decay(conn, born):
    cid, _ = born
    base_mood = cfg.ACTION_EFFECTS["play"]["mood"]
    got = []
    for i in range(3):
        child_mod.apply_action(conn, cid, "papa", "play",
                               idempotency_key=f"p{i}", now=T0 + 1000 + i * 60)
        rec = conn.execute(
            "SELECT payload_json FROM action_log WHERE child_id=? AND"
            " idempotency_key=?", (cid, f"p{i}")).fetchone()
        got.append(json.loads(rec["payload_json"]))
    assert got[0]["effects"]["mood"] == pytest.approx(base_mood)
    assert "decay_factor" not in got[0]
    assert got[1]["effects"]["mood"] == pytest.approx(base_mood * cfg.DAILY_DECAY)
    assert got[1]["decay_factor"] == pytest.approx(cfg.DAILY_DECAY)
    assert got[2]["effects"]["mood"] == pytest.approx(base_mood * cfg.DAILY_DECAY ** 2)
    # 三轴账同乘:play 的 esteem +1.5 → 第二次 ×0.75
    esteem = [r["delta"] for r in _axis_rows(conn, cid, reason="play")
              if r["axis"] == "esteem"]
    assert esteem[0] == pytest.approx(cfg.PSYCHE_RULES["play"]["esteem"])
    assert esteem[1] == pytest.approx(cfg.PSYCHE_RULES["play"]["esteem"] * cfg.DAILY_DECAY)


def test_decay_floor(conn, born):
    cid, _ = born
    for i in range(8):
        child_mod.apply_action(conn, cid, "papa", "burp",
                               idempotency_key=f"b{i}", now=T0 + 2000 + i * 30)
    rec = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='b7'",
        (cid,)).fetchone()
    assert json.loads(rec["payload_json"])["decay_factor"] == pytest.approx(
        cfg.DAILY_DECAY_FLOOR)


def test_decay_resets_next_day(conn, born):
    cid, _ = born
    child_mod.apply_action(conn, cid, "papa", "play", idempotency_key="d0", now=T0)
    child_mod.apply_action(conn, cid, "papa", "play", idempotency_key="d1",
                           now=T0 + DAY)
    rec = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='d1'",
        (cid,)).fetchone()
    p = json.loads(rec["payload_json"])
    # 假时钟隔了整整一天,本地零点必然翻篇 → 全额
    assert "decay_factor" not in p


def test_feed_exempt_from_decay(conn, born):
    """feed 走语料线(消化负荷管),不吃当日递减。"""
    cid, brain = born
    for i, txt in enumerate(["第一句新话", "第二句新话", "第三句新话"]):
        child_mod.feed_corpus(conn, brain, cid, txt, actor="papa",
                              idempotency_key=f"f{i}", now=T0 + 3000 + i * 60)
    rows = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND kind='feed'"
        " AND idempotency_key LIKE 'f%'", (cid,)).fetchall()
    assert len(rows) == 3
    for r in rows:
        assert "decay_factor" not in json.loads(r["payload_json"])


def test_discipline_never_decays(conn, born):
    cid, _ = born
    for i in range(3):
        child_mod.apply_action(conn, cid, "papa", "discipline",
                               idempotency_key=f"dc{i}", now=T0 + 4000 + i * 60)
    rows = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND kind='discipline'",
        (cid,)).fetchall()
    for r in rows:
        p = json.loads(r["payload_json"])
        assert "decay_factor" not in p
        assert p["effects"]["mood"] == pytest.approx(
            cfg.ACTION_EFFECTS["discipline"]["mood"])


# ── 夜哭窗口豁免 ──

def _open_cry_window(conn, cid, t):
    conn.execute(
        "INSERT INTO scheduled_event(child_id, kind, due_at, expires_at,"
        " status, payload_json, idempotency_key)"
        " VALUES(?, 'night_cry', ?, ?, 'fired', ?, ?)",
        (cid, t - 600, t + 3600, json.dumps({"date": "2027-01-15"}),
         f"cry:{int(t)}"))


def test_night_window_exempts_decay(conn, born):
    cid, _ = born
    t = T0 + 5000
    _open_cry_window(conn, cid, t)
    for i in range(2):
        child_mod.apply_action(conn, cid, "papa", "soothe",
                               idempotency_key=f"ns{i}", now=t + i * 60)
    rows = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND kind='soothe'",
        (cid,)).fetchall()
    for r in rows:
        p = json.loads(r["payload_json"])
        assert "decay_factor" not in p            # 窗内响应永远全额
        assert "calm_soothe" not in p             # 窗开着不算平静
        assert p["effects"]["mood"] == pytest.approx(
            cfg.ACTION_EFFECTS["soothe"]["mood"])
    # 夜哭被响应加成照记(每晚一次)
    bonus = _axis_rows(conn, cid, reason="night_cry_responded")
    assert len(bonus) == 1


# ── 情境化安抚 ──

def test_calm_soothe_dependence_account(conn, born):
    cid, _ = born
    t = T0 + 6000
    _set_state(conn, cid, t, mood=70.0)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="cs1", now=t)
    rec = json.loads(conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='cs1'",
        (cid,)).fetchone()["payload_json"])
    assert rec["calm_soothe"] is True
    anx = [r["delta"] for r in _axis_rows(conn, cid, reason="soothe")
           if r["axis"] == "anxiety"]
    assert anx[-1] == pytest.approx(
        cfg.PSYCHE_RULES["soothe"]["anxiety"] * cfg.CALM_SOOTHE_ANXIETY_FACTOR)
    dep = _axis_rows(conn, cid, reason="calm_soothe")
    assert len(dep) == 1 and dep[0]["axis"] == "independence"
    assert dep[0]["delta"] == pytest.approx(cfg.CALM_SOOTHE_INDEPENDENCE)
    # 状态效果不动情境(只走递减):mood 全额
    assert rec["effects"]["mood"] == pytest.approx(cfg.ACTION_EFFECTS["soothe"]["mood"])


def test_distressed_soothe_full_effect(conn, born):
    cid, _ = born
    t = T0 + 7000
    _set_state(conn, cid, t, mood=30.0)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="ds1", now=t)
    rec = json.loads(conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='ds1'",
        (cid,)).fetchone()["payload_json"])
    assert "calm_soothe" not in rec
    anx = [r["delta"] for r in _axis_rows(conn, cid, reason="soothe")
           if r["axis"] == "anxiety"]
    assert anx[-1] == pytest.approx(cfg.PSYCHE_RULES["soothe"]["anxiety"])


def test_mama_calm_soothe_also_counts(conn, born):
    cid, _ = born
    t = T0 + 8000
    _set_state(conn, cid, t, mood=70.0)
    child_mod.apply_action(conn, cid, "mama", "mama_hug",
                           idempotency_key="mh1", now=t)
    dep = _axis_rows(conn, cid, reason="calm_soothe")
    assert len(dep) == 1


# ── 切换时刻门:v1 语义回归 ──

def test_pre_cutover_keeps_v1(conn, born, monkeypatch):
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))
    cid, brain = born
    t = T0 + 9000
    for i in range(2):
        child_mod.apply_action(conn, cid, "papa", "play",
                               idempotency_key=f"v1p{i}", now=t + i * 60)
    rows = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND"
        " idempotency_key LIKE 'v1p%'", (cid,)).fetchall()
    for r in rows:
        p = json.loads(r["payload_json"])
        assert "decay_factor" not in p
        assert p["effects"]["mood"] == pytest.approx(cfg.ACTION_EFFECTS["play"]["mood"])
    r = child_mod.feed_corpus(conn, brain, cid, "切换前喂的一句", actor="papa",
                              idempotency_key="v1f", now=t + 300)
    assert r["digest_load"] == pytest.approx(0.0)
    assert not r["overloaded"]


def test_rules_v2_default_always_on():
    """开源默认 0=新档始终 v2;运营老档可自设未来切换时刻(config 注释)。"""
    assert _ORIG_SINCE == 0.0


# ── 老档升级 cutoff:升级前动作不追溯(评审回归)──

def test_old_save_upgrade_not_retroactive(tmp_path, monkeypatch):
    """v5 老档带同日动作 → 迁移钉 stamp → 升级后首个动作全额(不吃升级前计数)。"""
    import re
    import sqlite3 as _sq
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", 0.0)
    p = str(tmp_path / "old.db")
    v5 = pdb._SCHEMA.split("CREATE TABLE IF NOT EXISTS chunk_index")[0]
    v5 = re.sub(r"^\s*digest_load REAL.*\n", "", v5, flags=re.M)
    v5 = re.sub(r"^\s*scene\s+TEXT,.*\n", "", v5, flags=re.M)
    raw = _sq.connect(p)
    raw.executescript(v5)
    now = time.time()
    raw.execute("INSERT INTO child(child_id, caregiver_id, name, status, born_at,"
                " total_paused_seconds, stage_policy_version, rng_seed,"
                " state_version, created_at, updated_at)"
                " VALUES('c1','papa','囡','active',?,0,1,1,0,?,?)",
                (now - 3600, now - 3600, now - 3600))
    raw.execute("INSERT INTO child_state(child_id, mood, health, intimacy,"
                " nutrition, fatigue, last_settled_at, updated_at)"
                " VALUES('c1',60,80,20,50,20,?,?)", (now - 3600, now - 3600))
    for i in range(3):   # 升级前同日已玩过三次
        raw.execute("INSERT INTO action_log(child_id, actor, kind, payload_json,"
                    " effective_at, created_at, idempotency_key,"
                    " state_version_before, state_version_after)"
                    " VALUES('c1','papa','play','{}',?,?,?,0,0)",
                    (now - 1800 + i, now - 1800 + i, f"old{i}"))
    raw.execute("PRAGMA user_version=5")
    raw.commit()
    raw.close()
    c = pdb.connect(p)   # 迁移:钉 rules_v2_since stamp
    stamp = c.execute("SELECT value FROM parenting_meta WHERE child_id='c1'"
                      " AND key='rules_v2_since'").fetchone()
    assert stamp is not None and float(stamp[0]) >= now
    child_mod.apply_action(c, "c1", "papa", "play", idempotency_key="new1",
                           now=now + 60)
    rec = json.loads(c.execute(
        "SELECT payload_json FROM action_log WHERE child_id='c1'"
        " AND idempotency_key='new1'").fetchone()[0])
    assert "decay_factor" not in rec   # 升级前三次不计,升级后首次=全额
    assert rec["effects"]["mood"] == pytest.approx(cfg.ACTION_EFFECTS["play"]["mood"])
    c.close()


# ── 模型小数权重 ──

def test_model_fractional_weight_roundtrip():
    m = VariableOrderMarkov(2)
    m.feed("抱抱", weight=0.5)
    uni = m.counts[0][""]
    assert uni["抱"] == pytest.approx(1.0)   # 两个"抱"各 0.5
    blob = m.to_blob()
    m2 = VariableOrderMarkov.from_blob(blob)
    assert m2.counts[0][""]["抱"] == pytest.approx(1.0)
    # 采样不炸(权重下限 0.1 兜零权重分布)
    ch = m2.sample_next("\n", 2, 0.0, 1.0, random.Random(1))
    assert isinstance(ch, str)


# ── 评审回归

def test_settle_split_equals_whole_across_night_boundary():
    """非整点跨夜窗:任意切分点,消化分量整段=分段(精确,边界贴步)。"""
    base = dict(mood=60.0, health=80.0, intimacy=20.0, nutrition=50.0,
                fatigue=20.0, darkness=0.0, digest_load=60.0)
    t2230 = _local(2026, 7, 22, 22, 30)
    whole = child_mod.settle_state(dict(base), 2.0, start=t2230)
    a = child_mod.settle_state(dict(base), 0.7, start=t2230)
    split = child_mod.settle_state(a, 1.3, start=t2230 + 0.7 * 3600)
    assert split["digest_load"] == pytest.approx(whole["digest_load"], abs=1e-6)
    # 期望值精确可算:22:30-23:00 白天 0.5h + 23:00-00:30 夜窗 1.5h
    expect = 60.0 - cfg.DIGEST_DECAY_PER_H * 0.5 - cfg.DIGEST_NIGHT_DECAY_PER_H * 1.5
    assert whole["digest_load"] == pytest.approx(expect, abs=1e-6)


def test_feed_same_key_different_text_rejected(conn, born):
    """同幂等键不同正文的重试=整体视为重放:语料/模型/动作账三本都不动。"""
    cid, brain = born
    child_mod.feed_corpus(conn, brain, cid, "第一次的正文", actor="papa",
                          idempotency_key="dupkey", now=T0 + 100)
    n_corpus = conn.execute("SELECT COUNT(*) FROM corpus_item WHERE child_id=?",
                            (cid,)).fetchone()[0]
    n_act = conn.execute("SELECT COUNT(*) FROM action_log WHERE child_id=?",
                         (cid,)).fetchone()[0]
    ver = child_mod.get_child(conn, cid)["state_version"]
    chars = brain.model.total_chars
    r2 = child_mod.feed_corpus(conn, brain, cid, "换了一份正文重试", actor="papa",
                               idempotency_key="dupkey", now=T0 + 101)
    assert r2["duplicate"]
    assert conn.execute("SELECT COUNT(*) FROM corpus_item WHERE child_id=?",
                        (cid,)).fetchone()[0] == n_corpus
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE child_id=?",
                        (cid,)).fetchone()[0] == n_act
    assert child_mod.get_child(conn, cid)["state_version"] == ver
    assert brain.model.total_chars == chars


def test_same_timestamp_actions_still_decay(conn, born):
    """两个不同幂等键、同一 effective_at 的动作:第二个照样递减。"""
    cid, _ = born
    t = T0 + 1234
    child_mod.apply_action(conn, cid, "papa", "play", idempotency_key="st0", now=t)
    child_mod.apply_action(conn, cid, "papa", "play", idempotency_key="st1", now=t)
    rec = json.loads(conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='st1'",
        (cid,)).fetchone()["payload_json"])
    assert rec["decay_factor"] == pytest.approx(cfg.DAILY_DECAY)


def test_overload_feed_catchup_consistency(conn, born):
    """过载打折权重经 snapshot=False+重新 load 的 catch-up 重放,与当场喂一致。"""
    cid, brain = born
    t = T0 + 300
    _set_state(conn, cid, t, digest_load=cfg.DIGEST_OVERLOAD_AT + 10)
    child_mod.feed_corpus(conn, brain, cid, "打折权重的那句话", actor="papa",
                          idempotency_key="cc1", snapshot=False, now=t)
    live = brain.model.counts[0][""]
    reload_brain = child_mod.ChildBrain.load(conn, cid)
    replayed = reload_brain.model.counts[0][""]
    for ch in set(live) | set(replayed):
        assert replayed.get(ch, 0) == pytest.approx(live.get(ch, 0), abs=1e-9), ch


def test_overload_threshold_semantics(conn, born):
    """口径:负荷恰=阈值→吸收已打折(≥),出口碎化按超出比例连续起步(=0)。"""
    cid, brain = born
    t = T0 + 400
    _set_state(conn, cid, t, digest_load=cfg.DIGEST_OVERLOAD_AT)
    r = child_mod.feed_corpus(conn, brain, cid, "阈值上的一句", actor="papa",
                              idempotency_key="th1", now=t)
    assert r["overloaded"]
    w = conn.execute("SELECT training_weight FROM corpus_item WHERE id=?",
                     (r["corpus_id"],)).fetchone()[0]
    assert w == pytest.approx(cfg.DIGEST_ABSORB_FACTOR)   # 恰=阈值也打折落库
    _set_state(conn, cid, t + 60, digest_load=cfg.DIGEST_OVERLOAD_AT)
    res = child_mod.child_speak(conn, brain, cid, now=t + 60)
    assert "overload" not in res.params    # 超出=0,碎化比例连续从 0 起步


def test_db_v5_migrates_to_v6(tmp_path):
    """真实 v5 结构(无 digest_load)直迁 v6:列补上默认 0,既有状态行不动。"""
    import re
    import sqlite3 as _sq
    p = str(tmp_path / "v5.db")
    v5 = re.sub(r"^\s*digest_load REAL.*\n", "", pdb._SCHEMA, flags=re.M)
    assert v5 != pdb._SCHEMA
    v5b = re.sub(r"^\s*scene\s+TEXT,.*\n", "", v5, flags=re.M)   # v7 列同剥
    assert v5b != v5
    v5 = v5b
    raw = _sq.connect(p)
    raw.executescript(v5)
    raw.execute("INSERT INTO child(child_id, caregiver_id, status,"
                " stage_policy_version, rng_seed, created_at, updated_at)"
                " VALUES('c1','papa','active',1,1,0,0)")
    raw.execute("INSERT INTO child_state(child_id, mood, health, intimacy,"
                " nutrition, fatigue, last_settled_at, updated_at)"
                " VALUES('c1', 61, 82, 23, 44, 15, 0, 0)")
    raw.execute("PRAGMA user_version=5")
    raw.commit()
    raw.close()
    c = pdb.connect(p)
    row = c.execute("SELECT * FROM child_state WHERE child_id='c1'").fetchone()
    assert row["digest_load"] == 0
    assert (row["mood"], row["health"], row["intimacy"]) == (61, 82, 23)
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    c.close()
    pdb.connect(p).close()   # 幂等


# ── driver 提示面 ──

def test_driver_overload_hint(tmp_path, monkeypatch):
    from nursery import texts
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", 0.0)
    monkeypatch.setattr(cfg, "DIGEST_OVERLOAD_AT", 1.0)
    out = driver.init_birth("papa", "囡", now=T0)
    assert out.startswith("born:")
    r1 = driver.run("papa", ["feed",
                          "第一句要说得足够长足够长,把消化负荷一口气顶过测试阈值,"
                          "这样第二句就该吃撑了"], now=T0 + 60)
    assert texts.FEED_OVERLOAD_HINT not in r1     # 喂之前负荷还是 0
    r2 = driver.run("papa", ["feed", "第二句话就该吃撑了"], now=T0 + 120)
    assert texts.FEED_OVERLOAD_HINT in r2
    st = driver.run("papa", ["status"], now=T0 + 180)
    assert texts.STATUS_OVERLOAD_LINE in st
