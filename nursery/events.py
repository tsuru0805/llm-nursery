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
from .config import (DAILY_EVENT_P,
                     FIRST_SENTENCE_MIN_LEN, GIFT_EVENT_KEYS,
                     MILESTONE_NEW_CHARS_STEP, STAGE_CN, STAGE_SCHEDULE_V1,
                     SURPRISE_ANCHOR_MIN_RUN, SURPRISE_P_PER_TICK,
                     SURPRISE_WEEK_QUOTA)


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


def _prev_stage(stage: str, child=None) -> str | None:
    """策略表里 stage 的上一段(infant 无上一段=None)。装订漏拍补位用。
    按孩子的 policy 查表(各版名字现同,防未来版改名漏拍)。"""
    schedule = child_mod.stage_schedule_for(child) if child is not None \
        else STAGE_SCHEDULE_V1
    names = [s for s, _ in schedule]
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

    # ── 首次记录扩充(全部幂等,文案=texts 层)──
    # 第一次说「不要」
    if not _album_has(conn, child_id, "first_no"):
        row = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND text LIKE '%不要%' ORDER BY id LIMIT 1", (child_id,)).fetchone()
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone", item_kind="first_no",
                     title=texts.MS_FIRST_NO_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]) +
                     texts.MS_FIRST_NO_NOTE,
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_no:{child_id}", t=t, utterance_id=row["id"]):
                hit.append("first_no")

    # 第一次说自己的名字(有名才查;instr=字面子串,名字含 %/_ 不作通配)
    if child["name"] and not _album_has(conn, child_id, "first_own_name"):
        row = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND stage!='infant' AND instr(text, ?)>0 ORDER BY id LIMIT 1",
            (child_id, child["name"])).fetchone()
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone",
                     item_kind="first_own_name",
                     title=texts.MS_FIRST_NAME_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]) +
                     texts.MS_FIRST_NAME_NOTE,
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_own_name:{child_id}", t=t,
                     utterance_id=row["id"]):
                hit.append("first_own_name")

    # 第一次涌现句(足够长且与全部语料重合极低=没人教过的组合;挡婴儿乱语)
    if not _album_has(conn, child_id, "first_novel"):
        row = conn.execute(
            "SELECT id, text FROM utterance WHERE child_id=? AND accepted=1"
            " AND stage!='infant' AND LENGTH(text)>=6 AND max_source_overlap<=3"
            " ORDER BY id LIMIT 1", (child_id,)).fetchone()
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone",
                     item_kind="first_novel",
                     title=texts.MS_FIRST_NOVEL_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]) +
                     texts.MS_FIRST_NOVEL_NOTE,
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_novel:{child_id}", t=t,
                     utterance_id=row["id"]):
                hit.append("first_novel")

    # 第一次整词说话(词块起头的首句)。LIKE 只做预筛,真判定=解析 JSON 的
    # chunk 键非空(裸 LIKE 会被值里出现的 'chunk' 字样误触发)
    if not _album_has(conn, child_id, "first_chunk"):
        row = None
        for cand in conn.execute(
                "SELECT id, text, generation_params_json FROM utterance"
                " WHERE child_id=? AND accepted=1"
                " AND generation_params_json LIKE '%\"chunk\"%' ORDER BY id",
                (child_id,)):
            try:
                if json.loads(cand["generation_params_json"] or "{}").get("chunk"):
                    row = cand
                    break
            except ValueError:
                continue
        if row is not None:
            if _emit(conn, child_id, kind="nursery.milestone",
                     item_kind="first_chunk",
                     title=texts.MS_FIRST_CHUNK_TITLE.format(name=name),
                     note=texts.MS_QUOTE_NOTE.format(text=row["text"]) +
                     texts.MS_FIRST_CHUNK_NOTE,
                     payload={"utterance": row["text"]},
                     idem=f"ms:first_chunk:{child_id}", t=t,
                     utterance_id=row["id"]):
                hit.append("first_chunk")

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
            old_stage = _prev_stage(stage, child) or ""
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
    # v0.4:成年不是「长大了一点」——是成年日(当晚他会提出离开,tick_farewell_arc)
    stage_title = (texts.COMING_OF_AGE_TITLE.format(name=name) if stage == "adult"
                   else texts.MS_STAGE_TITLE.format(name=name,
                                                    stage_cn=STAGE_CN[stage]))
    ok = _emit(conn, child_id, kind="nursery.milestone",
               item_kind=f"stage_{stage}",
               title=stage_title,
               note=note, payload={"stage": stage},
               idem=f"ms:stage:{stage}:{child_id}", t=t)
    # 生日 set-piece:阶段跃迁=过一次生日,全家出席。
    # infant 不发(出生开场已有);runaway 期不开生日会(人不在家);
    # album 卡自带幂等键,与庆祝事件互不牵连。describe 邀请在上面已有,不重复。
    if stage != "infant" and child["status"] == "active":
        _emit(conn, child_id, kind="nursery.event",
              item_kind=f"birthday_{stage}",
              title=texts.BIRTHDAY_TITLE.format(name=name, stage_cn=STAGE_CN[stage]),
              note=texts.BIRTHDAY_NOTE,
              payload={"stage": stage, "birthday": True},
              idem=f"bday:{stage}:{child_id}", t=t)
    with tx(conn):
        conn.execute("UPDATE child SET celebrated_stage=?, updated_at=?"
                     " WHERE child_id=?", (stage, t, child_id))
    return stage if ok else None


