#!/usr/bin/env bash
# GCE startup script (runs as root on every boot): install the pinned commit of
# howlrs/crypto-analytics and run the order-book collector plus the hourly upload.
set -euo pipefail

md() { curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
COMMIT=$(md orderbook-commit)
BUCKET=$(md orderbook-bucket)
UNTIL=$(md orderbook-until)
[[ "$COMMIT" =~ ^[0-9a-f]{7,40}$ ]] || { echo "invalid orderbook-commit: $COMMIT" >&2; exit 1; }
STREAMS=hyperliquid:perp:BTC,hyperliquid:perp:BTC:4,binance:perp:BTCUSDT,binance:spot:BTCUSDT,hyperliquid:perp:ETH,binance:perp:ETHUSDT,binance:spot:ETHUSDT,hyperliquid:perp:HYPE,hyperliquid:perp:HYPE:4,bybit:perp:HYPEUSDT,bybit:spot:HYPEUSDT

id orderbook >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir /var/lib/orderbook --shell /usr/sbin/nologin orderbook
install -d -o orderbook -g orderbook /var/lib/orderbook/data /var/lib/orderbook/upload

CODE=/opt/orderbook/$COMMIT
if [ ! -d "$CODE" ]; then
  tmp=$(mktemp -d)
  curl -fsSL "https://codeload.github.com/howlrs/crypto-analytics/tar.gz/$COMMIT" | tar -xz -C "$tmp" --strip-components=1
  mkdir -p /opt/orderbook
  mv "$tmp" "$CODE"
  chmod -R a+rX "$CODE"
fi
previous=$(readlink /opt/orderbook/current || true)
ln -sfn "$CODE" /opt/orderbook/current

command -v gcloud >/dev/null 2>&1 || snap install google-cloud-cli --classic

cat > /etc/systemd/system/orderbook-collector.service <<UNIT
[Unit]
Description=Order-book snapshot collector (crypto-analytics $COMMIT)
After=network-online.target
Wants=network-online.target

[Service]
User=orderbook
WorkingDirectory=/opt/orderbook/current
ExecStart=/usr/bin/python3 -m orderbook.supervise --data-dir /var/lib/orderbook/data --db-period day \\
  --allow-default-route --streams $STREAMS --interval-sec 60 --chunk-min 360 --until $UNTIL \\
  --min-free-gb 3
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=3
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/orderbook-upload.service <<UNIT
[Unit]
Description=Upload closed daily order-book databases to gs://$BUCKET/orderbook

[Service]
Type=oneshot
User=orderbook
WorkingDirectory=/opt/orderbook/current
ExecStart=/opt/orderbook/current/orderbook/deploy/gce/upload-closed.sh gs://$BUCKET/orderbook
UNIT

cat > /etc/systemd/system/orderbook-upload.timer <<UNIT
[Unit]
Description=Hourly upload of closed daily order-book databases

[Timer]
OnCalendar=*-*-* *:20:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now orderbook-upload.timer
systemctl enable orderbook-collector.service
if [ "$previous" != "$CODE" ]; then
  systemctl restart orderbook-collector.service
else
  systemctl start orderbook-collector.service
fi
