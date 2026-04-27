#!/usr/bin/env bash
# Activate the Tier 1 RT hardening once, after the cmdline.txt reboot.
# Idempotent: safe to run multiple times. Run as the synth user (no sudo).
#
# What it does:
#   1. Redeploys systemd/stave-synth.service to ~/.config/systemd/user/
#      so the new WatchdogSec + NotifyAccess take effect
#   2. systemctl --user daemon-reload + restart stave-synth
#   3. Verifies CPU 3 is isolated, render thread pinned, watchdog wired
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT="$HOME/.config/systemd/user/stave-synth.service"
USER_DROPIN="$HOME/.config/systemd/user/stave-synth.service.d"

GREEN=$'\033[0;32m'
ORANGE=$'\033[0;33m'
RED=$'\033[0;31m'
NC=$'\033[0m'

echo "${ORANGE}[1/3]${NC} Redeploying systemd unit..."
mkdir -p "$USER_DROPIN"
cp "$SCRIPT_DIR/systemd/stave-synth.service" "$USER_UNIT"
cp "$SCRIPT_DIR/systemd/stave-synth.service.d/faust.conf" "$USER_DROPIN/faust.conf"
systemctl --user daemon-reload
echo "${GREEN}  unit installed${NC}"

echo "${ORANGE}[2/3]${NC} Restarting synth..."
systemctl --user restart stave-synth.service
sleep 4
if ! systemctl --user is-active --quiet stave-synth.service; then
    echo "${RED}  synth did not come up — check: journalctl --user -u stave-synth -n 60${NC}"
    exit 1
fi
echo "${GREEN}  synth running${NC}"

echo "${ORANGE}[3/3]${NC} Verifying activation..."
ISOLATED="$(cat /sys/devices/system/cpu/isolated 2>/dev/null || echo '')"
if [ "$ISOLATED" = "3" ]; then
    echo "${GREEN}  isolcpus=3 active${NC}"
else
    echo "${RED}  CPU 3 not isolated (got '$ISOLATED') — did you reboot after install.sh?${NC}"
fi

# Render thread pinning shows up in the journal one-time during startup.
if journalctl --user -u stave-synth --since "30 seconds ago" 2>/dev/null | grep -q "pinned to CPU 3"; then
    echo "${GREEN}  render thread pinned to CPU 3${NC}"
else
    echo "${RED}  CPU pin log line not found — check: journalctl --user -u stave-synth | grep 'pinned'${NC}"
fi

# Watchdog: heartbeat init logs once.
if journalctl --user -u stave-synth --since "30 seconds ago" 2>/dev/null | grep -q "systemd watchdog: heartbeat"; then
    echo "${GREEN}  systemd watchdog wired${NC}"
else
    echo "${RED}  watchdog heartbeat log not found — verify WatchdogSec= is in the deployed unit${NC}"
fi

# Render thread TID + actual core
RTID="$(journalctl --user -u stave-synth --since "30 seconds ago" 2>/dev/null | grep -oP 'render thread: pinned' | wc -l)"
PID="$(systemctl --user show stave-synth -p MainPID --value)"
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
    CORE3_THREADS="$(ps -eLo pid,tid,psr,comm | awk -v p="$PID" '$1==p && $3==3 {print}' | wc -l)"
    if [ "$CORE3_THREADS" -ge 1 ]; then
        echo "${GREEN}  $CORE3_THREADS thread(s) currently on core 3 (render thread)${NC}"
    else
        echo "${ORANGE}  no threads on core 3 yet (render thread may not have started — try again in a few seconds)${NC}"
    fi
fi

echo
echo "${GREEN}Done.${NC} Verify by playing — pre-reboot xrun count was the baseline; post-reboot should be lower."
