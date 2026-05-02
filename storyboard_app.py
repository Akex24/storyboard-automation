#!/usr/bin/env python3
"""
Storyboard Studio — macOS / Windows приложение для storyboard-automation.

При первом запуске спрашивает папку проекта и запоминает её.
Запуск из исходников: python3 storyboard_app.py
"""

import re
import sys
import io
import json
import time
import base64
import shutil
import zipfile
import tempfile
import datetime
import subprocess
from pathlib import Path
from typing import Optional, List, Dict

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
    QDialogButtonBox, QTabWidget, QComboBox, QPlainTextEdit, QMenu,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QFileSystemWatcher, QTimer, QSize, QSettings, QRectF, QPoint
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPainterPath, QAction

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
LOCATIONS_DIR   = Path()
CHARACTERS_DIR  = Path()
OBJECTS_DIR     = Path()


def shows_dir(project_root: Path) -> Path:
    return project_root / "shows"


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


def _pick_lang(value, lang: str) -> str:
    """Извлекает строку из value:
      - если value строка → возвращает как есть (любой язык, fallback)
      - если value dict {ru, uk, en, ...} → берёт по lang, fallback на ru/en/любой
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return (value.get(lang) or value.get('ru') or value.get('en')
                or next(iter([v for v in value.values() if v]), ""))
    return str(value)


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
    global SHOW_ROOT, ENV_FILE, PROMPTS_DIR, STORYBOARDS_DIR
    global LOCATIONS_DIR, CHARACTERS_DIR, OBJECTS_DIR
    ENV_FILE = project_root / ".env"
    if not show_name:
        SHOW_ROOT       = project_root / "shows" / "_none_"
        PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
        STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
        LOCATIONS_DIR   = SHOW_ROOT / "refs" / "locations"
        CHARACTERS_DIR  = SHOW_ROOT / "refs" / "characters"
        OBJECTS_DIR     = SHOW_ROOT / "refs" / "objects"
        return
    SHOW_ROOT       = project_root / "shows" / show_name
    PROMPTS_DIR     = SHOW_ROOT / "output" / "prompts"
    STORYBOARDS_DIR = SHOW_ROOT / "output" / "storyboards"
    refs            = SHOW_ROOT / "refs"
    LOCATIONS_DIR   = refs / "locations"
    CHARACTERS_DIR  = refs / "characters"
    OBJECTS_DIR     = refs / "objects"
    STORYBOARDS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def list_episodes() -> List[str]:
    """Список ID эпизодов в активном сериале (`ep20`, `ep21`...). Сортировка по номеру."""
    seen: set = set()
    for f in STORYBOARDS_DIR.glob("*_block_*_shot*.jpg"):
        m = re.match(r'(ep\d+)_block_', f.name)
        if m:
            seen.add(m.group(1))
    for p in PROMPTS_DIR.glob("*_block_*.txt"):
        m = re.match(r'(ep\d+)_block_', p.name)
        if m:
            seen.add(m.group(1))
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


def block_total_duration(block_name: str) -> int:
    pf = PROMPTS_DIR / f"{block_name}.txt"
    if not pf.exists():
        return 0
    total = 0
    try:
        for shot in parse_shots(pf.read_text(encoding="utf-8")):
            if not shot.get("is_blank"):
                m = re.search(r'(\d+)', shot.get("duration", ""))
                if m:
                    total += int(m.group(1))
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
# Поддерживаемые языки UI: код, отображаемый заголовок (с флагом),
# и полное название в выпадающем списке.
SUPPORTED_LANGUAGES = [
    ('ru', '🇷🇺 РУС', 'Русский'),
    ('uk', '🇺🇦 УКР', 'Українська'),
    ('en', '🇬🇧 ENG', 'English'),
]

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    'ru': {
        'tab_editor': 'Редактор', 'tab_settings': 'Настройки',
        'series': 'Сериал:', 'no_shows': 'В этом проекте нет сериалов',
        'no_episodes': 'В сериале «{show}» пока нет эпизодов',
        'ep_short': 'ЭП', 'block': 'Блок', 'empty_shot': 'ПУСТО',
        'overlay_regen': '↻\n\nПЕРЕГЕНЕРИРОВАТЬ',
        'overlay_edit':  '✎\n\nИЗМЕНИТЬ',
        'save_png': '💾  Сохранить стриборд как PNG',
        'sec_project': 'ПРОЕКТ', 'sec_about': 'О ПРИЛОЖЕНИИ',
        'open_folder': 'Открыть папку проекта',
        'app_version': 'Версия приложения', 'project_version': 'Версия проекта',
        'send_update_title': 'Отправить обновление',
        'send_update_desc': 'Запушить текущую версию проекта в GitHub для коллег',
        'send_update_btn': '↑  Отправить обновление',
        'edit_dialog_title': 'Изменить SHOT {n}',
        'edit_dialog_q': 'Что изменить в этом шоте?',
        'edit_dialog_hint': 'Опиши коротко (русский / английский). Композиция, стиль и остальные элементы сохранятся.',
        'edit_dialog_placeholder': 'Например: убери девушку слева; смени костюм на чёрный…',
        'edit_dialog_send': '↑  Отправить', 'edit_dialog_cancel': 'Отмена',
        'edit_no_image_title': 'Сначала сгенерируй шот',
        'edit_no_image_msg': 'Чтобы редактировать шот, у него должна быть исходная картинка.\nСначала сделай обычную регенерацию (наведи курсор на шот → ↻).',
        'status_regenerating': 'Регенерирую SHOT {n} в {block}…',
        'status_editing': 'Применяю изменения к SHOT {n}…',
        'status_already_genning': 'SHOT {n} уже генерируется — подожди…',
        'status_no_shots': 'Нет шотов для экспорта',
        'status_saved': 'Сохранено: {path}',
        'status_shot_done': 'SHOT {n} обновлён ✓',
        'status_shot_done_other': 'SHOT {n} в [{block}] обновлён ✓',
        'status_loading_stats': 'загружаю статистику…',
        'status_no_stats': 'нет данных о скачиваниях',
        'downloads_format': 'v{ver}: {n} скач.',
    },
    'uk': {
        'tab_editor': 'Редактор', 'tab_settings': 'Налаштування',
        'series': 'Серіал:', 'no_shows': 'У цьому проєкті немає серіалів',
        'no_episodes': 'У серіалі «{show}» поки немає епізодів',
        'ep_short': 'ЕП', 'block': 'Блок', 'empty_shot': 'ПОРОЖНЬО',
        'overlay_regen': '↻\n\nПЕРЕГЕНЕРУВАТИ',
        'overlay_edit':  '✎\n\nЗМІНИТИ',
        'save_png': '💾  Зберегти стриборд як PNG',
        'sec_project': 'ПРОЄКТ', 'sec_about': 'ПРО ДОДАТОК',
        'open_folder': 'Відкрити папку проєкту',
        'app_version': 'Версія додатку', 'project_version': 'Версія проєкту',
        'send_update_title': 'Надіслати оновлення',
        'send_update_desc': 'Запушити поточну версію проєкту на GitHub для колег',
        'send_update_btn': '↑  Надіслати оновлення',
        'edit_dialog_title': 'Змінити SHOT {n}',
        'edit_dialog_q': 'Що змінити в цьому шоті?',
        'edit_dialog_hint': 'Опиши коротко (українською / англійською). Композиція, стиль та інші елементи збережуться.',
        'edit_dialog_placeholder': 'Наприклад: прибери дівчину зліва; зміни костюм на чорний…',
        'edit_dialog_send': '↑  Надіслати', 'edit_dialog_cancel': 'Скасувати',
        'edit_no_image_title': 'Спочатку згенеруй шот',
        'edit_no_image_msg': 'Щоб редагувати шот, у нього має бути вихідна картинка.\nСпочатку зроби звичайну регенерацію (наведи курсор на шот → ↻).',
        'status_regenerating': 'Регенерую SHOT {n} у {block}…',
        'status_editing': 'Застосовую зміни до SHOT {n}…',
        'status_already_genning': 'SHOT {n} вже генерується — зачекай…',
        'status_no_shots': 'Немає шотів для експорту',
        'status_saved': 'Збережено: {path}',
        'status_shot_done': 'SHOT {n} оновлено ✓',
        'status_shot_done_other': 'SHOT {n} у [{block}] оновлено ✓',
        'status_loading_stats': 'завантажую статистику…',
        'status_no_stats': 'немає даних про завантаження',
        'downloads_format': 'v{ver}: {n} зав.',
    },
    'en': {
        'tab_editor': 'Editor', 'tab_settings': 'Settings',
        'series': 'Series:', 'no_shows': 'No series in this project yet',
        'no_episodes': 'No episodes in series "{show}" yet',
        'ep_short': 'EP', 'block': 'Block', 'empty_shot': 'EMPTY',
        'overlay_regen': '↻\n\nREGENERATE',
        'overlay_edit':  '✎\n\nEDIT',
        'save_png': '💾  Save storyboard as PNG',
        'sec_project': 'PROJECT', 'sec_about': 'ABOUT',
        'open_folder': 'Open project folder',
        'app_version': 'App version', 'project_version': 'Project version',
        'send_update_title': 'Send update',
        'send_update_desc': 'Push the current project version to GitHub for colleagues',
        'send_update_btn': '↑  Send update',
        'edit_dialog_title': 'Edit SHOT {n}',
        'edit_dialog_q': 'What to change in this shot?',
        'edit_dialog_hint': 'Describe briefly (any language). Composition, style and other elements will stay.',
        'edit_dialog_placeholder': 'For example: remove the woman on the left; change suit to black…',
        'edit_dialog_send': '↑  Send', 'edit_dialog_cancel': 'Cancel',
        'edit_no_image_title': 'Generate the shot first',
        'edit_no_image_msg': 'To edit a shot it must already have a source image.\nDo a regular regeneration first (hover the shot → ↻).',
        'status_regenerating': 'Regenerating SHOT {n} in {block}…',
        'status_editing': 'Applying changes to SHOT {n}…',
        'status_already_genning': 'SHOT {n} is already generating — wait…',
        'status_no_shots': 'No shots to export',
        'status_saved': 'Saved: {path}',
        'status_shot_done': 'SHOT {n} updated ✓',
        'status_shot_done_other': 'SHOT {n} in [{block}] updated ✓',
        'status_loading_stats': 'loading stats…',
        'status_no_stats': 'no download data',
        'downloads_format': 'v{ver}: {n} dl.',
    },
}


def get_lang() -> str:
    """Активный язык UI из QSettings (default: ru)."""
    try:
        s = QSettings(APP_ORG, APP_NAME)
        v = str(s.value("ui_lang", "ru") or "ru")
        return v if v in [c for c, _, _ in SUPPORTED_LANGUAGES] else "ru"
    except Exception:
        return "ru"


def set_lang(lang: str) -> None:
    QSettings(APP_ORG, APP_NAME).setValue("ui_lang", lang)


def tr(key: str, **kwargs) -> str:
    """Перевод по ключу. Fallback на русский, потом на сам ключ."""
    lang = get_lang()
    table = TRANSLATIONS.get(lang, TRANSLATIONS['ru'])
    text  = table.get(key) or TRANSLATIONS['ru'].get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


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
    """Возвращает info об asset (mac zip) для релиза app-vX.Y.Z, или None."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases"
        r = requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return None
        for rel in r.json():
            if rel.get("tag_name") == f"app-v{version}":
                for asset in rel.get("assets", []):
                    name = asset.get("name", "")
                    if name.endswith(".zip") and "mac" in name.lower():
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
QMainWindow                 { background: #0d0a14; }
QWidget                     { color: #e0e0e0; font-family: -apple-system, "Segoe UI", Helvetica Neue; }
QWidget#main-bg {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #2a1845, stop:0.35 #161020, stop:0.65 #1a0f1f, stop:1 #3a1525);
}
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

QPushButton#save {
    background: #1a1e2a; border: 1px solid #2a2e3f; color: #b0c4ff;
    font-size: 13px; padding: 11px; font-weight: 500;
}
QPushButton#save:hover      { background: #1f2430; }
QPushButton#secondary       { background: #1d1727; font-size: 12px; color: #aaa; }
QPushButton#secondary:hover { color: #ddd; }

