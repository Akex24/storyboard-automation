# -*- coding: utf-8 -*-
"""
threads/generate.py — потоки генерации (Fast Gen API + Claude CLI).

Содержит 5 классов QThread:
    - GenerateThread          — генерация отдельного шота сториборда
    - RefGenerateThread       — регенерация / редактирование рефа (локация/объект/персонаж)
    - GenerateActorRefThread  — character-реф актёра (multi-photo → персонаж)
    - ClaudeGeometryThread    — фоновое обновление geometry через Claude CLI
    - RunEpisodeThread        — запуск Claude CLI с инструкцией на эпизод

КРУГОВОЙ ИМПОРТ — ТА ЖЕ ПРОБЛЕМА что в threads/update.py: эти треды
используют helpers и константы из storyboard_app.py, а тот импортирует
эти треды. Решение — `_AppProxy` lazy proxy: атрибут `_sa.X` резолвится
только при первом обращении (внутри `run()`), к этому моменту
storyboard_app уже полностью загружен.

История: вытащено из storyboard_app.py 2026-05-04 (шаг 2B рефакторинга).
"""

from __future__ import annotations

import re
import sys
import time
import base64
import io
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import requests
from PIL import Image


# ─── Лимит размера рефов для API ─────────────────────────────────
# Fast Gen «many-image requests» отбивает запрос если хотя бы один
# приложенный реф БОЛЬШЕ 2000px по любой стороне:
#   "An image in the conversation exceeds the dimension limit
#    for many-image requests (2000px)."
# Сама граница 2000 проходит — отбой только при >2000. Поэтому
# MAX_REF_SIDE = 2000: картинки ≤ 2000 уходят как есть (без потерь
# качества), а >2000 пережимаются до 2000 в памяти. Файл на диске
# НЕ трогается — пережатие делается в `_read_image_for_upload`.
MAX_REF_SIDE = 2000


