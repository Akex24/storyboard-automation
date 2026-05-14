# -*- coding: utf-8 -*-
"""
agents/instruction_loader.py — загрузка ГЛАВНАЯ_ИНСТРУКЦИЯ.md (и других
промпт-инструкций) из bundle с селективным извлечением разделов.

v1.0.66: ранее каждый агент имел hard-coded system_prompt в
`agents/montage_prompts.py`. Это приводило к рассинхрону с Алексовой
ГЛАВНАЯ_ИНСТРУКЦИЯ.md в claude.ai-флоу (отсутствовали ШАГ 1-4
алгоритма разбивки, ПРАВИЛА 1-4 про реплики, ИЕРАРХИЯ СЖАТИЯ).
Симптом — Scriptwriter на ep2 «Финальный расчёт» выкидывал
драматические триггеры (стройка Дэвида) при сжатии длинного сценария.

Решение — runtime загрузка ГЛАВНАЯ_ИНСТРУКЦИЯ.md из bundle через
`storyboard_app.read_bundled_text` + парсинг markdown-секций «## N. ...»
+ селективное извлечение нужных агенту разделов.

Карта селекции:
    Scriptwriter:     [1, 3, 4, 6, 8]  — роль + ДНК + тайминг + карта + теги
    Validator:        [4, 6, 8]        — формула + лимиты + теги
    Editor:           [3, 4, 6, 8]     — ДНК + формула + лимиты + теги
    Context Reviewer: [1, 3]           — роль + ДНК

Кэширование: module-level dict, ключ `(filename, tuple(sections))`.
Загрузка один раз при первом import `agents/montage_prompts.py` —
orchestrator живёт в Studio process пока юзер открывает монтажки.

Fallback: если файл не зашит в bundle (старый Installer без instructions/)
→ `read_bundled_text` вернёт пустую строку → `load_sections` тоже
пустую → `agents/montage_prompts.py` применит свой `_FALLBACK_*`
(текущие hard-coded версии текстов). Vanilla cutover в v1.0.67+
когда все коллеги обновятся.

Cross-platform: чистый Python + stdlib + lazy import `storyboard_app`.
Mac и Win одинаково.

История: создано 2026-05-13 (v1.0.66).
"""

from __future__ import annotations

import re
from typing import List, Tuple


# Имя по умолчанию — главная инструкция. Лежит в bundle через
# StoryboardStudio.spec datas (v1.0.66) как
# `instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md` относительно sys._MEIPASS.
DEFAULT_INSTRUCTION_FILE = "ГЛАВНАЯ_ИНСТРУКЦИЯ.md"
INSTRUCTIONS_DIR = "instructions"

# Кэш: ключ = (filename, sections_tuple), value = склеенный текст.
# Заполняется при первом вызове `load_sections`, освобождается при
# завершении процесса Studio.
_CACHE: dict = {}


def _resolve_reader():
    """Lazy import `storyboard_app.read_bundled_text` чтобы избежать
    circular import: agents/montage_prompts.py импортирует этот модуль,
    storyboard_app.py тоже импортирует подмодули agents/.
    Возвращает callable (rel_path, default) -> str.
    """
    try:
        import sys as _sys
        main_mod = _sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, 'read_bundled_text'):
            return main_mod.read_bundled_text
        from storyboard_app import read_bundled_text  # noqa: WPS433
        return read_bundled_text
    except Exception:
        return lambda _rp, default="": default


def load_instruction_md(filename: str = DEFAULT_INSTRUCTION_FILE) -> str:
    """Загружает полный текст файла-инструкции из bundle.

    Args:
        filename: имя файла в `instructions/`. По умолчанию
                  ГЛАВНАЯ_ИНСТРУКЦИЯ.md.
    Returns:
        Содержимое файла или "" если не найден (silent fallback).
    """
    reader = _resolve_reader()
    rel_path = f"{INSTRUCTIONS_DIR}/{filename}"
    return reader(rel_path, default="")


# Regex для парсинга markdown-заголовков верхнего уровня
# («## 1. РОЛЬ И ЗАДАЧА», «## 6. МОНТАЖНАЯ КАРТА — РАЗБИВКА НА БЛОКИ»).
# Захватывает номер раздела и всю строку с заголовком (для повторной
# вставки в выход).
_SECTION_HEADER_RE = re.compile(r'^##\s+(\d+)\.\s+.+$', re.MULTILINE)


def extract_md_sections(text: str, sections: List[int]) -> str:
    """Извлекает указанные разделы верхнего уровня из markdown-текста.

    Парсит заголовки `## N. ...` (где N — целое число), отбирает
    разделы по списку `sections`, склеивает их в порядке появления
    в исходнике (не в порядке `sections`).

    Сохраняет заголовки разделов и весь контент до следующего `## `
    заголовка верхнего уровня (включая подразделы `### ...`).

    Args:
        text:     полный текст ГЛАВНАЯ_ИНСТРУКЦИЯ.md.
        sections: список номеров разделов (например [1, 3, 4, 6, 8]).
    Returns:
        Склеенный markdown с только выбранными разделами.
        Если text пуст / разделы не найдены — пустая строка.
    """
    if not text or not sections:
        return ""
    wanted = set(int(n) for n in sections)
    # Найти все top-level разделы с их позициями в тексте.
    matches = list(_SECTION_HEADER_RE.finditer(text))
    if not matches:
        return ""
    parts: List[str] = []
    for i, m in enumerate(matches):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        if num not in wanted:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].rstrip()
        parts.append(chunk)
    if not parts:
        return ""
    return "\n\n".join(parts)


