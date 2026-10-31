#!/usr/bin/env bash
# Delete every J-Quants copy this repository's tools keep, as J-Quants requires after a cancellation
# (official help: the acquired data, its copies and derivatives that can restore it must be deleted;
# derivatives that cannot restore it, such as aggregate statistics, may stay if not published).
#
#   data-tools/purge_jquants.sh         dry run: list what would be deleted (GCS listing needs the VPN check)
#   data-tools/purge_jquants.sh --yes   delete
#
# Deleted: JQ_DATA_DIR (raw mirror, jquants.db, results with J-Quants-derived values), unfinished build
# files in JQ_WORK_DIR, and every version of the jquants/ prefix in the GCS backup (soft delete is cleared
# first, so nothing is kept for recovery). Kept: the Hyperliquid data (not J-Quants), code, docs and the
# forward-test registry (rules only). Other projects' J-Quants data is listed, not touched.
# Renewal: put the API key in .env and run data-tools/update_japan.sh --force; the sync refetches all.
set -euo pipefail
export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/sbin:/sbin:$HOME/google-cloud-sdk/bin"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${JQ_DATA_DIR:-/mnt/e/Datas/market/jquants}"
WORK="${JQ_WORK_DIR:-$HOME/.cache/jquants-build}"
GCS_URI="${JQ_GCS_URI-gs://crypto-bitflyer-418902-jquants}"
GCLOUD_CONFIG="${JQ_GCLOUD_CONFIG:-gonumb}"
OTHER=("$HOME/workspace/stocks/research/data/jquants")  # J-Quants data kept by other projects
# shellcheck source=vpn_route.sh
source "$REPO/data-tools/vpn_route.sh"

YES=0
case "${1:-}" in
  --yes) YES=1 ;;
  "") ;;
  *) echo "usage: $0 [--yes]"; exit 2 ;;
esac
resolved="$(realpath -m "$DATA")"
if [[ "$(basename "$resolved")" != "jquants" || "$resolved/" == "$REPO/"* || "$REPO/" == "$resolved/"* ]]; then
  echo "refusing: JQ_DATA_DIR must be a directory named jquants outside the repository ($resolved)"
  exit 2
fi

echo "== local (deleted)"
if [[ -e "$resolved" ]]; then du -sh "$resolved"; else echo "$resolved: absent"; fi
shopt -s nullglob
leftovers=("$WORK"/.jquants.db.tmp-* "$WORK"/.jquants-stage.tmp-*)
for f in "${leftovers[@]}"; do du -sh "$f"; done

gcs_objects=0
if [[ -n "$GCS_URI" ]]; then
  echo "== GCS (deleted, every version): $GCS_URI/jquants/"
  vpn_check storage.googleapis.com oauth2.googleapis.com
  gcs_objects=$(gcloud --quiet --configuration="$GCLOUD_CONFIG" storage ls --all-versions -r "$GCS_URI/jquants/**" 2>/dev/null \
                | grep -c '^gs://' || true)
  echo "$gcs_objects object versions"
fi

echo "== other J-Quants data on this machine (not touched here; delete it in its own project)"
for d in "${OTHER[@]}"; do
  if [[ -e "$d" ]]; then du -sh "$d"; fi
done

if [[ "$YES" != 1 ]]; then
  echo "dry run: nothing deleted. Run with --yes to delete the items above."
  exit 0
fi
rm -rf -- "$resolved"
for f in "${leftovers[@]}"; do rm -f -- "$f"; done
if [[ -n "$GCS_URI" && "$gcs_objects" -gt 0 ]]; then
  bucket="gs://$(cut -d/ -f3 <<<"$GCS_URI")"
  gcloud --quiet --configuration="$GCLOUD_CONFIG" storage buckets update "$bucket" --clear-soft-delete
  gcloud --quiet --configuration="$GCLOUD_CONFIG" storage rm -r --all-versions "$GCS_URI/jquants/"
fi
date -u +%FT%TZ >"$WORK/jquants-purged"
echo "== deleted. Remaining J-Quants data elsewhere must be removed in its project (listed above)."
