#!/usr/bin/env python3
"""
Юнит-тесты для scenario_parser.py.

Запуск:
    python3 tests/test_scenario_parser.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scenario_parser as sp  # noqa: E402


_passed = 0
_failed = 0


def _check(condition: bool, label: str) -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        _failed += 1
        print(f"  ✗ {label}")


def _check_eq(actual, expected, label: str) -> None:
    if actual == expected:
        _check(True, label)
    else:
        _check(False, f"{label}\n     ожидалось: {expected!r}\n     получено:  {actual!r}")


# ─── Тесты parse_episodes_doc ───────────────────────────────────────────

def test_basic_bible_plus_episodes() -> None:
    print("\n=== библия + 2 эпизода (русский) ===")
    text = """БИБЛИЯ СЕРИАЛА
Главный герой — Дэвид. Его жена Лора плетёт интриги.

ЭПИЗОД 1: ВСТРЕЧА У СТЕКЛА
Тюрьма, Лора видит мужа.
Сцена 1: ...

ЭПИЗОД 2: ХОЛОДНЫЙ ПРИГОВОР
Виктор советует Дэвиду.
Сцена 1: ..."""
    p = sp.parse_episodes_doc(text)
    _check("Главный герой — Дэвид" in p.bible, "библия содержит описание персонажей")
    _check("ЭПИЗОД 1" not in p.bible, "библия НЕ содержит первый эпизод")
    _check_eq(len(p.episodes), 2, "распознано 2 эпизода")
    _check_eq(p.episodes[0].ep_num, 1, "ep_num первого = 1")
    _check_eq(p.episodes[0].title, "ВСТРЕЧА У СТЕКЛА", "title первого извлечён")
    _check("Тюрьма, Лора" in p.episodes[0].content, "контент первого включает текст серии")
    _check_eq(p.episodes[1].ep_num, 2, "ep_num второго = 2")
    _check_eq(p.episodes[1].title, "ХОЛОДНЫЙ ПРИГОВОР", "title второго извлечён")


def test_no_bible_only_episodes() -> None:
    print("\n=== без библии (сразу с ЭПИЗОД 1) ===")
    text = """ЭПИЗОД 1: НАЧАЛО
Текст.

ЭПИЗОД 2: ПРОДОЛЖЕНИЕ
Текст."""
    p = sp.parse_episodes_doc(text)
    _check_eq(p.bible, "", "библия пустая")
    _check_eq(len(p.episodes), 2, "2 эпизода")


def test_only_bible_no_episodes() -> None:
    print("\n=== только библия (без эпизодов) ===")
    text = """БИБЛИЯ
Некоторое описание мира без серий внутри."""
    p = sp.parse_episodes_doc(text)
    _check("описание мира" in p.bible, "вся подача в библию")
    _check_eq(len(p.episodes), 0, "0 эпизодов")


def test_multilang_keywords() -> None:
    print("\n=== украинский / английский маркеры ===")
    text = """Загальний опис

ЕПІЗОД 1: ПОЧАТОК
Зміст.

EPISODE 2: MIDDLE
Content.

СЕРИЯ 3: КОНЕЦ
Текст."""
    p = sp.parse_episodes_doc(text)
    _check_eq(len(p.episodes), 3, "распознано 3 эпизода (uk/en/ru)")
    _check_eq([e.ep_num for e in p.episodes], [1, 2, 3], "номера 1, 2, 3")


def test_marker_separators() -> None:
    print("\n=== разные разделители ':', '.', '—', '-' ===")
    text = """ЭПИЗОД 1: Двоеточие
A
ЭПИЗОД 2. Точка
B
ЭПИЗОД 3 — Тире
C
ЭПИЗОД 4 - Дефис
D
ЭПИЗОД 5 Без знака
E"""
    p = sp.parse_episodes_doc(text)
    _check_eq(len(p.episodes), 5, "распознано 5 вариантов разделителей")


def test_case_insensitive() -> None:
    print("\n=== регистр маркера не важен ===")
    text = """эпизод 1: малый регистр
текст

Эпизод 2: смешанный
текст"""
    p = sp.parse_episodes_doc(text)
    _check_eq(len(p.episodes), 2, "обе серии найдены")


def test_empty_text() -> None:
    print("\n=== пустой / None text ===")
    p = sp.parse_episodes_doc("")
    _check_eq(p.bible, "", "пустой текст → пустая библия")
    _check_eq(len(p.episodes), 0, "пустой текст → 0 эпизодов")


def test_preserves_episode_content() -> None:
    print("\n=== контент эпизода сохраняется целиком ===")
    text = """Библия.

