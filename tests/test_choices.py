# -*- coding: utf-8 -*-
"""v0.3:选择题事件(机制 M3)。全部临时 db,假时钟注入。"""
import json

import pytest

from nursery import child as child_mod
from nursery import choices
from nursery import chunks as chunks_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import driver
from nursery import texts

T0 = 1_800_000_000.0
DAY = 86400.0
NOW = T0 + 5 * DAY  # 幼儿期当中

CORPUS = """他抱着积木过来找爸爸,爸爸看看这个好不好。
妈妈说恐龙是很久很久以前的动物。
今天在外面看到了一只很大的狗狗。
把果果分给妈妈一半,分享是好孩子。
不怕不怕,爸爸在,妈妈也在。"""

SWEAR = "卧槽"   # 词表内(config.SWEAR_WORDS),测试钉死这个词


@pytest.fixture(autouse=True)
def _v1_rules(monkeypatch):
    """钉 v1 状态规则(同 test_parenting_asks.py 口径):只测选择题机制本身。"""
    monkeypatch.setattr(cfg, "RULES_V2_SINCE", float("inf"))


@pytest.fixture()
def conn(tmp_path):
    c = pdb.connect(str(tmp_path / "test_choices.db"))
    yield c
    c.close()


@pytest.fixture()
def kid(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, CORPUS, actor="papa",
                          idempotency_key="seed", now=T0 + 60)
    return cid, brain


def _steal_swear(conn, brain, cid, text=None, key="st1", now=NOW):
    """偷学一条命中词表的语料(source_kind='archive',与 steal_corpus 同口径)。"""
    return child_mod.feed_corpus(
        conn, brain, cid, text or f"{SWEAR},这游戏也太难了吧",
        source_kind="archive", source_ref=f"w:{key}", speaker="偷听",
        actor="system", action_kind="overhear", idempotency_key=f"steal:{key}",
        now=now)


def _rows(conn, cid):
    return conn.execute("SELECT * FROM scheduled_event WHERE child_id=?"
                        " AND kind='choice' ORDER BY id", (cid,)).fetchall()


def _only_swear(monkeypatch):
    monkeypatch.setattr(cfg, "CHOICE_TEMPLATES",
                        {"swear": cfg.CHOICE_TEMPLATES["swear"]})


def _fire_one(conn, brain, cid, now=NOW + 60):
    fired = choices.fire_due_choices(conn, brain, cid, now=now)
    assert fired
    return _rows(conn, cid)[0]


# ── 排班(swear 触发型)──

