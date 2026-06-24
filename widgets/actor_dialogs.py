# -*- coding: utf-8 -*-
"""
widgets/actor_dialogs.py — диалоги вокруг работы с актёрами.

Шаг 4A (2026-05-04): простые диалоги без callback'ов в ActorsView:
    - AddActorDialog          — popup «Создать актёра» / «Переименовать»
    - ChooseActorDialog       — выбор папки куда положить фото
    - _PhotoThumb             — кликабельный thumbnail в галерее
    - _BigPhotoLabel          — большая фотка
    - ActorPhotosDialog       — попап галереи фото актёра

Шаг 4B (2026-05-04): диалоги с duck-typed callback'ами в ActorsView
(через `owner_view` параметр, не через импорт):
    - _LayoutVariantCard      — карточка варианта layout-а
    - CreateActorRefDialog    — popup создания character-рефа
    - RefResultDialog         — стек pending-вариантов с переключением

Зависимости от storyboard_app (через `_AppProxy`): list_actors,
actor_display_name, get_icon, block_wheel_event, ACTOR_REF_PROMPT_DETAILED/
SIMPLE, build_actor_ref_filename, list_shows, get_current_show,
list_show_characters, transliterate_for_filename. См. threads/update.py
для объяснения проблемы dual-instance в PyInstaller и почему _AppProxy
ищет в __main__.
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QSize, QUrl, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QDialog, QLabel, QLineEdit, QPlainTextEdit, QComboBox, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QFrame,
    QScrollArea, QStackedWidget, QMessageBox, QDialogButtonBox,
    QSlider, QGraphicsOpacityEffect,
)

from i18n import tr


class _AppProxy:
    """Прокси к module storyboard_app — приоритет __main__.
    См. подробное объяснение в threads/update.py."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


# ─── Создание / переименование актёра ────────────────────────────

