# -*- coding: utf-8 -*-
"""
threads/seedance_pipeline.py — pipeline записи .txt промптов Seedance
из утверждённой монтажной карты.

Запускается ПАРАЛЛЕЛЬНО с `StoryboardPipelineThread` после утверждения
карты. Пока Fast Gen занят генерацией шотов через NARWHAL, Opus
свободен — пишет Seedance промпты в фоне.

Поток:
  Для каждого блока карты:
    1. claude -p Seedance Writer (system: SEEDANCE_WRITER_SYSTEM)
       → текст билингвального промпта (русская сводка + китайский body)
    2. Запись в `output/seedance/<ep>_block_N.txt`
    3. Эмит сигнал `block_seedance_ready(block_n, basename)`
       — UI ловит и обновляет состояние кнопки на блоке.

Studio НЕ отправляет этот промпт никуда — юзер открывает попап на
блоке, копирует текст и идёт в внешний Seedance.

История: создано 2026-05-06 (фича Этап 3 — Seedance промпты).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from agents.seedance_prompts import (
    SYSTEM as SEEDANCE_WRITER_SYSTEM,
    build_user_prompt as build_seedance_user_prompt,
)


class SeedancePipelineThread(QThread):
    """Фон-поток — итерируется по блокам карты, для каждого вызывает
    Seedance Writer (claude -p) и пишет результат в `output/seedance/`.

    На вход:
      • claude_cli_path  — путь к исполняемому файлу `claude`
      • montage_card     — JSON-карта от Сценариста (с blocks[])
      • refs_summary     — словарь {locations:[], objects:[], characters:[]}
      • characters_dict  — {slug: english_name} — для подстановки имён
      • ep_id            — например "ep1" — используется в имени файла
                           `<ep_id>_block_N.txt`
      • seedance_dir     — Path где хранить .txt
                           (`shows/<show>/output/seedance/`)
      • bible_text       — содержимое `shows/<slug>/bible.txt`
      • voice_profiles_text — содержимое
                           `instructions/ГОЛОСОВЫЕ_ПРОФИЛИ_ПЕРСОНАЖЕЙ.txt`
      • storyboard_prompts_dir — Path с уже-готовыми сториборд промптами
                           (`shows/<slug>/output/prompts/`) — читаем
                           `<ep>_block_N.txt` для каждого блока, передаём
                           Opus как смысловой контекст ракурсов/мимики
      • model            — id модели CLI (`claude-opus-4-7` и т.п.).
                           Если None — берётся дефолт CLI.

    Сигналы (для UI):
      • block_started(block_n)
      • block_seedance_ready(block_n, basename)  — basename без `.txt`
      • block_failed(block_n, msg)
      • all_done(success_count, fail_count)
      • aborted()
    """

    block_started = pyqtSignal(int)
    block_seedance_ready = pyqtSignal(int, str)
    block_failed = pyqtSignal(int, str)
    all_done = pyqtSignal(int, int)
    aborted = pyqtSignal()

    SUBPROCESS_TIMEOUT_SEC = 600

    def __init__(self, claude_cli_path: str,
                 montage_card: dict,
                 refs_summary: dict,
                 characters_dict: Dict[str, str],
                 ep_id: str,
                 seedance_dir: Path,
                 bible_text: str = "",
                 voice_profiles_text: str = "",
                 storyboard_prompts_dir: Optional[Path] = None,
                 model: Optional[str] = None,
                 parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
        self._card = montage_card or {}
        self._refs = refs_summary or {}
        self._chars_dict = characters_dict or {}
        self._ep_id = ep_id
        self._seedance_dir = Path(seedance_dir)
        self._bible = bible_text or ""
        self._voices = voice_profiles_text or ""
        self._sb_prompts_dir = (
            Path(storyboard_prompts_dir) if storyboard_prompts_dir else None
        )
        self._model = model
        self._stop = False

    def stop(self):
        self._stop = True

    # ──────────────────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D401
        try:
            self._seedance_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.all_done.emit(0, 0)
            self.block_failed.emit(0, f"seedance_dir: {e}")
            return

        blocks = list(self._card.get('blocks') or [])
        success = 0
        fail = 0
        for b in blocks:
            if self._stop:
                self.aborted.emit()
                return
            n = int(b.get('n') or 0)
            if n <= 0:
                continue
            self.block_started.emit(n)
            try:
                txt = self._call_seedance_writer(b)
                txt = self._sanitize(txt)
                filename = f"{self._ep_id}_block_{n}.txt"
                out_path = self._seedance_dir / filename
                out_path.write_text(txt, encoding='utf-8')
                block_basename = out_path.stem
                self.block_seedance_ready.emit(n, block_basename)
                success += 1
            except Exception as e:
                fail += 1
                self.block_failed.emit(n, str(e)[:500])
                # Не прерываем — пробуем следующий блок.
                continue

        self.all_done.emit(success, fail)

    # ──────────────────────────────────────────────────────────────────

    def _call_seedance_writer(self, block: dict) -> str:
        # Подгружаем сториборд-промпт этого блока для смыслового контекста.
        sb_text = ""
        if self._sb_prompts_dir is not None:
            n = int(block.get('n') or 0)
            sb_path = self._sb_prompts_dir / f"{self._ep_id}_block_{n}.txt"
            try:
                if sb_path.exists():
                    sb_text = sb_path.read_text(encoding='utf-8')
            except Exception:
                # Не критично — без него Opus справится по карте + Bible.
                sb_text = ""

        user = build_seedance_user_prompt(
            block=block,
            refs_summary=self._refs,
            characters_dict=self._chars_dict,
            ep_id=self._ep_id,
            bible=self._bible,
            voice_profiles=self._voices,
            storyboard_prompt_text=sb_text,
        )
        return self._run_claude(SEEDANCE_WRITER_SYSTEM, user)

    def _run_claude(self, system_prompt: str, user_prompt: str) -> str:
        if not self._cli:
            raise RuntimeError("claude CLI not found")
        cmd = [self._cli, "-p",
               "--system-prompt", system_prompt,
               "--output-format", "text"]
        if self._model:
            cmd.extend(["--model", self._model])
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
            raise RuntimeError("empty response from Seedance Writer")
        return out

    @staticmethod
    def _sanitize(raw: str) -> str:
        """Проверяет что в ответе есть и сводка рефов сверху, и
        ```...``` блок с китайским промптом. Чистит от лишних
        обёрток если AI завернул весь ответ в markdown.
        """
        text = (raw or "").strip()
        # Если AI обернул ВЕСЬ ответ в ```markdown ... ``` — снимаем.
        # Но НЕ трогаем внутренний ```...``` блок с китайским промптом.
        m = re.match(r'^```(?:markdown|md)?\s*\n(.*)\n```\s*$', text, re.DOTALL)
        if m:
            inner = m.group(1).strip()
            # Снимаем только если внутри есть свой ```...``` блок
            # (т.е. AI завернул и сводку, и promptcode в один большой код).
            if '```' in inner:
                text = inner
        return text


# ─────────────────────────────────────────────────────────────────────────
# REGEN: одиночная перегенерация Seedance промпта для ОДНОГО блока.
# 2026-05-07: добавлено для попапа «🎬 Промпт Seedance» — кнопка
# «↻ Перегенерировать» с опциональной textarea «что переделать».
# Используется ВНЕ массового pipeline — юзер открывает попап, пишет
# фидбэк, нажимает кнопку → этот thread переписывает один файл.
# ─────────────────────────────────────────────────────────────────────────
class SeedanceRegenThread(QThread):
    """Перегенерация Seedance промпта одного блока с опциональным
    user-инструкцией от автора.

    На вход — те же контекстные данные что у `SeedancePipelineThread`,
    плюс:
      • block_n           — номер блока (1-based) который нужно
                            перегенерировать
      • previous_prompt   — старый текст промпта блока (читается из
                            файла перед запуском)
      • user_instruction  — фидбэк автора что изменить (может быть пуст
                            для альтернативной вариации)

    Сигналы:
      • done(text)        — успешная перегенерация, текст уже записан
                            в файл, передаётся как новое содержимое
                            попапа
      • failed(msg)       — ошибка
    """

    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    SUBPROCESS_TIMEOUT_SEC = 600

    def __init__(self, claude_cli_path: str,
                 montage_card: dict,
                 refs_summary: dict,
                 characters_dict: Dict[str, str],
                 ep_id: str,
                 block_n: int,
                 seedance_dir: Path,
                 previous_prompt: str,
                 user_instruction: str = "",
                 bible_text: str = "",
                 voice_profiles_text: str = "",
                 storyboard_prompts_dir: Optional[Path] = None,
                 model: Optional[str] = None,
                 parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
        self._card = montage_card or {}
        self._refs = refs_summary or {}
        self._chars_dict = characters_dict or {}
        self._ep_id = ep_id
        self._block_n = int(block_n)
        self._seedance_dir = Path(seedance_dir)
        self._previous_prompt = previous_prompt or ""
        self._user_instruction = user_instruction or ""
        self._bible = bible_text or ""
        self._voices = voice_profiles_text or ""
        self._sb_prompts_dir = (
            Path(storyboard_prompts_dir) if storyboard_prompts_dir else None
        )
        self._model = model

    def run(self) -> None:
        try:
            blocks = list(self._card.get('blocks') or [])
            target = next(
                (b for b in blocks if int(b.get('n') or 0) == self._block_n),
                None,
            )
            if target is None:
                self.failed.emit(
                    f"block {self._block_n} not found in montage card")
                return

            sb_text = ""
            if self._sb_prompts_dir is not None:
                sb_path = (self._sb_prompts_dir
                           / f"{self._ep_id}_block_{self._block_n}.txt")
                try:
                    if sb_path.exists():
                        sb_text = sb_path.read_text(encoding='utf-8')
                except Exception:
                    sb_text = ""

            user = build_seedance_user_prompt(
                block=target,
                refs_summary=self._refs,
                characters_dict=self._chars_dict,
                ep_id=self._ep_id,
                bible=self._bible,
                voice_profiles=self._voices,
                storyboard_prompt_text=sb_text,
                previous_prompt=self._previous_prompt,
                user_instruction=self._user_instruction,
            )

            txt = self._run_claude_regen(SEEDANCE_WRITER_SYSTEM, user)
            txt = SeedancePipelineThread._sanitize(txt)

            self._seedance_dir.mkdir(parents=True, exist_ok=True)
            out_path = (self._seedance_dir
                        / f"{self._ep_id}_block_{self._block_n}.txt")
            out_path.write_text(txt, encoding='utf-8')

            self.done.emit(txt)
        except Exception as e:
            self.failed.emit(str(e)[:500])

    def _run_claude_regen(self, system_prompt: str, user_prompt: str) -> str:
        """Дубликат `_run_claude` из `SeedancePipelineThread` — выделен
        чтобы не зависеть от instance pipeline-треда. Логика идентична."""
        if not self._cli:
            raise RuntimeError("claude CLI not found")
        cmd = [self._cli, "-p",
               "--system-prompt", system_prompt,
               "--output-format", "text"]
        if self._model:
            cmd.extend(["--model", self._model])
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
            raise RuntimeError("empty response from Seedance Writer")
        return out
