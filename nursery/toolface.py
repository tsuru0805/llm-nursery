# -*- coding: utf-8 -*-
"""工具面(纯逻辑,零 mcp 依赖):白名单校验 + spawn driver 子进程。

server.py(MCP 壳)只是把这里的 nursery() 注册成工具;这样测试与其他接入层
(bot/web 面板)都能直接复用同一套校验,不用装 mcp 包。

每次调用 spawn 全新子进程(python -m nursery.driver):模块全局零共享,
flock 防并发丢档;本面只放行玩法指令,--tick/--init-birth 等运维入口绝不暴露。
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import driver
from . import texts

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAX_CMD = 700   # feed 正文 cap 600 + 指令头富余
_TIMEOUT = 20    # 子进程超时(秒;含模型装载)

# 公开命令白名单(运维入口不放行;妈妈通道走接入层专口,不在此面)。
# ⚠校验按首 token 判定,判定前 strip 引号防绕过。
_PUBLIC_CMDS = frozenset({
    "help", "status", "feed", "soothe", "diaper", "burp", "play", "teach",
    "talk", "discipline", "describe", "name", "album", "log",
})


def _driver_env() -> dict:
    env = dict(os.environ)
    saves = os.getenv("NURSERY_SAVES_DIR")
    if saves:
        env["NURSERY_SAVES_DIR"] = saves
    return env


def nursery(player: str, command: str = "") -> str:
    """摇篮房一条指令 → 给人看的文本。校验白名单后 spawn driver 子进程执行。"""
    if player not in driver.PLAYER_DIR:
        return texts.TOOLFACE_UNKNOWN_PLAYER.format(player=repr(player))
    command = (command or "").strip()
    if len(command) > _MAX_CMD:
        return texts.TOOLFACE_TOO_LONG.format(max_len=_MAX_CMD)
    # 首 token 按第一个空白切(partition 保正文原文,不经 shlex——
    # feed 带引号时按解码长度切原串会丢字);判定前 strip 引号防绕过
    head_raw, _, body = command.partition(" ")
    head = head_raw.strip("'\"").lower() if head_raw else "help"
    if not head:
        head = "help"
    if head not in _PUBLIC_CMDS:
        return texts.TOOLFACE_UNKNOWN_CMD.format(cmd=head)
    body = body.strip()
    if head in ("feed", "teach", "talk", "describe", "name"):
        argv = [head] + ([body] if body else [])
    else:
        argv = [head]  # 其余命令不带参数,尾巴忽略
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "nursery.driver", player, *argv],
            capture_output=True, text=True, timeout=_TIMEOUT,
            cwd=_ROOT, env=_driver_env())
    except subprocess.TimeoutExpired:
        return texts.TOOLFACE_TIMEOUT
    if proc.returncode != 0:
        return texts.TOOLFACE_ERROR
    return proc.stdout.strip() or texts.TOOLFACE_SILENT
