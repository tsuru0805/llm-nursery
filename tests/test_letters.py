# -*- coding: utf-8 -*-
"""v0.4 成年书信线:排信/生成(LLM+无 key 降级)/写信/回信提前/记忆返流/探望/
信箱面/老档升级锚兜底。LLM 永不真调(mock 或走无 key 本地模板路)。"""
import json
import time

import pytest

from nursery import child as child_mod
from nursery import config as cfg
from nursery import db as pdb
from nursery import driver, events, letters, scheduler, texts
from nursery import letter_prompts as lp

DAY = 86400.0
T0 = time.mktime((2030, 1, 1, 12, 0, 0, 0, 0, -1))


def _day_at(n: int, hour: int) -> float:
    lt = time.localtime(T0)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + int(n),
                        hour, 0, 0, 0, 0, -1))


@pytest.fixture
def saves(tmp_path, monkeypatch):
    monkeypatch.setenv("NURSERY_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.delenv("NURSERY_ARCHIVE_DB", raising=False)
    monkeypatch.delenv("NURSERY_EVENT_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return tmp_path / "saves"


T_GRAD = None


@pytest.fixture
def away(saves):
    """已毕业进书信阶段的孩子(reconciled;告别=主照护人在窗内亲口)。"""
    global T_GRAD
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    child_mod.feed_corpus(conn, brain, cid, "睡吧睡吧,爸爸在这里陪着你。",
                          now=T0 + 60)
    child_mod.feed_corpus(conn, brain, cid, "煎鸡蛋的时候火不要开太大。",
                          now=T0 + 120)
    conn.execute("UPDATE child_state SET intimacy=85, darkness=10")
    conn.commit()
    events.check_stage_transition(conn, cid, now=_day_at(48, 13))
    t_win = _day_at(48, 21)
    events.tick_farewell_arc(conn, cid, now=t_win)
    T_GRAD = t_win + 3600
    child_mod.apply_action(conn, cid, "papa", "farewell", idempotency_key="fw",
                           now=T_GRAD)
    assert events.judge_ending(conn, brain, cid, now=T_GRAD + 60) == "reconciled"
    yield conn, cid, brain
    conn.close()


def _mock_ds(content="信到了。今天路过一家店,看到熟悉的点心。 囡"):
    calls = []

    def ds(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"content": content, "model": "mock-llm"}
    ds.calls = calls
    return ds


# ── 排程 ──

def test_schedule_first_letter_gap_and_idempotent(away):
    conn, cid, brain = away
    t = T_GRAD + 3600
    assert letters.schedule_next_letter(conn, cid, now=t) == 1
    assert letters.schedule_next_letter(conn, cid, now=t + 60) == 0
    row = conn.execute("SELECT due_at FROM letters WHERE direction='in'").fetchone()
    gap = (row["due_at"] - T_GRAD) / DAY
    assert cfg.FIRST_LETTER_GAP_DAYS[0] - 0.1 <= gap <= \
        cfg.FIRST_LETTER_GAP_DAYS[1] + 1.1
    lo, hi = cfg.LETTER_DELIVER_HOURS
    assert lo <= time.localtime(row["due_at"]).tm_hour < hi


def test_no_letters_before_graduation(saves):
    driver.init_birth("papa", "囡", now=T0)
    conn = pdb.connect(driver._db_path("papa"))
    cid = conn.execute("SELECT child_id FROM child").fetchone()["child_id"]
    assert letters.schedule_next_letter(conn, cid, now=T0 + 10 * DAY) == 0
    conn.close()


def test_legacy_graduated_without_farewell_gets_anchor(away):
    """v0.2 自动开奖时代的老档:graduated+ending 但没有 farewell 账——
    锚兜底 child.updated_at,升级后书信照来(不死寂)。"""
    conn, cid, brain = away
    conn.execute("DELETE FROM action_log WHERE kind='farewell'")
    conn.execute("DELETE FROM letters")
    conn.commit()
    assert letters.schedule_next_letter(conn, cid, now=T_GRAD + 7200) == 1


# ── 生成与投递 ──

def test_deliver_letter_daytime_with_outbox(away):
    conn, cid, brain = away
    letters.schedule_next_letter(conn, cid, now=T_GRAD + 3600)
    due = conn.execute("SELECT due_at FROM letters").fetchone()["due_at"]
    ds = _mock_ds()
    lt = time.localtime(due)
    night = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 3, 0, 0, 0, 0, -1))
    assert letters.deliver_due_letters(conn, brain, cid, now=night,
                                       ds_complete=ds) == 0   # 半夜不送
    noon = night + 9 * 3600
    assert letters.deliver_due_letters(conn, brain, cid, now=noon,
                                       ds_complete=ds) == 1
    row = conn.execute("SELECT * FROM letters WHERE direction='in'").fetchone()
    assert row["status"] == "delivered" and "点心" in row["body"]
    ob = conn.execute("SELECT payload_json FROM outbox WHERE"
                      " kind='nursery.letter'").fetchone()
    p = json.loads(ob["payload_json"])
    assert p["title"] == texts.LETTER_ARRIVE_TITLE and p["body"] == row["body"]
    assert "第一封信" in ds.calls[0]["prompt"]
    assert "是爸爸说的「去吧」" in ds.calls[0]["prompt"]
    assert ds.calls[0]["max_tokens"] == cfg.LETTER_DS_MAX_TOKENS


