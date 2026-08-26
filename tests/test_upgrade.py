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
    # v8 当年还没有这两个索引(_SCHEMA 是「最新全量」,老档形状要拆干净)
    raw.execute("DROP INDEX IF EXISTS idx_psyche_dec_child_time")
    raw.execute("DROP INDEX IF EXISTS idx_sched_child_kind")
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


# ── 不翻旧账:升级前偷学语料不触发 swear 两难 ──

def test_pre_upgrade_swear_corpus_never_fires(tmp_path):
    from nursery import choices
    p = _make_v8_db(str(tmp_path / "old.db"))
    conn = pdb.connect(p)   # 打开即迁移,钉下 rules_v3_since
    # 把升级戳对齐到夹具时间轴(迁移写的是真实墙钟;语义不变:戳=升级时刻)
    t_upgrade = T0 + 10 * DAY
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE parenting_meta SET value=? WHERE child_id='oldkid'"
                 " AND key='rules_v3_since'", (repr(t_upgrade),))
    # 升级前偷学的语料,含词表词
    conn.execute(
        "INSERT INTO corpus_item(child_id, source_kind, speaker, text,"
        " content_hash, tokenizer_version, char_count, training_weight,"
        " acquired_at) VALUES('oldkid','archive','papa',"
        "'他小声说了句卧槽然后跑了','h1','v1',12,1,?)", (t_upgrade - 5 * DAY,))
    conn.commit()
    choices.plan_choices(conn, "oldkid", now=t_upgrade + 3 * DAY)
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_event WHERE kind='choice'"
        " AND idempotency_key LIKE 'choice:swear:%'").fetchone()[0] == 0
    # 升级之后新偷学的同词=正常触发(不翻旧账≠永不触发)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO corpus_item(child_id, source_kind, speaker, text,"
        " content_hash, tokenizer_version, char_count, training_weight,"
        " acquired_at) VALUES('oldkid','archive','papa',"
        "'又听见一句卧槽这词真难甩','h2','v1',12,1,?)", (t_upgrade + 4 * DAY,))
    conn.commit()
    choices.plan_choices(conn, "oldkid", now=t_upgrade + 4 * DAY + 60)
    assert conn.execute(
        "SELECT COUNT(*) FROM scheduled_event WHERE kind='choice'"
        " AND idempotency_key LIKE 'choice:swear:%'").fetchone()[0] == 1
    conn.close()


# ── tick 自守闸:ask/choice/chain 任何一件炸了,events/outbox 照跑 ──

@pytest.mark.parametrize("mod_name,fn_name", [
    ("nursery.asks", "plan_asks"),
    ("nursery.choices", "plan_choices"),
    ("nursery.chains", "plan_chains"),
])
def test_tick_survives_new_mechanism_failure(saves, monkeypatch,
                                             mod_name, fn_name):
    import importlib
    from nursery import scheduler
    driver.init_birth("papa", "囡", now=T0)
    mod = importlib.import_module(mod_name)

    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(mod, fn_name, _boom)
    out = scheduler.tick_one(str(saves / "papa" / "nursery.db"), "papa",
                             now=T0 + 3600)
    # 炸的那件空手而归,但事件系统与投递面照常存在
    assert "events" in out and "outbox" in out


# ── 升级当日不翻旧账:被晾窗与唠叨免费额都从升级戳起算 ──

def test_upgrade_day_quiet_window_starts_at_stamp(tmp_path):
    from nursery import friction
    import time as _time
    p = _make_v8_db(str(tmp_path / "old.db"))
    conn = pdb.connect(p)
    # 用真实墙钟摆一个「今天 21:30、白天全程没人理」的场景:
    # 升级戳=今天 15:00 → 只有 15:00-21:30 算窗(6.5h≥6h 会hit),
    # 但把戳挪到 16:00 → 窗只剩 5.5h(<6h)= 不该记「被晾」
    lt = _time.localtime()
    day0 = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    t_eval = day0 + 21.5 * 3600
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE parenting_meta SET value=? WHERE child_id='oldkid'"
                 " AND key='rules_v3_since'", (repr(day0 + 16 * 3600),))
    # 让他在 teen(v1 表 24-36 天):born_at 挪到 30 天前
    conn.execute("UPDATE child SET born_at=? WHERE child_id='oldkid'",
                 (t_eval - 30 * DAY,))
    conn.commit()
    assert friction._quiet_annoyance(conn, "oldkid", t_eval) is False
    # 戳挪回 15:00:窗 6.5h ≥ 阈值,正常记账(升级后被晾照算)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE parenting_meta SET value=? WHERE child_id='oldkid'"
                 " AND key='rules_v3_since'", (repr(day0 + 15 * 3600),))
    conn.commit()
    assert friction._quiet_annoyance(conn, "oldkid", t_eval) is True
    conn.close()


