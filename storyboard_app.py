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
import webbrowser
import base64
import shutil
import zipfile
import tempfile
import datetime
import subprocess
import traceback
from pathlib import Path
from typing import Optional, List, Dict

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

# scenario_parser — разбор документа со сценариями (библия + эпизоды).
# Чистый Python без Qt, юниты в tests/test_scenario_parser.py.
import scenario_parser

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
    RunEpisodeThread,
)

# views/ — главные view-вкладки.
#   Шаг 4C (2026-05-04): ActorsView + ActorCard.
#   Шаг 5B (2026-05-04): EpisodeChatView + ChatInputEdit (views/episode_chat.py).
#   Шаг 5C (2026-05-04): NewEpisodeView (views/new_episode.py).
from views import (
    ActorsView, ActorCard,
    EpisodeChatView, ChatInputEdit,
    NewEpisodeView,
)

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
)

import requests
from PIL import Image as PILImage

# SSL-fix для PyInstaller frozen .app
if getattr(sys, 'frozen', False):
    import os, certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    os.environ.setdefault('REQUESTS_CA_BUNDLE', certifi.where())

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QListWidget, QListWidgetItem,
    QStatusBar, QFileDialog, QMessageBox, QProgressBar, QDialog,
    QDialogButtonBox, QTabWidget, QComboBox, QPlainTextEdit, QTextEdit, QMenu,
    QStackedWidget, QGridLayout, QGraphicsOpacityEffect, QLineEdit, QSizePolicy,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QFileSystemWatcher, QTimer, QSize, QSettings,
    QRectF, QPoint, QPropertyAnimation, QEasingCurve, QObject, QEvent,
)
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPainterPath, QAction, QGuiApplication, QKeySequence, QShortcut, QColor, QTextCursor, QIcon

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
    """Список slug'ов актёров (имена папок в actors/, исключая системные)."""
    ad = actors_dir(project_root)
    if not ad.exists():
        return []
    return sorted(p.name for p in ad.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


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
                self._scene_fmt.setForeground(QColor("#ffd47a"))
                self._scene_fmt.setBackground(QColor("#3d2c14"))
                self._scene_fmt.setFontWeight(QFont.Weight.Bold)
                # Эпизод-заголовок: ярко-фиолетовый «#c9a8ff», bold —
                # юзер сказал что этот цвет «ещё нормально», оставляем.
                self._ep_fmt = QTextCharFormat()
                self._ep_fmt.setForeground(QColor("#c9a8ff"))
                self._ep_fmt.setFontWeight(QFont.Weight.Bold)
                # 2026-05-08: реплики в кавычках (диалоги) — спокойный
                # холодный голубой, italic. Не конфликтует со scene
                # (тёплый янтарь) и episode (фиолетовый).
                self._dialog_fmt = QTextCharFormat()
                self._dialog_fmt.setForeground(QColor("#7fb8ff"))
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


def finalize_pending_update(project_root: Path):
    """Финализирует авто-обновление после рестарта через bootstrap-скрипт.

    Вызывается из MainWindow.__init__ при каждом старте Studio. Делает
    три вещи (все опциональны, ошибки молча проглатываются):

      1. Если рядом с version.json лежит `pending_version.txt` (его
         написал DownloadAppUpdateThread прямо перед запуском bootstrap)
         — обновляет version.json[app_version] на содержимое маркера и
         удаляет маркер. После этого в углу окна показывается новая
         цифра, и баннер «Скачать приложение» сам пропадает.

      2. Чистит старые update-папки `storyboard_update_*` в системном
         temp. Bootstrap-скрипт обычно сам себя удаляет, но если
         процесс прервался на полпути — мусор может остаться. Удаляем
         всё что соответствует паттерну.

      3. Чистит резидуальные `.exe.old` / `.app.old` рядом с текущим
         бандлом — на случай если когда-то использовался старый
         rename trick.

    Все операции внутри try/except — Studio не должна падать при старте
    из-за проблем с update-инфраструктурой.
    """
    try:
        marker = project_root / "pending_version.txt"
        if marker.exists():
            new_version = marker.read_text(encoding='utf-8').strip()
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
                marker.unlink()
            except Exception:
                pass
    except Exception:
        traceback.print_exc()

    try:
        import tempfile as _tf
        tmp_root = Path(_tf.gettempdir())
        for p in tmp_root.glob("storyboard_update_*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if getattr(sys, 'frozen', False):
            exe_dir = Path(sys.executable).parent
            for p in exe_dir.glob("*.exe.old"):
                try:
                    p.unlink()
                except Exception:
                    pass
            for p in exe_dir.glob("*.app.old"):
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


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
    global LOCATIONS_DIR, CHARACTERS_DIR, OBJECTS_DIR
    ENV_FILE = project_root / ".env"
    if not show_name:
        SHOW_ROOT       = project_root / "shows" / "_none_"
        PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
        STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
        SEEDANCE_DIR    = SHOW_ROOT / "output" / "seedance"
        LOCATIONS_DIR   = SHOW_ROOT / "refs" / "locations"
        CHARACTERS_DIR  = SHOW_ROOT / "refs" / "characters"
        OBJECTS_DIR     = SHOW_ROOT / "refs" / "objects"
        return
    SHOW_ROOT       = project_root / "shows" / show_name
    PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
    STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
    SEEDANCE_DIR    = SHOW_ROOT / "output" / "seedance"
    refs            = SHOW_ROOT / "refs"
    LOCATIONS_DIR   = refs / "locations"
    CHARACTERS_DIR  = refs / "characters"
    OBJECTS_DIR     = refs / "objects"
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
        loc_decisions = decisions.get('location') if isinstance(decisions, dict) else None
        if isinstance(loc_decisions, dict):
            for loc_name, entry in loc_decisions.items():
                if not isinstance(entry, dict) or entry.get('decision') != 'linked':
                    continue
                fn = entry.get('filename') or f"{loc_name}.jpg"
                cand = LOCATIONS_DIR / fn
                if cand.exists():
                    locs.append({'name': _pretty_stem(cand.stem),
                                 'filename': fn, 'path': cand})
        obj_decisions = decisions.get('object') if isinstance(decisions, dict) else None
        if isinstance(obj_decisions, dict):
            for obj_name, entry in obj_decisions.items():
                if not isinstance(entry, dict) or entry.get('decision') != 'linked':
                    continue
                fn = entry.get('filename') or f"{obj_name}.jpg"
                cand = OBJECTS_DIR / fn
                if cand.exists():
                    objs.append({'name': _pretty_stem(cand.stem),
                                 'filename': fn, 'path': cand})
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
            locs.append({'name': _pretty_stem(cand.stem), 'filename': fn, 'path': cand})
            continue
        # 2) objects
        cand = OBJECTS_DIR / fn
        if cand.exists():
            objs.append({'name': _pretty_stem(cand.stem), 'filename': fn, 'path': cand})
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
QMainWindow                 { background: #0a0a0d; }
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
QDialog                     { background: #1a1424; }

QPushButton {
    background: #221a30; border: 1px solid #2e2440; border-radius: 6px;
    padding: 7px 14px; color: #e0e0e0; font-size: 13px;
}
QPushButton:hover           { background: #2c2240; border-color: #3d2f55; }
QPushButton:pressed         { background: #1c1626; }
QPushButton:disabled        { background: #181222; color: #444; border-color: #221a30; }

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
    background: #e4344a; border: 1px solid #e4344a; color: #ffffff;
    font-weight: 500;
}
QPushButton#pill[active="true"]:hover { background: #d92d44; }

/* Pill-кнопка «+ Новый эпизод» — красная subtle (LUMZ accent_red_subtle).
   Активна когда юзер на странице NewEpisodeView. */
QPushButton#pill-new {
    background: rgba(228, 52, 74, 0.10);
    border: 1px solid rgba(228, 52, 74, 0.25);
    border-radius: 6px; padding: 4px 8px;
    color: #e4344a;
    font-size: 11px; font-weight: 500;
    min-width: 0;
}
QPushButton#pill-new:hover {
    background: rgba(228, 52, 74, 0.18);
    border-color: rgba(228, 52, 74, 0.40);
}
QPushButton#pill-new[active="true"] {
    background: rgba(228, 52, 74, 0.25);
    border: 1px solid rgba(228, 52, 74, 0.50);
    color: #ffffff;
}
QPushButton#pill-new[active="true"]:hover { background: #7d5bd4; }

/* 2026-05-08 редизайн Этап 4: полоса блоков (Блок 1/2/3/4 + Референсы +
   Чат) обёрнута в один контейнер #blocks-bar (см. _build_editor_tab),
   внутри которого 6 элементов прижаты друг к другу. Активная пилюля —
   с лёгкой красной подсветкой (accent_red_bg), неактивные — прозрачные. */
QWidget#blocks-bar {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
}

QPushButton#pill-block {
    background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 6px 14px;
    color: rgba(255, 255, 255, 0.55);
    font-size: 12px; font-weight: 500; min-height: 14px;
}
QPushButton#pill-block:hover {
    background: rgba(255, 255, 255, 0.04);
    color: rgba(255, 255, 255, 0.85);
}

