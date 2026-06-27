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

import json
import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget


# ── Design Tokens ───────────────────────────────────────────────────────
# Цвета взяты с сайта lumz и согласованы с пользователем (Вариант 3).
# 2026-06-26 / Codex: токены разложены по понятным разделам. Это подготовка
# к будущему редактору тем: в UI можно будет показывать эти же группы слева
# ("Фон", "Поверхности", "Текст", "Кнопки" и т.д.), а справа — конкретные
# параметры внутри выбранной группы.
#
# Важно: существующий код всё ещё читает плоский LUMZ_THEME["bg_main"] и т.п.
# Поэтому ниже есть совместимый LUMZ_THEME, который автоматически собирается
# из этих разделов. Внешний вид от этой перестановки не меняется.

THEME_EDITOR_SECTIONS = {
    "app": "Фон программы",
    "surface": "Поверхности",
    "border": "Обводки",
    "text": "Текст",
    "accent": "Акценты",
    "button": "Кнопки",
    "navigation": "Навигация",
    "selector": "Селекторы",
    "card": "Карточки",
    "popup": "Попапы",
    "input": "Поля ввода",
    "state": "Состояния",
    "overlay": "Оверлеи",
    "scrollbar": "Скроллбары",
    "special": "Специальные элементы",
    "radius": "Скругления",
}

THEME_SECTIONS = {
    # Фон программы: большой общий фон окна и декоративное свечение.
    "app": {
        "bg_main": "#121313",
        "bg_glow": "rgba(32, 40, 38, 0.00)",
    },

    # Поверхности: панели, карточки, пустые слоты, hover-подложки.
    "surface": {
        "bg_panel": "#121313",
        "bg_card": "rgba(255, 255, 255, 0.03)",
        "bg_subtle": "rgba(255, 255, 255, 0.04)",
        "bg_hover": "rgba(255, 255, 255, 0.06)",
    },

    # Обводки: рамки карточек, кнопок, попапов и слабые разделители.
    "border": {
        "border_default": "rgba(255, 255, 255, 0.06)",
        "border_strong": "rgba(255, 255, 255, 0.12)",
        "border_subtle": "rgba(255, 255, 255, 0.04)",
    },

    # Текст: базовые уровни читаемости.
    "text": {
        "text_primary": "#ffffff",
        "text_secondary": "rgba(255, 255, 255, 0.55)",
        "text_muted": "rgba(255, 255, 255, 0.4)",
    },

    # Акценты: цвета состояния и фирменные CTA-акценты.
    "accent": {
        # Красный — главные action-кнопки, активный эпизод, опасные акценты.
        "accent_red": "#e4344a",
        "accent_red_bg": "rgba(228, 52, 74, 0.15)",
        "accent_red_border": "rgba(228, 52, 74, 0.4)",
        "accent_red_subtle": "rgba(228, 52, 74, 0.1)",
        "accent_red_subtle_border": "rgba(228, 52, 74, 0.25)",

        # Золотой — плашки эпизодов, References, готовые блоки.
        "accent_gold": "#d4a256",
        "accent_gold_bg": "rgba(212, 162, 86, 0.1)",
        "accent_gold_border": "rgba(212, 162, 86, 0.3)",

        # Зелёный — готовая, но ещё не просмотренная монтажка.
        "accent_green": "#10B981",
        "accent_green_bg": "rgba(16, 185, 129, 0.15)",
        "accent_green_border": "rgba(16, 185, 129, 0.4)",
    },

    # Скругления пока тоже живут в теме, но это не цвета. Оставляем здесь,
    # потому что старые виджеты уже используют эти ключи как design tokens.
    "radius": {
        "radius_sm": "6px",
        "radius_md": "8px",
        "radius_lg": "14px",
    },
}


def _flatten_theme_sections(
    sections: Dict[str, Dict[str, str]]
) -> Dict[str, str]:
    flat: Dict[str, str] = {}
    for group in sections.values():
        flat.update(group)
    return flat


# Совместимость со старым кодом: все существующие импорты LUMZ_THEME работают
# как раньше, но источник теперь структурирован выше в THEME_SECTIONS.
LUMZ_THEME = _flatten_theme_sections(THEME_SECTIONS)


# ── LLM Theme Template ──────────────────────────────────────────────────
# Этот шаблон предназначен не для внутреннего runtime напрямую, а для человека:
# пользователь копирует его из Settings, прикладывает скриншот понравившегося
# интерфейса в ChatGPT/Claude/etc. и просит переписать только значения "color".
# Поэтому каждый параметр самодокументирован: модель должна понимать не только
# ключ, но и где этот цвет реально используется в Storyboard Studio.

