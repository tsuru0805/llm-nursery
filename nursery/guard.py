# -*- coding: utf-8 -*-
"""输出护栏(反复读检测)+ PII 遮盖。

反复读护栏(双层):
1. 最长公共连续子串 ≥ overlap_limit → 拒(6-gram 倒排预筛+候选行精确 LCS;短源行单独整句检)。
2. 4-gram shingle 重合率:输出 ≥ SHINGLE_RATIO_MIN_LEN 字时,其 4-gram 命中源比例 ≥
   SHINGLE_RATIO_LIMIT → 拒(兜跨行高密拼接的"伪原创")。
跨来源低密拼接是特性不拦——目标手感「明显学歪了,但找不到原句」。

PII:凭证/联系方式类遮盖(类型化占位符);日期豁免;NSFW 词汇不在此列。
地址类 regex 误伤率高暂不做(已知欠账)。
"""
from __future__ import annotations

import re
from collections import defaultdict

SHINGLE_K = 6            # LCS 预筛窗
RATIO_K = 4              # 重合率层 shingle 窗
SHINGLE_RATIO_LIMIT = 0.85
SHINGLE_RATIO_MIN_LEN = 10   # 短输出 4-gram 太少,比率噪大,不启用比率层

# ── PII ──
_DATE_RE = re.compile(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")
_PII_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("email", "□邮箱□", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("url", "□链接□", re.compile(r"https?://\S+")),
    ("token", "□密钥□", re.compile(
        r"(?<![A-Za-z0-9_\-])(?:sk|pk|ghp|gho|xox[bap])[-_][A-Za-z0-9_\-]{8,}")),
    # 电话:10-15 位连续数字,或 2-4/3-4/3-4 分段;日期先豁免
    ("phone", "□电话□", re.compile(
        r"(?<!\d)(?:\+?\d{10,15}|\d{2,4}[- ]\d{3,4}[- ]\d{3,4})(?!\d)")),
    # 长数字:≥9 位(8 位日期形如 20260717 放过)
    ("longnum", "□序列□", re.compile(r"(?<!\d)\d{9,}(?!\d)")),
]


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """遮盖凭证/联系方式,返回(清洗后文本, 命中类别)。日期不误伤。"""
    # 日期先换哨兵护住,扫完还原
    dates: list[str] = []

    def _stash(m: re.Match) -> str:
        dates.append(m.group(0))
        return f"\x00{len(dates) - 1}\x00"

    out = _DATE_RE.sub(_stash, text)
    flags: list[str] = []
    for name, placeholder, pat in _PII_PATTERNS:
        if pat.search(out):
            flags.append(name)
            out = pat.sub(placeholder, out)
    for i, d in enumerate(dates):
        out = out.replace(f"\x00{i}\x00", d)
    return out, flags


class OverlapGuard:
    """语料原文重合检测。add_source 喂入训练原文行,check 查输出。"""

    def __init__(self, k: int = SHINGLE_K):
        self.k = k
        self.lines: list[str] = []
        self.short_lines: set[str] = set()                    # <k 的源行:整句照抄检
        self._index: dict[str, set[int]] = defaultdict(set)   # k-gram -> 行号
        self._ratio_shingles: set[str] = set()                # 4-gram 全源集合

    def add_source(self, text: str) -> None:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if len(line) < self.k:
                self.short_lines.add(line)
            else:
                idx = len(self.lines)
                self.lines.append(line)
                for i in range(len(line) - self.k + 1):
                    self._index[line[i:i + self.k]].add(idx)
            for i in range(max(0, len(line) - RATIO_K + 1)):
                self._ratio_shingles.add(line[i:i + RATIO_K])

    def max_overlap(self, text: str) -> int:
        """输出与任一语料行的最长公共连续子串长度(含短源行整句出现)。"""
        best = 0
        for short in self.short_lines:
            if short in text:
                best = max(best, len(short))
        n = len(text)
        if n < self.k:
            for line in self.lines:
                if text and text in line:
                    best = max(best, n)
            return best
        candidates: set[int] = set()
        for i in range(n - self.k + 1):
            candidates |= self._index.get(text[i:i + self.k], set())
        for idx in candidates:
            line = self.lines[idx]
            for i in range(n):
                if best >= n - i:
                    break
                L = best + 1
                while i + L <= n and text[i:i + L] in line:
                    best = L
                    L += 1
        return best

    def shingle_ratio(self, text: str) -> float:
        grams = [text[i:i + RATIO_K] for i in range(len(text) - RATIO_K + 1)]
        if not grams:
            return 0.0
        hit = sum(1 for g in grams if g in self._ratio_shingles)
        return hit / len(grams)

    def check(self, text: str, overlap_limit: int) -> tuple[bool, int]:
        """(是否放行, 实测最长重合)。双层:LCS 阈值 + 4-gram 重合率。"""
        ov = self.max_overlap(text)
        if ov >= overlap_limit:
            return False, ov
        if len(text) >= SHINGLE_RATIO_MIN_LEN and \
                self.shingle_ratio(text) >= SHINGLE_RATIO_LIMIT:
            return False, ov
        return True, ov