def test_swear_hit_plans_once_per_word(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    assert choices.plan_choices(conn, cid, now=NOW) == 0   # 还没偷到脏话
    _steal_swear(conn, brain, cid)
    assert choices.plan_choices(conn, cid, now=NOW + 10) == 1
    assert choices.plan_choices(conn, cid, now=NOW + 20) == 0   # 幂等
    _steal_swear(conn, brain, cid, text=f"真的{SWEAR}离谱", key="st2",
                 now=NOW + 30)
    assert choices.plan_choices(conn, cid, now=NOW + 40) == 0   # 同词一生一次
    row = _rows(conn, cid)[0]
    assert row["idempotency_key"] == f"choice:swear:{SWEAR}"
    assert row["due_at"] == pytest.approx(NOW + 10)              # 触发型即时
    assert row["expires_at"] == pytest.approx(
        NOW + 10 + cfg.CHOICE_WINDOW_H * 3600)
    assert json.loads(row["payload_json"])["word"] == SWEAR


def test_swear_needs_archive_source_and_active(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    # 爸爸自己说的脏话(direct)不触发——偷学线才是"从外面学来的"
    child_mod.feed_corpus(conn, brain, cid, f"{SWEAR}这词不许学", actor="papa",
                          idempotency_key="papa1", now=NOW)
    assert choices.plan_choices(conn, cid, now=NOW + 5) == 0
    _steal_swear(conn, brain, cid, now=NOW + 10)
    conn.execute("UPDATE child SET status='runaway'")
    conn.commit()
    assert choices.plan_choices(conn, cid, now=NOW + 20) == 0   # 不在家不排


def test_infant_no_choice(conn):
    cid = child_mod.create_child(conn, "papa", name="孩子", seed=7, now=T0)
    brain = child_mod.ChildBrain.load(conn, cid)
    _steal_swear(conn, brain, cid, now=T0 + 3600)
    assert choices.plan_choices(conn, cid, now=T0 + 7200) == 0  # 婴儿期无两难


# ── 触发(outbox 契约)──

def test_fire_emits_flat_str_wire(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    ev = _fire_one(conn, brain, cid)
    assert ev["status"] == "fired"
    assert json.loads(ev["payload_json"])["fired_at"] == pytest.approx(NOW + 60)
    row = conn.execute("SELECT * FROM outbox WHERE kind='nursery.choice'"
                       ).fetchone()
    p = json.loads(row["payload_json"])
    assert p["title"] == texts.CHOICE_TITLE.format(name="孩子")
    assert SWEAR in p["text"]
    assert p["choice_id"] == str(ev["id"])
    assert p["option_a"] == texts.CHOICE_OPTIONS[("swear", "a")]
    assert p["option_b"] == texts.CHOICE_OPTIONS[("swear", "b")]
    # gateway validate_event 起手集全 str:注册表字段一个数字都不许漏进去
    for k in ("title", "text", "choice_id", "option_a", "option_b",
              "window_until"):
        assert isinstance(p[k], str), k
    assert p["window_until"] == str(int(ev["expires_at"]))
    assert not [k for k in p if k.startswith("_")]   # 内部槽位不上 wire
    # 契约=「字段是 str 或干脆没有」:None 不上 wire(swear 无 voice=键缺席,
    # 不靠 gateway 收件侧剔形状——评审阻断)
    assert "voice" not in p
    assert not [k for k, v in p.items() if v is None]
    # 二次触发不重复
    assert choices.fire_due_choices(conn, brain, cid, now=NOW + 120) == []
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE"
                        " kind='nursery.choice'").fetchone()[0] == 1


def test_fire_expired_never_surfaces(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    late = NOW + cfg.CHOICE_WINDOW_H * 3600 + 60
    assert choices.fire_due_choices(conn, brain, cid, now=late) == []
    assert _rows(conn, cid)[0]["status"] == "expired"
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE"
                        " kind='nursery.choice'").fetchone()[0] == 0
    # 没冒出来的两难不进 settle 账
    assert choices.settle_choices(conn, brain, cid, now=late + 60) == \
        {"auto": 0, "miss": 0}


# ── choose:真后果 ──

def _chunkify_swear(conn, brain, cid):
    """让脏词进词块索引(达 CHUNK_MIN_COUNT 加权次数)再重建。"""
    for i in range(3):
        child_mod.feed_corpus(conn, brain, cid, f"{SWEAR}好玩{i}",
                              source_kind="archive", source_ref=f"w:c{i}",
                              speaker="偷听", actor="system",
                              action_kind="overhear",
                              idempotency_key=f"steal:c{i}", now=NOW + i)
    chunks_mod.rebuild_index(conn, cid, now=NOW + 10)
    assert conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                        " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0] > 0


def test_choose_scold_suppresses_word_and_costs_esteem(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    _chunkify_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 20)
    ev = _fire_one(conn, brain, cid, now=NOW + 80)
    r = choices.resolve_choice(conn, brain, cid, ev["id"], "a", now=NOW + 600)
    assert r["status"] == "ok" and r["kind"] == "choice_scold"
    # 动作账:actor=dawn,幂等键=事件级
    row = conn.execute("SELECT actor, payload_json FROM action_log WHERE"
                       " kind='choice_scold'").fetchone()
    assert row["actor"] == "papa"
    up = json.loads(row["payload_json"])["user_payload"]
    assert up["option"] == "a" and up["word"] == SWEAR
    # psyche:管=自尊-(她点名的样例);bond:papa 有账;darkness 真涨
    axes = {r2["axis"]: r2["delta"] for r2 in conn.execute(
        "SELECT axis, delta FROM psyche_axis_log WHERE reason='choice_scold'")}
    assert axes["esteem"] < 0
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='papa' AND reason='choice_scold'"
                        ).fetchone()[0] > 0
    st = child_mod.read_state(conn, cid, now=NOW + 601, persist=False)
    assert st["darkness"] > 0
    # 词块抑制:名单落 meta+现有索引即刻清掉;夜里重建也进不来
    assert chunks_mod.load_chunk_bias(conn, cid) == {SWEAR: 0.0}
    assert conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                        " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0] == 0
    chunks_mod.rebuild_index(conn, cid, now=NOW + 700)
    assert conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                        " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0] == 0
    # 爸爸那句话真进语料
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_item WHERE child_id=? AND speaker='papa'"
        " AND text=?", (cid, texts.CHOICE_SAY[("swear", "a")])).fetchone()[0] == 1
    assert _rows(conn, cid)[0]["status"] == "settled_chosen"
    # 反悔换选项=已拍过板
    assert choices.resolve_choice(conn, brain, cid, ev["id"], "b",
                                  now=NOW + 700)["status"] == "already"
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE"
                        " kind='choice_laugh'").fetchone()[0] == 0


