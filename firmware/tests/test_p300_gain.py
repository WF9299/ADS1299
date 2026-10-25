"""Gain wire transports, truthful UI state and scale boundaries; no real device IO."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import json
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.p300_gain import *
from tools.p300_common import EegBatch, batch_to_record, crc32c
from tools.p300_http_push import HttpPacketAggregator
from tools.p300_serial import SerialSession
from tools.p300_serial_protocol import SerialParser, CONTROL, CONTROL_REPLY, CONFIG
from tools import p300_monitor
from test_p300_dual_transport import FakePort


def config(gain=12, stream=100):
    return dict(sample_rate_hz=1000, config_id=1, stream_id=stream,
                vref_v=4.5, pga_gain=gain, gain_control=True, gain_verified=True)


def batch(gain=12, stream=100, seq=0):
    # Same physical 100-uV input encoded at the selected hardware gain.
    code=round(100e-6*gain/4.5*8388607)
    return EegBatch(stream, 1, seq, 1800000000000000+seq*1000,
        np.full((20,8),code,dtype=np.int32), np.zeros(20,dtype=np.uint16))


class GainPort(FakePort):
    gain=12
    def config(self):
        self.reply(CONFIG,0,json.dumps(config(self.gain,self.stream_id)).encode())
    def write(self,data):
        frames=SerialParser().feed(data)
        if len(frames)==1 and frames[0].kind==CONTROL and frames[0].payload[3]==GAIN_CONTROL_REQUEST:
            frame=frames[0]
            rid,op,gain=parse_gain_control_request(frame.payload)
            if op==GainOperation.SET:
                self.gain=gain
                self.stream_id+=1
            self.reply(CONTROL_REPLY,frame.request_id,build_gain_control_result(
                GainResultData(rid,GainStatus.OK,gain,self.gain,GainFlags.VERIFIED,self.stream_id)))
            if op==GainOperation.SET: self.config()
            return len(data)
        return super().write(data)


class GainProtocolTests(unittest.TestCase):
    def test_seven_gains_validation_crc_reserved_and_failure_state(self):
        self.assertNotEqual(GAIN_CONTROL_REQUEST,14)  # Existing TIME_ANCHOR.
        for gain in GAINS:
            packet=build_gain_control_request(7,GainOperation.SET,gain)
            self.assertEqual(parse_gain_control_request(packet),(7,GainOperation.SET,gain))
        for gain in (0,3,16,25,255,1.5):
            with self.assertRaises(ValueError): build_gain_control_request(1,GainOperation.SET,gain)
        with self.assertRaises(ValueError): build_gain_control_request(1,GainOperation.QUERY,12)
        p=bytearray(build_gain_control_request(1,GainOperation.SET,4))
        p[10]=1;p[12:]=crc32c(p[:12]).to_bytes(4,'big')
        with self.assertRaises(GainProtocolError): parse_gain_control_request(p)
        p[15]^=1
        with self.assertRaises(GainProtocolError): parse_gain_control_request(p)
        failed=GainResultData(8,GainStatus.RESTORE_FAILED,24,0,GainFlags.CHANGED,0)
        self.assertFalse(parse_gain_control_result(build_gain_control_result(failed)).verified)
        invalid=GainResultData(8,GainStatus.RESTORE_FAILED,24,12,GainFlags.VERIFIED,100)
        with self.assertRaises(GainProtocolError): parse_gain_control_result(build_gain_control_result(invalid))

    def test_udp_same_id_retry_and_set_timeout_query_reconciliation(self):
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1',0));sock.settimeout(2)
        requests=[];failures=[]
        def server():
            try:
                for i in range(2):
                    packet,addr=sock.recvfrom(128)
                    rid,op,gain=parse_gain_control_request(packet);requests.append((rid,op,gain))
                    if i==0: continue
                    sock.sendto(build_gain_control_result(GainResultData(rid,GainStatus.OK,
                        gain,gain,GainFlags.VERIFIED,101)),addr)
            except Exception as e: failures.append(e)
        worker=threading.Thread(target=server);worker.start()
        try:
            client=GainControlClient('127.0.0.1',sock.getsockname()[1],timeout_s=.08,retries=1)
            self.assertTrue(client.set_gain(6,91).applied)
            self.assertEqual(requests,[(91,GainOperation.SET,6)]*2)
            self.assertFalse(failures)
        finally:
            worker.join(2);sock.close()
        def exchange(packet,*args):
            rid,op,gain=parse_gain_control_request(packet)
            if op==GainOperation.SET: raise TimeoutError('lost response')
            return build_gain_control_result(GainResultData(rid,GainStatus.OK,0,6,GainFlags.VERIFIED,101))
        outcome=GainControlClient('TEST',exchange=exchange).set_gain(6)
        self.assertTrue(outcome.reconciled and outcome.applied)

    def test_serial_control_and_fresh_config_use_single_owner(self):
        session=SerialSession('TEST',921600,queue.Queue(),port_factory=GainPort)
        try:
            session.open()
            client=GainControlClient('TEST',exchange=session.control_exchange)
            self.assertEqual(client.query().actual_gain,12)
            self.assertTrue(client.set_gain(24).applied)
            limit=time.monotonic()+1
            while session.config.get('pga_gain')!=24 and time.monotonic()<limit: time.sleep(.01)
            self.assertEqual(session.config['pga_gain'],24)
        finally: session.close()

    def test_input_uv_is_independent_of_gain_and_raw_record_carries_scale(self):
        for gain in GAINS:
            r=batch_to_record(batch(gain),config(gain))
            np.testing.assert_allclose(r['data'],100,atol=.3)
            self.assertEqual((r['units'],r['vref_v'],r['pga_gain']),('uV',4.5,gain))

    def test_http_aggregation_never_mixes_range_even_with_same_stream_id(self):
        agg=HttpPacketAggregator()
        for i in range(9): self.assertIsNone(agg.ingest(batch(12,seq=i*20),batch_to_record(batch(12,seq=i*20),config())))
        for i in range(9,18): self.assertIsNone(agg.ingest(batch(24,seq=i*20),batch_to_record(batch(24,seq=i*20),config(24))))
        result=agg.ingest(batch(24,seq=360),batch_to_record(batch(24,seq=360),config(24)))
        self.assertEqual(len(result['data']),200)
        self.assertAlmostEqual(result['timestamp'],batch(seq=180).timestamp_us/1e6)


@unittest.skipIf(p300_monitor.QtWidgets is None,'Qt unavailable')
class GainGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=p300_monitor.QtWidgets.QApplication.instance() or p300_monitor.QtWidgets.QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.w=p300_monitor.MonitorWindow('127.0.0.1',0,
            http_settings_path=Path(self.tmp.name)/'settings.json',auto_start=False)
        self.w.connected=True;self.w.peer_ip='TEST';self.w.has_valid_eeg=True
        self.w._apply_stream_config(config())
    def tearDown(self):
        self.w.writer=self.w.http_sender=None
        self.w.close();self.app.processEvents();self.tmp.cleanup()
    def deliver(self,gain=24,status=GainStatus.OK,flags=GainFlags.VERIFIED,stream=101):
        self.w.gain_last_request_id=44;self.w.gain_pending=True
        result=GainResultData(44,status,24,gain,flags,stream)
        self.w._handle_gain_event(dict(state='result',request_id=44,result=GainSetOutcome(result,24,44)))
    def test_draft_never_changes_actual_or_conversion_and_legacy_disables(self):
        self.w.gain_combo.setCurrentIndex(GAINS.index(24))
        self.assertEqual(self.w.gain_actual,12)
        self.assertEqual(self.w.model.config['pga_gain'],12)
        self.assertTrue(self.w.gain_apply_button.isEnabled())
        self.w._apply_stream_config(dict(sample_rate_hz=1000,pga_gain=12))
        self.assertFalse(self.w.gain_apply_button.isEnabled())
        self.assertIn('不支持',self.w.gain_status.text())
        self.w._apply_stream_config(config())
        self.assertTrue(self.w.gain_apply_button.isEnabled())
        self.assertNotIn('不支持',self.w.gain_status.text())
    def test_ack_waits_for_matching_config_and_config_before_ack_also_works(self):
        self.deliver()
        self.assertEqual(self.w.model.config['pga_gain'],12)
        self.assertTrue(self.w.gain_pending)
        self.w._apply_stream_config(config(24,101))
        self.assertFalse(self.w.gain_pending)
        self.deliver()
        self.assertFalse(self.w.gain_pending)
        self.assertIn('同步',self.w.gain_status.text())
    def test_pending_gain_blocks_other_controls_and_recording_blocks_gain(self):
        self.w.gain_pending=True
        self.w._update_record_controls();self.w._update_impedance_button();self.w._update_self_test_button()
        self.assertFalse(self.w.record_button.isEnabled())
        self.assertFalse(self.w.http_button.isEnabled())
        self.assertFalse(self.w.impedance_button.isEnabled())
        self.assertFalse(self.w.self_test_button.isEnabled())
        self.assertFalse(self.w.bias_query_button.isEnabled())
        self.w.gain_pending=False
        for attr in ('writer','http_sender'):
            setattr(self.w,attr,Mock());self.w._update_gain_controls()
            self.assertFalse(self.w.gain_apply_button.isEnabled())
            setattr(self.w,attr,None)
        self.w._update_gain_controls();self.assertTrue(self.w.gain_apply_button.isEnabled())
    def test_scale_change_clears_history_stops_outputs_and_uses_batch_metadata(self):
        old=batch();self.w.model.ingest(old)
        writer=Mock();sender=Mock()
        self.w.writer=writer;self.w.http_sender=sender
        self.w._apply_stream_config(config(24,101))
        self.assertEqual(len(self.w.model._values),0)
        writer.close.assert_called_once();sender.stop.assert_called_once()
        self.assertIsNone(self.w.writer);self.assertIsNone(self.w.http_sender)
        self.assertIn('量程变化',self.w.http_detail.text())
        self.w.events.put(('batch',config(24,101),batch(24,101)))
        self.w._drain_events()
        np.testing.assert_allclose(self.w.model.last_record['data'],100,atol=.03)
    def test_failed_rollback_never_claims_verified_gain(self):
        self.deliver(0,GainStatus.RESTORE_FAILED,GainFlags.CHANGED,0)
        self.assertIsNone(self.w.gain_actual)
        self.assertTrue(self.w.restore_failed)
        self.assertFalse(self.w.gain_apply_button.isEnabled())
    def test_apply_failure_reports_restored_value_and_config_timeout_unlocks(self):
        self.deliver(12,GainStatus.APPLY_FAILED,stream=100)
        self.assertEqual(self.w.gain_actual,12)
        self.assertIn('回滚',self.w.gain_status.text())
        self.deliver()
        self.w.gain_deadline=0
        self.w._update_gain_controls()
        self.assertFalse(self.w.gain_pending)
        self.assertIsNone(self.w.gain_actual)
