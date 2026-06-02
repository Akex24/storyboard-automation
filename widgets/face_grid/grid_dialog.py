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

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QApplication, QGraphicsView, QGraphicsScene,
)

from i18n import tr
from views.theme import lumz_button_qss


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


class GridDialog(QDialog):
    """Попап наложения сеток на лица (скелет — Этап 3)."""

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
            + lumz_button_qss('subtle', 'grid_btn_pick')
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

        # ── Хинт-строка: подсказка управления + фидбэк заглушек ──
        self.hint_lbl = QLabel(tr('grid_view_hint'))
        self.hint_lbl.setObjectName("hint")
        lay.addWidget(self.hint_lbl)

        # ── Кнопки ──
        actions = QHBoxLayout()
        actions.setSpacing(10)

        # Заглушки (логика — Этапы 4/5/7). Клик → хинт, без действия.
        self.btn_pick = QPushButton(tr('grid_btn_pick'))
        self.btn_pick.setObjectName("grid_btn_pick")
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.clicked.connect(self._stub)
        actions.addWidget(self.btn_pick)

        self.btn_apply = QPushButton(tr('grid_btn_apply'))
        self.btn_apply.setObjectName("grid_btn_apply")
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.clicked.connect(self._stub)
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

    def _stub(self):
        """Заглушка кнопок Этапа 3 — логика появится на Этапах 4/5/7."""
        self.hint_lbl.setText(tr('grid_stub_hint'))
