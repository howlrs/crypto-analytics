#!/usr/bin/env bash
# Create or update (idempotent) the email alert for the order-book collector.
#
#   PROJECT=crypto-bitflyer-418902 ALERT_EMAIL=<address> ./alerting.sh
#
# What it sets up:
#   - the Logging and Monitoring APIs, and permission for the collector's service account to write logs;
#   - a log-based counter of healthy heartbeats (heartbeat.sh writes one every 10 minutes);
#   - an email notification channel for ALERT_EMAIL (reused when one already exists);
#   - an alert policy that fires when no healthy heartbeat arrives for ORDERBOOK_ALERT_ABSENT_SEC (30 min).
# The address is taken from the environment so it never has to be written into the repository.
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT}
ALERT_EMAIL=${ALERT_EMAIL:?set ALERT_EMAIL to the address that should receive the alert}
GCLOUD_CONFIG=${ORDERBOOK_GCLOUD_CONFIG:-gonumb}
SA_NAME=${ORDERBOOK_SERVICE_ACCOUNT:-orderbook-collector}
ABSENT_SEC=${ORDERBOOK_ALERT_ABSENT_SEC:-1800}
[[ "$ABSENT_SEC" =~ ^[0-9]+$ ]] && (( ABSENT_SEC >= 600 )) || { echo "ORDERBOOK_ALERT_ABSENT_SEC must be >= 600" >&2; exit 2; }
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
METRIC=orderbook_heartbeat_ok
POLICY_NAME="orderbook collector: no healthy heartbeat"
CHANNEL_NAME="orderbook collector alerts"

g() { gcloud --configuration="$GCLOUD_CONFIG" --project="$PROJECT" --quiet "$@"; }

echo "account: $(gcloud --configuration="$GCLOUD_CONFIG" config get-value account 2>/dev/null)  project: $PROJECT"
g services enable logging.googleapis.com monitoring.googleapis.com
g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/logging.logWriter \
  --condition=None >/dev/null

if ! g logging metrics describe "$METRIC" >/dev/null 2>&1; then
  g logging metrics create "$METRIC" \
    --description="Healthy heartbeats of the order-book collector (orderbook/deploy/gce/heartbeat.sh)" \
    --log-filter="logName=\"projects/$PROJECT/logs/orderbook-heartbeat\" AND jsonPayload.status=\"ok\""
fi

channel=$(g beta monitoring channels list --filter="type=\"email\" AND labels.email_address=\"$ALERT_EMAIL\"" \
  --format='value(name)' | head -1)
if [ -z "$channel" ]; then
  channel=$(g beta monitoring channels create --display-name="$CHANNEL_NAME" --type=email \
    --channel-labels=email_address="$ALERT_EMAIL" --format='value(name)')
fi
[ -n "$channel" ] || { echo "no notification channel" >&2; exit 1; }

policy=$(mktemp)
trap 'rm -f "$policy"' EXIT
cat > "$policy" <<JSON
{
  "displayName": "$POLICY_NAME",
  "combiner": "OR",
  "conditions": [{
    "displayName": "no healthy heartbeat for $((ABSENT_SEC / 60)) minutes",
    "conditionAbsent": {
      "filter": "metric.type=\"logging.googleapis.com/user/$METRIC\" AND resource.type=\"global\"",
      "duration": "${ABSENT_SEC}s",
      "aggregations": [{"alignmentPeriod": "600s", "perSeriesAligner": "ALIGN_SUM"}]
    }
  }],
  "notificationChannels": ["$channel"],
  "alertStrategy": {"autoClose": "86400s"},
  "documentation": {
    "mimeType": "text/markdown",
    "content": "The order-book collector has not reported a healthy heartbeat. Snapshots missed while it is down cannot be recovered.\n\n1. Problems in the latest heartbeat: gcloud logging read 'logName=\"projects/$PROJECT/logs/orderbook-heartbeat\"' --limit=3\n2. VM state: gcloud compute instances describe orderbook-collector --zone=asia-northeast1-b --format='value(status)'\n3. On the VM (IAP SSH): systemctl status orderbook-collector; journalctl -u orderbook-collector -n 20\n4. Bucket freshness: orderbook/deploy/gce/check-freshness.sh gs://$PROJECT-orderbook/orderbook\n\nRunbook: docs/orderbook.md in howlrs/crypto-analytics."
  }
}
JSON

existing=$(g monitoring policies list --filter="displayName=\"$POLICY_NAME\"" --format='value(name)' | head -1)
if [ -n "$existing" ]; then
  g monitoring policies update "$existing" --policy-from-file="$policy" >/dev/null
  echo "updated policy $existing"
else
  g monitoring policies create --policy-from-file="$policy" --format='value(name)'
fi
echo "channel $channel"
