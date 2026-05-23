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

2026-05-23 (Этап 1 переключателя режимов A/B/C/D): константы скоростей
и буферов вынесены в `SPEECH_CONFIG[mode]`. С Этапа 2.1 режим B имеет
независимые значения: fast=4.0, normal=3.5, emotional=3.5 (алиас на
normal), slow=2.3, buffer=0. Режимы A/C/D со старыми значениями.

Этап 3.3: для режима B значения speeds читаются ДИНАМИЧЕСКИ из
QSettings через `_speeds_for_mode("b")` (lazy import getter'ов
`storyboard_app.speech_speed_b_fast/normal/slow` из Этапа 3.1 + UI
слайдеры Этапа 3.2). Юзер крутит слайдер в Settings → значение
применяется к следующей сгенерированной монтажке без рестарта Studio.
SPEECH_CONFIG["b"]["speeds"] остаётся источником fallback'а — те же
значения (4.0/3.5/2.3) на случай если lazy import упал (batch без
Qt, partially initialized цикл импортов). A/C/D — без runtime
override, всегда из SPEECH_CONFIG.

Speed по speech_type (слова/сек) — дефолты:
    A/C/D:  fast=3.0, normal=2.75, emotional=2.25, slow=1.75
    B:      fast=4.0, normal=3.5,  emotional=3.5,  slow=2.3
            (B-значения юзер может менять в Settings → секция
             «Скорость речи актёров (режим B)»)

Reserve по числу слов:
    A/C/D:  <=5: 0.5  |  6-15: 1.0  |  >=16: 1.5
    B:      <=5: 0.0  |  6-15: 0.0  |  >=16: 0.0  (без буфера)

Cross-platform: чистый Python + math.ceil + dict-логика. Mac=Win.
Lazy import `agents.mode_loader` для авто-резолва режима — обёрнут
в try/except, чтобы модуль импортировался и без Qt (юнит-тесты,
batch-скрипты). Lazy import `storyboard_app.speech_speed_b_*` тоже
через try/except + sys.modules['__main__'] — тот же паттерн что в
agents/instruction_loader.py:_resolve_reader.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


# 2026-05-23: SPEECH_CONFIG — конфиг по режимам. На Этапе 1 значения
# идентичны для всех режимов. Структура:
#   SPEECH_CONFIG[mode]["speeds"][speech_type] -> float (слов/сек)
#   SPEECH_CONFIG[mode]["buffer"][bucket]      -> float (сек)
#     bucket: "<=5" | "<=15" | ">15"
_DEFAULT_SPEEDS: Dict[str, float] = {
    "fast":      3.0,
    "normal":    2.75,
    "emotional": 2.25,
    "slow":      1.75,
}
_DEFAULT_BUFFER: Dict[str, float] = {
    "<=5":  0.5,
    "<=15": 1.0,
    ">15":  1.5,
}

SPEECH_CONFIG: Dict[str, Dict[str, Dict[str, float]]] = {
    "a": {"speeds": dict(_DEFAULT_SPEEDS), "buffer": dict(_DEFAULT_BUFFER)},
    "b": {
        # 2026-05-23 (Этап 2.1): режим B — быстрая речь без буфера
        # (короткие шоты без воздуха). emotional → алиас на normal
        # для совместимости со старыми монтажками; новые карты в
        # режиме B emotional не используют.
        "speeds": {
            "fast":      4.0,
            "normal":    3.5,
            "emotional": 3.5,
            "slow":      2.3,
        },
        "buffer": {
            "<=5":  0.0,
            "<=15": 0.0,
            ">15":  0.0,
        },
    },
    "c": {"speeds": dict(_DEFAULT_SPEEDS), "buffer": dict(_DEFAULT_BUFFER)},
    "d": {"speeds": dict(_DEFAULT_SPEEDS), "buffer": dict(_DEFAULT_BUFFER)},
}

# Legacy: используется внешними импортами (agents/__init__.py реэкспортит
# SPEED_MAP? — нет, только функции; но оставим для прямой совместимости
# если кто-то импортирует константу). Содержит «дефолтные» значения =
# режим A. На Этапе вычистки можно будет удалить вместе с проверкой что
# никто не ссылается.
SPEED_MAP: Dict[str, float] = dict(_DEFAULT_SPEEDS)


def _resolve_mode(mode: Optional[str]) -> str:
    """Нормализует переданный mode или достаёт текущий из mode_loader.

    Сценарии:
      • mode явно передан как строка → lower, проверка enum, fallback 'a'.
      • mode is None → lazy import `agents.mode_loader.get_current_mode()`.
        get_current_mode() уже возвращает валидированную строку из
        {'a','b','c','d'} — берём её. При недоступности (нет Qt event
        loop / тесты / batch-скрипты без QApplication) — fallback 'a'.

    Lazy import нужен чтобы `timing_post_check.py` сохранял
    нулевые зависимости от PyQt6 на import time.
    """
    if mode is not None:
        m = str(mode).lower()
        return m if m in SPEECH_CONFIG else "a"
    try:
        from agents.mode_loader import get_current_mode  # lazy
        m = (get_current_mode() or "a").lower()
        return m if m in SPEECH_CONFIG else "a"
    except Exception:
        return "a"