def test_no_key_local_template_letter(away):
    """开源独有:没配 LLM key=纯本地模板信照来(书信阶段绝不死寂)。"""
    conn, cid, brain = away
    letters.schedule_next_letter(conn, cid, now=T_GRAD + 3600)
    conn.execute("UPDATE letters SET due_at=?", (_day_at(53, 12),))
    conn.commit()
    n = letters.deliver_due_letters(conn, brain, cid, now=_day_at(53, 12))
    assert n == 1
    row = conn.execute("SELECT body, sources_json FROM letters WHERE"
                       " direction='in'").fetchone()
    assert row["body"].strip().endswith("囡")
    assert json.loads(row["sources_json"])["composer"] == "local_template"


def test_generation_failure_defers_never_drops(away):
    conn, cid, brain = away
    letters.schedule_next_letter(conn, cid, now=T_GRAD + 3600)
    due = conn.execute("SELECT due_at FROM letters").fetchone()["due_at"]

    def bad_ds(prompt, **kw):
        raise RuntimeError("llm_down")
    assert letters.deliver_due_letters(conn, brain, cid, now=due + 60,
                                       ds_complete=bad_ds) == 0
    row = conn.execute("SELECT * FROM letters").fetchone()
    assert row["status"] == "scheduled" and row["attempt_count"] == 1
    assert row["due_at"] >= due + cfg.LETTER_RETRY_H * 3600 - 120


# ── 写信与回信 ──

def test_write_letter_not_into_corpus(away):
    conn, cid, brain = away
    before = conn.execute("SELECT COUNT(*) FROM corpus_item").fetchone()[0]
    r = letters.write_letter(conn, cid, "papa", "最近怎么样?有点想你了。",
                             now=T_GRAD + 2 * 3600)
    assert r["ok"]
    assert conn.execute("SELECT COUNT(*) FROM corpus_item").fetchone()[0] == before


def test_reply_pulls_next_letter_earlier(away, monkeypatch):
    conn, cid, brain = away
    monkeypatch.setattr(cfg, "LETTER_REPLY_P", 1.0)
    letters.schedule_next_letter(conn, cid, now=T_GRAD + 3600)
    conn.execute("UPDATE letters SET due_at=?", (T_GRAD + 30 * DAY,))
    conn.commit()
    t_w = T_GRAD + 3 * 3600
    letters.write_letter(conn, cid, "papa", "想你了。", now=t_w)
    due = conn.execute("SELECT due_at FROM letters WHERE direction='in'"
                       ).fetchone()["due_at"]
    assert due <= t_w + (cfg.LETTER_REPLY_GAP_DAYS[1] + 1.1) * DAY
    assert due >= t_w + (cfg.LETTER_REPLY_GAP_DAYS[0] - 0.1) * DAY   # 绝不即时


def test_reply_never_schedules_into_past(away, monkeypatch):
    """停摆钳:写完信后 tick 停了很多天,补排的回信不许落在过去。"""
    conn, cid, brain = away
    monkeypatch.setattr(cfg, "LETTER_REPLY_P", 1.0)
    letters.write_letter(conn, cid, "papa", "想你。", now=T_GRAD + 3600)
    t_late = T_GRAD + 10 * DAY
    assert letters.schedule_next_letter(conn, cid, now=t_late) == 1
    due = conn.execute("SELECT due_at FROM letters WHERE direction='in'"
                       ).fetchone()["due_at"]
    assert due >= t_late + (cfg.LETTER_REPLY_GAP_DAYS[0] - 0.1) * DAY


