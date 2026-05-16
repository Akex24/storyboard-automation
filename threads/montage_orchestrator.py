# -*- coding: utf-8 -*-
"""
threads/montage_orchestrator.py — multi-agent оркестратор монтажной карты.

Цепочка:
  1. SCRIPTWRITER  → пишет монтажную карту в JSON
  2. VALIDATOR     → проверяет; возвращает {ok, errors, report}
  3. EDITOR        → если errors не пуст → правит → новая карта
  4. Повтор шагов 2-3 до 3 раундов или до ok=true.

Результат — финальная карта или последняя версия с ошибками (юзер сам
решит фиксить вручную).

Все агенты вызываются через `claude -p <prompt>` subprocess. Между ними
передаётся JSON.

История: создано 2026-05-06 (фича Multi-agent monton card → storyboards).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from agents.montage_prompts import (
    SCRIPTWRITER_SYSTEM,
    VALIDATOR_SYSTEM,
    EDITOR_SYSTEM,
    CONTEXT_REVIEWER_SYSTEM,
    build_scriptwriter_user_prompt,
    build_validator_user_prompt,
    build_editor_user_prompt,
    build_context_reviewer_user_prompt,
    get_validator_system,
    get_geometry_editor_system,
    build_geometry_editor_user_prompt,
)
from agents.validator_prefilter import prefilter_check
from agents.timing_post_check import apply_timing_post_check


class MontageOrchestratorThread(QThread):
    """Поток: запускает Сценарист → Чекер → (если errors > 0) Редактор →
    опционально Финальный редактор. Линейный пайплайн без раундов
    (v1.0.62). Эмитит сигналы прогресса для UI и финальный сигнал с
    итоговой картой + отчётом Чекера.
    """

    # стадии: "scriptwriter_running", "validator_running",
    # "validator_done", "editor_running",
    # "context_reviewer_running", "context_reviewer_done"
    progress = pyqtSignal(str, dict)
    # финальный сигнал: monton_card (dict), checker_report (dict),
    # rounds_used (int), agent_log_path (str), agent_summary (dict)
    # agent_summary — компактный отчёт для UI попапа: что делал каждый агент.
    finished_ok = pyqtSignal(dict, dict, int, str, dict)
    # ошибка, не удалось получить даже первой версии
    failed = pyqtSignal(str)

    # 2026-05-13 (v1.0.62): MAX_ROUNDS убран. Цикл стал линейным:
    # Scriptwriter → Validator → (если errors > 0) Editor → ГОТОВО.
    # Без повторной проверки. Если включён Context Reviewer toggle —
    # после Editor (или после Validator при errors=0) запускается
    # Reviewer, и если он нашёл concerns — ещё один Editor.
    # Анализ ep2 v1.0.61: Validator R2 длился ~7 мин и фиксировал
    # ошибки которые УЖЕ не правились (MAX_ROUNDS исчерпан) — пустая
    # трата времени. ep4 v1.0.61 принят с одного раунда — R2 там не
    # запускался. Делаем это поведение по умолчанию.
    SUBPROCESS_TIMEOUT_SEC = 600  # 10 минут на каждый вызов CLI
    # v1.0.86 (этап 1 стриминга): таймаут МЕЖДУ JSONL-чанками в
    # stream-json режиме. На слабом интернете 600с общего timeout'а
    # не помогает — TCP-соединение может «дышать» keep-alive байтами
    # и subprocess.run сбрасывает timer. Каждый JSONL-чанк от CLI =
    # активность; если 60с тишины — считаем что соединение мертво,
    # terminate'им CLI и поднимаем ошибку. Юзер видит понятный fail
    # быстрее, не висит 10 минут.
    STREAM_CHUNK_TIMEOUT_SEC = 60
    # v1.0.86 (этап 4): отдельный таймаут для Opus 4.7 этапов
    # (Scriptwriter, Editor). Diagnostic probe-скрипт показал что Opus
    # с extended thinking в стрим-режиме МОЛЧИТ ~80 секунд между
    # `content_block_start` (начало thinking-блока) и `signature_delta`
    # (его конец) — никаких `thinking_delta` чанков для Opus 4.7 CLI
    # не присылает (в отличие от Haiku где плотный поток thinking_delta).
    # 60с chunk-timeout убивал живой запрос посреди thinking. 150с =
    # измеренные 81.7с + запас на медленный TTFT и большой scenario.
    # Применяется только для Opus callsite через параметр
    # `chunk_timeout_sec=`. Sonnet/Haiku остаются на default 60с.
    STREAM_CHUNK_TIMEOUT_OPUS_SEC = 150

    # 2026-05-09: per-agent model routing. Юзер не выбирает модели для
    # пайплайнов — каждый агент прибит к задаче.
    # 2026-05-12 (v1.0.54): Editor переведён на Sonnet 4.6 (применение
    # N исправлений к JSON-карте — дисциплинированная задача).
    # 2026-05-12 (v1.0.58): ВСЕ 4 агента переведены на Sonnet 4.6 —
    # эксперимент по максимальному ускорению.
    # 2026-05-13 (v1.0.59): убран episodes_summary из контекста агентов
    # (~8KB / 2000 tok на каждый вызов). Качество карт высшее, но
    # Scriptwriter на Sonnet даёт 7+ мин (был 22 мин полный цикл).
    # 2026-05-13 (v1.0.60): Scriptwriter ВОЗВРАЩЁН на Opus 4.7.
    # Scriptwriter — творческий агент, генерирует карту с нуля. Sonnet
    # на этой задаче медленнее Opus (7+ мин vs 1-2 мин у Opus) + даёт
    # менее чистый первый вариант, из-за чего цикл Validator↔Editor
    # крутится 3 раунда. Возврат на Opus экономит 10-15 мин на эпизод.
    # Validator/Editor/Reviewer — структурные/проверочные, остаются
    # на Sonnet 4.6.
    # v1.0.72 (2026-05-14): Validator переведён на Haiku 4.5. Sonnet
    # 4.6 на семантической проверке (5 правил после Python pre-filter
    # v1.0.69) упирался в reasoning-токены и тратил 8+ минут на 4 KB
    # output (9 ch/sec). Прямой ручной тест того же prompt'а на Haiku
    # 4.5 — 2:04 / 6.4 KB output / 51 ch/sec, реально нашёл timing-
    # math ошибку (dialog_too_short_for_words). На механике после
    # prefilter'а семантический объём задачи Haiku достаточен.
    # Editor оставлен на Sonnet 4.6 — он делает creative-правку
    # реплик с учётом характера + иерархии сжатия, тут Sonnet нужен.
    # v1.0.76 (2026-05-14): Editor переведён на Opus 4.7. На ep2
    # v1.0.75 Sonnet 4.6 «отрапортовал» что исправил 5 ошибок, но в
    # карте все 5 фактически остались (подтверждено Validator R2):
    # одна модель пропускает ошибки при многозадачности (timing math +
    # forbidden_phrase одновременно). Opus умнее → реже промахивается.
    # Цена: ~3-4 мин на Editor вместо 2:49. Бонус: Validator R2 теперь
    # запускается после Editor → Studio видит реальное оставшееся
    # количество ошибок (вместо лживой «поправил 5 ошибок»).
    # Откат при регрессии качества — git revert этого коммита.
    MODEL_SCRIPTWRITER     = "claude-opus-4-7"
    MODEL_VALIDATOR        = "claude-haiku-4-5"
    MODEL_EDITOR           = "claude-opus-4-7"
    MODEL_CONTEXT_REVIEWER = "claude-sonnet-4-6"
    # v1.0.75: Geometry Editor — узкий сабагент для shot.geometry.
    # Простая структурная правка → Haiku 4.5 хватает; разгружает
    # главный Editor от missing_geometry-ошибок (на ep2 v1.0.74 их
    # было 3 из 8 — половина reasoning-нагрузки на Sonnet).
    MODEL_GEOMETRY_EDITOR  = "claude-haiku-4-5"

    def __init__(self, claude_cli_path: str,
                 scenario_text: str,
                 refs_summary: dict,
                 show_context: Optional[dict] = None,
                 log_path: Optional[Path] = None,
                 use_context_reviewer: bool = False,
                 parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
        # 2026-05-13 (v1.0.61): Context Reviewer теперь опциональный.
        # Caller (EpisodeChatView._on_montage_start) читает QSettings
        # ключ "montage/context_reviewer_enabled" и передаёт сюда.
        # Default False — Reviewer пропускается, экономим ~2 мин на эпизод.
        # Анализ логов показал что concerns на 4 тестовых эпизодах = 0
        # (Reviewer запускался 2 раза, оба раза clean). Юзер может
        # включить toggle в Settings для сложных эпизодов.
        self._use_context_reviewer = bool(use_context_reviewer)
        self._scenario = scenario_text
        self._refs = refs_summary
        # 2026-05-06: контекст всего сериала (Bible + краткие описания
        # других эпизодов) — для соответствия характерам и сюжетной
        # целостности. См. _format_show_context в montage_prompts.py.
        self._show_context: dict = show_context or {}
        self._log_path = log_path
        self._stop = False
        self._agent_log: List[dict] = []  # для финального дампа
        # v1.0.86: handle активного claude-Popen — для terminate() при
        # внешнем stop() или chunk-timeout. Используется только в
        # _run_claude_stream (новый метод). Старый _run_claude через
        # subprocess.run этим не пользуется.
        self._proc: Optional[subprocess.Popen] = None

    def stop(self):
        self._stop = True
        # v1.0.86: жёсткий kill активного CLI subprocess'а если стрим
        # ещё идёт. Раньше флаг _stop проверялся только между этапами
        # (между _call_*), а внутри subprocess.run был блок на 600с.
        # Теперь — мгновенная остановка стрима.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D401
        # 2026-05-13 (v1.0.62): Линейный пайплайн без раундов.
        #   1. Scriptwriter
        #   2. Validator (один раз)
        #   3. Если Validator.errors > 0 → Editor
        #   4. Если toggle Context Reviewer ON:
        #        Reviewer → если concerns > 0 → Editor
        #   5. Финал
        # Никаких повторных проверок. rounds_used = 1 всегда (для
        # совместимости с сигналом finished_ok и caller'ом).
        try:
            # 1) Scriptwriter
            self.progress.emit("scriptwriter_running", {})
            try:
                montage_card = self._call_scriptwriter()
            except Exception as e:
                self._dump_log()
                self.failed.emit(f"scriptwriter_failed: {e}")
                return

            if self._stop:
                self.failed.emit("cancelled")
                return

            checker_report: Dict = {"ok": False, "errors": [], "report": []}

            # 2) Validator — один раз
            self.progress.emit("validator_running", {})
            try:
                checker_report = self._call_validator(montage_card)
            except Exception as e:
                self._agent_log.append({
                    "stage": "validator",
                    "error": str(e),
                })
                # Не fatal — идём дальше без правок.
                self._finalize(montage_card, checker_report)
                return

            if self._stop:
                self.failed.emit("cancelled")
                return

            errors_count = len(checker_report.get("errors", []))
            self.progress.emit("validator_done",
                                {"ok": checker_report.get("ok", False),
                                 "errors_count": errors_count})

            # v1.0.75: 2.5) Geometry Editor — Haiku-сабагент для
            # `missing_geometry` ошибок. Отделяем их от остальных,
            # обрабатываем структурной правкой (добавление shot.geometry).
            # Основной Editor (Sonnet) дальше получает ТОЛЬКО оставшиеся
            # ошибки — меньше reasoning-нагрузка.
            # Если Geometry Editor упал (TimeoutExpired / exception) —
            # missing_geometry-ошибки передаются обратно Editor'у как
            # fallback (Q1=B по плану v1.0.75).
            all_errors = list(checker_report.get("errors", []) or [])
            geometry_errors = [
                e for e in all_errors
                if (e.get("code") or "").endswith("_missing_geometry")
            ]
            other_errors = [
                e for e in all_errors
                if not (e.get("code") or "").endswith("_missing_geometry")
            ]
            editor_input_errors = list(all_errors)  # fallback default

            if geometry_errors:
                self.progress.emit("geometry_editor_running",
                                    {"errors_count": len(geometry_errors)})
                try:
                    montage_card = self._call_geometry_editor(
                        montage_card, geometry_errors)
                    # Успех — Editor получит только other_errors
                    editor_input_errors = list(other_errors)
                except Exception as e:
                    self._agent_log.append({
                        "stage": "geometry_editor",
                        "error": str(e),
                    })
                    # Fallback (Q1=B): Editor получит ВСЕ ошибки, попробует
                    # сам исправить и missing_geometry. Карта остаётся
                    # такой, какой её отдал Scriptwriter (Geometry Editor
                    # не успел применить правки). UI покажет «⚠ Geometry
                    # Editor УПАЛ» через _build_agent_summary.

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            # 3) Editor — только если есть оставшиеся ошибки
            editor_ran = False
            if not checker_report.get("ok") and len(editor_input_errors) > 0:
                self.progress.emit("editor_running",
                                    {"errors_count": len(editor_input_errors)})
                try:
                    montage_card = self._call_editor(
                        montage_card, editor_input_errors)
                    editor_ran = True
                    # v1.0.81: Python post-check таймингов — гарантирует
                    # duration_sec >= min_duration для всех шотов с
                    # репликой. Закрывает класс багов где Editor расширил
                    # реплику без пересчёта duration.
                    montage_card = self._apply_post_check_timings(
                        montage_card, round_num=1)
                except Exception as e:
                    self._agent_log.append({
                        "stage": "editor",
                        "error": str(e),
                    })
                    # Не fatal — отдаём карту до Editor'а.
                    self._finalize(montage_card, checker_report)
                    return

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            # v1.0.76: 3.5) Validator R2 — ТОЛЬКО если Editor реально
            # отработал. Цель — оценить сколько ошибок Editor реально
            # устранил (set-сравнение R1 vs R2 в UI), и не показывает
            # ли он новые ошибки которых не было в R1.
            # При exception в R2 — checker_report остаётся от R1, UI
            # покажет «⚠ Не удалось проверить результат Editor» через
            # honest-UI ветку summary['validator_r2'].failed.
            validator_r2_ok = False  # True если R2 успешно отдал валидный report
            if editor_ran:
                self.progress.emit("validator_r2_running", {})
                try:
                    r2_report = self._call_validator(montage_card, round_num=2)
                    checker_report = r2_report
                    validator_r2_ok = True
                except Exception as e:
                    self._agent_log.append({
                        "stage": "validator_r2",
                        "error": str(e),
                    })
                    # Не fatal — checker_report остаётся от R1, UI пометит
                    # validator_r2 как failed=True.

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            # v1.0.77: 3.6) Editor R2 — ТОЛЬКО если:
            #   - Validator R2 отработал успешно (validator_r2_ok)
            #   - И остались ошибки (checker_report.errors > 0)
            # Тот же EDITOR_SYSTEM и MODEL_EDITOR (Opus 4.7). Без
            # geometry-split — Opus справится со всеми остаточными.
            # При exception в Editor R2 — honest UI «⚠ Редактор R2 УПАЛ»,
            # pipeline идёт дальше (на Context Reviewer если включён),
            # checker_report остаётся от Validator R2.
            editor_r2_ran = False
            r2_errors_remaining = list(checker_report.get("errors", []) or [])
            if (validator_r2_ok
                    and not checker_report.get("ok")
                    and len(r2_errors_remaining) > 0):
                self.progress.emit("editor_r2_running",
                                    {"errors_count": len(r2_errors_remaining)})
                try:
                    montage_card = self._call_editor(
                        montage_card, r2_errors_remaining, round_num=2)
                    editor_r2_ran = True
                    # v1.0.81: Python post-check таймингов после Editor R2
                    montage_card = self._apply_post_check_timings(
                        montage_card, round_num=2)
                except Exception as e:
                    self._agent_log.append({
                        "stage": "editor_r2",
                        "error": str(e),
                    })
                    # Не fatal — checker_report остаётся от R2.

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            # v1.0.77: 3.7) Validator R3 — ТОЛЬКО если Editor R2 реально
            # отработал. Цель — финальная честная цифра остатка.
            # Editor R3 НЕ запускаем (по плану — стоп после R3, юзер
            # сам решает что делать с остатком).
            # При exception в R3 — checker_report остаётся от R2, UI
            # покажет «⚠ Не удалось проверить результат Editor R2».
            if editor_r2_ran:
                self.progress.emit("validator_r3_running", {})
                try:
                    r3_report = self._call_validator(montage_card, round_num=3)
                    checker_report = r3_report
                except Exception as e:
                    self._agent_log.append({
                        "stage": "validator_r3",
                        "error": str(e),
                    })
                    # Не fatal — checker_report остаётся от R2.

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            # 4) Context Reviewer — опционально (toggle в Settings)
            if self._use_context_reviewer:
                self.progress.emit("context_reviewer_running", {})
                try:
                    reviewer_report = self._call_context_reviewer(montage_card)
                except Exception as e:
                    self._agent_log.append({
                        "stage": "context_reviewer",
                        "error": str(e),
                    })
                    self._finalize(montage_card, checker_report)
                    return

                if self._stop:
                    self.failed.emit("cancelled")
                    return

                concerns = reviewer_report.get("concerns") or []
                self.progress.emit("context_reviewer_done",
                                    {"ok": reviewer_report.get("ok", True),
                                     "concerns_count": len(concerns)})

                if not reviewer_report.get("ok", True) and concerns:
                    converted_errors = [
                        {
                            "code": c.get("code", "context_concern"),
                            "where": c.get("where", ""),
                            "details": c.get("details", ""),
                        }
                        for c in concerns
                    ]
                    self.progress.emit("editor_running",
                                        {"errors_count": len(converted_errors)})
                    try:
                        montage_card = self._call_editor(
                            montage_card, converted_errors)
                    except Exception as e:
                        self._agent_log.append({
                            "stage": "editor_after_reviewer",
                            "error": str(e),
                        })
                        self._finalize(montage_card, checker_report)
                        return

            # 5) Финал
            self._finalize(montage_card, checker_report)
        except Exception as e:
            self._dump_log()
            self.failed.emit(f"unexpected: {e}")

    def _finalize(self, montage_card: dict, checker_report: dict) -> None:
        """Общий путь финализации — dump log + emit finished_ok.
        rounds_used = 1 всегда (раундов больше нет, поле оставлено для
        совместимости с сигналом и caller'ом episode_chat.py).
        """
        log_path_str = self._dump_log()
        agent_summary = self._build_agent_summary(1)
        self.finished_ok.emit(montage_card, checker_report, 1,
                               log_path_str or "", agent_summary)

    # ──────────────────────────────────────────────────────────────────
    # Конкретные вызовы агентов через CLI.
    # ──────────────────────────────────────────────────────────────────

    def _call_scriptwriter(self) -> dict:
        user = build_scriptwriter_user_prompt(
            self._scenario, self._refs, show_context=self._show_context)
        # 2026-05-13 (v1.0.63): замер времени обнимает ТОЛЬКО _run_claude
        # (subprocess) — build_user_prompt и _parse_json копеечные.
        # v1.0.86 (этап 3/4): Scriptwriter переключён на _run_claude_stream
        # (stream-json). FATAL-этап: при exception `run()` ловит,
        # `_dump_log + failed.emit` стопорит pipeline целиком.
        # v1.0.86 (этап 4/4): chunk-timeout 150с (вместо default 60с).
        # Opus 4.7 extended thinking молчит ~80с между chunks
        # (probe-скрипт на ep21 показал 81.7с тишины). 60с убивал
        # живой запрос посреди thinking.
        # v1.0.86 (этап 5): effort="low" — короткий thinking. Probe
        # показал что на default effort silence 120-160с (≥150с
        # лимита), а с low total всего 49с, max_silence 2.6с. Качество
        # карты на low проверяется юзером — если ухудшилось, переводим
        # на "medium".
        t0 = time.time()
        raw = self._run_claude_stream(
            SCRIPTWRITER_SYSTEM, user,
            model=self.MODEL_SCRIPTWRITER,
            chunk_timeout_sec=self.STREAM_CHUNK_TIMEOUT_OPUS_SEC,
            effort="low")
        duration_sec = round(time.time() - t0, 2)
        montage = self._parse_json(raw)
        self._agent_log.append({
            "stage": "scriptwriter",
            "round": 1,
            "model_used": self.MODEL_SCRIPTWRITER,
            "started_at": t0,
            "duration_sec": duration_sec,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "result": montage,
        })
        return montage

    def _call_validator(self, montage_card: dict,
                         round_num: int = 1) -> dict:
        # v1.0.69: Python pre-filter перед AI. 10 механических правил
        # (#1-#5, #7, #8, #10, #11, #13) проверяются в Python без LLM —
        # см. agents/validator_prefilter.py. AI получает урезанный
        # system_prompt только с правилами #6, #7а, #9, #12, #14
        # (семантика, требует reasoning).
        # v1.0.76: round_num=1 (после Scriptwriter) пишется в _agent_log
        # как stage='validator'; round_num=2 (после Editor) — как
        # stage='validator_r2'. UI рендерит обе стадии раздельно для
        # отчёта «Editor исправил X из Y / создал N новых».
        py_errors, rules_done = prefilter_check(montage_card, self._refs)
        validator_system = get_validator_system(skip_rules=rules_done)

        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_validator_user_prompt(
            card_json, self._refs, show_context=self._show_context)
        t0 = time.time()
        # v1.0.86 (этап 2/4): Validator переключён на стриминг через
        # _run_claude_stream (--output-format stream-json + JSONL чанки +
        # chunk-timeout 60с). Сигнатура и финальная строка идентичны
        # старому _run_claude (валидация на этапе 1 показала SHA-256
        # match). Остальные 4 callsite (scriptwriter/editor/geometry/
        # context_reviewer) пока на старом методе — этап 3.
        # Покрывает все три раунда (R1, R2, R3) — это одна функция
        # вызывается с разным round_num.
        raw = self._run_claude_stream(validator_system, user,
                                       model=self.MODEL_VALIDATOR)
        duration_sec = round(time.time() - t0, 2)
        ai_report = self._parse_json(raw)

        # Объединяем ошибки: Python + AI. ok=False если есть хоть что-то.
        ai_errors = list(ai_report.get("errors") or [])
        merged_errors = list(py_errors) + ai_errors
        report = {
            "ok": (len(merged_errors) == 0),
            "errors": merged_errors,
            "report": ai_report.get("report") or [],
        }
        stage_name = "validator" if round_num == 1 else f"validator_r{round_num}"
        self._agent_log.append({
            "stage": stage_name,
            "model_used": self.MODEL_VALIDATOR,
            "started_at": t0,
            "duration_sec": duration_sec,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "prefilter_errors": len(py_errors),
            "prefilter_rules_done": sorted(rules_done),
            "validator_system_chars": len(validator_system),
            "result": report,
        })
        return report

    def _call_editor(self, montage_card: dict, errors: list,
                      round_num: int = 1) -> dict:
        # v1.0.77: round_num=1 (после Validator R1, обычно с geometry-split) —
        # stage='editor'; round_num=2 (после Validator R2 если остались
        # ошибки) — stage='editor_r2'. Тот же EDITOR_SYSTEM и MODEL_EDITOR.
        # After-reviewer случай (Context Reviewer concerns > 0 → ещё Editor)
        # пока остаётся под stage='editor' — Bug 6 в очереди, не сейчас.
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_editor_user_prompt(
            card_json, errors, self._refs,
            original_scenario=self._scenario,
            show_context=self._show_context)
        t0 = time.time()
        # v1.0.86 (этап 3/4): Editor переключён на стриминг. Покрывает
        # все три callsite (R1 после Validator R1, R2 после Validator R2,
        # editor_after_reviewer) — это одна функция вызывается с разным
        # round_num. Не fatal: при exception pipeline ловит в run(),
        # помечает stage failed и продолжает с картой до Editor'а.
        # v1.0.86 (этап 4/4): chunk-timeout 150с — Editor тоже Opus 4.7
        # с extended thinking, та же тишина 80+ сек что у Scriptwriter.
        # v1.0.86 (этап 5): effort="low" — та же мотивация что у
        # Scriptwriter. Editor применяет N исправлений к JSON-карте
        # — менее открытая задача, low effort должен справиться.
        raw = self._run_claude_stream(
            EDITOR_SYSTEM, user,
            model=self.MODEL_EDITOR,
            chunk_timeout_sec=self.STREAM_CHUNK_TIMEOUT_OPUS_SEC,
            effort="low")
        duration_sec = round(time.time() - t0, 2)
        new_card = self._parse_json(raw)
        stage_name = "editor" if round_num == 1 else f"editor_r{round_num}"
        self._agent_log.append({
            "stage": stage_name,
            "model_used": self.MODEL_EDITOR,
            "started_at": t0,
            "duration_sec": duration_sec,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "errors_in": len(errors),
            "result": new_card,
        })
        return new_card

    def _call_geometry_editor(self, montage_card: dict,
                                geometry_errors: list) -> dict:
        """v1.0.75: Geometry Editor — Haiku-сабагент. Узкая задача:
        добавить поле `geometry` к шотам, на которые Validator выдал
        ошибку `block_N_shot_M_missing_geometry`.

        Args:
            montage_card:    текущая карта (после Scriptwriter, или
                             уже после предыдущих стадий).
            geometry_errors: подмножество errors[] Validator'а с кодами
                             вида '*_missing_geometry'.
        Returns:
            Новая карта (тот же dict-формат) с добавленными geometry.
        """
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_geometry_editor_user_prompt(card_json, geometry_errors)
        system = get_geometry_editor_system()
        t0 = time.time()
        # v1.0.86 (этап 3/4): Geometry Editor (Haiku) переключён на
        # стриминг. Запускается только при `*_missing_geometry` ошибках.
        # Не fatal: при exception падает в fallback (Editor получит ВСЕ
        # ошибки включая missing_geometry).
        raw = self._run_claude_stream(system, user,
                                       model=self.MODEL_GEOMETRY_EDITOR)
        duration_sec = round(time.time() - t0, 2)
        new_card = self._parse_json(raw)
        self._agent_log.append({
            "stage": "geometry_editor",
            "model_used": self.MODEL_GEOMETRY_EDITOR,
            "started_at": t0,
            "duration_sec": duration_sec,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "errors_in": len(geometry_errors),
            "geometry_editor_system_chars": len(system),
            "result": new_card,
        })
        return new_card

    def _apply_post_check_timings(self, montage_card: dict,
                                    round_num: int) -> dict:
        """v1.0.81: гарантированный Python post-check таймингов после
        Editor R1/R2. Поднимает duration_sec шотов с репликой до
        min_duration_sec (ceil(words_en / speed + reserve)).

        Не уговариваем Opus промптами — правим в коде. Закрывает класс
        ошибок где Editor расширил реплику или сменил speech_type без
        пересчёта duration.

        Args:
            montage_card: текущая карта (после _call_editor).
            round_num:    1 (после Editor R1) или 2 (после Editor R2).
                          Используется только для stage_name в логе.
        Returns:
            Обновлённая карта (in-place в apply_timing_post_check).
        """
        t0 = time.time()
        montage_card, summary = apply_timing_post_check(montage_card)
        duration_sec = round(time.time() - t0, 4)
        stage_name = f"post_check_timings_r{round_num}"
        self._agent_log.append({
            "stage": stage_name,
            "started_at": t0,
            "duration_sec": duration_sec,
            "shots_checked": summary["shots_checked"],
            "shots_fixed": summary["shots_fixed"],
            "fixes": summary["fixes"],
            "old_total_seconds": summary["old_total_seconds"],
            "new_total_seconds": summary["new_total_seconds"],
            "delta_total_seconds": summary["delta_total_seconds"],
        })
        return montage_card

    def _call_context_reviewer(self, montage_card: dict) -> dict:
        """Финальный супер-редактор. Проверяет соответствие карты
        Bible'и сериала и другим эпизодам. Возвращает dict с полями
        `ok` и `concerns`.
        """
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_context_reviewer_user_prompt(
            card_json, self._scenario, show_context=self._show_context)
        t0 = time.time()
        # v1.0.86 (этап 3/4): Context Reviewer переключён на стриминг.
        # Опциональный (toggle в Settings), default OFF — для большинства
        # пользователей этот код не запускается. Не fatal: при exception
        # pipeline catch'ит и идёт к _finalize.
        raw = self._run_claude_stream(CONTEXT_REVIEWER_SYSTEM, user,
                                       model=self.MODEL_CONTEXT_REVIEWER)
        duration_sec = round(time.time() - t0, 2)
        report = self._parse_json(raw)
        # Нормализуем — на случай если AI вернул concerns под другим
        # ключом или забыл ok.
        if not isinstance(report, dict):
            report = {"ok": True, "concerns": []}
        report.setdefault("ok", True)
        report.setdefault("concerns", [])
        self._agent_log.append({
            "stage": "context_reviewer",
            "model_used": self.MODEL_CONTEXT_REVIEWER,
            "started_at": t0,
            "duration_sec": duration_sec,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "result": report,
        })
        return report

    # ──────────────────────────────────────────────────────────────────

    def _run_claude(self, system_prompt: str, user_prompt: str,
                    model: str) -> str:
        """Один вызов `claude -p` через subprocess. Возвращает stdout.

        2026-05-09: `model` — обязательный параметр. Каждый агент
        (Scriptwriter / Validator / Editor / ContextReviewer) передаёт
        свою модель через MODEL_* class constants (см. шапку класса).
        """
        if not self._cli:
            raise RuntimeError("claude CLI not found")
        cmd = [self._cli, "-p",
               "--system-prompt", system_prompt,
               "--output-format", "text",
               "--model", model]
        # На Windows запускаем без отдельной консоли (мы хотим тихий
        # subprocess для backend-AI).
        kwargs = {
            'input': user_prompt,
            'capture_output': True,
            'text': True,
            'timeout': self.SUBPROCESS_TIMEOUT_SEC,
            'encoding': 'utf-8',
        }
        if sys.platform == 'win32':
            # Скрываем консольное окно (backend-вызов, юзеру окно cmd
            # не нужно).
            CREATE_NO_WINDOW = 0x08000000
            kwargs['creationflags'] = CREATE_NO_WINDOW
        r = subprocess.run(cmd, **kwargs)
        if r.returncode != 0:
            stderr = (r.stderr or "")[:500]
            raise RuntimeError(f"claude exit={r.returncode}: {stderr}")
        return (r.stdout or "").strip()

    def _run_claude_stream(self, system_prompt: str, user_prompt: str,
                            model: str,
                            chunk_timeout_sec: Optional[int] = None,
                            effort: Optional[str] = None) -> str:
        """v1.0.86: стриминг-вариант _run_claude через --output-format stream-json.

        Сигнатура та же что у `_run_claude` (для замены callsite'ов на
        этапе 2). Возвращает финальную строку — `.result` из JSONL-чанка
        `{"type":"result"}`. Валидация показала что эта строка побайтно
        идентична stdout старого `--output-format text` режима.

        Зачем нужен: на слабом интернете старый `subprocess.run(timeout=
        600)` зависает на 10 минут без признаков жизни, потом таймаутит.
        Стриминг даёт JSONL-чанки от CLI каждые ~0.5-3 секунды (system
        init, status, rate_limit_event, message_start, content_block_delta,
        result). Каждый чанк сбрасывает chunk-timer. Если 60с тишины —
        terminate'им CLI и валим с понятной ошибкой.

        Cross-platform: используем threading.Thread для чтения stdout
        (select на pipes под Windows не работает). На win32 —
        creationflags=CREATE_NO_WINDOW.

        Этап 1 фичи: метод существует, но никто его пока не зовёт.
        Старый `_run_claude` остаётся как fallback.

        v1.0.86 (этап 4): добавлен `chunk_timeout_sec` параметр для
        per-stage таймаута. Opus с extended thinking молчит до 80с
        между chunks (диагностика: probe-скрипт на ep21 показал 81.7с
        тишины между `content_block_start` и `signature_delta`). Для
        Scriptwriter/Editor (Opus 4.7) передаётся
        `STREAM_CHUNK_TIMEOUT_OPUS_SEC=150`. Sonnet/Haiku — default 60с.

        v1.0.86 (этап 5): добавлен `effort` параметр (low | medium |
        high | xhigh | max) — управляет thinking budget claude CLI.
        Diagnostic probe-скрипт показал что default effort (видимо
        high/xhigh) даёт 120-160с silence во время Opus thinking,
        а `--effort low` сокращает thinking почти до нуля (max silence
        2.6с, total 49с vs 200с). Для Scriptwriter/Editor (Opus 4.7)
        передаётся `effort="low"`. Качество карты на low проверяется
        пользователем — если ухудшилось, перевести на "medium" или
        отказаться от флага вообще.
        """
        if not self._cli:
            raise RuntimeError("claude CLI not found")
        # v1.0.86 (этап 4): резолвим эффективный chunk-timeout.
        # Параметр None → default 60с (Sonnet/Haiku); Opus передаёт 150с.
        timeout = chunk_timeout_sec or self.STREAM_CHUNK_TIMEOUT_SEC
        cmd = [self._cli, "-p",
               "--system-prompt", system_prompt,
               "--output-format", "stream-json",
               "--verbose",
               "--include-partial-messages",
               "--model", model]
        # v1.0.86 (этап 5): если задан thinking effort — пробрасываем
        # в CLI. Опциональный, чтобы не ломать существующие callsite.
        if effort:
            cmd.extend(["--effort", effort])
        popen_kwargs: dict = {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'encoding': 'utf-8',
            'errors': 'replace',
            'bufsize': 1,  # line-buffered — важно для своевременных JSONL чанков
        }
        if sys.platform == 'win32':
            CREATE_NO_WINDOW = 0x08000000
            popen_kwargs['creationflags'] = CREATE_NO_WINDOW

        proc = subprocess.Popen(cmd, **popen_kwargs)
        self._proc = proc
        try:
            # Пишем prompt в stdin и закрываем — claude ждёт EOF.
            try:
                assert proc.stdin is not None
                proc.stdin.write(user_prompt)
                proc.stdin.close()
            except Exception as e:
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise RuntimeError(f"failed to write stdin: {e}")

            # Reader-поток складывает строки stdout в очередь. Главный
            # поток дёргает queue.get(timeout=) — это и есть chunk-timeout.
            # select на pipes под Windows не работает, threading +
            # queue — кросс-платформенный паттерн.
            import threading
            import queue
            q: "queue.Queue[Optional[str]]" = queue.Queue()

            def _reader(stream, q):
                try:
                    for line in stream:
                        q.put(line)
                except Exception:
                    pass
                finally:
                    q.put(None)  # EOF marker

            t = threading.Thread(
                target=_reader, args=(proc.stdout, q), daemon=True)
            t.start()

            final_result: Optional[str] = None
            recv_bytes = 0
            total_started = time.monotonic()
            while True:
                # v1.0.86: ранний выход если внешний stop() пришёл.
                if self._stop:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise RuntimeError("cancelled")
                # Общий cap на всякий случай (бесконечно медленный поток).
                if time.monotonic() - total_started > self.SUBPROCESS_TIMEOUT_SEC:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"stream total timeout ({self.SUBPROCESS_TIMEOUT_SEC}s)")
                try:
                    line = q.get(timeout=timeout)
                except queue.Empty:
                    # Тишина больше chunk-timeout — TCP/HTTP стрим мёртв
                    # ИЛИ Opus thinking-блок не присылал deltas (для
                    # Opus callsite даём 150с — этого хватает на 81с
                    # тишины замеренные в probe + запас).
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"stream chunk timeout "
                        f"({timeout}s no activity)")
                if line is None:
                    break  # EOF reader-потока
                recv_bytes += len(line.encode('utf-8', errors='replace'))
                # Парсим JSONL. Невалидные строки игнорируем — CLI иногда
                # пишет служебный текст в stdout (ANSI, прогресс-точки).
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    chunk = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                # Финальный результат — единственный type=result чанк.
                if chunk.get('type') == 'result':
                    if chunk.get('is_error'):
                        # CLI зафиксировал ошибку — извлечь сообщение и
                        # поднять — caller обработает (как и для старого).
                        err = (chunk.get('api_error_status')
                               or chunk.get('result')
                               or 'unknown stream error')
                        raise RuntimeError(f"claude stream error: {err}")
                    final_result = chunk.get('result') or ""
                    # НЕ break: дочитываем хвост до EOF чтобы proc
                    # корректно завершился без SIGPIPE.

            # Ждём корректного завершения процесса (короткий wait — все
            # данные уже прочитаны).
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc.wait(timeout=2)

            if proc.returncode != 0 and final_result is None:
                # stderr достать (Popen.stderr — текстовый pipe, читаем
                # синхронно после EOF stdout — он уже не должен блокировать).
                stderr_text = ""
                try:
                    if proc.stderr is not None:
                        stderr_text = (proc.stderr.read() or "")[:500]
                except Exception:
                    pass
                raise RuntimeError(
                    f"claude exit={proc.returncode}: {stderr_text}")
            if final_result is None:
                raise RuntimeError(
                    "stream ended without type=result chunk "
                    f"(recv={recv_bytes}B)")
            return final_result.strip()
        finally:
            # Очищаем handle — даже при exception, чтобы внешний stop()
            # не дёргал terminate на уже-мёртвом процессе.
            self._proc = None

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Парсит JSON из ответа CLI. Удаляет возможные обёртки markdown
        ```json ... ``` если AI всё-таки их добавил."""
        if not raw:
            raise ValueError("empty response")
        cleaned = raw.strip()
        # Срезаем markdown-обёртку если есть.
        m = re.match(r'^```(?:json)?\s*(.*?)\s*```\s*$', cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
        # Ищем первую `{` и последнюю `}` если AI добавил пред-/пост-текст.
        first = cleaned.find('{')
        last = cleaned.rfind('}')
        if first != -1 and last != -1 and last > first:
            cleaned = cleaned[first:last + 1]
        return json.loads(cleaned)

    def _build_agent_summary(self, rounds_used: int) -> dict:
        """Собирает компактный отчёт по работе всех 4 агентов — для UI
        попапа сводки. Без огромных user_prompt'ов и raw-ответов.

        Структура:
            {
              "scriptwriter": {"ran": True, "blocks_written": N, "shots_total": N},
              "validator": {"runs": N, "rounds_passed": [{"round": 1, "errors": []}, ...]},
              "editor": {"runs": N, "rounds": [{"round": 1, "errors_in": N}, ...]},
              "context_reviewer": {"ran": True, "checks_performed": [...], "concerns": [...]}
            }
        """
        summary = {
            "scriptwriter": {"ran": False},
            "validator": {"runs": 0, "rounds_passed": []},
            "geometry_editor": {"ran": False},
            "editor": {"runs": 0, "rounds": []},
            "validator_r2": {"ran": False},
            "editor_r2": {"ran": False},
            "validator_r3": {"ran": False},
            "context_reviewer": {"ran": False},
            # v1.0.81: список post-check проходов (R1, R2)
            "post_check_timings": [],
            "rounds_used": rounds_used,
            # 2026-05-13 (v1.0.63): per-stage timings (вложено в
            # agent_summary чтобы не менять сигнатуру finished_ok).
            "timing": {"per_stage": [], "total_sec": 0.0},
        }
        for s in self._agent_log:
            stage = s.get('stage', '')
            # Timing: каждая стадия с замером попадает в timing.per_stage.
            # Если стадия завершилась ошибкой и duration не успел записаться —
            # пропускаем (строка в UI просто не появится).
            d = s.get('duration_sec')
            if isinstance(d, (int, float)) and stage:
                summary['timing']['per_stage'].append({
                    'stage': stage,
                    'model': s.get('model_used', ''),
                    'duration_sec': float(d),
                })
                summary['timing']['total_sec'] += float(d)
            if stage == 'scriptwriter':
                res = s.get('result', {}) or {}
                blocks = res.get('blocks', []) or []
                summary['scriptwriter'] = {
                    'ran': True,
                    'blocks_written': len(blocks),
                    'shots_total': sum(len(b.get('shots') or []) for b in blocks),
                    'total_seconds': res.get('total_seconds', 0),
                }
            elif stage == 'validator':
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                summary['validator']['runs'] += 1
                if error_msg and not res:
                    # v1.0.71: stage упал до записи result (TimeoutExpired
                    # или любой другой exception в _run_claude/CLI).
                    # Раньше попадало в else-ветку UI как «0 ошибок» — это
                    # вводило юзера в заблуждение. Помечаем failed=True
                    # чтобы UI отрисовал честную причину.
                    summary['validator']['rounds_passed'].append({
                        'ok': False,
                        'errors_count': 0,
                        'errors_codes': [],
                        'failed': True,
                        'error': str(error_msg),
                    })
                else:
                    summary['validator']['rounds_passed'].append({
                        'ok': res.get('ok'),
                        'errors_count': len(res.get('errors', []) or []),
                        'errors_codes': [
                            e.get('code', '?')
                            for e in (res.get('errors', []) or [])
                        ][:6],
                        # v1.0.76: полный список ошибок R1 — нужен UI
                        # для set-сравнения с validator_r2.errors.
                        'errors': list(res.get('errors', []) or []),
                    })
            elif stage == 'validator_r2':
                # v1.0.76: повторная валидация после Editor. Хранит errors
                # отдельно от R1 — UI рендерит set-сравнение R1\R2 / R1∩R2 /
                # R2\R1 для отчёта «исправил X из Y / создал N новых».
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                if error_msg and not res:
                    summary['validator_r2'] = {
                        'ran': True,
                        'failed': True,
                        'error': str(error_msg),
                        'ok': False,
                        'errors': [],
                    }
                else:
                    errs = res.get('errors', []) or []
                    summary['validator_r2'] = {
                        'ran': True,
                        'failed': False,
                        'ok': res.get('ok', False),
                        'errors': errs,  # полный список — UI считает sets
                    }
            elif stage == 'editor_r2':
                # v1.0.77: второй раунд Editor — если Validator R2
                # нашёл остаточные. Симметрично к editor R1 + honest UI
                # на exception (как v1.0.74 для R1).
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                if error_msg and not res:
                    summary['editor_r2'] = {
                        'ran': True,
                        'failed': True,
                        'error': str(error_msg),
                        'errors_in': 0,
                    }
                else:
                    summary['editor_r2'] = {
                        'ran': True,
                        'failed': False,
                        'errors_in': s.get('errors_in', 0),
                    }
            elif stage == 'validator_r3':
                # v1.0.77: третья валидация после Editor R2. Финальная
                # честная цифра остатка. UI рендерит set-сравнение R2 vs R3.
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                if error_msg and not res:
                    summary['validator_r3'] = {
                        'ran': True,
                        'failed': True,
                        'error': str(error_msg),
                        'ok': False,
                        'errors': [],
                    }
                else:
                    errs = res.get('errors', []) or []
                    summary['validator_r3'] = {
                        'ran': True,
                        'failed': False,
                        'ok': res.get('ok', False),
                        'errors': errs,
                    }
            elif stage in ('post_check_timings_r1',
                            'post_check_timings_r2'):
                # v1.0.81: post-check таймингов после Editor.
                round_num = 1 if stage.endswith('_r1') else 2
                summary['post_check_timings'].append({
                    'round': round_num,
                    'shots_checked': s.get('shots_checked', 0),
                    'shots_fixed': s.get('shots_fixed', 0),
                    'delta_total_seconds': s.get('delta_total_seconds', 0),
                    'old_total_seconds': s.get('old_total_seconds', 0),
                    'new_total_seconds': s.get('new_total_seconds', 0),
                })
            elif stage == 'geometry_editor':
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                if error_msg and not res:
                    # v1.0.75: honest UI на exception (та же паттерн что
                    # v1.0.71/v1.0.74 для других стадий).
                    summary['geometry_editor'] = {
                        'ran': True,
                        'errors_in': 0,
                        'failed': True,
                        'error': str(error_msg),
                    }
                else:
                    summary['geometry_editor'] = {
                        'ran': True,
                        'errors_in': s.get('errors_in', 0),
                        'failed': False,
                    }
            elif stage == 'editor':
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                summary['editor']['runs'] += 1
                if error_msg and not res:
                    # v1.0.74: editor упал до записи result (TimeoutExpired
                    # или другой exception в _run_claude). По аналогии с
                    # v1.0.71 fix для validator. Без этого UI выводил
                    # «Редактор — поправил 0 ошибок за 1 раунд(ов)» при
                    # реальном таймауте.
                    summary['editor']['rounds'].append({
                        'errors_in': 0,
                        'failed': True,
                        'error': str(error_msg),
                    })
                else:
                    summary['editor']['rounds'].append({
                        'errors_in': s.get('errors_in', 0),
                    })
            elif stage == 'context_reviewer':
                res = s.get('result', {}) or {}
                error_msg = s.get('error')
                if error_msg and not res:
                    # v1.0.74: context_reviewer упал до записи result. Без
                    # этого UI выводил «Финальный редактор — прошёл 0
                    # проверок по Bible'и, противоречий нет» при реальном
                    # таймауте (ran=True+ok=True по default'у).
                    summary['context_reviewer'] = {
                        'ran': True,
                        'ok': False,
                        'checks_performed': [],
                        'concerns': [],
                        'failed': True,
                        'error': str(error_msg),
                    }
                else:
                    summary['context_reviewer'] = {
                        'ran': True,
                        'ok': res.get('ok', True),
                        'checks_performed': res.get('checks_performed', []) or [],
                        'concerns': res.get('concerns', []) or [],
                    }
        summary['timing']['total_sec'] = round(
            summary['timing']['total_sec'], 2)
        return summary

    def _dump_log(self) -> Optional[str]:
        """Сохраняет агентский лог в JSON для дебага. Возвращает путь
        или None если не удалось."""
        if not self._log_path:
            return None
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text(
                json.dumps({
                    "timestamp": time.time(),
                    "models": {
                        "scriptwriter": self.MODEL_SCRIPTWRITER,
                        "validator": self.MODEL_VALIDATOR,
                        "editor": self.MODEL_EDITOR,
                        "context_reviewer": self.MODEL_CONTEXT_REVIEWER,
                    },
                    "scenario_chars": len(self._scenario),
                    "refs_summary": self._refs,
                    "stages": self._agent_log,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            return str(self._log_path)
        except Exception:
            return None
