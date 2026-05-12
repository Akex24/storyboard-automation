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
)


class MontageOrchestratorThread(QThread):
    """Поток: запускает Сценарист → Чекер → Редактор → Чекер → … до 3
    раундов. Эмитит сигналы прогресса для UI и финальный сигнал с
    итоговой картой + отчётом Чекера.
    """

    # стадии: "scriptwriter_running", "validator_running",
    # "editor_running", "context_reviewer_running", "round_done",
    # "context_reviewer_done"
    progress = pyqtSignal(str, dict)
    # финальный сигнал: monton_card (dict), checker_report (dict),
    # rounds_used (int), agent_log_path (str), agent_summary (dict)
    # agent_summary — компактный отчёт для UI попапа: что делал каждый агент.
    finished_ok = pyqtSignal(dict, dict, int, str, dict)
    # ошибка, не удалось получить даже первой версии
    failed = pyqtSignal(str)

    MAX_ROUNDS = 3
    SUBPROCESS_TIMEOUT_SEC = 600  # 10 минут на каждый вызов CLI

    # 2026-05-09: per-agent model routing. Юзер не выбирает модели для
    # пайплайнов — каждый агент прибит к задаче. Validator — механический
    # чек-лист (формула тайминга, whitelist значений), Sonnet справляется
    # отлично и даёт ~3× ускорение. Scriptwriter и Context Reviewer —
    # творческие/семантические, остаются на Opus.
    MODEL_SCRIPTWRITER     = "claude-opus-4-7"
    MODEL_VALIDATOR        = "claude-sonnet-4-6"
    # 2026-05-12 (v1.0.54): Editor переведён на Sonnet 4.6.
    # Задача дисциплинированная — применить N исправлений из errors[]
    # к JSON-карте (тайминги реплик, расстановка шотов, разбивка
    # блоков). Творческие элементы (микромимика, стилевая ДНК) идут
    # позже в PromptWriter (Nano Banana) / Seedance — там Opus остаётся.
    # Ожидаемый эффект: монтажная карта ~15 мин → ~5-7 мин на эпизод
    # (Editor — самая прожорливая стадия, 60-70% времени).
    MODEL_EDITOR           = "claude-sonnet-4-6"
    MODEL_CONTEXT_REVIEWER = "claude-opus-4-7"

    def __init__(self, claude_cli_path: str,
                 scenario_text: str,
                 refs_summary: dict,
                 show_context: Optional[dict] = None,
                 log_path: Optional[Path] = None,
                 parent=None):
        super().__init__(parent)
        self._cli = claude_cli_path
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
            rounds_used = 0

            # 2-3) Validator → Editor цикл, до MAX_ROUNDS раундов
            for round_idx in range(1, self.MAX_ROUNDS + 1):
                rounds_used = round_idx
                self.progress.emit("validator_running",
                                    {"round": round_idx,
                                     "max_rounds": self.MAX_ROUNDS})
                try:
                    checker_report = self._call_validator(montage_card)
                except Exception as e:
                    # Не fatal — оставляем последнюю карту, репорт пустой
                    self._agent_log.append({
                        "stage": "validator",
                        "round": round_idx,
                        "error": str(e),
                    })
                    break

                if self._stop:
                    self.failed.emit("cancelled")
                    return

                self.progress.emit("round_done",
                                    {"round": round_idx,
                                     "ok": checker_report.get("ok", False),
                                     "errors_count": len(checker_report.get("errors", []))})

                if checker_report.get("ok"):
                    # 4) Context Reviewer — финальный супер-редактор.
                    #    Проверяет соответствие Bible'и и другим эпизодам.
                    #    Если есть concerns — даём ещё один раунд Редактора
                    #    (только если не превысили MAX_ROUNDS).
                    self.progress.emit("context_reviewer_running",
                                        {"round": round_idx})
                    try:
                        reviewer_report = self._call_context_reviewer(
                            montage_card)
                    except Exception as e:
                        # Не fatal — Чекер уже подтвердил карту, идём
                        # дальше с предупреждением в логе.
                        self._agent_log.append({
                            "stage": "context_reviewer",
                            "round": round_idx,
                            "error": str(e),
                        })
                        break

                    concerns = reviewer_report.get("concerns") or []
                    self.progress.emit("context_reviewer_done",
                                        {"round": round_idx,
                                         "ok": reviewer_report.get("ok", True),
                                         "concerns_count": len(concerns)})

                    if reviewer_report.get("ok") or not concerns:
                        # Карта чистая по всем фронтам — выходим.
                        break

                    # Reviewer нашёл проблемы → конвертируем concerns в
                    # формат errors[] для Редактора и идём ещё раунд.
                    if round_idx >= self.MAX_ROUNDS:
                        break  # лимит раундов исчерпан, оставляем
                                # карту с пометкой в логе

                    converted_errors = [
                        {
                            "code": c.get("code", "context_concern"),
                            "where": c.get("where", ""),
                            "details": c.get("details", ""),
                        }
                        for c in concerns
                    ]
                    self.progress.emit("editor_running",
                                        {"round": round_idx,
                                         "errors_count": len(converted_errors)})
                    try:
                        montage_card = self._call_editor(montage_card,
                                                           converted_errors)
                    except Exception as e:
                        self._agent_log.append({
                            "stage": "editor_after_reviewer",
                            "round": round_idx,
                            "error": str(e),
                        })
                        break
                    continue  # → следующий раунд Validator → Reviewer

                if round_idx >= self.MAX_ROUNDS:
                    break  # лимит раундов

                # Редактор правит ошибки от Чекера
                self.progress.emit("editor_running",
                                    {"round": round_idx,
                                     "errors_count": len(checker_report.get("errors", []))})
                try:
                    montage_card = self._call_editor(montage_card,
                                                       checker_report.get("errors", []))
                except Exception as e:
                    self._agent_log.append({
                        "stage": "editor",
                        "round": round_idx,
                        "error": str(e),
                    })
                    break

                if self._stop:
                    self.failed.emit("cancelled")
                    return

            log_path_str = self._dump_log()
            agent_summary = self._build_agent_summary(rounds_used)
            self.finished_ok.emit(montage_card, checker_report, rounds_used,
                                   log_path_str or "", agent_summary)
        except Exception as e:
            self._dump_log()
            self.failed.emit(f"unexpected: {e}")

    # ──────────────────────────────────────────────────────────────────
    # Конкретные вызовы агентов через CLI.
    # ──────────────────────────────────────────────────────────────────

    def _call_scriptwriter(self) -> dict:
        user = build_scriptwriter_user_prompt(
            self._scenario, self._refs, show_context=self._show_context)
        raw = self._run_claude(SCRIPTWRITER_SYSTEM, user,
                                model=self.MODEL_SCRIPTWRITER)
        montage = self._parse_json(raw)
        self._agent_log.append({
            "stage": "scriptwriter",
            "round": 1,
            "model_used": self.MODEL_SCRIPTWRITER,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "result": montage,
        })
        return montage

    def _call_validator(self, montage_card: dict) -> dict:
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_validator_user_prompt(
            card_json, self._refs, show_context=self._show_context)
        raw = self._run_claude(VALIDATOR_SYSTEM, user,
                                model=self.MODEL_VALIDATOR)
        report = self._parse_json(raw)
        self._agent_log.append({
            "stage": "validator",
            "model_used": self.MODEL_VALIDATOR,
            "user_prompt_chars": len(user),
            "raw_response_chars": len(raw),
            "parsed_ok": True,
            "result": report,
        })
        return report

    def _call_editor(self, montage_card: dict, errors: list) -> dict:
        card_json = json.dumps(montage_card, ensure_ascii=False, indent=2)
        user = build_editor_user_prompt(
            card_json, errors, self._refs,
            original_scenario=self._scenario,
            show_context=self._show_context)
        raw = self._run_claude(EDITOR_SYSTEM, user,
                                model=self.MODEL_EDITOR)
        new_card = self._parse_json(raw)
        self._agent_log.append({
            "stage": "editor",
            "model_used": self.MODEL_EDITOR,
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
        raw = self._run_claude(CONTEXT_REVIEWER_SYSTEM, user,
                                model=self.MODEL_CONTEXT_REVIEWER)
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
        }
        for s in self._agent_log:
            stage = s.get('stage', '')
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
                summary['validator']['runs'] += 1
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
