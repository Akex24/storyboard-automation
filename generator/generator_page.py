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

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QPlainTextEdit,
    QVBoxLayout, QHBoxLayout, QScrollArea, QSizePolicy,
)


# Модели по режиму: (отображаемое имя, внутренний id для payload["model"]).
# image-id реальные (см. pipeline.py / threads/generate.py). video-id пока
# TBD — привяжем когда подключим видео-провайдеров.
MODELS_BY_MODE = {
    "image": [("Nano Banana 2", "nano-banana-2"), ("OpenAI", "openai-image")],
    "video": [("Veo 3.1", None), ("Omni Flash", None)],
}


class GeneratorPage(QWidget):
    """Каркас страницы генератора. Заглушки без логики (этот шаг — только UI)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("generator-page")
        self._mode = "image"   # активный режим (пока статично, без переключения)
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
            # Холст-чипы
            "QFrame#canvas-chip { background:#1a1424; border:1px solid #2a1f3d;"
            " border-radius:8px; }"
            "QFrame#canvas-chip[active=\"true\"] { background:#2a1f3d;"
            " border:1px solid #8e6cd4; }"
            "QLabel#canvas-title { color:#cfcfcf; font-size:12px;"
            " background:transparent; }"
            "QPushButton#canvas-new { background:transparent; color:#a8c8ff;"
            " border:1px dashed #4d6a8a; border-radius:8px; padding:6px 12px;"
            " font-size:12px; }"
            "QPushButton#canvas-close { background:transparent; border:none; }"
            # Область результатов
            "QScrollArea#results { background:#120d1c; border:1px solid #221a33;"
            " border-radius:10px; }"
            "QLabel#results-empty { color:#6a6a78; font-size:13px;"
            " background:transparent; }"
            # Поле промпта
            "QFrame#prompt-bar { background:#1a1424; border:1px solid #3a2c52;"
            " border-radius:12px; }"
            "QPlainTextEdit#prompt-input { background:#120d1c; color:#fff;"
            " border:1px solid #2a1f3d; border-radius:8px; padding:8px 10px;"
            " font-size:13px; }"
            "QLabel#group-cap { color:#888; font-size:10px;"
            " background:transparent; }"
            # Сегмент-кнопки (режим / формат / кол-во)
            "QFrame#seg-group { background:#120d1c; border:1px solid #2a1f3d;"
            " border-radius:8px; }"
            "QPushButton#seg { background:transparent; color:rgba(255,255,255,0.55);"
            " border:none; border-radius:6px; padding:5px 12px; font-size:12px; }"
            "QPushButton#seg:hover { color:rgba(255,255,255,0.85); }"
            "QPushButton#seg[active=\"true\"] { background:rgba(255,255,255,0.08);"
            " color:#fff; }"
            # Выпадашка моделей
            "QComboBox#model-combo { background:#120d1c; color:#fff;"
            " border:1px solid #2a1f3d; border-radius:8px; padding:5px 10px;"
            " font-size:12px; min-width:150px; }"
            # Кнопка запуска (акцент — янтарь, как вкладка)
            "QPushButton#run-btn { background:#d4a256; color:#15101e;"
            " border:none; border-radius:10px; padding:8px; font-weight:600;"
            " min-width:44px; min-height:44px; }"
            "QPushButton#run-btn:hover { background:#e8b86a; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        root.addLayout(self._build_canvas_row())
        root.addWidget(self._build_results_area(), stretch=1)
        root.addWidget(self._build_prompt_bar())

    # ── (B) ряд холстов — заглушки ──────────────────────────────────────
    def _build_canvas_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        # Холст 1 активен (без ✕), 2 и 3 — с ✕. Пока ничего не переключают.
        for i in range(1, 4):
            row.addWidget(self._canvas_chip(f"Холст {i}", active=(i == 1)))
        new_btn = QPushButton("+ Новый")
        new_btn.setObjectName("canvas-new")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # заглушка: без обработчика
        row.addWidget(new_btn)
        row.addStretch()
        return row

    def _canvas_chip(self, title: str, active: bool) -> QFrame:
        chip = QFrame()
        chip.setObjectName("canvas-chip")
        chip.setProperty("active", active)
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(12, 6, 8 if not active else 12, 6)
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
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addStretch()
        empty = QLabel("Здесь появятся сгенерированные картинки")
        empty.setObjectName("results-empty")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        il.addWidget(empty)
        il.addStretch()
        area.setWidget(inner)
        return area

    # ── (D) поле промпта снизу — все элементы enabled, без обработчиков ──
    def _build_prompt_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("prompt-bar")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        # Ввод промпта
        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setObjectName("prompt-input")
        self.prompt_input.setPlaceholderText("Что хочешь сгенерировать?")
        self.prompt_input.setFixedHeight(64)
        outer.addWidget(self.prompt_input)

        # Ряд контролов
        ctl = QHBoxLayout()
        ctl.setSpacing(10)

        # Режим: Картинка / Видео (Картинка активна)
        self.mode_seg, self.mode_btns = self._seg_group(
            [("Картинка", "image"), ("Видео", "video")], active_key="image")
        ctl.addWidget(self.mode_seg)

        # Формат: 16:9 / 9:16 (16:9 активен)
        self.fmt_seg, self.fmt_btns = self._seg_group(
            [("16:9", "16:9"), ("9:16", "9:16")], active_key="16:9")
        ctl.addWidget(self.fmt_seg)

        # Количество: ×1..×4 (×1 активно)
        self.count_seg, self.count_btns = self._seg_group(
            [("×1", "1"), ("×2", "2"), ("×3", "3"), ("×4", "4")], active_key="1")
        ctl.addWidget(self.count_seg)

        # Модель (зависит от режима — заполняем для активного режима)
        self.model_combo = QComboBox()
        self.model_combo.setObjectName("model-combo")
        self.model_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self._populate_models(self._mode)
        ctl.addWidget(self.model_combo)

        ctl.addStretch()

        # Кнопка запуска (заглушка, без обработчика). Иконка Lucide wand-2.
        self.run_btn = QPushButton()
        self.run_btn.setObjectName("run-btn")
        self.run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ic = self._icon("wand-2")
        if ic is not None:
            self.run_btn.setIcon(ic)
            self.run_btn.setIconSize(QSize(20, 20))
        else:
            self.run_btn.setText("➤")  # fallback если иконка не нашлась
        ctl.addWidget(self.run_btn)

        outer.addLayout(ctl)
        return bar

    # ── helpers ─────────────────────────────────────────────────────────
    def _seg_group(self, items, active_key: str):
        """Сегмент-переключатель. items = [(label, key), ...]. Возвращает
        (QFrame группа, {key: QPushButton}). Кнопки enabled, БЕЗ обработчиков —
        активная помечена property active (визуально). Переключение привяжем позже."""
        grp = QFrame()
        grp.setObjectName("seg-group")
        lay = QHBoxLayout(grp)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(0)
        btns = {}
        for label, key in items:
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("active", key == active_key)
            b.setProperty("_seg_key", key)
            # заглушка: без .connect()
            lay.addWidget(b)
            btns[key] = b
        return grp, btns

    def _populate_models(self, mode: str):
        """Заполняет выпадашку моделей под режим. id модели — в itemData
        (userData), не в тексте. Вызов при смене режима — позже (1 connect)."""
        self.model_combo.clear()
        for label, mid in MODELS_BY_MODE.get(mode, []):
            self.model_combo.addItem(label, mid)