# ────────────────────────── 每日随机事件 ──────────────────────────

def maybe_daily_event(conn, child_id: str, rng: random.Random, now=None, *,
                      brain=None) -> str | None:
    """确定性日抽签(若每 tick 独立抽 35%,一天几百拍≈每天必出,语义漂):
    以 (child, date) 种子一次性决定「今天有没有事+几点发生」,tick 只在到点后投递。

    送礼藏品卡:命中 GIFT_EVENT_KEYS(「捡东西给你」类)时,_emit 同事务在
    growth_album 落一张真实藏品卡(item_kind=gift_{key}_{date} 幂等),
    note 带他当场 speak 的一句(brain=None/speak 失败=卡照落、note 空,fail-open)。"""
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
    item_kind = note = None
    if key in GIFT_EVENT_KEYS:
        if conn.execute("SELECT 1 FROM outbox WHERE idempotency_key=?",
                        (idem,)).fetchone() is not None:
            return None  # 已发过:幂等重放不再白耗一次 speak/rng
        item_kind = f"gift_{key}_{date}"
        if brain is not None:
            try:
                # 崩溃重放防线:speak 提交后、_emit 前崩过=当日已有 gift
                # utterance(**含被拒的**——rejected 也提交了行、推进了 RNG),
                # 一律不再 speak;note 只认 accepted 且正文非空的那句
                day0 = time.mktime(time.strptime(date, "%Y-%m-%d"))
                prev = conn.execute(
                    "SELECT text, accepted FROM utterance WHERE child_id=?"
                    " AND trigger='gift' AND created_at>=?"
                    " ORDER BY id DESC LIMIT 1", (child_id, day0)).fetchone()
                if prev is not None:
                    voice = prev["text"] if prev["accepted"] else None
                else:
                    res = child_mod.child_speak(conn, brain, child_id,
                                                trigger="gift", now=t)
                    voice = res.text if res.accepted and res.text.strip() else None
                if voice and voice.strip():
                    note = texts.GIFT_ALBUM_NOTE.format(voice=voice)
            except Exception:
                note = None  # fail-open:嘴上没憋出话,东西照样递到你手里
    try:
        emitted = _emit(conn, child_id, kind="nursery.event", item_kind=item_kind,
                        title=text, note=note, payload={"event": key, "stage": stage},
                        idem=idem, t=t, expires_at=t + 86400)
    except Exception:
        if item_kind is None:
            raise  # 非送礼路径维持既有语义(事件系统本账,不在 fail-open 范围)
        # 藏品卡写不动=退化成普通每日事件(送礼件坏了绝不炸 tick;卡事务已回滚,
        # 重发 outbox 幂等键同一把,不会双事件)
        emitted = _emit(conn, child_id, kind="nursery.event", item_kind=None,
                        title=text, note=None,
                        payload={"event": key, "stage": stage},
                        idem=idem, t=t, expires_at=t + 86400)
    if emitted:
        return key
    return None


