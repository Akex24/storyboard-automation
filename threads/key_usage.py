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

# Цвета usage-строки (тёмная тема): базовый приглушённо-светлый + пороги
# подсветки. ВАЖНО: красный — СТРОГО при исчерпании (used >= limit, 100%+),
# жёлтый — 75%..<100%. Порог по ПРОЦЕНТУ (лимиты разные), кроме крайнего 100%.
USAGE_BASE_COLOR = "#b0b0c0"   # < 75% (или limit=0) — базовый
USAGE_WARN_COLOR = "#e0b020"   # >= 75% и < 100% — жёлтый
USAGE_CRIT_COLOR = "#ff4040"   # used >= limit (100% и выше) — красный


def _seg_color(used, limit) -> str:
    """Цвет куска x/y (IMG/VID/потоки) по used/limit. limit<=0 → базовый (БЕЗ
    деления на 0; напр. VID 0/0 не красим). used >= limit (исчерпан, 100%+) →
    красный; иначе >=75% → жёлтый; иначе базовый."""
    try:
        if not limit or float(limit) <= 0:
            return USAGE_BASE_COLOR
        u = float(used)
        l = float(limit)
        if u >= l:                       # 100% и выше — лимит исчерпан
            return USAGE_CRIT_COLOR
        if (u / l) * 100.0 >= 75:        # 75%..<100%
            return USAGE_WARN_COLOR
        return USAGE_BASE_COLOR
    except Exception:
        return USAGE_BASE_COLOR


def _exp_color(exp_ts) -> str:
    """Цвет exp по остатку дней до истечения: <=1 день → красный, <=5 дней →
    жёлтый, иначе базовый. Нет/битый ts → базовый. (Уже истёк → days<=1 → красный.)"""
    try:
        if not exp_ts:
            return USAGE_BASE_COLOR
        days = (float(exp_ts) - time.time()) / 86400.0
        if days <= 1:
            return USAGE_CRIT_COLOR
        if days <= 5:
            return USAGE_WARN_COLOR
        return USAGE_BASE_COLOR
    except Exception:
        return USAGE_BASE_COLOR


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
                f"{API_BASE}/api/v6/usage",
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
        """Собрать HTML rich-text строку по ПОДТВЕРЖДЁННОЙ форме /api/v5/usage.
        Базовый цвет — outer span (USAGE_BASE_COLOR, перекрывает stylesheet лейбла).
        КРАШЕНЫЕ куски: IMG, VID и ПОТОКИ (I x/y, V x/y) — по _seg_color
        (>=75% жёлтый, used>=limit красный, limit=0 базовый); EXP — по _exp_color
        (<=5 дней жёлтый, <=1 день красный). RESET и слово 'threads' — базовый.
        Защитно: image/video_generation МОГУТ быть null → 0; нет window_start → reset='—'."""
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

            img_c = _seg_color(img_used, img_lim)
            vid_c = _seg_color(vid_used, vid_lim)
            it_c = _seg_color(it, it_lim)        # поток image: I x/y
            vt_c = _seg_color(vt, vt_lim)        # поток video: V x/y
            exp_c = _exp_color(exp_ts)
            # Крашеные span'ы: IMG · VID · I(threads) · V(threads) · exp.
            # Базовый: слово 'threads', разделители '·', 'reset {reset}'.
            # Всё в outer span базового цвета → QLabel рендерит rich-text,
            # inline-цвет перекрывает color из stylesheet лейбла.
            return (
                '<span style="color:%s">'
                '<span style="color:%s">IMG %s/%s</span> · '
                '<span style="color:%s">VID %s/%s</span> · '
                'threads <span style="color:%s">I %s/%s</span> '
                '<span style="color:%s">V %s/%s</span> · '
                'reset %s · '
                '<span style="color:%s">exp %s</span></span>'
                % (USAGE_BASE_COLOR,
                   img_c, img_used, img_lim,
                   vid_c, vid_used, vid_lim,
                   it_c, it, it_lim,
                   vt_c, vt, vt_lim,
                   reset,
                   exp_c, exp))
        except Exception:
            return "usage: parse error"
