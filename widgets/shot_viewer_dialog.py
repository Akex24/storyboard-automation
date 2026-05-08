# -*- coding: utf-8 -*-
"""
widgets/shot_viewer_dialog.py — попап просмотра шота с историей версий.

Открывается при клике на карточку шота в гриде блока. Содержит:
  • Большую картинку текущей активной версии (9:16, ~600×1080).
  • Ленту миниатюр всех ранее сгенерированных версий.
  • Кнопки: «✏️ Редактировать», «↻ Перегенерировать», «✓ Использовать
    эту» (active при выборе не-активной версии).

История хранится в `STORYBOARDS_DIR/_history/<basename>/v1.jpg, v2.jpg, …`
+ `active.txt` указывает на текущую активную. См. helper'ы
`shot_history_dir / list_shot_versions / read_active_version /
set_active_version` в `storyboard_app.py`.

История: создано 2026-05-07 (Правка B — попап с версиями).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, List

from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QSizePolicy
)

from i18n import tr


# 2026-05-07 (фикс UI): уменьшено чтобы попап влезал на 14" MacBook
# (1512×982 logical = ~900px usable height после menu bar). Раньше было
# 540×960 — попап был 1080+px и не помещался. Теперь preview ≈ исходный
# размер шота 384×688, дополнительно × 1.4 для удобства просмотра.
PREVIEW_W = 384
PREVIEW_H = 688

# Миниатюра в ленте версий — компактнее.
THUMB_W = 70
THUMB_H = 125  # 70 × (688/384) ≈ 125


class VersionThumb(QFrame):
    """Кликабельная миниатюра версии в ленте."""

    clicked = pyqtSignal(int)  # version_n

    def __init__(self, version_n: int, image_path: Path, is_active: bool,
                 parent=None):
        super().__init__(parent)
        self.version_n = version_n
        self.image_path = image_path
        self._is_active = is_active
        self._is_selected = is_active  # выбран по умолчанию = активный
        self.setObjectName("VersionThumb")
        self.setFixedSize(THUMB_W + 6, THUMB_H + 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._refresh_style()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(4)
        self.img_lbl = QLabel()
        self.img_lbl.setFixedSize(THUMB_W, THUMB_H)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet(
            "background:#1a1424; border-radius:4px;")
        # Загружаем картинку.
        try:
            pix = QPixmap(str(self.image_path))
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(THUMB_W, THUMB_H),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.img_lbl.setPixmap(pix)
        except Exception:
            pass
        lay.addWidget(self.img_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        active_suffix = "  ✓" if self._is_active else ""
        self.label = QLabel(f"v{self.version_n}{active_suffix}")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._is_active:
            self.label.setStyleSheet(
                "color:#7fbf7f; font-size:11px; font-weight:600;")
        else:
            self.label.setStyleSheet(
                "color:#bba4d6; font-size:11px;")
        lay.addWidget(self.label)

    def set_selected(self, selected: bool):
        self._is_selected = selected
        self._refresh_style()

    def _refresh_style(self):
        if self._is_selected:
            border = "2px solid #6e4cc4"
            bg = "#231840"
        else:
            border = "1px solid #322545"
            bg = "transparent"
        self.setStyleSheet(
            f"#VersionThumb {{ background:{bg}; border:{border};"
            f" border-radius:6px; }}")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.version_n)
        super().mousePressEvent(ev)


class ShotViewerDialog(QDialog):
    """Non-modal попап просмотра одного шота с лентой версий.

    Сигналы наружу (MainWindow подключает):
      • edit_requested(int)           — клик «Редактировать» (panel_idx)
      • regen_requested(int)          — клик «Перегенерировать»
      • version_use_requested(int,    — клик «Использовать эту» (panel_idx, version_n)
                              int)
    """

    edit_requested = pyqtSignal(int)
    regen_requested = pyqtSignal(int)
    version_use_requested = pyqtSignal(int, int)

    def __init__(self, panel_idx: int, block_name: str,
                 active_path: Path, history_dir: Path,
                 parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.panel_idx = panel_idx
        self.block_name = block_name
        self.active_path = Path(active_path)
        self.history_dir = Path(history_dir)
        self._selected_version: int = 0  # current selection in thumb strip
        self._active_version: int = 0    # which is actually active
        self._thumbs: List[VersionThumb] = []
        self.setWindowTitle(
            tr('shot_viewer_title', n=panel_idx + 1))
        self.setModal(False)
        # 2026-05-07: уменьшен min size чтобы помещался на 14" MacBook.
        # Total ≈ header(25) + selected(20) + preview(688) + strip_label(15) +
        # strip(135) + actions(40) + spacings/margins (~70) = ~993 → ставим
        # min 480×780 (preview можно слегка ужать в маленьком окне через
        # AspectRatio scaling).
        # min width = sum minimumWidth кнопок (150+170+180+stretch+110) +
        # margins/spacings. Получаем ~700px минимум.
        self.setMinimumSize(720, 800)
        self.resize(740, 900)
        self._build()
        self.refresh()

    def _build(self):
        self.setStyleSheet(
            "QDialog { background:#0f0a18; }"
            "QLabel#header { color:#fff; font-size:14px; font-weight:600; }"
            "QLabel#hint { color:#888; font-size:11px; }"
            "QLabel#empty { color:#6a6a8a; font-style:italic; font-size:12px; }"
            "QPushButton#action { background:#2a1d4a; border:1px solid #4a3470;"
            " border-radius:6px; color:#fff; font-size:13px; padding:6px 14px;"
            " min-height:30px; }"
            "QPushButton#action:hover { background:#3a2a60;"
            " border-color:#6a4ea0; }"
            "QPushButton#action:disabled { background:#1a1330; color:#666;"
            " border-color:#322545; }"
            "QPushButton#primary { background:#6e4cc4; border:1px solid #6e4cc4;"
            " color:#fff; font-weight:600; }"
            "QPushButton#primary:hover { background:#7d5bd4; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        # Header row: «SHOT N» + selected_lbl справа.
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header = QLabel(tr('shot_viewer_header', n=self.panel_idx + 1))
        header.setObjectName("header")
        header_row.addWidget(header)
        # 2026-05-07 (UI fix): selected_lbl переехал из-под превью в шапку,
        # чтобы не налезать визуально на картинку.
        self.selected_lbl = QLabel("")
        self.selected_lbl.setObjectName("hint")
        self.selected_lbl.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.selected_lbl, stretch=1)
        lay.addLayout(header_row)

        # Большая картинка — обернём в QScrollArea чтобы при сжатом окне
        # юзер мог проскроллить, а не ужималась через AspectRatio scaling.
        # При normal-size окне scrollbar не появится (preview помещается).
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        preview_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        preview_scroll.setStyleSheet("QScrollArea { border:none; }")
        preview_holder = QWidget()
        ph = QVBoxLayout(preview_holder)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(0)
        self.preview_lbl = QLabel()
        self.preview_lbl.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_lbl.setStyleSheet(
            "background:#1a1424; border:1px solid #322545; border-radius:8px;")
        ph.addWidget(self.preview_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
        preview_scroll.setWidget(preview_holder)
        lay.addWidget(preview_scroll, stretch=1)

        # Лента миниатюр (горизонтальный scroll)
        strip_label = QLabel(tr('shot_viewer_versions_label'))
        strip_label.setObjectName("hint")
        lay.addWidget(strip_label)

        self.strip_scroll = QScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        self.strip_scroll.setFixedHeight(THUMB_H + 50)
        self.strip_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.strip_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip_scroll.setStyleSheet(
            "QScrollArea { border:1px solid #25193a; border-radius:6px;"
            " background:#0a0612; }")
        strip_container = QWidget()
        self.strip_layout = QHBoxLayout(strip_container)
        self.strip_layout.setContentsMargins(8, 6, 8, 6)
        self.strip_layout.setSpacing(8)
        self.strip_layout.addStretch()
        self.strip_scroll.setWidget(strip_container)
        lay.addWidget(self.strip_scroll)

        self.empty_versions_lbl = QLabel(tr('shot_viewer_no_versions'))
        self.empty_versions_lbl.setObjectName("empty")
        self.empty_versions_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_versions_lbl.hide()
        lay.addWidget(self.empty_versions_lbl)

        # Действия
        actions = QHBoxLayout()
        actions.setSpacing(10)

        # 2026-05-07 (UI fix): кнопки имеют sizeHint по тексту, и при
        # длинных русских надписях («Перегенерировать», «Использовать эту»)
        # обрезаются в узком окне. Ставим minimumWidth по чуть-чуть
        # больше fontMetrics, плюс делаем общий min width диалога
        # достаточным чтобы помещались все 4 кнопки в одну строку.
        self.btn_edit = QPushButton(tr('shot_viewer_btn_edit'))
        self.btn_edit.setObjectName("action")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_edit.setMinimumWidth(150)
        self.btn_edit.clicked.connect(
            lambda: self.edit_requested.emit(self.panel_idx))
        actions.addWidget(self.btn_edit)

        self.btn_regen = QPushButton(tr('shot_viewer_btn_regen'))
        self.btn_regen.setObjectName("action")
        self.btn_regen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_regen.setMinimumWidth(170)
        self.btn_regen.clicked.connect(
            lambda: self.regen_requested.emit(self.panel_idx))
        actions.addWidget(self.btn_regen)

        self.btn_use = QPushButton(tr('shot_viewer_btn_use'))
        self.btn_use.setObjectName("action")
        self.btn_use.setProperty("primary", True)
        self.btn_use.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_use.setMinimumWidth(180)
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self._on_use_clicked)
        actions.addWidget(self.btn_use)

        actions.addStretch()

        self.btn_close = QPushButton(tr('shot_viewer_btn_close'))
        self.btn_close.setObjectName("action")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setMinimumWidth(110)
        self.btn_close.clicked.connect(self.close)
        actions.addWidget(self.btn_close)

        lay.addLayout(actions)

    def _on_use_clicked(self):
        if self._selected_version <= 0:
            return
        if self._selected_version == self._active_version:
            return
        self.version_use_requested.emit(self.panel_idx, self._selected_version)

    def _show_preview(self, image_path: Path):
        """Загружает картинку в большое превью."""
        try:
            pix = QPixmap(str(image_path))
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(PREVIEW_W, PREVIEW_H),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.preview_lbl.setPixmap(pix)
                return
        except Exception:
            pass
        self.preview_lbl.clear()
        self.preview_lbl.setText(tr('shot_viewer_no_image'))
        self.preview_lbl.setStyleSheet(
            "background:#1a1424; border:1px solid #322545; border-radius:8px;"
            " color:#666; font-size:13px;")

    def _on_thumb_clicked(self, version_n: int):
        self._selected_version = version_n
        # Update visual selection
        for thumb in self._thumbs:
            thumb.set_selected(thumb.version_n == version_n)
        # Show preview of selected
        version_path = self.history_dir / f"v{version_n}.jpg"
        if version_path.exists():
            self._show_preview(version_path)
        # Update label
        is_active = (version_n == self._active_version)
        if is_active:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_active', n=version_n))
        else:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_other', n=version_n))
        self.btn_use.setEnabled(not is_active and version_n > 0)

    def refresh(self):
        """Перерисовка списка версий и превью. Зовётся:
          • при создании,
          • после регенерации (MW.refresh_open_shot_viewer),
          • после клика «Использовать эту» (refresh active),
          • после миграции v1 при первом открытии."""
        # 1) Если history пустой, но active_path существует — мигрируем
        #    в v1 чтобы юзер сразу видел текущий шот в ленте версий.
        if (not self.history_dir.exists()
                or not _has_any_versions(self.history_dir)):
            if self.active_path.exists():
                try:
                    self.history_dir.mkdir(parents=True, exist_ok=True)
                    v1 = self.history_dir / "v1.jpg"
                    if not v1.exists():
                        shutil.copy2(str(self.active_path), str(v1))
                    # Импорт _sa здесь чтобы не было циркуляра.
                    from storyboard_app import set_active_version
                    set_active_version(self.history_dir, 1)
                except Exception:
                    pass

        # 2) Загрузить список версий + active.
        from storyboard_app import list_shot_versions, read_active_version
        versions = list_shot_versions(self.history_dir)
        self._active_version = read_active_version(self.history_dir)
        # Если active не в списке (например 0) — берём максимальный v.
        if not any(p.stem == f"v{self._active_version}" for p in versions):
            if versions:
                # max N
                self._active_version = max(
                    int(p.stem[1:]) for p in versions
                    if p.stem.startswith("v") and p.stem[1:].isdigit())
            else:
                self._active_version = 0

        # 3) Очистить strip layout от старых thumbs (кроме stretch).
        for thumb in self._thumbs:
            try:
                thumb.setParent(None)
                thumb.deleteLater()
            except Exception:
                pass
        self._thumbs.clear()

        # 4) Отрисовать новые thumbs.
        if not versions:
            self.empty_versions_lbl.show()
            self.strip_scroll.hide()
            self._show_preview(self.active_path if self.active_path.exists()
                                else Path("/nonexistent"))
            self.selected_lbl.setText(tr('shot_viewer_no_versions'))
            self.btn_use.setEnabled(False)
            return

        self.empty_versions_lbl.hide()
        self.strip_scroll.show()
        for p in versions:
            try:
                n = int(p.stem[1:])
            except (ValueError, IndexError):
                continue
            is_active = (n == self._active_version)
            thumb = VersionThumb(n, p, is_active)
            thumb.clicked.connect(self._on_thumb_clicked)
            self.strip_layout.insertWidget(
                self.strip_layout.count() - 1, thumb)
            self._thumbs.append(thumb)

        # 5) Selected = active (по умолчанию).
        self._selected_version = self._active_version
        for thumb in self._thumbs:
            thumb.set_selected(thumb.version_n == self._active_version)
        # Превью активной.
        active_path_in_history = (
            self.history_dir / f"v{self._active_version}.jpg")
        if active_path_in_history.exists():
            self._show_preview(active_path_in_history)
        elif self.active_path.exists():
            self._show_preview(self.active_path)

        if self._active_version > 0:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_active', n=self._active_version))
        else:
            self.selected_lbl.setText("")
        self.btn_use.setEnabled(False)

    def apply_lang(self):
        self.setWindowTitle(tr('shot_viewer_title', n=self.panel_idx + 1))


def _has_any_versions(history_dir: Path) -> bool:
    """True если в каталоге есть хоть один файл vN.jpg."""
    if not history_dir.exists() or not history_dir.is_dir():
        return False
    for p in history_dir.iterdir():
        if p.is_file() and p.name.startswith("v") and "." in p.name:
            try:
                int(p.name.split(".", 1)[0][1:])
                return True
            except ValueError:
                continue
    return False
