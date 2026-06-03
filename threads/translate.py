# -*- coding: utf-8 -*-
"""threads/translate.py — батч-перевод реплик через Claude CLI (Haiku).

Этап 2 (2026-06-03): украинский перевод dialog.en на лету — в монтажной
карте лежат только ru+en, для uk перевода нет. ОДИН вызов
`claude -p --model claude-haiku-4-5` переводит СРАЗУ все непереведённые
реплики эпизода (батч), а не по одной — старт CLI (~8с) платится один раз
на эпизод, не на каждый клик. Результат кэшируется + персистится на диск.

Маршрут en→uk по ИНДЕКСУ (не по содержимому): шлём нумерованный список,
просим JSON-объект {"1": "...", ...} → маппим en[i] ↔ result[str(i+1)].
Устойчиво к перестановке и к переносам/цифрам/кавычкам внутри перевода.

Cross-platform: CREATE_NO_WINDOW на win32 (иначе мигает cmd-окно), без
shell, пути через str(). Паттерн зеркалит лёгкие CLI-треды (threads/generate).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from PyQt6.QtCore import QThread, pyqtSignal


# Код языка интерфейса → английское имя для промпта Haiku.
_LANG_NAME = {"uk": "Ukrainian", "ru": "Russian", "en": "English"}


class TranslateThread(QThread):
    """Батч-перевод списка английских реплик на target_lang через Claude CLI
    (Haiku). Эмитит result_ready(dict) — {en: uk} ТОЛЬКО для валидных непустых
    пар — при успехе, или failed(str) если запрос/парсинг провалился целиком."""

    result_ready = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, project_root, cli_path: str, texts: List[str],
                 target_lang: str, model: str = "claude-haiku-4-5", parent=None):
        super().__init__(parent)
        self._root = Path(project_root)
        self._cli = cli_path
        # Уникализируем + сохраняем порядок (на случай дублей в монтажке).
        seen = set()
        self._texts: List[str] = []
        for t in texts:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                self._texts.append(t)
        self._target = target_lang
        self._model = model

    @staticmethod
    def _strip_code_fence(s: str) -> str:
        """Снять возможную обёртку ```json … ``` вокруг ответа модели."""
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s)
        return s.strip()

    def run(self):
        if not self._texts:
            self.result_ready.emit({})
            return
        target_name = _LANG_NAME.get(self._target, self._target)
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(self._texts))
        prompt = (
            f"Translate each numbered line of dialogue from English to "
            f"{target_name}. Return ONLY a JSON object mapping each line "
            f"number (as a string key) to its translation — no quotes around "
            f"the whole object, no markdown, no commentary. Example: "
            f'{{"1": "...", "2": "..."}}.\n\nLines:\n{numbered}'
        )
        try:
            args = [self._cli, "--model", self._model, "-p", prompt,
                    "--dangerously-skip-permissions"]
            popen_kwargs = dict(
                cwd=str(self._root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",      # Win-fix: иначе cp1252 ловит UTF-8 → crash
                errors="replace",
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(args, timeout=120, **popen_kwargs)
            raw = (proc.stdout or "").strip()
            if proc.returncode != 0 or not raw:
                self.failed.emit(raw or "translate failed")
                return
            try:
                parsed = json.loads(self._strip_code_fence(raw))
            except Exception:
                self.failed.emit("bad JSON from model")
                return
            if not isinstance(parsed, dict):
                self.failed.emit("unexpected JSON shape")
                return
            # Маппинг по индексу: en[i] ↔ parsed[str(i+1)]. В результат идут
            # ТОЛЬКО валидные непустые пары; пропущенные останутся на следующий
            # клик. Кривые/пустые значения отбрасываем.
            mapping: Dict[str, str] = {}
            for i, en in enumerate(self._texts):
                uk = parsed.get(str(i + 1))
                if isinstance(uk, str) and uk.strip():
                    mapping[en] = uk.strip()
            self.result_ready.emit(mapping)
        except Exception as e:
            self.failed.emit(str(e))
