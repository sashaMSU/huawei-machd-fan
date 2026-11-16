#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash uninstall.sh' >&2; exit 1; }
script=/usr/local/lib/huawei-fan/huawei_fan_table.py
systemctl disable --now huawei-fan.service
# Also covers a previously crashed process. Leave the files installed if
# restoration fails: do not pretend the EC returned to factory settings.
if [[ -f $script ]]; then
    /usr/sbin/modprobe acpi_call
    /usr/bin/python3 "$script" restore
fi
rm -f /etc/systemd/system/huawei-fan.service "$script"
rmdir /usr/local/lib/huawei-fan 2>/dev/null || true
systemctl daemon-reload
printf '%s\n' 'Factory table verified; service and program removed.'
