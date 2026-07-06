# -*- coding: utf-8 -*-
"""
threads/cancel.py — фоновая best-effort отмена задач генерации на сервере FastGen
при нажатии «Остановить генерацию». Для каждой пары (op_id, key) шлём
DELETE /api/v5/generations/{id} с X-API-Key ЭТОЙ задачи (операции привязаны к
ключу, которым созданы) и ПУСТЫМ телом. Локальный стоп уже произошёл —
серверная отмена лишь освобождает кредиты/слоты быстрее.

v5 (2026-06-20, форма подтверждена живым прозвоном коллеги): отмена —
DELETE /api/v5/generations/{id} (id в URL, тела нет). HTTP 200 ≠ «отменено» —
читать поле "cancelled" в теле (приходит СТРОКОЙ "True"/"False"). Старый v4
POST /api/v4/operations/cancel с body {"operation_id": …} больше НЕ работает
(поле operation_id не принимается → 422). Результат каждой отмены пишем в
runtime.log тегом [CANCEL] (print → studio tee); сам ключ в лог НЕ пишем.

Cross-platform: чистый requests, без subprocess/shell.
"""
from __future__ import annotations

import requests
from PyQt6.QtCore import QThread, QTimer

# Тот же боевой хост, что и генерация (storyboard_app.API_BASE).
API_BASE = "https://googler.fast-gen.ai"
CANCEL_TIMEOUT_SEC = 15


class CancelThread(QThread):
    """Отменяет список задач [(op_id, key), …] на сервере. best-effort, без
    сигналов результата — вызывающий снимает поток через reaper по finished."""

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self._pairs = list(pairs or [])

    def run(self):
        for op_id, key in self._pairs:
            if not op_id or not key:
                continue
            try:
                # v5: отмена — DELETE /api/v5/generations/{id}, X-API-Key
                # ключа-создателя, ПУСТОЕ тело (без json=). raise_for_status
                # НЕ зовём — 4xx/5xx по одной паре не должны ронять остальные.
                r = requests.delete(
                    f"{API_BASE}/api/v6/generations/{op_id}",
                    headers={"X-API-Key": str(key)},
                    timeout=CANCEL_TIMEOUT_SEC)
                # v5: HTTP 200 ≠ отменено. Поле "cancelled" — СТРОКА "True"/"False".
                cancelled = False
                try:
                    cancelled = str(r.json().get("cancelled")).lower() == "true"
                except Exception:
                    pass  # ответ не-JSON/пустой → cancelled=False, http залогируем
                # Лог в runtime.log (studio tee, тег [CANCEL]). Ключ НЕ пишем.
                print(f"[CANCEL] op_id={op_id} http={r.status_code} "
                      f"cancelled={cancelled}")
            except Exception as e:
                # Сеть/таймаут — best-effort: логируем и продолжаем остальные пары.
                print(f"[CANCEL] op_id={op_id} FAILED: {e}")


def spawn_server_cancel(owner, pairs, on_done=None):
    """Best-effort серверная отмена задач [(op_id, key), …] в фоне — ЕДИНАЯ
    точка и для «Остановить генерацию» шотов (MainWindow._stop_all_generation),
    и для отмены по корзине в GeneratorPage. Создаёт CancelThread, держит ссылку
    в owner._cancel_threads (анти-GC), по finished зовёт on_done(ct) (обычно
    retire). Пустые/битые пары отфильтровываются. Возвращает CancelThread | None.
    """
    clean = [(o, k) for (o, k) in (pairs or []) if o and k]
    if not clean:
        return None
    try:
        ct = CancelThread(clean, owner)
        owner._cancel_threads = getattr(owner, "_cancel_threads", [])
        owner._cancel_threads.append(ct)
        if on_done is not None:
            ct.finished.connect(lambda c=ct: on_done(c))
        ct.start()
        return ct
    except Exception:
        return None


class ThreadReaper:
    """Переиспользуемый жнец QThread'ов. Безопасно снимает ссылку на
    завершившийся поток: НЕ дропаем сразу — кастомный сигнал finished/error
    летит ИЗНУТРИ run() ДО его возврата, и немедленный drop последней ссылки
    даёт GC работающего QThread → 'Destroyed while thread is still running' →
    abort(). Держим в keep-alive пока isFinished(), затем deleteLater по таймеру.

    owner — QObject-владелец (parent для QTimer, чтобы таймер жил с ним).
    Логика зеркалит исторический inline-жнец MainWindow (_retire_thread /
    _reap_finished_threads); вынесена сюда для переиспользования GeneratorPage
    (отмена по корзине) без копипасты.
    """

    def __init__(self, owner, interval=500):
        self._pending = set()
        self._timer = QTimer(owner)
        self._timer.setInterval(int(interval))
        self._timer.timeout.connect(self._reap)

    def pending(self):
        """Копия набора ещё-не-утилизированных потоков (для close-проверок)."""
        return set(self._pending)

    def retire(self, t):
        if t is None:
            return
        self._pending.add(t)
        if not self._timer.isActive():
            self._timer.start()

    def _reap(self):
        if not self._pending:
            self._timer.stop()
            return
        done = [t for t in self._pending if t is None or t.isFinished()]
        for t in done:
            self._pending.discard(t)
            if t is not None:
                try:
                    t.deleteLater()
                except Exception:
                    pass
        if not self._pending:
            self._timer.stop()
