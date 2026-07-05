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
    Qt, QSize, QTimer, QSettings, QEvent, QPoint, QRect,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve,
)
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QImageReader, QColor, QFont, QFontMetrics, QPen
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QToolButton, QComboBox, QTextEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QSizePolicy,
    QGraphicsOpacityEffect, QMessageBox, QApplication, QCheckBox,
)

from generator.result_cell import ShimmerCell, resolve_existing_path
from generator.model_select import ModelSelect
from i18n import tr   # локализация UI (i18n — лист-модуль, без circular import)


# Модели по режиму: (отображаемое имя, внутренний id для payload["model"]).
# id реальные из /api/v5/models. ВНИМАНИЕ: генерация видео пока НЕ подключена —
# в _on_run режим "video" возвращает «Видео скоро будет» ДО использования id.
MODELS_BY_MODE = {
    "image": [("Nano Banana 2", "nano-banana-2"),
              ("Nano Banana 2 Lite", "nano-banana-2-lite"),
              ("OpenAI", "openai-image")],
    "video": [("Veo 3.1 Fast (8s)", "flow-video-fast"),
              ("Veo 3.1 Light (8s)", "flow-video-light"),
              ("Omni Flash", "flow-video-omni-flash")],
}

# Число плиток 9:16 в ряду — ЖЁСТКО по режиму (_grid_cols). Ширина 9:16 подгоняется
# под доступную ширину так, чтобы ровно n_v штук легли без дыры справа; высота 9:16 —
# производная (w*16//9, выше 16:9). 16:9 при этом не меняется (как было).
N_VERT_BY_COLS = {2: 5, 3: 8, 4: 11}


class _RunButton(QToolButton):
    """Кнопка отправки с КАСТОМНОЙ отрисовкой (paintEvent). На macOS крупный нативный
    QPushButton/QToolButton рисуется серым бевелом и игнорирует QSS-фон — поэтому рисуем
    сами: золотой скруглённый квадрат + стрелка ↑ по центру. Цвет ВСЕГДА один (#cfff24 —
    золотой, как раньше были активные сегменты), БЕЗ смены на hover."""

    _BG = "#cfff24"   # ЗОЛОТОЙ ВСЕГДА (тот яркий, что был у активных сегментов); hover НЕ меняет
    _FG = "#15101e"

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 14.0, 14.0)
        p.fillPath(path, QColor(self._BG))
        f = QFont()
        f.setPixelSize(26)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self._FG))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "↑")
        p.end()