def test_unanswered_letters_feed_sources_not_qna(away, monkeypatch):
    conn, cid, brain = away
    monkeypatch.setattr(cfg, "LETTER_MEMORY_P", 1.0)
    letters.write_letter(conn, cid, "papa", "最近怎么样?", now=T_GRAD + 3600)
    letters.write_letter(conn, cid, "mama", "记得吃饭。", now=T_GRAD + 7200)
    letters.schedule_next_letter(conn, cid, now=T_GRAD + 3 * 3600)
    conn.execute("UPDATE letters SET due_at=? WHERE direction='in'",
                 (_day_at(53, 12),))
    conn.commit()
    ds = _mock_ds()
    assert letters.deliver_due_letters(conn, brain, cid, now=_day_at(53, 12),
                                       ds_complete=ds) == 1
    src = json.loads(conn.execute(
        "SELECT sources_json FROM letters WHERE direction='in'").fetchone()[0])
    assert len(src["reply_to_ids"]) == 2          # 两封都进素材池(非一问一答)
    assert "memory_corpus_id" in src              # 童年素材返流留痕
    prompt = ds.calls[0]["prompt"]
    assert "记得吃饭" in prompt
    assert lp.MEMORY_BLOCK.split("{")[0] in prompt
    assert lp.INBOX_BLOCK.split("{")[0].strip() in prompt


# ── 探望 ──

def test_visit_two_acts_and_cooldown(away, monkeypatch):
    conn, cid, brain = away
    monkeypatch.setitem(cfg.LETTER_TONE["reconciled"], "visit_day_p", 1.0)
    t1 = _day_at(60, 12)
    assert letters.tick_visit(conn, cid, now=t1).get("visit_start") is True
    assert letters.tick_visit(conn, cid, now=t1 + 3600) == {}
    out = letters.tick_visit(conn, cid, now=t1 + cfg.VISIT_STAY_HOURS * 3600 + 60)
    assert out.get("visit_end") is True
    rows = [json.loads(r["payload_json"]) for r in conn.execute(
        "SELECT payload_json FROM outbox WHERE idempotency_key LIKE 'visit%'")]
    assert any(p["title"] == texts.VISIT_END_TITLE for p in rows)
    assert letters.tick_visit(conn, cid, now=t1 + 2 * DAY) == {}   # 冷却


def test_visit_window_allows_small_talk(saves, away, monkeypatch):
    """探望那天能说上话(talk/mama say),他走了又只剩写信。"""
    conn, cid, brain = away
    monkeypatch.setitem(cfg.LETTER_TONE["reconciled"], "visit_day_p", 1.0)
    t1 = _day_at(60, 12)
    assert letters.tick_visit(conn, cid, now=t1).get("visit_start") is True
    out = driver.run("papa", ["talk"], now=t1 + 3600)
    assert out != texts.AWAY_TALK_HINT
    r = json.loads(driver.run("papa", ["mama", "say", "回来啦。"],
                              now=t1 + 3700))
    assert r["ok"] and r.get("visiting") is True
    t2 = t1 + cfg.VISIT_STAY_HOURS * 3600 + 60
    letters.tick_visit(conn, cid, now=t2)
    assert driver.run("papa", ["talk"], now=t2 + 60) == texts.AWAY_TALK_HINT
    r = json.loads(driver.run("papa", ["mama", "say", "还在吗"], now=t2 + 120))
    assert r == {"ok": False, "error": "graduated"}


# ── driver / mama 信箱面 ──

def test_driver_mailbox_faces(saves, away):
    conn, cid, brain = away
    t = T_GRAD + 3600
    assert driver.run("papa", ["talk"], now=t) == texts.AWAY_TALK_HINT
    assert "还没来过信" in driver.run("papa", ["status"], now=t)
    assert "letters" in driver.run("papa", ["help"], now=t)
    out = driver.run("papa", ["write", "好好吃饭。"], now=t)
    assert out == texts.LETTER_SENT_REPLY
    assert "寄出" in driver.run("papa", ["letters"], now=t + 60)
    assert texts.AWAY_QUIET in driver.run("papa", ["feed", "喂话"], now=t)


def test_read_letter_marks_and_pages(away):
    """信箱三件:未读数/读某封(销未读)/翻页。"""
    conn, cid, brain = away
    t = T_GRAD + 3600
    for i in range(8):
        conn.execute(
            "INSERT INTO letters(child_id, direction, author, body, status,"
            " created_at, delivered_at, idempotency_key)"
            " VALUES(?,'in','self',?,'delivered',?,?,?)",
            (cid, f"第{i}封。", t + i, t + i, f"letter:in:t{i}"))
    conn.commit()
    box = letters.mailbox_summary(conn, cid, now=t + 100)
    assert box["unread"] == 8 and box["total"] == 8
    out = driver.run("papa", ["letters"], now=t + 100)
    assert "●" in out and "没拆的信:8 封" in out and "letters page 2" in out
    lid = conn.execute("SELECT id FROM letters ORDER BY id LIMIT 1"
                       ).fetchone()["id"]
    out = driver.run("papa", ["letters", str(lid)], now=t + 200)
    assert "第0封。" in out
    assert letters.mailbox_summary(conn, cid, now=t + 300)["unread"] == 7
    out = driver.run("papa", ["letters", "page", "2"], now=t + 400)
    assert out.count("#") == 2