/* Pills — эпизоды МЕНЬШЕ, блоки БОЛЬШЕ (по макету).
   border-radius: 100px — «безопасно большой» радиус, гарантирует полностью
   скруглённую (pill) форму вне зависимости от итоговой высоты кнопки. */
QPushButton#pill {
    background: rgba(34, 26, 48, 0.7); border: 1px solid #322545;
    border-radius: 100px; padding: 5px 14px; color: #b0a8c0; font-size: 11px;
    font-weight: 600; min-height: 14px; min-width: 30px;
}
QPushButton#pill:hover  { background: rgba(46, 36, 64, 0.9); color: #ddd; }
QPushButton#pill[active="true"] {
    background: #e63946; border: 1px solid #e63946; color: #fff;
}
QPushButton#pill[active="true"]:hover { background: #d6313c; }

QPushButton#pill-block {
    background: rgba(28, 22, 40, 0.7); border: 1px solid #2a2238;
    border-radius: 100px; padding: 10px 24px; color: #a89fb8; font-size: 13px;
    font-weight: 500; min-height: 20px;
}
QPushButton#pill-block:hover { background: rgba(40, 32, 56, 0.9); color: #ddd; }

/* Блок с непросмотренными шотами — оранжевый акцент (как бейдж NEW на карточке) */
QPushButton#pill-block[unseen="true"] {
    background: rgba(74, 48, 16, 0.55); border: 1px solid #6a4520; color: #ffcc66;
    font-weight: 600;
}
QPushButton#pill-block[unseen="true"]:hover {
    background: rgba(90, 60, 20, 0.7); color: #ffd680;
}

/* АКТИВНЫЙ блок — самый заметный: насыщенный фиолетовый + светлая обводка 2px.
   Перекрывает unseen-стиль чтобы юзер всегда видел, на каком блоке он сейчас. */
QPushButton#pill-block[active="true"] {
    background: rgba(95, 70, 165, 1.0); border: 2px solid #c8a8ff;
    color: #fff; font-weight: 700;
}
QPushButton#pill-block[active="true"]:hover {
    background: rgba(110, 85, 180, 1.0);
}
/* Активный блок с непросмотренными шотами — обводка светло-оранжевая,
   фон тоже оранжевый (но насыщеннее), сохраняем цветовую идентификацию NEW */
QPushButton#pill-block[active="true"][unseen="true"] {
    background: rgba(140, 90, 30, 1.0); border: 2px solid #ffcc66;
    color: #fff; font-weight: 700;
}
QPushButton#pill-block[active="true"][unseen="true"]:hover {
    background: rgba(160, 105, 35, 1.0);
}

/* Карточка шота — без кнопки снизу, регенерация по hover-overlay */
QFrame#card {
    background: rgba(20, 16, 30, 0.85); border: 1px solid #2a2238; border-radius: 10px;
}
QFrame#card:hover { border-color: #4a3d65; }

/* Hover overlay — полупрозрачная плашка с двумя кнопками действий */
QFrame#regen-overlay {
    background: rgba(0, 0, 0, 0.82); border-radius: 8px;
}
QPushButton#overlay-action {
    background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 10px; padding: 6px 8px; color: #fff;
    font-size: 12px; font-weight: 600; text-align: center;
}
QPushButton#overlay-action:hover {
    background: rgba(230, 57, 70, 0.55); border: 1px solid rgba(230, 57, 70, 0.9);
}
QPushButton#overlay-action:pressed {
    background: rgba(180, 40, 50, 0.65);
}

/* Header — LUMZ + красный квадрат + Storyboard Studio (всё в одной rich-text QLabel) */
QLabel#header-version    { font-size: 12px; color: #666; }

/* Tabs — Редактор / Настройки. Прижаты к ЛЕВОМУ краю (по макету) */
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

