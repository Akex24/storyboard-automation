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

import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QApplication, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsItem, QStyle,
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

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
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
            if isinstance(item, GridItem):
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
            else:
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)


class GridItem(QGraphicsPixmapItem):
    """Наложенная сетка на лице (Этап 6a). Перетаскиваемая (ItemIsMovable),
    с hover-подсветкой рамкой.

    Лежит в ТОЙ ЖЕ сцене что и сториборд → зум колесом и панорама вида двигают
    её ВМЕСТЕ с картинкой (сетка приклеена к лицу). Перетаскивание мышью меняет
    только её pos внутри сцены (в координатах = пикселях оригинала) — это и
    есть ручная корректировка положения. Этапы 6b/6c добавят сюда ручку
    ресайза и крестик удаления.
    """

    def __init__(self, pixmap, parent=None):
        super().__init__(pixmap, parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._hover = False

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()
        super().hoverEnterEvent(e)

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(e)

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
            self.view = StoryboardView(pix)
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
        self._face_boxes = []   # list[QGraphicsRectItem] — рамки подсветки лиц

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
        self.btn_save.clicked.connect(self._stub)
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
        """Убрать прошлые наложенные сетки и рамки лиц из сцены."""
        if self.view is None:
            return
        scene = self.view._scene
        for it in self._grid_items + self._face_boxes:
            try:
                scene.removeItem(it)
            except Exception:
                pass
        self._grid_items = []
        self._face_boxes = []

    def _on_apply(self):
        """«🔲 Наложить»: найти лица (YuNet) → на каждое положить активную
        сетку с запасом FACE_GRID_SCALE + рамка подсветки. Повторный клик
        очищает прошлые наложения и кладёт заново."""
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
        pen = QPen(QColor(228, 52, 74, 180))   # полупрозрачная рамка лица
        pen.setWidth(2)
        pen.setCosmetic(True)                  # толщина не зависит от зума
        for (x, y, w, h) in boxes:
            # Рамка подсветки найденного лица (для оценки детекции глазами).
            rect = scene.addRect(float(x), float(y), float(w), float(h), pen)
            rect.setZValue(20)
            self._face_boxes.append(rect)

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
            item = GridItem(grid_pix)   # перетаскиваемая + hover-подсветка (6a)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setOffset(-pw / 2.0, -ph / 2.0)   # origin = центр pixmap
            item.setScale(s)
            item.setPos(cx, cy)                    # центр сетки = центр лица
            item.setZValue(30)                     # поверх рамок
            scene.addItem(item)
            self._grid_items.append(item)

        self.hint_lbl.setText(tr('grid_applied', n=len(boxes)))

    def _stub(self):
        """Заглушка кнопки «Сохранить» — логика на Этапе 7."""
        self.hint_lbl.setText(tr('grid_stub_hint'))
