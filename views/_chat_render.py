# -*- coding: utf-8 -*-
"""
views/_chat_render.py — общие хелперы рендера чата эпизода.

Сейчас содержит парсер встроенных маркеров генерации (`[[GEN:type:name:description]]`).
В будущем сюда же переедет общая логика рендера строк (Долг 1 в _session_log.md
про цвета строк планирования).

История: создано 2026-05-04 для sub-MVP «кнопка автономной генерации в чате».
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Tuple


# Регулярка для маркера [[GEN:type:name:description]].
# - type — `location` / `object` / `character` (ASCII, нижний регистр)
# - name — slug в snake_case (ASCII)
# - description — произвольный текст (любые символы кроме `]`), может содержать
#   пробелы, кириллицу, спецсимволы. Нужен AI-агенту чтобы написать промпт.
#
# Маркер встраивается AI-агентом после строк типа «- ✗ <name> — нужно
# генерировать». UI парсит, скрывает из видимого лога и эмитит сигнал
# наверх (в EpisodeChatView/NewEpisodeView), который добавляет элемент
# в очередь генерации (sub-MVP: одна кнопка за раз).
GEN_MARKER_RE = re.compile(
    r'\[\[GEN:(?P<type>[a-z_]+):(?P<name>[a-z0-9_]+):(?P<desc>[^\]]*)\]\]'
)


class GenMarker(NamedTuple):
    """Один распарсенный GEN-маркер из chunk'а ответа AI."""
    type: str         # location / object / character
    name: str         # slug папки (prison_phone_hallway)
    description: str  # для промпта web-search / pipeline.py
    display: str = ""  # человекочитаемое имя «Муж» (если slug — транслит)


def parse_gen_markers(text: str) -> Tuple[str, List[GenMarker]]:
    """Извлекает все GEN-маркеры из текста.

    Возвращает кортеж `(clean_text, markers)`:
      • `clean_text` — текст с удалёнными маркерами (для отображения в логе).
      • `markers` — список `GenMarker` в порядке появления.

    Если маркеров нет — возвращает `(text, [])`. Безопасно для пустых строк
    и неполных маркеров (regex не матчит → текст не меняется).

    Не использует `re.IGNORECASE` — формат строгий, AI должен соблюдать
    точно, иначе маркер игнорируется и юзер увидит сырой текст вместо
    кнопки (graceful degradation, не падаем)."""
    if not text:
        return text, []
    markers: List[GenMarker] = []
    for m in GEN_MARKER_RE.finditer(text):
        markers.append(GenMarker(
            type=m.group('type').strip(),
            name=m.group('name').strip(),
            description=m.group('desc').strip(),
        ))
    if not markers:
        return text, []
    clean = GEN_MARKER_RE.sub('', text)
    return clean, markers


# ── Fallback-парсер: AI иногда «забывает» вставить машиночитаемые
# [[GEN:...]] маркеры и пишет просто текстом «- ✗ name — рефа нет».
# В таком случае Studio оставался без кнопок, юзеру приходилось вручную
# просить AI повторить с маркерами. Чтобы не зависеть от каприза AI,
# мы сами сканируем полный ответ ПОСЛЕ завершения потока — ищем строки
# вида «- ✗ slug — описание» под секционными заголовками
# «ЛОКАЦИИ:» / «ОБЪЕКТЫ:» (LOCATIONS / OBJECTS — для англ. ответов).
# Персонажи (CHARACTERS / ПЕРСОНАЖИ) — пропускаем, у них другой пайплайн
# (через вкладку «Актёры»).

# Заголовки секций — определяем `current_type` для последующих строк.
# 2026-05-07: разрешён опциональный markdown-bold (`**ЛОКАЦИИ:**`) — AI стал
# писать заголовки в bold, и старый regex (без `\*`) их не матчил → секция
# не определялась → ✓/✗ строки ниже игнорировались → парсер возвращал 0
# маркеров → карточки рефов в чате не появлялись вообще. Делаем `**` опц.
# 2026-05-08: добавлены украинские варианты (ЛОКАЦІЇ / ОБ'ЄКТИ / ПЕРСОНАЖІ).
# Когда юзер выбирает 🇺🇦 в шапке — analyst пишет ответ по-украински, и
# заголовки выходят как `ЛОКАЦІЇ:` вместо `ЛОКАЦИИ:`. Без этих вариантов
# fallback-парсер не находил секции → кнопки автогенерации не появлялись.
# Апостроф в `ОБ'ЄКТИ` — два типа: ASCII `'` (U+0027) и юникодный `'`
# (U+2019), оба покрываем альтернативой.
_SECTION_LOCATION_RE = re.compile(
    r"^\s*\*{0,2}\s*(?:ЛОКАЦИИ|LOCATIONS|ЛОКАЦІЇ)\b", re.IGNORECASE)