/* Заголовки */
QLabel#episode-title    { font-size: 16px; color: #fff; font-weight: 500; }
QLabel#episode-duration { font-size: 13px; color: #888; }
QLabel#block-title      { font-size: 12px; color: #777; font-weight: 600; letter-spacing: 1.5px; }

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
QComboBox {
    background: #221a30; border: 1px solid #322545; border-radius: 6px;
    padding: 6px 14px; color: #ddd; font-size: 13px; min-width: 160px;
}
QComboBox:hover         { border-color: #4a3d65; }
QComboBox::drop-down    { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #1a1424; border: 1px solid #322545; selection-background-color: #2a1f3a;
    color: #ddd; padding: 4px;
}

/* Переключатель языка в шапке — крупная кнопка-пилюля + кастомный QMenu */
QPushButton#lang-btn {
    background: rgba(34, 26, 48, 0.7); border: 1px solid #322545;
    border-radius: 100px; padding: 7px 16px; color: #fff;
    font-size: 13px; font-weight: 600; min-width: 110px; text-align: center;
}
QPushButton#lang-btn:hover {
    background: rgba(50, 38, 72, 0.9); border-color: #5a4880;
}
QPushButton#lang-btn:pressed { background: rgba(40, 30, 58, 1.0); }

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
    lines = [l.strip() for l in ENV_FILE.read_text().splitlines() if l.strip()]
    return lines[0]


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

    return f"{header_new.strip()}\n\n{panel_body}"


# ─── Утилиты — изображения ───────────────────────────────────────────────────

def shot_path(block_name: str, shot_idx: int) -> Path:
    """Путь к отдельному файлу шота: {block}_shot{N}.jpg (N с 1)."""
    return STORYBOARDS_DIR / f"{block_name}_shot{shot_idx + 1}.jpg"


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


def stitch_shots_to_landscape(block_name: str, dest: Path) -> None:
    """Склеивает все 9:16 шоты блока в одну 16:9 картинку (4 в ряд) и сохраняет.

    Пустые позиции (где нет файла) заполняются белым.
    """
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

    fmt = "PNG" if dest.suffix.lower() == ".png" else "JPEG"
    if fmt == "JPEG":
        canvas.save(dest, format=fmt, quality=95)
    else:
        canvas.save(dest, format=fmt)


# ─── Поток генерации ─────────────────────────────────────────────────────────