ЭПИЗОД 1: ТЕСТ
Строка 1
Строка 2

Сцена с пустой строкой выше.

ЭПИЗОД 2: СЛЕДУЮЩИЙ"""
    p = sp.parse_episodes_doc(text)
    ep1 = p.episodes[0].content
    _check("Строка 1" in ep1, "первая строка контента сохранена")
    _check("Строка 2" in ep1, "вторая строка контента сохранена")
    _check("Сцена с пустой" in ep1, "текст после пустой строки сохранён")
    _check("ЭПИЗОД 2" not in ep1, "контент первого НЕ включает второй")


# ─── Тесты save_parsed_doc ──────────────────────────────────────────────

def test_save_creates_files() -> None:
    print("\n=== save_parsed_doc создаёт файлы ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        slug = "test"
        (root / "shows" / slug / "scenarios").mkdir(parents=True)

        parsed = sp.ParsedDoc(
            bible="Это библия сериала.",
            episodes=[
                sp.ParsedEpisode(ep_num=1, title="Один", content="ЭПИЗОД 1: Один\nТекст 1"),
                sp.ParsedEpisode(ep_num=2, title="Два", content="ЭПИЗОД 2: Два\nТекст 2"),
            ],
        )
        summary = sp.save_parsed_doc(root, slug, parsed)

        _check_eq(summary['bible_saved'], True, "summary.bible_saved=True")
        _check_eq(summary['episodes_saved'], 2, "summary.episodes_saved=2")
        _check_eq(summary['episode_files'], ['ep01.txt', 'ep02.txt'],
                  "имена файлов с zero-pad")

        bible_text = (root / "shows" / slug / "bible.txt").read_text(encoding="utf-8")
        _check("Это библия" in bible_text, "bible.txt содержит библию")

        ep1 = (root / "shows" / slug / "scenarios" / "ep01.txt").read_text(encoding="utf-8")
        _check("Текст 1" in ep1, "ep01.txt содержит текст серии")


def test_save_no_bible() -> None:
    print("\n=== save без библии (пустая) ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        slug = "test"
        (root / "shows" / slug / "scenarios").mkdir(parents=True)

        parsed = sp.ParsedDoc(
            bible="",
            episodes=[sp.ParsedEpisode(ep_num=5, title="X", content="ЭПИЗОД 5: X\n.")],
        )
        summary = sp.save_parsed_doc(root, slug, parsed)

        _check_eq(summary['bible_saved'], False, "bible_saved=False")
        _check(not (root / "shows" / slug / "bible.txt").exists(),
               "bible.txt НЕ создан при пустой библии")
        _check((root / "shows" / slug / "scenarios" / "ep05.txt").exists(),
               "ep05.txt создан с правильным zero-pad")


def test_save_overwrites_existing() -> None:
    print("\n=== save перезаписывает существующие файлы ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        slug = "test"
        (root / "shows" / slug / "scenarios").mkdir(parents=True)
        (root / "shows" / slug / "bible.txt").write_text("СТАРАЯ", encoding="utf-8")

        parsed = sp.ParsedDoc(bible="НОВАЯ", episodes=[])
        sp.save_parsed_doc(root, slug, parsed)

        bible = (root / "shows" / slug / "bible.txt").read_text(encoding="utf-8")
        _check("НОВАЯ" in bible, "bible.txt перезаписан новым содержимым")
        _check("СТАРАЯ" not in bible, "старое содержимое не осталось")


def test_save_missing_show_raises() -> None:
    print("\n=== save в несуществующий сериал → FileNotFoundError ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        try:
            sp.save_parsed_doc(root, "missing", sp.ParsedDoc(bible="x"))
            _check(False, "ожидался FileNotFoundError")
        except FileNotFoundError:
            _check(True, "FileNotFoundError выбрасывается корректно")


# ─── Запуск ──────────────────────────────────────────────────────────────

def main() -> int:
    print("Тестирую scenario_parser.py")
    test_basic_bible_plus_episodes()
    test_no_bible_only_episodes()
    test_only_bible_no_episodes()
    test_multilang_keywords()
    test_marker_separators()
    test_case_insensitive()
    test_empty_text()
    test_preserves_episode_content()
    test_save_creates_files()
    test_save_no_bible()
    test_save_overwrites_existing()
    test_save_missing_show_raises()

    total = _passed + _failed
    print(f"\n{'─' * 50}")
    print(f"Итого: {_passed}/{total} прошло, {_failed} упало")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
