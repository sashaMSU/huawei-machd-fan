# Fan control for Huawei MateBook X Pro 2021 (MACHD-WXX9)

A Linux script for adjusting your laptop's cooling: quieter fans under light
load, different start and ramp-up temperatures, and faster slowdown after a
workload ends.

It changes the settings of the built-in cooling controller (EC). The controller
still adjusts the fans automatically. No BIOS flashing is needed, and the
script does not change CPU power limits or Turbo Boost settings.

[Русский](README.md) · [All settings and factory defaults](docs/defaults.md)

## Compatibility

Only **MACHD-WXX9**, board **MACHD-WXX9-PCB**, **BIOS 1.67 / EC 1.67.0**.
**PCB-B1 and other models are not supported.** Do not bypass model or version
checks to run this on another laptop.

This is an experimental project, not a Huawei application. Lower fan speeds can
raise temperatures and cause thermal throttling. The script **does not stop
when temperatures get too high** and is not a thermal safety mechanism. Check
temperatures under load and make sure both fans start reliably from a standstill.

## Included settings

The supplied profile raises SEN1 temperature thresholds, lowers the minimum fan
target and allows faster slowdown:

| Setting | Factory | This profile |
|---|---|---|
| SEN1 step thresholds | 47, 50, 54, 56, 60 °C | 52, 55, 59, 61, 65 °C |
| SEN1 `low/high` targets | 47 / 50 °C | 52 / 55 °C |
| SEN1 `low/high` switching | 47 / 50 °C | 52 / 55 °C |
| SEN1 fan on / off | 43 / 40 °C | 48 / 45 °C |
| Minimum first-fan target | 4100 RPM | 3100 RPM |
| Target change per EC calculation: up / down | 50 / 20 RPM | 50 / 60 RPM |

All other settings retain their factory values. See
[docs/defaults.md](docs/defaults.md) for the complete table.
`Ki` and `Kd` must remain zero in this release.

## Installation

Requires Linux, Python 3.10+ and the `acpi_call` module. On Ubuntu / Kubuntu,
run these commands from the repository directory:

```bash
sudo apt install python3 acpi-call-dkms "linux-headers-$(uname -r)"
sudo modprobe acpi_call
python3 huawei_fan_table.py check
sudo python3 huawei_fan_table.py show
```

`check` validates settings without accessing hardware; `show` reads the EC.
Do not continue if a check fails. Do not run other tools that use
`/proc/acpi/call` at the same time.

Try it in a terminal first:

```bash
sudo python3 -u huawei_fan_table.py run
```

Press **Ctrl+C** to stop and wait for `factory_restored verified=true` to confirm
that the factory settings have been restored.

Then install the service and enable it at boot:

```bash
sudo bash install.sh
sudo systemctl enable --now huawei-fan.service
```

The installer does not start the service itself. When upgrading, stop the
service first, copy your `TABLE` into the new file and install it with
`sudo bash install.sh --replace`. Disable and remove any old
`huawei-fan-sleep.service` from earlier versions; it is not used here.

## Changing settings

Edit `TABLE` and `INTERVAL` at the top of the script. Leave `FACTORY`, memory
addresses and checks alone: `FACTORY` holds the values used for restoration.

For an installed service:

```bash
sudo systemctl stop huawei-fan.service
sudoedit /usr/local/lib/huawei-fan/huawei_fan_table.py
python3 /usr/local/lib/huawei-fan/huawei_fan_table.py check
sudo systemctl start huawei-fan.service
```

With `INTERVAL = 5`, the program pauses for five seconds between checks and
reapplies the settings if they were reset, including after sleep. This is not
the fan controller's update rate: the EC runs independently. File edits take
effect only after restarting the program.

`low/high` are temperature targets. `step` selects a fan-speed limit,
**not a fixed speed**. See [how the controller works](docs/algorithm.md).

## Logs

```bash
journalctl -u huawei-fan.service -f -o cat
```

Example:

```text
mode=auto/low enabled=1 preset=1 step=1 rpm=3150/3060 target=3100 command=3100/2900 limit=5000 CPU_EC=59.2 SEN1=52.6 Charger=46.2 written=0
```

`rpm` is the measured speed, `target` is the internal first-fan target,
`command` contains the command values for both fans, and `limit` is the current
step's cap. Temperatures are filtered by the EC; `CPU_EC` is not necessarily
the same as `coretemp`. `written=0` means no settings needed rewriting.

## Stopping and uninstalling

Stop the service and disable it at boot:

```bash
sudo systemctl disable --now huawei-fan.service
```

On a normal stop, the script attempts to restore factory settings. To retry
restoration manually, with the service already stopped:

```bash
sudo python3 /usr/local/lib/huawei-fan/huawei_fan_table.py restore
```

Restoration is not guaranteed after a crash, and a reboot is not a verified way
to reset the EC. Before booting Windows, wait for `factory_restored verified=true`
if you need the factory settings there.

To uninstall, run from the repository directory:

```bash
sudo bash uninstall.sh
```

Uninstallation stops if factory restoration cannot be verified.

## Further reading

[EC layout and sources](docs/hardware.md) · [Tests](docs/testing.md) ·
[Contributing](CONTRIBUTING.md)

License: [MIT](LICENSE).
