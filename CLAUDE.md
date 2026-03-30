# CLAUDE.md — SONiC Route Consistency Checker + AI Agent Demo

## 1. Project Overview

A route consistency checker for SONiC NOS that compares routing state across four planes
(FRR RIB, APP_DB, ASIC_DB, Kernel FIB), detects cross-plane inconsistencies, and drives a
LangGraph ReAct agent backed by Claude to perform root cause analysis. The full stack —
FastAPI collector/diff server, Streamlit dashboard with streaming Claude chat, fault
injection test harness — is built and working against a live SONiC-VS Docker container on
Ubuntu 24.04. Nothing is TODO; the demo runs end-to-end.

---

## 2. Quick Start

```bash
# Terminal 1 — start FastAPI inside the container + inject test routes
docker exec -d sonic-vs bash -c \
  "cd /opt/sonic-route-checker && \
   python3 -m uvicorn checker.api:app --host 0.0.0.0 --port 8000 --reload \
   > /tmp/api.log 2>&1"

docker exec sonic-vs vtysh -c "
conf t
ip route 10.30.0.0/24 Null0
ip route 10.40.0.0/24 Null0
end
write
"

# Allow inbound from Docker bridge gateway (required after container restart)
docker exec sonic-vs iptables -I INPUT 1 -s 172.17.0.1 -p tcp --dport 8000 -j ACCEPT

# Terminal 2 — start Streamlit on HOST (NOT inside container — container has no internet)
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/streamlit run dashboard/app.py \
  --server.port 8502 --server.address 0.0.0.0
```

Open `http://localhost:8502` — dashboard with live inconsistency table + streaming Claude chat.
Open `http://localhost:8000/docs` — FastAPI Swagger UI.

**Critical**: Streamlit runs on the HOST at port **8502**. Port 8501 is mapped to the
container but unused — the container has no internet so Anthropic API calls fail from inside.

---

## 3. Architecture

### SONiC Routing Pipeline

```
FRR (zebra/bgpd)
     │  via FPM socket (TCP :2620)
     ▼
fpmsyncd  ──────────────────────────────► APP_DB (Redis DB 0)
                                                  │
                                                  │ orchagent reads APP_DB
                                                  ▼
                                          SAI driver / syncd
                                                  │
                                                  ▼
                                          ASIC_DB (Redis DB 1)
                                                  │
                                                  ▼
                                          ASIC hardware (forwarding plane)

FRR (zebra) ──► netlink ──► Kernel FIB   (parallel path)
```

### Four Planes Compared

| Plane | Source | Key format |
|---|---|---|
| FRR RIB | `vtysh -c 'show ip route json'` | Only `selected`/`installed` entries |
| APP_DB | Redis DB 0 | `ROUTE_TABLE:<prefix>` |
| ASIC_DB | Redis DB 1 | `ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{json}` |
| Kernel FIB | pyroute2 netlink (AF_INET), fallback to `ip route show` | — |

### Agent Streaming Architecture

The agent uses LangGraph `stream_mode="messages"` throughout. This is a **single-phase**
design:

1. On first use, `build_agent()` spawns `agent/mcp_server.py` as a subprocess via
   `MultiServerMCPClient` (stdio transport) and loads all 12 tools as LangChain
   `BaseTool` objects. The subprocess is alive only for the duration of the tool-loading
   call; tools returned are plain objects that manage their own subprocess communication.
2. LangGraph ReAct loop runs with `recursion_limit=25`. All graph execution uses the
   **async** path (`agent.ainvoke` / `agent.astream`) because MCP tools are async-only.
   The `call_model` node is `async def` and uses `llm_with_tools.ainvoke()`.
3. The public API remains synchronous. `run_agent_query` and the non-stream `run_rca`
   bridge via `asyncio.run(agent.ainvoke(...))`. The streaming functions
   (`stream_agent_response`, `run_rca(stream=True)`) bridge via a daemon thread running
   `asyncio.run(agent.astream(...))` that feeds a `queue.Queue`; the sync generator
   drains the queue and yields events, preserving real-time token delivery to Streamlit.
4. Tool names are extracted from chunks where `tool_call_chunks` is non-empty.
5. Final answer text is extracted from chunks where `tool_call_chunks` is empty and
   `content` is a list of `{"type": "text", "text": "..."}` blocks.

