import json
from pathlib import Path
import tempfile
import time
import unittest
import numpy as np
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.receiver import WorkerController, validate_packet
from ssvep_realtime_test.simulator import samples
from test_receiver import free_port, payload, post


def mapped_packet(start, data, pair=(3, 6)):
    mapping = [1, 2, 3, 4, 5, 6, *pair]
    return {**payload(start, data[:, np.array(mapping) - 1]),
            "channel_mode": "six_to_eight", "channel_map": mapping}


class ChannelMappingTests(unittest.TestCase):
    def test_legacy_defaults_and_invalid_metadata(self):
        original = payload(time.time())
        self.assertEqual(validate_packet(original)["channel_map"], list(range(1, 9)))
        valid = mapped_packet(time.time(), np.zeros((200, 8)))
        self.assertEqual(validate_packet(valid)["channel_map"], valid["channel_map"])
        for extra in (
            {"channel_mode": "unknown"}, {"channel_map": None},
            {"channel_map": [1, 2, 3, 4, 5, 6, 3, True]},
            {"channel_map": [1, 2, 3, 4, 5, 6, 3, 3]},
            {"channel_map": [1, 2, 3, 4, 5, 6, 3, 7]},
            {"channel_map": [2, 1, 3, 4, 5, 6, 3, 6]},
            {"channel_mode": "raw8"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_packet({**valid, **extra})

    def test_contiguous_http_mapping_change_resets_window_and_records_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = WorkerController(Settings(port=free_port(), window_seconds=1,
                update_seconds=.1, stale_seconds=3, analysis_profile="matlab"), folder)
            controller.start()
            start = time.time() - 2.5
            controller.begin(start - .1)
            data = samples(17, 1000, 0, 2400, np.random.default_rng(23))

            def wait_result(predicate):
                end = time.monotonic() + 2
                last = None
                while time.monotonic() < end:
                    result = controller.poll()
                    if result:
                        last = result
                        if predicate(result):
                            return result
                    time.sleep(.02)
                self.fail(f"Expected result did not arrive: {last}")

            first = mapped_packet(start, data[:1200])
            changed = mapped_packet(start + 1.2, data[1200:1400], (1, 4))
            rest = mapped_packet(start + 1.4, data[1400:2400], (1, 4))
            try:
                post(controller.config.port, first)
                old = wait_result(lambda r: r.get("valid"))
                self.assertEqual(old["cca_frequency"], 17)
                self.assertEqual(old["input_context"]["channel_map"], first["channel_map"])
                # Timestamps are contiguous; only the channel sources changed.
                post(controller.config.port, changed)
                reset = wait_result(lambda r: r.get("resets", 0) > old["resets"])
                self.assertFalse(reset["valid"])
                self.assertNotIn("cca_frequency", reset)
                self.assertEqual(reset["input_context"]["channel_map"], changed["channel_map"])
                post(controller.config.port, rest)
                new = wait_result(lambda r: r.get("valid"))
                self.assertEqual(new["cca_frequency"], 17)
                self.assertGreaterEqual(new["analysis_data_start"], start + 1.2)
                # Returning to a legacy physical-channel packet also clears it.
                post(controller.config.port, payload(start + 2.4, data[:200]))
                raw = wait_result(lambda r: r.get("resets", 0) > new["resets"])
                self.assertFalse(raw["valid"])
                self.assertNotIn("channel_map", raw["input_context"])
            finally:
                controller.stop()
            saved = [json.loads(line) for line in (Path(folder) / "raw_eeg.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(saved), 4)
            for record, original in zip(saved, (first, changed, rest)):
                self.assertEqual(record["channel_map"], original["channel_map"])
                np.testing.assert_array_equal(record["data"], original["data"])
            self.assertEqual(saved[-1]["channel_map"], list(range(1, 9)))


if __name__ == "__main__":
    unittest.main()
