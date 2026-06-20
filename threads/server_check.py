# -*- coding: utf-8 -*-
"""
threads/server_check.py — ручная проверка ЖИВОСТИ сервера генерации FastGen
(кнопка «Проверить сервер»). РЕАЛЬНЫЙ боевой тест генерации: прогоняет ДВА
теста ПОСЛЕДОВАТЕЛЬНО — model="nano-banana-2" (narwhal), затем "openai-image" —
каждый со СВОИМ замером времени и СВОИМ потолком CEILING_SEC (счётчик времени
сбрасывается между тестами; первый НЕ съедает время второго).

v5-контракт (как у мигрированных путей): submit
POST /api/v5/generations?result_format=ref (payload +model, БЕЗ рефов) →
op_id = data["id"] → poll GET /api/v5/generations/{id}?result_format=ref
(статусы-множества готово/ошибка). Стоит ~5 кредитов (banana ~4 + openai ~1).

Поток получает ГОТОВЫЙ ключ (UI берёт key_pool.next_key(); пустой = «нет
доступных» обрабатывается в UI ДО старта). Один ключ работает на ОБОИХ
провайдерах (провайдер выбирается полем model). Эмитит ОДИН раз в конце:
  • results(list) — список из 2 dict (по тесту):
      {"provider": str, "outcome": 'ok'|'down'|'noconn', "sec": int,
       "op_id": str, "reason": str}

Секунды для индикации ведёт UI-таймер (на больном сервере poll-GET блокирует
поток); поток отдаёт лишь ИТОГ. STOP-флаг (closeEvent дренаж): прерывает ОБА
теста — если стоп, второй не запускается, emit не происходит.
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
# v5: провайдер задаётся полем model. Прогоняем оба последовательно.
TEST_MODELS = ("nano-banana-2", "openai-image")

SUBMIT_TIMEOUT_SEC = 60     # POST submit — обычно быстрый (операция async)
POLL_TIMEOUT_SEC   = 30     # один GET generations/{id}
POLL_EVERY_SEC     = 5      # как часто опрашивать статус операции
CEILING_SEC        = 300    # потолок ожидания НА КАЖДЫЙ тест (свой t0, 5 минут)


class ServerCheckThread(QThread):
    """Боевой тест генерации: ДВА прохода (nano-banana-2, openai-image)
    последовательно. Итог — здоровье сервера по каждому провайдеру."""

    # results: list из 2 dict {provider, outcome, sec, op_id, reason}.
    results = pyqtSignal(list)

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = str(key or "")
        self._stop = False

    def stop(self):
        """Запросить остановку (graceful shutdown). Прерывает ОБА теста."""
        self._stop = True

    def run(self):
        session = requests.Session()
        session.headers.update({"X-API-Key": self._key})
        out = []
        for model in TEST_MODELS:
            if self._stop:          # стоп ДО теста → второй не запускаем, без emit
                return
            res = self._run_one_test(session, model)
            if res is None:         # стоп ВНУТРИ теста → выходим без emit
                return
            out.append(res)
        if self._stop:
            return
        self.results.emit(out)

    def _run_one_test(self, session, model: str):
        """Один v5-тест для данного model. СВОЙ t0 + СВОЙ потолок CEILING_SEC
        (счётчик времени сброшен на старте — первый тест НЕ съедает время второго).
        Возвращает dict результата ИЛИ None при стопе (прерывание обоих тестов)."""
        t0 = time.monotonic()       # ← СВОЙ таймер ЭТОГО теста (сброс между тестами)
        op_id = ""

        # 1) SUBMIT (v5): POST /api/v5/generations?result_format=ref, payload +model,
        #    БЕЗ рефов (inputs не передаём — тестовая генерация из текста).
        payload = {"prompt": TEST_PROMPT, "aspect_ratio": TEST_ASPECT, "model": model}
        try:
            r = session.post(f"{API_BASE}/api/v5/generations",
                             params={"result_format": "ref"},
                             json=payload, timeout=SUBMIT_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("id") or ""        # v5: op_id в поле "id"
            if not op_id:
                return self._mk(model, 'down', t0, "", _reason_from(data))
        except requests.exceptions.ConnectionError:
            return self._mk(model, 'noconn', t0, "", "")
        except Exception as _e:
            # timeout submit, 4xx/5xx, JSON-мусор — сервер не принял задачу.
            return self._mk(model, 'down', t0, "", _reason_from(_e))

        # 2) POLL (v5) — механика ПРЕЖНЯЯ: msleep(1000) + POLL_EVERY_SEC троттл,
        #    потолок CEILING_SEC по СВОЕМУ t0; self._stop прерывает оба теста.
        last_poll = time.monotonic()
        while True:
            if self._stop:
                return None                     # стоп → прерываем оба теста (без emit)
            if time.monotonic() - t0 >= CEILING_SEC:
                break                           # потолок ЭТОГО теста (свой t0)
            self.msleep(1000)
            if time.monotonic() - last_poll < POLL_EVERY_SEC:
                continue
            last_poll = time.monotonic()
            try:
                pr = session.get(f"{API_BASE}/api/v5/generations/{op_id}",
                                 params={"result_format": "ref"},
                                 timeout=POLL_TIMEOUT_SEC)
                pr.raise_for_status()
                d = pr.json() or {}
                status = d.get("status")
                if status in ("succeeded", "success", "completed", "done"):
                    return self._mk(model, 'ok', t0, op_id, "")
                if status in ("failed", "error", "cancelled"):
                    return self._mk(model, 'down', t0, op_id, _reason_from(d))
                # queued / running / pending — продолжаем опрашивать.
            except requests.exceptions.ConnectionError:
                return self._mk(model, 'noconn', t0, op_id, "")
            except Exception:
                # Транзиентный таймаут/5xx одного poll — не сдаёмся, опрашиваем
                # дальше до потолка (на перегруженном сервере GET тоже виснет).
                continue

        # Потолок CEILING_SEC без success — сервер висит на этом провайдере.
        return self._mk(model, 'down', t0, op_id, "timeout")

    def _mk(self, provider: str, outcome: str, t0: float, op_id: str, reason: str):
        """Собрать dict результата (sec по СВОЕМУ t0 этого теста)."""
        return {"provider": str(provider), "outcome": str(outcome),
                "sec": int(time.monotonic() - t0),
                "op_id": str(op_id or ""), "reason": str(reason or "")[:120]}


def _reason_from(obj) -> str:
    """Краткая причина провала из v5-тела {error, code} ИЛИ исключения. '' если нет.
    Ключ/секреты НЕ извлекаются — только серверный текст ошибки. Изолировано, не кидает."""
    try:
        if isinstance(obj, dict):
            e = obj.get("error") or ""
            c = obj.get("code") or ""
            return (f"{e} [{c}]".strip() if (e or c) else "")[:120]
        resp = getattr(obj, "response", None)
        if resp is not None:
            try:
                d = resp.json()
                if isinstance(d, dict):
                    e = d.get("error") or ""
                    c = d.get("code") or ""
                    if e or c:
                        return f"{e} [{c}]".strip()[:120]
            except Exception:
                pass
            try:
                return (resp.text or "")[:120]
            except Exception:
                return ""
    except Exception:
        pass
    return ""
