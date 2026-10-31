#!/usr/bin/env bash
# Weekly refresh for the xyz Japan perps, the J-Quants mirror and the pre-registered forward tracking
# (docs/japan-perp-forward.md), then a backup to a private GCS bucket (JQ_GCS_URI; empty disables it).
#
#   data-tools/update_japan.sh           run each part whose last success is JQ_UPDATE_EVERY_DAYS (6) old
#   data-tools/update_japan.sh --force   run every part now
#   data-tools/update_japan.sh --check   only print the route evidence and exit
#
# The parts are independent. xyz: fetch_hl_xyz (its 15-minute candles reach back only ~52 days, so it
# keeps running while J-Quants is unavailable). J-Quants: sync -> build -> japan_forward status. When
# J-Quants refuses a dataset (sync exit 3: the subscription ended or the plan changed) nothing local
# changes; the date goes to $WORK/jquants-access-denied, the log repeats the licence duty (after a
# cancellation every J-Quants copy must be deleted: data-tools/purge_jquants.sh) and the next week
# checks again. After a renewal the sync fetches every file missing locally, so the gap is refilled.
# If $WORK/jquants-cancel-requested exists (the user submitted the cancellation), the first refusal
# also runs purge_jquants.sh --yes, so the data goes when the paid period ends; a failed purge is
# retried next hour. Remove the marker if the cancellation is undone; regained access removes it.
# Network access waits for the read-only VPN route check in data-tools/vpn_route.sh; resolution is IPv4.
set -euo pipefail
export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/sbin:/sbin:$HOME/google-cloud-sdk/bin"  # cron gives /usr/bin:/bin only
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${JQ_DATA_DIR:-/mnt/e/Datas/market/jquants}"
WORK="${JQ_WORK_DIR:-$HOME/.cache/jquants-build}"
LOGS="${JQ_LOG_DIR:-$WORK/logs}"
EVERY_DAYS="${JQ_UPDATE_EVERY_DAYS:-6}"
HL_DIR="${JQ_HL_DIR:-/mnt/e/Datas/market/hyperliquid/xyz}"
GCS_URI="${JQ_GCS_URI-gs://crypto-bitflyer-418902-jquants}"
GCLOUD_CONFIG="${JQ_GCLOUD_CONFIG:-gonumb}"
HOSTS=(api.jquants.com api.hyperliquid.xyz)
GCS_HOSTS=(storage.googleapis.com oauth2.googleapis.com)
HL_STAMP="$WORK/hl-last-success"
JQ_STAMP="$WORK/jquants-last-success"
DENIED="$WORK/jquants-access-denied"
CANCEL="$WORK/jquants-cancel-requested"
PURGED="$WORK/jquants-purged"
# shellcheck source=vpn_route.sh
source "$REPO/data-tools/vpn_route.sh"
mkdir -p "$WORK" "$LOGS"
if [[ -f "$WORK/last-success" ]]; then  # single stamp of the first version
  cp -p "$WORK/last-success" "$HL_STAMP" && mv "$WORK/last-success" "$JQ_STAMP"
fi

FORCE=0
case "${1:-}" in
  --check)
    vpn_check "${HOSTS[@]}"
    [[ -z "$GCS_URI" ]] || vpn_check "${GCS_HOSTS[@]}"
    exit ;;
  --force) FORCE=1 ;;
  "") ;;
  *) echo "usage: $0 [--force|--check]"; exit 2 ;;
esac
due() { [[ "$FORCE" == 1 || ! -f "$1" ]] || (( $(date +%s) - $(stat -c %Y "$1") >= EVERY_DAYS * 86400 )); }
due "$HL_STAMP" || due "$JQ_STAMP" || exit 0

exec 9>"$WORK/.lock"
flock -n 9 || { echo "another update is running"; exit 0; }
exec >>"$LOGS/update-$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1
echo "== $(date -u +%FT%TZ) update_japan.sh"
vpn_check "${HOSTS[@]}"
cd "$REPO"
hl_ok=0
jq_ok=0

if due "$HL_STAMP"; then
  if python3 data-tools/ipv4_run.py data-tools/fetch_hl_xyz.py --base "$HL_DIR"; then
    touch "$HL_STAMP"
    hl_ok=1
  else
    echo "xyz fetch failed; it is retried next hour"
  fi
fi

if due "$JQ_STAMP"; then
  rc=0
  python3 data-tools/ipv4_run.py data-tools/fetch_jquants.py sync --data-dir "$DATA" || rc=$?
  case "$rc" in
    0)
      if [[ -f "$DENIED" ]]; then
        echo "J-Quants access is back (refused since $(cat "$DENIED")); every missing file was fetched"
        rm -f "$DENIED" "$PURGED" "$CANCEL"
      fi
      python3 data-tools/fetch_jquants.py build --data-dir "$DATA" --work-dir "$WORK"
      python3 backtests/japan_forward.py status --jquants-db "$DATA/jquants.db" >/dev/null
      touch "$JQ_STAMP"
      jq_ok=1 ;;
    3)
      [[ -f "$DENIED" ]] || date -u +%F >"$DENIED"
      echo "NOTICE: J-Quants refuses access since $(cat "$DENIED"); nothing local was changed."
      echo "NOTICE: if the subscription has ended, the J-Quants licence requires deleting every acquired copy:"
      echo "NOTICE:   data-tools/purge_jquants.sh (dry run), then data-tools/purge_jquants.sh --yes"
      echo "NOTICE: access is checked again next week; after a renewal the sync refetches everything missing."
      purged=1
      if [[ -f "$CANCEL" && ! -f "$PURGED" ]]; then
        echo "NOTICE: cancellation requested on $(head -1 "$CANCEL"): deleting the J-Quants data now"
        JQ_DATA_DIR="$DATA" JQ_WORK_DIR="$WORK" JQ_GCS_URI="$GCS_URI" JQ_GCLOUD_CONFIG="$GCLOUD_CONFIG" \
          "$REPO/data-tools/purge_jquants.sh" --yes || purged=0
      fi
      if [[ "$purged" == 1 ]]; then
        touch "$JQ_STAMP"
      else
        echo "purge failed; it is retried next hour"  # JQ_STAMP stays old, so the part stays due
      fi ;;
    *)
      echo "J-Quants sync failed (exit $rc); it is retried next hour" ;;
  esac
fi

backup() {
  [[ -n "$GCS_URI" ]] || return 0
  vpn_check "${GCS_HOSTS[@]}" || return 1
  if [[ "$jq_ok" == 1 ]]; then  # never while access is refused: the copy must not outlive the licence
    gcloud --quiet --configuration="$GCLOUD_CONFIG" storage rsync "$DATA/raw" "$GCS_URI/jquants/raw" --recursive \
      --exclude='(^|/)\.sync\.lock$'
  fi
  if [[ "$hl_ok" == 1 ]]; then
    gcloud --quiet --configuration="$GCLOUD_CONFIG" storage rsync "$HL_DIR" "$GCS_URI/hyperliquid/xyz" --recursive
  fi
}
if [[ "$jq_ok$hl_ok" != "00" ]]; then
  backup || echo "backup to $GCS_URI failed; the next weekly run retries it"
fi
echo "== $(date -u +%FT%TZ) done"
