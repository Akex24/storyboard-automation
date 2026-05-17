# -*- coding: utf-8 -*-
"""
views/episode_chat.py — чат конкретного эпизода (внутри Editor).

Содержит:
    - ChatInputEdit — QPlainTextEdit с поведением мессенджера (Enter/Shift+Enter/Cmd+Enter)
    - EpisodeChatView — панель чата эпизода (страница 2 в content_stack Редактора)

Зависимости от storyboard_app.py (через `_AppProxy` lazy proxy):
    - APP_ORG, APP_NAME (для QSettings)
    - block_wheel_event
    - load_chat_messages, append_chat_message
    - CHAT_LINE_COLORS, format_chat_inline, detect_line_kind

Зависимости от threads (прямой импорт):
    - RunEpisodeThread (threads.generate)

История: вытащено из storyboard_app.py 2026-05-04 (шаг 5B рефакторинга).
"""

from __future__ import annotations

import traceback
from typing import Dict, Optional

from PyQt6.QtCore import Qt, QTimer, QSettings, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QPlainTextEdit, QTextEdit, QComboBox,
    QVBoxLayout, QHBoxLayout, QMessageBox,
)

from i18n import tr
from threads import RunEpisodeThread, AutonomousGenThread, SuggestOutfitsThread
from views._chat_render import parse_gen_markers, synthesize_gen_markers
from widgets import GenButton, CharacterOutfitPicker
from widgets.montage_cta import MontageCTA
from widgets.montage_summary_dialog import MontageSummaryDialog


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


# ─── Поле ввода чата с поддержкой Enter / Shift+Enter / Cmd+Enter ────────────

class ChatInputEdit(QPlainTextEdit):
    """QPlainTextEdit с поведением мессенджера:
       Enter           → отправить (emit `submit_requested`)
       Shift+Enter     → новая строка (стандартное поведение QPlainTextEdit)
       Cmd/Ctrl+Enter  → отправить (тоже работает — для тех кто привык)

    Кросс-платформенно: Mac (Cmd+Enter), Windows/Linux (Ctrl+Enter), плюс
    голый Enter везде. Сам клик по кнопке «Отправить» остаётся как был.
    """
    submit_requested = pyqtSignal()

    def keyPressEvent(self, event):
        key = event.key()
        # Enter без Shift → отправка. С Shift — обычный перевод строки.
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            if mods & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Enter — пробрасываем дальше (новая строка)
                super().keyPressEvent(event)
                return
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# 2026-05-10: фикс БАГ 2 — keyword-эвристика «character marker — это
# на самом деле животное?». Studio показывает CharacterOutfitPicker
# для любого character-маркера, но для собак/кошек/лошадей выбор
# одежды абсурден. Если описание/slug содержит хотя бы одно ключевое
# слово ниже — animal-flow вместо outfit picker'а (=> object-генерация).
# Это safety net: первичный фикс — правило в PRODUCER_INSTRUCTIONS +
# new_episode.py промпт «животные → секция ОБЪЕКТЫ». Этот список —
# страховка на случай если агент опять напутает.
_ANIMAL_KEYWORDS = (
    # RU
    'пёс', 'пес', 'собак', 'кот', 'котён', 'кошк',
    'лошад', 'конь', 'коня', 'коне',
    'птиц', 'медвед', 'волк', 'лис',
    'крыс', 'мыш', 'хомяк', 'кролик',
    'животн',
    # UA
    'кіт', 'кішк', 'кінь', 'птах', 'тварин',
    'ведмідь', 'вовк',
    # EN
    'dog', 'cat', 'horse', 'bird', 'rabbit', 'mouse', 'rat',
    'bear', 'wolf', 'fox',
    'animal', 'pet', 'creature', 'beast',
)


# ─── Чат конкретного эпизода (внутри Editor → content_stack page 2) ───────────

