# -*- coding: utf-8 -*-
"""
generator/fal_angles_thread.py — потоки fal.ai для вкладки «Камера» (2026-07-02).

FalAnglesThread: одна смена ракурса через fal-ai/qwen-image-edit-2511-multiple-angles.
Контракт (снят живой разведкой 2026-07-02):
  submit POST https://queue.fal.run/{MODEL}
         headers {"Authorization": "Key <id:secret>"}
         json {"image_urls": ["data:image/...;base64,..."],
               "horizontal_angle": 0..360, "vertical_angle": -30..90,
               "zoom": 0..10}
         → {"request_id", "status_url", "response_url"}
  poll   GET status_url → status: IN_QUEUE | IN_PROGRESS | COMPLETED
  result GET response_url → {"images":[{"url":...}], "prompt", "seed"}
Значения углов уходят КАК ЕСТЬ (float) — квантование в пресеты LoRA
происходит на сервере, клиент ничего не защёлкивает.

FalBalanceThread: живой баланс аккаунта в $ —
  GET https://rest.alpha.fal.ai/billing/user_balance → голое число ("9.65").
  Alpha-эндпоинт: может переехать; любая ошибка → сигнал error, UI кажет «—».

Образец — generator/generator_thread.py (GeneratorImageThread): сигналы
str-only (run_id вяжет caller лямбдами), кооперативный stop, data URI без
temp-файлов. storyboard_app тянется ЛЕНИВО в run() (load_fal_key) —
circular-import защита для frozen .app.

Cross-platform: requests + pathlib.Path, БЕЗ subprocess/shell — никаких
console-window нюансов Win; пути через Path.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from pathlib import Path

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from i18n import tr

FAL_MODEL = "fal-ai/qwen-image-edit-2511-multiple-angles"
FAL_QUEUE_URL = f"https://queue.fal.run/{FAL_MODEL}"
FAL_BALANCE_URL = "https://rest.alpha.fal.ai/billing/user_balance"
POLL_DELAY_SEC = 1.5
POLL_TIMEOUT_SEC = 300   # потолок ожидания генерации (реальные ~10-27с)


class FalAnglesThread(QThread):
    """Один ракурс через fal. Сигналы: progress(str)/finished(str=path)/error(str)."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)   # абсолютный путь сохранённого файла
    error = pyqtSignal(str)

    def __init__(self, image_path: Path, horizontal_angle: float,
                 vertical_angle: float, zoom: float,
                 out_dir: Path, parent=None):
        super().__init__(parent)
        self.image_path = Path(image_path)
        self.horizontal_angle = float(horizontal_angle)
        self.vertical_angle = float(vertical_angle)
        self.zoom = float(zoom)
        self.out_dir = Path(out_dir)
        self._stop = False

    def stop(self):
        """Кооперативная остановка: run() выйдет на ближайшей poll-итерации."""
        self._stop = True

    @staticmethod
    def _data_uri(path: Path) -> str:
        """Файл → base64 data URI (в памяти, без temp-файлов и upload-шага —
        fal принимает data:URI в image_urls, проверено живым тестом)."""
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "webp": "webp"}.get(ext, "png")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/{mime};base64,{b64}"

    def _diag(self, rid, status, elapsed, result, extra=""):
        """Диаг-строка [FAL] → runtime.log через studio tee (формат как
        [FASTGEN] в generator_thread.py)."""
        print(f"[FAL] path=FalAnglesThread model={FAL_MODEL} "
              f"h={self.horizontal_angle} v={self.vertical_angle} "
              f"z={self.zoom} request_id={rid} status={status} "
              f"time={elapsed} result={result}{extra}")

    def run(self):
        import storyboard_app as _sa   # ленивый: load_fal_key
        t0 = time.time()
        rid = ""
        try:
            key = _sa.load_fal_key()
            if not key:
                self.error.emit(tr('camera_fal_no_key'))
                return
            if not self.image_path.exists():
                self.error.emit(tr('camera_need_current'))
                return
            session = requests.Session()
            session.headers["Authorization"] = f"Key {key}"
            session.headers["Content-Type"] = "application/json"

            self.progress.emit(tr('camera_fal_uploading'))
            payload = {
                "image_urls": [self._data_uri(self.image_path)],
                "horizontal_angle": self.horizontal_angle,
                "vertical_angle": self.vertical_angle,
                "zoom": self.zoom,
            }
            r = session.post(FAL_QUEUE_URL, data=json.dumps(payload), timeout=60)
            r.raise_for_status()
            sub = r.json()
            rid = sub.get("request_id", "")
            status_url = sub.get("status_url") or f"{FAL_QUEUE_URL}/requests/{rid}/status"
            response_url = sub.get("response_url") or f"{FAL_QUEUE_URL}/requests/{rid}"

            self.progress.emit(tr('camera_fal_generating'))
            status = ""
            while True:
                if self._stop:
                    self._diag(rid, "stopped", int(time.time() - t0), "stop")
                    return
                if time.time() - t0 > POLL_TIMEOUT_SEC:
                    self._diag(rid, "timeout", int(time.time() - t0), "error")
                    self.error.emit(tr('camera_fal_timeout'))
                    return
                rs = session.get(status_url, timeout=30)
                status = (rs.json() or {}).get("status", "")
                if status == "COMPLETED":
                    break
                if status in ("FAILED", "ERROR"):
                    self._diag(rid, status, int(time.time() - t0), "error")
                    self.error.emit(f"fal: {status}")
                    return
                time.sleep(POLL_DELAY_SEC)

            rr = session.get(response_url, timeout=60)
            rr.raise_for_status()
            out = rr.json()
            images = out.get("images") or []
            if not images or not images[0].get("url"):
                self._diag(rid, status, int(time.time() - t0), "error",
                           " extra=no_images")
                self.error.emit(tr('camera_fal_empty'))
                return
            img_url = images[0]["url"]

            self.progress.emit(tr('camera_fal_downloading'))
            self.out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            h = int(round(self.horizontal_angle))
            v = int(round(self.vertical_angle))
            # 2026-07-03: сохраняем СРАЗУ В JPEG. Внешний Adobe/Hazel-вотчер
            # у Alex пере-кодирует свежие .png → .jpg и УДАЛЯЕТ оригинал
            # (лог: exists=False через минуту после генерации; тот же
            # призрак, что в generator — см. память project_4k_ref_break).
            # JPEG вотчер не трогает (все старые .jpg в outputs целы).
            out_path = self.out_dir / f"angle_{stamp}_h{h}_v{v}.jpg"
            rd = requests.get(img_url, timeout=120)
            rd.raise_for_status()
            import io as _io
            from PIL import Image as _Image
            with _Image.open(_io.BytesIO(rd.content)) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(str(out_path), "JPEG", quality=95, optimize=True)

            self._diag(rid, status, int(time.time() - t0), "ok",
                       f" prompt={out.get('prompt', '')!r}")
            self.finished.emit(str(out_path))
        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:200]
            except Exception:
                pass
            code = getattr(getattr(e, 'response', None), 'status_code', '?')
            self._diag(rid, f"http_{code}", int(time.time() - t0), "error",
                       f" body={body!r}")
            if code == 401:
                self.error.emit(tr('camera_fal_key_bad'))
            else:
                self.error.emit(f"fal HTTP {code}: {body}")
        except Exception as e:
            self._diag(rid, "exception", int(time.time() - t0), "error",
                       f" err={str(e)[:120]!r}")
            self.error.emit(str(e)[:300])


class FalBalanceThread(QThread):
    """Баланс fal в $: GET user_balance → balance(float). Сигналы:
    balance(float) / error(str). Alpha-эндпоинт — ошибки не критичны."""

    balance = pyqtSignal(float)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        import storyboard_app as _sa
        try:
            key = _sa.load_fal_key()
            if not key:
                self.error.emit("no key")
                return
            r = requests.get(FAL_BALANCE_URL,
                             headers={"Authorization": f"Key {key}"},
                             timeout=6)   # короткий: teardown ждёт максимум ~7с
            r.raise_for_status()
            self.balance.emit(float(r.text.strip()))
        except Exception as e:
            self.error.emit(str(e)[:200])
