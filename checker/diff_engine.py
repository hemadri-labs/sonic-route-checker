"""
diff_engine.py — SONiC route consistency checker: cross-plane diffing

Compares routes across all four routing planes (FRR RIB, APP_DB, ASIC_DB,
Kernel FIB) and produces structured Inconsistency reports.

Known inconsistency patterns:
  FRR present, APP_DB absent   → fpmsyncd not processing route
  APP_DB present, ASIC_DB absent → orchagent/SAI programming failure
  ASIC_DB present, kernel absent → netlink/fpmsyncd sync issue
  Nexthop mismatch between planes → partial update or race condition

Note on ASIC_DB nexthops: ASIC_DB stores SAI OIDs, not IP addresses.
Full OID → IP resolution requires an additional ASIC_DB lookup that is
out of scope here. Nexthop comparison involving ASIC_DB is therefore
skipped and flagged as "unresolved" to avoid false positives.

Filter rules (suppress from output by default):
  - SAI-internal entries: ASIC_DB-only routes whose VRF is a SAI OID
    (e.g. "oid:0x3000000000003"). These are ASIC infrastructure entries
    intentionally not surfaced to FRR or APP_DB.
  - Management-plane routes: routes learned via eth0 / Docker bridge
    (172.17.0.0/16 and its host routes). SONiC deliberately does not
    program management routes into the dataplane.
  - Kernel-internal routes: loopback and local host routes (127.0.0.0/8,
    127.0.0.1/32, 127.255.255.255/32, <container-ip>/32) that will never
    appear in SONiC's APP_DB or ASIC_DB.
  - IPv6 link-local infrastructure: fe80::/64 APP_DB ghost entries that
    fpmsyncd writes but never fully programs through to the ASIC.
"""

from dataclasses import dataclass, field
from ipaddress import ip_network, ip_address
from typing import Optional

from .collector import RouteSnapshot


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PLANES = ("frr", "app_db", "asic_db", "kernel")


@dataclass
class Inconsistency:
    """A detected inconsistency between routing planes for a single prefix."""
    prefix: str
    vrf: str
    # Which planes have/lack this prefix
    present_in: list[str]
    missing_in: list[str]
    # Nexthop mismatch details: plane → nexthop list (empty if not applicable)
    nexthop_mismatch: dict[str, list[str]] = field(default_factory=dict)
    # "critical" | "warning" | "info"
    severity: str = "info"
    # Human-readable one-liner for display and agent prompting
    diagnosis: str = ""

    def to_dict(self) -> dict:
        return {
            "prefix": self.prefix,
            "vrf": self.vrf,
            "present_in": self.present_in,
            "missing_in": self.missing_in,
            "nexthop_mismatch": self.nexthop_mismatch,
            "severity": self.severity,
            "diagnosis": self.diagnosis,
        }


# ---------------------------------------------------------------------------
# Filter configuration
# ---------------------------------------------------------------------------

# Prefixes that are always kernel-internal and will never be programmed
# into SONiC's dataplane — suppress entirely regardless of plane coverage.
_KERNEL_INTERNAL_PREFIXES = {
    "127.0.0.0/8",
    "127.0.0.1/32",
    "127.255.255.255/32",
}

# Management subnets — routes learned via eth0 / Docker bridge that SONiC
# intentionally does not program into the dataplane.
_MGMT_SUBNETS = [
    ip_network("172.17.0.0/16"),   # Docker bridge default
    ip_network("172.16.0.0/12"),   # Broader RFC1918 mgmt range (optional)
]

# IPv6 link-local prefix — fe80::/10 and fe80::/64 ghost entries written
# by fpmsyncd but never fully programmed through orchagent to the ASIC.
_IPV6_LINK_LOCAL = ip_network("fe80::/10")


# ---------------------------------------------------------------------------
# Severity and diagnosis rules
# ---------------------------------------------------------------------------

