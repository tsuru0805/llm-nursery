# -*- coding: utf-8 -*-
"""v0.4 成年书信线:离家后的往来。

幼年是养育,成年是通信。graduated≠结束——他离家生活,低频来信;照护者可写信,
不即时回复,非严格一问一答;童年记忆低概率自然返流;极低频回家探望。

机制纪律:
- 排程确定性(rng 种子=child+信序号/日期),幂等键兜底;重复 tick 不重复排。
- 信体生成:配了 LLM key(env DEEPSEEK_API_KEY,任何 OpenAI 兼容端点)=LLM 起草
  骨架+他真实历史 utterance 嵌入;**没配 key=纯本地模板信降级**(短、朴素,
  但信一定会来——书信阶段绝不因缺 key 死寂)。LLM 挂=顺延 LETTER_RETRY_H
  绝不空投绝不丢信。
- 生成跑在 scheduler graduated 分支的主 flock 内(毕业后指令面只剩低频信箱,
  库上并发≈0;若未来 graduated 面加高频指令/第二处网络调用,先挪锁外)。
- 寄出的信不进语料(他毕业了,模型冻结),只进下一封来信的素材池。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time

from . import child as child_mod
from . import config as cfg
from . import letter_prompts as lp
from . import texts
from .child import tx
from .events import _meta_get

log = logging.getLogger(__name__)

DAY = 86400.0

# 照护人 → 信素材里的称呼(自定义 persona=原名照用)
_WHO_CN = {"papa": "爸爸", "mama": "妈妈"}


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _meta_set(conn, child_id: str, key: str, value: str, t: float) -> None:
    conn.execute(
        "INSERT INTO parenting_meta(child_id, key, value, updated_at)"
        " VALUES(?,?,?,?) ON CONFLICT(child_id, key) DO UPDATE SET"
        " value=excluded.value, updated_at=excluded.updated_at",
        (child_id, key, value, t))


def _meta_del(conn, child_id: str, key: str) -> None:
    conn.execute("DELETE FROM parenting_meta WHERE child_id=? AND key=?",
                 (child_id, key))


def _is_away(child) -> bool:
    """书信阶段=毕业且判过结局(告别发生过)。v0.3 老档已 graduated 的同样成立
    (旧告别门要求 farewell 落账才判,锚兼容;见 _first_anchor 兜底)。"""
    return child["status"] == "graduated" and bool(child["ending"])


def _daytime_snap(t: float, rng: random.Random) -> float:
    """把时刻钳进白天投递窗(半夜不惊动人):落在窗外→顺延到次日窗内随机时点。"""
    lo, hi = cfg.LETTER_DELIVER_HOURS
    lt = time.localtime(t)
    if lo <= lt.tm_hour < hi:
        return t
    day0 = t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    if lt.tm_hour >= hi:
        day0 += DAY
    return day0 + rng.uniform(lo, min(hi, lo + 4)) * 3600


# ── 毕业画像快照(告别信 spec:成年期满时刻的画像=唯一事实源)──

def graduation_portrait(conn, brain, child_id: str, t: float) -> dict:
    """毕业画像快照。正常落点=judge_ending 判定时刻;这里的缓存查读是兜底。
    brain=None 的 degraded 版**不落缓存**——一次加载失败不冻成永久事实源。"""
    cached = _meta_get(conn, child_id, "graduation_portrait_json")
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            pass
    from .portrait import build_portrait
    p = build_portrait(conn, brain, child_id, now=t)
    if brain is not None:
        with tx(conn):
            _meta_set(conn, child_id, "graduation_portrait_json",
                      json.dumps(p, ensure_ascii=False), t)
    return p


# ── 来信排程 ──

def _first_anchor(conn, child_id: str, child) -> float | None:
    """首封信的时间锚=告别落账时刻。v0.2 时代自动开奖的老档可能没有 farewell
    账——兜底用 child.updated_at(≈毕业时刻;升级后第一封信从近期起算)。"""
    row = conn.execute(
        "SELECT MAX(effective_at) AS t FROM action_log WHERE child_id=?"
        " AND kind='farewell'", (child_id,)).fetchone()
    if row and row["t"] is not None:
        return row["t"]
    if child["ending"]:
        return child["updated_at"]
    return None


def schedule_next_letter(conn, child_id: str, now=None) -> int:
    """无在途来信时排下一封。锚=上一封来信寄达时刻(首封=告别时刻);
    间隔按结局基调;锚后有家里寄来的信→按概率提前(他想回)。全幂等。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if not _is_away(child):
        return 0
    pending = conn.execute(
        "SELECT 1 FROM letters WHERE child_id=? AND direction='in'"
        " AND status='scheduled' LIMIT 1", (child_id,)).fetchone()
    if pending is not None:
        return 0
    seq = conn.execute(
        "SELECT COUNT(*) FROM letters WHERE child_id=? AND direction='in'",
        (child_id,)).fetchone()[0] + 1
    last_in = conn.execute(
        "SELECT MAX(delivered_at) AS t FROM letters WHERE child_id=?"
        " AND direction='in' AND status='delivered'", (child_id,)).fetchone()
    anchor = last_in["t"] if last_in and last_in["t"] is not None \
        else _first_anchor(conn, child_id, child)
    if anchor is None:
        return 0   # 没有告别账也没结局=还没走到书信阶段(防御)
    rng = random.Random(f"{child_id}:letter:{seq}")
    tone = cfg.LETTER_TONE.get(child["ending"]) or cfg.LETTER_TONE["silent"]
    if seq == 1:
        gap = rng.uniform(*cfg.FIRST_LETTER_GAP_DAYS)
    else:
        gap = rng.uniform(*tone["gap"])
        # 锚后家里来过信 → 他想回:按概率把这封提前(非必答,他有自己的生活)
        out_row = conn.execute(
            "SELECT MIN(created_at) AS t FROM letters WHERE child_id=?"
            " AND direction='out' AND created_at>?",
            (child_id, anchor)).fetchone()
        if out_row and out_row["t"] is not None and \
                rng.random() < cfg.LETTER_REPLY_P:
            anchor = out_row["t"]
            gap = rng.uniform(*cfg.LETTER_REPLY_GAP_DAYS)
    due = _daytime_snap(anchor + gap * DAY, rng)
    # 钳未来:tick 停摆几天后 anchor+gap 可能已是过去时刻,不钳=机器一醒就送信,
    # 「写信后无即时回复」被停摆打穿。最快也隔回信下限。
    floor = _daytime_snap(t + cfg.LETTER_REPLY_GAP_DAYS[0] * DAY, rng)
    due = max(due, floor)
    with tx(conn):
        try:
            conn.execute(
                "INSERT INTO letters(child_id, direction, author, status,"
                " due_at, created_at, idempotency_key)"
                " VALUES(?,'in','self','scheduled',?,?,?)",
                (child_id, due, t, f"letter:in:{seq}"))
        except Exception:
            return 0   # UNIQUE 撞=并发已排,幂等
    return 1


