#!/bin/bash
# 用 IP 访问网站时保持 COOKIE_SECURE=0（不要改成 1）
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
  echo "错误：没有 python3。请先在宝塔「软件商店」安装 Python 3，或安装：apt install python3 python3-venv python3-pip"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "第一次运行：正在创建虚拟环境并安装依赖（可能要 1～2 分钟）..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements-server.txt
else
  source .venv/bin/activate
fi

export COOKIE_SECURE=0

echo "正在启动… 浏览器打开：http://本服务器IP:8000/"
echo "（若打不开：宝塔「安全」和云服务器「安全组」都要放行 8000 端口）"
echo "按 Ctrl+C 可停止服务。"
uvicorn server.main:app --host 0.0.0.0 --port 8000
