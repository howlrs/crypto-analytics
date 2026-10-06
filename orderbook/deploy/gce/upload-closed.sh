#!/usr/bin/env bash
# Upload each closed daily database (UTC days before today) once, as a consistent copy.
# Usage: upload-closed.sh gs://bucket/prefix   (run as the collector user)
set -euo pipefail
DEST=${1:?usage: upload-closed.sh gs://bucket/prefix}
DATA=${DATA:-/var/lib/orderbook/data}
STAGE=${STAGE:-/var/lib/orderbook/upload}
cd "$(dirname "$0")/../../.."                 # repository root, for python3 -m orderbook.collect
today=$(date -u +%F)
for db in "$DATA"/orderbook-????-??-??.db; do
  [ -e "$db" ] || continue
  name=$(basename "$db")
  day=${name#orderbook-}
  day=${day%.db}
  [[ "$day" < "$today" ]] || continue          # the collector never writes a past UTC day again
  [ -e "$STAGE/$name.uploaded" ] && continue
  rm -f "$STAGE/$name" "$STAGE/$name.partial"
  python3 -m orderbook.collect backup --db "$db" --output "$STAGE/$name"
  gcloud storage cp --no-clobber "$STAGE/$name" "$DEST/$name"
  rm -f "$STAGE/$name"
  touch "$STAGE/$name.uploaded"
done