# ── 素材抽取+信体生成 ──

def _portrait_lines(p: dict) -> dict:
    """画像 → prompt 摘要行(防御性取值:画像缺块=该行降级,不炸)。"""
    corpus = p.get("corpus") or {}
    by_src = corpus.get("by_speaker") or corpus.get("by_source") or {}
    if isinstance(by_src, dict) and by_src:
        corpus_line = ",".join(f"{k} {v}" for k, v in list(by_src.items())[:5])
    else:
        corpus_line = f"总语料 {corpus.get('total_chars', '?')} 字"
    firsts = p.get("firsts") or []
    firsts_line = ";".join(
        str(f.get("title") or f.get("item_kind") or "") for f in firsts[:4]
        if isinstance(f, dict)) or "(没记下来)"
    bond = p.get("bond") or {}
    conf = bond.get("confidence")
    vals = bond.get("values") or {}
    parts = []
    for cg, cn in (("papa", "爸爸"), ("mama", "妈妈")):
        dims = vals.get(cg) or {}
        if dims:
            top = max(dims, key=lambda d: dims[d] if d != "resentment" else -1)
            parts.append(f"对{cn}最重的是{cfg.BOND_CN.get(top, top)}")
    bond_line = ",".join(parts) or "(账不全)"
    if conf == "low":
        bond_line += "(早年的账是估的,写的时候留余地,用「大概/好像记得」的口气)"
    return {"corpus_line": corpus_line, "firsts_line": firsts_line,
            "bond_line": bond_line}


