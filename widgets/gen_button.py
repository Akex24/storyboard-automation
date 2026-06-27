# -*- coding: utf-8 -*-
"""
widgets/gen_button.py — компактная карточка-кнопка автономной генерации
рефа в чате эпизода. Sub-MVP: одна за раз (Phase 1).

UX-состояния:
  • idle    — три кнопки: «🎨 Сгенерировать», «🚫 Не нужен»,
              «📁 Выбрать существующий»
  • running — прогресс-фраза + спиннер-точки (некликабельная)
  • done    — «✓ <name> готов — открыть в РЕФЕРЕНСАХ»
  • error   — «✗ <name> ошибка: <msg>» + маленькая «↻ Повторить»
  • skipped — «🚫 <name> помечен как ненужный» + «↶ Передумал»
              (Долг 13, добавлено 2026-05-04 hotfix #10)
  • linked  — «📁 <name> → выбран файл «X.jpg»» + «↶ Передумал»
              (Долг 13)

Сигналы наружу:
  • generate_requested(type, name, description) — клик «🎨 Сгенерировать»
  • skip_requested(type, name) — клик «🚫 Не нужен»
  • use_existing_requested(type, name) — клик «📁 Выбрать существующий»
  • undo_requested(type, name) — клик «↶ Передумал» в skipped/linked
  • open_refs_requested() — клик в done
  • retry_requested() — клик в error

Виджет НЕ запускает тред сам и НЕ открывает попапы — это делает
caller (EpisodeChatView). Так удобнее: модуль `widgets/` не знает
про `threads/`, `views/` или `Q*Dialog`. Чисто UI.

История: создано 2026-05-04 для sub-MVP «кнопка автономной генерации
в чате эпизода». Расширено 2026-05-04 (Долг 13) до трёх кнопок выбора.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
)

from i18n import tr


class GenButton(QFrame):
    """Карточка-кнопка автономной генерации одного рефа."""

    generate_requested = pyqtSignal(str, str, str)   # type, name, description
    open_refs_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    # Долг 13 (2026-05-04 hotfix #10): три кнопки выбора в idle.
    skip_requested = pyqtSignal(str, str)            # type, name
    use_existing_requested = pyqtSignal(str, str)    # type, name
    undo_requested = pyqtSignal(str, str)            # type, name (для skipped/linked)

    def __init__(self, gen_type: str, name: str, description: str,
                 parent=None, display_name: str = ""):
        super().__init__(parent)
        self._gen_type = gen_type
        self._name = name
        # Долг 13 (2026-05-05): человекочитаемое имя для UI. Если пусто —
        # юзер видит сам slug (то же что было раньше). Если задано —
        # карточка показывает «<slug> (<display>)» и в текстах кнопок
        # «🎨 Сгенерировать «slug (Муж)»».
        self._display = (display_name or "").strip()
        self._description = description
        self._state = "idle"  # idle / running / done / error / skipped / linked
        self._linked_filename: str = ""  # имя файла после use_existing
        self._dot_step = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._last_progress = ""
        self._setup_ui()
        self._apply_state()

    def _name_for_user(self) -> str:
        """Человекочитаемое имя для UI: если задан display — «<slug> (<display>)»,
        иначе сам slug. Используется в title, кнопке «Сгенерировать» и
        прогресс-фразах."""
        if self._display and self._display.lower() != self._name.lower():
            return f"{self._name} ({self._display})"
        return self._name

    # ── UI ────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 2026-05-08: полный переход GenButton с фиолетового old-стиля
        # на LUMZ. Цвета берутся из views/theme.py:LUMZ_THEME (но
        # инлайнятся в QSS т.к. setStyleSheet не умеет шаблонизацию).
        # Маппинг:
        #   bg_subtle = rgba(255,255,255,0.04)
        #   border_default = rgba(255,255,255,0.06)
        #   border_strong = rgba(255,255,255,0.12)
        #   text_primary = #ffffff
        #   text_secondary = rgba(255,255,255,0.55)
        #   accent_red = #e4344a (primary action)
        #   accent_red_subtle = rgba(228,52,74,0.10) (error bg)
        #   accent_red_border = rgba(228,52,74,0.40) (error border / hover red)
        #   accent_gold = #d4a256 (running progress / done)
        #   accent_gold_subtle = rgba(212,162,86,0.10) (running bg accent)
        #   accent_gold_border = rgba(212,162,86,0.40) (running border)
        self.setStyleSheet(
            # ── База карточки ────────────────────────────────────────
            "GenButton { background:rgba(255,255,255,0.04);"
            " border:1px solid rgba(255,255,255,0.06);"
            " border-radius:8px; }"
            "GenButton[state=\"running\"] {"
            " border-color:rgba(212,162,86,0.40); }"
            "GenButton[state=\"done\"] {"
            " border-color:rgba(212,162,86,0.40);"
            " background:rgba(212,162,86,0.10); }"
            "GenButton[state=\"error\"] {"
            " border-color:rgba(228,52,74,0.40);"
            " background:rgba(228,52,74,0.10); }"
            "GenButton[state=\"skipped\"] {"
            " border-color:rgba(255,255,255,0.06);"
            " background:rgba(255,255,255,0.03); }"
            "GenButton[state=\"linked\"] {"
            " border-color:rgba(255,255,255,0.12);"
            " background:rgba(255,255,255,0.04); }"
            # ── Лейблы ───────────────────────────────────────────────
            "QLabel#gen-title { color:#ffffff; font-size:13px;"
            " font-weight:600; }"
            "QLabel#gen-desc { color:rgba(255,255,255,0.55);"
            " font-size:11px; }"
            "QLabel#gen-progress { color:#d4a256; font-size:12px;"
            " font-family:'Menlo','Consolas',monospace; }"
            "QLabel#gen-done { color:#d4a256; font-size:12px;"
            " font-weight:600; }"
            "QLabel#gen-error { color:#e35d5d; font-size:12px; }"
            # ── Primary action: «Сгенерировать» (залитая красная) ───
            "QPushButton#gen-action { background:#e4344a; color:#fefefe;"
            " border:none; border-radius:6px; padding:6px 14px;"
            " font-size:12px; font-weight:500; }"
            "QPushButton#gen-action:hover { background:#d92d44; }"
            "QPushButton#gen-action:pressed { background:#c52539; }"
            "QPushButton#gen-action:disabled {"
            " background:rgba(255,255,255,0.06);"
            " color:rgba(255,255,255,0.40); }"
            # ── Secondary (нейтральные): pick / skip / undo / open ──
            # Все в одном save-style: bg_hover + border_strong + white.
            "QPushButton#gen-pick, QPushButton#gen-skip,"
            " QPushButton#gen-undo, QPushButton#gen-open {"
            " background:rgba(255,255,255,0.06);"
            " border:1px solid rgba(255,255,255,0.12);"
            " color:#fbfbfb; border-radius:6px;"
            " padding:6px 14px; font-size:12px; font-weight:500; }"
            "QPushButton#gen-pick:hover, QPushButton#gen-skip:hover,"
            " QPushButton#gen-undo:hover, QPushButton#gen-open:hover {"
            " background:rgba(255,255,255,0.10);"
            " border-color:rgba(255,255,255,0.20); }"
            # ── Retry (после error): красный subtle ─────────────────
            "QPushButton#gen-retry {"
            " background:rgba(228,52,74,0.10);"
            " color:#e4344a;"
            " border:1px solid rgba(228,52,74,0.25);"
            " border-radius:6px; padding:6px 12px;"
            " font-size:12px; font-weight:500; }"
            "QPushButton#gen-retry:hover {"
            " background:rgba(228,52,74,0.18);"
            " border-color:rgba(228,52,74,0.40); }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        # Шапка: тип + имя + описание
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        # 2026-05-08: убраны иконки в шапке (📍/🎁/👤) — юзер просил
        # чистый текст без эмодзи. Тип уже понятен из контекста чата.
        self.title_lbl = QLabel(self._name_for_user())
        self.title_lbl.setObjectName("gen-title")
        head_row.addWidget(self.title_lbl)
        head_row.addStretch()
        outer.addLayout(head_row)

        if self._description:
            self.desc_lbl = QLabel(self._description)
            self.desc_lbl.setObjectName("gen-desc")
            self.desc_lbl.setWordWrap(True)
            outer.addWidget(self.desc_lbl)

        # Строка прогресса (только в running/error)
        self.progress_lbl = QLabel("")
        self.progress_lbl.setObjectName("gen-progress")
        self.progress_lbl.setWordWrap(True)
        outer.addWidget(self.progress_lbl)

        # Низ: кнопки
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.action_btn = QPushButton(
            tr('gen_btn_start', name=self._name_for_user()))
        self.action_btn.setObjectName("gen-action")
        self.action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_btn.clicked.connect(self._on_action_clicked)
        btn_row.addWidget(self.action_btn)

        self.retry_btn = QPushButton(tr('gen_btn_retry'))
        self.retry_btn.setObjectName("gen-retry")
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.clicked.connect(self.retry_requested.emit)
        self.retry_btn.hide()
        btn_row.addWidget(self.retry_btn)

        self.open_btn = QPushButton(tr('gen_btn_open_refs'))
        self.open_btn.setObjectName("gen-open")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self.open_refs_requested.emit)
        self.open_btn.hide()
        btn_row.addWidget(self.open_btn)

        # Долг 13: «🚫 Не нужен» и «📁 Выбрать существующий» в idle.
        self.skip_btn = QPushButton(tr('gen_btn_skip'))
        self.skip_btn.setObjectName("gen-skip")
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.clicked.connect(self._on_skip_clicked)
        btn_row.addWidget(self.skip_btn)

        self.pick_btn = QPushButton(tr('gen_btn_use_existing'))
        self.pick_btn.setObjectName("gen-pick")
        self.pick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pick_btn.clicked.connect(self._on_pick_clicked)
        btn_row.addWidget(self.pick_btn)

        # «↶ Передумал» — для skipped/linked состояний (вернуть в idle).
        self.undo_btn = QPushButton(tr('gen_btn_undo'))
        self.undo_btn.setObjectName("gen-undo")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(self._on_undo_clicked)
        self.undo_btn.hide()
        btn_row.addWidget(self.undo_btn)

        btn_row.addStretch()
        outer.addLayout(btn_row)

    # ── Состояния ─────────────────────────────────────────────────────

    def _apply_state(self):
        self.setProperty("state", self._state)
        self.style().unpolish(self)
        self.style().polish(self)
        # По умолчанию все «дополнительные» кнопки скрыты — каждое состояние
        # показывает только свой набор. Простое перечисление всех hide()
        # надёжнее чем поштучные hide в каждой ветке.
        for b in (self.action_btn, self.retry_btn, self.open_btn,
                  self.skip_btn, self.pick_btn, self.undo_btn):
            b.hide()
        # Phase 2 hotfix #16: в done скрываем заголовок «📍 name» и
        # описание — они дублируют зелёную done-строку «✓ name готов»
        # и засоряют чат. В остальных state'ах показываем как раньше.
        show_head = self._state != "done"
        self.title_lbl.setVisible(show_head)
        if hasattr(self, 'desc_lbl'):
            self.desc_lbl.setVisible(show_head)
        if self._state == "idle":
            # Три кнопки: 🎨 Сгенерировать | 🚫 Не нужен | 📁 Выбрать
            # Phase 2 hotfix #23: для character скрываем «🚫 Не нужен»
            # — персонаж не может быть «не нужен» в эпизоде где он
            # упомянут, юзер должен явно выбрать реф из существующих.
            self.action_btn.show()
            self.action_btn.setEnabled(True)
            self.action_btn.setText(tr('gen_btn_start', name=self._name_for_user()))
            # 2026-05-08: skip_btn («Не нужен») больше НЕ показываем —
            # юзер просил убрать. Виджет остаётся orphan для совместимости
            # (сигнал skip_requested и slot _on_gen_skip живут — на случай
            # если кнопку вернут).
            self.pick_btn.show()
            self.progress_lbl.setText("")
            self._dot_timer.stop()
        elif self._state == "running":
            self.action_btn.show()
            self.action_btn.setEnabled(False)
            self.action_btn.setText(tr('gen_btn_running'))
            self.progress_lbl.setStyleSheet("")  # сброс на default
            self.progress_lbl.setObjectName("gen-progress")
            self._dot_step = 0
            self._dot_timer.start()
        elif self._state == "done":
            self.open_btn.show()
            self.progress_lbl.setObjectName("gen-done")
            self.style().unpolish(self.progress_lbl)
            self.style().polish(self.progress_lbl)
            self.progress_lbl.setText(
                tr('gen_state_done', name=self._name_for_user()))
            self._dot_timer.stop()
        elif self._state == "error":
            self.retry_btn.show()
            self.progress_lbl.setObjectName("gen-error")
            self.style().unpolish(self.progress_lbl)
            self.style().polish(self.progress_lbl)
            self._dot_timer.stop()
        elif self._state == "skipped":
            # «🚫 <name> помечен как ненужный» + «↶ Передумал»
            self.undo_btn.show()
            self.progress_lbl.setObjectName("gen-progress")
            self.progress_lbl.setStyleSheet(
                "color:#aaa; font-size:12px;")
            self.progress_lbl.setText(
                tr('gen_state_skipped', name=self._name_for_user()))
            self._dot_timer.stop()
        elif self._state == "linked":
            # «📁 <name> → выбран файл «X.jpg»» + «↶ Передумал»
            self.undo_btn.show()
            self.progress_lbl.setObjectName("gen-progress")
            self.progress_lbl.setStyleSheet(
                "color:#a8c8ff; font-size:12px;")
            self.progress_lbl.setText(
                tr('gen_state_linked',
                   name=self._name_for_user(),
                   filename=self._linked_filename))
            self._dot_timer.stop()

    def _tick_dots(self):
        if self._state != "running":
            return
        self._dot_step = (self._dot_step + 1) % 4
        dots = ["·   ", "··  ", "··· ", "····"][self._dot_step]
        base = self._last_progress or tr('gen_state_running_default')
        self.progress_lbl.setText(f"{base} {dots}")

    # ── Публичный API (вызывается из EpisodeChatView) ────────────────

    def set_running(self):
        self._state = "running"
        self._apply_state()

    def set_progress(self, text: str):
        """Принимает короткую строку прогресса от треда."""
        self._last_progress = (text or "").strip()
        if self._state != "running":
            return
        # Перерисуем сразу (точки добавит таймер)
        self.progress_lbl.setText(self._last_progress)

    def set_image_ready(self):
        """Phase 2 hotfix #18: location-картинка уже на диске, идёт
        запись геометрии. Меняем running-фразу на «✓ картинка готова —
        описываю геометрию» (синхронизация с появлением в РЕФЕРЕНСАХ).
        Состояние остаётся `running` — таймер точек продолжает идти."""
        self._last_progress = tr('gen_state_image_ready')
        if self._state == "running":
            self.progress_lbl.setText(self._last_progress)

    def set_done(self):
        self._state = "done"
        self._apply_state()

    def set_error(self, msg: str):
        self._state = "error"
        self._last_progress = msg or ""
        self._apply_state()
        self.progress_lbl.setText(
            tr('gen_state_error', msg=(msg or "")[:120]))

    def reset_to_idle(self):
        """Сброс в idle (для retry — caller её зовёт перед перезапуском)."""
        self._state = "idle"
        self._last_progress = ""
        self._linked_filename = ""
        self._apply_state()

    def set_skipped(self):
        """Долг 13: пометить как «не нужен» — карточка остаётся в чате,
        но без кнопок действий кроме «↶ Передумал»."""
        self._state = "skipped"
        self._linked_filename = ""
        self._apply_state()

    def set_linked(self, filename: str):
        """Долг 13: пометить что юзер выбрал существующий файл `filename`.
        В режиме linked показываем «📁 <name> → выбран файл «X.jpg»»."""
        self._state = "linked"
        self._linked_filename = filename or ""
        self._apply_state()

    def apply_lang(self):
        """Перевод текстов на текущий язык."""
        if self._state == "idle":
            self.action_btn.setText(tr('gen_btn_start', name=self._name_for_user()))
        elif self._state == "running":
            self.action_btn.setText(tr('gen_btn_running'))
        self.retry_btn.setText(tr('gen_btn_retry'))
        self.open_btn.setText(tr('gen_btn_open_refs'))
        if self._state == "done":
            self.progress_lbl.setText(
                tr('gen_state_done', name=self._name_for_user()))

    # ── Внутренние слоты ─────────────────────────────────────────────

    def _on_action_clicked(self):
        if self._state != "idle":
            return
        self.generate_requested.emit(
            self._gen_type, self._name, self._description)

    def _on_skip_clicked(self):
        """Долг 13: «🚫 Не нужен» в idle. Эмитим сигнал — caller сам
        записывает решение в episodes.json и зовёт `set_skipped()`."""
        if self._state != "idle":
            return
        self.skip_requested.emit(self._gen_type, self._name)

    def _on_pick_clicked(self):
        """Долг 13: «📁 Выбрать существующий» в idle. Эмитим сигнал —
        caller открывает QFileDialog в `refs/<type>/`, после выбора
        зовёт `set_linked(filename)`."""
        if self._state != "idle":
            return
        self.use_existing_requested.emit(self._gen_type, self._name)

    def _on_undo_clicked(self):
        """Долг 13: «↶ Передумал» в skipped/linked. Эмитим — caller
        стирает запись из episodes.json и зовёт `reset_to_idle()`."""
        if self._state not in ("skipped", "linked"):
            return
        self.undo_requested.emit(self._gen_type, self._name)
