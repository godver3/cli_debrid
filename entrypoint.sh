#!/bin/bash

set_permissions() {
    echo "Setting permissions for /user directory..."
    chmod -R 755 /user
    find /user -type f -exec chmod 644 {} +
    chown -R $PUID:$PGID /user
    echo "Permissions set successfully"
}

# Xorg requires this shared socket directory to be owned by root and writable
# with the sticky bit. Create it before dropping to a custom PUID/PGID so Xvfb
# does not leave unusable user-owned sockets behind.
mkdir -p /tmp/.X11-unix
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
