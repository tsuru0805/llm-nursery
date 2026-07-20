# -*- coding: utf-8 -*-
"""LLM 心理层(可选:不配 API key 整层静默停用,孩子照旧纯 n-gram 说话)。

三层分工:
1. **程序层(本文件上半,可审计事实)**:事件/动作 → 三轴(不安/独立/自尊)增量的
   确定性规则表(config.PSYCHE_RULES),挂在 child._apply_action_locked 里与动作账
   同事务同幂等;婴儿期就开始记账,只是 DS 不上场。流水全进 psyche_axis_log。
2. **DS 决策层(本文件下半)**:maybe_decide() 把近期事件+轴趋势(只给方向不给裸数值)
   +成长履历摘录喂给 LLM(默认 DeepSeek,任何 OpenAI 兼容端点均可),拿回结构化 JSON
   (行为/姿态/锚词/证据引用/可不行动),全量留痕 psyche_decision。LLM 只做结构化
   决策的推理引擎,不直接替孩子出文。
3. **n-gram 嘴不退役**:锚词经 child.child_speak → decoder 做采样软偏置,护栏三层
   原封不动;无有效决策=零偏置照旧。

fail-open 铁律:LLM 不可用/超时/坏 JSON/超预算,孩子照旧纯 n-gram 说话,
绝不因心理层挂掉不说话、不炸 tick。
阶段闸:embryo/infant 不调 DS(轴照记),toddler 起生效(cfg.PSYCHE_DS_STAGES)。
预算闸:当日真出网次数 ≥ cfg.PSYCHE_DAILY_CALL_MAX ⇒ 当日 fail-open(留痕一次可审计)。

依赖纪律:纯标准库。DS key 走 env DEEPSEEK_API_KEY,永不落库不打印。
循环依赖纪律:child.py 顶层 import 本模块 ⇒ 本模块**只在函数内**懒 import child。
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request

from . import config as cfg
from .psyche_prompt import PSYCHE_PROMPT, PSYCHE_PROMPT_VERSION

_TREND_CN = {"rising": "在上升", "falling": "在下降", "flat": "平稳"}

# 输入摘要的中文渲染(给 DS 看的事实行)
_ACTOR_CN = {"papa": "爸爸", "mama": "妈妈", "system": ""}
_KIND_CN = {
    "feed": "喂了他,跟他说了话", "soothe": "哄了他", "diaper": "给他换了尿布",
    "burp": "给他拍了嗝", "play": "陪他玩了一阵", "talk": "跟他谈了心",
    "teach": "教了他东西", "discipline": "管教了他", "homecoming": "把他找回了家",
    "neglect": "他一整晚哭了,没有人来", "runaway": "他离家出走了",
    "mama_hug": "抱了抱他", "mama_soothe": "哄了他", "mama_touch": "摸了摸他",
    "mama_say": "跟他说了话",
}


def _local_midnight(t: float) -> float:
    """本地当日零点(与 scheduler 同口径;为免 child→psyche→scheduler 循环,微复制)。"""
    lt = time.localtime(t)
    return t - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)


def _payload_dict(raw) -> dict:
    """payload_json 防御性解析:坏 JSON/合法但非对象(null/[]/1)一律
    空 dict——心理层任何读入都不许因脏行抛异常(scheduler 会吞掉导致决策静默停摆)。"""
    try:
        p = json.loads(raw or "{}")
    except ValueError:
        return {}
    return p if isinstance(p, dict) else {}


def _event_ts(p: dict):
    """payload.ts → 有限实数或 None(bool 是 int 子类要排除,
    Infinity 能被 json.loads 解析进来也要排除)。"""
    ts = p.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not math.isfinite(ts):
        return None
    return float(ts)


# ────────────────────────── 程序层:轴与规则表 ──────────────────────────

def _ensure_axes_locked(conn, child_id: str, t: float) -> None:
    """三轴行不存在则按出生基线补齐(懒建:老档/生产库迁移后无需回填)。"""
    for axis in cfg.PSYCHE_AXES:
        conn.execute(
            "INSERT OR IGNORE INTO psyche_axis(child_id, axis, value, updated_at)"
            " VALUES(?,?,?,?)", (child_id, axis, cfg.PSYCHE_BASELINE[axis], t))


def read_axes(conn, child_id: str) -> dict:
    """三轴当前值(无行=出生基线)。DS 决策层拿趋势不拿这个;此口留给围观/调试。"""
    vals = dict(cfg.PSYCHE_BASELINE)
    for r in conn.execute(
            "SELECT axis, value FROM psyche_axis WHERE child_id=?", (child_id,)):
        if r["axis"] in vals:
            vals[r["axis"]] = r["value"]
    return vals


def _bump_locked(conn, child_id: str, deltas: dict, *, reason: str,
                 source_key: str | None, t: float) -> dict:
    """轴增量落账(0-100 夹取+流水)。必须已在调用方事务内。"""
    _ensure_axes_locked(conn, child_id, t)
    applied: dict = {}
    for axis, delta in deltas.items():
        if axis not in cfg.PSYCHE_AXES or not delta:
            continue
        cur = conn.execute(
            "SELECT value FROM psyche_axis WHERE child_id=? AND axis=?",
            (child_id, axis)).fetchone()["value"]
        after = max(0.0, min(100.0, cur + delta))
        conn.execute(
            "UPDATE psyche_axis SET value=?, updated_at=? WHERE child_id=? AND axis=?",
            (after, t, child_id, axis))
        conn.execute(
            "INSERT INTO psyche_axis_log(child_id, axis, delta, value_after, reason,"
            " source_key, created_at) VALUES(?,?,?,?,?,?,?)",
            (child_id, axis, delta, after, reason, source_key, t))
        applied[axis] = delta
    return applied


def _open_night_cry_date(conn, child_id: str, t: float) -> str | None:
    """t 时刻是否落在某个已 fired 未过期的夜哭窗口内;是则返回该夜 date。"""
    row = conn.execute(
        "SELECT payload_json FROM scheduled_event WHERE child_id=? AND kind='night_cry'"
        " AND status='fired' AND due_at<=? AND expires_at IS NOT NULL AND expires_at>?"
        " ORDER BY due_at DESC LIMIT 1", (child_id, t, t)).fetchone()
    if row is None:
        return None
    return _payload_dict(row["payload_json"]).get("date") or None


def apply_rules_locked(conn, child_id: str, kind: str, t: float, *,
                       source_key: str | None = None) -> dict:
    """事件/动作 → 轴增量(确定性规则表)。**必须在调用方事务内**——
    child._apply_action_locked 调用=与动作账同事务同幂等(重放动作在落账前早退,
    规则不会双记)。返回 {axis: delta} 供动作账 payload 留痕。"""
    applied: dict = {}
    deltas = cfg.PSYCHE_RULES.get(kind)
    if deltas:
        applied.update(_bump_locked(conn, child_id, deltas,
                                    reason=kind, source_key=source_key, t=t))
    # 夜哭被响应→不安-(动作规则之外的额外加成;每晚只记一次)
    if kind in cfg.PSYCHE_NIGHT_RESPONSE_KINDS:
        date = _open_night_cry_date(conn, child_id, t)
        if date:
            sk = f"nightresp:{date}"
            dup = conn.execute(
                "SELECT 1 FROM psyche_axis_log WHERE child_id=? AND source_key=?"
                " LIMIT 1", (child_id, sk)).fetchone()
            if dup is None:
                for ax, dv in _bump_locked(
                        conn, child_id, cfg.PSYCHE_NIGHT_RESPONSE_BONUS,
                        reason="night_cry_responded", source_key=sk, t=t).items():
                    applied[ax] = applied.get(ax, 0.0) + dv
    return applied


def _trend_word(net: float) -> str:
    if abs(net) < cfg.PSYCHE_TREND_FLAT_EPS:
        return "flat"
    return "rising" if net > 0 else "falling"


def axis_trends(conn, child_id: str, t: float) -> dict:
    """近 PSYCHE_TREND_WINDOW_H 小时轴流水净变化 → 方向词(DS 只拿方向)。"""
    t0 = t - cfg.PSYCHE_TREND_WINDOW_H * 3600
    net = {a: 0.0 for a in cfg.PSYCHE_AXES}
    for r in conn.execute(
            "SELECT axis, SUM(delta) AS s FROM psyche_axis_log"
            " WHERE child_id=? AND created_at>=? GROUP BY axis", (child_id, t0)):
        if r["axis"] in net:
            net[r["axis"]] = r["s"] or 0.0
    return {a: _trend_word(v) for a, v in net.items()}


def darkness_trend(conn, child_id: str, t: float) -> str:
    """黑暗值趋势(DS 只读不写):从动作账 state_before/after 差分求净变化方向。"""
    t0 = t - cfg.PSYCHE_TREND_WINDOW_H * 3600
    net = 0.0
    for r in conn.execute(
            "SELECT payload_json FROM action_log WHERE child_id=? AND created_at>=?",
            (child_id, t0)):
        p = _payload_dict(r["payload_json"])
        sa, sb = p.get("state_after"), p.get("state_before")
        if not (isinstance(sa, dict) and isinstance(sb, dict)):
            continue
        try:
            net += float(sa.get("darkness", 0.0) or 0.0) - \
                float(sb.get("darkness", 0.0) or 0.0)
        except (ValueError, TypeError):
            continue
    return _trend_word(net)


# ────────────────────────── DS 决策层 ──────────────────────────

def build_input(conn, child_id: str, child_row, stage: str, t: float) -> tuple:
    """组装 DS 输入:近期事件(带编号)+轴趋势(方向)+成长履历摘录+近期话语。

    返回 (digest_dict, valid_ids, prompt_str)。编号 a<action_log.id>/g<growth_album.id>
    =证据引用的合法域(判断必须指回履历证据)。"""
    from . import child as child_mod
    name = child_row["name"] or "孩子"
    age_days = int(child_mod.logical_age_days(child_row, t)) + 1
    appearance = child_row["appearance"] or "还没人描述过他的样子"

    trends = axis_trends(conn, child_id, t)
    dark = darkness_trend(conn, child_id, t)
    trend_lines = [f"  - {cfg.PSYCHE_CN[a]}:{_TREND_CN[trends[a]]}"
                   for a in cfg.PSYCHE_AXES]
    trend_lines.append(f"  - 叛逆(只读参考):{_TREND_CN[dark]}")

    valid_ids: set = set()
    event_lines: list[str] = []
    for r in conn.execute(
            "SELECT id, kind, actor, effective_at FROM action_log WHERE child_id=?"
            " ORDER BY id DESC LIMIT ?", (child_id, cfg.PSYCHE_INPUT_ACTIONS)):
        rid = f"a{r['id']}"
        valid_ids.add(rid)
        hrs = max(0.0, (t - r["effective_at"]) / 3600.0)
        who = _ACTOR_CN.get(r["actor"], r["actor"])
        event_lines.append(
            f"  [{rid}] {hrs:.1f} 小时前:{who}{_KIND_CN.get(r['kind'], r['kind'])}")
    # 氛围事件也进近期事件:每日随机/语出惊人/夜哭,
    # 出自 outbox(事件系统的既有真相层),编号 o<id> 同样可作证据引用
    for r in conn.execute(
            "SELECT id, kind, payload_json FROM outbox WHERE child_id=? AND kind IN"
            " ('nursery.event','nursery.surprise','nursery.cry')"
            " ORDER BY id DESC LIMIT ?", (child_id, cfg.PSYCHE_INPUT_EVENTS)):
        oid = f"o{r['id']}"
        valid_ids.add(oid)
        p = _payload_dict(r["payload_json"])
        title = str(p.get("title") or p.get("text") or r["kind"])[:60]
        ts = _event_ts(p)
        when = f"{max(0.0, (t - ts) / 3600.0):.1f} 小时前:" if ts is not None else ""
        event_lines.append(f"  [{oid}] {when}{title}")
    if not event_lines:
        event_lines = ["  (最近很安静,什么都没发生)"]

    album_lines: list[str] = []
    for r in conn.execute(
            "SELECT id, title, note FROM growth_album WHERE child_id=?"
            " ORDER BY id DESC LIMIT ?", (child_id, cfg.PSYCHE_INPUT_ALBUM)):
        gid = f"g{r['id']}"
        valid_ids.add(gid)
        note = f"({r['note'][:40]})" if r["note"] else ""
        album_lines.append(f"  [{gid}] {r['title']}{note}")
    if not album_lines:
        album_lines = ["  (履历还是空的)"]

    recent = [r["text"] for r in conn.execute(
        "SELECT text FROM utterance WHERE child_id=? AND accepted=1"
        " ORDER BY id DESC LIMIT ?", (child_id, cfg.PSYCHE_INPUT_UTTER))]
    recent_lines = [f"  「{x}」" for x in recent] or ["  (还没怎么说过话)"]

    digest = {
        "prompt_version": PSYCHE_PROMPT_VERSION, "stage": stage, "age_days": age_days,
        "trends": trends, "darkness_trend": dark,
        "events": event_lines, "album": album_lines, "recent": recent,
    }
    prompt = PSYCHE_PROMPT.format(
        name=name, stage_cn=cfg.STAGE_CN.get(stage, stage), age_days=age_days,
        appearance=appearance, trend_lines="\n".join(trend_lines),
        event_lines="\n".join(event_lines), album_lines="\n".join(album_lines),
        recent_lines="\n".join(recent_lines))
    return digest, valid_ids, prompt


def _ds_complete(prompt: str, *, timeout: float | None = None) -> dict:
    """真 DS 调用(OpenAI 兼容 /chat/completions,纯 urllib 零依赖)。
    默认 DeepSeek;env DEEPSEEK_BASE / PSYCHE_DS_MODEL 可指向任何 OpenAI 兼容端点。
    注:DeepSeek V4 必须显式 thinking disabled,否则小 max_tokens 下 content 恒空。
    测试永不走到这里(maybe_decide 注入 ds_complete mock)。"""
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    base = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")
    model = os.getenv("PSYCHE_DS_MODEL", cfg.PSYCHE_DS_MODEL_DEFAULT)
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.PSYCHE_DS_TEMPERATURE,
            "max_tokens": cfg.PSYCHE_DS_MAX_TOKENS,
            "thinking": {"type": "disabled"}}
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(
            req, timeout=timeout or cfg.PSYCHE_DS_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    return {"content": data["choices"][0]["message"]["content"] or "",
            "model": data.get("model") or model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens")}


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, TimeoutError):   # socket.timeout=TimeoutError(py3.10+)
        return True
    return isinstance(getattr(e, "reason", None), TimeoutError)  # URLError 包装


def _parse_decision(content: str, valid_ids: set) -> dict:
    """DS 返回 → 结构化决策。任何不合格 raise ValueError ⇒ 上层记 bad_json fail-open。"""
    s = (content or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no_json_object")
    d = json.loads(s[i:j + 1])   # JSONDecodeError 是 ValueError 子类
    if not isinstance(d, dict):
        raise ValueError("not_object")
    no_action = bool(d.get("no_action", False))
    behavior = str(d.get("behavior") or "").strip()[:60]
    if not behavior:
        if not no_action:
            raise ValueError("missing_behavior")
        behavior = "不行动"
    posture = str(d.get("posture") or "").strip()[:60]
    anchors_raw = d.get("anchor_words") if d.get("anchor_words") is not None else []
    if not isinstance(anchors_raw, list):
        raise ValueError("anchor_words_not_list")
    anchors: list[str] = []
    for w in anchors_raw:
        w = str(w).strip()
        if w and len(w) <= 8:
            anchors.append(w)
        if len(anchors) >= cfg.PSYCHE_MAX_ANCHORS:
            break
    if no_action:
        anchors = []   # 「不行动/说不出来」=不给嘴递词
    ev_raw = d.get("evidence") if d.get("evidence") is not None else []
    if not isinstance(ev_raw, list):
        raise ValueError("evidence_not_list")
    evidence = [str(e).strip() for e in ev_raw if str(e).strip() in valid_ids]
    if not no_action and not evidence:
        raise ValueError("no_valid_evidence")   # 判断必须指回履历证据
    return {"behavior": behavior, "posture": posture, "anchor_words": anchors,
            "evidence": evidence, "no_action": no_action,
            "reason": str(d.get("reason") or "").strip()[:200]}


def _insert_decision(conn, child_id: str, *, stage: str, trigger: str, status: str,
                     t: float, api_called: int = 0, digest: dict | None = None,
                     parsed: dict | None = None, raw: str | None = None,
                     error: str | None = None, model: str | None = None,
                     latency_ms: int | None = None, prompt_tokens: int | None = None,
                     completion_tokens: int | None = None) -> int:
    from .child import tx
    p = parsed or {}
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO psyche_decision(child_id, stage, trigger, status, api_called,"
            " input_digest_json, behavior, posture, anchor_words_json, evidence_json,"
            " no_action, raw_json, error, model, latency_ms, prompt_tokens,"
            " completion_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_id, stage, trigger, status, api_called,
             json.dumps(digest, ensure_ascii=False) if digest else None,
             p.get("behavior"), p.get("posture"),
             json.dumps(p.get("anchor_words"), ensure_ascii=False)
             if p.get("anchor_words") is not None else None,
             json.dumps(p.get("evidence"), ensure_ascii=False)
             if p.get("evidence") is not None else None,
             1 if p.get("no_action") else 0, raw, error, model, latency_ms,
             prompt_tokens, completion_tokens, t))
        return cur.lastrowid


def maybe_decide(conn, child_id: str, *, trigger: str = "tick",
                 now: float | None = None, ds_complete=None) -> dict | None:
    """tick 路径的心理决策入口(scheduler 每拍调;fail-open,永不炸调用方)。

    闸门顺序:active → 阶段闸 → 节流(1h,含失败尝试)→ 活动闸(没新事不烧钱)
    → 预算闸(当日真出网 ≥ 上限=fail-open,留痕一次/日)→ 无 key 留痕 → 真调。
    返回 None=被闸(静默);返回 dict=有留痕(含失败态,status 字段区分)。
    DS 调用发生在**事务外**(20s 网络等待绝不押着写锁)。
    ⚠并发口径:节流/活动/预算闸是「读-出网-写」非原子,
    **跨进程互斥由调用方持每库 .psyche.lock 保证**(scheduler tick 已持,
    非阻塞抢不到即跳过);别的入口要调本函数,同样先拿这把锁。"""
    if conn.in_transaction:   # 公开入口禁嵌套(与 child.tx 同纪律):
        raise RuntimeError(   # 绝不允许带着外部事务去等 20s 网络
            "maybe_decide 不支持在外部事务内调用(网络调用不押事务)")
    from . import child as child_mod
    t = child_mod._now(now)
    child_row = child_mod.get_child(conn, child_id)
    if child_row["status"] != "active":
        return None
    stage = child_mod.stage_of(child_row, t)
    if stage not in cfg.PSYCHE_DS_STAGES:
        return None   # 阶段闸:embryo/infant 轴照记,DS 不上场

    last = conn.execute(
        "SELECT id, created_at FROM psyche_decision WHERE child_id=?"
        " ORDER BY id DESC LIMIT 1", (child_id,)).fetchone()
    if last is not None and t - last["created_at"] < cfg.PSYCHE_MIN_INTERVAL_S:
        return None   # 节流
    since = last["created_at"] if last is not None else 0.0
    fresh = conn.execute(
        "SELECT 1 FROM action_log WHERE child_id=? AND created_at>? LIMIT 1",
        (child_id, since)).fetchone() or conn.execute(
        "SELECT 1 FROM growth_album WHERE child_id=? AND created_at>? LIMIT 1",
        (child_id, since)).fetchone()
    if fresh is None:
        # 氛围事件也算新活动:以 payload.ts 判定——入队即写死永不改写
        # (next_attempt_at 会被投递 backoff 改写,旧事件会被反复当新活动,
        # webhook 故障能吃光当日预算,不可作代理)
        for r in conn.execute(
                "SELECT payload_json FROM outbox WHERE child_id=? AND kind IN"
                " ('nursery.event','nursery.surprise','nursery.cry')"
                " ORDER BY id DESC LIMIT 10", (child_id,)):
            ts = _event_ts(_payload_dict(r["payload_json"]))
            # since < ts <= t:未来时间戳的脏数据不许持续开闸
            if ts is not None and since < ts <= t:
                fresh = True
                break
    if not fresh:
        return None   # 活动闸:没新东西可消化

    day0 = _local_midnight(t)
    used = conn.execute(
        "SELECT COUNT(*) FROM psyche_decision WHERE child_id=? AND api_called=1"
        " AND created_at>=?", (child_id, day0)).fetchone()[0]
    if used >= cfg.PSYCHE_DAILY_CALL_MAX:
        already = conn.execute(
            "SELECT 1 FROM psyche_decision WHERE child_id=? AND"
            " status='budget_exceeded' AND created_at>=? LIMIT 1",
            (child_id, day0)).fetchone()
        if already is None:   # 当日留痕一次,不刷屏
            did = _insert_decision(conn, child_id, stage=stage, trigger=trigger,
                                   status="budget_exceeded", t=t)
            return {"decision_id": did, "status": "budget_exceeded"}
        return None
    if ds_complete is None and not os.getenv("DEEPSEEK_API_KEY", ""):
        did = _insert_decision(conn, child_id, stage=stage, trigger=trigger,
                               status="no_key", t=t)
        return {"decision_id": did, "status": "no_key"}

    digest, valid_ids, prompt = build_input(conn, child_id, child_row, stage, t)
    fn = ds_complete or _ds_complete
    t0 = time.monotonic()
    status, error, raw, parsed, resp = "ok", None, None, None, None
    try:
        resp = fn(prompt)
        raw = (resp or {}).get("content", "")
        parsed = _parse_decision(raw, valid_ids)
    except Exception as e:   # fail-open:孩子照旧纯 n-gram 说话
        if _is_timeout(e):
            status = "timeout"
        elif isinstance(e, ValueError):
            status = "bad_json"
        else:
            status = "api_error"
        error = f"{type(e).__name__}: {e}"[:300]
    latency_ms = int((time.monotonic() - t0) * 1000)
    resp = resp or {}
    did = _insert_decision(
        conn, child_id, stage=stage, trigger=trigger, status=status, t=t,
        api_called=1,   # 走到这=真发起了出网(失败也计预算,宁紧勿松)
        digest=digest, parsed=parsed, raw=raw, error=error,
        model=resp.get("model"), latency_ms=latency_ms,
        prompt_tokens=resp.get("prompt_tokens"),
        completion_tokens=resp.get("completion_tokens"))
    out = {"decision_id": did, "status": status}
    if parsed:
        out.update(parsed)
    return out


def latest_anchor_words(conn, child_id: str, t: float) -> list | None:
    """锚词接力读口:**最新一条决策说了算**——最新一条是
    ok 且非不行动且在 TTL 内才给锚词;最新一条是 timeout/bad_json/no_key/
    budget_exceeded/no_action ⇒ None=回纯 n-gram(不许翻旧账捞更早的 ok 锚词)。"""
    row = conn.execute(
        "SELECT status, no_action, anchor_words_json, created_at FROM psyche_decision"
        " WHERE child_id=? ORDER BY id DESC LIMIT 1", (child_id,)).fetchone()
    if row is None or row["status"] != "ok" or row["no_action"]:
        return None
    if t - row["created_at"] > cfg.PSYCHE_DECISION_TTL_S:
        return None
    try:
        ws = json.loads(row["anchor_words_json"] or "[]")
    except ValueError:
        return None
    if not isinstance(ws, list):
        return None
    ws = [str(w).strip() for w in ws if str(w).strip()]
    return ws or None
