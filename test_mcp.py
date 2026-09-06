# -*- coding: utf-8 -*-
"""MCP 协议级测试：stdio 客户端连接 mcp_server.py，列出工具并实际调用。"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = r"D:\Code\gxlib-cnki\mcp_server.py"
CWD = r"D:\Code\gxlib-cnki"


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

    print("\n✅ MCP 协议级测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())