# -*- coding: utf-8 -*-
"""事件系统:里程碑/每日随机/语出惊人/离家出走/结局判定。

产出双通道:growth_album(永久收藏,相册)+ outbox(投 webhook,kind 家族
nursery.milestone / nursery.event / nursery.surprise / nursery.runaway /
nursery.ending)。文案朴素键值制,想换语气改字符串即可。

幂等纪律:album 用 (child_id, item_kind) 查询幂等;outbox 用 idempotency_key;
每日事件 key=daily:{date};语出惊人每阶段配额+同锚一次。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time

from . import child as child_mod
from . import texts
from .child import tx
from .config import (ADULT_GRADUATE_DAYS, DAILY_EVENT_P,
                     FIRST_SENTENCE_MIN_LEN, MILESTONE_NEW_CHARS_STEP, STAGE_CN,
                     STAGE_SCHEDULE_V1, SURPRISE_P_PER_TICK, SURPRISE_STAGE_QUOTA)


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _album_has(conn, child_id: str, item_kind: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=? LIMIT 1",
        (child_id, item_kind)).fetchone() is not None


def _emit_locked(conn, child_id: str, *, kind: str, item_kind: str | None,
                 title: str, note: str | None, payload: dict, idem: str, t: float,
                 utterance_id: int | None = None,
                 expires_at: float | None = None) -> bool:
    """album(可选)+outbox 双写。**必须已在调用方事务内**;idem 冲突=已发过,返回 False。

    状态跃迁类事件(出走/结局)用这个版本,让「改状态」与「发事件」落同一事务
    ——避免状态提交后崩溃导致事件永久丢失(状态一变,下拍条件就不再成立)。"""
    dup = conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=?",
                       (idem,)).fetchone()
    if dup is not None:
        return False
    if item_kind is not None:
        conn.execute(
            "INSERT INTO growth_album(child_id, item_kind, utterance_id, title,"
            " note, created_at) VALUES(?,?,?,?,?,?)",
            (child_id, item_kind, utterance_id, title, note, t))
    body = {"kind": kind, "title": title, "note": note, "ts": t,
            "source_event_id": idem, **payload}
    conn.execute(
        "INSERT INTO outbox(child_id, target, kind, payload_json, status,"
        " next_attempt_at, expires_at, idempotency_key)"
        " VALUES(?,?,?,?,'pending',?,?,?)",
        (child_id, "webhook", kind, json.dumps(body, ensure_ascii=False),
         t, expires_at, idem))
    return True


def _emit(conn, child_id: str, *, kind: str, item_kind: str | None, title: str,
          note: str | None, payload: dict, idem: str, t: float,
          utterance_id: int | None = None, expires_at: float | None = None) -> bool:
    """_emit_locked 的自开事务版(无状态跃迁的普通事件用)。"""
    with tx(conn):
        return _emit_locked(conn, child_id, kind=kind, item_kind=item_kind,
                            title=title, note=note, payload=payload, idem=idem,
                            t=t, utterance_id=utterance_id, expires_at=expires_at)


# ──────────────── 阶段跃迁装订(旧阶段亲口语料 → 相册纪念件) ────────────────

KEEPSAKE_SPEAKER_ROLES = {"papa": "papa", "mama": "mama"}   # speaker → 声部键


def _prev_stage(stage: str) -> str | None:
    """策略表里 stage 的上一段(infant 无上一段=None)。装订漏拍补位用。"""
    names = [s for s, _ in STAGE_SCHEDULE_V1]
    i = names.index(stage) if stage in names else -1
    return names[i - 1] if i > 0 else None


def _bind_stage_keepsakes(conn, child, old_stage: str, t: float) -> list[str]:
    """离开旧阶段那一刻,把窗口内的亲口语料(source_kind='direct')按 speaker
    装订进成长相册。

    窗口=该 speaker 上一件**阶段系列**藏品(item_kind LIKE 'keepsake_stage_%_{role}')
    的 created_at 之后 → 跃迁时刻;无前件=born_at 起。day1 手工件
    (keepsake_papa_day1)是"第一天"特别件,不属阶段系列,窗口判定天然不含它。
    幂等:(child_id, item_kind) 已存在不重建;零语料不建空件。
    note 格式与 day1 件统一:「HH:MM · 正文」段落空行分隔(前端解析同一份);
    created_at=pinned_at=跃迁时刻(金边置顶)。只落相册不投 outbox——
    装订是静默归档,不是要递到谁面前的事件。
    """
    child_id = child["child_id"]
    made: list[str] = []
    with tx(conn):
        for speaker, role in KEEPSAKE_SPEAKER_ROLES.items():
            item_kind = f"keepsake_stage_{old_stage}_{role}"
            if conn.execute(
                    "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=?"
                    " LIMIT 1", (child_id, item_kind)).fetchone() is not None:
                continue  # 已装订过(幂等)
            prev = conn.execute(
                "SELECT MAX(created_at) FROM growth_album WHERE child_id=?"
                " AND item_kind LIKE ?",
                (child_id, f"keepsake_stage_%_{role}")).fetchone()[0]
            if prev is not None:
                cond, since = "acquired_at>?", prev   # 上件窗口收到 <=prev,不重不漏
            else:
                cond, since = "acquired_at>=?", child["born_at"] or 0.0
            rows = conn.execute(
                "SELECT text, acquired_at FROM corpus_item WHERE child_id=?"
                " AND source_kind='direct' AND speaker=? AND TRIM(text)!=''"
                f" AND {cond} AND acquired_at<=? ORDER BY acquired_at, id",
                (child_id, speaker, since, t)).fetchall()
            if not rows:
                continue  # 该窗口该 speaker 零语料=不建空件
            note = "\n\n".join(
                time.strftime("%H:%M", time.localtime(r["acquired_at"]))
                + f" · {r['text']}" for r in rows)
            conn.execute(
                "INSERT INTO growth_album(child_id, item_kind, title, note,"
                " created_at, pinned_at) VALUES(?,?,?,?,?,?)",
                (child_id, item_kind,
                 texts.KEEPSAKE_TITLE.format(stage_cn=STAGE_CN[old_stage],
                                             role_cn=texts.ROLE_CN[role]),
                 note, t, t))
            made.append(item_kind)
    return made


# ────────────────────────── 里程碑 ──────────────────────────

def check_milestones(conn, brain, child_id: str, now=None) -> list[str]:
    """扫已接受的 utterance/语料量,发未发过的里程碑。返回本轮触发的 item_kind。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    hit: list[str] = []

    # 第一次叫爸爸(utterance 首次含"爸爸"/"爸")
    if not _album_has(conn, child_id, "first_papa"):
        row = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND (text LIKE '%爸爸%' OR text LIKE '%爸%') ORDER BY id LIMIT 1",
            (child_id,)).fetchone()
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone",
                     item_kind="first_papa",
                     title=texts.MS_FIRST_PAPA_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]),
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_papa:{child_id}", t=t,
                     utterance_id=row["id"]):
                hit.append("first_papa")

    # 第一次独立成句(accepted 且长度≥阈值)
    if not _album_has(conn, child_id, "first_sentence"):
        row = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND LENGTH(text)>=? ORDER BY id LIMIT 1",
            (child_id, FIRST_SENTENCE_MIN_LEN)).fetchone()
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone",
                     item_kind="first_sentence",
                     title=texts.MS_FIRST_SENTENCE_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]),
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_sentence:{child_id}", t=t,
                     utterance_id=row["id"]):
                hit.append("first_sentence")

    # 词汇量步进(每 +MILESTONE_NEW_CHARS_STEP 新字一次)
    vocab = len(brain.model.vocab_by_freq())
    step = vocab // MILESTONE_NEW_CHARS_STEP
    if step >= 1:
        kind_key = f"vocab_{step}"
        if not _album_has(conn, child_id, kind_key):
            if _emit(conn, child_id, kind="nursery.milestone", item_kind=kind_key,
                     title=texts.MS_VOCAB_TITLE.format(
                         name=name, n=step * MILESTONE_NEW_CHARS_STEP),
                     note=None, payload={"vocab": vocab},
                     idem=f"ms:{kind_key}:{child_id}", t=t):
                hit.append(kind_key)
    return hit