**Critical implementation detail**: LangChain-Anthropic with `stream_mode="messages"`
always emits `content` as a **list of block dicts**, never a plain string. Checking
`isinstance(chunk.content, str)` will always be False and silently drops all tokens.
The correct extraction is:
```python
text = "".join(
    block.get("text", "")
    for block in chunk.content
    if isinstance(block, dict) and block.get("type") == "text"
)
```

---

## 4. Full Stack Status

| Component | Status | Runs on | Port | Access |
|---|---|---|---|---|
| Redis (APP_DB + ASIC_DB) | Running | Container | 6379 | `localhost:6379` (mapped) |
| FastAPI checker server | Running | Container | 8000 | `http://localhost:8000` |
| Streamlit dashboard | Running | **Host** | 8502 | `http://localhost:8502` |
| LangGraph RCA agent | Working | **Host** | — | imported by dashboard |
| Fault injection tool | Working | Host (docker exec) | — | `python3 tests/fault_inject.py` |

---

## 5. Environment

### Docker Container

```
Container name : sonic-vs
Image          : docker-sonic-vs
Port mappings  : -p 6379:6379 -p 8000:8000 -p 8501:8501
```

Port 8501 is mapped but unused. Streamlit runs on the host at 8502 to avoid the
container's lack of internet access (Anthropic API is unreachable from inside the
container).

**Ubuntu 24.04 nftables**: Docker port mappings work via Docker's own nftables rules.
Manually adding iptables rules on the host has no effect on Docker traffic. Port
mappings must be set at `docker run` time with `-p` flags. Inside the container,
add the INPUT rule to allow connections from the Docker bridge gateway:
```bash
docker exec sonic-vs iptables -I INPUT 1 -s 172.17.0.1 -p tcp --dport 8000 -j ACCEPT
```

**docker0 bridge NO-CARRIER** — seen after host sleep/resume or Docker restart:
```bash
docker exec sonic-vs ip link set eth0 up
# If docker0 is still DOWN:
sudo systemctl restart docker && docker start sonic-vs
```

**Python versions**: host = 3.12.3, container = 3.11.2. Always download wheels with
`--python-version 3.11 --platform manylinux_2_17_x86_64 --only-binary=:all:` — binary
wheels are not cross-version compatible.

**No internet inside container** — all Python dependencies are installed from pre-downloaded
wheels at `/tmp/sonic-wheels` and `/tmp/agent-wheels`. See "Recreate Container" section.

**ANTHROPIC_API_KEY** must be in the host environment where Streamlit and the agent CLI
run. Never set it inside the container.

### venv

The project uses `.venv` at the project root on the host (Python 3.12.3). All host-side
commands use `.venv/bin/python3` or `.venv/bin/streamlit`.

---

## 6. SONiC-VS Specifics

### Redis Databases

| DB ID | Name | Key pattern |
|---|---|---|
| 0 | APP_DB | `ROUTE_TABLE:<prefix>` or `ROUTE_TABLE:<vrf>:<prefix>` |
| 1 | ASIC_DB | `ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{json}` |
| 6 | STATE_DB | not used by checker |

Redis is bound to `127.0.0.1` inside the container. Accessible from the host only via the
`-p 6379:6379` port mapping.

### ASIC_DB Key Format

```
ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{"dest":"10.30.0.0/24","switch_id":"oid:0x21000000000000","vr":"oid:0x3000000000022"}
```

Split on `:` twice, then `json.loads()` the remainder. VRF field is `"vr"` not `"vr_id"`.
The collector handles both: `meta.get("vr", meta.get("vr_id", "default"))`.

ASIC_DB nexthops are SAI OIDs, not IP addresses. Nexthop comparison between ASIC_DB and
other planes is skipped entirely to avoid false positives.

### Running Daemons

Core daemons running: `orchagent`, `syncd`, `fpmsyncd`, `zebra`, `neighsyncd`,
`portsyncd`, `teamsyncd`, `fdbsyncd`, `natsyncd`.

`bgpd` starts STOPPED — start manually if needed:
```bash
docker exec sonic-vs supervisorctl start bgpd
```
"show bgp summary" returns "BGP instance not found" until a BGP ASN/neighbor is
configured in `/etc/frr/frr.conf`.

### Test Routes

