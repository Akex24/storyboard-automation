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

from typing import Optional

from PyQt6.QtCore import Qt, QSize, QTimer, QSettings, QEvent, QPoint
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QTextEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
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
        # Размер плиток: ВСЕГДА стартуем с M (3 колонки), игнорируя прошлые сессии
        # (по требованию). Переключение S/M/L работает в рамках сессии через _grid_cols.
        self._grid_cols = 3
        self._build_ui()

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
        ресайзе окна). Только на смену ширины → без рекурсии с setFixedHeight."""
        if obj is getattr(self, "prompt_input", None) and ev.type() == QEvent.Type.Resize:
            w = self.prompt_input.viewport().width()
            if w != getattr(self, "_last_prompt_w", -1):
                self._last_prompt_w = w
                self._adjust_prompt_height()
        return super().eventFilter(obj, ev)

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
        # ×N независимых параллельных генераций → N плиток. Pattern A: parent=None +
        # ссылка в списке. Захват своей ячейки и потока (default-arg → без late-binding):
        # каждая генерация заменит ИМЕННО свою плитку, даже при параллельных финишах.
        for _ in range(count):
            cell = self._add_cell(aspect)
            cell.set_model_label(model_label)   # бейдж виден сразу (loading) и далее
            if is_video:
                th = GeneratorVideoThread(prompt, aspect, model_id, duration_arg,
                                          out_dir, parent=None)
            else:
                th = GeneratorImageThread(prompt, aspect, model_id, out_dir, parent=None)
            self._gen_threads.append(th)
            th.finished.connect(lambda pth, c=cell, t=th: self._on_gen_done(c, t, pth))
            th.error.connect(lambda msg, c=cell, t=th: self._on_gen_fail(c, t, msg))
            th.start()

    # ── сетка результатов + параллельные плитки ───────────────────────
    def _add_cell(self, aspect: str = "16:9") -> ShimmerCell:
        """Loading-плитка. Ширина под формат (16:9 шире, 9:16 уже), высота ОБЩАЯ.
        Раскладка по рядам с группировкой по формату — в _relayout_grid."""
        if self._cell_count == 0:
            self._empty_host.hide()
            self._grid_host.show()
        w, h = self._cell_wh(aspect)
        cell = ShimmerCell(self, w, h, aspect=aspect)
        self._cells.append(cell)
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
                cell.set_video_placeholder(path)   # видео: ▶ на тёмном фоне (кадр — кусок 3)
            else:
                cell.set_image(path)
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