class AddActorDialog(QDialog):
    """Диалог создания нового актёра. Юзер вводит имя (например «Оля»).
    Если передан current_name — режим переименования (другие лейблы)."""

    def __init__(self, parent=None, current_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr('add_actor_title') if not current_name
                             else tr('rename_actor_title'))
        self.setFixedSize(440, 200)
        self.setStyleSheet("QDialog { background: #1a1424; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(12)

        lbl = QLabel(tr('add_actor_label') if not current_name
                     else tr('rename_actor_label'))
        lbl.setStyleSheet("color:#cfcfcf; font-size:13px;")
        v.addWidget(lbl)

        self.input = QLineEdit(current_name)
        self.input.setPlaceholderText(tr('add_actor_placeholder'))
        self.input.setStyleSheet(
            "QLineEdit { background:#1a1424; border:1px solid #3a2c52;"
            " border-radius:6px; padding:10px; color:#fff; font-size:14px; }")
        v.addWidget(self.input)

        v.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(tr('add_actor_cancel'))
        cancel_btn.setFixedHeight(34)
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton(
            tr('rename_actor_save') if current_name else tr('add_actor_create'))
        ok_btn.setObjectName("save")
        ok_btn.setFixedHeight(34)
        ok_btn.setMinimumWidth(130)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        v.addLayout(btn_row)

    def value(self) -> str:
        return self.input.text().strip()


# ─── Выбор актёра при drop'е фоток ───────────────────────────────

class ChooseActorDialog(QDialog):
    """Диалог выбора актёра при перетаскивании фоток.
    Список существующих актёров + опция «Создать нового».
    Возвращает выбранный slug, либо None."""

    NEW_SENTINEL = "__NEW__"

    def __init__(self, project_root: Path, file_count: int, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.setWindowTitle(tr('choose_actor_title'))
        self.setFixedSize(460, 240)
        self.setStyleSheet("QDialog { background: #1a1424; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(12)

        lbl = QLabel(tr('choose_actor_label'))
        lbl.setStyleSheet("color:#cfcfcf; font-size:13px;")
        v.addWidget(lbl)

        self.combo = QComboBox()
        self.combo.setFixedHeight(36)
        self.combo.addItem(tr('choose_actor_placeholder'), "")
        for slug in _sa.list_actors(project_root):
            self.combo.addItem(_sa.actor_display_name(project_root, slug), slug)
        self.combo.addItem(tr('actors_add_btn'), self.NEW_SENTINEL)
        self.combo.setStyleSheet(
            "QComboBox { background:#1a1424; border:1px solid #3a2c52;"
            " border-radius:6px; padding:6px 10px; color:#fff; font-size:14px; }")
        _sa.block_wheel_event(self.combo)
        v.addWidget(self.combo)

        count_lbl = QLabel(tr('choose_actor_count', n=file_count))
        count_lbl.setStyleSheet("color:#888; font-size:12px;")
        v.addWidget(count_lbl)

        v.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(tr('add_actor_cancel'))
        cancel_btn.setFixedHeight(38)
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton(tr('choose_actor_ok'))
        ok_btn.setObjectName("save")
        ok_btn.setIcon(_sa.get_icon('download'))
        ok_btn.setIconSize(QSize(14, 14))
        ok_btn.setFixedHeight(38)
        ok_btn.setMinimumWidth(140)
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        v.addLayout(btn_row)

    def selected_slug(self) -> Optional[str]:
        """Возвращает slug выбранного актёра, NEW_SENTINEL если "новый",
        или None если ничего не выбрано."""
        v = self.combo.currentData()
        if not v:
            return None
        return v


# ─── Подвиджеты для ActorPhotosDialog ────────────────────────────

class _PhotoThumb(QLabel):
    """Кликабельный thumbnail фотки в галерее. По клику emit clicked(path).
    Hover-эффект делается через CSS — лёгкая обводка чтобы было видно
    что элемент интерактивный."""

    clicked = pyqtSignal(str)  # absolute path

    def __init__(self, photo_path: Path, thumb_size: int, parent=None):
        super().__init__(parent)
        self._path = str(photo_path)
        self.setFixedSize(thumb_size, thumb_size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QLabel { background:#1a1424; border:1px solid #2a1f3d;"
            " border-radius:8px; }"
            "QLabel:hover { border:1px solid #6e4cc4; }")
        try:
            pix = QPixmap(self._path)
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(thumb_size - 2, thumb_size - 2),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.setPixmap(pix)
        except Exception:
            pass

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._path)
        super().mousePressEvent(ev)


class _BigPhotoLabel(QLabel):
    """QLabel в режиме «увеличенной фотки». Клик → emit clicked() — диалог
    возвращает юзера к гриду."""

    clicked = pyqtSignal()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


# ─── Галерея фото актёра ─────────────────────────────────────────

class ActorPhotosDialog(QDialog):
    """Попап с галереей всех фото одного актёра.

    Два режима через QStackedWidget:
    - page 0 (`grid`): сетка thumbnail-ов 220×220 в скролле.
    - page 1 (`single`): одна большая фотка (~700×700, KeepAspectRatio),
      клик возвращает на page 0.

    Закрыть: Esc, крестик окна, или кнопка «Закрыть» внизу.
    """

    THUMB_SIZE = 220
    BIG_MAX = 700

    # Сигнал — клик «✓ Использовать в эпизоде» на конкретной превью.
    # Эмитится с полным path сгенерированного рефа. Caller (ActorsView)
    # извлекает character_slug из родительской папки и пишет decision.
    picked_for_ep = pyqtSignal(object)   # Path
    # 2026-05-17: сигнал «✎ Изменить» — отправляется когда юзер ввёл
    # текст инструкции в попапе. Caller (ActorsView) сам запускает
    # EditActorRefThread с этими (path, instruction).
    edit_ref_requested = pyqtSignal(object, str)   # (Path, instruction)
    # 2026-05-17 (Этап 2): сигнал «🎨 Наложить текстуру» — отправляется
    # при клике под thumb-ом. Caller (ActorsView) сам открывает
    # ApplyTextureDialog с выбором текстуры + opacity, и запускает
    # ApplyTextureThread. Здесь только UI-trigger, никаких диалогов.
    apply_texture_requested = pyqtSignal(object)   # Path
    # 2026-06-03: сигнал «🔲 Сетка на лицо» — клик под thumb-ом. Caller
    # (ActorsView) открывает ActorGridDialog (детект лиц + наложение сетки),
    # результат — отдельный файл characters_grid/<slug>/<stem>_grid.jpg.
    apply_grid_requested = pyqtSignal(object)      # Path

    def __init__(self, display_name: str, photos: List[Path], parent=None,
                 folder_path: Optional[Path] = None,
                 enable_delete: bool = False,
                 enable_pick_for_ep: bool = False,
                 enable_edit: bool = False,
                 enable_texture: bool = False,
                 texture_folder_path: Optional[Path] = None,
                 grid_folder_path: Optional[Path] = None):
        super().__init__(parent)
        self.photos = list(photos)
        # Включает кнопку «🗑 Удалить» под каждым thumb в сетке. По умолчанию
        # выкл — для фото актёра (исходники) удаление опасно. Включается
        # caller'ом для попапа character-рефов («Все референсы»).
        self.enable_delete = bool(enable_delete)
        # 2026-05-05: включает кнопку «✓ Использовать в эпизоде» под
        # каждым thumb'ом. Активна когда ActorsView в режиме wildcard
        # pending — юзер пришёл из «+ Добавить персонажа» и хочет взять
        # готовый реф вместо генерации нового.
        self.enable_pick_for_ep = bool(enable_pick_for_ep)
        # 2026-05-17: включает кнопку «✎ Изменить» под каждым thumb'ом.
        # Активна в попапе «Все референсы» — юзер может коротко описать
        # правку (например «добавь штукатурку на голову»), Studio запустит
        # EditActorRefThread (FastGen с текущим рефом как identity-якорь
        # + инструкцией). Лицо/идентичность сохранятся, изменится только
        # запрошенный элемент.
        self.enable_edit = bool(enable_edit)
        # 2026-05-17 (Этап 2): включает кнопку «🎨 Текстура» под thumb'ом.
        # Локальный PIL-композит (без API). Результат — отдельный файл
        # в shows/<show>/refs/characters_texture/<character>/.
        self.enable_texture = bool(enable_texture)
        # 2026-05-17 (Этап 3): путь к папке результатов наложения текстур
        # (shows/<show>/refs/characters_texture/<character>/). Если задан —
        # внизу диалога появляется вторая кнопка «🎨 Папка с текстурами»
        # рядом с «📂 Показать в папке». Папка создаётся лениво при клике
        # (mkdir parents=True, exist_ok=True) — кнопка работает даже если
        # юзер ещё не накладывал текстуру.
        self._texture_folder_path: Optional[Path] = (
            Path(texture_folder_path) if texture_folder_path else None)
        # 2026-06-03: папка результатов наложения сеток
        # (shows/<show>/refs/characters_grid/<character>/). Если задан —
        # внизу диалога кнопка «🔲 Папка с сетками». Создаётся лениво при клике.
        self._grid_folder_path: Optional[Path] = (
            Path(grid_folder_path) if grid_folder_path else None)
        self._display_name = display_name
        self.setWindowTitle(tr('actor_photos_title',
                               name=display_name, n=len(self.photos)))
        # Не блокировать всё приложение — модальный только относительно ActorsView
        self.setModal(True)
        self.resize(820, 720)
        self.setStyleSheet(
            "QDialog { background:#15101e; }"
            "QLabel#photos-hint { color:#aaa; font-size:12px; }"
            "QLabel#photos-empty { color:#888; font-size:14px;"
            " font-style:italic; padding:40px; }"
            "QPushButton#photos-close { background:#3a2c52; color:#fff;"
            " border:none; border-radius:6px; padding:8px 18px; font-size:13px; }"
            "QPushButton#photos-close:hover { background:#4d3a6b; }"
            "QPushButton#photos-delete { background:transparent; color:#e08080;"
            " border:1px solid #5a2a2a; border-radius:6px; padding:6px 10px;"
            " font-size:12px; font-weight:500; }"
            "QPushButton#photos-delete:hover { background:#3a1a1a; color:#ff9a9a;"
            " border-color:#7a3a3a; }"
            "QPushButton#photos-pick { background:#3a5a3a; color:#d8ffd8;"
            " border:1px solid #4d8a4d; border-radius:6px; padding:6px 10px;"
            " font-size:12px; font-weight:600; }"
            "QPushButton#photos-pick:hover { background:#4d7a4d; color:#fff;"
            " border-color:#6dba6d; }"
            # 2026-05-17: кнопка «✎ Изменить» — нейтральный outline стиль
            # (не destructive как Удалить, не accept как Использовать).
            "QPushButton#photos-edit { background:transparent;"
            " color:#d8c8ff; border:1px solid #6e4cc4;"
            " border-radius:6px; padding:6px 10px; font-size:12px;"
            " font-weight:500; }"
            "QPushButton#photos-edit:hover { background:#2a1f3d;"
            " color:#fff; border-color:#8e6cdc; }"
            # 2026-05-17 (Этап 2): кнопка «🎨 Текстура» — промежуточный
            # вариант между accept и destructive (золотой/янтарный
            # outline, ассоциация с краской/материалом).
            "QPushButton#photos-texture { background:transparent;"
            " color:#d4a256; border:1px solid #b88a3c;"
            " border-radius:6px; padding:6px 10px; font-size:12px;"
            " font-weight:500; }"
            "QPushButton#photos-texture:hover { background:#2a1f12;"
            " color:#ffd24d; border-color:#d4a256; }"
            # 2026-06-03: кнопка «🔲 Сетка на лицо» — активное действие
            # (голубой outline, отличается от серой заглушки-текстуры).
            "QPushButton#photos-grid { background:transparent;"
            " color:#9fd0ff; border:1px solid #4a7fb0;"
            " border-radius:6px; padding:6px 10px; font-size:12px;"
            " font-weight:500; }"
            "QPushButton#photos-grid:hover { background:#16222e;"
            " color:#cfe8ff; border-color:#6aa0d8; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        self.hint_lbl = QLabel(tr('actor_photos_hint'))
        self.hint_lbl.setObjectName("photos-hint")
        outer.addWidget(self.hint_lbl)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, stretch=1)

        # ── page 0: грид thumbnails ─────────────────────────────────────
        # Контейнер grid-страницы — содержимое перестраивается через
        # _rebuild_grid() при удалении рефов. Сама страница в stack
        # добавляется один раз, меняется только её layout-content.
        self._grid_page = QWidget()
        self._grid_page.setStyleSheet("background: transparent;")
        self._grid_page_lay = QVBoxLayout(self._grid_page)
        self._grid_page_lay.setContentsMargins(0, 0, 0, 0)
        self._build_grid_content()
        self.stack.addWidget(self._grid_page)

        # ── page 1: большая одна фотка ──────────────────────────────────
        big_page = QWidget()
        big_page.setStyleSheet("background: transparent;")
        bp_lay = QVBoxLayout(big_page)
        bp_lay.setContentsMargins(0, 0, 0, 0)
        self.big_lbl = _BigPhotoLabel()
        self.big_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.big_lbl.setStyleSheet(
            "QLabel { background:#1a1424; border-radius:10px; }")
        self.big_lbl.clicked.connect(self._show_grid)
        bp_lay.addWidget(self.big_lbl, stretch=1)
        self.stack.addWidget(big_page)

        # ── низ: «Показать в папке» (если есть фото) + «Закрыть» ─────────
        bottom = QHBoxLayout()
        # Папка по умолчанию определяется автоматически из первого фото
        # (все фото актёра лежат в одной папке). Если caller передал явный
        # folder_path — используем его (например, для show-wide рефов
        # `shows/<show>/refs/characters/` где фото из разных подпапок).
        self._folder_path: Optional[Path] = folder_path
        if self._folder_path is None and self.photos:
            try:
                self._folder_path = self.photos[0].parent
            except Exception:
                self._folder_path = None

        if self._folder_path is not None:
            self.open_folder_btn = QPushButton(tr('actor_photos_open_folder'))
            self.open_folder_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#d8c8ff;"
                " border:1px solid #6e4cc4; border-radius:6px;"
                " padding:8px 14px; font-size:12px; font-weight:600; }"
                "QPushButton:hover { background:#2a1f3d; color:#fff; }")
            self.open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.open_folder_btn.clicked.connect(self._on_open_folder)
            bottom.addWidget(self.open_folder_btn)
        else:
            self.open_folder_btn = None

        # 2026-05-17 (Этап 3): «🎨 Папка с текстурами» — рядом с «Показать
        # в папке». Только если caller передал texture_folder_path.
        # Стиль зеркалит «🎨 Текстура» кнопки под thumb (золотой outline)
        # для визуальной связи. Папка создаётся лениво в _on_show_texture_folder.
        if self._texture_folder_path is not None:
            self.open_texture_folder_btn = QPushButton(
                tr('actor_photos_show_texture_folder_btn'))
            self.open_texture_folder_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#d4a256;"
                " border:1px solid #b88a3c; border-radius:6px;"
                " padding:8px 14px; font-size:12px; font-weight:600; }"
                "QPushButton:hover { background:#2a1f12; color:#ffd24d;"
                " border-color:#d4a256; }")
            self.open_texture_folder_btn.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self.open_texture_folder_btn.clicked.connect(
                self._on_show_texture_folder)
            bottom.addWidget(self.open_texture_folder_btn)
        else:
            self.open_texture_folder_btn = None

        # 2026-06-03: «🔲 Папка с сетками» — рядом с «🎨 Папка с текстурами».
        # Только если caller передал grid_folder_path. Стиль зеркалит кнопку
        # «🔲 Сетка» под thumb (голубой outline). Папка создаётся лениво.
        if self._grid_folder_path is not None:
            self.open_grid_folder_btn = QPushButton(
                tr('actor_photos_show_grid_folder_btn'))
            self.open_grid_folder_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#9fd0ff;"
                " border:1px solid #4a7fb0; border-radius:6px;"
                " padding:8px 14px; font-size:12px; font-weight:600; }"
                "QPushButton:hover { background:#16222e; color:#cfe8ff;"
                " border-color:#6aa0d8; }")
            self.open_grid_folder_btn.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self.open_grid_folder_btn.clicked.connect(
                self._on_show_grid_folder)
            bottom.addWidget(self.open_grid_folder_btn)
        else:
            self.open_grid_folder_btn = None

        bottom.addStretch()
        self.close_btn = QPushButton(tr('actor_photos_close'))
        self.close_btn.setObjectName("photos-close")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.close_btn)
        outer.addLayout(bottom)

    def _build_grid_content(self):
        """Строит/перестраивает содержимое grid-страницы из self.photos.
        Очищает текущий layout и заново раскладывает thumbnails (3 колонки).
        Если включён enable_delete — под каждым thumb появляется кнопка
        «🗑 Удалить»."""
        # Очищаем текущее содержимое (если перестраиваем после delete)
        while self._grid_page_lay.count():
            item = self._grid_page_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self.photos:
            empty = QLabel(tr('actor_photos_empty'))
            empty.setObjectName("photos-empty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid_page_lay.addWidget(empty)
            return

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        grid = QGridLayout(inner)
        grid.setSpacing(12)
        grid.setContentsMargins(0, 0, 0, 0)
        cols = 3  # фиксированно — попап ~820px, 3×220+spacing влезает
        for i, p in enumerate(self.photos):
            # Контейнер: thumb сверху + кнопка «🗑 Удалить» снизу (если включена)
            cell = QWidget()
            cell_lay = QVBoxLayout(cell)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.setSpacing(6)
            thumb = _PhotoThumb(p, self.THUMB_SIZE)
            thumb.clicked.connect(self._show_big)
            cell_lay.addWidget(thumb)
            if self.enable_pick_for_ep:
                pick_btn = QPushButton(tr('actor_photos_pick_for_ep'))
                pick_btn.setObjectName("photos-pick")
                pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                pick_btn.clicked.connect(
                    lambda _checked=False, path=p: self._on_pick_thumb(path))
                cell_lay.addWidget(pick_btn)
            if self.enable_edit:
                edit_btn = QPushButton(tr('actor_ref_edit_btn'))
                edit_btn.setObjectName("photos-edit")
                edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                edit_btn.clicked.connect(
                    lambda _checked=False, path=p: self._on_edit_thumb(path))
                cell_lay.addWidget(edit_btn)
            if self.enable_texture:
                # 2026-06-04: текстура снова активна (была временно
                # заблокирована в Коммите 2 фичи «сетка на лицо»).
                texture_btn = QPushButton(tr('actor_photos_texture_btn'))
                texture_btn.setObjectName("photos-texture")
                texture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                texture_btn.clicked.connect(
                    lambda _checked=False, path=p: self._on_texture_thumb(path))
                cell_lay.addWidget(texture_btn)
                # 2026-06-03: «🔲 Сетка на лицо» — открывает ActorGridDialog
                # (через сигнал apply_grid_requested → ActorsView). Тот же гейт.
                grid_btn = QPushButton(tr('actor_grid_btn'))
                grid_btn.setObjectName("photos-grid")
                grid_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                grid_btn.clicked.connect(
                    lambda _checked=False, path=p: self._on_grid_thumb(path))
                cell_lay.addWidget(grid_btn)
            if self.enable_delete:
                del_btn = QPushButton(tr('actor_photos_delete_btn'))
                del_btn.setObjectName("photos-delete")
                del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                # Захватываем path по значению (через default-arg)
                del_btn.clicked.connect(
                    lambda _checked=False, path=p: self._on_delete_thumb(path))
                cell_lay.addWidget(del_btn)
            r, c = divmod(i, cols)
            grid.addWidget(cell, r, c)
        for c in range(cols):
            grid.setColumnStretch(c, 0)
        grid.setColumnStretch(cols, 1)
        # Фантомная строка-распорка под контентом забирает лишнюю
        # вертикальную высоту (scroll.setWidgetResizable(True) раздувает
        # inner). Без неё слабина делится между строками контента →
        # cell_lay (без trailing stretch) растягивает кнопки по высоте.
        # Зеркало трюка с фантомной колонкой выше.
        n_rows = (len(self.photos) - 1) // cols + 1
        grid.setRowStretch(n_rows, 1)
        scroll.setWidget(inner)
        self._grid_page_lay.addWidget(scroll)

    def _on_pick_thumb(self, path: Path):
        """Клик «✓ Использовать в эпизоде» под thumb-ом. Эмитим сигнал
        с полным path и закрываем диалог — caller сам разберёт slug
        из родителя и запишет decision."""
        try:
            self.picked_for_ep.emit(path)
        except Exception:
            traceback.print_exc()
        self.accept()

    def _on_edit_thumb(self, path):
        """2026-05-17: клик «✎ Изменить» под thumb-ом.

        Открывает попап с QPlainTextEdit инструкцией. На Ok с непустой
        инструкцией — emit `edit_ref_requested(Path, instruction)`. Сам
        thread запускает caller (ActorsView._on_edit_actor_ref), здесь
        только UI. Pattern скопирован с _ask_ref_edit_instruction из
        storyboard_app.py, но с actor-специфичными i18n ключами.
        """
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle(tr('actor_ref_edit_dialog_title'))
            dlg.setFixedSize(460, 260)
            v = QVBoxLayout(dlg)
            v.setSpacing(12)
            v.setContentsMargins(20, 18, 20, 16)
            title = QLabel(tr('actor_ref_edit_dialog_q'))
            title.setStyleSheet(
                "color:#ddd; font-size:14px; font-weight:500;")
            v.addWidget(title)
            hint = QLabel(tr('actor_ref_edit_dialog_hint'))
            hint.setStyleSheet("color:#888; font-size:11px;")
            hint.setWordWrap(True)
            v.addWidget(hint)
            text = QPlainTextEdit()
            text.setPlaceholderText(
                tr('actor_ref_edit_dialog_placeholder'))
            text.setStyleSheet(
                "QPlainTextEdit { background:#15101e;"
                " border:1px solid #2c2240; border-radius:6px;"
                " color:#ddd; padding:8px; font-size:13px; }")
            v.addWidget(text, stretch=1)
            btns = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel)
            btns.button(QDialogButtonBox.StandardButton.Ok).setText(
                tr('edit_dialog_send'))
            btns.button(QDialogButtonBox.StandardButton.Cancel).setText(
                tr('edit_dialog_cancel'))
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            v.addWidget(btns)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            instr = text.toPlainText().strip()
            if not instr:
                return
            try:
                self.edit_ref_requested.emit(Path(path), instr)
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()

    def _on_texture_thumb(self, path):
        """2026-05-17 (Этап 2): клик «🎨 Текстура» под thumb-ом.

        Просто эмит сигнала с Path рефа. Caller (ActorsView) откроет
        ApplyTextureDialog (picker + слайдер + preview), получит
        выбор юзера и запустит ApplyTextureThread.
        """
        try:
            self.apply_texture_requested.emit(Path(path))
        except Exception:
            traceback.print_exc()

    def _on_grid_thumb(self, path):
        """2026-06-03: клик «🔲 Сетка на лицо» под thumb-ом. Эмит сигнала с
        Path рефа. Caller (ActorsView) откроет ActorGridDialog (детект лиц +
        наложение сетки), результат — отдельный файл characters_grid/."""
        try:
            self.apply_grid_requested.emit(Path(path))
        except Exception:
            traceback.print_exc()

    def _on_delete_thumb(self, path: Path):
        """Клик «🗑 Удалить» под thumb-ом. Спрашивает подтверждение,
        удаляет файл с диска, перестраивает grid. Это необратимое
        действие — confirm обязателен."""
        try:
            ans = QMessageBox.question(
                self,
                tr('actor_photos_delete_title'),
                tr('actor_photos_delete_confirm', filename=Path(path).name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                traceback.print_exc()
                return
            # Удаляем из локального списка и перестраиваем сетку
            self.photos = [p for p in self.photos if Path(p) != Path(path)]
            self.setWindowTitle(tr('actor_photos_title',
                                   name=self._display_name,
                                   n=len(self.photos)))
            self._build_grid_content()
        except Exception:
            traceback.print_exc()

    def _on_open_folder(self):
        """Открывает папку с фото в Finder/Explorer через Qt-кроссплатформ.
        QDesktopServices работает на macOS/Linux/Windows одинаково."""
        try:
            if self._folder_path is None or not self._folder_path.exists():
                return
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._folder_path.resolve())))
        except Exception:
            traceback.print_exc()

    def _on_show_texture_folder(self):
        """2026-05-17 (Этап 3): «🎨 Папка с текстурами» — открыть в
        Finder/Explorer папку результатов наложения текстур. Если папки
        ещё нет (юзер ни разу не накладывал) — создаём через
        mkdir(parents=True, exist_ok=True). Симметрия с Этапом 1 где
        actors/_textures/ тоже создаётся лениво."""
        try:
            if self._texture_folder_path is None:
                return
            self._texture_folder_path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(
                    str(self._texture_folder_path.resolve())))
        except Exception:
            traceback.print_exc()

    def _on_show_grid_folder(self):
        """2026-06-03: «🔲 Папка с сетками» — открыть в Finder/Explorer папку
        результатов наложения сеток (characters_grid/<character>/). Папка
        создаётся лениво (mkdir parents). Зеркало _on_show_texture_folder —
        QDesktopServices (cross-platform, без subprocess)."""
        try:
            if self._grid_folder_path is None:
                return
            self._grid_folder_path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(
                    str(self._grid_folder_path.resolve())))
        except Exception:
            traceback.print_exc()

    def _show_big(self, path: str):
        """Показать выбранную фотку крупно (page 1)."""
        try:
            pix = QPixmap(path)
            if pix.isNull():
                return
            # Размер максимум BIG_MAX×BIG_MAX, но не больше доступной площади
            avail_w = max(200, self.width() - 60)
            avail_h = max(200, self.height() - 140)
            target = min(self.BIG_MAX, avail_w, avail_h)
            pix = pix.scaled(
                QSize(target, target),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.big_lbl.setPixmap(pix)
            self.stack.setCurrentIndex(1)
        except Exception:
            traceback.print_exc()

    def _show_grid(self):
        """Вернуться к гриду thumbnail-ов (page 0)."""
        try:
            self.stack.setCurrentIndex(0)
            # Освобождаем память от большой pixmap
            self.big_lbl.clear()
        except Exception:
            pass

    def keyPressEvent(self, ev):
        """Esc на page 1 → возврат к гриду. Esc на page 0 → стандартное
        закрытие диалога (через QDialog.reject)."""
        if ev.key() == Qt.Key.Key_Escape and self.stack.currentIndex() == 1:
            self._show_grid()
            ev.accept()
            return
        super().keyPressEvent(ev)


# ═════════════════════════════════════════════════════════════════
# Шаг 4B: создание character-рефа + просмотр стека вариантов
# ═════════════════════════════════════════════════════════════════
#
# Эти диалоги вызывают `owner_view.X()` — duck typing, не реальная
# зависимость через импорт. Когда ActorsView переедет в `views/actors.py`
# на шаге 4C, он будет передавать `self` как `owner_view`, и интерфейс
# (методы start_ref_generation / update_pending_variants /
# confirm_pending_kept) останется тот же.


# ─── Карточка варианта layout-а ──────────────────────────────────

class _LayoutVariantCard(QFrame):
    """Кликабельная карточка варианта layout-а в попапе создания рефа.
    При клике emit chosen(variant_id). Визуально показывает выбранный
    вариант через рамку (`-selected` стиль)."""

    chosen = pyqtSignal(str)  # 'detailed' | 'simple'

    def __init__(self, variant_id: str, title: str, hint: str,
                 panels_count: int, parent=None):
        super().__init__(parent)
        self.variant_id = variant_id
        self._selected = False
        self.setObjectName("variant-card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(150)
        self._apply_style()

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            "color:#fff; font-size:14px; font-weight:600; background:transparent;")
        v.addWidget(title_lbl)

        panels_lbl = QLabel(f"{panels_count} панелей")
        panels_lbl.setStyleSheet(
            "color:#ffd24d; font-size:12px; font-weight:600; background:transparent;")
        v.addWidget(panels_lbl)

        hint_lbl = QLabel(hint)
        hint_lbl.setWordWrap(True)
        hint_lbl.setStyleSheet(
            "color:#bbb; font-size:11px; background:transparent;")
        v.addWidget(hint_lbl)
        v.addStretch()

    def setSelected(self, sel: bool):
        self._selected = bool(sel)
        self._apply_style()

    def _apply_style(self):
        if self._selected:
            self.setStyleSheet(
                "QFrame#variant-card { background:#2a1f3d;"
                " border:2px solid #8e6cd4; border-radius:10px; }")
        else:
            self.setStyleSheet(
                "QFrame#variant-card { background:#1a1424;"
                " border:1px solid #2a1f3d; border-radius:10px; }"
                "QFrame#variant-card:hover { border:1px solid #5a4a82; }")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.chosen.emit(self.variant_id)
        super().mousePressEvent(ev)


# ─── Создание character-рефа актёра ──────────────────────────────

class CreateActorRefDialog(QDialog):
    """Попап: ввод описания одежды/состояния + выбор варианта layout-а
    + кнопка «Сгенерировать». После клика создаёт GenerateActorRefThread
    (через owner_view.start_ref_generation) и закрывается с показом
    прогресса в статус-баре главного окна."""

    def __init__(self, project_root: Path, actor_slug: Optional[str],
                 display_name: str,
                 photos: List[Path], status_bar=None, owner_view=None,
                 parent=None,
                 prefill_show: Optional[str] = None,
                 prefill_character: Optional[str] = None,
                 prefill_description: Optional[str] = None,
                 custom_mode: bool = False):
        super().__init__(parent)
        self.project_root = project_root
        # 2026-06-24 (монстры 3б-redo): custom_mode=True → диалог для
        # нестандартного персонажа (монстра): тот же UI 1:1, но генерация
        # без фото (text2img + ACTOR_REF_PROMPT_CUSTOM) и БЕЗ записи
        # actors.json. actor_slug в этом режиме не используется (может быть None).
        self.custom_mode = custom_mode
        self.actor_slug = actor_slug
        self.display_name = display_name
        self.photos = list(photos)
        self.status_bar = status_bar
        # Долг 13: префиллы из чата эпизода (выбранный вариант одежды +
        # сериал + персонаж). Применяются после построения комбобоксов.
        self._prefill_show = prefill_show
        self._prefill_character = prefill_character
        self._prefill_description = prefill_description
        # owner_view = ActorsView, через него запускаем поток. ВАЖНО: thread
        # НЕ должен иметь диалог как родителя — иначе при close() диалога
        # Qt удалит работающий QThread → qFatal → краш всего приложения.
        self.owner_view = owner_view
        self._selected_variant = "simple"  # default (Базовый; «Расширенный» заглушён)

        self.setWindowTitle(
            tr('custom_char_title') if custom_mode
            else tr('create_ref_title', name=display_name))
        self.setModal(True)
        # 2026-05-19: адаптивный размер под parent/screen. Раньше
        # resize(640, 700) фиксированный — на 14" MBP (1512×982 logical
        # visible) контент обрезался снизу. Pattern из ShotViewerDialog:
        # min = 560×500, max = 90% parent/screen, resize клампируется по max.
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            pw, ph = parent_win.width(), parent_win.height()
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            pw, ph = geo.width(), geo.height()
        max_w, max_h = int(pw * 0.9), int(ph * 0.9)
        self.setMinimumSize(560, 500)
        self.setMaximumSize(max_w, max_h)
        self.resize(min(640, max_w), min(700, max_h))
        self.setStyleSheet(
            "QDialog { background:#15101e; }"
            "QLabel#cr-section { color:#cfcfcf; font-size:12px;"
            " font-weight:700; letter-spacing:1px; }"
            "QLabel#cr-hint { color:#aaa; font-size:12px; }"
            "QPlainTextEdit#cr-desc {"
            " background:#1a1424; border:1px solid #2a1f3d; border-radius:8px;"
            " color:#fff; padding:10px; font-size:13px; }"
            "QPlainTextEdit#cr-desc:focus { border:1px solid #6e4cc4; }"
            "QLineEdit#cr-filename {"
            " background:#15101e; border:1px solid #2a1f3d; border-radius:6px;"
            " color:#888; padding:7px 10px; font-size:13px;"
            " font-family: 'Menlo','Courier New',monospace; }"
            "QLineEdit#cr-newchar {"
            " background:#1a1424; border:1px solid #2a1f3d; border-radius:6px;"
            " color:#fff; padding:7px 10px; font-size:13px; }"
            "QLineEdit#cr-newchar:focus { border:1px solid #6e4cc4; }"
            "QComboBox#cr-show, QComboBox#cr-character {"
            " background:#1a1424; border:1px solid #2a1f3d; border-radius:6px;"
            " color:#fff; padding:6px 10px; font-size:13px; }"
            "QComboBox#cr-show::drop-down, QComboBox#cr-character::drop-down {"
            " border:0; width:18px; }"
            "QComboBox QAbstractItemView { background:#1a1424; color:#ddd;"
            " selection-background-color:#322545; border:1px solid #322545; }"
            "QPushButton#cr-generate { background:#6e4cc4; color:#fff;"
            " border:none; border-radius:8px; padding:10px 22px;"
            " font-size:14px; font-weight:600; }"
            "QPushButton#cr-generate:hover { background:#7d5bd4; }"
            "QPushButton#cr-cancel { background:transparent; color:#aaa;"
            " border:1px solid #3a2c52; border-radius:6px; padding:8px 16px;"
            " font-size:13px; }"
            "QPushButton#cr-cancel:hover { color:#fff;"
            " border-color:#5a4a82; }")

        # 2026-05-19: контент обёрнут в QScrollArea, чтобы при ужатом
        # окне на маленьком экране (14" MBP) нижние кнопки/поля не
        # обрезались — появлялся вертикальный scrollbar. Имя `outer`
        # сохранено: оно теперь привязано к content_widget'у внутри
        # scroll-area, все 16 вызовов `outer.addWidget/addLayout` ниже
        # работают как раньше.
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }")
        content_widget = QWidget()
        outer = QVBoxLayout(content_widget)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(10)
        content_scroll.setWidget(content_widget)
        root_lay.addWidget(content_scroll)

        # ── Описание (textarea) ─────────────────────────────────────────
        self.desc_section_lbl = QLabel(tr('create_ref_desc_section'))
        self.desc_section_lbl.setObjectName("cr-section")
        outer.addWidget(self.desc_section_lbl)

        self.desc_hint_lbl = QLabel(tr('create_ref_desc_hint'))
        self.desc_hint_lbl.setObjectName("cr-hint")
        self.desc_hint_lbl.setWordWrap(True)
        outer.addWidget(self.desc_hint_lbl)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setObjectName("cr-desc")
        self.desc_edit.setPlaceholderText(tr('create_ref_desc_placeholder'))
        self.desc_edit.setFixedHeight(110)
        self.desc_edit.textChanged.connect(self._on_desc_changed)
        outer.addWidget(self.desc_edit)

        # ── Сериал и персонаж ──────────────────────────────────────────
        # Реф ложится в shows/<show>/refs/characters/<character>/, имя файла
        # строится по имени персонажа. Дропдаун персонажей наполняется из
        # episodes.json + папок refs/characters/ выбранного сериала.
        outer.addSpacing(4)
        self.show_section_lbl = QLabel(tr('create_ref_show_section'))
        self.show_section_lbl.setObjectName("cr-section")
        outer.addWidget(self.show_section_lbl)

        self.show_hint_lbl = QLabel(tr('create_ref_show_hint'))
        self.show_hint_lbl.setObjectName("cr-hint")
        self.show_hint_lbl.setWordWrap(True)
        outer.addWidget(self.show_hint_lbl)

        # Строка 1: Сериал
        sh_row = QHBoxLayout()
        sh_row.setSpacing(8)
        self.show_lbl = QLabel(tr('create_ref_show_label'))
        self.show_lbl.setStyleSheet("color:#cfcfcf; font-size:12px;"
                                    " min-width:80px;")
        sh_row.addWidget(self.show_lbl)
        self.show_combo = QComboBox()
        self.show_combo.setObjectName("cr-show")
        self._populate_show_combo()
        self.show_combo.currentIndexChanged.connect(self._on_show_changed)
        _sa.block_wheel_event(self.show_combo)
        sh_row.addWidget(self.show_combo, stretch=1)
        outer.addLayout(sh_row)

        # Строка 2: Персонаж
        ch_row = QHBoxLayout()
        ch_row.setSpacing(8)
        self.char_lbl = QLabel(tr('create_ref_character_label'))
        self.char_lbl.setStyleSheet("color:#cfcfcf; font-size:12px;"
                                    " min-width:80px;")
        ch_row.addWidget(self.char_lbl)
        self.char_combo = QComboBox()
        self.char_combo.setObjectName("cr-character")
        self.char_combo.currentIndexChanged.connect(self._on_character_changed)
        _sa.block_wheel_event(self.char_combo)
        ch_row.addWidget(self.char_combo, stretch=1)
        outer.addLayout(ch_row)

        # Строка 3: Поле для нового персонажа (показывается когда выбрано
        # «➕ Создать нового…» в дропдауне)
        self.new_char_edit = QLineEdit()
        self.new_char_edit.setObjectName("cr-newchar")
        self.new_char_edit.setPlaceholderText(
            tr('create_ref_character_new_placeholder'))
        self.new_char_edit.textChanged.connect(self._update_filename_preview)
        self.new_char_edit.hide()
        outer.addWidget(self.new_char_edit)

        # Строка 4: Превью имени файла (readonly)
        fn_row = QHBoxLayout()
        fn_row.setSpacing(8)
        self.fn_lbl = QLabel(tr('create_ref_filename_label'))
        self.fn_lbl.setStyleSheet("color:#cfcfcf; font-size:12px;"
                                  " min-width:80px;")
        fn_row.addWidget(self.fn_lbl)
        self.filename_edit = QLineEdit()
        self.filename_edit.setObjectName("cr-filename")
        self.filename_edit.setReadOnly(True)
        self.filename_edit.setPlaceholderText("character_red_suit")
        fn_row.addWidget(self.filename_edit, stretch=1)
        self.fn_ext_lbl = QLabel(".jpg")
        self.fn_ext_lbl.setStyleSheet("color:#888; font-size:13px;"
                                      " font-family:'Menlo','Courier New',monospace;")
        fn_row.addWidget(self.fn_ext_lbl)
        outer.addLayout(fn_row)

        # Сначала заполняем character combo для текущего сериала
        self._populate_character_combo()
        # Дефолтное превью имени файла
        self._update_filename_preview()

        # ── Выбор варианта layout-а ─────────────────────────────────────
        outer.addSpacing(6)
        self.var_section_lbl = QLabel(tr('create_ref_variant_section'))
        self.var_section_lbl.setObjectName("cr-section")
        outer.addWidget(self.var_section_lbl)

        var_row = QHBoxLayout()
        var_row.setSpacing(12)
        self.card_detailed = _LayoutVariantCard(
            "detailed",
            tr('create_ref_variant_detailed_title'),
            tr('create_ref_variant_detailed_hint'),
            14)
        # 2026-06-20: «Расширенный» — заглушка (видно, но недоступно).
        # setEnabled(False) → mousePressEvent не доходит, клик невозможен.
        # QGraphicsOpacityEffect 0.4 → визуально приглушён; эффект НЕ зависит
        # от stylesheet карточки, поэтому _apply_style (дёргается setSelected
        # при клике по «Базовому») его НЕ затирает.
        self.card_detailed.setEnabled(False)
        _detailed_dim = QGraphicsOpacityEffect(self.card_detailed)
        _detailed_dim.setOpacity(0.4)
        self.card_detailed.setGraphicsEffect(_detailed_dim)
        self.card_simple = _LayoutVariantCard(
            "simple",
            tr('create_ref_variant_simple_title'),
            tr('create_ref_variant_simple_hint'),
            7)
        self.card_detailed.chosen.connect(self._on_variant_chosen)
        self.card_simple.chosen.connect(self._on_variant_chosen)
        var_row.addWidget(self.card_detailed, stretch=1)
        var_row.addWidget(self.card_simple, stretch=1)
        outer.addLayout(var_row)
        # По дефолту выбран simple (Базовый) — «Расширенный» заглушён выше.
        self.card_simple.setSelected(True)

        # ── Низ: статус + кнопки ────────────────────────────────────────
        outer.addStretch()
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(
            "color:#ffd24d; font-size:12px;")
        outer.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.cancel_btn = QPushButton(tr('create_ref_cancel'))
        self.cancel_btn.setObjectName("cr-cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.generate_btn = QPushButton(tr('create_ref_generate'))
        self.generate_btn.setObjectName("cr-generate")
        self.generate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.generate_btn)
        outer.addLayout(btn_row)

        # Долг 13: применяем префиллы (показ, персонаж, описание) после
        # того как все комбобоксы наполнены.
        self._apply_prefills()

    def _apply_prefills(self):
        """Применяет prefill_show / prefill_character / prefill_description
        если они переданы. Вызывается в самом конце __init__.

        2026-05-05: для wildcard потока («+ Добавить персонажа» из
        РЕФЕРЕНСОВ — `prefill_character == ""` или None) принудительно
        ставим combo на «➕ Создать нового» и **дисейблим** «Сгенерировать»
        пока юзер не выберет реального персонажа из combo или не введёт
        имя в new_char_edit. Это предотвращает баг когда юзер «не заметил»
        что combo по умолчанию указывает на не того персонажа и реф
        прикрепляется к чужой роли."""
        try:
            # 2026-06-24 (монстры 3б-redo+fix): в custom_mode НЕ идём в
            # обычный wildcard (он удаляет «➕ Создать нового»). У монстра —
            # свой placeholder-режим (ветка ниже) который КОПИРУЕТ вид wildcard
            # (placeholder «👇 Выбери персонажа» + красная рамка), но КЕЕПИТ
            # «➕ Создать нового». Сам wildcard и его хендлер НЕ трогаем.
            wildcard_mode = (not bool(self._prefill_character)
                             and not self.custom_mode)
            # 1. Сериал — выставляем по data-полю combobox'а
            if self._prefill_show:
                for i in range(self.show_combo.count()):
                    if self.show_combo.itemData(i) == self._prefill_show:
                        if self.show_combo.currentIndex() != i:
                            self.show_combo.setCurrentIndex(i)
                        break
            # 2. Персонаж
            if self.custom_mode:
                # 2026-06-24 (монстры 3б-fix): поле «Персонаж» — копия
                # актёрского wildcard (placeholder «👇 Выбери персонажа» +
                # красная рамка + дисейбл «Сгенерировать»), НО «➕ Создать
                # нового» СОХРАНЕНА. Реальный персонаж НЕ предвыбран (раньше =
                # «guest_1»). Реактивация кнопки — тем же общим (НЕ изменённым)
                # _on_wildcard_selection_changed. Рамка НЕ снимается после
                # выбора — РОВНО как у актёра.
                self._set_custom_char_placeholder()
                try:
                    self.char_combo.currentIndexChanged.connect(
                        self._on_wildcard_selection_changed)
                except Exception:
                    pass
            elif wildcard_mode:
                # 2026-05-05: для wildcard потока (юзер пришёл из «+
                # Добавить персонажа» в РЕФЕРЕНСАХ) полностью убираем
                # поле «введи имя нового» — имена персонажей берутся
                # из сценария, новых руками не создаём.
                # Combo переставляем: первый item — placeholder
                # «👇 Выбери персонажа», option «➕ Создать нового»
                # удаляется. Combo визуально подсвечен (красная рамка)
                # чтобы юзер сразу видел куда тыкать.
                try:
                    self.new_char_edit.hide()
                except Exception:
                    pass
                # Чистим combo от «__new__» + ставим placeholder на 0
                try:
                    self.char_combo.blockSignals(True)
                    # Удаляем все вхождения __new__
                    for i in range(self.char_combo.count() - 1, -1, -1):
                        if self.char_combo.itemData(i) == "__new__":
                            self.char_combo.removeItem(i)
                    # Удаляем "no_characters" placeholder если есть
                    for i in range(self.char_combo.count() - 1, -1, -1):
                        if self.char_combo.itemData(i) is None:
                            self.char_combo.removeItem(i)
                    # Вставляем наш placeholder в начало
                    self.char_combo.insertItem(
                        0, tr('create_ref_wildcard_pick'), None)
                    self.char_combo.setCurrentIndex(0)
                    self.char_combo.blockSignals(False)
                except Exception:
                    traceback.print_exc()
                # Подсветка combo красным чтобы юзер видел куда тыкать.
                try:
                    self.char_combo.setStyleSheet(
                        "QComboBox#cr-character {"
                        " background:#3a1e26; border:2px solid #ff6464;"
                        " border-radius:6px; color:#fff;"
                        " padding:6px 10px; font-size:13px;"
                        " font-weight:600; }"
                        "QComboBox#cr-character::drop-down {"
                        " border:0; width:18px; }"
                        "QComboBox QAbstractItemView { background:#1a1424;"
                        " color:#ddd;"
                        " selection-background-color:#322545;"
                        " border:1px solid #322545; }")
                except Exception:
                    pass
                # Дисейблим «Сгенерировать» — реактивируется при выборе.
                try:
                    self.generate_btn.setEnabled(False)
                except Exception:
                    pass
                # Хендлер для реактивации.
                try:
                    self.char_combo.currentIndexChanged.connect(
                        self._on_wildcard_selection_changed)
                except Exception:
                    pass
            elif self._prefill_character:
                # Обычный поток (из чата) — slug известен, ищем в combo
                want = self._prefill_character
                found = False
                for i in range(self.char_combo.count()):
                    if self.char_combo.itemData(i) == want:
                        if self.char_combo.currentIndex() != i:
                            self.char_combo.setCurrentIndex(i)
                        found = True
                        break
                if not found:
                    for i in range(self.char_combo.count()):
                        if self.char_combo.itemData(i) == "__new__":
                            self.char_combo.setCurrentIndex(i)
                            break
                    self.new_char_edit.setText(want)
            # 3. Описание (текст одежды от AI)
            if self._prefill_description:
                self.desc_edit.setPlainText(self._prefill_description)
            self._update_filename_preview()
        except Exception:
            traceback.print_exc()

    def _on_wildcard_selection_changed(self):
        """Wildcard-режим: реактивируем «Сгенерировать» когда юзер
        выбрал реального персонажа в combo. Placeholder (data=None)
        не считается — кнопка остаётся disabled."""
        try:
            data = self.char_combo.currentData()
            slug = str(data) if data else ""
            self.generate_btn.setEnabled(bool(slug))
        except Exception:
            self.generate_btn.setEnabled(True)

    def _set_custom_char_placeholder(self):
        """2026-06-24 (монстры 3б-fix): custom_mode — поле «Персонаж» 1:1 как
        у актёрского wildcard: placeholder «👇 Выбери персонажа» (data=None) на
        index 0, та же красная рамка (НЕ снимается после выбора — как у актёра),
        «Сгенерировать» дисейблится. «➕ Создать нового» СОХРАНЕНА. Реальный
        персонаж НЕ предвыбирается (раньше combo вставал на первого = «guest_1»).
        Реактивация кнопки — общим _on_wildcard_selection_changed. Вызывается
        после каждого _populate_character_combo (в т.ч. при смене сериала).

        Красный QSS — КОПИЯ строки из wildcard-ветки (актёрскую ветку НЕ
        трогаем, её поведение не меняем; небольшое дублирование строки —
        цена того что актёрский флоу остаётся байт-в-байт)."""
        try:
            self.new_char_edit.hide()
            self.char_combo.blockSignals(True)
            # убрать «(пока нет)» placeholder (data=None) если есть —
            # вставим свой единый placeholder вместо него.
            for i in range(self.char_combo.count() - 1, -1, -1):
                if self.char_combo.itemData(i) is None:
                    self.char_combo.removeItem(i)
            self.char_combo.insertItem(
                0, tr('create_ref_wildcard_pick'), None)
            self.char_combo.setCurrentIndex(0)
            self.char_combo.blockSignals(False)
            self.char_combo.setStyleSheet(
                "QComboBox#cr-character {"
                " background:#3a1e26; border:2px solid #ff6464;"
                " border-radius:6px; color:#fff;"
                " padding:6px 10px; font-size:13px;"
                " font-weight:600; }"
                "QComboBox#cr-character::drop-down {"
                " border:0; width:18px; }"
                "QComboBox QAbstractItemView { background:#1a1424;"
                " color:#ddd;"
                " selection-background-color:#322545;"
                " border:1px solid #322545; }")
            self.generate_btn.setEnabled(False)
        except Exception:
            traceback.print_exc()

    def _on_variant_chosen(self, variant_id: str):
        self._selected_variant = variant_id
        self.card_detailed.setSelected(variant_id == "detailed")
        self.card_simple.setSelected(variant_id == "simple")

    # ─── Сериал / Персонаж — наполнение и реактивность ──────────────

    def _populate_show_combo(self):
        """Заполняет дропдаун сериалов из shows/. По умолчанию выбран
        активный (current_show.json)."""
        self.show_combo.clear()
        try:
            shows = _sa.list_shows(self.project_root)
        except Exception:
            shows = []
        if not shows:
            # Нет сериалов вообще — дропдаун с placeholder'ом
            self.show_combo.addItem(tr('create_ref_no_shows'), "")
            self.show_combo.setEnabled(False)
            return
        for s in shows:
            self.show_combo.addItem(s, s)
        # Дефолт — активный сериал
        try:
            active = _sa.get_current_show(self.project_root)
        except Exception:
            active = None
        if active:
            for i in range(self.show_combo.count()):
                if self.show_combo.itemData(i) == active:
                    self.show_combo.setCurrentIndex(i)
                    break

    def _populate_character_combo(self):
        """Перенаполняет дропдаун персонажей под выбранный сериал.
        Источник — list_show_characters (episodes.json + папки рефов).
        В конец добавляется опция «➕ Создать нового персонажа…»."""
        self.char_combo.blockSignals(True)
        self.char_combo.clear()
        show = self.show_combo.currentData() or ""
        chars = []
        if show:
            try:
                chars = _sa.list_show_characters(self.project_root, show)
            except Exception:
                chars = []
        if chars:
            for c in chars:
                self.char_combo.addItem(c, c)
        else:
            # Сериал есть, но персонажей пока нет — даём подсказку как
            # disabled-айтем (data=None), чтобы юзер сразу пошёл в «новый»
            self.char_combo.addItem(tr('create_ref_no_characters_yet'), None)
        # «➕ Создать нового персонажа…» (data="__new__")
        self.char_combo.addItem(tr('create_ref_character_new_option'), "__new__")
        self.char_combo.blockSignals(False)
        # Если был только placeholder + new — выбираем new и показываем поле
        self._on_character_changed(self.char_combo.currentIndex())

    def _on_show_changed(self, _index: int):
        """Смена сериала → перезагрузка списка персонажей."""
        self._populate_character_combo()
        # 2026-06-24 (монстры 3б-fix): в custom_mode после ребилда списка
        # снова ставим placeholder + красную рамку — иначе combo авто-выбрал
        # бы первого персонажа нового сериала (баг с «guest_1»).
        if getattr(self, 'custom_mode', False):
            self._set_custom_char_placeholder()
        self._update_filename_preview()

    def _on_character_changed(self, _index: int):
        """Смена персонажа → показать/скрыть поле ввода нового имени и
        обновить превью имени файла."""
        data = self.char_combo.currentData()
        if data == "__new__":
            self.new_char_edit.show()
            self.new_char_edit.setFocus()
        else:
            self.new_char_edit.hide()
        self._update_filename_preview()

    def _current_character_slug(self) -> str:
        """Текущее имя персонажа: либо выбранное в дропдауне, либо
        введённое в поле новой роли. Транслитерируется и чистится."""
        data = self.char_combo.currentData()
        if data == "__new__":
            raw = self.new_char_edit.text().strip()
        elif data:
            return str(data)
        else:
            raw = ""
        # Транслит для нового имени (пользователь может ввести по-русски)
        slug = _sa.transliterate_for_filename(raw, max_words=2, max_len=24) \
            if raw else ""
        if not slug:
            slug = re.sub(r'[^a-zA-Z0-9_-]', '_', raw).strip('_-').lower()
        return slug

    def _update_filename_preview(self):
        """Обновляет readonly-превью имени файла:
            <character>_<desc_slug>.jpg
        Вызывается при изменении описания, персонажа или сериала."""
        try:
            desc = self.desc_edit.toPlainText().strip()
            char = self._current_character_slug()
            if not char:
                self.filename_edit.setText("")
                return
            self.filename_edit.setText(_sa.build_actor_ref_filename(char, desc))
        except Exception:
            pass

    def _on_desc_changed(self):
        """При вводе описания обновляем превью имени файла."""
        self._update_filename_preview()

    def _on_generate(self):
        """Делегирует запуск GenerateActorRefThread в ActorsView (owner_view).
        Поток живёт в ActorsView (а не в этом диалоге!), потому что диалог
        закрывается через accept() сразу после старта — если бы поток был
        ребёнком диалога, Qt убил бы работающий QThread и вызвал qFatal."""
        try:
            desc = self.desc_edit.toPlainText().strip()
            show = self.show_combo.currentData() or ""
            character = self._current_character_slug()
            if not show:
                self.status_lbl.setText(tr('create_ref_no_shows'))
                return
            if not character:
                # Юзер выбрал «➕ Создать нового» но ничего не ввёл
                self.status_lbl.setText(tr('create_ref_character_new_placeholder'))
                self.new_char_edit.setFocus()
                return
            filename = _sa.build_actor_ref_filename(character, desc)
            filename = re.sub(r'[^a-zA-Z0-9_-]', '_', filename)
            if not filename:
                filename = character

            # 2026-06-24 (монстры 3б-redo): custom_mode — генерация листа
            # нестандартного персонажа. Тот же target_dir что у актёра
            # (shows/<show>/refs/characters/<character>), но: text2img (без
            # identity_anchor), ACTOR_REF_PROMPT_CUSTOM, и БЕЗ set_actor_role
            # (actors.json синкается коллегам — не трогаем). Карточка монстра
            # локальная (custom.json). Актёрский путь ниже НЕ затрагивается.
            if self.custom_mode:
                if not desc:
                    self.status_lbl.setText(tr('create_ref_desc_placeholder'))
                    self.desc_edit.setFocus()
                    return
                if self.owner_view is None:
                    self.status_lbl.setText(tr('create_ref_failed'))
                    return
                prompt = _sa.ACTOR_REF_PROMPT_CUSTOM.format(description=desc)
                target_dir = (self.project_root / "shows" / show / "refs"
                              / "characters" / character)
                data = self.char_combo.currentData()
                if data == "__new__":
                    display_label = self.new_char_edit.text().strip() or character
                else:
                    display_label = self.char_combo.currentText() or character
                ok = self.owner_view.start_custom_ref_generation(
                    character, display_label, desc, prompt, filename, target_dir)
                if ok:
                    self.accept()
                else:
                    self.status_lbl.setText(tr('create_ref_failed'))
                return

            outfit_text = desc if desc else (
                "Keep the person's appearance exactly as in the reference "
                "images. Use the same clothing visible in the reference photos.")
            tmpl = (_sa.ACTOR_REF_PROMPT_DETAILED
                    if self._selected_variant == "detailed"
                    else _sa.ACTOR_REF_PROMPT_SIMPLE)
            identity_anchor = "\n".join(
                f"[@]img{i + 1} {p.name} = {self.display_name}"
                for i, p in enumerate(self.photos)
            )
            prompt = tmpl.format(outfit=outfit_text,
                                 identity_anchor=identity_anchor)

            if self.owner_view is None:
                self.status_lbl.setText(tr('create_ref_failed'))
                return
            target_dir = (self.project_root / "shows" / show / "refs"
                          / "characters" / character)
            # Записываем связь «актёр → персонаж в сериале» в actors.json.
            # Это нужно чтобы кнопка «🖼 Все референсы (N)» на карточке
            # актёра знала какую папку персонажа открывать.
            try:
                _sa.set_actor_role(self.project_root, self.actor_slug,
                                   show, character)
            except Exception:
                traceback.print_exc()
            self.owner_view.start_ref_generation(
                self.actor_slug, self.photos, prompt, filename,
                self.display_name, target_dir, outfit_text=desc,
                variant_id=self._selected_variant)
            # Закрываем попап — поток живёт в ActorsView, не зависит от диалога
            self.accept()
        except Exception:
            traceback.print_exc()
            self.status_lbl.setText(tr('create_ref_failed'))