def _classify(present: set[str], missing: set[str]) -> tuple[str, str]:
    """
    Return (severity, diagnosis) based on which planes have/lack the route.

    Rules are ordered by impact severity — first match wins.
    """
    if "frr" in present and "asic_db" in missing:
        return "critical", (
            "Route is installed in FRR but absent from ASIC_DB — "
            "traffic will be black-holed. Likely orchagent or SAI programming failure."
        )

    if "frr" in present and "app_db" in missing:
        return "critical", (
            "Route is in FRR RIB but missing from APP_DB — "
            "fpmsyncd has not forwarded it to orchagent."
        )

    if "app_db" in present and "asic_db" in missing:
        return "warning", (
            "Route is in APP_DB but absent from ASIC_DB — "
            "orchagent received the route but SAI programming failed. "
            "Check orchagent logs for SAI errors or ASIC resource limits."
        )

    if "asic_db" in present and "kernel" in missing:
        return "warning", (
            "Route is programmed in ASIC_DB but missing from the kernel FIB — "
            "netlink or fpmsyncd sync issue."
        )

    if "kernel" in present and "app_db" in missing and "frr" in missing:
        return "info", (
            "Route is in the kernel FIB only (not seen by FRR or APP_DB) — "
            "likely a kernel-internal or locally generated route."
        )

    missing_str = ", ".join(sorted(missing))
    present_str = ", ".join(sorted(present))
    return "info", (
        f"Route present in [{present_str}] but missing from [{missing_str}]."
    )


# ---------------------------------------------------------------------------
# DiffEngine
# ---------------------------------------------------------------------------