def _pick_materials(conn, child_id: str, rng: random.Random) -> dict:
    """素材抽取(LLM 版与本地降级版共用):童年痕迹/他小时候的话/未回信/上一封。"""
    m: dict = {"memory": None, "voices": [], "inbox": [], "prev": None,
               "sources": {}}
    if rng.random() < cfg.LETTER_MEMORY_P:
        n = conn.execute(
            "SELECT COUNT(*) FROM corpus_item WHERE child_id=?"
            " AND source_kind='direct' AND LENGTH(text)>=8",
            (child_id,)).fetchone()[0]
        if n:
            row = conn.execute(
                "SELECT id, text FROM corpus_item WHERE child_id=?"
                " AND source_kind='direct' AND LENGTH(text)>=8"
                " ORDER BY id LIMIT 1 OFFSET ?",
                (child_id, rng.randrange(n))).fetchone()
            m["memory"] = row["text"][:120]
            m["sources"]["memory_corpus_id"] = row["id"]
    vn = rng.randrange(cfg.LETTER_VOICE_MAX + 1)
    if vn:
        rows = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND TRIM(text)!='' AND LENGTH(text)>=4 ORDER BY id",
            (child_id,)).fetchall()
        if rows:
            picked = rng.sample(rows, min(vn, len(rows)))
            m["voices"] = [r["text"] for r in picked]
            m["sources"]["voice_utterance_ids"] = [r["id"] for r in picked]
    last_in = conn.execute(
        "SELECT MAX(delivered_at) AS t FROM letters WHERE child_id=?"
        " AND direction='in' AND status='delivered'", (child_id,)).fetchone()
    since = last_in["t"] if last_in and last_in["t"] is not None else 0.0
    inbox = conn.execute(
        "SELECT id, author, body FROM letters WHERE child_id=?"
        " AND direction='out' AND created_at>? ORDER BY id DESC LIMIT 3",
        (child_id, since)).fetchall()
    if inbox:
        m["inbox"] = [(r["author"], r["body"][:120]) for r in reversed(inbox)]
        m["sources"]["reply_to_ids"] = [r["id"] for r in inbox]
    prev = conn.execute(
        "SELECT body FROM letters WHERE child_id=? AND direction='in'"
        " AND status='delivered' ORDER BY id DESC LIMIT 1",
        (child_id,)).fetchone()
    if prev:
        m["prev"] = prev["body"][:80]
    return m


def _compose_letter_local(child, m: dict, rng: random.Random) -> str:
    """无 LLM key 的降级信:纯本地模板+真实素材拼装。短、朴素,但一定会来。
    只嵌 voices[0] 一句——留痕同步收窄(sources 不许虚记没嵌进去的素材)。"""
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    tpl = texts.LETTER_LOCAL_TEMPLATES[
        rng.randrange(len(texts.LETTER_LOCAL_TEMPLATES))]
    memory_line = texts.LETTER_LOCAL_MEMORY.format(text=m["memory"]) \
        if m["memory"] else ""
    voice_line = texts.LETTER_LOCAL_VOICE.format(text=m["voices"][0]) \
        if m["voices"] else ""
    if len(m.get("voices") or []) > 1:
        m["sources"]["voice_utterance_ids"] = \
            m["sources"]["voice_utterance_ids"][:1]
    return tpl.format(memory_line=memory_line, voice_line=voice_line) + \
        "\n" + name


