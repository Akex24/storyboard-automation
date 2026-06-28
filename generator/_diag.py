# -*- coding: utf-8 -*-
"""ВРЕМЕННАЯ диагностика (удалить после поимки бага «реф на тяжёлой 4K ломает плитку»).

Пишет строки с таймстампом в ~/storyboard_diag.log (домашняя папка юзера —
кроссплатформенно: macOS /Users/<имя>/, Windows C:\\Users\\<имя>\\). Любая ошибка записи
проглатывается — диагностика НЕ должна влиять на работу приложения.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

_DIAG_PATH = Path.home() / "storyboard_diag.log"


def diag(msg: str) -> None:
    """Дописать строку с таймстампом (HH:MM:SS.mmm) в лог-файл."""
    try:
        with open(_DIAG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], str(msg)))
    except Exception:
        pass
