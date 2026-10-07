#!/bin/bash
# Starts Radicale, the sync loop and (with ENROLLMENT=true or ADMIN_UI=true) the web endpoint.
# If any process dies, the container stops (and is restarted by --restart unless-stopped).
python3 /app/init.py || exit 1

python3 -m radicale --config "${DATA_DIR:-/data}/config/radicale.conf" &
RAD=$!
python3 /app/sync.py &
SYNC=$!
ENR=
case "${ENROLLMENT,,}|${ADMIN_UI,,}" in
  1\|*|true\|*|yes\|*|*\|1|*\|true|*\|yes) python3 /app/web.py & ENR=$! ;;
esac

term() { kill -TERM "$RAD" "$SYNC" $ENR 2>/dev/null; wait; exit 0; }
trap term TERM INT

wait -n
rc=$?
echo "[entrypoint] A process exited (rc=$rc) - stopping container" >&2
kill -TERM "$RAD" "$SYNC" $ENR 2>/dev/null
wait
exit 1
