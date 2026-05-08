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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


# Расширения файлов которые мы умеем читать.
SUPPORTED_EXTENSIONS = {".txt", ".md", ".rtf", ".docx"}


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
    """Разбирает один документ на (bible, [episodes]).

    Если ни одного маркера серии не найдено — весь текст становится bible,
    episodes пустой. Это позволяет юзеру сначала загрузить только библию
    без серий, а потом докинуть серии отдельным файлом.
    """
    if not text:
        return ParsedDoc()

    matches = list(_EPISODE_MARKER_RE.finditer(text))
    if not matches:
        return ParsedDoc(bible=text.strip(), episodes=[])

    bible = text[:matches[0].start()].strip()

    episodes: List[ParsedEpisode] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        ep_num = int(m.group(2))
        title = (m.group(3) or "").strip()
        episodes.append(ParsedEpisode(ep_num=ep_num, title=title, content=content))

    return ParsedDoc(bible=bible, episodes=episodes)


# ─── Сохранение на диск ──────────────────────────────────────────────────

def save_parsed_doc(project_root: Path, slug: str, parsed: ParsedDoc) -> Dict:
    """Сохраняет распарсенный документ в структуру сериала.

    - bible (если непустой) → `shows/<slug>/bible.txt`
    - каждая серия → `shows/<slug>/scenarios/epNN.txt` (zero-padded до 2)
      Если ep_num >= 100 — остаётся как есть (ep100.txt).

    Возвращает summary:
      {
        'bible_saved': bool,
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
        'episodes_saved': 0,
        'episode_files': [],
    }

    # Bible
    if parsed.bible:
        (show_root / "bible.txt").write_text(parsed.bible + "\n", encoding="utf-8")
        summary['bible_saved'] = True

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
