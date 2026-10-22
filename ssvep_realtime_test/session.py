"""Bounded asynchronous diagnostic logging: no disk IO on the frame callback."""
import csv
import json
from pathlib import Path
import queue
import threading


class SessionLog:
    def __init__(self, folder, config):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        config.save(self.folder / "settings.json")
        self.events = queue.Queue(maxsize=8192)
        self.stop_event = threading.Event()
        self.dropped = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def put(self, kind, data):
        try:
            self.events.put_nowait((kind, data))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            with (self.folder / "frames.csv").open("w", newline="", encoding="utf-8") as frames, (self.folder / "analysis.jsonl").open("w", encoding="utf-8") as analysis:
                writer = csv.writer(frames)
                writer.writerow(("frame", "swap_monotonic", "interval_ms", "white", "phase_seconds", "estimated_missed_frames", "target_frequency", "trial_id"))
                while not self.stop_event.is_set() or not self.events.empty():
                    try:
                        kind, data = self.events.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if kind == "frame":
                        writer.writerow(data)
                    else:
                        analysis.write(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n")
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        (self.folder / "logging_status.json").write_text(json.dumps({"dropped_log_entries": self.dropped,
            "error": self.error, "writer_finished": not self.thread.is_alive()}, ensure_ascii=False), encoding="utf-8")


class RawRecorder:
    """Worker-local queue keeps raw JSON encoding/disk activity out of analysis."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue(maxsize=128)
        self.stop_event = threading.Event()
        self.dropped = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def put(self, packet):
        try:
            self.events.put_nowait(packet)
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            with (self.folder / "raw_eeg.jsonl").open("w", encoding="utf-8") as output:
                while not self.stop_event.is_set() or not self.events.empty():
                    try:
                        item = self.events.get(timeout=.1)
                    except queue.Empty:
                        continue
                    # Keep nonfinite input evidence rather than silently alter raw
                    # values. Python's JSON reader accepts these NaN/Infinity tokens.
                    output.write(json.dumps(item, ensure_ascii=False) + "\n")
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        (self.folder / "raw_recording_status.json").write_text(json.dumps({"dropped_packets": self.dropped,
            "error": self.error, "writer_finished": not self.thread.is_alive()}, ensure_ascii=False), encoding="utf-8")
