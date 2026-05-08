# -*- coding: utf-8 -*-
"""
widgets/ref_picker_dialog.py — попап с превью референсов из папки.

Phase 2 hotfix #24: заменяет нативный QFileDialog на кастомный диалог
с сеткой превью. Юзер кликает по превью (или по кнопке «Выбрать» под
ним) → файл выбран. Сетка адаптивная, до 3 колонок.

Используется в EpisodeChatView для «📁 Выбрать существующий»:
  • location → все картинки из refs/locations/ сериала
  • object → все из refs/objects/
  • character → все из refs/characters/<name>/

Если папка пуста — показывается «Нет рефов в папке» + кнопка «✕ Закрыть».
Возвращаемое значение через accept() + selected_filename property
(имя файла относительно folder_path).

История: создано 2026-05-05 (Этап 2 фичи) по запросу юзера —
QFileDialog открывает файловый менеджер ОС, юзер хочет «всё в одном
окне» с превьюшками.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QWidget, QFrame,
)

from i18n import tr


_IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}


class _RefThumbCard(QFrame):
    """Одна карточка с превью + кнопкой «Выбрать»."""

    def __init__(self, file_path: Path, on_pick, parent=None):
        super().__init__(parent)
        self._file_path = file_path
        self._on_pick = on_pick
        self.setObjectName("ref-thumb-card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 2026-05-08: LUMZ-стиль — bg_subtle + LUMZ accent_red на hover/btn.
        self.setStyleSheet(
            "QFrame#ref-thumb-card { background:rgba(255,255,255,0.04);"
            " border:1px solid rgba(255,255,255,0.06); border-radius:8px; }"
            "QFrame#ref-thumb-card:hover {"
            " border-color:rgba(228,52,74,0.40); }"
            "QLabel#thumb-name { color:rgba(255,255,255,0.70);"
            " font-size:11px; }"
            "QPushButton#thumb-pick { background:#e4344a; color:#fff;"
            " border:none; border-radius:8px; padding:6px 14px;"
            " font-size:12px; font-weight:500; }"
            "QPushButton#thumb-pick:hover { background:#d92d44; }"
            "QPushButton#thumb-pick:pressed { background:#c4283c; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # Превью
        self.img_lbl = QLabel()
        self.img_lbl.setFixedSize(180, 180)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet(
            "background:#0e0a16; border-radius:4px;")
        try:
            pm = QPixmap(str(file_path))
            if not pm.isNull():
                pm = pm.scaled(
                    QSize(180, 180),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.img_lbl.setPixmap(pm)
            else:
                self.img_lbl.setText("?")
        except Exception:
            self.img_lbl.setText("?")
        # Клик по картинке = выбор (по запросу юзера)
        self.img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.img_lbl.mousePressEvent = self._on_thumb_click
        lay.addWidget(self.img_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        # Имя файла
        name_lbl = QLabel(file_path.name)
        name_lbl.setObjectName("thumb-name")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setWordWrap(True)
        lay.addWidget(name_lbl)

        # Кнопка «Выбрать»
        pick_btn = QPushButton(tr('ref_picker_btn_select'))
        pick_btn.setObjectName("thumb-pick")
        pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pick_btn.clicked.connect(self._on_pick_click)
        lay.addWidget(pick_btn)

    def _on_thumb_click(self, _ev):
        try:
            self._on_pick(self._file_path)
        except Exception:
            pass

    def _on_pick_click(self):
        try:
            self._on_pick(self._file_path)
        except Exception:
            pass


class RefPickerDialog(QDialog):
    """Попап выбора референса из папки.

    После выбора `selected_filename` содержит имя файла относительно
    `folder_path` (либо `None` если юзер закрыл без выбора).

    Использование:
        dlg = RefPickerDialog(folder_path, "Выбрать реф для laura", parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            picked = dlg.selected_filename  # str, например "laura_clean.jpg"
    """
    def __init__(self, folder_path: Path, title: str,
                 parent=None, columns: int = 3):
        super().__init__(parent)
        self.folder_path = Path(folder_path)
        self.selected_filename: Optional[str] = None
        self._columns = max(1, columns)
        self.setWindowTitle(title)
        self.setMinimumSize(680, 560)
        # 2026-05-08: LUMZ — фон тёмный нейтральный, заголовок белый,
        # close-btn без подсвечивающейся фиолетовой рамки.
        self.setStyleSheet(
            "QDialog { background:#0e0a18; }"
            "QLabel#empty-msg { color:rgba(255,255,255,0.55);"
            " font-size:13px; padding:40px; }"
            "QPushButton#close-btn { background:transparent;"
            " color:rgba(255,255,255,0.55);"
            " border:1px solid rgba(255,255,255,0.12);"
            " border-radius:8px; padding:6px 18px; font-size:12px; }"
            "QPushButton#close-btn:hover {"
            " background:rgba(255,255,255,0.06);"
            " color:#ffffff;"
            " border-color:rgba(255,255,255,0.20); }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Header
        header = QLabel(title)
        header.setStyleSheet(
            "color:#ffffff; font-size:14px; font-weight:600;")
        outer.addWidget(header)

        # Сканируем папку
        files = self._collect_files()

        if not files:
            empty = QLabel(tr('ref_picker_empty', folder=self.folder_path.name))
            empty.setObjectName("empty-msg")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            outer.addWidget(empty, stretch=1)
        else:
            # Scroll area со сеткой превью
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setSpacing(12)
            grid.setContentsMargins(4, 4, 4, 4)
            for i, fp in enumerate(files):
                row, col = divmod(i, self._columns)
                card = _RefThumbCard(fp, self._on_pick)
                grid.addWidget(card, row, col)
            # Заполнить пустыми колонками если нужно
            grid.setColumnStretch(self._columns, 1)
            scroll.setWidget(grid_host)
            outer.addWidget(scroll, stretch=1)

        # Footer — кнопка Закрыть/Отмена
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(tr('ref_picker_btn_close'))
        close_btn.setObjectName("close-btn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _collect_files(self) -> List[Path]:
        """Сканирует folder_path на картинки. Сортирует по имени."""
        try:
            if not self.folder_path.is_dir():
                return []
            files = [
                p for p in self.folder_path.iterdir()
                if p.is_file() and p.suffix.lower() in _IMG_EXT
            ]
            return sorted(files, key=lambda p: p.name.lower())
        except Exception:
            return []

    def _on_pick(self, file_path: Path):
        # 2026-05-07 (1г): подтверждение перед привязкой. Юзер случайно
        # тыкал по миниатюре → файл моментально залинковывался к эпизоду.
        # Теперь спрашиваем: «Точно выбрать <имя>?». Отмена → возвращаемся
        # в попап без изменений.
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(tr('ref_picker_confirm_title'))
            box.setText(tr('ref_picker_confirm_msg', filename=file_path.name))
            yes_btn = box.addButton(
                tr('ref_picker_confirm_yes'), QMessageBox.ButtonRole.AcceptRole)
            no_btn = box.addButton(
                tr('ref_picker_confirm_no'), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no_btn)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
        except Exception:
            # Если что-то пошло не так с диалогом — fallback на старое поведение
            # (привязываем без подтверждения), чтобы не залипало.
            pass
        try:
            self.selected_filename = file_path.name
        except Exception:
            self.selected_filename = None
        self.accept()
