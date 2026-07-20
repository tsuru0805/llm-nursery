# -*- coding: utf-8 -*-
"""DS 心理层测试。

纪律:DS client 全 mock(测试**永不真调 API**——maybe_decide 一律注入 ds_complete;
唯一不注入的 no_key 用例先 delenv 掉 DEEPSEEK_API_KEY);全走临时 db;假时钟注入。
覆盖:迁移 v5 / 轴规则表(含幂等+夜哭响应加成+忽视)/ 阶段闸 / fail-open 三态 /
锚词偏置生效与绕护栏不可能 / 预算闸 / 节流与活动闸 / tick 接线。
"""
import json
import os
import random
import sqlite3
import urllib.error

import pytest

from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import events as events_mod
from nursery import psyche
from nursery import scheduler
from nursery.config import STAGE_DECODE_V1
from nursery.decoder import FALLBACK_BABBLE, speak
from nursery.guard import OverlapGuard
from nursery.model import VariableOrderMarkov

T0 = 1_800_000_000.0
DAY = 86400.0
T_TODDLER = T0 + 5 * DAY   # 4-12 天=幼儿期(DS 阶段闸开)

CORPUS = """睡吧,睡吧,午睡着了就不哭了。
乖,不哭不哭,爸爸在。
喝奶奶了,慢一点,不着急。
爸爸在家,妈妈在家,你也在家,我们三个都有太阳。
把果果分给妈妈一半,分享是好孩子。
月亮出来了,星星也出来了,该睡觉了。"""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """全模块封网:任何用例误触 urlopen 直接炸,永不真调 API。"""
    def _boom(*a, **k):
        raise AssertionError("psyche 测试禁止真实网络调用")
    monkeypatch.setattr("urllib.request.urlopen", _boom)


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "psyche_test.db"))
    yield c
    c.close()


@pytest.fixture()
def born(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed-corpus", now=T0 + 60)
    return cid, brain


def _ok_ds(conn, cid, calls, anchors=("果果",)):
    """合法 DS mock:证据永远指回最新一条 action_log(必在近 8 条输入编号内)。"""
    def fake(prompt):
        calls.append(prompt)
        aid = conn.execute("SELECT MAX(id) FROM action_log WHERE child_id=?",
                           (cid,)).fetchone()[0]
        return {"content": json.dumps(
            {"behavior": "凑近一点", "posture": "小心翼翼",
             "anchor_words": list(anchors), "evidence": [f"a{aid}"],
             "no_action": False, "reason": "有人陪着",
             # inner=终稿(wanwan-v1)新增字段:解析器刻意不消费,整段进 raw_json 留痕
             "inner": "想再靠近一点点"}, ensure_ascii=False),
            "model": "mock-ds", "prompt_tokens": 100, "completion_tokens": 50}
    return fake


# ── A. schema v5 迁移 ──

def test_db_fresh_has_psyche_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"psyche_axis", "psyche_axis_log", "psyche_decision"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION == 5


def test_db_v4_migrates_to_v5(tmp_path):
    """v4 旧库(无 psyche 三表)连接即迁移;重连幂等。"""
    p = str(tmp_path / "v4.db")
    base = pdb._SCHEMA.split("CREATE TABLE IF NOT EXISTS psyche_axis")[0]
    assert "psyche" not in base, "v4 造库不应含 psyche 表"
    raw = sqlite3.connect(p)
    raw.executescript(base)
    raw.execute("PRAGMA user_version=4")
    raw.commit()
    raw.close()
    c = pdb.connect(p)   # 触发迁移
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"psyche_axis", "psyche_axis_log", "psyche_decision"} <= tables
    assert c.execute("PRAGMA user_version").fetchone()[0] == 5
    c.close()
    pdb.connect(p).close()   # 再连=幂等


# ── B. 程序层规则表(可审计事实) ──