def test_upgrade_day_nag_quota_ignores_pre_upgrade_actions(tmp_path):
    p = _make_v8_db(str(tmp_path / "old.db"))
    conn = pdb.connect(p)
    t_upgrade = T0 + 25 * DAY + 12 * 3600   # teen 期某天中午升级
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE parenting_meta SET value=? WHERE child_id='oldkid'"
                 " AND key='rules_v3_since'", (repr(t_upgrade),))
    conn.commit()
    # 升级前同日 talk 三次(免费额=3;若被计入,升级后第一次 talk 就该攒烦)
    for i in range(3):
        child_mod.apply_action(conn, "oldkid", "papa", "talk",
                               idempotency_key=f"pre{i}",
                               now=t_upgrade - 3600 + i * 60)
    child_mod.apply_action(conn, "oldkid", "papa", "talk",
                           idempotency_key="post1", now=t_upgrade + 600)
    st = child_mod.read_state(conn, "oldkid", now=t_upgrade + 600, persist=False)
    assert st.get("annoyance", 0.0) == 0.0   # 升级前的唠叨不翻旧账
    conn.close()


def test_upgrade_day_tattle_ignores_pre_upgrade_actions(tmp_path):
    """告状稿的当日计数窗也从升级戳起算:升级前同日的 discipline 不进告状。"""
    from nursery import asks
    p = _make_v8_db(str(tmp_path / "old.db"))
    conn = pdb.connect(p)
    t_upgrade = T0 + 6 * DAY + 14 * 3600     # 某天 14:00 升级
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE parenting_meta SET value=? WHERE child_id='oldkid'"
                 " AND key='rules_v3_since'", (repr(t_upgrade),))
    conn.commit()
    # 升级前同日 10:00 有一条管教
    child_mod.apply_action(conn, "oldkid", "papa", "discipline",
                           idempotency_key="pre-disc", now=t_upgrade - 4 * 3600)
    line = asks.derive_tattle(conn, "oldkid", "mama", t_upgrade + 3600)
    from nursery import texts
    assert texts.TATTLE_MAMA_DISC not in line   # 升级前的凶不进状纸
    # 升级后再凶一次=照常告状(不翻旧账≠不告状)
    child_mod.apply_action(conn, "oldkid", "papa", "discipline",
                           idempotency_key="post-disc", now=t_upgrade + 7200)
    line2 = asks.derive_tattle(conn, "oldkid", "mama", t_upgrade + 7300)
    assert texts.TATTLE_MAMA_DISC in line2
    conn.close()


def test_embryo_hatched_after_upgrade_gets_current_policy(tmp_path):
    """v0.2 存量 embryo 在 v0.3 孵化=此刻才出生,按当前阶段表(v2)走。"""
    p = _make_v8_db(str(tmp_path / "old2.db"))
    conn = pdb.connect(p)
    eid = child_mod.create_child(conn, "papa", status="embryo", now=T0)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE child SET stage_policy_version=1 WHERE child_id=?",
                 (eid,))   # 模拟 v0.2 时代建的 embryo 档
    conn.commit()
    child_mod.hatch_child(conn, eid, name="蛋蛋", now=T0 + 100 * DAY)
    child = child_mod.get_child(conn, eid)
    assert child["stage_policy_version"] == 2
    assert child_mod.stage_of(child, T0 + 100 * DAY + 40 * DAY) == "teen"  # v2:48 线
    conn.close()
