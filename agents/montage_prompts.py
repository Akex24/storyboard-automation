# -*- coding: utf-8 -*-
"""
agents/montage_prompts.py — системные промпты для трёх агентов
оркестратора монтажной карты.

Архитектура (см. threads/montage_orchestrator.py):
  1. SCRIPTWRITER  — пишет монтажную карту в JSON по сценарию + рефам.
  2. VALIDATOR     — жёстко проверяет тайминги/лимиты/формат, возвращает
                     список ошибок (или []).
  3. EDITOR        — если есть ошибки → правит JSON, возвращает новую
                     версию. Цикл VALIDATOR → EDITOR до 3 раундов или
                     до пустого списка ошибок.

Все промпты — module-level константы. Правила inlined прямо сюда чтобы
не зависеть от внешних `instructions/*.txt` файлов в момент работы CLI
(промпт уходит в `claude -p` целиком). Если правила в `instructions/`
изменятся — обновить и тут.

История: создано 2026-05-06.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Set
from agents.montage_rules import (
    COMMON_RULES,
    STRUCTURAL_RULES,
    _FALLBACK_SCRIPTWRITER_SYSTEM,
    _FALLBACK_VALIDATOR_SYSTEM,
    _FALLBACK_EDITOR_SYSTEM,
    _FALLBACK_CONTEXT_REVIEWER_SYSTEM,
    _SCRIPTWRITER_ROLE,
    _VALIDATOR_ROLE,
    _EDITOR_ROLE,
    _REVIEWER_ROLE,
    _SCRIPTWRITER_JSON_TAIL,
    _VALIDATOR_JSON_TAIL,
    _EDITOR_JSON_TAIL,
    _REVIEWER_JSON_TAIL,
    _GEOMETRY_EDITOR_ROLE,
    _GEOMETRY_EDITOR_JSON_TAIL,
)














# ─────────────────────────────────────────────────────────────────────────
# v1.0.66: Module-level build публичных system prompts.
#
# Источник правды — `instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md` (зашита в bundle
# через StoryboardStudio.spec datas). Загружается через
# `agents.instruction_loader.load_sections` с селективным извлечением
# разделов по агенту. Результат склеивается с _*_ROLE и _*_JSON_TAIL.
#
# Карта селекции:
#   Scriptwriter:     [1, 3, 4, 6, 8]  — роль + ДНК + тайминг + карта + теги
#   Validator:        [4, 6, 8]        — формула + лимиты + теги
#   Editor:           [3, 4, 6, 8]     — ДНК + формула + лимиты + теги
#   Context Reviewer: [1, 3]           — роль + ДНК
#
# Кэш — module-level (в instruction_loader._CACHE). Загружается один раз
# при импорте этого модуля. orchestrator живёт в Studio process, повторных
# чтений нет.
#
# Fallback: если bundle не содержит ГЛАВНАЯ_ИНСТРУКЦИЯ.md (старый
# Installer, dev без файла, ошибка чтения) → `load_sections` вернёт ""
# → `_build_system` применит соответствующий `_FALLBACK_*` (текущие
# hard-coded версии). Будет удалено в v1.0.67+.
# ─────────────────────────────────────────────────────────────────────────

# v1.0.66 (5.4-bis): lazy build через PEP 562 __getattr__.
# Раньше (5.4 eager) module-level вызов `_load_sections([...])` происходил
# при первом импорте `agents.montage_prompts`. Этот импорт случается во
# время загрузки `storyboard_app.py` (через цепочку threads/__init__ →
# montage_orchestrator → agents.montage_prompts), когда сам storyboard_app
# на ранней стадии — `read_bundled_text` ещё не определена. Результат —
# циклический import, _MAIN_* = "" во всех 4, fallback используется
# везде даже на боевом запуске. Lazy build решает проблему: build
# выполняется при первом обращении к публичному имени (внутри
# `MontageOrchestratorThread.run()`, после `app.exec()` старта),
# к этому моменту все модули полностью загружены.
_PUBLIC_CACHE: dict = {}


def _build_lazy(key: str) -> str:
    """Lazy build публичного system prompt по ключу.

    При первом обращении к `SCRIPTWRITER_SYSTEM` / `VALIDATOR_SYSTEM` /
    `EDITOR_SYSTEM` / `CONTEXT_REVIEWER_SYSTEM` через `__getattr__`:
      1. Импорт `load_sections` из `agents.instruction_loader`.
      2. Извлечение нужных разделов из ГЛАВНАЯ_ИНСТРУКЦИЯ.md.
      3. Если `main` пустой (bundle без .md, ошибка чтения) →
         возвращает `_FALLBACK_*` (полный hard-coded текст).
      4. Иначе склеивает role + main + tail.
      5. Кэширует в `_PUBLIC_CACHE` — последующие обращения мгновенные.

    Args:
        key: одно из 'SCRIPTWRITER_SYSTEM', 'VALIDATOR_SYSTEM',
             'EDITOR_SYSTEM', 'CONTEXT_REVIEWER_SYSTEM'.
    Returns:
        Финальный system prompt для конкретного агента.
    """
    if key in _PUBLIC_CACHE:
        return _PUBLIC_CACHE[key]
    sections_map = {
        # v1.0.70 (2026-05-14): добавлен раздел 9 «ВИЗУАЛЬНЫЙ ПРИОРИТЕТ»
        # ко всем агентам монтажа — закрывает gap «Scriptwriter пишет
        # 'белая футболка' → Validator ловит как forbidden_phrase →
        # Editor правит» (стоил ~3 мин на эпизод).
        # Context Reviewer расширен с [1,3] до полного состава {1,3,5,7,
        # 8,9,10,11,12} — теперь видит референсы, режиссуру камеры,
        # синтаксис тегов, структуру выдачи, чеклист и передаточный
        # пакет (он отвечает за финальные Seedance/Storyboard промпты,
        # без этих разделов работал «по интуиции Opus»).
        'SCRIPTWRITER_SYSTEM':     [1, 3, 4, 6, 8, 9],
        'VALIDATOR_SYSTEM':        [4, 6, 8, 9],
        'EDITOR_SYSTEM':           [3, 4, 6, 8, 9],
        'CONTEXT_REVIEWER_SYSTEM': [1, 3, 5, 7, 8, 9, 10, 11, 12],
    }
    role_map = {
        'SCRIPTWRITER_SYSTEM':     _SCRIPTWRITER_ROLE,
        'VALIDATOR_SYSTEM':        _VALIDATOR_ROLE,
        'EDITOR_SYSTEM':           _EDITOR_ROLE,
        'CONTEXT_REVIEWER_SYSTEM': _REVIEWER_ROLE,
    }
    tail_map = {
        'SCRIPTWRITER_SYSTEM':     _SCRIPTWRITER_JSON_TAIL,
        'VALIDATOR_SYSTEM':        _VALIDATOR_JSON_TAIL,
        'EDITOR_SYSTEM':           _EDITOR_JSON_TAIL,
        'CONTEXT_REVIEWER_SYSTEM': _REVIEWER_JSON_TAIL,
    }
    fallback_map = {
        'SCRIPTWRITER_SYSTEM':     _FALLBACK_SCRIPTWRITER_SYSTEM,
        'VALIDATOR_SYSTEM':        _FALLBACK_VALIDATOR_SYSTEM,
        'EDITOR_SYSTEM':           _FALLBACK_EDITOR_SYSTEM,
        'CONTEXT_REVIEWER_SYSTEM': _FALLBACK_CONTEXT_REVIEWER_SYSTEM,
    }
    try:
        from agents.instruction_loader import load_sections as _load_sections
        main = _load_sections(sections_map[key])
    except Exception:
        main = ""
    # v1.0.66 fix: если main пустой (например circular import — storyboard_app
    # ещё не полностью загружен, read_bundled_text недоступна, reader
    # упал в lambda fallback) — возвращаем fallback БЕЗ кэширования.
    # На следующем обращении (когда storyboard_app загружен) попробуем
    # снова — теперь main подгрузится корректно и закэшируется правильно.
    if not main:
        return fallback_map.get(key, "")
    result = (f"{role_map[key].rstrip()}\n\n"
               f"{main}\n\n"
               f"{tail_map[key].rstrip()}")
    _PUBLIC_CACHE[key] = result
    return result


def __getattr__(name: str) -> str:
    """PEP 562 — lazy module-level attribute resolution.

    Когда импортёр делает `from agents.montage_prompts import
    SCRIPTWRITER_SYSTEM` или `montage_prompts.SCRIPTWRITER_SYSTEM`,
    Python сначала ищет в module __dict__, если не находит — зовёт
    `__getattr__`. На этой стадии (после полной загрузки storyboard_app)
    `read_bundled_text` уже доступна → ГЛАВНАЯ_ИНСТРУКЦИЯ.md грузится.
    """
    if name in ('SCRIPTWRITER_SYSTEM', 'VALIDATOR_SYSTEM',
                'EDITOR_SYSTEM', 'CONTEXT_REVIEWER_SYSTEM'):
        return _build_lazy(name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}")


# v1.0.69: skip-rules для Validator system prompt.
# Парные маркеры <!-- BEGIN rule_X --> ... <!-- END rule_X --> в
# _VALIDATOR_JSON_TAIL и _FALLBACK_VALIDATOR_SYSTEM — каждый пункт 1..14
# обёрнут. При skip_rules={'rule_1','rule_3',...} соответствующие блоки
# целиком вырезаются из system prompt'а Validator'а. Так AI не тратит
# reasoning на правила, которые уже отработал Python pre-filter
# (см. agents/validator_prefilter.py).
_RULE_BLOCK_RE = re.compile(
    r'<!-- BEGIN (rule_\w+) -->.*?<!-- END \1 -->\n?',
    re.DOTALL,
)


def _strip_rules(text: str, skip_rules: Set[str]) -> str:
    """Удаляет помеченные блоки из текста. Маркер начала/конца —
    `<!-- BEGIN rule_X --> ... <!-- END rule_X -->`. Парность гарантируется
    регуляркой с backreference \\1 — не задетые блоки остаются как есть.
    """
    if not skip_rules:
        return text
    def _repl(m: "re.Match[str]") -> str:
        return '' if m.group(1) in skip_rules else m.group(0)
    return _RULE_BLOCK_RE.sub(_repl, text)


def get_validator_system(skip_rules: Optional[Set[str]] = None) -> str:
    """Возвращает VALIDATOR_SYSTEM с опциональным выкидыванием правил,
    проверенных Python pre-filter'ом.

    Args:
        skip_rules: набор имён правил для удаления, напр.
            {'rule_1','rule_3','rule_5'}. Поддерживаются те же имена,
            что обёрнуты маркерами в _VALIDATOR_JSON_TAIL /
            _FALLBACK_VALIDATOR_SYSTEM (rule_1..rule_14 + rule_7a).
            Если None или пустой — возвращает полный VALIDATOR_SYSTEM
            (поведение идентично прежнему `VALIDATOR_SYSTEM`).

    Returns:
        Финальная строка system prompt для передачи в `claude -p`.
    """
    base = _build_lazy('VALIDATOR_SYSTEM')
    return _strip_rules(base, skip_rules or set())


# ─────────────────────────────────────────────────────────────────────────


def get_geometry_editor_system() -> str:
    """Собирает system prompt для Geometry Editor'а из ROLE + подсекции
    «ПРОСТРАНСТВЕННАЯ ГЕОМЕТРИЯ СЦЕНЫ» раздела 6 ГИ + JSON_TAIL.

    Подсекция загружается через `load_subsection(6, "ПРОСТРАНСТВЕННАЯ
    ГЕОМЕТРИЯ СЦЕНЫ")` — фрагмент ГИ без дублирования. Кэширование
    делается в instruction_loader на уровне `(filename, sub, 6, anchor)`.

    Returns:
        Финальная строка system prompt для передачи в `claude -p`.
        Если подсекция не найдена (bundle без ГИ) — возвращает
        ROLE + TAIL без середины (минимально работоспособный fallback).
    """
    try:
        from agents.instruction_loader import load_subsection as _load_sub
        sub = _load_sub(6, "ПРОСТРАНСТВЕННАЯ ГЕОМЕТРИЯ СЦЕНЫ")
    except Exception:
        sub = ""
    role = _GEOMETRY_EDITOR_ROLE.rstrip()
    tail = _GEOMETRY_EDITOR_JSON_TAIL.rstrip()
    if sub:
        return f"{role}\n\n{sub}\n\n{tail}"
    return f"{role}\n\n{tail}"


def build_geometry_editor_user_prompt(
    montage_card_json: str,
    geometry_errors: list,
) -> str:
    """User-prompt для Geometry Editor'а.

    Args:
        montage_card_json: текущая карта (после Scriptwriter), JSON-строка.
        geometry_errors:   подмножество errors[] из Validator с кодами
                           вида 'block_N_shot_M_missing_geometry'.
    """
    errors_json = json.dumps(geometry_errors, ensure_ascii=False, indent=2)
    return f"""Монтажная карта (JSON):
{montage_card_json}

