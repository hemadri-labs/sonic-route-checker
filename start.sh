#!/usr/bin/env bash
# start.sh — Bring up the full SONiC Route Checker stack
#
# Prerequisites:
#   - sonic-vs container is running
#   - .env file exists with ANTHROPIC_API_KEY=sk-ant-...
#   - .venv/ exists with all host dependencies installed
#
# Usage:
#   ./start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Check ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if [ -f .env ]; then
        # shellcheck disable=SC1091
        set -a && source .env && set +a
    fi
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set."
    echo "  Set it in .env:  echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env"
    echo "  Or export it:    export ANTHROPIC_API_KEY=sk-ant-..."
    exit 1
fi

echo "ANTHROPIC_API_KEY: set (${#ANTHROPIC_API_KEY} chars)"

# ---------------------------------------------------------------------------
# 2. Verify sonic-vs container is running
# ---------------------------------------------------------------------------

if ! docker inspect sonic-vs --format '{{.State.Status}}' 2>/dev/null | grep -q running; then
    echo "ERROR: sonic-vs container is not running."
    echo "  Start it with: docker start sonic-vs"
    exit 1
fi

echo "Container sonic-vs: running"

# ---------------------------------------------------------------------------
# 3. Bring eth0 up (guard against NO-CARRIER after host sleep/resume)
# ---------------------------------------------------------------------------

docker exec sonic-vs ip link set eth0 up 2>/dev/null || true
echo "eth0: up"

# ---------------------------------------------------------------------------
# 4. Start bgpd if not running
# ---------------------------------------------------------------------------

docker exec sonic-vs supervisorctl start bgpd 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Re-inject test routes (lost on every container restart)
# ---------------------------------------------------------------------------

echo "Injecting test routes..."
docker exec sonic-vs vtysh -c "
conf t
ip route 10.30.0.0/24 Null0
ip route 10.40.0.0/24 Null0
end
write
" > /dev/null 2>&1
echo "Test routes: 10.30.0.0/24, 10.40.0.0/24 via Null0"

# ---------------------------------------------------------------------------
# 6. Kill and restart FastAPI inside container
# ---------------------------------------------------------------------------

echo "Restarting FastAPI..."
docker exec sonic-vs bash -c "pkill -f 'uvicorn checker.api' 2>/dev/null; sleep 1; true"

docker exec -d sonic-vs bash -c \
    "cd /opt/sonic-route-checker && \
     python3 -m uvicorn checker.api:app \
     --host 0.0.0.0 --port 8000 --reload > /tmp/api.log 2>&1"

# ---------------------------------------------------------------------------
# 7. Wait and verify FastAPI health
# ---------------------------------------------------------------------------

echo "Waiting for FastAPI to start..."
sleep 3

HEALTH=$(curl -s --max-time 5 http://localhost:8000/health 2>/dev/null || true)
if [ -z "$HEALTH" ]; then
    echo "ERROR: FastAPI health check failed — no response from http://localhost:8000/health"
    echo "  Check logs: docker exec sonic-vs cat /tmp/api.log"
    exit 1
fi

echo "FastAPI health: $HEALTH" | python3 -m json.tool 2>/dev/null || echo "FastAPI health: $HEALTH"

# ---------------------------------------------------------------------------
# 8. Kill any streamlit running on host port 8502
# ---------------------------------------------------------------------------

STREAMLIT_PIDS=$(lsof -t -i:8502 2>/dev/null || true)
if [ -n "$STREAMLIT_PIDS" ]; then
    echo "Stopping existing Streamlit on port 8502..."
    # shellcheck disable=SC2086
    kill $STREAMLIT_PIDS 2>/dev/null || true
    sleep 1
fi

# ---------------------------------------------------------------------------
# 9. Start Streamlit on host
# ---------------------------------------------------------------------------

VENV_STREAMLIT="$SCRIPT_DIR/.venv/bin/streamlit"
if [ ! -x "$VENV_STREAMLIT" ]; then
    echo "ERROR: Streamlit not found at $VENV_STREAMLIT"
    echo "  Install with: .venv/bin/pip install streamlit"
    exit 1
fi

echo "Starting Streamlit on http://localhost:8502 ..."
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
CHECKER_API_URL="http://localhost:8000" \
nohup "$VENV_STREAMLIT" run dashboard/app.py \
    --server.port 8502 \
    --server.address 0.0.0.0 \
    --server.headless true \
    > /tmp/streamlit.log 2>&1 &

STREAMLIT_PID=$!
sleep 2

if ! kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    echo "ERROR: Streamlit failed to start."
    echo "  Check logs: cat /tmp/streamlit.log"
    exit 1
fi

# ---------------------------------------------------------------------------
# 10. Final status
# ---------------------------------------------------------------------------

echo ""
echo "============================================================"
echo " Stack is up"
echo "============================================================"
echo "  FastAPI:    http://localhost:8000"
echo "  Swagger UI: http://localhost:8000/docs"
echo "  Dashboard:  http://localhost:8502"
echo ""
echo "  FastAPI logs:    docker exec sonic-vs cat /tmp/api.log"
echo "  Streamlit logs:  cat /tmp/streamlit.log"
echo ""
echo "Fault injection:"
echo "  python3 tests/fault_inject.py list"
echo "  python3 tests/fault_inject.py fpmsyncd_gap"
echo "  python3 tests/fault_inject.py fpmsyncd_gap --restore"
echo "============================================================"