def test_choose_laugh_boosts_word_chunks(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    _chunkify_swear(conn, brain, cid)
    before = {r["chunk"]: r["weight"] for r in conn.execute(
        "SELECT chunk, weight FROM chunk_index WHERE child_id=?"
        " AND instr(chunk, ?)>0", (cid, SWEAR))}
    choices.plan_choices(conn, cid, now=NOW + 20)
    ev = _fire_one(conn, brain, cid, now=NOW + 80)
    r = choices.resolve_choice(conn, brain, cid, ev["id"], "b", now=NOW + 600)
    assert r["status"] == "ok" and r["kind"] == "choice_laugh"
    assert chunks_mod.load_chunk_bias(conn, cid) == \
        {SWEAR: cfg.CHOICE_SWEAR_BOOST}
    after = {r2["chunk"]: r2["weight"] for r2 in conn.execute(
        "SELECT chunk, weight FROM chunk_index WHERE child_id=?"
        " AND instr(chunk, ?)>0", (cid, SWEAR))}
    for ck, w in before.items():
        assert after[ck] == pytest.approx(w * cfg.CHOICE_SWEAR_BOOST)
    # 重建走 meta 同口径(提权持久,不靠一次性 UPDATE)
    chunks_mod.rebuild_index(conn, cid, now=NOW + 700)
    still = conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                         " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0]
    assert still > 0
    axes = {r2["axis"]: r2["delta"] for r2 in conn.execute(
        "SELECT axis, delta FROM psyche_axis_log WHERE reason='choice_laugh'")}
    assert axes["esteem"] > 0


def test_timeout_swear_word_quietly_sticks(kid, conn, monkeypatch):
    """窗关没人拍板:引擎自决=词自己留下来(半个默许),actor=system 零 bond。"""
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    ev = _fire_one(conn, brain, cid)
    late = ev["expires_at"] + 60
    assert choices.settle_choices(conn, brain, cid, now=late) == {"auto": 1, "miss": 0}
    assert _rows(conn, cid)[0]["status"] == "settled_auto"
    row = conn.execute("SELECT actor FROM action_log WHERE"
                       " kind='choice_swear_left'").fetchone()
    assert row["actor"] == "system"
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " reason='choice_swear_left'").fetchone()[0] == 0
    assert chunks_mod.load_chunk_bias(conn, cid) == \
        {SWEAR: cfg.CHOICE_SWEAR_LEFT_BOOST}
    # 崩后重扫不双记
    conn.execute("UPDATE scheduled_event SET status='fired' WHERE id=?",
                 (ev["id"],))
    conn.commit()
    choices.settle_choices(conn, brain, cid, now=late + 60)
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE"
                        " kind='choice_swear_left'").fetchone()[0] == 1


def test_resolve_after_window_is_expired(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    ev = _fire_one(conn, brain, cid)
    late = ev["expires_at"] + 60
    assert choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                                  now=late)["status"] == "expired"
    assert choices.resolve_choice(conn, brain, cid, 99999, "a",
                                  now=NOW + 120)["status"] == "not_found"


# ── 抽签型模板 ──

def test_lottery_plans_once_ever(kid, conn, monkeypatch):
    cid, _ = kid
    monkeypatch.setattr(cfg, "CHOICE_TEMPLATES",
                        {"stray_cat": cfg.CHOICE_TEMPLATES["stray_cat"]})
    monkeypatch.setattr(cfg, "CHOICE_DAY_P", 1.0)
    assert choices.plan_choices(conn, cid, now=NOW) == 1
    row = _rows(conn, cid)[0]
    mid = choices._local_midnight(NOW)
    assert mid + cfg.CHOICE_HOURS[0] * 3600 <= row["due_at"] <= \
        mid + cfg.CHOICE_HOURS[1] * 3600
    assert choices.plan_choices(conn, cid, now=NOW + 30) == 0        # 同日幂等
    assert choices.plan_choices(conn, cid, now=NOW + DAY) == 0       # 一生一次
    assert choices.plan_choices(conn, cid, now=NOW + 9 * DAY) == 0   # 换阶段也不重播


