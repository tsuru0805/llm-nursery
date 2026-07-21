# -*- coding: utf-8 -*-
"""双照护人关系状态(见 AGENTS.md)。

孩子对爸爸和对妈妈的感情**分开长**:谁喂得多、谁半夜来、谁凶他,他心里分得清。
四维(中文名=设计定案):亲近 attachment/安心 trust/踏实 predictability/
委屈 resentment。每 caregiver 各一套 0-100。

纪律:
- apply_locked **必须在 child._apply_action_locked 的事务内调**(与动作账同事务
  同幂等,重放早退不双记);流水全进 caregiver_bond_log。
- 过去两天的账用既往 action_log **估底**(每笔按规则表 ×BOND_INIT_FACTOR 半额折算),
  meta 落 bond_initialized_from_history=true + bond_confidence=low——
  是估算,不装完全知道(原则)。
- 夜哭忽视(neglect,actor=system)账记爸爸(设计定案)。
- 委屈 v1 无自愈:只有 homecoming/后续修复类动作能降(观察项:堆积过快再调)。
"""
from __future__ import annotations

import time

from . import config as cfg

_META_INIT = "bond_initialized_from_history"
_TREND_CN = {"rising": "在上升", "falling": "在下降", "flat": "平稳"}


def _caregiver_of(actor: str, kind: str) -> str | None:
    if kind == "neglect":
        return "papa"   # 夜哭忽视账只认爸爸(她拍)
    return cfg.BOND_ACTOR_TO_CG.get(actor)


def _ensure_rows_locked(conn, child_id: str, t: float) -> None:
    for cg in cfg.BOND_CAREGIVERS:
        for dim in cfg.BOND_DIMS:
            conn.execute(
                "INSERT OR IGNORE INTO caregiver_bond(child_id, caregiver, dim,"
                " value, updated_at) VALUES(?,?,?,?,?)",
                (child_id, cg, dim, cfg.BOND_BASELINE[dim], t))


