# -*- coding: utf-8 -*-
"""
widgets/provider_toggle.py — двухпозиционный сегментированный переключатель
провайдера генерации (замена QComboBox в зоне провайдеров на странице
Настроек).

QWidget с двумя checkable-кнопками `QPushButton` в одном эксклюзивном
`QButtonGroup`. Визуально — сегмент-контрол: активная вкладка с заливкой,
неактивная серым текстом без фона (QSS-селекторы `#provider-toggle-btn` в
DARK, см. storyboard_app.py). data-значение каждой кнопки хранится в
property `data_value`; ярлык (видимый текст) задаётся отдельно через
`set_labels` — так retranslate меняет подписи, не трогая data.

API:
    t = ProviderToggle(parent)
    t.set_options("narwhal", "openai")              # data-значения кнопок
    t.set_labels("Nano Banana 2", "OpenAI")          # видимый текст (i18n)
    t.set_value("openai")                            # отметить — БЕЗ сигнала
    t.valueChanged.connect(slot)                     # slot(value: str)
    t.value()                                        # -> "narwhal" / "openai"

`set_value` намеренно НЕ эмитит `valueChanged` (через `blockSignals`) —
иначе инициализация страницы Настроек значением из QSettings случайно
переписала бы QSettings обратно (и затёрла бы реальный выбор при гонке
ретрансляции). Сигнал летит только от пользовательского клика.

Чистый PyQt6 — без file-IO, subprocess, путей. Cross-platform по умолчанию.

История:
  • 2026-06-16 — создан для Коммита 3 (зона провайдеров → сегмент-контрол
    по макету: один общий заголовок + две карточки рядом).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QButtonGroup,
)


class ProviderToggle(QWidget):
    """Сегментированный двухпозиционный переключатель (data_value на кнопку).

    Сигнал:
        valueChanged(str) — эмитится ТОЛЬКО при клике пользователя; несёт
                            data_value выбранной кнопки. Программный
                            `set_value` сигнал не шлёт.
    """

    valueChanged = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._btn_left = QPushButton(self)
        self._btn_right = QPushButton(self)
        for btn in (self._btn_left, self._btn_right):
            btn.setObjectName("provider-toggle-btn")
            btn.setCheckable(True)
            # cursor pointer ставит сам QSS-движок Qt? нет — оставляем
            # дефолт, как у прочих кнопок Settings (единообразие).

        # Эксклюзивная группа: всегда отмечена ровно одна кнопка.
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.addButton(self._btn_left, 0)
        self._group.addButton(self._btn_right, 1)
        self._group.buttonClicked.connect(self._on_button_clicked)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._btn_left, stretch=1)
        lay.addWidget(self._btn_right, stretch=1)

    # ──────────────────────────────────────────────────────────────────

    def set_options(self, value_left: str, value_right: str) -> None:
        """Задаёт data_value кнопок (логические значения, не видимый текст)."""
        self._btn_left.setProperty("data_value", value_left)
        self._btn_right.setProperty("data_value", value_right)

    def set_labels(self, label_left: str, label_right: str) -> None:
        """Задаёт видимый текст кнопок. Зовётся из retranslate при смене
        языка — data_value при этом не трогается."""
        self._btn_left.setText(label_left)
        self._btn_right.setText(label_right)

    def set_value(self, value: str) -> None:
        """Отмечает кнопку с data_value == value. НЕ эмитит valueChanged
        (blockSignals на время установки) — для безопасной инициализации
        из QSettings. Неизвестное значение → отмечается левая кнопка
        (дефолт-провайдер)."""
        target = self._btn_left
        for btn in (self._btn_left, self._btn_right):
            if btn.property("data_value") == value:
                target = btn
                break
        # blockSignals на группе И на кнопке: setChecked может породить
        # toggled, а group.buttonClicked мы и так не дёргаем программно,
        # но перестраховываемся симметрично.
        self._group.blockSignals(True)
        target.blockSignals(True)
        try:
            target.setChecked(True)
        finally:
            target.blockSignals(False)
            self._group.blockSignals(False)

    def value(self) -> str:
        """data_value отмеченной кнопки. Если по какой-то причине ничего
        не отмечено — возвращает data_value левой кнопки (дефолт)."""
        checked = self._group.checkedButton()
        if checked is None:
            checked = self._btn_left
        val = checked.property("data_value")
        return val if isinstance(val, str) else ""

    # ──────────────────────────────────────────────────────────────────

    def _on_button_clicked(self, _btn: QPushButton) -> None:
        """Слот group.buttonClicked — только пользовательский клик. Эмитит
        valueChanged с актуальным data_value."""
        self.valueChanged.emit(self.value())
