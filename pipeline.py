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

FASTGEN_BASE    = "https://googler.fast-gen.ai"
FASTGEN_STORAGE = "https://storage.fast-gen.ai"


def load_key() -> str:
    lines = [l.strip() for l in ENV_FILE.read_text().splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(".env is empty — add your Fast Gen AI key as the first line")
    return lines[0]


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


def generate_image(prompt: str, name: str, fastgen_key: str) -> Path:
    LOCATIONS_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"X-API-Key": fastgen_key, "Content-Type": "application/json"}

    r = requests.post(
        f"{FASTGEN_BASE}/api/v4/flow/image/generate",
        headers=headers,
        params={"result_format": "ref"},
        json={"prompt": prompt, "aspect_ratio": "16:9", "model": "NARWHAL"},
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

    content_type = r.headers.get("Content-Type", "image/jpeg")
    ext = "png" if "png" in content_type else "jpg"
    out = LOCATIONS_DIR / f"{name}.{ext}"
    out.write_bytes(r.content)
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "generate":
        if len(sys.argv) < 4:
            print('Usage: python pipeline.py generate <name> "<prompt>"')
            sys.exit(1)
        name = sys.argv[2]
        prompt = sys.argv[3]
        key = load_key()
        existing = list(LOCATIONS_DIR.glob(f"{name}.*"))
        if existing:
            print(f"Already exists: {existing[0]}")
            print("Delete it first if you want to regenerate.")
            sys.exit(0)
        print(f"Generating: {name}")
        img_path = generate_image(prompt, name, key)
        print(f"Saved: {img_path}")

    elif command == "check":
        if len(sys.argv) < 3:
            print("Usage: python pipeline.py check <name>")
            sys.exit(1)
        name = sys.argv[2]
        existing = list(LOCATIONS_DIR.glob(f"{name}.*"))
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