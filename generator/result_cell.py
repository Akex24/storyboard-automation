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

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QPainter, QPainterPath, QLinearGradient, QColor, QPixmap, QFont
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


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
        self._sec_timer = QTimer(self)
        self._sec_timer.setInterval(1000)
        self._sec_timer.timeout.connect(self._tick_seconds)
        self._sec_timer.start()

        # регистрируемся в общем shimmer-такте страницы
        try:
            self._page.register_loading(self)
        except Exception:
            pass

    # ── счётчик секунд ──────────────────────────────────────────────────
    def _tick_seconds(self):
        if self._state != "loading":
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
        self._original_pix = pix     # оригинал — для перемасштаба при смене размера
        self._info_lbl.hide()
        self._rescale_pixmap()
        self.update()

    def set_video_placeholder(self, path: str):
        """Готовое ВИДЕО: останавливаем shimmer/счётчик. Если видео-поток положил
        рядом кадр-превью gen_*.jpg (то же имя, .jpg) — грузим его фоном плитки через
        QPixmap (Qt понимает не-ASCII пути → отображение надёжно на Windows). Нет .jpg
        (не-ASCII путь и cv2 не смог) → тёмный фон + ▶. ▶ рисуется поверх в любом случае."""
        self._finish_common()
        self._state = "video"
        self._video_path = path
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

    def set_error(self, msg: str):
        self._finish_common()
        self._state = "error"
        self._pixmap = None
        # Шрифт мельче (11px) + wordWrap + поля 10px → длинная причина переносится
        # и помещается ЦЕЛИКОМ в плитке, не торчит за край.
        self._info_lbl.setStyleSheet(
            "color:#ffb3b3; font-size:11px; background:transparent;")
        self._info_lbl.setText(msg or "Ошибка")
        self._info_lbl.show()
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

        # ── VIDEO: кадр-превью (если поток извлёк gen_*.jpg) ИЛИ тёмный фон;
        # ▶ ВСЕГДА поверх (маркер «это видео») + бейдж. ──
        if self._state == "video":
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

        # Базовая тёмная плитка (loading-скелет).
        p.fillPath(path, _BASE_COLOR)

        if self._state == "loading":
            # ── ДЫШАЩАЯ БАЗА: яркость колеблется по sin(angle) бесшовно ────
            # pulse ∈ [0, 1], 0.5+0.5·sin(angle) — производная непрерывна, разрывов
            # нет. Никаких бегущих блик/wrap → «слепых зон» не существует.
            pulse = 0.5 + 0.5 * math.sin(self._angle)
            r = int(_BASE_DARK_R + (_BASE_LIGHT_R - _BASE_DARK_R) * pulse)
            gc = int(_BASE_DARK_G + (_BASE_LIGHT_G - _BASE_DARK_G) * pulse)
            b = int(_BASE_DARK_B + (_BASE_LIGHT_B - _BASE_DARK_B) * pulse)
            p.fillPath(path, QColor(r, gc, b))
            # ── СТАТИЧНЫЙ ВЕРТИКАЛЬНЫЙ ГРАДИЕНТ объёма (без анимации) ──────
            # Сверху чуть светлее, снизу чуть темнее — намёк на «глубину», как
            # у премиальных skeleton. От фазы НЕ зависит.
            gv = QLinearGradient(0, 0, 0, self.height())
            gv.setColorAt(0.0, _DEPTH_TOP)
            gv.setColorAt(1.0, _DEPTH_BOTTOM)
            p.fillPath(path, gv)
            # ── ОПЦИОНАЛЬНАЯ ТЁПЛАЯ ТОЧКА в верх-лев углу (статично) ──────
            # Янтарь с очень низкой α, мягкий радиальный градиент. Цветовой
            # нюанс «без скучноты», не двигается, бесплатно по CPU.
            from PyQt6.QtGui import QRadialGradient
            corner = QRadialGradient(0.0, 0.0, max(self.width(), self.height()) * 0.7)
            corner.setColorAt(0.0, _WARM_ACCENT_INNER)
            corner.setColorAt(1.0, _WARM_ACCENT_OUTER)
            p.fillPath(path, corner)

        # Бейдж модели поверх loading-плитки (сразу при старте; error сюда не доходит —
        # у него ранний return выше). На image бейдж рисуется в своей ветке.
        if self._model_label:
            self._draw_model_badge(p)
        p.setPen(_BORDER_COLOR)
        p.drawPath(path)
        p.end()
