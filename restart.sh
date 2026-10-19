#!/bin/bash
# 重启抖音提取 API
# 服务已由 systemd 托管（douyin-api.service），这里委托给 systemctl，
# 避免手工进程与 systemd 同时占用 8765 端口。
set -e
PORT="${1:-8765}"

if systemctl list-unit-files douyin-api.service >/dev/null 2>&1; then
  systemctl restart douyin-api
  sleep 2
  systemctl is-active --quiet douyin-api || {
    echo "启动失败，最近日志："
    journalctl -u douyin-api -n 20 --no-pager
    exit 1
  }
  echo "已重启 douyin-api (systemd)"
else
  # 无 systemd 时的降级路径：前台手工拉起
  DIR="$(cd "$(dirname "$0")" && pwd)"
  PIDFILE="/tmp/douyin-api.pid"
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    sleep 1
  fi
  cd "$DIR" || exit 1
  setsid nohup python3 server.py --port "$PORT" > "$DIR/server.log" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  sleep 2
  echo "已启动 (手工, pid $(cat "$PIDFILE"))"
fi

curl -s -m 5 "http://127.0.0.1:$PORT/health"; echo