Static blackhole routes via Null0 reliably program through the full pipeline
(FRR → fpmsyncd → APP_DB → orchagent → SAI → ASIC_DB). These are re-injected after
every container restart.

```bash
docker exec sonic-vs vtysh -c "
conf t
ip route 10.30.0.0/24 Null0
ip route 10.40.0.0/24 Null0
end
write
"
```

Routes via `eth0` (e.g. `via 172.17.0.1`) do NOT reach APP_DB — fpmsyncd deliberately
skips management-plane routes. Routes via Loopback0 require the interface to be
operationally up.

### Redis Usage

Always use cursor-based `SCAN` with `count=500`, never `KEYS *`. `KEYS *` blocks Redis
on large tables.

VRF routes in APP_DB: `ROUTE_TABLE:<vrf>:<prefix>`. Pass `vrf="Vrf_blue"` to
`AppDbCollector.collect()` for non-default VRFs.

Keyspace notifications (required for `subscribe_changes()`):
```bash
docker exec sonic-vs redis-cli config set notify-keyspace-events KEA
```

---

## 7. Diff Engine Filters

`DiffEngine._should_suppress()` in `checker/diff_engine.py` filters known-good SONiC-VS
noise from the default output. Call `diff(suppress_noise=False)` (or `GET
/inconsistencies?raw=true`) to see everything.

| Rule | Suppresses | Why |
|---|---|---|
| SAI-internal | ASIC_DB-only entries where `vrf` starts with `oid:` | SAI infrastructure entries (fe80::/10, link-local host routes) not visible to FRR/APP_DB by design |
| Kernel-internal | `127.0.0.0/8` and all subnets | Loopback routes never programmed into SONiC dataplane |
| Management-plane | `172.17.0.0/16` and subnets | Docker bridge routes fpmsyncd deliberately skips |
| IPv6 link-local | Any prefix within `fe80::/10` | fpmsyncd ghost entries written to APP_DB but never fully programmed through orchagent |

After filtering, a fresh container with test routes injected shows exactly **2 critical
inconsistencies** — `10.30.0.0/24` and `10.40.0.0/24` present in FRR+kernel but absent
from APP_DB+ASIC_DB (genuine fpmsyncd sync gap on container restart).

**stale_asic fault injection note**: the injected ASIC_DB key intentionally omits `"vr"`
so vrf resolves to `"default"` rather than an OID. ASIC_DB-only entries with OID VRFs are
noise-suppressed; entries with `vrf="default"` are treated as real inconsistencies.

---

## 8. Agent

### Tools (`agent/mcp_server.py`)

12 tools exposed as an MCP server over stdio transport. All run on the HOST (not
container) — API calls go to `http://127.0.0.1:8000` which is port-mapped from the
container. Subprocess tools (`get_daemon_status`, `get_orchagent_logs`, etc.) run
locally on the host, not via `docker exec`. `supervisorctl` is not available on the
host — those tools return `"supervisorctl not available"`.

`agent/tools.py` still exists but its `@tool`-decorated functions and `TOOLS` list are
**dead code** (commented out) since MCP adoption. The private helpers `_api_get`,
`_api_post`, `_run_local` remain active — they are imported by `mcp_server.py`.

| Tool | Description |
|---|---|
| `get_inconsistencies` | GET /inconsistencies — noise-suppressed, call this first |
| `get_inconsistencies_raw` | GET /inconsistencies?raw=true — includes suppressed noise |
| `get_route_detail(prefix)` | GET /routes/{prefix} — per-prefix state across all 4 planes |
| `get_orchagent_logs` | tail -200 /var/log/syslog grep orchagent/syncd/SAI |
| `get_fpmsyncd_logs` | tail -200 /var/log/syslog grep fpmsyncd/zebra/netlink |
| `get_daemon_status` | supervisorctl status (not available on host — returns "not available") |
| `get_route_history(prefix)` | GET /history/{prefix} — Redis stream events for prefix |
| `take_snapshot` | POST /snapshot — force fresh collection, bypasses 30s API cache |
| `get_bgp_neighbors` | vtysh -c 'show bgp summary json' |
| `run_traceroute(destination)` | traceroute -n -m 10 -w 2 {dst} |
| `get_checker_health` | GET /health — Redis connectivity + snapshot age |
| `inject_fault(fault_type, prefix)` | Injects demo faults directly into Redis |

### Agent Configuration

