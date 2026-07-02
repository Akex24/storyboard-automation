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
from PyQt6.QtGui import QPixmap, QTransform, QColor, QPainter, QPen, QPolygon, QPolygonF, QCursor, QIcon
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QFrame, QSizePolicy, QApplication,
    QStackedWidget, QMessageBox, QGraphicsItem, QGraphicsView,
    QGraphicsDropShadowEffect
)

from i18n import tr
from views.theme import lumz_button_qss, theme_qcolor


# 2026-05-07 (фикс UI): уменьшено чтобы попап влезал на 14" MacBook
# (1512×982 logical = ~900px usable height после menu bar). Раньше было
# 540×960 — попап был 1080+px и не помещался. Теперь preview ≈ исходный
# размер шота 384×688, дополнительно × 1.4 для удобства просмотра.
PREVIEW_W = 384
PREVIEW_H = 688
# 16:9 (горизонтальный шот): отдельный бокс превью, чтобы кадр занимал ширину
# попапа. PREVIEW_W/H выше НЕ трогаем (от них выведена лента версий THUMB).
PREVIEW_W_LAND = 880
PREVIEW_H_LAND = 495   # 880 × 9/16 = 495 → 16:9

# Миниатюра в ленте версий — компактнее.
THUMB_W = 70
THUMB_H = 125  # 70 × (688/384) ≈ 125

# 16:9 (горизонтальный шот): лента версий горизонтальная — перевёрнутая пара.
# Старые THUMB_W/THUMB_H (вертикаль 9:16) НЕ трогаем.
THUMB_W_LAND = 125
THUMB_H_LAND = 70  # 125 × 9/16 ≈ 70 → 16:9

# 2026-06-13 (дерево версий, Слой 2.2): фон/рамка карточки версии по depth.
# Заметный шаг между уровнями. depth>=2 → последний элемент (повтор, не падаем).
_VERSION_DEPTH_COLORS = [
    ("#191b1d", "rgba(255,255,255,0.10)"),  # depth 0 — фон как shot-card
    ("#191b1d", "rgba(255,255,255,0.10)"),  # depth 1 — без фиолетовой заливки
    ("#191b1d", "rgba(255,255,255,0.10)"),  # depth 2+ — без фиолетовой заливки
]

# Шаг горизонтальной прокрутки ленты версий колесом для классической мыши
# (Windows): angleDelta идёт квантами ~120 на «щелчок». Сколько пикселей
# двигать ленту за один квант. # подобрать на живой Windows-мыши.
_STRIP_WHEEL_STEP_PX = 80

