# -*- coding: utf-8 -*-
"""nursery · 育儿模拟器:把一个小语言模型当孩子养。

孩子的大脑=插值式可变阶字符 Markov(model)+阶段化解码(decoder)+原文重合护栏(guard);
生命周期与状态机在 child,SQLite 落盘在 db,事件系统在 events,调度在 scheduler,
可选的 LLM 心理层在 psyche。命令行入口:python -m nursery.driver。
"""
