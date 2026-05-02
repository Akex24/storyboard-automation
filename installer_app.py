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

IS_MAC     = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


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
                r = subprocess.run([py, "--version"],
                                    capture_output=True, text=True, timeout=5)
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
    def __init__(self, parent=None):
        super().__init__(4, 5, "Установи Claude Code", parent)

        info = QLabel(
            "Claude Code — это инструмент в котором ты пишешь сценарий\n"
            "и получаешь готовые сториборды.\n\n"
            "Если у тебя уже установлен Claude Code — пропусти этот шаг."
        )
        info.setObjectName("desc")
        info.setWordWrap(True)
        self.lay.addWidget(info)

        download_btn = QPushButton("⬇  Открыть страницу скачивания Claude Code")
        download_btn.setObjectName("primary")
        download_btn.setMinimumHeight(40)
        download_btn.clicked.connect(self._open_download)
        self.lay.addWidget(download_btn)

        hint = QLabel(
            "После установки авторизуйся в Claude Code своим аккаунтом\n"
            "(админ проекта подскажет если нужен общий аккаунт)."
        )
        hint.setObjectName("desc")
        hint.setWordWrap(True)
        self.lay.addWidget(hint)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        skip = QPushButton("Уже установлен / Пропустить")
        skip.setObjectName("secondary")
        skip.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(skip)
        next_btn = QPushButton("Продолжить →")
        next_btn.setObjectName("primary")
        next_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(next_btn)
        self.lay.addLayout(btn_row)

    def _open_download(self):
        url = CLAUDE_CODE_DOWNLOAD_PAGE
        if IS_MAC:
            subprocess.run(["open", url])
        elif IS_WINDOWS:
            os.startfile(url)


class StepDone(StepBase):
    project_path: Optional[Path] = None
    launch_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(5, 5, "Всё готово!", parent)

        big = QLabel("🎉  Установка завершена")
        big.setObjectName("h1")
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(big)

        desc = QLabel(
            "Теперь ты можешь:\n\n"
            "  • Запустить Storyboard Studio чтобы смотреть сториборды\n"
            "  • Открыть Claude Code и работать со сценарием\n"
            "  • Получать обновления автоматически — кнопка появится в приложении"
        )
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lay.addWidget(desc)

        self.lay.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        launch_btn = QPushButton("🚀  Запустить Storyboard Studio")
        launch_btn.setObjectName("primary")
        launch_btn.setMinimumHeight(44)
        launch_btn.clicked.connect(self.launch_requested.emit)
        btn_row.addWidget(launch_btn)
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
        self.done.launch_requested.connect(self._launch_app)

    def _goto(self, idx: int):
        self.stack.setCurrentIndex(idx)

    def _after_download(self):
        self.project_path = self.dl.project_path
        if self.project_path:
            self.key.set_project_path(self.project_path)
            self.done.project_path = self.project_path
        self._goto(3)

    def _launch_app(self):
        # Сохраним путь проекта в QSettings (тот же ключ что использует Storyboard Studio)
        from PyQt6.QtCore import QSettings
        if self.project_path:
            QSettings("StoryboardStudio", "StoryboardApp").setValue(
                "project_root", str(self.project_path))

        # Пытаемся запустить установленный Storyboard Studio.app
        if IS_MAC:
            for candidate in [
                Path("/Applications/Storyboard Studio.app"),
                Path.home() / "Applications" / "Storyboard Studio.app",
            ]:
                if candidate.exists():
                    subprocess.Popen(["open", str(candidate)])
                    self.close()
                    return
            # Запасной вариант — Python скрипт в проекте
            if self.project_path:
                script = self.project_path / "storyboard_app.py"
                py = find_system_python()
                if script.exists() and py:
                    subprocess.Popen([py, str(script)])
                    self.close()
                    return
        elif IS_WINDOWS:
            for candidate in [
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Storyboard Studio" / "Storyboard Studio.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Storyboard Studio" / "Storyboard Studio.exe",
            ]:
                if candidate.exists():
                    subprocess.Popen([str(candidate)])
                    self.close()
                    return
            if self.project_path:
                script = self.project_path / "storyboard_app.py"
                py = find_system_python()
                if script.exists() and py:
                    subprocess.Popen([py, str(script)])
                    self.close()
                    return

        QMessageBox.information(
            self, "Установка завершена",
            f"Проект установлен в:\n{self.project_path}\n\n"
            "Storyboard Studio.app пока не найден — запусти его вручную "
            "после установки приложения."
        )
        self.close()


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
