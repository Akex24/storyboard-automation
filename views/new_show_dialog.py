# -*- coding: utf-8 -*-
"""
views/new_show_dialog.py — диалог создания нового сериала.

Чистый UI, без файловых операций. Логика создания папок и meta.json —
в `show_manager.create_show()`. Диалог только спрашивает название и
показывает live-превью slug'а под полем ввода.

При accept — caller (storyboard_app) вызывает `show_manager.create_show()`
с введённым display_name. Caller также сам решает: становится ли новый
сериал активным (set_current_show), как обновить дропдаун и т.п.

История: создан 2026-05-05 для UI «➕ Создать сериал».
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)

import show_manager
from i18n import tr


class NewShowDialog(QDialog):
    """Спрашивает название сериала. На accept каллер достаёт его из `display_name`.

    Use:
        dlg = NewShowDialog(parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name = dlg.display_name()
            slug = show_manager.create_show(project_root, name)
            ...
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr('new_show_dialog_title'))
        self.setModal(True)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        # Поле ввода названия
        self._name_label = QLabel(tr('new_show_name_label'))
        lay.addWidget(self._name_label)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("")
        self._name_edit.textChanged.connect(self._on_name_changed)
        lay.addWidget(self._name_edit)

        # Live-превью slug'а
        self._slug_hint = QLabel(tr('new_show_folder_hint_empty'))
        self._slug_hint.setStyleSheet("color: #888888; font-size: 11px;")
        lay.addWidget(self._slug_hint)

        # Сообщение об ошибке (изначально скрыто)
        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: #d65a5a; font-size: 11px;")
        self._error_label.hide()
        lay.addWidget(self._error_label)

        lay.addSpacing(6)

        # Кнопки
        btns = QHBoxLayout()
        btns.addStretch(1)
        self._cancel_btn = QPushButton(tr('new_show_btn_cancel'))
        self._cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self._cancel_btn)

        self._create_btn = QPushButton(tr('new_show_btn_create'))
        self._create_btn.setDefault(True)
        self._create_btn.setEnabled(False)
        self._create_btn.clicked.connect(self._on_create_clicked)
        btns.addWidget(self._create_btn)
        lay.addLayout(btns)

        self._name_edit.setFocus()

    # ─── Public API ──────────────────────────────────────────────────────

    def display_name(self) -> str:
        """Введённое юзером название сериала (без trim — caller сам)."""
        return self._name_edit.text()

    # ─── Сигналы / слоты ─────────────────────────────────────────────────

    def _on_name_changed(self, text: str) -> None:
        """Обновляет live-превью slug'а и скрывает ошибку."""
        name = text.strip()
        if not name:
            self._slug_hint.setText(tr('new_show_folder_hint_empty'))
            self._create_btn.setEnabled(False)
        else:
            slug = show_manager.make_slug(name)
            self._slug_hint.setText(tr('new_show_folder_hint', slug=slug))
            self._create_btn.setEnabled(True)
        self._error_label.hide()

    def _on_create_clicked(self) -> None:
        """Финальная валидация перед accept."""
        if not self._name_edit.text().strip():
            self._error_label.setText(tr('new_show_error_empty_name'))
            self._error_label.show()
            return
        self.accept()

    def keyPressEvent(self, event) -> None:
        """Enter в поле имени → создать (если кнопка enabled)."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._create_btn.isEnabled():
                self._on_create_clicked()
                return
        super().keyPressEvent(event)
