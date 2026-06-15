#!/usr/bin/env bash
# One-time: copy the BANANA_TOKEN from your banana-mcp config into this tool's
# .env and recreate the container so it can read Banana's live chart of accounts.
set -e
cd "$(dirname "$0")"

TOK=$(python3 - <<'PY'
import re, os
s = open(os.path.expanduser('~/.claude.json')).read()
m = re.search(r'"BANANA_TOKEN"\s*:\s*"([^"]+)"', s)
print(m.group(1) if m else '')
PY
)

if [ -z "$TOK" ]; then
  echo "❌ BANANA_TOKEN not found in ~/.claude.json."
  echo "   Get it from Banana > Tools > Program options > Webserver and add"
  echo "   a line  BANANA_TOKEN=<value>  to tools/banana-import/.env yourself."
  exit 1
fi

# Replace any existing BANANA_TOKEN line, then append the fresh one.
grep -v '^BANANA_TOKEN=' .env > .env.tmp 2>/dev/null || true
mv .env.tmp .env 2>/dev/null || true
printf 'BANANA_TOKEN=%s\n' "$TOK" >> .env
echo "✅ Wrote BANANA_TOKEN (length ${#TOK}) to .env"

docker compose up -d --force-recreate
echo "✅ Container recreated — now click the ↻ refresh in the browser."
