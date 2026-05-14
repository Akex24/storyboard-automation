# -*- coding: utf-8 -*-
"""
widgets/montage_summary_dialog.py — попап-сводка монтажной карты после
работы оркестратора.

Содержит:
  • Таблицу блоков (Блок | Шотов | Секунд) + строку «Итого».
  • Раскрывашку «Показать как чекер посчитал» — детальный отчёт по
    репликам и таймингам.
  • Две кнопки: «✎ Поправить» (отмена, юзер вернётся к чату) и
    «🎨 Делать сториборды» (запуск генерации).

История: создано 2026-05-06 (Multi-agent монтажная карта).
"""

from __future__ import annotations

from typing import Optional, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit, QSizePolicy,
    QDialogButtonBox, QMessageBox
)

from i18n import tr


class MontageSummaryDialog(QDialog):
    """Попап-сводка по утверждённой монтажной карте."""

    # Эмитим signals чтобы не зависеть от .exec() (но можно и QDialog.Accepted)
    confirm_storyboards = pyqtSignal()
    edit_requested = pyqtSignal()

    def __init__(self, montage_card: dict, checker_report: dict,
                 rounds_used: int, agent_summary: Optional[dict] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('montage_summary_title'))
        self.setMinimumSize(700, 500)
        # 2026-05-08: LUMZ-стиль. Цвета из views/theme.py:LUMZ_THEME.
        self.setStyleSheet("""
            QDialog { background: #0e0a18; }
            QLabel { color: #ffffff; }
            QTextEdit { background: rgba(255,255,255,0.04);
                        color: #ffffff;
                        border: 1px solid rgba(255,255,255,0.06);
                        border-radius: 8px;
                        padding: 8px; }
            QPushButton { padding: 6px 14px; border-radius: 6px; }
        """)

        self._card = montage_card
        self._report = checker_report
        self._rounds_used = rounds_used
        self._agent_summary = agent_summary or {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        # 2026-05-06: Заголовок переписан — теперь это отчёт по 4 агентам
        # (а не одна строка про Чекер). Юзер видит что сделал каждый.
        ok = checker_report.get('ok', False)
        # 2026-05-08: LUMZ-палитра. ok → gold (выполнено), not-ok → red.
        head_color = "#d4a256" if ok else "#e4344a"
        head_lines = self._build_agent_lines()
        head = QLabel("\n".join(head_lines))
        head.setStyleSheet(
            f"color: {head_color}; font-size: 12px; "
            f"font-weight: 500; font-family: 'Menlo','Consolas',monospace;")
        head.setWordWrap(True)
        outer.addWidget(head)

        # v1.0.63: Таблица таймингов стадий (per-stage timings).
        # timing вложен в agent_summary['timing'] оркестратором — не меняет
        # сигнатуру finished_ok. Если ключа нет (старая сборка) — секция
        # просто не появится (forward-compat).
        timing_label = self._build_timing_label()
        if timing_label is not None:
            outer.addWidget(timing_label)

        # Таблица блоков
        blocks = montage_card.get('blocks', []) or []
        total_seconds = montage_card.get('total_seconds', 0)
        total_shots = sum(len(b.get('shots', []) or []) for b in blocks)

        table = QTableWidget(len(blocks) + 1, 3, self)
        table.setHorizontalHeaderLabels([
            tr('montage_summary_col_block'),
            tr('montage_summary_col_shots'),
            tr('montage_summary_col_seconds'),
        ])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        # 2026-05-08: LUMZ-стиль таблицы.
        table.setStyleSheet("""
            QTableWidget {
                background: rgba(255,255,255,0.04);
                color: #ffffff;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 8px;
                gridline-color: rgba(255,255,255,0.06);
            }
            QHeaderView::section {
                background: rgba(255,255,255,0.06);
                color: rgba(255,255,255,0.70);
                padding: 8px;
                border: 0;
                border-right: 1px solid rgba(255,255,255,0.06);
                font-weight: 500;
            }
            QTableWidget::item {
                padding: 6px;
            }
        """)
        for i, b in enumerate(blocks):
            n = b.get('n', i + 1)
            name = b.get('name', '')
            shots_n = len(b.get('shots', []) or [])
            block_secs = sum(s.get('duration_sec', 0) for s in (b.get('shots', []) or []))
            table.setItem(i, 0, QTableWidgetItem(f"Блок {n} — {name}"))
            table.setItem(i, 1, QTableWidgetItem(str(shots_n)))
            table.setItem(i, 2, QTableWidgetItem(f"{block_secs}с"))
        # Строка ИТОГО
        total_row = len(blocks)
        total_item_name = QTableWidgetItem(tr('montage_summary_total'))
        total_item_name.setForeground(Qt.GlobalColor.white)
        total_item_shots = QTableWidgetItem(str(total_shots))
        total_item_secs = QTableWidgetItem(f"{total_seconds}с")
        for it in (total_item_name, total_item_shots, total_item_secs):
            f = it.font()
            f.setBold(True)
            it.setFont(f)
        table.setItem(total_row, 0, total_item_name)
        table.setItem(total_row, 1, total_item_shots)
        table.setItem(total_row, 2, total_item_secs)
        outer.addWidget(table, stretch=1)

        # Раскрывашка «Как чекер посчитал»
        self._details_btn = QPushButton(tr('montage_summary_show_details'))
        # 2026-05-08: LUMZ — приглушённый «secondary link». Голубой `#a7c8ff`
        # → text_secondary с hover-подсветкой через :hover.
        self._details_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            " color: rgba(255,255,255,0.55); border: none;"
            " text-align: left; padding: 4px; font-size: 12px; }"
            "QPushButton:hover { color: #ffffff; }")
        self._details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._details_btn.clicked.connect(self._toggle_details)
        outer.addWidget(self._details_btn)

        self._details_view = QTextEdit()
        self._details_view.setReadOnly(True)
        self._details_view.hide()
        self._details_view.setMinimumHeight(180)
        self._details_view.setText(self._build_details_text())
        outer.addWidget(self._details_view, stretch=1)

        # Кнопки внизу
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        # 2026-05-06: «Поправить вручную» убрал — кнопка путала юзеров
        # (один клик и работа потеряна без видимой пользы). Если карта
        # не нравится — закрыть попап крестиком и заново нажать
        # «🎬 Сделать сториборды» в чате (оркестратор пройдёт ещё раз).
        self.confirm_btn = QPushButton(tr('montage_summary_btn_storyboards'))
        # 2026-05-08: LUMZ red primary CTA. Раньше был фиолетовый #4a5fcc.
        self.confirm_btn.setStyleSheet(
            "QPushButton { background: #e4344a; color: #ffffff;"
            " font-weight: 500; border: none;"
            " padding: 8px 18px; font-size: 13px;"
            " border-radius: 6px; }"
            "QPushButton:hover { background: #d92d44; }"
            "QPushButton:pressed { background: #c52539; }")
        self.confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.confirm_btn)

        outer.addLayout(btn_row)

    # ──────────────────────────────────────────────────────────────────

    def _build_agent_lines(self) -> List[str]:
        """Формирует читабельный отчёт по работе всех 4 агентов для
        заголовка попапа. Источник данных — `self._agent_summary` от
        оркестратора.
        """
        s = self._agent_summary or {}
        lines: List[str] = []
        # Сценарист
        sw = s.get('scriptwriter', {}) or {}
        if sw.get('ran'):
            lines.append(
                f"🎬 Сценарист — написал карту: "
                f"{sw.get('blocks_written', 0)} блоков, "
                f"{sw.get('shots_total', 0)} шотов, "
                f"{sw.get('total_seconds', 0)}с"
            )
        # Чекер
        v = s.get('validator', {}) or {}
        v_runs = v.get('runs', 0)
        validator_failed = False
        if v_runs > 0:
            rps = v.get('rounds_passed') or []
            last = rps[-1] if rps else {}
            if last.get('failed'):
                # v1.0.71: Чекер упал (TimeoutExpired или exception).
                # Показываем честно — раньше попадало в else-ветку
                # как «0 ошибок» и юзер думал что валидация прошла.
                validator_failed = True
                err = (last.get('error') or '').strip()
                low = err.lower()
                if 'timed out' in low or 'timeout' in low:
                    reason = "превысил лимит времени (10 минут)"
                else:
                    reason = (err.split('\n', 1)[0] or 'unknown')[:120]
                lines.append(
                    f"⚠ Чекер УПАЛ — {reason}. Карта НЕ ПРОВЕРЕНА — "
                    f"возможны нарушения правил, которые остались "
                    f"незамеченными."
                )
            elif last.get('ok'):
                if v_runs == 1:
                    lines.append(
                        "🔍 Чекер — прошёл 11 правил с первого раза, ошибок 0"
                    )
                else:
                    lines.append(
                        f"🔍 Чекер — за {v_runs} раунда(ов) подтвердил карту"
                    )
            else:
                errs = last.get('errors_count', 0)
                lines.append(
                    f"🔍 Чекер — нашёл {errs} ошибок (после {v_runs} раунда)"
                )
        # v1.0.75: Geometry Editor (Haiku-сабагент для shot.geometry)
        ge = s.get('geometry_editor', {}) or {}
        if ge.get('ran'):
            if ge.get('failed'):
                err = (ge.get('error') or '').strip()
                low = err.lower()
                if 'timed out' in low or 'timeout' in low:
                    reason = "превысил лимит времени (10 минут)"
                else:
                    reason = (err.split('\n', 1)[0] or 'unknown')[:120]
                lines.append(
                    f"⚠ Geometry Editor УПАЛ — {reason}. "
                    f"Missing_geometry-ошибки переданы основному Editor'у."
                )
            else:
                n = ge.get('errors_in', 0)
                lines.append(
                    f"📐 Geometry Editor — поправил {n} ошибок геометрии"
                )
        # Редактор
        ed = s.get('editor', {}) or {}
        ed_runs = ed.get('runs', 0)
        ed_rounds = ed.get('rounds') or []
        ed_failed_round = next(
            (r for r in ed_rounds if r.get('failed')), None)
        if ed_failed_round:
            # v1.0.74: Editor упал (TimeoutExpired или exception).
            # Раньше попадало в позитивную ветку как «поправил 0 ошибок
            # за 1 раунд» и юзер думал что Editor отработал.
            err = (ed_failed_round.get('error') or '').strip()
            low = err.lower()
            if 'timed out' in low or 'timeout' in low:
                reason = "превысил лимит времени (10 минут)"
            else:
                reason = (err.split('\n', 1)[0] or 'unknown')[:120]
            lines.append(
                f"⚠ Редактор УПАЛ — {reason}. Часть ошибок осталась без "
                f"правок — карта отдана в состоянии до Editor'а."
            )
        elif ed_runs > 0:
            errs_total = sum(r.get('errors_in', 0) for r in ed_rounds)
            lines.append(
                f"✏ Редактор — поправил {errs_total} ошибок за {ed_runs} раунд(ов)"
            )
        elif validator_failed:
            lines.append(
                "✏ Редактор — не запускался (Чекер упал, входа нет)"
            )
        else:
            lines.append("✏ Редактор — не запускался (нечего было править)")
        # Финальный редактор
        cr = s.get('context_reviewer', {}) or {}
        if cr.get('ran'):
            if cr.get('failed'):
                # v1.0.74: Context Reviewer упал. Раньше попадало в
                # позитивную ветку («прошёл 0 проверок, противоречий
                # нет») из-за default'ов ok=True/ran=True.
                err = (cr.get('error') or '').strip()
                low = err.lower()
                if 'timed out' in low or 'timeout' in low:
                    reason = "превысил лимит времени (10 минут)"
                else:
                    reason = (err.split('\n', 1)[0] or 'unknown')[:120]
                lines.append(
                    f"⚠ Финальный редактор УПАЛ — {reason}. "
                    f"Bible-сверка не выполнена."
                )
            else:
                checks = cr.get('checks_performed') or []
                concerns = cr.get('concerns') or []
                if cr.get('ok') and not concerns:
                    lines.append(
                        f"🎯 Финальный редактор — прошёл {len(checks)} проверок"
                        f" по Bible'и, противоречий нет"
                    )
                else:
                    lines.append(
                        f"🎯 Финальный редактор — нашёл {len(concerns)} замечаний"
                        f" (поправлено в раунде Редактора)"
                    )
        else:
            lines.append("🎯 Финальный редактор — не запускался")
        return lines

    # ──────────────────────────────────────────────────────────────────
    # v1.0.63: per-stage timing table
    # ──────────────────────────────────────────────────────────────────
    # Display-имена стадий (юзер просил не переводить — это технические
    # имена агентов).
    _STAGE_DISPLAY = {
        'scriptwriter':     'Scriptwriter',
        'validator':        'Validator',
        'editor':           'Editor',
        'context_reviewer': 'Context Reviewer',
    }
    # Порядок отрисовки — в каком пайплайн запускает стадии.
    _STAGE_ORDER = ('scriptwriter', 'validator', 'editor', 'context_reviewer')

    @staticmethod
    def _pretty_model(model_id: str) -> str:
        """claude-opus-4-7 → Opus 4.7, claude-sonnet-4-6 → Sonnet 4.6 и т.п."""
        if not model_id:
            return ''
        mapping = {
            'claude-opus-4-7':            'Opus 4.7',
            'claude-sonnet-4-6':          'Sonnet 4.6',
            'claude-haiku-4-5-20251001':  'Haiku 4.5',
        }
        return mapping.get(model_id, model_id)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """< 60 сек → 'X сек', >= 60 → 'X мин Y сек'. i18n через tr()."""
        s = max(0, int(round(seconds)))
        if s < 60:
            return tr('timing_unit_sec').format(sec=s)
        m, sec = divmod(s, 60)
        return tr('timing_unit_minsec').format(min=m, sec=sec)

    def _build_timing_label(self) -> Optional[QLabel]:
        """Возвращает QLabel с моноширинной таблицей таймингов стадий, либо
        None если timing-данных нет (старая сборка / пустой лог).
        """
        timing = (self._agent_summary or {}).get('timing') or {}
        per_stage = timing.get('per_stage') or []
        if not per_stage:
            return None
        # Сворачиваем в dict {stage: duration_sec_summed} — на случай
        # если в будущем стадия повторится (сейчас editor может запуститься
        # дважды если включён Context Reviewer и нашёл concerns).
        durations: Dict[str, float] = {}
        models: Dict[str, str] = {}
        for entry in per_stage:
            st = entry.get('stage') or ''
            d = entry.get('duration_sec') or 0
            if st:
                durations[st] = durations.get(st, 0.0) + float(d)
                # Модель берём из первого упоминания (для editor она одна).
                models.setdefault(st, entry.get('model') or '')
        # Собираем display-строки по фикс. порядку, только для стадий
        # которые реально запускались (юзер: не показывать «0 сек»).
        rows: List[tuple] = []  # (left_part, right_part)
        for stage_key in self._STAGE_ORDER:
            if stage_key not in durations:
                continue
            display_name = self._STAGE_DISPLAY.get(stage_key, stage_key)
            model_pretty = self._pretty_model(models.get(stage_key, ''))
            left = (f"{display_name} ({model_pretty}):" if model_pretty
                    else f"{display_name}:")
            right = self._format_duration(durations[stage_key])
            rows.append((left, right))
        if not rows:
            return None
        # ИТОГО
        total = float(timing.get('total_sec') or 0)
        total_right = self._format_duration(total)
        total_left = f"{tr('timing_total')}:"
        # Выравнивание колонок: подбираем ширину левой колонки по самой
        # длинной строке (включая ИТОГО). +2 для воздуха перед правой.
        max_left = max(len(r[0]) for r in rows + [(total_left, total_right)])
        pad = max_left + 2
        lines: List[str] = [tr('timing_section_title'), '']
        for left, right in rows:
            lines.append(f"  {left.ljust(pad)}{right}")
        # Разделитель перед ИТОГО — длина под ширину колонок.
        lines.append('  ' + '─' * (pad + max(len(r[1]) for r in rows + [(total_left, total_right)])))
        lines.append(f"  {total_left.ljust(pad)}{total_right}")
        label = QLabel("\n".join(lines))
        # Тот же моноширинный шрифт что у head, но цвет приглушённый —
        # таблица должна читаться, но не отвлекать от контента карты.
        label.setStyleSheet(
            "color: rgba(255,255,255,0.65); font-size: 12px; "
            "font-family: 'Menlo','Consolas',monospace;")
        return label

    def _build_details_text(self) -> str:
        """Формирует читабельный отчёт чекера + сами реплики из карты."""
        lines: List[str] = []
        blocks = self._card.get('blocks', []) or []
        report_blocks = {r.get('block_n'): r for r in (self._report.get('report') or [])}
        for b in blocks:
            n = b.get('n', '?')
            name = b.get('name', '')
            block_secs = sum(s.get('duration_sec', 0) for s in (b.get('shots', []) or []))
            shots_n = len(b.get('shots', []) or [])
            lines.append(f"━━━ БЛОК {n} — «{name}» — {block_secs}с / {shots_n} шот(а) ━━━")
            br = report_blocks.get(n) or {}
            sb = {s.get('shot_n'): s for s in (br.get('shot_breakdown') or [])}
            for shot in (b.get('shots', []) or []):
                sn = shot.get('n', '?')
                dur = shot.get('duration_sec', 0)
                desc = shot.get('description_ru', '')
                lines.append(f"  SHOT {sn} ({dur}с) — {desc}")
                dia = shot.get('dialog')
                if dia:
                    ru = dia.get('ru', '')
                    en = dia.get('en', '')
                    speech = dia.get('speech_type', 'normal')
                    speaker = dia.get('speaker', '?')
                    lines.append(f"    Реплика {speaker} ({speech}): «{ru}» («{en}»)")
                rb = sb.get(sn)
                if rb and rb.get('calc'):
                    lines.append(f"    🔍 Чекер: {rb['calc']}")
            lines.append("")
        # Ошибки от чекера (если остались)
        errors = self._report.get('errors') or []
        if errors:
            lines.append("━━━ НЕРАЗРЕШЁННЫЕ ОШИБКИ ОТ ЧЕКЕРА ━━━")
            for e in errors:
                lines.append(f"  • [{e.get('code', '?')}] {e.get('details', '')}")
        # 2026-05-06: отчёт Финального Редактора (что он реально проверил)
        cr = (self._agent_summary or {}).get('context_reviewer', {}) or {}
        if cr.get('ran'):
            lines.append("")
            lines.append("━━━ ОТЧЁТ ФИНАЛЬНОГО РЕДАКТОРА (Bible-сверка) ━━━")
            checks = cr.get('checks_performed') or []
            if checks:
                for c in checks:
                    name = c.get('check', '?')
                    det = c.get('details', '')
                    lines.append(f"  ✓ {name}")
                    if det:
                        # Перенос длинных строк по словам.
                        for chunk in [det[i:i+100]
                                       for i in range(0, len(det), 100)]:
                            lines.append(f"      {chunk}")
            else:
                lines.append("  (отчёт пуст — ничего не проверил, "
                              "обнови промпт)")
            crc = cr.get('concerns') or []
            if crc:
                lines.append("")
                lines.append("  Найденные замечания:")
                for c in crc:
                    lines.append(f"    • [{c.get('code', '?')}] {c.get('details', '')}")
        return "\n".join(lines)

    def _toggle_details(self):
        if self._details_view.isVisible():
            self._details_view.hide()
            self._details_btn.setText(tr('montage_summary_show_details'))
        else:
            self._details_view.show()
            self._details_btn.setText(tr('montage_summary_hide_details'))

    def _on_confirm(self):
        # Юзер кликнул «🎨 Делать сториборды» — это явный путь дальше,
        # подтверждение не нужно. Помечаем флаг чтобы override reject
        # не показал предупреждение.
        self._user_confirmed = True
        self.confirm_storyboards.emit()
        self.accept()

    def _on_edit(self):
        self._user_confirmed = True
        self.edit_requested.emit()
        self.reject()

    def reject(self):
        """2026-05-06: при попытке закрытия без клика «Делать сториборды»
        (крестик / Esc / закрытие окна) — запрашиваем подтверждение,
        потому что вся работа агентов потеряется и придётся запускать
        оркестратор заново (1-3 минуты)."""
        if getattr(self, '_user_confirmed', False):
            super().reject()
            return
        m = QMessageBox(self)
        m.setIcon(QMessageBox.Icon.Warning)
        m.setWindowTitle(tr('montage_discard_title'))
        m.setText(tr('montage_discard_text'))
        yes = m.addButton(tr('montage_discard_yes'),
                           QMessageBox.ButtonRole.DestructiveRole)
        no = m.addButton(tr('montage_discard_no'),
                          QMessageBox.ButtonRole.RejectRole)
        m.setDefaultButton(no)
        m.exec()
        if m.clickedButton() is yes:
            self._user_confirmed = True
            super().reject()
        # Иначе остаёмся в диалоге.

    def closeEvent(self, event):
        """Перехватываем системное закрытие окна (X в углу) — направляем
        в наш reject() с подтверждением."""
        if getattr(self, '_user_confirmed', False):
            event.accept()
            return
        # reject() сам спросит подтверждение и закроет диалог если ОК.
        event.ignore()
        self.reject()
