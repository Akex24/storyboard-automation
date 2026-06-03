# -*- coding: utf-8 -*-
"""widgets/face_grid/actor_grid_dialog.py — попап наложения PNG-сеток на лица
РЕФЕРЕНСА АКТЁРА (страница «Актёры» → «Все референсы» → «🔲 Сетка»).

Этап актёрской сетки (2026-06-03). Независимый класс — сторибордовый
`GridDialog` (widgets/face_grid/grid_dialog.py) НЕ трогается. Переиспользуем
готовые кирпичики из grid_dialog.py импортом:
  • StoryboardView  — зум колесом + панорама + дабл-клик;
  • GridItem        — наложенная сетка (drag/resize/delete, центр-origin);
  • _GridThumb      — миниатюра PNG-сетки в ленте библиотеки;
  • FACE_GRID_SCALE / MIN_GRID_SCALE / MAX_GRID_SCALE — пределы.
Плюс `library` (персистентная библиотека PNG-сеток) и `detector.detect_faces`.

ОТЛИЧИЯ от сторибордового GridDialog:
  1. Источник картинки и цель сохранения РАЗНЫЕ:
       • image_path  — чистый реф `refs/characters/<slug>/<name>.jpg` (НЕ
         перезаписывается — показывается и служит базой композита);
       • save_path   — `refs/characters_grid/<slug>/<stem>_grid.jpg` (СЮДА
         пишется композит, отдельный файл; оригинал цел).
  2. Персист расстановки — рядом с save_path: `<stem>_grid.json` (имя/pos/scale
     сеток). Имя завязано на stem рефа → рефы одного персонажа не конфликтуют.
  3. Поведение при открытии:
       • json НЕТ (первое открытие) → АВТО-детект лиц + наложение активной сетки;
       • json ЕСТЬ → восстановить расстановку ровно как сохранил юзер, БЕЗ
         повторного авто-детекта.

Cross-platform: PyQt6 + PIL (str-пути) + detector (PIL→numpy, не cv2.imread).
Без subprocess/shell. К UI подключается отдельно (кнопка в ActorPhotosDialog).
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QSize, QPoint, QEvent
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QApplication, QScrollArea, QWidget, QFrame, QFileDialog, QMessageBox,
)

from i18n import tr
from views.theme import lumz_button_qss
from widgets.face_grid import library
# Переиспользуемые кирпичики сторибордового диалога (его самого НЕ трогаем).
from widgets.face_grid.grid_dialog import (
    StoryboardView, GridItem, _GridThumb,
    FACE_GRID_SCALE, MIN_GRID_SCALE, MAX_GRID_SCALE,
)

# Версия схемы persist-файла позиций (на будущее, как у сторибордового).
ACTOR_GRID_JSON_SCHEMA = 1


class ActorGridDialog(QDialog):
    """Попап наложения сеток на лица референса актёра.

    Конструктор:
      • image_path — путь к чистому рефу (показывается, база композита);
      • save_path  — куда писать композит-с-сеткой (отдельный файл);
      • title      — имя для заголовка окна (актёр / реф);
      • parent     — для адаптивного размера окна.
    Persist позиций — `save_path` с расширением `.json` (тот же stem).
    """

    def __init__(self, image_path, save_path, title: str = "", parent=None):
        super().__init__(parent)
        self.image_path = Path(image_path)
        self.save_path = Path(save_path)
        self._grid_json = self.save_path.with_suffix(".json")
        self._title = str(title or "")

        self.setWindowTitle(tr('actor_grid_dialog_title', name=self._title))
        self.setModal(True)

        # Адаптивный размер под родительское окно (как GridDialog).
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            pw, ph = parent_win.width(), parent_win.height()
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            pw, ph = geo.width(), geo.height()
        win_w = min(1100, max(700, int(pw * 0.95)))
        win_h = min(760, max(500, int(ph * 0.95)))
        self.setFixedSize(win_w, win_h)

        self._grid_items = []
        self._build()

    # ── UI ──────────────────────────────────────────────────────────────
    def _build(self):
        self.setStyleSheet(
            "QDialog { background:#0a0a0d; }"
            "QLabel#hint { color:rgba(255,255,255,0.55); font-size:11px; }"
            "QLabel#empty { color:rgba(255,255,255,0.40);"
            " font-style:italic; font-size:13px; }"
            + lumz_button_qss('subtle', 'grid_btn_add')
            + lumz_button_qss('primary', 'grid_btn_apply')
            + lumz_button_qss('secondary', 'grid_btn_save')
            + lumz_button_qss('subtle', 'grid_btn_help')
            + lumz_button_qss('subtle', 'grid_btn_close')
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # Просмотр рефа: полноразмерный pixmap в сцену (координаты = пиксели 1:1).
        pix = QPixmap(str(self.image_path))
        self.view = None
        if not pix.isNull():
            self.view = StoryboardView(pix, on_double_click=self._add_grid_at)
            lay.addWidget(self.view, stretch=1)
        else:
            empty = QLabel(tr('grid_no_image'))
            empty.setObjectName("empty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(empty, stretch=1)

        # Лента сеток: «+ Добавить» + горизонтальный скролл миниатюр.
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

        # Хинт-строка (динамический статус). Жесты — в help-панели по кнопке.
        self.hint_lbl = QLabel("")
        self.hint_lbl.setObjectName("hint")
        lay.addWidget(self.hint_lbl)

        # Кнопки.
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

        self.btn_help = QPushButton(tr('grid_help_btn'))
        self.btn_help.setObjectName("grid_btn_help")
        self.btn_help.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_help.clicked.connect(self._toggle_help)
        actions.addWidget(self.btn_help)

        self.btn_close = QPushButton(tr('grid_btn_close'))
        self.btn_close.setObjectName("grid_btn_close")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        actions.addWidget(self.btn_close)

        lay.addLayout(actions)

        self._refresh_grids()
        # Поведение открытия: есть сохранённая расстановка → восстановить её
        # (без авто-детекта); нет → авто-детект лиц + наложение.
        if self._grid_json.exists():
            self._restore_grids()
        else:
            self._on_apply()
        self._help_panel = self._build_help_panel()

    # ── Лента сеток ─────────────────────────────────────────────────────
    def _refresh_grids(self):
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

        insert_at = self._grids_strip.count() - 1
        for p in grids:
            thumb = _GridThumb(p, is_active=(p.name == active))
            thumb.clicked.connect(self._on_pick_grid)
            thumb.delete_requested.connect(self._on_delete_grid)
            self._grids_strip.insertWidget(insert_at, thumb)
            insert_at += 1
            self._thumbs.append(thumb)

        if active:
            self.hint_lbl.setText(tr('grid_active_lbl', name=active))
        else:
            self.hint_lbl.setText("")

    def _on_pick_grid(self, name: str):
        library.set_active_grid(name)
        for t in self._thumbs:
            t.set_active(t.name == name)
        self.hint_lbl.setText(tr('grid_active_lbl', name=name))

    def _on_add_grid(self):
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
        if QMessageBox.question(
                self, tr('grid_del_tooltip'),
                tr('grid_delete_confirm', name=name)
        ) != QMessageBox.StandardButton.Yes:
            return
        library.delete_grid(name)
        self._refresh_grids()

    # ── Наложение / детекция ────────────────────────────────────────────
    def _clear_overlays(self):
        if self.view is None:
            return
        scene = self.view._scene
        for it in self._grid_items:
            it._on_delete = None
        for it in self._grid_items:
            try:
                scene.removeItem(it)
            except Exception:
                pass
        self._grid_items = []

    def _on_apply(self):
        """Авто-детект лиц (YuNet) → на каждое лицо активная сетка с запасом
        FACE_GRID_SCALE. Повторный клик очищает прошлые наложения и кладёт
        заново. Источник детекции — реф (image_path)."""
        if self.view is None:
            return
        grid_path = library.get_active_grid()
        if not grid_path:
            self.hint_lbl.setText(tr('grid_no_active'))
            return

        self.hint_lbl.setText(tr('grid_searching'))
        QApplication.processEvents()
        try:
            from widgets.face_grid.detector import detect_faces
            boxes = detect_faces(self.image_path)
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
            item.setPos(cx, cy)
            item.setZValue(30)
            scene.addItem(item)
            self._grid_items.append(item)

        self.hint_lbl.setText(tr('grid_applied', n=len(boxes)))

    def _add_grid_at(self, scene_pos):
        """Дабл-клик по пустому месту → активная сетка центром в точку клика
        (крупный план / лицо не найдено). Стартовый масштаб ≈ 1/6 ширины кадра."""
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
        item.setPos(scene_pos)
        item.setZValue(30)
        self.view._scene.addItem(item)
        self._grid_items.append(item)
        item.setSelected(True)
        self.hint_lbl.setText(tr('grid_applied', n=len(self._grid_items)))

    def _remove_grid_item(self, item):
        try:
            if self.view is not None and item.scene() is not None:
                self.view._scene.removeItem(item)
        except Exception:
            traceback.print_exc()
        if item in self._grid_items:
            self._grid_items.remove(item)
        item._on_delete = None

    def keyPressEvent(self, e):
        if (e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self.view is not None):
            for it in list(self.view._scene.selectedItems()):
                if isinstance(it, GridItem):
                    self._remove_grid_item(it)
            e.accept()
            return
        super().keyPressEvent(e)

    # ── Персист расстановки (<stem>_grid.json) ──────────────────────────
    def _restore_grids(self):
        """Восстановить сохранённую расстановку из <stem>_grid.json (повторное
        открытие). PNG резолвится по имени через library. Нет PNG / битый
        pixmap → пропуск + счётчик. Битый/пустой json → тихо ничего."""
        if self.view is None:
            return
        if not self._grid_json.exists():
            return
        try:
            data = json.loads(self._grid_json.read_text(encoding="utf-8"))
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
            if src is None:
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
        if skipped:
            self.hint_lbl.setText(
                tr('grid_restored_skipped', n=restored, m=skipped))
        else:
            self.hint_lbl.setText(tr('grid_restored', n=restored))

    def _write_grid_json(self):
        """Записать позиции сеток рядом с save_path (<stem>_grid.json):
        имя PNG + центр (пиксели рефа) + scale + размер рефа. Не-фатально."""
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
            "schema": ACTOR_GRID_JSON_SCHEMA,
            "board_w": int(round(rect.width())),
            "board_h": int(round(rect.height())),
            "grids": grids,
        }
        try:
            self._grid_json.parent.mkdir(parents=True, exist_ok=True)
            self._grid_json.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            traceback.print_exc()

    def _delete_grid_outputs(self):
        """Ноль сеток → убрать stale grid-jpg И json, чтобы downstream
        (рефы блока / zip) не подхватили устаревшую версию-с-сеткой."""
        for p in (self.save_path, self._grid_json):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                traceback.print_exc()

    # ── Сохранение ──────────────────────────────────────────────────────
    def _on_save(self):
        """Впечатать сетки в реф и сохранить ОТДЕЛЬНЫМ файлом save_path
        (characters_grid/<slug>/<stem>_grid.jpg). Оригинал НЕ трогаем.
        Ноль сеток → удалить stale grid-файлы, закрыть. Ошибка → не закрываем."""
        if self.view is None:
            self.reject()
            return
        if not self._grid_items:
            self._delete_grid_outputs()
            self.hint_lbl.setText(tr('actor_grid_save_empty'))
            self.accept()
            return

        self.hint_lbl.setText(tr('grid_saving'))
        QApplication.processEvents()
        try:
            from PIL import Image as PILImage
            base = PILImage.open(str(self.image_path)).convert("RGBA")
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
                layer.paste(grid, (left, top), grid)
                painted += 1
            out = PILImage.alpha_composite(base, layer).convert("RGB")
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            out.save(str(self.save_path), format="JPEG", quality=95)
        except Exception as e:
            traceback.print_exc()
            self.hint_lbl.setText(tr('actor_grid_save_error', err=str(e)))
            QMessageBox.warning(self, tr('grid_btn_save'), str(e))
            return
        # Композит записан → персист позиций (не-фатально).
        self._write_grid_json()
        self.hint_lbl.setText(tr('actor_grid_saved', n=painted))
        self.accept()

    # ── Help-панель по жестам (поповер) ─────────────────────────────────
    def _build_help_panel(self):
        panel = QFrame(self)
        panel.setObjectName("GridHelpPanel")
        panel.setStyleSheet(
            "QFrame#GridHelpPanel { background:#15101f; border:1px solid #322545;"
            " border-radius:10px; }"
            "QLabel#help-title { color:rgba(255,255,255,0.92); font-size:13px;"
            " font-weight:600; }"
            "QLabel#help-name { color:rgba(255,255,255,0.92); font-size:12px;"
            " font-weight:600; }"
            "QLabel#help-desc { color:rgba(255,255,255,0.50); font-size:12px; }")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(16, 14, 16, 14)
        pl.setSpacing(10)

        title = QLabel(tr('grid_help_title'))
        title.setObjectName("help-title")
        pl.addWidget(title)

        try:
            from storyboard_app import get_icon
        except Exception:
            get_icon = None

        rows = [
            ('mouse', 'grid_help_wheel'),
            ('hand', 'grid_help_pan'),
            ('move', 'grid_help_move'),
            ('maximize-2', 'grid_help_resize'),
            ('x', 'grid_help_delete'),
            ('mouse-pointer-click', 'grid_help_place'),
        ]
        for icon_name, key in rows:
            row = QHBoxLayout()
            row.setSpacing(10)
            ic = QLabel()
            ic.setFixedSize(20, 20)
            ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if get_icon is not None:
                pm = get_icon(icon_name).pixmap(QSize(18, 18))
                if not pm.isNull():
                    ic.setPixmap(pm)
            row.addWidget(ic)
            name, _, desc = tr(key).partition('|')
            name_lbl = QLabel(name)
            name_lbl.setObjectName("help-name")
            row.addWidget(name_lbl)
            if desc:
                desc_lbl = QLabel("— " + desc)
                desc_lbl.setObjectName("help-desc")
                row.addWidget(desc_lbl)
            row.addStretch()
            pl.addLayout(row)

        panel.adjustSize()
        panel.hide()
        return panel

    def _toggle_help(self):
        if self._help_panel.isVisible():
            self._hide_help()
        else:
            self._show_help()

    def _show_help(self):
        panel = self._help_panel
        panel.adjustSize()
        btn_tl = self.btn_help.mapTo(self, QPoint(0, 0))
        x = btn_tl.x() + self.btn_help.width() - panel.width()
        y = btn_tl.y() - panel.height() - 8
        x = max(8, min(x, self.width() - panel.width() - 8))
        y = max(8, y)
        panel.move(x, y)
        panel.show()
        panel.raise_()
        app = QApplication.instance()
        app.removeEventFilter(self)
        app.installEventFilter(self)

    def _hide_help(self):
        panel = getattr(self, "_help_panel", None)
        if panel is not None and panel.isVisible():
            panel.hide()
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.MouseButtonPress
                and getattr(self, "_help_panel", None) is not None
                and self._help_panel.isVisible()):
            gp = event.globalPosition().toPoint()
            in_panel = self._help_panel.rect().contains(
                self._help_panel.mapFromGlobal(gp))
            in_btn = self.btn_help.rect().contains(
                self.btn_help.mapFromGlobal(gp))
            if not in_panel and not in_btn:
                self._hide_help()
        return super().eventFilter(obj, event)

    def hideEvent(self, e):
        self._hide_help()
        super().hideEvent(e)
