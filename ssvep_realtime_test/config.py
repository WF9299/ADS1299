"""Validated experiment settings and physical display geometry (no Qt dependency)."""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path


def parse_candidates(text):
    values = []
    for part in text.replace("，", ",").split(","):
        if ":" in part:
            start, stop, step = map(float, part.split(":"))
            if not all(map(math.isfinite, (start, stop, step))) or step <= 0 or stop < start:
                raise ValueError("候选范围应为 起点:终点:步长，例如 8:30:0.5")
            count = int((stop - start) / step + 1.000001)
            if count > 256:
                raise ValueError("候选频点最多 256 个")
            values.extend(start + i * step for i in range(count))
        elif part.strip():
            values.append(float(part))
    return tuple(sorted(set(round(v, 6) for v in values)))


def parse_channels(text):
    return tuple(sorted(set(int(v.strip()) for v in text.replace("，", ",").split(",") if v.strip())))


@dataclass
class Settings:
    schema_version: int = 4
    frequency: float = 17.0
    candidates: tuple = field(default_factory=lambda: parse_candidates("8:30:0.5"))
    legacy_candidates: tuple | None = None
    channels: tuple = tuple(range(1, 9))
    window_seconds: float = 4.0
    update_seconds: float = 0.2
    stale_seconds: float = 1.0
    harmonics: int = 4
    analysis_profile: str = "matlab"
    calibrate_before_run: bool = False
    frequency_center: float | None = None
    switch_transition_seconds: float = 0.5
    exclude_165: bool = False
    baseline_windows: int = 10
    response_windows: int = 3
    max_calibrated_channels: int = 3
    confirmation_seconds: float = 0.8
    record_raw: bool = True
    notch: bool = True
    fullscale_uv: float = 375000.0
    port: int = 25001
    screen_index: int = 0
    diagonal_inches: float = 27.0
    native_width: int = 2560
    native_height: int = 1440
    expected_refresh: float = 210.0
    size_mm: float = 50.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    calibrated: bool = False
    screen_fingerprint: str = ""

    def validate(self):
        if self.frequency_center is None:
            self.frequency_center = self.frequency
        numeric = (self.frequency, self.window_seconds, self.update_seconds, self.stale_seconds,
                   self.fullscale_uv, self.diagonal_inches, self.expected_refresh, self.size_mm,
                   self.scale_x, self.scale_y, self.confirmation_seconds, self.frequency_center,
                   self.switch_transition_seconds)
        if not all(math.isfinite(v) for v in numeric):
            raise ValueError("设置中不能包含 NaN 或无穷大")
        if not 6 <= self.frequency <= 80:
            raise ValueError("刺激频率范围为 6–80 Hz，并且必须小于实际刷新率的一半")
        if self.analysis_profile == "matlab":
            self.harmonics = 4
            self.calibrate_before_run = False
            if self.legacy_candidates is None:
                self.legacy_candidates = tuple(self.candidates)
            self.candidates = self.analysis_frequencies
            if self.candidates[0] < 6 or self.candidates[-1] * 4 >= 125:
                raise ValueError("当前刺激的完整CCA±4 Hz范围要求刺激≥10且<27.25 Hz，四次谐波需低于125 Hz")
            if self.frequency not in self.candidates:
                raise ValueError("当前刺激不在初始中心±4 Hz、0.5 Hz步进的频点中")
        if not 0 <= self.switch_transition_seconds <= 2:
            raise ValueError("切频过渡时间需为0–2秒")
        if not self.candidates or len(self.candidates) > 256 or any(
                not math.isfinite(v) or not 6 <= v <= 80 for v in self.candidates):
            raise ValueError("候选频率需在 6–80 Hz 内，至少两个、最多 256 个")
        self.candidates = tuple(sorted(set(self.candidates) | {self.frequency}))
        if not 2 <= len(self.candidates) <= 256:
            raise ValueError("需要 2–256 个不同的候选频率，含刺激频率")
        if len([f for f in self.candidates if f not in self.excluded_frequencies]) < 2:
            raise ValueError("屏蔽后需要至少两个有效候选频率")
        self.channels = tuple(sorted(set(self.channels)))
        if not self.channels or any(c not in range(1, 9) for c in self.channels):
            raise ValueError("请选择 CH1–CH8 中至少一个实际采集通道")
        if not 1 <= self.window_seconds <= 10 or not 0.1 <= self.update_seconds <= 2:
            raise ValueError("分析窗范围 1–10 秒，更新间隔 0.1–2 秒")
        if not 0.5 <= self.stale_seconds <= 5 or self.fullscale_uv <= 0:
            raise ValueError("断流超时需为 0.5–5 秒；满量程必须大于 0")
        if not 1 <= self.harmonics <= 5 or not 1 <= self.port <= 65535:
            raise ValueError("谐波数为 1–5；HTTP 端口为 1–65535")
        if self.analysis_profile not in ("matlab", "focused", "wide"):
            raise ValueError("分析模式应为 matlab、focused 或 wide")
        if self.analysis_profile == "focused" and self.frequency > 22:
            raise ValueError("6–45 Hz 调试模式的刺激频率需≤22 Hz，以保留二次谐波；其他频率请选宽带模式")
        if self.frequency > self.bands[0][1] - 1 or any(f > self.bands[0][1] - 1 for f in self.candidates):
            raise ValueError("候选/刺激频率必须低于所选分析带宽上限 1 Hz；较高频率请选择宽带模式")
        if not 3 <= self.baseline_windows <= 30 or not 2 <= self.response_windows <= 10:
            raise ValueError("静态标定需 3–30 个独立窗，响应标定需 2–10 个独立窗")
        if not 1 <= self.max_calibrated_channels <= 8 or not 0.2 <= self.confirmation_seconds <= 3:
            raise ValueError("标定通道数应为 1–8；确认时间为 0.2–3 秒")
        if self.screen_index < 0 or min(self.native_width, self.native_height) <= 0:
            raise ValueError("显示器参数无效")
        if not 10 <= self.diagonal_inches <= 100 or not 20 <= self.expected_refresh <= 1000:
            raise ValueError("显示器尺寸或刷新率超出范围")
        if not 0.2 <= self.scale_x <= 5 or not 0.2 <= self.scale_y <= 5 or self.size_mm != 50:
            raise ValueError("校准系数范围 0.2–5；本实验固定为 50 mm")
        return self

    @property
    def analysis_frequencies(self):
        return tuple(round(self.frequency + .5 * k, 6) for k in range(-8, 9))

    @property
    def excluded_frequencies(self):
        return (16.5,) if self.exclude_165 else ()

    @property
    def switch_frequencies(self):
        center = self.frequency if self.frequency_center is None else self.frequency_center
        return tuple(round(center + .5 * k, 6) for k in range(-8, 9))

    @property
    def bands(self):
        high = 45.0 if self.analysis_profile == "focused" else 90.0
        return ((6.0, high), (14.0, high), (22.0, high))

    def analysis_signature(self):
        return {k: self.to_dict()[k] for k in ("frequency", "candidates", "channels", "window_seconds",
                "harmonics", "analysis_profile", "notch", "fullscale_uv", "confirmation_seconds", "exclude_165")}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version", 1) < 2:
            data.update(schema_version=2, window_seconds=4.0, analysis_profile="focused", harmonics=2)
            if data.get("frequency", 17) > 22 or any(f > 44 for f in data.get("candidates", ())):
                data.update(analysis_profile="wide", harmonics=3)
        if data.get("schema_version", 1) < 3:
            center = data.get("frequency", 17.0)
            data.update(schema_version=3, frequency_center=center)
            if 10 <= center < 27.25:
                data.update(analysis_profile="matlab", harmonics=4, calibrate_before_run=False)
        data["schema_version"] = 4
        return cls(**data).validate()

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def square_pixels(config, display_width, display_height):
    diagonal_mm = config.diagonal_inches * 25.4
    norm = math.hypot(config.native_width, config.native_height)
    panel_width = diagonal_mm * config.native_width / norm
    panel_height = diagonal_mm * config.native_height / norm
    return (round(config.size_mm * display_width / panel_width * config.scale_x),
            round(config.size_mm * display_height / panel_height * config.scale_y))


def stimulus_white(frequency, elapsed):
    """Sample a 50% duty continuous-phase square wave on the presentation grid."""
    return math.cos(2 * math.pi * frequency * elapsed) >= 0
