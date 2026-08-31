#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""育儿模拟器·每次一个子进程的 driver(短命进程,flock 防并发丢档)。

孩子是 **solo 存档**:每 caregiver 一份 SQLite(saves/papa/nursery.db 等),物理隔离。
⚠身份=调用方自报(白名单只挡未知 persona);接入层如需真实鉴权自行前置。
所有面向玩家的文案在 texts.py(文案层),本文件只做流程与格式拼装。

argv 形态:
- <persona> <cmd> [args...]     主照护人的动作指令(文本输出,给人看)
- <persona> mama <hug|soothe|touch|say> [text...]
                                妈妈通道(第二照护人):输出单行 JSON,给接入层消费
- --tick                        定时巡检:排夜奶/触发到期事件/投递 outbox。
                                只吐 JSON 到 stdout(引擎不碰网络,除非
                                NURSERY_EVENT_URL 配了才由 scheduler 投)
- --init-birth <persona> [名字] [--embryo]
                                建档。名字可现在给(提前想好的),也可留空——
                                出生后用 name 指令定,或让孩子一起挑;
                                --embryo=占位胚胎,日后不带 --embryo 再跑一次=孵化
- --pause / --resume <persona>   冻龄/解冻(管理面,不进阶段解锁表)。
                                冻龄=逻辑年龄停走,状态/事件/说话照常
- --set-policy <persona> <ver>   阶段表升版(显式迁移;config 头注红线:
                                不许悄悄重写既有孩子年龄。只升不降)