# ─── Стек pending-вариантов character-рефа ───────────────────────

class RefResultDialog(QDialog):
    """Попап со стеком сгенерированных character-референсов.

    UX:
    - Открывается ТОЛЬКО по клику на «🆕 Готов новый референс (N)» на
      карточке актёра. Никогда не открывается автоматически из finished.
    - Содержит initial_variants — это `_pending_variants[slug]` из
      ActorsView. Все варианты которые юзер ещё не подтвердил.
    - Юзер кликает по thumbnail внизу → переключает большое превью.
    - Кнопка «Пересоздать» → попап ЗАКРЫВАЕТСЯ сам (через self.close()),
      на карточке снова бежит прогресс. Когда новая генерация кончится →
      добавляется в pending. При следующем клике на «Готов новый» юзер
      увидит уже все накопленные варианты.
    - Кнопка «✓ Оставить этот, остальные удалить» → выбранный остаётся,
      остальные удаляются с диска, pending очищается, попап закрывается.
    """

    BIG_MAX = 700
    THUMB_SIZE = 90

    def __init__(self, actor_slug: str, display_name: str,
                 photos: List[Path], initial_variants: List[str],
                 owner_view, initial_outfit: str = "",
                 variant_id: str = "detailed", parent=None):
        super().__init__(parent)
        self.actor_slug = actor_slug
        self.display_name = display_name
        self.photos = list(photos)
        self.owner_view = owner_view
        self._variant_id = variant_id
        self._initial_outfit = initial_outfit

        self._variants: List[str] = list(initial_variants)
        self._current_idx: int = 0

        self.setWindowTitle(tr('ref_result_title', name=display_name))
        self.setModal(False)
        self.resize(900, 880)
        self.setStyleSheet(
            "QDialog { background:#15101e; }"
            "QLabel#rr-section { color:#cfcfcf; font-size:12px;"
            " font-weight:700; letter-spacing:1px; }"
            "QLabel#rr-hint { color:#aaa; font-size:12px; }"
            "QLabel#rr-counter { color:#ffd24d; font-size:12px;"
            " font-weight:600; }"
            "QPlainTextEdit#rr-desc {"
            " background:#1a1424; border:1px solid #2a1f3d; border-radius:8px;"
            " color:#fff; padding:10px; font-size:13px; }"
            "QPlainTextEdit#rr-desc:focus { border:1px solid #6e4cc4; }"
            "QLineEdit#rr-filename {"
            " background:#1a1424; border:1px solid #2a1f3d; border-radius:6px;"
            " color:#fff; padding:7px 10px; font-size:13px;"
            " font-family: 'Menlo','Courier New',monospace; }"
            "QLineEdit#rr-filename:focus { border:1px solid #6e4cc4; }"
            "QPushButton#rr-regen { background:#6e4cc4; color:#fff;"
            " border:none; border-radius:8px; padding:10px 20px;"
            " font-size:13px; font-weight:600; }"
            "QPushButton#rr-regen:hover { background:#7d5bd4; }"
            "QPushButton#rr-regen:disabled { background:#3a2c52; color:#888; }"
            "QPushButton#rr-done { background:#3a8c52; color:#fff;"
            " border:none; border-radius:6px; padding:8px 18px;"
            " font-size:13px; font-weight:600; }"
            "QPushButton#rr-done:hover { background:#4d9e6b; }"
            "QPushButton#rr-done:disabled { background:#3a2c52; color:#888; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)

        self.preview_lbl = QLabel()
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_lbl.setStyleSheet(
            "background:#1a1424; border-radius:10px;")
        self.preview_lbl.setMinimumHeight(380)
        outer.addWidget(self.preview_lbl, stretch=1)

        strip_row = QHBoxLayout()
        strip_row.setSpacing(0)
        self.variants_counter = QLabel("")
        self.variants_counter.setObjectName("rr-counter")
        strip_row.addWidget(self.variants_counter)
        strip_row.addStretch()
        outer.addLayout(strip_row)

        self.strip_widget = QWidget()
        self.strip_widget.setStyleSheet("background: transparent;")
        self.strip_layout = QHBoxLayout(self.strip_widget)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(8)
        outer.addWidget(self.strip_widget)

        self.desc_section_lbl = QLabel(tr('ref_result_desc_section'))
        self.desc_section_lbl.setObjectName("rr-section")
        outer.addWidget(self.desc_section_lbl)

        self.desc_hint_lbl = QLabel(tr('ref_result_desc_hint'))
        self.desc_hint_lbl.setObjectName("rr-hint")
        self.desc_hint_lbl.setWordWrap(True)
        outer.addWidget(self.desc_hint_lbl)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setObjectName("rr-desc")
        self.desc_edit.setPlaceholderText(tr('create_ref_desc_placeholder'))
        self.desc_edit.setPlainText(initial_outfit)
        self.desc_edit.setFixedHeight(70)
        self.desc_edit.textChanged.connect(self._on_desc_changed)
        outer.addWidget(self.desc_edit)

        fn_row = QHBoxLayout()
        fn_row.setSpacing(8)
        self.fn_lbl = QLabel(tr('create_ref_filename_label'))
        self.fn_lbl.setStyleSheet("color:#cfcfcf; font-size:12px;")
        fn_row.addWidget(self.fn_lbl)
        self.filename_edit = QLineEdit()
        self.filename_edit.setObjectName("rr-filename")
        first_path = self._variants[0] if self._variants else ""
        self.filename_edit.setText(Path(first_path).stem if first_path else self.actor_slug)
        fn_row.addWidget(self.filename_edit, stretch=1)
        self.fn_ext_lbl = QLabel(".jpg")
        self.fn_ext_lbl.setStyleSheet(
            "color:#888; font-size:13px;"
            " font-family:'Menlo','Courier New',monospace;")
        fn_row.addWidget(self.fn_ext_lbl)
        outer.addLayout(fn_row)

        btn_row = QHBoxLayout()
        self.delete_all_btn = QPushButton(tr('ref_result_delete_all'))
        self.delete_all_btn.setStyleSheet(
            "QPushButton { background:transparent; color:#c4304c;"
            " border:1px solid #c4304c; border-radius:6px;"
            " padding:8px 14px; font-size:12px; font-weight:600; }"
            "QPushButton:hover { background:#c4304c; color:#fff; }")
        self.delete_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_all_btn.clicked.connect(self._on_delete_all)
        btn_row.addWidget(self.delete_all_btn)
        btn_row.addStretch()

        self.regen_btn = QPushButton(tr('ref_result_regen'))
        self.regen_btn.setObjectName("rr-regen")
        self.regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.regen_btn.clicked.connect(self._on_regen)
        btn_row.addWidget(self.regen_btn)

        self.done_btn = QPushButton(tr('ref_result_done_keep'))
        self.done_btn.setObjectName("rr-done")
        self.done_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_btn.clicked.connect(self._on_done)
        btn_row.addWidget(self.done_btn)
        outer.addLayout(btn_row)

        self._refresh_preview()
        self._refresh_strip()

    @property
    def target_path(self) -> str:
        """Путь к ТЕКУЩЕМУ выбранному варианту (используется снаружи)."""
        if 0 <= self._current_idx < len(self._variants):
            return self._variants[self._current_idx]
        return ""

    def _refresh_preview(self):
        """Загружает текущий вариант в превью с KeepAspectRatio."""
        try:
            path = self.target_path
            if not path:
                return
            pix = QPixmap(path)
            if pix.isNull():
                return
            avail_w = max(400, self.width() - 60)
            target = min(self.BIG_MAX, avail_w)
            pix = pix.scaled(
                QSize(target, target),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.preview_lbl.setPixmap(pix)
            self.filename_edit.setText(Path(path).stem)
        except Exception:
            traceback.print_exc()

    def _refresh_strip(self):
        """Перерисовывает стрипу thumbnails. Показывается только когда
        вариантов >1. Текущий выделен жёлтым лейблом #N."""
        while self.strip_layout.count():
            it = self.strip_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        n = len(self._variants)
        if n <= 1:
            self.strip_widget.hide()
            self.variants_counter.setText("")
            return
        self.strip_widget.show()
        self.variants_counter.setText(
            tr('ref_result_variants_count', n=n))

        for i, path in enumerate(self._variants):
            cell = self._make_variant_cell(i, path,
                                           selected=(i == self._current_idx))
            self.strip_layout.addWidget(cell)
        self.strip_layout.addStretch()

    def _make_variant_cell(self, idx: int, path: str, selected: bool) -> QWidget:
        """Один thumbnail в стрипе: превью + номер #N + кнопка
        «🗑 Удалить» в виде полной строки под thumbnail.
        Клик по thumbnail — переключает текущий выбранный."""
        cell = QFrame()
        cell.setFixedSize(self.THUMB_SIZE + 4, self.THUMB_SIZE + 44)
        cell.setStyleSheet("QFrame { background:transparent; border:none; }")

        v = QVBoxLayout(cell)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(3)

        thumb = QLabel()
        thumb.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(
            "QLabel { background:#1a1424; border-radius:4px; }"
            "QLabel:hover { background:#241830; }")
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        try:
            pix = QPixmap(path)
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(self.THUMB_SIZE - 2, self.THUMB_SIZE - 2),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                thumb.setPixmap(pix)
        except Exception:
            pass
        thumb.mousePressEvent = lambda ev, _i=idx: self._on_select_variant(_i)
        v.addWidget(thumb)

        num = QLabel(f"#{idx + 1}")
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if selected:
            num.setStyleSheet(
                "color:#ffd24d; font-size:11px; font-weight:700;"
                " background:transparent;")
        else:
            num.setStyleSheet(
                "color:#888; font-size:10px; background:transparent;")
        v.addWidget(num)

        del_btn = QPushButton(tr('ref_result_delete_variant'))
        del_btn.setFixedHeight(22)
        del_btn.setStyleSheet(
            "QPushButton { background:#3a2c52; color:#d8c8ff;"
            " border:none; border-radius:4px; padding:2px 4px;"
            " font-size:11px; font-weight:600; }"
            "QPushButton:hover { background:#c4304c; color:#fff; }")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(lambda _, _i=idx: self._on_delete_variant(_i))
        v.addWidget(del_btn)
        return cell

    def _on_select_variant(self, idx: int):
        """Клик по thumbnail → переключить большое превью."""
        if 0 <= idx < len(self._variants) and idx != self._current_idx:
            self._current_idx = idx
            self._refresh_preview()
            self._refresh_strip()

    def _on_delete_variant(self, idx: int):
        """Удалить файл варианта с диска и убрать из списка. Если это
        последний оставшийся — попап закрывается (юзер удалил все, ушёл)."""
        if not (0 <= idx < len(self._variants)):
            return
        path = self._variants[idx]
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            traceback.print_exc()
        del self._variants[idx]
        if not self._variants:
            self._sync_pending_to_owner()
            self.accept()
            return
        if self._current_idx >= len(self._variants):
            self._current_idx = len(self._variants) - 1
        elif self._current_idx > idx:
            self._current_idx -= 1
        self._refresh_preview()
        self._refresh_strip()
        self._sync_pending_to_owner()

    def _on_delete_all(self):
        """Кнопка «🗑 Удалить все варианты»: удаляет все файлы с диска,
        очищает pending, закрывает попап. Юзер психанул — ушёл."""
        try:
            for p in self._variants:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    traceback.print_exc()
            self._variants = []
            self._sync_pending_to_owner()
            self.accept()
        except Exception:
            traceback.print_exc()
            self.accept()

    def _on_desc_changed(self):
        """Если описание поменялось — обновляем имя файла (если оно
        выглядит как auto-generated, т.е. начинается с актёрского slug)."""
        try:
            desc = self.desc_edit.toPlainText().strip()
            current = self.filename_edit.text().strip()
            new_auto = _sa.build_actor_ref_filename(self.actor_slug, desc)
            if not current or current == self.actor_slug or current.startswith(
                    self.actor_slug + "_"):
                self.filename_edit.setText(new_auto)
        except Exception:
            pass

    def append_variant(self, path: str):
        """Добавить новый сгенерированный вариант в стек (вызывается из
        ActorsView когда юзер запускает «Пересоздать» при открытом попапе
        и генерация завершилась)."""
        try:
            self._variants.append(path)
            self._current_idx = len(self._variants) - 1
            self._refresh_preview()
            self._refresh_strip()
        except Exception:
            traceback.print_exc()

    def closeEvent(self, ev):
        """Закрытие через X — синхронизируем pending в owner_view (если
        юзер удалял ✕ — отразить количество). Файлы НЕ удаляются."""
        try:
            self._sync_pending_to_owner()
        except Exception:
            traceback.print_exc()
        super().closeEvent(ev)

    def _sync_pending_to_owner(self):
        """Синхронизирует self._variants → owner_view._pending_variants[slug]."""
        try:
            if self.owner_view is not None:
                self.owner_view.update_pending_variants(
                    self.actor_slug, list(self._variants))
        except Exception:
            traceback.print_exc()

    def _on_regen(self):
        """«Пересоздать»: запускаем новую генерацию через owner_view и
        ЗАКРЫВАЕМ попап. Когда генерация финиширует — путь добавится в
        pending, юзер увидит «🆕 Готов новый референс (N+1)» на карточке."""
        try:
            desc = self.desc_edit.toPlainText().strip()
            filename = self.filename_edit.text().strip()
            if not filename:
                filename = self.actor_slug
            filename = re.sub(r'[^a-zA-Z0-9_-]', '_', filename)
            if not filename:
                filename = self.actor_slug

            outfit_text = desc if desc else (
                "Keep the person's appearance exactly as in the reference "
                "images. Use the same clothing visible in the reference photos.")
            tmpl = (_sa.ACTOR_REF_PROMPT_DETAILED
                    if self._variant_id == "detailed"
                    else _sa.ACTOR_REF_PROMPT_SIMPLE)
            identity_anchor = "\n".join(
                f"[@]img{i + 1} {p.name} = {self.display_name}"
                for i, p in enumerate(self.photos)
            )
            prompt = tmpl.format(outfit=outfit_text,
                                 identity_anchor=identity_anchor)

            self._sync_pending_to_owner()
            # Регенерация в ту же папку что у уже существующих вариантов —
            # это `shows/<show>/refs/characters/<character>/`. Берём из
            # parent любого варианта (все варианты лежат в одной папке).
            try:
                target_dir = Path(self._variants[0]).parent if self._variants \
                    else Path(self.target_path).parent
            except Exception:
                target_dir = Path()
            self.owner_view.start_ref_generation(
                self.actor_slug, self.photos, prompt, filename,
                self.display_name, target_dir, outfit_text=desc,
                variant_id=self._variant_id)
            self.accept()
        except Exception:
            traceback.print_exc()
            self.accept()

    def _on_done(self):
        """«✓ Оставить этот, остальные удалить»: удаляет ВСЕ варианты с
        диска кроме текущего, очищает pending в owner_view, закрывает попап.
        После закрытия открывает наложение сетки на сохранённый реф."""
        # target_path — @property от _variants[_current_idx]; захватываем ДО
        # accept(), т.к. после закрытия попапа обращение может стать невалидным.
        kept_path = self.target_path
        try:
            for i, p in enumerate(self._variants):
                if i == self._current_idx:
                    continue
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    traceback.print_exc()
            if self.owner_view is not None:
                self.owner_view.confirm_pending_kept(
                    self.actor_slug, self.target_path)
            self.accept()
        except Exception:
            traceback.print_exc()
        # 2026-06-05: после фиксации варианта и закрытия окна — сразу открыть
        # наложение сетки на сохранённый реф (переиспользуем логику ActorsView,
        # которая сама считает save_path и открывает ActorGridDialog). Свой
        # try/except: падение грида не должно ломать уже-сделанный выбор.
        try:
            if self.owner_view is not None:
                self.owner_view._on_apply_grid_to_ref(kept_path)
        except Exception:
            traceback.print_exc()


# ─── Этап 2 (2026-05-17): диалог наложения текстуры на реф актёра ────────────


class _TexturePickerThumb(QFrame):
    """Превью одной текстуры (90×90) в picker'е ApplyTextureDialog.

    Отличия от `_TextureThumb` (views/actors.py):
      • НЕТ кнопки «Удалить» (picker — read-only).
      • НЕТ fullscreen-просмотра по клику.
      • Клик → emit clicked(Path) для выбора в picker'е.
      • Selected-state: рамка меняет цвет (золотая когда выбрана).

    Создаём отдельный класс, а не реюзаем `_TextureThumb` из views/actors.py
    — у того другая семантика (storage management vs picker).
    """

    THUMB_SIZE = 90

    clicked = pyqtSignal(object)   # Path

    def __init__(self, file_path: Path, parent=None):
        super().__init__(parent)
        self._file_path = Path(file_path)
        self._is_selected = False
        self.setObjectName("texture-picker-thumb")
        self._apply_style()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        self._img_lbl = QLabel()
        self._img_lbl.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet(
            "background:#1a1424; border-radius:4px;")
        try:
            pix = QPixmap(str(self._file_path))
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(self.THUMB_SIZE - 4, self.THUMB_SIZE - 4),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self._img_lbl.setPixmap(pix)
        except Exception:
            pass
        lay.addWidget(self._img_lbl,
                      alignment=Qt.AlignmentFlag.AlignCenter)

    def _apply_style(self):
        if self._is_selected:
            self.setStyleSheet(
                "QFrame#texture-picker-thumb {"
                " background:rgba(212,162,86,0.20);"
                " border:2px solid #d4a256;"
                " border-radius:6px; }")
        else:
            self.setStyleSheet(
                "QFrame#texture-picker-thumb {"
                " background:transparent;"
                " border:2px solid transparent;"
                " border-radius:6px; }"
                "QFrame#texture-picker-thumb:hover {"
                " border-color:#6e4cc4; }")

    def set_selected(self, selected: bool):
        self._is_selected = bool(selected)
        self._apply_style()

    def file_path(self) -> Path:
        return self._file_path

    def mousePressEvent(self, ev):
        try:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self._file_path)
        except Exception:
            traceback.print_exc()
        super().mousePressEvent(ev)


class _PreviewLabel(QLabel):
    """2026-05-17 (Этап 2 доп): QLabel с drag-handler'ами для перетаскивания
    текстуры внутри preview.

    Сигнал drag_moved(dx, dy) эмитится при движении мыши с зажатой ЛКМ,
    delta в координатах preview-pixels (caller сам конвертирует в
    full-size через `_preview_ratio`).

    Drag активируется через set_drag_enabled(True) — например когда
    zoom > 100%. При zoom = 100% перетаскивать нечего, курсор Arrow,
    события игнорируются.
    """

    drag_moved = pyqtSignal(int, int)   # dx, dy in preview pixels

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_enabled = False
        self._dragging = False
        self._last_pos = None
        self.setMouseTracking(False)

    def set_drag_enabled(self, enabled: bool):
        self._drag_enabled = bool(enabled)
        if not self._drag_enabled:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, ev):
        try:
            if (self._drag_enabled
                    and ev.button() == Qt.MouseButton.LeftButton):
                self._dragging = True
                # PyQt6: event.position() возвращает QPointF — берём int.
                try:
                    p = ev.position()
                    self._last_pos = (int(p.x()), int(p.y()))
                except Exception:
                    p = ev.pos()
                    self._last_pos = (int(p.x()), int(p.y()))
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
        except Exception:
            traceback.print_exc()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        try:
            if self._dragging and self._last_pos is not None:
                try:
                    p = ev.position()
                    x, y = int(p.x()), int(p.y())
                except Exception:
                    p = ev.pos()
                    x, y = int(p.x()), int(p.y())
                dx = x - self._last_pos[0]
                dy = y - self._last_pos[1]
                self._last_pos = (x, y)
                if dx or dy:
                    self.drag_moved.emit(dx, dy)
        except Exception:
            traceback.print_exc()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        try:
            if (self._dragging
                    and ev.button() == Qt.MouseButton.LeftButton):
                self._dragging = False
                if self._drag_enabled:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
        except Exception:
            traceback.print_exc()
        super().mouseReleaseEvent(ev)


