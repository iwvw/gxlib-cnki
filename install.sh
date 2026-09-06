#!/usr/bin/env bash
# gxlib-cnki 一键安装（macOS / Linux）
# 用法：./install.sh          国内网络加速：MIRROR=1 ./install.sh
set -e
cd "$(dirname "$0")"

echo "[1/5] 检查 Python (需 3.10-3.12)..."
python3 --version

echo "[2/5] 创建虚拟环境..."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate

echo "[3/5] 安装依赖..."
if [ -n "$MIRROR" ]; then
  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
else
  pip install -r requirements.txt
fi

echo "[4/5] 安装 Playwright 浏览器内核..."
if [ -n "$MIRROR" ]; then
  export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
fi
python -m playwright install chromium

echo "[5/5] 生成 config.json..."
[ -f config.json ] || cp config.example.json config.json

echo ""
echo "=== 安装完成 ==="
echo "  1) 编辑 config.json 填入图书馆读者证号/密码"
echo "  2) source .venv/bin/activate"
echo "  3) python cnki_fulltext.py browser-trust"