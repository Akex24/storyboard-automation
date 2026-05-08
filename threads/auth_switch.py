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
import time
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal


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
                    timeout=15, capture_output=True, text=True
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
                        timeout=10, capture_output=True, text=True
                    )
                elif sys.platform == 'win32':
                    # Предпочитаем Windows Terminal (wt.exe) если есть —
                    # современный UX. Иначе классический cmd.exe.
                    cli_quoted = f'"{self._cli}"'
                    wt = shutil.which('wt.exe') or shutil.which('wt')
                    CREATE_NEW_CONSOLE = 0x00000010
                    if wt:
                        subprocess.Popen(
                            [wt, 'new-tab', 'cmd.exe', '/K',
                             f'{cli_quoted} auth login'],
                            creationflags=CREATE_NEW_CONSOLE
                        )
                    else:
                        subprocess.Popen(
                            ['cmd.exe', '/K',
                             f'{cli_quoted} auth login'],
                            creationflags=CREATE_NEW_CONSOLE
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
        """Возвращает email текущего залогиненного аккаунта или None."""
        try:
            r = subprocess.run(
                [self._cli, "auth", "status"],
                timeout=10, capture_output=True, text=True
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout.strip() or "{}")
            if not data.get("loggedIn"):
                return None
            return data.get("email") or None
        except Exception:
            return None
