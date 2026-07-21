# -*- coding: utf-8 -*-
"""成长控制器:阶段化解码——同一个大脑,长大的是"说话的权限"。

speak() 是唯一出口:采样 → 词汇解锁过滤 → 叠词 → 护栏 → 近期去重,全过才算一句话。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .config import (DIGEST_SPEAK_LEN_CUT, DIGEST_SPEAK_REDUP_BOOST,
                     DIGEST_SPEAK_TEMP_BOOST, PSYCHE_ANCHOR_BOOST, STAGE_DECODE_V1)
from .guard import OverlapGuard
from .model import LINE_BOUND, VariableOrderMarkov
from .texts import FALLBACK_BABBLE   # 护栏全拒兜底(文案层;此处 re-export)

SENT_END = "。!?~！？"
MAX_TRIES = 30


@dataclass
class SpeakResult:
    text: str
    retries: int
    max_overlap: int
    accepted: bool
    stage: str
    params: dict
    refused: bool = False   # 态度层:听懂了,但他就是不(Creatures 式不服从)


def speak(model: VariableOrderMarkov, guard: OverlapGuard, stage: str,
          rng: random.Random, recent_texts: list[str] | None = None,
          seed: str = "", refuse_p: float = 0.0,
          anchor_words: list[str] | None = None,
          overload: float = 0.0, chunk: str | None = None) -> SpeakResult:
    """seed=锚词起头继续说(语出惊人用);refuse_p=已读不回概率(teen 黑暗值驱动);
    anchor_words=psyche 决策锚词:锚词字符采样权重 ×PSYCHE_ANCHOR_BOOST 的
    **软偏置**——只影响采样偏好,护栏三层原封不动(过不了 guard 照样拒);
    None=零偏置照旧。锚词进 params 留痕(utterance.generation_params_json 可审计)。
    overload=消化过载比例 0-1:话说不利索——温度升/句长缩/叠词回升,
    与锚词同款软通道,护栏与词汇解锁照跑;0=原行为。同样进 params 留痕。
    chunk=家庭词块整词起头:等价 seed(显式 seed 优先,语出惊人不受扰),
    **词块字符必须全在词汇解锁集内**,否则本次不整词(seed 直进输出,不设闸
    =绕过 vocab_ratio);进 params 留痕。"""
    p = STAGE_DECODE_V1[stage]
    if overload and overload > 0:
        ov = min(1.0, max(0.0, float(overload)))
        p = dict(p,
                 temperature=p["temperature"] + DIGEST_SPEAK_TEMP_BOOST * ov,
                 max_len=max(p["min_len"], int(p["max_len"] * (1 - DIGEST_SPEAK_LEN_CUT * ov))),
                 reduplicate_p=min(0.9, p["reduplicate_p"] + DIGEST_SPEAK_REDUP_BOOST * ov),
                 overload=round(ov, 2))
    bias = None
    if anchor_words:
        chars = {c for w in anchor_words for c in str(w) if not c.isspace()}
        if chars:
            bias = {c: PSYCHE_ANCHOR_BOOST for c in chars}
            p = dict(p, anchors=list(anchor_words))
    if refuse_p > 0 and rng.random() < refuse_p:
        return SpeakResult(text="", retries=0, max_overlap=0, accepted=False,
                           stage=stage, params=p, refused=True)
    recent = set(recent_texts or [])

    ranked = model.vocab_by_freq()
    if not ranked:  # 空模型(没喂过任何语料)有界返回,不死循环
        return SpeakResult(text=FALLBACK_BABBLE, retries=0, max_overlap=0,
                           accepted=False, stage=stage, params=p)
    allowed = set(ranked[: max(8, int(len(ranked) * p["vocab_ratio"]))])
    # 词块起头:字符全在解锁集才生效(不绕 vocab 闸);显式 seed 优先
    if chunk and not seed and all(c in allowed for c in chunk):
        seed = chunk
        p = dict(p, chunk=chunk)

    retries = 0
    last_overlap = 0
    for _ in range(MAX_TRIES):
        out: list[str] = list(seed)
        history = LINE_BOUND + seed
        steps = 0
        max_steps = p["max_len"] * 8  # 停句/词汇锁全程拒绝时的硬上限
        while len(out) < p["max_len"] and steps < max_steps:
            steps += 1
            ch = model.sample_next(history, p["max_order"], p["backoff_p"],
                                   p["temperature"], rng, bias=bias)
            if ch == LINE_BOUND or ch in SENT_END:
                if len(out) >= p["min_len"]:
                    break
                continue  # 太短不许停
            if ch not in allowed:
                continue  # 词汇未解锁
            out.append(ch)
            if rng.random() < p["reduplicate_p"]:
                out.append(ch)  # 婴幼儿叠词
            history += ch
        text = "".join(out).strip()
        if not text or text in recent:
            retries += 1
            continue
        ok, overlap = guard.check(text, p["overlap_limit"])
        last_overlap = overlap
        if not ok:
            retries += 1
            continue
        return SpeakResult(text=text, retries=retries, max_overlap=overlap,
                           accepted=True, stage=stage, params=p)
    return SpeakResult(text=FALLBACK_BABBLE, retries=retries, max_overlap=last_overlap,
                       accepted=False, stage=stage, params=p)