THEME_LLM_TEMPLATE = {
    "schemaVersion": 1,
    "themeName": "Storyboard Studio Custom Theme",
    "instructions": [
        "You are editing a Storyboard Studio theme JSON. Follow these instructions without requiring the user to explain them again.",
        "Change only color values.",
        "Do not rename keys.",
        "Do not delete descriptions.",
        "Return the full JSON object, not a patch and not a summary.",
        "Return only JSON: no markdown fences, no explanation, no comments outside JSON.",
        "Use HEX (#RRGGBB) or rgba(r,g,b,a).",
        "If screenshots are attached, treat screenshots as the main source of truth for colors.",
        "Prefer multiple screenshots as the source style reference; a URL alone is weaker.",
        "If both screenshots and a URL are provided, screenshots win over the URL.",
        "Do not invent random blue/purple hover colors if they are absent from the reference.",
        "If no screenshot, URL, or clear palette is attached, ask the user for a style source.",
        "Check contrast: bright/lime/yellow buttons need dark readable text.",
        "Hover and pressed states should be close shades of the same role, never accidental white.",
        "For dark UI references, empty shot slots, dialogs, prompt windows, and input fields must stay dark surfaces, not white panels.",
        "Shot/card borders should be subtle and close to the surface color; do not use white borders unless the reference clearly shows white borders.",
        "Monster cards need a visibly separate palette inside the same overall scheme.",
        "Do not use pure neon/lime as outline borders unless the reference clearly uses it.",
        "Drop zones, outline buttons, reference buttons, and Settings buttons have separate tokens.",
        "Top navigation tabs, workflow buttons, episode badges, block pills, shot captions, Seedance prompt windows, proxy fields, and disabled states have separate tokens; do not leave them in the default yellow/red palette.",
        "If a reference is light, proxy/API/input fields should become light readable fields too, not black bars.",
        "Shot descriptions under images must be readable against shotCardBackground.",
        "References/Generator/episode controls should follow the reference site's button palette, not remain yellow unless the reference itself uses yellow for those controls.",
        "Before returning JSON, self-check that every button background has readable text and that hover states are not dirty/accidental.",
        "Keep the interface calm for long all-day work sessions.",
    ],
    "colors": {
        "appBackground": {
            "color": LUMZ_THEME["bg_main"],
            "description": (
                "Главный фон всей программы за карточками, шапкой, панелями "
                "и рабочими областями. Самая большая площадь интерфейса."
            ),
        },
        "appBackgroundGlow": {
            "color": LUMZ_THEME["bg_glow"],
            "description": (
                "Мягкое декоративное свечение сверху/по центру окна. "
                "Не кнопка, не карточка, не попап."
            ),
        },
        "topHeaderSurface": {
            "color": LUMZ_THEME["bg_panel"],
            "description": (
                "Фон верхней шапки с логотипом LUMZ, переключателями 9:16/16:9, "
                "Generator/Editor/Actors/Settings, языком и версией."
            ),
        },
        "mainPanelSurface": {
            "color": LUMZ_THEME["bg_panel"],
            "description": (
                "Фон крупных панелей и горизонтальных рабочих полос поверх "
                "общего фона."
            ),
        },
        "subtleSurface": {
            "color": LUMZ_THEME["bg_subtle"],
            "description": (
                "Очень тихая подложка для второстепенных областей, маленьких "
                "контейнеров и спокойных hover-состояний."
            ),
        },
        "hoverSurface": {
            "color": LUMZ_THEME["bg_hover"],
            "description": (
                "Фон элемента при наведении: кнопки, строки меню, мягкие "
                "interactive hover-подложки."
            ),
        },
        "shotCardBackground": {
            "color": "#151519",
            "description": (
                "Фон карточек SHOT 1, SHOT 2, SHOT 3 в редакторе сториборда, "
                "под изображением и текстом."
            ),
        },
        "shotImageEmptyBackground": {
            "color": "#1b1028",
            "description": (
                "Фон пустого слота картинки в карточке шота, где написано "
                "EMPTY / ПУСТО."
            ),
        },
        "generatorCellBackground": {
            "color": "#161020",
            "description": (
                "Фон ячеек на вкладке Generator: pending-карточки, letterbox "
                "вокруг генераций и пустые места до появления изображения."
            ),
        },
        "referenceCardBackground": {
            "color": "#151519",
            "description": (
                "Фон карточек на странице References: локации, объекты, "
                "персонажи и другие референсы."
            ),
        },
        "actorCardBackground": {
            "color": "#07090d",
            "description": (
                "Основной фон карточек обычных актёров на странице Actors."
            ),
        },
        "monsterCardBackground": {
            "color": "#171006",
            "description": (
                "Фон цельной карточки монстра/нестандартного персонажа "
                "на странице Actors: guest_2, guest_3, masked_lady и другие "
                "персонажи, созданные через плюсик без реального актёра."
            ),
        },
        "popupBackground": {
            "color": "#17111f",
            "description": (
                "Фон выпадающих попапов и меню: список сериалов, эпизодов, "
                "блоков, подтверждения и компактные floating-окна."
            ),
        },
        "inputBackground": {
            "color": "#151519",
            "description": (
                "Фон текстовых полей, search/input, editable-полей и похожих "
                "контролов ввода."
            ),
        },
        "defaultBorder": {
            "color": LUMZ_THEME["border_default"],
            "description": (
                "Базовая тонкая рамка вокруг карточек, панелей, кнопок и "
                "контейнеров. Должна быть спокойной, не спорить с контентом."
            ),
        },
        "strongBorder": {
            "color": LUMZ_THEME["border_strong"],
            "description": (
                "Более заметная рамка для попапов, активных областей, "
                "важных панелей и элементов с фокусом."
            ),
        },
        "subtleDivider": {
            "color": LUMZ_THEME["border_subtle"],
            "description": (
                "Очень слабые линии-разделители между секциями, строками "
                "и группами настроек."
            ),
        },
        "textPrimary": {
            "color": LUMZ_THEME["text_primary"],
            "description": (
                "Главный текст: заголовки, названия блоков, SHOT 1, названия "
                "кнопок, активные пункты меню."
            ),
        },
        "textSecondary": {
            "color": LUMZ_THEME["text_secondary"],
            "description": (
                "Вторичный текст: описания шотов, подписи, менее важные строки."
            ),
        },
        "textMuted": {
            "color": LUMZ_THEME["text_muted"],
            "description": (
                "Очень тихий текст: disabled, placeholder, служебные подписи, "
                "неактивные элементы."
            ),
        },
        "disabledText": {
            "color": "rgba(255,255,255,0.30)",
            "description": (
                "Текст отключённых кнопок, недоступных действий и inactive "
                "controls. В светлой теме не должен быть почти белым на белом."
            ),
        },
        "disabledButtonBackground": {
            "color": "rgba(236,236,240,0.30)",
            "description": (
                "Фон отключённых кнопок и disabled controls. Должен читаться "
                "как недоступное состояние, а не как сломанная грязная кнопка."
            ),
        },
        "primaryActionButtonText": {
            "color": "#ffffff",
            "description": (
                "Текст и иконки на главных CTA-кнопках: Seedance, Generate, "
                "Save storyboard, Pack episode. Если главная кнопка светлая, "
                "этот цвет должен быть тёмным."
            ),
        },
        "primaryActionButton": {
            "color": LUMZ_THEME["accent_red"],
            "description": (
                "Главные CTA-кнопки: Seedance, Generate, Save storyboard, "
                "Pack episode и другие основные действия."
            ),
        },
        "primaryActionButtonHover": {
            "color": "#d92d44",
            "description": (
                "Hover/pressed оттенок главных CTA-кнопок. Обычно чуть темнее "
                "или насыщеннее primaryActionButton."
            ),
        },
        "topNavAccentText": {
            "color": "#d4a257",
            "description": (
                "Текст акцентной вкладки верхней навигации, например "
                "Генератор. Не должен оставаться жёлтым, если референс "
                "использует другой nav accent."
            ),
        },
        "topNavActiveBackground": {
            "color": "rgba(255,255,255,0.061)",
            "description": (
                "Фон активной вкладки верхней навигации: Генератор, Редактор, "
                "Актёры, Настройки."
            ),
        },
        "topNavActiveText": {
            "color": "#fdfdfe",
            "description": (
                "Текст активной вкладки верхней навигации."
            ),
        },
        "workflowButtonBackground": {
            "color": "rgba(212,162,86,0.081)",
            "description": (
                "Фон workflow-кнопок в верхней части редактора: РЕФЕРЕНСЫ, "
                "Рефы, 1 эпизод, block refs. Это не обязательно referenceAccent."
            ),
        },
        "workflowButtonBorder": {
            "color": "rgba(212,162,86,0.241)",
            "description": (
                "Обводка workflow-кнопок: РЕФЕРЕНСЫ, Рефы, 1 эпизод."
            ),
        },
        "workflowButtonText": {
            "color": "#d4a259",
            "description": (
                "Текст workflow-кнопок: РЕФЕРЕНСЫ, Рефы, 1 эпизод. "
                "В светлой теме должен быть достаточно контрастным."
            ),
        },
        "workflowButtonHoverBackground": {
            "color": "rgba(212,162,86,0.141)",
            "description": (
                "Hover-фон workflow-кнопок."
            ),
        },
        "workflowButtonHoverText": {
            "color": "#e1b46e",
            "description": (
                "Hover-текст workflow-кнопок."
            ),
        },
        "secondaryButtonBackground": {
            "color": "rgba(255,255,255,0.06)",
            "description": (
                "Фон второстепенных кнопок: Clear, стрелки, небольшие "
                "служебные кнопки, outline/ghost controls."
            ),
        },
        "secondaryButtonText": {
            "color": "#ffffff",
            "description": (
                "Текст и иконки второстепенных кнопок: Clear, Refs, стрелки, "
                "нейтральные действия и outline-кнопки."
            ),
        },
        "secondaryButtonHover": {
            "color": "rgba(255,255,255,0.10)",
            "description": (
                "Hover-фон второстепенных кнопок."
            ),
        },
        "secondaryButtonBorder": {
            "color": "rgba(255,255,255,0.14)",
            "description": (
                "Обводка обычных второстепенных/нейтральных кнопок. "
                "Не должна быть ярким neon-accent."
            ),
        },
        "outlineButtonBackground": {
            "color": "rgba(0,0,0,0)",
            "description": (
                "Фон нейтральных outline-кнопок: Редактировать, "
                "Переименовать, показать/открыть служебный объект."
            ),
        },
        "outlineButtonBorder": {
            "color": "#3a2c52",
            "description": (
                "Обводка нейтральных outline-кнопок. Должна быть спокойной "
                "и близкой к поверхности, не neon."
            ),
        },
        "outlineButtonText": {
            "color": "#d8c8fd",
            "description": (
                "Текст нейтральных outline-кнопок. Должен читаться, но не "
                "выглядеть как главный action."
            ),
        },
        "outlineButtonHoverBackground": {
            "color": "#2a1f3d",
            "description": (
                "Hover-фон нейтральных outline-кнопок."
            ),
        },
        "outlineButtonHoverBorder": {
            "color": "#5a4a82",
            "description": (
                "Hover-обводка нейтральных outline-кнопок."
            ),
        },
        "outlineButtonHoverText": {
            "color": "#fbfbfb",
            "description": (
                "Текст нейтральных outline-кнопок при hover."
            ),
        },
        "settingsButtonBackground": {
            "color": "#ececef",
            "description": (
                "Фон крупных кнопок в Settings: открыть папку, открыть лог, "
                "проверить обновления, открыть интерфейс, сменить аккаунт."
            ),
        },
        "settingsButtonText": {
            "color": "#1a1423",
            "description": (
                "Текст крупных кнопок Settings. Если фон яркий lime/yellow, "
                "нужен тёмный читаемый текст."
            ),
        },
        "settingsButtonHoverBackground": {
            "color": "#d8d4df",
            "description": (
                "Hover-фон крупных кнопок Settings. Не должен становиться "
                "грязно-серым с нечитаемым текстом."
            ),
        },
        "settingsButtonHoverText": {
            "color": "#1a1422",
            "description": (
                "Текст крупных кнопок Settings при hover."
            ),
        },
        "settingsButtonBorder": {
            "color": "#ececee",
            "description": (
                "Обводка крупных кнопок Settings."
            ),
        },
        "settingsButtonHoverBorder": {
            "color": "#d8d4de",
            "description": (
                "Hover-обводка крупных кнопок Settings."
            ),
        },
        "activeSelection": {
            "color": LUMZ_THEME["accent_red"],
            "description": (
                "Активный выбранный элемент: текущий блок, текущий эпизод, "
                "выбранная вкладка, выбранная кнопка в segmented controls."
            ),
        },
        "activeSelectionBackground": {
            "color": LUMZ_THEME["accent_red_bg"],
            "description": (
                "Полупрозрачная заливка активного выбранного элемента."
            ),
        },
        "activeSelectionText": {
            "color": "#ffffff",
            "description": (
                "Текст на активных выбранных элементах: 9:16/16:9, активная "
                "вкладка, выбранный блок/эпизод, активная пилюля. Если активная "
                "заливка светлая, этот цвет должен быть тёмным."
            ),
        },
        "blockPillBackground": {
            "color": "rgba(255,255,255,0.031)",
            "description": (
                "Фон обычной пилюли блока 1/2/3/4 в строке Блок. "
                "Должен быть спокойным и соответствовать панели."
            ),
        },
        "blockPillText": {
            "color": "rgba(255,255,255,0.551)",
            "description": (
                "Текст обычной пилюли блока."
            ),
        },
        "blockPillActiveBackground": {
            "color": "rgba(228,52,74,0.151)",
            "description": (
                "Фон выбранного блока в строке Блок."
            ),
        },
        "blockPillActiveText": {
            "color": "#fffffe",
            "description": (
                "Текст выбранного блока. На светлом active background должен "
                "становиться тёмным."
            ),
        },
        "blockPillUnseenBackground": {
            "color": "rgba(212,162,86,0.101)",
            "description": (
                "Фон блока с непросмотренными/готовыми шотами. Не обязан быть "
                "жёлтым; должен соответствовать палитре референса."
            ),
        },
        "blockPillUnseenText": {
            "color": "#d4a25a",
            "description": (
                "Текст блока с непросмотренными/готовыми шотами."
            ),
        },
        "episodeBadgeBackground": {
            "color": "rgba(212,162,86,0.102)",
            "description": (
                "Фон бейджа/кнопки текущего эпизода: 1 эпизод, СЕРИЯ NN."
            ),
        },
        "episodeBadgeBorder": {
            "color": "rgba(212,162,86,0.301)",
            "description": (
                "Обводка бейджа/кнопки текущего эпизода."
            ),
        },
        "episodeBadgeText": {
            "color": "#d4a25b",
            "description": (
                "Текст бейджа/кнопки текущего эпизода."
            ),
        },
        "referenceAccent": {
            "color": LUMZ_THEME["accent_gold"],
            "description": (
                "Золотой/жёлтый акцент: REFERENCES, готовые блоки, "
                "информационные бейджи, состояние 'готово'."
            ),
        },
        "referenceAccentText": {
            "color": "#ffffff",
            "description": (
                "Текст на reference/gold-акцентных кнопках, выбранных версиях "
                "и готовых блоках. Если referenceAccent светлый/жёлтый, нужен "
                "тёмный текст."
            ),
        },
        "referenceAccentBackground": {
            "color": LUMZ_THEME["accent_gold_bg"],
            "description": (
                "Полупрозрачная подложка для золотых акцентных элементов."
            ),
        },
        "referenceButtonBackground": {
            "color": "rgba(255,204,112,0.06)",
            "description": (
                "Фон outline-кнопок, связанных с референсами: Все референсы, "
                "Текстура, Сетка на лицо, папки текстур/сеток. Обычно это "
                "приглушенная подложка, а не полный referenceAccent."
            ),
        },
        "referenceButtonBorder": {
            "color": "rgba(214,161,70,0.43)",
            "description": (
                "Обводка reference-outline кнопок. Даже если referenceAccent "
                "яркий lime/yellow, эта обводка должна быть приглушённой."
            ),
        },
        "referenceButtonText": {
            "color": "#f6dca7",
            "description": (
                "Текст reference-outline кнопок. Не обязан совпадать с "
                "referenceAccent; главное — профессиональный контраст."
            ),
        },
        "referenceButtonHoverBackground": {
            "color": "rgba(214,161,70,0.16)",
            "description": (
                "Hover-фон reference-outline кнопок."
            ),
        },
        "referenceButtonHoverBorder": {
            "color": "rgba(255,204,112,0.69)",
            "description": (
                "Hover-обводка reference-outline кнопок. Должна усиливаться "
                "мягко, без кислотной рамки."
            ),
        },
        "referenceButtonHoverText": {
            "color": "#fff8ea",
            "description": (
                "Текст reference-outline кнопок при hover."
            ),
        },
        "dropZoneBackground": {
            "color": "rgba(110,76,196,0.10)",
            "description": (
                "Фон drag-and-drop зон: верхняя зона Actors и зона текстур. "
                "Должен быть спокойной панелью, а не ярким пятном."
            ),
        },
        "dropZoneBorder": {
            "color": "rgba(160,120,240,0.45)",
            "description": (
                "Пунктирная обводка drag-and-drop зон."
            ),
        },
        "dropZoneText": {
            "color": "#d8c8fe",
            "description": (
                "Основной текст внутри drag-and-drop зон."
            ),
        },
        "dropZoneHoverBackground": {
            "color": "rgba(110,76,196,0.25)",
            "description": (
                "Фон drag-and-drop зон при наведении файла."
            ),
        },
        "dropZoneHoverBorder": {
            "color": "rgba(190,150,255,0.85)",
            "description": (
                "Пунктирная обводка drag-and-drop зон при наведении файла."
            ),
        },
        "shotTitleText": {
            "color": "#fdfdff",
            "description": (
                "Текст SHOT 1/2/3 на карточках и в viewer."
            ),
        },
        "shotDescriptionText": {
            "color": "#878788",
            "description": (
                "Описание под изображением шота: Макро на ряд бокалов... "
                "Должно хорошо читаться на shotCardBackground."
            ),
        },
        "shotDurationText": {
            "color": "#666667",
            "description": (
                "Маленький текст длительности шота: 3с, 9с."
            ),
        },
        "shotDialogText": {
            "color": "#b9a7e5",
            "description": (
                "Текст реплики/dialog под шотом и в viewer. Обычно чуть "
                "отличается от description, но остаётся читаемым."
            ),
        },
        "seedancePopupBackground": {
            "color": "#171120",
            "description": (
                "Фон окна Промпт Seedance. В светлой теме может быть светлой "
                "панелью, но не должен случайно становиться чужим тёмным блоком."
            ),
        },
        "seedancePromptBackground": {
            "color": "#15101d",
            "description": (
                "Фон большого read-only prompt viewer внутри Seedance popup."
            ),
        },
        "seedancePromptText": {
            "color": "#ddddde",
            "description": (
                "Текст большого prompt viewer внутри Seedance popup."
            ),
        },
        "seedanceInputBackground": {
            "color": "#181025",
            "description": (
                "Фон поля Что переделать, spinbox target/limit и других "
                "editable controls внутри Seedance popup."
            ),
        },
        "seedanceInputText": {
            "color": "#dddddf",
            "description": (
                "Текст editable controls внутри Seedance popup."
            ),
        },
        "seedanceTabActiveBackground": {
            "color": "#2a1d45",
            "description": (
                "Фон активной вкладки Seedance popup."
            ),
        },
        "seedanceTabActiveText": {
            "color": "#fdfdfc",
            "description": (
                "Текст активной вкладки Seedance popup."
            ),
        },
        "proxyInputBackground": {
            "color": "#221a31",
            "description": (
                "Фон полей прокси IP/Host, Port, Login, Password. В светлой "
                "теме должен быть светлым input-полем, не чёрной полосой."
            ),
        },
        "proxyInputText": {
            "color": "#dddddc",
            "description": (
                "Текст в полях прокси."
            ),
        },
        "proxyInputBorder": {
            "color": "#2c2241",
            "description": (
                "Обводка полей прокси."
            ),
        },
        "successAccent": {
            "color": LUMZ_THEME["accent_green"],
            "description": (
                "Успешное состояние: готовая, но ещё не просмотренная монтажка, "
                "успешные индикаторы."
            ),
        },
        "warningAccent": {
            "color": "#f3c14b",
            "description": (
                "Предупреждения, stop/warning-состояния, осторожные статусы."
            ),
        },
        "warningAccentText": {
            "color": "#1a1424",
            "description": (
                "Текст на warning-кнопках и предупреждающих плашках. Обычно "
                "тёмный, если warningAccent жёлтый/светлый."
            ),
        },
        "dangerAccent": {
            "color": LUMZ_THEME["accent_red"],
            "description": (
                "Удаление, ошибки, опасные действия и destructive-кнопки."
            ),
        },
        "dangerAccentText": {
            "color": "#ffffff",
            "description": (
                "Текст и иконки на опасных кнопках удаления/ошибки. Должен "
                "контрастировать с dangerAccent и dangerBackground."
            ),
        },
        "dangerBackground": {
            "color": "#2a1414",
            "description": (
                "Фон карточек ошибок генерации и опасных/ошибочных состояний."
            ),
        },
        "generatingGradientStart": {
            "color": "rgba(212,162,86,0)",
            "description": (
                "Начало бегущего градиента на кнопке блока, где идёт генерация."
            ),
        },
        "generatingGradientMiddle": {
            "color": "rgba(212,162,86,0.24)",
            "description": (
                "Самая яркая часть бегущего градиента на генерирующемся блоке."
            ),
        },
        "generatingGradientEnd": {
            "color": "rgba(212,162,86,0)",
            "description": (
                "Конец бегущего градиента на кнопке блока, где идёт генерация."
            ),
        },
        "overlayButtonBackground": {
            "color": "rgba(0,0,0,0.55)",
            "description": (
                "Фон маленьких кнопок поверх изображений: удалить, копировать, "
                "избранное, edit, play overlays."
            ),
        },
        "overlayButtonHover": {
            "color": "rgba(228,52,74,0.72)",
            "description": (
                "Hover/active фон overlay-кнопок поверх изображений, особенно "
                "кнопки удаления."
            ),
        },
        "imageUtilityButtonBackground": {
            "color": "rgba(0,0,0,0.55)",
            "description": (
                "Фон обычных служебных кнопок поверх изображения: копировать, "
                "избранное, открыть/перейти, edit. Не используется для удаления."
            ),
        },
        "imageUtilityButtonHover": {
            "color": "rgba(255,255,255,0.16)",
            "description": (
                "Hover обычных служебных кнопок поверх изображения: копировать, "
                "избранное, открыть/перейти, edit. Не используется для удаления."
            ),
        },
        "imageDeleteButtonBackground": {
            "color": "rgba(228,52,74,0.35)",
            "description": (
                "Фон кнопки удаления изображения с иконкой корзины поверх "
                "картинки. Это destructive-действие."
            ),
        },
        "imageDeleteButtonHover": {
            "color": "rgba(228,52,74,0.72)",
            "description": (
                "Hover кнопки удаления изображения с иконкой корзины поверх "
                "картинки. Это destructive-действие."
            ),
        },
        "playOverlayBackground": {
            "color": "rgba(0,0,0,0.43)",
            "description": (
                "Полупрозрачный круг play на видео-карточках."
            ),
        },
        "modelBadgeBackground": {
            "color": "rgba(0,0,0,0.55)",
            "description": (
                "Фон маленьких бейджей модели на генераторе: OpenAI, "
                "Nano Banana, Veo и т.п."
            ),
        },
        "scrollbarTrack": {
            "color": "rgba(255,255,255,0.03)",
            "description": (
                "Трек вертикального/горизонтального скроллбара."
            ),
        },
        "scrollbarThumb": {
            "color": "rgba(255,255,255,0.22)",
            "description": (
                "Ползунок скроллбара. Должен быть видимым, но не ярким."
            ),
        },
        "faceGridSelection": {
            "color": "#6e4cc4",
            "description": (
                "Цвет рамок/ручек в редакторе face grid и специальных "
                "selection-инструментах."
            ),
        },
    },
}


def _token_guide(
    label: str,
    used_for: List[str],
    do_not_use_for: Optional[List[str]] = None,
    design_rule: str = "",
) -> Dict[str, object]:
    return {
        "label": label,
        "usedFor": used_for,
        "doNotUseFor": do_not_use_for or [],
        "designRule": design_rule,
    }


