#!/usr/bin/env python3
"""
Storyboard Studio — macOS / Windows приложение для storyboard-automation.

При первом запуске спрашивает папку проекта и запоминает её.
Запуск из исходников: python3 storyboard_app.py
"""

import re
import os
import sys
import io
import json
import time
import threading
import webbrowser
import base64
import shutil
import zipfile
import tempfile
import datetime
import subprocess
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Optional, List, Dict, Tuple

# i18n — TRANSLATIONS / get_lang / set_lang / tr / _pick_lang / SUPPORTED_LANGUAGES
# вытащены в i18n.py 2026-05-04 чтобы уменьшить размер этого файла на ~600 строк.
from i18n import (
    SUPPORTED_LANGUAGES,
    TRANSLATIONS,
    _pick_lang,
    get_lang,
    set_lang,
    tr,
)

# show_manager — управление сериалами (создание, slug-транслит, meta.json).
# Чистый Python без Qt, юниты в tests/test_show_manager.py.
import show_manager
import frame_format

# scenario_parser — разбор документа со сценариями (библия + эпизоды).
# Чистый Python без Qt, юниты в tests/test_scenario_parser.py.
import scenario_parser
from agents import mode_loader

# threads — фоновые QThread классы. В подмодулях threads/*.py используется
# lazy proxy `_sa = _AppProxy()` для доступа к storyboard_app — это решает
# circular import в PyInstaller-сборке (см. threads/update.py docstring +
# запись в _session_log.md от 2026-05-04 «Хотфикс circular import»).
# Вытащены 2026-05-04 в 2 шага: update.py (5 тредов) + generate.py (5 тредов).
from threads import (
    # update.py
    CheckUpdateThread,
    DownloadUpdateThread,
    DownloadAppUpdateThread,
    SendUpdateThread,
    FetchStatsThread,
    # generate.py
    GenerateThread,
    RefGenerateThread,
    GenerateActorRefThread,
    ClaudeGeometryThread,
    ImprovePromptThread,
    RunEpisodeThread,
)

# views/ — главные view-вкладки.
#   Шаг 4C (2026-05-04): ActorsView + ActorCard.
#   Шаг 5B (2026-05-04): EpisodeChatView + ChatInputEdit (views/episode_chat.py).
#   Шаг 5C (2026-05-04): NewEpisodeView (views/new_episode.py).
from views import (
    ActorsView, ActorCard,
    CameraLabView,
    EpisodeChatView, ChatInputEdit,
    NewEpisodeView,
)

from generator import GeneratorPage

# widgets/ — диалоги. Шаги 3 (2026-05-04, dialogs.py) и 4A (actor_dialogs.py).
from widgets import (
    # dialogs.py (шаг 3)
    FullscreenImageDialog,
    RefDoneNoticeDialog,
    GeometryDoneNoticeDialog,
    CloseConfirmDialog,
    # actor_dialogs.py (шаг 4A): простые actor-диалоги без callback'ов
    AddActorDialog,
    ChooseActorDialog,
    ActorPhotosDialog,
    # actor_dialogs.py (шаг 4B): диалоги с owner_view-callback (duck typing)
    CreateActorRefDialog,
    RefResultDialog,
    # editor_widgets.py (шаг 5A): виджеты карточек шотов и рефов
    OverlayActionBtn,
    ShotCard,
    RoundedTopImage,
    RefCard,
    # provider_toggle.py (2026-06-16): сегмент-контрол провайдера в Настройках
    ProviderToggle,
    # mode_segment.py (2026-06-16): N-сегментный контрол режима монтажки
    ModeSegment,
    # stepper.py (2026-06-16): числовой степпер [− N +] для зоны Mode C
    Stepper,
)

import requests
from PIL import Image as PILImage

# SSL-fix для PyInstaller frozen .app
if getattr(sys, 'frozen', False):
    import os, certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    os.environ.setdefault('REQUESTS_CA_BUNDLE', certifi.where())

# ─────────────────────────────────────────────────────────────────────────
# v1.0.65: proxy settings — централизованная установка HTTP_PROXY/HTTPS_PROXY
# из QSettings в os.environ при старте Studio.
#
# Источник истины — QSettings ключи "proxy/enabled" / "proxy/host" /
# "proxy/port" / "proxy/username" / "proxy/password" (заполняются через
# UI в Settings → секция «🌐 ПРОКСИ-СЕРВЕР»).
#
# Эффект: все исходящие HTTP-запросы Studio (requests-based — Fast Gen,
# GitHub API, GitHub Releases, threads/generate.py) автоматически идут
# через прокси. Subprocess'ы Claude CLI наследуют env Studio'шного
# процесса → они тоже видят HTTPS_PROXY (если CLI его уважает).
#
# Изменения окружения через `os.environ[...] = ...` (не setdefault) —
# юзер хочет что прокси Studio перетёр любые системные настройки.
# Если в системе уже стоял HTTPS_PROXY на другой адрес — Studio
# использует свой, не системный.
#
# Логирование (через sys.stderr): «Proxy enabled: host=… port=… user=…»,
# БЕЗ пароля. stderr перенаправлен в runtime.log через
# _init_studio_file_logging — лог попадёт туда (visible через
# Settings → «Открыть лог»).
#
# ВАЖНО: функция вызывается ровно ОДИН РАЗ в `def main()` ПОСЛЕ
# _init_studio_file_logging() и _install_qt_message_handler(), ДО
# `app = QApplication(sys.argv)`. QSettings с явным (APP_ORG, APP_NAME)
# работает без созданного QApplication.
# ─────────────────────────────────────────────────────────────────────────
def apply_proxy_from_settings() -> None:
    """Читает прокси-настройки из QSettings и применяет к os.environ.

    Если proxy/enabled=False — no-op.
    Если host или port пусты — лог-warning и no-op (некорректные настройки).
    Если username+password пусты — формирует URL без auth-части
    (`http://host:port`).
    Иначе — `http://user:pwd@host:port`.

    Устанавливает 4 env-vars в верхнем и нижнем регистре:
        HTTP_PROXY, HTTPS_PROXY, http_proxy, https_proxy.
    Это нужно для совместимости — `requests` смотрит uppercase,
    некоторые библиотеки (включая утилиты subprocess'ов) — lowercase.
    """
    try:
        from PyQt6.QtCore import QSettings as _QS
        s = _QS(APP_ORG, APP_NAME)
        enabled = s.value("proxy/enabled", False, type=bool)
        if not enabled:
            return
        host = (s.value("proxy/host", "", type=str) or "").strip()
        port = (s.value("proxy/port", "", type=str) or "").strip()
        user = (s.value("proxy/username", "", type=str) or "").strip()
        pwd = s.value("proxy/password", "", type=str) or ""
        if not (host and port):
            try:
                sys.stderr.write(
                    f"[proxy] enabled=True but host/port empty — skipping\n")
            except Exception:
                pass
            return
        if user and pwd:
            proxy_url = f"http://{user}:{pwd}@{host}:{port}"
        else:
            proxy_url = f"http://{host}:{port}"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        # Лог БЕЗ пароля. Если user пуст — пишем "user=(none)".
        safe_user = user if user else "(none)"
        try:
            sys.stderr.write(
                f"[proxy] enabled: host={host} port={port} user={safe_user}\n")
        except Exception:
            pass
    except Exception:
        # Любая ошибка чтения QSettings / set env — не блокирует старт Studio.
        # Юзер просто пойдёт без прокси, прочитает stderr и поймёт.
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass


from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QListWidget, QListWidgetItem,
    QStatusBar, QFileDialog, QMessageBox, QProgressBar, QDialog,
    QDialogButtonBox, QTabWidget, QComboBox, QPlainTextEdit, QTextEdit, QMenu,
    QWidgetAction,
    QStackedWidget, QGridLayout, QGraphicsOpacityEffect, QLineEdit, QSizePolicy,
    QCheckBox,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QFileSystemWatcher, QTimer, QSize, QSettings,
    QRectF, QPoint, QPropertyAnimation, QEasingCurve, QObject, QEvent,
    QByteArray,
)
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPainterPath, QAction, QGuiApplication, QKeySequence, QShortcut, QColor, QTextCursor, QIcon

from views.theme import (
    build_llm_theme_prompt,
    install_theme_runtime,
    load_active_theme_overrides,
    load_theme_store,
    normalize_theme_payload,
    save_theme_store,
    set_theme_overrides_from_payload,
    theme_qcolor,
    theme_qss,
    validate_theme_payload,
)


class _BlockCarouselScrollArea(QScrollArea):
    """Horizontal wheel-scroll area for compact block pills."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def wheelEvent(self, ev):
        delta = ev.angleDelta()
        step = delta.x() if delta.x() else delta.y()
        if step:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - step)
            ev.accept()
            return
        super().wheelEvent(ev)

# ─── Константы ───────────────────────────────────────────────────────────────
APP_ORG  = "StoryboardStudio"
APP_NAME = "StoryboardApp"

# GitHub координаты
GITHUB_USER = "Akex24"
GITHUB_REPO = "storyboard-automation"
GITHUB_BRANCH = "main"

API_BASE     = "https://googler.fast-gen.ai"
STORAGE_BASE = "https://storage.fast-gen.ai"
MODEL        = "NARWHAL"
PANELS       = 4
# 2026-06-11 (авто-добор Mode C): сколько проходов добора недостающих версий
# делать. 1 = основа + один добор = 2 захода всего (минимизируем общее время).
MODE_C_RETRY_MAX_PASSES = 1

# Папки которые НЕ перезаписываются обновлением (контент пользователя)
PRESERVE_ON_UPDATE = {".env", ".env.local", "output", "refs", "scenarios",
                      "shows", "current_show.json",
                      "_inbox", ".claude", ".git", "build", "dist", "__pycache__",
                      ".DS_Store"}

_upload_cache: Dict[str, str] = {}

# Глобальные пути — инициализируются через setup_paths_for_show()
SHOW_ROOT       = Path()
ENV_FILE        = Path()
PROMPTS_DIR     = Path()
STORYBOARDS_DIR = Path()
SEEDANCE_DIR    = Path()
LOCATIONS_DIR   = Path()
CHARACTERS_DIR  = Path()
OBJECTS_DIR     = Path()
# 2026-05-17 (Этап 2): результаты «🎨 Наложить текстуру» —
# PIL-композит ref + texture с opacity. Папка лениво создаётся
# в ApplyTextureThread.run через mkdir(parents=True, exist_ok=True).
CHARACTERS_TEXTURE_DIR = Path()


def shows_dir(project_root: Path) -> Path:
    return project_root / "shows"


# ─── Актёры (общая папка для всех сериалов) ──────────────────────────────────
# `actors/` — реальные фото актёров, общие для всех сериалов одного админа.
# Структура:
#   actors/
#   ├── actors.json              ← {"olya": {"display_name": "Оля Петрова"}, ...}
#   ├── olya/
#   │   ├── front.jpg, side.jpg  ← оригинальные фото (несколько ракурсов)
#   └── marya/...
# Папка в .gitignore (локальная) — не уходит к коллегам.
# Только админ создаёт/переименовывает актёров и загружает фото.

def actors_dir(project_root: Path) -> Path:
    return project_root / "actors"


def actors_meta_path(project_root: Path) -> Path:
    return actors_dir(project_root) / "actors.json"


def read_actors_meta(project_root: Path) -> Dict:
    """Возвращает {slug: {"display_name": str}}. Пустой dict если файла нет."""
    f = actors_meta_path(project_root)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def write_actors_meta(project_root: Path, meta: Dict) -> None:
    actors_dir(project_root).mkdir(parents=True, exist_ok=True)
    actors_meta_path(project_root).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def list_actors(project_root: Path) -> List[str]:
    """Список slug'ов актёров (имена папок в actors/, исключая системные).

    Системные = начинаются с `.` (скрытые .DS_Store и т.п.) ИЛИ с `_`
    (служебные store-папки: `_textures/` для Этапа 1 хранилища текстур
    и любые будущие). Не отображаются на вкладке Актёры как карточки.
    """
    ad = actors_dir(project_root)
    if not ad.exists():
        return []
    return sorted(p.name for p in ad.iterdir()
                  if p.is_dir()
                  and not p.name.startswith(".")
                  and not p.name.startswith("_"))


def actor_display_name(project_root: Path, slug: str) -> str:
    """Красивое имя актёра из actors.json. Fallback — slug."""
    meta = read_actors_meta(project_root)
    info = meta.get(slug, {})
    return info.get("display_name") or slug


def get_actor_photos(project_root: Path, slug: str) -> List[Path]:
    """Список фото в папке актёра (jpg/jpeg/png). Сортировка по имени."""
    actor_path = actors_dir(project_root) / slug
    if not actor_path.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"}
    return sorted([f for f in actor_path.iterdir()
                   if f.is_file() and f.suffix in exts])


def get_actor_role(project_root: Path, actor_slug: str,
                   show_slug: str) -> Optional[str]:
    """Возвращает имя персонажа которого играет актёр в указанном сериале.
    Хранится в `actors.json` под ключом `roles: {<show>: <character_slug>}`.
    Записывается при генерации рефа (CreateActorRefDialog._on_generate).
    Возвращает None если связь не зафиксирована (актёр ещё не сыграл
    ни одной роли в этом сериале — кнопка «Все референсы» покажет
    пустое состояние)."""
    try:
        meta = read_actors_meta(project_root)
        actor = meta.get(actor_slug) or {}
        roles = actor.get("roles") or {}
        char = roles.get(show_slug)
        return char if isinstance(char, str) and char else None
    except Exception:
        return None


def set_actor_role(project_root: Path, actor_slug: str, show_slug: str,
                   character_slug: str) -> None:
    """Записывает «актёр играет персонажа в сериале» в `actors.json`.
    Вызывается из CreateActorRefDialog при создании рефа — благодаря
    этому кнопка «Все референсы (N)» на карточке знает в какую
    character-папку вести.

    Если у актёра уже была роль в этом сериале — перезаписывает (юзер
    сменил роль). Не идеально для сценария «один актёр играет двух
    персонажей в одном сериале», но в твоей практике это «как правило
    один в один»."""
    if not (actor_slug and show_slug and character_slug):
        return
    try:
        meta = read_actors_meta(project_root)
        # 2026-05-22 (v1.0.77): детективное логирование коллизий двойных
        # привязок (Артём+Женя на Caleb и т.п.). before_meta снимается
        # ДО изменений, лог пишется в actors/actors_changes.log.
        # Никогда не ломает основную функцию (отдельный try/except внутри).
        _log_actor_role_call(project_root, actor_slug, show_slug,
                              character_slug, meta)
        actor = meta.get(actor_slug)
        if not isinstance(actor, dict):
            actor = {}
        roles = actor.get("roles")
        if not isinstance(roles, dict):
            roles = {}
        roles[show_slug] = character_slug
        actor["roles"] = roles
        meta[actor_slug] = actor
        write_actors_meta(project_root, meta)
    except Exception:
        traceback.print_exc()


def _log_actor_role_call(project_root: Path, actor_slug: str, show_slug: str,
                           character_slug: str, before_meta: Dict) -> None:
    """v1.0.77: детективное логирование вызовов set_actor_role в
    `actors/actors_changes.log` для диагностики двойных привязок актёров
    к одному персонажу.

    Параметр `before_meta` — снимок read_actors_meta() ДО записи (для
    отображения before_roles и поиска коллизий). after_roles вычисляется
    из before_meta + новое назначение (актуальное состояние).

    Никогда не бросает исключения — вся запись wrapped в try/except.
    Если файл недоступен / диск полный / любая другая ошибка — логирование
    тихо пропускается, set_actor_role продолжает работу.

    Ротация: при размере > 1 MB файл переименовывается в .log.old (с
    перезаписью старого .old) и начинается новый. Один бэкап на ступень.

    Cross-platform: Path + open(encoding='utf-8'). Никаких subprocess/shell.
    """
    try:
        log_path = actors_dir(project_root) / "actors_changes.log"
        # Ротация при > 1 MB
        try:
            if log_path.exists() and log_path.stat().st_size > 1_000_000:
                old_path = log_path.with_suffix(".log.old")
                try:
                    if old_path.exists():
                        old_path.unlink()
                except Exception:
                    pass
                try:
                    log_path.rename(old_path)
                except Exception:
                    pass
        except Exception:
            pass

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        before_actor = before_meta.get(actor_slug) or {}
        before_roles = before_actor.get("roles") if isinstance(
            before_actor, dict) else None
        if not isinstance(before_roles, dict):
            before_roles = {}
        # after_roles = before_roles + новое назначение
        after_roles = dict(before_roles)
        after_roles[show_slug] = character_slug

        # Поиск коллизии: другой актёр уже играет этого character в этом show
        collision_actor = None
        try:
            for other_slug, other_data in (before_meta or {}).items():
                if other_slug == actor_slug:
                    continue
                if not isinstance(other_data, dict):
                    continue
                other_roles = other_data.get("roles")
                if not isinstance(other_roles, dict):
                    continue
                if other_roles.get(show_slug) == character_slug:
                    collision_actor = other_slug
                    break
        except Exception:
            pass

        # Stack trace (последние 10 фреймов до этого вызова)
        try:
            # extract_stack() возвращает фреймы от старейшего к текущему.
            # Берём срез [-12:-2]: убираем 2 последних (этот логгер и
            # set_actor_role) и оставляем максимум 10 предков.
            frames = traceback.extract_stack()[-12:-2]
            stack_lines = []
            for fr in frames:
                stack_lines.append(
                    f'  File "{fr.filename}", line {fr.lineno}, in {fr.name}')
            stack_text = "\n".join(stack_lines) if stack_lines else "  (empty)"
        except Exception:
            stack_text = "  (stack unavailable)"

        # Сборка лога
        lines = []
        lines.append("--- ENTRY START ---")
        lines.append(
            f"{ts} [set_actor_role] actor={actor_slug} "
            f"show={show_slug} character={character_slug}")
        lines.append(
            f"before_roles: {json.dumps(before_roles, ensure_ascii=False)}")
        lines.append(
            f"after_roles:  {json.dumps(after_roles, ensure_ascii=False)}")
        if collision_actor:
            lines.append(
                f"[COLLISION DETECTED] character={character_slug} "
                f"уже привязан к актёру: {collision_actor}")
        lines.append("caller_stack:")
        lines.append(stack_text)
        lines.append("--- ENTRY END ---")
        lines.append("")  # пустая строка-разделитель

        # Запись
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Никогда не падать из-за логирования
        pass


def get_actor_generated_refs_paths(slug: str) -> List[Path]:
    """Список character-рефов АКТИВНОГО сериала которые сгенерированы
    через данного актёра. Связь «актёр→персонаж в сериале» хранится в
    `actors.json:roles` и пишется при генерации рефа (CreateActorRefDialog).

    Используется кнопкой «🖼 Все референсы (N)» на карточке актёра:
    клик → попап с рефами того персонажа которого этот актёр играет в
    активном сериале (например akter_4 → `refs/characters/laura/*.jpg`).

    Если актёр ещё не сыграл ни одной роли в активном сериале (нет
    записи в actors.json:roles) — возвращает пустой список. После
    первой генерации рефа связь записывается и кнопка начнёт
    показывать рефы.

    Старая логика возвращала рефы из `refs/characters/<actor_slug>/`,
    но после переключения на character-папки (2026-05-04) этот путь
    содержит только legacy-артефакты от тестовых прогонов."""
    try:
        if not CHARACTERS_DIR.exists() or not CHARACTERS_DIR.is_dir():
            return []
        # PROJECT_ROOT — `actors/` лежит на 4 уровня выше CHARACTERS_DIR
        # (project / shows / <show> / refs / characters).
        project_root = CHARACTERS_DIR.parent.parent.parent.parent
        cur_show = get_current_show(project_root)
        if not cur_show:
            return []
        character = get_actor_role(project_root, slug, cur_show)
        if not character:
            return []
        char_dir = CHARACTERS_DIR / character
        if not char_dir.exists() or not char_dir.is_dir():
            return []
        exts = {".jpg", ".jpeg", ".png"}
        return sorted([f for f in char_dir.iterdir()
                       if f.is_file() and f.suffix.lower() in exts])
    except Exception:
        return []


def slugify_actor_name(name: str) -> str:
    """Превращает «Оля Петрова» в `olya_petrova` для имени папки.
    Транслит RU→EN + lowercase + замена пробелов/спецсимволов на _.
    Если результат пустой — fallback на 'actor'."""
    import re as _re
    tr = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
        'з':'z','и':'i','й':'i','к':'k','л':'l','м':'m','н':'n','о':'o',
        'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts',
        'ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
        'і':'i','ї':'yi','є':'ye','ґ':'g',
    }
    out = []
    for c in name.lower():
        out.append(tr.get(c, c))
    s = ''.join(out)
    s = _re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s or 'actor'


def create_actor(project_root: Path, display_name: str) -> str:
    """Создаёт папку нового актёра. Возвращает slug.
    Если slug коллизит — добавляет _2, _3 и т.д."""
    slug = slugify_actor_name(display_name)
    base = actors_dir(project_root)
    base.mkdir(parents=True, exist_ok=True)
    candidate = slug
    i = 2
    while (base / candidate).exists():
        candidate = f"{slug}_{i}"
        i += 1
    (base / candidate).mkdir()
    meta = read_actors_meta(project_root)
    meta[candidate] = {"display_name": display_name.strip() or candidate}
    write_actors_meta(project_root, meta)
    return candidate


def rename_actor(project_root: Path, slug: str, new_display_name: str) -> None:
    """Меняет ТОЛЬКО display_name в actors.json (папку не трогает —
    чтобы не ломать ссылки на фото из других мест)."""
    meta = read_actors_meta(project_root)
    if slug not in meta:
        meta[slug] = {}
    meta[slug]["display_name"] = (new_display_name or "").strip() or slug
    write_actors_meta(project_root, meta)


def delete_actor(project_root: Path, slug: str) -> None:
    """Удаляет актёра ЛОКАЛЬНО: папку `actors/<slug>/` со всеми фото
    + запись в `actors.json`. На GitHub попадёт через следующий
    `SendUpdateThread` (когда админ нажмёт «Отправить обновление»).

    Сгенерированные character-рефы в `shows/*/refs/characters/<slug>/`
    НЕ трогаем — это либо админская локальная работа (которую он сам
    почистит если хочет), либо у коллег это вообще их собственные
    рефы которые мы НИКОГДА не должны трогать (см. архитектуру в
    `.gitignore` для actors/)."""
    target_dir = actors_dir(project_root) / slug
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    meta = read_actors_meta(project_root)
    if slug in meta:
        del meta[slug]
        write_actors_meta(project_root, meta)


def add_photo_to_actor(project_root: Path, slug: str, src_path: Path) -> Path:
    """Копирует файл в папку актёра. Если файл с таким именем уже есть —
    добавляет суффикс _2, _3. Возвращает итоговый путь.

    Для `.heic` / `.heif` (iPhone photos) делаем конвертацию в `.jpg` через
    нативный macOS `sips` — Fast Gen API не принимает HEIC, и Qt без
    HEIF-плагина не покажет превью. На macOS `sips` всегда есть.

    EXIF Orientation (iPhone): фотки с телефона часто хранят пиксели в
    «канонической» ориентации + флаг «поверни на 90/180/270°». Qt этот
    флаг не применяет → юзер видит реф боком. Лечим через
    `PIL.ImageOps.exif_transpose` на этапе копирования: пиксели реально
    поворачиваются, флаг убирается, результат показывается правильно
    везде (Qt, Fast Gen API)."""
    target_dir = actors_dir(project_root) / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    base = src_path.stem
    suf_lower = src_path.suffix.lower()

    def _save_with_exif_normalized(dst: Path) -> bool:
        """Открывает src через PIL, применяет EXIF Orientation к пикселям,
        ресайзит до max 2048px по длинной стороне (4K-фотки айфона
        ужимаются с ~5MB до ~400KB), сохраняет в dst как JPEG quality=85.

        Зачем сжимаем: actors/ синкается через GitHub. 4K-фото актёров
        в репо — это 100MB+ для команды, никому не нужно. 2048px вполне
        хватает для обучения identity-рефа: лицо ~800px высотой, все
        анатомические детали (глаза, нос, рот) сохраняются.

        Возвращает True если успех."""
        try:
            from PIL import ImageOps
            img = PILImage.open(src_path)
            img = ImageOps.exif_transpose(img)  # реально поворачивает пиксели
            # Ресайз: max 2048px по длинной стороне, пропорции сохраняются.
            # thumbnail() — in-place, сохраняет aspect ratio, делает only
            # downscale (если фото уже меньше 2048 — не растягивает).
            MAX_DIM = 2048
            if max(img.size) > MAX_DIM:
                img.thumbnail((MAX_DIM, MAX_DIM), PILImage.Resampling.LANCZOS)
            # JPEG не поддерживает RGBA — конвертируем если нужно
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            # quality=85 — sweet spot для портретных JPEG: визуально
            # не отличить от 95, размер в 2-3 раза меньше. 90+ имеет
            # смысл только для печати.
            img.save(dst, 'JPEG', quality=85, optimize=True)
            return True
        except Exception:
            traceback.print_exc()
            return False

    # HEIC → JPEG конвертация. После sips прогоняем через PIL pipeline
    # (resize до 2048px + quality=85) — иначе iPhone 4K-фотки остаются
    # 5MB файлами после конвертации.
    if suf_lower in ('.heic', '.heif'):
        suf = '.jpg'
        candidate = target_dir / f"{base}{suf}"
        i = 2
        while candidate.exists():
            candidate = target_dir / f"{base}_{i}{suf}"
            i += 1
        # 1) sips → temp JPEG
        import subprocess
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False) as tmpf:
                tmp_jpg = Path(tmpf.name)
            r = subprocess.run(
                ["sips", "-s", "format", "jpeg",
                 str(src_path), "--out", str(tmp_jpg)],
                capture_output=True, timeout=30)
            if r.returncode != 0 or not tmp_jpg.exists() or tmp_jpg.stat().st_size == 0:
                # sips упал — копируем оригинал как есть с .heic
                candidate = candidate.with_suffix('.heic')
                shutil.copy2(src_path, candidate)
                try:
                    tmp_jpg.unlink(missing_ok=True)
                except Exception:
                    pass
                return candidate
            # 2) Прогоняем temp через PIL pipeline (resize + recompress).
            # Подменяем src_path в _save_with_exif_normalized через локальную
            # копию pipeline здесь — иначе пришлось бы делать closure.
            try:
                from PIL import ImageOps
                img = PILImage.open(tmp_jpg)
                img = ImageOps.exif_transpose(img)
                MAX_DIM = 2048
                if max(img.size) > MAX_DIM:
                    img.thumbnail((MAX_DIM, MAX_DIM),
                                  PILImage.Resampling.LANCZOS)
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                img.save(candidate, 'JPEG', quality=85, optimize=True)
            except Exception:
                traceback.print_exc()
                # PIL упал — копируем sips-результат без сжатия
                shutil.copy2(tmp_jpg, candidate)
            finally:
                try:
                    tmp_jpg.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()
            candidate = candidate.with_suffix('.heic')
            shutil.copy2(src_path, candidate)
        return candidate

    # JPG/JPEG/PNG/WEBP — нормализуем EXIF Orientation через PIL.
    # Если PIL по какой-то причине упал — fallback на shutil.copy2 (хотя
    # бы файл сохранён, юзер увидит косяк и сможет поправить).
    if suf_lower in ('.jpg', '.jpeg', '.png', '.webp'):
        # Все фото актёра нормализуем в JPEG — единый формат, единый размер,
        # единое поведение в Qt и в Fast Gen API. Расширение всегда .jpg.
        suf = '.jpg'
        candidate = target_dir / f"{base}{suf}"
        i = 2
        while candidate.exists():
            candidate = target_dir / f"{base}_{i}{suf}"
            i += 1
        if _save_with_exif_normalized(candidate):
            return candidate
        # PIL упал — копируем как есть, оставляем оригинальное расширение
        suf = src_path.suffix
        candidate = target_dir / f"{base}{suf}"
        while candidate.exists():
            candidate = target_dir / f"{base}_{i}{suf}"
            i += 1
        shutil.copy2(src_path, candidate)
        return candidate

    # Любое другое расширение (на всякий) — простое копирование.
    suf = src_path.suffix
    candidate = target_dir / f"{base}{suf}"
    i = 2
    while candidate.exists():
        candidate = target_dir / f"{base}_{i}{suf}"
        i += 1
    shutil.copy2(src_path, candidate)
    return candidate


def convert_heic_to_jpg(src) -> Optional[bytes]:
    """2026-06-24: ОБЩИЙ конвертер heic/heif → JPEG bytes (q90, EXIF-normalized).
    Гибрид C:
      1) primary — pillow_heif: register_heif_opener() вызван в main() на старте,
         поэтому PIL открывает heic нативно (кроссплатформенно, wheels Win/Mac/Linux);
      2) fallback — macOS sips (как в add_photo_to_actor) если PIL не смог;
      3) None — конвертера нет (Win без pillow_heif / битый файл) → caller отклоняет.

    Зачем общий: и location-DnD (этап 3), и будущая миграция add_photo_to_actor
    зовут ОДИН путь. Возвращает bytes готового JPEG (на диск НЕ пишет — caller сам).

    Cross-platform: pathlib.Path + PIL + io; sips ТОЛЬКО darwin, через
    **no_console_kwargs() (без чёрного cmd-окна на Win), без shell=True.
    """
    src = Path(src)
    # 1) primary: pillow_heif → PIL (heic откроется если opener зарегистрирован)
    try:
        from PIL import ImageOps
        img = PILImage.open(str(src))
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=90, optimize=True)
        return buf.getvalue()
    except Exception:
        pass
    # 2) fallback: sips (только macOS)
    if sys.platform == 'darwin':
        try:
            import subprocess
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tf:
                tmp_jpg = Path(tf.name)
            try:
                r = subprocess.run(
                    ['sips', '-s', 'format', 'jpeg',
                     str(src), '--out', str(tmp_jpg)],
                    capture_output=True, timeout=30, **no_console_kwargs())
                if (r.returncode != 0 or not tmp_jpg.exists()
                        or tmp_jpg.stat().st_size == 0):
                    return None
                from PIL import ImageOps
                img = PILImage.open(str(tmp_jpg))
                img = ImageOps.exif_transpose(img)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                buf = io.BytesIO()
                img.save(buf, 'JPEG', quality=90, optimize=True)
                return buf.getvalue()
            finally:
                try:
                    tmp_jpg.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()
            return None
    # 3) конвертера нет
    return None


def list_shows(project_root: Path) -> List[str]:
    """Список slug'ов сериалов в `shows/` (сортировка алфавитная)."""
    sd = shows_dir(project_root)
    if not sd.exists():
        return []
    return sorted(p.name for p in sd.iterdir() if p.is_dir() and not p.name.startswith("."))


def get_current_show(project_root: Path) -> Optional[str]:
    """Активный сериал из current_show.json. None если файла нет или пусто."""
    f = project_root / "current_show.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("current") or None
    except Exception:
        return None


def set_current_show(project_root: Path, show_name: str) -> None:
    """Записывает активный сериал в current_show.json."""
    f = project_root / "current_show.json"
    try:
        f.write_text(json.dumps({"current": show_name}, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except Exception:
        pass


def read_episodes_meta(show_root: Path) -> Dict:
    """Читает episodes.json из папки сериала. Возвращает {} если файла нет."""
    f = show_root / "episodes.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def list_show_characters(project_root: Path, show_slug: str) -> List[str]:
    """Список ПЕРСОНАЖЕЙ сериала — для дропдауна выбора при создании
    character-рефа на вкладке «Актёры».

    Источники (объединяются, дедуп, сортировка алфавитно):
      1. `shows/<show>/episodes.json` → `refs.characters` всех эпизодов.
         Запись может быть либо `"laura"`, либо `"laura/laura_prison_grey.jpg"`
         — берём префикс до `/` (имя папки персонажа).
      2. Существующие папки в `shows/<show>/refs/characters/` КРОМЕ тех
         которые совпадают по имени со slug'ом актёра в `actors/`.
         Актёр — реальный человек/исполнитель (имя придумывает админ,
         например «vova», «artem», «akter_5»). Персонаж — роль в сериале
         (имя приходит из сценария, например «mark», «laura»). Это разные
         namespace. Если в `refs/characters/` лежит папка с именем актёра
         (старая логика складывала рефы туда) — её фильтруем, чтобы в
         дропдауне выбора персонажа не показывались имена реальных людей."""
    if not show_slug:
        return []
    show_root = project_root / "shows" / show_slug
    names = set()
    # Источник 1 — episodes.json
    meta = read_episodes_meta(show_root)
    for ep_data in (meta or {}).values():
        if not isinstance(ep_data, dict):
            continue
        refs = ep_data.get("refs") or {}
        chars = refs.get("characters") or []
        if not isinstance(chars, list):
            continue
        for entry in chars:
            if not isinstance(entry, str) or not entry.strip():
                continue
            # entry может быть "laura" или "laura/laura_prison_grey.jpg" —
            # берём только префикс папки
            head = entry.split('/', 1)[0].strip()
            if head:
                names.add(head)
    # Список slug'ов актёров — для фильтрации папок-артефактов
    try:
        actor_slugs = set(list_actors(project_root))
    except Exception:
        actor_slugs = set()
    # Источник 2 — папки в refs/characters/, исключая совпадающие с актёрами
    chars_dir = show_root / "refs" / "characters"
    if chars_dir.exists() and chars_dir.is_dir():
        for p in chars_dir.iterdir():
            if p.is_dir() and not p.name.startswith('.'):
                if p.name in actor_slugs:
                    # Папка названа именем актёра (реального человека),
                    # а не персонажа сериала — в дропдаун не включаем.
                    continue
                names.add(p.name)
    return sorted(names)


def setup_fade_overlay(overlay: QFrame) -> 'QPropertyAnimation':
    """Подготавливает overlay к hover-показу. Изначально hide().

    БЫЛО: QGraphicsOpacityEffect + QPropertyAnimation для плавного fade.
    СТАЛО: простой show/hide без анимации. Анимация opacity через
    QGraphicsEffect вызывала визуальный «сдвиг» overlay на 1-2 пикселя
    при пересчёте + при первом показе layout strip-кнопок мог «прыгать».
    Юзер просил сначала плавность, но из-за нестабильности отказались
    от анимации hover-overlay.

    Возвращает None для совместимости со старым API (anim больше не нужен).
    """
    overlay.hide()
    overlay._fade_effect = None  # type: ignore
    overlay._fade_anim = None    # type: ignore
    return None


def fade_in(overlay: QFrame, anim):
    """Показывает overlay немедленно (без анимации)."""
    if overlay is None:
        return
    overlay.show()
    overlay.raise_()


def fade_out(overlay: QFrame, anim):
    """Скрывает overlay немедленно (без анимации)."""
    if overlay is None:
        return
    overlay.hide()


def apply_modal_dim(window):
    """Накладывает полупрозрачную плашку поверх главного окна и плавно
    проявляет (fade-in 220мс, OutCubic). Возвращает overlay чтобы потом
    убрать через `remove_modal_dim`. Используется когда показывается
    модальный диалог — даёт визуальный фокус на нём.
    """
    try:
        overlay = QFrame(window)
        overlay.setObjectName("modal-dim")
        overlay.setStyleSheet(
            "QFrame#modal-dim { background: rgba(8,4,16,0.55); border: none; }")
        overlay.setGeometry(0, 0, window.width(), window.height())
        overlay.show()
        overlay.raise_()
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        anim = QPropertyAnimation(effect, b"opacity", overlay)
        anim.setDuration(int(220 * _anim_speed_multiplier()))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutQuint)
        anim.start()
        overlay._fade_anim = anim  # удерживаем ссылку
        return overlay
    except Exception:
        traceback.print_exc()
        return None


def get_icon(name: str) -> QIcon:
    """Загружает SVG-иконку из `assets/icons/<name>.svg`. Работает и из
    исходников (Path(__file__).parent), и из PyInstaller-бандла (sys._MEIPASS).
    Кэширует результаты — Qt не сильно страдает от повторных QIcon, но не
    хочется каждый раз QIcon(str(path)) на каждой карточке."""
    if not hasattr(get_icon, '_cache'):
        get_icon._cache = {}
    if name in get_icon._cache:
        return get_icon._cache[name]
    try:
        base = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(__file__).parent
        icon_path = base / "assets" / "icons" / f"{name}.svg"
        if not icon_path.exists():
            # Фолбэк: project_root через QSettings (как в _build_ui)
            try:
                stored = get_stored_root()
                if stored:
                    alt = stored / "assets" / "icons" / f"{name}.svg"
                    if alt.exists():
                        icon_path = alt
            except Exception:
                pass
        ic = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
    except Exception:
        ic = QIcon()
    get_icon._cache[name] = ic
    return ic


def get_model_path(name: str) -> Optional[Path]:
    """2026-06-02: путь к бандлованной ML-модели `assets/models/<name>`.
    _MEIPASS-aware (как get_icon): работает из исходников (Path(__file__).parent)
    и из PyInstaller-бандла (sys._MEIPASS → Mac Contents/Resources, Win _internal).
    Фолбэк на project_root из QSettings. Возвращает Path если файл есть, иначе None.

    Используется детектором лиц YuNet (cv2.FaceDetectorYN) для попапа наложения
    сеток на сториборд. Модель — assets/models/face_detection_yunet_2023mar.onnx,
    бандлится через StoryboardStudio.spec datas."""
    try:
        base = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(__file__).parent
        p = base / "assets" / "models" / name
        if p.exists():
            return p
        try:
            stored = get_stored_root()
            if stored:
                alt = stored / "assets" / "models" / name
                if alt.exists():
                    return alt
        except Exception:
            pass
        return p if p.exists() else None
    except Exception:
        return None


class _SceneHighlighter:
    """2026-05-07: подсветка «СЦЕНА N:» / «SCENE N:» / «Сцена N:» в попапе
    оригинального сценария эпизода. Юзер хочет глазом мгновенно ловить
    границы сцен в монотонном тексте.

    Multi-language: ловим 3 варианта (CЦЕНА — RU/UK тот же корень,
    SCENE — EN). Регэксп case-insensitive. Доп. подсветка для «ЭПИЗОД N» /
    «EPISODE N» — заголовок эпизода в начале файла, чуть жирнее.

    Не наследует QSyntaxHighlighter в module-level чтобы не падать при
    импорте если PyQt6.QtGui не подгружен (для CLI/headless-режима).
    Класс реализует duck-typed интерфейс QSyntaxHighlighter ленивым
    созданием через _make_real_highlighter."""
    def __new__(cls, document):
        # Ленивое создание реального QSyntaxHighlighter подкласса.
        from PyQt6.QtGui import (
            QSyntaxHighlighter, QTextCharFormat, QColor, QFont)
        from PyQt6.QtCore import QRegularExpression

        class _Real(QSyntaxHighlighter):
            def __init__(self, doc):
                super().__init__(doc)
                # 2026-05-07 v3: тёплый приглушённый янтарный — мягче чем
                # ярко-жёлтый Stabilo, но всё равно «маркер»-эффект.
                # Foreground — кремово-золотой `#ffd47a`, background —
                # тёмно-янтарный `#3d2c14` (почти невидимый pill, но даёт
                # форму). Long-read friendly, глаз не режет.
                self._scene_fmt = QTextCharFormat()
                self._scene_fmt.setForeground(theme_qcolor("#ffd47a"))
                self._scene_fmt.setBackground(theme_qcolor("#3d2c14"))
                self._scene_fmt.setFontWeight(QFont.Weight.Bold)
                # Эпизод-заголовок: ярко-фиолетовый «#c9a8ff», bold —
                # юзер сказал что этот цвет «ещё нормально», оставляем.
                self._ep_fmt = QTextCharFormat()
                self._ep_fmt.setForeground(theme_qcolor("#c9a8ff"))
                self._ep_fmt.setFontWeight(QFont.Weight.Bold)
                # 2026-05-08: реплики в кавычках (диалоги) — спокойный
                # холодный голубой, italic. Не конфликтует со scene
                # (тёплый янтарь) и episode (фиолетовый).
                self._dialog_fmt = QTextCharFormat()
                self._dialog_fmt.setForeground(theme_qcolor("#7fb8ff"))
                self._dialog_fmt.setFontItalic(True)
                # 2026-05-07: ОБЯЗАТЕЛЬНО UseUnicodePropertiesOption —
                # без него `\b` в Qt не считает кириллицу word-character'ом
                # и `\bСЦЕНА\b` НЕ матчится. С Unicode-flag — работает.
                _opts = (
                    QRegularExpression.PatternOption.CaseInsensitiveOption
                    | QRegularExpression.PatternOption.UseUnicodePropertiesOption)
                # СЦЕНА (RU/UK совпадают) или SCENE (EN). Case-insensitive.
                self._scene_re = QRegularExpression(
                    r'\b(СЦЕНА|SCENE)\s*\d+\s*:?', _opts)
                # ЭПИЗОД (RU) / ЕПІЗОД (UK) / EPISODE (EN).
                self._ep_re = QRegularExpression(
                    r'\b(ЭПИЗОД|ЕПІЗОД|EPISODE)\s*\d+', _opts)
                # 2026-05-08: реплики в кавычках. Поддерживаем три стиля:
                # ASCII "..." (most common), curly “...” и ёлочки «...».
                # Захватываем минимально, без переноса строк.
                self._dialog_re = QRegularExpression(
                    r'"[^"\n]+"|“[^”\n]+”|«[^»\n]+»',
                    QRegularExpression.PatternOption.UseUnicodePropertiesOption)

            def highlightBlock(self, text):
                for regex, fmt in (
                        (self._ep_re, self._ep_fmt),
                        (self._scene_re, self._scene_fmt),
                        (self._dialog_re, self._dialog_fmt)):
                    it = regex.globalMatch(text)
                    while it.hasNext():
                        m = it.next()
                        self.setFormat(
                            m.capturedStart(), m.capturedLength(), fmt)

        return _Real(document)


def _finalize_pending_reboot(project_root: Path,
                              reboot_marker: Path) -> Optional[Tuple]:
    """2026-05-11 (v1.0.44): обработчик `pending_reboot.txt`.

    Маркер пишется PowerShell-helper'ом в bootstrap'е когда retry-loop
    не отвинтил handle на target onedir (Defender держит). Helper
    через MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT) планирует move/copy
    на следующий рестарт Windows и пишет в `pending_reboot.txt`:
        target_version=X.Y.Z
        scheduled_at=<ISO UTC>

    Логика финализации:
      1. Парсим target_version + scheduled_at.
      2. На Win получаем last boot time через ctypes
         `kernel32.GetTickCount64()` → boot_time = now - uptime_ms/1000.
      3. Если boot_time > scheduled_at → reboot ПРОИЗОШЁЛ после планирования
         → Windows применила MoveFileEx'ы из registry → bundle подменён →
         bump version.json, удаляем markers, возвращаем
         ('reboot_install_success', target_version, None, None).
      4. Иначе → reboot ещё не было → возвращаем
         ('reboot_pending', target_version, None, scheduled_at_unix)
         чтобы MainWindow показал inline-баннер «нужна перезагрузка».
      5. На Mac (where defer-marker не должен появляться) — defensive
         cleanup маркера, возвращаем None.
    """
    if sys.platform != 'win32':
        # Defensive cleanup на не-Windows — defer-маркер тут не создаётся.
        try:
            reboot_marker.unlink()
        except Exception:
            pass
        return None

    try:
        content = reboot_marker.read_text(encoding='utf-8').strip()
        kv = {}
        for line in content.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                kv[k.strip()] = v.strip()
        target_version = kv.get('target_version', '?')
        scheduled_at_str = kv.get('scheduled_at', '')
        # Parse ISO UTC timestamp (e.g. "2026-05-11T15:30:00Z").
        scheduled_at_unix: Optional[float] = None
        if scheduled_at_str:
            try:
                # Python 3.7+: fromisoformat не понимает 'Z' до 3.11 → strip.
                s = scheduled_at_str.rstrip('Z')
                dt = datetime.datetime.fromisoformat(s)
                # Treat as UTC. Convert to Unix epoch.
                scheduled_at_unix = dt.replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                traceback.print_exc()

        # Last boot time via ctypes GetTickCount64.
        boot_time_unix: Optional[float] = None
        try:
            import ctypes as _ct
            ticks_ms = _ct.windll.kernel32.GetTickCount64()
            boot_time_unix = time.time() - (ticks_ms / 1000.0)
        except Exception:
            traceback.print_exc()

        if (scheduled_at_unix is not None and
                boot_time_unix is not None and
                boot_time_unix > scheduled_at_unix):
            # Reboot произошёл после планирования → MoveFileEx применился →
            # install completed. Bump version.json и чистим markers.
            try:
                vfile = project_root / "version.json"
                data = (json.loads(vfile.read_text(encoding='utf-8'))
                        if vfile.exists() else {})
                data["app_version"] = target_version
                data["version"] = target_version  # legacy compat
                tmp = vfile.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding='utf-8',
                )
                import os as _os
                _os.replace(str(tmp), str(vfile))
            except Exception:
                traceback.print_exc()
            for marker in ("pending_reboot.txt", "pending_version.txt",
                           "pending_rollback.txt"):
                try:
                    p = project_root / marker
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            return ('reboot_install_success', target_version, None, None)

        # Reboot ещё не было → показываем баннер.
        return ('reboot_pending', target_version, None, scheduled_at_unix)
    except Exception:
        traceback.print_exc()
        return None


def heal_stale_decisions(project_root: Path) -> int:
    """2026-05-11 (v1.0.45, expanded v1.0.46): self-heal битых
    filename'ов в `episodes.json[<ep>].refs_decisions` при старте Studio.

    Зачем: при регенерации / collision-resolve / character outfit-pick'е
    в decisions мог остаться устаревший filename. Симптомы:
      - extension changed (.png в decisions, .jpg на диске) → ломает CTA
      - character filename без folder prefix (legacy bug до v1.0.46) →
        CTA hide потому что `<file>` без `<folder>/<file>` не находится
      - location/object collision-resolve переименовал slug в `<slug>_N`,
        но decisions остался под старым slug с filename первого ep'а →
        storyboard pipeline берёт чужой файл

    Алгоритм по kind (приоритет веток сверху вниз):

      **character:**
        - filename с `/` И файл существует → no-op
        - filename с `/` но файла нет → outfit-safety SKIP + log
        - filename БЕЗ `/` (legacy v1.0.45-) → ищем `refs/characters/
          <slug>/<filename>`. Найдено → heal на `<slug>/<filename>`,
          slug-based lookup (НЕ scan'ить все subdirs). Не найдено → log.

      **location/object:**
        A) [no-op] filename существует И в manifest `refs.<sub>` → ok.
        B) [heal-disk-glob] filename НЕ существует → ищем
           `<stem>.<{jpg,jpeg,png,webp}>` И `<slug>.<{...}>` в той же
           подпапке.
        C) [heal-suffix-variant] filename НЕ существует И disk-glob не
           помог → пробуем suffix variants `<slug>_2..._9.<ext>`. Найдено
           РОВНО ОДНО → переименовать bucket key + filename. Найдено
           несколько → ambiguity SKIP + log. (Skip если slug уже ends
           in _N — не плодим nested suffixes.)
        D) [heal-manifest-driven] (v1.0.46) filename СУЩЕСТВУЕТ на диске
           НО НЕ в manifest `episodes.json[ep].refs.<sub>` — это значит
           decisions указывает на чужой файл (от другого эпизода после
           collision-resolve). Ищем в manifest entries со slug-prefix
           `<slug>(_[2-9])?\.<ext>`. РОВНО ОДНО → heal к manifest variant
           (с переименованием bucket key если slug отличается). Несколько
           → ambiguity SKIP. Пусто → leave as-is (file существует, может
           быть валидный сценарий).
        E) [heal-skip] ничего не помогло → log skip, user должен relink.

      Атомарная запись через temp + os.replace для каждого show.

    Bucket key rename: для location/object suffix/manifest heal слаг
    в decisions меняется (например `house_corridor` → `house_corridor_2`).
    Применяется ПОСЛЕ итерации через `pending_renames` list — нельзя
    модифицировать dict во время iteration.

    Возвращает: число эпизодов, в которых что-то реально обновили.

    Cross-platform: pure Python. Mac и Win одинаково.
    """
    sys.stderr.write(
        f"[heal] starting scan, project_root={project_root}\n")
    healed_eps = 0
    try:
        shows_root = project_root / "shows"
        if not shows_root.is_dir():
            return 0
        sub_map = {'location': 'locations',
                   'object': 'objects',
                   'character': 'characters'}
        exts = ('.jpg', '.jpeg', '.png', '.webp')
        import re as _re
        for show_dir in shows_root.iterdir():
            if not show_dir.is_dir():
                continue
            ep_meta = show_dir / "episodes.json"
            if not ep_meta.exists():
                continue
            try:
                data = json.loads(ep_meta.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            changed = False
            for ep_id, ep in data.items():
                if not isinstance(ep, dict):
                    continue
                decisions = ep.get('refs_decisions')
                if not isinstance(decisions, dict):
                    continue
                ep_changed = False
                for kind in ('location', 'object', 'character'):
                    bucket = decisions.get(kind)
                    if not isinstance(bucket, dict):
                        continue
                    sub = sub_map[kind]
                    sub_base = show_dir / "refs" / sub
                    if not sub_base.is_dir():
                        continue
                    # Manifest (только для location/object) — для (7b)
                    # manifest-driven branch.
                    manifest_list: List = []
                    if kind in ('location', 'object'):
                        refs_block = ep.get('refs')
                        if isinstance(refs_block, dict):
                            ml = refs_block.get(sub)
                            if isinstance(ml, list):
                                manifest_list = [
                                    s for s in ml if isinstance(s, str)]
                    # Pending bucket-key renames для location/object —
                    # применяем ПОСЛЕ iteration (нельзя мутировать dict
                    # во время for-loop).
                    pending_renames: List = []
                    for slug, entry in list(bucket.items()):
                        if not isinstance(entry, dict):
                            continue
                        if entry.get('decision') != 'linked':
                            continue
                        old_fn = entry.get('filename', '') or ''
                        if not old_fn:
                            continue

                        # ── CHARACTER ──
                        if kind == 'character':
                            if '/' in old_fn:
                                folder_part, _, file_part = old_fn.partition('/')
                                if not folder_part or not file_part:
                                    continue
                                full_path = sub_base / folder_part / file_part
                                if full_path.exists() and full_path.is_file():
                                    continue  # OK
                                # Outfit-safety: НЕ подменяем.
                                sys.stderr.write(
                                    f"[heal-character-skip] {show_dir.name}/"
                                    f"{ep_id}/character/{slug}: outfit file "
                                    f"not found on disk ({old_fn}), skipping "
                                    f"(user must relink via UI)\n")
                            else:
                                # v1.0.46: filename без folder prefix —
                                # slug-based lookup (НЕ scan все subdirs).
                                candidate = sub_base / slug / old_fn
                                if (candidate.exists()
                                        and candidate.is_file()):
                                    new_fn = f"{slug}/{old_fn}"
                                    entry['filename'] = new_fn
                                    ep_changed = True
                                    sys.stderr.write(
                                        f"[heal-character-slug] "
                                        f"{show_dir.name}/{ep_id}/character/"
                                        f"{slug}: {old_fn} → {new_fn} "
                                        f"(added folder prefix)\n")
                                else:
                                    sys.stderr.write(
                                        f"[heal-character-skip] "
                                        f"{show_dir.name}/{ep_id}/character/"
                                        f"{slug}: legacy filename without "
                                        f"folder, file refs/characters/"
                                        f"{slug}/{old_fn} not found, "
                                        f"skipping\n")
                            continue

                        # ── LOCATION / OBJECT ──
                        full_path = sub_base / old_fn
                        file_exists = (full_path.exists()
                                       and full_path.is_file())

                        # (A) [no-op] file exists AND in manifest (or no
                        # manifest available).
                        if file_exists and (
                                not manifest_list or old_fn in manifest_list):
                            continue

                        # (D) [heal-manifest-driven]: file existsНО not in
                        # manifest. Find candidates in manifest with same
                        # slug-prefix.
                        if file_exists and manifest_list:
                            slug_re = _re.compile(
                                rf'^{_re.escape(slug)}(_[2-9])?$')
                            candidates_md = []
                            for m_entry in manifest_list:
                                m_stem = Path(m_entry).stem
                                if m_stem == slug:
                                    # base slug в manifest — это и есть
                                    # old_fn? Уже проверили above (in
                                    # manifest → no-op). Если нет —
                                    # значит current decisions filename
                                    # отличается от manifest base.
                                    if m_entry != old_fn:
                                        candidates_md.append(m_entry)
                                elif slug_re.match(m_stem):
                                    candidates_md.append(m_entry)
                            if len(candidates_md) == 1:
                                new_fn = candidates_md[0]
                                new_stem = Path(new_fn).stem
                                if new_stem != slug:
                                    # Bucket key rename
                                    pending_renames.append(
                                        (slug, new_stem, new_fn))
                                    sys.stderr.write(
                                        f"[heal-manifest-driven] "
                                        f"{show_dir.name}/{ep_id}/{kind}/"
                                        f"{slug}: {old_fn} → {new_stem}/"
                                        f"{new_fn} (key+filename rename, "
                                        f"manifest match)\n")
                                else:
                                    entry['filename'] = new_fn
                                    ep_changed = True
                                    sys.stderr.write(
                                        f"[heal-manifest-driven] "
                                        f"{show_dir.name}/{ep_id}/{kind}/"
                                        f"{slug}: {old_fn} → {new_fn} "
                                        f"(filename only)\n")
                            elif len(candidates_md) > 1:
                                sys.stderr.write(
                                    f"[heal-manifest-driven] "
                                    f"{show_dir.name}/{ep_id}/{kind}/"
                                    f"{slug}: ambiguous manifest candidates "
                                    f"{candidates_md}, skipping\n")
                            else:
                                # 0 candidates: file exists, no manifest
                                # slug-prefix match. Возможно legitimate
                                # (slug=X, manifest=[Y.jpg] без явной
                                # связи) — leave as-is, но логируем чтобы
                                # юзер мог проверить если что-то странное.
                                sys.stderr.write(
                                    f"[heal-manifest-driven] "
                                    f"{show_dir.name}/{ep_id}/{kind}/"
                                    f"{slug}: file exists but no "
                                    f"slug-prefix match in manifest "
                                    f"({manifest_list}), leaving as-is\n")
                            continue

                        # File doesn't exist — (B) [heal-disk-glob]
                        new_fn = None
                        log_branch = None
                        old_stem = Path(old_fn).stem
                        for ext in exts:
                            cand = sub_base / f"{old_stem}{ext}"
                            if cand.exists() and cand.is_file():
                                new_fn = f"{old_stem}{ext}"
                                log_branch = 'heal-disk-glob'
                                break
                        if new_fn is None:
                            for ext in exts:
                                cand = sub_base / f"{slug}{ext}"
                                if cand.exists() and cand.is_file():
                                    new_fn = f"{slug}{ext}"
                                    log_branch = 'heal-disk-glob'
                                    break

                        # (C) [heal-suffix-variant] — slug_2..._9.
                        new_slug_to_rename = None
                        if (new_fn is None
                                and not _re.search(r'_[2-9]$', slug)):
                            suffix_matches = []
                            for n in range(2, 10):
                                variant_stem = f"{slug}_{n}"
                                for ext in exts:
                                    cand = sub_base / f"{variant_stem}{ext}"
                                    if cand.exists() and cand.is_file():
                                        suffix_matches.append(
                                            (variant_stem,
                                             f"{variant_stem}{ext}"))
                                        break
                            if len(suffix_matches) == 1:
                                new_slug_to_rename, new_fn = suffix_matches[0]
                                log_branch = 'heal-suffix-variant'
                            elif len(suffix_matches) > 1:
                                sys.stderr.write(
                                    f"[heal-suffix-variant] "
                                    f"{show_dir.name}/{ep_id}/{kind}/"
                                    f"{slug}: ambiguous variants "
                                    f"{suffix_matches}, skipping\n")

                        if new_fn and new_slug_to_rename:
                            pending_renames.append(
                                (slug, new_slug_to_rename, new_fn))
                            sys.stderr.write(
                                f"[{log_branch}] {show_dir.name}/{ep_id}/"
                                f"{kind}/{slug}: {old_fn} → "
                                f"{new_slug_to_rename}/{new_fn} "
                                f"(bucket key rename)\n")
                        elif new_fn:
                            entry['filename'] = new_fn
                            ep_changed = True
                            sys.stderr.write(
                                f"[{log_branch}] {show_dir.name}/{ep_id}/"
                                f"{kind}/{slug}: {old_fn} → {new_fn}\n")
                        else:
                            sys.stderr.write(
                                f"[heal-skip] {show_dir.name}/{ep_id}/"
                                f"{kind}/{slug}: no matching file for "
                                f"{old_fn}, skipping (user must relink "
                                f"via UI)\n")

                    # Apply pending bucket-key renames AFTER iteration.
                    for old_slug, new_slug, new_fn in pending_renames:
                        old_entry = bucket.pop(old_slug, None)
                        if old_entry is None:
                            continue
                        if not isinstance(old_entry, dict):
                            old_entry = {'decision': 'linked'}
                        old_entry['filename'] = new_fn
                        bucket[new_slug] = old_entry
                        ep_changed = True

                    # 2026-05-11 (v1.0.48) [heal-twin-cleanup] — для
                    # location/object. После collision-resolve в decisions
                    # могут оказаться ДВЕ записи под разными ключами,
                    # указывающие на ОДИН файл:
                    #   K1 = chat-marker name (e.g., 'house_corridor')
                    #   K2 = stem от collision-renamed file (e.g.,
                    #        'house_corridor_2', stem от 'house_corridor_2.jpg')
                    # Оба linked, оба filename='house_corridor_2.jpg'.
                    # Тогда `list_episode_refs` отрисует ДВЕ карточки
                    # одинакового файла в UI References (img7+img8 кейс).
                    # Лечим: оставляем K1 (chat marker, "правдивее"),
                    # удаляем K2 (technical artifact).
                    if kind in ('location', 'object'):
                        keys_to_delete: List = []
                        for k2 in list(bucket.keys()):
                            entry2 = bucket.get(k2)
                            if not isinstance(entry2, dict):
                                continue
                            if entry2.get('decision') != 'linked':
                                continue
                            # K2 must end in `_[2-9]`
                            mt = _re.search(r'^(.+?)_([2-9])$', k2)
                            if not mt:
                                continue
                            k1 = mt.group(1)
                            entry1 = bucket.get(k1)
                            if not isinstance(entry1, dict):
                                continue
                            if entry1.get('decision') != 'linked':
                                continue
                            fn1 = entry1.get('filename') or ''
                            fn2 = entry2.get('filename') or ''
                            if not fn1 or fn1 != fn2:
                                continue
                            # Дополнительная safety: stem(filename)
                            # должен совпадать с K2 (это значит K2 —
                            # technical artifact от collision-resolve,
                            # а не legitimate slug с _N суффиксом).
                            if Path(fn2).stem != k2:
                                continue
                            keys_to_delete.append((k1, k2, fn2))
                        for k1, k2, fn in keys_to_delete:
                            bucket.pop(k2, None)
                            ep_changed = True
                            sys.stderr.write(
                                f"[heal-twin-cleanup] {show_dir.name}/"
                                f"{ep_id}/{kind}: removed twin {k2!r} "
                                f"(kept {k1!r}, both pointed to {fn!r})\n")
                if ep_changed:
                    changed = True
                    healed_eps += 1
            if changed:
                # Atomic write
                try:
                    tmp = ep_meta.with_suffix('.json.tmp')
                    tmp.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding='utf-8')
                    import os as _os
                    _os.replace(str(tmp), str(ep_meta))
                except Exception:
                    traceback.print_exc()
    except Exception:
        traceback.print_exc()
    sys.stderr.write(
        f"[heal] scan complete, healed_eps={healed_eps}\n")
    return healed_eps


def finalize_pending_update(project_root: Path) -> Optional[Tuple]:
    """Финализирует авто-обновление после рестарта через bootstrap-скрипт.

    Вызывается из MainWindow.__init__ при каждом старте Studio. Делает:

      1. Маркеры от прошлого DownloadAppUpdateThread:
         - `pending_reboot.txt` (v1.0.44+) — bootstrap'у не удалось подменить
           bundle сразу (Defender лочит), вместо ошибки запланирован move
           через MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT). Содержит
           `target_version=X.Y.Z` и `scheduled_at=<ISO UTC>`. Сравниваем
           `scheduled_at` с last boot time через ctypes GetTickCount64:
           - если boot был ПОСЛЕ scheduled → reboot произошёл → MoveFileEx
             применился Windows'ом до user logon → install completed →
             bump version.json + удаляем все markers → возвращаем
             ('reboot_install_success', target_version, None, None).
           - если boot ДО scheduled → reboot ещё не было → возвращаем
             ('reboot_pending', target_version, None, scheduled_at_unix)
             чтобы MainWindow показал inline-баннер «нужна перезагрузка».
         - `pending_rollback.txt` (содержит OLD app_version) — если bat
           ЗАВЕРШИЛСЯ УСПЕШНО, он сам удалил этот файл. Если файл ОСТАЛСЯ
           → bat упал на середине → подмена .exe не удалась →
           откатываем `version.json[app_version]` на старое значение и
           возвращаем ('update_failed', attempted_version, log_path, None)
           чтобы MainWindow показал popup со ссылкой на ручный Installer.
         - `pending_version.txt` (содержит NEW app_version) — пишется
           всегда вместе с rollback. При success-сценарии: только этот
           файл существует → обновляем `version.json[app_version]` на
           NEW и удаляем оба маркера.

      2. Чистит старые update-папки `storyboard_update_*` в системном
         temp. Bootstrap не удаляет их сам (с 2026-05-09 — оставляет
         для bootstrap.log). Чистим всё кроме самой свежей.

      3. Чистит резидуальные `.exe.old` / `.app.old` рядом с текущим
         бандлом — на случай если когда-то использовался старый
         rename trick.

    Возвращает:
        None — обычный случай (нет update либо clean success).
        4-tuple (state, target_version, log_path, scheduled_at) где
        state ∈ {'update_failed', 'reboot_pending', 'reboot_install_success'}.
        MainWindow в showEvent / на старте обрабатывает по state.

    Все операции внутри try/except — Studio не должна падать при старте
    из-за проблем с update-инфраструктурой.
    """
    # 2026-05-11 (v1.0.44): сначала проверяем pending_reboot.txt — это
    # «defer-mode» маркер от RM API + MoveFileEx escalation в bootstrap.
    # Если есть — определяем, был ли реальный рестарт Windows после
    # планирования (boot_time > scheduled_at), и либо финализируем install
    # либо ждём reboot.
    try:
        reboot_marker = project_root / "pending_reboot.txt"
        if reboot_marker.exists():
            return _finalize_pending_reboot(project_root, reboot_marker)
    except Exception:
        traceback.print_exc()

    update_failed: Optional[Tuple] = None
    try:
        rollback_marker = project_root / "pending_rollback.txt"
        version_marker = project_root / "pending_version.txt"

        if rollback_marker.exists():
            # FAILURE PATH — bat упал, не успел удалить rollback marker.
            try:
                old_version = rollback_marker.read_text(encoding='utf-8').strip()
                attempted = ""
                if version_marker.exists():
                    attempted = version_marker.read_text(encoding='utf-8').strip()
                # Откатываем version.json[app_version] на старое значение.
                if old_version:
                    vfile = project_root / "version.json"
                    try:
                        data = (json.loads(vfile.read_text(encoding='utf-8'))
                                if vfile.exists() else {})
                        data["app_version"] = old_version
                        tmp = vfile.with_suffix(".json.tmp")
                        tmp.write_text(
                            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding='utf-8',
                        )
                        import os as _os
                        _os.replace(str(tmp), str(vfile))
                    except Exception:
                        traceback.print_exc()
                # Найти лог bootstrap'а в %TEMP% (последний по mtime —
                # это и есть тот что только что упал).
                # TODO v1.0.56: с v1.0.55 update_dir выбирается через
                # DownloadAppUpdateThread._choose_update_dir() в
                # %LOCALAPPDATA%\Temp\StoryboardStudio\ — НЕ всегда
                # совпадает с tempfile.gettempdir(). Нужно искать
                # bootstrap.log и в новом пути тоже:
                #   shared_dir/Temp/StoryboardStudio/storyboard_update_*
                #   + tempfile.gettempdir()/storyboard_update_* (legacy).
                # Также cleanup старых папок (ниже на ~1396) нужно
                # расширить аналогично.
                log_path: Optional[Path] = None
                try:
                    import tempfile as _tf
                    tmp_root = Path(_tf.gettempdir())
                    candidates = sorted(
                        tmp_root.glob("storyboard_update_*/bootstrap.log"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
                    if candidates:
                        log_path = candidates[0]
                except Exception:
                    pass
                update_failed = ('update_failed', attempted or "?", log_path, None)
                # Удалить markers (мы уже обработали fail).
                try:
                    rollback_marker.unlink()
                except Exception:
                    pass
                try:
                    if version_marker.exists():
                        version_marker.unlink()
                except Exception:
                    pass
            except Exception:
                traceback.print_exc()
        elif version_marker.exists():
            # SUCCESS PATH — bat сработал, удалил rollback marker.
            # Нам осталось обновить version.json на NEW.
            new_version = version_marker.read_text(encoding='utf-8').strip()
            if new_version:
                vfile = project_root / "version.json"
                try:
                    data = (json.loads(vfile.read_text(encoding='utf-8'))
                            if vfile.exists() else {})
                    data["app_version"] = new_version
                    tmp = vfile.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding='utf-8',
                    )
                    import os as _os
                    _os.replace(str(tmp), str(vfile))
                except Exception:
                    traceback.print_exc()
            try:
                version_marker.unlink()
            except Exception:
                pass
    except Exception:
        traceback.print_exc()

    try:
        import tempfile as _tf
        tmp_root = Path(_tf.gettempdir())
        # Если у нас есть active log_path для popup'а — не трогаем его dir,
        # юзер кликнет на ссылку «Открыть лог».
        # update_failed = (state, version, log_path, scheduled_at), индекс 2 = log_path.
        protected_dir = None
        if update_failed is not None and update_failed[2] is not None:
            protected_dir = update_failed[2].parent
        for p in tmp_root.glob("storyboard_update_*"):
            try:
                if p.is_dir() and p != protected_dir:
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if getattr(sys, 'frozen', False):
            # 2026-05-08: на Win Studio теперь onedir (папка), при апдейте
            # bootstrap переименовывает старую папку в `.old` и копирует
            # новую. Очищаем `.old` папки рядом с текущим бандлом — это
            # папка где лежит .exe и её родитель (если onedir структура).
            exe_dir = Path(sys.executable).parent
            cleanup_dirs = [exe_dir, exe_dir.parent]
            for cleanup_dir in cleanup_dirs:
                try:
                    for p in cleanup_dir.glob("*.old"):
                        try:
                            if p.is_dir():
                                shutil.rmtree(p, ignore_errors=True)
                            else:
                                p.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception:
        pass

    return update_failed


def sync_pipeline_py_to_project(project_root: Path) -> None:
    """Копирует `pipeline.py` из bundle (или dev-корня) в `project_root`.

    Зачем: AutonomousGenThread запускает `claude -p ...` с
    `cwd=project_root`. Агент через Bash tool вызывает
    `python3 pipeline.py generate <name> "<prompt>"`. Если pipeline.py
    отсутствует в project_root — субпроцесс падает с «pipeline.py not
    found in working directory». Installer исторически НЕ копировал
    pipeline.py в project_root (`ALLOW_FILES = {version.json}` до
    2026-05-09).

    Плюс self-healing skew: после auto-update bundle содержит свежий
    pipeline.py, а в project_root остаётся старый. Этот синк убирает
    skew — каждый запуск Studio overwrite'ит project pipeline.py из
    bundle если содержимое отличается.

    Все ошибки молча проглатываются — Studio не должна падать на старте
    из-за проблем с этой синхронизацией.
    """
    try:
        if getattr(sys, 'frozen', False):
            # PyInstaller datas: на Mac .app/Contents/Resources, на Win
            # onedir — _internal/. sys._MEIPASS указывает на корень.
            bundle_root = Path(getattr(sys, '_MEIPASS', ''))
        else:
            # Dev-режим: pipeline.py рядом с storyboard_app.py.
            bundle_root = Path(__file__).resolve().parent
        # 2026-06-09: синкаем pipeline.py И key_pool.py (диспетчер пула ключей) —
        # оба нужны CLI: pipeline.py делает `from key_pool import next_key`.
        # Цикл с `continue` (а не `return`), чтобы skip-identical одного файла
        # НЕ пропускал копирование второго. Механизм копирования прежний.
        for _name in ("pipeline.py", "generate_storyboards.py", "key_pool.py"):
            src = bundle_root / _name
            if not src.exists() or not src.is_file():
                continue  # bundle не содержит этот файл — пропускаем
            dst = project_root / _name
            try:
                # Если target существует и идентичен — не трогаем mtime.
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    if src.read_bytes() == dst.read_bytes():
                        continue
                shutil.copy2(str(src), str(dst))
            except Exception:
                traceback.print_exc()
    except Exception:
        traceback.print_exc()


def read_bundled_text(rel_path: str, default: str = "") -> str:
    """Читает текст из bundle (frozen) или project root (dev).

    Универсальный read-only паттерн для bundled data files (instructions,
    template-промпты, конфиги). Cross-platform — на Mac берёт из
    `Contents/Resources/<rel_path>`, на Win onedir — из
    `_internal/<rel_path>`. В обоих случаях `sys._MEIPASS` указывает на
    корень bundle. В dev-режиме (запуск .py из исходников) — из папки
    рядом с `storyboard_app.py` (project_root).

    Args:
        rel_path: путь относительно корня bundle / project root.
                  Например: "instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md".
        default:  что вернуть если файл не найден / ошибка чтения.
                  По умолчанию — пустая строка (вызывающая сторона
                  должна делать fallback на свою hard-coded версию).

    Returns:
        Содержимое файла в utf-8 или `default` при ошибке.

    История: v1.0.66 — добавлено для загрузки ГЛАВНАЯ_ИНСТРУКЦИЯ.md в
    agents/montage_prompts.py. Паттерн уже используется в проекте:
    `sync_pipeline_py_to_project` (выше) делает copy-on-startup,
    `get_icon` ([storyboard_app.py:760+]) — in-memory access для SVG.
    Эта функция — обобщённый in-memory text reader. Без кэша — кэширование
    отдаётся вызывающей стороне (agents/instruction_loader.py).
    """
    try:
        if getattr(sys, 'frozen', False):
            # PyInstaller bundle: на Mac .app/Contents/Resources,
            # на Win onedir — _internal/. sys._MEIPASS = корень.
            bundle_root = Path(getattr(sys, '_MEIPASS', ''))
        else:
            # Dev-режим: файлы лежат рядом с storyboard_app.py.
            bundle_root = Path(__file__).resolve().parent
        target = bundle_root / rel_path
        if not target.exists() or not target.is_file():
            return default
        return target.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return default


def no_console_kwargs() -> dict:
    """Возвращает kwargs для subprocess.run/Popen чтобы НЕ показывать
    чёрное окно cmd на Windows. На Mac/Linux возвращает пустой dict —
    поведение subprocess'а не меняется.

    Применять КО ВСЕМ subprocess.run / subprocess.Popen которые могут
    запуститься на Win. Без этого Windows показывает чёрное cmd-окно
    на доли секунды при каждом запуске subprocess'а — особенно заметно
    на периодических вызовах (раньше QTimer'ы дёргали git status каждые
    5 сек, claude auth status каждые 90 сек — каждый раз окно).

    Использование:
        r = subprocess.run([...], capture_output=True, **no_console_kwargs())
        proc = subprocess.Popen([...], **no_console_kwargs())
    """
    if sys.platform == 'win32':
        return {'creationflags': 0x08000000}  # CREATE_NO_WINDOW
    return {}


def reveal_in_file_manager(path) -> None:
    """Показать файл в системном файловом менеджере с ВЫДЕЛЕНИЕМ (reveal-and-select):
    открыть папку и подсветить сам файл. Кросс-платформенно:
      • macOS:   open -R <file>            (Finder, выделяет файл)
      • Windows: explorer /select,<file>   (Explorer, выделяет файл)
      • Linux:   xdg-open <папка>          (reveal-select не стандартизован →
                                            открываем папку, без выделения)

    Особенности Windows: explorer требует «/select,<путь>» ОДНОЙ строкой-аргументом
    (через запятую, без пробела) и ОБРАТНЫЕ слеши — формируем str(Path) который на
    Win даёт '\\'. explorer ВОЗВРАЩАЕТ НЕНУЛЕВОЙ код даже при успехе → используем
    Popen и НЕ проверяем returncode. no_console_kwargs() — без чёрного cmd-окна.

    Любая ошибка глушится (UI-удобство, не критичный путь). 2026-06-22."""
    try:
        from pathlib import Path
        p = Path(str(path))
        if sys.platform == 'darwin':
            subprocess.run(["open", "-R", str(p)], **no_console_kwargs())
        elif sys.platform == 'win32':
            # ОДНА строка-аргумент: "/select,C:\\...\\file.jpg". Popen, без check.
            subprocess.Popen(["explorer", f"/select,{str(p)}"], **no_console_kwargs())
        else:
            # Linux: reveal-select нет — открываем родительскую папку.
            subprocess.run(["xdg-open", str(p.parent)], **no_console_kwargs())
    except Exception:
        traceback.print_exc()


def block_wheel_event(widget):
    """Блокирует прокрутку колесом мыши на виджете — событие проходит
    дальше к родителю (например QScrollArea будет скроллить страницу,
    а не менять значение слайдера / комбобокса под курсором).

    Применяется ко ВСЕМ настройкам в Settings: слайдер скорости анимаций,
    дропдаун модели, и любым будущим настройкам которые юзер может
    случайно изменить колёсиком когда хочет проскроллить страницу."""
    try:
        widget.wheelEvent = lambda ev: ev.ignore()
    except Exception:
        traceback.print_exc()


def cross_fade_swap(grab_widget, parent_for_overlay,
                     switch_callback=None, duration: int = 300):
    """Cross-fade переход между двумя состояниями виджета.

    1. Делает snapshot (QPixmap) текущего вида `grab_widget` ДО переключения
    2. Накладывает snapshot как QLabel поверх `parent_for_overlay` на тех же
       координатах что у `grab_widget` (через mapTo)
    3. Вызывает `switch_callback()` — переключение state (setCurrentIndex,
       display_block, show_refs_view и т.п.). Новое состояние СРАЗУ под snapshot
    4. Запускает fade-out snapshot (opacity 1→0) → новый widget постепенно
       проявляется через прозрачность

    Эффект: старое плавно исчезает, новое плавно появляется одновременно
    (true cross-fade, а не просто fade-in нового).

    Если `switch_callback=None` — Qt сам переключит виджет (для табов через
    tabBarClicked сигнал). Snapshot всё равно скроет переключение.
    """
    try:
        from PyQt6.QtCore import QPoint
        # 1. Snapshot текущего вида
        pix = grab_widget.grab()
        if pix.isNull():
            # Fallback — просто переключение без анимации
            if switch_callback:
                switch_callback()
            return None
        snap = QLabel(parent_for_overlay)
        snap.setPixmap(pix)
        snap.setScaledContents(True)
        # Позиционируем snapshot ровно над grab_widget в координатах parent
        top_left = grab_widget.mapTo(parent_for_overlay, QPoint(0, 0))
        snap.setGeometry(top_left.x(), top_left.y(),
                          grab_widget.width(), grab_widget.height())
        # Не ловит клики — пропускает их к виджетам под собой
        snap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        snap.show()
        snap.raise_()

        # 2. Переключение (новый widget уже виден под snapshot)
        if switch_callback:
            try:
                switch_callback()
            except Exception:
                traceback.print_exc()

        # 3. Fade-out snapshot
        eff = QGraphicsOpacityEffect(snap)
        snap.setGraphicsEffect(eff)
        eff.setOpacity(1.0)
        anim = QPropertyAnimation(eff, b"opacity", snap)
        anim.setDuration(int(duration * _anim_speed_multiplier()))
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutQuint)
        anim.finished.connect(snap.deleteLater)
        snap._anim = anim  # удерживаем ссылку чтобы GC не убил
        anim.start()
        return snap
    except Exception:
        traceback.print_exc()
        if switch_callback:
            try:
                switch_callback()
            except Exception:
                pass
        return None


def _anim_speed_multiplier() -> float:
    """Глобальный множитель скорости анимаций. Управляется слайдером в
    Настройках (только для админа). Дефолт 1.0×, диапазон 0.5–2.5×.

    Применяется ко ВСЕМ fade-переходам:
    - `fade_in_widget` (табы / блоки / refs / chat / fullscreen)
    - `apply_modal_dim` / `remove_modal_dim` (затемнение фона у попапов)
    - `_fade_in_chat_page` (драматичный переход после плашки)
    - `setup_fade_overlay` (hover overlay карточек)

    Чтобы протестировать значение без пересборки .app — поменяй слайдер
    в Настройках → анимации применяются мгновенно к следующему переходу.
    """
    try:
        # Дефолт 1.5× — чуть плавнее «голых» базовых значений (юзер просил
        # «в два раза» — 1.5 ощущается мягко без затягивания)
        v = QSettings(APP_ORG, APP_NAME).value("anim_speed_multiplier", 1.5)
        m = float(v)
        # Кламп в безопасный диапазон чтобы случайно не сделать 0мс или часовую анимацию
        return max(0.3, min(10.0, m))
    except Exception:
        return 1.0


# Глобальный флаг готовности UI. Ставится в True через ~250мс после showEvent
# главного окна. Пока False — fade_in_widget пропускается, чтобы первичная
# инициализация (показ окна, _populate_episodes → _select_block → fade) не
# давала моргания «opacity=0 → 1» на свежезапущенном приложении.
_UI_READY = False


def _set_ui_ready():
    global _UI_READY
    _UI_READY = True


def fade_in_widget(widget, duration: int = 280):
    """Универсальный плавный fade-in для любого QWidget (таб, страница,
    панель). Применяет QGraphicsOpacityEffect 0→1 за `duration` мс с
    OutCubic easing. После окончания снимает эффект чтобы не тормозить
    последующие отрисовки/скролл (особенно важно для refs cards).

    Используется для переходов: между табами Editor/Новый эпизод/Настройки,
    между блоками/эпизодами в редакторе, между страницами content_stack
    (shots / refs / chat).
    """
    if widget is None:
        return None
    # Защита от моргания на старте: пока UI не полностью отрисовался после
    # запуска приложения, fade-in пропускаем (виджеты сразу видны в полном
    # виде). Флаг ставится в True через ~250мс после показа главного окна.
    if not _UI_READY:
        return None
    try:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(int(duration * _anim_speed_multiplier()))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        # OutQuint — более «вкусное» плавное замедление в конце (юзер просил
        # больше плавности; OutCubic ощущался резковато на длинных переходах)
        anim.setEasingCurve(QEasingCurve.Type.OutQuint)

        def _cleanup(w=widget):
            try:
                w.setGraphicsEffect(None)
            except Exception:
                pass
        anim.finished.connect(_cleanup)
        # Сохраняем ссылку на анимацию в атрибуте виджета — иначе GC её
        # подберёт до завершения. Перезаписывает прошлую если была.
        widget._fade_in_anim = anim
        anim.start()
        return anim
    except Exception:
        traceback.print_exc()
        return None


def remove_modal_dim(overlay):
    """Плавно прячет overlay (fade-out 180мс) и удаляет его.
    Безопасно если overlay=None."""
    if overlay is None:
        return
    try:
        effect = overlay.graphicsEffect()
        if effect is None:
            overlay.deleteLater()
            return
        anim = QPropertyAnimation(effect, b"opacity", overlay)
        anim.setDuration(int(180 * _anim_speed_multiplier()))
        anim.setStartValue(effect.opacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(overlay.deleteLater)
        anim.start()
        overlay._fade_anim = anim
    except Exception:
        traceback.print_exc()
        try:
            overlay.deleteLater()
        except Exception:
            pass


def get_block_meta(meta: Dict, ep: str, blk_n: str, lang: Optional[str] = None) -> Dict:
    """Унификация формата блока в episodes.json (поддержка обратной совместимости).

    Форматы блока:
      "1": "Встреча у стекла"  ← старый, только имя (одноязычный)
      "1": {"name": "...", "shots": {...}}  ← новый
      "1": {"name": {"ru": ..., "uk": ..., "en": ...}, "shots": {...}}  ← с переводами

    Форматы описания шота (внутри shots):
      "1": "Лора сидит..."  ← одноязычный
      "1": {"ru": "...", "uk": "...", "en": "..."}  ← с переводами

    Возвращает уже разрешённое для текущего языка: {"name": str, "shots": {str: str}}.
    """
    if lang is None:
        lang = get_lang()
    blocks = meta.get(ep, {}).get("blocks", {})
    raw = blocks.get(str(blk_n))
    if raw is None:
        montage_blocks = (meta.get(ep, {}).get("montage_card", {}) or {}).get("blocks", [])
        try:
            target_n = int(blk_n)
        except Exception:
            target_n = None
        if isinstance(montage_blocks, list) and target_n is not None:
            for item in montage_blocks:
                if not isinstance(item, dict):
                    continue
                try:
                    item_n = int(item.get("n", -1))
                except Exception:
                    item_n = -1
                if item_n != target_n:
                    continue
                shots = {}
                for shot in item.get("shots", []) or []:
                    if not isinstance(shot, dict):
                        continue
                    n = shot.get("n")
                    if n is None:
                        continue
                    desc = (
                        shot.get(f"description_{lang}")
                        or shot.get("description_ru")
                        or shot.get("description")
                        or shot.get("scene_action")
                        or ""
                    )
                    shots[str(n)] = _pick_lang(desc, lang)
                return {
                    "name": _pick_lang(item.get("name", ""), lang),
                    "shots": shots,
                }
    if raw is None:
        return {"name": "", "shots": {}}
    if isinstance(raw, str):
        return {"name": raw, "shots": {}}
    if isinstance(raw, dict):
        shots_raw = raw.get("shots", {}) or {}
        return {
            "name":  _pick_lang(raw.get("name", ""), lang),
            "shots": {str(k): _pick_lang(v, lang) for k, v in shots_raw.items()},
        }
    return {"name": "", "shots": {}}


def get_episode_title(meta: Dict, ep: str, lang: Optional[str] = None) -> str:
    """Название эпизода с поддержкой переводов."""
    if lang is None:
        lang = get_lang()
    title_raw = meta.get(ep, {}).get("title", "")
    return _pick_lang(title_raw, lang)


def setup_paths_for_show(project_root: Path, show_name: Optional[str]) -> None:
    """Устанавливает глобальные пути в папку активного сериала.
    Если show_name=None — пути «зануляются» (в проекте нет сериалов)."""
    global SHOW_ROOT, ENV_FILE, PROMPTS_DIR, STORYBOARDS_DIR, SEEDANCE_DIR
    global LOCATIONS_DIR, CHARACTERS_DIR, OBJECTS_DIR, CHARACTERS_TEXTURE_DIR
    ENV_FILE = project_root / ".env"
    if not show_name:
        SHOW_ROOT       = project_root / "shows" / "_none_"
        PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
        STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
        SEEDANCE_DIR    = SHOW_ROOT / "output" / "seedance"
        LOCATIONS_DIR   = SHOW_ROOT / "refs" / "locations"
        CHARACTERS_DIR  = SHOW_ROOT / "refs" / "characters"
        OBJECTS_DIR     = SHOW_ROOT / "refs" / "objects"
        CHARACTERS_TEXTURE_DIR = SHOW_ROOT / "refs" / "characters_texture"
        return
    SHOW_ROOT       = project_root / "shows" / show_name
    PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
    STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
    SEEDANCE_DIR    = SHOW_ROOT / "output" / "seedance"
    refs            = SHOW_ROOT / "refs"
    LOCATIONS_DIR   = refs / "locations"
    CHARACTERS_DIR  = refs / "characters"
    OBJECTS_DIR     = refs / "objects"
    CHARACTERS_TEXTURE_DIR = refs / "characters_texture"
    STORYBOARDS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    SEEDANCE_DIR.mkdir(parents=True, exist_ok=True)


# ─── Подсветка строк чата (эвристика по маркерам) ─────────────────────────────

CHAT_LINE_COLORS = {
    None:     "#cfcfcf",   # обычный текст Claude
    'system': "#b08af7",   # ▶ системные строки Studio
    'progress':"#d8a8ff",  # 🌐 ✏ 🎨 📝 — прогресс Claude
    'user':   "#6fb6ff",   # 💬 Ты:
    'ok':     "#6db86d",   # ✓ Готово / реф есть
    'warn':   "#ffd24d",   # ✗ нет рефа / Жду команды
    'error':  "#ff7a7a",   # ⚠ Ошибка
}


def detect_line_kind(line: str) -> Optional[str]:
    """Определяет тип строки по первым непробельным символам и ключевым фразам.
    Возвращает None если строка обычная (нейтральный серый).

    Маркеры для покраски:
      ▶              — system (фиолетовый)
      🌐 ✏ 🎨 📝 🔍 🚀  — progress (светло-фиолетовый)
      ✓ / ✅          — ok (зелёный)
      ✗ / ❌          — warn (жёлтый — «нет, нужно сделать»)
      ⚠              — error (красный)
      💬             — user (голубой)
      Жду команды.   — warn (Claude ждёт ответа, важно заметить)
    """
    if not line or not line.strip():
        return None
    ls = line.lstrip()
    # Системные маркеры Studio
    if ls.startswith('▶'):
        return 'system'
    # Прогресс Claude
    for prefix in ('🌐', '✏', '🎨', '📝', '🔍', '🚀', '🛠', '⚙'):
        if ls.startswith(prefix):
            return 'progress'
    # Успех / ошибки / отсутствие рефа
    if ls.startswith('✓') or ls.startswith('✅'):
        return 'ok'
    if ls.startswith('✗') or ls.startswith('❌'):
        return 'warn'
    if ls.startswith('⚠'):
        return 'error'
    if ls.startswith('💬'):
        return 'user'
    if ls.startswith('⏹'):
        return 'warn'
    # Ключевые фразы (любая позиция в строке)
    if 'Жду команды' in ls or 'жду команды' in ls:
        return 'warn'
    return None


def format_chat_inline(line: str) -> str:
    """Экранирует HTML, потом восстанавливает markdown-подобные элементы:
      **bold**      → <b>bold</b>          (имена рефов, секций)
      `code`        → голубой моно-span    (пути файлов)
      \\n           → <br>
      двойные пробелы → &nbsp;&nbsp; (для отступов)
    """
    from html import escape as _esc
    out = _esc(line)
    out = re.sub(r'\*\*([^*\n]+?)\*\*',
                 r'<b style="color:#fff;">\1</b>', out)
    out = re.sub(r'`([^`\n]+?)`',
                 r'<span style="color:#7fc8ff; background:#1a1424; '
                 r'padding:0 4px; border-radius:3px;">\1</span>', out)
    out = out.replace('\n', '<br>').replace('  ', '&nbsp;&nbsp;')
    return out


def chat_log_path(ep_id: str) -> Path:
    """Путь к файлу истории чата для эпизода: `shows/<slug>/chats/<ep_id>.jsonl`.
    Один файл на эпизод. Каждая строка — отдельная реплика в JSON:
      {"ts": ISO8601, "role": "user"|"assistant"|"system", "kind": str|None, "text": str}
    `kind` нужен только для UI-цвета (см. NewEpisodeView._LOG_COLORS). Сообщения
    модели → role="assistant", kind=None (нейтральный серый). Юзера → role="user".
    Системные строки Studio (▶, ✓, ⚠) → role="system" + соответствующий kind.
    """
    return SHOW_ROOT / "chats" / f"{ep_id}.jsonl"


def append_chat_message(ep_id: str, role: str, text: str,
                         kind: Optional[str] = None) -> None:
    """Append-only запись одной реплики в `chats/<ep_id>.jsonl`. Создаёт папку
    при необходимости. Игнорирует ошибки (запись не должна валить UI)."""
    if not ep_id or not text:
        return
    try:
        from datetime import datetime
        path = chat_log_path(ep_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec='seconds'),
            "role": role,
            "kind": kind,
            "text": text,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_chat_messages(ep_id: str) -> List[Dict]:
    """Читает chat.jsonl для эпизода. Возвращает [] если нет файла или ошибка."""
    path = chat_log_path(ep_id)
    if not path.exists():
        return []
    out: List[Dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and "text" in rec:
                    out.append(rec)
            except Exception:
                continue
    except Exception:
        pass
    return out


def list_episodes() -> List[str]:
    """Список ID эпизодов в активном сериале (`ep20`, `ep21`...). Сортировка по номеру.
    Источники: storyboard-файлы, prompt-файлы блоков, episodes.json (для пустых
    эпизодов которые юзер только что запустил через «Новый эпизод» — у них
    ещё нет блоков, но они уже должны быть видны в UI)."""
    seen: set = set()
    for f in STORYBOARDS_DIR.glob("*_block_*_shot*.jpg"):
        m = re.match(r'(ep\d+)_block_', f.name)
        if m:
            seen.add(m.group(1))
    for p in PROMPTS_DIR.glob("*_block_*.txt"):
        m = re.match(r'(ep\d+)_block_', p.name)
        if m:
            seen.add(m.group(1))
    # episodes.json может содержать эпизоды без блоков (только что запущенный
    # эпизод — рефы есть, блоков ещё нет)
    if SHOW_ROOT and SHOW_ROOT.exists():
        try:
            meta = read_episodes_meta(SHOW_ROOT)
            for k in meta.keys():
                if re.match(r'^ep\d+$', k):
                    seen.add(k)
        except Exception:
            pass
    def num(ep): m = re.match(r'ep(\d+)', ep); return int(m.group(1)) if m else 0
    return sorted(seen, key=num)


def list_blocks_for_episode(ep: str) -> List[str]:
    """Имена блоков эпизода (`ep20_block_1`, `ep20_block_2`...) — отсортированы по номеру."""
    seen: set = set()
    for f in STORYBOARDS_DIR.glob(f"{ep}_block_*_shot*.jpg"):
        m = re.match(rf'({re.escape(ep)}_block_\d+)_shot\d+', f.stem)
        if m:
            seen.add(m.group(1))
    for p in PROMPTS_DIR.glob(f"{ep}_block_*.txt"):
        seen.add(p.stem)
    def num(b): m = re.match(r'.*_block_(\d+)', b); return int(m.group(1)) if m else 0
    return sorted(seen, key=num)


def episode_total_duration(ep: str) -> int:
    """Сумма длительностей всех шотов эпизода (в секундах)."""
    total = 0
    for blk in list_blocks_for_episode(ep):
        pf = PROMPTS_DIR / f"{blk}.txt"
        if pf.exists():
            try:
                for shot in parse_shots(pf.read_text(encoding="utf-8")):
                    if not shot.get("is_blank"):
                        m = re.search(r'(\d+)', shot.get("duration", ""))
                        if m:
                            total += int(m.group(1))
            except Exception:
                pass
    return total


def ref_prompt_path(image_path: Path) -> Path:
    """Путь к `<name>_prompt.txt` рядом с картинкой рефа.
    Сохраняется при первой генерации (pipeline.py) — нужен для кнопки «Перегенерировать»."""
    return image_path.parent / f"{image_path.stem}_prompt.txt"


def ref_geometry_path(image_path: Path) -> Path:
    """Путь к `<name>_geometry.txt` — описание геометрии локации (только для locations)."""
    return image_path.parent / f"{image_path.stem}_geometry.txt"


def list_episode_refs(ep: str) -> Dict[str, List[Dict]]:
    """Собирает рефы, использованные в эпизоде. Источники в порядке приоритета:

    1) `episodes.json[ep].refs` — manifest, который Claude пишет при работе
       над эпизодом. Формат:
         "refs": {
           "locations":  ["prison_phone_hallway.jpg", "..."],   # filename'ы
           "objects":    ["briefcase.jpg"],                      # filename'ы
           "characters": ["mark", "victor"]                     # имена ПАПОК
         }
       Это самый точный источник — именно эти рефы нужны эпизоду.

    2) Шапки промптов блоков `# [@]imgN = filename.jpg` — для эпизодов
       которые УЖЕ имеют монтажную карту, но manifest почему-то не сохранён.

    3) Если ни manifest ни блоков — возвращаем ПУСТОЙ список. Не показываем
       весь refs/ кучу — это вводит юзера в заблуждение (рефы из ep20
       подмешиваются в ep21 если общая папка). Юзер увидит «РЕФЕРЕНСЫ пусты»
       и поймёт что Claude ещё не сделал ШАГ 1.

    Каждый ref: {'name': str, 'filename': str, 'path': Path, 'tag': 'imgN'}
    Тег `imgN` — глобальный по эпизоду (locations → objects → characters).
    """
    locs:  List[Dict] = []
    objs:  List[Dict] = []
    chars: List[Dict] = []

    def _pretty_stem(stem: str) -> str:
        return stem.replace('_', ' ').strip().capitalize()

    IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}

    def _find_first_char_image(sub_dir: Path) -> Optional[Path]:
        """Берём первый файл-картинку в папке персонажа (репрезентативный реф)."""
        for p in sorted(sub_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMG_EXT:
                return p
        return None

    # ── Источник 1: manifest в episodes.json[ep].refs ────────────────────
    try:
        meta_all = read_episodes_meta(SHOW_ROOT)
        ep_meta = meta_all.get(ep, {}) if isinstance(meta_all, dict) else {}
        manifest = ep_meta.get('refs') if isinstance(ep_meta, dict) else None
        # Phase 2 hotfix #11 (Долг 12+13): юзер мог через UI пометить
        # часть рефов как «не нужны» (skipped) или выбрать конкретный
        # файл (linked). Эти решения хранятся в `refs_decisions` и
        # перекрывают manifest от AI.
        decisions = ep_meta.get('refs_decisions') if isinstance(ep_meta, dict) else None
    except Exception:
        manifest = None
        decisions = None

    def _decision_for(gen_type: str, name: str) -> Optional[Dict]:
        """Вернёт {'decision':'skipped'} / {'decision':'linked','filename':'X'} или None."""
        if not isinstance(decisions, dict):
            return None
        bucket = decisions.get(gen_type)
        if not isinstance(bucket, dict):
            return None
        entry = bucket.get(name)
        return entry if isinstance(entry, dict) else None

    if isinstance(manifest, dict) or isinstance(decisions, dict):
        # 2026-05-05 (Жёсткий контроль ВСЕХ типов рефов): manifest от AI
        # ПОЛНОСТЬЮ ИГНОРИРУЕТСЯ. Рефы появляются в РЕФЕРЕНСАХ ТОЛЬКО
        # если юзер явно нажал «📁 Выбрать существующий» в чате
        # (decision == 'linked'), либо «🎨 Сгенерировать → done» (тогда
        # episode_chat.py:_on_gen_finished пишет linked с именем файла).
        # Раньше manifest-driven логика подгружала рефы автоматически —
        # юзер видел «магически» появившиеся локации/объекты в
        # РЕФЕРЕНСАХ ещё до своего первого клика.
        # 2026-05-10 (БАГ 10 fix — read-side self-healing): если filename
        # в decisions устарел (например `.png` от агентского overwrite, а
        # на диске реально лежит `.jpg`), пытаемся найти actual файл
        # через glob `{name}.*`. Защита независимо от write-side фиксов
        # — даже если кто-то снаружи (agent через bash) затрёт decisions
        # с неправильным расширением, render всё равно покажет файл.
        def _find_on_disk(base_dir: Path, slug: str,
                          hint_filename: str) -> Optional[Path]:
            # 1. Hint exists — return as is.
            if hint_filename:
                p = base_dir / hint_filename
                if p.exists() and p.is_file():
                    return p
            # 2. Glob `{slug}.*` — disk truth fallback.
            for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                p = base_dir / f"{slug}{ext}"
                if p.exists() and p.is_file():
                    return p
            return None

        loc_decisions = decisions.get('location') if isinstance(decisions, dict) else None
        if isinstance(loc_decisions, dict):
            for loc_name, entry in loc_decisions.items():
                if not isinstance(entry, dict) or entry.get('decision') != 'linked':
                    continue
                fn = entry.get('filename') or f"{loc_name}.jpg"
                cand = _find_on_disk(LOCATIONS_DIR, loc_name, fn)
                if cand is not None:
                    locs.append({'name': _pretty_stem(cand.stem),
                                 'filename': cand.name, 'path': cand,
                                 'slug': loc_name})
        obj_decisions = decisions.get('object') if isinstance(decisions, dict) else None
        if isinstance(obj_decisions, dict):
            for obj_name, entry in obj_decisions.items():
                if not isinstance(entry, dict) or entry.get('decision') != 'linked':
                    continue
                fn = entry.get('filename') or f"{obj_name}.jpg"
                cand = _find_on_disk(OBJECTS_DIR, obj_name, fn)
                if cand is not None:
                    objs.append({'name': _pretty_stem(cand.stem),
                                 'filename': cand.name, 'path': cand,
                                 'slug': obj_name})
        # Phase 2 hotfix #14 (характеры — было сделано раньше, теперь
        # тот же подход применён ко ВСЕМ типам рефов):
        char_decisions = decisions.get('character') if isinstance(decisions, dict) else None
        if isinstance(char_decisions, dict):
            for char_name, entry in char_decisions.items():
                if not isinstance(entry, dict) or entry.get('decision') != 'linked':
                    continue
                picked = entry.get('filename') or ''
                if not picked:
                    continue
                if '/' in picked:
                    char_entry = picked
                else:
                    char_entry = f"{char_name}/{picked}"
                folder_name, _, file_name = char_entry.partition('/')
                sub = CHARACTERS_DIR / folder_name
                p = sub / file_name if sub.is_dir() else None
                if p is None or not p.exists():
                    p = _find_first_char_image(sub) if sub.is_dir() else None
                if p is not None:
                    chars.append({'name': folder_name.replace('_', ' ').title(),
                                  'filename': p.name, 'path': p})
        n = 1
        for r in locs + objs + chars:
            r['tag'] = f'img{n}'
            n += 1
        return {'locations': locs, 'objects': objs, 'characters': chars}

    # ── Источник 2: шапки промптов блоков ────────────────────────────────
    filenames: set = set()
    for blk in list_blocks_for_episode(ep):
        pf = PROMPTS_DIR / f"{blk}.txt"
        if not pf.exists():
            continue
        try:
            for line in pf.read_text(encoding="utf-8").splitlines():
                m = re.match(r'#\s*\[@\]img\d+\s*=\s*(.+?)\s*$', line)
                if m:
                    filenames.add(m.group(1).strip())
        except Exception:
            continue

    if not filenames:
        # Источник 3: пусто. НЕ показываем все refs/ — это путает юзера.
        return {'locations': [], 'objects': [], 'characters': []}

    for fn in sorted(filenames):
        # 1) locations
        cand = LOCATIONS_DIR / fn
        if cand.exists():
            locs.append({'name': _pretty_stem(cand.stem), 'filename': fn,
                         'path': cand, 'slug': cand.stem})
            continue
        # 2) objects
        cand = OBJECTS_DIR / fn
        if cand.exists():
            objs.append({'name': _pretty_stem(cand.stem), 'filename': fn,
                         'path': cand, 'slug': cand.stem})
            continue
        # 3) characters — ищем во всех подпапках
        if CHARACTERS_DIR.exists():
            found = None
            for sub in CHARACTERS_DIR.iterdir():
                if sub.is_dir():
                    p = sub / fn
                    if p.exists():
                        found = (sub, p)
                        break
            if found:
                sub, p = found
                # Имя персонажа = имя подпапки (Title Case)
                chars.append({'name': sub.name.replace('_', ' ').title(),
                              'filename': fn, 'path': p})
                continue

    # Глобальная нумерация imgN
    n = 1
    for r in locs + objs + chars:
        r['tag'] = f'img{n}'
        n += 1
    return {'locations': locs, 'objects': objs, 'characters': chars}


def _load_montage_card(ep_id: str) -> Optional[dict]:
    """Читает `output/_agent_log_<ep>.json` и возвращает последний
    montage_card (dict с blocks → shots → duration_sec).

    2026-05-08: это надёжный источник длительностей шотов и блоков —
    AI scriptwriter всегда выдаёт `duration_sec` в JSON-структуре,
    которая сохраняется в _agent_log. Парсинг annotation в промптах
    (`Text annotation below Panel N: "SHOT N / Xс / ..."`) AI часто
    забывает — поэтому полагаться нельзя.

    Возвращает None если файла нет или нет stages с blocks.
    """
    try:
        path = SHOW_ROOT / "output" / f"_agent_log_{ep_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        stages = data.get("stages") or []
        # Последний stage с blocks — самая свежая утверждённая карта
        # (либо после scriptwriter если validator с первого раза, либо
        # после editor если были фиксы).
        for stage in reversed(stages):
            res = stage.get("result") or {}
            if isinstance(res, dict) and res.get("blocks"):
                return res
    except Exception:
        pass
    return None


def get_block_shot_durations(ep_id: str, block_n: int) -> Dict[int, int]:
    """Возвращает словарь `{shot_n: duration_sec}` для конкретного
    блока эпизода. Источник — `_agent_log_<ep>.json`. Пусто если
    данных нет."""
    out: Dict[int, int] = {}
    card = _load_montage_card(ep_id)
    if not card:
        return out
    try:
        blocks = card.get("blocks") or []
        for b in blocks:
            if int(b.get("n", 0)) != int(block_n):
                continue
            for s in (b.get("shots") or []):
                try:
                    out[int(s.get("n", 0))] = int(s.get("duration_sec", 0))
                except Exception:
                    pass
            break
    except Exception:
        pass
    return out


def get_block_shot_dialogs(ep_id: str, block_n: int) -> Dict[int, Dict[str, str]]:
    """Возвращает `{shot_n: {"en": ..., "ru": ...}}` для блока эпизода —
    только шоты с НЕПУСТОЙ репликой (дискриминатор — en). Источник —
    `_agent_log_<ep>.json` (тот же первоисточник, что у Seedance-промпта).
    Пусто если данных/реплик нет. Зеркало get_block_shot_durations.
    Этап 1 — реплика (en) под карточкой; Этап 2 — ru для перевода."""
    out: Dict[int, Dict[str, str]] = {}
    card = _load_montage_card(ep_id)
    if not card:
        return out
    try:
        for b in (card.get("blocks") or []):
            if int(b.get("n", 0)) != int(block_n):
                continue
            for s in (b.get("shots") or []):
                d = s.get("dialog")
                if isinstance(d, dict):
                    en = (d.get("en") or "").strip()
                    if en:
                        try:
                            out[int(s.get("n", 0))] = {
                                "en": en,
                                "ru": (d.get("ru") or "").strip(),
                            }
                        except Exception:
                            pass
            break
    except Exception:
        pass
    return out


def _uk_tr_path(ep_id: str) -> Path:
    """Путь файла украинских переводов реплик эпизода (per-episode). Лежит в
    output/ рядом с _agent_log_<ep>.json — НЕ в .cache/ (не под выборочной
    чисткой _on_block_refs_btn); удаляется явно при «Удалить эпизод»."""
    return SHOW_ROOT / "output" / f"_translations_uk_{ep_id}.json"


def load_uk_translations(ep_id: str) -> Dict[str, str]:
    """Читает {en: uk} из файла эпизода. Нет файла / битый / не dict → {}.
    Только валидные непустые строковые пары."""
    out: Dict[str, str] = {}
    try:
        p = _uk_tr_path(ep_id)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and k and v:
                        out[k] = v
    except Exception:
        pass
    return out


def save_uk_translations(ep_id: str, mapping: Dict[str, str]) -> None:
    """Атомарно (temp + os.replace) пишет {en: uk} в файл эпизода. mapping —
    полный словарь для записи (caller формирует подмножество реплик этого
    эпизода). Ошибки молча игнорим — перевод не критичен для работы."""
    try:
        p = _uk_tr_path(ep_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception:
        traceback.print_exc()


def collect_episode_dialogs_en(ep_id: str) -> List[str]:
    """Уникальные непустые dialog.en по ВСЕМ блокам монтажки эпизода (тот же
    источник, что реплики под карточками). Пусто если карты нет."""
    out: List[str] = []
    seen = set()
    card = _load_montage_card(ep_id)
    if not card:
        return out
    try:
        for b in (card.get("blocks") or []):
            for s in (b.get("shots") or []):
                d = s.get("dialog")
                if isinstance(d, dict):
                    en = (d.get("en") or "").strip()
                    if en and en not in seen:
                        seen.add(en)
                        out.append(en)
    except Exception:
        pass
    return out


def block_total_duration(block_name: str) -> int:
    """Сумма длительностей шотов блока. Сначала пробуем `_agent_log_<ep>.json`
    (надёжный JSON источник), потом fallback на парсинг annotation
    в промпт-файле (`SHOT N / Xс / ...`)."""
    # 2026-05-08: парсим имя `epXX_block_M`
    m = re.match(r'(ep\d+)_block_(\d+)$', block_name)
    if m:
        ep_id, blk_n = m.group(1), int(m.group(2))
        durs = get_block_shot_durations(ep_id, blk_n)
        if durs:
            return sum(durs.values())
    # Fallback на старый парсер промпта
    pf = PROMPTS_DIR / f"{block_name}.txt"
    if not pf.exists():
        return 0
    total = 0
    try:
        for shot in parse_shots(pf.read_text(encoding="utf-8")):
            if not shot.get("is_blank"):
                ms = re.search(r'(\d+)', shot.get("duration", ""))
                if ms:
                    total += int(ms.group(1))
    except Exception:
        pass
    return total


def get_stored_root() -> Optional[Path]:
    s = QSettings(APP_ORG, APP_NAME)
    p = s.value("project_root", "")
    return Path(p) if p and Path(p).is_dir() else None


def store_root(root: Path) -> None:
    QSettings(APP_ORG, APP_NAME).setValue("project_root", str(root))


def is_valid_project(path: Path) -> bool:
    return (path / "pipeline.py").exists() or (path / "shows").is_dir() or (path / "output").is_dir()


# ─── i18n ────────────────────────────────────────────────────────────────────
# TRANSLATIONS / SUPPORTED_LANGUAGES / get_lang / set_lang / tr / _pick_lang
# вытащены в i18n.py 2026-05-04. Импортированы в начале файла.



# ─── Обновления ──────────────────────────────────────────────────────────────

def github_raw_url(filename: str) -> str:
    return f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{filename}"


def github_zip_url() -> str:
    return f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"


def read_local_version(root: Path) -> str:
    f = root / "version.json"
    if not f.exists():
        return "0.0.0"
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def read_local_app_version(root: Path) -> str:
    """Версия самого приложения (Storyboard Studio.app), отдельно от версии проекта."""
    f = root / "version.json"
    if not f.exists():
        return "0.0.0"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("app_version") or data.get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def version_gt(a: str, b: str) -> bool:
    """True если версия a строго больше версии b (семантическое сравнение)."""
    try:
        return tuple(int(x) for x in a.split('.')) > tuple(int(x) for x in b.split('.'))
    except (ValueError, AttributeError):
        return False


# ─── GitHub API: токен, Releases, статистика ─────────────────────────────────

def get_github_token_from_remote(root: Path) -> Optional[str]:
    """Извлекает GitHub PAT из URL origin.

    Поддерживаемые форматы:
      https://TOKEN@github.com/...
      https://USER:TOKEN@github.com/...
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
            **no_console_kwargs(),
        )
        if r.returncode != 0:
            return None
        url = r.stdout.strip()
        m = re.match(r'https://(?:[^@:/]+:)?([^@:/]+)@github\.com/', url)
        if m:
            return m.group(1)
        return None
    except Exception:
        return None


def fetch_latest_app_release_version() -> Optional[str]:
    """Возвращает версию последнего опубликованного релиза приложения (тег app-vX.Y.Z)."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        for rel in r.json():
            tag = rel.get("tag_name", "")
            if tag.startswith("app-v"):
                return tag[len("app-v"):]
        return None
    except Exception:
        return None


def fetch_release_asset_info(version: str) -> Optional[Dict]:
    """Возвращает info об asset (zip) для релиза app-vX.Y.Z под текущую
    платформу: на Mac ищет `*-mac.zip`, на Windows — `*-win.zip`.
    На Linux/прочих — fallback на mac (dev-режим)."""
    # 2026-05-08: платформо-зависимый фильтр. Mac-zip заливается локально
    # из SendUpdateThread, Win-zip — GitHub Actions workflow `build-windows`
    # (см. .github/workflows/build-windows.yml). Имена файлов совпадают
    # по паттерну `Storyboard Studio vX.Y.Z-{mac,win}.zip`.
    if sys.platform == 'win32':
        platform_marker = 'win'
    else:
        platform_marker = 'mac'  # darwin + dev/linux fallback
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        for rel in r.json():
            if rel.get("tag_name") == f"app-v{version}":
                for asset in rel.get("assets", []):
                    name = asset.get("name", "").lower()
                    if name.endswith(".zip") and platform_marker in name:
                        return asset
        return None
    except Exception:
        return None


def fetch_release_asset_by_name(version: str, name_pattern: str) -> Optional[Dict]:
    """Возвращает info об asset релиза `app-vX.Y.Z` по подстроке в имени.

    В отличие от `fetch_release_asset_info` (платформо-зависимый поиск
    `*-mac.zip` / `*-win.zip`) ищет ЛЮБОЙ asset по substring (case-insensitive).
    Используется для дополнительных release-assets: `actors-snapshot-vX.Y.Z.zip`
    (платформо-независимый снапшот папки actors/ для тихой синхронизации
    на стороне коллеги — см. `DownloadAppUpdateThread._sync_actors_snapshot`).

    Args:
        version: версия без префикса `app-v` (например "1.0.69").
        name_pattern: подстрока для поиска в `asset.name` (например
                      "actors-snapshot" или "actors").
    Returns:
        Полный asset dict от GitHub API (содержит `browser_download_url`,
        `size`, `name`) или None если не найден / сеть упала.
    """
    needle = (name_pattern or "").lower()
    if not needle:
        return None
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        r = requests.get(url, timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        for rel in r.json():
            if rel.get("tag_name") == f"app-v{version}":
                for asset in rel.get("assets", []):
                    name = asset.get("name", "").lower()
                    if needle in name:
                        return asset
        return None
    except Exception:
        return None


def fetch_all_release_stats() -> List[Dict]:
    """Возвращает список {tag, version, downloads} для последних релизов приложения."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return []
        result = []
        for rel in r.json()[:5]:
            tag = rel.get("tag_name", "")
            if tag.startswith("app-v"):
                total = sum(a.get("download_count", 0) for a in rel.get("assets", []))
                result.append({
                    "tag":       tag,
                    "version":   tag[len("app-v"):],
                    "downloads": total,
                })
        return result
    except Exception:
        return []


def find_current_app_bundle() -> Optional[Path]:
    """Возвращает путь к .app-бандлу в котором работает этот exe, или None если не frozen."""
    if not getattr(sys, 'frozen', False):
        return None
    exe = Path(sys.executable)
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def create_github_release(token: str, tag: str, name: str, body: str = "") -> Optional[Dict]:
    """Создаёт GitHub Release. Возвращает данные релиза или None."""
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "tag_name":         tag,
        "target_commitish": GITHUB_BRANCH,
        "name":             name,
        "body":             body,
        "draft":            False,
        "prerelease":       False,
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            return r.json()
        return None
    except Exception:
        return None


def upload_release_asset(token: str, upload_url_template: str, file_path: Path) -> bool:
    """Загружает файл как asset в существующий GitHub Release."""
    upload_url = upload_url_template.split("{")[0]
    headers = {
        "Authorization": f"token {token}",
        "Content-Type":  "application/octet-stream",
        "Accept":        "application/vnd.github+json",
    }
    params = {"name": file_path.name}
    try:
        with open(file_path, "rb") as f:
            r = requests.post(upload_url, headers=headers, params=params,
                              data=f.read(), timeout=600)
        return r.status_code in (200, 201)
    except Exception:
        return False


def is_admin_mode(root: Path) -> bool:
    """Админ — это владелец репозитория с GitHub PAT-токеном в origin URL.
    Сотрудники получают проект через установщик (без .git или без токена),
    поэтому им is_admin = False и кнопка «Отправить обновление» не показывается."""
    if not (root / ".git").is_dir():
        return False
    return get_github_token_from_remote(root) is not None


def github_configured() -> bool:
    return GITHUB_USER and GITHUB_USER != "PLACEHOLDER_USER"


# ─── Тема ─────────────────────────────────────────────────────────────────────
# Акцентный красный (с макета LUMZ): #E63946
DARK = """
QMainWindow                 { background: #121313; }
QWidget                     { color: #e0e0e0; font-family: __FONT_FAMILY__; }
/* 2026-05-08: фон главного окна теперь рисуется paintEvent'ом класса
   `views.theme.LumzBackground` (радиальный градиент сверху по центру).
   Старый QSS-блок `QWidget#main-bg { qlineargradient(...) }` удалён —
   он перебивал paintEvent. Объект-имя `main-bg` оставлено на bg-виджете
   для обратной совместимости (на случай если где-то ещё селектор по нему).
   Прозрачные дочерние виджеты автоматически просвечивают радиал через
   правила QScrollArea ниже. */
QScrollArea                 { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QDialog                     { background: #121313; }

QPushButton {
    background: #221a30; border: 1px solid #2e2440; border-radius: 6px;
    padding: 7px 14px; color: #e0e0e0; font-size: 13px;
}
QPushButton:hover           { background: #2c2240; border-color: #3d2f55; }
QPushButton:pressed         { background: #1c1626; }
QPushButton:disabled        { background: #181222; color: #444; border-color: #221a30; }

QPushButton#nav-select {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 8px;
    color: #ffffff;
    padding: 0 10px;
    font-size: 12px;
    font-weight: 600;
    text-align: left;
}
QPushButton#nav-select:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.18);
}
QPushButton#nav-select[empty="true"] {
    color: rgba(255, 255, 255, 0.48);
}
QPushButton#nav-select[active="true"] {
    background: rgba(228, 52, 74, 0.15);
    border-color: rgba(228, 52, 74, 0.40);
}
QPushButton#nav-arrow {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    color: rgba(255, 255, 255, 0.62);
    font-size: 19px;
    font-weight: 900;
    min-width: 24px;
    max-width: 24px;
    min-height: 26px;
    max-height: 26px;
    padding: 0;
}
QPushButton#nav-arrow:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.16);
    color: rgba(255, 255, 255, 0.92);
}
QPushButton#nav-arrow:disabled {
    background: rgba(255, 255, 255, 0.02);
    border-color: rgba(255, 255, 255, 0.04);
    color: rgba(255, 255, 255, 0.26);
}
QLabel#nav-label {
    color: rgba(255, 255, 255, 0.55);
    font-size: 13px;
    font-weight: 500;
}
QFrame#nav-popup {
    background: #151519;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 10px;
}
QWidget#nav-popup-item-wrap {
    background: transparent;
}
QLabel#nav-popup-title {
    color: rgba(255, 255, 255, 0.55);
    font-size: 11px;
    font-weight: 700;
}
QPushButton#nav-popup-item {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    color: rgba(255, 255, 255, 0.72);
    padding: 7px 10px;
    font-size: 12px;
    font-weight: 600;
    min-height: 26px;
}
QPushButton#nav-popup-item:hover {
    background: #05070c;
    color: #ffffff;
}
QPushButton#nav-popup-del {
    background: transparent;
    border: none;
    color: rgba(255, 255, 255, 0.28);
    font-size: 14px;
    font-weight: 700;
    padding: 0;
    border-radius: 7px;
}
QPushButton#nav-popup-del:hover {
    color: #e4344a;
    background: rgba(228, 52, 74, 0.12);
}
QPushButton#nav-popup-item[active="true"] {
    background: rgba(228, 52, 74, 0.18);
    border-color: rgba(228, 52, 74, 0.45);
    color: #ffffff;
}
QPushButton#nav-popup-add {
    background: rgba(228, 52, 74, 0.10);
    border: 1px solid rgba(228, 52, 74, 0.28);
    border-radius: 7px;
    color: #e4344a;
    padding: 8px 10px;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#nav-popup-add:hover {
    background: rgba(228, 52, 74, 0.18);
    border-color: rgba(228, 52, 74, 0.42);
}

/* QPushButton#save — нейтральная action-кнопка («Отправить» в чате,
   «Сохранить сториборд» внизу редактора). 2026-05-08 редизайн Этап 6:
   LUMZ-стиль — приглушённый фон, тонкая граница, белый текст. */
QPushButton#save {
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #ffffff;
    font-size: 13px; padding: 11px; font-weight: 500;
    border-radius: 8px;
}
QPushButton#save:hover {
    background: rgba(255, 255, 255, 0.10);
    border-color: rgba(255, 255, 255, 0.20);
}
QPushButton#save:disabled {
    background: rgba(255, 255, 255, 0.03);
    color: rgba(255, 255, 255, 0.30);
    border-color: rgba(255, 255, 255, 0.06);
}
QPushButton#storyboard-save {
    background: #131516;
    border: 1px solid #1d1e20;
    color: #f2f3f0;
    font-size: 13px; padding: 11px; font-weight: 600;
    border-radius: 8px;
}
QPushButton#storyboard-save:hover {
    background: #131516;
    border-color: #1d1e20;
    color: #ffffff;
}
QPushButton#storyboard-save:disabled {
    background: #131516;
    border-color: #1d1e20;
    color: rgba(255, 255, 255, 0.30);
}
QPushButton#secondary {
    background: transparent;
    color: rgba(255, 255, 255, 0.55);
    font-size: 12px;
}
QPushButton#secondary:hover { color: #ffffff; }

/* Кнопки эпизодов (#pill) и «+ Новый эпизод» (#pill-new).
   2026-05-08 редизайн Этап 3: LUMZ-стиль — активный эпизод с заливкой
   accent_red, неактивные с приглушённым фоном bg_subtle и серым текстом.
   Кнопка «+» — красная subtle (как в макете LUMZ).
   Padding/font-size/radius взяты из брифа: padding 6×14, radius 6px,
   font-size 11px. */
QPushButton#pill {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px; padding: 4px 8px;
    color: rgba(255, 255, 255, 0.55);
    font-size: 11px; font-weight: 500;
    min-width: 0;
}
QPushButton#pill:hover {
    background: rgba(255, 255, 255, 0.06);
    color: rgba(255, 255, 255, 0.85);
}
QPushButton#pill[active="true"] {
    background: #303438; border: 1px solid rgba(255,255,255,0.14); color: #f5f7f1;
    font-weight: 500;
}
QPushButton#pill[active="true"]:hover { background: #383d41; }

/* Pill-кнопка «+ Новый эпизод» — красная subtle (LUMZ accent_red_subtle).
   Активна когда юзер на странице NewEpisodeView. */
QPushButton#pill-new {
    background: rgba(199, 240, 74, 0.10);
    border: 1px solid rgba(199, 240, 74, 0.24);
    border-radius: 6px; padding: 4px 8px;
    color: #d9f68a;
    font-size: 11px; font-weight: 500;
    min-width: 0;
}
QPushButton#pill-new:hover {
    background: rgba(199, 240, 74, 0.14);
    border-color: rgba(199, 240, 74, 0.36);
}
QPushButton#pill-new[active="true"] {
    background: #303438;
    border: 1px solid rgba(255,255,255,0.14);
    color: #f5f7f1;
}
QPushButton#pill-new[active="true"]:hover { background: #383d41; }

/* 2026-05-08 редизайн Этап 4: полоса блоков (Блок 1/2/3/4 + Референсы +
   Чат) обёрнута в один контейнер #blocks-bar (см. _build_editor_tab),
   внутри которого 6 элементов прижаты друг к другу. Активная пилюля —
   с лёгкой красной подсветкой (accent_red_bg), неактивные — прозрачные. */
QWidget#blocks-bar {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
}
QScrollArea#block-carousel-scroll {
    background: transparent;
    border: none;
}
QPushButton#block-carousel-arrow {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    color: rgba(255, 255, 255, 0.62);
    font-size: 19px;
    font-weight: 900;
    min-width: 24px;
    max-width: 24px;
    min-height: 26px;
    max-height: 26px;
    padding: 0;
}
QPushButton#block-carousel-arrow:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.16);
    color: rgba(255, 255, 255, 0.92);
}
QPushButton#block-carousel-arrow:disabled {
    background: rgba(255, 255, 255, 0.02);
    border-color: rgba(255, 255, 255, 0.04);
    color: rgba(255, 255, 255, 0.26);
}

QPushButton#pill-block {
    background: rgba(255, 255, 255, 0.018); border: 1px solid transparent;
    border-radius: 6px; padding: 6px 14px;
    color: rgba(255, 255, 255, 0.46);
    font-size: 12px; font-weight: 500; min-height: 14px;
}
QPushButton#pill-block[compact="true"] {
    padding: 0;
    min-width: 44px;
    max-width: 44px;
    min-height: 26px;
    max-height: 26px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton#pill-block[compact="true"][generating="true"] {
    color: #ffffff;
    border: 1px solid rgba(228, 52, 74, 0.58);
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="0"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.93),
        stop:0.001 rgba(22, 16, 32, 0.93),
        stop:0.002 rgba(228, 52, 74, 0.12),
        stop:0.003 rgba(232, 178, 86, 0.23),
        stop:0.004 rgba(228, 52, 74, 0.12),
        stop:0.1 rgba(32, 24, 46, 0.93),
        stop:1 rgba(18, 13, 28, 0.93));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="1"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.94),
        stop:0.001 rgba(22, 16, 32, 0.94),
        stop:0.002 rgba(228, 52, 74, 0.13),
        stop:0.003 rgba(232, 178, 86, 0.24),
        stop:0.004 rgba(228, 52, 74, 0.13),
        stop:0.164 rgba(32, 24, 46, 0.94),
        stop:1 rgba(18, 13, 28, 0.94));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="2"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.95),
        stop:0.001 rgba(22, 16, 32, 0.95),
        stop:0.002 rgba(228, 52, 74, 0.13),
        stop:0.003 rgba(232, 178, 86, 0.24),
        stop:0.0287 rgba(228, 52, 74, 0.13),
        stop:0.229 rgba(32, 24, 46, 0.95),
        stop:1 rgba(18, 13, 28, 0.95));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="3"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.95),
        stop:0.001 rgba(22, 16, 32, 0.95),
        stop:0.002 rgba(228, 52, 74, 0.13),
        stop:0.003 rgba(232, 178, 86, 0.25),
        stop:0.093 rgba(228, 52, 74, 0.13),
        stop:0.293 rgba(32, 24, 46, 0.95),
        stop:1 rgba(18, 13, 28, 0.95));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="4"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.96),
        stop:0.001 rgba(22, 16, 32, 0.96),
        stop:0.002 rgba(228, 52, 74, 0.14),
        stop:0.0174 rgba(232, 178, 86, 0.26),
        stop:0.157 rgba(228, 52, 74, 0.14),
        stop:0.357 rgba(32, 24, 46, 0.96),
        stop:1 rgba(18, 13, 28, 0.96));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="5"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.96),
        stop:0.001 rgba(22, 16, 32, 0.96),
        stop:0.002 rgba(228, 52, 74, 0.14),
        stop:0.0817 rgba(232, 178, 86, 0.26),
        stop:0.222 rgba(228, 52, 74, 0.14),
        stop:0.422 rgba(32, 24, 46, 0.96),
        stop:1 rgba(18, 13, 28, 0.96));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="6"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.96),
        stop:0.001 rgba(22, 16, 32, 0.96),
        stop:0.0261 rgba(228, 52, 74, 0.14),
        stop:0.146 rgba(232, 178, 86, 0.26),
        stop:0.286 rgba(228, 52, 74, 0.14),
        stop:0.486 rgba(32, 24, 46, 0.96),
        stop:1 rgba(18, 13, 28, 0.96));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="7"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.96),
        stop:0.001 rgba(22, 16, 32, 0.96),
        stop:0.0904 rgba(228, 52, 74, 0.14),
        stop:0.21 rgba(232, 178, 86, 0.26),
        stop:0.35 rgba(228, 52, 74, 0.14),
        stop:0.55 rgba(32, 24, 46, 0.96),
        stop:1 rgba(18, 13, 28, 0.96));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="8"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.96),
        stop:0.001 rgba(22, 16, 32, 0.96),
        stop:0.155 rgba(228, 52, 74, 0.14),
        stop:0.275 rgba(232, 178, 86, 0.26),
        stop:0.415 rgba(228, 52, 74, 0.14),
        stop:0.615 rgba(32, 24, 46, 0.96),
        stop:1 rgba(18, 13, 28, 0.96));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="9"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.95),
        stop:0.0591 rgba(22, 16, 32, 0.95),
        stop:0.219 rgba(228, 52, 74, 0.13),
        stop:0.339 rgba(232, 178, 86, 0.25),
        stop:0.479 rgba(228, 52, 74, 0.13),
        stop:0.679 rgba(32, 24, 46, 0.95),
        stop:1 rgba(18, 13, 28, 0.95));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="10"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.95),
        stop:0.123 rgba(22, 16, 32, 0.95),
        stop:0.283 rgba(228, 52, 74, 0.13),
        stop:0.403 rgba(232, 178, 86, 0.24),
        stop:0.543 rgba(228, 52, 74, 0.13),
        stop:0.743 rgba(32, 24, 46, 0.95),
        stop:1 rgba(18, 13, 28, 0.95));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="11"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.94),
        stop:0.188 rgba(22, 16, 32, 0.94),
        stop:0.348 rgba(228, 52, 74, 0.13),
        stop:0.468 rgba(232, 178, 86, 0.24),
        stop:0.608 rgba(228, 52, 74, 0.13),
        stop:0.808 rgba(32, 24, 46, 0.94),
        stop:1 rgba(18, 13, 28, 0.94));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="12"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.93),
        stop:0.252 rgba(22, 16, 32, 0.93),
        stop:0.412 rgba(228, 52, 74, 0.12),
        stop:0.532 rgba(232, 178, 86, 0.23),
        stop:0.672 rgba(228, 52, 74, 0.12),
        stop:0.872 rgba(32, 24, 46, 0.93),
        stop:1 rgba(18, 13, 28, 0.93));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="13"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.92),
        stop:0.317 rgba(22, 16, 32, 0.92),
        stop:0.477 rgba(228, 52, 74, 0.11),
        stop:0.597 rgba(232, 178, 86, 0.22),
        stop:0.737 rgba(228, 52, 74, 0.11),
        stop:0.937 rgba(32, 24, 46, 0.92),
        stop:1 rgba(18, 13, 28, 0.92));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="14"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.92),
        stop:0.381 rgba(22, 16, 32, 0.92),
        stop:0.541 rgba(228, 52, 74, 0.11),
        stop:0.661 rgba(232, 178, 86, 0.21),
        stop:0.801 rgba(228, 52, 74, 0.11),
        stop:1 rgba(32, 24, 46, 0.92),
        stop:1 rgba(18, 13, 28, 0.92));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="15"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.91),
        stop:0.445 rgba(22, 16, 32, 0.91),
        stop:0.605 rgba(228, 52, 74, 0.11),
        stop:0.725 rgba(232, 178, 86, 0.21),
        stop:0.865 rgba(228, 52, 74, 0.11),
        stop:1 rgba(32, 24, 46, 0.91),
        stop:1 rgba(18, 13, 28, 0.91));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="16"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.90),
        stop:0.51 rgba(22, 16, 32, 0.90),
        stop:0.67 rgba(228, 52, 74, 0.10),
        stop:0.79 rgba(232, 178, 86, 0.20),
        stop:0.93 rgba(228, 52, 74, 0.10),
        stop:1 rgba(32, 24, 46, 0.90),
        stop:1 rgba(18, 13, 28, 0.90));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="17"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.90),
        stop:0.574 rgba(22, 16, 32, 0.90),
        stop:0.734 rgba(228, 52, 74, 0.10),
        stop:0.854 rgba(232, 178, 86, 0.20),
        stop:0.994 rgba(228, 52, 74, 0.10),
        stop:1 rgba(32, 24, 46, 0.90),
        stop:1 rgba(18, 13, 28, 0.90));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="18"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.90),
        stop:0.638 rgba(22, 16, 32, 0.90),
        stop:0.798 rgba(228, 52, 74, 0.10),
        stop:0.918 rgba(232, 178, 86, 0.20),
        stop:1 rgba(228, 52, 74, 0.10),
        stop:1 rgba(32, 24, 46, 0.90),
        stop:1 rgba(18, 13, 28, 0.90));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="19"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.90),
        stop:0.703 rgba(22, 16, 32, 0.90),
        stop:0.863 rgba(228, 52, 74, 0.10),
        stop:0.983 rgba(232, 178, 86, 0.20),
        stop:1 rgba(228, 52, 74, 0.10),
        stop:1 rgba(32, 24, 46, 0.90),
        stop:1 rgba(18, 13, 28, 0.90));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="20"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.90),
        stop:0.767 rgba(22, 16, 32, 0.90),
        stop:0.927 rgba(228, 52, 74, 0.10),
        stop:1 rgba(232, 178, 86, 0.20),
        stop:1 rgba(228, 52, 74, 0.10),
        stop:1 rgba(32, 24, 46, 0.90),
        stop:1 rgba(18, 13, 28, 0.90));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="21"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.91),
        stop:0.831 rgba(22, 16, 32, 0.91),
        stop:0.991 rgba(228, 52, 74, 0.11),
        stop:1 rgba(232, 178, 86, 0.21),
        stop:1 rgba(228, 52, 74, 0.11),
        stop:1 rgba(32, 24, 46, 0.91),
        stop:1 rgba(18, 13, 28, 0.91));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="22"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.92),
        stop:0.896 rgba(22, 16, 32, 0.92),
        stop:1 rgba(228, 52, 74, 0.11),
        stop:1 rgba(232, 178, 86, 0.21),
        stop:1 rgba(228, 52, 74, 0.11),
        stop:1 rgba(32, 24, 46, 0.92),
        stop:1 rgba(18, 13, 28, 0.92));
}
QPushButton#pill-block[compact="true"][generating="true"][gen_phase="23"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(22, 16, 32, 0.92),
        stop:0.96 rgba(22, 16, 32, 0.92),
        stop:1 rgba(228, 52, 74, 0.11),
        stop:1 rgba(232, 178, 86, 0.22),
        stop:1 rgba(228, 52, 74, 0.11),
        stop:1 rgba(32, 24, 46, 0.92),
        stop:1 rgba(18, 13, 28, 0.92));
}
QPushButton#pill-block:hover {
    background: rgba(255, 255, 255, 0.04);
    color: rgba(255, 255, 255, 0.85);
}

/* Блок с непросмотренными шотами — золотой акцент (LUMZ accent_gold). */
QPushButton#pill-block[unseen="true"] {
    background: rgba(212, 162, 86, 0.101);
    border: 1px solid rgba(212, 162, 86, 0.30);
    color: #d4a25a; font-weight: 500;
}
QPushButton#pill-block[unseen="true"]:hover {
    background: rgba(212, 162, 86, 0.18);
    color: #e1b46d;
}

/* Активный блок — как Higgsfield Video: тёмная плашка + светлый текст. */
QPushButton#pill-block[active="true"] {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    color: #f2f3f0; font-weight: 700;
}
QPushButton#pill-block[active="true"]:hover {
    background: #242628;
    border-color: rgba(255,255,255,0.10);
}
/* Активный + unseen: сохраняем тёмную активную плашку. */
QPushButton#pill-block[active="true"][unseen="true"] {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    color: #f2f3f0; font-weight: 700;
}
QPushButton#pill-block[active="true"][unseen="true"]:hover {
    background: #242628;
    border-color: rgba(255,255,255,0.10);
}

/* Кнопка «Референсы» — Higgsfield Upgrade/Assets style. */
QPushButton#pill-refs {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 0 14px;
    color: #f2f3f0;
    font-size: 12px; font-weight: 600;
}
QPushButton#pill-refs:hover {
    background: #2c2f31;
    border-color: rgba(255,255,255,0.16);
    color: #ffffff;
}
QPushButton#pill-refs[active="true"] {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    color: #f2f3f0; font-weight: 600;
}

/* Пульсирующая подсветка «Референсы» — янтарь (LUMZ accent_gold). */
QPushButton#pill-refs[has_notice="true"] {
    background: rgba(212, 162, 86, 0.18);
    border: 1px solid rgba(212, 162, 86, 0.50);
    color: #fefefe; font-weight: 500;
}
QPushButton#pill-refs[has_notice="true"][pulse_on="true"] {
    background: rgba(212, 162, 86, 0.30);
    border: 1px solid rgba(212, 162, 86, 0.70);
    color: #fefefe; font-weight: 500;
}
QPushButton#pill-refs[has_notice="true"]:hover {
    background: rgba(212, 162, 86, 0.25);
    color: #fefefe;
}

/* Пилюля «ЧАТ» — Higgsfield Upgrade/Assets style. */
QPushButton#pill-chat {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 0 14px;
    color: #f2f3f0;
    font-size: 12px; font-weight: 600;
}
QPushButton#pill-chat:hover {
    background: #2c2f31;
    border-color: rgba(255,255,255,0.16);
    color: #ffffff;
}
QPushButton#pill-chat[active="true"] {
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    color: #f2f3f0; font-weight: 600;
}

/* Кнопка «Удалить эпизод» — иконка корзины справа от полосы блоков.
   2026-05-08 редизайн: text_muted по умолчанию, hover → accent_red. */
QPushButton#delete-episode-btn {
    background: #242628; border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 0;
    color: #f2f3f0;
    font-size: 16px; font-weight: 500;
}
QPushButton#delete-episode-btn:hover {
    background: #2c2f31;
    border: 1px solid rgba(255,255,255,0.16);
    color: #ffffff;
}
QPushButton#delete-episode-btn:disabled {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
    color: rgba(255, 255, 255, 0.20);
}

/* Refs view — секции и карточки. 2026-05-08 редизайн Этап 6: LUMZ-стиль —
   приглушённые цвета заголовков, карточки в bg_subtle с border_default. */
QLabel#refs-section-header {
    color: rgba(255, 255, 255, 0.55); font-size: 11px;
    font-weight: 700; letter-spacing: 2px;
}
QLabel#refs-section-count {
    color: rgba(255, 255, 255, 0.40); font-size: 11px; font-weight: 500;
}
QFrame#ref-card {
    background: #191b1d;
    border: 1px solid #191b1d;
    border-radius: 8px;
}
QFrame#ref-card:hover {
    background: #191b1d;
    border-color: #191b1d;
}
QWidget#ref-card-info { background: transparent; }
QLabel#ref-name { color: #ffffff; font-size: 13px; font-weight: 500; }
QLabel#ref-tag  { color: rgba(255, 255, 255, 0.40); font-size: 11px; }

/* Карточка шота — 2026-05-08 редизайн Этап 5: лёгкая LUMZ-стиль
   карточка — фон bg_subtle (rgba(255,255,255,0.04)), border default
   (rgba(255,255,255,0.06)), radius 8px. На hover чуть ярче border. */
QFrame#card {
    background: #191b1d;
    border: none;
    border-radius: 8px;
}
QFrame#card:hover { border: none; }

/* Hover overlay — полупрозрачная плашка с двумя кнопками действий.
   Используется для refs view (overlay-action — composite кнопка с текстом).
   Шоты используют отдельный стиль shot-overlay (см. ниже) — там круглые
   иконки без текста, без затемнения всей картинки.
   2026-05-08 редизайн: hover state кнопок-действий на LUMZ accent_red. */
QFrame#regen-overlay {
    background: rgba(0, 0, 0, 0.78); border-radius: 8px;
}
QFrame#overlay-action {
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 10px;
}
QFrame#overlay-action:hover {
    background: rgba(228, 52, 74, 0.50);
    border: 1px solid rgba(228, 52, 74, 0.90);
}
QLabel#overlay-action-icon {
    color: #fff; font-size: 38px; font-weight: 600;
    background: transparent; border: none;
}
QLabel#overlay-action-text {
    color: #fff; font-size: 11px; font-weight: 600;
    background: transparent; border: none; letter-spacing: 1px;
}

/* Shot hover-overlay — без затемнения картинки. Только две круглые
   иконки (Edit + Regenerate) в нижней части на лёгком gradient'е,
   чтобы они были читаемы и на светлой, и на тёмной картинке.
   Сам контейнер shot-overlay прозрачен; gradient рисуется через
   shot-overlay-strip (нижняя плашка). */
QFrame#shot-overlay { background: transparent; border: none; }
QFrame#shot-overlay-strip {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(0,0,0,0), stop:1 rgba(0,0,0,0.55));
    border: none; border-bottom-left-radius: 6px; border-bottom-right-radius: 6px;
}
/* 2026-05-08 редизайн: shot-overlay-btn в LUMZ-палитре —
   нейтральная subtle подсветка default, accent_red на primary. */
QPushButton#shot-overlay-btn {
    background: rgba(255, 255, 255, 0.12); color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.20); border-radius: 22px;
    font-size: 18px; font-weight: 600; padding: 0;
    min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px;
}
QPushButton#shot-overlay-btn:hover {
    background: rgba(255, 255, 255, 0.18);
    border: 1px solid rgba(255, 255, 255, 0.30);
}
QPushButton#shot-overlay-btn[primary="true"] {
    background: rgba(228, 52, 74, 0.90);
    border: 1px solid #e4344a;
}
QPushButton#shot-overlay-btn[primary="true"]:hover {
    background: #e4344a;
    border: 1px solid #e4344a;
}
/* 2026-05-07: подсказка «Нажми чтобы открыть» поверх картинки шота
   (вместо двух круглых кнопок edit/regen). Edit/regen теперь живут
   внутри ShotViewerDialog. */
QLabel#shot-overlay-hint {
    color: #fff; font-size: 11px; font-weight: 600;
    background: transparent; border: none; padding: 4px 8px;
    /* 2026-05-08: убран `text-shadow` — Qt не поддерживает CSS3
       text-shadow (это HTML/CSS свойство), и при каждом применении
       стиля Qt спамил `[Qt WARNING] Unknown property text-shadow`
       в runtime.log. Эффект тени и так был незаметен на overlay
       поверх картинки. */
}

/* Кнопки на RefCard overlay (edit / delete / regen).
   2026-05-08 редизайн Этап 6: прямоугольная форма (radius 6 — как
   pill эпизодов), плотный непрозрачный фон (юзер: «убрать прозрачность»),
   SVG-иконки Lucide. Default — нейтральный тёмный, primary (regen) —
   LUMZ accent_red. */
QPushButton#ref-overlay-btn {
    background: rgba(20,20,24,0.82);
    color: #ffffff;
    border: 1px solid rgba(20,20,24,0.82);
    border-radius: 6px;
    padding: 0;
    min-width: 36px; max-width: 36px;
    min-height: 32px; max-height: 32px;
}
QPushButton#ref-overlay-btn:hover {
    background: rgba(207,255,34,0.22);
    border: 1px solid rgba(207,255,34,0.22);
}
QPushButton#ref-overlay-btn[primary="true"] {
    background: rgba(20,20,24,0.82);
    border: 1px solid rgba(20,20,24,0.82);
}
QPushButton#ref-overlay-btn[primary="true"]:hover {
    background: rgba(207,255,34,0.22);
    border: 1px solid rgba(207,255,34,0.22);
}

/* Header — LUMZ + красный квадрат + Storyboard Studio (всё в одной rich-text QLabel).
   2026-05-08 редизайн: шапка стала карточкой с фоном bg_panel + border + radius_lg.
   Табы перенесены ВНУТРЬ шапки как pill-группа справа (см. ниже #header-tabs). */
QLabel#header-version    { font-size: 12px; color: #666; }

/* Header card — карточка шапки с лого LUMZ слева и табами справа */
QFrame#header-card {
    background: #121313;
    border: none;
    border-radius: 14px;
}

/* Tabs-pill-group — контейнер вокруг трёх pill-кнопок «Редактор/Актёры/Настройки» */
QFrame#header-tabs {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 8px;
    min-height: 34px;
    max-height: 34px;
}
/* Lang-wrapper — невидимая обёртка вокруг кнопки переключения языка.
   Те же geometric параметры (padding 3+3 + 1px border) что у
   `header-tabs` чтобы lang-btn внутри сидела на той же высоте что
   pill-кнопки внутри tabs-group. Но фон и граница — прозрачные:
   обёртка не видна, выглядит как одиночная пилюля рядом с группой. */
QFrame#lang-wrapper {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
}
/* Каждая pill-кнопка таба */
QPushButton#tab-pill {
    background: transparent; color: rgba(255, 255, 255, 0.55);
    border: none; border-radius: 6px;
    padding: 0 14px; font-size: 12px; font-weight: 600;
    min-height: 28px;
    max-height: 28px;
}
QPushButton#tab-pill:hover {
    color: #e8b86a;
}
/* Активный таб — тёмная плашка + жёлтый текст как hover. */
QPushButton#tab-pill[active="true"] {
    background: #242628; color: #e8b86a; font-weight: 600;
}
/* 2026-06-20: акцентная pill «Генератор» — тёплый янтарь (#d4a256, accent_gold
   из палитры), выделяется среди серых табов. Через [accent="true"] поверх
   objectName tab-pill, чтобы не ломать общий _sync_header_tab_active. */
QPushButton#tab-pill[accent="true"] {
    color: #d4a257;
}
QPushButton#tab-pill[accent="true"]:hover {
    color: #e8b86a;
}
QPushButton#tab-pill[accent="true"][active="true"] {
    background: #242628; color: #e8b86a; font-weight: 600;
}
/* Этап 2 (формат): сегмент-переключатель 9:16 | 16:9 в шапке. Активная
   половина — фирменный красный (#e63946, как точка в логотипе), неактивная
   приглушённая. По образцу #header-tabs + #tab-pill. */
QFrame#aspect-seg {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 8px;
    min-height: 34px;
    max-height: 34px;
}
QPushButton#aspect-seg-btn {
    background: transparent; color: rgba(255, 255, 255, 0.55);
    border: none; border-radius: 6px;
    padding: 0 12px; font-size: 12px; font-weight: 600;
    min-height: 28px;
    max-height: 28px;
}
QPushButton#aspect-seg-btn:hover { color: #e8b86a; }
QPushButton#aspect-seg-btn[active="true"] {
    background: #242628;
    color: #e8b86a;
    border: none;
}
QPushButton#aspect-seg-btn:disabled { color: rgba(255, 255, 255, 0.25); }

/* Старый QTabBar — оставлен в QSS на случай если где-то ещё используется,
   но в главном окне tab-bar СКРЫТ программно (см. _build_ui). */
QTabBar::tab {
    background: transparent; color: #888; padding: 10px 22px;
    border: none; border-bottom: 2px solid transparent;
    font-size: 13px; font-weight: 500;
}
QTabBar::tab:selected   { color: #fff; border-bottom: 2px solid #e63946; }
QTabBar::tab:hover:!selected { color: #ccc; }
QTabWidget::pane        { border: none; background: transparent; }
QTabWidget::tab-bar     { left: 28px; }   /* отступ слева как у контента */

/* Тонкие разделительные линии между шапкой / табами / контентом */
QFrame#header-divider, QFrame#tabs-divider {
    background: rgba(255, 255, 255, 0.06); max-height: 1px; min-height: 1px;
    border: none;
}
/* Вертикальный сепаратор между пилюлями блоков и пилюлей «Референсы».
   2026-05-08 редизайн: тоньше (border_default rgba(255,255,255,0.06))
   чтобы вписаться в общий приглушённый стиль blocks-bar. */
QFrame#pills-vsep {
    background: rgba(255, 255, 255, 0.06); border: none;
}

/* Заголовки */
QLabel#episode-title    { font-size: 16px; color: #fff; font-weight: 500; }
/* Плашка «СЕРИЯ NN» / название эпизода — Higgsfield Upgrade/Assets style. */
QPushButton#episode-title-btn {
    font-size: 11px; color: #f2f3f0; font-weight: 600;
    background: #242628;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 0 12px;
    text-align: left;
    letter-spacing: 0.3px;
}
QPushButton#episode-title-btn:hover {
    background: #2c2f31;
    border-color: rgba(255,255,255,0.16);
}
QPushButton#episode-title-btn:pressed {
    background: #1f2123;
}
QLabel#episode-duration { font-size: 13px; color: #888; }
/* 2026-05-08 редизайн Этап 5: заголовок над сторибордами в LUMZ-стиле —
   text_primary (#fff), font 14, обычный вес 500, без letter-spacing. */
QLabel#block-title      { font-size: 14px; color: #b8b8b8; font-weight: 500; letter-spacing: 0; }

/* Карточка шота — внутренние подписи */
QLabel#shot-num         { font-size: 13px; font-weight: 600; color: #878788; }
QLabel#shot-dur         { font-size: 11px; color: #666667; }
QLabel#shot-desc        { font-size: 11px; color: #878788; }
QLabel#shot-dialog      { font-size: 11px; color: #b9a7e5; font-style: italic; }
QLabel#step-label       { font-size: 11px; color: #5a8a5a; }
QLabel#new-badge {
    color: #ffaa44; font-size: 10px; font-weight: bold;
    background: #2a1f0a; border: 1px solid #4a3010; border-radius: 4px;
    padding: 1px 6px;
}
QLabel#gen-time         { font-size: 10px; color: #5a8aaa; }


/* Settings tab — современный вид по макету: рамки с тонкими разделителями */
QFrame#settings-group {
    background: rgba(20, 16, 30, 0.5); border: 1px solid #2a2238; border-radius: 12px;
}
/* Тёмная «ванна»-карточка (Коммит 7, 2026-06-17) — ТОЛЬКО для трёх верхних
   карточек Настроек (Проект / О приложении / AI-аккаунт). Стиль ванны
   ProviderToggle/ModeSegment. #settings-group глобально НЕ трогаем. */
QFrame#settings-group-tile {
    background: #241c34; border: 1px solid #3a2f52; border-radius: 12px;
}
QLabel#settings-section {
    font-size: 11px; font-weight: 700;
    color: rgba(255, 255, 255, 0.55); letter-spacing: 2.5px;
}

/* Кнопка-строка внутри #settings-group (открыть папку): без рамки внутри, как пункт меню */
QPushButton#settings-row-btn {
    background: transparent; border: none; padding: 16px 20px;
    color: #ddd; font-size: 13px; text-align: left;
}
QPushButton#settings-row-btn:hover  { background: rgba(60, 48, 90, 0.25); color: #fff; }
QPushButton#settings-row-btn:pressed { background: rgba(60, 48, 90, 0.4); }

/* Компактная кнопка настроек — dark action-style как References/Chat. */
QPushButton#settings-light-btn {
    background: #242628; color: #f2f3f0;
    border: 1px solid #242628; border-radius: 8px;
    padding: 8px 14px; font-size: 13px; font-weight: 600;
    text-align: center;
}
QPushButton#settings-light-btn:hover { background: #2c2f31; color: #ffffff; border-color: #2c2f31; }
QPushButton#settings-light-btn:pressed { background: #1f2123; color: #ffffff; border-color: #1f2123; }
QPushButton#settings-light-btn:disabled {
    background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.30); border-color: rgba(255,255,255,0.04);
}

/* Строка ключ-значение внутри about (Версия приложения  v1.0.12) */
QWidget#settings-row     { background: transparent; }
QLabel#settings-row-key  { color: #aaa; font-size: 13px; }
QLabel#settings-row-val  { color: #fff; font-size: 13px; font-weight: 500; }
QFrame#settings-divider  { background: rgba(255, 255, 255, 0.06); border: none; }

QStatusBar              { background: #121313; color: #777; font-size: 11px; border-top: none; }
QStatusBar::item        { border: none; }

/* Скроллбары — тонкие, полупрозрачные. Кросс-платформенный стиль (Mac + Windows). */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 4px 2px 4px 2px; border: none;
}
QScrollBar::handle:vertical {
    background: rgba(180, 160, 220, 0.18); border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover    { background: rgba(180, 160, 220, 0.32); }
QScrollBar::handle:vertical:pressed  { background: rgba(180, 160, 220, 0.45); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    background: none; border: none; height: 0; width: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 2px 4px 2px 4px; border: none;
}
QScrollBar::handle:horizontal {
    background: rgba(180, 160, 220, 0.18); border-radius: 4px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover    { background: rgba(180, 160, 220, 0.32); }
QScrollBar::handle:horizontal:pressed  { background: rgba(180, 160, 220, 0.45); }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    background: none; border: none; height: 0; width: 0;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }

QSlider::groove:horizontal {
    background: rgba(255,255,255,0.12); height: 4px; border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #8a8d92; border-radius: 2px;
}
QSlider::add-page:horizontal {
    background: rgba(255,255,255,0.10); border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #b8bbc0; border: 1px solid rgba(255,255,255,0.20);
    width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background: #d0d3d8; border-color: #8a8d92;
}

QProgressBar {
    background: #1a1a1a; border: none; border-radius: 3px; height: 5px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a7a3a, stop:1 #5ab85a);
    border-radius: 3px;
}

QFrame#update-banner {
    background: rgba(31, 38, 48, 0.85); border: 1px solid #2d4060; border-radius: 8px;
}
QLabel#update-text       { font-size: 13px; color: #88aadd; }
QPushButton#update-btn {
    background: #2a3d5a; border: 1px solid #3d5680; border-radius: 6px;
    padding: 6px 14px; color: #b0d0ff; font-size: 12px;
}
QPushButton#update-btn:hover { background: #344870; }

QFrame#app-update-banner {
    background: rgba(32, 26, 48, 0.85); border: 1px solid #503070; border-radius: 8px;
}
QLabel#app-update-text   { font-size: 13px; color: #cc99ff; }
QPushButton#app-update-btn {
    background: #3a2560; border: 1px solid #5a3880; border-radius: 6px;
    padding: 6px 14px; color: #e0bbff; font-size: 12px;
}
QPushButton#app-update-btn:hover { background: #462e72; }

QFrame#admin-send-frame {
    background: rgba(42, 31, 58, 0.7); border: 1px solid #4a3060; border-radius: 10px;
}
QPushButton#admin-send {
    background: #3a2560; border: 1px solid #5a3880; color: #e0bbff;
    font-size: 13px; font-weight: 500; padding: 11px;
}
QPushButton#admin-send:hover { background: #462e72; }
QPushButton#admin-send:disabled { background: #221830; color: #555; border-color: #2a2240; }

QLabel#stats-label { font-size: 10px; color: #6d5d8c; }
QLabel#admin-send-title { font-size: 13px; color: #c090ff; font-weight: 500; }
QLabel#admin-send-desc  { font-size: 11px; color: #6d5d8c; }

/* Админ-зона (Коммит 6, 2026-06-16) — все админ-блоки в одной красной рамке
   с бейджем-плашкой «АДМИН-НАСТРОЙКИ» первой строкой внутри. Видна только
   админу (ветка if self._is_admin). */
QFrame#admin-zone {
    background: rgba(228, 52, 74, 0.06);
    border: 2px solid rgba(228, 52, 74, 0.85);
    border-radius: 12px;
}
QLabel#admin-zone-badge {
    color: #fefefe; background: #e4344a; border-radius: 6px;
    padding: 4px 12px; font-size: 12px; font-weight: 700; letter-spacing: 1px;
}

/* Show selector (dropdown) */
/* QComboBox — селектор сериала. 2026-05-08 редизайн: LUMZ-стиль —
   приглушённый фон bg_subtle, граница border_strong, radius 8px,
   padding 7×16. */
QComboBox {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    padding: 7px 16px;
    color: #ffffff;
    font-size: 12px;
    min-width: 160px;
}
QComboBox:hover         { border-color: rgba(255, 255, 255, 0.20); }
QComboBox::drop-down    { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #191b1d;
    border: 1px solid rgba(255, 255, 255, 0.12);
    selection-background-color: rgba(228, 52, 74, 0.20);
    color: #ddd; padding: 4px;
}

/* Переключатель языка в шапке. 2026-05-08 редизайн: ИДЕНТИЧНЫЙ
   стиль с активной tab-pill кнопкой (Editor) — те же `border: none`,
   `padding: 6px 14px`, `font-size: 12px`. Без border совпадение
   высоты гарантировано (раньше 1px border делал кнопку на 2px
   крупнее — она «выпадала» из линии Editor/Actors/Settings).
   Фон чуть темнее чем у активного Editor-pill чтобы визуально
   отличить (это всё-таки отдельный action, не таб). */
QPushButton#lang-btn {
    background: rgba(255, 255, 255, 0.04);
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    color: #ffffff;
    font-size: 12px;
    font-weight: 500;
    min-width: 50px;
    text-align: center;
}
QPushButton#lang-btn:hover {
    background: rgba(255, 255, 255, 0.08);
}
QPushButton#lang-btn:pressed { background: rgba(255, 255, 255, 0.10); }

/* Выпадающий список языков (как на макете LUMZ) */
QMenu#lang-menu {
    background: #121313; border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 10px;
    padding: 6px;
}
QMenu#lang-menu::item {
    padding: 11px 22px; color: #ddd; font-size: 13px; border-radius: 8px;
    min-width: 160px;
}
QMenu#lang-menu::item:selected {
    background: #242628; color: #f2f3f0;
}

/* Сегмент-контрол провайдера генерации (ProviderToggle) — БЕЗ QSS.
   2026-06-16: виджет рисует себя сам кастомным paintEvent (ванна+пилюля),
   потому что нативный macOS-QStyle перебивал QSS background/border на
   checkable-QPushButton (см. widgets/provider_toggle.py, секцию «ПОЧЕМУ
   КАСТОМНЫЙ paintEvent»). Палитра захардкожена в самом виджете —
   кроссплатформенно идентична. Здесь стилей для него нет намеренно. */
"""

# ─── Утилиты — промпты ────────────────────────────────────────────────────────

def load_api_key() -> str:
    """Возвращает API-ключ Fast Gen.
    Приоритет: QSettings (юзер вставил в Настройках) → .env файл (fallback).
    Юзер может в любой момент обновить ключ через UI без перезапуска Studio
    и без правки .env. Старый .env остаётся как дефолт для первого запуска.
    """
    try:
        qs = QSettings(APP_ORG, APP_NAME)
        ui_key = qs.value("fastgen_api_key", "", type=str)
        if ui_key and ui_key.strip():
            return ui_key.strip()
    except Exception:
        pass
    try:
        lines = [l.strip() for l in ENV_FILE.read_text().splitlines() if l.strip()]
        return lines[0]
    except Exception:
        return ""


def next_api_key():
    """Round-robin обёртка над пулом ключей (key_pool) → (key, idx).

    2026-06-09 (фикс racy-idx): возвращает КОРТЕЖ (key, idx) — поток получает
    свой idx в одни руки (для лампочки + корректного failover-выбивания), не
    читая racy-глобал `last_index()`. При 1 настроенном ключе ведёт себя 1-в-1
    с load_api_key() (idx=0). При ЛЮБОЙ ошибке диспетчера — fallback на
    load_api_key() с idx=None (kill-switch: генерация не падает; None →
    лампочка молчит, disable_key(None)=no-op, чужой ключ не выбьется).

    Ленивый import key_pool — чтобы не тянуть модуль на уровне импорта
    storyboard_app и не плодить зависимостей при старте."""
    try:
        import key_pool
        k, idx = key_pool.next_key_with_idx()
        if k:
            return k, idx
    except Exception:
        traceback.print_exc()
    return load_api_key(), None


def acquire_api_slot():
    """Троттл-обёртка над key_pool.acquire_slot() → (key, idx, ok).

    ok=True  → слот взят (или kill-switch fallback без троттла) — бери key и
               работай, в finally обязательно вызови release_api_slot(idx).
    ok=False → все живые ключи на потолке (≥MAX_INFLIGHT_PER_KEY/ключ) — подожди
               и повтори. При ЛЮБОЙ ошибке пула → fallback load_api_key() с
               idx=None, ok=True (генерация не падает, троттл просто отключается).

    Ленивый import key_pool — как в next_api_key()."""
    try:
        import key_pool
        return key_pool.acquire_slot()
    except Exception:
        traceback.print_exc()
    return load_api_key(), None, True


def release_api_slot(idx) -> None:
    """Освободить слот ключа idx (key_pool.release_slot). idx=None → no-op.
    Зовётся в finally потока — НИКОГДА не кидает."""
    try:
        import key_pool
        key_pool.release_slot(idx)
    except Exception:
        pass


def save_api_key(key: str) -> None:
    """Сохраняет API-ключ Fast Gen в QSettings + синхронизирует в `.env`
    project root'а (bridge для pipeline.py, который читает только из .env)."""
    key = (key or "").strip()
    try:
        QSettings(APP_ORG, APP_NAME).setValue("fastgen_api_key", key)
    except Exception:
        traceback.print_exc()
    try:
        project_root = _project_root_for_provider_sync()
        if project_root is not None:
            sync_api_key_to_env(project_root, key)
    except Exception:
        traceback.print_exc()


# ─── fal.ai ключ (вкладка «Камера»: qwen multi-angle) ────────────────────
# 2026-07-02: хранение по образцу FastGen-ключа (load_api_key): приоритет
# QSettings (юзер вставил на вкладке, работает без перезапуска) → сайдкар
# fal_key.txt в project_root (fallback/бэкап; в .gitignore — содержит ключ).

def load_fal_key() -> str:
    """API-ключ fal.ai (формат id:secret). QSettings → fal_key.txt → ''."""
    try:
        qs = QSettings(APP_ORG, APP_NAME)
        ui_key = qs.value("fal_api_key", "", type=str)
        if ui_key and ui_key.strip():
            return ui_key.strip()
    except Exception:
        pass
    try:
        root = _project_root_for_provider_sync()
        if root is not None:
            return (root / "fal_key.txt").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def save_fal_key(key: str) -> None:
    """Сохраняет fal-ключ: QSettings + сайдкар fal_key.txt (project_root).
    Пустой key → очистка обоих мест. Ошибки молча (Studio не падает)."""
    key = (key or "").strip()
    try:
        QSettings(APP_ORG, APP_NAME).setValue("fal_api_key", key)
    except Exception:
        traceback.print_exc()
    try:
        root = _project_root_for_provider_sync()
        if root is not None:
            dst = root / "fal_key.txt"
            if key:
                dst.write_text(key, encoding="utf-8")
            elif dst.exists():
                dst.unlink()
    except Exception:
        traceback.print_exc()


def sync_api_key_to_env(project_root: Path, key: str) -> None:
    """Bridge для pipeline.py: обновляет первую строку .env свежим
    Fast Gen API ключом. Сохраняет остальные строки (Anthropic ключ
    и т.д.) и trailing newline. Идемпотентно: если первая строка
    уже равна key — возвращает без записи (не дёргает mtime)."""
    try:
        key = (key or "").strip()
        if not key:
            return
        dst = Path(project_root) / ".env"
        if dst.exists():
            try:
                content = dst.read_text(encoding="utf-8")
                lines = content.splitlines()
                if lines and lines[0].strip() == key:
                    return  # ключ не изменился
                if lines:
                    lines[0] = key
                else:
                    lines = [key]
                ending = "\n" if content.endswith("\n") else ""
                dst.write_text("\n".join(lines) + ending, encoding="utf-8")
            except Exception:
                traceback.print_exc()
        else:
            # .env нет — создаём с одной строкой
            dst.write_text(key + "\n", encoding="utf-8")
    except Exception:
        traceback.print_exc()


# ─── Провайдер картинок (NARWHAL Nano Banana 2 / NB2 Lite / OpenAI) ──────
# Админский переключатель в Settings. Влияет ТОЛЬКО на массовую генерацию
# шотов (`GenerateThread`). Локации/объекты (`RefGenerateThread`) и regen
# рефов персонажей идут на OpenAI всегда (так было исторически).
#
# Сделан как fallback на случай когда NARWHAL captcha-сервис у Fast Gen
# лежит. При переключении на OpenAI — рефы автоматически режутся до 2
# (NARWHAL принимает 3-10, OpenAI flow внутри Fast Gen ломается на 3+
# pydantic-ошибкой). Content-policy OpenAI блокирует огнестрел/узнаваемых
# людей — для криминальных сцен не сработает, нужно знать.
IMAGE_PROVIDER_NARWHAL = "narwhal"
# 2026-07-02: Nano Banana 2 Lite — третий провайдер FastGen (model
# "nano-banana-2-lite", подтверждено официальной OpenAPI-схемой: провайдер
# flow, операция nano_banana_2_lite_image_generate, 4 кред. base, до 10 рефов).
IMAGE_PROVIDER_NARWHAL_LITE = "narwhal_lite"
IMAGE_PROVIDER_OPENAI  = "openai"
# Все валидные значения ключей image_provider_actors / image_provider_admin.
IMAGE_PROVIDERS_ALL = (IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_NARWHAL_LITE,
                       IMAGE_PROVIDER_OPENAI)
# Мапа провайдер → model-строка payload'а FastGen (v6 generations).
# Единственный источник истины для тредов (threads/generate.py); CLI
# (pipeline.py / generate_storyboards.py) держат свою копию — им нельзя
# импортировать storyboard_app (subprocess без Qt).
IMAGE_PROVIDER_MODEL = {
    IMAGE_PROVIDER_NARWHAL:      "nano-banana-2",
    IMAGE_PROVIDER_NARWHAL_LITE: "nano-banana-2-lite",
    IMAGE_PROVIDER_OPENAI:       "openai-image",
}
# Потолок reference_images для OpenAI v4 (/api/v4/openai/image/generate):
# сервер принимает 0-10 (maxItems:10) — проверено живым тестом (10 OK, 11 отбой).
OPENAI_MAX_REFS = 10
# Провайдеры с явной обрезкой рефов до OPENAI_MAX_REFS: OpenAI (как было)
# + NB2 Lite (схема заявляет до 10 рефов — режем, не полагаясь на сервер).
# NARWHAL (nano-banana-2) без обрезки — историческое поведение.
IMAGE_PROVIDERS_REF_CAPPED = (IMAGE_PROVIDER_OPENAI,
                              IMAGE_PROVIDER_NARWHAL_LITE)


def image_provider() -> str:
    """Возвращает идентификатор провайдера ('narwhal' | 'openai').

    Default: 'narwhal' (Nano Banana 2). Читается на каждый запуск шотового
    треда — переключатель работает без перезапуска Studio.
    """
    try:
        v = QSettings(APP_ORG, APP_NAME).value("image_provider", IMAGE_PROVIDER_NARWHAL, type=str)
        return v if v in (IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_OPENAI) else IMAGE_PROVIDER_NARWHAL
    except Exception:
        return IMAGE_PROVIDER_NARWHAL


def set_image_provider(value: str) -> None:
    """Сохраняет провайдер в QSettings. Принимает только known values.

    Дополнительно пишет `image_provider.txt` в project_root — это
    bridge-файл для `pipeline.py` (который запускается AI-агентом в
    subprocess'е и не имеет доступа к QSettings). См. `load_provider`
    в pipeline.py и `sync_image_provider_to_project` ниже.
    """
    try:
        if value not in (IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_OPENAI):
            value = IMAGE_PROVIDER_NARWHAL
        QSettings(APP_ORG, APP_NAME).setValue("image_provider", value)
        try:
            project_root = _project_root_for_provider_sync()
            if project_root is not None:
                sync_image_provider_to_project(project_root, value)
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()


def _project_root_for_provider_sync() -> Optional[Path]:
    """Получить project_root для записи bridge-файла. Возвращает None
    если корень не настроен (юзер ещё не прошёл онбординг)."""
    try:
        s = QSettings(APP_ORG, APP_NAME)
        raw = s.value("project_root", "", type=str)
        if not raw:
            return None
        p = Path(raw)
        return p if p.exists() else None
    except Exception:
        return None


def sync_image_provider_to_project(project_root: Path, value: str) -> None:
    """Пишет текущего провайдера в `<project_root>/image_provider.txt`.

    Это bridge для pipeline.py — он запускается AI-агентом в отдельном
    subprocess'е и не имеет доступа к QSettings. Вызывается:
    (1) из `set_image_provider_admin` при изменении настройки юзером
        (т.к. pipeline.py делает локации/объекты/сториборды — это
        админский контекст);
    (2) при старте Studio рядом с `sync_pipeline_py_to_project`, чтобы
        файл существовал даже если юзер ни разу не открывал Settings
        после установки.

    Все ошибки молча проглатываются — Studio не должна падать из-за
    проблем с этой синхронизацией.
    """
    try:
        if value not in IMAGE_PROVIDERS_ALL:
            value = IMAGE_PROVIDER_NARWHAL
        dst = Path(project_root) / "image_provider.txt"
        try:
            if dst.exists():
                old = dst.read_text(encoding="utf-8").strip()
                if old == value:
                    return  # не трогаем mtime если значение не менялось
        except Exception:
            pass
        dst.write_text(value, encoding="utf-8")
    except Exception:
        traceback.print_exc()


# 2026-05-23: разделение единого `image_provider` на два независимых:
#  • `image_provider_actors` — для актёрских референсов (видим ВСЕМ).
#    Влияет на GenerateActorRefThread, EditActorRefThread, и
#    RefGenerateThread когда путь содержит /characters/.
#  • `image_provider_admin` — для шотов/локаций/объектов (видим только
#    админу). Влияет на GenerateThread, RefGenerateThread когда путь
#    /locations/ или /objects/, и pipeline.py/generate_storyboards.py
#    через bridge-файл `image_provider.txt`.
# Старые `image_provider()` / `set_image_provider()` оставлены как dead
# code до отдельной задачи вычистки. Новые getter'ы при первом чтении
# мигрируют значение из старого ключа `image_provider`.


def image_provider_actors() -> str:
    """Провайдер для актёрских референсов (видим всем юзерам).

    Default: 'narwhal' (Nano Banana 2). При первом чтении мигрирует
    значение из старого общего ключа `image_provider` если новый ключ
    ещё не задан. Читается на каждый запуск actor-ref треда —
    переключатель работает без перезапуска Studio.
    """
    try:
        s = QSettings(APP_ORG, APP_NAME)
        v = s.value("image_provider_actors", None, type=str)
        if not v:
            # Миграция со старого общего ключа при первом чтении.
            old = s.value("image_provider", IMAGE_PROVIDER_NARWHAL, type=str)
            v = old if old in IMAGE_PROVIDERS_ALL else IMAGE_PROVIDER_NARWHAL
            s.setValue("image_provider_actors", v)
        return v if v in IMAGE_PROVIDERS_ALL else IMAGE_PROVIDER_NARWHAL
    except Exception:
        return IMAGE_PROVIDER_NARWHAL


def set_image_provider_actors(value: str) -> None:
    """Сохраняет провайдер актёров. В `image_provider.txt` НЕ пишет —
    batch-скрипты (pipeline.py / generate_storyboards.py) актёрские
    рефы не генерят, они работают только через Studio thread'ы
    (GenerateActorRefThread / EditActorRefThread).
    """
    try:
        if value not in IMAGE_PROVIDERS_ALL:
            value = IMAGE_PROVIDER_NARWHAL
        QSettings(APP_ORG, APP_NAME).setValue("image_provider_actors", value)
    except Exception:
        traceback.print_exc()


def image_provider_admin() -> str:
    """Провайдер для шотов сторибордов + рефов локаций/объектов
    (видим только админу). Default: 'narwhal'. Миграция аналогична
    `image_provider_actors`.
    """
    try:
        s = QSettings(APP_ORG, APP_NAME)
        v = s.value("image_provider_admin", None, type=str)
        if not v:
            old = s.value("image_provider", IMAGE_PROVIDER_NARWHAL, type=str)
            v = old if old in IMAGE_PROVIDERS_ALL else IMAGE_PROVIDER_NARWHAL
            s.setValue("image_provider_admin", v)
        return v if v in IMAGE_PROVIDERS_ALL else IMAGE_PROVIDER_NARWHAL
    except Exception:
        return IMAGE_PROVIDER_NARWHAL


def set_image_provider_admin(value: str) -> None:
    """Сохраняет провайдер для админского контекста + синхронизирует
    в `image_provider.txt` (bridge-файл для pipeline.py и
    generate_storyboards.py — они делают локации/объекты/сториборды,
    т.е. админский контекст).
    """
    try:
        if value not in IMAGE_PROVIDERS_ALL:
            value = IMAGE_PROVIDER_NARWHAL
        QSettings(APP_ORG, APP_NAME).setValue("image_provider_admin", value)
        try:
            project_root = _project_root_for_provider_sync()
            if project_root is not None:
                sync_image_provider_to_project(project_root, value)
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()


# 2026-05-23 (Этап 3.1): settings layer для скоростей речи актёров
# в режиме B. Три параметра — fast / normal / slow, в словах/сек.
# Дефолты (4.0 / 3.5 / 2.3) дублируют значения из
# agents/timing_post_check.py:66-69 (SPEECH_CONFIG["b"]["speeds"]).
# В подэтапе 3.3 SPEECH_CONFIG будет ЧИТАТЬ из этих функций —
# settings станет единым source of truth. UI слайдеры — 3.2.


def speech_speed_b_fast() -> float:
    """Скорость речи 'fast' для режима B (слов/сек).
    Default: 4.0. Используется в SPEECH_CONFIG["b"]["speeds"]["fast"]
    (подключение — Этап 3.3). Кламп 1.0–10.0 — защита от кривых
    значений в QSettings.
    """
    try:
        v = QSettings(APP_ORG, APP_NAME).value("speech_speed_b_fast", 4.0)
        m = float(v)
        return max(1.0, min(10.0, m))
    except Exception:
        return 4.0


def set_speech_speed_b_fast(value: float) -> None:
    """Сохраняет скорость 'fast' для режима B. Кламп 1.0–10.0."""
    try:
        v = max(1.0, min(10.0, float(value)))
        QSettings(APP_ORG, APP_NAME).setValue("speech_speed_b_fast", v)
    except Exception:
        traceback.print_exc()


def speech_speed_b_normal() -> float:
    """Скорость речи 'normal' для режима B (слов/сек).
    Default: 3.5. Кламп 1.0–10.0.
    """
    try:
        v = QSettings(APP_ORG, APP_NAME).value("speech_speed_b_normal", 3.5)
        m = float(v)
        return max(1.0, min(10.0, m))
    except Exception:
        return 3.5


def set_speech_speed_b_normal(value: float) -> None:
    """Сохраняет скорость 'normal' для режима B. Кламп 1.0–10.0."""
    try:
        v = max(1.0, min(10.0, float(value)))
        QSettings(APP_ORG, APP_NAME).setValue("speech_speed_b_normal", v)
    except Exception:
        traceback.print_exc()


def speech_speed_b_slow() -> float:
    """Скорость речи 'slow' для режима B (слов/сек).
    Default: 2.3. Кламп 1.0–10.0.
    """
    try:
        v = QSettings(APP_ORG, APP_NAME).value("speech_speed_b_slow", 2.3)
        m = float(v)
        return max(1.0, min(10.0, m))
    except Exception:
        return 2.3


def set_speech_speed_b_slow(value: float) -> None:
    """Сохраняет скорость 'slow' для режима B. Кламп 1.0–10.0."""
    try:
        v = max(1.0, min(10.0, float(value)))
        QSettings(APP_ORG, APP_NAME).setValue("speech_speed_b_slow", v)
    except Exception:
        traceback.print_exc()


def mode_c_versions_per_shot() -> int:
    """Mode C: сколько версий генерить на один шот (1-10, дефолт 1)."""
    try:
        v = QSettings(APP_ORG, APP_NAME).value("mode_c/versions_per_shot", 1)
        return max(1, min(10, int(v)))
    except Exception:
        return 1


def set_mode_c_versions_per_shot(n: int) -> None:
    """Mode C: сохраняет число версий на шот. Кламп 1-10."""
    try:
        v = max(1, min(10, int(n)))
        QSettings(APP_ORG, APP_NAME).setValue("mode_c/versions_per_shot", v)
    except Exception:
        traceback.print_exc()


def mode_c_parallel_blocks() -> int:
    """Mode C: сколько блоков генерить параллельно (1-10, дефолт 1).
    Пока только настройка — логика параллельных блоков отдельной задачей."""
    try:
        v = QSettings(APP_ORG, APP_NAME).value("mode_c/parallel_blocks", 1)
        return max(1, min(10, int(v)))
    except Exception:
        return 1


def set_mode_c_parallel_blocks(n: int) -> None:
    """Mode C: сохраняет число параллельных блоков. Кламп 1-10."""
    try:
        v = max(1, min(10, int(n)))
        QSettings(APP_ORG, APP_NAME).setValue("mode_c/parallel_blocks", v)
    except Exception:
        traceback.print_exc()


def mode_c_concurrent_shots() -> int:
    """Конвейер шотов: сколько шотов генерить одновременно (1-20, дефолт 4),
    каждый со своими версиями. Читается пулом слотов _pump_shot_window. Ключ
    отдельный (`mode_c/concurrent_shots`) от legacy no-op `mode_c/parallel_blocks`,
    который оставлен как есть."""
    try:
        v = QSettings(APP_ORG, APP_NAME).value("mode_c/concurrent_shots", 4)
        return max(1, min(20, int(v)))
    except Exception:
        return 4


def set_mode_c_concurrent_shots(n: int) -> None:
    """Сохраняет число одновременных шотов конвейера. Кламп 1-20."""
    try:
        v = max(1, min(20, int(n)))
        QSettings(APP_ORG, APP_NAME).setValue("mode_c/concurrent_shots", v)
    except Exception:
        traceback.print_exc()


def find_ref_image(filename: str) -> Optional[Path]:
    """Резолвит имя файла рефа в физический путь на диске.

    2026-05-15: сравнение через NFC-нормализацию обеих сторон.
    Причина: на macOS APFS имена с кириллицей + диакритикой
    хранятся в NFD (декомпозированной форме: «Е»+◌̈ вместо «Ё»),
    а refs_decisions / .txt промпты пишутся в NFC. Без normalize
    «Ё» (NFC `\\xd0\\xa1\\xd1\\x91`) ≠ «Е+◌̈» (NFD `\\xd0\\xb5\\xcc\\x88`)
    → возвращали None → реф молча выпадал из payload → модель
    видела `[@]imgN` в тексте без файла → рисовала случайное лицо.
    Кейс: ep24/block_1/shot3 «Адвокат_Victor…Тёмно-серый…» —
    Viktor подменился на случайного персонажа.

    На Windows NTFS подобной проблемы нет (там форма что записал
    создатель), но normalize не мешает — если оба NFC, оба NFD,
    или один NFC а другой NFD, всё равно сматчится.
    """
    target = unicodedata.normalize("NFC", filename)
    target_stem = unicodedata.normalize(
        "NFC", filename.rsplit(".", 1)[0]) if "." in filename else target
    for directory in [LOCATIONS_DIR, OBJECTS_DIR]:
        for f in directory.glob("*"):
            if not f.is_file():
                continue
            f_name = unicodedata.normalize("NFC", f.name)
            f_stem = unicodedata.normalize("NFC", f.stem)
            if f_name == target or f_stem == target or f_stem == target_stem:
                return f
    for f in CHARACTERS_DIR.rglob("*"):
        if not f.is_file():
            continue
        f_name = unicodedata.normalize("NFC", f.name)
        f_stem = unicodedata.normalize("NFC", f.stem)
        if f_name == target or f_stem == target or f_stem == target_stem:
            return f
    return None


def parse_refs(prompt_text: str) -> Dict[str, Path]:
    refs: Dict[str, Path] = {}
    for m in re.finditer(r'#\s*\[@\]img(\d+)\s*=\s*(.+?)(?:\s*$)', prompt_text, re.MULTILINE):
        tag   = f"[@]img{m.group(1)}"
        found = find_ref_image(m.group(2).strip())
        if found:
            refs[tag] = found
    return refs


def parse_shots(prompt_text: str) -> List[Dict]:
    shots: List[Dict] = []
    panel_re = re.compile(
        r'Panel\s+(\d+)\s+\([^)]+\):(.*?)(?=Panel\s+\d+\s+\(|===ПРОМПТ_БЛОК.*?КОНЕЦ|$)',
        re.DOTALL,
    )
    for m in panel_re.finditer(prompt_text):
        body     = m.group(2).strip()
        is_blank = "COMPLETELY BLANK" in body
        ann_m    = re.search(r'Text annotation below Panel \d+:\s*"([^"]+)"', body)
        ann      = ann_m.group(1) if ann_m else ""

        shot_num, duration, description = int(m.group(1)), "", ""
        if ann:
            parts = [p.strip() for p in ann.split("/")]
            if parts:
                sm = re.search(r'SHOT\s*(\d+)', parts[0], re.IGNORECASE)
                if sm:
                    shot_num = int(sm.group(1))
            if len(parts) >= 2:
                duration = parts[1]
            if len(parts) >= 3:
                description = " / ".join(parts[2:])

        shots.append(dict(shot_num=shot_num, duration=duration,
                          description=description, is_blank=is_blank))

    while len(shots) < PANELS:
        shots.append(dict(shot_num=len(shots)+1, duration="", description="",
                          is_blank=True))
    return shots[:PANELS]


# 2026-06-04 — анти-«большая голова»: каждый скетч-шот получает инструкцию
# про естественные пропорции тела. Вставляется в КОНЕЦ шапки (после всех
# layout-скрабов) внутри extract_shot_prompt → попадает в скетч/regen payload
# и на старых, и на новых .txt без перегенерации файлов. Realistic/edit этот
# вывод отбрасывают (см. _collect_shot_refs), Seedance читает сырой .txt.
_BODY_PROPORTION_RULE = (
    "Render all characters with realistic, natural human body proportions — "
    "head sized correctly relative to the body (an adult head is roughly "
    "one-seventh to one-eighth of full standing height). Avoid an oversized "
    "or enlarged head, avoid a bobblehead look, keep the head-to-body ratio "
    "anatomically correct."
)


_STRICT_CAMERA_RULE = (
    "STRICT CAMERA RULE: The CAMERA line defines the exact viewpoint, angle "
    "and framing for this shot. Render the scene strictly from that camera "
    "position. This camera instruction overrides any default or habitual "
    "framing — do not fall back to a generic in-car composition."
)


def extract_shot_prompt(prompt_text: str, panel_idx: int, aspect: str = "9:16") -> Optional[str]:
    """Извлекает контент одного шота как самостоятельный 9:16 промпт.

    Берёт общий хедер блока (стиль, рефы, персонажи) + тело конкретного Panel,
    адаптирует layout-инструкции с "ONE wide horizontal sheet, 4 panels"
    на "Single vertical 9:16 panel".

    Возвращает None если панель помечена как COMPLETELY BLANK или не найдена.
    """
    target = panel_idx + 1

    cleaned = "\n".join(
        l for l in prompt_text.splitlines()
        if not l.startswith("# [@]") and not l.startswith("===ПРОМПТ_БЛОК")
    ).strip()

    first_panel_m = re.search(r'(?im)^\s*Panel\s+1\s+\(', cleaned)
    if not first_panel_m:
        return None

    header      = cleaned[:first_panel_m.start()].rstrip()
    panels_text = cleaned[first_panel_m.start():]

    panel_pat = re.compile(
        r'(?is)Panel\s+(\d+)\s+\([^)]+\):\s*(.*?)(?=Panel\s+\d+\s+\(|$)'
    )
    panel_body: Optional[str] = None
    for m in panel_pat.finditer(panels_text):
        if int(m.group(1)) == target:
            panel_body = m.group(2).strip()
            break

    if not panel_body or "COMPLETELY BLANK" in panel_body.upper():
        return None

    _panel = frame_format.single_panel_phrase(aspect)
    # Этап 3.2: regex обобщён под ОБА формата — 9:16 «ONE wide horizontal sheet,
    # EXACTLY N vertical panels…» и 16:9 «ONE storyboard sheet, EXACTLY N
    # horizontal panels…». На 9:16-шапке результат идентичен прежнему (контроль в).
    header_new = re.sub(
        r'(?i)(Film storyboard layout,\s*)?ONE\s+(?:\w+\s+){1,2}sheet,?\s*'
        r'EXACTLY\s+\d+\s+(?:vertical|horizontal)\s+panels[^.]*\.\s*',
        _panel + ' ',
        header,
    )
    if header_new == header:
        header_new = re.sub(
            r'(?i)ONE\s+(?:\w+\s+){1,2}sheet[^.]*\.',
            _panel,
            header,
        )

    header_new = re.sub(
        r'(?i)Blank\s+panels:[^.]*\.(?:\s*(?:Pure\s+white\s+space|No\s+drawing|No\s+text\s+annotation)[^.]*\.)*',
        '',
        header_new,
    )

    # 2026-05-07: PromptWriter иногда вставляет metadata-блок вида
    # `[sketch storyboard, 4 panels, 16:9, pencil sketch, ...]`. Слова
    # «N panels» и «16:9» в нём заставляют модель генерить landscape
    # storyboard-sheet вместо одиночной 9:16 панели — даже когда payload
    # шлёт `aspect_ratio: "9:16"` (модель слушается текста промпта). Здесь
    # вычищаем layout-указания ТОЛЬКО из квадратных metadata-блоков, не
    # трогая стиль-теги внутри них.
    def _scrub_layout_in_brackets(m):
        body = m.group(1)
        body = re.sub(r'(?i)\b\d+\s*panels?\b', '', body)
        body = re.sub(r'(?i)\b\d+\s*:\s*\d+\b', '', body)
        body = re.sub(r'(?i)\bstoryboard\s+sheet\b', '', body)
        # После удалений могут остаться `, , ,` — разбиваем по запятой,
        # выкидываем пустые, склеиваем обратно. Надёжнее regex'а.
        parts = [p.strip() for p in body.split(',')]
        parts = [p for p in parts if p]
        body = ', '.join(parts)
        return f'[{body}]' if body else ''
    header_new = re.sub(r'\[([^\[\]]*)\]', _scrub_layout_in_brackets, header_new)

    header_final = header_new.strip()
    if "natural human body proportions" not in header_final:
        header_final = f"{header_final} {_BODY_PROPORTION_RULE}"
    # 2026-06-07 (Mode C): жёсткая директива про камеру — дописывается в шапку
    # ТОЛЬКО в Mode C. Гейт через QSettings-режим (lazy import, фолбэк 'a' при
    # сбое — тот же паттерн что в build_user_prompt, Коммит 2). При a/b/d или
    # ошибке импорта директива НЕ добавляется, header_final посимвольно прежний.
    try:
        from agents.mode_loader import get_current_mode as _get_mode
        _mode_c = (_get_mode() == 'c')
    except Exception:
        _mode_c = False
    if _mode_c:
        header_final = f"{header_final} {_STRICT_CAMERA_RULE}"
    return f"{header_final}\n\n{panel_body}"


# ─────────────────────────────────────────────────────────────────────────
# Seedance prompt tabs — persistent state of versions (2026-05-18)
# ─────────────────────────────────────────────────────────────────────────
# Каждый блок монтажной карты имеет один или несколько Seedance промптов
# (оригинал + альтернативные версии через regen/compress). Чтобы не
# терять версии при закрытии попапа — хранение на диске:
#   <seedance_dir>/<ep>_block_<N>.txt           — Вкладка 1 (оригинал)
#   <seedance_dir>/<ep>_block_<N>_tab<K>.txt    — последующие вкладки
#   <seedance_dir>/<ep>_block_<N>_tabs.json     — порядок + active_idx +
#                                                  monotonic next_idx
#
# JSON формат:
#   {
#     "version": 1,
#     "next_idx": 4,                  ← monotonic, только растёт
#     "active_idx": 1,
#     "tabs": [
#       {"title": "Вкладка 1", "file": "<ep>_block_<N>.txt",      "source": "original"},
#       {"title": "Вкладка 2", "file": "<ep>_block_<N>_tab2.txt", "source": "regen"},
#       {"title": "Вкладка 3", "file": "<ep>_block_<N>_tab3.txt", "source": "compress"}
#     ]
#   }
#
# Helper'ы тестируемы в unit-режиме без QtWidgets (только pathlib/json).
# ─────────────────────────────────────────────────────────────────────────


def _seedance_tabs_path(seedance_dir: Path, ep_id: str, block_n: int) -> Path:
    """Путь к JSON-файлу с состоянием вкладок Seedance промпта блока."""
    return Path(seedance_dir) / f"{ep_id}_block_{block_n}_tabs.json"


def _next_tab_idx(seedance_dir: Path, ep_id: str, block_n: int,
                    hint_from_json: int = 2) -> int:
    """Monotonic next_idx для нового `_tab<K>.txt`.

    Возвращает `max(hint_from_json, max(существующих _tab<K>.txt) + 1)`.
    Защита от collisions если кто-то положил `_tab<K>.txt` руками минуя
    JSON. Минимум — 2 (так как вкладка 1 это `_block_<N>.txt` без суффикса).
    """
    max_existing = 1
    base = f"{ep_id}_block_{block_n}_tab"
    try:
        for p in Path(seedance_dir).glob(f"{base}*.txt"):
            stem = p.stem
            if not stem.startswith(base):
                continue
            tail = stem[len(base):]
            if tail.isdigit():
                n = int(tail)
                if n > max_existing:
                    max_existing = n
    except Exception:
        pass
    return max(hint_from_json, max_existing + 1)


def _save_seedance_tabs(tabs_path: Path, tabs_state: list, active_idx: int,
                          next_idx: int) -> None:
    """Атомарно сохраняет JSON с состоянием вкладок.

    Использует temp-file + os.replace для атомарности (паттерн как у
    `_save_active_gen_decision` — атомарный rename POSIX-style).
    Silent fail в stderr если диск переполнен / нет прав.
    """
    payload = {
        "version": 1,
        "next_idx": int(next_idx),
        "active_idx": int(active_idx),
        "tabs": list(tabs_state),
    }
    try:
        tabs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tabs_path.with_suffix(
            tabs_path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(str(tmp), str(tabs_path))
    except Exception as e:
        try:
            sys.stderr.write(
                f"[_save_seedance_tabs] WARN: {type(e).__name__}: {e}\n")
        except Exception:
            pass


def _load_seedance_tabs(tabs_path: Path, seedance_dir: Path,
                          ep_id: str, block_n: int,
                          fallback_text: Optional[str] = None
                          ) -> tuple:
    """Загружает state вкладок Seedance промпта блока.

    Возвращает `(tabs_state, active_idx, next_idx, save_needed)`:
      • tabs_state — list[dict] с полями `title`, `file`, `source`,
                     `text` (тело прочитано с диска)
      • active_idx — индекс активной вкладки (clamped в bounds)
      • next_idx   — monotonic счётчик для следующей новой вкладки
      • save_needed — True если состояние нужно сразу записать обратно
                      (либо создан дефолт, либо отфильтрованы stale entries)

    Логика:
      1. Если JSON отсутствует: создаём дефолт с одной вкладкой
         «Вкладка 1» → `<ep>_block_<N>.txt`. Если fallback_text задан
         и .txt отсутствует — пишем fallback_text в .txt тоже.
      2. Если JSON есть: читаем, для каждой вкладки читаем file.txt.
         Stale entries (файл из JSON отсутствует на диске) пропускаются
         + save_needed=True (auto-clean).
      3. Битый JSON → log + создаём дефолт.
    """
    orig_file = f"{ep_id}_block_{block_n}.txt"
    orig_path = Path(seedance_dir) / orig_file

    def _make_default() -> tuple:
        # Дефолт: одна вкладка «Вкладка 1» → оригинал.
        text = fallback_text
        # `not text` покрывает и None, и пустую строку (если caller передал
        # "" — пробуем прочитать с диска вместо показа пустой вкладки).
        if not text:
            try:
                if orig_path.exists():
                    text = orig_path.read_text(encoding="utf-8")
            except Exception:
                text = None
        tab = {
            "title": "Вкладка 1",
            "file": orig_file,
            "source": "original",
            "text": text or "",
        }
        nxt = _next_tab_idx(seedance_dir, ep_id, block_n, hint_from_json=2)
        return [tab], 0, nxt, True

    if not Path(tabs_path).exists():
        return _make_default()

    try:
        raw = json.loads(Path(tabs_path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("invalid version or schema")
        tabs_in = raw.get("tabs") or []
        if not isinstance(tabs_in, list) or not tabs_in:
            raise ValueError("empty tabs list")
    except Exception as e:
        try:
            sys.stderr.write(
                f"[_load_seedance_tabs] WARN: broken JSON {tabs_path.name}: "
                f"{type(e).__name__}: {e}, falling back to default\n")
        except Exception:
            pass
        return _make_default()

    # Auto-clean stale entries
    tabs_state: list = []
    save_needed = False
    for entry in tabs_in:
        if not isinstance(entry, dict):
            save_needed = True
            continue
        fname = entry.get("file")
        if not isinstance(fname, str):
            save_needed = True
            continue
        fpath = Path(seedance_dir) / fname
        if not fpath.exists():
            save_needed = True
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception:
            save_needed = True
            continue
        tabs_state.append({
            "title": entry.get("title") or f"Вкладка {len(tabs_state) + 1}",
            "file": fname,
            "source": entry.get("source") or "unknown",
            "text": text,
        })

    if not tabs_state:
        return _make_default()

    # active_idx — clamp в bounds
    try:
        active_idx = int(raw.get("active_idx", 0))
    except (TypeError, ValueError):
        active_idx = 0
    if active_idx < 0 or active_idx >= len(tabs_state):
        active_idx = 0
        save_needed = True

    # next_idx — monotonic + защита от collisions
    try:
        json_next = int(raw.get("next_idx", 2))
    except (TypeError, ValueError):
        json_next = 2
    nxt = _next_tab_idx(seedance_dir, ep_id, block_n, hint_from_json=json_next)
    if nxt != json_next:
        save_needed = True

    return tabs_state, active_idx, nxt, save_needed


def _extract_panel_body(prompt_text: str, panel_idx: int) -> Optional[str]:
    """Извлекает «сырое» тело Panel N из .txt промпта блока — то что
    идёт после «Panel N (Position):» до следующей `Panel \\d+ (` или
    `===ПРОМПТ_БЛОК...КОНЕЦ===`. БЕЗ хедера блока. Используется в
    `_on_edit_shot` для pre-fill попапа правки.

    Защита от LLM self-correction артефактов (v1.0.70+):
    PromptWriter Opus иногда пишет в финальный output строку вида
    «Panel 2 (Far Left—wait correcting): see next.» рядом с настоящей
    «Panel 2 (Middle Left): <реальное тело>». Парсер по re.search брал
    первое совпадение → пользователь видел мусор. Кейс: ep16_block_5
    финальный_расчет_2 (см. _session_log).

    Алгоритм при множественных совпадениях:
      1. Собрать все matches для `Panel N (...)`.
      2. Отфильтровать совпадения с markers self-correction:
         «wait correcting», «see next», «ignore above», «actually,»
         + любое body длиной <20 символов начинающееся с «see ».
      3. Из оставшихся выбрать совпадение с самым длинным body
         (мусор обычно короткий, реальная панель — длинная).
      4. Fallback если все отфильтрованы: вернуть последнее
         совпадение (LLM обычно «исправляется» в сторону правильного).
      5. Logging в stderr с количеством отфильтрованных.
    """
    target = panel_idx + 1
    pat = re.compile(
        rf'Panel\s+{target}\s+\([^)]+\):\s*(.*?)(?=Panel\s+\d+\s+\(|===ПРОМПТ_БЛОК.*?КОНЕЦ|\Z)',
        re.DOTALL,
    )
    matches = list(pat.finditer(prompt_text))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0].group(1).strip()

    # Множественные matches — это LLM-артефакт. Чистим.
    ARTIFACT_MARKERS = (
        "wait, correcting", "wait correcting",
        "see next", "ignore above", "actually,",
    )
    bodies = [m.group(1).strip() for m in matches]

    def _is_artifact(b: str) -> bool:
        bl = b.lower()
        if len(b) < 20 and bl.startswith("see "):
            return True
        for marker in ARTIFACT_MARKERS:
            if marker in bl:
                return True
        return False

    real = [b for b in bodies if not _is_artifact(b)]
    if real:
        picked = max(real, key=len)
        filtered_count = len(bodies) - len(real)
    else:
        # Все matches помечены как артефакт — fallback на последнее.
        picked = bodies[-1]
        filtered_count = len(bodies)
    try:
        sys.stderr.write(
            f"[_extract_panel_body] WARN: Panel {target} has "
            f"{len(matches)} matches, filtered {filtered_count} as "
            f"self-correction artifacts, picked body_len={len(picked)}\n")
    except Exception:
        pass
    return picked


def _replace_panel_body(prompt_text: str, panel_idx: int, new_body: str) -> str:
    """Заменяет тело Panel N в .txt промпта блока на `new_body`.
    Шапка и остальные панели остаются нетронутыми. Возвращает обновлённый
    полный текст файла."""
    target = panel_idx + 1
    pat = re.compile(
        rf'(Panel\s+{target}\s+\([^)]+\):\s*)(.*?)(?=Panel\s+\d+\s+\(|===ПРОМПТ_БЛОК.*?КОНЕЦ|\Z)',
        re.DOTALL,
    )
    def _repl(m):
        return m.group(1) + new_body.strip() + '\n\n'
    return pat.sub(_repl, prompt_text, count=1)


def extract_shot_tags(prompt_text: str, panel_idx: int) -> set:
    """Возвращает множество [@]imgN тегов реально упомянутых в теле Panel N+1.

    Используется для «умной» регенерации одного шота: при перегенерации
    отправляются ТОЛЬКО те референсы что реально упомянуты в данной панели,
    а не все рефы блока (как было раньше). Это:
      • точнее по архитектуре (инструкция: см. nano banana 6.0 — теги только
        для тех персонажей/объектов которые реально нужны в шоте);
      • экономит трафик и cost API;
      • обходит лимит «many-image requests 2000px» когда «толстый» реф
        персонажа реально не присутствует в этом конкретном шоте.

    Возвращает множество строк вида `"[@]img1"`, `"[@]img2"`, ...
    Если Panel пустой/blank/не найден — возвращает пустое множество.
    Шапка блока (CHARACTERS секция, [[@]img1 - …, [@]img2 - …] список)
    в анализ НЕ включается — только тело конкретной панели.
    """
    target = panel_idx + 1

    cleaned = "\n".join(
        l for l in prompt_text.splitlines()
        if not l.startswith("# [@]") and not l.startswith("===ПРОМПТ_БЛОК")
    ).strip()

    panel_pat = re.compile(
        r'(?is)Panel\s+(\d+)\s+\([^)]+\):\s*(.*?)(?=Panel\s+\d+\s+\(|$)'
    )
    for m in panel_pat.finditer(cleaned):
        if int(m.group(1)) == target:
            body = m.group(2).strip()
            if "COMPLETELY BLANK" in body.upper():
                return set()
            return set(re.findall(r'\[@\]img\d+', body))
    return set()


# ─── Утилиты — изображения ───────────────────────────────────────────────────

def shot_path(block_name: str, shot_idx: int) -> Path:
    """Путь к отдельному файлу шота: {block}_shot{N}.jpg (N с 1)."""
    return STORYBOARDS_DIR / f"{block_name}_shot{shot_idx + 1}.jpg"


# 2026-05-07 — История версий шотов.
# Каждая регенерация шота копит предыдущие версии в `_history/<basename>/v1.jpg,
# v2.jpg, …`. ShotViewerDialog показывает все версии — юзер может вернуться
# к любой через «✓ Использовать эту». Активная версия — копия одного из
# `vN.jpg` в основном `output/storyboards/<basename>.jpg` (loader'ы
# не меняются). Указатель «какая активна» хранится в `_history/<basename>/active.txt`.

def shot_history_dir(block_name: str, shot_idx: int) -> Path:
    """Каталог истории версий шота: STORYBOARDS_DIR/_history/{block}_shot{N}/."""
    basename = f"{block_name}_shot{shot_idx + 1}"
    return STORYBOARDS_DIR / "_history" / basename


def list_shot_versions(history_dir: Path) -> list:
    """Возвращает отсортированный по N список путей `vN.jpg` в history_dir.
    Пустой список если каталога нет или версий нет."""
    if not history_dir.exists() or not history_dir.is_dir():
        return []
    out = []
    for p in history_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        # vN.jpg / vN.jpeg / vN.png
        if name.startswith("v") and "." in name:
            stem = name.split(".", 1)[0]
            try:
                n = int(stem[1:])
                out.append((n, p))
            except ValueError:
                continue
    out.sort(key=lambda x: x[0])
    return [p for _, p in out]


def next_history_index(history_dir: Path) -> int:
    """Следующий свободный N для нового vN.jpg. Если history пуста → 1."""
    versions = list_shot_versions(history_dir)
    if not versions:
        return 1
    # Берём максимальный N + 1.
    max_n = 0
    for p in versions:
        try:
            n = int(p.stem[1:])
            if n > max_n:
                max_n = n
        except (ValueError, IndexError):
            continue
    return max_n + 1


def build_shot_version_tree(history_dir: Path) -> list:
    """2026-06-13 (дерево версий, Слой 2.1): ЧИСТЫЙ читатель структуры дерева
    версий шота. Файлы на диске остаются ПЛОСКИМИ (v1..vN, целые номера) —
    дерево это НАКЛЕЙКА для UI поверх sidecar'ов parent_v{N}.json. Ничего не
    пишет, UI не трогает, никто пока не вызывает (Слой 2.1 — только читатель).

    Возвращает список узлов в DISPLAY-порядке (DFS: корни по возрастанию n,
    потомок сразу за родителем перед следующим братом родителя, дети по
    возрастанию n). Каждый узел:
        {
          "n": int,            # плоский номер файла v{n}.jpg (как на диске)
          "parent": int|None,  # номер родителя, None у корня
          "depth": int,        # 0 корень, 1 ребёнок, 2 внук, ...
          "dotted": str,       # 'v5' (корень=плоский номер), 'v5.1' (1-й
                               #  ребёнок), 'v5.1.1' (внук). Буква v везде.
          "has_children": bool,
          "children": list,    # номера прямых детей по возрастанию
        }
    Устойчив к мусору (НЕ падает): отсутствующий/битый parent_v → корень;
    parent=0 / родителя нет в версиях / сам на себя / цикл → версия = корень
    (сирот по правилам не будет, но защищаемся). Дубли номера (Mode C
    .prompt.txt в list_shot_versions) схлопываются — берём уникальные.
    Формат един для 9:16 и 16:9 (history_dir общая)."""
    history_dir = Path(history_dir)
    # 1) уникальные плоские номера версий (дубли от .prompt.txt схлопываем)
    nums = []
    seen_n = set()
    for p in list_shot_versions(history_dir):
        try:
            k = int(p.stem[1:])
        except (ValueError, IndexError):
            continue
        if k not in seen_n:
            seen_n.add(k)
            nums.append(k)
    if not nums:
        return []
    nums.sort()
    numset = set(nums)

    # 2) child -> parent по parent_v{N}.json (любой мусор → None = корень)
    import json as _json
    parent_of = {}
    for k in nums:
        par = None
        f = history_dir / f"parent_v{k}.json"
        if f.exists():
            try:
                data = _json.loads(f.read_text(encoding='utf-8')) or {}
                pv = int(data.get("parent", 0) or 0)
                if pv > 0 and pv in numset and pv != k:
                    par = pv
            except Exception:
                par = None
        parent_of[k] = par

    # 3) children map (по возрастанию)
    children = {k: [] for k in nums}
    for k in nums:
        par = parent_of[k]
        if par is not None:
            children[par].append(k)
    for k in nums:
        children[k].sort()

    # 4) DFS из корней; visited страхует от циклов
    out = []
    visited = set()

    def _walk(k, depth, dotted):
        if k in visited:
            return
        visited.add(k)
        kids = children[k]
        out.append({
            "n": k,
            "parent": parent_of[k],
            "depth": depth,
            "dotted": dotted,
            "has_children": bool(kids),
            "children": list(kids),
        })
        for idx, child in enumerate(kids, start=1):
            _walk(child, depth + 1, f"{dotted}.{idx}")

    for r in [k for k in nums if parent_of[k] is None]:
        _walk(r, 0, f"v{r}")
    # 5) защита от циклов: непосещённые звенья → как корни, по возрастанию
    for k in nums:
        if k not in visited:
            _walk(k, 0, f"v{k}")
    return out


def read_active_version(history_dir: Path) -> int:
    """Читает `_history/<basename>/active.txt` → N (int). 0 если файла
    нет или содержимое некорректно. Юзер: 0 = «активная версия не
    отслеживается», UI берёт самую свежую (max N) или текущий
    основной файл."""
    f = history_dir / "active.txt"
    if not f.exists():
        return 0
    try:
        text = f.read_text(encoding='utf-8').strip()
        return int(text)
    except Exception:
        return 0


def set_active_version(history_dir: Path, n: int) -> None:
    """Пишет `_history/<basename>/active.txt` = N."""
    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        (history_dir / "active.txt").write_text(str(int(n)), encoding='utf-8')
    except Exception:
        pass


def add_shot_version_from_bytes(block_name: str, shot_idx: int,
                                image_bytes: bytes) -> int:
    """2026-06-02: добавляет картинку (bytes) НОВОЙ версией в историю шота и
    делает её активной. Используется кнопкой «Вставить» (копирование шотов
    между блоками). Возвращает номер новой версии (vN).

    Логика — копия save-паттерна из GenerateThread (threads/generate.py):
      • если history пуста, а активный файл шота есть — мигрируем его в v1
        (чтобы не потерять текущую активную версию);
      • пишем картинку в новый vN.jpg (ОРИГИНАЛЬНЫЙ размер ~768×1376, q90);
      • копируем vN.jpg в активный файл шота ({block}_shot{N}.jpg);
      • помечаем active.txt = N.
    Старые версии не трогаются.

    Cross-platform: только pathlib.Path + PIL + shutil + io.BytesIO,
    без subprocess/shell. GenerateThread НЕ трогается (осознанное
    дублирование, чтобы не рефакторить рабочий генератор)."""
    history_dir = shot_history_dir(block_name, shot_idx)
    history_dir.mkdir(parents=True, exist_ok=True)
    shot_file = shot_path(block_name, shot_idx)
    if not list_shot_versions(history_dir) and shot_file.exists():
        try:
            shutil.copy2(str(shot_file), str(history_dir / "v1.jpg"))
        except Exception:
            pass  # миграция фейл — продолжаем
    next_n = next_history_index(history_dir)
    new_version_path = history_dir / f"v{next_n}.jpg"
    with PILImage.open(io.BytesIO(image_bytes)) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img.save(str(new_version_path), 'JPEG', quality=90, optimize=True)
    shutil.copy2(str(new_version_path), str(shot_file))
    set_active_version(history_dir, next_n)
    return next_n


# 2026-06-04 (C2): ОБРАТИМЫЙ кроп версии шота. Видимый v{N}.jpg = оригинал +
# применённый кроп (уходит в лист/Seedance/zip). Рядом: ЧИСТЫЙ оригинал
# orig_v{N}.jpg (снимок до первого кропа, неизменен) + параметры
# crop_v{N}.json (scene_rect в пикселях оригинала). Префиксы orig_/crop_ НЕ
# начинаются с "v"+цифра → list_shot_versions/_has_any_versions/лента/stitch/
# Seedance/zip их НЕ видят. Кроп ВСЕГДА считается от оригинала → полностью
# обратим, без потери качества при повторном редактировании.
# Cross-platform: pathlib.Path + PIL + shutil + json, без subprocess/shell.
CROP_JSON_SCHEMA = 1


def shot_orig_path(history_dir: Path, n: int) -> Path:
    """Чистый оригинал версии v{N} (снимок до кропа). list_shot_versions его
    игнорит (имя не на 'v'+цифра)."""
    return Path(history_dir) / f"orig_v{int(n)}.jpg"


def shot_crop_json_path(history_dir: Path, n: int) -> Path:
    """Параметры кропа версии v{N} (scene_rect + размеры оригинала)."""
    return Path(history_dir) / f"crop_v{int(n)}.json"


def read_shot_crop(history_dir: Path, n: int) -> Optional[dict]:
    """crop_v{N}.json → dict {schema, scene_rect|None, mirror, img_w/h} или None.
    None если нет json/оригинала или json битый. scene_rect валидируется ЕСЛИ
    присутствует (может быть null при mirror-only). mirror нормализуется."""
    jp = shot_crop_json_path(history_dir, n)
    op = shot_orig_path(history_dir, n)
    if not jp.exists() or not op.exists():
        return None
    try:
        data = json.loads(jp.read_text(encoding='utf-8'))
        r = data.get('scene_rect')
        if r is not None:
            for k in ('x', 'y', 'w', 'h'):
                float(r[k])
        data['mirror'] = bool(data.get('mirror', False))
        return data
    except Exception:
        return None


def _render_shot_version(opath: Path, vpath: Path, mirror: bool,
                         scene_rect) -> Optional[tuple]:
    """Ядро сборки видимого v{N}: orig → [flip-horizontal если mirror] →
    [crop по scene_rect → resize к размеру оригинала если есть]. Сохраняет в
    vpath JPEG q95. Возвращает (W,H) или None (вырожденный кроп <2px).
    Для mirror=False С scene_rect пайплайн ИДЕНТИЧЕН прежнему apply_shot_crop
    (байт-в-байт). Cross-platform: PIL, без subprocess/shell."""
    from PIL import ImageOps
    with PILImage.open(str(opath)) as im:
        W, H = im.size
        base = ImageOps.mirror(im) if mirror else im
        if scene_rect is not None:
            x, y = float(scene_rect['x']), float(scene_rect['y'])
            w, h = float(scene_rect['w']), float(scene_rect['h'])
            left = max(0, min(W, int(round(x))))
            upper = max(0, min(H, int(round(y))))
            right = max(0, min(W, int(round(x + w))))
            lower = max(0, min(H, int(round(y + h))))
            if right - left < 2 or lower - upper < 2:
                return None
            out = base.crop((left, upper, right, lower))
            if out.mode != 'RGB':
                out = out.convert('RGB')
            out = out.resize((W, H), PILImage.Resampling.LANCZOS)
        else:
            out = base if base.mode == 'RGB' else base.convert('RGB')
        out.save(str(vpath), 'JPEG', quality=95)
        return (W, H)


def apply_shot_crop(history_dir: Path, n: int, scene_rect: dict,
                    active_path: Path, img_w: int = 0, img_h: int = 0) -> bool:
    """Применяет кроп к версии v{N}. При ПЕРВОМ кропе снимает оригинал
    (v{N} → orig_v{N}); затем v{N} = orig → [flip если mirror из json] →
    crop(scene_rect) → resize; пишет crop_v{N}.json (сохраняя mirror); копирует
    v{N} → active_path + set_active_version(N). scene_rect = {x,y,w,h} в
    пикселях ТЕКУЩЕЙ базы (orig или flip(orig)). True при успехе. Вырожденный
    кроп (<2px) → False, файл не трогаем."""
    history_dir = Path(history_dir)
    n = int(n)
    vpath = history_dir / f"v{n}.jpg"
    opath = shot_orig_path(history_dir, n)
    if not vpath.exists() and not opath.exists():
        return False
    try:
        if not opath.exists():
            shutil.copy2(str(vpath), str(opath))  # снимок оригинала ОДИН раз
        mirror = bool((read_shot_crop(history_dir, n) or {}).get('mirror', False))
        res = _render_shot_version(opath, vpath, mirror, scene_rect)
        if res is None:
            return False
        W, H = res
        x, y = float(scene_rect['x']), float(scene_rect['y'])
        w, h = float(scene_rect['w']), float(scene_rect['h'])
        data = {
            'schema': CROP_JSON_SCHEMA,
            'scene_rect': {'x': x, 'y': y, 'w': w, 'h': h},
            'mirror': mirror,
            'img_w': int(img_w or W), 'img_h': int(img_h or H),
        }
        shot_crop_json_path(history_dir, n).write_text(
            json.dumps(data), encoding='utf-8')
        shutil.copy2(str(vpath), str(Path(active_path)))
        set_active_version(history_dir, n)
        return True
    except Exception:
        return False


def clear_shot_crop(history_dir: Path, n: int, active_path: Path) -> bool:
    """Сброс кропа v{N}: восстанавливает v{N} из orig_v{N}, удаляет
    orig_v{N}.jpg + crop_v{N}.json, копирует v{N} → active_path + active.txt.
    No-op (False) если оригинала нет (кроп и так не применён)."""
    history_dir = Path(history_dir)
    n = int(n)
    opath = shot_orig_path(history_dir, n)
    if not opath.exists():
        return False
    try:
        vpath = history_dir / f"v{n}.jpg"
        shutil.copy2(str(opath), str(vpath))
        try:
            opath.unlink()
        except Exception:
            pass
        jp = shot_crop_json_path(history_dir, n)
        if jp.exists():
            try:
                jp.unlink()
            except Exception:
                pass
        shutil.copy2(str(vpath), str(Path(active_path)))
        set_active_version(history_dir, n)
        return True
    except Exception:
        return False


def set_shot_mirror(history_dir: Path, n: int, mirror: bool,
                    active_path: Path) -> bool:
    """Тогл горизонтального зеркала версии v{N}. Упрощение (Alex): зеркало
    СБРАСЫВАЕТ кроп. Снимок оригинала один раз. mirror=False и нет кропа →
    clear (pristine). mirror=True → рендер flip(orig) (полный кадр) + json
    {scene_rect:null, mirror:true}. True при успехе."""
    history_dir = Path(history_dir)
    n = int(n)
    vpath = history_dir / f"v{n}.jpg"
    opath = shot_orig_path(history_dir, n)
    mirror = bool(mirror)
    if not vpath.exists() and not opath.exists():
        return False
    try:
        if not mirror:
            # выключили зеркало; кроп тоже сброшен (упрощение) → версия чистая
            if opath.exists():
                return clear_shot_crop(history_dir, n, active_path)
            return True
        if not opath.exists():
            shutil.copy2(str(vpath), str(opath))
        res = _render_shot_version(opath, vpath, True, None)  # flip, полный кадр
        if res is None:
            return False
        W, H = res
        data = {'schema': CROP_JSON_SCHEMA, 'scene_rect': None,
                'mirror': True, 'img_w': W, 'img_h': H}
        shot_crop_json_path(history_dir, n).write_text(
            json.dumps(data), encoding='utf-8')
        shutil.copy2(str(vpath), str(Path(active_path)))
        set_active_version(history_dir, n)
        return True
    except Exception:
        return False


def delete_shot_version(history_dir: Path, n: int, active_path: Path) -> int:
    """Удаляет ЛИСТ-версию v{N} БЕЗВОЗВРАТНО. Слой 2.3b «дерево версий»,
    ВАРИАНТ B: номера НЕ перенумеровываются — после удаления остаётся ДЫРА
    (было v3,v4,v5 → удалили v4 → стало v3,v5). Это сохраняет указатели
    parent_v{M}.json валидными (плоские номера не сдвигаются), а лента
    (build_shot_version_tree) устойчива к дыркам и пересчитывает dotted-подписи
    по живым братьям. Возвращает НЕИЗМЕНЁННЫЙ номер активной версии (>=1) на
    успех, или -1 при отказе/ошибке.

    Удаляются ВСЕ файлы версии N: v{N}.jpg + orig_v{N}.jpg + crop_v{N}.json +
    parent_v{N}.json (родство удаляемой) + v{N}.prompt.txt (Mode C-диагностика) —
    каждый через if exists(), чтобы не осталось сирот.

    Гарды (дублируют скрытие крестика в UI): n нет в списке / n == min(«v1») /
    n == активная → -1, диск НЕ трогаем. Лист-инвариант (нет детей) обеспечен
    UI: крестик скрыт на версии с детьми → у N нет потомков с parent==N, сирот
    не возникает. active.txt и живой файл шота НЕ трогаются (удаляем всегда
    НЕ активную; номера не сдвигаются → активная и её файл остаются прежними).

    Частичный сбой → log в stderr + -1 (без rollback — локальная история;
    caller делает refresh). Cross-platform: pathlib.Path.unlink, без
    subprocess/shell/os.replace."""
    history_dir = Path(history_dir)
    n = int(n)
    nums = []
    for p in list_shot_versions(history_dir):
        try:
            nums.append(int(p.stem[1:]))
        except (ValueError, IndexError):
            continue
    if not nums or n not in nums:
        return -1
    active = read_active_version(history_dir)
    if n == min(nums) or n == active:
        return -1  # «v1» (минимальный) и активную удалять нельзя

    try:
        # Удаляем ВСЕ файлы листа N (вариант B: без сдвига номеров). active.txt
        # и живой файл шота не трогаем — удаляемая версия не активна.
        for f in (history_dir / f"v{n}.jpg",
                  shot_orig_path(history_dir, n),            # orig_v{n}.jpg
                  shot_crop_json_path(history_dir, n),       # crop_v{n}.json
                  history_dir / f"parent_v{n}.json",         # родство удаляемой
                  history_dir / f"v{n}.prompt.txt"):         # Mode C-диагностика
            if f.exists():
                f.unlink()
        return active  # номер активной не изменился (>=1)
    except Exception:
        import traceback
        sys.stderr.write(
            f"[del_version] failed n={n}: {traceback.format_exc()}\n")
        sys.stderr.flush()
        return -1


def format_gen_duration(seconds: int) -> str:
    """Форматирует длительность генерации в человекочитаемый вид.

    < 60s     → "42с"
    60-3599s  → "1м 5с"
    >= 3600s  → "1ч 5м"
    """
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}с"
    minutes, secs = divmod(s, 60)
    if minutes < 60:
        return f"{minutes}м {secs}с"
    hours, mins = divmod(minutes, 60)
    return f"{hours}ч {mins}м"


def is_block_complete(block_name: str) -> bool:
    """True если ВСЕ non-blank шоты блока имеют сгенерированные файлы.

    Считаем шот «нужным» если в промпт-файле его панель не помечена как
    COMPLETELY BLANK. Если все нужные шоты есть на диске — блок завершён.
    Если промпта нет — определить нельзя, возвращаем False.
    """
    prompt_file = PROMPTS_DIR / f"{block_name}.txt"
    if not prompt_file.exists():
        return False
    try:
        text = prompt_file.read_text(encoding="utf-8")
    except Exception:
        return False
    shots = parse_shots(text)
    needed_indices = [i for i, s in enumerate(shots) if not s.get("is_blank")]
    if not needed_indices:
        return False
    for i in needed_indices:
        if not shot_path(block_name, i).exists():
            return False
    return True


def _find_overlay_font(size: int):
    """Находит TTF-шрифт для PIL.ImageDraw в зависимости от ОС.
    Fallback на default-шрифт PIL если ничего не найдено."""
    from PIL import ImageFont
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    elif sys.platform == "win32":
        candidates = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def stitch_shots_to_landscape(block_name: str, dest: Path, aspect: str = "9:16") -> None:
    """Склеивает шоты блока в один лист 16:9 и сохраняет.

    aspect="9:16" (дефолт) — 4 вертикальных кадра В РЯД (как было).
    aspect="16:9" — 4 горизонтальных кадра сеткой 2×2.

    Пустые позиции (где нет файла) заполняются белым.
    На каждом непустом шоте СИСТЕМНО накладывает подпись «SHOT N · Xs · описание»
    в нижнем-левом углу с белой плашкой — чтобы Nano Banana не пришлось рисовать
    эти подписи на самой картинке (нестабильно, артефакты).
    """
    from PIL import ImageDraw

    paths: List[Optional[Path]] = []
    for i in range(PANELS):
        p = shot_path(block_name, i)
        paths.append(p if p.exists() else None)

    panel_w, panel_h = 0, 0
    for p in paths:
        if p is not None:
            with PILImage.open(p) as img:
                panel_w, panel_h = img.size
            break

    if panel_w == 0 or panel_h == 0:
        return

    # Этап 5 (формат кадра): раскладка листа. 9:16 → 4 панели В РЯД (cols=4,
    # rows=1) — РОВНО как было: canvas = panel_w*4 × panel_h, ячейка i = (i*panel_w,0).
    # 16:9 → сетка 2×2 (cols=2, rows=2). Позицию ячейки панели i даёт _cell(i).
    if aspect == "16:9":
        cols, rows = 2, 2
    else:
        cols, rows = PANELS, 1
    canvas = PILImage.new("RGB", (panel_w * cols, panel_h * rows), (255, 255, 255))

    def _cell(i):
        return ((i % cols) * panel_w, (i // cols) * panel_h)

    for i, p in enumerate(paths):
        if p is None:
            continue
        with PILImage.open(p) as img:
            piece = img.convert("RGB")
            if piece.size != (panel_w, panel_h):
                if piece.height != panel_h:
                    new_w = max(1, int(piece.width * panel_h / piece.height))
                    piece = piece.resize((new_w, panel_h), PILImage.LANCZOS)
                if piece.width > panel_w:
                    x0 = (piece.width - panel_w) // 2
                    piece = piece.crop((x0, 0, x0 + panel_w, panel_h))
                elif piece.width < panel_w:
                    pad = PILImage.new("RGB", (panel_w, panel_h), (255, 255, 255))
                    pad.paste(piece, ((panel_w - piece.width) // 2, 0))
                    piece = pad
        _ox, _oy = _cell(i)
        canvas.paste(piece, (_ox, _oy))

    # Системное наложение подписей шотов поверх картинки.
    # Берём метаданные из промпт-файла блока через parse_shots.
    shots: List[Dict] = []
    pf = PROMPTS_DIR / f"{block_name}.txt"
    if pf.exists():
        try:
            shots = parse_shots(pf.read_text(encoding="utf-8"))
        except Exception:
            pass

    if shots:
        draw = ImageDraw.Draw(canvas)
        # Размер шрифта — пропорционально высоте панели
        font_size = max(20, int(panel_h * 0.026))
        font = _find_overlay_font(font_size)
        margin = max(12, int(panel_h * 0.018))
        pad_x  = max(10, int(font_size * 0.5))
        pad_y  = max(8,  int(font_size * 0.35))

        for i in range(PANELS):
            if i >= len(shots) or paths[i] is None:
                continue
            shot = shots[i]
            if shot.get('is_blank'):
                continue
            parts = [f"SHOT {shot.get('shot_num', i+1)}"]
            dur = shot.get('duration', '').strip()
            if dur:
                parts.append(dur)
            desc = shot.get('description', '').strip()
            if desc:
                parts.append(desc)
            text = "  ·  ".join(parts)

            x_offset, y_base = _cell(i)
            # Размер текста
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except Exception:
                text_w, text_h = len(text) * font_size // 2, font_size

            # Если текст шире панели — обрезаем добавляя «…»
            max_text_w = panel_w - 2 * margin - 2 * pad_x
            if text_w > max_text_w:
                # Простое сокращение по символам
                while text and text_w > max_text_w and len(text) > 3:
                    text = text[:-2]
                    try:
                        bbox = draw.textbbox((0, 0), text + "…", font=font)
                        text_w = bbox[2] - bbox[0]
                    except Exception:
                        text_w = len(text) * font_size // 2
                text = text + "…"

            # Координаты белой плашки в нижнем-левом углу панели
            box_x0 = x_offset + margin
            box_y1 = y_base + panel_h - margin
            box_y0 = box_y1 - text_h - 2 * pad_y
            box_x1 = box_x0 + text_w + 2 * pad_x

            # Тень под плашкой для контраста
            shadow_offset = 2
            draw.rectangle(
                [box_x0 + shadow_offset, box_y0 + shadow_offset,
                 box_x1 + shadow_offset, box_y1 + shadow_offset],
                fill=(0, 0, 0))
            # Белая плашка
            draw.rectangle([box_x0, box_y0, box_x1, box_y1], fill=(255, 255, 255))
            # Чёрная рамка
            draw.rectangle([box_x0, box_y0, box_x1, box_y1],
                           outline=(0, 0, 0), width=1)
            # Текст
            draw.text((box_x0 + pad_x, box_y0 + pad_y), text,
                      fill=(0, 0, 0), font=font)

    fmt = "PNG" if dest.suffix.lower() == ".png" else "JPEG"
    if fmt == "JPEG":
        canvas.save(dest, format=fmt, quality=95)
    else:
        canvas.save(dest, format=fmt)
# ─── Поток генерации ─────────────────────────────────────────────────────────
# class GenerateThread / class RefGenerateThread — вытащены в threads/generate.py
# 2026-05-04. Импортированы вверху файла из `threads`.

# class OverlayActionBtn — вытащен в widgets/editor_widgets.py
# 2026-05-04 (шаг 5A). Импортирован вверху файла из `widgets`.


# ─── Claude Code CLI: автообновление geometry в фоне ─────────────────────────

_claude_cli_cache: Optional[str] = None  # кешируем найденный путь

def find_claude_cli() -> Optional[str]:
    """Ищет бинарник Claude Code CLI на машине пользователя.

    Порядок поиска (кросс-платформенно — Mac и Windows 10/11):
      1. shutil.which('claude') — стандартный поиск через PATH (на Win
         учитывает PATHEXT, .exe/.cmd подхватывается автоматически).
      2. Стандартные пути native installer'а:
         • Mac:  ~/.local/bin/claude, /opt/homebrew/bin/claude,
                 /usr/local/bin/claude
         • Win:  %USERPROFILE%\.local\bin\claude.exe,
                 %USERPROFILE%\.local\bin\claude,
                 %LOCALAPPDATA%\Anthropic\claude.exe
                 (соответствует тому что ставит installer_app.py через
                 https://claude.ai/install.ps1)

    Возвращает абсолютный путь к бинарнику или None если не найден.
    Результат кешируется до перезапуска (один раз на сессию).

    Зачем абсолютный путь, а не просто 'claude': когда Studio.app
    запускается из Finder/Explorer, у него урезанный PATH, и subprocess
    не найдёт CLI даже если он работает из Terminal/CMD.
    """
    global _claude_cli_cache
    if _claude_cli_cache is not None:
        return _claude_cli_cache or None
    candidates = []
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    home = Path.home()
    if sys.platform == 'win32':
        # Windows 10/11. Установщик кладёт в %USERPROFILE%\.local\bin\
        # (через https://claude.ai/install.ps1).
        local_appdata = os.environ.get(
            'LOCALAPPDATA',
            str(home / "AppData" / "Local")
        )
        candidates.extend([
            str(home / ".local" / "bin" / "claude.exe"),
            str(home / ".local" / "bin" / "claude.cmd"),
            str(home / ".local" / "bin" / "claude"),
            str(Path(local_appdata) / "Anthropic" / "claude.exe"),
            str(Path(local_appdata) / "Programs" / "claude" / "claude.exe"),
        ])
    else:
        # macOS (Apple Silicon + Intel) и Linux.
        candidates.extend([
            str(home / ".local" / "bin" / "claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ])
    for c in candidates:
        try:
            if c and Path(c).exists() and Path(c).is_file():
                _claude_cli_cache = c
                return c
        except Exception:
            continue
    _claude_cli_cache = ""  # mark as searched, not found
    return None


def claude_auth_status(timeout: float = 8.0) -> dict:
    """Возвращает dict с состоянием авторизации `claude` CLI.

    Структура успешного ответа CLI (`claude auth status` → JSON):
        {
          "loggedIn": True,
          "authMethod": "claude.ai",
          "apiProvider": "firstParty",
          "email": "user@example.com",
          "orgId": "...",
          "orgName": "...",
          "subscriptionType": "max"
        }

    Эта обёртка возвращает ровно тот же dict (или `{"loggedIn": False,
    "error": "..."}` если что-то пошло не так — CLI отсутствует, упал, не
    вернул JSON и т.п.).

    Используется:
      • При старте Studio для запоминания текущего email.
      • Периодическим QTimer'ом раз в 90 сек чтобы заметить
        смену/выход/истечение лимита и показать AuthBanner юзеру.
      • Перед каждым запуском чата эпизода (pre-flight).
    """
    cli = find_claude_cli()
    if not cli:
        return {"loggedIn": False, "error": "cli_not_found"}
    try:
        r = subprocess.run(
            [cli, "auth", "status"],
            timeout=timeout, capture_output=True, text=True,
            encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
            **no_console_kwargs(),
        )
        if r.returncode != 0:
            return {
                "loggedIn": False,
                "error": f"exit_{r.returncode}",
                "stderr": (r.stderr or "")[:200],
            }
        out = (r.stdout or "").strip()
        if not out:
            return {"loggedIn": False, "error": "empty_output"}
        import json as _json
        data = _json.loads(out)
        if not isinstance(data, dict):
            return {"loggedIn": False, "error": "not_a_dict"}
        return data
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": "timeout"}
    except Exception as e:
        return {"loggedIn": False, "error": f"exception: {e}"}


# class ClaudeGeometryThread — вытащен в threads/generate.py 2026-05-04


def extract_text_from_file(path: Path) -> str:
    """Читает текст из файла. Поддерживаем .txt/.md/.rtf/.docx.
    Для .docx требуется python-docx (если не установлен — кидаем ошибку)."""
    suf = path.suffix.lower()
    if suf in (".txt", ".md", ".rtf"):
        # .rtf минимально читаем как plain — заголовки чистые, разметка обычно
        # игнорируется парсером Claude. Если будут жалобы — добавим striprtf.
        return path.read_text(encoding="utf-8", errors="replace")
    if suf == ".docx":
        try:
            from docx import Document  # type: ignore
        except ImportError:
            raise RuntimeError(
                "python-docx not installed; ask admin to add it to the build")
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    # Любой другой текстовый файл — пробуем как utf-8
    return path.read_text(encoding="utf-8", errors="replace")


def parse_episode_number(query: str) -> Optional[int]:
    """Извлекает целое число из строки вида «эпизод 15», «серия 15»,
    «episode 15», «15», «15.». Возвращает int или None."""
    m = re.search(r'\d+', query or "")
    return int(m.group(0)) if m else None


def find_episode_section(text: str, episode_num: int) -> Optional[str]:
    """Ищет секцию указанной серии в общем документе. Возвращает её содержимое
    (от своего заголовка до следующего такого же заголовка) или None если не нашла.

    Поддерживает заголовки: «Серия N», «Эпизод N», «Серія N», «Епізод N»,
    «Episode N», «Chapter N», «Глава N», или просто «N.» / «N)» в начале строки.
    Регистронезависимо. Латинская/кириллическая «E» — обе ловятся.
    """
    keywords = (
        r"(?:серия|серія|эпизод|епізод|episode|chapter|глава|серия\s*№|"
        r"эпизод\s*№)"
    )
    # Паттерн заголовка: KEYWORD <num>  ИЛИ  голый <num>. в начале строки
    pat_keyworded = re.compile(
        rf"(?im)^\s*{keywords}\s*[№#]?\s*(\d+)\b"
    )
    pat_bare = re.compile(r"(?m)^\s*(\d+)\s*[\.\)]\s")

    matches = []
    for m in pat_keyworded.finditer(text):
        try:
            matches.append((int(m.group(1)), m.start()))
        except ValueError:
            continue
    for m in pat_bare.finditer(text):
        try:
            matches.append((int(m.group(1)), m.start()))
        except ValueError:
            continue
    matches.sort(key=lambda x: x[1])

    # Найдём начало нужной серии
    start = None
    for i, (num, pos) in enumerate(matches):
        if num == episode_num:
            start = pos
            # Конец = начало следующего заголовка ЛЮБОЙ серии
            end = len(text)
            for n2, p2 in matches[i+1:]:
                if p2 > pos:
                    end = p2
                    break
            return text[start:end].strip()
    return None


def extract_episode_title(text: str, episode_num: int) -> str:
    """Phase 2 hotfix #9: вытаскивает «человеческое» название эпизода
    из заголовка секции в сценарии. Ищет строку вида
    «ЭПИЗОД 21: ТРОЙНОЕ ДНО» / «Episode 21 — Title» / «Серия 21. Имя» —
    возвращает то что после номера серии и разделителя.

    Поддерживает заголовки RU/UK/EN: «Серия», «Эпизод», «Серія»,
    «Епізод», «Episode», «Chapter», «Глава». Разделители между номером
    и названием: `:`, `.`, `—`, `–`, `-`, пробелы.

    Возвращает пустую строку если заголовка с этим номером нет или
    после номера ничего нет (например «ЭПИЗОД 21\n\nСцена 1...»).
    """
    if not text:
        return ""
    # Разделитель между номером и названием — `:`, `.`, `—`, `–`, `-` или
    # пробел/таб. НЕ включаем `\n` чтобы не «прыгать» на следующую строку
    # когда после номера ничего не написано (например «ЭПИЗОД 21\n\nСцена 1»).
    pattern = re.compile(
        r'^[ \t]*(?:серия|серія|эпизод|епізод|episode|chapter|глава)[ \t]*[№#]?[ \t]*'
        + str(episode_num)
        + r'[ \t:.\-—–]+(?P<title>[^\n\r]+?)[ \t]*$',
        re.IGNORECASE | re.MULTILINE
    )
    m = pattern.search(text)
    if not m:
        return ""
    title = m.group('title').strip()
    # Чистим хвостовые знаки препинания
    title = title.rstrip('.,;:—–-')
    if not title:
        return ""
    # Если ВСЁ КАПСОМ (например «ТРОЙНОЕ ДНО») — делаем нормальный регистр
    # (первая буква каждого слова заглавная, остальные строчные).
    if title.isupper():
        title = title.capitalize()
    return title


# class RunEpisodeThread — вытащен в threads/generate.py 2026-05-04


# ─── Обновления — потоки ─────────────────────────────────────────────────────
# CheckUpdateThread / DownloadUpdateThread / DownloadAppUpdateThread /
# SendUpdateThread / FetchStatsThread — вытащены в threads/update.py 2026-05-04.
# Импортированы вверху файла из `threads`.

# ─── Карточка шота ───────────────────────────────────────────────────────────
# class ShotCard — вытащен в widgets/editor_widgets.py
# 2026-05-04 (шаг 5A). Импортирован вверху файла из `widgets`.


# ─── Карточка референса (refs view) ──────────────────────────────────────────
# class RoundedTopImage / class RefCard — вытащены в widgets/editor_widgets.py
# 2026-05-04 (шаг 5A). Импортированы вверху файла из `widgets`.


# ─── Полноэкранный просмотр референса ────────────────────────────────────────

# ─── Диалоги (4 независимых) ────────────────────────────────────────────────
# class FullscreenImageDialog / RefDoneNoticeDialog /
# GeometryDoneNoticeDialog / CloseConfirmDialog
# вытащены в widgets/dialogs.py 2026-05-04. Импортированы в начале файла.


# ─── Вкладка «🎬 Новый эпизод» ────────────────────────────────────────────────
# class NewEpisodeView — вытащен в views/new_episode.py
# 2026-05-04 (шаг 5C). Импортирован вверху файла из `views`.


# ─── Поле ввода чата + Чат эпизода ────────────────────────────────────────────
# class ChatInputEdit / class EpisodeChatView — вытащены в views/episode_chat.py
# 2026-05-04 (шаг 5B). Импортированы вверху файла из `views`.



# ─── Вкладка «Актёры» ────────────────────────────────────────────────────────
# class ActorCard / class ActorsView — вытащены в views/actors.py
# 2026-05-04 (шаг 4C). Импортированы вверху файла из `views`.


# ─── Попап с галереей фото актёра ──────────────────────────────────────────────
# class _PhotoThumb / class _BigPhotoLabel / class ActorPhotosDialog —
# вытащены в widgets/actor_dialogs.py 2026-05-04 (шаг 4A).


# ─── Создание character-референса из фото актёра ──────────────────────────────


# Два варианта layout-а identity reference sheet. Юзер выбирает в попапе
# CreateActorRefDialog (widgets/actor_dialogs.py). Промпты зашиты как
# константы — иначе пришлось бы тащить файл в .app бандл.
# Маркер `{outfit}` подставляется из поля «Описание» юзера. Если поле
# пустое — подставляется generic строка чтобы не оставлять плейсхолдер.

ACTOR_REF_PROMPT_DETAILED = """Use the attached reference image or attached reference images as the identity anchor.

{identity_anchor}

Create one single clean technical identity reference sheet for the same exact person, designed for future image generation consistency.

This must be a single composite image with a custom asymmetric layout.

Important identity-source rule:
All facial detail panels on the right side must be derived directly from the attached reference image or attached reference images of the same person.
Do not invent new facial anatomy. Do not redesign the features. Do not reinterpret accessories. Do not substitute jewelry.
If multiple reference images are attached, use them together to preserve the exact same identity, exact same feature shapes, exact same accessories, and exact same facial structure.

Overall layout:
- left side: keep the original 6-panel structure unchanged
- right side: replace the former large face panel with 8 separate extreme facial detail panels

Left side layout:
- top left: 3 small head reference panels in one horizontal row:
  1. front head close-up
  2. left 3/4 head close-up
  3. left side profile head close-up
- bottom left: 3 body reference panels in one horizontal row:
  4. full body front view, head to toe
  5. full body left side view, head to toe
  6. full body back view, head to toe

Right side layout:
- 8 separate technical facial detail panels arranged in 4 rows and 2 columns:
  7. eyes only, slight right 45-degree angle
  8. eyes only, slight left 45-degree angle
  9. right ear only, slight 45-degree angle
  10. left ear only, slight 45-degree angle
  11. mouth only, slight right 45-degree angle, neutral relaxed expression
  12. mouth only, slight left 45-degree angle, subtle smile with visible teeth
  13. nose crop, slight right 45-degree angle
  14. nose crop, slight left 45-degree angle

Critical layout rule:
- keep panels 1-6 unchanged in structure and logic
- the right side must contain exactly 8 separate facial detail panels
- no full-face panel on the right side
- one single organized technical sheet only

No grid rule:
- do not place any grid, mesh, wireframe, or facial mapping overlay anywhere

Head panels rules:
- panels 1, 2, and 3 must remain in one horizontal row on the upper left
- use very tight head crops, head occupies most of each panel
- minimize empty space, keep the full head visible
- neutral natural expression, do not stylize or beautify the face

Body panels rules:
- panels 4, 5, and 6 must remain in one horizontal row on the lower left
- show the full figure from head to toe
- keep clean front, side, and back body views

Facial detail panels rules:
- Panels 7-8: eyes only — eyes, eyebrows, eyelids, lashes, nearby skin. Tight narrow crop.
- Panels 9-10: ears only — at 45-degree angle, isolated ear shape with earlobe, helix, antihelix, tragus. Preserve earrings if visible.
- Panels 11-12: mouth only — lips, philtrum, mouth corners. Panel 11 neutral, panel 12 subtle smile.
- Panels 13-14: nose only — nasal bridge, tip, nostrils. Crop starts below the eye line.

Global detail-panel rules:
- all 8 facial detail panels must feel technical and anatomical, not glamorous
- preserve realistic skin texture, visible pores, natural anatomy
- do not airbrush, smooth pores, or add beauty retouching

Identity preservation:
Preserve the exact same person from the attached reference images.
Same facial identity, age, head shape, hairstyle, hairline, eyebrow shape, eye shape, nose, lips, jawline, ears, skin tone, neck and body proportions.
Preserve any visible accessories exactly as shown.
Maintain high identity consistency across all 14 panels.

Clothing and condition:
{outfit}

Style:
Realistic studio photography, plain neutral studio background, soft even lighting, natural skin texture, sharp realistic detail, clean technical presentation, accurate proportions.

Hard constraints:
- exactly 3 small head panels, 3 small body panels, 8 facial detail panels
- right side panels in 4 rows and 2 columns
- no grid, no mesh, no wireframe
- one single organized composite sheet only
"""

ACTOR_REF_PROMPT_SIMPLE = """Use the attached reference image as the identity anchor.

{identity_anchor}

Create a clean technical identity reference sheet for the same exact person, designed for future image generation consistency.
Create one single image with a custom asymmetric layout based on a 3x3 grid structure.
Layout:
- top left: 3 full-body reference panels in one row:
  1. front full-body view, full figure from head to feet, including the head
  2. left side full-body view, full figure from head to feet, including the head
  3. back full-body view, full figure from head to feet, including the head
- bottom left: 3 head-and-shoulders reference panels in one row:
  4. front head-and-shoulders close-up
  5. left 3/4 head-and-shoulders close-up
  6. left side profile head-and-shoulders close-up
- right side: 1 enlarged portrait detail panel occupying a strict 2x2 area.
  This enlarged portrait panel must be much larger than the other panels and must clearly dominate the layout.
  Show the same exact person in a large frontal portrait view, looking directly into the camera.
  The full head must be visible inside the panel, with the top of the head fully included and not cropped.
  Show the face, full head, neck, upper shoulders, and the upper part of the clothing/collar.
  Use tight portrait framing, but not an extreme face crop.
  The person should fill most of the panel while still leaving a small amount of neutral background around the head and shoulders.
  Keep clearly visible natural skin texture, realistic pores, subtle under-eye texture, realistic lips, realistic nose shape, realistic facial detail, hairline, ears, neck, shoulders, and clothing details.
Composition rules:
- the large portrait panel occupies four standard cells on the right side
- the 3 full-body panels stay in one horizontal row on the upper left
- the 3 head-and-shoulders panels stay in one horizontal row on the lower left
- panels 1, 2, 3: full figure from head to feet including the head, natural realistic body proportions, head correctly sized relative to the body
- panels 4, 5, 6: head and shoulders visible, head fills most of the panel
- clean technical organized layout
Identity preservation:
Preserve the exact same person from the reference image.
Same facial identity, age, head shape, hairstyle, hairline, eyebrow shape, eye shape, nose, lips, jawline, ears, skin tone, neck and body proportions.
Do not redesign or beautify the face.
High identity consistency across all panels.
Clothing:
{outfit}
Style:
Realistic studio photography, plain neutral studio background, soft even lighting, natural skin texture, sharp realistic detail, clean presentation, accurate proportions.
Hard constraints:
- exactly 3 full-body panels showing the full figure from head to feet including the head
- exactly 3 head-and-shoulders panels
- exactly 1 enlarged portrait detail panel occupying a 2x2 area on the right side
- the enlarged portrait panel must show the complete head, neck, upper shoulders, and upper clothing/collar
- the enlarged portrait panel must not be an extreme face crop
- the top of the head must not be cropped
- panels 1, 2, 3 show the complete figure from head to feet with correct natural body proportions
"""


ACTOR_REF_PROMPT_CUSTOM = """{description}

Create a clean technical character reference sheet for a single consistent original character, designed for future image generation consistency.
Create one single image with a custom asymmetric layout based on a 3x3 grid structure.
Layout:
- top left: 3 full-body reference panels in one row:
  1. front full-body view, full figure from head to feet, including the head
  2. left side full-body view, full figure from head to feet, including the head
  3. back full-body view, full figure from head to feet, including the head
- bottom left: 3 head-and-shoulders reference panels in one row:
  4. front head-and-shoulders close-up
  5. left 3/4 head-and-shoulders close-up
  6. left side profile head-and-shoulders close-up
- right side: 1 enlarged portrait detail panel occupying a strict 2x2 area.
  This enlarged portrait panel must be much larger than the other panels and must clearly dominate the layout.
  Show the same character in a large frontal portrait view, looking directly into the camera.
  The full head must be visible inside the panel, with the top of the head fully included and not cropped.
  Show the face, full head, neck, upper shoulders, and the upper part of the clothing/collar.
  Use tight portrait framing, but not an extreme face crop.
  The character should fill most of the panel while still leaving a small amount of neutral background around the head and shoulders.
  Keep clearly visible natural surface texture, realistic pores where applicable, surface and material detail, hairline or equivalent, ears or equivalent, neck, shoulders, and clothing details.
Composition rules:
- the large portrait panel occupies four standard cells on the right side
- the 3 full-body panels stay in one horizontal row on the upper left
- the 3 head-and-shoulders panels stay in one horizontal row on the lower left
- panels 1, 2, 3: full figure from head to feet including the head, natural realistic body proportions, head correctly sized relative to the body
- panels 4, 5, 6: head and shoulders visible, head fills most of the panel
- clean technical organized layout
Character consistency:
Keep one single consistent character across all panels.
The same head shape, silhouette, body proportions, colors, textures, surface features, distinctive traits, and any accessories must stay identical in every panel.
Do not redesign the character between panels and do not introduce variations.
High visual consistency across all panels.
Style:
Realistic studio photography, plain neutral studio background, soft even lighting, natural skin texture, sharp realistic detail, clean presentation, accurate proportions.
Hard constraints:
- exactly 3 full-body panels showing the full figure from head to feet including the head
- exactly 3 head-and-shoulders panels
- exactly 1 enlarged portrait detail panel occupying a 2x2 area on the right side
- the enlarged portrait panel must show the complete head, neck, upper shoulders, and upper clothing/collar
- the enlarged portrait panel must not be an extreme face crop
- the top of the head must not be cropped
- panels 1, 2, 3 show the complete figure from head to feet with correct natural body proportions
"""


# Простой ASCII-транслит RU/UK → латиница для авто-имени файла.
# Не перевод (для перевода нужен AI). Просто phonetic mapping чтобы
# юзер мог увидеть осмысленное название и поправить если хочет.
_TRANSLIT_RU = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu',
    'я':'ya','і':'i','ї':'yi','є':'ye','ґ':'g',
}


# Стоп-слова которые выкидываются из автогенерируемого имени файла —
# они не несут смысла (мужчина, в, и, у, на, …) и только удлиняют название.
# Имя должно быть коротким и описательным: «artem_red_suit», а не
# «artem_muzhchina_v_krasnom_kostume_i_chernykh_ochkakh».
_FILENAME_STOPWORDS = {
    'muzhchina', 'zhenshchina', 'paren', 'devushka', 'chelovek', 'lico',
    'aktyor', 'aktyer', 'aktor', 'man', 'woman', 'person', 'guy', 'girl',
    'mister', 'mr', 'ms', 'mrs',
    'v', 've', 'vo', 'i', 'u', 'na', 'po', 'so', 's', 'k', 'ko', 'pri',
    'ot', 'iz', 'do', 'pod', 'nad', 'za', 'ob', 'pro', 'dlya', 'che',
    'in', 'on', 'at', 'of', 'with', 'and', 'or', 'a', 'the', 'an',
    'ego', 'eyo', 'ee', 'ona', 'oni', 'his', 'her',
    'kakoyto', 'kakoy', 'tipa', 'esche', 'tozhe', 'budet', 'byl',
    'byla', 'bylo', 'byli', 'byt', 'eto', 'tot', 'ta', 'to', 'te',
}


def transliterate_for_filename(s: str, max_words: int = 4,
                               max_len: int = 30) -> str:
    """Превращает «мужчина в красном костюме и чёрных очках» →
    'krasnom_kostume_chernykh_ochkakh' (без стоп-слов и без артефактов).

    Логика:
    1. Транслит RU/UK → латиница, lowercase
    2. Разбивка на слова, выброс стоп-слов («muzhchina», «v», «i», и т.п.)
    3. Не больше `max_words` слов и не длиннее `max_len` символов
    4. Юзер может вручную поправить — поле `Имя файла` редактируемое."""
    if not s:
        return ""
    out = []
    for ch in s.lower().strip():
        if ch in _TRANSLIT_RU:
            out.append(_TRANSLIT_RU[ch])
        elif ch.isascii() and (ch.isalnum() or ch in ' -_'):
            out.append(ch)
        elif ch.isspace():
            out.append(' ')
        # остальное (пунктуация, эмодзи, ё-варианты) — выбрасываем
    s2 = ''.join(out)
    # Чистим всё что не [a-z0-9_-\s]
    s2 = re.sub(r'[^a-z0-9_\s-]', '', s2)
    # Разбиваем на слова и фильтруем стоп-слова + слишком короткие (<2 букв)
    words = [w for w in re.split(r'[\s_-]+', s2) if w]
    words = [w for w in words if w not in _FILENAME_STOPWORDS and len(w) >= 2]
    if not words:
        return ""
    # Берём первые max_words значимых слов и обрезаем по max_len
    result = '_'.join(words[:max_words])
    if len(result) > max_len:
        # Обрезаем по границе слова чтобы не получить «artem_krasn»
        parts = result[:max_len].rsplit('_', 1)
        result = parts[0] if len(parts) > 1 else result[:max_len]
    return result.strip('_-')


def build_actor_ref_filename(actor_slug: str, description: str) -> str:
    """Строит имя файла рефа: <actor_slug>_<desc_slug>.jpg.
    Если описание пустое — <actor_slug>_<timestamp>.jpg."""
    desc_slug = transliterate_for_filename(description)
    if not desc_slug:
        desc_slug = time.strftime("%Y%m%d_%H%M%S")
    return f"{actor_slug}_{desc_slug}"
# class GenerateActorRefThread — вытащен в threads/generate.py 2026-05-04

# ─── Создание character-референса из фото актёра ──────────────────────────────
# class _LayoutVariantCard / CreateActorRefDialog / RefResultDialog —
# вытащены в widgets/actor_dialogs.py 2026-05-04 (шаг 4B).


# ─── Главное окно ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    # 2026-05-12 (v1.0.55): защита от запуска Studio через ярлык во время
    # auto-update. Splash-PS-процесс пишет свой PID в lock-file при старте,
    # удаляет в FormClosing. Если Studio стартует через ярлык пока bat
    # обновляет bundle — мы видим lock + PID жив → показываем popup и
    # закрываемся. Это убирает основную причину move_failed багов
    # v1.0.39-v1.0.54: юзер тыкал ярлык, открывал старую .exe, лочил
    # bundle для bat → апдейт падал.
    @staticmethod
    def _get_update_lock_path() -> Path:
        """Путь к lock-file. Cross-platform но реально использует только Win."""
        if sys.platform == 'win32':
            base = Path(os.environ.get('LOCALAPPDATA', '')
                        or (Path.home() / 'AppData' / 'Local'))
            return base / 'StoryboardStudio' / 'updating.lock'
        return (Path.home() / 'Library' / 'Application Support'
                / 'StoryboardStudio' / 'updating.lock')

    @staticmethod
    def _is_update_pid_alive(pid: int) -> bool:
        """True если процесс с этим PID живёт. На Win через tasklist
        (с no_console_kwargs чтобы не мигало чёрное окно), на Mac через
        os.kill(pid, 0). Возвращает False если PID мёртв или check упал."""
        if sys.platform == 'win32':
            try:
                r = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}'],
                    capture_output=True, text=True, timeout=5,
                    **no_console_kwargs())
                return str(pid) in (r.stdout or '')
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

    def _check_update_lock_alive(self) -> bool:
        """True если сейчас идёт auto-update (PS-splash жив с lock-file).
        False если lock-file нет или PID в нём мёртв (orphan — удаляем).

        Используется в __init__ для отказа стартовать Studio пока bat
        обновляет bundle."""
        lock_path = self._get_update_lock_path()
        if not lock_path.exists():
            return False
        try:
            pid_str = lock_path.read_text(encoding='utf-8').strip()
            pid = int(pid_str)
        except (ValueError, IOError, OSError):
            try:
                lock_path.unlink()  # corrupted lock — удаляем
            except Exception:
                pass
            return False
        if self._is_update_pid_alive(pid):
            return True
        # Orphan lock (PS crashed без cleanup) — удаляем, продолжаем старт.
        try:
            lock_path.unlink()
        except Exception:
            pass
        return False

    def __init__(self, project_root: Path):
        super().__init__()
        # 2026-05-12 (v1.0.55): защита от race с активным auto-update.
        # Если splash-PS процесс жив с lock-file — другой bootstrap прямо
        # сейчас обновляет Studio. Показываем popup и закрываемся —
        # иначе мы залочим bundle и сломаем bat'у move. Эта защита
        # дополняет QMessageBox перед quit (там юзер ПРЕДУПРЕЖДЁН не
        # запускать) — fail-safe на случай если юзер всё равно тыкнул ярлык.
        try:
            if self._check_update_lock_alive():
                QMessageBox.warning(
                    None, tr('update_lock_title'),
                    tr('update_lock_text'))
                QTimer.singleShot(0, QApplication.quit)
                return
        except Exception:
            # 2026-05-12 (v1.0.55): на .exe-сборке stderr идёт в /dev/null,
            # traceback.print_exc() невидим. Пишем в файл рядом с update_*.log
            # чтобы при падении lock-check юзер мог переслать диагностику.
            try:
                import traceback as _tb
                if sys.platform == 'win32':
                    log_dir = (Path(os.environ.get('LOCALAPPDATA', '')
                                    or (Path.home() / 'AppData' / 'Local'))
                               / 'StoryboardStudio' / 'logs')
                else:
                    log_dir = (Path.home() / 'Library' / 'Logs'
                               / 'StoryboardStudio')
                log_dir.mkdir(parents=True, exist_ok=True)
                from datetime import datetime as _dt
                ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_dir / 'studio_startup.log', 'a',
                          encoding='utf-8') as _f:
                    _f.write(f"{ts}  [lock_check_failed]\n")
                    _f.write(_tb.format_exc())
                    _f.write("\n")
            except Exception:
                pass  # никогда не валим pipeline из-за логирования
        self._project_root = project_root
        self._is_admin     = is_admin_mode(project_root)
        self._camera_lab_tab_idx: Optional[int] = 4 if self._is_admin else None
        # 2026-05-10 (БАГ 10 fix): Python-side lock для атомарного
        # read-modify-write episodes.json. Защищает от параллельных
        # `_save_active_gen_decision` вызовов когда несколько
        # AutonomousGenThread заканчиваются одновременно (shotgun+phone
        # генерились parallel — race на write losses). Cross-process
        # защита (от внешнего agent'а в claude -p) — через atomic
        # `os.replace()` в самом `_save_active_gen_decision`.
        self._episodes_json_lock = threading.Lock()
        # 2026-05-09: AI-auth state атрибуты инициализируем СРАЗУ —
        # `_build_settings_tab` (вызывается ниже из tabs.addTab) дёргает
        # `_refresh_claude_account_email`, который читает
        # `_last_known_auth_email`. Раньше эти атрибуты ставились ниже,
        # после tabs setup → AttributeError ловился try/except в helper'е,
        # но `traceback.print_exc()` логировал шум при каждом старте.
        self._last_known_auth_email: Optional[str] = None
        self._last_known_auth_loggedin: bool = True
        self._auth_dismissed_email: Optional[str] = None
        self._auth_switch_thread: Optional[QThread] = None

        # 2026-05-08 (Шаг 2): post-bootstrap finalize.
        # 2026-05-09: добавлен detect failed bootstrap (через
        # `pending_rollback.txt`). Если bat упал на середине, finalize
        # откатывает version.json → показываем popup пользователю.
        # Отложенный показ — через showEvent + 200мс QTimer, чтобы окно
        # успело отрисоваться даже на медленных Win-машинах (фиксированный
        # 500мс не гарантировал на slow-моделях).
        # 2026-05-11 (v1.0.44): finalize_pending_update возвращает 4-tuple
        # `(state, target_version, log_path, scheduled_at)` либо None.
        # state ∈ {'update_failed', 'reboot_pending', 'reboot_install_success'}.
        # `_update_failure_info` хранит весь tuple; диспатч в showEvent
        # идёт по индексу 0. Доступ к полям — ТОЛЬКО через индексы
        # (info[1], info[2], info[3]), НЕ через unpacking — чтобы изменения
        # формы tuple'а в будущем не ломали call sites.
        self._update_failure_info: Optional[Tuple] = (
            finalize_pending_update(project_root))
        # 2026-05-11 (v1.0.45): self-heal битых filename'ов в
        # `refs_decisions` всех episodes.json. Лечит mismatches от
        # старых регенераций (например `.png` в decisions, `.jpg` на
        # диске). Безопасно даже на пустых проектах — no-op если
        # ничего не найдено. Character outfit'ы НЕ подменяются
        # (outfit-safety, только лог в stderr). Подробности — в
        # `heal_stale_decisions` docstring.
        try:
            healed_n = heal_stale_decisions(project_root)
            if healed_n:
                sys.stderr.write(
                    f"[heal] startup self-heal: updated decisions in "
                    f"{healed_n} episode(s)\n")
        except Exception:
            traceback.print_exc()
        self._first_show_done: bool = False
        # 2026-05-09: self-healing синк pipeline.py из bundle в project_root.
        # Без этого AutonomousGenThread падал на «pipeline.py not found»
        # у коллег которые ставили через Installer (whitelist не копировал
        # .py до сегодняшнего дня), а после auto-update — у всех у кого
        # bundle обновился но project pipeline.py остался старым.
        try:
            sync_pipeline_py_to_project(project_root)
        except Exception:
            traceback.print_exc()
        # 2026-05-15: bridge-файл `image_provider.txt` для pipeline.py.
        # Pipeline запускается AI-агентом в subprocess'е и не имеет
        # доступа к QSettings — Studio пишет туда текущий выбор юзера.
        # Без этого настройка «Nano Banana 2 / OpenAI» в GUI влияла
        # только на шоты (`GenerateThread`), а локации/объекты
        # (pipeline.py) всегда шли через OpenAI flow.
        # 2026-05-23: после разделения провайдеров pipeline.py делает
        # локации/объекты/сториборды — это админский контекст. Берём
        # значение из `image_provider_admin()`.
        try:
            sync_image_provider_to_project(project_root, image_provider_admin())
        except Exception:
            traceback.print_exc()
        # 2026-05-21: bridge для pipeline.py — синхронизация Fast Gen ключа в .env
        # при старте Studio (на случай если ключ обновлён в QSettings но .env устарел).
        try:
            sync_api_key_to_env(project_root, load_api_key())
        except Exception:
            traceback.print_exc()
        # 2026-06-09: пул ключей (key_pool) — указываем writable project_root,
        # чтобы сайдкар fastgen_keys.txt и курсор легли рядом с image_provider.txt
        # /.env, а НЕ в read-only _MEIPASS (в frozen GUI key_pool импортируется
        # из бандла, поэтому его дефолтный __file__.parent не writable). CLI этот
        # код не исполняет (storyboard_app не импортируется в subprocess'ах).
        try:
            import key_pool
            key_pool.set_root(project_root)
        except Exception:
            traceback.print_exc()
        # Активный сериал и эпизод
        self._current_show:    Optional[str] = get_current_show(project_root)
        self._current_episode: Optional[str] = None
        self._meta: Dict = {}
        # Готовим пути для активного сериала (или None если сериалов нет)
        shows = list_shows(project_root)
        if self._current_show not in shows:
            self._current_show = shows[0] if shows else None
            if self._current_show:
                set_current_show(project_root, self._current_show)
        setup_paths_for_show(project_root, self._current_show)
        if self._current_show:
            self._meta = read_episodes_meta(SHOW_ROOT)
        # Этап 1 (формат кадра): читаем формат активного сериала в глобал.
        # Дефолт "9:16". Логика генерации/UI его пока НЕ используют.
        self._current_aspect: str = (
            show_manager.show_aspect(project_root, self._current_show)
            if self._current_show else "9:16")
        # 2026-05-11 (v1.0.46) diagnostic: для расследования "empty chats
        # after auto-update". Если проблема повторится — запуск из
        # терминала с `2>&1 | tee log.txt` соберёт SHOW_ROOT + meta state
        # на старте.
        try:
            sys.stderr.write(
                f"[init] current_show={self._current_show!r} "
                f"SHOW_ROOT={SHOW_ROOT} "
                f"_meta loaded with {len(self._meta)} episodes\n")
        except Exception:
            pass

        self.setWindowTitle("Storyboard Studio")
        # Размер окна = ровно под 4 шота 9:16 + chrome без горизонтального скролла.
        # Карточка: 207 (CARD_W) + 10×2 (внутренний padding QFrame) = 227
        # 4 карточки × 227 + 3 spacing × 12 = 944
        # + 28×2 margins tab content = 1000
        # ИТОГО минимум 1000 ширина — ряд карточек занимает ровно ту же ширину
        # что и кнопка «Сохранить стриборд как PNG» внизу (она stretch=full).
        # 2026-05-08 hotfix: minimum size снижен с 1000×900 до 900×600 чтобы
        # окно влезало на маленьких ноутбуках (1366×768 типично у коллег).
        # Внутренние QScrollArea позволят прокручивать контент если он не
        # помещается. Стартовый resize — 1100×800 (если экран позволяет;
        # Qt сам ограничит до экрана если нет).
        # 2026-05-08: запоминание размера окна между запусками.
        # Сохранение в closeEvent через saveGeometry, восстановление здесь
        # через restoreGeometry. На Mac работает надёжно (plist знает binary).
        # Стартовый размер 1000×800 — точно под 4 карточки шотов без
        # пустого пространства справа (4×227 + 3×12 + 28×2 margin ≈ 1000).
        self.setMinimumSize(900, 600)
        self.resize(1000, 800)
        try:
            _gs = QSettings(APP_ORG, APP_NAME)
            _geom = _gs.value("main_window_geometry")
            if _geom:
                self.restoreGeometry(_geom)
        except Exception:
            traceback.print_exc()
        self.current_block: Optional[str] = None
        # Параллельные регенерации: ключ (block_name, panel_idx) → поток.
        # Каждый шот в каждом блоке может генериться независимо от других.
        self._active_regens: Dict[tuple, GenerateThread] = {}
        # 2026-06-08: время старта генерации шота {(block, panel_idx): ts} —
        # чтобы busy-overlay карточки показывал верные секунды СРАЗУ при заходе
        # на блок с активной генерацией (как _active_generations у актёров).
        self._shot_gen_started_at: Dict[tuple, float] = {}
        # 2026-06-09: keep-alive для завершившихся GenerateThread'ов. Кастомный
        # сигнал finished/error летит ИЗНУТРИ run() ДО его возврата; немедленный
        # .pop последней ссылки в слоте → GC уничтожает QThread пока он ещё
        # running → "QThread: Destroyed while thread is still running" → abort()
        # (краш под Mode C, ~30 потоков). _retire_thread держит ссылку в этом
        # множестве, пока QThread.isFinished() (= run() ВЕРНУЛСЯ), затем
        # deleteLater; reaper-таймер периодически сметает завершившиеся.
        self._threads_pending_delete: set = set()
        self._thread_reaper = QTimer(self)
        self._thread_reaper.setInterval(1500)
        self._thread_reaper.timeout.connect(self._reap_finished_threads)
        # 2026-06-03 (Этап 2): перевод реплик на uk через Haiku. Кэш {en→uk}
        # (повторный клик не дёргает модель) + список живых тредов (чтобы их
        # не собрал GC до завершения).
        self._uk_tr_cache: Dict[str, str] = {}
        self._translate_threads: list = []
        # Эпизоды, чей файл переводов уже подгружен в кэш (ленивая загрузка).
        self._uk_tr_loaded_eps: set = set()
        # Эпизоды с идущим батч-переводом — гард от дубля при кликах подряд.
        self._uk_tr_busy_eps: set = set()
        # 2026-06-02: буфер копирования картинки шота между шотами/блоками.
        # Храним БАЙТЫ (снимок активной картинки на момент Copy), не путь —
        # чтобы вставка не зависела от последующих изменений источника.
        self._shot_clipboard: Optional[bytes] = None
        # 2026-05-07: глобальный реестр параллельных генераций
        # location/object из чата эпизода (`AutonomousGenThread`). Ключ —
        # f"{ep_id}:{gen_type}:{name}". Значение — dict с полями:
        #   thread, ep_id, gen_type, name, description, target_filename
        # Юзер может запускать новые генерации в любом эпизоде, при этом
        # старые продолжают идти в фоне. UI: попап `_active_gens_panel`
        # (non-modal) + кнопка-индикатор внизу EpisodeChatView с бегущими
        # точками. character сюда не попадает (отдельный flow).
        self._active_gens: Dict[str, dict] = {}
        self._active_gens_panel = None  # lazy-created widgets.ActiveGensPanel
        # 2026-05-07: реестр активных character-генераций (через Актёры).
        # ep_id → set(character_slug). Используется EpisodeChatView чтобы
        # не показывать заново gen-карточку для персонажа пока юзер уже
        # стартанул его генерацию через выбор варианта одежды → Создать
        # референс. Когда генерация завершится (либо linked в decisions,
        # либо ошибка) — slug удаляется отсюда.
        self._active_character_gens: Dict[str, set] = {}
        # 2026-05-08: ПОБЛОЧНАЯ генерация. Внутри блока — все шоты
        # параллельно (по числу шотов в блоке: 1/2/3/4). Между блоками —
        # последовательно: следующий блок стартует когда ВСЕ шоты
        # текущего блока завершились (включая упавшие — упавшие не
        # блокируют переход дальше; юзер перезапустит вручную).
        #
        # _storyboard_blocks_queue — список незапущенных блоков; каждый
        # элемент = list[(block_basename, panel_idx)]. Заполняется в
        # `_on_storyboard_block_prompt_ready` по мере прихода .txt.
        # _storyboard_active_block — basename блока который сейчас идёт
        # (None если ничего не идёт). Используется только как маркер.
        # _storyboard_active_pending — сколько шотов в активном блоке
        # ещё не закончили. Когда 0 → активный блок завершён, берём
        # следующий из _storyboard_blocks_queue.
        # Регенерации юзера (`_on_regen`) этим состоянием не управляются.
        self._storyboard_blocks_queue: List[List[tuple]] = []
        self._storyboard_active_block: Optional[str] = None
        self._storyboard_active_pending: int = 0
        # 2026-06-11 (стоп-кнопка): глобальный флаг остановки. True → автопереход
        # к следующему блоку и старт новых блоков подавлены (сброс при новом прогоне).
        self._generation_stopped: bool = False
        # 2026-06-06 (Mode C): отдельный реестр тредов версий шота
        # (ключ (block, panel_idx, version_index)). Не пересекается с
        # _active_regens — старые хендлеры его не видят.
        self._active_mode_c_version_threads: Dict[tuple, GenerateThread] = {}
        # 2026-06-13 (конвейер по шотам): сквозное окно шотов эпизода. Заменяет
        # блок-сериализацию для Mode C — до mode_c_concurrent_shots() шотов в
        # полёте, шоты разных блоков идут единым потоком. _queue:
        # list[(block,panel,n,cams)]; _inflight: set[(block,panel)] занятых
        # слотов; _versions_left: сколько версий шота ещё летит (слот свободен
        # при 0). Наполняется из block_prompt_ready → замкнуто на текущий ep_id.
        self._shot_window_queue: List[tuple] = []
        self._shot_window_inflight: set = set()
        self._shot_window_versions_left: Dict[tuple, int] = {}
        # 2026-06-13 (строгий порядок блоков): буфер упорядочивания. Режиссёры
        # камер разных блоков финишируют вразнобой → результаты копятся здесь и
        # выпускаются в окно СТРОГО по номеру блока. _release_next — следующий
        # ожидаемый номер; _release_ready[block_n] = (shots,n,cams) ИЛИ None
        # (пропуск/сбой блока — продвигает порядок, ничего не enqueue'ит).
        self._block_release_next: Optional[int] = None
        self._block_release_ready: Dict[int, Optional[tuple]] = {}
        # 2026-06-09 (дотяжка Mode C): ключи (block,panel,version) версий, которые
        # УЖЕ повторно перезапускались — гарантия РОВНО 1 повтора (анти-задвоение).
        # Чистится в начале _spawn_mode_c_versions (новый блок = свежий бюджет).
        self._mode_c_retried: set = set()
        # 2026-06-13 (авто-добор per-shot): проходов добора НА ШОТ (ключ
        # (block,panel)). До MODE_C_RETRY_MAX_PASSES; чистится при завершении
        # шота (_note_shot_version_done) и в _stop_all_generation.
        self._shot_retry_pass: Dict[tuple, int] = {}
        # 2026-06-07 (Mode C): реестр фоновых тредов режиссёра камер
        # (ключ block_basename). Отдельно от версий; нужен для shutdown.
        self._camera_director_threads: Dict[str, QThread] = {}
        self._update_thread:     Optional[QThread]                 = None
        self._app_update_thread: Optional[DownloadAppUpdateThread] = None
        self._stats_thread:      Optional[FetchStatsThread]        = None
        self._latest_app_ver: Optional[str] = None
        # Множество (block, panel_idx) — недавно регенерированные шоты, ещё не
        # просмотренные пользователем. На карточке у них висит бейдж NEW.
        self._unseen_shots: set = set()
        # 2026-07-02: (block, panel_idx, version_n) — доборные версии Mode C,
        # ещё не открытые кликом. NEW на плитке версии в попапе. Переживает
        # закрытие/переоткрытие попапа (источник истины — MW, не диалог).
        # Снимается ТОЛЬКО кликом по версии (mark_version_seen); смена блока
        # НЕ чистит (в отличие от _unseen_shots).
        self._unseen_versions: set = set()
        # 2026-05-07: per-episode set путей рефов которые юзер ещё не «увидел»
        # после обновления (regen/edit). На карточке висит NEW-бейдж до тех
        # пор пока юзер не уйдёт с refs view этого эпизода (тогда чистим).
        # Структура: {ep_id: set[Path resolved]}.
        self._unseen_refs: Dict[str, set] = {}
        # Очередь уведомлений «изображение реф-картинки обновлено», накопленных
        # пока юзер не на refs-view. Каждый элемент: (image_path, mode, kind).
        # Показываются по одному когда юзер кликнет на пилюлю «Референсы».
        self._pending_ref_notices: list = []
        # Множество путей реф-картинок для которых сейчас идёт фоновое
        # обновление geometry через Claude CLI (между _start_geometry_thread
        # и _on_geometry_done/_on_geometry_error). Используется чтобы при
        # пересборке refs-view (новые карточки) ВЕРНУТЬ им «занятое» состояние —
        # юзер не сможет случайно кликнуть на картинку и запустить регенерацию.
        # 2026-05-07: dict[path → ep_id] (раньше set[path]) — нужно для
        # `_refs_busy(ep_id)` чтобы точки бежали только на пилюле того
        # эпизода, в котором запустили edit/regen рефа. `in`-проверки
        # (например в `_build_ref_card`) продолжают работать т.к. dict
        # поддерживает `key in dict`.
        self._active_geometry_paths: dict = {}
        # 2026-05-07: реестр путей для которых сейчас идёт image-gen фаза
        # (RefGenerateThread). Аналог `_active_geometry_paths`, но для
        # фазы 1. Нужно чтобы при пересоборе refs view (watcher debounce
        # и т.п.) новые карточки восстанавливали busy_overlay с надписью
        # «Генерирую изображение» / «Обновляю картинку», а не теряли его.
        # Структура: dict[Path resolved → {'ep_id': str, 'label_key': str}].
        self._active_image_paths: dict = {}
        # Тогглер для пульсации пилюли «Референсы» когда есть pending уведомления
        self._refs_pulse_on = False
        # Анимация индикаторов возле блоков с активной регенерацией.
        # Старые wide-пилюли используют точки, compact-пилюли — shimmer-фазы.
        self._dot_step = 0
        self._dot_anim_tick = 0
        self._block_gen_phase = 0
        # v1.0.85: state seedance_btn — 'ready' | 'pending' | 'restart'.
        # Используется в `_on_seedance_btn` для роутинга клика.
        self._seedance_btn_mode = 'pending'
        # Пилюли эпизодов и блоков (заполняются динамически)
        self._episode_pills: Dict[str, QPushButton] = {}
        self._block_pills:   Dict[str, QPushButton] = {}

        self._build_ui()
        self._populate_shows()
        self._populate_episodes()

        # File watcher следит за папкой сториборда активного сериала
        self._watcher = QFileSystemWatcher([str(STORYBOARDS_DIR)])
        self._watcher.directoryChanged.connect(
            lambda: QTimer.singleShot(600, self._reload_show))

        # v1.0.85: периодическая переоценка seedance_btn state — нужно
        # чтобы лейбл «Готовится…» сменился на «🔄 Перезапустить»
        # после 5 минут зависания (порог `SEEDANCE_STUCK_THRESHOLD_SEC`).
        # Тик каждые 30с — точность ±30с от порога, юзеру норм; нагрузка
        # минимальная (early-return если seedance_btn не виден).
        self._seedance_state_timer = QTimer(self)
        self._seedance_state_timer.setInterval(30_000)
        self._seedance_state_timer.timeout.connect(
            self._refresh_seedance_btn_state)
        self._seedance_state_timer.start()

        # Отдельный watcher для refs/ (locations/objects/characters) и
        # episodes.json — нужен чтобы перерисовать refs-view + дропдаун
        # эпизодов когда Claude через `claude -p` создаёт новые рефы или
        # обновляет episodes.json. Иначе юзер не увидит новые картинки до
        # перезапуска приложения.
        self._refs_watcher = QFileSystemWatcher()
        self._refs_watcher.directoryChanged.connect(
            lambda _path: QTimer.singleShot(800, self._refresh_after_external_change))
        self._refs_watcher.fileChanged.connect(
            lambda _p: QTimer.singleShot(800, self._refresh_after_external_change))
        self._wire_refs_watcher()

        # 2026-06-09: watcher файла-моста пула ключей (.fastgen_keys_active)
        # для лампочки round-robin в Настройках (КОСМЕТИКА). Путь = project_root
        # (тот же, что отдан в key_pool.set_root). Слушаем и файл, и каталог
        # (файл может ещё не существовать; Qt теряет путь при os.replace →
        # переподписка в слоте). Любая ошибка — молча, генерация не трогается.
        self._keypool_last_nonce = None
        try:
            self._keypool_active_path = str(self._project_root / ".fastgen_keys_active")
            self._keypool_watcher = QFileSystemWatcher()
            if os.path.exists(self._keypool_active_path):
                self._keypool_watcher.addPath(self._keypool_active_path)
            if self._project_root and os.path.isdir(str(self._project_root)):
                self._keypool_watcher.addPath(str(self._project_root))
            self._keypool_watcher.fileChanged.connect(self._on_keypool_active_changed)
            self._keypool_watcher.directoryChanged.connect(self._on_keypool_active_changed)
        except Exception:
            traceback.print_exc()

        # Авто-проверка обновлений через 2 секунды после запуска
        if github_configured():
            QTimer.singleShot(2000, self._check_updates)

        # Для админа: проверка наличия изменений для отправки.
        # 2026-05-08: убран периодический QTimer (5s) — на Win показывал
        # чёрное окно cmd при каждом git status. Теперь проверка лениво —
        # один раз при старте + при переключении на вкладку Settings
        # (см. _on_main_tab_changed).
        if self._is_admin:
            QTimer.singleShot(800, self._refresh_send_button)   # первая проверка

        # Статистика скачиваний для админа (из GitHub Releases API)
        if self._is_admin and github_configured():
            QTimer.singleShot(4000, self._fetch_download_stats)

        # Таймер анимации у блоков с активной регенерацией.
        # Срабатывает часто для плавного compact shimmer. Wide-пилюли с точками
        # обновляются только каждый третий тик, сохраняя старый темп.
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start(125)

        # Таймер пульсации пилюли «Референсы» когда там есть pending-уведомления.
        # Запущен всегда, но переключает CSS-property только если pending не пуст.
        self._refs_pulse_timer = QTimer(self)
        self._refs_pulse_timer.timeout.connect(self._tick_refs_pulse)
        self._refs_pulse_timer.start(600)

        # v1.0.88 (индикатор failed эпизодов): polling-таймер для красных
        # точек на пилюлях. 3с интервал — orchestrator пишет dump в
        # `_agent_log_<ep>.json` асинхронно из QThread; main-thread получает
        # сигналы progress, но также есть параллельные эпизоды для которых
        # сигнал не достигнет EpisodeChatView (юзер не на этом эпизоде).
        # Polling 3с — дешёвая операция (file exists + json head per ep,
        # <1ms), даёт near-real-time индикацию failed/running состояний
        # для всех эпизодов сразу. Setter сам сравнивает со старым
        # значением и вызывает update() только при изменении — без лишних
        # repaint'ов.
        self._pill_indicator_timer = QTimer(self)
        self._pill_indicator_timer.timeout.connect(
            self._refresh_episode_pill_indicators)
        self._pill_indicator_timer.start(3000)

        # 2026-06-10 (задача Б, этап 2): статусы ключей в Settings. base-цвет
        # индикатора per-idx (чтобы blink round-robin сбрасывал в СТАТУС-цвет,
        # не в жёсткий серый) + таймер обновления (countdown «вернётся через
        # N мин»), тикает ТОЛЬКО когда открыта вкладка Settings.
        self._key_status_colors: dict = {}
        # 2026-06-10 (лампочки v2): монотонный дедлайн зелёной вспышки per-idx —
        # _refresh не затирает индикатор пока вспышка активна (тик теперь 1с).
        self._key_flash_until: dict = {}
        self._key_status_timer = QTimer(self)
        self._key_status_timer.setInterval(1000)   # 1с: countdown лимита MM:SS
        self._key_status_timer.timeout.connect(
            self._refresh_key_status_indicators)
        # 2026-06-10 (health check): транзиентные UI-статусы ключей по field_idx
        # (checking/limit/server_down — НЕ в key_pool, только UI) + реестр
        # health-потоков для graceful shutdown.
        self._key_health_status: dict = {}
        self._key_health_threads: list = []
        # 2026-06-20 (usage-статистика): строка лимитов/потоков/reset/exp на КАЖДОЕ
        # поле ключа (field_idx → str); живёт между refresh'ами, отрисовка — в
        # _refresh_key_status_indicators. + реестр usage-потоков для shutdown.
        self._key_usage: dict = {}
        self._key_usage_threads: list = []
        # 2026-06-10 (очередь #1): одиночный поток боевой проверки сервера FastGen.
        self._server_check_thread = None
        self._server_check_timer = None      # UI-таймер секунд (не из потока)
        self._server_check_t0 = 0.0
        self._server_check_down_info = None  # {'n':sec,'op_id':str} последнего down — для письма в техподдержку

    def _tick_dots(self):
        """Перебирает шаги анимации точек и обновляет индикаторы у блоков
        с активной регенерацией шотов + у пилюли «Референсы» если там идёт
        регенерация рефа или обновление geometry. Также обновляет кнопку
        активных генераций в EpisodeChatView и строки попапа (2026-05-07).
        Если ничего не активно — ничего не делает."""
        refs_busy = self._refs_busy()
        active_gens_busy = bool(self._active_gens)
        if (not self._active_regens and not self._active_mode_c_version_threads
                and not refs_busy and not active_gens_busy):
            return
        self._dot_anim_tick = (self._dot_anim_tick + 1) % 3
        if self._dot_anim_tick == 0:
            self._dot_step = (self._dot_step + 1) % 3
        self._block_gen_phase = (self._block_gen_phase + 1) % 24
        active_blocks = {b for (b, _) in self._active_regens.keys()}
        active_blocks |= {
            b for (b, _p, _v) in self._active_mode_c_version_threads.keys()}
        for b in active_blocks:
            self._refresh_block_indicator(b)
        if refs_busy:
            self._refresh_refs_pill_text()
        if active_gens_busy:
            # Кнопка-индикатор живёт в EpisodeChatView; зовём её обновление
            # если view существует. Также шагаем точки в попапе.
            try:
                ev = getattr(self, 'episode_chat_view', None)
                if ev is not None and hasattr(ev, 'tick_active_gens_button'):
                    ev.tick_active_gens_button(self._dot_step)
                if self._active_gens_panel is not None:
                    self._active_gens_panel.tick_dots(self._dot_step)
            except Exception:
                import traceback
                traceback.print_exc()

    def _refs_busy(self, ep_id: Optional[str] = None) -> bool:
        """True если идёт регенерация/edit рефа ИЛИ обновление geometry.
        Используется для анимации точек на пилюле «Референсы».

        2026-05-07: per-episode фильтрация. Если `ep_id` задан — учитываем
        только треды/geometry-задачи относящиеся к этому эпизоду.
        Без `ep_id` — legacy-поведение (любая активность во всех эпизодах).

        Источники ep_id:
          • `_active_geometry_paths` теперь Dict[Path, ep_id].
          • `RefGenerateThread` помечен `_ep_id` атрибутом в `_start_ref_thread`.
          • `ClaudeGeometryThread` помечен `_ep_id` в `_on_ref_done`.
        """
        gpaths = getattr(self, '_active_geometry_paths', None) or {}
        if ep_id is None:
            if gpaths:
                return True
            for t in (getattr(self, '_ref_threads', []) or []):
                if t.isRunning():
                    return True
            return False
        # Фильтр по конкретному эпизоду.
        if isinstance(gpaths, dict):
            for _p, e in gpaths.items():
                if e == ep_id:
                    return True
        for t in (getattr(self, '_ref_threads', []) or []):
            if t.isRunning() and getattr(t, '_ep_id', None) == ep_id:
                return True
        return False

    def _refresh_refs_pill_text(self):
        """Обновляет текст пилюли «Референсы»: добавляет анимированные точки
        перед базовым текстом если есть активная регенерация рефа/geometry,
        иначе — только базовый текст. Шаг точек берётся из `_dot_step`
        (общий с блок-пилюлями).

        2026-05-07: точки бегут только если активность ПРИНАДЛЕЖИТ
        текущему эпизоду. Раньше pill блинкал на всех эпизодах когда
        edit запущен из одного — баг подсветки."""
        pill = getattr(self, '_refs_pill', None)
        if pill is None:
            return
        base = tr('refs')
        cur_ep = getattr(self, '_current_episode', None)
        if cur_ep and self._refs_busy(cur_ep):
            dots_pattern = ["·    ", "· ·  ", "· · ·"]
            pill.setText(dots_pattern[self._dot_step] + "  " + base)
        else:
            pill.setText(base)

    def _tick_refs_pulse(self):
        """Тоггл pulse_on на пилюле «Референсы» когда есть pending-уведомления.
        Если pending пуст — ничего не делает (пилюля в спокойном состоянии)."""
        pill = getattr(self, '_refs_pill', None)
        if pill is None:
            return
        if not self._pending_ref_notices:
            return
        self._refs_pulse_on = not self._refs_pulse_on
        pill.setProperty("pulse_on", self._refs_pulse_on)
        pill.style().unpolish(pill); pill.style().polish(pill)

    # ── 2026-05-07: глобальный реестр параллельных генераций ────────

    def _ensure_active_gens_panel(self):
        """Lazy-создание попапа `ActiveGensPanel` (non-modal). Панель
        живёт пока живёт MainWindow, между показами/скрытиями состояние
        не теряется."""
        if self._active_gens_panel is not None:
            return self._active_gens_panel
        from widgets import ActiveGensPanel
        panel = ActiveGensPanel(parent=self)
        panel.open_episode_requested.connect(self._on_active_gens_open_episode)
        panel.dismiss_requested.connect(self._on_active_gens_dismiss)
        self._active_gens_panel = panel
        return panel

    def register_active_gen(self, ep_id: str, gen_type: str, name: str,
                             description: str, thread, target_filename: str = ""):
        """Регистрирует запущенный AutonomousGenThread в глобальном реестре.

        Зовётся из EpisodeChatView._on_gen_button_clicked после thread.start().
        Подключает сигналы прогресса/финиша/ошибки к методам MW (попап),
        добавляет строку в попап, обновляет кнопку-индикатор в чате.

        Параметры:
            ep_id: 'ep4' и т.п.
            gen_type: 'location' / 'object'
            name: slug рефа (lawyer_office)
            description: описание для попапа (sub-text)
            thread: уже-стартанутый AutonomousGenThread
            target_filename: имя файла который тред должен создать (для
                             auto-link через _save_ref_decision при finish).
        """
        key = f"{ep_id}:{gen_type}:{name}"
        # Дедуп: если такой же ключ уже зарегистрирован — игнорируем.
        if key in self._active_gens:
            return
        self._active_gens[key] = {
            'thread': thread,
            'ep_id': ep_id,
            'gen_type': gen_type,
            'name': name,
            'description': description,
            'target_filename': target_filename,
            'status': 'running',
        }
        # Сигналы AutonomousGenThread → методы MW.
        try:
            thread.progress.connect(self._on_active_gen_progress)
            thread.image_ready.connect(self._on_active_gen_image_ready)
            thread.finished_ok.connect(self._on_active_gen_finished)
            thread.error.connect(self._on_active_gen_error)
        except Exception:
            import traceback
            traceback.print_exc()
        # Добавляем строку в попап (создавая попап лениво).
        panel = self._ensure_active_gens_panel()
        try:
            panel.add_row(key, ep_id, gen_type, name)
        except Exception:
            import traceback
            traceback.print_exc()
        # Обновляем кнопку-индикатор в чате.
        self._refresh_active_gens_button()

    def is_active_gen(self, ep_id: str, gen_type: str, name: str) -> bool:
        """Используется EpisodeChatView чтобы при возврате в эпизод не
        создавать idle-карточку для уже-бегущей генерации."""
        return f"{ep_id}:{gen_type}:{name}" in self._active_gens

    def active_gens_count(self) -> int:
        """Глобальный счётчик активных генераций — сумма по всем эпизодам.
        Используется для попапа `ActiveGensPanel` (там видно ВСЁ что бежит).
        Для индикатора «N в работе» в шапке чата эпизода — НЕ подходит,
        смотри `active_gens_count_for_ep` ниже."""
        return len(self._active_gens)

    def active_gens_count_for_ep(self, ep_id: str) -> int:
        """2026-05-10: per-ep счётчик. Используется индикатором
        «🎨 N в работе» в шапке чата — раньше там был глобальный счёт,
        протекал между эпизодами (запустил на ep7 → видно на ep4/5/6
        тоже)."""
        if not ep_id:
            return 0
        return sum(1 for info in self._active_gens.values()
                   if info.get('ep_id') == ep_id)

    def has_active_gens_for_ep(self, ep_id: str) -> bool:
        for info in self._active_gens.values():
            if info.get('ep_id') == ep_id:
                return True
        return False

    def has_active_character_gens_for_ep(self, ep_id: str) -> bool:
        """2026-05-17: симметрия с has_active_gens_for_ep, но для
        character-flow (Actors-вкладка). EpisodeChatView._check_montage_ready
        раньше смотрел глобальный ActorsView._active_generations (без ep_id) —
        любая character-генерация на любом эпизоде скрывала CTA «Сделать
        монтажную карту» во ВСЕХ эпизодах. Теперь читаем per-ep реестр
        `_active_character_gens` (заполняется через
        register_active_character_gen с привязкой к ep_id).
        """
        try:
            bucket = (self._active_character_gens or {}).get(ep_id)
            return bool(bucket)
        except Exception:
            return False

    # 2026-05-07: реестр character-генераций (Actors-flow). Парный API
    # с register_active_gen для location/object, но проще — без thread'а
    # и без панели (character-генерации уже видны в табе «Актёры»).
    def register_active_character_gen(self, ep_id: str, slug: str):
        if not ep_id or not slug:
            return
        self._active_character_gens.setdefault(ep_id, set()).add(slug)

    def unregister_active_character_gen(self, ep_id: str, slug: str):
        if not ep_id or not slug:
            return
        bucket = self._active_character_gens.get(ep_id)
        if not bucket:
            return
        bucket.discard(slug)
        if not bucket:
            self._active_character_gens.pop(ep_id, None)

    def is_active_character_gen(self, ep_id: str, slug: str) -> bool:
        if not ep_id or not slug:
            return False
        bucket = self._active_character_gens.get(ep_id)
        return bool(bucket and slug in bucket)

    def _key_for_gen_thread(self, sender) -> str:
        """Ищет ключ в `_active_gens` по объекту треда (sender в слотах)."""
        if sender is None:
            return ""
        for k, info in self._active_gens.items():
            if info.get('thread') is sender:
                return k
        return ""

    def _on_active_gen_progress(self, line: str):
        """AutonomousGenThread.progress → строка в попапе."""
        try:
            sender = self.sender()
            key = self._key_for_gen_thread(sender)
            if not key:
                return
            panel = self._active_gens_panel
            if panel is not None:
                panel.update_progress(key, line)
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_active_gen_image_ready(self):
        """AutonomousGenThread.image_ready → меняем статус на «картинка готова»."""
        try:
            sender = self.sender()
            key = self._key_for_gen_thread(sender)
            if not key:
                return
            panel = self._active_gens_panel
            if panel is not None:
                panel.update_progress(key, tr('gen_state_image_ready'))
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_active_gen_finished(self, path_hint: str):
        """AutonomousGenThread.finished_ok → done в попапе + auto-link decision.
        Через 3с строка авто-удаляется."""
        try:
            sender = self.sender()
            key = self._key_for_gen_thread(sender)
            if not key:
                return
            info = self._active_gens.get(key, {})
            ep_id = info.get('ep_id', '')
            gen_type = info.get('gen_type', '')
            name = info.get('name', '')
            # 2026-06-18: disk-check ДО записи linked — закрывает «ложный
            # finished_ok» (агент рапортует ✓ done, но провайдер картинок
            # упал и .jpg не создан). Без проверки decision=linked писался
            # на несуществующий файл → карточка уходила, в РЕФЕРЕНСАХ пусто
            # (list_episode_refs disk-check скрывает), в логе тишина.
            file_on_disk = True
            try:
                if gen_type in ('location', 'object') and name:
                    cur_show = get_current_show(self._project_root)
                    if cur_show:
                        sub = ('locations' if gen_type == 'location'
                               else 'objects')
                        base = (self._project_root / "shows" / cur_show
                                / "refs" / sub)
                        file_on_disk = any(
                            (base / f"{name}{ext}").exists()
                            for ext in ('.jpg', '.jpeg', '.png', '.webp'))
            except Exception:
                # Ошибка проверки → не ломаем старое поведение (как раньше).
                file_on_disk = True
            if ep_id and gen_type and name and not file_on_disk:
                # Ложный finished_ok: файла нет. НЕ пишем linked (иначе
                # карточка молча исчезает), логируем причину, показываем
                # ошибку в попапе. Карточка вернётся к нажатию после
                # перезагрузки/переключения сериала (_gen_seen_names
                # сбрасывается в EpisodeChatView.set_episode).
                sub = ('locations' if gen_type == 'location' else 'objects')
                try:
                    self._log_autogen_error(
                        gen_type, name,
                        f"finished_ok но файл не создан (refs/{sub}/{name}.* "
                        f"отсутствует) — вероятно провайдер картинок упал, "
                        f"агент ложно отрапортовал done")
                except Exception:
                    pass
                panel = self._active_gens_panel
                if panel is not None:
                    try:
                        panel.set_error(
                            key, "файл не создан — повторите генерацию")
                    except Exception:
                        pass
                self._active_gens.pop(key, None)
                self._refresh_active_gens_button()
                return
            # Auto-link decision: записать в episodes.json[ep_id].refs_decisions.
            try:
                if ep_id and gen_type and name:
                    fn = self._resolve_active_gen_filename(
                        gen_type, name, path_hint)
                    if fn:
                        self._save_active_gen_decision(
                            ep_id, gen_type, name, "linked", filename=fn)
            except Exception:
                import traceback
                traceback.print_exc()
            # Done в попапе.
            panel = self._active_gens_panel
            if panel is not None:
                panel.set_done(key)
            # Через 3с авто-удаление строки + декремент счётчика.
            QTimer.singleShot(3000,
                              lambda k=key: self._cleanup_active_gen(k))
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_active_gen_error(self, msg: str):
        """AutonomousGenThread.error → красная плашка с ✕ кнопкой.
        Сразу декрементим счётчик (юзер увидит ошибку и сам дисмиснет)."""
        try:
            sender = self.sender()
            key = self._key_for_gen_thread(sender)
            if not key:
                return
            panel = self._active_gens_panel
            if panel is not None:
                panel.set_error(key, msg)
            # Поток умер, держать его в counter'е смысла нет.
            self._active_gens.pop(key, None)
            self._refresh_active_gens_button()
        except Exception:
            import traceback
            traceback.print_exc()

    def _cleanup_active_gen(self, key: str):
        """Через 3с после done — убираем строку из попапа + из реестра."""
        try:
            self._active_gens.pop(key, None)
            panel = self._active_gens_panel
            if panel is not None:
                panel.remove_row(key)
            self._refresh_active_gens_button()
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_active_gens_dismiss(self, key: str):
        """Юзер нажал ✕ на error-строке — убираем её."""
        try:
            self._active_gens.pop(key, None)
            panel = self._active_gens_panel
            if panel is not None:
                panel.remove_row(key)
            self._refresh_active_gens_button()
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_active_gens_open_episode(self, ep_id: str):
        """Юзер кликнул по строке в попапе — переключаем на этот эпизод."""
        try:
            if not ep_id:
                return
            # Метод переключения эпизода MainWindow ищет пилюлю по ep_id.
            pill = self._episode_pills.get(ep_id)
            if pill is not None:
                pill.click()
        except Exception:
            import traceback
            traceback.print_exc()

    def _refresh_active_gens_button(self):
        """Зовётся при add/remove — обновляет видимость и текст кнопки
        в EpisodeChatView."""
        try:
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None and hasattr(ev, 'refresh_active_gens_button'):
                ev.refresh_active_gens_button()
        except Exception:
            import traceback
            traceback.print_exc()

    def open_active_gens_panel(self):
        """Юзер нажал на кнопку-индикатор в чате — открываем попап."""
        panel = self._ensure_active_gens_panel()
        try:
            panel.show()
            panel.raise_()
            panel.activateWindow()
        except Exception:
            import traceback
            traceback.print_exc()

    def _resolve_active_gen_filename(self, gen_type: str, name: str,
                                      hint_path: str) -> str:
        """Имя файла рефа после автономной генерации.

        2026-05-10 (БАГ 3 fix): сначала глоб по диску (disk truth),
        потом hint от агента с обязательным `exists()` check. Раньше
        hint возвращался слепо — если агент эмитил `✓ done shotgun.png`
        но pipeline.py создал `.jpg`, в decisions писалось `.png`,
        list_episode_refs делал `(.png).exists() == False` и реф не
        появлялся в РЕФЕРЕНСАХ эпизода (хотя файл реально лежал)."""
        try:
            cur_show = get_current_show(self._project_root)
            if not cur_show:
                return f"{name}.jpg"
            sub = {'location': 'locations', 'object': 'objects'}.get(
                gen_type, gen_type + 's')
            base = self._project_root / "shows" / cur_show / "refs" / sub
            # 1. Disk truth — глоб по фактически лежащему файлу.
            for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                if (base / f"{name}{ext}").exists():
                    return f"{name}{ext}"
            # 2. Fallback: hint от агента, но только если файл реально
            #    существует (защита от hallucinated extension).
            if hint_path and '.' in hint_path:
                cand = hint_path.split('/')[-1]
                if cand and not cand.endswith('/'):
                    if (base / cand).exists():
                        return cand
            # 3. Last resort — стандартный .jpg (даже если файла нет;
            #    list_episode_refs дальше сам проверит exists()).
            return f"{name}.jpg"
        except Exception:
            return f"{name}.jpg"

    def _save_active_gen_decision(self, ep_id: str, gen_type: str,
                                   name: str, decision: str,
                                   filename: str = ""):
        """Аналог `EpisodeChatView._save_ref_decision`, но MW-уровневый —
        умеет писать decision для любого ep_id (не только текущего).

        2026-05-10 (БАГ 10 fix):
          • Disk-validation параметра `filename` — если файла нет на
            диске под этим именем, ищем actual file через glob `{name}.*`.
            Это safety net в дополнение к `_resolve_active_gen_filename`
            на случай TOCTOU race или вызова из других мест.
          • Threading.Lock вокруг read-modify-write — защита от Python-
            side race (parallel finished_ok).
          • Atomic write через temp-file + `os.replace()` — защита от
            частичных записей если процесс убьют посередине; на POSIX
            atomic rename (cross-process gate, хотя agent в claude -p
            может всё равно overwrite после нас).
        """
        try:
            shows_root = self._project_root / "shows"
            cur_show = get_current_show(self._project_root)
            if not cur_show:
                return
            # Disk-validation filename — защита от устаревшего расширения.
            if filename and gen_type in ('location', 'object'):
                try:
                    sub = ('locations' if gen_type == 'location'
                           else 'objects')
                    base = (self._project_root / "shows" / cur_show
                            / "refs" / sub)
                    if not (base / filename).exists():
                        # Filename НЕ найден на диске — ищем real file
                        # с тем же base name через glob.
                        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                            p = base / f"{name}{ext}"
                            if p.exists():
                                filename = f"{name}{ext}"
                                break
                except Exception:
                    pass
            ep_meta = (shows_root / cur_show / "episodes.json")
            # Lock + atomic write.
            with self._episodes_json_lock:
                import json
                data = {}
                if ep_meta.exists():
                    try:
                        data = json.loads(
                            ep_meta.read_text(encoding='utf-8')) or {}
                    except Exception:
                        data = {}
                ep_block = data.get(ep_id) or {}
                if not isinstance(ep_block, dict):
                    ep_block = {}
                decisions = ep_block.get('refs_decisions') or {}
                if not isinstance(decisions, dict):
                    decisions = {}
                type_map = decisions.get(gen_type) or {}
                if not isinstance(type_map, dict):
                    type_map = {}
                entry = {'decision': decision}
                if filename:
                    entry['filename'] = filename
                type_map[name] = entry
                decisions[gen_type] = type_map
                ep_block['refs_decisions'] = decisions
                data[ep_id] = ep_block
                # Atomic write: temp + os.replace(). Имя tmp включает
                # PID — защита от collision если юзер случайно запустил
                # две Studio процесса одновременно.
                tmp = ep_meta.with_suffix(f'.json.tmp.{os.getpid()}')
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding='utf-8')
                os.replace(tmp, ep_meta)
        except Exception:
            import traceback
            traceback.print_exc()


    def _ref_kind_from_path(self, image_path: Path) -> str:
        """По пути реф-картинки возвращает 'location' / 'object' / 'character'."""
        parts = {p.lower() for p in image_path.parts}
        if 'locations' in parts:
            return 'location'
        if 'objects' in parts:
            return 'object'
        if 'characters' in parts:
            return 'character'
        return 'location'

    def _sync_decision_filenames_after_regen(self, image_path: Path,
                                              kind: str) -> None:
        """2026-05-11 (v1.0.45): после успешной регенерации рефа обновить
        filename в `refs_decisions` всех эпизодов **текущего show** где
        этот реф был `linked`. Кросс-show мы НЕ трогаем — реф из show A
        не должен влиять на decisions show B (даже если у них совпадают
        slug'и). Текущий show определяется через `get_current_show`.

        Защита от устаревших filename'ов (например `.png` в decisions,
        а на диске после regen теперь `.jpg`).

        Логика по kind:
          location/object — slug = image_path.stem, filename = image_path.name.
            Обновляем decisions[<kind>][<slug>] если filename отличается.
          character — folder = parent dir name, file = image_path.name.
            decision filename для character имеет формат `<folder>/<file>`.
            slug в decisions = имя character'а (как в чате AI). Обновляем
            только если existing entry имеет matching folder prefix
            (чтобы не подменить другой outfit того же character'а).

        Атомарно через `_episodes_json_lock` + temp + os.replace
        (для episodes.json текущего show).
        """
        try:
            cur_show = get_current_show(self._project_root)
            if not cur_show:
                return
            ep_meta_path = (self._project_root / "shows" / cur_show
                            / "episodes.json")
            if not ep_meta_path.exists():
                return
            new_name = image_path.name
            slug = image_path.stem
            char_folder = image_path.parent.name if kind == 'character' else None
            with self._episodes_json_lock:
                data = json.loads(ep_meta_path.read_text(encoding='utf-8'))
                changed = False
                for ep_id, ep in data.items():
                    if not isinstance(ep, dict):
                        continue
                    decisions = ep.get('refs_decisions')
                    if not isinstance(decisions, dict):
                        continue
                    bucket = decisions.get(kind)
                    if not isinstance(bucket, dict):
                        continue
                    for name, entry in bucket.items():
                        if not isinstance(entry, dict):
                            continue
                        if entry.get('decision') != 'linked':
                            continue
                        old_fn = entry.get('filename', '')
                        if kind == 'character':
                            # decision filename format: "<folder>/<file>"
                            # Обновляем только если folder matches.
                            if '/' not in old_fn:
                                continue
                            old_folder, _, _ = old_fn.partition('/')
                            if old_folder != char_folder:
                                continue
                            new_full = f"{char_folder}/{new_name}"
                            if old_fn == new_full:
                                continue
                            entry['filename'] = new_full
                            changed = True
                            sys.stderr.write(
                                f"[sync] {ep_id}/{kind}/{name}: "
                                f"{old_fn} → {new_full}\n")
                        else:
                            if old_fn == new_name:
                                continue
                            # Проверяем что slug совпадает (защита от
                            # подмены другого рефа с тем же extension)
                            old_slug = Path(old_fn).stem
                            if old_slug != slug:
                                continue
                            entry['filename'] = new_name
                            changed = True
                            sys.stderr.write(
                                f"[sync] {ep_id}/{kind}/{name}: "
                                f"{old_fn} → {new_name}\n")
                if changed:
                    tmp = ep_meta_path.with_suffix('.json.tmp')
                    tmp.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding='utf-8')
                    import os as _os
                    _os.replace(str(tmp), str(ep_meta_path))
        except Exception:
            traceback.print_exc()

    def _set_refs_pill_notice(self, has_notice: bool):
        """Поднимает/снимает оранжевую подсветку пилюли «Референсы»."""
        pill = getattr(self, '_refs_pill', None)
        if pill is None:
            return
        pill.setProperty("has_notice", has_notice)
        # Сбрасываем фазу пульсации чтобы при включении начать с тёмной фазы
        if has_notice:
            self._refs_pulse_on = False
            pill.setProperty("pulse_on", False)
            try:
                pill.setToolTip(tr('ref_pill_has_notice_tooltip'))
            except Exception:
                pass
        else:
            pill.setProperty("pulse_on", False)
            try:
                pill.setToolTip("")
            except Exception:
                pass
        pill.style().unpolish(pill); pill.style().polish(pill)

    def _drain_pending_ref_notices(self):
        """Показывает один за другим все накопленные диалоги об обновлении
        реф-картинок. Очищает очередь и снимает подсветку с пилюли."""
        if not self._pending_ref_notices:
            self._set_refs_pill_notice(False)
            return
        # Снимаем подсветку — юзер уже на refs view, увидел обновления
        self._set_refs_pill_notice(False)
        notices = list(self._pending_ref_notices)
        self._pending_ref_notices.clear()
        for image_path, mode, kind in notices:
            try:
                if mode == 'geometry_done':
                    dlg = GeometryDoneNoticeDialog(image_path.stem, parent=self, kind=kind)
                else:
                    dlg = RefDoneNoticeDialog(image_path.stem, parent=self, kind=kind)
                dlg.exec()
            except Exception:
                pass

    def _build_ui(self):
        # Центральный виджет с фирменным LUMZ-фоном:
        # радиальный градиент сверху по центру (paintEvent внутри
        # `LumzBackground`) → к краям переход в глубокий чёрный
        # #0a0a0d. Этап 1 редизайна 2026-05-08.
        from views.theme import LumzBackground
        bg = LumzBackground()
        bg.setObjectName("main-bg")
        self.setCentralWidget(bg)
        main = QVBoxLayout(bg)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        main.addWidget(self._build_header())

        # 2026-05-08 редизайн Этап 2: разделительная линия под шапкой удалена —
        # карточка шапки сама отделена от контента отступом (margins у outer-
        # контейнера в `_build_header`).

        # 2026-05-06: AuthBanner — плашка для уведомлений о смене AI-аккаунта.
        # Скрыта по умолчанию; показывается через self._show_auth_banner(...).
        from widgets.auth_banner import AuthBanner
        self.auth_banner = AuthBanner(parent=bg)
        self.auth_banner.hide()
        self.auth_banner.switch_requested.connect(self._on_auth_switch_requested)
        self.auth_banner.dismiss_requested.connect(self._on_auth_dismiss)
        main.addWidget(self.auth_banner)

        self.tabs = QTabWidget()
        # 2026-05-08 редизайн Этап 2: иконки на табах УБРАНЫ (юзер сказал
        # «иконки в шапке не нужны»). Сам нативный QTabBar тоже скрыт —
        # переключение идёт через pill-кнопки в шапке (см. _build_header).
        # Контент-pane QTabWidget'а остаётся — переключение страниц
        # работает через `setCurrentIndex(idx)`.

        # NewEpisodeView создаём ДО _build_editor_tab() — он будет
        # встроен в content_stack Editor'а как 4-я страница (index=3),
        # активируется кнопкой «+» рядом с пилюлями эпизодов.
        self.new_episode_view = NewEpisodeView(self)
        self.tabs.addTab(self._build_editor_tab(), tr('tab_editor'))
        # Вкладка «Актёры» видна ВСЕМ юзерам — генерить character-референсы
        # может каждый. Но управлять оригиналами актёров — только админ.
        self.actors_view = ActorsView(self._project_root,
                                      status_bar=None,
                                      is_admin=self._is_admin)
        self.tabs.addTab(self.actors_view, tr('tab_actors'))
        self._actors_tab_idx = self.tabs.count() - 1  # для мигания
        self.tabs.addTab(self._build_settings_tab(), tr('tab_settings'))
        # 2026-06-20: страница «Генератор» — append в КОНЕЦ (idx 3), чтобы НЕ
        # двигать хардкод-индексы (Editor==0, Actors==1, Settings==2). В ряду
        # шапки pill «Генератор» рисуется ПЕРВЫМ (см. _build_header, явный _tab_idx).
        self.generator_view = GeneratorPage(self)
        self.tabs.addTab(self.generator_view, tr('tab_generator'))
        if self._is_admin:
            self.camera_lab_view = CameraLabView(
                self._project_root,
                self,
                default_provider=image_provider_admin(),
                provider_changed=set_image_provider_admin,
                get_shot_clipboard=lambda: self._shot_clipboard,
                set_shot_clipboard=self._set_shot_clipboard_bytes,
            )
            self.tabs.addTab(self.camera_lab_view, tr('tab_camera_lab'))
            self._camera_lab_tab_idx = self.tabs.count() - 1
        # Скрываем нативный QTabBar — переключение через pill в шапке.
        try:
            self.tabs.tabBar().hide()
        except Exception:
            pass
        # Плавный fade-in при переключении табов + синхронизация active
        # pill-кнопки в шапке.
        self.tabs.currentChanged.connect(self._on_tab_changed_fade)
        self.tabs.currentChanged.connect(self._sync_header_tab_active)
        self.tabs.currentChanged.connect(self._on_main_tab_changed)
        main.addWidget(self.tabs, stretch=1)

        # Статус-бар (вариант B): пустой когда нечего показать
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        # Подвязываем status_bar к ActorsView (для уведомлений «Загружено N фото»)
        self.actors_view.status_bar = self.status_bar
        # Авто-очистка статус-бара через 8с после последнего сообщения.
        # Раньше «✓ SHOT 2 в [ep20_block_2] обновлён» висел постоянно после
        # успешной регенерации — юзер уже посмотрел шот, а сообщение мозолит
        # глаза. Теперь оно само исчезнет через 8с (если юзер не сменил
        # блок/refs/chat — там очистка сразу через _clear_status_now).
        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.setInterval(8000)
        self._status_clear_timer.timeout.connect(self.status_bar.clearMessage)
        self.status_bar.messageChanged.connect(self._on_status_msg_changed)

        # 2026-05-06: периодическая проверка AI-аккаунта CLI.
        # Сценарий: юзер сидит в Studio весь день. В фоне у него закончился
        # лимит / он сделал logout в другом инструменте / переключил аккаунт.
        # Studio через ≤90с заметит и покажет AuthBanner с одной кнопкой
        # «Войти в другой аккаунт».
        # 2026-05-09: атрибуты `_last_known_auth_email/_loggedin/_dismissed_email/_switch_thread`
        # инициализируются в самом начале __init__ (см. выше) — Settings tab
        # билдится раньше этой точки и читает их через `_refresh_claude_account_email`.
        # Первый замер при старте Studio (синхронный, 1с).
        # 2026-05-08: убран периодический QTimer (90s) — на Win показывал
        # чёрное окно cmd при каждом claude auth status. Теперь проверка
        # лениво: один раз при старте (выше) + при переключении на вкладку
        # Settings (см. _on_main_tab_changed) + pre-flight перед запуском
        # эпизода.
        self._auth_check_tick(initial=True)

    def _on_status_msg_changed(self, msg: str):
        """Перезапускает авто-очистку каждый раз когда показывается новое
        непустое сообщение. Если сообщение пустое (clear) — таймер не нужен."""
        if msg:
            self._status_clear_timer.start()
        else:
            self._status_clear_timer.stop()

    def _clear_status_now(self):
        """Немедленно очищает статус-бар — вызывается при действиях юзера
        (смена блока/refs/chat/таба) которые означают «я уже увидел»."""
        try:
            self.status_bar.clearMessage()
        except Exception:
            pass

    # ── 2026-05-06: AuthBanner — авто-детект смены AI-аккаунта ──

    def _auth_check_tick(self, initial: bool = False):
        """Тик периодической проверки AI-аккаунта CLI.

        Сравнивает текущий статус (`claude auth status`) с запомненным.
        Если изменился (юзер залогинился в другой аккаунт / разлогинился /
        исчерпал лимит и CLI потерял auth) — показывает AuthBanner.

        `initial=True` — первый замер при старте Studio: только запоминаем
        состояние, баннер не показываем (если уже разлогинен — покажем,
        чтобы юзер сразу залогинился; смены email тут не бывает).
        """
        try:
            data = claude_auth_status(timeout=8.0)
        except Exception:
            return
        cur_logged = bool(data.get("loggedIn"))
        cur_email: Optional[str] = data.get("email") if cur_logged else None

        # Запоминаем для следующего сравнения.
        prev_email = self._last_known_auth_email
        prev_logged = self._last_known_auth_loggedin
        self._last_known_auth_email = cur_email
        self._last_known_auth_loggedin = cur_logged
        # 2026-05-09: обновить лейбл в Settings → AI-АККАУНТ.
        self._refresh_claude_account_email()

        # Скрытие баннера если юзер залогинен и состояние не меняется.
        if initial:
            # При старте: если разлогинен — показать «войди»; иначе ничего.
            if not cur_logged:
                self._show_auth_banner('logged_out')
            return

        # Случай 1: разлогинен (или CLI пропал)
        if not cur_logged:
            if prev_logged:
                # Только что разлогинился — показываем сразу, игнорируем
                # _auth_dismissed_email (это другая ситуация).
                self._auth_dismissed_email = None
                self._show_auth_banner('logged_out')
            else:
                # Уже был разлогинен — баннер либо уже висит, либо юзер
                # его скрыл; не дёргаем.
                pass
            return

        # Случай 2: залогинен, email изменился
        if prev_email is not None and cur_email != prev_email:
            # Если юзер ранее скрыл баннер для этого нового email — не доставать.
            if self._auth_dismissed_email == cur_email:
                return
            self._show_auth_banner('changed', email=cur_email)
            return

        # Случай 3: всё стабильно — если баннер виден и состояние теперь ок,
        # тихо его скрываем (например юзер залогинился вне Studio).
        if self.auth_banner.isVisible():
            # AuthBanner показывался для logged_out / changed / quota. Если
            # сейчас logged_in и email совпадает с запомненным — скрываем.
            if cur_logged and cur_email and cur_email == prev_email:
                # Ничего не показываем — но если он висел в done-состоянии,
                # пусть дождётся клика «Скрыть».
                pass

    def _show_auth_banner(self, kind: str, email: Optional[str] = None):
        """Показать плашку нужного типа."""
        try:
            self.auth_banner.show_for(kind, email=email)
        except Exception as e:
            print(f"[auth_banner] failed to show: {e}")

    def _hide_auth_banner(self):
        try:
            self.auth_banner.hide()
        except Exception:
            pass

    def _refresh_claude_account_email(self):
        """Обновляет email-лейбл в Settings → секция «AI-АККАУНТ».

        Вызывается:
          • После постройки Settings tab (первая отрисовка через
            `_build_settings_tab` — это происходит РАНЬШЕ initial-tick'а
            `_auth_check_tick`, поэтому email на момент первого вызова
            ещё None — лейбл покажет «Не залогинен», правильное значение
            подставится через ~1с).
          • Из `_auth_check_tick` (initial-tick + при переключении на
            вкладку Settings).
          • Из `_on_auth_switch_done` (юзер сменил аккаунт через UI).
          • Из `_apply_lang` (смена языка).

        Защита: лейбл может не существовать (Settings tab ещё не построен).
        Атрибут `_last_known_auth_email` тоже может отсутствовать на
        ранних стадиях init — читаем через `getattr` defensively.
        """
        try:
            lbl = getattr(self, 'claude_acc_email_lbl', None)
            if lbl is None:
                return
            email = getattr(self, '_last_known_auth_email', None)
            lbl.setText(email or tr('ai_account_not_logged'))
        except Exception:
            traceback.print_exc()

    def _on_auth_dismiss(self):
        """Клик «✕ Скрыть» на плашке. Запоминаем текущий email чтобы не
        доставать юзера повторно тем же сообщением."""
        self._auth_dismissed_email = self._last_known_auth_email
        self._hide_auth_banner()

    def _on_auth_switch_requested(self):
        """Клик «🔄 Войти в другой аккаунт» — запускаем AuthSwitchThread."""
        cli = find_claude_cli()
        if not cli:
            self.auth_banner.show_failed()
            return
        # Защита от двойного клика — если поток уже идёт, ничего не делаем.
        if self._auth_switch_thread is not None:
            try:
                if self._auth_switch_thread.isRunning():
                    return
            except RuntimeError:
                self._auth_switch_thread = None

        from threads.auth_switch import AuthSwitchThread
        t = AuthSwitchThread(cli, parent=self)
        t.progress.connect(self._on_auth_switch_progress)
        t.finished_ok.connect(self._on_auth_switch_done)
        t.failed.connect(self._on_auth_switch_failed)
        # Авто-очистка ссылки когда поток умрёт
        t.finished.connect(lambda: setattr(self, '_auth_switch_thread', None))
        self._auth_switch_thread = t
        # UI: переходим в progress-состояние
        self.auth_banner.show_progress()
        t.start()

    def _on_auth_switch_progress(self, stage: str):
        """Тики прогресса от AuthSwitchThread (не используются для UI пока —
        баннер уже в progress-state). Зарезервировано для статус-бара."""
        if stage == "opening_terminal":
            try:
                self.status_bar.showMessage(tr('auth_terminal_hint'), 8000)
            except Exception:
                pass

    def _on_auth_switch_done(self, email: str):
        """OAuth завершён, CLI на новом аккаунте."""
        self._auth_dismissed_email = None
        # Обновим запомненный email сразу (чтобы next tick не воспринял это
        # как «снова сменилось»).
        self._last_known_auth_email = email
        self._last_known_auth_loggedin = True
        try:
            self.auth_banner.show_done(email)
        except Exception:
            pass
        # 2026-05-09: обновить лейбл в Settings → AI-АККАУНТ.
        self._refresh_claude_account_email()

    def showEvent(self, event):
        """Triggered Qt'ом при первом show() окна.

        2026-05-09: используется чтобы показать update-failed popup ПОСЛЕ
        того как окно полностью отрисовалось (на slow Win-машинах
        фиксированный QTimer.singleShot(500ms) из __init__ не гарантировал
        что окно уже видимо). One-shot guard — `_first_show_done`. 200мс
        даёт frame painter завершиться.
        """
        super().showEvent(event)
        if not getattr(self, '_first_show_done', False):
            self._first_show_done = True
            info = getattr(self, '_update_failure_info', None)
            if info is not None:
                # 2026-05-11 (v1.0.44): диспатч по state[0]:
                #   'update_failed'           → popup со ссылкой на Installer
                #   'reboot_pending'          → inline баннер «нужна перезагрузка»
                #   'reboot_install_success'  → toast «обновлено»
                state = info[0] if info else None
                if state == 'reboot_pending':
                    QTimer.singleShot(200, self._show_reboot_pending_banner)
                elif state == 'reboot_install_success':
                    QTimer.singleShot(200, self._show_install_success_toast)
                else:
                    QTimer.singleShot(200, self._show_update_failed_dialog)

    def _show_update_failed_dialog(self):
        """Popup при старте если предыдущий auto-update упал на середине.

        Triggered: `finalize_pending_update` вернул кортеж с state='update_failed'.
        Действия: показать QMessageBox с кликабельной ссылкой на Installer
        и (если есть) на bootstrap.log. После клика OK — popup исчезает.

        2026-05-11 (v1.0.44): info теперь 4-tuple (state, version, log, scheduled_at),
        разворачиваем индексы 1 (version) и 2 (log).
        """
        info = getattr(self, '_update_failure_info', None)
        if not info:
            return
        self._update_failure_info = None  # show only once
        # info = (state, target_version, log_path, scheduled_at)
        attempted_version = info[1]
        log_path = info[2]

        msg = QMessageBox(self)
        msg.setWindowTitle(tr('update_failed_title'))
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(tr('update_failed_text').format(version=attempted_version))

        installer_url = (
            "https://github.com/Akex24/storyboard-automation/releases/download/"
            f"app-v{attempted_version}/Storyboard.Studio.Installer.v{attempted_version}.exe"
        )
        body_html = tr('update_failed_body').format(
            version=attempted_version,
            installer_url=installer_url,
        )
        if log_path and log_path.exists():
            log_url = log_path.as_uri()
            body_html += "<br><br>" + tr('update_failed_log').format(
                log_url=log_url, log_path=str(log_path))
        msg.setInformativeText(body_html)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        try:
            msg.exec()
        except Exception:
            traceback.print_exc()

    def _show_reboot_pending_banner(self):
        """2026-05-11 (v1.0.44): inline-баннер «нужна перезагрузка» если
        прошлый bootstrap'у пришлось эскалировать в reboot-defer fallback
        (RM API + MoveFileEx). Триггер: `finalize_pending_update` вернул
        кортеж с state='reboot_pending'. info=(state, target_ver, None,
        scheduled_at_unix).

        UX: появляется НЕ-блокирующим баннером в шапке (после auth-баннера).
        Кнопка «Понятно» скрывает баннер до следующего запуска. Если
        прошло >7 дней с момента scheduled_at — текст становится более
        настойчивым (включает версию)."""
        info = getattr(self, '_update_failure_info', None)
        if not info or info[0] != 'reboot_pending':
            return
        self._update_failure_info = None
        target_version = info[1]
        scheduled_at_unix = info[3]

        days_passed = 0
        if scheduled_at_unix is not None:
            try:
                days_passed = (time.time() - scheduled_at_unix) / 86400.0
            except Exception:
                pass
        if days_passed >= 7:
            text = tr('update_pending_reboot_urgent').format(
                version=target_version)
        else:
            text = tr('update_pending_reboot_short')
        try:
            self._build_and_show_reboot_banner(text)
        except Exception:
            traceback.print_exc()

    def _build_and_show_reboot_banner(self, text: str):
        """Создаёт inline-баннер «нужна перезагрузка» и вставляет в layout
        рядом с auth-баннером. Скрыть — кнопка «Понятно» (баннер удаляется
        из layout). На следующем запуске Studio баннер появится снова
        пока `pending_reboot.txt` существует И boot_time < scheduled_at."""
        from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton
        banner = QFrame(self)
        banner.setObjectName("reboot-banner")
        banner.setStyleSheet(
            "QFrame#reboot-banner { background: #2c2240; "
            "border: 1px solid #4a3a6a; border-radius: 8px; padding: 6px; }"
            "QLabel { color: #e0d0ff; font-size: 13px; }"
            "QPushButton { background: #3a2d55; color: #fff; "
            "border: 1px solid #5a4a78; border-radius: 6px; padding: 6px 14px; }"
            "QPushButton:hover { background: #4a3a6a; }"
        )
        lay = QHBoxLayout(banner)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)
        label = QLabel(f"🔄  {text}")
        label.setWordWrap(True)
        dismiss = QPushButton(tr('update_pending_reboot_dismiss'))
        dismiss.clicked.connect(lambda: self._dismiss_reboot_banner(banner))
        lay.addWidget(label, 1)
        lay.addWidget(dismiss, 0)
        # Вставляем в main layout сразу после auth-banner если он есть,
        # иначе в начало content area.
        try:
            ab = getattr(self, 'auth_banner', None)
            if ab is not None and ab.parent() is not None:
                parent_layout = ab.parent().layout()
                if parent_layout is not None:
                    idx = parent_layout.indexOf(ab)
                    if idx >= 0:
                        parent_layout.insertWidget(idx + 1, banner)
                        self._reboot_banner = banner
                        return
        except Exception:
            traceback.print_exc()
        # Fallback: добавляем в центральный widget.
        try:
            cw = self.centralWidget()
            if cw is not None and cw.layout() is not None:
                cw.layout().insertWidget(0, banner)
                self._reboot_banner = banner
        except Exception:
            traceback.print_exc()

    def _dismiss_reboot_banner(self, banner):
        """Скрыть reboot-баннер до следующего запуска Studio. НЕ удаляем
        `pending_reboot.txt` — при следующем старте баннер появится снова
        (если reboot всё ещё не произошёл)."""
        try:
            banner.setParent(None)
            banner.deleteLater()
        except Exception:
            traceback.print_exc()

    def _show_install_success_toast(self):
        """2026-05-11 (v1.0.44): short notification что reboot произошёл и
        deferred install был применён Windows'ом до user logon. Триггер:
        `finalize_pending_update` вернул state='reboot_install_success' —
        version.json уже забампан, markers удалены. Юзеру просто покажем
        тостер «обновлено до vX.Y.Z» на 5 секунд."""
        info = getattr(self, '_update_failure_info', None)
        if not info or info[0] != 'reboot_install_success':
            return
        self._update_failure_info = None
        target_version = info[1]
        try:
            from PyQt6.QtWidgets import QLabel
            from PyQt6.QtCore import Qt as _Qt
            toast = QLabel(self)
            toast.setText(
                tr('update_install_success_toast').format(version=target_version))
            toast.setStyleSheet(
                "background: #2d4a2d; color: #d0ffd0; "
                "border: 1px solid #4a6e4a; border-radius: 8px; "
                "padding: 10px 18px; font-size: 13px;")
            toast.setWindowFlags(
                _Qt.WindowType.ToolTip | _Qt.WindowType.FramelessWindowHint)
            toast.adjustSize()
            # Позиционируем по центру-снизу окна.
            geo = self.geometry()
            toast.move(
                geo.x() + (geo.width() - toast.width()) // 2,
                geo.y() + geo.height() - toast.height() - 80)
            toast.show()
            QTimer.singleShot(5000, toast.deleteLater)
        except Exception:
            traceback.print_exc()

    def _on_auth_switch_failed(self, reason: str):
        """OAuth не прошёл за 5 мин или произошла ошибка.

        Reason может быть:
          • "same_account:<email>" — юзер случайно залогинился в тот же
            аккаунт. Показываем специальный текст «выбери ДРУГОЙ».
          • "timeout" / "cancelled" / прочее — общая ошибка.
        """
        try:
            if reason.startswith("same_account:"):
                email = reason.split(":", 1)[1].strip()
                # Возвращаем _last_known_auth_email на этот же email чтобы
                # 90с таймер не показал «changed» (это не смена, это тот же).
                self._last_known_auth_email = email
                self._last_known_auth_loggedin = True
                self.auth_banner.show_same_account(email)
            else:
                self.auth_banner.show_failed()
        except Exception:
            pass

    def _build_header(self) -> QWidget:
        # 2026-05-08 редизайн Этап 2: шапка стала карточкой LUMZ-стиля
        # с лого слева и pill-группой табов справа.
        # Внешний контейнер с margins — чтобы карточка была с отступом от
        # краёв окна (а не вплотную). Внутренний QFrame#header-card —
        # сама карточка с фоном/border/radius.
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(14, 14, 14, 0)
        outer_lay.setSpacing(0)

        h = QFrame()
        h.setObjectName("header-card")
        h.setFixedHeight(58)
        outer_lay.addWidget(h)

        lay = QHBoxLayout(h)
        lay.setContentsMargins(18, 8, 18, 8)
        lay.setSpacing(0)

        # LUMZ + красный квадрат-точка возле буквы Z + Storyboard Studio.
        # Логотип КЛИКАБЕЛЕН: клик → открывает https://lumz.ai/ в браузере.
        # Юзер: «логотип LUMZ оставить как сейчас» — текст и стиль не трогаем.
        logo = QLabel(
            '<span style="color:#fff; font-size:20px; font-weight:700; letter-spacing:1px;">LUMZ</span>'
            '<span style="color:#e63946; font-size:20px; font-weight:900;">▪</span>'
            '<span style="color:#888; font-size:14px;">  Storyboard Studio</span>'
        )
        logo.setTextFormat(Qt.TextFormat.RichText)
        logo.setObjectName("logo-text")
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        def _open_lumz(_ev):
            try:
                webbrowser.open("https://lumz.ai/")
            except Exception:
                pass
        logo.mousePressEvent = _open_lumz  # type: ignore
        lay.addWidget(logo, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Этап 2 (формат кадра): сегмент-переключатель 9:16 | 16:9 вплотную
        # справа от логотипа. Активная половина — фирменный красный. Клик по
        # неактивной → подтверждение → запись meta.aspect + просьба перезапустить.
        # Логику генерации/UI НЕ трогает (Этап 3+).
        self._aspect_seg_buttons: Dict[str, QPushButton] = {}
        aspect_seg = QFrame()
        aspect_seg.setObjectName("aspect-seg")
        aspect_seg.setFixedHeight(34)
        as_lay = QHBoxLayout(aspect_seg)
        as_lay.setContentsMargins(3, 3, 3, 3)
        as_lay.setSpacing(0)
        for _code in ("9:16", "16:9"):
            _b = QPushButton(_code)
            _b.setObjectName("aspect-seg-btn")
            _b.setFixedHeight(28)
            _b.setCursor(Qt.CursorShape.PointingHandCursor)
            _b.setProperty("active", False)
            _b.clicked.connect(
                lambda _checked=False, c=_code: self._on_aspect_seg_clicked(c))
            self._aspect_seg_buttons[_code] = _b
            as_lay.addWidget(_b)
        lay.addSpacing(12)
        lay.addWidget(aspect_seg, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._refresh_aspect_segment()

        lay.addStretch()

        # Pill-группа табов верхней навигации.
        # Кнопки переключают `self.tabs.setCurrentIndex(idx)`.
        # Сам QTabBar QTabWidget'а скрыт (см. _build_ui ниже).
        # БЕЗ ИКОНОК — юзер: «иконки на табах не нужны».
        self._header_tab_buttons: List[QPushButton] = []
        tabs_group = QFrame()
        tabs_group.setObjectName("header-tabs")
        tabs_group.setFixedHeight(34)
        tg_lay = QHBoxLayout(tabs_group)
        tg_lay.setContentsMargins(3, 3, 3, 3)
        tg_lay.setSpacing(0)
        # 2026-06-20: ЯВНЫЙ маппинг pill→страница по _tab_idx (НЕ позиционный).
        # Визуальный порядок ряда ≠ порядок страниц: «Генератор» рисуется слева,
        # но его страница — idx 3 (append в addTab, чтобы не двигать хардкоды
        # Editor==0/Settings==2). page_idx → _tab_idx; по нему же
        # _sync_header_tab_active подсвечивает активную.
        tab_items = [(3, 'tab_generator'), (0, 'tab_editor'),
                     (1, 'tab_actors'), (2, 'tab_settings')]
        camera_idx = getattr(self, '_camera_lab_tab_idx', None)
        if getattr(self, '_is_admin', False) and camera_idx is not None:
            tab_items.insert(0, (camera_idx, 'tab_camera_lab'))
        for page_idx, key in tab_items:
            btn = QPushButton(tr(key))
            btn.setObjectName("tab-pill")
            btn.setFixedHeight(28)
            btn.setProperty("active", page_idx == 0)
            if key == 'tab_generator':
                btn.setProperty("accent", True)   # янтарная акцентная pill
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("_tab_idx", page_idx)
            btn.setProperty("_i18n_key", key)
            btn.clicked.connect(
                lambda _checked=False, i=page_idx: self._on_header_tab_clicked(i))
            self._header_tab_buttons.append(btn)
            tg_lay.addWidget(btn)
        lay.addWidget(tabs_group, alignment=Qt.AlignmentFlag.AlignVCenter)

        # 2026-05-08 редизайн: 14 → 24 (юзер просил +10px между группой
        # табов и переключателем языка для визуального дыхания).
        lay.addSpacing(24)

        # Переключатель языка интерфейса.
        # 2026-05-08: lang-btn обёрнута в такую же `header-tabs` группу
        # как и табы Editor/Actors/Settings — это гарантирует что обе
        # пилюли (внутри своих обёрток) занимают одинаковое вертикальное
        # положение в общей строке шапки. Без обёртки lang-btn сидела
        # на 1-2px выше/ниже из-за отсутствия border+padding контейнера
        # и перекоса при AlignVCenter.
        lang_wrapper = QFrame()
        # 2026-05-08: своё objectName "lang-wrapper" — невидимая обёртка
        # (transparent фон+border), но с теми же geometric параметрами что
        # `header-tabs` (padding 3+3 + 1px border). Это сохраняет позицию
        # lang-btn на одной линии с tab-pill, при этом визуально обёртки
        # не видно — выглядит как одиночная пилюля рядом с группой табов.
        lang_wrapper.setObjectName("lang-wrapper")
        lw_lay = QHBoxLayout(lang_wrapper)
        lw_lay.setContentsMargins(3, 3, 3, 3)
        lw_lay.setSpacing(0)

        self.lang_btn = QPushButton()
        self.lang_btn.setObjectName("lang-btn")
        self.lang_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lang_menu = QMenu(self.lang_btn)
        self._lang_menu.setObjectName("lang-menu")
        for code, label, full_name in SUPPORTED_LANGUAGES:
            flag = label.split(" ", 1)[0]
            act = self._lang_menu.addAction(f"  {flag}   {full_name}")
            act.triggered.connect(lambda _checked=False, c=code: self._set_lang(c))
        self.lang_btn.clicked.connect(self._open_lang_menu)
        self._refresh_lang_btn()
        lw_lay.addWidget(self.lang_btn)
        lay.addWidget(lang_wrapper, alignment=Qt.AlignmentFlag.AlignVCenter)

        lay.addSpacing(10)

        self.header_version = QLabel(f"v{read_local_app_version(self._project_root)}")
        self.header_version.setObjectName("header-version")
        lay.addWidget(self.header_version, alignment=Qt.AlignmentFlag.AlignVCenter)
        return outer

    def _on_header_tab_clicked(self, idx: int):
        """Pill-кнопка таба в шапке кликнута → переключаем QTabWidget.
        Active-стиль pill обновится в `_sync_header_tab_active` через
        `currentChanged` сигнал."""
        try:
            self.tabs.setCurrentIndex(idx)
        except Exception:
            pass

    def _sync_header_tab_active(self, idx: int):
        """Синхронизирует active-флаг pill-кнопок в шапке с активным
        индексом QTabWidget. Вызывается из `currentChanged`."""
        btns = getattr(self, '_header_tab_buttons', None)
        if not btns:
            return
        # 2026-06-20: сравнение по сохранённому _tab_idx кнопки, НЕ по позиции
        # в ряду — визуальный порядок pill (Генератор первым) ≠ порядок страниц.
        for btn in btns:
            btn.setProperty("active", btn.property("_tab_idx") == idx)
            # Перезапускаем styling — Qt не подхватывает property changes
            # без явного unpolish/polish.
            try:
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            except Exception:
                pass

    def _on_main_tab_changed(self, idx: int):
        """Обновляет ленивые проверки при переключении на вкладку Settings.

        2026-05-08: ранее эти проверки крутились в QTimer'ах (5с git status,
        90с claude auth status) и на Win показывали чёрные cmd-окна каждый
        тик. Теперь — лениво, только когда юзер реально открыл Settings и
        может увидеть результат:
          • `_refresh_send_button` (для админа) — обновить состояние
            кнопки «📤 Отправить обновление».
          • `_auth_check_tick` — пересчитать статус AI-аккаунта чтобы
            AuthBanner отрисовался актуально.

        Settings tab — индекс 2 (Editor=0, Actors=1, Settings=2).
        """
        if idx != 2:
            # 2026-06-10: ушли с Settings → гасим таймер статусов ключей.
            try:
                self._key_status_timer.stop()
            except Exception:
                pass
            return
        try:
            if getattr(self, '_is_admin', False):
                self._refresh_send_button()
        except Exception:
            pass
        try:
            self._auth_check_tick()
        except Exception:
            pass
        # 2026-06-10 (этап 2): обновить статусы ключей + завести таймер countdown
        # (тикает только пока открыта вкладка Settings).
        try:
            self._refresh_key_status_indicators()
            self._key_status_timer.start()
        except Exception:
            pass
        # 2026-06-10 (стабильность блока ключей): пересчитать резерв высоты
        # лейбла итога под реальную ширину (вкладка Settings теперь видима).
        try:
            self._reserve_server_check_lbl_height()
        except Exception:
            pass

    def _refresh_lang_btn(self):
        """Обновляет текст кнопки языка под текущий выбор: «РУС ▾».

        2026-05-08 редизайн: флаг-эмодзи убран из текста кнопки. Apple
        Color Emoji имеет baseline ниже чем SF Pro Display → вместе с
        латиницей выглядел вертикально съехавшим относительно tab-pill
        («Editor»/«Actors»/«Settings»). С чистой латиницей кнопка
        идеально центрируется на одной линии. Флаги остались в
        выпадающем меню (`_lang_menu`) — там они работают нормально.
        """
        cur = get_lang()
        for code, label, _full in SUPPORTED_LANGUAGES:
            if code == cur:
                # `label` имеет формат «🇷🇺 РУС» — берём часть после
                # первого пробела (текстовый код языка).
                parts = label.split(" ", 1)
                text_only = parts[1] if len(parts) > 1 else label
                self.lang_btn.setText(f"{text_only} ▾")
                return
        self.lang_btn.setText("▾")

    def _open_lang_menu(self):
        """Открывает кастомный QMenu со списком языков под кнопкой."""
        pos = self.lang_btn.mapToGlobal(QPoint(0, self.lang_btn.height() + 4))
        self._lang_menu.setMinimumWidth(self.lang_btn.width() + 60)
        self._lang_menu.exec(pos)

    def _refresh_aspect_segment(self):
        """Подсветка сегмента формата под self._current_aspect (активная
        половина — красная). Нет активного сериала → сегмент выключен (no-op
        при клике). Перекраска через unpolish/polish (как у tab-pill)."""
        cur = getattr(self, '_current_aspect', '9:16')
        has_show = bool(getattr(self, '_current_show', None))
        for code, btn in getattr(self, '_aspect_seg_buttons', {}).items():
            btn.setProperty("active", bool(has_show and code == cur))
            btn.setEnabled(has_show)
            try:
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            except Exception:
                pass

    def _on_aspect_seg_clicked(self, code: str):
        """Клик по половине сегмента формата. По неактивной → подтверждение
        «Применить/Отменить»; ТОЛЬКО при «Применить» пишем meta.aspect активного
        сериала + просим перезапуск. «Отменить» → полный откат, ничего не пишем
        (подсветка остаётся на старом формате). Логику генерации НЕ трогает."""
        if not getattr(self, '_current_show', None):
            return  # нет активного сериала — писать некуда
        if code == getattr(self, '_current_aspect', '9:16'):
            return  # уже активный формат
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr('aspect_switch_title'))
        box.setText(tr('aspect_switch_confirm', fmt=code))
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Ok).setText(tr('aspect_switch_apply'))
        box.button(QMessageBox.StandardButton.Cancel).setText(
            tr('aspect_switch_cancel'))
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return  # Отмена — полный откат, ничего не трогаем
        # read-modify-write meta.aspect (save_show_meta дампит весь dict →
        # остальные поля сохраняются).
        saved = False
        try:
            meta = show_manager.load_show_meta(
                self._project_root, self._current_show)
            meta["aspect"] = code
            show_manager.save_show_meta(
                self._project_root, self._current_show, meta)
            self._current_aspect = code
            saved = True
        except Exception:
            traceback.print_exc()
        if saved:
            # Формат уже записан на диск (save_show_meta синхронна) ДО закрытия.
            # Закрываемся ШТАТНО через self.close() → closeEvent →
            # _graceful_shutdown_all_threads (корректно гасит треды, ничего не
            # виснет). Откладываем на след. тик event-loop'а (как остальной код
            # делает для quit) чтобы текущий клик-хендлер завершился чисто. Юзер
            # открывает Studio заново — стартует в новом формате.
            QTimer.singleShot(0, self.close)
        else:
            # Запись упала → НЕ закрываемся (иначе откроется со старым форматом),
            # возвращаем подсветку сегмента на текущий формат.
            self._refresh_aspect_segment()

    def _set_lang(self, code: str):
        """Применяет выбранный язык: сохраняет в QSettings + перерисовывает UI."""
        if code == get_lang():
            return
        set_lang(code)
        self._refresh_lang_btn()
        self._apply_translations()

    def _apply_translations(self):
        """Применяет текущий язык ко всем UI-элементам без перезапуска."""
        # Tabs (Phase 1 + hotfix #2 2026-05-04: после удаления отдельной
        # вкладки «Новый эпизод» осталось 3 вкладки — индексы сдвинулись.
        # NewEpisodeView живёт внутри Editor'а через «+» pill, его
        # apply_lang всё равно зовём — он переводит свои внутренние строки).
        if hasattr(self, 'tabs'):
            self.tabs.setTabText(0, tr('tab_editor'))
            self.tabs.setTabText(1, tr('tab_actors'))
            self.tabs.setTabText(2, tr('tab_settings'))
            if self.tabs.count() > 3:
                self.tabs.setTabText(3, tr('tab_generator'))
            camera_idx = getattr(self, '_camera_lab_tab_idx', None)
            if camera_idx is not None and 0 <= camera_idx < self.tabs.count():
                self.tabs.setTabText(camera_idx, tr('tab_camera_lab'))
            # 2026-05-08: pill-кнопки табов в шапке тоже переводим. Они
            # хранят i18n-ключ в property `_i18n_key` (см. _build_header).
            for btn in getattr(self, '_header_tab_buttons', []):
                key = btn.property("_i18n_key")
                if key:
                    btn.setText(tr(key))
            self.actors_view.apply_lang()
            if hasattr(self, 'camera_lab_view'):
                self.camera_lab_view.apply_lang()
            if hasattr(self, 'new_episode_view'):
                self.new_episode_view.apply_lang()
        # 2026-05-06: AuthBanner — обновить тексты кнопок и баннера.
        if hasattr(self, 'auth_banner'):
            try:
                self.auth_banner.retranslate()
            except Exception:
                traceback.print_exc()
        # 2026-05-12 (v1.0.56): MontageCTA внутри EpisodeChatView — кнопки
        # start/retry/cancel ставили текст только в __init__/retranslate.
        # При смене языка через UI без перезапуска CTA-кнопки оставались
        # в исходном языке. retranslate() тут синхронизирует тексты; _render()
        # внутри MontageCTA дополнительно зовёт setText для активной кнопки
        # (defense-in-depth — на случай если retranslate миновали).
        ev = getattr(self, 'episode_chat_view', None)
        if ev is not None and hasattr(ev, '_montage_cta'):
            try:
                ev._montage_cta.retranslate()
            except Exception:
                traceback.print_exc()
        # 2026-05-05: refs-view (заголовки секций «ЛОКАЦИИ/ОБЪЕКТЫ/
        # ПЕРСОНАЖИ» + кнопка «+ Добавить персонажа») создаётся в
        # `_build_refs_view` через `tr(...)`. Сама вьюха не имеет
        # apply_lang — labels хранят свои текущие строки. Поэтому
        # пересобираем секцию целиком при смене языка.
        try:
            if (hasattr(self, '_build_refs_view')
                    and self._current_episode is not None
                    and hasattr(self, 'refs_layout')):
                self._build_refs_view(self._current_episode)
        except Exception:
            traceback.print_exc()
        # Editor tab
        if hasattr(self, 'show_lbl'):
            self.show_lbl.setText(tr('series'))
        # 2026-05-08: лейбл «Эпизод:» перед рядом пилюль.
        if hasattr(self, 'ep_label'):
            self.ep_label.setText(tr('episode'))
        if hasattr(self, 'save_btn'):
            self.save_btn.setText(tr('save_png'))
        if hasattr(self, 'new_show_btn'):
            self.new_show_btn.setToolTip(tr('new_show_btn_tooltip'))
        # scenario_drop_zone теперь внутри NewEpisodeView (см. _build).
        # NewEpisodeView.apply_lang() сам ретранслирует свою drop-зону.
        # Settings tab
        if hasattr(self, 'sec_project_lbl'):
            self.sec_project_lbl.setText(tr('sec_project'))
        if hasattr(self, 'sec_about_lbl'):
            self.sec_about_lbl.setText(tr('sec_about'))
        if hasattr(self, 'open_folder_btn'):
            self.open_folder_btn.setText(tr('open_folder'))
        if hasattr(self, 'open_log_btn'):
            self.open_log_btn.setText(tr('open_log_btn'))
            self.open_log_btn.setToolTip(tr('open_log_btn_tooltip'))
        # 2026-05-12 (v1.0.53): кнопка «🔄 Проверить обновления».
        if hasattr(self, 'check_updates_btn'):
            self.check_updates_btn.setText(tr('check_updates_btn'))
            self.check_updates_btn.setToolTip(tr('check_updates_btn_tooltip'))
        # AI-АККАУНТ — секция, лейбл «Текущий аккаунт:», кнопка «Сменить».
        # Email (claude_acc_email_lbl) обновляем через refresh — там либо
        # сам email (не переводится), либо tr('ai_account_not_logged').
        if hasattr(self, 'sec_ai_account_lbl'):
            self.sec_ai_account_lbl.setText(tr('sec_ai_account'))
        if hasattr(self, 'claude_acc_key_lbl'):
            self.claude_acc_key_lbl.setText(tr('ai_account_current'))
        if hasattr(self, 'claude_acc_switch_btn'):
            self.claude_acc_switch_btn.setText(tr('ai_account_switch_btn'))
        self._refresh_claude_account_email()
        # API-ключ — секция, подсказка, плейсхолдер, обе кнопки
        if hasattr(self, 'sec_apikey_lbl'):
            try:
                self.sec_apikey_lbl.setText(tr('sec_apikey'))
                self.apikey_hint_lbl.setText(tr('apikey_hint'))
                self.apikey_input.setPlaceholderText(tr('apikey_placeholder'))
                self.apikey_save_btn.setText(tr('apikey_save'))
                # Кнопка «Показать»/«Скрыть» зависит от текущего checked-состояния
                if self.apikey_show_btn.isChecked():
                    self.apikey_show_btn.setText(tr('apikey_hide'))
                else:
                    self.apikey_show_btn.setText(tr('apikey_show'))
                # 2026-06-10 (очередь #1): кнопки проверки сервера. Базовый
                # текст кнопки НЕ трогаем пока идёт тест (там «проверяю… Nс»).
                if hasattr(self, 'server_check_btn'):
                    _sct = getattr(self, '_server_check_thread', None)
                    if _sct is None or not _sct.isRunning():
                        self.server_check_btn.setText(tr('server_check_btn'))
                    self.server_check_btn.setToolTip(tr('server_check_tip'))
                if hasattr(self, 'server_support_btn'):
                    self.server_support_btn.setText(tr('server_support_btn'))
                # 2026-06-10 (фикс перевода на лету в области ключей): чекбоксы
                # «use» (все 5), плейсхолдеры всех полей ключей, перерисовка
                # статус-лейблов. server_check_result_lbl НЕ трогаем — там может
                # висеть итог теста с форматированными данными.
                for _tg in getattr(self, '_apikey_toggles', []) or []:
                    _tg.setText(tr('key_use_toggle'))
                for _fl in getattr(self, '_apikey_fields', []) or []:
                    _fl.setPlaceholderText(tr('apikey_placeholder'))
                self._refresh_key_status_indicators()
            except Exception:
                traceback.print_exc()
        # v1.0.65: секция «🌐 ПРОКСИ-СЕРВЕР» — retranslate всех меток.
        if hasattr(self, 'sec_proxy_lbl'):
            try:
                self.sec_proxy_lbl.setText(tr('sec_proxy'))
                self.proxy_use_chk.setText(tr('proxy_use_checkbox'))
                self.proxy_host_label.setText(tr('proxy_host_label'))
                self.proxy_port_label.setText(tr('proxy_port_label'))
                self.proxy_username_label.setText(tr('proxy_username_label'))
                self.proxy_password_label.setText(tr('proxy_password_label'))
                self.proxy_host_input.setPlaceholderText(
                    tr('proxy_host_placeholder'))
                self.proxy_port_input.setPlaceholderText(
                    tr('proxy_port_placeholder'))
                self.proxy_username_input.setPlaceholderText(
                    tr('proxy_username_placeholder'))
                self.proxy_test_btn.setText(tr('proxy_test_button'))
                self.proxy_save_btn.setText(tr('proxy_save_button'))
                self.proxy_restart_hint_lbl.setText(tr('proxy_restart_hint'))
                # proxy_result_lbl не трогаем — там результат теста,
                # язык применится при следующем клике «Проверить».
            except Exception:
                traceback.print_exc()
        # v1.0.61: секция «🎬 МОНТАЖНАЯ КАРТА» (видна всем, не админ-only).
        if hasattr(self, 'sec_montage_lbl'):
            try:
                self.sec_montage_lbl.setText(tr('sec_montage'))
                self.context_reviewer_chk.setText(
                    tr('settings_context_reviewer_label'))
                self.context_reviewer_hint_lbl.setText(
                    tr('settings_context_reviewer_hint'))
            except Exception:
                traceback.print_exc()
        # Секция «Провайдеры генерации» (один заголовок + две карточки, видна всем)
        if hasattr(self, 'sec_image_providers_lbl'):
            try:
                self.sec_image_providers_lbl.setText(
                    tr('sec_image_providers'))
                self.image_provider_actors_card_title_lbl.setText(
                    tr('image_provider_actors_card_title'))
                self.image_provider_actors_hint_lbl.setText(
                    tr('image_provider_actors_hint'))
                self.image_provider_actors_toggle.set_labels(
                    [tr('image_provider_narwhal'), tr('image_provider_nb2_lite'),
                     tr('image_provider_openai')])
                self.image_provider_admin_card_title_lbl.setText(
                    tr('image_provider_admin_card_title'))
                self.image_provider_admin_hint_lbl.setText(
                    tr('image_provider_admin_hint'))
                self.image_provider_admin_toggle.set_labels(
                    [tr('image_provider_narwhal'), tr('image_provider_nb2_lite'),
                     tr('image_provider_openai')])
            except Exception:
                traceback.print_exc()
        # Секция «Режим монтажной карты» (2026-06-16, Коммит 4: чиним retranslate
        # зоны режима — раньше не переводилась). Подписи сегментов «Mode A/B/C/D» —
        # литералы, не переводятся.
        if hasattr(self, 'sec_montage_mode_lbl'):
            try:
                self.sec_montage_mode_lbl.setText(tr('sec_montage_mode'))
                self.montage_mode_hint_lbl.setText(tr('montage_mode_hint'))
            except Exception:
                traceback.print_exc()
        # Секция «Скорость речи актёров» (только режим B)
        if hasattr(self, 'sec_speech_speed_b_lbl'):
            try:
                self.sec_speech_speed_b_lbl.setText(tr('sec_speech_speed_b'))
                self.speech_speed_b_hint_lbl.setText(tr('speech_speed_b_hint'))
                self.speech_speed_b_fast_label_lbl.setText(
                    tr('speech_speed_b_fast_label'))
                self.speech_speed_b_normal_label_lbl.setText(
                    tr('speech_speed_b_normal_label'))
                self.speech_speed_b_slow_label_lbl.setText(
                    tr('speech_speed_b_slow_label'))
                self.speech_speed_b_fast_reset_btn.setText(
                    tr('speech_speed_b_reset'))
                self.speech_speed_b_normal_reset_btn.setText(
                    tr('speech_speed_b_reset'))
                self.speech_speed_b_slow_reset_btn.setText(
                    tr('speech_speed_b_reset'))
                self._refresh_speech_speed_b_fast_value()
                self._refresh_speech_speed_b_normal_value()
                self._refresh_speech_speed_b_slow_value()
            except Exception:
                traceback.print_exc()
        # Админ-разделитель + секция «АНИМАЦИИ» (только если админ)
        if hasattr(self, 'sec_admin_div_lbl'):
            try:
                self.sec_admin_div_lbl.setText(tr('sec_admin_divider'))
                self.sec_anim_lbl.setText(tr('sec_anim'))
                self.anim_hint_lbl.setText(tr('anim_speed_hint'))
                self.anim_speed_label_lbl.setText(tr('anim_speed_label'))
                self.anim_speed_reset_btn.setText(tr('anim_speed_reset'))
                self._refresh_anim_speed_value()
            except Exception:
                traceback.print_exc()
        if hasattr(self, 'send_update_title_lbl'):
            self.send_update_title_lbl.setText(tr('send_update_title'))
        if hasattr(self, 'send_update_desc_lbl'):
            self.send_update_desc_lbl.setText(tr('send_update_desc'))
        if hasattr(self, 'send_update_btn'):
            self.send_update_btn.setText(tr('send_update_btn'))
        # Versions row labels (включают ключи: app_version, project_version)
        self._refresh_settings_versions()
        # Карточки шотов (overlay-кнопки)
        for card in getattr(self, 'shot_cards', []):
            card.apply_lang()
        # Чат эпизода
        ev = getattr(self, 'episode_chat_view', None)
        if ev is not None:
            try:
                ev.apply_lang()
            except Exception:
                pass
        # Пилюля «ЧАТ» (переводится при перезаполнении блок-пилюль через _populate_episodes,
        # но если она уже создана — обновим текст вручную)
        if hasattr(self, '_chat_pill') and self._chat_pill is not None:
            try:
                self._chat_pill.setText(tr('chat_pill'))
            except Exception:
                pass
        if hasattr(self, '_refs_pill') and self._refs_pill is not None:
            try:
                self._refresh_refs_pill_text()
            except Exception:
                pass
        self._refresh_compact_nav()
        # Кнопка «Удалить эпизод» — text НЕ устанавливаем (используем
        # SVG-иконку через setIcon, см. `_build_editor_tab`). Раньше
        # tr('delete_ep_btn') возвращал '🗑' и рисовался ПОВЕРХ SVG —
        # получалось две корзины рядом. 2026-05-08 hotfix.
        if hasattr(self, 'delete_ep_btn'):
            try:
                self.delete_ep_btn.setToolTip(tr('delete_ep_btn_tooltip'))
            except Exception:
                pass
        # v1.0.88 (Stage 17): block_refs_btn — перерисовка при смене языка
        # через UI. _display_block тоже зовёт setText, но если юзер на
        # refs/chat-view и переключает язык — block_refs_btn скрыта, и
        # без этого вызова текст обновится только при следующем кликe на
        # пилюлю блока.
        if hasattr(self, 'block_refs_btn'):
            self.block_refs_btn.setText(tr('block_refs_btn'))
        if hasattr(self, 'compile_ep_btn'):
            # Только если кнопка в покое — не затирать «⏳ Собираю серию…»
            # во время активного compile-потока.
            if getattr(self, '_compile_thread', None) is None:
                self.compile_ep_btn.setText(tr('compile_ep_btn'))
        # Перерисовать пилюли эпизодов и блоков (префикс «ЭП/ЕП/EP», «Блок/Block»)
        if hasattr(self, '_meta') and self._current_show:
            self._meta = read_episodes_meta(SHOW_ROOT)
        if hasattr(self, 'ep_pills_layout'):
            self._populate_episodes()  # пересоздаст пилюли + вызовет _select_episode → _populate_blocks → _display_block
        # Стат-метка скачиваний (если есть)
        if hasattr(self, 'stats_label') and self.stats_label.text() in (
                "загружаю статистику…", "завантажую статистику…", "loading stats…",
                "нет данных о скачиваниях", "немає даних про завантаження", "no download data"):
            self.stats_label.setText(tr('status_loading_stats'))

    def _wire_shot_card(self, card):
        """Подключение сигналов карточки шота — общее для обеих раскладок
        (ряд 9:16 и сетка 2×2 16:9), чтобы не дублировать connect'ы."""
        card.regen_requested.connect(self._on_regen)
        card.edit_requested.connect(self._on_edit_shot)
        # клик по картинке → попап ShotViewerDialog (превью + версии + edit/regen)
        card.image_clicked.connect(self._on_shot_image_clicked)
        # копирование активной картинки между шотами/блоками
        card.copy_requested.connect(self._on_copy_shot)
        card.paste_requested.connect(self._on_paste_shot)
        # удаление одного шота (со всеми версиями) — 3-я угловая кнопка
        card.delete_requested.connect(self._on_delete_shot)
        # перевод реплики (uk) через Haiku
        card.translate_requested.connect(self._on_translate_shot)

    def eventFilter(self, obj, ev):
        try:
            if (ev.type() == QEvent.Type.Resize
                    and getattr(self, '_current_aspect', None) == "16:9"
                    and hasattr(self, 'shots_scroll')
                    and obj is self.shots_scroll.viewport()):
                QTimer.singleShot(0, self._recalc_shot_cards_size)
        except Exception:
            traceback.print_exc()
        return super().eventFilter(obj, ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, '_current_aspect', None) == "16:9":
            QTimer.singleShot(0, self._recalc_shot_cards_size)

    def _recalc_shot_cards_size(self):
        """Adaptive 16:9 storyboard grid for large editor viewports only.

        Startup/small laptop windows keep the old fixed card size and horizontal
        scroll behavior. Once the storyboard viewport is tall enough, the 2x2
        grid stretches down toward Save storyboard without vertical scrolling.
        """
        if getattr(self, '_current_aspect', None) != "16:9":
            return
        cards = getattr(self, 'shot_cards', None) or []
        if not cards or not hasattr(self, 'shots_scroll'):
            return
        try:
            viewport = self.shots_scroll.viewport()
            viewport_w = viewport.width()
            viewport_h = viewport.height()
            if viewport_w <= 0 or viewport_h <= 0:
                return

            adaptive_min_h = 760
            if viewport_h < adaptive_min_h:
                for card in cards:
                    card.reset_image_size()
                return

            spacing = 12
            card_h = max(180, (viewport_h - spacing) // 2)
            # SHOT row + up to four compact description/dialog lines.
            # Larger reserve made adaptive cards leave visible empty tails below
            # short text blocks.
            text_h = 56
            image_h_by_height = max(90, card_h - 20 - 6 - text_h)
            # Fill the available two-column width in large/split-screen mode.
            # The previous extra 40px reserve left side gutters, so 16:9 images
            # were width-limited and could not grow taller even with less text.
            image_w_by_width = max(160, (viewport_w - spacing) // 2 - 20)
            image_h_by_width = int(image_w_by_width * 9 / 16)
            image_h = max(90, min(image_h_by_height, image_h_by_width))
            image_w = int(image_h * 16 / 9)

            for card in cards:
                card.apply_image_size(image_w, image_h, card_h)
        except Exception:
            traceback.print_exc()

    def _build_editor_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(14)
        lay.setContentsMargins(28, 14, 28, 14)

        # 2026-05-08 (Шаг B): синий баннер «Обновление проекта» удалён.
        # Раньше тут создавался update_banner + update_btn для скачивания
        # project zip — старый механизм где коллегам отдавался весь репо
        # включая instructions/ и agents/. Теперь обновляется только .exe
        # через app_update_banner ниже (DownloadAppUpdateThread + bootstrap).

        # Баннер обновления приложения
        self.app_update_banner = QFrame()
        self.app_update_banner.setObjectName("app-update-banner")
        self.app_update_banner.hide()
        aub_lay = QHBoxLayout(self.app_update_banner)
        aub_lay.setContentsMargins(14, 10, 10, 10)
        self.app_update_text = QLabel("")
        self.app_update_text.setObjectName("app-update-text")
        aub_lay.addWidget(self.app_update_text, stretch=1)
        self.app_update_btn = QPushButton("Скачать приложение →")
        self.app_update_btn.setObjectName("app-update-btn")
        self.app_update_btn.clicked.connect(self._download_app_update)
        aub_lay.addWidget(self.app_update_btn)
        lay.addWidget(self.app_update_banner)

        # 2026-06-24 Codex: компактная навигация редактора.
        # Старые строки пилюль остаются ниже как hidden/service widgets, чтобы
        # не ломать существующую логику выбора, индикаторов и hotfix'ов.
        self._compact_nav_enabled = True
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(8)
        self.compact_nav_layout = nav_row
        NAV_H = 34

        self.nav_show_label = QLabel(tr('series').rstrip(':'))
        self.nav_show_label.setObjectName("nav-label")
        nav_row.addWidget(self.nav_show_label)
        self.show_select_btn = QPushButton("")
        self.show_select_btn.setObjectName("nav-select")
        self.show_select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_select_btn.setMinimumWidth(190)
        self.show_select_btn.setFixedHeight(NAV_H)
        self.show_select_btn.clicked.connect(self._show_show_picker)
        nav_row.addWidget(self.show_select_btn)

        nav_row.addSpacing(18)
        self.nav_ep_label = QLabel(tr('episode').rstrip(':'))
        self.nav_ep_label.setObjectName("nav-label")
        nav_row.addWidget(self.nav_ep_label)
        self.ep_prev_btn = QPushButton("‹")
        self.ep_prev_btn.setObjectName("nav-arrow")
        self.ep_prev_btn.setFixedSize(24, 26)
        self.ep_prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ep_prev_btn.clicked.connect(lambda: self._step_episode(-1))
        nav_row.addWidget(self.ep_prev_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.ep_select_btn = QPushButton("")
        self.ep_select_btn.setObjectName("nav-select")
        self.ep_select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ep_select_btn.setFixedSize(58, NAV_H)
        self.ep_select_btn.clicked.connect(self._show_episode_picker)
        nav_row.addWidget(self.ep_select_btn)
        self.ep_next_btn = QPushButton("›")
        self.ep_next_btn.setObjectName("nav-arrow")
        self.ep_next_btn.setFixedSize(24, 26)
        self.ep_next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ep_next_btn.clicked.connect(lambda: self._step_episode(1))
        nav_row.addWidget(self.ep_next_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.nav_block_label = QLabel(tr('block'))
        self.nav_block_label.setObjectName("nav-label")
        self.nav_block_label.hide()
        self.block_prev_btn = QPushButton("‹")
        self.block_prev_btn.setObjectName("nav-arrow")
        self.block_prev_btn.setFixedSize(24, NAV_H)
        self.block_prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.block_prev_btn.clicked.connect(lambda: self._step_block(-1))
        self.block_prev_btn.hide()
        self.block_select_btn = QPushButton("")
        self.block_select_btn.setObjectName("nav-select")
        self.block_select_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.block_select_btn.setFixedSize(86, NAV_H)
        self.block_select_btn.clicked.connect(self._show_block_picker)
        self.block_select_btn.hide()
        self.block_next_btn = QPushButton("›")
        self.block_next_btn.setObjectName("nav-arrow")
        self.block_next_btn.setFixedSize(24, NAV_H)
        self.block_next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.block_next_btn.clicked.connect(lambda: self._step_block(1))
        self.block_next_btn.hide()

        self._refs_pill = QPushButton(tr('refs'))
        self._refs_pill.setObjectName("pill-refs")
        self._refs_pill.setFixedHeight(NAV_H)
        self._refs_pill.setProperty("active", False)
        self._refs_pill.setProperty("has_notice", False)
        self._refs_pill.setProperty("pulse_on", False)
        self._refs_pill.clicked.connect(self._show_refs_view)
        nav_row.addWidget(self._refs_pill)

        self._chat_pill = QPushButton(tr('chat_pill'))
        self._chat_pill.setObjectName("pill-chat")
        self._chat_pill.setFixedHeight(NAV_H)
        self._chat_pill.setProperty("active", False)
        self._chat_pill.clicked.connect(self._show_chat_view)
        nav_row.addWidget(self._chat_pill)

        nav_row.addStretch()
        lay.addLayout(nav_row)

        # Селектор сериала
        show_row = QHBoxLayout()
        show_row.setSpacing(10)
        self.show_lbl = QLabel(tr('series'))
        # 2026-05-08 редизайн: LUMZ text_secondary (rgba(255,255,255,0.55))
        self.show_lbl.setStyleSheet(
            "color: rgba(255, 255, 255, 0.55); font-size: 13px;")
        show_row.addWidget(self.show_lbl)
        self.show_combo = QComboBox()
        # Дропдаун хранит display_name как текст и slug в userData(role=Qt.UserRole).
        # Слот ловит индекс — не текст, потому что display_name может совпадать
        # у двух сериалов (slug всё равно уникальный).
        self.show_combo.currentIndexChanged.connect(self._on_show_changed_idx)
        show_row.addWidget(self.show_combo)

        # Кнопка «+» Новый сериал — открывает NewShowDialog.
        # ASCII-плюс (не emoji ➕) чтобы гарантированно отображался в
        # PyInstaller-сборке без emoji-шрифта. Локальный stylesheet
        # перекрывает глобальный QPushButton{padding:7px 14px}.
        # 2026-05-08 редизайн Этап 3: LUMZ accent_red_subtle — лёгкая
        # красная подсветка, в отличие от ярко-залитой главной action-
        # кнопки «Промпт Seedance». Это «второстепенная» action-кнопка.
        self.new_show_btn = QPushButton("+")
        self.new_show_btn.setToolTip(tr('new_show_btn_tooltip'))
        self.new_show_btn.setFixedSize(32, 32)
        self.new_show_btn.setStyleSheet("""
            QPushButton {
                background: rgba(228, 52, 74, 0.10);
                border: 1px solid rgba(228, 52, 74, 0.25);
                border-radius: 8px;
                padding: 0;
                color: #e4344a;
                font-size: 14px;
                font-weight: 400;
            }
            QPushButton:hover {
                background: rgba(228, 52, 74, 0.18);
                border-color: rgba(228, 52, 74, 0.40);
            }
            QPushButton:pressed { background: rgba(228, 52, 74, 0.25); }
        """)
        self.new_show_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_show_btn.clicked.connect(self._on_new_show)
        show_row.addWidget(self.new_show_btn)

        self.show_lbl.hide()
        self.show_combo.hide()
        self.new_show_btn.hide()
        show_row.addStretch()
        if not getattr(self, '_compact_nav_enabled', False):
            lay.addLayout(show_row)

        # Эпизоды + название/длительность
        ep_row = QHBoxLayout()
        ep_row.setSpacing(8)
        # 2026-05-08 редизайн Этап 3: лейбл «Эпизод:» перед рядом пилюль
        # (по аналогии с лейблом «Сериал:» в строке выше). На пилюлях теперь
        # только номер «01»/«02»/«10» — этот лейбл их объясняет.
        self.ep_label = QLabel(tr('episode'))
        self.ep_label.setStyleSheet(
            "color: rgba(255, 255, 255, 0.55); font-size: 13px;")
        ep_row.addWidget(self.ep_label)
        self.ep_pills_container = QWidget()
        # 2026-05-05: переход с горизонтального скролла на сетку «10 пилюль
        # в ряду», переносы автоматически. ep_pills_layout теперь VBoxLayout —
        # каждая строка = HBoxLayout с до 10 кнопок. Логика заполнения
        # в _populate_episodes.
        # 2026-05-07: фиксируем sizePolicy по вертикали — иначе при
        # изменении контента ниже (например, появление outfit picker'а в
        # чате) Qt-layout пересчитывает и контейнер «дышит» на 1-2px,
        # из-за чего пилюли визуально становятся ниже. Vertical=Fixed
        # — высота строго по sizeHint (зависит от числа рядов).
        # 2026-05-08: Horizontal=Maximum — контейнер занимает РОВНО
        # столько ширины сколько нужно для пилюль, не растягивается.
        # Это позволяет addStretch ниже толкнуть ep_title_label вправо.
        # С Preferred контейнер всё равно расширялся → title не двигался.
        self.ep_pills_container.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.ep_pills_layout = QVBoxLayout(self.ep_pills_container)
        self.ep_pills_layout.setContentsMargins(0, 0, 0, 0)
        self.ep_pills_layout.setSpacing(4)
        # 2026-05-08 редизайн: pills_container БЕЗ stretch=1 — он
        # занимает только свою естественную ширину. Stretch перенесён
        # ПЕРЕД ep_title_label чтобы плашка названия серии прижималась
        # к правому краю строки (на одной линии с правым краем кнопки
        # «▶ Промпт Seedance» ниже). Раньше с stretch=1 у pills_container
        # title-плашка торчала «за пилюлями», не на правом краю.
        ep_row.addWidget(self.ep_pills_container)
        ep_row.addStretch()
        # 2026-05-19: «📦 Собрать серию» — packing все блоки эпизода
        # (рефы + сториборды + Seedance .txt) в один zip-архив.
        # Размещение: СПРАВА от ep_pills, СЛЕВА от ep_title_label.
        # Видимость синхронно с delete_ep_btn (когда выбран эпизод).
        self.compile_ep_btn = QPushButton(tr('compile_ep_btn'))
        self.compile_ep_btn.setObjectName("compile-ep-btn")
        self.compile_ep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compile_ep_btn.setStyleSheet(
            "QPushButton#compile-ep-btn {"
            " background: #242628;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 8px;"
            " color: #f2f3f0;"
            " padding: 0 14px;"
            " font-size: 12px; font-weight: 600;"
            "}"
            "QPushButton#compile-ep-btn:hover {"
            " background: #2c2f31;"
            " border-color: rgba(255,255,255,0.16); }"
            "QPushButton#compile-ep-btn:pressed {"
            " background: #1f2123; }"
            "QPushButton#compile-ep-btn:disabled {"
            " color: rgba(242,244,239,0.30);"
            " background: rgba(255,255,255,0.055);"
            " border-color: rgba(255,255,255,0.08); }"
        )
        self.compile_ep_btn.setFixedHeight(NAV_H)
        self.compile_ep_btn.setVisible(False)
        self.compile_ep_btn.clicked.connect(self._on_compile_episode_btn)
        if getattr(self, '_compact_nav_enabled', False):
            self.compact_nav_layout.addWidget(self.compile_ep_btn)
        else:
            ep_row.addWidget(self.compile_ep_btn)
        # 2026-05-07: title — кликабельная кнопка. Клик → попап с
        # оригинальным сценарием эпизода (из shows/<slug>/scenarios/epNN.txt).
        self.ep_title_label = QPushButton("")
        self.ep_title_label.setObjectName("episode-title-btn")
        self.ep_title_label.setFlat(True)
        self.ep_title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ep_title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ep_title_label.clicked.connect(self._on_ep_title_clicked)
        if getattr(self, '_compact_nav_enabled', False):
            self.ep_title_label.setFixedHeight(NAV_H)
            self.compact_nav_layout.addWidget(self.ep_title_label)
        else:
            ep_row.addWidget(self.ep_title_label)
        self.ep_dur_label = QLabel("")
        self.ep_dur_label.setObjectName("episode-duration")
        ep_row.addWidget(self.ep_dur_label)
        if getattr(self, '_compact_nav_enabled', False):
            self.ep_label.hide()
            self.ep_pills_container.hide()
            self.ep_dur_label.hide()
        if not getattr(self, '_compact_nav_enabled', False):
            lay.addLayout(ep_row)

        # 2026-05-05 v3: drop-зона переехала внутрь NewEpisodeView (форма
        # «Новый эпизод»). На стартовом экране редактора её больше нет —
        # она появляется только когда юзер кликнул «+».
        # Атрибут scenario_drop_zone оставлен как None для совместимости
        # с проверками `if hasattr(self, 'scenario_drop_zone')`.
        self.scenario_drop_zone = None

        # Блоки (пилюли) — обёртка-контейнер #blocks-bar в LUMZ-стиле
        # (см. QSS): фон bg_card, border, radius 8px. Внутри 6 элементов
        # (Блок 1-N, Референсы, Чат) прижаты друг к другу. 2026-05-08.
        self.block_pills_container = QWidget()
        self.block_pills_container.setObjectName("blocks-bar")
        self.block_pills_container.setProperty(
            "compact", getattr(self, '_compact_nav_enabled', False))
        # 2026-05-22: контейнер строк (VBox). Внутри — несколько HBoxLayout
        # по 9 блок-пилюль в каждой. Refs/Chat пилюли всегда в первой строке
        # (напротив блоков 1-9). _clear_layout рекурсивный — чистит вложенные.
        self.block_pills_layout = QVBoxLayout(self.block_pills_container)
        self.block_pills_layout.setContentsMargins(4, 3, 4, 3)
        self.block_pills_layout.setSpacing(0)
        if getattr(self, '_compact_nav_enabled', False):
            # 10 compact block buttons: 10*44px + 9*2px spacing + 8px margins.
            # Keep the outer pill fixed so fullscreen layouts cannot stretch
            # the background into a long empty bar.
            self.block_pills_container.setFixedSize(466, 34)
            self.block_pills_container.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        blk_row = QHBoxLayout()
        blk_row.setSpacing(8)
        self.inline_block_label = QLabel(tr('block'))
        self.inline_block_label.setObjectName("nav-label")
        blk_row.addWidget(self.inline_block_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        if getattr(self, '_compact_nav_enabled', False):
            self._block_carousel_left_btn = QPushButton("‹")
            self._block_carousel_left_btn.setObjectName("block-carousel-arrow")
            self._block_carousel_left_btn.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self._block_carousel_left_btn.clicked.connect(
                lambda: self._scroll_block_carousel(-5))
            blk_row.addWidget(
                self._block_carousel_left_btn,
                alignment=Qt.AlignmentFlag.AlignVCenter)
        blk_row.addWidget(self.block_pills_container)
        if getattr(self, '_compact_nav_enabled', False):
            self._block_carousel_right_btn = QPushButton("›")
            self._block_carousel_right_btn.setObjectName("block-carousel-arrow")
            self._block_carousel_right_btn.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self._block_carousel_right_btn.clicked.connect(
                lambda: self._scroll_block_carousel(5))
            blk_row.addWidget(
                self._block_carousel_right_btn,
                alignment=Qt.AlignmentFlag.AlignVCenter)
        blk_row.addStretch()
        # Кнопка «Удалить эпизод» — всегда справа в той же строке. Disabled
        # когда нет активного эпизода. Клик → диалог подтверждения → удаление
        # episodes.json[ep] + chats/<ep>.jsonl + output/prompts/<ep>_* +
        # output/storyboards/<ep>_*. Refs не трогает.
        # 2026-05-08 редизайн Этап 5: иконка корзины — Lucide SVG
        # `trash-2.svg` (белая, тонкая, в стиле остальных Lucide-иконок).
        # Раньше был emoji `🗑` — выглядел разнокалиберно с остальным UI.
        self.delete_ep_btn = QPushButton("")
        self.delete_ep_btn.setObjectName("delete-episode-btn")
        self.delete_ep_btn.setFixedSize(NAV_H, NAV_H)
        self.delete_ep_btn.setToolTip(tr('delete_ep_btn_tooltip'))
        self.delete_ep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_ep_btn.setEnabled(False)
        self.delete_ep_btn.setVisible(False)
        # Загрузка SVG-иконки. assets/icons/ бандлится в .app через .spec.
        try:
            from PyQt6.QtCore import QSize
            icons_dir = self._project_root / "assets" / "icons"
            if not icons_dir.exists():
                base = (Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS')
                        else Path(__file__).parent)
                icons_dir = base / "assets" / "icons"
            self.delete_ep_btn.setIcon(QIcon(str(icons_dir / "trash-2.svg")))
            self.delete_ep_btn.setIconSize(QSize(16, 16))
        except Exception:
            # Fallback на emoji если SVG не загрузился (PyInstaller edge-case)
            self.delete_ep_btn.setText("🗑")
        self.delete_ep_btn.clicked.connect(self._on_delete_episode_clicked)
        if getattr(self, '_compact_nav_enabled', False):
            self.block_pills_container.show()
            lay.addLayout(blk_row)
            self.compact_nav_layout.addWidget(self.delete_ep_btn)
        else:
            blk_row.addWidget(self.delete_ep_btn)
        if not getattr(self, '_compact_nav_enabled', False):
            lay.addLayout(blk_row)

        # Заголовок блока («КАМЕРА ЛОРЫ ~8с») + кнопка Seedance справа
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        self.block_title = QLabel("")
        self.block_title.setObjectName("block-title")
        title_row.addWidget(self.block_title, stretch=1)
        # v1.0.88 (Stage 16): кнопка «🗂 Рефы блока» СЛЕВА от Seedance.
        # Собирает все рефы текущего блока (location + objects + characters)
        # в .cache/_block_view/<ep>_block<N>/ и открывает папку в Finder.
        # Стиль — outline subtle red (как «+ Добавить» в refs-секциях,
        # ниже на line ~8951): secondary action, не конкурирует с primary
        # залитым красным Seedance-кнопкой.
        self.block_refs_btn = QPushButton(tr('block_refs_btn'))
        self.block_refs_btn.setObjectName("block-refs-btn")
        self.block_refs_btn.setFixedHeight(34)   # единая высота 4 кнопок
        self.block_refs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.block_refs_btn.setIcon(get_icon('folder'))
        self.block_refs_btn.setIconSize(QSize(16, 16))
        self.block_refs_btn.setStyleSheet(
            "QPushButton#block-refs-btn {"
            " background: #242628;"
            " border: 1px solid rgba(255,255,255,0.10);"
            " border-radius: 8px;"
            " color: #f2f3f0;"
            " padding: 8px 10px;"
            " font-size: 12px; font-weight: 600;"
            "}"
            "QPushButton#block-refs-btn:hover {"
            " background: #2c2f31;"
            " border-color: rgba(255,255,255,0.16);"
            " color: #ffffff; }"
            "QPushButton#block-refs-btn:pressed {"
            " background: #1f2123; }"
        )
        self.block_refs_btn.clicked.connect(self._on_block_refs_btn)
        self.block_refs_btn.setVisible(False)
        if not getattr(self, '_compact_nav_enabled', False):
            title_row.addWidget(self.block_refs_btn)
        # 2026-05-06: Этап 3 — кнопка открытия попапа с Seedance промптом.
        # Видна только когда current_block указывает на блок утверждённой
        # карты эпизода. State обновляется в _display_block.
        self.seedance_btn = QPushButton(tr('seedance_btn'))
        self.seedance_btn.setObjectName("seedance-btn")
        self.seedance_btn.setFixedHeight(34)   # единая высота 4 кнопок
        self.seedance_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # 2026-05-08 редизайн Этап 5: главная action-кнопка экрана —
        # залитая красная (LUMZ accent_red). Это единственная такая
        # яркая кнопка на странице блока, привлекает внимание к
        # «следующему шагу» (запуск промпта Seedance для видео).
        self.seedance_btn.setStyleSheet(
            "QPushButton#seedance-btn {"
            " background: #e4344a; color: #fefefe;"
            " border: none; border-radius: 8px;"
            " padding: 8px 10px;"
            " font-size: 12px; font-weight: 500;"
            "}"
            "QPushButton#seedance-btn:hover { background: #d92d44; }"
            "QPushButton#seedance-btn:pressed { background: #c52539; }"
            "QPushButton#seedance-btn:disabled {"
            " background: rgba(228, 52, 74, 0.30);"
            " color: rgba(255, 255, 255, 0.40);"
            "}"
        )
        self.seedance_btn.clicked.connect(self._on_seedance_btn)
        self.seedance_btn.setVisible(False)
        if not getattr(self, '_compact_nav_enabled', False):
            title_row.addWidget(self.seedance_btn)
        # 2026-06-11 (стоп-кнопка): «Остановить генерацию» — ПЕРВАЯ в ряду
        # (перед «Рефы блока»), видна пока идёт генерация. Залитый янтарный —
        # выделяется на фоне двух красных кнопок (Seedance/refs).
        self.stop_gen_btn = QPushButton(tr('stop_gen_btn'))
        self.stop_gen_btn.setObjectName("stop-gen-btn")
        self.stop_gen_btn.setFixedHeight(34)   # единая высота 4 кнопок
        self.stop_gen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_gen_btn.setStyleSheet(
            "QPushButton#stop-gen-btn {"
            " background: #e8a33d; color: #1a1208; border: none;"
            " border-radius: 8px;"
            " padding: 8px 10px; font-size: 12px; font-weight: 600; }"
            "QPushButton#stop-gen-btn:hover { background: #f0b355; }"
            "QPushButton#stop-gen-btn:disabled {"
            " background: rgba(232, 163, 61, 0.35);"
            " color: rgba(26, 18, 8, 0.55); }")
        self.stop_gen_btn.clicked.connect(self._on_stop_gen_btn)
        self.stop_gen_btn.setVisible(False)
        # Первой среди кнопок (после заголовка-stretch, перед «Рефы блока»).
        if not getattr(self, '_compact_nav_enabled', False):
            title_row.insertWidget(1, self.stop_gen_btn)
        # 2026-06-11 (очистка блока): «🗑 Очистить блок» — ПЕРВОЙ в ряду (перед
        # «Остановить»). Тихая outline; видна только если в блоке есть картинки
        # и нет активной генерации (см. _refresh_clear_btn).
        self.clear_block_btn = QPushButton(tr('clear_block_btn'))
        self.clear_block_btn.setObjectName("clear-block-btn")
        self.clear_block_btn.setFixedHeight(34)   # единая высота 4 кнопок
        self.clear_block_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_block_btn.setIcon(get_icon('trash-2'))
        self.clear_block_btn.setIconSize(QSize(16, 16))
        self.clear_block_btn.setStyleSheet(
            "QPushButton#clear-block-btn {"
            " background: #242628; color: #f2f3f0;"
            " border: 1px solid rgba(255,255,255,0.10); border-radius: 8px;"
            " padding: 8px 10px; font-size: 12px; font-weight: 600; }"
            "QPushButton#clear-block-btn:hover {"
            " background: #2c2f31; color: #ffffff;"
            " border-color: rgba(255,255,255,0.16); }"
            "QPushButton#clear-block-btn:pressed { background: #1f2123; }")
        self.clear_block_btn.clicked.connect(self._on_clear_block_btn)
        self.clear_block_btn.setVisible(False)
        if getattr(self, '_compact_nav_enabled', False):
            blk_row.addWidget(self.stop_gen_btn)
            blk_row.addWidget(self.clear_block_btn)
            blk_row.addWidget(self.block_refs_btn)
            blk_row.addWidget(self.seedance_btn)
        else:
            title_row.insertWidget(1, self.clear_block_btn)
        lay.addLayout(title_row)

        # ── Стек: страница 0 = шоты, страница 1 = референсы ─────────────────
        self.content_stack = QStackedWidget()

        # ── Страница 0: карточки шотов ──────────────────────────────────────
        cards_w = QWidget()
        self.shot_cards: List[ShotCard] = []
        # Этап 4 (формат кадра): 16:9 → сетка 2×2 горизонтальных карточек;
        # 9:16 → текущий горизонтальный ряд (поведение НЕ меняется).
        if self._current_aspect == "16:9":
            grid = QGridLayout(cards_w)
            grid.setSpacing(12)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setAlignment(
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            for i in range(PANELS):
                card = ShotCard(i, aspect="16:9")
                self._wire_shot_card(card)
                self.shot_cards.append(card)
                grid.addWidget(card, i // 2, i % 2)   # (0,0)(0,1)(1,0)(1,1)
        else:
            self.cards_row = QHBoxLayout(cards_w)
            self.cards_row.setSpacing(12)
            self.cards_row.setContentsMargins(0, 0, 0, 0)
            for i in range(PANELS):
                card = ShotCard(i)
                self._wire_shot_card(card)
                self.shot_cards.append(card)
                self.cards_row.addWidget(card)
            self.cards_row.addStretch()

        self.shots_scroll = QScrollArea()
        self.shots_scroll.setWidgetResizable(True)
        self.shots_scroll.setWidget(cards_w)
        self.shots_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.shots_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_stack.addWidget(self.shots_scroll)   # index 0
        if self._current_aspect == "16:9":
            self.shots_scroll.viewport().installEventFilter(self)
            QTimer.singleShot(0, self._recalc_shot_cards_size)

        # ── Страница 1: референсы ──────────────────────────────────────────
        self.refs_container = QWidget()
        self.refs_layout = QVBoxLayout(self.refs_container)
        self.refs_layout.setSpacing(20)
        self.refs_layout.setContentsMargins(0, 0, 0, 0)
        self.refs_layout.addStretch()

        self.refs_scroll = QScrollArea()
        self.refs_scroll.setWidgetResizable(True)
        self.refs_scroll.setWidget(self.refs_container)
        self.refs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.refs_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_stack.addWidget(self.refs_scroll)   # index 1

        # ── Страница 2: чат конкретного эпизода ────────────────────────────
        self.episode_chat_view = EpisodeChatView(self)
        self.content_stack.addWidget(self.episode_chat_view)   # index 2

        # ── Страница 3: создание нового эпизода ────────────────────────────
        # `self.new_episode_view` уже создан выше (до addTab Editor).
        # Активируется кнопкой «+» рядом с пилюлями эпизодов.
        # 2026-05-04: UX-переделка — отдельная вкладка убрана, всё внутри
        # Editor'а как часть единого потока работы над сериалом.
        self.content_stack.addWidget(self.new_episode_view)   # index 3

        lay.addWidget(self.content_stack, stretch=1)

        # Кнопка сохранения сториборда. 2026-05-08 редизайн: emoji 💾
        # убрано из текста, добавлена Lucide SVG иконка `download.svg`
        # (как остальные иконки в шапке/корзине).
        self.save_btn = QPushButton(tr('save_png'))
        self.save_btn.setObjectName("storyboard-save")
        self.save_btn.setEnabled(False)
        try:
            from PyQt6.QtCore import QSize
            icons_dir = self._project_root / "assets" / "icons"
            if not icons_dir.exists():
                base = (Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS')
                        else Path(__file__).parent)
                icons_dir = base / "assets" / "icons"
            self.save_btn.setIcon(QIcon(str(icons_dir / "download.svg")))
            self.save_btn.setIconSize(QSize(16, 16))
        except Exception:
            pass
        self.save_btn.clicked.connect(self._save_png)
        lay.addWidget(self.save_btn)
        return w

    def _build_settings_tab(self) -> QWidget:
        # Внешний контейнер с QScrollArea — секции в Настройках растут
        # вниз (API-ключ, админ-секции с анимациями и обновлениями).
        # Без прокрутки они начинают сжиматься. С QScrollArea — каждый
        # фрейм сохраняет свою высоту, появляется вертикальный scroll.
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        w = QWidget()
        # 2026-06-17 (Коммит 7 фикс): скоупим прозрачность ID-селектором.
        # Раньше bare-правило "background: transparent;" каскадило прозрачный
        # фон на ВСЕХ потомков → непрозрачные карточки/кнопки внутри Настроек
        # не прокрашивались (только border). С #id-селектором правило бьёт
        # только сам w, потомки красятся своими #id-правилами app-DARK.
        w.setObjectName("settings-scroll-content")
        w.setStyleSheet(
            "QWidget#settings-scroll-content { background: transparent; }")
        lay = QVBoxLayout(w)
        lay.setSpacing(22)
        lay.setContentsMargins(28, 26, 28, 26)

        # ── ПРОЕКТ — одна кнопка «Открыть папку проекта» ────────────────────
        self.sec_project_lbl = QLabel(tr('sec_project'))
        self.sec_project_lbl.setObjectName("settings-section")

        proj_frame = QFrame()
        proj_frame.setObjectName("settings-group-tile")
        pf = QVBoxLayout(proj_frame)
        pf.setSpacing(8)
        pf.setContentsMargins(12, 12, 12, 12)
        # 2026-06-17 (Коммит 7 фикс): stretch первым → обе кнопки прижаты к низу
        # карточки (нижние кнопки трёх колонок на одной линии).
        pf.addStretch()
        self.open_folder_btn = QPushButton(tr('open_folder'))
        self.open_folder_btn.setObjectName("settings-light-btn")
        self.open_folder_btn.clicked.connect(self._open_folder)
        pf.addWidget(self.open_folder_btn)
        # 2026-05-08: кнопка «Открыть лог» — показывает runtime.log
        # с диагностикой ошибок (stdout+stderr Studio пишутся туда).
        self.open_log_btn = QPushButton(tr('open_log_btn'))
        self.open_log_btn.setObjectName("settings-light-btn")
        self.open_log_btn.setToolTip(tr('open_log_btn_tooltip'))
        self.open_log_btn.clicked.connect(self._open_studio_log)
        pf.addWidget(self.open_log_btn)

        # ── О ПРИЛОЖЕНИИ — версии (две строки с тонкой разделительной) ─────
        self.sec_about_lbl = QLabel(tr('sec_about'))
        self.sec_about_lbl.setObjectName("settings-section")

        about_frame = QFrame()
        about_frame.setObjectName("settings-group-tile")
        af = QVBoxLayout(about_frame)
        af.setSpacing(8)
        af.setContentsMargins(12, 12, 12, 12)

        # Версия приложения (одна строка — раньше были 2: app + project,
        # 2026-05-08 объединили в одну, поле version в version.json устарело)
        row_app = QWidget()
        row_app.setObjectName("settings-row")
        ra = QHBoxLayout(row_app)
        ra.setContentsMargins(0, 0, 0, 0)
        self.app_ver_key_lbl = QLabel(tr('app_version'))
        self.app_ver_key_lbl.setObjectName("settings-row-key")
        ra.addWidget(self.app_ver_key_lbl)
        ra.addStretch()
        self.app_ver_val_lbl = QLabel("")
        self.app_ver_val_lbl.setObjectName("settings-row-val")
        ra.addWidget(self.app_ver_val_lbl)
        af.addWidget(row_app)
        # 2026-06-17 (Коммит 7 фикс): версия сверху, кнопка прижата к низу.
        af.addStretch()

        # 2026-05-12 (v1.0.53): кнопка ручной проверки обновлений.
        # Auto-check работает только при старте Studio (QTimer.singleShot
        # через 2 сек). Если юзер открыл Studio неделю назад и не
        # перезапускал — новые версии он не увидит. Эта кнопка
        # позволяет проверить прямо сейчас без перезапуска.
        # Особенно полезна для коллег после обновления через Installer:
        # «нажми эту кнопку чтобы убедиться что auto-update работает».
        self.check_updates_btn = QPushButton(tr('check_updates_btn'))
        self.check_updates_btn.setObjectName("settings-light-btn")
        self.check_updates_btn.setToolTip(tr('check_updates_btn_tooltip'))
        self.check_updates_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_updates_btn.clicked.connect(self._on_manual_check_updates)
        af.addWidget(self.check_updates_btn)

        # ── AI-АККАУНТ ──────────────────────────────────────────────────
        # Показывает email текущего залогиненного claude CLI аккаунта.
        # Кнопка «Сменить аккаунт» дёргает существующий AuthSwitchThread
        # (logout + open Terminal с claude auth login + polling).
        # 2026-05-09: вынесено в Settings — раньше смена была доступна
        # только через AuthBanner (только при детекции quota/logged_out).
        self.sec_ai_account_lbl = QLabel(tr('sec_ai_account'))
        self.sec_ai_account_lbl.setObjectName("settings-section")

        claude_acc_frame = QFrame()
        claude_acc_frame.setObjectName("settings-group-tile")
        cf = QVBoxLayout(claude_acc_frame)
        cf.setSpacing(8)
        cf.setContentsMargins(12, 12, 12, 12)

        # Строка с email — повторяет стиль строки версии в about_frame.
        row_acc = QWidget()
        row_acc.setObjectName("settings-row")
        ra2 = QHBoxLayout(row_acc)
        ra2.setContentsMargins(0, 0, 0, 0)
        self.claude_acc_key_lbl = QLabel(tr('ai_account_current'))
        self.claude_acc_key_lbl.setObjectName("settings-row-key")
        ra2.addWidget(self.claude_acc_key_lbl)
        ra2.addStretch()
        self.claude_acc_email_lbl = QLabel(tr('ai_account_loading'))
        self.claude_acc_email_lbl.setObjectName("settings-row-val")
        ra2.addWidget(self.claude_acc_email_lbl)
        cf.addWidget(row_acc)
        # 2026-06-17 (Коммит 7 фикс): email сверху, кнопка прижата к низу.
        cf.addStretch()

        # Кнопка «Сменить аккаунт» — светлый стиль (settings-light-btn).
        self.claude_acc_switch_btn = QPushButton(tr('ai_account_switch_btn'))
        self.claude_acc_switch_btn.setObjectName("settings-light-btn")
        self.claude_acc_switch_btn.clicked.connect(self._on_auth_switch_requested)
        cf.addWidget(self.claude_acc_switch_btn)

        # 2026-06-17 (Коммит 7): колонки рядом — «Проект» | «О приложении» |
        # «AI-аккаунт». В каждой свой заголовок секции + frame. Карточки
        # растягиваются на одну высоту через frame со stretch=1 (выравнивание
        # высоты); кнопки внутри прижаты к низу (addStretch внутри frame'ов).
        info_row = QHBoxLayout()
        info_row.setSpacing(12)

        col_proj = QVBoxLayout()
        col_proj.setSpacing(6)
        col_proj.addWidget(self.sec_project_lbl)
        col_proj.addWidget(proj_frame, stretch=1)
        col_proj_widget = QWidget()
        col_proj_widget.setMinimumWidth(220)
        col_proj_widget.setLayout(col_proj)
        info_row.addWidget(col_proj_widget, stretch=1)

        col_about = QVBoxLayout()
        col_about.setSpacing(6)
        col_about.addWidget(self.sec_about_lbl)
        col_about.addWidget(about_frame, stretch=1)
        col_about_widget = QWidget()
        col_about_widget.setMinimumWidth(220)
        col_about_widget.setLayout(col_about)
        info_row.addWidget(col_about_widget, stretch=1)

        col_account = QVBoxLayout()
        col_account.setSpacing(6)
        col_account.addWidget(self.sec_ai_account_lbl)
        col_account.addWidget(claude_acc_frame, stretch=1)
        col_account_widget = QWidget()
        col_account_widget.setMinimumWidth(220)
        col_account_widget.setLayout(col_account)
        info_row.addWidget(col_account_widget, stretch=1)

        lay.addLayout(info_row)

        # Инициализируем email актуальным значением. _last_known_auth_email
        # уже populated initial-tick'ом при старте Studio (см. __init__).
        self._refresh_claude_account_email()

        # ── API КЛЮЧ Fast Gen ──────────────────────────────────────────────
        # Поле для замены ключа без правки .env: коллега получил новый
        # ключ от админа → вставил → «Сохранить» → работает сразу.
        # Сохраняется в QSettings (приоритет над .env). См. load_api_key().
        self.sec_apikey_lbl = QLabel(tr('sec_apikey'))
        self.sec_apikey_lbl.setObjectName("settings-section")
        # 2026-06-10 (выделение блока ключей): золотой заголовок поверх общего
        # #settings-section — блок видно при быстрой прокрутке Settings.
        self.sec_apikey_lbl.setStyleSheet(
            "QLabel#settings-section { font-size:11px; font-weight:700;"
            " color:rgba(255,255,255,0.55); letter-spacing:2.5px; }")
        lay.addWidget(self.sec_apikey_lbl)

        apikey_frame = QFrame()
        apikey_frame.setObjectName("settings-group")
        # 2026-06-10 (выделение блока ключей): тёплая золотая рамка поверх общей
        # #settings-group; фон/радиус оставляем как у остальных групп.
        apikey_frame.setStyleSheet(
            "QFrame#settings-group { background: rgba(20, 16, 30, 0.5);"
            " border:1px solid #b08a3e; border-radius:12px; }")
        akf = QVBoxLayout(apikey_frame)
        akf.setSpacing(0)
        akf.setContentsMargins(18, 14, 18, 14)

        # Подсказка над полем
        self.apikey_hint_lbl = QLabel(tr('apikey_hint'))
        self.apikey_hint_lbl.setObjectName("apikey-hint")
        self.apikey_hint_lbl.setWordWrap(True)
        self.apikey_hint_lbl.setStyleSheet(
            "color:#aaa; font-size:12px; padding-bottom:10px;")
        akf.addWidget(self.apikey_hint_lbl)

        # Строка: input + кнопки. 2026-06-09: + лампочка-индикатор round-robin
        # (косметика — мигает когда этот ключ отдал генерацию). Список
        # self._apikey_indicators индексно совпадает с self._apikey_fields.
        self._apikey_indicators = []
        # 2026-06-10 (этап 2): per-row тумблер «использовать» + лейбл статуса.
        # Индексно совпадают с self._apikey_fields (0..4).
        self._apikey_toggles = []
        self._apikey_status_labels = []
        def _mk_key_indicator():
            _d = QLabel()
            _d.setFixedSize(12, 12)
            _d.setStyleSheet(
                "QLabel { background:#3a3a3a; border-radius:6px; }")  # тусклый = неактивен
            return _d
        def _mk_key_status_lbl():
            _l = QLabel("")
            _l.setStyleSheet(
                "color:#888; font-size:11px; padding:0 0 9px 20px;")
            # Резерв высоты в одну строку — появление статуса («не работает —
            # замени» и т.п.) не дёргает высоту ряда ключа.
            _l.setFixedHeight(_l.fontMetrics().height() + 11)
            return _l
        def _mk_key_toggle(_idx):
            _t = QCheckBox(tr('key_use_toggle'))
            _t.setStyleSheet("QCheckBox { color:#aaa; font-size:11px; }")
            _t.toggled.connect(lambda _checked, i=_idx: self._on_key_toggle(i))
            return _t
        ak_row = QHBoxLayout()
        ak_row.setSpacing(8)
        _ind0 = _mk_key_indicator()
        ak_row.addWidget(_ind0)
        self._apikey_indicators.append(_ind0)
        self.apikey_input = QLineEdit()
        self.apikey_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.apikey_input.setPlaceholderText(tr('apikey_placeholder'))
        try:
            self.apikey_input.setText(load_api_key() or "")
        except Exception:
            pass
        self.apikey_input.setStyleSheet(
            "QLineEdit { background:#191b1d; border:1px solid #1d1e20;"
            " border-radius:6px; padding:8px 10px; color:#ddd;"
            " font-size:13px; font-family: 'Menlo', monospace; }")
        ak_row.addWidget(self.apikey_input, stretch=1)

        self.apikey_show_btn = QPushButton(tr('apikey_show'))
        self.apikey_show_btn.setFixedHeight(34)
        self.apikey_show_btn.setMinimumWidth(110)
        self.apikey_show_btn.setCheckable(True)
        self.apikey_show_btn.toggled.connect(self._on_apikey_toggle_visibility)
        # 2026-06-10 (косметика): кнопки «Скрыть»/«Сохранить» НЕ добавляем в
        # ak_row — переносятся в отдельный нижний ряд под 5-м полем (ниже).
        # Объекты и коннекты создаём здесь как раньше (логика не меняется).

        self.apikey_save_btn = QPushButton(tr('apikey_save'))
        self.apikey_save_btn.setObjectName("save")
        self.apikey_save_btn.setFixedHeight(34)
        self.apikey_save_btn.setMinimumWidth(120)
        self.apikey_save_btn.clicked.connect(self._on_apikey_save)
        _tgl0 = _mk_key_toggle(0)
        ak_row.addWidget(_tgl0)
        self._apikey_toggles.append(_tgl0)
        akf.addLayout(ak_row)
        _lbl0 = _mk_key_status_lbl()
        akf.addWidget(_lbl0)
        self._apikey_status_labels.append(_lbl0)

        # 2026-06-09: пул до 5 ключей (round-robin). Поле 1 = apikey_input выше
        # (primary, идёт в .env строку 0). Поля 2..5 — дополнительные ключи пула,
        # хранятся ТОЛЬКО в QSettings (fastgen_api_key_2..5) + сайдкаре
        # fastgen_keys.txt; в .env НЕ пишутся (там со 2-й строки Anthropic-ключ
        # и пр.). Единый список self._apikey_fields используют toggle видимости
        # и _on_apikey_save.
        self._apikey_fields = [self.apikey_input]
        _qs_keys = QSettings(APP_ORG, APP_NAME)
        for _i in range(2, 6):
            _inp = QLineEdit()
            _inp.setEchoMode(QLineEdit.EchoMode.Password)
            _inp.setPlaceholderText(tr('apikey_placeholder'))
            try:
                _inp.setText(_qs_keys.value(f"fastgen_api_key_{_i}", "", type=str) or "")
            except Exception:
                pass
            _inp.setStyleSheet(
                "QLineEdit { background:#191b1d; border:1px solid #1d1e20;"
                " border-radius:6px; padding:8px 10px; color:#ddd;"
                " font-size:13px; font-family: 'Menlo', monospace;"
                " margin-top:6px; }")
            _row_i = QHBoxLayout()
            _row_i.setSpacing(8)
            _ind_i = _mk_key_indicator()
            _row_i.addWidget(_ind_i)
            _row_i.addWidget(_inp, stretch=1)
            _tgl_i = _mk_key_toggle(_i - 1)   # поле _i (2..5) → индекс _i-1 (1..4)
            _row_i.addWidget(_tgl_i)
            akf.addLayout(_row_i)
            _lbl_i = _mk_key_status_lbl()
            akf.addWidget(_lbl_i)
            self._apikey_fields.append(_inp)
            self._apikey_indicators.append(_ind_i)
            self._apikey_toggles.append(_tgl_i)
            self._apikey_status_labels.append(_lbl_i)

        # 2026-06-10 (UX-полировка очереди #1): все 4 кнопки в ОДИН ряд внизу —
        # слева диагностика [Проверить сервер][Техподдержка], справа [Показать]
        # [Сохранить], между ними stretch. Высота 34 у всех. «Проверить сервер» —
        # вторичный синий акцент; «Сохранить» — #save-акцент; «Показать»/
        # «Техподдержка» нейтральны (дефолтный QPushButton).
        self.server_check_btn = QPushButton(tr('server_check_btn'))
        self.server_check_btn.setToolTip(tr('server_check_tip'))
        self.server_check_btn.setFixedHeight(34)
        self.server_check_btn.setStyleSheet(self._SERVER_CHECK_QSS_IDLE)
        self.server_check_btn.clicked.connect(self._on_server_check_click)
        self.server_support_btn = QPushButton(tr('server_support_btn'))
        self.server_support_btn.setFixedHeight(34)
        self.server_support_btn.setStyleSheet(self._SERVER_SUPPORT_QSS)
        self.server_support_btn.clicked.connect(self._on_server_support_click)

        _btns_row = QHBoxLayout()
        _btns_row.setSpacing(8)
        _btns_row.addWidget(self.server_check_btn)
        _btns_row.addWidget(self.server_support_btn)
        # 2026-06-10: статус сохранения («несохранено» / «✓ Сохранено» / «пусто»)
        # в ОДНОЙ строке с кнопками (высота ряда 34 фикс → не дёргается) — вместо
        # отдельных лейблов в вертикальном стеке, дававших пустой подвал.
        self.apikey_inline_status_lbl = QLabel("")
        self.apikey_inline_status_lbl.setStyleSheet("font-size:12px;")
        _btns_row.addWidget(self.apikey_inline_status_lbl)
        _btns_row.addStretch(1)
        _btns_row.addWidget(self.apikey_show_btn)
        _btns_row.addWidget(self.apikey_save_btn)
        akf.addLayout(_btns_row)

        # Пояснение во время теста / итог проверки — один лейбл под рядом кнопок.
        # В покое показывает постоянную idle-подсказку (лейбл НИКОГДА не пуст →
        # нет «пустого подвала»). minHeight зарезервирован под самый длинный
        # текст (explain) → переходы idle→explain→итог не меняют высоту блока.
        self.server_check_result_lbl = QLabel(tr('server_check_idle_hint'))
        self.server_check_result_lbl.setWordWrap(True)
        self.server_check_result_lbl.setStyleSheet(
            "color:#9a8fb0; font-size:12px; padding-top:6px;")
        akf.addWidget(self.server_check_result_lbl)
        self._reserve_server_check_lbl_height()

        # 2026-06-10 (этап 2): первичная отрисовка статусов (цвет/текст/тумблеры).
        try:
            self._refresh_key_status_indicators()
        except Exception:
            traceback.print_exc()

        # 2026-06-10 (косметика): #1 фикс обрезки кнопки «Сохранить» (padding
        # 7px вместо глобальных 11px — текст влезает в высоту 34px) + базовый
        # стиль (dirty=False). #3 — textChanged полей включает dirty-индикацию.
        # Коннект ПОСЛЕ build: программный setText полей был выше (до коннекта),
        # ложный dirty невозможен; refresh поля не setText'ит.
        self._set_apikey_dirty(False)
        for _f in self._apikey_fields:
            try:
                _f.textChanged.connect(self._on_apikey_field_changed)
            except Exception:
                pass

        lay.addWidget(apikey_frame)

        # ── 🌐 ПРОКСИ-СЕРВЕР — настройки прокси (видны всем, не админ-only) ─
        # v1.0.65: настройка HTTP-прокси через QSettings + os.environ.
        # Применяется при старте Studio через `apply_proxy_from_settings()`
        # ДО создания QApplication и subprocess'ов Claude CLI. Юзер видит
        # секцию даже если admin не передал API-ключи — прокси нужен
        # ДО любых запросов. Изменения требуют перезапуска (env vars
        # читаются только при старте requests-сессии).
        self.sec_proxy_lbl = QLabel(tr('sec_proxy'))
        self.sec_proxy_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_proxy_lbl)

        proxy_frame = QFrame()
        proxy_frame.setObjectName("settings-group")
        pxf = QVBoxLayout(proxy_frame)
        pxf.setSpacing(8)
        pxf.setContentsMargins(20, 18, 20, 18)

        # 1) Чекбокс «Использовать прокси-сервер»
        self.proxy_use_chk = QCheckBox(tr('proxy_use_checkbox'))
        self.proxy_use_chk.setChecked(
            QSettings(APP_ORG, APP_NAME).value(
                "proxy/enabled", False, type=bool))
        self.proxy_use_chk.toggled.connect(
            self._on_proxy_checkbox_toggled)
        pxf.addWidget(self.proxy_use_chk)

        # 2) 4 поля ввода — host, port, username, password
        # Каждое поле в HBox: лейбл слева (фикс. ширина 110), input справа.
        # Initial value читается из QSettings. Password — echoMode=Password.
        qs_proxy = QSettings(APP_ORG, APP_NAME)
        _LABEL_W = 110

        host_row = QHBoxLayout()
        host_row.setSpacing(8)
        self.proxy_host_label = QLabel(tr('proxy_host_label'))
        self.proxy_host_label.setFixedWidth(_LABEL_W)
        host_row.addWidget(self.proxy_host_label)
        self.proxy_host_input = QLineEdit()
        self.proxy_host_input.setObjectName("proxy-input")
        self.proxy_host_input.setPlaceholderText(tr('proxy_host_placeholder'))
        self.proxy_host_input.setText(
            qs_proxy.value("proxy/host", "", type=str))
        host_row.addWidget(self.proxy_host_input, stretch=1)
        pxf.addLayout(host_row)

        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        self.proxy_port_label = QLabel(tr('proxy_port_label'))
        self.proxy_port_label.setFixedWidth(_LABEL_W)
        port_row.addWidget(self.proxy_port_label)
        self.proxy_port_input = QLineEdit()
        self.proxy_port_input.setObjectName("proxy-input")
        self.proxy_port_input.setPlaceholderText(tr('proxy_port_placeholder'))
        self.proxy_port_input.setText(
            qs_proxy.value("proxy/port", "", type=str))
        port_row.addWidget(self.proxy_port_input, stretch=1)
        pxf.addLayout(port_row)

        user_row = QHBoxLayout()
        user_row.setSpacing(8)
        self.proxy_username_label = QLabel(tr('proxy_username_label'))
        self.proxy_username_label.setFixedWidth(_LABEL_W)
        user_row.addWidget(self.proxy_username_label)
        self.proxy_username_input = QLineEdit()
        self.proxy_username_input.setObjectName("proxy-input")
        self.proxy_username_input.setPlaceholderText(
            tr('proxy_username_placeholder'))
        self.proxy_username_input.setText(
            qs_proxy.value("proxy/username", "", type=str))
        user_row.addWidget(self.proxy_username_input, stretch=1)
        pxf.addLayout(user_row)

        # Пароль + кнопка-глазик (toggle echo mode). i18n для глазика
        # не нужен — это чистый символ 👁 / 🙈.
        pwd_row = QHBoxLayout()
        pwd_row.setSpacing(8)
        self.proxy_password_label = QLabel(tr('proxy_password_label'))
        self.proxy_password_label.setFixedWidth(_LABEL_W)
        pwd_row.addWidget(self.proxy_password_label)
        self.proxy_password_input = QLineEdit()
        self.proxy_password_input.setObjectName("proxy-input")
        self.proxy_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_password_input.setText(
            qs_proxy.value("proxy/password", "", type=str))
        pwd_row.addWidget(self.proxy_password_input, stretch=1)
        # Кнопка-глазик 28×28px справа от поля пароля
        self.proxy_password_toggle_btn = QPushButton("👁")
        self.proxy_password_toggle_btn.setFixedSize(28, 28)
        self.proxy_password_toggle_btn.setCursor(
            Qt.CursorShape.PointingHandCursor)
        self.proxy_password_toggle_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.06);"
            " border: 1px solid rgba(255,255,255,0.12);"
            " border-radius: 4px; font-size: 14px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.10); }")
        self.proxy_password_toggle_btn.clicked.connect(
            self._on_proxy_password_visibility_toggled)
        pwd_row.addWidget(self.proxy_password_toggle_btn)
        pxf.addLayout(pwd_row)

        _proxy_input_qss = (
            "QLineEdit#proxy-input { background:#191b1d; color:#dddddc;"
            " border:1px solid #1d1e20; border-radius:6px;"
            " padding:6px 8px; font-size:12px; }"
            "QLineEdit#proxy-input:disabled { background:#191b1d;"
            " color:rgba(255,255,255,0.30); }"
        )
        for _proxy_inp in (
            self.proxy_host_input,
            self.proxy_port_input,
            self.proxy_username_input,
            self.proxy_password_input,
        ):
            _proxy_inp.setStyleSheet(_proxy_input_qss)

        # Применяем начальное состояние enabled полей по чекбоксу
        self._on_proxy_checkbox_toggled(self.proxy_use_chk.isChecked())

        # 3) Кнопка «Проверить подключение» + label результата
        # Кнопка ВСЕГДА активна (даже если чекбокс снят — даёт тест
        # прямого подключения).
        pxf.addSpacing(6)
        self.proxy_test_btn = QPushButton(tr('proxy_test_button'))
        self.proxy_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.proxy_test_btn.clicked.connect(self._on_proxy_test_clicked)
        pxf.addWidget(self.proxy_test_btn)

        self.proxy_result_lbl = QLabel("")
        self.proxy_result_lbl.setWordWrap(True)
        self.proxy_result_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.proxy_result_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.85); font-size: 12px;"
            " font-family: 'Menlo','Consolas',monospace;"
            " padding: 4px 0;")
        pxf.addWidget(self.proxy_result_lbl)

        # 4) Кнопка «Сохранить и применить» + хинт про перезапуск
        pxf.addSpacing(6)
        self.proxy_save_btn = QPushButton(tr('proxy_save_button'))
        self.proxy_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.proxy_save_btn.clicked.connect(self._on_proxy_save_clicked)
        pxf.addWidget(self.proxy_save_btn)

        self.proxy_restart_hint_lbl = QLabel(tr('proxy_restart_hint'))
        self.proxy_restart_hint_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.45); font-size: 11px;"
            " padding-top: 2px;")
        self.proxy_restart_hint_lbl.setWordWrap(True)
        pxf.addWidget(self.proxy_restart_hint_lbl)

        lay.addWidget(proxy_frame)

        # Слот для держания живого ProxyTestThread'а (чтобы Qt GC не
        # убил его пока он работает в фоне).
        self._proxy_test_thread = None

        # ── 🎬 МОНТАЖНАЯ КАРТА — настройки оркестратора монтажки ───────────
        # 2026-05-13 (v1.0.61): toggle «Использовать Context Reviewer».
        # Context Reviewer (4-й агент монтажного оркестратора) делает
        # Bible-сверку финальной карты. На практике редко находит проблемы
        # (concerns=0 на тестовых эпизодах) и съедает ~2 мин на эпизод.
        # Default OFF — для скорости. Юзер включает для сложных эпизодов.
        self.sec_montage_lbl = QLabel(tr('sec_montage'))
        self.sec_montage_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_montage_lbl)

        montage_frame = QFrame()
        montage_frame.setObjectName("settings-group")
        mf = QVBoxLayout(montage_frame)
        mf.setSpacing(8)
        mf.setContentsMargins(20, 18, 20, 18)

        self.context_reviewer_chk = QCheckBox(
            tr('settings_context_reviewer_label'))
        self.context_reviewer_chk.setChecked(
            QSettings(APP_ORG, APP_NAME).value(
                "montage/context_reviewer_enabled", False, type=bool))
        self.context_reviewer_chk.toggled.connect(
            lambda checked: QSettings(APP_ORG, APP_NAME).setValue(
                "montage/context_reviewer_enabled", bool(checked)))
        mf.addWidget(self.context_reviewer_chk)

        self.context_reviewer_hint_lbl = QLabel(
            tr('settings_context_reviewer_hint'))
        self.context_reviewer_hint_lbl.setObjectName("settings-hint")
        self.context_reviewer_hint_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.55); font-size: 11px;")
        self.context_reviewer_hint_lbl.setWordWrap(True)
        mf.addWidget(self.context_reviewer_hint_lbl)

        lay.addWidget(montage_frame)

        # ── ПРОВАЙДЕРЫ ГЕНЕРАЦИИ (видны всем юзерам) ──────────────────────
        # 2026-06-16 (Коммит 3): по макету — один общий заголовок секции +
        # две карточки рядом (QHBoxLayout), в каждой свой сегмент-контрол
        # ProviderToggle вместо QComboBox.
        #   • actors — провайдер актёрских рефов (`image_provider_actors`).
        #     Влияет на: GenerateActorRefThread, EditActorRefThread,
        #     RefGenerateThread когда путь содержит /characters/.
        #   • admin  — провайдер шотов / локаций / объектов
        #     (`image_provider_admin`). Влияет на: GenerateThread (шоты),
        #     RefGenerateThread (/locations/ или /objects/), + bridge-файл
        #     `image_provider.txt` для batch-скриптов pipeline.py /
        #     generate_storyboards.py. (2026-06-15: раскрыт всем, раньше
        #     был под `if self._is_admin`.)
        self.sec_image_providers_lbl = QLabel(tr('sec_image_providers'))
        self.sec_image_providers_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_image_providers_lbl)

        # ── Карточка: референсы актёров ──
        provider_actors_frame = QFrame()
        provider_actors_frame.setObjectName("settings-group")
        paf = QVBoxLayout(provider_actors_frame)
        paf.setSpacing(0)
        paf.setContentsMargins(18, 14, 18, 14)

        self.image_provider_actors_card_title_lbl = QLabel(
            tr('image_provider_actors_card_title'))
        self.image_provider_actors_card_title_lbl.setStyleSheet(
            "color:#e0e0e0; font-size:13px; font-weight:600; "
            "padding-bottom:4px;")
        paf.addWidget(self.image_provider_actors_card_title_lbl)

        self.image_provider_actors_hint_lbl = QLabel(
            tr('image_provider_actors_hint'))
        self.image_provider_actors_hint_lbl.setWordWrap(True)
        self.image_provider_actors_hint_lbl.setStyleSheet(
            "color:#aaa; font-size:12px; padding-bottom:10px;")
        paf.addWidget(self.image_provider_actors_hint_lbl)

        # 2026-07-02: ProviderToggle (жёстко 2 позиции) → ModeSegment
        # (N сегментов, палитра 1:1 — см. коммент в mode_segment.py).
        # API set_value/value/valueChanged идентичен — слоты не тронуты.
        self.image_provider_actors_toggle = ModeSegment(self)
        self.image_provider_actors_toggle.set_options(
            [IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_NARWHAL_LITE,
             IMAGE_PROVIDER_OPENAI])
        self.image_provider_actors_toggle.set_labels(
            [tr('image_provider_narwhal'), tr('image_provider_nb2_lite'),
             tr('image_provider_openai')])
        self.image_provider_actors_toggle.set_value(image_provider_actors())
        self.image_provider_actors_toggle.valueChanged.connect(
            self._on_image_provider_actors_changed)
        paf.addWidget(self.image_provider_actors_toggle)

        # ── Карточка: сториборды, локации, объекты ──
        provider_admin_frame = QFrame()
        provider_admin_frame.setObjectName("settings-group")
        pf = QVBoxLayout(provider_admin_frame)
        pf.setSpacing(0)
        pf.setContentsMargins(18, 14, 18, 14)

        self.image_provider_admin_card_title_lbl = QLabel(
            tr('image_provider_admin_card_title'))
        self.image_provider_admin_card_title_lbl.setStyleSheet(
            "color:#e0e0e0; font-size:13px; font-weight:600; "
            "padding-bottom:4px;")
        pf.addWidget(self.image_provider_admin_card_title_lbl)

        self.image_provider_admin_hint_lbl = QLabel(
            tr('image_provider_admin_hint'))
        self.image_provider_admin_hint_lbl.setWordWrap(True)
        self.image_provider_admin_hint_lbl.setStyleSheet(
            "color:#aaa; font-size:12px; padding-bottom:10px;")
        pf.addWidget(self.image_provider_admin_hint_lbl)

        self.image_provider_admin_toggle = ModeSegment(self)
        self.image_provider_admin_toggle.set_options(
            [IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_NARWHAL_LITE,
             IMAGE_PROVIDER_OPENAI])
        self.image_provider_admin_toggle.set_labels(
            [tr('image_provider_narwhal'), tr('image_provider_nb2_lite'),
             tr('image_provider_openai')])
        self.image_provider_admin_toggle.set_value(image_provider_admin())
        self.image_provider_admin_toggle.valueChanged.connect(
            self._on_image_provider_admin_changed)
        pf.addWidget(self.image_provider_admin_toggle)

        # ── Две карточки рядом ──
        providers_row = QHBoxLayout()
        providers_row.setSpacing(12)
        provider_actors_frame.setMinimumWidth(280)
        provider_admin_frame.setMinimumWidth(280)
        providers_row.addWidget(provider_actors_frame, stretch=1)
        providers_row.addWidget(provider_admin_frame, stretch=1)
        lay.addLayout(providers_row)

        # ── 🎬 РЕЖИМ МОНТАЖНОЙ КАРТЫ — переключатель A/B/C/D (виден всем) ──
        self.sec_montage_mode_lbl = QLabel(tr('sec_montage_mode'))
        self.sec_montage_mode_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_montage_mode_lbl)

        mm_frame = QFrame()
        mm_frame.setObjectName("settings-group")
        mmf = QVBoxLayout(mm_frame)
        mmf.setSpacing(0)
        mmf.setContentsMargins(18, 14, 18, 14)

        self.montage_mode_hint_lbl = QLabel(tr('montage_mode_hint'))
        self.montage_mode_hint_lbl.setWordWrap(True)
        self.montage_mode_hint_lbl.setStyleSheet(
            "color:#aaa; font-size:12px; padding-bottom:10px;")
        mmf.addWidget(self.montage_mode_hint_lbl)

        # 2026-06-16 (Коммит 4): выбор режима — N-сегментный контрол
        # ModeSegment (ванна+пилюля, кастомный paintEvent) вместо QComboBox.
        # Кастомная отрисовка, т.к. нативный macOS-скин QPushButton перебивает
        # и QSS :checked, и property [active] (см. widgets/mode_segment.py).
        # Логика выбора (рестарт-диалог, mode_loader) не меняется — только вид.
        self.montage_mode_segment = ModeSegment(self)
        self.montage_mode_segment.set_options(list(mode_loader.VALID_MODES))
        self.montage_mode_segment.set_labels(
            [f"Mode {m.upper()}" for m in mode_loader.VALID_MODES])
        self.montage_mode_segment.set_value(mode_loader.get_current_mode())
        self.montage_mode_segment.valueChanged.connect(
            self._on_montage_mode_changed)
        mmf.addWidget(self.montage_mode_segment)

        lay.addWidget(mm_frame)

        # ── 🎙 СКОРОСТЬ РЕЧИ АКТЁРОВ — слайдеры (для режимов B и C) ─────
        # 2026-05-23 (Этап 3.2): live-крутка fast/normal/slow для речи
        # в режимах B и C (C — копия B). Значения пишутся в QSettings через
        # set_speech_speed_b_* (Этап 3.1). Подключение к SPEECH_CONFIG —
        # Этап 3.3. Гейтинг через mode_loader.get_current_mode(): в A/D
        # секция вообще не создаётся (атрибутов self.* нет — retranslate
        # пропускает блок через hasattr).
        if mode_loader.get_current_mode() in ('b', 'c'):
            from PyQt6.QtWidgets import QSlider

            self.sec_speech_speed_b_lbl = QLabel(tr('sec_speech_speed_b'))
            self.sec_speech_speed_b_lbl.setObjectName("settings-section")
            lay.addWidget(self.sec_speech_speed_b_lbl)

            speech_speed_b_frame = QFrame()
            speech_speed_b_frame.setObjectName("settings-group")
            ssb_lay = QVBoxLayout(speech_speed_b_frame)
            ssb_lay.setContentsMargins(18, 14, 18, 14)
            ssb_lay.setSpacing(10)

            self.speech_speed_b_hint_lbl = QLabel(tr('speech_speed_b_hint'))
            self.speech_speed_b_hint_lbl.setWordWrap(True)
            self.speech_speed_b_hint_lbl.setStyleSheet(
                "color:#aaa; font-size:12px; padding-bottom:10px;")
            ssb_lay.addWidget(self.speech_speed_b_hint_lbl)

            # Row Fast (3.0–6.0, default 4.0)
            fast_row = QHBoxLayout()
            fast_row.setSpacing(12)
            self.speech_speed_b_fast_label_lbl = QLabel(
                tr('speech_speed_b_fast_label'))
            self.speech_speed_b_fast_label_lbl.setMinimumWidth(220)
            self.speech_speed_b_fast_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            fast_row.addWidget(self.speech_speed_b_fast_label_lbl)

            self.speech_speed_b_fast_slider = QSlider(Qt.Orientation.Horizontal)
            self.speech_speed_b_fast_slider.setRange(30, 60)
            self.speech_speed_b_fast_slider.setSingleStep(1)
            self.speech_speed_b_fast_slider.setPageStep(5)
            self.speech_speed_b_fast_slider.setValue(
                int(round(speech_speed_b_fast() * 10)))
            block_wheel_event(self.speech_speed_b_fast_slider)
            self.speech_speed_b_fast_slider.valueChanged.connect(
                self._on_speech_speed_b_fast_changed)
            fast_row.addWidget(self.speech_speed_b_fast_slider, stretch=1)

            self.speech_speed_b_fast_value_lbl = QLabel("")
            self.speech_speed_b_fast_value_lbl.setStyleSheet(
                "color:#ffd24d; font-size:13px; font-weight:600; "
                "min-width:170px;")
            fast_row.addWidget(self.speech_speed_b_fast_value_lbl)

            self.speech_speed_b_fast_reset_btn = QPushButton(
                tr('speech_speed_b_reset'))
            self.speech_speed_b_fast_reset_btn.setFixedHeight(30)
            self.speech_speed_b_fast_reset_btn.clicked.connect(
                self._on_speech_speed_b_fast_reset)
            fast_row.addWidget(self.speech_speed_b_fast_reset_btn)
            ssb_lay.addLayout(fast_row)

            # Row Normal (2.0–5.0, default 3.5)
            normal_row = QHBoxLayout()
            normal_row.setSpacing(12)
            self.speech_speed_b_normal_label_lbl = QLabel(
                tr('speech_speed_b_normal_label'))
            self.speech_speed_b_normal_label_lbl.setMinimumWidth(220)
            self.speech_speed_b_normal_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            normal_row.addWidget(self.speech_speed_b_normal_label_lbl)

            self.speech_speed_b_normal_slider = QSlider(Qt.Orientation.Horizontal)
            self.speech_speed_b_normal_slider.setRange(20, 50)
            self.speech_speed_b_normal_slider.setSingleStep(1)
            self.speech_speed_b_normal_slider.setPageStep(5)
            self.speech_speed_b_normal_slider.setValue(
                int(round(speech_speed_b_normal() * 10)))
            block_wheel_event(self.speech_speed_b_normal_slider)
            self.speech_speed_b_normal_slider.valueChanged.connect(
                self._on_speech_speed_b_normal_changed)
            normal_row.addWidget(self.speech_speed_b_normal_slider, stretch=1)

            self.speech_speed_b_normal_value_lbl = QLabel("")
            self.speech_speed_b_normal_value_lbl.setStyleSheet(
                "color:#ffd24d; font-size:13px; font-weight:600; "
                "min-width:170px;")
            normal_row.addWidget(self.speech_speed_b_normal_value_lbl)

            self.speech_speed_b_normal_reset_btn = QPushButton(
                tr('speech_speed_b_reset'))
            self.speech_speed_b_normal_reset_btn.setFixedHeight(30)
            self.speech_speed_b_normal_reset_btn.clicked.connect(
                self._on_speech_speed_b_normal_reset)
            normal_row.addWidget(self.speech_speed_b_normal_reset_btn)
            ssb_lay.addLayout(normal_row)

            # Row Slow (1.0–4.0, default 2.3)
            slow_row = QHBoxLayout()
            slow_row.setSpacing(12)
            self.speech_speed_b_slow_label_lbl = QLabel(
                tr('speech_speed_b_slow_label'))
            self.speech_speed_b_slow_label_lbl.setMinimumWidth(220)
            self.speech_speed_b_slow_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            slow_row.addWidget(self.speech_speed_b_slow_label_lbl)

            self.speech_speed_b_slow_slider = QSlider(Qt.Orientation.Horizontal)
            self.speech_speed_b_slow_slider.setRange(10, 40)
            self.speech_speed_b_slow_slider.setSingleStep(1)
            self.speech_speed_b_slow_slider.setPageStep(5)
            self.speech_speed_b_slow_slider.setValue(
                int(round(speech_speed_b_slow() * 10)))
            block_wheel_event(self.speech_speed_b_slow_slider)
            self.speech_speed_b_slow_slider.valueChanged.connect(
                self._on_speech_speed_b_slow_changed)
            slow_row.addWidget(self.speech_speed_b_slow_slider, stretch=1)

            self.speech_speed_b_slow_value_lbl = QLabel("")
            self.speech_speed_b_slow_value_lbl.setStyleSheet(
                "color:#ffd24d; font-size:13px; font-weight:600; "
                "min-width:170px;")
            slow_row.addWidget(self.speech_speed_b_slow_value_lbl)

            self.speech_speed_b_slow_reset_btn = QPushButton(
                tr('speech_speed_b_reset'))
            self.speech_speed_b_slow_reset_btn.setFixedHeight(30)
            self.speech_speed_b_slow_reset_btn.clicked.connect(
                self._on_speech_speed_b_slow_reset)
            slow_row.addWidget(self.speech_speed_b_slow_reset_btn)
            ssb_lay.addLayout(slow_row)

            lay.addWidget(speech_speed_b_frame)

            # Инициализация подписей значений
            self._refresh_speech_speed_b_fast_value()
            self._refresh_speech_speed_b_normal_value()
            self._refresh_speech_speed_b_slow_value()

        # 2026-06-06 (Mode C): версий на шот / блоков параллельно.
        # Секция видна ТОЛЬКО в режиме C (гейт по get_current_mode).
        if mode_loader.get_current_mode() == 'c':
            self.sec_mode_c_lbl = QLabel(tr('sec_mode_c_settings'))
            self.sec_mode_c_lbl.setObjectName("settings-section")
            lay.addWidget(self.sec_mode_c_lbl)

            mode_c_frame = QFrame()
            mode_c_frame.setObjectName("settings-group")
            mc_lay = QVBoxLayout(mode_c_frame)

            # 2026-06-16 (Коммит 5): два поля в ДВЕ КОЛОНКИ рядом (по макету).
            # В каждой колонке: подпись СВЕРХУ → spin под ней → hint под полем.
            # Логика полей не меняется — только раскладка. Имена виджетов те же.
            fields_row = QHBoxLayout()
            fields_row.setSpacing(16)

            # ── Левая колонка: «Версий на шот» ──
            self.mode_c_versions_label_lbl = QLabel(
                tr('mode_c_versions_per_shot_label'))
            self.mode_c_versions_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            # 2026-06-16 (Коммит 5b): QSpinBox → степпер [− N +] (ручной ввод
            # запрещён, только кнопки). Логика сохранения не меняется.
            self.mode_c_versions_stepper = Stepper()
            self.mode_c_versions_stepper.set_range(1, 10)
            self.mode_c_versions_stepper.set_value(mode_c_versions_per_shot())
            self.mode_c_versions_stepper.valueChanged.connect(
                set_mode_c_versions_per_shot)
            self.mode_c_versions_hint_lbl = QLabel(
                tr('mode_c_versions_per_shot_hint'))
            self.mode_c_versions_hint_lbl.setWordWrap(True)
            self.mode_c_versions_hint_lbl.setStyleSheet(
                "color:rgba(255,255,255,0.45); font-size:11px;")

            col_versions = QVBoxLayout()
            col_versions.setSpacing(4)
            col_versions.addWidget(self.mode_c_versions_label_lbl)
            col_versions.addWidget(self.mode_c_versions_stepper)
            col_versions.addWidget(self.mode_c_versions_hint_lbl)
            col_versions_widget = QWidget()
            col_versions_widget.setMinimumWidth(260)
            col_versions_widget.setLayout(col_versions)
            fields_row.addWidget(col_versions_widget, stretch=1)

            # ── Правая колонка: «Шотов одновременно» ──
            self.mode_c_parallel_label_lbl = QLabel(
                tr('mode_c_parallel_blocks_label'))
            self.mode_c_parallel_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            self.mode_c_parallel_stepper = Stepper()
            self.mode_c_parallel_stepper.set_range(1, 20)
            self.mode_c_parallel_stepper.set_value(mode_c_concurrent_shots())
            self.mode_c_parallel_stepper.valueChanged.connect(
                set_mode_c_concurrent_shots)
            self.mode_c_parallel_hint_lbl = QLabel(
                tr('mode_c_parallel_blocks_hint'))
            self.mode_c_parallel_hint_lbl.setWordWrap(True)
            self.mode_c_parallel_hint_lbl.setStyleSheet(
                "color:rgba(255,255,255,0.45); font-size:11px;")

            col_parallel = QVBoxLayout()
            col_parallel.setSpacing(4)
            col_parallel.addWidget(self.mode_c_parallel_label_lbl)
            col_parallel.addWidget(self.mode_c_parallel_stepper)
            col_parallel.addWidget(self.mode_c_parallel_hint_lbl)
            col_parallel_widget = QWidget()
            col_parallel_widget.setMinimumWidth(260)
            col_parallel_widget.setLayout(col_parallel)
            fields_row.addWidget(col_parallel_widget, stretch=1)

            mc_lay.addLayout(fields_row)

            lay.addWidget(mode_c_frame)

        # ── АДМИН-СЕКЦИИ: видны только админу (`_is_admin`) ─────────────────
        # Большая визуальная разделительная плашка чтобы юзер сразу понимал
        # что ниже — настройки которые НЕ уйдут к коллегам (у них этих
        # секций просто не существует — ветка if self._is_admin не сработает).
        if self._is_admin:
            # 2026-06-16 (Коммит 6): все админ-блоки обёрнуты в ОДНУ красную
            # рамку #admin-zone с бейджем «АДМИН-НАСТРОЙКИ» первой строкой внутри
            # (по макету). Старая оранжевая разделительная линия убрана — рамка
            # её заменяет. Гейтинг _is_admin и содержимое блоков не меняются.
            lay.addSpacing(8)
            admin_zone = QFrame()
            admin_zone.setObjectName("admin-zone")
            az = QVBoxLayout(admin_zone)
            az.setContentsMargins(16, 14, 16, 16)
            az.setSpacing(10)
            # Лёгкое красное свечение вокруг рамки (box-shadow в QSS Qt не
            # поддерживает надёжно → через QGraphicsDropShadowEffect).
            from PyQt6.QtWidgets import QGraphicsDropShadowEffect as _QGDSE
            _admin_glow = _QGDSE(admin_zone)
            _admin_glow.setBlurRadius(24)
            _admin_glow.setColor(theme_qcolor("rgba(228,52,74,0.55)"))
            _admin_glow.setOffset(0, 0)
            admin_zone.setGraphicsEffect(_admin_glow)

            self.sec_admin_div_lbl = QLabel(tr('sec_admin_divider'))
            self.sec_admin_div_lbl.setObjectName("admin-zone-badge")
            # WA_StyledBackground — чтобы QSS-фон плашки прокрасился на QLabel.
            self.sec_admin_div_lbl.setAttribute(
                Qt.WidgetAttribute.WA_StyledBackground, True)
            # Компактная плашка по размеру текста, выровнена влево (не на всю ширину).
            self.sec_admin_div_lbl.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            az.addWidget(self.sec_admin_div_lbl, alignment=Qt.AlignmentFlag.AlignLeft)

            # ── 🧠 МОНТАЖКА — модели и таймауты (v1.0.86 этап 6) ───────
            # Три runtime-настройки оркестратора:
            #  • montage/opus_effort  — режим Opus thinking (low..max)
            #  • montage/chunk_timeout_opus_sec  — timeout для Opus этапов
            #  • montage/chunk_timeout_default_sec — для Haiku/Sonnet
            # Сохраняются в QSettings, читаются EpisodeChatView._on_montage_start
            # и передаются в MontageOrchestratorThread. Применяются при
            # перезапуске Studio (поток создаётся заново на каждый клик).
            self.sec_montage_runtime_lbl = QLabel(tr('sec_montage_runtime'))
            self.sec_montage_runtime_lbl.setObjectName("settings-section")
            az.addWidget(self.sec_montage_runtime_lbl)

            montage_rt_frame = QFrame()
            montage_rt_frame.setObjectName("settings-group")
            mrf = QVBoxLayout(montage_rt_frame)
            mrf.setSpacing(10)
            mrf.setContentsMargins(20, 18, 20, 18)

            self.montage_runtime_hint_lbl = QLabel(tr('montage_runtime_hint'))
            self.montage_runtime_hint_lbl.setWordWrap(True)
            self.montage_runtime_hint_lbl.setStyleSheet(
                "color:#aaa; font-size:12px; padding-bottom:6px;")
            mrf.addWidget(self.montage_runtime_hint_lbl)

            # Подгружаем текущие значения из QSettings.
            _qs_mrt = QSettings(APP_ORG, APP_NAME)
            cur_effort = _qs_mrt.value(
                "montage/opus_effort", "low", type=str)
            if cur_effort not in ("low", "medium", "high", "xhigh", "max"):
                cur_effort = "low"
            try:
                cur_timeout_opus = int(_qs_mrt.value(
                    "montage/chunk_timeout_opus_sec", 150))
            except (TypeError, ValueError):
                cur_timeout_opus = 150
            try:
                cur_timeout_default = int(_qs_mrt.value(
                    "montage/chunk_timeout_default_sec", 60))
            except (TypeError, ValueError):
                cur_timeout_default = 60

            from PyQt6.QtWidgets import QComboBox as _QCB, QSpinBox as _QSB

            # Строка 1: режим Opus thinking
            opus_row = QHBoxLayout()
            opus_row.setSpacing(12)
            self.montage_opus_effort_label_lbl = QLabel(
                tr('montage_opus_effort_label'))
            self.montage_opus_effort_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            opus_row.addWidget(self.montage_opus_effort_label_lbl)
            self.montage_opus_effort_combo = _QCB()
            for val in ("low", "medium", "high", "xhigh", "max"):
                self.montage_opus_effort_combo.addItem(
                    tr(f'montage_opus_effort_{val}'), val)
            _idx = self.montage_opus_effort_combo.findData(cur_effort)
            if _idx >= 0:
                self.montage_opus_effort_combo.setCurrentIndex(_idx)
            block_wheel_event(self.montage_opus_effort_combo)
            opus_row.addWidget(self.montage_opus_effort_combo, stretch=1)
            mrf.addLayout(opus_row)
            self.montage_opus_effort_hint_lbl = QLabel(
                tr('montage_opus_effort_hint'))
            self.montage_opus_effort_hint_lbl.setWordWrap(True)
            self.montage_opus_effort_hint_lbl.setStyleSheet(
                "color:rgba(255,255,255,0.45); font-size:11px;")
            mrf.addWidget(self.montage_opus_effort_hint_lbl)

            # Строка 2: chunk timeout для Opus
            ct_opus_row = QHBoxLayout()
            ct_opus_row.setSpacing(12)
            self.montage_chunk_timeout_opus_label_lbl = QLabel(
                tr('montage_chunk_timeout_opus_label'))
            self.montage_chunk_timeout_opus_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            ct_opus_row.addWidget(self.montage_chunk_timeout_opus_label_lbl)
            self.montage_chunk_timeout_opus_spin = _QSB()
            self.montage_chunk_timeout_opus_spin.setMinimum(30)
            self.montage_chunk_timeout_opus_spin.setMaximum(600)
            self.montage_chunk_timeout_opus_spin.setSingleStep(10)
            self.montage_chunk_timeout_opus_spin.setValue(cur_timeout_opus)
            block_wheel_event(self.montage_chunk_timeout_opus_spin)
            ct_opus_row.addWidget(self.montage_chunk_timeout_opus_spin, stretch=1)
            mrf.addLayout(ct_opus_row)
            self.montage_chunk_timeout_opus_hint_lbl = QLabel(
                tr('montage_chunk_timeout_opus_hint'))
            self.montage_chunk_timeout_opus_hint_lbl.setWordWrap(True)
            self.montage_chunk_timeout_opus_hint_lbl.setStyleSheet(
                "color:rgba(255,255,255,0.45); font-size:11px;")
            mrf.addWidget(self.montage_chunk_timeout_opus_hint_lbl)

            # Строка 3: chunk timeout для Haiku/Sonnet
            ct_def_row = QHBoxLayout()
            ct_def_row.setSpacing(12)
            self.montage_chunk_timeout_default_label_lbl = QLabel(
                tr('montage_chunk_timeout_default_label'))
            self.montage_chunk_timeout_default_label_lbl.setStyleSheet(
                "color:#cfcfcf; font-size:13px;")
            ct_def_row.addWidget(self.montage_chunk_timeout_default_label_lbl)
            self.montage_chunk_timeout_default_spin = _QSB()
            self.montage_chunk_timeout_default_spin.setMinimum(30)
            self.montage_chunk_timeout_default_spin.setMaximum(600)
            self.montage_chunk_timeout_default_spin.setSingleStep(10)
            self.montage_chunk_timeout_default_spin.setValue(cur_timeout_default)
            block_wheel_event(self.montage_chunk_timeout_default_spin)
            ct_def_row.addWidget(self.montage_chunk_timeout_default_spin, stretch=1)
            mrf.addLayout(ct_def_row)
            self.montage_chunk_timeout_default_hint_lbl = QLabel(
                tr('montage_chunk_timeout_default_hint'))
            self.montage_chunk_timeout_default_hint_lbl.setWordWrap(True)
            self.montage_chunk_timeout_default_hint_lbl.setStyleSheet(
                "color:rgba(255,255,255,0.45); font-size:11px;")
            mrf.addWidget(self.montage_chunk_timeout_default_hint_lbl)

            # Кнопка «Сохранить и применить» + warning
            self.montage_runtime_save_btn = QPushButton(
                tr('montage_runtime_save_button'))
            self.montage_runtime_save_btn.setCursor(
                Qt.CursorShape.PointingHandCursor)
            self.montage_runtime_save_btn.clicked.connect(
                self._on_montage_runtime_save_clicked)
            mrf.addWidget(self.montage_runtime_save_btn)

            self.montage_runtime_restart_hint_lbl = QLabel(
                tr('montage_runtime_restart_hint'))
            self.montage_runtime_restart_hint_lbl.setStyleSheet(
                "color: rgba(255,255,255,0.45); font-size: 11px;"
                " padding-top: 2px;")
            self.montage_runtime_restart_hint_lbl.setWordWrap(True)
            mrf.addWidget(self.montage_runtime_restart_hint_lbl)

            az.addWidget(montage_rt_frame)

            # ── АНИМАЦИИ — слайдер скорости fade-переходов ─────────────────
            self.sec_anim_lbl = QLabel(tr('sec_anim'))
            self.sec_anim_lbl.setObjectName("settings-section")
            az.addWidget(self.sec_anim_lbl)

            anim_frame = QFrame()
            anim_frame.setObjectName("settings-group")
            anf = QVBoxLayout(anim_frame)
            anf.setSpacing(0)
            anf.setContentsMargins(18, 14, 18, 14)

            self.anim_hint_lbl = QLabel(tr('anim_speed_hint'))
            self.anim_hint_lbl.setWordWrap(True)
            self.anim_hint_lbl.setStyleSheet(
                "color:#aaa; font-size:12px; padding-bottom:10px;")
            anf.addWidget(self.anim_hint_lbl)

            anim_row = QHBoxLayout()
            anim_row.setSpacing(12)
            self.anim_speed_label_lbl = QLabel(tr('anim_speed_label'))
            self.anim_speed_label_lbl.setStyleSheet("color:#cfcfcf; font-size:13px;")
            anim_row.addWidget(self.anim_speed_label_lbl)

            from PyQt6.QtWidgets import QSlider
            self.anim_speed_slider = QSlider(Qt.Orientation.Horizontal)
            self.anim_speed_slider.setMinimum(50)     # 0.5×
            self.anim_speed_slider.setMaximum(1000)   # 10.0× (~2800мс на табах)
            self.anim_speed_slider.setSingleStep(10)  # шаг 0.1×
            self.anim_speed_slider.setPageStep(50)
            try:
                cur = float(QSettings(APP_ORG, APP_NAME).value(
                    "anim_speed_multiplier", 1.5))
                self.anim_speed_slider.setValue(int(round(cur * 100)))
            except Exception:
                self.anim_speed_slider.setValue(150)
            self.anim_speed_slider.valueChanged.connect(self._on_anim_speed_changed)
            # КРИТИЧНО: блокируем колесо мыши, иначе прокрутка страницы Настроек
            # курсором над слайдером МЕНЯЕТ скорость анимаций — это раздражает.
            # Правило: ВСЕ настройки управляются ТОЛЬКО кликом/drag, не колесом.
            block_wheel_event(self.anim_speed_slider)
            anim_row.addWidget(self.anim_speed_slider, stretch=1)

            # Текущее значение «1.5× (~360мс на табах)»
            self.anim_speed_value_lbl = QLabel("")
            self.anim_speed_value_lbl.setStyleSheet(
                "color:#ffd24d; font-size:13px; font-weight:600; min-width:120px;")
            anim_row.addWidget(self.anim_speed_value_lbl)

            self.anim_speed_reset_btn = QPushButton(tr('anim_speed_reset'))
            self.anim_speed_reset_btn.setFixedHeight(30)
            self.anim_speed_reset_btn.clicked.connect(self._on_anim_speed_reset)
            anim_row.addWidget(self.anim_speed_reset_btn)
            anf.addLayout(anim_row)

            # Сразу показать текущее значение в подписи
            self._refresh_anim_speed_value()

            az.addWidget(anim_frame)

            # ── Админ: отправить обновление + статистика ──────────────────
            admin_frame = QFrame()
            admin_frame.setObjectName("admin-send-frame")
            ai = QVBoxLayout(admin_frame)
            ai.setSpacing(0)
            ai.setContentsMargins(20, 18, 20, 18)

            self.send_update_title_lbl = QLabel(tr('send_update_title'))
            self.send_update_title_lbl.setObjectName("admin-send-title")
            ai.addWidget(self.send_update_title_lbl)

            ai.addSpacing(4)
            self.send_update_desc_lbl = QLabel(tr('send_update_desc'))
            self.send_update_desc_lbl.setObjectName("admin-send-desc")
            self.send_update_desc_lbl.setWordWrap(True)
            ai.addWidget(self.send_update_desc_lbl)

            ai.addSpacing(16)
            self.send_update_btn = QPushButton(tr('send_update_btn'))
            self.send_update_btn.setObjectName("admin-send")
            self.send_update_btn.clicked.connect(self._send_update)
            ai.addWidget(self.send_update_btn)

            ai.addSpacing(12)
            self.stats_label = QLabel(tr('status_loading_stats'))
            self.stats_label.setObjectName("stats-label")
            self.stats_label.setWordWrap(True)
            ai.addWidget(self.stats_label)
            az.addWidget(admin_frame)

            # Вся админ-зона — одной красной рамкой в главный layout.
            lay.addWidget(admin_zone)

        lay.addStretch()
        self._refresh_settings_versions()
        scroll.setWidget(w)
        outer_lay.addWidget(scroll)
        return outer

    def _refresh_settings_versions(self):
        """Обновляет тексты версии Studio в настройках + цифру в шапке."""
        v_app  = read_local_app_version(self._project_root)
        if hasattr(self, 'app_ver_key_lbl'):
            self.app_ver_key_lbl.setText(tr('app_version'))
        if hasattr(self, 'app_ver_val_lbl'):
            self.app_ver_val_lbl.setText(f"v{v_app}")
        if hasattr(self, 'header_version'):
            self.header_version.setText(f"v{v_app}")

    # ── Shows / Episodes / Blocks ────────────────────────────────────────────

    def _populate_shows(self):
        """Заполняет дропдаун сериалов и выбирает активный.

        Текст пункта = display_name из shows/<slug>/meta.json (или title-case
        slug если meta.json нет — для legacy сериалов). userData = slug,
        он используется для путей и записи в current_show.json.
        """
        self.show_combo.blockSignals(True)
        self.show_combo.clear()
        shows = list_shows(self._project_root)
        for slug in shows:
            display = show_manager.display_name_for(self._project_root, slug)
            self.show_combo.addItem(display, userData=slug)
        if self._current_show and self._current_show in shows:
            idx = self.show_combo.findData(self._current_show)
            if idx >= 0:
                self.show_combo.setCurrentIndex(idx)
        self.show_combo.setEnabled(bool(shows))
        if not shows:
            self.show_combo.addItem(tr('no_shows'), userData=None)
        self.show_combo.blockSignals(False)
        self._refresh_compact_nav()

    def _set_nav_button_text(self, btn: QPushButton, prefix: str, value: str = "", empty: bool = False):
        """Small helper for compact editor navigation buttons."""
        if btn is None:
            return
        text = f"{value} ▾" if value else "— ▾"
        btn.setText(text)
        btn.setProperty("empty", bool(empty))
        btn.style().unpolish(btn); btn.style().polish(btn)

    def _episode_display_label(self, ep: Optional[str]) -> str:
        if not ep:
            return ""
        m = re.match(r'ep(\d+)', ep)
        return f"{int(m.group(1)):02d}" if m else ep

    def _block_display_label(self, block_name: Optional[str]) -> str:
        if not block_name:
            return ""
        m = re.match(r'.*_block_(\d+)', block_name)
        return f"{tr('block')} {m.group(1)}" if m else block_name

    def _refresh_compact_nav(self):
        """Refreshes compact selector labels without changing selection."""
        if not getattr(self, '_compact_nav_enabled', False):
            return
        try:
            show_display = ""
            if self._current_show:
                show_display = show_manager.display_name_for(
                    self._project_root, self._current_show)
            self._set_nav_button_text(
                getattr(self, 'show_select_btn', None),
                tr('series').rstrip(':'),
                show_display or tr('no_shows'),
                empty=not bool(self._current_show),
            )
            self._set_nav_button_text(
                getattr(self, 'ep_select_btn', None),
                tr('episode').rstrip(':'),
                self._episode_display_label(getattr(self, '_current_episode', None))
                    or tr('pill_new_episode_title'),
                empty=not bool(getattr(self, '_current_episode', None)),
            )
            self._set_nav_button_text(
                getattr(self, 'block_select_btn', None),
                tr('block'),
                self._block_display_label(getattr(self, 'current_block', None))
                    or "—",
                empty=not bool(getattr(self, 'current_block', None)),
            )
            if hasattr(self, 'nav_show_label'):
                self.nav_show_label.setText(tr('series').rstrip(':'))
            if hasattr(self, 'nav_ep_label'):
                self.nav_ep_label.setText(tr('episode').rstrip(':'))
            if hasattr(self, 'nav_block_label'):
                self.nav_block_label.setText(tr('block'))
            if hasattr(self, 'inline_block_label'):
                self.inline_block_label.setText(tr('block'))
            eps = list_episodes() if self._current_show else []
            ep_idx = eps.index(self._current_episode) if self._current_episode in eps else -1
            for btn_name, enabled in (
                    ('ep_prev_btn', ep_idx > 0),
                    ('ep_next_btn', ep_idx >= 0 and ep_idx < len(eps) - 1)):
                btn = getattr(self, btn_name, None)
                if btn is not None:
                    btn.setEnabled(enabled)
            blocks = (list_blocks_for_episode(self._current_episode)
                      if self._current_episode else [])
            blk_idx = (blocks.index(self.current_block)
                       if self.current_block in blocks else -1)
            for btn_name, enabled in (
                    ('block_prev_btn', blk_idx > 0),
                    ('block_next_btn', blk_idx >= 0 and blk_idx < len(blocks) - 1)):
                btn = getattr(self, btn_name, None)
                if btn is not None:
                    btn.setEnabled(enabled)
        except Exception:
            traceback.print_exc()

    def _step_episode(self, delta: int):
        eps = list_episodes() if self._current_show else []
        if not eps or self._current_episode not in eps:
            return
        idx = eps.index(self._current_episode) + delta
        if 0 <= idx < len(eps):
            self._select_episode(eps[idx])

    def _step_block(self, delta: int):
        if not self._current_episode:
            return
        blocks = list_blocks_for_episode(self._current_episode)
        if not blocks or self.current_block not in blocks:
            return
        idx = blocks.index(self.current_block) + delta
        if 0 <= idx < len(blocks):
            self._select_block(blocks[idx])

    def _show_nav_menu(self, anchor: QPushButton, width: int, title: str, builder):
        """Builds a QMenu with a custom QWidget body; it overlays the UI."""
        menu = QMenu(self)
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, False)
        menu.setStyleSheet(
            "QMenu { background: transparent; border: none; padding: 0; }")

        frame = QFrame()
        frame.setObjectName("nav-popup")
        frame.setMinimumWidth(width)
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(12)
        label = QLabel(title)
        label.setObjectName("nav-popup-title")
        v.addWidget(label)
        builder(v, menu)

        action = QWidgetAction(menu)
        action.setDefaultWidget(frame)
        menu.addAction(action)
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        menu.exec(pos)

    def _nav_item_wrap(self, btn: QPushButton, h_margin: int = 0, v_margin: int = 4) -> QWidget:
        wrap = QWidget()
        wrap.setObjectName("nav-popup-item-wrap")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(h_margin, v_margin, h_margin, v_margin)
        lay.setSpacing(0)
        lay.addWidget(btn)
        return wrap

    def _show_show_picker(self):
        shows = list_shows(self._project_root)

        def build(v, menu):
            # Плотный список сериалов: почти вплотную (было spacing=12 из
            # _show_nav_menu — для show-пикера ужимаем).
            v.setSpacing(4)
            for slug in shows:
                display = show_manager.display_name_for(self._project_root, slug)
                is_active = (slug == self._current_show)
                # Элемент = строка [кнопка-сериал (растянута)][✕ у НЕактивного].
                # Две отдельные кнопки → клики не конфликтуют (X не открывает сериал).
                row = QWidget()
                row.setObjectName("nav-popup-item-wrap")
                hb = QHBoxLayout(row)
                hb.setContentsMargins(0, 0, 0, 0)
                hb.setSpacing(6)
                btn = QPushButton(display)
                btn.setObjectName("nav-popup-item")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumHeight(38)
                btn.setProperty("active", is_active)
                btn.clicked.connect(
                    lambda _, s=slug: (menu.close(), self._on_show_changed(s)))
                hb.addWidget(btn, 1)
                if not is_active:
                    # Крестик удаления — только у НЕактивного сериала.
                    del_btn = QPushButton("✕")
                    del_btn.setObjectName("nav-popup-del")
                    del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    del_btn.setFixedSize(30, 38)
                    del_btn.setToolTip(tr('delete_show_tooltip'))
                    del_btn.clicked.connect(
                        lambda _, s=slug, d=display:
                        (menu.close(), self._on_delete_show(s, d)))
                    hb.addWidget(del_btn, 0)
                v.addWidget(row)
            add_btn = QPushButton("+ " + tr('new_show_btn_tooltip'))
            add_btn.setObjectName("nav-popup-add")
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setMinimumHeight(40)
            add_btn.clicked.connect(lambda: (menu.close(), self._on_new_show()))
            v.addWidget(self._nav_item_wrap(add_btn, v_margin=5))

        self._show_nav_menu(
            self.show_select_btn, 280, tr('series').rstrip(':'), build)

    def _on_delete_show(self, slug: str, display: str):
        """Клик по крестику удаления сериала. Confirm → show_manager.delete_show
        (бэкенд: rmtree папки shows/<slug>/). НЕ трогает current_show.json сам —
        активный сериал удалить нельзя (крестика у него нет + страховка тут)."""
        # Страховка: активный не удаляем (крестика у него и так нет).
        if slug == self._current_show:
            QMessageBox.warning(
                self, tr('delete_show_confirm_title'),
                tr('delete_show_active_guard'))
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr('delete_show_confirm_title'))
        box.setText(tr('delete_show_confirm_text', name=display))
        yes_btn = box.addButton(
            tr('delete_show_yes'), QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton(
            tr('delete_show_cancel'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        if box.clickedButton() is not yes_btn:
            return
        ok, reason = show_manager.delete_show(self._project_root, slug)
        if ok:
            # Список строится сканом shows/ → просто переоткрываем picker,
            # удалённого сериала там уже нет.
            self._show_show_picker()
        else:
            QMessageBox.critical(
                self, tr('delete_show_confirm_title'),
                tr('delete_show_error', reason=reason))

    def _show_episode_picker(self):
        eps = list_episodes() if self._current_show else []

        def build(v, menu):
            grid_w = QWidget()
            grid = QGridLayout(grid_w)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(10)
            cols = 6
            for i, ep in enumerate(eps):
                btn = QPushButton(self._episode_display_label(ep))
                btn.setObjectName("nav-popup-item")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumSize(62, 40)
                btn.setProperty("active", ep == self._current_episode)
                btn.clicked.connect(lambda _, e=ep: (menu.close(), self._select_episode(e)))
                grid.addWidget(self._nav_item_wrap(btn, v_margin=3), i // cols, i % cols)
            v.addWidget(grid_w)
            add_btn = QPushButton("+ " + tr('pill_new_episode_tip'))
            add_btn.setObjectName("nav-popup-add")
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.setMinimumHeight(42)
            add_btn.clicked.connect(lambda: (menu.close(), self._show_new_episode_view()))
            v.addWidget(self._nav_item_wrap(add_btn, v_margin=5))

        self._show_nav_menu(
            self.ep_select_btn, 320, tr('episode').rstrip(':'), build)

    def _show_block_picker(self):
        blocks = (list_blocks_for_episode(self._current_episode)
                  if self._current_episode else [])

        def build(v, menu):
            grid_w = QWidget()
            grid = QGridLayout(grid_w)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(10)
            cols = 4
            for i, blk in enumerate(blocks):
                btn = QPushButton(self._block_display_label(blk))
                btn.setObjectName("nav-popup-item")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumSize(88, 40)
                btn.setProperty("active", blk == self.current_block)
                btn.clicked.connect(lambda _, b=blk: (menu.close(), self._select_block(b)))
                grid.addWidget(self._nav_item_wrap(btn, v_margin=3), i // cols, i % cols)
            if not blocks:
                empty = QLabel("—")
                empty.setObjectName("nav-popup-title")
                grid.addWidget(empty, 0, 0)
            v.addWidget(grid_w)

        self._show_nav_menu(
            self.block_select_btn, 300, tr('block'), build)

    def _on_show_changed_idx(self, idx: int):
        """Слот currentIndexChanged. Извлекает slug из userData."""
        if idx < 0:
            return
        slug = self.show_combo.itemData(idx)
        if not slug or slug == self._current_show:
            return
        self._on_show_changed(slug)

    def _on_new_show(self):
        """Открывает диалог создания сериала, на accept создаёт структуру.

        Важно: после `create_show` нужно вручную выполнить тот же путь что
        и `_on_show_changed`: записать current_show.json, переключить
        пути и перерисовать вкладки. Если просто звать `setCurrentIndex` —
        Qt может не выпустить сигнал (если индекс уже совпадает) и
        активный сериал останется None → UI покажет «нет сериалов».
        """
        from views.new_show_dialog import NewShowDialog
        dlg = NewShowDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            slug = show_manager.create_show(self._project_root, dlg.display_name())
        except ValueError:
            return
        # 1. Записываем активный сериал явно — не полагаемся на сигнал combo.
        set_current_show(self._project_root, slug)
        # 2. Перерисовываем дропдаун (новый сериал появится).
        self._populate_shows()
        # 3. Выбираем его в combo — это ОБНОВИТ self._current_show через
        #    `_on_show_changed_idx` если индекс реально меняется. Если это
        #    единственный сериал и индекс уже 0 — сигнал не выйдет, поэтому
        #    делегируем напрямую.
        idx = self.show_combo.findData(slug)
        if idx >= 0:
            self.show_combo.blockSignals(True)
            self.show_combo.setCurrentIndex(idx)
            self.show_combo.blockSignals(False)
        # 4. Принудительно вызываем _on_show_changed чтобы обновить
        #    setup_paths_for_show, _meta, перерисовать эпизоды и блоки.
        self._on_show_changed(slug)
        # 5. Статус-бар сообщение
        try:
            self.status_bar.showMessage(
                tr('new_show_status_created', name=dlg.display_name().strip()),
                5000,
            )
        except Exception:
            pass

    def _on_scenario_file(self, path: Path):
        """Слот сигнала ScenarioDropZone.file_dropped.

        Читает файл, парсит на bible + episodes, сохраняет в shows/<slug>/,
        обновляет episodes.json (чтобы пилюли эпизодов появились), и
        перерисовывает UI.
        """
        if not self._current_show:
            try:
                self.status_bar.showMessage(tr('no_shows'), 5000)
            except Exception:
                pass
            return

        # 1. Читаем файл через scenario_parser — он сам выбирает реализацию
        #    в зависимости от расширения (.txt/.md/.rtf — plain text с
        #    fallback на cp1251; .docx — через python-docx).
        try:
            text = scenario_parser.read_scenario_file(path)
        except Exception as e:
            self.status_bar.showMessage(tr('scenario_load_error', error=str(e)), 8000)
            return

        # 2. Парсинг
        parsed = scenario_parser.parse_episodes_doc(text)
        if not parsed.episodes and not parsed.bible:
            self.status_bar.showMessage(tr('scenario_load_empty'), 8000)
            return
        if not parsed.episodes:
            # Только библия загружена — это валидно (юзер мог докинуть
            # библию отдельно). Показываем особое сообщение.
            scenario_parser.save_parsed_doc(self._project_root, self._current_show, parsed)
            self.status_bar.showMessage(
                tr('scenario_loaded_no_bible', n_episodes=0), 5000)
            return

        # 3. Сохранение на диск
        try:
            scenario_parser.save_parsed_doc(
                self._project_root, self._current_show, parsed)
        except Exception as e:
            self.status_bar.showMessage(tr('scenario_load_error', error=str(e)), 8000)
            return

        # 4. Архив сценариев (epNN.txt + bible.txt) уже сохранён в save_parsed_doc.
        #    episodes.json НЕ трогаем — пилюли эпизодов должны появляться
        #    только когда юзер реально начал работать над серией (через
        #    «+» → форма «Новый эпизод» → ввод номера → Запустить). До этого
        #    20 пустых пилюль визуально мешают.

        # 5. Перерисовываем UI — drop-зона может скрыться если scenarios/
        #    непуст, но пилюли остаются как были (только реально начатые).
        self._populate_episodes()

        # 6. Сообщение в статус-баре
        msg_key = ('scenario_loaded_with_bible' if parsed.bible
                   else 'scenario_loaded_no_bible')
        self.status_bar.showMessage(
            tr(msg_key, n_episodes=len(parsed.episodes)), 6000)

    def _on_show_changed(self, show_name: str):
        if not show_name or show_name == self._current_show:
            return
        if show_name == "(нет сериалов)":
            return
        self._current_show = show_name
        set_current_show(self._project_root, show_name)
        setup_paths_for_show(self._project_root, show_name)
        self._meta = read_episodes_meta(SHOW_ROOT)
        # Этап 1 (формат кадра): обновляем формат при смене сериала.
        self._current_aspect = show_manager.show_aspect(
            self._project_root, show_name)
        # Этап 2 (формат кадра): подсветка сегмента под новый сериал.
        if hasattr(self, '_aspect_seg_buttons'):
            self._refresh_aspect_segment()
        self.current_block = None
        self._current_episode = None
        # Перевешиваем file watcher на новый путь
        if hasattr(self, '_watcher'):
            self._watcher.removePaths(self._watcher.directories())
            self._watcher.addPath(str(STORYBOARDS_DIR))
        self._populate_episodes()
        # Перечитать холст Генератора под новый сериал (персистенция холста).
        # current_show.json уже содержит новый slug (записан выше в этом методе) →
        # reload_canvas прочитает холст нужного сериала. Guard в стиле метода.
        if hasattr(self, 'generator_view'):
            self.generator_view.reload_canvas()

    def _reload_show(self):
        """Перечитать эпизоды/блоки текущего сериала (после изменений на диске).
        Если юзер сейчас на экране Референсов — НЕ перебрасывать его на shots.
        """
        content_idx = (
            self.content_stack.currentIndex()
            if hasattr(self, 'content_stack') else 0
        )
        was_on_refs = content_idx == 1
        was_on_aux_view = content_idx in (1, 2)
        prev_eps = list(getattr(self, '_episode_pills', {}).keys())
        prev_blocks = list(getattr(self, '_block_pills', {}).keys())
        self._meta = read_episodes_meta(SHOW_ROOT) if self._current_show else {}

        # Storyboard watcher fires when a shot image appears on disk. If the
        # user is reading References/Chat and episode/block lists did not
        # change, rebuilding navigation would only cause a visible one-frame
        # blink without showing any new information.
        try:
            new_eps = list_episodes() if self._current_show else []
            new_blocks = (
                list_blocks_for_episode(self._current_episode)
                if self._current_episode else []
            )
            if was_on_aux_view and new_eps == prev_eps and new_blocks == prev_blocks:
                self._refresh_compact_nav()
                if hasattr(self, '_refs_watcher'):
                    self._wire_refs_watcher()
                return
        except Exception:
            pass

        self._populate_episodes()
        if was_on_refs and self._current_episode:
            self._show_refs_view()
        # Перепривязать watcher на актуальные refs-папки текущего сериала
        if hasattr(self, '_refs_watcher'):
            self._wire_refs_watcher()

    def _wire_refs_watcher(self):
        """Подписать `_refs_watcher` на актуальные refs-папки активного сериала
        и на episodes.json. Вызывается при инициализации и при смене сериала."""
        watcher = getattr(self, '_refs_watcher', None)
        if watcher is None:
            return
        # Снять старые
        old_dirs = watcher.directories()
        old_files = watcher.files()
        if old_dirs:
            watcher.removePaths(old_dirs)
        if old_files:
            watcher.removePaths(old_files)
        if not self._current_show:
            return
        # Добавить актуальные
        paths_to_watch = []
        for sub in ('locations', 'objects', 'characters'):
            p = LOCATIONS_DIR.parent / sub
            if p.exists():
                paths_to_watch.append(str(p))
        # episodes.json — отдельный файл-watcher, чтобы при появлении нового
        # эпизода через Claude дропдаун обновлялся
        ep_meta = SHOW_ROOT / "episodes.json"
        if ep_meta.exists():
            paths_to_watch.append(str(ep_meta))
        if paths_to_watch:
            watcher.addPaths(paths_to_watch)

    def _refresh_after_external_change(self):
        """Слот watcher'а refs/episodes.json. Перечитывает meta, обновляет
        дропдаун эпизодов и refs-view (если юзер на нём).

        ⚠ Важно: НЕ дёргаем `_populate_episodes()` если список эпизодов не
        изменился. Раньше каждый тик watcher'а перерисовывал пилюли → это
        вызывало `_select_episode` → `_populate_blocks` → `_select_block` →
        переключение `content_stack` обратно на shots-view. Юзера выкидывало
        с refs-view на блок при любом fileChanged в refs-папке.

        Debouncing для refs-view rebuild: если автономный агент пишет
        картинку + geometry-файл в фоне, watcher триггерится 2-3 раза
        подряд за секунду. Каждый ребилд — clear+create всех RefCard
        (тяжёлая операция). Юзер видит микро-фризы и скачок скролла
        вверх. Сжимаем все триггеры в один отложенный singleShot."""
        if not self._current_show:
            return
        try:
            self._meta = read_episodes_meta(SHOW_ROOT)
        except Exception:
            pass
        # Перерисовываем пилюли эпизодов ТОЛЬКО если список реально изменился —
        # иначе пересоздание сбрасывает active state и выкидывает юзера с refs.
        try:
            new_eps = list_episodes()
            cur_eps = list(getattr(self, '_episode_pills', {}).keys())
            if new_eps != cur_eps:
                self._populate_episodes()
        except Exception:
            pass
        # Перепривязать watcher (на случай если папки только что были созданы)
        self._wire_refs_watcher()
        # Refs-view перерисовать если юзер сейчас на нём — но через debounce
        on_refs = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
            and self._current_episode is not None
        )
        if on_refs:
            self._schedule_refs_rebuild()

    def _schedule_refs_rebuild(self):
        """Дебаунсер для `_build_refs_view` — собирает несколько триггеров
        watcher'а в один отложенный вызов через 250 мс. Защищает от
        каскадных ребилдов сетки рефов когда автономный агент пишет
        картинку + geometry подряд (юзер видит фризы и скачок скролла)."""
        if not hasattr(self, '_refs_rebuild_timer'):
            t = QTimer(self)
            t.setSingleShot(True)
            t.setInterval(250)
            t.timeout.connect(self._do_refs_rebuild)
            self._refs_rebuild_timer = t
        # Перезапускаем таймер — каждый новый сигнал отодвигает ребилд
        self._refs_rebuild_timer.start()

    def _refs_signature_from_refs(self, refs: Dict[str, List[Dict]]) -> tuple:
        """Stable signature of what References actually renders.

        The refs watcher also receives episodes.json writes from background
        storyboard generation. Those writes should not rebuild the References
        page unless the visible refs cards really changed.
        """
        sig = []
        for kind in ('locations', 'objects', 'characters'):
            for r in refs.get(kind, []) or []:
                if not isinstance(r, dict):
                    sig.append((kind, repr(r), '', '', -1, -1))
                    continue
                raw_path = r.get('path')
                path = Path(raw_path) if raw_path else None
                try:
                    if path is not None and path.exists():
                        st = path.stat()
                        path_sig = (str(path.resolve()), st.st_size, st.st_mtime_ns)
                    else:
                        path_sig = (str(raw_path or ''), -1, -1)
                except Exception:
                    path_sig = (str(raw_path or ''), -1, -1)
                sig.append((
                    kind,
                    str(r.get('name', '')),
                    str(r.get('filename', '')),
                    str(r.get('tag', '')),
                    path_sig,
                ))
        return tuple(sig)

    def _refs_view_signature(self, ep: str) -> Optional[tuple]:
        try:
            return self._refs_signature_from_refs(list_episode_refs(ep))
        except Exception:
            traceback.print_exc()
            return None

    def _do_refs_rebuild(self):
        """Реальный ребилд refs-view, вызывается дебаунсером.
        Дополнительная защита: если юзер уже ушёл с refs (или сменил
        эпизод) — ничего не делаем."""
        try:
            on_refs = (
                hasattr(self, 'content_stack')
                and self.content_stack.currentIndex() == 1
                and self._current_episode is not None
            )
            if on_refs:
                ep = self._current_episode
                new_sig = self._refs_view_signature(ep)
                sig_cache = getattr(self, '_refs_view_signatures', {})
                if new_sig is not None and sig_cache.get(ep) == new_sig:
                    return
                scroll_bar = None
                scroll_value = 0
                try:
                    scroll = getattr(self, 'refs_scroll', None)
                    scroll_bar = scroll.verticalScrollBar() if scroll else None
                    scroll_value = scroll_bar.value() if scroll_bar else 0
                except Exception:
                    scroll_bar = None
                self._build_refs_view(ep)
                if scroll_bar is not None:
                    def _restore_refs_scroll():
                        try:
                            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
                        except Exception:
                            pass
                    QTimer.singleShot(0, _restore_refs_scroll)
        except Exception:
            traceback.print_exc()

    def _is_blocks_view_active(self) -> bool:
        """True only when the storyboard shot cards page is visible."""
        return (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 0
        )

    def _clear_layout(self, layout):
        """Рекурсивно чистит layout: и виджеты, и вложенные layouts.
        2026-05-05: расширен для поддержки сетки пилюль (ep_pills_layout
        теперь VBoxLayout содержащий несколько HBoxLayout-строк)."""
        while layout.count():
            item = layout.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                self._clear_layout(sub)

    def _populate_episodes(self):
        """Перерисовывает ряд пилюль эпизодов активного сериала.

        Также управляет видимостью drop-зоны для загрузки документа
        со сценариями: видна только когда есть активный сериал и
        в нём пока нет эпизодов.
        """
        self._clear_layout(self.ep_pills_layout)
        self._episode_pills = {}

        if not self._current_show:
            self.ep_title_label.setText(tr('no_shows'))
            self.ep_title_label.setVisible(False)
            self.ep_dur_label.setText("")
            if hasattr(self, 'delete_ep_btn'):
                self.delete_ep_btn.setEnabled(False)
                self.delete_ep_btn.setVisible(False)
            if hasattr(self, 'compile_ep_btn'):
                self.compile_ep_btn.setVisible(False)
            # 2026-05-08 hotfix: scenario_drop_zone устанавливается в None
            # (для совместимости со старыми callers), поэтому hasattr=True
            # но значение None. Защита через is-not-None — иначе крах
            # на свежей установке без current_show (типичный сценарий
            # Win-коллег после первого запуска Studio.exe).
            if getattr(self, 'scenario_drop_zone', None) is not None:
                self.scenario_drop_zone.hide()  # без сериала прятаем
            self._populate_blocks()
            self._refresh_compact_nav()
            return

        eps = list_episodes()
        # 2026-05-05 v3: scenario_drop_zone теперь живёт внутри NewEpisodeView,
        # на стартовом экране её нет — здесь не управляем видимостью.

        # 2026-05-05: пилюли заполняем сеткой 13 в ряду — переход на
        # следующий ряд автоматически. Высота 28px (компактнее чем 32).
        EPS_PER_ROW = 13
        PILL_H = 28
        # 2026-05-08 редизайн: пилюли эпизодов сжаты — содержат только
        # двузначный номер «01»/«02»/«10». Изначально было 60, потом 44,
        # но юзер сообщил что визуально всё ещё слишком крупные. Сделал
        # ещё компактнее (38 + padding 4×8 в QSS) чтобы они выглядели
        # явно меньше блок-пилюль («Блок 1» ~85px ширины).
        PILL_W = 38

        def _make_row():
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.setAlignment(Qt.AlignmentFlag.AlignLeft)
            return row

        # v1.0.88 (индикатор failed эпизодов): EpisodePillButton — QPushButton
        # с paintEvent override, рисующий красную точку в правом верхнем
        # углу когда `_agent_log_<ep>.json` содержит pipeline_state.status
        # ∈ {"failed","running"} с непустым last_completed_stage. См.
        # `_episode_has_failed_pipeline` + `_refresh_episode_pill_indicators`.
        from widgets.episode_pill_button import EpisodePillButton

        current_row = _make_row()
        self.ep_pills_layout.addLayout(current_row)
        items_in_row = 0
        for ep in eps:
            m = re.match(r'ep(\d+)', ep)
            # 2026-05-08 редизайн Этап 3: пилюли эпизодов теперь содержат
            # ТОЛЬКО номер с ведущим нулём («01», «02», «10») — без префикса
            # «ЭП». Перед рядом пилюль есть отдельный лейбл «Эпизод:» (см.
            # `_build_editor_tab`), который и поясняет о чём речь.
            if m:
                n_label = f"{int(m.group(1)):02d}"
            else:
                n_label = ep
            btn = EpisodePillButton(n_label)
            btn.setObjectName("pill")
            btn.setFixedHeight(PILL_H)
            btn.setFixedWidth(PILL_W)
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _, e=ep: self._select_episode(e))
            current_row.addWidget(btn)
            self._episode_pills[ep] = btn
            items_in_row += 1
            # Перенос: после 13 пилюль создаём новую строку.
            # 2026-05-08: убран `addStretch(1)` — он делал sizeHint
            # контейнера бесконечным, из-за чего Maximum sizePolicy
            # не работал и плашка «1 серия» в ep_row не могла
            # сдвинуться вправо. Кнопки прижимаются к левому через
            # `row.setAlignment(AlignLeft)` в `_make_row()`.
            if items_in_row >= EPS_PER_ROW:
                current_row = _make_row()
                self.ep_pills_layout.addLayout(current_row)
                items_in_row = 0

        # «+» — пилюля создания нового эпизода. Кладём в текущую строку.
        # 2026-05-04: UX-переделка, заменяет отдельную вкладку «Новый эпизод».
        new_btn = QPushButton("+")
        new_btn.setObjectName("pill-new")
        new_btn.setFixedHeight(PILL_H)
        new_btn.setFixedWidth(PILL_W)
        new_btn.setToolTip(tr('pill_new_episode_tip'))
        new_btn.setProperty("active", False)
        new_btn.clicked.connect(self._show_new_episode_view)
        current_row.addWidget(new_btn)
        # 2026-05-08: убран `addStretch(1)` — `row.setAlignment(AlignLeft)`
        # уже прижимает кнопки к левому. Stretch делал контейнер
        # растягивающимся → блокировал перемещение «1 серия» вправо.
        self._new_ep_pill = new_btn

        if eps:
            prev = self._current_episode if self._current_episode in eps else eps[0]
            self._select_episode(prev)
        else:
            self._current_episode = None
            # Показываем юзеру display_name (то что он ввёл при создании),
            # а не slug (имя папки на латинице).
            self.ep_title_label.setText(tr(
                'no_episodes',
                show=show_manager.display_name_for(self._project_root, self._current_show),
            ))
            self.ep_title_label.setVisible(False)
            self.ep_dur_label.setText("")
            if hasattr(self, 'delete_ep_btn'):
                self.delete_ep_btn.setEnabled(False)
                self.delete_ep_btn.setVisible(False)
            if hasattr(self, 'compile_ep_btn'):
                self.compile_ep_btn.setVisible(False)
            self._populate_blocks()

        # v1.0.88 (индикатор failed эпизодов): после пересоздания пилюль —
        # сразу нарисовать точки для упавших монтажек. Иначе на cold-start
        # юзер увидит точки только после первого тика _pill_indicator_timer
        # (3с задержка).
        try:
            self._refresh_episode_pill_indicators()
        except Exception:
            traceback.print_exc()
        self._refresh_compact_nav()

    def _episode_pipeline_state(self, ep_id: str):
        """v1.0.88 (Stage 10): возвращает (state, last_completed_stage|None)
        для индикатора пилюли эпизода.

        Возможные state:
          • "failed"            — pipeline упал (status="failed" в логе)
                                  ИЛИ status="running" но тред мёртв
                                  (force-quit, cold-start). Точка красная
                                  мигающая.
          • "completed_unseen"  — карта готова в episodes.json + флаг
                                  `montage_card_seen` отсутствует / False.
                                  Точка зелёная статичная.
          • "running_alive"     — status="running" + тред в
                                  `episode_chat_view._montage_threads`
                                  активен. Точки нет.
          • "none"              — ни лога, ни карты, либо просмотрена.
                                  Точки нет.

        Возвращаемый stage используется только для "failed" (в tooltip).
        Для остальных state — None.
        """
        if not ep_id or not self._current_show:
            return ("none", None)

        # ── 1. Проверка _agent_log_<ep>.json
        try:
            log_path = (self._project_root / "shows" / self._current_show
                        / "output" / f"_agent_log_{ep_id}.json")
        except Exception:
            log_path = None

        status: Optional[str] = None
        last: Optional[str] = None
        if log_path is not None and log_path.exists():
            try:
                import json as _json
                data = _json.loads(log_path.read_text(encoding='utf-8')) or {}
                ps = data.get("pipeline_state")
                if isinstance(ps, dict):
                    status = ps.get("status")
                    last = ps.get("last_completed_stage")
            except Exception:
                # Битый JSON / race с atomic write — игнорируем лог,
                # пойдём дальше проверять episodes.json.
                pass

        # ── 2. status="failed" → красная (точка) сразу
        if status == "failed" and last and last != "finalize":
            return ("failed", last)

        # ── 3. status="running" → проверить живой ли тред
        # v1.0.88 (Stage 11 Bug 1 fix): relax guard — `last` может быть
        # None если orchestrator был убит ДО первого dump'а этапа
        # (early force-quit Studio в первые секунды после клика).
        # Точка должна гореть, но tooltip без stage placeholder'а
        # (см. _refresh_episode_pill_indicators failed-no-stage branch).
        if status == "running" and last != "finalize":
            ev = getattr(self, 'episode_chat_view', None)
            threads = (getattr(ev, '_montage_threads', None) or {}) if ev else {}
            t = threads.get(ep_id)
            alive = t is not None and t.isRunning()
            if alive:
                # Pipeline нормально работает — точки нет.
                return ("running_alive", None)
            # Тред мёртв (force-quit, краш Studio, или ev=None на
            # cold-start) — это де-факто failed. `last` может быть
            # None (early force-quit до scriptwriter dump'а).
            return ("failed", last)

        # ── 4. status="completed" / legacy лог / нет лога → проверить
        #       episodes.json: есть ли карта + просмотрена ли.
        # `read_episodes_meta` — локальная функция модуля
        # (определена в storyboard_app.py:632). Читаем свежие данные
        # с диска чтобы поймать только что записанный
        # montage_card_seen=True (например после клика «📂 Открыть»
        # в этой же сессии).
        try:
            meta = read_episodes_meta(
                self._project_root / "shows" / self._current_show)
        except Exception:
            meta = getattr(self, '_meta', None) or {}
        ep_meta = meta.get(ep_id) or {}
        card = ep_meta.get('montage_card') or {}
        if card.get('blocks'):
            # 2026-07-02: неполная карта — Editor не дозаполнил геометрию
            # (checker_report.ok=False). Лог может быть status="completed"
            # (Editor-краш не пишет "error"), но карта дефектна → красная
            # мигающая, НЕ зелёная. Только явный ok is False (legacy без
            # поля → None → не триггерит, поведение прежнее).
            cr = ep_meta.get('montage_checker_report') or {}
            if cr.get('ok') is False:
                return ("failed", "__incomplete__")
            seen = bool(ep_meta.get('montage_card_seen'))
            if not seen:
                return ("completed_unseen", None)
            # Просмотрена — точки не нужно.
            return ("none", None)

        # Карты в episodes.json нет — точки тоже не нужно. Если лог был
        # с legacy completed (без pipeline_state) → fallback "none"
        # (нечего показывать без production-карты).
        return ("none", None)

    def _refresh_episode_pill_indicators(self):
        """v1.0.88 (Stage 8/10): пробегает по всем `_episode_pills` и
        обновляет цветную точку через `set_state(state, stage,
        tooltip_template)`.

        Stage 8 (изначально): только красная failed-точка.
        Stage 10: 3-state система — failed (red blinking) /
                  completed_unseen (green static) / running_alive/none
                  (точки нет).

        Stage human-имя берётся через i18n `montage_stage_name_<stage>`
        (есть из этапа 7B, 3 локали); tooltip templates —
        `montage_pill_failed_tooltip` (red) и
        `montage_pill_completed_tooltip` (green). Если ключа нет —
        fallback на дефолтную фразу.

        EpisodePillButton сам управляет blink-таймером per-state →
        дёшево вызывать каждые 3с фоновым polling-таймером.
        """
        pills = getattr(self, '_episode_pills', None)
        if not pills:
            return
        # v1.0.88 (Stage 11 diag): собираем краткий summary всех state'ов
        # для одного stderr-лога в конце тика — иначе при 25 эпизодах
        # spam'илось бы 25 строк. По этому summary видно: «pill refresh:
        # 5 eps → {ep21: completed_unseen, ep22: running_alive, ...}».
        state_summary: dict = {}
        for ep_id, btn in pills.items():
            try:
                state, stage_id = self._episode_pipeline_state(ep_id)
                state_summary[ep_id] = state
                if state == "failed" and stage_id == "__incomplete__":
                    # 2026-07-02: карта собрана, но неполная (ok=False) —
                    # красная точка + tooltip «переделай».
                    tooltip_template = tr('montage_pill_incomplete_tooltip')
                    if tooltip_template == 'montage_pill_incomplete_tooltip':
                        tooltip_template = 'Карта неполная — переделай'
                    btn.set_state("failed",
                                   tooltip_template=tooltip_template)
                elif state == "failed" and stage_id:
                    stage_human = tr(f'montage_stage_name_{stage_id}')
                    if stage_human.startswith('montage_stage_name_'):
                        stage_human = stage_id
                    tooltip_template = tr('montage_pill_failed_tooltip')
                    if tooltip_template == 'montage_pill_failed_tooltip':
                        tooltip_template = 'Монтажка прервана на этапе {stage}'
                    btn.set_state("failed",
                                   stage=stage_human,
                                   tooltip_template=tooltip_template)
                elif state == "failed":
                    # v1.0.88 (Stage 11 Bug 1 fix): stage_id=None case —
                    # orchestrator упал ДО первого этапа dump'а (early
                    # force-quit). Точка нужна, но stage human-name не
                    # существует → tooltip без {stage} placeholder'а.
                    tooltip_template = tr('montage_pill_failed_tooltip_no_stage')
                    if tooltip_template == 'montage_pill_failed_tooltip_no_stage':
                        tooltip_template = 'Монтажка прервана'
                    btn.set_state("failed",
                                   tooltip_template=tooltip_template)
                elif state == "completed_unseen":
                    tooltip_template = tr('montage_pill_completed_tooltip')
                    if tooltip_template == 'montage_pill_completed_tooltip':
                        # Fallback default (русская — админ всегда поймёт).
                        tooltip_template = 'Монтажка готова — кликни чтобы открыть'
                    btn.set_state("completed_unseen",
                                   tooltip_template=tooltip_template)
                else:
                    # "running_alive" или "none" — точки нет.
                    btn.set_state(state)
            except Exception as e:
                # Сломалась одна пилюля — не валим остальные.
                state_summary[ep_id] = f"ERROR:{type(e).__name__}"
                traceback.print_exc()
                continue
        # v1.0.88 (Stage 11 diag): один сводный лог на тик. Активный
        # юзер в Console.app увидит изменение state'ов когда зелёная
        # должна была появиться, но не появилась — точная диагностика.
        # v1.0.88 (noise fix): пишем в лог ТОЛЬКО при реальном изменении
        # state'ов с прошлого тика. Раньше polling 3с генерил десятки
        # тысяч одинаковых "8 eps → all none" строк в _studio_diag.log
        # за рабочий день — юзер не мог найти реальные события.
        # `_last_pill_states` создаётся лениво как атрибут instance —
        # None при первом тике, потом dict с prev state_summary.
        prev_summary = getattr(self, '_last_pill_states', None)
        if state_summary != prev_summary:
            self._last_pill_states = dict(state_summary)
            try:
                import sys as _sys_log
                _sys_log.stderr.write(
                    f"[pill] refresh: {len(pills)} eps → {state_summary}\n")
                _sys_log.stderr.flush()
            except Exception:
                pass

    def _select_episode(self, ep: str):
        # 2026-05-07: уход с refs view — очистить unseen для prev ep.
        # Делаем ДО смены `_current_episode` чтобы хелпер видел правильный ep.
        self._mark_refs_seen_if_leaving()
        # Запоминаем какой view активен ДО смены эпизода: refs / chat /
        # shots-with-block. После смены восстановим тот же view (юзер просил —
        # «если на refs ep21 → переход на ep20 должен тоже открыть refs»).
        # Если был на блоке (shots) — открываем первый блок нового эпизода.
        prev_view = 'shots'  # по умолчанию
        try:
            if hasattr(self, 'content_stack'):
                idx = self.content_stack.currentIndex()
                if idx == 1:
                    prev_view = 'refs'
                elif idx == 2:
                    prev_view = 'chat'
        except Exception:
            pass

        self._current_episode = ep
        for e, btn in self._episode_pills.items():
            btn.setProperty("active", e == ep)
            btn.style().unpolish(btn); btn.style().polish(btn)
        # «+» pill — снимаем подсветку при переключении на обычный эпизод
        new_pill = getattr(self, '_new_ep_pill', None)
        if new_pill is not None:
            new_pill.setProperty("active", False)
            new_pill.style().unpolish(new_pill); new_pill.style().polish(new_pill)
        # Возвращаем строку «Блок 1..N | РЕФЕРЕНСЫ | ЧАТ» (была скрыта пока
        # юзер был на «+» — там она не имела смысла, нет блоков и рефов
        # для виртуального «нового» эпизода).
        if hasattr(self, 'block_pills_container'):
            self.block_pills_container.show()

        title = get_episode_title(self._meta, ep) or ep.upper()
        dur = episode_total_duration(ep)
        self.ep_title_label.setText(title)
        self.ep_title_label.setVisible(True)
        self.ep_dur_label.setText(f"{dur}с" if dur else "")
        # Активируем кнопку удаления только когда есть эпизод
        if hasattr(self, 'delete_ep_btn'):
            self.delete_ep_btn.setEnabled(True)
            self.delete_ep_btn.setVisible(True)
        if hasattr(self, 'compile_ep_btn'):
            self.compile_ep_btn.setVisible(True)
        self._populate_blocks()
        self._refresh_compact_nav()
        # Восстанавливаем view (refs/chat) если был не на shots.
        # _populate_blocks по умолчанию переключил content_stack на блок 0
        # (shots) — для prev_view='shots' это и нужно. Иначе перебиваем.
        try:
            if prev_view == 'refs':
                self._show_refs_view()
            elif prev_view == 'chat':
                self._show_chat_view()
        except Exception:
            traceback.print_exc()

    def _switch_to_episode_chat(self, ep_id: str, animated: bool = False):
        """Переключает фокус на Editor → выбранный эпизод → панель ЧАТ.
        Вызывается из NewEpisodeView после первого успешного ответа Claude,
        чтобы дальнейший разговор юзер вёл в персистентном чате эпизода.

        animated=True → fade-in эффект на content_stack (~280ms) для
        плавного появления страницы чата. Без скачка.
        """
        if not ep_id:
            return
        try:
            # Убедимся что список эпизодов актуален (новый эпизод мог только
            # что появиться в episodes.json)
            self._meta = read_episodes_meta(SHOW_ROOT)
            self._populate_episodes()
            if ep_id in self._episode_pills:
                self._select_episode(ep_id)
            # Переключаем таб на Editor (index 0)
            if hasattr(self, 'tabs'):
                self.tabs.setCurrentIndex(0)
            # Открываем ЧАТ для этого эпизода
            self._show_chat_view()
            # Плавное появление страницы чата
            if animated and hasattr(self, 'episode_chat_view'):
                self._fade_in_chat_page()
        except Exception:
            traceback.print_exc()

    def _fade_in_chat_page(self):
        """Плавное fade-in для страницы EpisodeChatView в content_stack.
        Используется после автоматического перехода из «Новый эпизод».
        Эффект применяется к content_stack чтобы анимировался весь
        переход (заголовок блока + страница чата вместе)."""
        try:
            target = self.content_stack
            effect = QGraphicsOpacityEffect(target)
            target.setGraphicsEffect(effect)
            effect.setOpacity(0.0)
            anim = QPropertyAnimation(effect, b"opacity", target)
            # 560мс базы × множитель — драматичный переход после плашки.
            # Юзер регулирует общую скорость через Настройки (anim_speed_multiplier).
            anim.setDuration(int(560 * _anim_speed_multiplier()))
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutQuint)

            # После анимации убираем эффект чтобы не влиял на
            # перерисовку (QGraphicsEffect может тормозить скролл рефов)
            def _cleanup():
                try:
                    target.setGraphicsEffect(None)
                except Exception:
                    pass
            anim.finished.connect(_cleanup)

            # Сохраняем ссылку чтобы GC не убил анимацию до завершения
            self._chat_fade_anim = anim
            anim.start()
        except Exception:
            traceback.print_exc()

    def _count_active_tasks(self) -> Dict[str, int]:
        """Считает активные фоновые задачи разных типов.

        2026-05-11 (v1.0.45): расширено для покрытия ВСЕХ типов QThread'ов
        в проекте, иначе при закрытии Studio во время генерации сторибордов
        / монтажа / outfit-picker'а Qt destroys live QThread → fatal
        SIGABRT. Раньше учитывались только shot/ref/geometry/episode —
        SeedancePipeline, MontageOrchestrator, SuggestOutfits, AutonomousGen
        и treads из мульти-эпизодных реестров (`_external_threads`,
        `_threads`) проходили мимо счётчика → диалог подтверждения не
        показывался → закрытие → краш.

        Возвращает dict: {'shot', 'ref', 'geometry', 'episode',
        'storyboard', 'montage', 'outfit', 'autogen', 'auth'}.
        """
        counts = {'shot': 0, 'ref': 0, 'geometry': 0, 'episode': 0,
                  'storyboard': 0, 'montage': 0, 'outfit': 0,
                  'autogen': 0, 'auth': 0}
        try:
            # Регенерации шотов (GenerateThread) + Mode C версии шотов
            counts['shot'] = sum(
                1 for t in self._active_regens.values()
                if t is not None and t.isRunning())
            counts['shot'] += sum(
                1 for t in self._active_mode_c_version_threads.values()
                if t is not None and t.isRunning())

            # Регенерации рефов (RefGenerateThread)
            ref_threads = getattr(self, '_ref_threads', [])
            counts['ref'] = sum(
                1 for t in ref_threads
                if t is not None and t.isRunning())

            # Обновления geometry (ClaudeGeometryThread)
            geom_threads = getattr(self, '_geometry_threads', [])
            counts['geometry'] = sum(
                1 for t in geom_threads
                if t is not None and t.isRunning())

            # Запросы к Claude в «Новый эпизод» + «Чат эпизода»
            episode_count = 0
            nev = getattr(self, 'new_episode_view', None)
            if nev is not None:
                t = getattr(nev, '_thread', None)
                if t is not None and t.isRunning():
                    episode_count += 1
                # 2026-05-11 (v1.0.45): per-ep реестр от multi-ep fix.
                nev_threads = getattr(nev, '_threads', None) or {}
                for t in nev_threads.values():
                    if t is not None and t.isRunning():
                        episode_count += 1
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None:
                t = getattr(ev, '_thread', None)
                if t is not None and t.isRunning():
                    episode_count += 1
                ext_threads = getattr(ev, '_external_threads', None) or {}
                for t in ext_threads.values():
                    if t is not None and t.isRunning():
                        episode_count += 1
            counts['episode'] = episode_count

            # 2026-05-11 (v1.0.45): новые категории.
            # Seedance pipeline (генерация сторибордов) + regen шотов через Seedance.
            if ev is not None:
                t = getattr(ev, '_seedance_pipeline_thread', None)
                if t is not None and t.isRunning():
                    counts['storyboard'] += 1
            seedance_regen = getattr(self, '_seedance_regen_threads', [])
            counts['storyboard'] += sum(
                1 for t in seedance_regen
                if t is not None and t.isRunning())

            # MontageOrchestrator per ep_id
            if ev is not None:
                montage_threads = getattr(ev, '_montage_threads', None) or {}
                counts['montage'] = sum(
                    1 for t in montage_threads.values()
                    if t is not None and t.isRunning())

            # Outfit picker thread per ep_id
            if ev is not None:
                outfit_threads = getattr(ev, '_outfit_threads', None) or {}
                counts['outfit'] = sum(
                    1 for t in outfit_threads.values()
                    if t is not None and t.isRunning())

            # AutonomousGen (location/object autonomous generation).
            # _active_gens — dict с тредами и метаданными; берём только тред.
            active_gens = getattr(self, '_active_gens', None) or {}
            for entry in active_gens.values():
                t = entry.get('thread') if isinstance(entry, dict) else entry
                if t is not None and hasattr(t, 'isRunning') and t.isRunning():
                    counts['autogen'] += 1

            # Auth switch (OAuth flow).
            auth_t = getattr(self, '_auth_switch_thread', None)
            if auth_t is not None and auth_t.isRunning():
                counts['auth'] = 1
        except Exception:
            traceback.print_exc()
        return counts

    def _collect_all_threads(self) -> List:
        """2026-05-11 (v1.0.45): собирает плоский список ВСЕХ QThread'ов
        из реестров MainWindow + EpisodeChatView + NewEpisodeView.
        Используется в `closeEvent` для graceful shutdown — перед
        фактическим закрытием Studio мы вызываем `.stop()` + `.wait()`
        на каждом, чтобы избежать SIGABRT от Qt при destruction'е
        живого QThread'а.
        """
        threads = []
        try:
            threads.extend(self._active_regens.values())
            threads.extend(self._active_mode_c_version_threads.values())
            # 2026-06-09: keep-alive завершающихся потоков — редкий ещё-бегущий
            # при закрытии надо дождаться, иначе SIGABRT при destruction'е.
            threads.extend(getattr(self, '_threads_pending_delete', set()) or [])
            threads.extend(getattr(self, '_camera_director_threads', {}).values())
            threads.extend(getattr(self, '_ref_threads', []) or [])
            threads.extend(getattr(self, '_geometry_threads', []) or [])
            threads.extend(getattr(self, '_seedance_regen_threads', []) or [])
            threads.extend(getattr(self, '_key_health_threads', []) or [])
            threads.extend(getattr(self, '_key_usage_threads', []) or [])
            threads.extend(getattr(self, '_cancel_threads', []) or [])
            t = getattr(self, '_server_check_thread', None)
            if t is not None:
                threads.append(t)
            t = getattr(self, '_auth_switch_thread', None)
            if t is not None:
                threads.append(t)
            active_gens = getattr(self, '_active_gens', None) or {}
            for entry in active_gens.values():
                t = entry.get('thread') if isinstance(entry, dict) else entry
                if t is not None:
                    threads.append(t)
            nev = getattr(self, 'new_episode_view', None)
            if nev is not None:
                t = getattr(nev, '_thread', None)
                if t is not None:
                    threads.append(t)
                threads.extend(
                    (getattr(nev, '_threads', None) or {}).values())
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None:
                t = getattr(ev, '_thread', None)
                if t is not None:
                    threads.append(t)
                threads.extend(
                    (getattr(ev, '_external_threads', None) or {}).values())
                threads.extend(
                    (getattr(ev, '_outfit_threads', None) or {}).values())
                threads.extend(
                    (getattr(ev, '_montage_threads', None) or {}).values())
                t = getattr(ev, '_seedance_pipeline_thread', None)
                if t is not None:
                    threads.append(t)
                # v1.0.85: StoryboardPipelineThread тоже держит claude
                # CLI subprocess'ы — без cleanup при закрытии могут
                # повиснуть зомби и продолжить тратить токены. До v1.0.85
                # этот тред не собирался — упущение из v1.0.45.
                t = getattr(ev, '_storyboard_pipeline_thread', None)
                if t is not None:
                    threads.append(t)
        except Exception:
            traceback.print_exc()
        # Уникальные ненулевые + отсев МЁРТВЫХ wrapper'ов: C++ объект уже
        # удалён через deleteLater (напр. отработавший CancelThread, чья Python-
        # ссылка осталась в _cancel_threads — список не чистится). Любой их метод
        # (isRunning в _graceful_shutdown) кинул бы RuntimeError и уронил бы
        # graceful-shutdown ДО остановки живых тредов → пробуем живость заранее.
        out = []
        for t in {id(t): t for t in threads if t is not None}.values():
            try:
                t.isRunning()  # проба: на мёртвом wrapper'е кидает RuntimeError
            except RuntimeError:
                continue
            except Exception:
                pass
            out.append(t)
        return out

    def _retire_thread(self, t):
        """Безопасно снимает ссылку на завершившийся GenerateThread.

        НЕ дропаем объект сразу: кастомный сигнал finished/error летит
        ИЗНУТРИ run() ДО его возврата, и если реестр держал ПОСЛЕДНЮЮ
        ссылку — её немедленный .pop в слоте даёт GC, который уничтожает
        QThread пока тот ещё running → 'QThread: Destroyed while thread is
        still running' → abort(). Держим ссылку в _threads_pending_delete и
        отдаём deleteLater только когда QThread.isFinished() — т.е. run()
        реально вернулся. Сметает периодический reaper-таймер.
        """
        if t is None:
            return
        self._threads_pending_delete.add(t)
        if not self._thread_reaper.isActive():
            self._thread_reaper.start()

    def _reap_finished_threads(self):
        """Reaper-таймер: удаляет из keep-alive потоки, у которых run()
        уже вернулся (isFinished()). deleteLater отдаёт фактическое
        разрушение Qt event-loop'у. Пустое множество → стоп таймера
        (не тикаем вхолостую)."""
        if not self._threads_pending_delete:
            self._thread_reaper.stop()
            return
        done = [t for t in self._threads_pending_delete
                if t is None or t.isFinished()]
        for t in done:
            self._threads_pending_delete.discard(t)
            if t is not None:
                try:
                    t.deleteLater()
                except Exception:
                    pass
        if not self._threads_pending_delete:
            self._thread_reaper.stop()

    def closeEvent(self, event):
        """Перехват закрытия окна. Если есть активные фоновые задачи —
        показываем CloseConfirmDialog. По reject (Подождать) — отмена
        закрытия. По accept (Закрыть всё равно) — graceful shutdown
        всех QThread'ов перед фактическим закрытием.

        2026-05-11 (v1.0.45): добавлен graceful shutdown. Раньше код
        полагался на «потоки умрут вместе с процессом», но Qt destructor
        QThread'а на live thread вызывает qFatal → SIGABRT. Особенно
        стабильно крашилось при закрытии во время генерации сторибордов
        (SeedancePipelineThread + дочерние GenerateThread'ы). Теперь
        проходим по всем потокам через `_collect_all_threads`, вызываем
        `.stop()` если есть, ждём ~2 сек каждого. После — Qt уже видит
        мёртвый QThread, деструкторы безопасны.
        """
        try:
            counts = self._count_active_tasks()
            total = sum(counts.values())
            if total > 0:
                # Собираем человекочитаемый список
                lines = []
                if counts['shot']:
                    lines.append(tr('close_task_shot', n=counts['shot']))
                if counts['ref']:
                    lines.append(tr('close_task_ref', n=counts['ref']))
                if counts['geometry']:
                    lines.append(tr('close_task_geometry', n=counts['geometry']))
                if counts['episode']:
                    lines.append(tr('close_task_episode', n=counts['episode']))
                # 2026-05-11 (v1.0.45): новые категории. Если перевода
                # нет (старая Studio) — fallback на raw имя категории.
                if counts.get('storyboard'):
                    try:
                        lines.append(tr('close_task_storyboard',
                                        n=counts['storyboard']))
                    except Exception:
                        lines.append(f"сторибордов: {counts['storyboard']}")
                if counts.get('montage'):
                    try:
                        lines.append(tr('close_task_montage',
                                        n=counts['montage']))
                    except Exception:
                        lines.append(f"монтажных карт: {counts['montage']}")
                if counts.get('outfit'):
                    try:
                        lines.append(tr('close_task_outfit',
                                        n=counts['outfit']))
                    except Exception:
                        lines.append(f"outfit-генераций: {counts['outfit']}")
                if counts.get('autogen'):
                    try:
                        lines.append(tr('close_task_autogen',
                                        n=counts['autogen']))
                    except Exception:
                        lines.append(f"авто-генераций: {counts['autogen']}")
                if counts.get('auth'):
                    try:
                        lines.append(tr('close_task_auth',
                                        n=counts['auth']))
                    except Exception:
                        lines.append(f"смена AI-аккаунта: {counts['auth']}")

                overlay = apply_modal_dim(self)
                try:
                    dlg = CloseConfirmDialog(lines, parent=self)
                    accepted = (dlg.exec() == QDialog.DialogCode.Accepted)
                finally:
                    remove_modal_dim(overlay)

                if not accepted:
                    # «Подождать» — игнорируем закрытие, окно остаётся
                    event.ignore()
                    return
            # Иначе — нет активных задач или юзер сказал «закрыть всё равно».
            # Сохраняем размер/позицию окна в QSettings.
            try:
                _gs = QSettings(APP_ORG, APP_NAME)
                _gs.setValue("main_window_geometry", self.saveGeometry())
                _gs.sync()
            except Exception:
                traceback.print_exc()
            # 2026-05-11 (v1.0.45): graceful shutdown всех QThread'ов
            # ПЕРЕД event.accept() чтобы избежать Qt fatal'а при
            # destruction'е живого thread'а.
            try:
                self._graceful_shutdown_all_threads()
            except Exception:
                traceback.print_exc()
            event.accept()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)
        except Exception:
            traceback.print_exc()
            event.accept()
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)

    def _graceful_shutdown_all_threads(self) -> None:
        """2026-05-11 (v1.0.45): корректно останавливает все QThread'ы
        перед закрытием Studio.

        Шаги:
          1. Собираем все потоки через `_collect_all_threads`.
          2. Для каждого: если есть метод `.stop()` — вызываем (выставит
             флаг + terminate subprocess'а CLI у нас в любом thread-классе).
          3. `wait(2000)` — даём 2 сек на graceful exit.
          4. Если всё ещё running → `terminate()` + `wait(500)` (Qt-level,
             не abort).
          5. v1.0.85: финальная страховка от зомби-claude-процессов.
             Если QThread.terminate() не убил subprocess (например, CLI
             заблокирован в I/O и не реагирует на SIGTERM от treads-stop),
             добиваем через pkill / PowerShell Stop-Process. Иначе CLI
             продолжит жить после закрытия Studio и сожрёт токены.

        Без этого Qt при destruction'е QObject-иерархии MainWindow натыкается
        на живые QThread → `qFatal("QThread: Destroyed while thread is still
        running")` → SIGABRT. Особенно болезненно при закрытии во время
        генерации сторибордов (SeedancePipelineThread + child GenerateThreads).
        """
        threads = self._collect_all_threads()
        running_before = sum(1 for t in threads
                              if hasattr(t, 'isRunning')
                              and t.isRunning())
        self._log_close_diag(
            f"_graceful_shutdown_all_threads: total={len(threads)} "
            f"running={running_before}")
        # Phase 1: ask nicely.
        for t in threads:
            try:
                if hasattr(t, 'stop'):
                    t.stop()
            except Exception:
                pass
        # Phase 2: wait briefly.
        for t in threads:
            try:
                if hasattr(t, 'isRunning') and t.isRunning():
                    t.wait(2000)
            except Exception:
                pass
        # Phase 3: nuke остатки.
        for t in threads:
            try:
                if hasattr(t, 'isRunning') and t.isRunning():
                    t.terminate()
                    if hasattr(t, 'wait'):
                        t.wait(500)
            except Exception:
                pass
        # Phase 4 (v1.0.85): pkill claude CLI зомби — last resort.
        # Запускается ВСЕГДА если были живые pipeline-треды (даже если
        # все они сейчас «мёртвые» по QThread.isRunning() — subprocess
        # CLI мог отделиться и продолжить жить). Безопасно: pkill убьёт
        # только claude процессы с маркером `--system-prompt` (Studio
        # запускает CLI именно с этим флагом; интерактивный Claude
        # Code в репозитории — без него).
        killed = False
        if running_before > 0:
            killed = self._pkill_claude_subprocs()
        self._log_close_diag(
            f"_graceful_shutdown_all_threads: done pkill_attempted="
            f"{running_before > 0} killed_ok={killed}")

    def _pkill_claude_subprocs(self) -> bool:
        """v1.0.85: убивает все claude CLI subprocess'ы с маркером
        `--system-prompt`. Cross-platform.

        Возвращает True если операция прошла без исключений (не значит
        что что-то реально было убито — это только indicator успешного
        вызова).

        Используется:
          • как Phase 4 в `_graceful_shutdown_all_threads` при выходе;
          • как страховка в `_on_seedance_restart` и `_on_montage_cancel`.

        Маркер `--system-prompt` стабильный: Studio запускает все CLI
        потоки с этим флагом (MontageOrchestrator/StoryboardPipeline/
        SeedancePipeline). Интерактивный Claude Code в репо стартует
        без него — не трогаем.
        """
        try:
            import subprocess as _sp
            if sys.platform == 'win32':
                ps_cmd = (
                    "Get-CimInstance Win32_Process "
                    "-Filter \"Name='claude.exe'\" "
                    "| Where-Object { $_.CommandLine -like '*--system-prompt*' } "
                    "| ForEach-Object { "
                    "  try { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue } catch {} }"
                )
                CREATE_NO_WINDOW = 0x08000000
                _sp.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                        capture_output=True, timeout=5,
                        creationflags=CREATE_NO_WINDOW)
            else:
                _sp.run(['pkill', '-TERM', '-f', 'claude -p '],
                        capture_output=True, timeout=3)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def _log_close_diag(self, line: str) -> None:
        """v1.0.85: лог в `shows/<active>/_studio_diag.log` для
        диагностики выхода. Тихо проглатывает ошибки — closeEvent не
        должен падать из-за проблем с логированием.
        """
        try:
            import datetime
            cur = getattr(self, '_current_show', None)
            root = getattr(self, '_project_root', None)
            if not cur or root is None:
                return
            log_path = root / "shows" / cur / "_studio_diag.log"
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [close] {line}\n")
        except Exception:
            pass

    def _log_ref_error(self, image_path, msg: str) -> None:
        """Коммит E: персистентный лог ошибок рефов в
        `shows/<active>/_studio_diag.log` (тег [REF-ERROR]). Текст ошибки —
        ПОЛНОСТЬЮ (не обрезаем). Тихо проглатывает ошибки — _on_ref_error
        не должен падать из-за логирования. Зеркалит _log_close_diag."""
        try:
            import datetime
            cur = getattr(self, '_current_show', None)
            root = getattr(self, '_project_root', None)
            if not cur or root is None:
                return
            try:
                fname = image_path.name if hasattr(image_path, 'name') else str(image_path)
            except Exception:
                fname = str(image_path)
            log_path = root / "shows" / cur / "_studio_diag.log"
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [REF-ERROR] {fname}: {msg}\n")
        except Exception:
            pass

    def _log_autogen_error(self, gen_type: str, name: str, msg: str) -> None:
        """2026-06-18: MW-side лог ошибок autonomous-генерации в
        `shows/<active>/_studio_diag.log` (тег [AUTOGEN-ERROR]). Формат
        идентичен thread-side `AutonomousGenThread._diag_log`, чтобы ВСЕ
        autogen-сбои шли единообразно: error-путь логируется в треде, а
        «ложный finished_ok» (агент сказал done, но файла нет) — здесь.
        Зеркалит `_log_ref_error`. Тихо проглатывает свои ошибки."""
        try:
            import datetime
            cur = getattr(self, '_current_show', None)
            root = getattr(self, '_project_root', None)
            if not cur or root is None:
                return
            log_path = root / "shows" / cur / "_studio_diag.log"
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [AUTOGEN-ERROR] {gen_type}/{name}: {msg}\n")
        except Exception:
            pass

    def _log_shot_regen(self, msg: str) -> None:
        """2026-06-19: MW-side лог одиночной перегенерации шота в
        `shows/<active>/_studio_diag.log` (тег [SHOT-REGEN]). Зеркалит
        `_log_autogen_error`/`_log_ref_error`. Тихо проглатывает свои
        ошибки — regen-flow не должен падать из-за логирования.
        Cross-platform: pathlib + open(encoding='utf-8'), без subprocess."""
        try:
            import datetime
            cur = getattr(self, '_current_show', None)
            root = getattr(self, '_project_root', None)
            if not cur or root is None:
                return
            log_path = root / "shows" / cur / "_studio_diag.log"
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [SHOT-REGEN] {msg}\n")
        except Exception:
            pass

    def _on_delete_episode_clicked(self):
        """Обработчик кнопки «🗑 Удалить эпизод».
        Показывает диалог подтверждения. При Yes — удаляет всё что относится
        к эпизоду (запись в episodes.json, чат, промпты блоков, сториборды),
        НЕ ТРОГАЯ refs/. После — refresh пилюль и переключение на следующий
        эпизод (если есть) или пустое состояние."""
        ep_id = self._current_episode
        if not ep_id or not self._current_show:
            return
        title = get_episode_title(self._meta, ep_id) or ep_id.upper()
        # Диалог подтверждения с явным указанием что удаляется и что нет
        box = QMessageBox(self)
        box.setWindowTitle(tr('delete_ep_confirm_title'))
        box.setText(tr('delete_ep_confirm_msg', ep=ep_id, title=title))
        box.setIcon(QMessageBox.Icon.Warning)
        yes_btn = box.addButton(tr('delete_ep_yes'),
                                QMessageBox.ButtonRole.DestructiveRole)
        no_btn  = box.addButton(tr('delete_ep_no'),
                                QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no_btn)  # default = «Отмена», без случайных Enter
        box.exec()
        if box.clickedButton() is not yes_btn:
            return

        # 2026-05-08: ОСТАНАВЛИВАЕМ фоновый RunEpisodeThread для этого ep'а
        # ДО чистки файлов. Без этого:
        #   • subprocess `claude -p` продолжает работать в фоне
        #   • в `NewEpisodeView._threads[ep_id]` остаётся живой объект
        #   • при следующей попытке создать ep с тем же номером в форме «+»
        #     — `_update_run_btn_state` видит «уже бежит» → блокирует кнопку
        # `thread.stop()` шлёт terminate subprocess'у; сигнал `stopped`
        # приедет асинхронно и сделает свой pop — но мы pop'аем сразу,
        # чтобы кнопка «Запустить» разблокировалась мгновенно. Проверка
        # `is sender_s` в `_on_thread_stopped` гарантирует что повторный
        # pop не уронит логику.
        try:
            nev = getattr(self, 'new_episode_view', None)
            if nev is not None:
                threads = getattr(nev, '_threads', None) or {}
                t = threads.get(ep_id)
                if t is not None:
                    try:
                        if t.isRunning():
                            t.stop()
                    except Exception:
                        pass
                    try:
                        threads.pop(ep_id, None)
                    except Exception:
                        pass
                    try:
                        nev._update_run_btn_state()
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()

        # ── Само удаление (мягкое — ошибки игнорим, но сообщаем в статус-бар) ──
        errors = []
        # 1. Запись в episodes.json
        try:
            ep_meta_path = SHOW_ROOT / "episodes.json"
            meta = read_episodes_meta(SHOW_ROOT)
            if ep_id in meta:
                del meta[ep_id]
                ep_meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                self._meta = meta
        except Exception as ex:
            errors.append(f"episodes.json: {ex}")
        # 2. Файл чата
        try:
            chat_path = chat_log_path(ep_id)
            if chat_path.exists():
                chat_path.unlink()
        except Exception as ex:
            errors.append(f"chat: {ex}")
        # 3. Промпты блоков `output/prompts/<ep>_block_*.txt`
        try:
            for p in PROMPTS_DIR.glob(f"{ep_id}_block_*.txt"):
                try:
                    p.unlink()
                except Exception as ex:
                    errors.append(f"prompt {p.name}: {ex}")
        except Exception as ex:
            errors.append(f"prompts dir: {ex}")
        # 4. Сториборды `output/storyboards/<ep>_block_*_shot*.jpg`
        try:
            for p in STORYBOARDS_DIR.glob(f"{ep_id}_block_*"):
                try:
                    p.unlink()
                except Exception as ex:
                    errors.append(f"storyboard {p.name}: {ex}")
        except Exception as ex:
            errors.append(f"storyboards dir: {ex}")
        # 4b. История версий сторибордов `output/storyboards/_history/
        # <ep>_block_*_shot*/v*.jpg` (2026-05-07 — добавлено вместе с
        # ShotViewerDialog popup). При удалении эпизода чистим всю
        # историю его шотов вместе с самими шотами.
        try:
            history_root = STORYBOARDS_DIR / "_history"
            if history_root.exists():
                import shutil
                for sub in history_root.glob(f"{ep_id}_block_*"):
                    if sub.is_dir():
                        try:
                            shutil.rmtree(str(sub))
                        except Exception as ex:
                            errors.append(f"shot history {sub.name}: {ex}")
        except Exception as ex:
            errors.append(f"history dir: {ex}")
        # 5. Промпты Seedance `output/seedance/<ep>_block_*.txt`
        # 2026-05-07: добавлено для консистентности — раньше при удалении
        # эпизода Seedance промпты оставались сиротами (картинки шотов и
        # промпты сторибордов чистились, а Seedance — нет). Теперь корзина
        # удаляет всё что относится к эпизоду.
        try:
            # 2026-05-18 (UI tabs): расширено — удаляем не только
            # `<ep>_block_*.txt` (оригиналы + tab-версии _tab<K>.txt
            # подпадают под этот же glob), но и `<ep>_block_*_tabs.json`
            # (per-block state вкладок).
            for p in SEEDANCE_DIR.glob(f"{ep_id}_block_*.txt"):
                try:
                    p.unlink()
                except Exception as ex:
                    errors.append(f"seedance {p.name}: {ex}")
            for p in SEEDANCE_DIR.glob(f"{ep_id}_block_*_tabs.json"):
                try:
                    p.unlink()
                except Exception as ex:
                    errors.append(f"seedance {p.name}: {ex}")
        except Exception as ex:
            errors.append(f"seedance dir: {ex}")
        # 6. Лог агентов монтажной карты `output/_agent_log_<ep>.json`
        # 2026-05-07: добавлено вместе с Seedance — после удаления эпизода
        # лог по уже несуществующей карте бесполезен (файлы шотов/промптов
        # стёрты, attribution анализу не на чем работать). Чистим.
        try:
            agent_log = SHOW_ROOT / "output" / f"_agent_log_{ep_id}.json"
            if agent_log.exists():
                agent_log.unlink()
        except Exception as ex:
            errors.append(f"agent_log: {ex}")
        # 6b. Украинские переводы реплик `output/_translations_uk_<ep>.json`
        # (Этап 2). Файл в output/ (не .cache/) → чистим явно, как _agent_log.
        try:
            uk_tr = _uk_tr_path(ep_id)
            if uk_tr.exists():
                uk_tr.unlink()
        except Exception as ex:
            errors.append(f"translations_uk: {ex}")
        # Сброс флага загрузки кэша эпизода (на случай пересоздания с тем же id).
        try:
            self._uk_tr_loaded_eps.discard(ep_id)
        except Exception:
            pass

        # Сбрасываем кэш unseen/active по этому эпизоду — чтобы счётчики
        # не показывали NEW для удалённых блоков
        try:
            self._unseen_shots = {(b, i) for (b, i) in self._unseen_shots
                                  if not b.startswith(f"{ep_id}_")}
        except Exception:
            pass

        # 2026-05-05: Сбрасываем состояние EpisodeChatView (gen-кнопки,
        # очередь, _gen_seen_names). Иначе при пересоздании эпизода с
        # тем же ep_id юзер не увидит карточки выбора рефов — имена
        # «застряли» в seen_names от прошлого запуска.
        try:
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None and hasattr(ev, 'reset_state'):
                ev.reset_state()
        except Exception:
            pass

        # Перерисовать дропдаун: текущий эпизод исчез, выбираем следующий
        self._current_episode = None
        try:
            self._populate_episodes()
        except Exception:
            pass

        msg = tr('delete_ep_done', ep=ep_id)
        if errors:
            msg += " (" + "; ".join(errors[:2]) + ")"
        try:
            self.status_bar.showMessage(msg, 6000)
        except Exception:
            pass

    def _populate_blocks(self):
        """Перерисовывает ряд пилюль блоков + пилюлю «Референсы» для текущего эпизода.

        Если у эпизода блоков ещё нет (только что запущен через «Новый эпизод»,
        Claude нагенерил рефы, а монтажа пока нет) — рисуем ТОЛЬКО пилюлю
        «РЕФЕРЕНСЫ» и сразу открываем refs-view. Пустых шот-карточек в этом
        случае быть не должно.
        """
        self._clear_layout(self.block_pills_layout)
        self._block_pills = {}
        compact_nav = getattr(self, '_compact_nav_enabled', False)
        if not compact_nav:
            self._refs_pill = None

        if not self._current_episode:
            self.current_block = None
            self.block_title.setText("")
            if hasattr(self, 'seedance_btn'):
                self.seedance_btn.setVisible(False)
            if hasattr(self, 'block_refs_btn'):
                self.block_refs_btn.setVisible(False)
            for card in self.shot_cards:
                card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                card.set_image(None)
            if hasattr(self, 'content_stack'):
                self.content_stack.setCurrentIndex(0)
            self.save_btn.show()
            self.save_btn.setEnabled(False)
            self._refresh_compact_nav()
            return

        if compact_nav:
            blocks = list_blocks_for_episode(self._current_episode)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.block_pills_layout.addLayout(row)

            self._block_carousel_scroll = _BlockCarouselScrollArea()
            self._block_carousel_scroll.setObjectName("block-carousel-scroll")
            self._block_carousel_scroll.setFixedSize(458, 28)
            self._block_carousel_scroll.horizontalScrollBar().valueChanged.connect(
                self._update_block_carousel_arrows)
            row.addWidget(
                self._block_carousel_scroll,
                alignment=Qt.AlignmentFlag.AlignVCenter)

            self._block_carousel_content = QWidget()
            self._block_carousel_content.setCursor(Qt.CursorShape.SizeHorCursor)
            inner = QHBoxLayout(self._block_carousel_content)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(2)
            inner.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._block_carousel_scroll.setWidget(self._block_carousel_content)

            for blk in blocks:
                btn = QPushButton(self._format_compact_block_label(blk))
                btn.setObjectName("pill-block")
                btn.setFixedSize(44, 26)
                btn.setProperty("compact", True)
                btn.setProperty("active", False)
                has_active = any(b == blk for (b, _) in self._active_regens.keys())
                has_active = has_active or any(
                    b == blk for (b, _p, _v)
                    in self._active_mode_c_version_threads.keys())
                has_unseen = (not has_active) and any(
                    b == blk for (b, _) in self._unseen_shots)
                btn.setProperty("generating", has_active)
                btn.setProperty(
                    "gen_phase", str(getattr(self, '_block_gen_phase', 0)))
                btn.setProperty("unseen", has_unseen)
                btn.clicked.connect(lambda _, b=blk: self._select_block(b))
                inner.addWidget(btn)
                self._block_pills[blk] = btn
            content_w = len(blocks) * 44 + max(0, len(blocks) - 1) * 2
            self._block_carousel_content.setMinimumWidth(max(458, content_w))
            self._update_block_carousel_arrows()

            if self._refs_pill is not None:
                self._refs_pill.setText(tr('refs'))
                self._refs_pill.setProperty("active", False)
                self._refs_pill.style().unpolish(self._refs_pill)
                self._refs_pill.style().polish(self._refs_pill)
            if self._chat_pill is not None:
                self._chat_pill.setText(tr('chat_pill'))
                self._chat_pill.setProperty("active", False)
                self._chat_pill.style().unpolish(self._chat_pill)
                self._chat_pill.style().polish(self._chat_pill)
            if self._pending_ref_notices:
                self._set_refs_pill_notice(True)
            if blocks:
                prev = self.current_block if self.current_block in blocks else blocks[0]
                self._select_block(prev)
            else:
                self.current_block = None
                for card in self.shot_cards:
                    card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                    card.set_image(None)
                self._show_refs_view()
            self._refresh_compact_nav()
            return

        # 2026-05-22: сетка по 9 блок-пилюль в строке. Refs/Chat пилюли
        # ВСЕГДА остаются в первой строке (напротив блоков 1-9).
        # Последующие строки содержат только продолжение блоков (10-18, ...).
        # Паттерн взят из _populate_episodes (см. EPS_PER_ROW=10).
        # 9 (не 10) — иначе при 10 блоках + Refs + Chat первая строка не
        # помещается по ширине окна и «Референсы» обрезается.
        BLOCKS_PER_ROW = 9

        def _make_block_row():
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.setAlignment(Qt.AlignmentFlag.AlignLeft)
            return row

        blocks = list_blocks_for_episode(self._current_episode)

        current_row = _make_block_row()
        first_row = current_row  # запомнили — туда пойдут Refs/Chat в конце
        self.block_pills_layout.addLayout(current_row)
        items_in_row = 0

        for blk in blocks:
            # Перенос: новую строку создаём ТОЛЬКО когда есть следующий блок
            # для неё (иначе при N кратном 10 появлялась бы пустая строка).
            if items_in_row >= BLOCKS_PER_ROW:
                current_row = _make_block_row()
                self.block_pills_layout.addLayout(current_row)
                items_in_row = 0
            btn = QPushButton(self._format_block_label(blk))
            btn.setObjectName("pill-block")
            btn.setFixedHeight(28)   # 2026-05-08 редизайн: компактнее (было 34)
            btn.setProperty("active", False)
            has_active = any(b == blk for (b, _) in self._active_regens.keys())
            has_active = has_active or any(
                b == blk for (b, _p, _v)
                in self._active_mode_c_version_threads.keys())
            has_unseen = (not has_active) and any(b == blk for (b, _) in self._unseen_shots)
            btn.setProperty("generating", has_active)
            btn.setProperty("gen_phase", str(getattr(self, '_block_gen_phase', 0)))
            btn.setProperty("unseen", has_unseen)
            btn.clicked.connect(lambda _, b=blk: self._select_block(b))
            current_row.addWidget(btn)
            self._block_pills[blk] = btn
            items_in_row += 1

        # Пилюля «Референсы» — показывается ВСЕГДА когда есть current_episode.
        # Если блоков нет — это единственная пилюля, и refs-view подгружается
        # автоматически. Если блоки есть — между ними и refs стоит разделитель.
        # 2026-05-22: separator/Refs/Chat добавляются в ПЕРВУЮ строку
        # (напротив блоков 1-9), не плавают за последним блоком.
        if blocks:
            # 2026-05-08 редизайн: разделитель тоньше и короче (1×18px).
            first_row.addSpacing(8)
            sep = QFrame()
            sep.setObjectName("pills-vsep")
            sep.setFixedSize(1, 18)
            first_row.addWidget(sep, alignment=Qt.AlignmentFlag.AlignVCenter)
            first_row.addSpacing(8)

        self._refs_pill = QPushButton(tr('refs'))
        self._refs_pill.setObjectName("pill-refs")
        self._refs_pill.setFixedHeight(28)  # 2026-05-08 редизайн
        self._refs_pill.setProperty("active", False)
        self._refs_pill.setProperty("has_notice", False)
        self._refs_pill.setProperty("pulse_on", False)
        self._refs_pill.clicked.connect(self._show_refs_view)
        first_row.addWidget(self._refs_pill)
        if self._pending_ref_notices:
            self._set_refs_pill_notice(True)

        # Пилюля «Чат» — для просмотра/продолжения переписки с Claude
        # по этому конкретному эпизоду. История хранится в
        # `shows/<slug>/chats/<ep_id>.jsonl`, переживает перезапуск .app.
        first_row.addSpacing(8)
        self._chat_pill = QPushButton(tr('chat_pill'))
        self._chat_pill.setObjectName("pill-chat")
        self._chat_pill.setFixedHeight(28)  # 2026-05-08 редизайн
        self._chat_pill.setProperty("active", False)
        self._chat_pill.clicked.connect(self._show_chat_view)
        first_row.addWidget(self._chat_pill)

        if blocks:
            prev = self.current_block if self.current_block in blocks else blocks[0]
            self._select_block(prev)
        else:
            # Эпизод без блоков → автоматически открываем refs-view, шот-карточки
            # не рисуем (сбрасываем их в blank на случай если осталась картинка
            # от предыдущего эпизода).
            self.current_block = None
            for card in self.shot_cards:
                card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                card.set_image(None)
            self._show_refs_view()

    def _block_carousel_step_px(self, pills: int = 5) -> int:
        """Scroll distance for compact block carousel arrow clicks."""
        direction = -1 if pills < 0 else 1
        return direction * max(1, abs(pills)) * (44 + 2)

    def _scroll_block_carousel(self, pills: int) -> None:
        """Scroll compact block carousel by N block buttons."""
        scroll = getattr(self, '_block_carousel_scroll', None)
        if scroll is None:
            return
        try:
            bar = scroll.horizontalScrollBar()
            bar.setValue(bar.value() + self._block_carousel_step_px(pills))
            self._update_block_carousel_arrows()
        except Exception:
            traceback.print_exc()

    def _update_block_carousel_arrows(self, *_args) -> None:
        """Enable/disable carousel arrows when the scroll area hits edges."""
        scroll = getattr(self, '_block_carousel_scroll', None)
        left = getattr(self, '_block_carousel_left_btn', None)
        right = getattr(self, '_block_carousel_right_btn', None)
        if scroll is None or left is None or right is None:
            return
        try:
            bar = scroll.horizontalScrollBar()
            left.setEnabled(bar.value() > bar.minimum())
            right.setEnabled(bar.value() < bar.maximum())
        except Exception:
            pass
        self._sync_block_pill_states()

    def _ensure_block_carousel_visible(self, block_name: str) -> None:
        """Scroll compact block carousel so the active block pill is visible."""
        scroll = getattr(self, '_block_carousel_scroll', None)
        btn = self._block_pills.get(block_name) if hasattr(self, '_block_pills') else None
        if scroll is None or btn is None:
            return

        def _do_scroll():
            try:
                bar = scroll.horizontalScrollBar()
                x = btn.x()
                w = btn.width()
                left = bar.value()
                right = left + scroll.viewport().width()
                pad = 6
                if x < left + pad:
                    bar.setValue(max(bar.minimum(), x - pad))
                elif x + w > right - pad:
                    bar.setValue(min(bar.maximum(), x + w - scroll.viewport().width() + pad))
                self._update_block_carousel_arrows()
            except Exception:
                traceback.print_exc()

        QTimer.singleShot(0, _do_scroll)

    def _select_block(self, name: str):
        # Если юзер кликнул на ТОТ ЖЕ блок что и сейчас — fade-in
        # анимация content_stack даст видимое моргание opacity 0→1 за
        # 240мс. Re-click того же блока не должен моргать — никаких
        # реальных изменений не происходит.
        same_block = (self.current_block == name)
        was_on_blocks = self._is_blocks_view_active()
        if was_on_blocks and self.current_block and self.current_block != name:
            self._mark_block_seen(self.current_block)
        self.current_block = name
        for b, btn in self._block_pills.items():
            btn.setProperty("active", b == name)
            btn.style().unpolish(btn); btn.style().polish(btn)
        self._ensure_block_carousel_visible(name)
        for pill_attr in ('_refs_pill', '_chat_pill'):
            pill = getattr(self, pill_attr, None)
            if pill is not None:
                pill.setProperty("active", False)
                pill.style().unpolish(pill); pill.style().polish(pill)
        if hasattr(self, 'content_stack'):
            self.content_stack.setCurrentIndex(0)
            self.save_btn.show()
        self._display_block(name)
        self._refresh_compact_nav()
        self._clear_status_now()
        # fade-in ТОЛЬКО при реальном переходе на другой блок
        if hasattr(self, 'content_stack') and not same_block:
            fade_in_widget(self.content_stack, duration=240)

    def _on_tab_changed_fade(self, index: int):
        """Плавный fade-in при переключении табов."""
        try:
            self._clear_status_now()
            w = self.tabs.widget(index)
            fade_in_widget(w, duration=260)
            # Если зашли на вкладку Актёры — останавливаем мигание (если было)
            if index == getattr(self, '_actors_tab_idx', -1):
                self._stop_actors_tab_blink()
        except Exception:
            traceback.print_exc()

    def _start_actors_tab_blink(self):
        """Стартует мигание заголовка вкладки «Актёры» (приходит сигнал
        от ActorsView когда генерация рефа закончилась а юзер на другом
        табе). Меняем цвет таба между фиолетовым и обычным каждые 600мс
        пока юзер не зайдёт на эту вкладку."""
        if not hasattr(self, '_actors_tab_idx'):
            return
        idx = self._actors_tab_idx
        if idx < 0 or idx >= self.tabs.count():
            return
        if self.tabs.currentIndex() == idx:
            return  # уже на нужной вкладке — не мигаем
        if not hasattr(self, '_actors_blink_timer'):
            from PyQt6.QtGui import QColor as _QColor
            from PyQt6.QtGui import QFont as _QFont
            self._actors_blink_state = False
            # Запоминаем оригинальный шрифт таба чтобы вернуть его при stop
            self._actors_orig_font = _QFont(
                self.tabs.tabBar().font())
            self._actors_blink_timer = QTimer(self)
            self._actors_blink_timer.setInterval(600)

            def _blink():
                try:
                    self._actors_blink_state = not self._actors_blink_state
                    bar = self.tabs.tabBar()
                    if self._actors_blink_state:
                        # ON state: жирный + оранжевый
                        font = _QFont(self._actors_orig_font)
                        font.setBold(True)
                        bar.setTabTextColor(idx, theme_qcolor("#ffd24d"))
                    else:
                        # OFF state: обычный шрифт + дефолтный цвет
                        font = _QFont(self._actors_orig_font)
                        font.setBold(False)
                        bar.setTabTextColor(idx, theme_qcolor("#cfcfcf"))
                    # Применяем шрифт через setTabFont (PyQt6) или fallback
                    try:
                        bar.setTabFont(idx, font)
                    except (AttributeError, TypeError):
                        # Старый Qt без setTabFont — только цвет меняется
                        pass
                except Exception:
                    pass
            self._actors_blink_timer.timeout.connect(_blink)
        self._actors_blink_timer.start()

    def _stop_actors_tab_blink(self):
        """Останавливает мигание + восстанавливает обычный цвет/шрифт."""
        try:
            if hasattr(self, '_actors_blink_timer'):
                self._actors_blink_timer.stop()
            from PyQt6.QtGui import QColor as _QColor
            idx = getattr(self, '_actors_tab_idx', -1)
            if idx >= 0 and idx < self.tabs.count():
                bar = self.tabs.tabBar()
                bar.setTabTextColor(idx, _QColor())  # default
                if hasattr(self, '_actors_orig_font'):
                    try:
                        bar.setTabFont(idx, self._actors_orig_font)
                    except (AttributeError, TypeError):
                        pass
        except Exception:
            pass

    def _on_ep_title_clicked(self):
        """2026-05-07: клик по кнопке-заголовку эпизода (например «Его план»).
        Открывает NON-MODAL попап с оригинальным текстом сценария этого
        эпизода (из `shows/<slug>/scenarios/epNN.txt`). Если файла нет —
        попап с пометкой «сценарий не сохранён».

        Юзер может держать попап открытым сбоку и продолжать работать
        в Studio параллельно. Если для того же эпизода уже открыт попап —
        просто поднимаем его на передний план (без дубля)."""
        ep_id = self._current_episode
        if not ep_id:
            return
        cur_show = getattr(self, '_current_show', None)
        if not cur_show:
            return
        # Реестр открытых попапов: (show, ep_id) → QDialog. Если уже
        # открыт — raise на передний план вместо создания нового.
        if not hasattr(self, '_ep_scenario_dialogs'):
            self._ep_scenario_dialogs: Dict[tuple, QDialog] = {}
        key = (cur_show, ep_id)
        existing = self._ep_scenario_dialogs.get(key)
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except Exception:
                # Виджет умер — выкидываем из реестра и продолжаем.
                self._ep_scenario_dialogs.pop(key, None)
        # Кандидаты на путь сценария — те же что в EpisodeChatView._read_scenario.
        scen_dir = self._project_root / "shows" / cur_show / "scenarios"
        candidates = [
            scen_dir / f"{ep_id}.txt",
            scen_dir / f"{ep_id.lstrip('ep')}.txt",
        ]
        try:
            num_str = ep_id.lstrip('ep')
            if num_str.isdigit():
                candidates.append(scen_dir / f"ep{int(num_str):02d}.txt")
        except Exception:
            pass
        text = ""
        used_path: Optional[Path] = None
        for p in candidates:
            try:
                if p.exists() and p.is_file():
                    text = p.read_text(encoding='utf-8', errors='replace')
                    used_path = p
                    break
            except Exception:
                continue
        title = get_episode_title(self._meta, ep_id) or ep_id.upper()
        # Попап как ОТДЕЛЬНОЕ ОКНО (Qt.Window) — non-modal, не блокирует
        # главное окно Studio. Кросс-платформенно: на macOS и Win10/11
        # ведёт себя одинаково (отдельная плавающая палитра).
        try:
            dlg = QDialog(self, Qt.WindowType.Window)
            dlg.setWindowModality(Qt.WindowModality.NonModal)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dlg.setWindowTitle(tr('ep_scenario_dialog_title', title=title))
            dlg.setMinimumSize(560, 520)
            dlg.resize(640, 720)
            dlg.setStyleSheet("QDialog { background:#13101a; }")

            v = QVBoxLayout(dlg)
            v.setContentsMargins(20, 18, 20, 16)
            v.setSpacing(10)

            header = QLabel(title)
            header.setStyleSheet(
                "color:#fff; font-size:18px; font-weight:600;")
            v.addWidget(header)

            sub = QLabel(
                tr('ep_scenario_dialog_path',
                   path=str(used_path.name) if used_path else "—"))
            sub.setStyleSheet("color:#888; font-size:11px;")
            v.addWidget(sub)

            edit = QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setPlainText(
                text if text else tr('ep_scenario_dialog_empty', ep=ep_id))
            edit.setStyleSheet(
                "QPlainTextEdit { background:#191b1d; border:1px solid #1d1e20; "
                "border-radius:6px; color:#ddd; padding:10px; font-size:13px;"
                " font-family:'Menlo','Monaco','Courier New',monospace; }")
            # 2026-05-07: подсветка «СЦЕНА N:» / «СЦЕНА N:» / «SCENE N:» —
            # мультиязычно. Юзер хочет глазом сразу выделять границы сцен
            # в монотонном тексте сценария.
            try:
                highlighter = _SceneHighlighter(edit.document())
                # Сохраняем ссылку на highlighter чтобы GC не убил его
                # (Qt не держит сильную ссылку через setDocument).
                edit._scene_highlighter = highlighter
            except Exception:
                traceback.print_exc()
            v.addWidget(edit, stretch=1)

            btns = QHBoxLayout()
            btns.addStretch()
            close_btn = QPushButton(tr('ep_scenario_dialog_close'))
            close_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#aaa;"
                " border:1px solid #4a4a4a; border-radius:6px;"
                " padding:6px 18px; font-size:12px; }"
                "QPushButton:hover { background:#2a2a2a; color:#ddd; }")
            close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_btn.clicked.connect(dlg.close)
            btns.addWidget(close_btn)
            v.addLayout(btns)

            # Удаляем из реестра когда попап закрывается — чтобы при
            # следующем клике создался новый (с актуальным текстом).
            def _on_destroyed(_obj=None, _key=key):
                try:
                    self._ep_scenario_dialogs.pop(_key, None)
                except Exception:
                    pass
            try:
                dlg.destroyed.connect(_on_destroyed)
            except Exception:
                pass

            self._ep_scenario_dialogs[key] = dlg
            # Позиционируем справа от главного окна Studio (если влезает).
            try:
                main_geo = self.geometry()
                screen = self.screen() if hasattr(self, 'screen') else None
                screen_rect = (screen.availableGeometry()
                                if screen is not None else None)
                x = main_geo.x() + main_geo.width() + 20
                y = main_geo.y() + 60
                if (screen_rect is not None
                        and x + dlg.width() > screen_rect.right()):
                    # Не влезает справа — кладём поверх правой части.
                    x = max(screen_rect.left(),
                            screen_rect.right() - dlg.width() - 20)
                dlg.move(x, y)
            except Exception:
                pass
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            traceback.print_exc()

    def _show_refs_view(self):
        """Переключает контент на экран референсов эпизода."""
        if not self._current_episode:
            return
        # Re-click по «РЕФЕРЕНСЫ» когда мы уже на этой странице → no-op:
        # не перерисовываем сетку рефов, не запускаем fade-in. Иначе
        # opacity 0→1 + ребилд layout даёт видимое моргание (тот же класс
        # бага что 2026-05-04 чинили для повторного клика по блоку).
        # 2026-05-07: но ТОЛЬКО если refs view последний раз строился
        # ДЛЯ ЭТОГО ЖЕ эпизода. Если юзер кликнул ep2 пока был на refs
        # ep1, content_stack.currentIndex == 1 (всё ещё refs view),
        # но `_current_episode` уже ep2 — нужна перерисовка с рефами ep2.
        # Раньше не было сравнения с last-built ep, и refs view
        # «застревал» с прошлым эпизодом до клика chat→refs.
        last_built = getattr(self, '_refs_view_built_for_ep', None)
        same_ep_already_built = last_built == self._current_episode
        if (hasattr(self, 'content_stack')
                and self.content_stack.currentIndex() == 1
                and same_ep_already_built):
            if self._pending_ref_notices:
                QTimer.singleShot(150, self._drain_pending_ref_notices)
            # Синхронизируем active-state pill'ов: после _populate_blocks
            # (вызвался при клике на пилюлю эпизода) они все были сброшены
            # в active=false. Юзер на refs view → REFS pill ДОЛЖЕН быть
            # подсвечен синим, иначе UI показывает «нигде не активна».
            if self._refs_pill is not None:
                self._refs_pill.setProperty("active", True)
                self._refs_pill.style().unpolish(self._refs_pill)
                self._refs_pill.style().polish(self._refs_pill)
            chat_pill = getattr(self, '_chat_pill', None)
            if chat_pill is not None:
                chat_pill.setProperty("active", False)
                chat_pill.style().unpolish(chat_pill)
                chat_pill.style().polish(chat_pill)
            return
        # Do not mark the current block as seen when opening References.
        # During background generation current_block can point to a block the
        # user is not visually inspecting, so clearing NEW here loses the
        # "fresh shot" state for hidden carousel blocks.
        for b, btn in self._block_pills.items():
            btn.setProperty("active", False)
            btn.style().unpolish(btn); btn.style().polish(btn)
        if self._refs_pill is not None:
            self._refs_pill.setProperty("active", True)
            self._refs_pill.style().unpolish(self._refs_pill)
            self._refs_pill.style().polish(self._refs_pill)
        chat_pill = getattr(self, '_chat_pill', None)
        if chat_pill is not None:
            chat_pill.setProperty("active", False)
            chat_pill.style().unpolish(chat_pill); chat_pill.style().polish(chat_pill)

        ep = self._current_episode
        self.block_title.setText(tr('refs').upper())
        if hasattr(self, 'seedance_btn'):
            self.seedance_btn.setVisible(False)
        if hasattr(self, 'block_refs_btn'):
            self.block_refs_btn.setVisible(False)

        self._build_refs_view(ep)
        # 2026-05-07: запоминаем для какого ep последний раз строили —
        # чтобы при следующем _show_refs_view'е знать когда можно
        # делать no-op (re-click), а когда нужна перерисовка (другой ep).
        self._refs_view_built_for_ep = ep
        self.content_stack.setCurrentIndex(1)
        self._refresh_stop_btn()
        self.save_btn.hide()
        self._refresh_compact_nav()
        self._clear_status_now()
        fade_in_widget(self.content_stack, duration=260)

        # Если в очереди есть накопленные уведомления об обновлении реф-картинок
        # (юзер был на блоке/настройках во время регенерации) — снимаем подсветку
        # и показываем диалоги по одному. Делаем через singleShot чтобы рисование
        # refs-view успело завершиться раньше попапа.
        if self._pending_ref_notices:
            QTimer.singleShot(150, self._drain_pending_ref_notices)
        else:
            self._set_refs_pill_notice(False)

    def _show_chat_view(self):
        """Переключает контент на чат конкретного эпизода (page index 2).
        Загружает историю из `chats/<ep_id>.jsonl`. Если истории нет — показывается
        хинт «Запусти эпизод через Новый эпизод»."""
        if not self._current_episode:
            return
        # 2026-05-07: уход с refs view — очистить unseen для текущего ep,
        # чтобы при следующем заходе на refs NEW-бейджей не было.
        self._mark_refs_seen_if_leaving()
        # Re-click по «ЧАТ» когда мы уже на чате этого же эпизода → no-op:
        # не зовём set_episode (он бы clear()-нул log_view и перезагрузил
        # историю с нуля → видимое моргание), не fade-in'им. Если
        # _ep_id у view'а отличается от текущего эпизода (юзер сменил
        # эпизод не выходя из чата) — продолжаем как раньше.
        ev = getattr(self, 'episode_chat_view', None)
        if (hasattr(self, 'content_stack')
                and self.content_stack.currentIndex() == 2
                and ev is not None
                and getattr(ev, '_ep_id', None) == self._current_episode):
            # Синхронизация active-state pill'ов после _populate_blocks
            # (см. аналогичный фикс в _show_refs_view выше).
            if self._refs_pill is not None:
                self._refs_pill.setProperty("active", False)
                self._refs_pill.style().unpolish(self._refs_pill)
                self._refs_pill.style().polish(self._refs_pill)
            chat_pill = getattr(self, '_chat_pill', None)
            if chat_pill is not None:
                chat_pill.setProperty("active", True)
                chat_pill.style().unpolish(chat_pill)
                chat_pill.style().polish(chat_pill)
            return
        # Do not clear block NEW badges when switching to Chat; the user may
        # be away from the shot cards while background generation finishes.
        for b, btn in self._block_pills.items():
            btn.setProperty("active", False)
            btn.style().unpolish(btn); btn.style().polish(btn)
        if self._refs_pill is not None:
            self._refs_pill.setProperty("active", False)
            self._refs_pill.style().unpolish(self._refs_pill)
            self._refs_pill.style().polish(self._refs_pill)
        chat_pill = getattr(self, '_chat_pill', None)
        if chat_pill is not None:
            chat_pill.setProperty("active", True)
            chat_pill.style().unpolish(chat_pill); chat_pill.style().polish(chat_pill)
        self.block_title.setText(tr('chat_pill').upper())
        if hasattr(self, 'seedance_btn'):
            self.seedance_btn.setVisible(False)
        if hasattr(self, 'block_refs_btn'):
            self.block_refs_btn.setVisible(False)
        try:
            self.episode_chat_view.set_episode(self._current_episode)
        except Exception:
            pass
        self.content_stack.setCurrentIndex(2)
        self._refresh_stop_btn()
        self.save_btn.hide()
        self._refresh_compact_nav()
        self._clear_status_now()
        fade_in_widget(self.content_stack, duration=260)

    def _show_new_episode_view(self):
        """Кнопка «+» в строке пилюль эпизодов → открывает NewEpisodeView
        (страница 3 в content_stack). UX-переделка 2026-05-04: вкладка
        «Новый эпизод» убрана, всё в Editor'е.

        Re-click по «+» когда уже на этой странице → no-op (тот же
        паттерн что в `_show_refs_view` / `_show_chat_view` для
        исключения мерцания)."""
        if not self._current_show:
            return
        # same-view guard
        if (hasattr(self, 'content_stack')
                and self.content_stack.currentIndex() == 3):
            # Защита: строка блок-пилюль должна быть скрыта (на случай
            # если что-то её показало), pills эпизодов — все false.
            if hasattr(self, 'block_pills_container'):
                self.block_pills_container.hide()
            for ep, ep_btn in self._episode_pills.items():
                ep_btn.setProperty("active", False)
                ep_btn.style().unpolish(ep_btn); ep_btn.style().polish(ep_btn)
            new_pill = getattr(self, '_new_ep_pill', None)
            if new_pill is not None:
                new_pill.setProperty("active", True)
                new_pill.style().unpolish(new_pill); new_pill.style().polish(new_pill)
            return
        # Сбрасываем active у пилюль эпизодов и подсвечиваем «+».
        for ep, ep_btn in self._episode_pills.items():
            ep_btn.setProperty("active", False)
            ep_btn.style().unpolish(ep_btn); ep_btn.style().polish(ep_btn)
        new_pill = getattr(self, '_new_ep_pill', None)
        if new_pill is not None:
            new_pill.setProperty("active", True)
            new_pill.style().unpolish(new_pill); new_pill.style().polish(new_pill)
        # Скрываем строку «Блок 1..N | РЕФЕРЕНСЫ | ЧАТ» — для виртуального
        # «нового» эпизода она не имеет смысла (блоков ещё нет, рефы и чат
        # принадлежат другому эпизоду). Юзер увидит только пилюли ЭП + «+».
        if hasattr(self, 'block_pills_container'):
            self.block_pills_container.hide()
        # Заголовок и переключение страницы
        self.block_title.setText(tr('pill_new_episode_title'))
        if hasattr(self, 'seedance_btn'):
            self.seedance_btn.setVisible(False)
        if hasattr(self, 'block_refs_btn'):
            self.block_refs_btn.setVisible(False)
        self.ep_title_label.setText(tr('new_ep_title'))
        self.ep_title_label.setVisible(False)
        self.ep_dur_label.setText("")
        # Кнопка удаления неактивна для виртуального «нового» эпизода
        if hasattr(self, 'delete_ep_btn'):
            self.delete_ep_btn.setEnabled(False)
            self.delete_ep_btn.setVisible(False)
        if hasattr(self, 'compile_ep_btn'):
            self.compile_ep_btn.setVisible(False)
        self.content_stack.setCurrentIndex(3)
        self._refresh_stop_btn()
        self.save_btn.hide()
        self._refresh_compact_nav()
        self._clear_status_now()
        fade_in_widget(self.content_stack, duration=260)

    def _clear_vbox(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

    def _build_refs_view(self, ep: str):
        """Перерисовывает контент refs_container — 3 секции."""
        self._clear_vbox(self.refs_layout)
        refs = list_episode_refs(ep)
        if not hasattr(self, '_refs_view_signatures'):
            self._refs_view_signatures = {}
        self._refs_view_signatures[ep] = self._refs_signature_from_refs(refs)

        sections = [
            ('refs_locations',  refs['locations'],  'location'),
            ('refs_objects',    refs['objects'],    'object'),
            ('refs_characters', refs['characters'], 'character'),
        ]
        for title_key, items, kind in sections:
            self.refs_layout.addWidget(
                self._build_refs_section(tr(title_key), items, kind))

        self.refs_layout.addStretch()

    def _build_refs_section(self, title: str, refs: List[Dict], kind: str) -> QWidget:
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setSpacing(10)
        bl.setContentsMargins(0, 0, 0, 0)

        # Заголовок секции: «ЛОКАЦИИ   3 файлов» + кнопка «+ Добавить»
        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        h = QLabel(title)
        h.setObjectName("refs-section-header")
        head_row.addWidget(h)
        cnt = QLabel(tr('refs_files', n=len(refs)))
        cnt.setObjectName("refs-section-count")
        head_row.addWidget(cnt)
        head_row.addStretch()
        # 2026-05-05: «+ Добавить персонажа» — переключает на вкладку
        # Актёры с pending-запросом на текущий эпизод. Юзер либо
        # сгенерит нового актёра, либо подтвердит существующего → реф
        # автоматически попадёт в РЕФЕРЕНСЫ этого эпизода.
        # 2026-05-07 (1в): «+ Локация» / «+ Объект» — открывают
        # RefPickerDialog со ВСЕМИ файлами из refs/locations/ или
        # refs/objects/ сериала. Выбор → запись в refs_decisions
        # текущего эпизода (linked). НИКАКОЙ AI-генерации — это просто
        # переиспользование существующих рефов между эпизодами.
        # 2026-05-08: LUMZ-стиль (как `new_show_btn`) — accent_red_subtle.
        # Прозрачный красный фон, тонкий красный border, текст в LUMZ red.
        _lumz_add_qss = (
            "QPushButton {"
            " background: rgba(228, 52, 74, 0.10);"
            " border: 1px solid rgba(228, 52, 74, 0.25);"
            " border-radius: 8px;"
            " color: #e4344a;"
            " padding: 6px 14px;"
            " font-size: 12px;"
            " font-weight: 500; }"
            "QPushButton:hover {"
            " background: rgba(228, 52, 74, 0.18);"
            " border-color: rgba(228, 52, 74, 0.40); }"
            "QPushButton:pressed { background: rgba(228, 52, 74, 0.25); }"
        )
        if kind == 'character':
            add_btn = QPushButton(tr('refs_add_character'))
            add_btn.setStyleSheet(_lumz_add_qss)
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.clicked.connect(self._on_refs_add_character)
            head_row.addWidget(add_btn)
        elif kind in ('location', 'object'):
            add_btn = QPushButton(
                tr('refs_add_location') if kind == 'location'
                else tr('refs_add_object'))
            add_btn.setStyleSheet(_lumz_add_qss)
            add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            add_btn.clicked.connect(
                lambda _checked=False, k=kind: self._on_refs_add_loc_or_obj(k))
            head_row.addWidget(add_btn)
        bl.addLayout(head_row)

        if not refs:
            empty = QLabel(tr('refs_empty'))
            empty.setStyleSheet("color: #555; font-size: 12px; padding: 6px 0;")
            bl.addWidget(empty)
            return box

        # Сетка ВСЕГДА 3 колонки: чтобы карточка не растягивалась на всю ширину
        # когда в секции 1 или 2 файла. Лишние колонки просто остаются пустыми.
        # 4+ файлов — переносятся на следующий ряд.
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setContentsMargins(0, 0, 0, 0)
        cols = 3
        for i in range(cols):
            grid.setColumnStretch(i, 1)
        for i, r in enumerate(refs):
            card = self._build_ref_card(r, kind)
            row, col = divmod(i, cols)
            grid.addWidget(card, row, col)
        bl.addLayout(grid)
        return box

    def _on_ref_character_drop(self, slug: str, src_path: Path) -> None:
        """2026-06-24 (DnD этап 1/7): юзер dropped файл из Finder на
        character-карточку. Копируем файл в refs/characters/<slug>/ под
        исходным именем (NFC normalized; +суффикс _2/_3 при duplicate),
        обновляем refs_decisions.character.<slug>.filename для текущего
        эпизода. Старый файл в папке НЕ трогаем — может пригодиться в
        существующих блоках сториборда (их шапка ссылается на конкретный
        filename, find_ref_image:4008 найдёт старый файл рядом с новым).

        Для locations/objects этот метод НЕ зовётся (DnD пока только character —
        этапы 3 и 5 в roadmap'е DnD).
        """
        ep_id = self._current_episode
        if not ep_id or not slug:
            return
        try:
            src = Path(src_path)
            if not src.is_file():
                return
            target_dir = CHARACTERS_DIR / slug
            target_dir.mkdir(parents=True, exist_ok=True)
            # NFC-нормализация имени: согласовано с find_ref_image (стр.4008).
            # На macOS APFS кириллица хранится в NFD, refs_decisions/.txt
            # промпты пишутся в NFC — иначе mismatch при поиске файла.
            import unicodedata
            target_name = unicodedata.normalize("NFC", src.name)
            target = target_dir / target_name
            # Уникализация: если уже есть файл с таким именем — добавляем
            # суффикс _2, _3, ... (не перезаписываем, чтобы не сломать старые
            # блоки которые ссылаются на исходное имя в шапке .txt).
            if target.exists():
                stem = target.stem
                suffix = target.suffix
                n = 2
                while True:
                    cand = target_dir / f"{stem}_{n}{suffix}"
                    if not cand.exists():
                        target = cand
                        break
                    n += 1
                    if n > 999:  # защита от runaway-цикла
                        return
            import shutil as _shutil
            _shutil.copy2(str(src), str(target))
            # Обновляем episodes.json. Формат filename для character —
            # '<slug>/<filename>' (list_episode_refs:2398-2416 ожидает именно так).
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.get(ep_id) if isinstance(data, dict) else None
            if not isinstance(ep, dict):
                return
            decisions = ep.get('refs_decisions') or {}
            if not isinstance(decisions, dict):
                decisions = {}
            char_bucket = decisions.get('character') or {}
            if not isinstance(char_bucket, dict):
                char_bucket = {}
            entry = char_bucket.get(slug) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry['decision'] = 'linked'
            entry['filename'] = f"{slug}/{target.name}"
            char_bucket[slug] = entry
            decisions['character'] = char_bucket
            ep['refs_decisions'] = decisions
            data[ep_id] = ep
            # Atomic write (тот же паттерн что _set_ref_decision_filename:6740-6748).
            import os as _os
            tmp = meta_path.with_suffix(f'.json.tmp.{_os.getpid()}')
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8')
            _os.replace(tmp, meta_path)
            self._meta = data
            # Перерисовываем РЕФЕРЕНСЫ — карточка покажет новую картинку.
            # 2026-06-24 (фикс краша drop): ОТЛОЖЕННО через QTimer.singleShot(0).
            # _build_refs_view → _clear_vbox.deleteLater сносит секции вместе с
            # RefCard'ами (включая ту, чей dropEvent СЕЙЧАС в стеке). Прямой
            # вызов уничтожал бы виджет внутри его же drop-хендлера → Cocoa
            # drag-сессия финализирует drop на мёртвом target → abort. Отложенный
            # вызов даёт dropEvent вернуться в Qt/Cocoa до сноса дерева.
            QTimer.singleShot(0, lambda e=ep_id: self._build_refs_view(e))
        except Exception:
            traceback.print_exc()

    def _on_ref_location_drop(self, slug, src_path, cur_path) -> None:
        """2026-06-24 (DnD этап 3/7): юзер dropped файл из Finder на location-
        карточку. Новый файл становится каноническим refs/locations/<slug>.<ext>;
        старый (cur_path) архивируется в той же папке как
        <slug>_<YYYY-MM-DD-HHMM>.<old_ext> (НЕ удаляется). В episodes.json
        обновляется refs_decisions.location.<slug>.filename (+decision='linked').
        Перерисовка refs-view ОТЛОЖЕНА через QTimer.singleShot(0) — тот же фикс
        краша что в _on_ref_character_drop (синхронный _build_refs_view сносит
        карточку внутри её же dropEvent -> abort на macOS).

        slug = КЛЮЧ refs_decisions.location (НЕ stem имени файла: в реальных
        данных stem != key, напр. key=gloria_living_room file=gloria_living_room_3.jpg).

        Архив: GUARD — если cur-файл линкуется ещё каким-то ключом location в
        этом show (shared, напр. hale_secret_office.jpg на 2 локации) → COPY
        (оригинал остаётся, сиблинг цел); иначе MOVE (rename).

        HEIC/HEIF: через ОБЩИЙ convert_heic_to_jpg (гибрид C: pillow_heif →
        sips → None). None → отклоняем с сообщением, битый файл НЕ кладём.
        Канонический файл при конвертации = <slug>.jpg. Sibling <slug>_geometry.txt
        НЕ трогаем (этап 7).

        Cross-platform: pathlib.Path + shutil + datetime; heic-конвертер сам
        гейтит платформу. Без shell=True.
        """
        ep_id = self._current_episode
        if not ep_id or not slug:
            return
        try:
            src = Path(src_path)
            if not src.is_file():
                return
            import unicodedata
            import shutil as _shutil
            import os as _os
            ext = src.suffix.lower()
            LOCATIONS_DIR.mkdir(parents=True, exist_ok=True)
            # ── Канонический target. heic/heif → конвертация в jpg. ──────────
            jpg_bytes = None
            if ext in ('.heic', '.heif'):
                jpg_bytes = convert_heic_to_jpg(src)
                if jpg_bytes is None:
                    QMessageBox.warning(
                        self, tr('refs_loc_drop_heic_fail_title'),
                        tr('refs_loc_drop_heic_fail_msg'))
                    return
                target = LOCATIONS_DIR / unicodedata.normalize("NFC", f"{slug}.jpg")
            else:
                target = LOCATIONS_DIR / unicodedata.normalize("NFC", f"{slug}{ext}")

            # ── Архив старого файла (cur_path). НЕ удаляем. ──────────────────
            cur = Path(cur_path) if cur_path else None
            if cur is not None and cur.is_file():
                ts = datetime.datetime.now().strftime('%Y-%m-%d-%H%M')
                arch = LOCATIONS_DIR / unicodedata.normalize(
                    "NFC", f"{slug}_{ts}{cur.suffix}")
                k = 2
                while arch.exists():
                    arch = LOCATIONS_DIR / unicodedata.normalize(
                        "NFC", f"{slug}_{ts}_{k}{cur.suffix}")
                    k += 1
                    if k > 999:
                        return
                shared = self._location_file_is_shared(cur.name, ep_id, slug)
                try:
                    if shared:
                        _shutil.copy2(str(cur), str(arch))  # сиблинг цел
                    else:
                        _shutil.move(str(cur), str(arch))   # rename
                except Exception:
                    traceback.print_exc()

            # ── Пишем новый канонический файл. ──────────────────────────────
            if jpg_bytes is not None:
                target.write_bytes(jpg_bytes)
            else:
                _shutil.copy2(str(src), str(target))

            # ── episodes.json: refs_decisions.location.<slug>.filename ──────
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.get(ep_id) if isinstance(data, dict) else None
            if not isinstance(ep, dict):
                return
            decisions = ep.get('refs_decisions') or {}
            if not isinstance(decisions, dict):
                decisions = {}
            loc_bucket = decisions.get('location') or {}
            if not isinstance(loc_bucket, dict):
                loc_bucket = {}
            entry = loc_bucket.get(slug) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry['decision'] = 'linked'
            entry['filename'] = target.name
            loc_bucket[slug] = entry
            decisions['location'] = loc_bucket
            ep['refs_decisions'] = decisions
            data[ep_id] = ep
            tmp = meta_path.with_suffix(f'.json.tmp.{_os.getpid()}')
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8')
            _os.replace(tmp, meta_path)
            self._meta = data
            # 2026-06-24 (этап 3, автогеометрия): после замены картинки авто-
            # запуск geometry-only (как ↻, но БЕЗ перегенерации картинки —
            # _on_ref_regen затёр бы дроп). Молча, без попапа. Только если
            # Claude CLI есть; дедуп-гард по _active_geometry_paths (повторный
            # дроп пока идёт geometry не плодит второй тред). Стартуем ДО
            # отложенного rebuild — _active_geometry_paths уже содержит target,
            # новая карточка сразу покажет непрерывный busy-overlay (1:1 _on_ref_done).
            # Свой try/except: сбой старта геометрии НЕ должен сорвать rebuild ниже.
            try:
                if find_claude_cli() is not None and \
                        target.resolve() not in self._active_geometry_paths:
                    self._start_geometry_thread(target, source_ep_id=ep_id)
            except Exception:
                traceback.print_exc()
            # Перерисовка ОТЛОЖЕНА (фикс краша, см. _on_ref_character_drop).
            QTimer.singleShot(0, lambda e=ep_id: self._build_refs_view(e))
        except Exception:
            traceback.print_exc()

    def _location_file_is_shared(self, filename, cur_ep, cur_slug) -> bool:
        """True если filename линкуется ещё каким-то ключом location в любом
        эпизоде текущего show (кроме самой пары cur_ep/cur_slug). Защищает от
        MOVE-архивации файла, который шарят несколько локаций (напр.
        hale_secret_office.jpg на penthouse_kitchen_morning + yacht_cabin_maren)."""
        try:
            data = read_episodes_meta(SHOW_ROOT)
            if not isinstance(data, dict):
                return False
            for ep_id, ep in data.items():
                if not isinstance(ep, dict):
                    continue
                loc = (ep.get('refs_decisions') or {}).get('location') or {}
                if not isinstance(loc, dict):
                    continue
                for k, v in loc.items():
                    if not isinstance(v, dict):
                        continue
                    if v.get('decision') != 'linked':
                        continue
                    if v.get('filename') != filename:
                        continue
                    if ep_id == cur_ep and k == cur_slug:
                        continue
                    return True
            return False
        except Exception:
            return False

    def _on_ref_object_drop(self, slug, src_path, cur_path) -> None:
        """2026-06-24 (DnD этап 5/7): юзер dropped файл из Finder на object-
        карточку. Новый файл = канонический refs/objects/<slug>.<ext>; старый
        (cur_path) архивируется как <slug>_<YYYY-MM-DD-HHMM>.<old_ext> (НЕ
        удаляется). refs_decisions.object.<slug>.filename обновляется
        (+decision='linked'). Перерисовка ОТЛОЖЕНА QTimer.singleShot(0) (фикс
        краша, как у локаций).

        Отличие от _on_ref_location_drop: БЕЗ автогеометрии (у объектов geometry-
        агента нет; sibling <slug>_prompt.txt — это GENERATION-промпт, его НЕ
        трогаем, остаётся от старого объекта).

        slug = КЛЮЧ refs_decisions.object (НЕ stem: в данных stem != key, напр.
        key=impala_burgundy file=impala_burgundy_2.jpg).

        Архив: GUARD _object_file_is_shared — если cur-файл линкуется ещё каким-то
        ключом object в show (у объектов шаринг массовый: printout_bank_statements.jpg
        на 'printout_bank_statements' + 'shareholder_register') → COPY (сиблинг
        цел); иначе MOVE.

        HEIC/HEIF: общий convert_heic_to_jpg → None → отклоняем
        (refs_loc_drop_heic_fail_*, переиспользуем). Канонический при конвертации
        = <slug>.jpg.

        Cross-platform: pathlib.Path + shutil + datetime; heic-конвертер сам гейтит
        платформу. Без shell=True.
        """
        ep_id = self._current_episode
        if not ep_id or not slug:
            return
        try:
            src = Path(src_path)
            if not src.is_file():
                return
            import unicodedata
            import shutil as _shutil
            import os as _os
            ext = src.suffix.lower()
            OBJECTS_DIR.mkdir(parents=True, exist_ok=True)
            # ── Канонический target. heic/heif → конвертация в jpg. ──────────
            jpg_bytes = None
            if ext in ('.heic', '.heif'):
                jpg_bytes = convert_heic_to_jpg(src)
                if jpg_bytes is None:
                    QMessageBox.warning(
                        self, tr('refs_loc_drop_heic_fail_title'),
                        tr('refs_loc_drop_heic_fail_msg'))
                    return
                target = OBJECTS_DIR / unicodedata.normalize("NFC", f"{slug}.jpg")
            else:
                target = OBJECTS_DIR / unicodedata.normalize("NFC", f"{slug}{ext}")

            # ── Архив старого файла (cur_path). НЕ удаляем. ──────────────────
            cur = Path(cur_path) if cur_path else None
            if cur is not None and cur.is_file():
                ts = datetime.datetime.now().strftime('%Y-%m-%d-%H%M')
                arch = OBJECTS_DIR / unicodedata.normalize(
                    "NFC", f"{slug}_{ts}{cur.suffix}")
                k = 2
                while arch.exists():
                    arch = OBJECTS_DIR / unicodedata.normalize(
                        "NFC", f"{slug}_{ts}_{k}{cur.suffix}")
                    k += 1
                    if k > 999:
                        return
                shared = self._object_file_is_shared(cur.name, ep_id, slug)
                try:
                    if shared:
                        _shutil.copy2(str(cur), str(arch))  # сиблинг цел
                    else:
                        _shutil.move(str(cur), str(arch))   # rename
                except Exception:
                    traceback.print_exc()

            # ── Пишем новый канонический файл. ──────────────────────────────
            if jpg_bytes is not None:
                target.write_bytes(jpg_bytes)
            else:
                _shutil.copy2(str(src), str(target))

            # ── episodes.json: refs_decisions.object.<slug>.filename ────────
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.get(ep_id) if isinstance(data, dict) else None
            if not isinstance(ep, dict):
                return
            decisions = ep.get('refs_decisions') or {}
            if not isinstance(decisions, dict):
                decisions = {}
            obj_bucket = decisions.get('object') or {}
            if not isinstance(obj_bucket, dict):
                obj_bucket = {}
            entry = obj_bucket.get(slug) or {}
            if not isinstance(entry, dict):
                entry = {}
            entry['decision'] = 'linked'
            entry['filename'] = target.name
            obj_bucket[slug] = entry
            decisions['object'] = obj_bucket
            ep['refs_decisions'] = decisions
            data[ep_id] = ep
            tmp = meta_path.with_suffix(f'.json.tmp.{_os.getpid()}')
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8')
            _os.replace(tmp, meta_path)
            self._meta = data
            # Перерисовка ОТЛОЖЕНА (фикс краша, см. _on_ref_character_drop).
            # У объектов автогеометрии НЕТ — sibling <slug>_prompt.txt не трогаем.
            QTimer.singleShot(0, lambda e=ep_id: self._build_refs_view(e))
        except Exception:
            traceback.print_exc()

    def _object_file_is_shared(self, filename, cur_ep, cur_slug) -> bool:
        """True если filename линкуется ещё каким-то ключом object в любом
        эпизоде текущего show (кроме самой пары cur_ep/cur_slug). Защищает от
        MOVE-архивации файла, который шарят несколько объектов (шаринг массовый:
        printout_bank_statements.jpg на 'printout_bank_statements' + 'shareholder_register')."""
        try:
            data = read_episodes_meta(SHOW_ROOT)
            if not isinstance(data, dict):
                return False
            for ep_id, ep in data.items():
                if not isinstance(ep, dict):
                    continue
                obj = (ep.get('refs_decisions') or {}).get('object') or {}
                if not isinstance(obj, dict):
                    continue
                for k, v in obj.items():
                    if not isinstance(v, dict):
                        continue
                    if v.get('decision') != 'linked':
                        continue
                    if v.get('filename') != filename:
                        continue
                    if ep_id == cur_ep and k == cur_slug:
                        continue
                    return True
            return False
        except Exception:
            return False

    def _on_ref_character_remove(self, slug: str):
        """2026-05-05: клик «🗑 Удалить» на character-карточке в РЕФЕРЕНСАХ.
        Удаляет запись из `refs_decisions[character][slug]` для текущего
        эпизода. Файл рефа на диске НЕ трогаем — может пригодиться
        в другом эпизоде через «📁 Выбрать существующий»."""
        ep_id = self._current_episode
        if not ep_id or not slug:
            return
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(tr('refs_remove_char_title'))
            box.setText(tr('refs_remove_char_msg', slug=slug, ep=ep_id))
            yes_btn = box.addButton(tr('delete_ep_yes'),
                                    QMessageBox.ButtonRole.DestructiveRole)
            no_btn = box.addButton(tr('delete_ep_no'),
                                   QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no_btn)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
        except Exception:
            traceback.print_exc()
            return
        # Удаляем запись из episodes.json
        try:
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.get(ep_id)
            if isinstance(ep, dict):
                decisions = ep.get('refs_decisions') or {}
                bucket = decisions.get('character') or {}
                if slug in bucket:
                    del bucket[slug]
                if not bucket:
                    decisions.pop('character', None)
                if not decisions:
                    ep.pop('refs_decisions', None)
                meta_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                self._meta = data
                # Перерисовываем РЕФЕРЕНСЫ
                self._build_refs_view(ep_id)
        except Exception:
            traceback.print_exc()

    def _on_refs_add_loc_or_obj(self, kind: str):
        """2026-05-07 (1в): клик «+ Добавить локацию» / «+ Добавить объект»
        в секциях location/object вкладки РЕФЕРЕНСЫ.

        Открывает RefPickerDialog со ВСЕМИ файлами из refs/<kind>s/ сериала.
        Юзер кликает миниатюру → подтверждает в попапе (1г) → файл
        записывается в `refs_decisions[<kind>][<slug>]` текущего эпизода
        как `linked`. Карточка появляется в РЕФЕРЕНСАХ.

        В отличие от кнопки «+ Добавить персонажа», тут НЕТ перехода на
        вкладку Актёры и НЕТ AI-генерации. Это переиспользование
        существующих файлов между эпизодами.

        kind: 'location' | 'object'.
        """
        if kind not in ('location', 'object'):
            return
        ep_id = self._current_episode
        if not ep_id:
            return
        # Каталог рефов по типу
        folder = LOCATIONS_DIR if kind == 'location' else OBJECTS_DIR
        try:
            if not folder.is_dir():
                return
        except Exception:
            return
        # Импорт здесь чтобы избежать circular import при frozen-сборке.
        try:
            from widgets.ref_picker_dialog import RefPickerDialog
        except Exception:
            traceback.print_exc()
            return
        title = (tr('refs_picker_title_location') if kind == 'location'
                 else tr('refs_picker_title_object'))
        try:
            dlg = RefPickerDialog(folder, title, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            picked = dlg.selected_filename
            if not picked:
                return
        except Exception:
            traceback.print_exc()
            return
        # Записываем как linked-decision. slug = stem файла без расширения.
        slug = Path(picked).stem.lower()
        try:
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.setdefault(ep_id, {})
            decisions = ep.setdefault('refs_decisions', {})
            bucket = decisions.setdefault(kind, {})
            bucket[slug] = {'decision': 'linked', 'filename': picked}
            meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            self._meta = data
            self._build_refs_view(ep_id)
        except Exception:
            traceback.print_exc()

    def _on_refs_add_character(self):
        """2026-05-05: клик «+ Добавить персонажа» в секции character
        вкладки РЕФЕРЕНСЫ. Переключает на вкладку 🎭 Актёры + ставит
        pending-запрос «привязать любой следующий character-реф к
        текущему эпизоду» (slug=='', wildcard).

        В отличие от потока через чат (где slug известен заранее),
        здесь юзер сам выбирает персонажа в попапе создания референса.
        После «✓ Оставить этот» auto-link сработает по `target_dir.name`."""
        ep_id = self._current_episode
        if not ep_id:
            return
        try:
            actors_view = getattr(self, 'actors_view', None)
            if actors_view is None:
                return
            # Помечаем wildcard-запрос: любой следующий character-реф
            # завершённый этим юзером → линкуется к ep_id.
            actors_view.set_pending_any_character_for_ep(ep_id)
            tabs = getattr(self, 'tabs', None)
            idx = getattr(self, '_actors_tab_idx', -1)
            if tabs is not None and idx is not None and idx >= 0:
                tabs.setCurrentIndex(idx)
        except Exception:
            traceback.print_exc()

    def _build_ref_card(self, r: Dict, kind: str) -> QFrame:
        """Создаёт RefCard с привязанными обработчиками.
        kind: 'location' | 'object' | 'character' — определяет какие кнопки
        показывать в hover-overlay (характеры — без кнопок, только клик→fullscreen).
        """
        card = RefCard(r, kind)
        # Клик по картинке (любой kind) — открыть в полноэкранном просмотре
        card.image_clicked.connect(lambda p=r['path']: self._open_fullscreen(p))
        # Кнопки overlay — только для локаций и объектов
        if kind in ('location', 'object'):
            card.regen_requested.connect(lambda p=r['path'], k=kind: self._on_ref_regen(p, k))
            card.edit_requested.connect(lambda p=r['path'], k=kind: self._on_ref_edit(p, k))
            card.delete_requested.connect(lambda p=r['path'], k=kind: self._on_ref_delete(p, k))
            # 2026-06-24 (DnD этап 3/7 + 5/7): drop файла из Finder на location-
            # или object-карточку → новый файл = канонический <slug>.<ext>, старый
            # архивируется датой, refs_decisions.<kind>.<slug> обновляется.
            # slug = КЛЮЧ decisions (r['slug']), НЕ stem пути (в данных stem≠key).
            if kind == 'location':
                card.image_dropped.connect(
                    lambda src, s=r.get('slug'), cur=r['path']:
                        self._on_ref_location_drop(s, src, cur))
            elif kind == 'object':
                card.image_dropped.connect(
                    lambda src, s=r.get('slug'), cur=r['path']:
                        self._on_ref_object_drop(s, src, cur))
        elif kind == 'character':
            # 2026-05-05: для character-карточки только «🗑 Удалить».
            # Удаляет запись из refs_decisions[character][slug] —
            # реф пропадает из РЕФЕРЕНСОВ эпизода. Файл на диске НЕ
            # трогаем (можно переиспользовать в другом эпизоде).
            slug = Path(r['filename']).stem if '/' not in str(r.get('filename') or '') \
                else str(r['filename']).split('/', 1)[0]
            # Безопаснее — берём из имени папки родителя файла.
            try:
                slug = Path(r['path']).parent.name
            except Exception:
                pass
            card.delete_requested.connect(
                lambda s=slug: self._on_ref_character_remove(s))
            # 2026-06-24 (DnD этап 1/7): drop файла из Finder на character-
            # карточку → копируем в refs/characters/<slug>/ + обновляем filename
            # в refs_decisions для текущего эпизода. Старый файл в папке остаётся.
            card.image_dropped.connect(
                lambda src, s=slug: self._on_ref_character_drop(s, src))
        # Если для этой картинки прямо сейчас идёт фоновое обновление geometry
        # (refs-view был перерисован после _on_ref_done пока Claude ещё не
        # дописал geometry-файл) — карточку нужно вернуть в busy-состояние,
        # чтобы юзер случайно не запустил ПОВТОРНУЮ регенерацию кликом по
        # картинке, пока ждёт «✓ geometry обновлена».
        try:
            if Path(r['path']).resolve() in self._active_geometry_paths:
                card.set_geometry_updating(True)
        except Exception:
            pass
        # 2026-05-07: image-gen фаза в реестре — навесить busy_overlay с
        # надписью «Генерирую изображение» / «Обновляю картинку». Это
        # восстанавливает overlay при пересоборе refs view (watcher
        # debounce, manual rebuild и т.п.) — раньше overlay пропадал
        # на ~1с между триггером watcher'а и `_on_ref_done`'ом.
        try:
            entry = self._active_image_paths.get(
                Path(r['path']).resolve())
            if entry and hasattr(card, 'set_image_updating'):
                card.set_image_updating(
                    True, entry.get('label_key', '') or '',
                    started_at=entry.get('started_at'))
        except Exception:
            pass
        # 2026-05-07: NEW-бейдж — если путь карточки в `_unseen_refs`
        # текущего эпизода (юзер обновил реф через regen/edit, ещё не
        # ушёл с refs view). Применимо для location и object.
        try:
            ep_id = self._current_episode
            if ep_id and hasattr(card, 'set_new_badge'):
                bucket = self._unseen_refs.get(ep_id, set())
                is_unseen = Path(r['path']).resolve() in bucket
                card.set_new_badge(is_unseen)
        except Exception:
            pass
        return card

    def _block_indicator_for(self, block_name: str) -> str:
        """Префикс текста пилюли — анимация точек во время регенерации.
        NEW визуально показывается через property `unseen` и CSS (оранжевый фон),
        а не через эмодзи в тексте."""
        has_active = (
            any(b == block_name for (b, _) in self._active_regens.keys())
            or any(b == block_name
                   for (b, _p, _v) in self._active_mode_c_version_threads.keys()))
        if has_active:
            dots_pattern = ["·    ", "· ·  ", "· · ·"]
            return dots_pattern[self._dot_step] + "  "
        return ""

    def _format_block_label(self, block_name: str) -> str:
        """Текст пилюли блока — «Блок N» / «Block N» (+ префикс точек при генерации)."""
        m = re.match(r'.*_block_(\d+)', block_name)
        base = f"{tr('block')} {m.group(1)}" if m else block_name
        return self._block_indicator_for(block_name) + base

    def _format_compact_block_label(self, block_name: str) -> str:
        """Short block pill label for compact navigation: only the block number."""
        m = re.match(r'.*_block_(\d+)', block_name)
        return m.group(1) if m else block_name

    def _refresh_block_indicator(self, block_name: str):
        """Обновляет пилюлю блока: текст (точки если идёт генерация) + property `unseen`."""
        btn = self._block_pills.get(block_name)
        if btn is None:
            self._refresh_compact_nav()
            self._refresh_stop_btn()
            return
        self._apply_block_pill_state(block_name, btn)
        self._refresh_compact_nav()
        self._refresh_stop_btn()

    def _block_has_active_generation(self, block_name: str) -> bool:
        return (
            any(b == block_name for (b, _) in self._active_regens.keys())
            or any(b == block_name
                   for (b, _p, _v) in self._active_mode_c_version_threads.keys()))

    def _block_has_unseen_shots(self, block_name: str) -> bool:
        # NEW показывается ТОЛЬКО когда нет активных генераций (взаимоисключающие).
        return (
            not self._block_has_active_generation(block_name)
            and any(b == block_name for (b, _) in self._unseen_shots)
        )

    def is_version_unseen(self, block_name: str, panel_idx: int,
                          version_n: int) -> bool:
        """NEW на плитке версии в попапе (геттер для ShotViewerDialog.refresh)."""
        return (block_name, panel_idx, int(version_n)) in self._unseen_versions

    def mark_version_seen(self, block_name: str, panel_idx: int,
                          version_n: int) -> None:
        """Явный клик по версии в попапе снимает её NEW (закрытие/смена блока — нет)."""
        self._unseen_versions.discard((block_name, panel_idx, int(version_n)))

    def _apply_block_pill_state(self, block_name: str, btn: QPushButton) -> None:
        """Apply active/generating/unseen state from registries to one block pill.

        Compact carousel buttons may be outside the visible viewport when a
        background shot finishes. Their visual state must not depend on whether
        the user could see the button at that moment.
        """
        has_active = self._block_has_active_generation(block_name)
        has_unseen = self._block_has_unseen_shots(block_name)
        if bool(btn.property("compact")):
            btn.setText(self._format_compact_block_label(block_name))
        else:
            btn.setText(self._format_block_label(block_name))
        btn.setProperty("active", bool(
            self._is_blocks_view_active() and self.current_block == block_name))
        btn.setProperty("generating", has_active)
        btn.setProperty("gen_phase", str(getattr(self, '_block_gen_phase', 0)))
        btn.setProperty("unseen", has_unseen)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _sync_block_pill_states(self) -> None:
        """Refresh all block pills from current active/unseen registries."""
        try:
            for block_name, btn in getattr(self, '_block_pills', {}).items():
                self._apply_block_pill_state(block_name, btn)
        except Exception:
            traceback.print_exc()

    def _refresh_stop_btn(self):
        """Видимость кнопки «Остановить»: ТОЛЬКО на странице блоков (content_stack
        index 0) И только если открытый эпизод == эпизод, чьи блоки сейчас
        генерятся (ep-префикс ключей реестров: b.split('_block_')[0]). После
        клика держим «Останавливаю…» пока реестры не опустеют."""
        self._refresh_clear_btn()
        btn = getattr(self, 'stop_gen_btn', None)
        if btn is None:
            return
        on_blocks = (hasattr(self, 'content_stack')
                     and self.content_stack.currentIndex() == 0)
        ep = getattr(self, '_current_episode', None)
        def _ep_of(b):
            return b.split('_block_', 1)[0]
        active = bool(
            on_blocks and ep is not None and (
                any(_ep_of(b) == ep for (b, _p) in self._active_regens)
                or any(_ep_of(b) == ep
                       for (b, _p, _v) in self._active_mode_c_version_threads)))
        try:
            btn.setVisible(active)
            if active and not getattr(self, '_generation_stopped', False):
                btn.setEnabled(True)
                btn.setText(tr('stop_gen_btn'))
        except Exception:
            pass

    def _on_stop_gen_btn(self):
        """Клик «Остановить генерацию» → подтверждение (Cancel по умолчанию) →
        глобальный стоп. Cancel → кнопка остаётся активной. Ok → «Останавливаю…»
        (disabled) + _stop_all_generation."""
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr('stop_confirm_title'))
            box.setText(tr('stop_confirm_text'))
            box.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Ok).setText(
                tr('stop_confirm_ok'))
            box.button(QMessageBox.StandardButton.Cancel).setText(
                tr('stop_confirm_cancel'))
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Ok:
                return
        except Exception:
            traceback.print_exc()
            return
        try:
            self.stop_gen_btn.setEnabled(False)
            self.stop_gen_btn.setText(tr('stop_gen_running'))
        except Exception:
            pass
        self._stop_all_generation()

    def _stop_all_generation(self):
        """Глобальный стоп: гасит текущий блок И всю очередь следующих. Уже
        доставленные картинки остаются. Кооперативно (без terminate): стоп-флаг
        тредам + утилизация через жнец (без wait в UI) + best-effort серверная
        отмена в фоне."""
        self._generation_stopped = True
        # 1) upstream pipeline-тред (пишет .txt блоков) — стоп, чтобы не плодил
        #    новые блоки и не жёг claude-токены.
        try:
            ev = getattr(self, 'episode_chat_view', None)
            t = getattr(ev, '_storyboard_pipeline_thread', None) if ev else None
            if t is not None and hasattr(t, 'stop'):
                t.stop()
        except Exception:
            traceback.print_exc()
        # 1b) Seedance-промпт-пайплайны (Opus 4.7) идут ПАРАЛЛЕЛЬНО шотам и НЕ
        #     гаснут со стопом картинок. Стоп-кнопка = «остановить ВСЁ по эпизоду»,
        #     поэтому глушим и их (stop() терминирует живой claude-Popen). Покрывает
        #     основной пайплайн чата эпизода + регенерации Seedance-промптов.
        try:
            ev = getattr(self, 'episode_chat_view', None)
            st = getattr(ev, '_seedance_pipeline_thread', None) if ev else None
            if st is not None and hasattr(st, 'stop'):
                st.stop()
        except Exception:
            traceback.print_exc()
        try:
            for st in list(getattr(self, '_seedance_regen_threads', []) or []):
                if st is not None and hasattr(st, 'stop'):
                    st.stop()
        except Exception:
            traceback.print_exc()
        # 2) очередь блоков — очистить, активный сбросить (автопереход подавлен).
        try:
            self._storyboard_blocks_queue.clear()
        except Exception:
            pass
        # 2b) конвейер по шотам — очистить сквозное окно (новые шоты не заводятся).
        try:
            self._shot_window_queue.clear()
            self._shot_window_inflight.clear()
            self._shot_window_versions_left.clear()
            # строгий порядок блоков — сброс буфера упорядочивания
            self._block_release_ready.clear()
            self._block_release_next = None
            # авто-добор per-shot — сброс бюджета проходов
            self._shot_retry_pass.clear()
        except Exception:
            pass
        self._storyboard_active_block = None
        self._storyboard_active_pending = 0
        # 3) собрать (op_id, key) летящих задач ДО остановки (для серверной отмены).
        threads = (list(self._active_regens.values())
                   + list(self._active_mode_c_version_threads.values()))
        pairs = []
        for t in threads:
            oid = getattr(t, '_op_id', "") or ""
            k = getattr(t, '_used_key', "") or ""
            if oid and k:
                pairs.append((oid, k))
        # 4) кооперативный стоп + утилизация через жнец (НЕ wait в UI-потоке).
        for t in threads:
            try:
                if hasattr(t, 'stop'):
                    t.stop()
            except Exception:
                pass
            self._retire_thread(t)
        self._active_regens.clear()
        self._active_mode_c_version_threads.clear()
        # 5) серверная отмена в фоне (best-effort, ошибки глотаем).
        if pairs:
            try:
                from threads.cancel import CancelThread
                ct = CancelThread(pairs, self)
                ct.finished.connect(lambda c=ct: self._retire_thread(c))
                self._cancel_threads = getattr(self, '_cancel_threads', [])
                self._cancel_threads.append(ct)
                ct.start()
            except Exception:
                traceback.print_exc()
        # 6) UI: погасить прогресс/секунды карточек текущего блока — перерисовка
        #    в idle (реестры уже пусты → _shot_active=False для всех карточек,
        #    set_loading(False)+stop_progress()); без ухода в другой блок.
        try:
            if self.current_block:
                self._display_block(self.current_block)
        except Exception:
            traceback.print_exc()
        # Индикаторы всех блоков (снять точки) + спрятать кнопку.
        try:
            for bn in list(getattr(self, '_block_pills', {}).keys()):
                self._refresh_block_indicator(bn)
        except Exception:
            pass
        self._refresh_stop_btn()
        try:
            self.status_bar.showMessage(tr('stop_gen_done'))
        except Exception:
            pass

    def _refresh_clear_btn(self):
        """Видимость «Очистить блок»: ТОЛЬКО на странице блоков (content_stack=0)
        И если в текущем блоке есть хотя бы один файл шота И НЕТ активных тредов
        этого блока (очистка во время генерации запрещена)."""
        btn = getattr(self, 'clear_block_btn', None)
        if btn is None:
            return
        block = self.current_block
        on_blocks = (hasattr(self, 'content_stack')
                     and self.content_stack.currentIndex() == 0)
        has_img = bool(block) and any(
            shot_path(block, i).exists() for i in range(PANELS))
        busy = bool(block) and (
            any(b == block for (b, _p) in self._active_regens)
            or any(b == block
                   for (b, _p, _v) in self._active_mode_c_version_threads))
        try:
            btn.setVisible(bool(on_blocks and has_img and not busy))
        except Exception:
            pass

    def _on_clear_block_btn(self):
        """Клик «Очистить блок» → подтверждение (Cancel по умолчанию) → удаление.
        Запрещено при активной генерации блока (кнопка и так скрыта — двойная защита)."""
        block = self.current_block
        if not block:
            return
        busy = (any(b == block for (b, _p) in self._active_regens)
                or any(b == block
                       for (b, _p, _v) in self._active_mode_c_version_threads))
        if busy:
            return
        m = re.match(r'(ep\d+)_block_(\d+)', block)
        n = m.group(2) if m else "?"
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr('clear_confirm_title', n=n))
            box.setText(tr('clear_confirm_text'))
            box.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Ok).setText(
                tr('clear_confirm_ok'))
            box.button(QMessageBox.StandardButton.Cancel).setText(
                tr('clear_confirm_cancel'))
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Ok:
                return
        except Exception:
            traceback.print_exc()
            return
        self._clear_current_block(block)

    def _delete_shot_files(self, block, i):
        """Сносит с диска ОДИН слот шота: активный файл {block}_shot{i+1}.jpg +
        папка истории версий _history/{block}_shot{i+1}/ (поимённый unlink + rmdir)
        + косметика (gen_time, unseen, started_at). ОБЩИЙ для _clear_current_block
        (цикл по всем слотам) и _on_delete_shot (один слот). Монтажку/промпт/рефы
        НЕ трогает. Cross-platform: pathlib.Path unlink/rmdir, без shell."""
        try:
            shot_path(block, i).unlink(missing_ok=True)
        except Exception:
            traceback.print_exc()
        # История версий шота — папка _history/{block}_shot{N}/ (НА ШОТ):
        # поимённый unlink файлов + rmdir пустой (не rmtree).
        try:
            hist = shot_history_dir(block, i)
            if hist.exists() and hist.is_dir():
                for f in list(hist.iterdir()):
                    try:
                        if f.is_file():
                            f.unlink()
                    except Exception:
                        pass
                try:
                    hist.rmdir()
                except Exception:
                    pass
        except Exception:
            traceback.print_exc()
        # Косметика: время генерации + unseen/overlay.
        try:
            QSettings(APP_ORG, APP_NAME).remove(
                f"gen_time_{block}_shot{i + 1}")
        except Exception:
            pass
        self._unseen_shots.discard((block, i))
        self._shot_gen_started_at.pop((block, i), None)

    def _clear_current_block(self, block: str):
        """Адресно удаляет с диска ВСЕ шоты и версии блока + косметику + склеенный
        лист. Рефы, монтажку, .txt-промпт, соседние блоки НЕ трогает. Без rm -rf
        по storyboards — только поимённый unlink файлов этого блока."""
        for i in range(PANELS):
            self._delete_shot_files(block, i)
        # Склеенный лист блока: .cache/_block_view/{ep}_block{N}/{ep}_block{N}.*
        try:
            m = re.match(r'(ep\d+)_block_(\d+)', block)
            if m and self._current_show:
                ep_id, bn = m.group(1), int(m.group(2))
                sheet_dir = (self._project_root / "shows" / self._current_show
                             / ".cache" / "_block_view" / f"{ep_id}_block{bn}")
                for ext in ("jpg", "jpeg", "png"):
                    try:
                        (sheet_dir / f"{ep_id}_block{bn}.{ext}").unlink(
                            missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()
        # Закрыть открытые ShotViewer этого блока (версии удалены).
        try:
            for b, _p, dlg in list(self._ensure_open_shot_viewers()):
                if b == block:
                    try:
                        dlg.close()
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()
        # UI: карточки → ПУСТО (перерисовка, файлы удалены) + индикатор/кнопки.
        try:
            if self.current_block == block:
                self._display_block(block)
        except Exception:
            traceback.print_exc()
        try:
            self._refresh_block_indicator(block)
        except Exception:
            pass
        try:
            self.status_bar.showMessage(tr('clear_done'))
        except Exception:
            pass

    def _on_delete_shot(self, panel_idx: int) -> None:
        """Удаляет ОДИН шот (активный файл + все версии в _history) текущего блока.
        Слот опустошается (ДЫРКА, не сдвиг) — монтажка/промпт/рефы/соседние шоты НЕ
        трогаются, реген перезальёт слот из описания shot_num. Запрещено пока
        ИМЕННО этот шот генерируется. Поведение = Clear блока, суженный до одного
        слота (общий хелпер _delete_shot_files)."""
        block = self.current_block
        if not block:
            return
        # busy-гард по (block, panel_idx): нельзя сносить файлы под пишущим тредом.
        busy = (any(b == block and p == panel_idx
                    for (b, p) in self._active_regens)
                or any(b == block and p == panel_idx
                       for (b, p, _v) in self._active_mode_c_version_threads))
        if busy:
            return
        # Нечего удалять — пустой слот.
        try:
            if not shot_path(block, panel_idx).exists():
                return
        except Exception:
            return
        # Попап Warning (Cancel по умолчанию), текст про ОДИН шот.
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr('shot_delete_confirm_title', n=panel_idx + 1))
            box.setText(tr('shot_delete_confirm_text', n=panel_idx + 1))
            box.setStandardButtons(
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            box.button(QMessageBox.StandardButton.Ok).setText(
                tr('shot_delete_confirm_ok'))
            box.button(QMessageBox.StandardButton.Cancel).setText(
                tr('shot_delete_confirm_cancel'))
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Ok:
                return
        except Exception:
            traceback.print_exc()
            return
        # Снос файлов слота (общий хелпер) — поведение = один проход Clear-блока.
        self._delete_shot_files(block, panel_idx)
        # Склеенный лист блока стал устаревшим — удаляем (регенерится при сборке).
        try:
            m = re.match(r'(ep\d+)_block_(\d+)', block)
            if m and self._current_show:
                ep_id, bn = m.group(1), int(m.group(2))
                sheet_dir = (self._project_root / "shows" / self._current_show
                             / ".cache" / "_block_view" / f"{ep_id}_block{bn}")
                for ext in ("jpg", "jpeg", "png"):
                    try:
                        (sheet_dir / f"{ep_id}_block{bn}.{ext}").unlink(
                            missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()
        # Закрыть открытый ShotViewer ИМЕННО этого шота (версии удалены).
        try:
            for b, _p, dlg in list(self._ensure_open_shot_viewers()):
                if b == block and _p == panel_idx:
                    try:
                        dlg.close()
                    except Exception:
                        pass
        except Exception:
            traceback.print_exc()
        # UI: перерисовка блока (слот станет blank) + индикатор.
        try:
            if self.current_block == block:
                self._display_block(block)
        except Exception:
            traceback.print_exc()
        try:
            self._refresh_block_indicator(block)
        except Exception:
            pass

    def _mark_block_seen(self, block_name: str):
        """Очищает бейджи NEW у всех шотов указанного блока + обновляет индикатор."""
        keys = [(b, i) for (b, i) in self._unseen_shots if b == block_name]
        if not keys:
            return
        for k in keys:
            self._unseen_shots.discard(k)
        # Оранжевый акцент NEW на пилюле блока должен пропасть после просмотра
        self._refresh_block_indicator(block_name)

    # ── 2026-05-07: helpers для NEW-бейджа на рефах + мигание пилюли ──

    def _mark_ref_unseen(self, ep_id: Optional[str], image_path: Path):
        """Помечает реф «новым» — после успешной regen/edit. Применяется:
        • Карточка получит NEW-бейдж при следующей пересборке refs view.
        • Пилюля «РЕФЕРЕНСЫ» начнёт мигать если юзер не на refs view
          этого эпизода.
        Безопасно: если ep_id пустой — no-op."""
        if not ep_id:
            return
        try:
            p = image_path.resolve()
        except Exception:
            p = image_path
        self._unseen_refs.setdefault(ep_id, set()).add(p)
        # Мигание pill: только если юзер не на refs view ЭТОГО эпизода.
        on_refs_view = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
            and self._current_episode == ep_id
        )
        if not on_refs_view:
            try:
                self._set_refs_pill_notice(True)
            except Exception:
                traceback.print_exc()

    def _mark_refs_seen_for_ep(self, ep_id: Optional[str]):
        """Очищает unseen-set для эпизода. Зовётся при УХОДЕ с refs view —
        бейджи пропадут при следующем заходе. Безопасно при пустом ep_id."""
        if not ep_id:
            return
        bucket = self._unseen_refs.get(ep_id)
        if bucket:
            bucket.clear()

    def _mark_refs_seen_if_leaving(self):
        """Helper: если юзер сейчас на refs view — очищаем unseen для
        текущего эпизода. Зовётся в `_show_chat_view`, `_display_block`,
        `_select_episode` ПЕРЕД сменой view."""
        try:
            on_refs = (hasattr(self, 'content_stack')
                       and self.content_stack.currentIndex() == 1)
            if on_refs:
                self._mark_refs_seen_for_ep(self._current_episode)
        except Exception:
            pass

    def _display_block(self, name: str):
        # 2026-05-07: уход с refs view — очистить unseen.
        self._mark_refs_seen_if_leaving()
        prompt_file = PROMPTS_DIR / f"{name}.txt"

        # Заголовок блока: «КАМЕРА ЛОРЫ ~8с» — имя из episodes.json (поддержка
        # ОБЕИХ форм: строка-имя ИЛИ объект {name, shots})
        m = re.match(r'(ep\d+)_block_(\d+)', name)
        ep, blk_n = (m.group(1), m.group(2)) if m else (None, None)
        block_meta: Dict = {"name": "", "shots": {}}
        if ep and blk_n:
            block_meta = get_block_meta(self._meta, ep, blk_n)
            # 2026-05-08 редизайн: убран .upper() — заголовок теперь
            # в обычном регистре («Подготовка в коридоре») как в макете LUMZ.
            title_part = block_meta["name"] or f"{tr('block')} {blk_n}"
        else:
            title_part = name
        # 2026-05-08 редизайн: длительность блока в скобках после названия
        # — «Подготовка в коридоре (8с)». Раньше был длинный отступ +
        # тильда «   ~8с», теперь компактно как в макете LUMZ.
        dur = block_total_duration(name)
        dur_part = f" ({dur}с)" if dur else ""
        self.block_title.setText(title_part + dur_part)

        shots: List[Dict] = []
        if prompt_file.exists():
            shots = parse_shots(prompt_file.read_text(encoding="utf-8"))

        # Подмена description на русское (или другое локальное) описание из
        # episodes.json — оно показывается под карточкой шота вместо короткой
        # английской аннотации из промпта.
        shot_descs = block_meta.get("shots", {}) if isinstance(block_meta, dict) else {}
        for s in shots:
            local = shot_descs.get(str(s.get("shot_num", "")))
            if local:
                s["description"] = local

        # 2026-05-08: подмена duration из надёжного JSON-источника
        # (`output/_agent_log_<ep>.json`) — там scriptwriter сохраняет
        # точные `duration_sec`. AI часто забывает аннотации в промптах
        # (`Text annotation below Panel N: "SHOT N / Xс / ..."`), а
        # JSON-стадия всегда есть. Если данных нет — оставляем что
        # parse_shots вытащил (или пусто).
        if ep and blk_n:
            try:
                durations = get_block_shot_durations(ep, int(blk_n))
                if durations:
                    for s in shots:
                        d = durations.get(int(s.get("shot_num", 0)))
                        if d:
                            s["duration"] = f"{d}с"
            except Exception:
                traceback.print_exc()

            # 2026-06-03 (Этап 1/2): реплика под карточкой шота — патчим
            # s["dialog_en"] (+ "dialog_ru" для перевода) из монтажки
            # (первоисточник), симметрично длительностям. Нет монтажки/реплики
            # → ключи не добавятся → ShotCard.set_shot_info спрячет строку.
            try:
                dialogs = get_block_shot_dialogs(ep, int(blk_n))
                if dialogs:
                    for s in shots:
                        dd = dialogs.get(int(s.get("shot_num", 0)))
                        if dd:
                            s["dialog_en"] = dd.get("en", "")
                            if dd.get("ru"):
                                s["dialog_ru"] = dd["ru"]
            except Exception:
                traceback.print_exc()

        # Загружаем по одному 9:16 файлу на каждый шот: {block}_shot{N}.jpg
        panels: List[Optional[bytes]] = [None] * PANELS
        any_exists = False
        for i in range(PANELS):
            p = shot_path(name, i)
            if p.exists():
                try:
                    panels[i] = p.read_bytes()
                    any_exists = True
                except Exception as e:
                    self.status_bar.showMessage(f"Ошибка загрузки {p.name}: {e}")

        settings = QSettings(APP_ORG, APP_NAME)
        for i, card in enumerate(self.shot_cards):
            shot = shots[i] if i < len(shots) else dict(
                shot_num=i+1, duration="", description="", is_blank=True)
            card.set_shot_info(shot)
            card.set_image(panels[i] if i < len(panels) else None)
            # Если этот шот ИМЕННО ЭТОГО блока сейчас регенерируется —
            # оставляем спиннер на карточке. Иначе чистим состояние.
            # Активен если идёт обычная регенерация (_active_regens) ИЛИ хотя бы
            # одна Mode C версия этого шота (3-tuple ключ (block, panel, version)).
            _shot_active = ((name, i) in self._active_regens or any(
                b == name and p == i
                for (b, p, _v) in self._active_mode_c_version_threads))
            if _shot_active:
                card.set_loading(True)
                # redrive=True если шот сейчас в доборе (_shot_retry_pass>0) →
                # подпись «Добор» держится на перерисовках, не мигает.
                card.start_progress(
                    self._shot_gen_started_at.get((name, i)),
                    redrive=self._shot_retry_pass.get((name, i), 0) > 0)
            else:
                card.set_loading(False)
                card.stop_progress()
            # Бейдж NEW и время генерации — оба показываются ТОЛЬКО для
            # непросмотренных шотов. Когда юзер уходит и возвращается,
            # NEW и ⏱ исчезают вместе.
            is_unseen = (name, i) in self._unseen_shots
            card.set_new_badge(is_unseen)
            if is_unseen:
                try:
                    gt = int(settings.value(f"gen_time_{name}_shot{i + 1}", 0) or 0)
                except (TypeError, ValueError):
                    gt = 0
            else:
                gt = 0   # для уже просмотренных шотов время скрываем
            card.set_gen_time(gt)

        # Кнопка экспорта активна если хотя бы один шот сгенерирован
        self.save_btn.setEnabled(any_exists)

        # 2026-05-06 Этап 3: кнопка «🎬 Промпт Seedance». Показываем
        # только если current_block — это блок реального эпизода
        # (имя матчит ep<N>_block_<M>). Лейбл меняем по состоянию файла:
        # готов → 'seedance_btn', нет → 'seedance_btn_pending'.
        # v1.0.85: если pipeline-тред бежит дольше 5 минут и файла всё
        # ещё нет — лейбл становится «🔄 Перезапустить» (клик → restart).
        # Состояние держим в `_seedance_btn_mode` чтобы click handler
        # знал куда роутить.
        if ep and blk_n:
            self.seedance_btn.setVisible(True)
            # v1.0.88 (Stage 16): Block refs button visible вместе с seedance —
            # обе показывают только когда current_block — реальный блок карты.
            if hasattr(self, 'block_refs_btn'):
                # v1.0.88 (Stage 17): setText на каждом _display_block —
                # симметрично seedance_btn. Без этого текст застывает в
                # языке инициализации MainWindow (часто en до QSettings load).
                self.block_refs_btn.setText(tr('block_refs_btn'))
                self.block_refs_btn.setVisible(True)
            seedance_path = SEEDANCE_DIR / f"{name}.txt"
            ready = seedance_path.exists() and seedance_path.stat().st_size > 0
            mode = self._compute_seedance_btn_mode(ready)
            self._seedance_btn_mode = mode
            if mode == 'ready':
                self.seedance_btn.setText(tr('seedance_btn'))
                self.seedance_btn.setToolTip("")
            elif mode == 'restart':
                self.seedance_btn.setText(tr('seedance_btn_restart'))
                self.seedance_btn.setToolTip(tr('seedance_btn_restart_tip'))
            else:  # 'pending'
                self.seedance_btn.setText(tr('seedance_btn_pending'))
                self.seedance_btn.setToolTip("")
            self.seedance_btn.setEnabled(True)
        else:
            self.seedance_btn.setVisible(False)
            self._seedance_btn_mode = 'pending'
            if hasattr(self, 'block_refs_btn'):
                self.block_refs_btn.setVisible(False)
        # 2026-06-11 (стоп-кнопка): видимость по глобальному наличию активных
        # генераций (стоп глобальный), независимо от показанного блока.
        self._refresh_stop_btn()
        if getattr(self, '_current_aspect', None) == "16:9":
            QTimer.singleShot(0, self._recalc_shot_cards_size)

    # ── Regeneration ─────────────────────────────────────────────────────────

    def _on_shot_image_clicked(self, panel_idx: int):
        """2026-05-07: клик по картинке шота → попап ShotViewerDialog
        с большим превью и историей версий. Внутри попапа — кнопки
        edit/regen/use-version. Попап non-modal: можно закрыть и
        прогресс-бар продолжит на карточке грида."""
        if not self.current_block:
            return
        try:
            from widgets import ShotViewerDialog
            active = shot_path(self.current_block, panel_idx)
            history = shot_history_dir(self.current_block, panel_idx)
            # 2026-06-23: один попап шота на всё приложение. Если открыт ЛЮБОЙ
            # ShotViewerDialog — клик по другому шоту НЕ плодит второй попап, а
            # выводит уже открытый на передний план. (_get_open_shot_viewer НЕ
            # трогаем — её per-(block,panel) семантику используют edit/regen/improve.)
            viewers = self._ensure_open_shot_viewers()
            if viewers:
                try:
                    _b0, _p0, dlg0 = viewers[0]
                    dlg0.show()
                    dlg0.raise_()
                    dlg0.activateWindow()
                except Exception:
                    pass
                return
            # 2026-06-13: стиль блока (commit 1 пишет в episodes.json
            # blocks[str(n)]['style']). Читаем СВЕЖИМ с диска — кеш self._meta
            # может отставать, т.к. episode_chat пишет файл напрямую. Дефолт
            # 'sketch' при любой неудаче/старом формате/отсутствии флага →
            # кнопка «Сделать реалистичным» показана как прежде.
            _blk_style = 'sketch'
            try:
                _bm = re.match(r'(ep\d+)_block_(\d+)$', self.current_block)
                if _bm and self._current_episode:
                    _blk_n = _bm.group(2)
                    _fresh_meta = read_episodes_meta(SHOW_ROOT)
                    _braw = (_fresh_meta.get(self._current_episode, {})
                                        .get('blocks', {})
                                        .get(str(_blk_n)))
                    if isinstance(_braw, dict):
                        _blk_style = _braw.get('style', 'sketch') or 'sketch'
            except Exception:
                _blk_style = 'sketch'
            # 2026-06-23: описание шота из карточки того же блока — действие и
            # реплика РАЗДЕЛЬНО (реплика стилизуется как на карточке: #shot-dialog
            # = фиолетовый курсив). Защита: нет карточки/атрибутов → обе пустые →
            # диалог даст старый hint.
            _desc = ""
            _dialog = ""
            try:
                if 0 <= panel_idx < len(self.shot_cards):
                    card = self.shot_cards[panel_idx]
                    _desc = card.desc_label.text().strip()
                    _rep  = getattr(card, "_dlg_en", "").strip()
                    if _rep:
                        _dialog = f'"{_rep}"'
            except Exception:
                _desc = ""
                _dialog = ""
            dlg = ShotViewerDialog(
                panel_idx=panel_idx,
                block_name=self.current_block,
                active_path=active,
                history_dir=history,
                aspect=self._current_aspect,
                style=_blk_style,
                description=_desc,
                dialog=_dialog,
                parent=self)
            # Edit/regen — те же handlers что hover-overlay имели раньше.
            dlg.edit_requested.connect(self._on_edit_shot)
            dlg.regen_requested.connect(self._on_regen)
            dlg.realistic_requested.connect(self._on_make_realistic)
            dlg.version_use_requested.connect(self._on_shot_version_use)
            dlg.crop_committed.connect(self._on_shot_crop_committed)
            # Регистрируем чтобы можно было закрыть/обновить позже.
            self._open_shot_viewers.append((self.current_block, panel_idx, dlg))
            dlg.finished.connect(
                lambda _=None, b=self.current_block, p=panel_idx, d=dlg:
                self._on_shot_viewer_closed(b, p, d))
            dlg.show()
        except Exception:
            import traceback
            traceback.print_exc()

    def _ensure_open_shot_viewers(self):
        """Lazy-инициализация списка открытых попапов."""
        if not hasattr(self, '_open_shot_viewers'):
            self._open_shot_viewers = []
        return self._open_shot_viewers

    def _get_open_shot_viewer(self, block_name: str, panel_idx: int):
        for b, p, dlg in self._ensure_open_shot_viewers():
            if b == block_name and p == panel_idx:
                return dlg
        return None

    def _on_shot_viewer_closed(self, block_name: str, panel_idx: int, dlg):
        """Удаляем попап из реестра когда он закрылся."""
        try:
            self._open_shot_viewers = [
                (b, p, d) for (b, p, d) in self._ensure_open_shot_viewers()
                if not (b == block_name and p == panel_idx and d is dlg)
            ]
        except Exception:
            pass

    def refresh_open_shot_viewer(self, block_name: str, panel_idx: int):
        """Зовётся MW когда регенерация шота завершилась — если попап
        для этого шота открыт, обновляем превью + ленту версий."""
        dlg = self._get_open_shot_viewer(block_name, panel_idx)
        if dlg is not None:
            try:
                dlg.refresh()
            except Exception:
                import traceback
                traceback.print_exc()

    def _on_shot_version_use(self, panel_idx: int, version_n: int):
        """Юзер кликнул «✓ Использовать эту» в попапе. Копируем
        `_history/<basename>/v{N}.jpg` поверх основного файла и помечаем
        active.txt = N. Затем перерисовываем карточку шота и попап."""
        if not self.current_block:
            return
        try:
            history = shot_history_dir(self.current_block, panel_idx)
            src = history / f"v{int(version_n)}.jpg"
            if not src.exists():
                return
            active = shot_path(self.current_block, panel_idx)
            import shutil
            shutil.copy2(str(src), str(active))
            set_active_version(history, int(version_n))
            # Перерисовываем карточку грида.
            try:
                if 0 <= panel_idx < len(self.shot_cards):
                    card = self.shot_cards[panel_idx]
                    card.set_image(active.read_bytes() if active.exists() else None)
            except Exception:
                import traceback
                traceback.print_exc()
            # Обновляем попап (если открыт — у этого панелика он точно
            # открыт, потому что юзер только что в нём кликнул).
            self.refresh_open_shot_viewer(self.current_block, panel_idx)
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_shot_crop_committed(self, panel_idx: int):
        """C2b: попап перезаписал просматриваемую версию кропом (зум при
        закрытии). Диск уже записан в диалоге — здесь перерисовываем карточку
        грида из обновлённого активного файла."""
        if not self.current_block:
            return
        try:
            active = shot_path(self.current_block, panel_idx)
            if 0 <= panel_idx < len(self.shot_cards):
                self.shot_cards[panel_idx].set_image(
                    active.read_bytes() if active.exists() else None)
        except Exception:
            traceback.print_exc()

    def _on_copy_shot(self, panel_idx: int):
        """2026-06-02: «Копировать» в углу карточки. Кладём БАЙТЫ активной
        картинки шота в буфер MainWindow (снимок), включаем «Вставить» у всех
        карточек. Копируется только активная картинка."""
        if not self.current_block:
            return
        src = shot_path(self.current_block, panel_idx)
        if not src.exists():
            self.status_bar.showMessage(tr('status_no_shots'))
            return
        try:
            self._set_shot_clipboard_bytes(src.read_bytes())
        except Exception:
            traceback.print_exc()
            return
        self.status_bar.showMessage(tr('status_shot_copied', n=panel_idx + 1))

    def _set_shot_clipboard_bytes(self, data: bytes):
        """Единый буфер картинки шота для Editor ↔ Camera Lab."""
        if not data:
            return
        self._shot_clipboard = bytes(data)
        try:
            for c in getattr(self, 'shot_cards', []):
                c.set_paste_available(True)
        except Exception:
            traceback.print_exc()
        try:
            camera_view = getattr(self, 'camera_lab_view', None)
            if camera_view is not None:
                camera_view.set_shot_clipboard_available(True)
        except Exception:
            traceback.print_exc()

    def _on_paste_shot(self, panel_idx: int):
        """2026-06-02: «Вставить» в углу карточки. Картинка из буфера
        добавляется НОВОЙ версией в историю шота-назначения и становится
        активной (старые версии сохраняются). Источник мог быть в другом
        блоке — буфер хранит байты, привязки к блоку нет."""
        if not self.current_block:
            return
        if not self._shot_clipboard:
            self.status_bar.showMessage(tr('status_clipboard_empty'))
            return
        try:
            add_shot_version_from_bytes(
                self.current_block, panel_idx, self._shot_clipboard)
        except Exception:
            traceback.print_exc()
            self.status_bar.showMessage(tr('status_clipboard_empty'))
            return
        # Перерисовываем карточку грида новой активной картинкой.
        try:
            active = shot_path(self.current_block, panel_idx)
            if 0 <= panel_idx < len(self.shot_cards):
                self.shot_cards[panel_idx].set_image(
                    active.read_bytes() if active.exists() else None)
        except Exception:
            traceback.print_exc()
        # Индикатор блока (шот стал «готовым») + попап если открыт.
        self._refresh_block_indicator(self.current_block)
        self.refresh_open_shot_viewer(self.current_block, panel_idx)
        self.status_bar.showMessage(tr('status_shot_pasted', n=panel_idx + 1))

    def _on_translate_shot(self, panel_idx: int, en_text: str):
        """2026-06-03 (Этап 2): перевод реплик эпизода на украинский через
        Haiku ОДНИМ батчем + персист (output/_translations_uk_<ep>.json).
        Клик переводит не одну реплику, а ВСЕ непереведённые реплики эпизода —
        старт CLI (~8с) платится один раз. Кэш {en→uk} (память + файл) →
        повторные клики мгновенные. Гард _uk_tr_busy_eps от дубля батча."""
        en = (en_text or "").strip()
        ep = self._current_episode
        if not en or not ep:
            return
        card = (self.shot_cards[panel_idx]
                if 0 <= panel_idx < len(self.shot_cards) else None)
        if card is None:
            return
        # Ленивая подгрузка файла эпизода в кэш (поверх памяти, не затирая).
        if ep not in self._uk_tr_loaded_eps:
            self._uk_tr_cache.update(load_uk_translations(ep))
            self._uk_tr_loaded_eps.add(ep)
        # Кэш-хит → мгновенно, без модели.
        cached = self._uk_tr_cache.get(en)
        if cached:
            card.set_translation_result(cached)
            return
        # Гард: батч по эпизоду уже идёт — оставляем «Перекладаю…», карточка
        # возьмёт перевод из кэша по готовности первого батча (_apply_uk_batch_result).
        if ep in self._uk_tr_busy_eps:
            return
        cli = find_claude_cli()
        if not cli:
            card.set_translation_result(tr('translate_error'))
            return
        pending = [e for e in collect_episode_dialogs_en(ep)
                   if e not in self._uk_tr_cache]
        if not pending:
            card.set_translation_result(tr('translate_error'))
            return
        try:
            from threads.translate import TranslateThread
        except Exception:
            traceback.print_exc()
            card.set_translation_result(tr('translate_error'))
            return
        th = TranslateThread(self._project_root, cli, pending, "uk",
                             model="claude-haiku-4-5")
        self._translate_threads.append(th)
        self._uk_tr_busy_eps.add(ep)

        def _done(mapping: dict):
            self._uk_tr_busy_eps.discard(ep)
            try:
                if mapping:
                    self._uk_tr_cache.update(mapping)
                    # Файл = только реплики ЭТОГО эпизода, что есть в кэше.
                    ep_en = collect_episode_dialogs_en(ep)
                    file_map = {e: self._uk_tr_cache[e] for e in ep_en
                                if e in self._uk_tr_cache}
                    save_uk_translations(ep, file_map)
            except Exception:
                traceback.print_exc()
            # Обновить открытые поповеры из кэша (ожидавшая карточка покажет перевод).
            self._apply_uk_batch_result()
            # Кликнутую реплику модель могла пропустить → явная ошибка.
            if en not in self._uk_tr_cache:
                card.set_translation_result(tr('translate_error'))
            try:
                self._translate_threads.remove(th)
            except ValueError:
                pass

        def _err(_msg: str):
            self._uk_tr_busy_eps.discard(ep)
            card.set_translation_result(tr('translate_error'))
            try:
                self._translate_threads.remove(th)
            except ValueError:
                pass

        th.result_ready.connect(_done)
        th.failed.connect(_err)
        th.start()

    def _apply_uk_batch_result(self):
        """После батча обновить ОТКРЫТЫЕ поповеры перевода на карточках из
        кэша. set_translation_result сам no-op'ит если поповер скрыт, поэтому
        безопасно пройтись по всем; обновятся только видимые с готовым uk
        (ru/en-поповеры не трогаем — для них en→uk в кэше нет)."""
        for c in self.shot_cards:
            try:
                en = (getattr(c, "_dlg_en", "") or "").strip()
                uk = self._uk_tr_cache.get(en)
                if uk:
                    c.set_translation_result(uk)
            except Exception:
                pass

    def _on_edit_shot(self, panel_idx: int, parent_version: int = 0,
                      block_name: Optional[str] = None):
        """2026-05-06: Поведение полностью перепроектировано.

        Раньше: попап с инструкцией → NARWHAL edit-режим (правит
        существующую картинку). Требовал чтобы файл шота существовал.

        Теперь: попап с ПОЛНЫМ ПРОМПТОМ шота (тело Panel N из .txt
        файла блока) — юзер видит точный текст который пошёл бы в
        NARWHAL и может его править. После сохранения Studio:
          1. Записывает новый текст тела Panel N обратно в .txt блока
             (заменяя старое тело).
          2. Запускает обычный regen — GenerateThread прочитает .txt,
             extract_shot_prompt вернёт уже новый текст.

        Работает И на сгенерированных шотах, И на упавших (где файла
        картинки нет) — потому что промпт читается ИЗ .txt а не из
        изображения.
        """
        # 2026-06-19 (Вариант 2, edit): блок ИЗ СИГНАЛА (block_name) — попап
        # ShotViewerDialog не-модальный, current_block мог занулиться/смениться
        # к моменту клика «Редактировать» (был «немой» ранний return → edit не
        # запускался). Fallback на current_block — для карточки грида (она
        # эмитит edit_requested без block_name).
        target_block = block_name or self.current_block
        if not target_block:
            self._log_shot_regen(
                f"skip-no-block action=edit panel={panel_idx} "
                f"block_name={block_name!r} current_block={self.current_block!r}")
            return
        key = (target_block, panel_idx)
        if key in self._active_regens:
            self.status_bar.showMessage(tr('status_already_genning', n=panel_idx + 1))
            return

        # Читаем .txt блока чтобы вытащить текущее тело Panel N
        prompt_file = PROMPTS_DIR / f"{target_block}.txt"
        if not prompt_file.exists():
            QMessageBox.information(
                self, "Промпт не найден",
                f"Файл {prompt_file.name} ещё не сгенерирован. "
                "Дождись когда PromptWriter допишет промпт блока.")
            return
        try:
            prompt_text = prompt_file.read_text(encoding='utf-8')
        except Exception as e:
            QMessageBox.warning(self, "Ошибка чтения",
                                  f"Не смог прочитать {prompt_file.name}: {e}")
            return

        # Извлекаем ТЕКУЩЕЕ тело Panel N (без хедера блока) для
        # стартового текста в попапе.
        current_body = _extract_panel_body(prompt_text, panel_idx) or ""
        if not current_body.strip():
            QMessageBox.information(
                self, "Шот пустой",
                f"Panel {panel_idx + 1} помечен как пустой (BLANK) "
                "или не найден в промпте блока. Редактировать нечего.")
            return

        # 2026-06-23: если правим конкретную версию (parent_version>0) и у неё
        # есть сохранённый AI-edit сайдкар edit_v{N}.json — достаём прошлую
        # правку, чтобы подставить в нижнее поле диалога. Нет файла/битый →
        # пустая строка (поведение как раньше).
        initial_short = ""
        if parent_version and parent_version > 0:
            _edit_sc = (shot_history_dir(target_block, panel_idx)
                        / f"edit_v{int(parent_version)}.json")
            if _edit_sc.exists():
                try:
                    import json as _json
                    _ed = _json.loads(_edit_sc.read_text(encoding='utf-8')) or {}
                    initial_short = str(_ed.get("ai_edit", "") or "")
                except Exception:
                    initial_short = ""

        # Попап с pre-filled промптом + второе поле «короткая инструкция AI»
        result = self._ask_edit_full_prompt(panel_idx, current_body, target_block,
                                            initial_short=initial_short)
        if not result:
            return  # юзер нажал Отмена / закрыл окно
        new_body, short_instruction = result

        # 2026-05-07: ДВА РЕЖИМА.
        # 1) Если заполнено поле «короткая инструкция AI» — запускаем
        #    edit-режим NARWHAL (existing картинка как реф + инструкция).
        #    .txt блока НЕ трогаем. Требуется существующий файл картинки.
        # 2) Иначе — старое поведение: replace panel body в .txt
        #    (если изменилось) + обычный regen из .txt.
        if short_instruction:
            shot_file = shot_path(target_block, panel_idx)
            if not shot_file.exists():
                QMessageBox.information(
                    self, "Картинка ещё не сгенерирована",
                    f"AI-edit режим работает только над уже сгенерированным "
                    f"шотом (он берёт текущую картинку как основу). Файла "
                    f"{shot_file.name} ещё нет — сначала сделай обычную "
                    f"генерацию (можно из этого же попапа, оставив поле "
                    f"«короткая инструкция» пустым).")
                return
            # Запускаем GenerateThread в edit-режиме.
            _now = time.time()
            self._shot_gen_started_at[(target_block, panel_idx)] = _now
            # Карточку грида трогаем ТОЛЬКО если открыт именно target_block
            # (он мог прийти из сигнала и отличаться от current_block).
            if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
                card = self.shot_cards[panel_idx]
                card.set_loading(True)
                card.start_progress(_now)
            # 2026-06-07 (фича маркера, Шаг C): если открыт ShotViewerDialog
            # и юзер нарисовал маркером поверх шота — запекаем штрихи в
            # temp-картинку и отдаём её как базу [@]img0 для Nano Banana
            # (модель поймёт какой объект тронуть). Нет диалога / нет
            # штрихов → marked=None → путь байт-в-байт прежний.
            # 2026-06-14 (фикс слёта маркера): предпочитаем ПРЕД-запечённый
            # отпечаток (заготовлен в _on_edit_clicked ДО активации, пока штрихи
            # живы); fallback на bake для edit НЕ из btn_edit (grid-карточка).
            sv = self._get_open_shot_viewer(target_block, panel_idx)
            marked = ((sv.take_pending_marked() or sv._bake_marked_image())
                      if sv is not None else None)
            # 2026-06-14 (маркер виден при Edit): активация выбранной версии
            # УБРАНА из _on_edit_clicked → база больше НЕ «активный файл шота».
            # marked (temp с меткой) → база. Если маркера нет, а правка из попапа
            # версий (parent_version>0) — база = ВЫБРАННАЯ версия v{parent}.jpg
            # (то, что юзер видел/правил), НЕ активная. parent_version=0 (grid-
            # карточка без viewer) → base_override=None → shot_path как раньше.
            base_override = marked
            if base_override is None and parent_version and parent_version > 0:
                _sel_img = (shot_history_dir(target_block, panel_idx)
                            / f"v{int(parent_version)}.jpg")
                if _sel_img.exists():
                    base_override = _sel_img
            # 2026-07-02: сохраняем AI-edit промт СРАЗУ, ДО старта треда —
            # чтобы обрыв/ошибка генерации не потеряли введённый текст. При
            # успехе generate.py:1127 перезапишет тем же ключом (идемпотентно).
            # Привязка к parent_version (существующей версии); parent==0 (edit
            # с грида) — ключа нет, пропускаем. Сбой записи не критичен.
            if parent_version and parent_version > 0:
                try:
                    (shot_history_dir(target_block, panel_idx)
                     / f"edit_v{int(parent_version)}.json").write_text(
                        json.dumps({"ai_edit": short_instruction},
                                   ensure_ascii=False),
                        encoding='utf-8')
                except Exception:
                    pass
            thread = GenerateThread(target_block, panel_idx,
                                     edit_instruction=short_instruction,
                                     base_image_override=base_override,
                                     parent_version=parent_version,
                                     aspect=self._current_aspect)
            self._active_regens[key] = thread
            thread.progress.connect(self.status_bar.showMessage)
            thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
            thread.step.connect(
                lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
            thread.finished.connect(
                lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
            thread.error.connect(
                lambda msg: self._on_regen_error(msg, target_block, panel_idx))
            # temp-картинка маркера живёт только на время аплоада в треде;
            # после finished/error удаляем её (на обоих сигналах). 2026-06-14:
            # гард `if marked is not None` КРИТИЧЕН — чистим ТОЛЬКО temp-отпечаток,
            # НЕ постоянный v{parent}.jpg (base_override), который мимо cleanup.
            if marked is not None:
                def _cleanup_marked(*_a, _p=marked):
                    try:
                        _p.unlink(missing_ok=True)
                    except Exception:
                        pass
                thread.finished.connect(_cleanup_marked)
                thread.error.connect(_cleanup_marked)
            thread.start()
            # 2026-05-17: закрываем родительский ShotViewerDialog (если был
            # открыт) — юзер не должен вручную крестить попап после
            # «Сохранить и регенерировать». sv уже получен выше — переиспользуем.
            if sv is not None:
                try:
                    sv.close()
                except Exception:
                    pass
            self._refresh_block_indicator(target_block)
            self.status_bar.showMessage(
                f"AI-edit SHOT {panel_idx + 1}: «{short_instruction[:60]}»…")
            return

        # Режим 2: правка тела Panel-описания.
        if not new_body:
            return  # большое поле очищено и инструкции нет — отказ
        # Если текст изменился — записываем новое тело обратно в .txt блока.
        # Если не изменился — просто запускаем regen с уже-валидным файлом
        # (юзер мог открыть попап чтобы просто повторить генерацию).
        if new_body.strip() != current_body.strip():
            try:
                new_text = _replace_panel_body(prompt_text, panel_idx, new_body.strip())
                prompt_file.write_text(new_text, encoding='utf-8')
            except Exception as e:
                QMessageBox.warning(self, "Ошибка записи",
                                      f"Не смог сохранить новый промпт: {e}")
                return

        # Запускаем обычный regen — GenerateThread прочитает обновлённый .txt
        _now = time.time()
        self._shot_gen_started_at[(target_block, panel_idx)] = _now
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            card = self.shot_cards[panel_idx]
            card.set_loading(True)
            card.start_progress(_now)
        thread = GenerateThread(target_block, panel_idx,
                                parent_version=parent_version,
                                aspect=self._current_aspect)
        self._active_regens[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
        thread.step.connect(
            lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
        thread.finished.connect(
            lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        # 2026-05-17: закрываем родительский ShotViewerDialog (если был
        # открыт) — симметрично с AI-edit веткой выше.
        sv = self._get_open_shot_viewer(target_block, panel_idx)
        if sv is not None:
            try:
                sv.close()
            except Exception:
                pass
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(
            f"Перегенерирую SHOT {panel_idx + 1} с правленым промптом…")

    def _ask_edit_full_prompt(self, panel_idx: int,
                                  current_body: str,
                                  target_block: str,
                                  initial_short: str = ""
                                  ) -> Optional[tuple]:
        """Попап правки SHOT — два режима в одном окне (2026-05-07).

        Возвращает кортеж `(new_body, short_instruction)` или None если
        юзер закрыл окно/нажал Отмена.
          • new_body: str — отредактированное тело Panel-описания
            (если юзер менял большое поле; пустое → не менять).
          • short_instruction: str — короткая правка для AI-edit режима
            (если юзер заполнил нижнее поле; пустое → не использовать).
        Если short_instruction непустой — caller запустит edit-режим
        `GenerateThread(... edit_instruction=...)` который изменит
        ТОЛЬКО заданное на текущей картинке, не трогая остальное.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Правка промпта SHOT {panel_idx + 1}")
        dlg.setMinimumSize(720, 620)
        dlg.setStyleSheet(theme_qss("""
            QDialog { background:#121313; color:#ffffff; }
            QLabel { color:#ddd; background:transparent; }
            QDialogButtonBox QPushButton {
                background:#242628;
                color:#f2f3f0;
                border:1px solid rgba(255,255,255,0.10);
                border-radius:8px;
                padding:8px 14px;
                font-weight:600;
            }
            QDialogButtonBox QPushButton:hover {
                background:#2c2f31;
                border-color:rgba(255,255,255,0.16);
            }
        """))
        v = QVBoxLayout(dlg)
        v.setSpacing(10)
        v.setContentsMargins(20, 16, 20, 16)

        title = QLabel(f"Промпт для SHOT {panel_idx + 1} "
                        "(текст который пойдёт в Nano Banana 2)")
        title.setStyleSheet("color:#ddd; font-size:14px; font-weight:600;")
        v.addWidget(title)

        hint = QLabel(
            "Это тело Panel-описания. Можешь править — например "
            "переформулировать сцену под content-фильтр (убрать "
            "«aiming at face» → «raising barrel»), сменить ракурс, "
            "уточнить позиционирование. После сохранения SHOT "
            "сразу регенерируется с новым текстом.")
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        text = QPlainTextEdit()
        text.setPlainText(current_body)
        text.setStyleSheet(theme_qss(
            "QPlainTextEdit { background:#191b1d; "
            "border:1px solid rgba(255,255,255,0.055); "
            "border-radius:8px; color:#a8a8a8; padding:10px; "
            "font-size:12px; font-family: 'Menlo','Consolas',monospace; }"))
        v.addWidget(text, stretch=1)

        # 2026-05-07: разделитель + второе поле «короткая инструкция AI».
        sep = QLabel("──────  ИЛИ  ──────")
        sep.setStyleSheet("color:#cfff24; font-size:11px; font-weight:600;")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sep)

        short_title = QLabel(
            "Короткая правка существующей картинки (AI-edit режим)")
        short_title.setStyleSheet(
            "color:#d8d8d8; font-size:13px; font-weight:600;")
        v.addWidget(short_title)

        short_field = QPlainTextEdit()
        short_field.setPlaceholderText(
            "убери ружьё, остальное оставь как есть… можно коротко "
            "по-русски и нажать ✨ Улучшить — AI перепишет в точный промпт")
        # 2026-06-23: предзаполняем нижнее поле сохранённой AI-edit правкой
        # версии (если есть). Пусто → остаётся placeholder, как раньше.
        if initial_short:
            short_field.setPlainText(initial_short)
        short_field.setMinimumHeight(150)
        short_field.setStyleSheet(theme_qss(
            "QPlainTextEdit { background:#191b1d; "
            "border:1px solid rgba(255,255,255,0.055); "
            "border-radius:8px; color:#fff; padding:8px; font-size:12px; }"))
        v.addWidget(short_field)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить и регенерировать")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if ok_btn is not None:
            ok_btn.setFixedSize(250, 40)
        if cancel_btn is not None:
            cancel_btn.setFixedSize(150, 40)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        # 2026-06-08: «✨ Улучшить» — зрячий Sonnet 4.6 переписывает короткую
        # русскую правку из short_field в командный английский промпт для Nano
        # Banana, ГЛЯДЯ на картинку текущей версии шота. Если открыт
        # ShotViewerDialog со штрихами маркера — Sonnet видит ту же размеченную
        # картинку, что увидит Nano Banana (целится в обведённый объект).
        improve_row = QHBoxLayout()
        improve_btn = QPushButton(tr('improve_btn'))
        improve_btn.setObjectName("improve-compact")
        # лево-выравнивание текста: при анимации точек слово «Улучшаю промпт»
        # стоит у левого края, точки растут вправо (не ездит). Отдельный
        # objectName нужен, чтобы кнопка не наследовала глобальный #save и не
        # раздувалась вместе с другими action-кнопками.
        improve_btn.setStyleSheet(theme_qss("""
            QPushButton#improve-compact {
                background:#242628;
                border:1px solid #242628;
                border-radius:8px;
                color:#f2f3f0;
                font-size:13px;
                font-weight:600;
                min-width:132px;
                max-width:132px;
                min-height:40px;
                max-height:40px;
                padding:0 12px;
                text-align:left;
            }
            QPushButton#improve-compact:hover {
                background:#2c2f31;
                border-color:#2c2f31;
                color:#ffffff;
            }
            QPushButton#improve-compact:disabled {
                background:#1f2123;
                border-color:#1f2123;
                color:rgba(255,255,255,0.42);
            }
        """))
        improve_btn.setFixedSize(132, 40)
        improve_btn.setMinimumSize(132, 40)
        improve_btn.setMaximumSize(132, 40)
        improve_btn.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        improve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        improve_btn.setEnabled(bool(short_field.toPlainText().strip()))
        improve_row.addWidget(improve_btn)
        improve_row.addStretch()
        v.addLayout(improve_row)
        v.addWidget(btns)

        # guard времени жизни модалки: поздний результат фонового потока не
        # должен трогать удалённые виджеты после закрытия диалога.
        _improve_state = {'alive': True, 'dots': 0}
        dlg.finished.connect(lambda *_: _improve_state.update(alive=False))

        # анимация мигающих точек на «✨ Улучшаю промпт» пока крутится вызов.
        # Таймер parented к dlg (умрёт с диалогом); тик защищён alive-guard'ом
        # и try/except — не трогает удалённый виджет после закрытия.
        _improve_timer = QTimer(dlg)
        _improve_timer.setInterval(500)
        _improve_base = tr('improve_running').rstrip('.… ')

        def _tick_dots():
            if not _improve_state['alive']:
                _improve_timer.stop()
                return
            _improve_state['dots'] = (_improve_state['dots'] % 3) + 1
            try:
                improve_btn.setText(_improve_base + '.' * _improve_state['dots'])
            except RuntimeError:
                _improve_timer.stop()
        _improve_timer.timeout.connect(_tick_dots)
        dlg.finished.connect(lambda *_: _improve_timer.stop())

        def _toggle_improve():
            improve_btn.setEnabled(bool(short_field.toPlainText().strip()))
        short_field.textChanged.connect(_toggle_improve)

        def _do_improve():
            txt = short_field.toPlainText().strip()
            if not txt:
                return
            cli = find_claude_cli()
            if not cli:
                self.status_bar.showMessage(
                    tr('improve_error', msg='claude CLI not found'))
                return
            # marked-or-clean: размеченная картинка если открыт viewer со
            # штрихами, иначе чистый активный шот (та же база, что и edit).
            # 2026-06-14: предпочесть ПРЕД-отпечаток (с маркером, заготовлен в
            # _on_edit_clicked ДО активации). peek — НЕ one-shot: отпечаток
            # остаётся для финальной edit-генерации. _own_marked=True только если
            # бейкали САМИ — тогда и чистим (чужой pending edit'а не трогаем).
            sv = self._get_open_shot_viewer(target_block, panel_idx)
            _peeked = sv.peek_pending_marked() if sv is not None else None
            if _peeked is not None:
                marked = _peeked
                _own_marked = False
            else:
                marked = sv._bake_marked_image() if sv is not None else None
                _own_marked = True
            img = marked or shot_path(target_block, panel_idx)
            ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
            improve_btn.setEnabled(False)
            _improve_state['dots'] = 1
            improve_btn.setText(_improve_base + '.')
            _improve_timer.start()
            short_field.setReadOnly(True)
            if ok_btn is not None:
                ok_btn.setEnabled(False)
            th = ImprovePromptThread(txt, img, self._project_root, cli)
            if not hasattr(self, '_improve_threads'):
                self._improve_threads = []
            self._improve_threads.append(th)

            def _restore():
                try:
                    _improve_timer.stop()
                    improve_btn.setText(tr('improve_btn'))
                    short_field.setReadOnly(False)
                    improve_btn.setEnabled(
                        bool(short_field.toPlainText().strip()))
                    if ok_btn is not None:
                        ok_btn.setEnabled(True)
                except RuntimeError:
                    pass

            def _on_improve_ok(res):
                if not _improve_state['alive']:
                    return
                try:
                    short_field.setPlainText(res)
                except RuntimeError:
                    pass
                _restore()

            def _on_improve_err(msg):
                if not _improve_state['alive']:
                    return
                self.status_bar.showMessage(tr('improve_error', msg=msg))
                _restore()

            def _cleanup_marked():
                # temp-картинка маркера живёт только на время вызова Sonnet —
                # чистим по завершении потока. 2026-06-14: ТОЛЬКО если это НАШ
                # бейк (_own_marked). Если marked — чужой пред-отпечаток edit'а
                # (из peek) — НЕ удаляем: им владеет take_pending_marked (edit) /
                # _clear_pending_marked (закрытие попапа).
                if marked is not None and _own_marked:
                    try:
                        Path(marked).unlink(missing_ok=True)
                    except Exception:
                        pass

            th.result_ready.connect(_on_improve_ok)
            th.error.connect(_on_improve_err)
            th.finished.connect(_cleanup_marked)

            def _detach(*_):
                # на закрытие модалки отцепляем виджет-апдейтеры (поздний сигнал
                # игнор); _cleanup_marked на th.finished остаётся.
                for sig in (th.result_ready, th.error):
                    try:
                        sig.disconnect()
                    except (TypeError, RuntimeError):
                        pass
            dlg.finished.connect(_detach)
            th.start()

        improve_btn.clicked.connect(_do_improve)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        new_text = text.toPlainText().strip()
        short_instr = short_field.toPlainText().strip()
        return (new_text, short_instr)

    def _ask_edit_instruction(self, panel_idx: int) -> Optional[str]:
        """Маленький модальный попап: текстовое поле + Отправить/Отмена.
        Возвращает текст инструкции или None при отмене/пустом вводе."""
        dlg = QDialog(self)
        dlg.setWindowTitle(tr('edit_dialog_title', n=panel_idx + 1))
        dlg.setFixedSize(440, 230)
        v = QVBoxLayout(dlg)
        v.setSpacing(12)
        v.setContentsMargins(20, 18, 20, 16)

        title = QLabel(tr('edit_dialog_q'))
        title.setStyleSheet("color:#ddd; font-size:14px; font-weight:500;")
        v.addWidget(title)

        hint = QLabel(tr('edit_dialog_hint'))
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        text = QPlainTextEdit()
        text.setPlaceholderText(tr('edit_dialog_placeholder'))
        text.setStyleSheet(
            "QPlainTextEdit { background:#191b1d; border:1px solid #1d1e20; "
            "border-radius:6px; color:#ddd; padding:8px; font-size:13px; }")
        v.addWidget(text, stretch=1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText(tr('edit_dialog_send'))
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('edit_dialog_cancel'))
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        instr = text.toPlainText().strip()
        return instr or None

    # ── Refs view handlers ──────────────────────────────────────────────────

    def _open_fullscreen(self, image_path: Path):
        """Открывает полноэкранный просмотр референсной картинки.
        Закрытие: клик в любое место или Esc.

        ⚠ Любое исключение здесь сообщаем юзеру через статус-бар,
        но НЕ даём ему улететь обратно в Qt event-loop — иначе
        PyQt6 переводит его в qFatal()→abort() и валит всё приложение."""
        try:
            dlg = FullscreenImageDialog(image_path, parent=self)
            dlg.exec()
        except Exception as ex:
            import traceback; traceback.print_exc()
            try:
                self.status_bar.showMessage(f"Не удалось открыть просмотр: {ex}", 6000)
            except Exception:
                pass

    def _find_ref_card(self, image_path: Path) -> Optional['RefCard']:
        """Находит RefCard для данного пути (для обновления state)."""
        if not hasattr(self, 'refs_container'):
            return None
        for card in self.refs_container.findChildren(RefCard):
            try:
                # Сравниваем по resolve чтобы не зависеть от форм пути
                if card._kind in ('location', 'object'):
                    info = card.findChild(QLabel, "ref-name")
                    if info and (Path(image_path).resolve() ==
                                 self._ref_paths_by_card.get(id(card))):
                        return card
            except Exception:
                pass
        return None

    def _start_ref_thread(self, image_path: Path, mode: str,
                          instruction: Optional[str] = None,
                          reference_path: Optional[Path] = None):
        """Запускает RefGenerateThread (regen или edit) с UI-callback'ами.
        На карточке сразу показывается прогресс-бар (set_loading)."""
        if not hasattr(self, '_ref_threads'):
            self._ref_threads: List[QThread] = []

        thread = RefGenerateThread(image_path, mode, instruction, reference_path)
        # 2026-05-07: помечаем тред эпизодом в котором его запустили —
        # для per-episode фильтрации в `_refs_busy(ep_id)`. Без этого
        # точки на пилюле «Референсы» бежали на всех эпизодах сразу.
        try:
            thread._ep_id = getattr(self, '_current_episode', None)
        except Exception:
            pass
        self._ref_threads.append(thread)

        ref_name = image_path.stem

        # Находим RefCard по пути — для показа прогресса прямо на карточке
        target_card: Optional[RefCard] = None
        if hasattr(self, 'refs_container'):
            try:
                wanted = image_path.resolve()
            except Exception:
                wanted = image_path
            for card in self.refs_container.findChildren(RefCard):
                try:
                    if card._image_path.resolve() == wanted:
                        target_card = card
                        break
                except Exception:
                    pass

        # 2026-05-07: показываем busy_overlay с надписью «Генерирую
        # изображение» (location) или «Обновляю картинку» (object) —
        # юзер сразу видит что фаза image gen идёт. Раньше был только
        # тонкий progress-bar 5px внизу, юзер не понимал что происходит.
        # Для location фазы 1 (image) → 2 (geometry) — overlay не моргает,
        # set_image_updating(False) не скроет его если уже включится geom.
        kind_for_label = self._ref_kind_from_path(image_path)
        image_label_key = ('ref_busy_image_object'
                           if kind_for_label == 'object'
                           else 'ref_busy_image_location')
        # 2026-05-07: регистрируем путь в `_active_image_paths` —
        # `_build_ref_card` навесит overlay на новую карточку если
        # refs view пересоберётся во время image gen (например watcher
        # debounce от записи файла, или manual rebuild).
        # `started_at` сохраняется чтобы счётчик секунд НЕ сбрасывался
        # при пересборе карточки.
        try:
            import time as _time
            self._active_image_paths[image_path.resolve()] = {
                'ep_id': getattr(self, '_current_episode', None),
                'label_key': image_label_key,
                'started_at': _time.time(),
            }
        except Exception:
            traceback.print_exc()
        if target_card is not None:
            target_card.set_loading(True)
            try:
                target_card.set_image_updating(True, image_label_key)
            except Exception:
                traceback.print_exc()

        if mode == 'regen':
            self.status_bar.showMessage(tr('ref_status_regen', name=ref_name))
        else:
            self.status_bar.showMessage(tr('ref_status_edit', name=ref_name))

        thread.progress.connect(self.status_bar.showMessage)
        thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin

        def _on_step(lbl: str, pct: int):
            try:
                self.status_bar.showMessage(f"{lbl} ({pct}%)")
                if target_card is not None:
                    try:
                        target_card.set_progress(lbl, pct)
                    except Exception:
                        pass
            except Exception:
                import traceback
                traceback.print_exc()
        thread.step.connect(_on_step)

        # 2026-05-07: захватываем ep_id в замыкании чтобы передать в
        # `_on_ref_done` (он стартует ClaudeGeometryThread и кладёт path
        # в `_active_geometry_paths` — нужно знать чей это эпизод).
        _start_ep = getattr(self, '_current_episode', None)
        def _safe_done(_elapsed, _ep=_start_ep):
            try:
                self._on_ref_done(image_path, mode, source_ep_id=_ep)
            except Exception:
                import traceback
                traceback.print_exc()
        thread.finished.connect(_safe_done)

        def _safe_err(msg):
            try:
                self._on_ref_error(image_path, msg)
            except Exception:
                import traceback
                traceback.print_exc()
        thread.error.connect(_safe_err)
        thread.start()
        self._refresh_refs_pill_text()

    def _on_ref_regen(self, image_path: Path, kind: str):
        """Кнопка «Перегенерировать» на refs-карточке (location/object).
        Читает сохранённый промпт → FastGen → перезапись файла.

        2026-05-07:
          • Перед запуском показывает confirmation-попап «будет
            перегенерировано, заменит текущее» — юзер может отменить.
          • Для object'а ищем prompt-файл с fallback на LOCATIONS_DIR
            (pipeline.py исторически писал object-промпты туда — баг,
            см. _session_log). Если найден в legacy-месте — копируем
            в правильную папку чтобы regen больше не падал.
        """
        # 1) Найти prompt с fallback'ами для объектов.
        pf = ref_prompt_path(image_path)
        if not pf.exists():
            recovered = False
            if kind == 'object':
                # Fallback'ы: prompt мог осесть в legacy-местах.
                # 1) refs/locations/<stem>_prompt.txt — баг pipeline.py.
                # 2) refs/objects/<stem-без-цифр>_prompt.txt — варианты
                #    типа `shotgun1.jpg` / `shotgun2.jpg` без своих
                #    промптов используют base-промпт `shotgun_prompt.txt`.
                # 3) refs/locations/<stem-без-цифр>_prompt.txt — комбо.
                stem = image_path.stem
                base_stem = re.sub(r'\d+$', '', stem) or stem
                candidates = [
                    LOCATIONS_DIR / f"{stem}_prompt.txt",
                    image_path.parent / f"{base_stem}_prompt.txt",
                    LOCATIONS_DIR / f"{base_stem}_prompt.txt",
                ]
                for legacy_pf in candidates:
                    try:
                        if legacy_pf.exists() and legacy_pf != pf:
                            pf.write_text(
                                legacy_pf.read_text(encoding='utf-8'),
                                encoding='utf-8')
                            recovered = True
                            break
                    except Exception:
                        traceback.print_exc()
            if not recovered:
                QMessageBox.information(
                    self, tr('ref_no_prompt_title'),
                    tr('ref_no_prompt_msg_kind' if kind == 'object'
                       else 'ref_no_prompt_msg',
                       name=image_path.stem))
                return
        # 2) Confirmation popup — юзер должен подтвердить замену.
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            if kind == 'object':
                box.setWindowTitle(tr('ref_regen_confirm_title_object'))
                box.setText(tr('ref_regen_confirm_msg_object',
                               name=image_path.stem))
            else:
                box.setWindowTitle(tr('ref_regen_confirm_title_location'))
                box.setText(tr('ref_regen_confirm_msg_location',
                               name=image_path.stem))
            yes_btn = box.addButton(
                tr('ref_regen_confirm_yes'), QMessageBox.ButtonRole.AcceptRole)
            no_btn = box.addButton(
                tr('ref_regen_confirm_no'), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no_btn)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
        except Exception:
            traceback.print_exc()
            return
        # 3) Поехали.
        self._start_ref_thread(image_path, 'regen')

    def _on_ref_edit(self, image_path: Path, kind: str):
        """Кнопка «Изменить» на refs-карточке (location/object).
        Открывает RefEditDialog (один слот картинки + опц. замена картинки на
        другой реф эпизода того же типа) → FastGen → перезапись файла рефа.
        2026-06-17 (Коммит C): заменил старый _ask_ref_edit_instruction-попап."""
        from widgets.ref_edit_dialog import RefEditDialog
        from PyQt6.QtWidgets import QDialog

        ep_id = self._current_episode
        if not ep_id:
            return
        refs_map = list_episode_refs(ep_id)
        if kind == 'location':
            episode_refs = list(refs_map.get('locations', []))
        elif kind == 'object':
            episode_refs = list(refs_map.get('objects', []))
        else:
            # character / неизвестный тип — edit ✏️ для них на UI нет; защита.
            return

        dlg = RefEditDialog(image_path, episode_refs, kind, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.instruction:
            return
        self._start_ref_thread(
            image_path, 'edit', dlg.instruction, dlg.reference_path)

    def _on_ref_delete(self, image_path: Path, kind: str):
        """Кнопка «🗑 Удалить» на refs-карточке (location/object).
        2026-05-07: переписано симметрично с `_on_ref_character_remove`.
        Раньше метод УДАЛЯЛ файл с диска (+ geometry для локаций) — это
        было опасно: реф мог переиспользоваться в других эпизодах,
        случайный клик стирал безвозвратно. Теперь:
          • Удаляется только запись `refs_decisions[kind][slug]`
            ТЕКУЩЕГО эпизода — реф пропадает из РЕФЕРЕНСОВ этого эпизода.
          • Файл на диске НЕ трогаем (можно переиспользовать через
            «+ Добавить локацию/объект» в этом или другом эпизоде).
          • Geometry-файл НЕ трогаем (по той же причине).
        Тексты попапа: `refs_remove_loc_*` для kind='location',
        `refs_remove_obj_*` для kind='object'.

        Этот метод работает только для kind in ('location', 'object').
        Для character — отдельный flow через `_on_ref_character_remove`."""
        if kind not in ('location', 'object'):
            return
        ep_id = self._current_episode
        if not ep_id:
            return
        slug = Path(image_path).stem.lower()
        if not slug:
            return
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            if kind == 'location':
                box.setWindowTitle(tr('refs_remove_loc_title'))
                box.setText(tr('refs_remove_loc_msg', slug=slug, ep=ep_id))
            else:
                box.setWindowTitle(tr('refs_remove_obj_title'))
                box.setText(tr('refs_remove_obj_msg', slug=slug, ep=ep_id))
            yes_btn = box.addButton(tr('delete_ep_yes'),
                                    QMessageBox.ButtonRole.DestructiveRole)
            no_btn = box.addButton(tr('delete_ep_no'),
                                   QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no_btn)
            box.exec()
            if box.clickedButton() is not yes_btn:
                return
        except Exception:
            traceback.print_exc()
            return
        # Удаляем запись из refs_decisions — файл на диске НЕ трогаем.
        try:
            meta_path = SHOW_ROOT / "episodes.json"
            data = read_episodes_meta(SHOW_ROOT)
            ep = data.get(ep_id)
            if isinstance(ep, dict):
                decisions = ep.get('refs_decisions') or {}
                bucket = decisions.get(kind) or {}
                if slug in bucket:
                    del bucket[slug]
                if not bucket:
                    decisions.pop(kind, None)
                if not decisions:
                    ep.pop('refs_decisions', None)
                meta_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                self._meta = data
                self._build_refs_view(ep_id)
        except Exception:
            traceback.print_exc()

    def _on_ref_done(self, image_path: Path, mode: str,
                       source_ep_id: Optional[str] = None):
        """Регенерация/edit рефа успешно завершён.

        Первая попытка — автоматическое обновление geometry через Claude Code
        CLI в фоне (если CLI установлен). Юзер ничего не делает руками.

        Если CLI не установлен — fallback на старое поведение:
        копим уведомление + подсвечиваем пилюлю «Референсы» (или показываем
        попап сразу если юзер уже на refs-view).

        2026-05-07: `source_ep_id` — эпизод, в котором был запущен
        ref-edit. Прокидывается в geometry-flow чтобы точки на пилюле
        бежали только на пилюле этого эпизода.
        """
        kind = self._ref_kind_from_path(image_path)
        self.status_bar.showMessage("")

        # 2026-05-07: image gen завершилась — снимаем из реестра.
        # `_build_ref_card` больше не будет выставлять image_updating overlay
        # на новой карточке (для location включится geometry-фаза).
        try:
            self._active_image_paths.pop(image_path.resolve(), None)
        except Exception:
            pass

        # 2026-05-11 (v1.0.45): auto-update filename в `refs_decisions` для
        # ВСЕХ эпизодов где этот реф был `linked`. Раньше после регенерации
        # на этой же карточке (которая может прийти с другим расширением,
        # если pipeline сохранил содержимое не по hint-у — БАГ 10 era)
        # filename в decisions оставался устаревшим (например `.png` в
        # decisions, `.jpg` на диске). Для location/object у нас был disk-
        # glob fallback в `_linked_file_exists`, но он маскировал проблему
        # и не лечил источник. Теперь чиним при каждой удачной регенерации.
        try:
            self._sync_decision_filenames_after_regen(image_path, kind)
        except Exception:
            traceback.print_exc()

        on_refs_view = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
            and self._current_episode is not None
        )

        # 2026-05-07: для LOCATION: чтобы overlay не моргал между фазами
        # (image gen → geometry), стартуем geometry-тред ДО `_build_refs_view`.
        # Тогда `_active_geometry_paths` уже содержит путь, и
        # `_build_ref_card` (новая карточка после rebuild'а) сразу вызовет
        # `set_geometry_updating(True)` — overlay непрерывный, меняется
        # только label («Обновляю картинку» → «Обновляю описание»).
        # Для OBJECT: фазы 2 нет, просто снимаем image_updating и rebuild
        # — overlay уйдёт чисто (картинка готова, нечего показывать).
        if kind == 'object':
            try:
                card_for_image = self._find_ref_card_by_path(image_path)
                if (card_for_image is not None
                        and hasattr(card_for_image, 'set_image_updating')):
                    card_for_image.set_image_updating(False)
            except Exception:
                traceback.print_exc()
            # 2026-05-07: помечаем как «новый» — NEW-бейдж + мигание пилюли.
            self._mark_ref_unseen(
                source_ep_id or self._current_episode, image_path)
            if on_refs_view:
                self._build_refs_view(self._current_episode)
            self._refresh_refs_pill_text()
            return

        # LOCATION ниже:

        # ПОПЫТКА #1 — автомат через Claude Code CLI (только для location).
        # Сначала стартуем geometry — это добавляет путь в
        # `_active_geometry_paths`. Потом rebuild увидит путь и навесит
        # `set_geometry_updating(True)` на новую карточку.
        if find_claude_cli() is not None:
            self._start_geometry_thread(image_path, source_ep_id=source_ep_id)
            # 2026-05-07: для LOCATION НЕ помечаем как unseen здесь —
            # geometry-фаза ещё впереди. NEW-бейдж появится только когда
            # geometry завершится (в `_on_geometry_done`). Раньше бейдж
            # появлялся пока overlay ещё показывал «Обновляю описание» —
            # юзер видел противоречие.
            # Сохраняем source_ep_id на треде чтобы _on_geometry_done знал,
            # для какого эпизода ставить unseen.
            try:
                if (hasattr(self, '_geometry_threads')
                        and self._geometry_threads):
                    last_t = self._geometry_threads[-1]
                    if last_t is not None:
                        last_t._source_ep_id = (
                            source_ep_id or self._current_episode)
            except Exception:
                traceback.print_exc()
            if on_refs_view:
                self._build_refs_view(self._current_episode)
            # Точки на пилюле не снимаем — операция продолжается через
            # ClaudeGeometryThread, _refs_busy() останется True
            self._refresh_refs_pill_text()
            return

        # FALLBACK — CLI не установлен. Снимаем image_updating, rebuild,
        # старое поведение (попап с фразой для копирования).
        try:
            card_for_image = self._find_ref_card_by_path(image_path)
            if (card_for_image is not None
                    and hasattr(card_for_image, 'set_image_updating')):
                card_for_image.set_image_updating(False)
        except Exception:
            traceback.print_exc()
        # 2026-05-07: NEW-бейдж + мигание пилюли (тоже для location-fallback).
        self._mark_ref_unseen(
            source_ep_id or self._current_episode, image_path)
        if on_refs_view:
            self._build_refs_view(self._current_episode)
        self._fallback_ref_notice(image_path, mode, kind, on_refs_view)
        self._refresh_refs_pill_text()

    def _fallback_ref_notice(self, image_path: Path, mode: str,
                             kind: str, on_refs_view: bool):
        """Старое поведение когда автомат недоступен (CLI не установлен)."""
        if on_refs_view:
            dlg = RefDoneNoticeDialog(image_path.stem, parent=self, kind=kind)
            dlg.exec()
            return
        # Не на refs-view → копим уведомление, подсвечиваем пилюлю
        self._pending_ref_notices = [
            n for n in self._pending_ref_notices if n[0] != image_path
        ]
        self._pending_ref_notices.append((image_path, mode, kind))
        self._set_refs_pill_notice(True)

    def _find_ref_card_by_path(self, image_path: Path) -> Optional['RefCard']:
        """Ищет RefCard в текущем refs-view по пути картинки.
        Возвращает None если refs-view не открыт или карточки не существует."""
        try:
            target = image_path.resolve()
        except Exception:
            target = image_path
        for c in self.findChildren(RefCard):
            try:
                if c._image_path.resolve() == target:
                    return c
            except Exception:
                continue
        return None

    def _set_card_geometry_busy(self, image_path: Path, busy: bool):
        """Помечает карточку с этим путём как занятую/свободную.
        Безопасно если карточка не найдена (refs-view не открыт)."""
        card = self._find_ref_card_by_path(image_path)
        if card is not None:
            card.set_geometry_updating(busy)

    def _start_geometry_thread(self, image_path: Path,
                                 source_ep_id: Optional[str] = None):
        """Запускает ClaudeGeometryThread в фоне. После завершения geometry-файл
        будет переписан, в статус-баре — итоговое сообщение.
        Параллельно блокирует карточку чтобы юзер случайно не кликнул на
        картинку (это могло бы запустить повторную регенерацию).

        2026-05-07: `source_ep_id` — эпизод в котором запустили ref-edit.
        Записываем его в `_active_geometry_paths[path]` чтобы точки на
        пилюле «Референсы» бежали только на этом эпизоде, а не на всех."""
        ref_name = image_path.stem
        # Фраза на текущем языке UI, которую отправим Claude
        try:
            phrase = tr('ref_chat_phrase_location', name=ref_name)
        except Exception:
            phrase = f"обнови geometry для {ref_name}"
        self.status_bar.showMessage(tr('geom_auto_status', name=ref_name))

        # Блокируем карточку: hover/click по картинке игнорируются пока
        # Claude обрабатывает geometry. После _on_geometry_done/error разблок.
        # ep_id берём из параметра или, если не передан, из текущего —
        # для legacy-совместимости.
        ep_for_path = source_ep_id or getattr(self, '_current_episode', None)
        self._active_geometry_paths[image_path.resolve()] = ep_for_path
        self._set_card_geometry_busy(image_path, True)

        thread = ClaudeGeometryThread(
            image_path=image_path,
            project_root=self._project_root,
            ref_name=ref_name,
            lang_phrase=phrase,
        )
        # Сохраняем ссылку чтобы поток не убил GC до завершения
        if not hasattr(self, '_geometry_threads'):
            self._geometry_threads = []
        self._geometry_threads.append(thread)
        thread.finished.connect(self._on_geometry_done)
        thread.error.connect(self._on_geometry_error)
        thread.start()
        self._refresh_refs_pill_text()

    def _on_geometry_done(self, image_path: Path):
        """Claude CLI успешно переписал geometry-файл."""
        ref_name = image_path.stem
        self.status_bar.showMessage(tr('geom_auto_done', name=ref_name))
        # 2026-05-07: помечаем реф как «новый» (NEW-бейдж + мигание
        # пилюли). Для location это правильное место — geometry-фаза
        # завершилась, реф ПОЛНОСТЬЮ готов. Раньше unseen ставился в
        # `_on_ref_done` (после image-gen), но overlay ещё крутил
        # «Обновляю описание» — юзер видел NEW-бейдж + работающий
        # overlay одновременно (противоречие).
        try:
            sender_t = self.sender()
            source_ep = getattr(sender_t, '_source_ep_id', None) if sender_t else None
            self._mark_ref_unseen(
                source_ep or self._current_episode, image_path)
        except Exception:
            traceback.print_exc()
        # Снимаем блокировку с карточки — клики снова работают
        self._active_geometry_paths.pop(image_path.resolve(), None)
        self._set_card_geometry_busy(image_path, False)
        # Очистим список завершённых потоков (опционально)
        if hasattr(self, '_geometry_threads'):
            self._geometry_threads = [
                t for t in self._geometry_threads if t.isRunning()]

        # Если юзер сейчас НЕ на refs-view — заводим pending-notice и зажигаем
        # пульсацию пилюли «РЕФЕРЕНСЫ» чтобы юзер увидел: «локация полностью
        # готова». Когда он переключится на refs — покажем GeometryDoneNoticeDialog.
        # Если он УЖЕ на refs-view — карточка обновилась, busy-overlay снят, и
        # статус-бара «✓ geometry обновлена» достаточно: попап не нужен.
        on_refs_view = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
        )
        if not on_refs_view:
            kind = self._ref_kind_from_path(image_path)
            # Дедуп: если для этого пути уже есть запись — заменяем (свежее важнее)
            self._pending_ref_notices = [
                n for n in self._pending_ref_notices if n[0] != image_path
            ]
            self._pending_ref_notices.append((image_path, 'geometry_done', kind))
            self._set_refs_pill_notice(True)
        # Если ничего больше не активно — снять точки с пилюли
        self._refresh_refs_pill_text()

    def _on_geometry_error(self, image_path: Path, msg: str):
        """CLI вернул ошибку или не найден. Показываем fallback-попап
        с фразой для ручного копирования (старое поведение)."""
        ref_name = image_path.stem
        self.status_bar.showMessage(
            tr('geom_auto_error', msg=msg[:80]))
        # Снимаем блокировку с карточки даже при ошибке
        self._active_geometry_paths.pop(image_path.resolve(), None)
        self._set_card_geometry_busy(image_path, False)
        # Fallback на попап
        kind = self._ref_kind_from_path(image_path)
        on_refs_view = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
        )
        self._fallback_ref_notice(image_path, 'auto-failed', kind, on_refs_view)
        if hasattr(self, '_geometry_threads'):
            self._geometry_threads = [
                t for t in self._geometry_threads if t.isRunning()]
        self._refresh_refs_pill_text()

    def _on_ref_error(self, image_path: Path, msg: str):
        # Phase 2 hotfix #18 (Bug Modal): убрали блокирующий QMessageBox.
        # Ошибка регенерации показывается ненавязчиво через status_bar
        # (как в `_on_geometry_error`) + пилюля REFS моргает уведомлением.
        # Phase 2 hotfix #19: ВЕСЬ метод обёрнут в try/except — PyQt6
        # на unhandled Python exception в слоте делает abort() (fatal),
        # не printf. Любой baz внутри = краш приложения.
        try:
            try:
                fname = image_path.name if hasattr(image_path, 'name') else str(image_path)
            except Exception:
                fname = str(image_path)
            short = ""
            if msg:
                try:
                    short = (str(msg).splitlines() or [""])[0][:120]
                except Exception:
                    short = str(msg)[:120]
            # Коммит E: персистентный лог ПОЛНОГО текста ошибки в _studio_diag.log.
            try:
                self._log_ref_error(image_path, str(msg) if msg else "(no message)")
            except Exception:
                pass
            # Коммит E: статус-бар без автоскрытия (0) — раньше исчезало за 10с.
            try:
                self.status_bar.showMessage(f"✗ {fname}: {short}", 0)
            except Exception:
                pass
            try:
                card = self._find_ref_card_by_path(image_path)
                if card is not None:
                    if hasattr(card, 'set_loading'):
                        card.set_loading(False)
                    if hasattr(card, 'set_geometry_updating'):
                        card.set_geometry_updating(False)
                    # 2026-05-07: на ошибке тоже снимаем image_updating
                    # overlay (если фаза image gen не дошла до finished).
                    if hasattr(card, 'set_image_updating'):
                        card.set_image_updating(False)
                # 2026-05-07: снимаем из глобального реестра — иначе
                # rebuild navесит overlay вечно.
                try:
                    self._active_image_paths.pop(image_path.resolve(), None)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                on_refs_view = (
                    hasattr(self, 'content_stack')
                    and self.content_stack.currentIndex() == 1)
                if not on_refs_view and hasattr(self, '_set_refs_pill_notice'):
                    self._set_refs_pill_notice(True)
            except Exception:
                pass
            try:
                if hasattr(self, '_refresh_refs_pill_text'):
                    self._refresh_refs_pill_text()
            except Exception:
                pass
        except Exception as e:
            # Финальный сейфти-сетка: даже если что-то непредвиденное —
            # никаких abort/crash. Печатаем в stderr и продолжаем.
            try:
                import traceback
                traceback.print_exc()
                print(f"[_on_ref_error fatal-guard] {e}", flush=True)
            except Exception:
                pass

    def _on_regen(self, panel_idx: int, parent_version: int = 0,
                  block_name: Optional[str] = None):
        # 2026-06-19 (Вариант 2): блок берём ИЗ СИГНАЛА (block_name), а не из
        # self.current_block — попап ShotViewerDialog НЕ модальный, current_block
        # мог занулиться/смениться к моменту клика «Перегенерировать» (был
        # «немой» ранний return → regen не стартовал). Fallback на current_block —
        # для карточки грида (она эмитит regen_requested без block_name).
        target_block = block_name or self.current_block
        if not target_block:
            self._log_shot_regen(
                f"skip-no-block panel={panel_idx} "
                f"block_name={block_name!r} current_block={self.current_block!r}")
            return

        key = (target_block, panel_idx)

        # 2026-07-02: Mode C + versions_per_shot>1 → «Перегенерировать» = ДОБОР
        # недостающих ПЛОСКИХ слотов v1..vN (а не одиночная дочерняя версия).
        # present считаем ТОЛЬКО по верхнеуровневым слотам: vn ≤ N И БЕЗ
        # parent_v{vn}.json (дочерние (parent_version>0) sidecar имеют — они не
        # слоты; их номер обычно >N, но при дырах может попасть в 1..N, потому
        # фильтруем по sidecar, не по одному диапазону).
        if (mode_loader.get_current_mode() == 'c'
                and mode_c_versions_per_shot() > 1):
            self._generation_stopped = False
            # уже идёт по этому шоту (ручной regen ИЛИ слот конвейера) → не дублируем
            if key in self._active_regens or key in self._shot_window_inflight:
                self.status_bar.showMessage(
                    tr('status_already_genning', n=panel_idx + 1))
                self._refresh_stop_btn()
                return
            _n = mode_c_versions_per_shot()
            _hist = shot_history_dir(target_block, panel_idx)
            _present = set()
            for _p in list_shot_versions(_hist):
                try:
                    _vn = int(_p.stem[1:])
                except (ValueError, IndexError):
                    continue
                # верхнеуровневый слот: в диапазоне И без parent-сайдкара
                if _vn <= _n and not (_hist / f"parent_v{_vn}.json").exists():
                    _present.add(_vn)
            _gaps = [v for v in range(1, _n + 1) if v not in _present]
            if not _gaps:
                # Попап открыт → баннер поверх него (он НЕ закроется: диалог
                # читает suppress-флаг после синхронного DirectConnection-эмита).
                # Нет попапа (клик с карточки грида) → прежний тихий статус-бар.
                dlg = self._get_open_shot_viewer(target_block, panel_idx)
                if dlg is not None:
                    dlg.show_limit_banner(_n)
                else:
                    self.status_bar.showMessage(
                        tr('status_all_versions_present', versions_count=_n))
                self._log_shot_regen(
                    f"topup-noop block={target_block} panel={panel_idx} "
                    f"n={_n} (все слоты на месте)")
                return
            # ручной клик перебивает исчерпанный бюджет авто-проходов
            self._shot_retry_pass[key] = 0
            # держим слот шота в конвейере + учёт pending (как серийный добор)
            self._shot_window_inflight.add(key)
            self._shot_window_versions_left[key] = len(_gaps)
            self._storyboard_active_pending += len(_gaps)
            _now = time.time()
            self._shot_gen_started_at[key] = _now
            if (self.current_block == target_block
                    and 0 <= panel_idx < len(self.shot_cards)):
                try:
                    self.shot_cards[panel_idx].set_loading(True)
                    self.shot_cards[panel_idx].start_progress(_now, redrive=True)
                except Exception:
                    pass
            # спавним ТОЛЬКО дыры как ПЛОСКИЕ слоты (version_index=v, БЕЗ
            # parent_version → sidecar не пишется), ракурс из v{v}.prompt.txt.
            for v in _gaps:
                # NEW на доборной версии (снимется кликом по плитке в попапе).
                self._unseen_versions.add((target_block, panel_idx, v))
                _cam = self._recover_camera_override(target_block, panel_idx, v)
                self._spawn_one_mode_c_version(target_block, panel_idx, v, _cam)
            self._refresh_block_indicator(target_block)
            self._refresh_stop_btn()
            self._log_shot_regen(
                f"topup block={target_block} panel={panel_idx} n={_n} "
                f"gaps={_gaps}")
            self.status_bar.showMessage(
                tr('status_topup_started', versions_count=len(_gaps), n=_n))
            return
        # ── иначе (versions_per_shot<=1 или не Mode C) — прежнее поведение ──

        # 2026-06-19: пользовательский regen = свежее намерение генерации.
        # Снимаем залипший глобальный стоп-флаг В САМОМ НАЧАЛЕ — чтобы ЛЮБОЙ
        # последующий _refresh_stop_btn (в т.ч. на раннем return ниже) видел
        # stopped=False. Корень бага e7e4868: [SHOT-REGEN] start не писался →
        # _on_regen уходил в ранний return (guard двойного клика) ДО прежней
        # точки сброса после thread.start().
        self._generation_stopped = False

        # Защита от двойного клика на один и тот же шот
        if key in self._active_regens:
            self.status_bar.showMessage(tr('status_already_genning', n=panel_idx + 1))
            # Кнопку всё равно обновляем (флаг уже снят) — иначе она залипнет
            # в «Останавливаю…» именно на этом раннем return.
            self._refresh_stop_btn()
            self._log_shot_regen(
                f"skip-already-active block={target_block} panel={panel_idx}")
            return

        # Дизейблим только КОНКРЕТНУЮ карточку (другие шоты остаются доступны
        # для параллельной регенерации, в том числе в других блоках).
        _now = time.time()
        self._shot_gen_started_at[(target_block, panel_idx)] = _now
        # Карточку грида трогаем ТОЛЬКО если сейчас открыт именно target_block
        # (он мог прийти из сигнала и отличаться от current_block).
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            card = self.shot_cards[panel_idx]
            card.set_loading(True)
            card.start_progress(_now)

        thread = GenerateThread(target_block, panel_idx,
                                parent_version=parent_version,
                                aspect=self._current_aspect)
        self._active_regens[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
        thread.step.connect(
            lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
        thread.finished.connect(
            lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        # Стоп-флаг уже снят в начале метода; кнопку обновляем под активный regen.
        self._refresh_stop_btn()
        self._log_shot_regen(
            f"start block={target_block} panel={panel_idx} parent={parent_version}")
        # Показать ⋯ возле блока в списке (идёт регенерация)
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(tr('status_regenerating', n=panel_idx + 1, block=target_block))

    def _on_make_realistic(self, panel_idx: int, parent_version: int = 0,
                           block_name: Optional[str] = None):
        """2026-06-01: «🎬 Сделать реалистичным» из ShotViewerDialog.
        Фотореалистичный ре-рендер текущей активной версии шота. Логика
        идентична `_on_regen`, отличие одно — поток стартует с realistic=True
        (внутри GenerateThread это edit-механика + фотореалистичный промпт).
        Переиспользует тот же реестр `_active_regens` и те же callback'и
        `_on_regen_step / _on_regen_done / _on_regen_error`."""
        # 2026-06-19 (Вариант 2): блок из сигнала (см. _on_regen).
        target_block = block_name or self.current_block
        if not target_block:
            self._log_shot_regen(
                f"skip-no-block panel={panel_idx} realistic=1 "
                f"block_name={block_name!r} current_block={self.current_block!r}")
            return

        key = (target_block, panel_idx)

        # 2026-06-19: см. _on_regen — снимаем стоп-флаг В НАЧАЛЕ (до guard),
        # чтобы любой последующий _refresh_stop_btn видел stopped=False.
        self._generation_stopped = False

        # Защита от двойного клика (та же блокировка что у обычного regen —
        # один шот не может генериться дважды параллельно).
        if key in self._active_regens:
            self.status_bar.showMessage(tr('status_already_genning', n=panel_idx + 1))
            self._refresh_stop_btn()
            self._log_shot_regen(
                f"skip-already-active block={target_block} panel={panel_idx} realistic=1")
            return

        _now = time.time()
        self._shot_gen_started_at[(target_block, panel_idx)] = _now
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            card = self.shot_cards[panel_idx]
            card.set_loading(True)
            card.start_progress(_now)

        thread = GenerateThread(target_block, panel_idx, realistic=True,
                                parent_version=parent_version,
                                aspect=self._current_aspect)
        self._active_regens[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
        thread.step.connect(
            lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
        thread.finished.connect(
            lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        # Стоп-флаг уже снят в начале метода; кнопку обновляем под активный regen.
        self._refresh_stop_btn()
        self._log_shot_regen(
            f"start block={target_block} panel={panel_idx} parent={parent_version} realistic=1")
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(tr('status_regenerating', n=panel_idx + 1, block=target_block))

    def _on_regen_step(self, lbl: str, pct: int, target_block: str, panel_idx: int):
        # Прогресс показываем ТОЛЬКО если пользователь сейчас смотрит на тот
        # блок где идёт регенерация. Иначе обновлять чужие карточки нельзя.
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            self.shot_cards[panel_idx].set_progress(lbl, pct)

    def _on_regen_done(self, panel_idx: int, target_block: str, elapsed_seconds: int = 0):
        self._retire_thread(self._active_regens.pop((target_block, panel_idx), None))
        self._shot_gen_started_at.pop((target_block, panel_idx), None)
        # Помечаем шот «непросмотренным» — на карточке появится бейдж NEW.
        # Очистится когда юзер переключится с этого блока на другой.
        self._unseen_shots.add((target_block, panel_idx))
        # 2026-06-19: diag-лог завершения одиночного regen ([SHOT-REGEN]).
        try:
            _vers = list_shot_versions(shot_history_dir(target_block, panel_idx))
            _new_v = _vers[-1].stem[1:] if _vers else "?"
        except Exception:
            _new_v = "?"
        self._log_shot_regen(
            f"done block={target_block} panel={panel_idx} new_version={_new_v}")

        # Сохраняем длительность генерации в QSettings (показывается на карточке)
        if elapsed_seconds > 0:
            try:
                key = f"gen_time_{target_block}_shot{panel_idx + 1}"
                QSettings(APP_ORG, APP_NAME).setValue(key, int(elapsed_seconds))
            except Exception:
                pass

        # Обновляем текущий блок ТОЛЬКО если есть что показывать:
        # 1. Завершилась генерация в нём же — нужно показать новую картинку
        # 2. В нём ОСТАЛИСЬ другие активные регенерации — их прогресс надо
        #    обновить (после этого callback'а одна регенерация уже снята из
        #    _active_regens, остальные — реальные оставшиеся).
        # Иначе (юзер на чужом блоке без активных регенераций) — НЕ перерисовываем,
        # иначе все карточки моргают на 1 кадр без пользы.
        if self.current_block and self._is_blocks_view_active():
            needs_redraw = (
                self.current_block == target_block
                or any(b == self.current_block
                       for (b, _) in self._active_regens)
            )
            if needs_redraw:
                self._display_block(self.current_block)
        # Обновляем индикатор у ЦЕЛЕВОГО блока в списке (✓ если все шоты готовы)
        self._refresh_block_indicator(target_block)
        # 2026-05-07: если попап ShotViewerDialog для этого шота открыт —
        # перерисуем его (новая версия в ленте + превью).
        try:
            self.refresh_open_shot_viewer(target_block, panel_idx)
        except Exception:
            import traceback
            traceback.print_exc()

        if self.current_block == target_block:
            self.status_bar.showMessage(tr('status_shot_done', n=panel_idx + 1))
        else:
            self.status_bar.showMessage(
                tr('status_shot_done_other', n=panel_idx + 1, block=target_block))

    def _on_regen_error(self, msg: str, target_block: str, panel_idx: int):
        self._retire_thread(self._active_regens.pop((target_block, panel_idx), None))
        self._shot_gen_started_at.pop((target_block, panel_idx), None)
        # 2026-06-19: diag-лог ошибки одиночного regen ([SHOT-REGEN]).
        self._log_shot_regen(
            f"error block={target_block} panel={panel_idx} reason={(msg or '')[:200]}")

        # Перерисовываем текущий блок ТОЛЬКО если есть смысл (см. _on_regen_done
        # выше — та же логика, иначе моргание у юзера на чужом блоке).
        if self.current_block and self._is_blocks_view_active():
            needs_redraw = (
                self.current_block == target_block
                or any(b == self.current_block
                       for (b, _) in self._active_regens)
            )
            if needs_redraw:
                self._display_block(self.current_block)
        # Снимаем ⋯ с блока (генерация прервалась, но новых файлов не появилось)
        self._refresh_block_indicator(target_block)

        prefix = "Ошибка" if self.current_block == target_block else f"Ошибка [{target_block}]"
        self.status_bar.showMessage(f"{prefix} SHOT {panel_idx + 1}: {msg}")

        # 2026-05-06: при ручной regen-ошибке (Edit-popup или 🔄 на карточке)
        # тоже пишем в чат эпизода — иначе юзер видит только мелькающую
        # status_bar строку и не понимает что шот упал. До этой правки
        # такое уведомление было только в pipeline-флоу (_on_storyboard_shot_error).
        try:
            self._notify_storyboard_failure(
                target_block, panel_idx,
                f"SHOT {panel_idx + 1} блока «{target_block}»: {msg[:300]}"
            )
        except Exception:
            traceback.print_exc()

    # ── Storyboard pipeline (Этап 2, 2026-05-06) ─────────────────────────────
    # Слушатели сигналов от `StoryboardPipelineThread` (PromptWriter).
    # Pipeline пишет .txt промпты блоков — на каждый ready'у:
    #  1. перерисовываем pill'ы (на случай если блок появился после
    #     первого рендера),
    #  2. собираем список шотов блока (panel_idx с непустыми панелями
    #     и без уже сгенерированных файлов),
    #  3. если активного блока нет — стартуем все шоты этого блока
    #     ПАРАЛЛЕЛЬНО. Иначе кладём блок в очередь.
    #
    # 2026-05-08: переход с sequential (1 шот за раз) на batch-per-block
    # (внутри блока — параллельно по числу шотов, между блоками —
    # последовательно по завершении всех шотов предыдущего).

    def _on_storyboard_block_prompt_ready(self, block_n: int,
                                            block_basename: str):
        """Pipeline дописал .txt для блока block_n. block_basename —
        имя файла без `.txt` (например "ep1_block_1"). Собираем список
        шотов и запускаем их батчем."""
        try:
            if getattr(self, '_generation_stopped', False):
                return   # стоп активен — поздние prompt_ready не стартуют блок
            if hasattr(self, '_render_block_pills'):
                self._render_block_pills()

            prompt_path = PROMPTS_DIR / f"{block_basename}.txt"
            if not prompt_path.exists():
                self.status_bar.showMessage(
                    f"Промпт блока {block_n} не найден на диске.")
                return
            try:
                prompt_text = prompt_path.read_text(encoding='utf-8')
            except Exception as e:
                self.status_bar.showMessage(
                    f"Не смог прочитать промпт блока {block_n}: {e}")
                return

            # Конвейер по шотам (Mode C, N>1): порядок блоков строгий. Анкер
            # счётчика выпуска на первый блок свежего прогона (всё пусто) — нужен
            # ДО skip-ветки, чтобы пропущенный блок тоже продвигал порядок.
            _mode_c_window = (mode_loader.get_current_mode() == 'c'
                              and mode_c_versions_per_shot() > 1)
            if _mode_c_window:
                self._maybe_anchor_block_release(block_n)

            shots: List[tuple] = []
            for panel_idx in range(PANELS):
                body = extract_shot_prompt(prompt_text, panel_idx, aspect=self._current_aspect)
                if not body:
                    continue
                # Уже на диске (юзер успел регенерить вручную) — пропуск
                if shot_path(block_basename, panel_idx).exists():
                    continue
                # Юзер сам уже регенерит — пусть идёт своим треком
                if (block_basename, panel_idx) in self._active_regens:
                    continue
                shots.append((block_basename, panel_idx))

            # Если первый блок и юзер ещё на чате — переключаемся на него
            # чтобы было видно как начинают тикать секунды на карточках.
            if (block_n == 1 and self.current_block != block_basename
                    and hasattr(self, '_select_block')):
                try:
                    self._select_block(block_basename)
                except Exception:
                    pass

            if not shots:
                # В блоке нечего генерить (всё на диске или пусто). В конвейере
                # пропущенный блок ОБЯЗАН продвинуть порядок (None-релиз), иначе
                # буфер застопорит выпуск следующих блоков. Legacy — как было.
                if _mode_c_window:
                    self._record_block_release(block_n, None)
                return

            # Конвейер по шотам (Mode C, N>1): блок идёт через буфер упорядочивания
            # (release строго по номеру) → его шоты попадут в окно когда подойдёт
            # его очередь. Legacy (не-C / N==1) — прежняя блок-очередь байт-в-байт.
            if _mode_c_window:
                self._start_storyboard_block(shots)
            elif self._storyboard_active_block is None:
                self._start_storyboard_block(shots)
            else:
                # Активный блок ещё идёт — в очередь
                self._storyboard_blocks_queue.append(shots)
        except Exception:
            traceback.print_exc()

    def _start_storyboard_block(self, shots: List[tuple]):
        """Запускает все шоты блока ПАРАЛЛЕЛЬНО. shots — список
        кортежей (block_basename, panel_idx). Все панели в shots уже
        отфильтрованы: непустые, файла нет, юзер сам не регенерит."""
        if not shots:
            return
        block_basename = shots[0][0]
        self._storyboard_active_block = block_basename
        # 2026-06-11 (defense-in-depth): новый блок стартовал → снять глобальный
        # стоп-флаг (вторая точка сброса помимо episode_chat при старте pipeline).
        self._generation_stopped = False
        # 2026-06-06 (Mode C): N версий на шот. Только режим C и N>1 — уходим
        # в отдельный спавнер; иначе всё ниже работает как раньше (байт-в-байт).
        if (mode_loader.get_current_mode() == 'c'
                and mode_c_versions_per_shot() > 1):
            self._start_storyboard_block_mode_c(
                shots, mode_c_versions_per_shot())
            return
        self._storyboard_active_pending = len(shots)

        _now = time.time()
        for block_basename, panel_idx in shots:
            # started_at для всех шотов блока (overlay покажет верные секунды
            # при заходе на блок); start_progress — только для видимых сейчас.
            self._shot_gen_started_at[(block_basename, panel_idx)] = _now
            # Если юзер сейчас на этом блоке — крутим прелоадер на карточке
            if (self.current_block == block_basename
                    and 0 <= panel_idx < len(self.shot_cards)):
                try:
                    self.shot_cards[panel_idx].set_loading(True)
                    self.shot_cards[panel_idx].start_progress(_now)
                except Exception:
                    pass

            key = (block_basename, panel_idx)
            thread = GenerateThread(block_basename, panel_idx,
                                    aspect=self._current_aspect)
            self._active_regens[key] = thread
            thread.progress.connect(self.status_bar.showMessage)
            thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
            thread.step.connect(
                lambda lbl, pct, bn=block_basename, pi=panel_idx:
                    self._on_regen_step(lbl, pct, bn, pi))
            thread.finished.connect(
                lambda elapsed, bn=block_basename, pi=panel_idx:
                    self._on_storyboard_shot_finished(bn, pi, elapsed))
            thread.error.connect(
                lambda msg, bn=block_basename, pi=panel_idx:
                    self._on_storyboard_shot_error(bn, pi, msg))
            thread.start()

        self._refresh_block_indicator(block_basename)
        self.status_bar.showMessage(
            f"Сториборды: блок {block_basename} → {len(shots)} шот(ов) параллельно…")

    def _maybe_start_next_storyboard_block(self):
        """Активный блок завершён — берём следующий из очереди.
        Если очередь пуста — Pipeline ещё не выдал следующий .txt
        (или эпизод закончился). На следующий ready-сигнал блок
        стартует автоматически из `_on_storyboard_block_prompt_ready`."""
        self._storyboard_active_block = None
        self._storyboard_active_pending = 0
        if getattr(self, '_generation_stopped', False):
            return   # глобальный стоп — следующий блок не запускаем
        if self._storyboard_blocks_queue:
            next_shots = self._storyboard_blocks_queue.pop(0)
            self._start_storyboard_block(next_shots)

    def _on_storyboard_shot_finished(self, block_basename: str,
                                      panel_idx: int, elapsed: int = 0):
        self._on_regen_done(panel_idx, block_basename, elapsed)
        self._storyboard_active_pending = max(
            0, self._storyboard_active_pending - 1)
        if self._storyboard_active_pending == 0:
            self._maybe_start_next_storyboard_block()

    def _on_storyboard_shot_error(self, block_basename: str,
                                    panel_idx: int, msg: str):
        # _on_regen_error сам вызовет _notify_storyboard_failure —
        # упавший шот не блокирует переход к следующему блоку.
        # Юзер перезапустит вручную через карточку шота.
        self._on_regen_error(msg, block_basename, panel_idx)
        self._storyboard_active_pending = max(
            0, self._storyboard_active_pending - 1)
        if self._storyboard_active_pending == 0:
            self._maybe_start_next_storyboard_block()

    # ── Mode C: N версий на шот (2026-06-06) ─────────────────────────────
    def _start_storyboard_block_mode_c(self, shots: List[tuple], n: int):
        """Mode C batch (ДИСПЕТЧЕР). Перед спавном версий — фоновый агент-
        режиссёр камер (camera_director) даёт альт-ракурсы для v2..vN, не
        морозя UI. По его result_ready спавним версии через
        _spawn_mode_c_versions. Если режиссёр не нужен/недоступен — спавним
        сразу с пустым cams (все версии = авторский ракурс, как было)."""
        block_basename = shots[0][0]
        _bm = re.search(r'_block_(\d+)', block_basename)
        block_n = int(_bm.group(1)) if _bm else None
        # Конвейер по шотам: карточка показывает «занято», но таймер здесь НЕ
        # стартуем и started_at НЕ ставим — отсчёт пойдёт в _pump_shot_window при
        # РЕАЛЬНОМ выходе шота из очереди (иначе секунды накручивали бы ожидание
        # в окне: режиссёр камер + слот). set_loading сам таймер не показывает.
        for _b, panel_idx in shots:
            if (self.current_block == block_basename
                    and 0 <= panel_idx < len(self.shot_cards)):
                try:
                    self.shot_cards[panel_idx].set_loading(True)
                except Exception:
                    pass
        # Контекст шотов блока из montage_card (строго по позиции массива).
        shot_contexts = self._collect_camera_shot_contexts(shots)
        cli = find_claude_cli()
        if n > 1 and shot_contexts and cli:
            try:
                from threads.camera_director_thread import CameraDirectorThread
                th = CameraDirectorThread(shot_contexts, n, cli, timeout_sec=120)
                self._camera_director_threads[block_basename] = th
                th.result_ready.connect(
                    lambda cams, s=shots, nn=n, bn=block_basename, b9=block_n:
                        self._on_camera_director_ready(bn, s, nn, cams, b9))
                th.start()
                self.status_bar.showMessage(
                    f"Mode C: подбираю ракурсы для блока {block_basename}…")
                return
            except Exception:
                traceback.print_exc()
        # Фолбэк: без режиссёра — тем же путём (буфер упорядочивания, авторский
        # ракурс), что и результат режиссёра → release строго по номеру блока.
        self._on_camera_director_ready(block_basename, shots, n, {}, block_n)

    def _collect_camera_shot_contexts(self, shots: List[tuple]) -> List[dict]:
        """Контекст шотов блока для агента-режиссёра камер: по каждому шоту
        scene_action / dialog / author_camera (строка CAMERA: из .txt) /
        characters_count. СТРОГО block["shots"][panel_idx] по позиции массива
        + sanity n==panel_idx+1. Любой сбой → [] (режиссёр пропускается)."""
        try:
            block_basename = shots[0][0]
            m = re.match(r'(ep\d+)_block_(\d+)', block_basename)
            if not m or not self._current_show:
                return []
            ep_id, block_n = m.group(1), int(m.group(2))
            show_root = self._project_root / "shows" / self._current_show
            meta_all = read_episodes_meta(show_root)
            montage_card = ((meta_all or {}).get(ep_id) or {}).get('montage_card') or {}
            block = next((b for b in (montage_card.get('blocks') or [])
                          if b.get('n') == block_n), None)
            if block is None:
                return []
            card_shots = block.get('shots') or []
            block_chars = block.get('characters') or []
            try:
                prompt_text = (PROMPTS_DIR / f"{block_basename}.txt").read_text(
                    encoding='utf-8')
            except Exception:
                prompt_text = ""
            ctxs: List[dict] = []
            for _b, panel_idx in shots:
                if panel_idx >= len(card_shots):       # защита
                    continue
                sc = card_shots[panel_idx]             # по ПОЗИЦИИ массива
                if sc.get('n') != panel_idx + 1:       # sanity-страховка
                    sys.stderr.write(
                        f"[camera_director] sanity: {block_basename} idx="
                        f"{panel_idx} имеет n={sc.get('n')}, ждали "
                        f"{panel_idx + 1} — пропуск\n")
                    continue
                author_camera = ""
                if prompt_text:
                    body = _extract_panel_body(prompt_text, panel_idx) or ""
                    cm = re.search(r'(?im)^CAMERA:\s*(.*)$', body)
                    if cm:
                        author_camera = cm.group(1).strip()
                dlg = sc.get('dialog')
                ctxs.append({
                    "panel_idx": panel_idx,
                    "scene_action": sc.get('scene_action') or "",
                    "dialog": (dlg.get('en') or "") if isinstance(dlg, dict) else "",
                    "characters_count": len(block_chars),
                    "author_camera": author_camera,
                })
            return ctxs
        except Exception:
            traceback.print_exc()
            return []

    def _on_camera_director_ready(self, block_basename: str,
                                  shots: List[tuple], n: int, cams: dict,
                                  block_n: Optional[int] = None):
        """Режиссёр вернул ракурсы (или {} при сбое). Снимаем тред из реестра
        и спавним версии с camera_override.

        keep-alive: result_ready эмитится ПОСЛЕДНЕЙ строкой run() (изнутри, до
        возврата), а реестр держит ЕДИНСТВЕННУЮ ссылку — голый pop в слоте даёт
        GC живого QThread → 'Destroyed while running' → abort(). Под конвейером
        директора разных блоков финишируют параллельно → гонка стабильна. Через
        _retire_thread держим ссылку до isFinished() (как у GenerateThread)."""
        self._retire_thread(self._camera_director_threads.pop(block_basename, None))
        # Строгий порядок блоков: результат не сразу в окно, а в буфер
        # упорядочивания → выпуск строго по номеру блока (_drain_block_releases).
        self._record_block_release(block_n, (shots, n, cams or {}))

    def _maybe_anchor_block_release(self, block_n: int):
        """Строгий порядок: если конвейер полностью простаивает (окно, буфер и
        режиссёры пусты) — это первый блок свежего прогона, ставим счётчик
        выпуска на его номер. Среди прогона не трогаем (что-то в полёте)."""
        if (not self._shot_window_inflight and not self._shot_window_queue
                and not self._block_release_ready
                and not self._camera_director_threads):
            self._block_release_next = block_n

    def _record_block_release(self, block_n, payload):
        """Кладёт результат блока в буфер под его номером и дренирует в окно
        строго по порядку. payload=(shots,n,cams) — реальный блок; None —
        пропуск/сбой (двигает порядок, ничего не enqueue'ит). block_n=None
        (номер не распарсили) — деградация: сразу в окно, без упорядочивания."""
        if block_n is None:
            if payload is not None:
                self._spawn_mode_c_versions(*payload)
            return
        self._block_release_ready[block_n] = payload
        self._drain_block_releases()

    def _drain_block_releases(self):
        """Выпускает в окно подряд блоки начиная с _block_release_next, пока
        очередной номер готов в буфере. Реальный payload → _spawn_mode_c_versions
        (enqueue в окно); None → просто пропуск номера."""
        if self._block_release_next is None:
            return
        while self._block_release_next in self._block_release_ready:
            payload = self._block_release_ready.pop(self._block_release_next)
            self._block_release_next += 1
            if payload is not None:
                shots, n_ver, cams = payload
                self._spawn_mode_c_versions(shots, n_ver, cams)

    def _spawn_one_mode_c_version(self, block_basename: str, panel_idx: int,
                                   v: int, camera_override):
        """Спавн ОДНОЙ версии Mode C: создаёт GenerateThread(version_index=v,
        camera_override=...), регистрирует под 3-tuple ключом в
        _active_mode_c_version_threads, заводит сигналы, стартует.

        Вызывается из _spawn_mode_c_versions (первичный проход) И из retry-ветки
        _on_mode_c_version_error (дотяжка). При вызове из retry ключ к этому
        моменту УЖЕ освобождён (pop в начале хендлера) → запись под тем же
        ключом не перезаписывает живую ссылку; старый упавший тред уже ушёл в
        keep-alive (_retire_thread), reaper дожмёт по isFinished() — конфликта нет."""
        key = (block_basename, panel_idx, v)
        thread = GenerateThread(
            block_basename, panel_idx, version_index=v,
            camera_override=camera_override, aspect=self._current_aspect)
        self._active_mode_c_version_threads[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.key_used.connect(self._blink_key_indicator)  # лампочка round-robin
        thread.step.connect(
            lambda lbl, pct, bn=block_basename, pi=panel_idx:
                self._on_regen_step(lbl, pct, bn, pi))
        thread.finished.connect(
            lambda elapsed, bn=block_basename, pi=panel_idx, vi=v:
                self._on_mode_c_version_finished(bn, pi, vi, elapsed))
        thread.error.connect(
            lambda msg, bn=block_basename, pi=panel_idx, vi=v:
                self._on_mode_c_version_error(bn, pi, vi, msg))
        thread.start()

    def _spawn_mode_c_versions(self, shots: List[tuple], n: int, cams: dict):
        """Спавнит N версий на каждый шот в реестр _active_mode_c_version_threads
        (как было), подставляя camera_override из cams для v2..vN. cams пустой
        → camera_override=None для всех → авторский ракурс (текущее поведение)."""
        block_basename = shots[0][0]
        # Конвейер по шотам: pending накапливается (блоки перекрываются в окне).
        self._storyboard_active_pending += len(shots) * n
        # Бюджет инлайн-повтора версий — свежий только на ПЕРВОМ блоке прогона
        # (окно пусто); перекрывающиеся блоки не затирают друг другу бюджет.
        if not self._shot_window_inflight and not self._shot_window_queue:
            self._mode_c_retried = set()
        # Шоты блока → в сквозную очередь окна (каждый со своим cams), пинаем пул.
        for _b, panel_idx in shots:
            self._shot_window_queue.append((block_basename, panel_idx, n, cams))
        self._pump_shot_window()
        self._refresh_block_indicator(block_basename)
        self.status_bar.showMessage(
            f"Сториборды (Mode C, конвейер): блок {block_basename} → "
            f"{len(shots)} шот(ов) x {n} в сквозную очередь…")

    def _pump_shot_window(self):
        """Конвейер по шотам: держит в полёте до mode_c_concurrent_shots() шотов
        одновременно; по мере освобождения слота заводит следующий шот из сквозной
        очереди _shot_window_queue (шоты разных блоков текущего эпизода идут
        подряд — граница блоков не стена). Каждый шот спавнит свои N версий через
        существующий _spawn_one_mode_c_version."""
        if getattr(self, '_generation_stopped', False):
            return
        limit = mode_c_concurrent_shots()
        while (len(self._shot_window_inflight) < limit
               and self._shot_window_queue):
            block_basename, panel_idx, n, cams = self._shot_window_queue.pop(0)
            self._shot_window_inflight.add((block_basename, panel_idx))
            self._shot_window_versions_left[(block_basename, panel_idx)] = n
            # Таймер шота — от РЕАЛЬНОГО старта (выход из очереди), НЕ от прихода
            # блока: пока шот ждал слот в окне, секунды не накручиваются.
            _now = time.time()
            self._shot_gen_started_at[(block_basename, panel_idx)] = _now
            if (self.current_block == block_basename
                    and 0 <= panel_idx < len(self.shot_cards)):
                try:
                    self.shot_cards[panel_idx].set_loading(True)
                    self.shot_cards[panel_idx].start_progress(_now)
                except Exception:
                    pass
            for v in range(1, n + 1):
                self._spawn_one_mode_c_version(
                    block_basename, panel_idx, v, cams.get((panel_idx, v)))

    def _note_shot_version_done(self, block_basename: str, panel_idx: int):
        """Версия шота закрылась (успех / ошибка без повтора). Когда у шота не
        осталось версий в полёте — слот свободен, заводим следующий шот окна.
        При инлайн-повторе хендлер делает early-return ДО этого вызова → счётчик
        не падает и слот держится, пока повтор летит."""
        key = (block_basename, panel_idx)
        left = self._shot_window_versions_left.get(key, 0) - 1
        if left > 0:
            self._shot_window_versions_left[key] = left
            return
        # Все версии шота отстрелялись → авто-добор недостающих (per-shot). Если
        # запустился — слот шота ДЕРЖИМ (добор сам выставил versions_left = число
        # дыр), следующий шот окна НЕ заводим, пока добор летит.
        if self._maybe_redrive_shot_gaps(block_basename, panel_idx):
            return
        # Дыр нет / бюджет проходов исчерпан → освобождаем слот шота, чистим его
        # бюджет добора, заводим следующий шот окна.
        self._shot_window_versions_left.pop(key, None)
        self._shot_window_inflight.discard(key)
        self._shot_retry_pass.pop(key, None)
        self._pump_shot_window()

    def _maybe_redrive_shot_gaps(self, block_basename: str, panel_idx: int) -> bool:
        """Авто-добор per-shot: когда все версии шота отстрелялись, сканирует диск
        на недостающие v1..vN и пере-спавнивает ТОЛЬКО дыры (ракурс из
        v{N}.prompt.txt через _recover_camera_override). До MODE_C_RETRY_MAX_PASSES
        проходов НА ШОТ. Бежит уже после того как сервер мог ожить (FastGen флакает).

        True → запустил добор: слот шота ДЕРЖИМ (versions_left=число дыр), пул
        следующий шот не заводит. False → дыр нет / бюджет исчерпан / стоп / N<=1 →
        слот можно освобождать (обычный путь _note_shot_version_done).

        В рамках слота шота: лишних слотов окна НЕ плодит (inflight не трогаем);
        порядок блоков (_block_release_*) не трогаем; новых блоков не заводим."""
        if getattr(self, '_generation_stopped', False):
            return False
        n = mode_c_versions_per_shot()
        if n <= 1:
            return False
        key = (block_basename, panel_idx)
        if self._shot_retry_pass.get(key, 0) >= MODE_C_RETRY_MAX_PASSES:
            return False
        present = set()
        for pth in list_shot_versions(shot_history_dir(block_basename, panel_idx)):
            try:
                present.add(int(pth.stem[1:]))
            except Exception:
                pass
        gaps = [v for v in range(1, n + 1) if v not in present]
        if not gaps:
            return False
        self._shot_retry_pass[key] = self._shot_retry_pass.get(key, 0) + 1
        # слот шота держим: versions_left = число дыр; pending +дыры (каждая
        # версия декрементит его в своём хендлере, как при первичном спавне).
        self._shot_window_versions_left[key] = len(gaps)
        self._storyboard_active_pending += len(gaps)
        # таймер: добор — НОВЫЙ заход → метку СБРАСЫВАЕМ на свежую (секунды с 0,
        # не суммируем с primary). Видимой карточке — start_progress свежей меткой
        # + режим «Добор» (redrive=True, подпись над секундами).
        _now_redrive = time.time()
        self._shot_gen_started_at[key] = _now_redrive
        if (self.current_block == block_basename
                and 0 <= panel_idx < len(self.shot_cards)):
            try:
                self.shot_cards[panel_idx].set_loading(True)
                self.shot_cards[panel_idx].start_progress(_now_redrive, redrive=True)
            except Exception:
                pass
        for v in gaps:
            cam = self._recover_camera_override(block_basename, panel_idx, v)
            self._spawn_one_mode_c_version(block_basename, panel_idx, v, cam)
        try:
            self._notify_storyboard_failure(
                block_basename, panel_idx,
                f"Авто-добор шота {panel_idx + 1} (проход "
                f"{self._shot_retry_pass[key]}/{MODE_C_RETRY_MAX_PASSES}): "
                f"{len(gaps)} недостающих версий")
            self._refresh_block_indicator(block_basename)
        except Exception:
            traceback.print_exc()
        return True

    def _on_mode_c_version_finished(self, block_basename: str,
                                     panel_idx: int, version_index: int,
                                     elapsed: int = 0):
        """Одна версия шота Mode C завершилась. pop из нового реестра,
        декремент общего счётчика, обновление UI (аналог _on_regen_done,
        но без касания _active_regens)."""
        self._retire_thread(self._active_mode_c_version_threads.pop(
            (block_basename, panel_idx, version_index), None))
        # Если для этого шота не осталось НИ ОДНОЙ версии — последняя ушла,
        # чистим started_at (overlay гасит re-render блока ниже / reopen loop).
        if not any(b == block_basename and p == panel_idx
                   for (b, p, _v) in self._active_mode_c_version_threads):
            self._shot_gen_started_at.pop((block_basename, panel_idx), None)
        # gen_time per-shot (без version_index) — последний завершившийся
        # тред перезапишет своим временем (= самой долгой версии, т.к.
        # стартовали одновременно). Логика как в _on_regen_done.
        if elapsed > 0:
            try:
                key = f"gen_time_{block_basename}_shot{panel_idx + 1}"
                QSettings(APP_ORG, APP_NAME).setValue(key, int(elapsed))
            except Exception:
                pass
        self._unseen_shots.add((block_basename, panel_idx))
        if self.current_block and self._is_blocks_view_active():
            needs_redraw = (
                self.current_block == block_basename
                or any(b == self.current_block
                       for (b, _p, _v) in self._active_mode_c_version_threads)
            )
            if needs_redraw:
                self._display_block(self.current_block)
        self._refresh_block_indicator(block_basename)
        try:
            self.refresh_open_shot_viewer(block_basename, panel_idx)
        except Exception:
            traceback.print_exc()
        self._storyboard_active_pending = max(
            0, self._storyboard_active_pending - 1)
        # Конвейер по шотам: версия закрылась → освобождаем слот шота, когда у него
        # не осталось версий в полёте (замена блок-сериализации/добора).
        self._note_shot_version_done(block_basename, panel_idx)

    def _on_mode_c_version_error(self, block_basename: str,
                                  panel_idx: int, version_index: int,
                                  msg: str):
        """Одна версия шота Mode C упала. pop из нового реестра, декремент
        счётчика, уведомление юзера. Не блокирует остальные версии/блоки.

        2026-06-09 (дотяжка): ПЕРЕД обычным путём «не вышло» — ОДНА повторная
        попытка этой версии (ракурс из v{N}.prompt.txt). pop выше уже освободил
        ключ → повтор регистрируется чисто; pending НЕ декрементим (слот держит
        повторный тред) → блок не финиширует пока повтор в полёте."""
        self._retire_thread(self._active_mode_c_version_threads.pop(
            (block_basename, panel_idx, version_index), None))
        if self._try_retry_mode_c_version(block_basename, panel_idx,
                                          version_index, msg):
            return
        if not any(b == block_basename and p == panel_idx
                   for (b, p, _v) in self._active_mode_c_version_threads):
            self._shot_gen_started_at.pop((block_basename, panel_idx), None)
        if self.current_block and self._is_blocks_view_active():
            needs_redraw = (
                self.current_block == block_basename
                or any(b == self.current_block
                       for (b, _p, _v) in self._active_mode_c_version_threads)
            )
            if needs_redraw:
                self._display_block(self.current_block)
        self._refresh_block_indicator(block_basename)
        try:
            self._notify_storyboard_failure(
                block_basename, panel_idx,
                f"SHOT {panel_idx + 1} v{version_index} блока "
                f"«{block_basename}»: {msg[:300]}")
        except Exception:
            traceback.print_exc()
        self._storyboard_active_pending = max(
            0, self._storyboard_active_pending - 1)
        # Конвейер по шотам: версия упала (без повтора) → освобождаем слот шота,
        # когда у него не осталось версий в полёте.
        self._note_shot_version_done(block_basename, panel_idx)

    def _recover_camera_override(self, block_basename: str, panel_idx: int,
                                  version_index: int):
        """Дотяжка (путь B): восстановить ракурс упавшей версии из её
        v{N}.prompt.txt (пишется ДО POST → переживает падение по 403/timeout).
        Возвращает строку из `CAMERA:` или None (нет файла / нет строки →
        повтор пойдёт с авторским ракурсом, согласовано). Не кидает."""
        try:
            vpath = (shot_history_dir(block_basename, panel_idx)
                     / f"v{version_index}.prompt.txt")
            if not vpath.exists():
                return None
            txt = vpath.read_text(encoding="utf-8")
            m = re.search(r'(?im)^CAMERA:\s*(.*)$', txt)
            if m:
                return m.group(1).strip() or None
        except Exception:
            traceback.print_exc()
        return None

    def _try_retry_mode_c_version(self, block_basename: str, panel_idx: int,
                                   version_index: int, msg: str = "") -> bool:
        """Дотяжка: РОВНО одна повторная попытка упавшей версии. True → повтор
        запущен (хендлер сразу return, слот остаётся pending). False → повтор не
        нужен/невозможен (обычный путь «не вышло» — исходный msg логируется там).

        Гейты: (0) ошибка НЕ таймаут (таймаут = сервер перегружен; повтор почти
        обречён и удваивает время блока 300+300с → False, ошибка идёт обычным
        путём); (1) key не в _mode_c_retried (анти-задвоение, ≤1 повтор);
        (2) есть v{N}.prompt.txt (есть что воспроизвести путём B); (3) есть живой
        ключ — иначе уведомление + False (без заведомо-мёртвого запроса).

        2026-06-10: при ЗАПУСКЕ повтора в чат пишем и исходный msg — раньше
        early-return глотал текст ошибки (видно было только «повтор»)."""
        # Гейт 0: таймаут — НЕ повторяем (перегруженный сервер). 'timed out'
        # покрывает 'read timed out'; 'api timeout' — наш текст poll-таймаута.
        # 2026-06-13 (вариант D): + ЯВНЫЕ серверные storage/DB-поломки
        # (DiskFull/psycopg2/5xx/no space/OOM) — сервер только что лёг, инлайн-
        # повтор сразу почти обречён и удваивает primary-заход (~300+300с),
        # раздувая таймер шота. Версию закрываем как дыру; недостающее дошлёт
        # ДОБОР позже, когда сервер мог ожить. Дешёвые быстрые кейсы (404-реф и
        # т.п.) инлайн-повтор СОХРАНЯЕТ (они не матчат маркеры ниже).
        _low = (msg or "").lower()
        _server_fail = (
            "diskfull" in _low
            or "could not resize shared memory" in _low
            or "psycopg2" in _low
            or "no space left" in _low
            or "out of memory" in _low
            or "server error" in _low          # 500 Internal Server Error
            or "bad gateway" in _low           # 502
            or "service unavailable" in _low   # 503
            or "gateway timeout" in _low       # 504
        )
        if "api timeout" in _low or "timed out" in _low or _server_fail:
            return False
        key = (block_basename, panel_idx, version_index)
        if key in self._mode_c_retried:
            return False
        vpath = (shot_history_dir(block_basename, panel_idx)
                 / f"v{version_index}.prompt.txt")
        if not vpath.exists():
            return False  # промпта нет (сбой ДО его записи) — нечего повторять
        try:
            import key_pool
            live = key_pool.live_key_count()
        except Exception:
            live = 1  # не смогли посчитать — не блокируем повтор
        if live <= 0:
            try:
                self._notify_storyboard_failure(
                    block_basename, panel_idx,
                    f"SHOT {panel_idx + 1} v{version_index}: живых ключей не "
                    f"осталось — повтор пропущен")
            except Exception:
                traceback.print_exc()
            return False
        self._mode_c_retried.add(key)
        cam = self._recover_camera_override(
            block_basename, panel_idx, version_index)
        self._spawn_one_mode_c_version(block_basename, panel_idx,
                                       version_index, cam)
        try:
            self._notify_storyboard_failure(
                block_basename, panel_idx,
                f"SHOT {panel_idx + 1} v{version_index} блока "
                f"«{block_basename}»: повтор (дотяжка, 1 попытка): {msg[:300]}")
        except Exception:
            traceback.print_exc()
        return True

    def _notify_storyboard_failure(self, block_basename: str,
                                     panel_idx: int, line: str):
        """Записывает строку об ошибке в чат эпизода (persistent jsonl)
        + если EpisodeChatView сейчас открыт на тот эпизод — рендерит
        сообщение мгновенно. ep_id извлекается из basename вида
        `<ep_id>_block_<n>` (например `ep1_block_4` → `ep1`).
        """
        # Извлечь ep_id: всё до '_block_'
        ep_id = ''
        marker = '_block_'
        if marker in block_basename:
            ep_id = block_basename.split(marker, 1)[0]
        if not ep_id:
            return
        text = f"\n⚠ {line}\n"
        # Persistent log — увидится при следующем открытии чата эпизода.
        try:
            append_chat_message(ep_id, 'system', text, kind='err')
        except Exception:
            traceback.print_exc()
        # Мгновенный рендер если юзер сейчас в чате этого эпизода.
        ev = getattr(self, 'episode_chat_view', None)
        if ev is None:
            return
        cur_ep = getattr(ev, '_ep_id', None)
        if cur_ep != ep_id:
            return
        if hasattr(ev, '_render_message'):
            try:
                ev._render_message(text, kind='err')
            except Exception:
                traceback.print_exc()

    def _on_storyboard_block_failed(self, block_n: int, msg: str):
        """PromptWriter не смог написать .txt для блока. Не fatal —
        pipeline идёт дальше; этот блок останется без шотов."""
        try:
            self.status_bar.showMessage(
                f"PromptWriter упал на блоке {block_n}: {msg[:200]}")
        except Exception:
            pass
        # Строгий порядок блоков: упавший блок (.txt не написан, шотов не будет)
        # ОБЯЗАН продвинуть порядок, иначе буфер застопорит выпуск следующих.
        try:
            if (mode_loader.get_current_mode() == 'c'
                    and mode_c_versions_per_shot() > 1):
                self._maybe_anchor_block_release(block_n)
                self._record_block_release(block_n, None)
        except Exception:
            traceback.print_exc()

    def _on_storyboard_pipeline_done(self, success: int, fail: int):
        """Все блоки прошли через PromptWriter. Очередь шотов может
        ещё работать — она независима от этого сигнала."""
        try:
            if fail == 0:
                self.status_bar.showMessage(
                    f"PromptWriter готов: {success} блок(а). "
                    "Шоты допишутся в гриде.")
            else:
                self.status_bar.showMessage(
                    f"PromptWriter: {success} ✓ / {fail} ✗ блока. "
                    "См. лог для деталей.")
        except Exception:
            pass

    # ── Seedance pipeline (Этап 3, 2026-05-06) ───────────────────────────────
    # Параллельно с PromptWriter / GenerateThread. Пока Fast Gen занят
    # шотами — Opus в фоне пишет Seedance промпты блоков. Юзер открывает
    # попап через кнопку «🎬 Промпт Seedance» на блоке.

    def _on_seedance_block_ready(self, block_n: int,
                                    block_basename: str):
        """Файл `output/seedance/<block_basename>.txt` дописан. Если
        юзер сейчас на этом блоке — обновим лейбл кнопки с pending → готов."""
        try:
            if self.current_block == block_basename and hasattr(self, 'seedance_btn'):
                self.seedance_btn.setText(tr('seedance_btn'))
        except Exception:
            pass

    def _on_seedance_block_failed(self, block_n: int, msg: str):
        """Seedance Writer упал на конкретном блоке. Не критично для
        pipeline — у юзера всегда есть кнопка повторить через попап
        (но повтор пока не реализован — будет TODO).
        """
        try:
            self.status_bar.showMessage(
                f"Seedance промпт блок {block_n}: {msg[:200]}")
        except Exception:
            pass

    def _on_seedance_pipeline_done(self, success: int, fail: int):
        try:
            if fail == 0 and success > 0:
                self.status_bar.showMessage(
                    f"Seedance промпты готовы: {success} блок(а).")
            elif fail > 0:
                self.status_bar.showMessage(
                    f"Seedance: {success} ✓ / {fail} ✗ блока.")
        except Exception:
            pass

    SEEDANCE_STUCK_THRESHOLD_SEC = 300  # v1.0.85: 5 минут → restart-mode

    def _compute_seedance_btn_mode(self, ready: bool) -> str:
        """v1.0.85: state-машина для seedance_btn.
        Возвращает 'ready' | 'pending' | 'restart'.

        - ready: файл на диске и >0 байт → клик открывает попап.
        - restart: pipeline бежит, прошло >5 мин, файла всё ещё нет →
          клик зовёт `_on_seedance_restart` (terminate + start заново).
        - pending: обычное «Готовится…».
        """
        if ready:
            return 'ready'
        ev = getattr(self, 'episode_chat_view', None)
        thread = getattr(ev, '_seedance_pipeline_thread', None) if ev else None
        if thread is None:
            return 'pending'
        # `_start_time` устанавливается в run() (Этап 1). До старта None.
        start_time = getattr(thread, '_start_time', None)
        try:
            if not thread.isRunning():
                # Тред уже умер, файла нет — это «мёртвая зависшая задача».
                # Тоже даём кнопку «Перезапустить».
                if start_time is not None:
                    return 'restart'
                return 'pending'
        except Exception:
            return 'pending'
        if start_time is None:
            return 'pending'
        elapsed = time.time() - start_time
        if elapsed > self.SEEDANCE_STUCK_THRESHOLD_SEC:
            return 'restart'
        return 'pending'

    def _on_block_refs_btn(self):
        """v1.0.88 (Stage 16): клик «🗂 Рефы блока».

        Собирает все рефы текущего блока (location + objects + characters)
        в одну временную папку и открывает её в файловом менеджере:
          shows/<show>/.cache/_block_view/<ep_id>_block<N>/

        Каждый клик: rmtree папки → mkdir → копия всех найденных файлов
        → open in Finder/Explorer. Папка — рабочий кэш, не source of
        truth (.cache gitignore-friendly, не попадает в list_episode_refs).

        Cross-platform: subprocess.run с **no_console_kwargs() для тихого
        запуска под Win, shutil.copy2 + Path.mkdir — stdlib.

        Источники данных:
          • montage_card.blocks[i] — slugs (location/objects/characters)
          • refs_decisions[<category>][<slug>] — filename → реальный путь
            на диске (shows/<show>/refs/<plural>/<filename>).

        На пропавшие файлы / отсутствующие decisions — stderr с префиксом
        [block_refs], копируем что есть, открываем папку. Юзер увидит
        в Finder неполный набор и сам решит.
        """
        import shutil
        import sys as _sys_log
        if not self.current_block:
            return
        try:
            m = re.match(r'(ep\d+)_block_(\d+)', self.current_block)
            if not m:
                return
            ep_id = m.group(1)
            block_n = int(m.group(2))

            if not self._current_show:
                return
            show_root = self._project_root / "shows" / self._current_show

            # 1. Свежий episodes.json — `refs_decisions` могли только что
            #    обновиться (после нового linking рефа в чате).
            meta_all = read_episodes_meta(show_root)
            ep_meta = (meta_all or {}).get(ep_id) or {}
            montage_card = ep_meta.get('montage_card') or {}
            blocks = montage_card.get('blocks') or []
            block = next((b for b in blocks if b.get('n') == block_n), None)
            if block is None:
                _sys_log.stderr.write(
                    f"[block_refs] ep={ep_id} block={block_n}: "
                    f"block not found in montage_card.blocks\n")
                _sys_log.stderr.flush()
                QMessageBox.warning(
                    self, tr('block_refs_btn'),
                    tr('block_refs_btn_error'))
                return

            decisions = ep_meta.get('refs_decisions') or {}

            # 2. Собрать список slugs нужных категорий.
            #    Singular keys в refs_decisions: location / object / character.
            #    Plural folders на диске: locations/ objects/ characters/.
            targets: list = []  # list of (category_singular, slug)
            loc = block.get('location')
            if isinstance(loc, str) and loc:
                targets.append(('location', loc))
            for obj_slug in (block.get('objects') or []):
                if isinstance(obj_slug, str) and obj_slug:
                    targets.append(('object', obj_slug))
            for char_slug in (block.get('characters') or []):
                if isinstance(char_slug, str) and char_slug:
                    targets.append(('character', char_slug))

            # 3. Резолвить slug → путь на диске.
            cat_to_plural = {'location': 'locations',
                             'object': 'objects',
                             'character': 'characters'}
            refs_root = show_root / "refs"
            resolved: list = []  # list of (category, slug, src_path, basename_for_dest)
            missing_count = 0
            for cat, slug in targets:
                bucket = decisions.get(cat) if isinstance(decisions, dict) else None
                entry = (bucket or {}).get(slug) if isinstance(bucket, dict) else None
                filename = (entry or {}).get('filename') if isinstance(entry, dict) else None
                if not filename:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"no filename in refs_decisions for {cat}/{slug}\n")
                    missing_count += 1
                    continue
                src = refs_root / cat_to_plural[cat] / filename
                if not src.exists():
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"file not on disk: {src}\n")
                    missing_count += 1
                    continue
                # Для characters filename часто содержит slug-префикс
                # (например "david/david_belaya_khlopkovaya_rubashka.jpg").
                # В dest-папке используем только basename — без поддиректории.
                basename = src.name
                resolved.append((cat, slug, src, basename))

            # 4. Создать .cache/_block_view/<ep>_block<N>/ — очистить если был.
            #    2026-05-17: сторибоарды (<ep>_block<N>.jpg, <ep>_block<N>_2.jpg
            #    и т.д. от _save_png) живут в этой же папке и НЕ должны
            #    удаляться — юзер хочет смотреть рефы и склейку рядом.
            #    Удаляем только то что НЕ сториборд.
            dest_dir = (show_root / ".cache" / "_block_view"
                        / f"{ep_id}_block{block_n}")
            storyboard_pattern = re.compile(
                rf'^{re.escape(ep_id)}_block{block_n}(_\d+)?\.(jpg|jpeg|png)$',
                re.IGNORECASE
            )
            # 2026-05-19: Seedance save-file (.txt) + ZIP-снапшот папки
            # (.zip) с именами вида <show>_<ep_id>_block_<N>.<ext> —
            # сохраняем при cleanup'е. .txt пишется кнопкой «💾 Сохранить»
            # Seedance popup, .zip создаётся ниже в этом же методе
            # (rezip каждый клик).
            seedance_artifact_pattern = re.compile(
                rf'^.+_{re.escape(ep_id)}_block_{block_n}\.(txt|zip)$',
                re.IGNORECASE
            )
            try:
                if dest_dir.exists():
                    for item in dest_dir.iterdir():
                        # 2026-06-02 (Этап 8): grids.json — состояние наложенных
                        # сеток лица (имя PNG + pos + scale), пишется попапом
                        # GridDialog (widgets/face_grid, const GRIDS_JSON_NAME)
                        # рядом со сторибордом. Сохраняем при cleanup'е — иначе
                        # повторное открытие попапа потеряет расстановку сеток.
                        if item.is_file() and (
                                storyboard_pattern.match(item.name)
                                or seedance_artifact_pattern.match(item.name)
                                or item.name == "grids.json"):
                            continue
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                dest_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                _sys_log.stderr.write(
                    f"[block_refs] ep={ep_id} block={block_n}: "
                    f"failed to prepare dest_dir {dest_dir}: "
                    f"{type(e).__name__}: {e}\n")
                _sys_log.stderr.flush()
                QMessageBox.warning(
                    self, tr('block_refs_btn'),
                    tr('block_refs_btn_error'))
                return

            # 5. Копировать. На коллизию имён — добавить префикс категории.
            #    Для category=='character' накапливаем (slug, src.stem)
            #    в character_originals — Этап 4 использует это чтобы
            #    подобрать конкретно ту текстуру, которая сделана
            #    из этого оригинала (а не любую свежую в slug-папке).
            seen_basenames: set = set()
            copied_count = 0
            character_originals: list = []  # list[(slug, src_stem)]
            for cat, slug, src, basename in resolved:
                final_name = basename
                if final_name in seen_basenames:
                    final_name = f"{cat}__{basename}"
                seen_basenames.add(final_name)
                try:
                    shutil.copy2(src, dest_dir / final_name)
                    copied_count += 1
                    if cat == 'character':
                        character_originals.append((slug, src.stem))
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"copy failed for {src}: "
                        f"{type(e).__name__}: {e}\n")

            # 5b. (Этап 4) Дополнить текстурированными версиями персонажей.
            #     Для КАЖДОГО скопированного оригинала персонажа ищем в
            #     characters_texture/<slug>/ файлы которые начинаются с
            #     того же stem + "_" (например для оригинала
            #     "laura_belyy_khalat" подойдут "laura_belyy_khalat_30pct.jpg",
            #     "laura_belyy_khalat_50pct.jpg"). Берём самый свежий по
            #     mtime среди этой группы и кладём в dest_dir под именем
            #     texture__<slug>__<latest.name>.
            #     Префикс "_" в фильтре критичен — иначе "laura_khalat" мог
            #     бы поймать "laura_khalat2_30pct".
            #     Имя dest-файла не матчит storyboard_pattern → cleanup-цикл
            #     на следующем клике его корректно стирает.
            texture_root = show_root / "refs" / "characters_texture"
            texture_count = 0
            allowed_ext = {'.jpg', '.jpeg', '.png'}
            for char_slug, orig_stem in character_originals:
                tex_dir = texture_root / char_slug
                if not tex_dir.exists() or not tex_dir.is_dir():
                    continue
                prefix = orig_stem + "_"
                try:
                    candidates = [
                        p for p in tex_dir.iterdir()
                        if p.is_file()
                        and p.suffix.lower() in allowed_ext
                        and p.stem.startswith(prefix)
                    ]
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"failed to list textures for {char_slug}/{orig_stem}: "
                        f"{type(e).__name__}: {e}\n")
                    continue
                if not candidates:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"no textures for {char_slug}/{orig_stem}\n")
                    continue
                try:
                    latest = max(candidates, key=lambda p: p.stat().st_mtime)
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"failed to pick latest texture for {char_slug}/{orig_stem}: "
                        f"{type(e).__name__}: {e}\n")
                    continue
                dest_name = f"texture__{char_slug}__{latest.name}"
                try:
                    shutil.copy2(latest, dest_dir / dest_name)
                    texture_count += 1
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"texture copy failed for {latest}: "
                        f"{type(e).__name__}: {e}\n")

            # 5b-2. (2026-06-04) Дополнить версиями-с-сеткой персонажей.
            #     Зеркало 5b, но источник — characters_grid/<slug>/ и имя
            #     файла ДЕТЕРМИНИРОВАННОЕ: <orig_stem>_grid.jpg (один файл на
            #     реф, views/actors.py пишет ровно его). Поэтому матчим ТОЧНЫЙ
            #     stem (orig_stem + "_grid"), а не startswith — иначе для рефа
            #     "laura" префикс "laura_" поймал бы и сетку рефа "laura_2".
            #     .json-сайдкар отсекается allowed_ext. max-by-mtime —
            #     defensive на случай дублей расширений. Имя dest grid__<slug>__
            #     не матчит storyboard_pattern → cleanup стирает как кэш.
            grid_root = show_root / "refs" / "characters_grid"
            grid_count = 0
            for char_slug, orig_stem in character_originals:
                grid_dir = grid_root / char_slug
                if not grid_dir.exists() or not grid_dir.is_dir():
                    continue
                target_stem = orig_stem + "_grid"
                try:
                    candidates = [
                        p for p in grid_dir.iterdir()
                        if p.is_file()
                        and p.suffix.lower() in allowed_ext
                        and p.stem == target_stem
                    ]
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"failed to list grids for {char_slug}/{orig_stem}: "
                        f"{type(e).__name__}: {e}\n")
                    continue
                if not candidates:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"no grid for {char_slug}/{orig_stem}\n")
                    continue
                try:
                    latest = max(candidates, key=lambda p: p.stat().st_mtime)
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"failed to pick latest grid for {char_slug}/{orig_stem}: "
                        f"{type(e).__name__}: {e}\n")
                    continue
                dest_name = f"grid__{char_slug}__{latest.name}"
                try:
                    shutil.copy2(latest, dest_dir / dest_name)
                    grid_count += 1
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"grid copy failed for {latest}: "
                        f"{type(e).__name__}: {e}\n")

            _sys_log.stderr.write(
                f"[block_refs] ep={ep_id} block={block_n} "
                f"found={copied_count} missing={missing_count} "
                f"textures={texture_count} grids={grid_count} "
                f"dest={dest_dir}\n")
            _sys_log.stderr.flush()

            # 5c. (2026-05-19) ZIP-снапшот папки для удобной отправки
            # коллегам. Имя: <show>_<ep>_block_<N>.zip. Пересоздаётся
            # каждый клик (Вариант A — всегда актуальный). Сам zip
            # исключается из своего содержимого (защита от рекурсии)
            # и сохраняется от cleanup'а через seedance_artifact_pattern
            # выше. Если папка пуста (нет рефов) — zip не создаём.
            # Если self._current_show пуст (странный edge) — тоже skip.
            if self._current_show:
                try:
                    zip_name = (f"{self._current_show}_{ep_id}"
                                f"_block_{block_n}.zip")
                    zip_path = dest_dir / zip_name
                    if zip_path.exists():
                        zip_path.unlink()
                    zip_path_resolved = zip_path.resolve()
                    items_to_zip = [
                        p for p in dest_dir.iterdir()
                        if p.is_file()
                        and p.name not in ('.DS_Store', 'Thumbs.db')
                        and p.resolve() != zip_path_resolved
                    ]
                    if items_to_zip:
                        with zipfile.ZipFile(
                                zip_path, 'w',
                                zipfile.ZIP_DEFLATED,
                                compresslevel=6) as zf:
                            for item in items_to_zip:
                                zf.write(item, arcname=item.name)
                        _sys_log.stderr.write(
                            f"[block_refs] ep={ep_id} block={block_n} "
                            f"zip={zip_path.name} files={len(items_to_zip)}\n")
                        _sys_log.stderr.flush()
                except Exception as e:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: "
                        f"zip failed: {type(e).__name__}: {e}\n")
                    _sys_log.stderr.flush()
                    # не валим — продолжаем к open

            # 5d. (2026-06-29) Нарезка Seedance-промпта блока на пошотовые
            # папки shots/shot_<k>/ (нарезанный .txt + только рефы шота +
            # сториборд). Только если промпт блока СОХРАНЁН юзером (кнопка
            # «💾 Save» попапа Seedance положила <show>_<ep>_block_<N>.txt в
            # dest_dir); иначе молча пропускаем (нарезка появляется РОВНО
            # когда и общий промпт в «Рефах»). Папка
            # shots/ — dir → текущий cleanup-цикл сносит её на следующем клике
            # и пересоздаёт, без накопления стале. Источник рефов шота —
            # montage_card shots[].scene_action [@]imgK → resolved[K-1].
            try:
                # SAVE-файл — тот же, что пишет _do_save попапа (текст активной
                # вкладки = утверждённая версия). Формат имени байт-в-байт как в
                # _do_save: <show>_<ep>_block_<N>.txt. Генерационный
                # output/seedance/<ep>_block_<N>.txt (есть ДО Save) НЕ берём.
                seedance_txt = (
                    dest_dir
                    / f"{self._current_show}_{ep_id}_block_{block_n}.txt")
                if seedance_txt.exists() and seedance_txt.stat().st_size > 50:
                    from seedance_shot_slicer import slice_block_to_shots
                    # Главный склеенный лист (с face-сетками) в dest_dir —
                    # БЕЗ суффикса _N; из него режется панель шота.
                    sheet = None
                    for p in sorted(dest_dir.glob(f"{ep_id}_block{block_n}.*")):
                        if (p.is_file()
                                and p.suffix.lower() in ('.jpg', '.jpeg', '.png')):
                            sheet = p
                            break
                    # Раскладка панелей листа = как у stitch_shots_to_landscape:
                    # 16:9 → сетка 2×2, иначе (9:16) → PANELS в ряд.
                    _aspect = show_manager.show_aspect(
                        self._project_root, self._current_show)
                    grid_cols, grid_rows = (
                        (2, 2) if _aspect == "16:9" else (PANELS, 1))
                    slice_block_to_shots(
                        seedance_txt, block, resolved, sheet,
                        grid_cols, grid_rows,
                        dest_dir / "shots", ep_id, block_n,
                        log=_sys_log.stderr.write,
                        grid_root=refs_root / "characters_grid",
                    )
                else:
                    _sys_log.stderr.write(
                        f"[block_refs] ep={ep_id} block={block_n}: seedance "
                        f"prompt not saved yet → skip shot-slicing\n")
                    _sys_log.stderr.flush()
            except Exception as e:
                _sys_log.stderr.write(
                    f"[block_refs] ep={ep_id} block={block_n}: shot-slicing "
                    f"failed: {type(e).__name__}: {e}\n")
                _sys_log.stderr.flush()
                # не валим — продолжаем к open

            # 6. Открыть папку в файловом менеджере.
            if sys.platform == "darwin":
                subprocess.run(["open", str(dest_dir)],
                               **no_console_kwargs())
            elif sys.platform == "win32":
                subprocess.run(["explorer", str(dest_dir)],
                               **no_console_kwargs())
            else:
                subprocess.run(["xdg-open", str(dest_dir)],
                               **no_console_kwargs())
        except Exception as e:
            try:
                _sys_log.stderr.write(
                    f"[block_refs] unexpected error: "
                    f"{type(e).__name__}: {e}\n")
                _sys_log.stderr.flush()
            except Exception:
                pass
            traceback.print_exc()
            try:
                QMessageBox.warning(
                    self, tr('block_refs_btn'),
                    tr('block_refs_btn_error'))
            except Exception:
                pass

    def _on_compile_episode_btn(self):
        """v1.0.X (2026-05-19): клик «📦 Собрать серию».

        Запускает CompileEpisodeThread в фоне — собирает все блоки
        текущего эпизода (рефы + сториборды + Seedance .txt) в один zip:
          shows/<show>/output/<ep_id>/<show>_<ep_id>.zip

        UI: 3 состояния кнопки с бегущими точками во время работы.
          • Покой:    "📦 Собрать серию"
          • В работе: "⏳ Собираю серию." → ".." → "..." (QTimer 400ms)
          • Готово:   возврат в покой через 0.5 сек после finished.
                      Finder/Explorer открыт на shows/<show>/output/<ep_id>/.
          • Ошибка:   "✕ Ошибка — повтори" на 3 сек, потом покой.
        """
        if not self._current_episode or not self._current_show:
            return
        # Защита от двойного клика — пока поток жив, кнопка disabled.
        if getattr(self, '_compile_thread', None) is not None:
            return
        # 2026-05-20: confirm-dialog с 2 чекбоксами (сториборды + Seedance).
        # Защищает от случайной сборки серии до того как контент готов.
        # ESC / крестик / «Отмена» → reject → return без старта потока.
        from widgets.compile_confirm_dialog import CompileConfirmDialog
        dlg = CompileConfirmDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        from threads.compile_episode import CompileEpisodeThread

        btn = self.compile_ep_btn
        base_running = tr('compile_ep_btn_running')

        # ── Animation state (бегущие точки) ────────────────────────
        anim = {"timer": None, "tick": 0}

        def _tick():
            anim["tick"] = (anim["tick"] + 1) % 3
            dots = "." * (anim["tick"] + 1)
            try:
                btn.setText(f"{base_running}{dots}")
            except Exception:
                pass

        def _start_animation():
            try:
                btn.setText(f"{base_running}.")
                # резерв ширины чтобы кнопка не дёргалась
                fm = btn.fontMetrics()
                reserved = fm.horizontalAdvance(base_running + "...") + 30
                btn.setMinimumWidth(max(btn.width(), reserved))
            except Exception:
                pass
            t = QTimer(self)
            t.setInterval(400)
            t.timeout.connect(_tick)
            t.start()
            anim["timer"] = t

        def _stop_animation():
            t = anim["timer"]
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
                anim["timer"] = None
            try:
                btn.setMinimumWidth(0)
            except Exception:
                pass

        # ── Cleanup общий ──────────────────────────────────────────
        def _restore_idle():
            try:
                btn.setText(tr('compile_ep_btn'))
                btn.setEnabled(True)
            except Exception:
                pass

        # ── Handlers сигналов ──────────────────────────────────────
        def _on_finished(zip_path: str):
            _stop_animation()
            self._compile_thread = None
            # Открыть папку <ep_id>/ в файловом менеджере (юзер увидит
            # zip внутри). Pattern из _on_block_refs_btn.
            try:
                folder = str(Path(zip_path).parent)
                if sys.platform == "darwin":
                    subprocess.run(
                        ["open", folder], **no_console_kwargs())
                elif sys.platform == "win32":
                    subprocess.run(
                        ["explorer", folder], **no_console_kwargs())
                else:
                    subprocess.run(
                        ["xdg-open", folder], **no_console_kwargs())
            except Exception:
                traceback.print_exc()
            # Возврат в покой через 500мс.
            QTimer.singleShot(500, _restore_idle)

        def _on_error(msg: str):
            _stop_animation()
            self._compile_thread = None
            if msg == "empty_episode":
                try:
                    QMessageBox.information(
                        self, tr('compile_ep_btn'),
                        tr('compile_ep_empty'))
                except Exception:
                    pass
                _restore_idle()
                return
            try:
                btn.setText(tr('compile_ep_btn_error'))
                btn.setEnabled(False)
            except Exception:
                pass
            # Через 3 сек обратно в покой.
            QTimer.singleShot(3000, _restore_idle)

        # ── Запуск ─────────────────────────────────────────────────
        try:
            btn.setEnabled(False)
            _start_animation()
            thread = CompileEpisodeThread(
                self._project_root, self._current_show,
                self._current_episode, parent=self)
            thread.finished.connect(_on_finished)
            thread.error.connect(_on_error)
            self._compile_thread = thread
            thread.start()
        except Exception as e:
            _stop_animation()
            self._compile_thread = None
            traceback.print_exc()
            try:
                QMessageBox.warning(
                    self, tr('compile_ep_btn'),
                    f"{type(e).__name__}: {e}")
            except Exception:
                pass
            _restore_idle()

    def _on_seedance_btn(self):
        """Клик по кнопке «🎬 Промпт Seedance» на текущем блоке.
        Роутинг по `_seedance_btn_mode`:
          • 'ready'   → открыть попап с готовым текстом промпта;
          • 'restart' → перезапустить зависший pipeline (v1.0.85);
          • 'pending' → инфо-попап «генерируется…».
        """
        if not self.current_block:
            return
        m = re.match(r'(ep\d+)_block_(\d+)', self.current_block)
        if not m:
            return
        ep_id = m.group(1)
        block_n = int(m.group(2))

        # v1.0.85: restart-режим — отдельная ветка ДО проверки файла.
        # Файла нет потому что pipeline завис; перезапускаем.
        mode = getattr(self, '_seedance_btn_mode', 'pending')
        if mode == 'restart':
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None and hasattr(ev, '_on_seedance_restart'):
                ev._on_seedance_restart(ep_id)
            # После рестарта state-машина переоценится при следующем тике.
            return

        # 2026-05-18 (UI tabs): попап читает state через
        # _load_seedance_tabs (учитывает _tabs.json + читает _tab<K>.txt
        # файлы вкладок). Здесь же даём ему fallback-текст из
        # `<ep>_block_<N>.txt` для случая когда _tabs.json нет —
        # тогда создастся одна вкладка «оригинал».
        # Pending check работает по оригиналу: если .txt отсутствует И
        # _tabs.json тоже отсутствует — промпт ещё генерится.
        # ep_id уже распарсен из self.current_block выше regex'ом —
        # используем его (а не self._current_episode) чтобы исключить
        # рассинхрон если current_episode отстал от current_block.
        seedance_path = SEEDANCE_DIR / f"{self.current_block}.txt"
        tabs_json_path = _seedance_tabs_path(SEEDANCE_DIR, ep_id, block_n)
        has_txt = seedance_path.exists() and seedance_path.stat().st_size > 0
        has_json = tabs_json_path.exists()
        if not has_txt and not has_json:
            QMessageBox.information(
                self, tr('seedance_popup_title', block_n=block_n),
                tr('seedance_popup_pending'))
            return
        text = ""
        if has_txt:
            try:
                text = seedance_path.read_text(encoding='utf-8')
            except Exception as e:
                # Если только _tabs.json — это ОК (попап прочитает
                # сам по записям JSON). Если и .txt нечитаем и .json нет
                # — реальная ошибка.
                if not has_json:
                    QMessageBox.warning(
                        self, tr('seedance_popup_title', block_n=block_n),
                        tr('seedance_popup_failed', msg=str(e)[:200]))
                    return
                text = ""
        self._show_seedance_popup(block_n, ep_id, text)

    def _refresh_seedance_btn_state(self):
        """v1.0.85: периодически (раз в 30с) переоценивает state seedance_btn.

        Нужно потому что «Готовится → Перезапустить» — переход по
        времени (>5 мин), не по событию. Без таймера лейбл не сменится
        пока юзер не кликнет по другому блоку и обратно.

        Лёгкая операция — просто перерисовать карточки текущего блока
        если current_block принадлежит реальному эпизоду.
        """
        if not getattr(self, 'current_block', None):
            return
        if not hasattr(self, 'seedance_btn') or not self.seedance_btn.isVisible():
            return
        try:
            # Просто перерисовываем — `_render_shot_cards_for_current_block`
            # (или аналогичный) пересчитает mode через `_compute_seedance_btn_mode`.
            # Если такого метода нет — пересчитываем здесь напрямую.
            m = re.match(r'(ep\d+)_block_(\d+)', self.current_block)
            if not m:
                return
            name = self.current_block
            seedance_path = SEEDANCE_DIR / f"{name}.txt"
            ready = seedance_path.exists() and seedance_path.stat().st_size > 0
            mode = self._compute_seedance_btn_mode(ready)
            if mode == getattr(self, '_seedance_btn_mode', None):
                return  # state не изменился — экономим
            self._seedance_btn_mode = mode
            if mode == 'ready':
                self.seedance_btn.setText(tr('seedance_btn'))
                self.seedance_btn.setToolTip("")
            elif mode == 'restart':
                self.seedance_btn.setText(tr('seedance_btn_restart'))
                self.seedance_btn.setToolTip(tr('seedance_btn_restart_tip'))
            else:
                self.seedance_btn.setText(tr('seedance_btn_pending'))
                self.seedance_btn.setToolTip("")
        except Exception:
            traceback.print_exc()

    def _show_seedance_popup(self, block_n: int, ep_id: str, text: str):
        """Попап с QTabWidget версий Seedance промпта блока.

        Архитектура (2026-05-18 — UI tabs):
          • Каждая вкладка — отдельный файл `_tab<K>.txt`.
          • Вкладка 1 (без крестика) = оригинал `<ep>_block_<N>.txt`,
            никогда не перезаписывается через regen.
          • Вкладки 2+ создаются кнопками «Изменить» / «Сократить» —
            пишутся в `<ep>_block_<N>_tab<K>.txt` (K = monotonic).
          • Состояние (порядок, active_idx, next_idx) хранится в
            `<ep>_block_<N>_tabs.json` — переживает перезапуск Studio.

        Args:
          block_n: номер блока (1-based, например 5 → ep16_block_5).
          ep_id:   идентификатор эпизода `ep<N>` — распарсен caller'ом
                   из `self.current_block` чтобы исключить рассинхрон
                   с `self._current_episode`.
          text:    fallback-содержимое оригинала для создания «Вкладка 1»
                   если `_tabs.json` отсутствует.
        """
        from PyQt6.QtWidgets import QSpinBox as _QSB, QTabWidget as _QTW, QTabBar as _QTB
        from PyQt6.QtGui import QColor as _QColor

        tabs_path = _seedance_tabs_path(SEEDANCE_DIR, ep_id, block_n)
        loaded_tabs, loaded_active, next_idx_box, save_needed = \
            _load_seedance_tabs(
                tabs_path, SEEDANCE_DIR, ep_id, block_n, fallback_text=text)

        # Mutable boxes для closures (next_idx — счётчик, нужно менять)
        next_idx_holder = [int(next_idx_box)]
        # tabs_state — синхронизирован с QTabWidget. Каждая запись:
        # {"title": str, "file": str, "source": str, "text": str}
        tabs_state: list = list(loaded_tabs)

        dlg = QDialog(self)
        dlg.setWindowTitle(tr('seedance_popup_title', block_n=block_n))
        dlg.setMinimumSize(780, 720)
        dlg.setStyleSheet(theme_qss("""
            QDialog { background:#121313; color:#ffffff; }
            QLabel { color:#aaa; }
            QSpinBox {
                background:#191b1d; color:#dddddf; border:1px solid #1d1e20;
                border-radius:4px; padding:3px 6px; font-size:12px;
            }
        """))
        v = QVBoxLayout(dlg)
        v.setSpacing(10)
        v.setContentsMargins(20, 16, 20, 16)

        # ── QTabWidget с вкладками промптов ──
        tabs_widget = _QTW()
        tabs_widget.setTabsClosable(True)
        tabs_widget.setMovable(False)  # порядок стабилен (idx=0 всегда оригинал)
        # БАГ 2 — на macOS Qt по умолчанию рендерит close-button слева (Apple
        # convention). План А: QSS subcontrol-position: right переводит крестик
        # на правую сторону вкладки. Если на macOS не сработает (Qt может
        # игнорировать subcontrol-position для close-button) — см. план Б ниже
        # в _disable_tab_close (закомментирован _ensure_close_btn_right).
        tabs_widget.setStyleSheet(
            "QTabWidget::pane { border:none; background:transparent; }"
            "QTabBar::tab { background:transparent; color:rgba(255,255,255,0.58); "
            "padding:9px 16px; border:none; border-radius:8px; margin-right:6px; }"
            "QTabBar::tab:selected { background:#242628; color:#e8b86a; }"
            "QTabBar::tab:hover:!selected { background:#191b1d; color:#f2f3f0; }"
            "QTabBar::close-button { subcontrol-position: right; }")
        v.addWidget(tabs_widget, stretch=1)

        # ── Счётчик длины активной вкладки ──
        length_label = QLabel("")
        length_label.setStyleSheet("color:#7cc97c; font-size:11px;")
        v.addWidget(length_label)

        # ── Лейбл + textarea инструкции «что переделать» ──
        instr_lbl = QLabel(tr('seedance_popup_instruction_label'))
        instr_lbl.setStyleSheet("color:#aaa; font-size:12px; font-weight:500;")
        v.addWidget(instr_lbl)

        instr_ta = QPlainTextEdit()
        instr_ta.setPlaceholderText(tr('seedance_popup_instruction_placeholder'))
        instr_ta.setMaximumHeight(80)
        instr_ta.setStyleSheet(
            "QPlainTextEdit { background:#191b1d; border:1px solid #1d1e20; "
            "border-radius:6px; color:#dddddf; padding:8px; font-size:12px; }")
        v.addWidget(instr_ta)

        regen_status = QLabel("")
        regen_status.setStyleSheet("color:#ff8a8a; font-size:11px;")
        regen_status.setWordWrap(True)
        regen_status.setVisible(False)
        v.addWidget(regen_status)

        # ── Кнопки: regen / copy / close ──
        btns_row = QHBoxLayout()
        btns_row.setSpacing(8)
        seedance_action_qss = (
            "QPushButton { background:#242628; color:#f2f3f0; "
            "border:1px solid rgba(255,255,255,0.10); border-radius:8px; "
            "padding:0 10px; font-size:13px; font-weight:600; }"
            "QPushButton:hover { background:#2c2f31; "
            "border-color:rgba(255,255,255,0.16); color:#ffffff; }"
            "QPushButton:pressed { background:#1f2123; border-color:#1f2123; }"
            "QPushButton:disabled { background:#242628; border-color:#242628; "
            "color:rgba(242,244,239,0.35); }")

        def _style_seedance_action_button(btn: QPushButton) -> None:
            btn.setFixedSize(176, 40)
            btn.setStyleSheet(seedance_action_qss)

        regen_btn = QPushButton(tr('seedance_popup_regen'))
        regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _style_seedance_action_button(regen_btn)
        copy_btn = QPushButton(tr('seedance_popup_copy'))
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _style_seedance_action_button(copy_btn)
        # 2026-05-19: «💾 Сохранить» — пишет текст активной вкладки в
        # shows/<show>/.cache/_block_view/<ep_id>_block<N>/<show>_<ep_id>_block_<N>.txt
        # (ту же папку открывает кнопка «🗂 Рефы блока» на странице эпизода).
        # Стиль наследует от copy_btn — единая action-кнопка.
        save_btn = QPushButton(tr('seedance_popup_save'))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _style_seedance_action_button(save_btn)
        close_btn = QPushButton(tr('seedance_popup_close'))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _style_seedance_action_button(close_btn)

        # ── Compress row: target/limit/compress ──
        try:
            _qs_compress = QSettings(APP_ORG, APP_NAME)
            try:
                cur_target = int(_qs_compress.value(
                    "seedance_compress/target_chars", 3700))
            except (TypeError, ValueError):
                cur_target = 3700
            try:
                cur_limit = int(_qs_compress.value(
                    "seedance_compress/limit_chars", 4000))
            except (TypeError, ValueError):
                cur_limit = 4000
        except Exception:
            cur_target, cur_limit = 3700, 4000

        compress_row = QHBoxLayout()
        compress_row.setSpacing(8)
        target_lbl = QLabel(tr('compress_target_label'))
        target_lbl.setStyleSheet("color:#aaa; font-size:12px;")
        compress_row.addWidget(target_lbl)
        target_spin = _QSB()
        target_spin.setRange(500, 15000)
        target_spin.setSingleStep(100)
        target_spin.setValue(cur_target)
        target_spin.setStyleSheet(
            "QSpinBox { background:#191b1d; color:#ddd; border:1px solid "
            "#1d1e20; border-radius:4px; padding:3px 6px; font-size:12px; "
            "min-width: 80px; }")
        block_wheel_event(target_spin)
        compress_row.addWidget(target_spin)

        limit_lbl = QLabel(tr('compress_limit_label'))
        limit_lbl.setStyleSheet("color:#aaa; font-size:12px;")
        compress_row.addWidget(limit_lbl)
        limit_spin = _QSB()
        limit_spin.setRange(500, 15000)
        limit_spin.setSingleStep(100)
        limit_spin.setValue(cur_limit)
        limit_spin.setStyleSheet(target_spin.styleSheet())
        block_wheel_event(limit_spin)
        compress_row.addWidget(limit_spin)

        compress_btn = QPushButton(tr('compress_btn'))
        compress_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _style_seedance_action_button(compress_btn)
        compress_row.addWidget(compress_btn)
        compress_row.addStretch()

        # ── Helper closures ──
        TAB_TEXTAREA_QSS = (
            "QPlainTextEdit { background:#191b1d; border:none; "
            "color:#ddddde; padding:10px; font-size:12px; "
            "font-family: 'Menlo','Consolas',monospace; }")

        def _build_textarea(text_val: str) -> QPlainTextEdit:
            t = QPlainTextEdit()
            t.setPlainText(text_val or "")
            t.setReadOnly(True)
            t.setStyleSheet(TAB_TEXTAREA_QSS)
            return t

        def _format_tab_title(pos_idx: int, text_len: int) -> str:
            """Заголовок «Вкладка N (X)» где N — позиция в QTabWidget,
            X — текущая длина текста этой вкладки."""
            return tr('seedance_tab_label_format',
                       n=pos_idx + 1, chars=text_len)

        def _apply_tab_color(pos_idx: int, text_len: int) -> None:
            """Красит заголовок зелёным/красным по лимиту."""
            lim = int(limit_spin.value())
            try:
                bar = tabs_widget.tabBar()
                color = (theme_qcolor("#7cc97c") if text_len <= lim
                         else theme_qcolor("#ff8a8a"))
                bar.setTabTextColor(pos_idx, color)
            except Exception:
                pass

        def _refresh_titles() -> None:
            """Пересчитывает все заголовки + цвета (после смены limit /
            добавления/удаления вкладки)."""
            for i, st in enumerate(tabs_state):
                ln = len(st.get("text") or "")
                tabs_widget.setTabText(i, _format_tab_title(i, ln))
                _apply_tab_color(i, ln)
                # Также синхронизируем title в state (для save)
                st["title"] = f"Вкладка {i + 1}"

        def _disable_tab_close(pos_idx: int) -> None:
            """Убирает крестик у заданной вкладки. Используется для idx=0
            (оригинал — нельзя закрыть). Убираем с обеих сторон: macOS Qt
            кладёт close-button на LeftSide, Win/Linux — на RightSide."""
            try:
                bar = tabs_widget.tabBar()
                bar.setTabButton(pos_idx,
                                 _QTB.ButtonPosition.RightSide, None)
                bar.setTabButton(pos_idx,
                                 _QTB.ButtonPosition.LeftSide, None)
            except Exception:
                pass

        # ── ПЛАН Б для БАГ 2 (раскомментировать если QSS subcontrol-position
        #    выше не сработал на macOS — Qt иногда игнорирует это правило).
        #    Также нужно добавить QToolButton в импорт PyQt6.QtWidgets вверху
        #    функции _show_seedance_popup. Вызывать _ensure_close_btn_right(i)
        #    в _add_new_tab сразу после tabs_widget.addTab(...) для idx > 0. ──
        # def _ensure_close_btn_right(pos_idx: int) -> None:
        #     """Кастомный крестик ✕ на RightSide, дефолтный с LeftSide убирается."""
        #     try:
        #         bar = tabs_widget.tabBar()
        #         bar.setTabButton(pos_idx, _QTB.ButtonPosition.LeftSide, None)
        #         btn = QToolButton()
        #         btn.setText("✕")
        #         btn.setCursor(Qt.CursorShape.PointingHandCursor)
        #         btn.setStyleSheet(
        #             "QToolButton { background:transparent; color:#888; "
        #             "border:none; padding:0 4px; font-size:12px; }"
        #             "QToolButton:hover { color:#d8c8ff; }")
        #         btn.clicked.connect(lambda _ck=False, _i=pos_idx: _on_tab_close(_i))
        #         bar.setTabButton(pos_idx, _QTB.ButtonPosition.RightSide, btn)
        #     except Exception:
        #         pass

        def _active_text() -> str:
            i = tabs_widget.currentIndex()
            if 0 <= i < len(tabs_state):
                return tabs_state[i].get("text") or ""
            return ""

        def _update_length_label() -> None:
            ln = len(_active_text())
            lim = int(limit_spin.value())
            color = "#7cc97c" if ln <= lim else "#ff8a8a"
            length_label.setText(tr('compress_length_label', n=ln))
            length_label.setStyleSheet(f"color:{color}; font-size:11px;")

        def _persist() -> None:
            _save_seedance_tabs(
                tabs_path, tabs_state,
                active_idx=tabs_widget.currentIndex(),
                next_idx=next_idx_holder[0])

        def _build_initial_tabs() -> None:
            for i, st in enumerate(tabs_state):
                ta_w = _build_textarea(st.get("text") or "")
                tabs_widget.addTab(ta_w, "")
            _refresh_titles()
            # Idx 0 — без крестика
            _disable_tab_close(0)
            # Активная вкладка
            ai = min(max(0, loaded_active), len(tabs_state) - 1)
            tabs_widget.setCurrentIndex(ai)

        def _add_new_tab(new_text: str, source: str) -> None:
            k = int(next_idx_holder[0])
            new_file = f"{ep_id}_block_{block_n}_tab{k}.txt"
            new_path = SEEDANCE_DIR / new_file
            try:
                SEEDANCE_DIR.mkdir(parents=True, exist_ok=True)
                new_path.write_text(new_text, encoding="utf-8")
            except Exception as e:
                sys.stderr.write(
                    f"[seedance_tabs] failed to write {new_file}: "
                    f"{type(e).__name__}: {e}\n")
                return
            next_idx_holder[0] = k + 1
            st_entry = {
                "title": f"Вкладка {len(tabs_state) + 1}",
                "file": new_file,
                "source": source,
                "text": new_text,
            }
            tabs_state.append(st_entry)
            ta_w = _build_textarea(new_text)
            tabs_widget.addTab(ta_w, "")
            _refresh_titles()
            tabs_widget.setCurrentIndex(len(tabs_state) - 1)
            _persist()

        def _on_tab_close(pos_idx: int) -> None:
            if pos_idx == 0:
                return  # защита — оригинал нельзя закрыть
            if pos_idx < 0 or pos_idx >= len(tabs_state):
                return
            # Удалить файл с диска
            try:
                fname = tabs_state[pos_idx].get("file") or ""
                if fname:
                    fpath = SEEDANCE_DIR / fname
                    if fpath.exists():
                        fpath.unlink()
            except Exception as e:
                sys.stderr.write(
                    f"[seedance_tabs] failed to unlink: "
                    f"{type(e).__name__}: {e}\n")
            del tabs_state[pos_idx]
            tabs_widget.removeTab(pos_idx)
            _refresh_titles()
            _persist()

        def _on_tab_changed(new_idx: int) -> None:
            _update_length_label()
            # БАГ 4 — copy_btn общий для всех вкладок; после копирования
            # её текст становится «✓ Скопировано» и оставался таким на всех
            # вкладках. Сбрасываем при каждом переключении.
            copy_btn.setText(tr('seedance_popup_copy'))
            # save_btn — та же логика что у copy_btn: общая на все вкладки,
            # после «✓ Сохранено» возвращаем дефолтный текст при переключении.
            save_btn.setText(tr('seedance_popup_save'))
            _update_compress_btn_state()
            _persist()

        # ── Smart enabled state regen_btn ──
        def _on_instr_text_changed() -> None:
            has = bool(instr_ta.toPlainText().strip())
            if not has:
                regen_btn.setEnabled(False)
                regen_btn.setToolTip(tr('seedance_regen_disabled_tooltip'))
            else:
                regen_btn.setEnabled(True)
                regen_btn.setToolTip("")

        def _update_compress_btn_state() -> None:
            """БАГ 7 — disabled когда текущая длина уже ≤ limit_chars.
            Гард во время операции (анимация активна) — не трогаем
            enabled-state, им управляет _lock_ui / _unlock_ui."""
            if _anim["timer"] is not None:
                return
            ln = len(_active_text())
            lim = int(limit_spin.value())
            if ln <= lim:
                compress_btn.setEnabled(False)
                compress_btn.setToolTip(
                    tr('seedance_compress_disabled_tooltip'))
            else:
                compress_btn.setEnabled(True)
                compress_btn.setToolTip("")

        # ── БАГ 5 — анимация бегущих точек на «Перегенерирую/Сжимаю» ──
        # mutable dict в closure (чтобы не нужен nonlocal в inner-функциях).
        # parent=dlg у QTimer → авто-cleanup при закрытии попапа.
        _anim = {"timer": None, "btn": None, "base": "", "tick": 0}

        def _tick_anim() -> None:
            _anim["tick"] = (_anim["tick"] + 1) % 3
            dots = "." * (_anim["tick"] + 1)
            btn = _anim["btn"]
            if btn is not None:
                try:
                    btn.setText(f"{_anim['base']}{dots}")
                except Exception:
                    pass

        def _start_btn_animation(btn: QPushButton, base_text: str) -> None:
            _anim["btn"] = btn
            _anim["base"] = base_text
            _anim["tick"] = 0
            btn.setText(f"{base_text}.")  # сразу 1 точка, без задержки
            t = QTimer(dlg)
            t.setInterval(400)
            t.timeout.connect(_tick_anim)
            t.start()
            _anim["timer"] = t

        def _stop_btn_animation() -> None:
            t = _anim["timer"]
            btn = _anim["btn"]  # запоминаем до очистки state
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
            if btn is not None:
                try:
                    btn.setFixedSize(176, 40)
                except Exception:
                    pass
            _anim["timer"] = None
            _anim["btn"] = None
            _anim["base"] = ""
            _anim["tick"] = 0

        def _lock_ui(active_btn: QPushButton, active_text: str) -> None:
            active_btn.setEnabled(False)
            _start_btn_animation(active_btn, active_text)
            target_spin.setEnabled(False)
            limit_spin.setEnabled(False)
            tabs_widget.tabBar().setEnabled(False)
            instr_ta.setEnabled(False)
            # Вторая кнопка тоже disabled пока работает первая
            if active_btn is regen_btn:
                compress_btn.setEnabled(False)
            else:
                regen_btn.setEnabled(False)
            regen_status.setVisible(False)
            regen_status.setText("")
            copy_btn.setText(tr('seedance_popup_copy'))

        def _unlock_ui() -> None:
            _stop_btn_animation()
            regen_btn.setText(tr('seedance_popup_regen'))
            compress_btn.setText(tr('compress_btn'))
            target_spin.setEnabled(True)
            limit_spin.setEnabled(True)
            tabs_widget.tabBar().setEnabled(True)
            instr_ta.setEnabled(True)
            compress_btn.setEnabled(True)
            _on_instr_text_changed()  # пересчёт enabled для regen_btn
            _update_compress_btn_state()  # БАГ 7 — пересчёт для compress_btn

        # ── Handlers ──
        def _do_copy():
            try:
                QApplication.clipboard().setText(_active_text())
                copy_btn.setText(tr('seedance_popup_copied'))
            except Exception:
                pass

        def _do_save():
            """2026-05-19: пишет текст активной вкладки на диск в папку
            рефов блока (которую открывает кнопка «🗂 Рефы блока»).
            Путь: shows/<show>/.cache/_block_view/<ep_id>_block<N>/.
            Имя файла: <show_slug>_<ep_id>_block_<N>.txt
            (с подчёркиванием перед N — отличается от имени папки
            «<ep_id>_block<N>» где подчёркивания нет).
            Перезаписывает существующий файл молча."""
            try:
                text = _active_text()
                if not text:
                    return
                show_slug = self._current_show or ""
                if not show_slug:
                    return
                dest_dir = (SHOW_ROOT / ".cache" / "_block_view"
                            / f"{ep_id}_block{block_n}")
                dest_dir.mkdir(parents=True, exist_ok=True)
                fname = f"{show_slug}_{ep_id}_block_{block_n}.txt"
                (dest_dir / fname).write_text(text, encoding="utf-8")
                save_btn.setText(tr('seedance_popup_saved'))
            except Exception:
                traceback.print_exc()

        def _do_regen():
            instruction = instr_ta.toPlainText().strip()
            try:
                self._start_seedance_regen(
                    block_n=block_n,
                    previous_prompt=_active_text(),
                    user_instruction=instruction,
                    on_done=lambda new_text: _on_regen_success(new_text),
                    on_failed=lambda msg: _on_regen_error(msg),
                )
                _lock_ui(regen_btn, tr('seedance_popup_regenerating'))
            except Exception as e:
                _on_regen_error(str(e)[:200])

        def _on_regen_success(new_text: str):
            _add_new_tab(new_text, source="regen")
            instr_ta.clear()
            _unlock_ui()

        def _on_regen_error(msg: str):
            _unlock_ui()
            regen_status.setText(tr('seedance_popup_regen_failed', msg=msg))
            regen_status.setVisible(True)

        def _persist_compress_settings():
            try:
                _qs = QSettings(APP_ORG, APP_NAME)
                _qs.setValue(
                    "seedance_compress/target_chars", int(target_spin.value()))
                _qs.setValue(
                    "seedance_compress/limit_chars", int(limit_spin.value()))
            except Exception:
                pass

        def _do_compress():
            target_chars = int(target_spin.value())
            limit_chars = int(limit_spin.value())
            _persist_compress_settings()
            try:
                self._start_seedance_compress(
                    current_prompt=_active_text(),
                    target_chars=target_chars,
                    limit_chars=limit_chars,
                    on_done=lambda new_text: _on_compress_success(new_text),
                    on_failed=lambda msg: _on_compress_error(msg),
                    ep_id=ep_id,
                    block_n=block_n,
                )
                _lock_ui(compress_btn, tr('compressing'))
            except Exception as e:
                _on_compress_error(str(e)[:200])

        def _on_compress_success(new_text: str):
            _add_new_tab(new_text, source="compress")
            _unlock_ui()

        def _on_compress_error(msg: str):
            _unlock_ui()
            regen_status.setText(tr('compress_failed', msg=msg))
            regen_status.setVisible(True)

        # ── Wire up ──
        _build_initial_tabs()
        _update_length_label()
        if save_needed:
            _persist()  # auto-clean stale entries / создание дефолтного JSON

        regen_btn.clicked.connect(_do_regen)
        copy_btn.clicked.connect(_do_copy)
        save_btn.clicked.connect(_do_save)
        close_btn.clicked.connect(dlg.accept)
        compress_btn.clicked.connect(_do_compress)
        tabs_widget.tabCloseRequested.connect(_on_tab_close)
        tabs_widget.currentChanged.connect(_on_tab_changed)
        instr_ta.textChanged.connect(_on_instr_text_changed)
        limit_spin.valueChanged.connect(
            lambda _v: (_refresh_titles(), _update_length_label(),
                        _update_compress_btn_state()))
        target_spin.valueChanged.connect(
            lambda _v: _update_compress_btn_state())
        # Финальная страховка — при закрытии попапа дозаписать state
        dlg.finished.connect(lambda _r: _persist())

        # Initial enabled state regen_btn (нет фидбэка → disabled)
        _on_instr_text_changed()
        _update_compress_btn_state()

        # 2026-05-26: re-attach живого compress-потока этого блока.
        # При повторном открытии попапа после закрытия крестиком (когда
        # сжатие ещё идёт): ищем в self._seedance_compress_threads
        # активный поток с теми же (ep_id, block_n). Если есть —
        # отрезаем СТАРЫЕ done/failed слоты (они держат closures
        # закрытого диалога — _on_compress_success там обратится к
        # уже невидимым виджетам и затрёт tabs.json устаревшим state),
        # подключаем новые callbacks + _resub_cleanup (он был утрачен
        # disconnect()'ом), включаем _lock_ui для анимации «Сжимаю...».
        # Если живого потока нет — окно открывается как обычно.
        try:
            existing_threads = getattr(
                self, '_seedance_compress_threads', None) or []
            live_thread = None
            for _t in existing_threads:
                try:
                    if (_t.isRunning()
                            and getattr(_t, '_ep_id', None) == ep_id
                            and getattr(_t, '_block_n', None) == block_n):
                        live_thread = _t
                        break
                except Exception:
                    continue
            if live_thread is not None:
                # Снять все прежние слоты с обоих сигналов. disconnect()
                # без аргументов сбрасывает все коннекты сразу. Если
                # слотов уже нет — Qt бросает TypeError/RuntimeError,
                # игнорируем.
                try:
                    live_thread.done.disconnect()
                except (TypeError, RuntimeError):
                    pass
                try:
                    live_thread.failed.disconnect()
                except (TypeError, RuntimeError):
                    pass
                # Свежие callbacks этого окна.
                live_thread.done.connect(
                    lambda new_text: _on_compress_success(new_text))
                live_thread.failed.connect(
                    lambda msg: _on_compress_error(msg))
                # _cleanup был потерян на disconnect() — навешиваем
                # эквивалент заново, чтобы поток вычистился из реестра
                # после завершения.
                def _resub_cleanup(_thr=live_thread):
                    try:
                        self._seedance_compress_threads.remove(_thr)
                    except Exception:
                        pass
                live_thread.done.connect(lambda _t: _resub_cleanup())
                live_thread.failed.connect(lambda _m: _resub_cleanup())
                # UI: показать «Сжимаю...» с бегущими точками — ровно
                # как сразу после нажатия «Сократить».
                # 2026-05-26: отложено через QTimer(0) — иначе _lock_ui
                # → _start_btn_animation фиксирует compress_btn.width()
                # ДО прохода Qt layout'а (на момент сборки окна ширина
                # аномально большая), и setMinimumWidth растягивает
                # кнопку на всю строку. После layout'а width() уже
                # корректный — кнопка остаётся компактной.
                QTimer.singleShot(
                    0, lambda: _lock_ui(compress_btn, tr('compressing')))
        except Exception:
            # Любая ошибка re-attach не должна мешать открытию окна.
            traceback.print_exc()

        btns_row.addWidget(regen_btn)
        btns_row.addWidget(copy_btn)
        btns_row.addWidget(save_btn)
        btns_row.addStretch()
        btns_row.addWidget(close_btn)
        v.addLayout(btns_row)
        v.addLayout(compress_row)

        dlg.exec()

    def _start_seedance_regen(self, block_n: int,
                                  previous_prompt: str,
                                  user_instruction: str,
                                  on_done,
                                  on_failed):
        """Запускает `SeedanceRegenThread` — переписывает один Seedance
        промпт блока на основе предыдущего текста и фидбэка автора.

        Контекст:
          • Полная карта (с geometry) — из `output/_agent_log_<ep>.json`,
            берём последний stage где `result.blocks` непустой.
          • refs_summary — из `episodes.json[ep].refs_decisions` (по той
            же логике что у `_build_refs_summary_for_orchestrator`).
          • Bible — `shows/<slug>/bible.txt`.
          • Голосовые профили — `shows/<slug>/voices.txt` активного
            сериала (per-show).
          • claude CLI — через глобальный `find_claude_cli()`.
          • Модель — Opus 4.7 (как и массовый Seedance pipeline).

        Сигналы прокидываются в попап через callbacks `on_done(text)` и
        `on_failed(msg)` — попап обновляет UI без знания деталей thread'а.
        """
        ep_id = self._current_episode
        if not ep_id:
            on_failed("no current episode")
            return
        if not self._current_show:
            on_failed("no current show")
            return

        # 1. Полная карта монтажа из agent_log (там есть geometry)
        agent_log_path = SHOW_ROOT / "output" / f"_agent_log_{ep_id}.json"
        if not agent_log_path.exists():
            on_failed(
                "Не найден _agent_log файл. Сделай монтажную карту заново.")
            return
        try:
            agent_log = json.loads(agent_log_path.read_text(encoding="utf-8"))
        except Exception as e:
            on_failed(f"_agent_log parse: {e}")
            return
        # Берём последний stage с blocks в result — это самая свежая
        # утверждённая версия карты (может быть либо scriptwriter если
        # validator прошёл с первого раза, либо editor если были фиксы).
        montage_card = None
        for stage in reversed(agent_log.get("stages", []) or []):
            res = stage.get("result") or {}
            if isinstance(res, dict) and res.get("blocks"):
                montage_card = res
                break
        if not montage_card:
            on_failed("в _agent_log нет blocks ни в одном stage")
            return

        # 2. refs_summary из refs_decisions (как _build_refs_summary_for_orchestrator)
        refs_summary: dict = {"locations": [], "objects": [], "characters": []}
        characters_dict: Dict[str, str] = {}
        try:
            ep_meta = read_episodes_meta(SHOW_ROOT)
            ep_data = (ep_meta.get(ep_id) or {}) if isinstance(ep_meta, dict) else {}
            decisions = ep_data.get("refs_decisions") or {}
            for kind, key in (("location", "locations"),
                              ("object", "objects"),
                              ("character", "characters")):
                bucket = decisions.get(kind) or {}
                if not isinstance(bucket, dict):
                    continue
                for slug, d in bucket.items():
                    if not isinstance(d, dict):
                        continue
                    if d.get("decision") != "linked":
                        continue
                    fn = (d.get("filename") or "").replace("\\", "/").split("/")[-1]
                    refs_summary[key].append({"slug": slug, "filename": fn})
                    if kind == "character":
                        characters_dict[slug] = slug.capitalize()
        except Exception as e:
            on_failed(f"refs build: {e}")
            return

        # 3. Bible сериала
        bible_text = ""
        try:
            bible_path = SHOW_ROOT / "bible.txt"
            if bible_path.exists():
                bible_text = bible_path.read_text(encoding="utf-8")
        except Exception:
            bible_text = ""

        # 4. Голосовые профили — из корня проекта (как в episode_chat)
        voices_text = ""
        try:
            voices_path = (self._project_root / "shows"
                           / self._current_show / "voices.txt")
            if voices_path.exists():
                voices_text = voices_path.read_text(encoding="utf-8")
        except Exception:
            voices_text = ""

        # 5. claude CLI + Opus 4.7 (Seedance ВСЕГДА на Opus, как в pipeline)
        cli = find_claude_cli()
        if not cli:
            on_failed("claude CLI not found")
            return

        from threads.seedance_pipeline import SeedanceRegenThread
        thread = SeedanceRegenThread(
            claude_cli_path=cli,
            montage_card=montage_card,
            refs_summary=refs_summary,
            characters_dict=characters_dict,
            ep_id=ep_id,
            block_n=block_n,
            seedance_dir=SEEDANCE_DIR,
            previous_prompt=previous_prompt,
            user_instruction=user_instruction,
            bible_text=bible_text,
            voice_profiles_text=voices_text,
            storyboard_prompts_dir=PROMPTS_DIR,
            model="claude-opus-4-7",
            parent=self,
        )
        thread.done.connect(on_done)
        thread.failed.connect(on_failed)
        # Хранить ссылку чтобы тред не сборщил мусор пока работает
        if not hasattr(self, '_seedance_regen_threads'):
            self._seedance_regen_threads = []
        self._seedance_regen_threads.append(thread)

        def _cleanup():
            try:
                self._seedance_regen_threads.remove(thread)
            except Exception:
                pass

        thread.done.connect(lambda _t: _cleanup())
        thread.failed.connect(lambda _m: _cleanup())
        thread.start()

    def _start_seedance_compress(self, current_prompt: str,
                                   target_chars: int,
                                   limit_chars: int,
                                   on_done,
                                   on_failed,
                                   ep_id: Optional[str] = None,
                                   block_n: Optional[int] = None):
        """Запускает `SeedanceCompressThread` — сжимает Seedance промпт
        в коридор [target_chars..limit_chars] через Opus 4.7 + --effort low.
        Если current_prompt уже ≤ limit_chars — Opus вернёт текст 1:1
        (правило коридора в SYSTEM_COMPRESS).

        В отличие от `_start_seedance_regen` не требует montage_card /
        refs / Bible / voice_profiles — Compressor работает только с
        текущим текстом промпта (всё необходимое уже внутри).

        Файл `output/seedance/<ep>_block_N.txt` НЕ перезаписывается —
        результат возвращается в попап через callback, юзер сам решит
        копировать или ещё раз сжимать.

        2026-05-26: ep_id/block_n — опциональные метки, прокидываются в
        thread (атрибуты `_ep_id` / `_block_n`). Используются
        `_show_seedance_popup` чтобы при повторном открытии попапа найти
        живой compress-поток ИМЕННО этого блока и переподключить его на
        новое окно.
        """
        cli = find_claude_cli()
        if not cli:
            on_failed("claude CLI not found")
            return
        from threads.seedance_pipeline import SeedanceCompressThread
        thread = SeedanceCompressThread(
            claude_cli_path=cli,
            current_prompt=current_prompt,
            target_chars=int(target_chars),
            limit_chars=int(limit_chars),
            model="claude-opus-4-7",
            parent=self,
            ep_id=ep_id,
            block_n=block_n,
        )
        thread.done.connect(on_done)
        thread.failed.connect(on_failed)
        # Хранить ссылку чтобы тред не сборщил мусор пока работает
        if not hasattr(self, '_seedance_compress_threads'):
            self._seedance_compress_threads = []
        self._seedance_compress_threads.append(thread)

        def _cleanup():
            try:
                self._seedance_compress_threads.remove(thread)
            except Exception:
                pass

        thread.done.connect(lambda _t: _cleanup())
        thread.failed.connect(lambda _m: _cleanup())
        thread.start()

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _on_apikey_toggle_visibility(self, checked: bool):
        """Toggle Password ↔ Normal в QLineEdit для просмотра ключа."""
        try:
            mode = (QLineEdit.EchoMode.Normal if checked
                    else QLineEdit.EchoMode.Password)
            for _f in getattr(self, '_apikey_fields', [self.apikey_input]):
                _f.setEchoMode(mode)
            self.apikey_show_btn.setText(
                tr('apikey_hide') if checked else tr('apikey_show'))
        except Exception:
            traceback.print_exc()

    def _on_image_provider_actors_changed(self, value: str):
        """Слот сигнала ProviderToggle.valueChanged для «Референсы актёров»
        (виден всем). Сохраняет в QSettings ключ `image_provider_actors`.
        GenerateActorRefThread / EditActorRefThread / RefGenerateThread
        (когда путь содержит /characters/) читают значение на каждый
        запуск — изменение применяется к следующей генерации, перезапуск
        не нужен.
        """
        try:
            set_image_provider_actors(value or IMAGE_PROVIDER_NARWHAL)
        except Exception:
            traceback.print_exc()

    def _on_image_provider_admin_changed(self, value: str):
        """Слот сигнала ProviderToggle.valueChanged для «Сториборды,
        локации, объекты» (виден всем). Сохраняет в QSettings ключ
        `image_provider_admin` + синхронизирует bridge-файл
        `image_provider.txt` для batch-скриптов pipeline.py /
        generate_storyboards.py. GenerateThread читает значение на каждый
        запуск — изменение применяется к следующему шоту, перезапуск
        не нужен.
        """
        try:
            set_image_provider_admin(value or IMAGE_PROVIDER_NARWHAL)
        except Exception:
            traceback.print_exc()

    def _on_anim_speed_changed(self, slider_value: int):
        """Слайдер: 50..250 → множитель 0.5..2.5. Сохраняет в QSettings.
        Применяется мгновенно — следующий fade-переход уже использует новое."""
        try:
            mult = round(slider_value / 100.0, 2)
            QSettings(APP_ORG, APP_NAME).setValue("anim_speed_multiplier", mult)
            self._refresh_anim_speed_value()
        except Exception:
            traceback.print_exc()

    def _on_anim_speed_reset(self):
        """Сброс на дефолт 1.5×."""
        try:
            self.anim_speed_slider.setValue(150)  # триггерит valueChanged
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_fast_changed(self, slider_value: int):
        """Слайдер fast: 30..60 → 3.0..6.0 wps. Сохраняет в QSettings."""
        try:
            wps = round(slider_value / 10.0, 2)
            set_speech_speed_b_fast(wps)
            self._refresh_speech_speed_b_fast_value()
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_fast_reset(self):
        """Сброс fast к дефолту 4.0 (slider value 40)."""
        try:
            self.speech_speed_b_fast_slider.setValue(40)
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_normal_changed(self, slider_value: int):
        """Слайдер normal: 20..50 → 2.0..5.0 wps. Сохраняет в QSettings."""
        try:
            wps = round(slider_value / 10.0, 2)
            set_speech_speed_b_normal(wps)
            self._refresh_speech_speed_b_normal_value()
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_normal_reset(self):
        """Сброс normal к дефолту 3.5 (slider value 35)."""
        try:
            self.speech_speed_b_normal_slider.setValue(35)
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_slow_changed(self, slider_value: int):
        """Слайдер slow: 10..40 → 1.0..4.0 wps. Сохраняет в QSettings."""
        try:
            wps = round(slider_value / 10.0, 2)
            set_speech_speed_b_slow(wps)
            self._refresh_speech_speed_b_slow_value()
        except Exception:
            traceback.print_exc()

    def _on_speech_speed_b_slow_reset(self):
        """Сброс slow к дефолту 2.3 (slider value 23)."""
        try:
            self.speech_speed_b_slow_slider.setValue(23)
        except Exception:
            traceback.print_exc()

    # ── v1.0.65: proxy settings handlers ─────────────────────────────────
    def _on_proxy_checkbox_toggled(self, checked: bool):
        """Чекбокс «Использовать прокси» → enable/disable 4 поля ввода.
        Кнопки «Проверить подключение» и «Сохранить» остаются активны
        всегда (тест direct connection работает даже с выключенным
        чекбоксом).
        """
        try:
            for w in (self.proxy_host_input, self.proxy_port_input,
                      self.proxy_username_input, self.proxy_password_input,
                      self.proxy_password_toggle_btn):
                w.setEnabled(bool(checked))
        except Exception:
            traceback.print_exc()

    def _on_proxy_password_visibility_toggled(self):
        """Глазик 👁/🙈 — переключает EchoMode пароля между Password и Normal."""
        try:
            cur = self.proxy_password_input.echoMode()
            if cur == QLineEdit.EchoMode.Password:
                self.proxy_password_input.setEchoMode(QLineEdit.EchoMode.Normal)
                self.proxy_password_toggle_btn.setText("🙈")
            else:
                self.proxy_password_input.setEchoMode(QLineEdit.EchoMode.Password)
                self.proxy_password_toggle_btn.setText("👁")
        except Exception:
            traceback.print_exc()

    def _on_proxy_test_clicked(self):
        """Запуск ProxyTestThread с текущими значениями из UI (НЕ из QSettings).
        Юзер мог не сохранить — тест должен использовать введённое сейчас."""
        try:
            use_proxy = self.proxy_use_chk.isChecked()
            host = self.proxy_host_input.text().strip()
            port = self.proxy_port_input.text().strip()
            user = self.proxy_username_input.text().strip()
            pwd = self.proxy_password_input.text()  # без strip — пароль
            # Валидация: при use_proxy=True все 4 поля обязательны.
            if use_proxy and not (host and port and user and pwd):
                QMessageBox.warning(
                    self, tr('proxy_use_checkbox'),
                    tr('proxy_empty_fields_warning'))
                return
            # Дизаблим кнопку, меняем текст на «Проверяю...».
            self.proxy_test_btn.setEnabled(False)
            self.proxy_test_btn.setText(tr('proxy_test_running'))
            self.proxy_result_lbl.setText("")
            # Lazy import — избегаем circular (threads → _sa).
            from threads.proxy_test import ProxyTestThread
            self._proxy_test_thread = ProxyTestThread(
                use_proxy=use_proxy,
                host=host, port=port,
                username=user, password=pwd,
                parent=self)
            self._proxy_test_thread.result_ready.connect(
                self._on_proxy_test_result)
            self._proxy_test_thread.finished.connect(
                lambda: setattr(self, '_proxy_test_thread', None))
            self._proxy_test_thread.start()
        except Exception:
            traceback.print_exc()
            self.proxy_test_btn.setEnabled(True)
            self.proxy_test_btn.setText(tr('proxy_test_button'))

    def _on_proxy_test_result(self, data: dict):
        """ProxyTestThread.result_ready → форматирует строки + вердикт."""
        try:
            self.proxy_test_btn.setEnabled(True)
            self.proxy_test_btn.setText(tr('proxy_test_button'))
            use_proxy = data.get("use_proxy", False)
            lines = []
            # GitHub
            gh = data.get("github", {})
            if gh.get("ok"):
                lines.append(
                    "<span style='color:#6db86d;'>" +
                    tr('proxy_result_github_ok').format(
                        time_ms=gh.get('time_ms', 0)) +
                    "</span>")
            else:
                lines.append(
                    "<span style='color:#e4344a;'>" +
                    tr('proxy_result_github_err').format(
                        error=gh.get('error') or 'unknown') +
                    "</span>")
            # Fast Gen
            fg = data.get("fastgen", {})
            if fg.get("ok"):
                lines.append(
                    "<span style='color:#6db86d;'>" +
                    tr('proxy_result_fastgen_ok').format(
                        time_ms=fg.get('time_ms', 0)) +
                    "</span>")
            else:
                lines.append(
                    "<span style='color:#e4344a;'>" +
                    tr('proxy_result_fastgen_err').format(
                        error=fg.get('error') or 'unknown') +
                    "</span>")
            # Геолокация
            geo = data.get("geo", {})
            if geo.get("ok"):
                tmpl = ('proxy_geo_label_proxy' if use_proxy
                        else 'proxy_geo_label_direct')
                lines.append(
                    "<span style='color:#6db86d;'>" +
                    tr(tmpl).format(
                        city=geo.get('city', '?'),
                        country=geo.get('country', '?'),
                        country_code=geo.get('country_code', '?'),
                        org=geo.get('org', '?'),
                        ip=geo.get('ip', '?')) +
                    "</span>")
            else:
                lines.append(
                    "<span style='color:#e4344a;'>" +
                    tr('proxy_geo_err').format(
                        error=geo.get('error') or 'unknown') +
                    "</span>")
            # Финальный вердикт.
            all_ok = (gh.get("ok") and fg.get("ok") and geo.get("ok"))
            if all_ok:
                verdict_key = ('proxy_works_ok' if use_proxy
                               else 'proxy_direct_ok')
                lines.append(
                    "<span style='color:#6db86d; font-weight:600;'>" +
                    tr(verdict_key) + "</span>")
            else:
                lines.append(
                    "<span style='color:#e4344a; font-weight:600;'>" +
                    tr('proxy_failed') + "</span>")
            html = "<br>".join(lines).replace("\n", "<br>")
            self.proxy_result_lbl.setText(html)
        except Exception:
            traceback.print_exc()

    def _on_proxy_save_clicked(self):
        """Сохранение в QSettings + диалог рестарта.

        При use_proxy=True валидирует что все 4 поля непустые.
        Сохраняет ключи proxy/enabled, /host, /port, /username, /password.
        Показывает диалог «Перезапустить сейчас / позже» — при «сейчас»
        зовёт QApplication.quit() (юзер открывает .app заново).
        При «позже» — info-диалог что настройки сохранены но не активны
        до рестарта.
        """
        try:
            use_proxy = self.proxy_use_chk.isChecked()
            host = self.proxy_host_input.text().strip()
            port = self.proxy_port_input.text().strip()
            user = self.proxy_username_input.text().strip()
            pwd = self.proxy_password_input.text()
            if use_proxy and not (host and port and user and pwd):
                QMessageBox.warning(
                    self, tr('sec_proxy'),
                    tr('proxy_empty_fields_warning'))
                return
            qs = QSettings(APP_ORG, APP_NAME)
            qs.setValue("proxy/enabled", bool(use_proxy))
            qs.setValue("proxy/host", host)
            qs.setValue("proxy/port", port)
            qs.setValue("proxy/username", user)
            qs.setValue("proxy/password", pwd)
            qs.sync()
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Icon.Information)
            m.setWindowTitle(tr('sec_proxy'))
            m.setText(tr('proxy_save_restart_dialog'))
            now_btn = m.addButton(
                tr('proxy_restart_now'),
                QMessageBox.ButtonRole.AcceptRole)
            later_btn = m.addButton(
                tr('proxy_restart_later'),
                QMessageBox.ButtonRole.RejectRole)
            m.setDefaultButton(later_btn)
            m.exec()
            if m.clickedButton() is now_btn:
                QTimer.singleShot(0, QApplication.quit)
            else:
                QMessageBox.information(
                    self, tr('sec_proxy'),
                    tr('proxy_save_no_restart_notice'))
        except Exception:
            traceback.print_exc()

    def _on_montage_mode_changed(self, mode: str):
        """Клик по сегменту режима монтажки (Коммит 4: ModeSegment вместо combo).
        Если режим меняется — показывает диалог рестарта (now/later). ВАЖНО:
        ModeSegment при клике УЖЕ переключил пилюлю на новый сегмент, поэтому
        при ESC надо вернуть её на текущий режим (set_value(cur_mode))."""
        try:
            cur_mode = mode_loader.get_current_mode()
            if not mode or mode == cur_mode:
                return  # тот же режим — ничего не делаем
            m = QMessageBox(self)
            m.setIcon(QMessageBox.Icon.Information)
            m.setWindowTitle(tr('montage_mode_changed_title'))
            m.setText(tr('montage_mode_changed_text'))
            now_btn = m.addButton(tr('montage_mode_restart_now'),
                                  QMessageBox.ButtonRole.AcceptRole)
            later_btn = m.addButton(tr('montage_mode_restart_later'),
                                    QMessageBox.ButtonRole.RejectRole)
            m.setDefaultButton(later_btn)
            m.exec()
            clicked = m.clickedButton()
            if clicked is None:
                # ESC / крестик — юзер передумал. Пилюля уже уехала на новый
                # сегмент — возвращаем её на текущий режим (set_value без сигнала).
                self.montage_mode_segment.set_value(cur_mode)
                return
            # юзер нажал одну из кнопок — сохраняем выбор (пилюля уже на месте)
            mode_loader.set_current_mode(mode)
            if clicked is now_btn:
                QTimer.singleShot(0, QApplication.quit)
            else:
                QMessageBox.information(
                    self, tr('sec_montage_mode'),
                    tr('montage_mode_restart_later_notice'))
        except Exception:
            traceback.print_exc()

    def _on_montage_runtime_save_clicked(self):
        """v1.0.86 (этап 6): сохранить 3 runtime-настройки оркестратора
        монтажки в QSettings + диалог рестарта (по образцу
        `_on_proxy_save_clicked`).

        Ключи: `montage/opus_effort`, `montage/chunk_timeout_opus_sec`,
        `montage/chunk_timeout_default_sec`. Применяются при создании
        нового MontageOrchestratorThread (после перезапуска или нового
        клика «🎬 Сделать сториборды» — settings читаются в
        `EpisodeChatView._on_montage_start`).
        """
        try:
            effort = self.montage_opus_effort_combo.currentData() or "low"
            if effort not in ("low", "medium", "high", "xhigh", "max"):
                effort = "low"
            timeout_opus = int(self.montage_chunk_timeout_opus_spin.value())
            timeout_default = int(
                self.montage_chunk_timeout_default_spin.value())

            qs = QSettings(APP_ORG, APP_NAME)
            qs.setValue("montage/opus_effort", effort)
            qs.setValue("montage/chunk_timeout_opus_sec", timeout_opus)
            qs.setValue("montage/chunk_timeout_default_sec", timeout_default)
            qs.sync()

            m = QMessageBox(self)
            m.setIcon(QMessageBox.Icon.Information)
            m.setWindowTitle(tr('sec_montage_runtime'))
            m.setText(tr('montage_runtime_restart_dialog'))
            now_btn = m.addButton(
                tr('montage_runtime_restart_now'),
                QMessageBox.ButtonRole.AcceptRole)
            later_btn = m.addButton(
                tr('montage_runtime_restart_later'),
                QMessageBox.ButtonRole.RejectRole)
            m.setDefaultButton(later_btn)
            m.exec()
            if m.clickedButton() is now_btn:
                QTimer.singleShot(0, QApplication.quit)
            else:
                QMessageBox.information(
                    self, tr('sec_montage_runtime'),
                    tr('montage_runtime_save_no_restart_notice'))
        except Exception:
            traceback.print_exc()

    def _refresh_anim_speed_value(self):
        """Обновляет подпись «1.5× (≈420мс на табах)» рядом со слайдером.
        Вызывается при изменении слайдера и при смене языка."""
        try:
            mult = round(self.anim_speed_slider.value() / 100.0, 2)
            # 280мс — базовая длительность fade_in_widget по умолчанию.
            # Показываем результат для табов (наглядно).
            tab_ms = int(280 * mult)
            self.anim_speed_value_lbl.setText(
                tr('anim_speed_value', x=f"{mult:g}", ms=tab_ms))
        except Exception:
            traceback.print_exc()

    def _refresh_speech_speed_b_fast_value(self):
        """Обновляет подпись «4.0 слов/сек (≈2.5с на 10 слов)» рядом
        со слайдером fast. Вызывается из valueChanged-слота, при
        инициализации и при смене языка. Пример «10 слов» — иллюстрация
        для юзера, не реальный duration_sec (без округления вверх и
        без буфера, в отличие от формулы Validator'а)."""
        try:
            wps = round(self.speech_speed_b_fast_slider.value() / 10.0, 2)
            example_sec = round(10 / wps, 1) if wps > 0 else 0
            self.speech_speed_b_fast_value_lbl.setText(
                tr('speech_speed_b_value',
                   wps=f"{wps:g}", ex=f"{example_sec:g}"))
        except Exception:
            traceback.print_exc()

    def _refresh_speech_speed_b_normal_value(self):
        """Обновляет подпись рядом со слайдером normal. См. fast выше."""
        try:
            wps = round(self.speech_speed_b_normal_slider.value() / 10.0, 2)
            example_sec = round(10 / wps, 1) if wps > 0 else 0
            self.speech_speed_b_normal_value_lbl.setText(
                tr('speech_speed_b_value',
                   wps=f"{wps:g}", ex=f"{example_sec:g}"))
        except Exception:
            traceback.print_exc()

    def _refresh_speech_speed_b_slow_value(self):
        """Обновляет подпись рядом со слайдером slow. См. fast выше."""
        try:
            wps = round(self.speech_speed_b_slow_slider.value() / 10.0, 2)
            example_sec = round(10 / wps, 1) if wps > 0 else 0
            self.speech_speed_b_slow_value_lbl.setText(
                tr('speech_speed_b_value',
                   wps=f"{wps:g}", ex=f"{example_sec:g}"))
        except Exception:
            traceback.print_exc()

    def _on_keypool_active_changed(self, *args):
        """Слот watcher'а файла-моста пула ключей. Читает .fastgen_keys_active,
        парсит индекс и мигает соответствующей лампочкой. Чистая КОСМЕТИКА —
        любая ошибка проглатывается, на генерацию/UI не влияет. Переподписывает
        путь (Qt теряет его при os.replace)."""
        try:
            path = getattr(self, '_keypool_active_path', None)
            if not path:
                return
            try:
                w = self._keypool_watcher
                if path not in w.files() and os.path.exists(path):
                    w.addPath(path)
            except Exception:
                pass
            if not os.path.exists(path):
                return
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
            if not raw:
                return
            parts = raw.split()
            idx = int(parts[0])
            nonce = parts[1] if len(parts) > 1 else raw
            # Дедуп: мигаем только на новое значение (watcher шумит по каталогу).
            if nonce == getattr(self, '_keypool_last_nonce', None):
                return
            self._keypool_last_nonce = nonce
            self._blink_key_indicator(idx)
        except Exception:
            pass

    def _blink_key_indicator(self, idx):
        """Зелёная ВСПЫШКА лампочки ключа idx (~700мс), затем назад в статус-цвет
        (_key_status_colors[idx]; база теперь серая «погашена»). Дедлайн вспышки
        в _key_flash_until → _refresh (тик 1с) её не затирает. Зовётся на key_used
        (доставка картинки) и на health-alive. Косметика — ошибки игнорятся."""
        try:
            inds = getattr(self, '_apikey_indicators', [])
            if not (0 <= idx < len(inds)):
                return
            dot = inds[idx]
            self._key_flash_until[idx] = time.monotonic() + 0.7
            dot.setStyleSheet(
                "QLabel { background:#46d160; border-radius:6px; }")
            QTimer.singleShot(700, lambda ix=idx: self._end_key_flash(ix))
        except Exception:
            pass

    def _end_key_flash(self, idx):
        """Конец зелёной вспышки idx → вернуть индикатор в статус-цвет, если за
        700мс не стартовала новая вспышка (тогда её дедлайн ещё в будущем)."""
        try:
            if time.monotonic() < self._key_flash_until.get(idx, 0):
                return
            self._key_flash_until.pop(idx, None)
            inds = getattr(self, '_apikey_indicators', [])
            if 0 <= idx < len(inds):
                inds[idx].setStyleSheet(
                    "QLabel { background:%s; border-radius:6px; }"
                    % getattr(self, '_key_status_colors', {}).get(idx, "#3a3a3a"))
        except Exception:
            pass

    # ── Статусы ключей в Settings (задача Б, этап 2) ──────────────────────
    _KEY_COL_GREY   = "#3a3a3a"   # неактивен / пустое поле / отключён вручную
    _KEY_COL_GREEN  = "#46d160"   # живой
    _KEY_COL_YELLOW = "#e0b341"   # лимит (temp, 429)
    _KEY_COL_RED    = "#ff2b2b"   # выбит насовсем (perm, 401/403/license)

    # 2026-06-26 / Codex: кнопки проверки сервера подключены к theme tokens.
    # Раньше тут жил отдельный синий акцент, который ломал импортированные
    # палитры вроде Higgsfield.
    _SERVER_CHECK_QSS_IDLE = (
        "QPushButton { border:1px solid #d4a256; color:#d4a256; }"
        "QPushButton:hover { background: rgba(212,162,86,0.10); }"
        "QPushButton:disabled { color: rgba(255,255,255,0.40);"
        " border-color: rgba(255,255,255,0.08); }")
    # color:#fbfbfb (НЕ #fcfcfc): тема ремапит #fcfcfc → #101208 (почти чёрный) →
    # текст «Проверяю… Nс» был чёрным. #fbfbfb → #f7f8f4 (белый). Только цвет; фон/
    # рамка/паддинги/размер не тронуты.
    _SERVER_CHECK_QSS_RUNNING = (
        "QPushButton { border:1px solid #d4a256; color:#fbfbfb;"
        " background: rgba(212,162,86,0.18);"
        " text-align:left; padding-left:12px; }"
        "QPushButton:disabled { color:#fbfbfb;"
        " background: rgba(212,162,86,0.18); border-color:#d4a256;"
        " text-align:left; padding-left:12px; }")

    # 2026-06-26 / Codex: «Написать в техподдержку» — обычная secondary-кнопка,
    # без собственного фиолетового острова поверх пользовательской темы.
    _SERVER_SUPPORT_QSS = (
        "QPushButton { border:1px solid rgba(255,255,255,0.12); color:#fbfbfb; }"
        "QPushButton:hover { background:rgba(255,255,255,0.10);"
        " border-color:rgba(255,255,255,0.20); }"
        "QPushButton:disabled { color: rgba(255,255,255,0.40);"
        " border-color: rgba(255,255,255,0.08); }")

    def _refresh_key_status_indicators(self):
        """Перекрашивает индикаторы и пишет статус-лейблы 5 полей ключей по
        данным key_pool: manual-off (серый «отключён вручную») > perm (красный
        «не работает — замени») > temp (жёлтый «лимит, вернётся через N мин») >
        live (зелёный). Пустое поле → серый/без текста/тумблер disabled. Текст не
        из get_keys() (несохранённый) → нейтральный статус, без исключений.
        Косметика — любые ошибки молча проглатываются."""
        try:
            import key_pool, time
            keys = key_pool.get_keys()
            dis = key_pool.disabled_status()      # {idx:(reason,until)}
            manual = key_pool.manual_off_keys()   # set строк-ключей
        except Exception:
            return
        now = time.time()
        fields = getattr(self, '_apikey_fields', [])
        inds = getattr(self, '_apikey_indicators', [])
        lbls = getattr(self, '_apikey_status_labels', [])
        tgls = getattr(self, '_apikey_toggles', [])
        for i, fld in enumerate(fields):
            try:
                txt = (fld.text() or "").strip()
                ind = inds[i] if i < len(inds) else None
                lbl = lbls[i] if i < len(lbls) else None
                tgl = tgls[i] if i < len(tgls) else None
                if not txt:
                    color, status = self._KEY_COL_GREY, ""
                    if tgl is not None:
                        tgl.blockSignals(True)
                        tgl.setChecked(False)
                        tgl.setEnabled(False)
                        tgl.blockSignals(False)
                else:
                    if tgl is not None:
                        tgl.setEnabled(True)
                    is_off = txt in manual
                    # idx ключа в пространстве get_keys() — по ТЕКСТУ, не позиции
                    # поля; нет в get_keys() (несохранён) → kidx=None, нейтрально.
                    kidx = keys.index(txt) if txt in keys else None
                    entry = dis.get(kidx) if kidx is not None else None
                    reason = entry[0] if entry else None
                    # 2026-06-10 (health check): транзиентный UI-статус (НЕ в
                    # key_pool) имеет приоритет над perm/temp/live, но НЕ над
                    # manual-off. Ставится _on_key_health / _start_key_health_check.
                    _h = getattr(self, '_key_health_status', {}).get(i)
                    if is_off:
                        color, status = self._KEY_COL_GREY, tr('key_status_manual_off')
                    elif _h == 'checking':
                        color, status = self._KEY_COL_GREY, tr('key_status_checking')
                    elif _h == 'limit':
                        color, status = self._KEY_COL_YELLOW, tr('key_status_ratelimited')
                    elif reason == 'perm':
                        color, status = self._KEY_COL_RED, tr('key_status_dead')
                    elif reason == 'temp':
                        rem = max(0, int(entry[1] - now))
                        color = self._KEY_COL_YELLOW
                        status = tr('key_status_limit_mmss',
                                    t="%02d:%02d" % (rem // 60, rem % 60))
                    else:
                        # 2026-06-10 (лампочки v2): по умолчанию ПОГАШЕНА (серая);
                        # зелёный — только ВСПЫШКА (health-alive / key_used).
                        # 2026-06-20: для live-ключа в лейбле — usage-статистика
                        # (если уже загружена), иначе пусто. error-состояния ВЫШЕ
                        # (manual-off/limit/dead/temp) имеют приоритет — usage их НЕ
                        # затирает (ветка else сюда не доходит при error).
                        color = self._KEY_COL_GREY
                        status = (getattr(self, '_key_usage', {}).get(i) or "")
                    if tgl is not None:
                        tgl.blockSignals(True)
                        tgl.setChecked(not is_off)
                        tgl.blockSignals(False)
                # base-цвет для blink-сброса (idx индикатора = i)
                self._key_status_colors[i] = color
                # Guard: не затирать активную зелёную вспышку (тик 1с мог бы).
                if ind is not None and \
                        time.monotonic() >= self._key_flash_until.get(i, 0):
                    ind.setStyleSheet(
                        "QLabel { background:%s; border-radius:6px; }" % color)
                if lbl is not None:
                    lbl.setText(status)
                    lbl.setStyleSheet(
                        "color:%s; font-size:11px; padding:0 0 9px 20px;"
                        % (color if status else "#888"))
            except Exception:
                continue

    def _on_key_toggle(self, i):
        """Тумблер «использовать» у ключа i. Снятая галка → ключ в manual-off
        (исключён из ротации, текст в поле остаётся); поставленная → вернуть.
        Хранение по ТЕКСТУ ключа (key_pool.set_manual_off). Пустое поле — no-op."""
        try:
            fields = getattr(self, '_apikey_fields', [])
            tgls = getattr(self, '_apikey_toggles', [])
            if not (0 <= i < len(fields)) or not (0 <= i < len(tgls)):
                return
            txt = (fields[i].text() or "").strip()
            if not txt:
                return
            import key_pool
            key_pool.set_manual_off(txt, not tgls[i].isChecked())
            self._refresh_key_status_indicators()
        except Exception:
            traceback.print_exc()

    def _on_apikey_save(self):
        """Сохраняет введённый API-ключ в QSettings и показывает «✓ Сохранено».
        load_api_key() при следующем вызове вернёт уже новый ключ — без
        перезапуска приложения. Старый .env остаётся как fallback."""
        try:
            fields = getattr(self, '_apikey_fields', [self.apikey_input])
            vals = [f.text().strip() for f in fields]   # по порядку, пустые допустимы
            nonempty = [v for v in vals if v]
            if not nonempty:
                self.apikey_inline_status_lbl.setText(tr('apikey_inline_empty'))
                self.apikey_inline_status_lbl.setStyleSheet(
                    "color:#ff7a7a; font-size:12px;")
                return
            # Доп. ключи 2..5 → только QSettings (в .env НЕ пишем: там Anthropic-ключ).
            qs = QSettings(APP_ORG, APP_NAME)
            for i in range(2, 6):
                v = vals[i - 1] if (i - 1) < len(vals) else ""
                qs.setValue(f"fastgen_api_key_{i}", v)
            # Сайдкар round-robin: все НЕПУСТЫЕ по порядку (key_pool читает его).
            try:
                import key_pool
                key_pool.save_keys(nonempty)
            except Exception:
                traceback.print_exc()
            # Primary в QSettings(fastgen_api_key) + .env строка 0 — через
            # существующий save_api_key (его логику НЕ меняем). primary = поле 1;
            # если поле 1 пустое — берём первый непустой, чтобы legacy
            # load_key()/load_api_key() не остались без ключа.
            primary = vals[0] if vals else ""
            save_api_key(primary or nonempty[0])
            # Снимаем dirty ПЕРЕД показом «✓ Сохранено» (оба статуса теперь в
            # одном inline-лейбле — иначе сброс dirty затёр бы «Сохранено»).
            self._set_apikey_dirty(False)
            self.apikey_inline_status_lbl.setText(tr('apikey_inline_saved'))
            self.apikey_inline_status_lbl.setStyleSheet(
                "color:#6db86d; font-size:12px;")
            # Через 4с прячем «Сохранено» — но только если за это время не
            # появилась dirty-индикация (её не затираем).
            QTimer.singleShot(4000, lambda: (
                self.apikey_inline_status_lbl.setText("")
                if self.apikey_inline_status_lbl.text() == tr('apikey_inline_saved')
                else None))
            # 2026-06-10 (health check): после сохранения — асинхронная проверка
            # живости ключей (лёгкий запрос без генерации). Не блокирует UI.
            self._start_key_health_check()
            self._start_key_usage_check()
        except Exception:
            traceback.print_exc()

    def _set_apikey_dirty(self, dirty: bool):
        """2026-06-10: индикация несохранённых изменений в полях ключей.
        dirty=True → оранжевая рамка у «Сохранить» + лейбл «есть несохранённые
        изменения»; False → норма. Inline-стиль #save также несёт фикс паддинга
        (#1): 7px вместо глобальных 11px, чтобы текст не обрезался в высоту 34px.
        Косметика — ошибки молча проглатываются."""
        try:
            border = " border:1px solid #e0913a;" if dirty else ""
            self.apikey_save_btn.setStyleSheet(
                "QPushButton#save { padding: 7px 14px;%s }" % border)
        except Exception:
            pass
        try:
            lbl = getattr(self, 'apikey_inline_status_lbl', None)
            if lbl is not None:
                if dirty:
                    lbl.setText(tr('apikey_inline_dirty'))
                    lbl.setStyleSheet("color:#e0913a; font-size:12px;")
                else:
                    lbl.setText("")
        except Exception:
            pass

    def _on_apikey_field_changed(self, *args):
        """Любое изменение текста в поле ключа → dirty (пока не нажато
        «Сохранить»). textChanged коннектится ПОСЛЕ build, так что программный
        setText при сборке вкладки сюда не приводит."""
        self._set_apikey_dirty(True)

    def _start_key_health_check(self):
        """2026-06-10 (health check): запустить асинхронную проверку живости
        непустых, НЕ-manual-off ключей (лёгкий POST /upload без файла, без
        генерации). Помечает поля «проверяю…» и стартует KeyHealthThread.
        Косметика/диагностика — ошибки молча проглатываются."""
        try:
            import key_pool
            from threads.key_health import KeyHealthThread
            manual = key_pool.manual_off_keys()
            fields = getattr(self, '_apikey_fields', [])
            pairs = []
            for i, fld in enumerate(fields):
                txt = (fld.text() or "").strip()
                if not txt or txt in manual:
                    self._key_health_status.pop(i, None)   # не проверяем
                    continue
                pairs.append((i, txt))
                self._key_health_status[i] = 'checking'
            self._refresh_key_status_indicators()
            if not pairs:
                return
            th = KeyHealthThread(pairs, self)
            th.key_health.connect(self._on_key_health)
            # Снятие из реестра на ВСТРОЕННОМ finished (летит ПОСЛЕ возврата
            # run() → без premature-GC). KeyHealthThread кастомный finished НЕ
            # объявляет, так что это родной QThread.finished.
            th.finished.connect(
                lambda t=th: (self._key_health_threads.remove(t)
                              if t in self._key_health_threads else None))
            self._key_health_threads.append(th)
            th.start()
        except Exception:
            traceback.print_exc()

    def _on_key_health(self, field_idx: int, key_str: str, status: str):
        """Результат проверки одного ключа. dead → disable_key(perm) + красный;
        alive → снять perm + зелёная ВСПЫШКА (база серая); limit → жёлтый UI
        (в disabled НЕ пишем); server_down/timeout/5xx → НЕ красим (погашена).
        Затем refresh."""
        try:
            import key_pool
            keys = key_pool.get_keys()
            kidx = keys.index(key_str) if key_str in keys else None
            if status == 'dead':
                if kidx is not None:
                    key_pool.disable_key(kidx, 'perm')
                self._key_health_status.pop(field_idx, None)
            elif status == 'alive':
                if kidx is not None:
                    key_pool.clear_disabled(kidx)   # заменён на рабочий — снять perm
                self._key_health_status.pop(field_idx, None)
            elif status == 'limit':
                self._key_health_status[field_idx] = 'limit'
            else:
                # server_down / timeout / 5xx — НЕ красим, ключ остаётся погашенным.
                self._key_health_status.pop(field_idx, None)
            self._refresh_key_status_indicators()
            if status == 'alive':
                self._blink_key_indicator(field_idx)   # зелёная вспышка «живой»
        except Exception:
            traceback.print_exc()

    def _start_key_usage_check(self):
        """2026-06-20: usage-статистика (GET /api/v5/usage) по КАЖДОМУ непустому
        полю ключа — ВКЛЮЧАЯ выключенные чекбоксом «использовать» (статистика
        полезна и для них). Только пустые поля пропускаем. ОДИН поток
        последовательно (KeyUsageThread). Косметика — ошибки молча проглатываются."""
        try:
            from threads.key_usage import KeyUsageThread
            fields = getattr(self, '_apikey_fields', [])
            pairs = []
            for i, fld in enumerate(fields):
                txt = (fld.text() or "").strip()
                if not txt:
                    self._key_usage.pop(i, None)   # пустое поле — без статистики
                    continue
                pairs.append((i, txt))
                self._key_usage[i] = "usage…"       # loading-плейсхолдер
            self._refresh_key_status_indicators()
            if not pairs:
                return
            th = KeyUsageThread(pairs, self)
            th.key_usage.connect(self._on_key_usage)
            th.finished.connect(
                lambda t=th: (self._key_usage_threads.remove(t)
                              if t in self._key_usage_threads else None))
            self._key_usage_threads.append(th)
            th.start()
        except Exception:
            traceback.print_exc()

    def _on_key_usage(self, field_idx: int, usage_str: str):
        """Результат usage по одному ключу → строка под полем. Хранится в
        self._key_usage (переживает refresh'ы); отрисовку делает
        _refresh_key_status_indicators (usage для live-ключа; error-приоритеты выше)."""
        try:
            self._key_usage[int(field_idx)] = str(usage_str or "")
            self._refresh_key_status_indicators()
        except Exception:
            traceback.print_exc()

    def _on_server_check_click(self):
        """2026-06-10 (очередь #1, UX): запустить боевой тест сервера FastGen.
        Ключ — первый доступный из пула (next_key, фильтрует выбитые/manual-off).
        Пустой → «нет доступных ключей», поток НЕ стартует. Секунды/точки ведёт
        UI-таймер (на больном сервере poll-GET блокирует поток — сигнал из потока
        «замирал» бы). Кнопка disabled + фикс-ширина (не прыгает) + выделение."""
        try:
            sct = getattr(self, '_server_check_thread', None)
            if sct is not None and sct.isRunning():
                return  # уже идёт
            import key_pool
            try:
                key = key_pool.next_key() or ""
            except Exception:
                key = ""
            if not key:
                self.server_check_result_lbl.setText(tr('server_check_nokeys'))
                self.server_check_result_lbl.setStyleSheet(
                    "color:#e0913a; font-size:12px; padding-top:6px;")
                return
            from threads.server_check import ServerCheckThread
            self._server_check_down_info = None   # новый тест — прошлый down неактуален
            # Пояснение «что происходит» на время теста (заменится итогом).
            self._reserve_server_check_lbl_height()   # под текущую ширину
            self.server_check_result_lbl.setText(tr('server_check_explain'))
            self.server_check_result_lbl.setStyleSheet(
                "color:#9a8fb0; font-size:12px; padding-top:6px;")
            # Резерв ширины под самый длинный текст («Проверяю… 300с»), фикс →
            # кнопка не дёргается; лево-выравнивание держит надпись стабильной.
            self._server_check_t0 = time.monotonic()
            _reserve = tr('server_check_running').format(n=300)
            _fm = self.server_check_btn.fontMetrics()
            _w = _fm.horizontalAdvance(_reserve) + 32
            self.server_check_btn.setFixedWidth(
                max(self.server_check_btn.width(), _w))
            self.server_check_btn.setEnabled(False)
            self.server_check_btn.setStyleSheet(self._SERVER_CHECK_QSS_RUNNING)
            self._tick_server_check()   # сразу «Проверяю… 0с», без паузы 0.5с
            # UI-таймер: 500мс (точки плавные), секунды по time.monotonic.
            from PyQt6.QtCore import QTimer
            self._server_check_timer = QTimer(self)
            self._server_check_timer.setInterval(500)
            self._server_check_timer.timeout.connect(self._tick_server_check)
            self._server_check_timer.start()
            th = ServerCheckThread(key, self)
            th.results.connect(self._on_server_check_result)
            th.finished.connect(self._on_server_check_finished)
            self._server_check_thread = th
            th.start()
        except Exception:
            traceback.print_exc()
            self._stop_server_check_timer()
            self._reset_server_check_btn()

    def _tick_server_check(self):
        """UI-тик: ТОЛЬКО секунды по time.monotonic. Многоточие в тексте
        статичное «…» (не анимируется) — иначе при смене числа точек менялась бы
        ширина текста и надпись ездила. Кнопка лево-выровнена → секунды растут
        вправо внутри фикс-ширины, слева ничего не дёргается. Секунды НЕ из
        потока — на больном сервере poll-GET блокирует поток до 30с."""
        try:
            sec = int(time.monotonic() - (self._server_check_t0 or time.monotonic()))
            self.server_check_btn.setText(
                tr('server_check_running').format(n=sec))
        except Exception:
            pass

    def _stop_server_check_timer(self):
        """Остановить и снять UI-таймер секунд (idempotent)."""
        t = getattr(self, '_server_check_timer', None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
            self._server_check_timer = None

    def _reset_server_check_btn(self):
        """Вернуть кнопку в idle: текст, idle-стиль, снять фикс-ширину."""
        try:
            self.server_check_btn.setEnabled(True)
            self.server_check_btn.setMinimumWidth(0)
            self.server_check_btn.setMaximumWidth(16777215)
            self.server_check_btn.setStyleSheet(self._SERVER_CHECK_QSS_IDLE)
            self.server_check_btn.setText(tr('server_check_btn'))
        except Exception:
            pass

    def _reserve_server_check_lbl_height(self):
        """minHeight лейбла итога = ровно 4 строки (lineSpacing*4 + отступ). От
        ширины НЕ зависит (без boundingRect/оценок) → идемпотентен; держит
        стабильную высоту блока: лейбл всегда непуст, без «пустого подвала» и без
        завышенного резерва на широких окнах."""
        try:
            lbl = getattr(self, 'server_check_result_lbl', None)
            if lbl is None:
                return
            lbl.setMinimumHeight(lbl.fontMetrics().lineSpacing() * 6 + 12)
        except Exception:
            pass

    def _on_server_check_result(self, results: list):
        """Итог ДВУХ тестов (nano-banana-2, openai-image) — две цветные строки
        в server_check_result_lbl (HTML rich-text, цвет на строку: ok зелёный /
        down красный / noconn оранж). Письмо в техподдержку (_server_check_down_info)
        заполняем ТОЛЬКО по nano-banana-2 down (он = индикатор живости сервера;
        провал только openai = модерация, техподдержку НЕ триггерит). Заменяет
        пояснение, висит до следующего теста."""
        self._stop_server_check_timer()
        try:
            _COL = {'ok': '#46d160', 'down': '#ff2b2b', 'noconn': '#e0913a'}
            _SYM = {'ok': '✓', 'down': '✗', 'noconn': '⚠'}

            def _esc(s):
                return (str(s or "").replace('&', '&amp;')
                        .replace('<', '&lt;').replace('>', '&gt;'))

            self._server_check_down_info = None
            _lines = []
            for _r in (results or []):
                _out = _r.get('outcome')
                _prov = _esc(_r.get('provider'))
                _sec = int(_r.get('sec') or 0)
                _reason = _esc(_r.get('reason'))
                _col = _COL.get(_out, '#9a8fb0')
                _sym = _SYM.get(_out, '?')
                if _out == 'noconn':
                    _body = "%s: %s %s" % (_prov, _sym, _esc(tr('server_check_noconn')))
                else:
                    _body = "%s: %s %ds" % (_prov, _sym, _sec)
                    if _out == 'down' and _reason:
                        _body += " — %s" % _reason
                _lines.append('<span style="color:%s">%s</span>' % (_col, _body))
                # Письмо в техподдержку — ТОЛЬКО по nano-banana-2 down (живость сервера).
                if _r.get('provider') == 'nano-banana-2' and _out == 'down':
                    self._server_check_down_info = {
                        'n': _sec, 'op_id': str(_r.get('op_id') or "")}
            self.server_check_result_lbl.setText("<br>".join(_lines))
            self.server_check_result_lbl.setStyleSheet(
                "font-size:12px; padding-top:6px;")
        except Exception:
            traceback.print_exc()

    def _on_server_check_finished(self):
        """Снятие потока + таймера, сброс кнопки (встроенный QThread.finished —
        ПОСЛЕ возврата run(), без premature-GC)."""
        self._stop_server_check_timer()
        self._reset_server_check_btn()
        self._server_check_thread = None

    def _on_server_support_click(self):
        """Чат техподдержки FastGen (Telegram). Если перед этим тест провалился
        (down) — копируем в буфер готовое письмо (время + op_id) и показываем
        QMessageBox с инструкцией «вставь Cmd/Ctrl+V» + сам текст письма; по
        кнопке «Открыть Telegram» открываем чат (телеграм всплывает ПОСЛЕ окна —
        юзер уже знает что вставить); крестик/Esc = Отмена → НЕ открываем. Без
        провального теста — сразу открываем."""
        try:
            info = getattr(self, '_server_check_down_info', None)
            if info:
                try:
                    body = tr('server_check_support_msg').format(
                        n=int(info.get('n', 0)),
                        op_id=(info.get('op_id') or 'не получен'))
                    QApplication.clipboard().setText(body)
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Icon.Information)
                    box.setWindowTitle(tr('server_check_support_title'))
                    box.setText(tr('server_check_support_body'))
                    box.setInformativeText(body)
                    box.setStandardButtons(
                        QMessageBox.StandardButton.Ok
                        | QMessageBox.StandardButton.Cancel)
                    box.button(QMessageBox.StandardButton.Ok).setText(
                        tr('server_check_support_open'))
                    box.button(QMessageBox.StandardButton.Cancel).setText(
                        tr('server_check_support_cancel'))
                    box.setDefaultButton(QMessageBox.StandardButton.Ok)
                    # Телеграм открываем ТОЛЬКО по «Открыть Telegram». Крестик/Esc
                    # → Cancel → НЕ открываем (текст уже в буфере — это ок).
                    if box.exec() != QMessageBox.StandardButton.Ok:
                        return
                except Exception:
                    traceback.print_exc()
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl("https://t.me/vlad_automatoin"))
        except Exception:
            traceback.print_exc()

    def _open_folder(self):
        # Открываем папку АКТИВНОГО сериала (со всеми его storyboards/refs/etc).
        # Если сериала нет — открываем корень проекта.
        target = SHOW_ROOT if self._current_show else self._project_root
        if not target.exists():
            target = self._project_root
        if sys.platform == "win32":
            subprocess.run(["explorer", str(target)])
        else:
            subprocess.run(["open", str(target)])

    def _copy_theme_template(self):
        """Копирует professionalPalette-шаблон темы в буфер обмена."""
        try:
            QApplication.clipboard().setText(build_llm_theme_prompt())
            QMessageBox.information(
                self,
                "Шаблон темы скопирован",
                "Профессиональный шаблон палитры скопирован в буфер обмена.\n\n"
                "Теперь вставь его в ChatGPT/Claude и добавь скриншот или "
                "ссылку на сайт, стиль которого хочешь повторить."
            )
        except Exception as e:
            QMessageBox.warning(
                self,
                "Не удалось скопировать шаблон",
                str(e)
            )

    def _parse_theme_payload_text(self, text: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """Достаёт JSON-тему из вставленного текста."""
        text = (text or "").strip()
        if not text:
            return None, "Вставь JSON темы в поле импорта."
        try:
            payload = json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None, "Не нашёл JSON-объект в тексте."
            try:
                payload = json.loads(text[start:end + 1])
            except Exception as e:
                return None, f"JSON не читается: {e}"
        if not isinstance(payload, dict):
            return None, "Тема должна быть JSON-объектом."
        return payload, ""

    def _open_theme_manager(self):
        """Окно импорта/экспорта и выбора цветовых тем интерфейса."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Интерфейс")
        dlg.resize(760, 640)
        dlg.setStyleSheet(theme_qss("""
            QDialog { background:#121313; color:#ffffff; }
            QLabel { color:#ffffff; }
            QLabel#theme-manager-note { color:rgba(255,255,255,0.58); font-size:12px; }
            QFrame#theme-manager-panel {
                background:#15151d;
                border:1px solid rgba(255,255,255,0.10);
                border-radius:8px;
            }
            QComboBox, QPlainTextEdit {
                background:#191b1d;
                color:#ffffff;
                border:1px solid #1d1e20;
                border-radius:7px;
                padding:8px 10px;
                selection-background-color:#e4344a;
                selection-color:#fefefe;
            }
            QComboBox QAbstractItemView {
                background:#191b1d;
                color:#ffffff;
                border:1px solid #1d1e20;
                selection-background-color:#e4344a;
                selection-color:#fefefe;
                outline:0;
            }
            QComboBox QAbstractItemView::item:selected {
                color:#fefefe;
            }
            QPlainTextEdit { font-family:Menlo, Consolas, monospace; font-size:11px; }
            QPushButton {
                background:#20182c;
                color:#fbfbfb;
                border:1px solid rgba(255,255,255,0.18);
                border-radius:7px;
                padding:9px 12px;
                font-weight:700;
            }
            QPushButton:hover { border-color:rgba(255,255,255,0.32); }
            QPushButton#theme-primary {
                background:#e4344a;
                color:#fefefe;
                border-color:#e4344a;
            }
            QPushButton#theme-danger {
                background:rgba(228,52,74,0.16);
                color:#fafafa;
                border-color:rgba(228,52,74,0.52);
            }
        """))

        root = QVBoxLayout(dlg)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Темы интерфейса")
        title.setStyleSheet("font-size:18px; font-weight:800;")
        root.addWidget(title)

        note = QLabel(
            "Скопируй шаблон, вставь его в ChatGPT/Claude вместе со скриншотом "
            "или ссылкой на понравившийся интерфейс. Studio принимает короткую "
            "professionalPalette или старую полную JSON-тему, сама собирает "
            "безопасные цвета для кнопок, полей, попапов и карточек."
        )
        note.setObjectName("theme-manager-note")
        note.setWordWrap(True)
        root.addWidget(note)

        saved_panel = QFrame()
        saved_panel.setObjectName("theme-manager-panel")
        saved_lay = QVBoxLayout(saved_panel)
        saved_lay.setSpacing(10)
        saved_lay.setContentsMargins(12, 12, 12, 12)

        saved_title = QLabel("Сохранённые темы")
        saved_title.setStyleSheet("font-size:13px; font-weight:800;")
        saved_lay.addWidget(saved_title)

        theme_combo = QComboBox()
        saved_lay.addWidget(theme_combo)

        saved_btn_row = QHBoxLayout()
        saved_btn_row.setSpacing(8)
        apply_btn = QPushButton("Сделать активной")
        apply_btn.setObjectName("theme-primary")
        copy_selected_btn = QPushButton("Скопировать тему")
        delete_btn = QPushButton("Удалить тему")
        delete_btn.setObjectName("theme-danger")
        saved_btn_row.addWidget(apply_btn)
        saved_btn_row.addWidget(copy_selected_btn)
        saved_btn_row.addWidget(delete_btn)
        saved_lay.addLayout(saved_btn_row)
        root.addWidget(saved_panel)

        import_panel = QFrame()
        import_panel.setObjectName("theme-manager-panel")
        import_lay = QVBoxLayout(import_panel)
        import_lay.setSpacing(10)
        import_lay.setContentsMargins(12, 12, 12, 12)

        import_title = QLabel("Импорт новой темы")
        import_title.setStyleSheet("font-size:13px; font-weight:800;")
        import_lay.addWidget(import_title)

        import_text = QPlainTextEdit()
        import_text.setPlaceholderText(
            "Вставь сюда professionalPalette или полную JSON-тему, которую вернул ChatGPT/Claude..."
        )
        import_text.setMinimumHeight(220)
        import_lay.addWidget(import_text, stretch=1)

        import_btn_row = QHBoxLayout()
        import_btn_row.setSpacing(8)
        copy_template_btn = QPushButton("Скопировать шаблон для чата")
        save_theme_btn = QPushButton("Сохранить вставленную тему")
        save_theme_btn.setObjectName("theme-primary")
        import_btn_row.addWidget(copy_template_btn)
        import_btn_row.addWidget(save_theme_btn)
        import_lay.addLayout(import_btn_row)
        root.addWidget(import_panel, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dlg.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        settings = QSettings(APP_ORG, APP_NAME)

        def refresh_combo():
            store = load_theme_store(settings)
            active_name = store.get("active") or ""
            theme_combo.blockSignals(True)
            theme_combo.clear()
            theme_combo.addItem("Оригинальная LUMZ", "")
            for item in store.get("themes", []):
                theme_combo.addItem(str(item.get("name", "Theme")), item.get("name", ""))
            index = 0
            if active_name:
                for i in range(theme_combo.count()):
                    if theme_combo.itemData(i) == active_name:
                        index = i
                        break
            theme_combo.setCurrentIndex(index)
            theme_combo.blockSignals(False)

        def selected_theme_item() -> Optional[Dict[str, Any]]:
            name = theme_combo.currentData()
            if not name:
                return None
            store = load_theme_store(settings)
            for item in store.get("themes", []):
                if item.get("name") == name:
                    return item
            return None

        def copy_template():
            QApplication.clipboard().setText(build_llm_theme_prompt())
            QMessageBox.information(
                dlg,
                "Шаблон скопирован",
                "Шаблон professionalPalette скопирован. Вставь его в ChatGPT/Claude "
                "и добавь скриншот или ссылку на интерфейс, стиль которого хочешь взять."
            )

        def save_imported_theme():
            payload, err = self._parse_theme_payload_text(import_text.toPlainText())
            if err:
                QMessageBox.warning(dlg, "Тема не сохранена", err)
                return
            assert payload is not None
            ok, validation_err = validate_theme_payload(payload)
            if not ok:
                QMessageBox.warning(dlg, "Тема не сохранена", validation_err)
                return

            normalized = normalize_theme_payload(payload)
            name = str(normalized.get("themeName") or "Custom Theme").strip()
            store = load_theme_store(settings)
            themes = [
                item for item in store.get("themes", [])
                if item.get("name") != name
            ]
            themes.append({"name": name, "payload": normalized})
            store["themes"] = themes
            store["active"] = name
            save_theme_store(store, settings)
            set_theme_overrides_from_payload(normalized)
            refresh_combo()
            import_text.clear()
            QMessageBox.information(
                dlg,
                "Тема сохранена",
                f"Тема «{name}» сохранена и выбрана активной.\n\n"
                "Перезапусти Storyboard Studio, чтобы вся палитра применилась полностью."
            )

        def apply_selected_theme():
            name = theme_combo.currentData()
            store = load_theme_store(settings)
            if not name:
                store["active"] = ""
                save_theme_store(store, settings)
                QMessageBox.information(
                    dlg,
                    "Оригинальная тема выбрана",
                    "Оригинальная тема выбрана активной.\n\n"
                    "Перезапусти Storyboard Studio, чтобы вся палитра применилась полностью."
                )
                return
            item = selected_theme_item()
            if not item:
                QMessageBox.warning(dlg, "Тема не найдена", "Не удалось найти выбранную тему.")
                return
            store["active"] = name
            save_theme_store(store, settings)
            set_theme_overrides_from_payload(item["payload"])
            QMessageBox.information(
                dlg,
                "Тема выбрана",
                f"Тема «{name}» выбрана активной.\n\n"
                "Перезапусти Storyboard Studio, чтобы вся палитра применилась полностью."
            )

        def copy_selected_theme():
            item = selected_theme_item()
            if not item:
                QMessageBox.information(
                    dlg,
                    "Нет темы для копирования",
                    "Оригинальная тема встроена в приложение. Скопировать можно сохранённую тему."
                )
                return
            QApplication.clipboard().setText(
                json.dumps(item["payload"], ensure_ascii=False, indent=2)
            )
            QMessageBox.information(
                dlg,
                "Тема скопирована",
                f"Тема «{item.get('name')}» скопирована как JSON. Её можно отправить коллеге."
            )

        def delete_selected_theme():
            name = theme_combo.currentData()
            if not name:
                QMessageBox.information(
                    dlg,
                    "Нельзя удалить",
                    "Оригинальная тема не удаляется."
                )
                return
            if QMessageBox.question(
                dlg,
                "Удалить тему?",
                f"Удалить тему «{name}» из сохранённых?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            store = load_theme_store(settings)
            store["themes"] = [
                item for item in store.get("themes", [])
                if item.get("name") != name
            ]
            if store.get("active") == name:
                store["active"] = ""
            save_theme_store(store, settings)
            refresh_combo()

        copy_template_btn.clicked.connect(copy_template)
        save_theme_btn.clicked.connect(save_imported_theme)
        apply_btn.clicked.connect(apply_selected_theme)
        copy_selected_btn.clicked.connect(copy_selected_theme)
        delete_btn.clicked.connect(delete_selected_theme)

        refresh_combo()
        dlg.exec()

    def _open_studio_log(self):
        """Открывает runtime.log в системной программе по умолчанию.
        Используется для диагностики ошибок (юзер видит stdout+stderr
        Studio за всю сессию). Кросс-платформенно: macOS/Linux → `open`,
        Windows → `explorer`/`start`. 2026-05-08."""
        try:
            log_path = studio_log_path()
            if not log_path.exists():
                # Создаём пустой файл чтобы открытие не падало
                log_path.touch()
            if sys.platform == "win32":
                # `start "" "path"` для открытия файла дефолтным
                # редактором. CREATE_NO_WINDOW чтобы не мигало консолью.
                subprocess.Popen(
                    ["cmd", "/c", "start", "", str(log_path)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            elif sys.platform == "darwin":
                subprocess.run(["open", str(log_path)])
            else:
                subprocess.run(["xdg-open", str(log_path)])
        except Exception:
            traceback.print_exc()

    def _save_png(self):
        """v1.0.88 (2026-05-17): прямой save в папку блока без QFileDialog.
        Раньше открывался системный «Сохранить как» — юзер сам выбирал
        путь. Теперь файл сразу копируется в .cache/_block_view/<ep>_block<N>/
        с именем `<ep>_block<N>.jpg`. На повторных кликах файл
        перезаписывается. Один блок = один файл.

        2026-05-20: убрана логика суффикса `_2`, `_3` (было «collision-free
        накопление»). Юзер хочет перезапись, а не историю — иначе
        накапливаются десятки версий, и «Собрать серию» не знает какую
        брать. Дополнительно при каждом save'е чистим legacy-файлы
        `<base>_<digit>.jpg` от прошлых версий Studio.
        Папка та же, что у кнопки «🗂 Рефы блока», но без rmtree —
        cleanup только legacy-суффиксов.
        """
        if not self.current_block:
            return
        # Проверяем что хотя бы один шот существует
        any_exists = any(shot_path(self.current_block, i).exists() for i in range(PANELS))
        if not any_exists:
            self.status_bar.showMessage(tr('status_no_shots'))
            return
        # Парсим ep_id + block_n из current_block (формат "epN_block_M")
        m = re.match(r'(ep\d+)_block_(\d+)', self.current_block)
        if not m:
            return
        ep_id = m.group(1)
        block_n = int(m.group(2))
        if not self._current_show:
            return
        # Папка блока в .cache/_block_view/ (та же, что у «Рефы блока»,
        # но БЕЗ rmtree — файлы накапливаются между кликами).
        show_root = self._project_root / "shows" / self._current_show
        dest_dir = (show_root / ".cache" / "_block_view"
                    / f"{ep_id}_block{block_n}")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, "Ошибка экспорта", str(e))
            return
        # Один блок = один файл (без суффиксов _2/_3). Image.save в
        # stitch_shots_to_landscape молча перезаписывает существующий.
        base = f"{ep_id}_block{block_n}"
        ext = ".jpg"
        candidate = dest_dir / f"{base}{ext}"
        # Cleanup legacy `<base>_<digit>.jpg` от прошлых версий Studio
        # (когда работала collision-free логика и накапливались
        # _2/_3/.../_10). После первого save'а с новым кодом папка
        # очищается до единственного актуального файла.
        legacy_pat = re.compile(
            rf'^{re.escape(base)}_\d+\.(jpg|jpeg|png)$', re.IGNORECASE)
        try:
            for f in dest_dir.iterdir():
                if f.is_file() and legacy_pat.match(f.name):
                    try:
                        f.unlink()
                    except Exception:
                        pass  # locked / permission — не валим pipeline
        except Exception:
            pass
        try:
            stitch_shots_to_landscape(self.current_block, candidate,
                                      aspect=self._current_aspect)
            self.status_bar.showMessage(
                tr('status_saved', path=str(candidate)), 6000)
            # 2026-06-02 (вариант A): чистый <base>.jpg сохранён как раньше
            # (нужен «Собрать серию»). Поверх открываем попап наложения
            # PNG-сеток на лица. Отдельный try — чтобы ошибка попапа НЕ
            # маскировалась под «Ошибка экспорта». Импорт ленивый (без
            # circular import). Модальный (.exec()).
            try:
                from widgets.face_grid.grid_dialog import GridDialog
                GridDialog(candidate, ep_id, int(block_n),
                           dest_dir, parent=self).exec()
            except Exception:
                traceback.print_exc()
        except Exception as e:
            QMessageBox.warning(self, "Ошибка экспорта", str(e))

    # ── Обновления ─────────────────────────────────────────────────────────────

    def _check_updates(self):
        if self._update_thread and self._update_thread.isRunning():
            return
        self._update_thread = CheckUpdateThread(self._project_root)
        self._update_thread.update_found.connect(self._show_update_banner)
        self._update_thread.no_update.connect(lambda: None)
        self._update_thread.error.connect(
            lambda e: self.status_bar.showMessage(f"Не удалось проверить обновления: {e}"))
        self._update_thread.start()

    def _on_manual_check_updates(self):
        """2026-05-12 (v1.0.53): обработчик кнопки «🔄 Проверить
        обновления» в Settings. В отличие от auto-check при старте,
        этот вызов даёт UI feedback — кнопка в busy-состоянии, статус
        в status_bar, явное «✓ у тебя последняя версия» через toast
        если обновлений нет.

        Полезно для коллег после ручной установки через Installer:
        «нажми кнопку чтобы убедиться что auto-update работает».
        """
        if self._update_thread and self._update_thread.isRunning():
            self.status_bar.showMessage(tr('check_updates_busy'), 3000)
            return
        if not hasattr(self, 'check_updates_btn'):
            return
        # UI feedback — кнопка disabled + текст «Проверяю…».
        try:
            self.check_updates_btn.setEnabled(False)
            self.check_updates_btn.setText(tr('check_updates_busy'))
        except Exception:
            pass

        def _restore_btn():
            try:
                self.check_updates_btn.setEnabled(True)
                self.check_updates_btn.setText(tr('check_updates_btn'))
            except Exception:
                pass

        def _on_no_update():
            try:
                curr = read_local_app_version(self._project_root)
                self.status_bar.showMessage(
                    tr('check_updates_no_update', version=curr), 6000)
            except Exception:
                pass
            _restore_btn()

        def _on_error(err: str):
            try:
                self.status_bar.showMessage(
                    tr('check_updates_error', msg=str(err)[:200]), 8000)
            except Exception:
                pass
            _restore_btn()

        def _on_update_found(curr_proj, latest_proj, curr_app, latest_app):
            # Показываем стандартный update banner (как auto-check).
            try:
                self._show_update_banner(curr_proj, latest_proj, curr_app, latest_app)
            except Exception:
                pass
            _restore_btn()

        self._update_thread = CheckUpdateThread(self._project_root)
        self._update_thread.update_found.connect(_on_update_found)
        self._update_thread.no_update.connect(_on_no_update)
        self._update_thread.error.connect(_on_error)
        self._update_thread.start()

    def _show_update_banner(self, curr_proj: str, latest_proj: str,
                            curr_app: str, latest_app: str):
        """Показывает баннер обновления Storyboard Studio.

        2026-05-08 (Шаг B): убрано понятие «версия проекта» — теперь у
        Studio одна цифра, она же app_version. Синий баннер «Обновление
        проекта» больше НЕ показывается, даже если curr_proj != latest_proj
        (это legacy-расхождение в version.json у некоторых коллег с
        предыдущих версий — не должно вылезать в UI).

        Сравниваем только app_version → один баннер.
        """
        if latest_app != curr_app:
            self._latest_app_ver = latest_app
            self.app_update_text.setText(
                f"⬇  Новое приложение:  v{curr_app} → v{latest_app}"
            )
            self.app_update_banner.show()

    # 2026-05-08 (Шаг B): удалены `_download_update`, `_on_update_done`,
    # `_on_update_error` — мёртвый код для синего баннера «Обновление
    # проекта» который физически убран из layout. DownloadUpdateThread
    # больше не вызывается. Если когда-нибудь понадобится восстановить
    # — есть git history.

    def _download_app_update(self):
        """Скачивает и устанавливает новый .app бинарник из GitHub Releases."""
        if self._app_update_thread and self._app_update_thread.isRunning():
            return
        if not self._latest_app_ver:
            return

        confirm = QMessageBox.question(
            self, "Обновить приложение?",
            f"Будет скачана версия v{self._latest_app_ver} (~50–150 МБ).\n\n"
            "После загрузки Storyboard Studio закроется и автоматически\n"
            "запустится в новой версии. Все сториборды и настройки\n"
            "сохраняются.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.app_update_btn.setEnabled(False)
        self.app_update_btn.setText("Скачивается…")

        self._app_update_thread = DownloadAppUpdateThread(self._latest_app_ver, self._project_root)
        self._app_update_thread.progress.connect(
            lambda msg, pct: self.status_bar.showMessage(f"{msg} ({pct}%)"))
        self._app_update_thread.finished.connect(self._on_app_update_done)
        self._app_update_thread.error.connect(self._on_app_update_error)
        self._app_update_thread.start()

    def _on_app_update_done(self, new_version: str, install_path: str):
        """Bootstrap-скрипт уже запущен — нам остаётся закрыть Studio.

        2026-05-08 (Шаг 2): новая логика. DownloadAppUpdateThread больше
        НЕ подменяет .exe/.app сам — он только качает + создаёт
        bootstrap-скрипт + запускает его detached. Скрипт ждёт пока
        наш процесс умрёт, потом подменяет файл, потом запускает
        обновлённый Studio. Поэтому здесь:
          • статус-бар: «Перезапускаюсь…».
          • прячем баннер.
          • инкрементим baseline счётчика скачиваний.
          • через 1.5 секунды — QApplication.quit() (даём юзеру
            увидеть статус и thread'у завершиться корректно).
        """
        self.app_update_banner.hide()

        # Фиксируем что это «своё» скачивание — не показывать в счётчике коллег
        try:
            settings = QSettings(APP_ORG, APP_NAME)
            key = f"dl_baseline_{new_version}"
            cur = int(settings.value(key, 0) or 0)
            settings.setValue(key, cur + 1)
        except Exception:
            pass

        self.status_bar.showMessage(
            f"Перезапускаюсь до v{new_version}…")

        # 2026-05-12 (v1.0.55): explicit pre-quit popup. Объясняет юзеру
        # что произойдёт: Studio закроется на 30-60 сек, потом сама
        # откроется обновлённая. Главное — предупреждаем НЕ запускать
        # Studio через ярлык в эти 60 сек (это была главная причина
        # move_failed багов в v1.0.39-v1.0.54: юзер тыкал ярлык, открывал
        # старую .exe, лочил bundle для bat).
        # Параллельно bootstrap.bat запустит TopMost splash-окно которое
        # будет видно на экране весь процесс.
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle(tr('update_quit_msg_title'))
            box.setText(tr('update_quit_msg_text', version=new_version))
            box.setStandardButtons(QMessageBox.StandardButton.NoButton)
            box.addButton(tr('update_quit_msg_btn'),
                          QMessageBox.ButtonRole.AcceptRole)
            box.exec()
        except Exception:
            pass

        # Дать UI отрисовать статус + thread'у мирно завершиться, потом quit.
        # Bootstrap-скрипт в это время уже ждёт смерти нашего PID.
        # 1500мс — необходимое окно для graceful shutdown background-thread'ов
        # (DownloadAppUpdateThread должен flush'ить file handles перед exit).
        # QMessageBox.exec() выше блокирует ТОЛЬКО Qt event loop, фоновые
        # QThread'ы продолжают работать — им нужно физическое время на cleanup.
        QTimer.singleShot(1500, QApplication.quit)

    def _on_app_update_error(self, msg: str):
        self.app_update_btn.setEnabled(True)
        self.app_update_btn.setText("Скачать приложение →")
        QMessageBox.warning(self, "Ошибка загрузки приложения", msg)

    def _fetch_download_stats(self):
        if self._stats_thread and self._stats_thread.isRunning():
            return
        self._stats_thread = FetchStatsThread()
        self._stats_thread.finished.connect(self._on_stats_fetched)
        self._stats_thread.start()

    def _on_stats_fetched(self, stats: list):
        """Показываем счётчик скачиваний за вычетом «своих» (админских).

        Логика baseline:
          • Когда видим версию ВПЕРВЫЕ (нет ключа `dl_baseline_<v>` в QSettings),
            записываем baseline = текущий total. То есть всё что было ДО
            считаем «своим» (тестовые скачивания админа).
          • Когда админ скачивает .app через приложение — инкрементим baseline.
          • Отображение: max(0, total - baseline).
        """
        if not hasattr(self, "stats_label"):
            return
        if not stats:
            self.stats_label.setText(tr('status_no_stats'))
            return

        settings = QSettings(APP_ORG, APP_NAME)
        lines = []
        for s in stats[:3]:
            version = s["version"]
            total   = int(s.get("downloads", 0))
            key     = f"dl_baseline_{version}"
            raw     = settings.value(key)
            if raw is None:
                # Первый показ этой версии — фиксируем baseline = total
                # (всё что уже скачано — считаем своим)
                settings.setValue(key, total)
                baseline = total
            else:
                try:
                    baseline = int(raw)
                except (TypeError, ValueError):
                    baseline = 0
            shown = max(0, total - baseline)
            lines.append(tr('downloads_format', ver=version, n=shown))
        self.stats_label.setText("\n".join(lines))

    def _send_update(self):
        if not self._is_admin:
            return
        if self._update_thread and self._update_thread.isRunning():
            return

        # Проверяем есть ли свежесобранный .app в dist/
        app_path = self._project_root / "dist" / "Storyboard Studio.app"
        has_app  = app_path.exists()

        if not has_app:
            QMessageBox.warning(
                self, "Приложение не пересобрано",
                "В папке dist/ нет Storyboard Studio.app.\n\n"
                "Сначала пересобери приложение командой:\n"
                "  ./build.sh\n"
                "и потом отправь обновление снова."
            )
            return

        confirm = QMessageBox.question(
            self, "Отправить обновление?",
            f"Будет загружена новая версия приложения в GitHub Releases.\n\n"
            "Коллеги в открытой Studio увидят баннер «Обновить» и одним\n"
            "кликом получат новую версию.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.send_update_btn.setEnabled(False)
        self.send_update_btn.setText("Отправляю…")
        self._update_thread = SendUpdateThread(self._project_root, upload_app=True)
        self._update_thread.progress.connect(self.status_bar.showMessage)
        self._update_thread.finished.connect(self._on_send_done)
        self._update_thread.error.connect(self._on_send_error)
        self._update_thread.start()

    def _on_send_done(self, new_version: str, new_app_version: str, app_uploaded: bool):
        self.send_update_btn.setText("↑  Отправить обновление")

        # Запоминаем mtime текущего .app — пригодится чтобы понять
        # «приложение было пересобрано после последней отправки».
        app_path = self._project_root / "dist" / "Storyboard Studio.app"
        if app_path.exists():
            try:
                QSettings(APP_ORG, APP_NAME).setValue(
                    "last_sent_app_mtime", app_path.stat().st_mtime)
            except Exception:
                pass

        self._refresh_settings_versions()

        msg = (
            f"Storyboard Studio v{new_app_version} загружена в GitHub Releases.\n\n"
            "Коллеги в открытой Studio увидят баннер «Обновить» и одним\n"
            "кликом получат новую версию (Studio автоматически перезапустится)."
        )
        QMessageBox.information(self, "Обновление отправлено", msg)

        self.status_bar.showMessage(
            f"Опубликовано: Storyboard Studio v{new_app_version} ✓"
        )

        # После успешной отправки нет изменений → кнопка должна стать неактивной.
        self._refresh_send_button()
        # Обновляем счётчик скачиваний (после задержки чтобы GitHub успел проиндексировать)
        QTimer.singleShot(3000, self._fetch_download_stats)

    def _on_send_error(self, msg: str):
        # На ошибке возвращаем кнопку в нормальное состояние, активность
        # выставит _refresh_send_button (изменения остались — должна быть активна).
        self.send_update_btn.setText("↑  Отправить обновление")
        self._refresh_send_button()
        QMessageBox.warning(self, "Ошибка отправки", msg)

    # ── Проверка изменений для админа ───────────────────────────────────────

    def _has_changes_to_send(self) -> tuple:
        """Возвращает (есть_ли_изменения, список_причин).

        Кнопка должна быть активной если:
        • Есть незакоммиченные изменения (`git status --porcelain` непустой)
        • Есть неотправленные коммиты (`HEAD` впереди `origin/main`)
        • dist/Storyboard Studio.app был пересобран после последней отправки
        """
        reasons: List[str] = []
        root = self._project_root

        # 1. Незакоммиченные изменения в файлах проекта
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                **no_console_kwargs(),
            )
            if r.returncode == 0 and r.stdout.strip():
                count = len(r.stdout.strip().splitlines())
                reasons.append(f"измений в файлах: {count}")
        except Exception:
            pass

        # 2. Локальные коммиты впереди origin/main (ещё не запушены)
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count",
                 f"origin/{GITHUB_BRANCH}..HEAD"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                **no_console_kwargs(),
            )
            if r.returncode == 0:
                try:
                    n = int(r.stdout.strip() or "0")
                except ValueError:
                    n = 0
                if n > 0:
                    reasons.append(f"неотправленных коммитов: {n}")
        except Exception:
            pass

        # 3. .app пересобран после последней отправки
        app_path = root / "dist" / "Storyboard Studio.app"
        if app_path.exists():
            try:
                last_sent_raw = QSettings(APP_ORG, APP_NAME).value(
                    "last_sent_app_mtime", 0)
                last_sent_mtime = float(last_sent_raw or 0)
            except (TypeError, ValueError):
                last_sent_mtime = 0.0
            current_mtime = app_path.stat().st_mtime
            # 1 сек tolerance чтобы не путать одинаковые mtime
            if current_mtime > last_sent_mtime + 1:
                reasons.append("приложение пересобрано")

        return (len(reasons) > 0, reasons)

    def _refresh_send_button(self):
        """Обновляет состояние кнопки 'Отправить обновление' и подсказку."""
        if not self._is_admin or not hasattr(self, "send_update_btn"):
            return
        # Если идёт отправка — не трогаем (кнопка дизейблена и так)
        if self._update_thread is not None and self._update_thread.isRunning() \
                and isinstance(self._update_thread, SendUpdateThread):
            return

        has_changes, reasons = self._has_changes_to_send()
        self.send_update_btn.setEnabled(has_changes)
        if has_changes:
            self.send_update_btn.setToolTip(
                "Есть что отправить:\n• " + "\n• ".join(reasons)
            )
        else:
            self.send_update_btn.setToolTip(
                "Нет изменений для отправки.\n"
                "Внеси изменения в проект или пересобери приложение."
            )


# ─── Диалог выбора проекта при первом запуске ────────────────────────────────

def ask_project_root(app: QApplication) -> Optional[Path]:
    """Show welcome dialog if no project root stored. Returns chosen path or None."""
    dlg = QDialog()
    dlg.setWindowTitle("Storyboard Studio")
    dlg.setFixedSize(480, 220)
    lay = QVBoxLayout(dlg)
    lay.setSpacing(16)
    lay.setContentsMargins(30, 30, 30, 24)

    title = QLabel("Добро пожаловать в Storyboard Studio")
    title.setStyleSheet("font-size: 16px; font-weight: bold; color: #e0e0e0;")
    lay.addWidget(title)

    desc = QLabel(
        "Укажи папку проекта storyboard-automation.\n"
        "Приложение запомнит её и откроет автоматически в следующий раз.")
    desc.setStyleSheet("font-size: 13px; color: #888;")
    desc.setWordWrap(True)
    lay.addWidget(desc)

    path_label = QLabel("Папка не выбрана")
    path_label.setStyleSheet("font-size: 12px; color: #555; font-style: italic;")
    lay.addWidget(path_label)

    chosen: Dict[str, Optional[Path]] = {"path": None}

    btn_row = QHBoxLayout()
    browse_btn = QPushButton("📁  Выбрать папку…")
    browse_btn.setStyleSheet(
        "background:#2a2a2a; border:1px solid #444; border-radius:6px; "
        "padding:8px 20px; color:#e0e0e0; font-size:13px;")

    def on_browse():
        folder = QFileDialog.getExistingDirectory(dlg, "Выбери папку storyboard-automation")
        if folder:
            p = Path(folder)
            if is_valid_project(p):
                chosen["path"] = p
                path_label.setText(str(p))
                path_label.setStyleSheet("font-size: 12px; color: #6db86d;")
                ok_btn.setEnabled(True)
            else:
                path_label.setText("⚠  Это не папка проекта. Нужна папка с pipeline.py")
                path_label.setStyleSheet("font-size: 12px; color: #cc6666;")

    browse_btn.clicked.connect(on_browse)
    btn_row.addWidget(browse_btn)
    btn_row.addStretch()

    ok_btn = QPushButton("Открыть →")
    ok_btn.setEnabled(False)
    ok_btn.setStyleSheet(
        "background:#1a2a1a; border:1px solid #3a5a3a; border-radius:6px; "
        "padding:8px 20px; color:#6db86d; font-size:13px;")
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)

    lay.addLayout(btn_row)
    dlg.exec()
    return chosen["path"]


# ─── Entry point ─────────────────────────────────────────────────────────────

class _ToolTipBlocker(QObject):
    """Глобальный фильтр событий — поглощает все QEvent.Type.ToolTip
    события чтобы native-tooltips никогда не показывались. Юзер сказал
    что они портят интерфейс (закрывают часть карточки на hover кнопок
    «Перегенерировать»/«Изменить»). Лучше так — одним фильтром убрать
    ВСЕ tooltip-ы во всём приложении, не вычищая `setToolTip` по коду."""

    def eventFilter(self, obj, ev):
        try:
            if ev.type() == QEvent.Type.ToolTip:
                return True  # съедаем — tooltip не покажется
        except Exception:
            pass
        return False


def _safe_excepthook(exc_type, exc_value, exc_tb):
    """Phase 2 hotfix #21: глобальная защита от abort() со стороны PyQt6.

    PyQt6 при unhandled Python exception в любом слоте делает qFatal()
    который → abort() (приложение крашится). Это новое поведение
    PyQt6 ≥ 6.5. Если переопределить sys.excepthook — PyQt6 вызывает
    наш hook (который просто печатает traceback), abort НЕ происходит.

    Это самый надёжный способ защиты, лучше чем оборачивать каждый
    слот в try/except индивидуально.
    """
    import traceback as _tb
    try:
        _tb.print_exception(exc_type, exc_value, exc_tb)
    except Exception:
        pass


# ─── Файловое логирование (для диагностики) ─────────────────────────────
# 2026-05-08: Studio пишет stdout+stderr в файл `runtime.log` в системной
# папке логов. Это позволяет юзеру открыть файл из Settings и прислать
# нам что упало (раньше при крахе AutonomousGenThread / popup'а след
# терялся). Кросс-платформенно: macOS → ~/Library/Logs/Storyboard Studio/,
# Win → %LOCALAPPDATA%\Storyboard Studio\Logs\, Linux → ~/.cache/...

def studio_logs_dir() -> Path:
    """Возвращает (создавая если нет) папку для файла логов."""
    home = Path.home()
    if sys.platform == 'darwin':
        d = home / "Library" / "Logs" / "Storyboard Studio"
    elif sys.platform == 'win32':
        base = os.environ.get("LOCALAPPDATA")
        d = (Path(base) if base else home / "AppData" / "Local") \
            / "Storyboard Studio" / "Logs"
    else:
        d = home / ".cache" / "storyboard-studio" / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


_STUDIO_LOG_PATH: Optional[Path] = None


def studio_log_path() -> Path:
    """Путь к текущему runtime-логу."""
    return _STUDIO_LOG_PATH or (studio_logs_dir() / "runtime.log")


def _studio_log_path_candidates() -> list[Path]:
    """Кандидаты для runtime.log, от системной папки до безопасного fallback."""
    candidates = [studio_logs_dir() / "runtime.log"]
    try:
        candidates.append(Path.cwd() / "runtime.log")
    except Exception:
        pass
    try:
        candidates.append(Path(__file__).resolve().parent / "runtime.log")
    except Exception:
        pass
    try:
        import tempfile as _tempfile
        candidates.append(
            Path(_tempfile.gettempdir()) / "storyboard-studio-runtime.log")
    except Exception:
        pass

    unique: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


class _StudioTeeStream:
    """Дублирует записи в несколько потоков (console + файл).

    Используется чтобы перенаправить sys.stdout/sys.stderr в файл,
    сохранив вывод в console.app / Terminal.

    2026-05-08: добавлены timestamps `[YYYY-MM-DD HH:MM:SS]` перед
    каждой новой строкой при записи в файл. В console (sys.__stdout__)
    timestamps НЕ добавляются — там и так видно по времени запуска.
    Префикс ставится только для записи в файл (`tag_file=True`)."""

    def __init__(self, *streams_with_tag):
        # streams_with_tag может быть либо просто stream, либо (stream, True)
        # где True = добавлять timestamp. Для обратной совместимости
        # принимаем оба варианта.
        norm = []
        for item in streams_with_tag:
            if item is None:
                continue
            if isinstance(item, tuple):
                stream, tag = item
                norm.append((stream, bool(tag)))
            else:
                norm.append((item, False))
        self._streams = norm
        # Состояние «начало строки» — чтобы вставлять префикс только
        # после `\n` или в самом начале.
        self._at_line_start = True

    def _stamp(self) -> str:
        from datetime import datetime as _dt
        return _dt.now().strftime("[%Y-%m-%d %H:%M:%S] ")

    def write(self, data):
        if not data:
            return 0
        # Для streams с tag=True добавляем timestamp перед каждой новой
        # строкой. В Python `print(...)` вызывает write дважды: payload
        # и `\n`. Поэтому отслеживаем _at_line_start.
        for stream, tag in self._streams:
            try:
                if not tag:
                    stream.write(data)
                else:
                    out_parts = []
                    for ch in data:
                        if self._at_line_start and ch != '\n':
                            out_parts.append(self._stamp())
                            self._at_line_start = False
                        out_parts.append(ch)
                        if ch == '\n':
                            self._at_line_start = True
                    stream.write(''.join(out_parts))
                stream.flush()
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        for stream, _tag in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return any(s.isatty() for s, _t in self._streams)
        except Exception:
            return False


def _init_studio_file_logging() -> Optional[Path]:
    """Открывает runtime.log в режиме append и подменяет sys.stdout/stderr
    на Tee-поток. Возвращает путь к файлу (или None при ошибке).

    Кросс-платформенно: использует только Path/io/sys — Mac/Win идентично.
    Файл-rotation простой: если размер > 5MB — создаём новый, старый
    переименовываем в runtime.log.1 (вытесняет предыдущий .1).
    """
    global _STUDIO_LOG_PATH
    errors = []
    for log_path in _studio_log_path_candidates():
        # Простая ротация чтобы лог не разрастался бесконечно
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                old = log_path.with_suffix('.log.1')
                if old.exists():
                    old.unlink()
                log_path.rename(old)
        except Exception:
            pass
        try:
            log_file = open(log_path, 'a', encoding='utf-8', buffering=1)
            _STUDIO_LOG_PATH = log_path
            from datetime import datetime as _dt
            # Маркер запуска — без timestamps (отдельная разделительная строка).
            log_file.write(
                f"\n=== Storyboard Studio session started at "
                f"{_dt.now().isoformat()} ===\n")
            log_file.flush()
            # Для файла tag=True (добавлять timestamps), для console tag=False.
            sys.stdout = _StudioTeeStream(sys.__stdout__, (log_file, True))
            sys.stderr = _StudioTeeStream(sys.__stderr__, (log_file, True))
            return log_path
        except Exception as e:
            errors.append((log_path, e))
            continue

    # Если файл нельзя открыть нигде (права, диск полный) — просто работаем
    # без файлового лога, не падаем.
    try:
        print("[logging] runtime.log disabled; all candidates failed:",
              errors, file=sys.__stderr__)
    except Exception:
        pass
    return None


def _install_qt_message_handler():
    """Перенаправляет Qt warning/critical/fatal в наш sys.stderr.

    Без этого Qt пишет варнинги напрямую в системный stderr (не наш
    Tee), и они НЕ попадают в файл. Сюда же идут предупреждения вроде
    «QObject::connect: signal not found», «QPainter on inactive widget» —
    это часто причина «кнопка не реагирует / странное поведение».
    2026-05-08."""
    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
        msg_type_names = {
            QtMsgType.QtDebugMsg: "DEBUG",
            QtMsgType.QtInfoMsg: "INFO",
            QtMsgType.QtWarningMsg: "WARNING",
            QtMsgType.QtCriticalMsg: "CRITICAL",
            QtMsgType.QtFatalMsg: "FATAL",
        }

        def _handler(mode, context, message):
            try:
                tag = msg_type_names.get(mode, "QT")
                # Печатаем в stderr (наш Tee → попадёт в файл).
                print(f"[Qt {tag}] {message}", file=sys.stderr)
            except Exception:
                pass

        qInstallMessageHandler(_handler)
    except Exception:
        pass


def main():
    # 2026-05-08: файловое логирование включаем САМЫМ ПЕРВЫМ — чтобы
    # любой crash до создания QApplication тоже попал в лог.
    _init_studio_file_logging()
    sys.excepthook = _safe_excepthook
    # Qt-варнинги (qWarning, qCritical) — перенаправляем в наш stderr,
    # чтобы они тоже попадали в runtime.log с timestamp.
    _install_qt_message_handler()
    # 2026-06-24: pillow-heif — регистрируем HEIF/HEIC opener для PIL ОДИН раз
    # на старте (до любого heic-open). Кроссплатформенно (wheels Win/Mac/Linux).
    # try/except: пакет может быть не установлен — НЕ падаем, heic уйдёт в
    # sips-fallback внутри convert_heic_to_jpg либо будет отклонён.
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        sys.stderr.write("[pillow-heif] registered\n")
    except Exception as _heif_e:
        sys.stderr.write(f"[pillow-heif] not available: {_heif_e}\n")
    # 2026-06-22: self-check QtMultimedia — ОДНОРАЗОВЫЙ диагностический импорт на
    # старте, пишет в runtime.log результат. Нужен чтобы у КОЛЛЕГ в .exe (Windows)
    # сразу было видно, дотянул ли PyInstaller media-стек, БЕЗ ожидания первого
    # hover видео-плитки. На сам автоплей не влияет (плеер ленивый в result_cell).
    try:
        from PyQt6.QtMultimedia import QMediaPlayer  # noqa: F401
        from PyQt6.QtMultimediaWidgets import QVideoWidget  # noqa: F401
        sys.stderr.write("[multimedia] import ok\n")
    except Exception as _mm_e:
        sys.stderr.write(f"[multimedia] import FAIL {_mm_e}\n")
    # v1.0.65: применяем настройки прокси из QSettings к os.environ
    # ДО создания QApplication и ДО запуска любого subprocess. Если
    # юзер включил «🌐 ПРОКСИ-СЕРВЕР» в Settings — все исходящие HTTP
    # запросы (requests-based) пойдут через прокси автоматически,
    # subprocess'ы Claude CLI унаследуют env. См. apply_proxy_from_settings.
    # Лог «[proxy] enabled: ...» пишется в stderr — благодаря
    # _init_studio_file_logging выше попадёт в runtime.log.
    apply_proxy_from_settings()
    app = QApplication(sys.argv)
    load_active_theme_overrides(QSettings(APP_ORG, APP_NAME))
    install_theme_runtime()
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName("Storyboard Studio")
    app.setOrganizationName(APP_ORG)
    # 2026-05-08: платформо-зависимый шрифт-стек чтобы Qt не варнил
    # на отсутствующие шрифты (например «Segoe UI» на macOS, «Helvetica
    # Neue» на Windows). Используем нативные имена для каждой ОС +
    # один общий fallback Arial.
    if sys.platform == "darwin":
        _font_stack = '"Helvetica Neue", Arial'
    elif sys.platform == "win32":
        _font_stack = '"Segoe UI", Arial'
    else:
        _font_stack = '"DejaVu Sans", Arial'
    app.setStyleSheet(theme_qss(DARK.replace("__FONT_FAMILY__", _font_stack)))

    # Глобально блокируем все tooltips (они портят интерфейс)
    _tooltip_blocker = _ToolTipBlocker(app)
    app.installEventFilter(_tooltip_blocker)
    app._tooltip_blocker_keepalive = _tooltip_blocker  # ссылка чтобы GC не съел

    root = get_stored_root()
    if root is None:
        root = ask_project_root(app)
        if root is None:
            sys.exit(0)
        store_root(root)

    win = MainWindow(root)
    win.show()
    # Через 800мс после показа окна — разрешаем fade-in анимации.
    # Это убирает «моргание» на старте: первые setCurrentIndex/select_block
    # вызовы при инициализации НЕ запускают opacity 0→1 (виджеты сразу
    # полностью непрозрачны). 800мс перекрывает время отрисовки + любые
    # отложенные обновления (CheckUpdateThread, FetchStatsThread, watcher).
    QTimer.singleShot(800, _set_ui_ready)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
