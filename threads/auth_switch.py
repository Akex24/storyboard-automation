# -*- coding: utf-8 -*-
"""
threads/auth_switch.py — поток смены AI-аккаунта в Studio.

Сценарий:
  1. logout текущего аккаунта (silent через `claude auth logout`).
  2. Открывает Терминал с командой `claude auth login` через osascript.
     Юзер в браузере жмёт «Authorize» — это ОДНО его действие, обойти нельзя
     (OAuth требует браузерного подтверждения).
  3. Polling `claude auth status` каждые 2с до loggedIn=true (или таймаут 5 мин).
  4. Эмитит `finished_ok(email)` или `failed(reason)`.

История: создано 2026-05-06 (фича «авто-плашка смены AI-аккаунта в Studio»).
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal


def _no_console_kwargs() -> dict:
    """CREATE_NO_WINDOW для Win — без него `auth status`/`logout` мигают
    чёрными cmd-окнами при каждом polling cycle (2с × до 150 раз = до
    5 мин мигания). На Mac/Linux — пустой dict (`creationflags` —
    Win-only параметр subprocess'а).

    Дублирует `storyboard_app.no_console_kwargs()` чтобы избежать
    `_AppProxy`-зависимости в этом тонком thread-файле.
    """
    if sys.platform == 'win32':
        return {'creationflags': 0x08000000}
    return {}


class AuthSwitchThread(QThread):
    """Запускает logout + login flow и polling до подтверждения нового аккаунта.

    Все шаги последовательные. Если на любом этапе fatal-error — эмитит
    `failed(reason)` и завершается. Stop через `.stop()` останавливает
    polling (но не убивает Терминал юзера — он сам закроет когда хочет).
    """

    progress = pyqtSignal(str)            # человеко-читаемый статус
    finished_ok = pyqtSignal(str)         # email нового аккаунта
    failed = pyqtSignal(str)              # reason для лога/UI

    POLL_INTERVAL_SEC = 2.0
    POLL_TIMEOUT_SEC = 300.0  # 5 минут — больше чем достаточно для OAuth

    def __init__(self, claude_cli_path: str, parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self) -> None:  # noqa: D401
        try:
            # 1) Запоминаем стартовый email — чтобы в polling считать «вход
            #    под НОВЫЙ аккаунт» завершённым (не просто loggedIn=true,
            #    а другой email чем был до logout).
            start_email = self._auth_status_email()

            # 2) logout (silent)
            self.progress.emit("logout")
            try:
                subprocess.run(
                    [self._cli, "auth", "logout"],
                    timeout=15, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                    **_no_console_kwargs(),  # 2026-05-09: без CREATE_NO_WINDOW мигало cmd-окно на Win.
                )
            except Exception:
                # logout мог упасть если уже разлогинен — не fatal, идём дальше
                pass

            if self._stop:
                self.failed.emit("cancelled")
                return

            # 3) Открыть терминал/командную строку с командой login.
            #    Кросс-платформенно: Mac → Terminal.app через osascript,
            #    Windows → cmd.exe (или Windows Terminal если установлен).
            self.progress.emit("opening_terminal")
            try:
                if sys.platform == 'darwin':
                    login_cmd = f"{shlex.quote(self._cli)} auth login"
                    apple = (
                        'tell application "Terminal" to activate\n'
                        'tell application "Terminal" to do script '
                        f'"{login_cmd}"'
                    )
                    subprocess.run(
                        ["osascript", "-e", apple],
                        timeout=10, capture_output=True, text=True,
                        encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                    )
                elif sys.platform == 'win32':
                    # 2026-05-09: ранее использовали `cmd /K "<cli> auth
                    # login"` (+ опционально wt.exe wrapper). На конфигурациях
                    # где `find_claude_cli` резолвит .cmd-шим (npm install)
                    # двойной cmd-wrapping ломал quoting → окно открывалось
                    # и сразу закрывалось, claude не успевал напечатать
                    # OAuth URL → браузер не дёргался. Юзер видел только
                    # «мигающие чёрные окна» (на самом деле часть мигания —
                    # это polling без CREATE_NO_WINDOW, см. _auth_status_email,
                    # часть — этот crash'нувший cmd /K).
                    #
                    # Решение: создать .bat в %TEMP% с явным claude вызовом
                    # и `pause >nul` в конце. Окно остаётся открытым пока
                    # юзер не закроет — он может прочитать инструкции и
                    # увидеть OAuth URL если браузер не открылся автоматически.
                    bat_dir = Path(tempfile.gettempdir())
                    bat_path = (bat_dir
                                / f"storyboard_auth_login_{int(time.time())}.bat")
                    bat_content = (
                        "@echo off\r\n"
                        "chcp 65001 >nul\r\n"  # UTF-8 для русских строк
                        "echo.\r\n"
                        "echo === Storyboard Studio: вход в Max-аккаунт ===\r\n"
                        "echo Сейчас откроется браузер. Войди под нужным\r\n"
                        "echo Max-аккаунтом и подтверди разрешение.\r\n"
                        "echo.\r\n"
                        f'"{self._cli}" auth login\r\n'
                        "echo.\r\n"
                        "echo Готово. Нажми любую клавишу чтобы закрыть окно.\r\n"
                        "pause >nul\r\n"
                    )
                    bat_path.write_bytes(bat_content.encode('utf-8'))
                    CREATE_NEW_CONSOLE = 0x00000010
                    subprocess.Popen(
                        ['cmd.exe', '/c', str(bat_path)],
                        creationflags=CREATE_NEW_CONSOLE,
                    )
                else:
                    # Linux — попытка через xterm/gnome-terminal/konsole.
                    term = (shutil.which('gnome-terminal')
                            or shutil.which('konsole')
                            or shutil.which('xterm'))
                    if term:
                        subprocess.Popen(
                            [term, '-e',
                             f'{shlex.quote(self._cli)} auth login']
                        )
                    else:
                        self.failed.emit("terminal_open_failed: no terminal app")
                        return
            except Exception as e:
                self.failed.emit(f"terminal_open_failed: {e}")
                return

            # 4) Polling до loggedIn=true с (по возможности) другим email.
            #    Если юзер случайно залогинился в ТОТ ЖЕ аккаунт — детектим
            #    это специальным "same_account:<email>" сигналом, чтобы UI
            #    мог сказать «это опять тот же аккаунт, выбери другой».
            self.progress.emit("waiting_for_login")
            deadline = time.monotonic() + self.POLL_TIMEOUT_SEC
            new_email: Optional[str] = None
            same_account_email: Optional[str] = None
            while time.monotonic() < deadline:
                if self._stop:
                    self.failed.emit("cancelled")
                    return
                time.sleep(self.POLL_INTERVAL_SEC)
                cur_email = self._auth_status_email()
                if cur_email and cur_email != start_email:
                    new_email = cur_email
                    break
                # Если start_email был None и сейчас logged in — тоже success.
                if cur_email and start_email is None:
                    new_email = cur_email
                    break
                # Если юзер залогинился в тот же email — даём ему ещё немного
                # времени (вдруг это промежуточная стадия), но запоминаем для
                # таймаут-репорта.
                if cur_email and cur_email == start_email:
                    same_account_email = cur_email

            if new_email:
                self.finished_ok.emit(new_email)
            elif same_account_email:
                # Юзер залогинился, но в ТОТ ЖЕ аккаунт — это явный признак
                # что он не понял что нужно сменить email. Сигналим UI с
                # человекочитаемым reason.
                self.failed.emit(f"same_account:{same_account_email}")
            else:
                self.failed.emit("timeout")
        except Exception as e:
            self.failed.emit(f"unexpected: {e}")

    # ── helpers ──

    def _auth_status_email(self) -> Optional[str]:
        """Возвращает email текущего залогиненного аккаунта или None.

        2026-05-09: добавлен CREATE_NO_WINDOW для Win — этот метод
        вызывается до 150 раз в polling-цикле (2с × 5 мин). Без флага
        каждый вызов мигал чёрным cmd-окном — юзер видел «мигающие окна
        которые нельзя закрыть».
        """
        try:
            r = subprocess.run(
                [self._cli, "auth", "status"],
                timeout=10, capture_output=True, text=True,
                encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                **_no_console_kwargs(),
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout.strip() or "{}")
            if not data.get("loggedIn"):
                return None
            return data.get("email") or None
        except Exception:
            return None