- **Model**: `claude-sonnet-4-6` via `langchain_anthropic.ChatAnthropic`
- **Tools**: loaded from `agent/mcp_server.py` at agent startup via
  `MultiServerMCPClient` (stdio transport). `asyncio.run(_load_mcp_tools())` is called
  once inside `build_agent()` — safe because all callers are synchronous (Streamlit main
  thread, CLI). Calling from an async context would raise `RuntimeError`.
- **recursion_limit**: 25 — passed in config at every `.ainvoke()` and `.astream()` call.
  This was 10 originally but 10 was too low: with 5+ tool calls, the graph hit the limit
  before the model could write a final answer, producing empty responses.
- **Graph**: `StateGraph(AgentState)` with `agent` and `tools` nodes; conditional edge
  routes to `tools` if `last.tool_calls`, else `__end__`.

### Streaming Fix (Critical)

`stream_mode="messages"` with LangChain-Anthropic always emits `AIMessageChunk.content`
as a **list of dicts**, never a plain string. The content blocks look like:
```python
[{"type": "text", "text": "Route is installed in FRR...", "index": 0}]
```
Checking `isinstance(chunk.content, str)` always returns False and silently drops all
final-answer tokens. The fix in `stream_agent_response()`:
```python
if isinstance(chunk.content, list):
    text = "".join(
        block.get("text", "")
        for block in chunk.content
        if isinstance(block, dict) and block.get("type") == "text"
    )
```
Skip chunks where `chunk.tool_call_chunks` is non-empty (tool call in progress) or
`chunk.tool_call_id` is set (ToolMessage).

### Public API

```python
from agent.agent import run_agent_query, stream_agent_response, run_rca

# Blocking — returns {"answer": str, "tool_calls": list[str], "messages": list[dict]}
result = run_agent_query("RCA for 10.30.0.0/24", conversation_history=[])

# Streaming generator — yields ("tool_call", name) and ("token", text)
for event_type, content in stream_agent_response("RCA for 10.30.0.0/24"):
    ...

# CLI streaming — yields ("tool_call", name) and ("response", full_text)
for event_type, content in run_rca("...", stream=True):
    ...
```

### CLI

```bash
# From project root on host, with venv activated
python3 -m agent.agent --diagnose           # auto full RCA
python3 -m agent.agent --query "RCA for 10.30.0.0/24"
python3 -m agent.agent                      # interactive
```

### Performance

~20–38 seconds wall clock for a full diagnosis. Streaming makes perceived wait much
shorter — first tokens appear within 2–3 seconds of tool calls completing.

### LLM Choice

`claude-sonnet-4-6` only. Ollama was tested (qwen2.5-coder:7b) and rejected: 4m36s
response time, incorrect Redis DB IDs, missed fpmsyncd as root cause.

---

## 9. Dashboard

Runs on the **host** at port **8502** (not inside the container).

```bash
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/streamlit run dashboard/app.py \
  --server.port 8502 --server.address 0.0.0.0
```

### Left Panel — Inconsistency Table

- Wrapped in `@st.fragment(run_every=10)` (requires Streamlit ≥ 1.33; installed version
  is 1.55.0 on both host and container).
- Fragment re-runs every 10 s independently — does NOT trigger a full page rerun, so the
  chat panel is unaffected.
- Raw mode toggle: calls `/inconsistencies?raw=true` to include suppressed noise.
- Manual refresh button forces `last_fetch_ts = 0` to bypass the 30s API cache.
- Countdown timer (`Next refresh in Xs`) rendered via `st.components.v1.html` with a
  self-looping JS `setInterval`. The JS resets to `REFRESH_INTERVAL_S` when it reaches 0
  rather than stopping, because Streamlit reuses the component iframe across fragment
  re-runs and does not re-execute the script on each re-run.
- No `time.sleep()` or `st.rerun()` anywhere — those caused full-page reruns that
  re-triggered agent calls.

### Right Panel — Claude Chat

- Uses `stream_agent_response()` from `agent/agent.py` — tokens stream in as they arrive.
- `st.chat_input()` not `st.text_input()` — only triggers rerun on submit, not keystrokes.
- `last_processed_query` in `st.session_state` prevents the same query from being
  processed twice if a fragment rerun coincides with the response.
