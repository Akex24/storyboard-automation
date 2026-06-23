"""Shimmer-визуал loading-плитки — общий paint-хелпер и self-contained overlay.

Источник правды для двух мест:
  • generator/result_cell.py / ShimmerCell — loading-ветка paintEvent зовёт
    paint_shimmer_loading (рефактор без смены визуала; общий таймер страницы
    крутит self._angle, ячейка только репейнтит).
  • widgets/editor_widgets.py / ShotCard.gen_overlay — заменил QProgressBar на
    ShimmerOverlay (self-driven: свой ~30мс таймер крутит угол + repaint).

Математика 1:1 как в прошлом ShimmerCell.paintEvent loading-ветке:
  слой 1 — _BASE_COLOR (тёмная подложка),
  слой 2 — sin-pulse между _BASE_DARK_*/_BASE_LIGHT_* (дышащая база),
  слой 3 — _DEPTH_TOP→_BOTTOM вертикальный градиент (объём),
  слой 4 — _WARM_ACCENT радиалка из (0,0) (тёплый нюанс в углу).
Скругление углов — радиус 8px (как у ShimmerCell).

Cross-platform: чистый Qt + math. Без subprocess/Path/IO. Win-safe.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt, QRectF, QTimer
from PyQt6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                          QRadialGradient)
from PyQt6.QtWidgets import QWidget


# ── Цветовые константы (копия значений из ShimmerCell, единый источник тут) ──
# pulse колеблется между _BASE_DARK и _BASE_LIGHT по sin(angle); амплитуда ~10%.
_BASE_DARK_R, _BASE_DARK_G, _BASE_DARK_B = 20, 15, 30      # ≈ #14 0F 1E
_BASE_LIGHT_R, _BASE_LIGHT_G, _BASE_LIGHT_B = 32, 24, 46   # ≈ #20 18 2E
# Статичные слои объёма — НЕ зависят от фазы.
_DEPTH_TOP = QColor(255, 255, 255, 14)     # лёгкая подсветка сверху
_DEPTH_BOTTOM = QColor(0, 0, 0, 18)        # лёгкая тень снизу
_WARM_ACCENT_INNER = QColor(212, 162, 86, 26)   # янтарь, центр радиалки
_WARM_ACCENT_OUTER = QColor(212, 162, 86, 0)    # затухание к 0
# Тёмная подложка ДО pulse (без неё на первом кадре виден фон родителя).
_BASE_COLOR = QColor(22, 16, 32)           # #161020

# Радиус скругления rounded-rect (как у ShimmerCell.paintEvent).
_SHIMMER_RADIUS = 8


def paint_shimmer_loading(painter: QPainter, rect: QRectF,
                           angle: float) -> None:
    """Нарисовать loading-визуал в `rect` под заданным углом фазы.

    Порядок слоёв строго: _BASE_COLOR → pulse(angle) → depth → warm corner.
    Антиалиасинг включает caller (мы не трогаем RenderHints чужого painter).
    Рамку и model_badge не рисуем — это специфика конкретного caller'а
    (ShimmerCell сам добавляет _BORDER_COLOR drawPath после хелпера).

    `rect` может стартовать не из (0,0) — путь и градиенты считаются относительно
    самого rect, поэтому overlay в любом виджете рисуется корректно.
    """
    if not isinstance(rect, QRectF):
        rect = QRectF(rect)
    w = rect.width()
    h = rect.height()
    if w <= 0 or h <= 0:
        return

    path = QPainterPath()
    path.addRoundedRect(rect, _SHIMMER_RADIUS, _SHIMMER_RADIUS)

    # (1) Тёмная подложка
    painter.fillPath(path, _BASE_COLOR)

    # (2) Дышащая база — sin-пульсация яркости (бесшовно).
    pulse = 0.5 + 0.5 * math.sin(angle)
    r = int(_BASE_DARK_R + (_BASE_LIGHT_R - _BASE_DARK_R) * pulse)
    g = int(_BASE_DARK_G + (_BASE_LIGHT_G - _BASE_DARK_G) * pulse)
    b = int(_BASE_DARK_B + (_BASE_LIGHT_B - _BASE_DARK_B) * pulse)
    painter.fillPath(path, QColor(r, g, b))

    # (3) Вертикальный depth-градиент (статика, объём).
    gv = QLinearGradient(rect.x(), rect.y(), rect.x(), rect.y() + h)
    gv.setColorAt(0.0, _DEPTH_TOP)
    gv.setColorAt(1.0, _DEPTH_BOTTOM)
    painter.fillPath(path, gv)

    # (4) Тёплая угловая радиалка (статика). Центр — в верх-лев углу rect,
    # радиус = max(w, h) * 0.7 (как в эталоне).
    corner = QRadialGradient(rect.x(), rect.y(), max(w, h) * 0.7)
    corner.setColorAt(0.0, _WARM_ACCENT_INNER)
    corner.setColorAt(1.0, _WARM_ACCENT_OUTER)
    painter.fillPath(path, corner)


class ShimmerOverlay(QWidget):
    """Self-contained overlay для loading-визуала (генератор-стиль).

    Свой QTimer ~30мс крутит фазу и зовёт update(). Внешний код звать таймер
    не должен — start()/stop() полностью управляют жизненным циклом анимации.

    Дочерние виджеты (например, секунды-счётчик) могут лежать поверх overlay —
    paintEvent рисует только фон, на содержимое не влияет.
    """

    # ~30 fps; шаг угла подобран так, чтобы один цикл sin занимал ~2.5с
    # (как в генераторе на 4 плитках: 15fps × 0.167 ≈ 2.5с/цикл).
    _TIMER_MS = 33
    _STEP_RAD = 0.084   # 2π / (30fps × 2.5s)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self._TIMER_MS)
        self._timer.timeout.connect(self._tick)
        # Не перехватываем клики — overlay декоративный; кнопки/лейблы поверх
        # должны получать события без проблем.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def start(self) -> None:
        """Показать overlay и запустить анимацию.

        НЕ зовём raise_() — наоборот, гарантируем что shimmer остаётся ПОД
        sibling-виджетами родителя (gen_overlay'я лейблами секунд/«Добор»).
        Иначе при повторном start() shimmer перекрыл бы счётчик секунд.
        """
        if not self._timer.isActive():
            self._timer.start()
        self.show()
        self.lower()
        self.update()

    def stop(self) -> None:
        """Спрятать overlay и остановить таймер (не крутится вхолостую)."""
        if self._timer.isActive():
            self._timer.stop()
        self.hide()

    def hideEvent(self, ev) -> None:    # noqa: D401
        # Защита: если overlay скрыли извне (родитель спрятал) — таймер не должен
        # продолжать тикать и жечь CPU. start() при повторном показе оживит.
        if self._timer.isActive():
            self._timer.stop()
        super().hideEvent(ev)

    def _tick(self) -> None:
        self._angle += self._STEP_RAD
        two_pi = 2.0 * math.pi
        if self._angle >= two_pi:
            self._angle -= two_pi
        self.update()

    def paintEvent(self, ev) -> None:    # noqa: D401
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_shimmer_loading(p, QRectF(0, 0, self.width(), self.height()),
                               self._angle)
        p.end()
