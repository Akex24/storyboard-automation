#!/usr/bin/env python3
"""
Storyboard Studio Installer — мастер установки для коллег.

Проводит пользователя через 5 шагов:
  1) Проверка Python
  2) Скачивание проекта с GitHub
  3) Ввод Fast Gen AI ключа
  4) Установка Claude Code
  5) Готово → запуск Storyboard Studio
"""

import io
import os
import sys
import json
import shutil
import zipfile
import platform
import tempfile
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional


def find_system_python() -> Optional[str]:
    """Найти системный Python — НЕ запакованный установщик.

    Внутри PyInstaller .app-бандла sys.executable указывает на сам бандл,
    что приводит к fork-bomb при попытке запустить Python через subprocess.
    """
    if not getattr(sys, 'frozen', False):
        return sys.executable
    for cmd in ("python3", "python"):
        path = shutil.which(cmd)
        if path:
            return path
    # Стандартные места установки на macOS
    for guess in (
        "/usr/local/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3",
    ):
        if Path(guess).exists():
            return guess
    return None

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QMessageBox,
    QStackedWidget, QProgressBar, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

# ─── Настройки ──────────────────────────────────────────────────────────────
GITHUB_USER   = "Akex24"
GITHUB_REPO   = "storyboard-automation"
GITHUB_BRANCH = "main"

CLAUDE_CODE_DOWNLOAD_PAGE = "https://claude.ai/download"

# Native installer Claude Code CLI (one-line download без Node.js / npm).
# Та же команда что в документации Anthropic. Для macOS запускается через
# bash, для Windows — через PowerShell. После установки бинарник лежит
# в ~/.local/bin/claude (Mac) или %USERPROFILE%\.local\bin\claude (Win).
CLAUDE_CLI_INSTALL_URL_MAC = "https://claude.ai/install.sh"
CLAUDE_CLI_INSTALL_URL_WIN = "https://claude.ai/install.ps1"

