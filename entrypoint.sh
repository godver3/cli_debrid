#!/bin/bash

set_permissions() {
    echo "Setting permissions for /user directory..."
    chmod -R 755 /user
    find /user -type f -exec chmod 644 {} +
    chown -R $PUID:$PGID /user
    prepare_mount_dirs
    echo "Permissions set successfully"
}

# The built-in mount engine runs as PUID: it can't create /mnt/clid itself,
# and fusermount3 refuses a mountpoint the user can't write to. Create the
# default and the configured mount path and hand them (not their contents) to
# PUID. A path that's already mounted is left alone. Only touches the host
# when the built-in mount is in use: /mnt/clid is created otherwise only if
# /mnt is the container's own filesystem (so a fresh install that picks the
# built-in mount in onboarding can mount without a restart), never inside a
# host /mnt bind used by Zurg/rclone setups.
prepare_mount_dirs() {
    local info in_use configured paths=""
    info=$(python3 -c "
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
up, bm = c.get('Usenet Provider') or {}, c.get('Built-in Mount') or {}
used = (up.get('enabled') and str(up.get('provider') or '').strip().lower() == 'builtin') or bm.get('enabled') or bm.get('run_alongside')
print('1' if used else '0')
print(bm.get('mount_path') or '')" "${USER_CONFIG:-/user/config}/config.json" 2>/dev/null)
    in_use=$(printf '%s\n' "$info" | sed -n 1p)
    configured=$(printf '%s\n' "$info" | sed -n 2p)
    if [ "$in_use" = "1" ] || [ "$(stat -c %d /mnt 2>/dev/null)" = "$(stat -c %d / 2>/dev/null)" ]; then
        paths="/mnt/clid"
    fi
    if [ "$in_use" = "1" ]; then
        case "$configured" in
            /*) paths="$paths
$configured" ;;
        esac
    fi
    printf '%s\n' "$paths" | while IFS= read -r p; do
        [ -n "$p" ] || continue
        mountpoint -q "$p" 2>/dev/null && continue
        mkdir -p "$p" 2>/dev/null && chown $PUID:$PGID "$p" 2>/dev/null
    done
}

clean_stale_x11_sockets() {
    for stale_socket in /tmp/.X11-unix/X*; do
        [ -S "$stale_socket" ] || continue

        display_number=${stale_socket##*/X}
        case "$display_number" in
            ''|*[!0-9]*) continue ;;
        esac

        # A running Xvfb owns both a socket and a matching lock file. Remove
        # only orphaned sockets while the entrypoint still has root privileges,
        # including sockets left by an older root-run container process.
        [ -e "/tmp/.X${display_number}-lock" ] && continue
        rm -f -- "$stale_socket"
        echo "Removed stale X11 socket: $stale_socket"
    done
}

# Xorg requires this shared socket directory to be owned by root and writable
# with the sticky bit. Create it before dropping to a custom PUID/PGID so Xvfb
# does not leave unusable user-owned sockets behind.
mkdir -p /tmp/.X11-unix
clean_stale_x11_sockets
chown root:root /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

if [ $PUID != 0 ] || [ $PGID != 0 ]; then
    echo "Starting with custom user - PUID: $PUID, PGID: $PGID"
    groupadd -g $PGID appuser
    useradd -u $PUID -g $PGID -d /app appuser
    set_permissions
    echo "Created user appuser with UID: $PUID and GID: $PGID"
    sed -i "s/user=root/user=appuser/" /etc/supervisor/conf.d/supervisord.conf
    echo "Updated supervisord configuration to use appuser"
else
    echo "Starting with root user (PUID=0, PGID=0)"
    set_permissions
fi

if [ $PUID != 0 ] || [ $PGID != 0 ]; then
    echo "Starting supervisord as appuser"
    gosu appuser supervisord -n -c /etc/supervisor/conf.d/supervisord.conf &
else
    echo "Starting supervisord as root"
    supervisord -n -c /etc/supervisor/conf.d/supervisord.conf &
fi
SUPERVISORD_PID=$!

# docker stop signals PID 1 (this script): pass it on to supervisord so every
# program shuts down cleanly (the built-in mount unmounts instead of leaving
# a dead mount behind) rather than being SIGKILLed when the timeout ends.
shutdown() {
    echo "Stopping supervisord..."
    kill -TERM "$SUPERVISORD_PID" 2>/dev/null
    wait "$SUPERVISORD_PID"
    kill "$TAIL_PID" 2>/dev/null
    exit 0
}
trap shutdown TERM INT

sleep 2
tail -F /user/logs/debug.log &
TAIL_PID=$!
wait "$SUPERVISORD_PID"
