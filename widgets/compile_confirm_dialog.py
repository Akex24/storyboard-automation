# -*- coding: utf-8 -*-
"""
widgets/compile_confirm_dialog.py — попап подтверждения перед сборкой
серии в zip.

Открывается при клике «📦 Собрать серию» в редакторе эпизода
(_on_compile_episode_btn). Юзер должен явно подтвердить два условия
чекбоксами:
  • «Все сториборды сохранены» — нужная активная версия (use this) выбрана
    для каждого блока.
  • «Все промпты Seedance сохранены» — нужная вкладка промпта в попапе
    Seedance активирована для каждого блока.

Защита от случайной сборки серии до того как контент готов.

Состояния:
  • Открытие — оба чекбокса пустые, кнопка «📦 Собрать серию» disabled.
  • Любой чекбокс отмечен — пока второй не отмечен, кнопка остаётся disabled.
  • Оба отмечены — кнопка активна, клик → accept().
  • ESC / крестик окна / кнопка «Отмена» — reject() (QDialog default).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
)

from i18n import tr


class CompileConfirmDialog(QDialog):
    """Модальный confirm-dialog: 2 чекбокса + Отмена/Собрать."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('compile_confirm_title'))
        self.setModal(True)
        self.setMinimumSize(440, 280)
        self.resize(480, 320)
        self.setStyleSheet("QDialog { background: #121313; }")

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)

        # ── Заголовок (красный — primary action color из compile_ep_btn) ──
        title = QLabel(tr('compile_confirm_title'))
        title.setStyleSheet(
            "color: #e4344a; font-size: 16px; font-weight: 700;")
        v.addWidget(title)

        # ── Описание ──────────────────────────────────────────────────
        msg = QLabel(tr('compile_confirm_msg'))
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #ddd; font-size: 13px;")
        v.addWidget(msg)

        v.addSpacing(8)

        # ── Чекбоксы ──────────────────────────────────────────────────
        chk_style = ("QCheckBox { color: #ddd; font-size: 13px;"
                     " padding: 4px; }"
                     "QCheckBox::indicator { width: 16px; height: 16px; }")
        self.chk_storyboards = QCheckBox(
            tr('compile_confirm_chk_storyboards'))
        self.chk_storyboards.setStyleSheet(chk_style)
        self.chk_storyboards.stateChanged.connect(self._update_confirm_state)
        v.addWidget(self.chk_storyboards)

        self.chk_seedance = QCheckBox(
            tr('compile_confirm_chk_seedance'))
        self.chk_seedance.setStyleSheet(chk_style)
        self.chk_seedance.stateChanged.connect(self._update_confirm_state)
        v.addWidget(self.chk_seedance)

        v.addStretch()

        # ── Кнопки ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        # «Отмена» — subtle, слева, всегда активна.
        self.cancel_btn = QPushButton(tr('compile_confirm_cancel'))
        self.cancel_btn.setFixedHeight(36)
        self.cancel_btn.setMinimumWidth(120)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #aaa;"
            " border: 1px solid #3a2c52; border-radius: 6px;"
            " padding: 8px 16px; font-size: 13px; }"
            "QPushButton:hover { color: #fff; border-color: #5a4a82; }")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        btn_row.addStretch()

        # «📦 Собрать серию» — primary red, справа. Disabled пока оба
        # чекбокса не отмечены. Текст reuse'ит ключ compile_ep_btn чтобы
        # не дублировать i18n.
        self.confirm_btn = QPushButton(tr('compile_ep_btn'))
        self.confirm_btn.setFixedHeight(36)
        self.confirm_btn.setMinimumWidth(180)
        self.confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.setStyleSheet(
            "QPushButton { background: #e4344a; color: #fefefe;"
            " border: none; border-radius: 6px;"
            " padding: 8px 18px; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: #ec4d62; }"
            "QPushButton:disabled { background: #3a2530; color: #888; }")
        self.confirm_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.confirm_btn)

        v.addLayout(btn_row)

        # Фокус на первом чекбоксе для клавиатурной навигации (Space).
        self.chk_storyboards.setFocus()

    def _update_confirm_state(self) -> None:
        """Активирует confirm_btn когда оба чекбокса отмечены."""
        both_checked = (self.chk_storyboards.isChecked()
                        and self.chk_seedance.isChecked())
        self.confirm_btn.setEnabled(both_checked)
