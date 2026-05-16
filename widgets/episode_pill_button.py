# -*- coding: utf-8 -*-
"""
widgets/episode_pill_button.py — пилюля номера эпизода в шапке Studio с
индикатором состояния монтажки.

QPushButton-подкласс. Над обычной кнопкой (QSS стили `QPushButton#pill`)
дополнительно рисует цветной кружок в правом верхнем углу. Состояний три
(см. `set_state`):
  • "failed"            — КРАСНАЯ мигающая (500мс цикл). Pipeline упал
                          или running без живого треда (force-quit).
  • "completed_unseen"  — ЗЕЛЁНАЯ статичная. Карта готова, юзер ещё не
                          открывал её через CTA «📂 Открыть».
  • "running_alive"     — НИЧЕГО. Pipeline нормально работает, тред жив.
  • "none"              — НИЧЕГО. Нет монтажки или уже просмотрена.

История:
  • 2026-05-16 (v1.0.88, Stage 8) — изначально создан только для красной
    failed-точки через `set_failed_indicator(bool)`.
  • 2026-05-16 (v1.0.88, Stage 10) — переработан на 3-state API
    `set_state(state, stage, tooltip_template)`. Добавлен blink-таймер
    для failed, зелёный цвет для completed_unseen.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QPainter
from PyQt6.QtWidgets import QPushButton

from views.theme import LUMZ_THEME


class EpisodePillButton(QPushButton):
    """Пилюля «01»/«02»/... с опциональным цветным индикатором upper-right.

    API:
        btn = EpisodePillButton("01")
        btn.setObjectName("pill")
        btn.set_state("failed", stage="validator_r2",
                      tooltip_template="Монтажка прервана на этапе {stage}")
        # → красная мигающая точка + tooltip.

        btn.set_state("completed_unseen",
                      tooltip_template="Монтажка готова — кликни чтобы открыть")
        # → зелёная статичная точка + tooltip (без {stage} placeholder'а).

        btn.set_state("running_alive")  # или "none"
        # → точка скрыта, blink-таймер остановлен.
    """

    DOT_DIAMETER = 6  # px — компактно, не перекрывает текст «01»
    DOT_MARGIN_TR = 3  # px — отступ от правого/верхнего края
    BLINK_INTERVAL_MS = 500  # 1Hz blink — attention pattern для failed

    # Допустимые состояния. "running_alive" эквивалентен "none" по
    # отрисовке — оба не показывают точку. Разделены для семантики
    # caller'а (storyboard_app._episode_pipeline_state).
    STATES = ("none", "running_alive", "failed", "completed_unseen")

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._state: str = "none"
        self._indicator_color: Optional[QColor] = None
        # Для failed состояния: True → точка нарисована, False → скрыта.
        # Toggle'ится `_on_blink_tick` каждые 500мс. Для completed_unseen
        # всегда True (статичная точка).
        self._blink_visible: bool = True
        # Кэшируем base tooltip чтобы при set_state("none") восстановить
        # «обычный» tooltip пилюли (если он когда-то был).
        self._base_tooltip: str = ""
        # QTimer создаётся лениво (in __init__), стартует только при
        # state="failed". Для пилюль без failed-pipeline тики не идут.
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(self.BLINK_INTERVAL_MS)
        self._blink_timer.timeout.connect(self._on_blink_tick)

    # ──────────────────────────────────────────────────────────────────

    def set_state(self, state: str,
                   stage: Optional[str] = None,
                   tooltip_template: Optional[str] = None) -> None:
        """Главный API для переключения визуала.

        Args:
            state: "failed" / "completed_unseen" / "running_alive" / "none".
                   Невалидное значение → fallback на "none".
            stage: human-readable имя этапа (например «Validator R2»).
                   Используется в tooltip_template.format(stage=...).
                   Игнорируется для state не "failed".
            tooltip_template: уже локализованная строка (caller подаёт через
                              tr(...)). Если None — tooltip не меняется.
                              Может содержать `{stage}` placeholder.
        """
        if state not in self.STATES:
            state = "none"
        prev_state = self._state
        self._state = state

        # Цвет + blink-таймер per state.
        if state == "failed":
            self._indicator_color = QColor(
                LUMZ_THEME.get("accent_red", "#e4344a"))
            # При переходе → ON: запускаем blink с visible=True (точка
            # сразу видна, не ждём первого тика). Если уже было failed —
            # не перезапускаем (продолжает в текущей фазе).
            if prev_state != "failed":
                self._blink_visible = True
                self._blink_timer.start(self.BLINK_INTERVAL_MS)
        elif state == "completed_unseen":
            self._indicator_color = QColor(
                LUMZ_THEME.get("accent_green", "#10B981"))
            # Статичная точка — таймер остановлен, всегда видна.
            self._blink_timer.stop()
            self._blink_visible = True
        else:
            # running_alive / none — точки нет, таймер стоп.
            self._indicator_color = None
            self._blink_timer.stop()
            self._blink_visible = True  # ready-state для следующего цикла

        # Tooltip — установить из template если есть, иначе восстановить base.
        if state in ("failed", "completed_unseen") and tooltip_template:
            try:
                # Placeholder {stage} опционален — для completed_unseen
                # template без stage, .format() с extra kwargs не упадёт.
                self.setToolTip(tooltip_template.format(
                    stage=stage if stage is not None else ""))
            except Exception:
                # На случай неправильного template (например {foo}
                # вместо {stage}) — просто покажем raw.
                self.setToolTip(tooltip_template)
        else:
            self.setToolTip(self._base_tooltip)

        # Запросить перерисовку (даже если state не изменился —
        # tooltip/цвет могли).
        self.update()

    def set_base_tooltip(self, tooltip: str) -> None:
        """Опциональный «обычный» tooltip пилюли — восстанавливается
        когда set_state("none") / set_state("running_alive")."""
        self._base_tooltip = tooltip or ""
        if self._state in ("none", "running_alive"):
            self.setToolTip(self._base_tooltip)

    # ──────────────────────────────────────────────────────────────────

    def _on_blink_tick(self) -> None:
        """Тик blink-таймера — toggle видимости и перерисовка. Зовётся
        только когда state="failed" (для других state таймер остановлен)."""
        if self._state != "failed":
            # Safety: на случай race между set_state и pending timeout —
            # если состояние сменилось между остановкой и доставкой тика,
            # просто игнорируем тик.
            return
        self._blink_visible = not self._blink_visible
        self.update()

    def paintEvent(self, event) -> None:
        # Сначала рисуем обычную кнопку (QSS-стили, текст, active state).
        super().paintEvent(event)
        # Точка рисуется только для failed/completed_unseen и только
        # когда _blink_visible (для completed_unseen всегда True).
        if self._state not in ("failed", "completed_unseen"):
            return
        if not self._blink_visible:
            return
        if self._indicator_color is None:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(QBrush(self._indicator_color))
            painter.setPen(Qt.PenStyle.NoPen)
            d = self.DOT_DIAMETER
            m = self.DOT_MARGIN_TR
            x = self.width() - d - m
            y = m
            painter.drawEllipse(x, y, d, d)
        finally:
            painter.end()
