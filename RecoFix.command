#!/bin/bash
# Doble clic para abrir RecoFix en el navegador. Pide la contraseña de administrador
# porque leer discos en bruto (/dev/rdisk*) requiere permisos de root.
cd "$(dirname "$0")" || exit 1
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)
PORT=8765
TOKEN=$(uuidgen | tr -d '-')
echo "RecoFix — introduce tu contraseña de administrador para acceder a los discos."
sudo -v || exit 1
sudo env RECOFIX_TOKEN="$TOKEN" PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" "$PY" -m recofix serve --port "$PORT" --no-browser &
SERVER=$!
for _ in $(seq 1 50); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.2
done
open "http://127.0.0.1:$PORT/?t=$TOKEN"
echo "RecoFix abierto. Cierra esta ventana o pulsa Ctrl+C para salir."
trap 'sudo kill $SERVER 2>/dev/null; exit 0' INT TERM
wait $SERVER
