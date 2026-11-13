#!/usr/bin/env bash
# End-to-end smoke test for the local server and its packaged browser UI.
#
# Starts the real FastAPI app against the scripted model from tests/fake_model.py,
# then checks the health/chat API and every asset the web UI loads. Uses a
# throwaway KOVA_HOME and a free loopback port, so it is safe to run next to a
# normal Kova install. No model, network or browser is required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/kova-smoke.XXXXXX")"
SERVER_PID=""
SERVER_LOG="$WORK/server.log"

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  printf 'smoke: %s\n' "$*" >&2
  if [[ -f "$SERVER_LOG" ]]; then
    printf '\n--- server log ---\n' >&2
    cat "$SERVER_LOG" >&2
  fi
  exit 1
}

step() { printf '  %s\n' "$*"; }

# Prefer the project's uv environment so uvicorn and dependencies are present.
if command -v uv >/dev/null 2>&1; then
  uv sync --quiet
  RUN=(uv run --quiet python)
else
  RUN=("${PYTHON:-python3}")
fi

# A free loopback port; the server binds it immediately afterwards.
PORT="$("${RUN[@]}" - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
)"
[[ -n "$PORT" ]] || fail "could not pick a free port"

cat >"$WORK/server.py" <<'PY'
"""Run the real Kova server with the scripted test model as its transport."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["KOVA_SMOKE_ROOT"])

import uvicorn  # noqa: E402

from kova.config import load_config, save_config  # noqa: E402
from kova.server import create_app  # noqa: E402
from tests.fake_model import FakeModel, text  # noqa: E402

home = Path(os.environ["KOVA_HOME"])
files = home / "files"
files.mkdir(parents=True, exist_ok=True)

cfg = load_config()
cfg.port = int(os.environ["KOVA_SMOKE_PORT"])
cfg.files_path = files
cfg.models[0].model = "smoke-model"
cfg.models[0].base_url = "http://model.test/v1"
cfg.active = cfg.models[0].name
save_config(cfg)

app = create_app(cfg, transport=FakeModel([text("Hello from smoke test!")] * 5).transport)
uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning", access_log=False)
PY

BASE="http://127.0.0.1:$PORT"
HEADERS="$WORK/headers"
BODY="$WORK/body"

fetch() { # fetch OUTPUT CURL_ARGS...
  local out="$1"
  shift
  curl -sS --max-time 20 -o "$out" -D "$HEADERS" -w '%{http_code}' "$@"
}

content_type() {
  tr -d '\r' <"$HEADERS" | grep -i '^content-type:' | head -n 1 | sed 's/^[^:]*:[[:space:]]*//'
}

expect_type() { # expect_type NAME FRAGMENT
  local name="$1" wanted="$2" actual
  actual="$(content_type | tr '[:upper:]' '[:lower:]')"
  [[ "$actual" == *"$wanted"* ]] || fail "$name has content-type '$actual', wanted something like '$wanted'"
  step "$name -> $actual"
}

step "starting server on $BASE (KOVA_HOME=$WORK/home)"
KOVA_HOME="$WORK/home" KOVA_SMOKE_PORT="$PORT" KOVA_SMOKE_ROOT="$ROOT" \
  "${RUN[@]}" "$WORK/server.py" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

READY=0
for _ in $(seq 1 200); do
  if curl -fsS --max-time 1 "$BASE/health" >/dev/null 2>&1; then READY=1; break; fi
  kill -0 "$SERVER_PID" 2>/dev/null || fail "server exited during startup"
  sleep 0.1
done
[[ "$READY" == 1 ]] || fail "server did not become healthy"

STATUS="$(fetch "$WORK/health.json" "$BASE/health")"
[[ "$STATUS" == 200 ]] || fail "GET /health returned $STATUS"
"${RUN[@]}" - "$WORK/health.json" <<'PY' || fail "GET /health returned unexpected JSON"
import json, sys
body = json.load(open(sys.argv[1]))
assert {"version", "model", "model_ok"} <= body.keys(), body
assert body["model_ok"] is True, body
PY
step "GET /health -> 200, model_ok"

TOKEN="$(cat "$WORK/home/token")"

STATUS="$(fetch "$WORK/chat.json" -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' "$BASE/chats")"
[[ "$STATUS" == 200 ]] || fail "POST /chats returned $STATUS: $(cat "$WORK/chat.json")"
CHAT_ID="$("${RUN[@]}" - "$WORK/chat.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["id"])
PY
)"
step "POST /chats -> 200 (id=$CHAT_ID)"

STATUS="$(fetch "$WORK/turn.sse" -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"text": "hello"}' \
  "$BASE/chats/$CHAT_ID/messages")"
[[ "$STATUS" == 200 ]] || fail "POST /chats/$CHAT_ID/messages returned $STATUS: $(cat "$WORK/turn.sse")"
expect_type "POST /chats/{id}/messages" "text/event-stream"
"${RUN[@]}" - "$WORK/turn.sse" <<'PY' || fail "chat stream did not contain a complete reply followed by done"
import json, sys
frames = [block for block in open(sys.argv[1]).read().split("\n\n") if block.strip()]
events = []
for frame in frames:
    lines = frame.splitlines()
    assert lines[0].startswith("event: ") and lines[1].startswith("data: "), frame
    events.append(json.loads(lines[1][len("data: "):]))
assert events[-1]["type"] == "done", events[-1]
reply = "".join(event.get("text", "") for event in events[:-1])
assert reply == "Hello from smoke test!", reply
PY
step "chat stream -> reply and done event"

STATUS="$(fetch "$WORK/index.html" "$BASE/app")"
[[ "$STATUS" == 200 ]] || fail "GET /app returned $STATUS"
expect_type "GET /app" "text/html"
grep -q 'id="composer"' "$WORK/index.html" || fail "GET /app did not return the web UI shell"
step "GET /app -> 200 with UI shell"

check_asset() { # check_asset PATH EXPECTED_CONTENT_TYPE
  local path="$1" wanted="$2" status
  status="$(fetch "$BODY" "$BASE/app/assets/$path")"
  [[ "$status" == 200 ]] || fail "GET /app/assets/$path returned $status"
  [[ -s "$BODY" ]] || fail "GET /app/assets/$path returned an empty body"
  expect_type "GET /app/assets/$path" "$wanted"
}

check_asset "app.js" "javascript"
check_asset "app.css" "text/css"
check_asset "icon.svg" "image/svg+xml"
check_asset "vendor/marked.js" "javascript"
check_asset "vendor/purify.js" "javascript"
check_asset "vendor/highlight.js" "javascript"
check_asset "fonts/InterVariable.woff2" "font/woff2"
check_asset "fonts/InterVariable-Italic.woff2" "font/woff2"
check_asset "fonts/JetBrainsMonoVariable.woff2" "font/woff2"

STATUS="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' \
  "$BASE/app/assets/%2e%2e/%2e%2e/server.py")"
[[ "$STATUS" == 404 ]] || fail "asset traversal returned $STATUS instead of 404"
step "asset traversal -> 404"

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  fail "server exited before shutdown"
fi
step "shutting down"
printf 'smoke: all checks passed\n'
