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


def build_single_shot_prompt(prompt_text: str, panel_idx: int) -> str:
    BLANK = ("COMPLETELY BLANK AND EMPTY. Pure white space only. "
             "No drawing, no lines. No text annotation below.")
    target   = panel_idx + 1
    panel_re = re.compile(
        r'(Panel\s+\d+\s+\([^)]+\):)(.*?)(?=Panel\s+\d+\s+\(|===ПРОМПТ_БЛОК.*?КОНЕЦ|$)',
        re.DOTALL,
    )

    def replacer(m):
        header = m.group(1)
        pn     = re.search(r'Panel\s+(\d+)', header)
        if pn and int(pn.group(1)) == target:
            return m.group(0)
        return f"{header}\n{BLANK}\n\n"

    return panel_re.sub(replacer, prompt_text)


# ─── Утилиты — изображения ───────────────────────────────────────────────────

def split_storyboard(image_path: Path) -> List[bytes]:
    img  = PILImage.open(image_path)
    w, h = img.size
    pw   = w // PANELS
    out  = []
    for i in range(PANELS):
        left  = i * pw
        right = (i + 1) * pw if i < PANELS - 1 else w
        buf   = io.BytesIO()
        img.crop((left, 0, right, h)).save(buf, format="JPEG", quality=92)
        out.append(buf.getvalue())
    return out


def composite_panel(storyboard_path: Path, new_bytes: bytes, panel_idx: int) -> None:
    orig       = PILImage.open(storyboard_path)
    w, h       = orig.size
    pw         = w // PANELS
    new_img    = PILImage.open(io.BytesIO(new_bytes))
    nw, _      = new_img.size
    if nw > pw * 1.5:
        spw = nw // PANELS
        new_img = new_img.crop((panel_idx * spw, 0, (panel_idx + 1) * spw, new_img.height))
    new_img = new_img.resize((pw, h), PILImage.LANCZOS)
    orig.paste(new_img, (panel_idx * pw, 0))
    orig.save(storyboard_path, format="JPEG", quality=92)


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
            mod_prompt  = build_single_shot_prompt(prompt_text, self.panel_idx)
            clean = "\n".join(
                l for l in mod_prompt.splitlines()
                if not l.startswith("===ПРОМПТ_БЛОК") and not l.startswith("# [@]")
            ).strip()

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
                "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
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

            self.step.emit("Вставляю панель…", 92)
            sb_path = STORYBOARDS_DIR / f"{self.block_name}.jpg"
            if sb_path.exists():
                composite_panel(sb_path, image_bytes, self.panel_idx)
            else:
                sb_path.write_bytes(image_bytes)

            self.step.emit("Готово!", 100)
            self.finished.emit()

        except Exception as e:
            self.error.emit(str(e))


# ─── Обновления — потоки ─────────────────────────────────────────────────────

class CheckUpdateThread(QThread):
    """Проверяет наличие новой версии на GitHub."""
    update_found = pyqtSignal(str, str)   # (current, latest)
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
            current = read_local_version(self.root)
            r = requests.get(github_raw_url("version.json"), timeout=10)
            r.raise_for_status()
            latest = r.json().get("version", "0.0.0")
            if latest != current:
                self.update_found.emit(current, latest)
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


