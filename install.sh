#!/usr/bin/env bash
# Install files only. Starting the service is an explicit, separate action.
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")"
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash install.sh [--replace]' >&2; exit 1; }
[[ $# -eq 0 || ( $# -eq 1 && $1 == --replace ) ]] || { echo 'Usage: install.sh [--replace]' >&2; exit 1; }
target=/usr/local/lib/huawei-fan/huawei_fan_table.py
if systemctl is-active --quiet huawei-fan.service; then
    echo 'Stop the existing service first: sudo systemctl stop huawei-fan.service' >&2
    exit 1
fi
if [[ -e $target && ${1:-} != --replace ]] && ! cmp -s huawei_fan_table.py "$target"; then
    echo 'Existing script differs. Copy your TABLE into this source, then use --replace.' >&2
    echo 'The installer does not silently discard or import existing settings.' >&2
    exit 1
fi
python3 ./huawei_fan_table.py check
install -d -m 0755 /usr/local/lib/huawei-fan
install -m 0644 huawei_fan_table.py "$target"
install -m 0644 systemd/huawei-fan.service /etc/systemd/system/huawei-fan.service
systemd-analyze verify /etc/systemd/system/huawei-fan.service
systemctl daemon-reload
printf '%s\n' 'Installed; no EC parameters have been changed by this installer.' \
  'Inspect hardware: sudo modprobe acpi_call; sudo python3 /usr/local/lib/huawei-fan/huawei_fan_table.py show' \
  'Start explicitly: sudo systemctl enable --now huawei-fan.service'