# ────────────────────────── 语出惊人 ──────────────────────────

def maybe_surprise(conn, brain, child_id: str, rng: random.Random, now=None) -> dict | None:
    """child/teen 期概率引爆:从偷学语料取锚,模型现场重新生成(过护栏),
    绝不是查库贴原文。滚动 7 天配额(v0.3,原「每阶段终身」烧干后整条机制
    永久哑火)+同锚窗只爆一次+锚滤渣(只从纯话芯连续段取锚,时间戳/markdown
    渣进不来)。"""
    from .chunks import _clean_runs
    from .decoder import speak
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    stage = child_mod.stage_of(child, t)
    quota = SURPRISE_WEEK_QUOTA.get(stage)
    if quota is None or child["status"] != "active":
        return None
    if rng.random() > SURPRISE_P_PER_TICK:
        return None
    used = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE child_id=? AND kind='nursery.surprise'"
        " AND idempotency_key LIKE ? AND next_attempt_at>=?",
        (child_id, "sp:%", t - 7 * 86400)).fetchone()[0]
    if used >= quota:
        return None
    # 最小间隔闸:周配额不挤在同一个钟头里烧完
    from .config import SURPRISE_MIN_GAP_H
    recent = conn.execute(
        "SELECT 1 FROM outbox WHERE child_id=? AND kind='nursery.surprise'"
        " AND idempotency_key LIKE ? AND next_attempt_at>=? LIMIT 1",
        (child_id, "sp:%", t - SURPRISE_MIN_GAP_H * 3600)).fetchone()
    if recent is not None:
        return None
    # 确定性采样:候选按 id 取全量(偷学有每日上限,量小),用传入 rng 抽样
    # ——同种子同库恒同结果(重复 tick 幂等,测试不偶发红)。
    rows = list(conn.execute(
        "SELECT id, source_ref, text FROM corpus_item WHERE child_id=?"
        " AND source_kind='archive' ORDER BY id DESC LIMIT 200",
        (child_id,)).fetchall())   # 近窗有界:长档不整表进内存
    rng.shuffle(rows)   # 洗牌必须做:不洗则配对钉死在已爆过的窗上永不再爆
    rows = rows[:6]
    # 锚词取自**至少两个不同窗**,由模型重新生成。
    # 滤渣:该行必须有 ≥SURPRISE_ANCHOR_MIN_RUN 的纯话芯段,锚只在段内取。
    by_win: dict[str, str] = {}
    for r in rows:
        w = (r["source_ref"] or "").split("@", 1)[0]
        if not w or w in by_win:
            continue
        runs = [run for run in _clean_runs(r["text"])
                if len(run) >= SURPRISE_ANCHOR_MIN_RUN]
        if runs:
            by_win[w] = max(runs, key=len)
    if len(by_win) < 2:
        return None
    (win_a, run_a), (win_b, run_b) = list(by_win.items())[:2]
    fired = conn.execute(
        "SELECT 1 FROM outbox WHERE child_id=? AND kind='nursery.surprise'"
        " AND (payload_json LIKE ? OR payload_json LIKE ?) LIMIT 1",
        (child_id, f'%"{win_a}"%', f'%"{win_b}"%')).fetchone()
    if fired is not None:
        return None  # 同锚窗只爆一次(win_id 约定为 uuid 类无 LIKE 元字符形态,接受此查询面)

    def _anchor(body: str) -> str:
        off = rng.randrange(0, len(body) - 3)
        return body[off:off + 3]

    seed = _anchor(run_a) + _anchor(run_b)  # 两窗话芯锚拼接起头
    res = speak(brain.model, brain.guard, stage, rng, seed=seed)
    if not res.accepted:
        return None
    name = child["name"] or texts.DEFAULT_CHILD_NAME
    idem = f"sp:{stage}:{child_id}:{int(t)}"
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

