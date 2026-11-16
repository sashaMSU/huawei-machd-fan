#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Maintain the EC fan table on MACHD-WXX9-PCB, BIOS/EC 1.67 only.

Python 3.10+, Linux, root and acpi_call. No temperature cutoff, sleep hook or
recovery file. The EC, not this process, runs the automatic fan controller.
Run `check` before installation; `show` reads hardware; `restore` writes factory
values. SIGINT/SIGTERM request best-effort factory restoration before exit.
"""

import argparse
from contextlib import contextmanager
import fcntl
import itertools
import json
import math
import os
from pathlib import Path
import re
import signal
import struct
import sys
import threading

# Пауза после каждого прохода (само чтение EC занимает дополнительное время).
INTERVAL = 5
TABLE = {
    #                 CPU EC, SEN1, Charger (°C)
    "step0":          (75,    52,   63),
    "step1":          (90,    55,   68),
    "step2":          (95,    59,   75),
    "step3":          (100,   61,   80),
    "step4":          (102,   65,   85),
    "low":            (75,    52,   63),
    "high":           (90,    55,   68),
    "kp":             (5,     3,    3),
    "ki":             (0,     0,    0),
    "kd":             (0,     0,    0),
    #                 SEN1, Charger (°C)
    "on":             (48, 50),
    "off":            (45, 47),
    "switch":         (52, 55),  # SEN1: high -> low, low -> high
    #                 Ступени 0..5: пределы RPM первого вентилятора
    "rpm0":           (3600, 3900, 4450, 5000, 5600, 7300),
    "rpm1":           (4400, 5000, 5500, 6100, 7000, 7600),
    "rpm2":           (4200, 4600, 5400, 6400, 7100, 7800),
    "preset":         1,
    "minimum_rpm":    3100,
    "filter":         (0.98, 0.02),  # Предыдущее, новое измерение
    "slew":           (50, 60),      # Рост, снижение RPM за цикл EC
}

# Независимая заводская таблица: НЕ меняйте её вместе с TABLE.
FACTORY = {
    "step0": (75, 47, 63), "step1": (90, 50, 68),
    "step2": (95, 54, 75), "step3": (100, 56, 80), "step4": (102, 60, 85),
    "low": (75, 47, 63), "high": (90, 50, 68),
    "kp": (5, 3, 3), "ki": (0, 0, 0), "kd": (0, 0, 0),
    "on": (43, 50), "off": (40, 47), "switch": (47, 50),
    "rpm0": (3600, 3900, 4450, 5000, 5600, 7300),
    "rpm1": (4400, 5000, 5500, 6100, 7000, 7600),
    "rpm2": (4200, 4600, 5400, 6400, 7100, 7800),
    "preset": 1, "minimum_rpm": 4100,
    "filter": (0.98, 0.02), "slew": (50, 20),
}

BASE, SIZE = 0x403C, 472
RDER = r"\_SB.PC00.LPCB.H_EC.RDER"
WTER = r"\_SB.PC00.LPCB.H_EC.WTER"
CALL_FILE = "/proc/acpi/call"
LOCK_FILE = "/run/huawei-fan167.lock"
IDENTITY = {"sys_vendor": "HUAWEI", "product_name": "MACHD-WXX9",
            "board_name": "MACHD-WXX9-PCB", "bios_version": "1.67"}

# key: (RDER offset, scalar format, count, minimum, maximum).
# Bounds reject implausible configuration, not measured temperatures; they are
# NOT vendor-approved operating limits. Nonzero I/D are deliberately unsupported
# until their state handling has been independently verified for this release.
FIELDS = {
    **{f"step{i}": (0x40D0 + 24 * i, "f", 3, 0, 125) for i in range(5)},
    "low": (0x419C, "f", 3, 0, 125), "high": (0x41B4, "f", 3, 0, 125),
    "kp": (0x41CC, "f", 3, 0, 100),
    "ki": (0x41E4, "f", 3, 0, 0), "kd": (0x41FC, "f", 3, 0, 0),
    "on": (0x405C, "f", 2, 0, 125), "off": (0x4064, "f", 2, 0, 125),
    "switch": (0x4080, "f", 2, 0, 125),
    **{f"rpm{i}": (0x4154 + 24 * i, "I", 6, 1000, 8200) for i in range(3)},
    "preset": (0x4074, "I", 1, 0, 2),
    "minimum_rpm": (0x406C, "I", 1, 1000, 8200),
    "filter": (0x4044, "f", 2, 0, 1), "slew": (0x409C, "I", 2, 1, 1000),
}
WRITABLE = {a + i for a, _, n, _, _ in FIELDS.values() for i in range(n * 4)}


class ECError(RuntimeError):
    """No verified access/write path for the current state."""


def validate(table, intermediate=False):
    if set(table) != set(FIELDS):
        raise ValueError("TABLE has missing or unknown keys")
    for key, (_, fmt, count, lo, hi) in FIELDS.items():
        values = (table[key],) if count == 1 else table[key]
        if not isinstance(values, (tuple, list)) or len(values) != count:
            raise ValueError(f"{key}: expected {count} value(s)")
        for value in values:
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not lo <= value <= hi or (fmt == "I" and type(value) is not int)):
                raise ValueError(f"{key}: invalid value {value!r}; range {lo}..{hi}")
    for i in range(3):
        if any(table[f"step{j}"][i] >= table[f"step{j+1}"][i] for j in range(4)):
            raise ValueError("step thresholds must be strictly increasing per sensor")
        if table["low"][i] > table["high"][i]:
            raise ValueError("low target must not exceed high target")
    if any(a >= b for a, b in zip(table["off"], table["on"])):
        raise ValueError("off must be below on for each sensor")
    if table["switch"][0] >= table["switch"][1]:
        raise ValueError("switch down must be below switch up")
    for key in ("rpm0", "rpm1", "rpm2"):
        if any(a > b for a, b in zip(table[key], table[key][1:])):
            raise ValueError(f"{key} must be nondecreasing")
    weight = sum(table["filter"])
    if table["filter"][1] <= 0 or not 0 < weight <= 1.000001:
        raise ValueError("filter needs a positive new weight and sum <= 1")
    if not intermediate and abs(weight - 1) > 1e-6:
        raise ValueError("filter weights must sum to 1")


def decode(block):
    result = {}
    for key, (address, fmt, count, _, _) in FIELDS.items():
        values = struct.unpack_from("<" + str(count) + fmt, block, address - BASE)
        result[key] = values[0] if count == 1 else values
    return result


def encode(table, template):
    block = bytearray(template)
    for key, (address, fmt, count, _, _) in FIELDS.items():
        values = (table[key],) if count == 1 else table[key]
        struct.pack_into("<" + str(count) + fmt, block, address - BASE, *values)
    return bytes(block)


def factory_block():
    """Reconstruct configuration from the retained live read-only snapshot."""
    block = bytearray(SIZE)
    header = {0: 6, 4: 0x840A4, 16: 6, 20: 0x840BC, 24: 0, 28: 1,
              52: 3, 60: 0x84148, 64: 0, 76: 0x8419C, 80: 0x841B4,
              84: 0x841CC, 88: 0x841E4, 92: 0x841FC}
    for offset, value in header.items():
        struct.pack_into("<I", block, offset, value)
    struct.pack_into("<6I", block, 0x40A4 - BASE, 8, 0, 1, 10, 11, 9)
    struct.pack_into("<5I", block, 0x40BC - BASE,
                     0x840D0, 0x840E8, 0x84100, 0x84118, 0x84130)
    struct.pack_into("<3I", block, 0x4148 - BASE, 0x84154, 0x8416C, 0x84184)
    for address in [0x40D0 + i * 24 for i in range(5)] + [0x419C, 0x41B4]:
        struct.pack_into("<3f", block, address + 12 - BASE, 999, 999, 999)
    struct.pack_into("<3f", block, 0x41CC + 12 - BASE, 2, 2, 2)
    return encode(FACTORY, block)


FACTORY_BLOCK = factory_block()


def check_layout(block):
    if len(block) != SIZE:
        raise ECError("Incomplete EC configuration")
    for i, value in enumerate(block):
        if BASE + i not in WRITABLE and value != FACTORY_BLOCK[i]:
            raise ECError(f"Unknown EC layout at 0x{BASE+i:04X}; no writes")


def plan_writes(current, desired):
    """Plan byte writes, keeping intermediate scalar/table values plausible.

    WTER is not atomic. This avoids torn NaNs/out-of-range values, but does not
    stop the EC from using an intermediate configuration between writes.
    """
    check_layout(current)
    check_layout(desired)
    validate(decode(current), intermediate=True)
    validate(decode(desired))
    block, writes = bytearray(current), []
    pending = [(a + 4 * j, fmt) for a, fmt, n, _, _ in FIELDS.values()
               for j in range(n) if current[a+4*j-BASE:a+4*j-BASE+4]
               != desired[a+4*j-BASE:a+4*j-BASE+4]]
    while pending:
        for address, fmt in pending:
            start = address - BASE
            indices = [i for i in range(4) if block[start+i] != desired[start+i]]
            endpoints = [struct.unpack_from("<" + fmt, b, start)[0]
                         for b in (block, desired)]
            for order in itertools.permutations(indices):
                trial = bytearray(block)
                try:
                    for i in order:
                        trial[start+i] = desired[start+i]
                        value = struct.unpack_from("<" + fmt, trial, start)[0]
                        if fmt == "f" and value > max(endpoints):
                            raise ValueError("Intermediate float exceeds both endpoints")
                        validate(decode(trial), intermediate=True)
                except ValueError:
                    continue
                writes.extend((address+i, desired[start+i]) for i in order)
                block = trial
                pending.remove((address, fmt))
                break
            else:
                continue
            break
        else:
            raise ECError("No verified byte-write order; nothing applied")
    return writes


class EC:
    def request(self, method, address, value=None):
        if not 0 <= address < 0xC000:
            raise ECError("Address outside verified RAM window")
        if method == RDER and value is None:
            command = f"{RDER} 0x{address:04X}\n"
        elif method == WTER and address in WRITABLE and type(value) is int and 0 <= value <= 255:
            command = f"{WTER} 0x{address:04X} 0x{value:02X}\n"
        else:
            raise ECError("Request outside write whitelist")
        data = command.encode("ascii")
        with open(CALL_FILE, "wb", buffering=0) as stream:
            if stream.write(data) != len(data):
                raise ECError("Short ACPI request")
        with open(CALL_FILE, "rb", buffering=0) as stream:
            reply = stream.read(4096).split(b"\0", 1)[0].strip()
        if not re.fullmatch(rb"0x[0-9A-Fa-f]+", reply):
            raise ECError(f"ACPI reply: {reply!r}")
        number = int(reply, 16)
        if not 0 <= number <= 255:
            raise ECError("ACPI reply is not a byte")
        return number

    def read(self, address, size):
        return bytes(self.request(RDER, address+i) for i in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def write(self, address, value):
        if self.request(WTER, address, value) != 0:
            raise ECError(f"WTER failed at 0x{address:04X}")
        if self.read(address, 1) != bytes([value]):
            raise ECError(f"Readback mismatch at 0x{address:04X}")


def check_identity(root=Path("/sys/class/dmi/id")):
    found = {key: (root / key).read_text().strip() for key in IDENTITY}
    if found != IDENTITY:
        raise ECError("Supported only: HUAWEI MACHD-WXX9-PCB, BIOS 1.67")


def check_ec(ec):
    if ec.read(0x0700, 3) != b"\x01\x43\x00" or ec.u32(0x3164) != 0x8403C:
        raise ECError("EC version/configuration pointer changed; no writes")
    if ec.read(0x2C20, 1)[0] & 0x0A:
        raise ECError("Manual fan mode detected; no automatic-table writes")


def read_config(ec):
    check_ec(ec)
    block = ec.read(BASE, SIZE)
    check_ec(ec)
    check_layout(block)
    return block


def reconcile(ec, table):
    validate(table)
    current = read_config(ec)
    desired = encode(table, FACTORY_BLOCK)
    writes = plan_writes(current, desired)
    # Verify the full preimage before the first byte; another utility must not
    # race this process. The lock cannot serialize firmware or unrelated tools.
    if writes and read_config(ec) != current:
        raise ECError("Configuration changed while planning; retry")
    for address, value in writes:
        check_ec(ec)
        ec.write(address, value)
    if writes and read_config(ec) != desired:
        raise ECError("Final table verification failed; retry")
    return len(writes)


def temperature(value):
    return f"{value:.1f}" if math.isfinite(value) and -20 <= value <= 150 else "?"


def status_line(ec, written=0):
    if (ec.u32(0x3174) != 0x84214 or ec.read(0x4214, 12)
            != struct.pack("<3I", 0x84240, 0x84258, 0x84270)):
        raise ECError("Runtime pointers changed")
    temps = struct.unpack("<3f", ec.read(0x4258, 12))
    rpm = struct.unpack("<2H", ec.read(0x2A00, 4))
    commands = tuple(v * 100 for v in ec.read(0x2A04, 2))
    target, high, enabled, step = [ec.u32(a) for a in (0x422C, 0x4234, 0x4238, 0x423C)]
    preset = ec.u32(0x4074)
    limit = ec.u32(0x4154 + preset * 24 + step * 4) if preset < 3 and step < 6 else "?"
    mode = ("high" if high else "low") if high in (0, 1) else "?"
    return (f"mode=auto/{mode} enabled={enabled} preset={preset} step={step} "
            f"rpm={rpm[0]}/{rpm[1]} target={target} command={commands[0]}/{commands[1]} "
            f"limit={limit} CPU_EC={temperature(temps[0])} SEN1={temperature(temps[1])} "
            f"Charger={temperature(temps[2])} written={written}")


@contextmanager
def instance_lock(path=LOCK_FILE):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ECError("Another instance holds the EC lock") from error
        yield
    finally:
        os.close(fd)


def run(ec, stop, log=print):
    managed = False
    try:
        while not stop.is_set():
            try:
                # Arm restoration before reconcile: an interrupted request may
                # already have changed EC RAM even if no success was returned.
                check_ec(ec)
                managed = True
                written = reconcile(ec, TABLE)
                log(status_line(ec, written), flush=True)
            except (ECError, OSError, ValueError, struct.error) as error:
                log(f"error={error}; retry after {INTERVAL}s", flush=True)
            stop.wait(INTERVAL)
    finally:
        if managed:
            reconcile(ec, FACTORY)
            log("factory_restored verified=true", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="run",
                        choices=("run", "show", "restore", "check", "defaults"))
    args = parser.parse_args()
    try:
        if args.action == "defaults":
            print(json.dumps(FACTORY, indent=2))
            return 0
        if args.action in ("run", "check"):
            validate(TABLE)
            if type(INTERVAL) not in (int, float) or not math.isfinite(INTERVAL) or INTERVAL < 1:
                raise ValueError("INTERVAL must be finite and at least 1 second")
        if args.action == "check":
            count = len(plan_writes(FACTORY_BLOCK, encode(TABLE, FACTORY_BLOCK)))
            print(f"TABLE valid; {count} planned byte writes from factory; no hardware accessed")
            return 0
        if os.geteuid() != 0:
            raise ValueError("Use sudo for run/show/restore")
        check_identity()
    except (ECError, OSError, ValueError) as error:
        print(f"error={error}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
    try:
        with instance_lock():
            ec = EC()
            if args.action == "run":
                run(ec, stop)
            elif args.action == "restore":
                reconcile(ec, FACTORY)
                print("factory_restored verified=true", flush=True)
            else:
                block = read_config(ec)
                print(json.dumps(decode(block), indent=2))
                print(status_line(ec))
    except (ECError, OSError, ValueError, struct.error) as error:
        print(f"error={error}; restoration NOT confirmed; use restore after fixing access",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
