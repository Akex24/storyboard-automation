"""Python pre-filter механических правил Validator'а монтажной карты.

Покрывает 10 из 14 правил Validator'а без обращения к AI. AI остаются:
#6 (timing math), #7а (voice profile), #9 (forbidden_phrase),
#12 (растяжка), #14 (duplicate shots).

Формат возвращаемых ошибок идентичен AI Validator'у —
{code, where, details} — чтобы Editor мог их потребить без отличий.

КРИТИЧНО: каждая _check_* функция содержит ЦИТАТУ соответствующего
пункта из `_VALIDATOR_JSON_TAIL` (agents/montage_prompts.py). При
изменении текста правила в .md/промпте — синхронно править docstring.
Источник истины: _VALIDATOR_JSON_TAIL пункты 1-14.
"""
from __future__ import annotations

from typing import Tuple, List, Set, Dict, Any


VALID_SPEECH_TYPES: Set[str] = {"fast", "normal", "emotional", "slow"}
MAX_SHOTS_PER_BLOCK = 4
MAX_BLOCK_DURATION = 15
MAX_BLOCKS = 7


PREFILTER_RULES: Set[str] = {
    "rule_1", "rule_2", "rule_3", "rule_4", "rule_5",
    "rule_7", "rule_8", "rule_10", "rule_11", "rule_13",
}


def _err(code: str, where: str, details: str) -> Dict[str, str]:
    return {"code": code, "where": where, "details": details}


def _block_total(b: Dict[str, Any]) -> int:
    return sum(int(s.get("duration_sec") or 0) for s in (b.get("shots") or []))


