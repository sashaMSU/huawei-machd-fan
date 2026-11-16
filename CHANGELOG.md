# Changelog

## 0.1.0 — 2026-11-16

- Initial publication-ready source snapshot; not a hardware-certified release.
- Owner-selected SEN1 +5 °C thresholds/targets/on-off gate, 3100 RPM floor,
  and 50/60 RPM slew; polling pause 5 seconds.
- Independent factory defaults, exact DMI/EC/layout checks and byte readback.
- Foreground CLI, minimal systemd unit, explicit installer and uninstaller.
- Logs distinguish measured RPM, internal target, command fields and step cap.
- No thermal cutoff, sleep hook or persistent recovery file.
- I/D kept at zero pending a verified port of their state handling.
- Offline tests and CI.
