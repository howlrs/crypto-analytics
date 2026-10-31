# Sourced by update_japan.sh and purge_jquants.sh. Read-only check that the host-managed VPN carries
# IPv4: the split routes 0.0.0.0/1 and 128.0.0.0/1 must share one device, every host must route through
# it, and no host may resolve to IPv6 (IPv6 has a native route on this WSL host). It never starts,
# changes or works around a VPN.

vpn_check() {  # vpn_check HOST...
  local dev_lo dev_hi host ip dev
  dev_lo=$(ip -4 route show 0.0.0.0/1 | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | head -1)
  dev_hi=$(ip -4 route show 128.0.0.0/1 | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | head -1)
  if [[ -z "$dev_lo" || "$dev_lo" != "$dev_hi" ]]; then
    echo "route check failed: no VPN split routes on one device (0/1: '${dev_lo}', 128/1: '${dev_hi}')"
    return 1
  fi
  echo "split routes 0.0.0.0/1 and 128.0.0.0/1 via $dev_lo ($(ip -4 -brief addr show "$dev_lo" | awk '{print $3}'))"
  for host in "$@"; do
    if getent ahostsv6 "$host" | awk '{print $1}' | grep -q -v '^::ffff:'; then
      echo "route check failed: $host has IPv6 addresses, which do not use the VPN here"
      return 1
    fi
    ip=$(getent ahostsv4 "$host" | awk 'NR == 1 {print $1}')
    [[ -n "$ip" ]] || { echo "route check failed: $host does not resolve"; return 1; }
    dev=$(ip -4 route get "$ip" | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | head -1)
    if [[ "$dev" != "$dev_lo" ]]; then
      echo "route check failed: $host ($ip) leaves via '$dev', not $dev_lo"
      return 1
    fi
    echo "$host -> $ip via $dev"
  done
}
