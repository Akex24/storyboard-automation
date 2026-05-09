# -*- coding: utf-8 -*-
"""
threads/storyboard_pipeline.py — pipeline записи .txt промптов
сторибордов из утверждённой монтажной карты.

Поток:
  Для каждого блока карты:
    1. claude -p PromptWriter  (system: STORYBOARD_WRITER_SYSTEM)
       → текст готового файла промпта (с шапкой тегов)
    2. Запись в `output/prompts/<ep>_block_N.txt`
    3. Эмит сигнал `block_prompt_ready(block_n, block_filename)`
       — MainWindow ловит и запускает GenerateThread по шотам этого
       блока через свою существующую очередь.

Pipeline ОТВЕЧАЕТ ТОЛЬКО за создание .txt промптов. Саму генерацию
изображений делает MainWindow через GenerateThread (per-shot
NARWHAL/Nano Banana) — не дублируем.

История: создано 2026-05-06 (фича Этап 2 — генерация сторибордов).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from agents.storyboard_writer_prompts import (
    SYSTEM as STORYBOARD_WRITER_SYSTEM,
    build_user_prompt as build_storyboard_writer_user_prompt,
)


class StoryboardPipelineThread(QThread):
    """Фон-поток — итерируется по блокам карты, для каждого вызывает
    PromptWriter (claude -p) и пишет результат в `output/prompts/`.

    На вход:
      • claude_cli_path — путь к исполняемому файлу `claude`
      • montage_card    — JSON-карта от Сценариста (с blocks[])
      • refs_summary    — словарь {locations:[], objects:[], characters:[]}
                          (как _build_refs_summary_for_orchestrator)
      • characters_dict — {slug: english_name} — для подстановки имён
                          в промпт (например {"muzh":"David", ...})
      • ep_id           — например "ep1" — используется в имени файла
                          `<ep_id>_block_N.txt`
      • prompts_dir     — Path где хранить .txt
                          (`shows/<show>/output/prompts/`)
      • model           — id модели CLI (`claude-opus-4-7` и т.п.).
                          Если None — берётся дефолт CLI.

    Сигналы (для UI):
      • block_started(block_n)            — стартует PromptWriter
      • block_prompt_ready(block_n, filename)
                                          — .txt записан, можно
                                            запускать шоты
      • block_failed(block_n, msg)        — провал PromptWriter
                                            или записи (ошибка не
                                            прерывает pipeline; идём
                                            дальше)
      • all_done(success_count, fail_count)
      • aborted()                         — после явной остановки
    """

    block_started = pyqtSignal(int)
    block_prompt_ready = pyqtSignal(int, str)
    block_failed = pyqtSignal(int, str)
    all_done = pyqtSignal(int, int)
    aborted = pyqtSignal()

    SUBPROCESS_TIMEOUT_SEC = 600

    # 2026-05-09: модель прибита под задачу. PromptWriter — структурный
    # агент с длинным system prompt'ом и тонкими правилами 6/6б/6.1
    # (binding тегов, никаких визуальных описаний). Opus стабильнее
    # держит дисциплину. Sonnet возможен, но сначала bench-тест.
    MODEL = "claude-opus-4-7"

    def __init__(self, claude_cli_path: str,
                 montage_card: dict,
                 refs_summary: dict,
                 characters_dict: Dict[str, str],
                 ep_id: str,
                 prompts_dir: Path,
                 geometry_context: Optional[Dict[str, str]] = None,
                 parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
        self._card = montage_card or {}
        self._refs = refs_summary or {}
        self._chars_dict = characters_dict or {}
        self._ep_id = ep_id
        self._prompts_dir = Path(prompts_dir)
        # 2026-05-06: geometry_context = {location_slug: geometry_text}
        # — описание пространства локации, передаётся PromptWriter'у
        # ТОЛЬКО для позиционирования персонажей (где относительно
        # мебели/стен/окон). НЕ для описания мебели словами.
        self._geometry: Dict[str, str] = geometry_context or {}
        self._stop = False

    def stop(self):
        self._stop = True

    # ──────────────────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D401
        try:
            self._prompts_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.all_done.emit(0, 0)
            self.block_failed.emit(0, f"prompts_dir: {e}")
            return

        blocks = list(self._card.get('blocks') or [])
        success = 0
        fail = 0
        # 2026-05-09: PW timing инструментация. Печатается в stderr
        # (видно в Console.app для собранной .app или в терминале для
        # dev-режима). Греп по `[PW_TIMING]`.
        t_pipeline_start = time.time()
        for b in blocks:
            if self._stop:
                self.aborted.emit()
                return
            n = int(b.get('n') or 0)
            if n <= 0:
                continue
            self.block_started.emit(n)
            try:
                t0 = time.time()
                txt = self._call_prompt_writer(b)
                elapsed = time.time() - t0
                self._log_pw_timing(
                    f"[PW_TIMING] block {n} PW elapsed {elapsed:.1f}s")
                txt = self._sanitize(txt, n)
                filename = f"{self._ep_id}_block_{n}.txt"
                out_path = self._prompts_dir / filename
                out_path.write_text(txt, encoding='utf-8')
                # Имя БЕЗ расширения — то что ждёт GenerateThread
                # (он сам прибавит .txt в `PROMPTS_DIR / f"{block_name}.txt"`).
                block_basename = out_path.stem
                self.block_prompt_ready.emit(n, block_basename)
                success += 1
            except Exception as e:
                fail += 1
                self.block_failed.emit(n, str(e)[:500])
                # Не прерываем — пробуем следующий блок.
                continue

        total = time.time() - t_pipeline_start
        self._log_pw_timing(
            f"[PW_TIMING] total {total:.1f}s for {success} OK / {fail} fail")
        self.all_done.emit(success, fail)

    # ──────────────────────────────────────────────────────────────────

    def _log_pw_timing(self, line: str) -> None:
        """Печатает PW-timing в stderr + дублирует в файл
        `~/.storyboard_studio_pw_timing.log`.

        2026-05-09: на Mac stderr виден в Console.app / в терминале
        (dev-режим). На Win windowed-сборка (`console=False`)
        перехватывает stderr в null — поэтому дополнительно пишем в
        home-директорию. Чтение:
            Mac/Linux:  tail -f ~/.storyboard_studio_pw_timing.log
            Windows:    Get-Content -Wait $env:USERPROFILE\\.storyboard_studio_pw_timing.log
        """
        print(line, file=sys.stderr, flush=True)
        try:
            log_path = Path.home() / ".storyboard_studio_pw_timing.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {line}\n")
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────

    def _call_prompt_writer(self, block: dict) -> str:
        # Геометрия для конкретной локации этого блока (если есть).
        loc_slug = block.get('location') or ''
        geometry_for_block = self._geometry.get(loc_slug, '') if loc_slug else ''
        user = build_storyboard_writer_user_prompt(
            block=block,
            refs_summary=self._refs,
            characters_dict=self._chars_dict,
            ep_id=self._ep_id,
            geometry=geometry_for_block,
        )
        return self._run_claude(STORYBOARD_WRITER_SYSTEM, user)

    def _run_claude(self, system_prompt: str, user_prompt: str) -> str:
        if not self._cli:
            raise RuntimeError("claude CLI not found")
        cmd = [self._cli, "-p",
               "--system-prompt", system_prompt,
               "--output-format", "text",
               "--model", self.MODEL]
        kwargs: dict = {
            'input': user_prompt,
            'capture_output': True,
            'text': True,
            'timeout': self.SUBPROCESS_TIMEOUT_SEC,
            'encoding': 'utf-8',
        }
        if sys.platform == 'win32':
            CREATE_NO_WINDOW = 0x08000000
            kwargs['creationflags'] = CREATE_NO_WINDOW
        r = subprocess.run(cmd, **kwargs)
        if r.returncode != 0:
            stderr = (r.stderr or "")[:500]
            raise RuntimeError(f"claude exit={r.returncode}: {stderr}")
        out = (r.stdout or "").strip()
        if not out:
            raise RuntimeError("empty response from PromptWriter")
        return out

    @staticmethod
    def _sanitize(raw: str, block_n: int) -> str:
        """Чистит ответ от возможных markdown-обёрток ```...``` и
        проверяет/чинит обязательную шапку тегов + теги начала/конца блока.

        Если AI забыл `===ПРОМПТ_БЛОК_N_НАЧАЛО===` — добавляем сами.
        """
        text = (raw or "").strip()
        # Срезаем тройные бэктики если AI всё-таки их добавил.
        m = re.match(r'^```(?:[a-zA-Z]+)?\s*(.*?)\s*```\s*$', text, re.DOTALL)
        if m:
            text = m.group(1).strip()

        # Обязательная шапка тегов: должна быть хотя бы одна строка
        # `# [@]img1 = ...` в начале (до тегов начала блока). Это
        # критично — без неё generate_storyboards.py / GenerateThread
        # не найдут рефы.
        first_line = text.splitlines()[0] if text else ''
        if not first_line.startswith("# [@]"):
            raise RuntimeError(
                f"PromptWriter не вернул шапку '# [@]img1 = ...' "
                f"для блока {block_n}. Ответ начинается: {first_line[:80]!r}"
            )

        # Если тегов начала/конца блока нет — добавим стандартные.
        # GenerateThread сам обрезает строки `===ПРОМПТ_БЛОК` в
        # extract_shot_prompt, так что наличие тегов не вредит.
        if "===ПРОМПТ_БЛОК" not in text:
            # Найдём конец шапки `# [@]...`, вставим открывающий тег
            lines = text.splitlines()
            header_end = 0
            for i, line in enumerate(lines):
                if line.startswith("# [@]"):
                    header_end = i + 1
                else:
                    break
            lines.insert(header_end, f"===ПРОМПТ_БЛОК_{block_n}_НАЧАЛО===")
            lines.append(f"===ПРОМПТ_БЛОК_{block_n}_КОНЕЦ===")
            text = "\n".join(lines)
        return text + ("\n" if not text.endswith("\n") else "")