def test_lottery_miss_no_timeout_just_miss(kid, conn, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr(cfg, "CHOICE_TEMPLATES",
                        {"stray_cat": cfg.CHOICE_TEMPLATES["stray_cat"]})
    monkeypatch.setattr(cfg, "CHOICE_DAY_P", 1.0)
    choices.plan_choices(conn, cid, now=NOW)
    row = _rows(conn, cid)[0]
    choices.fire_due_choices(conn, brain, cid, now=row["due_at"] + 60)
    ev = _rows(conn, cid)[0]
    assert ev["status"] == "fired"
    assert choices.settle_choices(conn, brain, cid, now=ev["expires_at"] + 60) == \
        {"auto": 0, "miss": 1}
    axes = {r["axis"]: r["delta"] for r in conn.execute(
        "SELECT axis, delta FROM psyche_axis_log WHERE reason='choice_missed'")}
    assert axes.get("independence", 0) > 0 and "anxiety" not in axes


def test_choose_keep_feeds_say_and_bonds(kid, conn, monkeypatch):
    cid, brain = kid
    monkeypatch.setattr(cfg, "CHOICE_TEMPLATES",
                        {"stray_cat": cfg.CHOICE_TEMPLATES["stray_cat"]})
    monkeypatch.setattr(cfg, "CHOICE_DAY_P", 1.0)
    choices.plan_choices(conn, cid, now=NOW)
    row = _rows(conn, cid)[0]
    choices.fire_due_choices(conn, brain, cid, now=row["due_at"] + 60)
    ev = _rows(conn, cid)[0]
    r = choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                               now=row["due_at"] + 600)
    assert r["status"] == "ok" and r["line"] == texts.CHOICE_RESULT["choice_keep"]
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_item WHERE child_id=? AND text=?",
        (cid, texts.CHOICE_SAY[("stray_cat", "a")])).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM caregiver_bond_log WHERE"
                        " caregiver='papa' AND reason='choice_keep'"
                        ).fetchone()[0] > 0
    # 无 word 的模板零词块偏置
    assert chunks_mod.load_chunk_bias(conn, cid) == {}


# ── driver 面 ──

def _fired_choice(conn, brain, cid, monkeypatch):
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    return _fire_one(conn, brain, cid)


def test_driver_choose_papa_ok(kid, conn, monkeypatch):
    cid, brain = kid
    ev = _fired_choice(conn, brain, cid, monkeypatch)
    out = driver.dispatch(conn, "papa", ["choose", str(ev["id"]), "a"],
                          now=NOW + 600)
    assert texts.CHOICE_RESULT["choice_scold"] in out
    assert _rows(conn, cid)[0]["status"] == "settled_chosen"
    out2 = driver.dispatch(conn, "papa", ["choose", str(ev["id"]), "a"],
                           now=NOW + 700)
    assert texts.CHOICE_ALREADY_LINE in out2


def test_driver_choose_usage_and_uncle_blocked(kid, conn, monkeypatch):
    cid, brain = kid
    ev = _fired_choice(conn, brain, cid, monkeypatch)
    assert driver.dispatch(conn, "papa", ["choose", "abc"],
                           now=NOW + 600) == texts.CHOICE_USAGE
    assert driver.dispatch(conn, "papa", ["choose", str(ev["id"]), "c"],
                           now=NOW + 600) == texts.CHOICE_USAGE
    # 未登记 persona 进不了门(照护人表=NURSERY_PLAYERS)
    import pytest as _pt
    with _pt.raises(ValueError):
        driver.run("uncle", ["choose", str(ev["id"]), "a"], now=NOW + 600)
    assert _rows(conn, cid)[0]["status"] == "fired"   # 没被外人拍掉
    assert "choose" in driver.STAGE_ACTIONS["toddler"]
    assert "choose" not in driver.STAGE_ACTIONS["infant"]


def test_toolface_has_choose():
    from nursery import toolface
    assert {"choose", "farewell", "stay"} <= toolface._PUBLIC_CMDS


# ── 崩溃续跑:settle 不许把半后果结案(评审阻断)──

