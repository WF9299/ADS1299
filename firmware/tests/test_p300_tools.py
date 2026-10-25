"""Host-side tests for the P300 desktop monitor/simulator protocol core."""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from tools.p300_common import (
    BATCH_BYTES,
    BatchStreamParser,
    EegBatch,
    batch_to_record,
    codes_to_uv,
    crc32c,
    decode_eeg_batch,
    encode_eeg_batch,
    export_jsonl_to_csv,
    export_jsonl_to_json,
)
from tools.p300_marker_client import (
    MAGIC,
    MARKER,
    MARKER_ACK,
    MARKER_ACK_STRUCT,
    MARKER_STRUCT,
    SYNC_REPLY,
    SYNC_REPLY_STRUCT,
    SYNC_REQ,
    SYNC_REQ_STRUCT,
    ClockSyncState,
    MarkerStatus,
    P300MarkerClient,
    build_marker,
    build_sync_request,
    build_sync_result,
    monotonic_us,
    parse_marker_ack,
    parse_sync_reply,
)
from tools.p300_impedance import (
    IMPEDANCE_REQUEST_BYTES,
    IMPEDANCE_RESULT_BYTES,
    InputNetworkModel,
    ImpedanceCalibration,
    ImpedanceClient,
    ImpedanceFlags,
    ImpedanceModelStatus,
    ImpedanceResultData,
    ImpedanceStatus,
    build_impedance_report,
    build_impedance_request,
    parse_impedance_result,
)
from tools.p300_self_test import (
    CONFIG_INTERNAL_SHORT,
    CONFIG_NORMAL,
    SELF_TEST_REQUEST_BYTES,
    SELF_TEST_RESULT_BYTES,
    InternalShortAnalyzer,
    SelfTestClient,
    SelfTestFlags,
    SelfTestGrade,
    SelfTestStatus,
    build_self_test_request,
    parse_self_test_result,
)
import tools.p300_monitor as p300_monitor_tool
from tools.p300_monitor import (
    MonitorDataModel,
    TcpReceiver,
    apply_marker_override,
    is_recordable_batch,
)
from tools.p300_http_push import HttpJsonSender, HttpPacketAggregator, HttpPushSettings
from tools.p300_filters import (
    SCIPY_AVAILABLE,
    FilterMode,
    FilterUnavailableError,
    RealtimeEegFilter,
)
from tools.p300_bias import (
    BIAS_CONTROL_REQUEST_BYTES,
    BIAS_CONTROL_RESULT_BYTES,
    BiasControlClient,
    BiasFlags,
    BiasOperation,
    BiasResultData,
    BiasStatus,
    build_bias_control_request,
    build_bias_control_result,
    parse_bias_control_request,
    parse_bias_control_result,
    recommend_bias_mask,
)
from tools.p300_tx_control import (
    TX_CONTROL_REQUEST_BYTES,
    TX_CONTROL_RESULT_BYTES,
    TxControlClient,
    TxFlags,
    TxOperation,
    TxResultData,
    TxSetOutcome,
    TxStatus,
    build_tx_control_request,
    build_tx_control_result,
    parse_tx_control_request,
    parse_tx_control_result,
)
from tools.p300_display_reference import DisplayReferenceProcessor, ReferenceMode
from tools.p300_signal_quality import (
    DisplaySignalQualityAnalyzer,
    QualityGrade,
    SignalQualityAnalyzer,
    mains_suppression_db,
)
from tools.p300_simulator import (
    PGA_GAIN,
    SAMPLE_RATE,
    VREF_V,
    SelfTestControlServer,
    SignalGenerator,
    build_stream_config,
    run_simulator,
)