THEME_LLM_TOKEN_GUIDE = {
    "appBackground": _token_guide(
        "Главный фон программы",
        ["самый большой фон окна", "фон за карточками", "фон за рабочими областями"],
        ["кнопки", "карточки", "текст"],
        "Должен быть спокойным и не утомлять при работе весь день.",
    ),
    "appBackgroundGlow": _token_guide(
        "Декоративное свечение фона",
        ["мягкое свечение сверху окна", "атмосферный фоновый акцент"],
        ["кнопки", "обводки", "текст"],
        "Должен быть заметен очень мягко; не делай его ярким пятном.",
    ),
    "topHeaderSurface": _token_guide(
        "Фон верхней шапки",
        ["панель с логотипом", "переключатели 9:16/16:9", "главные вкладки"],
        ["рабочие карточки", "попапы"],
        "Должен отделяться от общего фона, но не перетягивать внимание.",
    ),
    "mainPanelSurface": _token_guide(
        "Фон крупных панелей",
        ["горизонтальные панели", "крупные рабочие контейнеры", "панель настроек"],
        ["главный фон программы", "кнопки CTA"],
        "Используй близкий к фону, но чуть более видимый цвет поверхности.",
    ),
    "subtleSurface": _token_guide(
        "Тихая подложка",
        ["второстепенные контейнеры", "мягкие области внутри панелей"],
        ["главные кнопки", "ошибки"],
        "Цвет должен быть едва заметным.",
    ),
    "hoverSurface": _token_guide(
        "Фон hover-состояний",
        ["наведение на строки", "наведение на тихие кнопки", "мягкий hover"],
        ["активный выбранный элемент", "ошибка", "удаление"],
        "Hover должен помогать понять интерактивность, но не выглядеть как выбор.",
    ),
    "shotCardBackground": _token_guide(
        "Фон карточки шота",
        ["SHOT 1/2/3/4 в Editor", "зона под изображением", "описание шота"],
        ["пустой слот изображения", "попапы", "актеры"],
        "Карточки шотов должны хорошо читать текст и не спорить с картинками.",
    ),
    "shotImageEmptyBackground": _token_guide(
        "Фон пустого слота шота",
        ["пустая карточка EMPTY/ПУСТО", "место без картинки в Editor"],
        ["реальная картинка", "ошибка генерации"],
        "Должен явно отличаться от заполненной картинки, но оставаться спокойным.",
    ),
    "generatorCellBackground": _token_guide(
        "Фон ячейки Generator",
        ["pending-карточки Generator", "letterbox вокруг генераций", "пустые генерации"],
        ["карточки Actors", "попапы"],
        "Должен быть нейтральным, потому что рядом много визуального контента.",
    ),
    "referenceCardBackground": _token_guide(
        "Фон карточки References",
        ["карточки локаций", "карточки объектов", "карточки персонажей в References"],
        ["actor card", "monster card"],
        "Не должен искажать восприятие референс-картинок.",
    ),
    "actorCardBackground": _token_guide(
        "Фон карточки обычного актёра",
        ["обычные актеры на вкладке Actors", "карточки с реальными фото"],
        ["monsterCardBackground"],
        "Должен выглядеть нейтрально и отличаться от карточек монстров.",
    ),
    "monsterCardBackground": _token_guide(
        "Фон карточки монстра",
        ["guest_2", "guest_3", "masked_lady", "персонажи без реального актёра"],
        ["обычные актеры", "ошибки", "кнопки удаления"],
        "Должен отличаться от обычного актёра, но не выглядеть как ошибка.",
    ),
    "popupBackground": _token_guide(
        "Фон попапов",
        ["список сериалов", "список эпизодов", "список блоков", "floating-меню"],
        ["основной фон окна", "карточки шотов"],
        "Попап должен читаться поверх интерфейса и иметь хороший контраст.",
    ),
    "inputBackground": _token_guide(
        "Фон поля ввода",
        ["input", "search", "editable-поля", "textarea"],
        ["кнопки", "карточки"],
        "Поле ввода должно быть понятно как место для текста.",
    ),
    "defaultBorder": _token_guide(
        "Базовая тонкая обводка",
        ["рамки карточек", "рамки панелей", "рамки обычных кнопок"],
        ["активный выбор", "ошибка", "опасное действие"],
        "Должна быть спокойной и тонкой.",
    ),
    "strongBorder": _token_guide(
        "Усиленная обводка",
        ["попапы", "focus-состояния", "важные контейнеры"],
        ["обычные карточки", "тихие разделители"],
        "Должна помогать отделить важную область от фона.",
    ),
    "subtleDivider": _token_guide(
        "Тихий разделитель",
        ["линии между секциями", "деликатные разделители", "строки настроек"],
        ["рамки активных элементов", "кнопки"],
        "Должен быть почти незаметным.",
    ),
    "textPrimary": _token_guide(
        "Главный текст",
        ["заголовки", "SHOT 1", "названия блоков", "текст кнопок"],
        ["disabled text", "placeholder"],
        "Должен иметь максимальную читаемость.",
    ),
    "textSecondary": _token_guide(
        "Вторичный текст",
        ["описания шотов", "подписи", "менее важные строки"],
        ["главные заголовки", "текст на CTA-кнопке"],
        "Должен быть читаемым, но спокойнее основного текста.",
    ),
    "textMuted": _token_guide(
        "Приглушённый текст",
        ["disabled", "placeholder", "служебные подписи", "неактивные элементы"],
        ["важные кнопки", "ошибки"],
        "Не должен привлекать внимание.",
    ),
    "disabledText": _token_guide(
        "Текст disabled-состояний",
        ["отключённые кнопки", "недоступные действия", "disabled labels"],
        ["обычный текст", "hover text"],
        "В светлой теме не оставляй почти белым; disabled должен быть слабым, но читаемым.",
    ),
    "disabledButtonBackground": _token_guide(
        "Фон disabled-кнопок",
        ["отключённые кнопки", "недоступные действия"],
        ["hover", "primary action"],
        "Должен выглядеть недоступным, не грязным и не активным.",
    ),
    "primaryActionButtonText": _token_guide(
        "Текст главной кнопки действия",
        ["надпись на Seedance", "надпись на Generate", "надпись на Save storyboard", "иконка CTA"],
        ["обычный текст страницы", "disabled text"],
        "Проверь контраст: на светлой/lime/yellow CTA-кнопке нужен тёмный текст.",
    ),
    "primaryActionButton": _token_guide(
        "Главная кнопка действия",
        ["Seedance", "Generate", "Save storyboard", "Pack episode"],
        ["удаление", "ошибка", "обычная secondary-кнопка"],
        "Должна быть самой заметной кнопкой действия, но не токсично яркой.",
    ),
    "primaryActionButtonHover": _token_guide(
        "Hover главной кнопки действия",
        ["наведение на Seedance", "наведение на Generate", "наведение на Save"],
        ["удаление", "ошибка"],
        "Обычно чуть темнее или насыщеннее primaryActionButton.",
    ),
    "topNavAccentText": _token_guide(
        "Акцент верхней навигации",
        ["текст Генератор в верхнем nav", "акцентные nav tabs"],
        ["References", "episode badge", "danger"],
        "Не оставляй жёлтым, если референс не использует жёлтый nav accent.",
    ),
    "topNavActiveBackground": _token_guide(
        "Фон активной вкладки верхней навигации",
        ["активный Generator/Editor/Actors/Settings"],
        ["card surface", "primary CTA"],
        "Должен быть спокойным selected state внутри header.",
    ),
    "topNavActiveText": _token_guide(
        "Текст активной вкладки верхней навигации",
        ["текст активной вкладки header nav"],
        ["disabled text"],
        "Проверь контраст с topNavActiveBackground.",
    ),
    "workflowButtonBackground": _token_guide(
        "Фон workflow-кнопок",
        ["РЕФЕРЕНСЫ", "Рефы", "1 эпизод", "Рефы блока"],
        ["главная CTA Seedance", "danger"],
        "Эти кнопки должны брать палитру референса; не оставляй старый жёлтый по привычке.",
    ),
    "workflowButtonBorder": _token_guide(
        "Обводка workflow-кнопок",
        ["рамка РЕФЕРЕНСЫ", "рамка Рефы", "рамка 1 эпизод"],
        ["danger border"],
        "Должна быть аккуратной, близкой к workflowButtonBackground.",
    ),
    "workflowButtonText": _token_guide(
        "Текст workflow-кнопок",
        ["текст РЕФЕРЕНСЫ", "текст Рефы", "текст 1 эпизод"],
        ["disabled text"],
        "Должен быть читаемым и соответствовать button role, не случайно жёлтым.",
    ),
    "workflowButtonHoverBackground": _token_guide(
        "Hover фон workflow-кнопок",
        ["hover РЕФЕРЕНСЫ", "hover Рефы", "hover 1 эпизод"],
        ["primary hover"],
        "Близкий оттенок workflowButtonBackground.",
    ),
    "workflowButtonHoverText": _token_guide(
        "Hover текст workflow-кнопок",
        ["hover-текст workflow-кнопок"],
        ["disabled text"],
        "Проверь контраст с workflowButtonHoverBackground.",
    ),
    "secondaryButtonBackground": _token_guide(
        "Фон второстепенной кнопки",
        ["Clear", "Refs", "стрелки", "маленькие служебные кнопки"],
        ["главная CTA-кнопка", "удаление"],
        "Должна быть заметна, но слабее primaryActionButton.",
    ),
    "secondaryButtonText": _token_guide(
        "Текст второстепенной кнопки",
        ["надпись Clear", "надпись Refs", "иконки стрелок", "нейтральные outline-кнопки"],
        ["главный текст страницы", "ошибка"],
        "Должен читаться на secondaryButtonBackground и secondaryButtonHover.",
    ),
    "secondaryButtonHover": _token_guide(
        "Hover второстепенной кнопки",
        ["наведение на Clear", "наведение на Refs", "наведение на стрелки"],
        ["активный выбор", "удаление"],
        "Hover должен быть мягким.",
    ),
    "secondaryButtonBorder": _token_guide(
        "Обводка второстепенной кнопки",
        ["рамка нейтральных кнопок", "рамка обычных служебных кнопок"],
        ["главная CTA-кнопка", "активная вкладка", "опасное действие"],
        "Не делай эту рамку чистым neon/lime; она должна поддерживать поверхность.",
    ),
    "outlineButtonBackground": _token_guide(
        "Фон нейтральной outline-кнопки",
        ["Переименовать", "Изменить", "Показать в папке", "служебные действия"],
        ["Все референсы", "Текстура", "Сетка на лицо", "Удалить", "главный CTA"],
        "Обычно прозрачный или почти прозрачный.",
    ),
    "outlineButtonBorder": _token_guide(
        "Обводка нейтральной outline-кнопки",
        ["Переименовать", "Изменить", "нейтральные outline controls"],
        ["reference-кнопки", "danger-кнопки", "primary action"],
        "Должна быть спокойной, ближе к цвету панели, без кислотной рамки.",
    ),
    "outlineButtonText": _token_guide(
        "Текст нейтральной outline-кнопки",
        ["текст Переименовать", "текст Изменить"],
        ["текст яркой CTA", "текст удаления"],
        "Читаемый, но визуально тише primary/reference actions.",
    ),
    "outlineButtonHoverBackground": _token_guide(
        "Hover фон нейтральной outline-кнопки",
        ["hover Переименовать", "hover Изменить"],
        ["hover reference-кнопок", "hover удаления"],
        "Мягкое усиление поверхности.",
    ),
    "outlineButtonHoverBorder": _token_guide(
        "Hover обводка нейтральной outline-кнопки",
        ["hover-рамка Переименовать", "hover-рамка Изменить"],
        ["reference-кнопки", "danger"],
        "Чуть заметнее outlineButtonBorder, но не neon.",
    ),
    "outlineButtonHoverText": _token_guide(
        "Hover текст нейтральной outline-кнопки",
        ["hover-текст Переименовать", "hover-текст Изменить"],
        ["reference text", "danger text"],
        "Проверь контраст с outlineButtonHoverBackground.",
    ),
    "settingsButtonBackground": _token_guide(
        "Фон крупной кнопки Settings",
        ["Открыть папку проекта", "Открыть лог", "Проверить обновления", "Открыть интерфейс", "Сменить аккаунт"],
        ["disabled", "danger", "маленькие outline-кнопки"],
        "Может быть заметным, но hover должен оставаться профессиональным.",
    ),
    "settingsButtonText": _token_guide(
        "Текст крупной кнопки Settings",
        ["текст кнопок Settings"],
        ["обычный текст страницы"],
        "Если settingsButtonBackground яркий или светлый, текст должен быть тёмным.",
    ),
    "settingsButtonHoverBackground": _token_guide(
        "Hover фон крупной кнопки Settings",
        ["hover кнопок Settings"],
        ["disabled", "danger"],
        "Подбирай близкий оттенок той же роли; не грязный серый, если текст тёмный.",
    ),
    "settingsButtonHoverText": _token_guide(
        "Hover текст крупной кнопки Settings",
        ["hover-текст кнопок Settings"],
        ["disabled text"],
        "Проверь контраст с settingsButtonHoverBackground.",
    ),
    "settingsButtonBorder": _token_guide(
        "Обводка крупной кнопки Settings",
        ["рамка кнопок Settings"],
        ["панели", "danger"],
        "Обычно близка к settingsButtonBackground.",
    ),
    "settingsButtonHoverBorder": _token_guide(
        "Hover обводка крупной кнопки Settings",
        ["hover-рамка кнопок Settings"],
        ["danger"],
        "Обычно близка к settingsButtonHoverBackground.",
    ),
    "activeSelection": _token_guide(
        "Активный выбранный элемент",
        ["текущий блок", "текущий эпизод", "выбранная вкладка", "segmented control"],
        ["ошибка", "удаление", "успех"],
        "Должен ясно показывать, где пользователь находится сейчас.",
    ),
    "activeSelectionBackground": _token_guide(
        "Фон активного выбранного элемента",
        ["заливка текущего блока", "заливка выбранного эпизода", "активная вкладка"],
        ["обычный hover", "ошибка"],
        "Должен поддерживать activeSelection и не быть слишком плотным.",
    ),
    "activeSelectionText": _token_guide(
        "Текст активного выбранного элемента",
        ["текст 16:9 когда он выбран", "текст активной вкладки", "номер выбранного блока"],
        ["обычный текст", "disabled text"],
        "Проверь контраст: на яркой выбранной пилюле нужен тёмный текст.",
    ),
    "blockPillBackground": _token_guide(
        "Фон обычной пилюли блока",
        ["блок 1/2/3/4 неактивный"],
        ["active block", "References pill"],
        "Обычная пилюля должна быть тихой и не конкурировать с активной.",
    ),
    "blockPillText": _token_guide(
        "Текст обычной пилюли блока",
        ["номер блока в неактивной пилюле"],
        ["active text", "disabled text"],
        "Читаемый, но вторичный.",
    ),
    "blockPillActiveBackground": _token_guide(
        "Фон выбранного блока",
        ["выбранный блок 1/2/3/4"],
        ["unseen/got-ready block", "danger"],
        "Не обязан быть primary; должен ясно показывать текущий блок.",
    ),
    "blockPillActiveText": _token_guide(
        "Текст выбранного блока",
        ["номер выбранного блока"],
        ["disabled text"],
        "Проверь контраст с blockPillActiveBackground.",
    ),
    "blockPillUnseenBackground": _token_guide(
        "Фон готового/непросмотренного блока",
        ["блок с готовыми непросмотренными шотами"],
        ["active block", "danger"],
        "Не оставляй жёлтым, если референс не использует жёлтый для ready state.",
    ),
    "blockPillUnseenText": _token_guide(
        "Текст готового/непросмотренного блока",
        ["номер ready/unseen блока"],
        ["disabled text"],
        "Должен читаться на blockPillUnseenBackground.",
    ),
    "episodeBadgeBackground": _token_guide(
        "Фон бейджа эпизода",
        ["1 эпизод", "СЕРИЯ NN", "episode title badge"],
        ["primary CTA", "danger"],
        "Должен быть частью навигации, не случайно старым жёлтым.",
    ),
    "episodeBadgeBorder": _token_guide(
        "Обводка бейджа эпизода",
        ["рамка 1 эпизод", "рамка СЕРИЯ NN"],
        ["danger border"],
        "Близка к episodeBadgeBackground.",
    ),
    "episodeBadgeText": _token_guide(
        "Текст бейджа эпизода",
        ["текст 1 эпизод", "текст СЕРИЯ NN"],
        ["disabled text"],
        "Проверь контраст с episodeBadgeBackground.",
    ),
    "referenceAccent": _token_guide(
        "Акцент References / готово",
        ["REFERENCES", "готовые блоки", "информационные бейджи", "состояние готово"],
        ["ошибка", "удаление"],
        "Может быть заметным акцентом, но не должен спорить с primary action.",
    ),
    "referenceAccentText": _token_guide(
        "Текст References / готово",
        ["текст на reference-кнопках", "текст выбранной версии", "текст готового блока"],
        ["обычный текст страницы", "danger"],
        "Если referenceAccent светлый или жёлтый, используй тёмный текст.",
    ),
    "referenceAccentBackground": _token_guide(
        "Фон References / готово",
        ["подложка REFERENCES", "подложка готовых блоков", "инфо-бейджи"],
        ["опасные действия"],
        "Полупрозрачная спокойная версия referenceAccent.",
    ),
    "referenceButtonBackground": _token_guide(
        "Фон reference-outline кнопки",
        ["Все референсы", "Текстура", "Сетка на лицо", "Папка с текстурами", "Папка с сетками"],
        ["главная CTA", "нейтральное Переименовать", "Удалить"],
        "Это не полный neon accent, а спокойная подложка для reference-действий.",
    ),
    "referenceButtonBorder": _token_guide(
        "Обводка reference-outline кнопки",
        ["рамка Все референсы", "рамка Текстура", "рамка Сетка на лицо"],
        ["primary action", "danger"],
        "Если referenceAccent яркий lime/yellow, рамка должна быть приглушённой, не кислотной.",
    ),
    "referenceButtonText": _token_guide(
        "Текст reference-outline кнопки",
        ["текст Все референсы", "текст Текстура", "текст Сетка на лицо"],
        ["primary action text", "danger text"],
        "Должен выглядеть как профессиональная secondary/reference кнопка.",
    ),
    "referenceButtonHoverBackground": _token_guide(
        "Hover фон reference-outline кнопки",
        ["hover Все референсы", "hover Текстура", "hover Сетка на лицо"],
        ["danger hover"],
        "Мягко усиливает referenceButtonBackground.",
    ),
    "referenceButtonHoverBorder": _token_guide(
        "Hover обводка reference-outline кнопки",
        ["hover-рамка reference-outline кнопок"],
        ["primary action", "danger"],
        "Не используй чистый neon, если он режет глаз на тёмной панели.",
    ),
    "referenceButtonHoverText": _token_guide(
        "Hover текст reference-outline кнопки",
        ["hover-текст reference-outline кнопок"],
        ["danger text"],
        "Проверь контраст с referenceButtonHoverBackground.",
    ),
    "dropZoneBackground": _token_guide(
        "Фон drag-and-drop зоны",
        ["верхняя drop zone Actors", "drop zone текстур"],
        ["кнопки", "карточки актёров", "ошибки"],
        "Должен быть тихой панелью под стиль сайта, не яркой фиолетовой/синей плитой.",
    ),
    "dropZoneBorder": _token_guide(
        "Обводка drag-and-drop зоны",
        ["пунктирная рамка верхней Actors zone", "пунктирная рамка текстур"],
        ["кнопки", "активная вкладка"],
        "Пунктир должен быть видимым, но спокойным.",
    ),
    "dropZoneText": _token_guide(
        "Текст drag-and-drop зоны",
        ["текст Перетащи фото актёра сюда", "текст зоны текстур"],
        ["кнопки", "заголовки секций"],
        "Читаемый поверх dropZoneBackground.",
    ),
    "dropZoneHoverBackground": _token_guide(
        "Hover фон drag-and-drop зоны",
        ["drop zone когда файл наведён сверху"],
        ["обычное состояние"],
        "Немного активнее dropZoneBackground, но не превращается в кислотную панель.",
    ),
    "dropZoneHoverBorder": _token_guide(
        "Hover обводка drag-and-drop зоны",
        ["пунктирная рамка при drag-over"],
        ["кнопки"],
        "Чуть заметнее dropZoneBorder.",
    ),
    "shotTitleText": _token_guide(
        "Заголовок шота",
        ["SHOT 1/2/3 на карточках", "SHOT N в viewer"],
        ["описание шота", "duration"],
        "Должен быть самым читаемым текстом внутри shot card.",
    ),
    "shotDescriptionText": _token_guide(
        "Описание шота",
        ["текст Макро на ряд бокалов...", "описание под картинкой"],
        ["disabled text"],
        "Обязательно проверь контраст с shotCardBackground.",
    ),
    "shotDurationText": _token_guide(
        "Длительность шота",
        ["3с", "9с", "маленькое время справа"],
        ["заголовок шота"],
        "Может быть тише описания, но не исчезать.",
    ),
    "shotDialogText": _token_guide(
        "Реплика шота",
        ["dialog/реплика под шотом", "реплика в viewer"],
        ["description", "danger"],
        "Может иметь свой мягкий оттенок, но должен быть читаемым.",
    ),
    "seedancePopupBackground": _token_guide(
        "Фон Seedance popup",
        ["окно Промпт Seedance"],
        ["appBackground", "shot card"],
        "Должен подходить теме; в светлой теме не оставляй тёмным островом без причины.",
    ),
    "seedancePromptBackground": _token_guide(
        "Фон prompt viewer",
        ["большое read-only поле промпта Seedance"],
        ["editable input"],
        "Для светлой темы может быть белым/светлым, но текст обязан читаться.",
    ),
    "seedancePromptText": _token_guide(
        "Текст prompt viewer",
        ["текст большого Seedance prompt"],
        ["placeholder", "disabled"],
        "Проверь контраст с seedancePromptBackground.",
    ),
    "seedanceInputBackground": _token_guide(
        "Фон input внутри Seedance",
        ["Что переделать", "target/limit spinbox"],
        ["read-only prompt viewer"],
        "Должен выглядеть как input данной темы.",
    ),
    "seedanceInputText": _token_guide(
        "Текст input внутри Seedance",
        ["текст Что переделать", "числа target/limit"],
        ["placeholder"],
        "Проверь контраст с seedanceInputBackground.",
    ),
    "seedanceTabActiveBackground": _token_guide(
        "Фон активной вкладки Seedance",
        ["Вкладка 1 активная"],
        ["primary CTA"],
        "Selected tab внутри popup, не главная кнопка приложения.",
    ),
    "seedanceTabActiveText": _token_guide(
        "Текст активной вкладки Seedance",
        ["текст Вкладка 1 активная"],
        ["disabled text"],
        "Проверь контраст с seedanceTabActiveBackground.",
    ),
    "proxyInputBackground": _token_guide(
        "Фон proxy input",
        ["IP/Host", "Port", "Login", "Password в секции Прокси-сервер"],
        ["cards", "disabled bars"],
        "В светлой теме это светлое поле ввода, не чёрная полоса.",
    ),
    "proxyInputText": _token_guide(
        "Текст proxy input",
        ["текст IP/Host", "Port", "Login", "Password"],
        ["placeholder", "disabled"],
        "Проверь контраст с proxyInputBackground.",
    ),
    "proxyInputBorder": _token_guide(
        "Обводка proxy input",
        ["рамки proxy fields"],
        ["danger border"],
        "Спокойная input-border роль.",
    ),
    "successAccent": _token_guide(
        "Успешное состояние",
        ["готовая монтажка", "успешные индикаторы", "completed state"],
        ["ошибка", "удаление", "предупреждение"],
        "Должен читаться как успех/готово.",
    ),
    "warningAccent": _token_guide(
        "Предупреждение",
        ["Stop", "warning-состояния", "осторожные статусы"],
        ["успех", "обычная кнопка", "главный CTA"],
        "Должен читаться как внимание, но не как критическая ошибка.",
    ),
    "warningAccentText": _token_guide(
        "Текст предупреждения",
        ["надпись Stop", "текст warning-кнопки", "warning badge"],
        ["обычный текст", "главный CTA"],
        "Обычно тёмный текст на жёлтой/светлой warning-кнопке.",
    ),
    "dangerAccent": _token_guide(
        "Опасное действие",
        ["удаление", "ошибки", "destructive-кнопки", "иконка корзины"],
        ["успех", "обычный hover", "главная позитивная кнопка"],
        "Должен явно ощущаться как опасность. Не делай его зелёным или позитивным.",
    ),
    "dangerAccentText": _token_guide(
        "Текст опасного действия",
        ["надпись Delete", "текст удаления", "иконка корзины на danger-кнопке"],
        ["обычный текст", "success"],
        "Должен хорошо читаться на dangerAccent/dangerBackground.",
    ),
    "dangerBackground": _token_guide(
        "Фон ошибки",
        ["карточки ошибок генерации", "ошибочные состояния", "danger panels"],
        ["обычные карточки", "успешные состояния"],
        "Должен быть читаемым как ошибка, но не слепить.",
    ),
    "generatingGradientStart": _token_guide(
        "Старт градиента генерации",
        ["бегущий градиент блока, где идёт генерация"],
        ["готовый блок", "ошибка"],
        "Обычно прозрачный край анимации.",
    ),
    "generatingGradientMiddle": _token_guide(
        "Яркая часть градиента генерации",
        ["центр бегущего градиента блока, где идёт генерация"],
        ["готовый блок", "ошибка", "обычный hover"],
        "Должен показывать процесс генерации, а не финальный успех.",
    ),
    "generatingGradientEnd": _token_guide(
        "Конец градиента генерации",
        ["бегущий градиент блока, где идёт генерация"],
        ["готовый блок", "ошибка"],
        "Обычно прозрачный край анимации.",
    ),
    "overlayButtonBackground": _token_guide(
        "Общий фон overlay-кнопок",
        ["фон маленьких кнопок поверх изображений"],
        ["цвет конкретного удаления", "play-кнопка"],
        "Базовый нейтральный фон; специфичные действия уточняются отдельными ключами.",
    ),
    "overlayButtonHover": _token_guide(
        "Общий hover overlay-кнопок",
        ["hover маленьких кнопок поверх изображений"],
        ["удаление изображения", "опасные действия"],
        "Для удаления используй imageDeleteButtonHover, а не этот общий ключ.",
    ),
    "imageUtilityButtonBackground": _token_guide(
        "Фон обычной кнопки поверх изображения",
        ["копировать", "избранное", "открыть папку", "edit поверх картинки"],
        ["кнопка удаления", "ошибка"],
        "Нейтральная служебная кнопка. Не должна выглядеть опасной.",
    ),
    "imageUtilityButtonHover": _token_guide(
        "Hover обычной кнопки поверх изображения",
        ["hover копирования", "hover избранного", "hover edit поверх картинки"],
        ["кнопка удаления", "ошибка"],
        "Может брать акцент темы, но не должен выглядеть destructive.",
    ),
    "imageDeleteButtonBackground": _token_guide(
        "Фон кнопки удаления изображения",
        ["кнопка с корзиной поверх картинки", "удаление изображения с холста Generator"],
        ["лайк", "копировать", "edit", "успех"],
        "Должна выглядеть как destructive-действие. Не делай её зелёной или позитивной.",
    ),
    "imageDeleteButtonHover": _token_guide(
        "Hover кнопки удаления изображения",
        ["наведение на корзину поверх картинки", "удаление изображения"],
        ["обычный hover", "успех", "главная CTA-кнопка"],
        "Самый явный цвет удаления. Должен оставаться опасным даже в светлой/неоновой теме.",
    ),
    "playOverlayBackground": _token_guide(
        "Фон play поверх видео",
        ["круглая кнопка play на видео-карточке"],
        ["удаление", "копирование", "ошибка"],
        "Должен хорошо читаться поверх видео, обычно полупрозрачный тёмный.",
    ),
    "modelBadgeBackground": _token_guide(
        "Фон бейджа модели",
        ["OpenAI", "Nano Banana", "Veo", "маленькая метка модели на Generator"],
        ["главные кнопки", "ошибки"],
        "Бейдж должен быть читаемым, но не важнее изображения.",
    ),
    "scrollbarTrack": _token_guide(
        "Трек скроллбара",
        ["фон вертикального скроллбара", "фон горизонтального скроллбара"],
        ["ползунок скроллбара"],
        "Обычно почти невидимый.",
    ),
    "scrollbarThumb": _token_guide(
        "Ползунок скроллбара",
        ["видимая часть скроллбара", "drag thumb"],
        ["фон скроллбара"],
        "Должен быть видимым, но не ярким.",
    ),
    "faceGridSelection": _token_guide(
        "Выделение face grid",
        ["рамки face grid", "ручки resize", "selection-инструменты"],
        ["обычные карточки", "текст"],
        "Должен быть хорошо виден поверх изображения.",
    ),
}


