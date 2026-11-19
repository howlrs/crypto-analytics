#!/usr/bin/env bash
# Upload each closed daily database (UTC days before today) once, as a consistent copy, then free
# local disk: a database older than KEEP_DAYS is removed only after its object is confirmed in the bucket.
# Usage: upload-closed.sh gs://bucket/prefix   (run as the collector user)
set -euo pipefail
DEST=${1:?usage: upload-closed.sh gs://bucket/prefix}
DATA=${DATA:-/var/lib/orderbook/data}
STAGE=${STAGE:-/var/lib/orderbook/upload}
KEEP_DAYS=${KEEP_DAYS:-14}                     # closed days kept on local disk after upload
[[ "$KEEP_DAYS" =~ ^[1-9][0-9]*$ ]] || { echo "KEEP_DAYS must be a positive integer: $KEEP_DAYS" >&2; exit 2; }
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

# Without this the disk fills, the collector stops at its free-space floor and nothing restarts it.
# The bucket copy is the durable one (the service account cannot delete or overwrite objects there),
# so a local file goes only when that copy is confirmed to exist with a non-zero size.
cutoff=$(date -u -d "$today - $KEEP_DAYS days" +%F)
for db in "$DATA"/orderbook-????-??-??.db; do
  [ -e "$db" ] || continue
  name=$(basename "$db")
  day=${name#orderbook-}
  day=${day%.db}
  [[ "$day" < "$cutoff" ]] || continue
  [ -e "$STAGE/$name.uploaded" ] || continue
  size=$(gcloud storage objects describe "$DEST/$name" --format='value(size)' 2>/dev/null || true)
  if [[ "$size" =~ ^[1-9][0-9]*$ ]]; then
    rm -f "$db" "$db-wal" "$db-shm"
    echo "pruned $name (bucket copy: $size bytes)"
  else
    echo "kept $name: no confirmed copy at $DEST/$name" >&2
  fi
done