- Live tool status line (`tool_status = st.empty()`) shows `🔧 tool1 → tool2 → ...` while
  tools are running; cleared once text starts streaming.
- Fallback message shown if stream completes with no text tokens.
- Restart required after code changes (Streamlit reloads the module but session state
  persists stale agent singletons).

---

## 10. Fault Injection

`tests/fault_inject.py` runs on the host via `docker exec sonic-vs`. No SSH required.

### CLI

```bash
python3 tests/fault_inject.py <scenario>            # inject
python3 tests/fault_inject.py <scenario> --restore  # undo
python3 tests/fault_inject.py list                  # describe all
python3 tests/fault_inject.py demo                  # guided walkthrough with pauses
python3 tests/fault_inject.py restore-all           # undo all scenarios
```

### Scenarios

| Scenario | Prefix | Plane break | Detected severity | Pattern |
|---|---|---|---|---|
| `fpmsyncd_gap` | 10.50.0.0/24 | Stops fpmsyncd, adds FRR route | CRITICAL | FRR present, APP_DB absent |
| `sai_failure` | 10.60.0.0/24 | Writes to APP_DB, no ASIC_DB | WARNING | APP_DB present, ASIC_DB absent |
| `stale_asic` | 10.70.0.0/24 | Writes to ASIC_DB, no APP_DB/FRR | WARNING | ASIC_DB present, others absent |
| `nexthop_mismatch` | 10.80.0.0/24 | APP_DB nexthop=10.0.0.1, kernel=172.17.0.1 | WARNING | nexthop_mismatch populated |

### Implementation Notes

- All Redis writes use `dexec_py()` — pipes a Python script via `docker exec -i python3`
  stdin to avoid shell quoting issues with JSON keys.
- `fpmsyncd_gap` uses `supervisorctl stop/start fpmsyncd`. After restore, fpmsyncd
  reconnects to zebra's FPM socket and re-syncs the full RIB.
- `stale_asic` omits `"vr"` from the ASIC_DB key so `vrf="default"` (not an OID),
  bypassing the SAI-internal noise suppression rule.
- `nexthop_mismatch` uses `172.17.0.1` (Docker bridge gateway) as the kernel nexthop —
  always reachable on eth0 inside the container.
- After injection, call `POST /snapshot` to bypass the 30s API cache before querying
  `/inconsistencies`.

### Verify After Injection

```bash
curl -s -X POST http://localhost:8000/snapshot
curl -s http://localhost:8000/inconsistencies | python3 -m json.tool
curl -s 'http://localhost:8000/inconsistencies?raw=true' | python3 -m json.tool
```

---

## 11. API Endpoints

All served by FastAPI inside the container, port-mapped to host at 8000.

| Method | Path | Description | Notes |
|---|---|---|---|
| GET | `/health` | Redis connectivity + snapshot age | Returns `{status, redis_app_db, redis_asic_db, snapshot_age_seconds}` |
| GET | `/inconsistencies` | Diff engine output, noise-suppressed | `suppress_noise=True` |
| GET | `/inconsistencies?raw=true` | Diff engine output, unfiltered | `suppress_noise=False` |
| GET | `/routes/{prefix}` | Per-prefix state across all 4 planes | URL-encode `/` as `%2F` |
| POST | `/snapshot` | Force fresh collection, reset 30s cache | Returns `{timestamp, app_db_routes, ...}` |
| GET | `/history/{prefix}` | Redis stream event log for prefix | Requires keyspace notifications enabled |
| GET | `/docs` | FastAPI Swagger UI | Auto-generated from Pydantic models |

**Snapshot cache**: 30s TTL in `_CACHE_TTL`. After fault injection always POST `/snapshot`
first. `SONIC_HOST` env var sets the Redis host (default `127.0.0.1`).

---

## 12. Recreate Container From Scratch

