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

import time
from datetime import datetime
from pathlib import Path

import requests
from PyQt6.QtCore import QThread, pyqtSignal


POLL_TIMEOUT_SEC = 300   # потолок ожидания (как у actor/shot-потоков)
_OK_STATUSES = ("succeeded", "success", "completed", "done")
_FAIL_STATUSES = ("failed", "error", "cancelled")


class GeneratorImageThread(QThread):
    """Одна картинка через FastGen v5. Сигналы: progress(str)/finished(path)/error(str)."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # абсолютный путь к сохранённому .jpg
    error = pyqtSignal(str)

    def __init__(self, prompt: str, aspect_ratio: str, model_id: str,
                 out_dir: Path, refs=None, parent=None):
        super().__init__(parent)
        self.prompt = (prompt or "").strip()
        self.aspect_ratio = aspect_ratio or "16:9"
        self.model_id = model_id
        self.out_dir = Path(out_dir)
        # Прикреплённые рефы (per-генерация). Default None → конвертим в list тут
        # (избегаем mutable-default). Пусто → обычная генерация без inputs.
        self.refs: list = [str(p) for p in (refs or [])]
        self._stop = False

    def stop(self):
        self._stop = True

    def _upload(self, session: requests.Session, path: Path) -> str:
        """Загрузить файл в FastGen storage и вернуть file_hash (32-hex без
        префикса 'file:' — v5 требует голый хеш). Реюз модуль-уровневого
        _sa._upload_cache (path.resolve() → file_hash): если файл уже грузился
        (для шотов/актёров), скачка не повторяется. MIME — по магическим байтам
        (PNG-в-jpg иначе валит 422). Без ресайза (PIL не используем).
        Образец 1:1 — threads/generate.py:1180 (_upload в GenerateThread)."""
        import storyboard_app as _sa
        cache_key = str(path.resolve())
        if cache_key in _sa._upload_cache:
            return _sa._upload_cache[cache_key]
        with open(path, "rb") as f:
            head = f.read(12); f.seek(0)
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                mime = "image/png"
            elif head[:3] == b'\xff\xd8\xff':
                mime = "image/jpeg"
            elif head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                mime = "image/webp"
            else:
                ext = path.suffix.lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "webp": "image/webp"
                        }.get(ext, "image/jpeg")
            r = session.post(f"{_sa.STORAGE_BASE}/upload",
                             files={"file": (path.name, f, mime)}, timeout=60)
        r.raise_for_status()
        data = r.json()
        fh = data.get("file_hash") or data.get("file") or data.get("hash") or ""
        # v5: inputs.input требует ГОЛЫЙ 32-hex; /upload отдаёт с префиксом "file:"
        fh = fh[5:] if fh.startswith("file:") else fh
        _sa._upload_cache[cache_key] = fh
        return fh

    def _fastgen(self, op_id, status, elapsed, result, extra=""):
        """Диаг-строка [FASTGEN] (→ runtime.log через studio tee), формат как в
        threads/generate.py. provider=generator — метка пути."""
        print(f"[FASTGEN] path=GeneratorImageThread api=v5 "
              f"endpoint=/api/v5/generations auth=X-API-Key "
              f"model={self.model_id} provider=generator result_format=ref "
              f"inputs={len(self.refs)} op_id={op_id} status={status} time={elapsed} "
              f"result={result}{extra}")

    def run(self):
        import storyboard_app as _sa
        try:
            key, _idx = _sa.next_api_key()
            if not key:
                self.error.emit("Нет доступного API-ключа")
                return
            session = requests.Session()
            session.headers.update({"X-API-Key": key})

            # Upload прикреплённых рефов → file_hashes (32-hex). Degrade: ошибка
            # на одном НЕ валит всю генерацию — progress-сообщение и продолжаем
            # без него. Лимит OpenAI: режем до OPENAI_MAX_REFS (=10).
            ref_hashes = []
            if self.refs:
                for _p in self.refs:
                    try:
                        ref_hashes.append(self._upload(session, Path(_p)))
                    except Exception:
                        self.progress.emit(f"Реф не загрузился: {Path(_p).name}")
                if (self.model_id == "openai-image"
                        and len(ref_hashes) > _sa.OPENAI_MAX_REFS):
                    ref_hashes = ref_hashes[:_sa.OPENAI_MAX_REFS]

            # v5: провайдер выбирается полем model; единый эндпоинт.
            payload = {
                "prompt": self.prompt,
                "aspect_ratio": self.aspect_ratio,
                "model": self.model_id,
            }
            if ref_hashes:
                payload["inputs"] = [{"name": f"img{i+1}", "input": h}
                                     for i, h in enumerate(ref_hashes)]
            r = session.post(f"{_sa.API_BASE}/api/v5/generations",
                             params={"result_format": "ref"},
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("id")
            if not op_id:
                self.error.emit(f"Сервер не вернул id: {str(data)[:200]}")
                return

            self.progress.emit("Генерирую…")
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
                        f"оставался {elapsed}с (>5 мин). Попробуй ещё раз.")
                    return
                rr = session.get(f"{_sa.API_BASE}/api/v5/generations/{op_id}",
                                 params={"result_format": "ref"}, timeout=30)
                rr.raise_for_status()
                d = rr.json()
                status = (d.get("status") or "").lower()
                last_status = status
                self.progress.emit(f"Генерирую… ({elapsed}с · {status or '...'})")

                if status in _OK_STATUSES:
                    # v5: storage_id = results[0].metadata.storage_id; fallback v4-разбор.
                    results = d.get("results") or d.get("result") or []
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
                    self._fastgen(op_id, status, elapsed, "error",
                                  f" error={str(d.get('error') or '<none>')[:120]}")
                    self.error.emit(f"Ошибка генерации: {d.get('error')}")
                    return
                # queued / running / processing / pending — продолжаем poll

            if self._stop:
                return
            # Сохранение: shows/<slug>/generator/gen_YYYYmmdd_HHMMSS.jpg (уникально)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
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
            self.error.emit(str(e)[:300])
