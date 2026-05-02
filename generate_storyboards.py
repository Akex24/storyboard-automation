#!/usr/bin/env python3
"""Generate storyboard images via Nano Banana 2 (NARWHAL model on flow/image/generate).

Reads # [@]imgN = filename.jpg headers from each prompt file,
uploads all refs to fast-gen storage (up to 10 refs supported),
and sends to flow/image/generate with model=NARWHAL.

Usage: python generate_storyboards.py ep20_block_1 ep20_block_2 ...
       python generate_storyboards.py  (generates all *block_*.txt prompts)
"""

import re
import sys
import time
import base64
from pathlib import Path
from typing import Optional

import requests

ROOT            = Path(__file__).parent
ENV_FILE        = ROOT / ".env"
PROMPTS_DIR     = ROOT / "output" / "prompts"
STORYBOARDS_DIR = ROOT / "output" / "storyboards"
REFS_DIR        = ROOT / "refs"
LOCATIONS_DIR   = REFS_DIR / "locations"
CHARACTERS_DIR  = REFS_DIR / "characters"
OBJECTS_DIR     = REFS_DIR / "objects"

API_BASE     = "https://googler.fast-gen.ai"
STORAGE_BASE = "https://storage.fast-gen.ai"
MODEL        = "NARWHAL"  # Nano Banana 2

_upload_cache: dict = {}


def load_key() -> str:
    lines = [l.strip() for l in ENV_FILE.read_text().splitlines() if l.strip()]
    return lines[0]


def upload_ref(path: Path, session: requests.Session) -> str:
    key = str(path.resolve())
    if key in _upload_cache:
        print(f"    cached {path.name}")
        return _upload_cache[key]
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}.get(ext,"image/jpeg")
    with open(path, "rb") as f:
        r = session.post(f"{STORAGE_BASE}/upload",
                         files={"file": (path.name, f, mime)}, timeout=60)
    r.raise_for_status()
    data = r.json()
    file_hash = data.get("file_hash") or data.get("file") or data.get("hash")
    if not file_hash:
        raise RuntimeError(f"upload missing file_hash: {data}")
    _upload_cache[key] = file_hash
    print(f"    uploaded {path.name} → {file_hash[:35]}...")
    return file_hash


def poll_operation(op_id: str, session: requests.Session) -> bytes:
    while True:
        time.sleep(4)
        r = session.get(f"{API_BASE}/api/v4/operations/{op_id}", timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        print(f"    status: {status}")
        if status == "success":
            result = data.get("result") or []
            uri = result[0] if isinstance(result, list) else result
            if isinstance(uri, dict):
                uri = uri.get("url") or uri.get("ref") or uri.get("file_hash") or ""
            uri = str(uri)
            if uri.startswith("data:"):
                _, b64 = uri.split(",", 1)
                return base64.b64decode(b64)
            file_hash = uri[5:] if uri.startswith("file:") else uri
            r2 = session.get(f"{STORAGE_BASE}/file/{file_hash}/raw", timeout=120)
            r2.raise_for_status()
            return r2.content
        if status == "error":
            raise RuntimeError(f"Generation error: {data.get('error')}")


def find_image_by_filename(filename: str) -> Optional[Path]:
    for f in LOCATIONS_DIR.glob("*"):
        if f.is_file() and (f.name == filename or f.stem == filename):
            return f
    for f in CHARACTERS_DIR.rglob("*"):
        if f.is_file() and (f.name == filename or f.stem == filename):
            return f
    for f in OBJECTS_DIR.glob("*"):
        if f.is_file() and (f.name == filename or f.stem == filename):
            return f
    return None


def parse_refs_from_prompt(prompt_text: str) -> dict:
    refs = {}
    pattern = re.compile(r'#\s*\[@\]img(\d+)\s*=\s*(.+?)(?:\s*$)', re.MULTILINE)
    for m in pattern.finditer(prompt_text):
        n, filename = m.group(1), m.group(2).strip()
        tag = f"[@]img{n}"
        found = find_image_by_filename(filename)
        if found:
            refs[tag] = found
            print(f"    {tag} → {found.name}")
        else:
            print(f"    WARNING: {tag} = {filename} — file not found!")
    return refs


def build_ordered_ref_hashes(refs: dict, session: requests.Session) -> list:
    sorted_tags = sorted(refs.keys(), key=lambda t: int(re.search(r'\d+', t).group()))
    return [upload_ref(refs[tag], session) for tag in sorted_tags]


def main():
    STORYBOARDS_DIR.mkdir(parents=True, exist_ok=True)
    key = load_key()
    session = requests.Session()
    session.headers.update({"X-API-Key": key})

    if len(sys.argv) > 1:
        block_names = sys.argv[1:]
    else:
        block_names = sorted(p.stem for p in PROMPTS_DIR.glob("*block_*.txt"))

    if not block_names:
        print("No block prompt files found in output/prompts/")
        sys.exit(1)

    print(f"Blocks to generate: {block_names}")
    print(f"Model: {MODEL} (Nano Banana 2)\n")

    for block_name in block_names:
        prompt_file = PROMPTS_DIR / f"{block_name}.txt"
        if not prompt_file.exists():
            print(f"\nSkipping {block_name} — prompt file not found")
            continue

        out_jpg = STORYBOARDS_DIR / f"{block_name}.jpg"
        if out_jpg.exists():
            print(f"\n{block_name} already exists, skipping.")
            continue

        print(f"\n{'='*50}")
        print(f"Generating: {block_name}")

        prompt_text = prompt_file.read_text(encoding="utf-8")
        clean_prompt = "\n".join(
            l for l in prompt_text.splitlines()
            if not l.startswith("===ПРОМПТ_БЛОК") and not l.startswith("# [@]")
        ).strip()

        print("  Detecting refs from prompt...")
        refs = parse_refs_from_prompt(prompt_text)

        ref_hashes = []
        if refs:
            print(f"  Uploading {len(refs)} reference images...")
            ref_hashes = build_ordered_ref_hashes(refs, session)

        payload = {
            "prompt": clean_prompt,
            "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "model": MODEL,
        }
        if ref_hashes:
            payload["reference_images"] = ref_hashes

        print(f"  Prompt length: {len(clean_prompt)} chars | Refs: {len(ref_hashes)}")
        r = session.post(f"{API_BASE}/api/v4/flow/image/generate",
                         json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if not data.get("operation_id"):
            raise RuntimeError(f"No operation_id: {data}")

        op_id = data["operation_id"]
        print(f"  op_id: {op_id}")
        image_bytes = poll_operation(op_id, session)

        out_jpg.write_bytes(image_bytes)
        print(f"  → Saved: output/storyboards/{out_jpg.name} ({len(image_bytes)} bytes)")

    print("\nDone.")


if __name__ == "__main__":
    sys.exit(main())
