"""Общие примитивы запуска claude CLI без упирания в Windows command-line
лимит (~8 KB через cmd.exe для .cmd-shim, ~32 KB через CreateProcess).

Два канала:
  • system-prompt → временный файл, передаётся как `--system-prompt-file <path>`
    (на старом CLI без этого флага — fallback на `--system-prompt <text>`,
    поведение остаётся «как сейчас»: на Win лимит вернётся, но это не
    наша новая регрессия — это исходное состояние).
  • user-prompt   → stdin (`Popen(stdin=PIPE)` → `write` → `close`).

Кросс-платформенно: на win32 — `creationflags=CREATE_NO_WINDOW`, на остальных
— без флагов. Пути — `pathlib.Path` + `str()`. tempfile.mkstemp с явным unlink
в caller'е через try/finally.

Caller паттерн (single source of truth):

    from threads._claude_shared import (
        write_system_prompt_to_tmp, popen_kwargs_for_claude,
        build_system_prompt_args, send_prompt_via_stdin,
    )

    sysp = write_system_prompt_to_tmp(system_prompt) if system_prompt else None
    try:
        cmd = [cli, "-p"] + build_system_prompt_args(cli, sysp, system_prompt) + [
              "--output-format", "stream-json",
              "--model", model]
        proc = subprocess.Popen(cmd, **popen_kwargs_for_claude(
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE))
        send_prompt_via_stdin(proc, user_prompt)
        # Сразу проверяем не помер ли claude — иначе следующий Win-баг
        # станет «нет ответа» (молчаливый exit). См. raise_if_died_early.
        raise_if_died_early(proc)
        # … обычный read loop / wait …
    finally:
        if sysp is not None:
            try: sysp.unlink(missing_ok=True)
            except Exception: pass
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict, Any


# Кешируем результат feature-detection ТОЛЬКО при True. False/exception НЕ
# кешируем — на медленном Win cold-start `claude --help` может таймаутнуть
# и тогда мы бы зафиксировали False навсегда → фикс молча отвалится. При
# False/exception следующий вызов попробует заново; стоимость — лишний
# `--help` запуск раз в N callsite'ов до первого успеха.
_SUPPORTS_SYSPROMPT_FILE: Optional[bool] = None


def supports_system_prompt_file(cli: str) -> bool:
    """True если установленная версия claude CLI принимает
    `--system-prompt-file <path>`. Проверяется через `claude --help`.

    Кешируем ТОЛЬКО True (см. комментарий у `_SUPPORTS_SYSPROMPT_FILE`).
    На таймаут / exit != 0 / отсутствие текста — возвращаем False БЕЗ
    кеширования: следующий callsite попробует снова.
    """
    global _SUPPORTS_SYSPROMPT_FILE
    if _SUPPORTS_SYSPROMPT_FILE is True:
        return True
    try:
        kwargs: Dict[str, Any] = dict(
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        r = subprocess.run([cli, "--help"], **kwargs)
        help_text = (r.stdout or "") + (r.stderr or "")
        if "--system-prompt-file" in help_text:
            _SUPPORTS_SYSPROMPT_FILE = True
            return True
    except Exception:
        pass
    return False


def write_system_prompt_to_tmp(text: str) -> Path:
    """Записать system-prompt во временный UTF-8 файл и вернуть Path.

    Caller обязан удалить файл в finally:
        try: path.unlink(missing_ok=True)
        except Exception: pass

    Использует stdlib `tempfile.mkstemp` — атомарно создаёт уникальное имя,
    безопасно для параллельных Mode C-генераций (десятки thread'ов
    одновременно). suffix '.sysprompt.txt' для удобства диагностики
    «забытых» файлов при padении Studio.
    """
    fd, name = tempfile.mkstemp(suffix=".sysprompt.txt", text=False)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except Exception:
        try: os.unlink(name)
        except Exception: pass
        raise
    return Path(name)


def build_system_prompt_args(cli: str,
                              tmp_path: Optional[Path],
                              raw_text: str) -> List[str]:
    """Вернуть argv-кусок для передачи system-prompt в claude.

    Логика:
      • если флаг поддерживается И есть `tmp_path` → `["--system-prompt-file", str(path)]`
      • иначе → fallback `["--system-prompt", raw_text]` (как раньше; на Win лимит
        вернётся, но это исходное состояние, не наша новая регрессия)
      • если `raw_text` пустой → `[]` (system-prompt не задан)
    """
    if not raw_text:
        return []
    if tmp_path is not None and supports_system_prompt_file(cli):
        return ["--system-prompt-file", str(tmp_path)]
    return ["--system-prompt", raw_text]


def popen_kwargs_for_claude(**extra: Any) -> Dict[str, Any]:
    """Базовые kwargs для subprocess.Popen/run при запуске claude CLI.

    Дефолты: text=True, encoding='utf-8', errors='replace' (Win cp1252 fix);
    на win32 — creationflags=CREATE_NO_WINDOW. `extra` мерджится поверх.
    """
    kwargs: Dict[str, Any] = dict(
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    kwargs.update(extra)
    return kwargs


def send_prompt_via_stdin(proc: subprocess.Popen, user_prompt: str) -> None:
    """Запись user_prompt в child stdin + close. Подавляет BrokenPipe / OSError
    (claude мог упасть раньше — caller вызовет `raise_if_died_early`
    непосредственно после, чтобы получить понятную ошибку).

    Звать СРАЗУ после Popen, ДО чтения stdout — иначе claude висит на read(stdin).
    """
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(user_prompt)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # caller'у предстоит raise_if_died_early


def raise_if_died_early(proc: subprocess.Popen,
                         settle_ms: int = 50) -> None:
    """Если claude уже завершился с ненулевым кодом — вытащить stderr и
    бросить понятную ошибку. Иначе — ничего не делать.

    Звать СРАЗУ после `send_prompt_via_stdin`. Защита от «нет ответа»: если
    claude.cmd упал на старте (например argv overflow на Win, прежний симптом
    у коллеги), `proc.stdin.write` тихо проглотил BrokenPipe, а reader-loop
    висел бы на пустом stdout до общего таймаута. Здесь — мгновенный raise
    с stderr.

    `settle_ms` — крошечный sleep чтобы дать процессу шанс exit'нуть и
    обновить `.returncode` (без него poll() = None даже когда процесс умер
    наносекунду назад). 50мс — невидимо для UI, достаточно для exit-syscall.
    """
    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)
    if proc.poll() is None:
        return  # жив — продолжаем штатно
    if proc.returncode == 0:
        return  # «помер успешно» — пусть caller дочитает stdout
    stderr_text = ""
    try:
        if proc.stderr is not None:
            stderr_text = (proc.stderr.read() or "")[:500]
    except Exception:
        pass
    raise RuntimeError(
        f"claude died immediately: rc={proc.returncode} "
        f"stderr={stderr_text}")
