---
name: gxlib-cnki
description: 广西图书馆·知网全文工具。通过广西壮族自治区图书馆数字资源平台（知网包库代理）批量获取知网文献的全文 PDF、摘要、元数据与 CNKI 原始引文（GB/T 7714-2025）。用于文献综述的全文阅读、参考文献整理。触发词：知网全文、下载知网文献、CNKI 引文、文献综述取正文。
---

# gxlib-cnki · 知网全文获取

通过广西图书馆知网包库代理，自动完成 **检索 → 下载全文 PDF → 引文/元数据**。

## 前置

- 项目目录：`D:\Code\gxlib-cnki`（git 仓库；全新机器安装见其 `INSTALL.md`）
- 依赖：`pip install -r requirements.txt && python -m playwright install chromium`
- 配置：`config.json`（gitignore）含读者证号密码，参考 `config.example.json`

## 架构（一句话）

**Playwright 建信任态（每会话一次）→ curl_cffi 纯 HTTP 调数据接口**，绕开知网滑块。

- `POST /kns8s/brief/grid` = 检索数据接口（需信任态 cookie + Chrome 指纹）
- 信任态 = 浏览器登录平台→进知网→检索一次后导出的 cookie（含 HttpOnly），存 `gxlib_session.json`

## 命令

```bash
cd D:\Code\gxlib-cnki

python cnki_fulltext.py browser-trust [--headed]   # 建信任态（出滑块加 --headed 人工通过）
python cnki_fulltext.py search "关键词" --limit 20 # 检索
python cnki_fulltext.py download <文章URL>         # 下载全文 PDF → ./fulltext/
python cnki_fulltext.py cite <文章URL>             # CNKI 引文（GB/T 7714-2025/研学/EndNote）
python cnki_fulltext.py meta <文章URL>             # 摘要/关键词/基金/目录等元数据
```

## 文献综述工作流

1. 确认信任态：`python cnki_fulltext.py session-info`（无效先 `browser-trust`）。
2. 按检索式批量检索，收集文章 URL。
3. 对每篇：`meta` 拿摘要+目录（判断相关性/研究框架）→ 相关则 `download` 拿全文 → `cite` 拿 GB/T 7714 引文。
4. 全文 PDF 用 pymupdf 提取正文（`pdftotext` 对该刊 PDF 会乱码）。

## MCP 接入（可选）

`mcp_server.py` 暴露 `search_papers / download_fulltext / get_citation / get_metadata / setup_trust / session_status` 工具，任何 MCP 客户端可配置：

```json
{ "mcpServers": { "gxlib-cnki": { "command": "python",
    "args": ["D:/Code/gxlib-cnki/mcp_server.py"], "cwd": "D:/Code/gxlib-cnki" } } }
```

## 注意事项

- 平台单会话：login/接管会踢旧会话，信任态有效期内勿反复登录。
- 信任态有效期短，过期重新 `browser-trust`。
- 文献 PDF 为版权内容，仅限本人论文写作，勿传播；不入 git。