def _build_documented_llm_template() -> Dict[str, object]:
    template = copy.deepcopy(THEME_LLM_TEMPLATE)
    for key, token in template["colors"].items():
        guide = THEME_LLM_TOKEN_GUIDE.get(key)
        if guide:
            # label ставим первым после color при сериализации insertion-order.
            color = token.pop("color")
            description = token.pop("description")
            token["label"] = guide["label"]
            token["color"] = color
            token["description"] = description
            token["usedFor"] = guide["usedFor"]
            token["doNotUseFor"] = guide["doNotUseFor"]
            token["designRule"] = guide["designRule"]
    return template


THEME_LLM_PALETTE_TEMPLATE = {
    "schemaVersion": 2,
    "mode": "professionalPalette",
    "themeName": "Storyboard Studio Custom Theme",
    "instructions": [
        "Return only this JSON object with changed palette values.",
        "Do not create a full 100-token theme. Storyboard Studio compiles this palette into safe UI tokens.",
        "Do not use large website side gutters or ad backgrounds as the app background.",
        "Use brand/saturated colors as accents, not as full-screen app backgrounds.",
        "Copy core UI surfaces directly: appBase, surface, panelSurface, cardSurface, popupSurface and inputSurface should be sampled from the real interface areas, not guessed.",
        "For light references, keep the app mostly neutral-light with readable dark text.",
        "For dark references, keep the app mostly neutral-dark with readable light text.",
        "Buttons, hover states, disabled controls and borders are generated by Studio from this palette.",
    ],
    "palette": {
        "appearance": "auto",
        "appBase": "#0f1014",
        "surface": "#181a20",
        "surfaceAlt": "#22252d",
        "panelSurface": "#181a20",
        "cardSurface": "#181a20",
        "popupSurface": "#22252d",
        "inputSurface": "#15171a",
        "text": "#f5f6f8",
        "mutedText": "#8d939d",
        "brand": "#e4344a",
        "accent": "#d4a256",
        "danger": "#e4344a",
    },
}


def _build_palette_llm_template() -> Dict[str, object]:
    return copy.deepcopy(THEME_LLM_PALETTE_TEMPLATE)


def build_llm_theme_prompt() -> str:
    """Готовый текст для кнопки Settings → «Скопировать шаблон темы».

    Пользователь вставляет этот текст в LLM и прикладывает скриншот/ссылку.
    По умолчанию просим не полную тему, а короткую палитру: Studio сама
    компилирует её в безопасные UI-токены с проверкой контраста.
    """
    payload = json.dumps(
        _build_palette_llm_template(),
        ensure_ascii=False,
        indent=2,
    )
    return (
        "Ты senior product/UI designer и theme engineer. Твоя задача — извлечь "
        "из приложенного скриншота/сайта короткую профессиональную палитру для "
        "Storyboard Studio.\n\n"
        "ВАЖНО ДЛЯ АССИСТЕНТА:\n"
        "Эта инструкция уже содержит все правила. Не проси пользователя объяснять, "
        "какие кнопки/попапы/инпуты красить. Ты НЕ создаёшь полный theme JSON на "
        "100+ токенов. Ты возвращаешь только compact professionalPalette JSON ниже, "
        "а Storyboard Studio сама разложит палитру по кнопкам, карточкам, попапам, "
        "полям ввода, Seedance, Actors, Settings и disabled/hover states.\n\n"
        "Источник стиля:\n"
        "1. Если приложены скриншоты, они главный источник цветов. Ссылка или текст "
        "слабее скриншотов.\n"
        "2. Отдели настоящий интерфейс от рекламы, пустых ad-блоков, белых дыр, "
        "браузерного chrome и боковых цветных полей сайта.\n"
        "3. Не добавляй случайные цвета, которых нет в референсе.\n"
        "3. Если нет скриншота, изображения, ссылки или понятного описания палитры, "
        "не выдумывай тему. Ответь коротко: "
        "\"Пришли скриншот, ссылку или описание палитры, откуда брать стиль\".\n\n"
        "Как заполнять professionalPalette:\n"
        "- schemaVersion оставь 2, mode оставь professionalPalette;\n"
        "- themeName сделай коротким понятным названием;\n"
        "- appearance: \"light\", \"dark\" или \"auto\". Если сомневаешься, выбери по основной рабочей области референса;\n"
        "- appBase: спокойный общий фон приложения. Не используй яркие боковые поля сайта как appBase;\n"
        "- surface: базовая нейтральная поверхность интерфейса;\n"
        "- surfaceAlt: чуть отличающаяся поверхность для hover/disabled/secondary areas;\n"
        "- panelSurface: точный цвет крупных панелей/шапки/настроек из референса;\n"
        "- cardSurface: точный цвет карточек/shots/actors/reference cards из референса;\n"
        "- popupSurface: точный цвет попапов/диалогов из референса;\n"
        "- inputSurface: точный цвет полей ввода/textarea/proxy/API fields из референса;\n"
        "- text: основной читаемый текст;\n"
        "- mutedText: вторичный текст, но не настолько бледный, чтобы описания шотов пропадали;\n"
        "- brand: главный брендовый цвет референса. Это акцент, не обязательно фон приложения;\n"
        "- accent: вторичный акцент для navigation/reference/workflow controls;\n"
        "- danger: цвет удаления/ошибки, обычно красный/бордовый;\n"
        "- используй только HEX #RRGGBB для palette values;\n"
        "- верни только JSON: без markdown, без ```json, без комментариев и без пояснений.\n\n"
        "Финальная самопроверка перед ответом:\n"
        "- если референс светлый, appBase/surface должны быть светлыми нейтральными, text тёмным;\n"
        "- если референс тёмный, appBase/surface должны быть тёмными нейтральными, text светлым;\n"
        "- appBase, panelSurface и cardSurface — самые важные цвета. Скопируй их максимально близко к скриншоту;\n"
        "- яркий brand/accent не должен становиться полноэкранным фоном;\n"
        "- JSON валиден и содержит все ключи palette.\n\n"
        "Ниже шаблон professionalPalette. Заполни его по источнику стиля:\n"
        f"{payload}"
    )


