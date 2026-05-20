# -*- coding: utf-8 -*-
"""
scenario_parser.py — разбор документа со сценариями на отдельные эпизоды + библию.

Юзкейс: юзер кидает в Studio большой `.txt` или `.md` где в самом верху
лежит библия сериала (сюжет, персонажи, арки), а ниже — серии в виде:

    [ библия сериала здесь — любой текст до первой "ЭПИЗОД 1" ]

    ЭПИЗОД 1: ВСТРЕЧА У СТЕКЛА
    Локация: ...
    Персонажи: ...
    [текст серии]

    ЭПИЗОД 2: ХОЛОДНЫЙ ПРИГОВОР
    [текст серии]
    ...

Парсер режет по маркерам и сохраняет:
    shows/<slug>/bible.txt          — всё что выше первой ЭПИЗОД 1
    shows/<slug>/scenarios/ep01.txt — содержимое ЭПИЗОД 1
    shows/<slug>/scenarios/ep02.txt — содержимое ЭПИЗОД 2
    ...

Чистый Python без Qt — легко тестируется юнитами в tests/.

История: создан 2026-05-05 для долга-фичи A
(drag&drop сценария + bible.txt + per-episode storage).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Расширения файлов которые мы умеем читать.
SUPPORTED_EXTENSIONS = {".txt", ".md", ".rtf", ".docx"}


# ─── Голосовые профили: маркеры старта/конца блока ──────────────────────
# Блок профилей опционально лежит в самом верху документа. Если найден —
# извлекается в shows/<slug>/voices.txt и удаляется из bible.

# Маркер начала ищется в первых 5 непустых строках после нормализации
# (strip → upper → только буквы и пробелы → collapse spaces).
_MARKERS_START = {
    "ГОЛОСОВЫЕ ПРОФИЛИ",
    "ГОЛОСОВЫЕ ПРОФИЛИ ДЛЯ ПЕРСОНАЖЕЙ",
    "ГОЛОСОВЫЕ ПРОФИЛИ ПЕРСОНАЖЕЙ",
    "ГОЛОСОВІ ПРОФІЛІ",
    "ГОЛОСОВІ ПРОФІЛІ ПЕРСОНАЖІВ",
    "VOICE PROFILES",
    "CHARACTER VOICES",
    "ПРОФИЛИ ГОЛОСОВ",
    "ГОЛОСА ПЕРСОНАЖЕЙ",
}

# Маркер конца, вариант B: BIBLE-токен в любом месте строки как
# отдельное слово (после split по неалфавитным символам и upper).
_BIBLE_TOKENS = {"BIBLE", "БИБЛИЯ", "БІБЛІЯ"}

# Маркер конца, вариант A: строка из 3+ одинаковых разделительных
# символов (после strip).
_DIVIDER_CHARS = set("/=═—_")


def _normalize_marker_line(line: str) -> str:
    """Нормализация для match'а MARKERS_START: убирает всё кроме букв и пробелов,
    collapse spaces, upper."""
    upper = line.strip().upper()
    cleaned = []
    for ch in upper:
        if ch.isalpha() or ch == " ":
            cleaned.append(ch)
    return re.sub(r" +", " ", "".join(cleaned)).strip()


def _is_voices_start_line(line: str) -> bool:
    norm = _normalize_marker_line(line)
    if not norm:
        return False
    return any(marker in norm for marker in _MARKERS_START)


def _is_divider_line(line: str) -> bool:
    """Match `///`, `====`, `═══════`, `———`, `___` — 3+ одинаковых символов
    из набора `_DIVIDER_CHARS` (после strip). Смешение символов не match."""
    s = line.strip()
    if len(s) < 3:
        return False
    first = s[0]
    if first not in _DIVIDER_CHARS:
        return False
    return all(c == first for c in s)


def _has_bible_token(line: str) -> bool:
    """True если в строке есть BIBLE/БИБЛИЯ/БІБЛІЯ как отдельный токен
    (split по неалфавитным символам, upper)."""
    # Split на токены по любым неалфавитным символам, оставляя только буквы.
    tokens = re.split(r"[^\w]+", line.upper())
    for tok in tokens:
        # Оставляем в токене только буквы (отбрасываем цифры/подчёркивания).
        letters = "".join(ch for ch in tok if ch.isalpha())
        if letters in _BIBLE_TOKENS:
            return True
    return False


def _find_voices_block(text: str) -> Optional[Tuple[int, int, int, str]]:
    """Ищет блок голосовых профилей в начале документа.

    Возвращает кортеж `(start_line_idx, end_line_idx, cut_until_idx, content)`
    или None если блок не найден / не извлекаем.

    - `start_line_idx` — индекс строки-старта (в split по \\n).
    - `end_line_idx` — индекс строки-конца (разделитель / BIBLE / эпизод).
    - `cut_until_idx` — до какого индекса включительно (exclusive) удалить
      из текста: для разделителя `end_line_idx + 1` (включая разделитель),
      для BIBLE / эпизод-маркера `end_line_idx` (строку конца не трогаем).
    - `content` — содержимое блока (между start и end, без обеих строк,
      strip ведущих/завершающих пустых строк).

    Если start найден но конец не найден — логирует stderr и возвращает None.
    Если контент пустой → возвращает None.
    """
    lines = text.split("\n")

    # Шаг 1: найти строку-старт в первых 5 НЕПУСТЫХ строках.
    start_idx: Optional[int] = None
    nonempty_seen = 0
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        nonempty_seen += 1
        if nonempty_seen > 5:
            break
        if _is_voices_start_line(ln):
            start_idx = i
            break

    if start_idx is None:
        return None

    # Шаг 2: найти строку-конец после старта.
    # Перебираем варианты A (divider) и B (BIBLE-токен) построчно;
    # для варианта C (эпизод-маркер) переиспользуем _EPISODE_MARKER_RE
    # через прогон finditer по обрезанному тексту от строки после старта.
    end_idx: Optional[int] = None
    end_kind: Optional[str] = None  # 'divider' | 'bible' | 'episode'

    for j in range(start_idx + 1, len(lines)):
        ln = lines[j]
        if _is_divider_line(ln):
            end_idx = j
            end_kind = "divider"
            break
        if _has_bible_token(ln):
            end_idx = j
            end_kind = "bible"
            break
        # Вариант C: эпизод-маркер. Проверяем построчно через regex
        # (тот же шаблон что _EPISODE_MARKER_RE, но в single-line режиме).
        if re.match(
            r'^[ \t]*(ЭПИЗОД|СЕРИЯ|EPISODE|ЕПІЗОД)[ \t]+(\d+)[ \t]*[:.\-—]?[ \t]*(.*?)$',
            ln,
            re.IGNORECASE,
        ):
            end_idx = j
            end_kind = "episode"
            break

    if end_idx is None:
        sys.stderr.write(
            "[scenario_parser] voices: маркер начала найден, маркер конца "
            "(разделитель / BIBLE / эпизод) НЕ найден — блок не извлекается.\n"
        )
        return None

    # Шаг 3: контент между start и end (обе строки исключены).
    block_lines = lines[start_idx + 1:end_idx]
    # Strip ведущих и завершающих пустых строк.
    while block_lines and not block_lines[0].strip():
        block_lines.pop(0)
    while block_lines and not block_lines[-1].strip():
        block_lines.pop()
    content = "\n".join(block_lines)
    if not content.strip():
        return None

    # cut_until_idx: для divider — включаем строку конца (она тоже мусор).
    # Для bible/episode — оставляем строку конца в bible.
    cut_until = end_idx + 1 if end_kind == "divider" else end_idx

    return (start_idx, end_idx, cut_until, content)


def read_scenario_file(path: Path) -> str:
    """Читает документ со сценариями. Поддержка .txt/.md/.rtf (как plain text)
    и .docx (через python-docx — извлекаются абзацы, склеиваются \\n).

    Для plain text сначала пробуем utf-8, fallback cp1251 (часто старые
    русские .txt в Windows-кодировке).

    Raises:
        ValueError — если расширение не поддерживается.
        OSError / Exception — наружу для caller'а.
    """
    ext = path.suffix.lower()
    if ext == ".docx":
        # Ленивая загрузка чтобы не тянуть python-docx при работе с txt
        from docx import Document  # type: ignore
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext in (".txt", ".md", ".rtf"):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="cp1251")
    raise ValueError(f"Неподдерживаемое расширение: {ext}")


# ─── Структуры данных ────────────────────────────────────────────────────

@dataclass
class ParsedEpisode:
    """Одна серия из документа."""
    ep_num: int               # «ЭПИЗОД 1» → 1
    title: str                # «ВСТРЕЧА У СТЕКЛА» (что после двоеточия)
    content: str              # полный текст серии включая заголовок


@dataclass
class ParsedDoc:
    """Результат разбора документа."""
    bible: str = ""                         # всё до первой «ЭПИЗОД 1», trimmed
    episodes: List[ParsedEpisode] = field(default_factory=list)
    voices: Optional[str] = None            # блок голосовых профилей, None если не найден


# ─── Парсинг ──────────────────────────────────────────────────────────────

# Маркер начала серии. Поддерживает русский, украинский, английский.
# Примеры что матчится:
#   ЭПИЗОД 1: ВСТРЕЧА У СТЕКЛА
#   эпизод 21: тройное дно
#   СЕРИЯ 5. Название
#   EPISODE 12 - Title
#   ЕПІЗОД 3: Назва
# Группы: (1) keyword, (2) номер, (3) заголовок (может быть пустым)
_EPISODE_MARKER_RE = re.compile(
    # ВАЖНО: используем [ \t]* (а не \s*), чтобы не «проглатывать»
    # переносы строк — иначе при формате «ЭПИЗОД 1\n\nЖанр: …» парсер
    # схватит «Жанр: …» как название эпизода (баг 2026-05-05).
    r'^[ \t]*(ЭПИЗОД|СЕРИЯ|EPISODE|ЕПІЗОД)[ \t]+(\d+)[ \t]*[:.\-—]?[ \t]*(.*?)$',
    re.IGNORECASE | re.MULTILINE,
)


def parse_episodes_doc(text: str) -> ParsedDoc:
    """Разбирает один документ на (bible, [episodes], voices).

    Если ни одного маркера серии не найдено — весь текст становится bible,
    episodes пустой. Это позволяет юзеру сначала загрузить только библию
    без серий, а потом докинуть серии отдельным файлом.

    Если в шапке документа найден блок голосовых профилей (маркер старта
    из `_MARKERS_START` в первых 5 непустых строках + маркер конца:
    разделитель / BIBLE / эпизод-маркер) — он вырезается из текста и
    кладётся в `voices`. Подробнее — в `_find_voices_block`.
    """
    if not text:
        return ParsedDoc()

    # Шаг 0: попытка извлечь блок голосовых профилей из шапки.
    voices: Optional[str] = None
    block = _find_voices_block(text)
    if block is not None:
        start_idx, _end_idx, cut_until, content = block
        voices = content
        lines = text.split("\n")
        # Вырезаем строки [start_idx, cut_until) — оставшийся текст
        # уходит в обычный pipeline (bible + episodes).
        remaining = lines[:start_idx] + lines[cut_until:]
        text = "\n".join(remaining)

    matches = list(_EPISODE_MARKER_RE.finditer(text))
    if not matches:
        return ParsedDoc(bible=text.strip(), episodes=[], voices=voices)

    bible = text[:matches[0].start()].strip()

    episodes: List[ParsedEpisode] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        ep_num = int(m.group(2))
        title = (m.group(3) or "").strip()
        episodes.append(ParsedEpisode(ep_num=ep_num, title=title, content=content))

    return ParsedDoc(bible=bible, episodes=episodes, voices=voices)


# ─── Сохранение на диск ──────────────────────────────────────────────────

def save_parsed_doc(project_root: Path, slug: str, parsed: ParsedDoc) -> Dict:
    """Сохраняет распарсенный документ в структуру сериала.

    - bible (если непустой) → `shows/<slug>/bible.txt`
    - каждая серия → `shows/<slug>/scenarios/epNN.txt` (zero-padded до 2)
      Если ep_num >= 100 — остаётся как есть (ep100.txt).

    Возвращает summary:
      {
        'bible_saved': bool,
        'voices_saved': bool,
        'episodes_saved': int,
        'episode_files': ['ep01.txt', 'ep02.txt', ...],
      }

    Существующие файлы перезаписываются. Это намеренно — юзер обычно
    кидает обновлённый документ и хочет получить актуальные данные.
    """
    show_root = project_root / "shows" / slug
    if not show_root.exists():
        raise FileNotFoundError(f"Сериал не существует: shows/{slug}")

    summary: Dict = {
        'bible_saved': False,
        'voices_saved': False,
        'episodes_saved': 0,
        'episode_files': [],
    }

    # Bible
    if parsed.bible:
        (show_root / "bible.txt").write_text(parsed.bible + "\n", encoding="utf-8")
        summary['bible_saved'] = True

    # Voices (per-show голосовые профили)
    if parsed.voices is not None:
        (show_root / "voices.txt").write_text(parsed.voices + "\n", encoding="utf-8")
        summary['voices_saved'] = True

    # Episodes
    scenarios_dir = show_root / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    for ep in parsed.episodes:
        # epNN.txt с zero-pad до 2 цифр (ep01, ep02 ... ep99)
        # Для номеров >= 100 уходит без pad — ep100.txt и т.д.
        if ep.ep_num < 100:
            fname = f"ep{ep.ep_num:02d}.txt"
        else:
            fname = f"ep{ep.ep_num}.txt"
        (scenarios_dir / fname).write_text(ep.content + "\n", encoding="utf-8")
        summary['episodes_saved'] += 1
        summary['episode_files'].append(fname)

    return summary