def load_sections(
    sections: List[int],
    filename: str = DEFAULT_INSTRUCTION_FILE,
) -> str:
    """Combo: load_instruction_md + extract_md_sections + cache.

    Кэширует результат по ключу `(filename, tuple(sections))` — один
    раз парсит md, последующие вызовы возвращают готовую строку.

    Args:
        sections: номера разделов, например [1, 3, 4, 6, 8].
        filename: имя файла-инструкции (default — ГЛАВНАЯ_ИНСТРУКЦИЯ.md).
    Returns:
        Селективно склеенный markdown или "" при ошибке/отсутствии.
    """
    key: Tuple[str, Tuple[int, ...]] = (filename, tuple(sorted(sections)))
    if key in _CACHE:
        return _CACHE[key]
    full_text = load_instruction_md(filename)
    extracted = extract_md_sections(full_text, sections)
    # v1.0.66 fix: НЕ кэшируем пустой результат — на ранней стадии
    # загрузки storyboard_app (циклический import до определения
    # read_bundled_text) reader падает в lambda fallback и возвращает
    # "". Если кэшировать "" — последующие вызовы (когда storyboard_app
    # полностью загружен) тоже вернут "". Не кэшируя empty — даём шанс
    # следующей попытке прочитать .md правильно.
    if not extracted:
        return ""
    _CACHE[key] = extracted
    return extracted


def clear_cache() -> None:
    """Очистка кэша. Используется в тестах / dev hot-reload (в продакшене
    не вызывается — кэш живёт пока живёт процесс Studio)."""
    _CACHE.clear()


# v1.0.75: regex для парсинга подзаголовков `### Текст` внутри раздела.
_SUBSECTION_HEADER_RE = re.compile(r'^###\s+(.+?)\s*$', re.MULTILINE)


def extract_md_subsection(text: str, section_num: int, anchor: str) -> str:
    """Извлекает подсекцию `### <anchor>` из раздела N верхнего уровня.

    Сначала находит раздел `## {section_num}. ...` и его границы (до
    следующего `## ` или EOF). Внутри ищет `### <anchor>` (substring
    match, regex-escape) и возвращает текст от этого `### ` до
    следующего `### ` ИЛИ `## ` (исключительно), включая сам заголовок.

    Args:
        text:        полный текст ГЛАВНАЯ_ИНСТРУКЦИЯ.md.
        section_num: номер раздела верхнего уровня (например 6).
        anchor:      подстрока заголовка `###` для поиска (например
                     "ПРОСТРАНСТВЕННАЯ ГЕОМЕТРИЯ СЦЕНЫ"). Поиск регистро-
                     чувствительный, начало строки заголовка должно
                     содержать эту подстроку.
    Returns:
        Текст подсекции (с её заголовком `### ...`) или "" если не
        найдено.
    """
    if not text or not anchor:
        return ""
    section_matches = list(_SECTION_HEADER_RE.finditer(text))
    sect_start = sect_end = None
    for i, m in enumerate(section_matches):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        if num == section_num:
            sect_start = m.start()
            sect_end = (section_matches[i + 1].start()
                        if i + 1 < len(section_matches) else len(text))
            break
    if sect_start is None:
        return ""
    section_body = text[sect_start:sect_end]
    sub_matches = list(_SUBSECTION_HEADER_RE.finditer(section_body))
    for j, sm in enumerate(sub_matches):
        title = sm.group(1)
        if anchor in title:
            sub_start = sm.start()
            sub_end = (sub_matches[j + 1].start()
                       if j + 1 < len(sub_matches) else len(section_body))
            return section_body[sub_start:sub_end].rstrip()
    return ""


def load_subsection(
    section_num: int,
    anchor: str,
    filename: str = DEFAULT_INSTRUCTION_FILE,
) -> str:
    """Combo: load_instruction_md + extract_md_subsection + cache.

    Кэширует по ключу `(filename, 'sub', section_num, anchor)`. Empty
    результат НЕ кэшируется (та же логика что у `load_sections` —
    защита от циклического import на ранней стадии).

    Args:
        section_num: номер раздела верхнего уровня.
        anchor:      подстрока заголовка `### ...` для поиска.
        filename:    имя файла-инструкции (default ГЛАВНАЯ_ИНСТРУКЦИЯ.md).
    Returns:
        Текст подсекции или "" если не найдено / файл пуст.
    """
    key = (filename, 'sub', section_num, anchor)
    if key in _CACHE:
        return _CACHE[key]
    full_text = load_instruction_md(filename)
    extracted = extract_md_subsection(full_text, section_num, anchor)
    if not extracted:
        return ""
    _CACHE[key] = extracted
    return extracted
