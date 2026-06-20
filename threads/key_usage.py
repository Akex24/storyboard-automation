# -*- coding: utf-8 -*-
"""
threads/key_usage.py — фоновый запрос usage-статистики API-ключей FastGen
(Settings, строка под полем ключа). На «Сохранить и проверить» по КАЖДОМУ
непустому ключу (ВКЛЮЧАЯ выключенные чекбоксом «использовать» — статистика
полезна и для них) дёргаем:

    GET {API_BASE}/api/v5/usage  с заголовком X-API-Key.

Собираем ОДНУ строку: лимиты/использование image+video, активные потоки,
сброс часового окна (window_start+3600-now), срок действия ключа (exp).
Ключи опрашиваются ПОСЛЕДОВАТЕЛЬНО (≤5, лёгкий GET) — ОДИН поток, per-key emit
(UI обновляет лейбл сразу, без гонок). КЛЮЧ в строку/лог НЕ пишем (поле api_key
в теле ответа игнорируем). STOP-флаг для graceful shutdown.
Cross-platform: чистый requests, без subprocess/shell.
"""
from __future__ import annotations

import time
from datetime import datetime
import requests
from PyQt6.QtCore import QThread, pyqtSignal

# Тот же боевой хост, что и генерация (storyboard_app.API_BASE).
API_BASE = "https://googler.fast-gen.ai"
USAGE_TIMEOUT_SEC = 15


class KeyUsageThread(QThread):
    """GET /api/v5/usage по списку ключей. Per-key emit готовой строки, в конце done."""

    # (field_idx, usage_str); usage_str — строка лимитов/потоков/reset/exp ИЛИ
    # короткая ошибка ('usage: HTTP 4xx' / 'usage: timeout' и т.п.).
    key_usage = pyqtSignal(int, str)
    done = pyqtSignal()

    def __init__(self, pairs, parent=None):
        """pairs: список (field_idx:int, key_str:str) — НЕПУСТЫЕ."""
        super().__init__(parent)
        self._pairs = list(pairs or [])
        self._stop = False

    def stop(self):
        """Запросить остановку (graceful shutdown). Прерывает между ключами."""
        self._stop = True

    def run(self):
        for field_idx, key_str in self._pairs:
            if self._stop:
                break
            line = self._fetch(key_str)
            try:
                self.key_usage.emit(int(field_idx), str(line))
            except Exception:
                pass
        try:
            self.done.emit()
        except Exception:
            pass

    def _fetch(self, key_str) -> str:
        """Один GET /api/v5/usage → строка. Никогда не кидает наружу; на любой
        ошибке/не-200 возвращает короткий 'usage: …'. Ключ в строку НЕ попадает."""
        try:
            r = requests.get(
                f"{API_BASE}/api/v5/usage",
                headers={"X-API-Key": str(key_str or "")},
                timeout=USAGE_TIMEOUT_SEC)
            if r.status_code != 200:
                return "usage: HTTP %d" % r.status_code
            data = r.json() or {}
            return self._format(data)
        except requests.exceptions.Timeout:
            return "usage: timeout"
        except requests.exceptions.ConnectionError:
            return "usage: нет связи"
        except Exception:
            return "usage: error"

    @staticmethod
    def _format(data: dict) -> str:
        """Собрать строку по ПОДТВЕРЖДЁННОЙ форме /api/v5/usage. Защитно:
        image_generation/video_generation МОГУТ быть null (нет генераций в окне)
        → используется 0; если у обоих нет window_start → reset='—'."""
        try:
            lim = data.get("account_limits") or {}
            cur = data.get("current_usage") or {}
            hourly = cur.get("hourly_usage") or {}
            threads = cur.get("active_threads") or {}
            img = hourly.get("image_generation") or {}
            vid = hourly.get("video_generation") or {}

            img_used = img.get("current_usage") or 0
            vid_used = vid.get("current_usage") or 0
            img_lim = lim.get("img_gen_per_hour_limit") or 0
            vid_lim = lim.get("video_gen_per_hour_limit") or 0
            it = threads.get("image_threads") or 0
            it_lim = lim.get("img_generation_threads_allowed") or 0
            vt = threads.get("video_threads") or 0
            vt_lim = lim.get("video_generation_threads_allowed") or 0

            ws = img.get("window_start") or vid.get("window_start")
            if ws:
                rem = int((float(ws) + 3600 - time.time()) / 60)
                reset = "%dm" % max(0, rem)
            else:
                reset = "—"

            exp_ts = data.get("expiration_date")
            try:
                exp = datetime.fromtimestamp(float(exp_ts)).strftime("%d.%m") if exp_ts else "—"
            except Exception:
                exp = "—"

            return ("IMG %s/%s · VID %s/%s · threads I %s/%s V %s/%s · reset %s · exp %s"
                    % (img_used, img_lim, vid_used, vid_lim,
                       it, it_lim, vt, vt_lim, reset, exp))
        except Exception:
            return "usage: parse error"
