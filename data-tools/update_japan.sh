#!/usr/bin/env bash
# Weekly refresh for the J-Quants mirror, the xyz Japan perps and the pre-registered forward tracking
# (docs/japan-perp-forward.md): fetch_hl_xyz -> fetch_jquants sync -> build -> japan_forward status,
# then a backup of the raw files to a private GCS bucket (JQ_GCS_URI; empty disables it). Standard
# serves a rolling ten-year window, so months that leave it exist only in the local raw mirror.
#
#   data-tools/update_japan.sh           run if the last success is older than JQ_UPDATE_EVERY_DAYS (6)
#   data-tools/update_japan.sh --force   run now
#   data-tools/update_japan.sh --check   only print the route evidence and exit
#
# Before any network access it checks, read-only, that the host-managed VPN carries IPv4: the split
# routes 0.0.0.0/1 and 128.0.0.0/1 must share one device and every target host must route through
# it. It never starts, changes or works around a VPN; if the check fails it stops. Name resolution
# is forced to IPv4 (data-tools/ipv4_run.py) because IPv6 has a native route here.
set -euo pipefail
export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/sbin:/sbin:$HOME/google-cloud-sdk/bin"  # cron gives /usr/bin:/bin only
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${JQ_DATA_DIR:-/mnt/e/Datas/market/jquants}"
WORK="${JQ_WORK_DIR:-$HOME/.cache/jquants-build}"
EVERY_DAYS="${JQ_UPDATE_EVERY_DAYS:-6}"
HL_DIR="${JQ_HL_DIR:-/mnt/e/Datas/market/hyperliquid/xyz}"
GCS_URI="${JQ_GCS_URI-gs://crypto-bitflyer-418902-jquants}"
GCLOUD_CONFIG="${JQ_GCLOUD_CONFIG:-gonumb}"
HOSTS=(api.jquants.com api.hyperliquid.xyz)
GCS_HOSTS=(storage.googleapis.com oauth2.googleapis.com)
mkdir -p "$WORK" "$DATA/logs"
STAMP="$WORK/last-success"

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

backup() {
  [[ -n "$GCS_URI" ]] || return 0
  vpn_check "${GCS_HOSTS[@]}" || return 1
  gcloud --quiet --configuration="$GCLOUD_CONFIG" storage rsync "$DATA/raw" "$GCS_URI/jquants/raw" --recursive \
    --exclude='(^|/)\.sync\.lock$'
  gcloud --quiet --configuration="$GCLOUD_CONFIG" storage rsync "$HL_DIR" "$GCS_URI/hyperliquid/xyz" --recursive
}

if [[ "${1:-}" == "--check" ]]; then
  vpn_check "${HOSTS[@]}"
  [[ -z "$GCS_URI" ]] || vpn_check "${GCS_HOSTS[@]}"
  exit
fi
if [[ "${1:-}" != "--force" && -f "$STAMP" ]] && (( $(date +%s) - $(stat -c %Y "$STAMP") < EVERY_DAYS * 86400 )); then
  exit 0
fi

exec 9>"$WORK/.lock"
flock -n 9 || { echo "another update is running"; exit 0; }
LOG="$DATA/logs/update-$(date -u +%Y%m%dT%H%M%SZ).log"
exec >>"$LOG" 2>&1
echo "== $(date -u +%FT%TZ) update_japan.sh"
vpn_check "${HOSTS[@]}"
cd "$REPO"
python3 data-tools/ipv4_run.py data-tools/fetch_hl_xyz.py
python3 data-tools/ipv4_run.py data-tools/fetch_jquants.py sync --data-dir "$DATA"
python3 data-tools/fetch_jquants.py build --data-dir "$DATA" --work-dir "$WORK"
python3 backtests/japan_forward.py status --jquants-db "$DATA/jquants.db" >/dev/null
touch "$STAMP"
echo "== $(date -u +%FT%TZ) updated"
backup || echo "backup to $GCS_URI failed; the next weekly run retries it"
echo "== $(date -u +%FT%TZ) done"
