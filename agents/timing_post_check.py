"""
Python post-check таймингов реплик в монтажной карте после Editor.

v1.0.81: гарантированный invariant `duration_sec >= min_duration` —
не уговариваем AI промптами, правим в коде. Запускается из orchestrator
ПОСЛЕ каждого `_call_editor` (R1 и R2) до Validator R2/R3.

Корень: на ep2 v1.0.79 несмотря на «КРИТИЧЕСКИЙ ИНВАРИАНТ — ТАЙМИНГ
РЕПЛИК» в EDITOR_SYSTEM, Editor R1/R2 продолжали создавать
`dialog_too_short_for_words` ошибки. Промпт-фикс работал частично —
Opus иногда забывал правило или менял speech_type без пересчёта duration.

Формула min_duration: математика из правила #6 Validator AI (раздел 4
ГИ), реализованная в Python:
    min_duration = ceil(words_en / speed + reserve)

Speed по speech_type (слова/сек):
    fast=3.0, normal=2.75, emotional=2.25, slow=1.75

Reserve по числу слов:
    <=5 слов: 0.5
    6-15:    1.0
    >=16:    1.5

Cross-platform: чистый Python + math.ceil + dict-логика. Mac=Win.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple


# Скорости речи (слова в секунду) — те же что использует AI Validator
# в правиле #6 (раздел 4 ГИ).
SPEED_MAP: Dict[str, float] = {
    "fast":      3.0,
    "normal":    2.75,
    "emotional": 2.25,
    "slow":      1.75,
}


def _reserve_for_words(words: int) -> float:
    """Запас (сек) по числу слов EN."""
    if words <= 5:
        return 0.5
    if words <= 15:
        return 1.0
    return 1.5


def _words_en(dialog: Dict[str, Any]) -> int:
    """Считает слова в EN-оригинале по пробелам — стандарт AI Validator."""
    en = (dialog.get("en") or "").strip()
    if not en:
        return 0
    return len(en.split())


def min_duration_sec(words: int, speech_type: str) -> int:
    """Минимальная duration_sec шота с репликой.

    Args:
        words: число слов EN (по пробелам).
        speech_type: один из {fast, normal, emotional, slow}.
                     Если не в enum — fallback на normal (2.75 сл/сек).
    Returns:
        Целое число секунд (округление ВВЕРХ через math.ceil).
    """
    speed = SPEED_MAP.get(speech_type, SPEED_MAP["normal"])
    raw = words / speed + _reserve_for_words(words)
    return math.ceil(raw)


def apply_timing_post_check(
    card: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Поднимает `duration_sec` шотов с репликой до min_duration_sec
    (если меньше). Пересчитывает `card['total_seconds']`.

    Изменения вносятся IN-PLACE в переданный dict.

    Пропускает шот если:
        - `dialog` is None / отсутствует
        - `dialog.en` пустой
        - `speech_type` не в SPEED_MAP (валидатор поймает rule #7)

    Args:
        card: монтажная карта (формат от Scriptwriter / Editor).
    Returns:
        (card, summary):
            card — обновлённая карта (тот же dict).
            summary — dict с метаданными для лога:
                shots_checked:        int — сколько шотов проверено
                shots_fixed:          int — сколько шотов поправлено
                fixes:                list[dict] — детали каждой правки
                old_total_seconds:    int — поле total_seconds ДО
                new_total_seconds:    int — сумма после пересчёта
                delta_total_seconds:  int — new - old
    """
    fixes: List[Dict[str, Any]] = []
    checked = 0
    for b in card.get("blocks") or []:
        bn = b.get("n")
        for s in b.get("shots") or []:
            d = s.get("dialog")
            if not d:
                continue
            en = (d.get("en") or "").strip()
            if not en:
                continue
            sp = d.get("speech_type")
            if sp not in SPEED_MAP:
                continue
            checked += 1
            words = _words_en(d)
            min_d = min_duration_sec(words, sp)
            cur = int(s.get("duration_sec") or 0)
            if cur < min_d:
                fixes.append({
                    "block_n":      bn,
                    "shot_n":       s.get("n"),
                    "speaker":      d.get("speaker"),
                    "words_en":     words,
                    "speech_type":  sp,
                    "old_duration": cur,
                    "new_duration": min_d,
                })
                s["duration_sec"] = min_d

    # Пересчёт total_seconds (поле обновляется для совместимости — UI
    # после v1.0.78 уже считает сам, но другие читатели могут полагаться
    # на это поле).
    old_total = int(card.get("total_seconds") or 0)
    new_total = sum(
        sum(int(s.get("duration_sec") or 0)
            for s in (b.get("shots") or []))
        for b in (card.get("blocks") or [])
    )
    card["total_seconds"] = new_total

    return card, {
        "shots_checked":       checked,
        "shots_fixed":         len(fixes),
        "fixes":               fixes,
        "old_total_seconds":   old_total,
        "new_total_seconds":   new_total,
        "delta_total_seconds": new_total - old_total,
    }
