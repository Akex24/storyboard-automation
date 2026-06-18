# -*- coding: utf-8 -*-
"""
widgets/ref_edit_dialog.py — диалог редактирования рефа (локация / объект).

Один слот картинки. По умолчанию в слоте стоит сам редактируемый реф; юзер
может ЗАМЕНИТЬ его на другой реф эпизода (клик по картинке или по плюсику в
правом верхнем углу → внутренний picker). Кнопки «вернуть исходник» нет —
закрыть окно и открыть edit заново. Снизу — поле инструкции и кнопка
«Перегенерировать» (активна только когда есть текст).

После accept():
  • self.instruction    : str            — текст инструкции пользователя
  • self.reference_path : Optional[Path] — путь к ВЫБРАННОЙ картинке, если
                                           юзер заменил исходник; иначе None
                                           (в FastGen уйдёт сам редактируемый реф)

Стиль/палитра согласованы с widgets/ref_picker_dialog.py (LUMZ: фон
#0e0a18, accent #e4344a). Создано 2026-06-17 (Коммит A), переделано под
один слот (Коммит A2). Подключение к кнопке edit ✏️ — отдельно (Коммит C).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QWidget, QFrame,
)

from i18n import tr


_IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}

# kind → имя папки в пути рефа (для фильтрации кандидатов picker'а).
_KIND_DIR_TOKEN = {'location': 'locations', 'object': 'objects'}


def _pretty_stem(stem: str) -> str:
    """Человекочитаемое имя из stem файла (как list_episode_refs._pretty_stem)."""
    return stem.replace('_', ' ').strip().capitalize()


def _scaled_pixmap(path: Path, width: int,
                   height: Optional[int] = None) -> Optional[QPixmap]:
    """Лениво грузит + масштабирует превью под бокс (width × height|width),
    KeepAspectRatio. None если файл не читается."""
    try:
        pm = QPixmap(str(path))
        if pm.isNull():
            return None
        box = QSize(width, width if height is None else height)
        return pm.scaled(
            box,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    except Exception:
        return None


_DIALOG_QSS = (
    "QDialog#ref-edit-dialog { background:#0e0a18; }"
    "QLabel#section-label { color:rgba(255,255,255,0.55);"
    " font-size:11px; font-weight:700; letter-spacing:1px; }"
    "QLabel#ref-name { color:#ffffff; font-size:15px; font-weight:600; }"
    "QLabel#ref-tag { color:rgba(255,255,255,0.45); font-size:12px; }"
    "QLabel#hint-tertiary { color:rgba(255,255,255,0.38); font-size:11px; }"
    "QFrame#img-slot { background:#0e0a16;"
    " border:1px solid rgba(255,255,255,0.06); border-radius:8px; }"
    "QFrame#img-slot:hover { border-color:rgba(228,52,74,0.40); }"
    "QPushButton#slot-replace { background:#e4344a; color:#ffffff;"
    " border:none; border-radius:8px; font-size:22px; font-weight:700; }"
    "QPushButton#slot-replace:hover { background:#d92d44; }"
    "QPlainTextEdit#instruction-edit { background:#15101e;"
    " border:1px solid #2c2240; border-radius:6px; color:#ddd;"
    " padding:8px; font-size:13px; }"
    "QPushButton#crest-close { background:transparent;"
    " color:rgba(255,255,255,0.45); border:none; font-size:16px; }"
    "QPushButton#crest-close:hover { color:#ffffff; }"
    "QPushButton#btn-secondary { background:transparent;"
    " color:rgba(255,255,255,0.60); border:1px solid rgba(255,255,255,0.14);"
    " border-radius:8px; padding:8px 18px; font-size:12px; }"
    "QPushButton#btn-secondary:hover { background:rgba(255,255,255,0.06);"
    " color:#ffffff; border-color:rgba(255,255,255,0.22); }"
    "QPushButton#btn-primary { background:#e4344a; color:#ffffff;"
    " border:none; border-radius:8px; padding:8px 18px; font-size:12px;"
    " font-weight:600; }"
    "QPushButton#btn-primary:hover { background:#d92d44; }"
    "QPushButton#btn-primary:pressed { background:#c4283c; }"
    "QPushButton#btn-primary:disabled { background:rgba(228,52,74,0.25);"
    " color:rgba(255,255,255,0.40); }"
)

_PICKER_QSS = (
    "QDialog { background:#0e0a18; }"
    "QLabel#picker-header { color:#ffffff; font-size:14px; font-weight:600; }"
    "QLabel#empty-msg { color:rgba(255,255,255,0.55); font-size:13px;"
    " padding:40px; }"
    "QLabel#thumb-name { color:rgba(255,255,255,0.70); font-size:11px; }"
    "QFrame#style-thumb { background:rgba(255,255,255,0.04);"
    " border:1px solid rgba(255,255,255,0.06); border-radius:8px; }"
    "QFrame#style-thumb:hover { border-color:rgba(228,52,74,0.40); }"
    "QPushButton#close-btn { background:transparent;"
    " color:rgba(255,255,255,0.55); border:1px solid rgba(255,255,255,0.12);"
    " border-radius:8px; padding:6px 18px; font-size:12px; }"
    "QPushButton#close-btn:hover { background:rgba(255,255,255,0.06);"
    " color:#ffffff; border-color:rgba(255,255,255,0.20); }"
)


class _StyleThumb(QFrame):
    """Карточка превью в picker'е. Клик по карточке = выбор без подтверждения."""

    def __init__(self, file_path: Path, on_pick, parent=None):
        super().__init__(parent)
        self._file_path = file_path
        self._on_pick = on_pick
        self.setObjectName("style-thumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        img = QLabel()
        img.setFixedSize(160, 160)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img.setStyleSheet("background:#0e0a16; border-radius:4px;")
        pm = _scaled_pixmap(file_path, 160)
        if pm is not None:
            img.setPixmap(pm)
        else:
            img.setText("?")
        lay.addWidget(img, alignment=Qt.AlignmentFlag.AlignCenter)

        name = QLabel(file_path.name)
        name.setObjectName("thumb-name")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setWordWrap(True)
        lay.addWidget(name)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            try:
                self._on_pick(self._file_path)
            except Exception:
                pass
        super().mousePressEvent(ev)


class _StylePickerDialog(QDialog):
    """Внутренний picker картинки замены. Сетка рефов (уже отфильтрованных
    вызывающим RefEditDialog). Клик по карточке → selected_path + accept."""

    def __init__(self, candidates: List[Path], parent=None, columns: int = 3):
        super().__init__(parent)
        self.selected_path: Optional[Path] = None
        self._columns = max(1, columns)
        self.setWindowTitle(tr('ref_edit_dialog_picker_title'))
        self.setMinimumSize(620, 520)
        self.setStyleSheet(_PICKER_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        header = QLabel(tr('ref_edit_dialog_picker_title'))
        header.setObjectName("picker-header")
        outer.addWidget(header)

        if not candidates:
            empty = QLabel(tr('ref_edit_dialog_picker_empty'))
            empty.setObjectName("empty-msg")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            outer.addWidget(empty, stretch=1)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            host = QWidget()
            grid = QGridLayout(host)
            grid.setSpacing(12)
            grid.setContentsMargins(4, 4, 4, 4)
            for i, fp in enumerate(candidates):
                row, col = divmod(i, self._columns)
                grid.addWidget(_StyleThumb(fp, self._pick), row, col)
            grid.setColumnStretch(self._columns, 1)
            scroll.setWidget(host)
            outer.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(tr('ref_edit_dialog_picker_close'))
        close_btn.setObjectName("close-btn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _pick(self, file_path: Path):
        self.selected_path = file_path
        self.accept()


class _ReplaceableImageSlot(QFrame):
    """Слот картинки для генерации. По умолчанию — редактируемый реф; клик
    по картинке ИЛИ по плюсику (правый верхний угол, виден постоянно)
    открывает picker замены. Кнопки сброса нет."""

    def __init__(self, width: int, height: int, on_click, parent=None):
        super().__init__(parent)
        self.setObjectName("img-slot")
        self.setFixedSize(width, height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on_click = on_click

        self._img = QLabel(self)
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setStyleSheet(
            "color:rgba(255,255,255,0.35); font-size:14px; background:transparent;")

        # Плюсик-оверлей «заменить картинку» — виден всегда (не на hover).
        self.replace_btn = QPushButton("+", self)
        self.replace_btn.setObjectName("slot-replace")
        self.replace_btn.setFixedSize(36, 36)
        self.replace_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.replace_btn.clicked.connect(self._fire)

    def set_pixmap(self, pm: Optional[QPixmap]):
        if pm is not None:
            self._img.setPixmap(pm)
        else:
            self._img.setText("—")

    def resizeEvent(self, ev):
        m = 6
        self._img.setGeometry(m, m, self.width() - 2 * m, self.height() - 2 * m)
        bm = 8  # отступ плюсика от края слота (картинка остаётся на m=6)
        self.replace_btn.move(self.width() - self.replace_btn.width() - bm, bm)
        self.replace_btn.raise_()
        super().resizeEvent(ev)

    def _fire(self):
        if self._on_click:
            try:
                self._on_click()
            except Exception:
                pass

    def mousePressEvent(self, ev):
        # Плюсик — отдельная QPushButton (поглощает свой клик); картинка
        # (QLabel) клики не ловит → доходят сюда.
        if ev.button() == Qt.MouseButton.LeftButton:
            self._fire()
        super().mousePressEvent(ev)


class RefEditDialog(QDialog):
    """Диалог редактирования рефа локации/объекта (один слот картинки).

    Результат после accept(): self.instruction (str) + self.reference_path
    (Optional[Path] — None если юзер не менял картинку). Замена картинки/
    перегенерация выполняется вызывающим кодом (Коммит C) — диалог только
    собирает ввод пользователя.
    """

    SLOT_W = 360
    SLOT_H = 200

    def __init__(self, source_image_path: Path, episode_refs: List[Dict],
                 kind: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.source_image_path = Path(source_image_path)
        self._episode_refs: List[Dict] = list(episode_refs or [])
        self._kind = kind

        # Результат диалога.
        self.instruction: str = ""
        self.reference_path: Optional[Path] = None
        # Что сейчас в слоте + заменил ли юзер исходник (до accept).
        self._slot_path: Path = self.source_image_path
        self._is_replaced: bool = False

        self.setObjectName("ref-edit-dialog")
        self.setWindowTitle(tr('ref_edit_dialog_header'))
        self.setMinimumWidth(420)
        self.setStyleSheet(_DIALOG_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(0)

        outer.addLayout(self._build_header())
        outer.addSpacing(18)
        outer.addLayout(self._build_image_section())
        outer.addSpacing(20)
        outer.addLayout(self._build_instruction())
        outer.addSpacing(20)
        outer.addLayout(self._build_buttons())

        self._render_image_slot()
        self._sync_regen_enabled()

    # ── header ───────────────────────────────────────────────────────────
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        col = QVBoxLayout()
        col.setSpacing(2)
        lab = QLabel(tr('ref_edit_dialog_header').upper())
        lab.setObjectName("section-label")
        col.addWidget(lab)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name = QLabel(_pretty_stem(self.source_image_path.stem))
        name.setObjectName("ref-name")
        name_row.addWidget(name)
        tag = self._source_tag()
        if tag:
            tag_lbl = QLabel(tag)
            tag_lbl.setObjectName("ref-tag")
            name_row.addWidget(tag_lbl)
        name_row.addStretch()
        col.addLayout(name_row)
        row.addLayout(col, stretch=1)

        crest = QPushButton("✕")
        crest.setObjectName("crest-close")
        crest.setFixedSize(28, 28)
        crest.setCursor(Qt.CursorShape.PointingHandCursor)
        crest.clicked.connect(self.reject)
        row.addWidget(crest, alignment=Qt.AlignmentFlag.AlignTop)
        return row

    def _source_tag(self) -> str:
        """Тег (imgN) исходника — берём из episode_refs по совпадению пути."""
        try:
            src = self.source_image_path.resolve()
        except Exception:
            src = self.source_image_path
        for r in self._episode_refs:
            if not isinstance(r, dict):
                continue
            raw = r.get('path')
            if not raw:
                continue
            try:
                if Path(raw).resolve() == src:
                    return str(r.get('tag') or '')
            except Exception:
                continue
        return ''

    # ── image section (один слот) ────────────────────────────────────────
    def _build_image_section(self) -> QVBoxLayout:
        c = QVBoxLayout()
        c.setSpacing(8)

        self._image_slot = _ReplaceableImageSlot(
            self.SLOT_W, self.SLOT_H, self._open_style_picker)
        # центрируем слот по ширине диалога
        slot_row = QHBoxLayout()
        slot_row.addStretch()
        slot_row.addWidget(self._image_slot)
        slot_row.addStretch()
        c.addLayout(slot_row)

        hint = QLabel(tr('ref_edit_dialog_image_hint'))
        hint.setObjectName("hint-tertiary")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        c.addWidget(hint)
        return c

    def _render_image_slot(self):
        """Рисует превью текущего self._slot_path в слот."""
        pm = _scaled_pixmap(self._slot_path, self.SLOT_W - 12, self.SLOT_H - 12)
        self._image_slot.set_pixmap(pm)

    # ── instruction + buttons ────────────────────────────────────────────
    def _build_instruction(self) -> QVBoxLayout:
        c = QVBoxLayout()
        c.setSpacing(8)
        lab = QLabel(tr('ref_edit_dialog_instruction_label').upper())
        lab.setObjectName("section-label")
        c.addWidget(lab)
        self._instruction_edit = QPlainTextEdit()
        self._instruction_edit.setObjectName("instruction-edit")
        self._instruction_edit.setMinimumHeight(70)
        self._instruction_edit.setPlaceholderText(
            tr('ref_edit_dialog_instruction_placeholder'))
        self._instruction_edit.textChanged.connect(self._sync_regen_enabled)
        c.addWidget(self._instruction_edit)
        return c

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch()
        cancel = QPushButton(tr('ref_edit_dialog_cancel'))
        cancel.setObjectName("btn-secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self._regen_btn = QPushButton(tr('ref_edit_dialog_regenerate'))
        self._regen_btn.setObjectName("btn-primary")
        self._regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._regen_btn.clicked.connect(self._on_regenerate)
        row.addWidget(self._regen_btn)
        return row

    # ── replace picker ───────────────────────────────────────────────────
    def _open_style_picker(self):
        dlg = _StylePickerDialog(self._style_candidates(), parent=self)
        if (dlg.exec() == QDialog.DialogCode.Accepted
                and dlg.selected_path is not None):
            self._slot_path = dlg.selected_path
            self._is_replaced = True
            self._render_image_slot()

    def _style_candidates(self) -> List[Path]:
        """Рефы эпизода для picker'а: того же kind, без самого редактируемого
        рефа, без дублей по пути. Фильтрация по kind дублирует ту, что может
        сделать вызывающий код — двойная защита."""
        try:
            src = self.source_image_path.resolve()
        except Exception:
            src = self.source_image_path
        token = _KIND_DIR_TOKEN.get(self._kind)
        out: List[Path] = []
        seen = set()
        for r in self._episode_refs:
            raw = r.get('path') if isinstance(r, dict) else None
            if not raw:
                continue
            p = Path(raw)
            try:
                rp = p.resolve()
            except Exception:
                rp = p
            if rp == src:
                continue
            if token is not None:
                parts = {part.lower() for part in p.parts}
                if token not in parts:
                    continue
            key = str(rp)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    # ── helpers ──────────────────────────────────────────────────────────
    def _sync_regen_enabled(self):
        try:
            txt = self._instruction_edit.toPlainText().strip()
        except Exception:
            txt = ""
        self._regen_btn.setEnabled(bool(txt))

    def _on_regenerate(self):
        self.instruction = self._instruction_edit.toPlainText().strip()
        # reference_path заполняется ТОЛЬКО если юзер заменил картинку.
        self.reference_path = self._slot_path if self._is_replaced else None
        if not self.instruction:
            return
        self.accept()
