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

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QListWidget, QListWidgetItem,
    QStatusBar, QFileDialog, QMessageBox, QProgressBar, QDialog,
    QDialogButtonBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QFileSystemWatcher, QTimer, QSize, QSettings
from PyQt6.QtGui import QPixmap, QImage

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
                      "_inbox", ".claude", ".git", "build", "dist", "__pycache__",
                      ".DS_Store"}

_upload_cache: Dict[str, str] = {}

# Глобальные пути — инициализируются через setup_paths()
ENV_FILE        = Path()
PROMPTS_DIR     = Path()
STORYBOARDS_DIR = Path()
LOCATIONS_DIR   = Path()
CHARACTERS_DIR  = Path()
OBJECTS_DIR     = Path()


def setup_paths(root: Path) -> None:
    global ENV_FILE, PROMPTS_DIR, STORYBOARDS_DIR, LOCATIONS_DIR, CHARACTERS_DIR, OBJECTS_DIR
    ENV_FILE        = root / ".env"
    PROMPTS_DIR     = root / "output" / "prompts"
    STORYBOARDS_DIR = root / "output" / "storyboards"
    refs            = root / "refs"
    LOCATIONS_DIR   = refs / "locations"
    CHARACTERS_DIR  = refs / "characters"
    OBJECTS_DIR     = refs / "objects"
    STORYBOARDS_DIR.mkdir(parents=True, exist_ok=True)


def get_stored_root() -> Optional[Path]:
    s = QSettings(APP_ORG, APP_NAME)
    p = s.value("project_root", "")
    return Path(p) if p and Path(p).is_dir() else None


def store_root(root: Path) -> None:
    QSettings(APP_ORG, APP_NAME).setValue("project_root", str(root))


def is_valid_project(path: Path) -> bool:
    return (path / "pipeline.py").exists() or (path / "output").is_dir()


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
    """Админ — это владелец репозитория, у которого настроен git push origin."""
    if not (root / ".git").is_dir():
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip().startswith(("http", "git@"))
    except Exception:
        return False


def github_configured() -> bool:
    return GITHUB_USER and GITHUB_USER != "PLACEHOLDER_USER"