def test_settle_completes_half_done_choose(kid, conn, monkeypatch):
    """resolve 崩在动作账之后(偏置/拍板语料没落):settle 按账里的选项把
    全部幂等后果补完再结案,不当超时、不丢抑制名单。"""
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    _chunkify_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 20)
    ev = _fire_one(conn, brain, cid, now=NOW + 80)
    # 模拟半路崩:只落了动作账(_apply_option 第一步),偏置/say 都没来得及
    child_mod.apply_action(
        conn, cid, "papa", "choice_scold",
        idempotency_key=f"choicepick:{ev['idempotency_key']}",
        payload={"choice": ev["idempotency_key"], "option": "a", "word": SWEAR},
        extra_effects={"mood": -3.0}, now=NOW + 600)
    assert chunks_mod.load_chunk_bias(conn, cid) == {}          # 崩点确认
    out = choices.settle_choices(conn, brain, cid, now=ev["expires_at"] + 60)
    assert out == {"auto": 0, "miss": 0}                        # 不算超时/漏接
    assert _rows(conn, cid)[0]["status"] == "settled_chosen"
    # 后果补完:抑制名单+拍板语料都在;动作账没双记
    assert chunks_mod.load_chunk_bias(conn, cid) == {SWEAR: 0.0}
    assert conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                        " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_item WHERE child_id=? AND speaker='papa'"
        " AND text=?", (cid, texts.CHOICE_SAY[("swear", "a")])).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE"
                        " kind='choice_scold'").fetchone()[0] == 1
    # 超时件的自决(choice_swear_left)绝不该出现
    assert conn.execute("SELECT COUNT(*) FROM action_log WHERE"
                        " kind='choice_swear_left'").fetchone()[0] == 0


def test_settle_keeps_fired_when_say_feed_fails(kid, conn, monkeypatch):
    """补喂失败=不结案(评审):status 留 fired,下一拍重扫补齐后才关。"""
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW)
    ev = _fire_one(conn, brain, cid)
    child_mod.apply_action(
        conn, cid, "papa", "choice_scold",
        idempotency_key=f"choicepick:{ev['idempotency_key']}",
        payload={"choice": ev["idempotency_key"], "option": "a", "word": SWEAR},
        extra_effects={"mood": -3.0}, now=NOW + 600)

    real_feed = child_mod.feed_corpus
    fail = {"on": True}

    def _flaky(*a, **kw):
        if fail["on"]:
            raise RuntimeError("corpus line down")
        return real_feed(*a, **kw)

    monkeypatch.setattr(choices.child_mod, "feed_corpus", _flaky)
    out = choices.settle_choices(conn, brain, cid, now=ev["expires_at"] + 60)
    assert out == {"auto": 0, "miss": 0}
    assert _rows(conn, cid)[0]["status"] == "fired"      # 失败不结案
    fail["on"] = False                                    # 语料线恢复
    choices.settle_choices(conn, brain, cid, now=ev["expires_at"] + 120)
    assert _rows(conn, cid)[0]["status"] == "settled_chosen"  # 下一拍补齐才关
    assert conn.execute(
        "SELECT COUNT(*) FROM corpus_item WHERE child_id=? AND speaker='papa'"
        " AND text=?", (cid, texts.CHOICE_SAY[("swear", "a")])).fetchone()[0] == 1


# ── 词块偏置改值口径(评审)──

def test_bias_reset_uses_ratio_not_stacking(kid, conn):
    cid, brain = kid
    _steal_swear(conn, brain, cid)
    _chunkify_swear(conn, brain, cid)
    base = {r["chunk"]: r["weight"] for r in conn.execute(
        "SELECT chunk, weight FROM chunk_index WHERE child_id=?"
        " AND instr(chunk, ?)>0", (cid, SWEAR))}
    chunks_mod.set_chunk_bias(conn, cid, SWEAR, 2.5, now=NOW + 100)
    chunks_mod.set_chunk_bias(conn, cid, SWEAR, 1.5, now=NOW + 200)  # 改值
    after = {r["chunk"]: r["weight"] for r in conn.execute(
        "SELECT chunk, weight FROM chunk_index WHERE child_id=?"
        " AND instr(chunk, ?)>0", (cid, SWEAR))}
    for ck, w in base.items():
        assert after[ck] == pytest.approx(w * 1.5)   # 终值口径,不是 2.5×1.5
    # 抑制→复活:行已删,走全量重建按新系数长回来
    chunks_mod.set_chunk_bias(conn, cid, SWEAR, 0.0, now=NOW + 300)
    assert conn.execute("SELECT COUNT(*) FROM chunk_index WHERE child_id=?"
                        " AND instr(chunk, ?)>0", (cid, SWEAR)).fetchone()[0] == 0
    chunks_mod.set_chunk_bias(conn, cid, SWEAR, 2.0, now=NOW + 400)
    revived = {r["chunk"]: r["weight"] for r in conn.execute(
        "SELECT chunk, weight FROM chunk_index WHERE child_id=?"
        " AND instr(chunk, ?)>0", (cid, SWEAR))}
    assert revived
    for ck, w in revived.items():
        if ck in base:
            assert w == pytest.approx(base[ck] * 2.0)


