#!/usr/bin/env python3
"""Storyboard automation pipeline.

This script does ONE thing only:
- Generates location images via Fast Gen AI

Geometry description is done by Claude Code itself (no API cost).
Everything else is done by Claude Code using the user's subscription.

Usage:
  python pipeline.py generate <name> "<prompt>"
  python pipeline.py check <name>
"""

import json
import sys
import time
from pathlib import Path

import requests

ROOT     = Path(__file__).parent
ENV_FILE = ROOT / ".env"
# 2026-05-15: bridge-файл от Studio. Содержит «narwhal» или «openai» —
# выбор юзера в Настройках → провайдер картинок. Pipeline.py запускается
# AI-агентом в subprocess'е `claude -p` и не имеет доступа к QSettings,
# поэтому Studio пишет настройку сюда (см. `sync_pipeline_py_to_project`
# и `set_image_provider` в storyboard_app.py). Default при отсутствии
# файла — `openai` (обратная совместимость со старыми сборками).
PROVIDER_FILE = ROOT / "image_provider.txt"


def get_show_root() -> Path:
    """Папка активного сериала из current_show.json. Fallback на ROOT если нет."""
    show_file = ROOT / "current_show.json"
    if show_file.exists():
        try:
            current = json.loads(show_file.read_text(encoding="utf-8")).get("current")
            if current and (ROOT / "shows" / current).exists():
                return ROOT / "shows" / current
        except Exception:
            pass
    return ROOT


SHOW_ROOT     = get_show_root()
LOCATIONS_DIR = SHOW_ROOT / "refs" / "locations"
# 2026-05-07: ранее pipeline.py писал ВСЁ (включая объекты) в LOCATIONS_DIR.
# AI-агент потом перемещал .jpg в refs/objects/, но _prompt.txt оставался
# orphan'ом в locations/. Теперь pipeline сам знает куда писать через
# флаг `--kind=object|location`.
OBJECTS_DIR   = SHOW_ROOT / "refs" / "objects"

FASTGEN_BASE    = "https://googler.fast-gen.ai"
FASTGEN_STORAGE = "https://storage.fast-gen.ai"


