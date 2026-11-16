"""Offline tests. Never open /proc/acpi/call or access a real EC."""
import copy
import json
import math
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import huawei_fan_table as fan

ROOT = Path(__file__).resolve().parents[1]


class FakeEC:
    def __init__(self, block=None):
        self.memory = bytearray(0xC000)
        self.memory[fan.BASE:fan.BASE+fan.SIZE] = block or fan.FACTORY_BLOCK
        self.memory[0x700:0x703] = b'\x01\x43\x00'
        struct.pack_into('<I', self.memory, 0x3164, 0x8403C)
        struct.pack_into('<I', self.memory, 0x3174, 0x84214)
        struct.pack_into('<5I', self.memory, 0x4214,
                         0x84240, 0x84258, 0x84270, 0x84288, 0x842A0)
        struct.pack_into('<3f', self.memory, 0x4258, 50, 44, 35)
        struct.pack_into('<2H', self.memory, 0x2A00, 3150, 3060)
        self.memory[0x2A04:0x2A06] = bytes([31, 29])
        struct.pack_into('<I', self.memory, 0x422C, 3100)
        struct.pack_into('<I', self.memory, 0x4238, 1)
        self.writes = []
        self.fail_at = None
        self.fail_after = False
        self.on_write = None

    def read(self, address, size):
        return bytes(self.memory[address:address+size])

    def u32(self, address):
        return struct.unpack('<I', self.read(address, 4))[0]

    def write(self, address, value):
        assert address in fan.WRITABLE
        number = len(self.writes)
        if number == self.fail_at and not self.fail_after:
            raise fan.ECError('simulated failure before write')
        self.memory[address] = value
        self.writes.append((address, value))
        if self.on_write:
            self.on_write()
        if number == self.fail_at and self.fail_after:
            raise fan.ECError('simulated failure after write')


class ConfigurationTests(unittest.TestCase):
    def test_exact_requested_table(self):
        expected = copy.deepcopy(fan.FACTORY)
        for i in range(5):
            a,b,c=expected[f'step{i}']; expected[f'step{i}']=(a,b+5,c)
        expected.update(low=(75,52,63), high=(90,55,68), on=(48,50),
                        off=(45,47), switch=(52,55), minimum_rpm=3100, slew=(50,60))
        self.assertEqual(fan.TABLE, expected)
        self.assertEqual(fan.INTERVAL, 5)

    def test_factory_snapshot_header(self):
        data=json.loads((ROOT/'docs/evidence/ec167-factory.json').read_text())
        self.assertEqual(fan.FACTORY_BLOCK[:104].hex(),
                         data['automatic_curve']['config_raw_hex'])

    def test_factory_snapshot_arrays(self):
        a=json.loads((ROOT/'docs/evidence/ec167-factory.json').read_text())['automatic_curve']
        for i, row in enumerate(a['thresholds_C']):
            self.assertEqual(fan.FACTORY[f'step{i}'], tuple(row[:3]))
        for i, row in enumerate(a['rpm_presets']):
            self.assertEqual(fan.FACTORY[f'rpm{i}'], tuple(row))
        for key, source in [('low','low_targets'),('high','high_targets'),
                            ('kp','kp'),('ki','ki'),('kd','kd')]:
            self.assertEqual(fan.FACTORY[key], tuple(a[source][:3]))

    def test_factory_is_independent_of_user_table(self):
        saved=copy.deepcopy(fan.FACTORY)
        with patch.dict(fan.TABLE, minimum_rpm=3000):
            self.assertEqual(fan.FACTORY, saved)
            self.assertEqual(fan.decode(fan.FACTORY_BLOCK)['minimum_rpm'], 4100)

    def test_encode_decode_roundtrip(self):
        for table in (fan.FACTORY, fan.TABLE):
            result=fan.decode(fan.encode(table, fan.FACTORY_BLOCK))
            for key in table:
                if key != 'filter': self.assertEqual(result[key], table[key])
            self.assertAlmostEqual(sum(result['filter']), 1, places=6)

    def test_invalid_configurations(self):
        bad = [('preset',3),('preset',True),('minimum_rpm',-1),('slew',(50,0)),
               ('kp',(math.nan,3,3)),('low',(math.inf,52,63)),('filter',(.98,.1)),
               ('filter',(1,0)),('switch',(55,52)),('off',(48,50)),
               ('step1',(70,50,60)),('low',(91,52,63)),('rpm0',(5000,4000,4450,5000,5600,7300)),
               ('ki',(0,.1,0)),('kd',(0,.1,0)),('on',(48,))]
        for key,value in bad:
            with self.subTest(key=key,value=value):
                table=dict(fan.TABLE); table[key]=value
                with self.assertRaises(ValueError): fan.validate(table)

    def test_unknown_and_missing_keys(self):
        a=dict(fan.TABLE); a['wrong']=1
        with self.assertRaises(ValueError): fan.validate(a)
        del a['wrong'];del a['slew']
        with self.assertRaises(ValueError): fan.validate(a)

    def test_plan_has_14_byte_changes(self):
        dest=fan.encode(fan.TABLE,fan.FACTORY_BLOCK)
        writes=fan.plan_writes(fan.FACTORY_BLOCK,dest)
        self.assertEqual(len(writes),14)
        self.assertTrue(all(a in fan.WRITABLE for a,_ in writes))

    def test_every_intermediate_profile_is_valid(self):
        dest=fan.encode(fan.TABLE,fan.FACTORY_BLOCK)
        for initial,desired in [(fan.FACTORY_BLOCK,dest),(dest,fan.FACTORY_BLOCK)]:
            block=bytearray(initial)
            for a,value in fan.plan_writes(initial,desired):
                block[a-fan.BASE]=value
                fan.check_layout(block)
                fan.validate(fan.decode(block),intermediate=True)
            self.assertEqual(bytes(block),desired)

    def test_arbitrary_exposed_parameter_transitions(self):
        table=dict(fan.TABLE)
        table.update(kp=(6,4,4),filter=(.95,.05),preset=2,
                     rpm2=(4300,4700,5500,6500,7200,7900),slew=(60,80))
        dest=fan.encode(table,fan.FACTORY_BLOCK)
        fan.validate(table)
        for a,b in [(fan.FACTORY_BLOCK,dest),(dest,fan.FACTORY_BLOCK)]:
            block=bytearray(a)
            for address,value in fan.plan_writes(a,b):
                block[address-fan.BASE]=value
                fan.validate(fan.decode(block),intermediate=True)
            self.assertEqual(bytes(block),b)

    def test_all_immutable_bytes_are_checked(self):
        for i in range(fan.SIZE):
            if fan.BASE+i not in fan.WRITABLE:
                block=bytearray(fan.FACTORY_BLOCK);block[i]^=1
                with self.assertRaises(fan.ECError): fan.check_layout(block)