def _bump_locked(conn, child_id: str, caregiver: str, deltas: dict, *,
                 reason: str, source_key: str | None, t: float) -> dict:
    """流水记**实际生效增量**(0-100 夹取后 after-cur),饱和顶格=零增量不落行
    ——趋势永远反映真实变化,不虚报(评审。"""
    applied: dict = {}
    for dim, delta in deltas.items():
        if dim not in cfg.BOND_DIMS or not delta:
            continue
        cur = conn.execute(
            "SELECT value FROM caregiver_bond WHERE child_id=? AND caregiver=?"
            " AND dim=?", (child_id, caregiver, dim)).fetchone()["value"]
        after = max(0.0, min(100.0, cur + delta))
        real = after - cur
        if abs(real) < 1e-9:
            continue
        conn.execute(
            "UPDATE caregiver_bond SET value=?, updated_at=? WHERE child_id=?"
            " AND caregiver=? AND dim=?", (after, t, child_id, caregiver, dim))
        conn.execute(
            "INSERT INTO caregiver_bond_log(child_id, caregiver, dim, delta,"
            " value_after, reason, source_key, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (child_id, caregiver, dim, real, after, reason, source_key, t))
        applied[dim] = real
    return applied


def ensure_initialized_locked(conn, child_id: str, t: float) -> bool:
    """首次触达时用**既往全部** action_log 估底(每笔半额折算;对孩子=出生起的
    两天账,对任何孩子=上线前的全部履历——与 「根据现有统计估算基线」
    同义)。已初始化=False 直接回。必须在调用方事务内。"""
    row = conn.execute(
        "SELECT 1 FROM parenting_meta WHERE child_id=? AND key=?",
        (child_id, _META_INIT)).fetchone()
    if row is not None:
        return False
    _ensure_rows_locked(conn, child_id, t)
    est: dict = {}   # (caregiver, dim) -> 累计
    for r in conn.execute(
            "SELECT actor, kind FROM action_log WHERE child_id=?"
            " AND effective_at<=?", (child_id, t)):
        cg = _caregiver_of(r["actor"], r["kind"])
        deltas = cfg.BOND_RULES.get(r["kind"])
        if not cg or not deltas:
            continue
        for dim, dv in deltas.items():
            est[(cg, dim)] = est.get((cg, dim), 0.0) + dv * cfg.BOND_INIT_FACTOR
    for (cg, dim), dv in est.items():
        _bump_locked(conn, child_id, cg, {dim: dv},
                     reason="init_from_history", source_key=None, t=t)
    for key, val in ((_META_INIT, "true"), ("bond_confidence", "low")):
        conn.execute(
            "INSERT INTO parenting_meta(child_id, key, value, updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(child_id, key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (child_id, key, val, t))
    return True


def apply_locked(conn, child_id: str, actor: str, kind: str, t: float, *,
                 source_key: str | None = None, scale: float = 1.0,
                 night_date: str | None = None, calm: bool = False) -> dict:
    """动作 → 关系账(child._apply_action_locked 内调,同事务同幂等)。
    scale=当日递减系数同乘;night_date=开着的夜哭窗日期——响应类动作给
    爸爸的夜起加成,**每晚只记一次**(dedupe=bondnight:{date},与 psyche 夜哭
    响应同口径,评审;calm=平静时安抚的黏人账(联动)。
    返回 {dim: 实际生效增量} 供动作账留痕。"""
    caregiver = _caregiver_of(actor, kind)
    if caregiver is None:
        return {}
    ensure_initialized_locked(conn, child_id, t)
    deltas = dict(cfg.BOND_RULES.get(kind) or {})
    if calm:
        for dim, dv in cfg.BOND_CALM_SOOTHE.items():
            deltas[dim] = deltas.get(dim, 0.0) + dv
    if scale != 1.0:
        deltas = {d: v * scale for d, v in deltas.items()}
    applied: dict = {}
    if deltas:
        applied = _bump_locked(conn, child_id, caregiver, deltas,
                               reason=kind, source_key=source_key, t=t)
    if night_date and caregiver == "papa" and \
            kind in cfg.PSYCHE_NIGHT_RESPONSE_KINDS:
        # 每夜一次的占位=parenting_meta 独立标记(与增量流水解耦:饱和零增量
        # 不落流水行,标记也必须占住,否则同夜可重复领加成——评审
        sk = f"bondnight:{night_date}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO parenting_meta(child_id, key, value, updated_at)"
            " VALUES(?,?,'1',?)", (child_id, sk, t))
        if cur.rowcount > 0:   # 首次占位成功才发加成
            for dim, dv in _bump_locked(
                    conn, child_id, "papa", dict(cfg.BOND_NIGHT_RESPONSE),
                    reason="night_response", source_key=sk, t=t).items():
                applied[dim] = applied.get(dim, 0.0) + dv
    return applied


def read_bond(conn, child_id: str) -> dict:
    """{caregiver: {dim: value}}(无行=基线)。围观/调试读口。"""
    out = {cg: dict(cfg.BOND_BASELINE) for cg in cfg.BOND_CAREGIVERS}
    for r in conn.execute(
            "SELECT caregiver, dim, value FROM caregiver_bond WHERE child_id=?",
            (child_id,)):
        if r["caregiver"] in out and r["dim"] in cfg.BOND_BASELINE:
            out[r["caregiver"]][r["dim"]] = r["value"]
    return out


def bond_trends(conn, child_id: str, t: float) -> dict:
    """近 BOND_TREND_WINDOW_H 小时流水净变化 → 方向词(DS 只拿方向不拿裸数值,
    与三轴同口径;init_from_history 估底行不算趋势)。"""
    t0 = t - cfg.BOND_TREND_WINDOW_H * 3600
    net: dict = {cg: {d: 0.0 for d in cfg.BOND_DIMS} for cg in cfg.BOND_CAREGIVERS}
    for r in conn.execute(
            "SELECT caregiver, dim, SUM(delta) AS s FROM caregiver_bond_log"
            " WHERE child_id=? AND created_at>=? AND reason!='init_from_history'"
            " GROUP BY caregiver, dim", (child_id, t0)):
        if r["caregiver"] in net and r["dim"] in cfg.BOND_BASELINE:
            net[r["caregiver"]][r["dim"]] = r["s"] or 0.0
    out: dict = {}
    for cg, dims in net.items():
        out[cg] = {}
        for d, v in dims.items():
            if abs(v) < cfg.BOND_TREND_FLAT_EPS:
                out[cg][d] = "flat"
            else:
                out[cg][d] = "rising" if v > 0 else "falling"
    return out


def trend_lines_cn(conn, child_id: str, t: float) -> list[str]:
    """给 DS 输入摘要的中文行(只报非平稳项;全平=一行「都平稳」)。"""
    trends = bond_trends(conn, child_id, t)
    cg_cn = {"papa": "对爸爸", "mama": "对妈妈"}
    lines = []
    for cg in cfg.BOND_CAREGIVERS:
        moving = [f"{cfg.BOND_CN[d]}{_TREND_CN[w]}"
                  for d, w in trends[cg].items() if w != "flat"]
        lines.append(f"  - {cg_cn[cg]}:{'、'.join(moving) if moving else '都平稳'}")
    return lines
