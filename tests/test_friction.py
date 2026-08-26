# -*- coding: utf-8 -*-
"""v0.3:青春期专修·摩擦轴 annoyance。

设计原则钉死:摩擦轴独立于黑暗值——本文件同时验证 darkness 语义未被动过。
全部临时 db + 假时钟注入;真实 RULES_V2_SINCE(T0=2027 年,晚于切换时刻)。
"""
import json
import re
import time

import pytest

from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import friction

T0 = 1_800_000_000.0
DAY = 86400.0

CORPUS = """今天要记得穿外套,外面冷。
作业写完了吗,先写作业再玩。
恐龙博物馆下周带你去,说好了。
少喝冰的,对胃不好。
爸爸小时候也不爱写作业,但还是要写。"""


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_friction.db"))
    yield c
    c.close()


NOW = T0 + 30 * DAY   # teen(24-36 天)


@pytest.fixture()
def teen(conn):
    """青春期的孩子:born_at=T0,操作时刻 NOW=30 天。结算锚推到 NOW 附近,
    免得 30 天惰性结算把手动设的 annoyance 衰掉(那是设计行为)。"""
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    conn.execute("UPDATE child_state SET last_settled_at=?, updated_at=?",
                 (NOW, NOW))
    return cid, brain


def _set_annoy(conn, v: float, t: float = NOW):
    conn.execute("UPDATE child_state SET annoyance=?, last_settled_at=?", (v, t))


def _state(conn, cid, t):
    return child_mod.read_state(conn, cid, now=t, persist=False)


def _midnight(t):
    lt = time.localtime(t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# ── schema v8→v9 迁移 ──

def test_db_v8_migrates_to_v9_backfill_zero(tmp_path):
    """老 v8 库(无 annoyance 列)开新版:不炸+列补上+老档 backfill 0。"""
    import sqlite3 as _sq
    p = str(tmp_path / "v8.db")
    v8 = re.sub(r"^\s*annoyance\s+REAL.*\n", "", pdb._SCHEMA, flags=re.M)
    assert "annoyance" not in v8, "v8 造库没删掉 annoyance 列行"
    raw = _sq.connect(p)
    raw.executescript(v8)
    # 老档一行(FK 默认关,直插即可)
    raw.execute("INSERT INTO child(child_id, caregiver_id, status,"
                " stage_policy_version, rng_seed, created_at, updated_at)"
                " VALUES('old','papa','active',1,1,0,0)")
    raw.execute("INSERT INTO child_state(child_id, mood, health, intimacy,"
                " nutrition, fatigue, last_settled_at, updated_at)"
                " VALUES('old',60,80,20,50,20,0,0)")
    raw.execute("PRAGMA user_version=8")
    raw.commit()
    raw.close()
    c = pdb.connect(p)   # 触发迁移
    cols = {r[1] for r in c.execute("PRAGMA table_info(child_state)")}
    assert "annoyance" in cols
    assert c.execute("SELECT annoyance FROM child_state WHERE child_id='old'"
                     ).fetchone()[0] == 0
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    c.close()
    pdb.connect(p).close()   # 再连=幂等


# ── 自然时衰(settle,照 darkness 形制) ──

def test_annoyance_settles_down():
    s0 = dict(mood=60.0, health=80.0, intimacy=20.0, nutrition=50.0,
              fatigue=20.0, darkness=0.0, digest_load=0.0, annoyance=50.0)
    s = child_mod.settle_state(s0, 10.0)
    assert s["annoyance"] == pytest.approx(50.0 - cfg.ANNOY_HEAL_PER_H * 10.0)
    assert child_mod.settle_state(s0, 1000.0)["annoyance"] == 0.0   # 夹取不穿底


# ── 唠叨:当日同类超免费额的部分才涨 ──

def test_nag_rises_only_past_free_quota(teen, conn):
    cid, _ = teen
    for i in range(cfg.ANNOY_NAG_FREE):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"t{i}", now=NOW + i)
    a_free = _state(conn, cid, NOW + 10)["annoyance"]
    assert a_free == pytest.approx(0.0, abs=0.01)   # 免费额内零摩擦
    child_mod.apply_action(conn, cid, "papa", "talk",
                           idempotency_key="t_extra", now=NOW + 20)
    a1 = _state(conn, cid, NOW + 20)["annoyance"]
    assert a1 == pytest.approx(cfg.ANNOY_NAG_STEP, abs=0.01)
    child_mod.apply_action(conn, cid, "papa", "teach",
                           idempotency_key="teach0", now=NOW + 30)
    assert _state(conn, cid, NOW + 30)["annoyance"] == \
        pytest.approx(a1, abs=0.01)   # teach 计数独立,还在自己的免费额内


