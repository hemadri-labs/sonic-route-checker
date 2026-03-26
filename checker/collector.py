"""
collector.py — SONiC route consistency checker: data collection layer

Pulls route tables from four planes and normalizes them into RouteEntry objects:
  1. APP_DB   (Redis DB 0) — what orchagent received from FRR via fpmsyncd
  2. ASIC_DB  (Redis DB 1) — what SAI programmed into the ASIC
  3. FRR/vtysh             — the RIB as seen by the routing daemon
  4. Kernel FIB            — what the Linux kernel has via `ip route`

Usage (on-box or via SSH tunnel):
    collector = RouteCollector(host="127.0.0.1")
    snapshot = collector.collect()
    print(snapshot.app_db)   # dict[prefix -> RouteEntry]
    print(snapshot.asic_db)
    print(snapshot.frr)
    print(snapshot.kernel)
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from ipaddress import ip_network
from typing import Optional

import redis


# ---------------------------------------------------------------------------
# Redis DB indices (SONiC convention)
# ---------------------------------------------------------------------------
APPL_DB_ID  = 0
ASIC_DB_ID  = 1
STATE_DB_ID = 6

# APP_DB key pattern for IPv4/IPv6 routes
ROUTE_TABLE_PATTERN_V4 = "ROUTE_TABLE:*.*.*.*/*"
ROUTE_TABLE_PATTERN_V6 = "ROUTE_TABLE:*:*/*"

# ASIC_DB key pattern for SAI route entries
ASIC_ROUTE_PATTERN = "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:*"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RouteEntry:
    """Normalized route entry — source-agnostic."""
    prefix: str                         # canonical CIDR, e.g. "10.1.0.0/24"
    nexthops: list[str] = field(default_factory=list)   # list of NH IPs or OIDs
    interfaces: list[str] = field(default_factory=list) # egress interface names
    protocol: Optional[str] = None      # "bgp", "ospf", "static", "kernel", etc.
    metric: Optional[int] = None
    vrf: str = "default"
    raw: dict = field(default_factory=dict, repr=False)  # original key/value

    def __hash__(self):
        return hash((self.prefix, self.vrf))

    def __eq__(self, other):
        return self.prefix == other.prefix and self.vrf == other.vrf


@dataclass
class RouteSnapshot:
    """One point-in-time collection from all four planes."""
    timestamp: float
    app_db:  dict[str, RouteEntry] = field(default_factory=dict)
    asic_db: dict[str, RouteEntry] = field(default_factory=dict)
    frr:     dict[str, RouteEntry] = field(default_factory=dict)
    kernel:  dict[str, RouteEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_prefix(prefix: str) -> str:
    """Canonicalize a prefix string — removes host bits, normalizes notation."""
    try:
        return str(ip_network(prefix, strict=False))
    except ValueError:
        return prefix  # pass through unparseable entries for the diff engine to flag


def _parse_nexthops(nh_string: str) -> list[str]:
    """
    APP_DB stores nexthops as comma-separated IPs or empty string for
    directly connected / blackhole routes.
    """
    if not nh_string:
        return []
    return [nh.strip() for nh in nh_string.split(",") if nh.strip()]


# ---------------------------------------------------------------------------
# APP_DB collector  (Redis DB 0)
# ---------------------------------------------------------------------------

class AppDbCollector:
    """
    Reads ROUTE_TABLE from APP_DB.

    Key format:   ROUTE_TABLE:<prefix>
    Value fields: nexthop, ifname, weight, blackhole, protocol, vrf
    """

    def __init__(self, r: redis.Redis):
        self._r = r

    def collect(self, vrf: str = "default") -> dict[str, RouteEntry]:
        routes: dict[str, RouteEntry] = {}

        # Scan both IPv4 and IPv6 patterns
        patterns = [ROUTE_TABLE_PATTERN_V4, ROUTE_TABLE_PATTERN_V6]
        # VRF routes live under ROUTE_TABLE:<vrf>:<prefix> — add if needed
        if vrf != "default":
            patterns = [f"ROUTE_TABLE:{vrf}:*"]

        keys: list[bytes] = []
        for pattern in patterns:
            # Use SCAN instead of KEYS to avoid blocking the Redis server
            cursor = 0
            while True:
                cursor, batch = self._r.scan(cursor, match=pattern, count=500)
                keys.extend(batch)
                if cursor == 0:
                    break

        pipeline = self._r.pipeline(transaction=False)
        for key in keys:
            pipeline.hgetall(key)
        values = pipeline.execute()

        for key, fields in zip(keys, values):
            key_str = key.decode()
            # Strip "ROUTE_TABLE:" prefix
            prefix_raw = key_str.split(":", 1)[1]
            # Handle VRF prefix format "ROUTE_TABLE:<vrf>:<prefix>"
            if vrf != "default" and ":" in prefix_raw:
                prefix_raw = prefix_raw.split(":", 1)[1]

            prefix = _normalize_prefix(prefix_raw)
            decoded = {k.decode(): v.decode() for k, v in fields.items()}

            entry = RouteEntry(
                prefix=prefix,
                nexthops=_parse_nexthops(decoded.get("nexthop", "")),
                interfaces=_parse_nexthops(decoded.get("ifname", "")),
                protocol=decoded.get("protocol"),
                vrf=vrf,
                raw=decoded,
            )
            routes[prefix] = entry

        return routes


# ---------------------------------------------------------------------------
# ASIC_DB collector  (Redis DB 1)
# ---------------------------------------------------------------------------

class AsicDbCollector:
    """
    Reads SAI_OBJECT_TYPE_ROUTE_ENTRY from ASIC_DB.

    Key format (JSON-encoded):
        ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:
            {"dest":"10.1.0.0/24","switch_id":"oid:0x21000000000000","vr_id":"oid:0x3000000000022"}

    Value fields include:
        SAI_ROUTE_ENTRY_ATTR_NEXT_HOP_ID     — OID of nexthop or nexthop group
        SAI_ROUTE_ENTRY_ATTR_PACKET_ACTION   — FORWARD, DROP, TRAP
        SAI_ROUTE_ENTRY_ATTR_META_DATA       — optional metadata
    """

    # These prefixes are SAI internal (CPU, drop, loopback) — skip them
    _SKIP_ACTIONS = {"SAI_PACKET_ACTION_DROP", "SAI_PACKET_ACTION_TRAP"}

    def __init__(self, r: redis.Redis):
        self._r = r

    def collect(self) -> dict[str, RouteEntry]:
        routes: dict[str, RouteEntry] = {}

        cursor = 0
        keys: list[bytes] = []
        while True:
            cursor, batch = self._r.scan(cursor, match=ASIC_ROUTE_PATTERN, count=500)
            keys.extend(batch)
            if cursor == 0:
                break

        if not keys:
            return routes

        pipeline = self._r.pipeline(transaction=False)
        for key in keys:
            pipeline.hgetall(key)
        values = pipeline.execute()

        for key, fields in zip(keys, values):
            key_str = key.decode()
            # Extract the JSON body after the second ":"
            # ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{...}
            try:
                json_part = key_str.split(":", 2)[2]
                meta = json.loads(json_part)
            except (IndexError, json.JSONDecodeError):
                continue

            prefix_raw = meta.get("dest", "")
            if not prefix_raw:
                continue

            decoded = {k.decode(): v.decode() for k, v in fields.items()}
            action = decoded.get("SAI_ROUTE_ENTRY_ATTR_PACKET_ACTION", "SAI_PACKET_ACTION_FORWARD")

            if action in self._SKIP_ACTIONS:
                continue

            prefix = _normalize_prefix(prefix_raw)
            nh_oid = decoded.get("SAI_ROUTE_ENTRY_ATTR_NEXT_HOP_ID", "")

            # VR OID maps to a VRF — for the demo we keep the raw OID
            vr_oid = meta.get("vr", meta.get("vr_id", "default"))

            entry = RouteEntry(
                prefix=prefix,
                nexthops=[nh_oid] if nh_oid else [],
                vrf=vr_oid,
                raw=decoded,
            )
            routes[prefix] = entry

        return routes


# ---------------------------------------------------------------------------
# FRR / vtysh collector
# ---------------------------------------------------------------------------

class FrrCollector:
    """
    Queries FRR for the RIB via vtysh.

    Runs: vtysh -c "show ip route json"
          vtysh -c "show ipv6 route json"

    Can run locally (on-box) or via paramiko SSH for remote switches.
    """

    def __init__(self, ssh_client=None):
        """
        ssh_client: a connected paramiko SSHClient, or None to run locally.
        """
        self._ssh = ssh_client

    def _run(self, cmd: str) -> str:
        if self._ssh:
            _, stdout, _ = self._ssh.exec_command(cmd)
            return stdout.read().decode()
        else:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            return result.stdout

    def _parse_vtysh_json(self, json_str: str, family: str) -> dict[str, RouteEntry]:
        routes: dict[str, RouteEntry] = {}
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return routes

        # vtysh JSON format:
        # { "prefix": [ { "protocol": "bgp", "nexthops": [...], ... }, ... ] }
        for prefix_raw, entries in data.items():
            prefix = _normalize_prefix(prefix_raw)
            for e in entries:
                if not e.get("selected", False) and not e.get("installed", False):
                    # Only take the best/installed route
                    continue

                nexthops = []
                interfaces = []
                for nh in e.get("nexthops", []):
                    ip = nh.get("ip") or nh.get("gateway")
                    if ip:
                        nexthops.append(ip)
                    iface = nh.get("interfaceName")
                    if iface:
                        interfaces.append(iface)

                entry = RouteEntry(
                    prefix=prefix,
                    nexthops=nexthops,
                    interfaces=interfaces,
                    protocol=e.get("protocol"),
                    metric=e.get("metric"),
                    vrf=e.get("vrfName", "default"),
                    raw=e,
                )
                routes[prefix] = entry
                break  # first selected entry wins

        return routes

    def collect(self) -> dict[str, RouteEntry]:
        routes: dict[str, RouteEntry] = {}

        for family, cmd in [
            ("v4", "vtysh -c 'show ip route json'"),
            ("v6", "vtysh -c 'show ipv6 route json'"),
        ]:
            output = self._run(cmd)
            routes.update(self._parse_vtysh_json(output, family))

        return routes


# ---------------------------------------------------------------------------
# Kernel FIB collector  (via `ip route` / pyroute2)
# ---------------------------------------------------------------------------

class KernelFibCollector:
    """
    Reads the kernel FIB.

    Prefers pyroute2 (netlink, no subprocess) but falls back to
    parsing `ip route show` output if pyroute2 is not installed.
    """

    def __init__(self, ssh_client=None):
        self._ssh = ssh_client

    def _collect_via_pyroute2(self) -> dict[str, RouteEntry]:
        """On-box only — uses netlink via pyroute2."""
        from pyroute2 import IPRoute  # type: ignore
        routes: dict[str, RouteEntry] = {}

        with IPRoute() as ipr:
            for msg in ipr.get_routes(family=2):   # AF_INET
                dst_attr = msg.get_attr("RTA_DST")
                dst_len  = msg["dst_len"]
                if dst_attr is None:
                    continue
                prefix = _normalize_prefix(f"{dst_attr}/{dst_len}")
                nh_attr = msg.get_attr("RTA_GATEWAY")
                oif_idx = msg.get_attr("RTA_OIF")
                iface   = ipr.get_links(oif_idx)[0].get_attr("IFLA_IFNAME") if oif_idx else ""
                proto_id = msg["proto"]
                proto_map = {1: "kernel", 2: "kernel", 4: "static",
                             9: "ospf", 11: "ospf", 186: "bgp", 187: "isis"}
                proto = proto_map.get(proto_id, str(proto_id))

                routes[prefix] = RouteEntry(
                    prefix=prefix,
                    nexthops=[nh_attr] if nh_attr else [],
                    interfaces=[iface] if iface else [],
                    protocol=proto,
                )

        return routes

    def _collect_via_iproute(self) -> dict[str, RouteEntry]:
        """Remote or fallback — parses text output of `ip route show`."""
        routes: dict[str, RouteEntry] = {}

        if self._ssh:
            _, stdout, _ = self._ssh.exec_command("ip route show")
            output = stdout.read().decode()
        else:
            result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
            output = result.stdout

        # Example line:
        #   10.1.0.0/24 via 192.168.1.1 dev eth0 proto bgp metric 20
        #   10.2.0.0/24 dev eth1 proto kernel scope link src 10.2.0.1
        line_re = re.compile(
            r"^(?P<prefix>\S+)"
            r"(?:.*\bvia\s+(?P<nh>\S+))?"
            r"(?:.*\bdev\s+(?P<dev>\S+))?"
            r"(?:.*\bproto\s+(?P<proto>\S+))?"
            r"(?:.*\bmetric\s+(?P<metric>\d+))?"
        )

        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("default"):
                continue
            m = line_re.match(line)
            if not m:
                continue
            prefix = _normalize_prefix(m.group("prefix"))
            nh    = m.group("nh") or ""
            dev   = m.group("dev") or ""
            proto = m.group("proto") or "kernel"
            metric_str = m.group("metric")
            metric = int(metric_str) if metric_str else None

            routes[prefix] = RouteEntry(
                prefix=prefix,
                nexthops=[nh] if nh else [],
                interfaces=[dev] if dev else [],
                protocol=proto,
                metric=metric,
            )

        return routes

    def collect(self) -> dict[str, RouteEntry]:
        if self._ssh:
            # Remote — must use subprocess text method
            return self._collect_via_iproute()
        try:
            return self._collect_via_pyroute2()
        except ImportError:
            return self._collect_via_iproute()


# ---------------------------------------------------------------------------
# Top-level collector
# ---------------------------------------------------------------------------

class RouteCollector:
    """
    Orchestrates collection from all four planes and returns a RouteSnapshot.

    Parameters
    ----------
    host      : Redis host (default 127.0.0.1 — on-box SONiC)
    redis_port: Redis port (default 6379)
    ssh_client: Optional paramiko SSHClient for remote collection
    vrf       : VRF name to collect (default "default")

    Example — on-box
    ----------------
    collector = RouteCollector()
    snapshot  = collector.collect()

    Example — remote via SSH
    ------------------------
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.168.1.1", username="admin", password="YourPassword")
    collector = RouteCollector(host="127.0.0.1", ssh_client=ssh)
    snapshot  = collector.collect()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        redis_port: int = 6379,
        ssh_client=None,
        vrf: str = "default",
    ):
        self._host = host
        self._port = redis_port
        self._ssh  = ssh_client
        self._vrf  = vrf

        # If using SSH, tunnel Redis over the connection
        # For production, use an SSH tunnel or SONiC's gRPC telemetry instead
        self._app_redis  = redis.Redis(host=host, port=redis_port, db=APPL_DB_ID,
                                       socket_timeout=5, decode_responses=False)
        self._asic_redis = redis.Redis(host=host, port=redis_port, db=ASIC_DB_ID,
                                       socket_timeout=5, decode_responses=False)

    def collect(self) -> RouteSnapshot:
        import time
        ts = time.time()

        app_db  = AppDbCollector(self._app_redis).collect(vrf=self._vrf)
        asic_db = AsicDbCollector(self._asic_redis).collect()
        frr     = FrrCollector(ssh_client=self._ssh).collect()
        kernel  = KernelFibCollector(ssh_client=self._ssh).collect()

        return RouteSnapshot(
            timestamp=ts,
            app_db=app_db,
            asic_db=asic_db,
            frr=frr,
            kernel=kernel,
        )

    def subscribe_changes(self, callback):
        """
        Subscribe to APP_DB keyspace notifications for real-time route changes.
        Calls callback(event_type, prefix) on each change.

        Requires Redis keyspace notifications to be enabled on the SONiC box:
            redis-cli config set notify-keyspace-events KEA
        """
        pubsub = self._app_redis.pubsub()
        pubsub.psubscribe("__keyevent@0__:hset", "__keyevent@0__:del")

        for message in pubsub.listen():
            if message["type"] not in ("pmessage", "message"):
                continue
            data = message.get("data", b"")
            if isinstance(data, bytes):
                data = data.decode()
            if data.startswith("ROUTE_TABLE:"):
                prefix_raw = data.split(":", 1)[1]
                prefix = _normalize_prefix(prefix_raw)
                event  = message["channel"].decode().split(":")[-1]  # hset or del
                callback(event, prefix)


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    collector = RouteCollector(host="127.0.0.1")

    print("Collecting routes from all planes...")
    snap = collector.collect()

    print(f"\n{'='*60}")
    print(f"Snapshot at {time.ctime(snap.timestamp)}")
    print(f"{'='*60}")
    print(f"  APP_DB  : {len(snap.app_db):>5} routes")
    print(f"  ASIC_DB : {len(snap.asic_db):>5} routes")
    print(f"  FRR     : {len(snap.frr):>5} routes")
    print(f"  Kernel  : {len(snap.kernel):>5} routes")

    # Show any prefix present in APP_DB but missing from ASIC_DB
    missing_in_asic = set(snap.app_db.keys()) - set(snap.asic_db.keys())
    if missing_in_asic:
        print(f"\n[!] Routes in APP_DB but NOT in ASIC_DB ({len(missing_in_asic)}):")
        for p in sorted(missing_in_asic)[:10]:
            print(f"     {p}  ->  {snap.app_db[p].nexthops}")
    else:
        print("\n[OK] APP_DB and ASIC_DB are in sync.")
