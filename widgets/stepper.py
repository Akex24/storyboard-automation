# -*- coding: utf-8 -*-
"""
widgets/stepper.py — числовой степпер [− N +] для целых значений.

Замена нативного QSpinBox в зоне «Генерация сториборда (Mode C)» на странице
Настроек: кнопка «−» слева, крупное число по центру, кнопка «+» справа, всё в
одной скруглённой «ванне» в тон ProviderToggle/ModeSegment. РУЧНОЙ ВВОД
НЕВОЗМОЖЕН — значение это QLabel (не QLineEdit), меняется только кнопками.

ПОЧЕМУ ЗДЕСЬ МОЖНО QSS (в отличие от ProviderToggle/ModeSegment, ушедших в
paintEvent): кнопки «−»/«+» — обычные QPushButton, НЕ checkable. Нативный
macOS-скин перебивает QSS только у checkable-кнопок в :checked. Non-checkable
QPushButton честно красится из QSS (доказано #aspect-seg-btn в шапке). Ванна —
QFrame с QSS-фоном (как #settings-group). Стиль задаётся INLINE в самом виджете
(self.setStyleSheet) — DARK не трогаем, виджет self-contained.

Публичный API (drop-in под прежнее QSpinBox-использование):
    s = Stepper(parent)
    s.set_range(1, 10)          # границы + дизейбл кнопок на краях
    s.set_value(3)              # кламп в range, обновить число — БЕЗ сигнала
    s.valueChanged.connect(slot)  # slot(value: int) — ТОЛЬКО при клике +/−
    s.value()                   # -> int

`set_value` НЕ эмитит `valueChanged` — безопасная инициализация из QSettings.
Сигнал летит только при пользовательском клике по «−»/«+».

Чистый PyQt6 — без file-IO, subprocess, путей. Cross-platform по умолчанию.

История:
  • 2026-06-16 — создан для Коммита 5b (зона Mode C: QSpinBox → степпер [− N +]).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel,
)


# Inline-стиль виджета (палитра в тон ProviderToggle/ModeSegment). Кнопки
# non-checkable → QSS-фон берётся на macOS. Ванна — на самом Stepper через
# objectName + WA_StyledBackground (см. __init__).
_STEPPER_QSS = """
QWidget#stepper-root {
    background: #241c34;
    border: 1px solid #3a2f52;
    border-radius: 8px;
}
QPushButton#stepper-btn {
    background: transparent;
    border: none;
    color: rgba(255, 255, 255, 0.85);
    font-size: 20px; font-weight: 600;
    padding: 0;
}
QPushButton#stepper-btn:hover { color: #ffffff; }
QPushButton#stepper-btn:pressed { color: rgba(255, 255, 255, 0.60); }
QPushButton#stepper-btn:disabled { color: rgba(255, 255, 255, 0.20); }
QLabel#stepper-value {
    color: #ffffff;
    font-size: 22px; font-weight: 600;
    background: transparent;
}
"""


class Stepper(QWidget):
    """Числовой степпер [− N +]. Значение меняется только кнопками.

    Сигнал:
        valueChanged(int) — при клике пользователя по «−»/«+»; несёт новое
                            значение. Программный `set_value` сигнал не шлёт.
    """

    valueChanged = pyqtSignal(int)

    _BTN_SIZE = 40       # фикс. размер кнопок «−»/«+»
    _HEIGHT = 44         # высота ванны

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._min: int = 0
        self._max: int = 99
        self._value: int = 0

        self.setObjectName("stepper-root")
        # WA_StyledBackground — чтобы QSS-фон/рамка #stepper-root реально
        # рисовались на голом QWidget (без этого фон может не примениться).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(self._HEIGHT)
        self.setStyleSheet(_STEPPER_QSS)

        self._btn_minus = QPushButton("−", self)   # U+2212 minus sign
        self._btn_plus = QPushButton("+", self)
        for b in (self._btn_minus, self._btn_plus):
            b.setObjectName("stepper-btn")
            b.setFixedSize(self._BTN_SIZE, self._BTN_SIZE)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_minus.clicked.connect(lambda: self._bump(-1))
        self._btn_plus.clicked.connect(lambda: self._bump(+1))

        self._value_lbl = QLabel("", self)
        self._value_lbl.setObjectName("stepper-value")
        self._value_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(0)
        lay.addWidget(self._btn_minus)
        lay.addWidget(self._value_lbl, stretch=1)   # число занимает центр
        lay.addWidget(self._btn_plus)

    # ── Публичный API ───────────────────────────────────────────────────

    def set_range(self, minv: int, maxv: int) -> None:
        """Границы значения. Текущее значение клампится; кнопки дизейблятся
        на краях."""
        self._min = int(minv)
        self._max = int(maxv)
        if self._max < self._min:
            self._max = self._min
        self._value = max(self._min, min(self._max, self._value))
        self._refresh()

    def set_value(self, v: int) -> None:
        """Ставит значение (кламп в range). НЕ эмитит valueChanged —
        безопасная инициализация из QSettings."""
        self._value = max(self._min, min(self._max, int(v)))
        self._refresh()

    def value(self) -> int:
        """Текущее значение."""
        return self._value

    # ── Внутреннее ──────────────────────────────────────────────────────

    def _bump(self, delta: int) -> None:
        """Клик по «−»/«+». Меняет значение в пределах range; при реальной
        смене обновляет кнопки и эмитит valueChanged."""
        new = max(self._min, min(self._max, self._value + delta))
        if new != self._value:
            self._value = new
            self._refresh()
            self.valueChanged.emit(self._value)

    def _refresh(self) -> None:
        """Обновляет текст числа и enabled-состояние кнопок на границах."""
        self._value_lbl.setText(str(self._value))
        self._btn_minus.setEnabled(self._value > self._min)
        self._btn_plus.setEnabled(self._value < self._max)