# Толщина маркера в ЛОГИЧЕСКИХ пикселях (device-independent). QPainter рисует в
# логических координатах, Qt сам масштабирует по devicePixelRatio → видимая
# толщина ОДИНАКОВА на Retina (Mac) и обычных экранах (Windows). НЕ умножать на
# dpr вручную. «Средняя» фиксированная толщина (регулировки пока нет).
_MARKER_WIDTH = 3
_MARKER_COLOR = theme_qcolor("#e61e1e")   # фиксированный красный

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
                 can_delete: bool = False, aspect: str = "9:16",
                 depth: int = 0, dotted: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.version_n = version_n
        self.image_path = image_path
        self._is_active = is_active
        self._is_selected = is_active  # выбран по умолчанию = активный
        self._can_delete = can_delete
        self._aspect = aspect
        # 2026-06-13 (Слой 2.2): depth → цвет карточки; dotted → подпись
        # (наклейка дерева). Плоский version_n остаётся ключом. Старый дефолт
        # (depth=0, dotted=None) = прежнее поведение.
        self._depth = depth
        self._dotted = dotted
        # Этап (формат): 16:9 → горизонтальная миниатюра (перевёрнутая пара);
        # 9:16 → вертикальная THUMB_W×H (как было, байт-в-байт).
        self._thumb_w, self._thumb_h = (
            (THUMB_W_LAND, THUMB_H_LAND) if aspect == "16:9"
            else (THUMB_W, THUMB_H))
        self.setObjectName("VersionThumb")
        # 2026-06-14 (фикс наезда картинки на рамку): +6/+6 резерв под МАКС-border
        # (3px × 2 стороны), чтобы img_lbl(thumb_w×thumb_h) влезал ВНУТРЬ рамки при
        # активной (3px), а при 1px/2px — с зазором. Размер карточки от border не
        # зависит → КОНСТАНТЕН для всех состояний (не прыгает). Симметрично 9:16/16:9.
        self.setFixedSize(self._thumb_w + 6 + 6, self._thumb_h + 28 + 6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._refresh_style()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(4)
        self.img_lbl = QLabel()
        self.img_lbl.setFixedSize(self._thumb_w, self._thumb_h)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet(
            "background:#131516; border-radius:4px;")
        # Загружаем картинку.
        try:
            pix = QPixmap(str(self.image_path))
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(self._thumb_w, self._thumb_h),
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
        self.btn_del.move(self._thumb_w - 18 - 2, 2)
        self.btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_del.setStyleSheet(
            "QPushButton#thumb-del { background:rgba(10,6,18,0.7);"
            " border:none; border-radius:9px; }"
            "QPushButton#thumb-del:hover { background:rgba(150,40,40,0.9); }")
        self.btn_del.clicked.connect(
            lambda: self.delete_requested.emit(self.version_n))
        self.btn_del.setVisible(self._can_delete)
        self.btn_del.raise_()

        # 2026-07-02: бейдж NEW — overlay в top-LEFT превьюшки (тот же приём,
        # что крестик: родитель img_lbl, вне layout, .move + .raise_).
        # objectName "new-badge" → общий QSS приложения (storyboard_app.py:3934).
        # Видимость ставит refresh() через set_new_badge (по MW._unseen_versions).
        self.badge_new = QLabel("NEW", self.img_lbl)
        self.badge_new.setObjectName("new-badge")
        self.badge_new.move(2, 2)
        self.badge_new.hide()
        self.badge_new.raise_()

        active_suffix = "  ✓" if self._is_active else ""
        # 2026-06-13 (Слой 2.2): подпись = dotted ('v5.1'), fallback плоский.
        _base = self._dotted or f"v{self.version_n}"
        self.label = QLabel(f"{_base}{active_suffix}")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._is_active:
            self.label.setStyleSheet(
                "color:#7fbf7f; font-size:11px; font-weight:600;")
        else:
            self.label.setStyleSheet(
                "color:#b8b8b8; font-size:11px;")
        lay.addWidget(self.label)

    def set_selected(self, selected: bool):
        self._is_selected = selected
        self._refresh_style()

    def set_deletable(self, ok: bool):
        """Показывает/прячет крестик удаления (без пересоздания thumb)."""
        self._can_delete = bool(ok)
        if hasattr(self, 'btn_del'):
            self.btn_del.setVisible(self._can_delete)

    def set_new_badge(self, visible: bool):
        """Показ/скрытие бейджа NEW (без пересоздания thumb) — образец set_deletable."""
        if hasattr(self, 'badge_new'):
            self.badge_new.setVisible(bool(visible))

    def _refresh_style(self):
        # 2026-06-14: фон по depth; ПРИОРИТЕТ рамок: АКТИВНАЯ (яркое золото
        # #ffc83a 3px + золотое свечение) > выбранная мышкой (#6e4cc4 2px) >
        # depth-рамка. Активность ВАЖНЕЕ выбора (И активна И selected → золото).
        # depth>=2 → последний цвет (clamp, не падаем при глубже).
        _d = self._depth if isinstance(self._depth, int) and self._depth >= 0 else 0
        bg, base_border = _VERSION_DEPTH_COLORS[
            min(_d, len(_VERSION_DEPTH_COLORS) - 1)]
        if self._is_active:
            border = "3px solid #ffc83a"          # яркое золото — сильнейший сигнал
        elif self._is_selected:
            border = "2px solid rgba(255,255,255,0.18)"
        else:
            border = f"1px solid {base_border}"
        self.setStyleSheet(
            f"#VersionThumb {{ background:{bg}; border:{border};"
            f" border-radius:6px; }}")
        # Свечение активной — QGraphicsDropShadowEffect (золото, blur 18, offset 0).
        # Толстая золотая рамка выше — ГАРАНТ видимости даже если ореол местами
        # подрежется соседями/краем на дробном DPR. Тень аддитивна; на любой сбой
        # эффекта остаётся рамка. setGraphicsEffect(None) на неактивных чистит.
        if self._is_active:
            try:
                _eff = QGraphicsDropShadowEffect(self)
                _eff.setBlurRadius(18)
                _eff.setOffset(0, 0)
                _eff.setColor(theme_qcolor("rgba(255,200,58,0.78)"))
                self.setGraphicsEffect(_eff)
            except Exception:
                self.setGraphicsEffect(None)
        else:
            self.setGraphicsEffect(None)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.version_n)
        super().mousePressEvent(ev)