class SendUpdateThread(QThread):
    """Админ-режим: бампит версию + git commit + git push."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # new version
    error    = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            vfile = self.root / "version.json"
            data = json.loads(vfile.read_text(encoding="utf-8")) if vfile.exists() \
                   else {"version": "1.0.0"}
            major, minor, patch = data.get("version", "1.0.0").split(".")
            new_version = f"{major}.{minor}.{int(patch) + 1}"
            data["version"]  = new_version
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

            self.finished.emit(new_version)
        except subprocess.CalledProcessError as e:
            self.error.emit(f"Ошибка git: {e.stderr.decode() if e.stderr else str(e)}")
        except Exception as e:
            self.error.emit(str(e))


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
        self._thread:        Optional[GenerateThread]      = None
        self._update_thread: Optional[QThread]             = None
        self._build_ui()
        self._load_blocks()

        self._watcher = QFileSystemWatcher([str(STORYBOARDS_DIR)])
        self._watcher.directoryChanged.connect(
            lambda: QTimer.singleShot(600, self._load_blocks))

        # Авто-проверка обновлений через 2 секунды после запуска
        if github_configured():
            QTimer.singleShot(2000, self._check_updates)

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

        self.path_label = QLabel(
            f"v{read_local_version(self._project_root)}\n{self._project_root.name}"
        )
        self.path_label.setObjectName("project-path")
        self.path_label.setWordWrap(True)
        sl.addWidget(self.path_label)

        lay.addWidget(sidebar)

        # ── Right panel ───────────────────────────────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setSpacing(12)
        rl.setContentsMargins(0, 0, 0, 0)

        # Баннер обновлений (скрыт по умолчанию)
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
        for b in sorted(STORYBOARDS_DIR.glob("*block_*.jpg")):
            m       = re.match(r'(.*?)_block_(\d+)', b.stem)
            display = f"Блок {m.group(2)}  [{m.group(1)}]" if m else b.stem
            item    = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, b.stem)
            self.block_list.addItem(item)
        self.block_list.blockSignals(False)

        if prev:
            for i in range(self.block_list.count()):
                if self.block_list.item(i).data(Qt.ItemDataRole.UserRole) == prev:
                    self.block_list.setCurrentRow(i)
                    return
        if self.block_list.count() > 0:
            self.block_list.setCurrentRow(0)

    def _on_block_selected(self, item: Optional[QListWidgetItem]):
        if not item:
            return
        self.current_block = item.data(Qt.ItemDataRole.UserRole)
        self._display_block(self.current_block)

    def _display_block(self, name: str):
        sb_path     = STORYBOARDS_DIR / f"{name}.jpg"
        prompt_file = PROMPTS_DIR / f"{name}.txt"

        m = re.match(r'(.*?)_block_(\d+)', name)
        self.block_title.setText(
            f"БЛОК {m.group(2)}  ·  {m.group(1).upper()}" if m else name)

        shots: List[Dict] = []
        if prompt_file.exists():
            shots = parse_shots(prompt_file.read_text(encoding="utf-8"))

        panels: List[Optional[bytes]] = [None] * PANELS
        if sb_path.exists():
            try:
                panels = split_storyboard(sb_path)
            except Exception as e:
                self.status_bar.showMessage(f"Ошибка загрузки: {e}")

        for i, card in enumerate(self.shot_cards):
            shot = shots[i] if i < len(shots) else dict(
                shot_num=i+1, duration="", description="", is_blank=True)
            card.set_shot_info(shot)
            card.set_image(panels[i] if i < len(panels) else None)

        self.save_btn.setEnabled(sb_path.exists())

    # ── Regeneration ─────────────────────────────────────────────────────────

    def _on_regen(self, panel_idx: int):
        if not self.current_block:
            return
        if self._thread and self._thread.isRunning():
            self.status_bar.showMessage("Генерация уже идёт — подожди…")
            return

        for c in self.shot_cards:
            c.regen_btn.setEnabled(False)
        card = self.shot_cards[panel_idx]
        card.set_loading(True)

        self._thread = GenerateThread(self.current_block, panel_idx)
        self._thread.progress.connect(self.status_bar.showMessage)
        self._thread.step.connect(lambda lbl, pct: card.set_progress(lbl, pct))
        self._thread.finished.connect(lambda: self._on_regen_done(panel_idx))
        self._thread.error.connect(self._on_regen_error)
        self._thread.start()
        self.status_bar.showMessage(f"Регенерирую SHOT {panel_idx + 1}…")

    def _on_regen_done(self, panel_idx: int):
        if self.current_block:
            self._display_block(self.current_block)
        self.status_bar.showMessage(f"SHOT {panel_idx + 1} обновлён ✓")

    def _on_regen_error(self, msg: str):
        self.status_bar.showMessage(f"Ошибка: {msg}")
        if self.current_block:
            self._display_block(self.current_block)

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
            self.path_label.setText(str(self._project_root))
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
        src = STORYBOARDS_DIR / f"{self.current_block}.jpg"
        if not src.exists():
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Сохранить стриборд",
            str(Path.home() / "Desktop" / f"{self.current_block}.png"),
            "PNG (*.png);;JPEG (*.jpg)")
        if dest:
            PILImage.open(src).save(dest)
            self.status_bar.showMessage(f"Сохранено: {dest}")

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

    def _show_update_banner(self, current: str, latest: str):
        self.update_text.setText(
            f"🔄  Доступно обновление:  v{current} → v{latest}"
        )
        self.update_banner.show()

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
        self.path_label.setText(f"v{new_version}\n{self._project_root.name}")
        QMessageBox.information(
            self, "Обновление установлено",
            f"Обновлено до версии v{new_version}.\n\n"
            "Если изменения затронули само приложение — закрой и открой его заново."
        )
        self.status_bar.showMessage(f"Обновлено до v{new_version} ✓")

    def _on_update_error(self, msg: str):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Обновить →")
        QMessageBox.warning(self, "Ошибка обновления", msg)

    def _send_update(self):
        if not self._is_admin:
            return
        if self._update_thread and self._update_thread.isRunning():
            return

        confirm = QMessageBox.question(
            self, "Отправить обновление?",
            "Это создаст новый коммит с автоматически увеличенной версией\n"
            "и отправит изменения на GitHub.\n\nКоллеги увидят обновление при следующем запуске.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.send_update_btn.setEnabled(False)
        self.send_update_btn.setText("Отправляю…")
        self._update_thread = SendUpdateThread(self._project_root)
        self._update_thread.progress.connect(self.status_bar.showMessage)
        self._update_thread.finished.connect(self._on_send_done)
        self._update_thread.error.connect(self._on_send_error)
        self._update_thread.start()

    def _on_send_done(self, new_version: str):
        self.send_update_btn.setEnabled(True)
        self.send_update_btn.setText("📤  Отправить обновление")
        self.path_label.setText(f"v{new_version}\n{self._project_root.name}")
        QMessageBox.information(
            self, "Обновление отправлено",
            f"Версия v{new_version} опубликована на GitHub.\n"
            "Коллеги получат уведомление в приложении.")
        self.status_bar.showMessage(f"Опубликовано: v{new_version} ✓")

    def _on_send_error(self, msg: str):
        self.send_update_btn.setEnabled(True)
        self.send_update_btn.setText("📤  Отправить обновление")
        QMessageBox.warning(self, "Ошибка отправки", msg)


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