class ApplyTextureDialog(QDialog):
    """Picker текстуры + слайдер opacity + live preview.

    Возвращает через accept():
      • selected_texture: Optional[Path]  — выбранная текстура
      • selected_opacity: int             — 10..100 (slider value)

    Структура:
      ┌─ заголовок «Наложить текстуру»
      ├─ horizontal split:
      │   left (60%):  QScrollArea с сеткой превью текстур (~4 в ряду)
      │   right (40%): QLabel preview ~300×300 (downscaled ref+texture)
      ├─ слайдер opacity 10..100 + label
      └─ Apply (Ok) | Cancel

    Если папка `textures_dir` пуста — вместо сетки empty-сообщение,
    кнопка Apply скрыта, осталась только Close.

    Live preview: PIL.blend на downscaled версии ref'а + текстуры
    (preview-size фиксированный, ~300px). Debounce 200ms через QTimer
    single-shot — иначе на каждое движение слайдера CPU спайк.

    Cross-platform: только PyQt6 + PIL, никакого subprocess.
    """

    PREVIEW_MAX = 460
    DEBOUNCE_MS = 200

    def __init__(self, ref_path: Path, textures_dir: Path,
                 parent=None, result_dir: Optional[Path] = None):
        super().__init__(parent)
        self.ref_path = Path(ref_path)
        self.textures_dir = Path(textures_dir)
        # 2026-05-17 (Этап 2 патч): путь к папке результатов
        # (shows/<show>/refs/characters_texture/<character>/). Используется
        # для load_meta_for_source — восстановление настроек последней
        # применённой текстуры для этого ref'а.
        self.result_dir: Optional[Path] = (
            Path(result_dir) if result_dir else None)
        # texture_name → dict с {opacity, zoom, offset_x, offset_y, ...}
        # Заполняется в _load_meta_for_source() после _scan_textures.
        self._meta_by_texture: Dict[str, Dict] = {}
        self.selected_texture: Optional[Path] = None
        self.selected_opacity: int = 30
        # 2026-05-17 (Этап 2 доп): zoom + drag-offset текстуры.
        # zoom_percent: 100..300, default 100 (нет zoom).
        # offset_x/y — смещение центра crop'а в full-size base-px.
        self.selected_zoom: int = 100
        self.selected_offset_x: int = 0
        self.selected_offset_y: int = 0
        # Кеш для перевода координат preview ↔ full-size base.
        # Заполняется в _update_preview.
        self._preview_ratio: float = 1.0
        self._cached_base_size: Optional[tuple] = None
        self._thumbs: List[_TexturePickerThumb] = []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(self.DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._update_preview)

        self.setWindowTitle(tr('apply_texture_dialog_title'))
        self.setModal(True)
        self.resize(740, 520)
        self.setStyleSheet(
            "QDialog { background:#15101e; }"
            "QLabel#apply-tex-section { color:#d8c8ff;"
            " font-size:12px; font-weight:600;"
            " background:transparent; padding:4px 0; }"
            "QLabel#apply-tex-empty { color:#888; font-size:13px;"
            " font-style:italic; padding:24px;"
            " background:transparent; }"
            "QLabel#apply-tex-preview { background:#0a0612;"
            " border:1px solid #2c2240; border-radius:6px; }"
            "QLabel#apply-tex-opacity { color:#ddd;"
            " font-size:13px; background:transparent; }"
            "QPushButton#apply-tex-apply { background:#3a2c52;"
            " color:#fff; border:none; border-radius:6px;"
            " padding:8px 18px; font-size:13px; font-weight:600; }"
            "QPushButton#apply-tex-apply:hover { background:#4d3a6b; }"
            "QPushButton#apply-tex-apply:disabled { color:#666;"
            " background:#1a1330; }"
            "QPushButton#apply-tex-cancel { background:transparent;"
            " color:#cfcfcf; border:1px solid #3a2c52;"
            " border-radius:6px; padding:8px 16px; font-size:13px; }"
            "QPushButton#apply-tex-cancel:hover {"
            " background:#1f1730; color:#fff; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        split = QHBoxLayout()
        split.setSpacing(14)

        # left: picker
        left = QVBoxLayout()
        left.setSpacing(6)
        sec_pick = QLabel(tr('apply_texture_section_picker'))
        sec_pick.setObjectName("apply-tex-section")
        left.addWidget(sec_pick)

        textures = self._scan_textures()
        if not textures:
            empty = QLabel(tr('apply_texture_no_textures'))
            empty.setObjectName("apply-tex-empty")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            left.addWidget(empty, stretch=1)
            self._has_textures = False
        else:
            self._has_textures = True
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet(
                "QScrollArea { background:transparent;"
                " border:1px solid #2c2240; border-radius:6px; }")
            inner = QWidget()
            inner.setStyleSheet("background:transparent;")
            grid = QGridLayout(inner)
            grid.setSpacing(8)
            grid.setContentsMargins(8, 8, 8, 8)
            cols = 3
            for i, tx in enumerate(textures):
                thumb = _TexturePickerThumb(tx, parent=inner)
                thumb.clicked.connect(self._on_texture_clicked)
                r, c = divmod(i, cols)
                grid.addWidget(thumb, r, c)
                self._thumbs.append(thumb)
            for c in range(cols):
                grid.setColumnStretch(c, 0)
            grid.setColumnStretch(cols, 1)
            scroll.setWidget(inner)
            left.addWidget(scroll, stretch=1)
        split.addLayout(left, stretch=4)

        # right: preview
        right = QVBoxLayout()
        right.setSpacing(6)
        sec_prev = QLabel(tr('apply_texture_section_preview'))
        sec_prev.setObjectName("apply-tex-section")
        right.addWidget(sec_prev)
        self._preview_lbl = _PreviewLabel()
        self._preview_lbl.setObjectName("apply-tex-preview")
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_lbl.setMinimumSize(420, 420)
        # Drag активируется когда zoom>100 (см. _on_zoom_changed).
        self._preview_lbl.drag_moved.connect(self._on_drag_moved)
        right.addWidget(self._preview_lbl, stretch=1)
        # Подсказка под preview — видна только когда zoom>100.
        self._drag_hint_lbl = QLabel(tr('apply_texture_drag_hint'))
        self._drag_hint_lbl.setStyleSheet(
            "color:#888; font-size:11px; background:transparent;"
            " padding-top:4px;")
        self._drag_hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_hint_lbl.hide()
        right.addWidget(self._drag_hint_lbl)
        split.addLayout(right, stretch=6)
        outer.addLayout(split)

        # opacity слайдер
        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(12)
        self._opacity_lbl = QLabel(
            tr('apply_texture_opacity_label', n=self.selected_opacity))
        self._opacity_lbl.setObjectName("apply-tex-opacity")
        self._opacity_lbl.setMinimumWidth(180)
        opacity_row.addWidget(self._opacity_lbl)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(10)
        self._slider.setMaximum(100)
        self._slider.setSingleStep(5)
        self._slider.setPageStep(10)
        self._slider.setValue(self.selected_opacity)
        self._slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self._slider, stretch=1)
        outer.addLayout(opacity_row)

        # 2026-05-17 (Этап 2 доп): zoom слайдер 100..300% + reset позиции.
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(12)
        self._zoom_lbl = QLabel(
            tr('apply_texture_zoom_label', n=self.selected_zoom))
        self._zoom_lbl.setObjectName("apply-tex-opacity")
        self._zoom_lbl.setMinimumWidth(180)
        zoom_row.addWidget(self._zoom_lbl)
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setMinimum(100)
        self._zoom_slider.setMaximum(300)
        self._zoom_slider.setSingleStep(10)
        self._zoom_slider.setPageStep(10)
        self._zoom_slider.setValue(self.selected_zoom)
        self._zoom_slider.valueChanged.connect(self._on_zoom_changed)
        zoom_row.addWidget(self._zoom_slider, stretch=1)
        self._reset_pos_btn = QPushButton(
            tr('apply_texture_reset_position_btn'))
        self._reset_pos_btn.setObjectName("apply-tex-cancel")
        self._reset_pos_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_pos_btn.clicked.connect(self._on_reset_position)
        zoom_row.addWidget(self._reset_pos_btn)
        outer.addLayout(zoom_row)

        # кнопки внизу
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton(
            tr('apply_texture_cancel_btn') if self._has_textures
            else tr('apply_texture_close_btn'))
        self._cancel_btn.setObjectName("apply-tex-cancel")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)
        if self._has_textures:
            self._apply_btn = QPushButton(tr('apply_texture_apply_btn'))
            self._apply_btn.setObjectName("apply-tex-apply")
            self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._apply_btn.setEnabled(False)
            self._apply_btn.clicked.connect(self._on_apply)
            btn_row.addWidget(self._apply_btn)
        else:
            self._apply_btn = None
        outer.addLayout(btn_row)

        # 2026-05-17 (Этап 2 патч): подгружаем .meta.json для текущего
        # ref'а — собираем словарь {texture_name → meta} и автовыбираем
        # последнюю применённую текстуру + её настройки.
        try:
            self._meta_by_texture = self._load_meta_for_source()
            self._apply_last_meta_on_open()
        except Exception:
            traceback.print_exc()

        # Стартовый preview — чистый ref без текстуры (или с последней
        # текстурой если apply_last_meta_on_open её выбрал).
        self._update_preview()

    def _load_meta_for_source(self) -> Dict[str, Dict]:
        """2026-05-17 (Этап 2 патч): сканирует result_dir на .meta.json
        файлы с совпадающим `source_stem`. Возвращает
        {texture_name → meta_dict}, упорядоченный по mtime убыванию
        (последнее применение первым). Failures проглатываем — meta
        опциональна, при отсутствии диалог открывается с дефолтами.
        """
        try:
            if self.result_dir is None or not self.result_dir.is_dir():
                return {}
            import json as _json
            source_stem = self.ref_path.stem
            entries = []  # (mtime, texture_name, meta_dict)
            for p in self.result_dir.iterdir():
                if not p.is_file():
                    continue
                if not p.name.endswith('.meta.json'):
                    continue
                try:
                    data = _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get('source_stem') != source_stem:
                    continue
                tex_name = data.get('texture_name')
                if not isinstance(tex_name, str):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    mtime = 0.0
                entries.append((mtime, tex_name, data))
            entries.sort(key=lambda e: e[0], reverse=True)
            # Если для одной texture_name есть несколько записей —
            # оставляем САМУЮ свежую (первую после сортировки).
            result: Dict[str, Dict] = {}
            for _mtime, tex_name, data in entries:
                if tex_name not in result:
                    result[tex_name] = data
            return result
        except Exception:
            traceback.print_exc()
            return {}

    def _apply_last_meta_on_open(self):
        """2026-05-17 (Этап 2 патч): при открытии диалога автовыбираем
        текстуру + настройки из самой свежей .meta.json (если есть).
        Иначе оставляем дефолты (нет выбранной текстуры, opacity=30,
        zoom=100, offset=0)."""
        if not self._meta_by_texture:
            return
        # entries в _meta_by_texture упорядочены по убыванию mtime —
        # первый ключ это самая свежая запись.
        try:
            tex_name = next(iter(self._meta_by_texture.keys()))
            meta = self._meta_by_texture[tex_name]
        except StopIteration:
            return
        # Найти соответствующий thumb по basename
        target_thumb = None
        for t in self._thumbs:
            if t.file_path().name == tex_name:
                target_thumb = t
                break
        if target_thumb is None:
            return
        self.selected_texture = target_thumb.file_path()
        for t in self._thumbs:
            t.set_selected(t.file_path() == self.selected_texture)
        # Восстанавливаем настройки из meta (с защитой от пропущенных полей)
        self._apply_meta_settings(meta)
        if self._apply_btn is not None:
            self._apply_btn.setEnabled(True)

    def _apply_meta_settings(self, meta: Dict):
        """Применяет dict с настройками к слайдерам/state.
        Не зовёт _update_preview — caller сделает это сам."""
        try:
            op = int(meta.get('opacity', 30))
            op = max(10, min(100, op))
            self.selected_opacity = op
            try:
                self._slider.setValue(op)
            except Exception:
                pass
            self._opacity_lbl.setText(
                tr('apply_texture_opacity_label', n=op))
            zm = int(meta.get('zoom', 100))
            zm = max(100, min(300, zm))
            self.selected_zoom = zm
            try:
                self._zoom_slider.setValue(zm)
            except Exception:
                pass
            self._zoom_lbl.setText(
                tr('apply_texture_zoom_label', n=zm))
            self.selected_offset_x = int(meta.get('offset_x', 0))
            self.selected_offset_y = int(meta.get('offset_y', 0))
            # Включаем drag если zoom>100
            drag_on = self.selected_zoom > 100
            self._preview_lbl.set_drag_enabled(drag_on)
            if drag_on:
                self._drag_hint_lbl.show()
            else:
                self._drag_hint_lbl.hide()
        except Exception:
            traceback.print_exc()

    def _scan_textures(self) -> List[Path]:
        try:
            if not self.textures_dir.is_dir():
                return []
            exts = {".png", ".jpg", ".jpeg", ".webp"}
            return sorted([
                p for p in self.textures_dir.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            ], key=lambda p: p.name.lower())
        except Exception:
            traceback.print_exc()
            return []

    def _on_texture_clicked(self, file_path):
        try:
            self.selected_texture = Path(file_path)
            # 2026-05-17 (Этап 2 патч): если для этой текстуры есть
            # сохранённая meta (юзер раньше применял эту же текстуру к
            # этому же ref'у) — подставляем прежние opacity/zoom/offset.
            # Иначе сбрасываем offset на дефолт (zoom оставляем — юзер
            # мог настроить нужный масштаб и пробовать разные текстуры).
            meta = self._meta_by_texture.get(self.selected_texture.name)
            if isinstance(meta, dict):
                self._apply_meta_settings(meta)
            else:
                self.selected_offset_x = 0
                self.selected_offset_y = 0
            for t in self._thumbs:
                t.set_selected(t.file_path() == self.selected_texture)
            if self._apply_btn is not None:
                self._apply_btn.setEnabled(True)
            self._preview_timer.start()
        except Exception:
            traceback.print_exc()

    def _on_opacity_changed(self, value: int):
        try:
            self.selected_opacity = int(value)
            self._opacity_lbl.setText(
                tr('apply_texture_opacity_label', n=self.selected_opacity))
            self._preview_timer.start()
        except Exception:
            traceback.print_exc()

    def _on_zoom_changed(self, value: int):
        """2026-05-17 (Этап 2 доп): zoom-слайдер 100..300%."""
        try:
            self.selected_zoom = int(value)
            self._zoom_lbl.setText(
                tr('apply_texture_zoom_label', n=self.selected_zoom))
            # Включаем/выключаем drag по preview + подсказку
            drag_on = self.selected_zoom > 100
            self._preview_lbl.set_drag_enabled(drag_on)
            if drag_on:
                self._drag_hint_lbl.show()
            else:
                self._drag_hint_lbl.hide()
                # zoom=100 — offset уже не имеет смысла, обнуляем
                self.selected_offset_x = 0
                self.selected_offset_y = 0
            # Clamp offset под новый zoom (если уменьшили — может вылезти)
            self._clamp_offset()
            self._preview_timer.start()
        except Exception:
            traceback.print_exc()

    def _on_drag_moved(self, dx_preview: int, dy_preview: int):
        """2026-05-17 (Этап 2 доп): движение мыши с зажатой ЛКМ по preview.
        Переводим delta из preview-pixels в full-size base-pixels и
        копим в selected_offset_x/y с clamp.

        2026-05-17 (Этап 2 патч): инверсия знака — Photoshop hand-tool
        семантика. Тянешь мышь вправо → картинка едет вправо (на самом
        деле двигаем crop-окно ВЛЕВО внутри текстуры → видна более
        левая часть → визуально текстура съезжает вправо).
        """
        try:
            if self.selected_zoom <= 100:
                return
            if self._preview_ratio <= 0:
                return
            dx_full = int(round(dx_preview / self._preview_ratio))
            dy_full = int(round(dy_preview / self._preview_ratio))
            self.selected_offset_x -= dx_full
            self.selected_offset_y -= dy_full
            self._clamp_offset()
            self._preview_timer.start()
        except Exception:
            traceback.print_exc()

    def _on_reset_position(self):
        """2026-05-17 (Этап 2 доп): сброс zoom=100 + offset=(0,0)."""
        try:
            self.selected_zoom = 100
            self.selected_offset_x = 0
            self.selected_offset_y = 0
            try:
                self._zoom_slider.setValue(100)
            except Exception:
                pass
            self._zoom_lbl.setText(
                tr('apply_texture_zoom_label', n=self.selected_zoom))
            self._preview_lbl.set_drag_enabled(False)
            self._drag_hint_lbl.hide()
            self._preview_timer.start()
        except Exception:
            traceback.print_exc()

    def _clamp_offset(self):
        """Ограничивает offset так чтобы crop не вылез за границы
        zoomed-tex'а. Max |off_x| = (tex_w - base_w) / 2 в координатах
        full-size base."""
        try:
            bs = self._cached_base_size
            if not bs:
                return
            bw, bh = bs
            zoom = self.selected_zoom / 100.0
            tex_w = max(bw, int(round(bw * zoom)))
            tex_h = max(bh, int(round(bh * zoom)))
            max_off_x = max(0, (tex_w - bw) // 2)
            max_off_y = max(0, (tex_h - bh) // 2)
            self.selected_offset_x = max(
                -max_off_x, min(max_off_x, self.selected_offset_x))
            self.selected_offset_y = max(
                -max_off_y, min(max_off_y, self.selected_offset_y))
        except Exception:
            traceback.print_exc()

    def _update_preview(self):
        """PIL.blend на downscaled (≤PREVIEW_MAX) base+texture, потом в
        QLabel.setPixmap. Без выбранной текстуры — чистый ref."""
        try:
            from PIL import Image as _PILImage
            from PIL.ImageQt import ImageQt
        except Exception:
            # Fallback: PIL.ImageQt отсутствует — показываем чистый ref
            # без блендинга (preview без эффекта).
            try:
                pix = QPixmap(str(self.ref_path))
                if not pix.isNull():
                    pix = pix.scaled(
                        QSize(self.PREVIEW_MAX, self.PREVIEW_MAX),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    self._preview_lbl.setPixmap(pix)
            except Exception:
                traceback.print_exc()
            return
        try:
            base = _PILImage.open(self.ref_path).convert("RGB")
            # Кеш для drag-конвертации preview↔full и clamp.
            self._cached_base_size = base.size
            ratio = self.PREVIEW_MAX / float(max(base.size))
            if ratio < 1.0:
                new_w = max(1, int(base.size[0] * ratio))
                new_h = max(1, int(base.size[1] * ratio))
                base_small = base.resize(
                    (new_w, new_h), _PILImage.Resampling.LANCZOS)
                self._preview_ratio = ratio
            else:
                base_small = base
                self._preview_ratio = 1.0
            if self.selected_texture is not None:
                tex = _PILImage.open(
                    self.selected_texture).convert("RGB")
                # 2026-05-17 (Этап 2 доп): zoom + offset на preview.
                # Применяем тот же алгоритм что в ApplyTextureThread.run,
                # но на downscaled base_small.
                zoom = max(100, min(300, self.selected_zoom)) / 100.0
                bw, bh = base_small.size
                tex_w = max(bw, int(round(bw * zoom)))
                tex_h = max(bh, int(round(bh * zoom)))
                tex = tex.resize(
                    (tex_w, tex_h), _PILImage.Resampling.LANCZOS)
                # Переводим full-size offset в preview-space:
                off_x_prev = int(round(
                    self.selected_offset_x * self._preview_ratio))
                off_y_prev = int(round(
                    self.selected_offset_y * self._preview_ratio))
                # clamp preview-side
                max_off_x = max(0, (tex_w - bw) // 2)
                max_off_y = max(0, (tex_h - bh) // 2)
                off_x_prev = max(-max_off_x, min(max_off_x, off_x_prev))
                off_y_prev = max(-max_off_y, min(max_off_y, off_y_prev))
                left = (tex_w - bw) // 2 + off_x_prev
                top = (tex_h - bh) // 2 + off_y_prev
                cropped = tex.crop((left, top, left + bw, top + bh))
                alpha = self.selected_opacity / 100.0
                result = _PILImage.blend(base_small, cropped, alpha)
            else:
                result = base_small
            # 2026-05-17 (Этап 2 патч): PIL → QPixmap через PNG-буфер.
            # Раньше: ImageQt(result) + QPixmap.fromImage. На PyQt6
            # это давало артефакты (первые N строк норм, остальное
            # растягивается / серая текстура). Стабильный путь —
            # сохранить PIL.Image в PNG через BytesIO и загрузить
            # QPixmap из этих байтов.
            from io import BytesIO as _BytesIO
            buf = _BytesIO()
            result.save(buf, format="PNG")
            buf.seek(0)
            pix = QPixmap()
            pix.loadFromData(buf.getvalue(), "PNG")
            self._preview_lbl.setPixmap(pix)
        except Exception:
            traceback.print_exc()

    def _on_apply(self):
        if self.selected_texture is None:
            return
        self.accept()
