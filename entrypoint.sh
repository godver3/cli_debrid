#!/bin/bash

set_permissions() {
    echo "Setting permissions for /user directory..."
    chmod -R 755 /user
    find /user -type f -exec chmod 644 {} +
    chown -R $PUID:$PGID /user
    echo "Permissions set successfully"
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

sleep 2
exec tail -F /user/logs/debug.log