路径 env 覆盖(测试红线:真实 saves/ 永不当测试默认值):NURSERY_SAVES_DIR。
偷学源 env:NURSERY_ARCHIVE_DB(语料存档路径,只读硬闸与 schema 约定在 sampler)。
"""
from __future__ import annotations

import fcntl
import json
import os
import sys

from . import child as child_mod
from . import db as pdb
from . import texts
from . import config as cfg
from .config import STAGE_CN
from .decoder import SpeakResult

_HERE = os.path.dirname(os.path.abspath(__file__))
# 照护人登记:persona → 存档子目录。env NURSERY_PLAYERS 逗号分隔覆盖(默认单照护人)。
def current_players() -> dict:
    """现读 env 的照护人表。--tick 等「先 import 后 load .env」的路径必须用它。"""
    return {p.strip(): p.strip()
            for p in os.getenv("NURSERY_PLAYERS", "papa").split(",") if p.strip()}


PLAYER_DIR = current_players()   # 进程启动时的快照(CLI/工具面用;env 先于进程设定)
DEFAULT_SAVES_DIR = os.path.join(_HERE, "saves")

MAX_FEED_LEN = 600   # 一次喂语料正文上限(字)
MAX_NAME_LEN = 6     # 单个名字候选上限(字)

# ── 妈妈通道(第二照护人的互动;主照护人指令白名单里没有 mama)──
MAMA_ACTOR = "mama"        # action_log.actor / corpus_item.speaker 同口径
# farewell/stay=v0.4 告别窗(窗内谁说都算数);write/letters=书信阶段信箱口
MAMA_SUBCMDS = frozenset({"hug", "soothe", "touch", "say", "choose",
                          "farewell", "stay", "write", "letters"})
MAX_MAMA_SAY_LEN = 500     # 说给他听正文上限(接入层同值)

# 动作解锁表:阶段 gate(成长的仪式感;越阶调用给打趣文案不报错)。
# status/help/name 不进表——全阶段可用(name 在定名前有效)。
STAGE_ACTIONS = {
    "infant":  {"status", "feed", "soothe", "diaper", "burp", "describe",
                "album", "log", "help"},
    "toddler": {"status", "feed", "soothe", "diaper", "burp", "play", "teach",
                "describe", "album", "log", "help", "choose"},
    "child":   {"status", "feed", "soothe", "play", "teach", "talk", "discipline",
                "describe", "album", "log", "help", "choose"},
    "teen":    {"status", "feed", "talk", "discipline", "describe",
                "album", "log", "help", "choose"},
    "adult":   {"status", "talk", "describe", "album", "log", "help", "choose",
                "farewell", "stay"},   # 结局日:说再见/再等一天
}
MAX_DESCRIBE_LEN = 300

_BAR = 10


def _bar(v: float) -> str:
    fill = int(round(v / 100 * _BAR))
    return "█" * fill + "░" * (_BAR - fill)


def resolve_saves_dir() -> str:
    return os.getenv("NURSERY_SAVES_DIR") or DEFAULT_SAVES_DIR


def _db_path(sub: str) -> str:
    d = os.path.join(resolve_saves_dir(), sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "nursery.db")


def _active_child(conn):
    row = conn.execute(
        "SELECT * FROM child WHERE status IN ('active','runaway','graduated')"
        " ORDER BY created_at LIMIT 1").fetchone()
    return row


def _render_state(conn, child_id: str, now: float | None) -> str:
    s = child_mod.read_state(conn, child_id, now=now)
    return texts.STATE_PANEL.format(
        mood=_bar(s["mood"]), health=_bar(s["health"]),
        intimacy=_bar(s["intimacy"]), nutrition=_bar(s["nutrition"]))


def _speak_line(res: SpeakResult, name: str) -> str:
    return f"{name}:「{res.text}」"


def _idem(prefix: str, t: float) -> str:
    return f"{prefix}:{int(t * 1000)}"


def _opening(name: str | None) -> str:
    """出生开场文案:名字已定/未定各一条 name_line。"""
    line = (texts.OPENING_NAMED_LINE.format(name=name) if name
            else texts.OPENING_UNNAMED_LINE)
    return texts.OPENING.format(name_line=line)


def _mama_dispatch(conn, child, argv: list, t: float) -> str:
    """妈妈通道:第二照护人的互动 → 单行 JSON 给接入层消费。

    与主照护人动作同纪律:apply_action 幂等落账(actor=mama)/说的话走 feed_corpus
    既有喂语料管线(source_kind='direct', speaker='mama', action_kind='mama_say')
    =真训练;每次互动孩子都真实回一句(child_speak,进 utterance/status 最近在说)。
    这里不是给人看的文本面——错误也用 JSON 键,渲染归接入层。
    """
    def out(ok: bool, **kw) -> str:
        return json.dumps({"ok": ok, **kw}, ensure_ascii=False)

    sub = argv[0].lower() if argv else ""
    if sub not in MAMA_SUBCMDS:
        return out(False, error="unknown_subcmd")
    if child is None:
        return out(False, error="no_child")
    if child["status"] != "active":
        # v0.4:他回来探望那天,妈妈也能说上话(say 放行;其余照关)
        if child["status"] == "graduated" and sub == "say":
            from .letters import in_visit
            if in_visit(conn, child["child_id"], now=t):
                cid = child["child_id"]
                text = " ".join(argv[1:]).strip()
                if not text:
                    return out(False, error="empty_text")
                if len(text) > MAX_MAMA_SAY_LEN:
                    return out(False, error="too_long")
                brain_v = child_mod.ChildBrain.load(conn, cid)
                child_mod.apply_action(conn, cid, MAMA_ACTOR, "mama_say",
                                       idempotency_key=_idem("mama_say", t),
                                       now=t)   # 成年了,不再进语料——话是说给他听的
                res = child_mod.child_speak(conn, brain_v, cid,
                                            trigger="mama_say", now=t)
                return out(True, action="say", visiting=True,
                           said=None if res.refused else res.text)
            return out(False, error="graduated")
        # v0.4:书信阶段信箱口照开(graduated 只放 write/letters)
        if child["status"] == "graduated" and sub in ("write", "letters"):
            from .letters import mailbox_summary, write_letter
            cid = child["child_id"]
            if sub == "letters":
                return out(True, action="letters",
                           **mailbox_summary(conn, cid, now=t, limit=10))
            text = " ".join(argv[1:]).strip()
            r = write_letter(conn, cid, MAMA_ACTOR, text, now=t)
            if not r["ok"]:
                return out(False, error=r["error"])
            return out(True, action="write", sent=r["sent"])
        return out(False, error=child["status"])   # runaway / 未毕业面照旧
    cid = child["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)

    if sub in ("write", "letters"):
        # 信箱是离家后的事——他还在家(active)时用 say,不写信
        return out(False, error="not_away")

    if sub in ("farewell", "stay"):
        # v0.4:告别窗内她也能说(谁先说算谁的;窗没开=not_yet)
        from .events import farewell_window, judge_ending
        if farewell_window(conn, cid) is None:
            return out(False, error="not_yet")
        child_mod.apply_action(conn, cid, MAMA_ACTOR, sub,
                               idempotency_key=_idem(sub, t), now=t)
        if sub == "stay":
            return out(True, action="stay", line=texts.FAREWELL_STAY_REPLY)
        ending = judge_ending(conn, brain, cid, now=t)
        return out(True, action="farewell", ending=ending,
                   line=texts.FAREWELL_GO_REPLY,
                   ending_cn=texts.ENDING_CN.get(ending) if ending else None)

    if sub == "say":
        text = " ".join(argv[1:]).strip()
        if not text:
            return out(False, error="empty_text")
        if len(text) > MAX_MAMA_SAY_LEN:
            return out(False, error="too_long")
        r = child_mod.feed_corpus(conn, brain, cid, text, source_kind="direct",
                                  speaker=MAMA_ACTOR, actor=MAMA_ACTOR,
                                  action_kind="mama_say", now=t)
        if r["duplicate"]:
            return out(True, action="say", duplicate=True, said=None)
        res = child_mod.child_speak(conn, brain, cid, trigger="mama_say", now=t)
        return out(True, action="say", duplicate=False, fed=r["fed"],
                   said=None if res.refused else res.text)

    if sub == "choose":
        # 两难妈妈也能拍,谁先拍算谁的。
        from .choices import resolve_choice
        if len(argv) < 3 or not argv[1].isdigit() or \
                argv[2].lower() not in ("a", "b"):
            return out(False, error="bad_args")
        r = resolve_choice(conn, brain, cid, int(argv[1]), argv[2].lower(),
                           now=t, actor=MAMA_ACTOR)
        if r["status"] != "ok":
            return out(False, error=r["status"])
        return out(True, action="choose", line=r.get("line"), said=r.get("said"))

    kind = f"mama_{sub}"
    child_mod.apply_action(conn, cid, MAMA_ACTOR, kind,
                           idempotency_key=_idem(kind, t), now=t)
    res = child_mod.child_speak(conn, brain, cid, trigger=kind, now=t)
    return out(True, action=sub, said=None if res.refused else res.text)


def _name_dispatch(conn, child, rest: str, t: float) -> str:
    """name 指令:一生一次的定名。单候选=你说了算;多候选=他自己挑。"""
    if child["name"]:
        return texts.NAME_ALREADY.format(name=child["name"])
    cid = child["child_id"]
    brain = child_mod.ChildBrain.load(conn, cid)
    cands = rest.split()
    if not cands:
        sounds = child_mod.name_babble(brain, cid)
        babble = (texts.NAME_BABBLE_LINE.format(sounds=sounds) if sounds
                  else texts.NAME_NO_BABBLE_LINE)
        return texts.NAME_RULES.format(babble_line=babble)
    if any(len(c) > MAX_NAME_LEN for c in cands):
        return texts.NAME_TOO_LONG.format(max_len=MAX_NAME_LEN)
    picked = child_mod.pick_name(conn, brain, cid, cands, now=t)
    if picked.get("already"):
        return texts.NAME_ALREADY.format(name=picked["name"])
    # 定名已提交;回一句只是附加反馈——fail-open,失败绝不掩盖定名成功
    tail = ""
    try:
        res = child_mod.child_speak(conn, brain, cid, trigger="named", now=t)
        if res.accepted and not res.refused:
            tail = "\n" + _speak_line(res, picked["name"])
    except Exception:
        pass
    if len(cands) == 1:
        return texts.NAME_PICKED_SOLO.format(name=picked["name"]) + tail
    return texts.NAME_PICKED_TOGETHER.format(
        candidates="、".join(cands), name=picked["name"]) + tail


def dispatch(conn, persona: str, argv: list, now: float | None = None) -> str:
    """persona 的一条指令 → 给他看的文本。conn 已开(调用方管 flock)。"""
    t = child_mod._now(now)
    cmd = (argv[0] if argv else "help").lower()
    rest = " ".join(argv[1:]).strip()

    child = _active_child(conn)
    if cmd == "portrait":
        # 成长画像 JSON 面(围观/接入层消费):纯读零状态;照护指令白名单无此指令
        if child is None:
            return json.dumps({"ok": False, "error": "no_child"}, ensure_ascii=False)
        from .portrait import build_portrait
        try:
            pbrain = child_mod.ChildBrain.load(conn, child["child_id"])
        except Exception:
            pbrain = None   # 画像照出,vocab=None+degraded 标记(不把异常当事实缺失)
        out = {"ok": True, "portrait": build_portrait(conn, pbrain,
                                                      child["child_id"], now=t)}
        if pbrain is None:
            out["degraded"] = "brain_load_failed"
        return json.dumps(out, ensure_ascii=False)
    if cmd == "mama":
        # 妈妈通道走 JSON 面(接入层消费);主照护人指令白名单里没有它
        return _mama_dispatch(conn, child, argv[1:], t)
    if child is None:
        return texts.EMPTY_CRADLE
    cid, name = child["child_id"], child["name"] or texts.DEFAULT_CHILD_NAME
    stage = child_mod.stage_of(child, t)
    allowed = STAGE_ACTIONS.get(stage, STAGE_ACTIONS["infant"])

    # 离家出走:推理端离线——只有 status/feed(隔空喊话)有回应
    if child["status"] == "runaway":
        if cmd == "feed" and rest:
            brain = child_mod.ChildBrain.load(conn, cid)
            if child_mod.attempt_homecoming(conn, brain, cid, rest, now=t,
                                            actor=PLAYER_DIR[persona]):
                return texts.RUNAWAY_CALL_OK.format(name=name)
            hours = (t - (child["runaway_at"] or t)) / 3600.0
            return texts.RUNAWAY_CALL_NO_ECHO.format(hours=hours)
        if cmd == "status":
            hours = (t - (child["runaway_at"] or t)) / 3600.0
            return texts.RUNAWAY_STATUS.format(name=name, hours=hours)
        return texts.RUNAWAY_UNREACHABLE

    # v0.4:已毕业=书信阶段。相册/记录永远可看;数值面板整个不渲——
    # 他过得怎么样,只能从他的信里读(设计红线:成年不做第二套养成)。
    if child["status"] == "graduated":
        from .letters import in_visit, mailbox_summary, read_one_letter, \
            write_letter
        if cmd in ("album", "log"):
            pass  # 相册/记录永远可看
        elif cmd == "help":
            return texts.AWAY_HELP.format(name=name)
        elif cmd == "talk":
            # 他回来探望的那一天,能说上话(少量互动,不恢复幼年玩法)
            if in_visit(conn, cid, now=t):
                brain_v = child_mod.ChildBrain.load(conn, cid)
                child_mod.apply_action(conn, cid, PLAYER_DIR[persona], "talk",
                                       idempotency_key=_idem("talk", t), now=t)
                res = child_mod.child_speak(conn, brain_v, cid, trigger="talk",
                                            now=t)
                if res.refused:
                    return texts.AWAY_VISIT_REFUSED.format(name=name)
                return f"{texts.AWAY_VISIT_TALK}\n{_speak_line(res, name)}"
            return texts.AWAY_TALK_HINT
        elif cmd in ("status", "letters"):
            from datetime import datetime as _dt
            who = {"self": name, "papa": "爸爸", "mama": "妈妈"}
            parts = rest.split()
            # letters <编号> = 读某封(in 信首次读=销未读)
            if cmd == "letters" and parts and parts[0].isdigit():
                r = read_one_letter(conn, cid, int(parts[0]), now=t)
                if r is None:
                    return "没有这个编号的信。letters 看列表。"
                d = _dt.fromtimestamp(r["at"])
                tag = f"{name}的信" if r["direction"] == "in" else \
                    f"{who.get(r['author'], r['author'])}寄出"
                return f"#{r['id']} · {d.month}-{d.day:02d} · {tag}\n{r['body']}"
            page = 1
            if cmd == "letters" and len(parts) == 2 and parts[0] == "page" \
                    and parts[1].isdigit():
                page = max(1, int(parts[1]))
            per = 6
            box = mailbox_summary(conn, cid, now=t, limit=per,
                                  offset=(page - 1) * per)
            if box["last_in_days"] is None:
                line = texts.AWAY_NO_LETTER_YET
            elif box["last_in_days"] == 0:
                line = texts.AWAY_LAST_LETTER_TODAY
            else:
                line = texts.AWAY_LAST_LETTER.format(days=box["last_in_days"])
            if box["unread"]:
                line += f" 没拆的信:{box['unread']} 封。"
            lines = [texts.AWAY_STATUS_HEAD.format(name=name), line]
            if cmd == "letters":
                for r in box["letters"]:
                    d = _dt.fromtimestamp(r["at"])
                    tag = f"{name}的信" if r["direction"] == "in" else \
                        f"{who.get(r['author'], r['author'])}寄出"
                    mark = "●" if not r["read"] else "·"
                    head = r["body"][:24] + ("…" if len(r["body"]) > 24 else "")
                    lines.append(f"{mark} #{r['id']} {d.month}-{d.day:02d}"
                                 f" {tag}:{head}")
                if not box["letters"]:
                    lines.append("(这一页没有信。)" if page > 1
                                 else "(信箱还空着。)")
                if box["total"] > page * per:
                    lines.append(f"(还有更早的——letters page {page + 1})")
            return "\n".join(lines)
        elif cmd == "write":
            if not rest:
                return "write 后面接信的内容。他不会马上回——他有自己的日子了。"
            r = write_letter(conn, cid, PLAYER_DIR[persona], rest, now=t)
            if not r["ok"]:
                return {"empty": "信是空的。",
                        "too_long": f"太长了(>{cfg.MAX_LETTER_LEN} 字)。"
                                    "信不用一次写完,想说的下封再说。"
                        }.get(r["error"], r["error"])
            return texts.LETTER_SENT_REPLY
        else:
            return texts.AWAY_QUIET

    if cmd == "help":
        cmds = sorted(allowed - {"help"})
        if not child["name"]:
            cmds.append("name")
        out = texts.HELP_TEXT.format(name=name, stage_cn=STAGE_CN[stage],
                                     cmds=" / ".join(cmds))
        if not child["name"]:
            out += "\n" + texts.NAME_PROMPT_LINE
        return out

    if cmd == "name":
        return _name_dispatch(conn, child, rest, t)

    if cmd in ("write", "letters"):
        # 指令存在,只是还没到时候(信箱是离家后的事;口径与 mama not_away 对齐)
        return texts.NOT_AWAY_HINT.format(name=name)

    if cmd not in STAGE_ACTIONS["toddler"] | STAGE_ACTIONS["child"] | \
            STAGE_ACTIONS["teen"] | STAGE_ACTIONS["infant"] | STAGE_ACTIONS["adult"]:
        return texts.UNKNOWN_CMD.format(cmd=cmd)
    if cmd not in allowed:
        return texts.LOCKED_HINTS.get(
            cmd, texts.LOCKED_FALLBACK.format(stage_cn=STAGE_CN[stage], cmd=cmd))

    brain = child_mod.ChildBrain.load(conn, cid)

    if cmd == "status":
        recent = conn.execute(
            "SELECT text, created_at FROM utterance WHERE child_id=? AND accepted=1"
            " ORDER BY id DESC LIMIT 3", (cid,)).fetchall()
        # 妈妈的原话也展示:读 corpus_item 的 direct+speaker=mama 面——正是妈妈
        # 通道 say 落语料的那份(PII 已遮盖,夜奶 night_feed 不混)。近两句、
        # 原文不剪,与「最近在说」同格式;没有就整行不出。
        mama = conn.execute(
            "SELECT text FROM corpus_item WHERE child_id=? AND source_kind='direct'"
            " AND speaker=? ORDER BY id DESC LIMIT 2", (cid, MAMA_ACTOR)).fetchall()
        lines = [texts.STATUS_HEADER.format(name=name, stage_cn=STAGE_CN[stage],
                                            chars=brain.model.total_chars),
                 _render_state(conn, cid, t)]
        if child["appearance"]:
            lines.append(texts.STATUS_APPEARANCE.format(text=child["appearance"]))
        else:
            lines.append(texts.STATUS_NO_APPEARANCE)
        if not child["name"]:
            lines.append(texts.NAME_PROMPT_LINE)
        if recent:
            lines.append(texts.STATUS_RECENT +
                         " / ".join(f"「{r['text']}」" for r in recent))
        if mama:
            lines.append(texts.STATUS_MAMA_SAID +
                         " / ".join(f"「{r['text']}」" for r in mama))
        # 宝贝盒:词块索引纯派生的「他的宝贝」(索引空/故障=整行不出)
        try:
            from .visible_growth import treasure_list
            tr = treasure_list(conn, cid, top_n=3)
            if tr:
                lines.append(texts.STATUS_TREASURES +
                             "、".join(f"「{w}」" for w in tr))
        except Exception:
            pass
        # 消化过载提示
        s_now = child_mod.read_state(conn, cid, now=t, persist=False)
        if t >= child_mod._rules_v2_since(conn, cid) and \
                s_now.get("digest_load", 0.0) >= cfg.DIGEST_OVERLOAD_AT:
            lines.append(texts.STATUS_OVERLOAD_LINE)
        return "\n".join(lines)

    if cmd == "describe":
        if not rest:
            return texts.DESCRIBE_RULES
        if len(rest) > MAX_DESCRIBE_LEN:
            return texts.DESCRIBE_TOO_LONG.format(max_len=MAX_DESCRIBE_LEN)
        dup = conn.execute(
            "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=? LIMIT 1",
            (cid, f"appearance_{stage}")).fetchone()
        if dup is not None:
            return texts.DESCRIBE_DUP.format(stage_cn=STAGE_CN[stage])
        with child_mod.tx(conn):
            conn.execute("UPDATE child SET appearance=?, updated_at=? WHERE child_id=?",
                         (rest, t, cid))
            conn.execute(
                "INSERT INTO growth_album(child_id, item_kind, title, note, created_at)"
                " VALUES(?,?,?,?,?)",
                (cid, f"appearance_{stage}",
                 texts.MS_APPEARANCE_TITLE.format(name=name, stage_cn=STAGE_CN[stage]),
                 rest, t))
        return texts.DESCRIBE_OK.format(name=name, text=rest)

    if cmd == "feed":
        if not rest:
            return texts.FEED_EMPTY
        if len(rest) > MAX_FEED_LEN:
            return texts.FEED_TOO_LONG.format(max_len=MAX_FEED_LEN)
        r = child_mod.feed_corpus(conn, brain, cid, rest, source_kind="direct",
                                  speaker=persona, actor=PLAYER_DIR[persona],
                                  idempotency_key=None, now=t)
        if r["duplicate"]:
            return texts.FEED_DUP
        res = child_mod.child_speak(conn, brain, cid, trigger="feed", now=t)
        line = (texts.FEED_READ_RECEIPT.format(name=name) if res.refused
                else _speak_line(res, name))
        head = texts.FEED_OK.format(fed=r["fed"], nutrition=r["nutrition_delta"])
        if r.get("overloaded"):
            head += "\n" + texts.FEED_OVERLOAD_HINT
        return f"{head}\n{line}\n{_render_state(conn, cid, t)}"

    if cmd in ("soothe", "diaper", "burp", "play", "teach", "talk", "discipline"):
        child_mod.apply_action(conn, cid, PLAYER_DIR[persona], cmd,
                               idempotency_key=_idem(cmd, t), now=t)
        if cmd in ("teach", "talk") and rest:
            # 教的话/谈的心也是语料
            if len(rest) <= MAX_FEED_LEN:
                child_mod.feed_corpus(conn, brain, cid, rest, source_kind="direct",
                                      speaker=persona, actor=PLAYER_DIR[persona], now=t)
        res = child_mod.child_speak(conn, brain, cid, trigger=cmd, now=t)
        verb = texts.ACTION_VERBS[cmd]
        if res.refused:
            return (f"{verb}。\n{texts.ACTION_READ_RECEIPT.format(name=name)}\n"
                    f"{_render_state(conn, cid, t)}")
        return f"{verb}。\n{_speak_line(res, name)}\n{_render_state(conn, cid, t)}"

    if cmd in ("farewell", "stay"):
        # v0.4 新语义:告别窗开着(他自己提出「我想出去住了」)才有意义。
        # farewell=「我知道了。去吧。」落账后就地判结局;stay=「今天先别走」,
        # 他答应「那就明天」——可多次用,不延总窗(窗满他自己开口,tick 侧)。
        from .events import farewell_window, judge_ending
        if farewell_window(conn, cid) is None:
            return f"{name}还没提要走的事。"
        child_mod.apply_action(conn, cid, PLAYER_DIR[persona], cmd,
                               idempotency_key=_idem(cmd, t), now=t)
        if cmd == "stay":
            return texts.FAREWELL_STAY_REPLY
        ending = judge_ending(conn, brain, cid, now=t)
        tail = f"\n(结局:{texts.ENDING_CN.get(ending, ending)})" if ending else ""
        return f"{texts.FAREWELL_GO_REPLY}{tail}"

    if cmd == "choose":
        # 两难拍板(照护人专属;未登记 persona 在 run() 的白名单已拦)。
        # 编号=事件里带的 choice_id;后果真实落账,选完不能反悔。
        from .choices import resolve_choice
        parts = rest.split()
        if len(parts) != 2 or not parts[0].isdigit() or \
                parts[1].lower() not in ("a", "b"):
            return texts.CHOICE_USAGE
        r = resolve_choice(conn, brain, cid, int(parts[0]), parts[1].lower(),
                           now=t)
        if r["status"] == "not_found":
            return "没有这件事的编号。他没来问过,或者编号敲错了。"
        if r["status"] == "expired":
            return texts.CHOICE_EXPIRED_LINE
        if r["status"] == "already":
            return texts.CHOICE_ALREADY_LINE
        lines = [r["line"]] if r["line"] else []
        if r["said"]:
            lines.append(f"{name}:「{r['said']}」")
        lines.append(_render_state(conn, cid, t))
        return "\n".join(lines)

    if cmd == "album":
        rows = conn.execute(
            "SELECT title, note FROM growth_album WHERE child_id=?"
            " ORDER BY id DESC LIMIT 8", (cid,)).fetchall()
        if not rows:
            return texts.ALBUM_EMPTY
        return "\n".join(f"· {r['title']}" + (f":{r['note']}" if r["note"] else "")
                         for r in rows)

    if cmd == "log":
        rows = conn.execute(
            "SELECT kind, effective_at FROM action_log WHERE child_id=?"
            " ORDER BY id DESC LIMIT 10", (cid,)).fetchall()
        return "\n".join(f"· {r['kind']}" for r in rows) or texts.LOG_EMPTY

    return texts.UNKNOWN_CMD.format(cmd=cmd)


def init_birth(persona: str, name: str | None, now: float | None = None,
               embryo: bool = False) -> str:
    """建档(幂等):出生,或 embryo 占位胚胎(先占档,日后再"出生")。

    首行是机读结果(born:/embryo:/already:);出生成功后附开场文案(texts.OPENING)。
    与 run() 同级 flock:并发双击/超时重试绝不生出两个孩子。
    """
    sub = PLAYER_DIR[persona]
    db_path = _db_path(sub)
    lock_path = os.path.join(os.path.dirname(db_path), ".lock")
    with open(lock_path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            conn = pdb.connect(db_path)
            try:
                existing = conn.execute(
                    "SELECT child_id, status FROM child LIMIT 1").fetchone()
                if existing is not None:
                    # 孵化口:已有 embryo 档且这次不带 --embryo ⇒ 转正出生
                    if existing["status"] == "embryo" and not embryo:
                        cid = child_mod.hatch_child(conn, existing["child_id"],
                                                    name=name, now=now)
                        return f"born:{cid}\n\n" + _opening(name)
                    return f"already:{existing['child_id']}"
                status = "embryo" if embryo else "active"
                cid = child_mod.create_child(
                    conn, sub, name=name if status == "active" else None,
                    status=status, now=now)
                if status == "active":
                    return f"born:{cid}\n\n" + _opening(name)
                return f"embryo:{cid}"
            finally:
                conn.close()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def admin_freeze(persona: str, *, resume: bool = False,
                 now: float | None = None) -> str:
    """冻龄/解冻(管理面):对该 persona 库里的孩子操作,单行 JSON 回执。

    与 run() 同级 flock;无档=no_child;embryo 拒绝(pause_child 内闸)。"""
    sub = PLAYER_DIR[persona]
    db_path = _db_path(sub)
    lock_path = os.path.join(os.path.dirname(db_path), ".lock")
    with open(lock_path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            conn = pdb.connect(db_path)
            try:
                row = conn.execute("SELECT child_id FROM child LIMIT 1").fetchone()
                if row is None:
                    return json.dumps({"error": "no_child"}, ensure_ascii=False)
                fn = child_mod.resume_child if resume else child_mod.pause_child
                try:
                    res = fn(conn, row["child_id"], now=now)
                except ValueError as e:
                    return json.dumps({"error": str(e)}, ensure_ascii=False)
                return json.dumps({"child_id": row["child_id"], **res},
                                  ensure_ascii=False)
            finally:
                conn.close()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def admin_set_policy(persona: str, version: int,
                     now: float | None = None) -> str:
    """阶段表升版(管理面,单行 JSON 回执)。未知版号拒绝;幂等。

    老档升级路径:老孩子钉在建档时的 policy 不悄改人生进度;想给他续
    (如 teen 36→48)= 玩家显式跑这条。只升不降;倒龄守卫拒绝会让他
    从更大阶段退回更小阶段的升版(阶段史不倒序)。"""
    if version not in cfg.STAGE_SCHEDULES:
        return json.dumps({"error": f"unknown_policy:{version}"}, ensure_ascii=False)
    sub = PLAYER_DIR[persona]
    db_path = _db_path(sub)
    lock_path = os.path.join(os.path.dirname(db_path), ".lock")
    with open(lock_path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            conn = pdb.connect(db_path)
            try:
                row = conn.execute("SELECT child_id, stage_policy_version"
                                   " FROM child LIMIT 1").fetchone()
                if row is None:
                    return json.dumps({"error": "no_child"}, ensure_ascii=False)
                if row["stage_policy_version"] == version:
                    return json.dumps({"child_id": row["child_id"],
                                       "policy": version, "already": True},
                                      ensure_ascii=False)
                if version < row["stage_policy_version"]:
                    # 单向升版(降版=回头重写年龄语义,不开这个口)
                    return json.dumps({"error": "downgrade_forbidden"},
                                      ensure_ascii=False)
                child = child_mod.get_child(conn, row["child_id"])
                if child["status"] == "graduated":
                    return json.dumps({"error": "graduated_locked"},
                                      ensure_ascii=False)
                if child["born_at"] is not None:
                    # 倒龄守卫:升版不许让他从更大的阶段退回更小的(阶段史不倒序)
                    t0 = child_mod._now(now)
                    names = [n for n, _ in cfg.STAGE_SCHEDULES[version]]
                    cur = child_mod.stage_of(child, t0)
                    probe = dict(child)      # sqlite3.Row → dict,同键取值
                    probe["stage_policy_version"] = version
                    new_stage = child_mod.stage_of(probe, t0)
                    if cur not in names or new_stage not in names:
                        # fail closed:当前/新阶段名不在目标表=映射未知,不升
                        return json.dumps({"error": "stage_unmapped",
                                           "from": cur, "to": new_stage},
                                          ensure_ascii=False)
                    if names.index(new_stage) < names.index(cur):
                        return json.dumps({"error": "stage_would_regress",
                                           "from": cur, "to": new_stage},
                                          ensure_ascii=False)
                with child_mod.tx(conn):
                    conn.execute(
                        "UPDATE child SET stage_policy_version=?, updated_at=?"
                        " WHERE child_id=?",
                        (version, child_mod._now(now), row["child_id"]))
                return json.dumps({"child_id": row["child_id"], "policy": version,
                                   "already": False}, ensure_ascii=False)
            finally:
                conn.close()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def run(persona: str, cmd_argv: list, now: float | None = None) -> str:
    """同进程直接入口(测试用;仍走 flock)。"""
    sub = PLAYER_DIR.get(persona)
    if sub is None:
        raise ValueError(f"未知 persona {persona!r}(用 env NURSERY_PLAYERS 登记)")
    db_path = _db_path(sub)
    lock_path = os.path.join(os.path.dirname(db_path), ".lock")
    with open(lock_path, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            conn = pdb.connect(db_path)
            try:
                return dispatch(conn, persona, cmd_argv, now=now)
            finally:
                conn.close()
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def main(argv: list) -> int:
    if argv and argv[0] == "--tick":
        try:
            from dotenv import load_dotenv
            load_dotenv(".env", override=False)  # 定时器 cwd=仓根;已设的 env 不覆盖
        except ImportError:
            pass
        from .scheduler import tick_all
        print(json.dumps(tick_all(), ensure_ascii=False))
        return 0
    if argv and argv[0] == "--init-birth":
        embryo = "--embryo" in argv[1:]
        rest = [a for a in argv[1:] if a != "--embryo"]
        if not rest or rest[0] not in PLAYER_DIR:
            print("usage: --init-birth <persona> [名字] [--embryo]")
            return 2
        print(init_birth(rest[0], rest[1] if len(rest) > 1 else None, embryo=embryo))
        return 0
    if argv and argv[0] == "--set-policy":
        if len(argv) < 3 or argv[1] not in PLAYER_DIR or not argv[2].isdigit():
            print("usage: --set-policy <persona> <ver>")
            return 2
        print(admin_set_policy(argv[1], int(argv[2])))
        return 0
    if argv and argv[0] in ("--pause", "--resume"):
        if len(argv) < 2 or argv[1] not in PLAYER_DIR:
            print(f"usage: {argv[0]} <persona>")
            return 2
        print(admin_freeze(argv[1], resume=(argv[0] == "--resume")))
        return 0
    if len(argv) < 1 or argv[0] not in PLAYER_DIR:
        print("usage: <persona> <cmd> [args...] | --tick |"
              " --init-birth <persona> [名字] [--embryo] |"
              " --pause/--resume <persona> | --set-policy <persona> <ver>")
        return 2
    print(run(argv[0], argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
