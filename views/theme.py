# -*- coding: utf-8 -*-
"""
theme — фирменная тема LUMZ для Storyboard Studio.

Содержит:
  • LUMZ_THEME — словарь design tokens (цвета, радиусы). Все стили виджетов
    должны брать цвета ОТСЮДА, а не хардкодить hex по файлам. Это упрощает
    поддержку и гарантирует одинаковый вид на macOS / Win10 / Win11.
  • LumzBackground(QWidget) — кастомный фоновый виджет с радиальным
    градиентом: сверху по центру лёгкое фиолетово-синее свечение, к краям
    переход в глубокий чёрный #0a0a0d. Реализовано через QPainter (paintEvent),
    а не через QSS qradialgradient — paintEvent надёжнее работает на
    кросс-платформе при resize окна и retina-дисплеях.

История: создано 2026-05-08 на старте редизайна интерфейса под LUMZ-стиль
сайта. Этап 1 (фундамент). Виджеты на этом этапе НЕ перекрашиваются —
существующие стили работают поверх нового фона.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import QWidget


# ── Design Tokens ───────────────────────────────────────────────────────
# Цвета взяты с сайта lumz и согласованы с пользователем (Вариант 3).
# Никаких хардкод-цветов в виджетах — только через эти ключи.
LUMZ_THEME = {
    # Фон
    "bg_main": "#0a0a0d",
    "bg_panel": "rgba(20, 15, 30, 0.6)",
    "bg_card": "rgba(255, 255, 255, 0.03)",
    "bg_subtle": "rgba(255, 255, 255, 0.04)",
    "bg_hover": "rgba(255, 255, 255, 0.06)",

    # Границы
    "border_default": "rgba(255, 255, 255, 0.06)",
    "border_strong": "rgba(255, 255, 255, 0.12)",
    "border_subtle": "rgba(255, 255, 255, 0.04)",

    # Текст
    "text_primary": "#ffffff",
    "text_secondary": "rgba(255, 255, 255, 0.55)",
    "text_muted": "rgba(255, 255, 255, 0.4)",

    # Акцент — красный (главные action-кнопки, активный эпизод)
    "accent_red": "#e4344a",
    "accent_red_bg": "rgba(228, 52, 74, 0.15)",
    "accent_red_border": "rgba(228, 52, 74, 0.4)",
    "accent_red_subtle": "rgba(228, 52, 74, 0.1)",
    "accent_red_subtle_border": "rgba(228, 52, 74, 0.25)",

    # Акцент — золотой (плашка «СЕРИЯ NN», ссылка «Референсы»)
    "accent_gold": "#d4a256",
    "accent_gold_bg": "rgba(212, 162, 86, 0.1)",
    "accent_gold_border": "rgba(212, 162, 86, 0.3)",

    # v1.0.88 (Stage 10): акцент — зелёный (индикатор готовой,
    # ещё не просмотренной монтажки на пилюле эпизода). Tailwind
    # emerald-500 — современный, хорошо контрастирует с фиолетово-чёрным
    # LUMZ фоном, не слишком ядовито-яркий как Material Green A400.
    "accent_green": "#10B981",
    "accent_green_bg": "rgba(16, 185, 129, 0.15)",
    "accent_green_border": "rgba(16, 185, 129, 0.4)",

    # Радиусы скруглений
    "radius_sm": "6px",
    "radius_md": "8px",
    "radius_lg": "14px",
}


# Универсальный кросс-платформенный шрифт-стек.
# macOS подхватит SF Pro Display, Win11 — Segoe UI Variable, Win10 — Segoe UI.
# Никаких внешних файлов не подключаем — только системные.
LUMZ_FONT_STACK = (
    '"Helvetica Neue", "Segoe UI", Arial, sans-serif'
)


class LumzBackground(QWidget):
    """Главный фоновый виджет окна с радиальным градиентом.

    Рисует:
      1. Сплошной фон LUMZ_THEME["bg_main"] (#0a0a0d).
      2. Поверх — радиальное свечение с центром сверху по центру окна
         (cx=0.5, cy=0.0, радиус ≈ 70% высоты). Цвет свечения —
         мягкий фиолетово-синий (60, 50, 110, alpha=100), к краям
         прозрачность нарастает до полной (стоп 0.7).

    Использование:
      bg = LumzBackground()
      bg.setObjectName("main-bg")
      main_window.setCentralWidget(bg)
      # Дальше layout добавляется к bg как обычно.

    Кросс-платформенно: использует только Qt-Painter API, работает
    одинаково на Mac/Win10/Win11. На retina-дисплеях градиент
    автоматически масштабируется (Qt сам умножает на DPR).
    """

    def paintEvent(self, event):  # noqa: N802 (Qt-камелкейс)
        painter = QPainter(self)
        try:
            # 1. Базовая заливка — глубокий чёрный.
            painter.fillRect(self.rect(), QColor("#0a0a0d"))

            # 2. Радиальный градиент-свечение сверху по центру.
            # Центр — (width/2, 0). Радиус — 70% от ВЫСОТЫ (не ширины),
            # чтобы пятно света было «вертикальным» и не растягивалось
            # на широких мониторах.
            w = self.width()
            h = self.height()
            cx = w / 2.0
            cy = 0.0
            radius = max(h * 0.7, 1.0)
            gradient = QRadialGradient(cx, cy, radius)
            # alpha=100 (~40%) — мягкое свечение, не перекрывает контент
            gradient.setColorAt(0.0, QColor(60, 50, 110, 100))
            # к 70% радиуса — полностью прозрачный (стоп должен быть rgba(_,_,_,0))
            gradient.setColorAt(0.7, QColor(60, 50, 110, 0))
            painter.fillRect(self.rect(), QBrush(gradient))
        finally:
            painter.end()
        # Не вызываем super().paintEvent — QWidget по умолчанию не рисует
        # фон (если не задан autoFillBackground=True), а наш paint полностью
        # покрывает прямоугольник.


# ── Готовые стили кнопок LUMZ ──────────────────────────────────────────
# 2026-05-17: единый источник правды для стилей кнопок в LUMZ-стилистике.
# Раньше каждый виджет копипастил hex'ы LUMZ в свой setStyleSheet
# (gen_button.py, montage_cta.py, montage_summary_dialog.py и т.д.).
# Новые виджеты импортируют `lumz_button_qss` отсюда. Существующие
# модули с собственными inline-стилями НЕ трогаем — мигрировать
# можно отдельной задачей.

def lumz_button_qss(variant: str = 'primary', object_name: str = '') -> str:
    """Возвращает QSS-строку для кнопки в LUMZ-стилистике.

    Аргументы:
      variant:
        • 'primary'   — залитая accent_red (главное действие, CTA)
        • 'secondary' — нейтральная outline (вторичное действие)
        • 'subtle'    — transparent link-like (минимальное действие)
      object_name: если задан → селектор `QPushButton#<name>`;
        иначе `QPushButton` (применяется ко всем кнопкам в скоупе).
        Полезно склеивать несколько вариантов в одном setStyleSheet:
            dlg.setStyleSheet(
                "QDialog { background:#0a0a0d; }"
                + lumz_button_qss('primary', 'btn_save')
                + lumz_button_qss('subtle',  'btn_cancel')
            )
    """
    sel = f"QPushButton#{object_name}" if object_name else "QPushButton"
    if variant == 'primary':
        return (
            f"{sel} {{ background:#e4344a; color:#ffffff; border:none;"
            " border-radius:6px; padding:6px 14px;"
            " font-size:12px; font-weight:500; min-height:30px; }}"
            f"{sel}:hover {{ background:#d92d44; }}"
            f"{sel}:pressed {{ background:#c52539; }}"
            f"{sel}:disabled {{ background:rgba(255,255,255,0.06);"
            " color:rgba(255,255,255,0.40); }}"
        )
    if variant == 'secondary':
        return (
            f"{sel} {{ background:rgba(255,255,255,0.06);"
            " border:1px solid rgba(255,255,255,0.12);"
            " color:#ffffff; border-radius:6px;"
            " padding:6px 14px; font-size:12px; font-weight:500;"
            " min-height:30px; }}"
            f"{sel}:hover {{ background:rgba(255,255,255,0.10);"
            " border-color:rgba(255,255,255,0.20); }}"
            f"{sel}:disabled {{ color:rgba(255,255,255,0.40);"
            " border-color:rgba(255,255,255,0.06); }}"
        )
    if variant == 'subtle':
        return (
            f"{sel} {{ background:transparent;"
            " color:rgba(255,255,255,0.55); border:none;"
            " border-radius:6px; padding:6px 14px;"
            " font-size:12px; min-height:30px; }}"
            f"{sel}:hover {{ color:#ffffff;"
            " background:rgba(255,255,255,0.04); }}"
            f"{sel}:disabled {{ color:rgba(255,255,255,0.30); }}"
        )
    raise ValueError(f"unknown variant: {variant!r}")