def _check_block_count(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#13 — Количество блоков 4-7 (целевое 5-6). Если >7 — "too_many_blocks".

    Цитата из _VALIDATOR_JSON_TAIL пункт 13:
      "Количество блоков 4-7 (целевое 5-6). Если >7 блоков — ошибка
       \"too_many_blocks: <N> блоков, дроби beats слишком мелко\""

    По решению Алекса (2026-05-14): нижнюю границу <4 НЕ ловим
    (в исходной инструкции нет явной ошибки про мало блоков).
    """
    blocks = card.get("blocks") or []
    n = len(blocks)
    if n > MAX_BLOCKS:
        return [_err(
            "too_many_blocks",
            "card.blocks",
            f"{n} блоков, дроби beats слишком мелко. Объедини "
            f"соседние блоки в один beat — описания блоков ниже "
            f"подскажут какие слить.",
        )]
    return []


def _check_block_shot_count(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#1 — Каждый блок ≤4 шота — иначе "block_N_too_many_shots".

    Цитата из _VALIDATOR_JSON_TAIL пункт 1:
      "Каждый блок ≤4 шота — иначе ошибка \"block_N_too_many_shots\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        shots = b.get("shots") or []
        if len(shots) > MAX_SHOTS_PER_BLOCK:
            out.append(_err(
                f"block_{bn}_too_many_shots",
                f"blocks[{i}].shots",
                f"{len(shots)} шотов в блоке (макс {MAX_SHOTS_PER_BLOCK}). "
                f"Слей соседние шоты или вынеси часть в новый блок (если "
                f"общий хронометраж позволяет).",
            ))
    return out


def _check_block_duration(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#2 — Каждый блок ≤15 сек суммарно — иначе "block_N_over_15s".

    Цитата из _VALIDATOR_JSON_TAIL пункт 2:
      "Каждый блок ≤15 сек суммарно — иначе \"block_N_over_15s\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        total = _block_total(b)
        if total > MAX_BLOCK_DURATION:
            out.append(_err(
                f"block_{bn}_over_15s",
                f"blocks[{i}]",
                f"Сумма duration_sec блока = {total}с (макс "
                f"{MAX_BLOCK_DURATION}с). Сократи длительности "
                f"безголосовых шотов в этом блоке до минимума по "
                f"ПРАВИЛУ 5 (микрокадр 1-2с, простое действие 2с, "
                f"эмоция 3с, сложное действие 4-5с). Если безголосовые "
                f"уже на минимуме — пересмотри тайминг шота с репликой.",
            ))
    return out


def _check_shot_numbering(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#4 — Нумерация SHOT в каждом блоке начинается с 1 — иначе
    "block_N_shot_numbering".

    Цитата из _VALIDATOR_JSON_TAIL пункт 4:
      "Нумерация SHOT в каждом блоке начинается с 1 — иначе
       \"block_N_shot_numbering\"."

    Расширяем: помимо начала с 1, проверяем последовательность 1..N
    без пропусков и повторов.
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        shots = b.get("shots") or []
        expected = list(range(1, len(shots) + 1))
        actual = [s.get("n") for s in shots]
        if actual != expected:
            out.append(_err(
                f"block_{bn}_shot_numbering",
                f"blocks[{i}].shots",
                f"Нумерация шотов {actual} — ожидалось {expected} "
                f"(подряд с 1).",
            ))
    return out


def _check_dialog_languages(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#5 — Реплики имеют и `ru`, и `en` поле — иначе
    "block_N_shot_M_dialog_missing_lang".

    Цитата из _VALIDATOR_JSON_TAIL пункт 5:
      "Реплики имеют и `ru`, и `en` поле — иначе
       \"block_N_shot_M_dialog_missing_lang\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        for j, s in enumerate(b.get("shots") or []):
            sn = s.get("n", j + 1)
            d = s.get("dialog")
            if not d:
                continue
            ru = (d.get("ru") or "").strip()
            en = (d.get("en") or "").strip()
            if not ru or not en:
                missing = []
                if not ru:
                    missing.append("ru")
                if not en:
                    missing.append("en")
                out.append(_err(
                    f"block_{bn}_shot_{sn}_dialog_missing_lang",
                    f"blocks[{i}].shots[{j}].dialog",
                    f"Отсутствует поле(я): {', '.join(missing)}.",
                ))
    return out


def _check_speech_type_enum(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#7 — Поле `speech_type` каждой реплики — одно из:
    fast | normal | emotional | slow. Иначе "block_N_shot_M_invalid_speech_type".

    Цитата из _VALIDATOR_JSON_TAIL пункт 7:
      "Поле `speech_type` каждой реплики — одно из: fast | normal |
       emotional | slow. Иначе \"block_N_shot_M_invalid_speech_type\"."

    Совместимость со speech_type vs voice profile (#7а) — оставляем AI.
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        for j, s in enumerate(b.get("shots") or []):
            sn = s.get("n", j + 1)
            d = s.get("dialog")
            if not d:
                continue
            st = d.get("speech_type")
            if st not in VALID_SPEECH_TYPES:
                out.append(_err(
                    f"block_{bn}_shot_{sn}_invalid_speech_type",
                    f"blocks[{i}].shots[{j}].dialog.speech_type",
                    f"speech_type='{st}' — допустимы только "
                    f"{sorted(VALID_SPEECH_TYPES)}.",
                ))
    return out


def _check_speaker_in_characters(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """#8 — Поле `speaker` каждой реплики — один из characters блока.
    Иначе "block_N_shot_M_speaker_not_in_characters".

    Цитата из _VALIDATOR_JSON_TAIL пункт 8:
      "Поле `speaker` каждой реплики — один из characters блока. Иначе
       \"block_N_shot_M_speaker_not_in_characters\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        block_chars = set(b.get("characters") or [])
        for j, s in enumerate(b.get("shots") or []):
            sn = s.get("n", j + 1)
            d = s.get("dialog")
            if not d:
                continue
            speaker = d.get("speaker")
            if speaker and speaker not in block_chars:
                out.append(_err(
                    f"block_{bn}_shot_{sn}_speaker_not_in_characters",
                    f"blocks[{i}].shots[{j}].dialog.speaker",
                    f"speaker='{speaker}' нет в characters блока "
                    f"{sorted(block_chars)}.",
                ))
    return out


def _check_location_whitelist(card: Dict[str, Any],
                                locations: Set[str]) -> List[Dict[str, str]]:
    """#10 — Имя локации блока должно быть из списка доступных рефов
    локаций. Иначе "block_N_unknown_location".

    Цитата из _VALIDATOR_JSON_TAIL пункт 10:
      "Имя локации блока должно быть из списка доступных рефов локаций.
       Иначе \"block_N_unknown_location\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        loc = b.get("location")
        if loc and loc not in locations:
            out.append(_err(
                f"block_{bn}_unknown_location",
                f"blocks[{i}].location",
                f"location='{loc}' нет в whitelist рефов "
                f"({sorted(locations)}).",
            ))
    return out


def _check_characters_whitelist(card: Dict[str, Any],
                                  characters: Set[str]) -> List[Dict[str, str]]:
    """#11 — Все персонажи блока должны быть из списка доступных рефов
    персонажей. Иначе "block_N_unknown_character: <slug>".

    Цитата из _VALIDATOR_JSON_TAIL пункт 11:
      "Все персонажи блока должны быть из списка доступных рефов
       персонажей. Иначе \"block_N_unknown_character: <slug>\"."
    """
    out: List[Dict[str, str]] = []
    for i, b in enumerate(card.get("blocks") or []):
        bn = b.get("n", i + 1)
        for slug in (b.get("characters") or []):
            if slug not in characters:
                out.append(_err(
                    f"block_{bn}_unknown_character",
                    f"blocks[{i}].characters",
                    f"character slug='{slug}' нет в whitelist рефов "
                    f"({sorted(characters)}).",
                ))
    return out


def prefilter_check(card: Dict[str, Any],
                     refs: Dict[str, Any]) -> Tuple[List[Dict[str, str]], Set[str]]:
    """Прогоняет 10 механических правил Validator'а на карте.

    Args:
        card: монтажная карта (dict) от Scriptwriter'а — формат
              {blocks: [{n, location, characters, shots: [...]}], ...}.
        refs: refs_summary (dict) с ключами 'locations', 'objects',
              'characters' — каждое list[{slug, filename}].

    Returns:
        (errors, rules_done):
            errors      — list[{code, where, details}] в формате AI
                          Validator'а (Editor читает без отличий).
            rules_done  — set названий правил которые Python уже
                          проверил. Передаётся в сборщик system_prompt
                          AI Validator'а чтобы выкинуть проверенные
                          пункты из инструкции (PREFILTER_RULES).
    """
    locations = {l.get("slug") for l in (refs.get("locations") or []) if l.get("slug")}
    characters = {c.get("slug") for c in (refs.get("characters") or []) if c.get("slug")}

    errors: List[Dict[str, str]] = []
    errors += _check_block_count(card)
    errors += _check_block_shot_count(card)
    errors += _check_block_duration(card)
    errors += _check_shot_numbering(card)
    errors += _check_dialog_languages(card)
    errors += _check_speech_type_enum(card)
    errors += _check_speaker_in_characters(card)
    errors += _check_location_whitelist(card, locations)
    errors += _check_characters_whitelist(card, characters)
    return errors, set(PREFILTER_RULES)
