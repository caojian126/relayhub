#!/usr/bin/env bash
# 一键：起 RelayHub + 两个假中转站 -> 跑端到端测试 -> 重启验证数据还在
#
#   bash tests/run_e2e.sh
#
# 可用环境变量覆盖：
#   PY=/path/to/python   RH_PORT=8099   DATA=/tmp/rh-e2e-data
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-python3}"
RH_PORT="${RH_PORT:-8099}"
DATA="${DATA:-/tmp/rh-e2e-data}"
LOG="/tmp/relayhub-e2e"
mkdir -p "$LOG"

RH_PID=""; M1_PID=""; M2_PID=""
cleanup() {
  [ -n "$RH_PID" ] && kill "$RH_PID" 2>/dev/null
  [ -n "$M1_PID" ] && kill "$M1_PID" 2>/dev/null
  [ -n "$M2_PID" ] && kill "$M2_PID" 2>/dev/null
  sleep 1
}
trap cleanup EXIT

wait_up() {  # $1=url  $2=秒
  for _ in $(seq 1 "$2"); do
    if "$PY" - "$1" <<'PY' 2>/dev/null
import sys, httpx
httpx.get(sys.argv[1], timeout=2)
PY
    then return 0; fi
    sleep 1
  done
  return 1
}

echo "==> 清理旧进程与数据"
pkill -f "uvicorn app.main:app" 2>/dev/null
rm -rf "$DATA"; mkdir -p "$DATA"

echo "==> 启动 A站(8101) / B站(8102)"
MOCK_PORT=8101 "$PY" -m uvicorn tests.mock_upstream:app --host 127.0.0.1 --port 8101 \
  --log-level warning > "$LOG/mockA.log" 2>&1 &
M1_PID=$!
MOCK_PORT=8102 "$PY" -m uvicorn tests.mock_upstream:app --host 127.0.0.1 --port 8102 \
  --log-level warning > "$LOG/mockB.log" 2>&1 &
M2_PID=$!

echo "==> 启动 RelayHub(:$RH_PORT)  DATA_DIR=$DATA"
cd "$ROOT"
DATA_DIR="$DATA" ADMIN_PASSWORD=test123 "$PY" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$RH_PORT" > "$LOG/relayhub.log" 2>&1 &
RH_PID=$!

wait_up "http://127.0.0.1:8101/v1/models" 20 || { echo "假站 A 起不来"; exit 1; }
wait_up "http://127.0.0.1:$RH_PORT/healthz" 25 || {
  echo "RelayHub 起不来，日志："; tail -30 "$LOG/relayhub.log"; exit 1; }

echo
echo "==> 前端 JS 静态检查（不需要浏览器）"
"$PY" tests/js_check.py
JSRC=$?

echo
"$PY" tests/e2e.py
RC=$?

echo
echo "==> 重启 RelayHub，验证 SQLite 持久化"
cleanup
RH_PID=""
DATA_DIR="$DATA" ADMIN_PASSWORD=test123 "$PY" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$RH_PORT" > "$LOG/relayhub-restart.log" 2>&1 &
RH_PID=$!
wait_up "http://127.0.0.1:$RH_PORT/healthz" 25 || { echo "重启后起不来"; exit 1; }

"$PY" - "$RH_PORT" <<'PY'
import sys, httpx
port = sys.argv[1]
base = f"http://127.0.0.1:{port}"
c = httpx.Client(timeout=20)
tok = c.post(base + "/admin/api/login",
             json={"username": "admin", "password": "test123"}).json()["token"]
h = {"Authorization": "Bearer " + tok}
sites = c.get(base + "/admin/api/sites", headers=h).json()
groups = c.get(base + "/admin/api/groups", headers=h).json()
print(f"  重启后站点数 = {len(sites)}  {[s['name'] for s in sites]}")
print(f"  重启后统一模型 = {[g['name'] for g in groups]}")
ok = len(sites) >= 1 and any(g["name"] == "auto" for g in groups)
print("  [PASS] 重启后配置仍在" if ok else "  [FAIL] 重启后配置丢了")
sys.exit(0 if ok else 1)
PY
PRC=$?

echo
if [ "$RC" -eq 0 ] && [ "$PRC" -eq 0 ] && [ "$JSRC" -eq 0 ]; then
  echo "全部通过 ✅"
else
  echo "有失败项（js=$JSRC e2e=$RC 持久化=$PRC）❌"
  echo "日志目录：$LOG"
fi
exit $(( JSRC + RC + PRC ))