class P300ToolProtocolTests(unittest.TestCase):
    def make_batch(self) -> EegBatch:
        samples = np.zeros((20, 8), dtype=np.int32)
        samples[0] = [-8_388_608, -2, -1, 0, 1, 2, 8_388_607, 42]
        markers = np.zeros(20, dtype=np.uint16)
        markers[3] = 1
        return EegBatch(
            stream_id=0x10203040,
            config_id=7,
            start_seq=1000,
            timestamp_us=1_783_985_595_550_000,
            samples=samples,
            markers=markers,
        )

    @staticmethod
    def make_impedance_result(request_id: int, *, flags: int = 0x07) -> bytes:
        packet = bytearray(IMPEDANCE_RESULT_BYTES)
        struct.pack_into(">2sBBIBBBBHH", packet, 0, b"\xA5\x51", 1, 7, request_id, 0, 0xFF, 0x04, flags, 1842, 1024)
        struct.pack_into(">8I", packet, 16, 9_000, 12_000, 0xFFFFFFFF, 30_000, 47_000, 51_000, 7_500, 22_000)
        struct.pack_into(">8I", packet, 48, 275_000, 366_000, 0xFFFFFFFF, 917_000, 1_436_000, 1_558_000, 229_000, 672_000)
        struct.pack_into(">I", packet, 80, crc32c(packet[:80]))
        return bytes(packet)

    @staticmethod
    def make_self_test_result(
        request_id: int,
        *,
        status: int = 0,
        requested: int = CONFIG_INTERNAL_SHORT,
        actual: int = CONFIG_INTERNAL_SHORT,
        flags: int = 1,
        stream_id: int = 0x12345678,
    ) -> bytes:
        packet = bytearray(SELF_TEST_RESULT_BYTES)
        struct.pack_into(
            ">2sBBIBBBBI",
            packet,
            0,
            b"\xA5\x51",
            1,
            9,
            request_id,
            status,
            requested,
            actual,
            flags,
            stream_id,
        )
        struct.pack_into(">I", packet, 16, crc32c(packet[:16]))
        return bytes(packet)

    def test_crc32c_castagnoli_check_value(self) -> None:
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)

    def test_batch_round_trip_preserves_signed_int24_and_layout(self) -> None:
        frame = encode_eeg_batch(self.make_batch())
        self.assertEqual(len(frame), BATCH_BYTES)
        decoded = decode_eeg_batch(frame)
        self.assertEqual(decoded.start_seq, 1000)
        np.testing.assert_array_equal(decoded.samples[0], self.make_batch().samples[0])
        self.assertEqual(int(decoded.markers[3]), 1)

    def test_stream_parser_accepts_fragmented_config_and_batch(self) -> None:
        config = {"sampling_rate": 1000, "channels": 8, "device": "BrainCo-Simulator"}
        config_wire = json.dumps(config).encode("utf-8")
        payload = len(config_wire).to_bytes(4, "big") + config_wire + encode_eeg_batch(self.make_batch())
        parser = BatchStreamParser()
        emitted = []
        for offset in range(0, len(payload), 17):
            emitted.extend(parser.feed(payload[offset : offset + 17]))
        self.assertEqual(parser.config, config)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].start_seq, 1000)

    def test_stream_parser_accepts_two_batches_from_one_tcp_read(self) -> None:
        first = self.make_batch()
        second = EegBatch(
            stream_id=first.stream_id,
            config_id=first.config_id,
            start_seq=first.start_seq + 20,
            timestamp_us=first.timestamp_us + 20_000,
            samples=first.samples,
            markers=first.markers,
        )
        config = {"sampling_rate": 1000, "channels": 8}
        config_wire = json.dumps(config).encode("utf-8")
        wire = (
            len(config_wire).to_bytes(4, "big")
            + config_wire
            + encode_eeg_batch(first)
            + encode_eeg_batch(second)
        )

        parser = BatchStreamParser()
        emitted = parser.feed(wire)

        self.assertEqual([batch.start_seq for batch in emitted], [1000, 1020])
        self.assertEqual(parser.config, config)

    def test_code_to_uv_and_record_schema(self) -> None:
        converted = codes_to_uv(np.array([0, 8_388_607], dtype=np.int32), vref_v=4.5, pga_gain=12)
        self.assertAlmostEqual(float(converted[0]), 0.0)
        self.assertAlmostEqual(float(converted[1]), 375_000.0, places=3)

        record = batch_to_record(self.make_batch(), {"device": "BrainCo-Simulator", "eeg_type": 1})
        self.assertEqual(list(record), ["data", "timestamp", "sampling_rate", "channels", "device", "batch", "markers", "eeg_type", "units", "vref_v", "pga_gain"])
        self.assertEqual((record["units"], record["vref_v"], record["pga_gain"]), ("uV", 4.5, 12.0))
        self.assertEqual(len(record["data"]), 20)
        self.assertEqual(len(record["data"][0]), 8)
        self.assertEqual(record["markers"][3], 1)
        self.assertTrue(record["batch"])
        self.assertEqual(record["device"], "seu")

    def test_jsonl_export_creates_batches_document(self) -> None:
        record = batch_to_record(self.make_batch(), {"device": "BrainCo-Simulator"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.jsonl"
            target = Path(directory) / "capture.json"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            count = export_jsonl_to_json(source, target)
            document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(count, 1)
        self.assertEqual(document["batches"][0]["channels"], 8)

    def test_csv_export_writes_one_row_per_sample_and_marker_column(self) -> None:
        record = batch_to_record(self.make_batch(), {"device": "BrainCo-Simulator"})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.jsonl"
            target = Path(directory) / "capture.csv"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            count = export_jsonl_to_csv(source, target)
            lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(count, 20)
        self.assertEqual(lines[0], "timestamp,ch1_uv,ch2_uv,ch3_uv,ch4_uv,ch5_uv,ch6_uv,ch7_uv,ch8_uv,marker")
        self.assertEqual(lines[4].rsplit(",", 1)[1], "1")

    def test_stream_config_aliases_match_firmware_fields(self) -> None:
        record = batch_to_record(
            self.make_batch(),
            {"sample_rate_hz": 1000, "device_id": "P300_001122334455", "channels": 8},
        )
        self.assertEqual(record["sampling_rate"], 1000)
        self.assertEqual(record["device"], "seu")

    def test_simulator_is_seeded_and_inserts_periodic_marker(self) -> None:
        generator_a = SignalGenerator(seed=123, marker_period_s=0.02)
        generator_b = SignalGenerator(seed=123, marker_period_s=0.02)
        first_a = generator_a.next_batch()
        first_b = generator_b.next_batch()
        np.testing.assert_array_equal(first_a.samples, first_b.samples)
        self.assertEqual(first_a.start_seq, 0)
        self.assertEqual(int(first_a.markers[0]), 1)
        self.assertEqual(generator_a.next_batch().start_seq, 20)
        self.assertEqual(build_stream_config()["device"], "BrainCo-Simulator")

    def test_simulator_internal_short_profiles_match_grades(self) -> None:
        for profile, expected_grade in (
            ("pass", SelfTestGrade.PASS),
            ("warn", SelfTestGrade.WARN),
            ("fail", SelfTestGrade.FAIL),
        ):
            generator = SignalGenerator(seed=123, self_test_profile=profile)
            self.assertTrue(generator.set_config_id(CONFIG_INTERNAL_SHORT))
            analyzer = InternalShortAnalyzer()
            for _ in range(50):
                batch = generator.next_batch()
                self.assertEqual(batch.config_id, CONFIG_INTERNAL_SHORT)
                self.assertFalse(np.any(batch.markers))
                analyzer.ingest_batch(batch)
            self.assertEqual(analyzer.snapshot().overall, expected_grade)

    def test_simulator_udp_self_test_control_switches_batch_config(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        generator = SignalGenerator(seed=9, self_test_profile="pass")
        server = SelfTestControlServer(generator, port)
        server.start()
        self.assertTrue(server.ready.wait(1.0))
        self.assertIsNone(server.error)
        try:
            client = SelfTestClient("127.0.0.1", port, timeout_s=0.2, retries=0)
            enabled = client.switch(CONFIG_INTERNAL_SHORT, request_id=100)
            self.assertEqual(enabled.actual_config_id, CONFIG_INTERNAL_SHORT)
            self.assertEqual(generator.next_batch().config_id, CONFIG_INTERNAL_SHORT)
            restored = client.switch(CONFIG_NORMAL, request_id=101)
            self.assertEqual(restored.actual_config_id, CONFIG_NORMAL)
            self.assertEqual(generator.next_batch().config_id, CONFIG_NORMAL)
        finally:
            server.stop()
            server.join(timeout=1.0)

    def test_simulator_udp_bias_control_queries_and_changes_mask(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        generator = SignalGenerator(seed=10)
        server = SelfTestControlServer(generator, port)
        server.start()
        self.assertTrue(server.ready.wait(1.0))
        self.assertIsNone(server.error)
        try:
            client = BiasControlClient("127.0.0.1", port, timeout_s=0.2, retries=0)
            queried = client.query(request_id=200)
            self.assertEqual(queried.actual_mask, 0xFF)
            self.assertTrue(queried.verified)
            initial_stream = generator.stream_id
            outcome = client.set_mask(0x35, request_id=201)
            self.assertTrue(outcome.applied)
            self.assertEqual(generator.bias_mask, 0x35)
            self.assertNotEqual(generator.stream_id, initial_stream)
        finally:
            server.stop()
            server.join(timeout=1.0)

        for request_id, status in enumerate(
            (BiasStatus.BUSY, BiasStatus.APPLY_FAILED, BiasStatus.RESTORE_FAILED),
            start=300,
        ):
            failure_server = SelfTestControlServer(
                SignalGenerator(seed=request_id), 1235, status
            )
            response = failure_server._handle_bias(
                build_bias_control_request(request_id, BiasOperation.SET, 0x35)
            )
            self.assertIsNotNone(response)
            decoded = parse_bias_control_result(response, request_id)
            self.assertEqual(decoded.status, status)

    def test_simulator_udp_tx_control_and_real_two_frame_send_grouping(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        generator = SignalGenerator(seed=12)
        server = SelfTestControlServer(generator, port)
        server.start()
        self.assertTrue(server.ready.wait(1.0))
        self.assertIsNone(server.error)
        try:
            client = TxControlClient(
                "127.0.0.1", port, timeout_s=0.2, retries=0
            )
            queried = client.query(request_id=400)
            self.assertEqual(queried.actual_frames, 1)
            initial_stream = generator.stream_id
            outcome = client.set_frames(2, request_id=401)
            self.assertTrue(outcome.applied)
            self.assertEqual(generator.get_tx_group_frames(), 2)
            self.assertEqual(generator.stream_id, initial_stream)
        finally:
            server.stop()
            server.join(timeout=1.0)

        class FakeConnection:
            def __init__(self) -> None:
                self.payloads: list[bytes] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def sendall(self, payload: bytes) -> None:
                self.payloads.append(bytes(payload))

        connection = FakeConnection()
        with mock.patch(
            "tools.p300_simulator.socket.create_connection",
            return_value=connection,
        ):
            run_simulator(
                "127.0.0.1", 1234, generator, duration_s=0.085
            )
        data_payloads = connection.payloads[1:]
        self.assertTrue(any(len(payload) == 2 * BATCH_BYTES for payload in data_payloads))
        self.assertTrue(
            all(len(payload) in (BATCH_BYTES, 2 * BATCH_BYTES) for payload in data_payloads)
        )
        parser = BatchStreamParser()
        emitted = parser.feed(b"".join(connection.payloads))
        self.assertGreaterEqual(len(emitted), 4)
        self.assertTrue(all(batch.stream_id == initial_stream for batch in emitted))

    def test_simulator_quality_profiles_drive_expected_diagnostics(self) -> None:
        for profile, expected in (
            ("normal", QualityGrade.GOOD),
            ("mains", QualityGrade.BAD),
            ("dc", QualityGrade.BAD),
            ("clipped", QualityGrade.BAD),
        ):
            generator = SignalGenerator(seed=11, quality_profile=profile)
            analyzer = SignalQualityAnalyzer()
            for _ in range(50):
                analyzer.ingest_batch(generator.next_batch(), SAMPLE_RATE, VREF_V, PGA_GAIN)
            snapshot = analyzer.snapshot(force=True)
            self.assertTrue(snapshot.ready, profile)
            self.assertEqual(snapshot.overall, expected, profile)
            if profile == "clipped":
                self.assertGreater(snapshot.channels[0].clipped_samples, 0)

    def test_monitor_model_tracks_samples_markers_and_loss(self) -> None:
        model = MonitorDataModel(history_seconds=1.0)
        model.set_config({"sampling_rate": 1000, "channels": 8, "device": "test"})
        first = self.make_batch()
        second = EegBatch(
            first.stream_id, first.config_id, first.start_seq + 40, first.timestamp_us + 40_000,
            first.samples, first.markers,
        )
        model.ingest(first)
        model.ingest(second)
        restarted = EegBatch(
            first.stream_id + 1, first.config_id, 0, first.timestamp_us + 80_000,
            first.samples, first.markers,
        )
        model.ingest(restarted)
        times, values = model.plot_data()
        self.assertEqual(values.shape, (60, 8))
        self.assertEqual(times.shape, (60,))
        self.assertEqual(model.lost_samples, 20)
        self.assertEqual(model.stream_interruptions, 1)
        self.assertEqual(model.last_marker_code, 1)

    def test_monitor_model_separates_display_values_from_raw_record(self) -> None:
        model = MonitorDataModel(history_seconds=1.0)
        model.set_config({"sampling_rate": 1000, "channels": 8, "device": "raw-device"})
        source = self.make_batch()
        normal = EegBatch(
            source.stream_id,
            CONFIG_NORMAL,
            source.start_seq,
            source.timestamp_us,
            source.samples,
            source.markers,
        )
        displayed = np.full((20, 8), 12.5, dtype=np.float64)
        record = model.ingest(normal, display_values=displayed)
        _, plotted = model.plot_data()

        np.testing.assert_array_equal(plotted, displayed)
        self.assertEqual(record, batch_to_record(normal, model.config))
        self.assertNotEqual(record["data"], displayed.tolist())

        received_samples = model.received_samples
        model.clear_display_history()
        times, values = model.plot_data()
        self.assertEqual(len(times), 0)
        self.assertEqual(values.shape, (0, 8))
        self.assertEqual(model.received_samples, received_samples)

    @unittest.skipUnless(SCIPY_AVAILABLE, "SciPy unavailable")
    def test_realtime_filter_notch_and_p300_frequency_response(self) -> None:
        rate = 1000

        def amplitude(values: np.ndarray, frequency_hz: float, start: int) -> float:
            segment = values[start:, 0]
            indices = np.arange(start, len(values), dtype=np.float64)
            basis = np.exp(-2j * np.pi * frequency_hz * indices / rate)
            return float(2.0 * np.abs(np.dot(segment, basis)) / len(segment))

        def bandstop_gain(frequency_hz: float) -> float:
            indices = np.arange(12000, dtype=np.float64)
            signal = np.sin(2.0 * np.pi * frequency_hz * indices / rate)
            values = np.repeat(signal[:, None], 8, axis=1)
            output = RealtimeEegFilter(FilterMode.NOTCH_50).process(
                values,
                sample_rate_hz=rate,
                stream_id=1,
                config_id=CONFIG_NORMAL,
                start_seq=0,
            )
            return amplitude(output, frequency_hz, 4000)

        self.assertLess(bandstop_gain(49.0), 10 ** (-20.0 / 20.0))
        self.assertLess(bandstop_gain(49.65), 10 ** (-40.0 / 20.0))
        self.assertLess(bandstop_gain(50.0), 10 ** (-40.0 / 20.0))
        self.assertLess(bandstop_gain(51.0), 10 ** (-20.0 / 20.0))
        for passband_frequency in (10.0, 47.0, 53.0):
            gain = bandstop_gain(passband_frequency)
            self.assertGreater(gain, 10 ** (-0.5 / 20.0))
            self.assertLess(gain, 1.01)

        indices = np.arange(30000, dtype=np.float64)
        p300_input = sum(
            np.sin(2 * np.pi * frequency * indices / rate)
            for frequency in (0.1, 1.0, 2.0, 10.0, 30.0, 50.0)
        )
        p300_values = np.repeat(p300_input[:, None], 8, axis=1)
        original_values = p300_values.copy()
        for mode in (FilterMode.P300, FilterMode.P300_2_30):
            with self.subTest(mode=mode):
                p300 = RealtimeEegFilter(mode)
                p300_output = np.vstack([
                    p300.process(
                        p300_values[start:start + 20],
                        sample_rate_hz=rate,
                        stream_id=1,
                        config_id=CONFIG_NORMAL,
                        start_seq=start,
                    )
                    for start in range(0, len(p300_values), 20)
                ])
                self.assertLess(amplitude(p300_output, 0.1, 10000), 0.1)
                self.assertLess(amplitude(p300_output, 50.0, 10000), 10 ** (-25.0 / 20.0))
                self.assertAlmostEqual(amplitude(p300_output, 10.0, 10000), 1.0, delta=0.02)
                self.assertAlmostEqual(amplitude(p300_output, 30.0, 10000), 2 ** -0.5, delta=0.02)
                if mode == FilterMode.P300_2_30:
                    self.assertLess(amplitude(p300_output, 1.0, 10000), 0.1)
                    self.assertAlmostEqual(amplitude(p300_output, 2.0, 10000), 2 ** -0.5, delta=0.02)
                else:
                    self.assertGreater(amplitude(p300_output, 1.0, 10000), 0.9)
        np.testing.assert_array_equal(p300_values, original_values)

    @unittest.skipUnless(SCIPY_AVAILABLE, "SciPy unavailable")
    def test_realtime_filter_chunking_reset_and_internal_short_bypass(self) -> None:
        rate = 1000
        indices = np.arange(1000, dtype=np.float64)
        signal = np.sin(2 * np.pi * 50.0 * indices / rate)
        values = np.repeat(signal[:, None], 8, axis=1)

        whole_filter = RealtimeEegFilter(FilterMode.NOTCH_50)
        whole = whole_filter.process(
            values,
            sample_rate_hz=rate,
            stream_id=7,
            config_id=CONFIG_NORMAL,
            start_seq=0,
        )
        chunk_filter = RealtimeEegFilter(FilterMode.NOTCH_50)
        chunks = []
        for start in range(0, len(values), 20):
            chunks.append(chunk_filter.process(
                values[start:start + 20],
                sample_rate_hz=rate,
                stream_id=7,
                config_id=CONFIG_NORMAL,
                start_seq=start,
            ))
        np.testing.assert_allclose(np.vstack(chunks), whole, atol=1e-12, rtol=1e-12)

        reset_count = chunk_filter.reset_count
        chunk_filter.process(
            values[:20],
            sample_rate_hz=rate,
            stream_id=7,
            config_id=CONFIG_NORMAL,
            start_seq=2000,
        )
        self.assertGreater(chunk_filter.reset_count, reset_count)
        self.assertEqual(chunk_filter.last_reset_reason, "sequence")

        reset_count = chunk_filter.reset_count
        changed_stream = chunk_filter.process(
            values[20:40],
            sample_rate_hz=rate,
            stream_id=8,
            config_id=CONFIG_NORMAL,
            start_seq=0,
        )
        self.assertGreater(chunk_filter.reset_count, reset_count)
        self.assertEqual(chunk_filter.last_reset_reason, "stream")
        self.assertTrue(np.all(np.isfinite(changed_stream)))

        reset_count = chunk_filter.reset_count
        changed_rate = chunk_filter.process(
            values[40:60],
            sample_rate_hz=500,
            stream_id=8,
            config_id=CONFIG_NORMAL,
            start_seq=20,
        )
        self.assertGreater(chunk_filter.reset_count, reset_count)
        self.assertEqual(chunk_filter.last_reset_reason, "sample-rate")
        self.assertTrue(np.all(np.isfinite(changed_rate)))

        internal = chunk_filter.process(
            values[:20],
            sample_rate_hz=rate,
            stream_id=8,
            config_id=CONFIG_INTERNAL_SHORT,
            start_seq=0,
        )
        np.testing.assert_array_equal(internal, values[:20])
        self.assertFalse(chunk_filter.active)

        resumed = chunk_filter.process(
            values[:20],
            sample_rate_hz=rate,
            stream_id=9,
            config_id=CONFIG_NORMAL,
            start_seq=0,
        )
        self.assertTrue(chunk_filter.active)
        self.assertFalse(np.array_equal(resumed, values[:20]))

    def test_realtime_filter_raw_mode_survives_missing_scipy(self) -> None:
        values = np.arange(160, dtype=np.float64).reshape(20, 8)
        with mock.patch("tools.p300_filters.scipy_signal", None):
            raw_filter = RealtimeEegFilter(FilterMode.RAW)
            np.testing.assert_array_equal(
                raw_filter.process(
                    values,
                    sample_rate_hz=1000,
                    stream_id=1,
                    config_id=CONFIG_NORMAL,
                    start_seq=0,
                ),
                values,
            )
            filtered = RealtimeEegFilter(FilterMode.NOTCH_50)
            with self.assertRaises(FilterUnavailableError):
                filtered.process(
                    values,
                    sample_rate_hz=1000,
                    stream_id=1,
                    config_id=CONFIG_NORMAL,
                    start_seq=0,
                )

    def test_bias_wire_codec_and_timeout_query_reconciliation(self) -> None:
        query = build_bias_control_request(0x10203040, BiasOperation.QUERY, 0)
        self.assertEqual(len(query), BIAS_CONTROL_REQUEST_BYTES)
        self.assertEqual(parse_bias_control_request(query), (0x10203040, BiasOperation.QUERY, 0))
        set_packet = build_bias_control_request(0x55667788, BiasOperation.SET, 0x35)
        self.assertEqual(parse_bias_control_request(set_packet), (0x55667788, BiasOperation.SET, 0x35))
        with self.assertRaises(ValueError):
            build_bias_control_request(1, BiasOperation.QUERY, 1)

        result_packet = build_bias_control_result(BiasResultData(
            request_id=0x55667788,
            status=BiasStatus.OK,
            requested_mask=0x35,
            actual_mask=0x35,
            flags=BiasFlags.CHANGED | BiasFlags.VERIFIED,
            stream_id=0x12345678,
        ))
        self.assertEqual(len(result_packet), BIAS_CONTROL_RESULT_BYTES)
        decoded = parse_bias_control_result(result_packet, 0x55667788)
        self.assertEqual(decoded.actual_mask, 0x35)
        self.assertTrue(decoded.flags & BiasFlags.VERIFIED)

        ready = threading.Event()
        address: list[tuple[str, int]] = []
        received: list[tuple[int, int]] = []

        def fake_device() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", 0))
                server.settimeout(1.0)
                address.append(server.getsockname())
                ready.set()
                for _ in range(3):
                    packet, _source = server.recvfrom(64)
                    request_id, operation, _mask = parse_bias_control_request(packet)
                    received.append((request_id, int(operation)))
                packet, source = server.recvfrom(64)
                query_id, operation, _mask = parse_bias_control_request(packet)
                received.append((query_id, int(operation)))
                server.sendto(build_bias_control_result(BiasResultData(
                    request_id=query_id,
                    status=BiasStatus.OK,
                    requested_mask=0,
                    actual_mask=0x35,
                    flags=BiasFlags.VERIFIED,
                    stream_id=0x23456789,
                )), source)

        server_thread = threading.Thread(target=fake_device, daemon=True)
        server_thread.start()
        self.assertTrue(ready.wait(1.0))
        client = BiasControlClient(address[0][0], address[0][1], timeout_s=0.03, retries=2)
        outcome = client.set_mask(0x35, request_id=0x55667788)
        server_thread.join(timeout=1.0)
        self.assertTrue(outcome.reconciled)
        self.assertTrue(outcome.applied)
        self.assertEqual([item[0] for item in received[:3]], [0x55667788] * 3)
        self.assertEqual(received[3][1], int(BiasOperation.QUERY))

    def test_bias_recommendation_requires_valid_complete_impedance_result(self) -> None:
        result = ImpedanceResultData(
            request_id=1,
            status=ImpedanceStatus.OK,
            tested_mask=0xFF,
            open_mask=0x04,
            flags=ImpedanceFlags.DC_VALID | ImpedanceFlags.AC_VALID |
                  ImpedanceFlags.BACKGROUND_SUBTRACTED,
            duration_ms=2000,
            sample_count=1024,
            nominal_ohm=(14_989, 51_930, None, 14_989, 975_000, 14_989, 14_989, 14_989),
            excitation_nv_peak=(1,) * 8,
        )
        calibration = ImpedanceCalibration.default()
        self.assertEqual(recommend_bias_mask(result, calibration), 0xEB)
        invalid = ImpedanceResultData(
            result.request_id,
            ImpedanceStatus.SAMPLE_TIMEOUT,
            result.tested_mask,
            result.open_mask,
            result.flags,
            result.duration_ms,
            result.sample_count,
            result.nominal_ohm,
            result.excitation_nv_peak,
        )
        self.assertEqual(recommend_bias_mask(invalid, calibration), 0)

    def test_tx_control_wire_codec_and_timeout_query_reconciliation(self) -> None:
        query = build_tx_control_request(0x10203040, TxOperation.QUERY, 0)
        self.assertEqual(len(query), TX_CONTROL_REQUEST_BYTES)
        self.assertEqual(
            parse_tx_control_request(query),
            (0x10203040, TxOperation.QUERY, 0),
        )
        set_packet = build_tx_control_request(0x55667788, TxOperation.SET, 2)
        self.assertEqual(
            parse_tx_control_request(set_packet),
            (0x55667788, TxOperation.SET, 2),
        )
        with self.assertRaises(ValueError):
            build_tx_control_request(1, TxOperation.SET, 3)

        result_packet = build_tx_control_result(TxResultData(
            request_id=0x55667788,
            status=TxStatus.OK,
            requested_frames=2,
            actual_frames=2,
            flags=TxFlags.CHANGED | TxFlags.VERIFIED,
            stream_id=0x12345678,
        ))
        self.assertEqual(len(result_packet), TX_CONTROL_RESULT_BYTES)
        decoded = parse_tx_control_result(result_packet, 0x55667788)
        self.assertEqual(decoded.actual_frames, 2)
        self.assertTrue(decoded.verified)

        ready = threading.Event()
        address: list[tuple[str, int]] = []
        received: list[tuple[int, int]] = []

        def fake_device() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", 0))
                server.settimeout(1.0)
                address.append(server.getsockname())
                ready.set()
                for _ in range(3):
                    packet, _source = server.recvfrom(64)
                    request_id, operation, _frames = parse_tx_control_request(packet)
                    received.append((request_id, int(operation)))
                packet, source = server.recvfrom(64)
                query_id, operation, _frames = parse_tx_control_request(packet)
                received.append((query_id, int(operation)))
                good = build_tx_control_result(TxResultData(
                    request_id=query_id,
                    status=TxStatus.OK,
                    requested_frames=0,
                    actual_frames=2,
                    flags=TxFlags.VERIFIED,
                    stream_id=0x23456789,
                ))
                bad = bytearray(good)
                bad[-1] ^= 1
                server.sendto(bad, source)
                server.sendto(good, source)

        server_thread = threading.Thread(target=fake_device, daemon=True)
        server_thread.start()
        self.assertTrue(ready.wait(1.0))
        client = TxControlClient(
            address[0][0], address[0][1], timeout_s=0.03, retries=2
        )
        outcome = client.set_frames(2, request_id=0x55667788)
        server_thread.join(timeout=1.0)
        self.assertTrue(outcome.reconciled)
        self.assertTrue(outcome.applied)
        self.assertEqual([item[0] for item in received[:3]], [0x55667788] * 3)
        self.assertEqual(received[3][1], int(TxOperation.QUERY))

    def test_signal_quality_metrics_grade_and_continuity_reset(self) -> None:
        rate = 1000
        scale_uv = 4.5 / (12.0 * 8_388_607.0) * 1_000_000.0
        indices = np.arange(1000, dtype=np.float64)
        uv = 1000.0 + 20.0 * np.sin(2.0 * np.pi * 50.0 * indices / rate)
        codes = np.rint(uv / scale_uv).astype(np.int32)
        samples = np.repeat(codes[:, None], 8, axis=1)
        analyzer = SignalQualityAnalyzer(window_samples=1000, refresh_interval_s=0.5)
        for start in range(0, 1000, 20):
            analyzer.ingest_batch(EegBatch(
                7, CONFIG_NORMAL, start, 1_000_000 + start * 1000,
                samples[start:start + 20], np.zeros(20, dtype=np.uint16),
            ), rate, 4.5, 12.0)
        snapshot = analyzer.snapshot(force=True)
        self.assertTrue(snapshot.ready)
        self.assertEqual(snapshot.sample_count, 1000)
        self.assertAlmostEqual(snapshot.channels[0].dc_mv, 1.0, delta=0.01)
        self.assertAlmostEqual(snapshot.channels[0].mains_50hz_uv, 20.0, delta=0.2)
        self.assertEqual(snapshot.channels[0].grade, QualityGrade.WARN)

        analyzer.ingest_batch(EegBatch(
            7, CONFIG_NORMAL, 2000, 3_000_000,
            np.zeros((20, 8), dtype=np.int32), np.zeros(20, dtype=np.uint16),
        ), rate, 4.5, 12.0)
        self.assertEqual(analyzer.snapshot(force=True).sample_count, 20)
        self.assertEqual(analyzer.last_reset_reason, "sequence")

    @unittest.skipUnless(SCIPY_AVAILABLE, "SciPy unavailable")
    def test_display_quality_reports_filtered_mains_without_hiding_raw_bad(self) -> None:
        rate = 1000
        scale_uv = 4.5 / (12.0 * 8_388_607.0) * 1_000_000.0
        indices = np.arange(1000, dtype=np.float64)
        raw_uv = 1000.0 + 400.0 * np.sin(2.0 * np.pi * 49.65 * indices / rate)
        codes = np.rint(raw_uv / scale_uv).astype(np.int32)
        samples = np.repeat(codes[:, None], 8, axis=1)
        raw_analyzer = SignalQualityAnalyzer(window_samples=1000, refresh_interval_s=0.5)
        display_analyzer = DisplaySignalQualityAnalyzer(
            window_samples=1000, refresh_interval_s=0.5
        )
        display_filter = RealtimeEegFilter(FilterMode.NOTCH_50)

        for start in range(0, 1000, 20):
            batch = EegBatch(
                9,
                CONFIG_NORMAL,
                start,
                2_000_000 + start * 1000,
                samples[start:start + 20],
                np.zeros(20, dtype=np.uint16),
            )
            raw_analyzer.ingest_batch(batch, rate, 4.5, 12.0)
            filtered = display_filter.process(
                np.repeat(raw_uv[start:start + 20, None], 8, axis=1),
                sample_rate_hz=rate,
                stream_id=batch.stream_id,
                config_id=batch.config_id,
                start_seq=batch.start_seq,
            )
            display_analyzer.ingest_batch(batch, rate, filtered)

        raw_snapshot = raw_analyzer.snapshot(force=True)
        display_snapshot = display_analyzer.snapshot(force=True)
        self.assertTrue(raw_snapshot.ready)
        self.assertTrue(display_snapshot.ready)
        self.assertEqual(raw_snapshot.overall, QualityGrade.BAD)
        self.assertEqual(display_snapshot.overall, QualityGrade.GOOD)
        self.assertLess(display_snapshot.channels[0].mains_50hz_uv, 10.0)
        self.assertGreater(
            mains_suppression_db(
                raw_snapshot.channels[0].mains_50hz_uv,
                display_snapshot.channels[0].mains_50hz_uv,
            ),
            20.0,
        )

    def test_display_reference_is_copy_only_and_internal_short_bypasses(self) -> None:
        values = np.arange(160, dtype=np.float64).reshape(20, 8)
        original = values.copy()
        processor = DisplayReferenceProcessor(ReferenceMode.CAR, channel_mask=0x03)
        car = processor.process(values, CONFIG_NORMAL)
        expected_reference = np.mean(values[:, :2], axis=1, keepdims=True)
        np.testing.assert_allclose(car, values - expected_reference)
        np.testing.assert_array_equal(values, original)

        processor.set_mode(ReferenceMode.MEDIAN)
        median = processor.process(values, CONFIG_NORMAL)
        np.testing.assert_allclose(median, values - np.median(values[:, :2], axis=1, keepdims=True))
        np.testing.assert_array_equal(processor.process(values, CONFIG_INTERNAL_SHORT), values)
        with self.assertRaises(ValueError):
            processor.set_channel_mask(0x01)

    def test_marker_override_is_non_mutating_and_normal_only(self) -> None:
        source = self.make_batch()
        normal = EegBatch(
            source.stream_id,
            CONFIG_NORMAL,
            source.start_seq,
            source.timestamp_us,
            source.samples,
            source.markers,
        )
        original_markers = normal.markers.copy()

        self.assertIs(apply_marker_override(normal, False), normal)
        forced = apply_marker_override(normal, True)
        self.assertIsNot(forced, normal)
        self.assertIs(forced.samples, normal.samples)
        self.assertEqual(forced.stream_id, normal.stream_id)
        self.assertEqual(forced.config_id, normal.config_id)
        self.assertEqual(forced.start_seq, normal.start_seq)
        self.assertEqual(forced.timestamp_us, normal.timestamp_us)
        np.testing.assert_array_equal(forced.markers, np.ones(20, dtype=np.uint16))
        np.testing.assert_array_equal(normal.markers, original_markers)

        internal_short = EegBatch(
            normal.stream_id,
            CONFIG_INTERNAL_SHORT,
            normal.start_seq,
            normal.timestamp_us,
            normal.samples,
            normal.markers,
        )
        self.assertIs(apply_marker_override(internal_short, True), internal_short)

    def test_forced_markers_reach_record_and_http_payload(self) -> None:
        aggregator = HttpPacketAggregator()
        output = None
        for batch_index in range(10):
            raw = EegBatch(
                stream_id=0x12345678,
                config_id=CONFIG_NORMAL,
                start_seq=batch_index * 20,
                timestamp_us=1_783_985_595_550_000 + batch_index * 20_000,
                samples=np.full((20, 8), batch_index, dtype=np.int32),
                markers=np.zeros(20, dtype=np.uint16),
            )
            effective = apply_marker_override(raw, True)
            record = batch_to_record(effective, {"device": "P300_FORCE_MARKER"})
            self.assertEqual(record["markers"], [1] * 20)
            output = aggregator.ingest(effective, record)

        self.assertIsNotNone(output)
        self.assertEqual(output["markers"], [1] * 200)

    def test_simulator_to_monitor_tcp_loopback(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        events: queue.Queue = queue.Queue()
        receiver = TcpReceiver("127.0.0.1", port, events)
        receiver.start()
        time.sleep(0.05)
        run_simulator("127.0.0.1", port, SignalGenerator(seed=5), duration_s=0.06)
        time.sleep(0.1)
        receiver.stop()
        receiver.join(timeout=1.0)
        received = []
        while not events.empty():
            received.append(events.get_nowait())
        self.assertTrue(any(event[0] == "config" for event in received))
        self.assertTrue(any(event[0] == "batch" for event in received))
        self.assertTrue(any(event[0] == "peer" and event[1]["connected"] for event in received))
        self.assertTrue(any(event[0] == "peer" and not event[1]["connected"] for event in received))

    def test_marker_wire_helpers_and_adaptive_clock_policy(self) -> None:
        request = build_sync_request(7, 123_456)
        self.assertEqual(len(request), 16)
        reply = SYNC_REPLY_STRUCT.pack(MAGIC, 1, SYNC_REPLY, 7, 123_456, 4_123_500, 4_123_520)
        self.assertEqual(parse_sync_reply(reply), (7, 123_456, 4_123_500, 4_123_520))
        self.assertEqual(len(build_sync_result(7, 123_456, 4_123_500, 4_123_520, 123_700)), 40)
        marker = build_marker(9, 1, 500_000)
        self.assertEqual(len(marker), 24)
        ack_wire = MARKER_ACK_STRUCT.pack(MAGIC, 1, MARKER_ACK, 9, MarkerStatus.OK, 321, 250)
        ack = parse_marker_ack(ack_wire)
        self.assertEqual((ack.event_id, ack.status, ack.sample_seq, ack.error_us), (9, MarkerStatus.OK, 321, 250))

        state = ClockSyncState()
        for index in range(16):
            t1 = 1_000_000 + index * 10_000
            self.assertTrue(state.observe(t1, t1 + 4_000_050, t1 + 4_000_070, t1 + 200, now_s=index / 10))
        self.assertEqual(state.valid_measurements(now_s=1.5), 16)
        self.assertEqual(state.rate_hz(now_s=1.5), 2.0)
        self.assertFalse(state.synchronized(now_s=12.0))
        self.assertEqual(state.rate_hz(now_s=12.0), 10.0)

    def test_marker_client_udp_loopback(self) -> None:
        stop = threading.Event()
        ready = threading.Event()
        address: list[tuple[str, int]] = []

        def fake_device() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", 0))
                server.settimeout(0.1)
                address.append(server.getsockname())
                ready.set()
                while not stop.is_set():
                    try:
                        packet, source = server.recvfrom(64)
                    except TimeoutError:
                        continue
                    if len(packet) == SYNC_REQ_STRUCT.size and packet[3] == SYNC_REQ:
                        _, _, _, sequence, t1 = SYNC_REQ_STRUCT.unpack(packet)
                        t2 = monotonic_us() + 4_000_000
                        t3 = t2 + 20
                        server.sendto(SYNC_REPLY_STRUCT.pack(MAGIC, 1, SYNC_REPLY, sequence, t1, t2, t3), source)
                    elif len(packet) == MARKER_STRUCT.size and packet[3] == MARKER:
                        _, _, _, event_id, code, _ = MARKER_STRUCT.unpack(packet)
                        status = MarkerStatus.OK if code == 1 else MarkerStatus.INVALID_CODE
                        server.sendto(MARKER_ACK_STRUCT.pack(MAGIC, 1, MARKER_ACK, event_id, status, 1234, 200), source)

        server_thread = threading.Thread(target=fake_device, daemon=True)
        server_thread.start()
        self.assertTrue(ready.wait(1.0))
        client = P300MarkerClient(address[0][0], address[0][1])
        try:
            client.start()
            self.assertTrue(client.wait_until_synchronized(1.0), client.last_sync_error)
            ack = client.send_marker(1)
            self.assertEqual(ack.status, MarkerStatus.OK)
            self.assertEqual(ack.sample_seq, 1234)
        finally:
            client.stop()
            stop.set()
            server_thread.join(timeout=1.0)

    def test_impedance_wire_helpers_and_calibration(self) -> None:
        request = build_impedance_request(0x10203040, 0xA5)
        self.assertEqual(len(request), IMPEDANCE_REQUEST_BYTES)
        self.assertEqual(request[:9], b"\xA5\x51\x01\x06\x10\x20\x30\x40\xA5")
        self.assertEqual(struct.unpack_from(">I", request, 12)[0], crc32c(request[:12]))

        result = parse_impedance_result(self.make_impedance_result(0x10203040), expected_request_id=0x10203040)
        self.assertEqual(result.status, ImpedanceStatus.OK)
        self.assertEqual(result.flags, ImpedanceFlags.DC_VALID | ImpedanceFlags.AC_VALID | ImpedanceFlags.BACKGROUND_SUBTRACTED)
        self.assertEqual(result.open_mask, 0x04)
        self.assertIsNone(result.nominal_ohm[2])
        self.assertEqual(result.excitation_nv_peak[3], 917_000)

    def test_input_network_compensation_and_limits(self) -> None:
        network = InputNetworkModel()

        def raw_for(electrode_ohm: float) -> float:
            loop_ohm = electrode_ohm + network.series_ohm
            susceptance = 2.0 * math.pi * network.lead_off_frequency_hz * network.shunt_capacitance_f
            return 1.0 / math.sqrt((1.0 / loop_ohm) ** 2 + susceptance**2)

        calibration = ImpedanceCalibration.default("test")
        for electrode_ohm in (0.0, 10_000.0, 47_000.0, 100_000.0):
            estimate = calibration.estimate(0, raw_for(electrode_ohm))
            self.assertIn(estimate.model_status, (ImpedanceModelStatus.OK, ImpedanceModelStatus.CLAMPED_LOW))
            self.assertAlmostEqual(estimate.network_compensated_ohm, electrode_ohm, places=5)
            self.assertAlmostEqual(estimate.display_ohm, electrode_ohm, places=5)
            self.assertFalse(estimate.calibrated)

        clamped = calibration.estimate(0, 4_000.0)
        self.assertEqual(clamped.model_status, ImpedanceModelStatus.CLAMPED_LOW)
        self.assertEqual(clamped.network_compensated_ohm, 0.0)
        out_of_range = calibration.estimate(0, network.capacitive_limit_ohm * network.max_parallel_fraction)
        self.assertEqual(out_of_range.model_status, ImpedanceModelStatus.OUT_OF_RANGE)
        self.assertIsNone(out_of_range.display_ohm)

    def test_impedance_calibration_v2_and_v1_migration(self) -> None:
        network = InputNetworkModel()

        def raw_for(electrode_ohm: float) -> float:
            loop_ohm = electrode_ohm + network.series_ohm
            susceptance = 2.0 * math.pi * network.lead_off_frequency_hz * network.shunt_capacitance_f
            return 1.0 / math.sqrt((1.0 / loop_ohm) ** 2 + susceptance**2)

        low_raw = raw_for(10_000.0) * 0.96
        high_raw = raw_for(47_000.0) * 0.96
        calibration = ImpedanceCalibration.default("test")
        calibration.set_two_point(0, low_raw, high_raw)
        self.assertAlmostEqual(calibration.apply(0, low_raw), 10_000.0)
        self.assertAlmostEqual(calibration.apply(0, high_raw), 47_000.0)
        self.assertTrue(calibration.channels[0].calibrated)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            calibration.save(path)
            loaded = ImpedanceCalibration.load(path)
            self.assertEqual(loaded.version, 2)
            self.assertEqual(loaded.input_network.series_ohm, 4_990.0)
            self.assertAlmostEqual(loaded.apply(0, high_raw), 47_000.0)
            invalid = loaded.to_dict()
            invalid["input_network"]["max_parallel_fraction"] = 1.0
            with self.assertRaises(ValueError):
                ImpedanceCalibration.from_dict(invalid)

            legacy = {
                "version": 1,
                "device": "legacy",
                "channels": [
                    {"gain": 1.0, "offset_ohm": 0.0, "calibrated": False}
                    for _ in range(8)
                ],
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            migrated, warning = ImpedanceCalibration.load_or_default(path)
            self.assertEqual(migrated.version, 2)
            self.assertIn("version 1", warning or "")

            legacy["channels"][0]["calibrated"] = True
            path.write_text(json.dumps(legacy), encoding="utf-8")
            fallback, warning = ImpedanceCalibration.load_or_default(path, device="fallback")
            self.assertIn("recalibration", (warning or "").lower())
            self.assertFalse(any(channel.calibrated for channel in fallback.channels))

            path.write_text("{broken", encoding="utf-8")
            fallback, warning = ImpedanceCalibration.load_or_default(path, device="fallback")
        self.assertIsNotNone(warning)
        self.assertFalse(any(channel.calibrated for channel in fallback.channels))

    def test_impedance_report_preserves_raw_and_compensated_values(self) -> None:
        result = parse_impedance_result(self.make_impedance_result(0x10203040))
        calibration = ImpedanceCalibration.default("test")
        report = build_impedance_report(result, calibration)
        self.assertEqual(report["input_network"]["series_ohm"], 4_990.0)
        self.assertEqual(report["calibration"]["version"], 2)
        self.assertEqual(report["channels"][0]["nominal_ohm"], 9_000)
        self.assertLess(report["channels"][0]["network_compensated_ohm"], 9_000)
        self.assertEqual(report["channels"][2]["model_status"], "open")
        self.assertIsNone(report["channels"][2]["display_ohm"])

    def test_impedance_client_ignores_bad_crc_and_retries_same_request_id(self) -> None:
        ready = threading.Event()
        received_ids: list[int] = []
        address: list[tuple[str, int]] = []

        def fake_device() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", 0))
                server.settimeout(1.0)
                address.append(server.getsockname())
                ready.set()
                for attempt in range(2):
                    packet, source = server.recvfrom(64)
                    received_ids.append(struct.unpack_from(">I", packet, 4)[0])
                    response = bytearray(self.make_impedance_result(received_ids[-1]))
                    if attempt == 0:
                        response[-1] ^= 0xFF
                    server.sendto(response, source)

        server_thread = threading.Thread(target=fake_device, daemon=True)
        server_thread.start()
        self.assertTrue(ready.wait(1.0))
        client = ImpedanceClient(address[0][0], address[0][1], timeout_s=0.15, retries=1)
        result = client.measure(request_id=0x55667788)
        server_thread.join(timeout=1.0)
        self.assertEqual(result.request_id, 0x55667788)
        self.assertEqual(received_ids, [0x55667788, 0x55667788])

    def test_self_test_wire_helpers_and_client_retry(self) -> None:
        request = build_self_test_request(0x10203040, CONFIG_INTERNAL_SHORT)
        self.assertEqual(len(request), SELF_TEST_REQUEST_BYTES)
        self.assertEqual(request[:9], b"\xA5\x51\x01\x08\x10\x20\x30\x40\x02")
        self.assertEqual(struct.unpack_from(">I", request, 12)[0], crc32c(request[:12]))

        decoded = parse_self_test_result(
            self.make_self_test_result(0x10203040),
            expected_request_id=0x10203040,
        )
        self.assertEqual(decoded.status, SelfTestStatus.OK)
        self.assertEqual(decoded.requested_config_id, CONFIG_INTERNAL_SHORT)
        self.assertEqual(decoded.actual_config_id, CONFIG_INTERNAL_SHORT)
        self.assertEqual(decoded.flags, SelfTestFlags.CHANGED)
        self.assertEqual(decoded.stream_id, 0x12345678)

        ready = threading.Event()
        address: list[tuple[str, int]] = []
        received_ids: list[int] = []

        def fake_device() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", 0))
                server.settimeout(1.0)
                address.append(server.getsockname())
                ready.set()
                for attempt in range(2):
                    packet, source = server.recvfrom(64)
                    received_ids.append(struct.unpack_from(">I", packet, 4)[0])
                    response = bytearray(self.make_self_test_result(received_ids[-1]))
                    if attempt == 0:
                        response[-1] ^= 0xFF
                    server.sendto(response, source)

        server_thread = threading.Thread(target=fake_device, daemon=True)
        server_thread.start()
        self.assertTrue(ready.wait(1.0))
        client = SelfTestClient(address[0][0], address[0][1], timeout_s=0.1, retries=1)
        result = client.switch(CONFIG_INTERNAL_SHORT, request_id=0x55667788)
        server_thread.join(timeout=1.0)
        self.assertEqual(result.actual_config_id, CONFIG_INTERNAL_SHORT)
        self.assertEqual(received_ids, [0x55667788, 0x55667788])

    def test_internal_short_analyzer_thresholds_and_continuity(self) -> None:
        analyzer = InternalShortAnalyzer(sample_rate_hz=1000, window_samples=1000)
        sample_index = np.arange(1000, dtype=np.float64)

        expected = (
            (0.5, SelfTestGrade.PASS),
            (2.0, SelfTestGrade.WARN),
            (6.0, SelfTestGrade.FAIL),
        )
        for stream_id, (amplitude_uv, grade) in enumerate(expected, start=1):
            analyzer.reset()
            signal = amplitude_uv * np.sin(2.0 * np.pi * 50.0 * sample_index / 1000.0)
            for offset in range(0, 1000, 20):
                analyzer.ingest_uv(
                    stream_id=stream_id,
                    config_id=CONFIG_INTERNAL_SHORT,
                    start_seq=offset,
                    samples_uv=np.repeat(signal[offset : offset + 20, None], 8, axis=1),
                )
            snapshot = analyzer.snapshot()
            self.assertTrue(snapshot.ready)
            self.assertEqual(snapshot.overall, grade)
            self.assertAlmostEqual(snapshot.channels[0].mains_50hz_uv, amplitude_uv, delta=0.03)

        analyzer.reset()
        zeros = np.zeros((20, 8), dtype=np.float64)
        analyzer.ingest_uv(1, CONFIG_INTERNAL_SHORT, 0, zeros)
        analyzer.ingest_uv(1, CONFIG_INTERNAL_SHORT, 40, zeros)
        self.assertEqual(analyzer.sample_count, 20)
        self.assertFalse(analyzer.snapshot().ready)
        analyzer.ingest_uv(1, CONFIG_NORMAL, 60, zeros)
        self.assertEqual(analyzer.sample_count, 0)

        normal = self.make_batch()
        normal = EegBatch(
            normal.stream_id,
            CONFIG_NORMAL,
            normal.start_seq,
            normal.timestamp_us,
            normal.samples,
            normal.markers,
        )
        internal = EegBatch(
            normal.stream_id,
            CONFIG_INTERNAL_SHORT,
            normal.start_seq,
            normal.timestamp_us,
            normal.samples,
            normal.markers,
        )
        self.assertTrue(is_recordable_batch(normal))
        self.assertFalse(is_recordable_batch(internal))

    def test_tools_are_directly_executable_from_project_root(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for script in ("p300_monitor.py", "p300_simulator.py", "p300_marker_client.py"):
            result = subprocess.run(
                [sys.executable, str(root / "tools" / script), "--help"],
                cwd=root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gui_initializes_offscreen(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "p300_monitor.py"),
                "--host", "127.0.0.1",
                "--port", "0",
                "--smoke-test",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @staticmethod
    def make_http_payload(device: str = "P300_HTTP_TEST") -> dict:
        return {
            "data": [[float(sample + channel) for channel in range(8)] for sample in range(200)],
            "timestamp": 1_783_985_595.55,
            "sampling_rate": 1000,
            "channels": 8,
            "device": device,
            "batch": True,
            "markers": [1 if sample == 3 else 0 for sample in range(200)],
            "eeg_type": 0,
        }

    def test_http_settings_validate_persist_and_fallback(self) -> None:
        settings = HttpPushSettings("127.0.0.1", 25000, " /api/bci/data ").normalized()
        self.assertEqual(settings.endpoint, "/api/bci/data")
        self.assertEqual(settings.url, "http://127.0.0.1:25000/api/bci/data")
        with self.assertRaises(ValueError):
            HttpPushSettings("999.1.1.1", 25000, "/api/bci/data").normalized()
        with self.assertRaises(ValueError):
            HttpPushSettings("127.0.0.1", 0, "/api/bci/data").normalized()
        with self.assertRaises(ValueError):
            HttpPushSettings("127.0.0.1", 25000, "api/bci/data").normalized()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "http.json"
            settings.save(path)
            loaded, warning = HttpPushSettings.load_or_default(path)
            self.assertIsNone(warning)
            self.assertEqual(loaded, settings)
            path.write_text("{broken", encoding="utf-8")
            fallback, warning = HttpPushSettings.load_or_default(path)
            self.assertEqual(fallback, HttpPushSettings.defaults())
            self.assertIsNotNone(warning)

    def test_http_aggregator_builds_exact_200_sample_payload(self) -> None:
        aggregator = HttpPacketAggregator()
        output = None
        for batch_index in range(10):
            samples = np.full((20, 8), batch_index, dtype=np.int32)
            markers = np.zeros(20, dtype=np.uint16)
            markers[batch_index] = batch_index + 1
            batch = EegBatch(
                stream_id=0x12345678,
                config_id=CONFIG_NORMAL,
                start_seq=batch_index * 20,
                timestamp_us=1_783_985_595_550_000 + batch_index * 20_000,
                samples=samples,
                markers=markers,
            )
            record = batch_to_record(batch, {"device": "P300_HTTP", "eeg_type": 1})
            output = aggregator.ingest(batch, record)
            if batch_index < 9:
                self.assertIsNone(output)

        self.assertIsNotNone(output)
        self.assertEqual(list(output), [
            "data", "timestamp", "sampling_rate", "channels",
            "device", "batch", "markers", "eeg_type",
        ])
        self.assertEqual(len(output["data"]), 200)
        self.assertTrue(all(len(row) == 8 for row in output["data"]))
        self.assertEqual(len(output["markers"]), 200)
        self.assertEqual(output["timestamp"], 1_783_985_595.55)
        self.assertEqual(output["device"], "seu")
        self.assertEqual(output["eeg_type"], 0)
        self.assertEqual(output["markers"][0], 1)
        self.assertEqual(output["markers"][9 * 20 + 9], 10)
        self.assertEqual(aggregator.buffered_samples, 0)

    def test_http_aggregator_resets_on_stream_sequence_and_config_boundaries(self) -> None:
        aggregator = HttpPacketAggregator()

        def ingest(index: int, *, stream_id: int = 1, config_id: int = CONFIG_NORMAL, seq: int | None = None):
            start_seq = index * 20 if seq is None else seq
            batch = EegBatch(
                stream_id, config_id, start_seq, 1_700_000_000_000_000 + start_seq * 1000,
                np.full((20, 8), index, dtype=np.int32), np.zeros(20, dtype=np.uint16),
            )
            return aggregator.ingest(batch, batch_to_record(batch, {"device": "P300_HTTP"}))

        for index in range(5):
            self.assertIsNone(ingest(index))
        self.assertEqual(aggregator.buffered_samples, 100)
        self.assertIsNone(ingest(5, seq=140))
        self.assertEqual(aggregator.buffered_samples, 20)
        self.assertIsNone(ingest(8, stream_id=2, seq=160))
        self.assertEqual(aggregator.buffered_samples, 20)
        self.assertIsNone(ingest(9, stream_id=2, config_id=CONFIG_INTERNAL_SHORT, seq=180))
        self.assertEqual(aggregator.buffered_samples, 0)
        aggregator.reset()
        self.assertEqual(aggregator.buffered_samples, 0)

    def test_http_sender_drops_oldest_and_collapses_backlog_after_failure(self) -> None:
        calls: list[str] = []

        def fake_send(url: str, body: bytes, timeout_s: float) -> tuple[int, str]:
            del url, timeout_s
            device = json.loads(body.decode("utf-8"))["device"]
            calls.append(device)
            if len(calls) == 1:
                raise OSError("offline")
            return 204, "ok"

        sender = HttpJsonSender(
            HttpPushSettings("127.0.0.1", 25000, "/api/bci/data"),
            max_queue=10,
            send_func=fake_send,
        )
        for index in range(4):
            sender.submit(self.make_http_payload(f"packet-{index}"))
        sender.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snapshot = sender.snapshot()
            if snapshot.failed_packets == 1 and snapshot.successful_packets == 1:
                break
            time.sleep(0.01)
        sender.stop()
        snapshot = sender.snapshot()
        self.assertEqual(calls, ["packet-0", "packet-3"])
        self.assertEqual(snapshot.failed_packets, 1)
        self.assertEqual(snapshot.successful_packets, 1)
        self.assertEqual(snapshot.dropped_packets, 2)
        self.assertEqual(snapshot.sent_samples, 200)
        self.assertFalse(snapshot.running)

        bounded = HttpJsonSender(
            HttpPushSettings("127.0.0.1", 25000, "/api/bci/data"),
            max_queue=2,
            send_func=lambda *_: (204, ""),
        )
        bounded.submit(self.make_http_payload("oldest"))
        bounded.submit(self.make_http_payload("middle"))
        bounded.submit(self.make_http_payload("latest"))
        self.assertEqual(bounded.snapshot().dropped_packets, 1)
        self.assertEqual(bounded.snapshot().queue_depth, 2)
        bounded.stop()

    def test_http_sender_posts_json_to_configured_endpoint(self) -> None:
        received: queue.Queue[tuple[str, str, bytes]] = queue.Queue()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
                length = int(self.headers.get("Content-Length", "0"))
                received.put((self.path, self.headers.get("Content-Type", ""), self.rfile.read(length)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        sender = HttpJsonSender(
            HttpPushSettings("127.0.0.1", server.server_address[1], "/api/bci/data")
        )
        try:
            sender.start()
            sender.submit(self.make_http_payload())
            path, content_type, body = received.get(timeout=2.0)
            self.assertEqual(path, "/api/bci/data")
            self.assertEqual(content_type, "application/json")
            self.assertEqual(json.loads(body.decode("utf-8"))["eeg_type"], 0)
            deadline = time.monotonic() + 1.0
            while sender.snapshot().successful_packets < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(sender.snapshot().successful_packets, 1)
        finally:
            sender.stop()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_http_controls_are_independent_and_follow_pause_lifecycle(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "http-settings.json"
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=settings_path,
                auto_start=False,
            )
            try:
                window.http_ip_edit.setText("127.0.0.1")
                window.http_port_edit.setValue(25000)
                window.http_endpoint_edit.setText("/api/bci/data")
                window._start_http_push()
                self.assertIsNotNone(window.http_sender)
                self.assertFalse(window.http_ip_edit.isEnabled())
                self.assertTrue(window.record_button.isEnabled())
                self.assertTrue(settings_path.exists())

                window._set_self_test_active(True)
                window._refresh_http_status()
                self.assertTrue(window.http_button.isEnabled())
                self.assertFalse(window.record_button.isEnabled())
                self.assertIn("内部短接暂停", window.http_status.text())

                window._set_self_test_active(False)
                window.connected = False
                window.has_valid_eeg = False
                window._refresh_http_status()
                self.assertIn("等待NORMAL EEG设备", window.http_status.text())

                window._stop_http_push()
                self.assertIsNone(window.http_sender)
                self.assertTrue(window.http_ip_edit.isEnabled())
                self.assertEqual(window.http_button.text(), "开始 HTTP 推送")
            finally:
                window.close()
                application.processEvents()

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_bias_authority_draft_and_set_interlocks(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                window.connected = True
                window.has_valid_eeg = True
                window.peer_ip = "127.0.0.1"
                window._update_record_controls()
                self.assertTrue(window.bias_query_button.isEnabled())
                self.assertFalse(window.bias_apply_button.isEnabled())

                window.bias_pending = True
                window.bias_last_request_id = 0x12345678
                window._handle_bias_event({
                    "state": "result",
                    "request_id": 0x12345678,
                    "operation": "query",
                    "result": BiasResultData(
                        0x12345678, BiasStatus.OK, 0, 0x35,
                        BiasFlags.VERIFIED, 0xABCDEF01,
                    ),
                })
                self.assertEqual(window.bias_actual_mask, 0x35)
                self.assertEqual(window._bias_checkbox_mask(), 0x35)
                self.assertIn("设备实际 0x35", window.bias_actual_label.text())
                self.assertIn("待应用 0x35", window.bias_draft_label.text())
                self.assertTrue(window.bias_apply_button.isEnabled())

                window.writer = mock.Mock()
                window._update_bias_controls()
                self.assertFalse(window.bias_apply_button.isEnabled())
                window.writer = None
                window.http_sender = mock.Mock()
                window._update_bias_controls()
                self.assertFalse(window.bias_apply_button.isEnabled())
                window.http_sender = None
                window.impedance_active = True
                window._update_bias_controls()
                self.assertFalse(window.bias_apply_button.isEnabled())
                self.assertFalse(window.bias_query_button.isEnabled())
                window.impedance_active = False
                window._update_bias_controls()
                self.assertTrue(window.bias_apply_button.isEnabled())
            finally:
                window.writer = None
                window.http_sender = None
                window.close()
                application.processEvents()

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_tx_toggle_authority_interlocks_and_stable_self_test(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                window.connected = True
                window.has_valid_eeg = True
                window.peer_ip = "127.0.0.1"
                window.model.set_config({
                    "tx_group_frames": 1,
                    "tx_group_period_ms": 20,
                })
                window._set_tx_provisional_from_config()
                self.assertTrue(window.tx_control_button.isEnabled())
                self.assertIn("重新查询", window.tx_control_button.text())

                window.tx_pending = True
                window.tx_last_request_id = 0x10203040
                window._handle_tx_control_event({
                    "state": "result",
                    "request_id": 0x10203040,
                    "operation": "query",
                    "result": TxResultData(
                        0x10203040, TxStatus.OK, 0, 1,
                        TxFlags.VERIFIED, 0x12345678,
                    ),
                })
                self.assertEqual(window.tx_actual_frames, 1)
                self.assertTrue(window.tx_control_button.isEnabled())
                self.assertIn("2帧 / 40 ms", window.tx_control_button.text())

                window._set_self_test_active(True)
                self.assertTrue(window.tx_control_button.isEnabled())
                window.writer = mock.Mock()
                window._update_tx_control()
                self.assertFalse(window.tx_control_button.isEnabled())
                window.writer = None

                window.tx_pending = True
                window._update_impedance_button()
                window._update_self_test_button()
                window._update_record_controls()
                self.assertFalse(window.impedance_button.isEnabled())
                self.assertFalse(window.self_test_button.isEnabled())
                self.assertFalse(window.record_button.isEnabled())
                self.assertFalse(window.http_button.isEnabled())
            finally:
                window.writer = None
                window.close()
                application.processEvents()

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_calibration_requires_two_external_resistor_measurements(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                self.assertIn("板载4.99 kΩ不更换", window.calibration_group.title())
                self.assertEqual(window.calibration_low.value(), 0.0)
                self.assertEqual(window.calibration_high.value(), 0.0)
                self.assertEqual(window.calibration_low.specialValueText(), "未测量")
                self.assertFalse(window.apply_calibration_button.isEnabled())
                self.assertIn("EEGn", window.calibration_explanation.text())
                self.assertIn("REF", window.calibration_explanation.text())

                window.calibration_low.setValue(14.989)
                self.assertFalse(window.apply_calibration_button.isEnabled())
                window.calibration_high.setValue(51.930)
                self.assertTrue(window.apply_calibration_button.isEnabled())
                window.calibration_high.setValue(10.0)
                self.assertFalse(window.apply_calibration_button.isEnabled())
            finally:
                window.close()
                application.processEvents()

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_reference_changes_display_only_and_preserves_marker_state(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                values = np.arange(160, dtype=np.int32).reshape(20, 8)
                markers = np.zeros(20, dtype=np.uint16)
                markers[3] = 1
                batch = EegBatch(1, CONFIG_NORMAL, 0, 1_700_000_000_000_000, values, markers)
                record = batch_to_record(batch, {"sampling_rate": 1000, "vref_v": 4.5, "pga_gain": 12})
                window.model.marker_events.append((1.0, 1))
                window.model.last_marker_code = 1
                car_index = window.reference_combo.findData(ReferenceMode.CAR.value)
                window.reference_combo.setCurrentIndex(car_index)
                displayed = window._filter_display_values(batch, record)
                raw = np.asarray(record["data"], dtype=np.float64)
                np.testing.assert_allclose(displayed, raw - np.mean(raw, axis=1, keepdims=True))
                np.testing.assert_array_equal(np.asarray(record["data"]), raw)
                self.assertEqual(window.model.last_marker_code, 1)
                self.assertEqual(len(window.model.marker_events), 1)
            finally:
                window.close()
                application.processEvents()

    @unittest.skipIf(p300_monitor_tool.QtWidgets is None, "PyQt5/pyqtgraph unavailable")
    def test_gui_force_marker_toggle_resets_http_partial_and_suppresses_marker_lines(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                self.assertFalse(window.force_markers_enabled)
                self.assertFalse(window.force_marker_button.isChecked())
                self.assertIn("关闭", window.force_marker_button.text())

                raw = self.make_batch()
                raw = EegBatch(
                    raw.stream_id,
                    CONFIG_NORMAL,
                    raw.start_seq,
                    raw.timestamp_us,
                    raw.samples,
                    raw.markers,
                )
                raw_markers = raw.markers.copy()
                window.http_aggregator.ingest(
                    raw, batch_to_record(raw, {"device": "P300_FORCE_MARKER"})
                )
                self.assertEqual(window.http_aggregator.buffered_samples, 20)

                window.force_marker_button.click()
                self.assertTrue(window.force_markers_enabled)
                self.assertTrue(window.force_marker_button.isChecked())
                self.assertEqual(window.force_marker_button.text(), "Marker覆盖：全部为1")
                self.assertIn("#f0ad36", window.force_marker_button.styleSheet())
                self.assertFalse(window.force_marker_hint.isHidden())
                self.assertEqual(window.http_aggregator.buffered_samples, 0)

                window.events.put((
                    "batch",
                    {"sampling_rate": 1000, "channels": 8, "device": "P300_FORCE_MARKER"},
                    raw,
                ))
                window._drain_events()
                self.assertEqual(window.model.last_record["markers"], [1] * 20)
                np.testing.assert_array_equal(raw.markers, raw_markers)
                self.assertTrue(all(not lines for lines in window.marker_lines))
                self.assertEqual(window.metric_labels["marker"].text(), "1（强制）")

                window._set_self_test_active(True)
                window._refresh_diagnostics()
                self.assertTrue(window.force_marker_button.isEnabled())
                self.assertTrue(window.force_marker_button.isChecked())
                self.assertEqual(window.metric_labels["marker"].text(), "禁用（自检）")
                window._set_self_test_active(False)
                window._stop_receiver()
                self.assertTrue(window.force_markers_enabled)
                self.assertTrue(window.force_marker_button.isChecked())

                window.force_marker_button.click()
                self.assertFalse(window.force_markers_enabled)
                self.assertEqual(window.force_marker_button.text(), "Marker覆盖：关闭")
                self.assertTrue(window.force_marker_hint.isHidden())

                next_raw = EegBatch(
                    raw.stream_id,
                    CONFIG_NORMAL,
                    raw.start_seq + len(raw.samples),
                    raw.timestamp_us + 20_000,
                    raw.samples,
                    raw.markers,
                )
                window.events.put((
                    "batch",
                    {"sampling_rate": 1000, "channels": 8, "device": "P300_FORCE_MARKER"},
                    next_raw,
                ))
                window._drain_events()
                self.assertEqual(window.model.last_record["markers"], raw_markers.tolist())
                self.assertTrue(all(len(lines) == 1 for lines in window.marker_lines))
            finally:
                window.close()
                application.processEvents()

    @unittest.skipIf(
        p300_monitor_tool.QtWidgets is None or not SCIPY_AVAILABLE,
        "PyQt5/pyqtgraph/SciPy unavailable",
    )
    def test_gui_filter_mode_uses_display_copy_and_preserves_raw_outputs(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                self.assertEqual(window.filter_combo.currentData(), FilterMode.RAW.value)
                self.assertEqual(window.display_filter.mode, FilterMode.RAW)
                source = self.make_batch()
                normal = EegBatch(
                    source.stream_id,
                    CONFIG_NORMAL,
                    source.start_seq,
                    source.timestamp_us,
                    source.samples,
                    source.markers,
                )
                window.model.set_config({"sampling_rate": 1000, "channels": 8})
                window.model.ingest(normal)
                received_samples = window.model.received_samples
                window._set_force_markers_enabled(True)

                notch_index = window.filter_combo.findData(FilterMode.NOTCH_50.value)
                self.assertGreaterEqual(notch_index, 0)
                window.filter_combo.setCurrentIndex(notch_index)
                self.assertEqual(window.display_filter.mode, FilterMode.NOTCH_50)
                self.assertEqual(len(window.model.plot_data()[0]), 0)
                self.assertEqual(window.model.received_samples, received_samples)
                self.assertTrue(window.force_markers_enabled)
                self.assertIn("记录/HTTP：原始EEG", window.filter_status.text())

                self.assertGreaterEqual(window.filter_combo.findData(FilterMode.P300.value), 0)
                new_index = window.filter_combo.findData(FilterMode.P300_2_30.value)
                self.assertGreaterEqual(new_index, 0)
                window.filter_combo.setCurrentIndex(new_index)
                self.assertEqual(window.display_filter.mode, FilterMode.P300_2_30)
                self.assertIn("2–30 Hz", window.filter_status.text())
                self.assertIn("记录/HTTP：原始EEG", window.filter_status.text())

                ssvep_index = window.filter_combo.findData(FilterMode.SSVEP.value)
                self.assertGreaterEqual(ssvep_index, 0)
                window.filter_combo.setCurrentIndex(ssvep_index)
                self.assertEqual(window.display_filter.mode, FilterMode.SSVEP)
                self.assertIn("6–90 Hz", window.filter_status.text())
                self.assertIn("记录/HTTP：原始EEG", window.filter_status.text())

                captured_http_records = []
                window._submit_http_batch = lambda _batch, record: captured_http_records.append(record)
                process_calls = []

                def fake_process(values, **metadata):
                    process_calls.append(metadata)
                    return np.full_like(values, 12.5, dtype=np.float64)

                window.display_filter.process = fake_process
                window.events.put((
                    "batch",
                    {"sampling_rate": 1000, "channels": 8, "device": "filter-test"},
                    normal,
                ))
                window._drain_events()
                _, displayed = window.model.plot_data()
                np.testing.assert_array_equal(displayed, np.full((20, 8), 12.5))
                expected_record = batch_to_record(
                    apply_marker_override(normal, True), window.model.config
                )
                self.assertEqual(window.model.last_record, expected_record)
                self.assertEqual(captured_http_records, [expected_record])
                self.assertEqual(len(process_calls), 1)

                internal = EegBatch(
                    normal.stream_id + 1,
                    CONFIG_INTERNAL_SHORT,
                    0,
                    normal.timestamp_us + 20_000,
                    normal.samples,
                    normal.markers,
                )
                window.events.put((
                    "batch",
                    {"sampling_rate": 1000, "channels": 8, "device": "filter-test"},
                    internal,
                ))
                window._drain_events()
                _, internal_display = window.model.plot_data()
                expected_internal = np.asarray(
                    batch_to_record(internal, window.model.config)["data"], dtype=np.float64
                )
                np.testing.assert_array_equal(internal_display, expected_internal)
                self.assertEqual(len(process_calls), 1)
                self.assertEqual(window.filter_combo.currentData(), FilterMode.SSVEP.value)
            finally:
                window.close()
                application.processEvents()

    @unittest.skipIf(
        p300_monitor_tool.QtWidgets is None,
        "PyQt5/pyqtgraph unavailable",
    )
    def test_gui_quality_layout_is_readable_and_side_panel_is_resizable(self) -> None:
        application = (
            p300_monitor_tool.QtWidgets.QApplication.instance()
            or p300_monitor_tool.QtWidgets.QApplication([])
        )
        with tempfile.TemporaryDirectory() as directory:
            window = p300_monitor_tool.MonitorWindow(
                "127.0.0.1",
                0,
                http_settings_path=Path(directory) / "http-settings.json",
                auto_start=False,
            )
            try:
                window.resize(2048, 1152)
                window.show()
                application.processEvents()
                self.assertIn("font-size: 12pt", window.styleSheet())
                self.assertEqual(window.quality_table.columnCount(), 7)
                self.assertGreaterEqual(window.quality_table.rowHeight(0), 32)
                self.assertTrue(window.quality_detail.wordWrap())
                self.assertGreater(window.side_panel.maximumWidth(), 1000)
                self.assertGreaterEqual(window.side_panel.minimumWidth(), 620)
                self.assertEqual(window.main_splitter.count(), 2)
                self.assertGreaterEqual(window.side_panel.width(), 680)
                self.assertIn("tx", window.metric_labels)
            finally:
                window.close()
                application.processEvents()

    def test_simulator_tcp_batches_push_about_five_http_packets_per_second(self) -> None:
        received: queue.Queue[dict] = queue.Queue()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
                length = int(self.headers.get("Content-Length", "0"))
                received.put(json.loads(self.rfile.read(length).decode("utf-8")))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:
                del format, args

        http_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            tcp_port = probe.getsockname()[1]
        events: queue.Queue = queue.Queue()
        receiver = TcpReceiver("127.0.0.1", tcp_port, events)
        sender = HttpJsonSender(
            HttpPushSettings("127.0.0.1", http_server.server_address[1], "/api/bci/data")
        )
        aggregator = HttpPacketAggregator()
        model = MonitorDataModel(history_seconds=1.0)
        try:
            receiver.start()
            sender.start()
            time.sleep(0.05)
            run_simulator("127.0.0.1", tcp_port, SignalGenerator(seed=29), duration_s=1.05)
            time.sleep(0.15)
            receiver.stop()
            receiver.join(timeout=1.0)

            while not events.empty():
                event = events.get_nowait()
                if event[0] != "batch":
                    continue
                model.set_config(event[1])
                batch = event[2]
                payload = aggregator.ingest(batch, model.ingest(batch))
                if payload is not None:
                    sender.submit(payload)

            deadline = time.monotonic() + 2.0
            while sender.snapshot().successful_packets < 5 and time.monotonic() < deadline:
                time.sleep(0.01)
            payloads = []
            while not received.empty():
                payloads.append(received.get_nowait())
            self.assertEqual(len(payloads), 5)
            self.assertTrue(all(len(payload["data"]) == 200 for payload in payloads))
            self.assertTrue(all(len(payload["markers"]) == 200 for payload in payloads))
            self.assertTrue(all(payload["eeg_type"] == 0 for payload in payloads))
            self.assertEqual(sender.snapshot().sent_samples, 1000)
        finally:
            receiver.stop()
            receiver.join(timeout=1.0)
            sender.stop()
            http_server.shutdown()
            http_server.server_close()
            http_thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
