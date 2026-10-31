#!/bin/sh
# LeapMotor Mate server — remove it.
#
#   sudo ./packaging/linux/uninstall.sh            keeps your data and configuration
#   sudo ./packaging/linux/uninstall.sh --purge    also deletes ALL data (trips, charges, login)
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

systemctl disable --now leapmotor-mate.service 2>/dev/null || true
rm -f /etc/systemd/system/leapmotor-mate.service /usr/local/bin/mate-server
systemctl daemon-reload
rm -rf /opt/leapmotor-mate

if [ "$PURGE" -eq 1 ]; then
  rm -rf /var/lib/leapmotor-mate /etc/leapmotor-mate
  userdel leapmotor-mate 2>/dev/null || true
  echo "LeapMotor Mate and all its data removed."
else
  echo "LeapMotor Mate removed. Data kept in /var/lib/leapmotor-mate, configuration in /etc/leapmotor-mate."
  echo "Run again with --purge to delete them too."
fi