Ошибки типа missing_geometry от Чекера (нужно добавить shot.geometry):
{errors_json}

Добавь `geometry` к указанным шотам по правилам выше. Верни полную
карту в JSON.
"""


def _format_show_context(show_context: Optional[dict]) -> str:
    """Форматирует контекст сериала для подмешивания в user-prompt
    каждого агента.

    show_context структура:
        {
            "bible": "<полный текст Bible сериала или ''>",
            "episodes_summary": [
                {"ep_id": "ep1", "title": "...", "scenario_excerpt": "..."},
                {"ep_id": "ep2", ...},
                ...
            ]
        }

    Если show_context пуст — возвращает пустую строку (агент работает
    без контекста сериала, как раньше).
    """
    if not show_context:
        return ""
    parts: list = []
    bible = (show_context.get('bible') or '').strip()
    if bible:
        # Ограничиваем Bible до ~12К символов чтобы не раздувать промпт.
        # Этого хватает для среднего описания сериала с персонажами.
        parts.append("=== БИБЛИЯ СЕРИАЛА (СЮЖЕТ И ПЕРСОНАЖИ) ===")
        if len(bible) > 12000:
            parts.append(bible[:12000] + "\n[…Bible сокращён, "
                          "передано первые 12K символов]")
        else:
            parts.append(bible)
        parts.append("")
    # 2026-05-13 (v1.0.59): episodes_summary НЕ передаётся агентам.
    # Для монтажки одного эпизода контекст других эпизодов сериала
    # избыточен (~8KB / ~2000 tokens, 65% user-prompt'а).
    # Драматическая структура ep4 не зависит от ep1/ep5/ep15 — она
    # вытекает из текущего сценария + характеров (которые в Bible).
    # Эпизод 4 раньше падал по timeout=600s — Sonnet захлёбывалась
    # на 25KB input'е. Убираем episodes_summary — input сжимается до
    # ~17KB / ~4300 tokens, время Scriptwriter'а ~10мин → ~1-2мин.
    # `_load_show_context` всё ещё собирает episodes_summary в dict
    # (тронуть его — другой рефакторинг), но здесь его игнорируем.
    # Если когда-то понадобится для Context Reviewer'а (проверка
    # сюжетной целостности) — оживить через отдельную функцию-форматер.
    return "\n".join(parts)


def build_scriptwriter_user_prompt(scenario_text: str,
                                    refs_summary: dict,
                                    show_context: Optional[dict] = None) -> str:
    """Формирует user-prompt для Сценариста.

    Args:
        scenario_text: полный текст сценария эпизода.
        refs_summary: dict с залинкованными рефами:
          {
            "locations": [{"slug": "...", "filename": "..."}],
            "objects": [{"slug": "...", "filename": "..."}],
            "characters": [{"slug": "...", "filename": "..."}]
          }
        show_context (опционально): контекст всего сериала (Bible +
          описания других эпизодов). Используется чтобы реплики
          соответствовали характерам и не противоречили другим эпизодам.
    """
    refs_json = json.dumps(refs_summary, ensure_ascii=False, indent=2)
    ctx = _format_show_context(show_context)
    ctx_block = (ctx + "\n") if ctx else ""
    return f"""{ctx_block}Сценарий эпизода (текущий):