# ── Built-in Interface Theme ────────────────────────────────────────────
# 2026-06-27 / Codex:
# Пользовательский JSON-конструктор больше не является основным путём темы:
# встроенная Higgsfield-like палитра применяется всем пользователям при старте.
# Старый Theme Manager оставлен в коде для совместимости/отката, но runtime
# ниже больше не читает active theme из QSettings.

HIGGSFIELD_THEME_NAME = "Higgsfield Graphite"

HIGGSFIELD_THEME_COLORS: Dict[str, str] = {
    "appBackground": "#121313",
    "appBackgroundGlow": "rgba(32, 40, 38, 0.00)",
    "topHeaderSurface": "#121313",
    "mainPanelSurface": "#121313",
    "subtleSurface": "rgba(255,255,255,0.045)",
    "hoverSurface": "rgba(255,255,255,0.075)",
    "shotCardBackground": "#191b1d",
    "shotImageEmptyBackground": "#131516",
    "generatorCellBackground": "#191b1d",
    "referenceCardBackground": "#191b1d",
    "actorCardBackground": "#191b1d",
    "monsterCardBackground": "#1b1813",
    "popupBackground": "#1b1d20",
    "inputBackground": "#121416",
    "defaultBorder": "rgba(255,255,255,0.08)",
    "strongBorder": "rgba(255,255,255,0.14)",
    "subtleDivider": "rgba(255,255,255,0.05)",
    "textPrimary": "#f2f4ef",
    "textSecondary": "rgba(242,244,239,0.68)",
    "textMuted": "rgba(242,244,239,0.44)",
    "disabledText": "rgba(242,244,239,0.30)",
    "disabledButtonBackground": "rgba(255,255,255,0.055)",
    "primaryActionButtonText": "#101208",
    "primaryActionButton": "#c7f04a",
    "primaryActionButtonHover": "#d6ff5f",
    "topNavAccentText": "#d7ded4",
    "topNavActiveBackground": "rgba(255,255,255,0.08)",
    "topNavActiveText": "#f6f7f2",
    "workflowButtonBackground": "rgba(255,255,255,0.055)",
    "workflowButtonBorder": "rgba(255,255,255,0.10)",
    "workflowButtonText": "#d6ded2",
    "workflowButtonHoverBackground": "rgba(255,255,255,0.09)",
    "workflowButtonHoverText": "#f5f7f1",
    "secondaryButtonBackground": "rgba(255,255,255,0.06)",
    "secondaryButtonText": "#edf0ea",
    "secondaryButtonHover": "rgba(255,255,255,0.10)",
    "secondaryButtonBorder": "rgba(255,255,255,0.11)",
    "outlineButtonBackground": "rgba(0,0,0,0)",
    "outlineButtonBorder": "rgba(255,255,255,0.12)",
    "outlineButtonText": "#d7ded4",
    "outlineButtonHoverBackground": "rgba(255,255,255,0.075)",
    "outlineButtonHoverBorder": "rgba(255,255,255,0.20)",
    "outlineButtonHoverText": "#f7f8f4",
    "settingsButtonBackground": "#222528",
    "settingsButtonText": "#f2f4ef",
    "settingsButtonHoverBackground": "#2a2e31",
    "settingsButtonHoverText": "#ffffff",
    "settingsButtonBorder": "rgba(255,255,255,0.12)",
    "settingsButtonHoverBorder": "rgba(255,255,255,0.20)",
    "activeSelection": "#303438",
    "activeSelectionBackground": "rgba(255,255,255,0.09)",
    "activeSelectionText": "#f5f7f1",
    "blockPillBackground": "rgba(255,255,255,0.045)",
    "blockPillText": "rgba(242,244,239,0.62)",
    "blockPillActiveBackground": "rgba(199,240,74,0.14)",
    "blockPillActiveText": "#efffd0",
    "blockPillUnseenBackground": "rgba(199,240,74,0.10)",
    "blockPillUnseenText": "#d9f68a",
    "episodeBadgeBackground": "rgba(255,255,255,0.06)",
    "episodeBadgeBorder": "rgba(255,255,255,0.12)",
    "episodeBadgeText": "#d9e2d5",
    "referenceAccent": "#aeb8a7",
    "referenceAccentText": "#101208",
    "referenceAccentBackground": "rgba(174,184,167,0.10)",
    "referenceButtonBackground": "rgba(174,184,167,0.07)",
    "referenceButtonBorder": "rgba(174,184,167,0.24)",
    "referenceButtonText": "#d9e2d4",
    "referenceButtonHoverBackground": "rgba(174,184,167,0.13)",
    "referenceButtonHoverBorder": "rgba(174,184,167,0.38)",
    "referenceButtonHoverText": "#f4f7ef",
    "dropZoneBackground": "rgba(255,255,255,0.045)",
    "dropZoneBorder": "rgba(199,240,74,0.26)",
    "dropZoneText": "#d9e2d4",
    "dropZoneHoverBackground": "rgba(199,240,74,0.10)",
    "dropZoneHoverBorder": "rgba(199,240,74,0.46)",
    "shotTitleText": "#f6f7f2",
    "shotDescriptionText": "rgba(242,244,239,0.64)",
    "shotDurationText": "rgba(242,244,239,0.40)",
    "shotDialogText": "#cdd8c6",
    "seedancePopupBackground": "#1b1d20",
    "seedancePromptBackground": "#121416",
    "seedancePromptText": "#e8ece5",
    "seedanceInputBackground": "#141619",
    "seedanceInputText": "#e8ece5",
    "seedanceTabActiveBackground": "rgba(199,240,74,0.14)",
    "seedanceTabActiveText": "#efffd0",
    "proxyInputBackground": "#141619",
    "proxyInputText": "#e8ece5",
    "proxyInputBorder": "rgba(255,255,255,0.12)",
    "successAccent": "#8ccf62",
    "warningAccent": "#d9b75c",
    "warningAccentText": "#111315",
    "dangerAccent": "#e35d5d",
    "dangerAccentText": "#fff7f7",
    "dangerBackground": "#2a1818",
    "generatingGradientStart": "rgba(199,240,74,0)",
    "generatingGradientMiddle": "rgba(199,240,74,0.24)",
    "generatingGradientEnd": "rgba(199,240,74,0)",
    "overlayButtonBackground": "rgba(0,0,0,0.58)",
    "overlayButtonHover": "rgba(255,255,255,0.18)",
    "imageUtilityButtonBackground": "rgba(0,0,0,0.58)",
    "imageUtilityButtonHover": "rgba(255,255,255,0.18)",
    "imageDeleteButtonBackground": "rgba(227,93,93,0.34)",
    "imageDeleteButtonHover": "rgba(227,93,93,0.72)",
    "playOverlayBackground": "rgba(0,0,0,0.46)",
    "modelBadgeBackground": "rgba(0,0,0,0.58)",
    "scrollbarTrack": "rgba(255,255,255,0.035)",
    "scrollbarThumb": "rgba(255,255,255,0.22)",
    "faceGridSelection": "#c7f04a",
}


def _build_higgsfield_theme_payload() -> Dict[str, Any]:
    payload = copy.deepcopy(THEME_LLM_TEMPLATE)
    payload["themeName"] = HIGGSFIELD_THEME_NAME
    colors = payload.get("colors", {})
    if isinstance(colors, dict):
        for key, value in HIGGSFIELD_THEME_COLORS.items():
            token = colors.get(key)
            if isinstance(token, dict):
                token["color"] = value
    return payload


# ── Saved Interface Themes ──────────────────────────────────────────────
# 2026-06-26 / Codex:
# Менеджер тем в Settings хранит пользовательские JSON-темы в QSettings.
# Это не меняет внешний вид само по себе: активная тема превращается в
# THEME_COLOR_OVERRIDES при старте приложения.

THEME_STORE_SETTINGS_KEY = "interface_theme/store_json"


def _default_theme_store() -> Dict[str, Any]:
    return {
        "active": "",
        "themes": [],
    }


def load_theme_store(settings: Optional[QSettings] = None) -> Dict[str, Any]:
    """Читает сохранённые темы интерфейса из QSettings."""
    settings = settings or QSettings()
    raw = settings.value(THEME_STORE_SETTINGS_KEY, "", type=str)
    if not raw:
        return _default_theme_store()
    try:
        data = json.loads(raw)
    except Exception:
        return _default_theme_store()
    if not isinstance(data, dict):
        return _default_theme_store()
    active = data.get("active", "")
    themes = data.get("themes", [])
    if not isinstance(active, str):
        active = ""
    if not isinstance(themes, list):
        themes = []
    clean_themes: List[Dict[str, Any]] = []
    for item in themes:
        if isinstance(item, dict) and isinstance(item.get("payload"), dict):
            payload = item["payload"]
            try:
                payload = normalize_theme_payload(payload)
            except Exception:
                pass
            clean_themes.append({
                "name": str(item.get("name") or payload.get("themeName") or "Theme"),
                "payload": payload,
            })
    return {"active": active, "themes": clean_themes}


def save_theme_store(store: Dict[str, Any], settings: Optional[QSettings] = None) -> None:
    """Сохраняет темы интерфейса в QSettings."""
    settings = settings or QSettings()
    settings.setValue(
        THEME_STORE_SETTINGS_KEY,
        json.dumps(store, ensure_ascii=False, indent=2),
    )
    settings.sync()


def _is_valid_theme_color(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
        return True
    if re.fullmatch(
        r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}"
        r"(?:\s*,\s*(?:0|1|0?\.\d+|\d{1,3}))?\s*\)",
        value,
    ):
        return True
    return False


def _theme_rgb_tuple(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
        return (
            int(raw[1:3], 16),
            int(raw[3:5], 16),
            int(raw[5:7], 16),
        )
    m = re.fullmatch(
        r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})"
        r"(?:\s*,\s*(?:0|1|0?\.\d+|\d{1,3}))?\s*\)",
        raw,
    )
    if not m:
        return None
    return tuple(max(0, min(255, int(m.group(i)))) for i in range(1, 4))  # type: ignore[return-value]


def _theme_luminance(value: Any) -> Optional[float]:
    rgb = _theme_rgb_tuple(value)
    if rgb is None:
        return None
    vals = []
    for channel in rgb:
        c = channel / 255.0
        vals.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _theme_hex(rgb: Tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(v))) for v in rgb])


def _mix_rgb(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] * (1.0 - t) + b[i] * t) for i in range(3))  # type: ignore[return-value]


def _mix_color(a: str, b: str, t: float) -> str:
    ar = _theme_rgb_tuple(a) or (0, 0, 0)
    br = _theme_rgb_tuple(b) or (255, 255, 255)
    return _theme_hex(_mix_rgb(ar, br, t))


def _relative_saturation(rgb: Tuple[int, int, int]) -> float:
    high = max(rgb)
    low = min(rgb)
    if high <= 0:
        return 0.0
    return (high - low) / high


def _readable_solid_text(bg: str) -> str:
    lum = _theme_luminance(bg)
    if lum is not None and lum >= 0.43:
        return "#111316"
    return "#f7f8fb"


def _professional_theme_base_color(value: str, appearance: str) -> str:
    """Keeps dangerous saturated site colors out of the full-app background."""
    rgb = _theme_rgb_tuple(value)
    lum = _theme_luminance(value)
    if rgb is None or lum is None:
        return "#f4f6f8" if appearance == "light" else "#0b0d10"
    if _relative_saturation(rgb) >= 0.45:
        return "#f4f6f8" if appearance == "light" else "#101214"
    return value


def _is_bright_saturated(value: Any) -> bool:
    rgb = _theme_rgb_tuple(value)
    lum = _theme_luminance(value)
    if rgb is None or lum is None:
        return False
    return lum >= 0.48 and _relative_saturation(rgb) >= 0.45


def _calm_accent_for_ui(accent: str, surface: str, text: str, *, is_light: bool) -> str:
    """Tames neon/lime accents for labels, borders and non-CTA controls."""
    if _is_bright_saturated(accent):
        return _mix_color(accent, "#000000" if is_light else text, 0.18)
    return accent


def _is_palette_theme_payload(payload: Dict[str, Any]) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schemaVersion") == 2
        and payload.get("mode") == "professionalPalette"
        and isinstance(payload.get("palette"), dict)
    )