def test_rules_discipline_mama_and_ledger(conn, born):
    cid, _ = born
    ax0 = psyche.read_axes(conn, cid)
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="d1", now=T0 + 100)
    ax1 = psyche.read_axes(conn, cid)
    assert ax1["esteem"] < ax0["esteem"]       # 被管教→自尊-
    assert ax1["anxiety"] > ax0["anxiety"]
    child_mod.apply_action(conn, cid, "mama", "mama_hug",
                           idempotency_key="m1", now=T0 + 200)
    ax2 = psyche.read_axes(conn, cid)
    assert ax2["anxiety"] < ax1["anxiety"]     # 妈妈互动→不安-
    # 流水可审计:reason=规则键,source_key 指回动作幂等键
    rows = conn.execute(
        "SELECT * FROM psyche_axis_log WHERE child_id=? AND reason='discipline'",
        (cid,)).fetchall()
    assert rows and all(r["source_key"] == "d1" for r in rows)
    # 动作账 payload 同步留痕
    act = conn.execute(
        "SELECT payload_json FROM action_log WHERE child_id=? AND idempotency_key='d1'",
        (cid,)).fetchone()
    assert "psyche" in json.loads(act["payload_json"])


def test_rules_idempotent_replay(conn, born):
    cid, _ = born
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="same", now=T0 + 100)
    ax1 = psyche.read_axes(conn, cid)
    n1 = conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?",
                      (cid,)).fetchone()[0]
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="same", now=T0 + 200)   # 重放同 key
    assert psyche.read_axes(conn, cid) == ax1
    assert conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?",
                        (cid,)).fetchone()[0] == n1


def _insert_fired_cry(conn, cid, due, exp, date):
    with child_mod.tx(conn):
        conn.execute(
            "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
            " catchup_policy, status, payload_json, idempotency_key)"
            " VALUES(?,?,NULL,?,?,'drop','fired',?,?)",
            (cid, "night_cry", due, exp,
             json.dumps({"date": date}), f"nightcry:{date}"))


def test_neglect_rule_hits_axes(conn, born):
    """一整晚零回应:黑暗值账之外,不安+独立+也真记。"""
    cid, _ = born
    _insert_fired_cry(conn, cid, T0 + 3600, T0 + 3 * 3600, "2027-01-01")
    ax0 = psyche.read_axes(conn, cid)
    assert events_mod.check_neglect(conn, cid, now=T0 + 3 * 3600 + 10) == 1
    ax1 = psyche.read_axes(conn, cid)
    assert ax1["anxiety"] > ax0["anxiety"]
    assert ax1["independence"] > ax0["independence"]


def test_night_response_bonus_once_per_night(conn, born):
    """夜哭窗口内被响应→不安-(动作规则之外的加成);同晚只记一次。"""
    cid, _ = born
    due, exp = T0 + 3600, T0 + 3 * 3600
    _insert_fired_cry(conn, cid, due, exp, "2027-01-02")
    ax0 = psyche.read_axes(conn, cid)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s1", now=due + 60)
    ax1 = psyche.read_axes(conn, cid)
    expect = cfg.PSYCHE_RULES["soothe"]["anxiety"] + \
        cfg.PSYCHE_NIGHT_RESPONSE_BONUS["anxiety"]
    assert ax1["anxiety"] == pytest.approx(ax0["anxiety"] + expect)
    bonus_rows = conn.execute(
        "SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
        " AND reason='night_cry_responded'", (cid,)).fetchone()[0]
    assert bonus_rows == 1
    child_mod.apply_action(conn, cid, "papa", "feed",
                           idempotency_key="f1", now=due + 120)   # 同晚第二次响应
    assert conn.execute(
        "SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
        " AND reason='night_cry_responded'", (cid,)).fetchone()[0] == 1
    # 窗口外的动作没有加成
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="s2", now=exp + 3600)
    assert conn.execute(
        "SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
        " AND reason='night_cry_responded'", (cid,)).fetchone()[0] == 1


