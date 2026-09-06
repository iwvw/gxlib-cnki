# -*- coding: utf-8 -*-
"""MCP 协议级测试：stdio 客户端连接 mcp_server.py，列出工具并实际调用。"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
CWD = HERE


async def main():
    params = StdioServerParameters(command=sys.executable, args=[SERVER], cwd=CWD)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("== 可用工具 ==")
            for n in names:
                print("  -", n)

            print("\n== session_status ==")
            r = await session.call_tool("session_status", {})
            st = json.loads(r.content[0].text)
            print("有效:", st["session_valid"], "| 信任态:", st["has_trust"])

            if not st["session_valid"]:
                print("\n== setup_trust（重建信任态）==")
                r = await session.call_tool("setup_trust", {"headed": False})
                print(r.content[0].text[:220])
                r = await session.call_tool("session_status", {})
                st = json.loads(r.content[0].text)
                print("重建后有效:", st["session_valid"])

            print("\n== search_papers ==")
            r = await session.call_tool("search_papers", {"keyword": "直播电商 消费者购买意愿", "limit": 3})
            papers = json.loads(r.content[0].text)
            print("返回", len(papers), "篇")
            for p in papers:
                print("  -", p["title"][:45], "|", p.get("date", ""))

            url = papers[0]["url"]

            print("\n== get_citation ==")
            r = await session.call_tool("get_citation", {"article_url": url})
            c = json.loads(r.content[0].text)
            print("GB/T:", c.get("GB/T 7714-2025 格式引文", ["?"])[0][:100])

            print("\n== get_metadata ==")
            r = await session.call_tool("get_metadata", {"article_url": url})
            meta = json.loads(r.content[0].text)
            print("题名:", meta.get("title", "")[:40])
            print("关键词:", meta.get("keywords", "")[:60])

            print("\n== download_fulltext ==")
            r = await session.call_tool("download_fulltext", {"article_url": url})
            d = json.loads(r.content[0].text)
            print("PDF:", d.get("pdf_path", "")[-60:])

            print("\n== 字段检索（篇名）+ 作者拆分 ==")
            r = await session.call_tool("search_papers", {"keyword": "电商直播中智能属性营销", "search_type": "篇名", "limit": 1})
            ps = json.loads(r.content[0].text)
            print("作者:", ps[0].get("authors"), "| 来源:", ps[0].get("source"))

            print("\n== find_best_match ==")
            r = await session.call_tool("find_best_match", {"title": papers[0]["title"][:30]})
            ms = json.loads(r.content[0].text)
            print("top ratio:", ms[0]["ratio"], "|", ms[0]["title"][:30])

            print("\n== export_papers (ris) ==")
            r = await session.call_tool("export_papers", {"keyword": "直播电商", "fmt": "ris", "limit": 2})
            print(r.content[0].text[:70].replace("\n", " | "))

            print("\n== format_citation (apa/mla) ==")
            r = await session.call_tool("format_citation", {"title": "测试", "authors": "孟陆;刘凤军", "source": "南开管理评论",
                                                            "year": 2020, "volume": "23", "issue": "1", "pages": "131-143", "style": "apa"})
            print("apa:", r.content[0].text[:60])

    print("\n✅ MCP 协议级测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())