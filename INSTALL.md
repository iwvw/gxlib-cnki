# INSTALL.md · 全新机器部署教程

目标：在一台**新机器**上从零跑通「检索 → 下载全文 → 引文/元数据」。

> 环境假设：Windows 10/11（macOS / Linux 步骤基本一致，差异处已标注）。

## 0. 前置环境

| 组件 | 要求 | 说明 |
|---|---|---|
| Python | **3.10 – 3.12** | 3.13 部分依赖可能不兼容；用 3.10~3.12 最稳 |
| Git | 任意新版本 | 取代码用；或直接拷贝文件夹可跳过 |
| curl | Win10 1803+ 自带 | 脚本登录/会话用它（外壳 curl，无需安装）；Linux/macOS 自带 |

检查：

```bash
python --version     # 建议 3.10~3.12
git --version
curl --version       # Windows 10/11 自带
```

## 1. 获取代码（二选一）

**方式 A：直接拷贝**（仓库当前无远端，最省事）
把整个 `gxlib-cnki/` 文件夹（含 `.git/`）拷到新机器任意位置，保留 git 历史。

**方式 B：git 远端**（多机同步、协作）
1. 在 GitHub / Gitee 建空仓库（注意：**不要勾选**添加 README/.gitignore，保持空）
2. 本机推一次：
   ```bash
   cd gxlib-cnki
   git remote add origin <你的仓库地址>
   git push -u origin main
   ```
3. 新机器克隆：
   ```bash
   git clone <你的仓库地址>
   cd gxlib-cnki
   ```

> 仓库内 `config.json`、`gxlib_session.json`、`fulltext/` 已在 `.gitignore`，**不会**被推送，放心。

## 2. 创建虚拟环境 + 装依赖

```bash
cd gxlib-cnki
python -m venv .venv                # 创建虚拟环境
.venv\Scripts\activate              # Windows 激活（macOS/Linux: source .venv/bin/activate）
pip install -r requirements.txt     # curl_cffi / playwright / pymupdf
python -m playwright install chromium   # 下载 Playwright 浏览器内核（约 130MB）
```

**国内网络加速**（可选，慢或失败时加）：

```bash
# pip 走清华镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# playwright 浏览器内核走镜像
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/   # Windows
python -m playwright install chromium
```

## 3. 配置账号

```bash
copy config.example.json config.json    # Windows
# macOS/Linux: cp config.example.json config.json
```

编辑 `config.json`，填入图书馆读者证号与密码：

```json
{
  "username": "你的证号",
  "password": "你的密码"
}
```

> 该文件已被 gitignore，不会入库。

## 4. 首次运行：建立信任态

```bash
python cnki_fulltext.py browser-trust
```

- 自动完成：登录平台 → 进知网 → 检索一次 → 导出全部 cookie（含 HttpOnly）写入 `gxlib_session.json`
- 无头模式默认；**若出现滑块验证**，改用有头模式人工拖一次：
  ```bash
  python cnki_fulltext.py browser-trust --headed
  ```

## 5. 功能验证

```bash
# 检索（应返回真实论文标题）
python cnki_fulltext.py search "直播电商 消费者购买意愿" --limit 5

# 下载全文 PDF（落盘 ./fulltext/）
python cnki_fulltext.py download <文章URL>

# CNKI 原始引文（GB/T 7714-2025）
python cnki_fulltext.py cite <文章URL>

# 详情页元数据（摘要/关键词/基金/目录）
python cnki_fulltext.py meta <文章URL>
```

## 常见问题（实测踩坑记录）

| 现象 | 处理 |
|---|---|
| **pip 安装失败（清华镜像无 curl_cffi）** | 镜像源部分包缺失是常见现象：`install.ps1 -Mirror` / `MIRROR=1` 已加**自动回退官方 PyPI**；也可手动 `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` 失败后去掉 `-i` 重试官方源 |
| **MCP 里 `setup_trust` 报 "Sync API inside the asyncio loop"** | 已修复：`mcp_server.py` 全部工具改为 `async + asyncio.to_thread`，Playwright/curl_cffi 同步调用卸载到工作线程。若自改代码触发，同样用 `await asyncio.to_thread(同步函数, 参数)` |
| **报错 `'Locator' object is not callable`** | 代码里 `rl.first().click()` 写错（`first` 是属性不是方法）：应为 `rl.first.click()`。新版 Playwright 才暴露，`browser-trust` 触发 relogin 分支时必现 |
| **移动/复制目录时被拒（OneDrive 权限）** | OneDrive 目录（`D:\AAADATA\OneDrive - moi\...`）文件访问常被拒，`Move-Item` 报错。改用 `robocopy <源> <目标> /E` 复制后手动删源，或直接放到非 OneDrive 目录（如 `C:\Users\<用户>\tools\`） |
| **信任态有效期短，移机后失效** | 会话过期是常态：`session-info` 显示无效或 `search` 报会话错误时，重跑 `python cnki_fulltext.py browser-trust`（约 30 秒）即可恢复 |
| `playwright install chromium` 下载失败 | 设 `PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/` 后重试 |
| `browser-trust` 触发滑块 | 加 `--headed` 有头模式人工通过一次 |
| 提示 `未配置账号密码` | 检查 `config.json` 是否存在、字段名是否正确 |
| 下载报「授权链返回非PDF」 | 会话过期或该文无 PDF 全文，重新 `browser-trust` 后再试 |

## 注意事项

> ⚠️ **防滥用红线**：平台有反滥用检测。**严禁**循环/高频调用 `browser-trust`、`login`、或脚本化轰炸检索/下载——会被标记「涉嫌恶意下载」并停用账号（需联系广西图书馆读者服务申诉恢复，剩余登录次数有限）。保持人工使用节奏：信任态过期重跑一次 `browser-trust` 即可，检索间隔数秒以上。出现「已停用」提示立即停止一切自动操作。

- **单会话策略**：登录/接管会踢掉旧会话（含浏览器里的），信任态有效期内不要反复跑 `login`。
- **信任态有效期短**：会话过期重新 `browser-trust` 一次即可（约 30 秒）。
- 下载的 PDF 为版权文献，仅限本人论文写作使用，勿公开传播。