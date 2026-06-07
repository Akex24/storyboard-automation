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

from PyQt6.QtCore import Qt, pyqtSignal, QSize, QEvent, QRectF, QTimer, QPoint, QRect, QPointF
from PyQt6.QtGui import QPixmap, QTransform, QColor, QPainter, QPen, QPolygon, QCursor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QSizePolicy, QApplication,
    QStackedWidget, QMessageBox
)

from i18n import tr
from views.theme import lumz_button_qss


# 2026-05-07 (фикс UI): уменьшено чтобы попап влезал на 14" MacBook
# (1512×982 logical = ~900px usable height после menu bar). Раньше было
# 540×960 — попап был 1080+px и не помещался. Теперь preview ≈ исходный
# размер шота 384×688, дополнительно × 1.4 для удобства просмотра.
PREVIEW_W = 384
PREVIEW_H = 688

# Миниатюра в ленте версий — компактнее.
THUMB_W = 70
THUMB_H = 125  # 70 × (688/384) ≈ 125

# Шаг горизонтальной прокрутки ленты версий колесом для классической мыши
# (Windows): angleDelta идёт квантами ~120 на «щелчок». Сколько пикселей
# двигать ленту за один квант. # подобрать на живой Windows-мыши.
_STRIP_WHEEL_STEP_PX = 80

# Толщина маркера в ЛОГИЧЕСКИХ пикселях (device-independent). QPainter рисует в
# логических координатах, Qt сам масштабирует по devicePixelRatio → видимая
# толщина ОДИНАКОВА на Retina (Mac) и обычных экранах (Windows). НЕ умножать на
# dpr вручную. «Средняя» фиксированная толщина (регулировки пока нет).
_MARKER_WIDTH = 3
_MARKER_COLOR = QColor(230, 30, 30)   # фиксированный красный

# Курсор-кисть режима маркера: диаметр круга и толщина контура в ЛОГИЧЕСКИХ px
# (device-independent, как _MARKER_WIDTH — Qt масштабирует по dpr → одинаковый
# видимый размер на Retina/Windows). Центр круга = точка рисования (hotspot).
_MARKER_CURSOR_DIAM = 14
_MARKER_CURSOR_PEN = 2


class VersionThumb(QFrame):
    """Кликабельная миниатюра версии в ленте."""

    clicked = pyqtSignal(int)  # version_n
    delete_requested = pyqtSignal(int)  # version_n — крестик удаления

    def __init__(self, version_n: int, image_path: Path, is_active: bool,
                 can_delete: bool = False, parent=None):
        super().__init__(parent)
        self.version_n = version_n
        self.image_path = image_path
        self._is_active = is_active
        self._is_selected = is_active  # выбран по умолчанию = активный
        self._can_delete = can_delete
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

        # 2026-06-04: крестик удаления версии — overlay в top-right превьюшки
        # (родитель img_lbl, НЕ в layout, thumb фикс-размера → простой move).
        # Виден только если can_delete (не v1, не активная). Клик съедается
        # кнопкой → QFrame.mousePressEvent (выбор версии) не сработает.
        from storyboard_app import get_icon
        self.btn_del = QPushButton(self.img_lbl)
        self.btn_del.setObjectName("thumb-del")
        self.btn_del.setIcon(get_icon('x'))
        self.btn_del.setIconSize(QSize(12, 12))
        self.btn_del.setFixedSize(18, 18)
        self.btn_del.move(THUMB_W - 18 - 2, 2)
        self.btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_del.setStyleSheet(
            "QPushButton#thumb-del { background:rgba(10,6,18,0.7);"
            " border:none; border-radius:9px; }"
            "QPushButton#thumb-del:hover { background:rgba(150,40,40,0.9); }")
        self.btn_del.clicked.connect(
            lambda: self.delete_requested.emit(self.version_n))
        self.btn_del.setVisible(self._can_delete)
        self.btn_del.raise_()

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

    def set_deletable(self, ok: bool):
        """Показывает/прячет крестик удаления (без пересоздания thumb)."""
        self._can_delete = bool(ok)
        if hasattr(self, 'btn_del'):
            self.btn_del.setVisible(self._can_delete)

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