def _compose_letter(conn, brain, child_id: str, letter, ds_complete, t: float):
    """一封来信的正文。返回 (body, sources)。LLM 失败=抛(上层顺延);
    没配 key=本地模板信(不抛,信照来)。"""
    child = child_mod.get_child(conn, child_id)
    ending = child["ending"] or "silent"
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    rng = random.Random(f"{child_id}:compose:{letter['id']}")
    m = _pick_materials(conn, child_id, rng)
    sources = dict(m["sources"])

    if ds_complete is None and not os.getenv("DEEPSEEK_API_KEY", ""):
        body = _compose_letter_local(child, m, rng)
        sources["composer"] = "local_template"
        return body, sources

    p = graduation_portrait(conn, brain, child_id, t)
    lines = _portrait_lines(p)
    memory_block = lp.MEMORY_BLOCK.format(text=m["memory"]) if m["memory"] else ""
    voices_block = lp.VOICES_BLOCK.format(
        n=len(m["voices"]),
        lines=" / ".join(f"「{v}」" for v in m["voices"])) if m["voices"] else ""
    inbox_block = ""
    if m["inbox"]:
        lines_txt = "\n".join(
            f"  {_WHO_CN.get(a, a)}写:「{b}」" for a, b in m["inbox"])
        inbox_block = lp.INBOX_BLOCK.format(lines=lines_txt)
    last_block = lp.LAST_LETTER_BLOCK.format(digest=m["prev"]) if m["prev"] else ""

    first_extra = ""
    delivered_n = conn.execute(
        "SELECT COUNT(*) FROM letters WHERE child_id=? AND direction='in'"
        " AND status='delivered'", (child_id,)).fetchone()[0]
    if delivered_n == 0:
        fw = conn.execute(
            "SELECT actor FROM action_log WHERE child_id=? AND kind='farewell'"
            " ORDER BY effective_at DESC LIMIT 1", (child_id,)).fetchone()
        from .bond import _caregiver_of
        actor = fw["actor"] if fw else ""
        # actor 为空(v0.2 自动开奖老档,无 farewell 账)必须落兜底——
        # _caregiver_of 会把空串归 papa,等于给老档编一句「是爸爸说的去吧」
        # (画像里没有的往事不许编,文案层第一条纪律)
        slot = ("self" if actor == "self"
                else (_caregiver_of(actor, "farewell") if actor else None))
        line = lp.FAREWELL_LINE_BY_ACTOR.get(slot or "",
                                             lp.FAREWELL_LINE_FALLBACK)
        first_extra = lp.FIRST_LETTER_EXTRA.format(farewell_line=line)

    lo, hi = lp.LETTER_CHARS.get(ending, (100, 250))
    prompt = lp.LETTER_PROMPT.format(
        name=name, appearance=(child["appearance"] or "(没人记下来过)"),
        corpus_line=lines["corpus_line"], firsts_line=lines["firsts_line"],
        bond_line=lines["bond_line"], ending_tone=lp.ENDING_TONE[ending],
        first_letter_extra=first_extra, memory_block=memory_block,
        voices_block=voices_block, inbox_block=inbox_block,
        last_letter_block=last_block, min_chars=lo, max_chars=hi)
    if ds_complete is None:
        from .psyche import _ds_complete as ds_complete
    res = ds_complete(prompt, max_tokens=cfg.LETTER_DS_MAX_TOKENS,
                      temperature=cfg.LETTER_DS_TEMPERATURE)
    body = (res.get("content") or "").strip()
    if not body:
        raise RuntimeError("empty_letter_body")
    sources["model"] = res.get("model")
    return body, sources