class _DragBorderOverlay(QWidget):
    """Прозрачный оверлей ПОВЕРХ пиксмапа тумбы рефа: когда тумба — цель drag-to-swap,
    рисует ТОЛСТУЮ ПУНКТИРНУЮ белую скруглённую рамку (QPainter в paintEvent — надёжно на
    macOS, в отличие от QSS dashed+radius). Иначе прозрачен. Мышь не ловит
    (WA_TransparentForMouseEvents ставится при создании) → drag/hover не мешает."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False

    def set_active(self, on: bool):
        on = bool(on)
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, ev):
        if not self._on:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(255, 255, 255, 235))
        pen.setWidth(3)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        ins = 2.0   # чтобы 3px штрих не обрезался краем виджета
        p.drawRoundedRect(ins, ins, self.width() - 2 * ins, self.height() - 2 * ins, 6.0, 6.0)
        p.end()


class GeneratorPage(QWidget):
    """Каркас страницы генератора. Заглушки без логики (этот шаг — только UI)."""

    _PROMPT_MIN_H = 46      # минимум поля промпта (1-2 строки)
    _PROMPT_MAX_H = None    # потолок = 20 строк, считается лениво от ЖИВОГО шрифта поля
    _PROMPT_BAR_MAX_W = 1000  # жёсткий максимум ширины промпт-бара (компактный по центру,
                             # ≈ стандартный запуск); шире окно — бар не растягивается

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("generator-page")
        self.setAcceptDrops(True)   # drag-and-drop файлов с диска в холст (см. dropEvent)
        self._mode = "image"   # активный режим (пока статично, без переключения)
        self._duration = 8     # длительность видео (сек); видна только для Omni Flash
        # Параллельные генерации: список активных потоков (Pattern A: parent=None
        # + ссылка тут, Qt не соберёт; убираются по finished/error). Кнопку НЕ блокируем.
        self._gen_threads = []
        self._upscale_threads = []  # 2026-06-25 (апскейл): фоновые UpscaleThread
        # Общий shimmer-такт (~20fps) для всех loading-плиток; стоит в простое.
        self._loading_cells = set()
        self._shimmer_phase = 0.0
        self._shimmer_timer = None
        # Счётчик заполненных ячеек + ссылки на ВСЕ плитки (для перераскладки).
        self._cell_count = 0
        self._cells = []
        # 2026-06-28: ГЛОБАЛЬНЫЙ mute звука видео (всё приложение, все холсты/сериалы).
        # Хранится в QSettings (переживает перезапуск), НЕ привязан к проекту/странице.
        self._video_muted = bool(QSettings().value("generator/video_muted", False, type=bool))
        # Мультихолст (КУСОК 1 — только данные/хранение; таб-бар/переключение —
        # куски 2-3). self._cells = плитки АКТИВНОГО холста (как раньше). Секции
        # холстов: [{id,title,cells:[<meta>...]}]; cells активного синкаются из
        # self._cells при сохранении. canvas.json v2 хранит все холсты.
        self._canvases: list = []
        self._active_canvas_id = None
        # Путь A: ЖИВЫЕ виджеты плиток per-canvas {canvas_id: [ShimmerCell...]}.
        # Переключение холста НЕ уничтожает плитки (иначе обрывалась бы идущая
        # генерация — поток держит плитку), а ПРЯЧЕТ их и показывает плитки целевого.
        # Инвариант: _canvas_cells[active] — ТОТ ЖЕ список-объект что self._cells.
        self._canvas_cells: dict = {}
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
        # Таб-бар уже построен в _build_ui (дефолтный чип) — пересобрать под реально
        # загруженные холсты (могло быть несколько после миграции/v2).
        self._rebuild_canvas_row()

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
            # Холст-вкладки (браузерный тип): активная — как header tab-pill.
            "QFrame#canvas-chip { background:transparent; border:none;"
            " border-top-left-radius:9px; border-top-right-radius:9px;"
            " border-bottom:2px solid transparent; }"
            "QFrame#canvas-chip:hover { background:#191b1d; }"
            "QFrame#canvas-chip[active=\"true\"] { background:#242628;"
            " border-bottom:2px solid transparent; }"
            "QLabel#canvas-title { color:rgba(255,255,255,0.5); font-size:12px;"
            " background:transparent; }"
            "QFrame#canvas-chip[active=\"true\"] QLabel#canvas-title { color:#e8b86a; }"
            "QPushButton#canvas-new { background:transparent; color:#7a7488;"
            " border:none; border-radius:8px; padding:0px; font-size:18px; }"
            "QPushButton#canvas-new:hover { color:#cfcfcf; background:#191b1d; }"
            "QPushButton#canvas-new:disabled { color:#4a4458; background:transparent; }"
            "QPushButton#canvas-fav { background:transparent; border:none;"
            " border-radius:8px; padding:0 10px; }"
            "QPushButton#canvas-fav:hover { background:#191b1d; }"
            "QFrame#canvas-divider { background:#2a2438; border:none; }"
            "QPushButton#canvas-close { background:transparent; border:none;"
            " border-radius:4px; }"
            "QPushButton#canvas-close:hover { background:#2c2f31; }"
            # Ряд прикреплённых рефов в prompt-bar (показывается выше поля ввода)
            "QWidget#refs-row { background:transparent; }"
            "QFrame#ref-thumb { background:#1a1428;"
            " border:1px solid rgba(255,255,255,0.15); border-radius:6px; }"
            # drag-to-swap: подсветка тумбы-цели рисуется _DragBorderOverlay (paintEvent),
            # НЕ QSS — пунктир+радиус на macOS QSS ненадёжен.
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
            "QFrame#prompt-bar { background:#191b1d; border:1px solid rgba(255,255,255,0.055);"
            " border-radius:18px; }"
            "QTextEdit#prompt-input { background:transparent; color:#fff;"
            " border:none; padding:10px 14px 10px 0px;"
            " font-size:14px; }"
            "QLabel#group-cap { color:#888; font-size:10px;"
            " background:transparent; }"
            # Сегмент-кнопки (режим / формат / кол-во)
            "QFrame#seg-group { background:#131516; border:none;"
            " border-radius:10px; }"
            "QPushButton#seg { background:transparent; color:rgba(255,255,255,0.55);"
            " border:none; border-radius:6px; padding:0px 11px; font-size:12px; }"
            "QPushButton#seg:hover { color:rgba(255,255,255,0.85); }"
            "QPushButton#seg[active=\"true\"] { background:#e8b86a;"
            " color:#101208; border-radius:7px; font-weight:700; }"
            # Акцентный сегмент (режим Картинка/Видео): активная — янтарь
            "QPushButton#seg-accent { background:transparent;"
            " color:rgba(255,255,255,0.55); border:none; border-radius:6px;"
            " padding:0px 11px; font-size:12px; }"
            "QPushButton#seg-accent:hover { color:rgba(255,255,255,0.85); }"
            "QPushButton#seg-accent[active=\"true\"] { background:#e8b86a;"
            " color:#101208; border-radius:7px; font-weight:700; }"
            # Выпадашка моделей
            "QComboBox#model-combo { background:#131516; color:#fff;"
            " border:1px solid rgba(255,255,255,0.055); border-radius:10px; padding:7px 12px;"
            " font-size:13px; min-width:150px; }"
            "QComboBox#model-combo QAbstractItemView { background:#221b2e;"
            " border:1px solid #322a40; border-radius:12px; padding:6px;"
            " outline:none; color:#e8e3f0; font-size:13px;"
            " selection-background-color:#2c2438; }"
            "QComboBox#model-combo QAbstractItemView::item {"
            " padding:8px 10px; border-radius:8px; }"
            # Кнопка запуска (акцент — янтарь, как вкладка)
            "QToolButton#run-btn { background:#d4a256; color:#15101e;"
            " border:none; border-radius:14px;"
            " font-size:26px; font-weight:600; }"
            "QToolButton#run-btn:hover { background:#e8b86a; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        root.addWidget(self._build_canvas_row())
        root.addWidget(self._build_results_area(), stretch=1)
        # Промпт-бар — КОМПАКТНЫЙ по центру, НЕ растягивается на всю ширину окна. Жёсткий
        # максимум _PROMPT_BAR_MAX_W; по бокам stretch'и → бар по центру, лишнее в поля.
        # Окно уже максимума → бар сжимается с окном (это максимум, не fixed).
        _bar_row = QHBoxLayout()
        _bar_row.setContentsMargins(0, 0, 0, 0)
        _bar_row.addStretch(1)
        _bar_row.addWidget(self._build_prompt_bar(), 1000)
        _bar_row.addStretch(1)
        self._prompt_bar.setMaximumWidth(self._PROMPT_BAR_MAX_W)
        root.addLayout(_bar_row)
        self._update_duration_visibility()   # стартово скрыт (дефолт — режим image)

    # ── (B) ряд холстов — вкладки браузерного типа (только ВИД; логика — заход 2) ──
    def _build_canvas_row(self) -> QWidget:
        # Обёртка: ряд вкладок + тонкая линия-разделитель под ним.
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Ряд вкладок (chips + «+»), прижат к низу. Содержимое строит
        # _rebuild_canvas_row из self._canvases — layout храним для пересборки
        # (после add/switch/delete).
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.setAlignment(Qt.AlignmentFlag.AlignBottom)
        self._canvas_row_lay = row
        outer.addLayout(row)
        # Линия-разделитель 1px (активная вкладка разрывает её янтарным подчёркиванием).
        divider = QFrame()
        divider.setObjectName("canvas-divider")
        divider.setFixedHeight(1)
        outer.addWidget(divider)
        self._rebuild_canvas_row()   # первичное наполнение чипами + «+»
        return wrap

    def _rebuild_canvas_row(self):
        """Перерисовать ряд вкладок из self._canvases (активная подсвечена). Сносит
        старое содержимое ряда (чипы + «+») и строит заново. Зовётся после load /
        add / switch (и delete в куске 3)."""
        lay = getattr(self, "_canvas_row_lay", None)
        if lay is None:
            return
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._ensure_canvases()
        for c in self._canvases:
            chip = self._canvas_chip(c, active=(c.get("id") == self._active_canvas_id))
            lay.addWidget(chip)
        new_btn = QPushButton("+")
        new_btn.setObjectName("canvas-new")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setFixedSize(32, 34)
        new_btn.clicked.connect(self._add_canvas)   # активна (КУСОК 2)
        lay.addWidget(new_btn)
        lay.addStretch()
        # Кнопка «Избранное» — прижата к ПРАВОМУ краю ряда (после stretch), симметрично
        # левым чипам холстов. Иконка heart, стиль как canvas-new. Клик → окно избранного.
        fav_btn = QPushButton()
        fav_btn.setObjectName("canvas-fav")
        fav_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fav_btn.setFixedHeight(34)
        try:
            from storyboard_app import get_icon
            fav_btn.setIcon(get_icon("heart"))
            fav_btn.setIconSize(QSize(16, 16))
        except Exception:
            fav_btn.setText("♥")
        fav_btn.setToolTip(tr('gen_favorites'))
        fav_btn.clicked.connect(self._open_favorites)
        lay.addWidget(fav_btn)

    def _open_favorites(self):
        """Открыть окно «Избранное» (сетка избранных карточек текущего сериала).
        Ссылку держим на странице (анти-GC, как _open_viewer в result_cell)."""
        try:
            from generator.favorites_dialog import FavoritesDialog
            dlg = FavoritesDialog(self)
            self._favorites_dialog = dlg   # анти-GC
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            import traceback
            traceback.print_exc()

    def _canvas_chip(self, canvas: dict, active: bool) -> QFrame:
        chip = QFrame()
        chip.setObjectName("canvas-chip")
        chip.setProperty("active", active)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.setFixedHeight(34)   # вровень с сегментами нижней панели (#seg = 34)
        chip._canvas_id = canvas.get("id")    # для клика (см. eventFilter)
        chip.installEventFilter(self)         # ЛКМ-релиз по чипу → _switch_canvas
        lay = QHBoxLayout(chip)
        # ✕ стоит на АКТИВНОЙ вкладке (как в браузере) → у неё резервируем место
        # справа под крестик (✕ — абсолютный дочерний поверх правого края, не в layout).
        lay.setContentsMargins(16, 0, 28 if active else 16, 0)
        lay.setSpacing(8)
        # Отображаемое имя вкладки — ВСЕГДА по номеру из id «cN» (локализовано),
        # сохранённый title в canvas.json НЕ используется/НЕ переписывается (язык не
        # запекаем в данные; ручного переименования нет — подтверждено).
        _cid = canvas.get("id", "")
        _cn = int(_cid[1:]) if isinstance(_cid, str) and _cid[1:].isdigit() else 1
        lbl = QLabel(tr('gen_canvas', n=_cn))
        lbl.setObjectName("canvas-title")
        # клик по тексту должен уйти ЧИПУ (не съедаться лейблом).
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(lbl)
        if active:
            # Круглый ✕ ТОЧНО как на ref-thumb (objectName "ref-thumb-x": тёмный
            # кружок border-radius:9, белый ✕, hover-затемнение). АБСОЛЮТНЫЙ дочерний
            # chip (НЕ в layout, чтобы не дёргать ширину). ВСЕГДА виден на активной
            # вкладке (как закрытие таба в браузере). Позиция — по Resize в eventFilter.
            close = QPushButton("✕", chip)
            close.setObjectName("ref-thumb-x")
            close.setFixedSize(18, 18)
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            # обработчик удаления — КУСОК 3 (рендерим вид + позицию, клик НЕ вешаем).
            chip._close_btn = close
        return chip

    # ── мультихолст: добавление/переключение (КУСОК 2) ────────────────
    def _add_canvas(self):
        """«+»: создать новый ПУСТОЙ холст и сразу переключиться на него. id/номер —
        c<N>, N = max существующий индекс + 1 (НЕ count: после удаления номера НЕ
        переиспользуются). id и отображаемый номер связаны: c<N> ↔ «Холст N»."""
        self._ensure_canvases()
        nums = []
        for c in self._canvases:
            cid = c.get("id", "")
            if isinstance(cid, str) and cid.startswith("c") and cid[1:].isdigit():
                nums.append(int(cid[1:]))
        n = (max(nums) + 1) if nums else 1
        self._canvases.append({"id": f"c{n}", "title": f"Холст {n}", "cells": []})
        self._switch_canvas(f"c{n}")   # синк текущего + построить пустой + персист + пересборка

    def _switch_canvas(self, canvas_id):
        """Переключить активный холст (ПУТЬ A — плитки НЕ уничтожаются):
        синк meta текущих → секцию (persist) → СПРЯТАТЬ живые плитки текущего холста
        (в _canvas_cells, без deleteLater) → показать/построить плитки целевого →
        персист active → пересобрать таб-бар. No-op если уже активен / id неизвестен.
        Идущая генерация переживает switch: её плитка остаётся живой (скрытой)."""
        self._ensure_canvases()
        if not canvas_id or canvas_id == self._active_canvas_id:
            return   # гард: тот же холст — не пересобираем зря
        if not any(c.get("id") == canvas_id for c in self._canvases):
            return   # неизвестный id
        # ВАЖНО: meta готовых плиток текущего → секция ДО ухода (cross-session persist).
        self._sync_active_canvas_cells()
        self._detach_active_cells()   # ЖИВЫЕ плитки текущего → спрятать (НЕ удалять)
        self.clear_refs()             # рефы per-session — сброс (как в reload_canvas)
        self._active_canvas_id = canvas_id
        import storyboard_app as _sa
        root = _sa.get_stored_root()
        slug = _sa.get_current_show(root) if root else None
        out_dir = (root / "shows" / slug / "generator") if (root and slug) else None
        self._attach_canvas_cells(canvas_id, out_dir)
        self._save_canvas()        # зафиксировать active + все секции
        self._rebuild_canvas_row() # подсветить новую вкладку

    def _detach_active_cells(self):
        """СПРЯТАТЬ живые плитки текущего активного холста: открепить из рядов
        (setParent(None)+hide), оставить в self._canvas_cells[active] (НЕ deleteLater
        — иначе idущая генерация пишет в мёртвый виджет). Снести ряды-контейнеры.
        _loading_cells НЕ трогаем — генерящаяся плитка продолжает «дышать»/считать."""
        old = self._active_canvas_id
        if old is not None:
            self._canvas_cells[old] = self._cells   # держим живой список за холстом
        for cell in self._cells:
            try:
                cell.setParent(None)   # открепить из row-контейнера (виджет ЖИВ)
                cell.hide()
            except Exception:
                pass
        # снести ряды-контейнеры (плитки уже откреплены — deleteLater рядов их не заденет)
        while self._rows_v.count():
            it = self._rows_v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._cells = []
        self._cell_count = 0
        self._grid_host.hide()
        self._empty_host.show()

    def _attach_canvas_cells(self, canvas_id, out_dir):
        """Показать плитки целевого холста. Уже посещался (есть живые в
        _canvas_cells) → ПЕРЕПРИКРЕПИТЬ те же виджеты (таймеры/картинка/видео живы).
        Первый визит → построить из секции (_populate_cells). Инвариант:
        _canvas_cells[canvas_id] = self._cells (один список)."""
        cached = self._canvas_cells.get(canvas_id)
        if cached is not None:
            self._cells = cached
            for cell in self._cells:
                try:
                    cell.setParent(self._grid_host)
                    cell.show()
                except Exception:
                    pass
            self._cell_count = len(self._cells)
            if self._cells:
                self._empty_host.hide()
                self._grid_host.show()
                self._relayout_grid()   # переразложить живые ячейки по рядам
            else:
                self._grid_host.hide()
                self._empty_host.show()
        else:
            self._cells = []
            self._canvas_cells[canvas_id] = self._cells   # тот же объект — _populate append'ит
            if out_dir is not None:
                self._populate_cells(self._active_canvas().get("cells") or [], out_dir)

    def _destroy_stashed_canvas_cells(self):
        """Снести ЖИВЫЕ плитки ВСЕХ НЕактивных холстов (deleteLater) + очистить кэш.
        Активный холст (self._cells) сносит _clear_canvas. Зовётся при СМЕНЕ СЕРИАЛА
        (reload_canvas) — плитки прошлого сериала не нужны."""
        for cid, cells in list(self._canvas_cells.items()):
            if cid == self._active_canvas_id:
                continue   # активные = self._cells → их снесёт _clear_canvas
            for cell in cells:
                try:
                    self.unregister_loading(cell)
                except Exception:
                    pass
                try:
                    cell.setParent(None)
                    cell.deleteLater()
                except Exception:
                    pass
        self._canvas_cells = {}

    def _populate_cells(self, cells, out_dir):
        """Построить ShimmerCell'ы из списка meta (cells активного холста) в текущую
        сетку. Общий код для _load_canvas и _switch_canvas. Запись без файла на
        диске пропускается молча; битая запись не валит остальные."""
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
                self._cells.append(cell)   # APPEND → порядок 1:1 как в секции
            except Exception:
                continue   # одна битая запись не валит весь restore
        self._cell_count = len(self._cells)
        if self._cells:
            self._empty_host.hide()
            self._grid_host.show()
            self._relayout_grid()   # ОДИН раз в конце (не в цикле)

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
        empty = QLabel(tr('gen_empty_canvas'))
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
        # Верхний layout бара — ГОРИЗОНТАЛЬНЫЙ: слева колонка (рефы+промпт+контролы),
        # справа крупная кнопка отправки, прижатая к НИЗУ (AlignBottom). Кнопка ниже
        # своей колонки по высоте → НЕ распирает бар (высоту задаёт левая колонка),
        # поэтому её увеличение НЕ двигает промпт/рефы вверх.
        root_h = QHBoxLayout(bar)
        root_h.setContentsMargins(14, 12, 14, 12)
        root_h.setSpacing(10)
        outer = QVBoxLayout()             # левая колонка (без своих внешних полей)
        outer.setContentsMargins(0, 0, 0, 0)
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
        self.prompt_input.setPlaceholderText(tr('gen_prompt_placeholder'))
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
            [(tr('gen_seg_image'), "image"), (tr('gen_seg_video'), "video")], active_key="image", accent=True)
        # Клик по режиму: подсветка + смена списка моделей (см. _on_mode_change).
        for _k, _b in self.mode_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_mode_change(key))
        # 2026-07-02 (пиксельный дрейф ряда): QSS активного сегмента делает
        # текст bold (font-weight:700, :248) — bold шире regular на ~1px →
        # при Image↔Video mode_seg менял ширину и толкал всё правее (кнопка
        # модели «дёргалась»). Фиксируем ширину КАЖДОЙ кнопки сегмента по
        # BOLD-метрике её текста (bold ≥ regular; pixelSize 12 = QSS
        # font-size) + QSS-padding 11+11; группа = сумма + margins 2+2.
        # Ширина ряда физически идентична в обоих режимах.
        _bf = QFont(self.mode_seg.font())
        _bf.setPixelSize(12)
        _bf.setBold(True)
        _bfm = QFontMetrics(_bf)
        _seg_total = 0
        for _b in self.mode_btns.values():
            _bw = _bfm.horizontalAdvance(_b.text()) + 22
            _b.setFixedWidth(_bw)
            _seg_total += _bw
        self.mode_seg.setFixedWidth(_seg_total + 4)
        ctl.addWidget(self.mode_seg)

        # Модель — ВТОРАЯ, сразу после режима (зависит от режима).
        # Свой тёмный выпадающий виджет (всплывает ВВЕРХ). Контракт под _on_run:
        # current_model_id() == прежний currentData(). objectName "model-combo"
        # на кнопке-триггере ВНУТРИ виджета; высоту ряда (34) держим снаружи.
        self.model_combo = ModelSelect()
        self.model_combo.setFixedHeight(34)
        # 2026-07-02: ширина НЕ хардкодится (146 обрезал «Nano Banana 2 Lite» →
        # «Nano Banana 2 Li…»). Считаем по САМОМУ ДЛИННОМУ названию модели из
        # ВСЕХ режимов MODELS_BY_MODE (fontMetrics, pixelSize 13 — как QSS
        # font-size:13px у #model-combo-label): единая ширина для image и
        # video → кнопка НЕ прыгает при переключении режима, будущие модели
        # влезают автоматом. Слагаемые: 12+12 контент-мэржины триггера,
        # 8 spacing, ~12 стрелка ▾, +6 запас (галочка ✓ строк попапа уже
        # покрыта: попап = ширине кнопки, его строка text+8+✓ уже этой суммы).
        _mf = QFont(self.model_combo.font())
        _mf.setPixelSize(13)
        _mfm = QFontMetrics(_mf)
        _max_lbl = max(
            (_mfm.horizontalAdvance(lbl)
             for items in MODELS_BY_MODE.values() for (lbl, _mid) in items),
            default=120)
        self.model_combo.setFixedWidth(_max_lbl + 12 + 12 + 8 + 12 + 6)
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

        # Veo (flow-video-fast): режим работы рефов вместо длительности —
        # «Кадры» (keyframes: start/end frame guidance) или «Рефы» (ingredients:
        # style refs). По умолчанию — keyframes. Виден ТОЛЬКО для video + Veo,
        # взаимоисключение с dur_seg (показ — в _update_duration_visibility).
        self.veo_mode_seg, self.veo_mode_btns = self._seg_group(
            [(tr('gen_seg_keyframes'), "keyframes"), (tr('gen_seg_refs'), "refs")], active_key="keyframes")
        for _k, _b in self.veo_mode_btns.items():
            _b.clicked.connect(
                lambda _checked=False, key=_k: self._on_veo_mode_change(key))
        self.veo_mode_seg.setVisible(False)
        ctl.addWidget(self.veo_mode_seg)
        # Оба сегмента — одинаковая фиксированная ширина (максимум из двух sizeHint).
        # При переключении модели Veo↔Omni визуально ничего не дёргается.
        _seg_w = max(self.dur_seg.sizeHint().width(), self.veo_mode_seg.sizeHint().width())
        self.dur_seg.setFixedWidth(_seg_w)
        self.veo_mode_seg.setFixedWidth(_seg_w)

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
        outer.addLayout(ctl)

        # Левая колонка собрана → в горизонтальный root_h (растягивается на всю ширину
        # минус правая кнопка).
        root_h.addLayout(outer, 1)

        # Кнопка запуска — ПРЯМОУГОЛЬНАЯ со скруглением (не круг), КРУПНАЯ; стрелка ↑.
        # Прижата к НИЗУ колонки (AlignBottom): нижний край вровень с нижним рядом
        # кнопок (ctl), а сама тянется вверх к строке промпта. Высота 56 < высоты левой
        # колонки (мин 88) → бар не распирается, промпт/рефы не двигаются. Правая
        # колонка → не перекрывает промпт по горизонтали.
        # _RunButton — КАСТОМНАЯ отрисовка (paintEvent): золотой скруглённый квадрат +
        # стрелка ↑, золото СРАЗУ (нативный крупный QToolButton на macOS рисовался серым,
        # QSS-золото лишь на hover). Квадрат 56×56.
        self.run_btn = _RunButton()
        self.run_btn.setObjectName("run-btn")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.run_btn.setFixedSize(56, 56)
        self.run_btn.clicked.connect(self._on_run)   # MVP: запуск генерации
        root_h.addWidget(self.run_btn, 0, Qt.AlignmentFlag.AlignBottom)

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
            elif t == QEvent.Type.MouseButtonPress:
                # ЛКМ по тумбе (крестик — отдельный child, его клики сюда не доходят) →
                # начать ВОЗМОЖНЫЙ drag-to-swap. grabMouse: иначе QFrame не accept'ит press
                # и не получит move/release. Реальный drag — в MouseMove при сдвиге >5px.
                if ev.button() == Qt.MouseButton.LeftButton:
                    self._drag_thumb = obj
                    self._drag_start_pos = ev.globalPosition().toPoint()
                    self._drag_active = False
                    self._drag_target = None
                    try:
                        obj.grabMouse()
                    except Exception:
                        pass
                    return True
            elif t == QEvent.Type.MouseMove:
                if (getattr(self, "_drag_thumb", None) is obj
                        and getattr(self, "_drag_start_pos", None) is not None):
                    gp = ev.globalPosition().toPoint()
                    if not self._drag_active and (gp - self._drag_start_pos).manhattanLength() > 5:
                        self._drag_active = True   # порог 5px: клик/крестик ≠ drag
                        self._hide_thumb_popup()
                        QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
                    if self._drag_active:
                        self._update_drag_target(gp)
                    return True
            elif t == QEvent.Type.MouseButtonRelease:
                if (getattr(self, "_drag_thumb", None) is obj
                        and ev.button() == Qt.MouseButton.LeftButton):
                    if self._drag_active:
                        self._finish_drag_swap(ev.globalPosition().toPoint())
                    self._end_drag()
                    return True
        # canvas-chip: позиция ✕ (Resize) + ЛКМ-релиз → переключить холст (КУСОК 2).
        # ✕ теперь на АКТИВНОЙ вкладке и ВСЕГДА виден (без hover) — Enter/Leave не
        # нужны. ✕ — абсолютная дочерняя кнопка; её клики чипу не доходят (обработчик
        # удаления — кусок 3).
        if isinstance(obj, QFrame) and obj.objectName() == "canvas-chip":
            t = ev.type()
            b = getattr(obj, "_close_btn", None)   # None у НЕактивных вкладок (✕ нет)
            if t == QEvent.Type.Resize and b is not None:
                b.move(obj.width() - 18 - 6, (obj.height() - 18) // 2)
                b.raise_()
            elif t == QEvent.Type.MouseButtonRelease:
                try:
                    if ev.button() == Qt.MouseButton.LeftButton:
                        self._switch_canvas(getattr(obj, "_canvas_id", None))
                except Exception:
                    pass
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
            self._show_hint(tr('gen_hint_create_show_files'))
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
                # 2026-06-28: дроп КАРТИНКИ сразу кладём как JPEG (кроме уже-JPEG).
                # Причина: на машине юзера внешний Adobe watch-folder пере-кодирует
                # ЛЮБОЙ .png в этой папке в .jpg за секунды и удаляет оригинал — это
                # рассинхронивало путь плитки (meta=.png, на диске .jpg → клик/реф/папка
                # ломались). Кладём JPEG сами → вотчеру нечего трогать, конфликта нет.
                # JPEG quality=95, БЕЗ даунскейла (это реф для генерации — полное
                # разрешение, как и делает Adobe). Видео не трогаем. Конверсия не удалась
                # (QImage null) → фоллбэк: копируем оригинал как есть, не падаем.
                if ftype == "image" and ext not in (".jpg", ".jpeg"):
                    target = out_dir / f"{stem}.jpg"
                    converted = False
                    try:
                        r = QImageReader(str(src))
                        r.setAllocationLimit(0)     # тяжёлые 4K/8K не отбрасываем лимитом
                        r.setAutoTransform(True)    # учесть EXIF-ориентацию
                        img = r.read()
                        if not img.isNull():
                            converted = bool(img.save(str(target), "JPEG", 95))
                    except Exception:
                        pass
                    if not converted:
                        target = out_dir / f"{stem}{ext}"
                        shutil.copy2(str(src), str(target))   # фоллбэк: оригинал как есть
                else:
                    target = out_dir / f"{stem}{ext}"
                    shutil.copy2(str(src), str(target))   # JPEG/видео — копия без изменений
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

    def _on_veo_mode_change(self, key: str):
        """Клик Кадры/Рефы (Veo): подсветка сегмента + ПЕРЕСЧЁТ активности рефов.
        _max_refs зависит от под-режима Veo (Кадры=2, Рефы=3), поэтому притухшие/активные
        тумбы надо переразметить — иначе при Кадры→Рефы 3-й реф остался бы серым."""
        self._on_seg_click(self.veo_mode_btns, key)
        self._refresh_ref_activity()

    def _update_duration_visibility(self):
        """Видимость сегментов dur_seg / veo_mode_seg по режиму+модели:
          • video + Omni Flash → dur_seg (4/6/8/10s)
          • video + Veo (flow-video-fast / flow-video-light) → veo_mode_seg (Кадры/Рефы)
          • image → оба скрыты.
        Сегменты делят один слот в ctl-ряду (равная ширина), взаимоисключение.

        Зовётся model_combo.changed (смена модели) и _on_mode_change (image↔video).
        Рефы и промпт при этом СОХРАНЯЮТСЯ ПОЛНОСТЬЮ; рефы сверх нового _max_refs не
        удаляются, а помечаются притухшими (_refresh_ref_activity) — в генерацию уходят
        только активные первые N (_active_refs)."""
        model = self.model_combo.current_model_id()
        is_video = (self._mode == "video")
        self.dur_seg.setVisible(is_video and model == "flow-video-omni-flash")
        self.veo_mode_seg.setVisible(
            is_video and model in ("flow-video-fast", "flow-video-light"))
        # Рефы/промпт СОХРАНЯЮТСЯ при смене режима/модели; лишние (сверх _max_refs) не
        # удаляются, а тушатся (_refresh_ref_activity) — в payload идут только активные.
        self._refresh_ref_activity()

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
            self._show_hint(tr('gen_hint_enter_prompt'))
            return
        import storyboard_app as _sa
        root = _sa.get_stored_root()
        slug = _sa.get_current_show(root) if root else None
        if not root or not slug:
            # Картинки сохраняются в папку сериала — без него генерация невозможна.
            self._show_hint(tr('gen_hint_create_show_gen'))
            return
        model_id = self.model_combo.current_model_id()
        if not model_id:
            self._show_hint(tr('gen_hint_model_unavail'))
            return
        # ФИШКА 1б: сверхлимитные (притухшие) рефы блокируют запуск — юзер убирает лишние.
        # limit = _max_refs текущего режима (картинки 10, Veo «Кадры» 2 / «Рефы» 3, Omni 7).
        # Сообщение — в ту же плашку (_show_hint), что прочие pre-flight гарды выше.
        try:
            limit = max(0, int(self._max_refs()))
        except Exception:
            limit = len(self._pending_refs)
        extra = len(self._pending_refs) - limit
        if extra > 0:
            self._show_hint(tr('gen_hint_extra_refs', extra=extra, limit=limit))
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
        # Veo (flow-video-fast / flow-video-light) — без duration; Omni — текущая
        # длительность сегмента. Light по API идентичен Fast (фикс ~8с).
        duration_arg = (None if model_id in ("flow-video-fast", "flow-video-light")
                        else self._duration)
        # Veo: "Кадры" → payload.keyframes=True (start/end frame guidance);
        # "Рефы" → без флага (ingredients-режим, default сервера). Для Omni и
        # картинок keyframes_arg остаётся False, в payload поле не уйдёт.
        keyframes_arg = False
        if is_video and model_id in ("flow-video-fast", "flow-video-light"):
            keyframes_arg = (self._active_seg_key(self.veo_mode_btns) == "keyframes")
        if is_video:
            from generator.generator_video_thread import GeneratorVideoThread
        else:
            from generator.generator_thread import GeneratorImageThread
        # Прикреплённые рефы из prompt-bar → payload["inputs"]. ТОЛЬКО активные первые N
        # (_active_refs по _max_refs); притухшие (сверх лимита режима) НЕ уходят. Копия
        # списка — мутации в UI после старта не влияют на уже запущенные потоки.
        refs = self._active_refs()
        # Pre-flight гард: Veo Fast/Light «Кадры» (keyframes) ТРЕБУЕТ ≥1 стартовый кадр
        # (сервер: «flow-video-* keyframes requires 1-2 inputs; received 0» → HTTP
        # 400). Без рефов не отправляем — иначе заведомо невалидный запрос + плитка
        # с ошибкой. Тот же _show_hint-паттерн, что у промпта/сериала/модели выше.
        if (is_video and model_id in ("flow-video-fast", "flow-video-light")
                and keyframes_arg and not refs):
            self._show_hint(tr('gen_hint_keyframes_need_ref'))
            return
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
                          refs=[Path(r).name for r in refs] if refs else [],
                          # 2026-06-28 (попап-данные): полные пути рефов (для превью рефов
                          # в попапе; refs выше — только имена, оставлены как есть) +
                          # длительность видео (Omni: сек; Veo/картинки: None). canvas.json
                          # локальный у юзера, читается на той же машине → абс. пути ок.
                          ref_paths=[str(r) for r in refs] if refs else [],
                          duration=(duration_arg if is_video else None))
            if is_video:
                th = GeneratorVideoThread(prompt, aspect, model_id, duration_arg,
                                          out_dir, refs=refs,
                                          keyframes=keyframes_arg, parent=None)
            else:
                th = GeneratorImageThread(prompt, aspect, model_id, out_dir,
                                          refs=refs, parent=None)
            self._gen_threads.append(th)
            th.finished.connect(lambda pth, c=cell, t=th: self._on_gen_done(c, t, pth))
            th.error.connect(lambda msg, c=cell, t=th: self._on_gen_fail(c, t, msg))
            th.progress.connect(lambda msg, c=cell: c.set_loading_text(msg))
            th.start()
        # Генерация запущена → очистить поле промпта (prompt уже скопирован в локальную
        # переменную и в потоки выше; на их работу очистка не влияет). После цикла —
        # ранние return'ы (пустой промпт / нет сериала / нет модели) сюда не доходят.
        self.prompt_input.clear()
        self.clear_refs()   # рефы тоже относились к этой генерации → сброс UI/состояния

    # ── сетка результатов + параллельные плитки ───────────────────────
    def toggle_video_muted(self):
        """ГЛОБАЛЬНЫЙ mute звука видео (всё приложение). Инвертирует флаг, пишет в
        QSettings (переживает перезапуск, не привязан к проекту/холсту) и применяет ко
        ВСЕМ живым видео-карточкам активного холста. Видео на других холстах/новые
        подхватят состояние при следующем hover-play (ShimmerCell.enterEvent) и при
        создании плеера (_ensure_player)."""
        self._video_muted = not self._video_muted
        try:
            QSettings().setValue("generator/video_muted", self._video_muted)
        except Exception:
            pass
        for cell in self._cells:
            try:
                cell.apply_video_muted(self._video_muted)
            except Exception:
                pass

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

    def _capture_cell_global_positions(self, exclude=None) -> dict:
        """Снимок текущих экранных позиций карточек перед удалением.
        Global coordinates переживают перепривязку карточек в новые row-host'ы."""
        positions = {}
        for cell in self._cells:
            if cell is exclude or not self._cell_alive(cell):
                continue
            try:
                if cell.isVisible():
                    positions[cell] = cell.mapToGlobal(QPoint(0, 0))
            except Exception:
                pass
        return positions

    def _capture_results_scroll_state(self):
        """Снимок вертикального scroll для стабильного удаления внизу холста.
        Одного value недостаточно: после удаления высота контента уменьшается,
        и у нижних позиций важнее сохранить расстояние до низа."""
        try:
            bar = self._results_area.verticalScrollBar()
            return {
                "value": int(bar.value()),
                "maximum": int(bar.maximum()),
                "bottom_gap": int(bar.maximum() - bar.value()),
                "page_step": int(bar.pageStep()),
            }
        except Exception:
            return {"value": 0, "maximum": 0, "bottom_gap": 0, "page_step": 0}

    def _restore_results_scroll(self, value):
        """Вернуть вертикальный scroll после перестройки сетки.
        При удалении нижней карточки максимум может уменьшиться — берём min."""
        try:
            bar = self._results_area.verticalScrollBar()
            if isinstance(value, dict):
                old_value = int(value.get("value", 0))
                old_gap = max(0, int(value.get("bottom_gap", 0)))
                old_page = max(0, int(value.get("page_step", 0)))
                # Если пользователь был в нижней части холста, сохраняем якорь
                # "расстояние до низа", а не абсолютный value от верха.
                near_bottom = old_gap <= max(24, old_page // 2)
                if near_bottom:
                    target = bar.maximum() - old_gap
                else:
                    target = old_value
            else:
                target = int(value)
            bar.setValue(max(0, min(int(target), bar.maximum())))
        except Exception:
            pass

    def _relayout_grid_animated(self, old_positions: dict, scroll_value: int = None):
        """FLIP-анимация после удаления через overlay-снимки.
        Layout сразу ставит живые карточки в финальные места, но они временно
        прозрачные; поверх едут снимки со старых позиций в новые. Так нет
        промежуточного кадра, где живые карточки моргают уже в финальной позиции."""
        hidden_effects = {}
        overlays = {}
        try:
            overlay_parent = self._results_area.viewport()
        except Exception:
            overlay_parent = self._grid_host
        for cell, old_global in (old_positions or {}).items():
            if cell in self._cells and self._cell_alive(cell):
                try:
                    snapshot = cell.grab()
                    if snapshot.isNull():
                        continue
                    start_pos = overlay_parent.mapFromGlobal(old_global)
                    overlay = QLabel(overlay_parent)
                    overlay.setAttribute(
                        Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                    overlay.setPixmap(snapshot)
                    overlay.setScaledContents(True)
                    overlay.setGeometry(QRect(start_pos, cell.size()))
                    overlay.show()
                    overlay.raise_()
                    overlays[cell] = overlay
                    effect = QGraphicsOpacityEffect(cell)
                    effect.setOpacity(0.0)
                    cell.setGraphicsEffect(effect)
                    hidden_effects[cell] = effect
                except Exception:
                    pass
        self._relayout_grid()
        try:
            self._rows_v.activate()
            if scroll_value is not None:
                self._restore_results_scroll(scroll_value)
            QApplication.processEvents()
            if scroll_value is not None:
                self._restore_results_scroll(scroll_value)
        except Exception:
            pass

        group = QParallelAnimationGroup(self)
        moved = False
        for cell, old_global in (old_positions or {}).items():
            if cell not in self._cells or not self._cell_alive(cell):
                continue
            parent = cell.parentWidget()
            if parent is None:
                continue
            try:
                overlay = overlays.get(cell)
                if overlay is None:
                    continue
                end_pos = overlay_parent.mapFromGlobal(cell.mapToGlobal(QPoint(0, 0)))
                start_pos = overlay_parent.mapFromGlobal(old_global)
                if (start_pos - end_pos).manhattanLength() < 2:
                    continue
                overlay.raise_()
                anim = QPropertyAnimation(overlay, b"geometry", group)
                anim.setDuration(520)
                anim.setEasingCurve(QEasingCurve.Type.OutQuart)
                anim.setStartValue(QRect(start_pos, overlay.size()))
                anim.setEndValue(QRect(end_pos, cell.size()))
                group.addAnimation(anim)
                moved = True
            except Exception:
                continue

        if not moved:
            for cell in list(hidden_effects.keys()):
                try:
                    cell.setGraphicsEffect(None)
                except Exception:
                    pass
            for overlay in list(overlays.values()):
                try:
                    overlay.setParent(None)
                    overlay.deleteLater()
                except Exception:
                    pass
            group.deleteLater()
            return
        self._grid_move_anim = group

        def _cleanup_hidden_effects():
            for cell in list(hidden_effects.keys()):
                try:
                    if self._cell_alive(cell):
                        cell.setGraphicsEffect(None)
                except Exception:
                    pass
            for overlay in list(overlays.values()):
                try:
                    overlay.setParent(None)
                    overlay.deleteLater()
                except Exception:
                    pass
            if scroll_value is not None:
                self._restore_results_scroll(scroll_value)

        group.finished.connect(lambda: setattr(self, "_grid_move_anim", None))
        group.finished.connect(_cleanup_hidden_effects)
        group.finished.connect(group.deleteLater)
        group.start()

    def _animate_deleted_cell_collapse(self, snapshot: QPixmap, old_global: QPoint,
                                       start_size: QSize, scroll_value: int = None,
                                       finished_callback=None):
        """Мягко схлопнуть snapshot удаляемой карточки поверх сетки.
        Не анимируем живой layout-виджет: так нет драки между layout и animation."""
        if snapshot is None or snapshot.isNull():
            if callable(finished_callback):
                QTimer.singleShot(0, finished_callback)
            return
        overlay = None
        try:
            if scroll_value is not None:
                self._restore_results_scroll(scroll_value)
            start_pos = self._grid_host.mapFromGlobal(old_global)
            overlay = QLabel(self._grid_host)
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            overlay.setPixmap(snapshot)
            overlay.setScaledContents(True)
            overlay.setGeometry(QRect(start_pos, start_size))
            overlay.show()
            overlay.raise_()
            effect = QGraphicsOpacityEffect(overlay)
            overlay.setGraphicsEffect(effect)
            end_w = max(28, int(start_size.width() * 0.12))
            end_h = max(18, int(start_size.height() * 0.12))
            end_rect = QRect(
                start_pos.x() + (start_size.width() - end_w) // 2,
                start_pos.y() + (start_size.height() - end_h) // 2,
                end_w,
                end_h,
            )
            group = QParallelAnimationGroup(self)
            geom_anim = QPropertyAnimation(overlay, b"geometry", group)
            geom_anim.setDuration(560)
            geom_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            geom_anim.setStartValue(QRect(start_pos, start_size))
            geom_anim.setEndValue(end_rect)
            opacity_anim = QPropertyAnimation(effect, b"opacity", group)
            opacity_anim.setDuration(560)
            opacity_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            opacity_anim.setStartValue(1.0)
            opacity_anim.setEndValue(0.0)
            group.addAnimation(geom_anim)
            group.addAnimation(opacity_anim)
            if not hasattr(self, "_delete_collapse_anims"):
                self._delete_collapse_anims = []
            self._delete_collapse_anims.append(group)

            def _cleanup():
                try:
                    self._delete_collapse_anims.remove(group)
                except Exception:
                    pass
                try:
                    overlay.setGraphicsEffect(None)
                    overlay.setParent(None)
                    overlay.deleteLater()
                except Exception:
                    pass
                if callable(finished_callback):
                    try:
                        finished_callback()
                    except Exception:
                        pass
                group.deleteLater()

            group.finished.connect(_cleanup)
            group.start()
        except Exception:
            try:
                if overlay is not None:
                    overlay.deleteLater()
            except Exception:
                pass
            if callable(finished_callback):
                QTimer.singleShot(0, finished_callback)

    @staticmethod
    def _cell_alive(cell) -> bool:
        """Жив ли C++ объект плитки. Путь A плитки на switch НЕ удаляет, но при
        смене СЕРИАЛА (reload) поток мог финишировать в уже удалённую плитку —
        страховка от RuntimeError «wrapped C/C++ object has been deleted»."""
        try:
            from PyQt6 import sip
            return cell is not None and not sip.isdeleted(cell)
        except Exception:
            return cell is not None

    def _on_gen_done(self, cell, th, path: str):
        if not self._cell_alive(cell):
            # Плитка уже снесена (смена сериала во время генерации). Файл на диске
            # есть, но обновлять/персистить некуда — чистим поток и выходим.
            if th in self._gen_threads:
                self._gen_threads.remove(th)
            return
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
        if not self._cell_alive(cell):
            if th in self._gen_threads:
                self._gen_threads.remove(th)
            return
        try:
            cell.set_error((msg or tr('gen_err_label'))[:160])
        except Exception:
            pass
        if th in self._gen_threads:
            self._gen_threads.remove(th)

    def upscale_result_cell(self, cell: ShimmerCell):
        """2026-06-25 (апскейл): создать НОВУЮ loading-плитку рядом с исходной,
        запустить UpscaleThread × 2 через локальный Real-ESRGAN.

        ВАЖНО: исходная cell НЕ меняется. Результат — отдельный <stem>_2k.jpg
        в той же папке холста (shows/<slug>/generator/), новая плитка на холсте.
        Если движок не готов — спросить юзера разово через QMessageBox.question;
        отказ → ничего не делать, никакой плитки не создаём."""
        if not self._cell_alive(cell):
            return
        meta = cell.meta() if hasattr(cell, "meta") else {}
        if not isinstance(meta, dict):
            return
        if (meta.get("type") or "image") != "image":
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
            out_dir = root / "shows" / slug / "generator"
            out_root = out_dir.resolve()
            src_path = (out_dir / fname).resolve()
            # Защита границ: src должен быть внутри out_dir (защита от
            # битого canvas.json с относительным путём наружу).
            if out_root not in src_path.parents and src_path != out_root:
                return
            if not src_path.is_file():
                return
        except Exception:
            return

        # Имя выходного файла: <stem>_2k.jpg (+ _2k_2.jpg при коллизии).
        stem = src_path.stem
        out_path = out_dir / f"{stem}_2k.jpg"
        i = 2
        while out_path.exists():
            out_path = out_dir / f"{stem}_2k_{i}.jpg"
            i += 1

        # Спросить юзера про скачивание движка ОДИН РАЗ если он не готов.
        try:
            from threads.upscale_engine import is_engine_ready
            engine_ready = is_engine_ready()
        except Exception:
            engine_ready = False
        if not engine_ready:
            try:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Question)
                box.setWindowTitle(tr('gen_upscale_title'))
                box.setText(tr('gen_upscale_body'))
                yes_btn = box.addButton(tr('gen_btn_download'), QMessageBox.ButtonRole.AcceptRole)
                box.addButton(tr('gen_btn_cancel'), QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is not yes_btn:
                    return
            except Exception:
                traceback.print_exc()
                return

        # Создаём НОВУЮ loading-плитку с тем же аспектом что у исходной.
        aspect = (cell.aspect() if hasattr(cell, "aspect")
                  else (meta.get("aspect") or "16:9"))
        try:
            w, h = self._cell_wh(aspect)
        except Exception:
            w, h = (480, 270) if aspect != "9:16" else (270, 480)
        new_cell = ShimmerCell(self, w, h, aspect=aspect)
        # Сразу заявим в meta тип/aspect — _refresh_2k_enabled и т.п.
        # На loading 2K сама себя скроет (state != image).
        try:
            new_cell.set_meta(type="image", aspect=aspect, model_label="2K")
            new_cell.set_model_label("2K")
            new_cell.set_loading_text(tr('gen_loading_prep'))
        except Exception:
            pass

        # Зеркало _add_cell (генерация): новая плитка СВЕРХУ.
        # self._cells и self._canvas_cells[active] — ОДИН И ТОТ ЖЕ объект
        # (см. _canvas_cells[canvas_id] = self._cells, line ~377),
        # поэтому отдельная вставка в _canvas_cells НЕ НУЖНА и была неверна.
        try:
            self._empty_host.hide()
            self._grid_host.show()
            self._cells.insert(0, new_cell)
            try:
                self._cell_count = len(self._cells)
            except Exception:
                pass
            self._relayout_grid()
            self.register_loading(new_cell)
        except Exception:
            pass

        # Запуск воркера.
        from generator.upscale_thread import UpscaleThread
        th = UpscaleThread(src_path, out_path, parent=self)
        self._upscale_threads.append(th)
        th.progress.connect(
            lambda msg, c=new_cell: c.set_loading_text(msg))
        th.finished.connect(
            lambda path, c=new_cell, t=th: self._on_upscale_done(c, t, path))
        th.failed.connect(
            lambda msg, c=new_cell, t=th: self._on_upscale_fail(c, t, msg))
        th.start()

    def _on_upscale_done(self, cell: ShimmerCell, th, path: str):
        """2026-06-25 (апскейл): UpscaleThread.finished → файл готов.
        Зеркало _on_gen_done."""
        if not self._cell_alive(cell):
            if th in self._upscale_threads:
                self._upscale_threads.remove(th)
            return
        try:
            cell.set_image(path)
            import os, time
            fname = os.path.basename(path)
            cell.set_meta(file=fname, ts=time.time(), type="image")
            self._save_canvas()
        except Exception:
            pass
        if th in self._upscale_threads:
            self._upscale_threads.remove(th)

    def _on_upscale_fail(self, cell: ShimmerCell, th, msg: str):
        """2026-06-25 (апскейл): UpscaleThread.failed → ошибка на плитке."""
        if not self._cell_alive(cell):
            if th in self._upscale_threads:
                self._upscale_threads.remove(th)
            return
        try:
            cell.set_error((msg or tr('gen_err_upscale'))[:200])
        except Exception:
            pass
        if th in self._upscale_threads:
            self._upscale_threads.remove(th)

    def delete_result_cell(self, cell: ShimmerCell):
        """Удалить плитку генератора: готовый файл с диска или error-карточку
        без файла, затем убрать карточку с холста и обновить canvas.json."""
        if not self._cell_alive(cell):
            return
        meta = cell.meta() if hasattr(cell, "meta") else {}
        fname = (meta.get("file") or "").strip() if isinstance(meta, dict) else ""
        is_error_cell = getattr(cell, "_state", "") == "error"
        if not fname and not is_error_cell:
            return
        target = None
        try:
            import storyboard_app as _sa
            root = _sa.get_stored_root()
            slug = _sa.get_current_show(root) if root else None
            if not (root and slug):
                return
            out_dir = root / "shows" / slug / "generator"
            out_root = out_dir.resolve()
            if fname:
                target = (out_dir / fname).resolve()
                # Защита от битого canvas.json: удаляем только внутри папки generator.
                if out_root not in target.parents and target != out_root:
                    return
        except Exception:
            return

        settings = QSettings()
        skip_confirm = bool(settings.value(
            "generator/skip_delete_confirm", False, type=bool))
        if not skip_confirm:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr('gen_del_title'))
            if target is not None:
                box.setText(tr('gen_del_img_body'))
            else:
                box.setText(tr('gen_del_err_body'))
            dont_ask = QCheckBox(tr('gen_del_dont_ask'))
            box.setCheckBox(dont_ask)
            yes_btn = box.addButton(tr('gen_btn_delete'), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(tr('gen_btn_cancel'), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
            if dont_ask.isChecked():
                settings.setValue("generator/skip_delete_confirm", True)

        old_positions = self._capture_cell_global_positions(exclude=cell)
        scroll_value = self._capture_results_scroll_state()
        deleted_global = cell.mapToGlobal(QPoint(0, 0))
        deleted_size = cell.size()
        try:
            deleted_snapshot = cell.grab()
        except Exception:
            deleted_snapshot = QPixmap()
        paths_to_delete = [target] if target is not None else []
        if target is not None and meta.get("type") == "video":
            paths_to_delete.append(target.with_suffix(".jpg"))
        for p in paths_to_delete:
            try:
                if p.exists() and p.is_file():
                    p.unlink()
            except Exception:
                pass

        for p in paths_to_delete:
            try:
                self.remove_ref(str(p))
            except Exception:
                pass
        try:
            self.unregister_loading(cell)
        except Exception:
            pass
        for cells in list(self._canvas_cells.values()):
            try:
                while cell in cells:
                    cells.remove(cell)
            except Exception:
                pass
        try:
            while cell in self._cells:
                self._cells.remove(cell)
        except Exception:
            pass
        self._cell_count = len(self._cells)
        if self._cells:
            self._empty_host.hide()
            self._grid_host.show()
            try:
                hold_effect = QGraphicsOpacityEffect(cell)
                hold_effect.setOpacity(0.0)
                cell.setGraphicsEffect(hold_effect)
                cell.setEnabled(False)
            except Exception:
                pass

            def _finish_delete_and_shift():
                # Важно: до этого момента реальная карточка остаётся в layout как
                # невидимый держатель места. Иначе соседняя плитка телепортируется
                # под overlay удаляемой карточки ещё до старта FLIP-сдвига.
                try:
                    cell.hide()
                    cell.setGraphicsEffect(None)
                    cell.setParent(None)
                    cell.deleteLater()
                except Exception:
                    pass
                self._relayout_grid_animated(old_positions, scroll_value)

            self._animate_deleted_cell_collapse(
                deleted_snapshot,
                deleted_global,
                deleted_size,
                scroll_value,
                _finish_delete_and_shift,
            )
        else:
            try:
                cell.hide()
                cell.setParent(None)
                cell.deleteLater()
            except Exception:
                pass
            while self._rows_v.count():
                it = self._rows_v.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.deleteLater()
            self._grid_host.show()
            self._empty_host.hide()
            self._restore_results_scroll(scroll_value)
            self._animate_deleted_cell_collapse(
                deleted_snapshot, deleted_global, deleted_size, scroll_value)
            QTimer.singleShot(580, lambda: (
                self._grid_host.hide(),
                self._empty_host.show(),
            ))
        self._save_canvas()

    # ── мультихолст: секции/активный (КУСОК 1 — данные) ────────────────
    def _ensure_canvases(self):
        """Гарантировать ≥1 холст и валидный active. Дефолт — «Холст 1» (c1).
        Вызывается перед любым доступом к секциям (save/load/sync)."""
        if not self._canvases:
            self._canvases = [{"id": "c1", "title": "Холст 1", "cells": []}]
        if (not self._active_canvas_id
                or not any(c.get("id") == self._active_canvas_id
                           for c in self._canvases)):
            self._active_canvas_id = self._canvases[0]["id"]

    def _active_canvas(self) -> dict:
        """Секция активного холста (создаёт дефолт через _ensure_canvases)."""
        self._ensure_canvases()
        for c in self._canvases:
            if c.get("id") == self._active_canvas_id:
                return c
        return self._canvases[0]   # подстраховка (ensure уже выровнял active)

    def _sync_active_canvas_cells(self):
        """Снять meta текущих self._cells (плитки активного холста) в его секцию.
        Только плитки с готовым файлом (как раньше). Порядок self._cells = порядок
        на холсте (новое сверху)."""
        self._active_canvas()["cells"] = [
            c.meta() for c in self._cells
            if self._cell_alive(c) and c.meta().get("file")]

    def _sync_all_canvas_cells(self):
        """Синк meta ЖИВЫХ плиток ВСЕХ посещённых холстов (путь A: _canvas_cells) в
        их секции. Нужно чтобы генерация, ФИНИШИРОВАВШАЯ на СКРЫТОМ холсте, попала в
        canvas.json сразу (а не только при возврате) — иначе при выходе до возврата
        плитка терялась бы. Непосещённые секции остаются как загружены."""
        self._ensure_canvases()
        sect = {c.get("id"): c for c in self._canvases}
        synced = set()
        for cid, cells in self._canvas_cells.items():
            s = sect.get(cid)
            if s is not None:
                s["cells"] = [c.meta() for c in cells
                              if self._cell_alive(c) and c.meta().get("file")]
                synced.add(cid)
        # активный мог ещё не попасть в _canvas_cells (самый первый save) — явно.
        if self._active_canvas_id not in synced:
            self._active_canvas()["cells"] = [
                c.meta() for c in self._cells
                if self._cell_alive(c) and c.meta().get("file")]

    @staticmethod
    def _parse_canvas_data(data) -> tuple:
        """canvas.json (v1 ИЛИ v2) → (canvases:list, active_id:str).

        МИГРАЦИЯ v1→v2: старый формат version=1 имел top-level "cells" без понятия
        холста. Оборачиваем эти cells в ОДИН холст {id:"c1", title:"Холст 1"},
        active="c1" — старые плитки переезжают 1:1 БЕЗ потерь. Неизвестный/битый
        формат → один пустой холст по умолчанию."""
        if (isinstance(data, dict) and data.get("version") == 2
                and isinstance(data.get("canvases"), list)):
            canvases = [c for c in data["canvases"]
                        if isinstance(c, dict) and c.get("id")]
            if canvases:
                for c in canvases:
                    if not isinstance(c.get("cells"), list):
                        c["cells"] = []
                    c.setdefault("title", "Холст 1")
                active = data.get("active")
                if not any(c.get("id") == active for c in canvases):
                    active = canvases[0]["id"]
                return canvases, active
        # v1 / неизвестное → миграция top-level cells (или пусто) в один холст.
        old_cells = data.get("cells") if isinstance(data, dict) else None
        if not isinstance(old_cells, list):
            old_cells = []
        return [{"id": "c1", "title": "Холст 1", "cells": old_cells}], "c1"

    # ── персист холста на диск (canvas.json v2: ВСЕ холсты) ────────────
    def _save_canvas(self):
        """Записать ВСЕ холсты в shows/<slug>/generator/canvas.json (формат v2).

        Перед записью синкаем meta текущих self._cells в активную секцию
        (_sync_active_canvas_cells). Пишем только плитки с готовым файлом
        (meta['file']). Атомарно: .tmp в той же папке → os.replace (атомарен на
        Mac и Win). Ошибка записи НЕ роняет генерацию — молча проглатываем (лог в
        stderr). Зовётся из _on_gen_done на main-потоке → вызовы при ×N
        сериализованы, гонки за canvas.json.tmp нет."""
        try:
            import json, os
            import storyboard_app as _sa
            root = _sa.get_stored_root()
            slug = _sa.get_current_show(root) if root else None
            if not root or not slug:
                return
            out_dir = root / "shows" / slug / "generator"
            self._sync_all_canvas_cells()   # ВСЕ живые холсты → секции (вкл. фон-финиш)
            data = {"version": 2,
                    "active": self._active_canvas_id,
                    "canvases": self._canvases}
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out_dir / "canvas.json.tmp"
            final = out_dir / "canvas.json"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, final)
        except Exception as e:  # noqa: BLE001 — персист необязателен, не валим генерацию
            import sys
            print(f"[generator] canvas save failed: {e}", file=sys.stderr)

    # ── ИЗБРАННОЕ (favorites.json — близнец canvas.json, per-show, локально) ──
    def _favorites_path(self):
        """shows/<slug>/generator/favorites.json активного сериала (или None).
        slug = get_current_show(get_stored_root()) — как canvas.json."""
        import storyboard_app as _sa
        root = _sa.get_stored_root()
        slug = _sa.get_current_show(root) if root else None
        if not root or not slug:
            return None
        return root / "shows" / slug / "generator" / "favorites.json"

    def _load_favorites(self) -> list:
        """favorites.json → list[{"file","type"}] (дедуп по file). Битый/нет файла/
        нет сериала → []. Старт не роняем."""
        import json
        try:
            p = self._favorites_path()
            if not p or not p.exists():
                return []
            data = json.loads(p.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return []
            out, seen = [], set()
            for it in items:
                if isinstance(it, dict):
                    f = (it.get("file") or "").strip()
                    if f and f not in seen:
                        seen.add(f)
                        out.append({"file": f, "type": it.get("type") or "image"})
            return out
        except Exception:
            return []

    def _save_favorites(self, items: list) -> None:
        """Атомарно записать favorites.json (tmp+os.replace — копия _save_canvas).
        Персист необязателен — ошибка не валит UI."""
        import json, os, sys
        try:
            p = self._favorites_path()
            if not p:
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.parent / "favorites.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:  # noqa: BLE001
            print(f"[generator] favorites save failed: {e}", file=sys.stderr)

    def is_favorite(self, file: str) -> bool:
        """Есть ли file в избранном активного сериала (ключ — имя файла)."""
        f = (file or "").strip()
        if not f:
            return False
        return any(it.get("file") == f for it in self._load_favorites())

    def toggle_favorite(self, file: str, type: str = "image") -> bool:
        """Переключить file → вернуть НОВОЕ состояние (True=в избранном). Дедуп по
        file. Пустой file → False (без записи)."""
        f = (file or "").strip()
        if not f:
            return False
        items = self._load_favorites()
        idx = next((i for i, it in enumerate(items) if it.get("file") == f), -1)
        if idx >= 0:
            items.pop(idx)
            self._save_favorites(items)
            return False
        items.append({"file": f, "type": type or "image"})
        self._save_favorites(items)
        return True

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
        # Сброс мультихолст-состояния: load полностью перестраивает его из файла.
        # Иначе при смене сериала на шоу БЕЗ canvas.json остались бы секции прошлого
        # шоу. Пусто → _ensure_canvases создаст дефолт «Холст 1» лениво при save.
        self._canvases = []
        self._active_canvas_id = None
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
            # Миграция v1→v2 + выбор активного холста. Старый top-level "cells"
            # оборачивается в «Холст 1» (c1) без потерь (см. _parse_canvas_data).
            self._canvases, self._active_canvas_id = self._parse_canvas_data(data)
            self._ensure_canvases()
            cells = self._active_canvas().get("cells")
            if not isinstance(cells, list):
                return
            self._populate_cells(cells, out_dir)   # общий построитель (см. _switch_canvas)
            # Путь A инвариант: живой список активного холста держим в кэше.
            self._canvas_cells[self._active_canvas_id] = self._cells
        except Exception as e:  # noqa: BLE001 — restore необязателен, не валим страницу
            import sys
            print(f"[generator] canvas load failed: {e}", file=sys.stderr)

    def reload_canvas(self):
        """Публичный: перечитать холст под активный сериал (для смены сериала).
        Хук в storyboard_app._on_show_changed — отдельной правкой. Рефы старого
        шоу не релевантны → очищаем перед сменой содержимого холста. Путь A: живые
        плитки ВСЕХ холстов прошлого сериала тоже снести (не нужны новому сериалу)."""
        self.clear_refs()
        self._destroy_stashed_canvas_cells()   # живые плитки НЕактивных холстов прошлого шоу
        self._clear_canvas()                   # активные плитки + ряды + состояние
        self._canvas_cells = {}                # кэш холстов прошлого сериала — обнулить
        self._load_canvas()
        self._rebuild_canvas_row()   # таб-бар под холсты нового сериала

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
    def _load_thumb_pixmap(self, src: str, max_side: int) -> QPixmap:
        """Экономно загрузить превью: QImageReader декодирует СРАЗУ в уменьшенный размер
        (setScaledSize по большей стороне ≤ max_side) — НЕ держим полный 4K (~31МБ RAM) ради
        маленькой тумбы (это и был источник переполнения памяти и падения рефа в упакованной
        сборке с кучей 4K). allocation-лимит снят + EXIF-ориентация. null если не прочиталось."""
        try:
            r = QImageReader(str(src))
            r.setAllocationLimit(0)
            r.setAutoTransform(True)
            sz = r.size()
            w, h = sz.width(), sz.height()
            if w > 0 and h > 0 and max(w, h) > max_side:
                if w >= h:
                    r.setScaledSize(QSize(max_side, max(1, round(h * max_side / w))))
                else:
                    r.setScaledSize(QSize(max(1, round(w * max_side / h)), max_side))
            img = r.read()
            if not img.isNull():
                return QPixmap.fromImage(img)
        except Exception:
            pass
        return QPixmap()

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
            pix = self._load_thumb_pixmap(src, 128)   # эконом: декод сразу в ~128px, не 31МБ
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
        # Лейбл прозрачен для мыши → press/Enter доходят до тумбы (drag-to-swap + hover),
        # а не съедаются лейблом. Крестик (x_btn, raised) свои клики получает поверх.
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
        # Оверлей drag-target рамки — ПОВЕРХ пиксмапа (дочерние рисуются поверх paintEvent
        # тумбы), но НИЖЕ крестика. Прозрачный, мышь не ловит; пунктир рисует в paintEvent.
        border_ov = _DragBorderOverlay(thumb)
        border_ov.setGeometry(0, 0, 64, 64)
        border_ov.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        border_ov.raise_()
        x_btn.raise_()                 # крестик выше рамки
        thumb._border_ov = border_ov
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
            p0 = self._load_thumb_pixmap(src, 400)     # эконом: декод сразу в ~400px, не 31МБ
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
        """Прикрепить файл к следующей генерации. Дубликат — тихий выход.
        Превышение лимита под текущую модель/режим (_max_refs) — тоже тихий выход."""
        file_path = str(file_path or "").strip()
        if not file_path or file_path in self._pending_refs:
            return
        if len(self._pending_refs) >= self._max_refs():
            return
        self._pending_refs.append(file_path)
        thumb = self._make_ref_thumb(file_path)
        self._ref_thumbs[file_path] = thumb
        self._refs_row_lay.addWidget(thumb)
        self._refs_row.setVisible(True)
        self._refresh_ref_activity()   # новый реф: пометить активным/притухшим по лимиту

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
            real = resolve_existing_path(str(full))   # defensive: внешняя подмена расширения
            if not real:
                return
            self.add_ref(real)
        except Exception:
            pass

    def restore_from_meta(self, meta: dict):
        """«Повторить генерацию»: выставить в нижнее поле генератора промпт + ВСЕ рефы +
        настройки (режим image/video, модель, формат, длительность) из meta карточки.
        Зовётся стрелкой возврата в попапе просмотра (GeneratorViewerDialog). Любая
        отсутствующая настройка — гард, не падаем.

        Порядок важен: режим и модель ВНУТРИ дёргают _update_duration_visibility → clear_refs,
        поэтому рефы добавляем В КОНЦЕ (иначе сотрутся)."""
        if not isinstance(meta, dict):
            return
        # 1) режим image/video (репопулирует список моделей под режим)
        mtype = meta.get("type")
        if mtype in ("image", "video"):
            try:
                self._on_mode_change(mtype)
            except Exception:
                pass
        # 2) модель по id (из уже репопулированного под режим списка)
        mid = meta.get("model_id")
        if mid:
            try:
                self.model_combo.set_current_id(mid)
                self._update_duration_visibility()
            except Exception:
                pass
        # 3) формат 16:9 / 9:16
        asp = meta.get("aspect")
        if asp in ("16:9", "9:16"):
            try:
                self._on_seg_click(self.fmt_btns, asp)
            except Exception:
                pass
        # 4) длительность видео (4/6/8/10), если есть
        dur = meta.get("duration")
        try:
            if dur is not None and int(dur) in (4, 6, 8, 10):
                self._on_duration_change(str(int(dur)))
        except Exception:
            pass
        # 5) рефы — ПОСЛЕ режима/модели (они чистят поле); сначала очистка, потом добавление
        try:
            self.clear_refs()
            from pathlib import Path as _Path
            for rp in (meta.get("ref_paths") or []):
                try:
                    if rp and _Path(str(rp)).exists():
                        self.add_ref(str(rp))
                except Exception:
                    pass
        except Exception:
            pass
        # 6) промпт
        try:
            self.prompt_input.setPlainText(meta.get("prompt") or "")
        except Exception:
            pass

    # ── drag-to-swap рефов (ФИШКА 2): перетащил реф на другой → меняются местами ──
    def _ref_thumb_at(self, global_pt):
        """Тумба рефа под глобальной точкой (hit-test по geometry в глоб. координатах)."""
        for path, th in list(self._ref_thumbs.items()):
            try:
                tl = th.mapToGlobal(QPoint(0, 0))
                if QRect(tl, th.size()).contains(global_pt):
                    return th
            except Exception:
                continue
        return None

    def _update_drag_target(self, global_pt):
        """Подсветить тумбу-цель под курсором (рамка из темы), снять с прежней. Над
        собой/мимо рефов — без подсветки."""
        target = self._ref_thumb_at(global_pt)
        if target is getattr(self, "_drag_thumb", None):
            target = None
        if target is getattr(self, "_drag_target", None):
            return
        for th in (getattr(self, "_drag_target", None), target):
            if th is None:
                continue
            ov = getattr(th, "_border_ov", None)
            if ov is not None:
                ov.set_active(th is target)   # пунктирная рамка только на цели
        self._drag_target = target

    def _finish_drag_swap(self, global_pt):
        """Отпустили над ДРУГОЙ тумбой → SWAP в self._pending_refs (ИСТОЧНИК порядка для
        генерации, _active_refs/_on_run) + перелокация тумб + пересчёт яркие/серые (Фишка 1а).
        Визуал тумб — следствие порядка списка, а НЕ наоборот."""
        src = getattr(self, "_drag_thumb", None)
        target = self._ref_thumb_at(global_pt)
        if src is None or target is None or target is src:
            return
        pi = getattr(src, "_file_path", None)
        pj = getattr(target, "_file_path", None)
        if not pi or not pj or pi not in self._pending_refs or pj not in self._pending_refs:
            return
        i = self._pending_refs.index(pi)
        j = self._pending_refs.index(pj)
        self._pending_refs[i], self._pending_refs[j] = self._pending_refs[j], self._pending_refs[i]
        self._relayout_ref_thumbs()
        self._refresh_ref_activity()   # порядок изменился → пересчёт активных/притухших

    def _relayout_ref_thumbs(self):
        """Переложить тумбы в _refs_row_lay в порядке self._pending_refs (после swap)."""
        lay = getattr(self, "_refs_row_lay", None)
        if lay is None:
            return
        for path in self._pending_refs:
            th = self._ref_thumbs.get(path)
            if th is not None:
                lay.removeWidget(th)
        for path in self._pending_refs:
            th = self._ref_thumbs.get(path)
            if th is not None:
                lay.addWidget(th)

    def _end_drag(self):
        """Завершить drag: снять подсветку цели, вернуть курсор, отпустить мышь, сброс."""
        old = getattr(self, "_drag_target", None)
        if old is not None:
            ov = getattr(old, "_border_ov", None)
            if ov is not None:
                ov.set_active(False)
        if getattr(self, "_drag_active", False):
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass
        src = getattr(self, "_drag_thumb", None)
        if src is not None:
            try:
                src.releaseMouse()
            except Exception:
                pass
        self._drag_thumb = None
        self._drag_start_pos = None
        self._drag_active = False
        self._drag_target = None

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
        # удалили активный → следующий притухший становится активным (пересчёт по лимиту)
        self._refresh_ref_activity()
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

    def _active_refs(self) -> list:
        """Активные рефы — первые N (по _max_refs текущей модели/режима). ТОЛЬКО они
        уходят в payload генерации (inputs[]); остальные _pending_refs показаны
        притухшими (про запас при смене режима). _pending_refs при этом НЕ меняется."""
        try:
            limit = max(0, int(self._max_refs()))
        except Exception:
            return list(self._pending_refs)
        return list(self._pending_refs[:limit])

    def _refresh_ref_activity(self):
        """Пересчитать активность тумб рефов по текущему лимиту (_max_refs): первые N
        активны (яркие), остальные — притушены (про запас). Рефы НЕ удаляются (в
        генерацию идут только _active_refs). Зовётся при смене режима/модели
        (_update_duration_visibility) и из add_ref/remove_ref (актуализация порядка)."""
        try:
            limit = max(0, int(self._max_refs()))
        except Exception:
            limit = len(self._pending_refs)
        for i, path in enumerate(self._pending_refs):
            thumb = self._ref_thumbs.get(path)
            if thumb is not None:
                self._set_thumb_dimmed(thumb, i >= limit)

    def _set_thumb_dimmed(self, thumb, dimmed: bool):
        """Притушить/вернуть яркость тумбы рефа через QGraphicsOpacityEffect (реверсивно).
        dimmed → opacity 0.3 + tooltip «про запас»; активна → opacity 1.0. Эффект создаём
        ЛЕНИВО только при первом притушении (всегда-активные тумбы остаются без эффекта).
        Клики/hover-попап не страдают — эффект чисто визуальный."""
        eff = getattr(thumb, "_dim_effect", None)
        if not dimmed and eff is None:
            return   # активна и эффекта нет — дефолт (яркая), ничего не делаем
        if eff is None:
            eff = QGraphicsOpacityEffect(thumb)
            thumb.setGraphicsEffect(eff)
            thumb._dim_effect = eff
        try:
            eff.setOpacity(0.3 if dimmed else 1.0)
            thumb.setToolTip(
                "Сверх лимита текущего режима — в генерацию не уйдёт (про запас)"
                if dimmed else "")
        except Exception:
            pass

    def pending_refs(self) -> list:
        """Копия списка прикреплённых путей — для коммита 2 (передача в потоки)."""
        return list(self._pending_refs)

    def _max_refs(self) -> int:
        """Лимит количества рефов для add_ref-гарда — зависит от текущей модели/режима:
          • Veo 3.1 «Кадры»  → 2 (keyframes = start+end frame guidance)
          • Veo 3.1 «Рефы»   → 3 (ingredients)
          • Omni Flash       → 7
          • Картинки (Nano Banana 2 / OpenAI) и неизвестная модель → 10.
        Гарды hasattr() — на случай вызова до полной инициализации UI."""
        model_id = (self.model_combo.current_model_id()
                    if hasattr(self, "model_combo") else "")
        if model_id in ("flow-video-fast", "flow-video-light"):
            veo_mode = (self._active_seg_key(self.veo_mode_btns)
                        if hasattr(self, "veo_mode_btns") else None) or "keyframes"
            return 2 if veo_mode == "keyframes" else 3
        if model_id == "flow-video-omni-flash":
            return 7
        return 10   # nano-banana-2 / openai-image / прочее

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
