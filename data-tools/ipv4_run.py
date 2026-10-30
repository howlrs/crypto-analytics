#!/usr/bin/env python3
"""Run a Python script with name resolution restricted to IPv4.

On this WSL host the VPN carries IPv4 only (split routes 0.0.0.0/1 and 128.0.0.0/1) while IPv6 has
a native default route, so a host with an AAAA record would bypass the VPN. Usage:
  python3 data-tools/ipv4_run.py data-tools/fetch_jquants.py sync
"""
import runpy
import socket
import sys

_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    socket.getaddrinfo = _ipv4_only
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
