# -*- coding: utf-8 -*-
"""
generator/generator_video_thread.py — поток генерации ВИДЕО для «Генератора»
(2026-06-21).

Копия generator/generator_thread.py (GeneratorImageThread) с отличиями под видео:
  • duration: None для Veo ("flow-video-fast" — всегда ~8с, duration_seconds НЕ слать,
    иначе 422); int (4/6/8/10) для Omni ("flow-video-omni-flash" — обязателен).
  • POLL_TIMEOUT_SEC = 600 (видео генерится дольше картинок).
  • сохранение .mp4 (не .jpg); download /raw c бОльшим таймаутом (файлы крупнее).
v5-контракт (эндпоинт, op_id, poll, storage download, [FASTGEN]-диаг) — тот же.

storyboard_app тянется ЛЕНИВО внутри run() (API_BASE / STORAGE_BASE / next_api_key) —
circular-import защита для frozen .app (как в image-потоке).

Cross-platform: requests + pathlib.Path + datetime, БЕЗ subprocess/shell/open() —
никаких console-window нюансов Win; путь строится через Path, не f-string-слешами.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from PyQt6.QtCore import QThread, pyqtSignal


POLL_TIMEOUT_SEC = 600   # видео дольше картинок (генерация 60-180с; потолок 10 мин)
KEY_SEARCH_TIMEOUT_SEC = 180  # потолок ожидания СВОБОДНОГО ключа (перебор ДО op_id).
                              # По истечении — error + return, не виснем навсегда.
_OK_STATUSES = ("succeeded", "success", "completed", "done")
_FAIL_STATUSES = ("failed", "error", "cancelled")


class GeneratorVideoThread(QThread):
    """Одно видео через FastGen v5. Сигналы: progress(str)/finished(path .mp4)/error(str)."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # абсолютный путь к сохранённому .mp4
    error = pyqtSignal(str)

    def __init__(self, prompt: str, aspect_ratio: str, model_id: str,
                 duration, out_dir: Path, refs=None, keyframes=False, parent=None):
        super().__init__(parent)
        self.prompt = (prompt or "").strip()
        self.aspect_ratio = aspect_ratio or "16:9"
        self.model_id = model_id
        self.duration = duration         # None для Veo; int (4/6/8/10) для Omni
        self.out_dir = Path(out_dir)
        # Прикреплённые рефы (per-генерация). Default None → list тут (избегаем
        # mutable-default). Veo/Omni Flash принимают inputs того же формата
        # {"filename","input"} что и image-провайдеры (подтверждено живым тестом).
        self.refs: list = [str(p) for p in (refs or [])]
        # Veo «Кадры»-режим: payload.keyframes=True (start/end frame guidance).
        # False для Omni/прочих — флаг не уходит в payload (см. run()).
        self.keyframes = bool(keyframes)
        self._stop = False

    def stop(self):
        self._stop = True

    def _file_to_data_uri(self, path: Path) -> str:
        """Файл → base64 data URI для v5 inputs[].input. Без /upload — inputs
        видны ЛЮБОМУ ключу submit-loop'а (фикс 403: storage upload-ключа A
        не виден submit-ключу B). Нет TTL — рефы валидны столько же, сколько
        живёт запрос. MIME по расширению; неизвестные → png."""
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/{mime};base64,{b64}"

    def _fastgen(self, op_id, status, elapsed, result, extra=""):
        """Диаг-строка [FASTGEN] (→ runtime.log через studio tee). provider=generator-video."""
        print(f"[FASTGEN] path=GeneratorVideoThread api=v5 "
              f"endpoint=/api/v5/generations auth=X-API-Key "
              f"model={self.model_id} provider=generator-video result_format=ref "
              f"duration={self.duration} inputs={len(self.refs)} op_id={op_id} status={status} "
              f"time={elapsed} result={result}{extra}")

    def run(self):
        import storyboard_app as _sa
        try:
            # Сколько живых ключей — столько максимум попыток submit (часть ключей
            # может быть без видео-доступа → 403, или с исчерпанным лимитом → 429).
            try:
                import key_pool
                attempts = max(1, int(key_pool.live_key_count()))
            except Exception:
                attempts = 5

            # v5: провайдер выбирается полем model; duration_seconds — ТОЛЬКО для omni
            # (для Veo None → не слать, иначе 422).
            payload = {
                "prompt": self.prompt,
                "aspect_ratio": self.aspect_ratio,
                "model": self.model_id,
            }
            if self.duration is not None:
                payload["duration_seconds"] = self.duration

            # ── РЕФЫ → base64 data URI (без /upload, без storage hash) ──
            # Storage hash виден только сессии-аплоадеру, а submit-loop ниже
            # перебирает разные ключи → hash от upload-ключа A не виден submit-
            # ключу B → 403. Data URI идёт прямо в payload и не зависит от ключа.
            ref_inputs = []
            if self.refs:
                for _p in self.refs:
                    try:
                        pp = Path(_p)
                        ref_inputs.append({"filename": pp.name,
                                           "input": self._file_to_data_uri(pp)})
                    except Exception as e:
                        self.progress.emit(f"Реф не загрузился: {Path(_p).name}")
                        print(f"[FASTGEN] ref_to_base64 FAILED for {Path(_p).name}: {e}")
                # Если рефы были запрошены, но ни один не подготовился — не идём
                # дальше с «пустыми» inputs (юзер ждёт результат С рефом).
                if not ref_inputs:
                    self.error.emit("Не удалось подготовить ни одного рефа")
                    return
                # v5 schema: ключ "filename" (не "name") — критично для Veo/Omni.
                payload["inputs"] = ref_inputs
            # Veo «Кадры»-режим: payload.keyframes=True. На «Рефы» (ingredients —
            # default сервера) поле не уходит. Для Omni/прочих self.keyframes=False.
            if self.keyframes:
                payload["keyframes"] = True
            # Диагностика — что реально уходит в /generations для видео.
            print(f"[FASTGEN] outgoing video payload keys={list(payload.keys())} "
                  f"inputs_count={len(payload.get('inputs', []))}")

            # ── ПЕРЕБОР КЛЮЧЕЙ ПО КРУГУ (как генератор картинок, но НЕ сдаёмся) ──
            # Рабочий ключ держим в session и ИМ же поллим/качаем (чужой ключ на
            # poll = 404, op_id привязан к ключу submit). disable_key НЕ зовём —
            # ключ валиден для картинок; скип чисто локальный, в этом потоке.
            # dead       — ключи 401/403 (нет видео-доступа): выброс на ВСЮ сессию.
            # round_tried — ключи 429 (заняты/concurrency): до конца круга; круг
            #   замкнулся → пауза 2с + сброс + новый круг. НЕ сдаёмся пока не таймаут.
            # Потолок поиска — KEY_SEARCH_TIMEOUT_SEC (отдельный таймер search_t0).
            # Секундомер генерации (t0) стартует ТОЛЬКО после op_id — ниже.
            session = None
            op_id = None
            dead = set()
            round_tried = set()
            spins = 0
            idle_skips = 0   # подряд dead-пропусков без реальной работы (guard от busy-loop)
            search_t0 = time.monotonic()
            self.progress.emit("Ищу свободный ключ для видео…")
            while op_id is None:
                if self._stop:
                    return
                if (time.monotonic() - search_t0) > KEY_SEARCH_TIMEOUT_SEC:
                    self._fastgen("-", "submit",
                                  int(time.monotonic() - search_t0),
                                  "error", " error=key_search_timeout")
                    self.error.emit(
                        f"Не нашёл свободный ключ для видео за {KEY_SEARCH_TIMEOUT_SEC}с — "
                        f"все заняты, попробуй позже.")
                    return
                # Все живые ключи мертвы (401/403) — ждать бессмысленно, выходим.
                if len(dead) >= attempts:
                    self.error.emit("Нет ключей с доступом к видео")
                    return
                spins += 1
                key, idx = _sa.next_api_key()
                if not key:
                    self.error.emit("Нет доступного API-ключа")
                    return
                # idx=None → kill-switch fallback (.env один ключ): СТАБИЛЬНЫЙ tkey
                # "_fb" (не spin-уникальный) — иначе round_tried-пейсинг не сработал бы.
                tkey = idx if idx is not None else "_fb"
                if tkey in dead:
                    # Мёртвый ключ (401/403) — пропускаем. Guard от busy-loop: если
                    # next_api_key крутит ТОЛЬКО dead-ключи (live_count завысил attempts),
                    # после полного круга вхолостую — пауза 2с вместо греющего ядро spin.
                    idle_skips += 1
                    if idle_skips >= attempts:
                        time.sleep(2)
                        idle_skips = 0
                    continue
                if tkey in round_tried:
                    # Круг замкнулся — все живые ключи заняты → пауза и новый круг.
                    self.progress.emit("Ожидаю свободный ключ…")
                    time.sleep(2)
                    round_tried.clear()
                    idle_skips = 0
                    continue
                # Дошли до реальной попытки submit — сбрасываем idle-счётчик.
                idle_skips = 0
                s = requests.Session()
                s.headers.update({"X-API-Key": key})
                try:
                    r = s.post(f"{_sa.API_BASE}/api/v6/generations",
                               params={"result_format": "ref"},
                               json=payload, timeout=60)
                except requests.exceptions.RequestException as e:
                    # Сетевой сбой — НЕ ключевая проблема, не перебираем.
                    self.error.emit(f"Сеть/сервер недоступен: {str(e)[:200]}")
                    return
                code = r.status_code
                if code in (401, 403):
                    # Нет видео-доступа — выбрасываем ключ из ротации на сессию.
                    self._fastgen("-", "submit", 0, "error",
                                  f" error=http_{code} key_idx={idx}")
                    # Логируем body 4xx — подсказка от сервера (например "model not supported for inputs")
                    try:
                        body_text = r.text[:500] if r.text else ''
                        print(f"[FASTGEN] video submit {code} body={body_text}")
                    except Exception:
                        pass
                    dead.add(tkey)
                    continue
                if code == 429:
                    # Ключ занят (concurrency) — вернёмся к нему на следующем круге.
                    self._fastgen("-", "submit", 0, "error",
                                  f" error=http_{code} key_idx={idx}")
                    try:
                        body_text = r.text[:500] if r.text else ''
                        print(f"[FASTGEN] video submit {code} body={body_text}")
                    except Exception:
                        pass
                    round_tried.add(tkey)
                    continue
                if not r.ok:
                    # Не ключевая (400 битый payload, 5xx и т.п.) — обычная ошибка.
                    # Логируем тело — подсказка сервера (например keyframes requires
                    # 1-2 inputs). Тот же [FASTGEN]-формат что для 401/403/429 выше.
                    try:
                        body_text = r.text[:500] if r.text else ''
                        print(f"[FASTGEN] video submit {code} body={body_text}")
                    except Exception:
                        pass
                    self.error.emit(f"Ошибка отправки запроса: HTTP {code}")
                    return
                data = r.json()
                op_id = data.get("id")
                if not op_id:
                    self.error.emit(f"Сервер не вернул id: {str(data)[:200]}")
                    return
                session = s   # рабочий ключ — им поллим и качаем

            self.progress.emit("Генерирую видео…")
            t0 = time.monotonic()
            last_status = ""
            while True:
                if self._stop:
                    return
                time.sleep(1.5)
                if self._stop:
                    return
                elapsed = int(time.monotonic() - t0)
                if elapsed > POLL_TIMEOUT_SEC:
                    self._fastgen(op_id, last_status or "unknown", elapsed,
                                  "error", " error=timeout")
                    self.error.emit(
                        f"API timeout: статус «{last_status or 'unknown'}» "
                        f"оставался {elapsed}с (>10 мин). Попробуй ещё раз.")
                    return
                try:
                    rr = session.get(f"{_sa.API_BASE}/api/v6/generations/{op_id}",
                                     params={"result_format": "ref"}, timeout=30)
                    rr.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    # op_id привязан к ключу submit → переключить ключ на poll НЕЛЬЗЯ.
                    pc = getattr(getattr(e, "response", None), "status_code", None)
                    self._fastgen(op_id, last_status or "poll", elapsed,
                                  "error", f" error=poll_http_{pc}")
                    if pc in (401, 403, 429):
                        self.error.emit("Ключ исчерпал доступ во время генерации видео — "
                                        "попробуй ещё раз.")
                    else:
                        self.error.emit(f"Ошибка опроса статуса: HTTP {pc}")
                    return
                except requests.exceptions.RequestException as e:
                    self.error.emit(f"Сеть/сервер недоступен: {str(e)[:200]}")
                    return
                d = rr.json()
                status = (d.get("status") or "").lower()
                last_status = status
                self.progress.emit(f"Генерирую видео… ({elapsed}с · {status or '...'})")

                if status in _OK_STATUSES:
                    results = d.get("results") or d.get("result") or []
                    # v6: results[0].download_url — полный URL, скачиваем КАК ЕСТЬ.
                    # Если поля нет — fallback на v5 (storage_id → STORAGE_BASE/file/{fh}/raw).
                    download_url = ""
                    if results and isinstance(results[0], dict):
                        download_url = results[0].get("download_url") or ""
                    if download_url:
                        # видео крупнее картинок → больше таймаут на скачивание.
                        r2 = session.get(download_url, timeout=300)
                        r2.raise_for_status()
                        video_bytes = r2.content
                        fh = download_url   # для диаг-строки [FASTGEN] ниже
                    else:
                        # v5: storage_id = results[0].metadata.storage_id; fallback v4-разбор.
                        uri = ""
                        if results and isinstance(results[0], dict):
                            uri = ((results[0].get("metadata") or {}).get("storage_id") or "")
                        if not uri:
                            uri = results[0] if (isinstance(results, list) and results) else results
                            if isinstance(uri, dict):
                                uri = (uri.get("url") or uri.get("ref")
                                       or uri.get("file_hash") or "")
                            uri = str(uri)
                        fh = uri[5:] if uri.startswith("file:") else uri
                        # видео крупнее картинок → больше таймаут на скачивание.
                        r2 = session.get(f"{_sa.STORAGE_BASE}/file/{fh}/raw", timeout=300)
                        r2.raise_for_status()
                        video_bytes = r2.content
                    self._fastgen(op_id, status, elapsed, "ok",
                                  f" storage_id={str(fh)[:8]}")
                    break

                if status in _FAIL_STATUSES:
                    self._fastgen(op_id, status, elapsed, "error",
                                  f" error={str(d.get('error') or '<none>')[:120]}")
                    self.error.emit(f"Ошибка генерации: {d.get('error')}")
                    return
                # queued / running / processing / pending — продолжаем poll

            if self._stop:
                return
            # Сохранение: shows/<slug>/generator/gen_YYYYmmdd_HHMMSS.mp4 (уникально)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.out_dir / f"gen_{ts}.mp4"
            i = 2
            while target.exists():
                target = self.out_dir / f"gen_{ts}_{i}.mp4"
                i += 1
            target.write_bytes(video_bytes)
            # best-effort: первый кадр → gen_*.jpg рядом (плитка покажет как превью).
            # Не-ASCII путь / cv2 не смог → None, плитка останется на ▶ (не падаем).
            self._extract_first_frame(target)
            self.finished.emit(str(target))
        except Exception as e:
            if self._stop:
                return
            detail = str(e)[:300]
            # HTTP-ошибки: вытащить body — иначе 404/422 непрозрачны (то же,
            # что в image-thread).
            try:
                if hasattr(e, 'response') and e.response is not None:
                    body = e.response.text[:500] if e.response.text else ''
                    if body:
                        detail = f"{detail} | body: {body}"
                        print(f"[FASTGEN] path=GeneratorVideoThread error_detail={body[:300]}")
            except Exception:
                pass
            self.error.emit(detail)

    def _extract_first_frame(self, mp4_path: Path) -> Optional[Path]:
        """Первый кадр .mp4 → .jpg рядом (то же имя, расширение .jpg) — превью плитки.

        cv2 тянется ЛЕНИВО (паттерн detector.py — модуль не должен падать без
        opencv). Запись — через cv2.imencode(".jpg") + Path.write_bytes, НЕ
        cv2.imwrite: imwrite (как imread) спотыкается на не-ASCII путях Windows;
        write_bytes Unicode-путь переваривает. На не-ASCII пути VideoCapture может
        не открыть файл → возвращаем None (плитка покажет ▶). Любая ошибка → None:
        превью необязательно, генерация уже успешна.
        """
        try:
            import cv2
            import numpy as np  # noqa: F401  (cv2 возвращает numpy-массив кадра)
            cap = cv2.VideoCapture(str(mp4_path))
            try:
                ok, frame = cap.read()
            finally:
                cap.release()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                return None
            ok2, buf = cv2.imencode(".jpg", frame)
            if not ok2:
                return None
            jpg_path = mp4_path.with_suffix(".jpg")
            jpg_path.write_bytes(buf.tobytes())
            return jpg_path
        except Exception:
            return None
