import json
from pathlib import Path
import queue
import socket
import threading
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import numpy as np
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.receiver import WorkerController, create_server, validate_packet
from ssvep_realtime_test.simulator import samples


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def payload(start, data=None):
    return {"data": np.zeros((200, 8)).tolist() if data is None else data.tolist(),
            "timestamp": start, "sampling_rate": 1000, "channels": 8, "device": "SSVEP_SIMULATOR",
            "batch": True, "markers": [0] * (200 if data is None else len(data)), "eeg_type": 0}


def post(port, value, path="/api/bci/data"):
    with urlopen(Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(value).encode(),
                         headers={"Content-Type": "application/json"}), timeout=2) as response:
        return response.status


class HttpTests(unittest.TestCase):
    def test_monitor_schema_and_validation(self):
        p = payload(time.time())
        self.assertEqual(validate_packet(p)["sampling_rate"], 1000)
        for field, value in (("channels", 7), ("data", [[1, 2]]), ("markers", []),
                             ("sampling_rate", 256), ("timestamp", float("nan"))):
            bad = dict(p, **{field: value})
            with self.assertRaises(ValueError):
                validate_packet(bad)

    def test_http_path_errors_and_bounded_queue(self):
        packets = queue.Queue(maxsize=1)
        server = create_server(0, packets)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
        thread.start()
        try:
            self.assertEqual(post(port, payload(time.time())), 200)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            for body, path, expected in ((payload(time.time()), "/api/bci/data", 429),
                                         ({}, "/api/bci/data", 400), ({}, "/wrong", 404)):
                with self.assertRaises(HTTPError) as ctx:
                    post(port, body, path)
                self.assertEqual(ctx.exception.code, expected)
                ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_process_real_http_analysis_gap_stale_and_cleanup(self):
        port = free_port()
        controller = WorkerController(Settings(port=port, window_seconds=1, update_seconds=.1, stale_seconds=.6))
        controller.start()
        controller.begin()
        rng = np.random.default_rng(7)
        results = []
        try:
            epoch = time.time()
            base = time.monotonic()
            for i in range(9):
                time.sleep(max(0, base + (i + 1) * .2 - time.monotonic()))
                post(port, payload(epoch + i * .2, samples(17, 1000, i * 200, 200, rng)))
                time.sleep(.04)
                result = controller.poll()
                if result:
                    results.append(result)
            valid = [r for r in results if r.get("valid")]
            self.assertTrue(valid, results)
            self.assertEqual(valid[-1]["cca_frequency"], 17)
            self.assertEqual(valid[-1]["fbcca_frequency"], 17)
            self.assertTrue(valid[-1]["simulation"])
            # A missing batch invalidates the old full window immediately.
            time.sleep(.35)
            post(port, payload(epoch + 2.0, samples(17, 1000, 2000, 200, rng)))
            time.sleep(.15)
            gap = controller.poll()
            self.assertIsNotNone(gap)
            self.assertFalse(gap["valid"])
            self.assertGreater(gap["resets"], valid[-1]["resets"])
            time.sleep(.7)
            stale = controller.poll()
            self.assertFalse(stale["valid"])
            self.assertIn("断流", stale["status"])
            self.assertNotIn("cca_frequency", stale)
        finally:
            controller.stop()
        self.assertFalse(controller.process.is_alive())
        # The port is reusable after shutdown; no orphan receiver remains.
        server = create_server(port, queue.Queue())
        server.server_close()

    def test_epoch_restart_clears_existing_full_window(self):
        port = free_port()
        controller = WorkerController(Settings(port=port, window_seconds=1, update_seconds=.1, analysis_profile="focused", harmonics=2))
        controller.start()
        controller.begin(time.time() - 2)
        try:
            data = samples(17, 1000, 0, 1000, np.random.default_rng(9))
            post(port, payload(time.time() - 1, data))
            time.sleep(.25)
            full = controller.poll()
            self.assertIsNotNone(full)
            self.assertTrue(full["valid"], full)
            new_epoch = controller.begin()
            time.sleep(.15)
            restarted = controller.poll()
            self.assertIsNotNone(restarted)
            self.assertFalse(restarted["valid"])
            self.assertEqual(restarted["analysis_epoch"], new_epoch)
            self.assertNotIn("cca_frequency", restarted)
        finally:
            controller.stop()

    def test_port_conflict_explained(self):
        server = create_server(0, queue.Queue())
        controller = WorkerController(Settings(port=server.server_port))
        try:
            with self.assertRaisesRegex(RuntimeError, "无法监听"):
                controller.start()
        finally:
            controller.stop()
            server.server_close()

    def test_actual_range_raw_recording_and_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            port = free_port()
            controller = WorkerController(Settings(port=port, window_seconds=1, analysis_profile="focused", harmonics=2), folder)
            controller.start()
            controller.begin(time.time() - 2, mode="baseline")
            data = samples(17, 1000, 0, 1000, np.random.default_rng(9))
            data[:, 0] += 200000
            try:
                packet = payload(time.time() - 1, data)
                packet.update(units="uV", vref_v=4.5, pga_gain=24)
                self.assertEqual(post(port, packet), 200)
                time.sleep(.3)
                r = controller.poll()
                self.assertIsNotNone(r)
                self.assertEqual(r["phase"], "baseline")
                self.assertEqual(r["diagnostics"]["fullscale_uv"], 187500)
                self.assertTrue(r["input_context"]["metadata_known"])
                self.assertIn("1", r["excluded"])
                self.assertFalse(r["detected"])
            finally:
                controller.stop()
            raw = [json.loads(line) for line in (Path(folder) / "raw_eeg.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(raw), 1)
            np.testing.assert_array_equal(raw[0]["data"], data)
            self.assertEqual(raw[0]["pga_gain"], 24)
            self.assertTrue(json.loads((Path(folder) / "raw_recording_status.json").read_text())["writer_finished"])

    def test_units_and_incomplete_range_are_rejected(self):
        for extra in ({"units": "V"}, {"vref_v": 4.5}, {"vref_v": 4.5, "pga_gain": 0},
                      {"vref_v": 4.5, "pga_gain": 100}):
            with self.assertRaises(ValueError):
                validate_packet({**payload(time.time()), **extra})


    def test_runtime_gain_change_discards_old_analysis_window(self):
        port = free_port()
        controller = WorkerController(Settings(port=port, window_seconds=1, update_seconds=.1,
            stale_seconds=3, analysis_profile="focused", harmonics=2))
        controller.start()
        controller.begin(time.time() - 2)
        def await_gain(gain):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                result = controller.poll()
                if result and result.get("input_context", {}).get("pga_gain") == gain:
                    return result
                time.sleep(.02)
            self.fail(f"No result for gain {gain}")
        try:
            epoch = time.time() - 1
            data = samples(17, 1000, 0, 1000, np.random.default_rng(21))
            first = payload(epoch, data)
            first.update(units="uV", vref_v=4.5, pga_gain=12)
            post(port, first)
            full = await_gain(12)
            self.assertTrue(full["valid"], full)
            second = payload(epoch + 1, data[:200])
            second.update(units="uV", vref_v=4.5, pga_gain=24)
            post(port, second)
            changed = await_gain(24)
            self.assertFalse(changed["valid"])
            self.assertGreater(changed["resets"], full["resets"])
            self.assertEqual(changed["input_context"]["fullscale_uv"], 187500)
            self.assertNotIn("cca_frequency", changed)
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
