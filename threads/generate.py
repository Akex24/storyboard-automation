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
        """Загружает файл в Fast Gen storage, возвращает file_hash. Кеширует
        по (resolved-path, mtime_ns).

        2026-05-19: cache_key переведён с одиночного `str(path.resolve())`
        на кортеж `(resolved, mtime_ns)`. Старый ключ не учитывал содержимое
        файла — после `shutil.copy2(vN, public)` (как в GenerateThread save
        path и в "Использовать эту") public-файл менял content, но cache
        возвращал hash от ПРЕДЫДУЩЕГО content'а. Edit-mode payload улетал
        с устаревшим `[@]img0` → Nano Banana работала с СТАРОЙ базой,
        результаты всех последовательных edit'ов выглядели визуально
        идентично (ep25_block_5_shot2 v5..v8 — 4 «копии» одной и той же
        генерации). Добавление mtime_ns в ключ автоматически инвалидирует
        кэш при любой записи в файл — нужны явные `pop` нигде не нужны.

        Если картинка по большой стороне больше MAX_REF_SIDE (1920) —
        пережимает в памяти перед отправкой. Файл на диске не трогается.
        Так обходим лимит API «many-image requests 2000px».
        """
        resolved = str(path.resolve())
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            # Файл исчез между resolve() и stat() — гонка либо удаление.
            # 0 как mtime обеспечит cache miss (никакой реальный mtime != 0).
            mtime_ns = 0
        cache_key = (resolved, mtime_ns)
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

    def _build_edit_prompt(self, instruction: str,
                            source_prompt: str,
                            filtered_refs: Dict,
                            sorted_tags: List[str]) -> str:
        """Строит промпт для edit-режима.

        2026-05-19: edit-mode теперь прикрепляет к payload ВСЕ рефы шота
        (актёры/объекты/локация) — те же что в regen-режиме. Без них
        Nano Banana не знала кто такой «Arthur» в инструкции и рисовала
        случайное лицо. Текущий сториборд кладётся ПЕРВЫМ ref'ом
        с тегом [@]img0 (вне диапазона source-шапки img1..imgN).

        Параметры:
          • instruction — текст пользователя «что изменить».
          • source_prompt — текст output/prompts/<block>.txt целиком
            (нужен чтобы извлечь имена персонажей из CHARACTERS-блока
            вида «[@]img5 Arthur — wearing ...»).
          • filtered_refs — {tag: Path} рефов выбранных _collect_shot_refs
            для этого шота (только те теги что упомянуты в Panel N).
          • sorted_tags — порядок тегов в payload['reference_images']
            ПОСЛЕ [@]img0 (текущий шот = индекс 0).
        """
        # БАГ 8 — раньше regex `[@]imgN <Word>` бил по ВСЕМУ source-prompt'у
        # и хватал случайное слово после тега в теле панели (например
        # `windows of [@]img1 on the right` → label «on»). Чинится строгим
        # ограничением поиска CHARACTERS-блоком: от маркера `CHARACTERS:`
        # до пустой строки или начала первой `Panel N`. Внутри блока
        # каждая строка имеет вид «[@]imgN <Name> — wearing EXACT SAME...».
        # Если тега в CHARACTERS нет (это локация / объект / storyboard) —
        # fallback на `Path.stem` файла («wheelchair», «living_room_old_…»).
        char_block = ""
        m_block = re.search(
            r'CHARACTERS:\s*\n(.+?)(?:\n\s*\n|Panel\s+\d)',
            source_prompt, re.DOTALL)
        if m_block:
            char_block = m_block.group(1)
        legend_lines = []
        for tag in sorted_tags:
            p = filtered_refs.get(tag)
            if p is None:
                continue
            n = tag.split("img")[-1]
            label = None
            if char_block:
                nm = re.search(
                    rf'\[@\]img{n}\s+([A-Za-z][\w-]*)', char_block)
                if nm:
                    label = nm.group(1)
            if label is None:
                label = p.stem  # для локации / объектов
            legend_lines.append(f"{tag} = {label}")
        legend = ("\n".join(legend_lines) if legend_lines
                  else "(no additional references for this shot)")
        # 2026-05-19: смягчение edit-prompt'а. Прошлая версия содержала
        # «Apply ONLY the requested modification. Keep ALL other elements
        # EXACTLY identical to [@]img0: ... poses and expressions, ...».
        # Это блокировало позиционные правки: при инструкции «Анна и Макс
        # пересядьте с пола на диван» модель видела противоречие (инструкция
        # требует смены поз, но «Keep poses identical» запрещает) и
        # возвращала картинку почти 1:1 (с шумом, но без перестановки).
        # Новая версия явно РАЗРЕШАЕТ перестановки персонажей и оставляет
        # защищёнными только то что должно быть одинаковым ВСЕГДА:
        # художественный стиль (pencil sketch), формат (9:16), сеттинг.
        return (
            "[@]img0 is the CURRENT storyboard panel — pencil sketch, "
            "black and white, vertical 9:16 format. Use it as the BASE "
            "for modification.\n\n"
            "REFERENCE LEGEND (use these faces/objects when modifying):\n"
            f"{legend}\n\n"
            f"MODIFICATION REQUESTED: {instruction}\n\n"
            "Apply the requested modification to [@]img0. Characters CAN "
            "change positions, poses, and locations within the scene if "
            "the instruction requires it (for example: sitting/standing/"
            "walking, moving from floor to sofa, swapping places). When "
            "modifying:\n"
            "- Match character faces to their reference images "
            "([@]img5, [@]img6, etc.) by name in the legend above\n"
            "- Keep the same art style (pencil sketch, black and white, "
            "clear outlines)\n"
            "- Keep the same vertical 9:16 format\n"
            "- Use the same setting/location ([@]img1) unless instructed "
            "otherwise\n"
            "Output: single vertical 9:16 panel, same pencil sketch "
            "black and white style as [@]img0."
        )

    def _collect_shot_refs(
        self, session: requests.Session,
        apply_panel_filter: bool = True,
    ) -> tuple:
        """Собирает рефы для одного шота (общий хелпер для regen и edit).

        2026-05-19: вынесено из inline-логики regen-ветки чтобы edit-mode
        мог использовать ту же логику парсинга шапки. Без этого reuse'а
        edit-mode не получал бы ни одного фото актёра (Nano Banana
        рисовала случайные лица вместо Arthur'а).

        Параметр `apply_panel_filter` (default True — текущее regen-поведение):
          • True  — фильтр по тегам упомянутым в теле Panel N
            (умный regen: загружаем только то что реально в кадре).
          • False — берём ВСЕ рефы из шапки source-prompt'а без фильтра.
            Используется в edit-mode: юзер может в инструкции упомянуть
            ЛЮБОГО актёра из CHARACTERS-блока (даже того кого нет в этой
            конкретной панели), и его реф должен быть в payload.

        Читает output/prompts/<block>.txt, парсит шапку «# [@]imgN = file»,
        вычленяет тело Panel N + (опционально) фильтрует рефы по тегам
        упомянутым в этом шоте, загружает в Fast Gen storage в порядке
        возрастания N.

        Возвращает (ref_hashes, filtered_refs, sorted_tags, clean_body,
                    prompt_text):
          • ref_hashes — list[str] file_hash в порядке sorted_tags.
          • filtered_refs — {tag: Path}. При apply_panel_filter=False
            это все рефы шапки; при True — только использованные в Panel N.
          • sorted_tags — список тегов в порядке возрастания imgN.
          • clean_body — тело Panel N как готовый prompt без шапки.
          • prompt_text — оригинальный source-prompt (для извлечения имён).
        Если prompts/<block>.txt отсутствует — всё пустое (caller сам
        решает что делать с пустым clean_body).
        """
        prompt_file = _sa.PROMPTS_DIR / f"{self.block_name}.txt"
        if not prompt_file.exists():
            return ([], {}, [], "", "")
        prompt_text = prompt_file.read_text(encoding="utf-8")
        refs = _sa.parse_refs(prompt_text)
        # 2026-05-19: для character-тегов перепроверяем актуальный filename
        # в episodes.json.refs_decisions, который обновляется при смене
        # актёра в UI. Шапка prompts/*.txt генерится PromptWriter'ом единожды
        # и не перезаписывается → stale character filename. Без override'а
        # payload улетал с устаревшим фото актёра (см. диагностику
        # ep25_block_1_shot1: Arthur трижды переключался, payload улетал
        # с первым ref-файлом). Override НЕ трогает location/object refs.
        try:
            refs = self._override_character_refs(refs)
        except Exception:
            pass  # fallback на шапку при любой ошибке (старое поведение)
        clean_body = _sa.extract_shot_prompt(prompt_text, self.panel_idx) or ""
        filtered_refs: Dict = {}
        sorted_tags: List[str] = []
        ref_hashes: List[str] = []
        if refs:
            if apply_panel_filter:
                used_tags = _sa.extract_shot_tags(
                    prompt_text, self.panel_idx)
                if used_tags:
                    filtered_refs = {
                        t: refs[t] for t in refs if t in used_tags}
            else:
                # edit-mode: все рефы из шапки, без фильтрации.
                filtered_refs = dict(refs)
            skipped = sorted(set(refs.keys()) - set(filtered_refs.keys()),
                             key=lambda t: int(re.search(r'\d+', t).group()))
            if skipped:
                self.progress.emit(
                    f"Пропущены рефы (нет в шоте {self.panel_idx + 1}): "
                    + ", ".join(skipped))
            if filtered_refs:
                sorted_tags = sorted(
                    filtered_refs,
                    key=lambda t: int(re.search(r'\d+', t).group()),
                )
                n = len(sorted_tags)
                for idx, tag in enumerate(sorted_tags):
                    ref_hashes.append(
                        self._upload_file(session, filtered_refs[tag]))
                    pct = 5 + int((idx + 1) / n * 20)
                    self.step.emit(f"Загружаю рефы ({idx+1}/{n})…", pct)
        return (ref_hashes, filtered_refs, sorted_tags, clean_body, prompt_text)

    def _override_character_refs(self, refs: Dict[str, Path]) -> Dict[str, Path]:
        """Для character-рефов подменяет Path из шапки prompt-файла на
        актуальный из episodes.json.refs_decisions.

        Контракт episodes.json:
          refs_decisions.character[<slug>] = {
              "decision": "linked",
              "filename": "<slug>/<file>.jpg"   # относительный путь
                                                # от refs/characters/
          }

        Алгоритм:
          1. Извлекаем ep_id из self.block_name (regex 'ep\\d+').
          2. Читаем episodes.json текущего шоу через _sa.SHOW_ROOT.
          3. Для каждого (tag, path) в refs:
             - Если path не внутри CHARACTERS_DIR — пропускаем (location/object).
             - slug = path.parent.name (для refs/characters/arthur/*.jpg → 'arthur').
             - Берём refs_decisions.character[slug]['filename'] → basename.
             - Резолвим через _sa.find_ref_image() → новый Path.
             - Если найден и отличается от текущего — overrideим refs[tag],
               эмитим progress сообщение для диагностики.

        Fallback на любую ошибку (нет ep_id, нет JSON, нет slug, нет file
        на диске) — возвращаем refs без изменений. Не валит pipeline.

        Не трогает: locations, objects, character'ы вне CHARACTERS_DIR,
        character'ы без записи в refs_decisions, character'ы с
        decision != 'linked', character'ы где basename из refs_decisions
        совпадает с тем что в шапке (override не нужен).
        """
        import json
        m = re.match(r'^(ep\d+)_', self.block_name)
        if not m:
            return refs
        ep_id = m.group(1)
        episodes_json = _sa.SHOW_ROOT / "episodes.json"
        if not episodes_json.exists():
            return refs
        try:
            data = json.loads(episodes_json.read_text(encoding="utf-8"))
        except Exception:
            return refs
        ep = data.get(ep_id) if isinstance(data, dict) else None
        if not isinstance(ep, dict):
            return refs
        decisions = ep.get("refs_decisions")
        if not isinstance(decisions, dict):
            return refs
        char_decisions = decisions.get("character")
        if not isinstance(char_decisions, dict):
            return refs

        chars_dir = _sa.CHARACTERS_DIR
        try:
            chars_dir_resolved = chars_dir.resolve()
        except Exception:
            return refs

        out = dict(refs)  # копия чтобы не мутировать вход
        for tag, path in list(refs.items()):
            try:
                p_resolved = path.resolve()
            except Exception:
                continue
            # Является ли path character-рефом? (внутри CHARACTERS_DIR)
            try:
                p_resolved.relative_to(chars_dir_resolved)
            except ValueError:
                continue  # location/object — пропускаем
            slug = path.parent.name
            d = char_decisions.get(slug)
            if not isinstance(d, dict) or d.get("decision") != "linked":
                continue
            filename = d.get("filename") or ""
            if not filename:
                continue
            # filename из refs_decisions — относительный путь вида
            # "<slug>/<file>.jpg". Резолвим через find_ref_image по basename.
            basename = Path(filename).name
            new_path = _sa.find_ref_image(basename)
            if new_path is None:
                continue
            try:
                if new_path.resolve() == p_resolved:
                    continue  # уже актуальный — override не нужен
            except Exception:
                pass
            out[tag] = new_path
            try:
                self.progress.emit(
                    f"override {tag} {slug}: "
                    f"{path.name} → {new_path.name}")
            except Exception:
                pass
        return out

    def run(self):
        start_time = time.time()
        try:
            key     = _sa.load_api_key()
            session = requests.Session()
            session.headers["X-API-Key"] = key

            ref_hashes: List[str] = []
            clean: str = ""
            # Всегда определены к моменту dump-блока (см. ниже) — даже
            # если рефов нет, регенерация прошла без шапки или edit-mode
            # запущен без source-prompt'а.
            filtered_refs: Dict = {}
            sorted_tags: List[str] = []
            existing_path = None  # путь к текущему сториборду в edit-mode

            if self.edit_instruction:
                # ── EDIT-режим ─────────────────────────────────────────────
                # Текущий файл шота — БАЗА ([@]img0). К нему теперь добавляем
                # ВСЕ рефы шота (актёры/объекты/локация) — те же что в regen.
                # Без рефов актёров Nano Banana не знала кто такой «Arthur»
                # в инструкции «put Arthur in the wheelchair» и рисовала
                # случайное лицо. См. _build_edit_prompt и _collect_shot_refs.
                existing = _sa.shot_path(self.block_name, self.panel_idx)
                if not existing.exists():
                    self.error.emit(
                        f"Edit невозможен: исходного файла шота нет ({existing.name}). "
                        "Сначала сделай обычную регенерацию.")
                    return
                existing_path = existing
                self.step.emit("Загружаю текущий шот…", 10)
                base_hash = self._upload_file(session, existing)
                # ВСЕ рефы из шапки source-prompt'а (без Panel-фильтрации).
                # Юзер в инструкции может упомянуть любого актёра из
                # CHARACTERS — даже того кто не активен в этой конкретной
                # панели (например «охранник пусть встанет за диваном»
                # в Panel 1 где он не упоминался) — его реф нужен в payload.
                (shot_hashes, filtered_refs, sorted_tags, _body, prompt_text
                 ) = self._collect_shot_refs(
                    session, apply_panel_filter=False)
                # Текущий шот ПЕРВЫЙ → ему достанется [@]img0 в payload;
                # затем рефы по возрастанию N из source-шапки.
                ref_hashes = [base_hash] + shot_hashes
                clean = self._build_edit_prompt(
                    self.edit_instruction, prompt_text,
                    filtered_refs, sorted_tags)
            else:
                # ── Обычная регенерация ───────────────────────────────────
                prompt_file = _sa.PROMPTS_DIR / f"{self.block_name}.txt"
                if not prompt_file.exists():
                    self.error.emit(f"Промпт не найден: {prompt_file.name}")
                    return
                (ref_hashes, filtered_refs, sorted_tags, clean, _src
                 ) = self._collect_shot_refs(session)
                if not clean:
                    self.error.emit(
                        f"SHOT {self.panel_idx + 1}: панель пустая или Panel "
                        f"{self.panel_idx + 1} не найден в промпте {prompt_file.name}")
                    return

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
            # 2026-05-23: разделение провайдеров — шоты идут через
            # админский переключатель (`image_provider_admin`).
            provider = _sa.image_provider_admin()
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

            # ── 2026-05-15 DIAG DUMP (временно, для расследования) ────
            # Дамп ровно того payload что улетел в Fast Gen. Контекст:
            # юзер видит подмену персонажа в shot'е (ep23/block3/shot3
            # — нарисовало David вместо Mark). Нужно сравнить реально
            # отправленный prompt + порядок ref_hashes с ожидаемым.
            # Если данные верные — баг внутри модели Fast Gen.
            try:
                import datetime
                dump_path = (_sa.STORYBOARDS_DIR
                             / f"_payload_dump_{self.block_name}"
                               f"_shot{self.panel_idx + 1}.txt")
                # В edit-mode reference_images[0] — текущий сториборд
                # (тег [@]img0), далее идут рефы шота. В regen — только
                # рефы шота. filtered_refs/sorted_tags теперь ВСЕГДА
                # определены (см. инициализацию выше).
                pairs = []
                if self.edit_instruction:
                    pairs.append(("[@]img0", existing_path))
                for tag in sorted_tags:
                    pairs.append((tag, filtered_refs.get(tag)))
                lines = [
                    f"=== PAYLOAD DUMP {datetime.datetime.now().isoformat()} ===",
                    f"Block: {self.block_name}",
                    f"Shot:  {self.panel_idx + 1} (panel_idx={self.panel_idx})",
                    f"Mode:  {'edit' if self.edit_instruction else 'regen'}",
                    f"Provider: {provider}",
                    f"Endpoint: {_sa.API_BASE}{endpoint}",
                    f"Aspect ratio: {payload.get('aspect_ratio')}",
                    "",
                    "=== payload['reference_images'] (порядок!) ===",
                ]
                for i, (tag, src_path) in enumerate(pairs):
                    fh = ref_hashes[i] if i < len(ref_hashes) else "<missing>"
                    size = (src_path.stat().st_size
                            if src_path and src_path.exists() else -1)
                    lines.append(
                        f"  [{i}] tag={tag:<12} hash={fh}  "
                        f"size={size}  path={src_path}")
                lines += [
                    "",
                    "=== payload['prompt'] (полный текст что улетел) ===",
                    clean,
                    "",
                    "=== /DUMP ===",
                ]
                dump_path.write_text("\n".join(lines), encoding="utf-8")
                self.progress.emit(f"[DIAG] dump → {dump_path.name}")
            except Exception:
                traceback.print_exc()

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
            # 2026-05-23: разделение провайдеров. RefGenerateThread —
            # мульти-kind (локация / объект / персонаж сериала). Определяем
            # kind по пути (`Path.parts`, кроссплатформенно):
            #   • characters/<slug>/<file>.jpg → провайдер актёров
            #   • locations/ или objects/ → админский провайдер
            # Утилита `_ref_kind_from_path` живёт в storyboard_app.py, не
            # тянем её сюда — мини-версия (только проверка наличия
            # 'characters' в path.parts).
            parts_lower = {p.lower() for p in self.image_path.parts}
            if 'characters' in parts_lower:
                provider = _sa.image_provider_actors()
            else:
                # 'locations', 'objects', либо неожиданный путь → админский
                provider = _sa.image_provider_admin()
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
                encoding="utf-8",      # 2026-05-09 Win-fix: без encoding на
                errors="replace",      # win10/11 default = cp1252 → crash
                timeout=300,           # на 0x98 в UTF-8 stdout claude.
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
                encoding="utf-8",      # 2026-05-09 Win-fix: иначе на Win
                errors="replace",      # cp1252 ловит UTF-8 stdout → crash.
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