def test_nag_teen_only(teen, conn):
    """幼儿期(非 ANNOY_STAGES)同样连轰不涨摩擦。"""
    cid, _ = teen
    t = T0 + 6 * DAY   # toddler
    conn.execute("UPDATE child_state SET last_settled_at=?", (t,))
    for i in range(cfg.ANNOY_NAG_FREE + 3):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"td{i}", now=t + i)
    assert _state(conn, cid, t + 10)["annoyance"] == pytest.approx(0.0, abs=0.01)


def test_nag_leaves_darkness_semantics_alone(teen, conn):
    """设计原则:唠叨只挂摩擦轴——talk 的 darkness 效果(-2.5 温暖降叛逆)原样。"""
    cid, _ = teen
    conn.execute("UPDATE child_state SET darkness=50, last_settled_at=?", (NOW,))
    for i in range(cfg.ANNOY_NAG_FREE + 1):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"dk{i}", now=NOW + i)
    st = _state(conn, cid, NOW + 10)
    assert st["annoyance"] > 0
    # talk×4 全部照旧降黑暗(50 - 4×2.5 上下,当日递减系数会削一点,但必须在降)
    assert st["darkness"] < 50 - 4 * 2.5 * cfg.DAILY_DECAY_FLOOR


# ── 给台阶:高位时哄/谈心大幅消解+和解事件(每日一次) ──

def test_olive_branch_drops_and_emits_once(teen, conn):
    cid, _ = teen
    _set_annoy(conn, 60.0)
    child_mod.apply_action(conn, cid, "papa", "talk",
                           idempotency_key="olive1", now=NOW + 1)
    st = _state(conn, cid, NOW + 1)
    assert st["annoyance"] == pytest.approx(60.0 - cfg.ANNOY_OLIVE_DROP, abs=0.1)
    row = conn.execute("SELECT payload_json FROM action_log WHERE"
                       " idempotency_key='olive1'").fetchone()
    assert json.loads(row["payload_json"])["olive_branch"] is True
    olives = conn.execute("SELECT payload_json FROM outbox WHERE"
                          " idempotency_key LIKE 'olive:%'").fetchall()
    assert len(olives) == 1
    assert "台阶" in json.loads(olives[0]["payload_json"])["title"]
    # 同日再递台阶:摩擦照消,和解事件不刷屏(幂等 per date)
    _set_annoy(conn, 60.0, NOW + 100)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="olive2", now=NOW + 101,
                           extra_effects={})
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE"
                        " idempotency_key LIKE 'olive:%'").fetchone()[0] == 1


def test_olive_not_nag_when_high(teen, conn):
    """高位时的 talk=台阶不算唠叨:连轰也不涨,只降。"""
    cid, _ = teen
    _set_annoy(conn, 90.0)
    for i in range(cfg.ANNOY_NAG_FREE + 2):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"hi{i}", now=NOW + i)
    assert _state(conn, cid, NOW + 10)["annoyance"] < 90.0


def test_mama_soothe_gives_olive(teen, conn):
    """妈妈的哄也是台阶(ANNOY_OLIVE_KINDS 含 mama_soothe/mama_hug)。"""
    cid, _ = teen
    _set_annoy(conn, 60.0)
    child_mod.apply_action(conn, cid, "mama", "mama_soothe",
                           idempotency_key="mo1", now=NOW + 1)
    assert _state(conn, cid, NOW + 1)["annoyance"] == \
        pytest.approx(60.0 - cfg.ANNOY_OLIVE_DROP, abs=0.1)


# ── 已读不回:max(黑暗值路, 摩擦轴路),darkness 语义不动 ──