```bash
# Kill anything using host port 8000
kill $(lsof -t -i:8000) 2>/dev/null

docker stop sonic-vs && docker rm sonic-vs
docker run \
  -p 6379:6379 \
  -p 8000:8000 \
  -p 8501:8501 \
  -it --name sonic-vs --privileged -d \
  --network bridge --dns 8.8.8.8 --dns 8.8.4.4 \
  docker-sonic-vs

sleep 45  # SONiC takes ~45s to fully boot

# Download wheels on host (one-time, if not already done)
# Checker deps:
pip3 download fastapi uvicorn redis pyroute2 \
  starlette pydantic anyio sniffio h11 \
  -d /tmp/sonic-wheels \
  --python-version 3.11 --platform manylinux_2_17_x86_64 --only-binary=:all:

# Agent deps (langgraph, langchain-anthropic, mcp, etc.):
pip3 download langgraph langchain-anthropic langchain-core mcp langchain-mcp-adapters \
  -d /tmp/agent-wheels \
  --python-version 3.11 --platform manylinux_2_17_x86_64 --only-binary=:all:

# Copy wheels and project into container
docker cp /tmp/sonic-wheels sonic-vs:/tmp/sonic-wheels
docker cp /tmp/agent-wheels sonic-vs:/tmp/agent-wheels
docker cp ~/projects/sonic-route-checker sonic-vs:/opt/sonic-route-checker

# Install deps
docker exec sonic-vs pip3 install --no-index \
  --find-links /tmp/sonic-wheels \
  fastapi uvicorn redis pyroute2

docker exec sonic-vs pip3 install --no-index \
  --find-links /tmp/agent-wheels \
  langgraph langchain-anthropic langchain-core mcp langchain-mcp-adapters

# Allow inbound from Docker bridge gateway
docker exec sonic-vs iptables -I INPUT 1 -s 172.17.0.1 -p tcp --dport 8000 -j ACCEPT

# Start FastAPI
docker exec -d sonic-vs bash -c \
  "cd /opt/sonic-route-checker && \
   python3 -m uvicorn checker.api:app --host 0.0.0.0 --port 8000 --reload \
   > /tmp/api.log 2>&1"

# Inject test routes
docker exec sonic-vs vtysh -c "
conf t
ip route 10.30.0.0/24 Null0
ip route 10.40.0.0/24 Null0
end
write
"

# Start Streamlit on HOST (not in container)
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/streamlit run dashboard/app.py \
  --server.port 8502 --server.address 0.0.0.0
```

### Downloading Binary Wheels

Some packages (numpy, pyarrow, rpds-py, jiter, orjson) require explicit platform flags:

```bash
pip3 download "rpds-py>=0.25.0" jiter orjson \
  --platform manylinux_2_17_x86_64 --python-version 3.11 \
  --only-binary=:all: -d /tmp/sonic-wheels
```

---

## 13. Known Issues and Decisions

| Issue | Decision / Resolution |
|---|---|
| Container has no internet | Streamlit + agent run on host. Port 8501 mapped but unused. Streamlit on host port 8502. |
| `recursion_limit=5` caused empty agent responses | Raised to 25. Each tool call = 2 graph steps (agent + tools node). 5 steps → only 2 tool calls before graph terminates, not enough to produce a final answer. |
| LangGraph stream content not captured | `AIMessageChunk.content` is always a list of block dicts, not a string. Fixed by extracting `block.get("text")` from blocks with `type == "text"`. Checking `isinstance(content, str)` silently drops all tokens. |
| `graph.compile(recursion_limit=N)` raises TypeError | `recursion_limit` is a runtime config, not a compile-time argument. Pass via `config={"recursion_limit": 25}` in every `.stream()` and `.invoke()` call. |
| Routes via eth0/172.17.0.1 nexthop don't program | fpmsyncd skips management-plane routes. Use Null0 (blackhole) routes for test routes. |
| `stale_asic` scenario was noise-suppressed | Fixed by omitting `"vr"` from ASIC_DB key — vrf resolves to "default" instead of an OID, bypassing the SAI-internal filter. |
| Full-page reruns during auto-refresh triggered agent twice | Fixed with `@st.fragment(run_every=10)` on the left panel (isolates refresh) and `last_processed_query` guard on the chat input. |
| `supervisorctl` not available on host | `get_daemon_status` and log tools return "not available" when run from the host. Only useful when agent runs inside the container. For demo: daemon status is still informative for the base container daemons shown by `supervisorctl status` output cached from prior runs. |
| MCP tool loading adds ~0.5–1.5s to first agent call | `build_agent()` spawns `mcp_server.py` subprocess and performs MCP handshake on first `get_agent()` call. Subsequent calls use the singleton — no extra cost. |
| `asyncio.run()` not reentrant | `_load_mcp_tools()` is called via `asyncio.run()` inside `build_agent()`. Safe for all current callers (Streamlit main thread, CLI). Would raise `RuntimeError` if called from within an already-running event loop. |
| `StructuredTool does not support sync invocation` | MCP tools from `langchain-mcp-adapters` are async-only. Fixed by switching all graph execution to the async path: `call_model` is now `async def` using `ainvoke`; `run_agent_query` and non-stream `run_rca` use `asyncio.run(agent.ainvoke(...))`; streaming functions run `agent.astream()` in a daemon thread and feed a `queue.Queue` to preserve sync generator semantics. |
| `.env` not loaded automatically | Dashboard reads `ANTHROPIC_API_KEY` from the environment. Added `_load_dotenv()` in `dashboard/app.py` that parses the project-root `.env` file as a fallback if the env var is not already set. No new dependency — pure stdlib. |

