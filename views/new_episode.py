# -*- coding: utf-8 -*-
"""
views/new_episode.py — вкладка «🎬 Новый эпизод» Storyboard Studio.

Содержит:
    - NewEpisodeView — вся вкладка (drop-зона + paste + поле эпизода +
      дропдаун модели + Run/Stop + лог + чат-followup + плашка перехода
      в чат эпизода).

Зависимости от storyboard_app.py (через `_AppProxy` lazy proxy):
    - APP_ORG, APP_NAME (для QSettings — ключ "new_ep/model_v2")
    - block_wheel_event — блокировка колёсика на дропдауне модели
    - extract_text_from_file — парсер дропа файла
    - find_claude_cli — проверка наличия CLI перед запуском
    - parse_episode_number, find_episode_section — извлечение секции серии
    - read_episodes_meta — чтение episodes.json
    - append_chat_message — запись реплики в jsonl эпизода
    - detect_line_kind, format_chat_inline, CHAT_LINE_COLORS — рендер чата

Зависимости от threads / views (прямой импорт):
    - RunEpisodeThread (threads.generate)
    - ChatInputEdit (views.episode_chat — вынесен в шаге 5B)

История: вытащено из storyboard_app.py 2026-05-04 (шаг 5C рефакторинга).
"""

from __future__ import annotations

import json
import re
import traceback
from pathlib import Path
from typing import Optional, Dict

from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QPlainTextEdit, QTextEdit,
    QComboBox, QLineEdit, QVBoxLayout, QHBoxLayout,
)

from i18n import tr, get_lang
from threads import RunEpisodeThread
from views._chat_render import parse_gen_markers
from views.episode_chat import ChatInputEdit


