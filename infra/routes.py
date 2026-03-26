"""
routes.py — ExaBGP process API: initial route advertisements.

This script is run by ExaBGP as a process. It advertises the initial set
of test prefixes on startup, then blocks (waiting for signals from
fault_inject.py via the ExaBGP stdin pipe).

ExaBGP reads announcements from this process's stdout.
"""

import sys
import time

NEXTHOP = "192.168.10.1"

TEST_PREFIXES = [
    "10.100.0.0/24",
    "10.101.0.0/24",
    "10.102.0.0/24",
    "10.200.0.0/22",
]


def announce(prefix: str) -> None:
    print(f"announce route {prefix} next-hop {NEXTHOP}", flush=True)


def withdraw(prefix: str) -> None:
    print(f"withdraw route {prefix} next-hop {NEXTHOP}", flush=True)


if __name__ == "__main__":
    # Wait for ExaBGP to establish the BGP session before advertising
    time.sleep(5)

    for prefix in TEST_PREFIXES:
        announce(prefix)

    # Keep the process alive; ExaBGP sends commands via stdin
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("withdraw "):
            prefix = line.split()[1]
            withdraw(prefix)
        elif line.startswith("announce "):
            prefix = line.split()[1]
            announce(prefix)
