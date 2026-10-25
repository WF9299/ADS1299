"""Run production C control/register transactions with host fault injection."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_p300_bias_hardware import function_body


@unittest.skipUnless(shutil.which('gcc'), 'Host gcc required')
class GainHardwareTests(unittest.TestCase):
    def compile_run(self, sources, includes):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder)/'gain.exe'
            command = ['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function']
            command += ['-I'+str(p) for p in includes]
            command += [str(p) for p in sources] + ['-o', str(binary)]
            built = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stdout+built.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_production_gain_registers_rollback_and_diagnostic_restore(self):
        root = Path(__file__).resolve().parents[1]
        driver = (root/'components/ads1299/ads1299.c').read_text()
        acq = (root/'components/eeg_acq/eeg_acq.c').read_text()
        fixtures = root/'tests/fixtures'
        code = (fixtures/'bias_hardware_prefix.h').read_text()
        code += (fixtures/'gain_hardware_prefix.h').read_text()
        for name in ('ads1299_gain_bits', 'ads1299_set_gain_verified',
                     'ads1299_apply_verified_reference_config_impl', 'ads1299_apply_normal_config',
                     'ads1299_set_bias_sensp_verified', 'ads1299_apply_internal_short_config'):
            code += function_body(driver, name)
        for name in ('apply_normal_config', 'apply_ads1299_capture_config',
                     'eeg_acq_set_normal_bias_mask', 'eeg_acq_set_normal_gain', 'eeg_acq_set_config_id'):
            code += function_body(acq, name)
        code += (fixtures/'gain_impedance_stubs.h').read_text()
        code += function_body(acq, 'eeg_acq_measure_impedance')
        code += (fixtures/'gain_hardware_cases.c').read_text()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'hardware.c'
            source.write_text(code)
            self.compile_run([source], [root/'components/ads1299/include'])

    def test_production_protocol_and_state(self):
        root = Path(__file__).resolve().parents[1]
        self.compile_run([root/'components/p300_stream/p300_protocol.c',
            root/'components/p300_stream/p300_gain_state.c', root/'tests/test_p300_gain_protocol.c'],
            [root/'tests/fixtures', root/'components/p300_stream/include'])
