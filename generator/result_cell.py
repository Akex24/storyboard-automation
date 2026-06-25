# -*- coding: utf-8 -*-
"""
generator/result_cell.py — ячейка сетки результатов «Генератора» (2026-06-20).

Состояния:
  • loading — дышащая плитка (пульсация яркости базы по sin(angle), бесшовно) +
              статичный вертикальный градиент для объёма + тёплая точка в углу +
              ПЕРСОНАЛЬНЫЙ счётчик секунд «12с» (как overlay на шотах/актёрах — см.
              ActorCard.start_progress / _tick_progress в views/actors.py: локальный
              QTimer(1000) + time.time()).
  • image   — готовая картинка (QPixmap.scaled под размер ячейки).
  • error   — тёмно-красная плитка + ТЕКСТ ПРИЧИНЫ (wordwrap), без падения.

Два РАЗНЫХ такта (по требованию):
  • счётчик секунд — СВОЙ QTimer(1000мс) на каждую ячейку (генерации параллельные,
    у каждой своё время);
  • дыхание яркости — ОБЩИЙ угол на страницу (GeneratorPage гонит фазу адаптивно
    7–15fps и зовёт set_phase(angle_rad)), чтобы не плодить N анимаций на
    слабых Win-машинах. Все плитки дышат СИНХРОННО — выглядит как единый
    «живой ансамбль», а не как N независимых лоадеров.

Самодостаточный (PyQt6 + time). Без subprocess/IO → cross-platform тривиально.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QRectF, QSize
from PyQt6.QtGui import (QPainter, QPainterPath, QLinearGradient, QColor, QPixmap,
                         QFont)
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QToolButton, QWidget


# Модуль-уровневые константы — не пересоздаём в paintEvent (дёшево для CPU).
# 2026-06-20 (Этап 3): отказались от бегущего блика (любая «беговая» полоса
# даёт слепые зоны/wrap-артефакты). Теперь — ЧИСТАЯ ПУЛЬСАЦИЯ яркости базы
# по синусоиде (бесшовна МАТЕМАТИЧЕСКИ) + статичный вертикальный градиент
# для объёма + лёгкая тёплая статичная точка в верх-левом углу для нюанса.
# Никаких движущихся элементов → «слепых зон» нет, рывок невозможен.
_BORDER_COLOR = QColor(42, 31, 61)         # #2a1f3d — мягкая рамка
_ERR_BASE = QColor(42, 20, 20)             # #2a1414 — тёмно-красная база ошибки
_ERR_BORDER = QColor(138, 77, 77)
# «Дыхание» базы: цвет колеблется между _BASE_DARK и _BASE_LIGHT по sin(angle).
# Амплитуда ~10% яркости — деликатно, премиально.
_BASE_DARK_R, _BASE_DARK_G, _BASE_DARK_B = 20, 15, 30      # ≈ #14 0F 1E
_BASE_LIGHT_R, _BASE_LIGHT_G, _BASE_LIGHT_B = 32, 24, 46   # ≈ #20 18 2E
# Статичные слои объёма — НЕ зависят от фазы, можно держать как константы.
_DEPTH_TOP = QColor(255, 255, 255, 14)     # лёгкая «подсветка» сверху
_DEPTH_BOTTOM = QColor(0, 0, 0, 18)        # лёгкая «тень» снизу
_WARM_ACCENT_INNER = QColor(212, 162, 86, 26)   # янтарь (как кнопка запуска) — низкая α
_WARM_ACCENT_OUTER = QColor(212, 162, 86, 0)    # затухание к 0
# Для статичного фолбэка под image-letterbox (отдельный «нейтральный» цвет).
_BASE_COLOR = QColor(22, 16, 32)           # #161020 — используется в letterbox

# Полоска-прогресс видео: гориз. вставка от краёв (≥ радиус скругления 8px →
# концы не на углах) и подъём от самого низа (чище визуально, не липнет к кромке).
_PROG_INSET = 8
_PROG_LIFT = 2

# QSS для hover-оверлея с 4 действиями (heart/image-plus/corner-up-left/trash-2-red).
# QToolButton-кнопки с прозрачным фоном, скруглением 8px, тёмной подложкой и тонкой
# светлой рамкой; hover — чуть светлее. У trash рамка слегка красная (rgba), hover
# не меняется (visual cue «опасное действие»). Иконки SVG идут как QIcon — цвет
# зашит в SVG (Lucide stroke), QSS его не перекрашивает.
_ACTIONS_OVERLAY_QSS = (
    "QToolButton {"
    " background: rgba(20,20,24,0.72);"
    " border: 1px solid rgba(255,255,255,0.18);"
    " border-radius: 8px;"
    " padding: 0px;"
    "}"
    "QToolButton:hover {"
    " background: rgba(35,35,40,0.85);"
    "}"
    "QToolButton#cell-act-trash {"
    " border: 1px solid rgba(232,75,74,0.35);"
    "}"
    "QToolButton#cell-act-trash:hover {"
    " background: rgba(20,20,24,0.72);"
    "}"
    # 2026-06-25 (апскейл): «2K» — золотой LUMZ accent_gold #d4a256, текстом без иконки.
    "QToolButton#cell-act-2k {"
    " background: rgba(212,162,86,0.18);"
    " border: 1px solid rgba(212,162,86,0.55);"
    " color: #f3d28a;"
    " font-size: 11px; font-weight: 700;"
    "}"
    "QToolButton#cell-act-2k:hover {"
    " background: rgba(212,162,86,0.32);"
    " border-color: rgba(212,162,86,0.78);"
    " color: #fff3c4;"
    "}"
    "QToolButton#cell-act-2k:disabled {"
    " background: rgba(212,162,86,0.06);"
    " border: 1px solid rgba(212,162,86,0.18);"
    " color: rgba(212,162,86,0.45);"
    "}"
    "QToolButton:disabled {"
    " background: rgba(20,20,24,0.40);"
    " border: 1px solid rgba(255,255,255,0.08);"
    "}"
)


class ShimmerCell(QFrame):
    """Плитка результата. Создаётся в loading; page — для (un)register общего shimmer."""

    def __init__(self, page, width: int = 480, height: int = 270,
                 aspect: str = "16:9", parent: Optional[QFrame] = None):
        super().__init__(parent)
        self._page = page
        self._w, self._h = width, height
        self._aspect = aspect        # формат плитки ("16:9"/"9:16") — для перераскладки
        self.setFixedSize(width, height)
        self._state = "loading"      # loading | image | video | error
        self._angle = 0.0            # фаза в радианах [0, 2π) — ставит общий таймер страницы
        self._original_pix = None    # оригинал картинки (для перемасштаба при смене размера)
        self._pixmap = None          # масштабированная под ячейку (рисуется в paintEvent)
        self._model_label = ""       # читаемое имя модели — бейдж поверх картинки (UI-only)
        self._video_path = None      # путь к .mp4 (state "video"); кадр-превью — позже (cv2)
        self._result_path = None     # абсолютный путь к ГОТОВОМУ файлу (image .jpg / video .mp4)
                                     # — для reveal-кнопки (показать в Finder/Explorer)
        # hover-автоплей видео (ЛЕНИВО — плеер создаётся на ПЕРВОМ hover видео-
        # плитки, не в __init__: плиток много). Вывод — НЕ нативный QVideoWidget,
        # а QVideoSink: кадры рисуем в paintEvent (клип/скругление/клип-вьюпорта
        # «бесплатно», кнопки-оверлеи всегда поверх, без нативного z-order).
        # _mm_ok — tri-state: None=не пробовали импорт, True=QtMultimedia, False=нет.
        self._player = None
        self._audio = None
        self._video_sink = None
        self._mm_ok = None
        self._video_active = False     # hover хочет воспроизведение (гейт кадров/старта)
        self._video_playing = False    # пришёл ≥1 реальный кадр → рисуем видео (анти-мерцание)
        self._last_video_frame = None  # последний QImage от sink (рисуется в paintEvent)
        self._meta = {}              # метаданные плитки (prompt/model_id/model_label/aspect/
                                     # type/file/ts) — in-memory; на диск тут НЕ пишется

        v = QVBoxLayout(self)
        # Поля для ТЕКСТА (loading «{n}с» / error-причина). Картинка рисуется
        # full-bleed в paintEvent и эти поля игнорирует (как Flow — без подложки).
        v.setContentsMargins(10, 10, 10, 10)
        v.addStretch()
        # _info_lbl: loading → «{n}с», error → текст причины. БЕЗ alignment в addWidget —
        # иначе label сжимается до sizeHint и wordWrap не срабатывает (текст ошибки
        # обрезался). Заполняет ширину → перенос работает; setAlignment центрирует текст.
        self._info_lbl = QLabel("0с")
        self._info_lbl.setWordWrap(True)
        self._info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_lbl.setStyleSheet(
            "color:#cfcfda; font-size:13px; background:transparent;")
        v.addWidget(self._info_lbl)
        v.addStretch()

        # ── персональный счётчик секунд (паттерн ActorCard, views/actors.py:312) ──
        self._t0 = time.time()
        # 2026-06-25 (апскейл): если внешний код выставил текст через
        # set_loading_text — _tick_seconds НЕ перезаписывает _info_lbl до
        # _finish_common (там сбрасываем). Иначе «Скачиваю движок: 24%»
        # мигало бы с секундным счётчиком «12с» каждый тик.
        self._loading_text_external = False
        self._sec_timer = QTimer(self)
        self._sec_timer.setInterval(1000)
        self._sec_timer.timeout.connect(self._tick_seconds)
        self._sec_timer.start()

        # регистрируемся в общем shimmer-такте страницы
        try:
            self._page.register_loading(self)
        except Exception:
            pass

        # ── hover-оверлей: ряд из 4 кнопок справа сверху (КАРКАС, клики НЕ
        # подключены — оживление по одной отдельными шагами). Появляется в
        # enterEvent, скрывается в leaveEvent. Кнопки — реальные QToolButton
        # (painter не годится: ему не приходят клики и hover). Иконки —
        # get_icon (Lucide SVG); если пусто → кнопка без картинки, не падаем.
        self._actions_overlay = QWidget(self)
        self._actions_overlay.setObjectName("cell-actions")
        self._actions_overlay.setStyleSheet(_ACTIONS_OVERLAY_QSS)
        self._actions_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        # 2026-06-25 (апскейл): структура overlay'ев:
        #   16:9 — _actions_overlay становится VBox с двумя HBox-строками:
        #     верхняя: heart → image-plus → corner-up-left → trash
        #     нижняя: btn_2k под корзиной (с растяжкой слева)
        #     _aux_2k_overlay (спутник для 9:16) НЕ создаётся.
        #   9:16 — _actions_overlay остаётся VBox (4 иконки сверху вниз),
        #     btn_2k — в ОТДЕЛЬНОМ _aux_2k_overlay слева от верхней (корзины).
        # _aspect задаётся при __init__ и не меняется.
        # Ленивый импорт get_icon — паттерн generator_page._icon (избегает
        # circular import + frozen-проблем). Пустой QIcon → кнопка без картинки.
        try:
            from storyboard_app import get_icon as _get_icon
        except Exception:
            _get_icon = lambda _n: None   # noqa: E731
        def _mk_btn(icon_name: str, obj_name: str = "",
                    parent_overlay: QWidget = None) -> QToolButton:
            b = QToolButton(parent_overlay or self._actions_overlay)
            if obj_name:
                b.setObjectName(obj_name)
            b.setFixedSize(28, 28)
            b.setIconSize(QSize(16, 16))
            ic = _get_icon(icon_name)
            if ic is not None:
                b.setIcon(ic)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        self.btn_heart = _mk_btn("heart")
        self.btn_ref   = _mk_btn("image-plus")
        self.btn_back  = _mk_btn("corner-up-left")
        self.btn_trash = _mk_btn("trash-2-red", obj_name="cell-act-trash")
        self.btn_2k    = _mk_btn("", obj_name="cell-act-2k")
        self.btn_2k.setText("2K")
        self.btn_2k.setIconSize(QSize(0, 0))
        self.btn_2k.setToolTip("Улучшить качество ×2")
        # Спутник для 9:16: РОДИТЕЛЬ — сама плитка, не _actions_overlay (его
        # позиция считается в углу). _aux_2k_overlay позиционируется отдельно.
        self._aux_2k_overlay = None
        if self._aspect == "9:16":
            # _aux_2k_overlay — отдельный QWidget на плитке, слева от корзины.
            self._aux_2k_overlay = QWidget(self)
            self._aux_2k_overlay.setObjectName("cell-actions")
            self._aux_2k_overlay.setStyleSheet(_ACTIONS_OVERLAY_QSS)
            self._aux_2k_overlay.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            _aux_h = QHBoxLayout(self._aux_2k_overlay)
            _aux_h.setContentsMargins(0, 0, 0, 0)
            _aux_h.setSpacing(6)
            # Меняем родителя кнопки на спутник (была привязана к _actions_overlay
            # в _mk_btn по умолчанию). setParent сохраняет состояние.
            self.btn_2k.setParent(self._aux_2k_overlay)
            _aux_h.addWidget(self.btn_2k)
            self._aux_2k_overlay.hide()
            # _actions_overlay — VBox 4 иконки (без btn_2k).
            ah = QVBoxLayout(self._actions_overlay)
            ah.setContentsMargins(0, 0, 0, 0)
            ah.setSpacing(6)
            # 9:16 — сверху вниз: trash → back → ref → heart.
            for _b in (self.btn_trash, self.btn_back, self.btn_ref, self.btn_heart):
                ah.addWidget(_b)
        else:
            # 16:9 — _actions_overlay = VBox{ HBox{heart,ref,back,trash}, HBox{spacer,btn_2k} }.
            ah = QVBoxLayout(self._actions_overlay)
            ah.setContentsMargins(0, 0, 0, 0)
            ah.setSpacing(6)
            _top = QHBoxLayout()
            _top.setContentsMargins(0, 0, 0, 0)
            _top.setSpacing(6)
            for _b in (self.btn_heart, self.btn_ref, self.btn_back, self.btn_trash):
                _top.addWidget(_b)
            _bot = QHBoxLayout()
            _bot.setContentsMargins(0, 0, 0, 0)
            _bot.setSpacing(6)
            _bot.addStretch()
            _bot.addWidget(self.btn_2k)
            ah.addLayout(_top)
            ah.addLayout(_bot)
        # btn_back / btn_ref / btn_trash / btn_2k оживлены; heart пока пустая.
        self.btn_back.clicked.connect(self._on_back_clicked)
        self.btn_ref.clicked.connect(self._on_ref_clicked)
        self.btn_trash.clicked.connect(self._on_trash_clicked)
        self.btn_2k.clicked.connect(self._on_2k_clicked)
        self._refresh_back_enabled()   # начальное состояние от текущего _meta
        self._refresh_ref_enabled()    # btn_ref активна, когда есть meta.file
        self._refresh_trash_enabled()  # trash активна, когда есть готовый файл
        self._refresh_2k_enabled()     # 2k активна только когда meta.type=='image' + есть файл
        self._actions_overlay.hide()
        self._position_actions_overlay()
        if self._aux_2k_overlay is not None:
            self._position_aux_2k_overlay()

        # ── ЛЕВЫЙ overlay: одна кнопка «показать в Finder/Explorer» (reveal).
        # ОТДЕЛЬНЫЙ виджет (не в правом кластере heart/ref/back/trash): прижат в
        # ЛЕВЫЙ-верхний угол. Виден только на hover И только когда файл реально
        # есть на диске (_refresh_reveal_enabled). Тот же _mk_btn-стиль (28×28,
        # Lucide-иконка folder-open). _mk_btn ещё в scope этого __init__.
        self._left_overlay = QWidget(self)
        self._left_overlay.setObjectName("cell-actions")
        self._left_overlay.setStyleSheet(_ACTIONS_OVERLAY_QSS)
        self._left_overlay.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        _lh = QHBoxLayout(self._left_overlay)
        _lh.setContentsMargins(0, 0, 0, 0)
        _lh.setSpacing(6)
        self.btn_reveal = _mk_btn("folder-open")
        _lh.addWidget(self.btn_reveal)
        self.btn_reveal.clicked.connect(self._on_reveal_clicked)
        self._left_overlay.hide()
        self._position_left_overlay()
        self._refresh_reveal_enabled()   # стартово файла нет (loading) → скрыта

    # ── счётчик секунд ──────────────────────────────────────────────────
    def _tick_seconds(self):
        if self._state != "loading":
            return
        # 2026-06-25 (апскейл): не топчем внешний прогресс (см. set_loading_text).
        if getattr(self, "_loading_text_external", False):
            return
        elapsed = max(0, int(time.time() - self._t0))
        self._info_lbl.setText(f"{elapsed}с")

    # ── общий shimmer-такт (зовёт страница) ─────────────────────────────
    def set_phase(self, angle_rad: float):
        """Общий угол фазы (в радианах) для бесшовного градиентного перелива.
        Зовёт GeneratorPage из единого таймера. Не тригерит update() в не-loading."""
        if self._state != "loading":
            return
        self._angle = angle_rad
        self.update()

    # ── завершение ──────────────────────────────────────────────────────
    def _finish_common(self):
        try:
            self._sec_timer.stop()
        except Exception:
            pass
        # 2026-06-25 (апскейл): освободить флаг внешнего текста — на случай
        # если плитку позже переиспользуют под другую загрузку.
        self._loading_text_external = False
        try:
            self._page.unregister_loading(self)
        except Exception:
            pass

    def set_image(self, path: str):
        self._finish_common()
        pix = QPixmap(path)
        if pix.isNull():
            self.set_error("Не удалось открыть результат")
            return
        self._state = "image"
        self._result_path = path     # абсолютный путь готового файла → reveal-кнопка
        self._original_pix = pix     # оригинал — для перемасштаба при смене размера
        self._info_lbl.hide()
        self._rescale_pixmap()
        self._refresh_reveal_enabled()
        self._refresh_2k_enabled()
        self.update()

    def set_video_placeholder(self, path: str):
        """Готовое ВИДЕО: останавливаем shimmer/счётчик. Если видео-поток положил
        рядом кадр-превью gen_*.jpg (то же имя, .jpg) — грузим его фоном плитки через
        QPixmap (Qt понимает не-ASCII пути → отображение надёжно на Windows). Нет .jpg
        (не-ASCII путь и cv2 не смог) → тёмный фон + ▶. ▶ рисуется поверх в любом случае."""
        self._finish_common()
        self._state = "video"
        self._video_path = path
        self._result_path = path     # абсолютный путь .mp4 → reveal-кнопка
        self._info_lbl.hide()
        # Кадр-превью рядом с .mp4: то же имя, расширение .jpg. Чтение через Qt —
        # кириллица в пути не мешает (в отличие от cv2 на стороне видео-потока).
        try:
            from pathlib import Path
            jpg = str(Path(path).with_suffix(".jpg"))
            pix = QPixmap(jpg)
            if not pix.isNull():
                self._original_pix = pix
                self._rescale_pixmap()
        except Exception:
            pass
        self._refresh_reveal_enabled()
        self.update()

    def set_model_label(self, text: str):
        """Читаемое имя модели для бейджа в левом нижнем углу плитки. Рисуется
        ПОВЕРХ плитки (UI-only, в файл НЕ вшивается) — и на loading, и на image
        (не на error). Виден сразу при старте генерации."""
        self._model_label = (text or "").strip()
        if self._state in ("image", "loading"):
            self.update()

    def aspect(self) -> str:
        """Формат плитки ("16:9"/"9:16") — сетка берёт его для пересчёта размера."""
        return self._aspect

    def set_meta(self, **kwargs):
        """Обновить метаданные плитки (in-memory, на диск тут НЕ пишется). Поля
        заполняет GeneratorPage: _on_run при создании (prompt/model_id/model_label/
        aspect/type), _on_gen_done по факту файла (file/ts). Для будущей персистенции."""
        self._meta.update(kwargs)
        # meta мог получить/изменить prompt → обновить enabled-состояние btn_back
        # ("вернуть промпт"). guard: btn_back может быть ещё не создана если
        # set_meta зовётся очень рано (защитимся). Аналогично btn_ref ниже.
        # ДВА отдельных try/except → падение одной кнопки не глушит другую.
        try:
            self._refresh_back_enabled()
        except Exception:
            pass
        try:
            self._refresh_ref_enabled()
        except Exception:
            pass
        try:
            self._refresh_trash_enabled()
        except Exception:
            pass
        try:
            self._refresh_2k_enabled()
        except Exception:
            pass

    def meta(self) -> dict:
        """Текущие метаданные плитки (словарь). Источник для будущего сохранения холста."""
        return self._meta

    def set_size(self, width: int, height: int):
        """Изменить размер ячейки (перераскладка сетки 2/3/4 колонки). Состояние,
        счётчик секунд и дыхание сохраняются; картинка перемасштабируется из оригинала."""
        self._w, self._h = width, height
        self.setFixedSize(width, height)
        if self._state in ("image", "video") and self._original_pix is not None:
            self._rescale_pixmap()
        # Перепозиционировать hover-оверлеи под новый размер плитки.
        self._position_actions_overlay()
        self._position_left_overlay()
        self._position_aux_2k_overlay()
        self.update()

    def _rescale_pixmap(self):
        """Масштаб оригинала под ячейку с ЗАПОЛНЕНИЕМ (ByExpanding) — без тёмных
        полей; лишнее обрежет clip скруглённого прямоугольника в paintEvent."""
        if self._original_pix is None:
            return
        self._pixmap = self._original_pix.scaled(
            self._w, self._h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)

    # ── hover-оверлей: позиционирование и показ/скрытие ────────────────
    def _position_actions_overlay(self):
        """Поставить ряд из 4 кнопок в правый-верхний угол плитки. Зовётся в
        __init__ (один раз) и в set_size (при S/M/L / ресайзе окна)."""
        ov = getattr(self, "_actions_overlay", None)
        if ov is None:
            return
        ov.adjustSize()
        x = max(0, self._w - ov.width() - 8)
        y = 8
        ov.move(x, y)

    def _position_left_overlay(self):
        """Поставить reveal-кнопку в ЛЕВЫЙ-верхний угол плитки (x=8, y=8). Зовётся
        в __init__ и в set_size — симметрично _position_actions_overlay."""
        ov = getattr(self, "_left_overlay", None)
        if ov is None:
            return
        ov.adjustSize()
        ov.move(8, 8)

    def _position_aux_2k_overlay(self):
        """2026-06-25 (апскейл, 9:16): спутник с btn_2k — СЛЕВА от верхней кнопки
        основного overlay (= слева от корзины в 9:16). Зовётся в __init__ и в
        set_size (симметрично _position_actions_overlay)."""
        ov = getattr(self, "_aux_2k_overlay", None)
        if ov is None:
            return
        main = getattr(self, "_actions_overlay", None)
        if main is None:
            return
        main.adjustSize()
        ov.adjustSize()
        # _actions_overlay в углу: x=card_w - mw - 8, y=8. Спутник x=main.x - ov.w - 6.
        main_x = max(0, self._w - main.width() - 8)
        x = max(0, main_x - ov.width() - 6)
        y = 8
        ov.move(x, y)

    # ── hover-автоплей видео (QVideoSink → кадры рисуем в paintEvent) ──────
    def _ensure_player(self) -> bool:
        """ЛЕНИВО создать QMediaPlayer+QAudioOutput+QVideoSink для ЭТОЙ плитки на
        первом hover видео. Вывод — В QVideoSink (НЕ нативный QVideoWidget): кадры
        ловим в _on_video_frame и рисуем в paintEvent. setSource — ОДИН раз тут
        (а не на каждый hover → меньше пересборок ffmpeg-рендерера). True если
        медиа-стек готов; False если QtMultimedia недоступен (frozen без модуля) →
        автоплей тихо отключается, плитка живёт как раньше (превью-кадр+▶).

        _mm_ok кэширует исход импорта (не дёргаем import на каждый hover). Ленивый
        импорт QtMultimedia — паттерн get_icon (circular/frozen guard)."""
        if self._mm_ok is False:
            return False
        if self._player is not None:
            return True
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
            from PyQt6.QtCore import QUrl
        except Exception as e:
            self._mm_ok = False
            print(f"[multimedia] player import FAILED on tile: {e}")
            return False
        try:
            self._video_sink = QVideoSink(self)
            self._audio = QAudioOutput(self)        # звук ВКЛ (как Google Flow)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self._video_sink)   # sink, не виджет
            self._video_sink.videoFrameChanged.connect(self._on_video_frame)
            self._player.positionChanged.connect(self._on_video_position)
            # setSource ОДИН раз — на hover только setPosition(0)+play().
            self._player.setSource(QUrl.fromLocalFile(self._video_path))
            self._mm_ok = True
            return True
        except Exception as e:
            self._mm_ok = False
            print(f"[multimedia] player create FAILED on tile: {e}")
            return False

    def _on_video_frame(self, frame):
        """QVideoSink.videoFrameChanged: первый РЕАЛЬНЫЙ кадр → _video_playing=True
        (анти-мерцание: до первого кадра paintEvent рисует превью+▶, не черноту).
        .copy() — отвязать QImage от mapped-буфера кадра (иначе данные протухнут).
        Гейт _video_active: поздний кадр после leave/stop игнорируем."""
        if not self._video_active:
            return
        try:
            img = frame.toImage()
            if img is not None and not img.isNull():
                self._last_video_frame = img.copy()
                self._video_playing = True
                self.update()
        except Exception:
            pass

    def _on_video_position(self, _pos: int):
        """positionChanged → перерисовать плитку, чтобы полоска-прогресс (рисуется
        в paintEvent из player.position()/duration()) двигалась. Только когда уже
        идёт показ видео."""
        if self._video_playing:
            self.update()

    def _stop_video_playback(self):
        """Стоп воспроизведения + звука, сброс кадра/флагов → paintEvent
        возвращается к превью+▶. Поздний кадр после этого игнорится
        (_video_active=False). Плеер/источник НЕ уничтожаем — реюз на след. hover."""
        self._video_active = False
        self._video_playing = False
        self._last_video_frame = None
        if self._player is not None:
            try:
                self._player.stop()       # стоп видео И звука (no audio leak)
            except Exception:
                pass
        self.update()

    def _draw_progress(self, p: QPainter):
        """Полоска-прогресс поверх кадра: тонкая (3px) у нижнего края, с гориз.
        вставками _PROG_INSET (≥ радиус скругления → концы не на углах) и подъёмом
        _PROG_LIFT. Ширина = доступная * position/duration."""
        pl = getattr(self, "_player", None)
        if pl is None:
            return
        try:
            dur = pl.duration()
            if dur and dur > 0:
                frac = max(0.0, min(1.0, pl.position() / dur))
                avail = max(0, self.width() - 2 * _PROG_INSET)
                p.fillRect(QRectF(_PROG_INSET, self.height() - 3 - _PROG_LIFT,
                                  avail * frac, 3),
                           QColor(255, 255, 255, 217))   # ≈ rgba(255,255,255,0.85)
        except Exception:
            pass

    def _refresh_reveal_enabled(self):
        """reveal-кнопка активна только когда _result_path указывает на реально
        существующий файл (готовый результат). На loading/error файла нет → флаг
        False, кнопка не показывается в enterEvent. Зовётся из set_image /
        set_video_placeholder и __init__."""
        ok = False
        try:
            from pathlib import Path
            ok = bool(self._result_path) and Path(self._result_path).exists()
        except Exception:
            ok = False
        self._reveal_ok = ok
        btn = getattr(self, "btn_reveal", None)
        if btn is not None:
            btn.setEnabled(ok)

    def _on_reveal_clicked(self):
        """Показать готовый файл в Finder/Explorer (reveal-and-select). Делегирует
        кросс-платформенному storyboard_app.reveal_in_file_manager. Ленивый импорт
        (circular-import / frozen guard, как get_icon). Любая ошибка — тихий выход
        (UI-удобство, не критично)."""
        path = getattr(self, "_result_path", None)
        if not path:
            return
        try:
            from storyboard_app import reveal_in_file_manager
            reveal_in_file_manager(path)
        except Exception:
            pass

    def enterEvent(self, ev):
        super().enterEvent(ev)
        ov = getattr(self, "_actions_overlay", None)
        if ov is not None:
            ov.show()
            ov.raise_()
        # ЛЕВЫЙ overlay (reveal) — только если файл реально есть (_reveal_ok).
        lov = getattr(self, "_left_overlay", None)
        if lov is not None and getattr(self, "_reveal_ok", False):
            lov.show()
            lov.raise_()
        # 2026-06-25 (апскейл): спутник 2K (только в 9:16). На hover — поверх,
        # только если btn_2k enabled (image + есть file).
        aux2k = getattr(self, "_aux_2k_overlay", None)
        if aux2k is not None and self.btn_2k.isEnabled():
            aux2k.show()
            aux2k.raise_()
        # ── hover-автоплей видео (кадры через QVideoSink → paintEvent) ──
        # Кнопки-оверлеи показаны ВЫШЕ и от видео НЕ зависят (видео = отрисовка в
        # paintEvent, кнопки — дочерние QWidget поверх). Гейта видимости больше нет:
        # кадры рисуются в paintEvent и клипаются скролл-областью сами (не вылезают
        # за шапку). Плеер ленивый; QtMultimedia недоступен → тихо пропускаем.
        if self._state == "video" and self._video_path and self._ensure_player():
            try:
                self._video_active = True            # hover хочет воспроизведение
                self._video_playing = False          # ждём первый реальный кадр (анти-мерцание)
                self._last_video_frame = None
                self._player.setPosition(0)          # КАЖДЫЙ hover — с начала
                self._player.play()
            except Exception as e:
                print(f"[multimedia] play FAILED on tile: {e}")

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        ov = getattr(self, "_actions_overlay", None)
        if ov is not None:
            ov.hide()
        lov = getattr(self, "_left_overlay", None)
        if lov is not None:
            lov.hide()
        # 2026-06-25 (апскейл): спутник 2K (9:16).
        aux2k = getattr(self, "_aux_2k_overlay", None)
        if aux2k is not None:
            aux2k.hide()
        # Стоп видео+звука, сброс кадра/флагов → плитка возвращается к превью+▶
        # (paintEvent). Поздний кадр после этого игнорится (_video_active=False).
        self._stop_video_playback()

    # ── btn_back ("вернуть промпт"): оживление ─────────────────────────
    def _refresh_back_enabled(self):
        """Кнопка btn_back активна только если в _meta есть непустой prompt.
        Дропнутые плитки имеют prompt="" → кнопка disabled (QSS:disabled даст
        приглушённый вид; курсор Arrow вместо PointingHand). Зовётся из __init__
        и из set_meta при каждом обновлении."""
        btn = getattr(self, "btn_back", None)
        if btn is None:
            return
        prompt = ""
        if isinstance(self._meta, dict):
            prompt = (self._meta.get("prompt") or "").strip()
        enabled = bool(prompt)
        btn.setEnabled(enabled)
        btn.setCursor(Qt.CursorShape.PointingHandCursor
                      if enabled else Qt.CursorShape.ArrowCursor)

    def _on_back_clicked(self):
        """Клик по btn_back: положить prompt из _meta в поле ввода Генератора,
        ЗАМЕНЯЯ текущий текст. Пустой prompt → выход (кнопка должна быть disabled,
        но guard всё равно полезен)."""
        prompt = ""
        if isinstance(self._meta, dict):
            prompt = (self._meta.get("prompt") or "").strip()
        if not prompt:
            return
        if self._page is not None:
            try:
                self._page.set_prompt(prompt)
            except Exception:
                pass

    # ── btn_ref ("использовать как реф для следующей генерации"): оживление ──
    def _refresh_ref_enabled(self):
        """Кнопка btn_ref активна только если в _meta есть meta['file'] (плитка
        уже имеет готовый файл на диске). Loading/error — disabled (нет файла).
        Зеркало _refresh_back_enabled. Видео-плитки НЕ блокируем здесь — для
        них в _on_ref_clicked подменяется meta['file'] на парный .jpg-кадр
        (см. ниже); если .jpg нет — тихий выход."""
        btn = getattr(self, "btn_ref", None)
        if btn is None:
            return
        has_file = False
        if isinstance(self._meta, dict):
            has_file = bool((self._meta.get("file") or "").strip())
        btn.setEnabled(has_file)
        btn.setCursor(Qt.CursorShape.PointingHandCursor
                      if has_file else Qt.CursorShape.ArrowCursor)

    def _refresh_trash_enabled(self):
        """Кнопка удаления активна для готового результата или error-плитки.
        Loading не удаляем: генерация ещё может завершиться в эту плитку."""
        btn = getattr(self, "btn_trash", None)
        if btn is None:
            return
        has_file = False
        if isinstance(self._meta, dict):
            has_file = bool((self._meta.get("file") or "").strip())
        enabled = bool(has_file or self._state == "error")
        btn.setEnabled(enabled)
        btn.setCursor(Qt.CursorShape.PointingHandCursor
                      if enabled else Qt.CursorShape.ArrowCursor)

    def _refresh_2k_enabled(self):
        """2026-06-25 (апскейл): «2K» видна ТОЛЬКО у image-карточек с готовым
        файлом. type определяем по meta — стабильнее чем _state (живёт даже на
        loading-плитке, но enabled только при наличии file)."""
        btn = getattr(self, "btn_2k", None)
        if btn is None:
            return
        is_image = False
        has_file = False
        if isinstance(self._meta, dict):
            is_image = (self._meta.get("type") or "image") == "image"
            has_file = bool((self._meta.get("file") or "").strip())
        enabled = bool(is_image and has_file and self._state == "image")
        btn.setVisible(is_image)
        btn.setEnabled(enabled)
        btn.setCursor(Qt.CursorShape.PointingHandCursor
                      if enabled else Qt.CursorShape.ArrowCursor)
        aux = getattr(self, "_aux_2k_overlay", None)
        if aux is not None:
            aux.setVisible(False)  # спутник появляется на hover, не сразу

    def _on_ref_clicked(self):
        """Клик по btn_ref: прикрепить файл этой плитки к prompt-bar как реф
        к следующей генерации. Страница резолвит полный путь из meta['file']
        через add_ref_from_meta. Для ВИДЕО-плитки .mp4 не подходит upload-у
        (MIME не image/*) → подменяем file на парный .jpg-кадр (gen_<ts>.jpg
        рядом с .mp4). Если .jpg нет — тихий выход. _meta плитки НЕ мутируем —
        работаем с копией."""
        if not isinstance(self._meta, dict):
            return
        fname = (self._meta.get("file") or "").strip()
        if not fname:
            return
        if self._page is None:
            return
        meta_for_page = self._meta
        if self._meta.get("type") == "video":
            # Подмена на парный .jpg (первый кадр). Существование .jpg на диске
            # проверит add_ref_from_meta через full.exists() — мы только подменяем
            # имя в КОПИИ meta. Если .jpg не лежит рядом — тихий выход там же.
            from pathlib import Path
            jpg_name = Path(fname).with_suffix(".jpg").name
            meta_for_page = dict(self._meta)
            meta_for_page["file"] = jpg_name
        try:
            self._page.add_ref_from_meta(meta_for_page)
        except Exception:
            pass

    def _on_trash_clicked(self):
        """Клик по trash: делегировать удаление странице, где есть доступ к
        списку холста, canvas.json и текущему show-root."""
        if self._page is None:
            return
        try:
            self._page.delete_result_cell(self)
        except Exception:
            pass

    def _on_2k_clicked(self):
        """2026-06-25 (апскейл): делегировать страницe. Page создаст НОВУЮ
        loading-плитку рядом и запустит UpscaleThread; исходную НЕ трогает."""
        if self._page is None:
            return
        try:
            self._page.upscale_result_cell(self)
        except Exception:
            pass

    def set_loading_text(self, msg: str) -> None:
        """2026-06-25 (апскейл): публичный API для трансляции прогресса
        («Скачиваю движок: 24%», «Улучшаю качество: 42%») в плашку лоадера.
        Только в state=='loading' — иначе игнорируем (не плодим текст на image).
        Выставляет _loading_text_external=True, чтобы _tick_seconds не
        перетёр через секунду. Флаг сбросится в _finish_common."""
        if self._state != "loading":
            return
        try:
            self._loading_text_external = True
            self._info_lbl.setText(msg or "")
            self._info_lbl.show()
        except Exception:
            pass

    def set_error(self, msg: str):
        self._finish_common()
        self._state = "error"
        self._pixmap = None
        self._result_path = None
        # Шрифт мельче (11px) + wordWrap + поля 10px → длинная причина переносится
        # и помещается ЦЕЛИКОМ в плитке, не торчит за край.
        self._info_lbl.setStyleSheet(
            "color:#ffb3b3; font-size:11px; background:transparent;")
        self._info_lbl.setText(msg or "Ошибка")
        self._info_lbl.show()
        self._refresh_reveal_enabled()
        self._refresh_ref_enabled()
        self._refresh_trash_enabled()
        self._refresh_2k_enabled()
        self.update()

    # ── play-треугольник по центру (плитка готового видео, заглушка до кадра) ──
    def _draw_play_triangle(self, p: QPainter):
        """Полупрозрачный тёмный круг + белый ▶ по центру плитки."""
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = max(16.0, min(self.width(), self.height()) * 0.16)   # радиус круга
        circle = QPainterPath()
        circle.addEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.fillPath(circle, QColor(0, 0, 0, 110))
        # Равнобедренный треугольник «play», вписанный в круг (чуть сдвинут вправо
        # для оптической центровки).
        s = r * 0.9
        tri = QPainterPath()
        tri.moveTo(cx - s * 0.4, cy - s * 0.55)
        tri.lineTo(cx - s * 0.4, cy + s * 0.55)
        tri.lineTo(cx + s * 0.6, cy)
        tri.closeSubpath()
        p.fillPath(tri, QColor(255, 255, 255, 230))

    # ── бейдж модели поверх картинки (UI-only, в файл не вшивается) ──────
    def _draw_model_badge(self, p: QPainter):
        """Имя модели в левом нижнем углу: белый текст ~10px на полупрозрачной
        тёмной скруглённой подложке (контраст на светлых картинках)."""
        margin = 8
        pad_x, pad_y = 4, 2
        font = QFont()
        font.setPixelSize(10)
        p.setFont(font)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self._model_label)
        th = fm.height()
        rect_w = tw + pad_x * 2
        rect_h = th + pad_y * 2
        x = margin
        y = self.height() - margin - rect_h
        bg = QPainterPath()
        bg.addRoundedRect(QRectF(x, y, rect_w, rect_h), 4, 4)
        p.fillPath(bg, QColor(0, 0, 0, 140))   # rgba(0,0,0,≈0.55)
        p.setPen(QColor(255, 255, 255))
        p.drawText(int(x + pad_x), int(y + pad_y + fm.ascent()), self._model_label)

    # ── отрисовка базы/блика/ошибки ─────────────────────────────────────
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.width(), self.height()), 8, 8)

        # ── IMAGE: чистая картинка во всю ячейку, скруглённые углы, БЕЗ рамки/
        # подложки/паддинга (как Google Flow). Клип по rounded-rect + center-crop. ──
        if self._state == "image" and self._pixmap is not None:
            p.setClipPath(path)
            pm = self._pixmap
            x = (self.width() - pm.width()) // 2
            y = (self.height() - pm.height()) // 2
            p.drawPixmap(int(x), int(y), pm)
            if self._model_label:
                self._draw_model_badge(p)
            p.end()
            return

        # ── VIDEO ──
        if self._state == "video":
            # ИГРАЕМ (пришёл ≥1 кадр): рисуем последний кадр с клипом по rounded-rect
            # (углы скругляются сами, БЕЗ нативного виджета/маски) + полоска-прогресс.
            if self._video_playing and self._last_video_frame is not None:
                p.setClipPath(path)
                img = self._last_video_frame
                iw, ih = img.width(), img.height()
                if iw > 0 and ih > 0:
                    # fill ByExpanding + center-crop (как _rescale_pixmap для image).
                    scale = max(self.width() / iw, self.height() / ih)
                    tw, th = iw * scale, ih * scale
                    tx = (self.width() - tw) / 2.0
                    ty = (self.height() - th) / 2.0
                    p.drawImage(QRectF(tx, ty, tw, th), img)
                p.setClipping(False)
                self._draw_progress(p)               # полоска с отступами поверх кадра
                if self._model_label:
                    self._draw_model_badge(p)
                p.end()
                return
            # НЕ играем (нет hover / ждём первый кадр): превью-кадр (gen_*.jpg) ИЛИ
            # тёмный фон + ▶ ВСЕГДА поверх (маркер «это видео») + бейдж.
            if self._pixmap is not None:
                p.setClipPath(path)
                pm = self._pixmap
                x = (self.width() - pm.width()) // 2
                y = (self.height() - pm.height()) // 2
                p.drawPixmap(int(x), int(y), pm)
                p.setClipping(False)
            else:
                p.fillPath(path, _BASE_COLOR)
            self._draw_play_triangle(p)
            if self._model_label:
                self._draw_model_badge(p)
            p.end()
            return

        if self._state == "error":
            p.fillPath(path, _ERR_BASE)
            p.setPen(_ERR_BORDER)
            p.drawPath(path)
            p.end()
            return

        if self._state == "loading":
            # 2026-06-23: pulse+depth+corner вынесены в общий хелпер
            # widgets/shimmer_paint.paint_shimmer_loading — ровно тот же визуал
            # (порядок слоёв и значения констант 1:1), просто из одного источника
            # правды. Тёмный _BASE_COLOR теперь рисует сам хелпер первым слоем,
            # поэтому fillPath(path, _BASE_COLOR) выше БЫЛ продублирован — убрал.
            # set_phase / register_loading / общий таймер страницы не тронуты.
            from widgets.shimmer_paint import paint_shimmer_loading
            paint_shimmer_loading(
                p, QRectF(0, 0, self.width(), self.height()), self._angle)
        else:
            # НЕ-loading состояние сюда доходит, когда image/video/error не сработали
            # ранним return'ом (например, image без _pixmap). Поведение прежнее:
            # тёмная подложка под последующий бейдж/рамку.
            p.fillPath(path, _BASE_COLOR)

        # Бейдж модели поверх loading-плитки (сразу при старте; error сюда не доходит —
        # у него ранний return выше). На image бейдж рисуется в своей ветке.
        if self._model_label:
            self._draw_model_badge(p)
        p.setPen(_BORDER_COLOR)
        p.drawPath(path)
        p.end()