# ─── Детективное логирование актёрской генерации (v1.0.78) ────────

def _log_actor_ref_event(project_root, session_id: str, stage: str,
                          include_stack: bool = False, **kwargs) -> None:
    """v1.0.78: детективное логирование GenerateActorRefThread в
    `actors/actor_ref_changes.log` для диагностики «лицо непохоже»
    на 3-4-й повторной генерации.

    Парные с _log_actor_role_call (storyboard_app.py:_log_actor_role_call):
    одна сессия генерации = один session_id, несколько ENTRY/END секций
    в логе. Чтобы при следующей плохой генерации видеть точную цепочку:
    что улетело в API → какие fh пришли → что вернул /generate → какой
    uri отдал polling → размер сохранённого файла.

    Никогда не бросает исключения — вся запись wrapped в try/except.
    Ротация при > 1 MB → .log.old (с перезаписью старого .old).

    Cross-platform: pathlib.Path + open(encoding='utf-8'). Никаких
    subprocess/shell. datetime/traceback/json импортируются локально
    (стиль файла threads/generate.py — все эти модули используются
    inline, не module-level).

    Args:
        project_root: Path к корню проекта (там создаётся actors/).
        session_id:   уникальный ID одной генерации (timestamp с usec).
        stage:        'entry_start' | 'photos_uploaded' |
                       'generate_response' | 'result_saved' | 'end'.
        include_stack: True ТОЛЬКО для entry_start (экономия места).
        **kwargs:     поля стадии (см. примеры в caller'ах).
    """
    try:
        if project_root is None:
            return
        import datetime as _dt
        import json as _json
        actors_root = Path(project_root) / "actors"
        try:
            actors_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        log_path = actors_root / "actor_ref_changes.log"
        # Ротация при > 1 MB
        try:
            if log_path.exists() and log_path.stat().st_size > 1_000_000:
                old_path = log_path.with_suffix(".log.old")
                try:
                    if old_path.exists():
                        old_path.unlink()
                except Exception:
                    pass
                try:
                    log_path.rename(old_path)
                except Exception:
                    pass
        except Exception:
            pass

        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        lines.append(f"--- ENTRY session_id={session_id} stage={stage} ---")
        lines.append(f"{ts}")
        # Форматируем kwargs построчно. Списки/dict через json для читаемости.
        for k, v in kwargs.items():
            try:
                if isinstance(v, (list, tuple)):
                    lines.append(f"{k}:")
                    for item in v:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            lines.append(f"  - {item[0]} -> {item[1]}")
                        else:
                            lines.append(f"  - {item}")
                elif isinstance(v, dict):
                    lines.append(
                        f"{k}: {_json.dumps(v, ensure_ascii=False)}")
                elif isinstance(v, str) and "\n" in v:
                    # Многострочные значения — отдельным блоком
                    lines.append(f"{k}: <<<")
                    for line in v.splitlines():
                        lines.append(f"  {line}")
                    lines.append(">>>")
                else:
                    lines.append(f"{k}: {v}")
            except Exception:
                lines.append(f"{k}: <unrepr>")

        if include_stack:
            try:
                import traceback as _tb
                frames = _tb.extract_stack()[-12:-2]
                lines.append("caller_stack:")
                for fr in frames:
                    lines.append(
                        f'  File "{fr.filename}", line {fr.lineno}, '
                        f"in {fr.name}")
            except Exception:
                pass

        lines.append("--- END ---")
        lines.append("")  # разделитель

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Никогда не падать из-за логирования
        pass


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

    def _diag_log(self, msg: str) -> None:
        """2026-05-17: append [actor_gen] line to
        `shows/<show>/_studio_diag.log`. Active show определяется из
        `self.target_dir` (формат `shows/<show>/refs/characters/<char>/`).
        Failures проглатываются — actor-flow не должен валиться из-за
        проблем с логированием. Зеркалит паттерн `_diag_log_append` из
        views/episode_chat.py, но без зависимости от MainWindow."""
        try:
            from datetime import datetime as _dt
            # target_dir = .../shows/<show>/refs/characters/<character>/
            # parents[0]=characters, [1]=refs, [2]=<show>  ← show_root
            show_root = self.target_dir.parents[2]
            log_path = show_root / "_studio_diag.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [actor_gen] {msg}\n")
        except Exception:
            try:
                import sys as _sys
                _sys.stderr.write(f"[actor_gen] {msg}\n")
            except Exception:
                pass

    def run(self):
        # 2026-05-22 (v1.0.78): детективное логирование в
        # actors/actor_ref_changes.log. См. _log_actor_ref_event выше.
        # Никогда не ломает основной flow (try/except внутри функции).
        import datetime as _dt_ref
        _session_id = _dt_ref.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        _last_stage = "init"
        _error_msg = None
        try:
            _project_root = self.target_dir.parents[4]
        except Exception:
            _project_root = None
        try:
            _log_actor_ref_event(
                _project_root, _session_id, "entry_start",
                include_stack=True,
                actor_slug=self.actor_slug,
                target_dir=str(self.target_dir),
                output_filename=self.output_filename,
                prompt_text=self.prompt_text,
                photo_paths=[
                    f"{p} ({(p.stat().st_size if p.exists() else 0)} bytes)"
                    for p in self.photo_paths
                ])
            _last_stage = "entry_start"
        except Exception:
            pass
        try:
            key = _sa.load_api_key()
            if not key:
                self.error.emit(tr('create_ref_no_api_key'))
                return
            session = requests.Session()
            session.headers.update({"X-API-Key": key})

            # Загружаем все фотки актёра как референсы (до 10 — ограничение API)
            # 2026-05-17: переезд на общую _read_image_for_upload — даёт
            # ресайз до MAX_REF_SIDE=2000px (LANCZOS, JPEG q=92, в памяти)
            # и MIME по магическим байтам. Раньше inline-цикл слал
            # iPhone-фотки 2316×3088 как есть → Fast Gen «many-image
            # requests» жаловался на лимит 2000px и identity-bind ломался
            # (юзеру казалось что «фотки не доходят» — лицо непохоже).
            # Симметрия с GenerateThread._upload_file (шоты) и
            # RefGenerateThread._upload (location/object).
            self.progress.emit(tr('create_ref_uploading',
                                  n=min(len(self.photo_paths), 10)))
            ref_hashes = []
            for p in self.photo_paths[:10]:
                data_bytes, mime = _read_image_for_upload(p)
                r = session.post(f"{_sa.STORAGE_BASE}/upload",
                                 files={"file": (p.name, data_bytes, mime)},
                                 timeout=60)
                r.raise_for_status()
                data = r.json()
                fh = (data.get("file_hash") or data.get("file")
                      or data.get("hash"))
                if not fh:
                    raise RuntimeError(f"upload missing hash: {data}")
                ref_hashes.append(fh)

            # 2026-05-22 (v1.0.78): лог photos_uploaded — какие фото
            # реально улетели в Fast Gen и с какими fh ответил сервер.
            try:
                _log_actor_ref_event(
                    _project_root, _session_id, "photos_uploaded",
                    uploaded=list(zip(
                        [str(p) for p in self.photo_paths[:10]],
                        ref_hashes)))
                _last_stage = "photos_uploaded"
            except Exception:
                pass

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
            # Provider выбирается админом в Settings (default: NARWHAL).
            # Раньше (`Phase 2 hotfix #20`) endpoint был захардкожен на
            # OpenAI flow ради cost=1 (vs 4 у NARWHAL). 2026-05-15: actor
            # refs тоже подключены к GUI-переключателю — иначе при
            # «No accounts available for OpenAI operations» от Fast Gen
            # юзер вообще не мог создать референс актёра, хотя у него в
            # настройках стоит NARWHAL и шоты успешно генерятся.
            #
            # Endpoints — те же что в GenerateThread:
            #   NARWHAL `/api/v4/flow/image/generate` — multi-ref (3-10
            #     фото актёра подаются как identity refs), мягче content,
            #     cost=4. НЕ передавать `model`.
            #   OpenAI `/api/v4/openai/image/generate` — ломается на 3+
            #     refs (pydantic), режется до 2; cost=1.
            # 2026-05-23: разделение провайдеров — актёрские рефы идут
            # через `image_provider_actors` (виден всем юзерам, не
            # только админу).
            provider = _sa.image_provider_actors()
            payload = {
                "prompt": self.prompt_text,
                "aspect_ratio": "16:9",
            }
            if ref_hashes:
                if provider == _sa.IMAGE_PROVIDER_OPENAI and len(ref_hashes) > 2:
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
            op_id = data.get("operation_id")
            if not op_id:
                self.error.emit(f"No operation_id: {data}")
                return
            # 2026-05-22 (v1.0.78): лог generate_response — что именно
            # ушло в /generate (prompt + ref_hashes + endpoint) и какой
            # operation_id вернул сервер.
            try:
                _log_actor_ref_event(
                    _project_root, _session_id, "generate_response",
                    operation_id=op_id,
                    endpoint=endpoint,
                    ref_hashes=ref_hashes,
                    prompt_text=self.prompt_text)
                _last_stage = "generate_response"
            except Exception:
                pass
            # 2026-05-17: diag-лог в `shows/<show>/_studio_diag.log` —
            # видимость в чате при отладке (раньше actor-flow не писал
            # вообще ничего, симптомы «лицо непохоже» были без следов).
            self._diag_log(
                f"slug={self.actor_slug} provider={provider} "
                f"refs={len(ref_hashes)}/{len(self.photo_paths)} "
                f"op_id={op_id}")
            # 2026-05-17 (расширение): полный payload-дамп для случая когда
            # upload прошёл (refs=N/M, op_id есть), но лицо на выходе
            # непохоже. Сравнить ровно то что улетело в Fast Gen с тем
            # как юзер шлёт ВРУЧНУЮ на сайте Nano Banana. api_key в
            # payload НЕТ — он в session.headers (X-API-Key), не пишется.
            try:
                _prompt_head = (self.prompt_text or "")[:200].replace(
                    "\n", " ")
                self._diag_log(
                    f"slug={self.actor_slug} endpoint={endpoint} "
                    f"payload_keys={sorted(payload.keys())} "
                    f"aspect_ratio={payload.get('aspect_ratio')!r} "
                    f"prompt_length={len(self.prompt_text)}")
                self._diag_log(
                    f"slug={self.actor_slug} ref_hashes={ref_hashes}")
                self._diag_log(
                    f"slug={self.actor_slug} prompt_head={_prompt_head!r}")
            except Exception:
                pass

            # Polling
            # 2026-05-10 (БАГ 8 fix): добавлен timeout на весь polling-loop
            # (5 минут) + progress.emit на каждой итерации с текущим
            # статусом. Раньше при незнакомом status (queued/pending/...)
            # loop крутился молча — юзер видел overlay «Генерирую…» и
            # никаких update'ов, никаких error/finished emit'ов. Если
            # API залипало → thread висел вечно.
            POLL_TIMEOUT_SEC = 300  # 5 минут — потолок ожидания
            poll_started = time.monotonic()
            last_status = ""
            while True:
                time.sleep(4)
                elapsed = int(time.monotonic() - poll_started)
                if elapsed > POLL_TIMEOUT_SEC:
                    self.error.emit(
                        f"API timeout: статус «{last_status or 'unknown'}»"
                        f" оставался {elapsed}с (>5 мин). Попробуй ещё раз.")
                    return
                try:
                    r = session.get(
                        f"{_sa.API_BASE}/api/v4/operations/{op_id}",
                        timeout=30)
                    r.raise_for_status()
                    d = r.json()
                except Exception as poll_ex:
                    # Сетевые ошибки во время polling — сразу выходим с
                    # явной ошибкой, не молча.
                    self.error.emit(f"Polling network error: {poll_ex}")
                    return
                status = (d.get("status") or "").lower()
                last_status = status
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
                    # 2026-05-22 (v1.0.78): лог result_saved — какой uri
                    # отдал polling и какой реально файл сохранили.
                    # Если uri указывает на чужой fh — поймаем тут.
                    try:
                        try:
                            _result_size = target.stat().st_size
                        except Exception:
                            _result_size = 0
                        # 9f: uri может быть data:base64,<огромная картинка>
                        # на ~1 MB. Обрезаем для лога — оставляем тип uri
                        # (data:/file:/http) и первые 200 символов + длина
                        # оригинала. Само uri выше уже использовано для
                        # скачивания, изменение не влияет.
                        _uri_for_log = uri
                        if isinstance(uri, str) and len(uri) > 200:
                            _uri_for_log = (uri[:200]
                                + f"... (truncated, total {len(uri)} chars)")
                        _log_actor_ref_event(
                            _project_root, _session_id, "result_saved",
                            result_uri=_uri_for_log,
                            target_path=str(target),
                            target_size=_result_size)
                        _last_stage = "result_saved"
                    except Exception:
                        pass
                    self.finished.emit(str(target))
                    return
                if status == "error":
                    self.error.emit(f"API error: {d.get('error')}")
                    return
                # Любой другой status (queued/pending/processing/...) —
                # эмитим progress чтобы юзер видел что thread жив и в
                # каком статусе. Без этого UI overlay стоял с одним и
                # тем же лейблом «Генерирую…» бесконечно.
                self.progress.emit(
                    tr('create_ref_polling_status',
                       status=status or 'pending', sec=elapsed))

        except Exception as e:
            _error_msg = str(e)
            self.error.emit(_error_msg)
        finally:
            # 2026-05-22 (v1.0.78): финальный лог end — закрывает сессию.
            # last_stage показывает на какой стадии завершилось (полезно
            # для return-путей которые не доходят до result_saved).
            try:
                _log_actor_ref_event(
                    _project_root, _session_id, "end",
                    status=("error" if _error_msg else "success"),
                    last_stage=_last_stage,
                    error=_error_msg)
            except Exception:
                pass