def test_refuse_via_annoyance_path(teen, conn, monkeypatch):
    cid, brain = teen
    monkeypatch.setattr(cfg, "ANNOY_REFUSE_MAX_P", 1.0)   # 钉满免概率抖动
    _set_annoy(conn, 100.0)
    conn.execute("UPDATE child_state SET darkness=0")     # 好好带娃线:黑暗恒 0
    res = child_mod.child_speak(conn, brain, cid, now=NOW + 1)
    assert res.refused   # annoyance=100 → refuse_p=1.0,黑暗值 0 也能已读不回


def test_no_refuse_when_both_zero(teen, conn, monkeypatch):
    cid, brain = teen
    monkeypatch.setattr(cfg, "ANNOY_REFUSE_MAX_P", 1.0)
    conn.execute("UPDATE child_state SET darkness=0, annoyance=0,"
                 " last_settled_at=?", (NOW,))
    for i in range(5):
        res = child_mod.child_speak(conn, brain, cid, now=NOW + 1 + i)
        assert not res.refused


# ── 顶嘴拧话 snark ──

def test_snark_uses_recent_direct_words(teen, conn):
    cid, brain = teen
    _set_annoy(conn, cfg.ANNOY_SNARK_MIN + 10)
    conn.execute("UPDATE child_state SET darkness=0")
    res = child_mod.child_speak(conn, brain, cid, now=NOW + 1)
    assert res.params.get("snark") is True
    anchors = res.params.get("anchors") or []
    assert anchors, "snark 模式必须带上父母语料锚词"
    corpus_blob = "".join(r["text"] for r in conn.execute(
        "SELECT text FROM corpus_item WHERE source_kind='direct'"))
    for w in anchors:
        assert w in corpus_blob   # 拧的是父母真说过的词,不编
    # utterance 留痕
    row = conn.execute("SELECT generation_params_json FROM utterance"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row["generation_params_json"]).get("snark") is True


def test_no_snark_below_threshold(teen, conn):
    cid, brain = teen
    _set_annoy(conn, cfg.ANNOY_SNARK_MIN - 20)
    res = child_mod.child_speak(conn, brain, cid, now=NOW + 1)
    assert "snark" not in res.params


def test_recent_direct_anchors_derivation(teen, conn):
    cid, _ = teen
    ws = friction.recent_direct_anchors(conn, cid)
    assert ws and len(ws) <= cfg.SNARK_MAX_ANCHORS
    assert all(len(w) <= 8 for w in ws)


# ── tick_friction:被晾 / 摔门 / 深夜彩蛋 ──

def test_quiet_day_raises_annoyance_once(teen, conn):
    cid, _ = teen
    t21 = _midnight(NOW) + 21 * 3600 + 1800   # 当日 21:30(白天全程没人理)
    conn.execute("UPDATE child_state SET last_settled_at=?", (t21,))
    out = friction.tick_friction(conn, cid, now=t21)
    assert out.get("quiet") is True
    st = _state(conn, cid, t21)
    assert st["annoyance"] == pytest.approx(cfg.ANNOY_QUIET_STEP, abs=0.1)
    # 同日幂等
    assert "quiet" not in friction.tick_friction(conn, cid, now=t21 + 300)
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE kind='left_alone'"
                        ).fetchone()[0] == 1


def test_quiet_needs_evening_and_real_gap(teen, conn):
    cid, _ = teen
    t15 = _midnight(NOW) + 15 * 3600
    assert friction.tick_friction(conn, cid, now=t15) == {}   # 21 点前不判
    # 白天一直有人陪:20:59 前每 4h 一个动作 → 无超长 gap
    t21 = _midnight(NOW) + 21 * 3600 + 60
    for i, hh in enumerate((8, 12, 16, 20)):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"q{i}",
                               now=_midnight(NOW) + hh * 3600)
    out = friction.tick_friction(conn, cid, now=t21)
    assert "quiet" not in out


def test_door_slam_deterministic_daily_once(teen, conn):
    cid, _ = teen
    _set_annoy(conn, cfg.ANNOY_DOOR_AT + 5)
    out = friction.tick_friction(conn, cid, now=NOW + 1)
    assert out.get("door_slam") is True
    rows = conn.execute("SELECT payload_json FROM outbox WHERE"
                        " idempotency_key LIKE 'doorslam:%'").fetchall()
    assert len(rows) == 1
    assert "门" in json.loads(rows[0]["payload_json"])["title"]
    # 同日幂等;阈下不触发
    assert "door_slam" not in friction.tick_friction(conn, cid, now=NOW + 300)
    _set_annoy(conn, cfg.ANNOY_DOOR_AT - 10, NOW + 600)
    assert "door_slam" not in friction.tick_friction(
        conn, cid, now=NOW + 600 + DAY)   # 次日但摩擦已在阈下


