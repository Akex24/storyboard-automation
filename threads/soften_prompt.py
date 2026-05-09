# -*- coding: utf-8 -*-
"""
threads/soften_prompt.py — фоновый AI-помощник для смягчения отклонённого
промпта. Использует тот же `claude -p` CLI что и SuggestOutfitsThread.

Когда OpenAI Image API отклоняет описание референса по content-moderation,
ActorsView показывает PromptRetryDialog. Этот диалог запускает
SoftenPromptThread, который через локальный AI просит переписать
проблемный промпт в 3 альтернативных «мягких» вариантах.

В отличие от хардкоднутого словаря замен, AI может смягчать ЛЮБЫЕ
триггеры — даже неочевидные (например смесь слов в контексте), которые
мы не предвидим.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal


class _AppProxy:
    """Прокси к module storyboard_app — приоритет __main__."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


def build_soften_prompt(rejected_text: str, api_error: str = "") -> str:
    """Системный промпт для AI: «перепиши этот текст в 3 мягких варианта»."""
    err_block = ""
    if api_error.strip():
        err_block = (
            f"\nТекст ошибки от OpenAI (для понимания контекста):\n"
            f"«{api_error.strip()[:300]}»\n")
    return (
        "Ты помогаешь обойти content-moderation OpenAI image-gen API.\n\n"
        f"Юзер написал такое описание одежды персонажа:\n"
        f"«{rejected_text.strip()}»\n"
        f"{err_block}\n"
        "OpenAI это описание ОТКЛОНИЛ (модерация). Тебе нужно переписать "
        "описание в 3 разных МЯГКИХ вариантах. Цель — сохранить общую "
        "идею одежды, но заменить триггерные слова на нейтральные.\n\n"
        "СОДЕРЖАНИЕ ВАРИАНТА — ТОЛЬКО ОДЕЖДА:\n"
        "  • верх (футболка/рубашка/свитер/толстовка/майка/без рубашки/...)\n"
        "  • низ (джинсы/брюки/шорты/спортивные шорты/шорты-боксеры/...)\n"
        "  • обувь (ботинки/кроссовки/туфли/босиком/...)\n"
        "  • цвет, материал — желательно (синие джинсы, белая льняная рубашка)\n\n"
        "🔴 СТРОГО ЗАПРЕЩЕНО упоминать:\n"
        "  • позу (сидит, стоит, лежит, прислонился, держит руки и т.п.)\n"
        "  • действие (что персонаж делает)\n"
        "  • контекст сцены (на стуле, у окна, в спальне, на кровати, дома)\n"
        "  • интерьер, фон, локацию\n"
        "  • эмоции, состояние, взгляд, настроение\n"
        "  • прическу, лицо, бороду, тело\n"
        "  • освещение, ракурс, время суток\n\n"
        "Примеры замен триггерных слов (по смыслу — список не исчерпывающий):\n"
        "  • трусы / underwear → шорты-боксеры / спортивные шорты / lounge shorts\n"
        "  • халат / лифчик → лёгкая блузка / топ\n"
        "  • голое / обнажённое тело → обнажённый торс / без рубашки\n"
        "  • плавки → пляжные шорты\n"
        "  • intimate → casual\n\n"
        "ПРАВИЛА ВАРИАНТОВ:\n"
        "• 3 варианта должны различаться между собой (разные слова, "
        "разные стили — спортивный / бытовой / пляжный).\n"
        "• Каждый вариант — ТОЛЬКО ОДЕЖДА. Короткая фраза (10-20 слов), "
        "на ТОМ ЖЕ языке что и оригинал.\n"
        "• Каждый вариант ОБЯЗАТЕЛЬНО содержит верх + низ + обувь.\n"
        "• НЕ добавляй слов которые сами могут стать триггерами.\n\n"
        "ФОРМАТ ОТВЕТА (СТРОГО): ровно 3 варианта, разделитель `|||`, "
        "без нумерации, без markdown, без кавычек, без пояснений. "
        "Никаких других строк в ответе.\n\n"
        "Пример хорошего ответа:\n"
        "тёмно-синяя футболка, спортивные шорты, белые кроссовки ||| "
        "белая льняная рубашка с расстёгнутым воротом, бежевые шорты, "
        "коричневые сандалии ||| серая хлопковая майка, чёрные шорты-"
        "боксеры, босиком\n\n"
        "Теперь дай ответ."
    )


def parse_soften_response(raw: str) -> List[str]:
    """Парсит ответ AI в список вариантов."""
    if not raw:
        return []
    text = raw.strip()
    # Снять возможную ``` обёртку
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if "|||" in text:
        parts = [p.strip() for p in text.split("|||")]
    else:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
    cleaned: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if (p.startswith('«') and p.endswith('»')) or \
           (p.startswith('"') and p.endswith('"')):
            p = p[1:-1].strip()
        for pref in ("1.", "2.", "3.", "1)", "2)", "3)", "-", "•", "*"):
            if p.startswith(pref):
                p = p[len(pref):].lstrip(" \t")
                break
        if p:
            cleaned.append(p)
    return cleaned[:3]


class SoftenPromptThread(QThread):
    """Headless `claude -p` для смягчения отклонённого промпта.

    Сигналы:
      • results(list) — 1-3 строки с альтернативами.
      • error(str) — короткое сообщение об ошибке.
      • stopped() — после явной остановки.
    """
    results = pyqtSignal(list)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, project_root: Path, rejected_text: str,
                 api_error: str = "",
                 model: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.rejected_text = rejected_text
        self.api_error = api_error
        self.model = model
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self):
        cli = _sa.find_claude_cli()
        if not cli:
            self.error.emit("claude_cli_not_found")
            return
        try:
            prompt = build_soften_prompt(
                self.rejected_text, self.api_error)
            args = [cli]
            if self.model:
                args += ["--model", self.model]
            args += ["-p", prompt, "--dangerously-skip-permissions"]
            self._proc = subprocess.Popen(
                args, cwd=str(self.project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                bufsize=1)
            assert self._proc.stdout is not None
            buf = self._proc.stdout.read()
            rc = self._proc.wait(timeout=10)
            if self._stop_requested:
                self.stopped.emit()
                return
            variants = parse_soften_response(buf or "")
            if not variants:
                self.error.emit(
                    f"empty (rc={rc}): {(buf or '').strip()[:200]}")
                return
            self.results.emit(variants)
        except Exception as e:
            if self._stop_requested:
                self.stopped.emit()
                return
            self.error.emit(str(e)[:500])