def _speeds_for_mode(mode: str) -> Dict[str, float]:
    """Возвращает словарь speeds для режима mode.

    Для режимов 'a' / 'c' / 'd' — отдаёт SPEECH_CONFIG[mode]["speeds"]
    как есть (hardcoded дефолты, без runtime override).

    Для режима 'b' — подтягивает значения из QSettings через lazy
    import `storyboard_app.speech_speed_b_fast/normal/slow` (settings
    layer Этапа 3.1, UI слайдеры Этапа 3.2). `emotional` остаётся
    алиасом на `normal` (см. Этап 2.1 — совместимость старых монтажек,
    новые карты в режиме B emotional не используют).

    При любой ошибке lazy import'а (storyboard_app недоступен — batch
    без Qt; partially initialized модуль во время цикла импортов;
    QSettings.value() упал) — fallback на SPEECH_CONFIG["b"]["speeds"]
    (значения 4.0/3.5/2.3 идентичны default'ам getter'ов из Этапа 3.1,
    так что поведение совпадает).

    Lazy через sys.modules['__main__'] — паттерн скопирован из
    agents/instruction_loader.py:_resolve_reader. В PyInstaller-frozen
    .app `__main__` = `storyboard_app`, в dev-mode (python storyboard_app.py)
    тоже. Fallback на прямой import — для юнит-тестов.
    """
    if mode != "b":
        return SPEECH_CONFIG[mode]["speeds"]
    try:
        import sys as _sys
        main_mod = _sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, 'speech_speed_b_fast'):
            fast   = float(main_mod.speech_speed_b_fast())
            normal = float(main_mod.speech_speed_b_normal())
            slow   = float(main_mod.speech_speed_b_slow())
        else:
            from storyboard_app import (   # lazy
                speech_speed_b_fast,
                speech_speed_b_normal,
                speech_speed_b_slow,
            )
            fast   = float(speech_speed_b_fast())
            normal = float(speech_speed_b_normal())
            slow   = float(speech_speed_b_slow())
        return {
            "fast":      fast,
            "normal":    normal,
            "emotional": normal,
            "slow":      slow,
        }
    except Exception:
        return SPEECH_CONFIG["b"]["speeds"]


def _reserve_for_words(words: int, mode: Optional[str] = None) -> float:
    """Запас (сек) по числу слов EN.

    Args:
        words: число слов EN (по пробелам).
        mode: один из {'a','b','c','d'}. По умолчанию резолвится через
              `_resolve_mode(None)` — либо текущий режим из QSettings
              (через mode_loader), либо fallback на 'a'.
    """
    buf = SPEECH_CONFIG[_resolve_mode(mode)]["buffer"]
    if words <= 5:
        return buf["<=5"]
    if words <= 15:
        return buf["<=15"]
    return buf[">15"]


def _words_en(dialog: Dict[str, Any]) -> int:
    """Считает слова в EN-оригинале по пробелам — стандарт AI Validator."""
    en = (dialog.get("en") or "").strip()
    if not en:
        return 0
    return len(en.split())


def min_duration_sec(
    words: int,
    speech_type: str,
    mode: Optional[str] = None,
) -> int:
    """Минимальная duration_sec шота с репликой.

    Args:
        words: число слов EN (по пробелам).
        speech_type: один из {fast, normal, emotional, slow}.
                     Если не в enum для выбранного режима — fallback
                     на normal-скорость этого режима.
        mode: один из {'a','b','c','d'} или None (= текущий режим из
              mode_loader, fallback на 'a'). Старая сигнатура без mode
              продолжает работать.
    Returns:
        Целое число секунд (округление ВВЕРХ через math.ceil).
    """
    m = _resolve_mode(mode)
    speeds = _speeds_for_mode(m)
    speed = speeds.get(speech_type, speeds["normal"])
    raw = words / speed + _reserve_for_words(words, m)
    return math.ceil(raw)


def apply_timing_post_check(
    card: Dict[str, Any],
    mode: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Поднимает `duration_sec` шотов с репликой до min_duration_sec
    (если меньше). Пересчитывает `card['total_seconds']`.

    Изменения вносятся IN-PLACE в переданный dict.

    Пропускает шот если:
        - `dialog` is None / отсутствует
        - `dialog.en` пустой
        - `speech_type` не в SPEECH_CONFIG[mode]["speeds"] (валидатор
          поймает rule #7)

    Args:
        card: монтажная карта (формат от Scriptwriter / Editor).
        mode: один из {'a','b','c','d'} или None (= текущий из
              mode_loader, fallback 'a'). Старая сигнатура без mode
              продолжает работать.
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
                mode:                 str — фактически применённый режим
    """
    m = _resolve_mode(mode)
    speeds = _speeds_for_mode(m)

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
            if sp not in speeds:
                continue
            checked += 1
            words = _words_en(d)
            min_d = min_duration_sec(words, sp, m)
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
        "mode":                m,
    }
