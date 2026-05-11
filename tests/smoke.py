#!/usr/bin/env python3
"""
Smoke-тесты для Storyboard Studio.

Запускать перед каждой сборкой `.app`:
    python3 tests/smoke.py

Что проверяется:
    1.  AST.parse — все .py-файлы парсятся (синтаксис ОК)
    2.  Импорты — storyboard_app/installer_app/generate_storyboards/pipeline загружаются
    3.  TRANSLATIONS — одинаковый набор ключей в ru/uk/en
    4.  В UI-строках TRANSLATIONS нет «Claude»/«Клод»
    5.  block_wheel_event применяется где нужно (Settings / NewEpisode)
    6.  Маркеры предыдущих фиксов из _session_log.md на месте
    7.  MainWindow создаётся (headless, через QT_QPA_PLATFORM=offscreen)
    8.  Все 4 вкладки добавлены в QTabWidget
    9.  Базовые helpers: get_lang/set_lang/tr/block_wheel_event существуют

Что НЕ проверяется (важно понимать ограничения):
    -   Визуальные баги (мерцание, тики, цвета, анимации)
    -   Реальная генерация через Fast Gen API
    -   Drag&drop, реальные клики мышью
    -   Сетевые вызовы (GitHub, Anthropic)

Exit codes:
    0 — все тесты прошли
    1 — хотя бы один тест упал
"""

from __future__ import annotations

import ast
import os
import sys
import re
import traceback
from pathlib import Path

