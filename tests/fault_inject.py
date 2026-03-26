#!/usr/bin/env python3
"""
fault_inject.py — SONiC route consistency checker: fault injection

Targets the sonic-vs Docker container directly via docker exec.
Each scenario creates a specific, observable inconsistency that the checker
API at http://localhost:8000/inconsistencies will detect.

Scenarios
---------
  fpmsyncd_gap     Stop fpmsyncd, add FRR route → FRR present, APP_DB absent (CRITICAL)
  sai_failure      Write to APP_DB directly, skip ASIC → APP_DB present, ASIC_DB absent (WARNING)
  stale_asic       Write to ASIC_DB directly, no APP_DB/FRR → ASIC_DB present, others absent (WARNING)
  nexthop_mismatch Write same prefix with different nexthops to APP_DB vs kernel FIB (WARNING)

Usage
-----
  python3 tests/fault_inject.py <scenario>             # inject fault
  python3 tests/fault_inject.py <scenario> --restore   # undo fault
  python3 tests/fault_inject.py list                   # show all scenarios
  python3 tests/fault_inject.py demo                   # guided walkthrough
  python3 tests/fault_inject.py restore-all            # undo everything
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Constants (verified against live sonic-vs container)
# ---------------------------------------------------------------------------

CONTAINER = "sonic-vs"

# switch_id extracted from live ASIC_DB: used when writing fake ASIC_DB entries.
# Intentionally omit "vr" from ASIC_DB keys so the diff engine resolves vrf
# to "default" (not an OID), preventing noise-suppression from hiding the entry.
SWITCH_ID = "oid:0x21000000000000"

# Synthetic prefixes — chosen to not collide with real routes (10.30, 10.40)
# or management subnets (172.x) suppressed by the diff engine's noise filter.
PREFIXES = {
    "fpmsyncd_gap":     "10.50.0.0/24",
    "sai_failure":      "10.60.0.0/24",
    "stale_asic":       "10.70.0.0/24",
    "nexthop_mismatch": "10.80.0.0/24",
}

# Docker bridge gateway — always reachable on eth0 inside the container.
# Used as the "kernel" nexthop in nexthop_mismatch (APP_DB will have a different one).
DOCKER_GW = "172.17.0.1"


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def dexec(cmd: str, timeout: int = 15) -> tuple[str, str]:
    """Run a shell command inside the container. Returns (stdout, stderr)."""
    r = subprocess.run(
        ["docker", "exec", CONTAINER, "bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip(), r.stderr.strip()


def dexec_py(script: str) -> tuple[str, str]:
    """Pipe a Python script into the container's python3 via stdin."""
    r = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3"],
        input=script,
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout.strip(), r.stderr.strip()


def rdb_get(db: int, cmd: str) -> str:
    """Run redis-cli -n <db> <cmd> inside the container and return output."""
    out, _ = dexec(f"redis-cli -n {db} {cmd}")
    return out


def vtysh(*cmds: str) -> str:
    """Run a sequence of vtysh commands inside the container."""
    flags = " ".join(f"-c '{c}'" for c in cmds)
    out, err = dexec(f"vtysh {flags}")
    return out or err


def check_container() -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == "true"


def fetch_inconsistencies(raw: bool = False) -> dict:
    """Call GET /inconsistencies and return parsed JSON."""
    url = "http://localhost:8000/inconsistencies" + ("?raw=true" if raw else "")
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_SEV_COLOR = {"CRITICAL": "\033[91m", "WARNING": "\033[93m", "INFO": "\033[94m"}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"


def _sev(s: str) -> str:
    return _SEV_COLOR.get(s.upper(), "") + s.upper() + _RESET


def banner(title: str) -> None:
    print(f"\n{_BOLD}{'─' * 62}{_RESET}")
    print(f"{_BOLD}  {title}{_RESET}")
    print(f"{_BOLD}{'─' * 62}{_RESET}")


