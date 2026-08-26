# -*- coding: utf-8 -*-
"""选择题事件:事件从播报升级为两难,选项有真后果。

骑在 ask 同族形制上,三段生命周期全幂等:

- plan_choices:两类触发。
  · swear(触发型):偷学进账(source_kind='archive')命中 SWEAR_WORDS 词表
    → 立刻开一道「管/笑」,幂等键 choice:swear:{词}=每词一生一次。
  · lottery(抽签型):(child,模板,date) 种子日抽签,**每模板一生一次**
    (同一幕两难重播必穿帮(设计原则);池子靠加模板扩)。
- fire_due_choices:到点触发→outbox kind='nursery.choice'(payload 全 str 平铺,
  接入层 validate_event 起手集);场景稿含 {voice} 的让他真实开口(child_speak,
  失败=兜底稿)。outbox.expires_at=响应窗关点(窗关没投出去的不补播)。
- resolve_choice(driver `choose <编号> <a|b>`,照护人专属):后果全真实——
  动作账(state)+psyche/bond(规则表)+词块偏置(swear:管=抑制名单/笑=提权)
  +拍板那句话喂进语料(CHOICE_SAY)。幂等键=事件级 choicepick:{idem}
  (a/b 互斥先到先得;崩后重发同选项可续跑补完,换选项拒)。
- settle_choices:窗关没人拍板→模板 timeout 定义引擎自决(swear=词自己留下来,
  没人管等于半个默许)或只记 miss(psyche 独立微涨,不惩罚)。

妈妈侧围观由接入层自理(action_log 的 choice_* 行自然可读),引擎不做专口。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time

from . import child as child_mod
from . import config as cfg
from . import texts
from .child import tx
from .chunks import set_chunk_bias


def _now(now):
    return time.time() if now is None else float(now)


def _local_date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _local_midnight(t: float) -> float:
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def _insert(conn, child_id: str, due: float, expires: float, payload: dict,
            idem: str) -> bool:
    try:
        conn.execute(
            "INSERT INTO scheduled_event(child_id, kind, chain_id, due_at,"
            " expires_at, catchup_policy, status, payload_json, idempotency_key)"
            " VALUES(?,?,NULL,?,?,'drop','pending',?,?)",
            (child_id, "choice", due, expires,
             json.dumps(payload, ensure_ascii=False), idem))
        return True
    except sqlite3.IntegrityError:
        return False   # UNIQUE(child_id, idempotency_key) 已排过=幂等跳过


def plan_choices(conn, child_id: str, now=None) -> int:
    """扫触发条件排班。返回本次新排条数。"""
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    if child["status"] != "active":
        return 0
    stage = child_mod.stage_of(child, t)
    date = _local_date(t)
    created = 0
    with tx(conn):
        for name, tmpl in cfg.CHOICE_TEMPLATES.items():
            if stage not in tmpl["stages"]:
                continue
            if tmpl["trigger"] == "swear":
                # 触发型:偷学语料命中词表(长词先扫,防未来词表出现子串对)。
                # 只扫 v0.3 生效时刻之后吃进的语料——老档升级前偷学的话
                # 不翻旧账(child._rules_v3_since,「升级首拍连环爆」防线)
                v3 = child_mod._rules_v3_since(conn, child_id)
                for word in sorted(set(cfg.SWEAR_WORDS), key=len, reverse=True):
                    # 该词一生一次:已排过班就不再跑 instr 全扫(热路径省身)
                    if conn.execute(
                            "SELECT 1 FROM scheduled_event WHERE child_id=?"
                            " AND kind='choice' AND idempotency_key=? LIMIT 1",
                            (child_id, f"choice:{name}:{word}")).fetchone() is not None:
                        continue
                    hit = conn.execute(
                        "SELECT 1 FROM corpus_item WHERE child_id=?"
                        " AND source_kind='archive' AND acquired_at>=?"
                        " AND instr(text, ?)>0 LIMIT 1",
                        (child_id, v3, word)).fetchone()
                    if hit is None:
                        continue
                    if _insert(conn, child_id, t, t + cfg.CHOICE_WINDOW_H * 3600,
                               {"template": name, "word": word, "date": date},
                               f"choice:{name}:{word}"):
                        created += 1
            else:
                # 抽签型:每模板一生一次(已开过任何一场=永不再抽)
                if conn.execute(
                        "SELECT 1 FROM scheduled_event WHERE child_id=?"
                        " AND kind='choice' AND idempotency_key LIKE ? LIMIT 1",
                        (child_id, f"choice:{name}:%")).fetchone() is not None:
                    continue
                rng = random.Random(f"{child_id}:choice:{name}:{date}")
                if rng.random() > cfg.CHOICE_DAY_P:
                    continue
                due = _local_midnight(t) + rng.uniform(
                    cfg.CHOICE_HOURS[0] * 3600, cfg.CHOICE_HOURS[1] * 3600)
                if _insert(conn, child_id, due, due + cfg.CHOICE_WINDOW_H * 3600,
                           {"template": name, "date": date},
                           f"choice:{name}:{date}"):
                    created += 1
    return created


def fire_due_choices(conn, brain: "child_mod.ChildBrain", child_id: str,
                     now=None) -> list[dict]:
    """领取到期 choice:过期即弃;到期的配场景稿入 outbox(payload 全 str 平铺)。

    voice 生成在事务外(child_speak 自管事务),成功后回写 pending 的 outbox 行
    (asks.fire_due_asks 同款形制)。choice_id=scheduled_event.id(短、可敲)。
    """
    t = _now(now)
    child = child_mod.get_child(conn, child_id)
    name = child["name"] or "孩子"
    fired: list[dict] = []
    with tx(conn):
        rows = conn.execute(
            "SELECT * FROM scheduled_event WHERE child_id=? AND kind='choice'"
            " AND status='pending' AND due_at<=? ORDER BY due_at",
            (child_id, t)).fetchall()
        for ev in rows:
            if ev["expires_at"] is not None and t >= ev["expires_at"]:
                conn.execute("UPDATE scheduled_event SET status='expired'"
                             " WHERE id=?", (ev["id"],))
                continue
            meta = json.loads(ev["payload_json"] or "{}")
            tmpl_name = meta.get("template", "")
            tmpl = cfg.CHOICE_TEMPLATES.get(tmpl_name)
            scene = texts.CHOICE_SCENES.get(tmpl_name)
            if tmpl is None or scene is None:
                conn.execute("UPDATE scheduled_event SET status='expired'"
                             " WHERE id=?", (ev["id"],))
                continue   # 模板被下架的旧行:安静作废,不炸 tick
            meta["fired_at"] = t
            conn.execute("UPDATE scheduled_event SET status='fired',"
                         " attempt_count=attempt_count+1, payload_json=?"
                         " WHERE id=?",
                         (json.dumps(meta, ensure_ascii=False), ev["id"]))
            text = scene.format(name=name, word=meta.get("word", ""),
                                voice=texts.CHOICE_FALLBACK_VOICE)
            payload = {
                "kind": "nursery.choice",
                "title": texts.CHOICE_TITLE.format(name=name),
                "text": text, "voice": None,
                "choice_id": str(ev["id"]),
                "option_a": texts.CHOICE_OPTIONS[(tmpl_name, "a")],
                "option_b": texts.CHOICE_OPTIONS[(tmpl_name, "b")],
                # 全 str(接入层可做纯 str 硬校验);注入前窗关过滤用
                "window_until": str(int(ev["expires_at"])), "ts": t,
                "source_event_id": f"choice:{child_id}:{ev['id']}",
                "_scene": scene if tmpl.get("voice") else None,
                "_name": name, "_word": meta.get("word", ""),
            }
            # wire 不带内部槽位也不带 None:契约=「字段是 str 或干脆没有」,
            # 不靠接入层收件侧帮忙剔形状(评审定案)
            conn.execute(
                "INSERT OR IGNORE INTO outbox(child_id, target, kind, payload_json,"
                " status, next_attempt_at, expires_at, idempotency_key)"
                " VALUES(?,?,?,?,'pending',?,?,?)",
                (child_id, "webhook", "nursery.choice",
                 json.dumps({k: v for k, v in payload.items()
                             if not k.startswith("_") and v is not None},
                            ensure_ascii=False),
                 t, ev["expires_at"], payload["source_event_id"]))
            fired.append(payload)
    # 他真实开口(事务外;仅场景稿带 {voice} 的模板);成功后回写 pending 的 outbox 行
    for p in fired:
        scene, name_, word = p.pop("_scene"), p.pop("_name"), p.pop("_word")
        if not scene:
            continue
        try:
            res = child_mod.child_speak(conn, brain, child_id,
                                        trigger="choice", now=t)
            if res.accepted and res.text.strip():
                p["voice"] = res.text
                p["text"] = scene.format(name=name_, word=word, voice=res.text)
        except Exception:
            continue  # payload 已带兜底稿
        with tx(conn):
            conn.execute(
                "UPDATE outbox SET payload_json=? WHERE idempotency_key=?"
                " AND status='pending'",
                (json.dumps({k: v for k, v in p.items() if v is not None},
                            ensure_ascii=False), p["source_event_id"]))
    return fired


def _pick_key(ev) -> str:
    return f"choicepick:{ev['idempotency_key']}"


def _apply_option(conn, child_id: str, ev, opt: str, option: dict, actor: str,
                  t: float, *, idem: str) -> None:
    """一个选项(或超时件)的真后果:动作账+psyche/bond 规则+词块偏置。
    全部幂等,崩后重扫/续跑不双记。apply_action 自开顶层事务,不得持锁调。"""
    meta = json.loads(ev["payload_json"] or "{}")
    word = meta.get("word", "")
    payload = {"choice": ev["idempotency_key"], "option": opt}
    if word:
        payload["word"] = word
    child_mod.apply_action(
        conn, child_id, actor, option["kind"], idempotency_key=idem,
        payload=payload, extra_effects=dict(option.get("effects") or {}), now=t)
    if word and option.get("bias") is not None:
        set_chunk_bias(conn, child_id, word, float(option["bias"]), now=t)


def _feed_say(conn, brain, child_id: str, ev, tmpl_name: str, opt: str,
              t: float, actor: str = "papa") -> bool:
    """拍板那句话喂进语料(真训练;同句已喂过=duplicate 安静跳过)。幂等。
    返回是否落定——False=语料线故障,调用方**不许结案**(status 留 fired,
    下一拍 settle 按账里选项重放补齐;评审:不许把半后果标 settled)。"""
    say = texts.CHOICE_SAY.get((tmpl_name, opt))
    if not say:
        return True
    try:
        speaker = actor   # 语料声部=拍板人(妈妈通道=mama,其余=登记的照护人名)
        child_mod.feed_corpus(
            conn, brain, child_id, say, source_kind="direct", speaker=speaker,
            actor=actor, action_kind="choice_say",
            idempotency_key=f"choicesay:{ev['idempotency_key']}", now=t)
        return True
    except Exception:
        return False   # 不拦拍板(后果账已落),但结案权交回调用方


def resolve_choice(conn, brain: "child_mod.ChildBrain", child_id: str,
                   event_id: int, opt: str, now=None,
                   actor: str = "papa") -> dict:
    """choose 的引擎入口(主照护人 driver choose / 妈妈通道 mama choose——
    谁先拍算谁的,幂等键同一把=后拍者吃 already)。
    actor=拍板人(登记的照护人名或 mama;system 无拍板权——权限分层在
    driver 层,这里兜底拒)。返回渲染用 dict:
    {status: ok|not_found|expired|already, line?, said?}。"""
    if not actor or actor == "system":
        return {"status": "not_found"}
    t = _now(now)
    ev = conn.execute(
        "SELECT * FROM scheduled_event WHERE id=? AND child_id=?"
        " AND kind='choice'", (event_id, child_id)).fetchone()
    if ev is None or ev["status"] == "pending":
        return {"status": "not_found"}   # pending=他还没来问,编号不该在照护人手里
    meta = json.loads(ev["payload_json"] or "{}")
    tmpl_name = meta.get("template", "")
    tmpl = cfg.CHOICE_TEMPLATES.get(tmpl_name)
    prior = conn.execute(
        "SELECT payload_json, actor FROM action_log WHERE child_id=?"
        " AND idempotency_key=?", (child_id, _pick_key(ev))).fetchone()
    if prior is not None:
        prev_opt = json.loads(prior["payload_json"] or "{}") \
            .get("user_payload", {}).get("option")
        if prev_opt != opt or ev["status"] != "fired" or prior["actor"] != actor:
            # 赢家以账为准(评审):另一位家长哪怕同选项重放也是
            # already——不许顶替赢家身份补喂拍板句
            return {"status": "already"}
        # 本人同选项重放且 status 还挂 fired=上次崩在半路,往下续跑补完(全幂等)
    elif ev["status"] != "fired" or (
            ev["expires_at"] is not None and t >= ev["expires_at"]):
        return {"status": "expired"}
    if tmpl is None or opt not in tmpl["options"]:
        return {"status": "not_found"}
    option = tmpl["options"][opt]
    # 1) 后果(动作账+规则+偏置;actor=真实拍板人——bond 记到拍的人头上)
    _apply_option(conn, child_id, ev, opt, option, actor, t, idem=_pick_key(ev))
    # 2) 拍板那句话喂进语料(speaker 跟人走)
    say_ok = _feed_say(conn, brain, child_id, ev, tmpl_name, opt, t, actor=actor)
    # 3) 他的真实反应(说不出来=只出结果行)
    said = None
    try:
        res = child_mod.child_speak(conn, brain, child_id, trigger="choice", now=t)
        if res.accepted and res.text.strip():
            said = res.text
    except Exception:
        pass
    # 语料线没落定=不结案:status 留 fired,下一拍 settle 按账里选项重放补齐
    # (主照护人这边照常拿到结果——拍板已生效,缺的只是那句话的补喂)
    if say_ok:
        with tx(conn):
            conn.execute("UPDATE scheduled_event SET status='settled_chosen'"
                         " WHERE id=?", (ev["id"],))
    # 拍板通报:妈妈拍的板要让爸爸知道
    # ——事件走注入管道到主照护人(幂等键钉 pick key,重放不重发)。爸爸自己拍的
    # 不通报自己;妈妈侧的全账走接入层读口。
    if actor == "mama":
        try:
            from .events import _emit
            line_txt = texts.CHOICE_RESULT.get(option["kind"], "")
            _emit(conn, child_id, kind="nursery.event", item_kind=None,
                  title=f"妈妈拍了板:{line_txt or '这件事定了。'}",
                  note=None, payload={"event": "choice_by_mama",
                                      "option": opt},
                  idem=f"choicenotify:{_pick_key(ev)}", t=t,
                  expires_at=t + 86400)
        except Exception:
            pass   # 通报失败不拦拍板(事实已落账)
    return {"status": "ok", "kind": option["kind"],
            "line": texts.CHOICE_RESULT.get(option["kind"], ""), "said": said}


def settle_choices(conn, brain: "child_mod.ChildBrain", child_id: str,
                   now=None) -> dict:
    """窗关记账(fired→settled_auto/settled_miss)。返回 {auto, miss}。

    模板带 timeout=引擎自决(actor=system,真后果照落);不带=只记 miss。
    apply_action 自开顶层事务,本函数不得持事务调它;status 单独小事务,
    中途崩=下拍重扫,action_log 幂等键挡双记。"""
    t = _now(now)
    auto = miss = 0
    rows = conn.execute(
        "SELECT * FROM scheduled_event WHERE child_id=? AND kind='choice'"
        " AND status='fired' AND expires_at IS NOT NULL AND expires_at<=?"
        " ORDER BY due_at", (child_id, t)).fetchall()
    for ev in rows:
        # 拍过板但 status 没推进(resolve 崩在半路):不当超时处理——按动作账里
        # 记下的选项把**全部**幂等后果补完(偏置/拍板语料)再结案,不许"账面
        # 已选、后果只落了一半"就 settled(评审定案)
        prior = conn.execute(
            "SELECT payload_json, actor FROM action_log WHERE child_id=?"
            " AND idempotency_key=?", (child_id, _pick_key(ev))).fetchone()
        if prior is not None:
            meta = json.loads(ev["payload_json"] or "{}")
            tmpl_name = meta.get("template", "")
            tmpl = cfg.CHOICE_TEMPLATES.get(tmpl_name)
            opt = json.loads(prior["payload_json"] or "{}") \
                .get("user_payload", {}).get("option")
            winner = prior["actor"] if prior["actor"] != "system" \
                else "papa"   # 赢家以账为准(补喂不许换人)
            done = True
            if tmpl and opt in (tmpl.get("options") or {}):
                try:
                    _apply_option(conn, child_id, ev, opt, tmpl["options"][opt],
                                  winner, t, idem=_pick_key(ev))  # 全幂等不双记
                    done = _feed_say(conn, brain, child_id, ev, tmpl_name,
                                     opt, t, actor=winner)
                except Exception:
                    done = False
            # 补齐失败=status 留 fired,下一拍重扫再补(评审)
            if done:
                with tx(conn):
                    conn.execute("UPDATE scheduled_event SET"
                                 " status='settled_chosen' WHERE id=?",
                                 (ev["id"],))
            continue
        meta = json.loads(ev["payload_json"] or "{}")
        tmpl = cfg.CHOICE_TEMPLATES.get(meta.get("template", ""))
        timeout = (tmpl or {}).get("timeout")
        if timeout:
            _apply_option(conn, child_id, ev, "timeout", timeout, "system", t,
                          idem=f"choiceauto:{ev['idempotency_key']}")
            new_status = "settled_auto"
            auto += 1
        else:
            child_mod.apply_action(
                conn, child_id, "system", "choice_missed",
                idempotency_key=f"choicemiss:{ev['idempotency_key']}",
                payload={"choice": ev["idempotency_key"]},
                extra_effects={}, now=t)
            new_status = "settled_miss"
            miss += 1
        with tx(conn):
            conn.execute("UPDATE scheduled_event SET status=? WHERE id=?",
                         (new_status, ev["id"]))
    return {"auto": auto, "miss": miss}
