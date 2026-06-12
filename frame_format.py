# -*- coding: utf-8 -*-
"""frame_format.py — единый источник формат-зависимых фраз/параметров кадра.

Pure-Python (без Qt, без внешних зависимостей) — импортируется и из GUI
(storyboard_app), и из потоков (threads/generate), и из CLI. Источник aspect —
meta.aspect сериала (self._current_aspect в GUI; show_manager.show_aspect в CLI).

Этап 3.1: payload aspect_ratio, фраза одиночной панели (extract_shot_prompt),
формат-строка edit (_build_edit_prompt). Дефолт всюду "9:16" → строки
БАЙТ-В-БАЙТ как было вшито (вертикаль). writer_sheet_header (header писателя
storyboard_writer_prompts) — Этап 3.2.
"""

_VALID = ("9:16", "16:9")


def _norm(aspect: str) -> str:
    """Нормализация: всё кроме '16:9' → '9:16' (дефолт-вертикаль, без сюрпризов)."""
    return aspect if aspect in _VALID else "9:16"


def payload_aspect_ratio(aspect: str = "9:16") -> str:
    """Значение поля aspect_ratio в payload запроса на FastGen (один шот)."""
    return _norm(aspect)


def single_panel_phrase(aspect: str = "9:16") -> str:
    """Фраза «один кадр (НЕ лист), формат X» для промпта шота (extract_shot_prompt).
    Несёт ДВА смысла: (1) рисуй ОДИН кадр, а не лист из 4 панелей; (2) ориентация.
    БЕЗ финального пробела — caller добавляет где нужно.
    9:16 → байт-в-байт прежнее «Single vertical 9:16 panel.»."""
    if _norm(aspect) == "16:9":
        return "Single horizontal 16:9 panel."
    return "Single vertical 9:16 panel."


def edit_format_line(aspect: str = "9:16") -> str:
    """Краткая формат-строка для EDIT-промпта: подставляется в «... CURRENT
    panel, {x} format», «Keep the same {x} format», «single {x} panel».
    9:16 → «vertical 9:16» (прежнее)."""
    if _norm(aspect) == "16:9":
        return "horizontal 16:9"
    return "vertical 9:16"