def closed_cry_nights(conn, child_id: str, t: float) -> list[dict]:
    """已完结(expires_at<=t)的 fired **主哭夜**逐夜账(单一权威口径):
    date/due_at/expires_at/responded(窗内 feed/soothe/diaper)。
    消费方=check_neglect(忽视账)+portrait(画像)。judge_ending 的按夜响应率
    自带兜底窗口逻辑,判定时刻所有夜早已完结,结果等价。
    只算真 fired 的夜:调度停摆导致 expired(孩子压根没哭出来)不怪照护人。"""
    out = []
    for ev in conn.execute(
            "SELECT due_at, expires_at, payload_json FROM scheduled_event"
            " WHERE child_id=? AND kind='night_cry' AND chain_id IS NULL"
            " AND status='fired' AND expires_at IS NOT NULL AND expires_at<=?"
            " ORDER BY due_at", (child_id, t)):
        responded = conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND effective_at BETWEEN ?"
            " AND ? AND kind IN ('feed','soothe','diaper') LIMIT 1",
            (child_id, ev["due_at"], ev["expires_at"])).fetchone() is not None
        out.append({"date": json.loads(ev["payload_json"] or "{}").get("date", ""),
                    "due_at": ev["due_at"], "expires_at": ev["expires_at"],
                    "responded": responded})
    return out


def check_neglect(conn, child_id: str, now=None) -> int:
    """一整晚夜哭零回应 → darkness+。
    对每个已完结的主哭夜检查一次,幂等键 neglect:{date}(apply_action 自带去重);
    逐夜口径=closed_cry_nights(与画像同源)。"""
    from .config import DARKNESS_NEGLECT_NIGHT
    t = _now(now)
    hit = 0
    for ev in closed_cry_nights(conn, child_id, t):
        date = ev["date"]
        if not date:
            continue
        if ev["responded"]:
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


# ── v0.4 毕业过渡:告别窗(替换 v0.3 告别门) ──

def _meta_get(conn, child_id: str, key: str) -> str | None:
    row = conn.execute("SELECT value FROM parenting_meta WHERE child_id=? AND key=?",
                       (child_id, key)).fetchone()
    return row["value"] if row is not None else None


def farewell_window(conn, child_id: str) -> dict | None:
    """告别窗状态:未开=None;开了={opened_at, opened_age}(opened_age=开窗时逻辑天,
    窗末判定用它——冻龄时窗口时钟跟着停,绝对时刻只做展示)。"""
    at = _meta_get(conn, child_id, "farewell_window_opened_at")
    age = _meta_get(conn, child_id, "farewell_window_opened_age")
    if at is None or age is None:
        return None
    return {"opened_at": float(at), "opened_age": float(age)}


def in_departure_window(conn, child_id: str, now=None) -> bool:
    """窗口静默闸:窗开着且还没告别(判完 ending 即不再算窗内)。
    窗口期=纯缓冲:asks/choices/chains/magic/sickness/每日随机全不排新。"""
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child["ending"]:
        return False
    return farewell_window(conn, child_id) is not None


