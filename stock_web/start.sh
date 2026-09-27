#!/usr/bin/env bash
# 股票分析看板启动脚本
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "缺少依赖，正在安装 fastapi + uvicorn ..."
  python3 -m pip install --user fastapi "uvicorn[standard]"
fi

PORT="${STOCK_WEB_PORT:-8787}"
echo "启动股票分析看板: http://127.0.0.1:${PORT}"
exec python3 server.py
