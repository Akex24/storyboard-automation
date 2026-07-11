# -*- coding: utf-8 -*-
"""generator/favorites_dialog.py — окно «Избранное» (2026-07-05; justified-рерайт 2026-07-11).

Избранные карточки ТЕКУЩЕГО сериала (page._load_favorites()). Переиспользует
ShimmerCell в облегчённом режиме (enable_favorites_lite: heart/ref/reveal; кликом
открывается попап; карточку можно перетащить во внешнюю прогу). Размер/центрирование —
паттерн GeneratorViewerDialog (60% availableGeometry).

Раскладка — JUSTIFIED (как Google Photos): ряды целевой ВЫСОТЫ `_ROW_H`, ширина
карточки = высота * aspect (вертикальные узкие, горизонтальные широкие). Набираем в
ряд пока влезает по ширине, потом масштабируем ряд, чтобы он ТОЧНО занял всю ширину
(последний ряд не растягиваем). Абсолютное позиционирование в _grid_host (QGridLayout
так не умеет). resize окна → пересчёт. Никакой пустоты справа, никаких «лесенок».

Формат карточки — по РЕАЛЬНОМУ файлу через QImageReader.size() (ТОЛЬКО заголовок, без
декода — открытие мгновенное даже на сотнях 4K; image = сам файл, video = .jpg-превью).

Ленивая загрузка: карточки — плейсхолдеры (shimmer погашен), пиксмап грузится ТОЛЬКО
для видимых (geometry во вьюпорте + преднагрузка), догрузка на scroll/показ. Кэш
миниатюр _THUMB_CACHE (path→scaled QPixmap) — повторный показ мгновенно. Fade по готовности.

Кросс-платформенно: Path + QtWidgets/QtGui, без subprocess/shell.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QPixmap, QImageReader
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QScrollArea, QWidget, QLabel,
                             QFrame, QGraphicsOpacityEffect)

from i18n import tr

_ROW_H = 250           # целевая высота ряда (px); ширина карточки = _ROW_H * aspect
_GAP = 12
_THUMB_MAX = 360       # сторона кэш-миниатюры
_PRELOAD = 350         # преднагрузка за краем вьюпорта (px)

# Кэш миниатюр (модульный, живёт между открытиями): path → scaled QPixmap.
_THUMB_CACHE: dict = {}


class FavoritesDialog(QDialog):
    """Окно избранного: justified-сетка ShimmerCell из favorites.json сериала."""

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

        # Заглушка «Пока пусто».
        self._empty_lbl = QLabel(tr('gen_fav_empty'))
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setStyleSheet("color:rgba(255,255,255,0.4); font-size:15px;")
        root.addWidget(self._empty_lbl)

        # Прокручиваемая область. widgetResizable=False + ручной размер хоста (абсолютное
        # позиционирование). Скроллбар AlwaysOn → ширина вьюпорта стабильна (без осцилляции
        # scrollbar↔ширина при пересчёте justified-рядов).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setStyleSheet("background:transparent;")
        self._grid_host = QWidget()      # без layout — позиционируем карточки вручную
        self._scroll.setWidget(self._grid_host)
        root.addWidget(self._scroll, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(self._load_visible)

        w, h = self._target_size()
        self.resize(w, h)
        self._center_on_screen(w, h)

        self._build_cells()

    # ── размер / центрирование ──
    def _target_size(self):
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

    # ── формат по заголовку файла (БЕЗ полного декода) ──
    @staticmethod
    def _aspect_of(real: str, ftype: str) -> str:
        """'16:9'/'9:16' по РЕАЛЬНОМУ файлу через QImageReader.size() — читает ТОЛЬКО
        заголовок (без декода пикселей → быстро на сотнях 4K). image — сам файл; video —
        .jpg-превью рядом. Не прочитать → '16:9'."""
        try:
            probe = real if ftype != "video" else str(Path(real).with_suffix(".jpg"))
            if ftype == "video" and not Path(probe).exists():
                return "16:9"
            r = QImageReader(str(probe))
            sz = r.size()
            if sz.isValid() and sz.width() > 0 and sz.height() > 0:
                return "16:9" if sz.width() >= sz.height() else "9:16"
        except Exception:
            pass
        return "16:9"

    @staticmethod
    def _ar(aspect: str) -> float:
        """Отношение ширина/высота: 16:9 → 16/9 (широкая), 9:16 → 9/16 (узкая)."""
        return 16.0 / 9.0 if aspect == "16:9" else 9.0 / 16.0

    # ── наполнение (ЛЕНИВО: пиксмапы грузим потом) ──
    def _build_cells(self):
        try:
            items = self._page._load_favorites()
        except Exception:
            items = []
        if items:
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
                    continue
                try:
                    aspect = self._aspect_of(real, ftype)
                    cell = ShimmerCell(self._page, width=_ROW_H, height=_ROW_H, aspect=aspect)
                    cell.set_meta(file=fname, type=ftype, aspect=aspect)
                    cell.setParent(self._grid_host)     # абсолютное позиционирование
                    cell._result_path = real            # клик/reveal/сердечко живы ДО пиксмапа
                    cell._fav_real = real
                    cell._fav_type = ftype
                    cell._fav_aspect = aspect
                    cell._fav_loaded = False
                    try:
                        cell._finish_common()           # плейсхолдер не «дышит»
                    except Exception:
                        pass
                    cell.enable_favorites_lite()
                    try:
                        cell._refresh_heart_state()
                    except Exception:
                        pass
                    cell.btn_heart.clicked.connect(
                        lambda _c=False, cc=cell, fn=fname: self._on_cell_heart(cc, fn))
                    self._cells.append(cell)
                except Exception:
                    continue
        self._relayout()

    def _on_cell_heart(self, cell, fname):
        try:
            if not self._page.is_favorite(fname):
                if cell in self._cells:
                    self._cells.remove(cell)
                try:
                    cell._teardown()
                except Exception:
                    pass
                cell.setParent(None)
                cell.deleteLater()
                self._relayout()
        except Exception:
            pass

    # ── JUSTIFIED-раскладка (абсолютное позиционирование) ──
    def _relayout(self):
        cells = [c for c in self._cells if c is not None]
        empty = len(cells) == 0
        self._empty_lbl.setVisible(empty)
        self._scroll.setVisible(not empty)
        if empty:
            return
        try:
            W = self._scroll.viewport().width() - 2 * _GAP
        except Exception:
            W = 900
        W = max(160, W)

        # 1) набрать карточки в ряды по целевой высоте _ROW_H: добавляем карточку и
        # закрываем ряд, КОГДА он набран (натуральная ширина+зазоры >= W). Так ряд всегда
        # чуть переполнен → масштаб ≤1 (высота ≤ target), без гигантских одиночных рядов.
        rows, cur, cur_w = [], [], 0.0
        for c in cells:
            cur.append(c)
            cur_w += _ROW_H * self._ar(getattr(c, "_fav_aspect", "16:9"))
            if cur_w + _GAP * (len(cur) - 1) >= W:
                rows.append(cur)
                cur, cur_w = [], 0.0
        if cur:
            rows.append(cur)

        # 2) выложить ряды, масштабируя на ЗАПОЛНЕНИЕ ширины (кроме последнего, если не влез)
        y = _GAP
        right = _GAP + W
        for ri, row in enumerate(rows):
            n = len(row)
            nat = sum(_ROW_H * self._ar(getattr(c, "_fav_aspect", "16:9")) for c in row)
            avail = W - _GAP * (n - 1)
            scale = (avail / nat) if nat > 0 else 1.0
            if ri == len(rows) - 1:
                scale = min(scale, 1.0)     # последний ряд не растягиваем
            rh = max(90, int(round(_ROW_H * scale)))
            x = _GAP
            for j, c in enumerate(row):
                if j == n - 1 and ri != len(rows) - 1:
                    cw = max(40, right - x)         # последняя в ряду добирает до края (без щели)
                else:
                    cw = max(40, int(round(rh * self._ar(getattr(c, "_fav_aspect", "16:9")))))
                try:
                    c.set_size(cw, rh)
                    c.move(x, y)
                    c.show()
                except Exception:
                    pass
                x += cw + _GAP
            y += rh + _GAP

        try:
            self._grid_host.resize(self._scroll.viewport().width(), y)
        except Exception:
            pass
        QTimer.singleShot(0, self._load_visible)

    # ── ленивая подгрузка превью + кэш ──
    def _load_visible(self):
        if not self._cells:
            return
        try:
            top = self._scroll.verticalScrollBar().value() - _PRELOAD
            bot = top + self._scroll.viewport().height() + 2 * _PRELOAD
        except Exception:
            return
        for c in self._cells:
            if c is None or getattr(c, "_fav_loaded", True):
                continue
            try:
                cy, ch = c.y(), c.height()
            except Exception:
                continue
            if cy + ch >= top and cy <= bot:
                self._load_cell(c)

    def _load_cell(self, cell):
        cell._fav_loaded = True
        real, ftype = cell._fav_real, cell._fav_type
        key = real if ftype != "video" else str(Path(real).with_suffix(".jpg"))
        cached = _THUMB_CACHE.get(key)
        try:
            if ftype == "video":
                cell.set_video_placeholder(real, cached)
            else:
                cell.set_image(real, cached)
        except Exception:
            return
        if cached is None:
            pm = getattr(cell, "_original_pix", None)
            if pm is not None and not pm.isNull():
                _THUMB_CACHE[key] = pm.scaled(_THUMB_MAX, _THUMB_MAX,
                                              Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
        self._fade_in(cell)

    def _fade_in(self, cell):
        """Плавное появление превью при ПЕРВОМ показе (opacity 0→1, 450мс). Once-guard
        `_fav_faded`: повторная прокрутка на уже показанную карточку НЕ мигает (эффект
        не навешивается снова). placeholder (тёмная плитка нужной пропорции) виден, пока
        превью грузится → затем оно проявляется поверх."""
        if getattr(cell, "_fav_faded", False):
            return
        cell._fav_faded = True
        try:
            eff = QGraphicsOpacityEffect(cell)
            cell.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", cell)
            anim.setDuration(450)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda cc=cell: self._clear_effect(cc))
            cell._fav_fade = anim
            anim.start()
        except Exception:
            pass

    @staticmethod
    def _clear_effect(cell):
        try:
            cell.setGraphicsEffect(None)
        except Exception:
            pass

    def showEvent(self, ev):
        super().showEvent(ev)
        self._relayout()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._relayout()