def _local_midnight_of(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def tick_farewell_arc(conn, child_id: str, now=None) -> dict:
    """毕业过渡弧,每拍幂等推进:
    ①成年日前 3/2/1 天渐进预告(他自己开始变化,不直说要走);
    ②成年日当晚(≥20 点,或成年满 1 天兜底)他开口「我想出去住了」=告别窗开;
    ③窗内每日一条小变化(纯氛围);
    ④窗满 DEPARTURE_WINDOW_DAYS(逻辑天)没人开口 → 他自己告别(farewell 落账,
      actor='self'——绝不系统代照护人说,是他自己说的)。
    farewell/stay 指令语义在 driver;判结局在 judge_ending(同拍随后跑)。"""
    from .config import (DEPARTURE_WINDOW_DAYS, FAREWELL_WINDOW_EVENT_HOUR,
                         LEAVING_ANNOUNCE_HOUR, LEAVING_ANNOUNCE_MAX_LAG_DAYS,
                         PRE_FAREWELL_OFFSETS)
    t = _now(now)
    out: dict = {}
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child["ending"]:
        return out
    age = child_mod.logical_age_days(child, t)
    adult_start = child_mod.stage_schedule_for(child)[-2][1]  # teen 上限=成年日
    name = child["name"] or texts.DEFAULT_CHILD_NAME

    # ① 渐进预告(还在 teen 末尾;幂等各一次;进相册=毕业叙事的一部分)
    for i, off in enumerate(PRE_FAREWELL_OFFSETS, 1):
        if adult_start - off <= age < adult_start:
            if _emit(conn, child_id, kind="nursery.event",
                     item_kind=f"pre_farewell_{i}",
                     title=texts.PRE_FAREWELL_LINES[i - 1].format(name=name),
                     note=None, payload={"farewell_arc": f"pre_{i}"},
                     idem=f"prefw:{i}:{child_id}", t=t, expires_at=t + 86400):
                out[f"pre_{i}"] = True

    win = farewell_window(conn, child_id)

    # ② 成年日当晚开窗(是他提出离开)。「白天正常过成年日→当晚他才开口」的
    # 间隔要真实存在:成年跃迁事件落账 ≥2h 后才许宣告——防跃迁本身落在 20 点后
    # 时,生日会与「我想出去住了」同拍双发;lag 兜底不受此限。
    if win is None and age >= adult_start:
        hour = time.localtime(t).tm_hour
        adult_row = conn.execute(
            "SELECT created_at FROM growth_album WHERE child_id=?"
            " AND item_kind='stage_adult' LIMIT 1", (child_id,)).fetchone()
        settled_in = adult_row is not None and \
            t - adult_row["created_at"] >= 2 * 3600
        if (hour >= LEAVING_ANNOUNCE_HOUR and settled_in) or \
                age >= adult_start + LEAVING_ANNOUNCE_MAX_LAG_DAYS:
            emitted = _emit(conn, child_id, kind="nursery.milestone",
                            item_kind="leaving_announce",
                            title=texts.LEAVING_ANNOUNCE_TITLE.format(name=name),
                            note=texts.LEAVING_ANNOUNCE_NOTE,
                            payload={"farewell_arc": "announce"},
                            idem=f"leave:{child_id}", t=t)
            # meta 锚在 _emit 之后无条件补写(幂等):上一拍崩在事件提交与锚写入
            # 之间时,重放要能把锚补上——否则窗口永远开不了
            with tx(conn):
                for k, v in (("farewell_window_opened_at", str(t)),
                             ("farewell_window_opened_age", str(age))):
                    conn.execute(
                        "INSERT INTO parenting_meta(child_id, key, value,"
                        " updated_at) VALUES(?,?,?,?)"
                        " ON CONFLICT(child_id, key) DO NOTHING",
                        (child_id, k, v, t))
            if emitted:
                out["window_opened"] = True
            win = farewell_window(conn, child_id)

    if win is None:
        return out

    # ③ 窗内每日小变化(开窗后第 1/2/3 个本地日,白天投放;纯氛围幂等)
    opened_day0 = _local_midnight_of(win["opened_at"])
    for n in range(1, len(texts.FAREWELL_WINDOW_LINES) + 1):
        due = opened_day0 + n * 86400 + FAREWELL_WINDOW_EVENT_HOUR * 3600
        if t >= due:
            if _emit(conn, child_id, kind="nursery.event", item_kind=None,
                     title=texts.FAREWELL_WINDOW_LINES[n - 1].format(name=name),
                     note=None, payload={"farewell_arc": f"window_{n}"},
                     idem=f"fwwin:{n}:{child_id}", t=t, expires_at=t + 86400):
                out[f"window_{n}"] = True

    # ④ 窗满没人开口 → 他自己告别(逻辑天口径,冻龄安全;绝不代照护人说)。
    # 事件在前、落账在后:两半各自幂等——先落账后崩=下一拍整块跳过,
    # 「我走啦」那段叙事永久丢而结局照判;先发事件后崩=下一拍收敛无丢失。
    if age >= win["opened_age"] + DEPARTURE_WINDOW_DAYS:
        said = conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND kind='farewell'"
            " AND effective_at>=? LIMIT 1",
            (child_id, win["opened_at"])).fetchone()
        if said is None:
            _emit(conn, child_id, kind="nursery.milestone",
                  item_kind="self_farewell",
                  title=texts.SELF_FAREWELL_TITLE.format(name=name),
                  note=texts.SELF_FAREWELL_NOTE,
                  payload={"farewell_arc": "self_farewell"},
                  idem=f"selffw:ev:{child_id}", t=t)
            child_mod.apply_action(conn, child_id, "self", "farewell",
                                   idempotency_key=f"selffw:{child_id}",
                                   payload={"self_farewell": True}, now=t)
            out["self_farewell"] = True
    return out


