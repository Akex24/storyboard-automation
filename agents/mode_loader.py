# -*- coding: utf-8 -*-
"""
agents/mode_loader.py — загрузка модулей правил монтажной карты
в зависимости от выбранного юзером режима (A / B / C / D).

Режимы хранятся в QSettings под ключом 'montage_mode' (per-user,
аналогично 'ui_lang' и 'image_provider'). По умолчанию — режим A.

Маппинг режим → файлы:
  A → agents.montage_rules,       instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md
  B → agents.montage_rules_b,     instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_b.md
  C → agents.montage_rules_c,     instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_c.md
  D → agents.montage_rules_d,     instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ_d.md

(А также параллельные копии `agents.validator_prefilter[_x]`.)

Этот модуль — ТОЛЬКО загрузчик. Никакого UI, никаких импортов
`agents.montage_prompts` / `threads.montage_orchestrator` — чтобы
избежать циклических импортов. Зависимости: только `PyQt6.QtCore`,
`importlib`, `typing`.

Кто будет вызывать (после интеграции в коммитах 2-4):
  - `agents/montage_prompts.py` — заменит прямой `from agents.montage_rules`
    импорт на `import_rules_module()`.
  - `agents/instruction_loader.py` — заменит `DEFAULT_INSTRUCTION_FILE`
    на `get_instruction_filename()`.
  - `threads/montage_orchestrator.py` — заменит `from agents.validator_prefilter
    import prefilter_check` на `import_validator_prefilter().prefilter_check`.
  - Settings UI — `get_current_mode` / `set_current_mode` для дропдауна.

Cross-platform: `QSettings` разруливает Mac (.plist) / Win (registry) /
Linux (.conf) сам через Qt. Никаких subprocess/shell — Studio shipping
в Win .exe безопасен.

История: создан 2026-05-21 (переключатель режимов монтажной карты,
ветка feature/mode-switcher, коммит 1/4).
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Tuple

from PyQt6.QtCore import QSettings


# ─── Константы ───────────────────────────────────────────────────────────

# Идентификаторы Studio в QSettings — должны совпадать с
# APP_ORG/APP_NAME из storyboard_app.py и _QS_ORG/_QS_NAME из i18n.py.
APP_ORG = 'StoryboardStudio'
APP_NAME = 'StoryboardApp'

# Ключ настройки.
QS_KEY = 'montage_mode'

# Допустимые режимы (lowercase ASCII). Дефолт — A.
VALID_MODES: Tuple[str, ...] = ('a', 'b', 'c', 'd')
DEFAULT_MODE = 'a'


# ─── Чтение / запись текущего режима ─────────────────────────────────────

def get_current_mode() -> str:
    """Активный режим монтажной карты из QSettings.

    Возвращает один из VALID_MODES. При любой ошибке (нет QSettings,
    кривое сохранённое значение, отсутствие ключа) — возвращает
    DEFAULT_MODE.

    Поведение аналогично `i18n.get_lang()`.
    """
    try:
        s = QSettings(APP_ORG, APP_NAME)
        v = str(s.value(QS_KEY, DEFAULT_MODE) or DEFAULT_MODE).lower()
        return v if v in VALID_MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def set_current_mode(mode: str) -> None:
    """Сохраняет режим в QSettings + принудительный flush на диск.

    Если `mode` не входит в VALID_MODES — применяется DEFAULT_MODE
    (молча, без exception — UI должен валидировать заранее, но в
    тестах удобно полагаться на nice-failure).

    Поведение аналогично `i18n.set_lang()`.
    """
    m = (mode or '').lower()
    if m not in VALID_MODES:
        m = DEFAULT_MODE
    try:
        s = QSettings(APP_ORG, APP_NAME)
        s.setValue(QS_KEY, m)
        s.sync()
    except Exception:
        # QSettings вне Qt event loop / tests без QCoreApplication —
        # молча игнорируем чтобы не падать в edge cases.
        pass


# ─── Хелпер: режим → суффикс ─────────────────────────────────────────────

def get_suffix_for_mode(mode: str) -> str:
    """Суффикс для имени модуля / файла для данного режима.

    A → '' (без суффикса)
    B → '_b'
    C → '_c'
    D → '_d'

    Любое невалидное значение → '' (фолбэк на A) — это нужно чтобы
    `import_rules_module()` не пытался загрузить несуществующий
    `montage_rules_<garbage>.py`.
    """
    m = (mode or '').lower()
    if m == DEFAULT_MODE or m not in VALID_MODES:
        return ''
    return f'_{m}'


# ─── Импорт модулей по активному режиму ──────────────────────────────────

def import_rules_module() -> ModuleType:
    """Импортирует модуль правил монтажной карты для активного режима.

    Returns:
        Загруженный модуль `agents.montage_rules[_x]` (содержит
        COMMON_RULES, STRUCTURAL_RULES, 4×FALLBACK_*_SYSTEM, 4×ROLE,
        4×JSON_TAIL, GEOMETRY_EDITOR ROLE+JSON_TAIL).

    При ошибке импорта режимного модуля (отсутствует файл, синтаксис
    и т.п.) — фолбэк на `agents.montage_rules` (режим A). Ошибка
    логируется в stdout префиксом `[mode_loader]`.
    """
    mode = get_current_mode()
    suffix = get_suffix_for_mode(mode)
    module_name = f'agents.montage_rules{suffix}'
    try:
        return importlib.import_module(module_name)
    except Exception as e:
        print(
            f"[mode_loader] failed to import {module_name!r}: {e}. "
            f"Falling back to agents.montage_rules (mode A)."
        )
        return importlib.import_module('agents.montage_rules')


def import_validator_prefilter() -> ModuleType:
    """Импортирует модуль Python pre-filter Validator'а для активного режима.

    Returns:
        Загруженный модуль `agents.validator_prefilter[_x]` (содержит
        `prefilter_check`, `PREFILTER_RULES`, константы лимитов).

    При ошибке — фолбэк на `agents.validator_prefilter` (режим A).
    """
    mode = get_current_mode()
    suffix = get_suffix_for_mode(mode)
    module_name = f'agents.validator_prefilter{suffix}'
    try:
        return importlib.import_module(module_name)
    except Exception as e:
        print(
            f"[mode_loader] failed to import {module_name!r}: {e}. "
            f"Falling back to agents.validator_prefilter (mode A)."
        )
        return importlib.import_module('agents.validator_prefilter')


# ─── Имя файла главной инструкции ────────────────────────────────────────

def get_instruction_filename() -> str:
    """Имя файла главной инструкции для активного режима.

    Возвращает имя без префикса папки (папка `instructions/` зашита
    в `agents.instruction_loader.INSTRUCTIONS_DIR`):
      A → 'ГЛАВНАЯ_ИНСТРУКЦИЯ.md'
      B → 'ГЛАВНАЯ_ИНСТРУКЦИЯ_b.md'
      C → 'ГЛАВНАЯ_ИНСТРУКЦИЯ_c.md'
      D → 'ГЛАВНАЯ_ИНСТРУКЦИЯ_d.md'

    Аналог константы `DEFAULT_INSTRUCTION_FILE` в
    `agents/instruction_loader.py`.
    """
    mode = get_current_mode()
    suffix = get_suffix_for_mode(mode)
    return f'ГЛАВНАЯ_ИНСТРУКЦИЯ{suffix}.md'