class DiffEngine:
    """
    Compares a RouteSnapshot across all four planes and returns a list of
    Inconsistency objects for every prefix where planes disagree.

    Usage:
        engine = DiffEngine(snapshot)
        issues = engine.diff()

        # Include suppressed entries in output (useful for debugging)
        issues = engine.diff(suppress_noise=False)
    """

    def __init__(self, snapshot: RouteSnapshot):
        self._snap = snapshot

    def _plane_map(self) -> dict[str, dict[str, object]]:
        return {
            "frr":     self._snap.frr,
            "app_db":  self._snap.app_db,
            "asic_db": self._snap.asic_db,
            "kernel":  self._snap.kernel,
        }

    @staticmethod
    def _canonical(prefix: str) -> str:
        try:
            return str(ip_network(prefix, strict=False))
        except ValueError:
            return prefix

    @staticmethod
    def _should_suppress(prefix: str, vrf: str, present: set[str]) -> tuple[bool, str]:
        """
        Return (should_suppress, reason) for a given inconsistency.

        Suppressed entries are excluded from diff() output by default to
        reduce noise from known-good SONiC-VS behavior.

        Rules:
          1. SAI-internal: ASIC_DB-only entries with an OID VRF are ASIC
             infrastructure entries not visible to FRR/APP_DB by design.
          2. Kernel-internal: loopback and local host routes that SONiC
             never programs into the dataplane.
          3. Management-plane: routes on the Docker/mgmt subnet that
             fpmsyncd deliberately does not forward to orchagent.
          4. IPv6 link-local: fe80:: entries fpmsyncd writes to APP_DB
             but never programs through to the ASIC.
        """
        # Rule 1: SAI-internal — ASIC_DB-only with OID VRF
        if present == {"asic_db"} and vrf.startswith("oid:"):
            return True, "SAI-internal ASIC infrastructure entry"

        try:
            net = ip_network(prefix, strict=False)
        except ValueError:
            return False, ""

        # Rule 2: Kernel-internal loopback routes
        if prefix in _KERNEL_INTERNAL_PREFIXES:
            return True, "Kernel-internal loopback route"

        # Also suppress kernel host routes inside the loopback range
        if net.version == 4 and net.subnet_of(ip_network("127.0.0.0/8")):
            return True, "Kernel-internal loopback route"

        # Rule 3: Management-plane subnet routes
        for mgmt_net in _MGMT_SUBNETS:
            if net.version == 4 and (
                net == mgmt_net or net.subnet_of(mgmt_net)
            ):
                return True, f"Management-plane route (subnet of {mgmt_net})"

        # Rule 4: IPv6 link-local
        if net.version == 6 and net.subnet_of(_IPV6_LINK_LOCAL):
            return True, "IPv6 link-local infrastructure route"

        return False, ""

    def diff(self, suppress_noise: bool = True) -> list[Inconsistency]:
        """
        Compare all four planes. Return one Inconsistency per prefix that is
        not consistently present (or absent) across all planes.

        Parameters
        ----------
        suppress_noise : bool
            If True (default), filter out SAI-internal, management-plane,
            kernel-internal, and IPv6 link-local entries that are expected
            to be inconsistent in a SONiC-VS environment.
            Set to False to see all raw inconsistencies including noise.
        """
        planes = self._plane_map()

        all_prefixes: set[str] = set()
        for plane_routes in planes.values():
            all_prefixes.update(self._canonical(p) for p in plane_routes)

        inconsistencies: list[Inconsistency] = []

        for prefix in sorted(all_prefixes):
            present: set[str] = set()
            missing: set[str] = set()

            for plane_name, plane_routes in planes.items():
                canonical_keys = {self._canonical(k) for k in plane_routes}
                if prefix in canonical_keys:
                    present.add(plane_name)
                else:
                    missing.add(plane_name)

            # All planes agree — nothing to report
            if not missing or not present:
                continue

            severity, diagnosis = _classify(present, missing)

            # Nexthop mismatch check — exclude ASIC_DB (OIDs ≠ IPs)
            ip_planes = {p for p in present if p != "asic_db"}
            nexthop_mismatch: dict[str, list[str]] = {}
            if len(ip_planes) > 1:
                nexthop_sets: dict[str, frozenset[str]] = {}
                for p in ip_planes:
                    route = planes[p].get(prefix)
                    if route is None:
                        for k, v in planes[p].items():
                            if self._canonical(k) == prefix:
                                route = v
                                break
                    if route:
                        nexthop_sets[p] = frozenset(route.nexthops)

                unique_sets = set(nexthop_sets.values())
                if len(unique_sets) > 1:
                    for p, nhs in nexthop_sets.items():
                        nexthop_mismatch[p] = sorted(nhs)
                    if severity == "info":
                        severity = "warning"
                        diagnosis = (
                            f"Nexthop mismatch across planes for {prefix}: "
                            + "; ".join(
                                f"{p}={sorted(nhs)}"
                                for p, nhs in nexthop_sets.items()
                            )
                            + " — possible partial update or race condition."
                        )

            # Determine VRF from whichever plane has the route
            vrf = "default"
            for p in present:
                route = planes[p].get(prefix)
                if route is None:
                    for k, v in planes[p].items():
                        if self._canonical(k) == prefix:
                            route = v
                            break
                if route and route.vrf:
                    vrf = route.vrf
                    break

            # Apply noise suppression
            if suppress_noise:
                suppressed, reason = self._should_suppress(prefix, vrf, present)
                if suppressed:
                    continue

            inconsistencies.append(Inconsistency(
                prefix=prefix,
                vrf=vrf,
                present_in=sorted(present),
                missing_in=sorted(missing),
                nexthop_mismatch=nexthop_mismatch,
                severity=severity,
                diagnosis=diagnosis,
            ))

        return inconsistencies

    def summary(self) -> dict:
        """Return a brief summary dict for health checks and logging."""
        issues = self.diff()
        counts = {"critical": 0, "warning": 0, "info": 0}
        for i in issues:
            counts[i.severity] = counts.get(i.severity, 0) + 1
        return {
            "total_inconsistencies": len(issues),
            "by_severity": counts,
            "planes": {
                name: len(routes)
                for name, routes in self._plane_map().items()
            },
        }


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from .collector import RouteCollector

    collector = RouteCollector(host="127.0.0.1")
    snapshot = collector.collect()
    engine = DiffEngine(snapshot)

    print("=== Filtered (default) ===")
    issues = engine.diff()
    print(f"Found {len(issues)} inconsistencies\n")
    for issue in issues:
        print(f"[{issue.severity.upper():8}] {issue.prefix}")
        print(f"  present : {issue.present_in}")
        print(f"  missing : {issue.missing_in}")
        print(f"  diagnosis: {issue.diagnosis}")
        if issue.nexthop_mismatch:
            print(f"  nexthops: {issue.nexthop_mismatch}")
        print()

    print("=== Raw (unfiltered) ===")
    all_issues = engine.diff(suppress_noise=False)
    print(f"Found {len(all_issues)} total (including noise)\n")