def test_axis_and_darkness_trends(conn, born):
    cid, _ = born
    t = T_TODDLER
    child_mod.apply_action(conn, cid, "papa", "discipline",
                           idempotency_key="tr1", now=t - 3600)
    tr = psyche.axis_trends(conn, cid, t)
    assert tr["anxiety"] == "rising"
    assert tr["esteem"] == "falling"
    assert tr["independence"] == "flat"     # |0.5|<eps
    assert psyche.darkness_trend(conn, cid, t) == "rising"
    # 窗口外的旧账不算趋势(出生时那笔 feed 已在 48h 外)
    tr_old = psyche.axis_trends(conn, cid, t + cfg.PSYCHE_TREND_WINDOW_H * 3600 + 7200)
    assert tr_old["anxiety"] == "flat"


def test_runaway_hits_axes_same_tx(conn, born):
    """出走=心理事件:与状态跃迁同事务落轴账。"""
    cid, _ = born
    t_teen = T0 + 25 * DAY   # 24-36 天=青春期
    with child_mod.tx(conn):
        conn.execute("UPDATE child_state SET darkness=95, last_settled_at=?,"
                     " updated_at=? WHERE child_id=?", (t_teen - 60, t_teen - 60, cid))
    ax0 = psyche.read_axes(conn, cid)

    class _AlwaysFire:
        def random(self):
            return 0.0
    assert events_mod.maybe_runaway(conn, cid, _AlwaysFire(), now=t_teen) is True
    ax1 = psyche.read_axes(conn, cid)
    assert ax1["independence"] > ax0["independence"]
    assert conn.execute(
        "SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=? AND reason='runaway'",
        (cid,)).fetchone()[0] > 0


def test_ds_input_includes_outbox_events(conn, born):
    """氛围事件(每日随机/语出惊人/夜哭)进 DS 近期事件,o<id> 可作证据。"""
    cid, _ = born
    events_mod._emit(conn, cid, kind="nursery.event", item_kind=None,
                     title="书包里多了一颗小石头,说是捡给你的。", note=None,
                     payload={"event": "stone", "stage": "toddler"},
                     idem="daily:test", t=T_TODDLER - 1800)
    oid = conn.execute("SELECT id FROM outbox WHERE idempotency_key='daily:test'"
                       ).fetchone()[0]
    calls = []
    def fake(prompt):
        calls.append(prompt)
        return {"content": json.dumps(
            {"behavior": "把石头攥在手里", "anchor_words": ["石头"],
             "evidence": [f"o{oid}"], "no_action": False}, ensure_ascii=False),
            "model": "mock-ds", "prompt_tokens": 1, "completion_tokens": 1}
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)
    assert out["status"] == "ok"                       # o 编号是合法证据
    assert f"o{oid}" in calls[0] and "小石头" in calls[0]
    row = conn.execute("SELECT evidence_json FROM psyche_decision WHERE id=?",
                       (out["decision_id"],)).fetchone()
    assert json.loads(row["evidence_json"]) == [f"o{oid}"]


# ── C. DS 决策层:阶段闸/OK 路径/fail-open 三态 ──

def test_ds_stage_gate_infant(conn, born):
    """embryo/infant 不调 DS(轴照记),toddler 起生效。"""
    cid, _ = born
    calls = []
    out = psyche.maybe_decide(conn, cid, now=T0 + 3600,
                              ds_complete=_ok_ds(conn, cid, calls))
    assert out is None and not calls
    assert conn.execute("SELECT COUNT(*) FROM psyche_decision").fetchone()[0] == 0
    # 轴照记:婴儿期规则流水已经在长
    assert conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?",
                        (cid,)).fetchone()[0] > 0


def test_ds_not_when_runaway(conn, born):
    cid, _ = born
    with child_mod.tx(conn):
        conn.execute("UPDATE child SET status='runaway', runaway_at=? WHERE child_id=?",
                     (T_TODDLER - 60, cid))
    calls = []
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER,
                              ds_complete=_ok_ds(conn, cid, calls))
    assert out is None and not calls