def check_stage_transition(conn, child_id: str, now=None) -> str | None:
    """阶段跃迁只庆祝一次(celebrated_stage 记账)。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] not in ("active", "runaway"):
        return None
    stage = child_mod.stage_of(child, t)
    if stage == (child["celebrated_stage"] or "") or stage == "embryo":
        return None
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    # 离开旧阶段那一刻先装订——旧阶段窗口的亲口语料按 speaker 订进相册。
    # 出生跃入(embryo→infant)不装订:没有"旧阶段"可订。celebrated_stage 缺失
    # (调度停摆漏拍庆祝)时按策略表取当前阶段的上一段补位——窗口逻辑保证不漏话。
    if stage != "infant":
        old_stage = child["celebrated_stage"] or ""
        if old_stage == "embryo" or old_stage not in STAGE_CN:
            old_stage = _prev_stage(stage) or ""
        if old_stage:
            _bind_stage_keepsakes(conn, child, old_stage, t)
    # describe 邀请:新阶段还没记过样子 → 附言随里程碑事件递给照护人,
    # 免得他不知道相貌窗口开了(infant 不发:出生当天 status 里已有提示)
    note = None
    if stage != "infant":
        has_look = conn.execute(
            "SELECT 1 FROM growth_album WHERE child_id=? AND item_kind=? LIMIT 1",
            (child_id, f"appearance_{stage}")).fetchone()
        if has_look is None:
            note = texts.STAGE_APPEARANCE_INVITE
    ok = _emit(conn, child_id, kind="nursery.milestone",
               item_kind=f"stage_{stage}",
               title=texts.MS_STAGE_TITLE.format(name=name, stage_cn=STAGE_CN[stage]),
               note=note, payload={"stage": stage},
               idem=f"ms:stage:{stage}:{child_id}", t=t)
    with tx(conn):
        conn.execute("UPDATE child SET celebrated_stage=?, updated_at=?"
                     " WHERE child_id=?", (stage, t, child_id))
    return stage if ok else None


# ────────────────────────── 每日随机事件 ──────────────────────────

def maybe_daily_event(conn, child_id: str, rng: random.Random, now=None) -> str | None:
    """确定性日抽签(若每 tick 独立抽 35%,一天几百拍≈每天必出,语义漂):
    以 (child, date) 种子一次性决定「今天有没有事+几点发生」,tick 只在到点后投递。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    stage = child_mod.stage_of(child, t)
    pool = texts.DAILY_EVENTS.get(stage)
    if not pool or child["status"] != "active":
        return None
    date = _local_date(t)
    day_rng = random.Random(f"{child_id}:daily:{date}")
    if day_rng.random() > DAILY_EVENT_P:
        return None  # 今天注定平静(同一天重抽结果一样)
    happen_at = time.mktime(time.strptime(date, "%Y-%m-%d")) + \
        day_rng.uniform(9 * 3600, 21 * 3600)  # 事发时刻:白天 9-21 点随机
    if t < happen_at:
        return None  # 还没到那一刻
    key, text = pool[day_rng.randrange(len(pool))]
    idem = f"daily:{date}:{child_id}"
    if _emit(conn, child_id, kind="nursery.event", item_kind=None,
             title=text, note=None, payload={"event": key, "stage": stage},
             idem=idem, t=t, expires_at=t + 86400):
        return key
    return None


