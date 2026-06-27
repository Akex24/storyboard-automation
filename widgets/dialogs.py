# -*- coding: utf-8 -*-
"""
widgets/dialogs.py — независимые диалоги Storyboard Studio.

Содержит 4 класса QDialog которые НЕ зависят от ActorsView/EditorView
(не делают callback'ов в конкретные view), используют только:
    - tr() из i18n
    - стандартные PyQt6 виджеты

    - FullscreenImageDialog        — fullscreen-просмотр картинки (Esc/click)
    - RefDoneNoticeDialog          — попап после регенерации/edit'а рефа
    - GeometryDoneNoticeDialog     — попап после обновления geometry
    - CloseConfirmDialog           — подтверждение закрытия Studio при активных задачах

История: вытащено из storyboard_app.py 2026-05-04 (шаг 3 рефакторинга).

Эти диалоги НЕ нуждаются в `_AppProxy` (нет обращений к module-level state
storyboard_app). Если в будущем понадобится — паттерн уже отработан в
threads/update.py / threads/generate.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap, QGuiApplication, QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QDialog, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
)

from i18n import tr


# ─── Fullscreen-просмотр картинки ────────────────────────────────

class FullscreenImageDialog(QDialog):
    """Тёмный модальный fullscreen-просмотр картинки.
    Закрытие: клик в любое место ИЛИ Esc."""

    def __init__(self, image_path: Path, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        # На весь экран родителя. На macOS 26 `parent.geometry()` иногда
        # возвращает невалидный QRect для главного окна — оборачиваем в try
        # и фолбэкаем на размер скрина чтобы диалог не падал.
        try:
            if parent:
                geom = parent.geometry()
                if geom.isValid() and geom.width() > 100 and geom.height() > 100:
                    self.setGeometry(geom)
                else:
                    raise ValueError("invalid parent geometry")
            else:
                raise ValueError("no parent")
        except Exception:
            try:
                screen = QGuiApplication.primaryScreen()
                if screen is not None:
                    self.setGeometry(screen.availableGeometry())
            except Exception:
                self.resize(1280, 800)
        self.setStyleSheet("background: rgba(0, 0, 0, 0.97);")

        v = QVBoxLayout(self)
        v.setContentsMargins(40, 30, 40, 30)
        v.setSpacing(16)

        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet("background: transparent;")
        try:
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                # Масштаб под доступный размер диалога с сохранением пропорций
                w = max(800, self.width() - 100)
                h = max(600, self.height() - 100)
                scaled = pixmap.scaled(
                    QSize(w, h),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.img_label.setPixmap(scaled)
        except Exception:
            pass
        v.addWidget(self.img_label, stretch=1)

        hint = QLabel(tr('fullscreen_close_hint'))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
        v.addWidget(hint)

        # Esc → закрыть
        sc = QShortcut(QKeySequence("Escape"), self)
        sc.activated.connect(self.accept)

    def mousePressEvent(self, ev):
        # Клик в любое место → закрыть
        self.accept()


# ─── Попап после регенерации/edit'а рефа ─────────────────────────

class RefDoneNoticeDialog(QDialog):
    """Чёткий попап после успешной регенерации/edit'а локации:
    «Изображение локации обновлено. Скопируй фразу для ассистента чтобы он обновил geometry»."""

    def __init__(self, ref_name: str, parent=None, kind: str = 'location'):
        super().__init__(parent)
        # Заголовок-окна и текст плашки зависят от типа рефа
        title_key = {
            'location':  'ref_done_title_location',
            'object':    'ref_done_title_object',
            'character': 'ref_done_title_character',
        }.get(kind, 'ref_done_title')
        title_text = tr(title_key)
        self.setWindowTitle(title_text)
        self.setFixedSize(580, 320)
        self.setStyleSheet("QDialog { background: #121313; }")

        phrase = tr('ref_chat_phrase_location', name=ref_name)

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)

        title = QLabel("⚠  " + title_text)
        title.setStyleSheet("color: #ffaa44; font-size: 16px; font-weight: 700;")
        v.addWidget(title)

        msg = QLabel(tr('ref_done_msg'))
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #ddd; font-size: 13px;")
        v.addWidget(msg)

        # Поле с фразой для копирования
        phrase_lbl = QLabel(phrase)
        phrase_lbl.setStyleSheet(
            "background: #191b1d; border: 1px solid #1d1e20; border-radius: 6px;"
            "padding: 12px 14px; color: #c8a8ff; font-size: 14px; font-weight: 600;"
            "font-family: 'Menlo', 'Consolas', monospace;")
        phrase_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(phrase_lbl)

        # Кнопки внизу: Скопировать / Понял
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        copy_btn = QPushButton(tr('ref_done_copy'))
        copy_btn.setObjectName("save")
        copy_btn.setFixedHeight(36)
        def _copy():
            try:
                QGuiApplication.clipboard().setText(phrase)
                copy_btn.setText(tr('ref_done_copied'))
            except Exception:
                pass
        copy_btn.clicked.connect(_copy)
        btn_row.addWidget(copy_btn, stretch=1)

        ok_btn = QPushButton(tr('ref_done_ok'))
        ok_btn.setFixedHeight(36)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)

        v.addLayout(btn_row)


# ─── Попап после обновления geometry ─────────────────────────────

class GeometryDoneNoticeDialog(QDialog):
    """Попап после ПОЛНОГО завершения обновления рефа: ассистент уже
    переписал geometry-файл. Юзеру ничего делать не надо — просто подтверждаем
    что изображение и описание обновлены, можно продолжать работу.

    Показывается когда юзер открывает «Референсы» и в очереди есть
    notices с mode='geometry_done'."""

    def __init__(self, ref_name: str, parent=None, kind: str = 'location'):
        super().__init__(parent)
        title_key = {
            'location':  'geom_notice_title_location',
            'object':    'geom_notice_title_object',
            'character': 'geom_notice_title_character',
        }.get(kind, 'geom_notice_title_location')
        title_text = tr(title_key)
        self.setWindowTitle(title_text)
        self.setFixedSize(520, 200)
        self.setStyleSheet("QDialog { background: #121313; }")

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)

        title = QLabel(title_text)
        title.setStyleSheet("color: #6db86d; font-size: 16px; font-weight: 700;")
        v.addWidget(title)

        msg = QLabel(tr('geom_notice_msg', name=ref_name))
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #ddd; font-size: 13px;")
        v.addWidget(msg)

        v.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton(tr('ref_done_ok'))
        ok_btn.setObjectName("save")
        ok_btn.setFixedHeight(36)
        ok_btn.setMinimumWidth(120)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        v.addLayout(btn_row)


# ─── Подтверждение закрытия Studio при активных задачах ──────────

class CloseConfirmDialog(QDialog):
    """Попап при попытке закрыть Studio во время активных задач (генерация
    шотов, регенерация рефов, обновление geometry, AI-запросы).
    Дефолт-кнопка — «Подождать» (защита от случайного Enter).
    Accept = закрыть всё равно. Reject = подождать (отмена закрытия).
    """

    def __init__(self, task_lines: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('close_confirm_title'))
        self.setFixedSize(560, 320)
        self.setStyleSheet("QDialog { background: #121313; }")

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)

        title = QLabel(tr('close_confirm_title'))
        title.setStyleSheet("color: #ffaa44; font-size: 16px; font-weight: 700;")
        v.addWidget(title)

        # Сводка задач — собираем в одну HTML-строку с <br> между
        tasks_html = "<br>".join(task_lines) if task_lines else ""
        msg = QLabel(tr('close_confirm_msg', tasks=tasks_html))
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setStyleSheet("color: #ddd; font-size: 13px; line-height: 1.4;")
        v.addWidget(msg)

        v.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        # «Закрыть всё равно» — destructive, слева
        force_btn = QPushButton(tr('close_confirm_force'))
        force_btn.setFixedHeight(36)
        force_btn.setMinimumWidth(160)
        force_btn.setStyleSheet(
            "QPushButton { color: #ff7a7a; }"
            "QPushButton:hover { color: #ff9a9a; }")
        force_btn.clicked.connect(self.accept)
        btn_row.addWidget(force_btn)

        # «Подождать» — primary, справа, дефолт (защита от Enter)
        wait_btn = QPushButton(tr('close_confirm_wait'))
        wait_btn.setObjectName("save")
        wait_btn.setFixedHeight(36)
        wait_btn.setMinimumWidth(140)
        wait_btn.setDefault(True)
        wait_btn.clicked.connect(self.reject)
        btn_row.addWidget(wait_btn)

        v.addLayout(btn_row)
