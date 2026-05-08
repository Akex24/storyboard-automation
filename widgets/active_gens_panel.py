# -*- coding: utf-8 -*-
"""
widgets/active_gens_panel.py — non-modal попап «Активные генерации».

Показывает все запущенные параллельные генерации location/object по всем
эпизодам сериала. Не блокирует Studio: попап остаётся открытым пока юзер
ходит между эпизодами/вкладками. Источник истины — `MainWindow._active_gens`
(глобальный реестр), панель только рендерит строки и эмитит сигналы клика.

Сигналы:
    open_episode_requested(ep_id) — клик по строке (юзер хочет перейти
                                    в чат этого эпизода)
    dismiss_requested(key)        — клик «×» на error-строке

История: создано 2026-05-07 (Variant B параллельных генераций — карточки
бегают в попапе, чтобы не зажимать чат).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QSizePolicy
)

from i18n import tr


# Шаги анимации точек — синхронизированы с `MainWindow._dot_step`
DOTS_PATTERN = ["·    ", "· ·  ", "· · ·"]

# Иконки по типу
TYPE_ICON = {
    'location': '📍',
    'object': '🎁',
    'character': '🎭',  # на всякий, хотя character сюда не попадает
}


class ActiveGenRow(QFrame):
    """Одна строка попапа: [ЭП X] icon + name + status (running/done/error)."""

    open_episode_requested = pyqtSignal(str)  # ep_id
    dismiss_requested = pyqtSignal(str)       # key

    def __init__(self, key: str, ep_id: str, gen_type: str,
                 name: str, parent=None):
        super().__init__(parent)
        self._key = key
        self._ep_id = ep_id
        self._gen_type = gen_type
        self._name = name
        self._state = "running"
        self._progress_text = tr('gen_state_running_default')
        self._dot_step = 0
        self.setObjectName("ActiveGenRow")
        self._build()
        self._refresh_text()

    def _build(self):
        self.setStyleSheet(
            "#ActiveGenRow { background:#1a1330; border:1px solid #322545;"
            " border-radius:8px; padding:6px 10px; }"
            "#ActiveGenRow:hover { background:#231840; border-color:#4a3470; }"
            "QLabel { color:#cfcfcf; font-size:12px; }"
            "QLabel#ep_badge { color:#fff; background:#4a3470;"
            " border-radius:4px; padding:1px 6px; font-weight:600; }"
            "QLabel#name_lbl { color:#fff; font-weight:600; }"
            "QLabel#status_lbl { color:#bba4d6; font-style:italic; }"
            "QPushButton#dismiss_btn { background:transparent;"
            " border:none; color:#a85a5a; font-size:13px; padding:2px 6px; }"
            "QPushButton#dismiss_btn:hover { color:#ff7070; }"
        )
        # Click on the row body → open episode
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        ep_label = self._format_ep_label(self._ep_id)
        self.ep_lbl = QLabel(ep_label)
        self.ep_lbl.setObjectName("ep_badge")
        row.addWidget(self.ep_lbl)

        self.icon_lbl = QLabel(TYPE_ICON.get(self._gen_type, '🎨'))
        row.addWidget(self.icon_lbl)

        self.name_lbl = QLabel(self._name)
        self.name_lbl.setObjectName("name_lbl")
        row.addWidget(self.name_lbl)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("status_lbl")
        self.status_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status_lbl, stretch=1)

        # Dismiss кнопка — видна только при error
        self.dismiss_btn = QPushButton("✕")
        self.dismiss_btn.setObjectName("dismiss_btn")
        self.dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dismiss_btn.setFixedSize(22, 22)
        self.dismiss_btn.setVisible(False)
        self.dismiss_btn.clicked.connect(
            lambda: self.dismiss_requested.emit(self._key))
        row.addWidget(self.dismiss_btn)

    @staticmethod
    def _format_ep_label(ep_id: str) -> str:
        """`ep4` → `ЭП 4`. Для нестандартных id — как есть."""
        if ep_id and ep_id.startswith('ep'):
            tail = ep_id[2:]
            if tail.isdigit():
                return tr('active_gens_ep_badge', n=int(tail))
        return ep_id or '?'

    def mousePressEvent(self, ev):
        # Клик по телу строки (не по dismiss кнопке) → открыть эпизод
        if ev.button() == Qt.MouseButton.LeftButton:
            self.open_episode_requested.emit(self._ep_id)
        super().mousePressEvent(ev)

    # ── State updates ────────────────────────────────────────────────

    def set_progress_text(self, text: str):
        """Обновляет статус-фразу. Анимация точек — отдельно через `tick_dots`."""
        self._progress_text = text or tr('gen_state_running_default')
        self._refresh_text()

    def tick_dots(self, dot_step: int):
        """Шаг анимации точек (0/1/2). Зовётся из MainWindow._tick_dots."""
        self._dot_step = dot_step % len(DOTS_PATTERN)
        if self._state == "running":
            self._refresh_text()

    def set_done(self):
        self._state = "done"
        self.status_lbl.setText("✓ " + tr('active_gens_status_done'))
        self.status_lbl.setStyleSheet("color:#7fbf7f; font-style:normal;")
        self.dismiss_btn.setVisible(False)

    def set_error(self, msg: str):
        self._state = "error"
        short = (msg or "").strip()
        if len(short) > 80:
            short = short[:77] + "…"
        self.status_lbl.setText("✗ " + (short or tr('active_gens_status_error')))
        self.status_lbl.setStyleSheet("color:#d97070; font-style:normal;")
        self.dismiss_btn.setVisible(True)

    def _refresh_text(self):
        """Перерисовка статуса (для running — с бегущими точками)."""
        if self._state != "running":
            return
        dots = DOTS_PATTERN[self._dot_step]
        self.status_lbl.setText(dots + "  " + self._progress_text)
        self.status_lbl.setStyleSheet("color:#bba4d6; font-style:italic;")


class ActiveGensPanel(QDialog):
    """Non-modal попап со списком активных генераций.

    Не блокирует Studio (Qt.WindowType.Tool + non-modal). Источник истины —
    MainWindow, панель только рендерит. Все мутации (add/remove/update) идут
    через публичные методы из MW.
    """

    open_episode_requested = pyqtSignal(str)
    dismiss_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        # Tool window — не получает фокус, не блокирует main, ставится поверх.
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle(tr('active_gens_panel_title'))
        self.setModal(False)
        self.setMinimumSize(420, 220)
        self.resize(500, 320)
        self._rows: dict = {}  # key → ActiveGenRow
        self._build()

    def _build(self):
        self.setStyleSheet(
            "QDialog { background:#0f0a18; }"
            "QLabel#header_title { color:#fff; font-size:14px;"
            " font-weight:600; }"
            "QLabel#empty_lbl { color:#6a6a8a; font-style:italic;"
            " font-size:13px; }"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel(tr('active_gens_panel_title'))
        title.setObjectName("header_title")
        header.addWidget(title)
        header.addStretch()
        lay.addLayout(header)

        # Scroll area со строками
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet(
            "QScrollArea { border:1px solid #25193a; border-radius:6px;"
            " background:#0a0612; }")
        container = QWidget()
        self._rows_layout = QVBoxLayout(container)
        self._rows_layout.setContentsMargins(8, 8, 8, 8)
        self._rows_layout.setSpacing(6)
        self._rows_layout.addStretch()
        self.scroll.setWidget(container)
        lay.addWidget(self.scroll, stretch=1)

        # Empty placeholder
        self.empty_lbl = QLabel(tr('active_gens_empty'))
        self.empty_lbl.setObjectName("empty_lbl")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.empty_lbl)
        self._refresh_empty()

    # ── Public API (вызывается из MainWindow) ─────────────────────────

    def add_row(self, key: str, ep_id: str, gen_type: str, name: str):
        if key in self._rows:
            return
        row = ActiveGenRow(key, ep_id, gen_type, name)
        row.open_episode_requested.connect(self.open_episode_requested.emit)
        row.dismiss_requested.connect(self.dismiss_requested.emit)
        # Вставляем перед stretch
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._rows[key] = row
        self._refresh_empty()

    def update_progress(self, key: str, text: str):
        row = self._rows.get(key)
        if row is not None:
            row.set_progress_text(text)

    def set_done(self, key: str):
        row = self._rows.get(key)
        if row is not None:
            row.set_done()

    def set_error(self, key: str, msg: str):
        row = self._rows.get(key)
        if row is not None:
            row.set_error(msg)

    def remove_row(self, key: str):
        row = self._rows.pop(key, None)
        if row is not None:
            try:
                self._rows_layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            except Exception:
                pass
        self._refresh_empty()

    def tick_dots(self, dot_step: int):
        """Шаг анимации точек (0/1/2). Зовётся из MainWindow._tick_dots."""
        for row in self._rows.values():
            try:
                row.tick_dots(dot_step)
            except Exception:
                pass

    def row_count(self) -> int:
        return len(self._rows)

    def apply_lang(self):
        """Перевод после смены языка."""
        self.setWindowTitle(tr('active_gens_panel_title'))
        self.empty_lbl.setText(tr('active_gens_empty'))

    def _refresh_empty(self):
        empty = (len(self._rows) == 0)
        self.empty_lbl.setVisible(empty)
        self.scroll.setVisible(not empty)