# ────────────────────────── 语出惊人 ──────────────────────────

def maybe_surprise(conn, brain, child_id: str, rng: random.Random, now=None) -> dict | None:
    """child/teen 期概率引爆:从偷学语料取锚,模型现场重新生成(过护栏),
    绝不是查库贴原文。每阶段配额+同锚窗只爆一次。"""
    from .decoder import speak
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    stage = child_mod.stage_of(child, t)
    quota = SURPRISE_STAGE_QUOTA.get(stage)
    if quota is None or child["status"] != "active":
        return None
    if rng.random() > SURPRISE_P_PER_TICK:
        return None
    used = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE child_id=? AND kind='nursery.surprise'"
        " AND idempotency_key LIKE ?", (child_id, f"sp:{stage}:%")).fetchone()[0]
    if used >= quota:
        return None
    pool = conn.execute(
        "SELECT id, source_ref, text FROM corpus_item WHERE child_id=?"
        " AND source_kind='archive' ORDER BY id DESC LIMIT 50", (child_id,)).fetchall()
    rows = rng.sample(pool, min(4, len(pool)))  # 注入的确定性 rng,同时间片可重放
    # 锚词取自**至少两个不同窗**,由模型重新生成
    by_win: dict[str, sqlite3.Row] = {}
    for r in rows:
        w = (r["source_ref"] or "").split("@", 1)[0]
        if w and w not in by_win and len(r["text"]) >= 6:
            by_win[w] = r
    if len(by_win) < 2:
        return None
    (win_a, row_a), (win_b, row_b) = list(by_win.items())[:2]
    fired = conn.execute(
        "SELECT 1 FROM outbox WHERE child_id=? AND kind='nursery.surprise'"
        " AND (payload_json LIKE ? OR payload_json LIKE ?) LIMIT 1",
        (child_id, f'%"{win_a}"%', f'%"{win_b}"%')).fetchone()
    if fired is not None:
        return None  # 同锚窗只爆一次(win_id 约定为 uuid 类无 LIKE 元字符形态,接受此查询面)

    def _anchor(body: str) -> str:
        off = rng.randrange(0, len(body) - 3)
        return body[off:off + 3]

    seed = _anchor(row_a["text"]) + _anchor(row_b["text"])  # 两窗锚拼接起头
    res = speak(brain.model, brain.guard, stage, rng, seed=seed)
    if not res.accepted:
        return None
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    idem = f"sp:{stage}:{child_id}:{used + 1}"
    payload = {"utterance": res.text, "anchor_wins": [win_a, win_b], "stage": stage}
    if _emit(conn, child_id, kind="nursery.surprise", item_kind=None,
             title=texts.SURPRISE_TITLE.format(name=name),
             note=texts.SURPRISE_NOTE.format(text=res.text), payload=payload,
             idem=idem, t=t, expires_at=t + 86400):
        with tx(conn):  # 说话必留痕:语出惊人同样进 utterance(trigger='surprise')
            conn.execute(
                "INSERT INTO utterance(child_id, trigger, model_snapshot_id, stage,"
                " text, generation_params_json, max_source_overlap, accepted,"
                " created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                (child_id, "surprise", brain.snapshot_id, stage, res.text,
                 json.dumps(dict(res.params, seed=seed), ensure_ascii=False),
                 res.max_overlap, t))
        return payload
    return None


