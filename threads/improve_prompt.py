"""
threads/improve_prompt.py — фоновый Qt-поток для кнопки «✨ Улучшить» в окне
AI-edit шота.

Юзер пишет короткую правку по-русски простыми словами → Sonnet 4.6 (зрячий,
через Read-инструмент headless `claude -p`) смотрит на картинку текущей версии
шота и переписывает инструкцию в короткий командный английский промпт для
Nano Banana. Картинка может быть РАЗМЕЧЕНА красным маркером (тот же
`_bake_marked_image`, что и Шаг C edit-флоу) — тогда Sonnet целится в
обведённый объект.

Образец — threads/generate.py:ClaudeGeometryThread (тот же зрячий Read-канал,
подписка Claude Max, без Anthropic SDK) + agents/camera_director._call_sonnet
(один subprocess.run Sonnet с --system-prompt). Cross-platform: на win32 —
CREATE_NO_WINDOW, как в ClaudeGeometryThread/camera_director. subprocess.run
прервать нельзя — caller защищается guard'ом времени жизни модалки.
"""
from __future__ import annotations

import re
import sys
import subprocess
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal


_MODEL_IMPROVE = "claude-sonnet-4-6"


_NB_IMPROVE_SYSTEM = """You rewrite a user's short, casual edit request (usually in Russian) into a precise English instruction prompt for an image-EDIT model (Nano Banana / Gemini image edit).

You are given the CURRENT shot image — open it with the Read tool and look at the real composition (what objects are present, where they are, which way they face) so your prompt matches what is actually in the frame.

The image MAY contain red marker strokes / an outline drawn by the user to point at WHICH object to change. If you see red marks: target the outlined object. NEVER describe the red marks in your output, and NEVER ask the model to draw, keep or remove red marks — they are only a pointer.

Output rules:
- Output ONLY the final English edit prompt. No preamble, no explanation, no markdown, no surrounding quotes.
- Short and imperative, in the model's command style (e.g. "Rotate the two black SUVs 180 degrees so they face away from the camera, tail-lights toward the viewer").
- Always finish the change with a preservation clause: keep everything else unchanged — same composition, framing, characters, lighting and background.
- Preserve the art style of the current image exactly: do NOT convert between pencil sketch and photo, do NOT add or remove color, do NOT make it black-and-white.
- Keep the same vertical 9:16 framing.
- Refer to objects by what you actually SEE in the image, not by the red marks."""


def _strip_fence(s: str) -> str:
    """Убирает ```...``` обёртку и обрамляющие кавычки, если Sonnet их добавил."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r'^```[a-zA-Z]*\n', '', s)
        s = re.sub(r'\n```$', '', s)
        s = s.strip()
    if len(s) >= 2 and s[0] in '"«' and s[-1] in '"»':
        s = s[1:-1].strip()
    return s


class ImprovePromptThread(QThread):
    """Один зрячий вызов Sonnet 4.6 (`claude -p`, Read картинки шота). Эмитит
    result_ready(str) с готовым английским промптом ЛИБО error(str). Ровно одна
    эмиссия. Картинка (marked-or-clean) выбирается caller'ом — поток лишь
    показывает её путь Sonnet через Read."""

    result_ready = pyqtSignal(str)
    error        = pyqtSignal(str)

    def __init__(self, user_text: str, image_path: Path, project_root: Path,
                 cli_path: Optional[str], timeout_sec: int = 120, parent=None):
        super().__init__(parent)
        self._text    = (user_text or "").strip()
        self._image   = Path(image_path)
        self._root    = Path(project_root)
        self._cli     = cli_path
        self._timeout = int(timeout_sec)

    def run(self):
        if not self._cli:
            self.error.emit("claude_cli_not_found")
            return
        if not self._text:
            self.error.emit("empty_text")
            return
        user_prompt = (
            f"User's edit request (rewrite this): {self._text}\n\n"
            "Read the current shot image with the Read tool at this absolute "
            f"path:\n{self._image}\n\n"
            "Then output ONLY the rewritten English edit prompt."
        )
        run_kwargs = dict(
            cwd=str(self._root),
            capture_output=True,
            text=True,
            encoding="utf-8",      # Win-fix: иначе cp1252 → crash на не-ASCII
            errors="replace",
            timeout=self._timeout,
        )
        if sys.platform == "win32":
            run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [self._cli, "-p", user_prompt,
                 "--system-prompt", _NB_IMPROVE_SYSTEM,
                 "--dangerously-skip-permissions",
                 "--model", _MODEL_IMPROVE],
                **run_kwargs,
            )
        except subprocess.TimeoutExpired:
            self.error.emit("timeout")
            return
        except Exception as e:
            self.error.emit(str(e))
            return
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "exit != 0").strip()[:500]
            self.error.emit(msg)
            return
        out = _strip_fence(proc.stdout or "")
        if not out:
            self.error.emit("empty_output")
            return
        self.result_ready.emit(out)
