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
          "args": ["<本文件绝对路径>/mcp_server.py"],
          "cwd": "<项目目录>"
        }
      }
    }

注意：
  - 所有工具均为 async + asyncio.to_thread：底层是同步阻塞调用
    （curl_cffi / Playwright），不能在 asyncio 事件循环内直接跑，
    否则 Playwright 会报 "Sync API inside the asyncio loop"。
  - 依赖：mcp（pip install "mcp>=1.9,<2"）+ requirements.txt 内依赖。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cnki_fulltext as cf

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise SystemExit("需要 mcp 包：pip install \"mcp>=1.9,<2\"")

mcp = FastMCP("gxlib-cnki")

# 会话对象（懒初始化：首次调用工具时再创建，避免 import 阶段读文件失败）
_api = None


def api():
    global _api
    if _api is None:
        _api = cf.GxlibCNKI()
    return _api


@mcp.tool()
async def session_status() -> str:
    """查看当前会话是否有效（信任态是否存在/过期）。返回 JSON。"""
    return await asyncio.to_thread(_session_status_sync)


def _session_status_sync() -> str:
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
async def setup_trust(headed: bool = False) -> str:
    """用 Playwright 建立信任态（登录平台→进知网→检索一次→导出含 HttpOnly 的 cookie）。
    无头默认；若触发滑块，用 headed=True 有头模式人工通过一次。"""
    return await asyncio.to_thread(_setup_trust_sync, headed)


def _setup_trust_sync(headed: bool) -> str:
    a = api()
    # browser_trust 使用 Playwright 同步 API，必须卸载到工作线程
    a.browser_trust(headless=not headed)
    try:
        a.ensure_cnki_hosts()
        hosts = a.hosts
    except Exception:
        hosts = {}
    return json.dumps({"ok": True, "hosts": hosts, "cookie_count": len(a.cookies)},
                      ensure_ascii=False, indent=1)


@mcp.tool()
async def search_papers(keyword: str, limit: int = 20,
                        source_categories: str = "",
                        year_from: int = 0, year_to: int = 0,
                        sort_by: str = "被引", sort_order: str = "desc",
                        search_type: str = "主题") -> str:
    """检索知网文献（POST /kns8s/brief/grid）。支持精准筛选。
    参数：
      keyword           检索词
      search_type       检索字段：主题(默认)/篇关摘/关键词/篇名/全文/作者/第一作者/
                        通讯作者/作者单位/基金/摘要/参考文献/分类号/文献来源
      limit             返回条数（默认 20）
      source_categories 来源类别，逗号分隔：北大核心/CSSCI/CSCD/AMI/EI/WJCI
      year_from/year_to 发表年份范围（如 2020 / 2024；0 表示不限）
      sort_by           排序：被引(默认)/相关度/下载/综合/发表时间
      sort_order        desc(默认)/asc
    返回论文列表 JSON：[{title, url, authors, source, date, db, cited, download}]"""
    return await asyncio.to_thread(
        _search_papers_sync, keyword, limit, source_categories,
        year_from, year_to, sort_by, sort_order, search_type)


def _search_papers_sync(keyword, limit, source_categories, year_from, year_to, sort_by, sort_order, search_type) -> str:
    papers = api().search(
        keyword, limit=limit,
        source_categories=source_categories or None,
        year_from=year_from or None, year_to=year_to or None,
        sort_by=sort_by, sort_order=sort_order,
        search_type=search_type or None)
    return json.dumps(papers, ensure_ascii=False, indent=1)


@mcp.tool()
async def export_papers(keyword: str, fmt: str = "json", limit: int = 50,
                        search_type: str = "主题", source_categories: str = "",
                        year_from: int = 0, year_to: int = 0, sort_by: str = "被引") -> str:
    """检索并批量导出（可导入 Zotero/EndNote）。
    fmt: json/csv/ris/bibtex。其余参数同 search_papers。"""
    return await asyncio.to_thread(
        _export_sync, keyword, fmt, limit, search_type, source_categories,
        year_from, year_to, sort_by)


def _export_sync(keyword, fmt, limit, search_type, source_categories, year_from, year_to, sort_by) -> str:
    papers = api().search(keyword, limit=limit, search_type=search_type or None,
                          source_categories=source_categories or None,
                          year_from=year_from or None, year_to=year_to or None,
                          sort_by=sort_by)
    return cf.GxlibCNKI.export_papers(papers, fmt)


@mcp.tool()
async def find_best_match(title: str, limit: int = 30) -> str:
    """按题名检索并字符匹配，定位/验证某篇论文是否在库。
    返回按匹配度降序的候选 [{title, url, source, date, ratio}]。"""
    return await asyncio.to_thread(_match_sync, title, limit)


def _match_sync(title: str, limit: int) -> str:
    return json.dumps(api().find_best_match(title, limit=limit), ensure_ascii=False, indent=1)


@mcp.tool()
async def format_citation(title: str, authors: str, source: str, year: int,
                          volume: str = "", issue: str = "", pages: str = "",
                          doi: str = "", style: str = "gbt7714") -> str:
    """通用引文格式化（独立于 CNKI 原始引文）。
    style: gbt7714(GB/T 7714-2015)/apa/mla/chicago/vancouver。作者用 ; 或 , 分隔。"""
    return await asyncio.to_thread(
        _cite_fmt_sync, title, authors, source, year, volume, issue, pages, doi, style)


def _cite_fmt_sync(title, authors, source, year, volume, issue, pages, doi, style) -> str:
    return cf.GxlibCNKI.format_citation(title, authors, source, year,
                                        volume, issue, pages, doi, style)


@mcp.tool()
async def download_fulltext(article_url: str) -> str:
    """下载文章全文 PDF 到 ./fulltext/。返回文件路径。"""
    return await asyncio.to_thread(_download_sync, article_url)


def _download_sync(article_url: str) -> str:
    path, title = api().download(article_url)
    return json.dumps({"title": title, "pdf_path": path}, ensure_ascii=False, indent=1)


@mcp.tool()
async def get_citation(article_url: str) -> str:
    """获取 CNKI 原始引文（GB/T 7714-2025 / 知网研学 / EndNote）及结构化元数据。"""
    return await asyncio.to_thread(_cite_sync, article_url)


def _cite_sync(article_url: str) -> str:
    return json.dumps(api().cite(article_url), ensure_ascii=False, indent=1)


@mcp.tool()
async def get_metadata(article_url: str) -> str:
    """提取详情页元数据：标题/作者/期刊/摘要/关键词/基金/分类号/文章目录等。"""
    return await asyncio.to_thread(_meta_sync, article_url)


def _meta_sync(article_url: str) -> str:
    return json.dumps(api().meta(article_url), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run()
