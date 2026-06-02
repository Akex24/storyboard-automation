# -*- coding: utf-8 -*-
"""widgets/face_grid/grid_dialog.py — попап наложения PNG-сеток на лица
склеенного сториборда блока.

Этап 3 (2026-06-02) — СКЕЛЕТ: показывает склеенный сториборд + кнопки-заглушки.
Этап 3.5 (2026-06-02) — просмотр на QGraphicsView/QGraphicsScene: зум колесом +
панорама перетаскиванием (StoryboardView), окно фиксированное, без скроллбара.
Логики наложения/сохранения пока НЕТ:
  • Этап 4 — UI библиотеки сеток (список/добавить/удалить, выбор активной);
  • Этап 5 — «Наложить»: YuNet находит лица → активная сетка на каждое;
  • Этап 6 — ручной drag/resize + ручная установка сетки;
  • Этап 7 — «Сохранить в рефы блока» (композит в .cache/_block_view/...).

Открывается из MainWindow._save_png ПОСЛЕ записи чистого <base>.jpg (вариант A):
чистый сториборд сохраняется как раньше (нужен «Собрать серию»), попап —
надстройка сверху.

Модальный (вызывается через .exec()) — самодостаточная задача, не теряется
за главным окном, нет рассинхрона с переключением блока (работает с файлом,
захваченным при открытии).

Контекст, который попап держит для будущих этапов:
  • stitched_path — путь к чистому <base>.jpg (Этап 5 детекция, Этап 7 база);
  • ep_id, block_n — для имени файла-результата и заголовка;
  • dest_dir — .cache/_block_view/<ep>_block<N>/ (Этап 7 — куда сохранять).

Cross-platform: только PyQt6 + QPixmap(str(path)). Без subprocess/shell/open.
"""

from __future__ import annotations

import json
import math
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QSize, QPointF, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush, QPainterPath
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QApplication, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsItem, QGraphicsRectItem, QStyle,
    QScrollArea, QWidget, QFrame, QFileDialog, QMessageBox,
)

from i18n import tr
from views.theme import lumz_button_qss
from widgets.face_grid import library

# 2026-06-02 (Этап 5): запас наложения сетки относительно бокса лица YuNet.
# YuNet даёт бокс по «ядру» лица (глаза-нос-рот), он уже самого лица (лоб/
# подбородок/уши за рамкой). 1.4 = +40% — сетка центрируется на лице и
# покрывает его с запасом. Подбирается на глаз — менять ОДНОЙ цифрой здесь.
FACE_GRID_SCALE = 1.4

# 2026-06-02 (Этап 6b): пределы scale сетки при ручном ресайзе за угол.
# Абсолютные множители относительно натурального размера PNG. Доп. нижний
# порог «не меньше ~16 px по большей стороне» считается динамически в ручке.
MIN_GRID_SCALE = 0.05
MAX_GRID_SCALE = 20.0

# 2026-06-02 (Этап 8): персист состояния наложенных сеток рядом со сторибордом
# в .cache/_block_view/<ep>_block<N>/grids.json (имя PNG + pos + scale). Имя
# файла продублировано литералом в storyboard_app._on_block_refs_btn (keep-
# условие cleanup'а) — при переименовании менять В ОБОИХ местах.
GRIDS_JSON_NAME = "grids.json"
GRIDS_JSON_SCHEMA = 1