def load_key() -> str:
    lines = [l.strip() for l in ENV_FILE.read_text().splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(".env is empty — add your Fast Gen AI key as the first line")
    return lines[0]


def load_provider() -> str:
    """Возвращает 'narwhal' или 'openai'. Default — 'openai'.

    Studio пишет это значение в `image_provider.txt` рядом с .env при
    каждом изменении настройки и при старте. Если файла нет (старая
    сборка / dev-режим без Studio) — fallback на 'openai' (текущее
    историческое поведение pipeline.py до 2026-05-15).
    """
    try:
        if not PROVIDER_FILE.exists():
            return "openai"
        v = PROVIDER_FILE.read_text(encoding="utf-8").strip().lower()
        return v if v in ("narwhal", "openai") else "openai"
    except Exception:
        return "openai"


def _fastgen_poll(op_id: str, headers: dict) -> dict:
    while True:
        time.sleep(4)
        r = requests.get(
            f"{FASTGEN_BASE}/api/v4/operations/{op_id}",
            headers=headers,
            params={"result_format": "ref"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        print(f"    status: {status}")
        if status == "success" and data.get("result"):
            return data
        if status == "error":
            raise RuntimeError(f"Fast Gen error: {data}")


def generate_image(prompt: str, name: str, fastgen_key: str,
                    target_dir: Path = None) -> Path:
    """Генерация картинки через Fast Gen API.

    `target_dir` — куда сохранить .jpg + _prompt.txt. По умолчанию
    LOCATIONS_DIR (для обратной совместимости со старыми вызовами).
    Для объектов передавать OBJECTS_DIR через флаг `--kind=object`.
    """
    if target_dir is None:
        target_dir = LOCATIONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    headers = {"X-API-Key": fastgen_key, "Content-Type": "application/json"}

    # 2026-05-15: провайдер берётся из image_provider.txt (Studio пишет
    # туда выбор юзера из Настроек). До этой правки endpoint был
    # захардкожен на OpenAI flow (`Phase 2 hotfix #20` cost=1). Теперь
    # GUI-переключатель «Nano Banana 2 / OpenAI» влияет и на pipeline.py
    # тоже — раньше он применялся только к шотам (`GenerateThread`).
    #
    # NARWHAL `/api/v4/flow/image/generate`:
    #   • cost_charged=4. Мягче content-фильтр. Pool аккаунтов отдельный
    #     от OpenAI — спасает когда «No accounts available for OpenAI
    #     operations» (исторический трюк fallback'а).
    #   • НЕ передавать поле `model` — иначе flow маршрутизирует обратно
    #     в OpenAI с теми же policy и pydantic-ошибкой (см.
    #     threads/generate.py:242).
    # OpenAI `/api/v4/openai/image/generate`:
    #   • cost_charged=1. Без полей `model`/`resolution`.
    #   • Content-policy блокирует огнестрел/узнаваемых людей.
    provider = load_provider()
    endpoint = ("/api/v4/flow/image/generate"
                if provider == "narwhal"
                else "/api/v4/openai/image/generate")
    print(f"  provider: {provider} ({endpoint})")
    r = requests.post(
        f"{FASTGEN_BASE}{endpoint}",
        headers=headers,
        params={"result_format": "ref"},
        json={"prompt": prompt, "aspect_ratio": "16:9"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success") or not data.get("operation_id"):
        raise RuntimeError(f"Failed to start generation: {data}")

    op_id = data["operation_id"]
    print(f"  op_id: {op_id}")
    result_data = _fastgen_poll(op_id, headers)

    result = result_data["result"]
    ref = result[0] if isinstance(result, list) else result
    if isinstance(ref, dict):
        ref = ref.get("ref") or ref.get("url") or ref.get("file_hash") or ""
    file_hash = ref[5:] if str(ref).startswith("file:") else ref

    r = requests.get(
        f"{FASTGEN_STORAGE}/file/{file_hash}/raw",
        headers={"X-API-Key": fastgen_key},
        timeout=60,
    )
    r.raise_for_status()

    # 2026-05-07: расширение определяется по МАГИЧЕСКИМ БАЙТАМ контента,
    # а не по Content-Type заголовку (иногда API его не возвращает →
    # дефолт "image/jpeg" → файл сохранялся как `.jpg` хотя байты были
    # PNG → upload в Edit-режиме потом падал, MIME=image/jpeg vs PNG-bytes).
    head = r.content[:12]
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        ext = "png"
    elif head[:3] == b'\xff\xd8\xff':
        ext = "jpg"
    elif head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        ext = "webp"
    else:
        # Fallback по Content-Type (как было раньше)
        content_type = r.headers.get("Content-Type", "image/jpeg")
        ext = "png" if "png" in content_type else "jpg"
    out = target_dir / f"{name}.{ext}"
    out.write_bytes(r.content)

    # Сохраняем сам промпт рядом с картинкой — нужен для кнопки «Перегенерировать»
    # в Storyboard Studio (она читает этот файл и шлёт тот же промпт в FastGen,
    # получая новую вариацию того же рефа).
    prompt_file = target_dir / f"{name}_prompt.txt"
    try:
        prompt_file.write_text(prompt, encoding="utf-8")
    except Exception:
        pass

    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "generate":
        # 2026-05-07: добавлен флаг --force для перезаписи существующего
        # рефа. Используется когда юзер кликает «🎨 Сгенерировать» в Studio
        # на уже-сгенерированной локации/объекте — он ожидает НОВУЮ
        # картинку, а не сообщение «Already exists». Без флага поведение
        # старое (CLI-safe: не перезаписывать случайно).
        # 2026-05-07: добавлен флаг --kind=location|object — раньше всё
        # шло в LOCATIONS_DIR независимо от типа, _prompt.txt объектов
        # оставался orphan'ом в locations/. Теперь pipeline.py роутит
        # сам.
        # Argv: pipeline.py generate <name> "<prompt>" [--force] [--kind=K]
        args = sys.argv[2:]
        force = False
        kind = "location"
        cleaned = []
        for a in args:
            if a == "--force":
                force = True
            elif a.startswith("--kind="):
                k = a.split("=", 1)[1].strip().lower()
                if k in ("location", "object"):
                    kind = k
                else:
                    print(f"Unknown kind '{k}', expected location|object")
                    sys.exit(1)
            else:
                cleaned.append(a)
        args = cleaned
        if len(args) < 2:
            print('Usage: python pipeline.py generate <name> "<prompt>" '
                  '[--force] [--kind=location|object]')
            sys.exit(1)
        name = args[0]
        prompt = args[1]
        # 2026-06-19: ключ через round-robin пул (key_pool), как в
        # generate_storyboards.py:160-161. Ленивый импорт + fallback на
        # load_key(): если key_pool.py не докопировался рядом (старая
        # установка) или next_key кинул — работаем на одиночном .env-ключе
        # (старое поведение сохраняется при любых проблемах с пулом).
        try:
            from key_pool import next_key
            key = next_key() or load_key()
        except Exception:
            key = load_key()
        target_dir = OBJECTS_DIR if kind == "object" else LOCATIONS_DIR
        existing = list(target_dir.glob(f"{name}.*"))
        if existing and not force:
            print(f"Already exists: {existing[0]}")
            print("Delete it first if you want to regenerate, "
                  "or pass --force to overwrite.")
            sys.exit(0)
        if existing and force:
            # Удаляем все файлы с таким же базовым именем (jpg/png/webp)
            # чтобы избежать дубликатов с разными расширениями.
            for p in existing:
                try:
                    p.unlink()
                    print(f"Removed existing: {p.name}")
                except Exception as ex:
                    print(f"Could not remove {p.name}: {ex}")
        print(f"Generating: {name} (kind={kind})")
        img_path = generate_image(prompt, name, key, target_dir=target_dir)
        print(f"Saved: {img_path}")

    elif command == "check":
        # 2026-05-07: --kind=K — где искать. Default: location.
        args = sys.argv[2:]
        kind = "location"
        cleaned = []
        for a in args:
            if a.startswith("--kind="):
                k = a.split("=", 1)[1].strip().lower()
                if k in ("location", "object"):
                    kind = k
            else:
                cleaned.append(a)
        if not cleaned:
            print("Usage: python pipeline.py check <name> [--kind=location|object]")
            sys.exit(1)
        name = cleaned[0]
        target_dir = OBJECTS_DIR if kind == "object" else LOCATIONS_DIR
        existing = list(target_dir.glob(f"{name}.*"))
        if existing:
            print(f"EXISTS: {existing[0]}")
        else:
            print(f"NOT FOUND: {name}")

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())