class ControllerTests(unittest.TestCase):
    def test_apply_then_noop_then_restore(self):
        ec=FakeEC()
        self.assertEqual(fan.reconcile(ec,fan.TABLE),14)
        count=len(ec.writes)
        self.assertEqual(fan.reconcile(ec,fan.TABLE),0)
        self.assertEqual(len(ec.writes),count)
        fan.reconcile(ec,fan.FACTORY)
        self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.FACTORY_BLOCK)

    def test_failure_before_and_after_every_apply_byte(self):
        for after in (False,True):
            for position in range(14):
                with self.subTest(after=after,position=position):
                    ec=FakeEC();ec.fail_at=position;ec.fail_after=after
                    with self.assertRaises(fan.ECError): fan.reconcile(ec,fan.TABLE)
                    ec.fail_at=None
                    fan.reconcile(ec,fan.FACTORY)
                    self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.FACTORY_BLOCK)

    def test_failure_during_restore_then_retry(self):
        desired=fan.encode(fan.TABLE,fan.FACTORY_BLOCK)
        n=len(fan.plan_writes(desired,fan.FACTORY_BLOCK))
        for position in range(n):
            ec=FakeEC(desired);ec.fail_at=position;ec.fail_after=True
            with self.assertRaises(fan.ECError): fan.reconcile(ec,fan.FACTORY)
            ec.fail_at=None
            fan.reconcile(ec,fan.FACTORY)
            self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.FACTORY_BLOCK)

    def test_partial_apply_can_resume_without_recovery_file(self):
        for position in range(14):
            ec=FakeEC();ec.fail_at=position;ec.fail_after=True
            with self.assertRaises(fan.ECError): fan.reconcile(ec,fan.TABLE)
            ec.fail_at=None
            fan.reconcile(ec,fan.TABLE)
            self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.encode(fan.TABLE,fan.FACTORY_BLOCK))

    def test_firmware_reset_reapplies(self):
        ec=FakeEC();fan.reconcile(ec,fan.TABLE)
        ec.memory[fan.BASE:fan.BASE+fan.SIZE]=fan.FACTORY_BLOCK
        self.assertEqual(fan.reconcile(ec,fan.TABLE),14)

    def test_bad_identity_and_manual_mode_never_write(self):
        for address in (0x700,0x3164,0x2C20,0x40A4):
            ec=FakeEC();ec.memory[address]^=2
            with self.subTest(address=hex(address)):
                with self.assertRaises(fan.ECError):fan.reconcile(ec,fan.TABLE)
                self.assertEqual(ec.writes,[])

    def test_other_board_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            for key,value in fan.IDENTITY.items(): (p/key).write_text(value)
            fan.check_identity(p)
            (p/'board_name').write_text('MACHD-WXX9-PCB-B1')
            with self.assertRaises(fan.ECError): fan.check_identity(p)

    def test_run_restores_after_stop(self):
        ec=FakeEC();stop=threading.Event();messages=[]
        def log(message,**_):
            messages.append(message)
            if message.startswith('mode='):stop.set()
        fan.run(ec,stop,log)
        self.assertTrue(any('written=14' in m for m in messages))
        self.assertEqual(messages[-1],'factory_restored verified=true')
        self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.FACTORY_BLOCK)

    def test_stop_during_write_completes_then_restores(self):
        ec=FakeEC();stop=threading.Event();ec.on_write=stop.set
        fan.run(ec,stop,lambda *args,**kwargs:None)
        self.assertEqual(ec.read(fan.BASE,fan.SIZE),fan.FACTORY_BLOCK)

    def test_high_temperature_does_not_prevent_writes(self):
        ec=FakeEC();struct.pack_into('<3f',ec.memory,0x4258,110,90,85)
        self.assertEqual(fan.reconcile(ec,fan.TABLE),14)
        self.assertIn('CPU_EC=110.0',fan.status_line(ec))

    def test_nan_temperature_logged_as_unknown(self):
        ec=FakeEC();struct.pack_into('<3f',ec.memory,0x4258,math.nan,math.inf,35)
        self.assertEqual(fan.reconcile(ec,fan.TABLE),14)
        text=fan.status_line(ec)
        self.assertIn('CPU_EC=?',text);self.assertIn('SEN1=?',text)

    def test_zero_rpm_is_valid(self):
        ec=FakeEC();struct.pack_into('<2H',ec.memory,0x2A00,0,0)
        self.assertIn('rpm=0/0',fan.status_line(ec))

    def test_log_distinguishes_target_command_and_actual(self):
        text=fan.status_line(FakeEC())
        for field in ('rpm=3150/3060','target=3100','command=3100/2900','limit=4400'):
            self.assertIn(field,text)

    def test_runtime_pointer_change(self):
        ec=FakeEC();ec.memory[0x3174]^=1
        with self.assertRaises(fan.ECError): fan.status_line(ec)

    def test_duplicate_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with fan.instance_lock(str(Path(d)/'lock')):
                with self.assertRaises(fan.ECError):
                    with fan.instance_lock(str(Path(d)/'lock')): pass

    def test_transport_write_whitelist_before_io(self):
        with patch('builtins.open',side_effect=AssertionError('must not access hardware')):
            with self.assertRaises(fan.ECError):fan.EC().request(fan.WTER,0x3164,1)
            with self.assertRaises(fan.ECError):fan.EC().request('FAKE',0x40D4,1)

    def test_check_and_defaults_no_hardware(self):
        for action in ('check','defaults'):
            result=subprocess.run([sys.executable,str(ROOT/'huawei_fan_table.py'),action],
                                  capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_real_signals_including_inherited_blocked_sigint(self):
        for sig,blocked in [(signal.SIGTERM,False),(signal.SIGINT,False),(signal.SIGINT,True)]:
            with self.subTest(signal=sig,blocked=blocked):
                code=f'''
import contextlib, signal, sys
from unittest.mock import patch
import huawei_fan_table as fan
from tests.test_controller import FakeEC
if {blocked!r}: signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGINT])
fan.INTERVAL=60
sys.argv=['fan', 'run']
with patch.object(fan,'EC',FakeEC), patch.object(fan,'check_identity'), \\
     patch.object(fan.os,'geteuid',return_value=0), \\
     patch.object(fan,'instance_lock',return_value=contextlib.nullcontext()):
    sys.exit(fan.main())
'''
                proc=subprocess.Popen([sys.executable,'-u','-c',code],cwd=ROOT,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(10),'child did not produce a sample')
                    line=proc.stdout.readline();self.assertIn('mode=auto/',line)
                    proc.send_signal(sig)
                    output,error=proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode,0,error)
                    self.assertIn('factory_restored verified=true',output)
                    selector.close()
                finally:
                    if proc.poll() is None:proc.kill();proc.communicate()


if __name__ == '__main__':
    unittest.main()
