# -*- coding: utf-8 -*-
"""老档升级路径:v8 存档打开即迁移 v11 / policy 钉版不悄改 / 管理面冻龄与升版。

这是 v0.3 对老玩家的兼容性承诺本体——存档零手动迁移,新机制不翻旧账,
阶段表升版只走显式命令且永不倒龄。
"""
import json
import sqlite3

import pytest

from nursery import child as child_mod
from nursery import db as pdb
from nursery import driver, events

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    from nursery import config as _cfg
    monkeypatch.setattr(_cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    return tmp_path / "saves"


# ── 存档迁移:v8(v0.2.x 的档)打开即升 v11,零手动 ──

def _make_v8_db(path: str) -> str:
    """按当前 _SCHEMA 去掉 v9+ 增量,造一个 v0.2.x 形状的老档。"""
    ddl = pdb._SCHEMA.replace(
        "    annoyance  REAL NOT NULL DEFAULT 0,"
        "      -- v9 摩擦轴(唠叨/被晾攒的烦,独立于黑暗值,0-100)\n", "")
    assert "annoyance" not in ddl
    raw = sqlite3.connect(path)
    raw.executescript(ddl)
    raw.execute("PRAGMA user_version=8")
    # 一个活着的老孩子+状态行(迁移要给他 backfill)
    raw.execute(
        "INSERT INTO child(child_id, caregiver_id, name, status, born_at,"
        " total_paused_seconds, stage_policy_version, rng_seed, state_version,"
        " created_at, updated_at) VALUES('oldkid','papa','囡','active',?,0,1,7,0,?,?)",
        (T0, T0, T0))
    raw.execute(
        "INSERT INTO child_state(child_id, mood, health, intimacy, nutrition,"
        " fatigue, last_settled_at, updated_at) VALUES('oldkid',60,80,20,70,20,?,?)",
        (T0, T0))
    raw.commit()
    raw.close()
    return path


def test_v8_archive_migrates_in_place(tmp_path):
    p = _make_v8_db(str(tmp_path / "old.db"))
    c = pdb.connect(p)
    # 版本推进 + annoyance 回填 0(他还没攒过烦,不翻旧账)
    assert c.execute("PRAGMA user_version").fetchone()[0] == pdb.SCHEMA_VERSION
    row = c.execute("SELECT annoyance FROM child_state WHERE child_id='oldkid'"
                    ).fetchone()
    assert row is not None and row["annoyance"] == 0
    # 升级时刻戳:v0.3 新记账机制只从升级时刻起算
    stamp = c.execute("SELECT value FROM parenting_meta WHERE child_id='oldkid'"
                      " AND key='rules_v3_since'").fetchone()
    assert stamp is not None and float(stamp["value"]) > 0
    # 新索引都在
    idx = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_psyche_dec_child_time", "idx_sched_child_kind"} <= idx
    # 重开幂等(迁移不重跑不炸)
    c.close()
    pdb.connect(p).close()


def test_v8_child_policy_not_rewritten(tmp_path):
    """老孩子的 policy 版号迁移后原样:阶段表不被升级悄改。"""
    p = _make_v8_db(str(tmp_path / "old.db"))
    c = pdb.connect(p)
    child = child_mod.get_child(c, "oldkid")
    assert child["stage_policy_version"] == 1
    # v1 时间轴:40 天=adult(要是被悄改成 v2 这里会是 teen)
    assert child_mod.stage_of(child, T0 + 40 * DAY) == "adult"
    c.close()


# ── 管理面:--pause/--resume ──

def test_admin_freeze_pause_resume(saves):
    driver.init_birth("papa", "囡", now=T0)
    out = json.loads(driver.admin_freeze("papa", now=T0 + 100))
    assert out["paused"] is True and out["already"] is False
    out2 = json.loads(driver.admin_freeze("papa", now=T0 + 200))
    assert out2["already"] is True and out2["paused_at"] == T0 + 100
    out3 = json.loads(driver.admin_freeze("papa", resume=True, now=T0 + 300))
    assert out3["paused"] is False
    assert out3["total_paused_seconds"] == pytest.approx(200.0)


def test_admin_freeze_no_child_and_embryo(saves):
    assert json.loads(driver.admin_freeze("papa"))["error"] == "no_child"
    driver.init_birth("papa", None, now=T0, embryo=True)
    assert "没有年龄可冻" in json.loads(driver.admin_freeze("papa"))["error"]


def test_pause_not_on_nursery_face(saves):
    """--pause 是管理旗,照护指令面/帮助文本零暴露。"""
    assert not any("pause" in cmds or "resume" in cmds
                   for cmds in driver.STAGE_ACTIONS.values())
    driver.init_birth("papa", "囡", now=T0)
    out = driver.run("papa", ["pause"], now=T0 + 60)
    assert "冻" not in out  # 未知指令走 help/拒绝,不触发冻龄
    conn = pdb.connect(driver._db_path("papa"))
    assert conn.execute("SELECT paused_at FROM child").fetchone()["paused_at"] is None
    conn.close()


def test_pause_gates_ending_until_resumed_and_matured(saves):
    """冻龄闸住结局:冻在毕业线前,墙钟越线不判;解冻走满逻辑天才判。"""
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    conn.execute("UPDATE child SET stage_policy_version=1")
    conn.commit()
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你。",
                          now=T0 + 60)
    conn.execute("UPDATE child_state SET intimacy=85, darkness=10 WHERE child_id=?",
                 (cid,))
    conn.commit()
    t_pause = T0 + 34 * DAY                      # teen 尾巴冻住(毕业线=37.5天)
    child_mod.pause_child(conn, cid, now=t_pause)
    t_late = t_pause + 30 * DAY                  # 墙钟跨过死线很久
    out = events.tick_events(conn, brain, cid, now=t_late)
    assert "ending" not in out
    child = child_mod.get_child(conn, cid)
    assert child_mod.stage_of(child, t_late) == "teen"
    assert child["status"] == "active" and not child["ending"]
    # 解冻:逻辑年龄从 34 天续走,未满毕业线仍不判;走满+亲口 farewell 才判
    child_mod.resume_child(conn, cid, now=t_late)
    assert events.judge_ending(conn, brain, cid, now=t_late + 1 * DAY) is None
    assert events.judge_ending(conn, brain, cid, now=t_late + 4 * DAY - 120) is None
    child_mod.apply_action(conn, cid, "papa", "farewell",
                           idempotency_key="fw3", now=t_late + 4 * DAY - 60)
    assert events.judge_ending(conn, brain, cid,
                               now=t_late + 4 * DAY) == "reconciled"
    conn.close()


