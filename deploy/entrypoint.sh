#!/bin/bash
set -Eeuo pipefail
umask 027

children=()
shutdown() {
  trap - EXIT TERM INT
  if ((${#children[@]})); then
    kill -TERM "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
}
trap shutdown EXIT
trap 'exit 0' TERM INT

install -d -m 755 /run/dbus /run/cups /run/avahi-daemon
install -d -m 750 -o root -g familyprint /run/family-print-center
install -d -m 700 -o familyprint -g familyprint /run/family-print-center/office-runtime
rm -f /run/family-print-center/office-runtime/ready.json
install -d -m 750 -o familyprint -g familyprint "${DATA_DIR:-/var/lib/family-print-center}"
install -d -m 755 /etc/cups/ppd /var/cache/cups
install -d -m 710 -o root -g lp /var/spool/cups
install -d -m 1770 -o root -g lp /var/spool/cups/tmp

# Queue definitions and PPDs are persistent; enforce the local-only service policy
# on every launch, including when upgrading an existing configuration volume.
install -m 640 -o root -g lp /opt/print-center/defaults/cupsd.conf /etc/cups/cupsd.conf
install -m 640 -o root -g lp /opt/print-center/defaults/cups-files.conf /etc/cups/cups-files.conf

# The browser remains password-free. Only local app-to-CUPS requests authenticate.
python3 - <<'PY'
import os
import secrets
import subprocess
from pathlib import Path

password = secrets.token_urlsafe(36)
target = Path('/run/family-print-center/cups-password')
target.write_text(password + '\n')
os.chmod(target, 0o640)
os.chown(target, 0, 10000)
subprocess.run(['chpasswd'], input=f'familyprint:{password}\n', text=True, check=True)
PY

dbus-daemon --system --nofork --nopidfile &
children+=("$!")
dbus_ready=0
for attempt in {1..50}; do
  if dbus-send --system --print-reply --type=method_call \
      --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    dbus_ready=1
    break
  fi
  kill -0 "${children[0]}" 2>/dev/null || { echo 'D-Bus failed to start.' >&2; exit 1; }
  sleep 0.1
done
[[ "$dbus_ready" == 1 ]] || { echo 'D-Bus startup timed out.' >&2; exit 1; }

avahi-daemon --no-chroot --no-rlimits &
children+=("$!")
avahi_ready=0
for attempt in {1..50}; do
  if dbus-send --system --print-reply --reply-timeout=1000 --type=method_call \
      --dest=org.freedesktop.Avahi / org.freedesktop.Avahi.Server.GetVersionString >/dev/null 2>&1; then
    avahi_ready=1
    break
  fi
  for child in "${children[@]}"; do
    kill -0 "$child" 2>/dev/null || { echo 'The discovery service failed to start.' >&2; exit 1; }
  done
  sleep 0.1
done
[[ "$avahi_ready" == 1 ]] || { echo 'Avahi startup timed out.' >&2; exit 1; }

cupsd -f &
children+=("$!")

cups_ready=0
for attempt in {1..60}; do
  if [[ -S /run/cups/cups.sock ]]; then
    cups_ready=1
    break
  fi
  for child in "${children[@]}"; do
    kill -0 "$child" 2>/dev/null || { echo 'A print service failed to start.' >&2; exit 1; }
  done
  sleep 0.5
done
[[ "$cups_ready" == 1 ]] || { echo 'CUPS startup timed out.' >&2; exit 1; }
for child in "${children[@]}"; do
  kill -0 "$child" 2>/dev/null || { echo 'A print service failed to start.' >&2; exit 1; }
done

# Binary presence and a running CUPS do not prove that headless Office works.
# Exercise the same safe preview path once, as the application user, with only
# generated documents. No printer is selected and no print job is submitted.
runuser -u familyprint -- python -m print_center.office_probe \
  --marker /run/family-print-center/office-runtime/ready.json \
  || { echo 'Office startup check failed; see the troubleshooting tutorial.' >&2; exit 1; }

runuser -u familyprint -- "$@" &
children+=("$!")

# A failed child shuts down the whole service so Docker can restart it.
set +e
wait -n "${children[@]}"
status=$?
set -e
echo "A print-center process stopped (status $status)." >&2
[[ "$status" -ne 0 ]] || status=1
exit "$status"