def deliver_due_letters(conn, brain, child_id: str, now=None,
                        ds_complete=None) -> int:
    """到期来信:生成正文→delivered→投 outbox(标题=texts.LETTER_ARRIVE_TITLE,
    正文在信箱)。生成失败=顺延 LETTER_RETRY_H,绝不空投绝不丢信。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if not _is_away(child):
        return 0
    rows = conn.execute(
        "SELECT * FROM letters WHERE child_id=? AND direction='in'"
        " AND status='scheduled' AND due_at<=? ORDER BY id",
        (child_id, t)).fetchall()
    delivered = 0
    for letter in rows:
        lo, hi = cfg.LETTER_DELIVER_HOURS
        if not (lo <= time.localtime(t).tm_hour < hi):
            continue   # 只在白天送信(due 保持,下一拍白天再送)
        try:
            body, sources = _compose_letter(conn, brain, child_id, letter,
                                            ds_complete, t)
        except Exception as e:
            # 可观测+降频:失败每次 stderr 记一行;连败 12 次(≈2 天)后重试
            # 降到每日一次——空稿也计一次 LLM 调用,不许无人看管地烧
            attempts = (letter["attempt_count"] or 0) + 1
            retry_h = cfg.LETTER_RETRY_H if attempts < 12 else 24.0
            log.warning("[letters] 信 #%s 生成失败第 %s 次(%s: %s),顺延 %sh",
                        letter["id"], attempts, type(e).__name__, e, retry_h)
            with tx(conn):
                conn.execute(
                    "UPDATE letters SET attempt_count=attempt_count+1, due_at=?"
                    " WHERE id=? AND status='scheduled'",
                    (t + retry_h * 3600, letter["id"]))
            continue
        name = child["name"] or texts.DEFAULT_CHILD_NAME
        with tx(conn):
            conn.execute(
                "UPDATE letters SET body=?, sources_json=?, status='delivered',"
                " delivered_at=? WHERE id=? AND status='scheduled'",
                (body, json.dumps(sources, ensure_ascii=False), t, letter["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind,"
                " payload_json, status, next_attempt_at, idempotency_key)"
                " VALUES(?,?,?,?,'pending',?,?)",
                (child_id, "webhook", "nursery.letter",
                 json.dumps({"kind": "nursery.letter",
                             "title": texts.LETTER_ARRIVE_TITLE,
                             "note": None, "letter_id": letter["id"],
                             "author": name, "body": body, "ts": t,
                             "source_event_id": f"letterin:{letter['id']}"},
                            ensure_ascii=False), t, f"letterin:{letter['id']}"))
        delivered += 1
    return delivered


# ── 照护者写信 ──

def write_letter(conn, child_id: str, author: str, body: str,
                 now=None) -> dict:
    """寄一封信给他。不进语料(模型冻结);他若干天后才回,且不保证一问一答。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if not _is_away(child):
        return {"ok": False, "error": "not_away"}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}
    if len(body) > cfg.MAX_LETTER_LEN:
        return {"ok": False, "error": "too_long"}
    import sqlite3 as _sq
    with tx(conn):
        for bump in range(3):   # 同毫秒双击=键+1 重试,不把 IntegrityError 冒给用户
            try:
                conn.execute(
                    "INSERT INTO letters(child_id, direction, author, body,"
                    " status, created_at, idempotency_key)"
                    " VALUES(?,'out',?,?,'sent',?,?)",
                    (child_id, author, body, t,
                     f"letter:out:{int(t * 1000) + bump}:{author}"))
                break
            except _sq.IntegrityError:
                continue
        else:
            return {"ok": False, "error": "busy"}
        # 他想回:已排的下一封按概率提前(不即时——最快也隔 REPLY_GAP 下限)
        rng = random.Random(f"{child_id}:reply:{int(t)}")
        pending = conn.execute(
            "SELECT id, due_at FROM letters WHERE child_id=? AND direction='in'"
            " AND status='scheduled' LIMIT 1", (child_id,)).fetchone()
        if pending is not None and rng.random() < cfg.LETTER_REPLY_P:
            early = _daytime_snap(
                t + rng.uniform(*cfg.LETTER_REPLY_GAP_DAYS) * DAY, rng)
            if early < (pending["due_at"] or float("inf")):
                conn.execute("UPDATE letters SET due_at=? WHERE id=?",
                             (early, pending["id"]))
    return {"ok": True, "sent": len(body)}


# ── 极低频回家探望 ──

def in_visit(conn, child_id: str, now=None) -> bool:
    """他这会儿回来了吗(探望期间可短暂少量互动——talk/say 放行;
    不恢复完整幼年玩法)。窗=visit_open_at 起 VISIT_STAY_HOURS 内。"""
    t = _now(now)
    open_at = _meta_get(conn, child_id, "visit_open_at")
    if open_at is None:
        return False
    try:
        return t < float(open_at) + cfg.VISIT_STAY_HOURS * 3600
    except ValueError:
        return False


