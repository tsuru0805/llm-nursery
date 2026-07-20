# -*- coding: utf-8 -*-
"""孩子的学习器:插值式可变阶字符 Markov(0..MAX_CHAR_ORDER 同时计数,采样高阶向低阶退避)。

- 真训练真采样,零 API 成本;"长大"不靠升阶,靠 decoder 的解码参数(config.STAGE_DECODE_V1)。
- 快照=版本头+zlib(json),刻意不用 pickle(数据 blob 不该反序列化出代码);
  sha256 校验,坏快照回退由 child 层处理。
- 词级分词进场时另立 tokenizer_version,不静默混训。
"""
from __future__ import annotations

import hashlib
import json
import random
import zlib
from collections import Counter, defaultdict

from .config import MAX_CHAR_ORDER, SNAPSHOT_FORMAT_VERSION, TOKENIZER_VERSION

LINE_BOUND = "\n"   # 行界 token:句子起止


class VariableOrderMarkov:
    def __init__(self, max_order: int = MAX_CHAR_ORDER):
        self.max_order = max_order
        # counts[order][context_str] -> Counter(next_char)
        self.counts: list[dict[str, Counter]] = [defaultdict(Counter) for _ in range(max_order + 1)]
        self.total_chars = 0

    # ── 训练 ──
    def feed(self, text: str, weight: int = 1) -> int:
        """逐行增量训练,返回入训字符数。weight=整数重复权重(training_weight 取整)。"""
        fed = 0
        w = max(1, int(round(weight)))
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            s = LINE_BOUND + line + LINE_BOUND
            for i in range(1, len(s)):
                for order in range(0, self.max_order + 1):
                    if i - order < 0:
                        continue
                    self.counts[order][s[i - order:i]][s[i]] += w
            fed += len(line)
        self.total_chars += fed
        return fed

    # ── 采样 ──
    def sample_next(self, history: str, max_order: int, backoff_p: float,
                    temperature: float, rng: random.Random,
                    bias: dict[str, float] | None = None) -> str:
        """bias(心理层锚词软偏置):{字符: 权重乘数},只乘在候选分布上——
        模型没见过的字符不会因此凭空出现,护栏检查在 decoder 层照跑。None=原行为。"""
        for order in range(min(max_order, self.max_order, len(history)), -1, -1):
            ctx = history[-order:] if order > 0 else ""
            dist = self.counts[order].get(ctx)
            if not dist:
                continue
            if order > 0 and rng.random() < backoff_p:
                continue  # 低龄"抓不住上下文",概率性降阶
            chars = list(dist.keys())
            weights = [c ** (1.0 / max(temperature, 0.05)) for c in dist.values()]
            if bias:
                weights = [w * bias.get(ch, 1.0) for ch, w in zip(chars, weights)]
            return rng.choices(chars, weights=weights, k=1)[0]
        return LINE_BOUND

    def vocab_by_freq(self) -> list[str]:
        """按 unigram 频次降序的字表(decoder 词汇解锁用)。"""
        uni = self.counts[0].get("", Counter())
        return [ch for ch, _ in uni.most_common() if ch != LINE_BOUND]

    # ── 快照 ──
    def to_blob(self) -> bytes:
        payload = {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "tokenizer_version": TOKENIZER_VERSION,
            "max_order": self.max_order,
            "total_chars": self.total_chars,
            "counts": [
                {ctx: dict(counter) for ctx, counter in level.items()}
                for level in self.counts
            ],
        }
        return zlib.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), level=6)

    @classmethod
    def from_blob(cls, blob: bytes) -> "VariableOrderMarkov":
        payload = json.loads(zlib.decompress(blob).decode("utf-8"))
        if payload.get("format_version") != SNAPSHOT_FORMAT_VERSION:
            raise ValueError(f"快照版本不符: {payload.get('format_version')}")
        m = cls(max_order=payload["max_order"])
        m.total_chars = payload.get("total_chars", 0)
        for order, level in enumerate(payload["counts"]):
            for ctx, counter in level.items():
                m.counts[order][ctx] = Counter(counter)
        return m

    @staticmethod
    def checksum(blob: bytes) -> str:
        return hashlib.sha256(blob).hexdigest()