def print_api_state(raw: bool = False) -> None:
    """Print a short summary of current /inconsistencies response."""
    data = fetch_inconsistencies(raw=raw)
    if "error" in data:
        print(f"  {_DIM}[API unreachable: {data['error']}]{_RESET}")
        return
    total = data.get("total", 0)
    if total == 0:
        print(f"  [API] {_DIM}No inconsistencies.{_RESET}")
        return
    crit = data.get("critical", 0)
    warn = data.get("warning", 0)
    info = data.get("info", 0)
    print(f"  [API] {total} inconsistencies — "
          f"{_sev('critical')}: {crit}  {_sev('warning')}: {warn}  {_sev('info')}: {info}")
    for iss in data.get("inconsistencies", []):
        sev    = iss.get("severity", "info").upper()
        prefix = iss.get("prefix", "?")
        diag   = iss.get("diagnosis", "")[:72]
        print(f"         [{_sev(sev)}] {_BOLD}{prefix}{_RESET}")
        print(f"         {_DIM}{diag}{_RESET}")
        mismatch = iss.get("nexthop_mismatch", {})
        if mismatch:
            for plane, nhs in mismatch.items():
                print(f"         {_DIM}  nexthop ({plane}): {nhs}{_RESET}")


# ===========================================================================
# Scenario: fpmsyncd_gap
# ===========================================================================

def inject_fpmsyncd_gap() -> None:
    """
    Stop fpmsyncd so a freshly added FRR route is never forwarded to APP_DB.

    Pipeline break: FRR → [fpmsyncd DOWN] → APP_DB → ASIC_DB
    Result: 10.50.0.0/24 in FRR + kernel, absent from APP_DB + ASIC_DB.
    Severity: CRITICAL (FRR present, ASIC_DB absent).
    """
    prefix = PREFIXES["fpmsyncd_gap"]
    banner(f"fpmsyncd_gap — INJECT  ({prefix})")

    print("  [1/3] Stopping fpmsyncd...")
    out, _ = dexec("supervisorctl stop fpmsyncd")
    print(f"        {out or '(ok)'}")
    time.sleep(1)

    print(f"  [2/3] Adding {prefix} to FRR (static blackhole via Null0)...")
    out = vtysh("configure terminal", f"ip route {prefix} Null0", "end", "write memory")
    print(f"        {out or '(ok)'}")
    time.sleep(1)

    print(f"  [3/3] Checking plane coverage...")
    frr_count, _ = dexec(
        f"vtysh -c 'show ip route {prefix}' 2>/dev/null | grep -c Null0 || echo 0"
    )
    app_exists = rdb_get(0, f"EXISTS ROUTE_TABLE:{prefix}")
    print(f"        FRR    : {'✓ present' if frr_count.strip() != '0' else '✗ absent'}")
    print(f"        APP_DB : {'✓ present' if app_exists == '1' else '✗ absent (expected)'}")

    print()
    print(f"  Expected inconsistency:")
    print(f"    severity  : {_sev('CRITICAL')}")
    print(f"    prefix    : {prefix}")
    print(f"    present   : frr, kernel")
    print(f"    missing   : app_db, asic_db")
    print(f"    diagnosis : fpmsyncd has not forwarded route to orchagent")
    print()
    print(f"  Verify  : curl -s 'http://localhost:8000/inconsistencies' | python3 -m json.tool")
    print(f"  Restore : python3 tests/fault_inject.py fpmsyncd_gap --restore")


def restore_fpmsyncd_gap() -> None:
    prefix = PREFIXES["fpmsyncd_gap"]
    banner(f"fpmsyncd_gap — RESTORE  ({prefix})")

    print(f"  [1/2] Removing {prefix} from FRR...")
    out = vtysh("configure terminal", f"no ip route {prefix} Null0", "end", "write memory")
    print(f"        {out or '(ok)'}")

    print("  [2/2] Starting fpmsyncd...")
    out, _ = dexec("supervisorctl start fpmsyncd")
    print(f"        {out or '(ok)'}")
    time.sleep(2)


# ===========================================================================
# Scenario: sai_failure
# ===========================================================================

