#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gxlib-cnki MCP 服务器
=====================
把广西图书馆·知网全文工具暴露为 MCP 工具，任何支持 MCP 的 Agent
（Claude Desktop / Cursor / 各类 MCP 客户端）都可直接调用。

启动（stdio 传输）：
    python mcp_server.py

客户端配置示例（claude_desktop_config.json 或等效 MCP 配置）：
    {
      "mcpServers": {
        "gxlib-cnki": {
          "command": "python",
          "args": ["D:/Code/gxlib-cnki/mcp_server.py"],
          "cwd": "D:/Code/gxlib-cnki"
        }
      }
    }

依赖：mcp（pip install mcp）+ requirements.txt 内依赖。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cnki_fulltext as cf

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise SystemExit("需要 mcp 包：pip install mcp")

mcp = FastMCP("gxlib-cnki")

# 会话对象（懒初始化：首次调用工具时再创建，避免 import 阶段读文件失败）
_api = None


def api():
    global _api
    if _api is None:
        _api = cf.GxlibCNKI()
    return _api


@mcp.tool()
def session_status() -> str:
    """查看当前会话是否有效（信任态是否存在/过期）。返回 JSON。"""
    a = api()
    valid = a._session_valid()
    return json.dumps({
        "session_valid": valid,
        "cookie_count": len(a.cookies),
        "has_trust": "LID" in a.cookies or "SID_search" in a.cookies,
        "search_host": a.host_of("search"),
        "hint": "有效即可直接检索；无效请先调用 setup_trust"
    }, ensure_ascii=False, indent=1)


@mcp.tool()
def setup_trust(headed: bool = False) -> str:
    """用 Playwright 建立信任态（登录平台→进知网→检索一次→导出含 HttpOnly 的 cookie）。
    无头默认；若触发滑块，用 headed=True 有头模式人工通过一次。"""
    a = api()
    a.browser_trust(headless=not headed)
    try:
        a.ensure_cnki_hosts()
        hosts = a.hosts
    except Exception:
        hosts = {}
    return json.dumps({"ok": True, "hosts": hosts, "cookie_count": len(a.cookies)},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def search_papers(keyword: str, limit: int = 20) -> str:
    """检索知网文献（POST /kns8s/brief/grid）。返回论文列表 JSON：
    [{title, url, authors, source, date, cited}]。需信任态。"""
    papers = api().search(keyword, limit=limit)
    return json.dumps(papers, ensure_ascii=False, indent=1)


@mcp.tool()
def download_fulltext(article_url: str) -> str:
    """下载文章全文 PDF 到 ./fulltext/。返回文件路径。"""
    path, title = api().download(article_url)
    return json.dumps({"title": title, "pdf_path": path}, ensure_ascii=False, indent=1)


@mcp.tool()
def get_citation(article_url: str) -> str:
    """获取 CNKI 原始引文（GB/T 7714-2025 / 知网研学 / EndNote）及结构化元数据。"""
    cites = api().cite(article_url)
    return json.dumps(cites, ensure_ascii=False, indent=1)


@mcp.tool()
def get_metadata(article_url: str) -> str:
    """提取详情页元数据：标题/作者/期刊/摘要/关键词/基金/分类号/文章目录等。"""
    meta = api().meta(article_url)
    return json.dumps(meta, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run()
