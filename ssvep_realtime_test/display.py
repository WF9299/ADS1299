"""Shared static/stimulus drawing, with units explicitly in physical pixels."""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter
from .config import square_pixels


def screen_key(screen):
    g = screen.geometry()
    return f"{screen.name()}:{g.width()}x{g.height()}:{screen.devicePixelRatio():g}"


def result_lines(result):
    if not result or not result.get("valid"):
        lines = ["CCA / FBCCA", (result or {}).get("decision", ""), (result or {}).get("status", "等待 Monitor HTTP 数据"),
                 "数据无效时不显示旧的识别结果"]
        if result and result.get("excluded"):
            lines += [f"CH{k}: {v}" for k, v in result["excluded"].items()]
        if result and result.get("calibration_instruction"):
            lines.insert(1, result["calibration_instruction"])
        return lines
    label = "CCA 估计" if result.get("analysis_profile") == "matlab" else "CCA 候选"
    lines = [result.get("decision", "示例候选结果"),
             f"{label}      {result['cca_frequency']:g} Hz    ρ = {result['cca_rho']:.4f}",
             f"FBCCA 候选  {result['fbcca_frequency']:g} Hz    score = {result['fbcca_score']:.4f}",
             "子带 ρ   " + " / ".join(f"{r:.3f}" for r in result["fbcca_rhos"]),
             f"刺激频点  ρ = {result['target_rho']:.4f}    score = {result['target_score']:.4f}",
             "使用 " + ", ".join(f"CH{x}" for x in result["used_channels"])]
    if "target_snr_db" in result:
        lines.append(f"目标局部 SNR {result['target_snr_db']:.1f} dB  |  分数差 {result['target_margin']:.4f}")
    if result.get("analysis_profile") == "matlab":
        lines.append(f"CCA 第一/第二名差 {result.get('cca_winner_margin', 0):.4f} · 四谐波 · 逐窗零相位")
    if result.get("calibration_instruction"):
        lines.insert(1, result["calibration_instruction"])
    if result.get("selection_supported") is False:
        lines.append("标定未找到稳定增强通道；沿用质量有效通道")
    if result.get("excluded_frequencies"):
        lines.append("排名屏蔽 " + ", ".join(f"{f:g} Hz" for f in result["excluded_frequencies"]))
    if result.get("target_excluded"):
        lines.insert(1, "当前刺激已屏蔽，不能识别该目标")
    if result.get("excluded"):
        lines.append("剔除 " + ", ".join(f"CH{k}({v})" for k, v in result["excluded"].items()))
    if "age_ms" in result:
        lines.append(f"数据年龄 {result['age_ms']:.0f} ms  |  计算 {result.get('analysis_ms', 0):.1f} ms  |  重建 {result.get('resets', 0)}")
    lines.append("ρ 与 score 是匹配指标，不是概率")
    return lines


def frequency_buttons(width, height, config):
    if config.analysis_profile != "matlab":
        return []
    frequencies = config.switch_frequencies
    cell = (width - 48) / len(frequencies)
    return [(QRectF(24 + i * cell, height - 50, cell - 3, 30), f) for i, f in enumerate(frequencies)]


def draw_scene(painter, width, height, dpr, config, white, result, timing, preview=False, panel=None):
    painter.fillRect(QRectF(0, 0, width, height), Qt.black)
    pixels_x, pixels_y = square_pixels(config, width * dpr, height * dpr)
    sx, sy = pixels_x / dpr, pixels_y / dpr
    color = QColor("#808080") if white is None else (Qt.white if white else Qt.black)
    painter.fillRect(QRectF((width - sx) / 2, (height - sy) / 2, sx, sy), color)
    font_size = max(12, min(21, int(height / 52)))
    font = QFont("Microsoft YaHei")
    font.setPixelSize(font_size)
    painter.setFont(font)
    painter.setPen(QColor("#d9e1e8"))
    margin, line_height = 24, font_size + 9
    left = [f"刺激 {config.frequency:g} Hz", "50 × 50 mm  |  ESC 退出",
            f"{pixels_x} × {pixels_y} px  ·  " + ("已尺校准" if config.calibrated else "估算尺寸，待尺校准"),
            timing, f"HTTP 127.0.0.1:{config.port}/api/bci/data"]
    left += [f"{config.window_seconds:g} 秒窗 · {config.bands[0][0]:g}–{config.bands[0][1]:g} Hz · {config.harmonics} 谐波",
             "B CCA柱图 · D质量/频谱 · 1–8通道",
             "←/→ 切频 · Home 回初始频率" if config.analysis_profile == "matlab" else "R 标定"]
    if config.analysis_profile == "matlab":
        left.append(f"当前CCA {config.analysis_frequencies[0]:g}–{config.analysis_frequencies[-1]:g} Hz · 0.5 Hz")
        left.append(f"刺激快捷 {config.switch_frequencies[0]:g}–{config.switch_frequencies[-1]:g} Hz · Home {config.frequency_center or config.frequency:g}")
    right = result_lines(result)
    if result and result.get("simulation"):
        right.insert(0, "模拟数据 · 非真实 EEG")
    reserve = 60 if config.analysis_profile == "matlab" else 0
    available = height - margin - reserve - ((height + sy) / 2 + 12)
    line_height = min(line_height, max(14, int(available / max(len(left), len(right)))))
    font.setPixelSize(min(font_size, max(9, line_height - 4)))
    painter.setFont(font)
    for x, lines, box_width in ((margin, left, width * 0.45 - margin), (width * 0.52, right, width * 0.48 - margin)):
        y = height - margin - len(lines) * line_height - reserve
        for line in lines:
            painter.drawText(QRectF(x, y, box_width, line_height), Qt.AlignLeft | Qt.AlignVCenter, line)
            y += line_height
    for rect, frequency in frequency_buttons(width, height, config):
        current = frequency == config.frequency
        available = 10 <= frequency < 27.25
        painter.fillRect(rect, QColor("#164c68" if current else "#121c25" if available else "#090d12"))
        painter.setPen(QColor("#82dcff" if current else "#9caebd" if available else "#394550"))
        painter.drawText(rect, Qt.AlignCenter, f"{frequency:g}")
    if preview:
        painter.setPen(QColor("#ffce6b"))
        painter.drawText(QRectF(0, 25, width, 40), Qt.AlignCenter, "静态界面预览 · 分析数值为示例")
    if panel is not None:
        panel_width = width - 2 * margin
        panel_height = min(height * .26, panel_width * panel.height() / panel.width())
        painter.drawImage(QRectF(margin, 70, panel_width, panel_height), panel)