class EpisodeChatView(QWidget):
    """Панель чата для одного эпизода. История читается из
    `shows/<slug>/chats/<ep_id>.jsonl`, новые сообщения append'ятся туда же.

    UI: сверху — лог сообщений (QTextEdit с HTML-цветами как в NewEpisodeView).
    Снизу — поле ввода + кнопка «Отправить» (Cmd+Enter).

    Один экземпляр на MainWindow. При смене эпизода вызывается `set_episode()` —
    подгружает историю заново. При новых сообщениях NewEpisodeView дёргает
    `on_external_append()` чтобы обновить UI без перечитывания файла.
    """
    # Используем общий CHAT_LINE_COLORS на уровне модуля.

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._mw = main_window
        self._ep_id: Optional[str] = None
        self._thread: Optional['RunEpisodeThread'] = None
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(400)
        self._thinking_timer.timeout.connect(self._tick_thinking)
        self._thinking_step = 0
        self._thinking_active = False
        # 2026-05-08 hand-off fix: тред из NewEpisodeView (юзер запустил
        # новый эпизод и сразу переехал в чат). Свой `self._thread` ещё
        # None, но анимация thinking должна работать. begin_external_thinking
        # привязывает чужой тред и запускает тикер.
        #
        # 2026-05-11 multi-ep fix: РЕЕСТР тредов per ep_id вместо одного
        # слота. Симптом был — при параллельных генерациях в нескольких
        # эпизодах завершение ЛЮБОГО треда гасило таймер для всех (один
        # слот `_external_thread` + `thread.finished.connect` от каждого
        # запуска). Теперь dict: ключ ep_id → его тред; finished-хендлер
        # снимает только свой entry; тикер живёт пока у текущего ep_id
        # есть хоть один живой тред.
        self._external_threads: dict[str, 'RunEpisodeThread'] = {}
        # Буфер chunks от ассистента — рендерим построчно как в NewEpisodeView
        self._chunk_buffer: str = ''
        # Sub-MVP кнопки автономной генерации (Phase 1: одна idle за раз).
        # `_gen_button` — текущий active idle-виджет в layout (None если нет).
        # `_gen_seen_names` — set уже показанных имён, чтобы не дублировать
        # кнопку при повторном маркере в том же чате.
        self._gen_button: Optional[GenButton] = None
        # 2026-05-07: параллельная генерация location/object живёт на
        # уровне MainWindow (`_active_gens` registry + ActiveGensPanel
        # попап). После клика «Сгенерувати» карточка УДАЛЯЕТСЯ из чата
        # и появляется как строка в попапе (доступном через кнопку-
        # индикатор внизу чата). EpisodeChatView сам state тредов больше
        # не хранит — спрашивает у MW через `is_active_gen` /
        # `has_active_gens_for_ep` если нужно. character — отдельный flow.
        self._gen_seen_names: set = set()
        # Phase 2 hotfix #10 (Долг 13): очередь pending маркеров. Каждый
        # элемент — (gen_type, name, description). Если активная кнопка
        # уже есть, новые маркеры пушатся сюда. После skip/linked/done
        # текущая кнопка отвязывается (`_gen_button = None`, виджет
        # остаётся в layout как история), берётся следующий из очереди.
        self._pending_markers: list = []
        # Долг 13 (2026-05-05): для character-карточек используем
        # SuggestOutfitsThread + CharacterOutfitPicker вместо обычного
        # AutonomousGenThread.
        # 2026-05-07: per-episode dict'ы. Один EpisodeChatView
        # (MainWindow-уровневый) переиспользуется при переключении
        # эпизодов — раньше single-slot picker «протекал» в другие
        # чаты. Теперь key = ep_id.
        # `_outfit_pickers`        — key → CharacterOutfitPicker (живёт
        #                            в _gen_layout, при смене эпизода
        #                            hide/show через `_associated_ep_id`).
        # `_outfit_threads`        — key → активный SuggestOutfitsThread.
        # `_outfit_target_names`   — key → имя персонажа (slug).
        # `_outfit_target_displays`— key → display-name из source GenButton.
        # `_outfit_source_btns`    — key → исходная GenButton-карточка
        #                            (она hide'нута пока picker активен).
        self._outfit_pickers: Dict[str, CharacterOutfitPicker] = {}
        self._outfit_threads: Dict[str, SuggestOutfitsThread] = {}
        self._outfit_target_names: Dict[str, str] = {}
        self._outfit_target_displays: Dict[str, str] = {}
        self._outfit_source_btns: Dict[str, GenButton] = {}
        # 2026-05-07: накопленные ранее показанные варианты — на retry
        # передаются в SuggestOutfitsThread чтобы AI не повторял
        # одно и то же. Чистится в `_cleanup_outfit_picker`. Key = ep_id.
        self._outfit_seen_variants: Dict[str, list] = {}
        # 2026-05-10 (БАГ 4 fix): description от агента из манифеста
        # чата для текущего character-маркера. Сохраняется при
        # `_start_outfit_picker(name, description)` и читается в
        # `_launch_outfit_thread` (включая retry «Ещё 3 варианта»).
        # Чистится вместе с outfit picker'ом. Key = ep_id.
        self._outfit_descriptions: Dict[str, str] = {}
        # Phase 2 hotfix #8: накопитель полного ответа AI для fallback-парсера.
        # Сбрасывается в `_on_send` перед стартом потока. Используется в
        # `_on_done` когда AI не вставил [[GEN:...]] маркеры — синтезируем
        # их из строк «- ✗ name —» по секциям.
        self._stream_full: str = ''
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.setContentsMargins(0, 0, 0, 0)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        # 2026-05-08 редизайн Этап 6: LUMZ-стиль — приглушённый фон,
        # тонкая граница, белый текст, monospace оставляем (для логов).
        self.log_view.setStyleSheet(
            "QTextEdit { background: rgba(255, 255, 255, 0.03);"
            " border: 1px solid rgba(255, 255, 255, 0.06);"
            " border-radius: 8px; padding: 14px; color: #ffffff;"
            " font-family:'Menlo','Consolas',monospace; font-size: 12px; }")
        lay.addWidget(self.log_view, stretch=1)

        # Контейнер для GenButton (sub-MVP «кнопка автономной генерации»).
        # Виджет создаётся динамически когда AI выводит маркер
        # `[[GEN:type:name:description]]` в chunk-е. Видим только пока
        # есть актуальная задача генерации.
        self._gen_layout = QVBoxLayout()
        self._gen_layout.setContentsMargins(0, 0, 0, 0)
        self._gen_layout.setSpacing(6)
        lay.addLayout(self._gen_layout)

        # 2026-05-07: Кнопка-индикатор «🎨 N в работе» с анимированными
        # точками (·/··/···, синхронно с MainWindow._dot_step). Видна
        # ТОЛЬКО когда `MainWindow._active_gens` непуст (есть хоть одна
        # параллельная генерация location/object — глобально, не только
        # в текущем эпизоде). Клик открывает попап `ActiveGensPanel`.
        # См. `tick_active_gens_button` / `refresh_active_gens_button`.
        self.active_gens_btn = QPushButton("")
        self.active_gens_btn.setObjectName("active_gens_btn")
        self.active_gens_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.active_gens_btn.setFixedHeight(32)
        self.active_gens_btn.setStyleSheet(
            "QPushButton#active_gens_btn { background:#2a1d4a;"
            " border:1px solid #4a3470; border-radius:6px;"
            " color:#fff; font-size:13px; padding:4px 14px; }"
            "QPushButton#active_gens_btn:hover { background:#3a2a60;"
            " border-color:#6a4ea0; }"
        )
        self.active_gens_btn.clicked.connect(self._on_active_gens_btn_clicked)
        self.active_gens_btn.hide()
        lay.addWidget(self.active_gens_btn)

        # 2026-05-06: Multi-agent CTA — карточка «✓ Все рефы готовы →
        # сделать монтаж и сториборды». Скрыта по умолчанию; появляется
        # когда `_check_montage_ready()` возвращает True (все упомянутые
        # маркеры linked, никаких pending карточек, не идёт outfit picker).
        self._montage_cta = MontageCTA(parent=self)
        self._montage_cta.start_requested.connect(self._on_montage_start)
        self._montage_cta.retry_requested.connect(self._on_montage_start)
        self._montage_cta.cancel_requested.connect(self._on_montage_cancel)
        # v1.0.87 (этап 7D resume-фичи): кнопки KIND_RESUMABLE CTA.
        # resume_requested — продолжить pipeline с last_completed_stage из
        # `_agent_log_<ep>.json`. start_fresh_requested — удалить лог и
        # запустить заново.
        self._montage_cta.resume_requested.connect(self._on_montage_resume)
        self._montage_cta.start_fresh_requested.connect(
            self._on_montage_start_fresh)
        # v1.0.82: новая кнопка «📂 Открыть монтажную карту»
        self._montage_cta.open_map_requested.connect(self._on_open_map_clicked)
        self._montage_cta.hide()
        lay.addWidget(self._montage_cta)
        # 2026-05-07: per-episode оркестратор монтажной карты.
        # Key = ep_id ('ep5'), value = запущенный MontageOrchestratorThread.
        # Один EpisodeChatView, но `_montage_thread` теперь хранит треды
        # для разных эпизодов параллельно — юзер может стартануть карту
        # в ep5, переключиться на ep4 и стартануть карту там же. CTA
        # показывает running ТОЛЬКО для текущего ep_id (а не «вообще
        # любой бежит»). Состояние running CTA для каждого эпизода
        # хранится в `_montage_states` чтобы при возврате восстановить
        # последний прогресс.
        self._montage_threads: Dict[str, 'MontageOrchestratorThread'] = {}
        # Snapshot последнего progress/state для каждого ep_id —
        # value = {'kind': 'running'/'failed', 'stage': str, 'info': dict,
        #          'reason': str}.
        # Используется в `set_episode` чтобы перерисовать CTA при
        # переключении на эпизод где идёт оркестратор.
        self._montage_states: Dict[str, dict] = {}
        # 2026-05-07: если оркестратор закончил пока юзер был на другом
        # эпизоде — сохраняем результат сюда. При возврате на этот ep_id
        # `set_episode` откроет MontageSummaryDialog с готовой картой.
        self._pending_montage_results: Dict[str, dict] = {}
        # 2026-05-06: периодическая проверка готовности эпизода к монтажу.
        # Сценарий: юзер залинковал реф через wildcard «+ Добавить
        # персонажа» на Актёрах → вернулся в чат (без смены эпизода).
        # `_check_montage_ready` без таймера сработал бы только при
        # `set_episode()` (другой эпизод) или после RunEpisodeThread.
        # Теперь — раз в 2с проверяем decisions и обновляем CTA.
        # Это лёгкое: чтение episodes.json + сравнение состояния.
        self._montage_ready_timer = QTimer(self)
        self._montage_ready_timer.setInterval(2000)
        # 2026-05-07: purge запускается ДО проверки CTA. Если юзер
        # залинковал реф через Actors view (без смены эпизода в Editor)
        # — purge закроет picker, и следующая проверка CTA увидит
        # «picker is None». Слоты вызываются в порядке connect'а.
        self._montage_ready_timer.timeout.connect(self._purge_resolved_markers)
        self._montage_ready_timer.timeout.connect(self._check_montage_ready)
        self._montage_ready_timer.start()

        # 2026-05-08: status_lbl УБРАН из layout (юзер: «дублирует
        # анимацию точек в самом чате»). Сам QLabel оставлен как
        # orphan-виджет (не добавлен в lay) — старый код в _tick_thinking,
        # _on_done, _on_error, _on_stopped, _on_send и т.д. продолжает
        # вызывать `.setText(...)` / `.setStyleSheet(...)` без падения,
        # просто визуально ничего не происходит. Все статусы (Готово /
        # Ошибка / Остановлено / Долго думаю) пишутся в сам чат через
        # append_chat_message + _render_message.
        self.status_lbl = QLabel("")
        self.status_lbl.setVisible(False)

        # Дропдаун выбора модели — общий с NewEpisodeView через
        # QSettings(new_ep/model). Меняешь здесь — следующее сообщение этого
        # же чата уйдёт на новой модели, и при следующем «Новый эпизод» она
        # будет подхвачена. Помещаю в отдельной тонкой строке над инпутом.
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self.model_label = QLabel(tr('new_ep_model_label'))
        self.model_label.setStyleSheet("color:#aaa; font-size:12px;")
        model_row.addWidget(self.model_label)
        self.model_combo = QComboBox()
        self.model_combo.setFixedHeight(28)
        self.model_combo.setMinimumWidth(140)
        self.model_combo.setStyleSheet(
            "QComboBox { background:#15101e; border:1px solid #322545;"
            " border-radius:6px; padding:2px 8px; color:#ddd; font-size:12px; }"
            "QComboBox::drop-down { border:0; width:18px; }"
            "QComboBox QAbstractItemView { background:#15101e; color:#ddd;"
            " selection-background-color:#322545; border:1px solid #322545; }"
        )
        for label, mid in (
            ("Sonnet 4.6", "claude-sonnet-4-6"),
            ("Opus 4.7",   "claude-opus-4-7"),
            ("Haiku 4.5",  "claude-haiku-4-5-20251001"),
        ):
            self.model_combo.addItem(label, mid)
        try:
            qs = QSettings(_sa.APP_ORG, _sa.APP_NAME)
            saved = qs.value("new_ep/model_v2", "claude-opus-4-7", type=str)
            for i in range(self.model_combo.count()):
                if self.model_combo.itemData(i) == saved:
                    self.model_combo.setCurrentIndex(i)
                    break
        except Exception:
            pass
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        _sa.block_wheel_event(self.model_combo)
        model_row.addWidget(self.model_combo)
        model_row.addStretch()
        lay.addLayout(model_row)

        # Поле ввода + кнопка отправки
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input_edit = ChatInputEdit()
        self.input_edit.setPlaceholderText(tr('chat_input_placeholder'))
        # 2026-05-08 редизайн Этап 6: LUMZ-стиль для поля ввода чата.
        self.input_edit.setStyleSheet(
            "QPlainTextEdit { background: rgba(255, 255, 255, 0.04);"
            " border: 1px solid rgba(255, 255, 255, 0.12);"
            " border-radius: 8px; padding: 10px; color: #ffffff;"
            " font-size: 13px; }")
        self.input_edit.setFixedHeight(70)
        # Enter / Shift+Enter / Cmd+Enter — обработаны в ChatInputEdit
        self.input_edit.submit_requested.connect(self._on_send)
        # Дополнительно Cmd+Enter / Ctrl+Enter — для совместимости (обычно
        # ChatInputEdit сам ловит Enter, но shortcut не помешает)
        self._send_shortcut = QShortcut(
            QKeySequence("Ctrl+Return"), self.input_edit)
        self._send_shortcut.activated.connect(self._on_send)
        row.addWidget(self.input_edit, stretch=1)

        self.send_btn = QPushButton(tr('chat_send_btn'))
        self.send_btn.setObjectName("save")
        self.send_btn.setFixedHeight(70)
        self.send_btn.setMinimumWidth(120)
        self.send_btn.clicked.connect(self._on_send)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

    def apply_lang(self):
        self.input_edit.setPlaceholderText(tr('chat_input_placeholder'))
        self.send_btn.setText(tr('chat_send_btn'))
        if hasattr(self, 'model_label'):
            self.model_label.setText(tr('new_ep_model_label'))
        # Если открыт эпизод и история пустая — обновить хинт
        if self._ep_id is not None and not _sa.load_chat_messages(self._ep_id):
            self._render_empty_state()
        # Если есть активная GenButton — обновим её тексты
        if self._gen_button is not None:
            try:
                self._gen_button.apply_lang()
            except Exception:
                pass
        # 2026-05-07: текст кнопки активных генераций.
        try:
            self.refresh_active_gens_button()
        except Exception:
            pass

    # ── 2026-05-07: кнопка-индикатор «🎨 N в работе» ─────────────────

    def _on_active_gens_btn_clicked(self):
        """Клик по кнопке-индикатору — открывает попап MW."""
        try:
            self._mw.open_active_gens_panel()
        except Exception:
            traceback.print_exc()

    def refresh_active_gens_button(self):
        """Зовётся MW при add/remove из реестра. Обновляет видимость
        кнопки и её текст («🎨 N в работе»). Если N=0 — кнопка скрыта.

        2026-05-10: счётчик per-ep (был глобальный — протекал между
        эпизодами: запустил на ep7 → видно «1 в работе» на ep4/5/6).
        `set_episode` зовёт этот метод при переключении (line 355).
        """
        try:
            n = self._mw.active_gens_count_for_ep(self._ep_id or "")
        except Exception:
            n = 0
        if n <= 0:
            self.active_gens_btn.hide()
            return
        # Базовый текст ставим сразу. Точки — добавит `tick_active_gens_button`
        # при ближайшем тике `_dot_timer` (через 400 мс максимум).
        self.active_gens_btn.setText(tr('active_gens_btn_text', n=n))
        self.active_gens_btn.show()

    def tick_active_gens_button(self, dot_step: int):
        """Анимация точек на кнопке. Зовётся из MainWindow._tick_dots
        каждые 400 мс пока есть активные генерации.

        2026-05-10: счётчик per-ep — см. `refresh_active_gens_button`.
        Если на текущем эпизоде ничего не бежит — явно скрываем кнопку
        (раньше был просто early-return, что оставляло кнопку видимой
        с предыдущего эпизода когда юзер переключался без активной ген'и
        на новом ep'е).
        """
        try:
            n = self._mw.active_gens_count_for_ep(self._ep_id or "")
        except Exception:
            n = 0
        if n <= 0:
            try:
                self.active_gens_btn.hide()
            except Exception:
                pass
            return
        dots_pattern = ["·    ", "· ·  ", "· · ·"]
        prefix = dots_pattern[dot_step % len(dots_pattern)]
        self.active_gens_btn.setText(
            f"{prefix}  " + tr('active_gens_btn_text', n=n))

    def _on_model_changed(self, _index: int):
        """Сохраняем выбор в QSettings — переживёт перезапуск Studio.

        2026-05-09: дропдаун модели остался только в этом view (для
        свободного чата эпизода). NewEpisodeView читает то же значение
        через QSettings ключ "new_ep/model_v2" в своём _current_model().
        """
        try:
            mid = self.model_combo.currentData()
            if mid:
                QSettings(_sa.APP_ORG, _sa.APP_NAME).setValue("new_ep/model_v2", mid)
        except Exception:
            pass

    def _current_model(self) -> Optional[str]:
        try:
            return self.model_combo.currentData()
        except Exception:
            return None

    def set_episode(self, ep_id: Optional[str]):
        """Переключить чат на другой эпизод. Перерисовывает историю."""
        prev_ep = self._ep_id
        self._ep_id = ep_id
        self.log_view.clear()
        self._chunk_buffer = ''
        self.status_lbl.setText("")
        # 2026-05-10: пересчитать индикатор «🎨 N в работе» под новый ep.
        # Без этого вызова кнопка оставалась видимой с предыдущего ep'а
        # (с его счётом и текстом), но без анимации точек — юзер видел
        # «1 в работе» на ep4 хотя реально ген на ep7. Раньше refresh
        # звался только из `apply_lang` (смена языка), не из set_episode.
        try:
            self.refresh_active_gens_button()
        except Exception:
            traceback.print_exc()
        # Сбрасываем состояние GenButton ТОЛЬКО при смене эпизода
        # (другой ep_id) — иначе пред-установленная кнопка из
        # NewEpisodeView._on_run пропадёт при клике «→ Открыть чат»
        # с тем же ep_id (set_episode зовётся повторно). Если идём на
        # другой эпизод — конечно очищаем (другие задачи генерации).
        if prev_ep != ep_id:
            self._clear_gen_button()
            self._gen_seen_names.clear()
            # Phase 2 hotfix #10: очередь pending маркеров тоже сбрасываем
            # — она специфична эпизоду.
            self._pending_markers.clear()
            # 2026-05-07: outfit picker'ы per-episode. Скрываем picker'ы
            # других эпизодов и показываем picker для нового ep_id (если
            # есть). Виджеты живут в `_gen_layout` (parent=self), просто
            # переключаем visibility.
            try:
                self._refresh_outfit_pickers_visibility()
            except Exception:
                traceback.print_exc()
        if not ep_id:
            return
        msgs = _sa.load_chat_messages(ep_id)
        # 2026-05-11 (v1.0.46) diagnostic: для расследования "empty
        # chats after auto-update" (если повторится — собрать stderr
        # с запуска через terminal). Минимальный overhead — 1 write
        # на пере-загрузку чата.
        try:
            import sys as _sys
            _chat_path = _sa.chat_log_path(ep_id)
            _sys.stderr.write(
                f"[set_episode] ep={ep_id} msgs={len(msgs) if msgs else 0} "
                f"path={_chat_path} exists={_chat_path.exists()}\n")
        except Exception:
            pass
        if not msgs:
            self._render_empty_state()
            return
        for m in msgs:
            self._render_message(m.get('text', ''), m.get('kind'))
        # На случай если последняя реплика не закончилась \n — допечатать
        self._flush_chunk_buffer()
        # Phase 2 hotfix #15: ретроактивная синтеза GEN-кнопок из истории.
        # Сценарий: юзер был на ЭП20 пока поток ЭП21 стримил ответ. В
        # `on_external_append` chunks для ЭП21 отбрасывались (другой
        # `_ep_id`), `_stream_full` пуст, fallback в `_on_thread_finished`
        # не сработал. Текст всё равно сохранился в jsonl, и при заходе
        # на ЭП21 видно полный ответ — но без кнопок. Восстанавливаем
        # кнопки прогоняя assistant-историю через `synthesize_gen_markers`.
        if prev_ep != ep_id:
            self._restore_gen_buttons_from_history(msgs)
        # 2026-05-06: всегда сверяем активную карточку и очередь с
        # `refs_decisions`. Если за время отсутствия в чате (например
        # юзер залинковал персонажа через wildcard «+ Добавить персонажа»
        # на вкладке Актёров) маркер уже разрешён — карточка/элемент в
        # очереди должен пропасть.
        self._purge_resolved_markers()
        # 2026-05-07: после purge явно обновляем CTA (раньше это делал
        # сам `_purge_resolved_markers` в конце, но теперь вызов оттуда
        # убран чтобы не было рекурсии при подключении к таймеру).
        self._check_montage_ready()
        # 2026-05-07: per-episode восстановление CTA для оркестратора.
        # Если для нового ep_id уже идёт оркестратор (или есть pending
        # результат от прошедшего пока юзер был на другом эпизоде) —
        # перерисовываем CTA соответствующе.
        if prev_ep != ep_id:
            self._restore_montage_cta_for_current_ep()
        # 2026-05-11 multi-ep fix: пересчитать состояние тикера под
        # новый ep_id. Если у него есть живой тред в реестре — анимация
        # подхватится (история только что перерисована и содержит маркер
        # `▶ Думаю`). Иначе — таймер останавливается, точек нет.
        try:
            self._refresh_thinking_for_current_ep()
        except Exception:
            traceback.print_exc()

    def _restore_montage_cta_for_current_ep(self):
        """Перерисовывает CTA при переключении на эпизод. Сценарии:
          • Оркестратор бежит для этого ep_id → running с последним stage.
          • Карта уже сохранена на диске (episodes.json[ep].montage_card
            или fallback _agent_log_epN.json) → show_open_map.
          • Иначе — даём `_check_montage_ready` решить (idle / hidden).

        v1.0.82: убрана ветка автооткрытия попапа через
        `_pending_montage_results.pop()`. Карта теперь всегда на диске,
        попап открывается только по клику на CTA «📂 Открыть монтажную
        карту».
        """
        ep_id = self._ep_id
        if not ep_id:
            return
        # 1) Оркестратор бежит для этого эпизода — восстанавливаем running.
        state = self._montage_states.get(ep_id)
        thread = self._montage_threads.get(ep_id)
        if (state and state.get('kind') == 'running'
                and thread is not None and thread.isRunning()):
            stage = state.get('stage') or 'scriptwriter_running'
            info = state.get('info') or {}
            try:
                if stage == 'scriptwriter_running':
                    self._montage_cta.show_running('montage_status_scriptwriter')
                elif stage == 'validator_running':
                    self._montage_cta.show_running('montage_status_validator')
                elif stage == 'geometry_editor_running':
                    # v1.0.78 (Bug 5): новая стадия из v1.0.75
                    self._montage_cta.show_running(
                        'montage_status_geometry_editor')
                elif stage == 'editor_running':
                    self._montage_cta.show_running(
                        'montage_status_editor',
                        errors_count=info.get('errors_count', 0))
                elif stage == 'validator_r2_running':
                    # v1.0.78 (Bug 5): новая стадия из v1.0.76
                    self._montage_cta.show_running(
                        'montage_status_validator_r2')
                elif stage == 'editor_r2_running':
                    # v1.0.78 (Bug 5): новая стадия из v1.0.77
                    self._montage_cta.show_running(
                        'montage_status_editor_r2',
                        errors_count=info.get('errors_count', 0))
                elif stage == 'validator_r3_running':
                    # v1.0.78 (Bug 5): новая стадия из v1.0.77
                    self._montage_cta.show_running(
                        'montage_status_validator_r3')
                elif stage == 'validator_done':
                    if info.get('ok'):
                        self._montage_cta.show_running(
                            'montage_status_round_done_clean')
                    else:
                        self._montage_cta.show_running(
                            'montage_status_round_done_errors',
                            errors_count=info.get('errors_count', 0))
                elif stage == 'context_reviewer_running':
                    self._montage_cta.show_running(
                        'montage_status_context_reviewer')
                elif stage == 'context_reviewer_done':
                    concerns_n = info.get('concerns_count', 0)
                    if info.get('ok') or concerns_n == 0:
                        self._montage_cta.show_running(
                            'montage_status_context_reviewer_clean')
                    else:
                        self._montage_cta.show_running(
                            'montage_status_context_reviewer_concerns',
                            concerns_count=concerns_n)
                else:
                    self._montage_cta.show_running('montage_status_scriptwriter')
            except Exception:
                traceback.print_exc()
            return
        # v1.0.82: 2) Карта уже сохранена на диске → CTA «📂 Открыть».
        # v1.0.87 (этап 7D): completed карта приоритетнее resumable —
        # если pipeline дошёл до конца, ресюмить нечего.
        try:
            if self._has_saved_montage_card(ep_id):
                self._montage_cta.show_open_map()
                return
        except Exception:
            traceback.print_exc()
        # v1.0.87 (этап 7D resume-фичи): 3) Упавший pipeline с лога →
        # «🔄 Продолжить / 🆕 Начать заново».
        # Приоритет ВЫШЕ failed in-memory state: in-session fail после
        # первого incremental dump оставляет валидный лог; resumable CTA
        # полезнее красного «упало» баннера. Failed-ветка остаётся как
        # fallback для early-fail БЕЗ лога (Scriptwriter упал до первого
        # dump → лога нет → resumable вернёт None → доходим до failed).
        try:
            info = self._resumable_from_log(ep_id)
            if info:
                self._montage_cta.show_resumable(
                    info["last_completed_stage"],
                    info.get("next_stage"))
                self._montage_cta.show()
                return
        except Exception:
            traceback.print_exc()
        # 4) Failed snapshot (early-fail без лога или ручной reason).
        if state and state.get('kind') == 'failed':
            try:
                self._montage_cta.show_failed(state.get('reason') or "")
            except Exception:
                traceback.print_exc()
            return
        # 5) Иначе — пусть `_check_montage_ready` (тикает раз в 2с) решит.
        # Скрываем сейчас, чтобы старое состояние от прошлого ep_id не
        # «протекало» в новый.
        try:
            self._montage_cta.hide()
        except Exception:
            pass

    def _restore_gen_buttons_from_history(self, msgs):
        """Прогон всей assistant-истории через `synthesize_gen_markers`
        с фильтром по `refs_decisions`. Кнопка появляется ТОЛЬКО для
        нерешённых пунктов (без skipped/linked записи в decisions).

        Phase 2 hotfix #16 (Bug A): если в фоне идёт `_gen_thread` для
        одного из маркеров — создаём кнопку для него и сразу
        `set_running()`. Так юзер при возврате на эпизод видит «жёлтую»
        генерацию вместо idle."""
        if self._gen_button is not None:
            return  # активная кнопка уже есть — не плодим
        try:
            full_text = "\n".join(
                m.get('text', '') for m in msgs
                if m.get('kind') is None and m.get('text'))
        except Exception:
            return
        if not full_text:
            return
        markers = synthesize_gen_markers(full_text)
        if not markers:
            return
        decisions = self._read_refs_decisions()
        for m in markers:
            d = decisions.get(m.type, {}).get(m.name)
            if isinstance(d, dict):
                dec = d.get('decision')
                if dec == 'skipped':
                    continue  # юзер пометил «не нужен» — карточку не плодим
                if dec == 'linked':
                    # v1.0.58: linked → continue ТОЛЬКО если файл реально
                    # на диске. agent может писать ложные linked для
                    # несуществующих файлов — тогда карточка ДОЛЖНА
                    # появиться. См. фикс в _maybe_show_gen_button.
                    fn = d.get('filename', '') or ''
                    if self._linked_file_exists(m.type, fn, slug=m.name):
                        continue  # реф реально готов — кнопка не нужна
            # 2026-05-07: если за этот маркер сейчас работает тред в
            # глобальном реестре MW — idle-карточку в чате НЕ показываем
            # (running-строка живёт в попапе `ActiveGensPanel`).
            try:
                if self._mw.is_active_gen(self._ep_id, m.type, m.name):
                    continue
            except Exception:
                pass
            # 2026-05-07: для character'а — если юзер уже стартанул
            # генерацию через Актёры (выбрал outfit-вариант → Создать
            # референс), gen-карточку НЕ показываем. Слот освободится
            # либо когда реф залинкуется (decisions.linked) либо когда
            # пользователь отменит всё в Актёрах.
            try:
                if (m.type == 'character'
                        and self._mw.is_active_character_gen(
                            self._ep_id, m.name)):
                    continue
            except Exception:
                pass
            # 2026-05-05: фильтр `_ref_already_exists` (Phase 2 hotfix #17)
            # удалён по запросу юзера. При КАЖДОМ заходе в эпизод/закидке
            # сценария юзер должен видеть карточку выбора для каждого
            # упомянутого рефа — даже если файл уже на диске. UX:
            #   • 🎨 Сгенерировать заново
            #   • 📁 Выбрать существующий (попап покажет уже готовые файлы)
            #   • 🚫 Не нужен
            # Если юзер уже принял решение (linked/skipped) — карточка
            # не показывается (см. блок выше с `decisions`).
            self._maybe_show_gen_button(
                m.type, m.name, m.description,
                display=getattr(m, 'display', '') or '')

    def _ref_already_exists(self, gen_type: str, name: str) -> bool:
        """Phase 2 hotfix #17: реф уже сгенерирован (файл на диске)?
        Проверяем `refs/<type>/<name>.{jpg,jpeg,png,webp}` для location/
        object и `refs/characters/<name>/` для character (папка с хотя
        бы одной картинкой)."""
        try:
            IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
            if gen_type == 'location':
                base = _sa.LOCATIONS_DIR
            elif gen_type == 'object':
                base = _sa.OBJECTS_DIR
            elif gen_type == 'character':
                sub = _sa.CHARACTERS_DIR / name
                if not sub.is_dir():
                    return False
                for p in sub.iterdir():
                    if p.is_file() and p.suffix.lower() in IMG_EXT:
                        return True
                return False
            else:
                return False
            for ext in IMG_EXT:
                if (base / f"{name}{ext}").exists():
                    return True
            return False
        except Exception:
            return False

    def _read_refs_decisions(self) -> dict:
        """Читает `refs_decisions` для текущего эпизода из episodes.json."""
        if not self._ep_id:
            return {}
        path = self._ep_meta_path()
        if path is None or not path.exists():
            return {}
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            ep = data.get(self._ep_id) or {}
            d = ep.get('refs_decisions') if isinstance(ep, dict) else None
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _diag_log_append(self, tag: str, msg: str) -> None:
        """Append diagnostic line to `shows/<active>/_studio_diag.log`.
        Active show нет → fallback на stderr (видно только из терминала).
        Не валит UI — все ошибки задавлены.

        2026-05-11 (v1.0.50): добавлено для debug `_check_montage_ready`
        state transitions. .app запускается кликом по иконке (stderr идёт
        в /dev/null), поэтому существующие `[init]`/`[heal]`/`[set_episode]`
        / `[collision-resolve]` логи через stderr для юзера невидимы.
        Этот helper пишет в файл рядом с эпизодами — переживает рестарт
        Studio и доступен для пересылки в чат при отладке."""
        try:
            from pathlib import Path as _Path
            from datetime import datetime
            cur_show = getattr(self._mw, '_current_show', None)
            proj_root = getattr(self._mw, '_project_root', None)
            if cur_show and proj_root:
                log_path = (_Path(proj_root) / "shows" / cur_show
                            / "_studio_diag.log")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{ts} [{tag}] {msg}\n")
            else:
                import sys as _sys
                _sys.stderr.write(f"[{tag}] {msg}\n")
        except Exception:
            try:
                import sys as _sys
                _sys.stderr.write(f"[{tag}] {msg}\n")
            except Exception:
                pass

    def _render_empty_state(self):
        # Серый хинт по центру
        from html import escape as _esc
        html = (f'<div style="color:#666; font-size:12px; '
                f'padding:20px; text-align:center;">{_esc(tr("chat_empty_hint"))}</div>')
        self.log_view.insertHtml(html)

    def _render_message(self, text: str, kind: Optional[str] = None):
        """Логика как у `NewEpisodeView._append_log`:
          - kind задан → вся фраза одним цветом + inline markdown
          - kind=None  → buffered chunks ассистента, построчная авто-подсветка
        """
        if not text:
            return
        chat_colors = _sa.CHAT_LINE_COLORS
        if kind is not None:
            color = chat_colors.get(kind, chat_colors[None])
            html = _sa.format_chat_inline(text)
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            self.log_view.insertHtml(
                f'<span style="color:{color}; white-space:pre-wrap;">{html}</span>')
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            sb = self.log_view.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())
            return
        self._chunk_buffer += text
        while '\n' in self._chunk_buffer:
            line, _, self._chunk_buffer = self._chunk_buffer.partition('\n')
            self._render_chat_line_local(line + '\n')

    def _render_chat_line_local(self, line: str):
        chat_colors = _sa.CHAT_LINE_COLORS
        line_kind = _sa.detect_line_kind(line)
        color = chat_colors.get(line_kind, chat_colors[None])
        html = _sa.format_chat_inline(line)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertHtml(
            f'<span style="color:{color}; white-space:pre-wrap;">{html}</span>')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        sb = self.log_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _flush_chunk_buffer(self):
        rest = getattr(self, '_chunk_buffer', '')
        if rest:
            self._render_chat_line_local(rest)
            self._chunk_buffer = ''

    def on_external_append(self, ep_id: str, text: str, kind: Optional[str]):
        """Вызывается из MainWindow когда NewEpisodeView (или другой источник)
        записал новую реплику. Если открыт ТОТ ЖЕ эпизод — добавляем в UI.
        Для kind=None (chunks ассистента) парсим GEN-маркеры — кнопки
        генерации появляются и при работе через NewEpisodeView."""
        if ep_id != self._ep_id:
            return
        if kind is None:
            clean_text, _markers = parse_gen_markers(text)
            self._render_message(clean_text, kind)
            # Phase 2 hotfix #26: НЕ создаём GenButton на лету (markers
            # игнорим). Карточки появляются ТОЛЬКО после полного ответа
            # AI — в `_on_done`/`_on_thread_finished` через
            # `try_synthesize_gen_markers`. Так юзер не видит резкого
            # появления кнопок до того как монтажная карта готова.
            self._stream_full += clean_text
        else:
            self._render_message(text, kind)

    def _has_live_thread_for(self, ep_id) -> bool:
        """Есть ли у этого ep_id живой тред — in-chat followup (self._thread,
        он по построению относится к self._ep_id) или external (из реестра
        `_external_threads`, ключ ep_id). 2026-05-11 multi-ep fix."""
        if not ep_id:
            return False
        if ep_id == self._ep_id:
            own = self._thread
            try:
                if own is not None and own.isRunning():
                    return True
            except Exception:
                pass
        ext = self._external_threads.get(ep_id)
        try:
            if ext is not None and ext.isRunning():
                return True
        except Exception:
            pass
        return False

    def _tick_thinking(self):
        # 2026-05-11 multi-ep fix: тикер живёт пока у ТЕКУЩЕГО ep_id есть
        # хоть один живой тред (in-chat followup или external из реестра).
        # Завершение тредов других эпизодов сюда не приходит — у них свои
        # entry в `_external_threads`, finished-хендлер снимает только их.
        if not self._thinking_active or not self._has_live_thread_for(self._ep_id):
            self._thinking_timer.stop()
            self._thinking_active = False
            # Финализируем «висящие» точки: `▶ Думаю·` → `▶ Думаю…`,
            # `▶ Долго думаю... ····` → `▶ Долго думаю...`
            self._finalize_thinking_dots()
            return
        self._thinking_step = (self._thinking_step + 1) % 4
        dots = ["·   ", "··  ", "··· ", "····"][self._thinking_step]
        self.status_lbl.setText(f"{tr('new_ep_log_thinking')} {dots}")
        self._update_thinking_dots(dots)
        self._update_slow_thinking_dots(dots)

    def begin_external_thinking(self, thread, ep_id: Optional[str] = None):
        """Привязать чужой RunEpisodeThread (из NewEpisodeView после
        hand-off в чат) к конкретному ep_id. Тикер запустится, только
        если этот ep_id сейчас открыт в view (self._ep_id). Если юзер
        находится на другом эпизоде — тред просто регистрируется в
        реестре; анимация подхватится, когда юзер вернётся на этот ep.

        2026-05-11 multi-ep fix: per-ep_id реестр + per-instance lambda
        для finished-сигнала, чтобы завершение одного треда не гасило
        тикер для других эпизодов."""
        if thread is None:
            return
        if ep_id is None:
            ep_id = self._ep_id
        if not ep_id:
            return
        self._external_threads[ep_id] = thread
        if self._ep_id == ep_id:
            self._thinking_active = True
            self._thinking_step = 0
            self._thinking_timer.start()
        try:
            # Лямбда замыкает свой ep_id и thread → при срабатывании
            # снимаем только этот entry, остальные эпизоды не страдают.
            thread.finished.connect(
                lambda eid=ep_id, t=thread: self._end_external_thinking(eid, t),
                type=Qt.ConnectionType.QueuedConnection)
        except Exception:
            pass

    def _end_external_thinking(self, ep_id: Optional[str] = None,
                               thread=None):
        """Снять конкретный (ep_id, thread) entry из реестра. Тикер
        останавливается только если у текущего ep_id больше нет живых
        тредов (через `_maybe_stop_thinking`). 2026-05-11 multi-ep fix."""
        if ep_id is not None:
            cur = self._external_threads.get(ep_id)
            if cur is thread or thread is None:
                self._external_threads.pop(ep_id, None)
        self._maybe_stop_thinking()

    def _maybe_stop_thinking(self):
        """Останавливает тикер и финализирует точки только если у
        текущего ep_id больше нет живых тредов. Используется на
        finished/error/stopped — чтобы завершение одного треда не
        гасило анимацию параллельной генерации в том же эпизоде или
        не сбрасывало точки в чужом эпизоде, открытом на экране.
        2026-05-11 multi-ep fix."""
        if self._has_live_thread_for(self._ep_id):
            return
        self._thinking_active = False
        try:
            self._thinking_timer.stop()
        except Exception:
            pass
        try:
            self._finalize_thinking_dots()
        except Exception:
            pass

    def _refresh_thinking_for_current_ep(self):
        """Пересчитать состояние тикера под текущий self._ep_id —
        зовётся из `set_episode` после перерисовки истории. Если у
        нового ep_id есть живой тред → запускаем анимацию (история
        уже содержит маркер `▶ Думаю`, тикер дорисует точки). Иначе —
        стопим (история перерисована начисто, точек нет, финализация
        не нужна). 2026-05-11 multi-ep fix."""
        if self._has_live_thread_for(self._ep_id):
            self._thinking_active = True
            self._thinking_step = 0
            try:
                self._thinking_timer.start()
            except Exception:
                pass
        else:
            self._thinking_active = False
            try:
                self._thinking_timer.stop()
            except Exception:
                pass

    def _update_thinking_in_log(self, dots: str):
        """Совместимость со старым именем. Теперь обе анимации — точки:
        `▶ Думаю ···` и `▶ Долго думаю — это нормально. Не закрывай Studio. ····`."""
        self._update_thinking_dots(dots)
        self._update_slow_thinking_dots(dots)

    def _update_thinking_dots(self, dots: str):
        """Бегущие точки прямо в последней строке log_view с маркером `▶ Думаю`.
        Если после неё уже есть chunk ассистента — финализирует строку до
        просто `▶ Думаю` (без точек, без многоточия) — один раз."""
        base = tr('new_ep_log_thinking')
        marker = f"▶ {base}"
        finalized = marker  # просто `▶ Думаю` без хвоста
        doc = self.log_view.document()
        plain = doc.toPlainText()
        idx = plain.rfind(marker)
        if idx < 0:
            return
        end_idx = plain.find('\n', idx)
        if end_idx < 0:
            end_idx = len(plain)
        if plain[end_idx:].strip():
            current = plain[idx:end_idx]
            if current == finalized:
                return  # уже финализировано
            cursor = QTextCursor(doc)
            cursor.setPosition(idx)
            cursor.setPosition(end_idx, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            cursor.removeSelectedText()
            cursor.insertText(finalized, fmt)
            return
        # Обычная анимация
        cursor = QTextCursor(doc)
        cursor.setPosition(idx)
        cursor.setPosition(end_idx, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat()
        cursor.removeSelectedText()
        cursor.insertText(f"{marker} {dots}", fmt)

    def _update_slow_thinking_dots(self, dots: str):
        """Бегущие точки в КОНЦЕ строки `▶ Долго думаю — это нормально...
        Не закрывай Studio.` Каждые 400мс хвост обновляется на ` ····` /
        ` ··· ` / ` ··  ` / ` ·   ` (как у `▶ Думаю`)."""
        base = tr('new_ep_log_thinking_long')
        if not base:
            return
        doc = self.log_view.document()
        plain = doc.toPlainText()
        idx = plain.rfind(base)
        if idx < 0:
            return
        line_end = plain.find('\n', idx)
        if line_end < 0:
            line_end = len(plain)
        if plain[line_end:].strip():
            return  # после строки уже chunk — анимация замораживается
        cursor = QTextCursor(doc)
        cursor.setPosition(idx)
        cursor.setPosition(line_end, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat()
        cursor.removeSelectedText()
        cursor.insertText(f"{base} {dots}", fmt)

    def _finalize_thinking_dots(self):
        """При остановке тикера снять «висящие» точки: `▶ Думаю·   ` →
        `▶ Думаю` (просто маркер без точек); `▶ Долго думаю... ····` →
        `▶ Долго думаю...` (без хвоста)."""
        doc = self.log_view.document()
        # 1. Думаю
        base = tr('new_ep_log_thinking')
        marker = f"▶ {base}"
        finalized = marker  # просто `▶ Думаю` без многоточия
        plain = doc.toPlainText()
        idx = plain.rfind(marker)
        if idx >= 0:
            end_idx = plain.find('\n', idx)
            if end_idx < 0:
                end_idx = len(plain)
            current = plain[idx:end_idx]
            if current != finalized:
                cursor = QTextCursor(doc)
                cursor.setPosition(idx)
                cursor.setPosition(end_idx, QTextCursor.MoveMode.KeepAnchor)
                fmt = cursor.charFormat()
                cursor.removeSelectedText()
                cursor.insertText(finalized, fmt)
        # 2. Долго думаю — снять хвост точек до базовой строки
        long_base = tr('new_ep_log_thinking_long')
        if long_base:
            plain = doc.toPlainText()
            idx2 = plain.rfind(long_base)
            if idx2 >= 0:
                line_end = plain.find('\n', idx2)
                if line_end < 0:
                    line_end = len(plain)
                current = plain[idx2:line_end]
                if current != long_base:
                    cursor = QTextCursor(doc)
                    cursor.setPosition(idx2)
                    cursor.setPosition(line_end,
                                       QTextCursor.MoveMode.KeepAnchor)
                    fmt = cursor.charFormat()
                    cursor.removeSelectedText()
                    cursor.insertText(long_base, fmt)

    def _on_send(self):
        if self._thread is not None and self._thread.isRunning():
            return
        if not self._ep_id:
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        # Phase 2 hotfix #8: сбрасываем накопитель fallback-парсера
        # перед каждым новым запросом — чтобы синтез шёл по СВЕЖЕМУ ответу.
        self._stream_full = ''
        # Записываем в файл + рисуем в UI
        user_line = f"\n💬 {tr('new_ep_you_label')}: {text}\n\n"
        _sa.append_chat_message(self._ep_id, "user", user_line, kind='user')
        self._render_message(user_line, kind='user')
        # 2026-05-08: без `…` и без пустой строки после — сразу появляется
        # `▶ Думаю`, тикер заменит на `▶ Думаю · / ·· / ··· / ····`,
        # при первом chunk финализируется обратно на `▶ Думаю` без точек.
        # Одна `\n` чтобы slow_thinking встал ровно следующей строкой.
        thinking_line = f"▶ {tr('new_ep_log_thinking')}\n"
        _sa.append_chat_message(self._ep_id, "system", thinking_line, kind='system')
        self._render_message(thinking_line, kind='system')

        self.input_edit.clear()
        self.send_btn.setEnabled(False)
        self._thinking_step = 0
        self._thinking_active = True
        self._thinking_timer.start()
        self.status_lbl.setStyleSheet("color:#ffaa44; font-size:12px;")
        self.status_lbl.setText(tr('new_ep_log_thinking'))

        # Модель из СВОЕГО дропдауна (юзер мог переключить прямо в чате
        # эпизода — например на Opus для сложной задачи или Haiku для простого
        # вопроса). Значение сохраняется в QSettings, разделено с NewEpisodeView.
        model_id = self._current_model()
        self._thread = RunEpisodeThread(
            self._mw._project_root, text,
            continue_session=True, model=model_id)
        self._thread.output_chunk.connect(self._on_chunk)
        self._thread.finished_ok.connect(self._on_done)
        self._thread.error.connect(self._on_error)
        self._thread.stopped.connect(self._on_stopped)
        self._thread.slow_thinking.connect(self._on_slow_thinking)
        self._thread.start()

    def _on_slow_thinking(self):
        """Сигнал от RunEpisodeThread через 120с без первого chunk —
        пишем системную подсказку в чат, чтобы юзер не подумал что
        Studio зависла. Строка появляется сразу под `▶ Думаю` (без
        пустой строки между), потом тикер начнёт анимировать хвост точек."""
        if not self._ep_id:
            return
        line = f"{tr('new_ep_log_thinking_long')}\n\n"
        try:
            _sa.append_chat_message(self._ep_id, "system", line, kind='system')
        except Exception:
            pass
        self._render_message(line, kind='system')

    def _on_chunk(self, text: str):
        if not self._ep_id:
            return
        # Извлекаем GEN-маркеры до рендера: маркеры скрываем из лога.
        # Phase 2 hotfix #26: live-маркеры НЕ создают GenButton на лету —
        # карточки появятся только в `_on_done` через
        # `try_synthesize_gen_markers`. Юзер не должен видеть кнопки
        # до того как AI закончит писать монтажную карту.
        clean_text, _markers = parse_gen_markers(text)
        _sa.append_chat_message(self._ep_id, "assistant", clean_text, kind=None)
        self._render_message(clean_text, kind=None)
        self._stream_full += clean_text

    # ── Sub-MVP: автономная генерация по кнопке в чате ──────────────

    def try_synthesize_gen_markers(self, ep_id: str, full_text: str) -> int:
        """Phase 2 hotfix #8: fallback на случай когда AI не вставил
        `[[GEN:...]]` маркеры в свой ответ (а написал просто словами
        «- ✗ name — рефа нет»). Сканируем полный текст, парсим строки
        по секциям ЛОКАЦИИ/ОБЪЕКТЫ и создаём кнопки. Sub-MVP правило
        «одна кнопка за раз» соблюдается через `_maybe_show_gen_button`.

        Вызывается из:
          1) `_on_done` — после завершения followup'а в этом же view.
          2) `NewEpisodeView._on_thread_finished` после hand-off — когда
             первый запуск завершился, и юзер уже здесь.

        Возвращает количество созданных кнопок (0 если уже была
        активная кнопка или AI всё-таки вставил маркеры/ничего не
        нашлось)."""
        if ep_id != self._ep_id:
            return 0
        if self._gen_button is not None:
            return 0  # активная кнопка уже есть — sub-MVP не плодим
        markers = synthesize_gen_markers(full_text)
        if not markers:
            return 0
        before_count = 1 if self._gen_button is not None else 0
        for m in markers:
            self._maybe_show_gen_button(
                m.type, m.name, m.description,
                display=getattr(m, 'display', '') or '')
        return 1 if (self._gen_button is not None and not before_count) else 0

    def _maybe_show_gen_button(self, gen_type: str, name: str,
                                description: str, display: str = ""):
        """Показывает GenButton для нового маркера. Sub-MVP логика:
        ПЕРВЫЙ полученный маркер выигрывает — создаётся кнопка для него.
        Все последующие маркеры в этом сообщении/чате — запоминаются в
        `_gen_seen_names` (чтобы не дублировались при рестриминге), но
        НЕ заменяют существующую кнопку. Иначе AI выводит несколько
        строк подряд и финальная заменяет первую → юзер видит вместо
        ожидаемой первой локации последний попавшийся объект.

        Phase 2 заменит это полноценной очередью."""
        if not name or name in self._gen_seen_names:
            return
        # 2026-05-07: если для этого эпизода (type, name) уже бежит
        # параллельная генерация на уровне MW — idle-карточку не
        # создаём (UX: running живёт в попапе, не в чате).
        try:
            if self._mw.is_active_gen(self._ep_id, gen_type, name):
                self._gen_seen_names.add(name)
                return
        except Exception:
            pass
        # 2026-05-07: для character — проверяем активный outfit picker
        # текущего эпизода. Если для этого имени picker уже стоит — НЕ
        # плодим idle GenButton (иначе при возврате на эпизод появлялась
        # карточка-дубль ниже picker'а).
        if gen_type == 'character':
            try:
                cur_ep = self._ep_id or ""
                target = self._outfit_target_names.get(cur_ep, "")
                picker = self._outfit_pickers.get(cur_ep)
                if name == target and picker is not None:
                    self._gen_seen_names.add(name)
                    return
            except Exception:
                pass
        # Запоминаем имя ДАЖЕ если кнопка уже занята — чтобы при
        # повторных chunks (стрим) не пытаться создать ту же.
        self._gen_seen_names.add(name)
        # Phase 2 hotfix #24: автоматически создаём папку character'а
        # при первом упоминании в чате. Юзер хочет чтобы папки появлялись
        # сами при анализе сценария — без необходимости заходить во
        # вкладку Актёры.
        if gen_type == 'character':
            try:
                cur_show = getattr(self._mw, '_current_show', None)
                if cur_show:
                    char_dir = (self._mw._project_root / "shows" / cur_show
                                / "refs" / "characters" / name)
                    char_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        # 2026-05-07: Снят «жёсткий фильтр» от 2026-05-06 который скрывал
        # карточки для уже-linked/skipped рефов. Юзер хочет видеть ВСЕ
        # карточки чтобы можно было «↶ Передумал» и переназначить реф,
        # либо сгенерировать новый поверх старого. Старая логика (фильтр
        # return) ломала workflow: юзер не видел карточек ВООБЩЕ если все
        # рефы уже на диске → не появлялась CTA «Все рефы готовы» (потому
        # что _gen_button оставалась None но и unresolved оставались
        # пустыми по факту, при этом карточки не были показаны юзеру).
        #
        # Теперь:
        # • Карточка СОЗДАЁТСЯ для каждого маркера.
        # • Если есть pre-decision (linked/skipped) — карточка
        #   инициализируется в этом state (показывает filename + кнопку
        #   «↶ Передумал»). НЕ занимает слот active `_gen_button` — это
        #   карточка-история, sub-MVP очередь не блокирует.
        # • Если decision нет (idle) — карточка занимает active слот
        #   (sub-MVP: только одна idle активна за раз). Остальные idle
        #   ждут в `_pending_markers`.
        pre_decision = None
        pre_filename = ""
        try:
            d = self._read_refs_decisions().get(gen_type, {}).get(name)
            if isinstance(d, dict):
                dec = d.get('decision')
                if dec in ('linked', 'skipped'):
                    pre_decision = dec
                    pre_filename = d.get('filename', '') or ''
                    # v1.0.58: disk-check для linked-decisions. agent
                    # может писать ложные linked для несуществующих
                    # файлов в episodes.json через Bash tool. Если файла
                    # нет на диске — игнорируем decision, карточка
                    # станет active idle с кнопкой «Сгенерировать».
                    # Симметрично с `list_episode_refs` (окно РЕФЕРЕНСЫ)
                    # и `_linked_file_exists` (CTA-готовность) — везде
                    # один критерий: linked AND файл реально на диске.
                    if dec == 'linked' and not self._linked_file_exists(
                            gen_type, pre_filename, slug=name):
                        pre_decision = None
                        pre_filename = ""
        except Exception:
            pass

        # Sub-MVP guard ТОЛЬКО для idle-карточек: если active idle уже
        # есть — новая idle уходит в очередь. Pre-decided карточки
        # рендерятся всегда (не блокируются sub-MVP).
        # 2026-05-09: outfit picker для current ep тоже занимает slot.
        # Без этого следующий character/object/location появлялся пока
        # юзер ещё не выбрал вариант одежды для предыдущего character'а
        # (lora всплывала когда picker для david ещё активен → David's
        # picker визуально схлопывался вверх). После того как юзер создаст
        # реф для david в Actors, `_purge_resolved_markers` уберёт picker
        # и вызовет `_advance_gen_queue` — lora pop'нется из очереди.
        slot_busy = self._gen_button is not None
        if not slot_busy:
            try:
                cur_ep = self._ep_id or ""
                if self._outfit_pickers.get(cur_ep) is not None:
                    slot_busy = True
            except Exception:
                pass
        if pre_decision is None and slot_busy:
            self._pending_markers.append(
                (gen_type, name, description, display))
            return

        btn = GenButton(gen_type, name, description, parent=self,
                        display_name=display)
        btn.generate_requested.connect(self._on_gen_button_clicked)
        btn.open_refs_requested.connect(self._on_gen_open_refs)
        btn.retry_requested.connect(self._on_gen_retry)
        # Долг 13 (2026-05-04 hotfix #10): три кнопки выбора в idle.
        btn.skip_requested.connect(self._on_gen_skip)
        btn.use_existing_requested.connect(self._on_gen_use_existing)
        btn.undo_requested.connect(self._on_gen_undo)
        self._gen_layout.addWidget(btn)

        if pre_decision == 'linked':
            # Карточка-история в state «✓ привязан». Не занимает
            # `_gen_button` — sub-MVP не блокирует.
            try:
                btn.set_linked(pre_filename)
            except Exception:
                traceback.print_exc()
        elif pre_decision == 'skipped':
            try:
                btn.set_skipped()
            except Exception:
                traceback.print_exc()
        else:
            # idle — это active карточка, занимает слот.
            self._gen_button = btn
        # Phase 2 hotfix #26: плавный fade-in карточки чтобы не бить
        # резко в глаза. Использует существующую `_sa.fade_in` —
        # длительность анимации регулируется юзером в Settings через
        # `anim_speed_multiplier`.
        try:
            from PyQt6.QtCore import QPropertyAnimation
            btn._fade_anim = QPropertyAnimation(btn)
            _sa.fade_in(btn, btn._fade_anim)
        except Exception:
            pass

    def _clear_gen_button(self):
        if self._gen_button is not None:
            # 2026-05-07: если эта GenButton — source-кнопка активного
            # outfit picker'а (любого эпизода), НЕ удаляем виджет — он
            # нужен для restore_source при cancel'е picker'а. Просто
            # отвязываем указатель `self._gen_button` (слот свободен,
            # widget живёт в `_outfit_source_btns[ep_id]`, скрыт).
            try:
                if self._gen_button in self._outfit_source_btns.values():
                    self._gen_button = None
                    return
            except Exception:
                traceback.print_exc()
            try:
                self._gen_button.setParent(None)
                self._gen_button.deleteLater()
            except Exception:
                pass
            self._gen_button = None

    def reset_state(self):
        """Полный сброс состояния gen-кнопок + outfit picker'а. Вызывается
        из MainWindow после удаления эпизода — чтобы при пересоздании
        эпизода с тем же ep_id юзер увидел все карточки заново
        (а не старый `_gen_seen_names` от прошлого запуска)."""
        # 2026-05-07: per-episode outfit picker'ы. reset_state зовётся
        # при удалении эпизода (storyboard_app.py:_on_delete_episode_clicked)
        # для текущего эпизода — чистим только его state. Picker'ы
        # других эпизодов продолжают жить в `_gen_layout`.
        ep_id = self._ep_id or ""
        try:
            t = self._outfit_threads.get(ep_id)
            if t is not None and t.isRunning():
                t.stop()
        except Exception:
            pass
        self._outfit_threads.pop(ep_id, None)
        try:
            picker = self._outfit_pickers.pop(ep_id, None)
            if picker is not None:
                picker.setParent(None)
                picker.deleteLater()
        except Exception:
            pass
        self._outfit_target_names.pop(ep_id, None)
        self._outfit_target_displays.pop(ep_id, None)
        self._outfit_source_btns.pop(ep_id, None)
        self._outfit_descriptions.pop(ep_id, None)
        # 2026-05-07: глобальные параллельные генерации (MW._active_gens)
        # тут НЕ трогаем — они живут на уровне MW и переживают удаление
        # эпизода (юзер увидит финиш через попап). Если юзер удалит
        # эпизод физически (с диском) — refs всё равно сгенерятся в свою
        # папку, попап покажет ✓ done и сам уберёт строку.
        # Очистка карточек и очереди
        self._clear_gen_button()
        self._gen_seen_names.clear()
        self._pending_markers.clear()
        # Накопитель stream'а сбрасываем — fallback парсер пойдёт по
        # свежему ответу.
        self._stream_full = ''
        # Если в layout остались «осиротевшие» GenButton-карточки от
        # предыдущих маркеров (например done/skipped/linked) — удаляем.
        try:
            from widgets import GenButton
            from widgets.character_outfit_picker import CharacterOutfitPicker
            for i in reversed(range(self._gen_layout.count())):
                item = self._gen_layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, (GenButton, CharacterOutfitPicker)):
                    self._gen_layout.removeWidget(w)
                    w.setParent(None)
                    w.deleteLater()
        except Exception:
            traceback.print_exc()

    def _on_gen_button_clicked(self, gen_type: str, name: str,
                                description: str):
        """Старт автономной генерации в фоне.

        Долг 13 (2026-05-05): для `gen_type == 'character'` НЕ запускаем
        AutonomousGenThread (он не поддерживает character) — вместо
        этого подставляем CharacterOutfitPicker и зовём SuggestOutfitsThread
        чтобы AI предложил 3 варианта одежды. После клика по варианту
        переключаем на вкладку Актёры с предзаполненным описанием.

        2026-05-10 (БАГ 2 fix): если character marker по эвристике
        выглядит как ЖИВОТНОЕ (бульдог, кот, лошадь и т.п.) — НЕ
        показываем outfit picker (одежда для собаки бессмысленна),
        а перенаправляем на object-flow (стандартный
        AutonomousGenThread с gen_type='object'). См. _ANIMAL_KEYWORDS."""
        if gen_type == 'character':
            if self._is_likely_animal(name, description):
                self._append_animal_redirect_message(name)
                gen_type = 'object'
                # Fall through: запустим стандартный AutonomousGenThread
                # с gen_type='object'. Slug-collision detection
                # сработает на refs/objects/ автоматически.
            else:
                # 2026-05-10 (БАГ 4 fix): пробрасываем description из
                # манифеста агента (rich per-scene outfit notes) в
                # outfit picker. Без него SuggestOutfitsThread получал
                # только raw scenarios/<ep>.txt и игнорировал то что
                # агент написал в чате про этого character'а.
                self._start_outfit_picker(name, description)
                return
        # 2026-05-07: глобальный реестр в MW. Повторный клик по уже-
        # бегущему маркеру игнорируем.
        try:
            if self._mw.is_active_gen(self._ep_id, gen_type, name):
                return
        except Exception:
            pass
        if self._gen_button is None:
            return
        # Локально запоминаем активную idle-карточку — её удалим из
        # layout сразу после старта треда. Прогресс/финиш будут видны
        # в попапе MW.
        clicked_card = self._gen_button
        try:
            cur_show = _sa.get_current_show(self._mw._project_root)
        except Exception:
            cur_show = None
        # 2026-05-10: Если slug уже существует в refs/<sub>/ (другой
        # эпизод сгенерировал ref с тем же именем) — переименовываем
        # в `<name>_2` / `<name>_3` и т.д. Без этого pipeline.py с
        # `--force` перезаписал бы старый файл, ломая чужой эпизод.
        # Сейчас pipeline.py запускается БЕЗ `--force` (см.
        # threads/autonomous_gen.py) — это safety net на случай если
        # уникализация даст сбой. См. `_resolve_collision_free_slug`.
        if gen_type in ('location', 'object') and cur_show:
            try:
                name = self._resolve_collision_free_slug(
                    cur_show, gen_type, name)
            except Exception:
                traceback.print_exc()
        thread = AutonomousGenThread(
            self._mw._project_root, gen_type, name, description,
            ep_id=self._ep_id, show_slug=cur_show,
            model="claude-opus-4-7")  # 2026-05-09: agentic flow с WebSearch + visual + geometry — Opus.
        thread.start()
        # Регистрация в MW — она сама подключит сигналы прогресса/
        # финиша/ошибки к попапу.
        try:
            self._mw.register_active_gen(
                self._ep_id, gen_type, name, description, thread)
        except Exception:
            traceback.print_exc()
        # Карточка из чата исчезает (live в попапе теперь).
        try:
            clicked_card.setParent(None)
            clicked_card.deleteLater()
        except Exception:
            traceback.print_exc()
        self._gen_button = None
        # Следующая idle из очереди — появляется сразу.
        self._advance_gen_queue()

    def _is_likely_animal(self, name: str, description: str) -> bool:
        """Эвристика для БАГ 2 fix: character-маркер — на самом деле
        животное? Проверяем slug + описание против `_ANIMAL_KEYWORDS`.
        Если хотя бы одно ключевое слово найдено (substring match,
        case-insensitive) — True. Это safety net на случай если агент
        не послушал PRODUCER_INSTRUCTIONS правило «животные → ОБЪЕКТЫ».
        """
        blob = f"{name} {description or ''}".lower()
        return any(kw in blob for kw in _ANIMAL_KEYWORDS)

    def _append_animal_redirect_message(self, name: str) -> None:
        """System-сообщение в чат когда character-маркер
        ре-классифицирован как animal/object."""
        try:
            line = (
                f"ℹ `{name}` похоже на животное — генерирую через "
                f"object-flow (без выбора одежды). Если это всё-таки "
                f"человек — поправь манифест агента вручную.\n")
            _sa.append_chat_message(
                self._ep_id, "system", line, kind='system')
            self._render_message(line, kind='system')
        except Exception:
            traceback.print_exc()

    def _resolve_collision_free_slug(self, cur_show: str, gen_type: str,
                                     name: str) -> str:
        """Если `refs/<sub>/<name>.<ext>` уже существует — возвращает
        новый slug `<name>_2` / `<name>_3` / ... (первый свободный).
        Параллельно обновляет `episodes.json[ep_id].refs.<sub>`:
        заменяет старое имя файла на новое, чтобы манифест эпизода
        указывал на ещё-не-сгенерированный новый файл (а не на чужой
        старый). И эмитит system-сообщение в чат — юзер видит что
        слаг переименован.
        Если коллизии нет — возвращает name как есть, ничего не пишет.
        2026-05-10 — фикс БАГ 1 (slug-коллизия между эпизодами).
        """
        sub = 'locations' if gen_type == 'location' else 'objects'
        refs_dir = (self._mw._project_root / "shows" / cur_show
                    / "refs" / sub)
        if not refs_dir.exists():
            return name
        if not list(refs_dir.glob(f"{name}.*")):
            return name
        # Коллизия — ищем первый свободный суффикс.
        suffix = 2
        while list(refs_dir.glob(f"{name}_{suffix}.*")):
            suffix += 1
        new_name = f"{name}_{suffix}"
        # Обновляем episodes.json[ep_id].refs.<sub>: заменяем
        # «<name>.<ext>» на «<new_name>.<ext>». Без этого манифест
        # текущего эпизода продолжит указывать на чужой файл.
        try:
            import json as _json
            ep_meta_path = (self._mw._project_root / "shows" / cur_show
                            / "episodes.json")
            if ep_meta_path.exists() and self._ep_id:
                meta = _json.loads(
                    ep_meta_path.read_text(encoding='utf-8')) or {}
                ep_obj = meta.get(self._ep_id) or {}
                refs = ep_obj.get("refs") or {}
                arr = refs.get(sub) or []
                for i, item in enumerate(arr):
                    if not isinstance(item, str):
                        continue
                    if item == name:
                        arr[i] = new_name
                    elif "." in item and item.rsplit(".", 1)[0] == name:
                        ext = item.rsplit(".", 1)[1]
                        arr[i] = f"{new_name}.{ext}"
                refs[sub] = arr
                ep_obj["refs"] = refs
                # 2026-05-11 (v1.0.46): cleanup устаревшего refs_decisions
                # entry под старым slug. Иначе после регенерации decisions
                # продолжит указывать на чужой файл (от другого эпизода
                # с тем же первоначальным slug). Новая запись будет добавлена
                # позже в `_on_active_gen_finished` под new_name.
                sub_singular = ('location' if gen_type == 'location'
                                else 'object')
                decisions_block = ep_obj.get("refs_decisions") or {}
                bucket = decisions_block.get(sub_singular) or {}
                if name in bucket:
                    old_entry = bucket.pop(name)
                    if not bucket:
                        decisions_block.pop(sub_singular, None)
                    if not decisions_block:
                        ep_obj.pop("refs_decisions", None)
                    else:
                        ep_obj["refs_decisions"] = decisions_block
                    try:
                        import sys as _sys
                        _sys.stderr.write(
                            f"[collision-resolve] {self._ep_id}/"
                            f"{sub_singular}/{name} → {new_name}: removed "
                            f"stale decision {old_entry!r}\n")
                    except Exception:
                        pass
                meta[self._ep_id] = ep_obj
                ep_meta_path.write_text(
                    _json.dumps(meta, ensure_ascii=False, indent=2)
                    + "\n", encoding='utf-8')
        except Exception:
            traceback.print_exc()
        # System-сообщение в чат — ДО старта генерации (юзер видит
        # ясный «создаю новый вариант» вместо постфактумного
        # «уже занят, переименовал»).
        try:
            line = (
                f"🆕 Создаю новый вариант `{new_name}.jpg`. "
                f"`{name}.jpg` уже существует в библиотеке сериала "
                f"от предыдущей генерации — оставляю его без изменений. "
                f"Если хотел переиспользовать существующий — отмени и "
                f"нажми «📁 Выбрать существующий» на этой же карточке.\n")
            _sa.append_chat_message(
                self._ep_id, "system", line, kind='system')
            self._render_message(line, kind='system')
        except Exception:
            traceback.print_exc()
        return new_name

    # 2026-05-07: слоты `_on_gen_progress / image_ready / finished /
    # error` переехали в MainWindow (`_on_active_gen_*`). EpisodeChatView
    # больше не хранит ссылку на тред — он живёт в MW._active_gens, а
    # прогресс/финиш отображаются в попапе ActiveGensPanel.

    def _resolve_generated_filename(self, gen_type: str, name: str,
                                     hint_path: str) -> str:
        """Находит фактическое имя файла рефа после автономной генерации.
        Сначала пробует hint_path (то что сообщил тред), потом сканирует
        стандартные расширения в `refs/<type>/`."""
        try:
            if hint_path and '.' in hint_path:
                cand_name = hint_path.split('/')[-1]
                if cand_name and not cand_name.endswith('/'):
                    return cand_name
            try:
                cur_show = _sa.get_current_show(self._mw._project_root)
            except Exception:
                cur_show = None
            if not cur_show:
                return f"{name}.jpg"
            sub = {'location': 'locations', 'object': 'objects'}.get(
                gen_type, gen_type + 's')
            base = (self._mw._project_root / "shows" / cur_show
                    / "refs" / sub)
            for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                if (base / f"{name}{ext}").exists():
                    return f"{name}{ext}"
            return f"{name}.jpg"
        except Exception:
            return f"{name}.jpg"

    # `_on_gen_error` тоже переехал в MainWindow (`_on_active_gen_error`).

    def _on_gen_open_refs(self):
        """Клик «✓ Открыть в РЕФЕРЕНСАХ» — переключает Editor → REFS view
        и убирает done-карточку из чата (Phase 2 hotfix #16, Bug C).
        Юзер уже увидел реф — карточка отработала, в чате не нужна."""
        sender = self.sender()
        try:
            mw = self._mw
            if hasattr(mw, '_show_refs_view'):
                mw._show_refs_view()
        except Exception:
            pass
        # Удаляем родительскую GenButton-карточку. sender — это open_btn,
        # его parent — GenButton.
        try:
            card = sender
            while card is not None and not isinstance(card, GenButton):
                card = card.parent()
            if isinstance(card, GenButton):
                card.setParent(None)
                card.deleteLater()
        except Exception:
            pass

    def _on_gen_retry(self):
        if self._gen_button is None:
            return
        # Сброс кнопки в idle и повторный клик через слот
        self._gen_button.reset_to_idle()

    # ── Долг 13 (2026-05-05): 3 варианта одежды для character ─────────

    def _start_outfit_picker(self, character_name: str,
                             description: str = ""):
        """Запускает SuggestOutfitsThread и подставляет CharacterOutfitPicker
        под текущей GenButton-карточкой персонажа. Если уже есть активный
        пикер для ТЕКУЩЕГО эпизода — не дублируем (2026-05-07: per-episode).

        2026-05-10 (БАГ 4 fix): `description` — текст из манифеста
        агента в чате (rich per-scene outfit notes). Сохраняется в
        `self._outfit_descriptions[ep_id]` чтобы при retry
        («Ещё 3 варианта») передаваться повторно в SuggestOutfitsThread."""
        ep_id = self._ep_id or ""
        if not ep_id:
            return
        # Если для этого эпизода уже идёт запрос — игнорируем повторный клик.
        existing_thread = self._outfit_threads.get(ep_id)
        if existing_thread is not None and existing_thread.isRunning():
            return
        # Сохраняем description для текущего outfit picker'а (для
        # _launch_outfit_thread + retry).
        self._outfit_descriptions[ep_id] = description or ""
        # Скрываем существующий GenButton (он отработал свою задачу).
        # 2026-05-10 (revert после 1ef1976): source_btn.hide() — как
        # было до попытки «оставить pick_btn видимым». Pick existing
        # теперь живёт ВНУТРИ picker'а в bottom row — `source_btn` не
        # нужен пока picker открыт.
        source_btn = self._gen_button
        # Запоминаем display-name из source кнопки чтобы передать его
        # в баннер «Актёров» (юзеру важно видеть «Муж», а не «muzh»).
        display_for_banner = ""
        if source_btn is not None:
            try:
                source_btn.hide()
                display_for_banner = getattr(
                    source_btn, '_display', '') or ''
            except Exception:
                pass
        self._outfit_target_displays[ep_id] = display_for_banner
        if source_btn is not None:
            self._outfit_source_btns[ep_id] = source_btn
        # Создаём пикер с человекочитаемым display (если есть).
        picker_label = (
            f"{character_name} ({display_for_banner})"
            if display_for_banner else character_name)
        picker = CharacterOutfitPicker(picker_label, parent=self)
        # 2026-05-07: атрибут привязки к эпизоду — при переключении
        # `set_episode` скрывает picker'ы других эпизодов.
        picker._associated_ep_id = ep_id
        picker.variant_chosen.connect(self._on_outfit_variant_chosen)
        picker.retry_requested.connect(self._on_outfit_retry)
        picker.custom_requested.connect(self._on_outfit_custom)
        picker.pick_existing_requested.connect(
            self._on_outfit_pick_existing)
        self._gen_layout.addWidget(picker)
        self._outfit_pickers[ep_id] = picker
        self._outfit_target_names[ep_id] = character_name
        try:
            from PyQt6.QtCore import QPropertyAnimation
            picker._fade_anim = QPropertyAnimation(picker)
            _sa.fade_in(picker, picker._fade_anim)
        except Exception:
            pass
        self._launch_outfit_thread(character_name)

    def _launch_outfit_thread(self, character_name: str):
        """Стартует SuggestOutfitsThread для персонажа. Используется и
        при первом запросе, и при retry (Ещё 3 варианта)."""
        ep_id = self._ep_id or ""
        if not ep_id:
            return
        picker = self._outfit_pickers.get(ep_id)
        if picker is None:
            return
        try:
            cur_show = _sa.get_current_show(self._mw._project_root)
        except Exception:
            cur_show = None
        picker.set_loading()
        # 2026-05-07: накопленные ранее показанные варианты для этого ep
        # (включая первый запуск — там пусто). На retry передаём список
        # чтобы AI выдал кардинально другие.
        prev = list(self._outfit_seen_variants.get(ep_id, []))
        # 2026-05-10 (БАГ 4 fix): description от агента из манифеста
        # сохранён в `_start_outfit_picker`, читаем здесь и передаём
        # в SuggestOutfitsThread (включая retry-вызовы).
        chat_desc = self._outfit_descriptions.get(ep_id, "") or ""
        thread = SuggestOutfitsThread(
            self._mw._project_root,
            character_name,
            ep_id=ep_id,
            show_slug=cur_show,
            model="claude-sonnet-4-6",  # 2026-05-09: структурный outfit picker — Sonnet справляется.
            previous_variants=prev,
            chat_description=chat_desc)
        thread.results.connect(self._on_outfit_results)
        thread.error.connect(self._on_outfit_error)
        self._outfit_threads[ep_id] = thread
        thread.start()

    def _refresh_outfit_pickers_visibility(self):
        """2026-05-07: при смене эпизода скрываем все outfit picker'ы
        чьё `_associated_ep_id` не совпадает с текущим, и показываем
        picker для текущего ep_id (если есть). SuggestOutfitsThread'ы
        других эпизодов продолжают идти в фоне — результаты придут в
        свои picker'ы (которые сейчас скрыты, но live в layout)."""
        cur_ep = self._ep_id or ""
        for ep_id, picker in list(self._outfit_pickers.items()):
            try:
                if picker is None:
                    continue
                picker.setVisible(ep_id == cur_ep)
            except Exception:
                traceback.print_exc()
        # Source-кнопки picker'ов других эпизодов — тоже скрываем.
        # (Они hide'нуты при `_start_outfit_picker` — но если это был
        # source для другого эпизода, при show() в этом эпизоде он бы
        # нарисовался. Проверяем явно.)
        for ep_id, src_btn in list(self._outfit_source_btns.items()):
            try:
                if src_btn is None:
                    continue
                if ep_id == cur_ep:
                    # Source-кнопка остаётся скрытой пока picker activный
                    # — picker сам выше неё. Не трогаем.
                    pass
                else:
                    src_btn.setVisible(False)
            except Exception:
                traceback.print_exc()

    def _outfit_ep_for_sender(self) -> Optional[str]:
        """2026-05-07: ищет ep_id в `_outfit_threads` по `self.sender()`.
        Используется в слотах results/error чтобы знать какому эпизоду
        принадлежит пришедший результат."""
        try:
            sender = self.sender()
            if sender is None:
                return None
            for k, t in self._outfit_threads.items():
                if t is sender:
                    return k
        except Exception:
            traceback.print_exc()
        return None

    def _on_outfit_results(self, variants: list):
        ep_id = self._outfit_ep_for_sender()
        if ep_id is None:
            return
        picker = self._outfit_pickers.get(ep_id)
        if picker is None:
            self._outfit_threads.pop(ep_id, None)
            return
        try:
            picker.set_variants(list(variants))
        except Exception:
            traceback.print_exc()
        # 2026-05-07: накапливаем все показанные варианты — следующий
        # retry передаст их в `previous_variants`.
        try:
            seen = self._outfit_seen_variants.setdefault(ep_id, [])
            for v in variants or []:
                if v and v not in seen:
                    seen.append(v)
        except Exception:
            traceback.print_exc()
        self._outfit_threads.pop(ep_id, None)

    def _on_outfit_error(self, msg: str):
        ep_id = self._outfit_ep_for_sender()
        if ep_id is None:
            return
        picker = self._outfit_pickers.get(ep_id)
        if picker is None:
            self._outfit_threads.pop(ep_id, None)
            return
        try:
            picker.set_error(msg)
        except Exception:
            traceback.print_exc()
        self._outfit_threads.pop(ep_id, None)

    def _on_outfit_retry(self):
        # Retry-клик идёт от picker'а текущего эпизода.
        ep_id = self._ep_id or ""
        target = self._outfit_target_names.get(ep_id, "")
        if not target:
            return
        existing = self._outfit_threads.get(ep_id)
        if existing is not None and existing.isRunning():
            return
        self._launch_outfit_thread(target)

    def _on_outfit_custom(self):
        """«✎ Придумаю описание сам» — то же что variant_chosen, но с
        пустым описанием. Юзер заполнит в попапе создания референса."""
        self._on_outfit_variant_chosen("")

    def _on_outfit_pick_existing(self):
        """2026-05-10: клик «📁 Pick existing» внутри picker'а bottom row.

        Открывает RefPickerDialog для выбора готового рефа. На:
          • Accepted с picked_name → cleanup picker'а, stop thread'а,
            save decision, delete source_btn, advance queue.
          • Rejected (Cancel/Close в попапе) или Accepted с пустым
            picked_name → pure NO-OP: picker остаётся на месте, thread
            продолжает работать, sender_card не удаляется, очередь
            не двигается. Юзер передумал — UI как был.

        2026-05-10 fixup: cleanup перенесён ПОСЛЕ Accepted check —
        раньше шёл ДО `dlg.exec()`, поэтому при Rejected picker уже
        был удалён → пустота в чате на месте david'а + lora не
        появлялась (advance_gen_queue не вызывался).
        """
        ep_id = self._ep_id or ""
        name = self._outfit_target_names.get(ep_id, "")
        sender_card = self._outfit_source_btns.get(ep_id)
        if not name or sender_card is None:
            return
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show:
                return
            refs_dir = (self._mw._project_root / "shows" / cur_show
                        / "refs" / "characters" / name)
            refs_dir.mkdir(parents=True, exist_ok=True)
            from widgets import RefPickerDialog
            from PyQt6.QtWidgets import QDialog
            title = tr('gen_picker_title', name=name)
            dlg = RefPickerDialog(refs_dir, title, parent=self)
            # 1) Открываем диалог. До Accepted check — НИКАКИХ побочных
            #    эффектов на picker / thread / source_btn. Если юзер
            #    Cancel'нёт — всё остаётся как было.
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            picked_name = dlg.selected_filename
            if not picked_name:
                return
            # 2) Accepted с picked_name → теперь cleanup + save + advance.
            #    Stop outfit thread (terminate claude.exe subprocess —
            #    не тратим Max-tokens впустую если ещё бежит).
            outfit_thread = self._outfit_threads.get(ep_id)
            if outfit_thread is not None and outfit_thread.isRunning():
                try:
                    outfit_thread.stop()
                except Exception:
                    pass
            self._outfit_threads.pop(ep_id, None)
            # Delete picker.
            outfit_picker = self._outfit_pickers.pop(ep_id, None)
            if outfit_picker is not None:
                try:
                    outfit_picker.setParent(None)
                    outfit_picker.deleteLater()
                except Exception:
                    pass
            # Clear registries.
            self._outfit_source_btns.pop(ep_id, None)
            self._outfit_target_names.pop(ep_id, None)
            self._outfit_target_displays.pop(ep_id, None)
            self._outfit_seen_variants.pop(ep_id, None)
            self._outfit_descriptions.pop(ep_id, None)
            # Save decision + delete source_btn (скрытая GenButton в
            # _gen_layout) + advance queue.
            # 2026-05-11 (v1.0.46): character filename ВСЕГДА хранится с
            # folder prefix `<character_slug>/<file>`. Раньше передавали
            # просто picked_name без префикса → decisions ломались и
            # _linked_file_exists возвращал False → CTA блокировалась.
            # `name` здесь — это character_slug (имя в чате AI).
            self._save_ref_decision(
                'character', name, "linked",
                filename=f"{name}/{picked_name}")
            try:
                sender_card.setParent(None)
                sender_card.deleteLater()
            except Exception:
                pass
            if sender_card is self._gen_button:
                self._gen_button = None
            self._advance_gen_queue()
        except Exception:
            traceback.print_exc()

    def _on_outfit_variant_chosen(self, text: str):
        """Юзер выбрал вариант одежды. Сценарий:
          1. Записываем pending-запрос в ActorsView (character + show + text).
          2. Переключаем главную вкладку на «Актёры».
          3. Удаляем пикер и исходную character-кнопку из чата.
          4. Делаем _advance_gen_queue() — следующий маркер из очереди."""
        try:
            mw = self._mw
            cur_show = _sa.get_current_show(mw._project_root)
        except Exception:
            cur_show = None
        # Для показа в баннере берём display name сериала (как юзер
        # вводил при создании: «Финальный расчёт»), а не slug
        # (`finalnyy_raschet`). Если функция недоступна — fallback на slug.
        show_display = cur_show or ""
        try:
            from show_manager import display_name_for as _show_display_name
            if cur_show:
                show_display = _show_display_name(
                    mw._project_root, cur_show) or cur_show
        except Exception:
            pass
        # 2026-05-07: per-episode — берём state из dict'ов.
        ep_id = self._ep_id or ""
        target_name = self._outfit_target_names.get(ep_id, "")
        target_display = self._outfit_target_displays.get(ep_id, "")
        # Имя роли для баннера: «muzh (Муж)» если есть display, иначе slug.
        role_label = target_name or ""
        if target_display:
            role_label = f"{target_name} ({target_display})"
        try:
            actors_view = getattr(mw, 'actors_view', None)
            if actors_view is not None and hasattr(
                    actors_view, 'set_pending_create_request'):
                actors_view.set_pending_create_request(
                    role_label,
                    show_display or "",
                    text or "",
                    ep_id=ep_id,
                    character_slug=target_name)
        except Exception:
            traceback.print_exc()
        try:
            tabs = getattr(mw, 'tabs', None)
            idx = getattr(mw, '_actors_tab_idx', -1)
            if tabs is not None and idx is not None and idx >= 0:
                tabs.setCurrentIndex(idx)
        except Exception:
            traceback.print_exc()
        # 2026-05-07: НЕ удаляем picker сразу — юзер может передумать
        # в Actors view (вернуться → выбрать другой вариант или нажать
        # Cancel). Picker автоматически снимется когда персонаж реально
        # залинкуется в episodes.json (через `_purge_resolved_markers`,
        # который тикает раз в 2с в `_check_montage_ready`). Если юзер
        # отменит создание референса в Actors view — picker останется
        # с теми же 3 вариантами.

    def notify_character_generation_started(self, ep_id: str,
                                              character_slug: str):
        """2026-05-07: вызывается из views/actors.py:start_ref_generation
        когда юзер кликнул «Создать референс» в Актёрах.

        Закрываем outfit picker для эпизода `ep_id` (если он показывает
        варианты для `character_slug`). Юзер уже выбрал вариант и стартанул
        генерацию — picker больше не нужен.

        Если current ep == ep_id, дополнительно advance_gen_queue() чтобы
        сразу показать следующий character marker. Если юзер на другой
        вкладке/эпизоде — picker удаляется молча; при возвращении в этот
        эпизод `_restore_gen_buttons_from_history` пропустит этот character
        благодаря `mw.is_active_character_gen(ep_id, slug)`."""
        if not ep_id or not character_slug:
            return
        target_name = self._outfit_target_names.get(ep_id, "")
        if target_name and target_name != character_slug:
            # picker для другого character'а — не трогаем (хотя такой
            # ситуации не должно быть, picker всегда один на ep).
            return
        # Удаляем picker и связанные источники по ep_id (не self._ep_id).
        picker = self._outfit_pickers.pop(ep_id, None)
        if picker is not None:
            try:
                picker.setParent(None)
                picker.deleteLater()
            except Exception:
                traceback.print_exc()
        src = self._outfit_source_btns.pop(ep_id, None)
        if src is not None:
            try:
                if self._gen_button is src:
                    self._gen_button = None
                src.setParent(None)
                src.deleteLater()
            except Exception:
                traceback.print_exc()
        self._outfit_target_names.pop(ep_id, None)
        self._outfit_target_displays.pop(ep_id, None)
        self._outfit_seen_variants.pop(ep_id, None)
        self._outfit_descriptions.pop(ep_id, None)
        # Останавливаем фоновый SuggestOutfitsThread если ещё работает.
        thread = self._outfit_threads.pop(ep_id, None)
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.stop()
            except Exception:
                pass
        # Если юзер прямо сейчас на этом эпизоде — advance queue (показать
        # следующий character/object/location marker).
        if self._ep_id == ep_id:
            try:
                self._advance_gen_queue()
            except Exception:
                traceback.print_exc()

    def notify_character_generation_finished(self, ep_id: str,
                                                character_slug: str,
                                                success: bool):
        """2026-05-07: вызывается из views/actors.py когда character
        генерация завершилась (success=True если variant kept/linked,
        success=False если ошибка или юзер отменил все варианты).

        При success=True — реф уже linked в `refs_decisions`, и
        `_purge_resolved_markers` сам всё уберёт через timer.
        При success=False — сбрасываем флаг is_active_character_gen и
        даём юзеру шанс снова создать реф (gen-карточка вернётся при
        следующем set_episode → restore_gen_buttons)."""
        try:
            mw = self._mw
            if mw is not None:
                mw.unregister_active_character_gen(ep_id, character_slug)
        except Exception:
            traceback.print_exc()
        # Если юзер сейчас на этом эпизоде — после ошибки рендерим заново
        # gen-карточку для этого character (она была удалена при start).
        if not success and self._ep_id == ep_id:
            try:
                self._purge_resolved_markers()
                # Pending markers — добавим заново (на случай если очередь
                # уже опустела). Перерисовка gen buttons произойдёт через
                # _restore_gen_buttons_from_history при следующем set_episode.
            except Exception:
                traceback.print_exc()

    def _cleanup_outfit_picker(self, restore_source: bool):
        """Удаляет CharacterOutfitPicker для ТЕКУЩЕГО эпизода. Если
        restore_source=True — возвращает исходную GenButton-карточку
        (она была .hide()).

        2026-05-05 fix: при `restore_source=False` ОБЯЗАТЕЛЬНО зануляем
        `self._gen_button` если он указывает на удаляемый source —
        иначе указатель повисает на удалённый widget и `_advance_gen_queue`
        не вызывается каллером.

        2026-05-07: per-episode — оперируем dict'ами по `self._ep_id`."""
        ep_id = self._ep_id or ""
        picker = self._outfit_pickers.pop(ep_id, None)
        if picker is not None:
            try:
                picker.setParent(None)
                picker.deleteLater()
            except Exception:
                pass
        src = self._outfit_source_btns.get(ep_id)
        if restore_source and src is not None:
            try:
                src.show()
            except Exception:
                pass
        else:
            # Удаляем исходную кнопку из layout — её роль выполнил пикер.
            if src is not None:
                try:
                    if self._gen_button is src:
                        self._gen_button = None
                    src.setParent(None)
                    src.deleteLater()
                except Exception:
                    pass
            self._outfit_source_btns.pop(ep_id, None)
        # Имя цели и display сбрасываем всегда — они привязаны к этому
        # cancel/variant-chosen действию.
        self._outfit_target_names.pop(ep_id, None)
        self._outfit_target_displays.pop(ep_id, None)
        # 2026-05-07: накопленные seen-варианты тоже чистим — следующий
        # запуск picker'а для этого character'а начнёт с чистой памяти.
        self._outfit_seen_variants.pop(ep_id, None)
        self._outfit_descriptions.pop(ep_id, None)

    # ── Долг 13 (2026-05-04 hotfix #10): три кнопки выбора в idle ────

    def _ep_meta_path(self):
        """Путь к episodes.json активного сериала, или None если не определено."""
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show:
                return None
            root = self._mw._project_root / "shows" / cur_show
            return root / "episodes.json"
        except Exception:
            return None

    def _save_ref_decision(self, gen_type: str, name: str,
                            decision: str, filename: str = ""):
        """Записывает решение юзера по конкретному ✗ пункту в
        episodes.json[ep_id]["refs_decisions"][type][name].

        decision:
          • "skipped"  — реф не нужен (AI не цепляет к промптам).
          • "linked"   — юзер выбрал существующий файл `filename`.
          • "" (пустое) — стирает запись (для undo).

        2026-05-11 (v1.0.46): defense-in-depth для character filename.
        Инвариант: character filename ВСЕГДА хранится с folder prefix
        `<character_slug>/<file>`. Если caller забыл prefix —
        автоматически добавляем + log warning. Защищает от регрессии
        вроде до-v1.0.46 бага в `_on_outfit_accepted`.
        """
        if not self._ep_id:
            return
        if (gen_type == 'character' and filename
                and '/' not in filename):
            old = filename
            filename = f"{name}/{filename}"
            try:
                import sys as _sys
                _sys.stderr.write(
                    f"[save_ref_decision] auto-prepended character folder: "
                    f"{old} → {filename}\n")
            except Exception:
                pass
        path = self._ep_meta_path()
        if path is None:
            return
        try:
            import json
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    data = {}
            ep = data.setdefault(self._ep_id, {})
            decisions = ep.setdefault("refs_decisions", {})
            bucket = decisions.setdefault(gen_type, {})
            if not decision:
                bucket.pop(name, None)
                if not bucket:
                    decisions.pop(gen_type, None)
                if not decisions:
                    ep.pop("refs_decisions", None)
            else:
                entry = {"decision": decision}
                if filename:
                    entry["filename"] = filename
                bucket[name] = entry
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # Триггерим перечитывание meta в MainWindow чтобы UI рефов
            # увидел изменения. Также пересобираем refs view если юзер
            # сейчас на этом эпизоде — иначе изменения видны только при
            # перезаходе.
            try:
                self._mw._meta = data
                if (getattr(self._mw, '_current_episode', None) == self._ep_id
                        and hasattr(self._mw, '_build_refs_view')):
                    self._mw._build_refs_view(self._ep_id)
            except Exception:
                pass
        except Exception:
            traceback.print_exc()

    def _purge_resolved_markers(self):
        """2026-05-06: чистит `_pending_markers` и активную `_gen_button`
        от маркеров для которых юзер уже принял решение (`linked` /
        `skipped` в `refs_decisions`).

        Сценарий: юзер залинковал персонажа через wildcard «+ Добавить
        персонажа» на вкладке Актёров → вернулся в чат эпизода. До этой
        чистки активная карточка/очередь оставались с устаревшим
        состоянием и просили снова сгенерировать уже-готовый реф.

        Безопасно зовётся в любой момент: если ничего разрешать не нужно
        — функция тихо завершается без побочных эффектов.
        """
        try:
            decisions = self._read_refs_decisions()
        except Exception:
            return
        if not decisions:
            return

        def _resolved(gen_type: str, name: str) -> bool:
            d = decisions.get(gen_type, {}).get(name)
            if not isinstance(d, dict):
                return False
            dec = d.get('decision')
            if dec == 'skipped':
                return True
            if dec == 'linked':
                # v1.0.58: linked засчитываем resolved ТОЛЬКО если файл
                # реально на диске. Ложные linked от agent через Bash
                # tool (decision=linked без файла) НЕ считаются resolved
                # — маркер останется в queue и юзер увидит кнопку
                # «Сгенерировать». Симметрия с окном РЕФЕРЕНСЫ и CTA.
                fn = d.get('filename', '') or ''
                return self._linked_file_exists(gen_type, fn, slug=name)
            return False

        # 1) Чистим очередь pending'ов — выкидываем уже разрешённые.
        if self._pending_markers:
            kept: list = []
            for item in self._pending_markers:
                if len(item) == 4:
                    gen_type, name, _desc, _disp = item
                else:
                    gen_type, name, _desc = item
                if _resolved(gen_type, name):
                    # имя осталось в `_gen_seen_names` чтобы повторно не
                    # пытаться его создать
                    continue
                kept.append(item)
            self._pending_markers[:] = kept

        # 2) Активная карточка с уже-разрешённым именем — закрываем и
        #    берём следующий маркер из очищенной очереди.
        btn = self._gen_button
        if btn is not None:
            try:
                gen_type = getattr(btn, '_type', None) or getattr(btn, 'type', None)
                name = getattr(btn, '_name', None) or getattr(btn, 'name', None)
            except Exception:
                gen_type, name = None, None
            if gen_type and name and _resolved(gen_type, name):
                self._clear_gen_button()
                self._advance_gen_queue()
        # 3) 2026-05-07: outfit picker для текущего эпизода с разрешённым
        #    character — закрываем (юзер реально создал реф через Actors).
        try:
            ep_id = self._ep_id or ""
            target_name = self._outfit_target_names.get(ep_id, "")
            picker = self._outfit_pickers.get(ep_id)
            if picker is not None and target_name and _resolved('character', target_name):
                self._cleanup_outfit_picker(restore_source=False)
                self._advance_gen_queue()
        except Exception:
            traceback.print_exc()
        # 2026-05-06: после любого изменения decisions проверяем готов
        # ли эпизод к multi-agent монтажу — показываем/скрываем CTA.
        # 2026-05-07: убрали вызов `_check_montage_ready()` отсюда
        # чтобы не было рекурсии при подключении purge к timer'у.
        # Caller сам обновит CTA если нужно (`set_episode` делает явно;
        # таймер `_montage_ready_timer` подключён к обоим слотам подряд).

    def _advance_gen_queue(self):
        """Phase 2 hotfix #10: после skip/linked/done отвязываем
        указатель `_gen_button` (виджет остаётся в layout как история)
        и берём следующий маркер из `_pending_markers`.

        Phase 2 hotfix #13: НЕ дисконнектим сигналы старой кнопки —
        виджет остаётся в done/skipped/linked state, и его кнопки
        («✓ Открыть в РЕФЕРЕНСАХ», «↶ Передумал») должны продолжать
        работать. Idle-кнопки (Сгенерировать/Не нужен/Выбрать) на
        не-idle state'ах не реагируют благодаря guard'у в слотах
        GenButton.
        """
        self._gen_button = None
        # Берём следующий из очереди.
        if not self._pending_markers:
            return
        item = self._pending_markers.pop(0)
        # Backwards-compat: pending мог быть кортежем из 3 (старый формат)
        # или из 4 (с display).
        if len(item) == 4:
            gen_type, name, description, display = item
        else:
            gen_type, name, description = item
            display = ""
        # Имя уже в `_gen_seen_names` (было добавлено в _maybe_show_gen_button
        # ДО постановки в очередь). Чтобы _maybe_show_gen_button его не
        # отбросил по dedup, временно убираем.
        self._gen_seen_names.discard(name)
        self._maybe_show_gen_button(gen_type, name, description, display=display)

    def _on_gen_skip(self, gen_type: str, name: str):
        """🚫 Не нужен — записываем решение и переключаем GenButton в skipped."""
        self._save_ref_decision(gen_type, name, "skipped")
        if self._gen_button is not None:
            self._gen_button.set_skipped()
        self._advance_gen_queue()

    def _on_gen_use_existing(self, gen_type: str, name: str):
        """📁 Выбрать существующий — попап с превью референсов из папки.

        Phase 2 hotfix #24: заменили QFileDialog (нативный файловый
        менеджер ОС) на кастомный RefPickerDialog с сеткой превью.
        Юзер кликает по превью или кнопке «Выбрать» под ним.

        Phase 2 hotfix #25: после выбора карточка УДАЛЯЕТСЯ из чата
        (а не остаётся в state linked как раньше) и сразу показывается
        следующий ✗ маркер из очереди. Юзер хочет «выбрал → исчезло
        → следующий».
        """
        # Карточка с которой пришёл сигнал — найдём через sender,
        # т.к. _on_gen_use_existing может прилететь от любой idle-карточки.
        sender = self.sender()
        sender_card: Optional[GenButton] = None
        try:
            cur = sender
            while cur is not None and not isinstance(cur, GenButton):
                cur = cur.parent()
            if isinstance(cur, GenButton):
                sender_card = cur
        except Exception:
            sender_card = None
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show:
                return
            sub = {'location': 'locations', 'object': 'objects',
                   'character': 'characters'}.get(gen_type, gen_type + 's')
            refs_dir = (self._mw._project_root / "shows" / cur_show
                        / "refs" / sub)
            # Для character — папка конкретного героя.
            if gen_type == 'character':
                refs_dir = refs_dir / name
            refs_dir.mkdir(parents=True, exist_ok=True)
            from widgets import RefPickerDialog
            from PyQt6.QtWidgets import QDialog
            title = tr('gen_picker_title', name=name)
            # 2026-05-17: slug=name → exact-match (stem == name)
            # подсвечивается зелёной рамкой, "name*"-файлы поднимаются
            # в начало списка. Для character папка уже refs/characters/
            # <name>/ (slug-сортировка избыточна) — но передаём
            # единообразно: highlight'нёт точное «<name>.jpg» если оно
            # есть, остальные останутся по алфавиту.
            dlg = RefPickerDialog(refs_dir, title, parent=self, slug=name)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            picked_name = dlg.selected_filename
            if not picked_name:
                return
            # 2026-05-11 (v1.0.48): cleanup устаревшего twin entry перед
            # сохранением. Сценарий: AI назвал маркер X → запустил
            # autogen → collision-resolve переименовал в X_N → autogen
            # завершился → decisions['<kind>']['X_N'] записан с
            # filename='X_N.<ext>'. Теперь юзер кликает Pick existing для
            # маркера X (без _N), выбирает 'X_N.<ext>' (тот же файл).
            # Если оставить старый entry под X_N — в decisions будет
            # ДВА linked entries указывающих на один файл → UI
            # References отрисует 2 одинаковые карточки (img7+img8 кейс).
            # Чистим: stem(picked) != name И есть entry под picked_stem
            # с decision=linked → удаляем его. Только для location/object
            # (у character key=имя персонажа, structurally нет twin).
            if gen_type in ('location', 'object'):
                from pathlib import Path as _Path
                picked_stem = _Path(picked_name).stem
                if picked_stem != name:
                    try:
                        existing = (
                            self._read_refs_decisions()
                            .get(gen_type, {})
                            .get(picked_stem))
                        if (isinstance(existing, dict)
                                and existing.get('decision') == 'linked'):
                            # Удаляем twin через _save_ref_decision с
                            # пустым decision (это путь undo в _save).
                            self._save_ref_decision(
                                gen_type, picked_stem, "")
                            try:
                                import sys as _sys
                                _sys.stderr.write(
                                    f"[pick-existing-twin-cleanup] "
                                    f"{self._ep_id}/{gen_type}/{name}: "
                                    f"removed twin {picked_stem!r} "
                                    f"(was filename={existing.get('filename')!r})\n")
                            except Exception:
                                pass
                    except Exception:
                        traceback.print_exc()
            # Сохраняем имя файла относительно refs_dir.
            self._save_ref_decision(gen_type, name, "linked",
                                    filename=picked_name)
            # Hotfix #25: убираем карточку и переходим к следующему маркеру.
            if sender_card is not None:
                try:
                    sender_card.setParent(None)
                    sender_card.deleteLater()
                except Exception:
                    pass
                if sender_card is self._gen_button:
                    self._gen_button = None
            self._advance_gen_queue()
        except Exception:
            traceback.print_exc()

    def _on_gen_undo(self, gen_type: str, name: str):
        """↶ Передумал — стираем запись и возвращаем GenButton в idle.

        Phase 2 hotfix #22: ищем именно ту карточку с которой пришёл
        сигнал (через self.sender()), а не self._gen_button. Карточка
        skipped/linked может лежать в layout как «история» — указатель
        `_gen_button` уже ушёл на следующую (или None). Без этого
        фикса decision стирался в episodes.json, а карточка визуально
        оставалась в state «🚫 помечен как ненужный» — юзер видел
        застой.
        """
        self._save_ref_decision(gen_type, name, "")
        sender = self.sender()
        target_card: Optional[GenButton] = None
        try:
            cur = sender
            while cur is not None and not isinstance(cur, GenButton):
                cur = cur.parent()
            if isinstance(cur, GenButton):
                target_card = cur
        except Exception:
            pass
        # Если нашли конкретную карточку (через sender) — сбрасываем её.
        # Если sender не дошёл до GenButton (защита), фолбэк на _gen_button.
        if target_card is not None:
            target_card.reset_to_idle()
            # Если сейчас НЕТ активной генерации — назначаем эту карточку
            # активной (чтобы юзер мог опять выбрать 🎨/🚫/📁).
            if self._gen_button is None:
                self._gen_button = target_card
                # Из dedup-сета имя удаляем, иначе при повторном
                # synthesize эту же карточку не добавит.
                try:
                    self._gen_seen_names.discard(name)
                except Exception:
                    pass
        elif self._gen_button is not None:
            self._gen_button.reset_to_idle()

    def _on_done(self, _rc: int):
        # 2026-05-11 multi-ep fix: глушим тикер только если у текущего
        # ep_id больше нет живых тредов (раньше было unconditional stop —
        # это сносило анимацию параллельных генераций в других эпизодах,
        # отображаемых через тот же EpisodeChatView).
        self._maybe_stop_thinking()
        done = f"\n\n{tr('new_ep_log_done')}\n"
        if self._ep_id:
            _sa.append_chat_message(self._ep_id, "system", done, kind='ok')
            self._render_message(done, kind='ok')
        self.status_lbl.setStyleSheet("color:#6db86d; font-size:12px;")
        self.status_lbl.setText(tr('new_ep_log_done'))
        self.send_btn.setEnabled(True)
        # Phase 2 hotfix #8: если AI не вставил [[GEN:...]] маркеры в свой
        # ответ (просто словами «- ✗ name — рефа нет»), мы сами их
        # синтезируем по строкам секций ЛОКАЦИИ:/ОБЪЕКТЫ: и создаём кнопки.
        if self._ep_id and self._gen_button is None and self._stream_full:
            try:
                self.try_synthesize_gen_markers(self._ep_id, self._stream_full)
            except Exception:
                pass
        # 2026-05-06: проверяем готов ли эпизод к multi-agent монтажу.
        try:
            self._check_montage_ready()
        except Exception:
            traceback.print_exc()
        self.input_edit.setFocus()

    def _on_error(self, msg: str):
        # 2026-05-11 multi-ep fix: см. комментарий в `_on_done`.
        self._maybe_stop_thinking()
        line = f"\n\n{tr('new_ep_log_error')}: {msg}\n"
        if self._ep_id:
            _sa.append_chat_message(self._ep_id, "system", line, kind='error')
            self._render_message(line, kind='error')
        self.status_lbl.setStyleSheet("color:#cc6666; font-size:12px;")
        self.status_lbl.setText(f"{tr('new_ep_log_error')}: {msg[:120]}")
        self.send_btn.setEnabled(True)

    def _on_stopped(self):
        # 2026-05-11 multi-ep fix: см. комментарий в `_on_done`.
        self._maybe_stop_thinking()
        line = f"\n\n{tr('new_ep_log_stopped')}\n"
        if self._ep_id:
            _sa.append_chat_message(self._ep_id, "system", line, kind='warn')
            self._render_message(line, kind='warn')
        self.status_lbl.setStyleSheet("color:#aaa; font-size:12px;")
        self.status_lbl.setText(tr('new_ep_log_stopped'))
        self.send_btn.setEnabled(True)

    # ──────────────────────────────────────────────────────────────────
    # 2026-05-06: Multi-agent монтажная карта.
    # ──────────────────────────────────────────────────────────────────

    def _check_montage_ready(self) -> None:
        """Показывает/скрывает MontageCTA в зависимости от состояния
        эпизода.

        2026-05-07: дополнительно тикает `_purge_resolved_markers`
        чтобы outfit picker'ы / idle-карточки автоматически закрывались
        когда юзер залинковал реф через вкладку Actors (без смены
        эпизода в Editor).

        Условия для показа:
          • Эпизод выбран.
          • Поток `_thread` (RunEpisodeThread) НЕ работает.
          • Активная gen-кнопка `_gen_button` отсутствует, очередь
            `_pending_markers` пуста (то есть AI обработал все маркеры
            из сценария).
          • Outfit picker не активен.
          • Все маркеры что AI назвал в чате — `linked` или `skipped`
            в `refs_decisions` (не осталось «✗ нерешённых»).
          • Хотя бы один реф linked (иначе нечего монтировать).
          • Сценарий эпизода непустой (есть `scenarios/<ep>.txt` или
            `scenarios/_active.txt`).

        Условия для скрытия:
          • Любое из выше нарушено → hide.
          • Уже идёт `_montage_thread` → не трогаем (будет в
            running-state).
        """
        # 2026-05-07: оркестратор для ТЕКУЩЕГО эпизода уже бежит — CTA
        # уже в running-state, не трогаем. Треды других эпизодов на CTA
        # этого эпизода не влияют.
        cur_t = self._montage_threads.get(self._ep_id) if self._ep_id else None
        if cur_t is not None and cur_t.isRunning():
            return

        if not self._ep_id:
            self._montage_cta.hide()
            return

        # v1.0.82: Если на диске уже есть полная монтажная карта (через
        # episodes.json[ep].montage_card или fallback _agent_log_epN.json)
        # — CTA показывает «📂 Открыть монтажную карту». Юзер сам решает
        # когда открывать попап.
        try:
            if self._has_saved_montage_card(self._ep_id):
                self._montage_cta.show_open_map()
                return
        except Exception:
            traceback.print_exc()

        # v1.0.87 (этап 7D resume-фичи): если pipeline упал в прошлой
        # сессии (или продолжает крутиться по записи лога, но тред мёртв
        # после рестарта Studio) — показываем «🔄 Продолжить / 🆕 Начать
        # заново» вместо обычного idle. Приоритет выше legacy
        # episode_has_montage_card и in-memory failed-state (см.
        # _restore_montage_cta_for_current_ep).
        try:
            info = self._resumable_from_log(self._ep_id)
            if info:
                self._montage_cta.show_resumable(
                    info["last_completed_stage"],
                    info.get("next_stage"))
                self._montage_cta.show()
                new_state = f"resumable_{info['last_completed_stage']}"
                if getattr(self, '_last_montage_state', None) != new_state:
                    self._last_montage_state = new_state
                    self._diag_log_append(
                        'montage_ready',
                        f"ep={self._ep_id} state=resumable "
                        f"last={info['last_completed_stage']} "
                        f"next={info.get('next_stage')}")
                return
        except Exception:
            traceback.print_exc()

        # 2026-05-06 fallback (legacy): эпизоды до v1.0.82 могли иметь
        # только урезанный blocks-формат после клика «Делать сториборды»
        # — без полной montage_card. Тогда CTA скрыта (как раньше).
        try:
            if self._episode_has_montage_card():
                self._montage_cta.hide()
                return
        except Exception:
            pass

        # 1. Не должно быть активного AI-потока. Поток может жить в:
        #    • EpisodeChatView._thread — follow-up reply'ы из чата эпизода.
        #    • NewEpisodeView._thread — первичный запуск с «+» (когда юзер
        #      создаёт новый эпизод и Studio переехала в чат). Поток
        #      продолжает стримить chunks через on_external_append.
        # Также пока крутится `_thinking_active` (бегут точки) — AI ещё
        # генерирует ответ.
        if self._thinking_active:
            self._montage_cta.hide()
            return
        if (self._thread is not None and self._thread.isRunning()):
            self._montage_cta.hide()
            return
        try:
            nev = getattr(self._mw, 'new_episode_view', None)
            if nev is not None:
                # 2026-05-07: per-episode проверка. Раньше блокировалось
                # глобально (любой первичный анализ скрывал CTA на ВСЕХ
                # эпизодах). Теперь смотрим только тред СВОЕГО ep_id.
                nev_threads = getattr(nev, '_threads', None) or {}
                t_for_ep = nev_threads.get(self._ep_id)
                if t_for_ep is not None and t_for_ep.isRunning():
                    self._montage_cta.hide()
                    return
                # Fallback на legacy `_thread` для старого кода (если ep
                # совпадает с current_ep_id формы — она ещё не handed-off).
                nev_thread = getattr(nev, '_thread', None)
                nev_cur_ep = getattr(nev, '_current_ep_id', None)
                if (nev_thread is not None and nev_thread.isRunning()
                        and nev_cur_ep == self._ep_id):
                    self._montage_cta.hide()
                    return
        except Exception:
            pass

        # 2. Нет открытой gen-кнопки и пустая очередь.
        # 2026-05-07: учитываем параллельные running-треды location/object
        # для текущего эпизода (живут в MW._active_gens) — пока хоть один
        # из них в работе, рефы ещё не дособраны, CTA «Все рефы готовы»
        # появляться не должна.
        try:
            mw_busy_for_ep = self._mw.has_active_gens_for_ep(self._ep_id)
        except Exception:
            mw_busy_for_ep = False
        if (self._gen_button is not None or self._pending_markers
                or mw_busy_for_ep):
            self._montage_cta.hide()
            return

        # 3. Outfit picker не активен ДЛЯ ТЕКУЩЕГО эпизода.
        # 2026-05-07: per-episode — picker'ы других эпизодов на CTA
        # этого эпизода не влияют.
        cur_ep = self._ep_id or ""
        cur_picker = self._outfit_pickers.get(cur_ep)
        cur_outfit_thread = self._outfit_threads.get(cur_ep)
        if cur_picker is not None or (
                cur_outfit_thread is not None
                and cur_outfit_thread.isRunning()):
            self._montage_cta.hide()
            return

        # 4. Активные генерации рефов на вкладке Актёры — если есть хоть
        #    одна (юзер запустил «Создать референс» и она ещё крутится),
        #    CTA не показываем: реф не готов.
        try:
            av = getattr(self._mw, 'actors_view', None)
            active_gens = getattr(av, '_active_generations', None) or {}
            if active_gens:
                self._montage_cta.hide()
                return
        except Exception:
            pass

        # 5. Все упомянутые в чате маркеры (рефы) должны быть РАЗРЕШЕНЫ —
        #    либо `linked` (юзер выбрал/сгенерировал), либо `skipped` (не
        #    нужен). Если хотя бы один маркер БЕЗ решения — это значит
        #    рефы ещё не дособраны (юзер на середине пути), CTA скрыть.
        #    Также должен быть хотя бы один linked (есть с чем монтировать).
        decisions = self._read_refs_decisions()
        if not decisions:
            self._montage_cta.hide()
            return
        try:
            msgs = _sa.load_chat_messages(self._ep_id) or []
            full_text = "\n".join(
                m.get('text', '') for m in msgs
                if m.get('kind') is None and m.get('text'))
            markers = synthesize_gen_markers(full_text) if full_text else []
        except Exception:
            markers = []
        unresolved: list = []
        any_linked = False
        for m in markers:
            d = decisions.get(m.type, {}).get(m.name)
            # 2026-05-11 (v1.0.47) marker-alias fallback: AI в чате
            # называет маркер исходным именем (например 'house_corridor'),
            # но при collision-resolve файл переименован в
            # 'house_corridor_2.jpg' и decisions ключ стал
            # 'house_corridor_2' (либо через `_save_active_gen_decision`
            # с new_name, либо через v1.0.46 heal bucket-key rename).
            # Marker name из чата (`house_corridor`) не совпадает с
            # decisions key (`house_corridor_2`) → direct lookup fails.
            #
            # Fallback: пробуем `<m.name>_2..._9` как alias ключи.
            # Найдено РОВНО ОДНО с decision='linked' → используем.
            # Несколько → ambiguity, считаем unresolved (safety).
            # Это ТОЛЬКО read-side aliasing — НЕ мигрируем decisions,
            # НЕ переименовываем ключи. Чистый fallback на чтении.
            if not isinstance(d, dict):
                import re as _re
                # Skip alias search если marker уже заканчивается на _N
                # (избегаем nested suffix lookup `_2_2`).
                if not _re.search(r'_[2-9]$', m.name):
                    type_bucket = decisions.get(m.type, {}) or {}
                    alias_candidates = []
                    for n in range(2, 10):
                        alias_key = f"{m.name}_{n}"
                        cand = type_bucket.get(alias_key)
                        if (isinstance(cand, dict)
                                and cand.get('decision') == 'linked'):
                            alias_candidates.append((alias_key, cand))
                    if len(alias_candidates) == 1:
                        alias_key, d = alias_candidates[0]
                        try:
                            import sys as _sys
                            _sys.stderr.write(
                                f"[marker-alias] {self._ep_id}/{m.type}/"
                                f"{m.name} resolved via alias "
                                f"{alias_key}\n")
                        except Exception:
                            pass
                    elif len(alias_candidates) > 1:
                        try:
                            import sys as _sys
                            _sys.stderr.write(
                                f"[marker-alias] {self._ep_id}/{m.type}/"
                                f"{m.name}: ambiguous aliases "
                                f"{[k for k, _ in alias_candidates]}, "
                                f"treating as unresolved\n")
                        except Exception:
                            pass
                        unresolved.append(m.name)
                        continue
            if not isinstance(d, dict):
                unresolved.append(m.name)
                continue
            decision = d.get('decision')
            if decision == 'linked':
                # 2026-05-10 (БАГ 3 safety net): проверяем что файл
                # реально существует. В decisions может оказаться
                # устаревший filename (например неверное расширение
                # от старого auto-link до фикса БАГ 3, или юзер
                # удалил файл вручную) — тогда считаем unresolved,
                # CTA не показываем чтобы юзер не запустил монтаж
                # с битой ссылкой.
                if not self._linked_file_exists(
                        m.type, d.get('filename') or '', slug=m.name):
                    unresolved.append(m.name)
                    continue
                any_linked = True
            elif decision != 'skipped':
                unresolved.append(m.name)
        if unresolved:
            # Есть упомянутый маркер без решения — рефы ещё не дособраны.
            self._montage_cta.hide()
            # 2026-05-11 (v1.0.50): diagnostic log при изменении состояния.
            new_state = f"hidden_unresolved({len(unresolved)})"
            if getattr(self, '_last_montage_state', None) != new_state:
                self._last_montage_state = new_state
                resolved_count = len(markers) - len(unresolved)
                self._diag_log_append('montage_ready',
                    f"ep={self._ep_id} state=hidden "
                    f"markers={len(markers)} resolved={resolved_count} "
                    f"unresolved={sorted(set(unresolved))[:5]}")
            return
        if not any_linked:
            # Все маркеры skipped, ни одного linked — нечего монтировать.
            self._montage_cta.hide()
            new_state = "hidden_no_linked"
            if getattr(self, '_last_montage_state', None) != new_state:
                self._last_montage_state = new_state
                self._diag_log_append('montage_ready',
                    f"ep={self._ep_id} state=hidden_no_linked "
                    f"markers={len(markers)} (all skipped or no decisions)")
            return

        # 5. Должен быть текст сценария.
        scenario_text = self._load_scenario_text()
        if not scenario_text or len(scenario_text.strip()) < 50:
            self._montage_cta.hide()
            new_state = "hidden_no_scenario"
            if getattr(self, '_last_montage_state', None) != new_state:
                self._last_montage_state = new_state
                self._diag_log_append('montage_ready',
                    f"ep={self._ep_id} state=hidden_no_scenario")
            return

        # Все условия выполнены — показываем idle CTA.
        self._montage_cta.show_idle()
        self._montage_cta.show()
        new_state = "ready"
        if getattr(self, '_last_montage_state', None) != new_state:
            self._last_montage_state = new_state
            self._diag_log_append('montage_ready',
                f"ep={self._ep_id} state=ready markers={len(markers)} "
                f"all linked/skipped → CTA shown")

    def _linked_file_exists(self, gen_type: str, filename: str,
                             slug: Optional[str] = None) -> bool:
        """Проверяет что файл рефа реально существует на диске.
        Используется в `_check_montage_ready` как safety net для
        linked-decisions с устаревшим filename (см. БАГ 3 — wrong
        extension в decisions из-за слепого доверия hint'у агента).

        Path layout:
          • location:  refs/locations/<filename>
          • object:    refs/objects/<filename>
          • character: refs/characters/<filename>
                       (filename содержит folder/file.jpg)

        2026-05-11 (БАГ 11 fix): для location/object добавлен
        disk-glob fallback — если hint filename не существует
        (например `.png` в decisions, а на диске `.jpg`), ищем
        реальный файл с тем же base name через glob. Mirror
        логики `list_episode_refs` layer 2 self-healing
        (БАГ 10 fix). Без этого fallback CTA «Make storyboards»
        пряталась даже когда refs panel показывал файлы.
        """
        if not filename:
            return False
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show:
                return False
            sub = {
                'location': 'locations',
                'object': 'objects',
                'character': 'characters',
            }.get(gen_type)
            if not sub:
                return False
            base = (self._mw._project_root / "shows" / cur_show
                    / "refs" / sub)
            # 1. Hint exists check.
            path = base / filename
            if path.exists() and path.is_file():
                return True
            # 2. Disk-glob fallback для location/object.
            # 2026-05-11 (v1.0.45): расширено на character — для
            # filename вида `folder/file.jpg` ищем `folder/file.<ext>`
            # с тем же базовым именем. Folder остаётся неизменным:
            # подменяем только расширение в пределах одной outfit-папки.
            # Это безопасно (выбранный outfit персонажа = та же
            # папка), но защищает от устаревшего hint'а от агента.
            # 2026-05-11 (v1.0.46): для character БЕЗ '/' в filename
            # (legacy bug до v1.0.46) — slug-based lookup. Для
            # location/object — suffix-variant lookup `<slug>_2..._9`.
            from pathlib import Path as _Path
            exts = ('.jpg', '.jpeg', '.png', '.webp')
            if gen_type in ('location', 'object') and '/' not in filename:
                stem = _Path(filename).stem
                for ext in exts:
                    if (base / f"{stem}{ext}").exists():
                        return True
                # v1.0.46: suffix variants <slug>_N.<ext> для
                # collision-resolve кейсов где decisions устарел.
                # Используем slug если передан, иначе stem.
                lookup_slug = slug or stem
                import re as _re
                if not _re.search(r'_[2-9]$', lookup_slug):
                    for n in range(2, 10):
                        for ext in exts:
                            if (base / f"{lookup_slug}_{n}{ext}").exists():
                                return True
            elif gen_type == 'character':
                if '/' in filename:
                    # character: filename = "folder/file.ext"
                    folder, _, file_part = filename.partition('/')
                    file_stem = _Path(file_part).stem
                    folder_path = base / folder
                    if folder_path.is_dir():
                        for ext in exts:
                            if (folder_path / f"{file_stem}{ext}").exists():
                                return True
                elif slug:
                    # v1.0.46: filename без folder prefix (legacy bug
                    # до v1.0.46). Используем slug как имя character-
                    # folder. НЕ scan'им все subdirs — outfit-safety.
                    file_stem = _Path(filename).stem
                    folder_path = base / slug
                    if folder_path.is_dir():
                        for ext in exts:
                            if (folder_path / f"{file_stem}{ext}").exists():
                                return True
            return False
        except Exception:
            return False

    def _load_scenario_text(self) -> str:
        """Возвращает текст сценария текущего эпизода или пустую строку."""
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show or not self._ep_id:
                return ""
            scen_dir = (self._mw._project_root / "shows" / cur_show
                        / "scenarios")
            # 2026-05-10: zero-pad ep{NN:02d}.txt — primary source of truth.
            # _active.txt из кандидатов УБРАН — он stale и разъезжался с
            # UI-эпизодом (баг «агент читает не тот сценарий»).
            candidates = []
            try:
                num_str = self._ep_id.lstrip('ep')
                if num_str.isdigit():
                    candidates.append(scen_dir / f"ep{int(num_str):02d}.txt")
            except Exception:
                pass
            candidates.append(scen_dir / f"{self._ep_id}.txt")
            candidates.append(scen_dir / f"{self._ep_id.lstrip('ep')}.txt")
            for p in candidates:
                if p.exists() and p.is_file():
                    return p.read_text(encoding='utf-8', errors='replace')
        except Exception:
            traceback.print_exc()
        return ""

    def _load_show_context(self) -> dict:
        """Собирает контекст всего сериала для агентов:
          • Bible (`shows/<slug>/bible.txt`) если есть.
          • Краткие выжимки всех остальных эпизодов из `scenarios/`
            (первые ~400 символов каждого) + их title из episodes.json.

        Возвращает dict вида:
          {
            "bible": "<text or ''>",
            "episodes_summary": [
              {"ep_id": "ep2", "title": "...", "scenario_excerpt": "..."},
              ...
            ]
          }

        Если `current_show` не определён или ничего не найдено — пустые
        поля. Безопасно: исключения подавляются.
        """
        ctx: dict = {"bible": "", "episodes_summary": []}
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show:
                return ctx
            show_root = self._mw._project_root / "shows" / cur_show

            # Bible сериала
            bible_path = show_root / "bible.txt"
            if bible_path.exists() and bible_path.is_file():
                try:
                    ctx["bible"] = bible_path.read_text(
                        encoding='utf-8', errors='replace'
                    ).strip()
                except Exception:
                    pass

            # Сбор episodes_summary — все эпизоды кроме текущего.
            # Title берём из episodes.json, текст — первые ~400 символов
            # из scenarios/<ep>.txt.
            ep_meta: dict = {}
            ep_meta_path = show_root / "episodes.json"
            if ep_meta_path.exists():
                try:
                    import json as _json
                    ep_meta = _json.loads(
                        ep_meta_path.read_text(encoding='utf-8')) or {}
                except Exception:
                    ep_meta = {}

            scen_dir = show_root / "scenarios"
            if scen_dir.exists():
                # Сортируем по номеру эпизода если можно.
                files = sorted(scen_dir.glob("ep*.txt"))
                for p in files:
                    stem = p.stem  # ep01, ep02, ...
                    # ep01 / ep1 — нормализуем.
                    try:
                        n = int(stem.lstrip('ep'))
                        ep_id = f"ep{n}"
                    except Exception:
                        ep_id = stem
                    # Текущий эпизод пропускаем — он в основном промпте.
                    if ep_id == self._ep_id:
                        continue
                    try:
                        text = p.read_text(encoding='utf-8',
                                            errors='replace').strip()
                        excerpt = text[:400] + ("…" if len(text) > 400 else "")
                    except Exception:
                        excerpt = ""
                    title = ""
                    ep_obj = ep_meta.get(ep_id) or {}
                    if isinstance(ep_obj, dict):
                        title = ep_obj.get('title', '') or ''
                    ctx["episodes_summary"].append({
                        "ep_id": ep_id,
                        "title": title,
                        "scenario_excerpt": excerpt,
                    })
        except Exception:
            traceback.print_exc()
        return ctx

    def _episode_has_montage_card(self) -> bool:
        """True если в episodes.json для текущего эпизода уже сохранена
        утверждённая карта (`blocks` непустой dict). Используется чтобы
        прятать CTA-карточку после клика «🎨 Делать сториборды».
        """
        try:
            path = self._ep_meta_path()
            if path is None or not path.exists() or not self._ep_id:
                return False
            import json as _json
            data = _json.loads(path.read_text(encoding='utf-8')) or {}
            ep = data.get(self._ep_id) or {}
            blocks = ep.get('blocks')
            return isinstance(blocks, dict) and len(blocks) > 0
        except Exception:
            return False

    def _build_refs_summary_for_orchestrator(self) -> dict:
        """Собирает компактный summary рефов из refs_decisions —
        передаётся агентам как список доступных slug'ов и filename'ов.

        2026-05-06: filename характеров в `refs_decisions` хранится с
        подпапкой (`david/david_<file>.jpg`). Для шапки промпта
        `# [@]img3 = <filename>` нужен ТОЛЬКО basename (без слеша),
        иначе Studio.parse_refs не найдёт файл на диске и реф не
        загрузится в NARWHAL → Nano Banana сгенерирует случайных
        людей вместо персонажа.
        """
        out: dict = {"locations": [], "objects": [], "characters": []}
        decisions = self._read_refs_decisions()
        for kind, key in (('location', 'locations'),
                          ('object', 'objects'),
                          ('character', 'characters')):
            bucket = decisions.get(kind) or {}
            for slug, d in bucket.items():
                if isinstance(d, dict) and d.get('decision') == 'linked':
                    fn = d.get('filename', '') or ''
                    # Берём только последний компонент пути
                    fn = fn.replace('\\', '/').split('/')[-1]
                    out[key].append({
                        'slug': slug,
                        'filename': fn,
                    })
        return out

    def _on_montage_cancel(self):
        """Клик «✗ Прервать» в running-state CTA. Останавливает
        активный MontageOrchestratorThread + грубо убивает все
        зависшие `claude --model` subprocess'ы (subprocess.run в
        нашем оркестраторе блокирующий, флаг _stop проверяется только
        между этапами — при зависании внутри одного этапа этого мало).
        После убийства возвращает CTA в idle, юзер может нажать
        «🎬 Сделать сториборды» заново.
        """
        # 1. Мягкая остановка оркестратора через флаг — для текущего
        #    эпизода (треды других эпизодов НЕ трогаем).
        try:
            cur_ep = self._ep_id
            t = self._montage_threads.get(cur_ep) if cur_ep else None
            if t is not None and hasattr(t, 'stop'):
                t.stop()
            if cur_ep:
                self._montage_states.pop(cur_ep, None)
        except Exception:
            traceback.print_exc()
        # 2. Грубое убийство всех Studio's `claude -p` процессов
        #    кросс-платформенно (Mac/Linux — pkill, Windows — PowerShell
        #    с CIM-фильтром по cmdline). Studio запускает CLI как
        #    `claude -p --system-prompt ... --model claude-...`. Маркер
        #    `--system-prompt` стабилен и уникален: Claude Code в репо
        #    запускается БЕЗ этого флага (interactive mode), не задеваем.
        try:
            import subprocess as _sp
            import sys as _sys
            if _sys.platform == 'win32':
                # PowerShell — единственный надёжный путь на Win 10/11
                # с фильтром по cmdline. Stop-Process -Force = TerminateProcess.
                ps_cmd = (
                    "Get-CimInstance Win32_Process "
                    "-Filter \"Name='claude.exe'\" "
                    "| Where-Object { $_.CommandLine -like '*--system-prompt*' } "
                    "| ForEach-Object { "
                    "  try { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue } catch {} }"
                )
                CREATE_NO_WINDOW = 0x08000000
                _sp.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                        capture_output=True, timeout=10,
                        creationflags=CREATE_NO_WINDOW)
            else:
                # Mac/Linux: pkill ищет по подстроке cmdline.
                # Маркер `-p ` (с пробелом) — стабильный для Studio CLI.
                _sp.run(['pkill', '-TERM', '-f', 'claude -p '],
                        capture_output=True, timeout=5)
        except Exception:
            traceback.print_exc()
        # 3. Сообщение в чат.
        try:
            line = ("\n✗ Генерация монтажной карты отменена. "
                    "Можешь попробовать запустить заново.\n")
            if self._ep_id:
                _sa.append_chat_message(self._ep_id, "system", line, kind='err')
                self._render_message(line, kind='err')
        except Exception:
            pass
        # 4. CTA → idle.
        try:
            self._montage_cta.show_idle()
            self._montage_cta.show()
        except Exception:
            traceback.print_exc()

    def _on_montage_start(self):
        """Клик «🎬 Сделать сториборды» — запуск оркестратора.

        2026-05-07: per-episode. Тред кладётся в `_montage_threads[ep_id]`,
        состояние running — в `_montage_states[ep_id]`. Если для этого
        эпизода тред уже бежит — повторный клик игнорируется. Параллельно
        могут бежать треды для разных эпизодов.
        """
        ep_id = self._ep_id
        if not ep_id:
            return
        existing = self._montage_threads.get(ep_id)
        if existing is not None and existing.isRunning():
            return  # для этого эпизода уже бежит

        cli = _sa.find_claude_cli()
        if not cli:
            self._montage_cta.show_failed(tr('new_ep_cli_missing'))
            return

        scenario = self._load_scenario_text()
        if not scenario:
            self._montage_cta.show_failed("Не нашёл текст сценария эпизода.")
            return

        refs_summary = self._build_refs_summary_for_orchestrator()
        if not refs_summary['locations']:
            self._montage_cta.show_failed("Нет залинкованных локаций.")
            return

        # Лог агентов кладём в shows/<slug>/output/_agent_log_<ep>.json
        # (gitignored).
        log_path = None
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if cur_show:
                from pathlib import Path as _P
                log_path = (self._mw._project_root / "shows" / cur_show
                            / "output"
                            / f"_agent_log_{self._ep_id}.json")
        except Exception:
            pass

        # 2026-05-09: модели монтажки прибиты per-agent в
        # MontageOrchestratorThread (MODEL_* константы класса).
        # С v1.0.60: Scriptwriter на Opus 4.7, остальные 3 (Validator/
        # Editor/Context Reviewer) на Sonnet 4.6. Полная история и
        # обоснование — комментарий над MODEL_* в
        # montage_orchestrator.py. Дропдаун шапки чата НЕ влияет.

        # 2026-05-06: подгружаем контекст сериала (Bible + другие
        # эпизоды) — Сценарист, Редактор, Чекер и Context Reviewer
        # используют его для соответствия характерам и сюжетной
        # целостности.
        show_context = self._load_show_context()

        # v1.0.61: Context Reviewer стал опциональным (default OFF).
        # Читаем toggle из QSettings («Использовать Context Reviewer»
        # в секции «🎬 Монтажная карта» Settings) и передаём в orchestrator.
        # Если OFF — стадия Reviewer'а пропускается, экономия ~2 мин.
        _qs = QSettings(_sa.APP_ORG, _sa.APP_NAME)
        use_reviewer = _qs.value(
            "montage/context_reviewer_enabled", False, type=bool)
        # v1.0.86 (этап 6): runtime-настройки оркестратора из админ-UI.
        # Default'ы такие же что в MontageOrchestratorThread.__init__:
        # opus_effort="low", chunk_timeout_opus=150, default=60.
        opus_effort = _qs.value("montage/opus_effort", "low", type=str)
        if opus_effort not in ("low", "medium", "high", "xhigh", "max"):
            opus_effort = "low"
        try:
            chunk_timeout_opus = int(_qs.value(
                "montage/chunk_timeout_opus_sec", 150))
        except (TypeError, ValueError):
            chunk_timeout_opus = 150
        try:
            chunk_timeout_default = int(_qs.value(
                "montage/chunk_timeout_default_sec", 60))
        except (TypeError, ValueError):
            chunk_timeout_default = 60
        # Видно в Console.app для bundled .app, в терминале для dev.
        # Греп: `[montage] runtime settings:`
        import sys as _sys_log
        _sys_log.stderr.write(
            f"[montage] runtime settings: opus_effort={opus_effort}, "
            f"chunk_timeout_opus={chunk_timeout_opus}, "
            f"chunk_timeout_default={chunk_timeout_default}\n")
        _sys_log.stderr.flush()

        from threads.montage_orchestrator import MontageOrchestratorThread
        t = MontageOrchestratorThread(
            claude_cli_path=cli,
            scenario_text=scenario,
            refs_summary=refs_summary,
            show_context=show_context,
            log_path=log_path,
            use_context_reviewer=use_reviewer,
            opus_effort=opus_effort,
            chunk_timeout_opus=chunk_timeout_opus,
            chunk_timeout_default=chunk_timeout_default,
            ep_id=ep_id,
            parent=self,
        )
        t.progress.connect(self._on_montage_progress)
        t.finished_ok.connect(self._on_montage_finished_ok)
        t.failed.connect(self._on_montage_failed)
        # 2026-05-07: per-episode авто-очистка по ep_id (тред знает свой
        # ep_id через замыкание на `ep_id` локально).
        t.finished.connect(
            lambda ep=ep_id: self._montage_threads.pop(ep, None))
        self._montage_threads[ep_id] = t
        # Сохраняем стартовый state, чтобы при возврате на этот эпизод
        # восстановить running CTA с этим прогрессом.
        self._montage_states[ep_id] = {
            'kind': 'running',
            'stage': 'montage_status_scriptwriter',
            'info': {},
        }
        # UI: переход в running-state ТОЛЬКО если юзер сейчас на этом
        # эпизоде. Если он уйдёт на другой ep_id — `set_episode` перерисует
        # CTA из `_montage_states`.
        if ep_id == self._ep_id:
            self._montage_cta.show_running('montage_status_scriptwriter')
        t.start()

    def _on_montage_resume(self):
        """v1.0.87 (этап 7D resume-фичи): клик «🔄 Продолжить» в
        KIND_RESUMABLE CTA. Загружает _agent_log_<ep>.json целиком,
        собирает АКТУАЛЬНЫЕ runtime-настройки (из QSettings / filesystem
        — НЕ из лога!), создаёт MontageOrchestratorThread с resume_from=
        <распарсенный лог>. Orchestrator на 7C извлекает montage_card +
        checker_report + last_completed_stage и пропускает уже сделанные
        этапы.

        Структура почти идентична `_on_montage_start` — отличия:
          • `resume_from=log_data` kwarg в конструкторе.
          • System-сообщение в чат через `tr('montage_resume_starting',
            stage=<human_name>)` перед t.start().
          • Если лог исчез между показом resumable и кликом — fallback
            на обычный `_on_montage_start()`.
        """
        ep_id = self._ep_id
        if not ep_id:
            return
        existing = self._montage_threads.get(ep_id)
        if existing is not None and existing.isRunning():
            return  # для этого эпизода уже бежит — игнор клика

        info = self._resumable_from_log(ep_id)
        if info is None:
            # Лог пропал / стал completed между показом CTA и кликом.
            # Безопасный fallback — обычный старт с нуля.
            try:
                import sys as _sys_log
                _sys_log.stderr.write(
                    f"[montage] resume: log gone for ep={ep_id}, "
                    f"falling back to fresh start\n")
                _sys_log.stderr.flush()
            except Exception:
                pass
            self._on_montage_start()
            return
        log_data = info["log_data"]
        last_stage = info["last_completed_stage"]

        cli = _sa.find_claude_cli()
        if not cli:
            self._montage_cta.show_failed(tr('new_ep_cli_missing'))
            return

        scenario = self._load_scenario_text()
        if not scenario:
            self._montage_cta.show_failed("Не нашёл текст сценария эпизода.")
            return

        refs_summary = self._build_refs_summary_for_orchestrator()
        if not refs_summary['locations']:
            self._montage_cta.show_failed("Нет залинкованных локаций.")
            return

        # Лог-путь тот же что и у обычного старта — orchestrator
        # перезапишет его с новым прогрессом через atomic dump.
        log_path = None
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if cur_show:
                log_path = (self._mw._project_root / "shows" / cur_show
                            / "output"
                            / f"_agent_log_{self._ep_id}.json")
        except Exception:
            pass

        show_context = self._load_show_context()

        # Runtime-настройки — АКТУАЛЬНЫЕ (из QSettings), не из лога.
        # Юзер мог изменить settings между fail и resume — берём свежие.
        _qs = QSettings(_sa.APP_ORG, _sa.APP_NAME)
        use_reviewer = _qs.value(
            "montage/context_reviewer_enabled", False, type=bool)
        opus_effort = _qs.value("montage/opus_effort", "low", type=str)
        if opus_effort not in ("low", "medium", "high", "xhigh", "max"):
            opus_effort = "low"
        try:
            chunk_timeout_opus = int(_qs.value(
                "montage/chunk_timeout_opus_sec", 150))
        except (TypeError, ValueError):
            chunk_timeout_opus = 150
        try:
            chunk_timeout_default = int(_qs.value(
                "montage/chunk_timeout_default_sec", 60))
        except (TypeError, ValueError):
            chunk_timeout_default = 60
        import sys as _sys_log
        _sys_log.stderr.write(
            f"[montage] resume runtime settings: opus_effort={opus_effort}, "
            f"chunk_timeout_opus={chunk_timeout_opus}, "
            f"chunk_timeout_default={chunk_timeout_default}, "
            f"last_completed={last_stage}\n")
        _sys_log.stderr.flush()

        from threads.montage_orchestrator import MontageOrchestratorThread
        t = MontageOrchestratorThread(
            claude_cli_path=cli,
            scenario_text=scenario,
            refs_summary=refs_summary,
            show_context=show_context,
            log_path=log_path,
            use_context_reviewer=use_reviewer,
            opus_effort=opus_effort,
            chunk_timeout_opus=chunk_timeout_opus,
            chunk_timeout_default=chunk_timeout_default,
            resume_from=log_data,
            ep_id=ep_id,
            parent=self,
        )
        t.progress.connect(self._on_montage_progress)
        t.finished_ok.connect(self._on_montage_finished_ok)
        t.failed.connect(self._on_montage_failed)
        t.finished.connect(
            lambda ep=ep_id: self._montage_threads.pop(ep, None))
        self._montage_threads[ep_id] = t
        self._montage_states[ep_id] = {
            'kind': 'running',
            'stage': 'montage_status_scriptwriter',
            'info': {},
        }
        # System-сообщение в чат — пользователь видит «Продолжаем
        # монтажку с этапа X» прямо в истории эпизода.
        try:
            stage_human = tr(f'montage_stage_name_{last_stage}')
            # Если ключа нет, tr() возвращает сам ключ → fallback на raw id.
            if stage_human.startswith('montage_stage_name_'):
                stage_human = last_stage
            line = tr('montage_resume_starting', stage=stage_human)
            _sa.append_chat_message(self._ep_id, "system", line, kind='system')
            self._render_message(line, kind='system')
        except Exception:
            traceback.print_exc()
        if ep_id == self._ep_id:
            self._montage_cta.show_running('montage_status_scriptwriter')
        t.start()
        # v1.0.88 (индикатор failed эпизодов): resume стартовал → состояние
        # на диске остаётся «running» (orchestrator перепишет dump'ом
        # вскоре), точка временно остаётся видимой. Явно refresh-аем чтобы
        # tooltip обновился со свежим stage если он успел сдвинуться.
        self._refresh_pill_indicators_safe()

    def _on_montage_start_fresh(self):
        """v1.0.87 (этап 7D resume-фичи): клик «🆕 Начать заново» в
        KIND_RESUMABLE CTA. Удаляет `_agent_log_<ep>.json` чтобы старый
        pipeline_state не мешал — после этого CTA через
        `_check_montage_ready` встанет в обычный idle. Потом запускаем
        стандартный `_on_montage_start()`.

        Используется когда юзер решил что старая монтажка плохая или
        refs изменились настолько, что resume бесполезен.
        """
        ep_id = self._ep_id
        if not ep_id:
            return
        log_path = self._agent_log_path_for_ep(ep_id)
        if log_path is not None and log_path.exists():
            try:
                log_path.unlink(missing_ok=True)
                import sys as _sys_log
                _sys_log.stderr.write(
                    f"[montage] start fresh: removed {log_path}\n")
                _sys_log.stderr.flush()
            except Exception:
                traceback.print_exc()
        # In-memory failed-state тоже сбрасываем — иначе после удаления
        # лога CTA через restore покажет failed snapshot.
        self._montage_states.pop(ep_id, None)
        # v1.0.88 (Stage 10): снимаем флаг montage_card_seen — старая
        # карта (если была) уже не релевантна; новая должна получить
        # «непросмотренный» статус (зелёная точка) когда будет готова.
        self._unmark_montage_card_seen(ep_id)
        # v1.0.88 (индикатор failed эпизодов): лог удалён → точка убирается
        # СРАЗУ (до того как фоновый таймер тикнет через 3с).
        self._refresh_pill_indicators_safe()
        self._on_montage_start()

    def _refresh_pill_indicators_safe(self) -> None:
        """v1.0.88 (индикатор failed эпизодов): мгновенный refresh красных
        точек на пилюлях эпизодов через MainWindow. Вызывается из
        montage-handler'ов после изменения disk-state лога (finished_ok /
        failed / start_fresh / resume) — иначе юзер ждал бы до 3с пока
        фоновый `_pill_indicator_timer` тикнет.

        Безопасный (try/except) — MainWindow может быть в полузакрытом
        состоянии при shutdown, или метод ещё не добавлен в старой версии
        (cross-version).
        """
        try:
            mw = getattr(self, '_mw', None)
            if mw is not None and hasattr(mw, '_refresh_episode_pill_indicators'):
                mw._refresh_episode_pill_indicators()
        except Exception as e:
            # v1.0.88 (Stage 11 diag): не просто traceback — явный stderr-маркер,
            # чтобы при следующем repro видно было что refresh упал на главном
            # экране (а не где-то в глубине pipeline).
            try:
                import sys as _sys_log
                _sys_log.stderr.write(
                    f"[pill] _refresh_pill_indicators_safe FAILED: "
                    f"{type(e).__name__}: {e}\n")
                _sys_log.stderr.flush()
            except Exception:
                pass
            traceback.print_exc()

    def _montage_ep_for_sender(self) -> Optional[str]:
        """Возвращает ep_id orchestrator-треда отправившего сигнал.

        2026-05-07: изначально искал в `_montage_threads` по `t is sender`.

        v1.0.88 (Stage 11 Bug 2 fix): берём ep_id напрямую из
        `sender()._ep_id` (поле устанавливается в orchestrator.__init__).
        Старая логика была race-уязвима: `finished.connect lambda` удаляет
        thread из `_montage_threads` ДО того как `finished_ok` сигнал
        доходит до handler'а → sender есть, но в dict его уже нет → return
        None → карта НЕ сохранялась → зелёная точка не появлялась на
        async-завершённом эпизоде.

        Поле `_ep_id` на orchestrator живёт всю его жизнь, не зависит от
        внешнего dict membership. Если orchestrator каким-то образом
        создан без ep_id (cross-version) — getattr default None,
        backward-compatible.
        """
        try:
            sender = self.sender()
            if sender is None:
                return None
            return getattr(sender, '_ep_id', None)
        except Exception:
            traceback.print_exc()
        return None

    def _on_montage_progress(self, stage: str, info: dict):
        """Слот сигнала progress от оркестратора. Обновляет CTA только
        если юзер сейчас на эпизоде этого треда — иначе сохраняем state
        и применим его в `set_episode` при возврате."""
        ep_id = self._montage_ep_for_sender()
        try:
            # Snapshot state для возврата.
            if ep_id is not None:
                self._montage_states[ep_id] = {
                    'kind': 'running',
                    'stage': stage,
                    'info': dict(info or {}),
                }
            # Если юзер на другом эпизоде — UI не трогаем.
            if ep_id is not None and ep_id != self._ep_id:
                return
            if stage == 'scriptwriter_running':
                self._montage_cta.show_running('montage_status_scriptwriter')
            elif stage == 'validator_running':
                self._montage_cta.show_running('montage_status_validator')
            elif stage == 'geometry_editor_running':
                # v1.0.78 (Bug 5): новая стадия из v1.0.75
                self._montage_cta.show_running(
                    'montage_status_geometry_editor')
            elif stage == 'editor_running':
                self._montage_cta.show_running(
                    'montage_status_editor',
                    errors_count=info.get('errors_count', 0))
            elif stage == 'validator_r2_running':
                # v1.0.78 (Bug 5): новая стадия из v1.0.76
                self._montage_cta.show_running(
                    'montage_status_validator_r2')
            elif stage == 'editor_r2_running':
                # v1.0.78 (Bug 5): новая стадия из v1.0.77
                self._montage_cta.show_running(
                    'montage_status_editor_r2',
                    errors_count=info.get('errors_count', 0))
            elif stage == 'validator_r3_running':
                # v1.0.78 (Bug 5): новая стадия из v1.0.77
                self._montage_cta.show_running(
                    'montage_status_validator_r3')
            elif stage == 'validator_done':
                if info.get('ok'):
                    self._montage_cta.show_running(
                        'montage_status_round_done_clean')
                else:
                    self._montage_cta.show_running(
                        'montage_status_round_done_errors',
                        errors_count=info.get('errors_count', 0))
            elif stage == 'context_reviewer_running':
                self._montage_cta.show_running(
                    'montage_status_context_reviewer')
            elif stage == 'context_reviewer_done':
                concerns_n = info.get('concerns_count', 0)
                if info.get('ok') or concerns_n == 0:
                    self._montage_cta.show_running(
                        'montage_status_context_reviewer_clean')
                else:
                    self._montage_cta.show_running(
                        'montage_status_context_reviewer_concerns',
                        concerns_count=concerns_n)
        except Exception:
            traceback.print_exc()

    def _on_montage_finished_ok(self, montage_card: dict,
                                  checker_report: dict,
                                  rounds_used: int,
                                  agent_log_path: str,
                                  agent_summary: dict):
        """Оркестратор завершил работу.

        v1.0.82: попап БОЛЬШЕ НЕ выскакивает автоматически.
        Карта сохраняется в episodes.json[ep]['montage_card'] полным
        форматом, в чате эпизода CTA переключается на «📂 Открыть
        монтажную карту». Юзер сам кликает когда хочет посмотреть.

        2026-05-06: `agent_summary` — компактный отчёт по работе агентов.
        2026-05-07: per-episode. State сбрасывается для ep_id треда.
        """
        ep_id = self._montage_ep_for_sender()
        # v1.0.88 (Stage 11 diag): лог в Console.app — видно дошёл ли
        # сигнал и правильный ли ep_id (False-negative для green dot
        # bug = ep_id=None из старого _montage_ep_for_sender; после
        # Stage 11 Bug 2 fix должен быть валидный ep_id).
        try:
            import sys as _sys_log
            _sys_log.stderr.write(
                f"[montage] finished_ok received: ep_id={ep_id}, "
                f"blocks={len(montage_card.get('blocks') or [])}, "
                f"self._ep_id={self._ep_id}\n")
            _sys_log.stderr.flush()
        except Exception:
            pass
        # Snapshot — карта готова, снимаем running state.
        if ep_id is not None:
            self._montage_states.pop(ep_id, None)

        # v1.0.82: сохраняем полную карту + checker_report + agent_summary
        # на диск независимо от того где юзер сейчас находится. Карта
        # переживёт перезапуск Studio и будет доступна через «📂 Открыть».
        if ep_id is not None:
            try:
                self._save_full_montage_card(
                    ep_id, montage_card,
                    checker_report=checker_report,
                    agent_summary=agent_summary,
                    rounds_used=rounds_used)
                try:
                    import sys as _sys_log
                    _sys_log.stderr.write(
                        f"[montage] saved card for ep={ep_id} to episodes.json\n")
                    _sys_log.stderr.flush()
                except Exception:
                    pass
            except Exception:
                traceback.print_exc()

        # v1.0.88 (индикатор failed эпизодов): карта готова → точка на
        # пилюле эпизода должна пропасть (status=completed теперь).
        self._refresh_pill_indicators_safe()
        try:
            import sys as _sys_log
            _sys_log.stderr.write(
                f"[montage] _refresh_pill_indicators_safe called after save "
                f"(ep={ep_id})\n")
            _sys_log.stderr.flush()
        except Exception:
            pass

        # Если юзер на этом эпизоде — переключаем CTA на «Открыть».
        # Если на другом — там CTA обновится при возврате через
        # _restore_montage_cta_for_current_ep (которая прочитает с диска).
        if ep_id is None or ep_id != self._ep_id:
            return
        try:
            self._montage_cta.show_open_map()
        except Exception:
            traceback.print_exc()

    def _on_montage_failed(self, reason: str):
        """Оркестратор не смог завершить.
        2026-05-07: state записывается per-ep_id. UI обновляется только
        если юзер на этом эпизоде."""
        ep_id = self._montage_ep_for_sender()
        if ep_id is not None:
            self._montage_states[ep_id] = {
                'kind': 'failed',
                'reason': reason or "",
            }
        # v1.0.88 (индикатор failed эпизодов): pipeline упал → если
        # orchestrator успел dump'нуть pipeline_state="failed", точка
        # появится на пилюле этого эпизода (и любого другого если
        # параллельно тоже падают).
        self._refresh_pill_indicators_safe()
        if ep_id is not None and ep_id != self._ep_id:
            return
        self._montage_cta.show_failed(reason)

    def _on_montage_confirm_storyboards(self, montage_card: dict):
        """Юзер кликнул «🎨 Делать сториборды» в popup'е сводки.

        Этап 2 (2026-05-06):
        1. Записываем blocks в episodes.json (чтобы pill'ы блоков
           отрендерились автоматически по существующему механизму
           `list_blocks_for_episode`).
        2. Переключаем юзера с chat-view на главное окно (грид шотов
           блока 1) — он сразу видит pill'ы и пустые карточки.
        3. Стартуем `StoryboardPipelineThread` — пишет .txt промпты
           блоков и шлёт сигнал MainWindow когда каждый готов; тот
           запускает GenerateThread по шотам.
        """
        try:
            # 1) Сохраняем карту в episodes.json[ep].blocks
            self._save_montage_card_to_episodes_json(montage_card)

            # 1.5) 2026-05-06: Скрываем CTA-карточку «Все рефы готовы»
            # сразу после клика «🎨 Делать сториборды». Карточка свою
            # работу выполнила — карта утверждена, юзер ушёл смотреть
            # шоты в гриде. Если бы оставили её на idle — выглядело бы
            # как будто ничего не запустилось.
            try:
                if hasattr(self, '_montage_cta') and self._montage_cta:
                    self._montage_cta.hide()
            except Exception:
                traceback.print_exc()

            # 2) Переключаем юзера на главное окно с гридом блоков.
            mw = getattr(self, '_mw', None)
            if mw is not None and self._ep_id:
                try:
                    # Триггерим перечитывание meta + переход на refs/блоки
                    # — `_render_block_pills` сам выберет первый блок.
                    mw._current_episode = self._ep_id
                    if hasattr(mw, '_render_block_pills'):
                        mw._render_block_pills()
                    # Стек контента: index 0 = блоки. Если у MainWindow
                    # есть animated-helper — можно использовать его, но
                    # прямой setCurrentIndex надёжнее.
                    if hasattr(mw, 'content_stack'):
                        mw.content_stack.setCurrentIndex(0)
                    if hasattr(mw, 'block_pills_container'):
                        mw.block_pills_container.show()
                except Exception:
                    traceback.print_exc()

            # 3) Стартуем pipeline записи .txt-промптов.
            self._start_storyboard_pipeline(montage_card)
        except Exception:
            traceback.print_exc()

    def _start_storyboard_pipeline(self, montage_card: dict):
        """Запускает StoryboardPipelineThread и подключает сигналы к
        MainWindow (он отвечает за per-shot генерацию через
        существующий GenerateThread + UI обновление shot_cards).
        """
        from threads.storyboard_pipeline import StoryboardPipelineThread

        cli = _sa.find_claude_cli()
        if not cli:
            self._render_message(
                "\n⚠ Claude CLI не найден — не могу запустить PromptWriter.\n",
                kind='err')
            return

        refs_summary = self._build_refs_summary_for_orchestrator()
        characters_dict = self._build_characters_dict(montage_card)

        # prompts_dir = shows/<slug>/output/prompts
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            project_root = getattr(self._mw, '_project_root', None)
            if not cur_show or project_root is None:
                self._render_message(
                    "\n⚠ Не нашёл текущий сериал — pipeline не стартовал.\n",
                    kind='err')
                return
            prompts_dir = (project_root / "shows" / cur_show
                           / "output" / "prompts")
            # 2026-05-06: Геометрия локаций для PromptWriter (точное
            # позиционирование персонажей в кадре, не описание мебели
            # словами). Файлы: refs/locations/<slug>_geometry.txt.
            locations_root = (project_root / "shows" / cur_show
                              / "refs" / "locations")
            geometry_context = self._build_geometry_context(
                montage_card, locations_root)
        except Exception:
            traceback.print_exc()
            return

        # 2026-05-09: модель PromptWriter прибита в StoryboardPipelineThread
        # (MODEL = "claude-opus-4-7"). Дропдаун шапки чата больше НЕ влияет.

        thread = StoryboardPipelineThread(
            claude_cli_path=cli,
            montage_card=montage_card,
            refs_summary=refs_summary,
            characters_dict=characters_dict,
            ep_id=self._ep_id or "",
            prompts_dir=prompts_dir,
            geometry_context=geometry_context,
            parent=self,
        )
        # Сигналы → MainWindow (он держит UI блоков/шотов и
        # GenerateThread'ы). Отдельные методы со своими handler'ами:
        mw = getattr(self, '_mw', None)
        if mw is not None and hasattr(mw, '_on_storyboard_block_prompt_ready'):
            thread.block_prompt_ready.connect(
                mw._on_storyboard_block_prompt_ready)
        if mw is not None and hasattr(mw, '_on_storyboard_block_failed'):
            thread.block_failed.connect(mw._on_storyboard_block_failed)
        if mw is not None and hasattr(mw, '_on_storyboard_pipeline_done'):
            thread.all_done.connect(mw._on_storyboard_pipeline_done)

        # Сохраняем ссылку чтобы тред не собрался GC до завершения.
        self._storyboard_pipeline_thread = thread
        thread.start()

        # 2026-05-06 Этап 3: ПАРАЛЛЕЛЬНО запускаем SeedancePipelineThread.
        # Пока Fast Gen занят шотами через NARWHAL — Opus свободен и
        # пишет Seedance промпты в фоне. UI кнопка «🎬 Промпт Seedance»
        # на блоке покажет готовый текст когда юзер кликнет.
        try:
            self._start_seedance_pipeline(montage_card, cli, refs_summary,
                                            characters_dict, project_root,
                                            cur_show, prompts_dir)
        except Exception:
            traceback.print_exc()

        # Сообщение в чат и в episode log.
        line = ("\n✓ Монтажная карта утверждена. Запускаю генерацию "
                "сторибордов — пиши промпты блоков и пускаю шоты "
                "по очереди в гриде.\n")
        if self._ep_id:
            _sa.append_chat_message(self._ep_id, "system", line, kind='ok')
            self._render_message(line, kind='ok')

    def _start_seedance_pipeline(self, montage_card: dict, cli: str,
                                    refs_summary: dict,
                                    characters_dict: Dict[str, str],
                                    project_root,
                                    cur_show: str,
                                    storyboard_prompts_dir):
        """Запускает SeedancePipelineThread параллельно с PromptWriter.
        Файлы: shows/<slug>/output/seedance/<ep>_block_N.txt.

        НЕ зависит от готовности шотов — может стартовать сразу после
        утверждения карты, потому что промпт пишется по карте + Bible.
        """
        from threads.seedance_pipeline import SeedancePipelineThread

        seedance_dir = (project_root / "shows" / cur_show
                        / "output" / "seedance")
        bible_path = (project_root / "shows" / cur_show / "bible.txt")
        bible_text = ""
        try:
            if bible_path.exists():
                bible_text = bible_path.read_text(encoding='utf-8')
        except Exception:
            bible_text = ""

        # Голосовые профили — единый файл для всех сериалов.
        voices_path = (project_root / "instructions"
                       / "ГОЛОСОВЫЕ_ПРОФИЛИ_ПЕРСОНАЖЕЙ.txt")
        voices_text = ""
        try:
            if voices_path.exists():
                voices_text = voices_path.read_text(encoding='utf-8')
        except Exception:
            voices_text = ""

        # 2026-05-06: Seedance промпты ВСЕГДА на Opus 4.7 (независимо
        # от того что выбрано юзером в шапке Studio для остальных
        # этапов). Промпты Seedance большие и сложные — Sonnet даёт
        # хуже качество. Карту/PromptWriter можно на Sonnet (быстрее),
        # а Seedance — только Opus.
        seedance_model = "claude-opus-4-7"
        thread = SeedancePipelineThread(
            claude_cli_path=cli,
            montage_card=montage_card,
            refs_summary=refs_summary,
            characters_dict=characters_dict,
            ep_id=self._ep_id or "",
            seedance_dir=seedance_dir,
            bible_text=bible_text,
            voice_profiles_text=voices_text,
            storyboard_prompts_dir=storyboard_prompts_dir,
            model=seedance_model,
            parent=self,
        )
        mw = getattr(self, '_mw', None)
        if mw is not None and hasattr(mw, '_on_seedance_block_ready'):
            thread.block_seedance_ready.connect(mw._on_seedance_block_ready)
        if mw is not None and hasattr(mw, '_on_seedance_block_failed'):
            thread.block_failed.connect(mw._on_seedance_block_failed)
        if mw is not None and hasattr(mw, '_on_seedance_pipeline_done'):
            thread.all_done.connect(mw._on_seedance_pipeline_done)

        self._seedance_pipeline_thread = thread
        thread.start()

    def _on_seedance_restart(self, ep_id: str):
        """v1.0.85: «🔄 Перезапустить» при зависшем Seedance pipeline.

        Юзер кликнул на seedance_btn когда тот был в restart-режиме
        (elapsed > 5 мин + файл не появился). Делаем:
          1. stop() текущего треда — терминирует живой claude-Popen
             (через Этап 1 уже умеет).
          2. pkill claude (Mac/Linux) / PowerShell Stop-Process (Win) —
             страховка от зомби если CLI после terminate не умер.
          3. Перечитываем монтажную карту с диска (она утверждена) +
             собираем те же параметры (refs_summary, characters_dict,
             cli, project_root, cur_show, storyboard_prompts_dir).
          4. Запускаем SeedancePipelineThread заново — idempotent skip
             из Этапа 1 пропустит уже-готовые блоки, догенерится только
             хвост (тот что подвис).
        """
        try:
            # 1. Стоп текущего треда (если ещё жив).
            old = getattr(self, '_seedance_pipeline_thread', None)
            if old is not None:
                try:
                    if hasattr(old, 'stop'):
                        old.stop()
                except Exception:
                    traceback.print_exc()

            # 2. Страховочное убийство зависших claude-subprocess'ов.
            #    Тот же паттерн что в `_on_montage_cancel` (cross-platform).
            try:
                import subprocess as _sp
                import sys as _sys
                if _sys.platform == 'win32':
                    ps_cmd = (
                        "Get-CimInstance Win32_Process "
                        "-Filter \"Name='claude.exe'\" "
                        "| Where-Object { $_.CommandLine -like '*--system-prompt*' } "
                        "| ForEach-Object { "
                        "  try { Stop-Process -Id $_.ProcessId -Force "
                        "-ErrorAction SilentlyContinue } catch {} }"
                    )
                    CREATE_NO_WINDOW = 0x08000000
                    _sp.run(['powershell', '-NoProfile', '-Command', ps_cmd],
                            capture_output=True, timeout=10,
                            creationflags=CREATE_NO_WINDOW)
                else:
                    _sp.run(['pkill', '-TERM', '-f', 'claude -p '],
                            capture_output=True, timeout=5)
            except Exception:
                traceback.print_exc()

            # 3. Восстанавливаем параметры запуска.
            card, _checker, _summary, _rounds = \
                self._load_full_montage_card(ep_id)
            if not card or not card.get('blocks'):
                self._render_message(
                    "\n⚠ Не нашёл монтажную карту для перезапуска "
                    "Seedance.\n", kind='err')
                return
            cli = _sa.find_claude_cli()
            if not cli:
                self._render_message(
                    "\n⚠ Claude CLI не найден — нечем перезапускать.\n",
                    kind='err')
                return
            mw = getattr(self, '_mw', None)
            cur_show = getattr(mw, '_current_show', None) if mw else None
            project_root = getattr(mw, '_project_root', None) if mw else None
            if not cur_show or project_root is None:
                self._render_message(
                    "\n⚠ Не нашёл текущий сериал — restart не стартовал.\n",
                    kind='err')
                return
            prompts_dir = (project_root / "shows" / cur_show
                           / "output" / "prompts")
            refs_summary = self._build_refs_summary_for_orchestrator()
            characters_dict = self._build_characters_dict(card)

            # 4. Запускаем заново. Этап 1 skip-existing подхватит уже
            #    сгенерированные блоки и не будет дёргать Opus впустую.
            self._start_seedance_pipeline(card, cli, refs_summary,
                                          characters_dict, project_root,
                                          cur_show, prompts_dir)
            line = (f"\n🔄 Перезапускаю Seedance pipeline для {ep_id} — "
                    f"уже-готовые блоки пропустятся.\n")
            _sa.append_chat_message(ep_id, "system", line, kind='ok')
            self._render_message(line, kind='ok')
        except Exception:
            traceback.print_exc()

    def _build_geometry_context(self, montage_card: dict,
                                  locations_root) -> Dict[str, str]:
        """Собирает {location_slug: geometry_text} для всех локаций
        упомянутых в карте.

        Для каждого block.location ищем файл
        `refs/locations/<slug>_geometry.txt` (создаётся при первичной
        генерации локации через ClaudeGeometryThread). Если файла нет —
        slug просто отсутствует в результате.

        Этот текст потом передаётся в PromptWriter в user_prompt чтобы
        писать ТОЧНОЕ позиционирование (Lora sits on the bed in centre
        of back wall — где «centre of back wall» из geometry). НЕ для
        описания мебели словами!
        """
        out: Dict[str, str] = {}
        slugs: set = set()
        for b in (montage_card.get('blocks') or []):
            loc = b.get('location')
            if isinstance(loc, str) and loc.strip():
                slugs.add(loc.strip())
        for slug in slugs:
            try:
                geo_path = locations_root / f"{slug}_geometry.txt"
                if geo_path.exists() and geo_path.is_file():
                    text = geo_path.read_text(encoding='utf-8').strip()
                    if text:
                        out[slug] = text
            except Exception:
                traceback.print_exc()
        return out

    def _build_characters_dict(self, montage_card: dict) -> Dict[str, str]:
        """Собирает {slug: english_name} из `scene_action` шотов
        утверждённой карты.

        Сценарист в `scene_action` пишет фразы вида
        "David from [@]img3", "Mark from [@]img4". Парсим их и
        связываем найденные имена с slug'ами из `block.characters`.
        Если для slug'а не нашли — оставляем title-case fallback.
        """
        import re
        chars_used: Dict[str, str] = {}
        # Slug-list: все character-slug'и упомянутые в карте
        slug_set: set = set()
        for b in (montage_card.get('blocks') or []):
            for slug in (b.get('characters') or []):
                if isinstance(slug, str):
                    slug_set.add(slug)
        # Поиск имён в scene_action: "<Word> from [@]img\d+"
        # — берём первое имя на каждый img-номер, потом мапим
        # slug → name по позиции в block.characters / refs_summary.
        # Для надёжности: title-case slug как fallback.
        for slug in slug_set:
            chars_used[slug] = slug.replace('_', ' ').title()
        # Попытка вытащить настоящие имена: пройдёмся по shots и
        # поищем "<Name> from [@]imgN". Поскольку привязка номера
        # к slug'у меняется блоками, делаем эвристику: если какое-то
        # имя встречается чаще fallback'а — заменяем для всех slug'ов
        # в этом блоке по позиции. Безопаснее всего оставлять
        # title-case, AI-PromptWriter сам всё равно использует имена
        # из scene_action.
        return chars_used

    def _save_montage_card_to_episodes_json(self, montage_card: dict):
        """Записывает blocks в episodes.json в формате который ожидает
        Studio (`{n: {name, shots: {m: description_ru}}}`).
        """
        path = self._ep_meta_path()
        if path is None:
            return
        try:
            import json as _json
            data = {}
            if path.exists():
                try:
                    data = _json.loads(path.read_text(encoding='utf-8')) or {}
                except Exception:
                    data = {}
            ep = data.setdefault(self._ep_id or '', {})
            blocks_out: dict = {}
            for b in (montage_card.get('blocks') or []):
                n = b.get('n')
                if n is None:
                    continue
                shots_out: dict = {}
                for s in (b.get('shots') or []):
                    sn = s.get('n')
                    if sn is None:
                        continue
                    shots_out[str(sn)] = s.get('description_ru', '')
                blocks_out[str(n)] = {
                    'name': b.get('name', ''),
                    'shots': shots_out,
                }
            ep['blocks'] = blocks_out
            path.write_text(
                _json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            traceback.print_exc()

    # ── v1.0.82: персистентность полной монтажной карты ──────────────

    def _save_full_montage_card(self, ep_id: str, montage_card: dict,
                                  checker_report: dict = None,
                                  agent_summary: dict = None,
                                  rounds_used: int = 1):
        """Пишет полную карту + сопутствующие отчёты в episodes.json
        для повторного открытия через CTA «📂 Открыть». Структура:

            episodes.json[ep_id] = {
                ...
                'montage_card': {<полная карта со всеми полями>},
                'montage_checker_report': {...},
                'montage_agent_summary': {...},
                'montage_rounds_used': int,
            }
        """
        path = self._ep_meta_path()
        if path is None or not ep_id:
            return
        try:
            import json as _json
            data = {}
            if path.exists():
                try:
                    data = _json.loads(path.read_text(encoding='utf-8')) or {}
                except Exception:
                    data = {}
            ep = data.setdefault(ep_id, {})
            # v1.0.88 (Stage 12): новая карта = unseen для юзера, даже если
            # предыдущую он открывал. Универсальный сброс, чтобы не зависеть
            # от того откуда пришло сохранение (Start, Resume, async-complete).
            ep.pop('montage_card_seen', None)
            ep['montage_card'] = montage_card
            if checker_report is not None:
                ep['montage_checker_report'] = checker_report
            if agent_summary is not None:
                ep['montage_agent_summary'] = agent_summary
            ep['montage_rounds_used'] = int(rounds_used or 1)
            path.write_text(
                _json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            traceback.print_exc()

    def _has_saved_montage_card(self, ep_id: str) -> bool:
        """True если на диске есть карта для эпизода (production-формат
        в episodes.json или диагностический fallback _agent_log_epN.json).

        v1.0.87 (этап А resume-фичи): fallback на _agent_log_*.json
        уточнён — теперь True ТОЛЬКО для логов с
        pipeline_state.status == "completed". Раньше любой существующий
        файл лога считался «карта готова», что приводило к UI-миганию:
        после fail Scriptwriter лог записывался с пустым stages, UI
        ложно показывал «📂 Открыть монтажную карту».

        Legacy логи без поля pipeline_state (созданные до v1.0.87)
        считаются completed — backward compat для эпизодов где
        монтажка делалась раньше.
        """
        if not ep_id:
            return False
        # 1. Production-формат — самый надёжный источник истины.
        path = self._ep_meta_path()
        if path is not None and path.exists():
            try:
                import json as _json
                data = _json.loads(path.read_text(encoding='utf-8')) or {}
                card = (data.get(ep_id) or {}).get('montage_card') or {}
                if card.get('blocks'):
                    return True
            except Exception:
                traceback.print_exc()
        # 2. Fallback: _agent_log_epN.json. v1.0.87 — парсим
        # pipeline_state.status вместо тупой existence-проверки.
        log_path = self._agent_log_path_for_ep(ep_id)
        if log_path is not None and log_path.exists():
            try:
                import json as _json
                data = _json.loads(log_path.read_text(encoding='utf-8')) or {}
                ps = data.get('pipeline_state')
                if ps is None:
                    # Legacy лог (до v1.0.87) — считаем completed.
                    return True
                return ps.get('status') == 'completed'
            except Exception:
                # Битый JSON — считаем что карты нет (избежать ложного
                # «Открыть карту» на corrupted state).
                traceback.print_exc()
                return False
        return False

    def _resumable_from_log(self, ep_id: str):
        """v1.0.87 (этап 7D resume-фичи): True если для эпизода есть
        упавший pipeline, который можно продолжить.

        Возвращает dict {"log_data", "last_completed_stage", "next_stage"}
        либо None. На любую ошибку парсинга / неподходящий status / битый
        log_path — возвращает None (caller рисует обычный idle/hide).

        Условия для resumable:
          • Файл `_agent_log_<ep>.json` существует и парсится.
          • `pipeline_state` в JSON присутствует (legacy логи без поля
            считаются completed → ресюмить нечего).
          • `pipeline_state.status` ∈ {"failed", "running"} (completed —
            это уже готовая карта, для неё `_has_saved_montage_card`
            показывает "Открыть").
          • `last_completed_stage` не None и не "finalize" (finalize =
            pipeline дошёл до конца, нечего продолжать).
        """
        if not ep_id:
            return None
        log_path = self._agent_log_path_for_ep(ep_id)
        if log_path is None or not log_path.exists():
            return None
        try:
            import json as _json
            data = _json.loads(log_path.read_text(encoding='utf-8')) or {}
        except Exception:
            traceback.print_exc()
            return None
        ps = data.get("pipeline_state")
        if not isinstance(ps, dict):
            # Legacy лог (до v1.0.87) — считаем completed, не ресюмим.
            return None
        status = ps.get("status")
        last = ps.get("last_completed_stage")
        nxt = ps.get("next_stage")
        if status not in ("failed", "running"):
            return None
        if not last or last == "finalize":
            return None
        return {
            "log_data": data,
            "last_completed_stage": last,
            "next_stage": nxt,
        }

    def _agent_log_path_for_ep(self, ep_id: str):
        """Путь к диагностическому логу агентов конкретного эпизода
        (`shows/<slug>/output/_agent_log_<ep>.json`), либо None."""
        try:
            cur_show = getattr(self._mw, '_current_show', None)
            if not cur_show or not ep_id:
                return None
            from pathlib import Path as _P
            return (self._mw._project_root / "shows" / cur_show
                    / "output" / f"_agent_log_{ep_id}.json")
        except Exception:
            return None

    def _load_full_montage_card(self, ep_id: str):
        """Читает полную карту с fallback'ом.
        Returns: (montage_card, checker_report, agent_summary, rounds_used)
                 либо (None, None, None, 1) если ничего не найдено.
        """
        if not ep_id:
            return (None, None, None, 1)
        import json as _json
        # 1. Production-формат в episodes.json
        path = self._ep_meta_path()
        if path is not None and path.exists():
            try:
                data = _json.loads(path.read_text(encoding='utf-8')) or {}
                ep = data.get(ep_id) or {}
                card = ep.get('montage_card') or {}
                if card.get('blocks'):
                    return (
                        card,
                        ep.get('montage_checker_report') or {
                            "ok": True, "errors": [], "report": []},
                        ep.get('montage_agent_summary') or {},
                        int(ep.get('montage_rounds_used') or 1),
                    )
            except Exception:
                traceback.print_exc()
        # 2. Fallback: _agent_log_epN.json — reverse-search последней
        # stage с result.blocks.
        log_path = self._agent_log_path_for_ep(ep_id)
        if log_path is None or not log_path.exists():
            return (None, None, None, 1)
        try:
            log = _json.loads(log_path.read_text(encoding='utf-8'))
            for s in reversed(log.get('stages', []) or []):
                res = s.get('result') or {}
                if isinstance(res, dict) and res.get('blocks'):
                    # Pseudo-summary из последней validator stage
                    return (
                        res,
                        {"ok": True, "errors": [], "report": []},
                        {},  # agent_summary недоступен в fallback'е
                        int(log.get('rounds_used') or 1),
                    )
        except Exception:
            traceback.print_exc()
        return (None, None, None, 1)

    def _delete_full_montage_card(self, ep_id: str):
        """v1.0.83: удаляет ВСЁ относящееся к монтажной карте эпизода:

        В episodes.json[ep_id] — снимает поля:
          • montage_card (полная карта v1.0.82)
          • montage_checker_report
          • montage_agent_summary
          • montage_rounds_used
          • blocks (production-формат для StoryboardPipeline)

        На диске — удаляет ФИЗИЧЕСКИ:
          • shows/<slug>/output/_agent_log_<ep>.json

        НЕ трогает (по требованию юзера):
          • output/seedance/* — промпты Seedance остаются
          • output/storyboards/* — сториборды остаются

        Cross-platform: pathlib.Path.unlink(missing_ok=True) — Mac/Win OK.

        Это необходимо потому что `_has_saved_montage_card` имеет
        fallback на _agent_log: после удаления только полей в
        episodes.json fallback бы заново возвращал True и CTA
        «📂 Открыть» возвращался. Физическое удаление лога делает
        удаление окончательным.
        """
        if not ep_id:
            return
        # 1. Поля в episodes.json
        path = self._ep_meta_path()
        if path is not None and path.exists():
            try:
                import json as _json
                data = _json.loads(path.read_text(encoding='utf-8')) or {}
                ep = data.get(ep_id) or {}
                for k in ('montage_card', 'montage_checker_report',
                           'montage_agent_summary', 'montage_rounds_used',
                           'blocks'):
                    ep.pop(k, None)
                data[ep_id] = ep
                path.write_text(
                    _json.dumps(data, ensure_ascii=False, indent=2),
                    encoding='utf-8'
                )
            except Exception:
                traceback.print_exc()
        # 2. Физическое удаление _agent_log_epN.json
        log_path = self._agent_log_path_for_ep(ep_id)
        if log_path is not None:
            try:
                log_path.unlink(missing_ok=True)
            except Exception:
                traceback.print_exc()

    def _is_storyboard_or_seedance_running(self) -> bool:
        """v1.0.82: блокировка кнопки «🗑 Удалить» если активен
        пайплайн сторибордов или Seedance — иначе можно поломать
        текущую генерацию."""
        for attr in ('_storyboard_pipeline_thread',
                      '_seedance_pipeline_thread'):
            t = getattr(self, attr, None)
            try:
                if t is not None and t.isRunning():
                    return True
            except Exception:
                pass
        return False

    def _open_montage_summary_dialog(self, ep_id: str):
        """v1.0.82: открыть попап с картой из диска. Подключает 2 сигнала
        диалога: confirm_storyboards (как раньше) и delete_card."""
        try:
            card, checker_report, agent_summary, rounds_used = \
                self._load_full_montage_card(ep_id)
            if not card or not card.get('blocks'):
                return
            dlg = MontageSummaryDialog(
                montage_card=card,
                checker_report=checker_report,
                rounds_used=rounds_used,
                agent_summary=agent_summary,
                parent=self)
            # Блокируем «Удалить» если идёт пайплайн
            if self._is_storyboard_or_seedance_running():
                dlg.set_delete_enabled(
                    False, tr('montage_delete_blocked_pipeline'))
            dlg.confirm_storyboards.connect(
                lambda c=card: self._on_montage_confirm_storyboards(c))
            dlg.delete_card.connect(
                lambda d=dlg, e=ep_id: self._on_montage_delete_card(d, e))
            dlg.exec()
        except Exception:
            traceback.print_exc()

    def _on_open_map_clicked(self):
        """Юзер кликнул «📂 Открыть монтажную карту» в CTA.

        v1.0.88 (Stage 10): ДО открытия попапа — помечаем карту как
        просмотренную (`montage_card_seen=True` в episodes.json) и
        рефрешим пилюли. Зелёная точка пропадает СРАЗУ при клике,
        не дожидаясь закрытия попапа — юзер уже знает что карта готова
        в момент клика, держать индикатор во время просмотра избыточно.

        Race с self._refresh_pill_indicators_safe — нет: write_text
        в `_mark_montage_card_seen` синхронный, refresh читает свежий
        episodes.json через read_episodes_meta.
        """
        ep_id = self._ep_id
        if not ep_id:
            return
        self._mark_montage_card_seen(ep_id)
        self._refresh_pill_indicators_safe()
        self._open_montage_summary_dialog(ep_id)

    def _mark_montage_card_seen(self, ep_id: str) -> None:
        """v1.0.88 (Stage 10): записывает флаг
        `episodes.json[ep_id]['montage_card_seen'] = True`. Используется
        в `_on_open_map_clicked` для исчезновения зелёной индикаторной
        точки на пилюле.

        Безопасно вызывать даже если карты в episodes.json нет — флаг
        просто ляжет рядом, никаких других полей не трогает.
        """
        path = self._ep_meta_path()
        if path is None or not ep_id:
            return
        try:
            import json as _json
            data = {}
            if path.exists():
                try:
                    data = _json.loads(path.read_text(encoding='utf-8')) or {}
                except Exception:
                    data = {}
            ep = data.setdefault(ep_id, {})
            if ep.get('montage_card_seen') is True:
                return  # уже стоит, не переписываем
            ep['montage_card_seen'] = True
            path.write_text(
                _json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            traceback.print_exc()

    def _unmark_montage_card_seen(self, ep_id: str) -> None:
        """v1.0.88 (Stage 10): снимает флаг `montage_card_seen` —
        вызывается из `_on_montage_start_fresh` (юзер начал заново,
        старая карта уже не релевантна, новая должна снова получить
        «непросмотренный» статус когда будет готова).
        """
        path = self._ep_meta_path()
        if path is None or not ep_id:
            return
        try:
            import json as _json
            if not path.exists():
                return
            try:
                data = _json.loads(path.read_text(encoding='utf-8')) or {}
            except Exception:
                return
            ep = data.get(ep_id)
            if not isinstance(ep, dict):
                return
            if 'montage_card_seen' not in ep:
                return
            ep.pop('montage_card_seen', None)
            path.write_text(
                _json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception:
            traceback.print_exc()

    def _on_montage_delete_card(self, dlg, ep_id: str):
        """Юзер кликнул «🗑 Удалить» в попапе. Подтверждение + проверка
        активных пайплайнов + cleanup + закрытие попапа."""
        # Защита от race: если успели запустить пайплайн пока попап открыт
        if self._is_storyboard_or_seedance_running():
            try:
                QMessageBox.warning(
                    dlg,
                    tr('montage_delete_confirm_title'),
                    tr('montage_delete_blocked_pipeline'))
            except Exception:
                traceback.print_exc()
            return
        # Подтверждение
        try:
            m = QMessageBox(dlg)
            m.setIcon(QMessageBox.Icon.Warning)
            m.setWindowTitle(tr('montage_delete_confirm_title'))
            m.setText(tr('montage_delete_confirm_text'))
            yes = m.addButton(tr('montage_delete_confirm_yes'),
                               QMessageBox.ButtonRole.DestructiveRole)
            no = m.addButton(tr('montage_delete_confirm_no'),
                              QMessageBox.ButtonRole.RejectRole)
            m.setDefaultButton(no)
            m.exec()
            if m.clickedButton() is not yes:
                return
        except Exception:
            traceback.print_exc()
            return
        # Удаление
        try:
            self._delete_full_montage_card(ep_id)
        except Exception:
            traceback.print_exc()
        # Закрыть попап и обновить CTA
        try:
            dlg.close()
        except Exception:
            traceback.print_exc()
        try:
            # После удаления карты — возврат к стандартной логике CTA.
            self._montage_cta.hide()
            self._check_montage_ready()
        except Exception:
            traceback.print_exc()
