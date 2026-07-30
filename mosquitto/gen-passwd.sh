#!/bin/bash
# Usage: ./gen-passwd.sh <username> <password> [more-username-password-pairs...]
# Creates (or appends to) mosquitto/config/passwd, used by
# mosquitto.auth.conf.example. Requires mosquitto-clients installed locally
# (or run this inside the mosquitto container: docker compose exec mosquitto sh).
set -euo pipefail
cd "$(dirname "$0")/config"

if [ "$#" -lt 2 ] || [ $(( $# % 2 )) -ne 0 ]; then
  echo "Usage: $0 <username> <password> [<username> <password> ...]" >&2
  exit 1
fi

first=true
while [ "$#" -ge 2 ]; do
  user="$1"; pass="$2"; shift 2
  if [ "$first" = true ] && [ ! -f passwd ]; then
    mosquitto_passwd -c -b passwd "$user" "$pass"
    first=false
  else
    mosquitto_passwd -b passwd "$user" "$pass"
  fi
  echo "Added/updated user: $user"
done

chmod 0700 passwd
echo "Wrote $(pwd)/passwd"