def judge_ending(conn, brain, child_id: str, now=None) -> str | None:
    """告别之后→五分支结局。只判定+落数据。

    v0.4 门条件:结局必须发生在明确告别之后,但**不要求由谁发起**——
    告别窗开了(tick_farewell_arc)且窗开后存在任一照护人(或他自己
    actor='self')的 farewell 落账即判。system 不算(数据层白名单)。
    五分支判分口径与 v0.3 完全一致。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active" or child["ending"]:
        return None
    if child_mod.stage_of(child, t) != "adult":
        return None
    win = farewell_window(conn, child_id)
    if win is None:
        return None
    if conn.execute(
            "SELECT 1 FROM action_log WHERE child_id=? AND kind='farewell'"
            " AND actor!='system'"
            " AND effective_at>=? AND effective_at<=? LIMIT 1",
            (child_id, win["opened_at"], t)).fetchone() is None:
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
                     note=texts.ENDING_CN.get(ending),   # 判了哪个结局要说人话
                     payload=data, idem=f"end:{child_id}", t=t)
    # 毕业画像快照钉在判定时刻(告别信=第一封信的唯一事实源;拖到写信时才建
    # =state 已衰减)。失败不挡结局;首封信生成时兜底重建(brain 缺=不落缓存)。
    try:
        from .letters import graduation_portrait
        graduation_portrait(conn, brain, child_id, t)
    except Exception:
        pass
    return ending


def tick_events(conn, brain, child_id: str, now=None) -> dict:
    """scheduler 每拍调:全部事件检查。rng 用 (child, 时间片) 种子,重复 tick 幂等。"""
    t = _now(now)
    # 各机制独立种子命名空间:共用一个 rng 时,surprise 耗随机数会改变
    # runaway 的序列——一个机制的行为不许影响另一个的骰子
    rng = random.Random(f"{child_id}:events:{int(t // 300)}")
    rng_sp = random.Random(f"{child_id}:events:sp:{int(t // 300)}")
    rng_ra = random.Random(f"{child_id}:events:ra:{int(t // 300)}")
    out = {"milestones": check_milestones(conn, brain, child_id, now=t)}
    stage = check_stage_transition(conn, child_id, now=t)
    if stage:
        out["stage_up"] = stage
    neglect = check_neglect(conn, child_id, now=t)
    if neglect:
        out["neglect"] = neglect
    # v0.4 毕业过渡弧(预告/宣告开窗/窗口线/窗末他自告别;全幂等)
    arc = tick_farewell_arc(conn, child_id, now=t)
    if arc:
        out["farewell_arc"] = arc
    # 告别窗内=纯缓冲期:每日随机事件不抽(窗口有自己的小变化,不混台)
    if not in_departure_window(conn, child_id, now=t):
        ev = maybe_daily_event(conn, child_id, rng, now=t, brain=brain)
        if ev:
            out["daily"] = ev
    else:
        ev = None
    sp = maybe_surprise(conn, brain, child_id, rng_sp, now=t)
    if sp:
        out["surprise"] = True
    if maybe_runaway(conn, child_id, rng_ra, now=t):
        out["runaway"] = True
    end = judge_ending(conn, brain, child_id, now=t)
    if end:
        out["ending"] = end
    return out
