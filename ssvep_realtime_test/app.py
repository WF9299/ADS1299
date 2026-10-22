import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from .config import Settings, parse_candidates, parse_channels
from .receiver import WorkerController
from .session import SessionLog

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="C5 Monitor 配套 SSVEP 刺激与 CCA/FBCCA 实时分析")
    parser.add_argument("--config", type=Path, default=ROOT / "ssvep_realtime_test_settings.json")
    parser.add_argument("--frequency", type=float)
    parser.add_argument("--channels", type=parse_channels)
    parser.add_argument("--candidates", type=parse_candidates)
    parser.add_argument("--window", type=float)
    parser.add_argument("--profile", choices=("matlab", "focused", "wide"))
    calibration = parser.add_mutually_exclusive_group()
    calibration.add_argument("--calibrate", action="store_true", default=None)
    calibration.add_argument("--skip-calibration", dest="calibrate", action="store_false")
    parser.add_argument("--calibration-file", type=Path, default=ROOT / "ssvep_realtime_calibration.json")
    parser.add_argument("--no-raw", action="store_true")
    exclusion = parser.add_mutually_exclusive_group()
    exclusion.add_argument("--exclude-165", dest="exclude_165", action="store_true", default=None)
    exclusion.add_argument("--include-165", dest="exclude_165", action="store_false")
    parser.add_argument("--port", type=int)
    parser.add_argument("--screen", type=int, help="显示器索引，从 0 开始")
    parser.add_argument("--no-dialog", action="store_true", help="直接开始全屏闪烁")
    parser.add_argument("--headless", action="store_true", help="仅测试 HTTP/分析，不产生视觉刺激")
    parser.add_argument("--duration", type=float, default=10, help="headless 运行秒数")
    parser.add_argument("--preview", type=Path, help="保存静态 PNG 预览，不开始闪烁")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "ssvep_realtime")
    args = parser.parse_args(argv)
    try:
        config = Settings.load(args.config) if args.config.exists() else Settings()
        for field, value in (("frequency", args.frequency), ("channels", args.channels), ("candidates", args.candidates),
                             ("window_seconds", args.window), ("port", args.port), ("screen_index", args.screen)):
            if value is not None:
                setattr(config, field, value)
        if args.profile:
            config.analysis_profile = args.profile
            config.harmonics = {"matlab": 4, "focused": 2, "wide": 3}[args.profile]
        if args.frequency is not None:
            config.frequency_center = args.frequency
        if args.calibrate is not None:
            config.calibrate_before_run = args.calibrate
        if args.no_raw:
            config.record_raw = False
        if args.exclude_165 is not None:
            config.exclude_165 = args.exclude_165
        config.validate()
    except (OSError, ValueError, TypeError) as exc:
        print(f"设置错误：{exc}", file=sys.stderr)
        return 2
    if args.headless:
        return headless(config, args.duration, args.output, args.calibration_file,
                        args.calibrate is True and config.analysis_profile != "matlab")
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont, QImage, QPainter, QSurfaceFormat
    from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    fmt = QSurfaceFormat()
    fmt.setSwapInterval(1)
    fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Microsoft YaHei", 10))
    if args.preview:
        # Windows' offscreen Qt plugin may not enumerate system fonts. Load a
        # local font for this static artifact only; the visible app uses Qt's font database.
        from PyQt5.QtGui import QFontDatabase
        import os
        if not QFontDatabase().families():
            font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
            if font_file.exists():
                QFontDatabase.addApplicationFont(str(font_file))
        from .display import draw_scene
        image = QImage(2560, 1440, QImage.Format_RGB32)
        painter = QPainter(image)
        from .analysis import Analyzer
        from .simulator import samples
        from .cca_chart import render_cca_panel
        import numpy as np
        analyzer = Analyzer(config)
        analyzer.feed(samples(config.frequency, 1000, 0, round((config.window_seconds + .2) * 1000), np.random.default_rng(42)), 1000)
        example = analyzer.result()
        example.update(simulation=True, detected=False, decision="模拟示例 · 未作人体检测")
        draw_scene(painter, 2560, 1440, 1, config, True, example, "换帧 210.0 Hz  |  估计漏帧 0（示例）", preview=True,
                   panel=render_cca_panel(example, config.frequency, config.candidates))
        painter.end()
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(args.preview)):
            raise OSError(f"不能保存 {args.preview}")
        print(args.preview)
        return 0
    from .display import screen_key
    from .setup_dialog import SetupDialog
    from .stimulus import StimulusWindow
    if not args.no_dialog:
        dialog = SetupDialog(config, app.screens())
        if dialog.exec_() != QDialog.Accepted:
            return 0
        config = dialog.config
    if config.screen_index >= len(app.screens()):
        QMessageBox.warning(None, "显示器错误", "选择的显示器不存在")
        return 2
    screen = app.screens()[config.screen_index]
    if config.screen_fingerprint != screen_key(screen):
        config.calibrated = False
        config.scale_x = config.scale_y = 1.0
    config.save(args.config)
    folder = args.output / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log = SessionLog(folder, config)
    worker = WorkerController(config, folder, args.calibration_file)
    try:
        worker.start()
        window = StimulusWindow(config, screen, worker, log)
        window.showFullScreen()
        window.requestActivate()
        code = app.exec_()
        if window.failed:
            QMessageBox.warning(None, "显示时序未通过", window.result["status"])
        return code
    except (RuntimeError, OSError) as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return 2
    finally:
        worker.stop()
        log.close()


def headless(config, duration, output, calibration_file=None, calibrate=False):
    if duration <= 0:
        raise ValueError("duration 必须大于 0")
    folder = output / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log = SessionLog(folder, config)
    worker = WorkerController(config, folder, calibration_file)
    try:
        worker.start()
        worker.begin(mode="baseline" if calibrate else "test")
        print(f"HEADLESS (无视觉刺激): http://127.0.0.1:{config.port}/api/bci/data", flush=True)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            result = worker.poll()
            if result:
                log.put("result", result)
                print(json.dumps({k: v for k, v in result.items() if k not in ("candidates", "cca_rhos", "fbcca_scores", "diagnostics")}, ensure_ascii=False), flush=True)
                if result.get("action") in ("start_response", "start_test"):
                    worker.begin(mode="response" if result["action"] == "start_response" else "test")
            if not worker.process.is_alive():
                raise RuntimeError("分析进程意外退出")
            time.sleep(0.05)
        return 0
    finally:
        worker.stop()
        log.close()