_SECTION_OBJECT_RE = re.compile(
    r"^\s*\*{0,2}\s*(?:ОБЪЕКТЫ|OBJECTS|ОБ['’]ЄКТИ)\b", re.IGNORECASE)
_SECTION_CHARACTER_RE = re.compile(
    r"^\s*\*{0,2}\s*(?:ПЕРСОНАЖИ|CHARACTERS|ПЕРСОНАЖІ)\b", re.IGNORECASE)

# Строка элемента: «- ✗ name — описание» (или с другим dash/separator).
# `name` — slug ИЛИ русское/украинское слово. Если не-ASCII —
# транслитерируем в slug ниже через storyboard_app.transliterate_for_filename.
# `\w` с re.UNICODE матчит латиницу, кириллицу, цифры, подчёркивание.
_FALLBACK_LINE_RE = re.compile(
    r'^\s*[-•*]\s*✗\s*(?P<name>[\w-]+)'
    r'\s*(?:\((?P<orig>[^)]+)\))?'   # опц. (оригинал) если AI следует промпту
    r'\s*[—:\-–]\s*(?P<desc>.+?)\s*$',
    re.UNICODE
)

# 2026-05-10: ИНЛАЙН-формат когда AI пишет «summary»-шаг вместо секций.
# Пример (Opus 4.7 на ep8 в реальном чате):
#   «- ✗ НУЖНО СГЕНЕРИРОВАТЬ: private_house_living_room (локация),
#                              taser (объект)»
#   «- ✗ НУЖЕН ОТДЕЛЬНО (через «Актёры»): policeman»
# Парсер ниже распознаёт обе формы и извлекает (name, type) пары.
_INLINE_GEN_HEADER_RE = re.compile(
    r'^\s*[-•*]\s*✗\s*(?:НУЖНО СГЕНЕРИРОВАТЬ|TO GENERATE|TO BE GENERATED)\b'
    r'[^:]*:\s*(?P<rest>.+)$',
    re.IGNORECASE | re.UNICODE)
_INLINE_CHAR_HEADER_RE = re.compile(
    r'^\s*[-•*]\s*✗\s*(?:НУЖЕН ОТДЕЛЬНО|NEEDED SEPARATELY)\b'
    r'[^:]*:\s*(?P<rest>.+)$',
    re.IGNORECASE | re.UNICODE)
# Один item: `name (тип)`. Тип определяет gen_type.
# Пример: `private_house_living_room (локация)`, `taser (объект)`,
# `policeman (персонаж)`. Без скобок — просто имя (для CHAR_HEADER).
_INLINE_ITEM_RE = re.compile(
    r'(?P<name>[a-zа-яё][\w-]*)'
    r'(?:\s*\((?P<typ>локация|location|объект|object|персонаж|character)\))?',
    re.IGNORECASE | re.UNICODE)
_TYPE_MAP = {
    'локация': 'location', 'location': 'location',
    'объект': 'object', 'object': 'object',
    'персонаж': 'character', 'character': 'character',
}


def _to_slug(raw: str) -> str:
    """Превращает имя в ASCII snake_case slug. Для не-ASCII зовёт
    `storyboard_app.transliterate_for_filename`. Lazy-import чтобы
    избежать circular dep при PyInstaller-frozen."""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.isascii():
        return raw.lower()
    try:
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod,
                                              'transliterate_for_filename'):
            tr = main_mod.transliterate_for_filename
        else:
            from storyboard_app import transliterate_for_filename as tr
        slug = tr(raw, max_words=2, max_len=30) or ""
    except Exception:
        slug = ""
    if not slug:
        # Fallback: оставляем только ASCII-символы
        slug = re.sub(r'[^a-z0-9_-]', '_', raw.lower()).strip('_-')
    return slug or raw.lower()

# Phase 2 hotfix #23: для ✓-строк персонажей имя берём из пути
# `refs/characters/<name>/<file>.jpg` который AI пишет в строке —
# отдельная regex не нужна, парсится inline в synthesize_gen_markers.