\"\"\"
{scenario_text}
\"\"\"

Доступные рефы (используй ТОЛЬКО эти slug'и):
{refs_json}

Составь монтажную карту в JSON. Финальный хронометраж 60–80 секунд —
ЖЁСТКИЕ границы (не «допустимо 50-90», не «мягкий ориентир»). Карта
должна укладываться в 60–80 включительно. КАЖДЫЙ значимый драматический
beat = ОТДЕЛЬНЫЙ блок (см. COMMON_RULES «РАЗБИВКА НА БЛОКИ»). Целевое
количество блоков 5-6.
Если есть Bible сериала — учитывай характер персонажа при выборе
тона реплик.
"""


def build_validator_user_prompt(montage_card_json: str,
                                  refs_summary: dict,
                                  show_context: Optional[dict] = None) -> str:
    """User-prompt для Чекера. Подаёт ему карту + список доступных
    рефов + (опционально) контекст сериала для проверки соответствия
    характеров реплик Bible'и.
    """
    refs_json = json.dumps(refs_summary, ensure_ascii=False, indent=2)
    ctx = _format_show_context(show_context)
    ctx_block = (ctx + "\n") if ctx else ""
    return f"""{ctx_block}Доступные рефы:
{refs_json}

Монтажная карта (JSON):
{montage_card_json}

Проверь карту по всем правилам. Верни JSON с полями `ok`, `errors`,
`report`.
"""


def build_editor_user_prompt(montage_card_json: str,
                              errors: list,
                              refs_summary: dict,
                              original_scenario: Optional[str] = None,
                              show_context: Optional[dict] = None) -> str:
    """User-prompt для Редактора.

    Args:
        montage_card_json: текущая (некорректная) карта.
        errors: список ошибок от Чекера.
        refs_summary: доступные рефы.
        original_scenario: текст исходного сценария — нужен Редактору
          чтобы знать «что было разрешено» и не сочинять новые сцены.
        show_context: Bible + другие эпизоды (для удлинения реплик с
          учётом характеров).
    """
    errors_json = json.dumps(errors, ensure_ascii=False, indent=2)
    refs_json = json.dumps(refs_summary, ensure_ascii=False, indent=2)
    ctx = _format_show_context(show_context)
    ctx_block = (ctx + "\n") if ctx else ""
    scen_block = ""
    if original_scenario:
        scen_block = (
            "Оригинальный сценарий эпизода (НЕ выходи за его рамки, "
            "удлиняй реплики, не сочиняй сцены):\n"
            "\"\"\"\n"
            f"{original_scenario}\n"
            "\"\"\"\n\n"
        )
    return f"""{ctx_block}{scen_block}Доступные рефы:
{refs_json}

Текущая монтажная карта:
{montage_card_json}

Ошибки от Чекера которые нужно устранить:
{errors_json}

Перепиши карту с устранением всех ошибок. Верни JSON в том же формате.
Помни: при нехватке секунд УДЛИНЯЙ существующие реплики паузами и
междометиями, не сочиняй новые сцены.
"""


def build_context_reviewer_user_prompt(montage_card_json: str,
                                         original_scenario: str,
                                         show_context: Optional[dict] = None) -> str:
    """User-prompt для Context Reviewer (финальный супер-редактор).
    Получает финальную карту + оригинальный сценарий + Bible — проверяет
    логичность реплик в общем контексте сериала.
    """
    ctx = _format_show_context(show_context)
    ctx_block = (ctx + "\n") if ctx else ""
    return f"""{ctx_block}Оригинальный сценарий эпизода:
\"\"\"
{original_scenario}
\"\"\"

Утверждённая Чекером монтажная карта:
{montage_card_json}

Проверь карту на соответствие Bible'и и другим эпизодам сериала.
Сосредоточься на ДИАЛОГАХ: характеры, противоречия с другими сериями,
правдоподобность удлинений. Верни JSON `{{"ok": ..., "concerns": [...]}}`.
Если карта чистая (нет проблем) — `ok: true, concerns: []`.
"""