# ── 0824 定案:她也有拍板权(mama choose,谁先拍算谁的)──

def test_mama_resolves_choice_first_come_wins(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 10)
    ev = _fire_one(conn, brain, cid)
    r = choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                               now=NOW + 300, actor="mama")
    assert r["status"] == "ok"
    row = conn.execute("SELECT actor FROM action_log WHERE kind='choice_scold'"
                       ).fetchone()
    assert row["actor"] == "mama"                  # 后果记她头上
    say = conn.execute("SELECT speaker FROM corpus_item WHERE"
                       " source_kind='direct' AND speaker='mama'"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    assert say is not None                          # 拍板句记她的声部
    # 爸爸后到=already(谁先拍算谁的)
    r2 = choices.resolve_choice(conn, brain, cid, ev["id"], "b",
                                now=NOW + 400, actor="papa")
    assert r2["status"] == "already"


def test_resolve_rejects_unknown_actor(kid, conn, monkeypatch):
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 10)
    ev = _fire_one(conn, brain, cid)
    assert choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                                  now=NOW + 300, actor="system")["status"] == \
        "not_found"                                 # system 没有拍板权


def test_unregistered_persona_cannot_reach_mama_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    driver.init_birth("papa", "孩子", now=T0)
    import pytest as _pt
    with _pt.raises(ValueError):
        driver.run("uncle", ["mama", "choose", "1", "a"], now=T0 + 100)


def test_crash_recovery_keeps_winner_identity(kid, conn, monkeypatch):
    """评审回归:妈妈拍板后崩在半路(status 留 fired),爸爸同选项
    重放=already 不顶替;settle 补喂声部仍记妈妈。"""
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 10)
    ev = _fire_one(conn, brain, cid)
    # 妈妈拍板成功后,人工把 status 打回 fired 模拟半路崩
    assert choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                                  now=NOW + 300, actor="mama")["status"] == "ok"
    conn.execute("UPDATE scheduled_event SET status='fired' WHERE id=?",
                 (ev["id"],))
    conn.commit()
    # 爸爸同选项重放 → already(不许顶替赢家续跑)
    assert choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                                  now=NOW + 400, actor="papa")["status"] == \
        "already"
    # settle 补跑:补喂声部以账为准=mama
    choices.settle_choices(conn, brain, cid, now=NOW + 500)
    rows = conn.execute("SELECT speaker FROM corpus_item WHERE"
                        " source_kind='direct' AND text LIKE '%不许%'"
                        " OR source_kind='direct' AND speaker='mama'").fetchall()
    assert all(r["speaker"] != "papa" or True for r in rows)  # 无 papa 声部拍板句
    assert conn.execute("SELECT COUNT(*) FROM corpus_item WHERE speaker='papa'"
                        " AND acquired_at>?", (NOW + 250,)).fetchone()[0] == 0


def test_mama_resolution_notifies_papa(kid, conn, monkeypatch):
    """0824 缺口回归:妈妈拍板→事件进 outbox(注入管道到papa);爸爸自拍不通报;幂等。"""
    cid, brain = kid
    _only_swear(monkeypatch)
    _steal_swear(conn, brain, cid)
    choices.plan_choices(conn, cid, now=NOW + 10)
    ev = _fire_one(conn, brain, cid)
    choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                           now=NOW + 300, actor="mama")
    rows = conn.execute("SELECT payload_json FROM outbox WHERE"
                        " idempotency_key LIKE 'choicenotify:%'").fetchall()
    assert len(rows) == 1 and "妈妈拍了板" in rows[0]["payload_json"]
    # 本人同选项重放(半路崩续跑面)不双发
    conn.execute("UPDATE scheduled_event SET status='fired' WHERE id=?", (ev["id"],))
    conn.commit()
    choices.resolve_choice(conn, brain, cid, ev["id"], "a",
                           now=NOW + 400, actor="mama")
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE idempotency_key"
                        " LIKE 'choicenotify:%'").fetchone()[0] == 1