class _MarkerCanvas(QWidget):
    """Прозрачный overlay поверх превью для рисования красным маркером (Шаг A).
    Штрихи временные, в памяти (на диск не пишутся). Рисование разрешено ТОЛЬКО
    внутри image_rect (прямоугольник реальной 9:16-картинки в координатах
    viewport) — по чёрным полям letterbox рисовать нельзя. clear() сбрасывает.
    Когда неактивен — прозрачен для мыши (вид получает zoom/pan как обычно)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._strokes = []          # list[list[QPoint]]
        self._cur = []
        self._image_rect = QRect()  # зона картинки в координатах viewport
        self._drawing = False
        self.hide()

    def set_image_rect(self, rect):
        self._image_rect = QRect(rect)

    def set_active(self, on):
        # активен → ловит мышь и видим + круглый курсор-кисть; иначе прозрачен
        # для мыши, скрыт и курсор сброшен (вид показывает свой курсор pan).
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, not bool(on))
        self.setVisible(bool(on))
        if on:
            self.setCursor(self._build_cursor())
            self.raise_()
        else:
            self.unsetCursor()
        self.update()

    def _build_cursor(self):
        """Круглый курсор-кисть для режима маркера. QPixmap с devicePixelRatio
        → одинаковый видимый размер на Retina(Mac)/Windows. Рисуем в логических
        координатах (pm имеет dpr). Hotspot — центр круга (точка рисования)."""
        dpr = self.devicePixelRatioF() or 1.0
        edge = _MARKER_CURSOR_DIAM + 4           # логический размер pixmap (+поля)
        pm = QPixmap(int(round(edge * dpr)), int(round(edge * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        qp.setPen(QPen(_MARKER_COLOR, _MARKER_CURSOR_PEN))
        c = edge / 2.0                           # центр в ЛОГИЧЕСКИХ координатах
        r = _MARKER_CURSOR_DIAM / 2.0
        qp.drawEllipse(QPointF(c, c), r, r)
        qp.end()
        # hotspot при заданном devicePixelRatio трактуется в device-independent
        # координатах → центр.
        return QCursor(pm, int(c), int(c))

    def clear(self):
        self._strokes = []
        self._cur = []
        self._drawing = False
        self.update()

    def _pt(self, e):
        return e.position().toPoint()

    def mousePressEvent(self, e):
        p = self._pt(e)
        if (e.button() == Qt.MouseButton.LeftButton
                and self._image_rect.contains(p)):
            self._drawing = True
            self._cur = [p]

    def mouseMoveEvent(self, e):
        if not self._drawing:
            return
        p = self._pt(e)
        if self._image_rect.contains(p):
            self._cur.append(p)
            self.update()

    def mouseReleaseEvent(self, e):
        if not self._drawing:
            return
        self._drawing = False
        if len(self._cur) >= 2:
            self._strokes.append(self._cur)
        self._cur = []
        self.update()

    def paintEvent(self, _ev):
        if not self._strokes and len(self._cur) < 2:
            return
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(_MARKER_COLOR, _MARKER_WIDTH)   # ширина в логических px
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        qp.setPen(pen)
        if not self._image_rect.isNull():
            qp.setClipRect(self._image_rect)
        for stroke in self._strokes:
            if len(stroke) >= 2:
                qp.drawPolyline(QPolygon(stroke))
        if len(self._cur) >= 2:
            qp.drawPolyline(QPolygon(self._cur))


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
    # 2026-06-01: «🎬 Сделать реалистичным» — фотореалистичный ре-рендер
    # текущей активной версии шота (edit-механизм GenerateThread с
    # realistic=True). Отдельная кнопка рядом с «Перегенерировать».
    realistic_requested = pyqtSignal(int)
    version_use_requested = pyqtSignal(int, int)
    # 2026-06-04 (C2b): кроп при закрытии перезаписал просматриваемую версию +
    # активный файл → MW обновляет карточку грида (panel_idx).
    crop_committed = pyqtSignal(int)

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
        # 2026-05-17: адаптивный размер под родительское окно. Раньше был
        # фиксированный 720×800 min + 740×900 resize — не помещался на
        # 14" MacBook (1512×982 logical) при особенно высоком dock/menu.
        # Теперь max = 90% от main window (или экрана, если parent нет),
        # min = 600×700 — preview ужмётся в scroll-area если нужно.
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            pw, ph = parent_win.width(), parent_win.height()
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            pw, ph = geo.width(), geo.height()
        max_w, max_h = int(pw * 0.9), int(ph * 0.9)
        self.setMinimumSize(600, 700)
        self.setMaximumSize(max_w, max_h)
        self.resize(min(740, max_w), min(900, max_h))
        self._build()
        self.refresh()

    def _build(self):
        # 2026-05-17: убрана старая фиолетовая палитра #action/#primary —
        # перешли на LUMZ через lumz_button_qss() из views/theme.py.
        # Раскладка 4 кнопок: edit=subtle, regen=primary (главное действие),
        # use=secondary (outline), close=subtle.
        self.setStyleSheet(
            "QDialog { background:#0a0a0d; }"
            "QLabel#header { color:#fff; font-size:14px; font-weight:600; }"
            "QLabel#hint { color:rgba(255,255,255,0.55); font-size:11px; }"
            "QLabel#empty { color:rgba(255,255,255,0.40);"
            " font-style:italic; font-size:12px; }"
            + lumz_button_qss('subtle', 'btn_edit')
            + lumz_button_qss('primary', 'btn_regen')
            + lumz_button_qss('secondary', 'btn_realistic')
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

        # 2026-06-04 (C1): большое превью — зумируемый StoryboardView (колесо =
        # зум к курсору, drag = панорама). Импорт ЛЕНИВЫЙ — иначе `import widgets`
        # на старте потянет grid_dialog→widgets.face_grid.library до загрузки
        # storyboard_app (циклический импорт). grid_dialog НЕ трогаем.
        from widgets.face_grid.grid_dialog import StoryboardView
        self.preview_view = StoryboardView(QPixmap())
        # Стек: index0 = вид, index1 = заглушка «нет картинки» (сохраняем
        # прежний текстовый fallback из _show_preview).
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview_view)
        self.no_img_lbl = QLabel()
        self.no_img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_img_lbl.setStyleSheet(
            "background:#1a1424; border:1px solid #322545; border-radius:8px;"
            " color:#666; font-size:13px;")
        self.preview_stack.addWidget(self.no_img_lbl)
        # Контейнер тянется stretch=1 (занимает доступную вертикаль), стек
        # центрируем внутри. Реальный размер 9:16-рамки считаем императивно в
        # resizeEvent (_fit_preview_box) — декларативный layout не даёт честный
        # 9:16-бокс. Так рамка влезает в окно (не обрезается) и не раздувается.
        self.preview_holder = QWidget()
        _hl = QVBoxLayout(self.preview_holder)
        _hl.setContentsMargins(0, 0, 0, 0)
        _hl.addStretch()
        _prev_row = QHBoxLayout()
        _prev_row.addStretch()
        _prev_row.addWidget(self.preview_stack)
        _prev_row.addStretch()
        _hl.addLayout(_prev_row)
        _hl.addStretch()
        lay.addWidget(self.preview_holder, stretch=1)
        # Флаг «юзер реально зумил/панорамил» (для кропа в C2). Заводится в
        # eventFilter(Wheel) и при сдвиге скроллбаров вида; сбрасывается при
        # загрузке версии (_load_into_view) и во время программного _fit.
        self._preview_dirty = False
        self._loading_preview = False
        self._pending_crop_rect = None  # QRectF для восстановления кропа после show
        self.preview_view.viewport().installEventFilter(self)
        self.preview_view.horizontalScrollBar().valueChanged.connect(
            self._on_view_scrolled)
        self.preview_view.verticalScrollBar().valueChanged.connect(
            self._on_view_scrolled)
        # 2026-06-04 (M-b): overlay-кнопка горизонтального зеркала. РОДИТЕЛЬ =
        # preview_view.viewport() (НЕ в QGraphicsScene) → зум/панорама её не
        # двигают, и она поверх контента. Позиция — _position_mirror_btn.
        from storyboard_app import get_icon
        self.btn_mirror = QPushButton(self.preview_view.viewport())
        self.btn_mirror.setObjectName("shot-mirror")
        self.btn_mirror.setIcon(get_icon('flip-horizontal-2'))
        self.btn_mirror.setIconSize(QSize(16, 16))
        self.btn_mirror.setFixedSize(28, 28)
        self.btn_mirror.setCheckable(True)
        self.btn_mirror.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mirror.setStyleSheet(
            "QPushButton#shot-mirror { background:rgba(10,6,18,0.65);"
            " border:1px solid #322545; border-radius:6px; }"
            "QPushButton#shot-mirror:hover { background:rgba(40,24,64,0.85); }"
            "QPushButton#shot-mirror:checked { background:#231840;"
            " border-color:#6e4cc4; }")
        self.btn_mirror.clicked.connect(self._on_mirror_clicked)
        self.btn_mirror.raise_()
        # 2026-06-07 (Шаг A фичи маркера): overlay-canvas рисования + кнопка
        # «маркер» (child viewport, как btn_mirror). Canvas рисует только внутри
        # image_rect. Кнопка слева от зеркала, тот же стиль #shot-mirror.
        self.marker_canvas = _MarkerCanvas(self.preview_view.viewport())
        self.btn_marker = QPushButton(self.preview_view.viewport())
        self.btn_marker.setObjectName("shot-mirror")
        self.btn_marker.setIcon(get_icon('pencil'))
        self.btn_marker.setIconSize(QSize(16, 16))
        self.btn_marker.setFixedSize(28, 28)
        self.btn_marker.setCheckable(True)
        self.btn_marker.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_marker.setStyleSheet(
            "QPushButton#shot-mirror { background:rgba(10,6,18,0.65);"
            " border:1px solid #322545; border-radius:6px; }"
            "QPushButton#shot-mirror:hover { background:rgba(40,24,64,0.85); }"
            "QPushButton#shot-mirror:checked { background:#231840;"
            " border-color:#6e4cc4; }")
        self.btn_marker.clicked.connect(self._on_marker_clicked)
        self.btn_marker.raise_()

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
        # Колесо над лентой версий → горизонтальная прокрутка (eventFilter ниже).
        self.strip_scroll.viewport().installEventFilter(self)

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
        # 2026-06-01: кнопку «Закрыть» убрали (закрытие — системным крестиком
        # окна / Esc), осталось 4 кнопки. Места хватает с дефолтным стилем
        # theme (font 12 / padding 14), minimumWidth вернули покрупнее.
        self.btn_edit = QPushButton(tr('shot_viewer_btn_edit'))
        self.btn_edit.setObjectName("btn_edit")
        self.btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_edit.setMinimumWidth(130)
        # 2026-06-01: перед редактированием выделенная версия становится
        # активной (см. _activate_selected_version) — редактируется именно она.
        def _on_edit_clicked():
            self._activate_selected_version()
            self.edit_requested.emit(self.panel_idx)
        self.btn_edit.clicked.connect(_on_edit_clicked)
        actions.addWidget(self.btn_edit)

        self.btn_regen = QPushButton(tr('shot_viewer_btn_regen'))
        self.btn_regen.setObjectName("btn_regen")
        self.btn_regen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_regen.setMinimumWidth(155)
        # 2026-05-17: клик «Перегенерировать» закрывает попап (юзер просил —
        # не нужно вручную крестить после клика).
        # 2026-06-01: перед regen выделенная версия становится активной →
        # генерация идёт от неё (для regen это смена «текущей» перед новой).
        def _on_regen_clicked():
            self._activate_selected_version()
            self.regen_requested.emit(self.panel_idx)
            self.close()
        self.btn_regen.clicked.connect(_on_regen_clicked)
        actions.addWidget(self.btn_regen)

        # 2026-06-01: «🎬 Сделать реалистичным» — отдельная кнопка рядом с
        # regen. Берёт текущую активную версию как базу + те же рефы и
        # ре-рендерит в фотореализм (GenerateThread realistic=True). Как и
        # regen — закрывает попап после клика (потом перерисуется через
        # refresh_open_shot_viewer когда новая версия готова).
        self.btn_realistic = QPushButton(tr('shot_viewer_btn_realistic'))
        self.btn_realistic.setObjectName("btn_realistic")
        self.btn_realistic.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_realistic.setMinimumWidth(200)
        # 2026-06-01: перед realistic выделенная версия становится активной →
        # фотореалистичный ре-рендер идёт именно от неё (база = активный файл).
        def _on_realistic_clicked():
            self._activate_selected_version()
            self.realistic_requested.emit(self.panel_idx)
            self.close()
        self.btn_realistic.clicked.connect(_on_realistic_clicked)
        actions.addWidget(self.btn_realistic)

        # 2026-06-01: хвостовой stretch прижимает кнопки влево. Кнопку
        # «Использовать эту» убрали — теперь выделенная версия становится
        # активной автоматически при действии (edit/regen/realistic) или
        # при закрытии попапа (см. _activate_selected_version / closeEvent).
        actions.addStretch()

        lay.addLayout(actions)

    def _activate_selected_version(self):
        """Делает ВЫДЕЛЕННУЮ версию активной (копия vN→активный файл шота +
        active.txt) через сигнал version_use_requested → MW._on_shot_version_use.
        Вызывается ПЕРЕД действием (edit/regen/realistic) и из closeEvent.
        No-op если выделенная и так активна (guard) — лишнего disk-IO нет.

        ⚠️ КРИТИЧНО: version_use_requested ДОЛЖНА оставаться DirectConnection
        (она же AutoConnection в пределах одного GUI-потока). Тогда
        _on_shot_version_use (shutil.copy2 + set_active_version) отрабатывает
        СИНХРОННО и ПОЛНОСТЬЮ до того, как следующий эмит (regen/realistic/
        edit) стартует GenerateThread, который читает активный файл шота как
        базу. Если связь перевести в Qt.QueuedConnection — слот отложится в
        event loop, порядок сломается, и генерация возьмёт СТАРУЮ версию."""
        if self._selected_version > 0 and self._selected_version != self._active_version:
            self.version_use_requested.emit(self.panel_idx, self._selected_version)

    def closeEvent(self, ev):
        """2026-06-01: при закрытии попапа КРЕСТИКОМ окна (или self.close()
        из regen/realistic) выделенная версия становится активной. Для
        regen/realistic — no-op: они уже вызвали _activate_selected_version
        перед self.close() (guard selected==active).

        ВАЖНО: Escape сюда НЕ заходит — QDialog ловит Escape и зовёт reject()
        → done() → hide(), что НЕ шлёт QCloseEvent. Поэтому активация на Escape
        делается в переопределённом reject() ниже (а не здесь)."""
        self._maybe_save_crop()
        self._activate_selected_version()
        super().closeEvent(ev)

    def reject(self):
        """2026-06-01: закрытие по Escape идёт через QDialog.reject() (а не
        через closeEvent — hide() не шлёт QCloseEvent). Активируем выделенную
        версию здесь, чтобы Esc вёл себя так же как крестик окна.
        Двойной активации с closeEvent нет: крестик/self.close() идут через
        QCloseEvent (reject не вызывают), Escape — через reject (closeEvent не
        вызывают). Guard в _activate_selected_version страхует в любом случае."""
        self._maybe_save_crop()
        self._activate_selected_version()
        super().reject()

    def eventFilter(self, obj, ev):
        # Колесо над видом = зум → dirty (для C2; пропускаем дальше, зумит
        # StoryboardView.wheelEvent). Resize viewport → репозиция overlay-кнопки
        # зеркала РОВНО когда viewport получил реальный размер — детерминированно,
        # без гонки с layout (одинаково Mac/Win).
        if obj is self.preview_view.viewport():
            if (not self._loading_preview
                    and ev.type() == QEvent.Type.Wheel):
                self._preview_dirty = True
            elif ev.type() == QEvent.Type.Resize:
                self._position_mirror_btn()
        # Колесо над лентой версий → горизонтальная прокрутка ленты.
        # Кросс-платформа: Win-мышь шлёт angleDelta.y квантами ~120 (pixelDelta
        # пустой); Mac-трекпад — pixelDelta с инерцией, часто с горизонтальной
        # составляющей. Горизонтальный жест отдаём Qt (он сам скроллит H) —
        # без задвоения. Вертикальное колесо перенаправляем в H-скроллбар.
        if (obj is self.strip_scroll.viewport()
                and ev.type() == QEvent.Type.Wheel):
            pd = ev.pixelDelta()
            ad = ev.angleDelta()
            # Шаг 1: уже горизонтальный жест → отдать Qt (без задвоения на Mac).
            if (abs(pd.x()) > abs(pd.y())
                    or (pd.isNull() and abs(ad.x()) > abs(ad.y()))):
                return False
            # Шаг 2: вертикальное колесо → редирект в горизонтальный скроллбар.
            if not pd.isNull():
                dv = pd.y()                       # Mac: попиксельно (инерция)
            else:
                dv = ad.y() / 120.0 * _STRIP_WHEEL_STEP_PX   # Win: квант 120 → px
            hbar = self.strip_scroll.horizontalScrollBar()
            hbar.setValue(hbar.value() - int(dv))
            return True
        return super().eventFilter(obj, ev)

    def _on_view_scrolled(self, _value):
        # Сдвиг скроллбаров вида = панорама/зум → dirty, но НЕ во время
        # программной загрузки версии (_load_into_view / _fit / resize-бокса).
        if not self._loading_preview:
            self._preview_dirty = True
        # При панораме QGraphicsView скроллит дочерние виджеты viewport (кнопку)
        # вместе с контентом → возвращаем её в угол на каждый скролл.
        self._position_mirror_btn()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_preview_box()

    def _fit_preview_box(self):
        """Подгоняет 9:16-рамку превью под доступную область контейнера:
        максимум PREVIEW_W×PREVIEW_H, но сжимается на низких окнах, чтобы
        рамка (и весь шот) влезала без обрезки. fitInView вида срабатывает
        сам при ресайзе (StoryboardView.resizeEvent, если не было зума)."""
        if not hasattr(self, 'preview_holder'):
            return
        avail = self.preview_holder.size()
        aw, ah = max(1, avail.width()), max(1, avail.height())
        h = min(PREVIEW_H, ah)
        w = round(h * PREVIEW_W / PREVIEW_H)
        if w > min(PREVIEW_W, aw):
            w = min(PREVIEW_W, aw)
            h = round(w * PREVIEW_H / PREVIEW_W)
        if (self.preview_stack.width() != int(w)
                or self.preview_stack.height() != int(h)):
            self._loading_preview = True
            try:
                self.preview_stack.setFixedSize(int(w), int(h))
            finally:
                self._loading_preview = False
                self._preview_dirty = False
        # Основная репозиция — по Resize viewport в eventFilter (детерминирован-
        # но, любая ОС). Здесь доп. nudge на случай если viewport не ресайзится
        # (размер не изменился → Resize не придёт). Не рекурсивно: singleShot
        # планируется в call-site, сам _position_mirror_btn ничего не планирует.
        QTimer.singleShot(0, self._position_mirror_btn)

    def _position_mirror_btn(self):
        """Держит overlay-кнопку зеркала в НИЖНЕМ-правом углу viewport вида.
        Считает от РЕАЛЬНЫХ размеров viewport (родитель кнопки). Зовётся из
        eventFilter(Resize) И _on_view_scrolled — иначе при панораме
        (QGraphicsView скроллит дочерние виджеты viewport вместе с контентом)
        кнопка едет с картинкой."""
        btn = getattr(self, 'btn_mirror', None)
        if btn is None:
            return
        vp = self.preview_view.viewport()
        btn.move(max(0, vp.width() - btn.width() - 8),
                 max(0, vp.height() - btn.height() - 8))
        btn.raise_()
        # Шаг A фичи маркера: кнопка «маркер» слева от зеркала + ресайз canvas
        # под viewport + пересчёт image_rect (зум/панорама/ресайз).
        mk = getattr(self, 'btn_marker', None)
        if mk is not None:
            mk.move(max(0, vp.width() - mk.width() * 2 - 16),
                    max(0, vp.height() - mk.height() - 8))
            mk.raise_()
        cv = getattr(self, 'marker_canvas', None)
        if cv is not None:
            cv.setGeometry(0, 0, vp.width(), vp.height())
            cv.set_image_rect(self._compute_image_rect())
            if cv.isVisible():
                cv.raise_()
                btn.raise_()
                if mk is not None:
                    mk.raise_()

    def _compute_image_rect(self):
        """Прямоугольник реальной картинки внутри viewport (координаты
        viewport). mapFromScene учитывает текущий fit/зум/панораму. Пусто если
        картинки нет. Это зона, где маркеру разрешено рисовать."""
        view = getattr(self, 'preview_view', None)
        if view is None or getattr(view, 'pixmap_item', None) is None:
            return QRect()
        try:
            poly = view.mapFromScene(view.pixmap_item.sceneBoundingRect())
            return poly.boundingRect()
        except Exception:
            return QRect()

    def _on_marker_clicked(self, checked):
        """Тогл режима маркера. ВКЛ → canvas ловит мышь и виден (рисуем красным
        внутри картинки). ВЫКЛ → canvas прозрачен для мыши, скрыт, штрихи
        сброшены (разово). При ВЫКЛ зум/панорама/зеркало/клик по версиям —
        как раньше."""
        cv = getattr(self, 'marker_canvas', None)
        if cv is None:
            return
        self._position_mirror_btn()          # свежий размер + image_rect
        cv.set_active(bool(checked))
        if not checked:
            cv.clear()
        self.btn_mirror.raise_()
        self.btn_marker.raise_()

    def _on_mirror_clicked(self, checked):
        """M-b: тогл горизонтального зеркала просматриваемой версии. Зеркало
        СБРАСЫВАЕТ кроп (упрощение). Пишет через set_shot_mirror, перерисовывает
        вид, обновляет карточку грида."""
        if self._selected_version <= 0:
            return
        self._preview_dirty = False  # незакоммиченный кроп отбрасываем
        try:
            from storyboard_app import set_shot_mirror, read_shot_crop
            set_shot_mirror(self.history_dir, self._selected_version,
                            bool(checked), self.active_path)
            self._active_version = self._selected_version
            self._show_version(self._selected_version)
            cur = read_shot_crop(self.history_dir, self._selected_version)
            self.btn_mirror.setChecked(bool(cur and cur.get('mirror')))
            self.crop_committed.emit(self.panel_idx)
            self._refresh_thumb_deletability()
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)

    def _load_into_view(self, pix):
        """Кладёт QPixmap в существующий StoryboardView (свап pixmap_item в
        view._scene — тот же приём, что в actor_grid_dialog), ре-fit и СБРОС
        dirty. grid_dialog НЕ трогаем."""
        self._loading_preview = True
        try:
            view = self.preview_view
            if view.pixmap_item is not None:
                view._scene.removeItem(view.pixmap_item)
            view.pixmap_item = view._scene.addPixmap(pix)
            view._scene.setSceneRect(view.pixmap_item.boundingRect())
            view._fitted = False
            view._user_zoomed = False
            # Фитим СРАЗУ только если вид уже показан (рантайм-переключение
            # версий). При ПЕРВИЧНОЙ загрузке из __init__→refresh вид ещё не
            # виден и вьюпорт нулевой → _fit() посчитал бы неверно и закрыл
            # путь повторному фиту. Оставляем _fitted=False — собственный
            # showEvent StoryboardView (grid_dialog, НЕ трогаем) впишет
            # картинку целиком, когда вьюпорт получит реальный размер.
            if view.isVisible():
                view._fit()
        finally:
            self._loading_preview = False
            self._preview_dirty = False

    def _show_preview(self, image_path: Path):
        """Грузит картинку версии в зумируемый вид (полное разрешение, 1:1).
        Нет картинки → стек на заглушку с прежним текстом."""
        pix = QPixmap(str(image_path)) if image_path else QPixmap()
        if pix is not None and not pix.isNull():
            self.preview_stack.setCurrentWidget(self.preview_view)
            self._load_into_view(pix)
            return
        self.no_img_lbl.setText(tr('shot_viewer_no_image'))
        self.preview_stack.setCurrentWidget(self.no_img_lbl)

    def _show_version(self, n: int):
        """Показ версии v{n}. Есть сохранённый кроп (read_shot_crop) → грузим
        ОРИГИНАЛ orig_v{n} и применяем сохранённый scene_rect (юзер видит кадр,
        но отматывает от оригинала). Нет → грузим v{n}.jpg как обычно."""
        n = int(n)
        self._pending_crop_rect = None
        # Шаг A фичи маркера: смена/перешоу версии → старые штрихи неактуальны.
        _cv = getattr(self, 'marker_canvas', None)
        if _cv is not None:
            _cv.clear()
        crop = None
        try:
            from storyboard_app import read_shot_crop, shot_orig_path
            crop = read_shot_crop(self.history_dir, n)
        except Exception:
            crop = None
        try:
            self.btn_mirror.setChecked(bool(crop and crop.get('mirror')))
        except Exception:
            pass
        if crop and crop.get('scene_rect') is not None:
            try:
                opath = shot_orig_path(self.history_dir, n)
                r = crop.get('scene_rect')
                rect = QRectF(float(r['x']), float(r['y']),
                              float(r['w']), float(r['h']))
                if opath.exists():
                    pix = QPixmap(str(opath))
                    if bool(crop.get('mirror')):
                        # rect сохранён в координатах flip(orig) → база тоже flip
                        pix = pix.transformed(QTransform().scale(-1, 1))
                    if not pix.isNull():
                        self.preview_stack.setCurrentWidget(self.preview_view)
                        self._load_into_view(pix)        # грузим оригинал (+flip)
                        self._pending_crop_rect = rect
                        QTimer.singleShot(0, self._apply_pending_crop)
                        return
            except Exception:
                pass  # упало — обычный путь ниже
        vpath = self.history_dir / f"v{n}.jpg"
        if vpath.exists():
            self._show_preview(vpath)
        elif self.active_path.exists():
            self._show_preview(self.active_path)

    def _apply_pending_crop(self):
        """Ставит вид на сохранённый scene_rect (поверх загруженного оригинала)
        ПОСЛЕ show, когда вьюпорт реальный. Программно → под _loading_preview
        (без ложного dirty), _user_zoomed=True чтобы resize не сбросил кадр."""
        rect = self._pending_crop_rect
        view = self.preview_view
        if rect is None or view.pixmap_item is None:
            return
        self._loading_preview = True
        try:
            view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
            view._user_zoomed = True
            view._fitted = True
        finally:
            self._loading_preview = False
            self._preview_dirty = False
            self._pending_crop_rect = None

    def _maybe_save_crop(self):
        """C2b: если юзер зумил/панорамил (_preview_dirty) — сохранить кроп
        просматриваемой версии. Видимый scene-rect (в пикселях оригинала, т.к.
        в виде либо v{N}, либо orig_v{N} — оба размер шота). rect ≈ весь кадр
        (≥98% по обеим осям) → clear_shot_crop (сброс); иначе apply_shot_crop.
        Сам делает selected активной → _activate_selected_version → no-op."""
        if not self._preview_dirty or self._selected_version <= 0:
            return
        view = self.preview_view
        if view.pixmap_item is None:
            return
        try:
            sr = view._scene.sceneRect()
            W, H = sr.width(), sr.height()
            if W < 2 or H < 2:
                return
            vis = view.mapToScene(view.viewport().rect()).boundingRect()
            x = max(0.0, vis.left())
            y = max(0.0, vis.top())
            cw = max(0.0, min(W, vis.right()) - x)
            ch = max(0.0, min(H, vis.bottom()) - y)
            if cw < 2 or ch < 2:
                return
            from storyboard_app import apply_shot_crop, set_shot_mirror, \
                read_shot_crop
            cur = read_shot_crop(self.history_dir, self._selected_version)
            mirror = bool(cur and cur.get('mirror'))
            if cw >= 0.98 * W and ch >= 0.98 * H:
                # сброс КРОПА, но зеркало сохраняем: mirror=True → flip(orig)
                # полный кадр; mirror=False → clear (pristine).
                set_shot_mirror(self.history_dir, self._selected_version,
                                mirror, self.active_path)
            else:
                apply_shot_crop(self.history_dir, self._selected_version,
                                {'x': x, 'y': y, 'w': cw, 'h': ch},
                                self.active_path, img_w=int(W), img_h=int(H))
            self._active_version = self._selected_version  # _activate → no-op
            self._preview_dirty = False
            self.crop_committed.emit(self.panel_idx)
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)  # не блокируем закрытие

    def _on_thumb_clicked(self, version_n: int):
        # C2c: перед сменой версии коммитим отложенный кроп ТЕКУЩЕЙ выбранной
        # (она ещё в self._selected_version) — иначе правка v3 теряется при
        # переключении v3→v1. Не dirty → _maybe_save_crop выходит сразу.
        self._maybe_save_crop()
        self._selected_version = version_n
        # Update visual selection
        for thumb in self._thumbs:
            thumb.set_selected(thumb.version_n == version_n)
        # Показ версии с учётом сохранённого кропа (restore от оригинала).
        self._show_version(version_n)
        # Update label
        is_active = (version_n == self._active_version)
        if is_active:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_active', n=version_n))
        else:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_other', n=version_n))
        # 2026-06-01: клик по версии — ТОЛЬКО просмотр-превью, без записи на
        # диск. Активация выделенной происходит при действии или закрытии.
        self._refresh_thumb_deletability()

    def _refresh_thumb_deletability(self):
        """Лёгкий пересчёт видимости крестиков (без пересоздания ленты).
        Крестик скрыт на минимальной версии и на ВЫБРАННОЙ (selected). Зовётся
        после смены selected без полного refresh (клик/зеркало)."""
        mn = getattr(self, '_min_version', 0)
        for thumb in self._thumbs:
            thumb.set_deletable(thumb.version_n != mn
                                and thumb.version_n != self._selected_version)

    def _on_delete_version(self, version_n: int):
        """Крестик → подтверждение → delete_shot_version (удаление +
        перенумерация локстеп на диске) → refresh ленты + рефреш карточки."""
        ans = QMessageBox.question(
            self,
            tr('shot_viewer_delete_title'),
            tr('shot_viewer_delete_confirm', n=int(version_n)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            from storyboard_app import delete_shot_version
            new_active = delete_shot_version(
                self.history_dir, int(version_n), self.active_path)
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
            return
        if new_active < 1:
            return  # гард (v1/активная) или ошибка → диск не изменён
        self.refresh()                            # лента без дырок, крестики ок
        self.crop_committed.emit(self.panel_idx)  # рефреш карточки грида

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
        _nums = []
        for _p in versions:
            try:
                _nums.append(int(_p.stem[1:]))
            except (ValueError, IndexError):
                continue
        self._min_version = min(_nums) if _nums else 0
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
            return

        self.empty_versions_lbl.hide()
        self.strip_scroll.show()
        # Selected = active по умолчанию, ставим ДО построения thumbs — иначе на
        # момент цикла _selected_version стейл из прошлого refresh. Так крестик
        # сразу скрыт на выбранной (= активной при первом построении).
        self._selected_version = self._active_version
        for p in versions:
            try:
                n = int(p.stem[1:])
            except (ValueError, IndexError):
                continue
            is_active = (n == self._active_version)
            can_delete = (n != self._min_version
                          and n != self._selected_version)
            thumb = VersionThumb(n, p, is_active, can_delete=can_delete)
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.delete_requested.connect(self._on_delete_version)
            self.strip_layout.insertWidget(
                self.strip_layout.count() - 1, thumb)
            self._thumbs.append(thumb)

        # 5) Визуальное выделение активной (selected уже = active, см. выше).
        for thumb in self._thumbs:
            thumb.set_selected(thumb.version_n == self._active_version)
        # Превью активной (с учётом сохранённого кропа).
        self._show_version(self._active_version)

        if self._active_version > 0:
            self.selected_lbl.setText(
                tr('shot_viewer_selected_active', n=self._active_version))
        else:
            self.selected_lbl.setText("")

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
