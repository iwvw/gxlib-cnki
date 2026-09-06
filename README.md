# gxlib-cnki · 广西图书馆知网全文工具

通过广西壮族自治区图书馆数字资源平台（知网包库代理），批量获取知网文献的**全文 PDF / 摘要 / 元数据 / CNKI 原始引文**。

## 原理（2026-09 逆向验证）

知网的数据接口 `POST /kns8s/brief/grid` 本身是纯 HTTP 的，但要求会话带「信任态」——即经过真实浏览器执行 JS 后种下的一组 cookie（`LID` / `KNS2COOKIE` / `SID_search` 等）。因此：

- **Playwright**（无头 Chromium）负责每会话一次建立信任态（登录平台 → 进知网 → 检索一次 → 导出全部 cookie，含 HttpOnly）；
- **curl_cffi**（伪装 Chrome 的 TLS/HTTP2 指纹）负责之后的全部 API 调用，绕开知网滑块验证。

```
browser-trust(Playwright) ─→ gxlib_session.json ─→ search / download / cite / meta (纯 HTTP)
```

## 安装

全新机器从零部署见 **[INSTALL.md](INSTALL.md)**（Python 版本、虚拟环境、国内镜像、首次信任态、常见问题）。

快速开始：

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json   # 填你的证号密码
python cnki_fulltext.py browser-trust
```

## 配置账号

复制 `config.example.json` 为 `config.json`（已 gitignore，不会提交），填入你的图书馆读者证号与密码：

```json
{
  "username": "你的证号",
  "password": "你的密码"
}
```

脚本启动时自动读取；未配置时 `login` / `browser-trust` 会给出提示。

## 使用

```bash
# 1) 建立信任态（必需，每会话一次；若触发滑块加 --headed 人工通过）
python cnki_fulltext.py browser-trust [--headed]

# 2) 检索（返回题名/URL/来源/日期）
python cnki_fulltext.py search "直播电商 消费者购买意愿" --limit 20

# 3) 下载全文 PDF（落盘 ./fulltext/）
python cnki_fulltext.py download <文章URL>

# 4) CNKI 原始引文（GB/T 7714-2025 / 知网研学 / EndNote + 结构化元数据）
python cnki_fulltext.py cite <文章URL>

# 5) 详情页元数据（摘要全文/关键词/基金/分类号/文章目录等）
python cnki_fulltext.py meta <文章URL>

# 其他
python cnki_fulltext.py login            # 纯 API 登录（建不了信任态，仅供会话刷新）
python cnki_fulltext.py import-cookies "<document.cookie>"   # 覆盖式导入浏览器 cookie
python cnki_fulltext.py session-info     # 查看当前会话
```

## 命令一览

| 命令 | 说明 |
|---|---|
| `browser-trust [--headed]` | Playwright 建信任态并导出全部 cookie（含 HttpOnly），推荐入口 |
| `search <关键词>` | 纯 API 检索（POST /kns8s/brief/grid） |
| `download <URL...>` | 全文 PDF 下载（授权链 bar→docgateway→docdown） |
| `cite <URL...>` | CNKI 原始引文 + 元数据（题名/摘要/关键词/机构/卷期页） |
| `meta <URL...>` | 详情页元数据（摘要/关键词/基金/分类号/目录） |
| `import-cookies <串>` | 覆盖式导入浏览器 document.cookie |
| `login` / `session-info` | 程序化登录 / 查看会话 |

## 注意事项

- **平台单会话策略**：每次 login/relogin 会踢掉旧会话（含浏览器会话）。信任态有效期内不要反复跑 `login`。
- **信任态有效期短**：会话过期后重新 `browser-trust` 一次即可。
- 会话文件 `gxlib_session.json` 含登录状态，已 gitignore，勿提交。
- 下载的 PDF 为版权文献，`fulltext/` 已 gitignore。
- PDF 正文提取用 pymupdf（`pdftotext` 对 CNKI 期刊 PDF 会乱码）。