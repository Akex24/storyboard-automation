# -*- coding: utf-8 -*-
"""
widgets/montage_cta.py — CTA-карточка «Все рефы готовы — сделать
сториборды» в чате эпизода. Появляется когда юзер залинковал все
маркеры рефов из сценария.

Сигналы:
    start_requested — клик «🎬 Сделать сториборды» (запуск оркестратора)

Состояния:
    idle      — обычная карточка с кнопкой
    running   — статус-бегунок «🤔 Сценарист пишет…», кнопка скрыта
    failed    — красная плашка «не получилось», кнопка «Попробовать ещё»

История: создано 2026-05-06 (Multi-agent монтажная карта).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy
)

from i18n import tr


KIND_IDLE = 'idle'
KIND_RUNNING = 'running'
KIND_FAILED = 'failed'
# v1.0.82: новое состояние — на эпизоде есть готовая монтажная карта на диске.
# CTA показывает кнопку «📂 Открыть монтажную карту» вместо «Сделать».
KIND_OPEN_MAP = 'open_map'
# v1.0.87 (этап 7Б resume-фичи): незавершённая монтажка — pipeline
# упал на этапе X, есть _agent_log с pipeline_state.status=failed/running.
# CTA показывает 2 кнопки: «🔄 Продолжить» и «🆕 Начать заново».
KIND_RESUMABLE = 'resumable'


class MontageCTA(QFrame):
    """Карточка-CTA для запуска multi-agent монтажа."""

    start_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    cancel_requested = pyqtSignal()  # 2026-05-06: «✗ Прервать» в running-state
    open_map_requested = pyqtSignal()  # v1.0.82: «📂 Открыть монтажную карту»
    # v1.0.87 (этап 7Б): сигналы для KIND_RESUMABLE.
    resume_requested = pyqtSignal()       # «🔄 Продолжить»
    start_fresh_requested = pyqtSignal()  # «🆕 Начать заново»

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("montage-cta")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        self._kind: str = KIND_IDLE
        self._status_text: str = ""
        self._dot_step = 0
        # v1.0.87 (этап 7Б): для KIND_RESUMABLE — какой этап последний
        # успешный и какой будет следующим при resume. Используется в
        # _render для подстановки человечного имени в subtitle/кнопку.
        self._resume_last_stage: Optional[str] = None
        self._resume_next_stage: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        self.title_lbl = QLabel(tr('montage_cta_title_idle'))
        self.title_lbl.setObjectName("montage-cta-title")
        self.title_lbl.setWordWrap(True)
        outer.addWidget(self.title_lbl)

        self.subtitle_lbl = QLabel(tr('montage_cta_subtitle_idle'))
        self.subtitle_lbl.setObjectName("montage-cta-subtitle")
        self.subtitle_lbl.setWordWrap(True)
        outer.addWidget(self.subtitle_lbl)

        # 2026-05-06: Предупреждение про OS-попапы (видно в idle/running).
        # macOS/Windows во время работы агентов могут показывать запросы
        # на разрешения (TCC / UAC / SmartScreen / Network). Если юзер
        # нажмёт «Отмена» — субпроцесс рухнет. Текст напоминает что нужно
        # ВСЕГДА разрешать. Скрывается в failed-состоянии.
        self.warning_lbl = QLabel(tr('montage_cta_warning_os_popups'))
        self.warning_lbl.setObjectName("montage-cta-warning")
        self.warning_lbl.setWordWrap(True)
        outer.addWidget(self.warning_lbl)

        # Прогресс-строка (только в running состоянии)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("montage-cta-status")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.hide()
        outer.addWidget(self.status_lbl)

        # Ряд кнопок
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)

        self.start_btn = QPushButton(tr('montage_cta_button_start'))
        self.start_btn.setObjectName("montage-cta-btn-primary")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_requested.emit)
        btn_row.addWidget(self.start_btn)

        self.retry_btn = QPushButton(tr('montage_cta_button_retry'))
        self.retry_btn.setObjectName("montage-cta-btn-primary")
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.clicked.connect(self.retry_requested.emit)
        self.retry_btn.hide()
        btn_row.addWidget(self.retry_btn)

        # 2026-05-06: кнопка отмены в running-state. Если AI завис на
        # 5+ минут — юзер кликает, активные claude CLI убиваются,
        # CTA возвращается в idle и можно запустить заново.
        self.cancel_btn = QPushButton(tr('montage_cta_button_cancel'))
        self.cancel_btn.setObjectName("montage-cta-btn-cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        self.cancel_btn.hide()
        btn_row.addWidget(self.cancel_btn)

        # v1.0.82: «📂 Открыть монтажную карту»
        self.open_map_btn = QPushButton(tr('montage_cta_button_open_map'))
        self.open_map_btn.setObjectName("montage-cta-btn-primary")
        self.open_map_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_map_btn.clicked.connect(self.open_map_requested.emit)
        self.open_map_btn.hide()
        btn_row.addWidget(self.open_map_btn)

        # v1.0.87 (этап 7Б): «🔄 Продолжить» — resume с упавшего этапа.
        self.resume_btn = QPushButton(tr('montage_cta_button_resume'))
        self.resume_btn.setObjectName("montage-cta-btn-primary")
        self.resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resume_btn.clicked.connect(self.resume_requested.emit)
        self.resume_btn.hide()
        btn_row.addWidget(self.resume_btn)

        # v1.0.87 (этап 7Б): «🆕 Начать заново» — стереть лог и стартовать.
        self.start_fresh_btn = QPushButton(
            tr('montage_cta_button_start_fresh'))
        self.start_fresh_btn.setObjectName("montage-cta-btn-cancel")
        self.start_fresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_fresh_btn.clicked.connect(self.start_fresh_requested.emit)
        self.start_fresh_btn.hide()
        btn_row.addWidget(self.start_fresh_btn)

        outer.addLayout(btn_row)

        # Анимация "точек" в running
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(500)
        self._dot_timer.timeout.connect(self._tick_dots)

        self._apply_style()

    # ── публичные методы переключения состояний ──

    def show_idle(self):
        self._kind = KIND_IDLE
        self._render()
        self.show()  # 2026-05-07: гарантируем видимость

    def show_running(self, status_key: str, **fmt):
        """Показывает progress-state с конкретным сообщением.

        Args:
            status_key: ключ перевода (montage_status_*)
            **fmt: подстановки в текст (например round=2, max_rounds=3,
                   errors_count=5)

        v1.0.78 (Bug 1 fix): передаём **fmt в tr(...) чтобы сработал
        auto-inject `errors_word` через `plural_errors()` когда в
        kwargs есть `errors_count`. Раньше был `tr(status_key)` без
        kwargs + manual `.replace('{errors_count}', ...)` — `errors_word`
        оставался в UI как литерал «{errors_word}».
        """
        self._kind = KIND_RUNNING
        text = tr(status_key, **fmt)
        self._status_text = text
        self._dot_step = 0
        self._render()
        self._dot_timer.start()
        # 2026-05-07: гарантируем видимость. Раньше show_running менял
        # только state — если виджет был hide()'нут (например юзер ушёл
        # на другой эпизод), при возврате CTA оставался невидимым,
        # хотя оркестратор продолжал работать. Теперь .show() здесь.
        self.show()

    def show_failed(self, reason_text: str = ""):
        self._kind = KIND_FAILED
        self._status_text = reason_text
        self._render()
        self._dot_timer.stop()
        self.show()  # 2026-05-07: гарантируем видимость

    def show_open_map(self):
        """v1.0.82: показывает CTA «📂 Открыть монтажную карту».
        Используется когда на эпизоде есть готовая монтажная карта
        (episodes.json[ep]['montage_card'] или _agent_log_epN.json).
        Клик → сигнал open_map_requested → episode_chat читает карту
        с диска и открывает MontageSummaryDialog."""
        self._kind = KIND_OPEN_MAP
        self._render()
        self._dot_timer.stop()
        self.show()

    def show_resumable(self, last_completed_stage: str,
                        next_stage: Optional[str] = None):
        """v1.0.87 (этап 7Б): незавершённая монтажка — pipeline упал
        на этапе `last_completed_stage`. Показываем 2 кнопки —
        «🔄 Продолжить» (resume_requested) и «🆕 Начать заново»
        (start_fresh_requested). Stage-имена локализуются через
        i18n ключи `montage_stage_name_<key>`.
        """
        self._kind = KIND_RESUMABLE
        self._resume_last_stage = last_completed_stage
        self._resume_next_stage = next_stage
        self._render()
        self._dot_timer.stop()
        self.show()

    # ── внутреннее ──

    def _render(self):
        # v1.0.87 (этап 7Б): все ветки теперь должны явно прятать
        # resume_btn / start_fresh_btn (иначе они «прорастают» из
        # KIND_RESUMABLE state в другие при смене kind).
        if self._kind == KIND_IDLE:
            self.title_lbl.setText(tr('montage_cta_title_idle'))
            self.subtitle_lbl.setText(tr('montage_cta_subtitle_idle'))
            self.warning_lbl.setText(tr('montage_cta_warning_os_popups'))
            self.warning_lbl.show()
            self.status_lbl.hide()
            self.start_btn.setText(tr('montage_cta_button_start'))
            self.start_btn.show()
            self.retry_btn.hide()
            self.cancel_btn.hide()
            self.open_map_btn.hide()
            self.resume_btn.hide()
            self.start_fresh_btn.hide()
        elif self._kind == KIND_RUNNING:
            self.title_lbl.setText(tr('montage_cta_title_running'))
            self.subtitle_lbl.setText(tr('montage_cta_subtitle_running'))
            self.warning_lbl.setText(tr('montage_cta_warning_os_popups'))
            self.warning_lbl.show()
            self.status_lbl.setText(self._status_text + ' .' * self._dot_step)
            self.status_lbl.show()
            self.start_btn.hide()
            self.retry_btn.hide()
            self.cancel_btn.setText(tr('montage_cta_button_cancel'))
            self.cancel_btn.show()
            self.open_map_btn.hide()
            self.resume_btn.hide()
            self.start_fresh_btn.hide()
        elif self._kind == KIND_OPEN_MAP:
            # v1.0.82: монтажка готова, лежит на диске.
            self.title_lbl.setText(tr('montage_cta_title_open_map'))
            self.subtitle_lbl.setText(tr('montage_cta_subtitle_open_map'))
            self.warning_lbl.hide()
            self.status_lbl.hide()
            self.start_btn.hide()
            self.retry_btn.hide()
            self.cancel_btn.hide()
            self.open_map_btn.setText(tr('montage_cta_button_open_map'))
            self.open_map_btn.show()
            self.resume_btn.hide()
            self.start_fresh_btn.hide()
        elif self._kind == KIND_RESUMABLE:
            # v1.0.87 (этап 7Б): незавершённая монтажка — pipeline упал
            # на last_completed_stage. Title + subtitle подставляют
            # человечное имя этапа через i18n ключ
            # montage_stage_name_<stage_id>.
            self.title_lbl.setText(tr('montage_cta_title_resumable'))
            stage_id = self._resume_last_stage or "<unknown>"
            try:
                stage_human = tr(f'montage_stage_name_{stage_id}')
            except Exception:
                stage_human = stage_id
            self.subtitle_lbl.setText(
                tr('montage_cta_subtitle_resumable', stage=stage_human))
            self.warning_lbl.setText(tr('montage_cta_warning_os_popups'))
            self.warning_lbl.show()
            self.status_lbl.hide()
            self.start_btn.hide()
            self.retry_btn.hide()
            self.cancel_btn.hide()
            self.open_map_btn.hide()
            self.resume_btn.setText(tr('montage_cta_button_resume'))
            self.resume_btn.show()
            self.start_fresh_btn.setText(
                tr('montage_cta_button_start_fresh'))
            self.start_fresh_btn.show()
        else:  # KIND_FAILED
            self.title_lbl.setText(tr('montage_cta_title_failed'))
            self.subtitle_lbl.setText(self._status_text or tr('montage_cta_subtitle_failed'))
            self.warning_lbl.hide()
            self.status_lbl.hide()
            self.start_btn.hide()
            self.retry_btn.setText(tr('montage_cta_button_retry'))
            self.retry_btn.show()
            self.cancel_btn.hide()
            self.open_map_btn.hide()
            self.resume_btn.hide()
            self.start_fresh_btn.hide()
        self._apply_style()

    def _tick_dots(self):
        self._dot_step = (self._dot_step + 1) % 4
        self.status_lbl.setText(self._status_text + ' .' * self._dot_step)

    def _apply_style(self):
        # 2026-05-08: LUMZ-стилистика. Цвета из views/theme.py:LUMZ_THEME.
        # Каждое state даёт свой акцент:
        #   idle    → нейтральная карточка (bg_card + border_default),
        #             белый текст. CTA-акцент — на самой кнопке (red solid).
        #   running → gold accent — золотой border + золотой текст
        #             (выполняется работа, не ошибка).
        #   failed  → accent_red — красный border/bg + красный текст.
        if self._kind == KIND_IDLE:
            bg = "rgba(255,255,255,0.04)"
            border = "rgba(255,255,255,0.06)"
            title_color = "#ffffff"
            subtitle_color = "rgba(255,255,255,0.55)"
        elif self._kind == KIND_RUNNING:
            bg = "rgba(212,162,86,0.10)"
            border = "rgba(212,162,86,0.40)"
            title_color = "#d4a256"
            subtitle_color = "rgba(255,255,255,0.70)"
        elif self._kind == KIND_OPEN_MAP:
            bg = "#131516"
            border = "#1d1e20"
            title_color = "#50c878"
            subtitle_color = "rgba(255,255,255,0.70)"
        else:  # FAILED
            bg = "rgba(228,52,74,0.10)"
            border = "rgba(228,52,74,0.40)"
            title_color = "#e4344a"
            subtitle_color = "rgba(255,255,255,0.70)"

        self.setStyleSheet(
            f"""
            QFrame#montage-cta {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QLabel#montage-cta-title {{
                color: {title_color};
                font-size: 13px;
                font-weight: 600;
                background: transparent;
            }}
            QLabel#montage-cta-subtitle {{
                color: {subtitle_color};
                font-size: 12px;
                background: transparent;
            }}
            QLabel#montage-cta-status {{
                color: {title_color};
                font-size: 12px;
                background: transparent;
                font-style: italic;
            }}
            QLabel#montage-cta-warning {{
                color: #d4a256;
                font-size: 11px;
                background: transparent;
                padding-top: 4px;
            }}
            /* Primary CTA — solid LUMZ red */
            QPushButton#montage-cta-btn-primary {{
                background: #e4344a;
                color: #fefefe;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#montage-cta-btn-primary:hover {{
                background: #d92d44;
            }}
            QPushButton#montage-cta-btn-primary:pressed {{
                background: #c52539;
            }}
            /* Cancel — нейтральная save-style */
            QPushButton#montage-cta-btn-cancel {{
                background: rgba(255,255,255,0.06);
                color: #ffffff;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#montage-cta-btn-cancel:hover {{
                background: rgba(255,255,255,0.10);
                border-color: rgba(255,255,255,0.20);
            }}
            """
        )

    def retranslate(self):
        """Перерисовать тексты при смене языка."""
        self.start_btn.setText(tr('montage_cta_button_start'))
        self.retry_btn.setText(tr('montage_cta_button_retry'))
        self.cancel_btn.setText(tr('montage_cta_button_cancel'))
        self._render()