def test_ds_ok_path_full_trace(conn, born):
    cid, _ = born
    calls = []
    aid = conn.execute("SELECT MAX(id) FROM action_log WHERE child_id=?",
                       (cid,)).fetchone()[0]
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER,
                              ds_complete=_ok_ds(conn, cid, calls))
    assert out and out["status"] == "ok" and len(calls) == 1
    assert out["anchor_words"] == ["果果"] and out["no_action"] is False
    row = conn.execute("SELECT * FROM psyche_decision WHERE child_id=?",
                       (cid,)).fetchone()
    assert row["status"] == "ok" and row["api_called"] == 1
    assert row["stage"] == "toddler" and row["trigger"] == "tick"
    assert json.loads(row["anchor_words_json"]) == ["果果"]
    assert json.loads(row["evidence_json"]) == [f"a{aid}"]     # 证据指回事件 id
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50
    assert row["model"] == "mock-ds" and row["latency_ms"] is not None
    assert row["raw_json"] and row["input_digest_json"]         # 原始返回+输入摘要留痕
    # inner(终稿新增,她过目字段集):解析器不消费但必须完整留在 raw_json,围观页将来可挖
    assert "想再靠近一点点" in row["raw_json"]
    # prompt:趋势只给方向词,事件带编号
    assert "平稳" in calls[0] and f"a{aid}" in calls[0]
    # 锚词接力读口:TTL 内有效,超 TTL 归零
    assert psyche.latest_anchor_words(conn, cid, T_TODDLER + 60) == ["果果"]
    assert psyche.latest_anchor_words(
        conn, cid, T_TODDLER + cfg.PSYCHE_DECISION_TTL_S + 61) is None


def test_ds_no_action_is_legal(conn, born):
    cid, _ = born
    def fake(prompt):
        return {"content": '{"no_action": true, "anchor_words": ["忽略"],'
                           ' "evidence": [], "reason": "装作不在意"}',
                "model": "mock-ds", "prompt_tokens": 1, "completion_tokens": 1}
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)
    assert out["status"] == "ok" and out["no_action"] is True
    assert out["anchor_words"] == []                    # 不行动=不给嘴递词
    assert out["behavior"] == "不行动"
    assert psyche.latest_anchor_words(conn, cid, T_TODDLER + 60) is None


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("t"), "timeout"),
    (urllib.error.URLError(TimeoutError("t")), "timeout"),
    (RuntimeError("boom"), "api_error"),
])
def test_ds_fail_open_exceptions(conn, born, exc, expected):
    """fail-open 三态之 超时/API 错:留痕降级,孩子照旧纯 n-gram 说话。"""
    cid, brain = born
    def fake(prompt):
        raise exc
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)
    assert out["status"] == expected
    row = conn.execute("SELECT * FROM psyche_decision WHERE id=?",
                       (out["decision_id"],)).fetchone()
    assert row["status"] == expected and row["api_called"] == 1 and row["error"]
    res = child_mod.child_speak(conn, brain, cid, now=T_TODDLER + 1)
    assert res.text                                     # 绝不因心理层挂掉不说话
    assert "anchors" not in res.params                  # 无有效决策=零偏置


@pytest.mark.parametrize("content", [
    "我觉得他应该开心一点",                                  # 根本不是 JSON
    '{"posture": "x", "evidence": ["a1"]}',                  # 缺 behavior 且非 no_action
    '{"behavior": "b", "anchor_words": "不是列表", "evidence": ["a1"]}',
    '{"behavior": "b", "anchor_words": [], "evidence": ["z999"]}',  # 证据全非法
    '{"behavior": "b", "anchor_words": [], "evidence": []}',        # 无证据(必须指回输入编号)
])
def test_ds_bad_json_fail_open(conn, born, content):
    cid, brain = born
    def fake(prompt):
        return {"content": content, "model": "mock-ds",
                "prompt_tokens": 1, "completion_tokens": 1}
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)
    assert out["status"] == "bad_json"
    assert psyche.latest_anchor_words(conn, cid, T_TODDLER + 60) is None
    res = child_mod.child_speak(conn, brain, cid, now=T_TODDLER + 1)
    assert res.text                                     # 照样说话


