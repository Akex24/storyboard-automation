# -*- coding: utf-8 -*-
"""
threads/server_check.py — ручная проверка ЖИВОСТИ сервера генерации FastGen
(очередь #1). В отличие от key_health (лёгкий /upload без генерации) — здесь
РЕАЛЬНЫЙ боевой тест генерации одной картинки («яблоко»), как curl-тест из
handoff: POST /api/v4/flow/image/generate + poll /api/v4/operations/{id}.
Стоит ~4 кредита. Нужен когда сервер висит и непонятно — наш код или их сервер.

Поток получает ГОТОВЫЙ ключ (UI берёт key_pool.next_key() и передаёт сюда;
пустой ключ = «нет доступных» обрабатывается в UI ДО старта потока). Эмитит:
  • result(outcome, sec, op_id) — итог: 'ok' | 'down' | 'noconn'; op_id —
    идентификатор тестовой задачи (для письма в техподдержку при провале),
    пустая строка если submit не прошёл.

2026-06-10 (UX-полировка): секунды для индикации теперь ведёт UI-таймер
(на больном сервере poll-GET блокирует поток до POLL_TIMEOUT_SEC, и сигнал
секунд из потока «замирал» бы). Поток отдаёт лишь ИТОГ; реальное время до
итога считается здесь через time.monotonic (не инкремент). Потолок 300с.

STOP-флаг для graceful shutdown (closeEvent дренаж).
Cross-platform: чистый requests, без subprocess/shell.
"""
from __future__ import annotations

import time
import requests
from PyQt6.QtCore import QThread, pyqtSignal

# Тот же боевой endpoint, что и реальная генерация (storyboard_app.API_BASE).
API_BASE = "https://googler.fast-gen.ai"

# Боевой тест: простой product-shot БЕЗ рефов, вертикальный кадр 9:16.
TEST_PROMPT = "a single red apple on a plain white table, simple product shot"
TEST_ASPECT = "9:16"

SUBMIT_TIMEOUT_SEC = 60     # POST submit — обычно быстрый (операция async)
POLL_TIMEOUT_SEC   = 30     # один GET operations/{id}
POLL_EVERY_SEC     = 5      # как часто опрашивать статус операции
CEILING_SEC        = 300    # потолок ожидания генерации (5 минут)


class ServerCheckThread(QThread):
    """Боевой тест генерации одной картинки. Итог — здоровье СЕРВЕРА, не ключа."""

    # (outcome, sec, op_id); outcome ∈ ok | down | noconn; op_id "" если нет.
    result = pyqtSignal(str, int, str)

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = str(key or "")
        self._stop = False

    def stop(self):
        """Запросить остановку (graceful shutdown). Прерывает между тиками."""
        self._stop = True

    def run(self):
        t0 = time.monotonic()
        op_id = ""
        session = requests.Session()
        session.headers.update({"X-API-Key": self._key})

        # 1) Submit — POST на flow/image/generate (обычно отвечает быстро,
        #    генерация уходит в async-операцию).
        payload = {"prompt": TEST_PROMPT, "aspect_ratio": TEST_ASPECT}
        try:
            r = session.post(f"{API_BASE}/api/v4/flow/image/generate",
                             json=payload, timeout=SUBMIT_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("operation_id") or ""
            if not op_id:
                self._emit_result('down', t0, "")
                return
        except requests.exceptions.ConnectionError:
            self._emit_result('noconn', t0, "")
            return
        except Exception:
            # timeout submit, 4xx/5xx, JSON-мусор — сервер не принял задачу.
            self._emit_result('down', t0, "")
            return

        # 2) Poll — опрашиваем статус раз в POLL_EVERY_SEC, спим по секунде
        #    (быстрый отклик на stop). Потолок CEILING_SEC по monotonic.
        last_poll = time.monotonic()
        while True:
            if self._stop:
                return
            if time.monotonic() - t0 >= CEILING_SEC:
                break
            self.msleep(1000)
            if time.monotonic() - last_poll < POLL_EVERY_SEC:
                continue
            last_poll = time.monotonic()
            try:
                pr = session.get(f"{API_BASE}/api/v4/operations/{op_id}",
                                 timeout=POLL_TIMEOUT_SEC)
                pr.raise_for_status()
                status = (pr.json() or {}).get("status")
                if status == "success":
                    self._emit_result('ok', t0, op_id)
                    return
                if status == "error":
                    self._emit_result('down', t0, op_id)
                    return
                # pending / processing — продолжаем опрашивать.
            except requests.exceptions.ConnectionError:
                self._emit_result('noconn', t0, op_id)
                return
            except Exception:
                # Транзиентный таймаут/5xx одного poll — не сдаёмся, опрашиваем
                # дальше до потолка (на перегруженном сервере GET тоже виснет).
                continue

        # Потолок 300с без success — сервер висит.
        self._emit_result('down', t0, op_id)

    def _emit_result(self, outcome: str, t0: float, op_id: str = ""):
        if self._stop:
            return
        try:
            self.result.emit(str(outcome), int(time.monotonic() - t0),
                             str(op_id or ""))
        except Exception:
            pass
