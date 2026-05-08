# -*- coding: utf-8 -*-
"""
show_manager.py — управление сериалами Storyboard Studio.

Отвечает за создание новых сериалов, транслитерацию названия в slug,
чтение/запись метаданных сериала (display_name).

Чистый Python без Qt — легко тестируется юнитами.

Архитектура:
    - shows/<slug>/meta.json — метаданные сериала (display_name, slug, created_at)
    - имя папки = slug (латиница, snake_case) — нужно для путей и кросс-OS
    - display_name — то что юзер видит в UI (любой язык)
    - current_show.json — активный сериал (управляется в storyboard_app.py)

Триггер: юзер хочет создавать сериалы с любым названием на любом языке,
а папка должна быть всегда на латинице. См. Долг 1 в _session_log.md.

История: создан 2026-05-05.
Долг 4 в _session_log.md — функция delete_show будет добавлена позже.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, Optional, Set


# ─── Транслитерация ──────────────────────────────────────────────────────

# Таблица кириллица → латиница (русский + украинский).
# Согласно ГОСТ 7.79-2000 система Б, с украинскими дополнениями.
_TRANSLIT_MAP: Dict[str, str] = {
    # Русский
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    # Украинские дополнения
    'і': 'i', 'ї': 'yi', 'є': 'ye', 'ґ': 'g',
    # Белорусские дополнения (на всякий случай)
    'ў': 'u',
}


def transliterate(text: str) -> str:
    """Кириллица → латиница по ГОСТ 7.79-2000 (система Б).

    Поддерживает русский, украинский, белорусский. Латинские символы
    остаются как есть. Цифры и пробелы не трогаются.

    >>> transliterate('Последний план')
    'Posledniy plan'
    >>> transliterate('Останній план')
    'Ostanniy plan'
    >>> transliterate('The Last Plan')
    'The Last Plan'
    """
    out = []
    for ch in text:
        lower = ch.lower()
        if lower in _TRANSLIT_MAP:
            replacement = _TRANSLIT_MAP[lower]
            # Сохраняем регистр первой буквы
            if ch.isupper() and replacement:
                replacement = replacement[0].upper() + replacement[1:]
            out.append(replacement)
        else:
            out.append(ch)
    return ''.join(out)


def make_slug(display_name: str, taken: Optional[Set[str]] = None) -> str:
    """Превращает любое название в безопасный slug (латиница, snake_case).

    Шаги:
      1. Транслит кириллицы в латиницу.
      2. Lowercase.
      3. Все не-[a-z0-9] заменяются на `_`.
      4. Подряд идущие `_` сжимаются в один.
      5. Обрезаются `_` с краёв.
      6. Если slug пустой (например, ввели только эмодзи) → `show`.
      7. Если slug в taken — добавляется `_2`, `_3`, ... до уникального.

    >>> make_slug('Последний план')
    'posledniy_plan'
    >>> make_slug('The Last Plan!')
    'the_last_plan'
    >>> make_slug('  ', taken=set())
    'show'
    >>> make_slug('test', taken={'test', 'test_2'})
    'test_3'
    """
    slug = transliterate(display_name).lower()
    slug = re.sub(r'[^a-z0-9]+', '_', slug).strip('_')
    if not slug:
        slug = 'show'
    if taken and slug in taken:
        i = 2
        while f"{slug}_{i}" in taken:
            i += 1
        slug = f"{slug}_{i}"
    return slug


# ─── Файловые операции ────────────────────────────────────────────────────

def _shows_dir(project_root: Path) -> Path:
    return project_root / "shows"


def list_show_slugs(project_root: Path) -> Set[str]:
    """Множество slug'ов всех существующих сериалов."""
    sd = _shows_dir(project_root)
    if not sd.exists():
        return set()
    return {p.name for p in sd.iterdir() if p.is_dir() and not p.name.startswith(".")}


def create_show(project_root: Path, display_name: str) -> str:
    """Создаёт новый сериал. Возвращает slug.

    Шаги:
      1. Транслит название → slug, разрешение коллизий с существующими.
      2. Создаются все нужные подпапки структуры.
      3. Записывается meta.json с display_name + slug + created_at.
      4. Записывается пустой episodes.json = {}.

    Сам сериал НЕ становится активным автоматически — это решает caller
    (через storyboard_app.set_current_show). Так модуль остаётся чистым
    от знаний о current_show.json.

    Raises:
        ValueError: если display_name пустой или состоит только из пробелов.
    """
    name = (display_name or "").strip()
    if not name:
        raise ValueError("Название сериала не может быть пустым")

    taken = list_show_slugs(project_root)
    slug = make_slug(name, taken=taken)

    show_root = _shows_dir(project_root) / slug
    # Структура папок — соответствует тому что используют другие части кода
    # (см. storyboard_app.setup_paths_for_show, CLAUDE.md). Если что-то
    # появится новое — добавляй сюда.
    for sub in (
        "refs/locations",
        "refs/objects",
        "refs/characters",
        "output/prompts",
        "output/storyboards",
        "scenarios",
        "chats",
    ):
        (show_root / sub).mkdir(parents=True, exist_ok=True)

    # episodes.json — пустой объект
    (show_root / "episodes.json").write_text("{}\n", encoding="utf-8")

    # meta.json — отображаемое имя + слаг + время создания
    save_show_meta(project_root, slug, {
        "display_name": name,
        "slug": slug,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    return slug


def save_show_meta(project_root: Path, slug: str, meta: Dict) -> None:
    """Сохраняет meta.json в shows/<slug>/."""
    f = _shows_dir(project_root) / slug / "meta.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_show_meta(project_root: Path, slug: str) -> Dict:
    """Читает meta.json. Возвращает {} если файла нет или сломан JSON.

    Caller использует .get('display_name') с фоллбэком на slug —
    см. display_name_for() ниже.
    """
    if not slug:
        return {}
    f = _shows_dir(project_root) / slug / "meta.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def display_name_for(project_root: Path, slug: str) -> str:
    """Отображаемое имя сериала. Если в meta.json есть display_name — оно.
    Иначе fallback: slug в title-case с пробелами вместо `_`.

    >>> display_name_for(root_with_meta, 'the_last_plan')   # из meta
    'Последний план'
    >>> display_name_for(root_no_meta, 'the_last_plan')      # fallback
    'The Last Plan'
    """
    meta = load_show_meta(project_root, slug)
    name = meta.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    # Fallback: 'the_last_plan' → 'The Last Plan'
    return slug.replace("_", " ").title() if slug else ""