---

## 14. Repository Structure

```
sonic-route-checker/
├── checker/
│   ├── __init__.py         exports RouteEntry, RouteSnapshot, RouteCollector,
│   │                       Inconsistency, DiffEngine
│   ├── collector.py        AppDbCollector, AsicDbCollector, FrrCollector,
│   │                       KernelFibCollector, RouteCollector, RouteSnapshot
│   ├── diff_engine.py      DiffEngine, Inconsistency, noise suppression rules
│   └── api.py              FastAPI server, 5 endpoints, 30s snapshot cache
├── agent/
│   ├── __init__.py
│   ├── agent.py            LangGraph StateGraph, run_agent_query,
│   │                       stream_agent_response, run_rca, CLI;
│   │                       loads tools from mcp_server.py via MultiServerMCPClient
│   ├── mcp_server.py       FastMCP server — 12 tools over stdio transport;
│   │                       reuses _api_get/_api_post/_run_local from tools.py
│   ├── tools.py            private helpers (_api_get, _api_post, _run_local) —
│   │                       active, imported by mcp_server.py; @tool functions
│   │                       and TOOLS list are dead code (commented out)
│   └── prompts.py          SYSTEM_PROMPT — SONiC domain context for Claude
├── dashboard/
│   └── app.py              Streamlit UI, st.fragment auto-refresh, streaming chat;
│                           reads ANTHROPIC_API_KEY from .env if not in environment
├── tests/
│   └── fault_inject.py     4 fault scenarios via docker exec
├── .venv/                  Python 3.12 venv on host (langgraph, streamlit, etc.)
├── .env                    ANTHROPIC_API_KEY (loaded by host process)
└── CLAUDE.md               this file
```

---

## 15. TODO

- BGP configuration for real BGP routes (`router bgp 65001` in `/etc/frr/frr.conf`,
  `supervisorctl restart bgpd`)
- `infra/topology.yaml` — Containerlab multi-node topology (sonic1, sonic2, exabgp1)
- `infra/exabgp.conf` — BGP route injection/withdrawal for demo
- `start.sh` — single script to start FastAPI + inject routes + print URLs
- Nothing blocks the demo — project is complete as-is

---

## 16. Demo Script

```bash
# 0. Ensure container is running and FastAPI is up
curl -s http://localhost:8000/health | python3 -m json.tool

# 1. Start Streamlit on host (if not already running)
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/streamlit run dashboard/app.py \
  --server.port 8502 --server.address 0.0.0.0 &

# 2. Open http://localhost:8502
#    Left panel shows: Total=2, Critical=2 (10.30.0.0/24, 10.40.0.0/24)
#    These are the baseline test routes missing from ASIC_DB — normal on fresh start.

# 3. Inject a fault
python3 tests/fault_inject.py fpmsyncd_gap

# 4. Wait 10 seconds — dashboard auto-refreshes (fragment run_every=10)
#    Count goes to Total=3, Critical=3 — 10.50.0.0/24 appears

# 5. In the chat panel, ask:
#    "give me RCA for 10.50.0.0/24"
#    Watch: tool names appear in status line → tokens stream in within ~5s of tools finishing
#    Agent identifies: fpmsyncd STOPPED, route stuck before APP_DB, fix: supervisorctl start fpmsyncd

# 6. Restore
python3 tests/fault_inject.py fpmsyncd_gap --restore

# 7. Dashboard clears back to Total=2 after next auto-refresh

# 8. For full walkthrough of all 4 scenarios:
python3 tests/fault_inject.py demo
```