def tick_visit(conn, child_id: str, now=None) -> dict:
    """visit=惊喜事件,玩家不可主动触发。日抽签(确定性)+冷却;两段:
    「他今天回来了。」(+宝贝盒物件钩子)→次日「他走了。」(带走了旧玩具)。"""
    t = _now(now)
    out: dict = {}
    child = child_mod.get_child(conn, child_id)
    if not _is_away(child):
        return out
    name = child["name"] or texts.DEFAULT_CHILD_NAME

    # 尾声(先于开场判:开着的探望先送走)
    open_at = _meta_get(conn, child_id, "visit_open_at")
    if open_at is not None:
        if t >= float(open_at) + cfg.VISIT_STAY_HOURS * 3600:
            took = _meta_get(conn, child_id, "visit_took") == "1"
            date = _local_date(float(open_at))
            with tx(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO outbox(child_id, target, kind,"
                    " payload_json, status, next_attempt_at, idempotency_key)"
                    " VALUES(?,?,?,?,'pending',?,?)",
                    (child_id, "webhook", "nursery.event",
                     json.dumps({"kind": "nursery.event",
                                 "title": texts.VISIT_END_TITLE,
                                 "note": texts.VISIT_END_NOTE if took else None,
                                 "visit": "end", "ts": t,
                                 "source_event_id": f"visitend:{child_id}:{date}"},
                                ensure_ascii=False), t, f"visitend:{child_id}:{date}"))
                _meta_del(conn, child_id, "visit_open_at")
                _meta_del(conn, child_id, "visit_took")
            out["visit_end"] = True
        return out

    tone = cfg.LETTER_TONE.get(child["ending"]) or cfg.LETTER_TONE["silent"]
    date = _local_date(t)
    rng = random.Random(f"{child_id}:visit:{date}")
    if rng.random() > tone["visit_day_p"]:
        return out
    if time.localtime(t).tm_hour < 10:
        return out   # 白天才进门
    last = _meta_get(conn, child_id, "visit_last_at")
    if last is not None and t - float(last) < cfg.VISIT_COOLDOWN_DAYS * DAY:
        return out
    note = None
    took = False
    try:
        from .visible_growth import treasure_list
        tr = treasure_list(conn, child_id, top_n=1)
        if tr:
            note = texts.VISIT_TREASURE_NOTE
            took = True
    except Exception:
        pass
    with tx(conn):
        dup = conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=?",
                           (f"visit:{child_id}:{date}",)).fetchone()
        if dup is not None:
            return out
        conn.execute(
            "INSERT INTO outbox(child_id, target, kind, payload_json, status,"
            " next_attempt_at, idempotency_key) VALUES(?,?,?,?,'pending',?,?)",
            (child_id, "webhook", "nursery.event",
             json.dumps({"kind": "nursery.event",
                         "title": texts.VISIT_TITLE.format(name=name),
                         "note": note, "visit": "start", "ts": t,
                         "source_event_id": f"visit:{child_id}:{date}"},
                        ensure_ascii=False), t, f"visit:{child_id}:{date}"))
        _meta_set(conn, child_id, "visit_open_at", str(t), t)
        _meta_set(conn, child_id, "visit_last_at", str(t), t)
        _meta_set(conn, child_id, "visit_took", "1" if took else "0", t)
    out["visit_start"] = True
    return out


# ── 信箱读口 ──

def mailbox_summary(conn, child_id: str, now=None, limit: int = 5,
                    offset: int = 0) -> dict:
    t = _now(now)
    last_in = conn.execute(
        "SELECT MAX(delivered_at) AS t FROM letters WHERE child_id=?"
        " AND direction='in' AND status='delivered'", (child_id,)).fetchone()
    days = None
    if last_in and last_in["t"] is not None:
        days = int((t - last_in["t"]) // DAY)
    unread = conn.execute(
        "SELECT COUNT(*) FROM letters WHERE child_id=? AND direction='in'"
        " AND status='delivered' AND read_at IS NULL", (child_id,)).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM letters WHERE child_id=?"
        " AND status IN ('delivered','sent')", (child_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT id, direction, author, body, status, created_at, delivered_at,"
        " read_at FROM letters WHERE child_id=? AND status IN"
        " ('delivered','sent')"
        " ORDER BY COALESCE(delivered_at, created_at) DESC LIMIT ? OFFSET ?",
        (child_id, limit, offset)).fetchall()
    return {
        "last_in_days": days, "unread": unread, "total": total,
        "letters": [
            {"id": r["id"], "direction": r["direction"], "author": r["author"],
             "body": r["body"], "at": r["delivered_at"] or r["created_at"],
             "read": r["read_at"] is not None or r["direction"] == "out"}
            for r in rows],
    }


def read_one_letter(conn, child_id: str, letter_id: int,
                    now=None) -> dict | None:
    """读某封:in 信首次读到=标 read_at(未读列表口径)。"""
    t = _now(now)
    r = conn.execute(
        "SELECT id, direction, author, body, created_at, delivered_at, read_at"
        " FROM letters WHERE child_id=? AND id=? AND status IN"
        " ('delivered','sent')", (child_id, int(letter_id))).fetchone()
    if r is None:
        return None
    if r["direction"] == "in" and r["read_at"] is None:
        with tx(conn):
            conn.execute("UPDATE letters SET read_at=? WHERE id=?"
                         " AND read_at IS NULL", (t, r["id"]))
    return {"id": r["id"], "direction": r["direction"], "author": r["author"],
            "body": r["body"], "at": r["delivered_at"] or r["created_at"]}


# ── tick 入口(scheduler graduated 分支调)──

def tick_letters_fast(conn, child_id: str, now=None) -> dict:
    """排下一封+探望两段(快路,零网络)。生成在 deliver_due_letters。"""
    out: dict = {}
    try:
        n = schedule_next_letter(conn, child_id, now=now)
        if n:
            out["scheduled"] = n
    except Exception:
        pass
    try:
        out.update(tick_visit(conn, child_id, now=now))
    except Exception:
        pass
    return out
