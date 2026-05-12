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


class MontageCTA(QFrame):
    """Карточка-CTA для запуска multi-agent монтажа."""

    start_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    cancel_requested = pyqtSignal()  # 2026-05-06: «✗ Прервать» в running-state

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("montage-cta")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        self._kind: str = KIND_IDLE
        self._status_text: str = ""
        self._dot_step = 0

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
            **fmt: подстановки в текст (например round=2, max_rounds=3)
        """
        self._kind = KIND_RUNNING
        text = tr(status_key)
        for k, v in fmt.items():
            text = text.replace('{' + k + '}', str(v))
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

    # ── внутреннее ──

    def _render(self):
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
        else:  # KIND_FAILED
            self.title_lbl.setText(tr('montage_cta_title_failed'))
            self.subtitle_lbl.setText(self._status_text or tr('montage_cta_subtitle_failed'))
            self.warning_lbl.hide()
            self.status_lbl.hide()
            self.start_btn.hide()
            self.retry_btn.setText(tr('montage_cta_button_retry'))
            self.retry_btn.show()
            self.cancel_btn.hide()
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
                color: #ffffff;
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