IS_MAC     = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def find_claude_cli_path() -> Optional[str]:
    """Ищет установленный Claude Code CLI на машине.

    macOS: shutil.which → ~/.local/bin/claude → /opt/homebrew/bin → /usr/local/bin
    Windows: shutil.which → %USERPROFILE%\\.local\\bin\\claude.exe → %LOCALAPPDATA%

    Возвращает абсолютный путь или None если не найден."""
    found = shutil.which("claude")
    if found and Path(found).exists():
        return found
    candidates = []
    home = Path.home()
    if IS_WINDOWS:
        candidates += [
            str(home / ".local" / "bin" / "claude.exe"),
            str(home / ".local" / "bin" / "claude"),
            str(home / "AppData" / "Local" / "Anthropic" / "claude.exe"),
        ]
    else:
        candidates += [
            str(home / ".local" / "bin" / "claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ]
    for c in candidates:
        if c and Path(c).exists() and Path(c).is_file():
            return c
    return None


class InstallClaudeCliThread(QThread):
    """Фоновый поток: запускает официальный native installer Claude Code CLI.

    macOS:   bash -c "curl -fsSL https://claude.ai/install.sh | bash"
    Windows: powershell -Command "irm https://claude.ai/install.ps1 | iex"

    После завершения проверяет наличие бинарника. Эмитит progress (для
    лога), finished (с путём к claude) и error (если что-то сломалось)."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # путь к бинарнику
    error    = pyqtSignal(str)

    def run(self):
        try:
            if IS_MAC:
                self.progress.emit("Скачиваю официальный установщик с claude.ai…")
                cmd = [
                    "/bin/bash", "-c",
                    f"curl -fsSL {CLAUDE_CLI_INSTALL_URL_MAC} | bash"
                ]
            elif IS_WINDOWS:
                self.progress.emit("Скачиваю официальный установщик с claude.ai…")
                # PowerShell -Command + Bypass policy чтобы не упереться
                # в RemoteSigned execution policy на чистой Windows.
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-Command",
                    f"irm {CLAUDE_CLI_INSTALL_URL_WIN} | iex"
                ]
            else:
                self.error.emit(f"ОС не поддерживается: {sys.platform}")
                return

            # 2026-05-08: CREATE_NO_WINDOW guard для Win — не показывать
            # cmd-окно поверх установщика во время фонового powershell.
            run_kwargs = dict(capture_output=True, text=True, timeout=300)
            if IS_WINDOWS:
                run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(cmd, **run_kwargs)
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "exit != 0").strip()[:500]
                self.error.emit(f"Установщик завершился с ошибкой:\n{msg}")
                return

            cli = find_claude_cli_path()
            if not cli:
                # Иногда PATH ещё не обновился в spawned shell — глянем явно
                home = Path.home()
                guess = (home / ".local" / "bin" /
                         ("claude.exe" if IS_WINDOWS else "claude"))
                if guess.exists():
                    cli = str(guess)
            if not cli:
                self.error.emit(
                    "Установка прошла, но бинарник `claude` не найден.\n"
                    "Закрой и открой терминал заново — возможно нужно "
                    "перечитать PATH.")
                return
            self.finished.emit(cli)
        except subprocess.TimeoutExpired:
            self.error.emit("Установка заняла больше 5 минут — прервано.")
        except Exception as e:
            self.error.emit(f"Ошибка: {e}")


class CheckClaudeAuthThread(QThread):
    """Фоновая проверка: установлен ли CLI И залогинен ли пользователь.

    Пытается выполнить очень короткий headless-запрос:
        claude -p "ok" --dangerously-skip-permissions
    с timeout 25 секунд. По результату:
      • returncode=0 + непустой ответ → CLI готов, юзер залогинен
      • returncode!=0 + stderr содержит auth-keyword → CLI установлен, не залогинен
      • CLI не найден → not_installed
      • Прочее → error

    Эмитит сигнал status_ready(state, detail) где state одно из:
      'ready'           — всё готово
      'not_logged_in'   — CLI есть, нужен claude login
      'not_installed'   — CLI не найден на машине
      'error'           — другая проблема (нет интернета, claude.ai недоступен)
    """
    status_ready = pyqtSignal(str, str)   # (state, detail)

    def run(self):
        cli = find_claude_cli_path()
        if not cli:
            self.status_ready.emit('not_installed', '')
            return
        try:
            # 2026-05-08: CREATE_NO_WINDOW guard для Win.
            run_kwargs = dict(capture_output=True, text=True, timeout=25)
            if IS_WINDOWS:
                run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(
                [cli, "-p", "ok", "--dangerously-skip-permissions"],
                **run_kwargs,
            )
            stderr_low = (proc.stderr or '').lower()
            stdout_low = (proc.stdout or '').lower()
            auth_keywords = ('not logged in', 'log in', 'login required',
                             'unauthor', 'not authent', 'authenticate',
                             'no credentials', 'no api key')
            if proc.returncode == 0 and (proc.stdout or '').strip():
                self.status_ready.emit('ready', cli)
                return
            if any(k in stderr_low or k in stdout_low for k in auth_keywords):
                self.status_ready.emit('not_logged_in', cli)
                return
            # Возможно просто пустой ответ — но returncode=0 значит работает
            if proc.returncode == 0:
                self.status_ready.emit('ready', cli)
                return
            msg = (proc.stderr or proc.stdout or 'unknown').strip()[:300]
            self.status_ready.emit('error', msg)
        except subprocess.TimeoutExpired:
            # Если ответ не пришёл за 25с — скорее всего что-то сломано
            # (нет интернета или auth завис). Считаем not_logged_in чтобы юзер
            # попробовал авторизоваться заново.
            self.status_ready.emit('not_logged_in', cli)
        except Exception as e:
            self.status_ready.emit('error', str(e)[:300])


def github_zip_url() -> str:
    return f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"


# ─── Тёмная тема ────────────────────────────────────────────────────────────
DARK = """
QMainWindow, QWidget { background: #161616; color: #e0e0e0;
    font-family: -apple-system, "Segoe UI", Helvetica Neue; }
QLabel#h1 { font-size: 22px; font-weight: bold; color: #fff; }
QLabel#h2 { font-size: 15px; font-weight: bold; color: #ccc; }
QLabel#desc { font-size: 13px; color: #999; }
QLabel#step-num { font-size: 11px; color: #666; letter-spacing: 2px; }
QLabel#status-ok { font-size: 13px; color: #6db86d; }
QLabel#status-warn { font-size: 13px; color: #ddaa55; }
QLabel#status-err { font-size: 13px; color: #cc6666; }
QLineEdit {
    background: #1e1e1e; border: 1px solid #333; border-radius: 6px;
    padding: 9px 12px; color: #e0e0e0; font-size: 13px;
}
QLineEdit:focus { border-color: #5a8a5a; }
QPushButton {
    background: #2a2a2a; border: 1px solid #383838; border-radius: 6px;
    padding: 9px 18px; color: #e0e0e0; font-size: 13px;
}
QPushButton:hover { background: #333; }
QPushButton:disabled { background: #1e1e1e; color: #444; }
QPushButton#primary {
    background: #1a3d1a; border: 1px solid #2d6030; color: #aff0af; font-weight: bold;
}
QPushButton#primary:hover { background: #1f4d1f; }
QPushButton#primary:disabled { background: #181818; color: #333; border-color: #222; }
QPushButton#secondary { background: #222; color: #888; }
QFrame#step-divider { background: #2a2a2a; max-height: 1px; }
QProgressBar {
    background: #1a1a1a; border: none; border-radius: 3px; height: 8px; text-align: center;
    color: #888; font-size: 11px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a7a3a, stop:1 #5ab85a);
    border-radius: 3px;
}
"""


# ─── Поток скачивания проекта ─────────────────────────────────────────────────

class DownloadProjectThread(QThread):
    progress = pyqtSignal(int, str)      # percent, status
    finished = pyqtSignal(Path)          # path to extracted project
    error    = pyqtSignal(str)

    def __init__(self, dest_dir: Path):
        super().__init__()
        self.dest_dir = dest_dir

    def run(self):
        try:
            self.progress.emit(5, "Скачиваю с GitHub…")
            req = urllib.request.Request(github_zip_url(),
                                          headers={"User-Agent": "StoryboardStudioInstaller"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                buf   = io.BytesIO()
                done  = 0
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    buf.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 5 + int(done / total * 70)
                        self.progress.emit(pct, f"Скачиваю… {done // 1024} КБ")

            self.progress.emit(80, "Распаковка…")
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp)
                src = next(Path(tmp).iterdir())  # e.g. storyboard-automation-main
                target = self.dest_dir / GITHUB_REPO
                if target.exists():
                    # Сохраняем существующий контент пользователя
                    for item in src.iterdir():
                        dst = target / item.name
                        if dst.exists() and dst.is_dir():
                            continue
                        if item.is_dir():
                            import shutil
                            shutil.copytree(item, dst, dirs_exist_ok=True)
                        else:
                            import shutil
                            shutil.copy2(item, dst)
                else:
                    import shutil
                    shutil.copytree(src, target)

            self.progress.emit(100, "Готово!")
            self.finished.emit(target)
        except Exception as e:
            self.error.emit(str(e))


# ─── Шаги установщика ────────────────────────────────────────────────────────

class StepBase(QWidget):
    next_requested = pyqtSignal()
    skip_requested = pyqtSignal()

    def __init__(self, num: int, total: int, title: str, parent=None):
        super().__init__(parent)
        self.num   = num
        self.total = total
        self.title = title
        self.lay   = QVBoxLayout(self)
        self.lay.setSpacing(14)
        self.lay.setContentsMargins(40, 30, 40, 30)

        step_label = QLabel(f"ШАГ {num} ИЗ {total}")
        step_label.setObjectName("step-num")
        self.lay.addWidget(step_label)

        title_label = QLabel(title)
        title_label.setObjectName("h1")
        self.lay.addWidget(title_label)

        divider = QFrame()
        divider.setObjectName("step-divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        self.lay.addWidget(divider)


class StepWelcome(StepBase):
    def __init__(self, parent=None):
        super().__init__(0, 5, "Storyboard Studio", parent)
        # Сбросим заголовок шага для приветственного экрана
        for i in range(self.lay.count() - 1, -1, -1):
            w = self.lay.itemAt(i).widget()
            if w:
                w.setParent(None)
        self.lay.setContentsMargins(60, 60, 60, 40)
        self.lay.setSpacing(20)

        big = QLabel("Storyboard Studio")
        big.setObjectName("h1")
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(big)

        desc = QLabel(
            "Инструмент для генерации сторибордов вертикальных сериалов.\n\n"
            "Этот мастер настроит всё за 2 минуты:\n"
            "  • Проверит что у тебя установлен Python\n"
            "  • Скачает проект с GitHub\n"
            "  • Спросит твой Fast Gen AI ключ\n"
            "  • Поможет установить Claude Code"
        )
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(desc)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        start_btn = QPushButton("Начать установку →")
        start_btn.setObjectName("primary")
        start_btn.setMinimumHeight(40)
        start_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(start_btn)
        btn_row.addStretch()
        self.lay.addLayout(btn_row)


class StepPython(StepBase):
    def __init__(self, parent=None):
        super().__init__(1, 5, "Проверка Python", parent)

        info = QLabel("Storyboard Studio работает на Python 3.\nПроверяю что он установлен…")
        info.setObjectName("desc")
        info.setWordWrap(True)
        self.lay.addWidget(info)

        self.status = QLabel("Проверяю…")
        self.status.setObjectName("desc")
        self.lay.addWidget(self.status)

        self.action_btn = QPushButton("Скачать Python (откроется сайт)")
        self.action_btn.clicked.connect(self._open_python_site)
        self.action_btn.hide()
        self.lay.addWidget(self.action_btn)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.next_btn = QPushButton("Продолжить →")
        self.next_btn.setObjectName("primary")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(self.next_btn)
        self.lay.addLayout(btn_row)

        QTimer.singleShot(500, self._check_python)

    def _check_python(self):
        py = find_system_python()
        if py:
            try:
                # 2026-05-08: CREATE_NO_WINDOW guard для Win — иначе при
                # каждом старте установщика мигает чёрное cmd-окно.
                run_kwargs = dict(capture_output=True, text=True, timeout=5)
                if IS_WINDOWS:
                    run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
                r = subprocess.run([py, "--version"], **run_kwargs)
                if r.returncode == 0:
                    ver = r.stdout.strip() or r.stderr.strip()
                    self.status.setText(f"✓ {ver} установлен")
                    self.status.setStyleSheet("font-size: 13px; color: #6db86d;")
                    self.next_btn.setEnabled(True)
                    return
            except Exception:
                pass
        # Не нашли
        self.status.setText("✗ Python не найден на компьютере")
        self.status.setStyleSheet("font-size: 13px; color: #cc6666;")
        self.action_btn.show()

    def _open_python_site(self):
        url = "https://www.python.org/downloads/"
        if IS_MAC:
            subprocess.run(["open", url])
        elif IS_WINDOWS:
            os.startfile(url)
        QMessageBox.information(
            self, "Установи Python",
            "Откроется сайт python.org.\n\n"
            "1. Нажми кнопку 'Download Python'\n"
            "2. Запусти установщик\n"
            "3. ВАЖНО (Windows): поставь галочку 'Add Python to PATH'\n"
            "4. Вернись сюда и нажми 'Проверить ещё раз'"
        )
        self.action_btn.setText("Проверить ещё раз")
        self.action_btn.clicked.disconnect()
        self.action_btn.clicked.connect(self._check_python)


class StepDownload(StepBase):
    project_path: Optional[Path] = None

    def __init__(self, parent=None):
        super().__init__(2, 5, "Скачивание проекта", parent)

        info = QLabel(
            "Сейчас будет скачан проект storyboard-automation с GitHub.\n"
            "Выбери куда его положить (например, в Документы):"
        )
        info.setObjectName("desc")
        info.setWordWrap(True)
        self.lay.addWidget(info)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(str(Path.home() / "Documents"))
        path_row.addWidget(self.path_edit, stretch=1)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        self.lay.addLayout(path_row)

        self.download_btn = QPushButton("📥  Скачать проект")
        self.download_btn.clicked.connect(self._start_download)
        self.lay.addWidget(self.download_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        self.lay.addWidget(self.progress_bar)

        self.status = QLabel("")
        self.status.setObjectName("desc")
        self.lay.addWidget(self.status)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.next_btn = QPushButton("Продолжить →")
        self.next_btn.setObjectName("primary")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(self.next_btn)
        self.lay.addLayout(btn_row)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Выбери папку куда положить проект", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)

    def _start_download(self):
        dest = Path(self.path_edit.text())
        if not dest.is_dir():
            QMessageBox.warning(self, "Ошибка", "Выбери существующую папку")
            return

        if GITHUB_USER == "PLACEHOLDER_USER":
            # Резервный режим — просим выбрать существующую папку
            QMessageBox.warning(self, "GitHub не настроен",
                "Установщик ещё не подключён к GitHub.\n"
                "Выбери уже скачанную папку проекта вручную.")
            folder = QFileDialog.getExistingDirectory(
                self, "Выбери папку storyboard-automation", str(dest))
            if folder:
                self.project_path = Path(folder)
                self.status.setText(f"✓ Использую: {folder}")
                self.status.setStyleSheet("font-size: 13px; color: #6db86d;")
                self.next_btn.setEnabled(True)
            return

        self.download_btn.setEnabled(False)
        self.progress_bar.show()
        self.thread = DownloadProjectThread(dest)
        self.thread.progress.connect(self._on_progress)
        self.thread.finished.connect(self._on_done)
        self.thread.error.connect(self._on_error)
        self.thread.start()

    def _on_progress(self, pct: int, msg: str):
        self.progress_bar.setValue(pct)
        self.status.setText(msg)

    def _on_done(self, path: Path):
        self.project_path = path
        self.status.setText(f"✓ Скачано: {path}")
        self.status.setStyleSheet("font-size: 13px; color: #6db86d;")
        self.next_btn.setEnabled(True)

    def _on_error(self, msg: str):
        self.download_btn.setEnabled(True)
        self.status.setText(f"✗ Ошибка: {msg}")
        self.status.setStyleSheet("font-size: 13px; color: #cc6666;")


class StepKey(StepBase):
    project_path: Optional[Path] = None

    def __init__(self, parent=None):
        super().__init__(3, 5, "Введи Fast Gen AI ключ", parent)

        info = QLabel(
            "Этот ключ нужен для генерации картинок.\n"
            "Получи его у админа проекта (одноразово, потом запомнится)."
        )
        info.setObjectName("desc")
        info.setWordWrap(True)
        self.lay.addWidget(info)

        key_label = QLabel("API ключ:")
        key_label.setObjectName("h2")
        self.lay.addWidget(key_label)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Вставь ключ сюда…")
        self.key_input.textChanged.connect(self._on_key_changed)
        self.lay.addWidget(self.key_input)

        self.status = QLabel("")
        self.status.setObjectName("desc")
        self.lay.addWidget(self.status)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.skip_btn = QPushButton("Пропустить")
        self.skip_btn.setObjectName("secondary")
        self.skip_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(self.skip_btn)
        self.next_btn = QPushButton("Сохранить и продолжить →")
        self.next_btn.setObjectName("primary")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self._save_key)
        btn_row.addWidget(self.next_btn)
        self.lay.addLayout(btn_row)

    def set_project_path(self, path: Path):
        self.project_path = path
        # Если .env уже есть — показываем что ключ установлен
        env = path / ".env"
        if env.exists():
            content = env.read_text(encoding="utf-8").strip()
            if content and not content.startswith("paste"):
                self.status.setText(f"✓ Ключ уже установлен (длина: {len(content)})")
                self.status.setStyleSheet("font-size: 13px; color: #6db86d;")
                self.next_btn.setEnabled(True)
                self.next_btn.setText("Продолжить →")

    def _on_key_changed(self, text: str):
        self.next_btn.setEnabled(len(text.strip()) >= 8)

    def _save_key(self):
        if not self.project_path:
            QMessageBox.warning(self, "Ошибка", "Сначала скачай проект")
            return
        key = self.key_input.text().strip()
        if not key:
            self.next_requested.emit()
            return
        env = self.project_path / ".env"
        env.write_text(key + "\n", encoding="utf-8")
        self.status.setText("✓ Ключ сохранён")
        self.status.setStyleSheet("font-size: 13px; color: #6db86d;")
        self.next_requested.emit()


class StepClaudeCode(StepBase):
    """Шаг 4: установка Claude Code CLI (нужен для автообновления geometry
    после регенерации картинок локаций в Storyboard Studio).

    Логика:
      1. При входе — проверяем установлен ли уже CLI (find_claude_cli_path).
         Если да → показываем «✓ установлен» + пропускаем тяжёлые шаги.
      2. Если нет → большая кнопка «Установить автоматически» запускает
         InstallClaudeCliThread (curl|bash на Mac, irm|iex в PowerShell на
         Win). Прогресс-бар + статус.
      3. После установки → кнопка «Открыть терминал для входа» — запускает
         внешний терминал с командой `claude login` (юзер вручную проходит
         OAuth-флоу в браузере).
      4. Кнопка «Уже установлен / Пропустить» — для тех у кого CLI стоит
         или кто не хочет автомат geometry.
    """
    def __init__(self, parent=None):
        super().__init__(4, 5, "Установи Claude Code CLI", parent)
        self._install_thread: Optional[InstallClaudeCliThread] = None
        self._auth_thread:    Optional[CheckClaudeAuthThread]  = None
        self._cli_path: Optional[str] = None
        # Текущее состояние из CheckClaudeAuthThread:
        # 'unknown' / 'checking' / 'ready' / 'not_logged_in' / 'not_installed' / 'error'
        self._state: str = 'unknown'

        info = QLabel(
            "Claude Code CLI — это инструмент Anthropic, который Storyboard "
            "Studio использует для АВТОМАТИЧЕСКОГО обновления описаний "
            "локаций (geometry-файлов) после того как ты перегенерируешь "
            "картинку локации.\n\n"
            "Без него всё равно работает — но описания нужно будет обновлять "
            "вручную через попап с фразой для копирования."
        )
        info.setObjectName("desc")
        info.setWordWrap(True)
        self.lay.addWidget(info)

        # Статусная плашка (заполняется в _refresh_status)
        self.status_lbl = QLabel("Проверяю установку…")
        self.status_lbl.setObjectName("desc")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(
            "padding: 14px 16px; background: #1a1424; "
            "border: 1px solid #322545; border-radius: 8px; font-size: 13px;")
        self.lay.addWidget(self.status_lbl)

        # Прогресс-бар установки (изначально скрыт)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)   # неопределённый (busy indicator)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        self.lay.addWidget(self.progress)

        # Кнопки действий
        self.install_btn = QPushButton("⬇  Установить Claude Code CLI автоматически")
        self.install_btn.setObjectName("primary")
        self.install_btn.setMinimumHeight(40)
        self.install_btn.clicked.connect(self._on_install)
        self.lay.addWidget(self.install_btn)

        self.login_btn = QPushButton("🔑  Открыть терминал и войти в Claude")
        self.login_btn.setObjectName("primary")
        self.login_btn.setMinimumHeight(40)
        self.login_btn.clicked.connect(self._open_terminal_login)
        self.login_btn.hide()
        self.lay.addWidget(self.login_btn)

        # Кнопка повторной проверки (видима только в not_logged_in / error
        # состоянии — для тех кто только что залогинился вручную в терминале
        # и хочет чтобы плашка обновилась без ручного клика «Назад»)
        self.recheck_btn = QPushButton("🔄  Проверить ещё раз")
        self.recheck_btn.setObjectName("secondary")
        self.recheck_btn.setMinimumHeight(34)
        self.recheck_btn.clicked.connect(self._refresh_status)
        self.recheck_btn.hide()
        self.lay.addWidget(self.recheck_btn)

        login_hint = QLabel(
            "В открывшемся терминале выбери «Use existing subscription» и "
            "войди под общим аккаунтом (админ проекта подскажет логин).\n"
            "После «✓ Logged in as ...» закрой терминал и нажми "
            "«🔄 Проверить ещё раз» — плашка должна стать зелёной."
        )
        login_hint.setObjectName("desc")
        login_hint.setWordWrap(True)
        login_hint.setStyleSheet("font-size: 11px; color: #888;")
        self.login_hint = login_hint
        self.lay.addWidget(login_hint)
        login_hint.hide()

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        skip = QPushButton("Пропустить")
        skip.setObjectName("secondary")
        skip.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(skip)
        self.next_btn = QPushButton("Продолжить →")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(self.next_btn)
        self.lay.addLayout(btn_row)

        # При первом показе шага — проверяем установку
        QTimer.singleShot(50, self._refresh_status)

    def showEvent(self, ev):
        super().showEvent(ev)
        # Перепроверяем при каждом возврате на этот шаг (например юзер
        # установил вручную и вернулся через «Назад»)
        self._refresh_status()

    def _refresh_status(self):
        """Двухступенчатая проверка состояния:

        1. МГНОВЕННО — проверяем есть ли бинарник на диске
           (find_claude_cli_path). Если нет → показываем UI «нет CLI».
        2. Если бинарник есть → запускаем фоновую `CheckClaudeAuthThread`
           которая делает короткий headless-запрос (`claude -p "ok"`) и
           определяет реально ли работает (то есть есть ли auth-токен).
           Пока проверка идёт — показываем «🔍 Проверяю авторизацию…».
           По результату: 'ready' / 'not_logged_in' / 'error' → UI.
        """
        self._cli_path = find_claude_cli_path()
        if not self._cli_path:
            self._set_state('not_installed')
            return
        # Бинарник есть — запускаем фоновую проверку auth
        self._set_state('checking')
        # Если предыдущий thread ещё бежит — не запускаем новый (избегаем
        # одновременных параллельных проверок если юзер быстро прыгает по шагам)
        if self._auth_thread is not None and self._auth_thread.isRunning():
            return
        self._auth_thread = CheckClaudeAuthThread()
        self._auth_thread.status_ready.connect(self._on_auth_check_done)
        self._auth_thread.start()

    def _on_auth_check_done(self, state: str, detail: str):
        """Слот результата CheckClaudeAuthThread."""
        self._set_state(state, detail)

    # --- единая точка обновления UI по состоянию ---

    def _set_state(self, state: str, detail: str = ''):
        """Переключает плашку и видимость кнопок под новое состояние.

        Состояния:
          'not_installed' — серая плашка + кнопка «Установить»
          'checking'      — жёлтая плашка «🔍 Проверяю авторизацию…»
                            (показывается ~5-15с пока claude -p отрабатывает)
          'ready'         — ЗЕЛЁНАЯ плашка «✓ всё готово» + только Продолжить
          'not_logged_in' — оранжевая плашка + кнопка «Открыть терминал»
          'error'         — красная плашка + кнопка «Открыть терминал» (на всякий)
        """
        self._state = state

        # Скрыть всё, потом показать нужное
        self.install_btn.hide()
        self.login_btn.hide()
        self.login_hint.hide()
        self.recheck_btn.hide()

        if state == 'not_installed':
            ostxt = "macOS" if IS_MAC else ("Windows" if IS_WINDOWS else sys.platform)
            self.status_lbl.setText(
                f"Claude Code CLI не найден на твоей машине.\n"
                f"Нажми кнопку ниже — установщик скачает официальный "
                f"native-инсталлер Anthropic ({ostxt}). Это займёт 30-90 секунд.")
            self.status_lbl.setStyleSheet(
                "padding: 14px 16px; background: #1a1424; "
                "border: 1px solid #322545; border-radius: 8px; "
                "color: #ddd; font-size: 13px;")
            self.install_btn.show()

        elif state == 'checking':
            self.status_lbl.setText(
                f"🔍  Claude Code CLI найден ({self._cli_path or '?'}).\n"
                f"Проверяю авторизацию… (5-15 секунд)")
            self.status_lbl.setStyleSheet(
                "padding: 14px 16px; background: #2a1f0a; "
                "border: 1px solid #4a3010; border-radius: 8px; "
                "color: #ffcc66; font-size: 13px;")

        elif state == 'ready':
            self.status_lbl.setText(
                f"✓  Всё готово!\n"
                f"Claude CLI установлен и авторизован.\n\n"
                f"Storyboard Studio будет автоматически обновлять описания "
                f"локаций после регенерации картинок — без твоего участия. "
                f"Можешь продолжать.")
            self.status_lbl.setStyleSheet(
                "padding: 14px 16px; background: #14241a; "
                "border: 1px solid #2d5535; border-radius: 8px; "
                "color: #b0e0b0; font-size: 13px;")

        elif state == 'not_logged_in':
            self.status_lbl.setText(
                f"⚠  CLI установлен, но не залогинен.\n\n"
                f"Нажми кнопку ниже — откроется терминал с командой "
                f"авторизации. Войди под общим аккаунтом Claude (админ "
                f"проекта подскажет логин), потом закрой терминал и "
                f"нажми «🔄 Проверить ещё раз».")
            self.status_lbl.setStyleSheet(
                "padding: 14px 16px; background: #2a1f0a; "
                "border: 1px solid #4a3010; border-radius: 8px; "
                "color: #ffcc66; font-size: 13px;")
            self.login_btn.show()
            self.recheck_btn.show()
            self.login_hint.show()

        elif state == 'error':
            self.status_lbl.setText(
                f"⚠  Не удалось проверить статус автоматически:\n{detail[:200]}\n\n"
                f"Попробуй открыть терминал и войти вручную — или просто "
                f"нажми «Пропустить» (geometry будет обновляться вручную "
                f"через попап с фразой для копирования).")
            self.status_lbl.setStyleSheet(
                "padding: 14px 16px; background: #2a0a14; "
                "border: 1px solid #4a1020; border-radius: 8px; "
                "color: #ff8080; font-size: 13px;")
            self.login_btn.show()
            self.recheck_btn.show()

    def _on_install(self):
        if self._install_thread is not None and self._install_thread.isRunning():
            return
        self.install_btn.setEnabled(False)
        self.next_btn.setEnabled(False)
        self.progress.show()
        self.status_lbl.setText("Устанавливаю Claude Code CLI… (~30-90 секунд)")
        self.status_lbl.setStyleSheet(
            "padding: 14px 16px; background: #2a1f0a; "
            "border: 1px solid #4a3010; border-radius: 8px; "
            "color: #ffcc66; font-size: 13px;")
        self._install_thread = InstallClaudeCliThread()
        self._install_thread.progress.connect(self.status_lbl.setText)
        self._install_thread.finished.connect(self._on_install_done)
        self._install_thread.error.connect(self._on_install_error)
        self._install_thread.start()

    def _on_install_done(self, cli_path: str):
        self.progress.hide()
        self.install_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self._refresh_status()  # покажет «✓ установлен» + кнопку login

    def _on_install_error(self, msg: str):
        self.progress.hide()
        self.install_btn.setEnabled(True)
        self.next_btn.setEnabled(True)
        self.status_lbl.setText(
            f"✗ Не удалось установить автоматически:\n{msg}\n\n"
            f"Попробуй вручную в терминале: "
            f"{'curl -fsSL https://claude.ai/install.sh | bash' if IS_MAC else 'irm https://claude.ai/install.ps1 | iex'}")
        self.status_lbl.setStyleSheet(
            "padding: 14px 16px; background: #2a0a14; "
            "border: 1px solid #4a1020; border-radius: 8px; "
            "color: #ff8080; font-size: 13px;")

    def _open_terminal_login(self):
        """Открывает внешний терминал с командой `claude login`.
        macOS: AppleScript → Terminal.app
        Windows: cmd /K claude login (новое окно)
        """
        try:
            if IS_MAC:
                # AppleScript: новое окно Terminal с командой
                subprocess.Popen([
                    "osascript", "-e",
                    'tell application "Terminal" to do script "claude login"',
                    "-e",
                    'tell application "Terminal" to activate'
                ])
            elif IS_WINDOWS:
                # /K = выполнить и оставить окно открытым
                subprocess.Popen(
                    ["cmd", "/K", "claude login"],
                    creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                QMessageBox.information(
                    self, "Открой терминал вручную",
                    "Открой свой терминал и выполни команду:\n\nclaude login")
        except Exception as e:
            QMessageBox.warning(
                self, "Не удалось открыть терминал",
                f"Открой терминал вручную и выполни:\n\nclaude login\n\n({e})")


class StepDone(StepBase):
    """Финальный шаг — установщик закончил свою работу.
    Storyboard Studio.app — отдельное приложение, его юзер скачивает
    отдельно (от админа или с GitHub Releases). Установщик НЕ пытается
    его запустить (раньше была кнопка «Запустить» которая падала с
    ошибкой Python если .app не находился). Вместо этого — просто
    инструкция + кнопка «Открыть папку проекта»."""
    project_path: Optional[Path] = None
    open_folder_requested = pyqtSignal()
    close_requested       = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(5, 5, "Всё готово!", parent)

        big = QLabel("🎉  Установка завершена")
        big.setObjectName("h1")
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(big)

        desc = QLabel(
            "Установлено:\n"
            "  ✓  Проект Storyboard Automation скачан с GitHub\n"
            "  ✓  Fast Gen AI ключ сохранён\n"
            "  ✓  Claude Code CLI установлен и авторизован\n\n"
            "Что дальше:\n"
            "  • Скачай и запусти Storyboard Studio.app — это отдельное\n"
            "    приложение, ссылку получишь от админа проекта.\n"
            "  • При запуске Storyboard Studio укажи папку проекта (та куда\n"
            "    сейчас распаковался репозиторий)."
        )
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.lay.addWidget(desc)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        folder_btn = QPushButton("📁  Открыть папку проекта")
        folder_btn.setObjectName("secondary")
        folder_btn.setMinimumHeight(40)
        folder_btn.clicked.connect(self.open_folder_requested.emit)
        btn_row.addWidget(folder_btn)
        close_btn = QPushButton("Закрыть установщик")
        close_btn.setObjectName("primary")
        close_btn.setMinimumHeight(40)
        close_btn.clicked.connect(self.close_requested.emit)
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        self.lay.addLayout(btn_row)


# ─── Главное окно установщика ────────────────────────────────────────────────

class InstallerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Storyboard Studio Installer")
        self.setFixedSize(640, 520)
        self.project_path: Optional[Path] = None

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.welcome = StepWelcome()
        self.python  = StepPython()
        self.dl      = StepDownload()
        self.key     = StepKey()
        self.cc      = StepClaudeCode()
        self.done    = StepDone()

        for step in [self.welcome, self.python, self.dl, self.key, self.cc, self.done]:
            self.stack.addWidget(step)

        self.welcome.next_requested.connect(lambda: self._goto(1))
        self.python.next_requested.connect(lambda: self._goto(2))
        self.dl.next_requested.connect(self._after_download)
        self.key.next_requested.connect(lambda: self._goto(4))
        self.cc.next_requested.connect(lambda: self._goto(5))
        self.done.open_folder_requested.connect(self._open_project_folder)
        self.done.close_requested.connect(self.close)

    def _goto(self, idx: int):
        self.stack.setCurrentIndex(idx)

    def _after_download(self):
        self.project_path = self.dl.project_path
        if self.project_path:
            self.key.set_project_path(self.project_path)
            self.done.project_path = self.project_path
        self._goto(3)

    def _open_project_folder(self):
        """Открывает папку проекта в Finder (Mac) / Explorer (Windows).
        Также сохраняет путь в QSettings — Storyboard Studio при первом
        запуске сможет автоматически его подхватить."""
        # Сохраним путь проекта в QSettings (тот же ключ что использует Storyboard Studio)
        from PyQt6.QtCore import QSettings
        if self.project_path:
            QSettings("StoryboardStudio", "StoryboardApp").setValue(
                "project_root", str(self.project_path))

        if not self.project_path or not self.project_path.exists():
            QMessageBox.warning(
                self, "Папка проекта не найдена",
                "Не удалось определить путь к установленному проекту.")
            return
        try:
            if IS_MAC:
                subprocess.Popen(["open", str(self.project_path)])
            elif IS_WINDOWS:
                os.startfile(str(self.project_path))
        except Exception as e:
            QMessageBox.warning(
                self, "Не удалось открыть папку",
                f"{self.project_path}\n\n{e}")


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    app = QApplication(sys.argv)
    app.setApplicationName("Storyboard Studio Installer")
    app.setStyleSheet(DARK)
    win = InstallerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
