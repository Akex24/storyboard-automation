# -*- coding: utf-8 -*-
"""
generator/generator_thread.py — лёгкий поток генерации картинки для страницы
«Генератор» (MVP, 2026-06-20).

Сквозной путь: prompt + aspect_ratio + model_id → POST /api/v5/generations
(БЕЗ рефов, inputs нет) → poll → скачать → сохранить в out_dir → finished(path).

Образец — GenerateActorRefThread (threads/generate.py:1782), но упрощённый: без
upload фото / ref_hashes / actor-логов / троттл-слотов / regen-edit. v5-контракт
(эндпоинт, op_id, poll, storage download, [FASTGEN]-диаг) — тот же.

storyboard_app тянется ЛЕНИВО внутри run() (API_BASE / STORAGE_BASE / next_api_key) —
circular-import защита для frozen .app (как widgets/shot_viewer_dialog.py).

Cross-platform: requests + pathlib.Path + datetime, БЕЗ subprocess/shell/open() —
никаких console-window нюансов Win; путь строится через Path, не f-string-слешами.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from pathlib import Path

import requests
from PyQt6.QtCore import QThread, pyqtSignal

# tr() локализованных сообщений (i18n — лист-модуль, без circular import; как в
# threads/generate.py). gen_errors (классификатор + human_message) импортируется
# ЛЕНИВО в run() — он в пакете generator, а его __init__ тянет generator_page.
from i18n import tr


POLL_TIMEOUT_SEC = 300   # потолок ожидания ГЕНЕРАЦИИ (поллинг после op_id).
                         # OpenAI+inputs тянет 150-170с; 2026-06-28 снижен 600→300.
KEY_SEARCH_TIMEOUT_SEC = 180  # потолок ожидания СВОБОДНОГО ключа (перебор ДО op_id).
                              # По истечении — error + return, не виснем навсегда.
_OK_STATUSES = ("succeeded", "success", "completed", "done")
_FAIL_STATUSES = ("failed", "error", "cancelled")
# 2026-07-04: HTTP-коды перегруза/сброса гейтвея FastGen — ретраятся на submit/poll.
# 499 = «Client Closed Request» (гейтвей сбросил соединение под нагрузкой). 429 сюда
# НЕ входит (занятость ключа — свой перебор по кругу). Зеркально video-треду.
_RETRYABLE_HTTP = (408, 499, 500, 502, 503, 504)


# Детектор транзиентных ошибок (is_transient) + человеческие локализованные
# сообщения (human_message) вынесены в generator/gen_errors.py (единый модуль,
# больше не дублируется в image/video тредах). Импорт — ЛЕНИВЫЙ в run().


class GeneratorImageThread(QThread):
    """Одна картинка через FastGen v5. Сигналы: progress(str)/finished(path)/error(str)."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # абсолютный путь к сохранённому .jpg
    error = pyqtSignal(str)

    def __init__(self, prompt: str, aspect_ratio: str, model_id: str,
                 out_dir: Path, refs=None, parent=None, ref_names=None):
        super().__init__(parent)
        self.prompt = (prompt or "").strip()
        self.aspect_ratio = aspect_ratio or "16:9"
        self.model_id = model_id
        self.out_dir = Path(out_dir)
        # Прикреплённые рефы (per-генерация). Default None → конвертим в list тут
        # (избегаем mutable-default). Пусто → обычная генерация без inputs.
        self.refs: list = [str(p) for p in (refs or [])]
        self.ref_names: list = [str(n) for n in (ref_names or [])]
        self._stop = False

    def stop(self):
        self._stop = True

    def _file_to_data_uri(self, path: Path) -> str:
        """Файл → base64 data URI для v5 inputs[].input. Без /upload — не
        зависит от ключа (любая сессия видит inputs), нет TTL/expiration.
        Фикс 403 «hash от ключа A не виден ключу B» в видео/мульти-ключ
        сценариях. MIME по расширению (jpg/jpeg/png/webp); неизвестные → png."""
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
        raw = path.read_bytes()
        # 2026-07-04: тяжёлые рефы (4K/~4МБ) раздувают тело submit → на медленном
        # аплоаде gateway рвёт запрос (write timeout / 499). Жмём КОПИЮ В ПАМЯТИ
        # (on-disk реф не трогаем): длинная сторона ≤2048px, JPEG q90→80 до ~1МБ.
        # Мелкий лёгкий реф (≤2048px И ≤1.5МБ) — как есть. Любая ошибка PIL →
        # фолбэк на сырой файл (сжатие генерацию не роняет).
        try:
            import io
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
            if max(im.size) > 2048 or len(raw) > 1_500_000:
                if max(im.size) > 2048:
                    im.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGB")
                data = None
                for q in (90, 88, 85, 82, 80):
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=q, optimize=True)
                    data = buf.getvalue()
                    if len(data) <= 1_050_000:
                        break
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            print(f"[FASTGEN] ref compress skipped ({path.name}): {e}")
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:image/{mime};base64,{b64}"

    def _fastgen(self, op_id, status, elapsed, result, extra=""):
        """Диаг-строка [FASTGEN] (→ runtime.log через studio tee), формат как в
        threads/generate.py. provider=generator — метка пути."""
        print(f"[FASTGEN] path=GeneratorImageThread api=v6 "
              f"endpoint=/api/v6/generations auth=X-API-Key "
              f"model={self.model_id} provider=generator result_format=ref "
              f"inputs={len(self.refs)} op_id={op_id} status={status} time={elapsed} "
              f"result={result}{extra}")

    def run(self):
        import storyboard_app as _sa
        # gen_errors — ЛЕНИВО (пакет generator → __init__ тянет generator_page; на
        # момент run() страница уже загружена, circular нет). is_transient — ретрай-
        # решение; human_message — локализованная причина для юзера.
        from generator.gen_errors import is_transient, human_message, classify_error

        # 2026-07-04: единый механизм паузы перед авто-повтором (submit+poll).
        # 10с, дроблёные по _stop. False → пришёл stop, вызывающий обязан return.
        def _retry_pause(attempt) -> bool:
            self.progress.emit(tr('gen_prog_retry', n=attempt + 1, total=3))
            for _ in range(20):
                if self._stop:
                    return False
                time.sleep(0.5)
            return True
        try:
            # Рефы → v5 inputs[].input как base64 data URI (без /upload, без
            # storage hash, без TTL/expiration). Видны любому ключу — фикс 403
            # «hash от ключа A не виден ключу B». Degrade: ошибка на одном
            # НЕ валит всю генерацию (progress + продолжаем без него).
            # Лимит количества — на UI-уровне (generator_page._max_refs/add_ref),
            # сюда уже приходит проверенный self.refs.
            # 2026-06-28: payload собирается ДО перебора ключей (он от ключа не
            # зависит) — submit ниже шлёт его на КАЖДЫЙ пробуемый ключ.
            ref_inputs = []
            if self.refs:
                for i, _p in enumerate(self.refs):
                    try:
                        pp = Path(_p)
                        filename = self.ref_names[i] if i < len(self.ref_names) else pp.name
                        ref_inputs.append({"filename": filename,
                                           "input": self._file_to_data_uri(pp)})
                    except Exception as e:
                        self.progress.emit(tr('gen_prog_ref_failed', name=Path(_p).name))
                        print(f"[FASTGEN] ref_to_base64 FAILED for {Path(_p).name}: {e}")
                if not ref_inputs:
                    self.error.emit(tr('gen_err_no_refs'))
                    return

            # v5: провайдер выбирается полем model; единый эндпоинт.
            payload = {
                "prompt": self.prompt,
                "aspect_ratio": self.aspect_ratio,
                "model": self.model_id,
            }
            if ref_inputs:
                payload["inputs"] = ref_inputs
            # Диагностика — что реально уходит в /generations (data URI длинный,
            # печатаем только размер inputs, не первые байты).
            print(f"[FASTGEN] outgoing payload keys={list(payload.keys())} "
                  f"inputs_count={len(payload.get('inputs', []))}")

            # ── ПЕРЕБОР КЛЮЧЕЙ ПО КРУГУ (как видео-поток, но НЕ сдаёмся) ──────
            # dead       — ключи 401/403 (нет доступа): выброшены на ВСЮ сессию.
            # round_tried — ключи 429 (заняты): до конца круга; круг замкнулся
            #   (next_api_key вернул уже-пробованный) → пауза 2с + сброс + новый круг.
            # Потолок поиска — KEY_SEARCH_TIMEOUT_SEC (отдельный таймер search_t0).
            # Секундомер генерации (t0) стартует ТОЛЬКО после op_id — ниже.
            try:
                import key_pool
                attempts = max(1, int(key_pool.live_key_count()))
            except Exception:
                attempts = 5
            # 2026-06-28: авто-ретрай транзиентных ошибок — ВСЯ генерация (submit+poll)
            # обёрнута в цикл до 4 попыток. На транзиентном failed (gen_errors.is_transient)
            # и retry_attempt<3 → пауза 10с + новая попытка (свежий перебор ключей).
            # Контент/лицензия/валидация и POLL_TIMEOUT — НЕ ретраятся (см. ниже).
            # 2026-07-04: сюда же добавлены транзиентные HTTP 499/500/502/503/504 и обрыв
            # соединения (connection aborted / read-write timeout) на submit И poll —
            # тот же потолок 3 повтора. 401/403/429 (ключи), key_search_timeout,
            # POLL_TIMEOUT, invalid_request/контент/лицензия — НЕ ретраятся.
            for retry_attempt in range(4):
                # Сброс submit-состояния на КАЖДОЙ попытке (свежий перебор ключей).
                session = None
                op_id = None
                retry_pending = False   # True → транзиентный сбой submit/poll → повтор
                dead = set()
                round_tried = set()
                spins = 0
                idle_skips = 0   # подряд dead-пропусков без реальной работы (guard от busy-loop)
                search_t0 = time.monotonic()
                while op_id is None:
                    if self._stop:
                        return
                    if (time.monotonic() - search_t0) > KEY_SEARCH_TIMEOUT_SEC:
                        self._fastgen("-", "submit",
                                      int(time.monotonic() - search_t0),
                                      "error", " error=key_search_timeout")
                        self.error.emit(tr('gen_err_queue_timeout', sec=KEY_SEARCH_TIMEOUT_SEC))
                        return
                    # Все живые ключи мертвы (401/403) — ждать бессмысленно, выходим.
                    if len(dead) >= attempts:
                        self.error.emit(tr('gen_err_no_gen_keys'))
                        return
                    spins += 1
                    key, idx = _sa.next_api_key()
                    if not key:
                        self.error.emit(tr('gen_err_no_api_key'))
                        return
                    # idx=None → kill-switch fallback (.env один ключ): СТАБИЛЬНЫЙ tkey
                    # "_fb" (не spin-уникальный) — иначе round_tried-пейсинг не сработал бы.
                    tkey = idx if idx is not None else "_fb"
                    if tkey in dead:
                        # Мёртвый ключ (401/403) — пропускаем. Guard от busy-loop: если
                        # next_api_key крутит ТОЛЬКО dead-ключи (live_count завысил attempts
                        # → top-check len(dead)>=attempts не срабатывает), после полного круга
                        # вхолостую делаем паузу 2с вместо греющего ядро spin.
                        idle_skips += 1
                        if idle_skips >= attempts:
                            time.sleep(2)
                            idle_skips = 0
                        continue
                    if tkey in round_tried:
                        # Круг замкнулся — все живые ключи заняты → пауза и новый круг.
                        self.progress.emit(tr('gen_prog_queue'))
                        time.sleep(2)
                        round_tried.clear()
                        idle_skips = 0
                        continue
                    # Дошли до реальной попытки submit — сбрасываем idle-счётчик.
                    idle_skips = 0
                    self.progress.emit(tr('gen_prog_sending'))
                    s = requests.Session()
                    s.headers.update({"X-API-Key": key})
                    try:
                        r = s.post(f"{_sa.API_BASE}/api/v6/generations",
                                   params={"result_format": "ref"},
                                   json=payload, timeout=60)
                    except requests.exceptions.RequestException as e:
                        # 2026-07-04: обрыв/таймаут транспорта (connection aborted /
                        # read-write timeout) — транзиент. Ретраим до потолка.
                        if retry_attempt < 3:
                            self._fastgen("-", "submit", 0, "error", " error=net_retry")
                            if not _retry_pause(retry_attempt):
                                return
                            retry_pending = True
                            break
                        self.error.emit(tr('gen_err_network', detail=str(e)[:200]))
                        return
                    code = r.status_code
                    if code in (401, 403):
                        # Ключ без доступа — выбрасываем из ротации на сессию.
                        self._fastgen("-", "submit", 0, "error",
                                      f" error=http_{code} key_idx={idx}")
                        dead.add(tkey)
                        continue
                    if code == 429:
                        # Ключ занят (concurrency) — вернёмся к нему на следующем круге.
                        self._fastgen("-", "submit", 0, "error",
                                      f" error=http_{code} key_idx={idx}")
                        round_tried.add(tkey)
                        continue
                    if not r.ok:
                        # 400 битый payload / 5xx — не ключевая, не крутим.
                        b_code = b_err = ''
                        try:
                            jb = r.json()
                            if isinstance(jb, dict):
                                b_code = str(jb.get('code') or '')
                                b_err = str(jb.get('error') or '')
                        except Exception:
                            pass
                        print(
                            "[FASTGEN] submit non_ok "
                            f"http={code} code={b_code} error={b_err[:300]} "
                            f"body={r.text[:500]}"
                        )
                        # 2026-07-04: перегруз/сброс гейтвея (499/500/502/503/504) —
                        # транзиент, ретраим. invalid_request/контент ниже — НЕ ретраятся.
                        if code in _RETRYABLE_HTTP and retry_attempt < 3:
                            if not _retry_pause(retry_attempt):
                                return
                            retry_pending = True
                            break
                        # validation.invalid_request одинаков на всех ключах → человеческая
                        # причина; прочее (5xx) — с HTTP-кодом.
                        if classify_error(b_code, b_err) == 'invalid_request':
                            self.error.emit(human_message(b_code, b_err))
                        else:
                            self.error.emit(tr('gen_err_submit_http', code=code))
                        return
                    data = r.json()
                    op_id = data.get("id")
                    if not op_id:
                        self.error.emit(tr('gen_err_no_id', detail=str(data)[:200]))
                        return
                    session = s   # рабочий ключ — им поллим и качаем

                # 2026-07-04: submit-стадия попросила повтор (net/5xx) → следующая
                # попытка retry-цикла (op_id ещё None, poll пропускаем).
                if retry_pending:
                    continue
                self.progress.emit(tr('gen_prog_generating'))
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
                        self.error.emit(tr('gen_err_poll_timeout', status=last_status or 'unknown', sec=elapsed))
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
                        # 2026-07-04: перегруз/сброс гейтвея (499/500/502/503/504) на poll —
                        # транзиент, ретраим (новый submit+op_id). 401/403/429 ниже — НЕ ретраим.
                        if pc in _RETRYABLE_HTTP and retry_attempt < 3:
                            if not _retry_pause(retry_attempt):
                                return
                            retry_pending = True
                            break
                        if pc in (401, 403, 429):
                            self.error.emit(tr('gen_err_key_exhausted_poll'))
                        else:
                            self.error.emit(tr('gen_err_poll_http', code=pc))
                        return
                    except requests.exceptions.RequestException as e:
                        # 2026-07-04: обрыв/таймаут транспорта на poll — транзиент, ретраим.
                        if retry_attempt < 3:
                            self._fastgen(op_id, last_status or "poll", elapsed,
                                          "error", " error=net_retry")
                            if not _retry_pause(retry_attempt):
                                return
                            retry_pending = True
                            break
                        self.error.emit(tr('gen_err_network', detail=str(e)[:200]))
                        return
                    d = rr.json()
                    status = (d.get("status") or "").lower()
                    last_status = status
                    self.progress.emit(tr('gen_prog_generating'))

                    if status in _OK_STATUSES:
                        results = d.get("results") or d.get("result") or []
                        # v6: results[0].download_url — полный URL, скачиваем КАК ЕСТЬ.
                        # Если поля нет — fallback на v5 (storage_id → STORAGE_BASE/file/{fh}/raw).
                        download_url = ""
                        if results and isinstance(results[0], dict):
                            download_url = results[0].get("download_url") or ""
                        if download_url:
                            r2 = session.get(download_url, timeout=120)
                            r2.raise_for_status()
                            image_bytes = r2.content
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
                            r2 = session.get(f"{_sa.STORAGE_BASE}/file/{fh}/raw", timeout=120)
                            r2.raise_for_status()
                            image_bytes = r2.content
                        self._fastgen(op_id, status, elapsed, "ok",
                                      f" storage_id={str(fh)[:8]}")
                        break

                    if status in _FAIL_STATUSES:
                        err = d.get('error')
                        self._fastgen(op_id, status, elapsed, "error",
                                      f" error={str(err or '<none>')[:120]}")
                        # Транзиент + остались попытки → пауза 10с (дроблёно по stop) + повтор.
                        if is_transient(err) and retry_attempt < 3:
                            if not _retry_pause(retry_attempt):
                                return
                            retry_pending = True
                            break
                        # Реальная ошибка (контент/лицензия/валидация) или попытки кончились →
                        # человеческая локализованная причина (модерация без detail, прочие с [code]).
                        self.error.emit(human_message(d.get('code'), err))
                        return
                    # queued / running / processing / pending — продолжаем poll

                if retry_pending:
                    continue   # следующая попытка retry-цикла (новый submit + poll)
                break          # успех (image_bytes получен) → выходим из retry-цикла

            if self._stop:
                return
            # Сохранение: shows/<slug>/generator/gen_YYYYmmdd_HHMMSS.jpg (уникально)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = self.out_dir / f"gen_{ts}.jpg"
            i = 2
            while target.exists():
                target = self.out_dir / f"gen_{ts}_{i}.jpg"
                i += 1
            target.write_bytes(image_bytes)
            self.finished.emit(str(target))
        except Exception as e:
            if self._stop:
                return
            detail = str(e)[:300]
            # Логируем response body при HTTP-ошибках — иначе 404/422 непрозрачны.
            try:
                if hasattr(e, 'response') and e.response is not None:
                    body = e.response.text[:500] if e.response.text else ''
                    if body:
                        detail = f"{detail} | body: {body}"
                        print(f"[FASTGEN] path=GeneratorImageThread error_detail={body[:300]}")
            except Exception:
                pass
            self.error.emit(detail)