def validate_palette_theme_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверяет короткую professionalPalette-тему из ChatGPT/Claude."""
    if not _is_palette_theme_payload(payload):
        return False, "professionalPalette должна иметь schemaVersion 2, mode professionalPalette и объект palette."
    if not isinstance(payload.get("themeName"), str) or not payload["themeName"].strip():
        return False, "В палитре должен быть непустой themeName."

    palette = payload.get("palette", {})
    required = {
        "appearance",
        "appBase",
        "surface",
        "surfaceAlt",
        "text",
        "mutedText",
        "brand",
        "accent",
        "danger",
    }
    missing = sorted(required - set(palette.keys()))
    if missing:
        return False, "В professionalPalette не хватает ключей: " + ", ".join(missing)
    appearance = palette.get("appearance")
    if appearance not in {"auto", "light", "dark"}:
        return False, "palette.appearance должен быть auto, light или dark."
    for key in sorted(required - {"appearance"}):
        color = palette.get(key)
        if not _is_valid_theme_color(color):
            return False, f"У palette.{key} некорректный цвет: {color!r}."
    optional_colors = {
        "panelSurface",
        "cardSurface",
        "popupSurface",
        "inputSurface",
    }
    for key in sorted(optional_colors & set(palette.keys())):
        color = palette.get(key)
        if not _is_valid_theme_color(color):
            return False, f"У palette.{key} некорректный цвет: {color!r}."
    return True, ""


def compile_palette_theme_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Компилирует короткую professionalPalette в полный schemaVersion=1 JSON.

    LLM выбирает только базовые цвета, а Studio сама создаёт спокойные роли
    для карточек, кнопок, попапов, Seedance, proxy и disabled states.
    """
    ok, err = validate_palette_theme_payload(payload)
    if not ok:
        raise ValueError(err)

    palette = payload["palette"]
    appearance = str(palette.get("appearance") or "auto")
    base_input = str(palette["appBase"]).strip()
    base_lum = _theme_luminance(base_input)
    if appearance == "auto":
        appearance = "light" if base_lum is not None and base_lum >= 0.50 else "dark"

    is_light = appearance == "light"
    app_base = _professional_theme_base_color(base_input, appearance)
    surface = str(palette["surface"]).strip()
    surface_alt = str(palette["surfaceAlt"]).strip()
    panel_surface = str(palette.get("panelSurface") or surface).strip()
    card_surface = str(palette.get("cardSurface") or surface).strip()
    popup_surface = str(palette.get("popupSurface") or surface_alt).strip()
    input_surface = str(palette.get("inputSurface") or surface_alt).strip()
    text = str(palette["text"]).strip()
    muted = str(palette["mutedText"]).strip()
    brand = str(palette["brand"]).strip()
    accent = str(palette["accent"]).strip()
    danger = str(palette["danger"]).strip()

    neutral_dark = "#111316"
    neutral_light = "#f7f8fb"
    if _theme_luminance(surface) is None:
        surface = "#ffffff" if is_light else "#171a20"
    if _theme_luminance(surface_alt) is None:
        surface_alt = _mix_color(surface, neutral_dark if is_light else neutral_light, 0.08)
    if _theme_luminance(panel_surface) is None:
        panel_surface = surface
    if _theme_luminance(card_surface) is None:
        card_surface = surface
    if _theme_luminance(popup_surface) is None:
        popup_surface = surface_alt
    if _theme_luminance(input_surface) is None:
        input_surface = surface_alt

    text_on_surface = _readable_solid_text(surface)
    if _theme_luminance(text) is None:
        text = text_on_surface
    muted_guard = muted
    if _theme_luminance(muted_guard) is None:
        muted_guard = _mix_color(text_on_surface, surface, 0.45)

    primary_text = _readable_solid_text(brand)
    accent_text = _readable_solid_text(accent)
    danger_text = _readable_solid_text(danger)
    border_mix = "#000000" if is_light else "#ffffff"
    default_border = _mix_color(surface, border_mix, 0.12 if is_light else 0.18)
    strong_border = _mix_color(surface, border_mix, 0.22 if is_light else 0.28)
    subtle_border = _mix_color(surface, border_mix, 0.07 if is_light else 0.10)

    calm_brand = _calm_accent_for_ui(brand, surface, text, is_light=is_light)
    calm_accent = _calm_accent_for_ui(accent, surface, text, is_light=is_light)
    active_control = (
        _mix_color(surface, calm_brand, 0.34)
        if _is_bright_saturated(brand)
        else calm_brand
    )

    page_panel = panel_surface
    popup = popup_surface
    input_bg = input_surface
    card_bg = card_surface
    empty_bg = card_surface
    actor_bg = card_surface
    monster_bg = _mix_color(surface, accent, 0.09)
    hover_bg = _mix_color(surface, brand, 0.06)
    top_nav_accent_text = text if _is_bright_saturated(brand) else calm_brand

    template = _build_documented_llm_template()
    full = copy.deepcopy(template)
    full["themeName"] = str(payload.get("themeName") or "Custom Theme").strip()
    full["compiledFrom"] = "professionalPalette"
    if isinstance(payload.get("author"), str):
        full["author"] = payload["author"]
    if isinstance(payload.get("notes"), str):
        full["notes"] = payload["notes"]

    assignments = {
        "appBackground": app_base,
        "appBackgroundGlow": (
            f"rgba({(_theme_rgb_tuple(brand) or (120,120,120))[0]}, "
            f"{(_theme_rgb_tuple(brand) or (120,120,120))[1]}, "
            f"{(_theme_rgb_tuple(brand) or (120,120,120))[2]}, "
            f"{'0.16' if is_light else '0.32'})"
        ),
        "topHeaderSurface": page_panel,
        "mainPanelSurface": page_panel,
        "subtleSurface": surface_alt,
        "hoverSurface": hover_bg,
        "shotCardBackground": card_bg,
        "shotImageEmptyBackground": empty_bg,
        "generatorCellBackground": card_bg,
        "referenceCardBackground": card_bg,
        "actorCardBackground": actor_bg,
        "monsterCardBackground": monster_bg,
        "popupBackground": popup,
        "inputBackground": input_bg,
        "defaultBorder": default_border,
        "strongBorder": strong_border,
        "subtleDivider": subtle_border,
        "textPrimary": text,
        "textSecondary": muted_guard,
        "textMuted": _mix_color(muted_guard, surface, 0.25),
        "disabledText": _mix_color(muted_guard, surface, 0.30),
        "disabledButtonBackground": _mix_color(surface, "#808080", 0.12),
        "primaryActionButtonText": primary_text,
        "primaryActionButton": brand,
        "primaryActionButtonHover": _mix_color(brand, "#000000" if is_light else "#ffffff", 0.12),
        "topNavAccentText": top_nav_accent_text,
        "topNavActiveBackground": surface_alt,
        "topNavActiveText": text,
        "workflowButtonBackground": _mix_color(surface, calm_accent, 0.055),
        "workflowButtonBorder": _mix_color(default_border, calm_accent, 0.22),
        "workflowButtonText": calm_accent,
        "workflowButtonHoverBackground": _mix_color(surface, calm_accent, 0.10),
        "workflowButtonHoverText": calm_accent,
        "secondaryButtonBackground": surface_alt,
        "secondaryButtonText": text,
        "secondaryButtonHover": _mix_color(surface_alt, brand, 0.07),
        "secondaryButtonBorder": default_border,
        "outlineButtonBackground": "rgba(0,0,0,0)",
        "outlineButtonBorder": strong_border,
        "outlineButtonText": text,
        "outlineButtonHoverBackground": hover_bg,
        "outlineButtonHoverBorder": _mix_color(strong_border, brand, 0.30),
        "outlineButtonHoverText": text,
        "settingsButtonBackground": _mix_color(surface, "#ffffff" if is_light else "#000000", 0.10),
        "settingsButtonText": text,
        "settingsButtonHoverBackground": _mix_color(surface, calm_brand, 0.06),
        "settingsButtonHoverText": text,
        "settingsButtonBorder": default_border,
        "settingsButtonHoverBorder": _mix_color(default_border, calm_brand, 0.16),
        "activeSelection": active_control,
        "activeSelectionBackground": _mix_color(surface, calm_brand, 0.14),
        "activeSelectionText": text,
        "blockPillBackground": surface_alt,
        "blockPillText": muted_guard,
        "blockPillActiveBackground": _mix_color(surface, calm_brand, 0.14),
        "blockPillActiveText": text,
        "blockPillUnseenBackground": _mix_color(surface, calm_accent, 0.09),
        "blockPillUnseenText": calm_accent,
        "episodeBadgeBackground": _mix_color(surface, calm_accent, 0.09),
        "episodeBadgeBorder": _mix_color(default_border, calm_accent, 0.24),
        "episodeBadgeText": calm_accent,
        "referenceAccent": accent,
        "referenceAccentText": accent_text,
        "referenceAccentBackground": _mix_color(surface, calm_accent, 0.10),
        "referenceButtonBackground": _mix_color(surface, calm_accent, 0.045),
        "referenceButtonBorder": _mix_color(default_border, calm_accent, 0.22),
        "referenceButtonText": calm_accent,
        "referenceButtonHoverBackground": _mix_color(surface, calm_accent, 0.09),
        "referenceButtonHoverBorder": _mix_color(default_border, calm_accent, 0.34),
        "referenceButtonHoverText": calm_accent,
        "dropZoneBackground": _mix_color(surface, calm_brand, 0.035),
        "dropZoneBorder": _mix_color(default_border, calm_brand, 0.22),
        "dropZoneText": text,
        "dropZoneHoverBackground": _mix_color(surface, calm_brand, 0.075),
        "dropZoneHoverBorder": _mix_color(default_border, calm_brand, 0.34),
        "shotTitleText": text,
        "shotDescriptionText": muted_guard,
        "shotDurationText": _mix_color(muted_guard, surface, 0.18),
        "shotDialogText": _mix_color(text, accent, 0.28),
        "seedancePopupBackground": popup,
        "seedancePromptBackground": input_bg,
        "seedancePromptText": text,
        "seedanceInputBackground": input_bg,
        "seedanceInputText": text,
        "seedanceTabActiveBackground": _mix_color(surface, calm_brand, 0.11),
        "seedanceTabActiveText": text,
        "proxyInputBackground": input_bg,
        "proxyInputText": text,
        "proxyInputBorder": default_border,
        "successAccent": "#2fb66d" if is_light else "#43d17c",
        "warningAccent": accent,
        "warningAccentText": accent_text,
        "dangerAccent": danger,
        "dangerAccentText": danger_text,
        "dangerBackground": _mix_color(surface, danger, 0.14),
        "generatingGradientStart": "rgba(0,0,0,0)",
        "generatingGradientMiddle": _mix_color(surface, accent, 0.24),
        "generatingGradientEnd": "rgba(0,0,0,0)",
        "overlayButtonBackground": "rgba(0,0,0,0.56)" if is_light else "rgba(0,0,0,0.62)",
        "overlayButtonHover": _mix_color(danger, "#000000", 0.08),
        "imageUtilityButtonBackground": "rgba(0,0,0,0.52)",
        "imageUtilityButtonHover": "rgba(255,255,255,0.18)",
        "imageDeleteButtonBackground": _mix_color(danger, "#000000", 0.18),
        "imageDeleteButtonHover": danger,
        "playOverlayBackground": "rgba(0,0,0,0.46)",
        "modelBadgeBackground": "rgba(0,0,0,0.52)",
        "scrollbarTrack": _mix_color(surface, "#000000" if is_light else "#ffffff", 0.06),
        "scrollbarThumb": _mix_color(surface, "#000000" if is_light else "#ffffff", 0.22),
        "faceGridSelection": brand,
    }

    for key, color in assignments.items():
        if key in full["colors"]:
            full["colors"][key]["color"] = color
    return full


def _repair_compiled_professional_payload(payload: Dict[str, Any]) -> None:
    """Soft-migrates already saved professionalPalette themes.

    Early compiler versions spread bright brand/accent colors too widely. Saved
    themes are schemaVersion=1, so they need a lightweight repair path instead
    of asking the user to reimport the palette.
    """
    if payload.get("compiledFrom") != "professionalPalette":
        return
    colors = payload.get("colors")
    if not isinstance(colors, dict):
        return

    def get(key: str) -> Optional[str]:
        token = colors.get(key)
        if isinstance(token, dict) and isinstance(token.get("color"), str):
            return token["color"]
        return None

    def setc(key: str, value: Optional[str]) -> None:
        if value and key in colors and isinstance(colors[key], dict):
            colors[key]["color"] = value

    app = get("appBackground") or "#0b0d10"
    surface = get("mainPanelSurface") or get("topHeaderSurface") or "#181a20"
    surface_alt = get("subtleSurface") or "#22252d"
    text = get("textPrimary") or _readable_solid_text(surface)
    brand = get("primaryActionButton") or "#d8ff00"
    accent = get("referenceAccent") or get("workflowButtonText") or brand
    default_border = get("defaultBorder") or _mix_color(surface, "#ffffff", 0.16)
    is_light = (_theme_luminance(app) or 0.0) >= 0.50
    calm_brand = _calm_accent_for_ui(brand, surface, text, is_light=is_light)
    calm_accent = _calm_accent_for_ui(accent, surface, text, is_light=is_light)
    active_control = (
        _mix_color(surface, calm_brand, 0.34)
        if _is_bright_saturated(brand)
        else calm_brand
    )
    card_bg = get("shotCardBackground") or surface
    if _theme_luminance(card_bg) is None:
        card_bg = surface
    empty_bg = get("shotImageEmptyBackground") or card_bg
    if _theme_luminance(empty_bg) is None:
        empty_bg = card_bg
    popup_bg = get("popupBackground") or surface_alt
    if _theme_luminance(popup_bg) is None:
        popup_bg = surface_alt
    top_nav_accent_text = text if _is_bright_saturated(brand) else calm_brand

    # Large/structural surfaces should stay neutral. Bright accents belong to
    # CTA buttons and tiny state signals, not entire dialogs or Panels.
    setc("shotCardBackground", card_bg)
    setc("shotImageEmptyBackground", empty_bg)
    setc("generatorCellBackground", card_bg)
    setc("referenceCardBackground", card_bg)
    setc("actorCardBackground", card_bg)
    setc("popupBackground", popup_bg)
    setc("topNavAccentText", top_nav_accent_text)
    setc("topNavActiveBackground", surface_alt)
    setc("topNavActiveText", text)
    setc("workflowButtonBackground", _mix_color(surface, calm_accent, 0.055))
    setc("workflowButtonBorder", _mix_color(default_border, calm_accent, 0.22))
    setc("workflowButtonText", calm_accent)
    setc("workflowButtonHoverBackground", _mix_color(surface, calm_accent, 0.10))
    setc("workflowButtonHoverText", calm_accent)
    setc("settingsButtonHoverBackground", _mix_color(surface, calm_brand, 0.06))
    setc("settingsButtonHoverBorder", _mix_color(default_border, calm_brand, 0.16))
    setc("activeSelection", active_control)
    setc("activeSelectionBackground", _mix_color(surface, calm_brand, 0.14))
    setc("activeSelectionText", text)
    setc("blockPillActiveBackground", _mix_color(surface, calm_brand, 0.14))
    setc("blockPillActiveText", text)
    setc("blockPillUnseenBackground", _mix_color(surface, calm_accent, 0.09))
    setc("blockPillUnseenText", calm_accent)
    setc("episodeBadgeBackground", _mix_color(surface, calm_accent, 0.09))
    setc("episodeBadgeBorder", _mix_color(default_border, calm_accent, 0.24))
    setc("episodeBadgeText", calm_accent)
    setc("referenceAccentBackground", _mix_color(surface, calm_accent, 0.10))
    setc("referenceButtonBackground", _mix_color(surface, calm_accent, 0.045))
    setc("referenceButtonBorder", _mix_color(default_border, calm_accent, 0.22))
    setc("referenceButtonText", calm_accent)
    setc("referenceButtonHoverBackground", _mix_color(surface, calm_accent, 0.09))
    setc("referenceButtonHoverBorder", _mix_color(default_border, calm_accent, 0.34))
    setc("referenceButtonHoverText", calm_accent)
    setc("dropZoneBackground", _mix_color(surface, calm_brand, 0.035))
    setc("dropZoneBorder", _mix_color(default_border, calm_brand, 0.22))
    setc("dropZoneHoverBackground", _mix_color(surface, calm_brand, 0.075))
    setc("dropZoneHoverBorder", _mix_color(default_border, calm_brand, 0.34))
    setc("seedanceTabActiveBackground", _mix_color(surface, calm_brand, 0.11))