# ────────────────────────── 夜哭忽视(黑暗值) ──────────────────────────

def check_neglect(conn, child_id: str, now=None) -> int:
    """一整晚夜哭零回应 → darkness+。
    对每个已过期的主哭夜检查一次,幂等键 neglect:{date}(apply_action 自带去重)。"""
    from .config import DARKNESS_NEGLECT_NIGHT
    t = _now(now)
    hit = 0
    rows = conn.execute(
        "SELECT due_at, expires_at, payload_json FROM scheduled_event"
        " WHERE child_id=? AND kind='night_cry' AND chain_id IS NULL"
        " AND status='fired' AND expires_at IS NOT NULL"
        " AND expires_at<=?", (child_id, t)).fetchall()
    # 只算真 fired 的夜:调度停摆导致 expired(孩子压根没哭出来)不怪照护人
    for ev in rows:
        date = json.loads(ev["payload_json"] or "{}").get("date", "")
        if not date:
            continue
        responded = conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND effective_at BETWEEN ? AND ?"
            " AND kind IN ('feed','soothe','diaper') LIMIT 1",
            (child_id, ev["due_at"], ev["expires_at"])).fetchone()
        if responded is not None:
            continue
        already = conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND idempotency_key=?",
            (child_id, f"neglect:{date}")).fetchone()
        if already is not None:
            continue  # 这晚已记过账
        child_mod.apply_action(
            conn, child_id, "system", "neglect",
            idempotency_key=f"neglect:{date}",
            payload={"date": date},
            extra_effects={"darkness": DARKNESS_NEGLECT_NIGHT, "mood": -4.0,
                           "intimacy": -2.0}, now=t)
        hit += 1
    return hit


# ────────────────────────── 离家出走 / 结局 ──────────────────────────

def maybe_runaway(conn, child_id: str, rng: random.Random, now=None) -> bool:
    from .config import RUNAWAY_DARKNESS, RUNAWAY_P_PER_TICK
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child_mod.stage_of(child, t) != "teen":
        return False
    st = child_mod.read_state(conn, child_id, now=t, persist=False)
    if st.get("darkness", 0) < RUNAWAY_DARKNESS:
        return False
    if rng.random() > RUNAWAY_P_PER_TICK:
        return False
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    with tx(conn):
        conn.execute("UPDATE child SET status='runaway', runaway_at=?, updated_at=?"
                     " WHERE child_id=?", (t, t, child_id))
        # 出走也是心理事件——与状态跃迁同事务落三轴账(独立+不安+自尊-)
        from .psyche import apply_rules_locked
        apply_rules_locked(conn, child_id, "runaway", t,
                           source_key=f"ra:{child_id}:{int(t)}")
        # 事件与状态跃迁同一事务(_emit_locked):提交后崩溃不会丢出走事件
        _emit_locked(conn, child_id, kind="nursery.runaway", item_kind="runaway",
                     title=texts.RUNAWAY_EVENT_TITLE.format(name=name),
                     note=texts.RUNAWAY_EVENT_NOTE,
                     payload={"runaway_at": t}, idem=f"ra:{child_id}:{int(t)}", t=t)
    return True