def inject_sai_failure() -> None:
    """
    Write a route directly to APP_DB as if fpmsyncd forwarded it, but do NOT
    write to ASIC_DB.  Simulates orchagent receiving a route but failing to
    program it via SAI (e.g. ASIC resource exhaustion, SAI driver bug).

    Pipeline break: APP_DB → [orchagent/SAI FAIL] → ASIC_DB
    Result: 10.60.0.0/24 in APP_DB only.
    Severity: WARNING (APP_DB present, ASIC_DB absent).
    """
    prefix = PREFIXES["sai_failure"]
    banner(f"sai_failure — INJECT  ({prefix})")

    print(f"  [1/2] Writing {prefix} to APP_DB (simulating fpmsyncd output)...")
    out, err = dexec_py(f"""
import redis
r = redis.Redis(db=0, decode_responses=True)
r.hset("ROUTE_TABLE:{prefix}", mapping={{
    "nexthop":   "",
    "ifname":    "Null0",
    "blackhole": "true",
}})
print("hset ok")
""")
    print(f"        {out or err or '(ok)'}")

    print(f"  [2/2] Confirming ASIC_DB has no entry for {prefix}...")
    asic_count, _ = dexec(
        f"redis-cli -n 1 --scan --pattern "
        f"'ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:*' | grep -c '{prefix}' || echo 0"
    )
    print(f"        ASIC_DB matching keys: {asic_count.strip() or '0'} (expected 0)")

    print()
    print(f"  Expected inconsistency:")
    print(f"    severity  : {_sev('WARNING')}")
    print(f"    prefix    : {prefix}")
    print(f"    present   : app_db")
    print(f"    missing   : frr, asic_db, kernel")
    print(f"    diagnosis : orchagent received the route but SAI programming failed")
    print()
    print(f"  Verify  : curl -s 'http://localhost:8000/inconsistencies' | python3 -m json.tool")
    print(f"  Restore : python3 tests/fault_inject.py sai_failure --restore")


def restore_sai_failure() -> None:
    prefix = PREFIXES["sai_failure"]
    banner(f"sai_failure — RESTORE  ({prefix})")
    print(f"  Deleting {prefix} from APP_DB...")
    out = rdb_get(0, f"DEL ROUTE_TABLE:{prefix}")
    print(f"  DEL result: {out} (1=deleted, 0=not found)")


# ===========================================================================
# Scenario: stale_asic
# ===========================================================================

def inject_stale_asic() -> None:
    """
    Write a route entry directly into ASIC_DB with no corresponding APP_DB,
    FRR, or kernel entry.  Simulates a stale hardware entry left behind after
    orchagent crashed mid-delete or a failed route withdrawal propagation.

    Note on the key format: we intentionally omit the 'vr' field so the diff
    engine resolves vrf → "default" rather than an OID string.  ASIC_DB-only
    entries with OID VRFs are noise-suppressed (they are SAI infrastructure
    entries); entries with vrf="default" are real inconsistencies.

    Result: 10.70.0.0/24 in ASIC_DB only.
    Severity: WARNING (ASIC_DB present, kernel absent — and all others absent).
    """
    prefix  = PREFIXES["stale_asic"]
    banner(f"stale_asic — INJECT  ({prefix})")

    print(f"  [1/2] Writing {prefix} directly to ASIC_DB (no APP_DB, no FRR)...")
    out, err = dexec_py(f"""
import redis, json
r = redis.Redis(db=1, decode_responses=True)
# Omit 'vr' key so vrf resolves to "default" (not an OID) — prevents noise suppression
key = "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:" + json.dumps({{
    "dest": "{prefix}",
    "switch_id": "{SWITCH_ID}",
}})
r.hset(key, mapping={{"SAI_ROUTE_ENTRY_ATTR_PACKET_ACTION": "SAI_PACKET_ACTION_FORWARD"}})
print("hset ok:", key)
""")
    print(f"        {out or err or '(ok)'}")

    print(f"  [2/2] Confirming APP_DB and FRR do not have {prefix}...")
    app_exists = rdb_get(0, f"EXISTS ROUTE_TABLE:{prefix}")
    frr_count, _ = dexec(
        f"vtysh -c 'show ip route {prefix}' 2>/dev/null | grep -c {prefix} || echo 0"
    )
    print(f"        APP_DB : {'✓ present' if app_exists == '1' else '✗ absent (expected)'}")
    print(f"        FRR    : {'✓ present' if frr_count.strip() != '0' else '✗ absent (expected)'}")

    print()
    print(f"  Expected inconsistency:")
    print(f"    severity  : {_sev('WARNING')}")
    print(f"    prefix    : {prefix}")
    print(f"    present   : asic_db")
    print(f"    missing   : frr, app_db, kernel")
    print(f"    diagnosis : ASIC_DB present but no matching FRR/APP_DB entry — "
          f"stale entry after failed withdrawal")
    print()
    print(f"  Verify  : curl -s 'http://localhost:8000/inconsistencies' | python3 -m json.tool")
    print(f"  Restore : python3 tests/fault_inject.py stale_asic --restore")


def restore_stale_asic() -> None:
    prefix = PREFIXES["stale_asic"]
    banner(f"stale_asic — RESTORE  ({prefix})")
    print(f"  Deleting {prefix} from ASIC_DB...")
    out, err = dexec_py(f"""
import redis, json
r = redis.Redis(db=1, decode_responses=True)
key = "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:" + json.dumps({{
    "dest": "{prefix}",
    "switch_id": "{SWITCH_ID}",
}})
result = r.delete(key)
print(f"deleted {{result}} key(s)")
""")
    print(f"  {out or err}")


