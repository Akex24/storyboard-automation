# -*- coding: utf-8 -*-
"""
widgets/mode_segment.py — N-позиционный сегмент-контрол (ванна + пилюля) для
выбора режима монтажной карты на странице Настроек.

Старший брат `widgets/provider_toggle.py`: та же кастомная отрисовка
QPainter'ом (одна тёмная скруглённая «ванна», внутри — светлая «пилюля» на
активном сегменте), только сегментов N (не 2). Одна цельная капсула с N
сегментами вплотную, БЕЗ зазоров — НЕ отдельные кнопки.

ПОЧЕМУ КАСТОМНЫЙ paintEvent, а НЕ QPushButton+QSS (2026-06-16):
Нативный macOS-QStyle ("macintosh", приложение не зовёт setStyle/Fusion)
рисует свой скин QPushButton ПОВЕРХ QSS — и фон `:checked`, и property
`[active]` подсветка не берутся (активный сегмент выходил тёмным с белой
нативной рамкой — инверсия). Та же беда, что увела ProviderToggle на
paintEvent. Здесь весь визуал рисуется QPainter'ом напрямую
(`drawRoundedRect` + `drawText`) → ИДЕНТИЧНО на macOS и Windows, без
зависимости от нативного скина / QSS-капризов / оконного композита.
Все заливки непрозрачные; единственная альфа — цвет текста неактивных
сегментов, и её композитит сам QPainter детерминированно.

Публичный API (storyboard_app.py зависит ТОЛЬКО от него):
    s = ModeSegment(parent)
    s.set_options(['a', 'b', 'c', 'd'])              # data-значения сегментов
    s.set_labels(['Mode A', 'Mode B', 'Mode C', 'Mode D'])
    s.set_value('c')                                  # выбрать — БЕЗ сигнала
    s.valueChanged.connect(slot)                      # slot(value: str) — при клике
    s.value()                                         # -> 'a'/'b'/'c'/'d'

`set_value` НЕ эмитит `valueChanged` — безопасная инициализация из QSettings
и откат подсветки при ESC в рестарт-диалоге. Сигнал летит только при клике
по НЕактивному сегменту.

Чистый PyQt6 — без file-IO, subprocess, путей. Cross-platform по умолчанию.

История:
  • 2026-06-16 — создан для Коммита 4 (зона «Режим монтажной карты»:
    QComboBox → сегмент-контрол). Палитра/геометрия 1:1 с ProviderToggle.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PyQt6.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget, QSizePolicy

from views.theme import theme_qcolor


class ModeSegment(QWidget):
    """N-сегментный контрол «ванна + пилюля» с кастомной отрисовкой.

    Сигнал:
        valueChanged(str) — при клике пользователя по НЕактивному сегменту;
                            несёт data_value выбранного сегмента. Программный
                            `set_value` сигнал не шлёт.
    """

    valueChanged = pyqtSignal(str)

    # ── Палитра (1:1 с ProviderToggle — единый стиль страницы) ──
    # 2026-06-26 / Codex:
    # Храним literal'ы, а QColor берём в paintEvent через theme_qcolor().
    # Иначе кастомный QPainter-контрол не видел тему, загруженную из QSettings
    # после импорта модуля.
    _BATH_BG = "#151718"     # graphite-ванна
    _BATH_BORDER = "rgba(255,255,255,0.10)"
    _PILL_BG = "#303335"     # активная пилюля без светлого пятна
    _TEXT_ACTIVE = "#d8d8d8" # светлый текст на активной пилюле
    _TEXT_INACTIVE = "rgba(255,255,255,0.70)"

    _HEIGHT = 40        # фикс. высота контрола (как ProviderToggle)
    _BATH_RADIUS = 8    # скругление ванны
    _PILL_PAD = 3       # отступ пилюли от внутренних краёв ванны
    _FONT_PX = 13       # размер текста

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._values: List[str] = []   # data_value сегментов
        self._labels: List[str] = []   # видимый текст сегментов
        self._index: int = 0           # индекс активного сегмента
        self.setFixedHeight(self._HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ── Публичный API ───────────────────────────────────────────────────

    def set_options(self, values: Sequence[str]) -> None:
        """Задаёт data_value сегментов (логические значения)."""
        self._values = list(values)
        if self._index >= len(self._values):
            self._index = 0
        self.update()

    def set_labels(self, labels: Sequence[str]) -> None:
        """Задаёт видимый текст сегментов. Зовётся из retranslate — data
        не трогается. Перерисовывает контрол."""
        self._labels = list(labels)
        self.update()

    def set_value(self, value: str) -> None:
        """Выбирает сегмент с data_value == value. НЕ эмитит valueChanged
        (безопасная инициализация из QSettings + откат подсветки при ESC).
        Неизвестное значение → первый сегмент."""
        idx = 0
        for i, v in enumerate(self._values):
            if v == value:
                idx = i
                break
        self._index = idx
        self.update()

    def value(self) -> str:
        """data_value активного сегмента."""
        if 0 <= self._index < len(self._values):
            return self._values[self._index]
        return ""

    # ── Размер ──────────────────────────────────────────────────────────

    def sizeHint(self) -> QSize:
        """Минимально-разумная ширина по самой широкой подписи × N
        (растягивается шире layout'ом). Высота фиксирована."""
        fm = self.fontMetrics()
        w = 0
        for lbl in self._labels:
            w = max(w, fm.horizontalAdvance(lbl or ""))
        n = max(len(self._values), 1)
        total = (w + 36) * n + self._PILL_PAD * 2
        return QSize(max(total, 240), self._HEIGHT)

    # ── Взаимодействие ──────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        """Клик ЛКМ по сегменту. Эмитит valueChanged ТОЛЬКО при смене
        активного сегмента."""
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        n = len(self._values)
        if n == 0:
            return
        seg_w = self.width() / float(n)
        new_index = int(event.position().x() / seg_w) if seg_w > 0 else 0
        if new_index < 0:
            new_index = 0
        elif new_index > n - 1:
            new_index = n - 1
        if new_index != self._index:
            self._index = new_index
            self.update()
            self.valueChanged.emit(self.value())

    # ── Отрисовка ───────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Ванна (на полпикселя внутрь — чтобы рамка 1px не обрезалась).
            bath = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setBrush(theme_qcolor(self._BATH_BG))
            p.setPen(QPen(theme_qcolor(self._BATH_BORDER), 1))
            p.drawRoundedRect(bath, self._BATH_RADIUS, self._BATH_RADIUS)

            n = len(self._values)
            if n == 0:
                return

            # Геометрия сегментов (внутри ванны, с отступом _PILL_PAD).
            pad = self._PILL_PAD
            inner = bath.adjusted(pad, pad, -pad, -pad)
            seg_w = inner.width() / float(n)

            # Пилюля на активном сегменте.
            if 0 <= self._index < n:
                pill = QRectF(inner.left() + seg_w * self._index, inner.top(),
                              seg_w, inner.height())
                p.setBrush(theme_qcolor(self._PILL_BG))
                p.setPen(Qt.PenStyle.NoPen)
                pill_radius = max(self._BATH_RADIUS - pad, 3)
                p.drawRoundedRect(pill, pill_radius, pill_radius)

            # Текст сегментов: активный — тёмный жирный, неактивные — светло-серые.
            font = QFont(self.font())
            font.setPixelSize(self._FONT_PX)
            for i in range(n):
                rect = QRectF(inner.left() + seg_w * i, inner.top(),
                              seg_w, inner.height())
                active = (i == self._index)
                font.setBold(active)
                p.setFont(font)
                p.setPen(theme_qcolor(
                    self._TEXT_ACTIVE if active else self._TEXT_INACTIVE
                ))
                label = self._labels[i] if i < len(self._labels) else ""
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label or "")
        finally:
            p.end()