def test_night_egg_pct_from_real_corpus(teen, conn, monkeypatch):
    cid, _ = teen
    monkeypatch.setattr(cfg, "NIGHT_EGG_P", 1.0)   # 抽签钉中,验内容与幂等
    t23 = _midnight(NOW) + 23 * 3600 + 600
    conn.execute("UPDATE child_state SET last_settled_at=?", (t23,))
    out = friction.tick_friction(conn, cid, now=t23)
    assert out.get("night_egg") is True
    total = conn.execute("SELECT SUM(char_count) FROM corpus_item WHERE"
                         " child_id=?", (cid,)).fetchone()[0]
    row = conn.execute("SELECT payload_json FROM outbox WHERE"
                       " idempotency_key LIKE 'nightegg:%'").fetchone()
    p = json.loads(row["payload_json"])
    assert f"{total % 100}%" in p["title"]          # 百分比=真实语料量派生
    assert p["corpus_chars"] == total
    assert "night_egg" not in friction.tick_friction(conn, cid, now=t23 + 300)


def test_night_egg_idempotent_hit_skips_aggregate(teen, conn, monkeypatch):
    """幂等键前置(评审定案):当日已发后,同晚重复 tick 不再跑
    生命周期级 SUM 聚合(扫描量有界)。"""
    cid, _ = teen
    monkeypatch.setattr(cfg, "NIGHT_EGG_P", 1.0)
    t23 = _midnight(NOW) + 23 * 3600 + 600
    conn.execute("UPDATE child_state SET last_settled_at=?", (t23,))
    assert friction.tick_friction(conn, cid, now=t23).get("night_egg") is True
    seen: list = []
    conn.set_trace_callback(lambda sql: seen.append(sql))
    try:
        assert "night_egg" not in friction.tick_friction(conn, cid, now=t23 + 300)
    finally:
        conn.set_trace_callback(None)
    assert not any("SUM(char_count)" in s for s in seen)


def test_night_egg_respects_hour_and_odds(teen, conn, monkeypatch):
    cid, _ = teen
    monkeypatch.setattr(cfg, "NIGHT_EGG_P", 1.0)
    t22 = _midnight(NOW) + 22 * 3600
    assert "night_egg" not in friction.tick_friction(conn, cid, now=t22)
    monkeypatch.setattr(cfg, "NIGHT_EGG_P", 0.0)    # 今晚注定没彩蛋
    t23 = _midnight(NOW) + 23 * 3600 + 600
    assert "night_egg" not in friction.tick_friction(conn, cid, now=t23)


def test_tick_friction_gates_stage_and_status(teen, conn):
    cid, _ = teen
    _set_annoy(conn, 99.0, T0 + 6 * DAY)
    assert friction.tick_friction(conn, cid, now=T0 + 6 * DAY) == {}  # toddler 不生效
    conn.execute("UPDATE child SET status='runaway'")
    assert friction.tick_friction(conn, cid, now=NOW) == {}


# ── 日记上锁片段(观察日志,teen 限定) ──

def test_diary_peek_masks_real_utterance(teen, conn):
    from nursery.observer import daily_observe
    cid, _ = teen
    t21 = _midnight(NOW) + 21 * 3600 + 900
    said = "作业写完了再去看恐龙"
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " created_at) VALUES(?,?,?,?,1,?)",
                 (cid, "talk", "teen", said, t21 - 3600))
    out = daily_observe(conn, cid, now=t21)
    assert "diary" in out
    date = time.strftime("%Y-%m-%d", time.localtime(t21))
    row = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                       (f"obs:{date}:diary",)).fetchone()
    title = json.loads(row["payload_json"])["title"]
    keep = len(said) // 2
    assert said[:keep] in title            # 露出的一角=他真话的前半
    assert said not in title               # 整句绝不露
    assert title.count("▓") == len(said) - keep   # 遮一半字符