/* Блок с непросмотренными шотами — золотой акцент (LUMZ accent_gold). */
QPushButton#pill-block[unseen="true"] {
    background: rgba(212, 162, 86, 0.10);
    border: 1px solid rgba(212, 162, 86, 0.30);
    color: #d4a256; font-weight: 500;
}
QPushButton#pill-block[unseen="true"]:hover {
    background: rgba(212, 162, 86, 0.18);
    color: #e1b46d;
}

/* АКТИВНЫЙ блок — accent_red подсветка + красная рамка. */
QPushButton#pill-block[active="true"] {
    background: rgba(228, 52, 74, 0.15);
    border: 1px solid rgba(228, 52, 74, 0.40);
    color: #ffffff; font-weight: 500;
}
QPushButton#pill-block[active="true"]:hover {
    background: rgba(228, 52, 74, 0.22);
}
/* Активный + unseen: красный фон + золотая рамка (combo). */
QPushButton#pill-block[active="true"][unseen="true"] {
    background: rgba(228, 52, 74, 0.18);
    border: 1px solid rgba(212, 162, 86, 0.50);
    color: #ffffff; font-weight: 500;
}
QPushButton#pill-block[active="true"][unseen="true"]:hover {
    background: rgba(228, 52, 74, 0.25);
}

/* Кнопка «Референсы» — золотой текст (LUMZ accent_gold), прозрачный фон.
   Когда активна — accent_red подсветка + белый текст. */
QPushButton#pill-refs {
    background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 6px 14px;
    color: #d4a256;
    font-size: 12px; font-weight: 500; min-height: 14px;
}
QPushButton#pill-refs:hover {
    background: rgba(212, 162, 86, 0.10);
    color: #e1b46d;
}
QPushButton#pill-refs[active="true"] {
    background: rgba(228, 52, 74, 0.15);
    border: 1px solid rgba(228, 52, 74, 0.40);
    color: #ffffff; font-weight: 500;
}

/* Пульсирующая подсветка «Референсы» — янтарь (LUMZ accent_gold). */
QPushButton#pill-refs[has_notice="true"] {
    background: rgba(212, 162, 86, 0.18);
    border: 1px solid rgba(212, 162, 86, 0.50);
    color: #ffffff; font-weight: 500;
}
QPushButton#pill-refs[has_notice="true"][pulse_on="true"] {
    background: rgba(212, 162, 86, 0.30);
    border: 1px solid rgba(212, 162, 86, 0.70);
    color: #ffffff; font-weight: 500;
}
QPushButton#pill-refs[has_notice="true"]:hover {
    background: rgba(212, 162, 86, 0.25);
    color: #ffffff;
}

/* Пилюля «ЧАТ» — белый текст (text_primary), прозрачный фон.
   Когда активен — accent_red подсветка. */
QPushButton#pill-chat {
    background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 6px 14px;
    color: #ffffff;
    font-size: 12px; font-weight: 500; min-height: 14px;
}
QPushButton#pill-chat:hover {
    background: rgba(255, 255, 255, 0.06);
}
QPushButton#pill-chat[active="true"] {
    background: rgba(228, 52, 74, 0.15);
    border: 1px solid rgba(228, 52, 74, 0.40);
    color: #ffffff; font-weight: 500;
}

/* Кнопка «Удалить эпизод» — иконка корзины справа от полосы блоков.
   2026-05-08 редизайн: text_muted по умолчанию, hover → accent_red. */
QPushButton#delete-episode-btn {
    background: transparent; border: 1px solid transparent;
    border-radius: 8px; padding: 0;
    color: rgba(255, 255, 255, 0.40);
    font-size: 16px; font-weight: 500;
}
QPushButton#delete-episode-btn:hover {
    background: rgba(228, 52, 74, 0.15);
    border: 1px solid rgba(228, 52, 74, 0.40);
    color: #e4344a;
}
QPushButton#delete-episode-btn:disabled {
    background: transparent; border: 1px solid transparent;
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
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
}
QFrame#ref-card:hover { border-color: rgba(255, 255, 255, 0.12); }
QWidget#ref-card-info { background: transparent; }
QLabel#ref-name { color: #ffffff; font-size: 13px; font-weight: 500; }
QLabel#ref-tag  { color: rgba(255, 255, 255, 0.40); font-size: 11px; }

/* Карточка шота — 2026-05-08 редизайн Этап 5: лёгкая LUMZ-стиль
   карточка — фон bg_subtle (rgba(255,255,255,0.04)), border default
   (rgba(255,255,255,0.06)), radius 8px. На hover чуть ярче border. */
QFrame#card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
}
QFrame#card:hover { border-color: rgba(255, 255, 255, 0.12); }

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
    background: #1f1828;
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 6px;
    padding: 0;
    min-width: 36px; max-width: 36px;
    min-height: 32px; max-height: 32px;
}
QPushButton#ref-overlay-btn:hover {
    background: #2a2138;
    border: 1px solid rgba(255, 255, 255, 0.20);
}
QPushButton#ref-overlay-btn[primary="true"] {
    background: #e4344a;
    border: 1px solid #e4344a;
}
QPushButton#ref-overlay-btn[primary="true"]:hover {
    background: #d92d44;
    border: 1px solid #d92d44;
}

/* Header — LUMZ + красный квадрат + Storyboard Studio (всё в одной rich-text QLabel).
   2026-05-08 редизайн: шапка стала карточкой с фоном bg_panel + border + radius_lg.
   Табы перенесены ВНУТРЬ шапки как pill-группа справа (см. ниже #header-tabs). */
QLabel#header-version    { font-size: 12px; color: #666; }

/* Header card — карточка шапки с лого LUMZ слева и табами справа */
QFrame#header-card {
    background: rgba(20, 15, 30, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 14px;
}

/* Tabs-pill-group — контейнер вокруг трёх pill-кнопок «Редактор/Актёры/Настройки» */
QFrame#header-tabs {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 8px;
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
    padding: 6px 14px; font-size: 12px;
}
QPushButton#tab-pill:hover {
    color: rgba(255, 255, 255, 0.85);
}
/* Активный таб — приглушённый светлый фон + белый текст */
QPushButton#tab-pill[active="true"] {
    background: rgba(255, 255, 255, 0.06); color: #ffffff;
}

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
/* Плашка «СЕРИЯ NN» / название эпизода — кликабельная, открывает попап
   со сценарием. 2026-05-08 редизайн: LUMZ accent_gold (тёплый янтарь). */
QPushButton#episode-title-btn {
    font-size: 11px; color: #d4a256; font-weight: 500;
    background: rgba(212, 162, 86, 0.10);
    border: 1px solid rgba(212, 162, 86, 0.30);
    border-radius: 6px; padding: 5px 12px;
    text-align: left;
    letter-spacing: 0.3px;
}
QPushButton#episode-title-btn:hover {
    background: rgba(212, 162, 86, 0.18);
    border-color: rgba(212, 162, 86, 0.50);
}
QPushButton#episode-title-btn:pressed {
    background: rgba(212, 162, 86, 0.25);
}
QLabel#episode-duration { font-size: 13px; color: #888; }
/* 2026-05-08 редизайн Этап 5: заголовок над сторибордами в LUMZ-стиле —
   text_primary (#fff), font 14, обычный вес 500, без letter-spacing. */
QLabel#block-title      { font-size: 14px; color: #ffffff; font-weight: 500; letter-spacing: 0; }

/* Карточка шота — внутренние подписи */
QLabel#shot-num         { font-size: 13px; font-weight: 600; color: #fff; }
QLabel#shot-dur         { font-size: 11px; color: #666; }
QLabel#shot-desc        { font-size: 11px; color: #888; }
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
QLabel#settings-section {
    font-size: 11px; font-weight: 700; color: #5a5070; letter-spacing: 2.5px;
}

/* Кнопка-строка внутри #settings-group (открыть папку): без рамки внутри, как пункт меню */
QPushButton#settings-row-btn {
    background: transparent; border: none; padding: 16px 20px;
    color: #ddd; font-size: 13px; text-align: left;
}
QPushButton#settings-row-btn:hover  { background: rgba(60, 48, 90, 0.25); color: #fff; }
QPushButton#settings-row-btn:pressed { background: rgba(60, 48, 90, 0.4); }

/* Строка ключ-значение внутри about (Версия приложения  v1.0.12) */
QWidget#settings-row     { background: transparent; }
QLabel#settings-row-key  { color: #aaa; font-size: 13px; }
QLabel#settings-row-val  { color: #fff; font-size: 13px; font-weight: 500; }
QFrame#settings-divider  { background: rgba(255, 255, 255, 0.06); border: none; }

QStatusBar              { background: rgba(10, 8, 14, 0.95); color: #777; font-size: 11px; border-top: 1px solid #1a141f; }
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
    background: #15101e;
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
    background: #1a1424; border: 1px solid #322545; border-radius: 10px;
    padding: 6px;
}
QMenu#lang-menu::item {
    padding: 11px 22px; color: #ddd; font-size: 13px; border-radius: 8px;
    min-width: 160px;
}
QMenu#lang-menu::item:selected {
    background: rgba(70, 55, 105, 0.7); color: #fff;
}
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