def test_mama_mailbox_and_active_gate(saves, away):
    conn, cid, brain = away
    t = T_GRAD + 3600
    r = json.loads(driver.run("papa", ["mama", "write", "妈妈想你。"], now=t))
    assert r["ok"] and r["action"] == "write"
    r = json.loads(driver.run("papa", ["mama", "letters"], now=t + 60))
    assert r["ok"] and len(r["letters"]) == 1
    r = json.loads(driver.run("papa", ["mama", "hug"], now=t))
    assert r == {"ok": False, "error": "graduated"}   # 幼年动词照旧关门


def test_mama_write_blocked_while_active(saves):
    driver.init_birth("papa", "囡", now=T0)
    r = json.loads(driver.run("papa", ["mama", "write", "喂"], now=T0 + 3600))
    assert r == {"ok": False, "error": "not_away"}


# ── toolface 面(MCP 唯一入口;write/letters 参数必须直通,opus 审阻断1 回归)──

def test_toolface_letters_write_args_passthrough(saves, away):
    from nursery import toolface
    conn, cid, brain = away
    out = toolface.nursery("papa", "write 好好吃饭,别老熬夜。")
    assert out == texts.LETTER_SENT_REPLY
    assert conn.execute("SELECT COUNT(*) FROM letters WHERE direction='out'"
                        ).fetchone()[0] == 1
    t = T_GRAD + 3600
    conn.execute(
        "INSERT INTO letters(child_id, direction, author, body, status,"
        " created_at, delivered_at, idempotency_key)"
        " VALUES(?,'in','self','信正文。','delivered',?,?,'tfin1')", (cid, t, t))
    conn.commit()
    lid = conn.execute("SELECT id FROM letters WHERE direction='in'"
                       ).fetchone()["id"]
    assert "信正文。" in toolface.nursery("papa", f"letters {lid}")
    assert "这一页没有信" in toolface.nursery("papa", "letters page 9")


def test_toolface_write_length_gate_matches_config(saves, away):
    """toolface 上限跟 config 走:800 字信不被 700 老上限拦(opus 审建议3)。"""
    from nursery import toolface
    conn, cid, brain = away
    out = toolface.nursery("papa", "write " + "好" * 799)
    assert out == texts.LETTER_SENT_REPLY


def test_write_same_millisecond_no_integrity_error(away):
    """同毫秒双击:幂等键 bump 重试,不把 IntegrityError 冒给用户(opus 审建议2)。"""
    conn, cid, brain = away
    t = T_GRAD + 3600
    assert letters.write_letter(conn, cid, "papa", "一。", now=t)["ok"]
    assert letters.write_letter(conn, cid, "papa", "二。", now=t)["ok"]
    assert conn.execute("SELECT COUNT(*) FROM letters WHERE direction='out'"
                        ).fetchone()[0] == 2


def test_active_write_hint_not_unknown(saves):
    """active 时主照护人 write=「还没到时候」提示,不是「没有这个指令」。"""
    driver.init_birth("papa", "囡", now=T0)
    out = driver.run("papa", ["write", "喂"], now=T0 + 3600)
    assert out == texts.NOT_AWAY_HINT.format(name="囡")


# ── scheduler graduated 分支 ──

def test_tick_one_graduated_goes_letters_only(saves, away):
    conn, cid, brain = away
    out = scheduler.tick_one(str(driver._db_path("papa")), "papa",
                             now=T_GRAD + 3600)
    assert "letters" in out and "asks" not in out and "events" not in out
    assert conn.execute("SELECT COUNT(*) FROM letters WHERE direction='in'"
                        ).fetchone()[0] == 1   # 排上了下一封
    assert conn.execute("SELECT COUNT(*) FROM scheduled_event WHERE"
                        " kind='night_cry' AND due_at>?",
                        (T_GRAD,)).fetchone()[0] == 0


# ── 骨架完备性 ──

def test_prompt_skeletons_cover_all_endings():
    endings = {"reconciled", "independent", "silent", "precocious",
               "hidden_reunion"}
    assert set(lp.ENDING_TONE) == endings
    assert set(lp.LETTER_CHARS) == endings
    assert set(cfg.LETTER_TONE) == endings
    assert set(texts.ENDING_CN) == endings
