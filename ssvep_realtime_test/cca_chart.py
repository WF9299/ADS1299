"""Cached first-canonical-correlation bars; no score normalization or EEG edits."""
import math
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen

COLORS = {"candidate": "#55758d", "target": "#40b9d4", "winner": "#f0a34b",
          "match": "#54c68b", "excluded": "#666c76"}
PLOT = QRectF(80, 78, 1488, 156)


def cca_bars(result, target, expected_candidates=None):
    if not result or not result.get("valid"):
        return None
    candidates, rhos = result.get("candidates", []), result.get("cca_rhos", [])
    if not 2 <= len(candidates) <= 256 or len(candidates) != len(rhos):
        return None
    if expected_candidates is not None and tuple(candidates) != tuple(expected_candidates):
        return None
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in [*candidates, *rhos]):
        return None
    if any(not 0 <= v <= 1 for v in rhos):
        return None
    excluded = result.get("excluded_frequencies", [])
    winner = result.get("cca_frequency")
    bars = []
    for frequency, rho in zip(candidates, rhos):
        role = "candidate"
        if frequency in excluded:
            role = "excluded"
        elif frequency == target == winner:
            role = "match"
        elif frequency == winner:
            role = "winner"
        elif frequency == target:
            role = "target"
        bars.append({"frequency": frequency, "rho": rho, "role": role, "target": frequency == target})
    return bars


def render_cca_panel(result, target, expected_candidates=None):
    image = QImage(1600, 300, QImage.Format_RGB32)
    image.fill(QColor("#090d12"))
    p = QPainter(image)
    font = QFont("Microsoft YaHei")
    font.setPixelSize(18)
    p.setFont(font)
    p.setPen(QColor("#d3e1ed"))
    p.drawText(18, 26, "CCA 实时相关系数 ρ · 固定0–1 · 不是概率    B柱图 / D质量频谱")
    p.drawText(18, 55, "青=当前刺激   橙=最大有效候选   绿=两者一致   灰=屏蔽（仍显示真实ρ）")
    for tick in (0., .25, .5, .75, 1.):
        y = PLOT.bottom() - tick * PLOT.height()
        p.setPen(QColor("#293540"))
        p.drawLine(QPointF(PLOT.left(), y), QPointF(PLOT.right(), y))
        p.setPen(QColor("#9bafbf"))
        p.drawText(QRectF(5, y - 12, 65, 24), Qt.AlignRight | Qt.AlignVCenter, f"{tick:.2f}")
    bars = cca_bars(result, target, expected_candidates)
    if bars is None:
        p.setPen(QColor("#e9bb69"))
        message = (result or {}).get("status", "等待当前候选的连续EEG窗口")
        p.drawText(PLOT, Qt.AlignCenter, "无当前有效数据 · " + message)
        p.end()
        return image
    cell = PLOT.width() / len(bars)
    label_step = max(1, math.ceil(len(bars) / 24))
    font.setPixelSize(16)
    p.setFont(font)
    for index, bar in enumerate(bars):
        x = PLOT.left() + index * cell
        rectangle = QRectF(x + cell * .15, PLOT.bottom() - bar["rho"] * PLOT.height(), cell * .7, bar["rho"] * PLOT.height())
        p.fillRect(rectangle, QColor(COLORS[bar["role"]]))
        if bar["target"]:
            p.setPen(QPen(QColor(COLORS["target"]), 2))
            p.drawRect(rectangle)
        p.setPen(QColor("#c9d6e1"))
        significant = bar["target"] or bar["role"] in ("winner", "excluded", "match")
        if label_step == 1 or significant:
            p.drawText(QRectF(x - 8, max(58, rectangle.top() - 25), cell + 16, 22), Qt.AlignCenter, f"{bar['rho']:.3f}")
        if index % label_step == 0 or significant:
            p.drawText(QRectF(x - 6, 240, cell + 12, 24), Qt.AlignCenter, f"{bar['frequency']:g}")
        if bar["role"] == "excluded":
            p.setPen(QColor("#a2a8b2"))
            p.drawText(QRectF(x - 8, 264, cell + 16, 24), Qt.AlignCenter, "屏蔽")
    p.setPen(QColor("#d3e1ed"))
    p.drawText(QRectF(760, 4, 810, 30), Qt.AlignRight | Qt.AlignVCenter,
               f"刺激 {target:g} Hz   最大候选 {result['cca_frequency']:g} Hz   差 {result.get('cca_winner_margin', 0):.4f}")
    p.drawText(QRectF(1220, 267, 350, 25), Qt.AlignRight | Qt.AlignVCenter,
               f"当前CCA {bars[0]['frequency']:g}–{bars[-1]['frequency']:g} Hz")
    p.end()
    return image
