#!/usr/bin/env bash
# Collect GuestOS *host* logs for a failed customize job (no .env, no passwords).
#
# Paste on the GuestOS utility VM, or:
#   sudo bash extract_guestos_host_logs.sh VDI-W11-TEST08
#
# Guest Windows transcript is NOT here — that is on the clone:
#   C:\ProgramData\GuestOS\setup.log
set -euo pipefail

HOST_FILTER="${1:-}"
SINCE="${GUESTOS_LOG_SINCE:-48h}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${GUESTOS_LOG_OUT:-/tmp/guestos-host-logs-${STAMP}}"
mkdir -p "$OUT"

log() { printf '%s\n' "$*"; }

find_container() {
  local pat="$1"
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E "$pat" | head -n 1 || true
}

WEB="$(find_container '(^|[-_])web([-_][0-9]+)?$')"
VERIFY="$(find_container 'verify[-_]worker')"
WORKER="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '(^|[-_])worker([-_][0-9]+)?$' | grep -v verify | head -n 1 || true)"

{
  echo "stamp=$STAMP"
  echo "host_filter=${HOST_FILTER:-'(latest FAILURE tasks)'}"
  echo "since=$SINCE"
  echo "web=${WEB:-missing}"
  echo "worker=${WORKER:-missing}"
  echo "verify=${VERIFY:-missing}"
  echo "docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
} | tee "$OUT/meta.txt"

if [ -n "$WEB" ]; then
  docker exec "$WEB" sh -c 'cat /app/VERSION 2>/dev/null; cat /app/BUILD_TIMESTAMP 2>/dev/null; echo; env | grep -E "^(DOMAIN_JOIN_ODJ|DOMAIN_JOIN_ODJ_TIMEOUT|DOMAIN_JOIN_CRED_PROBE|APP_VERSION)=" || true' \
    > "$OUT/app_version_env.txt" 2>&1 || true
  docker inspect --format 'Image={{.Config.Image}} Created={{.Created}} Id={{.Id}}' "$WEB" > "$OUT/web.inspect.txt" 2>&1 || true
fi

dump_logs() {
  local name="$1" file="$2"
  [ -n "$name" ] || { echo "container missing" > "$file"; return; }
  docker logs --since "$SINCE" --timestamps "$name" > "$file" 2>&1 || true
  if [ -n "$HOST_FILTER" ]; then
    grep -Ei "join-path|ODJ:|domain join|Add-Computer|provision=|sysprep|${HOST_FILTER}" "$file" \
      > "${file%.log}.filtered.log" || true
  else
    grep -Ei 'join-path|ODJ:|domain join|Add-Computer|provision=|sysprep|domain-join-failed' "$file" \
      > "${file%.log}.filtered.log" || true
  fi
}

dump_logs "$WEB" "$OUT/web.log"
dump_logs "$WORKER" "$OUT/worker.log"
dump_logs "$VERIFY" "$OUT/verify-worker.log"

if [ -n "$WEB" ]; then
  docker exec -i -e HOST_FILTER="$HOST_FILTER" "$WEB" python3 - <<'PY' > "$OUT/tasks.txt" 2>&1 || true
import os, sqlite3, json
from pathlib import Path

db = Path("/app/instance/site.db")
if not db.is_file():
    cands = list(Path("/app/instance").glob("*.db"))
    db = cands[0] if cands else db
print(f"sqlite={db} exists={db.is_file()}")
if not db.is_file():
    raise SystemExit(0)

host = (os.environ.get("HOST_FILTER") or "").strip()
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
cols = {r[1] for r in cur.execute("PRAGMA table_info(task)")}
want = [
    "id", "name", "status", "progress", "message", "hostname", "result_vmid",
    "result_ip_address", "error_code", "error_details", "template_vmid",
    "batch_id", "timestamp", "updated_at", "options_json", "event_log",
]
sel = [c for c in want if c in cols]
if host:
    like = f"%{host}%"
    rows = cur.execute(
        f"SELECT {', '.join(sel)} FROM task WHERE hostname LIKE ? OR message LIKE ? "
        f"OR IFNULL(event_log,'') LIKE ? ORDER BY updated_at DESC LIMIT 20",
        (like, like, like),
    ).fetchall()
else:
    rows = cur.execute(
        f"SELECT {', '.join(sel)} FROM task WHERE status IN ('FAILURE','SUCCESS') "
        f"ORDER BY updated_at DESC LIMIT 8"
    ).fetchall()

secret_keys = {
    "administrator_password", "domain_password", "domain_join_b64",
    "cipassword", "sshkeys", "password",
}

def scrub_options(raw):
    if not raw:
        return raw
    try:
        obj = json.loads(raw)
    except Exception:
        return "(unparseable options_json)"
    if isinstance(obj, dict):
        for k in list(obj):
            if k.lower() in secret_keys or "password" in k.lower():
                obj[k] = "***"
    return json.dumps(obj, indent=2, sort_keys=True)

print(f"rows={len(rows)} filter={host or '(latest)'}")
for i, row in enumerate(rows, 1):
    d = dict(row)
    print("\n" + "=" * 72)
    print(f"TASK {i}")
    for k in sel:
        if k in ("options_json", "event_log"):
            continue
        print(f"  {k}: {d.get(k)}")
    print("  options_json:")
    print(scrub_options(d.get("options_json")))
    print("  event_log:")
    print(d.get("event_log") or "(empty)")
con.close()
PY
fi

TAR="/tmp/guestos-host-logs-${STAMP}.tar.gz"
tar -C "$(dirname "$OUT")" -czf "$TAR" "$(basename "$OUT")"
log ""
log "Wrote $OUT"
log "Archive $TAR"
log "Copy off the host:  scp user@guestos-host:$TAR ."
log "Windows clone log is still: C:\\ProgramData\\GuestOS\\setup.log"
ls -lh "$TAR"
