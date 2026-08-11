#!/usr/bin/env bash
#
# Install the gpu-lease tool, its reaper timer and the sudoers grant.
#
#   sudo ./deploy/install.sh
#
# Idempotent: safe to re-run.
#
set -euo pipefail

SRC_DIR="$(dirname "$(readlink -f "$0")")"
PROJ_DIR="$(dirname "$SRC_DIR")"
BIN=/usr/local/bin/gpu-lease
DOC_DIR=/usr/local/share/gpu-lease

[ "$(id -u)" -eq 0 ] || { echo "must run as root (use sudo)" >&2; exit 1; }

say() { printf '\n=== %s ===\n' "$*"; }

say "1/4 installing the tool"
# Root-owned and not group/world writable. The sudoers grant below makes this
# binary equivalent to root for the runner user, so a writable copy would be a
# privilege escalation.
install -o root -g root -m 0755 "$PROJ_DIR/gpu-lease" "$BIN"
install -d -o root -g root -m 0755 "$DOC_DIR"
install -o root -g root -m 0644 "$PROJ_DIR/README.md" "$DOC_DIR/README.md"

perms=$(stat -c '%U %G %a' "$BIN")
[ "$perms" = "root root 755" ] || {
    echo "refusing to continue: $BIN has permissions '$perms', expected 'root root 755'" >&2
    exit 1
}
"$BIN" --help >/dev/null && echo "  installed and runnable"

say "2/4 installing the sudoers grant"
RUNNER_USER=$(sed -n 's/^\([a-z_][a-z0-9_-]*\) ALL=.*/\1/p' "$SRC_DIR/gpu-lease.sudoers" | head -1)
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
    echo "  warning: user '$RUNNER_USER' does not exist on this host" >&2
fi
install -m 0440 "$SRC_DIR/gpu-lease.sudoers" /etc/sudoers.d/gpu-lease
if ! visudo -cf /etc/sudoers.d/gpu-lease; then
    rm -f /etc/sudoers.d/gpu-lease
    echo "sudoers file rejected; removed it and aborting" >&2
    exit 1
fi
echo "  granted to: $RUNNER_USER"

say "3/4 installing the reaper timer"
install -m 0644 "$SRC_DIR/gpu-lease-reap.service" /etc/systemd/system/
install -m 0644 "$SRC_DIR/gpu-lease-reap.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gpu-lease-reap.timer
systemctl list-timers gpu-lease-reap.timer --no-pager | sed -n '2p;3p'

say "4/4 checking current state"
"$BIN" status

cat <<EOF

Done. In a workflow:

  LEASE=\$(sudo gpu-lease acquire --ttl 1800 --reason "\$GITHUB_WORKFLOW")
  ...                                     # GPU is yours
  sudo gpu-lease release "\$LEASE"        # ideally in an always() step

An unreleased lease is cleaned up by gpu-lease-reap.timer within a minute of
its TTL, so a killed runner cannot strand the GPU.
EOF
