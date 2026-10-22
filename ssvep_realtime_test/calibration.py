"""Non-overlapping calibration windows and conservative single-target rejection.

Calibration chooses channels; subsequent test windows are never used to tune the
thresholds. Scores remain matching measures, not detection probabilities.
"""
import json
from pathlib import Path
import time
import numpy as np


def signature(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def upper_bound(values, increment=0.0):
    values = np.asarray(values)
    median = np.median(values)
    mad = np.median(np.abs(values - median)) * 1.4826
    return float(np.quantile(values, .95) + 2 * mad + increment + 1e-6)


class Calibration:
    def __init__(self, config, path=None):
        self.config = config
        self.path = Path(path) if path else None
        self.model = None
        self.context = None
        self.mode = "test"
        self.baseline, self.response = [], []
        self.last_capture = None
        self.confirmed_since = None
        self.error = None
        if self.path and self.path.exists():
            try:
                model = json.loads(self.path.read_text(encoding="utf-8"))
                if model.get("version") != 1 or signature(model["settings"]) != signature(config.analysis_signature()):
                    raise ValueError("标定设置与当前参数不一致")
                expected = {"target_rho", "target_score", "target_snr_db", "target_margin", "cca_margin"}
                if not isinstance(model.get("context"), dict) or not model.get("channels") or any(c not in config.channels for c in model["channels"]):
                    raise ValueError("标定通道或来源信息无效")
                if set(model.get("thresholds", {})) != expected or not all(np.isfinite(float(v)) for v in model["thresholds"].values()):
                    raise ValueError("标定阈值无效")
                if not isinstance(model.get("channel_ranking"), list) or "selection_supported" not in model:
                    raise ValueError("标定通道排名缺失")
                self.model = model
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self.error = f"旧标定未加载：{exc}"

    def set_context(self, context):
        if signature(self.context) == signature(context):
            return
        self.context = context
        if self.model and signature(self.model["context"]) != signature(context):
            self.model = None
            self.error = "来源或实际量程变化，需要重新标定"
        self.baseline, self.response = [], []
        self.last_capture = None
        self.confirmed_since = None

    def restart(self, mode):
        self.mode = mode
        self.last_capture = None
        self.confirmed_since = None
        if mode == "baseline":
            self.baseline, self.response, self.model = [], [], None
            self.error = None
        elif mode == "response":
            self.response = []

    @property
    def channels(self):
        return self.model["channels"] if self.mode == "test" and self.model else None

    def decorate(self, analyzer, result, data_end):
        result.update(detected=False, phase=self.mode)
        if result.get("target_excluded"):
            self.confirmed_since = None
            result["decision"] = "当前刺激16.5 Hz已屏蔽，不能标定或识别该目标"
            return result
        if self.mode != "test":
            return self._collect(analyzer, result, data_end)
        if not result.get("valid"):
            self.confirmed_since = None
            result["decision"] = "信号质量或连续数据不足"
            return result
        if not self.model:
            self.confirmed_since = None
            result["decision"] = self.error or "未标定，候选仅供诊断；按 R 标定"
            return result
        result["calibrated_channels"] = self.model["channels"]
        result["channel_ranking"] = self.model["channel_ranking"]
        result["selection_supported"] = self.model["selection_supported"]
        if result["used_channels"] != self.model["channels"]:
            self.confirmed_since = None
            result["decision"] = "标定通道质量不足，拒绝识别"
            return result
        tests = {key: float(result[key]) > threshold for key, threshold in self.model["thresholds"].items()}
        tests["CCA/FBCCA一致且匹配刺激"] = result["cca_frequency"] == result["fbcca_frequency"] == self.config.frequency
        result["thresholds"] = self.model["thresholds"]
        result["checks"] = tests
        if not all(tests.values()):
            self.confirmed_since = None
            result["decision"] = "未检出可靠响应"
        else:
            if self.confirmed_since is None:
                self.confirmed_since = data_end
            result["detected"] = data_end - self.confirmed_since >= self.config.confirmation_seconds
            result["decision"] = "检测通过" if result["detected"] else "候选响应确认中"
        return result

    def _collect(self, analyzer, result, data_end):
        windows = self.baseline if self.mode == "baseline" else self.response
        required = self.config.baseline_windows if self.mode == "baseline" else self.config.response_windows
        label = "静态基线" if self.mode == "baseline" else "刺激响应"
        result["calibration_instruction"] = "保持睁眼并注视静态灰块" if self.mode == "baseline" else "注视闪烁块，保持头眼稳定"
        if len(windows) < required and result.get("valid") and data_end is not None and (
                self.last_capture is None or data_end - self.last_capture >= self.config.window_seconds - .002):
            windows.append(analyzer.snapshot())
            self.last_capture = data_end
        result["calibration"] = {"phase": self.mode, "collected": len(windows), "required": required}
        result["decision"] = f"{label}标定 {len(windows)}/{required} 个独立窗"
        if len(windows) >= required:
            if self.mode == "baseline":
                result["action"] = "start_response"
            else:
                try:
                    self.model = self.fit(analyzer)
                    if self.path:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(self.model, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                        temporary.replace(self.path)
                    result["action"] = "start_test"
                    result["channel_ranking"] = self.model["channel_ranking"]
                    result["calibrated_channels"] = self.model["channels"]
                    result["selection_supported"] = self.model["selection_supported"]
                except (ValueError, OSError) as exc:
                    self.error = str(exc)
                    result["decision"] = f"标定失败：{exc}；按 R 重试"
        return result

    def fit(self, analyzer):
        if len(self.baseline) < self.config.baseline_windows:
            raise ValueError("缺少完整静态基线")
        eligible = set(self.config.channels)
        for snapshot in self.baseline + self.response:
            eligible.intersection_update(snapshot["used"])
        if not eligible:
            raise ValueError("没有跨标定窗持续有效的通道")
        target = self.config.candidates.index(self.config.frequency)
        background = np.array([x["diagnostics"]["snr_db"][target] for x in self.baseline])
        responses = np.array([x["diagnostics"]["snr_db"][target] for x in self.response])
        gains = np.median(responses, axis=0) - np.median(background, axis=0)
        rankings = [{"channel": c, "snr_gain_db": float(gains[c - 1]),
                     "consistent": bool(np.mean(responses[:, c - 1] > np.median(background[:, c - 1]) + 3) >= 2 / 3)}
                    for c in sorted(eligible)]
        rankings.sort(key=lambda x: x["snr_gain_db"], reverse=True)
        supported = [r["channel"] for r in rankings if r["consistent"] and r["snr_gain_db"] >= 3]
        channels = sorted(supported[:self.config.max_calibrated_channels]) if supported else sorted(eligible)
        baseline_results = [analyzer.result(channels=channels, snapshot=s) for s in self.baseline]
        thresholds = {key: upper_bound([r[key] for r in baseline_results], 3 if key == "target_snr_db" else 0)
                      for key in ("target_rho", "target_score", "target_snr_db", "target_margin", "cca_margin")}
        thresholds["target_margin"] = max(0.0, thresholds["target_margin"])
        thresholds["cca_margin"] = max(0.0, thresholds["cca_margin"])
        if thresholds["target_rho"] >= 1 or thresholds["target_score"] >= float(np.sum(np.arange(1, 4) ** -1.25 + .25)):
            raise ValueError("静态基线不稳定，无法建立可用阈值")
        return {"version": 1, "created_unix": time.time(), "settings": self.config.analysis_signature(),
                "context": self.context, "channels": channels, "channel_ranking": rankings,
                "selection_supported": bool(supported), "thresholds": thresholds,
                "baseline_windows": len(self.baseline), "response_windows": len(self.response),
                "note": "经验基线阈值，非概率；独立测试数据不用于调参"}
