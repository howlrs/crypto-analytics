#!/usr/bin/env bash
# Write one health record of the order-book collector to Cloud Logging (log "orderbook-heartbeat").
#
# status is "ok" only when all of these hold:
#   - the collector service is active,
#   - the newest snapshot is at most MAX_AGE_SEC old,
#   - at least EXPECTED_STREAMS streams saved a snapshot within MAX_AGE_SEC,
#   - no closed UTC day is still waiting for upload UPLOAD_GRACE_MIN after it closed.
# The alert from alerting.sh fires when no "ok" record arrives for a while, so a stopped VM, a stopped
# service, a stalled collector, a missing stream and a stalled upload all end in the same email.
#
# Usage: heartbeat.sh   (run as the collector user from a systemd timer; DRY_RUN=1 prints without writing)
set -uo pipefail
DATA=${DATA:-/var/lib/orderbook/data}
STAGE=${STAGE:-/var/lib/orderbook/upload}
CODE_LINK=${CODE_LINK:-/opt/orderbook/current}
MAX_AGE_SEC=${MAX_AGE_SEC:-300}
EXPECTED_STREAMS=${EXPECTED_STREAMS:-11}
UPLOAD_GRACE_MIN=${UPLOAD_GRACE_MIN:-90}
LOG_NAME=${LOG_NAME:-orderbook-heartbeat}
now=${NOW_EPOCH:-$(date -u +%s)}                 # NOW_EPOCH lets tests fix the clock
for value in "$now" "$MAX_AGE_SEC" "$EXPECTED_STREAMS" "$UPLOAD_GRACE_MIN"; do
  [[ "$value" =~ ^[0-9]+$ ]] || { echo "numeric settings must be non-negative integers: $value" >&2; exit 2; }
done

service=$(systemctl is-active orderbook-collector.service 2>/dev/null)
service=${service:-unknown}

# Newest snapshot age and the number of streams seen recently, from the two newest daily databases
# (around midnight the newest rows can still be in yesterday's file). -1 means nothing readable.
read -r age streams < <(python3 - "$DATA" "$now" "$MAX_AGE_SEC" <<'PY'
import sqlite3
import sys
from pathlib import Path

data, now, window = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
newest, streams = None, set()
for db in sorted(data.glob("orderbook-????-??-??.db"))[-2:]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            top = con.execute("SELECT MAX(snapshot_id) FROM snapshots").fetchone()[0]
            if top is None:
                continue
            rows = con.execute(
                "SELECT venue, market, symbol, aggregation, received_ms FROM snapshots WHERE snapshot_id > ?",
                (top - 1000,)).fetchall()            # about 90 minutes of 11 streams; enough for the window
        finally:
            con.close()
    except sqlite3.Error:
        continue
    for venue, market, symbol, aggregation, received_ms in rows:
        newest = received_ms if newest is None else max(newest, received_ms)
        if received_ms >= (now - window) * 1000:
            streams.add((venue, market, symbol, aggregation))
print(-1 if newest is None else max(0, now - newest // 1000), len(streams))
PY
)
age=${age:--1}
streams=${streams:-0}

pending=0
for db in "$DATA"/orderbook-????-??-??.db; do
  [ -e "$db" ] || continue
  name=$(basename "$db")
  day=${name#orderbook-}
  day=${day%.db}
  due=$(( $(date -u -d "$day" +%s) + 86400 + UPLOAD_GRACE_MIN * 60 ))
  (( now >= due )) || continue
  [ -e "$STAGE/$name.uploaded" ] || pending=$((pending + 1))
done

free_gb=$(df --output=avail -B1G "$DATA" 2>/dev/null | tail -1 | tr -d ' ')
[[ "$free_gb" =~ ^[0-9]+$ ]] || free_gb=-1
code=$(basename "$(readlink -f "$CODE_LINK" 2>/dev/null || echo unknown)")

problems=()
[ "$service" = active ] || problems+=("service_$service")
if (( age < 0 )); then problems+=("no_snapshots")
elif (( age > MAX_AGE_SEC )); then problems+=("newest_snapshot_${age}s_old"); fi
(( streams >= EXPECTED_STREAMS )) || problems+=("streams_${streams}_of_${EXPECTED_STREAMS}")
(( pending == 0 )) || problems+=("uploads_pending_${pending}")

if (( ${#problems[@]} == 0 )); then status=ok; severity=INFO; else status=stale; severity=ERROR; fi
joined=$(IFS=,; echo "${problems[*]:-}")
payload=$(printf '{"status":"%s","problems":"%s","service":"%s","newest_age_sec":%d,"streams_recent":%d,"expected_streams":%d,"pending_uploads":%d,"free_gb":%d,"code":"%s"}' \
  "$status" "$joined" "$service" "$age" "$streams" "$EXPECTED_STREAMS" "$pending" "$free_gb" "$code")
echo "$payload"
[ -n "${DRY_RUN:-}" ] && exit 0
gcloud --quiet logging write "$LOG_NAME" "$payload" --payload-type=json --severity="$severity" >/dev/null ||
  { echo "could not write the heartbeat to Cloud Logging" >&2; exit 3; }
