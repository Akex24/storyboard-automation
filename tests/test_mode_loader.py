#!/usr/bin/env python3
"""
Юнит-тесты для agents/mode_loader.py.

Запуск:
    python3 tests/test_mode_loader.py

Тесты get_current_mode / set_current_mode требуют QCoreApplication
(QSettings полагается на QApplication/QCoreApplication для resolve
организации/имени приложения в plist/registry). QCoreApplication
создаётся в начале test runner'а без event loop'а — этого достаточно
для QSettings API.

После прогона тестов значение QSettings 'montage_mode' восстанавливается
к исходному, чтобы юзер не потерял свой выбор после запуска тестов.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# QCoreApplication — для работы QSettings без GUI.
from PyQt6.QtCore import QCoreApplication, QSettings  # noqa: E402

_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

from agents import mode_loader as ml  # noqa: E402


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


# ─── Чистые тесты (не трогают QSettings) ────────────────────────────────

def test_constants():
    print("\n=== константы модуля ===")
    _check_eq(ml.DEFAULT_MODE, 'a', "DEFAULT_MODE == 'a'")
    _check_eq(ml.QS_KEY, 'montage_mode', "QS_KEY == 'montage_mode'")
    _check_eq(ml.APP_ORG, 'StoryboardStudio', "APP_ORG sync с storyboard_app")
    _check_eq(ml.APP_NAME, 'StoryboardApp', "APP_NAME sync с storyboard_app")
    _check_eq(set(ml.VALID_MODES), {'a', 'b', 'c', 'd'},
              "VALID_MODES = {a,b,c,d}")


def test_get_suffix_for_mode():
    print("\n=== get_suffix_for_mode ===")
    _check_eq(ml.get_suffix_for_mode('a'), '', "A → ''")
    _check_eq(ml.get_suffix_for_mode('b'), '_b', "B → '_b'")
    _check_eq(ml.get_suffix_for_mode('c'), '_c', "C → '_c'")
    _check_eq(ml.get_suffix_for_mode('d'), '_d', "D → '_d'")
    # edge cases
    _check_eq(ml.get_suffix_for_mode('A'), '',
              "uppercase A → '' (case-insensitive)")
    _check_eq(ml.get_suffix_for_mode('B'), '_b',
              "uppercase B → '_b' (case-insensitive)")
    _check_eq(ml.get_suffix_for_mode('x'), '',
              "невалидный 'x' → '' (фолбэк A)")
    _check_eq(ml.get_suffix_for_mode(''), '',
              "пустая строка → ''")


def test_get_instruction_filename_for_all_modes():
    print("\n=== get_instruction_filename для всех 4 режимов ===")
    # Сохраняем текущий режим и подставляем по очереди каждый.
    s = QSettings(ml.APP_ORG, ml.APP_NAME)
    saved = s.value(ml.QS_KEY, ml.DEFAULT_MODE)
    try:
        cases = [
            ('a', 'ГЛАВНАЯ_ИНСТРУКЦИЯ.md'),
            ('b', 'ГЛАВНАЯ_ИНСТРУКЦИЯ_b.md'),
            ('c', 'ГЛАВНАЯ_ИНСТРУКЦИЯ_c.md'),
            ('d', 'ГЛАВНАЯ_ИНСТРУКЦИЯ_d.md'),
        ]
        for mode, expected in cases:
            s.setValue(ml.QS_KEY, mode)
            s.sync()
            _check_eq(ml.get_instruction_filename(), expected,
                      f"режим {mode!r} → {expected!r}")
    finally:
        s.setValue(ml.QS_KEY, saved)
        s.sync()


# ─── Тесты с QSettings (write/read round-trip) ──────────────────────────

def test_set_get_round_trip():
    print("\n=== set_current_mode / get_current_mode round-trip ===")
    s = QSettings(ml.APP_ORG, ml.APP_NAME)
    saved = s.value(ml.QS_KEY, ml.DEFAULT_MODE)
    try:
        for mode in ml.VALID_MODES:
            ml.set_current_mode(mode)
            _check_eq(ml.get_current_mode(), mode,
                      f"set('{mode}') → get == '{mode}'")
    finally:
        s.setValue(ml.QS_KEY, saved)
        s.sync()


def test_set_invalid_falls_back_to_default():
    print("\n=== set_current_mode валидация: 'x' → DEFAULT_MODE ===")
    s = QSettings(ml.APP_ORG, ml.APP_NAME)
    saved = s.value(ml.QS_KEY, ml.DEFAULT_MODE)
    try:
        ml.set_current_mode('x')
        _check_eq(ml.get_current_mode(), ml.DEFAULT_MODE,
                  "невалидное 'x' → сохранён DEFAULT_MODE ('a')")
        ml.set_current_mode('')
        _check_eq(ml.get_current_mode(), ml.DEFAULT_MODE,
                  "пустая строка → DEFAULT_MODE")
        ml.set_current_mode('BANANA')
        _check_eq(ml.get_current_mode(), ml.DEFAULT_MODE,
                  "'BANANA' → DEFAULT_MODE")
    finally:
        s.setValue(ml.QS_KEY, saved)
        s.sync()


def test_get_current_mode_default_when_unset():
    print("\n=== get_current_mode default когда ключ удалён ===")
    s = QSettings(ml.APP_ORG, ml.APP_NAME)
    saved = s.value(ml.QS_KEY, ml.DEFAULT_MODE)
    try:
        s.remove(ml.QS_KEY)
        s.sync()
        _check_eq(ml.get_current_mode(), ml.DEFAULT_MODE,
                  "нет ключа в QSettings → DEFAULT_MODE")
    finally:
        s.setValue(ml.QS_KEY, saved)
        s.sync()


# ─── Runner ──────────────────────────────────────────────────────────────

def main() -> int:
    print("=== mode_loader tests ===")
    test_constants()
    test_get_suffix_for_mode()
    test_get_instruction_filename_for_all_modes()
    test_set_get_round_trip()
    test_set_invalid_falls_back_to_default()
    test_get_current_mode_default_when_unset()
    print(f"\n=== итог: {_passed} прошли, {_failed} упали ===")
    return 0 if _failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
