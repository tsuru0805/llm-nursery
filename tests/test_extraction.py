# -*- coding: utf-8 -*-
"""开源抽取后的补充回归:孵化流程/夜哭响应率口径/语出惊人留痕/出走事件同事务。"""
import json
import random

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver, events

T0 = 1_800_000_000.0
DAY = 86400.0


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


def test_embryo_hatch_flow(saves):
    """--embryo 建占位胚胎;再次 init-birth(不带 --embryo)=孵化转正出生。"""
    out = driver.init_birth("papa", None, now=T0, embryo=True)
    assert out.startswith("embryo:")
    out2 = driver.init_birth("papa", "小小", now=T0 + DAY)
    assert out2.startswith("born:")
    conn = pdb.connect(driver._db_path("papa"))
    c = conn.execute("SELECT * FROM child").fetchone()
    assert c["status"] == "active" and c["name"] == "小小"
    assert c["born_at"] == T0 + DAY          # 出生时刻=孵化时刻,不是建档时刻
    st = child_mod.read_state(conn, c["child_id"], now=T0 + DAY + 60)
    assert 0 <= st["mood"] <= 100            # 状态行已补齐,可正常结算
    # 再跑一次=幂等 already;--embryo 也不能把 active 打回去
    assert driver.init_birth("papa", "别名", now=T0 + 2 * DAY).startswith("already:")
    assert driver.init_birth("papa", None, now=T0 + 2 * DAY,
                             embryo=True).startswith("already:")
    conn.close()


def test_response_rate_counts_only_night_windows(born):
    """夜哭响应率按夜窗算:白天喂再多也刷不满,窗口内响应才算数。"""
    conn, cid, brain = born
    for i, date in enumerate(("2026-01-01", "2026-01-02")):
        conn.execute(
            "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at, expires_at,"
            " catchup_policy, status, payload_json, idempotency_key)"
            " VALUES(?,?,NULL,?,?,'drop','fired',?,?)",
            (cid, "night_cry", T0 + i * DAY + 3600, T0 + i * DAY + 2 * 3600,
             json.dumps({"date": date}), f"nightcry:{date}"))
    # 第一夜窗口内响应一次;白天猛喂 5 次(旧口径会把响应率刷到 1.0)
    child_mod.apply_action(conn, cid, "papa", "soothe",
                           idempotency_key="night-resp", now=T0 + 3600 + 60)
    for i in range(5):
        child_mod.apply_action(conn, cid, "papa", "feed",
                               idempotency_key=f"day-{i}", now=T0 + 12 * 3600 + i)
    conn.execute("UPDATE child_state SET intimacy=90, darkness=0, last_settled_at=?"
                 " WHERE child_id=?", (T0 + 37 * DAY, cid))
    end = events.judge_ending(conn, brain, cid, now=T0 + 38 * DAY)
    assert end is not None
    p = json.loads(conn.execute(
        "SELECT payload_json FROM outbox WHERE kind='nursery.ending'").fetchone()
        ["payload_json"])
    assert p["response_rate"] == 0.5   # 两夜一响;白天动作不计入


class _Fire(random.Random):
    """概率闸全过的确定性 rng(random()=0,其余方法继承)。"""

    def random(self):
        return 0.0


def test_surprise_writes_utterance(born):
    """语出惊人也要进 utterance 留痕(trigger='surprise',关联真实快照)。"""
    conn, cid, brain = born
    t = T0 + 13 * DAY   # child 期
    for i in range(2):
        child_mod.feed_corpus(
            conn, brain, cid, f"这句偷学来的话内容编号{i},足够长也足够怪。",
            source_kind="archive", source_ref=f"w{i}@0+20", now=T0 + 100 + i)
    got = None
    for i in range(200):
        got = events.maybe_surprise(conn, brain, cid, _Fire(i), now=t + i * 300)
        if got:
            break
    assert got, "两窗语料+概率闸全开,应能引爆"
    row = conn.execute("SELECT * FROM utterance WHERE trigger='surprise'").fetchone()
    assert row is not None and row["accepted"] == 1
    assert row["text"] == got["utterance"]
    assert row["model_snapshot_id"] is not None


def test_runaway_state_and_event_atomic(born, monkeypatch):
    """事件写入阶段炸=状态跃迁一起回滚,不出现「已出走但事件永久丢失」。"""
    conn, cid, brain = born
    t_teen = T0 + 30 * DAY
    conn.execute("UPDATE child_state SET darkness=100, last_settled_at=?"
                 " WHERE child_id=?", (t_teen, cid))

    def boom(*a, **k):
        raise RuntimeError("emit-fail")

    monkeypatch.setattr(events, "_emit_locked", boom)
    with pytest.raises(RuntimeError):
        events.maybe_runaway(conn, cid, _Fire(0), now=t_teen)
    c = child_mod.get_child(conn, cid)
    assert c["status"] == "active" and c["runaway_at"] is None  # 整体回滚,零半提交
    # 轴账也没落(同事务):出走的心理规则不能单独提交
    n = conn.execute("SELECT COUNT(*) FROM psyche_axis_log WHERE child_id=?"
                     " AND reason='runaway'", (cid,)).fetchone()[0]
    assert n == 0


def test_tick_all_reads_players_from_env(saves, monkeypatch):
    """NURSERY_PLAYERS 晚于 import 设定(如 --tick 加载 .env)也要对巡检生效。"""
    from nursery import scheduler
    monkeypatch.setenv("NURSERY_PLAYERS", "papa,guardian")
    out = scheduler.tick_all(now=T0)
    assert set(out) == {"papa", "guardian"}   # 两格都巡到(无档=skipped,不炸)