def validate_theme_payload(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Проверяет, что вставленный JSON соответствует теме Storyboard Studio."""
    if not isinstance(payload, dict):
        return False, "Тема должна быть JSON-объектом."
    if _is_palette_theme_payload(payload):
        return validate_palette_theme_payload(payload)
    if payload.get("schemaVersion") != 1:
        return False, "schemaVersion должен быть равен 1 или 2 professionalPalette."
    if not isinstance(payload.get("themeName"), str) or not payload["themeName"].strip():
        return False, "В теме должен быть непустой themeName."
    colors = payload.get("colors")
    if not isinstance(colors, dict):
        return False, "В теме должен быть объект colors."

    expected = set(THEME_LLM_TEMPLATE["colors"].keys())
    actual = set(colors.keys())
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        return False, "В теме не хватает ключей: " + ", ".join(missing[:8])
    if extra:
        return False, "В теме есть лишние ключи: " + ", ".join(extra[:8])

    for key in sorted(expected):
        token = colors.get(key)
        if not isinstance(token, dict):
            return False, f"Ключ {key} должен быть объектом."
        color = token.get("color")
        if not _is_valid_theme_color(color):
            return False, f"У ключа {key} некорректный color: {color!r}."
    return True, ""


def normalize_theme_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Оставляет в теме стабильный порядок и обязательные поля шаблона."""
    if _is_palette_theme_payload(payload):
        return compile_palette_theme_payload(payload)

    template = _build_documented_llm_template()
    normalized = copy.deepcopy(template)
    normalized["schemaVersion"] = 1
    normalized["themeName"] = str(payload.get("themeName") or "Custom Theme").strip()
    if isinstance(payload.get("author"), str):
        normalized["author"] = payload["author"]
    if isinstance(payload.get("notes"), str):
        normalized["notes"] = payload["notes"]

    src_colors = payload.get("colors", {})
    for key in normalized["colors"].keys():
        src_token = src_colors.get(key, {})
        if isinstance(src_token, dict) and isinstance(src_token.get("color"), str):
            normalized["colors"][key]["color"] = src_token["color"].strip()
    if payload.get("compiledFrom") == "professionalPalette":
        normalized["compiledFrom"] = "professionalPalette"
        _repair_compiled_professional_payload(normalized)
    return normalized


def theme_payload_to_overrides(payload: Dict[str, Any]) -> Dict[str, str]:
    """Преобразует JSON-тему в overrides старых цветовых literal'ов."""
    payload = normalize_theme_payload(payload)
    ok, _ = validate_theme_payload(payload)
    if not ok:
        return {}

    overrides: Dict[str, str] = {}
    colors = payload["colors"]

    def add_override(source: Any, target: Any, *, allow_replace: bool = False) -> None:
        if not isinstance(source, str) or not isinstance(target, str):
            return
        source_norm = _normalize_color_literal(source)
        target_norm = _normalize_color_literal(target)
        if not source_norm or source_norm == target_norm:
            return
        # Один и тот же старый literal исторически использовался для разных
        # смыслов (например #e4344a = primary, active, danger). Для темы лучше
        # сохранять первый смысловой токен из THEME_LLM_TEMPLATE, иначе поздние
        # danger/delete ключи перетирают главную CTA-палитру и юзер визуально
        # почти не видит, что тема применилась.
        if allow_replace or source_norm not in overrides:
            overrides[source_norm] = target.strip()

    for key, default_token in THEME_LLM_TEMPLATE["colors"].items():
        add_override(default_token.get("color"), colors.get(key, {}).get("color"))

    # Extra bindings для мест, где реальный старый UI уже имел отдельные
    # hardcode-цвета, но LLM-шаблон описывает их одним смысловым токеном.
    # Это особенно важно для monster-card на странице Actors: иначе JSON
    # меняет `monsterCardBackground`, а золотая карточка визуально остаётся
    # прежней.
    def color(key: str) -> Optional[str]:
        token = colors.get(key, {})
        return token.get("color") if isinstance(token, dict) else None

    app_bg = color("appBackground")
    top_surface = color("topHeaderSurface")
    main_surface = color("mainPanelSurface")
    subtle_surface = color("subtleSurface")
    hover_surface = color("hoverSurface")
    shot_bg = color("shotCardBackground")
    empty_bg = color("shotImageEmptyBackground")
    generator_bg = color("generatorCellBackground")
    reference_card_bg = color("referenceCardBackground")
    actor_bg = color("actorCardBackground")
    monster_bg = color("monsterCardBackground")
    popup_bg = color("popupBackground")
    input_bg = color("inputBackground")
    default_border = color("defaultBorder")
    strong_border = color("strongBorder")
    subtle_divider = color("subtleDivider")
    text_primary = color("textPrimary")
    text_secondary = color("textSecondary")
    text_muted = color("textMuted")
    disabled_text = color("disabledText")
    disabled_button_bg = color("disabledButtonBackground")
    primary_text = color("primaryActionButtonText")
    primary = color("primaryActionButton")
    primary_hover = color("primaryActionButtonHover")
    top_nav_accent_text = color("topNavAccentText")
    top_nav_active_bg = color("topNavActiveBackground")
    top_nav_active_text = color("topNavActiveText")
    workflow_bg = color("workflowButtonBackground")
    workflow_border = color("workflowButtonBorder")
    workflow_text = color("workflowButtonText")
    workflow_hover_bg = color("workflowButtonHoverBackground")
    workflow_hover_text = color("workflowButtonHoverText")
    secondary_bg = color("secondaryButtonBackground")
    secondary_text = color("secondaryButtonText")
    secondary_hover = color("secondaryButtonHover")
    secondary_border = color("secondaryButtonBorder")
    outline_bg = color("outlineButtonBackground")
    outline_border = color("outlineButtonBorder")
    outline_text = color("outlineButtonText")
    outline_hover_bg = color("outlineButtonHoverBackground")
    outline_hover_border = color("outlineButtonHoverBorder")
    outline_hover_text = color("outlineButtonHoverText")
    settings_button_bg = color("settingsButtonBackground")
    settings_button_text = color("settingsButtonText")
    settings_button_hover_bg = color("settingsButtonHoverBackground")
    settings_button_hover_text = color("settingsButtonHoverText")
    settings_button_border = color("settingsButtonBorder")
    settings_button_hover_border = color("settingsButtonHoverBorder")
    active = color("activeSelection")
    active_bg = color("activeSelectionBackground")
    active_text = color("activeSelectionText")
    block_pill_bg = color("blockPillBackground")
    block_pill_text = color("blockPillText")
    block_pill_active_bg = color("blockPillActiveBackground")
    block_pill_active_text = color("blockPillActiveText")
    block_pill_unseen_bg = color("blockPillUnseenBackground")
    block_pill_unseen_text = color("blockPillUnseenText")
    episode_badge_bg = color("episodeBadgeBackground")
    episode_badge_border = color("episodeBadgeBorder")
    episode_badge_text = color("episodeBadgeText")
    reference = color("referenceAccent")
    reference_text = color("referenceAccentText")
    reference_bg = color("referenceAccentBackground")
    reference_button_bg = color("referenceButtonBackground")
    reference_button_border = color("referenceButtonBorder")
    reference_button_text = color("referenceButtonText")
    reference_button_hover_bg = color("referenceButtonHoverBackground")
    reference_button_hover_border = color("referenceButtonHoverBorder")
    reference_button_hover_text = color("referenceButtonHoverText")
    drop_bg = color("dropZoneBackground")
    drop_border = color("dropZoneBorder")
    drop_text = color("dropZoneText")
    drop_hover_bg = color("dropZoneHoverBackground")
    drop_hover_border = color("dropZoneHoverBorder")
    shot_title_text = color("shotTitleText")
    shot_desc_text = color("shotDescriptionText")
    shot_duration_text = color("shotDurationText")
    shot_dialog_text = color("shotDialogText")
    seedance_popup_bg = color("seedancePopupBackground")
    seedance_prompt_bg = color("seedancePromptBackground")
    seedance_prompt_text = color("seedancePromptText")
    seedance_input_bg = color("seedanceInputBackground")
    seedance_input_text = color("seedanceInputText")
    seedance_tab_active_bg = color("seedanceTabActiveBackground")
    seedance_tab_active_text = color("seedanceTabActiveText")
    proxy_input_bg = color("proxyInputBackground")
    proxy_input_text = color("proxyInputText")
    proxy_input_border = color("proxyInputBorder")
    success = color("successAccent")
    warning = color("warningAccent")
    warning_text = color("warningAccentText")
    danger = color("dangerAccent")
    danger_text = color("dangerAccentText")
    danger_bg = color("dangerBackground")
    utility_bg = color("imageUtilityButtonBackground")
    utility_hover = color("imageUtilityButtonHover")
    delete_bg = color("imageDeleteButtonBackground")
    delete_hover = color("imageDeleteButtonHover")
    model_badge = color("modelBadgeBackground")
    scroll_track = color("scrollbarTrack")
    scroll_thumb = color("scrollbarThumb")
    face_grid = color("faceGridSelection")

    def _rgb_tuple(value: Optional[str]) -> Optional[tuple[int, int, int]]:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
            return (
                int(raw[1:3], 16),
                int(raw[3:5], 16),
                int(raw[5:7], 16),
            )
        m = re.fullmatch(
            r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})"
            r"(?:\s*,\s*(?:0|1|0?\.\d+|\d{1,3}))?\s*\)",
            raw,
        )
        if m:
            return tuple(max(0, min(255, int(m.group(i)))) for i in range(1, 4))  # type: ignore[return-value]
        return None

    def _rel_luminance(value: Optional[str]) -> Optional[float]:
        rgb = _rgb_tuple(value)
        if rgb is None:
            return None
        vals = []
        for channel in rgb:
            c = channel / 255.0
            vals.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

    def _readable_text_for(bg: Optional[str], current: Optional[str]) -> Optional[str]:
        """Runtime guard для LLM-тем: яркая кнопка не должна получить белый текст."""
        bg_lum = _rel_luminance(bg)
        txt_lum = _rel_luminance(current)
        if bg_lum is None or txt_lum is None:
            return current
        # Если фон яркий, а текст тоже светлый — берём тёмный token текста
        # primaryActionButtonText, если он годится; иначе fallback charcoal.
        if bg_lum >= 0.45 and txt_lum >= 0.55:
            primary_txt_lum = _rel_luminance(primary_text)
            if primary_text and primary_txt_lum is not None and primary_txt_lum < 0.35:
                return primary_text
            return "#080A0B"
        # Если фон тёмный, а текст тоже тёмный — возвращаем светлый readable.
        if bg_lum <= 0.18 and txt_lum <= 0.22:
            return text_primary or "#F5F5F2"
        return current

    primary_text = _readable_text_for(primary, primary_text)
    active_text = _readable_text_for(active, active_text)
    top_nav_active_text = _readable_text_for(top_nav_active_bg, top_nav_active_text)
    workflow_text = _readable_text_for(workflow_bg, workflow_text)
    workflow_hover_text = _readable_text_for(workflow_hover_bg, workflow_hover_text)
    block_pill_active_text = _readable_text_for(
        block_pill_active_bg,
        block_pill_active_text,
    )
    block_pill_unseen_text = _readable_text_for(
        block_pill_unseen_bg,
        block_pill_unseen_text,
    )
    episode_badge_text = _readable_text_for(episode_badge_bg, episode_badge_text)
    reference_text = _readable_text_for(reference, reference_text)
    settings_button_text = _readable_text_for(settings_button_bg, settings_button_text)
    settings_button_hover_text = _readable_text_for(
        settings_button_hover_bg,
        settings_button_hover_text,
    )
    warning_text = _readable_text_for(warning, warning_text)
    seedance_prompt_text = _readable_text_for(seedance_prompt_bg, seedance_prompt_text)
    seedance_input_text = _readable_text_for(seedance_input_bg, seedance_input_text)
    seedance_tab_active_text = _readable_text_for(
        seedance_tab_active_bg,
        seedance_tab_active_text,
    )
    proxy_input_text = _readable_text_for(proxy_input_bg, proxy_input_text)

    def add_many(sources: tuple[str, ...], target: Optional[str]) -> None:
        for source in sources:
            add_override(source, target)

    # Поверхности и фоны. Многие старые виджеты имели локальные оттенки
    # фиолетового/чёрного, которые не входили в исходный LLM-шаблон.
    add_many(("#000", "#000000", "#05070c"), app_bg)
    add_many(("#241c34", "#21182f", "#20182c", "#20162b"), top_surface)
    add_many((
        "#1a1424", "#181024", "#17111f", "#15101e", "#15101f",
        "#140f1e", "#13101a", "#0e0a18", "rgba(20,16,30,0.5)",
        "rgba(20, 16, 30, 0.5)", "rgba(20,14,30,0.78)",
        "rgba(20, 14, 30, 0.78)",
    ), popup_bg)
    add_many(("#221a30", "#221b2e", "#1c1626", "#181222", "#100b18", "#0f0a18"), input_bg)
    add_many(("#1f1730", "#2a1f3d", "#2b203e", "#2a2238", "#171220", "#1a1330"), main_surface)
    add_many(("#15151a",), shot_bg)
    add_many(("#15151b",), reference_card_bg)
    add_many(("#15151c",), actor_bg)
    add_many(("#15151d",), main_surface)
    add_many(("#151519", "#111118", "#121218"), shot_bg)
    add_many(("#1b1028", "#1a0f28", "#20122c", "#28143c"), empty_bg)
    add_many(("#161020", "#0f0f16"), generator_bg)
    add_many(("#07090d",), actor_bg)

    # Settings и segmented controls. Важно: ProviderToggle/ModeSegment — это
    # системный selected state страницы настроек, а не яркий CTA. Для
    # Higgsfield-like тем нельзя превращать их в lime/green brand-плашки.
    add_many(("#ececf0", "#f2f1f7", "#f4f3f8"), active)
    add_override("#ececf1", top_nav_active_bg, allow_replace=True)
    add_override("#1a1425", top_nav_active_text, allow_replace=True)
    add_many(("#ececef",), settings_button_bg)
    add_many(("#ececee",), settings_button_border)
    add_many(("#1a1423",), settings_button_text)
    add_many(("#d8d4df",), settings_button_hover_bg)
    add_many(("#d8d4de",), settings_button_hover_border)
    add_many(("#1a1422",), settings_button_hover_text)
    add_many(("#d8d4e0",), secondary_hover)
    add_many(("rgba(236,236,240,0.30)", "rgba(236, 236, 240, 0.30)"), secondary_bg)
    add_many(("rgba(26,20,36,0.40)", "rgba(26, 20, 36, 0.40)"), text_muted)
    add_override("#8a8d92", strong_border, allow_replace=True)
    add_override("#b8bbc0", text_secondary, allow_replace=True)
    add_override("#d0d3d8", text_primary, allow_replace=True)

    # Role-specific navigation/workflow tokens. Эти near-default literals
    # специально слегка отличаются от старых reference/active цветов, чтобы
    # LLM-тема могла менять их независимо.
    add_override("#d4a257", top_nav_accent_text, allow_replace=True)
    add_override("#d4a258", top_nav_active_bg, allow_replace=True)
    add_override("#15101d", top_nav_active_text, allow_replace=True)
    add_override("#fdfdfe", top_nav_active_text, allow_replace=True)
    add_many((
        "rgba(255,255,255,0.061)", "rgba(255, 255, 255, 0.061)",
    ), top_nav_active_bg)
    add_many((
        "rgba(212,162,86,0.081)", "rgba(212, 162, 86, 0.081)",
    ), workflow_bg)
    add_many((
        "rgba(212,162,86,0.241)", "rgba(212, 162, 86, 0.241)",
    ), workflow_border)
    add_many(("#d4a259",), workflow_text)
    add_many((
        "rgba(212,162,86,0.141)", "rgba(212, 162, 86, 0.141)",
    ), workflow_hover_bg)
    add_many(("#e1b46e",), workflow_hover_text)
    add_many((
        "rgba(255,255,255,0.031)", "rgba(255, 255, 255, 0.031)",
    ), block_pill_bg)
    add_many((
        "rgba(255,255,255,0.551)", "rgba(255, 255, 255, 0.551)",
    ), block_pill_text)
    add_many((
        "rgba(228,52,74,0.151)", "rgba(228, 52, 74, 0.151)",
    ), block_pill_active_bg)
    add_many(("#fffffe",), block_pill_active_text)
    add_many((
        "rgba(212,162,86,0.101)", "rgba(212, 162, 86, 0.101)",
    ), block_pill_unseen_bg)
    add_many(("#d4a25a",), block_pill_unseen_text)
    add_many((
        "rgba(212,162,86,0.102)", "rgba(212, 162, 86, 0.102)",
    ), episode_badge_bg)
    add_many((
        "rgba(212,162,86,0.301)", "rgba(212, 162, 86, 0.301)",
    ), episode_badge_border)
    add_many(("#d4a25b",), episode_badge_text)

    # Рамки и разделители.
    add_many((
        "#2e2440", "#2c2240", "#322545", "#3a2f52", "#3a2c52",
        "#3d2f55", "#3a2d55", "#2f2940", "#2a1f3d",
        "rgba(255,255,255,0.08)", "rgba(255, 255, 255, 0.08)",
        "rgba(255,255,255,0.10)", "rgba(255, 255, 255, 0.10)",
        "rgba(255,255,255,0.18)", "rgba(255, 255, 255, 0.18)",
    ), default_border)
    add_many((
        "#5a3a7a", "#5a4a82", "#463769", "#4a3a6e", "#b3445f",
        "#8a7a3e", "#25193a", "#322a40", "#2a2240", "#4a3470", "#4d3a6b",
        "rgba(255,255,255,0.20)", "rgba(255, 255, 255, 0.20)",
        "rgba(255,255,255,0.30)", "rgba(255, 255, 255, 0.30)",
    ), strong_border)
    add_many((
        "rgba(255,255,255,0.04)", "rgba(255, 255, 255, 0.04)",
        "rgba(255,255,255,0.05)", "rgba(255, 255, 255, 0.05)",
        "rgba(255,255,255,0.055)", "rgba(255, 255, 255, 0.055)",
        "rgba(255,255,255,0.03)", "rgba(255, 255, 255, 0.03)",
    ), subtle_divider)

    # Текст.
    add_many(("#fff", "#e0e0e0", "#dddddd", "#ddd", "#e8e3f0", "#f2eef8", "#f2f1f7", "#cfcfda", "#cfe8ff"), text_primary)
    add_many((
        "#aaa", "#cfcfcf", "#b9a7e6", "#d8c8ff", "#bba4d6", "#9a8fb0",
        "rgba(255,255,255,0.70)", "rgba(255, 255, 255, 0.70)",
        "rgba(255,255,255,0.62)", "rgba(255, 255, 255, 0.62)",
        "rgba(255,255,255,0.55)", "rgba(255, 255, 255, 0.55)",
        "rgba(255,255,255,0.85)", "rgba(255, 255, 255, 0.85)",
        "rgba(255,255,255,0.90)", "rgba(255, 255, 255, 0.90)",
    ), text_secondary)
    add_many((
        "#888", "#666", "#555", "#444",
        "rgba(255,255,255,0.48)", "rgba(255, 255, 255, 0.48)",
        "rgba(255,255,255,0.45)", "rgba(255, 255, 255, 0.45)",
        "rgba(255,255,255,0.40)", "rgba(255, 255, 255, 0.40)",
        "rgba(255,255,255,0.30)", "rgba(255, 255, 255, 0.30)",
    ), text_muted)
    add_many(("#fefefe",), primary_text)
    add_many(("#fbfbfb",), secondary_text)
    add_many(("#fdfdfd",), active_text)
    add_many(("#fcfcfc",), reference_text)
    add_many(("#f9f9f9",), warning_text)
    add_many(("#fafafa",), danger_text)
    add_many(("#fdfdff",), shot_title_text)
    add_many(("#878788",), shot_desc_text)
    add_many(("#666667",), shot_duration_text)
    add_many(("#b9a7e5",), shot_dialog_text)
    add_many(("rgba(255,255,255,0.30)", "rgba(255, 255, 255, 0.30)"), disabled_text)

    # Главные, вторичные и активные кнопки.
    add_many(("#e63946", "#e4344a"), primary)
    add_many(("#d92d44", "#c52539", "#c4283c"), primary_hover)
    add_many((
        "rgba(255,255,255,0.06)", "rgba(255, 255, 255, 0.06)",
        "rgba(255,255,255,0.10)", "rgba(255, 255, 255, 0.10)",
        "rgba(20,20,24,0.72)", "rgba(20, 20, 24, 0.72)",
        "rgba(35,35,40,0.85)", "rgba(35, 35, 40, 0.85)",
    ), secondary_bg)
    add_many((
        "#2c2438", "#372659",
        "rgba(255,255,255,0.12)", "rgba(255, 255, 255, 0.12)",
        "rgba(255,255,255,0.16)", "rgba(255, 255, 255, 0.16)",
        "rgba(255,255,255,0.18)", "rgba(255, 255, 255, 0.18)",
        "rgba(60,48,90,0.25)", "rgba(60, 48, 90, 0.25)",
        "rgba(60,48,90,0.4)", "rgba(60, 48, 90, 0.4)",
    ), secondary_hover)
    add_many(("rgba(255,255,255,0.14)", "rgba(255, 255, 255, 0.14)"), secondary_border)
    add_many(("rgba(0,0,0,0)", "rgba(0, 0, 0, 0)"), outline_bg)
    add_many(("#3a2c53",), outline_border)
    add_many(("#d8c8fd",), outline_text)
    add_many(("#2a1f3c",), outline_hover_bg)
    add_many(("#5a4a83",), outline_hover_border)
    add_many(("#fbfbfa",), outline_hover_text)
    add_many(("#4b2f73", "#6e4cc4", "#8e6cdc", "#8e6cd4", "#b08af7", "#d8a8ff", "#c9a8ff"), active)
    add_many((
        "rgba(228,52,74,0.10)", "rgba(228, 52, 74, 0.10)",
        "rgba(228,52,74,0.15)", "rgba(228, 52, 74, 0.15)",
        "rgba(228,52,74,0.25)", "rgba(228, 52, 74, 0.25)",
        "rgba(228,52,74,0.40)", "rgba(228, 52, 74, 0.40)",
        "rgba(228,52,74,0.45)", "rgba(228, 52, 74, 0.45)",
    ), active_bg)

    # Жёлтые/золотые старые акценты, статусы и готовые блоки.
    add_many((
        "#d4a256", "#f3c14b", "#ffd24d", "#ffe27a", "#fff3a0",
        "#ffc83a", "#f0a000", "#ffd47a", "#f3d28a", "#fff3c4",
        "#cdb081", "#f6dca8",
    ), reference)
    add_many((
        "rgba(212,162,86,0.1)", "rgba(212, 162, 86, 0.1)",
        "rgba(212,162,86,0.10)", "rgba(212, 162, 86, 0.10)",
        "rgba(212,162,86,0.18)", "rgba(212, 162, 86, 0.18)",
        "rgba(212,162,86,0.32)", "rgba(212, 162, 86, 0.32)",
        "rgba(255,204,112,0.06)", "rgba(255, 204, 112, 0.06)",
        "rgba(214,161,70,0.16)", "rgba(214, 161, 70, 0.16)",
    ), reference_bg)
    add_many((
        "rgba(255,204,112,0.065)", "rgba(255, 204, 112, 0.065)",
    ), reference_button_bg)
    add_many((
        "rgba(214,161,70,0.43)", "rgba(214, 161, 70, 0.43)",
    ), reference_button_border)
    add_many(("#f6dca7",), reference_button_text)
    add_many((
        "rgba(214,161,70,0.165)", "rgba(214, 161, 70, 0.165)",
    ), reference_button_hover_bg)
    add_many((
        "rgba(255,204,112,0.69)", "rgba(255, 204, 112, 0.69)",
    ), reference_button_hover_border)
    add_many(("#fff8ea",), reference_button_hover_text)
    add_many((
        "rgba(212,162,86,0.3)", "rgba(212, 162, 86, 0.3)",
        "rgba(212,162,86,0.55)", "rgba(212, 162, 86, 0.55)",
        "rgba(212,162,86,0.78)", "rgba(212, 162, 86, 0.78)",
        "rgba(214,161,70,0.82)", "rgba(214, 161, 70, 0.82)",
        "rgba(255,204,112,0.42)", "rgba(255, 204, 112, 0.42)",
        "rgba(255,204,112,0.70)", "rgba(255, 204, 112, 0.70)",
        "rgba(232,178,72,0.78)", "rgba(255,214,120,0.95)",
        "rgba(255,200,58,0.78)", "rgba(255, 200, 58, 0.78)",
    ), reference)

    # Success/warning/danger.
    add_many((
        "#3a5a3a", "#4d7a4d", "#3a8c52", "#46d160", "#6db86d",
        "#7cc97c", "#7fbf7f", "#4d8a4d", "#6dba6d", "#4d9e6b",
    ), success)
    add_many(("#ffcc66", "#ffaa44", "#e0913a"), warning)
    add_many((
        "#c4304c", "#c47878", "#7a3a3a", "#ff6464", "#ff7070", "#ff7a7a",
        "#ff8a8a", "#ff8a99", "#ff9a9a", "#ffb3b3",
    ), danger)
    add_many((
        "#2a1414", "#3a1a1a", "rgba(80,18,18,0.92)",
        "rgba(80, 18, 18, 0.92)", "rgba(196,48,76,0.10)",
        "rgba(196, 48, 76, 0.10)", "rgba(228,52,74,0.52)",
    ), danger_bg)

    # Overlay/image controls and generator badges.
    add_many((
        "rgba(0,0,0,0.55)", "rgba(0, 0, 0, 0.55)",
        "rgba(20,20,24,0.40)", "rgba(20, 20, 24, 0.40)",
        "rgba(10,6,18,0.7)", "rgba(10, 6, 18, 0.7)",
        "rgba(10,6,18,0.65)", "rgba(10, 6, 18, 0.65)",
    ), utility_bg)
    add_many((
        "rgba(255,255,255,0.16)", "rgba(255, 255, 255, 0.16)",
        "rgba(40,24,64,0.85)", "rgba(40, 24, 64, 0.85)",
    ), utility_hover)
    add_many((
        "rgba(228,52,74,0.35)", "rgba(228, 52, 74, 0.35)",
        "rgba(232,75,74,0.35)", "rgba(232, 75, 74, 0.35)",
    ), delete_bg)
    add_many((
        "rgba(228,52,74,0.72)", "rgba(228, 52, 74, 0.72)",
        "rgba(150,40,40,0.9)", "rgba(150, 40, 40, 0.9)",
    ), delete_hover)
    add_many(("rgba(0,0,0,0.7)", "rgba(0, 0, 0, 0.7)"), model_badge)
    add_many(("rgba(255,255,255,0.03)", "rgba(255, 255, 255, 0.03)"), scroll_track)
    add_many(("rgba(255,255,255,0.22)", "rgba(255, 255, 255, 0.22)"), scroll_thumb)
    add_many(("#171120",), seedance_popup_bg)
    add_override("#151020", seedance_prompt_bg, allow_replace=True)
    add_many(("#ddddde",), seedance_prompt_text)
    add_many(("#181025",), seedance_input_bg)
    add_many(("#dddddf",), seedance_input_text)
    add_many(("#2a1d45",), seedance_tab_active_bg)
    add_many(("#fdfdfc",), seedance_tab_active_text)
    add_many(("#221a31",), proxy_input_bg)
    add_many(("#dddddc",), proxy_input_text)
    add_many(("#2c2241",), proxy_input_border)
    add_many(("rgba(236,236,240,0.30)", "rgba(236, 236, 240, 0.30)"), disabled_button_bg)
    add_override("rgba(236,236,240,0.30)", disabled_button_bg, allow_replace=True)
    add_override("rgba(236, 236, 240, 0.30)", disabled_button_bg, allow_replace=True)

    # Monster-card на странице Actors.
    add_many(("#4a3416", "#24180b", "#0d0a08", "#2f2110", "#563d1b", "#2a1c0d"), monster_bg)
    add_many(("#7a5524", "#8e642b", "#b88a3c"), reference)
    add_many(("#fff8eb",), text_primary)
    add_many(("rgba(242,205,143,0.72)",), text_secondary)

    # Drop zones: Actors upload + textures upload. Эти зоны не должны брать
    # общий active/reference accent, иначе Higgsfield-like темы превращают их
    # в яркие фиолетовые/лаймовые пятна.
    add_many((
        "rgba(110,76,196,0.10)", "rgba(110, 76, 196, 0.10)",
    ), drop_bg)
    add_many((
        "rgba(160,120,240,0.45)", "rgba(160, 120, 240, 0.45)",
    ), drop_border)
    add_many(("#d8c8fe",), drop_text)
    add_many((
        "rgba(110,76,196,0.25)", "rgba(110, 76, 196, 0.25)",
    ), drop_hover_bg)
    add_many((
        "rgba(190,150,255,0.85)", "rgba(190, 150, 255, 0.85)",
    ), drop_hover_border)

    # Shot Viewer/version strip и локальные карточки истории шота.
    add_many(("#2b2142", "#5a2440", "#3a3520", "#231840"), secondary_bg)
    add_many(("#1a1428", "#16222e"), subtle_surface)

    # Спец-инструменты: face grid / selection. Старые голубые и фиолетовые
    # ручки должны уходить в один управляемый токен.
    add_many((
        "#7fb8ff", "#7fc8ff", "#5a8aaa", "#5bbcff", "#4aa3ff",
        "#6fb6ff", "#9fd0ff", "#4a7fb0", "#6aa0d8", "#a8c8ff", "#4d6a8a",
    ), face_grid)
    return overrides


