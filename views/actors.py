# -*- coding: utf-8 -*-
"""
views/actors.py — вкладка «Актёры» Storyboard Studio.

Содержит:
    - ActorCard — карточка одного актёра (превью + имя + кнопки + прогресс)
    - ActorsView — вся вкладка (drop-зона + слайдер + сетка карточек)

Зависимости от storyboard_app.py (через `_AppProxy` lazy proxy):
    - list_actors, actor_display_name, get_actor_photos,
      get_actor_generated_refs_paths
    - create_actor, rename_actor, add_photo_to_actor
    - get_icon, block_wheel_event
    - APP_ORG, APP_NAME (для QSettings)

Зависимости от threads/widgets (прямой импорт):
    - GenerateActorRefThread (threads.generate)
    - AddActorDialog, ChooseActorDialog, ActorPhotosDialog (widgets)
    - CreateActorRefDialog, RefResultDialog (widgets)

История: вытащено из storyboard_app.py 2026-05-04 (шаг 4C рефакторинга).
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QSize, QTimer, QSettings, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QProgressBar, QSlider,
    QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QDialog,
    QMessageBox,
)

from i18n import tr
from threads import (
    GenerateActorRefThread, EditActorRefThread, ApplyTextureThread,
)
from widgets import (
    AddActorDialog, ChooseActorDialog, ActorPhotosDialog,
    CreateActorRefDialog, RefResultDialog, CustomCharacterDialog,
)
# 2026-05-17 (Этап 2): ApplyTextureDialog не реэкспортирован через
# widgets/__init__.py — импортируем напрямую чтобы не задеть лишний файл.
from widgets.actor_dialogs import ApplyTextureDialog


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


# ─── Карточка одного актёра ──────────────────────────────────────

class ActorCard(QFrame):
    """Карточка одного актёра: превью первого фото + имя + кол-во фото
    + кнопка ✎ Переименовать (только для админа).

    Клик по самой карточке (но НЕ по кнопке Переименовать) → emit
    `clicked(slug)`. Слушающий открывает попап с галереей всех фото."""

    rename_requested = pyqtSignal(str)       # slug
    delete_requested = pyqtSignal(str)        # slug — admin-only кнопка 🗑
    clicked = pyqtSignal(str)                 # slug — клик по карточке (для галереи)
    create_ref_requested = pyqtSignal(str)    # slug — кнопка «Создать референс»
    view_refs_requested = pyqtSignal(str)     # slug — кнопка «Все референсы»
    pending_clicked = pyqtSignal(str)         # slug — кнопка «Готов новый референс»
    error_dismissed = pyqtSignal(str)         # slug — клик «✕ Скрыть» на error overlay

    def __init__(self, slug: str, display_name: str, photos: List[Path],
                 is_admin: bool, card_width: int = 220,
                 generated_refs_count: int = 0,
                 pending_count: int = 0, parent=None):
        super().__init__(parent)
        self.slug = slug
        self.setObjectName("ref-card")
        self.setFixedWidth(card_width)
        self._card_width = card_width
        # Курсор-палец на всей карточке — намёк что кликабельно.
        # На кнопке Переименовать он переопределится автоматически Qt.
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Контейнер для превью с overlay (плашка прогресса поверх).
        self.img_container = QWidget()
        self.img_container.setFixedSize(card_width, card_width)
        self.img_container.setStyleSheet(
            "background:#1a1424; border-top-left-radius:11px;"
            " border-top-right-radius:11px;")

        # Превью — первое фото актёра. Высота = ширине (квадрат), фото
        # вписывается ЦЕЛИКОМ (KeepAspectRatio, без обрезания) — по краям
        # тёмный фон. KeepAspectRatioByExpanding обрезал бы лицо.
        self.img_lbl = QLabel(self.img_container)
        self.img_lbl.setGeometry(0, 0, card_width, card_width)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet(
            "background:#1a1424; border-top-left-radius:11px;"
            " border-top-right-radius:11px; color:#555; font-size:13px;")
        if photos:
            try:
                pix = QPixmap(str(photos[0]))
                if not pix.isNull():
                    pix = pix.scaled(
                        QSize(card_width, card_width),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    self.img_lbl.setPixmap(pix)
            except Exception:
                pass
        else:
            self.img_lbl.setText("—")

        # Overlay прогресса генерации — плашка поверх превью с текстом
        # «Генерирую…» + полоса + секунды. По умолчанию скрыта.
        self.progress_overlay = QWidget(self.img_container)
        self.progress_overlay.setGeometry(0, 0, card_width, card_width)
        self.progress_overlay.setStyleSheet(
            "background: rgba(20, 14, 30, 0.78);"
            " border-top-left-radius:11px; border-top-right-radius:11px;")
        po = QVBoxLayout(self.progress_overlay)
        po.setContentsMargins(14, 14, 14, 14)
        po.addStretch()
        self.progress_label = QLabel(tr('actor_progress_starting'))
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_label.setWordWrap(True)
        self.progress_label.setStyleSheet(
            "color:#fff; font-size:13px; font-weight:600;"
            " background: transparent;")
        po.addWidget(self.progress_label)
        po.addSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate animation
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background:#2a1f3d; border:none; border-radius:3px; }"
            "QProgressBar::chunk { background:#8e6cd4; border-radius:3px; }")
        po.addWidget(self.progress_bar)
        po.addSpacing(6)
        self.progress_seconds_lbl = QLabel("0с")
        self.progress_seconds_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_seconds_lbl.setStyleSheet(
            "color:#ffd24d; font-size:12px; font-weight:600;"
            " background: transparent;")
        po.addWidget(self.progress_seconds_lbl)
        po.addStretch()
        self.progress_overlay.hide()
        self._gen_started_at: Optional[float] = None
        self._gen_timer: Optional[QTimer] = None

        # 2026-05-05: красная плашка ошибки генерации. Поверх превью,
        # над progress-overlay'ом. Видна пока юзер не кликнул «✕».
        self.error_overlay = QWidget(self.img_container)
        self.error_overlay.setGeometry(0, 0, card_width, card_width)
        self.error_overlay.setStyleSheet(
            "background: rgba(80, 18, 18, 0.92);"
            " border-top-left-radius:11px; border-top-right-radius:11px;")
        eo = QVBoxLayout(self.error_overlay)
        eo.setContentsMargins(12, 12, 12, 12)
        eo.setSpacing(6)
        eo.addStretch()
        self.error_title_lbl = QLabel("✗ Ошибка генерации")
        self.error_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_title_lbl.setWordWrap(True)
        self.error_title_lbl.setStyleSheet(
            "color:#ffd0d0; font-size:13px; font-weight:700;"
            " background:transparent;")
        eo.addWidget(self.error_title_lbl)
        self.error_msg_lbl = QLabel("")
        self.error_msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_msg_lbl.setWordWrap(True)
        self.error_msg_lbl.setStyleSheet(
            "color:#ffe6e6; font-size:11px; background:transparent;")
        eo.addWidget(self.error_msg_lbl)
        eo.addStretch()
        self.error_dismiss_btn = QPushButton("✕  Скрыть")
        self.error_dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.error_dismiss_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.10);"
            " color:#fff; border:1px solid rgba(255,255,255,0.30);"
            " border-radius:6px; padding:5px 10px; font-size:11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.18); }")
        self.error_dismiss_btn.clicked.connect(self._on_error_dismiss)
        eo.addWidget(self.error_dismiss_btn)
        self.error_overlay.hide()

        v.addWidget(self.img_container)

        # Подвал — имя + кол-во фото
        info = QWidget()
        info.setObjectName("ref-card-info")
        il = QVBoxLayout(info)
        il.setContentsMargins(14, 10, 14, 12)
        il.setSpacing(2)
        name_lbl = QLabel(display_name)
        name_lbl.setObjectName("ref-name")
        il.addWidget(name_lbl)
        cnt_lbl = QLabel(tr('actor_card_photos', n=len(photos)))
        cnt_lbl.setObjectName("ref-tag")
        il.addWidget(cnt_lbl)

        # Кнопка «Готов новый референс (N)» — иконка-колокольчик ТЁМНАЯ.
        # ВАЖНО: пульсация теперь делается централизованно в ActorsView
        # (один таймер на всю сетку, останавливается когда вкладка скрыта)
        # — иначе локальные таймеры на каждой карточке провоцируют каскад
        # repaint'ов и юзер видит мигание картинок на других вкладках.
        self.pending_btn = None
        self._pending_pulse_on = False
        if pending_count > 0:
            il.addSpacing(6)
            self.pending_btn = QPushButton(
                tr('actor_card_pending_ready', n=pending_count))
            self.pending_btn.setIcon(_sa.get_icon('bell-dark'))
            self.pending_btn.setIconSize(QSize(14, 14))
            self._apply_pending_btn_style(False)
            self.pending_btn.clicked.connect(
                lambda: self.pending_clicked.emit(self.slug))
            il.addWidget(self.pending_btn)

        # Кнопка «Создать референс» — иконка волшебной палочки. Видна ВСЕМ.
        il.addSpacing(6)
        create_ref_btn = QPushButton(tr('actor_card_create_ref'))
        create_ref_btn.setIcon(_sa.get_icon('wand-2'))
        create_ref_btn.setIconSize(QSize(14, 14))
        create_ref_btn.setStyleSheet(
            "QPushButton { background:#3a2c52; color:#fff; border:none;"
            " border-radius:5px; padding:6px 8px; font-size:11px;"
            " font-weight:600; text-align:left; }"
            "QPushButton:hover { background:#4d3a6b; }")
        create_ref_btn.clicked.connect(
            lambda: self.create_ref_requested.emit(self.slug))
        il.addWidget(create_ref_btn)

        # Кнопка «Все референсы (N)» — иконка стопки картинок. Видна когда >0.
        if generated_refs_count > 0:
            il.addSpacing(4)
            view_refs_btn = QPushButton(
                tr('actor_card_view_refs', n=generated_refs_count))
            view_refs_btn.setIcon(_sa.get_icon('images'))
            view_refs_btn.setIconSize(QSize(14, 14))
            view_refs_btn.setStyleSheet(
                "QPushButton { background:transparent; border:1px solid #6e4cc4;"
                " border-radius:5px; padding:5px 8px; color:#d8c8ff;"
                " font-size:11px; font-weight:600; text-align:left; }"
                "QPushButton:hover { background:#2a1f3d; }")
            view_refs_btn.clicked.connect(
                lambda: self.view_refs_requested.emit(self.slug))
            il.addWidget(view_refs_btn)

        # Кнопка «Переименовать» — иконка карандаша. Только админ.
        if is_admin:
            il.addSpacing(4)
            rename_btn = QPushButton(tr('actor_card_rename'))
            rename_btn.setIcon(_sa.get_icon('pencil'))
            rename_btn.setIconSize(QSize(14, 14))
            rename_btn.setStyleSheet(
                "QPushButton { background:transparent; border:1px solid #3a2c52;"
                " border-radius:4px; padding:4px 8px; color:#aaa;"
                " font-size:11px; text-align:left; }"
                "QPushButton:hover { color:#fff; border-color:#5a4a82; }")
            rename_btn.clicked.connect(lambda: self.rename_requested.emit(self.slug))
            il.addWidget(rename_btn)

            # Кнопка «🗑 Удалить» — destructive, только админ. Подтверждение
            # делается в ActorsView._on_delete_actor.
            il.addSpacing(2)
            del_btn = QPushButton(tr('actor_card_delete'))
            del_btn.setIcon(_sa.get_icon('trash-2'))
            del_btn.setIconSize(QSize(14, 14))
            del_btn.setStyleSheet(
                "QPushButton { background:transparent; border:1px solid #5a2c2c;"
                " border-radius:4px; padding:4px 8px; color:#c47878;"
                " font-size:11px; text-align:left; }"
                "QPushButton:hover { color:#fff; border-color:#c4304c;"
                " background:rgba(196,48,76,0.1); }")
            del_btn.clicked.connect(lambda: self.delete_requested.emit(self.slug))
            il.addWidget(del_btn)

        v.addWidget(info)

    def mousePressEvent(self, ev):
        """Клик по карточке (не по кнопке Переименовать — Qt туда не доходит,
        QPushButton accept'ит event первым) → emit clicked(slug). Слушающий
        ActorsView откроет ActorPhotosDialog с галереей всех фото."""
        try:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self.slug)
        except Exception:
            pass
        super().mousePressEvent(ev)

    # ── Прогресс генерации референса (на самой карточке) ────────────────

    def start_progress(self, label: Optional[str] = None):
        """Показывает overlay прогресса поверх превью. Запускает таймер
        который каждую секунду обновляет «12с» снизу. Полоса в
        indeterminate-режиме (бегущая анимация).

        Вызывается из ActorsView когда стартует GenerateActorRefThread."""
        try:
            self.progress_label.setText(label or tr('actor_progress_starting'))
            self.progress_seconds_lbl.setText("0с")
            self.progress_overlay.show()
            self.progress_overlay.raise_()
            self._gen_started_at = time.time()
            if self._gen_timer is None:
                self._gen_timer = QTimer(self)
                self._gen_timer.setInterval(1000)
                self._gen_timer.timeout.connect(self._tick_progress)
            self._gen_timer.start()
        except Exception:
            traceback.print_exc()

    def update_progress(self, label: str):
        """Обновляет текст в overlay (например «Загружаю фото 2/3»,
        «Генерирую…»). Секунды обновляются сами по таймеру."""
        try:
            if self.progress_overlay.isVisible():
                self.progress_label.setText(label)
        except Exception:
            pass

    def stop_progress(self):
        """Скрывает overlay прогресса. Вызывается на finished/error потока."""
        try:
            if self._gen_timer is not None:
                self._gen_timer.stop()
            self.progress_overlay.hide()
            self._gen_started_at = None
        except Exception:
            pass

    def _tick_progress(self):
        """Раз в секунду — обновляем счётчик секунд в overlay."""
        if self._gen_started_at is None:
            return
        elapsed = max(0, int(time.time() - self._gen_started_at))
        try:
            self.progress_seconds_lbl.setText(f"{elapsed}с")
        except Exception:
            pass

    # ── 2026-05-05: error overlay (видимая ошибка генерации) ────────────

    def set_error(self, msg: str):
        """Показывает красную плашку поверх превью с текстом ошибки.
        Висит до клика «✕ Скрыть» (юзер должен явно подтвердить что
        прочитал)."""
        try:
            self.error_msg_lbl.setText(
                tr('actor_error_msg', msg=(msg or "")[:200]))
            self.error_title_lbl.setText(tr('actor_error_title'))
            self.error_dismiss_btn.setText(tr('actor_error_dismiss'))
            self.error_overlay.show()
            self.error_overlay.raise_()
        except Exception:
            traceback.print_exc()

    def clear_error(self):
        """Скрывает плашку ошибки. Вызывается при retry или dismiss."""
        try:
            self.error_overlay.hide()
        except Exception:
            pass

    def _on_error_dismiss(self):
        """Клик «✕ Скрыть» — эмитим сигнал чтобы caller (ActorsView)
        очистил `_actor_errors[slug]` и не показывал overlay при
        следующем refresh."""
        self.clear_error()
        self.error_dismissed.emit(self.slug)

    def _apply_pending_btn_style(self, pulse_on: bool):
        """Стилизует жёлтую pending-кнопку. Два состояния:
        - OFF (`pulse_on=False`): #ffd24d (стандартный жёлтый)
        - ON  (`pulse_on=True`):  #fff3a0 (светло-жёлтый — пик пульсации).
        Вызывается централизованно из ActorsView._tick_pending_pulse."""
        if self.pending_btn is None:
            return
        bg = "#fff3a0" if pulse_on else "#ffd24d"
        self.pending_btn.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:#15101e;"
            f" border:none; border-radius:5px; padding:7px 8px;"
            f" font-size:12px; font-weight:700; text-align:left; }}"
            "QPushButton:hover { background:#ffe27a; }")


# ─── Вкладка «Актёры» ────────────────────────────────────────────

class ActorsView(QWidget):
    """Главная вкладка «Актёры» — drop-зона + кнопка создания + сетка карточек.
    Видна только админу (контролируется в MainWindow._build_ui)."""

    # Диапазон слайдера ширины карточек. Дефолт 220 — как было до настройки.
    CARD_WIDTH_MIN = 140
    CARD_WIDTH_MAX = 320
    CARD_WIDTH_DEFAULT = 220
    CARD_WIDTH_STEP = 20

    def __init__(self, project_root: Path, status_bar=None,
                 is_admin: bool = False, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.status_bar = status_bar
        # Админ может: добавлять/переименовывать актёров, видеть слайдер размера.
        # Коллеги видят только грид + кнопку «Создать референс» на карточке.
        self._is_admin = bool(is_admin)
        # Принимаем drop только в админ-режиме (drag из Finder в зону)
        self.setAcceptDrops(self._is_admin)
        # slug → list путей к файлам. Сгенерированные но НЕ подтверждённые
        # рефы. Появляются после finished, очищаются когда юзер выбрал один
        # вариант через RefResultDialog «Оставить этот».
        self._pending_variants: Dict[str, List[str]] = {}
        # slug → открытый RefResultDialog (если уже открыт). Чтобы при
        # завершении следующей генерации добавить вариант в существующий
        # попап, а не открывать новый. Закрытый попап удаляется отсюда.
        self._open_result_dialogs: Dict[str, RefResultDialog] = {}
        # slug → последнее использованное описание (из CreateActorRefDialog
        # или RefResultDialog._on_regen). Подставляется в попап при клике
        # «🆕 Готов новый референс» — юзер не должен заново писать что было.
        self._last_outfit: Dict[str, str] = {}
        # 2026-06-05: выбранная раскадровка (detailed/simple) последней
        # генерации per-slug — чтобы «Создать ещё один референс» повторял
        # ту же раскладку (14/7 панелей), а не дефолтил в detailed.
        self._last_variant: Dict[str, str] = {}
        # Долг 13: pending-запрос из чата эпизода. Когда юзер выбрал
        # вариант одежды для character'а в чате — мы переключили вкладку
        # сюда и записали сюда {character, show, description}. При клике
        # «Создать референс» на любой карточке актёра — поля префиллятся
        # из этого dict'а, после открытия попапа dict очищается.
        self._pending_create_request: Optional[Dict[str, str]] = None
        # 2026-05-05: ошибки генерации актёрских рефов. slug → последняя
        # ошибка. Очищается при `clear_error` / новой `start_progress`.
        self._actor_errors: Dict[str, str] = {}
        # 2026-05-05: словарь связок character_slug → ep_id для авто-
        # привязки сгенерированного рефа к эпизоду. Живёт дольше
        # `_pending_create_request` — до тех пор, пока генерация рефа
        # не завершилась (или юзер не отменил баннер).
        # 2026-05-05 fix: ИЗНАЧАЛЬНО был один-слот `_pending_ep_link`,
        # но юзер может запустить параллельные генерации (zhena → пошёл
        # в чат → lyubovnik) — второй слот затирал первый. Теперь
        # каждый character_slug — отдельный ключ, генерации не конфликтуют.
        self._pending_ep_links: Dict[str, str] = {}
        # Централизованный таймер пульсации pending-кнопок: один на всю
        # сетку, тогглит state каждые 600мс. ОСТАНАВЛИВАЕТСЯ когда
        # ActorsView не видна (showEvent/hideEvent) — иначе setStyleSheet
        # на скрытых карточках провоцирует Qt re-style cycle и юзер видит
        # мигание изображений на других вкладках.
        self._pending_pulse_state = False
        self._pending_pulse_timer = QTimer(self)
        self._pending_pulse_timer.setInterval(600)
        self._pending_pulse_timer.timeout.connect(self._tick_pending_pulse)
        # Ширина карточки актёра (px). Сохраняется в QSettings → переживает
        # перезапуск. Высота превью = ширине (квадрат).
        try:
            v = QSettings(_sa.APP_ORG, _sa.APP_NAME).value(
                "actors_card_width", self.CARD_WIDTH_DEFAULT)
            self._card_width = int(v)
        except Exception:
            self._card_width = self.CARD_WIDTH_DEFAULT
        self._card_width = max(self.CARD_WIDTH_MIN,
                               min(self.CARD_WIDTH_MAX, self._card_width))
        self._build()
        self.refresh()

    def _build(self):
        outer_lay = QVBoxLayout(self)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        # Прокрутка — на случай большого числа актёров
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        outer_lay.addWidget(scroll)

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(inner)
        lay.setSpacing(20)
        lay.setContentsMargins(28, 26, 28, 26)

        # ── Долг 13: баннер «выбери актёра для роли X» ──────────────────
        # 2026-05-05: ярко-жёлтый фон + стрелка ↓ чтобы юзер сразу видел
        # что ему нужно сделать (выбрать актёра в сетке снизу).
        self.pending_banner = QFrame()
        self.pending_banner.setObjectName("pending-banner")
        self.pending_banner.setStyleSheet(
            "QFrame#pending-banner {"
            " background: #ffd24d;"
            " border: 2px solid #f0a000;"
            " border-radius: 10px; }"
            "QLabel#pending-banner-text {"
            " color:#1a1424; font-size:14px; font-weight:700;"
            " background:transparent; }"
            "QLabel#pending-banner-arrow {"
            " color:#1a1424; font-size:22px; font-weight:900;"
            " background:transparent; }"
            "QPushButton#pending-banner-cancel {"
            " background:transparent; color:#1a1424;"
            " border:1px solid #1a1424;"
            " border-radius:6px; padding:6px 14px; font-size:12px;"
            " font-weight:600; }"
            "QPushButton#pending-banner-cancel:hover {"
            " background:rgba(0,0,0,0.10); color:#000; }")
        pb = QHBoxLayout(self.pending_banner)
        pb.setContentsMargins(18, 14, 18, 14)
        pb.setSpacing(12)
        self.pending_banner_arrow = QLabel("↓")
        self.pending_banner_arrow.setObjectName("pending-banner-arrow")
        self.pending_banner_arrow.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        pb.addWidget(self.pending_banner_arrow)
        self.pending_banner_lbl = QLabel("")
        self.pending_banner_lbl.setObjectName("pending-banner-text")
        self.pending_banner_lbl.setWordWrap(True)
        pb.addWidget(self.pending_banner_lbl, stretch=1)
        self.pending_banner_cancel = QPushButton(tr('actors_pending_cancel'))
        self.pending_banner_cancel.setObjectName("pending-banner-cancel")
        self.pending_banner_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pending_banner_cancel.clicked.connect(
            self._on_pending_banner_cancel)
        pb.addWidget(self.pending_banner_cancel)
        self.pending_banner.hide()
        lay.addWidget(self.pending_banner)

        # ── Админская секция: drop-зона + кнопка «Создать актёра» ────────
        # Скрыта для не-админов: они видят только грид актёров +
        # кнопку «Создать референс» на каждой карточке.
        if self._is_admin:
            self.sec_admin_lbl = QLabel(tr('actors_section_admin'))
            self.sec_admin_lbl.setObjectName("settings-section")
            lay.addWidget(self.sec_admin_lbl)

            # Drop-зона
            self.drop_frame = QFrame()
            self.drop_frame.setObjectName("actors-drop")
            self.drop_frame.setMinimumHeight(120)
            self.drop_frame.setStyleSheet(
                "QFrame#actors-drop {"
                " background: rgba(110,76,196,0.10);"
                " border: 2px dashed rgba(160,120,240,0.45);"
                " border-radius: 12px;"
                "}")
            df_lay = QVBoxLayout(self.drop_frame)
            df_lay.setContentsMargins(20, 18, 20, 18)
            df_lay.setSpacing(6)
            df_lay.addStretch()
            self.drop_lbl = QLabel(tr('actors_drop_label'))
            self.drop_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.drop_lbl.setStyleSheet(
                "color:#d8c8ff; font-size:14px; font-weight:600; background:transparent;")
            df_lay.addWidget(self.drop_lbl)
            self.drop_hint = QLabel(tr('actors_drop_hint'))
            self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.drop_hint.setStyleSheet(
                "color:#aaa; font-size:12px; background:transparent;")
            df_lay.addWidget(self.drop_hint)
            df_lay.addStretch()
            lay.addWidget(self.drop_frame)

            # Кнопка «Создать актёра»
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            self.add_btn = QPushButton(tr('actors_add_btn'))
            self.add_btn.setObjectName("save")
            self.add_btn.setFixedHeight(36)
            self.add_btn.setMinimumWidth(180)
            self.add_btn.clicked.connect(self._on_add_actor)
            btn_row.addWidget(self.add_btn)
            lay.addLayout(btn_row)
        else:
            # Не-админ: служебные ссылки на None чтобы apply_lang не падал
            self.sec_admin_lbl = None
            self.drop_frame = None
            self.drop_lbl = None
            self.drop_hint = None
            self.add_btn = None

        # ── Слайдер ширины карточек (только админ) ───────────────────────
        # Регулирует размер карточек актёров в сетке. Высота = ширине
        # (квадратное превью). Значение сохраняется в QSettings.
        if self._is_admin:
            size_frame = QFrame()
            size_frame.setObjectName("settings-group")
            sf = QVBoxLayout(size_frame)
            sf.setSpacing(0)
            sf.setContentsMargins(18, 14, 18, 14)

            self.size_hint_lbl = QLabel(tr('actors_card_size_hint'))
            self.size_hint_lbl.setWordWrap(True)
            self.size_hint_lbl.setStyleSheet(
                "color:#aaa; font-size:12px; padding-bottom:10px;")
            sf.addWidget(self.size_hint_lbl)

            size_row = QHBoxLayout()
            size_row.setSpacing(12)
            self.size_label_lbl = QLabel(tr('actors_card_size_label'))
            self.size_label_lbl.setStyleSheet("color:#cfcfcf; font-size:13px;")
            size_row.addWidget(self.size_label_lbl)

            self.card_size_slider = QSlider(Qt.Orientation.Horizontal)
            self.card_size_slider.setMinimum(self.CARD_WIDTH_MIN)
            self.card_size_slider.setMaximum(self.CARD_WIDTH_MAX)
            self.card_size_slider.setSingleStep(self.CARD_WIDTH_STEP)
            self.card_size_slider.setPageStep(self.CARD_WIDTH_STEP)
            self.card_size_slider.setValue(self._card_width)
            self.card_size_slider.valueChanged.connect(self._on_card_size_changed)
            # КРИТИЧНО: блокируем колесо мыши — иначе прокрутка страницы Актёров
            # курсором над слайдером МЕНЯЕТ ширину. Правило: настройки только
            # клик/drag, никогда не колесо.
            _sa.block_wheel_event(self.card_size_slider)
            size_row.addWidget(self.card_size_slider, stretch=1)

            self.size_value_lbl = QLabel(
                tr('actors_card_size_value', w=self._card_width))
            self.size_value_lbl.setStyleSheet(
                "color:#ffd24d; font-size:13px; font-weight:600; min-width:80px;")
            size_row.addWidget(self.size_value_lbl)
            sf.addLayout(size_row)

            lay.addWidget(size_frame)
        else:
            self.size_hint_lbl = None
            self.size_label_lbl = None
            self.card_size_slider = None
            self.size_value_lbl = None

        # Секция «АКТЁРЫ» — сетка карточек
        self.sec_list_lbl = QLabel(tr('actors_section_list'))
        self.sec_list_lbl.setObjectName("settings-section")
        lay.addSpacing(8)
        lay.addWidget(self.sec_list_lbl)

        self.empty_lbl = QLabel(tr('actors_empty'))
        self.empty_lbl.setStyleSheet(
            "color:#888; font-size:13px; font-style:italic; padding:14px;")
        self.empty_lbl.hide()
        lay.addWidget(self.empty_lbl)

        # Сетка карточек (3 колонки)
        self.grid_widget = QWidget()
        self.grid_widget.setStyleSheet("background: transparent;")
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setSpacing(14)
        self.grid.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.grid_widget)

        # 2026-05-17 (Этап 1): хранилище текстур — доступно ВСЕМ (не
        # admin-only). Папка actors/_textures/ глобальная, шарится между
        # сериалами. Этап 1 — только загрузка/хранение. Наложение —
        # Этап 2 (отдельно). Класс определён в конце файла.
        self.textures_zone = TexturesDropZone(self.project_root, parent=self)
        lay.addWidget(self.textures_zone)

        lay.addStretch()
        scroll.setWidget(inner)

    def refresh(self):
        """Перечитывает actors/ + actors.json и перерисовывает карточки."""
        # Чистим старые карточки
        while self.grid.count():
            item = self.grid.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

        slugs = _sa.list_actors(self.project_root)
        # 2026-06-24 (монстры 3а): нестандартные персонажи из ЛОКАЛЬНОГО стора
        # actors/_custom/ — читаем ОТДЕЛЬНО (list_actors исключает '_'-папки).
        monsters = self._list_custom_characters()
        # Грид показываем ВСЕГДА — внизу карточка-плюс «добавить нестандартного
        # персонажа». empty_lbl — подсказка только когда нет НИ актёров, НИ монстров.
        self.empty_lbl.setVisible(not slugs and not monsters)
        self.grid_widget.show()

        cols = self._calc_cols()
        self._last_cols = cols
        # Карточки по slug — для обновления прогресс-overlay из ActorsView
        # без полного refresh()
        self._cards_by_slug: Dict[str, ActorCard] = {}
        for i, slug in enumerate(slugs):
            display = _sa.actor_display_name(self.project_root, slug)
            photos = _sa.get_actor_photos(self.project_root, slug)
            refs_count = len(_sa.get_actor_generated_refs_paths(slug))
            pending_count = len(self._pending_variants.get(slug, []))
            card = ActorCard(slug, display, photos, is_admin=self._is_admin,
                             card_width=self._card_width,
                             generated_refs_count=refs_count,
                             pending_count=pending_count)
            card.rename_requested.connect(self._on_rename_actor)
            card.delete_requested.connect(self._on_delete_actor)
            card.clicked.connect(self._on_actor_card_clicked)
            card.create_ref_requested.connect(self._on_create_ref_requested)
            card.view_refs_requested.connect(self._on_view_refs_requested)
            card.pending_clicked.connect(self._on_pending_clicked)
            card.error_dismissed.connect(self._on_actor_error_dismissed)
            self._cards_by_slug[slug] = card
            # Если для этого актёра идёт активная генерация — сразу показать
            # прогресс на свежесозданной карточке (refresh мог быть вызван
            # из-за смены размера слайдера во время генерации)
            active_gen = getattr(self, '_active_generations', {}).get(slug)
            if active_gen is not None:
                card.start_progress(active_gen.get('label'))
                card._gen_started_at = active_gen.get('started_at')
            # 2026-05-05: восстанавливаем error overlay если ошибка была
            # ранее и юзер ещё её не закрыл (например refresh случился
            # из-за смены ширины карточек).
            err_msg = self._actor_errors.get(slug)
            if err_msg:
                card.set_error(err_msg)
            r, c = divmod(i, cols)
            self.grid.addWidget(card, r, c)
        # 2026-06-24 (монстры 3а): карточки нестандартных персонажей — ПОСЛЕ
        # обычных актёров, индексы продолжаются с len(slugs). На 3а monsters
        # обычно пуст (стора ещё нет) — это каркас под 3б/3в.
        next_idx = len(slugs)
        for m in monsters:
            mslug = m['slug']
            mcard = ActorCard(
                mslug, m['display_name'], m['photos'],
                is_admin=self._is_admin, card_width=self._card_width,
                generated_refs_count=len(m.get('sheets') or []),
                pending_count=0)
            mcard.clicked.connect(self._on_custom_card_clicked)
            # 2026-06-24 (монстры 3б): регистрируем карточку монстра в
            # _cards_by_slug — без этого общий прогресс-механизм ActorCard
            # (бар + счётчик секунд) не найдёт карточку во время генерации.
            # Зеркало актёрского цикла выше (восстановление active_gen).
            self._cards_by_slug[mslug] = mcard
            active_gen = getattr(self, '_active_generations', {}).get(mslug)
            if active_gen is not None:
                mcard.start_progress(active_gen.get('label'))
                mcard._gen_started_at = active_gen.get('started_at')
            r, c = divmod(next_idx, cols)
            self.grid.addWidget(mcard, r, c)
            next_idx += 1
        # 2026-06-24 (монстры 3а): карточка-плюс ВСЕГДА последней (после актёров
        # + монстров). Любой refresh пересчитывает её в самом конце → гарантия.
        add_card = self._make_add_custom_card()
        r, c = divmod(next_idx, cols)
        self.grid.addWidget(add_card, r, c)
        # Карточки прилипают к левому краю: stretch=0 на занятых колонках,
        # stretch=1 на финальной «пустой» колонке (cols) — она съедает
        # лишнее пространство справа. Иначе при cols=8 и 3 карточках они
        # бы расползались по ширине с гигантскими промежутками.
        for c in range(cols):
            self.grid.setColumnStretch(c, 0)
        self.grid.setColumnStretch(cols, 1)
        # После пересборки сетки — обновить состояние pulse-таймера
        # (старые карточки удалены, надо подкинуть стиль свежим).
        self._update_pending_pulse_running()

    def _list_custom_characters(self) -> List[Dict]:
        """2026-06-24 (монстры): читает локальный стор нестандартных персонажей
        actors/_custom/custom.json. Возвращает список записей с разрешённым путём
        превью (portrait). Нет файла / битый JSON → [] (НЕ падаем). Стор '_custom'
        ('_'-префикс) НЕ синкается к коллегам (snapshot upload и download-mirror
        пропускают '_'-папки) → локальные данные машины.

        Cross-platform: pathlib.Path + json, без subprocess/shell."""
        out: List[Dict] = []
        try:
            custom_root = self.project_root / "actors" / "_custom"
            meta_path = custom_root / "custom.json"
            if not meta_path.is_file():
                return out
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return out
            for slug, rec in data.items():
                if not isinstance(rec, dict):
                    continue
                portrait_rel = rec.get("portrait") or ""
                photos: List[Path] = []
                if portrait_rel:
                    p = custom_root / portrait_rel
                    if p.is_file():
                        photos = [p]
                out.append({
                    "slug": slug,
                    "display_name": rec.get("display_name") or slug,
                    "description": rec.get("description") or "",
                    "portrait": portrait_rel,
                    "photos": photos,
                    "sheets": rec.get("sheets") or [],
                    "created": rec.get("created") or "",
                })
        except Exception:
            traceback.print_exc()
        return out

    def _make_custom_slug(self, name: str) -> str:
        """2026-06-24 (монстры 3б): slug нестандартного персонажа из имени.
        База — актёрский slugify (транслит RU→EN), + суффикс _2/_3 при
        коллизии С КЛЮЧАМИ custom.json И с папками actors/_custom/<slug>/.
        Зеркалит логику create_actor (storyboard_app). Cross-platform:
        pathlib.Path + json, без subprocess/shell."""
        base_slug = _sa.slugify_actor_name(name)
        custom_root = self.project_root / "actors" / "_custom"
        taken = set()
        try:
            meta_path = custom_root / "custom.json"
            if meta_path.is_file():
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    taken.update(data.keys())
        except Exception:
            traceback.print_exc()
        candidate = base_slug
        i = 2
        while candidate in taken or (custom_root / candidate).exists():
            candidate = f"{base_slug}_{i}"
            i += 1
        return candidate

    def _upsert_custom_character(self, slug: str, display_name: str,
                                 description: str, sheet_rel: str) -> None:
        """2026-06-24 (монстры 3б): upsert записи монстра в
        actors/_custom/custom.json. sheet_rel (если непустой) добавляется в
        sheets[]. portrait НЕ пишем (отложено на 3в). Атомарная запись
        (temp + os.replace) — cross-platform (Mac/Win). Битый/отсутствующий
        JSON → стартуем с {}. Никогда не валит вызывающий flow."""
        import os
        from datetime import datetime as _dt
        try:
            custom_root = self.project_root / "actors" / "_custom"
            custom_root.mkdir(parents=True, exist_ok=True)
            meta_path = custom_root / "custom.json"
            data = {}
            if meta_path.is_file():
                try:
                    loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception:
                    data = {}
            rec = data.get(slug)
            if not isinstance(rec, dict):
                rec = {"display_name": display_name, "description": description,
                       "sheets": [],
                       "created": _dt.now().strftime("%Y-%m-%d %H:%M:%S")}
            sheets = rec.get("sheets")
            if not isinstance(sheets, list):
                sheets = []
            if sheet_rel and sheet_rel not in sheets:
                sheets.append(sheet_rel)
            rec["sheets"] = sheets
            rec["display_name"] = display_name
            rec["description"] = description
            rec.setdefault("created", _dt.now().strftime("%Y-%m-%d %H:%M:%S"))
            data[slug] = rec
            tmp_path = meta_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(str(tmp_path), str(meta_path))
        except Exception:
            traceback.print_exc()

    def _delete_custom_character(self, slug: str,
                                 only_if_empty: bool = False) -> None:
        """2026-06-24 (монстры 3б): удаляет запись монстра из custom.json.
        only_if_empty=True → удаляет лишь когда sheets пуст (откат
        placeholder-записи после неудачной ПЕРВОЙ генерации, чтобы не
        оставлять «призрак»-карточку без листов). Атомарно (temp+os.replace)."""
        import os
        try:
            custom_root = self.project_root / "actors" / "_custom"
            meta_path = custom_root / "custom.json"
            if not meta_path.is_file():
                return
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                return
            if not isinstance(data, dict) or slug not in data:
                return
            rec = data.get(slug) or {}
            if only_if_empty and (rec.get("sheets") or []):
                return
            del data[slug]
            tmp_path = meta_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(str(tmp_path), str(meta_path))
        except Exception:
            traceback.print_exc()

    def start_custom_ref_generation(self, slug: str, display_name: str,
                                    description: str) -> bool:
        """2026-06-24 (монстры 3б): ТОНКАЯ обёртка генерации листа
        нестандартного персонажа БЕЗ фото. Повторяет прогресс-обвязку
        start_ref_generation (бар + секунды на карточке — общий механизм
        ActorCard), НО:
          • photos=[] → GenerateActorRefThread шлёт text2img (без inputs);
          • prompt = ACTOR_REF_PROMPT_CUSTOM.format(description=...);
          • target_dir = actors/_custom/<slug>/ ('_'-папка к коллегам не синкается);
          • хвост _on_finished: вместо _pending_variants → _upsert_custom_character
            + refresh() (актёрский start_ref_generation НЕ трогаем).
        Поток parent=None + ссылка в self._ref_threads (паттерн A,
        ARCHITECTURE.md «parent для QThread»). Возвращает True если стартовал.

        placeholder-запись custom.json (sheets=[]) делается ДО старта — иначе
        карточки монстра нет в гриде и прогресс-бар некуда повесить; на finish
        дописываем реальный sheet_rel, на error — откат если листов нет."""
        try:
            target_dir = self.project_root / "actors" / "_custom" / slug
            prompt = _sa.ACTOR_REF_PROMPT_CUSTOM.format(description=description)
            if not hasattr(self, '_ref_threads'):
                self._ref_threads = []
            if not hasattr(self, '_active_generations'):
                self._active_generations: Dict[str, Dict] = {}

            # placeholder → refresh() нарисует карточку + повесит прогресс.
            self._upsert_custom_character(slug, display_name, description, "")

            thread = GenerateActorRefThread(
                slug, target_dir, [], prompt, slug, parent=None)
            self._ref_threads.append(thread)

            self._active_generations[slug] = {
                'started_at': time.time(),
                'label': tr('actor_progress_starting'),
            }
            self._actor_errors.pop(slug, None)
            # refresh() создаст карточку монстра и (через блок восстановления
            # в монстр-цикле) повесит на неё start_progress + секунды.
            self.refresh()

            def _on_progress(msg: str):
                if slug in self._active_generations:
                    self._active_generations[slug]['label'] = msg
                c = self._cards_by_slug.get(slug)
                if c is not None:
                    c.update_progress(msg)
                self._show_status_persistent(msg)

            def _on_finished(target_path: str):
                self._active_generations.pop(slug, None)
                c = self._cards_by_slug.get(slug)
                if c is not None:
                    c.stop_progress()
                # Хвост монстра: дописываем лист в custom.json (sheet_rel —
                # относительно actors/_custom/) + перерисовываем грид.
                try:
                    custom_root = self.project_root / "actors" / "_custom"
                    sheet_rel = str(Path(target_path).relative_to(custom_root))
                except Exception:
                    sheet_rel = Path(target_path).name
                self._upsert_custom_character(
                    slug, display_name, description, sheet_rel)
                self.refresh()
                self._notify_tab_blink_if_hidden()
                self._show_status_temp(
                    tr('create_ref_done', filename=Path(target_path).name))

            def _on_error(msg: str):
                self._active_generations.pop(slug, None)
                c = self._cards_by_slug.get(slug)
                if c is not None:
                    c.stop_progress()
                # Неудачная ПЕРВАЯ генерация: откат placeholder если листов нет
                # (не плодим «призрак»-карточки без листов).
                self._delete_custom_character(slug, only_if_empty=True)
                self.refresh()
                self._show_status_temp(msg or tr('create_ref_failed'))

            thread.progress.connect(_on_progress)
            thread.finished.connect(_on_finished)
            thread.error.connect(_on_error)
            thread.key_used.connect(
                lambda idx: getattr(self.window(), '_blink_key_indicator',
                                    lambda *a: None)(idx))
            self._show_status_persistent(
                tr('create_ref_started', actor=display_name))
            thread.start()
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _make_add_custom_card(self) -> QFrame:
        """2026-06-24 (монстры 3а): карточка-плюс «добавить нестандартного
        персонажа». Клик → _on_add_custom_clicked (на 3а заглушка; диалог в 3б)."""
        card = QFrame()
        card.setObjectName("add-custom-card")
        card.setFixedWidth(self._card_width)
        card.setMinimumHeight(self._card_width)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setStyleSheet(
            "QFrame#add-custom-card {"
            " background: rgba(255,255,255,0.03);"
            " border: 2px dashed rgba(255,255,255,0.18);"
            " border-radius: 12px; }"
            "QFrame#add-custom-card:hover {"
            " background: rgba(228,52,74,0.10);"
            " border-color: rgba(228,52,74,0.45); }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        plus = QLabel("+")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(
            "color: rgba(255,255,255,0.45); font-size: 64px;"
            " font-weight: 300; background: transparent; border: none;")
        cl.addWidget(plus)

        def _click(ev):
            if ev.button() == Qt.MouseButton.LeftButton:
                self._on_add_custom_clicked()
        card.mousePressEvent = _click  # type: ignore
        return card

    def _on_custom_card_clicked(self, slug: str):
        """2026-06-24 (монстры 3а, каркас): клик по карточке монстра. На 3в
        подключим попап «Все референсы» монстра. Сейчас — заглушка."""
        try:
            print(f"[custom] card clicked: {slug} (Шаг 3в подключит просмотр)")
        except Exception:
            pass

    def _on_add_custom_clicked(self):
        """2026-06-24 (монстры 3б): клик по карточке-плюс → диалог создания
        нестандартного персонажа (Имя + Описание). По «Сгенерировать» строим
        slug и запускаем генерацию листа БЕЗ фото-референса (text2img)."""
        try:
            dlg = CustomCharacterDialog(self.project_root, parent=self.window())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            name = (dlg.result_name or "").strip()
            desc = (dlg.result_desc or "").strip()
            if not name or not desc:
                return
            slug = self._make_custom_slug(name)
            self.start_custom_ref_generation(slug, name, desc)
        except Exception:
            traceback.print_exc()

    def _calc_cols(self) -> int:
        """Сколько карточек влезает в ряд при текущей ширине viewport
        и текущей ширине карточки. Учитывает spacing сетки.

        Доступная ширина = ширина grid_widget. Если ещё не отрисовался
        (на первом refresh после показа вкладки) — берём ширину самого
        ActorsView минус горизонтальные отступы inner-контейнера (28+28)."""
        spacing = self.grid.spacing() if hasattr(self, 'grid') else 14
        available = 0
        try:
            available = max(0, self.grid_widget.width())
        except Exception:
            pass
        if available <= self._card_width:
            try:
                available = max(0, self.width() - 56)  # 28+28 contentsMargins
            except Exception:
                available = self._card_width  # fallback → 1 колонка
        # Один «слот» = карточка + spacing справа. Последний слот в ряду
        # spacing справа не использует, поэтому формула с +spacing.
        slot = max(1, self._card_width + spacing)
        cols = max(1, (available + spacing) // slot)
        return int(cols)

    def resizeEvent(self, ev):
        """При изменении размера окна перерасчитываем количество колонок.
        Дебаунс 120мс — чтобы не дёргать refresh() на каждый пиксель
        пока юзер тянет угол окна."""
        super().resizeEvent(ev)
        try:
            t = getattr(self, '_resize_timer', None)
            if t is None:
                t = QTimer(self)
                t.setSingleShot(True)
                t.setInterval(120)
                t.timeout.connect(self._on_resize_done)
                self._resize_timer = t
            t.start()
        except Exception:
            pass

    def showEvent(self, ev):
        """Вкладка стала видимой — запускаем pulse если есть pending."""
        super().showEvent(ev)
        try:
            self._update_pending_pulse_running()
        except Exception:
            pass

    def hideEvent(self, ev):
        """Вкладка скрыта — останавливаем pulse-timer чтобы не было
        каскадных repaint'ов на других вкладках."""
        super().hideEvent(ev)
        try:
            self._pending_pulse_timer.stop()
        except Exception:
            pass

    def _update_pending_pulse_running(self):
        """Запускает или останавливает централизованный pulse-таймер
        в зависимости от того есть ли pending-варианты И видна ли вкладка.
        Вызывается из refresh() после пересоздания карточек и из showEvent."""
        try:
            has_pending = bool(self._pending_variants)
            is_visible = self.isVisible()
            if has_pending and is_visible:
                if not self._pending_pulse_timer.isActive():
                    self._pending_pulse_timer.start()
            else:
                self._pending_pulse_timer.stop()
                # Сбрасываем визуал к OFF состоянию у всех карточек
                self._pending_pulse_state = False
                for card in getattr(self, '_cards_by_slug', {}).values():
                    if card is not None:
                        try:
                            card._apply_pending_btn_style(False)
                        except Exception:
                            pass
        except Exception:
            traceback.print_exc()

    def _tick_pending_pulse(self):
        """Тоггл pulse-state каждые 600мс. Применяет стиль ко ВСЕМ pending
        кнопкам сразу через _cards_by_slug. Останавливается если pending
        опустел или вкладка скрыта."""
        try:
            if not self._pending_variants or not self.isVisible():
                self._pending_pulse_timer.stop()
                return
            self._pending_pulse_state = not self._pending_pulse_state
            for card in getattr(self, '_cards_by_slug', {}).values():
                if card is not None and card.pending_btn is not None:
                    try:
                        card._apply_pending_btn_style(self._pending_pulse_state)
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()

    def _on_resize_done(self):
        """Колбек дебаунса. Перерисовываем сетку только если число колонок
        реально изменилось — иначе зря дёргаем grid и моргают карточки."""
        try:
            new_cols = self._calc_cols()
            if new_cols != getattr(self, '_last_cols', None):
                self.refresh()
        except Exception:
            traceback.print_exc()

    def _on_actor_card_clicked(self, slug: str):
        """Клик по карточке актёра → открыть попап с галереей всех его фото.
        Если фото нет — показать статус-бар уведомление, попап не открывать."""
        try:
            display = _sa.actor_display_name(self.project_root, slug)
            photos = _sa.get_actor_photos(self.project_root, slug)
            dlg = ActorPhotosDialog(display, photos, parent=self)
            dlg.exec()
        except Exception:
            traceback.print_exc()

    def _on_view_refs_requested(self, slug: str):
        """Кнопка «Все референсы (N)» → попап с галереей character-рефов
        ЭТОГО актёра в АКТИВНОМ сериале.

        Связь «актёр играет персонажа Х в сериале Y» хранится в
        `actors.json:roles`, пишется при создании рефа в
        CreateActorRefDialog. Кнопка читает эту связь и открывает папку
        соответствующего персонажа (например akter_4 в the_last_plan
        играет laura → попап показывает refs/characters/laura/*.jpg).

        Если актёр не имеет ни одного рефа в активном сериале (никогда
        не использовался для генерации) — статус-сообщение «нет рефов»,
        попап не открывается. Юзер должен сгенерировать первый реф
        через ✨ — связь запишется и кнопка заработает."""
        try:
            display = _sa.actor_display_name(self.project_root, slug)
            refs = _sa.get_actor_generated_refs_paths(slug)
            if not refs:
                if self.status_bar:
                    self._show_status_temp(
                        tr('actor_refs_dialog_empty'))
                return
            # Папка для кнопки «📂 Показать в папке» — это папка
            # персонажа которого играет актёр (parent любого рефа).
            try:
                folder_path = refs[0].parent
            except Exception:
                folder_path = None
            # 2026-05-17 (Этап 3): вторая кнопка «🎨 Папка с текстурами» —
            # путь к shows/<show>/refs/characters_texture/<character>/.
            # character_slug определяется из folder_path (имя папки рефов).
            # Если folder_path не вычислился — texture_folder_path None,
            # кнопка просто не появится.
            texture_folder_path = None
            if folder_path is not None:
                try:
                    character_slug_for_tex = folder_path.name
                    tex_root_for_open = None
                    try:
                        tex_root_for_open = _sa.CHARACTERS_TEXTURE_DIR
                    except Exception:
                        tex_root_for_open = None
                    if not tex_root_for_open:
                        cur_show = (_sa.get_current_show(self.project_root)
                                    or "_none_")
                        tex_root_for_open = (
                            self.project_root / "shows" / cur_show
                            / "refs" / "characters_texture")
                    from pathlib import Path as _Path2
                    texture_folder_path = (
                        _Path2(tex_root_for_open) / character_slug_for_tex)
                except Exception:
                    traceback.print_exc()
                    texture_folder_path = None
            # 2026-05-05: если активен wildcard pending (юзер пришёл из
            # «+ Добавить персонажа» в РЕФЕРЕНСАХ) — показываем под каждой
            # превью кнопку «✓ Использовать в эпизоде» чтобы юзер взял
            # готовый реф вместо генерации нового.
            pick_for_ep_active = bool(
                self._pending_ep_links.get("__any__"))
            # 2026-06-03: «🔲 Папка с сетками» — shows/<show>/refs/
            # characters_grid/<character>/. character_slug = имя папки рефов.
            grid_folder_path = None
            if folder_path is not None:
                try:
                    cur_show_g = (_sa.get_current_show(self.project_root)
                                  or "_none_")
                    grid_folder_path = (
                        self.project_root / "shows" / cur_show_g
                        / "refs" / "characters_grid" / folder_path.name)
                except Exception:
                    traceback.print_exc()
                    grid_folder_path = None
            dlg = ActorPhotosDialog(display, refs, parent=self,
                                    folder_path=folder_path,
                                    enable_delete=True,
                                    enable_pick_for_ep=pick_for_ep_active,
                                    enable_edit=True,
                                    enable_texture=True,
                                    texture_folder_path=texture_folder_path,
                                    grid_folder_path=grid_folder_path)
            if pick_for_ep_active:
                dlg.picked_for_ep.connect(self._on_pick_existing_ref_for_ep)
            # 2026-05-17: edit-режим для рефа актёра (попап «Все референсы»).
            # Юзер вводит коротко правку → EditActorRefThread → новый
            # файл в той же папке актёра с инкрементным суффиксом.
            dlg.edit_ref_requested.connect(self._on_edit_actor_ref)
            # 2026-05-17 (Этап 2): «🎨 Текстура» — открыть ApplyTextureDialog
            # → ApplyTextureThread → shows/<show>/refs/characters_texture/.
            dlg.apply_texture_requested.connect(self._on_apply_texture_to_ref)
            # 2026-06-03: «🔲 Сетка на лицо» — открыть ActorGridDialog.
            dlg.apply_grid_requested.connect(self._on_apply_grid_to_ref)
            dlg.setWindowTitle(tr('actor_refs_dialog_title',
                                  name=display, n=len(refs)))
            dlg.exec()
            # Юзер посмотрел рефы — очищаем «непросмотренные» для этого актёра
            if hasattr(self, '_unseen_refs'):
                self._unseen_refs.pop(slug, None)
            # Юзер мог удалить рефы — обновляем счётчик «(N)» на карточке
            self.refresh()
        except Exception:
            traceback.print_exc()

    def _on_edit_actor_ref(self, path, instruction: str):
        """2026-05-17: edit-режим для рефа актёра из попапа «Все референсы».

        Юзер ввёл коротко правку (например «добавь штукатурку на голову»).
        Запускаем `EditActorRefThread`:
          • source = текущий реф `path` (uploaded через _read_image_for_upload —
            ресайз ≤2000px + MIME по магическим байтам, симметрия с
            create-flow и shot-edit);
          • prompt — шаблон с `[@]img1` identity-якорем + инструкцией;
          • результат — НОВЫЙ файл в той же папке с инкрементным суффиксом
            (тот же collision-rename что в GenerateActorRefThread).

        Сам thread живёт в ActorsView (не в попапе) — переживает
        закрытие диалога. Прогресс/finished/error обрабатываются
        локально (статус-бар + refresh карточки). Auto-link к эпизоду
        НЕ делается — это просто новый вариант рефа.

        Прогресс-индикатор (2026-05-17 fix): reverse-lookup actor_slug
        по character_slug (имя папки) + current_show через actors.json,
        затем тот же UI-flow что в create-flow — start_progress на
        карточке + регистрация в _active_generations.
        """
        try:
            from pathlib import Path as _Path
            src = _Path(path)
            if not src.exists():
                return
            # Папка актёра — родитель файла (refs/characters/<character>/).
            target_dir = src.parent
            # character_slug = имя папки (например 'laura'). Для diag-лога
            # и thread'а — этого достаточно.
            character_slug = target_dir.name
            # Reverse-lookup actor_slug по character_slug + current_show.
            # actors.json хранит {actor_slug: {roles: {show: character}}}.
            # Нужно чтобы найти ActorCard в self._cards_by_slug (он
            # индексирован по actor_slug, например 'akter_4', не по
            # character_slug 'laura'). Без этого индикатор прогресса
            # не появится на карточке.
            actor_slug = character_slug
            try:
                cur_show = _sa.get_current_show(self.project_root)
                actors_meta = _sa.read_actors_meta(self.project_root) or {}
                for a_slug, a_data in actors_meta.items():
                    roles = (a_data or {}).get('roles') or {}
                    if roles.get(cur_show) == character_slug:
                        actor_slug = a_slug
                        break
            except Exception:
                # Reverse-lookup упал — fallback на character_slug.
                # Thread всё равно стартует, просто без индикатора на
                # карточке. Юзер увидит результат после refresh.
                traceback.print_exc()
            if not hasattr(self, '_ref_threads'):
                self._ref_threads = []
            if not hasattr(self, '_active_generations'):
                self._active_generations: Dict[str, Dict] = {}
            thread = EditActorRefThread(
                actor_slug=actor_slug,
                target_dir=target_dir,
                source_image_path=src,
                instruction=instruction,
                parent=None,
            )
            self._ref_threads.append(thread)

            # Регистрация в _active_generations + старт индикатора на
            # карточке — симметрия с create-flow (start_ref_generation).
            self._active_generations[actor_slug] = {
                'started_at': time.time(),
                'label': tr('actor_progress_starting'),
            }
            self._actor_errors.pop(actor_slug, None)
            card = self._cards_by_slug.get(actor_slug) if hasattr(
                self, '_cards_by_slug') else None
            if card is not None:
                card.clear_error()
                card.start_progress(tr('actor_progress_starting'))

            def _on_progress(msg: str):
                # Обновляем overlay на карточке + статус-бар (симметрия
                # с create-flow _on_progress).
                if actor_slug in self._active_generations:
                    self._active_generations[actor_slug]['label'] = msg
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.update_progress(msg)
                self._show_status_persistent(msg)

            def _on_finished(target_path: str):
                # Снять индикатор + чистка _active_generations.
                self._active_generations.pop(actor_slug, None)
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.stop_progress()
                # 2026-05-17: добавляем результат в pending-список актёра — это
                # триггерит жёлтую плашку "🆕 Готов новый референс (N)" на карточке
                # при следующем refresh. Симметрия с create-flow _on_finished.
                try:
                    if not hasattr(self, '_pending_variants'):
                        self._pending_variants = {}
                    self._pending_variants.setdefault(actor_slug, []).append(target_path)
                    # Если открыт RefResultDialog — добавим вариант сразу в его стек
                    open_dlg = self._open_result_dialogs.get(actor_slug) if hasattr(
                        self, '_open_result_dialogs') else None
                    if open_dlg is not None:
                        try:
                            open_dlg.append_variant(target_path)
                        except Exception:
                            traceback.print_exc()
                except Exception:
                    traceback.print_exc()
                try:
                    name = _Path(target_path).name
                    self._show_status_temp(
                        tr('create_ref_done', filename=name))
                except Exception:
                    pass
                # Перерисуем карточку актёра — счётчик «(N)» вырастет
                try:
                    self.refresh()
                except Exception:
                    traceback.print_exc()
                try:
                    self._notify_tab_blink_if_hidden()
                except Exception:
                    pass

            def _on_error(msg: str):
                # Снять индикатор + чистка _active_generations.
                self._active_generations.pop(actor_slug, None)
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.stop_progress()
                try:
                    self._show_status_temp(f"⚠ {msg}")
                except Exception:
                    pass

            thread.progress.connect(_on_progress)
            thread.finished.connect(_on_finished)
            thread.error.connect(_on_error)
            thread.key_used.connect(lambda idx: getattr(self.window(), '_blink_key_indicator', lambda *a: None)(idx))  # лампочка round-robin
            thread.start()
            # Сразу даём фидбек что генерация пошла
            try:
                self._show_status_persistent(
                    tr('create_ref_uploading', n=1))
            except Exception:
                pass
        except Exception:
            traceback.print_exc()

    def _on_apply_grid_to_ref(self, ref_path):
        """2026-06-03: handler «🔲 Сетка на лицо» из попапа всех рефов.

        Открывает ActorGridDialog для рефа. Результат — ОТДЕЛЬНЫЙ файл
        shows/<show>/refs/characters_grid/<slug>/<stem>_grid.jpg (оригинал
        refs/characters/ НЕ трогаем). Persist позиций — <stem>_grid.json рядом.

        Вложенный .exec() поверх модального ActorPhotosDialog — тот же
        рабочий паттерн, что у «🎨 Текстура» (ApplyTextureDialog.exec()).
        Cross-platform: Path + mkdir, без subprocess."""
        try:
            from pathlib import Path as _Path
            src = _Path(ref_path)
            if not src.exists():
                return
            character_slug = src.parent.name
            cur_show = _sa.get_current_show(self.project_root) or "_none_"
            target_dir = (self.project_root / "shows" / cur_show
                          / "refs" / "characters_grid" / character_slug)
            target_dir.mkdir(parents=True, exist_ok=True)
            save_path = target_dir / f"{src.stem}_grid.jpg"
            from widgets.face_grid.actor_grid_dialog import ActorGridDialog
            ActorGridDialog(image_path=src, save_path=save_path,
                            title=src.stem, parent=self).exec()
            # Юзер мог сохранить/изменить сетку — обновим карточки.
            self.refresh()
        except Exception:
            traceback.print_exc()

    def _on_apply_texture_to_ref(self, ref_path):
        """2026-05-17 (Этап 2): handler «🎨 Текстура» из попапа всех рефов.

        Открывает ApplyTextureDialog (picker текстур + слайдер opacity +
        live preview). Если юзер выбрал текстуру и нажал «Применить» —
        запускает ApplyTextureThread:
          • source = ref_path
          • texture = диалог.selected_texture
          • opacity = диалог.selected_opacity
          • target = CHARACTERS_TEXTURE_DIR/<character_slug>/
                     <ref_stem>_<opacity>pct.jpg
            Если файл уже существует (тот же opacity повторно) —
            **перезаписывается** (юзер expectation).

        Индикатор прогресса — по тому же pattern что в _on_edit_actor_ref:
          • reverse-lookup actor_slug по character_slug + current_show
          • _active_generations[actor_slug] + card.start_progress
          • _on_progress / _on_finished / _on_error
        """
        try:
            from pathlib import Path as _Path
            src = _Path(ref_path)
            if not src.exists():
                return
            character_slug = src.parent.name
            # 1. Открыть picker-диалог. result_dir рассчитываем заранее
            #    (CHARACTERS_TEXTURE_DIR/<character>/) — диалог
            #    использует его для load_meta_for_source (восстановление
            #    прошлых настроек).
            textures_dir = self.project_root / "actors" / "_textures"
            try:
                tex_root_for_meta = _sa.CHARACTERS_TEXTURE_DIR
            except Exception:
                tex_root_for_meta = None
            if not tex_root_for_meta:
                cur_show = _sa.get_current_show(self.project_root) or "_none_"
                tex_root_for_meta = (
                    self.project_root / "shows" / cur_show
                    / "refs" / "characters_texture")
            result_dir_for_meta = (
                _Path(tex_root_for_meta) / character_slug)
            dlg = ApplyTextureDialog(
                src, textures_dir, parent=self,
                result_dir=result_dir_for_meta)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            tex_path = dlg.selected_texture
            opacity = int(dlg.selected_opacity)
            if tex_path is None:
                return
            # 2. Reverse-lookup actor_slug по character + current_show
            actor_slug = character_slug
            try:
                cur_show = _sa.get_current_show(self.project_root)
                actors_meta = _sa.read_actors_meta(self.project_root) or {}
                for a_slug, a_data in actors_meta.items():
                    roles = (a_data or {}).get('roles') or {}
                    if roles.get(cur_show) == character_slug:
                        actor_slug = a_slug
                        break
            except Exception:
                traceback.print_exc()
            # 3. Target path: CHARACTERS_TEXTURE_DIR/<char>/<stem>_NNpct.jpg
            try:
                tex_root = _sa.CHARACTERS_TEXTURE_DIR
            except Exception:
                tex_root = None
            if not tex_root:
                # Fallback — собираем путь руками если CHARACTERS_TEXTURE_DIR
                # ещё не инициализирован (теоретически не должно случаться,
                # init_show_paths вызывается при выборе шоу).
                cur_show = _sa.get_current_show(self.project_root) or "_none_"
                tex_root = (self.project_root / "shows" / cur_show
                            / "refs" / "characters_texture")
            target_dir = _Path(tex_root) / character_slug
            target_path = target_dir / f"{src.stem}_{opacity}pct.jpg"
            # 4. Thread + индикатор (симметрия с _on_edit_actor_ref)
            if not hasattr(self, '_ref_threads'):
                self._ref_threads = []
            if not hasattr(self, '_active_generations'):
                self._active_generations: Dict[str, Dict] = {}
            thread = ApplyTextureThread(
                source_image_path=src,
                texture_path=tex_path,
                opacity_percent=opacity,
                target_path=target_path,
                zoom_percent=int(getattr(dlg, 'selected_zoom', 100)),
                offset_x=int(getattr(dlg, 'selected_offset_x', 0)),
                offset_y=int(getattr(dlg, 'selected_offset_y', 0)),
                parent=None,
            )
            self._ref_threads.append(thread)
            self._active_generations[actor_slug] = {
                'started_at': time.time(),
                'label': tr('apply_texture_progress'),
            }
            self._actor_errors.pop(actor_slug, None)
            card = self._cards_by_slug.get(actor_slug) if hasattr(
                self, '_cards_by_slug') else None
            if card is not None:
                card.clear_error()
                card.start_progress(tr('apply_texture_progress'))

            def _on_progress(msg: str):
                if actor_slug in self._active_generations:
                    self._active_generations[actor_slug]['label'] = msg
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.update_progress(msg)
                self._show_status_persistent(msg)

            def _on_finished(out_path: str):
                self._active_generations.pop(actor_slug, None)
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.stop_progress()
                try:
                    name = _Path(out_path).name
                    self._show_status_temp(
                        tr('apply_texture_done', filename=name))
                except Exception:
                    pass
                try:
                    self.refresh()
                except Exception:
                    traceback.print_exc()
                try:
                    self._notify_tab_blink_if_hidden()
                except Exception:
                    pass

            def _on_error(msg: str):
                self._active_generations.pop(actor_slug, None)
                c = self._cards_by_slug.get(actor_slug)
                if c is not None:
                    c.stop_progress()
                try:
                    self._show_status_temp(f"⚠ {msg}")
                except Exception:
                    pass

            thread.progress.connect(_on_progress)
            thread.finished.connect(_on_finished)
            thread.error.connect(_on_error)
            thread.start()
            try:
                self._show_status_persistent(tr('apply_texture_progress'))
            except Exception:
                pass
        except Exception:
            traceback.print_exc()

    def _on_create_ref_requested(self, slug: str):
        """Кнопка «Создать референс» на карточке. Открываем попап,
        в нём юзер пишет описание одежды и выбирает вариант layout-а.
        После «Сгенерировать» — фоновый поток отправляется ActorsView
        (а не диалогу!), чтобы не убиться при закрытии диалога.

        Долг 13: если есть `_pending_create_request` (юзер пришёл из
        чата эпизода с готовым описанием одежды) — префиллим попап
        и сбрасываем pending после открытия диалога."""
        try:
            display = _sa.actor_display_name(self.project_root, slug)
            photos = _sa.get_actor_photos(self.project_root, slug)
            if not photos:
                if self.status_bar:
                    self.status_bar.showMessage(
                        tr('create_ref_no_photos', actor=display), 5000)
                return
            # Долг 13: префиллы из чата (если ждут).
            prefill_show = None
            prefill_character = None
            prefill_description = None
            req = self._pending_create_request
            if req:
                prefill_show = req.get("show") or None
                # 2026-05-05 fix Bug A: для combo-бокса используем чистый
                # slug ('muzh'), а НЕ composite-label 'muzh (Муж)'.
                # Иначе combo не находит существующий пункт → fallback на
                # «➕ Создать нового» → транслит даёт `muzh_muzh` →
                # дубликат папки.
                prefill_character = (req.get("character_slug")
                                     or req.get("character")
                                     or None)
                prefill_description = req.get("description") or None
            dlg = CreateActorRefDialog(
                self.project_root, slug, display,
                photos, status_bar=self.status_bar,
                owner_view=self, parent=self,
                prefill_show=prefill_show,
                prefill_character=prefill_character,
                prefill_description=prefill_description)
            # Чистим pending до показа диалога — если юзер закроет без
            # сабмита, повторный клик «Создать референс» уже не префилит
            # тем же текстом (это намеренно: юзер увидел и отменил).
            if req:
                self._clear_pending_request()
            dlg.exec()
        except Exception:
            traceback.print_exc()

    # ── Долг 13: pending-запрос из чата эпизода ─────────────────────

    def set_pending_create_request(self, character: str, show: str,
                                    description: str,
                                    ep_id: str = "",
                                    character_slug: str = ""):
        """Вызывается из EpisodeChatView когда юзер выбрал вариант
        одежды для character'а. Запоминаем запрос + показываем баннер
        сверху страницы с подсказкой «выбери актёра».

        `ep_id` + `character_slug` сохраняются в `_pending_ep_link`
        (живёт дольше — до завершения генерации) чтобы в `_on_finished`
        записать `refs_decisions` и реф автоматически появился в
        РЕФЕРЕНСАХ эпизода.

        `character` тут — composite-label «slug (Оригинал)» для UI баннера.
        `character_slug` — чистый ASCII slug, идёт в combo-бокс попапа
        напрямую (чтобы НЕ создавалась дубль-папка `muzh_muzh/`)."""
        self._pending_create_request = {
            "character": character or "",   # для текста баннера
            "character_slug": character_slug or "",  # для combo-бокса
            "show": show or "",
            "description": description or "",
        }
        if ep_id and character_slug:
            self._pending_ep_links[character_slug] = ep_id
        try:
            self.pending_banner_lbl.setText(
                tr('actors_pending_banner',
                   character=character or "?", show=show or "?"))
            self.pending_banner.show()
        except Exception:
            traceback.print_exc()

    def _clear_pending_request(self):
        """Сбрасывает pending-запрос и прячет баннер. НЕ трогает
        `_pending_ep_link` — он живёт до `_on_finished` (или явной
        отмены через _on_pending_banner_cancel)."""
        self._pending_create_request = None
        try:
            self.pending_banner.hide()
        except Exception:
            pass

    def set_pending_any_character_for_ep(self, ep_id: str):
        """2026-05-05: pending-запрос из вкладки РЕФЕРЕНСЫ — «привязать
        любой следующий character-реф к этому ep_id». В отличие от
        обычного pending (по конкретному slug'у), здесь slug заранее
        неизвестен — юзер выберет персонажа в попапе создания.

        Записывается под специальный ключ `__any__` в `_pending_ep_links`.
        В `_auto_link_actor_ref_to_episode` если для конкретного slug
        записи нет — пробуем `__any__` (и удаляем после применения).
        Также показываем баннер чтобы юзер видел что он в режиме
        привязки к эпизоду."""
        if not ep_id:
            return
        self._pending_ep_links["__any__"] = ep_id
        self._pending_create_request = {
            "character": tr('actors_pending_any_label', ep=ep_id),
            "character_slug": "",
            "show": ep_id,
            "description": "",
        }
        try:
            self.pending_banner_lbl.setText(
                tr('actors_pending_any_banner', ep=ep_id))
            self.pending_banner.show()
        except Exception:
            traceback.print_exc()

    def _on_pending_banner_cancel(self):
        """Клик «✕ Отмена» на баннере — сбросить pending без открытия попапа.

        2026-05-05: удаляем ТОЛЬКО slug текущего баннера из
        `_pending_ep_links`, не весь dict. Иначе отмена для одного
        персонажа стёрла бы автолинковку других параллельно идущих
        генераций. Если slug пустой (wildcard `__any__` от кнопки
        «+ Добавить персонажа») — удаляем именно `__any__`."""
        slug_to_clear = ""
        if self._pending_create_request:
            slug_to_clear = self._pending_create_request.get(
                "character_slug") or ""
        self._clear_pending_request()
        if slug_to_clear:
            self._pending_ep_links.pop(slug_to_clear, None)
        else:
            self._pending_ep_links.pop("__any__", None)

    def _auto_link_actor_ref_to_episode(self, target_dir: Path,
                                         target_path: str):
        """Ищем `target_dir.name` в `_pending_ep_links` (dict
        character_slug → ep_id). Если есть запись — пишем
        `refs_decisions[character][slug] = {decision: linked, filename:
        <slug>/<file>.jpg}` в `episodes.json[ep_id]` и удаляем эту
        запись из dict (другие параллельные генерации не трогаем)."""
        try:
            slug = target_dir.name
        except Exception:
            return
        if not slug:
            return
        ep_id = self._pending_ep_links.pop(slug, "")
        if not ep_id:
            # 2026-05-05: fallback на wildcard `__any__` (запрос из
            # кнопки «+ Добавить персонажа» в РЕФЕРЕНСАХ — slug заранее
            # не известен). Если есть — линкуем и удаляем.
            ep_id = self._pending_ep_links.pop("__any__", "")
            if not ep_id:
                return
        # Имя файла относительно папки персонажа.
        try:
            file_name = Path(target_path).name
        except Exception:
            return
        if not file_name:
            return
        # Определяем активный сериал и пишем в его episodes.json.
        try:
            cur_show = _sa.get_current_show(self.project_root)
        except Exception:
            cur_show = None
        if not cur_show:
            return
        meta_path = self.project_root / "shows" / cur_show / "episodes.json"
        try:
            import json
            data = {}
            if meta_path.exists():
                try:
                    data = json.loads(
                        meta_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            ep = data.setdefault(ep_id, {})
            decisions = ep.setdefault("refs_decisions", {})
            bucket = decisions.setdefault("character", {})
            bucket[slug] = {
                "decision": "linked",
                "filename": f"{slug}/{file_name}",
            }
            meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            traceback.print_exc()
            return
        # Триггерим refs-view rebuild чтобы юзер увидел новый реф
        # без перезагрузки эпизода (если сейчас на нём).
        try:
            mw = self.parent()
            while mw is not None and not hasattr(mw, '_build_refs_view'):
                mw = mw.parent()
            if mw is not None:
                mw._meta = data
                if (getattr(mw, '_current_episode', None) == ep_id):
                    mw._build_refs_view(ep_id)
        except Exception:
            traceback.print_exc()

    def start_ref_generation(self, actor_slug: str, photos: List[Path],
                             prompt_text: str, output_filename: str,
                             display_name: str, target_dir: Path,
                             outfit_text: str = "", *,
                             variant_id: str = "detailed"):
        """Стартует GenerateActorRefThread с ActorsView как родителем
        (живёт пока окно открыто) и хранит ссылку в self._ref_threads.
        ВАЖНО: parent=None для QThread — иначе если родитель = QDialog
        и юзер закрыл диалог → QThread destroyed while running → краш.

        `target_dir` — целевая папка для рефа (обычно
        shows/<show>/refs/characters/<character>/). Передаётся диалогом
        который знает выбранный сериал + персонажа.

        `outfit_text` — описание которое юзер ввёл (без шаблонной обвязки).
        Сохраняется в _last_outfit[slug] чтобы при открытии RefResultDialog
        попап показал предыдущее описание, а не пустое поле."""
        if outfit_text:
            self._last_outfit[actor_slug] = outfit_text
        self._last_variant[actor_slug] = variant_id
        if not hasattr(self, '_ref_threads'):
            self._ref_threads = []
        if not hasattr(self, '_active_generations'):
            # slug → {'started_at': ts, 'label': str} — на случай если
            # карточки пересоздадутся (resize/refresh) во время генерации
            self._active_generations: Dict[str, Dict] = {}
        if not hasattr(self, '_unseen_refs'):
            # slug → set(filename) — рефы которые юзер ещё не видел
            # после возврата на вкладку Актёры. Очищается при клике
            # «Все референсы» на карточке.
            self._unseen_refs: Dict[str, set] = {}

        thread = GenerateActorRefThread(
            actor_slug, target_dir, photos, prompt_text, output_filename,
            parent=None)
        self._ref_threads.append(thread)

        # Стартуем прогресс на самой карточке + регистрируем активную генерацию
        self._active_generations[actor_slug] = {
            'started_at': time.time(),
            'label': tr('actor_progress_starting'),
        }
        # 2026-05-05: чистим прошлую ошибку — юзер начал новую попытку.
        self._actor_errors.pop(actor_slug, None)
        card = self._cards_by_slug.get(actor_slug) if hasattr(
            self, '_cards_by_slug') else None
        if card is not None:
            card.clear_error()
            card.start_progress(tr('actor_progress_starting'))

        def _on_progress(msg: str):
            # Обновляем overlay на карточке + статус-бар
            if actor_slug in self._active_generations:
                self._active_generations[actor_slug]['label'] = msg
            c = self._cards_by_slug.get(actor_slug)
            if c is not None:
                c.update_progress(msg)
            self._show_status_persistent(msg)

        def _on_finished(target_path: str):
            self._active_generations.pop(actor_slug, None)
            c = self._cards_by_slug.get(actor_slug)
            if c is not None:
                c.stop_progress()
            # 2026-05-05: НЕ линкуем реф к эпизоду на этом этапе.
            # Сначала юзер должен подтвердить вариант через
            # RefResultDialog → «✓ Оставить этот». Только тогда
            # `confirm_pending_kept` запустит auto-link.
            # Кладём путь в pending-список этого актёра.
            self._pending_variants.setdefault(actor_slug, []).append(target_path)
            # Если уже открыт RefResultDialog для этого актёра — добавляем
            # вариант в его стек напрямую (юзер увидит сразу). Иначе
            # перерисовываем карточку, чтобы появилась кнопка
            # «🆕 Готов новый референс (N)».
            open_dlg = self._open_result_dialogs.get(actor_slug)
            if open_dlg is not None:
                try:
                    open_dlg.append_variant(target_path)
                except Exception:
                    traceback.print_exc()
            # Перерисовываем карточку (счётчик pending обновится)
            self.refresh()
            # Если юзер на другом табе — мигаем заголовком вкладки «Актёры»
            self._notify_tab_blink_if_hidden()
            # Финал → статус-бар (8с)
            name = Path(target_path).name
            self._show_status_temp(
                tr('create_ref_done', filename=name))
            # ВАЖНО: попап автоматически НЕ открываем. Юзер сам кликнет
            # на «🆕 Готов новый референс (N)» когда захочет посмотреть.

        def _on_error(msg: str):
            self._active_generations.pop(actor_slug, None)
            c = self._cards_by_slug.get(actor_slug)
            if c is not None:
                c.stop_progress()
            # 2026-05-07: снимаем флаг is_active_character_gen чтобы при
            # возврате в чат gen-карточка для этого персонажа появилась
            # снова (юзер сможет повторить выбор/попытку).
            try:
                char_slug = ""
                try:
                    char_slug = target_dir.name
                except Exception:
                    pass
                ep_id = self._pending_ep_links.get(char_slug, "") if char_slug else ""
                if not ep_id:
                    ep_id = self._pending_ep_links.get("__any__", "")
                if ep_id and char_slug:
                    mw = self.window()
                    if mw is not None and hasattr(mw, 'unregister_active_character_gen'):
                        ev = getattr(mw, 'episode_chat_view', None)
                        if ev is not None and hasattr(ev, 'notify_character_generation_finished'):
                            ev.notify_character_generation_finished(
                                ep_id, char_slug, success=False)
                        else:
                            mw.unregister_active_character_gen(ep_id, char_slug)
            except Exception:
                traceback.print_exc()
            # 2026-05-05: видимая ошибка — красная плашка на карточке
            # + сохранение в `_actor_errors` (восстановится при refresh).
            self._actor_errors[actor_slug] = msg or ""
            if c is not None:
                try:
                    c.set_error(msg or "")
                except Exception:
                    traceback.print_exc()
            # 2026-05-05: блокирующий PromptRetryDialog — показывает
            # исходный текст, ошибку API, и AI-предложенные смягчённые
            # варианты. Юзер кликает любой вариант → открываем
            # CreateActorRefDialog с предзаполненным описанием.
            try:
                from widgets import PromptRetryDialog
                rejected = self._last_outfit.get(actor_slug, "") or ""
                model = None  # default
                dlg = PromptRetryDialog(
                    actor_display=display_name,
                    original_prompt=rejected,
                    api_error=msg or "",
                    project_root=self.project_root,
                    model=model,
                    parent=self)
                dlg.retry_with.connect(
                    lambda new_text, slug=actor_slug:
                        self._on_prompt_retry(slug, new_text))
                dlg.exec()
            except Exception as retry_ex:
                # 2026-05-10 (БАГ 8 fix): раньше падение PromptRetryDialog
                # приводило к silent traceback.print_exc — юзер видел
                # только «карточка осталась как была» без UI feedback'а.
                # Теперь — fallback на QMessageBox с текстом исходной
                # ошибки и причиной краха retry-попапа.
                traceback.print_exc()
                try:
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.warning(
                        self,
                        tr('create_ref_failed_title'),
                        tr('create_ref_failed_msg',
                           actor=display_name,
                           error=msg or "?",
                           retry_error=str(retry_ex)[:200]))
                except Exception:
                    pass

        thread.progress.connect(_on_progress)
        thread.finished.connect(_on_finished)
        thread.error.connect(_on_error)
        thread.key_used.connect(lambda idx: getattr(self.window(), '_blink_key_indicator', lambda *a: None)(idx))  # лампочка round-robin
        # Старт — тоже persistent (висит до первого progress-сообщения)
        self._show_status_persistent(
            tr('create_ref_started', actor=display_name))
        thread.start()
        # 2026-05-07: уведомляем chat view о старте character-генерации.
        # Юзер выбрал outfit-вариант в чате → переключился сюда → нажал
        # Создать референс. Picker в чате должен закрыться сразу, а не
        # ждать пока юзер досмотрит варианты в Actors. character_slug =
        # имя папки рефа (target_dir.name = lora и т.п.).
        try:
            char_slug = ""
            try:
                char_slug = target_dir.name
            except Exception:
                pass
            ep_id = self._pending_ep_links.get(char_slug, "") if char_slug else ""
            if not ep_id:
                # fallback: wildcard «+ Добавить персонажа» из РЕФЕРЕНСОВ
                ep_id = self._pending_ep_links.get("__any__", "")
            if ep_id and char_slug:
                mw = self.window()
                if mw is not None and hasattr(mw, 'register_active_character_gen'):
                    mw.register_active_character_gen(ep_id, char_slug)
                    ev = getattr(mw, 'episode_chat_view', None)
                    if ev is not None and hasattr(ev, 'notify_character_generation_started'):
                        ev.notify_character_generation_started(ep_id, char_slug)
        except Exception:
            traceback.print_exc()

    def _on_pending_clicked(self, slug: str):
        """Клик по «🆕 Готов новый референс (N)» на карточке актёра.
        Открывает RefResultDialog со стеком pending-вариантов. Окно
        нельзя закрыть через X — только через выбор «Оставить этот»
        или клик «Пересоздать» (закрывает и стартует новую генерацию).

        В попап передаём последнее использованное описание (`_last_outfit`),
        чтобы юзер не вводил его заново при «Пересоздать»."""
        try:
            variants = list(self._pending_variants.get(slug, []))
            if not variants:
                return
            display = _sa.actor_display_name(self.project_root, slug)
            photos = _sa.get_actor_photos(self.project_root, slug)
            saved_outfit = self._last_outfit.get(slug, "")
            dlg = RefResultDialog(
                actor_slug=slug,
                display_name=display,
                photos=photos,
                initial_variants=variants,
                owner_view=self,
                initial_outfit=saved_outfit,
                variant_id=self._last_variant.get(slug, "detailed"),
                parent=self.window())
            self._open_result_dialogs[slug] = dlg
            # При закрытии любым способом — снять из открытых
            dlg.finished.connect(
                lambda _=None, s=slug: self._open_result_dialogs.pop(s, None))
            dlg.show()
        except Exception:
            traceback.print_exc()

    def _on_pick_existing_ref_for_ep(self, path):
        """2026-05-05: юзер кликнул «✓ Использовать в эпизоде» в попапе
        «Все референсы» актёра. Path — полный путь к выбранному рефу
        (например shows/<show>/refs/characters/<slug>/<file>.jpg).
        Извлекаем slug из родителя, пишем `_pending_ep_links[slug]`
        (используя ep_id из __any__) и зовём `_auto_link_actor_ref_to_episode`.
        """
        try:
            from pathlib import Path as _P
            p = _P(str(path))
            slug = p.parent.name
            ep_id = self._pending_ep_links.pop("__any__", "")
            if not slug or not ep_id:
                return
            # Ставим конкретный slug → ep_id чтобы auto-link нашёл match.
            self._pending_ep_links[slug] = ep_id
            self._auto_link_actor_ref_to_episode(p.parent, str(p))
            # Сбрасываем баннер.
            self._clear_pending_request()
        except Exception:
            traceback.print_exc()

    def _on_prompt_retry(self, actor_slug: str, new_text: str):
        """2026-05-05: юзер кликнул «↻ Повторить с этим вариантом» в
        PromptRetryDialog. Открываем CreateActorRefDialog для актёра
        с предзаполненным описанием = новый текст. Юзер может
        проверить/поправить и нажать «Сгенерировать»."""
        if not actor_slug or not new_text:
            return
        try:
            display = _sa.actor_display_name(self.project_root, actor_slug)
            photos = _sa.get_actor_photos(self.project_root, actor_slug)
            if not photos:
                if self.status_bar:
                    self.status_bar.showMessage(
                        tr('create_ref_no_photos', actor=display), 5000)
                return
            # Чистим overlay ошибки на карточке (юзер начинает retry).
            self._actor_errors.pop(actor_slug, None)
            c = self._cards_by_slug.get(actor_slug)
            if c is not None:
                c.clear_error()
            dlg = CreateActorRefDialog(
                self.project_root, actor_slug, display,
                photos, status_bar=self.status_bar,
                owner_view=self, parent=self,
                prefill_description=new_text)
            dlg.exec()
        except Exception:
            traceback.print_exc()

    def _on_actor_error_dismissed(self, slug: str):
        """Юзер кликнул «✕ Скрыть» на error overlay карточки. Удаляем
        ошибку из state — при следующем refresh не появится."""
        self._actor_errors.pop(slug, None)

    def confirm_pending_kept(self, slug: str, kept_path: str):
        """Юзер выбрал ОДИН вариант (нажал «Оставить этот»). Очищаем pending
        для этого актёра — карточка обновится без жёлтой кнопки.

        2026-05-05: ЗДЕСЬ запускаем auto-link рефа к эпизоду — раньше
        он бежал в `_on_finished` (сразу после генерации), но юзер мог
        ещё не выбрать финальный вариант (через «Пересоздать» он мог
        сделать ещё несколько). Теперь линкуем ровно тот файл который
        юзер подтвердил."""
        ep_id_for_unreg = ""
        char_slug_for_unreg = ""
        try:
            if kept_path:
                try:
                    target_dir = Path(kept_path).parent
                    char_slug_for_unreg = target_dir.name
                    # ep_id извлекаем из _pending_ep_links ДО _auto_link
                    # (т.к. auto_link его pop'ает).
                    ep_id_for_unreg = (
                        self._pending_ep_links.get(char_slug_for_unreg, "")
                        or self._pending_ep_links.get("__any__", ""))
                    self._auto_link_actor_ref_to_episode(
                        target_dir, kept_path)
                except Exception:
                    traceback.print_exc()
            self._pending_variants.pop(slug, None)
            self.refresh()
        except Exception:
            traceback.print_exc()
        # 2026-05-07: снимаем флаг is_active_character_gen — теперь реф
        # linked в episodes.json, и `_purge_resolved_markers` уберёт
        # gen-карточку из чата сам через timer.
        try:
            if ep_id_for_unreg and char_slug_for_unreg:
                mw = self.window()
                if mw is not None and hasattr(mw, 'unregister_active_character_gen'):
                    ev = getattr(mw, 'episode_chat_view', None)
                    if ev is not None and hasattr(ev, 'notify_character_generation_finished'):
                        ev.notify_character_generation_finished(
                            ep_id_for_unreg, char_slug_for_unreg, success=True)
                    else:
                        mw.unregister_active_character_gen(
                            ep_id_for_unreg, char_slug_for_unreg)
        except Exception:
            traceback.print_exc()

    def update_pending_variants(self, slug: str, variants: List[str]):
        """Синхронизация: попап говорит ActorsView актуальный список pending
        (юзер мог удалить какой-то вариант через ✕ в попапе). Карточка
        перерисуется чтобы счётчик «Готов новый (N)» совпадал."""
        try:
            if variants:
                self._pending_variants[slug] = list(variants)
            else:
                self._pending_variants.pop(slug, None)
            self.refresh()
        except Exception:
            traceback.print_exc()

    def _notify_tab_blink_if_hidden(self):
        """Если юзер на другой вкладке — заставляет таб «Актёры» мигать.
        Это аналог мигания пилюль блоков/референсов в редакторе.

        Через self.window() находим MainWindow → его tabs (QTabWidget) →
        вызываем хелпер `_blink_actors_tab` если он есть."""
        try:
            mw = self.window()
            if mw is None:
                return
            tabs = getattr(mw, 'tabs', None)
            if tabs is None:
                return
            # Индекс вкладки Актёры — ищем по тексту (избегаем хардкода
            # индекса т.к. порядок вкладок может меняться от версии).
            actors_idx = -1
            for i in range(tabs.count()):
                if tabs.tabText(i) == tr('tab_actors'):
                    actors_idx = i
                    break
            if actors_idx < 0:
                return
            # Если юзер УЖЕ на этой вкладке — ничего не мигаем
            if tabs.currentIndex() == actors_idx:
                return
            # Стартуем мигание через MainWindow
            blink = getattr(mw, '_start_actors_tab_blink', None)
            if callable(blink):
                blink()
        except Exception:
            traceback.print_exc()

    def _show_status_persistent(self, msg: str):
        """Показать сообщение в статус-баре БЕЗ авто-очистки через 8с.
        Используется ПОКА ИДЁТ процесс (генерация, загрузка) — сообщение
        должно висеть до явной замены, иначе юзер не видит прогресс.

        В MainWindow есть глобальный `_status_clear_timer` (8000мс) который
        перезапускается на каждом `messageChanged`. Стопаем его сразу
        после showMessage чтобы наше длинное сообщение не съели."""
        if not self.status_bar:
            return
        try:
            self.status_bar.showMessage(msg)
            mw = self.window()
            t = getattr(mw, '_status_clear_timer', None)
            if t is not None:
                t.stop()
        except Exception:
            traceback.print_exc()

    def _show_status_temp(self, msg: str, ms: int = 8000):
        """Финальное короткое сообщение, через `ms` мс пропадёт само.
        Для «✓ Готов», «✗ Ошибка» — юзер успеет увидеть результат
        и сообщение исчезнет."""
        if not self.status_bar:
            return
        try:
            self.status_bar.showMessage(msg, ms)
        except Exception:
            traceback.print_exc()

    def _on_card_size_changed(self, value: int):
        """Слайдер ширины карточек: сохраняем в QSettings и перерисовываем сетку.
        Перерисовка дешёвая — карточек обычно <30, превью в памяти."""
        self._card_width = int(value)
        try:
            QSettings(_sa.APP_ORG, _sa.APP_NAME).setValue(
                "actors_card_width", self._card_width)
        except Exception:
            pass
        try:
            self.size_value_lbl.setText(
                tr('actors_card_size_value', w=self._card_width))
        except Exception:
            pass
        self.refresh()

    def apply_lang(self):
        """Применить переводы. Админские лейблы могут быть None у коллег —
        проверяем перед setText."""
        try:
            if self.sec_admin_lbl is not None:
                self.sec_admin_lbl.setText(tr('actors_section_admin'))
            if self.drop_lbl is not None:
                self.drop_lbl.setText(tr('actors_drop_label'))
            if self.drop_hint is not None:
                self.drop_hint.setText(tr('actors_drop_hint'))
            if self.add_btn is not None:
                self.add_btn.setText(tr('actors_add_btn'))
            self.sec_list_lbl.setText(tr('actors_section_list'))
            self.empty_lbl.setText(tr('actors_empty'))
            if self.size_label_lbl is not None:
                self.size_label_lbl.setText(tr('actors_card_size_label'))
            if self.size_hint_lbl is not None:
                self.size_hint_lbl.setText(tr('actors_card_size_hint'))
            if self.size_value_lbl is not None:
                self.size_value_lbl.setText(
                    tr('actors_card_size_value', w=self._card_width))
            self.refresh()  # карточки тоже переведутся (кол-во фото)
        except Exception:
            traceback.print_exc()

    # ── Drag & Drop ──────────────────────────────────────────────────────────

    def dragEnterEvent(self, ev):
        if not self._is_admin:
            return
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            if self.drop_frame is not None:
                self.drop_frame.setStyleSheet(
                    "QFrame#actors-drop {"
                    " background: rgba(110,76,196,0.25);"
                    " border: 2px dashed rgba(190,150,255,0.85);"
                    " border-radius: 12px;"
                    "}")

    def dragMoveEvent(self, ev):
        # Без этого dropEvent НЕ сработает на macOS — drag-and-drop
        # требует чтобы dragMoveEvent тоже акцептил действие.
        if self._is_admin and ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        if self.drop_frame is not None:
            self.drop_frame.setStyleSheet(
                "QFrame#actors-drop {"
                " background: rgba(110,76,196,0.10);"
                " border: 2px dashed rgba(160,120,240,0.45);"
                " border-radius: 12px;"
                "}")

    def dropEvent(self, ev):
        self.dragLeaveEvent(ev)  # сброс стиля
        urls = ev.mimeData().urls() if ev.mimeData() else []
        files = [Path(u.toLocalFile()) for u in urls
                 if u.toLocalFile() and Path(u.toLocalFile()).is_file()]
        # Image-форматы. HEIC/HEIF — фото с iPhone, обрабатываются
        # add_photo_to_actor через `sips` (конвертация в jpg).
        exts = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
        files = [f for f in files if f.suffix.lower() in exts]
        if not files:
            return
        self._upload_to_actor(files)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _on_add_actor(self):
        dlg = AddActorDialog(parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.value()
        if not name:
            return
        try:
            slug = _sa.create_actor(self.project_root, name)
            self.refresh()
            if self.status_bar:
                self.status_bar.showMessage(f"✓ {name} → actors/{slug}/", 5000)
        except Exception as ex:
            traceback.print_exc()
            if self.status_bar:
                self.status_bar.showMessage(f"⚠ {ex}", 5000)

    def _on_rename_actor(self, slug: str):
        cur = _sa.actor_display_name(self.project_root, slug)
        dlg = AddActorDialog(parent=self.window(), current_name=cur)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dlg.value()
        if not new_name:
            return
        try:
            _sa.rename_actor(self.project_root, slug, new_name)
            self.refresh()
        except Exception:
            traceback.print_exc()

    def _on_delete_actor(self, slug: str):
        """Кнопка 🗑 на карточке актёра (admin-only). Показывает попап
        подтверждения, если юзер согласен — удаляет папку `actors/<slug>/`
        и запись из `actors.json`. Сгенерированные рефы в shows/*/refs/
        characters/ НЕ трогаем — это либо личная работа админа (он сам
        почистит), либо вообще не наша зона ответственности."""
        try:
            display = _sa.actor_display_name(self.project_root, slug)
            photos_count = len(_sa.get_actor_photos(self.project_root, slug))
            box = QMessageBox(self.window())
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr('delete_actor_title'))
            box.setText(tr('delete_actor_msg', name=display,
                           n=photos_count, slug=slug))
            yes_btn = box.addButton(tr('delete_actor_yes'),
                                    QMessageBox.ButtonRole.DestructiveRole)
            no_btn = box.addButton(tr('delete_actor_no'),
                                   QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no_btn)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
            _sa.delete_actor(self.project_root, slug)
            # Также чистим pending-стек для этого актёра (если был открыт
            # попап с pending-вариантами — закроется на следующем refresh,
            # потому что файлов больше нет)
            self._pending_variants.pop(slug, None)
            self._open_result_dialogs.pop(slug, None)
            self._last_outfit.pop(slug, None)
            self.refresh()
            if self.status_bar:
                self.status_bar.showMessage(
                    tr('delete_actor_done', name=display), 5000)
        except Exception:
            traceback.print_exc()

    def _upload_to_actor(self, files: List[Path]):
        """Спрашивает у юзера в какую папку положить, потом копирует."""
        dlg = ChooseActorDialog(self.project_root, len(files),
                                 parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target = dlg.selected_slug()
        if target is None:
            return
        # Если "Создать нового" — открываем диалог имени
        if target == ChooseActorDialog.NEW_SENTINEL:
            create_dlg = AddActorDialog(parent=self.window())
            if create_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            name = create_dlg.value()
            if not name:
                return
            try:
                target = _sa.create_actor(self.project_root, name)
            except Exception:
                traceback.print_exc()
                return
        # Копируем фото
        copied = 0
        for src in files:
            try:
                _sa.add_photo_to_actor(self.project_root, target, src)
                copied += 1
            except Exception:
                traceback.print_exc()
        self.refresh()
        if self.status_bar and copied:
            display = _sa.actor_display_name(self.project_root, target)
            self.status_bar.showMessage(
                tr('actors_loaded_status', n=copied, actor=display), 5000)


# ─── Этап 1: хранилище текстур (2026-05-17) ──────────────────────────────────
# Доступно ВСЕМ пользователям (не admin-only). Папка глобальная:
# `actors/_textures/` — рядом со слагами актёров, шарится между сериалами.
# Этап 1: только drag-drop загрузка + сетка превью + удаление + fullscreen.
# Этап 2 (позже): наложение на existing рефы. Этап 3: «Показать в папке».


class _TextureFullscreenDialog(QDialog):
    """Фуллскрин-просмотр текстуры. Клик в любую точку или Esc → закрыть.

    Pattern скопирован из FullscreenImageDialog (storyboard_app.py) — но
    локальный, чтобы не плодить зависимости. Сама картинка скейлится
    под доступную область экрана через KeepAspectRatio.
    """

    def __init__(self, image_path: Path, parent=None):
        super().__init__(parent)
        self._image_path = Path(image_path)
        # Frameless + modal на весь экран
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setStyleSheet(
            "QDialog { background:#000; }"
            "QLabel#tex-fs-img { background:#000; }"
            "QLabel#tex-fs-hint { color:rgba(255,255,255,0.55);"
            " font-size:12px; background:transparent;"
            " padding:8px; }")
        # Размер ~95% screen
        try:
            from PyQt6.QtWidgets import QApplication as _QApp
            geo = _QApp.primaryScreen().availableGeometry()
            self.resize(int(geo.width() * 0.95),
                        int(geo.height() * 0.95))
        except Exception:
            self.resize(1200, 800)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("tex-fs-img")
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        outer.addWidget(self._img_lbl, stretch=1)
        hint = QLabel(tr('actors_textures_fullscreen_hint'))
        hint.setObjectName("tex-fs-hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(hint)
        self._load_pixmap()

    def _load_pixmap(self):
        try:
            pix = QPixmap(str(self._image_path))
            if pix.isNull():
                return
            target_w = max(200, self.width() - 20)
            target_h = max(200, self.height() - 60)
            pix = pix.scaled(
                QSize(target_w, target_h),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._img_lbl.setPixmap(pix)
        except Exception:
            traceback.print_exc()

    def mousePressEvent(self, ev):
        # Клик в любую точку — закрыть.
        if ev.button() == Qt.MouseButton.LeftButton:
            self.accept()
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        # Esc — закрыть. (QDialog обычно сам обрабатывает Esc через
        # reject, но при FramelessWindowHint поведение нестабильно —
        # явный обработчик надёжнее.)
        if ev.key() == Qt.Key.Key_Escape:
            self.accept()
            return
        super().keyPressEvent(ev)

    def resizeEvent(self, ev):
        # При изменении размера окна пересчитываем pixmap чтобы
        # вписаться в новые габариты без обрезки.
        super().resizeEvent(ev)
        self._load_pixmap()


class _TextureThumb(QFrame):
    """Превью одной текстуры (90×90) + кнопка «🗑 Удалить» снизу.

    Сигналы:
      • clicked(Path)          — клик по превью → fullscreen-просмотр
      • delete_requested(Path) — клик «🗑 Удалить» (после confirm в parent)

    Pattern: вдохновлено `_PhotoThumb` в widgets/actor_dialogs.py, но
    конструкция отдельная (не наследование, не общий класс — Этап 1
    держит всё локально в views/actors.py чтобы не задеть actor_dialogs).
    """

    THUMB_SIZE = 90

    clicked = pyqtSignal(object)           # Path
    delete_requested = pyqtSignal(object)  # Path

    def __init__(self, file_path: Path, parent=None):
        super().__init__(parent)
        self._file_path = Path(file_path)
        self.setObjectName("texture-thumb")
        self.setStyleSheet(
            "QFrame#texture-thumb { background: transparent;"
            " border: none; }"
            "QLabel#texture-img { background:#1a1424;"
            " border:1px solid #2a1f3d; border-radius:6px; }"
            "QLabel#texture-img:hover { border-color:#6e4cc4; }"
            "QPushButton#texture-del { background:transparent;"
            " color:#c47878; border:1px solid #5a2c2c;"
            " border-radius:4px; padding:2px 6px; font-size:10px;"
            " font-weight:500; }"
            "QPushButton#texture-del:hover { color:#fff;"
            " border-color:#c4304c;"
            " background:rgba(196,48,76,0.10); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        # Превью
        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("texture-img")
        self._img_lbl.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        try:
            pix = QPixmap(str(self._file_path))
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(self.THUMB_SIZE - 2, self.THUMB_SIZE - 2),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self._img_lbl.setPixmap(pix)
        except Exception:
            pass
        # Клик по картинке → fullscreen
        self._img_lbl.mousePressEvent = self._on_img_clicked
        lay.addWidget(self._img_lbl,
                      alignment=Qt.AlignmentFlag.AlignCenter)
        # Кнопка удалить
        del_btn = QPushButton(tr('actors_textures_delete_btn'))
        del_btn.setObjectName("texture-del")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(self._on_delete_clicked)
        lay.addWidget(del_btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def _on_img_clicked(self, ev):
        try:
            if ev.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self._file_path)
        except Exception:
            traceback.print_exc()

    def _on_delete_clicked(self):
        try:
            self.delete_requested.emit(self._file_path)
        except Exception:
            traceback.print_exc()


class TexturesDropZone(QWidget):
    """Этап 1 хранилища текстур — drop-зона + сетка превью + удаление.

    Поведение:
      • drag-drop PNG/JPG/JPEG/WEBP — копируются в `actors/_textures/`
        (создаётся при первой загрузке через mkdir(parents=True,
        exist_ok=True)). Папка глобальная, шарится между сериалами.
      • collision: file already exists → суффикс `_2`, `_3`...
      • Превью 90×90 в сетке по 6 в ряд.
      • Клик по превью → fullscreen-просмотр (Esc/клик закрывает).
      • Кнопка «🗑 Удалить» под каждым превью с confirm.
      • Доступно ВСЕМ пользователям (не admin-only).

    drop-handlers скопированы pattern из ActorsView (1712-1751), но
    цель — `actors/_textures/`, а не `_upload_to_actor`. Существующие
    handlers ActorsView НЕ тронуты — они обрабатывают drop в зону
    фоток актёра, наша зона работает только когда курсор над её
    QFrame (отдельный widget = свой event-target).
    """

    THUMBS_PER_ROW = 6

    def __init__(self, project_root: Path, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.setAcceptDrops(True)
        self._build()
        self.refresh()

    def textures_dir(self) -> Path:
        """Путь к глобальной папке текстур — `actors/_textures/`.
        Создание делается лениво в _save_textures.
        """
        return self.project_root / "actors" / "_textures"

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # Заголовок секции — стиль "settings-section" как у других
        # секций ActorsView (sec_admin_lbl / sec_list_lbl).
        self._section_lbl = QLabel(tr('actors_section_textures'))
        self._section_lbl.setObjectName("settings-section")
        outer.addWidget(self._section_lbl)

        # Drop-зона (стиль аналогично actors-drop, но свой objectName)
        self._drop_frame = QFrame()
        self._drop_frame.setObjectName("textures-drop")
        self._drop_frame.setMinimumHeight(90)
        self._apply_drop_style(False)
        df_lay = QVBoxLayout(self._drop_frame)
        df_lay.setContentsMargins(20, 14, 20, 14)
        df_lay.setSpacing(4)
        df_lay.addStretch()
        self._drop_lbl = QLabel(tr('actors_textures_drop_label'))
        self._drop_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_lbl.setStyleSheet(
            "color:#d8c8ff; font-size:13px; font-weight:600;"
            " background:transparent;")
        df_lay.addWidget(self._drop_lbl)
        self._drop_hint = QLabel(tr('actors_textures_drop_hint'))
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setStyleSheet(
            "color:#aaa; font-size:11px; background:transparent;")
        df_lay.addWidget(self._drop_hint)
        df_lay.addStretch()
        outer.addWidget(self._drop_frame)

        # «Пусто» / сетка превью
        self._empty_lbl = QLabel(tr('actors_textures_empty'))
        self._empty_lbl.setStyleSheet(
            "color:#888; font-size:12px; font-style:italic; padding:8px;")
        self._empty_lbl.hide()
        outer.addWidget(self._empty_lbl)

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(10)
        self._grid.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._grid_widget)

    def _apply_drop_style(self, hover: bool):
        if hover:
            self._drop_frame.setStyleSheet(
                "QFrame#textures-drop {"
                " background: rgba(110,76,196,0.25);"
                " border: 2px dashed rgba(190,150,255,0.85);"
                " border-radius: 12px; }")
        else:
            self._drop_frame.setStyleSheet(
                "QFrame#textures-drop {"
                " background: rgba(110,76,196,0.10);"
                " border: 2px dashed rgba(160,120,240,0.45);"
                " border-radius: 12px; }")

    def refresh(self):
        """Сканирует actors/_textures/ и перерисовывает сетку превью."""
        # Чистим текущую сетку
        try:
            while self._grid.count():
                item = self._grid.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
        except Exception:
            traceback.print_exc()

        tex_dir = self.textures_dir()
        files: List[Path] = []
        try:
            if tex_dir.is_dir():
                exts = {".png", ".jpg", ".jpeg", ".webp"}
                files = sorted([
                    p for p in tex_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in exts
                ], key=lambda p: p.name.lower())
        except Exception:
            traceback.print_exc()
            files = []

        if not files:
            self._empty_lbl.show()
            self._grid_widget.hide()
            return
        self._empty_lbl.hide()
        self._grid_widget.show()

        cols = self.THUMBS_PER_ROW
        for i, fp in enumerate(files):
            thumb = _TextureThumb(fp, parent=self._grid_widget)
            thumb.clicked.connect(self._on_thumb_clicked)
            thumb.delete_requested.connect(self._on_thumb_delete)
            r, c = divmod(i, cols)
            self._grid.addWidget(thumb, r, c)
        # Distribute the unused tail to the right
        for c in range(cols):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(cols, 1)

    # ── drop-handlers (скопирован pattern из ActorsView 1712-1751) ───────

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self._apply_drop_style(True)

    def dragMoveEvent(self, ev):
        # Без этого dropEvent не сработает на macOS.
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self._apply_drop_style(False)

    def dropEvent(self, ev):
        self._apply_drop_style(False)
        try:
            urls = ev.mimeData().urls() if ev.mimeData() else []
            files = [Path(u.toLocalFile()) for u in urls
                     if u.toLocalFile() and Path(u.toLocalFile()).is_file()]
            exts = {".png", ".jpg", ".jpeg", ".webp"}
            files = [f for f in files if f.suffix.lower() in exts]
            if not files:
                return
            self._save_textures(files)
        except Exception:
            traceback.print_exc()

    # ── file ops ─────────────────────────────────────────────────────────

    def _save_textures(self, files: List[Path]) -> None:
        """Копирует файлы в `actors/_textures/`. На collision добавляет
        суффикс _2, _3... Файл-источник не трогаем. После — refresh."""
        import shutil
        tex_dir = self.textures_dir()
        try:
            tex_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            traceback.print_exc()
            return
        for src in files:
            try:
                dst = tex_dir / src.name
                if dst.exists():
                    stem, ext = src.stem, src.suffix
                    i = 2
                    while (tex_dir / f"{stem}_{i}{ext}").exists():
                        i += 1
                    dst = tex_dir / f"{stem}_{i}{ext}"
                shutil.copy2(str(src), str(dst))
            except Exception:
                traceback.print_exc()
        self.refresh()

    def _on_thumb_clicked(self, file_path):
        """Клик по превью → fullscreen-просмотр."""
        try:
            dlg = _TextureFullscreenDialog(Path(file_path), parent=self)
            dlg.exec()
        except Exception:
            traceback.print_exc()

    def _on_thumb_delete(self, file_path):
        """Клик «🗑 Удалить» под превью — confirm + unlink + refresh."""
        try:
            p = Path(file_path)
            ans = QMessageBox.question(
                self,
                tr('actors_textures_delete_btn'),
                tr('actors_textures_delete_confirm', name=p.name),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                p.unlink(missing_ok=True)
            except Exception:
                traceback.print_exc()
                return
            self.refresh()
        except Exception:
            traceback.print_exc()

    def apply_lang(self):
        """Перевод при смене языка (вызывается из ActorsView.apply_lang
        если потребуется в будущем — сейчас не подключено, текст обновится
        при следующем refresh)."""
        try:
            self._section_lbl.setText(tr('actors_section_textures'))
            self._drop_lbl.setText(tr('actors_textures_drop_label'))
            self._drop_hint.setText(tr('actors_textures_drop_hint'))
            self._empty_lbl.setText(tr('actors_textures_empty'))
        except Exception:
            traceback.print_exc()
