#!/bin/bash
# Generates a self-signed CA + broker certificate/key for TLS testing with
# mosquitto.tls.conf.example. NOT for production -- use certs from a real
# CA (or something like Let's Encrypt / your internal PKI) there.
set -euo pipefail
cd "$(dirname "$0")/config"
mkdir -p certs
cd certs

DAYS=825
CN="${1:-mosquitto}"   # hostname clients will connect to, e.g. your broker's DNS name

echo "Generating CA..."
openssl genrsa -out ca.key 2048 2>/dev/null
openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
  -subj "/CN=genai-tracker-test-ca" -out ca.crt 2>/dev/null

echo "Generating broker key + CSR..."
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -subj "/CN=${CN}" -out server.csr 2>/dev/null

echo "Signing broker cert with CA..."
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days "$DAYS" -sha256 2>/dev/null

rm -f server.csr ca.srl
chmod 600 ca.key server.key

echo "Done. Files written to $(pwd):"
ls -la ca.crt server.crt server.key
echo
echo "Set MQTT_CA_CERT to the path where ca.crt is mounted in each"
echo "proxy/collector container, and MQTT_USE_TLS=true."
