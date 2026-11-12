"""Host integration: real C serialization -> gateway -> backend validators.

No broker or hardware. Skipped when sibling firmware/gateway repos are absent.
"""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from app import calibrations


class FirmwareWireContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        fw = root / 'firmware_ESP32_SelfIQ-26-2/firmware_ESP32'
        gw = root / 'firmware_RPI_SelfIQ-26-2/SelfIQ_RPI/selfiq/calibration.py'
        cjson = fw / 'test/third_party/cjson'
        if not (shutil.which('gcc') and gw.exists() and (cjson / 'cJSON.c').exists()):
            raise unittest.SkipTest('Requires sibling firmware/gateway repos, GCC and cJSON')
        spec = importlib.util.spec_from_file_location('gateway_calibration_contract', gw)
        cls.gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gateway)
        module = fw / 'main/modules/calibration'
        with tempfile.TemporaryDirectory(prefix='selfiq-wire-') as tmp:
            binary = Path(tmp) / 'wire'
            subprocess.run([
                'gcc', '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-DCAL_CONTRACT_ACKS',
                '-Wall', '-Wextra', '-Werror', '-Wno-error=deprecated-declarations',
                f'-I{fw / "main"}', f'-I{module}', f'-I{fw / "test/stubs"}',
                f'-I{cjson}', str(fw / 'test/test_calibration_store.c'),
                str(module / 'calibration_store.c'), str(module / 'calibration_core.c'),
                str(module / 'calibration_cmd.c'), str(cjson / 'cJSON.c'),
                '-lm', '-o', str(binary),
            ], check=True, capture_output=True, text=True)
            output = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        cls.acks = [json.loads(line.removeprefix('ACK_JSON '))
                    for line in output.stdout.splitlines() if line.startswith('ACK_JSON ')]

    def test_actual_firmware_acks_pass_both_validators(self):
        self.assertEqual(len(self.acks), 12)
        for ack in self.acks:
            with self.subTest(action=ack['action'], active=ack.get('active'), draft=ack.get('draft')):
                self.assertLess(len(json.dumps(ack, separators=(',', ':')).encode()), 1024)
                session = {'id': ack['calibration_id'], 'roi_id': ack['roi_id'],
                           'calibration_version': ack['calibration_version']}
                command = {key: ack[key] for key in (
                    'id', 'req_id', 'calibration_id', 'roi_id', 'calibration_version')}
                command['_action'] = ack['action']
                if ack['action'] == 'cancel':
                    command['context_hash'] = ack['context_hash']
                    session['context_hash'] = ack['context_hash']
                    calibrations.validate_ack({'id': ack['id'], 'action': 'cancel'}, session, ack)
                else:
                    calibrations.validate_status_ack(session, ack)
                self.gateway.validate_ack(command, ack)

    def test_partial_draft_reports_absent_full_without_fake_crc(self):
        ack = next(a for a in self.acks if a.get('draft') and a['draft']['full_crc'] is None)
        self.assertIsNone(ack['active'])
        self.assertRegex(ack['draft']['empty_crc'], r'^[0-9a-f]{8}$')
        broken = copy.deepcopy(ack)
        del broken['draft']['full_crc']
        with self.assertRaises(calibrations.AckError):
            calibrations.validate_status_ack({'id': ack['calibration_id'], 'roi_id': ack['roi_id']}, broken)
        with self.assertRaises(self.gateway.CalibrationCommandError):
            self.gateway._status_entry(broken['draft'], 'draft')

    def test_active_pair_cannot_have_null_crc(self):
        ack = copy.deepcopy(next(a for a in self.acks if a.get('active')))
        ack['active']['full_crc'] = None
        with self.assertRaises(calibrations.AckError):
            calibrations.validate_status_ack({'id': ack['calibration_id'], 'roi_id': ack['roi_id']}, ack)
        with self.assertRaises(self.gateway.CalibrationCommandError):
            self.gateway._status_entry(ack['active'], 'active')


if __name__ == '__main__':
    unittest.main()