class StoryboardView(QGraphicsView):
    """Просмотр склеенного сториборда: зум колесом (к курсору) + панорама
    перетаскиванием. База под Этап 6 — сетки добавляются КАК ЭЛЕМЕНТЫ в ту же
    сцену поверх картинки, просмотр не переделывается.

    Картинка кладётся в сцену в ПОЛНОМ разрешении → координаты сцены = пиксели
    оригинала 1:1. Это нужно Этапу 5 (боксы лиц YuNet в координатах оригинала
    ложатся в сцену без пересчёта) и Этапу 7 (композит в полном разрешении).
    Масштабирует только вид (fitInView/scale), не сам pixmap.
    """

    MIN_REL = 1.0   # не мельче «вписано в окно» (картинка впритык, не меньше)
    MAX_REL = 8.0   # не крупнее 8× от «вписано»

    def __init__(self, pixmap: QPixmap, parent=None, on_double_click=None):
        super().__init__(parent)
        # 2026-06-02 (Этап 6d): коллбэк «двойной клик по пустому месту» →
        # GridDialog._add_grid_at(scene_pos). None = ничего не делаем.
        self._on_double_add = on_double_click
        self._scene = QGraphicsScene(self)
        # pixmap_item — публичный: Этап 6 добавит сетки в self._scene поверх.
        self.pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self.pixmap_item.boundingRect())
        self.setScene(self._scene)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)  # тащить = панорама
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)        # зум к курсору
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setStyleSheet("border:1px solid #25193a; border-radius:6px;"
                           " background:#0a0612;")
        self._fit_scale = 1.0     # абсолютный scale при «вписано»
        self._fitted = False
        self._user_zoomed = False

    def _fit(self):
        """Вписать картинку в видимую область, запомнить базовый масштаб."""
        if self.pixmap_item is None:
            return
        self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.transform().m11() or 1.0
        self._fitted = True

    def showEvent(self, e):
        super().showEvent(e)
        if not self._fitted:
            self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Пока пользователь не зумил вручную — держим картинку вписанной
        # (на случай если финальный размер окна устаканился после showEvent).
        if not self._user_zoomed:
            self._fit()

    def wheelEvent(self, e):
        if self.pixmap_item is None:
            return
        step = 1.05 if e.angleDelta().y() > 0 else 1.0 / 1.05
        cur_rel = (self.transform().m11() / self._fit_scale
                   if self._fit_scale else 1.0)
        new_rel = cur_rel * step
        # Клампим в пределы [MIN_REL, MAX_REL] относительно «вписано».
        if new_rel < self.MIN_REL:
            step = self.MIN_REL / cur_rel
        elif new_rel > self.MAX_REL:
            step = self.MAX_REL / cur_rel
        if abs(step - 1.0) < 1e-3:
            return
        self._user_zoomed = True
        self.scale(step, step)

    # 2026-06-02 (Этап 6a): развести «тащу сетку» от «тащу фон».
    # ЛКМ по GridItem → NoDrag (Qt сам двигает movable-сетку);
    # ЛКМ по фону/сториборду → ScrollHandDrag (панорама вида).
    # На release всегда возвращаем ScrollHandDrag (дефолт = панорама).
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(e.position().toPoint())
            # GridItem → двигаем сетку; _ResizeHandle → ресайз; _DeleteHandle →
            # удаление (ловят клик сами). Всё это = NoDrag (вид не панорамим).
            if isinstance(item, (GridItem, _ResizeHandle, _DeleteHandle)):
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
            else:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    # 2026-06-02 (Этап 6d): двойной клик по ПУСТОМУ месту → поставить активную
    # сетку (для крупных планов, где YuNet лицо не нашёл). Двойной клик по уже
    # наложенной сетке / её ручке / крестику новую НЕ плодит. Базовый
    # pixmap_item (сам сториборд) — QGraphicsPixmapItem, но НЕ GridItem →
    # считается «пустым местом», сетка ставится поверх кадра.
    def mouseDoubleClickEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton
                and self._on_double_add is not None):
            item = self.itemAt(e.position().toPoint())
            if not isinstance(item, (GridItem, _ResizeHandle, _DeleteHandle)):
                scene_pos = self.mapToScene(e.position().toPoint())
                self._on_double_add(scene_pos)
                e.accept()
                return
        super().mouseDoubleClickEvent(e)


class _ResizeHandle(QGraphicsRectItem):
    """Угловая ручка ресайза (Этап 6b) — дочерний item у GridItem в правом-
    нижнем углу. ItemIgnoresTransformations → постоянный экранный размер (~16px),
    не растёт при зуме вида / scale родителя. Тянем ручку → пересчитываем
    scale родителя ОТ ЦЕНТРА (pos родителя не трогаем → центр на лице стоит).
    Не движется сама и не двигает тело сетки — только меняет scale.
    """

    SIZE = 16     # экранный размер ручки (px)
    INSET = 3     # отступ от угла ВНУТРЬ сетки (px)

    def __init__(self, parent):
        # Якорь ручки (pos) ставится в правый-нижний угол сетки (pw/2, ph/2)
        # в GridItem. Сам прямоугольник рисуем в ВЕРХНЕ-ЛЕВОМ квадранте от
        # якоря (отрицательные координаты) + INSET → ручка целиком ВНУТРИ
        # квадрата сетки у внутреннего угла. Координаты прямоугольника — в
        # ЭКРАННЫХ px (ItemIgnoresTransformations), поэтому отступ не уедет
        # при зуме. Логика ресайза (от центра по scenePos) этим не затронута.
        super().__init__(-(self.SIZE + self.INSET), -(self.SIZE + self.INSET),
                         self.SIZE, self.SIZE, parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(QColor(110, 76, 196)))
        pen = QPen(QColor(255, 255, 255))
        pen.setWidth(1)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setZValue(40)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def mousePressEvent(self, e):
        p = self.parentItem()
        if p is not None:
            p.setSelected(True)   # держим ручку видимой во время ресайза
        e.accept()

    def mouseMoveEvent(self, e):
        p = self.parentItem()
        if p is None:
            return
        center = p.pos()          # scene-координаты центра (offset центрирует origin)
        cur = e.scenePos()
        dist = math.hypot(cur.x() - center.x(), cur.y() - center.y())
        hd = getattr(p, "_half_diag", 0.0) or 1.0
        s = dist / hd
        pm = p.pixmap()
        mx = max(pm.width(), pm.height()) or 1
        s_min = max(MIN_GRID_SCALE, 16.0 / mx)   # не мельче ~16px по большей стороне
        s = max(s_min, min(s, MAX_GRID_SCALE))
        p.setScale(s)             # pos НЕ трогаем → центр не уезжает
        e.accept()

    def mouseReleaseEvent(self, e):
        e.accept()


