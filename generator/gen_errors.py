# -*- coding: utf-8 -*-
"""
generator/gen_errors.py — классификация ошибок генерации FastGen + локализованные
человеческие сообщения для юзера. Единый источник (раньше детектор транзиентных
ошибок дублировался в generator_thread.py и generator_video_thread.py).

ДВА назначения:
  • is_transient(text) — ретраить ли ошибку (временный сбой сервера/сети). Логика
    БЕЗ изменений перенесена из тредов (deny-wins: контент/лицензия/валидация/
    протухший реф — НЕ ретраить; временный сбой — ретраить; иначе False).
  • classify_error(code, text) / human_message(code, text) — человеческая ПРИЧИНА
    падения для показа юзеру вместо сырого английского текста сервера. Категории:
    moderation / license / invalid_request / generic. Локализация через i18n.tr
    (ru/uk/en); сообщение строится ЗДЕСЬ — оба треда зовут human_message и emit'ят.

ВАЖНО (приватность): для МОДЕРАЦИИ сырой текст сервера НЕ подставляется (может
содержать описание запрещённого контента) — только нейтральная фраза. Для прочих
технических — добавляется код в скобках для диагностики.

Реальные коды/тексты сервера (проверено экспериментом 2026-06-29):
  code=auth.forbidden  «Video/Flow Ultra generation not allowed for this license»
  code=validation.invalid_request  «Unknown V6 model: …»
  code=auth.license_expired  (истёкшая лицензия ключа)
  code=rate_limit.concurrency_exceeded  (занятость — TRANSIENT, не сюда)
Контент-модерация приходит на POLL (status=failed) текстом в поле error.

i18n — лист-модуль (без circular import), tr безопасен. Этот модуль НЕ импортит
generator_page/storyboard_app — чтобы оставаться лёгким листом.
"""

from __future__ import annotations

from i18n import tr


# ── РЕТРАЙ: транзиент vs нет (перенесено из тредов БЕЗ изменения логики) ──
# Порядок КРИТИЧЕН — deny-wins: сперва чёрный список (контент/лицензия/валидация/
# протухший реф — НЕ ретраить), потом белый (временный сбой — ретрай), иначе
# по умолчанию НЕ ретраить (безопасно).
_RETRY_DENY = ("unsafe", "sexual", "minor", "prominent people", "guardrails",
               "safety filters", "audio filtered", "not allowed for this license",
               "file_not_found_or_expired")
_RETRY_ALLOW = ("try again", "captcha", "no accounts available", "concurrency",
                "failed to perform", "failed to generate", "generation failed",
                "temporarily unavailable",
                "503", "502", "504", "connection reset", "curl:")


def is_transient(err_text) -> bool:
    """True → ошибку генерации можно авто-повторить (временный сбой сервера/сети).
    deny-list проверяется ПЕРВЫМ: контент/лицензия/валидация/протухший реф никогда
    не ретраятся. Затем allow-list. Не в списках → False (по умолчанию не ретраим)."""
    t = (err_text or "")
    t = t.lower() if isinstance(t, str) else str(t).lower()
    if any(d in t for d in _RETRY_DENY):
        return False
    if any(a in t for a in _RETRY_ALLOW):
        return True
    return False


# ── КЛАССИФИКАЦИЯ ПРИЧИНЫ для человеческого сообщения (deny-wins по порядку) ──
# Категории взаимоисключающие; первая сработавшая выигрывает.
_MODERATION = ("unsafe", "sexual", "minor", "prominent people", "guardrails",
               "safety filter", "audio filtered", "content policy", "nsfw",
               "moderation", "not allowed for safety", "violat")
_LICENSE = ("license_expired", "license expired", "license has expired",
            "not allowed for this license", "licence")
_INVALID = ("validation.invalid_request", "invalid_request", "invalid request",
            "validation error")


def classify_error(code, text) -> str:
    """code+text сервера → категория причины: 'moderation' | 'license' |
    'invalid_request' | 'generic'. Регистр-независимо, deny-wins (модерация
    важнее лицензии важнее валидации). Не сматчилось → 'generic'."""
    blob = (str(code or "") + " " + str(text or "")).lower()
    if any(m in blob for m in _MODERATION):
        return "moderation"
    if any(l in blob for l in _LICENSE):
        return "license"
    if any(i in blob for i in _INVALID):
        return "invalid_request"
    return "generic"


def human_message(code, text) -> str:
    """Локализованное человеческое сообщение об ошибке генерации (через i18n.tr).
      • moderation → нейтральная фраза БЕЗ сырого текста (приватность контента).
      • license / invalid_request → фраза + [code] в скобках (диагностика).
      • generic → фраза + сырой текст сервера (обрезан) для диагностики.
    Вызывается из обоих тредов на терминальной ошибке генерации."""
    cat = classify_error(code, text)
    code_s = str(code or "").strip()
    if cat == "moderation":
        return tr("gen_err_moderation")            # БЕЗ detail — приватность
    if cat == "license":
        msg = tr("gen_err_license_expired")
        return f"{msg} [{code_s}]" if code_s else msg
    if cat == "invalid_request":
        msg = tr("gen_err_invalid_request")
        detail = str(text or "").strip()
        if detail and detail != code_s:
            return f"{msg}: {detail[:120]} [{code_s}]" if code_s else f"{msg}: {detail[:120]}"
        return f"{msg} [{code_s}]" if code_s else msg
    # generic — сохраняем сырой текст сервера (полезен для диагностики)
    t = str(text or "").strip()
    base = tr("gen_err_generic")
    return f"{base}: {t[:120]}" if t else base
