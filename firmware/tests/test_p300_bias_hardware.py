"""Compile unchanged production function bodies with host IO/scheduler stubs.

This validates transaction/rollback logic, not electrical or IRQ timing behavior.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


def function_body(source, name):
    match = re.search(r'^(?:static )?(?:esp_err_t|eeg_acq_bias_switch_status_t)\s+'
                      + re.escape(name) + r'\([^;]+?\)\s*\{', source, re.M)
    if match is None:
        raise AssertionError('Missing production function '+name)
    start = source.index('{', match.start())
    depth = 1
    end = start+1
    while depth:
        depth += (source[end]=='{') - (source[end]=='}')
        end += 1
    return source[match.start():end]+'\n'


@unittest.skipUnless(shutil.which('gcc'), 'Host gcc required')
class BiasHardwareTests(unittest.TestCase):
    def test_actual_register_functions_and_acquisition_rollback(self):
        root = Path(__file__).resolve().parents[1]
        driver = (root/'components/ads1299/ads1299.c').read_text()
        acquire = (root/'components/eeg_acq/eeg_acq.c').read_text()
        fixture = root/'tests/fixtures'
        code = (fixture/'bias_hardware_prefix.h').read_text()
        for name in ('ads1299_apply_verified_reference_config_impl',
                     'ads1299_apply_verified_reference_config_with_bias',
                     'ads1299_set_bias_sensp_verified'):
            code += function_body(driver, name)
        code += function_body(acquire, 'eeg_acq_set_normal_bias_mask')
        code += (fixture/'bias_hardware_cases.c').read_text()
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'bias.c'
            source.write_text(code)
            binary=Path(folder)/'bias.exe'
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-function', '-I'+str(root/'components/ads1299/include'),
                str(source), '-o', str(binary)], check=True, capture_output=True)
            result=subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('PASS:', result.stdout)
