"""
threads/camera_director_thread.py — фоновый Qt-поток для агента-режиссёра камер
(Mode C, фича «камеры для версий»).

Зачем: camera_director.propose_cameras делает БЛОКИРУЮЩИЙ `claude -p` subprocess
(Sonnet). Звать его синхронно в _start_storyboard_block_mode_c нельзя — спавнер
на UI-треде, UI замёрзнет. Этот тонкий QThread гоняет propose_cameras в фоне и
отдаёт результат сигналом; спавнер по нему спавнит версии с camera_override.

Образец — threads/translate.py:TranslateThread (claude -p + result-сигнал).
Cross-platform: subprocess живёт в camera_director (win32 CREATE_NO_WINDOW),
здесь только Qt-обёртка. Mac == Win.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal


class CameraDirectorThread(QThread):
    """Фоновый вызов camera_director.propose_cameras. Эмитит result_ready(dict)
    с {(panel_idx, v): camera_str}. При ЛЮБОМ сбое — result_ready({}) (фича не
    применяется, версии остаются с авторским ракурсом). Всегда ровно одна
    эмиссия result_ready → спавнер вызывается один раз."""

    result_ready = pyqtSignal(dict)

    def __init__(self, shot_contexts: List[dict], n: int,
                 cli_path: Optional[str], timeout_sec: int = 120,
                 parent=None):
        super().__init__(parent)
        self._shot_contexts = shot_contexts or []
        self._n = int(n)
        self._cli = cli_path
        self._timeout = int(timeout_sec)
        self._stop = False

    def stop(self):
        # subprocess.run прервать на полпути нельзя; флаг для совместимости с
        # graceful shutdown. Реальное ограничение времени — _timeout (120с).
        self._stop = True

    def run(self):
        cams = {}
        try:
            from agents import camera_director
            cams = camera_director.propose_cameras(
                self._shot_contexts, self._n, self._cli,
                timeout_sec=self._timeout) or {}
        except Exception:
            cams = {}  # фолбэк: версии с авторским ракурсом
        self.result_ready.emit(cams)