# ── 管理面:--set-policy(阶段表升版,老玩家给孩子续青春期的唯一入口) ──

def test_stage_policy_v2_lookup_and_migration(saves):
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    # 新档默认 v2:40 天=teen,49 天=adult
    child = child_mod.get_child(conn, cid)
    assert child["stage_policy_version"] == 2
    assert child_mod.stage_of(child, T0 + 40 * DAY) == "teen"
    assert child_mod.stage_of(child, T0 + 49 * DAY) == "adult"
    # 降版禁止(单向升版契约)/幂等/未知版拒绝
    assert json.loads(driver.admin_set_policy("papa", 1))["error"] == \
        "downgrade_forbidden"
    assert json.loads(driver.admin_set_policy("papa", 2))["already"] is True
    assert "unknown_policy" in json.loads(driver.admin_set_policy("papa", 7))["error"]
    conn.close()


def test_stage_policy_upgrade_v1_to_v2_with_regress_guard(saves):
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    conn.execute("UPDATE child SET stage_policy_version=1")
    conn.commit()
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    # v1 下 40 天=adult:升 v2 会倒回 teen → 拒绝(阶段史不倒序)
    out = json.loads(driver.admin_set_policy("papa", 2, now=T0 + 40 * DAY))
    assert out["error"] == "stage_would_regress" and out["from"] == "adult"
    # v1 下 34 天=teen,v2 仍 teen → 升版放行(老玩家续青春期的真实路径)
    out2 = json.loads(driver.admin_set_policy("papa", 2, now=T0 + 34 * DAY))
    assert out2["policy"] == 2 and out2["already"] is False
    child = child_mod.get_child(conn, cid)
    assert child_mod.stage_of(child, T0 + 40 * DAY) == "teen"
    # graduated 锁死
    conn.execute("UPDATE child SET status='graduated', stage_policy_version=1")
    conn.commit()
    assert json.loads(driver.admin_set_policy("papa", 2))["error"] == \
        "graduated_locked"
    conn.close()


def test_unknown_policy_version_fails_closed(saves):
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    conn.execute("UPDATE child SET stage_policy_version=99")
    conn.commit()
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    child = child_mod.get_child(conn, cid)
    with pytest.raises(ValueError):
        child_mod.stage_of(child, T0 + DAY)   # 坏档炸响不悄改语义
    conn.close()
