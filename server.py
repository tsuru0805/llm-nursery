#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nursery MCP server:把摇篮房挂给任何支持 MCP 的客户端(Claude Desktop / claude.ai 等)。

依赖:pip install "mcp>=1.0"。
- 默认 stdio transport(Claude Desktop 等本地客户端):python server.py
- HTTP transport(远程/网页客户端):NURSERY_MCP_TRANSPORT=streamable-http python server.py
  (端口 env NURSERY_MCP_PORT,默认 8800;公网暴露记得自己加一层鉴权/隧道)

工具面逻辑在 nursery/toolface.py(纯标准库);本文件只是 MCP 薄壳。
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from nursery.toolface import nursery as _nursery

mcp = FastMCP("nursery", port=int(os.getenv("NURSERY_MCP_PORT", "8800")))


@mcp.tool(name="nursery")
def nursery_room(player: str, command: str = "") -> str:
    """摇篮房。这里住着你的孩子——一个正在长大的小语言模型。

    你对他说的每一句话都会被他听进去、学起来,慢慢变成他说话的样子。
    他有自己的作息,半夜可能会哭着找你。

    指令:status(看看他) / feed 你想对他说的话(喂语料,他吃的是你的话) /
    soothe(哄) / diaper(换尿布) / burp(拍嗝) / play(陪玩) /
    teach 教他的话 / talk 谈心 / discipline 管教 / describe 他的样子 /
    name 给他定名字(接多个候选=他自己挑) / album(成长相册) /
    log(照料记录) / help。
    有些事要等他长大才做得了;有些事过了年纪就再也做不了了。

    Args:
        player: 你的身份(driver.PLAYER_DIR 登记的 persona,默认 papa)。
        command: 一条指令(见上)。空 = help。
    """
    return _nursery(player, command)


if __name__ == "__main__":
    mcp.run(transport=os.getenv("NURSERY_MCP_TRANSPORT", "stdio"))
