#!/bin/bash
# Starts Radicale and the sync loop. If either process dies, the container stops
# (and is restarted by --restart unless-stopped).
python3 /app/init.py || exit 1

python3 -m radicale --config "${DATA_DIR:-/data}/config/radicale.conf" &
RAD=$!
python3 /app/sync.py &
SYNC=$!

term() { kill -TERM "$RAD" "$SYNC" 2>/dev/null; wait; exit 0; }
trap term TERM INT

wait -n
rc=$?
echo "[entrypoint] A process exited (rc=$rc) - stopping container" >&2
kill -TERM "$RAD" "$SYNC" 2>/dev/null
wait
exit 1