def test_latest_decision_supersedes_older_ok(conn, born):
    """最新一条失败/不行动决策必须压掉旧 ok 锚词=回纯 n-gram。"""
    cid, brain = born
    calls = []
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER,
                               ds_complete=_ok_ds(conn, cid, calls))["status"] == "ok"
    assert psyche.latest_anchor_words(conn, cid, T_TODDLER + 60) == ["果果"]
    # 新活动+过节流窗后,一次 timeout 尝试
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="sup1", now=T_TODDLER + 3600)
    def bad(prompt):
        raise TimeoutError("t")
    t2 = T_TODDLER + 2 * 3600
    assert psyche.maybe_decide(conn, cid, now=t2, ds_complete=bad)["status"] == "timeout"
    assert psyche.latest_anchor_words(conn, cid, t2 + 60) is None   # 不翻旧账
    res = child_mod.child_speak(conn, brain, cid, now=t2 + 61)
    assert "anchors" not in res.params
    # 最新一条是 no_action 也同理
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="sup2", now=t2 + 3600)
    def na(prompt):
        return {"content": '{"no_action": true, "evidence": []}',
                "model": "mock-ds", "prompt_tokens": 1, "completion_tokens": 1}
    t3 = t2 + 2 * 3600
    assert psyche.maybe_decide(conn, cid, now=t3, ds_complete=na)["status"] == "ok"
    assert psyche.latest_anchor_words(conn, cid, t3 + 60) is None


def test_maybe_decide_rejects_nested_tx(conn, born):
    """公开入口禁嵌套:外部事务内调用直接 raise,不带锁出网。"""
    cid, _ = born
    calls = []
    with pytest.raises(RuntimeError):
        with child_mod.tx(conn):
            psyche.maybe_decide(conn, cid, now=T_TODDLER,
                                ds_complete=_ok_ds(conn, cid, calls))
    assert not calls


def test_ds_no_key_recorded(conn, born, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cid, _ = born
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER)   # 不注入 client 且无 key
    assert out["status"] == "no_key"
    row = conn.execute("SELECT api_called FROM psyche_decision WHERE id=?",
                       (out["decision_id"],)).fetchone()
    assert row["api_called"] == 0                        # 没出网,不吃预算


# ── C2. 节流/活动闸/预算闸 ──

def test_ds_throttle_and_activity_gate(conn, born):
    cid, _ = born
    calls = []
    fake = _ok_ds(conn, cid, calls)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)["status"] == "ok"
    # 1h 节流窗内=静默
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 600, ds_complete=fake) is None
    # 过节流窗但没有任何新动作/履历=活动闸,不烧调用
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 2 * 3600,
                               ds_complete=fake) is None
    assert len(calls) == 1
    # 有新动作后恢复
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="ag1", now=T_TODDLER + 3 * 3600)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 4 * 3600,
                               ds_complete=fake)["status"] == "ok"
    assert len(calls) == 2


def test_ds_budget_gate(conn, born, monkeypatch):
    """预算闸:当日真出网 ≥ 上限=fail-open;留痕 budget_exceeded 一次/日不刷屏。"""
    cid, _ = born
    monkeypatch.setattr(cfg, "PSYCHE_DAILY_CALL_MAX", 1)
    monkeypatch.setattr(cfg, "PSYCHE_MIN_INTERVAL_S", 0.0)
    calls = []
    fake = _ok_ds(conn, cid, calls)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER, ds_complete=fake)["status"] == "ok"
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="b1", now=T_TODDLER + 30)
    out = psyche.maybe_decide(conn, cid, now=T_TODDLER + 60, ds_complete=fake)
    assert out["status"] == "budget_exceeded" and len(calls) == 1   # 没再出网
    row = conn.execute("SELECT api_called FROM psyche_decision WHERE id=?",
                       (out["decision_id"],)).fetchone()
    assert row["api_called"] == 0
    # 同日再试=静默,不重复留痕
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="b2", now=T_TODDLER + 90)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 120, ds_complete=fake) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM psyche_decision WHERE child_id=?"
        " AND status='budget_exceeded'", (cid,)).fetchone()[0] == 1
    # 失败尝试也计预算(宁紧勿松):timeout 后配额烧掉照样闸
    monkeypatch.setattr(cfg, "PSYCHE_DAILY_CALL_MAX", 2)
    def bad(prompt):
        calls.append(prompt)
        raise TimeoutError("t")
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="b3", now=T_TODDLER + 150)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 180,
                               ds_complete=bad)["status"] == "timeout"
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="b4", now=T_TODDLER + 210)
    out2 = psyche.maybe_decide(conn, cid, now=T_TODDLER + 240, ds_complete=fake)
    assert out2 is None or out2["status"] == "budget_exceeded"
    assert len(calls) == 2


