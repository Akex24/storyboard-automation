#!/usr/bin/env python3
"""Генерация иконки приложения LUMZ Storyboard Studio.

Запуск: python3 assets/build_icon.py

Создаёт:
  • assets/icon.png   — 1024×1024
  • assets/icon.ico   — multi-size для Windows (16/32/48/64/128/256)
  • assets/icon.icns  — для macOS (через iconutil, только на Mac)

Дизайн (по ТЗ):
  • Холст 1024×1024
  • Фон: диагональный градиент #2a0a40 → #1a0828 → #300808
  • Полупрозрачная диагональная полоса (белая, opacity 0.03)
  • LUMZ — Arial Black, 380px, белый, letter-spacing -8px
  • Красная линия под текстом — gradient #ff4020 → #ff2060, rounded
  • STUDIO — 76px, белый opacity 0.3, letter-spacing 30px
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


W = H = 1024
ASSETS = Path(__file__).parent


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def lerp(c1, c2, t: float):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def find_font(candidates, size: int):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def diagonal_gradient(w: int, h: int):
    """Per-pixel диагональный градиент 3 стопа.
    Использует bytearray для скорости (значительно быстрее putpixel)."""
    c1 = hex_to_rgb("#2a0a40")
    c2 = hex_to_rgb("#1a0828")
    c3 = hex_to_rgb("#300808")

    img = Image.new("RGBA", (w, h))
    pixels = bytearray(w * h * 4)

    denom = w + h - 2
    for y in range(h):
        for x in range(w):
            t = (x + y) / denom
            if t < 0.5:
                r, g, b = lerp(c1, c2, t * 2)
            else:
                r, g, b = lerp(c2, c3, (t - 0.5) * 2)
            i = (y * w + x) * 4
            pixels[i]   = r
            pixels[i+1] = g
            pixels[i+2] = b
            pixels[i+3] = 255
    img.frombytes(bytes(pixels))
    return img


def draw_text_with_spacing(draw, xy, text, font, fill, spacing: int = 0):
    """Рисует текст с заданным letter-spacing (Pillow не поддерживает нативно)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), ch, font=font)
        x += (bbox[2] - bbox[0]) + spacing


def measure_text_with_spacing(draw, text, font, spacing: int = 0):
    total = 0
    for ch in text:
        bbox = draw.textbbox((0, 0), ch, font=font)
        total += (bbox[2] - bbox[0])
    total += spacing * (len(text) - 1)
    return total


def gradient_rounded_rect(w: int, h: int, radius: int, c1, c2):
    """Прямоугольник со скруглёнными углами и горизонтальным градиентом."""
    grad = Image.new("RGBA", (w, h))
    px = bytearray(w * h * 4)
    for x in range(w):
        t = x / max(1, w - 1)
        r, g, b = lerp(c1, c2, t)
        for y in range(h):
            i = (y * w + x) * 4
            px[i]   = r
            px[i+1] = g
            px[i+2] = b
            px[i+3] = 255
    grad.frombytes(bytes(px))

    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask=mask)
    return out


def build_icon() -> Image.Image:
    print("→ Фон (диагональный градиент)…")
    img = diagonal_gradient(W, H)

    print("→ Полупрозрачная диагональная полоса…")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon(
        [(0, 680), (1024, 256), (1024, 426), (0, 854)],
        fill=(255, 255, 255, int(255 * 0.03)),
    )
    img = Image.alpha_composite(img, overlay)

    draw = ImageDraw.Draw(img)

    print("→ Текст LUMZ (Arial Black, авто-подгонка под ширину)…")
    bold_candidates = [
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/Library/Fonts/Arial Black.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        # Windows
        "C:/Windows/Fonts/ariblk.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    text = "LUMZ"
    spacing = -8
    # Подгоняем размер шрифта так чтобы LUMZ занимал ~684px (как ширина красной линии)
    target_w = 684
    font_size = 380
    while font_size > 60:
        f = find_font(bold_candidates, font_size)
        w_now = measure_text_with_spacing(draw, text, f, spacing)
        if w_now <= target_w:
            font_lumz = f
            break
        font_size -= 8
    else:
        font_lumz = find_font(bold_candidates, 60)

    text_w = measure_text_with_spacing(draw, text, font_lumz, spacing)
    bbox = draw.textbbox((0, 0), text, font=font_lumz)
    text_h = bbox[3] - bbox[1]
    x = (W - text_w) // 2
    y = (H - text_h) // 2 - 90   # сдвиг вверх для красной полосы и STUDIO
    draw_text_with_spacing(draw, (x, y), text, font_lumz,
                           fill=(255, 255, 255, 255), spacing=spacing)

    print("→ Красная градиентная линия под LUMZ…")
    line_x, line_y, line_w, line_h, line_r = 170, 650, 684, 26, 13
    line = gradient_rounded_rect(
        line_w, line_h, line_r,
        hex_to_rgb("#ff4020"), hex_to_rgb("#ff2060"),
    )
    img.paste(line, (line_x, line_y), line)

    print("→ Текст STUDIO (76px, opacity 0.3)…")
    regular_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    font_studio = find_font(regular_candidates, 76)
    studio_text = "STUDIO"
    studio_spacing = 30
    sw = measure_text_with_spacing(draw, studio_text, font_studio, studio_spacing)
    sx = (W - sw) // 2
    sy = 730
    draw_text_with_spacing(
        draw, (sx, sy), studio_text, font_studio,
        fill=(255, 255, 255, int(255 * 0.3)), spacing=studio_spacing,
    )

    return img


def save_png(img: Image.Image, path: Path):
    print(f"→ Сохраняю {path.name}")
    img.save(path, "PNG")


def save_ico(img: Image.Image, path: Path):
    print(f"→ Сохраняю {path.name} (multi-size 16/32/48/64/128/256)")
    img.save(
        path, "ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def save_icns(img: Image.Image, path: Path):
    """ICNS через macOS iconutil. На Windows/Linux пропускаем (нет iconutil)."""
    if not shutil.which("iconutil"):
        print("→ iconutil не найден (не macOS) — пропускаю icon.icns")
        return
    iconset = path.parent / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    sizes = [16, 32, 64, 128, 256, 512]
    for s in sizes:
        img.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
        img.resize((s * 2, s * 2), Image.LANCZOS).save(
            iconset / f"icon_{s}x{s}@2x.png")
    print(f"→ Запускаю iconutil → {path.name}")
    r = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"   iconutil error: {r.stderr}")
    shutil.rmtree(iconset, ignore_errors=True)


def main():
    img = build_icon()
    save_png(img, ASSETS / "icon.png")
    save_ico(img, ASSETS / "icon.ico")
    save_icns(img, ASSETS / "icon.icns")
    print("Готово.")


if __name__ == "__main__":
    sys.exit(main())
