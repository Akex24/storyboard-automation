# -*- coding: utf-8 -*-
"""generator/favorites_dialog.py — окно «Избранное» (2026-07-05).

Адаптивная сетка избранных карточек ТЕКУЩЕГО сериала (page._load_favorites()).
Переиспользует ShimmerCell в облегчённом режиме (enable_favorites_lite: остаются
heart/ref/reveal, генеративные кнопки скрыты). Размер/центрирование — по паттерну
GeneratorViewerDialog (60% availableGeometry, центр по self.screen()), адаптив
14"→5K без фикс-геометрии окна. Ссылку на диалог держит page (анти-GC, как _open_viewer).

Кросс-платформенно: Path + QtWidgets, без subprocess/shell. Импорт generator.result_cell —
ЛЕНИВЫЙ внутри _build_cells (circular-import guard: result_cell тянет тему/иконки).
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QGridLayout, QScrollArea,
                             QWidget, QLabel, QFrame)

from i18n import tr

_CELL_W = 260          # ширина единой миниатюры (16:9)
_CELL_H = 146          # ≈ 260 * 9 / 16
_GAP = 12


class FavoritesDialog(QDialog):
    """Окно избранного: адаптивная сетка ShimmerCell из favorites.json сериала."""

    def __init__(self, page, parent=None):
        super().__init__(parent or page, Qt.WindowType.Window)
        self._page = page
        self._cells = []
        self.setWindowTitle(tr('gen_fav_title'))
        try:
            from views.theme import LUMZ_THEME
            self.setStyleSheet(f"QDialog {{ background:{LUMZ_THEME['bg_main']}; }}")
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Заглушка «Пока пусто» (по центру, приглушённый текст темы).
        self._empty_lbl = QLabel(tr('gen_fav_empty'))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet("color:rgba(255,255,255,0.4); font-size:15px;")
        root.addWidget(self._empty_lbl)

        # Прокручиваемая сетка карточек.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().setStyleSheet("background:transparent;")
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(_GAP, _GAP, _GAP, _GAP)
        self._grid.setSpacing(_GAP)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll.setWidget(self._grid_host)
        root.addWidget(self._scroll, 1)

        w, h = self._target_size()
        self.resize(w, h)
        self._center_on_screen(w, h)

        self._build_cells()

    # ── размер / центрирование (паттерн GeneratorViewerDialog._target_size) ──
    def _target_size(self):
        """Крупное окно, вписанное в доступную область экрана (адаптив 14"→5K)."""
        max_w, max_h = 1100, 720
        try:
            from PyQt6.QtWidgets import QApplication
            avail = QApplication.primaryScreen().availableGeometry()
            max_w = int(avail.width() * 0.6)
            max_h = int(avail.height() * 0.6)
        except Exception:
            pass
        return max(640, max_w), max(420, max_h)

    def _center_on_screen(self, w, h):
        try:
            from PyQt6.QtWidgets import QApplication
            scr = self.screen() or QApplication.primaryScreen()
            avail = scr.availableGeometry()
            self.move(avail.x() + (avail.width() - w) // 2,
                      avail.y() + (avail.height() - h) // 2)
        except Exception:
            pass

    # ── наполнение сетки из favorites.json ──
    def _build_cells(self):
        try:
            items = self._page._load_favorites()
        except Exception:
            items = []
        if items:
            # ЛЕНИВО: тяжёлые импорты (result_cell тянет тему/иконки) только когда есть
            # что показывать. Пустое окно → лишь заглушка, без импортов.
            from generator.result_cell import ShimmerCell, resolve_existing_path
            try:
                import storyboard_app as _sa
                root = _sa.get_stored_root()
                slug = _sa.get_current_show(root) if root else None
            except Exception:
                root = slug = None
            for it in items:
                fname = (it.get("file") or "").strip() if isinstance(it, dict) else ""
                ftype = (it.get("type") or "image") if isinstance(it, dict) else "image"
                if not fname or not (root and slug):
                    continue
                full = root / "shows" / slug / "generator" / fname
                real = resolve_existing_path(str(full))
                if not real:
                    continue   # файл не найден → скипаем молча
                try:
                    cell = ShimmerCell(self._page, width=_CELL_W, height=_CELL_H, aspect="16:9")
                    cell.set_meta(file=fname, type=ftype, aspect="16:9")
                    if ftype == "video":
                        cell.set_video_placeholder(real)
                    else:
                        cell.set_image(real)
                    cell.enable_favorites_lite()   # только heart/ref/reveal
                    # клик по сердечку → снять из избранного + убрать карточку из сетки сразу
                    cell.btn_heart.clicked.connect(
                        lambda _c=False, cc=cell, fn=fname: self._on_cell_heart(cc, fn))
                    self._cells.append(cell)
                except Exception:
                    continue
        self._relayout()

    def _on_cell_heart(self, cell, fname):
        """После клика по сердечку (cell._on_heart_clicked уже сделал toggle): если
        файл больше НЕ в избранном — убрать карточку из сетки немедленно."""
        try:
            if not self._page.is_favorite(fname):
                if cell in self._cells:
                    self._cells.remove(cell)
                cell.setParent(None)
                cell.deleteLater()
                self._relayout()
        except Exception:
            pass

    def _relayout(self):
        """Переложить карточки в грид по текущей ширине окна. Пустая сетка → заглушка."""
        while self._grid.count():
            self._grid.takeAt(0)
        cells = [c for c in self._cells if c is not None]
        empty = len(cells) == 0
        self._empty_lbl.setVisible(empty)
        self._scroll.setVisible(not empty)
        if empty:
            return
        cols = self._columns()
        for i, c in enumerate(cells):
            self._grid.addWidget(c, i // cols, i % cols,
                                 Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    def _columns(self) -> int:
        try:
            avail = self._scroll.viewport().width() - 2 * _GAP
            return max(1, (avail + _GAP) // (_CELL_W + _GAP))
        except Exception:
            return 3

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._relayout()
