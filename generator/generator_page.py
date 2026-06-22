# -*- coding: utf-8 -*-
"""
generator/generator_page.py — страница «Генератор» (КАРКАС, 2026-06-20).

ВИЗУАЛЬНЫЙ МАКЕТ. Все элементы нарисованы и enabled, но БЕЗ обработчиков и
логики — генерация / переключение холстов / сетка результатов / смена списка
моделей привязываются отдельными шагами.

Самодостаточный QWidget: на уровне модуля импортирует только PyQt6.
`get_icon` тянется ЛЕНИВО внутри метода (как widgets/shot_viewer_dialog.py),
чтобы не словить circular import со storyboard_app в frozen .app.

Раскладка (сверху вниз):
  • _build_canvas_row    — ряд «Холст 1/2/3» + «+ Новый» (заглушки, не переключаются;
                           активный без ✕, остальные с ✕ get_icon('x'))
  • _build_results_area  — QScrollArea с заглушкой по центру (сетку добавим позже)
  • _build_prompt_bar    — ввод промпта + режим Картинка/Видео + формат 16:9/9:16 +
                           кол-во ×1..×4 + выпадашка моделей (по режиму) + запуск (wand-2)

Список моделей зависит от режима — MODELS_BY_MODE + _populate_models(mode).
Привязка смены режима → _populate_models делается позже одним connect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QSize, QTimer, QSettings, QEvent, QPoint,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QTextEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
    QGraphicsOpacityEffect,
)

from generator.result_cell import ShimmerCell
from generator.model_select import ModelSelect


# Модели по режиму: (отображаемое имя, внутренний id для payload["model"]).
# id реальные из /api/v5/models. ВНИМАНИЕ: генерация видео пока НЕ подключена —
# в _on_run режим "video" возвращает «Видео скоро будет» ДО использования id.
MODELS_BY_MODE = {
    "image": [("Nano Banana 2", "nano-banana-2"), ("OpenAI", "openai-image")],
    "video": [("Veo 3.1 Fast (8s)", "flow-video-fast"),
              ("Omni Flash", "flow-video-omni-flash")],
}

# Число плиток 9:16 в ряду — ЖЁСТКО по режиму (_grid_cols). Ширина 9:16 подгоняется
# под доступную ширину так, чтобы ровно n_v штук легли без дыры справа; высота 9:16 —
# производная (w*16//9, выше 16:9). 16:9 при этом не меняется (как было).
N_VERT_BY_COLS = {2: 5, 3: 8, 4: 11}


class GeneratorPage(QWidget):
    """Каркас страницы генератора. Заглушки без логики (этот шаг — только UI)."""

    _PROMPT_MIN_H = 46      # минимум поля промпта (1-2 строки)
    _PROMPT_MAX_H = None    # потолок = 20 строк, считается лениво от ЖИВОГО шрифта поля

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("generator-page")
        self.setAcceptDrops(True)   # drag-and-drop файлов с диска в холст (см. dropEvent)
        self._mode = "image"   # активный режим (пока статично, без переключения)
        self._duration = 8     # длительность видео (сек); видна только для Omni Flash
        # Параллельные генерации: список активных потоков (Pattern A: parent=None
        # + ссылка тут, Qt не соберёт; убираются по finished/error). Кнопку НЕ блокируем.
        self._gen_threads = []
        # Общий shimmer-такт (~20fps) для всех loading-плиток; стоит в простое.
        self._loading_cells = set()
        self._shimmer_phase = 0.0
        self._shimmer_timer = None
        # Счётчик заполненных ячеек + ссылки на ВСЕ плитки (для перераскладки).
        self._cell_count = 0
        self._cells = []
        # Прикреплённые рефы (per-session, не per-show): пути файлов, которые
        # уходят в payload["inputs"] следующей генерации. UI — ряд превьюшек выше
        # поля ввода в prompt-bar. dict для O(1) remove по пути.
        self._pending_refs: list[str] = []
        self._ref_thumbs: dict = {}   # path → QFrame thumb-виджет
        self._thumb_popup = None      # QLabel popup увеличенной превьюшки (ленив)
        self._thumb_anim = None       # QPropertyAnimation для fade-in/out (хранится
                                      # на self, иначе GC соберёт во время анимации)
        # Размер плиток: ВСЕГДА стартуем с M (3 колонки), игнорируя прошлые сессии
        # (по требованию). Переключение S/M/L работает в рамках сессии через _grid_cols.
        self._grid_cols = 3
        # Первый показ ещё не случился → размеры плиток в _load_canvas считаются по
        # фолбэк-ширине (viewport=0); пересчёт под реальную ширину — в showEvent.
        self._shown_once = False
        self._build_ui()
        # Под-шаг 3: первичное восстановление холста активного сериала (если есть
        # canvas.json). Пустой/битый/отсутствующий файл → холст остаётся пустым.
        self._load_canvas()

    # ── ленивый Lucide-иконкозагрузчик (без module-level import storyboard_app) ──
    def _icon(self, name: str):
        try:
            from storyboard_app import get_icon
            return get_icon(name)
        except Exception:
            return None

    # ── UI ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setStyleSheet(
            "QWidget#generator-page { background:#15101e; }"
            # Холст-вкладки (браузерный тип): без коробки; активная — мягкая
            # подложка #181222 + янтарное подчёркивание 2px, садится на линию.
            "QFrame#canvas-chip { background:transparent; border:none;"
            " border-top-left-radius:9px; border-top-right-radius:9px;"
            " border-bottom:2px solid transparent; }"
            "QFrame#canvas-chip:hover { background:#140f1e; }"
            "QFrame#canvas-chip[active=\"true\"] { background:#181222;"
            " border-bottom:2px solid #d4a256; }"
            "QLabel#canvas-title { color:rgba(255,255,255,0.5); font-size:12px;"
            " background:transparent; }"
            "QFrame#canvas-chip[active=\"true\"] QLabel#canvas-title { color:#f2eef8; }"
            "QPushButton#canvas-new { background:transparent; color:#7a7488;"
            " border:none; border-radius:8px; padding:0px; font-size:18px; }"
            "QPushButton#canvas-new:hover { color:#cfcfcf; background:#140f1e; }"
            "QPushButton#canvas-new:disabled { color:#4a4458; background:transparent; }"
            "QFrame#canvas-divider { background:#2a2438; border:none; }"
            "QPushButton#canvas-close { background:transparent; border:none;"
            " border-radius:4px; }"
            "QPushButton#canvas-close:hover { background:#2c2438; }"
            # Ряд прикреплённых рефов в prompt-bar (показывается выше поля ввода)
            "QWidget#refs-row { background:transparent; }"
            "QFrame#ref-thumb { background:#1a1428;"
            " border:1px solid rgba(255,255,255,0.15); border-radius:6px; }"
            "QPushButton#ref-thumb-x { background:rgba(0,0,0,0.7); color:#ffffff;"
            " border:none; border-radius:9px; font-weight:600; font-size:14px;"
            " padding:0px; text-align:center; }"
            "QPushButton#ref-thumb-x:hover { background:rgba(0,0,0,0.9); }"
            # Popup увеличенной картинки при hover на ref-thumb (один QLabel
            # на страницу, переиспользуется, скрыт по умолчанию).
            "QLabel#ref-thumb-popup { background:#1a1428;"
            " border:1px solid rgba(255,255,255,0.25); border-radius:8px; }"
            # Область результатов
            "QScrollArea#results { background:transparent; border:none; }"
            "QLabel#results-empty { color:#6a6a78; font-size:13px;"
            " background:transparent; }"
            # Поле промпта
            "QFrame#prompt-bar { background:#181222; border:1px solid #281f36;"
            " border-radius:18px; }"
            "QTextEdit#prompt-input { background:transparent; color:#fff;"
            " border:none; padding:10px 14px 10px 0px;"
            " font-size:14px; }"
            "QLabel#group-cap { color:#888; font-size:10px;"
            " background:transparent; }"
            # Сегмент-кнопки (режим / формат / кол-во)
            "QFrame#seg-group { background:#100b18; border:none;"
            " border-radius:10px; }"
            "QPushButton#seg { background:transparent; color:rgba(255,255,255,0.55);"
            " border:none; border-radius:6px; padding:0px 11px; font-size:12px; }"
            "QPushButton#seg:hover { color:rgba(255,255,255,0.85); }"
            "QPushButton#seg[active=\"true\"] { background:rgba(255,255,255,0.08);"
            " color:#fff; border-radius:7px; }"
            # Акцентный сегмент (режим Картинка/Видео): активная — янтарь
            "QPushButton#seg-accent { background:transparent;"
            " color:rgba(255,255,255,0.55); border:none; border-radius:6px;"
            " padding:0px 11px; font-size:12px; }"
            "QPushButton#seg-accent:hover { color:rgba(255,255,255,0.85); }"
            "QPushButton#seg-accent[active=\"true\"] { background:#d4a256;"
            " color:#15101e; border-radius:7px; }"
            # Выпадашка моделей
            "QComboBox#model-combo { background:#100b18; color:#fff;"
            " border:1px solid #281f36; border-radius:10px; padding:7px 12px;"
            " font-size:13px; min-width:150px; }"
            "QComboBox#model-combo QAbstractItemView { background:#221b2e;"
            " border:1px solid #322a40; border-radius:12px; padding:6px;"
            " outline:none; color:#e8e3f0; font-size:13px;"
            " selection-background-color:#2c2438; }"
            "QComboBox#model-combo QAbstractItemView::item {"
            " padding:8px 10px; border-radius:8px; }"
            # Кнопка запуска (акцент — янтарь, как вкладка)
            "QPushButton#run-btn { background:#d4a256; color:#15101e;"
            " border:none; border-radius:17px;"
            " font-size:20px; font-weight:600; }"
            "QPushButton#run-btn:hover { background:#e8b86a; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        root.addWidget(self._build_canvas_row())
        root.addWidget(self._build_results_area(), stretch=1)
        root.addWidget(self._build_prompt_bar())
        self._update_duration_visibility()   # стартово скрыт (дефолт — режим image)

    # ── (B) ряд холстов — вкладки браузерного типа (только ВИД; логика — заход 2) ──
    def _build_canvas_row(self) -> QWidget:
        # Обёртка: ряд вкладок + тонкая линия-разделитель под ним.
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Ряд вкладок (chips + «+»), прижат к низу — вкладки садятся на линию.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        # Пока ОДИН холст (мультихолст-логика отложена). Активный — без ✕.
        row.addWidget(self._canvas_chip("Холст 1", active=True))
        new_btn = QPushButton("+")
        new_btn.setObjectName("canvas-new")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setFixedSize(32, 34)
        new_btn.setEnabled(False)   # неактивна (логика добавления — позже)
        # заглушка: без обработчика
        row.addWidget(new_btn)
        row.addStretch()
        outer.addLayout(row)
        # Линия-разделитель 1px (активная вкладка разрывает её янтарным подчёркиванием).
        divider = QFrame()
        divider.setObjectName("canvas-divider")
        divider.setFixedHeight(1)
        outer.addWidget(divider)
        return wrap

    def _canvas_chip(self, title: str, active: bool) -> QFrame:
        chip = QFrame()
        chip.setObjectName("canvas-chip")
        chip.setProperty("active", active)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)   # кликабельность — заход 2
        chip.setFixedHeight(34)   # вровень с сегментами нижней панели (#seg = 34)
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(16, 0, 16, 0)   # вертикаль держит setFixedHeight, текст по центру
        lay.setSpacing(8)
        lbl = QLabel(title)
        lbl.setObjectName("canvas-title")
        lay.addWidget(lbl)
        if not active:
            close = QPushButton()
            close.setObjectName("canvas-close")
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            ic = self._icon("x")
            if ic is not None:
                close.setIcon(ic)
                close.setIconSize(QSize(12, 12))
            else:
                close.setText("x")
            close.setFixedSize(18, 18)
            # заглушка: без обработчика
            lay.addWidget(close)
        return chip

    # ── (C) область результатов — пустая прокрутка с заглушкой ──────────
    def _build_results_area(self) -> QScrollArea:
        area = QScrollArea()
        area.setObjectName("results")
        area.setWidgetResizable(True)
        # ФИКС: горизонтального скролла НЕТ; перенос по рядам, скролл только вертикальный.
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Вертикальный скроллбар ВСЕГДА зарезервирован (не AsNeeded): иначе viewport().width()
        # прыгает на ~15px при появлении/исчезновении бара, и пересчёт плиток при смене
        # режима (4→3) идёт по неустаканенной ширине → крайняя плитка обрезается. AlwaysOn
        # делает ширину viewport детерминированной (area − scrollbar) на Mac и Win.
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        # Слить область с фоном страницы (как Flow): без рамки/подложки.
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.viewport().setStyleSheet("background:transparent;")
        self._results_area = area
        inner = QWidget()
        outer_v = QVBoxLayout(inner)
        outer_v.setContentsMargins(0, 0, 0, 0)
        outer_v.setSpacing(0)
        # Центрированная заглушка — видна пока сетка пуста.
        self._empty_host = QWidget()
        eh = QVBoxLayout(self._empty_host)
        eh.addStretch()
        empty = QLabel("Здесь появятся сгенерированные картинки")
        empty.setObjectName("results-empty")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        eh.addWidget(empty)
        eh.addStretch()
        self._results_empty = empty
        outer_v.addWidget(self._empty_host, stretch=1)
        # Ряды результатов: вертикальный стек рядов-QHBoxLayout (НЕ QGridLayout) —
        # каждый ряд НЕЗАВИСИМ, поэтому 9:16 (уже) и 16:9 (шире) лежат в СВОИХ рядах
        # с разным числом плиток, не подмешиваясь друг к другу. Высота у всех общая.
        self._grid_host = QWidget()
        self._rows_v = QVBoxLayout(self._grid_host)
        self._rows_v.setContentsMargins(0, 0, 0, 0)
        self._rows_v.setSpacing(12)
        self._grid_host.hide()
        # grid_host на всю ширину; левое выравнивание держит trailing-stretch в каждом ряду.
        grid_row = QHBoxLayout()
        grid_row.setContentsMargins(0, 0, 0, 0)
        grid_row.addWidget(self._grid_host, 1)
        outer_v.addLayout(grid_row)
        outer_v.addStretch(1)
        area.setWidget(inner)
        return area

    # ── (D) поле промпта снизу — все элементы enabled, без обработчиков ──
    def _build_prompt_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("prompt-bar")
        self._prompt_bar = bar   # ссылка для позиционирования тост-подсказки
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        # Ряд прикреплённых рефов — ВЫШЕ поля ввода (как Google Flow). Скрыт пока
        # _pending_refs пуст. add_ref/remove_ref/clear_refs управляют видимостью.
        self._refs_row = QWidget()
        self._refs_row.setObjectName("refs-row")
        self._refs_row_lay = QHBoxLayout(self._refs_row)
        self._refs_row_lay.setContentsMargins(0, 0, 0, 0)
        self._refs_row_lay.setSpacing(6)
        self._refs_row_lay.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._refs_row.setVisible(False)
        outer.addWidget(self._refs_row)

        # Ввод промпта
        self.prompt_input = QTextEdit()
        self.prompt_input.setObjectName("prompt-input")
        self.prompt_input.setAcceptRichText(False)   # вставка только plain-текст
        self.prompt_input.setPlaceholderText("Что хочешь сгенерировать?")
        # Авто-рост под текст (как Flow): высота по содержимому, потолок _PROMPT_MAX_H,
        # дальше — скролл ВНУТРИ поля; минимум — _PROMPT_MIN_H.
        self.prompt_input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.prompt_input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.prompt_input.setFixedHeight(self._PROMPT_MIN_H)
        self.prompt_input.document().contentsChanged.connect(self._adjust_prompt_height)
        self.prompt_input.installEventFilter(self)   # resize → пересчёт высоты
        outer.addWidget(self.prompt_input)

        # Ряд контролов. Порядок: режим → модель → формат → кол-во → колонки → run.
        ctl = QHBoxLayout()
        ctl.setSpacing(10)
        ctl.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Режим: Картинка / Видео (Картинка активна)
        self.mode_seg, self.mode_btns = self._seg_group(
            [("Картинка", "image"), ("Видео", "video")], active_key="image", accent=True)
        # Клик по режиму: подсветка + смена списка моделей (см. _on_mode_change).
        for _k, _b in self.mode_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_mode_change(key))
        ctl.addWidget(self.mode_seg)

        # Модель — ВТОРАЯ, сразу после режима (зависит от режима).
        # Свой тёмный выпадающий виджет (всплывает ВВЕРХ). Контракт под _on_run:
        # current_model_id() == прежний currentData(). objectName "model-combo"
        # на кнопке-триггере ВНУТРИ виджета; высоту ряда (34) держим снаружи.
        self.model_combo = ModelSelect()
        self.model_combo.setFixedHeight(34)
        # Фикс ширины ВПРИТЫК к самому длинному лейблу «Veo 3.1 Fast (8s)» (~142px):
        # 146 = текст + стрелка + поля, без большого зазора. (min-width триггера в
        # model_select.py снижен до 120, иначе пол 150 не дал бы сжаться.)
        self.model_combo.setFixedWidth(146)
        self._populate_models(self._mode)
        # Смена модели в выпадашке → пересчёт видимости сегмента длительности.
        self.model_combo.changed.connect(self._update_duration_visibility)
        ctl.addWidget(self.model_combo)

        # Длительность видео 4/6/8/10 (видна ТОЛЬКО для Omni Flash — show/hide в
        # _update_duration_visibility). Дефолт 8. Между моделью и форматом.
        self.dur_seg, self.dur_btns = self._seg_group(
            [("4s", "4"), ("6s", "6"), ("8s", "8"), ("10s", "10")],
            active_key=str(self._duration))
        for _k, _b in self.dur_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_duration_change(key))
        ctl.addWidget(self.dur_seg)

        # Формат: 16:9 / 9:16 (16:9 активен)
        self.fmt_seg, self.fmt_btns = self._seg_group(
            [("16:9", "16:9"), ("9:16", "9:16")], active_key="16:9")
        for _k, _b in self.fmt_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_seg_click(self.fmt_btns, key))
        ctl.addWidget(self.fmt_seg)

        # Количество: ×1..×4 (×1 активно)
        self.count_seg, self.count_btns = self._seg_group(
            [("×1", "1"), ("×2", "2"), ("×3", "3"), ("×4", "4")], active_key="1")
        for _k, _b in self.count_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_seg_click(self.count_btns, key))
        ctl.addWidget(self.count_seg)

        # Размер плиток S/M/L (слева→направо мелкий→крупный). Лейбл — буква, КЛЮЧ —
        # число колонок 16:9: S=4 (мелкие), M=3, L=2 (крупные). _on_cols_change/_grid_cols/
        # N_VERT_BY_COLS работают по ключу-числу как раньше; меняется только порядок и подпись.
        self.cols_seg, self.cols_btns = self._seg_group(
            [("S", "4"), ("M", "3"), ("L", "2")], active_key=str(self._grid_cols))
        for _k, _b in self.cols_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_cols_change(key))
        ctl.addWidget(self.cols_seg)

        ctl.addStretch()

        # Кнопка запуска — круглая, стрелка вверх (Flow-стиль).
        self.run_btn = QPushButton()
        self.run_btn.setObjectName("run-btn")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.setFixedSize(34, 34)
        self.run_btn.setText("↑")   # стрелка вверх; при необходимости заменим на svg
        self.run_btn.clicked.connect(self._on_run)   # MVP: запуск генерации
        ctl.addWidget(self.run_btn)

        outer.addLayout(ctl)

        # Транзиентная подсказка — ТОСТ поверх (родитель = страница, НЕ в layout),
        # чтобы НЕ двигать геометрию prompt-bar. Всплывает над панелью на 4с.
        self._hint_lbl = QLabel("", self)
        self._hint_lbl.setObjectName("gen-hint")
        self._hint_lbl.setStyleSheet(
            "color:#15101e; font-size:12px; font-weight:600;"
            " background:#d4a256; border-radius:8px; padding:6px 12px;")
        self._hint_lbl.hide()
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(self._hide_hint)
        return bar

    def _adjust_prompt_height(self):
        """Высота поля = под содержимое (РЕАЛЬНЫЕ пиксели), [MIN, MAX]; выше — внутренний
        скролл (как Flow). QTextEdit + document().setTextWidth(viewport) → size().height()
        в пикселях с учётом переноса (надёжно, в отличие от QPlainTextDocumentLayout,
        который мерил в строках). Потолок _PROMPT_MAX_H считается ЛЕНИВО = 20 строк от
        живого шрифта поля. setDocumentMargin(2)+chrome=22 → при 1-5 строках h ≥ контента,
        AsNeeded-скролл не появляется рано."""
        pi = self.prompt_input
        doc = pi.document()
        doc.setDocumentMargin(2)   # фикс. отступ документа (по умолчанию 4 и плавает)
        doc.setTextWidth(pi.viewport().width())
        content = doc.size().height()
        chrome = 22                # QSS padding (10+10) + 2px запас → скролл не лезет рано
        if self._PROMPT_MAX_H is None:
            line_px = pi.fontMetrics().lineSpacing()
            self._PROMPT_MAX_H = int(line_px * 20 + chrome)   # потолок = 20 строк
        h = int(content) + chrome
        h = max(self._PROMPT_MIN_H, min(self._PROMPT_MAX_H, h))
        pi.setFixedHeight(h)

    def eventFilter(self, obj, ev):
        """Пересчёт высоты поля при изменении его ШИРИНЫ (перенос строк едет при
        ресайзе окна). Только на смену ширины → без рекурсии с setFixedHeight.
        Также: Enter/Leave на ref-thumb (превьюшки рефов) — показать/скрыть ✕
        и popup увеличенной картинки."""
        if obj is getattr(self, "prompt_input", None) and ev.type() == QEvent.Type.Resize:
            w = self.prompt_input.viewport().width()
            if w != getattr(self, "_last_prompt_w", -1):
                self._last_prompt_w = w
                self._adjust_prompt_height()
        # ref-thumb hover: показ/скрытие крестика + popup увеличения.
        if isinstance(obj, QFrame) and obj.objectName() == "ref-thumb":
            t = ev.type()
            if t == QEvent.Type.Enter:
                btn = getattr(obj, "_x_btn", None)
                if btn is not None:
                    btn.setVisible(True)
                self._show_thumb_popup(obj)
            elif t == QEvent.Type.Leave:
                btn = getattr(obj, "_x_btn", None)
                if btn is not None:
                    btn.setVisible(False)
                self._hide_thumb_popup()
        return super().eventFilter(obj, ev)

    def showEvent(self, event):
        """Первый показ: viewport уже имеет реальную ширину → пересчитать размеры
        имеющихся плиток под текущий _grid_cols. Иначе восстановленные в _load_canvas
        (при __init__, до показа) плитки посчитаны по фолбэк-ширине 1200 и выглядят
        мелко (как S) пока юзер не переключит S/M/L вручную."""
        super().showEvent(event)
        if not self._shown_once:
            if self._cells:
                self._relayout_grid()
            self._shown_once = True

    def resizeEvent(self, event):
        """Ресайз окна → переразмерить уже выложенные плитки под новую ширину.
        _relayout_grid идемпотентен и дёшев (set_size без пересоздания плиток) —
        без дебаунса/таймера, просто зовём на каждый ресайз."""
        super().resizeEvent(event)
        if self._cells:
            self._relayout_grid()

    # ── drag-and-drop файлов с диска в холст ──────────────────────────
    def dragEnterEvent(self, ev):
        if ev.mimeData() and ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        # Без этого dropEvent НЕ сработает на macOS — drag-and-drop требует
        # акцепта действия и в move (как в views/actors.py).
        if ev.mimeData() and ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        # Дроп ИЗ prompt_input (QTextEdit) — не наш, пусть обрабатывает сам.
        # Внешний файловый дроп с диска имеет source()==None → не отсеивается.
        pi = getattr(self, "prompt_input", None)
        if pi is not None and ev.source() is pi:
            return
        md = ev.mimeData()
        if not md or not md.hasUrls():
            return
        from pathlib import Path
        paths = [Path(u.toLocalFile()) for u in md.urls()
                 if u.toLocalFile() and Path(u.toLocalFile()).is_file()]
        if not paths:
            return
        ev.acceptProposedAction()
        self._import_dropped_files(paths)

    def _aspect_from_image(self, path) -> str:
        """Бакет '16:9'/'9:16' по реальным размерам картинки (QImage — Unicode-safe,
        кириллица в пути не мешает). width >= height → '16:9', иначе '9:16'.
        Не прочитать → фолбэк '16:9'."""
        try:
            from PyQt6.QtGui import QImage
            img = QImage(str(path))
            if img.isNull() or img.width() <= 0 or img.height() <= 0:
                return "16:9"
            return "16:9" if img.width() >= img.height() else "9:16"
        except Exception:
            return "16:9"

    def _import_dropped_files(self, paths):
        """Скопировать дропнутые файлы в shows/<slug>/generator/ и поставить плитками
        СВЕРХУ (новейшие). Формат — по реальным размерам файла. Пишется в canvas.json
        (переживает перезапуск). Мультидроп: все наверх, порядок выделения сохранён.
        Оригиналы юзера НЕ трогаются (shutil.copy2). Битый файл не валит остальные."""
        import shutil, time
        import storyboard_app as _sa
        root = _sa.get_stored_root()
        slug = _sa.get_current_show(root) if root else None
        if not root or not slug:
            self._show_hint("Чтобы добавить файлы, создай любой сериал")
            return
        out_dir = root / "shows" / slug / "generator"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        IMG = {".jpg", ".jpeg", ".png", ".webp"}
        VID = {".mp4", ".mov", ".m4v", ".webm"}
        # Реюз извлечения первого кадра из видео-потока (cv2.imencode→jpg, НЕ imwrite —
        # не-ASCII пути). Метод _extract_first_frame не использует self → лёгкий
        # throwaway-инстанс (QThread НЕ стартуем, только как неймспейс). Создаём
        # лишь если в дропе есть видео.
        _frame = None
        if any(p.suffix.lower() in VID for p in paths):
            try:
                from generator.generator_video_thread import GeneratorVideoThread
                _frame = GeneratorVideoThread("", "16:9", "", None, out_dir, parent=None)
            except Exception:
                _frame = None
        added = 0
        # reversed: insert(0) в цикле перевернул бы порядок между файлами; reversed
        # компенсирует → итог сверху = порядок выделения.
        for src in reversed(list(paths)):
            try:
                ext = src.suffix.lower()
                if ext in VID:
                    ftype = "video"
                elif ext in IMG:
                    ftype = "image"
                else:
                    continue   # неподдерживаемый формат — пропуск
                # Уникальное имя по СТЕМУ gen_<ts> (резервируем весь стем, чтобы кадр-
                # превью gen_<ts>.jpg видео не затёр одноимённую дропнутую картинку).
                ts = time.strftime("%Y%m%d_%H%M%S")
                stem = f"gen_{ts}"
                n = 2
                while any(out_dir.glob(stem + ".*")):
                    stem = f"gen_{ts}_{n}"
                    n += 1
                target = out_dir / f"{stem}{ext}"
                shutil.copy2(str(src), str(target))   # оригинал не трогаем
                if ftype == "video":
                    # первый кадр → gen_<ts>.jpg рядом (для превью); нет кадра → ▶-фолбэк
                    if _frame is not None:
                        try:
                            _frame._extract_first_frame(target)
                        except Exception:
                            pass
                    jpg = target.with_suffix(".jpg")
                    aspect = self._aspect_from_image(jpg) if jpg.exists() else "16:9"
                else:
                    aspect = self._aspect_from_image(target)
                w, h = self._cell_wh(aspect)
                cell = ShimmerCell(self, w, h, aspect=aspect)
                cell.set_model_label("")
                cell.set_meta(prompt="", model_id="", model_label="",
                              aspect=aspect, type=ftype, file=target.name, ts=ts)
                if ftype == "video":
                    cell.set_video_placeholder(str(target))
                else:
                    cell.set_image(str(target))
                self._cells.insert(0, cell)   # дропнутое — сверху
                added += 1
            except Exception:
                continue   # битый файл не валит остальные
        if added:
            self._cell_count = len(self._cells)
            self._empty_host.hide()
            self._grid_host.show()
            self._relayout_grid()   # ОДИН раз в конце
            self._save_canvas()     # ОДИН раз в конце

    # ── helpers ─────────────────────────────────────────────────────────
    def _seg_group(self, items, active_key: str, accent: bool = False):
        """Сегмент-переключатель. items = [(label, key), ...]. Возвращает
        (QFrame группа, {key: QPushButton}). Кнопки enabled, активная помечена
        property active. accent=True → objectName "seg-accent" (активная — янтарь)."""
        grp = QFrame()
        grp.setObjectName("seg-group")
        lay = QHBoxLayout(grp)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        seg_obj = "seg-accent" if accent else "seg"
        btns = {}
        for label, key in items:
            b = QPushButton(label)
            b.setObjectName(seg_obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Expanding)   # заливка на всю высоту пилюли
            b.setProperty("active", key == active_key)
            b.setProperty("_seg_key", key)
            # заглушка: без .connect()
            lay.addWidget(b)
            btns[key] = b
        grp.setFixedHeight(34)
        return grp, btns

    def _populate_models(self, mode: str):
        """Заполняет выпадашку моделей под режим через виджет ModelSelect.
        items = [(label, model_id), ...]; id отдаётся current_model_id().
        Вызов при смене режима — позже (1 connect)."""
        self.model_combo.set_models(MODELS_BY_MODE.get(mode, []))

    # ── MVP сквозная генерация (текст → wand-2 → картинка) ─────────────
    def _active_seg_key(self, btns) -> Optional[str]:
        """Ключ активного сегмента (по property active). None если нет."""
        for key, b in btns.items():
            if b.property("active"):
                return key
        return None

    def _on_seg_click(self, btns, key: str):
        """Активировать сегмент key в группе btns: снять active с прочих, поставить
        на выбранную + реполиш (иначе QSS [active] не перерисуется динамически)."""
        for k, b in btns.items():
            b.setProperty("active", k == key)
            try:
                b.style().unpolish(b)
                b.style().polish(b)
            except Exception:
                pass

    def _on_mode_change(self, key: str):
        """Смена режима Картинка/Видео: подсветка сегмента + список моделей под режим."""
        self._on_seg_click(self.mode_btns, key)
        self._mode = key
        self._populate_models(key)
        self._update_duration_visibility()

    def _on_duration_change(self, key: str):
        """Клик 4/6/8/10: подсветка активной + запомнить длительность (сек)."""
        self._on_seg_click(self.dur_btns, key)
        try:
            self._duration = int(key)
        except Exception:
            pass

    def _update_duration_visibility(self):
        """Сегмент длительности виден ТОЛЬКО для video + Omni Flash (omni требует
        duration_seconds). Для image и video+Veo — скрыт."""
        show = (self._mode == "video"
                and self.model_combo.current_model_id() == "flow-video-omni-flash")
        self.dur_seg.setVisible(show)

    def _show_hint(self, text: str):
        """Транзиентная подсказка-тост поверх prompt-bar (4с). НЕ в layout → геометрию
        панели не двигает. Позиционируется над панелью по центру."""
        if not text:
            self._hint_lbl.hide()
            return
        self._hint_lbl.setText(text)
        self._hint_lbl.adjustSize()
        bar = getattr(self, "_prompt_bar", None)
        if bar is not None:
            tl = bar.mapTo(self, QPoint(0, 0))
            x = tl.x() + max(0, (bar.width() - self._hint_lbl.width()) // 2)
            y = max(4, tl.y() - self._hint_lbl.height() - 6)
            self._hint_lbl.move(x, y)
        self._hint_lbl.show()
        self._hint_lbl.raise_()
        self._hint_timer.start(4000)

    def _hide_hint(self):
        self._hint_lbl.hide()

    def _on_run(self):
        """Запуск генерации. ПАРАЛЛЕЛЬНО: кнопку НЕ блокируем; ×N → N потоков и N
        плиток (каждый поток берёт свой round-robin ключ в run()). Формат и
        количество — из активных сегментов; модель из выпадашки; без рефов."""
        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            self.prompt_input.setFocus()
            self._show_hint("Введи описание картинки")
            return
        import storyboard_app as _sa
        root = _sa.get_stored_root()
        slug = _sa.get_current_show(root) if root else None
        if not root or not slug:
            # Картинки сохраняются в папку сериала — без него генерация невозможна.
            self._show_hint("Чтобы генерировать, создай любой сериал")
            return
        model_id = self.model_combo.current_model_id()
        if not model_id:
            self._show_hint("Модель недоступна")
            return
        aspect = self._active_seg_key(self.fmt_btns) or "16:9"
        count_key = self._active_seg_key(self.count_btns) or "1"
        try:
            count = int(count_key)
        except Exception:
            count = 1
        model_label = self.model_combo.current_label()   # читаемое имя для бейджа
        out_dir = root / "shows" / slug / "generator"
        is_video = (self._mode == "video")
        # Veo (flow-video-fast) — без duration; Omni — текущая длительность сегмента.
        duration_arg = None if model_id == "flow-video-fast" else self._duration
        if is_video:
            from generator.generator_video_thread import GeneratorVideoThread
        else:
            from generator.generator_thread import GeneratorImageThread
        # Прикреплённые рефы из prompt-bar (коммит 1 завёл UI/хранение, коммит 2
        # шлёт в payload["inputs"] для картинок). Копия списка — мутации в UI после
        # старта не влияют на уже запущенные потоки.
        refs = self.pending_refs()
        # ×N независимых параллельных генераций → N плиток. Pattern A: parent=None +
        # ссылка в списке. Захват своей ячейки и потока (default-arg → без late-binding):
        # каждая генерация заменит ИМЕННО свою плитку, даже при параллельных финишах.
        for _ in range(count):
            cell = self._add_cell(aspect)
            cell.set_model_label(model_label)   # бейдж виден сразу (loading) и далее
            # Метаданные плитки (in-memory): известное на старте. file/ts допишет
            # _on_gen_done по факту сохранённого файла. type фиксируем здесь —
            # _on_gen_done его НЕ переопределяет по расширению. refs — basenames
            # (не полные пути) → попадают в canvas.json через _save_canvas.
            cell.set_meta(prompt=prompt, model_id=model_id, model_label=model_label,
                          aspect=aspect, type=("video" if is_video else "image"),
                          refs=[Path(r).name for r in refs] if refs else [])
            if is_video:
                th = GeneratorVideoThread(prompt, aspect, model_id, duration_arg,
                                          out_dir, parent=None)
            else:
                th = GeneratorImageThread(prompt, aspect, model_id, out_dir,
                                          refs=refs, parent=None)
            self._gen_threads.append(th)
            th.finished.connect(lambda pth, c=cell, t=th: self._on_gen_done(c, t, pth))
            th.error.connect(lambda msg, c=cell, t=th: self._on_gen_fail(c, t, msg))
            th.start()
        # Генерация запущена → очистить поле промпта (prompt уже скопирован в локальную
        # переменную и в потоки выше; на их работу очистка не влияет). После цикла —
        # ранние return'ы (пустой промпт / нет сериала / нет модели) сюда не доходят.
        self.prompt_input.clear()
        self.clear_refs()   # рефы тоже относились к этой генерации → сброс UI/состояния

    # ── сетка результатов + параллельные плитки ───────────────────────
    def _add_cell(self, aspect: str = "16:9") -> ShimmerCell:
        """Loading-плитка. Ширина под формат (16:9 шире, 9:16 уже), высота ОБЩАЯ.
        Раскладка по рядам с группировкой по формату — в _relayout_grid."""
        if self._cell_count == 0:
            self._empty_host.hide()
            self._grid_host.show()
        w, h = self._cell_wh(aspect)
        cell = ShimmerCell(self, w, h, aspect=aspect)
        # В НАЧАЛО списка → новая генерация сверху, предыдущие съезжают вниз (как
        # Google Flow). _relayout_grid раскладывает строго по порядку _cells
        # (начало→верх); привязка thread↔cell по объекту (c=cell), не по индексу —
        # вставка в начало безопасна для параллельных ×N генераций.
        self._cells.insert(0, cell)
        self._cell_count += 1
        self._relayout_grid()   # перестроить ряды (включая новую плитку)
        return cell

    def _cell_wh(self, aspect: str):
        """Размер плитки. 16:9 → w16×H (как было). 9:16 → ширина подогнана под n_v штук
        в ряду (по режиму), высота производная — см. _vert_dims."""
        if aspect == "16:9":
            return self._cell_size(self._grid_cols)
        w_v, h_v, _ = self._vert_dims()
        return w_v, h_v

    def _vert_dims(self):
        """Размер плитки 9:16 + число в ряду. n_v ЖЁСТКО по режиму (N_VERT_BY_COLS);
        ширина = (доступная − отступы)//n_v (ровно n_v штук без дыры справа); высота
        производная w_v*16//9. Возвращает (w_v, h_v, n_v)."""
        spacing = 12
        n_v = N_VERT_BY_COLS.get(self._grid_cols, 8)
        try:
            vw = self._results_area.viewport().width()
        except Exception:
            vw = 0
        if vw <= 0:
            vw = self.width() or 1200
        avail = vw - 4
        w_v = max(60, (avail - (n_v - 1) * spacing) // n_v)
        return w_v, (w_v * 16 // 9), n_v

    def _cell_size(self, cols: int):
        """Размер ячейки 16:9 под N колонок и текущую ширину области результатов.
        Ширина — от viewport (динамически); фолбэк на ширину страницы/1200 пока
        viewport ещё 0 (до первого показа). Без щелей: N плиток ровно по ширине."""
        spacing = 12
        try:
            vw = self._results_area.viewport().width()
        except Exception:
            vw = 0
        if vw <= 0:
            vw = self.width() or 1200
        avail = vw - (cols - 1) * spacing - 4   # -4: запас под бордер/паддинг
        w = max(180, avail // cols)
        h = w * 9 // 16   # пропорция 16:9
        return w, h

    def _on_cols_change(self, key: str):
        """Клик S/M/L (ключ = число колонок): перестроить сетку под новое число.
        Без персистенции — размер всегда стартует с M (см. __init__)."""
        try:
            n = int(key)
        except Exception:
            return
        if n not in (2, 3, 4):
            return
        if n == self._grid_cols:
            self._sync_cols_seg()   # повторный клик — просто синхронизируем подсветку
            return
        self._grid_cols = n
        self._sync_cols_seg()
        self._relayout_grid()

    def _sync_cols_seg(self):
        """Подсветка активной кнопки cols-сегмента (property active + unpolish/polish)."""
        for k, b in self.cols_btns.items():
            b.setProperty("active", k == str(self._grid_cols))
            try:
                b.style().unpolish(b)
                b.style().polish(b)
            except Exception:
                pass

    def _relayout_grid(self):
        """Перераскладка по РЯДАМ с группировкой по формату. Подряд идущие плитки
        одного формата → один ряд; смена формата ИЛИ заполнение ряда → новый ряд.
        16:9 в ряду: _grid_cols (2/3/4), размер w16×H. 9:16 в ряду: n_v ЖЁСТКО по режиму
        (N_VERT_BY_COLS), ширина подогнана под n_v без дыры, высота производная (h_v).
        Ряды 9:16 выше рядов 16:9 — ОК (ряды независимы). Ячейки НЕ пересоздаются
        (таймеры/картинка живут) — открепляем от старых рядов и кладём заново."""
        spacing = 12
        cols16 = self._grid_cols
        w16, H = self._cell_size(cols16)
        w_v, h_v, n_v = self._vert_dims()   # 9:16: ширина под n_v штук, высота производная
        # 1) открепить живые ячейки (состояние/таймеры сохраняются)
        for cell in self._cells:
            cell.setParent(self._grid_host)
        # 2) снести старые ряды (row_host'ы; ячейки уже откреплены — не удалятся)
        while self._rows_v.count():
            it = self._rows_v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        # 3) сгруппировать ячейки в ряды по формату
        rows, cur, cur_fmt = [], [], None
        for cell in self._cells:
            fmt = cell.aspect()
            limit = cols16 if fmt == "16:9" else n_v
            if cur and (fmt != cur_fmt or len(cur) >= limit):
                rows.append(cur)
                cur = []
            cur.append(cell)
            cur_fmt = fmt
        if cur:
            rows.append(cur)
        # 4) построить ряды: row_host + QHBoxLayout, плитки слева, stretch справа
        for row in rows:
            rw = QWidget(self._grid_host)
            hb = QHBoxLayout(rw)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.setSpacing(spacing)
            for cell in row:
                if cell.aspect() == "16:9":
                    cell.set_size(w16, H)
                else:
                    cell.set_size(w_v, h_v)   # 9:16: своя ширина + производная высота
                hb.addWidget(cell, 0,
                             Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                cell.show()
            hb.addStretch(1)
            self._rows_v.addWidget(rw)

    def _on_gen_done(self, cell, th, path: str):
        try:
            if path.lower().endswith(".mp4"):
                cell.set_video_placeholder(path)   # видео: кадр-превью + ▶
            else:
                cell.set_image(path)
            # Дописать в meta факт файла: basename (не полный путь) + ts из имени
            # gen_<YYYYmmdd_HHMMSS>; не распарсилось → time.time(). type НЕ трогаем
            # (он уже стоит из _on_run).
            import os, re, time
            fname = os.path.basename(path)
            m = re.search(r"(\d{8}_\d{6})", fname)
            cell.set_meta(file=fname, ts=(m.group(1) if m else time.time()))
            # Персист холста на диск (под-шаг 2): только при успешном финале —
            # внутри try, после дописи file/ts. Сам _save_canvas тоже guard'нут.
            self._save_canvas()
        except Exception:
            pass
        if th in self._gen_threads:
            self._gen_threads.remove(th)

    def _on_gen_fail(self, cell, th, msg: str):
        # Причина — на плитке (постоянной строки статуса нет).
        try:
            cell.set_error((msg or "Ошибка")[:160])
        except Exception:
            pass
        if th in self._gen_threads:
            self._gen_threads.remove(th)

    # ── персист холста на диск (под-шаг 2: ТОЛЬКО запись) ──────────────
    def _save_canvas(self):
        """Записать текущий холст в shows/<slug>/generator/canvas.json.

        ТОЛЬКО запись — чтение/восстановление плиток это под-шаг 3. Порядок
        self._cells сохраняется (начало списка = верх холста = новое сверху).
        Пишем только плитки с готовым файлом (meta['file']) — loading/error без
        файла пропускаем. Атомарно: .tmp в той же папке → os.replace (атомарен на
        Mac и Win, т.к. tmp и финал на одном томе). Ошибка записи НЕ роняет
        генерацию — молча проглатываем (опц. лог в stderr). Зовётся из _on_gen_done
        на main-потоке (сигналы QThread queued в event loop) → вызовы при ×N
        сериализованы, гонки за canvas.json.tmp нет."""
        try:
            import json, os
            import storyboard_app as _sa
            root = _sa.get_stored_root()
            slug = _sa.get_current_show(root) if root else None
            if not root or not slug:
                return
            out_dir = root / "shows" / slug / "generator"
            cells = [c.meta() for c in self._cells if c.meta().get("file")]
            data = {"version": 1, "cells": cells}
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out_dir / "canvas.json.tmp"
            final = out_dir / "canvas.json"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, final)
        except Exception as e:  # noqa: BLE001 — персист необязателен, не валим генерацию
            import sys
            print(f"[generator] canvas save failed: {e}", file=sys.stderr)

    # ── чтение/восстановление холста (под-шаг 3) ──────────────────────
    def _clear_canvas(self):
        """Полная очистка холста: снять все плитки с экрана и shimmer-такта,
        обнулить _cells/_cell_count, снести ряды-контейнеры, вернуть пустое
        состояние. Зовётся перед перечитыванием под новый сериал (reload_canvas)."""
        for cell in self._cells:
            try:
                self.unregister_loading(cell)   # снять с общего shimmer (если была loading)
            except Exception:
                pass
            try:
                cell.setParent(None)
                cell.deleteLater()
            except Exception:
                pass
        self._cells = []
        self._cell_count = 0
        # снести ряды-контейнеры (тот же механизм, что в _relayout_grid; плитки уже
        # откреплены выше — deleteLater рядов их не заденет)
        while self._rows_v.count():
            it = self._rows_v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._grid_host.hide()
        self._empty_host.show()

    def _load_canvas(self):
        """Прочитать canvas.json активного сериала и восстановить плитки.

        Холст у каждого сериала свой. Нет root/slug/файла, битый JSON → холст
        остаётся пустым (НЕ ошибка). Плитки добавляются APPEND в порядке файла
        (= порядок _cells, новое сверху) — НЕ через _add_cell (там insert(0)
        перевернул бы порядок). Запись, чей файл удалён с диска, пропускаем молча.
        Каждая запись обёрнута в try/except → битая запись не валит весь restore."""
        try:
            import json
            import storyboard_app as _sa
            root = _sa.get_stored_root()
            slug = _sa.get_current_show(root) if root else None
            if not root or not slug:
                return
            out_dir = root / "shows" / slug / "generator"
            cj = out_dir / "canvas.json"
            if not cj.exists():
                return
            try:
                data = json.loads(cj.read_text(encoding="utf-8"))
            except Exception:
                return   # битый JSON → пустой холст, не падаем
            cells = data.get("cells") if isinstance(data, dict) else None
            if not isinstance(cells, list):
                return
            for meta in cells:
                try:
                    if not isinstance(meta, dict):
                        continue
                    fname = meta.get("file")
                    if not fname:
                        continue
                    full = out_dir / fname
                    if not full.exists():
                        continue   # файл удалён юзером — пропускаем запись молча
                    aspect = meta.get("aspect", "16:9")
                    w, h = self._cell_wh(aspect)
                    cell = ShimmerCell(self, w, h, aspect=aspect)
                    cell.set_model_label(meta.get("model_label", ""))
                    cell.set_meta(**meta)   # вернуть метаданные (иначе save затрёт)
                    if meta.get("type") == "video":
                        cell.set_video_placeholder(str(full))
                    else:
                        cell.set_image(str(full))
                    self._cells.append(cell)   # APPEND → порядок 1:1 как в файле
                except Exception:
                    continue   # одна битая запись не валит весь restore
            self._cell_count = len(self._cells)
            if self._cells:
                self._empty_host.hide()
                self._grid_host.show()
                self._relayout_grid()   # ОДИН раз в конце (не в цикле)
        except Exception as e:  # noqa: BLE001 — restore необязателен, не валим страницу
            import sys
            print(f"[generator] canvas load failed: {e}", file=sys.stderr)

    def reload_canvas(self):
        """Публичный: перечитать холст под активный сериал (для смены сериала).
        Хук в storyboard_app._on_show_changed — отдельной правкой. Рефы старого
        шоу не релевантны → очищаем перед сменой содержимого холста."""
        self.clear_refs()
        self._clear_canvas()
        self._load_canvas()

    def set_prompt(self, text: str):
        """Положить text в поле промпта, ЗАМЕНЯЯ текущее содержимое. Зовётся
        плиткой по клику на 'вернуть промпт' (btn_back). setPlainText т.к.
        prompt_input.setAcceptRichText(False) — plain-text режим."""
        try:
            self.prompt_input.setPlainText(text or "")
            self.prompt_input.setFocus()
        except Exception:
            pass

    # ── прикреплённые рефы (per-session) ──────────────────────────────
    def _make_ref_thumb(self, file_path: str) -> QFrame:
        """Создать превьюшку 64×64 для прикреплённого рефа: rounded-clip pixmap
        + крестик в углу (remove). Для видео ищет парный .jpg рядом; если нет —
        тёмная заглушка с ▶. Pixmap клипится заранее через QPainter (QLabel не
        клипит pixmap по border-radius)."""
        from pathlib import Path
        VID = {".mp4", ".mov", ".m4v", ".webm"}
        thumb = QFrame(self._refs_row)
        thumb.setObjectName("ref-thumb")
        thumb.setFixedSize(64, 64)
        ext = Path(file_path).suffix.lower()
        src = file_path
        is_video = ext in VID
        if is_video:
            jpg = str(Path(file_path).with_suffix(".jpg"))
            src = jpg if Path(jpg).exists() else None
        if src is not None:
            pix = QPixmap(src)
            if pix.isNull():
                src = None
        if src is not None:
            base = pix.scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            clipped = QPixmap(64, 64)
            clipped.fill(Qt.GlobalColor.transparent)
            p = QPainter(clipped)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(0.0, 0.0, 64.0, 64.0, 6.0, 6.0)
            p.setClipPath(path)
            x = (64 - base.width()) // 2
            y = (64 - base.height()) // 2
            p.drawPixmap(int(x), int(y), base)
            p.end()
            lbl = QLabel(thumb)
            lbl.setPixmap(clipped)
            lbl.setGeometry(0, 0, 64, 64)
        else:
            # Видео без парного кадра (или нечитаемый файл) → ▶-заглушка.
            lbl = QLabel("▶", thumb)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                "color:#ffffff; font-size:18px;"
                " background:#161020; border-radius:6px;")
            lbl.setGeometry(0, 0, 64, 64)
        # Крестик-кнопка в правом верхнем углу (отступ 2px). "✕" (U+2715) —
        # симметричнее обычного "×" и центрируется в кнопке.
        x_btn = QPushButton("✕", thumb)
        x_btn.setObjectName("ref-thumb-x")
        x_btn.setFixedSize(18, 18)
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.move(64 - 18 - 2, 2)
        x_btn.clicked.connect(
            lambda _checked=False, fp=file_path: self.remove_ref(fp))
        x_btn.raise_()
        x_btn.setVisible(False)        # видимый только при hover на thumb
        # Ссылки для eventFilter (Enter/Leave → показать/скрыть ✕ + popup).
        thumb._x_btn = x_btn
        thumb._file_path = file_path
        thumb.installEventFilter(self)
        return thumb

    def _show_thumb_popup(self, thumb):
        """Показать увеличенную превьюшку файла рефа над thumb'ом (306 по большой
        стороне, KeepAspectRatio, rounded-clipped) с fade-in 150мс EaseOutCubic.
        Ленивая инициализация одного QLabel(self) — переиспользуется для всех
        thumb'ов. Для видео используется парный .jpg рядом (как в _make_ref_thumb);
        если нет — тёмная ▶-заглушка. Прозрачность через QGraphicsOpacityEffect
        (windowOpacity не работает для child-виджетов)."""
        from pathlib import Path
        VID = {".mp4", ".mov", ".m4v", ".webm"}
        file_path = getattr(thumb, "_file_path", "")
        if not file_path:
            return
        # Источник pixmap (тот же выбор, что и в _make_ref_thumb).
        ext = Path(file_path).suffix.lower()
        src = file_path
        if ext in VID:
            jpg = str(Path(file_path).with_suffix(".jpg"))
            src = jpg if Path(jpg).exists() else None
        pix = None
        if src is not None:
            p0 = QPixmap(src)
            if not p0.isNull():
                pix = p0
        # Ленивая сборка popup (QLabel parented к странице) + opacity-effect.
        if self._thumb_popup is None:
            self._thumb_popup = QLabel(self)
            self._thumb_popup.setObjectName("ref-thumb-popup")
            self._thumb_popup.setVisible(False)
            # QGraphicsOpacityEffect — для fade-анимаций child-виджета
            # (windowOpacity у нетоп-левел Qt игнорирует).
            eff = QGraphicsOpacityEffect(self._thumb_popup)
            eff.setOpacity(1.0)
            self._thumb_popup.setGraphicsEffect(eff)
            self._thumb_popup._opacity_effect = eff
        popup = self._thumb_popup
        # Рендер pixmap → масштаб до 306 по большой стороне → rounded-clip 8px.
        if pix is not None:
            scaled = pix.scaled(
                306, 306,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            w, h = scaled.width(), scaled.height()
            clipped = QPixmap(w, h)
            clipped.fill(Qt.GlobalColor.transparent)
            painter = QPainter(clipped)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(0.0, 0.0, float(w), float(h), 8.0, 8.0)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, scaled)
            painter.end()
            popup.setPixmap(clipped)
            popup.setFixedSize(w, h)
        else:
            # ▶-заглушка для видео без парного .jpg: тёмный квадрат 306×306 с ▶.
            popup.setPixmap(QPixmap())
            popup.setText("▶")
            popup.setAlignment(Qt.AlignmentFlag.AlignCenter)
            popup.setStyleSheet(
                "QLabel#ref-thumb-popup { background:#161020; color:#ffffff;"
                " font-size:48px; border-radius:8px;"
                " border:1px solid rgba(255,255,255,0.25); }")
            popup.setFixedSize(306, 306)
        # Позиция: над thumb'ом по центру. Если не влезает — под thumb.
        try:
            tl = thumb.mapTo(self, QPoint(0, 0))
        except Exception:
            return
        gap = 8
        px = tl.x() + thumb.width() // 2 - popup.width() // 2
        py = tl.y() - popup.height() - gap
        if py < 4:
            py = tl.y() + thumb.height() + gap   # не влезло сверху → снизу
        # Кламп в окно страницы.
        px = max(4, min(px, self.width() - popup.width() - 4))
        py = max(4, min(py, self.height() - popup.height() - 4))
        popup.move(px, py)
        # Стоп предыдущей анимации (если ещё бежит — например hide→show подряд).
        if self._thumb_anim is not None:
            try: self._thumb_anim.stop()
            except Exception: pass
        # Fade-in: opacity 0 → 1 за 150мс EaseOutCubic.
        eff = popup._opacity_effect
        eff.setOpacity(0.0)
        popup.show()
        popup.raise_()
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(150)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._thumb_anim = anim   # держим ссылку, иначе GC
        anim.start()

    def _hide_thumb_popup(self):
        """Скрыть popup увеличения с fade-out 120мс EaseInCubic. Зовётся из
        Leave-ивента thumb'а и из remove_ref/clear_refs (иначе popup мог бы
        повиснуть после удаления thumb'a до того как Leave успеет прийти)."""
        if self._thumb_popup is None or not self._thumb_popup.isVisible():
            return
        popup = self._thumb_popup
        eff = getattr(popup, "_opacity_effect", None)
        # Если эффекта нет (legacy) — простой hide без анимации.
        if eff is None:
            popup.hide()
            return
        # Стоп предыдущей анимации (могла быть show-анимация в процессе).
        if self._thumb_anim is not None:
            try: self._thumb_anim.stop()
            except Exception: pass
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(120)
        anim.setStartValue(eff.opacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(popup.hide)
        self._thumb_anim = anim
        anim.start()

    def add_ref(self, file_path: str):
        """Прикрепить файл к следующей генерации. Дубликат — тихий выход."""
        file_path = str(file_path or "").strip()
        if not file_path or file_path in self._pending_refs:
            return
        self._pending_refs.append(file_path)
        thumb = self._make_ref_thumb(file_path)
        self._ref_thumbs[file_path] = thumb
        self._refs_row_lay.addWidget(thumb)
        self._refs_row.setVisible(True)

    def add_ref_from_meta(self, meta: dict):
        """Прикрепить файл плитки (по её _meta) как реф к следующей генерации.
        Резолвит полный путь из meta['file'] через текущий root/slug:
            shows/<slug>/generator/<file>
        Зовётся плиткой (btn_ref → image-plus). Любая проблема (нет root/slug,
        пустой file, файла нет на диске) → тихий выход. Для video-плитки плитка
        ПРЕДВАРИТЕЛЬНО подменяет meta['file'] на парный .jpg-кадр (см.
        result_cell._on_ref_clicked) — сюда уже приходит .jpg-имя или original."""
        if not isinstance(meta, dict):
            return
        fname = (meta.get("file") or "").strip()
        if not fname:
            return
        try:
            import storyboard_app as _sa
            root = _sa.get_stored_root()
            slug = _sa.get_current_show(root) if root else None
            if not (root and slug):
                return
            full = root / "shows" / slug / "generator" / fname
            if not full.exists():
                return
            self.add_ref(str(full))
        except Exception:
            pass

    def remove_ref(self, file_path: str):
        """Открепить файл по пути. Если список опустел — скрыть ряд."""
        if file_path not in self._pending_refs:
            return
        # Popup мог быть открыт по этому thumb'у — скрыть до deleteLater,
        # иначе он повиснет (Leave не успеет прийти).
        self._hide_thumb_popup()
        self._pending_refs.remove(file_path)
        thumb = self._ref_thumbs.pop(file_path, None)
        if thumb is not None:
            try:
                thumb.setParent(None)
                thumb.deleteLater()
            except Exception:
                pass
        if not self._pending_refs:
            self._refs_row.setVisible(False)

    def clear_refs(self):
        """Очистить все прикреплённые рефы (вызывается из _on_run после запуска,
        и из reload_canvas при смене сериала — рефы старого шоу не релевантны)."""
        self._hide_thumb_popup()
        for _p, thumb in list(self._ref_thumbs.items()):
            try:
                thumb.setParent(None)
                thumb.deleteLater()
            except Exception:
                pass
        self._ref_thumbs.clear()
        self._pending_refs.clear()
        self._refs_row.setVisible(False)

    def pending_refs(self) -> list:
        """Копия списка прикреплённых путей — для коммита 2 (передача в потоки)."""
        return list(self._pending_refs)

    # ── общий такт «дыхания» плиток (один таймер на страницу) ─────────
    # 2026-06-20 (Этап 3): бегущий блик заменён на ЧИСТУЮ ПУЛЬСАЦИЯ яркости
    # базы (см. ShimmerCell.paintEvent). Угол [0, 2π) шлётся всем плиткам — они
    # дышат СИНХРОННО (единый ансамбль). Бесшовно по определению (sin непрерывен).
    # Адаптивный fps: чем больше плиток грузятся, тем реже репейнт (CPU на слабых Win).
    #   ≤3 cells  → 30 fps (33мс) — плавно, дыхание не пикселит
    #   4–7 cells → 15 fps (67мс)
    #   >7 cells  → 10 fps (100мс)
    # Длительность ОДНОГО цикла дыхания ≈ const (_SHIMMER_CYCLE_SEC),
    # шаг угла подстраивается под fps → визуальная скорость одинакова при любой нагрузке.
    _SHIMMER_CYCLE_SEC = 3.0   # 2026-06-20: чуть медленнее (премиальный темп пульсации)

    @staticmethod
    def _shimmer_fps_for(n_cells: int) -> int:
        if n_cells <= 3:
            return 30
        if n_cells <= 7:
            return 15
        return 10

    def _shimmer_retune(self):
        """Подстроить интервал таймера + шаг угла под текущее число loading-плиток.
        Длительность цикла фиксирована; визуально скорость не меняется."""
        if self._shimmer_timer is None:
            return
        fps = self._shimmer_fps_for(len(self._loading_cells))
        self._shimmer_timer.setInterval(int(1000 / fps))
        # Шаг угла = 2π / (fps · cycle_sec) ⇒ цикл всегда ≈ _SHIMMER_CYCLE_SEC.
        self._shimmer_step = (2.0 * 3.141592653589793) / (fps * self._SHIMMER_CYCLE_SEC)

    def register_loading(self, cell):
        self._loading_cells.add(cell)
        if self._shimmer_timer is None:
            self._shimmer_timer = QTimer(self)
            self._shimmer_timer.timeout.connect(self._shimmer_tick)
        self._shimmer_retune()
        if not self._shimmer_timer.isActive():
            self._shimmer_timer.start()

    def unregister_loading(self, cell):
        self._loading_cells.discard(cell)
        if not self._loading_cells:
            if self._shimmer_timer is not None:
                self._shimmer_timer.stop()
            return
        # Ещё есть грузящиеся — пересчитать fps (одной меньше).
        self._shimmer_retune()

    def _shimmer_tick(self):
        # Угол по кругу [0, 2π); wrap дёшевый, бесшовный по построению (см. paintEvent).
        # Репейнт ТОЛЬКО видимых loading-плиток.
        step = getattr(self, "_shimmer_step", 0.167)
        self._shimmer_phase += step
        TWO_PI = 6.283185307179586
        if self._shimmer_phase >= TWO_PI:
            self._shimmer_phase -= TWO_PI
        ang = self._shimmer_phase
        for cell in list(self._loading_cells):
            try:
                if cell.isVisible():
                    cell.set_phase(ang)
            except Exception:
                pass
