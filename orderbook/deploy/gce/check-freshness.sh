#!/usr/bin/env bash
# Report whether the collector is still delivering: the newest daily database in the bucket must be
# the last closed UTC day. Exit 0 when fresh, 1 when stale or empty, 2 on a usage or listing error.
# Usage: check-freshness.sh gs://bucket/prefix   (run where gcloud can read the bucket)
#
# The upload timer copies a closed day at 20 minutes past each hour, so until 01:00 UTC the day that
# just closed may not have arrived yet; before that time the day before it is still accepted.
set -euo pipefail
DEST=${1:?usage: check-freshness.sh gs://bucket/prefix}
GRACE_MINUTES=${GRACE_MINUTES:-60}
now=${NOW_EPOCH:-$(date -u +%s)}                 # NOW_EPOCH lets tests fix the clock
[[ "$now" =~ ^[0-9]+$ && "$GRACE_MINUTES" =~ ^[0-9]+$ ]] || { echo "NOW_EPOCH and GRACE_MINUTES must be integers" >&2; exit 2; }

listing=$(gcloud storage ls "$DEST/") || { echo "could not list $DEST" >&2; exit 2; }
latest=$(printf '%s\n' "$listing" | sed -n 's#.*/orderbook-\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\)\.db$#\1#p' | sort | tail -1)
expected=$(date -u -d "@$((now - GRACE_MINUTES * 60 - 86400))" +%F)   # last closed day, allowing the upload lag
if [[ -z "$latest" ]]; then
  echo "stale: no daily database under $DEST (expected $expected)"
  exit 1
fi
if [[ "$latest" < "$expected" ]]; then
  behind=$(( ( $(date -u -d "$expected" +%s) - $(date -u -d "$latest" +%s) ) / 86400 ))
  echo "stale: newest daily database is $latest, expected $expected ($behind day(s) missing)"
  exit 1
fi
echo "fresh: newest daily database is $latest"
