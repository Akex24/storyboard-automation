"""generator/upscale_thread.py — фоновой воркер апскейла одной картинки ×2.

Сценарий:
  1. (опц.) Догрузить движок: threads.upscale_engine.ensure_engine_downloaded(...)
     — если is_engine_ready() уже True, пропускается мгновенно.
     Прогресс скачивания транслируется как progress("Скачиваю движок: NN%").
  2. Запустить CLI Real-ESRGAN ncnn-vulkan:
        <bin> -i <src> -o <out> -m <models_dir> -n ultramix-balanced-4x \\
              -s 2 -f jpg
     На stderr ncnn пишет проценты вида "12.50%". Парсим → progress("12%").
  3. По returncode==0 → finished(<out_path>); иначе → failed(<stderr-tail>).

Контракт сигналов (зеркало GeneratorImageThread/GeneratorVideoThread):
  progress = pyqtSignal(str)   # человекочитаемый текст для loading-плитки
  finished = pyqtSignal(str)   # абсолютный путь к итоговой картинке
  failed   = pyqtSignal(str)   # сообщение ошибки (короткая причина)

Cross-platform: subprocess.Popen + Path + **_no_console_kwargs() (CREATE_NO_WINDOW
на Win — без него на Win моргает чёрное окно). Без shell=True.
"""

from __future__ import annotations

import re
import subprocess
import sys
import traceback
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from threads.upscale_engine import (
    ensure_engine_downloaded,
    get_upscayl_paths,
    is_engine_ready,
)
from i18n import tr   # локализация UI (i18n — лист-модуль, без circular import)


def _no_console_kwargs() -> dict:
    """CREATE_NO_WINDOW для subprocess на Win. Зеркало
    threads/auth_switch._no_console_kwargs. Без него на Win мигает cmd-окно."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ncnn-vulkan пишет в stderr что-то вроде "12.50%\n" или "...12.50%". Берём
# первое число с процентом в строке.
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class UpscaleThread(QThread):
    """Фоновой воркер апскейла одной картинки ×2 через локальный Real-ESRGAN."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, src_path: Path, out_path: Path,
                 model_name: str = "ultramix-balanced-4x",
                 scale: int = 2, fmt: str = "jpg", parent=None):
        super().__init__(parent)
        self._src = Path(src_path)
        self._out = Path(out_path)
        self._model_name = model_name
        self._scale = int(scale)
        self._fmt = fmt
        self._proc: subprocess.Popen | None = None

    # ── helpers ─────────────────────────────────────────────────────────────

    def _emit_engine_progress(self, done: int, total: int, phase: str) -> None:
        """Callback от ensure_engine_downloaded — транслируем как
        «Скачиваю движок: NN%». Для phase='binary' и 'model_bin'/'model_param'
        показываем общий процент по текущему файлу (total известен из
        Content-Length, иначе — байты)."""
        try:
            if total > 0:
                pct = int(min(100, done * 100 // total))
                label = {
                    "binary": tr('gen_up_label_engine'),
                    "model_bin": tr('gen_up_label_model'),
                    "model_param": tr('gen_up_label_params'),
                }.get(phase, phase)
                self.progress.emit(tr('gen_up_dl', label=label, pct=pct))
            else:
                kb = done // 1024
                self.progress.emit(tr('gen_up_dl_kb', kb=kb))
        except Exception:
            pass

    def _emit_engine_log(self, msg: str) -> None:
        # Логи модуля — не пихаем юзеру (это технические сообщения).
        # Для отладки можно раскомментировать.
        pass

    # ── run ─────────────────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: C901 — линейный поток событий
        try:
            self.progress.emit(tr('gen_loading_prep'))
            # 1. Движок: проверка/догрузка. Если ready — сразу же.
            if not is_engine_ready():
                ok = ensure_engine_downloaded(
                    progress_cb=self._emit_engine_progress,
                    on_log=self._emit_engine_log,
                )
                if not ok:
                    self.failed.emit(tr('gen_up_engine_fail'))
                    return
            paths = get_upscayl_paths()
            bin_path = paths["bin_path"]
            models_dir = paths["models_dir"]
            if not bin_path.is_file():
                self.failed.emit(tr('gen_up_binary_missing'))
                return
            if not models_dir.is_dir():
                self.failed.emit(tr('gen_up_models_missing'))
                return
            if not self._src.is_file():
                self.failed.emit(tr('gen_up_src_missing'))
                return

            # 2. CLI Real-ESRGAN. Параметры из ТЗ Alex:
            #    -i input -o output -m models_dir -n ultramix-balanced-4x -s 2 -f jpg
            self._out.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                str(bin_path),
                "-i", str(self._src),
                "-o", str(self._out),
                "-m", str(models_dir),
                "-n", self._model_name,
                "-s", str(self._scale),
                "-f", self._fmt,
            ]
            self.progress.emit(tr('gen_up_enhancing', pct=0))
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **_no_console_kwargs(),
                )
            except Exception as e:
                self.failed.emit(tr('gen_up_launch_fail', detail=f'{type(e).__name__}: {e}'))
                return

            # 3. Парсим stderr построчно: ncnn пишет проценты. Stdout обычно пустой.
            stderr_tail: list[str] = []
            assert self._proc.stderr is not None
            for raw in iter(self._proc.stderr.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                if not line:
                    continue
                stderr_tail.append(line)
                if len(stderr_tail) > 40:
                    stderr_tail.pop(0)
                m = _PERCENT_RE.search(line)
                if m:
                    try:
                        pct = int(float(m.group(1)))
                        self.progress.emit(tr('gen_up_enhancing', pct=pct))
                    except Exception:
                        pass

            rc = self._proc.wait()
            if rc != 0:
                tail = " | ".join(stderr_tail[-3:]) if stderr_tail else "—"
                self.failed.emit(tr('gen_up_code_fail', rc=rc, tail=tail)[:240])
                return
            if not self._out.is_file() or self._out.stat().st_size < 1024:
                tail = " | ".join(stderr_tail[-3:]) if stderr_tail else "—"
                self.failed.emit(tr('gen_up_no_file', tail=tail)[:240])
                return
            self.progress.emit(tr('gen_up_done'))
            self.finished.emit(str(self._out))
        except Exception as e:
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            # Закрыть pipes — без этого file descriptors могут утечь.
            try:
                if self._proc is not None:
                    if self._proc.stderr is not None:
                        self._proc.stderr.close()
                    if self._proc.stdout is not None:
                        self._proc.stdout.close()
            except Exception:
                pass
