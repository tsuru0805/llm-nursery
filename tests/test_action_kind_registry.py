# -*- coding: utf-8 -*-
"""动作 kind 注册表绊线:config.ACTION_KINDS_ALL 是全部 action_log kind 的
单一权威源——任何规则表/模板/机制引入新 kind 而忘登记,这里当场红。"""
from nursery import config as cfg

# psyche 专属键:runaway 只走 apply_rules_locked(状态跃迁),不落 action_log
PSYCHE_ONLY = {"runaway"}


def _rule_table_kinds() -> set:
    kinds = set()
    kinds |= set(cfg.ACTION_EFFECTS)
    kinds |= set(cfg.MAMA_ACTION_EFFECTS)
    kinds |= set(cfg.DARKNESS_BY_ACTION)
    kinds |= set(cfg.BOND_RULES)
    kinds |= set(cfg.PSYCHE_RULES) - PSYCHE_ONLY
    kinds |= set(cfg.SICK_CARE_KINDS)
    for plan in cfg.ASK_RESPONSE_KINDS.values():
        kinds |= set(plan)
    for tmpl in cfg.CHOICE_TEMPLATES.values():
        for opt in (tmpl.get("options") or {}).values():
            kinds.add(opt["kind"])
        if tmpl.get("timeout"):
            kinds.add(tmpl["timeout"]["kind"])
    for tmpl in cfg.CHAIN_TEMPLATES.values():
        for br in tmpl["branches"].values():
            kinds.add(br["kind"])
    return kinds


def test_every_rule_table_kind_is_registered():
    missing = _rule_table_kinds() - cfg.ACTION_KINDS_ALL
    assert not missing, f"新 kind 未登记进 ACTION_KINDS_ALL: {sorted(missing)}"


def test_registry_has_no_orphan_kind():
    """注册表反向:登记的 kind 至少要被某张规则表/模板认识,
    或在已知的引擎直写清单里——防注册表自己长垃圾。"""
    engine_direct = {"left_alone", "overhear", "neglect", "homecoming",
                     "ask_answered", "ask_missed", "choice_missed",
                     "choice_say", "farewell", "stay"}
    orphan = cfg.ACTION_KINDS_ALL - _rule_table_kinds() - engine_direct
    assert not orphan, f"注册表里有没人认识的 kind: {sorted(orphan)}"