def judge_ending(conn, brain, child_id: str, now=None) -> str | None:
    """成年期满→五分支结局。只判定+落数据(告别信等文案归接入层自定)。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child["ending"]:
        return None
    if child_mod.stage_of(child, t) != "adult":
        return None
    age = child_mod.logical_age_days(child, t)
    from .config import STAGE_SCHEDULE_V1
    adult_start = STAGE_SCHEDULE_V1[-2][1]  # teen 上限=adult 起点
    if age < adult_start + ADULT_GRADUATE_DAYS:
        return None

    st = child_mod.read_state(conn, child_id, now=t, persist=False)
    total_chars = brain.model.total_chars or 1
    vocab = len(brain.model.vocab_by_freq())
    diversity = vocab / max(1.0, total_chars ** 0.5)   # 词汇/规模开方,粗多样性
    runaways = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE child_id=? AND kind='nursery.runaway'",
        (child_id,)).fetchone()[0]
    # 夜哭响应率按「夜」算:只数真 fired 的主哭夜(连击不摊分母),
    # 该夜窗口内有过 feed/soothe/diaper 才算响应——白天日常动作刷不满这项
    nights = conn.execute(
        "SELECT due_at, expires_at FROM scheduled_event WHERE child_id=?"
        " AND status='fired' AND kind='night_cry' AND chain_id IS NULL",
        (child_id,)).fetchall()
    responded = 0
    for ev in nights:
        win_end = ev["expires_at"] if ev["expires_at"] is not None \
            else ev["due_at"] + 3600
        hit = conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND effective_at BETWEEN ?"
            " AND ? AND kind IN ('feed','soothe','diaper') LIMIT 1",
            (child_id, ev["due_at"], win_end)).fetchone()
        if hit is not None:
            responded += 1
    response_rate = responded / len(nights) if nights else 1.0
    utt_total = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=?", (child_id,)).fetchone()[0]
    refused = conn.execute(
        "SELECT COUNT(*) FROM utterance WHERE child_id=?"
        " AND rejection_reason='refused'", (child_id,)).fetchone()[0]
    refusal_rate = refused / utt_total if utt_total else 0.0  # 拒绝采样率

    intimacy, darkness = st["intimacy"], st.get("darkness", 0)
    if runaways >= 2 and 40 <= intimacy <= 85 and darkness < 60:
        ending = "hidden_reunion"      # 隐藏:两次出走两次找回,和解重生
    elif intimacy >= 70 and darkness < 40 and refusal_rate < 0.3:
        ending = "reconciled"          # 理解与原谅(毕业)
    elif intimacy < 40 or darkness >= 75 or refusal_rate >= 0.4:
        ending = "independent"         # 离家独立
    elif response_rate < 0.3 or diversity < 1.0:
        ending = "silent"              # 沉默平凡
    else:
        ending = "precocious"          # 早熟毒舌出书
    data = {"ending": ending, "intimacy": round(intimacy, 1),
            "darkness": round(darkness, 1), "diversity": round(diversity, 2),
            "response_rate": round(response_rate, 2),
            "refusal_rate": round(refusal_rate, 2), "runaways": runaways,
            "vocab": vocab, "total_chars": total_chars}
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    with tx(conn):
        conn.execute("UPDATE child SET status='graduated', ending=?, updated_at=?"
                     " WHERE child_id=?", (ending, t, child_id))
        # 同一事务:毕业与结局事件不许拆开(状态一变,下拍就再也判不到结局)
        _emit_locked(conn, child_id, kind="nursery.ending",
                     item_kind=f"ending_{ending}",
                     title=texts.MS_ENDING_TITLE.format(name=name),
                     note=None, payload=data, idem=f"end:{child_id}", t=t)
    return ending


def tick_events(conn, brain, child_id: str, now=None) -> dict:
    """scheduler 每拍调:全部事件检查。rng 用 (child, 时间片) 种子,重复 tick 幂等。"""
    t = _now(now)
    rng = random.Random(f"{child_id}:events:{int(t // 300)}")
    out = {"milestones": check_milestones(conn, brain, child_id, now=t)}
    stage = check_stage_transition(conn, child_id, now=t)
    if stage:
        out["stage_up"] = stage
    neglect = check_neglect(conn, child_id, now=t)
    if neglect:
        out["neglect"] = neglect
    ev = maybe_daily_event(conn, child_id, rng, now=t)
    if ev:
        out["daily"] = ev
    sp = maybe_surprise(conn, brain, child_id, rng, now=t)
    if sp:
        out["surprise"] = True
    if maybe_runaway(conn, child_id, rng, now=t):
        out["runaway"] = True
    end = judge_ending(conn, brain, child_id, now=t)
    if end:
        out["ending"] = end
    return out
