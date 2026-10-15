#!/usr/bin/env bash
# ChartLab control script:  ./chartlab.sh build | start | stop | restart | logs | status | test | dev
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=chartlab
IMAGE=chartlab:latest
PORT="${CHARTLAB_PORT:-8050}"
KAGGLE="$HOME/.cache/kagglehub/datasets/debashis74017"
INDEX_DIR="${CHARTLAB_INDEX_DIR:-$(ls -d "$KAGGLE"/nifty-50-minute-data/versions/*/ 2>/dev/null | sort -V | tail -1)}"
STOCK_DIR="${CHARTLAB_STOCK_DIR:-$(ls -d "$KAGGLE"/stock-market-data-nifty-50-stocks-1-min-data/versions/*/ 2>/dev/null | sort -V | tail -1)}"
CREDS="${CHARTLAB_ANGEL_CREDS:-$HOME/.config/angel/creds.env}"

start() {
  mkdir -p "$ROOT/data"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  local args=(-d --name "$NAME" --restart unless-stopped -p "127.0.0.1:$PORT:8000" --user "$(id -u):$(id -g)"
              --dns 1.1.1.1 --dns 8.8.8.8          # fixed public DNS: Wi-Fi changes must not cut live data
              -v "$ROOT/data:/data/app" -v "$ROOT/custom_indicators:/app/custom_indicators")
  if [[ -n "$INDEX_DIR" ]]; then args+=(-v "$INDEX_DIR:/data/kaggle/index:ro"); else echo "⚠ Kaggle index data not found (charts need history)"; fi
  [[ -n "$STOCK_DIR" ]] && args+=(-v "$STOCK_DIR:/data/kaggle/stocks:ro")
  if [[ -f "$CREDS" ]]; then args+=(-v "$CREDS:/run/secrets/angel.env:ro"); else echo "ℹ No Angel One creds file — using Yahoo (delayed) for recent/live data"; fi
  docker run "${args[@]}" "$IMAGE" >/dev/null
  echo "ChartLab running → http://localhost:$PORT   (logs: ./chartlab.sh logs)"
}

case "${1:-}" in
  build)   docker build -t "$IMAGE" "$ROOT" ;;
  start)   start ;;
  stop)    docker rm -f "$NAME" >/dev/null && echo stopped ;;
  restart) start ;;
  logs)    docker logs -f --tail 100 "$NAME" ;;
  status)  docker ps --filter "name=^$NAME$" --format '{{.Names}}  {{.Status}}  {{.Ports}}'; curl -s "http://localhost:$PORT/api/health" && echo ;;
  test)    (cd "$ROOT/backend" && PYTHONPATH="$ROOT/.pylib:.:tests" python3 -m pytest -q tests) && (cd "$ROOT" && node --test frontend/tests/) ;;
  dev)     cd "$ROOT/backend" && PYTHONPATH="$ROOT/.pylib:." exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload ;;
  *)       echo "usage: $0 build | start | stop | restart | logs | status | test | dev"; exit 1 ;;
esac