def set_theme_overrides_from_payload(payload: Dict[str, Any]) -> None:
    """Делает переданную тему активной для runtime-слоя текущего процесса."""
    THEME_COLOR_OVERRIDES.clear()
    THEME_COLOR_OVERRIDES.update(theme_payload_to_overrides(payload))


def apply_builtin_theme_overrides() -> str:
    """Applies the built-in shipped interface theme."""
    set_theme_overrides_from_payload(_build_higgsfield_theme_payload())
    return HIGGSFIELD_THEME_NAME


def load_active_theme_overrides(settings: Optional[QSettings] = None) -> Optional[str]:
    """Applies the shipped interface theme and ignores legacy user themes."""
    _ = settings
    return apply_builtin_theme_overrides()


# ── Runtime Color Layer ─────────────────────────────────────────────────
# 2026-06-26 / Codex:
# Раньше большая часть цветов была зашита прямо в QSS-строки по файлам
# storyboard_app.py, generator/*, views/*, widgets/*. Полная ручная замена
# каждого setStyleSheet(...) слишком рискованна: можно легко сломать размеры,
# селекторы или платформенные нюансы Qt.
#
# Поэтому вводим один центральный слой перекраски:
#   1. Любая QSS-строка перед установкой проходит через theme_qss(...).
#   2. theme_qss находит hex/rgb/rgba цвета.
#   3. Если цвет есть в THEME_COLOR_OVERRIDES — заменяет его на значение отсюда.
#   4. Если цвета нет — оставляет как было.
#
# ВАЖНО: по умолчанию словарь пустой, поэтому внешний вид программы НЕ меняется.
# Для новой палитры меняем только этот файл, например:
#   THEME_COLOR_OVERRIDES["#e4344a"] = "#d7ff00"
#   THEME_COLOR_OVERRIDES["rgba(228,52,74,0.40)"] = "rgba(215,255,0,0.35)"
#
# Нормализация убирает пробелы и приводит к lower-case, так что в коде могут
# встречаться и `rgba(255, 255, 255, 0.06)`, и `rgba(255,255,255,0.06)`.

THEME_COLOR_OVERRIDES: Dict[str, str] = {
    # Higgsfield / Graphite эксперименты будем добавлять сюда отдельной задачей.
}

_COLOR_RE = re.compile(
    r"#[0-9A-Fa-f]{3,8}|rgba?\([^)]*\)"
)


def _normalize_color_literal(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def theme_color_literal(value: str) -> str:
    """Возвращает цвет с учётом центральных override'ов темы.

    Функция специально принимает старый literal из кода. Это позволяет менять
    палитру без массового переписывания всех QSS-строк.
    """
    return THEME_COLOR_OVERRIDES.get(_normalize_color_literal(value), value)


def theme_qcolor(value: str) -> QColor:
    """QColor из центрального цветового слоя."""
    value = theme_color_literal(value)
    normalized = _normalize_color_literal(value)
    m = re.fullmatch(
        r"rgba?\((\d+),(\d+),(\d+)(?:,([0-9.]+))?\)",
        normalized,
    )
    if m:
        r, g, b = [int(m.group(i)) for i in range(1, 4)]
        alpha_s = m.group(4)
        if alpha_s is None:
            a = 255
        else:
            alpha_f = float(alpha_s)
            a = int(round(alpha_f * 255)) if alpha_f <= 1 else int(round(alpha_f))
        return QColor(r, g, b, max(0, min(255, a)))
    return QColor(value)


def theme_qss(qss: str) -> str:
    """Применяет центральные цветовые overrides к QSS-строке."""
    if not qss or not THEME_COLOR_OVERRIDES:
        return qss
    return _COLOR_RE.sub(lambda m: theme_color_literal(m.group(0)), qss)


_THEME_RUNTIME_INSTALLED = False


def install_theme_runtime() -> None:
    """Устанавливает безопасный глобальный QSS-hook для Qt widget styles.

    Hook меняет только строку stylesheet перед передачей в оригинальный
    QWidget.setStyleSheet. Если THEME_COLOR_OVERRIDES пустой, строка остаётся
    байт-в-байт той же.
    """
    global _THEME_RUNTIME_INSTALLED
    if _THEME_RUNTIME_INSTALLED:
        return

    original = QWidget.setStyleSheet

    def themed_set_style_sheet(self, style_sheet):  # noqa: ANN001
        if isinstance(style_sheet, str):
            style_sheet = theme_qss(style_sheet)
        return original(self, style_sheet)

    QWidget.setStyleSheet = themed_set_style_sheet
    _THEME_RUNTIME_INSTALLED = True


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
            painter.fillRect(
                self.rect(),
                QColor(theme_color_literal(LUMZ_THEME["bg_main"]))
            )

            # 2. Glow отключён: пользователь замеряет фон пипеткой и ожидает
            # ровно rgb(16,17,18), без смешивания с радиальным слоем.
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
            f"{sel} {{ background:#e4344a; color:#fefefe; border:none;"
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
            " color:#fbfbfb; border-radius:6px;"
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
            f"{sel}:hover {{ color:#fbfbfb;"
            " background:rgba(255,255,255,0.04); }}"
            f"{sel}:disabled {{ color:rgba(255,255,255,0.30); }}"
        )
    raise ValueError(f"unknown variant: {variant!r}")