def _read_image_for_upload(path: Path) -> tuple[bytes, str]:
    """Читает картинку с диска и при необходимости пережимает до MAX_REF_SIDE.

    Возвращает (bytes, mime). Если картинка ≤ MAX_REF_SIDE по большой
    стороне — отдаём байты без перекодирования (быстрее, без потерь).
    Иначе открываем через PIL, ресайзим LANCZOS, кодируем в JPEG q=92.
    Файл на диске не меняется.
    """
    def _detect_mime_from_bytes(raw_bytes: bytes, fallback_path: Path) -> str:
        """2026-05-07: MIME по магическим байтам, не по расширению.
        Раньше pipeline.py иногда сохранял PNG в `.jpg` файл — серверу
        приходило mismatching MIME, upload падал."""
        head = raw_bytes[:12]
        if head[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        if head[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return "image/webp"
        ext = fallback_path.suffix.lower().lstrip(".")
        return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp"
                }.get(ext, "image/jpeg")

    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= MAX_REF_SIDE:
                with open(path, "rb") as f:
                    raw = f.read()
                return raw, _detect_mime_from_bytes(raw, path)
            ratio = MAX_REF_SIDE / float(max(w, h))
            new_w = int(round(w * ratio))
            new_h = int(round(h * ratio))
            im2 = im.convert("RGB").resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im2.save(buf, format="JPEG", quality=92, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        with open(path, "rb") as f:
            raw = f.read()
        return raw, _detect_mime_from_bytes(raw, path)

from PyQt6.QtCore import QThread, pyqtSignal

# tr() для локализованных строк прогресса — i18n не имеет circular import
from i18n import tr


class _AppProxy:
    """Прокси к module storyboard_app. См. подробный docstring и объяснение
    в `threads/update.py` — там же фикс «двойной instance в PyInstaller»."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


# ─── Поток генерации шота ────────────────────────────────────────

class GenerateThread(QThread):
    progress = pyqtSignal(str)
    step     = pyqtSignal(str, int)   # (label, percent)
    finished = pyqtSignal(int)        # elapsed seconds
    error    = pyqtSignal(str)

    def __init__(self, block_name: str, panel_idx: int,
                 edit_instruction: Optional[str] = None):
        """
        Если `edit_instruction` задан — режим редактирования:
          • существующий файл шота загружается как ЕДИНСТВЕННЫЙ реф [@]img1
          • генерируется новый промпт «изменить только это, остальное оставить»
          • новая картинка пишется поверх старой
        Иначе — обычная регенерация по промпту блока + рефы локаций/персонажей.
        """
        super().__init__()
        self.block_name       = block_name
        self.panel_idx        = panel_idx
        self.edit_instruction = (edit_instruction or "").strip() or None

    def _upload_file(self, session: requests.Session, path: Path) -> str:
        """Загружает файл в Fast Gen storage, возвращает file_hash. Кеширует по resolved-path.

        Если картинка по большой стороне больше MAX_REF_SIDE (1920) —
        пережимает в памяти перед отправкой. Файл на диске не трогается.
        Так обходим лимит API «many-image requests 2000px».
        """
        cache_key = str(path.resolve())
        if cache_key in _sa._upload_cache:
            return _sa._upload_cache[cache_key]
        data_bytes, mime = _read_image_for_upload(path)
        r = session.post(f"{_sa.STORAGE_BASE}/upload",
                         files={"file": (path.name, data_bytes, mime)}, timeout=60)
        r.raise_for_status()
        data = r.json()
        fh   = data.get("file_hash") or data.get("file") or data.get("hash") or ""
        _sa._upload_cache[cache_key] = fh
        return fh

    def _build_edit_prompt(self, instruction: str) -> str:
        """Строит промпт для edit-режима: img1 = текущий шот, инструкция, всё остальное оставить."""
        return (
            "[@]img1 is the current storyboard panel — pencil sketch, "
            "black and white, vertical 9:16 format.\n\n"
            f"MODIFICATION REQUESTED: {instruction}\n\n"
            "Apply ONLY the requested modification. Keep ALL other elements "
            "EXACTLY identical to [@]img1: composition, framing, camera angle, "
            "remaining characters, their poses and expressions, lighting, "
            "background, the pencil sketch art style. Do not redraw or restyle. "
            "Output: single vertical 9:16 panel, same pencil sketch black and white style."
        )

    def run(self):
        start_time = time.time()
        try:
            key     = _sa.load_api_key()
            session = requests.Session()
            session.headers["X-API-Key"] = key

            ref_hashes: List[str] = []
            clean: str = ""

            if self.edit_instruction:
                # ── EDIT-режим ─────────────────────────────────────────────
                # Существующий файл шота → единственный реф.
                # Если файла нет — невозможно редактировать (нечего изменять).
                existing = _sa.shot_path(self.block_name, self.panel_idx)
                if not existing.exists():
                    self.error.emit(
                        f"Edit невозможен: исходного файла шота нет ({existing.name}). "
                        "Сначала сделай обычную регенерацию.")
                    return
                self.step.emit("Загружаю текущий шот…", 10)
                ref_hashes = [self._upload_file(session, existing)]
                clean = self._build_edit_prompt(self.edit_instruction)
            else:
                # ── Обычная регенерация ───────────────────────────────────
                prompt_file = _sa.PROMPTS_DIR / f"{self.block_name}.txt"
                if not prompt_file.exists():
                    self.error.emit(f"Промпт не найден: {prompt_file.name}")
                    return

                prompt_text = prompt_file.read_text(encoding="utf-8")
                refs        = _sa.parse_refs(prompt_text)
                clean       = _sa.extract_shot_prompt(prompt_text, self.panel_idx) or ""
                if not clean:
                    self.error.emit(
                        f"SHOT {self.panel_idx + 1}: панель пустая или Panel "
                        f"{self.panel_idx + 1} не найден в промпте {prompt_file.name}")
                    return

                # Умный реген: оставляем только те рефы, теги которых реально
                # упомянуты в теле этой панели. Раньше отправлялись ВСЕ рефы
                # блока (включая персонажа которого нет в кадре) — это и
                # экономически невыгодно и ломает API на «толстых» рефах.
                if refs:
                    used_tags = _sa.extract_shot_tags(prompt_text, self.panel_idx)
                    if used_tags:
                        filtered_refs = {t: refs[t] for t in refs if t in used_tags}
                        skipped = sorted(set(refs.keys()) - set(filtered_refs.keys()),
                                         key=lambda t: int(re.search(r'\d+', t).group()))
                    else:
                        # тело шота без тегов — отправляем без рефов
                        filtered_refs = {}
                        skipped = sorted(refs.keys(),
                                         key=lambda t: int(re.search(r'\d+', t).group()))
                    if skipped:
                        self.progress.emit(
                            f"Пропущены рефы (нет в шоте {self.panel_idx + 1}): "
                            + ", ".join(skipped))
                    if filtered_refs:
                        n = len(filtered_refs)
                        sorted_tags = sorted(
                            filtered_refs,
                            key=lambda t: int(re.search(r'\d+', t).group()),
                        )
                        for idx, tag in enumerate(sorted_tags):
                            ref_hashes.append(self._upload_file(session, filtered_refs[tag]))
                            pct = 5 + int((idx + 1) / n * 20)
                            self.step.emit(f"Загружаю рефы ({idx+1}/{n})…", pct)

            self.step.emit("Отправляю запрос…", 28)

            # Провайдер выбирается админом в Settings (default: NARWHAL).
            #
            # NARWHAL `/api/v4/flow/image/generate` (provider=flow):
            #   • Принимает 3-10 рефов корректно, content-фильтр мягче.
            #   • Cost=4 vs cost=1 у OpenAI — плата за multi-ref.
            #   • НЕ передавать поле `model` — иначе flow маршрутизирует
            #     запрос обратно в OpenAI с теми же policy и бажной
            #     валидацией pydantic.
            #
            # OpenAI `/api/v4/openai/image/generate` (fallback):
            #   • Cost=1 (дешевле), но валится pydantic-ошибкой на
            #     parts[2+] при 3+ reference_images — режем до 2.
            #   • Content policy блокирует огнестрел, узнаваемых людей —
            #     для криминальных/драматических сцен не работает.
            #   • Используется когда NARWHAL captcha-сервис у Fast Gen
            #     лежит и нужен fallback.
            provider = _sa.image_provider()
            payload: Dict = {
                "prompt":       clean,
                "aspect_ratio": "9:16",   # отдельный шот — портрет
            }
            if ref_hashes:
                if provider == _sa.IMAGE_PROVIDER_OPENAI and len(ref_hashes) > 2:
                    # OpenAI flow ломается на 3+ рефах. Режем до первых 2
                    # (по порядку обработки они уже упорядочены: img1=локация,
                    # img2=объект или первый персонаж — самые важные).
                    self.progress.emit(
                        f"OpenAI режет рефы до 2 (было {len(ref_hashes)})")
                    ref_hashes = ref_hashes[:2]
                payload["reference_images"] = ref_hashes

            endpoint = ("/api/v4/openai/image/generate"
                        if provider == _sa.IMAGE_PROVIDER_OPENAI
                        else "/api/v4/flow/image/generate")
            r = session.post(f"{_sa.API_BASE}{endpoint}",
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("operation_id"):
                self.error.emit(f"No operation_id: {data}")
                return

            op_id      = data["operation_id"]
            poll_count = 0
            self.step.emit("Генерирую…", 30)

            while True:
                time.sleep(4)
                r = session.get(f"{_sa.API_BASE}/api/v4/operations/{op_id}", timeout=30)
                r.raise_for_status()
                data   = r.json()
                status = data.get("status")
                poll_count += 1
                pct = min(85, 30 + int(poll_count / 20 * 55))
                self.step.emit(f"Генерирую… ({poll_count * 4}с)", pct)
                self.progress.emit(f"Статус: {status}…")

                if status == "success":
                    result = data.get("result") or []
                    uri    = result[0] if isinstance(result, list) else result
                    if isinstance(uri, dict):
                        uri = uri.get("url") or uri.get("ref") or uri.get("file_hash") or ""
                    uri = str(uri)
                    if uri.startswith("data:"):
                        _, b64 = uri.split(",", 1)
                        image_bytes = base64.b64decode(b64)
                    else:
                        fh  = uri[5:] if uri.startswith("file:") else uri
                        r2  = session.get(f"{_sa.STORAGE_BASE}/file/{fh}/raw", timeout=120)
                        r2.raise_for_status()
                        image_bytes = r2.content
                    break
                if status == "error":
                    self.error.emit(f"API error: {data.get('error')}")
                    return

            self.step.emit("Сохраняю шот…", 92)
            # Каждый шот — отдельный файл {block}_shot{N}.jpg в формате 9:16
            shot_file = _sa.shot_path(self.block_name, self.panel_idx)
            # 2026-05-07: уменьшение шотов до 384×688 (50% от исходных
            # 768×1376) для экономии места. Качества хватает для
            # storyboard-просмотра. JPEG quality 85 → ~3-4× меньше
            # вес файла. Pillow ресайз ~100-300 мс — незаметно в потоке.
            # 2026-05-07: ИСТОРИЯ ВЕРСИЙ — каждая регенерация копится
            # в _history/<basename>/vN.jpg. ShotViewerDialog показывает
            # все версии и даёт «использовать эту». Существующий шот
            # (если есть) копируется в v1 ДО перезаписи, новая версия
            # сохраняется как vN+1 + копия в основной файл.
            #
            # Если Pillow упадёт по любой причине — сохраняется оригинал
            # без истории (try/except вокруг). Касается ОБОИХ режимов:
            # обычный regen и edit-режим.
            try:
                history_dir = _sa.shot_history_dir(
                    self.block_name, self.panel_idx)
                history_dir.mkdir(parents=True, exist_ok=True)
                # Если history пуста, но shot_file существует — мигрируем
                # его в v1 чтобы не потерять предыдущую версию.
                existing_versions = _sa.list_shot_versions(history_dir)
                if not existing_versions and shot_file.exists():
                    try:
                        import shutil
                        v1_path = history_dir / "v1.jpg"
                        shutil.copy2(str(shot_file), str(v1_path))
                    except Exception:
                        pass  # миграция фейл — продолжаем с N=1
                # Новый индекс.
                next_n = _sa.next_history_index(history_dir)
                new_version_path = history_dir / f"v{next_n}.jpg"
                # Сохраняем новую картинку в history vN.jpg (resized).
                with Image.open(io.BytesIO(image_bytes)) as img:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    img_small = img.resize(
                        (384, 688), Image.Resampling.LANCZOS)
                    img_small.save(
                        str(new_version_path), 'JPEG',
                        quality=85, optimize=True)
                # Копируем history-версию в основной файл (active).
                import shutil
                shutil.copy2(str(new_version_path), str(shot_file))
                # Помечаем активную версию.
                _sa.set_active_version(history_dir, next_n)
            except Exception:
                # Fallback: если что-то пошло не так с Pillow или
                # history — пишем оригинальный файл, чтоб не потерять шот.
                try:
                    shot_file.write_bytes(image_bytes)
                except Exception:
                    pass

            elapsed = max(0, int(time.time() - start_time))
            self.step.emit("Готово!", 100)
            self.finished.emit(elapsed)

        except Exception as e:
            self.error.emit(str(e))


# ─── Поток регенерации/редактирования рефа ───────────────────────

class RefGenerateThread(QThread):
    """Регенерация / редактирование рефа (локация или объект).

    Два режима (взаимоисключающие):
      • mode='regen': перегенерация по сохранённому промпту (`<name>_prompt.txt`).
        Тот же промпт → FastGen → новая вариация. Без рефа на вход.
      • mode='edit':  edit-режим — текущая картинка как реф [@]img1 + инструкция.
        Используется для точечных правок («убери стулья», «сделай темнее»).

    Перезаписывает существующий файл картинки. Эмитит progress/finished/error.
    """
    progress = pyqtSignal(str)
    step     = pyqtSignal(str, int)   # (label, percent)
    finished = pyqtSignal(int)        # elapsed seconds
    error    = pyqtSignal(str)

    def __init__(self, image_path: Path, mode: str,
                 instruction: Optional[str] = None):
        super().__init__()
        self.image_path  = image_path
        self.mode        = mode  # 'regen' | 'edit'
        self.instruction = (instruction or "").strip() or None

    def _upload(self, session: requests.Session, path: Path) -> str:
        cache_key = str(path.resolve())
        if cache_key in _sa._upload_cache:
            return _sa._upload_cache[cache_key]
        # 2026-05-07: MIME определяется ПО МАГИЧЕСКИМ БАЙТАМ файла, а не
        # по расширению. Раньше pipeline.py иногда сохранял PNG-контент в
        # `.jpg` файл (из-за пустого Content-Type заголовка от API). При
        # upload'е сервер видел `image/jpeg` MIME + PNG bytes → отклонял
        # → Edit рефа падал тихо.
        with open(path, "rb") as f:
            head = f.read(12)
            f.seek(0)
            if head[:8] == b'\x89PNG\r\n\x1a\n':
                mime = "image/png"
            elif head[:3] == b'\xff\xd8\xff':
                mime = "image/jpeg"
            elif head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                mime = "image/webp"
            else:
                # fallback по расширению (для нестандартных форматов)
                ext = path.suffix.lower().lstrip(".")
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "webp": "image/webp"
                        }.get(ext, "image/jpeg")
            r = session.post(f"{_sa.STORAGE_BASE}/upload",
                             files={"file": (path.name, f, mime)}, timeout=60)
        r.raise_for_status()
        data = r.json()
        fh   = data.get("file_hash") or data.get("file") or data.get("hash") or ""
        _sa._upload_cache[cache_key] = fh
        return fh

    def run(self):
        start_time = time.time()
        try:
            key     = _sa.load_api_key()
            session = requests.Session()
            session.headers["X-API-Key"] = key

            ref_hashes: List[str] = []
            prompt_text: str      = ""

            if self.mode == "regen":
                # Читаем сохранённый промпт. Без него регенерировать нечего.
                pf = _sa.ref_prompt_path(self.image_path)
                if not pf.exists():
                    self.error.emit(
                        f"Нет файла промпта: {pf.name}. Сначала сгенерируй "
                        f"локацию через ассистента в чате (он создаст промпт).")
                    return
                prompt_text = pf.read_text(encoding="utf-8").strip()
                if not prompt_text:
                    self.error.emit(f"Файл промпта пустой: {pf.name}")
                    return
                self.step.emit("Отправляю запрос…", 20)

            elif self.mode == "edit":
                if not self.instruction:
                    self.error.emit("Edit без инструкции — нечего применять")
                    return
                if not self.image_path.exists():
                    self.error.emit(
                        f"Нет исходной картинки: {self.image_path.name}")
                    return
                self.step.emit("Загружаю текущую картинку…", 10)
                ref_hashes = [self._upload(session, self.image_path)]
                # Edit-промпт для локации (и других «полных» рефов): сохранить
                # композицию/стиль, изменить ТОЛЬКО запрошенное.
                prompt_text = (
                    "[@]img1 is the current reference image (location / object). "
                    f"MODIFICATION REQUESTED: {self.instruction}\n\n"
                    "Apply ONLY the requested modification. Keep ALL other "
                    "elements EXACTLY identical to [@]img1: composition, framing, "
                    "lighting, perspective, background, art style. Do NOT redraw "
                    "or restyle. Same aspect ratio as [@]img1."
                )
            else:
                self.error.emit(f"Unknown mode: {self.mode}")
                return

            # 2026-05-07: Edit-режим использует ВЫБОР ПРОВАЙДЕРА (как
            # GenerateThread у шотов). Раньше хардкодом OpenAI — но он
            # ломается pydantic-ошибкой на reference_images. NARWHAL
            # (`/api/v4/flow/image/generate`) корректно принимает refs.
            # Default провайдер — NARWHAL (см. storyboard_app.image_provider).
            # Локации/объекты — 16:9 landscape. Поля `model`/`resolution`
            # не передаём (NARWHAL flow маршрутизирует обратно в OpenAI
            # если есть `model` → ломается тот же pydantic).
            provider = _sa.image_provider()
            payload: Dict = {
                "prompt":       prompt_text,
                "aspect_ratio": "16:9",
            }
            if ref_hashes:
                if (provider == _sa.IMAGE_PROVIDER_OPENAI
                        and len(ref_hashes) > 2):
                    # OpenAI режет до 2 рефов (тот же баг что в GenerateThread)
                    ref_hashes = ref_hashes[:2]
                payload["reference_images"] = ref_hashes

            endpoint = ("/api/v4/openai/image/generate"
                        if provider == _sa.IMAGE_PROVIDER_OPENAI
                        else "/api/v4/flow/image/generate")
            r = session.post(f"{_sa.API_BASE}{endpoint}",
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("operation_id"):
                self.error.emit(f"No operation_id: {data}")
                return

            op_id      = data["operation_id"]
            poll_count = 0
            self.step.emit("Генерирую…", 30)

            while True:
                time.sleep(4)
                r = session.get(f"{_sa.API_BASE}/api/v4/operations/{op_id}", timeout=30)
                r.raise_for_status()
                data   = r.json()
                status = data.get("status")
                poll_count += 1
                pct = min(85, 30 + int(poll_count / 20 * 55))
                self.step.emit(f"Генерирую… ({poll_count * 4}с)", pct)
                self.progress.emit(f"Статус: {status}…")

                if status == "success":
                    result = data.get("result") or []
                    uri    = result[0] if isinstance(result, list) else result
                    if isinstance(uri, dict):
                        uri = uri.get("url") or uri.get("ref") or uri.get("file_hash") or ""
                    uri = str(uri)
                    if uri.startswith("data:"):
                        _, b64 = uri.split(",", 1)
                        image_bytes = base64.b64decode(b64)
                    else:
                        fh  = uri[5:] if uri.startswith("file:") else uri
                        r2  = session.get(f"{_sa.STORAGE_BASE}/file/{fh}/raw", timeout=120)
                        r2.raise_for_status()
                        image_bytes = r2.content
                    break
                if status == "error":
                    self.error.emit(f"API error: {data.get('error')}")
                    return

            self.step.emit("Сохраняю…", 92)
            # Перезаписываем картинку поверх старой
            self.image_path.write_bytes(image_bytes)
            # Кеш upload — устаревает, удаляем чтобы при следующем edit'е
            # уже залилась новая версия картинки, а не старый хеш
            _sa._upload_cache.pop(str(self.image_path.resolve()), None)

            elapsed = max(0, int(time.time() - start_time))
            self.step.emit("Готово!", 100)
            self.finished.emit(elapsed)

        except Exception as e:
            self.error.emit(str(e))


# ─── Поток обновления geometry через Claude CLI ──────────────────

class ClaudeGeometryThread(QThread):
    """Фоновый поток: дёргает Claude Code CLI с командой
    `обнови geometry для <name>`. CLI читает картинку рефа, переписывает
    `<name>_geometry.txt` и сам выходит.

    Использует ту же подписку Claude Max что и интерактивный Claude Code —
    отдельных денег не требует. Окно терминала не разворачивается:
    subprocess запускается detached, без stdin/stdout-привязки к UI.

    Сигналы:
      finished(image_path) — успех (geometry-файл переписан)
      error(image_path, msg) — ошибка (CLI не найден / упал / timeout)
    """
    finished = pyqtSignal(object)        # Path
    error    = pyqtSignal(object, str)   # Path, msg

    def __init__(self, image_path: Path, project_root: Path,
                 ref_name: str, lang_phrase: str):
        super().__init__()
        self.image_path   = image_path
        self.project_root = project_root
        self.ref_name     = ref_name
        # Готовая фраза на языке UI (`обнови geometry для X` / `update geometry…`)
        self.lang_phrase  = lang_phrase

    def run(self):
        cli = _sa.find_claude_cli()
        if not cli:
            self.error.emit(self.image_path, "claude_cli_not_found")
            return
        try:
            # cwd = project_root (там CLAUDE.md с инструкцией про обновление
            # geometry, прочтёт автоматически)
            # --dangerously-skip-permissions: разрешает Read/Write без
            # подтверждения (в headless mode иначе зависнет на permission-prompt)
            # 2026-05-08: CREATE_NO_WINDOW guard для Win10/11.
            run_kwargs = dict(
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=300,  # 5 минут — больше чем нужно, защита от зависа
            )
            if sys.platform == 'win32':
                run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            proc = subprocess.run(
                [cli, "-p", self.lang_phrase,
                 "--dangerously-skip-permissions"],
                **run_kwargs,
            )
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "exit != 0").strip()[:500]
                self.error.emit(self.image_path, msg)
                return
            self.finished.emit(self.image_path)
        except subprocess.TimeoutExpired:
            self.error.emit(self.image_path, "timeout (>5 min)")
        except Exception as e:
            self.error.emit(self.image_path, str(e))


# ─── Поток запуска эпизода через Claude CLI ──────────────────────

class RunEpisodeThread(QThread):
    """Запускает Claude Code CLI с инструкцией поработать над эпизодом.

    На вход:
      • project_root — рабочая директория (где CLAUDE.md)
      • prompt — текст что отдать Claude (например «Прочти scenarios/_active.txt
        и работай по CLAUDE.md, мы делаем эпизод 15»)

    Сигналы:
      • output_chunk(text) — кусок stdout (для стрима в UI)
      • finished_ok(returncode)
      • error(msg)
      • stopped() — после явной остановки

    Использует общий `find_claude_cli` и `--dangerously-skip-permissions`
    (как ClaudeGeometryThread)."""
    output_chunk = pyqtSignal(str)
    finished_ok  = pyqtSignal(int)
    error        = pyqtSignal(str)
    stopped      = pyqtSignal()
    # 2026-05-08 TODO 6: индикатор «долго думаю». Эмитится один раз через
    # 120с после старта если за это время не пришёл ни один chunk. Слот
    # на стороне view добавляет системную строку в чат с подсказкой
    # «не закрывай Studio» — чтобы юзер не подумал что зависло.
    slow_thinking = pyqtSignal()

    def __init__(self, project_root: Path, prompt: str,
                 continue_session: bool = False, model: Optional[str] = None):
        super().__init__()
        self.project_root = project_root
        self.prompt = prompt
        self.continue_session = continue_session
        # Полный model-id для флага `--model` (claude-sonnet-4-6 и т.п.)
        # Если None — Claude CLI использует свой default.
        self.model = model
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False
        self._first_chunk_seen = False

    def stop(self):
        """Просим тред остановиться. Убиваем subprocess если он жив."""
        self._stop_requested = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self):
        cli = _sa.find_claude_cli()
        if not cli:
            self.error.emit("claude_cli_not_found")
            return
        try:
            args = [cli]
            if self.continue_session:
                # `--continue` — продолжить последнюю беседу в этой cwd.
                # Claude CLI хранит историю по hash от cwd, так что если
                # юзер не менял проектную папку — подхватит свою же сессию.
                args.append("--continue")
            if self.model:
                args += ["--model", self.model]
            args += ["-p", self.prompt, "--dangerously-skip-permissions"]
            # 2026-05-08: CREATE_NO_WINDOW guard для Win10/11.
            popen_kwargs = dict(
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if sys.platform == 'win32':
                popen_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(args, **popen_kwargs)
            assert self._proc.stdout is not None
            # 2026-05-08 TODO 6: watchdog в отдельном threading.Timer.
            # Через 120с без первого chunk → emit slow_thinking. Сигнал
            # Qt thread-safe для cross-thread emit; UI-слот добавит
            # подсказку в чат. Если chunk пришёл раньше — таймер cancel.
            import threading as _threading
            def _watchdog():
                if not self._first_chunk_seen and not self._stop_requested:
                    try:
                        self.slow_thinking.emit()
                    except Exception:
                        pass
            slow_timer = _threading.Timer(120.0, _watchdog)
            slow_timer.daemon = True
            slow_timer.start()
            try:
                for line in self._proc.stdout:
                    if self._stop_requested:
                        break
                    if line:
                        if not self._first_chunk_seen:
                            self._first_chunk_seen = True
                            slow_timer.cancel()
                        self.output_chunk.emit(line)
            finally:
                slow_timer.cancel()
            rc = self._proc.wait(timeout=10)
            if self._stop_requested:
                self.stopped.emit()
                return
            self.finished_ok.emit(rc)
        except Exception as e:
            if self._stop_requested:
                self.stopped.emit()
                return
            self.error.emit(str(e)[:500])


# ─── Поток генерации character-рефа актёра ───────────────────────

class GenerateActorRefThread(QThread):
    """Фоновая генерация character-рефа через Fast Gen (NARWHAL).
    Берёт ВСЕ фото актёра как multi-reference, отправляет промпт
    выбранного варианта с подставленным описанием юзера.

    Результат сохраняется в `target_dir/<filename>.jpg`. Caller сам строит
    `target_dir` — обычно это `shows/<show>/refs/characters/<character>/`.
    `actor_slug` — идентификатор актёра-источника (для прогресс-трекинга
    в ActorsView), к пути не относится."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)        # путь к сохранённому файлу
    error = pyqtSignal(str)

    def __init__(self, actor_slug: str, target_dir: Path,
                 photo_paths: List[Path],
                 prompt_text: str, output_filename: str, parent=None):
        super().__init__(parent)
        self.actor_slug = actor_slug
        self.target_dir = Path(target_dir)
        self.photo_paths = list(photo_paths)
        self.prompt_text = prompt_text
        self.output_filename = output_filename  # без .jpg

    def run(self):
        try:
            key = _sa.load_api_key()
            if not key:
                self.error.emit(tr('create_ref_no_api_key'))
                return
            session = requests.Session()
            session.headers.update({"X-API-Key": key})

            # Загружаем все фотки актёра как референсы (до 10 — ограничение API)
            self.progress.emit(tr('create_ref_uploading',
                                  n=min(len(self.photo_paths), 10)))
            ref_hashes = []
            for p in self.photo_paths[:10]:
                ext = p.suffix.lower().lstrip(".")
                mime = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                        "png":"image/png","webp":"image/webp"}.get(
                            ext, "image/jpeg")
                with open(p, "rb") as f:
                    r = session.post(f"{_sa.STORAGE_BASE}/upload",
                                     files={"file": (p.name, f, mime)},
                                     timeout=60)
                r.raise_for_status()
                data = r.json()
                fh = (data.get("file_hash") or data.get("file")
                      or data.get("hash"))
                if not fh:
                    raise RuntimeError(f"upload missing hash: {data}")
                ref_hashes.append(fh)

            # Целевая директория — задана caller'ом (обычно
            # shows/<show>/refs/characters/<character>/). Создаём если нет.
            target_dir = self.target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{self.output_filename}.jpg"
            # Если уже существует — добавляем суффикс _2, _3 чтобы не перезаписать
            if target.exists():
                i = 2
                while (target_dir / f"{self.output_filename}_{i}.jpg").exists():
                    i += 1
                target = target_dir / f"{self.output_filename}_{i}.jpg"

            self.progress.emit(tr('create_ref_generating'))
            # Phase 2 hotfix #20: переключение на OpenAI flow (cost=1 vs 4).
            # Identity reference sheet с 14 анатомическими деталями. OpenAI
            # выдаёт ~1254 на 1:1 или ~1456×819 на 16:9 — для 14 панелей
            # каждая ~360×220px, чуть лучше чем NARWHAL. Параметра 2K
            # в API нет — endpoint всегда возвращает фиксированный размер.
            payload = {
                "prompt": self.prompt_text,
                "aspect_ratio": "16:9",
                "reference_images": ref_hashes,
            }
            r = session.post(f"{_sa.API_BASE}/api/v4/openai/image/generate",
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("operation_id")
            if not op_id:
                self.error.emit(f"No operation_id: {data}")
                return

            # Polling
            while True:
                time.sleep(4)
                r = session.get(f"{_sa.API_BASE}/api/v4/operations/{op_id}",
                                timeout=30)
                r.raise_for_status()
                d = r.json()
                status = d.get("status")
                if status == "success":
                    result = d.get("result") or []
                    uri = result[0] if isinstance(result, list) else result
                    if isinstance(uri, dict):
                        uri = (uri.get("url") or uri.get("ref")
                               or uri.get("file_hash") or "")
                    uri = str(uri)
                    if uri.startswith("data:"):
                        _, b64 = uri.split(",", 1)
                        image_bytes = base64.b64decode(b64)
                    else:
                        fh = uri[5:] if uri.startswith("file:") else uri
                        r2 = session.get(f"{_sa.STORAGE_BASE}/file/{fh}/raw",
                                         timeout=120)
                        r2.raise_for_status()
                        image_bytes = r2.content
                    target.write_bytes(image_bytes)
                    self.finished.emit(str(target))
                    return
                if status == "error":
                    self.error.emit(f"API error: {d.get('error')}")
                    return

        except Exception as e:
            self.error.emit(str(e))
