# Banana bridge tunnel (DESK-65)

The Buchungsdesk runs on the VPS; the Banana engine and the `.ac2` files live on
Daniel's Mac. `banana-import` on the Mac exposes `/bridge/ping|chart|book`, and this
reverse SSH tunnel lets the VPS desk reach it. Live-only by design: when the tunnel +
Banana are up, the desk books; otherwise the pill is grey (like the office VPN).

```
 VPS desk container ──▶ host.docker.internal:8501 ──▶ (VPS 127.0.0.1:8501)
        ═══ reverse SSH (this Mac initiates) ═══▶ Mac 127.0.0.1:8500 (banana-import)
                                                        └─▶ 127.0.0.1:8089 (Banana engine)
                                                        └─▶ BANANA_FILE_ROOT/*.ac2
```

## One-time setup

### 1. Mac banana-import with the bridge (v1.21.0+)
In `tools/banana-import/.env` set the folder that holds the `.ac2` files (the
SharePoint/communication-share path synced to this Mac):
```
BANANA_FILE_ROOT=/Users/danielbuetler/…/<synced Banana folder>
BANANA_TOKEN=…        # already set (the acstkn the invoice booker uses)
```
Rebuild + run, then verify (Banana must be open):
```
find tools/banana-import -name '._*' -delete
docker compose -f tools/banana-import/docker-compose.yml build --no-cache
docker compose -f tools/banana-import/docker-compose.yml up -d
curl -sk http://127.0.0.1:8500/bridge/ping    # -> {"ok":true,"engine_up":true,...}
```

### 2. SSH key Mac → VPS
The Mac must ssh to the VPS non-interactively (reuse the deploy key). Test:
`ssh <deploy>@<vps-host> true` should return without a prompt.

### 3. Install the tunnel LaunchAgent
Edit `com.danbuetler.banana-tunnel.plist` → set `VPS_HOST`, `VPS_USER`, ports. Then:
```
cp bridge-tunnel/com.danbuetler.banana-tunnel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.danbuetler.banana-tunnel.plist
```
Restart after edits: `launchctl kickstart -k gui/$(id -u)/com.danbuetler.banana-tunnel`.
Logs: `/tmp/banana-tunnel.log` / `.err`.

### 4. Verify from the VPS
```
curl -s http://127.0.0.1:8501/bridge/ping    # -> engine_up true when Banana is open
```

### 5. Point the desk at the bridge
In the desk's VPS `.env`:
```
BANANA_BRIDGE_URL=http://host.docker.internal:8501
```
Ensure `docker-compose.vps.yml` gives the desk backend
`extra_hosts: ["host.docker.internal:host-gateway"]`, then redeploy the desk. The
"Banana verbunden" pill goes green whenever the tunnel + Banana are up.

## Notes
- The tunnel binds the VPS end to `127.0.0.1` only — not exposed publicly.
- `BANANA_FILE_ROOT` and the folder Banana opens the file from must be the SAME
  synced folder, so a booking's file write is the file you reload in Banana.
- A booking writes the `.ac2`; reload (File → Reload) + ⇧⌘F9 in Banana to see it
  (the engine has no in-place write).