class _MarkerItem(QGraphicsItem):
    """Элемент сцены StoryboardView для рисования красным маркером (Шаг A/B/C).
    Рисуется КАК ЭЛЕМЕНТ сцены поверх картинки (тот же приём, что face-grid
    сетки) — НЕ отдельным translucent-виджетом: тот ронял cocoa в
    QBackingStore::flush на внешних мониторах с дробным DPR (M4 + Qt 6.10).
    Штрихи временные, в памяти, в координатах ITEM = SCENE = пиксели картинки
    1:1 (item в origin, без трансформа) → привязаны к картинке при ЛЮБОМ
    зуме/панораме (сцена сама трансформирует). Рисование разрешено ТОЛЬКО
    внутри картинки (pixmap_item). clear() сбрасывает."""

    def __init__(self, view):
        super().__init__()
        self._view = view            # StoryboardView — pixmap_item / dpr курсора
        self._strokes = []           # list[list[QPointF]] в SCENE-координатах
        self._cur = []
        self._drawing = False
        self.setZValue(1000)         # поверх картинки
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsToShape, True)
        self.setVisible(False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self):
        item = getattr(self._view, 'pixmap_item', None)
        return item.boundingRect() if item is not None else QRectF()

    def set_active(self, on):
        # активен → видим, ловит ЛКМ, круглый курсор-кисть; иначе скрыт, мышь
        # не ловит, курсор сброшен (вид показывает свой pan-курсор).
        self.setVisible(bool(on))
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton if on else Qt.MouseButton.NoButton)
        if on:
            self.setCursor(self._build_cursor())
        else:
            self.unsetCursor()
        self.update()

    def _safe_dpr(self):
        """device pixel ratio ТОЛЬКО когда вид реально realized на экране
        (есть windowHandle). Иначе / при ошибке — 1.0. Защита от SIGSEGV в
        cocoa-флаше на внешних мониторах с дробным DPR (M4 + Qt 6.10)."""
        try:
            w = self._view.window() if self._view else None
            wh = w.windowHandle() if w else None
            if wh is None:
                return 1.0
            return self._view.devicePixelRatioF() or 1.0
        except Exception:
            return 1.0

    def _build_cursor(self):
        """Круглый курсор-кисть для режима маркера. dpr ЦЕЛОЧИСЛЕННЫЙ (round) —
        дробный pixmap.setDevicePixelRatio роняет cocoa-курсор на нестандартных
        внешних мониторах. Hotspot — центр круга (точка рисования)."""
        dpr = max(1, int(round(self._safe_dpr())))
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
        return QCursor(pm, int(c), int(c))

    def clear(self):
        self._strokes = []
        self._cur = []
        self._drawing = False
        self.update()

    def has_strokes(self):
        return bool(self._strokes)

    def scene_strokes(self):
        # список штрихов, каждый — list[QPointF] в SCENE-координатах (пиксели).
        return self._strokes

    def _inside(self, sp):
        # точка внутри картинки? (item-координаты = scene = пиксели 1:1)
        item = getattr(self._view, 'pixmap_item', None)
        if item is None:
            return False
        return item.boundingRect().contains(sp)

    def mousePressEvent(self, e):
        sp = e.pos()                 # item-координаты = scene
        if e.button() == Qt.MouseButton.LeftButton and self._inside(sp):
            self._drawing = True
            self._cur = [sp]
            e.accept()
        else:
            e.ignore()

    def mouseMoveEvent(self, e):
        if not self._drawing:
            e.ignore()
            return
        sp = e.pos()
        if self._inside(sp):
            self._cur.append(sp)
            self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if not self._drawing:
            e.ignore()
            return
        self._drawing = False
        if len(self._cur) >= 2:
            self._strokes.append(self._cur)
        self._cur = []
        self.update()
        e.accept()

    def paint(self, qp, option, widget=None):
        if not self._strokes and len(self._cur) < 2:
            return
        qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(_MARKER_COLOR, _MARKER_WIDTH)   # экранная толщина (cosmetic)
        pen.setCosmetic(True)                      # постоянна при любом зуме сцены
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        qp.setPen(pen)
        for stroke in self._strokes:
            if len(stroke) >= 2:
                qp.drawPolyline(QPolygonF(stroke))   # item=scene координаты
        if len(self._cur) >= 2:
            qp.drawPolyline(QPolygonF(self._cur))


