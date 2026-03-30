"""
tools.py — LangGraph-compatible tool wrappers for the SONiC RCA agent.

All tools call the FastAPI checker endpoints (checker/api.py) or run
SONiC CLI commands directly. They return structured dicts that Claude
can reason over.

Environment variables:
    CHECKER_API_URL  Base URL of the checker FastAPI server
                     (default: http://127.0.0.1:8000)
    SONIC_HOST       Host for direct vtysh/traceroute commands
                     (default: 127.0.0.1, i.e. on-box)
"""

import json
import os
import subprocess
from typing import Annotated

import httpx
# DEAD CODE (MCP migration) — @tool decorator no longer used; tools are now
# defined in agent/mcp_server.py with @mcp.tool() and loaded at runtime via
# MultiServerMCPClient. Kept for reference.
# from langchain_core.tools import tool


_API_BASE = os.environ.get("CHECKER_API_URL", "http://127.0.0.1:8000")
_HTTP_TIMEOUT = 10.0


def _api_get(path: str, params: dict | None = None) -> dict:
    url = f"{_API_BASE}{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _api_post(path: str, body: dict | None = None) -> dict:
    url = f"{_API_BASE}{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(url, json=body or {})
        resp.raise_for_status()
        return resp.json()


def _run_local(cmd: str) -> str:
    """Run a shell command locally and return stdout + stderr."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        return out if out else err
    except subprocess.TimeoutExpired:
        return "Command timed out after 15 seconds."
    except Exception as exc:
        return f"Error running command: {exc}"


# ---------------------------------------------------------------------------
# DEAD CODE (MCP migration) — LangChain @tool-decorated functions
#
# These were the tool definitions used by agent.py before MCP adoption.
# Equivalent @mcp.tool() versions now live in agent/mcp_server.py and are
# loaded at runtime via MultiServerMCPClient. The private helpers above
# (_api_get, _api_post, _run_local) are still active — imported by mcp_server.py.
#
# Kept here as a reference for tool signatures and expected behaviour.
# ---------------------------------------------------------------------------

# @tool
# def get_inconsistencies() -> dict:
#     """
#     Fetch all cross-plane routing inconsistencies from the checker API
#     (noise-suppressed: SAI-internal, management-plane, and kernel-internal
#     entries are filtered out).
#
#     Returns a dict with:
#       - total: total number of inconsistencies
#       - critical/warning/info: counts by severity
#       - inconsistencies: list of {prefix, vrf, present_in, missing_in,
#         nexthop_mismatch, severity, diagnosis}
#
#     Call this first to get an overview of what is broken.
#     """
#     return _api_get("/inconsistencies")
#
#
# @tool
# def get_inconsistencies_raw() -> dict:
#     """
#     Fetch ALL cross-plane routing inconsistencies including suppressed noise
#     (SAI-internal OID entries, management-plane routes, kernel-internal
#     loopback routes, IPv6 link-local ghost entries).
#
#     Use this when get_inconsistencies() returns nothing but you suspect
#     there may be entries hidden by noise suppression, or when debugging
#     SONiC-VS-specific behavior.
#     """
#     return _api_get("/inconsistencies", params={"raw": "true"})
#
#
# @tool
# def get_route_detail(prefix: str) -> dict:
#     """
#     Return the per-prefix routing state across all four planes (FRR RIB,
#     APP_DB, ASIC_DB, Kernel FIB) for a specific prefix.
#
#     Args:
#         prefix: The IP prefix to look up, e.g. "10.1.0.0/24"
#
#     Returns a dict with keys frr, app_db, asic_db, kernel — each either
#     null (route absent) or a RouteEntry with nexthops, interfaces, protocol,
#     metric, and vrf.
#
#     Use this to drill into a specific inconsistent prefix.
#     """
#     encoded = prefix.replace("/", "%2F")
#     return _api_get(f"/routes/{encoded}")
#
#
# @tool
# def get_route_history(prefix: str) -> list:
#     """
#     Return the recent event log for a prefix from APP_DB Redis streams.
#
#     Args:
#         prefix: The IP prefix, e.g. "10.1.0.0/24"
#
#     Returns a list of {timestamp, event_type, prefix, plane} dicts,
#     newest first. Empty list if no history is available.
#
#     Useful for detecting BGP flaps or repeated install/withdraw cycles.
#     """
#     encoded = prefix.replace("/", "%2F")
#     return _api_get(f"/history/{encoded}")
#
#
# @tool
# def take_snapshot() -> dict:
#     """
#     Trigger a fresh route collection from all four planes and refresh the cache.
#
#     Returns a summary with route counts per plane and collection timestamp.
#     Use this before get_inconsistencies() if you want the freshest data.
#     """
#     return _api_post("/snapshot")
#
#
# @tool
# def get_orchagent_logs() -> str:
#     """
#     Return the last 200 syslog lines filtered for orchagent, syncd, and SAI
#     messages. This is the primary place to find SAI programming errors,
#     ASIC resource exhaustion, and orchagent crashes.
#
#     Look for lines containing: ERROR, WARN, SAI_STATUS, resource limit,
#     or entries like "Failed to create route entry".
#     """
#     return _run_local(
#         "tail -200 /var/log/syslog | grep -iE 'orchagent|syncd|SAI' || "
#         "tail -200 /var/log/syslog | grep -iE 'orchagent|syncd|SAI' 2>&1 || "
#         "echo 'No orchagent/syncd/SAI entries found in /var/log/syslog'"
#     )
#
#
# @tool
# def get_fpmsyncd_logs() -> str:
#     """
#     Return the last 200 syslog lines filtered for fpmsyncd, zebra, and
#     netlink messages. fpmsyncd bridges FRR and APP_DB; zebra manages the
#     kernel FIB via netlink.
#
#     Look for connection errors, route programming failures, or FPM socket
#     disconnects which would explain FRR → APP_DB gaps.
#     """
#     return _run_local(
#         "tail -200 /var/log/syslog | grep -iE 'fpmsyncd|zebra|netlink' || "
#         "tail -200 /var/log/syslog | grep -iE 'fpmsyncd|zebra|netlink' 2>&1 || "
#         "echo 'No fpmsyncd/zebra/netlink entries found in /var/log/syslog'"
#     )
#
#
# @tool
# def get_daemon_status() -> str:
#     """
#     Run `supervisorctl status` to check the running state of all SONiC
#     daemons (bgpd, zebra, fpmsyncd, orchagent, syncd, etc.).
#
#     A daemon in STOPPED, FATAL, or BACKOFF state is a likely root cause for
#     routing inconsistencies. Returns a table of daemon name, state, and uptime.
#     """
#     return _run_local("supervisorctl status 2>&1 || echo 'supervisorctl not available'")
#
#
# @tool
# def get_bgp_neighbors() -> str:
#     """
#     Run `vtysh -c 'show bgp summary json'` and return the BGP neighbor
#     summary as a JSON string.
#
#     Shows peer state (Established/Idle/Active), prefixes received, and
#     uptime. Useful for correlating route inconsistencies with BGP session
#     issues.
#     """
#     output = _run_local("vtysh -c 'show bgp summary json'")
#     # Validate it's actually JSON before returning
#     try:
#         data = json.loads(output)
#         return json.dumps(data, indent=2)
#     except json.JSONDecodeError:
#         return output  # return raw if not JSON (e.g. vtysh error)
#
#
# @tool
# def run_traceroute(destination: str) -> str:
#     """
#     Run a traceroute to a destination IP or prefix to test data-plane reachability.
#
#     Args:
#         destination: IP address or hostname to trace, e.g. "10.1.0.1"
#
#     Returns the traceroute output as a string. Use this to confirm whether
#     a black-holed route actually causes packet loss.
#     """
#     # Sanitize destination to prevent command injection
#     import re
#     if not re.match(r'^[0-9a-fA-F.:/-]+$', destination):
#         return f"Invalid destination: {destination!r}"
#     return _run_local(f"traceroute -n -m 10 -w 2 {destination}")
#
#
# @tool
# def get_checker_health() -> dict:
#     """
#     Check the health of the route checker service (Redis connectivity for
#     APP_DB and ASIC_DB, age of the cached snapshot).
#
#     Returns {status, redis_app_db, redis_asic_db, snapshot_age_seconds}.
#     """
#     return _api_get("/health")
#
#
# @tool
# def inject_fault(fault_type: str, prefix: str = "10.100.0.0/24") -> str:
#     """
#     Inject a fault scenario into the running SONiC environment for demo purposes.
#
#     Args:
#         fault_type: One of:
#             "drop_asic_route"    — delete prefix from ASIC_DB (simulates SAI failure)
#             "drop_app_route"     — delete prefix from APP_DB (simulates fpmsyncd failure)
#             "mismatched_nexthop" — write a wrong nexthop into APP_DB
#         prefix: The prefix to affect (default: "10.100.0.0/24")
#
#     Returns a description of what was injected and the expected inconsistency.
#
#     WARNING: This modifies live Redis state. For demo environments only.
#     """
#     import re
#     if not re.match(r'^[0-9./]+$', prefix):
#         return f"Invalid prefix: {prefix!r}"
#
#     if fault_type == "drop_asic_route":
#         # The ASIC_DB key includes a JSON suffix; we match on dest field
#         cmd = (
#             f"redis-cli -n 1 --scan --pattern "
#             f"'ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:*{prefix}*' | "
#             f"xargs -r redis-cli -n 1 DEL"
#         )
#         _run_local(cmd)
#         return (
#             f"Injected: deleted ASIC_DB entry for {prefix}. "
#             "Expected inconsistency: APP_DB present, ASIC_DB absent → "
#             "orchagent/SAI programming failure (warning severity)."
#         )
#
#     elif fault_type == "drop_app_route":
#         cmd = f"redis-cli -n 0 DEL 'ROUTE_TABLE:{prefix}'"
#         _run_local(cmd)
#         return (
#             f"Injected: deleted APP_DB entry for {prefix}. "
#             "Expected inconsistency: FRR present, APP_DB absent → "
#             "fpmsyncd not processing route (critical severity)."
#         )
#
#     elif fault_type == "mismatched_nexthop":
#         cmd = f"redis-cli -n 0 HSET 'ROUTE_TABLE:{prefix}' nexthop '1.2.3.4'"
#         _run_local(cmd)
#         return (
#             f"Injected: wrote fake nexthop 1.2.3.4 into APP_DB for {prefix}. "
#             "Expected inconsistency: nexthop mismatch between APP_DB and FRR → "
#             "partial update or race condition (warning severity)."
#         )
#
#     else:
#         return (
#             f"Unknown fault_type: {fault_type!r}. "
#             "Valid options: drop_asic_route, drop_app_route, mismatched_nexthop"
#         )


# ---------------------------------------------------------------------------
# DEAD CODE (MCP migration) — exported tool list for agent.py
#
# agent.py no longer imports TOOLS. Tools are now loaded from mcp_server.py
# via MultiServerMCPClient in build_agent().
# ---------------------------------------------------------------------------

# TOOLS = [
#     get_inconsistencies,
#     get_inconsistencies_raw,
#     get_route_detail,
#     get_orchagent_logs,
#     get_fpmsyncd_logs,
#     get_daemon_status,
#     get_route_history,
#     take_snapshot,
#     get_bgp_neighbors,
#     run_traceroute,
#     get_checker_health,
#     inject_fault,
# ]
