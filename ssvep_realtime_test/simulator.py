"""Explicit synthetic HTTP sender for numerical/link checks, never real EEG."""
import argparse
import json
import time
from urllib.request import Request, urlopen
import numpy as np


def samples(frequency, sampling_rate, start, count, rng):
    t = (start + np.arange(count)) / sampling_rate
    phases = np.arange(8) * 0.17
    eeg = sum((18 / k) * np.sin(2 * np.pi * frequency * k * t[:, None] + phases)
              for k in (1, 2, 3))
    return eeg + rng.normal(0, 18, (count, 8))


def send_stream(frequency=17, port=25001, duration=10, sampling_rate=1000, baseline_seconds=0):
    rng = np.random.default_rng(42)
    batch = round(sampling_rate * 0.2)
    epoch = time.time()
    monotonic = time.monotonic()
    total = 0
    for index in range(int(duration / 0.2)):
        due = monotonic + (index + 1) * 0.2
        time.sleep(max(0, due - time.monotonic()))
        data = rng.normal(0, 18, (batch, 8)) if index * .2 < baseline_seconds else samples(frequency, sampling_rate, index * batch, batch, rng)
        payload = {"data": data.tolist(),
                   "timestamp": epoch + index * 0.2, "sampling_rate": sampling_rate, "channels": 8,
                   "device": "SSVEP_SIMULATOR", "batch": True, "markers": [0] * batch, "eeg_type": 0,
                   "units": "uV", "vref_v": 4.5, "pga_gain": 12}
        request = Request(f"http://127.0.0.1:{port}/api/bci/data", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            response.read()
        total += batch
    return total


def main():
    parser = argparse.ArgumentParser(description="模拟 EEG HTTP 发送器；不会连接硬件")
    parser.add_argument("--frequency", type=float, default=17)
    parser.add_argument("--port", type=int, default=25001)
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--baseline-seconds", type=float, default=0)
    args = parser.parse_args()
    print(f"SIMULATION: {send_stream(args.frequency, args.port, args.duration, baseline_seconds=args.baseline_seconds)} samples sent")


if __name__ == "__main__":
    main()