class GenerateThread(QThread):
    progress = pyqtSignal(str)
    step     = pyqtSignal(str, int)   # (label, percent)
    finished = pyqtSignal(int)        # elapsed seconds
    error    = pyqtSignal(str)

    def __init__(self, block_name: str, panel_idx: int,
                 edit_instruction: Optional[str] = None):
        """
        Если `edit_instruction` задан — режим редактирования:
          • существующий файл шота загружается как ЕДИНСТВЕННЫЙ реф [@]img1
          • генерируется новый промпт «изменить только это, остальное оставить»
          • новая картинка пишется поверх старой
        Иначе — обычная регенерация по промпту блока + рефы локаций/персонажей.
        """
        super().__init__()
        self.block_name       = block_name
        self.panel_idx        = panel_idx
        self.edit_instruction = (edit_instruction or "").strip() or None

    def _upload_file(self, session: requests.Session, path: Path) -> str:
        """Загружает файл в Fast Gen storage, возвращает file_hash. Кеширует по resolved-path."""
        cache_key = str(path.resolve())
        if cache_key in _upload_cache:
            return _upload_cache[cache_key]
        ext  = path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png"}.get(ext, "image/jpeg")
        with open(path, "rb") as f:
            r = session.post(f"{STORAGE_BASE}/upload",
                             files={"file": (path.name, f, mime)}, timeout=60)
        r.raise_for_status()
        data = r.json()
        fh   = data.get("file_hash") or data.get("file") or data.get("hash") or ""
        _upload_cache[cache_key] = fh
        return fh

    def _build_edit_prompt(self, instruction: str) -> str:
        """Строит промпт для edit-режима: img1 = текущий шот, инструкция, всё остальное оставить."""
        return (
            "[@]img1 is the current storyboard panel — pencil sketch, "
            "black and white, vertical 9:16 format.\n\n"
            f"MODIFICATION REQUESTED: {instruction}\n\n"
            "Apply ONLY the requested modification. Keep ALL other elements "
            "EXACTLY identical to [@]img1: composition, framing, camera angle, "
            "remaining characters, their poses and expressions, lighting, "
            "background, the pencil sketch art style. Do not redraw or restyle. "
            "Output: single vertical 9:16 panel, same pencil sketch black and white style."
        )

    def run(self):
        start_time = time.time()
        try:
            key     = load_api_key()
            session = requests.Session()
            session.headers["X-API-Key"] = key

            ref_hashes: List[str] = []
            clean: str = ""

            if self.edit_instruction:
                # ── EDIT-режим ─────────────────────────────────────────────
                # Существующий файл шота → единственный реф.
                # Если файла нет — невозможно редактировать (нечего изменять).
                existing = shot_path(self.block_name, self.panel_idx)
                if not existing.exists():
                    self.error.emit(
                        f"Edit невозможен: исходного файла шота нет ({existing.name}). "
                        "Сначала сделай обычную регенерацию.")
                    return
                self.step.emit("Загружаю текущий шот…", 10)
                ref_hashes = [self._upload_file(session, existing)]
                clean = self._build_edit_prompt(self.edit_instruction)
            else:
                # ── Обычная регенерация ───────────────────────────────────
                prompt_file = PROMPTS_DIR / f"{self.block_name}.txt"
                if not prompt_file.exists():
                    self.error.emit(f"Промпт не найден: {prompt_file.name}")
                    return

                prompt_text = prompt_file.read_text(encoding="utf-8")
                refs        = parse_refs(prompt_text)
                clean       = extract_shot_prompt(prompt_text, self.panel_idx) or ""
                if not clean:
                    self.error.emit(
                        f"SHOT {self.panel_idx + 1}: панель пустая или Panel "
                        f"{self.panel_idx + 1} не найден в промпте {prompt_file.name}")
                    return

                if refs:
                    n = len(refs)
                    sorted_tags = sorted(refs, key=lambda t: int(re.search(r'\d+', t).group()))
                    for idx, tag in enumerate(sorted_tags):
                        ref_hashes.append(self._upload_file(session, refs[tag]))
                        pct = 5 + int((idx + 1) / n * 20)
                        self.step.emit(f"Загружаю рефы ({idx+1}/{n})…", pct)

            self.step.emit("Отправляю запрос…", 28)

            payload: Dict = {
                "prompt":       clean,
                "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",   # 9:16 — отдельный шот
                "model":        MODEL,
            }
            if ref_hashes:
                payload["reference_images"] = ref_hashes

            r = session.post(f"{API_BASE}/api/v4/flow/image/generate",
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("operation_id"):
                self.error.emit(f"No operation_id: {data}")
                return

            op_id      = data["operation_id"]
            poll_count = 0
            self.step.emit("Генерирую…", 30)

            while True:
                time.sleep(4)
                r = session.get(f"{API_BASE}/api/v4/operations/{op_id}", timeout=30)
                r.raise_for_status()
                data   = r.json()
                status = data.get("status")
                poll_count += 1
                pct = min(85, 30 + int(poll_count / 20 * 55))
                self.step.emit(f"Генерирую… ({poll_count * 4}с)", pct)
                self.progress.emit(f"Статус: {status}…")

                if status == "success":
                    result = data.get("result") or []
                    uri    = result[0] if isinstance(result, list) else result
                    if isinstance(uri, dict):
                        uri = uri.get("url") or uri.get("ref") or uri.get("file_hash") or ""
                    uri = str(uri)
                    if uri.startswith("data:"):
                        _, b64 = uri.split(",", 1)
                        image_bytes = base64.b64decode(b64)
                    else:
                        fh  = uri[5:] if uri.startswith("file:") else uri
                        r2  = session.get(f"{STORAGE_BASE}/file/{fh}/raw", timeout=120)
                        r2.raise_for_status()
                        image_bytes = r2.content
                    break
                if status == "error":
                    self.error.emit(f"API error: {data.get('error')}")
                    return

            self.step.emit("Сохраняю шот…", 92)
            # Каждый шот — отдельный файл {block}_shot{N}.jpg в формате 9:16
            shot_file = shot_path(self.block_name, self.panel_idx)
            shot_file.write_bytes(image_bytes)

            elapsed = max(0, int(time.time() - start_time))
            self.step.emit("Готово!", 100)
            self.finished.emit(elapsed)

        except Exception as e:
            self.error.emit(str(e))


# ─── Обновления — потоки ─────────────────────────────────────────────────────

class CheckUpdateThread(QThread):
    """Проверяет наличие новых версий проекта и приложения на GitHub."""
    # curr_proj, latest_proj, curr_app, latest_app
    update_found = pyqtSignal(str, str, str, str)
    no_update    = pyqtSignal()
    error        = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            if not github_configured():
                self.no_update.emit()
                return

            curr_proj = read_local_version(self.root)
            curr_app  = read_local_app_version(self.root)

            r = requests.get(github_raw_url("version.json"), timeout=10)
            r.raise_for_status()
            latest_proj = r.json().get("version", curr_proj)

            latest_app = fetch_latest_app_release_version() or curr_app

            if version_gt(latest_proj, curr_proj) or version_gt(latest_app, curr_app):
                self.update_found.emit(curr_proj, latest_proj, curr_app, latest_app)
            else:
                self.no_update.emit()
        except Exception as e:
            self.error.emit(str(e))


class DownloadUpdateThread(QThread):
    """Скачивает и применяет обновление с GitHub."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str)   # new version
    error    = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            self.progress.emit("Скачиваю обновление…", 5)
            r = requests.get(github_zip_url(), timeout=120, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            buf = io.BytesIO()
            done = 0
            for chunk in r.iter_content(chunk_size=16384):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 5 + int(done / total * 60)
                    self.progress.emit(f"Скачиваю… {done // 1024} КБ", pct)

            self.progress.emit("Распаковка…", 70)
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp)
                extracted = next(Path(tmp).iterdir())  # первая (единственная) папка

                self.progress.emit("Применяю изменения…", 85)
                copied = 0
                for src in extracted.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(extracted)
                    if rel.parts and rel.parts[0] in PRESERVE_ON_UPDATE:
                        continue
                    dst = self.root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1

            new_version = read_local_version(self.root)
            self.progress.emit(f"Обновлено! ({copied} файлов)", 100)
            self.finished.emit(new_version)
        except Exception as e:
            self.error.emit(str(e))


class DownloadAppUpdateThread(QThread):
    """Скачивает и устанавливает новую версию Storyboard Studio.app из GitHub Releases."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str, str)   # (new_app_version, install_path)
    error    = pyqtSignal(str)

    def __init__(self, target_version: str):
        super().__init__()
        self.target_version = target_version

    def run(self):
        try:
            self.progress.emit("Ищу релиз на GitHub…", 5)
            asset = fetch_release_asset_info(self.target_version)
            if not asset:
                self.error.emit(
                    f"Не найден .zip в релизе app-v{self.target_version}.\n"
                    "Попробуй обновить вручную — скачай с GitHub Releases.")
                return

            download_url = asset["browser_download_url"]
            size_bytes   = asset.get("size", 0)
            size_mb      = max(1, size_bytes // (1024 * 1024))

            self.progress.emit(f"Скачиваю приложение ({size_mb} МБ)…", 8)
            r = requests.get(download_url, timeout=600, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", size_bytes))
            buf   = io.BytesIO()
            done  = 0
            for chunk in r.iter_content(chunk_size=65536):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 8 + int(done / total * 60)
                    self.progress.emit(
                        f"Скачиваю… {done // (1024*1024)} / {total // (1024*1024)} МБ", pct)

            self.progress.emit("Распаковка…", 70)

            app_bundle  = find_current_app_bundle()
            install_dir = app_bundle.parent if app_bundle else (Path.home() / "Downloads")
            app_name    = app_bundle.name   if app_bundle else "Storyboard Studio.app"

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp_path)

                apps = list(tmp_path.rglob("*.app"))
                if not apps:
                    self.error.emit("В архиве не найдено .app приложение.")
                    return
                new_app_src = apps[0]

                self.progress.emit("Устанавливаю…", 82)
                dest = install_dir / app_name
                bak  = install_dir / (app_name + ".bak")

                try:
                    if dest.exists():
                        if bak.exists():
                            shutil.rmtree(bak, ignore_errors=True)
                        dest.rename(bak)
                    shutil.copytree(new_app_src, dest)
                    if bak.exists():
                        shutil.rmtree(bak, ignore_errors=True)
                except PermissionError:
                    dest = Path.home() / "Downloads" / app_name
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(new_app_src, dest)

            self.progress.emit("Готово!", 100)
            self.finished.emit(self.target_version, str(dest))
        except Exception as e:
            self.error.emit(str(e))


class SendUpdateThread(QThread):
    """Админ-режим: бампит версию + git commit + git push.
    Опционально — загружает Storyboard Studio.app в GitHub Releases.
    """
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, str, bool)   # (project_version, app_version, app_uploaded)
    error    = pyqtSignal(str)

    def __init__(self, root: Path, upload_app: bool = False):
        super().__init__()
        self.root       = root
        self.upload_app = upload_app

    def run(self):
        try:
            vfile = self.root / "version.json"
            data = json.loads(vfile.read_text(encoding="utf-8")) if vfile.exists() \
                   else {"version": "1.0.0", "app_version": "1.0.0"}

            major, minor, patch = data.get("version", "1.0.0").split(".")
            new_version = f"{major}.{minor}.{int(patch) + 1}"
            data["version"] = new_version

            cur_app_v = data.get("app_version", data.get("version", "1.0.0"))
            new_app_version = cur_app_v
            if self.upload_app:
                amaj, amin, apat = cur_app_v.split(".")
                new_app_version = f"{amaj}.{amin}.{int(apat) + 1}"
                data["app_version"] = new_app_version

            data["released"] = datetime.date.today().isoformat()
            vfile.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            self.progress.emit("Готовлю коммит…")
            subprocess.run(["git", "-C", str(self.root), "add", "-A"],
                           check=True, capture_output=True, timeout=30)
            r = subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", f"Update {new_version}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 and "nothing to commit" not in r.stdout:
                self.error.emit(f"Git commit error: {r.stderr or r.stdout}")
                return

            self.progress.emit("Отправляю на GitHub…")
            r = subprocess.run(
                ["git", "-C", str(self.root), "push", "origin", GITHUB_BRANCH],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                self.error.emit(f"Git push error: {r.stderr}")
                return

            uploaded = False
            if self.upload_app:
                app_path = self.root / "dist" / "Storyboard Studio.app"
                if not app_path.exists():
                    self.error.emit(
                        f"Не найден {app_path}.\n"
                        "Сначала пересобери приложение командой:\n"
                        "python3 -m PyInstaller StoryboardStudio.spec --noconfirm")
                    return

                token = get_github_token_from_remote(self.root)
                if not token:
                    self.error.emit(
                        "Не нашёл GitHub token в URL origin.\n"
                        "Чтобы загрузить .app в Releases — настрой git remote с токеном:\n"
                        "git remote set-url origin https://TOKEN@github.com/USER/REPO.git")
                    return

                self.progress.emit("Архивирую Storyboard Studio.app…")
                zip_name = f"Storyboard-Studio-{new_app_version}-mac.zip"
                zip_path = self.root / "dist" / zip_name
                if zip_path.exists():
                    zip_path.unlink()
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    for f in app_path.rglob("*"):
                        if f.is_file() or f.is_symlink():
                            zf.write(f, f.relative_to(app_path.parent))

                self.progress.emit("Создаю GitHub Release…")
                tag = f"app-v{new_app_version}"
                rel = create_github_release(
                    token, tag,
                    name=f"Storyboard Studio v{new_app_version}",
                    body=f"Версия приложения: {new_app_version}\n"
                         f"Версия проекта на момент сборки: {new_version}",
                )
                if not rel:
                    self.error.emit(
                        "Не удалось создать GitHub Release. Проверь токен и права (нужен scope 'repo').")
                    return

                size_mb = zip_path.stat().st_size // (1024 * 1024)
                self.progress.emit(f"Загружаю .app ({size_mb} МБ) в Release…")
                if not upload_release_asset(token, rel["upload_url"], zip_path):
                    self.error.emit("Не удалось загрузить .app в GitHub Release.")
                    return

                # Удаляем zip после успешной загрузки — он больше не нужен,
                # коллеги скачивают его прямо с GitHub Releases.
                try:
                    zip_path.unlink()
                except Exception:
                    pass

                uploaded = True

            self.finished.emit(new_version, new_app_version, uploaded)
        except subprocess.CalledProcessError as e:
            self.error.emit(f"Ошибка git: {e.stderr.decode() if e.stderr else str(e)}")
        except Exception as e:
            self.error.emit(str(e))


class FetchStatsThread(QThread):
    """Загружает статистику скачиваний из GitHub Releases (только для admin)."""
    finished = pyqtSignal(list)   # list of {tag, version, downloads}

    def run(self):
        self.finished.emit(fetch_all_release_stats())


# ─── Карточка шота ───────────────────────────────────────────────────────────

class ShotCard(QFrame):
    regen_requested = pyqtSignal(int)
    edit_requested  = pyqtSignal(int)   # запрос на edit-попап
    CARD_W, CARD_H  = 200, 356

    def __init__(self, panel_idx: int, parent=None):
        super().__init__(parent)
        self.panel_idx = panel_idx
        # Запоминаем что шот пустой/blank — чтобы overlay не показывался
        self._is_blank = False
        self._is_loading = False
        self.setObjectName("card")
        # Фиксированная ширина — чтобы пустые и с картинкой шоты были РОВНО
        # одной ширины (иначе sizeHint от desc_label делает их разной ширины).
        self.setFixedWidth(self.CARD_W + 20)
        self._build()
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.setContentsMargins(10, 10, 10, 10)

        # ── Зона изображения с hover-overlay ────────────────────────────────
        # Контейнер чтобы overlay позиционировался относительно картинки
        self.img_container = QWidget()
        self.img_container.setFixedSize(self.CARD_W, self.CARD_H)
        self.img_label = QLabel("нет изображения", self.img_container)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setGeometry(0, 0, self.CARD_W, self.CARD_H)
        self.img_label.setStyleSheet(
            "background:#1a1424; border-radius:6px; color:#333; font-size:12px;")

        # Hover-overlay — полупрозрачная плашка с ДВУМЯ кнопками:
        #   ↻ ПЕРЕГЕНЕРИРОВАТЬ  — обычная регенерация (по промпту блока)
        #   ✎ ИЗМЕНИТЬ          — edit-режим (попап с инструкцией)
        self.regen_overlay = QFrame(self.img_container)
        self.regen_overlay.setObjectName("regen-overlay")
        self.regen_overlay.setGeometry(0, 0, self.CARD_W, self.CARD_H)
        ov_lay = QVBoxLayout(self.regen_overlay)
        ov_lay.setContentsMargins(18, 18, 18, 18)
        ov_lay.setSpacing(10)

        self.overlay_regen_btn = QPushButton(tr('overlay_regen'))
        self.overlay_regen_btn.setObjectName("overlay-action")
        self.overlay_regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_regen_btn.clicked.connect(
            lambda: self.regen_requested.emit(self.panel_idx))
        ov_lay.addWidget(self.overlay_regen_btn)

        self.overlay_edit_btn = QPushButton(tr('overlay_edit'))
        self.overlay_edit_btn.setObjectName("overlay-action")
        self.overlay_edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_edit_btn.clicked.connect(
            lambda: self.edit_requested.emit(self.panel_idx))
        ov_lay.addWidget(self.overlay_edit_btn)

        self.regen_overlay.hide()

        lay.addWidget(self.img_container, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        lay.addWidget(self.progress_bar)

        self.step_label = QLabel("")
        self.step_label.setObjectName("step-label")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.step_label.hide()
        lay.addWidget(self.step_label)

        row = QHBoxLayout()
        self.num_label = QLabel(f"SHOT {self.panel_idx + 1}")
        self.num_label.setObjectName("shot-num")
        # Бейдж NEW — показывается после регенерации, исчезает при переключении блока
        self.new_badge = QLabel("NEW")
        self.new_badge.setObjectName("new-badge")
        self.new_badge.hide()
        # Время генерации шота — стоит сразу после NEW, исчезает вместе с ним
        self.gen_time_label = QLabel("")
        self.gen_time_label.setObjectName("gen-time")
        self.gen_time_label.hide()
        self.dur_label = QLabel("")
        self.dur_label.setObjectName("shot-dur")
        row.addWidget(self.num_label)
        row.addSpacing(6)
        row.addWidget(self.new_badge)
        row.addSpacing(4)
        row.addWidget(self.gen_time_label)
        row.addStretch()
        row.addWidget(self.dur_label)
        lay.addLayout(row)

        self.desc_label = QLabel("")
        self.desc_label.setObjectName("shot-desc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumWidth(self.CARD_W + 20)
        lay.addWidget(self.desc_label)

        lay.addStretch()

        # Скрытые кнопки для обратной совместимости с set_loading логикой —
        # реальное взаимодействие через hover-overlay (overlay_regen_btn / overlay_edit_btn)
        self.regen_btn = QPushButton()
        self.regen_btn.hide()
        self.edit_btn = QPushButton()
        self.edit_btn.hide()

    def enterEvent(self, ev):
        """Hover на карточку → показать overlay (если шот валиден и не грузится)."""
        if not self._is_blank and not self._is_loading:
            self.regen_overlay.show()
            self.regen_overlay.raise_()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        """Уход курсора → скрыть overlay."""
        self.regen_overlay.hide()
        super().leaveEvent(ev)

    def set_image(self, jpeg_bytes: Optional[bytes]):
        if not jpeg_bytes:
            self.img_label.clear()
            self.img_label.setText("ПУСТО")
            return
        pixmap = QPixmap.fromImage(QImage.fromData(jpeg_bytes)).scaled(
            QSize(self.CARD_W, self.CARD_H),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Скругляем углы картинки через QPainterPath-маску. Картинка
        # генерируется с прямыми углами — программно даём ей те же
        # скругления (radius 6px), что у пустых панелей и фона карточки.
        rounded = QPixmap(pixmap.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, pixmap.width(), pixmap.height()), 6, 6)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        self.img_label.setPixmap(rounded)

    def set_shot_info(self, shot: Dict):
        self._is_blank = bool(shot.get("is_blank"))
        if self._is_blank:
            self.num_label.setText(tr('empty_shot'))
            self.dur_label.setText("")
            self.desc_label.setText("")
            self.new_badge.hide()  # для пустого шота нечего быть «новым»
            self.gen_time_label.hide()
            self.regen_overlay.hide()  # пустые шоты не дают hover-overlay
        else:
            self.num_label.setText(f"SHOT {shot['shot_num']}")
            self.dur_label.setText(shot["duration"])
            self.desc_label.setText(shot["description"])

    def apply_lang(self):
        """Перевести тексты overlay-кнопок на текущий язык."""
        self.overlay_regen_btn.setText(tr('overlay_regen'))
        self.overlay_edit_btn.setText(tr('overlay_edit'))

    def set_new_badge(self, visible: bool):
        """Показ/скрытие бейджа NEW (только для НЕ-пустых шотов)."""
        if self._is_blank:
            self.new_badge.hide()
        else:
            self.new_badge.setVisible(bool(visible))

    def set_gen_time(self, seconds: int):
        """Показывает время генерации шота, например '⏱ 42с' или '⏱ 1м 5с'.
        Если 0 или это пустой шот — скрывает метку.
        """
        if self._is_blank or not seconds or seconds <= 0:
            self.gen_time_label.hide()
            self.gen_time_label.setText("")
        else:
            self.gen_time_label.setText(f"⏱ {format_gen_duration(seconds)}")
            self.gen_time_label.show()

    def set_progress(self, label: str, pct: int):
        self.progress_bar.setValue(pct)
        self.step_label.setText(label)
        self.progress_bar.show()
        self.step_label.show()

    def set_loading(self, loading: bool):
        self._is_loading = loading
        if loading:
            self.regen_overlay.hide()  # во время генерации overlay не показываем
        else:
            self.progress_bar.hide()
            self.step_label.hide()
            self.progress_bar.setValue(0)


# ─── Главное окно ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, project_root: Path):
        super().__init__()
        self._project_root = project_root
        self._is_admin     = is_admin_mode(project_root)
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
        # Карточка: 200 (CARD_W) + 10×2 (внутренний padding QFrame) = 220
        # 4 карточки × 220 + 3 spacing × 12 = 916
        # + 28×2 margins tab content = 972
        # + ~10px на чрезмерное паддинг scroll-area
        # ИТОГО минимум 1000 ширина чтобы все 4 шота гарантированно влезли.
        self.setMinimumSize(1000, 900)
        self.resize(1000, 920)
        self.current_block: Optional[str] = None
        # Параллельные регенерации: ключ (block_name, panel_idx) → поток.
        # Каждый шот в каждом блоке может генериться независимо от других.
        self._active_regens: Dict[tuple, GenerateThread] = {}
        self._update_thread:     Optional[QThread]                 = None
        self._app_update_thread: Optional[DownloadAppUpdateThread] = None
        self._stats_thread:      Optional[FetchStatsThread]        = None
        self._latest_app_ver: Optional[str] = None
        # Множество (block, panel_idx) — недавно регенерированные шоты, ещё не
        # просмотренные пользователем. На карточке у них висит бейдж NEW.
        self._unseen_shots: set = set()
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

        # Авто-проверка обновлений через 2 секунды после запуска
        if github_configured():
            QTimer.singleShot(2000, self._check_updates)

        # Для админа: периодически проверяем есть ли изменения для отправки.
        # Кнопка "Отправить обновление" активна только при наличии изменений.
        if self._is_admin:
            self._send_check_timer = QTimer(self)
            self._send_check_timer.timeout.connect(self._refresh_send_button)
            self._send_check_timer.start(5000)   # каждые 5 сек
            QTimer.singleShot(800, self._refresh_send_button)   # первая проверка

        # Статистика скачиваний для админа (из GitHub Releases API)
        if self._is_admin and github_configured():
            QTimer.singleShot(4000, self._fetch_download_stats)

        # Таймер анимации точек у блоков с активной регенерацией.
        # Срабатывает каждые 400ms — циклически меняет ·/··/···
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._dot_timer.start(400)

    def _tick_dots(self):
        """Перебирает шаги анимации точек и обновляет индикаторы у блоков
        где идёт регенерация. Если активных регенераций нет — ничего не делает."""
        if not self._active_regens:
            return
        self._dot_step = (self._dot_step + 1) % 3
        active_blocks = {b for (b, _) in self._active_regens.keys()}
        for b in active_blocks:
            self._refresh_block_indicator(b)

    def _build_ui(self):
        # Центральный виджет с градиентным фоном
        bg = QWidget()
        bg.setObjectName("main-bg")
        self.setCentralWidget(bg)
        main = QVBoxLayout(bg)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        main.addWidget(self._build_header())

        # Тонкая разделительная линия под шапкой LUMZ
        sep1 = QFrame(); sep1.setObjectName("header-divider"); sep1.setFixedHeight(1)
        main.addWidget(sep1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_editor_tab(), tr('tab_editor'))
        self.tabs.addTab(self._build_settings_tab(), tr('tab_settings'))
        main.addWidget(self.tabs, stretch=1)

        # Статус-бар (вариант B): пустой когда нечего показать
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    def _build_header(self) -> QWidget:
        h = QFrame()
        h.setFixedHeight(58)
        lay = QHBoxLayout(h)
        lay.setContentsMargins(28, 12, 28, 12)
        lay.setSpacing(0)

        # LUMZ + красный квадрат-точка возле буквы Z + Storyboard Studio
        # Используем rich-text QLabel чтобы квадрат идеально лёг по baseline.
        logo = QLabel(
            '<span style="color:#fff; font-size:20px; font-weight:700; letter-spacing:1px;">LUMZ</span>'
            '<span style="color:#e63946; font-size:20px; font-weight:900;">▪</span>'
            '<span style="color:#888; font-size:14px;">  Storyboard Studio</span>'
        )
        logo.setTextFormat(Qt.TextFormat.RichText)
        logo.setObjectName("logo-text")
        lay.addWidget(logo, alignment=Qt.AlignmentFlag.AlignVCenter)

        lay.addStretch()

        # Переключатель языка интерфейса — кнопка + кастомный QMenu вместо
        # системного QComboBox (на macOS он рендерится как spinner со стрелками).
        self.lang_btn = QPushButton()
        self.lang_btn.setObjectName("lang-btn")
        self.lang_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lang_menu = QMenu(self.lang_btn)
        self._lang_menu.setObjectName("lang-menu")
        for code, label, full_name in SUPPORTED_LANGUAGES:
            flag = label.split(" ", 1)[0]  # "🇷🇺"
            act = self._lang_menu.addAction(f"  {flag}   {full_name}")
            act.triggered.connect(lambda _checked=False, c=code: self._set_lang(c))
        self.lang_btn.clicked.connect(self._open_lang_menu)
        self._refresh_lang_btn()
        lay.addWidget(self.lang_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        lay.addSpacing(14)

        self.header_version = QLabel(f"v{read_local_app_version(self._project_root)}")
        self.header_version.setObjectName("header-version")
        lay.addWidget(self.header_version, alignment=Qt.AlignmentFlag.AlignVCenter)
        return h

    def _refresh_lang_btn(self):
        """Обновляет текст кнопки языка под текущий выбор: «🇷🇺 РУС ▾»."""
        cur = get_lang()
        for code, label, _full in SUPPORTED_LANGUAGES:
            if code == cur:
                self.lang_btn.setText(f"  {label}   ▾  ")
                return
        self.lang_btn.setText("  ▾  ")

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
        # Tabs
        if hasattr(self, 'tabs'):
            self.tabs.setTabText(0, tr('tab_editor'))
            self.tabs.setTabText(1, tr('tab_settings'))
        # Editor tab
        if hasattr(self, 'show_lbl'):
            self.show_lbl.setText(tr('series'))
        if hasattr(self, 'save_btn'):
            self.save_btn.setText(tr('save_png'))
        # Settings tab
        if hasattr(self, 'sec_project_lbl'):
            self.sec_project_lbl.setText(tr('sec_project'))
        if hasattr(self, 'sec_about_lbl'):
            self.sec_about_lbl.setText(tr('sec_about'))
        if hasattr(self, 'open_folder_btn'):
            self.open_folder_btn.setText(tr('open_folder'))
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

        # Баннер обновления проекта
        self.update_banner = QFrame()
        self.update_banner.setObjectName("update-banner")
        self.update_banner.hide()
        ub_lay = QHBoxLayout(self.update_banner)
        ub_lay.setContentsMargins(14, 10, 10, 10)
        self.update_text = QLabel("")
        self.update_text.setObjectName("update-text")
        ub_lay.addWidget(self.update_text, stretch=1)
        self.update_btn = QPushButton("Обновить →")
        self.update_btn.setObjectName("update-btn")
        self.update_btn.clicked.connect(self._download_update)
        ub_lay.addWidget(self.update_btn)
        lay.addWidget(self.update_banner)

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
        self.show_lbl.setStyleSheet("color: #888; font-size: 13px;")
        show_row.addWidget(self.show_lbl)
        self.show_combo = QComboBox()
        self.show_combo.currentTextChanged.connect(self._on_show_changed)
        show_row.addWidget(self.show_combo)
        show_row.addStretch()
        lay.addLayout(show_row)

        # Эпизоды + название/длительность
        ep_row = QHBoxLayout()
        ep_row.setSpacing(8)
        self.ep_pills_container = QWidget()
        self.ep_pills_layout = QHBoxLayout(self.ep_pills_container)
        self.ep_pills_layout.setContentsMargins(0, 0, 0, 0)
        self.ep_pills_layout.setSpacing(6)
        ep_row.addWidget(self.ep_pills_container)
        ep_row.addSpacing(20)
        sep = QLabel("│")
        sep.setStyleSheet("color: #2a2238; font-size: 14px;")
        ep_row.addWidget(sep)
        ep_row.addSpacing(10)
        self.ep_title_label = QLabel("")
        self.ep_title_label.setObjectName("episode-title")
        ep_row.addWidget(self.ep_title_label)
        ep_row.addStretch()
        self.ep_dur_label = QLabel("")
        self.ep_dur_label.setObjectName("episode-duration")
        ep_row.addWidget(self.ep_dur_label)
        lay.addLayout(ep_row)

        # Блоки (пилюли)
        self.block_pills_container = QWidget()
        self.block_pills_layout = QHBoxLayout(self.block_pills_container)
        self.block_pills_layout.setContentsMargins(0, 0, 0, 0)
        self.block_pills_layout.setSpacing(6)
        blk_row = QHBoxLayout()
        blk_row.addWidget(self.block_pills_container)
        blk_row.addStretch()
        lay.addLayout(blk_row)

        # Заголовок блока («КАМЕРА ЛОРЫ ~8с»)
        self.block_title = QLabel("")
        self.block_title.setObjectName("block-title")
        lay.addWidget(self.block_title)

        # Карточки шотов
        cards_w = QWidget()
        self.cards_row = QHBoxLayout(cards_w)
        self.cards_row.setSpacing(12)
        self.cards_row.setContentsMargins(0, 0, 0, 0)
        self.shot_cards: List[ShotCard] = []
        for i in range(PANELS):
            card = ShotCard(i)
            card.regen_requested.connect(self._on_regen)
            card.edit_requested.connect(self._on_edit_shot)
            self.shot_cards.append(card)
            self.cards_row.addWidget(card)
        self.cards_row.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(scroll, stretch=1)

        # Сохранить как PNG
        self.save_btn = QPushButton(tr('save_png'))
        self.save_btn.setObjectName("save")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_png)
        lay.addWidget(self.save_btn)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
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

        # Строка 1 — версия приложения
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

        # Тонкая разделительная линия между строками
        sep = QFrame()
        sep.setObjectName("settings-divider")
        sep.setFixedHeight(1)
        af.addWidget(sep)

        # Строка 2 — версия проекта
        row_proj = QWidget()
        row_proj.setObjectName("settings-row")
        rp = QHBoxLayout(row_proj)
        rp.setContentsMargins(18, 14, 18, 14)
        self.proj_ver_key_lbl = QLabel(tr('project_version'))
        self.proj_ver_key_lbl.setObjectName("settings-row-key")
        rp.addWidget(self.proj_ver_key_lbl)
        rp.addStretch()
        self.proj_ver_val_lbl = QLabel("")
        self.proj_ver_val_lbl.setObjectName("settings-row-val")
        rp.addWidget(self.proj_ver_val_lbl)
        af.addWidget(row_proj)

        lay.addWidget(about_frame)

        # ── Админ: отправить обновление + статистика ───────────────────────
        if self._is_admin:
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
        return w

    def _refresh_settings_versions(self):
        """Обновляет тексты версий + ключи (язык-зависимые) в настройках."""
        v_app  = read_local_app_version(self._project_root)
        v_proj = read_local_version(self._project_root)
        if hasattr(self, 'app_ver_key_lbl'):
            self.app_ver_key_lbl.setText(tr('app_version'))
        if hasattr(self, 'app_ver_val_lbl'):
            self.app_ver_val_lbl.setText(f"v{v_app}")
        if hasattr(self, 'proj_ver_key_lbl'):
            self.proj_ver_key_lbl.setText(tr('project_version'))
        if hasattr(self, 'proj_ver_val_lbl'):
            self.proj_ver_val_lbl.setText(f"v{v_proj}")
        if hasattr(self, 'header_version'):
            self.header_version.setText(f"v{v_app}")

    # ── Shows / Episodes / Blocks ────────────────────────────────────────────

    def _populate_shows(self):
        """Заполняет дропдаун сериалов и выбирает активный."""
        self.show_combo.blockSignals(True)
        self.show_combo.clear()
        shows = list_shows(self._project_root)
        for s in shows:
            self.show_combo.addItem(s)
        if self._current_show and self._current_show in shows:
            self.show_combo.setCurrentText(self._current_show)
        self.show_combo.blockSignals(False)
        if not shows:
            self.show_combo.setEnabled(False)
            self.show_combo.addItem("(нет сериалов)")

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
        """Перечитать эпизоды/блоки текущего сериала (после изменений на диске)."""
        self._meta = read_episodes_meta(SHOW_ROOT) if self._current_show else {}
        self._populate_episodes()

    def _clear_layout(self, layout: QHBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            wgt = item.widget()
            if wgt is not None:
                wgt.deleteLater()

    def _populate_episodes(self):
        """Перерисовывает ряд пилюль эпизодов активного сериала."""
        self._clear_layout(self.ep_pills_layout)
        self._episode_pills = {}

        if not self._current_show:
            self.ep_title_label.setText(tr('no_shows'))
            self.ep_dur_label.setText("")
            self._populate_blocks()
            return

        eps = list_episodes()
        for ep in eps:
            m = re.match(r'ep(\d+)', ep)
            n = m.group(1) if m else ep
            btn = QPushButton(f"{tr('ep_short')} {n}")
            btn.setObjectName("pill")
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _, e=ep: self._select_episode(e))
            self.ep_pills_layout.addWidget(btn)
            self._episode_pills[ep] = btn

        if eps:
            prev = self._current_episode if self._current_episode in eps else eps[0]
            self._select_episode(prev)
        else:
            self._current_episode = None
            self.ep_title_label.setText(tr('no_episodes', show=self._current_show))
            self.ep_dur_label.setText("")
            self._populate_blocks()

    def _select_episode(self, ep: str):
        self._current_episode = ep
        for e, btn in self._episode_pills.items():
            btn.setProperty("active", e == ep)
            btn.style().unpolish(btn); btn.style().polish(btn)

        title = get_episode_title(self._meta, ep) or ep.upper()
        dur = episode_total_duration(ep)
        self.ep_title_label.setText(title)
        self.ep_dur_label.setText(f"{dur}с" if dur else "")
        self._populate_blocks()

    def _populate_blocks(self):
        """Перерисовывает ряд пилюль блоков для текущего эпизода."""
        self._clear_layout(self.block_pills_layout)
        self._block_pills = {}

        if not self._current_episode:
            self.current_block = None
            self.block_title.setText("")
            for card in self.shot_cards:
                card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                card.set_image(None)
            self.save_btn.setEnabled(False)
            return

        blocks = list_blocks_for_episode(self._current_episode)
        for blk in blocks:
            btn = QPushButton(self._format_block_label(blk))
            btn.setObjectName("pill-block")
            btn.setProperty("active", False)
            # Если у блока есть непросмотренные шоты — сразу выставляем оранжевый акцент
            has_active = any(b == blk for (b, _) in self._active_regens.keys())
            has_unseen = (not has_active) and any(b == blk for (b, _) in self._unseen_shots)
            btn.setProperty("unseen", has_unseen)
            btn.clicked.connect(lambda _, b=blk: self._select_block(b))
            self.block_pills_layout.addWidget(btn)
            self._block_pills[blk] = btn

        if blocks:
            prev = self.current_block if self.current_block in blocks else blocks[0]
            self._select_block(prev)
        else:
            self.current_block = None
            self.block_title.setText("")
            for card in self.shot_cards:
                card.set_shot_info(dict(shot_num=1, duration="", description="", is_blank=True))
                card.set_image(None)
            self.save_btn.setEnabled(False)

    def _select_block(self, name: str):
        if self.current_block and self.current_block != name:
            self._mark_block_seen(self.current_block)
        self.current_block = name
        for b, btn in self._block_pills.items():
            btn.setProperty("active", b == name)
            btn.style().unpolish(btn); btn.style().polish(btn)
        self._display_block(name)

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

    def _display_block(self, name: str):
        prompt_file = PROMPTS_DIR / f"{name}.txt"

        # Заголовок блока: «КАМЕРА ЛОРЫ ~8с» — имя из episodes.json (поддержка
        # ОБЕИХ форм: строка-имя ИЛИ объект {name, shots})
        m = re.match(r'(ep\d+)_block_(\d+)', name)
        ep, blk_n = (m.group(1), m.group(2)) if m else (None, None)
        block_meta: Dict = {"name": "", "shots": {}}
        if ep and blk_n:
            block_meta = get_block_meta(self._meta, ep, blk_n)
            title_part = (block_meta["name"] or f"{tr('block')} {blk_n}").upper()
        else:
            title_part = name.upper()
        dur = block_total_duration(name)
        dur_part = f"   ~{dur}с" if dur else ""
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

    # ── Regeneration ─────────────────────────────────────────────────────────

    def _on_edit_shot(self, panel_idx: int):
        """Открывает попап с полем ввода инструкции для edit-режима регенерации."""
        if not self.current_block:
            return
        target_block = self.current_block
        key = (target_block, panel_idx)
        if key in self._active_regens:
            self.status_bar.showMessage(tr('status_already_genning', n=panel_idx + 1))
            return
        # Файл шота должен существовать — иначе нечего редактировать
        if not shot_path(target_block, panel_idx).exists():
            QMessageBox.information(
                self, tr('edit_no_image_title'), tr('edit_no_image_msg'))
            return

        instruction = self._ask_edit_instruction(panel_idx)
        if not instruction:
            return  # отмена или пусто

        # Запускаем регенерацию в edit-режиме (с инструкцией)
        card = self.shot_cards[panel_idx]
        card.set_loading(True)
        thread = GenerateThread(target_block, panel_idx, edit_instruction=instruction)
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
        self.status_bar.showMessage(tr('status_editing', n=panel_idx + 1))

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

        # Обновляем текущий блок (если виден этот же — увидим новую картинку,
        # если другой — карточки в нём перерисуются с актуальным состоянием
        # оставшихся регенераций).
        if self.current_block:
            self._display_block(self.current_block)
        # Обновляем индикатор у ЦЕЛЕВОГО блока в списке (✓ если все шоты готовы)
        self._refresh_block_indicator(target_block)

        if self.current_block == target_block:
            self.status_bar.showMessage(tr('status_shot_done', n=panel_idx + 1))
        else:
            self.status_bar.showMessage(
                tr('status_shot_done_other', n=panel_idx + 1, block=target_block))

    def _on_regen_error(self, msg: str, target_block: str, panel_idx: int):
        self._active_regens.pop((target_block, panel_idx), None)

        if self.current_block:
            self._display_block(self.current_block)
        # Снимаем ⋯ с блока (генерация прервалась, но новых файлов не появилось)
        self._refresh_block_indicator(target_block)

        prefix = "Ошибка" if self.current_block == target_block else f"Ошибка [{target_block}]"
        self.status_bar.showMessage(f"{prefix} SHOT {panel_idx + 1}: {msg}")

    # ── Misc ─────────────────────────────────────────────────────────────────

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

    def _change_project(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Выбери папку проекта storyboard-automation",
            str(self._project_root))
        if folder and is_valid_project(Path(folder)):
            self._project_root = Path(folder)
            store_root(self._project_root)
            # Сбрасываем активный сериал на тот что записан в новом проекте
            shows = list_shows(self._project_root)
            self._current_show = get_current_show(self._project_root)
            if self._current_show not in shows:
                self._current_show = shows[0] if shows else None
                if self._current_show:
                    set_current_show(self._project_root, self._current_show)
            setup_paths_for_show(self._project_root, self._current_show)
            self._meta = read_episodes_meta(SHOW_ROOT) if self._current_show else {}
            self._refresh_settings_versions()
            self._watcher.removePaths(self._watcher.directories())
            self._watcher.addPath(str(STORYBOARDS_DIR))
            self._populate_shows()
            self._populate_episodes()
        elif folder:
            QMessageBox.warning(self, "Ошибка",
                "Это не папка проекта storyboard-automation.\n"
                "Нужна папка с файлом pipeline.py или папкой shows/")

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
        """Показывает один или оба баннера в зависимости от того что устарело."""
        if latest_proj != curr_proj:
            self.update_text.setText(
                f"🔄  Обновление проекта:  v{curr_proj} → v{latest_proj}"
            )
            self.update_banner.show()
        if latest_app != curr_app:
            self._latest_app_ver = latest_app
            self.app_update_text.setText(
                f"⬇  Новое приложение:  v{curr_app} → v{latest_app}"
            )
            self.app_update_banner.show()

    def _download_update(self):
        if self._update_thread and self._update_thread.isRunning():
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Скачивается…")

        self._update_thread = DownloadUpdateThread(self._project_root)
        self._update_thread.progress.connect(
            lambda msg, pct: self.status_bar.showMessage(f"{msg} ({pct}%)"))
        self._update_thread.finished.connect(self._on_update_done)
        self._update_thread.error.connect(self._on_update_error)
        self._update_thread.start()

    def _on_update_done(self, new_version: str):
        self.update_banner.hide()
        self._refresh_settings_versions()
        QMessageBox.information(
            self, "Обновление установлено",
            f"Проект обновлён до версии v{new_version}.\n\n"
            "Все твои сториборды и референсы сохранены.\n\n"
            "Если изменения затронули само приложение — закрой и открой его заново."
        )
        self.status_bar.showMessage(f"Обновлено до v{new_version} ✓")

    def _on_update_error(self, msg: str):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Обновить →")
        QMessageBox.warning(self, "Ошибка обновления", msg)

    def _download_app_update(self):
        """Скачивает и устанавливает новый .app бинарник из GitHub Releases."""
        if self._app_update_thread and self._app_update_thread.isRunning():
            return
        if not self._latest_app_ver:
            return

        confirm = QMessageBox.question(
            self, "Скачать новое приложение?",
            f"Будет скачана версия v{self._latest_app_ver} (~50–150 МБ).\n\n"
            "После установки нужно перезапустить Storyboard Studio.\n"
            "Все сториборды и настройки будут сохранены.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.app_update_btn.setEnabled(False)
        self.app_update_btn.setText("Скачивается…")

        self._app_update_thread = DownloadAppUpdateThread(self._latest_app_ver)
        self._app_update_thread.progress.connect(
            lambda msg, pct: self.status_bar.showMessage(f"{msg} ({pct}%)"))
        self._app_update_thread.finished.connect(self._on_app_update_done)
        self._app_update_thread.error.connect(self._on_app_update_error)
        self._app_update_thread.start()

    def _on_app_update_done(self, new_version: str, install_path: str):
        self.app_update_banner.hide()
        self._refresh_settings_versions()

        # Фиксируем что это «своё» скачивание — не показывать в счётчике коллег
        try:
            settings = QSettings(APP_ORG, APP_NAME)
            key = f"dl_baseline_{new_version}"
            cur = int(settings.value(key, 0) or 0)
            settings.setValue(key, cur + 1)
        except Exception:
            pass

        installed_in_app = install_path.endswith(".app") or ".app/" in install_path
        if installed_in_app:
            msg = (
                f"Storyboard Studio v{new_version} установлен.\n\n"
                "Закрой приложение и открой его снова — "
                "новая версия запустится автоматически."
            )
        else:
            msg = (
                f"Storyboard Studio v{new_version} сохранён в:\n{install_path}\n\n"
                "Нет прав на замену текущего приложения.\n"
                "Перемести его вручную в папку Applications."
            )
        QMessageBox.information(self, "Приложение обновлено", msg)
        self.status_bar.showMessage(f"Приложение v{new_version} установлено ✓")

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

        if has_app:
            box = QMessageBox(self)
            box.setWindowTitle("Отправить обновление?")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("Что отправить коллегам?")
            box.setInformativeText(
                "В папке dist/ найдено приложение Storyboard Studio.app.\n\n"
                "• «Только проект» — отправит правила/скрипты на GitHub.\n"
                "• «Проект + приложение» — отправит правила И загрузит\n"
                "   новый .app в GitHub Releases (коллеги смогут скачать).\n\n"
                "Если ты не пересобирал приложение — выбирай «Только проект»."
            )
            btn_only = box.addButton("Только проект",       QMessageBox.ButtonRole.AcceptRole)
            btn_full = box.addButton("Проект + приложение", QMessageBox.ButtonRole.AcceptRole)
            btn_canc = box.addButton("Отмена",              QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(btn_full)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_canc:
                return
            upload_app = (clicked is btn_full)
        else:
            confirm = QMessageBox.question(
                self, "Отправить обновление?",
                "Это создаст новый коммит с автоматически увеличенной версией\n"
                "и отправит изменения проекта на GitHub.\n\n"
                "Коллеги увидят обновление при следующем запуске.\n\n"
                "(Чтобы загрузить и само приложение — сначала пересобери его\n"
                "командой: python3 -m PyInstaller StoryboardStudio.spec --noconfirm)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            upload_app = False

        self.send_update_btn.setEnabled(False)
        self.send_update_btn.setText("Отправляю…")
        self._update_thread = SendUpdateThread(self._project_root, upload_app=upload_app)
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

        msg = f"Проект: v{new_version} опубликован на GitHub.\n"
        if app_uploaded:
            msg += f"Приложение: v{new_app_version} загружено в GitHub Releases.\n"
        msg += "\nКоллеги получат уведомление в приложении."
        QMessageBox.information(self, "Обновление отправлено", msg)

        self.status_bar.showMessage(
            f"Опубликовано: проект v{new_version}"
            + (f" + приложение v{new_app_version}" if app_uploaded else "")
            + " ✓"
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

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Storyboard Studio")
    app.setOrganizationName(APP_ORG)
    app.setStyleSheet(DARK)

    root = get_stored_root()
    if root is None:
        root = ask_project_root(app)
        if root is None:
            sys.exit(0)
        store_root(root)

    win = MainWindow(root)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