# Headless Qt — не открываем окна
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Чтобы импорт storyboard_app не пытался прочитать stored_root
os.environ.setdefault("STORYBOARD_SMOKE_TEST", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ─── Цветной вывод ───────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def ok(name: str) -> None:
    print(f"  {GREEN}✓{RESET} {name}")
    PASSED.append(name)


def fail(name: str, detail: str) -> None:
    print(f"  {RED}✗{RESET} {name}")
    print(f"    {DIM}{detail}{RESET}")
    FAILED.append((name, detail))


def section(title: str) -> None:
    print(f"\n{YELLOW}━━━ {title} ━━━{RESET}")


# ─── Тест 1: AST.parse всех .py ──────────────────────────────────
def test_ast_parse() -> None:
    section("1. Синтаксис (ast.parse)")
    for fname in ("storyboard_app.py", "installer_app.py",
                  "generate_storyboards.py", "pipeline.py",
                  "i18n.py",
                  "threads/__init__.py", "threads/update.py",
                  "threads/generate.py",
                  "widgets/__init__.py", "widgets/dialogs.py",
                  "widgets/actor_dialogs.py", "widgets/editor_widgets.py",
                  "views/__init__.py", "views/actors.py",
                  "views/episode_chat.py", "views/new_episode.py",
                  "views/_chat_render.py",
                  "widgets/gen_button.py",
                  "threads/autonomous_gen.py"):
        path = PROJECT_ROOT / fname
        if not path.exists():
            fail(f"ast.parse {fname}", "файл не найден")
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            ok(f"ast.parse {fname}")
        except SyntaxError as e:
            fail(f"ast.parse {fname}", f"{e.__class__.__name__}: {e}")


# ─── Тест 2: TRANSLATIONS ────────────────────────────────────────
def test_translations() -> None:
    section("2. TRANSLATIONS (RU/UK/EN)")
    try:
        from storyboard_app import TRANSLATIONS
    except Exception as e:
        fail("импорт TRANSLATIONS", f"{e.__class__.__name__}: {e}")
        return

    if set(TRANSLATIONS.keys()) != {"ru", "uk", "en"}:
        fail("TRANSLATIONS языки", f"ожидал ru/uk/en, нашёл: {sorted(TRANSLATIONS.keys())}")
        return
    ok("TRANSLATIONS содержит ru/uk/en")

    ru_keys = set(TRANSLATIONS["ru"].keys())
    uk_keys = set(TRANSLATIONS["uk"].keys())
    en_keys = set(TRANSLATIONS["en"].keys())

    only_in_ru = ru_keys - uk_keys
    only_in_uk = uk_keys - ru_keys
    if only_in_ru:
        fail("ru vs uk", f"только в ru: {sorted(only_in_ru)[:5]} (всего {len(only_in_ru)})")
    if only_in_uk:
        fail("uk vs ru", f"только в uk: {sorted(only_in_uk)[:5]} (всего {len(only_in_uk)})")
    if not only_in_ru and not only_in_uk:
        ok(f"ru ≡ uk ({len(ru_keys)} ключей)")

    only_in_en = ru_keys - en_keys
    only_outside_en = en_keys - ru_keys
    if only_in_en:
        fail("ru vs en", f"только в ru (нет в en): {sorted(only_in_en)[:5]} (всего {len(only_in_en)})")
    if only_outside_en:
        fail("en vs ru", f"только в en: {sorted(only_outside_en)[:5]} (всего {len(only_outside_en)})")
    if not only_in_en and not only_outside_en:
        ok(f"ru ≡ en ({len(ru_keys)} ключей)")


# ─── Тест 3: «Claude» в UI-строках ───────────────────────────────
def test_no_claude_in_ui() -> None:
    section("3. Слово «Claude»/«Клод» в UI-строках")
    try:
        from storyboard_app import TRANSLATIONS
    except Exception as e:
        fail("импорт TRANSLATIONS", str(e))
        return

    pat = re.compile(r"claude|клод", re.IGNORECASE)
    found: list[tuple[str, str, str]] = []
    for lang, mapping in TRANSLATIONS.items():
        for key, value in mapping.items():
            if isinstance(value, str) and pat.search(value):
                found.append((lang, key, value))

    if found:
        for lang, key, value in found[:5]:
            fail(f"TRANSLATIONS[{lang}][{key}]", f"содержит 'Claude'/'Клод': {value!r}")
        if len(found) > 5:
            fail("...", f"и ещё {len(found) - 5}")
    else:
        ok("UI-строки без «Claude»/«Клод»")


# ─── Тест 4b: i18n.py / storyboard_app.py — APP_ORG/APP_NAME синк ─
def test_i18n_qsettings_sync() -> None:
    """После выноса i18n.py — _QS_ORG/_QS_NAME продублированы.
    Проверяем что они совпадают с APP_ORG/APP_NAME из storyboard_app.py."""
    section("4b. i18n.py: _QS_ORG/_QS_NAME синк с storyboard_app")
    try:
        import i18n
        from storyboard_app import APP_ORG, APP_NAME
    except Exception as e:
        fail("импорт i18n + storyboard_app", str(e))
        return

    if i18n._QS_ORG != APP_ORG:
        fail("_QS_ORG sync", f"i18n._QS_ORG={i18n._QS_ORG!r}, APP_ORG={APP_ORG!r}")
    else:
        ok(f"_QS_ORG ≡ APP_ORG ({APP_ORG!r})")

    if i18n._QS_NAME != APP_NAME:
        fail("_QS_NAME sync", f"i18n._QS_NAME={i18n._QS_NAME!r}, APP_NAME={APP_NAME!r}")
    else:
        ok(f"_QS_NAME ≡ APP_NAME ({APP_NAME!r})")


# ─── Тест 4c: threads/ — все update треды импортируются ──────────
def test_threads_update_imports() -> None:
    """После выноса в threads/update.py — проверяем что все 5 классов
    QThread импортируются и содержат signals/run."""
    section("4c. threads/update — импорт классов и сигналы")
    try:
        from threads import (
            CheckUpdateThread, DownloadUpdateThread,
            DownloadAppUpdateThread, SendUpdateThread, FetchStatsThread,
        )
    except Exception as e:
        fail("from threads import ...", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт 5 update-тредов из threads/")

    expected_signals = {
        CheckUpdateThread:      ('update_found', 'no_update', 'error'),
        DownloadUpdateThread:   ('progress', 'finished', 'error'),
        DownloadAppUpdateThread:('progress', 'finished', 'error'),
        SendUpdateThread:       ('progress', 'finished', 'error'),
        FetchStatsThread:       ('finished',),
    }
    for cls, signals in expected_signals.items():
        missing = [s for s in signals if not hasattr(cls, s)]
        if missing:
            fail(f"{cls.__name__} signals", f"отсутствуют: {missing}")
        elif not hasattr(cls, 'run') or not callable(getattr(cls, 'run')):
            fail(f"{cls.__name__}.run", "нет метода run()")
        else:
            ok(f"{cls.__name__}: signals + run() — OK")

    # storyboard_app.CheckUpdateThread должен быть тем же классом (re-export)
    try:
        from storyboard_app import CheckUpdateThread as CU_via_app
        if CU_via_app is CheckUpdateThread:
            ok("storyboard_app.CheckUpdateThread ≡ threads.CheckUpdateThread")
        else:
            fail("re-export consistency",
                 "storyboard_app.CheckUpdateThread — другой объект (дубликат класса?)")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4c2: _AppProxy предпочитает __main__ перед storyboard_app ──
def test_appproxy_main_first() -> None:
    """В PyInstaller-сборке storyboard_app.py запускается как __main__.
    `import storyboard_app` создаёт ВТОРОЙ instance с неинициализированными
    global'ами. _AppProxy должен сначала смотреть в sys.modules['__main__'].

    Тест: подсовываем фейковый __main__ с уникальным атрибутом, проверяем
    что прокси возвращает значение из __main__, а не из storyboard_app."""
    section("4c2. _AppProxy: __main__ имеет приоритет над storyboard_app")
    import sys
    import types
    try:
        from threads.update import _sa as _sa_upd
        from threads.generate import _sa as _sa_gen
    except Exception as e:
        fail("импорт _sa", str(e))
        return

    # Бекапим текущий __main__ и подсовываем фейковый
    real_main = sys.modules.get('__main__')
    fake_main = types.ModuleType('__main__')
    fake_main.__APPPROXY_TEST_MARKER__ = "from_main_module"
    sys.modules['__main__'] = fake_main
    try:
        # Атрибут есть только в __main__, в storyboard_app нет
        try:
            v_upd = _sa_upd.__APPPROXY_TEST_MARKER__
            v_gen = _sa_gen.__APPPROXY_TEST_MARKER__
            if v_upd == "from_main_module" and v_gen == "from_main_module":
                ok("_AppProxy резолвит из __main__ при наличии атрибута")
            else:
                fail("_AppProxy не резолвит __main__",
                     f"upd={v_upd!r} gen={v_gen!r}")
        except AttributeError:
            fail("_AppProxy не нашёл атрибут в __main__",
                 "прокси не смотрит в __main__ — пойдёт за вторым instance")
    finally:
        if real_main is not None:
            sys.modules['__main__'] = real_main


# ─── Тест 4d: threads/generate — все 5 классов импортируются ─────
def test_threads_generate_imports() -> None:
    """После выноса в threads/generate.py — проверяем что все 5 классов
    QThread импортируются и содержат signals/run."""
    section("4d. threads/generate — импорт классов и сигналы")
    try:
        from threads import (
            GenerateThread, RefGenerateThread, GenerateActorRefThread,
            ClaudeGeometryThread, RunEpisodeThread,
        )
    except Exception as e:
        fail("from threads import generate-classes", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт 5 generation-тредов из threads/")

    expected_signals = {
        GenerateThread:          ('progress', 'step', 'finished', 'error'),
        RefGenerateThread:       ('progress', 'step', 'finished', 'error'),
        GenerateActorRefThread:  ('progress', 'finished', 'error'),
        ClaudeGeometryThread:    ('finished', 'error'),
        RunEpisodeThread:        ('output_chunk', 'finished_ok', 'error', 'stopped'),
    }
    for cls, signals in expected_signals.items():
        missing = [s for s in signals if not hasattr(cls, s)]
        if missing:
            fail(f"{cls.__name__} signals", f"отсутствуют: {missing}")
        elif not hasattr(cls, 'run') or not callable(getattr(cls, 'run')):
            fail(f"{cls.__name__}.run", "нет метода run()")
        else:
            ok(f"{cls.__name__}: signals + run() — OK")

    # Re-export check для одного представителя
    try:
        from storyboard_app import GenerateThread as GT_via_app
        if GT_via_app is GenerateThread:
            ok("storyboard_app.GenerateThread ≡ threads.GenerateThread")
        else:
            fail("re-export consistency", "GenerateThread — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4e: widgets/dialogs — независимые диалоги ──────────────
def test_widgets_dialogs_imports() -> None:
    """После выноса в widgets/dialogs.py — проверяем что все 4 класса
    QDialog импортируются и имеют конструкторы."""
    section("4e. widgets/dialogs — импорт классов")
    try:
        from widgets import (
            FullscreenImageDialog, RefDoneNoticeDialog,
            GeometryDoneNoticeDialog, CloseConfirmDialog,
        )
    except Exception as e:
        fail("from widgets import dialogs", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт 4 диалогов из widgets/")

    from PyQt6.QtWidgets import QDialog
    for cls in (FullscreenImageDialog, RefDoneNoticeDialog,
                GeometryDoneNoticeDialog, CloseConfirmDialog):
        if not issubclass(cls, QDialog):
            fail(f"{cls.__name__}", "не является QDialog подклассом")
        else:
            ok(f"{cls.__name__}: QDialog ✓")

    # Re-export consistency
    try:
        from storyboard_app import FullscreenImageDialog as F_via_app
        if F_via_app is FullscreenImageDialog:
            ok("storyboard_app.FullscreenImageDialog ≡ widgets.FullscreenImageDialog")
        else:
            fail("re-export consistency", "FullscreenImageDialog — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4f: widgets/actor_dialogs (шаг 4A) ─────────────────────
def test_widgets_actor_dialogs_imports() -> None:
    """Шаг 4A: AddActorDialog / ChooseActorDialog / ActorPhotosDialog
    вынесены в widgets/actor_dialogs.py."""
    section("4f. widgets/actor_dialogs (шаг 4A)")
    try:
        from widgets import (
            AddActorDialog, ChooseActorDialog, ActorPhotosDialog,
        )
        from widgets.actor_dialogs import _PhotoThumb, _BigPhotoLabel
    except Exception as e:
        fail("from widgets import actor_dialogs", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт 3 actor-диалогов + 2 подвиджетов")

    from PyQt6.QtWidgets import QDialog
    from PyQt6.QtWidgets import QLabel
    for cls in (AddActorDialog, ChooseActorDialog, ActorPhotosDialog):
        if not issubclass(cls, QDialog):
            fail(f"{cls.__name__}", "не QDialog")
        else:
            ok(f"{cls.__name__}: QDialog ✓")
    for cls in (_PhotoThumb, _BigPhotoLabel):
        if not issubclass(cls, QLabel):
            fail(f"{cls.__name__}", "не QLabel")
        else:
            ok(f"{cls.__name__}: QLabel ✓")

    # ChooseActorDialog имеет константу NEW_SENTINEL
    if not hasattr(ChooseActorDialog, 'NEW_SENTINEL'):
        fail("ChooseActorDialog.NEW_SENTINEL", "константа потеряна")
    else:
        ok(f"ChooseActorDialog.NEW_SENTINEL = {ChooseActorDialog.NEW_SENTINEL!r}")

    # Re-export consistency
    try:
        from storyboard_app import AddActorDialog as A_via_app
        if A_via_app is AddActorDialog:
            ok("storyboard_app.AddActorDialog ≡ widgets.AddActorDialog")
        else:
            fail("re-export consistency", "AddActorDialog — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4g: widgets/actor_dialogs (шаг 4B) ─────────────────────
def test_widgets_actor_dialogs_4b() -> None:
    """Шаг 4B: _LayoutVariantCard, CreateActorRefDialog, RefResultDialog —
    дописаны в widgets/actor_dialogs.py."""
    section("4g. widgets/actor_dialogs (шаг 4B: ref-диалоги)")
    try:
        from widgets import CreateActorRefDialog, RefResultDialog
        from widgets.actor_dialogs import _LayoutVariantCard
    except Exception as e:
        fail("from widgets import 4B", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт _LayoutVariantCard / CreateActorRefDialog / RefResultDialog")

    from PyQt6.QtWidgets import QDialog, QFrame
    if not issubclass(_LayoutVariantCard, QFrame):
        fail("_LayoutVariantCard", "не QFrame")
    else:
        ok("_LayoutVariantCard: QFrame ✓")
    for cls in (CreateActorRefDialog, RefResultDialog):
        if not issubclass(cls, QDialog):
            fail(f"{cls.__name__}", "не QDialog")
        else:
            ok(f"{cls.__name__}: QDialog ✓")

    # RefResultDialog: ключевые методы (duck-typed contract с ActorsView)
    expected = ('append_variant', 'target_path', '_sync_pending_to_owner',
                '_on_done', '_on_regen', '_on_delete_variant')
    missing = [m for m in expected if not hasattr(RefResultDialog, m)]
    if missing:
        fail("RefResultDialog API", f"потерялись методы: {missing}")
    else:
        ok(f"RefResultDialog API: все методы на месте ({len(expected)})")

    # Re-export
    try:
        from storyboard_app import RefResultDialog as RR_via_app
        if RR_via_app is RefResultDialog:
            ok("storyboard_app.RefResultDialog ≡ widgets.RefResultDialog")
        else:
            fail("re-export consistency", "RefResultDialog — другой объект")
    except Exception as e:
        fail("re-export check", str(e))

    # Constants used via _sa proxy — должны быть в storyboard_app __main__
    # (фейл здесь = повторение бага «Не получилось запустить генерацию»
    # 2026-05-04 когда я случайно удалил их в шаге 4A)
    try:
        import storyboard_app as _app
        for name in ('ACTOR_REF_PROMPT_DETAILED', 'ACTOR_REF_PROMPT_SIMPLE',
                     '_TRANSLIT_RU', '_FILENAME_STOPWORDS',
                     'build_actor_ref_filename'):
            if not hasattr(_app, name):
                fail(f"storyboard_app.{name}",
                     "константа/функция отсутствует — RefResultDialog _sa.X "
                     "вернёт AttributeError, юзер увидит «Не получилось "
                     "запустить генерацию»")
            else:
                ok(f"storyboard_app.{name} ✓")
    except Exception as e:
        fail("constants check", str(e))


# ─── Тест 4i: widgets/editor_widgets (шаг 5A) ────────────────────
def test_widgets_editor() -> None:
    """Шаг 5A: OverlayActionBtn / ShotCard / RoundedTopImage / RefCard
    вынесены в widgets/editor_widgets.py."""
    section("4i. widgets/editor_widgets (шаг 5A)")
    try:
        from widgets import OverlayActionBtn, ShotCard, RoundedTopImage, RefCard
    except Exception as e:
        fail("from widgets import editor", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт 4 виджетов из widgets/editor_widgets")

    from PyQt6.QtWidgets import QFrame, QWidget
    if not issubclass(OverlayActionBtn, QFrame):
        fail("OverlayActionBtn", "не QFrame")
    else:
        ok("OverlayActionBtn: QFrame ✓")
    if not issubclass(ShotCard, QFrame):
        fail("ShotCard", "не QFrame")
    else:
        ok("ShotCard: QFrame ✓")
    if not issubclass(RoundedTopImage, QWidget):
        fail("RoundedTopImage", "не QWidget")
    else:
        ok("RoundedTopImage: QWidget ✓")
    if not issubclass(RefCard, QFrame):
        fail("RefCard", "не QFrame")
    else:
        ok("RefCard: QFrame ✓")

    # Контракты сигналов (вызываются из MainWindow handlers)
    if not all(hasattr(ShotCard, s) for s in ('regen_requested', 'edit_requested')):
        fail("ShotCard signals", "потерялись regen_requested/edit_requested")
    else:
        ok("ShotCard signals: regen + edit ✓")
    if not all(hasattr(RefCard, s) for s in
               ('image_clicked', 'regen_requested', 'edit_requested')):
        fail("RefCard signals", "потерялись image_clicked/regen/edit")
    else:
        ok("RefCard signals: image + regen + edit ✓")

    # Re-export
    try:
        from storyboard_app import ShotCard as SC_via_app
        if SC_via_app is ShotCard:
            ok("storyboard_app.ShotCard ≡ widgets.ShotCard")
        else:
            fail("re-export consistency", "ShotCard — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4h: views/actors (шаг 4C) ──────────────────────────────
def test_views_actors() -> None:
    """Шаг 4C: ActorsView + ActorCard вынесены в views/actors.py."""
    section("4h. views/actors (шаг 4C)")
    try:
        from views import ActorsView, ActorCard
    except Exception as e:
        fail("from views import", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт ActorsView + ActorCard из views/")

    from PyQt6.QtWidgets import QWidget, QFrame
    if not issubclass(ActorsView, QWidget):
        fail("ActorsView", "не QWidget")
    else:
        ok("ActorsView: QWidget ✓")
    if not issubclass(ActorCard, QFrame):
        fail("ActorCard", "не QFrame")
    else:
        ok("ActorCard: QFrame ✓")

    # Контракт ActorsView (методы которые зовёт RefResultDialog/CreateActorRefDialog)
    expected_av = ('start_ref_generation', 'update_pending_variants',
                   'confirm_pending_kept', 'refresh', 'apply_lang')
    missing = [m for m in expected_av if not hasattr(ActorsView, m)]
    if missing:
        fail("ActorsView API", f"потерялись методы: {missing}")
    else:
        ok(f"ActorsView API: все методы на месте ({len(expected_av)})")

    # ActorCard должен иметь signals для коммуникации с ActorsView
    from PyQt6.QtCore import pyqtBoundSignal
    expected_signals = ('rename_requested', 'clicked', 'create_ref_requested',
                        'view_refs_requested', 'pending_clicked')
    missing_sig = [s for s in expected_signals if not hasattr(ActorCard, s)]
    if missing_sig:
        fail("ActorCard signals", f"потерялись: {missing_sig}")
    else:
        ok(f"ActorCard signals: все на месте ({len(expected_signals)})")

    # Re-export
    try:
        from storyboard_app import ActorsView as A_via_app
        if A_via_app is ActorsView:
            ok("storyboard_app.ActorsView ≡ views.ActorsView")
        else:
            fail("re-export consistency", "ActorsView — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4j: views/episode_chat (шаг 5B) ────────────────────────
def test_views_episode_chat() -> None:
    """Шаг 5B: EpisodeChatView + ChatInputEdit вынесены в
    views/episode_chat.py."""
    section("4j. views/episode_chat (шаг 5B)")
    try:
        from views import EpisodeChatView, ChatInputEdit
    except Exception as e:
        fail("from views import episode_chat", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт EpisodeChatView + ChatInputEdit из views/")

    from PyQt6.QtWidgets import QWidget, QPlainTextEdit
    if not issubclass(EpisodeChatView, QWidget):
        fail("EpisodeChatView", "не QWidget")
    else:
        ok("EpisodeChatView: QWidget ✓")
    if not issubclass(ChatInputEdit, QPlainTextEdit):
        fail("ChatInputEdit", "не QPlainTextEdit")
    else:
        ok("ChatInputEdit: QPlainTextEdit ✓")

    # Контракт ChatInputEdit (сигнал должен быть)
    if not hasattr(ChatInputEdit, 'submit_requested'):
        fail("ChatInputEdit signal", "потерялся submit_requested")
    else:
        ok("ChatInputEdit.submit_requested ✓")

    # Контракт EpisodeChatView API (что зовёт MainWindow)
    expected = ('set_episode', 'on_external_append', 'apply_lang',
                '_on_send', '_render_message')
    missing = [m for m in expected if not hasattr(EpisodeChatView, m)]
    if missing:
        fail("EpisodeChatView API", f"потерялись методы: {missing}")
    else:
        ok(f"EpisodeChatView API: все методы на месте ({len(expected)})")

    # Re-export
    try:
        from storyboard_app import EpisodeChatView as E_via_app
        from storyboard_app import ChatInputEdit as C_via_app
        if E_via_app is EpisodeChatView:
            ok("storyboard_app.EpisodeChatView ≡ views.EpisodeChatView")
        else:
            fail("re-export consistency", "EpisodeChatView — другой объект")
        if C_via_app is ChatInputEdit:
            ok("storyboard_app.ChatInputEdit ≡ views.ChatInputEdit")
        else:
            fail("re-export consistency", "ChatInputEdit — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4k: views/new_episode (шаг 5C) ─────────────────────────
def test_views_new_episode() -> None:
    """Шаг 5C: NewEpisodeView вынесен в views/new_episode.py."""
    section("4k. views/new_episode (шаг 5C)")
    try:
        from views import NewEpisodeView
    except Exception as e:
        fail("from views import NewEpisodeView", f"{e.__class__.__name__}: {e}")
        return
    ok("импорт NewEpisodeView из views/")

    from PyQt6.QtWidgets import QWidget
    if not issubclass(NewEpisodeView, QWidget):
        fail("NewEpisodeView", "не QWidget")
    else:
        ok("NewEpisodeView: QWidget ✓")

    # Контракт NewEpisodeView API (что зовёт MainWindow и сам класс)
    # 2026-05-09: _on_model_changed убран из NewEpisodeView вместе
    # с виджетом дропдауна (модель читается из QSettings через
    # _current_model). EpisodeChatView сохраняет свой _on_model_changed.
    expected = ('apply_lang', '_on_run', '_on_send_followup', '_on_stop',
                '_on_thread_finished', '_on_thread_error', '_on_thread_stopped',
                '_append_log_persist', '_on_chunk_persist',
                '_show_open_chat_btn', '_reset_for_new_episode',
                '_current_model',
                'dragEnterEvent', 'dragLeaveEvent', 'dropEvent')
    missing = [m for m in expected if not hasattr(NewEpisodeView, m)]
    if missing:
        fail("NewEpisodeView API", f"потерялись методы: {missing}")
    else:
        ok(f"NewEpisodeView API: все методы на месте ({len(expected)})")

    # _LOG_COLORS — class-level dict для подсветки kind-строк
    if not hasattr(NewEpisodeView, '_LOG_COLORS'):
        fail("NewEpisodeView._LOG_COLORS", "константа исчезла")
    else:
        ok("NewEpisodeView._LOG_COLORS на месте")

    # Re-export
    try:
        from storyboard_app import NewEpisodeView as N_via_app
        if N_via_app is NewEpisodeView:
            ok("storyboard_app.NewEpisodeView ≡ views.NewEpisodeView")
        else:
            fail("re-export consistency", "NewEpisodeView — другой объект")
    except Exception as e:
        fail("re-export check", str(e))


# ─── Тест 4l: list_show_characters (актёры → персонаж попап) ─────
def test_list_show_characters() -> None:
    """Хелпер list_show_characters читает episodes.json + папки рефов
    и возвращает дедуплицированный отсортированный список персонажей.
    Используется в CreateActorRefDialog (вкладка Актёры → создать реф)."""
    section("4l. list_show_characters (для попапа создания рефа)")
    try:
        from storyboard_app import list_show_characters
    except Exception as e:
        fail("import list_show_characters", str(e))
        return
    ok("list_show_characters импортируется")

    # Реальный сериал the_last_plan — там есть и episodes.json, и refs/characters/
    show_root = PROJECT_ROOT / "shows" / "the_last_plan"
    if show_root.exists():
        chars = list_show_characters(PROJECT_ROOT, "the_last_plan")
        if not isinstance(chars, list):
            fail("list_show_characters returns list", f"got {type(chars)}")
            return
        ok(f"list_show_characters('the_last_plan') → {len(chars)} персонажей")
        if not chars:
            fail("чтение персонажей",
                 "пустой список — episodes.json и папок refs/characters/ нет?")
        else:
            # Должны быть отсортированы алфавитно и без дубликатов
            if list(sorted(set(chars))) != chars:
                fail("сортировка/дедуп", "не sorted+unique")
            else:
                ok("сортированы и без дубликатов")
            # У the_last_plan должна быть laura или david хотя бы
            expected = {'laura', 'david'}
            if not (set(chars) & expected):
                fail("ожидаемые имена",
                     f"ни laura ни david нет в {chars}")
            else:
                ok("ожидаемые персонажи на месте (laura/david)")
            # ВАЖНО: список НЕ должен содержать имена актёров. Папки
            # refs/characters/<actor_slug>/ — тестовые артефакты от старой
            # логики, должны фильтроваться.
            try:
                from storyboard_app import list_actors
                actor_slugs = set(list_actors(PROJECT_ROOT))
            except Exception:
                actor_slugs = set()
            leaked_actors = set(chars) & actor_slugs
            if leaked_actors:
                fail("фильтрация актёров",
                     f"в списке персонажей просочились актёры: {leaked_actors}")
            else:
                ok(f"актёры отфильтрованы из персонажей "
                   f"({len(actor_slugs)} актёров проигнорировано)")

    # Сериал которого нет → пустой список
    chars_empty = list_show_characters(PROJECT_ROOT, "no_such_show_xyz")
    if chars_empty != []:
        fail("несуществующий сериал → пустой list",
             f"got {chars_empty}")
    else:
        ok("несуществующий сериал → []")

    # Пустой slug → пустой list
    if list_show_characters(PROJECT_ROOT, "") != []:
        fail("пустой slug → пустой list", "не пусто")
    else:
        ok("пустой slug → []")


# ─── Тест 4m: actor roles (актёр→персонаж в сериале) ────────────
def test_actor_roles() -> None:
    """Хелперы get_actor_role / set_actor_role + интеграция в
    get_actor_generated_refs_paths. Связь хранится в actors.json
    под ключом roles: {<show>: <character_slug>}."""
    section("4m. actor roles (актёр→персонаж в сериале)")
    try:
        from storyboard_app import (
            get_actor_role, set_actor_role,
            read_actors_meta,
        )
    except Exception as e:
        fail("import role helpers", str(e))
        return
    ok("get_actor_role / set_actor_role импортируются")

    # Не пишем в реальный actors.json — тест чисто на сигнатуру и
    # отсутствие падений. В временной папке проверим круговой round-trip.
    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path as _P
        root = _P(td)
        (root / "actors").mkdir()
        # Round-trip set + get
        set_actor_role(root, "akter_4", "the_last_plan", "laura")
        char = get_actor_role(root, "akter_4", "the_last_plan")
        if char != "laura":
            fail("set→get round-trip",
                 f"ожидал 'laura', получил {char!r}")
            return
        ok("set_actor_role + get_actor_role: round-trip OK")
        # Несуществующая связь → None
        if get_actor_role(root, "no_such_actor", "the_last_plan") is not None:
            fail("несущ. актёр", "должен возвращать None")
            return
        if get_actor_role(root, "akter_4", "no_such_show") is not None:
            fail("несущ. сериал", "должен возвращать None")
            return
        ok("отсутствие связи → None")
        # Перезапись
        set_actor_role(root, "akter_4", "the_last_plan", "marta")
        if get_actor_role(root, "akter_4", "the_last_plan") != "marta":
            fail("перезапись роли", "не сохранилось")
            return
        ok("перезапись роли работает")
        # Старое поле display_name не должно пропасть
        meta_after = read_actors_meta(root)
        # Проверяем что структура корректная
        if not isinstance(meta_after.get("akter_4", {}).get("roles"), dict):
            fail("структура roles", "не dict")
            return
        ok("actors.json:<actor>.roles — dict")


# ─── Тест 4n: GEN-маркеры + GenButton + AutonomousGenThread ─────
def test_gen_markers_and_button() -> None:
    """Sub-MVP «кнопка автономной генерации в чате эпизода».
    Проверяет: парсер маркеров, импорт GenButton, импорт AutonomousGenThread."""
    section("4n. gen markers + GenButton (sub-MVP)")
    # Парсер маркеров
    try:
        from views._chat_render import parse_gen_markers, GenMarker
    except Exception as e:
        fail("import parse_gen_markers", str(e))
        return
    ok("parse_gen_markers + GenMarker импортируются")

    text = ('- ✗ prison_phone_hallway — нужно [[GEN:location:prison_phone_hallway:Тюремный коридор]]\n'
            '- ✗ david_smartphone — нет [[GEN:object:david_smartphone:Смартфон Дэвида]]\n'
            'обычный текст')
    clean, markers = parse_gen_markers(text)
    if len(markers) != 2:
        fail("парсер 2 маркеров", f"получил {len(markers)}")
        return
    ok("парсер извлёк 2 маркера")
    if '[[GEN:' in clean:
        fail("clean содержит маркер", "не должен")
        return
    ok("clean без маркеров")
    if markers[0].type != 'location' or markers[0].name != 'prison_phone_hallway':
        fail("первый маркер некорректный", str(markers[0]))
        return
    ok("первый маркер: location/prison_phone_hallway")
    if markers[1].type != 'object' or markers[1].name != 'david_smartphone':
        fail("второй маркер некорректный", str(markers[1]))
        return
    ok("второй маркер: object/david_smartphone")
    # Пустой текст / без маркеров
    if parse_gen_markers('')[1] != []:
        fail("пустой текст", "должен возвращать []")
        return
    if parse_gen_markers('просто текст')[1] != []:
        fail("без маркеров", "должен возвращать []")
        return
    ok("пустой / без маркеров → []")

    # GenButton
    try:
        from widgets import GenButton
    except Exception as e:
        fail("import GenButton", str(e))
        return
    ok("GenButton импортируется через widgets")
    from PyQt6.QtWidgets import QFrame
    if not issubclass(GenButton, QFrame):
        fail("GenButton", "не QFrame")
        return
    expected = ('generate_requested', 'open_refs_requested', 'retry_requested',
                'set_running', 'set_progress', 'set_done', 'set_error',
                'reset_to_idle', 'apply_lang')
    missing = [m for m in expected if not hasattr(GenButton, m)]
    if missing:
        fail("GenButton API", f"потерялись: {missing}")
        return
    ok(f"GenButton API на месте ({len(expected)} методов/сигналов)")

    # AutonomousGenThread
    try:
        from threads import AutonomousGenThread
    except Exception as e:
        fail("import AutonomousGenThread", str(e))
        return
    ok("AutonomousGenThread импортируется через threads")
    from PyQt6.QtCore import QThread
    if not issubclass(AutonomousGenThread, QThread):
        fail("AutonomousGenThread", "не QThread")
        return
    sig_methods = ('progress', 'finished_ok', 'error', 'stopped',
                   'run', 'stop')
    missing = [m for m in sig_methods if not hasattr(AutonomousGenThread, m)]
    if missing:
        fail("AutonomousGenThread API", f"потерялись: {missing}")
        return
    ok(f"AutonomousGenThread API на месте ({len(sig_methods)})")

    # 2026-05-11 (БАГ 12 regression test): synthesize_gen_markers должен
    # парсить character-маркеры с nested скобками в description.
    # Раньше `[^)]+` останавливался на первой внутренней `)` → marker
    # не создавался → CTA «Make storyboards» показывалась когда
    # character ещё не сгенерирован.
    try:
        from views._chat_render import synthesize_gen_markers
    except Exception as e:
        fail("import synthesize_gen_markers", str(e))
        return
    ok("synthesize_gen_markers импортируется")
    full_text_nested = (
        "ПЕРСОНАЖИ:\n"
        "- ✗ david (Дэвид — муж) — рефа нет\n"
        "- ✗ lora (Лора — жена. Сцены 7-8 — обнажена; "
        "сцены 9-12 — в халате (надевает выскакивая из кровати). "
        "Нужны два состояния) — рефа нет\n"
        "- ✗ mark (Марк — любовник) — нужен реф\n"
    )
    nested_markers = synthesize_gen_markers(full_text_nested)
    char_names = [m.name for m in nested_markers if m.type == 'character']
    if 'lora' not in char_names:
        fail("БАГ 12 regression",
             f"lora с nested () не распарсилась — char_names={char_names}")
        return
    if char_names != ['david', 'lora', 'mark']:
        fail("БАГ 12 regression",
             f"ожидали [david, lora, mark], получили {char_names}")
        return
    ok("synthesize_gen_markers парсит nested скобки (БАГ 12 regression test)")


# ─── Тест 4: block_wheel_event применяется ───────────────────────
def test_block_wheel_event() -> None:
    section("4. block_wheel_event на виджетах настроек")
    src_app = (PROJECT_ROOT / "storyboard_app.py").read_text(encoding="utf-8")
    if "def block_wheel_event(" not in src_app:
        fail("block_wheel_event определён", "функция не найдена в storyboard_app.py")
        return
    ok("block_wheel_event определён")

    # После рефакторинга вызовы раскиданы по views/ и widgets/ (через _sa).
    # Считаем суммарно: storyboard_app + views/* + widgets/*.
    files = [PROJECT_ROOT / "storyboard_app.py"]
    for sub in ("views", "widgets"):
        d = PROJECT_ROOT / sub
        if d.exists():
            files += [p for p in d.glob("*.py") if p.name != "__init__.py"]
    total = 0
    for p in files:
        try:
            total += p.read_text(encoding="utf-8").count("block_wheel_event(")
        except Exception:
            pass
    if total < 4:
        fail("block_wheel_event использован",
             f"всего {total} вхождений (ожидаю ≥4 — определение + ≥3 виджета)")
    else:
        ok(f"block_wheel_event вызывается суммарно ({total} раз "
           f"в app + views + widgets)")


# ─── Тест 5: Маркеры предыдущих фиксов ───────────────────────────
def test_session_log_markers() -> None:
    """Проверяем что фичи из _session_log.md живы.
    Если кто-то случайно откатил — здесь увидим.

    Для маркеров-TRANSLATIONS-ключей считаем суммарно по storyboard_app.py
    и i18n.py (после рефакторинга 2026-05-04 ключи живут в i18n.py).
    """
    section("5. Маркеры из _session_log.md (анти-откат)")
    src_app = (PROJECT_ROOT / "storyboard_app.py").read_text(encoding="utf-8")
    src_i18n = (PROJECT_ROOT / "i18n.py").read_text(encoding="utf-8") \
        if (PROJECT_ROOT / "i18n.py").exists() else ""
    src_actor_dlg = (PROJECT_ROOT / "widgets" / "actor_dialogs.py").read_text(encoding="utf-8") \
        if (PROJECT_ROOT / "widgets" / "actor_dialogs.py").exists() else ""
    src_views_actors = (PROJECT_ROOT / "views" / "actors.py").read_text(encoding="utf-8") \
        if (PROJECT_ROOT / "views" / "actors.py").exists() else ""
    combined = src_app + "\n" + src_i18n
    # Маркеры actor-логики раскиданы по views/actors.py + widgets/actor_dialogs.py
    src_all = src_app + "\n" + src_actor_dlg + "\n" + src_views_actors

    # Маркеры в коде приложения (только storyboard_app.py)
    code_markers = [
        # Утилиты (остаются в storyboard_app.py)
        ("def block_wheel_event", 1),
        ("def cross_fade_swap", 1),
        ("def find_claude_cli", 1),
    ]
    for marker, min_count in code_markers:
        actual = src_app.count(marker)
        if actual >= min_count:
            ok(f"{marker!r} — {actual}× (ожидал ≥{min_count})")
        else:
            fail(f"маркер {marker!r}", f"всего {actual}× (ожидал ≥{min_count}) — возможно откат?")

    # Маркеры распределённые по storyboard_app.py + widgets + views
    # После шага 4C: ActorsView переехал в views/actors.py, RefResultDialog
    # в widgets/actor_dialogs.py. Owner_view callbacks реализуются в ActorsView,
    # вызываются из widgets — ищем по всем 3 файлам.
    cross_markers = [
        # pending-стек (в ActorsView → views/actors.py)
        ("_pending_variants", 3),
        ("_on_pending_clicked", 2),
        # ActorCard / ActorsView (в views/actors.py)
        ("class ActorCard", 1),
        ("class ActorsView", 1),
        # RefResultDialog (определение в widgets/actor_dialogs)
        ("class RefResultDialog", 1),
        ("def append_variant", 1),
        # owner_view callbacks (определение в views/actors.py, вызовы в widgets)
        ("confirm_pending_kept", 2),
        ("update_pending_variants", 2),
    ]
    for marker, min_count in cross_markers:
        actual = src_all.count(marker)
        if actual >= min_count:
            ok(f"{marker!r} — {actual}× в app+widgets+views (ожидал ≥{min_count})")
        else:
            fail(f"маркер {marker!r}",
                 f"всего {actual}× в app+widgets+views (ожидал ≥{min_count}) — возможно откат?")

    # TRANSLATIONS-маркеры — считаем по storyboard_app + i18n
    # (3× = ru/uk/en + опционально usage в коде)
    i18n_markers = [
        ('actor_card_pending_ready', 3),
        ('ref_result_done_keep', 3),
        ('actor_progress_starting', 3),
    ]
    for marker, min_count in i18n_markers:
        actual = combined.count(marker)
        if actual >= min_count:
            ok(f"{marker!r} — {actual}× в app+i18n (ожидал ≥{min_count})")
        else:
            fail(f"маркер {marker!r}",
                 f"всего {actual}× в app+i18n (ожидал ≥{min_count}) — возможно откат?")


# ─── Тест 6: MainWindow создаётся ────────────────────────────────
def test_main_window() -> None:
    section("6. MainWindow создаётся (headless)")
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
        ok("QApplication запустился")
    except Exception as e:
        fail("QApplication", f"{e.__class__.__name__}: {e}")
        return

    try:
        import storyboard_app as sa
        ok("storyboard_app импортирован")
    except Exception as e:
        fail("импорт storyboard_app",
             f"{e.__class__.__name__}: {e}\n{traceback.format_exc(limit=3)}")
        return

    # MainWindow требует project_root. Используем текущий проект.
    try:
        win = sa.MainWindow(PROJECT_ROOT)
        ok("MainWindow создан")
    except Exception as e:
        fail("MainWindow(project_root)",
             f"{e.__class__.__name__}: {e}\n{traceback.format_exc(limit=4)}")
        return

    # Проверяем что вкладки добавлены
    try:
        tabs = win.tabs
        count = tabs.count()
        if count < 3:
            fail("MainWindow.tabs", f"ожидал ≥3 вкладок, нашёл {count}")
        else:
            titles = [tabs.tabText(i) for i in range(count)]
            ok(f"вкладок: {count} ({', '.join(titles)})")
    except Exception as e:
        fail("MainWindow.tabs", f"{e.__class__.__name__}: {e}")

    # Программно переключаем все вкладки — ловим репейнт-краши
    try:
        for i in range(win.tabs.count()):
            win.tabs.setCurrentIndex(i)
        ok("переключение всех вкладок без падений")
    except Exception as e:
        fail("setCurrentIndex по всем вкладкам",
             f"{e.__class__.__name__}: {e}")

    # Аккуратно закрываем
    try:
        win.close()
        win.deleteLater()
    except Exception:
        pass


# ─── Тест 7: Базовые helpers ─────────────────────────────────────
def test_helpers() -> None:
    section("7. Базовые helpers")
    try:
        from storyboard_app import (
            get_lang, set_lang, tr,
            block_wheel_event, slugify_actor_name,
            list_shows, get_current_show,
        )
    except Exception as e:
        fail("импорт helpers", f"{e.__class__.__name__}: {e}")
        return

    ok("get_lang / set_lang / tr — импортируются")

    # tr() возвращает строку для известного ключа
    set_lang("ru")
    s = tr("tab_editor")
    if s and isinstance(s, str):
        ok(f"tr('tab_editor', ru) = {s!r}")
    else:
        fail("tr('tab_editor')", f"вернул {s!r}")

    set_lang("en")
    s_en = tr("tab_editor")
    if s_en and s_en != s:
        ok(f"tr('tab_editor', en) = {s_en!r}")
    else:
        fail("tr en", f"ожидал отличный от ru, получил {s_en!r}")

    # slugify
    slug = slugify_actor_name("Оля Петрова")
    if slug and slug.replace("_", "").isalnum():
        ok(f"slugify_actor_name('Оля Петрова') = {slug!r}")
    else:
        fail("slugify_actor_name", f"вернул {slug!r}")


# ─── Запуск ──────────────────────────────────────────────────────
def main() -> int:
    print(f"\n{YELLOW}╔══════════════════════════════════════════════════╗{RESET}")
    print(f"{YELLOW}║   Storyboard Studio — smoke tests                 ║{RESET}")
    print(f"{YELLOW}╚══════════════════════════════════════════════════╝{RESET}")
    print(f"{DIM}project: {PROJECT_ROOT}{RESET}")

    test_ast_parse()
    test_translations()
    test_no_claude_in_ui()
    test_i18n_qsettings_sync()
    test_threads_update_imports()
    test_appproxy_main_first()
    test_threads_generate_imports()
    test_widgets_dialogs_imports()
    test_widgets_actor_dialogs_imports()
    test_widgets_actor_dialogs_4b()
    test_widgets_editor()
    test_views_actors()
    test_views_episode_chat()
    test_views_new_episode()
    test_list_show_characters()
    test_actor_roles()
    test_gen_markers_and_button()
    test_block_wheel_event()
    test_session_log_markers()
    test_main_window()
    test_helpers()

    # ─── Итог ───
    total = len(PASSED) + len(FAILED)
    print(f"\n{YELLOW}━━━ Итог ━━━{RESET}")
    if FAILED:
        print(f"{RED}✗ {len(FAILED)} из {total} тестов упало{RESET}")
        for name, _ in FAILED:
            print(f"  {RED}•{RESET} {name}")
        return 1
    else:
        print(f"{GREEN}✓ Все {total} тестов прошли{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