class _DeleteHandle(QGraphicsRectItem):
    """Крестик удаления (Этап 6c) — дочерний item у GridItem в ЛЕВОМ-ВЕРХНЕМ
    углу (зеркально ручке ресайза). ItemIgnoresTransformations → постоянный
    экранный размер. Красный (чтобы не путать с фиолетовой ручкой). Клик →
    parent._request_delete() (убрать эту сетку). Drag/ресайз не трогает.
    """

    SIZE = 16     # экранный размер (px)
    INSET = 3     # отступ от угла ВНУТРЬ сетки (px)

    def __init__(self, parent):
        # Якорь (pos) — левый-верхний угол сетки (-pw/2, -ph/2) в GridItem.
        # Прямоугольник в ПРАВОМ-НИЖНЕМ квадранте от якоря (+INSET) → крестик
        # целиком ВНУТРИ квадрата у внутреннего угла. Координаты — экранные px.
        super().__init__(self.INSET, self.INSET, self.SIZE, self.SIZE, parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setBrush(QBrush(QColor(228, 52, 74)))   # LUMZ red
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setZValue(41)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(tr('grid_del_tooltip'))

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)   # красный фон
        r = self.rect()
        pen = QPen(QColor(255, 255, 255))
        pen.setWidth(2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        m = 4
        painter.drawLine(QPointF(r.left() + m, r.top() + m),
                         QPointF(r.right() - m, r.bottom() - m))
        painter.drawLine(QPointF(r.left() + m, r.bottom() - m),
                         QPointF(r.right() - m, r.top() + m))

    def mousePressEvent(self, e):
        p = self.parentItem()
        if p is not None and hasattr(p, "_request_delete"):
            p._request_delete()
        e.accept()


class GridItem(QGraphicsPixmapItem):
    """Наложенная сетка на лице. Перетаскиваемая (6a) + ручка ресайза в углу (6b).

    Само-центрирование: origin = центр pixmap (setOffset), поэтому setScale
    масштабирует ВОКРУГ ЦЕНТРА (центр на лице не уезжает ни при drag, ни при
    ресайзе). Лежит в той же сцене что сториборд → зум/панорама вида двигают
    её вместе с картинкой. Координаты pos/scale — в пикселях оригинала (для
    Этапа 7 композита).
    """

    def __init__(self, pixmap, parent=None, on_delete=None, src_path=None):
        super().__init__(pixmap, parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._hover = False
        # Коллбэк удаления (bound-метод диалога). Обнуляется в
        # GridDialog._remove_grid_item при удалении → ссылка item→dialog
        # разрывается, item освобождается (без висячего цикла).
        self._on_delete = on_delete
        # 2026-06-02 (Этап 7): путь к PNG-сетке в библиотеке. При сохранении
        # композит читает альфу из ЭТОГО файла (полное разрешение), а не из
        # экранного QPixmap. None → сетка пропускается при впечатывании.
        self._src_path = src_path

        pm = self.pixmap()
        pw, ph = pm.width(), pm.height()
        # Центр pixmap = origin (0,0) → setScale вокруг центра.
        self.setOffset(-pw / 2.0, -ph / 2.0)
        self._half_diag = math.hypot(pw / 2.0, ph / 2.0)

        # Ручка ресайза — правый-нижний угол; крестик удаления — левый-верхний.
        # При scale родителя их local-pos неизменна, scene-позиция следует за
        # углом (ItemIgnoresTransformations держит экранный размер).
        self._handle = _ResizeHandle(self)
        self._handle.setPos(pw / 2.0, ph / 2.0)
        self._handle.setVisible(False)
        self._del = _DeleteHandle(self)
        self._del.setPos(-pw / 2.0, -ph / 2.0)
        self._del.setVisible(False)

    def shape(self):
        """2026-06-02 (6b-fix): хитбокс = ВЕСЬ прямоугольник pixmap, а не контур
        по альфа-каналу (дефолт QGraphicsPixmapItem). Иначе hover/drag сетки
        ловятся только на непрозрачных линиях PNG, а в прозрачных клетках
        событие проваливается на фон. boundingRect() уже учитывает setOffset
        (центрирование), поэтому хитбокс совпадает с видимым квадратом 1:1."""
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def _sync_overlays(self):
        """Показ/скрытие ручки ресайза И крестика удаления (вместе)."""
        vis = self._hover or self.isSelected()
        self._handle.setVisible(vis)
        self._del.setVisible(vis)

    def _request_delete(self):
        """Зов из крестика — попросить диалог удалить эту сетку."""
        if self._on_delete is not None:
            self._on_delete(self)

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()
        self._sync_overlays()
        super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()
        self._sync_overlays()
        super().hoverLeaveEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self._sync_overlays()
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        # Гасим дефолтную пунктирную рамку выделения Qt — рисуем свою.
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if self._hover or self.isSelected():
            pen = QPen(QColor(110, 76, 196))   # LUMZ accent
            pen.setWidth(2)
            pen.setCosmetic(True)              # постоянная толщина при зуме
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.boundingRect())


class _GridThumb(QFrame):
    """Кликабельная миниатюра PNG-сетки в ленте (Этап 4). Клик = выбрать
    активной; маленький «×» в углу = удалить из библиотеки."""

    clicked = pyqtSignal(str)           # имя файла сетки
    delete_requested = pyqtSignal(str)  # имя файла сетки
    THUMB = 48

    def __init__(self, path: Path, is_active: bool, parent=None):
        super().__init__(parent)
        self.name = path.name
        self._active = is_active
        self.setObjectName("GridThumb")
        self.setFixedSize(self.THUMB + 10, self.THUMB + 10)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(self.name)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        img = QLabel()
        img.setFixedSize(self.THUMB, self.THUMB)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(str(path))
        if not pix.isNull():
            img.setPixmap(pix.scaled(
                QSize(self.THUMB, self.THUMB),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(img, alignment=Qt.AlignmentFlag.AlignCenter)

        # «×» удаления — дочерний QPushButton (нативно потребляет клик, не
        # триггерит выбор миниатюры). Позиционируем абсолютно в правый угол.
        self.del_btn = QPushButton("×", self)
        self.del_btn.setObjectName("grid-thumb-del")
        self.del_btn.setFixedSize(16, 16)
        self.del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_btn.setToolTip(tr('grid_del_tooltip'))
        self.del_btn.move(self.width() - 18, 2)
        self.del_btn.clicked.connect(
            lambda: self.delete_requested.emit(self.name))

        self._refresh_style()

    def set_active(self, active: bool):
        self._active = active
        self._refresh_style()

    def _refresh_style(self):
        border = ("2px solid #e4344a" if self._active
                  else "1px solid #322545")
        bg = "#231840" if self._active else "transparent"
        self.setStyleSheet(
            f"#GridThumb {{ background:{bg}; border:{border};"
            f" border-radius:6px; }}"
            "QPushButton#grid-thumb-del {"
            " background:rgba(10,10,13,0.7); color:#fff; border:none;"
            " border-radius:8px; font-size:12px; font-weight:600; }"
            "QPushButton#grid-thumb-del:hover { background:#e4344a; }")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.name)
        super().mousePressEvent(ev)


class GridDialog(QDialog):
    """Попап наложения сеток на лица (Этапы 3/3.5/4)."""

    def __init__(self, stitched_path, ep_id: str, block_n: int,
                 dest_dir, parent=None):
        super().__init__(parent)
        # ── Контекст для будущих этапов (5/7) ──
        self.stitched_path = Path(stitched_path)
        self.ep_id = str(ep_id)
        self.block_n = int(block_n)
        self.dest_dir = Path(dest_dir)

        self.setWindowTitle(
            tr('grid_dialog_title', block=f"{self.ep_id}_block{self.block_n}"))
        self.setModal(True)

        # Адаптивный размер под родительское окно (как ShotViewerDialog).
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            pw, ph = parent_win.width(), parent_win.height()
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            pw, ph = geo.width(), geo.height()
        # Окно ФИКСИРОВАННОЕ (без resize/скроллбара): просмотр внутри —
        # зум колесом + панорама перетаскиванием (StoryboardView).
        win_w = min(1100, max(700, int(pw * 0.95)))
        win_h = min(760, max(500, int(ph * 0.95)))
        self.setFixedSize(win_w, win_h)

        self._build()

    def _build(self):
        self.setStyleSheet(
            "QDialog { background:#0a0a0d; }"
            "QLabel#hint { color:rgba(255,255,255,0.55); font-size:11px; }"
            "QLabel#empty { color:rgba(255,255,255,0.40);"
            " font-style:italic; font-size:13px; }"
            + lumz_button_qss('subtle', 'grid_btn_add')
            + lumz_button_qss('primary', 'grid_btn_apply')
            + lumz_button_qss('secondary', 'grid_btn_save')
            + lumz_button_qss('subtle', 'grid_btn_close')
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # ── Просмотр склеенного сториборда: зум колесом + панорама ──
        # Полноразмерный pixmap в сцену (координаты сцены = пиксели оригинала).
        # self.view.pixmap_item / self.view._scene — база под Этап 6 (сетки).
        pix = QPixmap(str(self.stitched_path))
        self.view = None
        if not pix.isNull():
            self.view = StoryboardView(pix, on_double_click=self._add_grid_at)
            lay.addWidget(self.view, stretch=1)
        else:
            empty = QLabel(tr('grid_no_image'))
            empty.setObjectName("empty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(empty, stretch=1)

        # ── Лента сеток (Этап 4): «+ Добавить» + горизонтальный скролл миниатюр ──
        grids_row = QHBoxLayout()
        grids_row.setSpacing(8)
        self.btn_add = QPushButton(tr('grid_btn_add'))
        self.btn_add.setObjectName("grid_btn_add")
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.clicked.connect(self._on_add_grid)
        grids_row.addWidget(self.btn_add, alignment=Qt.AlignmentFlag.AlignTop)

        self._grids_scroll = QScrollArea()
        self._grids_scroll.setWidgetResizable(True)
        self._grids_scroll.setFixedHeight(_GridThumb.THUMB + 16)
        self._grids_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._grids_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._grids_scroll.setStyleSheet(
            "QScrollArea { border:1px solid #25193a; border-radius:6px;"
            " background:#0a0612; }")
        strip = QWidget()
        self._grids_strip = QHBoxLayout(strip)
        self._grids_strip.setContentsMargins(6, 2, 6, 2)
        self._grids_strip.setSpacing(8)
        self._grids_empty_lbl = QLabel(tr('grid_empty_hint'))
        self._grids_empty_lbl.setObjectName("empty")
        self._grids_strip.addWidget(self._grids_empty_lbl)
        self._grids_strip.addStretch()
        self._grids_scroll.setWidget(strip)
        grids_row.addWidget(self._grids_scroll, stretch=1)
        lay.addLayout(grids_row)
        self._thumbs = []
        # Наложенные элементы поверх сцены (Этап 5). Координаты — в пикселях
        # оригинала (= координаты сцены). Этап 6 сделает их movable/resizable,
        # Этап 7 впечатает по pos/scale. Single source of truth — сами элементы.
        self._grid_items = []   # list[QGraphicsPixmapItem] — наложенные сетки

        # ── Хинт-строка: подсказка управления + фидбэк заглушек ──
        self.hint_lbl = QLabel(tr('grid_view_hint'))
        self.hint_lbl.setObjectName("hint")
        lay.addWidget(self.hint_lbl)

        # ── Кнопки ──
        actions = QHBoxLayout()
        actions.setSpacing(10)

        self.btn_apply = QPushButton(tr('grid_btn_apply'))
        self.btn_apply.setObjectName("grid_btn_apply")
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.clicked.connect(self._on_apply)
        actions.addWidget(self.btn_apply)

        self.btn_save = QPushButton(tr('grid_btn_save'))
        self.btn_save.setObjectName("grid_btn_save")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self._on_save)
        actions.addWidget(self.btn_save)

        actions.addStretch()

        self.btn_close = QPushButton(tr('grid_btn_close'))
        self.btn_close.setObjectName("grid_btn_close")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        actions.addWidget(self.btn_close)

        lay.addLayout(actions)

        # Первичное наполнение ленты сеток из библиотеки.
        self._refresh_grids()
        # Восстановить ранее сохранённые сетки (Этап 8) — живыми GridItem
        # поверх ЧИСТОЙ базы (stitch пересобрал jpg до попапа → задвоения нет).
        self._restore_grids()

    # ── Лента сеток (Этап 4) ────────────────────────────────────────────
    def _refresh_grids(self):
        """Перерисовать ленту из library.list_grids(); подсветить активную."""
        # Снять старые миниатюры.
        for t in getattr(self, '_thumbs', []):
            try:
                t.setParent(None)
                t.deleteLater()
            except Exception:
                pass
        self._thumbs = []

        grids = library.list_grids()
        active = library.get_active_grid_name()
        self._grids_empty_lbl.setVisible(not grids)

        # Вставляем миниатюры ПЕРЕД stretch (последний элемент layout).
        insert_at = self._grids_strip.count() - 1
        for p in grids:
            thumb = _GridThumb(p, is_active=(p.name == active))
            thumb.clicked.connect(self._on_pick_grid)
            thumb.delete_requested.connect(self._on_delete_grid)
            self._grids_strip.insertWidget(insert_at, thumb)
            insert_at += 1
            self._thumbs.append(thumb)

        # Хинт: активная сетка или подсказка управления.
        if active:
            self.hint_lbl.setText(tr('grid_active_lbl', name=active))
        else:
            self.hint_lbl.setText(tr('grid_view_hint'))

    def _on_pick_grid(self, name: str):
        """Клик по миниатюре — сделать активной."""
        library.set_active_grid(name)
        for t in self._thumbs:
            t.set_active(t.name == name)
        self.hint_lbl.setText(tr('grid_active_lbl', name=name))

    def _on_add_grid(self):
        """«+ Добавить» — выбрать PNG (любая папка) → скопировать в библиотеку,
        сделать активной, обновить ленту."""
        path, _ = QFileDialog.getOpenFileName(
            self, tr('grid_picker_caption'), "", "PNG (*.png)")
        if not path:
            return
        try:
            dest = library.add_grid(path)
        except Exception as e:
            QMessageBox.warning(self, tr('grid_btn_add'), str(e))
            return
        library.set_active_grid(dest.name)
        self._refresh_grids()

    def _on_delete_grid(self, name: str):
        """«×» на миниатюре — удалить из библиотеки (с подтверждением)."""
        if QMessageBox.question(
                self, tr('grid_del_tooltip'),
                tr('grid_delete_confirm', name=name)
        ) != QMessageBox.StandardButton.Yes:
            return
        library.delete_grid(name)
        self._refresh_grids()

    # ── Наложение сеток (Этап 5) ────────────────────────────────────────
    def _clear_overlays(self):
        """Убрать прошлые наложенные сетки из сцены."""
        if self.view is None:
            return
        scene = self.view._scene
        for it in self._grid_items:
            it._on_delete = None   # разрываем item→dialog перед удалением
        for it in self._grid_items:
            try:
                scene.removeItem(it)
            except Exception:
                pass
        self._grid_items = []

    def _on_apply(self):
        """«🔲 Наложить»: найти лица (YuNet) → на каждое положить активную
        сетку с запасом FACE_GRID_SCALE. Повторный клик очищает прошлые
        наложения и кладёт заново."""
        if self.view is None:
            return
        grid_path = library.get_active_grid()
        if not grid_path:
            self.hint_lbl.setText(tr('grid_no_active'))
            return

        # Индикатор: детекция (особенно первый вызов — загрузка модели) может
        # занять доли секунды. Показываем «Ищу лица…» и сразу перерисовываем.
        self.hint_lbl.setText(tr('grid_searching'))
        QApplication.processEvents()
        try:
            from widgets.face_grid.detector import detect_faces
            boxes = detect_faces(self.stitched_path)
        except Exception:
            traceback.print_exc()
            boxes = []

        self._clear_overlays()
        if not boxes:
            self.hint_lbl.setText(tr('grid_no_faces'))
            return

        scene = self.view._scene
        grid_pix = QPixmap(str(grid_path))
        for (x, y, w, h) in boxes:
            if grid_pix.isNull():
                continue
            # Сетка с запасом: расширяем бокс на FACE_GRID_SCALE, вписываем
            # PNG так чтобы ПОКРЫТЬ расширенный бокс (max-ratio), центрируем
            # на центре лица. Пропорции PNG сохраняются (uniform scale).
            cx, cy = x + w / 2.0, y + h / 2.0
            tw, th = w * FACE_GRID_SCALE, h * FACE_GRID_SCALE
            pw, ph = grid_pix.width(), grid_pix.height()
            if pw <= 0 or ph <= 0:
                continue
            s = max(tw / pw, th / ph)
            item = GridItem(grid_pix, on_delete=self._remove_grid_item,
                            src_path=grid_path)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setScale(s)
            item.setPos(cx, cy)                    # центр сетки = центр лица
            item.setZValue(30)                     # поверх рамок
            scene.addItem(item)
            self._grid_items.append(item)

        self.hint_lbl.setText(tr('grid_applied', n=len(boxes)))

    # ── Ручная установка сетки двойным кликом (Этап 6d) ──────────────────
    def _add_grid_at(self, scene_pos):
        """Положить активную сетку центром в точку scene_pos (зов из
        StoryboardView.mouseDoubleClickEvent по пустому месту). Стартовый
        масштаб: большая сторона ≈ 1/6 ширины сториборда (бокса лица нет —
        ориентируемся на кадр), затем clamp в [MIN_GRID_SCALE, MAX_GRID_SCALE].
        Юзер подгонит ручкой ресайза (6b). Ставится выделенной — ручка и
        крестик видны сразу."""
        if self.view is None:
            return
        grid_path = library.get_active_grid()
        if not grid_path:
            self.hint_lbl.setText(tr('grid_no_active'))
            return
        grid_pix = QPixmap(str(grid_path))
        if grid_pix.isNull():
            return

        pw, ph = grid_pix.width(), grid_pix.height()
        larger = max(pw, ph)
        if larger <= 0:
            return
        target = self.view._scene.sceneRect().width() / 6.0
        s = target / larger
        s = max(MIN_GRID_SCALE, min(s, MAX_GRID_SCALE))

        item = GridItem(grid_pix, on_delete=self._remove_grid_item,
                        src_path=grid_path)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setScale(s)
        item.setPos(scene_pos)          # центр сетки = точка клика (offset центрирует)
        item.setZValue(30)
        self.view._scene.addItem(item)
        self._grid_items.append(item)
        item.setSelected(True)          # сразу показать ручку ресайза + крестик
        self.hint_lbl.setText(tr('grid_applied', n=len(self._grid_items)))

    def _remove_grid_item(self, item):
        """Удалить ОДНУ сетку: из сцены + из списка + разорвать ссылку
        item→dialog (обнулить _on_delete), чтобы объект освободился без цикла.
        Зовётся из крестика (_DeleteHandle) и по Delete/Backspace."""
        try:
            if self.view is not None and item.scene() is not None:
                self.view._scene.removeItem(item)
        except Exception:
            traceback.print_exc()
        if item in self._grid_items:
            self._grid_items.remove(item)
        item._on_delete = None   # разрываем item→dialog (нет висячего цикла)

    def keyPressEvent(self, e):
        """Delete/Backspace — удалить выделенные сетки (бонус Этапа 6c)."""
        if (e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self.view is not None):
            for it in list(self.view._scene.selectedItems()):
                if isinstance(it, GridItem):
                    self._remove_grid_item(it)
            e.accept()
            return
        super().keyPressEvent(e)

    # ── Персист состояния сеток (Этап 8) ────────────────────────────────
    def _restore_grids(self):
        """Восстановить ранее сохранённые сетки живыми GridItem поверх ЧИСТОГО
        сториборда. Источник — dest_dir/grids.json. PNG резолвится по ИМЕНИ
        через library (переживает смену машины). Нет PNG в библиотеке / битый
        pixmap → пропуск + счётчик, не падаем. Отсутствующий/битый json →
        старт с чистого листа. board_w/h → sanity-check (хинт при расхождении)."""
        if self.view is None:
            return
        path = self.dest_dir / GRIDS_JSON_NAME
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            traceback.print_exc()
            return
        grids = data.get("grids") if isinstance(data, dict) else None
        if not isinstance(grids, list) or not grids:
            return

        scene = self.view._scene
        restored = 0
        skipped = 0
        for g in grids:
            if not isinstance(g, dict):
                skipped += 1
                continue
            src = library.get_grid_path(g.get("png") or "")
            if src is None:                 # PNG удалён из библиотеки
                skipped += 1
                continue
            pix = QPixmap(str(src))
            if pix.isNull():
                skipped += 1
                continue
            try:
                cx = float(g.get("cx"))
                cy = float(g.get("cy"))
                s = float(g.get("scale", 1.0))
            except (TypeError, ValueError):
                skipped += 1
                continue
            item = GridItem(pix, on_delete=self._remove_grid_item, src_path=src)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setScale(s)
            item.setPos(cx, cy)
            item.setZValue(30)
            scene.addItem(item)
            self._grid_items.append(item)
            restored += 1

        if not restored and not skipped:
            return
        # Sanity-check размера листа: координаты абсолютные, при смене размера
        # (шот пересоздан в другом разрешении) могли уехать — восстанавливаем,
        # но предупреждаем. Приоритет хинта: размер > пропуски > обычный.
        size_warn = False
        try:
            bw, bh = data.get("board_w"), data.get("board_h")
            rect = scene.sceneRect()
            if (bw and bh and (int(bw) != int(round(rect.width()))
                               or int(bh) != int(round(rect.height())))):
                size_warn = True
        except Exception:
            pass

        if size_warn:
            self.hint_lbl.setText(tr('grid_restored_resized', n=restored))
        elif skipped:
            self.hint_lbl.setText(
                tr('grid_restored_skipped', n=restored, m=skipped))
        else:
            self.hint_lbl.setText(tr('grid_restored', n=restored))

    def _write_grids_json(self):
        """Сохранить состояние наложенных сеток в dest_dir/grids.json (Этап 8):
        имя PNG (не путь) + центр (пиксели оригинала = pos) + scale; board_w/h
        для sanity-check. Не-фатально: ошибка json НЕ отменяет уже записанный
        jpg (лог в stderr). Только сетки с валидным _src_path (как в композите)."""
        if self.view is None:
            return
        rect = self.view._scene.sceneRect()
        grids = []
        for item in self._grid_items:
            src = getattr(item, "_src_path", None)
            if not src:
                continue
            pos = item.pos()
            grids.append({
                "png": Path(src).name,
                "cx": round(pos.x(), 2),
                "cy": round(pos.y(), 2),
                "scale": round(item.scale(), 4),
            })
        data = {
            "schema": GRIDS_JSON_SCHEMA,
            "board_w": int(round(rect.width())),
            "board_h": int(round(rect.height())),
            "grids": grids,
        }
        try:
            (self.dest_dir / GRIDS_JSON_NAME).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            traceback.print_exc()

    def _delete_grids_json(self):
        """Удалить grids.json — ноль сеток при сохранении, чтобы повторное
        открытие не восстановило устаревшее состояние. Не падаем если нет."""
        try:
            p = self.dest_dir / GRIDS_JSON_NAME
            if p.exists():
                p.unlink()
        except Exception:
            traceback.print_exc()

    # ── Сохранение композита в рефы блока (Этап 7) ──────────────────────
    def _on_save(self):
        """«💾 Сохранить в рефы блока»: впечатать наложенные сетки в чистый
        сториборд и перезаписать <ep>_block<N>.jpg (вариант A — тот же файл
        читают «Рефы блока» и «Собрать серию»).

        Координаты: сцена = пиксели оригинала 1:1; origin GridItem = центр
        (setOffset) → левый-верхний угол = центр − (pw·scale/2, ph·scale/2).
        Источник каждой сетки — её PNG из библиотеки (`_src_path`, полная
        альфа), НЕ экранный QPixmap. Края клипаются paste'ом (не падает).
        Декод/энкод через PIL (str-пути) — cv2 упал бы на русских путях.

        Ноль сеток → файл НЕ трогаем (он уже чистый: stitch отработал перед
        попапом), просто закрываем. Ошибка записи → попап НЕ закрываем."""
        if self.view is None:
            self.reject()
            return
        if not self._grid_items:
            self._delete_grids_json()   # ноль сеток → убрать устаревшее состояние
            self.hint_lbl.setText(tr('grid_save_empty'))
            self.accept()
            return

        self.hint_lbl.setText(tr('grid_saving'))
        QApplication.processEvents()
        try:
            from PIL import Image as PILImage
            base = PILImage.open(str(self.stitched_path)).convert("RGBA")
            layer = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
            painted = 0
            for item in self._grid_items:
                src = getattr(item, "_src_path", None)
                if not src:
                    continue
                pm = item.pixmap()
                pw, ph = pm.width(), pm.height()
                s = item.scale()
                tw, th = max(1, round(pw * s)), max(1, round(ph * s))
                center = item.pos()
                left = round(center.x() - pw * s / 2.0)
                top = round(center.y() - ph * s / 2.0)
                grid = PILImage.open(str(src)).convert("RGBA")
                grid = grid.resize((tw, th), PILImage.LANCZOS)
                # paste молча клипает отрицательные / выходящие за холст края.
                layer.paste(grid, (left, top), grid)
                painted += 1
            out = PILImage.alpha_composite(base, layer).convert("RGB")
            out.save(str(self.stitched_path), format="JPEG", quality=95)
        except Exception as e:
            traceback.print_exc()
            self.hint_lbl.setText(tr('grid_save_error', err=str(e)))
            QMessageBox.warning(self, tr('grid_btn_save'), str(e))
            return          # НЕ закрываем — юзер не теряет наложенные сетки
        # Композит записан → персист состояния (Этап 8). Отдельный не-фатальный
        # путь: упавший json не маскирует успешно сохранённый jpg.
        self._write_grids_json()
        self.hint_lbl.setText(tr('grid_saved', n=painted))
        self.accept()

    def _stub(self):
        """Заглушка кнопки «Сохранить» — логика на Этапе 7."""
        self.hint_lbl.setText(tr('grid_stub_hint'))
