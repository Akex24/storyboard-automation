# -*- coding: utf-8 -*-
"""
widgets/auth_banner.py — горизонтальная плашка-баннер «AI-аккаунт сменился /
вышел / лимит исчерпан». Появляется под шапкой Studio когда MainWindow
обнаруживает что текущий AI-аккаунт CLI больше не пригоден для работы.

Сигналы:
    switch_requested  — клик «🔄 Войти в другой аккаунт»
    dismiss_requested — клик «✕ Скрыть»

История: создано 2026-05-06 для auto-detect смены AI-аккаунта в Studio.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy

from i18n import tr


# Семантика типа баннера определяет цветовую схему и текст.
KIND_LOGGED_OUT = 'logged_out'
KIND_CHANGED = 'changed'
KIND_QUOTA = 'quota'
KIND_PROGRESS = 'progress'
KIND_DONE = 'done'
KIND_FAILED = 'failed'
KIND_SAME_ACCOUNT = 'same_account'


class AuthBanner(QFrame):
    """Плашка вверху главного окна для уведомлений об AI-аккаунте."""

    switch_requested = pyqtSignal()
    dismiss_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("auth-banner")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        # 2026-05-06: высота адаптивная (от minimum 40px до естественного
        # размера word-wrapped текста). Раньше было setFixedHeight(40) —
        # длинные сообщения вроде «Ты снова вошёл в тот же аккаунт …»
        # обрезались по краям. Теперь плашка растягивается вертикально.
        self.setMinimumHeight(40)

        self._kind: str = KIND_LOGGED_OUT
        self._email: str = ''

        h = QHBoxLayout(self)
        h.setContentsMargins(16, 8, 12, 8)
        h.setSpacing(12)

        self.text_lbl = QLabel("")
        self.text_lbl.setObjectName("auth-banner-text")
        # 2026-05-06: word-wrap включён — длинные сообщения переносятся на
        # вторую строку вместо обрезания.
        self.text_lbl.setWordWrap(True)
        self.text_lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Preferred)
        h.addWidget(self.text_lbl, stretch=1)

        self.switch_btn = QPushButton(tr('auth_button_switch'))
        self.switch_btn.setObjectName("auth-banner-btn-primary")
        self.switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch_btn.clicked.connect(self.switch_requested.emit)
        h.addWidget(self.switch_btn)

        self.dismiss_btn = QPushButton(tr('auth_button_dismiss'))
        self.dismiss_btn.setObjectName("auth-banner-btn-secondary")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.clicked.connect(self.dismiss_requested.emit)
        h.addWidget(self.dismiss_btn)

        # Одинаковый тёмный стиль (детали регулируются через kind).
        self._apply_style()

    def show_for(self, kind: str, email: Optional[str] = None):
        """Показать плашку с соответствующим текстом и стилем."""
        self._kind = kind
        self._email = email or ''
        self._render()
        self.show()

    def show_progress(self):
        self.show_for(KIND_PROGRESS)

    def show_done(self, email: str):
        self.show_for(KIND_DONE, email=email)

    def show_failed(self):
        self.show_for(KIND_FAILED)

    def show_same_account(self, email: str):
        """Юзер случайно залогинился в тот же аккаунт — показываем
        специальный текст «выбери ДРУГОЙ»."""
        self.show_for(KIND_SAME_ACCOUNT, email=email)

    def _render(self):
        # Текст
        if self._kind == KIND_LOGGED_OUT:
            self.text_lbl.setText(tr('auth_banner_logged_out'))
        elif self._kind == KIND_CHANGED:
            self.text_lbl.setText(
                tr('auth_banner_changed').replace('{email}', self._email or '?'))
        elif self._kind == KIND_QUOTA:
            self.text_lbl.setText(tr('auth_banner_quota_exceeded'))
        elif self._kind == KIND_PROGRESS:
            self.text_lbl.setText(tr('auth_progress_logging_in'))
        elif self._kind == KIND_DONE:
            self.text_lbl.setText(
                tr('auth_done_now_using').replace('{email}', self._email or '?'))
        elif self._kind == KIND_FAILED:
            self.text_lbl.setText(tr('auth_failed'))
        elif self._kind == KIND_SAME_ACCOUNT:
            self.text_lbl.setText(
                tr('auth_failed_same_account').replace('{email}', self._email or '?'))
        else:
            self.text_lbl.setText("")

        # Видимость кнопок
        if self._kind in (KIND_PROGRESS,):
            self.switch_btn.setVisible(False)
            self.dismiss_btn.setVisible(False)
        elif self._kind == KIND_DONE:
            self.switch_btn.setVisible(False)
            self.dismiss_btn.setVisible(True)
        elif self._kind == KIND_FAILED:
            self.switch_btn.setVisible(True)
            self.dismiss_btn.setVisible(True)
        else:
            self.switch_btn.setVisible(True)
            self.dismiss_btn.setVisible(True)
        self._apply_style()

    def _apply_style(self):
        # Цветовая палитра для тёмной темы Studio.
        if self._kind == KIND_DONE:
            bg, fg, border = "#1f3a23", "#a7e8b3", "#2d6c39"
        elif self._kind == KIND_FAILED:
            bg, fg, border = "#3a1e22", "#ff8a8a", "#702530"
        elif self._kind == KIND_PROGRESS:
            bg, fg, border = "#2a2740", "#c8c0ff", "#494070"
        elif self._kind == KIND_QUOTA:
            bg, fg, border = "#3a2a1c", "#ffc890", "#7a5828"
        elif self._kind == KIND_SAME_ACCOUNT:
            # Те же оранжевые тона что у quota — это семантически
            # «опять не сработало, нужно попробовать ещё раз».
            bg, fg, border = "#3a2a1c", "#ffc890", "#7a5828"
        else:
            # changed / logged_out
            bg, fg, border = "#2a2a40", "#e5d7a3", "#5a5a80"

        self.setStyleSheet(
            f"""
            QFrame#auth-banner {{
                background: {bg};
                border-bottom: 1px solid {border};
            }}
            QLabel#auth-banner-text {{
                color: {fg};
                font-size: 12px;
                background: transparent;
            }}
            QPushButton#auth-banner-btn-primary {{
                background: #4a5fcc;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#auth-banner-btn-primary:hover {{ background: #5a6fdc; }}
            QPushButton#auth-banner-btn-primary:pressed {{ background: #3a4fbc; }}
            QPushButton#auth-banner-btn-secondary {{
                background: transparent;
                color: {fg};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 12px;
            }}
            QPushButton#auth-banner-btn-secondary:hover {{
                background: rgba(255,255,255,0.05);
            }}
            """
        )

    def retranslate(self):
        """Перерисовать текст после смены языка."""
        self.switch_btn.setText(tr('auth_button_switch'))
        self.dismiss_btn.setText(tr('auth_button_dismiss'))
        self._render()