class EditActorRefThread(QThread):
    """2026-05-17: edit-режим для УЖЕ СГЕНЕРИРОВАННОГО рефа актёра.

    Симметричный аналог `RefGenerateThread(mode='edit')` (locations/objects)
    и `GenerateThread(edit_instruction=...)` (shots): берёт существующий
    реф как identity-anchor `[@]img1`, отправляет в FastGen с короткой
    инструкцией и шаблоном «keep identity, modify only requested element»,
    сохраняет результат НОВЫМ файлом в той же папке с инкрементным
    суффиксом (collision-rename как в GenerateActorRefThread).

    Отличие от GenerateActorRefThread:
      • 1 ref (текущий) вместо 1-10 (исходные фотки);
      • prompt построен из шаблона + instruction, не ACTOR_REF_PROMPT_*;
      • НЕ перезаписывает source-файл (юзер должен видеть оба варианта).

    Этот класс — отдельный, ничего из существующего кода не трогает.
    """

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)        # путь к сохранённому файлу
    error = pyqtSignal(str)

    # Шаблон промпта — identity-якорь + точечная правка. Зеркалит логику
    # _build_edit_prompt из GenerateThread и edit-промпт RefGenerateThread,
    # но с акцентом «лицо/идентичность НЕ перерисовывать».
    _EDIT_PROMPT_TEMPLATE = (
        "[@]img1 is the current actor identity reference sheet "
        "(multi-panel layout).\n\n"
        "MODIFICATION REQUESTED: {instruction}\n\n"
        "Apply ONLY the requested modification. Keep ALL identity "
        "features EXACTLY identical to [@]img1: same face, same person, "
        "same facial proportions, same eyes, same nose, same mouth, "
        "same hairstyle, same skin tone, same age, same ethnicity. "
        "Keep the overall multi-panel sheet layout, lighting, "
        "background, and art style EXACTLY as in [@]img1. Do NOT "
        "redraw the face. Apply the modification only to the requested "
        "element."
    )

    def __init__(self, actor_slug: str, target_dir: Path,
                 source_image_path: Path, instruction: str,
                 parent=None):
        super().__init__(parent)
        self.actor_slug = actor_slug
        self.target_dir = Path(target_dir)
        self.source_image_path = Path(source_image_path)
        self.instruction = (instruction or "").strip()

    def _diag_log(self, msg: str) -> None:
        """Append [actor_edit] line to `shows/<show>/_studio_diag.log`.
        Симметрия с GenerateActorRefThread._diag_log — show_root через
        self.target_dir.parents[2]. Failures проглатываются."""
        try:
            from datetime import datetime as _dt
            show_root = self.target_dir.parents[2]
            log_path = show_root / "_studio_diag.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [actor_edit] {msg}\n")
        except Exception:
            try:
                import sys as _sys
                _sys.stderr.write(f"[actor_edit] {msg}\n")
            except Exception:
                pass

    def run(self):
        try:
            if not self.instruction:
                self.error.emit("Edit без инструкции — нечего применять")
                return
            if not self.source_image_path.exists():
                self.error.emit(
                    f"Нет исходного рефа: {self.source_image_path.name}")
                return
            key = _sa.load_api_key()
            if not key:
                self.error.emit(tr('create_ref_no_api_key'))
                return
            session = requests.Session()
            session.headers.update({"X-API-Key": key})

            # 1. Upload текущего рефа как identity-anchor.
            #    _read_image_for_upload даёт ресайз ≤2000px (LANCZOS,
            #    JPEG q=92) + MIME по магическим байтам.
            self.progress.emit(tr('create_ref_uploading', n=1))
            data_bytes, mime = _read_image_for_upload(
                self.source_image_path)
            r = session.post(
                f"{_sa.STORAGE_BASE}/upload",
                files={"file": (self.source_image_path.name,
                                data_bytes, mime)},
                timeout=60)
            r.raise_for_status()
            upload_data = r.json()
            fh = (upload_data.get("file_hash")
                  or upload_data.get("file")
                  or upload_data.get("hash"))
            if not fh:
                raise RuntimeError(f"upload missing hash: {upload_data}")
            ref_hashes = [fh]

            # 2. Целевая папка + collision-free имя на основе source-stem.
            #    Пример: olya_ref_3.jpg → olya_ref_3_edit.jpg → ... _edit_2.jpg
            target_dir = self.target_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            base = self.source_image_path.stem + "_edit"
            ext = ".jpg"
            target = target_dir / f"{base}{ext}"
            if target.exists():
                i = 2
                while (target_dir / f"{base}_{i}{ext}").exists():
                    i += 1
                target = target_dir / f"{base}_{i}{ext}"

            # 3. Промпт + payload + endpoint (та же логика что в
            #    GenerateActorRefThread, без model field).
            # 2026-05-23: разделение провайдеров — edit актёрского рефа
            # идёт через `image_provider_actors` (виден всем юзерам).
            self.progress.emit(tr('create_ref_generating'))
            prompt_text = self._EDIT_PROMPT_TEMPLATE.format(
                instruction=self.instruction)
            provider = _sa.image_provider_actors()
            payload = {
                "prompt": prompt_text,
                "aspect_ratio": "16:9",
            }
            if ref_hashes:
                if (provider == _sa.IMAGE_PROVIDER_OPENAI
                        and len(ref_hashes) > 2):
                    ref_hashes = ref_hashes[:2]
                payload["reference_images"] = ref_hashes
            endpoint = ("/api/v4/openai/image/generate"
                        if provider == _sa.IMAGE_PROVIDER_OPENAI
                        else "/api/v4/flow/image/generate")
            r = session.post(f"{_sa.API_BASE}{endpoint}",
                             json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            op_id = data.get("operation_id")
            if not op_id:
                self.error.emit(f"No operation_id: {data}")
                return

            # Diag-лог формата, который просил юзер.
            self._diag_log(
                f"slug={self.actor_slug} "
                f"source={self.source_image_path.name} "
                f"instruction_length={len(self.instruction)} "
                f"target={target.name} op_id={op_id}")

            # 4. Polling (5 мин timeout, тот же pattern что в
            #    GenerateActorRefThread).
            POLL_TIMEOUT_SEC = 300
            poll_started = time.monotonic()
            last_status = ""
            while True:
                time.sleep(4)
                elapsed = int(time.monotonic() - poll_started)
                if elapsed > POLL_TIMEOUT_SEC:
                    self.error.emit(
                        f"API timeout: статус «{last_status or 'unknown'}» "
                        f"оставался {elapsed}с (>5 мин). Попробуй ещё раз.")
                    return
                try:
                    r = session.get(
                        f"{_sa.API_BASE}/api/v4/operations/{op_id}",
                        timeout=30)
                    r.raise_for_status()
                    d = r.json()
                except Exception as poll_ex:
                    self.error.emit(f"Polling network error: {poll_ex}")
                    return
                status = (d.get("status") or "").lower()
                last_status = status
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
                        r2 = session.get(
                            f"{_sa.STORAGE_BASE}/file/{fh}/raw",
                            timeout=120)
                        r2.raise_for_status()
                        image_bytes = r2.content
                    target.write_bytes(image_bytes)
                    self.finished.emit(str(target))
                    return
                if status == "error":
                    self.error.emit(f"API error: {d.get('error')}")
                    return
                self.progress.emit(
                    tr('create_ref_polling_status',
                       status=status or 'pending', sec=elapsed))

        except Exception as e:
            self.error.emit(str(e))


class ApplyTextureThread(QThread):
    """2026-05-17 (Этап 2): PIL-композит ref + texture с opacity, zoom, offset.

    Никаких API-запросов — чистая локальная PIL операция (1-3с на
    16:9 картинке). Запускается в QThread чтобы UI не подвисал.

    Алгоритм:
      base = Image.open(source).convert("RGB")
      tex  = Image.open(texture).convert("RGB")
      # zoom: тек size = base.size * (zoom/100). Если zoom=100, tex
      # точно ложится на base (так было до zoom-расширения).
      tex  = tex.resize((base.w * zoom/100, base.h * zoom/100), LANCZOS)
      # crop центр + offset → возвращает регион base.size:
      #   left = tex.w/2 - base.w/2 + offset_x
      #   top  = tex.h/2 - base.h/2 + offset_y
      # offset clamp: |off| ≤ (tex.size - base.size) / 2 — чтобы crop
      # не вылез за границы tex.
      cropped = tex.crop((left, top, left + base.w, top + base.h))
      result  = Image.blend(base, cropped, opacity / 100.0)
      result.save(target, "JPEG", quality=92)

    `Image.blend(a, b, α)` = a*(1-α) + b*α. При α=0 чистый base,
    при α=1 чистая texture. Юзер выбирает opacity 10-100, zoom 100-300.
    `offset_x`/`offset_y` — в координатах full-size base (px), могут
    быть отрицательными (двигать влево/вверх).

    Файл-источник (ref) и текстура — НЕ модифицируются. Имя результата
    не зависит от zoom/offset (только от opacity) — при повторном
    Apply с тем же opacity файл перезаписывается; договор с юзером.
    """

    progress = pyqtSignal(str)
    finished = pyqtSignal(str)        # путь к сохранённому файлу
    error = pyqtSignal(str)

    def __init__(self, source_image_path: Path, texture_path: Path,
                 opacity_percent: int, target_path: Path,
                 zoom_percent: int = 100,
                 offset_x: int = 0, offset_y: int = 0,
                 parent=None):
        super().__init__(parent)
        self.source_image_path = Path(source_image_path)
        self.texture_path = Path(texture_path)
        self.opacity_percent = int(opacity_percent)
        self.target_path = Path(target_path)
        self.zoom_percent = int(zoom_percent)
        self.offset_x = int(offset_x)
        self.offset_y = int(offset_y)

    def run(self):
        try:
            self.progress.emit(tr('apply_texture_progress'))
            if not self.source_image_path.exists():
                self.error.emit(
                    f"Нет исходного рефа: {self.source_image_path.name}")
                return
            if not self.texture_path.exists():
                self.error.emit(
                    f"Нет файла текстуры: {self.texture_path.name}")
                return
            opacity = max(0, min(100, self.opacity_percent)) / 100.0
            zoom = max(100, min(300, self.zoom_percent)) / 100.0
            base = Image.open(self.source_image_path).convert("RGB")
            tex = Image.open(self.texture_path).convert("RGB")
            bw, bh = base.size
            tex_w = max(bw, int(round(bw * zoom)))
            tex_h = max(bh, int(round(bh * zoom)))
            if tex.size != (tex_w, tex_h):
                tex = tex.resize(
                    (tex_w, tex_h), Image.Resampling.LANCZOS)
            # Crop центральной области размера base с offset
            max_off_x = (tex_w - bw) // 2
            max_off_y = (tex_h - bh) // 2
            off_x = max(-max_off_x, min(max_off_x, self.offset_x))
            off_y = max(-max_off_y, min(max_off_y, self.offset_y))
            left = (tex_w - bw) // 2 + off_x
            top = (tex_h - bh) // 2 + off_y
            cropped = tex.crop((left, top, left + bw, top + bh))
            result = Image.blend(base, cropped, opacity)
            self.target_path.parent.mkdir(parents=True, exist_ok=True)
            result.save(str(self.target_path), "JPEG", quality=92)
            # 2026-05-17 (Этап 2 патч): сохраняем .meta.json рядом с jpg.
            # При следующем открытии ApplyTextureDialog для того же ref'а —
            # восстанавливаем последние настройки (текстура + opacity +
            # zoom + offset). Failures проглатываем — основное действие
            # (jpg) уже выполнено, finished нужно эмитить.
            try:
                import json as _json
                from datetime import datetime as _dt
                meta_path = self.target_path.with_suffix('.meta.json')
                meta = {
                    "source_stem": self.source_image_path.stem,
                    "texture_name": self.texture_path.name,
                    "opacity": self.opacity_percent,
                    "zoom": self.zoom_percent,
                    "offset_x": self.offset_x,
                    "offset_y": self.offset_y,
                    "saved_at": _dt.now().isoformat(),
                }
                meta_path.write_text(
                    _json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                import traceback as _tb
                _tb.print_exc()
            self.finished.emit(str(self.target_path))
        except Exception as e:
            self.error.emit(str(e))
