"""Cached diagnostic chart: rendered only on result updates, never per swap."""
import math
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen


def render_panel(result, channel, target):
    diagnostics = result.get("diagnostics")
    if not diagnostics:
        return None
    image = QImage(1600, 280, QImage.Format_RGB32)
    image.fill(QColor("#090d12"))
    p = QPainter(image)
    font = QFont("Microsoft YaHei")
    font.setPixelSize(16)
    p.setFont(font)
    p.setPen(QColor("#c7d3df"))
    p.drawText(12, 24, "原始信号质量   D 隐藏/显示   数字 1–8 切换频谱")
    p.drawText(12, 50, "通道       DC/µV        RMS/µV      %FS      削顶%     目标SNR")
    candidates = result.get("candidates", [])
    ti = candidates.index(target) if target in candidates else None
    for i, row in enumerate(diagnostics["channels"]):
        y = 77 + i * 22
        p.setPen(QColor("#ff9d83") if row["reason"] else QColor("#a5ddb6"))
        if row["finite"]:
            snr = diagnostics["snr_db"][ti][i] if ti is not None else 0
            text = (f"CH{i+1:<2}  {row['dc_uv']:>11.1f}  {row['rms_uv']:>10.2f}  "
                    f"{row['peak_fs_percent']:>7.2f}  {row['clipped_fraction'] * 100:>7.2f}  {snr:>7.1f} dB")
        else:
            text = f"CH{i+1}   非有限值"
        p.drawText(12, y, text)
    p.setPen(QColor("#c7d3df"))
    p.drawText(650, 24, f"CH{channel} 原始 PSD（去均值，仅用于谱估计）   {result.get('range_source', '')}")
    plot = QRectF(700, 50, 875, 190)
    p.setPen(QColor("#303943"))
    for f in range(0, 101, 10):
        x = plot.left() + f / 100 * plot.width()
        p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        p.drawText(QRectF(x - 20, 245, 40, 25), Qt.AlignCenter, str(f))
    frequencies = diagnostics["frequencies"]
    values = [10 * math.log10(max(row[channel - 1], 1e-24)) for row in diagnostics["psd_uv2_hz"]]
    ordered = sorted(v for f, v in zip(frequencies, values) if 6 <= f <= 95)
    low = max(-120, min(-20, ordered[len(ordered) // 10] - 5))
    high = max(low + 30, min(140, max(values[1:]) + 5))
    for tick in range(5):
        y = plot.top() + tick * plot.height() / 4
        p.setPen(QColor("#303943"))
        p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        p.setPen(QColor("#c7d3df"))
        p.drawText(650, round(y + 5), f"{high - tick * (high - low) / 4:.0f}")
    p.setClipRect(plot)
    path = QPainterPath()
    for index, (frequency, value) in enumerate(zip(frequencies, values)):
        x = plot.left() + frequency / 100 * plot.width()
        y = plot.bottom() - (value - low) / (high - low) * plot.height()
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    p.setPen(QPen(QColor("#73c4ff"), 1.6))
    p.drawPath(path)
    for f, color in ((target, "#ffd166"), (2 * target, "#a89bf1"), (50, "#c06e6e")):
        p.setPen(QPen(QColor(color), 1, Qt.DashLine))
        x = plot.left() + f / 100 * plot.width()
        p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
    p.setClipping(False)
    p.setPen(QColor("#c7d3df"))
    p.drawText(1160, 275, "Hz   |   dB(µV²/Hz)   黄线=刺激频率")
    p.end()
    return image
