# -*- coding: utf-8 -*-
"""
widgets/provider_toggle.py — двухпозиционный сегмент-контрол провайдера
генерации (замена QComboBox в зоне провайдеров на странице Настроек).

iOS-подобная «ванна + пилюля»: скруглённый контейнер-капсула (тёмная ванна
с рамкой), внутри которой светлая пилюля стоит на выбранной половине и
переключается влево/вправо по клику. Две подписи-половины поверх.

ПОЧЕМУ КАСТОМНЫЙ paintEvent, а НЕ QPushButton+QSS (2026-06-16):
Нативный macOS-QStyle ("macintosh", приложение не зовёт setStyle/Fusion)
рисует свой скин QPushButton ПОВЕРХ QSS — фон `:checked`/background и рамка
игнорируются, проступает нативный bezel (на скрине: активная тёмная,
неактивная с белой рамкой, текст невидим). Прошлые попытки чинить через QSS
не брались на cocoa. Здесь весь визуал рисуется QPainter'ом напрямую —
`drawRoundedRect` + `drawText`. Это даёт ИДЕНТИЧНЫЙ результат на macOS и
Windows: нет зависимости от нативного скина, от QSS-капризов и от оконного
композита платформы. Все заливки непрозрачные; единственная альфа — цвет
текста неактивной половины, и её композитит сам QPainter детерминированно.

Публичный API (storyboard_app.py зависит ТОЛЬКО от него — drop-in):
    t = ProviderToggle(parent)
    t.set_options("narwhal", "openai")   # data-значения левой/правой половины
    t.set_labels("Nano Banana 2", "OpenAI")
    t.set_value("openai")                 # выбрать — БЕЗ сигнала
    t.valueChanged.connect(slot)          # slot(value: str) — только при клике
    t.value()                             # -> "narwhal" / "openai"

`set_value` НЕ эмитит `valueChanged` — безопасная инициализация из QSettings.
Сигнал летит только при пользовательском клике по неактивной половине.

История:
  • 2026-06-16 — создан как QPushButton+QButtonGroup сегмент (Коммит 3).
  • 2026-06-16 — переписан на кастомный paintEvent (ванна+пилюля): QSS на
    checkable-QPushButton не брался нативным скином macOS. Публичный API
    сохранён без изменений.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget, QSizePolicy


class ProviderToggle(QWidget):
    """Сегмент-контрол «ванна + пилюля» с кастомной отрисовкой.

    Сигнал:
        valueChanged(str) — при клике пользователя по НЕактивной половине;
                            несёт data_value выбранной половины. Программный
                            `set_value` сигнал не шлёт.
    """

    valueChanged = pyqtSignal(str)

    # ── Палитра (хардкод — кроссплатформенно идентична, без QSS/нативного скина) ──
    _BATH_BG = QColor("#241c34")           # тёмная ванна, в тон карточки
    _BATH_BORDER = QColor("#3a2f52")       # рамка ванны
    _PILL_BG = QColor("#ececf0")           # активная пилюля — мягкий сероватый белый
    _TEXT_ACTIVE = QColor("#1a1424")       # текст на пилюле — тёмный
    _TEXT_INACTIVE = QColor(255, 255, 255, 179)  # 0.70 * 255 — читаемый светло-серый

    _HEIGHT = 40        # фикс. высота контрола
    _BATH_RADIUS = 8    # скругление ванны
    _PILL_PAD = 3       # отступ пилюли от внутренних краёв ванны
    _FONT_PX = 13       # размер текста (как у прежних подписей)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._values: List[str] = ["", ""]   # data_value левой / правой
        self._labels: List[str] = ["", ""]   # видимый текст левой / правой
        self._index: int = 0                  # 0 — активна левая, 1 — правая
        self.setFixedHeight(self._HEIGHT)
        # По горизонтали растягиваемся на ширину карточки (как делал combo).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ── Публичный API ───────────────────────────────────────────────────

    def set_options(self, value_left: str, value_right: str) -> None:
        """Задаёт data_value половин (логические значения, не видимый текст)."""
        self._values = [value_left, value_right]

    def set_labels(self, label_left: str, label_right: str) -> None:
        """Задаёт видимый текст половин. Зовётся из retranslate — data_value
        не трогается. Перерисовывает контрол."""
        self._labels = [label_left, label_right]
        self.update()

    def set_value(self, value: str) -> None:
        """Выбирает половину с data_value == value. НЕ эмитит valueChanged
        (безопасная инициализация из QSettings). Неизвестное значение →
        левая половина (дефолт-провайдер)."""
        idx = 0
        for i, v in enumerate(self._values):
            if v == value:
                idx = i
                break
        self._index = idx
        self.update()

    def value(self) -> str:
        """data_value активной половины."""
        if 0 <= self._index < len(self._values):
            return self._values[self._index]
        return ""

    # ── Размер ──────────────────────────────────────────────────────────

    def sizeHint(self) -> QSize:
        """Минимально-разумная ширина по сумме подписей (растягивается шире
        layout'ом). Высота фиксирована."""
        fm = self.fontMetrics()
        w = 0
        for lbl in self._labels:
            w = max(w, fm.horizontalAdvance(lbl or ""))
        # две половины + горизонтальные отступы внутри половины (~22px) + паддинги
        total = (w + 44) * 2 + self._PILL_PAD * 2
        return QSize(max(total, 220), self._HEIGHT)

    # ── Взаимодействие ──────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        """Клик ЛКМ по половине. Эмитит valueChanged ТОЛЬКО при смене
        активной половины (как currentIndexChanged у combo)."""
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        new_index = 0 if event.position().x() < self.width() / 2.0 else 1
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
            p.setBrush(self._BATH_BG)
            p.setPen(QPen(self._BATH_BORDER, 1))
            p.drawRoundedRect(bath, self._BATH_RADIUS, self._BATH_RADIUS)

            # Геометрия половин (внутри ванны, с отступом _PILL_PAD).
            pad = self._PILL_PAD
            inner = bath.adjusted(pad, pad, -pad, -pad)
            half_w = inner.width() / 2.0
            left_half = QRectF(inner.left(), inner.top(), half_w, inner.height())
            right_half = QRectF(inner.left() + half_w, inner.top(),
                                half_w, inner.height())

            # Пилюля на активной половине.
            pill = left_half if self._index == 0 else right_half
            p.setBrush(self._PILL_BG)
            p.setPen(Qt.PenStyle.NoPen)
            pill_radius = max(self._BATH_RADIUS - pad, 3)
            p.drawRoundedRect(pill, pill_radius, pill_radius)

            # Текст половин: активная — тёмная жирная, неактивная — светло-серая.
            font = QFont(self.font())
            font.setPixelSize(self._FONT_PX)

            for i, (rect, label) in enumerate(
                    ((left_half, self._labels[0]), (right_half, self._labels[1]))):
                active = (i == self._index)
                font.setBold(active)
                p.setFont(font)
                p.setPen(self._TEXT_ACTIVE if active else self._TEXT_INACTIVE)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label or "")
        finally:
            p.end()