# ===========================================================================
# Scenario: nexthop_mismatch
# ===========================================================================

def inject_nexthop_mismatch() -> None:
    """
    Write the same prefix to APP_DB and the kernel FIB with deliberately
    different nexthop IPs, simulating a race condition or partial update
    where fpmsyncd and zebra disagree on the best path.

    APP_DB nexthop  : 10.0.0.1   (synthetic — written directly)
    Kernel nexthop  : 172.17.0.1 (Docker bridge gateway — always reachable on eth0)

    The diff engine compares nexthop sets across non-ASIC planes.
    Result: 10.80.0.0/24 present in app_db + kernel, nexthop_mismatch populated.
    Severity: WARNING (app_db present, asic_db absent — plus nexthop mismatch detail).
    """
    prefix     = PREFIXES["nexthop_mismatch"]
    nh_app_db  = "10.0.0.1"
    nh_kernel  = DOCKER_GW
    banner(f"nexthop_mismatch — INJECT  ({prefix})")
    print(f"  APP_DB nexthop : {nh_app_db}")
    print(f"  Kernel nexthop : {nh_kernel} (Docker bridge gateway via eth0)")

    print(f"\n  [1/2] Writing {prefix} to APP_DB with nexthop {nh_app_db}...")
    out, err = dexec_py(f"""
import redis
r = redis.Redis(db=0, decode_responses=True)
r.hset("ROUTE_TABLE:{prefix}", mapping={{
    "nexthop": "{nh_app_db}",
    "ifname":  "Ethernet0",
}})
print("hset ok")
""")
    print(f"        {out or err or '(ok)'}")

    print(f"  [2/2] Adding {prefix} via {nh_kernel} to kernel FIB...")
    out, err = dexec(
        f"ip route replace {prefix} via {nh_kernel} dev eth0 proto static 2>&1"
    )
    print(f"        {out or err or '(ok)'}")

    print(f"\n  Kernel FIB entry:")
    out, _ = dexec(f"ip route show {prefix}")
    print(f"        {out or '(not found)'}")

    print()
    print(f"  Expected inconsistency:")
    print(f"    severity         : {_sev('WARNING')}")
    print(f"    prefix           : {prefix}")
    print(f"    present          : app_db, kernel")
    print(f"    missing          : frr, asic_db")
    print(f"    nexthop_mismatch : app_db=['{nh_app_db}']  kernel=['{nh_kernel}']")
    print(f"    diagnosis        : orchagent received route, SAI not programmed; "
          f"nexthop mismatch between planes")
    print()
    print(f"  Verify  : curl -s 'http://localhost:8000/inconsistencies' | python3 -m json.tool")
    print(f"  Restore : python3 tests/fault_inject.py nexthop_mismatch --restore")


def restore_nexthop_mismatch() -> None:
    prefix = PREFIXES["nexthop_mismatch"]
    banner(f"nexthop_mismatch — RESTORE  ({prefix})")

    print(f"  [1/2] Deleting {prefix} from APP_DB...")
    out = rdb_get(0, f"DEL ROUTE_TABLE:{prefix}")
    print(f"        DEL result: {out}")

    print(f"  [2/2] Removing {prefix} from kernel FIB...")
    out, _ = dexec(f"ip route del {prefix} 2>/dev/null && echo 'deleted' || echo 'not found'")
    print(f"        {out}")


# ===========================================================================
# Dispatch table
# ===========================================================================

SCENARIOS: dict[str, dict] = {
    "fpmsyncd_gap": {
        "inject":      inject_fpmsyncd_gap,
        "restore":     restore_fpmsyncd_gap,
        "severity":    "CRITICAL",
        "pattern":     "FRR present, APP_DB absent",
        "description": "Stop fpmsyncd so a new FRR route never reaches APP_DB",
        "prefix":      PREFIXES["fpmsyncd_gap"],
    },
    "sai_failure": {
        "inject":      inject_sai_failure,
        "restore":     restore_sai_failure,
        "severity":    "WARNING",
        "pattern":     "APP_DB present, ASIC_DB absent",
        "description": "Write directly to APP_DB, skip ASIC_DB (simulates SAI programming failure)",
        "prefix":      PREFIXES["sai_failure"],
    },
    "stale_asic": {
        "inject":      inject_stale_asic,
        "restore":     restore_stale_asic,
        "severity":    "WARNING",
        "pattern":     "ASIC_DB present, no APP_DB / FRR / kernel",
        "description": "Write directly to ASIC_DB with no matching entry in other planes",
        "prefix":      PREFIXES["stale_asic"],
    },
    "nexthop_mismatch": {
        "inject":      inject_nexthop_mismatch,
        "restore":     restore_nexthop_mismatch,
        "severity":    "WARNING",
        "pattern":     "Nexthop mismatch: APP_DB != kernel",
        "description": "Write conflicting nexthops to APP_DB and kernel FIB for the same prefix",
        "prefix":      PREFIXES["nexthop_mismatch"],
    },
}