class _AppProxy:
    """Прокси к module storyboard_app — приоритет __main__.
    См. подробное объяснение в threads/update.py."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


class NewEpisodeView(QWidget):
    """Вкладка «🎬 Новый эпизод».

    Юзер либо перетаскивает файл сценария на drop-зону, либо вставляет текст
    в большое поле. Указывает какую серию делаем. Жмёт «🤖 Запустить» —
    Studio сохраняет сценарий в `shows/<slug>/scenarios/_inbox.txt`,
    пытается вырезать секцию указанной серии регексом, формирует промпт и
    запускает `RunEpisodeThread`. Поток ответа Claude стримится в правую
    панель.

    Сигналы наружу — нет; всё взаимодействует напрямую с MainWindow через
    переданный `main_window` (для доступа к project_root, активному
    сериалу, статус-бару)."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._mw = main_window
        self._pending_text: str = ""    # текст из дропа или пасты, ждёт submit
        self._pending_source: str = ""  # 'file' / 'paste' — для статусной строки
        self._thread: Optional[RunEpisodeThread] = None
        # 2026-05-07: per-episode реестр первичных треды-аналитиков —
        # юзер может запустить ep4, потом сразу же ep5, не дожидаясь.
        # Каждый тред живёт здесь по своему ep_id. `self._thread` остаётся
        # как ссылка на ПОСЛЕДНИЙ запущенный (для совместимости со старым
        # кодом проверки isRunning).
        self._threads: Dict[str, RunEpisodeThread] = {}
        # Запоминаем последний статус как (key, params, color) — нужно чтобы при
        # смене языка перевести строку (status_lbl динамический, не входит в
        # фиксированные label'ы).
        self._status_key: Optional[str] = None
        self._status_params: Dict = {}
        self._status_color: str = "#888"
        # Был ли уже хотя бы один успешный запуск — определяет можно ли
        # продолжать сессию через `claude --continue` (chat-режим)
        self._has_initial_run: bool = False
        # Phase 2.1: авто-переезд в чат эпизода сразу после клика «🤖 Запустить».
        # Когда True — поток ещё работает, но юзер уже в EpisodeChatView,
        # а UI на «+» очищен (creation_block видим, log_view пустой). Все
        # chunks/сообщения идут только в jsonl и в EpisodeChatView (не в
        # наш log_view, чтобы при возврате на «+» он был чистый).
        self._handed_off: bool = False
        # Phase 2 hotfix #8: накопитель полного ответа AI для fallback-парсера.
        # Сбрасывается в `_on_run` перед стартом потока. Если AI не вставил
        # [[GEN:...]] маркеры — синтезируем их в `_on_thread_finished`.
        self._stream_full: str = ''
        # ID эпизода над которым сейчас работает Claude. Устанавливается
        # в `_on_run` как только мы распарсили номер серии («эпизод 22» → "ep22").
        # Все строки чата (юзер, Claude, системные) пишутся в
        # `chats/<ep_id>.jsonl` чтобы переживать перезапуск .app и переезжать
        # в Editor → ЭП → ЧАТ после первого успешного ответа.
        self._current_ep_id: Optional[str] = None
        # Таймер анимации точек у статуса «Claude думает» во время работы потока
        self._thinking_step: int = 0
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(400)
        self._thinking_timer.timeout.connect(self._tick_thinking)
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        # Phase 2 UX-переделки (2026-05-04): вертикальный chat-style layout.
        # Сверху compact creation-блок (drop+paste+поле эпизода+model+run),
        # под ним status, log_view, плашка «→ Открыть чат», chat-input.
        # После клика «Запустить» creation-блок скрывается → получается
        # обычный чат с логом. Если ошибка/стоп ДО первого успеха —
        # creation-блок возвращается чтобы юзер мог поправить и снова
        # запустить (см. _on_thread_error / _on_thread_stopped).
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(28, 12, 28, 16)

        # ── Compact creation-блок (скрывается после старта генерации) ────
        self.creation_block = QWidget()
        cb = QVBoxLayout(self.creation_block)
        cb.setSpacing(6)
        cb.setContentsMargins(0, 0, 0, 0)

        # Phase 2 hotfix #6: title_lbl создан orphan'ом (не в layout) —
        # «Запустить новый эпизод» уже отображается в шапке Editor'а
        # рядом с пилюлями (через `ep_title_label`). Дубль выкинут чтобы
        # drop-зона поднялась выше. Атрибут оставлен для apply_lang.
        self.title_lbl = QLabel(tr('new_ep_title'))
        self.title_lbl.hide()

        # 2026-05-05 v3: drop-зона живёт ТУТ, внутри NewEpisodeView.
        # Раньше она была на стартовом экране редактора — но юзер сказал
        # что это сбивает с толку (виден до клика «+»). Теперь видна
        # только когда юзер открыл форму создания эпизода.
        # Сигнал file_dropped → _on_scenario_drop_in_form парсит документ
        # (bible + episodes) и сохраняет идентично пасте — workflow
        # объединён.
        from views.scenario_drop_zone import ScenarioDropZone
        self.scenario_drop_zone = ScenarioDropZone(self)
        self.scenario_drop_zone.file_dropped.connect(
            self._on_scenario_drop_in_form)
        cb.addWidget(self.scenario_drop_zone)
        # Заглушки для apply_lang (старый Зона-2 виджет, теперь не используется).
        self.drop_frame = self.scenario_drop_zone
        self.drop_lbl = QLabel("")
        self.drop_lbl.hide()
        self.drop_hint_lbl = QLabel("")
        self.drop_hint_lbl.hide()

        # Paste-поле — компактное (не растягивается, как было в 2-колоночном)
        self.paste_label = QLabel(tr('new_ep_paste_label'))
        self.paste_label.setStyleSheet("color:#aaa; font-size:12px;")
        cb.addWidget(self.paste_label)

        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText(tr('new_ep_paste_placeholder'))
        self.paste_edit.setStyleSheet(
            "QPlainTextEdit { background:#15101e; border:1px solid #322545;"
            " border-radius:6px; padding:8px; color:#ddd; font-size:13px; }")
        # Phase 2 hotfix #6: paste-поле выше (юзер просил «блок ввода
        # сценария можно сделать повыше»). Min 120, max снят чтобы при
        # большом окне поле тянулось.
        self.paste_edit.setMinimumHeight(120)
        self.paste_edit.textChanged.connect(self._on_paste_changed)
        cb.addWidget(self.paste_edit, stretch=1)

        # Поле «Какую серию» + хинт
        ep_row = QHBoxLayout()
        ep_row.setSpacing(10)
        self.ep_label = QLabel(tr('new_ep_episode_label'))
        self.ep_label.setStyleSheet("color:#ddd; font-size:13px; font-weight:500;")
        ep_row.addWidget(self.ep_label)
        self.ep_edit = QLineEdit()
        self.ep_edit.setPlaceholderText(tr('new_ep_episode_placeholder'))
        self.ep_edit.setStyleSheet(
            "QLineEdit { background:#15101e; border:1px solid #322545;"
            " border-radius:6px; padding:6px 10px; color:#ddd; font-size:13px; }")
        self.ep_edit.setMaximumWidth(260)
        self.ep_edit.textChanged.connect(self._update_run_btn_state)
        ep_row.addWidget(self.ep_edit)
        ep_row.addStretch()
        cb.addLayout(ep_row)

        self.ep_hint_lbl = QLabel(tr('new_ep_episode_hint'))
        self.ep_hint_lbl.setStyleSheet(
            "color:#666; font-size:11px; font-style:italic;")
        self.ep_hint_lbl.setWordWrap(True)
        cb.addWidget(self.ep_hint_lbl)

        # 2026-05-09: дропдаун модели убран. Все пайплайны прибиты
        # к моделям в коде (см. _internal/ARCHITECTURE.md). Свободный
        # чат серии управляется дропдауном в шапке EpisodeChatView —
        # это значение переживает в QSettings ключ "new_ep/model_v2"
        # и подхватывается _current_model() ниже.
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.run_btn = QPushButton(tr('new_ep_run_btn'))
        self.run_btn.setObjectName("save")
        self.run_btn.setFixedHeight(38)
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        btn_row.addWidget(self.run_btn, stretch=1)
        # stop_btn создаётся скрытым — оставлен для совместимости с
        # set_enabled-вызовами в _on_run/_on_thread_finished и т.п.
        self.stop_btn = QPushButton(tr('new_ep_stop_btn'))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.hide()
        cb.addLayout(btn_row)

        outer.addWidget(self.creation_block)

        # ── Status (между creation-блоком и логом) ──────────────────────
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color:#888; font-size:12px;")
        self.status_lbl.setWordWrap(True)
        # Phase 2 hotfix #5: status скрыт до запуска генерации — пустая
        # строка не должна занимать место и зрительно «давить» на пилюли.
        self.status_lbl.hide()
        outer.addWidget(self.status_lbl)

        # ── Лог (chat-style, растягивается) ─────────────────────────────
        # QTextEdit (не QPlainTextEdit) → можно вставлять HTML и красить
        # отдельные строки. Подсветка — в _append_log по эвристике.
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "QTextEdit { background:#0f0a18; border:1px solid #25193a;"
            " border-radius:6px; padding:10px; color:#cfcfcf;"
            " font-family:'Menlo','Consolas',monospace; font-size:12px; }")
        # Phase 2 hotfix #5: log_view скрыт до старта генерации. Пустой
        # большой чёрный блок никому не нужен и визуально «давил» на
        # шапку (юзер обозначил это как баг 2026-05-04).
        self.log_view.hide()
        outer.addWidget(self.log_view, stretch=1)

        # ── Плашка «→ Открыть чат эпизода» (появляется после первого
        #    успешного ответа AI) ────────────────────────────────────────
        self.open_chat_row = QWidget()
        ocr = QHBoxLayout(self.open_chat_row)
        ocr.setContentsMargins(0, 4, 0, 0)
        ocr.setSpacing(0)
        self.open_chat_btn = QPushButton(tr('open_episode_chat_btn', ep=''))
        self.open_chat_btn.setObjectName("open-chat-pill")
        self.open_chat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_chat_btn.setStyleSheet(
            "QPushButton#open-chat-pill {"
            "  background: rgba(110,76,196,0.20);"
            "  border: 1px solid rgba(160,120,240,0.55);"
            "  border-radius: 10px;"
            "  color: #d8c8ff;"
            "  padding: 12px 18px;"
            "  font-size: 13px;"
            "  font-weight: 600;"
            "  text-align: left;"
            "}"
            "QPushButton#open-chat-pill:hover {"
            "  background: rgba(140,100,220,0.32);"
            "  border-color: rgba(190,150,255,0.75);"
            "  color: #ffffff;"
            "}")
        self.open_chat_btn.setToolTip(tr('open_episode_chat_hint'))
        self.open_chat_btn.clicked.connect(self._on_open_chat_clicked)
        ocr.addWidget(self.open_chat_btn, stretch=1)
        self.open_chat_row.hide()
        outer.addWidget(self.open_chat_row)

        # ── Chat-input внизу (для followup'ов через `claude --continue`).
        #    Скрыт пока нет первого успешного run'а. ─────────────────────
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        chat_row.setContentsMargins(0, 0, 0, 0)
        self.chat_input = ChatInputEdit()
        self.chat_input.setPlaceholderText(tr('new_ep_chat_placeholder'))
        self.chat_input.setStyleSheet(
            "QPlainTextEdit { background:#15101e; border:1px solid #322545;"
            " border-radius:6px; padding:8px; color:#ddd; font-size:13px; }")
        self.chat_input.setFixedHeight(70)
        self.chat_input.submit_requested.connect(self._on_send_followup)
        self._chat_send_shortcut = QShortcut(
            QKeySequence("Ctrl+Return"), self.chat_input)
        self._chat_send_shortcut.activated.connect(self._on_send_followup)
        chat_row.addWidget(self.chat_input, stretch=1)
        self.send_btn = QPushButton(tr('new_ep_send_btn'))
        self.send_btn.setObjectName("save")
        self.send_btn.setFixedHeight(70)
        self.send_btn.setMinimumWidth(120)
        self.send_btn.clicked.connect(self._on_send_followup)
        chat_row.addWidget(self.send_btn)
        self.chat_row_widget = QWidget()
        self.chat_row_widget.setLayout(chat_row)
        self.chat_row_widget.hide()
        outer.addWidget(self.chat_row_widget)

        # log_title_lbl убран в Phase 2 (был дубль "Лог Claude" во второй
        # колонке HBox-layout'а). Создаём orphan-виджет чтобы apply_lang
        # и любой внешний код продолжали работать без падений.
        self.log_title_lbl = QLabel("")
        self.log_title_lbl.hide()

    def apply_lang(self):
        """Перевести все строки на текущий язык."""
        self.title_lbl.setText(tr('new_ep_title'))
        # Drop-зона внутри формы — обновляем её тексты тоже.
        if hasattr(self, 'scenario_drop_zone') and self.scenario_drop_zone is not None:
            try:
                self.scenario_drop_zone.retranslate()
            except Exception:
                pass
        self.paste_label.setText(tr('new_ep_paste_label'))
        self.paste_edit.setPlaceholderText(tr('new_ep_paste_placeholder'))
        self.ep_label.setText(tr('new_ep_episode_label'))
        self.ep_edit.setPlaceholderText(tr('new_ep_episode_placeholder'))
        self.ep_hint_lbl.setText(tr('new_ep_episode_hint'))
        self.run_btn.setText(tr('new_ep_run_btn'))
        self.stop_btn.setText(tr('new_ep_stop_btn'))
        self.log_title_lbl.setText(tr('new_ep_log_title'))
        self.chat_input.setPlaceholderText(tr('new_ep_chat_placeholder'))
        self.send_btn.setText(tr('new_ep_send_btn'))
        # Кнопка-плашка перехода в чат эпизода — переводим текст с текущим ep_id
        if hasattr(self, 'open_chat_btn'):
            ep_label = (self._current_ep_id or '').upper()
            self.open_chat_btn.setText(tr('open_episode_chat_btn', ep=ep_label))
            self.open_chat_btn.setToolTip(tr('open_episode_chat_hint'))
        # Перевести текущий статус (Текст вставлен / Загружен файл / Готово / …)
        if self._status_key:
            self.status_lbl.setText(tr(self._status_key, **self._status_params))
            self.status_lbl.setStyleSheet(
                f"color:{self._status_color}; font-size:12px;")

    def _set_status(self, key: Optional[str], color: str = "#888", **params):
        """Единая точка установки статусной строки. Запоминает ключ+параметры
        чтобы apply_lang смог переустановить текст на новом языке.
        Если key=None — очищает статус."""
        self._status_key = key
        self._status_params = params
        self._status_color = color
        if key is None:
            self.status_lbl.setText("")
        else:
            self.status_lbl.setText(tr(key, **params))
        self.status_lbl.setStyleSheet(f"color:{color}; font-size:12px;")

    def _tick_thinking(self):
        """Анимация бегущих точек у статуса «Claude думает».
        Запускается на старте потока, останавливается при завершении.
        Использует _thinking_step (0..2) для выбора паттерна точек."""
        # Если поток уже не работает или статус-ключ изменился — стоп
        if (self._thread is None or not self._thread.isRunning()
                or self._status_key != 'new_ep_log_thinking'):
            self._thinking_timer.stop()
            return
        self._thinking_step = (self._thinking_step + 1) % 4
        # 4 кадра: «·   », «··  », «··· », «····» (пустые символы для стабильной ширины)
        dots = ["·   ", "··  ", "··· ", "····"][self._thinking_step]
        base = tr('new_ep_log_thinking')
        self.status_lbl.setText(f"{base} {dots}")
        # Также бегущие точки прямо в чате (в последней строке log_view с
        # маркером `▶ Думаю`). Если после строки уже пришёл chunk ассистента —
        # точки замораживаются (см. _update_thinking_in_log).
        self._update_thinking_in_log(dots)

    def _on_slow_thinking(self):
        """RunEpisodeThread эмитит сигнал через 120с без первого chunk.
        Пишем системную подсказку в чат целевого эпизода (через sender'а
        чтобы при параллельных запусках не путались эпизоды)."""
        sender = self.sender()
        target_ep = getattr(sender, '_ep_id', None) if sender is not None else None
        target_ep = target_ep or self._current_ep_id
        if not target_ep:
            return
        line = f"{tr('new_ep_log_thinking_long')}\n\n"
        try:
            _sa.append_chat_message(target_ep, "system", line, kind='system')
        except Exception:
            pass
        # Если этот эпизод сейчас активен в форме «+» — отрисуем строку и тут
        if target_ep == self._current_ep_id and not self._handed_off:
            self._append_log(line, kind='system')
        # Роутим в EpisodeChatView если он открыт на этот ep
        try:
            ev = getattr(self._mw, 'episode_chat_view', None)
            if ev is not None:
                ev.on_external_append(target_ep, line, 'system')
        except Exception:
            pass

    def _update_thinking_in_log(self, dots: str):
        """Обновить хвост последней строки `▶ Думаю` в log_view бегущими
        точками. После прихода первого chunk строка перестаёт обновляться."""
        base = tr('new_ep_log_thinking')
        marker = f"▶ {base}"
        doc = self.log_view.document()
        plain = doc.toPlainText()
        idx = plain.rfind(marker)
        if idx < 0:
            return
        end_idx = plain.find('\n', idx)
        if end_idx < 0:
            end_idx = len(plain)
        if plain[end_idx:].strip():
            return
        cursor = QTextCursor(doc)
        cursor.setPosition(idx)
        cursor.setPosition(end_idx, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat()
        cursor.removeSelectedText()
        cursor.insertText(f"{marker} {dots}", fmt)

    # ── Drag & Drop ─────────────────────────────────────────────────────
    # 2026-05-05: dragEnterEvent / dragLeaveEvent / dropEvent УДАЛЕНЫ —
    # теперь файлы сценариев принимает только верхняя ScenarioDropZone
    # на стартовом экране редактора. Здесь остаётся paste + ввод номера
    # серии. Если юзер пытается перетащить файл прямо в этот виджет —
    # стандартное поведение Qt просто отклонит drop (acceptDrops не
    # установлен).

    def _on_scenario_drop_in_form(self, path):
        """Слот ScenarioDropZone.file_dropped — drop файла прямо в форме
        «Новый эпизод». Читает файл, парсит как полный документ
        (bible + episodes), сохраняет в shows/<slug>/. Затем кладёт
        полный текст в paste_edit чтобы юзер видел что загрузил
        (и мог редактировать перед запуском)."""
        from pathlib import Path as _Path
        try:
            import scenario_parser as _sp
        except Exception:
            return
        cur_show = getattr(self._mw, '_current_show', None)
        if not cur_show:
            return
        try:
            text = _sp.read_scenario_file(_Path(path))
        except Exception as ex:
            self._set_status_error(str(ex))
            return
        # Парсим и сохраняем bible + epNN.txt
        try:
            parsed = _sp.parse_episodes_doc(text)
            if parsed.bible or parsed.episodes:
                _sp.save_parsed_doc(self._mw._project_root, cur_show, parsed)
        except Exception:
            traceback.print_exc()
        # Кладём текст в paste_edit чтобы юзер видел что загружено
        # и мог продолжить ввод номера серии. _on_paste_changed
        # обновит pending_text и run_btn.
        self.paste_edit.blockSignals(True)
        self.paste_edit.setPlainText(text)
        self.paste_edit.blockSignals(False)
        self._pending_text = text
        self._pending_source = 'file'
        self._set_status('new_ep_loaded_file', color="#6db86d",
                         name=_Path(path).name, chars=len(text))
        self._update_run_btn_state()

    def _on_paste_changed(self):
        text = self.paste_edit.toPlainText()
        if text.strip():
            self._pending_text = text
            self._pending_source = 'paste'
            self._set_status('new_ep_loaded_paste', color="#6db86d", chars=len(text))
        else:
            # Если очистили пасту — pending не сбрасываем (юзер мог дропнуть файл)
            if self._pending_source == 'paste':
                self._pending_text = ""
                self._pending_source = ""
                self._set_status(None)
        self._update_run_btn_state()

    def _update_run_btn_state(self):
        """Активирует «Запустить» когда указан номер серии в Зоне 4.

        2026-05-05: убрана проверка на наличие текста. Раньше требовалось
        чтобы paste/file тоже были непустыми — но теперь сценарии лежат
        в `scenarios/epNN.txt` (сохраняются drop-зоной), и `_on_run`
        сначала пытается взять текст оттуда. Если файла нет и paste пуст —
        выскакивает попап Уровня 2 «Серии нет в базе». Поэтому кнопка
        должна работать сразу как юзер ввёл номер.

        2026-05-07: убрана глобальная блокировка по `self._thread.isRunning()`.
        Теперь блокируется ТОЛЬКО если для введённого ep'а уже бежит свой
        тред (parallel-runs allowed: ep4 + ep5 одновременно). Защита от
        дубль-кликов на тот же ep остаётся.
        """
        ep_text = self.ep_edit.text().strip()
        if not ep_text:
            self.run_btn.setEnabled(False)
            return
        # Если для конкретного ep'а уже бежит тред — блокируем.
        # Парсинг номера здесь упрощённый: просто берём цифры.
        try:
            num_str = ''.join(ch for ch in ep_text if ch.isdigit())
            if num_str:
                ep_id_candidate = f"ep{int(num_str)}"
                t = self._threads.get(ep_id_candidate)
                if t is not None and t.isRunning():
                    self.run_btn.setEnabled(False)
                    return
        except Exception:
            pass
        self.run_btn.setEnabled(True)

    # ── Запуск ──────────────────────────────────────────────────────────
    def _on_run(self):
        # Базовая валидация
        cur_show = getattr(self._mw, '_current_show', None)
        if not cur_show:
            self._set_status_error(tr('new_ep_no_show'))
            return
        ep_query = self.ep_edit.text().strip()
        if not ep_query:
            self._set_status_error(tr('new_ep_no_episode'))
            return
        ep_num = _sa.parse_episode_number(ep_query)

        text = self._pending_text or self.paste_edit.toPlainText()

        if _sa.find_claude_cli() is None:
            self._set_status_error(tr('new_ep_cli_missing'))
            return

        # 2026-05-06: pre-flight проверка AI-авторизации. Если CLI разлогинен,
        # сразу показываем плашку и НЕ запускаем генерацию (иначе юзер ждёт
        # 30+ секунд только чтобы увидеть ошибку в стриме).
        try:
            auth = _sa.claude_auth_status(timeout=8.0)
            if not auth.get('loggedIn'):
                # Просим MainWindow показать плашку logged_out
                if hasattr(self._mw, '_show_auth_banner'):
                    self._mw._show_auth_banner('logged_out')
                self._set_status_error(tr('auth_banner_logged_out'))
                return
        except Exception:
            # Не падаем если auth status недоступен — пропускаем pre-flight
            pass

        show_root = self._mw._project_root / "shows" / cur_show
        scenarios_dir = show_root / "scenarios"
        scenarios_dir.mkdir(parents=True, exist_ok=True)

        # ──────────────────────────────────────────────────────────────────
        # ПАРСИНГ ПАСТЫ: если юзер вставил полный документ с библией и/или
        # сериями — он работает идентично верхней drop-зоне. Извлекаем
        # bible.txt и epNN.txt сразу, до всех валидаций. Это значит что
        # «вставить текст в Зону 3» и «перетащить файл в Зону 1» дают
        # одинаковый результат.
        # ──────────────────────────────────────────────────────────────────
        try:
            import scenario_parser as _sp
        except Exception:
            _sp = None
        text_parsed_episode_nums: set = set()
        if _sp and text and text.strip():
            try:
                parsed = _sp.parse_episodes_doc(text)
                if parsed.bible or parsed.episodes:
                    _sp.save_parsed_doc(self._mw._project_root, cur_show, parsed)
                text_parsed_episode_nums = {ep.ep_num for ep in parsed.episodes}
            except Exception:
                # Парсер не должен ломать запуск. Если что-то странное в тексте
                # — продолжаем как раньше (старая логика _active.txt сохранит).
                import traceback
                traceback.print_exc()

        # ──────────────────────────────────────────────────────────────────
        # УРОВЕНЬ 0: библия обязательна для нового сериала.
        # ──────────────────────────────────────────────────────────────────
        bible_path = show_root / "bible.txt"
        if not bible_path.exists() or not bible_path.read_text(encoding="utf-8").strip():
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self,
                tr('run_no_bible_title'),
                tr('run_no_bible_msg'),
            )
            return

        # ──────────────────────────────────────────────────────────────────
        # УРОВЕНЬ 2: проверяем что серия доступна — либо в базе scenarios/,
        # либо в тексте который юзер только что вставил.
        # ──────────────────────────────────────────────────────────────────
        if ep_num is not None:
            existing_eps = set()
            for f in scenarios_dir.glob("ep*.txt"):
                m = re.match(r'ep(\d+)', f.stem)
                if m:
                    try:
                        existing_eps.add(int(m.group(1)))
                    except Exception:
                        pass
            in_base = ep_num in existing_eps
            in_text = bool(text and text.strip())
            if not in_base and not in_text:
                from PyQt6.QtWidgets import QMessageBox
                if existing_eps:
                    nums = ', '.join(str(n) for n in sorted(existing_eps))
                    msg = tr('run_episode_not_found_msg_have',
                             show=cur_show, available=nums, ep_n=ep_num)
                else:
                    msg = tr('run_episode_not_found_msg_empty', show=cur_show)
                QMessageBox.warning(
                    self, tr('run_episode_not_found_title'), msg)
                return

        # ──────────────────────────────────────────────────────────────────
        # УРОВЕНЬ 3: в тексте есть «ЭПИЗОД X», но в поле указан Y. Спросим.
        # ──────────────────────────────────────────────────────────────────
        if ep_num is not None and text_parsed_episode_nums:
            mismatched = [n for n in text_parsed_episode_nums if n != ep_num]
            if mismatched and ep_num not in text_parsed_episode_nums:
                from PyQt6.QtWidgets import QMessageBox, QPushButton
                # Берём первый «другой» номер из текста для подсказки
                text_n = sorted(text_parsed_episode_nums)[0]
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Question)
                box.setWindowTitle(tr('run_mismatch_title'))
                box.setText(tr('run_mismatch_msg',
                               text_n=text_n, field_n=ep_num))
                use_text_btn = box.addButton(
                    tr('run_mismatch_use_text', text_n=text_n),
                    QMessageBox.ButtonRole.AcceptRole)
                use_field_btn = box.addButton(
                    tr('run_mismatch_use_field', field_n=ep_num),
                    QMessageBox.ButtonRole.AcceptRole)
                cancel_btn = box.addButton(
                    tr('run_mismatch_cancel'),
                    QMessageBox.ButtonRole.RejectRole)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel_btn:
                    return
                if clicked is use_text_btn:
                    # Переключаем номер на тот что в тексте
                    ep_num = text_n
                    self.ep_edit.setText(str(ep_num))
                    ep_query = str(ep_num)
                # Если use_field_btn — оставляем ep_num как есть

        # ──────────────────────────────────────────────────────────────────
        # УРОВЕНЬ 4: над этой серией уже работали (есть блоки в episodes.json).
        # ──────────────────────────────────────────────────────────────────
        if ep_num is not None:
            try:
                meta_existing = _sa.read_episodes_meta(show_root) or {}
                ep_obj = meta_existing.get(f"ep{ep_num}") or {}
                blocks = ep_obj.get("blocks") if isinstance(ep_obj, dict) else None
                if isinstance(blocks, dict) and len(blocks) > 0:
                    from PyQt6.QtWidgets import QMessageBox
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Icon.Question)
                    box.setWindowTitle(tr('run_already_worked_title'))
                    box.setText(tr('run_already_worked_msg',
                                   ep_n=ep_num, n_blocks=len(blocks)))
                    cont_btn = box.addButton(
                        tr('run_already_worked_continue'),
                        QMessageBox.ButtonRole.AcceptRole)
                    restart_btn = box.addButton(
                        tr('run_already_worked_restart'),
                        QMessageBox.ButtonRole.AcceptRole)
                    cancel_btn = box.addButton(
                        tr('run_already_worked_cancel'),
                        QMessageBox.ButtonRole.RejectRole)
                    box.exec()
                    clicked = box.clickedButton()
                    if clicked is cancel_btn:
                        return
                    if clicked is cont_btn:
                        # Продолжить редактирование — переключаемся в чат
                        # эпизода без перезапуска. Существующий handoff
                        # делает это в _hand_off_to_episode_chat когда
                        # ep_id известен.
                        self._current_ep_id = f"ep{ep_num}"
                        self._hand_off_to_episode_chat()
                        return
                    # restart_btn — стираем blocks и продолжаем нормальный путь
                    if isinstance(ep_obj, dict):
                        ep_obj['blocks'] = {}
                        meta_existing[f"ep{ep_num}"] = ep_obj
                        try:
                            (show_root / "episodes.json").write_text(
                                json.dumps(meta_existing, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
                        except Exception:
                            pass
            except Exception:
                import traceback
                traceback.print_exc()

        # Если paste была пустая — берём текст серии из базы scenarios/epNN.txt
        if (not text or not text.strip()) and ep_num is not None:
            ep_file = scenarios_dir / (
                f"ep{ep_num:02d}.txt" if ep_num < 100 else f"ep{ep_num}.txt"
            )
            if ep_file.exists():
                try:
                    text = ep_file.read_text(encoding="utf-8")
                except Exception as ex:
                    self._set_status_error(f"read scenario: {ex}")
                    return

        # Защитная сетка — текст должен быть к этому моменту
        if not text or not text.strip():
            self._set_status_error(tr('new_ep_no_input'))
            return

        # Сохраняем входной текст в shows/<slug>/scenarios/_inbox.txt
        show_root = self._mw._project_root / "shows" / cur_show
        scenarios_dir = show_root / "scenarios"
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = scenarios_dir / "_inbox.txt"
        try:
            inbox_path.write_text(text, encoding="utf-8")
        except Exception as ex:
            self._set_status_error(f"write inbox: {ex}")
            return

        # Пробуем вырезать секцию указанной серии.
        # 2026-05-05: ep_num уже распарсен выше. Сначала пытаемся взять
        # точный текст из scenarios/epNN.txt (его сохранил
        # scenario_parser при загрузке документа). Этот путь надёжнее
        # старого find_episode_section, который может ошибиться на
        # документах где библия сверху содержит нумерованный список
        # персонажей «1. David…» — старый parser принимал это за
        # «ЭПИЗОД 1».
        active_text = text
        section_status = None
        if ep_num is not None:
            ep_file = scenarios_dir / (
                f"ep{ep_num:02d}.txt" if ep_num < 100 else f"ep{ep_num}.txt"
            )
            if ep_file.exists():
                try:
                    file_section = ep_file.read_text(encoding="utf-8")
                    if file_section.strip():
                        active_text = file_section
                        section_status = tr(
                            'new_ep_section_found', chars=len(file_section))
                except Exception:
                    traceback.print_exc()
            # Если по какой-то причине файла нет — fallback на старую
            # логику find_episode_section (юзер мог вставить только текст
            # одной серии без маркера ЭПИЗОД, но scenario_parser его не
            # сохранит как epNN.txt — тогда нужен fallback).
            if active_text is text:
                section = _sa.find_episode_section(text, ep_num)
                if section and len(section) < len(text):
                    active_text = section
                    section_status = tr('new_ep_section_found', chars=len(section))
            # Phase 2 hotfix #27: защита от случая когда юзер ввёл
            # неверный номер. Если в сценарии есть заголовок «ЭПИЗОД X:»
            # и X != ep_num — показываем предупреждение в статус-баре,
            # чтобы файлы не записались в неверный chats/ep{Y}.jsonl.
            try:
                import re as _re
                # Все упоминания «ЭПИЗОД N» / «СЕРИЯ N» / «EPISODE N» в тексте
                found_nums = set()
                for m in _re.finditer(
                        r'(?im)(?:эпизод|серия|серія|епізод|episode|chapter|глава)\s*[№#]?\s*(\d+)\b',
                        text):
                    try:
                        found_nums.add(int(m.group(1)))
                    except Exception:
                        pass
                if found_nums and ep_num not in found_nums:
                    # Юзер ввёл число которого нет в сценарии — warn.
                    nums_str = ', '.join(str(n) for n in sorted(found_nums))
                    warn_msg = (
                        f"❗ В сценарии эпизоды [{nums_str}], а ты вводишь "
                        f"{ep_num} — файлы пойдут под ep{ep_num}. "
                        f"Останови (Cmd+Q) если ошибся.")
                    try:
                        self._mw.status_bar.showMessage(warn_msg, 15000)
                    except Exception:
                        pass
                    self._append_log(f"\n⚠ {warn_msg}\n", kind='warn')
            except Exception:
                pass
        # 2026-05-10: single source of truth — scenarios/ep{NN:02d}.txt.
        # _active.txt больше не пишется (legacy, разъезжался с UI-эпизодом
        # → баг «агент читает не тот сценарий»). _inbox.txt пишется выше
        # как черновик ввода формы «+» — это отдельная сущность, не источник
        # сценария для агента.
        if ep_num is not None:
            ep_target = scenarios_dir / (
                f"ep{ep_num:02d}.txt" if ep_num < 100 else f"ep{ep_num}.txt")
            try:
                ep_target.write_text(active_text, encoding="utf-8")
            except Exception as ex:
                self._set_status_error(f"write ep{ep_num}.txt: {ex}")
                return
        if section_status is None:
            section_status = tr('new_ep_section_full', chars=len(active_text))

        # Создаём запись для эпизода в episodes.json чтобы он сразу появился в
        # дропдауне «Эпизод» (даже до того как Claude нагенерил блоки). Если
        # запись уже есть — не трогаем.
        if ep_num is not None:
            ep_id = f"ep{ep_num}"
            self._current_ep_id = ep_id
            # Sub-MVP «кнопка автономной генерации»: пред-устанавливаем
            # `_ep_id` в EpisodeChatView чтобы он сразу принимал маркеры
            # из chunks (через on_external_append). Иначе chat view ждёт
            # клика по плашке «→ Открыть чат» и пропускает все chunks
            # пока юзер на NewEpisodeView — кнопки не появляются.
            try:
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev._ep_id = ep_id
            except Exception:
                pass
            ep_meta_path = show_root / "episodes.json"
            try:
                meta_existing = _sa.read_episodes_meta(show_root)
                # Phase 2 hotfix #9: вытаскиваем title из сценария
                # («ЭПИЗОД 21: ТРОЙНОЕ ДНО» → «Тройное дно»).
                parsed_title = _sa.extract_episode_title(active_text, ep_num)
                if not parsed_title:
                    parsed_title = _sa.extract_episode_title(text, ep_num)
                final_title = parsed_title or ep_query
                if ep_id not in meta_existing:
                    meta_existing[ep_id] = {
                        "title": final_title,
                        "blocks": {},
                    }
                else:
                    # Phase 2 hotfix #12: эпизод уже был запущен раньше.
                    # Юзер кликнул «🤖 Запустить» снова → начинаем заново:
                    # обнуляем refs (manifest от старого AI ответа) и
                    # refs_decisions (старые юзер-решения). Пока AI не
                    # пришлёт свежий manifest, РЕФЕРЕНСЫ будут пустыми
                    # — иначе юзер видит «laura/david» из прошлой сессии
                    # ещё до ответа AI. title из сценария обновляем
                    # тоже (мог поменяться между запусками).
                    ep_obj = meta_existing[ep_id]
                    if isinstance(ep_obj, dict):
                        if final_title:
                            ep_obj['title'] = final_title
                        ep_obj.pop('refs', None)
                        ep_obj.pop('refs_decisions', None)
                ep_meta_path.write_text(
                    json.dumps(meta_existing, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                # Триггерим обновление UI: дропдаун эпизодов + перерисовка
                # РЕФЕРЕНСОВ (теперь пустых для рестарта).
                if hasattr(self._mw, '_meta'):
                    self._mw._meta = meta_existing
                if hasattr(self._mw, '_populate_episodes'):
                    try:
                        self._mw._populate_episodes()
                    except Exception:
                        pass
                if (getattr(self._mw, '_current_episode', None) == ep_id
                        and hasattr(self._mw, '_build_refs_view')):
                    try:
                        self._mw._build_refs_view(ep_id)
                    except Exception:
                        pass
            except Exception:
                pass  # не критично — AI сам потом перепишет episodes.json

        # 2026-05-08: язык ответа аналитика = язык интерфейса (флаг
        # 🇷🇺/🇺🇦/🇬🇧 в шапке). Раньше директива «Respond in user's
        # language» была размытой — модель видела большой русский
        # системный промпт + русский сценарий и фолбэчилась в RU,
        # игнорируя что юзер выбрал украинский UI. Теперь жёстко
        # указываем конкретный язык по `get_lang()` — никаких эвристик.
        chat_lang = get_lang()
        lang_name_en = {
            'ru': 'Russian',
            'uk': 'Ukrainian',
            'en': 'English',
        }.get(chat_lang, 'Russian')

        # 2026-05-10: single source of truth — scenarios/ep{NN:02d}.txt.
        # Раньше промпт hardcode говорил «Read _active.txt», но _active.txt
        # разъезжался с UI-эпизодом (агент читал чужую серию). Теперь путь
        # вычисляется по ep_num конкретного запуска.
        ep_file = (
            f"ep{ep_num:02d}.txt" if ep_num is not None and ep_num < 100
            else f"ep{ep_num}.txt" if ep_num is not None
            else None)
        scenario_rel = (
            f"scenarios/{ep_file}" if ep_file else "scenarios/")
        # Формируем промпт для Claude
        prompt = (
            f"User dropped a scenario document into Storyboard Studio. "
            f"They want to work on episode «{ep_query}». "
            f"Read shows/{cur_show}/{scenario_rel} and follow CLAUDE.md "
            f"(mark ALL references — locations, objects, characters — as ✗ "
            f"with a [[GEN:type:name:description]] marker on each line; user "
            f"decides per-card whether to reuse existing file or generate new).\n\n"
            "🔴 КРИТИЧНОЕ ПРАВИЛО ОТВЕТА (читай ПЕРВЫМ):\n"
            "Твой первый ответ — это ВСЕГДА полный список вида:\n"
            "  ЛОКАЦИИ:\n"
            "  - ✗ <name1> (короткое описание места — где это, что происходит) [[GEN:location:<name1>:<описание>]]\n"
            "  ОБЪЕКТЫ (правило ≥2 шотов):\n"
            "  - ✗ <obj1> (что это за предмет, для чего нужен; сцены X, Y) [[GEN:object:<obj1>:<описание>]]\n"
            "  ПЕРСОНАЖИ:\n"
            "  - ✗ <char1> (Имя — короткая роль в эпизоде; outfit notes если есть) [[GEN:character:<char1>:<Имя — роль; outfit notes>]]\n"
            "\n"
            "🔴 ВСЕ референсы помечаются как ✗. НИКАКИХ ✓ в новом списке.\n"
            "  Даже если ты «знаешь» что файл с таким именем уже лежит в\n"
            "  refs/<sub>/ — всё равно ставь ✗. Юзер сам решит на каждой\n"
            "  карточке: 🎨 сгенерировать новый, 📁 выбрать существующий\n"
            "  из библиотеки, или 🚫 пропустить. refs/<sub>/ — это БИБЛИОТЕКА\n"
            "  существующих рефов сериала, не indicator «нужен или не нужен».\n"
            "\n"
            "🔴 БЕЗ хвоста `— рефа нет` / `— нужен реф` / `— буду генерировать`.\n"
            "  После закрывающей `)` сразу идёт маркер `[[GEN:...]]`.\n"
            "  ПРАВИЛЬНО:\n"
            "    `- ✗ kitchen_main (кухня в современной квартире, утро) [[GEN:location:kitchen_main:...]]`\n"
            "  НЕПРАВИЛЬНО:\n"
            "    `- ✗ kitchen_main (кухня) — рефа нет, буду генерировать [[GEN:location:kitchen_main:...]]`\n"
            "\n"
            "🔴 ОПИСАНИЕ В СКОБКАХ (ОБЯЗАТЕЛЬНО для каждой ✗-строки):\n"
            "  • Сразу после slug в КРУГЛЫХ скобках — короткое описание (5-15 слов)\n"
            f"    in {lang_name_en} (the chat-output language fixed below).\n"
            "  • Это нужно для двух целей: (1) юзер сразу понимает что это\n"
            "    за реф БЕЗ открытия папки; (2) Studio использует содержимое\n"
            "    скобок как fallback-канал в pipeline если ты забыл вставить\n"
            "    `[[GEN:...]]` маркер. Маркер — основной канал, скобки —\n"
            "    запасной. Всегда вставляй маркер, но если забыл — описание\n"
            "    в скобках тебя страхует.\n"
            "  • Для ОБЪЕКТОВ — это короткое описание предмета (БЕЗ\n"
            "    дефиса `—` внутри скобок, чтобы парсер не путался;\n"
            "    разделитель деталей — `;` или `, `).\n"
            "  • 🔴 Для ЛОКАЦИЙ — ОБЯЗАТЕЛЬНО прочитай `bible.txt` ПЕРЕД\n"
            "    написанием описания. Social/genre context — НЕ ОПЦИЯ,\n"
            "    а часть описания КАЖДОЙ локации (luxury / средний класс /\n"
            "    бедный, городской / загородный, эпоха / современность).\n"
            "    Без него генератор картинки (Gemini) выдаёт нейтральный\n"
            "    дефолт — например для «коридора частного дома» может\n"
            "    нарисовать обшарпанный советский подъезд / общагу.\n"
            "      • Хорошо: `luxury_corridor (светлый коридор богатого\n"
            "        загородного дома, дорогой паркет, белые стены,\n"
            "        элегантный декор)` → Gemini сделает luxury interior.\n"
            "      • Плохо: `luxury_corridor (светлый коридор частного\n"
            "        загородного дома)` — слишком общо, Gemini додумает\n"
            "        что хочет.\n"
            "    Если bible НЕ говорит про social-класс прямо — выводи\n"
            "    из жанра:\n"
            "      • триллер про адюльтер богатых → luxury / upscale\n"
            "      • полицейский procedural → realistic / standard\n"
            "      • криминал на районе → grungy / working-class\n"
            "      • исторический → epoch-specific (Victorian / 1920s /\n"
            "        1970s). НЕ применяй национально-окрашенные стили\n"
            "        («советский», «постсоветский», «российская провинция»)\n"
            "        как дефолт — только если bible явно про эту страну\n"
            "        и эпоху.\n"
            "    БЕЗ дефиса `—` внутри скобок.\n"
            "  • Для ПЕРСОНАЖЕЙ — формат `(Имя — короткая роль; outfit notes если есть)`.\n"
            "    Имя и роль разделяются длинным тире `—`. Имя идёт первым, роль — после.\n"
            "    🔴 РОЛЬ — ТОЛЬКО ИЗ ТЕКСТА СЦЕНАРИЯ. Не приписывай статусы\n"
            "    («главный герой», «протагонист», «антагонист») если в сценарии\n"
            "    этого нет. Пиши ФАКТИЧЕСКОЕ занятие/функцию персонажа в\n"
            "    ЭТОМ эпизоде:\n"
            "      • Хорошо: `protagonist (Имя — адвокат, к которому пришёл клиент)`\n"
            "      • Хорошо: `client (Имя — клиент(ка), обратилась за защитой)`\n"
            "      • Хорошо: `detective (Имя — следователь, ведёт дело)`\n"
            "      • Плохо: `protagonist (Имя — главный герой)` — мы не знаем, кто главный\n"
            "      • Плохо: `client (Имя — протагонист(ка))` — пустой ярлык\n"
            "    Если из сценария НЕ ясна роль — оставь только Имя без `—`:\n"
            "      • `protagonist (Имя)` — допустимо, лучше пусто чем выдумано.\n"
            "  • 🔴 OUTFIT NOTES ПО СЦЕНАМ — ОБЯЗАТЕЛЬНО для CHARACTER\n"
            "    если в сценарии есть подсказки по одежде. Эти notes\n"
            "    идут ВНУТРИ скобок после роли — Studio передаёт их в\n"
            "    outfit picker который предлагает варианты одежды для\n"
            "    первой по хронологии сцены этого персонажа в эпизоде.\n"
            "    Без них picker даёт generic city wear.\n"
            "      • Хорошо: `suspect (Имя — подозреваемый. Сцена 7 —\n"
            "        обнажённый торс / простыня по пояс в кровати)`.\n"
            "        Picker предложит контекстные варианты\n"
            "        (бельё, голый торс + спортивные шорты, и т.д.).\n"
            "      • Плохо: `suspect (Имя — подозреваемый, ведёт себя\n"
            "        нагло)` — нет outfit-контекста → picker предложит\n"
            "        generic city wear (футболка/джинсы/кеды) даже когда\n"
            "        в кровати должен быть голый торс.\n"
            "    Если сценарий ЯВНО описывает одежду («в белом халате»,\n"
            "    «в форме», «обнажённый торс», «в кожаной куртке») —\n"
            "    ОБЯЗАТЕЛЬНО скопируй эти детали в описание ПЕРВОЙ сцены\n"
            "    с этим персонажем. Если у персонажа несколько разных\n"
            "    аутфитов в разных сценах — не пытайся уместить всё в одну\n"
            "    карточку; outfit picker даст 3 варианта для ПЕРВОЙ сцены,\n"
            "    а юзер вручную добавит другие аутфиты после первой\n"
            "    генерации через вкладку «Актёры».\n"
            "  • Если уже знаешь slug, но описания дать нечего (например в\n"
            "    сценарии один раз упомянут предмет) — придумай минимальное:\n"
            "    `(чёрный кейс с документами)`.\n"
            "\n"
            "ЗАПРЕЩЕНО заменять этот список ЛЮБЫМИ другими формулировками\n"
            "(«Manifest записан», «Вкладка РЕФЕРЕНСЫ покажет», «всё готово»,\n"
            "«видно в episodes.json», «проверь рефы»). Юзер ВИДИТ ТОЛЬКО ЭТОТ\n"
            "ТЕКСТ — у него нет другого способа узнать что ты определил.\n"
            "Кнопки «🎨 Сгенерировать» в Studio появляются на маркерах\n"
            "[[GEN:...]] в этом сообщении. Без них генерация невозможна.\n"
            "ДАЖЕ ЕСЛИ эпизод уже есть в episodes.json — ВСЁ РАВНО выводи\n"
            "список заново, СТРОКА В СТРОКУ. Полный формат + все правила —\n"
            "в CLAUDE.md, раздел «ШАГ 1».\n\n"
            "IMPORTANT UX RULES — you are running inside Storyboard Studio "
            "(headless `claude -p`), NOT inside an interactive Claude Code chat:\n"
            "- The Studio chat panel shows ONLY plain text — images do NOT render. "
            "NEVER say «picture above», «see image», «картинки выше в чате» — "
            "instead say: «✓ <name> готова — открой вкладку РЕФЕРЕНСЫ и проверь».\n"
            "- REFS MANIFEST (критически важно!): когда ты определил какие "
            "локации/объекты/персонажи нужны этому эпизоду (на ШАГЕ 1, ДО "
            "монтажной карты) — ОБЯЗАТЕЛЬНО запиши их в "
            f"`shows/<slug>/episodes.json` под ключ эпизода в формате:\n"
            "  ```json\n"
            "  \"epXX\": {\n"
            "    \"title\": \"...\",\n"
            "    \"refs\": {\n"
            "      \"locations\":  [\"prison_phone_hallway.jpg\", \"...\"],\n"
            "      \"objects\":    [\"briefcase.jpg\"],\n"
            "      \"characters\": []\n"
            "    },\n"
            "    \"blocks\": {}\n"
            "  }\n"
            "  ```\n"
            "  • locations/objects — ИМЕНА ФАЙЛОВ с расширением (`.jpg`, как лежат в refs/)\n"
            "  • 🔴 НИКОГДА не трогай поле `refs_decisions` в episodes.json.\n"
            "    Это поле принадлежит Studio (auto-link после генерации +\n"
            "    явный выбор юзера через «📁 Выбрать существующий»). При\n"
            "    обновлении manifest:\n"
            "      • Используй Edit tool вместо Write — он точечный и НЕ\n"
            "        трогает другие ключи в JSON.\n"
            "      • Если ВСЁ-ТАКИ нужен Write tool (полный overwrite) —\n"
            "        СНАЧАЛА Read episodes.json, сохрани `refs_decisions`\n"
            "        как есть в твоей копии JSON, и пиши обратно ВМЕСТЕ\n"
            "        с этим полем.\n"
            "    Без этой защиты Studio'шные auto-link decisions (с\n"
            "    правильными расширениями файлов на диске) затрутся твоим\n"
            "    overwrite'ом, и юзер увидит пустые РЕФЕРЕНСЫ эпизода.\n"
            "  • 🔴 characters — ВСЕГДА оставляй пустым массивом `[]`. Studio\n"
            "    игнорирует то, что ты сюда пишешь — персонажи подгружаются\n"
            "    ТОЛЬКО когда юзер явно выбирает файл через кнопку\n"
            "    «📁 Выбрать существующий» в чате. Если запишешь сюда\n"
            "    `[\"laura\", \"david\"]` — РЕФЕРЕНСЫ ВСЁ РАВНО будут пустыми\n"
            "    по персонажам, и юзер увидит «магическое» расхождение\n"
            "    между manifest и UI. Просто оставь `[]`.\n"
            "  • В ✗-строке секции ПЕРСОНАЖИ маркер `[[GEN:character:slug:description]]`\n"
            "    СТАВИТСЯ обязательно (как и для location/object). Studio\n"
            "    при клике 🎨 откроет CharacterOutfitPicker (3 варианта одежды\n"
            "    по контексту ПЕРВОЙ по хронологии сцены этого персонажа\n"
            "    в эпизоде). При клике 📁 — RefPickerDialog с галереей из\n"
            "    `refs/characters/<slug>/` для переиспользования.\n"
            "  • 🔴 ИМЯ ПЕРСОНАЖА в ✗-строке — СТРОГО ASCII slug в нижнем\n"
            "    snake_case (имя папки в refs/characters/). Если в сценарии\n"
            "    персонаж называется по-русски/по-украински/другому языку —\n"
            "    ТРАНСЛИТЕРИРУЙ САМ (НЕ переводи смыслово!) и пиши slug перед\n"
            "    оригиналом в скобках. Имя в скобках сохраняется как в\n"
            "    сценарии — на исходном языке. Примеры:\n"
            "      • «Муж» (рус) → `- ✗ muzh (Муж — короткая роль; outfit notes) [[GEN:character:muzh:Муж — короткая роль; outfit notes]]`\n"
            "      • «Жена» (рус) → `- ✗ zhena (Жена — короткая роль; outfit notes) [[GEN:character:zhena:Жена — короткая роль; outfit notes]]`\n"
            "      • «Чоловік» (укр) → `- ✗ cholovik (Чоловік — короткая роль; outfit notes) [[GEN:character:cholovik:Чоловік — короткая роль; outfit notes]]`\n"
            "      • «Дружина» (укр) → `- ✗ druzhyna (Дружина — короткая роль; outfit notes) [[GEN:character:druzhyna:Дружина — короткая роль; outfit notes]]`\n"
            "      • «Husband» (англ) → `- ✗ husband (Husband — short role; outfit notes) [[GEN:character:husband:Husband — short role; outfit notes]]`\n"
            "    🔴 НЕ переводи смыслово: «Муж» → НЕ `husband`, а `muzh`.\n"
            "    Транслитерация универсальна для любого языка и не путает\n"
            "    модель когда персонажей много (два «брата» = два разных\n"
            "    slug'а по транслиту имён, а не одинаковые `brother`).\n"
            "    Studio парсит slug для папки `refs/characters/<slug>/`,\n"
            "    а оригинал в скобках показывает юзеру в карточке.\n"
            "    БЕЗ slug — Studio не сможет найти папку рефа.\n"
            "  • 🔴 ЖИВОТНЫЕ — НЕ ПЕРСОНАЖИ. Собака/кот/лошадь/птица/\n"
            "    любое не-человеческое существо идёт в секцию ОБЪЕКТЫ\n"
            "    (правило ≥2 шотов), маркер `[[GEN:object:slug:описание]]`.\n"
            "    Причина: ПЕРСОНАЖИ → outfit picker (выбор одежды), для\n"
            "    животных это абсурд. Object-flow генерирует фотореалистичный\n"
            "    портрет существа без одежды-логики.\n"
            "      • Хорошо: `- ✗ guard_dog (сторожевой пёс среднего размера, агрессивная поза) [[GEN:object:guard_dog:сторожевой пёс среднего размера, агрессивная поза]]` в ОБЪЕКТАХ\n"
            "      • Плохо: `- ✗ guard_dog (сторожевой пёс)` в ПЕРСОНАЖАХ\n"
            "  • 🔴🔴 КРИТИЧНО — ОДИН ПЕРСОНАЖ = ОДИН SLUG. ВСЕГДА.\n"
            "    Никогда не создавай два разных slug'а для одного и\n"
            "    того же героя только потому что он переодевается или\n"
            "    появляется в разных сценах. Герой в кровати, герой в\n"
            "    халате, герой в куртке — это ВСЁ ОДИН персонаж со\n"
            "    slug'ом `protagonist`. Не пиши `protagonist_white_robe`,\n"
            "    `protagonist_jacket`, `protagonist_dinner` — это ОШИБКА.\n"
            "    Разные одежды/состояния одного героя — это РАЗНЫЕ\n"
            "    ФАЙЛЫ внутри одной папки `refs/characters/<slug>/`\n"
            "    (например `<slug>_robe.jpg`, `<slug>_jacket.jpg`), но\n"
            "    slug всегда один. Studio сама подберёт нужный файл\n"
            "    одежды в каждой сцене.\n"
            "  • 🔴 КОГДА у героя несколько аутфитов в одном эпизоде:\n"
            "    outfit picker даёт 3 варианта для ПЕРВОЙ по хронологии\n"
            "    сцены этого персонажа. Это базовый кейс «1 эпизод = 1\n"
            "    аутфит». Если в эпизоде несколько разных аутфитов —\n"
            "    укажи в скобках ТОЛЬКО первую сцену (picker делает\n"
            "    реф под неё), а про остальные напиши юзеру в той же\n"
            "    строке после маркера — он добавит варианты вручную\n"
            "    через вкладку «Актёры» после первой генерации.\n"
            "  • Без manifest вкладка РЕФЕРЕНСЫ в Studio будет ПУСТАЯ для нового эпизода — "
            "юзер не увидит сгенерированные локации.\n"
            "  • Если эпизод переиспользует уже существующие рефы — всё равно\n"
            "    перечисли их в manifest текущего эпизода (Studio при клике\n"
            "    📁 Выбрать существующий запишет правильный filename в decisions).\n"
            "- ШАГ 1 — СТРОГИЙ ФОРМАТ ОТВЕТА (см. CLAUDE.md, раздел «ШАГ 1»):\n"
            "  • ОБЯЗАТЕЛЬНО полный список с `✗` по КАЖДОМУ пункту\n"
            "    (локации, объекты-в-≥2-шотах, персонажи) — отдельной строкой.\n"
            "  • ЗАПРЕЩЕНО заменять список фразами «Manifest записан»,\n"
            "    «Все готово» — юзеру нужен полный список ДО работы\n"
            "    (он жмёт кнопки 🎨/📁/🚫 рядом с пунктами).\n"
            "  • Каждая `✗` строка (ЛОКАЦИЯ/ОБЪЕКТ/ПЕРСОНАЖ) ОБЯЗАТЕЛЬНО\n"
            "    содержит встроенный маркер `[[GEN:type:name:description]]` —\n"
            "    точный формат, примеры и правила в CLAUDE.md ШАГ 1.\n"
            "    Studio парсит маркеры и показывает рядом 3 кнопки:\n"
            "    «🎨 Сгенерировать», «📁 Выбрать существующий», «🚫 Не нужен».\n"
            "    Юзер кликает одну → Studio запускает соответствующее\n"
            "    действие. Без маркера — карточка не появится.\n"
            "- PROGRESS NARRATION (важно!): между долгими шагами ОБЯЗАТЕЛЬНО "
            "выводи короткую строку-статус — одну фразу на действие, чтобы "
            "юзер не сидел в тишине пока ты работаешь. Печатай её ДО запуска "
            "тула и ПОСЛЕ его завершения. Примеры:\n"
            "  • «🌐 Ищу референс для prison_visitation в интернете…»\n"
            "  • «✏ Пишу промпт для генерации (детали: окно с решёткой, ряд кабин)…»\n"
            "  • «🎨 Запускаю генерацию картинки через Fast Gen AI (~30с)…»\n"
            "  • «📝 Описываю геометрию по сгенерированной картинке…»\n"
            "  • «✓ prison_visitation готова — открой РЕФЕРЕНСЫ и проверь».\n"
            "Каждая такая строка — отдельная короткая фраза с пустой строкой "
            "вокруг. НЕ объединяй прогресс в один большой блок в конце.\n"
            "- ОКОНЧАНИЕ ОТВЕТА (СТРОГО): последняя строка должна быть формата\n"
            "  «Жду команды и начинаю с <следующий шаг>.»\n"
            "  Где <следующий шаг> — конкретное действие которое ты сделаешь "
            "СРАЗУ после подтверждения юзера. Примеры:\n"
            "  • «Жду команды и начинаю с веб-поиска для локаций.»\n"
            "  • «Жду команды и начинаю с генерации картинок локаций.»\n"
            "  • «Жду команды и начинаю с написания монтажной карты.»\n"
            "  Это ВАЖНО: юзер видит план и понимает что произойдёт следующим шагом.\n"
            "  ЗАПРЕЩЕНО писать: «Напиши поехали», «type continue», «send go», "
            "«нажми кнопку», «жми Cmd+Enter», «отправь подтверждение» или любую "
            "техническую инструкцию о том КАК отвечать — юзер сам знает как.\n"
            "  Просто фраза «Жду команды и начинаю с …» — и всё.\n"
            "- ВНУТРИ ТЕКСТА (план, шаги, ссылки на будущие действия): "
            "ЗАПРЕЩЕНО упоминать конкретные слова-команды юзера в кавычках:\n"
            "  • НЕТ: «После твоего „продолжай“ — монтажная карта»\n"
            "  • НЕТ: «Пришли „поехали“ и я начну»\n"
            "  • НЕТ: «По команде „go“ генерирую…»\n"
            "  • ДА: «После твоей команды — монтажная карта и промпты блоков»\n"
            "  • ДА: «По твоему подтверждению начну веб-поиск»\n"
            "  • ДА: «Когда подтвердишь — генерирую…»\n"
            "  Юзер сам формулирует команду как ему удобно — не навязывай ему\n"
            "  конкретное слово. Это касается ВСЕГО ответа, не только последней строки.\n"
            "- Do NOT print exit codes, file paths in backticks if the user "
            "doesn't need them, or markdown rendering hints. Be concise.\n"
            f"- 🔴 LANGUAGE OF YOUR ANSWER (CRITICAL): Reply ONLY in "
            f"{lang_name_en}. Human-readable text — explanations, status lines "
            f"(«🌐 Ищу…»/«🔍 Шукаю…»/«🔍 Searching…»), the ✗ list,\n"
            f"  descriptions in parentheses, the final «Жду команды…» line — "
            f"MUST be in {lang_name_en}. The rules above are MY system\n"
            f"  instructions written in Russian for clarity; do NOT echo them "
            f"and do NOT match their language. The scenario file may be in any\n"
            f"  language — translate references on the fly when explaining to "
            f"the user. Reply in {lang_name_en} regardless of the scenario's\n"
            f"  original language. The user has selected {lang_name_en} as "
            f"their interface language and expects ALL human text in that language.\n"
            f"- 🔴 INTERFACE LANGUAGE ≠ CULTURAL CONTEXT: то что юзер пишет\n"
            f"  тебе на {lang_name_en} НЕ означает что сериал происходит в\n"
            f"  стране где говорят на этом языке. Сериал может быть про что\n"
            f"  угодно (американский триллер, европейская драма, корейский\n"
            f"  thriller). Cultural / geographical / economic context для\n"
            f"  физических объектов и локаций (в English-промптах для\n"
            f"  Gemini) бери ТОЛЬКО из bible.txt. Если bible не указывает\n"
            f"  страну/эпоху явно — используй generic Western contemporary\n"
            f"  aesthetic как нейтральный географический дефолт. Уровень\n"
            f"  достатка (luxury / middle-class / cheap / run-down)\n"
            f"  определяется ОТДЕЛЬНО — из описания конкретной локации в\n"
            f"  сценарии и из bible. Например: «дешёвый мотель Sunset» →\n"
            f"  cheap motel run-down; «элитный ресторан» → upscale fine\n"
            f"  dining; «квартира адвоката» → professional middle-class;\n"
            f"  «стройка» → working construction site. НЕ применяй luxury\n"
            f"  к ВСЕМ локациям подряд. НЕ применяй национальный колорит\n"
            f"  (русский / советский / dacha / постсоветский) к физическим\n"
            f"  объектам если bible этого не запросил явно.\n"
            f"- 🔴 SECTION HEADERS (must match parser): use the LOCALIZED "
            f"versions of the three section labels exactly as the user expects:\n"
            f"    • Russian:    `ЛОКАЦИИ:` / `ОБЪЕКТЫ (правило ≥2 шотов):` / `ПЕРСОНАЖИ:`\n"
            f"    • Ukrainian:  `ЛОКАЦІЇ:` / `ОБ'ЄКТИ (правило ≥2 шотів):` / `ПЕРСОНАЖІ:`\n"
            f"    • English:    `LOCATIONS:` / `OBJECTS (≥2 shots rule):` / `CHARACTERS:`\n"
            f"  Pick the line that matches your reply language ({lang_name_en}).\n"
            f"  Studio's parser uses these headers to find your ✗ items —\n"
            f"  if you invent a different header, NO buttons will appear.\n"
            f"- 🔴 TECHNICAL TOKENS (NEVER TRANSLATE, NEVER OMIT): the marker\n"
            f"  `[[GEN:type:name:description]]` is a machine-readable token,\n"
            f"  NOT prose. It is REQUIRED on every `✗` line (location, object,\n"
            f"  AND character) even when your reply is in Ukrainian or English.\n"
            f"  The token's\n"
            f"  format is fixed forever:\n"
            f"    • `type` — strictly one of: `location`, `object`, `character`\n"
            f"      (lowercase ASCII; do NOT translate to `локація`/`об'єкт`/etc.).\n"
            f"    • `name` — ASCII snake_case slug (the folder name in refs/).\n"
            f"    • `description` — short prompt-text; THIS part may be in\n"
            f"      {lang_name_en} so the autonomous gen agent picks it up,\n"
            f"      but it MUST stay inside the `[[GEN:…]]` brackets.\n"
            f"  Without this token Studio CANNOT show a «🎨 Сгенерировати»/\n"
            f"  «🎨 Generate» button next to the item. Same rule for every\n"
            f"  `✗` line — no exceptions, no abbreviations."
        )

        # Стартуем поток
        # Phase 2 hotfix #8: сбрасываем накопитель fallback-парсера.
        self._stream_full = ''
        # 2026-05-06: сбрасываем флаг что auth/quota-ошибка уже поймана —
        # на новом запуске надо детектить заново.
        self._auth_error_signaled = False
        self.log_view.clear()
        # Phase 2 hotfix #5: показываем log_view + status_lbl только в
        # момент старта генерации (раньше пустой log_view висел на «+»
        # ещё до клика «Запустить»).
        self.log_view.show()
        self.status_lbl.show()
        self._chunk_buffer = ''  # на всякий — сбрасываем хвост от прошлого запуска
        self._append_log_persist(f"▶ {section_status}\n", kind='system')
        # 2026-05-08: без `…` и одна `\n` — slow_thinking встанет ровно
        # следующей строкой; финализация снимет точки до `▶ Думаю`.
        self._append_log_persist(f"▶ {tr('new_ep_log_thinking')}\n", kind='system')
        self._set_status('new_ep_log_thinking', color="#ffaa44")
        self._thinking_step = 0
        self._thinking_timer.start()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        # Чат-инпут пока недоступен, появится после первого успешного ответа
        self.chat_row_widget.hide()
        # Phase 2: сразу скрываем compact creation-блок (drop+paste+поле
        # эпизода+model+run). Получается обычный чат с логом. Если будет
        # ошибка/стоп ДО первого успеха — блок вернётся (см.
        # _on_thread_error/_on_thread_stopped) чтобы юзер мог поправить
        # и снова запустить.
        self.creation_block.hide()

        self._thread = RunEpisodeThread(
            self._mw._project_root, prompt,
            continue_session=False, model=self._current_model())
        # 2026-05-08: помечаем тред его собственным ep_id ДО connect
        # сигналов. Без этого `_on_chunk_persist` использовал глобальный
        # `self._current_ep_id`, и при параллельных запусках чанки ep5
        # попадали в чат ep6 (тот ep чей запуск был последним).
        self._thread._ep_id = self._current_ep_id
        self._thread.output_chunk.connect(self._on_chunk_persist)
        self._thread.finished_ok.connect(self._on_thread_finished)
        self._thread.error.connect(self._on_thread_error)
        self._thread.stopped.connect(self._on_thread_stopped)
        self._thread.slow_thinking.connect(self._on_slow_thinking)
        self._thread.start()
        # 2026-05-07: регистрируем тред в per-episode реестре. Юзер сможет
        # сразу запустить ep5 не дожидаясь ep4. `self._thread` остаётся
        # ссылкой на ПОСЛЕДНИЙ запущенный (back-compat).
        try:
            if self._current_ep_id:
                self._threads[self._current_ep_id] = self._thread
        except Exception:
            pass

        # Phase 2.1: сразу переезжаем в Editor → ЭП → ЧАТ. Поток
        # продолжает работать; chunks идут в EpisodeChatView через
        # on_external_append (см. `_on_chunk_persist`). На «+» остаётся
        # чистая форма для следующего эпизода.
        self._hand_off_to_episode_chat()

    def _hand_off_to_episode_chat(self):
        """Phase 2.1: после клика «🤖 Запустить» автоматически переехать
        в Editor → ЭП → ЧАТ и очистить визуальные поля «+» (так чтобы
        юзер мог сразу запустить ещё один эпизод). Поток продолжает
        работать; его сигналы сейчас НЕ пишут в наш log_view (см.
        guard `self._handed_off` в `_append_log`), но пишут в jsonl
        эпизода и в EpisodeChatView через `on_external_append`."""
        if not self._current_ep_id:
            return
        self._handed_off = True
        try:
            self._mw._switch_to_episode_chat(
                self._current_ep_id, animated=True)
        except Exception:
            traceback.print_exc()
        # 2026-05-08: запускаем анимацию thinking-dots в чате на этом
        # же тред'е (своего тикера у EpisodeChatView ещё нет — тред
        # принадлежит NewEpisodeView). begin_external_thinking подключит
        # finished-сигнал и сам остановит анимацию по завершении.
        try:
            ev = getattr(self._mw, 'episode_chat_view', None)
            if ev is not None and self._thread is not None:
                # 2026-05-11 multi-ep fix: явный ep_id вместо неявного
                # `ev._ep_id` — тред регистрируется в per-ep_id реестре
                # `_external_threads`, что позволяет параллельным
                # генерациям не глушить друг друга при finished.
                if (getattr(ev, '_ep_id', None) == self._current_ep_id):
                    ev.begin_external_thinking(
                        self._thread, ep_id=self._current_ep_id)
        except Exception:
            traceback.print_exc()
        # Очищаем визуальные поля. НЕ трогаем `_current_ep_id` / `_thread`
        # / `_has_initial_run` — они нужны для роутинга chunks потока в
        # EpisodeChatView (см. `_on_chunk_persist`).
        try:
            self.log_view.clear()
            self._chunk_buffer = ''
            self.paste_edit.clear()
            self.ep_edit.clear()
            self._pending_text = ""
            self._pending_source = ""
            self.creation_block.show()
            self.chat_row_widget.hide()
            self.open_chat_row.hide()
            # Phase 2 hotfix #5: скрываем log_view и status_lbl —
            # на «+» они должны появляться только после клика «Запустить».
            self.log_view.hide()
            self.status_lbl.hide()
            self._set_status(None)
            self._update_run_btn_state()
        except Exception:
            traceback.print_exc()

    def _on_send_followup(self):
        """Юзер ответил Claude в чат-инпуте. Запускаем `claude --continue -p` —
        Claude подхватит свою же предыдущую беседу в этой cwd."""
        if self._thread is not None and self._thread.isRunning():
            return  # ещё думает
        text = self.chat_input.toPlainText().strip()
        if not text:
            return
        # Сообщение юзера → role="user" в jsonl
        user_line = f"\n💬 {tr('new_ep_you_label')}: {text}\n\n"
        self._append_log(user_line, kind='user')
        if self._current_ep_id:
            _sa.append_chat_message(self._current_ep_id, "user", user_line, kind='user')
            try:
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev.on_external_append(self._current_ep_id, user_line, 'user')
            except Exception:
                pass
        self._append_log_persist(f"▶ {tr('new_ep_log_thinking')}\n", kind='system')
        self.chat_input.clear()
        self._set_status('new_ep_log_thinking', color="#ffaa44")
        self._thinking_step = 0
        self._thinking_timer.start()
        self.send_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self._thread = RunEpisodeThread(
            self._mw._project_root, text,
            continue_session=True, model=self._current_model())
        self._thread.output_chunk.connect(self._on_chunk_persist)
        self._thread.finished_ok.connect(self._on_thread_finished)
        self._thread.error.connect(self._on_thread_error)
        self._thread.stopped.connect(self._on_thread_stopped)
        self._thread.slow_thinking.connect(self._on_slow_thinking)
        self._thread.start()

    def _on_stop(self):
        if self._thread is not None and self._thread.isRunning():
            self._thread.stop()

    def _on_thread_finished(self, rc: int):
        # 2026-05-08: ep_id берём из sender — параллельные треды для
        # разных эпизодов не должны путать состояние.
        sender = self.sender()
        sender_ep = getattr(sender, '_ep_id', None) if sender is not None else None
        target_ep = sender_ep or self._current_ep_id
        is_current_form_thread = (target_ep == self._current_ep_id)

        self._thinking_timer.stop()
        self._flush_chunk_buffer()
        # exit-код не показываем — техническая инфа, юзеру не нужна.
        # `_append_log_persist` ВНУТРИ скипает запись в log_view если
        # уже `_handed_off`, но всё равно пишет в jsonl и в
        # EpisodeChatView через on_external_append.
        # 2026-05-08: пишем «✓ Готово» в JSONL целевого эпизода.
        try:
            done_text = f"\n\n{tr('new_ep_log_done')}\n"
            if target_ep:
                _sa.append_chat_message(target_ep, "system", done_text, kind='ok')
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev.on_external_append(target_ep, done_text, 'ok')
            if is_current_form_thread and not self._handed_off:
                self._append_log(done_text, kind='ok')
                self._set_status('new_ep_log_done', color="#6db86d")
        except Exception:
            traceback.print_exc()
        # UI кнопки stop/run ставим только если завершился ТЕКУЩИЙ ep
        # формы (для остальных параллельных тредов кнопок нет — юзер
        # ушёл оттуда).
        if is_current_form_thread:
            self.stop_btn.setEnabled(False)
        if rc == 0:
            first_run = not self._has_initial_run
            self._has_initial_run = True
            # Phase 2.1: если уже переехали в EpisodeChatView — нет смысла
            # показывать chat_row_widget и плашку «→ Открыть чат эпизода»
            # на «+». Юзер уже видит чат и может писать followup'ы там.
            if is_current_form_thread and not self._handed_off:
                self.chat_row_widget.show()
                self.send_btn.setEnabled(True)
                self.chat_input.setFocus()
                if first_run and self._current_ep_id:
                    self._show_open_chat_btn(self._current_ep_id)
        # Phase 2 hotfix #8: если AI не вставил [[GEN:...]] маркеры,
        # синтезируем их из строк «- ✗ name —» под секциями. Делаем
        # для target_ep чтобы кнопки попали в правильный чат.
        if rc == 0 and target_ep and self._stream_full and is_current_form_thread:
            # `_stream_full` накапливался для текущей формы — синтез
            # делаем только если этот тред = текущий ep формы. Параллельные
            # треды накапливают свой stream в своих JSONL, а кнопки
            # синтезируются в EpisodeChatView при заходе в эпизод
            # (через `_restore_gen_buttons_from_history`).
            try:
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None and getattr(ev, '_gen_button', None) is None:
                    ev.try_synthesize_gen_markers(target_ep, self._stream_full)
            except Exception:
                traceback.print_exc()
        # Поток завершился — отвязываем NewEpisodeView от ушедшего
        # эпизода (только если это тред формы и она handed-off).
        if is_current_form_thread and self._handed_off:
            self._current_ep_id = None
            self._handed_off = False
        # Снимаем тред из реестра (по target_ep — корректно для любого).
        try:
            if target_ep and self._threads.get(target_ep) is sender:
                self._threads.pop(target_ep, None)
        except Exception:
            pass
        self._update_run_btn_state()

    def _show_open_chat_btn(self, ep_id: str):
        """Показывает плашку-кнопку «→ Открыть чат эпизода» под логом.
        Активируется после первого успешного ответа Claude. Скрывается
        в `_reset_for_new_episode`."""
        try:
            ep_label = ep_id.upper() if ep_id else ''
            self.open_chat_btn.setText(tr('open_episode_chat_btn', ep=ep_label))
            self.open_chat_row.show()
        except Exception:
            traceback.print_exc()

    def _on_open_chat_clicked(self):
        """Клик по плашке «→ Открыть чат эпизода». Плавный переход в
        Editor → ЭП → ЧАТ + чистка формы «Новый эпизод»."""
        ep_id = self._current_ep_id
        if not ep_id:
            return
        try:
            self._mw._switch_to_episode_chat(ep_id, animated=True)
            self._reset_for_new_episode()
        except Exception:
            traceback.print_exc()

    def _reset_for_new_episode(self):
        """Очищает форму «Новый эпизод» после того как чат уехал в Editor.
        Юзер сможет сразу запустить ещё один эпизод не закрывая Studio."""
        try:
            self.log_view.clear()
            self.paste_edit.clear()
            self.ep_edit.clear()
            self._pending_text = ""
            self._pending_source = ""
            self.chat_input.clear()
            self.chat_row_widget.hide()
            self.open_chat_row.hide()
            # Phase 2: возвращаем compact creation-блок чтобы юзер мог
            # сразу запустить новый эпизод (drop+paste+ep+run снова видны).
            self.creation_block.show()
            # Phase 2 hotfix #5: скрываем log_view + status_lbl до
            # следующего клика «Запустить» — пустые блоки не висят на «+».
            self.log_view.hide()
            self.status_lbl.hide()
            self._has_initial_run = False
            self._current_ep_id = None
            self._set_status(None)
            self._update_run_btn_state()
        except Exception:
            pass

    def _on_thread_error(self, msg: str):
        # 2026-05-11 multi-ep misrouting fix: sender-aware запись «✗ Ошибка»
        # в JSONL правильного эпизода. Раньше шло через `_append_log_persist`,
        # который пишет в `self._current_ep_id` формы — если форма уже
        # переключилась на новый эпизод, сообщение «✗ Ошибка» от упавшего
        # параллельного треда попадало в чужой чат. Теперь записываем
        # напрямую в `target_ep` (sender_ep || form's current).
        sender_e = self.sender()
        sender_e_ep = getattr(sender_e, '_ep_id', None) if sender_e is not None else None
        target_ep = sender_e_ep or self._current_ep_id
        is_current_form_thread = (target_ep == self._current_ep_id)

        self._flush_chunk_buffer()
        err_text = f"\n\n{tr('new_ep_log_error')}: {msg}\n"
        try:
            if target_ep:
                _sa.append_chat_message(target_ep, "system", err_text, kind='error')
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev.on_external_append(target_ep, err_text, 'error')
            if is_current_form_thread and not self._handed_off:
                self._append_log(err_text, kind='error')
        except Exception:
            traceback.print_exc()

        # UI обновления — только для треда текущего эпизода формы.
        # Параллельные треды других эпизодов не должны трогать вид «+».
        if is_current_form_thread:
            self._thinking_timer.stop()
            self.stop_btn.setEnabled(False)
            if not self._handed_off:
                # Ошибочный текст содержит msg от AI — не переводим, ставим напрямую
                self._status_key = None
                self._status_params = {}
                self._status_color = "#cc6666"
                self.status_lbl.setText(f"{tr('new_ep_log_error')}: {msg[:120]}")
                self.status_lbl.setStyleSheet("color:#cc6666; font-size:12px;")
                # Если уже был успешный run — оставляем чат активным;
                # иначе ошибка ДО первого успеха → возвращаем creation-блок.
                if self._has_initial_run:
                    self.send_btn.setEnabled(True)
                else:
                    self.creation_block.show()

        # 2026-05-08: сбрасываем `_current_ep_id` только если упавший тред
        # был для текущего эпизода формы (а не параллельный фоновый).
        if is_current_form_thread and self._handed_off:
            self._current_ep_id = None
            self._handed_off = False
        try:
            if sender_e_ep and self._threads.get(sender_e_ep) is sender_e:
                self._threads.pop(sender_e_ep, None)
        except Exception:
            pass
        self._update_run_btn_state()

    def _on_thread_stopped(self):
        # 2026-05-11 multi-ep misrouting fix: sender-aware запись
        # «⏹ Остановлено» в JSONL правильного эпизода. Раньше шло через
        # `_append_log_persist` → попадала в `self._current_ep_id` формы,
        # даже если остановился тред другого эпизода (юзер в форме уже
        # начал заводить новый ep). См. зеркальный фикс в `_on_thread_error`.
        sender_s = self.sender()
        sender_s_ep = getattr(sender_s, '_ep_id', None) if sender_s is not None else None
        target_ep = sender_s_ep or self._current_ep_id
        is_current_form_thread = (target_ep == self._current_ep_id)

        self._flush_chunk_buffer()
        stopped_text = f"\n\n{tr('new_ep_log_stopped')}\n"
        try:
            if target_ep:
                _sa.append_chat_message(target_ep, "system", stopped_text, kind='warn')
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev.on_external_append(target_ep, stopped_text, 'warn')
            if is_current_form_thread and not self._handed_off:
                self._append_log(stopped_text, kind='warn')
        except Exception:
            traceback.print_exc()

        # UI обновления — только для треда текущего эпизода формы.
        if is_current_form_thread:
            self._thinking_timer.stop()
            if not self._handed_off:
                self._set_status('new_ep_log_stopped', color="#aaa")
                self.stop_btn.setEnabled(False)
                if self._has_initial_run:
                    self.send_btn.setEnabled(True)
                else:
                    # Phase 2: стоп ДО первого успеха → возвращаем creation-блок.
                    self.creation_block.show()
        if is_current_form_thread and self._handed_off:
            self._current_ep_id = None
            self._handed_off = False
        try:
            if sender_s_ep and self._threads.get(sender_s_ep) is sender_s:
                self._threads.pop(sender_s_ep, None)
        except Exception:
            pass
        self._update_run_btn_state()

    # Цвета для разных типов строк в чате. Поддерживается:
    #   None     — обычный stdout от Claude (нейтральный серый)
    #   'system' — ▶ системные строки (фиолетовый)
    #   'user'   — 💬 сообщение юзера (голубой)
    #   'ok'     — ✓ Готово (зелёный)
    #   'warn'   — предупреждения (жёлтый)
    #   'error'  — ⚠ ошибка (красный)
    _LOG_COLORS = {
        None:     "#cfcfcf",
        'system': "#b08af7",
        'user':   "#6fb6ff",
        'ok':     "#6db86d",
        'warn':   "#ffd24d",
        'error':  "#ff7a7a",
    }

    def _append_log_persist(self, text: str, kind: Optional[str] = None,
                              role: str = "system"):
        """Помимо UI — пишет реплику в `chats/<ep_id>.jsonl` если есть текущий
        эпизод. И уведомляет EpisodeChatView в Editor чтобы он добавил строку
        к открытой истории (если юзер сейчас на нём)."""
        self._append_log(text, kind=kind)
        if self._current_ep_id:
            _sa.append_chat_message(self._current_ep_id, role, text, kind=kind)
            try:
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    ev.on_external_append(self._current_ep_id, text, kind)
            except Exception:
                pass

    def _maybe_detect_auth_error(self, text: str) -> None:
        """2026-05-06: ловит в стриме CLI типичные строки про исчерпан лимит /
        не авторизован / rate limit. Если поймали — просим MainWindow
        показать AuthBanner соответствующего типа.

        Срабатывает один раз за стрим (`self._auth_error_signaled`), чтобы
        не дёргать баннер на каждом chunk'е.
        """
        if not text:
            return
        if getattr(self, '_auth_error_signaled', False):
            return
        low = text.lower()
        # Ключевые маркеры из claude CLI ответов.
        QUOTA_MARKERS = (
            "out of extra usage",
            "rate limit",
            "rate-limit",
            "quota exceeded",
            "usage limit",
        )
        AUTH_MARKERS = (
            "not authenticated",
            "not logged in",
            "please log in",
            "authentication required",
            "401 unauthorized",
        )
        kind: Optional[str] = None
        if any(m in low for m in QUOTA_MARKERS):
            kind = 'quota'
        elif any(m in low for m in AUTH_MARKERS):
            kind = 'logged_out'
        if kind is None:
            return
        self._auth_error_signaled = True
        try:
            if hasattr(self._mw, '_show_auth_banner'):
                self._mw._show_auth_banner(kind)
        except Exception:
            pass

    def _on_chunk_persist(self, text: str):
        """Слот для `output_chunk` потока. Пишет одновременно в UI лог,
        в jsonl файл эпизода (как `assistant`) и в EpisodeChatView.

        Sub-MVP «кнопки автономной генерации»: извлекаем GEN-маркеры
        ДО рендера, чтобы они не светились в логе и в JSONL. Чистый
        текст идёт во все три приёмника. Кнопки создаются в
        EpisodeChatView (через on_external_append, который сам парсит
        чистый текст — в нём маркеров уже нет, но он умеет парсить
        исходный текст если их пробросить иначе).

        2026-05-08: ep_id треда берётся из `self.sender()._ep_id` (не из
        shared `self._current_ep_id`). Иначе при параллельных запусках
        ep5/ep6/ep7 чанки старых тредов попадали в чат последнего ep'а.
        """
        clean_text, markers = parse_gen_markers(text)
        # Phase 2 hotfix #8: накапливаем clean_text для fallback-парсера.
        self._stream_full += clean_text
        # 2026-05-06: detect AI-auth/quota ошибок в стриме CLI. Когда лимит
        # на текущем аккаунте исчерпан, CLI пишет в stdout строки типа:
        #   "You're out of extra usage · resets 4pm (Europe/Kiev)"
        #   "Rate limit reached"
        #   "Not authenticated"
        # Парсим и сразу показываем AuthBanner — юзер не должен ждать 90с
        # таймера auth_check_tick.
        try:
            self._maybe_detect_auth_error(clean_text)
        except Exception:
            pass
        # 2026-05-08: вычисляем ep_id отправителя сигнала, fallback на
        # `_current_ep_id` для legacy-followup'ов которые шлются от
        # `self._thread` без `_ep_id` атрибута.
        sender = self.sender()
        sender_ep = getattr(sender, '_ep_id', None) if sender is not None else None
        target_ep = sender_ep or self._current_ep_id
        # Лог в NewEpisodeView рисуем только если этот тред — для
        # текущего эпизода формы. Иначе пишем тихо в JSONL/EpisodeChatView.
        if target_ep == self._current_ep_id and not getattr(self, '_handed_off', False):
            self._append_log(clean_text, kind=None)
        if target_ep:
            _sa.append_chat_message(target_ep, "assistant",
                                    clean_text, kind=None)
            try:
                ev = getattr(self._mw, 'episode_chat_view', None)
                if ev is not None:
                    # Передаём ИСХОДНЫЙ text (с маркерами) — чтобы chat
                    # smyl создать GenButton по маркерам. on_external_append
                    # сам распарсит и отрендерит clean.
                    ev.on_external_append(target_ep, text, None)
            except Exception:
                pass

    def _append_log(self, text: str, kind: Optional[str] = None):
        """Добавляет строку(и) в лог с подсветкой по типу.

        - `kind` задан явно (system/user/ok/warn/error) → ВСЯ строка красится
          в этот цвет, как было раньше. Используется для UI-сообщений Studio.
        - `kind=None` → это chunk потока Claude. Текст разбивается по \\n,
          для каждой полной строки определяется kind через `_sa.detect_line_kind`
          (✓ → ok, ✗ → warn, 🌐 → progress, **bold** → жирный белый, …).
          Неполная строка буферизируется в `_chunk_buffer` до прихода \\n.
        """
        if not text:
            return
        # Phase 2.1: после авто-переезда в EpisodeChatView ничего не пишем
        # в наш log_view. Юзер его не видит (он на ЭП → ЧАТ), а при
        # возврате на «+» log_view должен быть чистым для следующего
        # эпизода. Запись в jsonl и уведомление EpisodeChatView идут
        # параллельно в `_append_log_persist` / `_on_chunk_persist`.
        if self._handed_off:
            return
        if kind is not None:
            color = _sa.CHAT_LINE_COLORS.get(kind, _sa.CHAT_LINE_COLORS[None])
            html = _sa.format_chat_inline(text)
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            self.log_view.insertHtml(
                f'<span style="color:{color}; white-space:pre-wrap;">{html}</span>')
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            sb = self.log_view.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())
            return
        # Поток Claude — буферизируем до newline и красим построчно
        self._chunk_buffer = getattr(self, '_chunk_buffer', '') + text
        while '\n' in self._chunk_buffer:
            line, _, self._chunk_buffer = self._chunk_buffer.partition('\n')
            self._render_chat_line(line + '\n')

    def _render_chat_line(self, line: str):
        """Эвристически определяет kind строки и рендерит её в log_view с inline
        markdown-подсветкой (**bold**, `code`)."""
        line_kind = _sa.detect_line_kind(line)
        color = _sa.CHAT_LINE_COLORS.get(line_kind, _sa.CHAT_LINE_COLORS[None])
        html = _sa.format_chat_inline(line)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertHtml(
            f'<span style="color:{color}; white-space:pre-wrap;">{html}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        sb = self.log_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _flush_chunk_buffer(self):
        """Выводит остаток `_chunk_buffer` (строку без завершающего \\n).
        Вызывается при завершении/ошибке/остановке потока, чтобы не пропадал
        текст без переноса в конце."""
        rest = getattr(self, '_chunk_buffer', '')
        if rest:
            self._render_chat_line(rest)
            self._chunk_buffer = ''

    def _set_status_error(self, msg: str):
        # msg уже переведённая (через tr) — но при смене языка такой ошибочный
        # статус становится stale. Не сохраняем в _status_key, очищаем чтобы
        # apply_lang не пытался перевести произвольную строку.
        self._status_key = None
        self._status_params = {}
        self._status_color = "#cc6666"
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color:#cc6666; font-size:12px;")

    def _current_model(self) -> Optional[str]:
        """Читает текущую модель свободного чата из QSettings (тот же
        ключ что у дропдауна в EpisodeChatView).

        2026-05-09: виджет дропдауна убран из этого view — модель
        синхронизируется через QSettings. RunEpisodeThread на этом
        стартовом экране (свободный чат до handed-off) использует ту
        же модель что и чат эпизода после переезда. Дефолт — Opus 4.7
        для legacy-юзеров без записи в QSettings.
        """
        try:
            qs = QSettings(_sa.APP_ORG, _sa.APP_NAME)
            return qs.value("new_ep/model_v2", "claude-opus-4-7", type=str)
        except Exception:
            return None
