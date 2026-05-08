#!/usr/bin/env python3
"""
Юнит-тесты для show_manager.py.

Запуск:
    python3 tests/test_show_manager.py

Ничего не зависит от Qt — быстро, можно гонять часто.
Использует tempfile для изоляции файловых операций.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Чтобы импортировать show_manager при запуске из tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

import show_manager as sm  # noqa: E402


# ─── Утилиты ─────────────────────────────────────────────────────────────

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
        _check(False, f"{label}: ожидалось {expected!r}, получено {actual!r}")


# ─── Тесты транслитерации ────────────────────────────────────────────────

def test_transliterate() -> None:
    print("\n=== transliterate ===")
    _check_eq(sm.transliterate("Последний план"), "Posledniy plan",
              "русский: 'Последний план' → 'Posledniy plan'")
    _check_eq(sm.transliterate("Останній план"), "Ostanniy plan",
              "украинский: 'Останній план' → 'Ostanniy plan'")
    _check_eq(sm.transliterate("The Last Plan"), "The Last Plan",
              "английский: латиница не трогается")
    _check_eq(sm.transliterate("Привет, мир!"), "Privet, mir!",
              "знаки препинания сохраняются")
    _check_eq(sm.transliterate(""), "", "пустая строка → пустая")
    _check_eq(sm.transliterate("Ёлка"), "Yolka",
              "ё в начале слова: регистр сохраняется")
    # Многобуквенные транслиты (ш→sh, ё→yo) capitalize только первую букву —
    # это упрощение, в slug'е всё равно лowercased. Полный CAPS не требуется.
    _check_eq(sm.transliterate("ЩЁТКА"), "ShchYoTKA",
              "капс: первая буква транслита заглавная, остальные lowercase")
    _check_eq(sm.transliterate("Серия 21"), "Seriya 21",
              "цифры сохраняются")


# ─── Тесты make_slug ─────────────────────────────────────────────────────

def test_make_slug() -> None:
    print("\n=== make_slug ===")
    _check_eq(sm.make_slug("Последний план"), "posledniy_plan",
              "русский → snake_case латиница")
    _check_eq(sm.make_slug("The Last Plan!"), "the_last_plan",
              "знаки препинания убираются")
    _check_eq(sm.make_slug("  Test  "), "test",
              "обрезка пробелов с краёв")
    _check_eq(sm.make_slug("a---b___c"), "a_b_c",
              "сжатие подряд идущих разделителей")
    _check_eq(sm.make_slug(""), "show", "пустая строка → 'show'")
    _check_eq(sm.make_slug("   "), "show",
              "только пробелы → 'show'")
    _check_eq(sm.make_slug("🎬🎥"), "show",
              "только эмодзи → 'show'")
    _check_eq(sm.make_slug("test", taken={"test"}), "test_2",
              "коллизия → суффикс _2")
    _check_eq(sm.make_slug("test", taken={"test", "test_2", "test_3"}),
              "test_4",
              "несколько коллизий → следующий доступный _N")
    _check_eq(sm.make_slug("test", taken={"other"}), "test",
              "нет коллизии → без суффикса")


# ─── Тесты файловых операций ─────────────────────────────────────────────

def test_create_show() -> None:
    print("\n=== create_show ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 1. Создание первого сериала
        slug = sm.create_show(root, "Последний план")
        _check_eq(slug, "posledniy_plan", "slug сгенерирован транслитом")

        show_root = root / "shows" / slug
        _check(show_root.exists(), "папка сериала создана")

        for sub in ("refs/locations", "refs/objects", "refs/characters",
                    "output/prompts", "output/storyboards", "scenarios", "chats"):
            _check((show_root / sub).is_dir(),
                   f"подпапка {sub} создана")

        # episodes.json пустой
        ep_file = show_root / "episodes.json"
        _check(ep_file.exists(), "episodes.json создан")
        _check_eq(json.loads(ep_file.read_text(encoding="utf-8")), {},
                  "episodes.json = {}")

        # meta.json содержит display_name + slug
        meta_file = show_root / "meta.json"
        _check(meta_file.exists(), "meta.json создан")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        _check_eq(meta.get("display_name"), "Последний план",
                  "meta.display_name = 'Последний план'")
        _check_eq(meta.get("slug"), "posledniy_plan",
                  "meta.slug = 'posledniy_plan'")
        _check("created_at" in meta, "meta.created_at есть")

        # 2. Коллизия — второй сериал с тем же названием → suffix _2
        slug2 = sm.create_show(root, "Последний план")
        _check_eq(slug2, "posledniy_plan_2",
                  "коллизия → автосуффикс _2")
        _check((root / "shows" / "posledniy_plan_2").exists(),
               "папка posledniy_plan_2 создана")

        # 3. Пустое имя → ValueError
        try:
            sm.create_show(root, "")
            _check(False, "пустое имя должно бросать ValueError")
        except ValueError:
            _check(True, "пустое имя → ValueError")

        try:
            sm.create_show(root, "   ")
            _check(False, "пробелы должны бросать ValueError")
        except ValueError:
            _check(True, "только пробелы → ValueError")


def test_load_show_meta_and_display_name() -> None:
    print("\n=== load_show_meta + display_name_for ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # 1. С meta.json
        slug = sm.create_show(root, "Последний план")
        _check_eq(sm.display_name_for(root, slug), "Последний план",
                  "display_name_for читает meta.display_name")

        # 2. Без meta.json (legacy сериал) — fallback на title-case slug
        legacy_slug = "the_last_plan"
        (root / "shows" / legacy_slug).mkdir(parents=True)
        # meta.json НЕ создаём
        _check_eq(sm.display_name_for(root, legacy_slug), "The Last Plan",
                  "legacy без meta.json → title-case slug")

        # 3. meta.json с пустым display_name → fallback
        broken_slug = "broken"
        (root / "shows" / broken_slug).mkdir(parents=True)
        sm.save_show_meta(root, broken_slug, {"display_name": "  "})
        _check_eq(sm.display_name_for(root, broken_slug), "Broken",
                  "пустой display_name в meta → fallback на slug")

        # 4. Сломанный JSON
        weird_slug = "weird"
        (root / "shows" / weird_slug).mkdir(parents=True)
        (root / "shows" / weird_slug / "meta.json").write_text("not json{",
                                                                encoding="utf-8")
        _check_eq(sm.load_show_meta(root, weird_slug), {},
                  "сломанный JSON → load_show_meta возвращает {}")
        _check_eq(sm.display_name_for(root, weird_slug), "Weird",
                  "сломанный JSON → fallback")

        # 5. Несуществующий slug
        _check_eq(sm.load_show_meta(root, "nonexistent"), {},
                  "несуществующий slug → {}")


def test_list_show_slugs() -> None:
    print("\n=== list_show_slugs ===")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _check_eq(sm.list_show_slugs(root), set(),
                  "shows/ не существует → пустой set")

        sm.create_show(root, "Один")
        sm.create_show(root, "Два")
        sm.create_show(root, "Три")

        slugs = sm.list_show_slugs(root)
        _check_eq(slugs, {"odin", "dva", "tri"},
                  "три сериала → три slug'а")

        # .DS_Store или скрытые файлы не должны попадать
        (root / "shows" / ".hidden").mkdir()
        slugs2 = sm.list_show_slugs(root)
        _check(".hidden" not in slugs2, "скрытые папки исключаются")


# ─── Запуск ──────────────────────────────────────────────────────────────

def main() -> int:
    print("Тестирую show_manager.py")
    test_transliterate()
    test_make_slug()
    test_create_show()
    test_load_show_meta_and_display_name()
    test_list_show_slugs()

    total = _passed + _failed
    print(f"\n{'─' * 50}")
    print(f"Итого: {_passed}/{total} прошло, {_failed} упало")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