# ===========================================================================
# Demo sequence
# ===========================================================================

def run_demo() -> None:
    banner("SONiC Route Consistency Checker — Demo Sequence")
    print("  Injects each fault in turn, pauses for you to inspect the dashboard,")
    print("  then restores.  Dashboard: http://localhost:8501")
    print("  API:       http://localhost:8000/inconsistencies")

    if not check_container():
        print(f"\n  ERROR: container '{CONTAINER}' is not running.")
        sys.exit(1)

    def pause(scenario_name: str, s: dict) -> None:
        print(f"\n  {_DIM}{'·' * 58}{_RESET}")
        print(f"  Fault '{scenario_name}' is live.")
        print(f"  Try asking the Claude chat:")
        print(f"    \"What's wrong with {s['prefix']}? How do I fix it?\"")
        print(f"  Or: curl -s http://localhost:8000/inconsistencies | python3 -m json.tool")
        try:
            input(f"\n  Press Enter to restore and continue... ")
        except (KeyboardInterrupt, EOFError):
            print("\n  Interrupted — restoring all faults...")
            run_restore_all()
            sys.exit(0)

    for name, s in SCENARIOS.items():
        print(f"\n{'═' * 62}")
        print(f"  {_BOLD}Scenario: {name}{_RESET}  [{_sev(s['severity'])}]")
        print(f"  Pattern : {s['pattern']}")
        print(f"  {_DIM}{s['description']}{_RESET}")

        s["inject"]()
        time.sleep(2)

        print("\n  Live API state:")
        print_api_state()

        pause(name, s)

        s["restore"]()
        time.sleep(2)

        print("  After restore:")
        print_api_state()

    print(f"\n{'═' * 62}")
    print("  Demo complete — all faults restored.")


def run_restore_all() -> None:
    """Undo all scenarios regardless of current state."""
    banner("Restore all")
    for name, s in SCENARIOS.items():
        print(f"\n  Restoring {name}...")
        try:
            s["restore"]()
        except Exception as e:
            print(f"  Warning ({name}): {e}")


# ===========================================================================
# CLI
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fault_inject.py",
        description="SONiC route fault injector — targets the sonic-vs Docker container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "scenarios:\n"
            + "\n".join(
                f"  {n:<20} [{s['severity']:8}] {s['pattern']}"
                for n, s in SCENARIOS.items()
            )
            + "\n\nexamples:\n"
            "  python3 tests/fault_inject.py fpmsyncd_gap\n"
            "  python3 tests/fault_inject.py fpmsyncd_gap --restore\n"
            "  python3 tests/fault_inject.py demo\n"
            "  python3 tests/fault_inject.py restore-all\n"
        ),
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        metavar="SCENARIO",
        choices=list(SCENARIOS) + ["list", "demo", "restore-all"],
        help="Scenario to run, or: list / demo / restore-all",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Undo the fault instead of injecting it",
    )

    args = parser.parse_args()

    if not args.scenario or args.scenario == "list":
        print(f"\n{_BOLD}Available fault injection scenarios:{_RESET}\n")
        for name, s in SCENARIOS.items():
            print(f"  {_BOLD}{name}{_RESET}")
            print(f"    severity    : {_sev(s['severity'])}")
            print(f"    pattern     : {s['pattern']}")
            print(f"    prefix used : {s['prefix']}")
            print(f"    action      : {s['description']}")
            print()
        sys.exit(0)

    if args.scenario == "demo":
        run_demo()
        return

    if args.scenario == "restore-all":
        run_restore_all()
        return

    if not check_container():
        print(f"ERROR: container '{CONTAINER}' is not running.")
        sys.exit(1)

    s = SCENARIOS[args.scenario]
    if args.restore:
        s["restore"]()
    else:
        s["inject"]()


if __name__ == "__main__":
    main()