# ─── Тема ─────────────────────────────────────────────────────────────────────
DARK = """
QMainWindow, QWidget        { background: #161616; color: #e0e0e0; font-family: -apple-system, "Segoe UI", Helvetica Neue; }
QScrollArea                 { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QDialog                     { background: #1e1e1e; }

QListWidget {
    background: #1e1e1e; border: 1px solid #2c2c2c; border-radius: 8px;
    padding: 4px; outline: none;
}
QListWidget::item           { padding: 9px 12px; border-radius: 6px; color: #b0b0b0; font-size: 13px; }
QListWidget::item:selected  { background: #2d2d2d; color: #fff; }
QListWidget::item:hover     { background: #252525; }

QPushButton {
    background: #2a2a2a; border: 1px solid #383838; border-radius: 6px;
    padding: 7px 14px; color: #e0e0e0; font-size: 13px;
}
QPushButton:hover           { background: #333; border-color: #444; }
QPushButton:pressed         { background: #222; }
QPushButton:disabled        { background: #1e1e1e; color: #444; border-color: #2a2a2a; }

QPushButton#regen {
    background: #1a2a1a; border: 1px solid #2a3f2a; color: #6db86d; font-size: 12px;
}
QPushButton#regen:hover     { background: #1e311e; border-color: #3a5a3a; }
QPushButton#regen:disabled  { background: #181818; color: #333; border-color: #222; }

QPushButton#save {
    background: #1a1e2a; border: 1px solid #2a2e3f; color: #7a9ccc; font-size: 13px; padding: 9px;
}
QPushButton#save:hover      { background: #1f2430; }
QPushButton#secondary       { background: #222; font-size: 12px; color: #777; }
QPushButton#secondary:hover { color: #bbb; }

QFrame#card                 { background: #1e1e1e; border: 1px solid #2c2c2c; border-radius: 10px; }
QFrame#card:hover           { border-color: #3a3a3a; }

QLabel#sidebar-title        { font-size: 11px; font-weight: bold; color: #555; letter-spacing: 2px; }
QLabel#block-title          { font-size: 15px; font-weight: bold; color: #ccc; }
QLabel#shot-num             { font-size: 13px; font-weight: bold; color: #fff; }
QLabel#shot-dur             { font-size: 12px; color: #666; }
QLabel#shot-desc            { font-size: 12px; color: #888; }
QLabel#step-label           { font-size: 11px; color: #5a8a5a; }
QLabel#project-path         { font-size: 11px; color: #444; }
QLabel#stats-label          { font-size: 10px; color: #3d3d5c; }

QStatusBar                  { background: #111; color: #555; font-size: 12px; border-top: 1px solid #222; }

QProgressBar {
    background: #1a1a1a; border: none; border-radius: 3px; height: 5px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a7a3a, stop:1 #5ab85a);
    border-radius: 3px;
}

QFrame#update-banner {
    background: #1f2630; border: 1px solid #2d4060; border-radius: 8px;
}
QLabel#update-text          { font-size: 13px; color: #88aadd; }
QPushButton#update-btn {
    background: #2a3d5a; border: 1px solid #3d5680; border-radius: 6px;
    padding: 6px 14px; color: #b0d0ff; font-size: 12px;
}
QPushButton#update-btn:hover { background: #344870; }

QFrame#app-update-banner {
    background: #201a30; border: 1px solid #503070; border-radius: 8px;
}
QLabel#app-update-text      { font-size: 13px; color: #cc99ff; }
QPushButton#app-update-btn {
    background: #3a2560; border: 1px solid #5a3880; border-radius: 6px;
    padding: 6px 14px; color: #e0bbff; font-size: 12px;
}
QPushButton#app-update-btn:hover { background: #462e72; }

QPushButton#admin-send {
    background: #2a1f3a; border: 1px solid #4a3060; color: #c090ff; font-size: 12px;
}
QPushButton#admin-send:hover { background: #322647; }
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
    finished = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(self, block_name: str, panel_idx: int):
        super().__init__()
        self.block_name = block_name
        self.panel_idx  = panel_idx

    def run(self):
        try:
            key     = load_api_key()
            session = requests.Session()
            session.headers["X-API-Key"] = key

            prompt_file = PROMPTS_DIR / f"{self.block_name}.txt"
            if not prompt_file.exists():
                self.error.emit(f"Промпт не найден: {prompt_file.name}")
                return

            prompt_text = prompt_file.read_text(encoding="utf-8")
            refs        = parse_refs(prompt_text)
            clean       = extract_shot_prompt(prompt_text, self.panel_idx)
            if not clean:
                self.error.emit(
                    f"SHOT {self.panel_idx + 1}: панель пустая или Panel "
                    f"{self.panel_idx + 1} не найден в промпте {prompt_file.name}")
                return

            # Загрузка рефов
            ref_hashes: List[str] = []
            if refs:
                n = len(refs)
                sorted_tags = sorted(refs, key=lambda t: int(re.search(r'\d+', t).group()))
                for idx, tag in enumerate(sorted_tags):
                    path      = refs[tag]
                    cache_key = str(path.resolve())
                    if cache_key in _upload_cache:
                        ref_hashes.append(_upload_cache[cache_key])
                    else:
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
                        ref_hashes.append(fh)
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

            self.step.emit("Готово!", 100)
            self.finished.emit()

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

            if latest_proj != curr_proj or latest_app != curr_app:
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
    CARD_W, CARD_H  = 200, 356

    def __init__(self, panel_idx: int, parent=None):
        super().__init__(parent)
        self.panel_idx = panel_idx
        self.setObjectName("card")
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.setContentsMargins(10, 10, 10, 10)

        self.img_label = QLabel("нет изображения")
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setFixedSize(self.CARD_W, self.CARD_H)
        self.img_label.setStyleSheet(
            "background:#252525; border-radius:6px; color:#333; font-size:12px;")
        lay.addWidget(self.img_label, alignment=Qt.AlignmentFlag.AlignHCenter)

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
        self.dur_label = QLabel("")
        self.dur_label.setObjectName("shot-dur")
        row.addWidget(self.num_label)
        row.addStretch()
        row.addWidget(self.dur_label)
        lay.addLayout(row)

        self.desc_label = QLabel("")
        self.desc_label.setObjectName("shot-desc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumWidth(self.CARD_W + 20)
        lay.addWidget(self.desc_label)

        lay.addStretch()

        self.regen_btn = QPushButton("↺  Регенерировать")
        self.regen_btn.setObjectName("regen")
        self.regen_btn.setFixedWidth(self.CARD_W + 20)
        self.regen_btn.clicked.connect(lambda: self.regen_requested.emit(self.panel_idx))
        lay.addWidget(self.regen_btn, alignment=Qt.AlignmentFlag.AlignHCenter)

    def set_image(self, jpeg_bytes: Optional[bytes]):
        if not jpeg_bytes:
            self.img_label.clear()
            self.img_label.setText("ПУСТО")
            return
        pixmap = QPixmap.fromImage(QImage.fromData(jpeg_bytes))
        self.img_label.setPixmap(pixmap.scaled(
            QSize(self.CARD_W, self.CARD_H),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def set_shot_info(self, shot: Dict):
        if shot["is_blank"]:
            self.num_label.setText("ПУСТО")
            self.dur_label.setText("")
            self.desc_label.setText("")
            self.regen_btn.setEnabled(False)
        else:
            self.num_label.setText(f"SHOT {shot['shot_num']}")
            self.dur_label.setText(shot["duration"])
            self.desc_label.setText(shot["description"])
            self.regen_btn.setEnabled(True)

    def set_progress(self, label: str, pct: int):
        self.progress_bar.setValue(pct)
        self.step_label.setText(label)
        self.progress_bar.show()
        self.step_label.show()

    def set_loading(self, loading: bool):
        self.regen_btn.setEnabled(not loading)
        if not loading:
            self.progress_bar.hide()
            self.step_label.hide()
            self.progress_bar.setValue(0)


# ─── Главное окно ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, project_root: Path):
        super().__init__()
        setup_paths(project_root)
        self._project_root = project_root
        self._is_admin     = is_admin_mode(project_root)
        self.setWindowTitle("Storyboard Studio")
        self.setMinimumSize(1080, 720)
        self.current_block: Optional[str] = None
        # Параллельные регенерации: ключ (block_name, panel_idx) → поток.
        # Каждый шот в каждом блоке может генериться независимо от других.
        self._active_regens: Dict[tuple, GenerateThread] = {}
        self._update_thread:     Optional[QThread]                 = None
        self._app_update_thread: Optional[DownloadAppUpdateThread] = None
        self._stats_thread:      Optional[FetchStatsThread]        = None
        # Кешированная latest версия .app — нужна для скачивания при клике на баннер
        self._latest_app_ver: Optional[str] = None
        self._build_ui()
        self._load_blocks()

        self._watcher = QFileSystemWatcher([str(STORYBOARDS_DIR)])
        self._watcher.directoryChanged.connect(
            lambda: QTimer.singleShot(600, self._load_blocks))

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

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)
        lay.setSpacing(16)
        lay.setContentsMargins(16, 16, 16, 16)

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setFixedWidth(210)
        sl = QVBoxLayout(sidebar)
        sl.setSpacing(8)
        sl.setContentsMargins(0, 0, 0, 0)

        title = QLabel("БЛОКИ")
        title.setObjectName("sidebar-title")
        sl.addWidget(title)

        self.block_list = QListWidget()
        self.block_list.currentItemChanged.connect(self._on_block_selected)
        sl.addWidget(self.block_list)

        refresh_btn = QPushButton("⟳  Обновить список")
        refresh_btn.clicked.connect(self._load_blocks)
        sl.addWidget(refresh_btn)

        folder_btn = QPushButton("📂  Открыть в Finder")
        folder_btn.setObjectName("secondary")
        folder_btn.clicked.connect(self._open_folder)
        sl.addWidget(folder_btn)

        change_btn = QPushButton("⚙  Сменить папку проекта")
        change_btn.setObjectName("secondary")
        change_btn.clicked.connect(self._change_project)
        sl.addWidget(change_btn)

        # Кнопка для админа — отправить обновление коллегам
        if self._is_admin:
            self.send_update_btn = QPushButton("📤  Отправить обновление")
            self.send_update_btn.setObjectName("admin-send")
            self.send_update_btn.clicked.connect(self._send_update)
            sl.addWidget(self.send_update_btn)

            # Метка со статистикой скачиваний приложения
            self.stats_label = QLabel("загружаю статистику…")
            self.stats_label.setObjectName("stats-label")
            self.stats_label.setWordWrap(True)
            sl.addWidget(self.stats_label)

        self.path_label = QLabel(self._format_version_label())
        self.path_label.setObjectName("project-path")
        self.path_label.setWordWrap(True)
        sl.addWidget(self.path_label)

        lay.addWidget(sidebar)

        # ── Right panel ───────────────────────────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setSpacing(12)
        rl.setContentsMargins(0, 0, 0, 0)

        # Баннер обновления проекта (файлы/скрипты)
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
        rl.addWidget(self.update_banner)

        # Баннер обновления приложения (.app бинарник из GitHub Releases)
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
        rl.addWidget(self.app_update_banner)

        self.block_title = QLabel("← Выбери блок")
        self.block_title.setObjectName("block-title")
        rl.addWidget(self.block_title)

        cards_w = QWidget()
        self.cards_row = QHBoxLayout(cards_w)
        self.cards_row.setSpacing(12)
        self.cards_row.setContentsMargins(0, 0, 0, 0)

        self.shot_cards: List[ShotCard] = []
        for i in range(PANELS):
            card = ShotCard(i)
            card.regen_requested.connect(self._on_regen)
            self.shot_cards.append(card)
            self.cards_row.addWidget(card)
        self.cards_row.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_w)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rl.addWidget(scroll, stretch=1)

        self.save_btn = QPushButton("💾  Сохранить стриборд как PNG")
        self.save_btn.setObjectName("save")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save_png)
        rl.addWidget(self.save_btn)

        lay.addWidget(right, stretch=1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Готов")

    # ── Blocks ────────────────────────────────────────────────────────────────

    def _load_blocks(self):
        prev = self.current_block
        self.block_list.blockSignals(True)
        self.block_list.clear()

        # Новый формат: {block}_shot{N}.jpg — собираем уникальные имена блоков
        seen: set = set()
        for f in STORYBOARDS_DIR.glob("*_block_*_shot*.jpg"):
            m = re.match(r'(.*?_block_\d+)_shot\d+', f.stem)
            if m:
                seen.add(m.group(1))

        # Также показываем блоки у которых есть промпт-файл, даже если шотов
        # ещё нет — чтобы можно было запустить регенерацию.
        for p in PROMPTS_DIR.glob("*_block_*.txt"):
            seen.add(p.stem)

        for block_name in sorted(seen):
            item = QListWidgetItem(self._format_block_label(block_name))
            item.setData(Qt.ItemDataRole.UserRole, block_name)
            self.block_list.addItem(item)
        self.block_list.blockSignals(False)

        if prev:
            for i in range(self.block_list.count()):
                if self.block_list.item(i).data(Qt.ItemDataRole.UserRole) == prev:
                    self.block_list.setCurrentRow(i)
                    return
        if self.block_list.count() > 0:
            self.block_list.setCurrentRow(0)

    def _block_indicator_for(self, block_name: str) -> str:
        """Возвращает префикс-индикатор для блока в списке.

        ⋯ — идёт регенерация хотя бы одного шота этого блока
        ✓ — все нужные шоты сгенерированы (по промпту)
        ""  — иначе (нет шотов / частично готов)
        """
        if any(b == block_name for (b, _) in self._active_regens.keys()):
            return "⋯  "
        if is_block_complete(block_name):
            return "✓  "
        return ""

    def _format_block_label(self, block_name: str) -> str:
        """Текст пункта в списке блоков с индикатором завершённости."""
        m = re.match(r'(.*?)_block_(\d+)', block_name)
        base = f"Блок {m.group(2)}  [{m.group(1)}]" if m else block_name
        return self._block_indicator_for(block_name) + base

    def _refresh_block_indicator(self, block_name: str):
        """Обновляет иконку у одного блока в списке без перерисовки всего списка."""
        for i in range(self.block_list.count()):
            item = self.block_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == block_name:
                item.setText(self._format_block_label(block_name))
                return

    def _on_block_selected(self, item: Optional[QListWidgetItem]):
        if not item:
            return
        self.current_block = item.data(Qt.ItemDataRole.UserRole)
        self._display_block(self.current_block)

    def _display_block(self, name: str):
        prompt_file = PROMPTS_DIR / f"{name}.txt"

        m = re.match(r'(.*?)_block_(\d+)', name)
        self.block_title.setText(
            f"БЛОК {m.group(2)}  ·  {m.group(1).upper()}" if m else name)

        shots: List[Dict] = []
        if prompt_file.exists():
            shots = parse_shots(prompt_file.read_text(encoding="utf-8"))

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

        # Кнопка экспорта активна если хотя бы один шот сгенерирован
        self.save_btn.setEnabled(any_exists)

    # ── Regeneration ─────────────────────────────────────────────────────────

    def _on_regen(self, panel_idx: int):
        if not self.current_block:
            return

        target_block = self.current_block
        key = (target_block, panel_idx)

        # Защита от двойного клика на один и тот же шот
        if key in self._active_regens:
            self.status_bar.showMessage(
                f"SHOT {panel_idx + 1} уже генерируется — подожди…")
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
            lambda: self._on_regen_done(panel_idx, target_block))
        thread.error.connect(
            lambda msg: self._on_regen_error(msg, target_block, panel_idx))
        thread.start()
        # Показать ⋯ возле блока в списке (идёт регенерация)
        self._refresh_block_indicator(target_block)
        self.status_bar.showMessage(f"Регенерирую SHOT {panel_idx + 1} в {target_block}…")

    def _on_regen_step(self, lbl: str, pct: int, target_block: str, panel_idx: int):
        # Прогресс показываем ТОЛЬКО если пользователь сейчас смотрит на тот
        # блок где идёт регенерация. Иначе обновлять чужие карточки нельзя.
        if self.current_block == target_block and 0 <= panel_idx < len(self.shot_cards):
            self.shot_cards[panel_idx].set_progress(lbl, pct)

    def _on_regen_done(self, panel_idx: int, target_block: str):
        self._active_regens.pop((target_block, panel_idx), None)

        # Обновляем текущий блок (если виден этот же — увидим новую картинку,
        # если другой — карточки в нём перерисуются с актуальным состоянием
        # оставшихся регенераций).
        if self.current_block:
            self._display_block(self.current_block)
        # Обновляем индикатор у ЦЕЛЕВОГО блока в списке (✓ если все шоты готовы)
        self._refresh_block_indicator(target_block)

        if self.current_block == target_block:
            self.status_bar.showMessage(f"SHOT {panel_idx + 1} обновлён ✓")
        else:
            self.status_bar.showMessage(
                f"SHOT {panel_idx + 1} в [{target_block}] обновлён ✓")

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
        if sys.platform == "win32":
            subprocess.run(["explorer", str(STORYBOARDS_DIR)])
        else:
            subprocess.run(["open", str(STORYBOARDS_DIR)])

    def _change_project(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Выбери папку проекта storyboard-automation",
            str(self._project_root))
        if folder and is_valid_project(Path(folder)):
            self._project_root = Path(folder)
            store_root(self._project_root)
            setup_paths(self._project_root)
            self.path_label.setText(self._format_version_label())
            self._watcher.removePaths(self._watcher.directories())
            self._watcher.addPath(str(STORYBOARDS_DIR))
            self._load_blocks()
        elif folder:
            QMessageBox.warning(self, "Ошибка",
                "Это не папка проекта storyboard-automation.\n"
                "Нужна папка с файлом pipeline.py")

    def _save_png(self):
        if not self.current_block:
            return
        # Проверяем что хотя бы один шот существует
        any_exists = any(shot_path(self.current_block, i).exists() for i in range(PANELS))
        if not any_exists:
            self.status_bar.showMessage("Нет шотов для экспорта")
            return

        dest, _ = QFileDialog.getSaveFileName(
            self, "Сохранить стриборд",
            str(Path.home() / "Desktop" / f"{self.current_block}.png"),
            "PNG (*.png);;JPEG (*.jpg)")
        if not dest:
            return
        try:
            stitch_shots_to_landscape(self.current_block, Path(dest))
            self.status_bar.showMessage(f"Сохранено: {dest}")
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
        self.path_label.setText(self._format_version_label())
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
        self.path_label.setText(self._format_version_label())

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

    def _format_version_label(self) -> str:
        """Текст для нижней метки сайдбара — версии проекта и приложения."""
        v_proj = read_local_version(self._project_root)
        v_app  = read_local_app_version(self._project_root)
        return (
            f"Проект: v{v_proj}\n"
            f"Приложение: v{v_app}\n"
            f"{self._project_root.name}"
        )

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
            self.stats_label.setText("нет данных о скачиваниях")
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
            lines.append(f"v{version}: {shown} скач.")
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
        self.send_update_btn.setText("📤  Отправить обновление")

        # Запоминаем mtime текущего .app — пригодится чтобы понять
        # «приложение было пересобрано после последней отправки».
        app_path = self._project_root / "dist" / "Storyboard Studio.app"
        if app_path.exists():
            try:
                QSettings(APP_ORG, APP_NAME).setValue(
                    "last_sent_app_mtime", app_path.stat().st_mtime)
            except Exception:
                pass

        self.path_label.setText(self._format_version_label())

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
        self.send_update_btn.setText("📤  Отправить обновление")
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
