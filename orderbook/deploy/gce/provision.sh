#!/usr/bin/env bash
# Create the GCE order-book collector (idempotent: existing resources are kept, not modified).
#
#   PROJECT=crypto-bitflyer-418902 COMMIT=<git sha> ./provision.sh
#
# Resources (all named orderbook-*): IAM/IAP APIs, a dedicated VPC + subnet with only an
# IAP-SSH ingress rule, a service account allowed to create/read objects in one bucket,
# that bucket, and an e2-micro Ubuntu 24.04 VM whose startup script installs the pinned
# commit and the systemd units (see startup.sh).
set -euo pipefail

PROJECT=${PROJECT:?set PROJECT}
COMMIT=${COMMIT:?set COMMIT to the git commit the VM should run}
GCLOUD_CONFIG=${GCLOUD_CONFIG:-gonumb}
REGION=${REGION:-asia-northeast1}
ZONE=${ZONE:-asia-northeast1-b}
NAME=${NAME:-orderbook-collector}
NETWORK=${NETWORK:-orderbook-net}
SUBNET=${SUBNET:-orderbook-subnet}
SUBNET_RANGE=${SUBNET_RANGE:-10.20.0.0/24}
BUCKET=${BUCKET:-${PROJECT}-orderbook}
UNTIL=${UNTIL:-2026-11-06T00:00:00Z}
MACHINE_TYPE=${MACHINE_TYPE:-e2-micro}
DISK_GB=${DISK_GB:-20}
SA_NAME=${SA_NAME:-orderbook-collector}
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
HERE=$(cd "$(dirname "$0")" && pwd)

g() { gcloud --configuration="$GCLOUD_CONFIG" --project="$PROJECT" --quiet "$@"; }
exists() { g "$@" >/dev/null 2>&1; }

echo "account: $(gcloud --configuration="$GCLOUD_CONFIG" config get-value account 2>/dev/null)  project: $PROJECT"

g services enable iam.googleapis.com iap.googleapis.com

exists compute networks describe "$NETWORK" ||
  g compute networks create "$NETWORK" --subnet-mode=custom
exists compute networks subnets describe "$SUBNET" --region="$REGION" ||
  g compute networks subnets create "$SUBNET" --network="$NETWORK" --region="$REGION" --range="$SUBNET_RANGE"
# Only Google's IAP TCP-forwarding range may reach SSH; nothing else is allowed in.
exists compute firewall-rules describe "${NETWORK}-allow-iap-ssh" ||
  g compute firewall-rules create "${NETWORK}-allow-iap-ssh" --network="$NETWORK" --direction=INGRESS \
    --action=allow --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags="$NAME"

exists iam service-accounts describe "$SA" ||
  g iam service-accounts create "$SA_NAME" --display-name="Order-book collector"

exists storage buckets describe "gs://$BUCKET" ||
  g storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention
# Create and read objects in this bucket only; no delete or overwrite, no other buckets.
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role="$role" >/dev/null
done

if exists compute instances describe "$NAME" --zone="$ZONE"; then
  echo "instance $NAME exists; to change the commit run:"
  echo "  gcloud --configuration=$GCLOUD_CONFIG --project=$PROJECT compute instances add-metadata $NAME --zone=$ZONE --metadata=orderbook-commit=<sha>"
  echo "  and reboot the VM (the startup script installs the new commit)."
else
  g compute instances create "$NAME" --zone="$ZONE" --machine-type="$MACHINE_TYPE" \
    --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
    --boot-disk-size="${DISK_GB}GB" --boot-disk-type=pd-balanced \
    --subnet="$SUBNET" --tags="$NAME" \
    --service-account="$SA" --scopes=cloud-platform \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata="enable-oslogin=TRUE,orderbook-commit=$COMMIT,orderbook-bucket=$BUCKET,orderbook-until=$UNTIL" \
    --metadata-from-file=startup-script="$HERE/startup.sh"
fi
echo "done. SSH: gcloud --configuration=$GCLOUD_CONFIG --project=$PROJECT compute ssh $NAME --zone=$ZONE --tunnel-through-iap"