class ShotViewerDialog(QDialog):
    """Non-modal попап просмотра одного шота с лентой версий.

    Сигналы наружу (MainWindow подключает):
      • edit_requested(int)           — клик «Редактировать» (panel_idx)
      • regen_requested(int)          — клик «Перегенерировать»
      • version_use_requested(int,    — клик «Использовать эту» (panel_idx, version_n)
                              int)
    """

    edit_requested = pyqtSignal(int, int, str)  # (panel_idx, parent_version, block_name)
    regen_requested = pyqtSignal(int, int, str)  # (panel_idx, parent_version, block_name)
    # 2026-06-01: «🎬 Сделать реалистичным» — фотореалистичный ре-рендер
    # текущей активной версии шота (edit-механизм GenerateThread с
    # realistic=True). Отдельная кнопка рядом с «Перегенерировать».
    realistic_requested = pyqtSignal(int, int, str)  # (panel_idx, parent_version, block_name)
    version_use_requested = pyqtSignal(int, int)
    # 2026-06-04 (C2b): кроп при закрытии перезаписал просматриваемую версию +
    # активный файл → MW обновляет карточку грида (panel_idx).
    crop_committed = pyqtSignal(int)

    def __init__(self, panel_idx: int, block_name: str,
                 active_path: Path, history_dir: Path,
                 aspect: str = "9:16",
                 style: str = "sketch",
                 description: str = "",
                 dialog: str = "",
                 parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.panel_idx = panel_idx
        self.block_name = block_name
        # 2026-06-23: описание шота над лентой версий вместо технической
        # подсказки: действие (#shot-desc) + реплика (#shot-dialog, фиолетовый
        # курсив, как на карточке). Обе пустые → старый hint.
        self._description = description
        self._dialog = dialog
        self.active_path = Path(active_path)
        self.history_dir = Path(history_dir)
        # Этап (формат кадра): формат зоны просмотра. Дефолт "9:16" → как было.
        self._aspect = aspect
        # 2026-06-13: стиль блока ('sketch'|'realistic'). При 'realistic'
        # кнопка «Сделать реалистичным» скрывается в _build. Дефолт 'sketch'
        # → старые блоки/любая неудача чтения = кнопка показана как прежде.
        self._style = style
        self._selected_version: int = 0  # current selection in thumb strip
        self._active_version: int = 0    # which is actually active
        self._thumbs: List[VersionThumb] = []
        # 2026-06-14 (фикс слёта маркера): пред-запечённый маркер-PNG —
        # заготавливается в _on_edit_clicked ДО _activate (пока штрихи живы),
        # MW забирает через take_pending_marked(). One-shot; сброс при
        # закрытии/отмене (_clear_pending_marked).
        self._pending_marked_path = None
        # 2026-07-02: одноразовый флаг «не закрывать попап после regen-клика».
        # Ставит show_limit_banner (MW зовёт при лимите N/N). Дефолт False —
        # обычный regen / ветки vps<=1 и не-Mode-C закрывают попап как раньше.
        self._suppress_close_after_regen = False
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
        if aspect == "16:9":
            # Горизонтальная зона → попап шире и ниже.
            self.setMinimumSize(900, 600)
            self.setMaximumSize(max_w, max_h)
            self.resize(min(1100, max_w), min(750, max_h))
        else:
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
            "QDialog { background:#121313; }"
            "QLabel#header { color:#fff; font-size:14px; font-weight:600; }"
            "QLabel#hint { color:rgba(255,255,255,0.55); font-size:11px; }"
            "QLabel#empty { color:rgba(255,255,255,0.40);"
            " font-style:italic; font-size:12px; }"
            "QPushButton#btn_edit, QPushButton#btn_regen {"
            " background:#242628;"
            " border:1px solid rgba(255,255,255,0.10);"
            " border-radius:8px;"
            " color:#f2f3f0;"
            " padding:8px 14px;"
            " font-weight:600;"
            "}"
            "QPushButton#btn_edit:hover, QPushButton#btn_regen:hover {"
            " background:#2c2f31;"
            " border-color:rgba(255,255,255,0.16);"
            "}"
            "QPushButton#btn_edit:pressed, QPushButton#btn_regen:pressed {"
            " background:#1f2123;"
            "}"
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
            "background:#191b1d; border:1px solid rgba(255,255,255,0.055); border-radius:8px;"
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
        # 2026-06-07 (Шаг A + план C фичи маркера): маркер рисуется ЭЛЕМЕНТОМ
        # сцены preview_view (как face-grid сетки), НЕ translucent-виджетом —
        # тот ронял cocoa в QBackingStore::flush на внешних 5K-мониторах с
        # дробным DPR. Рядом — кнопка «маркер» слева от зеркала, стиль
        # #shot-mirror. Рисование разрешено только внутри картинки (pixmap_item).
        self.marker_canvas = _MarkerItem(self.preview_view)
        self.preview_view._scene.addItem(self.marker_canvas)
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
        # 2026-06-23: над лентой версий — описание шота РАЗДЕЛЬНО: действие
        # (#ffffff 14px) + реплика (#b9a7e6 курсив 14px) INLINE-стилем. Inline,
        # а НЕ objectName: app-level QSS #shot-desc/#shot-dialog (ID-селектор)
        # перебил бы inline по специфичности. Обе пустые → старый hint.
        if self._description or self._dialog:
            if self._description:
                action_lbl = QLabel(self._description)
                action_lbl.setStyleSheet("color:#878788; font-size:12px;")
                action_lbl.setWordWrap(True)
                lay.addWidget(action_lbl)
            if self._dialog:
                replica_lbl = QLabel(self._dialog)
                replica_lbl.setStyleSheet(
                    "color:#b9a7e5; font-style:italic; font-size:12px;")
                replica_lbl.setWordWrap(True)
                lay.addWidget(replica_lbl)
        else:
            strip_label = QLabel(tr('shot_viewer_versions_label'))
            strip_label.setObjectName("hint")
            lay.addWidget(strip_label)

        self.strip_scroll = QScrollArea()
        self.strip_scroll.setWidgetResizable(True)
        # Этап (формат): высота ленты от формата миниатюры (16:9 — ниже).
        _strip_thumb_h = (THUMB_H_LAND
                          if getattr(self, '_aspect', '9:16') == "16:9"
                          else THUMB_H)
        # 2026-06-14: +6 синхронно с резервом высоты карточки (см. VersionThumb
        # setFixedSize) — сохраняет прежний вертикальный запас ленты (22px), чтоб
        # карточка не обрезалась снизу и не появлялся лишний вертикальный скролл.
        self.strip_scroll.setFixedHeight(_strip_thumb_h + 50 + 6)
        self.strip_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.strip_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip_scroll.setStyleSheet(
            "QScrollArea { border:1px solid rgba(255,255,255,0.055); border-radius:6px;"
            " background:#191b1d; }")
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
        self.btn_edit.setFixedSize(150, 40)
        # 2026-06-01: перед редактированием выделенная версия становится
        # активной (см. _activate_selected_version) — редактируется именно она.
        def _on_edit_clicked():
            _parent = self._selected_version  # родитель будущего потомка
            # 2026-06-14 (маркер виден при Edit): активацию выбранной версии НЕ
            # вызываем — она звала refresh→_show_version→clear и стирала штрихи
            # маркера С ЭКРАНА. Без неё маркер виден; потомок станет активным
            # ПОСЛЕ генерации (set_active_version в GenerateThread).
            # closeEvent/reject/regen/realistic активацию сохраняют.
            # Пред-бейк маркера: MW заберёт отпечаток через take_pending_marked()
            # (+ peek для «Улучшить»). Нет штрихов → _bake вернёт None.
            self._pending_marked_path = self._bake_marked_image()
            self.edit_requested.emit(self.panel_idx, _parent, self.block_name)
        self.btn_edit.clicked.connect(_on_edit_clicked)
        actions.addWidget(self.btn_edit)

        self.btn_regen = QPushButton(tr('shot_viewer_btn_regen'))
        self.btn_regen.setObjectName("btn_regen")
        self.btn_regen.setCursor(Qt.CursorShape.PointingHandCursor)
        try:
            from storyboard_app import get_icon as _get_icon
            self.btn_regen.setText(tr('shot_viewer_btn_regen').replace('↻', '').strip())
            self.btn_regen.setIcon(_get_icon('corner-up-left'))
            self.btn_regen.setIconSize(QSize(16, 16))
        except Exception:
            self.btn_regen.setIcon(QIcon())
        self.btn_regen.setFixedSize(210, 40)
        # 2026-05-17: клик «Перегенерировать» закрывает попап (юзер просил —
        # не нужно вручную крестить после клика).
        # 2026-06-01: перед regen выделенная версия становится активной →
        # генерация идёт от неё (для regen это смена «текущей» перед новой).
        def _on_regen_clicked():
            _parent = self._selected_version  # родитель ДО активации
            self._suppress_close_after_regen = False
            self._activate_selected_version()
            # DirectConnection: _on_regen (MW) отрабатывает СИНХРОННО здесь же.
            # При лимите N/N он вызовет show_limit_banner → флаг True → не закрываем.
            self.regen_requested.emit(self.panel_idx, _parent, self.block_name)
            if not self._suppress_close_after_regen:
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
            _parent = self._selected_version  # родитель ДО активации
            self._activate_selected_version()
            self.realistic_requested.emit(self.panel_idx, _parent, self.block_name)
            self.close()
        self.btn_realistic.clicked.connect(_on_realistic_clicked)
        actions.addWidget(self.btn_realistic)
        # 2026-06-13: блок целиком сгенерён фотореалом → «Сделать
        # реалистичным» нечего делать, прячем. Атрибут создан всегда
        # (никаких Optional-проверок в остальном коде).
        if self._style == 'realistic':
            self.btn_realistic.hide()

        # 2026-06-01: хвостовой stretch прижимает кнопки влево. Кнопку
        # «Использовать эту» убрали — теперь выделенная версия становится
        # активной автоматически при действии (edit/regen/realistic) или
        # при закрытии попапа (см. _activate_selected_version / closeEvent).
        actions.addStretch()

        lay.addLayout(actions)

        # 2026-07-02: оверлей-баннер «лимит N/N» (скрыт; показывается MW при добор-нооп).
        self._build_limit_banner()

    def _build_limit_banner(self):
        """Внутридиалоговый оверлей-баннер (overlay-приём как btn_del: родитель —
        сам диалог, .move + .raise_, репозиция в resizeEvent). Скрыт по умолчанию."""
        self.limit_banner = QFrame(self)
        self.limit_banner.setObjectName("limit-banner")
        self.limit_banner.setStyleSheet(
            "QFrame#limit-banner { background:#2a1f0a;"
            " border:1px solid #4a3010; border-radius:8px; }"
            "QLabel#limit-banner-text { color:#ffaa44; font-size:12px;"
            " font-weight:600; background:transparent; border:none; }")
        _bl = QHBoxLayout(self.limit_banner)
        _bl.setContentsMargins(12, 8, 8, 8)
        _bl.setSpacing(10)
        self._limit_banner_lbl = QLabel("")
        self._limit_banner_lbl.setObjectName("limit-banner-text")
        self._limit_banner_lbl.setWordWrap(True)
        _bl.addWidget(self._limit_banner_lbl, stretch=1)
        from storyboard_app import get_icon
        _close = QPushButton(self.limit_banner)
        _close.setObjectName("limit-banner-close")
        _close.setIcon(get_icon('x'))
        _close.setIconSize(QSize(14, 14))
        _close.setFixedSize(22, 22)
        _close.setCursor(Qt.CursorShape.PointingHandCursor)
        _close.setStyleSheet(
            "QPushButton#limit-banner-close { background:transparent;"
            " border:none; border-radius:11px; }"
            "QPushButton#limit-banner-close:hover {"
            " background:rgba(255,170,68,0.18); }")
        _close.clicked.connect(self.limit_banner.hide)
        _bl.addWidget(_close, alignment=Qt.AlignmentFlag.AlignTop)
        self.limit_banner.hide()

    def show_limit_banner(self, n: int):
        """MW зовёт при добор-нооп (лимит N/N): показать баннер И подавить
        закрытие попапа после regen-клика. Синхронно в рамках DirectConnection."""
        self._suppress_close_after_regen = True
        try:
            self._limit_banner_lbl.setText(tr('banner_limit_reached', n=int(n)))
        except Exception:
            pass
        self._position_limit_banner()
        self.limit_banner.show()
        self.limit_banner.raise_()

    def _position_limit_banner(self):
        """Ширина = контент-полоса диалога (минус боковые поля 16), верх — 10px."""
        if not hasattr(self, 'limit_banner'):
            return
        m = 16
        self.limit_banner.setFixedWidth(max(1, self.width() - 2 * m))
        self.limit_banner.adjustSize()
        self.limit_banner.move(m, 10)

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
        self._clear_pending_marked()   # 2026-06-14: несъеденный отпечаток не течёт
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
        self._clear_pending_marked()   # 2026-06-14: несъеденный отпечаток не течёт
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
        # getattr-гард: фильтр на preview_view.viewport() ставится (:450) ДО
        # создания strip_scroll (:491) → preview-событие до сборки ленты иначе
        # роняет AttributeError. После сборки ленты поведение идентично.
        _strip = getattr(self, 'strip_scroll', None)
        if (_strip is not None and obj is _strip.viewport()
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
            hbar = _strip.horizontalScrollBar()
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
        if (getattr(self, 'limit_banner', None) is not None
                and self.limit_banner.isVisible()):
            self._position_limit_banner()

    def _fit_preview_box(self):
        """Подгоняет 9:16-рамку превью под доступную область контейнера:
        максимум PREVIEW_W×PREVIEW_H, но сжимается на низких окнах, чтобы
        рамка (и весь шот) влезала без обрезки. fitInView вида срабатывает
        сам при ресайзе (StoryboardView.resizeEvent, если не было зума)."""
        if not hasattr(self, 'preview_holder'):
            return
        avail = self.preview_holder.size()
        aw, ah = max(1, avail.width()), max(1, avail.height())
        # Этап (формат): 9:16 → вертикальный бокс PREVIEW_W×H (как было);
        # 16:9 → горизонтальный PREVIEW_W_LAND×H_LAND.
        _pw, _ph = ((PREVIEW_W_LAND, PREVIEW_H_LAND)
                    if getattr(self, '_aspect', '9:16') == "16:9"
                    else (PREVIEW_W, PREVIEW_H))
        h = min(_ph, ah)
        w = round(h * _pw / _ph)
        if w > min(_pw, aw):
            w = min(_pw, aw)
            h = round(w * _ph / _pw)
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
        # Шаг A фичи маркера: кнопка «маркер» слева от зеркала. Сам маркер —
        # ЭЛЕМЕНТ сцены (план C): позиционировать/ресайзить виджет не нужно,
        # сцена трансформирует его при зуме/панораме сама.
        mk = getattr(self, 'btn_marker', None)
        if mk is not None:
            mk.move(max(0, vp.width() - mk.width() * 2 - 16),
                    max(0, vp.height() - mk.height() - 8))
            mk.raise_()

    def _on_marker_clicked(self, checked):
        """Тогл режима маркера. ВКЛ → элемент-маркер виден и ловит ЛКМ, вид
        переводится в NoDrag (ЛКМ рисует, не панорамит). ВЫКЛ → элемент скрыт,
        штрихи сброшены (разово), прежний dragMode (пан) возвращается."""
        cv = getattr(self, 'marker_canvas', None)
        if cv is None:
            return
        if checked:
            # запоминаем прежний режим перетаскивания (пан) и выключаем его —
            # иначе ЛКМ панорамит вид, а не рисует.
            self._prev_drag_mode = self.preview_view.dragMode()
            self.preview_view.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            prev = getattr(self, '_prev_drag_mode', None)
            self.preview_view.setDragMode(
                prev if prev is not None
                else QGraphicsView.DragMode.ScrollHandDrag)
        cv.set_active(bool(checked))
        if not checked:
            cv.clear()
        self.btn_mirror.raise_()
        self.btn_marker.raise_()

    def _bake_marked_image(self):
        """Шаг B: запекает штрихи маркера в копию полноразмерной картинки версии.
        Возвращает путь к temp-PNG или None (нет штрихов / нет картинки). Оригинал
        версии НЕ трогается. crop/mirror уже в pixmap сцены — берём ЕЁ (НЕ
        перечитываем v{n}.jpg). Штрихи в SCENE-координатах = пиксели 1:1 → рисуем
        прямо. В crop-случае scene = orig (до crop) → финально вырезаем видимый
        кадр через .copy(scene_rect); crop НЕ применяется дважды. Для Шага C."""
        cv = getattr(self, 'marker_canvas', None)
        if cv is None or not cv.has_strokes():
            return None
        view = self.preview_view
        item = getattr(view, 'pixmap_item', None)
        if item is None:
            return None
        src = item.pixmap()
        if src is None or src.isNull():
            return None
        baked = src.copy()
        try:
            qp = QPainter(baked)
            qp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # перо в ПИКСЕЛЯХ картинки: экранную толщину делим на масштаб вида
            # (m11 = scene→viewport) → штрих на полноразмере как на экране.
            m11 = view.transform().m11() or 1.0
            pen = QPen(_MARKER_COLOR, max(1.0, _MARKER_WIDTH / m11))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            qp.setPen(pen)
            for stroke in cv.scene_strokes():
                if len(stroke) >= 2:
                    qp.drawPolyline(QPolygonF(stroke))   # scene = пиксель 1:1
            qp.end()
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
            return None
        # crop к видимому кадру версии (scene = orig в crop-случае).
        out = baked
        try:
            from storyboard_app import read_shot_crop
            crop = read_shot_crop(self.history_dir, self._selected_version)
            sr = crop.get('scene_rect') if crop else None
            if sr is not None:
                rect = QRect(int(sr['x']), int(sr['y']),
                             int(sr['w']), int(sr['h'])).intersected(baked.rect())
                if rect.width() > 1 and rect.height() > 1:
                    out = baked.copy(rect)
        except Exception:
            pass
        # temp PNG (gettempdir; чистится Шагом C после отправки).
        try:
            import tempfile
            fname = (f"shot_marked_{self.block_name}_shot{self.panel_idx + 1}"
                     f"_v{int(self._selected_version)}.png")
            path = Path(tempfile.gettempdir()) / fname
            if out.save(str(path), "PNG"):
                return path
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)
        return None

    def take_pending_marked(self):
        """2026-06-14 (фикс слёта маркера): отдаёт ПРЕД-запечённый маркер-PNG
        (заготовлен в _on_edit_clicked ДО _activate, пока штрихи живы) и
        ЗАНУЛЯЕТ поле — one-shot, чтобы отпечаток не утёк в следующую правку.
        None если маркера не было. Guard «файл существует»: если temp кто-то
        удалил → None, чтобы вызывающая сторона ре-бейкнула через
        `or _bake_marked_image()`. MW чистит temp на finished/error."""
        p = self._pending_marked_path
        self._pending_marked_path = None
        if p is not None and not p.exists():
            return None
        return p

    def peek_pending_marked(self):
        """2026-06-14 (Улучшить видит маркер): НЕ-разрушающий просмотр пред-
        отпечатка — поле НЕ зануляет, отпечаток остаётся для финальной edit-
        генерации (и для повторных нажатий «Улучшить»). Тот же guard «файл
        существует» → None если temp удалён."""
        p = self._pending_marked_path
        if p is not None and not p.exists():
            return None
        return p

    def _clear_pending_marked(self):
        """Сброс НЕсъеденного пред-отпечатка (отмена правки / закрытие диалога):
        удаляет temp-PNG если остался + зануляет поле. Если уже съеден
        (take_pending_marked вернул путь) — поле None, no-op (MW владеет
        очисткой)."""
        p = self._pending_marked_path
        self._pending_marked_path = None
        if p is not None:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

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
        # 2026-07-02: ЯВНЫЙ клик по версии снимает её NEW (авто-select при
        # открытии идёт НЕ сюда — refresh ставит selected напрямую, поэтому NEW
        # на активной держится до реального клика). Закрытие/смена блока — не чистят.
        try:
            _mw = self.parent()
            if _mw is not None and hasattr(_mw, 'mark_version_seen'):
                _mw.mark_version_seen(self.block_name, self.panel_idx, version_n)
        except Exception:
            pass
        self._selected_version = version_n
        # Update visual selection
        for thumb in self._thumbs:
            thumb.set_selected(thumb.version_n == version_n)
            if thumb.version_n == version_n:
                thumb.set_new_badge(False)
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
        Крестик скрыт на минимальной версии, на ВЫБРАННОЙ (selected) и на
        версии С ДЕТЬМИ (удаляем только листья дерева). Зовётся после смены
        selected без полного refresh (клик/зеркало)."""
        mn = getattr(self, '_min_version', 0)
        _kids = getattr(self, '_has_children_set', set())
        for thumb in self._thumbs:
            thumb.set_deletable(thumb.version_n != mn
                                and thumb.version_n != self._selected_version
                                and thumb.version_n not in _kids)

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
        # 2026-06-14 (Слой 2.3a): нельзя удалить АКТИВНУЮ версию (гард
        # delete_shot_version: n==active → -1). Если юзер кликнул ДРУГУЮ версию
        # (она selected) и жмёт крестик на активной — сперва делаем выбранную
        # активной СИНХРОННО (version_use_requested → MW._on_shot_version_use:
        # копия файла + active.txt + перерисовка грида + refresh попапа,
        # DirectConnection → отрабатывает ДО delete). Тогда version_n больше не
        # активна на диске и гард пропустит. Edge: selected пуст / selected ==
        # version_n / version_n != active → пред-активацию пропускаем (как
        # сейчас; крестик на selected и так скрыт, но подстраховка).
        if (int(version_n) == self._active_version
                and self._selected_version
                and self._selected_version != int(version_n)):
            self.version_use_requested.emit(
                self.panel_idx, self._selected_version)
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
        from storyboard_app import (list_shot_versions, read_active_version,
                                     build_shot_version_tree)
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
        # 2026-06-13 (дерево версий, Слой 2.2): порядок/depth/dotted из
        # build_shot_version_tree (наклейка поверх плоских файлов v{n}.jpg).
        # Плоский n остаётся ключом (клик/выбор/активная/удаление). Graceful
        # fallback на плоский список если дерево пустое/упало — лента не пропадёт.
        try:
            _tree = build_shot_version_tree(self.history_dir)
        except Exception:
            _tree = []
        if _tree:
            _nodes = _tree
        else:
            _nodes, _seen = [], set()
            for p in versions:
                try:
                    _fn = int(p.stem[1:])
                except (ValueError, IndexError):
                    continue
                if _fn in _seen:
                    continue
                _seen.add(_fn)
                _nodes.append({"n": _fn, "depth": 0, "dotted": None})
        # карта n → реальный путь картинки (по image-суффиксам; .prompt.txt
        # отсекается). Fallback на v{n}.jpg если в карте нет.
        _path_by_n = {}
        for p in versions:
            if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            try:
                _path_by_n[int(p.stem[1:])] = p
            except (ValueError, IndexError):
                continue
        # 2026-06-14 (Слой 2.3a): номера «с детьми» — крестик у них скрыт
        # (удаляем только листья). Пусто при плоском/упавшем дереве → все
        # листья → крестики как раньше.
        self._has_children_set = {nd["n"] for nd in _nodes if nd.get("has_children")}
        for node in _nodes:
            n = node["n"]
            img = _path_by_n.get(n) or (self.history_dir / f"v{n}.jpg")
            is_active = (n == self._active_version)
            can_delete = (n != self._min_version
                          and n != self._selected_version
                          and not node.get("has_children", False))
            thumb = VersionThumb(n, img, is_active, can_delete=can_delete,
                                 aspect=self._aspect,
                                 depth=node.get("depth", 0),
                                 dotted=node.get("dotted"))
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.delete_requested.connect(self._on_delete_version)
            # 2026-07-02: NEW на доборной версии — источник истины MW
            # (_unseen_versions), геттер держит refresh актуальным. parent()
            # диалога = MainWindow (создан parent=self в _open_shot_viewer).
            try:
                _mw = self.parent()
                if _mw is not None and hasattr(_mw, 'is_version_unseen'):
                    thumb.set_new_badge(
                        _mw.is_version_unseen(self.block_name, self.panel_idx, n))
            except Exception:
                pass
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
