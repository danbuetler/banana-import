#!/bin/bash
# Reverse SSH tunnel (DESK-65): expose THIS Mac's banana-import bridge
# (127.0.0.1:${LOCAL_PORT}) to the VPS as 127.0.0.1:${VPS_PORT}, so the desk
# container on the VPS can reach the Mac-local Banana engine. Mirrors the
# com.danbuetler.honcho-tunnel LaunchAgent, in the opposite direction.
#
# The VPS end binds to 127.0.0.1 only (no GatewayPorts needed): the desk container
# reaches it via host.docker.internal:${VPS_PORT}.
set -euo pipefail

VPS_HOST="${VPS_HOST:-5.102.145.140}"          # cloudscale b2b-tools-01
VPS_USER="${VPS_USER:-ubuntu}"
VPS_PORT="${VPS_PORT:-8501}"                    # port on the VPS side of the tunnel
LOCAL_PORT="${LOCAL_PORT:-8500}"               # this Mac's banana-import bridge
BIND_ADDR="${BIND_ADDR:-172.18.0.1}"           # the `web` docker network gateway the desk container routes through
SSH_KEY="${SSH_KEY:-$HOME/.cloudscale/cloudscale_ed25519}"   # the fleet deploy key

# Binding the docker-gateway IP (not 0.0.0.0) needs `GatewayPorts clientspecified`
# on the VPS sshd; it keeps 8501 off the public interface.
exec ssh -N -i "${SSH_KEY}" -o IdentitiesOnly=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
  -R "${BIND_ADDR}:${VPS_PORT}:127.0.0.1:${LOCAL_PORT}" \
  "${VPS_USER}@${VPS_HOST}"