# ── D. 锚词接力:软偏置生效 + 绕护栏不可能 ──

def test_anchor_bias_shifts_sampling():
    """同分布下锚词字符被显著偏爱;偏置不能让没学过的字凭空出现。"""
    m = VariableOrderMarkov(2)
    for _ in range(50):
        m.feed("xa")
        m.feed("xb")
    rng = random.Random(1)
    base = sum(1 for _ in range(400)
               if m.sample_next("x", 1, 0.0, 1.0, rng) == "a")
    rng2 = random.Random(1)
    biased = sum(1 for _ in range(400)
                 if m.sample_next("x", 1, 0.0, 1.0, rng2, bias={"a": 5.0}) == "a")
    assert biased > base + 50            # ≈50% → ≈83%,显著偏移
    rng3 = random.Random(2)
    outs = {m.sample_next("x", 1, 0.0, 1.0, rng3, bias={"z": 100.0})
            for _ in range(50)}
    assert "z" not in outs               # 无中生有不可能


def test_anchor_cannot_bypass_guard():
    """锚词怂恿复读原句:护栏三层原封不动,照拒(锚词只影响偏好,不绕 guard)。"""
    m = VariableOrderMarkov(5)
    g = OverlapGuard()
    line = "上周三你陪我去动物园看长颈鹿和白企鹅呀"   # 19 字全唯一=模型只会复读原句
    m.feed(line)
    g.add_source(line)
    res = speak(m, g, "adult", random.Random(3), anchor_words=[line])
    assert not res.accepted and res.text == FALLBACK_BABBLE
    assert res.max_overlap >= STAGE_DECODE_V1["adult"]["overlap_limit"]


def test_child_speak_reads_latest_anchor(conn, born):
    cid, brain = born
    t = T_TODDLER
    with child_mod.tx(conn):
        conn.execute(
            "INSERT INTO psyche_decision(child_id, stage, trigger, status, api_called,"
            " anchor_words_json, no_action, created_at) VALUES(?,?,?,?,1,?,0,?)",
            (cid, "toddler", "tick", "ok", json.dumps(["果果"]), t - 60))
    res = child_mod.child_speak(conn, brain, cid, now=t)
    assert res.params.get("anchors") == ["果果"]        # 留痕:params 带锚词
    row = conn.execute(
        "SELECT generation_params_json FROM utterance WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    assert json.loads(row["generation_params_json"]).get("anchors") == ["果果"]
    if res.accepted:
        assert res.max_overlap < STAGE_DECODE_V1["toddler"]["overlap_limit"]


def test_child_speak_without_decision_zero_bias(conn, born):
    cid, brain = born
    res = child_mod.child_speak(conn, brain, cid, now=T0 + 3600)
    assert "anchors" not in res.params                  # 无决策=零偏置照旧


def test_activity_gate_ignores_backoff_bump(conn, born):
    """投递 backoff 改写 next_attempt_at 不得把旧事件当新活动
    (活动闸以 payload.ts 判定,入队即写死)。"""
    cid, _ = born
    events_mod._emit(conn, cid, kind="nursery.event", item_kind=None,
                     title="旧事件", note=None, payload={"event": "stone"},
                     idem="daily:bump", t=T_TODDLER - 600)
    calls = []
    fake = _ok_ds(conn, cid, calls)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER,
                               ds_complete=fake)["status"] == "ok"
    # 模拟投递失败 backoff:next_attempt_at 被推到决策之后
    with child_mod.tx(conn):
        conn.execute("UPDATE outbox SET next_attempt_at=?, attempt_count=1"
                     " WHERE idempotency_key='daily:bump'", (T_TODDLER + 3600,))
    # 过节流窗:没有任何真正的新活动,旧事件不得再开闸
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 2 * 3600,
                               ds_complete=fake) is None
    assert len(calls) == 1


