# -*- coding: utf-8 -*-
"""
widgets/episode_pill_button.py — пилюля номера эпизода в шапке Studio с
индикатором незавершённой монтажки.

QPushButton-подкласс. Над обычной кнопкой (QSS стили `QPushButton#pill`)
дополнительно рисует красный кружок в правом верхнем углу, если для
эпизода в `_agent_log_<ep>.json` остался `pipeline_state.status="failed"`
или `"running"`. Точка убирается когда pipeline дошёл до `completed`
или лог удалён (через «🆕 Начать заново»).

Зачем: при параллельной монтажке нескольких эпизодов юзер не видит
обрыва на эпизодах где сейчас не находится — точка моментально сигналит
о упавших на других пилюлях.

История: создано 2026-05-16 (v1.0.88, индикатор failed эпизодов).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QPainter
from PyQt6.QtWidgets import QPushButton

from views.theme import LUMZ_THEME


class EpisodePillButton(QPushButton):
    """Пилюля «01»/«02»/... с опциональным красным индикатором upper-right.

    Использование:
        btn = EpisodePillButton("01")
        btn.setObjectName("pill")
        btn.set_failed_indicator(True, stage="validator_r2",
                                 tooltip_template="Монтажка прервана на этапе {stage}")
        # Позже:
        btn.set_failed_indicator(False)  # точка пропадает + tooltip очищается
    """

    DOT_DIAMETER = 6  # px — компактно, не перекрывает текст «01»
    DOT_MARGIN_TR = 3  # px — отступ от правого/верхнего края

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._has_failed_pipeline: bool = False
        # Кэшируем base tooltip чтобы при set_failed_indicator(False)
        # восстановить «обычный» tooltip пилюли (если он когда-то был).
        self._base_tooltip: str = ""

    # ──────────────────────────────────────────────────────────────────

    def set_failed_indicator(self, has_failed: bool,
                              stage: Optional[str] = None,
                              tooltip_template: Optional[str] = None) -> None:
        """Включает/выключает красную точку и tooltip.

        Args:
            has_failed: True → точка видна, False → скрыта.
            stage: human-readable имя этапа (например «Validator R2»).
                   Используется в tooltip_template.format(stage=...).
                   Игнорируется если has_failed=False.
            tooltip_template: строка с `{stage}` plaeholder'ом — caller
                              передаёт уже локализованную через i18n.
                              Если None — tooltip не меняется.
        """
        changed = has_failed != self._has_failed_pipeline
        self._has_failed_pipeline = bool(has_failed)
        if has_failed:
            if tooltip_template and stage is not None:
                try:
                    self.setToolTip(tooltip_template.format(stage=stage))
                except Exception:
                    # На случай если template без {stage} placeholder'а —
                    # просто покажем raw template.
                    self.setToolTip(tooltip_template)
        else:
            # Возвращаем base tooltip (обычно пустой для пилюль).
            self.setToolTip(self._base_tooltip)
        if changed:
            self.update()  # триггерит paintEvent

    def set_base_tooltip(self, tooltip: str) -> None:
        """Опциональный «обычный» tooltip пилюли — восстанавливается
        когда set_failed_indicator(False)."""
        self._base_tooltip = tooltip or ""
        if not self._has_failed_pipeline:
            self.setToolTip(self._base_tooltip)

    # ──────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        # Сначала рисуем обычную кнопку (QSS-стили, текст, active state).
        super().paintEvent(event)
        if not self._has_failed_pipeline:
            return
        # Поверх — красный кружок upper-right.
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            color = QColor(LUMZ_THEME.get("accent_red", "#e4344a"))
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            d = self.DOT_DIAMETER
            m = self.DOT_MARGIN_TR
            x = self.width() - d - m
            y = m
            painter.drawEllipse(x, y, d, d)
        finally:
            painter.end()