def test_diary_only_for_teen(teen, conn):
    from nursery.observer import daily_observe
    cid, _ = teen
    t = T0 + 6 * DAY   # toddler
    t21 = _midnight(t) + 21 * 3600 + 900
    conn.execute("INSERT INTO utterance(child_id, trigger, stage, text, accepted,"
                 " created_at) VALUES(?,?,?,?,1,?)",
                 (cid, "talk", "toddler", "恐龙博物馆好玩", t21 - 3600))
    out = daily_observe(conn, cid, now=t21)
    assert "diary" not in out


# ── child 后半扩展:账减半/前半不闹/戏剧件不下放 ──

CHILD_LATE = T0 + 20 * DAY    # child(12-24)后半:过中点 18 天
CHILD_EARLY = T0 + 13 * DAY   # child 前半


def test_annoy_stage_gate(teen, conn):
    cid, _ = teen
    child = child_mod.get_child(conn, cid)
    assert friction.annoy_stage(child, NOW) == "teen"
    assert friction.annoy_stage(child, CHILD_LATE) == "child"
    assert friction.annoy_stage(child, CHILD_EARLY) is None   # 前半懂事
    assert friction.annoy_stage(child, T0 + 6 * DAY) is None  # toddler 不闹


def test_nag_child_late_half_step(teen, conn):
    """child 后半唠叨涨半额(ANNOY_STAGE_SCALE)。"""
    cid, _ = teen
    t = CHILD_LATE
    conn.execute("UPDATE child_state SET last_settled_at=?", (t,))
    for i in range(cfg.ANNOY_NAG_FREE + 1):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"cl{i}", now=t + i)
    assert _state(conn, cid, t + 10)["annoyance"] == pytest.approx(
        cfg.ANNOY_NAG_STEP * cfg.ANNOY_STAGE_SCALE["child"], abs=0.01)


def test_nag_child_early_none(teen, conn):
    cid, _ = teen
    t = CHILD_EARLY
    conn.execute("UPDATE child_state SET last_settled_at=?", (t,))
    for i in range(cfg.ANNOY_NAG_FREE + 3):
        child_mod.apply_action(conn, cid, "papa", "talk",
                               idempotency_key=f"ce{i}", now=t + i)
    assert _state(conn, cid, t + 10)["annoyance"] == pytest.approx(0.0, abs=0.01)


def test_olive_full_drop_at_child_late(teen, conn):
    """台阶消解不折减:给台阶就给到位(child 后半同 teen 全额 -25)。"""
    cid, _ = teen
    t = CHILD_LATE
    _set_annoy(conn, 60.0, t)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="olc1", now=t)
    assert _state(conn, cid, t + 1)["annoyance"] == pytest.approx(
        60.0 - cfg.ANNOY_OLIVE_DROP, abs=0.5)


def test_tick_child_late_quiet_scaled_no_drama(teen, conn, monkeypatch):
    """child 后半 tick:被晾涨半额;摩擦顶格也不摔门、抽签钉中也不发深夜彩蛋
    (摔门/彩蛋=ANNOY_DRAMA_STAGES 青春期戏码)。"""
    cid, _ = teen
    monkeypatch.setattr(cfg, "NIGHT_EGG_P", 1.0)
    t23 = _midnight(CHILD_LATE) + 23 * 3600 + 600   # 23 点后:quiet/彩蛋窗全开
    a0 = cfg.ANNOY_DOOR_AT + 20
    conn.execute("UPDATE child_state SET last_settled_at=?, annoyance=?",
                 (t23, a0))
    out = friction.tick_friction(conn, cid, now=t23)
    assert out.get("quiet") is True
    assert "door_slam" not in out and "night_egg" not in out
    assert _state(conn, cid, t23)["annoyance"] == pytest.approx(
        a0 + cfg.ANNOY_QUIET_STEP * cfg.ANNOY_STAGE_SCALE["child"], abs=0.1)


def test_tick_teen_drama_unchanged(teen, conn):
    """teen 语义不动:摔门照旧(回归锚)。"""
    cid, _ = teen
    _set_annoy(conn, cfg.ANNOY_DOOR_AT + 5)
    assert friction.tick_friction(conn, cid, now=NOW + 1).get("door_slam") is True
