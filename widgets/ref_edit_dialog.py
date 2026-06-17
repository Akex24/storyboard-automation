# -*- coding: utf-8 -*-
"""
widgets/ref_edit_dialog.py — расширенный диалог редактирования рефа
(локация / объект).

Слева — исходник (read-only, будет заменён результатом). Справа —
опциональный слот «образец стиля»: клик открывает внутренний picker с
сеткой рефов текущего эпизода (того же kind, без самого исходника). Снизу —
поле инструкции и кнопка «Перегенерировать» (активна только когда есть текст).

После accept():
  • self.instruction    : str            — текст инструкции пользователя
  • self.reference_path : Optional[Path] — путь к выбранному эталону
                                           (None если образец не выбран)

Стиль/палитра согласованы с widgets/ref_picker_dialog.py (LUMZ: фон
#0e0a18, accent #e4344a). Создано 2026-06-17 (Коммит A). Сам по себе
никем не вызывается — подключение к кнопке edit ✏️ делается отдельно
(Коммит C).
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


def _scaled_pixmap(path: Path, side: int) -> Optional[QPixmap]:
    """Лениво грузит + масштабирует превью. None если файл не читается."""
    try:
        pm = QPixmap(str(path))
        if pm.isNull():
            return None
        return pm.scaled(
            QSize(side, side),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    except Exception:
        return None


_DIALOG_QSS = (
    "QDialog#ref-edit-dialog { background:#0e0a18; }"
    "QLabel#section-label { color:rgba(255,255,255,0.55);"
    " font-size:11px; font-weight:700; letter-spacing:1px; }"
    "QLabel#dim-label { color:rgba(255,255,255,0.32);"
    " font-size:11px; font-weight:600; letter-spacing:1px; }"
    "QLabel#ref-name { color:#ffffff; font-size:15px; font-weight:600; }"
    "QLabel#ref-tag { color:rgba(255,255,255,0.45); font-size:12px; }"
    "QLabel#hint-tertiary { color:rgba(255,255,255,0.38); font-size:11px; }"
    "QFrame#img-slot { background:#0e0a16;"
    " border:1px solid rgba(255,255,255,0.06); border-radius:8px; }"
    "QFrame#style-slot { background:rgba(255,255,255,0.02);"
    " border:1px dashed rgba(255,255,255,0.15); border-radius:8px; }"
    "QFrame#style-slot:hover { border-color:rgba(228,52,74,0.40); }"
    "QLabel#plus-circle { background:rgba(228,52,74,0.15); color:#e4344a;"
    " border-radius:16px; font-size:20px; font-weight:600; }"
    "QLabel#slot-empty-text { color:rgba(255,255,255,0.70); font-size:12px; }"
    "QLabel#slot-empty-sub { color:rgba(255,255,255,0.38); font-size:11px; }"
    "QPushButton#slot-clear { background:rgba(14,10,22,0.85); color:#fff;"
    " border:1px solid rgba(255,255,255,0.20); border-radius:11px;"
    " font-size:12px; }"
    "QPushButton#slot-clear:hover { background:#e4344a; border-color:#e4344a; }"
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
    """Внутренний picker эталона стиля. Сетка рефов (уже отфильтрованных
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


class _ImageSlot(QFrame):
    """Правый слот «образец стиля». Тело кликабельно (открывает picker);
    крестик-сброс в правом верхнем углу позиционируется в resizeEvent."""

    def __init__(self, height: int, on_body_click, on_clear, parent=None):
        super().__init__(parent)
        self.setObjectName("style-slot")
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on_body_click = on_body_click

        self.clear_btn = QPushButton("✕", self)
        self.clear_btn.setObjectName("slot-clear")
        self.clear_btn.setFixedSize(22, 22)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(on_clear)
        self.clear_btn.hide()

    def resizeEvent(self, ev):
        m = 6
        self.clear_btn.move(self.width() - self.clear_btn.width() - m, m)
        self.clear_btn.raise_()
        super().resizeEvent(ev)

    def mousePressEvent(self, ev):
        # Крестик — отдельная QPushButton (поглощает свой клик), сюда не доходит.
        if ev.button() == Qt.MouseButton.LeftButton and self._on_body_click:
            try:
                self._on_body_click()
            except Exception:
                pass
        super().mousePressEvent(ev)


class RefEditDialog(QDialog):
    """Расширенный диалог редактирования рефа локации/объекта.

    Результат после accept(): self.instruction (str) + self.reference_path
    (Optional[Path]). Замена картинки/перегенерация выполняется вызывающим
    кодом (Коммит C) — диалог только собирает ввод пользователя.
    """

    PREVIEW_H = 180

    def __init__(self, source_image_path: Path, episode_refs: List[Dict],
                 kind: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.source_image_path = Path(source_image_path)
        self._episode_refs: List[Dict] = list(episode_refs or [])
        self._kind = kind

        # Результат диалога.
        self.instruction: str = ""
        self.reference_path: Optional[Path] = None
        # Рабочее состояние выбранного образца (до accept).
        self._style_path: Optional[Path] = None

        self.setObjectName("ref-edit-dialog")
        self.setWindowTitle(tr('ref_edit_dialog_header'))
        self.setMinimumWidth(600)
        self.setStyleSheet(_DIALOG_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(0)

        outer.addLayout(self._build_header())
        outer.addSpacing(18)
        outer.addLayout(self._build_columns())
        outer.addSpacing(20)
        outer.addLayout(self._build_instruction())
        outer.addSpacing(20)
        outer.addLayout(self._build_buttons())

        self._render_style_slot()
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

    # ── columns ──────────────────────────────────────────────────────────
    def _build_columns(self) -> QHBoxLayout:
        cols = QHBoxLayout()
        cols.setSpacing(16)
        cols.addLayout(self._build_source_column(), stretch=1)
        cols.addLayout(self._build_style_column(), stretch=1)
        return cols

    def _build_source_column(self) -> QVBoxLayout:
        c = QVBoxLayout()
        c.setSpacing(8)
        lab = QLabel(tr('ref_edit_dialog_source_label').upper())
        lab.setObjectName("section-label")
        c.addWidget(lab)

        frame = QFrame()
        frame.setObjectName("img-slot")
        frame.setFixedHeight(self.PREVIEW_H)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        img = QLabel()
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = _scaled_pixmap(self.source_image_path, self.PREVIEW_H - 12)
        if pm is not None:
            img.setPixmap(pm)
        else:
            img.setText("—")
            img.setStyleSheet("color:rgba(255,255,255,0.35); font-size:14px;")
        fl.addWidget(img)
        c.addWidget(frame)

        hint = QLabel(tr('ref_edit_dialog_source_hint'))
        hint.setObjectName("hint-tertiary")
        hint.setWordWrap(True)
        c.addWidget(hint)
        return c

    def _build_style_column(self) -> QVBoxLayout:
        c = QVBoxLayout()
        c.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        lab = QLabel(tr('ref_edit_dialog_style_label').upper())
        lab.setObjectName("section-label")
        head.addWidget(lab)
        opt = QLabel(tr('ref_edit_dialog_style_optional'))
        opt.setObjectName("dim-label")
        head.addWidget(opt)
        head.addStretch()
        c.addLayout(head)

        self._style_slot = _ImageSlot(
            self.PREVIEW_H, self._open_style_picker, self._clear_style)
        c.addWidget(self._style_slot)
        c.addStretch()
        return c

    def _render_style_slot(self):
        """Перерисовывает правый слот под текущее self._style_path."""
        slot = self._style_slot
        lay = slot.layout()
        if lay is None:
            lay = QVBoxLayout(slot)
            lay.setContentsMargins(10, 10, 10, 10)
            lay.setSpacing(6)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            self._clear_layout(lay)

        if self._style_path is None:
            slot.clear_btn.hide()
            plus = QLabel("＋")  # fullwidth plus, центр круга
            plus.setObjectName("plus-circle")
            plus.setFixedSize(32, 32)
            plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(plus, alignment=Qt.AlignmentFlag.AlignCenter)
            t = QLabel(tr('ref_edit_dialog_style_empty'))
            t.setObjectName("slot-empty-text")
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setWordWrap(True)
            lay.addWidget(t)
            sub = QLabel(tr('ref_edit_dialog_style_empty_hint'))
            sub.setObjectName("slot-empty-sub")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(sub)
        else:
            img = QLabel()
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = _scaled_pixmap(self._style_path, self.PREVIEW_H - 24)
            if pm is not None:
                img.setPixmap(pm)
            else:
                img.setText("?")
            lay.addWidget(img)
            slot.clear_btn.show()
            slot.clear_btn.raise_()

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

    # ── style picker ─────────────────────────────────────────────────────
    def _open_style_picker(self):
        dlg = _StylePickerDialog(self._style_candidates(), parent=self)
        if (dlg.exec() == QDialog.DialogCode.Accepted
                and dlg.selected_path is not None):
            self._style_path = dlg.selected_path
            self._render_style_slot()

    def _style_candidates(self) -> List[Path]:
        """Рефы эпизода для picker'а: того же kind, без самого исходника,
        без дублей по пути. Фильтрация по kind дублирует ту, что может
        сделать вызывающий код — двойная защита (см. ТЗ Коммита A)."""
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

    def _clear_style(self):
        self._style_path = None
        self._render_style_slot()

    # ── helpers ──────────────────────────────────────────────────────────
    def _sync_regen_enabled(self):
        try:
            txt = self._instruction_edit.toPlainText().strip()
        except Exception:
            txt = ""
        self._regen_btn.setEnabled(bool(txt))

    def _on_regenerate(self):
        self.instruction = self._instruction_edit.toPlainText().strip()
        self.reference_path = self._style_path
        if not self.instruction:
            return
        self.accept()

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