def synthesize_gen_markers(full_text: str) -> List[GenMarker]:
    """Fallback-парсер: вытаскивает GenMarker'ы из «человеческого» ответа
    AI без `[[GEN:...]]` маркеров. Идёт построчно, отслеживает текущую
    секцию по заголовкам ЛОКАЦИИ:/ОБЪЕКТЫ:, для каждой строки `- ✗ name —
    описание` создаёт `GenMarker(type=section_type, name=name, description=desc)`.

    Используется ТОЛЬКО на завершённом ответе потока (не chunk-by-chunk),
    т.к. парсер контекстный (нужны заголовки выше). Безопасно для пустых
    строк и текста без секций — возвращает пустой список.
    """
    if not full_text:
        return []
    markers: List[GenMarker] = []
    current_type: str = ''  # location / object / character / ''
    seen_names: set = set()  # дедуп — один name не должен попасть дважды
    for line in full_text.splitlines():
        # 2026-05-10: inline-формат «- ✗ НУЖНО СГЕНЕРИРОВАТЬ: name1
        # (локация), name2 (объект)» — Opus иногда пишет такой summary
        # вместо section-headers. Распознаём ДО section-проверок чтобы
        # не сбросить current_type зря.
        m_inline = _INLINE_GEN_HEADER_RE.match(line)
        if m_inline:
            rest = m_inline.group('rest')
            for it in _INLINE_ITEM_RE.finditer(rest):
                raw_name = it.group('name') or ''
                typ_raw = (it.group('typ') or '').lower()
                gen_type = _TYPE_MAP.get(typ_raw)
                if not raw_name or not gen_type:
                    continue
                slug = (_to_slug(raw_name) if not raw_name.isascii()
                        else raw_name.lower())
                if not slug or slug in seen_names:
                    continue
                seen_names.add(slug)
                markers.append(GenMarker(
                    type=gen_type, name=slug, description='', display=''))
            continue
        m_char_inline = _INLINE_CHAR_HEADER_RE.match(line)
        if m_char_inline:
            rest = m_char_inline.group('rest')
            for it in _INLINE_ITEM_RE.finditer(rest):
                raw_name = it.group('name') or ''
                typ_raw = (it.group('typ') or '').lower()
                gen_type = _TYPE_MAP.get(typ_raw, 'character')  # default
                if not raw_name:
                    continue
                slug = (_to_slug(raw_name) if not raw_name.isascii()
                        else raw_name.lower())
                if not slug or slug in seen_names:
                    continue
                seen_names.add(slug)
                markers.append(GenMarker(
                    type=gen_type, name=slug, description='', display=''))
            continue
        if _SECTION_LOCATION_RE.match(line):
            current_type = 'location'
            continue
        if _SECTION_OBJECT_RE.match(line):
            current_type = 'object'
            continue
        if _SECTION_CHARACTER_RE.match(line):
            current_type = 'character'
            continue
        # Пустая строка / другой заголовок (например MANIFEST) — секция
        # обрывается. AI часто пишет «Manifest записан» между секциями.
        if line.strip() and line.strip().endswith(':') and not line.lstrip().startswith(('-', '•', '*')):
            current_type = ''
            continue
        if not current_type:
            continue
        # Phase 2 hotfix #11 (Долг 12): для character тоже создаём
        # маркеры — юзер будет управлять через те же 3 кнопки
        # (📁 Выбрать существующий / 🚫 Не нужен / 🎨 Сгенерировать).
        # «Сгенерировать» для character пока возвращает graceful error
        # в AutonomousGenThread — для них юзер использует «Выбрать
        # существующий» (открывает refs/characters/<name>/) или вкладку
        # «Актёры» для создания нового рефа.
        m = _FALLBACK_LINE_RE.match(line)
        if m:
            raw_name = m.group('name').strip()
            orig = (m.group('orig') or '').strip()
            # 2026-05-05: имя может быть кириллицей («- ✗ Муж») или
            # ASCII-slug с оригиналом в скобках («- ✗ muzh (Муж)»).
            # Slug — то что используется в filesystem (папка
            # `refs/<type>/<slug>/`). Display — человекочитаемое имя
            # которое юзер видит на карточке.
            if not raw_name.isascii():
                # Случай «- ✗ Муж» — name = транслит, display = «Муж»
                slug = _to_slug(raw_name)
                display = raw_name
            else:
                # Случай «- ✗ muzh (Муж)» — name = muzh, display = «Муж»
                slug = raw_name.lower()
                display = orig if orig else ""
                # 2026-05-07 (1а): для location/object содержимое скобок —
                # это UI-описание, а НЕ display name. Не записываем в display
                # чтобы карточка GenButton сохранила slug как лейбл.
                # Для character: формат «(Имя — короткая роль)» — берём
                # только часть до длинного тире `—` как display.
                if current_type in ('location', 'object'):
                    display = ""
                elif current_type == 'character' and display and '—' in display:
                    display = display.split('—', 1)[0].strip()
            if not slug or slug in seen_names:
                continue
            seen_names.add(slug)
            desc = m.group('desc').strip()
            markers.append(GenMarker(
                type=current_type,
                name=slug,
                description=desc,
                display=display,
            ))
            continue
        # Phase 2 hotfix #23/#26: для ВСЕХ типов парсим и ✓ строки тоже.
        # Юзер хочет видеть карточки даже для тех у кого реф уже есть —
        # чтобы переназначить (у Лоры может быть 2 рефа в тюремной
        # одежде; локация тюрьмы может иметь несколько вариантов).
        #
        # 2026-05-07: новый формат ✓ строк БЕЗ пути в скобках:
        #   `- ✓ lawyer_office (кабинет адвоката) — реф есть — сцены 1-4`
        # (раньше было `(refs/locations/lawyer_office.jpg)` — теперь юзер
        # просил убрать дубликат пути после «реф есть»). Парсер берёт slug
        # из начала строки. Для обратной совместимости со старыми чатами,
        # где путь ещё есть — пробуем path-regex первым.
        path_re = {
            'location': r'refs/locations/([a-z][a-z0-9_]*)',
            'object': r'refs/objects/([a-z][a-z0-9_]*)',
            'character': r'refs/characters/([a-z][a-z0-9_]*)',
        }.get(current_type)
        if path_re:
            name = ""
            m_path = re.search(path_re, line)
            if m_path:
                name = m_path.group(1).strip().lower()
            else:
                # Новый формат — slug в начале строки после `- ✓ `.
                m_head = re.match(
                    r'^\s*[-•*]\s*✓\s*([a-z][a-z0-9_]*)\b',
                    line, re.UNICODE)
                if m_head:
                    name = m_head.group(1).strip().lower()
            if not name:
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            # 2026-05-07: извлекаем display и desc корректно — `—` внутри
            # скобок (например `(Дэвид — главный герой)`) НЕ должен резать
            # head. Берём slug + опц. parens отдельно через regex.
            stripped = line.strip()
            display = ""
            # Форматы:
            #   • «- ✓ slug — реф есть [— дальше]»       → no parens
            #   • «- ✓ slug (описание/Имя — роль) — реф есть» → with parens
            #   • «- ✓ Лора — ...»                        → cyrillic name
            m_full = re.match(
                r'^\s*[-•*]\s*✓\s*'
                r'(?P<head_name>[\w-]+)'
                r'\s*(?:\((?P<paren>[^)]+)\))?'
                r'\s*(?:[—:\-–]\s*(?P<rest>.*))?$',
                stripped, re.UNICODE)
            paren_inner = ""
            head_name_raw = ""
            rest_text = ""
            if m_full:
                head_name_raw = m_full.group('head_name') or ""
                paren_inner = (m_full.group('paren') or "").strip()
                rest_text = (m_full.group('rest') or "").strip()
            # Display name:
            # 2026-05-07 (1а):
            #   • location/object: parens = UI-описание (НЕ display).
            #   • character: parens = «Имя — роль» → display = «Имя».
            #   • Если head_name — кириллица (не ASCII) и не совпадает с
            #     slug — берём head_name как display.
            if paren_inner and not paren_inner.isascii():
                if current_type == 'character':
                    inner = paren_inner
                    if '—' in inner:
                        inner = inner.split('—', 1)[0].strip()
                    display = inner
                # location / object — display не выставляем
            elif (head_name_raw and not head_name_raw.isascii()
                    and head_name_raw.lower() != name):
                display = head_name_raw
            desc = rest_text or stripped
            markers.append(GenMarker(
                type=current_type,
                name=name,
                description=desc[:200],
                display=display,
            ))
    return markers