def save_api_key(key: str) -> None:
    """Сохраняет API-ключ Fast Gen в QSettings (системное хранилище)."""
    try:
        QSettings(APP_ORG, APP_NAME).setValue("fastgen_api_key", (key or "").strip())
    except Exception:
        traceback.print_exc()


# ─── Провайдер картинок (NARWHAL Nano Banana 2 / OpenAI) ─────────────────
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
IMAGE_PROVIDER_OPENAI  = "openai"


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
    """Сохраняет провайдер в QSettings. Принимает только known values."""
    try:
        if value not in (IMAGE_PROVIDER_NARWHAL, IMAGE_PROVIDER_OPENAI):
            value = IMAGE_PROVIDER_NARWHAL
        QSettings(APP_ORG, APP_NAME).setValue("image_provider", value)
    except Exception:
        traceback.print_exc()


def find_ref_image(filename: str) -> Optional[Path]:
    for directory in [LOCATIONS_DIR, OBJECTS_DIR]:
        for f in directory.glob("*"):
            if f.is_file() and (f.name == filename or f.stem == filename):
                return f
    for f in CHARACTERS_DIR.rglob("*"):
        if f.is_file() and (f.name == filename or f.stem == filename):
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


def extract_shot_prompt(prompt_text: str, panel_idx: int) -> Optional[str]:
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

    header_new = re.sub(
        r'(?i)(Film storyboard layout,\s*)?ONE\s+wide\s+horizontal\s+sheet,?\s*'
        r'EXACTLY\s+\d+\s+vertical\s+panels[^.]*\.\s*',
        'Single vertical 9:16 panel. ',
        header,
    )
    if header_new == header:
        header_new = re.sub(
            r'(?i)ONE\s+wide\s+horizontal\s+sheet[^.]*\.',
            'Single vertical 9:16 panel.',
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

    return f"{header_new.strip()}\n\n{panel_body}"


def _extract_panel_body(prompt_text: str, panel_idx: int) -> Optional[str]:
    """Извлекает «сырое» тело Panel N из .txt промпта блока — то что
    идёт после «Panel N (Position):» до следующей `Panel \\d+ (` или
    `===ПРОМПТ_БЛОК...КОНЕЦ===`. БЕЗ хедера блока. Используется в
    `_on_edit_shot` для pre-fill попапа правки.
    """
    target = panel_idx + 1
    pat = re.compile(
        rf'Panel\s+{target}\s+\([^)]+\):\s*(.*?)(?=Panel\s+\d+\s+\(|===ПРОМПТ_БЛОК.*?КОНЕЦ|\Z)',
        re.DOTALL,
    )
    m = pat.search(prompt_text)
    if not m:
        return None
    return m.group(1).strip()


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


def stitch_shots_to_landscape(block_name: str, dest: Path) -> None:
    """Склеивает все 9:16 шоты блока в одну 16:9 картинку (4 в ряд) и сохраняет.

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

    total_w = panel_w * PANELS
    canvas  = PILImage.new("RGB", (total_w, panel_h), (255, 255, 255))

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
        canvas.paste(piece, (i * panel_w, 0))

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

            x_offset = i * panel_w
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
            box_y1 = panel_h - margin
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
Create a clean technical identity reference sheet for the same exact person, designed for future image generation consistency.
Create one single image with a custom asymmetric layout based on a 3x3 grid structure.

Layout:
- top left: 3 small head reference panels in one row:
  1. front head close-up
  2. left 3/4 head close-up
  3. left side profile close-up
- bottom left: 3 body reference panels in one row:
  4. front body view, cropped from shoulders to feet
  5. left side body view, cropped from shoulders to feet
  6. back body view, cropped from shoulders to feet
- right side: 1 enlarged facial detail panel occupying a strict 2x2 area.
  This enlarged face panel must be much larger than the other panels and must clearly dominate the layout.
  Show the same exact person in an extreme close frontal face crop, looking directly into the camera, with very tight framing so that only the eyes, nose, lips, and nearby cheek area are visible.
  The face must fill the panel as much as possible. Keep clearly visible natural skin texture, realistic pores, subtle under-eye texture, realistic lips, realistic nose shape, and realistic facial detail.

Composition rules:
- the large facial panel occupies four standard cells on the right side
- the 3 small head panels stay in one horizontal row on the upper left
- the 3 body panels stay in one horizontal row on the lower left
- panels 1, 2, 3: very tight head crop, head fills most of the panel
- panels 4, 5, 6: shoulders to feet, no head visible
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
- exactly 3 small head panels
- exactly 3 small body panels
- exactly 1 enlarged facial detail panel occupying a 2x2 area on the right side
- panels 4, 5, 6 cropped from shoulders to feet, no head visible
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
    def __init__(self, project_root: Path):
        super().__init__()
        self._project_root = project_root
        self._is_admin     = is_admin_mode(project_root)

        # 2026-05-08 (Шаг 2): post-bootstrap finalize.
        # Если предыдущий запуск Studio инициировал авто-обновление через
        # bootstrap-скрипт — сейчас это уже НОВАЯ версия. Подхватываем
        # маркер `pending_version.txt`, обновляем version.json, чистим
        # старые update-папки в temp.
        finalize_pending_update(project_root)
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
        self.setMinimumSize(900, 600)
        self.resize(1100, 800)

        # 2026-05-08: восстановление размера и позиции окна между запусками.
        # Юзер настраивает ширину/высоту мышкой → закрывает Studio →
        # при следующем запуске окно открывается ровно того же размера и
        # на том же месте экрана. Сохранение — в closeEvent через
        # saveGeometry. На первом запуске (geom=None) остаётся стартовый
        # 1100×800. Qt restoreGeometry сам уважает setMinimumSize.
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
        self._update_thread:     Optional[QThread]                 = None
        self._app_update_thread: Optional[DownloadAppUpdateThread] = None
        self._stats_thread:      Optional[FetchStatsThread]        = None
        self._latest_app_ver: Optional[str] = None
        # Множество (block, panel_idx) — недавно регенерированные шоты, ещё не
        # просмотренные пользователем. На карточке у них висит бейдж NEW.
        self._unseen_shots: set = set()
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
        # Анимация точек ⋯ возле блоков с активной регенерацией
        self._dot_step = 0
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

        # Таймер анимации точек у блоков с активной регенерацией.
        # Срабатывает каждые 400ms — циклически меняет ·/··/···
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start(400)

        # Таймер пульсации пилюли «Референсы» когда там есть pending-уведомления.
        # Запущен всегда, но переключает CSS-property только если pending не пуст.
        self._refs_pulse_timer = QTimer(self)
        self._refs_pulse_timer.timeout.connect(self._tick_refs_pulse)
        self._refs_pulse_timer.start(600)

    def _tick_dots(self):
        """Перебирает шаги анимации точек и обновляет индикаторы у блоков
        с активной регенерацией шотов + у пилюли «Референсы» если там идёт
        регенерация рефа или обновление geometry. Также обновляет кнопку
        активных генераций в EpisodeChatView и строки попапа (2026-05-07).
        Если ничего не активно — ничего не делает."""
        refs_busy = self._refs_busy()
        active_gens_busy = bool(self._active_gens)
        if (not self._active_regens and not refs_busy
                and not active_gens_busy):
            return
        self._dot_step = (self._dot_step + 1) % 3
        active_blocks = {b for (b, _) in self._active_regens.keys()}
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
        return len(self._active_gens)

    def has_active_gens_for_ep(self, ep_id: str) -> bool:
        for info in self._active_gens.values():
            if info.get('ep_id') == ep_id:
                return True
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
        """Имя файла рефа после автономной генерации — проверяет hint и
        стандартные расширения в refs/<sub>/."""
        try:
            if hint_path and '.' in hint_path:
                cand = hint_path.split('/')[-1]
                if cand and not cand.endswith('/'):
                    return cand
            cur_show = get_current_show(self._project_root)
            if not cur_show:
                return f"{name}.jpg"
            sub = {'location': 'locations', 'object': 'objects'}.get(
                gen_type, gen_type + 's')
            base = self._project_root / "shows" / cur_show / "refs" / sub
            for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                if (base / f"{name}{ext}").exists():
                    return f"{name}{ext}"
            return f"{name}.jpg"
        except Exception:
            return f"{name}.jpg"

    def _save_active_gen_decision(self, ep_id: str, gen_type: str,
                                   name: str, decision: str,
                                   filename: str = ""):
        """Аналог `EpisodeChatView._save_ref_decision`, но MW-уровневый —
        умеет писать decision для любого ep_id (не только текущего)."""
        try:
            shows_root = self._project_root / "shows"
            cur_show = get_current_show(self._project_root)
            if not cur_show:
                return
            ep_meta = (shows_root / cur_show / "episodes.json")
            import json
            data = {}
            if ep_meta.exists():
                try:
                    data = json.loads(ep_meta.read_text(encoding='utf-8')) or {}
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
            ep_meta.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding='utf-8')
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
        self._last_known_auth_email: Optional[str] = None
        self._last_known_auth_loggedin: bool = True
        self._auth_dismissed_email: Optional[str] = None  # юзер скрыл — не доставать пока не сменится
        self._auth_switch_thread: Optional[QThread] = None  # активный AuthSwitchThread
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

        lay.addStretch()

        # Pill-группа табов «Редактор / Актёры / Настройки».
        # Кнопки переключают `self.tabs.setCurrentIndex(idx)`.
        # Сам QTabBar QTabWidget'а скрыт (см. _build_ui ниже).
        # БЕЗ ИКОНОК — юзер: «иконки на табах не нужны».
        self._header_tab_buttons: List[QPushButton] = []
        tabs_group = QFrame()
        tabs_group.setObjectName("header-tabs")
        tg_lay = QHBoxLayout(tabs_group)
        tg_lay.setContentsMargins(3, 3, 3, 3)
        tg_lay.setSpacing(0)
        for idx, key in enumerate(('tab_editor', 'tab_actors', 'tab_settings')):
            btn = QPushButton(tr(key))
            btn.setObjectName("tab-pill")
            btn.setProperty("active", idx == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("_tab_idx", idx)
            btn.setProperty("_i18n_key", key)
            btn.clicked.connect(
                lambda _checked=False, i=idx: self._on_header_tab_clicked(i))
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
        for i, btn in enumerate(btns):
            btn.setProperty("active", i == idx)
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
            # 2026-05-08: pill-кнопки табов в шапке тоже переводим. Они
            # хранят i18n-ключ в property `_i18n_key` (см. _build_header).
            for btn in getattr(self, '_header_tab_buttons', []):
                key = btn.property("_i18n_key")
                if key:
                    btn.setText(tr(key))
            self.actors_view.apply_lang()
            if hasattr(self, 'new_episode_view'):
                self.new_episode_view.apply_lang()
        # 2026-05-06: AuthBanner — обновить тексты кнопок и баннера.
        if hasattr(self, 'auth_banner'):
            try:
                self.auth_banner.retranslate()
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
            except Exception:
                traceback.print_exc()
        # Админ-разделитель + секция «АНИМАЦИИ» (только если админ)
        if hasattr(self, 'sec_admin_div_lbl'):
            try:
                self.sec_admin_div_lbl.setText(tr('sec_admin_divider'))
                self.sec_image_provider_lbl.setText(tr('sec_image_provider'))
                self.image_provider_hint_lbl.setText(tr('image_provider_hint'))
                self.image_provider_label_lbl.setText(tr('image_provider_label'))
                # Items в QComboBox: пересоздать тексты с сохранением data
                self.image_provider_combo.setItemText(0, tr('image_provider_narwhal'))
                self.image_provider_combo.setItemText(1, tr('image_provider_openai'))
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
        # Кнопка «Удалить эпизод» — text НЕ устанавливаем (используем
        # SVG-иконку через setIcon, см. `_build_editor_tab`). Раньше
        # tr('delete_ep_btn') возвращал '🗑' и рисовался ПОВЕРХ SVG —
        # получалось две корзины рядом. 2026-05-08 hotfix.
        if hasattr(self, 'delete_ep_btn'):
            try:
                self.delete_ep_btn.setToolTip(tr('delete_ep_btn_tooltip'))
            except Exception:
                pass
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

        show_row.addStretch()
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
        # 2026-05-07: title — кликабельная кнопка. Клик → попап с
        # оригинальным сценарием эпизода (из shows/<slug>/scenarios/epNN.txt).
        self.ep_title_label = QPushButton("")
        self.ep_title_label.setObjectName("episode-title-btn")
        self.ep_title_label.setFlat(True)
        self.ep_title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ep_title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ep_title_label.clicked.connect(self._on_ep_title_clicked)
        ep_row.addWidget(self.ep_title_label)
        self.ep_dur_label = QLabel("")
        self.ep_dur_label.setObjectName("episode-duration")
        ep_row.addWidget(self.ep_dur_label)
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
        self.block_pills_layout = QHBoxLayout(self.block_pills_container)
        self.block_pills_layout.setContentsMargins(5, 5, 5, 5)
        self.block_pills_layout.setSpacing(2)
        blk_row = QHBoxLayout()
        blk_row.addWidget(self.block_pills_container)
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
        self.delete_ep_btn.setFixedSize(34, 34)
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
        blk_row.addWidget(self.delete_ep_btn)
        lay.addLayout(blk_row)

        # Заголовок блока («КАМЕРА ЛОРЫ ~8с») + кнопка Seedance справа
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        self.block_title = QLabel("")
        self.block_title.setObjectName("block-title")
        title_row.addWidget(self.block_title, stretch=1)
        # 2026-05-06: Этап 3 — кнопка открытия попапа с Seedance промптом.
        # Видна только когда current_block указывает на блок утверждённой
        # карты эпизода. State обновляется в _display_block.
        self.seedance_btn = QPushButton(tr('seedance_btn'))
        self.seedance_btn.setObjectName("seedance-btn")
        self.seedance_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # 2026-05-08 редизайн Этап 5: главная action-кнопка экрана —
        # залитая красная (LUMZ accent_red). Это единственная такая
        # яркая кнопка на странице блока, привлекает внимание к
        # «следующему шагу» (запуск промпта Seedance для видео).
        self.seedance_btn.setStyleSheet(
            "QPushButton#seedance-btn {"
            " background: #e4344a; color: #ffffff;"
            " border: none; border-radius: 8px;"
            " padding: 9px 18px;"
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
        title_row.addWidget(self.seedance_btn)
        lay.addLayout(title_row)

        # ── Стек: страница 0 = шоты, страница 1 = референсы ─────────────────
        self.content_stack = QStackedWidget()

        # ── Страница 0: карточки шотов ──────────────────────────────────────
        cards_w = QWidget()
        self.cards_row = QHBoxLayout(cards_w)
        self.cards_row.setSpacing(12)
        self.cards_row.setContentsMargins(0, 0, 0, 0)
        self.shot_cards: List[ShotCard] = []
        for i in range(PANELS):
            card = ShotCard(i)
            card.regen_requested.connect(self._on_regen)
            card.edit_requested.connect(self._on_edit_shot)
            # 2026-05-07: клик по картинке → попап ShotViewerDialog с
            # большим превью + историей версий + edit/regen внутри.
            card.image_clicked.connect(self._on_shot_image_clicked)
            self.shot_cards.append(card)
            self.cards_row.addWidget(card)
        self.cards_row.addStretch()

        shots_scroll = QScrollArea()
        shots_scroll.setWidgetResizable(True)
        shots_scroll.setWidget(cards_w)
        shots_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        shots_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_stack.addWidget(shots_scroll)   # index 0

        # ── Страница 1: референсы ──────────────────────────────────────────
        self.refs_container = QWidget()
        self.refs_layout = QVBoxLayout(self.refs_container)
        self.refs_layout.setSpacing(20)
        self.refs_layout.setContentsMargins(0, 0, 0, 0)
        self.refs_layout.addStretch()

        refs_scroll = QScrollArea()
        refs_scroll.setWidgetResizable(True)
        refs_scroll.setWidget(self.refs_container)
        refs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        refs_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_stack.addWidget(refs_scroll)   # index 1

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
        self.save_btn.setObjectName("save")
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
        w.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(w)
        lay.setSpacing(22)
        lay.setContentsMargins(28, 26, 28, 26)

        # ── ПРОЕКТ — одна кнопка «Открыть папку проекта» ────────────────────
        self.sec_project_lbl = QLabel(tr('sec_project'))
        self.sec_project_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_project_lbl)

        proj_frame = QFrame()
        proj_frame.setObjectName("settings-group")
        pf = QVBoxLayout(proj_frame)
        pf.setSpacing(0)
        pf.setContentsMargins(0, 0, 0, 0)
        self.open_folder_btn = QPushButton(tr('open_folder'))
        self.open_folder_btn.setObjectName("settings-row-btn")
        self.open_folder_btn.clicked.connect(self._open_folder)
        pf.addWidget(self.open_folder_btn)
        # 2026-05-08: кнопка «Открыть лог» — показывает runtime.log
        # с диагностикой ошибок (stdout+stderr Studio пишутся туда).
        self.open_log_btn = QPushButton(tr('open_log_btn'))
        self.open_log_btn.setObjectName("settings-row-btn")
        self.open_log_btn.setToolTip(tr('open_log_btn_tooltip'))
        self.open_log_btn.clicked.connect(self._open_studio_log)
        pf.addWidget(self.open_log_btn)
        lay.addWidget(proj_frame)

        # ── О ПРИЛОЖЕНИИ — версии (две строки с тонкой разделительной) ─────
        self.sec_about_lbl = QLabel(tr('sec_about'))
        self.sec_about_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_about_lbl)

        about_frame = QFrame()
        about_frame.setObjectName("settings-group")
        af = QVBoxLayout(about_frame)
        af.setSpacing(0)
        af.setContentsMargins(0, 0, 0, 0)

        # Версия приложения (одна строка — раньше были 2: app + project,
        # 2026-05-08 объединили в одну, поле version в version.json устарело)
        row_app = QWidget()
        row_app.setObjectName("settings-row")
        ra = QHBoxLayout(row_app)
        ra.setContentsMargins(18, 14, 18, 14)
        self.app_ver_key_lbl = QLabel(tr('app_version'))
        self.app_ver_key_lbl.setObjectName("settings-row-key")
        ra.addWidget(self.app_ver_key_lbl)
        ra.addStretch()
        self.app_ver_val_lbl = QLabel("")
        self.app_ver_val_lbl.setObjectName("settings-row-val")
        ra.addWidget(self.app_ver_val_lbl)
        af.addWidget(row_app)

        lay.addWidget(about_frame)

        # ── API КЛЮЧ Fast Gen ──────────────────────────────────────────────
        # Поле для замены ключа без правки .env: коллега получил новый
        # ключ от админа → вставил → «Сохранить» → работает сразу.
        # Сохраняется в QSettings (приоритет над .env). См. load_api_key().
        self.sec_apikey_lbl = QLabel(tr('sec_apikey'))
        self.sec_apikey_lbl.setObjectName("settings-section")
        lay.addWidget(self.sec_apikey_lbl)

        apikey_frame = QFrame()
        apikey_frame.setObjectName("settings-group")
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

        # Строка: input + кнопки
        ak_row = QHBoxLayout()
        ak_row.setSpacing(8)
        self.apikey_input = QLineEdit()
        self.apikey_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.apikey_input.setPlaceholderText(tr('apikey_placeholder'))
        try:
            self.apikey_input.setText(load_api_key() or "")
        except Exception:
            pass
        self.apikey_input.setStyleSheet(
            "QLineEdit { background:#1a1424; border:1px solid #3a2c52;"
            " border-radius:6px; padding:8px 10px; color:#ddd;"
            " font-size:13px; font-family: 'Menlo', monospace; }")
        ak_row.addWidget(self.apikey_input, stretch=1)

        self.apikey_show_btn = QPushButton(tr('apikey_show'))
        self.apikey_show_btn.setFixedHeight(34)
        self.apikey_show_btn.setMinimumWidth(110)
        self.apikey_show_btn.setCheckable(True)
        self.apikey_show_btn.toggled.connect(self._on_apikey_toggle_visibility)
        ak_row.addWidget(self.apikey_show_btn)

        self.apikey_save_btn = QPushButton(tr('apikey_save'))
        self.apikey_save_btn.setObjectName("save")
        self.apikey_save_btn.setFixedHeight(34)
        self.apikey_save_btn.setMinimumWidth(120)
        self.apikey_save_btn.clicked.connect(self._on_apikey_save)
        ak_row.addWidget(self.apikey_save_btn)
        akf.addLayout(ak_row)

        # Статус «✓ Сохранено» появляется на 2 секунды после клика «Сохранить»
        self.apikey_status_lbl = QLabel("")
        self.apikey_status_lbl.setStyleSheet(
            "color:#6db86d; font-size:12px; padding-top:8px;")
        akf.addWidget(self.apikey_status_lbl)

        lay.addWidget(apikey_frame)

        # ── АДМИН-СЕКЦИИ: видны только админу (`_is_admin`) ─────────────────
        # Большая визуальная разделительная плашка чтобы юзер сразу понимал
        # что ниже — настройки которые НЕ уйдут к коллегам (у них этих
        # секций просто не существует — ветка if self._is_admin не сработает).
        if self._is_admin:
            # Тонкая разделительная линия + заголовок «АДМИН-НАСТРОЙКИ»
            lay.addSpacing(8)
            admin_div = QFrame()
            admin_div.setObjectName("admin-divider-line")
            admin_div.setFixedHeight(1)
            admin_div.setStyleSheet(
                "QFrame#admin-divider-line { background: rgba(255,170,68,0.4); border:none; }")
            lay.addWidget(admin_div)
            lay.addSpacing(8)

            self.sec_admin_div_lbl = QLabel(tr('sec_admin_divider'))
            self.sec_admin_div_lbl.setStyleSheet(
                "color:#ffaa44; font-size:12px; font-weight:700; letter-spacing:1.2px;")
            lay.addWidget(self.sec_admin_div_lbl)

            # ── ПРОВАЙДЕР КАРТИНОК — переключатель NARWHAL / OpenAI ───────
            # Влияет на массовую генерацию шотов. Fallback когда NARWHAL
            # captcha-сервис у Fast Gen лежит. Сохраняется в QSettings
            # ключ "image_provider", читается на каждый запуск GenerateThread.
            self.sec_image_provider_lbl = QLabel(tr('sec_image_provider'))
            self.sec_image_provider_lbl.setObjectName("settings-section")
            lay.addWidget(self.sec_image_provider_lbl)

            provider_frame = QFrame()
            provider_frame.setObjectName("settings-group")
            pf = QVBoxLayout(provider_frame)
            pf.setSpacing(0)
            pf.setContentsMargins(18, 14, 18, 14)

            self.image_provider_hint_lbl = QLabel(tr('image_provider_hint'))
            self.image_provider_hint_lbl.setWordWrap(True)
            self.image_provider_hint_lbl.setStyleSheet(
                "color:#aaa; font-size:12px; padding-bottom:10px;")
            pf.addWidget(self.image_provider_hint_lbl)

            provider_row = QHBoxLayout()
            provider_row.setSpacing(12)
            self.image_provider_label_lbl = QLabel(tr('image_provider_label'))
            self.image_provider_label_lbl.setStyleSheet("color:#cfcfcf; font-size:13px;")
            provider_row.addWidget(self.image_provider_label_lbl)

            from PyQt6.QtWidgets import QComboBox
            self.image_provider_combo = QComboBox()
            self.image_provider_combo.addItem(
                tr('image_provider_narwhal'), IMAGE_PROVIDER_NARWHAL)
            self.image_provider_combo.addItem(
                tr('image_provider_openai'), IMAGE_PROVIDER_OPENAI)
            cur_provider = image_provider()
            idx = self.image_provider_combo.findData(cur_provider)
            if idx >= 0:
                self.image_provider_combo.setCurrentIndex(idx)
            self.image_provider_combo.currentIndexChanged.connect(
                self._on_image_provider_changed)
            # КРИТИЧНО: блокируем колесо мыши, иначе прокрутка страницы
            # курсором над комбобоксом МЕНЯЕТ провайдера случайно.
            block_wheel_event(self.image_provider_combo)
            provider_row.addWidget(self.image_provider_combo, stretch=1)
            pf.addLayout(provider_row)

            lay.addWidget(provider_frame)

            # ── АНИМАЦИИ — слайдер скорости fade-переходов ─────────────────
            self.sec_anim_lbl = QLabel(tr('sec_anim'))
            self.sec_anim_lbl.setObjectName("settings-section")
            lay.addWidget(self.sec_anim_lbl)

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

            lay.addWidget(anim_frame)

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
            lay.addWidget(admin_frame)

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
        self.current_block = None
        self._current_episode = None
        # Перевешиваем file watcher на новый путь
        if hasattr(self, '_watcher'):
            self._watcher.removePaths(self._watcher.directories())
            self._watcher.addPath(str(STORYBOARDS_DIR))
        self._populate_episodes()

    def _reload_show(self):
        """Перечитать эпизоды/блоки текущего сериала (после изменений на диске).
        Если юзер сейчас на экране Референсов — НЕ перебрасывать его на shots.
        """
        was_on_refs = (
            hasattr(self, 'content_stack')
            and self.content_stack.currentIndex() == 1
        )
        self._meta = read_episodes_meta(SHOW_ROOT) if self._current_show else {}
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
                self._build_refs_view(self._current_episode)
        except Exception:
            traceback.print_exc()

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
            self.ep_dur_label.setText("")
            if hasattr(self, 'delete_ep_btn'):
                self.delete_ep_btn.setEnabled(False)
                self.delete_ep_btn.setVisible(False)
            # 2026-05-08 hotfix: scenario_drop_zone устанавливается в None
            # (для совместимости со старыми callers), поэтому hasattr=True
            # но значение None. Защита через is-not-None — иначе крах
            # на свежей установке без current_show (типичный сценарий
            # Win-коллег после первого запуска Studio.exe).
            if getattr(self, 'scenario_drop_zone', None) is not None:
                self.scenario_drop_zone.hide()  # без сериала прятаем
            self._populate_blocks()
            return

        eps = list_episodes()
        # 2026-05-05 v3: scenario_drop_zone теперь живёт внутри NewEpisodeView,
        # на стартовом экране её нет — здесь не управляем видимостью.

        # 2026-05-05: пилюли заполняем сеткой 10 в ряду — переход на
        # следующий ряд автоматически. Высота 28px (компактнее чем 32).
        EPS_PER_ROW = 10
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
            btn = QPushButton(n_label)
            btn.setObjectName("pill")
            btn.setFixedHeight(PILL_H)
            btn.setFixedWidth(PILL_W)
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _, e=ep: self._select_episode(e))
            current_row.addWidget(btn)
            self._episode_pills[ep] = btn
            items_in_row += 1
            # Перенос: после 10 пилюль создаём новую строку.
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
            self.ep_dur_label.setText("")
            if hasattr(self, 'delete_ep_btn'):
                self.delete_ep_btn.setEnabled(False)
                self.delete_ep_btn.setVisible(False)
            self._populate_blocks()

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
        self.ep_dur_label.setText(f"{dur}с" if dur else "")
        # Активируем кнопку удаления только когда есть эпизод
        if hasattr(self, 'delete_ep_btn'):
            self.delete_ep_btn.setEnabled(True)
            self.delete_ep_btn.setVisible(True)
        self._populate_blocks()
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
        Возвращает dict: {'shot': N, 'ref': N, 'geometry': N, 'episode': N}.
        Используется в closeEvent чтобы решить — показывать ли подтверждение.
        """
        counts = {'shot': 0, 'ref': 0, 'geometry': 0, 'episode': 0}
        try:
            # Регенерации шотов (GenerateThread)
            counts['shot'] = sum(
                1 for t in self._active_regens.values()
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
            ev = getattr(self, 'episode_chat_view', None)
            if ev is not None:
                t = getattr(ev, '_thread', None)
                if t is not None and t.isRunning():
                    episode_count += 1
            counts['episode'] = episode_count
        except Exception:
            traceback.print_exc()
        return counts

    def closeEvent(self, event):
        """Перехват закрытия окна. Если есть активные фоновые задачи —
        показываем CloseConfirmDialog. По reject (Подождать) — отмена
        закрытия. По accept (Закрыть всё равно) — обычное закрытие
        (потоки умрут вместе с процессом).
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
            # Сохраняем размер/позицию окна — в __init__ восстановится.
            try:
                QSettings(APP_ORG, APP_NAME).setValue(
                    "main_window_geometry", self.saveGeometry())
            except Exception:
                traceback.print_exc()
            event.accept()
        except Exception:
            traceback.print_exc()
            event.accept()

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
            for p in SEEDANCE_DIR.glob(f"{ep_id}_block_*.txt"):
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
        self._refs_pill = None

        if not self._current_episode:
            self.current_block = None
            self.block_title.setText("")
            if hasattr(self, 'seedance_btn'):
                self.seedance_btn.setVisible(False)
            for card in self.shot_cards:
                card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                card.set_image(None)
            if hasattr(self, 'content_stack'):
                self.content_stack.setCurrentIndex(0)
            self.save_btn.show()
            self.save_btn.setEnabled(False)
            return

        blocks = list_blocks_for_episode(self._current_episode)
        for blk in blocks:
            btn = QPushButton(self._format_block_label(blk))
            btn.setObjectName("pill-block")
            btn.setFixedHeight(28)   # 2026-05-08 редизайн: компактнее (было 34)
            btn.setProperty("active", False)
            has_active = any(b == blk for (b, _) in self._active_regens.keys())
            has_unseen = (not has_active) and any(b == blk for (b, _) in self._unseen_shots)
            btn.setProperty("unseen", has_unseen)
            btn.clicked.connect(lambda _, b=blk: self._select_block(b))
            self.block_pills_layout.addWidget(btn)
            self._block_pills[blk] = btn

        # Пилюля «Референсы» — показывается ВСЕГДА когда есть current_episode.
        # Если блоков нет — это единственная пилюля, и refs-view подгружается
        # автоматически. Если блоки есть — между ними и refs стоит разделитель.
        if blocks:
            # 2026-05-08 редизайн: разделитель тоньше и короче (1×18px).
            self.block_pills_layout.addSpacing(8)
            sep = QFrame()
            sep.setObjectName("pills-vsep")
            sep.setFixedSize(1, 18)
            self.block_pills_layout.addWidget(sep, alignment=Qt.AlignmentFlag.AlignVCenter)
            self.block_pills_layout.addSpacing(8)

        self._refs_pill = QPushButton(tr('refs'))
        self._refs_pill.setObjectName("pill-refs")
        self._refs_pill.setFixedHeight(28)  # 2026-05-08 редизайн
        self._refs_pill.setProperty("active", False)
        self._refs_pill.setProperty("has_notice", False)
        self._refs_pill.setProperty("pulse_on", False)
        self._refs_pill.clicked.connect(self._show_refs_view)
        self.block_pills_layout.addWidget(self._refs_pill)
        if self._pending_ref_notices:
            self._set_refs_pill_notice(True)

        # Пилюля «Чат» — для просмотра/продолжения переписки с Claude
        # по этому конкретному эпизоду. История хранится в
        # `shows/<slug>/chats/<ep_id>.jsonl`, переживает перезапуск .app.
        self.block_pills_layout.addSpacing(8)
        self._chat_pill = QPushButton(tr('chat_pill'))
        self._chat_pill.setObjectName("pill-chat")
        self._chat_pill.setFixedHeight(28)  # 2026-05-08 редизайн
        self._chat_pill.setProperty("active", False)
        self._chat_pill.clicked.connect(self._show_chat_view)
        self.block_pills_layout.addWidget(self._chat_pill)

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

    def _select_block(self, name: str):
        # Если юзер кликнул на ТОТ ЖЕ блок что и сейчас — fade-in
        # анимация content_stack даст видимое моргание opacity 0→1 за
        # 240мс. Re-click того же блока не должен моргать — никаких
        # реальных изменений не происходит.
        same_block = (self.current_block == name)
        if self.current_block and self.current_block != name:
            self._mark_block_seen(self.current_block)
        self.current_block = name
        for b, btn in self._block_pills.items():
            btn.setProperty("active", b == name)
            btn.style().unpolish(btn); btn.style().polish(btn)
        for pill_attr in ('_refs_pill', '_chat_pill'):
            pill = getattr(self, pill_attr, None)
            if pill is not None:
                pill.setProperty("active", False)
                pill.style().unpolish(pill); pill.style().polish(pill)
        if hasattr(self, 'content_stack'):
            self.content_stack.setCurrentIndex(0)
            self.save_btn.show()
        self._display_block(name)
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
                        bar.setTabTextColor(idx, _QColor("#ffd24d"))
                    else:
                        # OFF state: обычный шрифт + дефолтный цвет
                        font = _QFont(self._actors_orig_font)
                        font.setBold(False)
                        bar.setTabTextColor(idx, _QColor("#cfcfcf"))
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
                "QPlainTextEdit { background:#15101e; border:1px solid #2c2240; "
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
        if self.current_block:
            self._mark_block_seen(self.current_block)
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

        self._build_refs_view(ep)
        # 2026-05-07: запоминаем для какого ep последний раз строили —
        # чтобы при следующем _show_refs_view'е знать когда можно
        # делать no-op (re-click), а когда нужна перерисовка (другой ep).
        self._refs_view_built_for_ep = ep
        self.content_stack.setCurrentIndex(1)
        self.save_btn.hide()
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
        if self.current_block:
            self._mark_block_seen(self.current_block)
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
        try:
            self.episode_chat_view.set_episode(self._current_episode)
        except Exception:
            pass
        self.content_stack.setCurrentIndex(2)
        self.save_btn.hide()
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
        self.ep_title_label.setText(tr('new_ep_title'))
        self.ep_dur_label.setText("")
        # Кнопка удаления неактивна для виртуального «нового» эпизода
        if hasattr(self, 'delete_ep_btn'):
            self.delete_ep_btn.setEnabled(False)
            self.delete_ep_btn.setVisible(False)
        self.content_stack.setCurrentIndex(3)
        self.save_btn.hide()
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
        has_active = any(b == block_name for (b, _) in self._active_regens.keys())
        if has_active:
            dots_pattern = ["·    ", "· ·  ", "· · ·"]
            return dots_pattern[self._dot_step] + "  "
        return ""

    def _format_block_label(self, block_name: str) -> str:
        """Текст пилюли блока — «Блок N» / «Block N» (+ префикс точек при генерации)."""
        m = re.match(r'.*_block_(\d+)', block_name)
        base = f"{tr('block')} {m.group(1)}" if m else block_name
        return self._block_indicator_for(block_name) + base

    def _refresh_block_indicator(self, block_name: str):
        """Обновляет пилюлю блока: текст (точки если идёт генерация) + property `unseen`."""
        btn = self._block_pills.get(block_name)
        if btn is None:
            return
        has_active = any(b == block_name for (b, _) in self._active_regens.keys())
        # NEW показывается ТОЛЬКО когда нет активных генераций (взаимоисключающие)
        has_unseen = (not has_active) and any(
            b == block_name for (b, _) in self._unseen_shots)
        btn.setText(self._format_block_label(block_name))
        btn.setProperty("unseen", has_unseen)
        btn.style().unpolish(btn); btn.style().polish(btn)

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
            if (name, i) in self._active_regens:
                card.set_loading(True)
            else:
                card.set_loading(False)
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
        if ep and blk_n:
            self.seedance_btn.setVisible(True)
            seedance_path = SEEDANCE_DIR / f"{name}.txt"
            ready = seedance_path.exists() and seedance_path.stat().st_size > 0
            self.seedance_btn.setText(
                tr('seedance_btn') if ready else tr('seedance_btn_pending')
            )
            self.seedance_btn.setEnabled(True)
        else:
            self.seedance_btn.setVisible(False)

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
            # Если попап для этого шота уже открыт — поднимаем существующий.
            existing = self._get_open_shot_viewer(self.current_block, panel_idx)
            if existing is not None:
                existing.refresh()
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            dlg = ShotViewerDialog(
                panel_idx=panel_idx,
                block_name=self.current_block,
                active_path=active,
                history_dir=history,
                parent=self)
            # Edit/regen — те же handlers что hover-overlay имели раньше.
            dlg.edit_requested.connect(self._on_edit_shot)
            dlg.regen_requested.connect(self._on_regen)
            dlg.version_use_requested.connect(self._on_shot_version_use)
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

    def _on_edit_shot(self, panel_idx: int):
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
        if not self.current_block:
            return
        target_block = self.current_block
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

        # Попап с pre-filled промптом + второе поле «короткая инструкция AI»
        result = self._ask_edit_full_prompt(panel_idx, current_body)
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
            card = self.shot_cards[panel_idx]
            card.set_loading(True)
            thread = GenerateThread(target_block, panel_idx,
                                     edit_instruction=short_instruction)
            self._active_regens[key] = thread
            thread.progress.connect(self.status_bar.showMessage)
            thread.step.connect(
                lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
            thread.finished.connect(
                lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
            thread.error.connect(
                lambda msg: self._on_regen_error(msg, target_block, panel_idx))
            thread.start()
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
        card = self.shot_cards[panel_idx]
        card.set_loading(True)
        thread = GenerateThread(target_block, panel_idx)
        self._active_regens[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.step.connect(
            lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
        thread.finished.connect(
            lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(
            f"Перегенерирую SHOT {panel_idx + 1} с правленым промптом…")

    def _ask_edit_full_prompt(self, panel_idx: int,
                                  current_body: str
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
        text.setStyleSheet(
            "QPlainTextEdit { background:#15101e; border:1px solid #2c2240; "
            "border-radius:6px; color:#ddd; padding:10px; "
            "font-size:12px; font-family: 'Menlo','Consolas',monospace; }")
        v.addWidget(text, stretch=1)

        # 2026-05-07: разделитель + второе поле «короткая инструкция AI».
        sep = QLabel("──────  ИЛИ  ──────")
        sep.setStyleSheet("color:#6e4cc4; font-size:11px; font-weight:600;")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sep)

        short_title = QLabel(
            "Короткая правка существующей картинки (AI-edit режим)")
        short_title.setStyleSheet(
            "color:#d8c8ff; font-size:13px; font-weight:600;")
        v.addWidget(short_title)

        short_hint = QLabel(
            "Заполни ТОЛЬКО если хочешь точечно изменить уже "
            "сгенерированную картинку — например «убери ружьё», "
            "«удали стул», «без шляпы». AI возьмёт текущую картинку "
            "как основу и поменяет ТОЛЬКО заданное, остальное "
            "(композиция, ракурс, поза, фон) сохранит. Большое поле "
            "выше при этом не используется.")
        short_hint.setStyleSheet("color:#888; font-size:11px;")
        short_hint.setWordWrap(True)
        v.addWidget(short_hint)

        short_field = QPlainTextEdit()
        short_field.setPlaceholderText(
            "например: «убери ружьё, остальное оставь как есть»")
        short_field.setFixedHeight(72)
        short_field.setStyleSheet(
            "QPlainTextEdit { background:#1a1330; border:1px solid #4a3470; "
            "border-radius:6px; color:#fff; padding:8px; font-size:12px; }")
        v.addWidget(short_field)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить и регенерировать")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)

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
            "QPlainTextEdit { background:#15101e; border:1px solid #2c2240; "
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
                          instruction: Optional[str] = None):
        """Запускает RefGenerateThread (regen или edit) с UI-callback'ами.
        На карточке сразу показывается прогресс-бар (set_loading)."""
        if not hasattr(self, '_ref_threads'):
            self._ref_threads: List[QThread] = []

        thread = RefGenerateThread(image_path, mode, instruction)
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
        Открывает диалог инструкции → FastGen с картинкой как ref → перезапись."""
        instruction = self._ask_ref_edit_instruction(image_path)
        if not instruction:
            return
        self._start_ref_thread(image_path, 'edit', instruction)

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

    def _ask_ref_edit_instruction(self, image_path: Path) -> Optional[str]:
        """Маленький модальный попап для edit-инструкции (аналог _ask_edit_instruction
        для шотов, но с другими текстами)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(tr('ref_edit_dialog_title', name=image_path.stem))
        dlg.setFixedSize(460, 240)
        v = QVBoxLayout(dlg)
        v.setSpacing(12)
        v.setContentsMargins(20, 18, 20, 16)

        title = QLabel(tr('ref_edit_dialog_q'))
        title.setStyleSheet("color:#ddd; font-size:14px; font-weight:500;")
        v.addWidget(title)

        hint = QLabel(tr('ref_edit_dialog_hint'))
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        text = QPlainTextEdit()
        text.setPlaceholderText(tr('ref_edit_dialog_placeholder'))
        text.setStyleSheet(
            "QPlainTextEdit { background:#15101e; border:1px solid #2c2240; "
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
            try:
                self.status_bar.showMessage(f"✗ {fname}: {short}", 10000)
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

    def _on_regen(self, panel_idx: int):
        if not self.current_block:
            return

        target_block = self.current_block
        key = (target_block, panel_idx)

        # Защита от двойного клика на один и тот же шот
        if key in self._active_regens:
            self.status_bar.showMessage(tr('status_already_genning', n=panel_idx + 1))
            return

        # Дизейблим только КОНКРЕТНУЮ карточку (другие шоты остаются доступны
        # для параллельной регенерации, в том числе в других блоках).
        card = self.shot_cards[panel_idx]
        card.set_loading(True)

        thread = GenerateThread(target_block, panel_idx)
        self._active_regens[key] = thread
        thread.progress.connect(self.status_bar.showMessage)
        thread.step.connect(
            lambda lbl, pct: self._on_regen_step(lbl, pct, target_block, panel_idx))
        thread.finished.connect(
            lambda elapsed: self._on_regen_done(panel_idx, target_block, elapsed))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        # Показать ⋯ возле блока в списке (идёт регенерация)
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(tr('status_regenerating', n=panel_idx + 1, block=target_block))

    def _on_regen_step(self, lbl: str, pct: int, target_block: str, panel_idx: int):
        # Прогресс показываем ТОЛЬКО если пользователь сейчас смотрит на тот
        # блок где идёт регенерация. Иначе обновлять чужие карточки нельзя.
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            self.shot_cards[panel_idx].set_progress(lbl, pct)

    def _on_regen_done(self, panel_idx: int, target_block: str, elapsed_seconds: int = 0):
        self._active_regens.pop((target_block, panel_idx), None)
        # Помечаем шот «непросмотренным» — на карточке появится бейдж NEW.
        # Очистится когда юзер переключится с этого блока на другой.
        self._unseen_shots.add((target_block, panel_idx))

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
        if self.current_block:
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
        self._active_regens.pop((target_block, panel_idx), None)

        # Перерисовываем текущий блок ТОЛЬКО если есть смысл (см. _on_regen_done
        # выше — та же логика, иначе моргание у юзера на чужом блоке).
        if self.current_block:
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

            shots: List[tuple] = []
            for panel_idx in range(PANELS):
                body = extract_shot_prompt(prompt_text, panel_idx)
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
                # В блоке нечего генерить (всё на диске или пусто) —
                # ничего не запускаем, ничего не ставим в очередь.
                # Если активного блока нет — ничего не происходит.
                # Если активный есть — он сам закончит и возьмёт
                # следующий блок из очереди.
                return

            if self._storyboard_active_block is None:
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
        self._storyboard_active_pending = len(shots)

        for block_basename, panel_idx in shots:
            # Если юзер сейчас на этом блоке — крутим прелоадер на карточке
            if (self.current_block == block_basename
                    and 0 <= panel_idx < len(self.shot_cards)):
                try:
                    self.shot_cards[panel_idx].set_loading(True)
                except Exception:
                    pass

            key = (block_basename, panel_idx)
            thread = GenerateThread(block_basename, panel_idx)
            self._active_regens[key] = thread
            thread.progress.connect(self.status_bar.showMessage)
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

    def _on_seedance_btn(self):
        """Клик по кнопке «🎬 Промпт Seedance» на текущем блоке.
        Открывает попап с готовым текстом промпта (если файл есть),
        либо показывает «генерируется…» если файл ещё не готов.
        """
        if not self.current_block:
            return
        m = re.match(r'(ep\d+)_block_(\d+)', self.current_block)
        if not m:
            return
        block_n = int(m.group(2))
        seedance_path = SEEDANCE_DIR / f"{self.current_block}.txt"
        if not seedance_path.exists() or seedance_path.stat().st_size == 0:
            QMessageBox.information(
                self, tr('seedance_popup_title', block_n=block_n),
                tr('seedance_popup_pending'))
            return
        try:
            text = seedance_path.read_text(encoding='utf-8')
        except Exception as e:
            QMessageBox.warning(
                self, tr('seedance_popup_title', block_n=block_n),
                tr('seedance_popup_failed', msg=str(e)[:200]))
            return
        self._show_seedance_popup(block_n, text)

    def _show_seedance_popup(self, block_n: int, text: str):
        """Большой попап с textarea промпта + textarea инструкции +
        кнопками «Перегенерировать», «Скопировать», «Закрыть».

        Регенерация работает прямо в этом попапе: юзер пишет фидбэк
        (опционально) → клик «Перегенерировать» → Opus 4.7 переписывает
        блок → главный textarea обновляется новым текстом, фидбэк
        очищается. Файл `output/seedance/<ep>_block_N.txt` перезаписан.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(tr('seedance_popup_title', block_n=block_n))
        dlg.setMinimumSize(780, 720)  # +100px по высоте под нижнюю textarea
        v = QVBoxLayout(dlg)
        v.setSpacing(10)
        v.setContentsMargins(20, 16, 20, 16)

        hint = QLabel(tr('seedance_popup_hint'))
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # Главный textarea — текст промпта (обновляется при regen)
        ta = QPlainTextEdit()
        ta.setPlainText(text)
        ta.setReadOnly(True)
        ta.setStyleSheet(
            "QPlainTextEdit { background:#15101e; border:1px solid #2c2240; "
            "border-radius:6px; color:#ddd; padding:10px; "
            "font-size:12px; font-family: 'Menlo','Consolas',monospace; }")
        v.addWidget(ta, stretch=1)

        # ── Лейбл + textarea инструкции «что переделать» ──
        instr_lbl = QLabel(tr('seedance_popup_instruction_label'))
        instr_lbl.setStyleSheet("color:#aaa; font-size:12px; font-weight:500;")
        v.addWidget(instr_lbl)

        instr_ta = QPlainTextEdit()
        instr_ta.setPlaceholderText(tr('seedance_popup_instruction_placeholder'))
        instr_ta.setMaximumHeight(80)
        instr_ta.setStyleSheet(
            "QPlainTextEdit { background:#181024; border:1px solid #3a2c52; "
            "border-radius:6px; color:#ddd; padding:8px; font-size:12px; }")
        v.addWidget(instr_ta)

        # Маленькая статус-строка под инструкцией для ошибок regen
        regen_status = QLabel("")
        regen_status.setStyleSheet("color:#ff8a8a; font-size:11px;")
        regen_status.setWordWrap(True)
        regen_status.setVisible(False)
        v.addWidget(regen_status)

        btns_row = QHBoxLayout()
        regen_btn = QPushButton(tr('seedance_popup_regen'))
        regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        regen_btn.setStyleSheet(
            "QPushButton { background:#2a1d44; color:#c9aaff; border:1px solid "
            "#4a2f7a; border-radius:6px; padding:8px 14px; font-size:13px; "
            "font-weight:600; }"
            "QPushButton:hover { background:#372659; }"
            "QPushButton:disabled { background:#1a1428; color:#666; "
            "border-color:#2a2240; }")
        copy_btn = QPushButton(tr('seedance_popup_copy'))
        copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn = QPushButton(tr('seedance_popup_close'))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # Текущий текст в попапе (обновляется при успешном regen).
        # Используем list-обёртку чтобы closures могли его менять.
        current_text_box = [text]

        def _do_copy():
            try:
                QApplication.clipboard().setText(current_text_box[0])
                copy_btn.setText(tr('seedance_popup_copied'))
            except Exception:
                pass

        def _do_regen():
            instruction = instr_ta.toPlainText().strip()
            try:
                self._start_seedance_regen(
                    block_n=block_n,
                    previous_prompt=current_text_box[0],
                    user_instruction=instruction,
                    on_done=lambda new_text: _on_regen_success(new_text),
                    on_failed=lambda msg: _on_regen_error(msg),
                )
                regen_btn.setEnabled(False)
                regen_btn.setText(tr('seedance_popup_regenerating'))
                instr_ta.setEnabled(False)
                regen_status.setVisible(False)
                regen_status.setText("")
                # Сбрасываем «✓ Скопировано» — после regen скопированный
                # старый текст уже неактуален, копировать надо новый.
                copy_btn.setText(tr('seedance_popup_copy'))
            except Exception as e:
                _on_regen_error(str(e)[:200])

        def _on_regen_success(new_text: str):
            current_text_box[0] = new_text
            ta.setPlainText(new_text)
            instr_ta.clear()
            instr_ta.setEnabled(True)
            regen_btn.setEnabled(True)
            regen_btn.setText(tr('seedance_popup_regen'))

        def _on_regen_error(msg: str):
            instr_ta.setEnabled(True)
            regen_btn.setEnabled(True)
            regen_btn.setText(tr('seedance_popup_regen'))
            regen_status.setText(tr('seedance_popup_regen_failed', msg=msg))
            regen_status.setVisible(True)

        regen_btn.clicked.connect(_do_regen)
        copy_btn.clicked.connect(_do_copy)
        close_btn.clicked.connect(dlg.accept)
        btns_row.addWidget(regen_btn)
        btns_row.addWidget(copy_btn)
        btns_row.addStretch()
        btns_row.addWidget(close_btn)
        v.addLayout(btns_row)

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
          • Голосовые профили — `instructions/ГОЛОСОВЫЕ_ПРОФИЛИ_…txt` из
            корня проекта (через `self._project_root`).
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
            voices_path = (self._project_root / "instructions"
                           / "ГОЛОСОВЫЕ_ПРОФИЛИ_ПЕРСОНАЖЕЙ.txt")
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

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _on_apikey_toggle_visibility(self, checked: bool):
        """Toggle Password ↔ Normal в QLineEdit для просмотра ключа."""
        try:
            mode = (QLineEdit.EchoMode.Normal if checked
                    else QLineEdit.EchoMode.Password)
            self.apikey_input.setEchoMode(mode)
            self.apikey_show_btn.setText(
                tr('apikey_hide') if checked else tr('apikey_show'))
        except Exception:
            traceback.print_exc()

    def _on_image_provider_changed(self, idx: int):
        """Сохраняет выбранного провайдера в QSettings.
        GenerateThread читает значение на каждый запуск — изменение
        применяется к следующему сгенерированному шоту, перезапуск не нужен."""
        try:
            value = self.image_provider_combo.itemData(idx)
            set_image_provider(value or IMAGE_PROVIDER_NARWHAL)
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

    def _on_apikey_save(self):
        """Сохраняет введённый API-ключ в QSettings и показывает «✓ Сохранено».
        load_api_key() при следующем вызове вернёт уже новый ключ — без
        перезапуска приложения. Старый .env остаётся как fallback."""
        try:
            key = self.apikey_input.text().strip()
            if not key:
                self.apikey_status_lbl.setText(tr('apikey_empty'))
                self.apikey_status_lbl.setStyleSheet(
                    "color:#ff7a7a; font-size:12px; padding-top:8px;")
                return
            save_api_key(key)
            self.apikey_status_lbl.setText(tr('apikey_saved'))
            self.apikey_status_lbl.setStyleSheet(
                "color:#6db86d; font-size:12px; padding-top:8px;")
            # Через 4с прячем подтверждение чтобы не висело
            QTimer.singleShot(4000, lambda: self.apikey_status_lbl.setText(""))
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
        if not self.current_block:
            return
        # Проверяем что хотя бы один шот существует
        any_exists = any(shot_path(self.current_block, i).exists() for i in range(PANELS))
        if not any_exists:
            self.status_bar.showMessage(tr('status_no_shots'))
            return

        dest, _ = QFileDialog.getSaveFileName(
            self, "Сохранить стриборд",
            str(Path.home() / "Desktop" / f"{self.current_block}.png"),
            "PNG (*.png);;JPEG (*.jpg)")
        if not dest:
            return
        try:
            stitch_shots_to_landscape(self.current_block, Path(dest))
            self.status_bar.showMessage(tr('status_saved', path=dest))
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

        # Дать UI отрисовать статус + thread'у мирно завершиться, потом quit.
        # Bootstrap-скрипт в это время уже ждёт смерти нашего PID.
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


def studio_log_path() -> Path:
    """Путь к текущему runtime-логу."""
    return studio_logs_dir() / "runtime.log"


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
    try:
        log_path = studio_log_path()
        # Простая ротация чтобы лог не разрастался бесконечно
        try:
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                old = log_path.with_suffix('.log.1')
                if old.exists():
                    old.unlink()
                log_path.rename(old)
        except Exception:
            pass
        log_file = open(log_path, 'a', encoding='utf-8', buffering=1)
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
    except Exception:
        # Если файл нельзя открыть (права, диск полный) — просто работаем
        # без файлового лога, не падаем.
        try:
            import traceback as _tb
            _tb.print_exc()
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
    app = QApplication(sys.argv)
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
    app.setStyleSheet(DARK.replace("__FONT_FAMILY__", _font_stack))

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