def test_outbox_event_opens_activity_gate_by_ts(conn, born):
    """真正的新氛围事件(ts 在上次决策之后)开活动闸。"""
    cid, _ = born
    calls = []
    fake = _ok_ds(conn, cid, calls)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER,
                               ds_complete=fake)["status"] == "ok"
    events_mod._emit(conn, cid, kind="nursery.event", item_kind=None,
                     title="新事件", note=None, payload={"event": "hide"},
                     idem="daily:fresh", t=T_TODDLER + 3600)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 2 * 3600,
                               ds_complete=fake)["status"] == "ok"
    assert len(calls) == 2


def test_dirty_payload_rows_never_crash_or_open_gate(conn, born):
    """payload_json 为 null/[]/1/ts=true/ts=Infinity 的脏行,
    既不许让 maybe_decide/build_input 抛异常,也不许开活动闸。"""
    cid, _ = born
    calls = []
    fake = _ok_ds(conn, cid, calls)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER,
                               ds_complete=fake)["status"] == "ok"
    dirty = ["null", "[]", "1", '{"ts": true, "title": "脏行"}',
             '{"ts": Infinity, "title": "脏行"}', "not-json{{{"]
    with child_mod.tx(conn):
        for i, pj in enumerate(dirty):
            conn.execute(
                "INSERT INTO outbox(child_id, target, kind, payload_json, status,"
                " next_attempt_at, idempotency_key) VALUES(?,?,?,?,'pending',?,?)",
                (cid, "webhook", "nursery.event", pj, T_TODDLER + 3600,
                 f"dirty:{i}"))
    # 过节流窗:脏行不算新活动,不出网、不炸
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 2 * 3600,
                               ds_complete=fake) is None
    assert len(calls) == 1
    # 真新动作后决策恢复,build_input 面对脏行也不炸
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="dirty-ok", now=T_TODDLER + 3 * 3600)
    assert psyche.maybe_decide(conn, cid, now=T_TODDLER + 4 * 3600,
                               ds_complete=fake)["status"] == "ok"
    assert len(calls) == 2


# ── E. tick 接线(fail-open 不炸整拍) ──

def test_tick_one_psyche_wired_and_gated(tmp_path):
    db_path = str(tmp_path / "saves" / "papa" / "nursery.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    c = pdb.connect(db_path)
    child_mod.create_child(c, "papa", name="孩子", seed=1, now=T0)
    c.close()
    out = scheduler.tick_one(db_path, "papa", now=T0 + 120)
    assert "scheduled" in out            # 整拍没被心理层炸掉
    assert "psyche" not in out           # infant 阶段闸静默,绝无出网


def test_tick_one_psyche_lock_skips_when_held(tmp_path, monkeypatch):
    """.psyche.lock 被别的 tick 持着=本拍跳过,不双出网。"""
    import fcntl
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db_path = str(tmp_path / "saves" / "papa" / "nursery.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    c = pdb.connect(db_path)
    cid = child_mod.create_child(c, "papa", name="孩子", seed=1, now=T0)
    child_mod.apply_action(c, cid, "papa", "soothe",
                           idempotency_key="lk1", now=T0 + 60)   # 给活动闸供料
    c.close()
    lk = open(os.path.join(os.path.dirname(db_path), ".psyche.lock"), "a")
    fcntl.flock(lk, fcntl.LOCK_EX)
    try:
        out = scheduler.tick_one(db_path, "papa", now=T_TODDLER)
        assert "psyche" not in out       # 锁被占=跳过,主 tick 照常
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()
    out2 = scheduler.tick_one(db_path, "papa", now=T_TODDLER + 60)
    assert out2.get("psyche") == "no_key"   # 锁放开后正常走到留痕(无 key=不出网)
    c2 = pdb.connect(db_path)
    assert c2.execute("SELECT COUNT(*) FROM psyche_decision").fetchone()[0] == 1
    c2.close()
