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
)
from agents.validator_prefilter import prefilter_check


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
    # Откат при регрессии качества — git revert этого коммита.
    MODEL_SCRIPTWRITER     = "claude-opus-4-7"
    MODEL_VALIDATOR        = "claude-sonnet-4-6"
    MODEL_EDITOR           = "claude-sonnet-4-6"
    MODEL_CONTEXT_REVIEWER = "claude-sonnet-4-6"

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

    def stop(self):
        self._stop = True

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

            # 3) Editor — только если Validator нашёл ошибки
            if not checker_report.get("ok") and errors_count > 0:
                self.progress.emit("editor_running",
                                    {"errors_count": errors_count})
                try:
                    montage_card = self._call_editor(
                        montage_card, checker_report.get("errors", []))
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
        t0 = time.time()
        raw = self._run_claude(SCRIPTWRITER_SYSTEM, user,
                                model=self.MODEL_SCRIPTWRITER)
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

    def _call_validator(self, montage_card: dict) -> dict:
        # v1.0.69: Python pre-filter перед AI. 10 механических правил
        # (#1-#5, #7, #8, #10, #11, #13) проверяются в Python без LLM —
        # см. agents/validator_prefilter.py. AI получает урезанный
        # system_prompt только с правилами #6, #7а, #9, #12, #14
        # (семантика, требует reasoning).
        py_errors, rules_done = prefilter_check(montage_card, self._refs)
        validator_system = get_validator_system(skip_rules=rules_done)

        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_validator_user_prompt(
            card_json, self._refs, show_context=self._show_context)
        t0 = time.time()
        raw = self._run_claude(validator_system, user,
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
        self._agent_log.append({
            "stage": "validator",
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

    def _call_editor(self, montage_card: dict, errors: list) -> dict:
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_editor_user_prompt(
            card_json, errors, self._refs,
            original_scenario=self._scenario,
            show_context=self._show_context)
        t0 = time.time()
        raw = self._run_claude(EDITOR_SYSTEM, user,
                                model=self.MODEL_EDITOR)
        duration_sec = round(time.time() - t0, 2)
        new_card = self._parse_json(raw)
        self._agent_log.append({
            "stage": "editor",
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

    def _call_context_reviewer(self, montage_card: dict) -> dict:
        """Финальный супер-редактор. Проверяет соответствие карты
        Bible'и сериала и другим эпизодам. Возвращает dict с полями
        `ok` и `concerns`.
        """
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_context_reviewer_user_prompt(
            card_json, self._scenario, show_context=self._show_context)
        t0 = time.time()
        raw = self._run_claude(CONTEXT_REVIEWER_SYSTEM, user,
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
            "editor": {"runs": 0, "rounds": []},
            "context_reviewer": {"ran": False},
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
                    })
            elif stage == 'editor':
                summary['editor']['runs'] += 1
                summary['editor']['rounds'].append({
                    'errors_in': s.get('errors_in', 0),
                })
            elif stage == 'context_reviewer':
                res = s.get('result', {}) or {}
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
