# -*- coding: utf-8 -*-
"""
widgets/prompt_retry_dialog.py — popup ошибки генерации актёрского рефа
с AI-предложениями смягчённых вариантов промпта.

UX: когда GenerateActorRefThread эмитит `error` с текстом
content-moderation-отказа, ActorsView показывает этот диалог. Юзер видит:
- Свой исходный текст описания (который зарезала модерация).
- Полный текст ошибки от API.
- Спиннер «🤔 Думаю над альтернативами...» пока работает SoftenPromptThread.
- После окончания: до 3 AI-предложенных смягчённых вариантов.
- Под каждым — кнопка «↻ Повторить с этим вариантом».
- Кнопка «✕ Закрыть» снизу.

Клик по варианту → диалог эмитит `retry_with(text)` и закрывается.
ActorsView ловит сигнал, открывает CreateActorRefDialog с
prefill_description=text — юзер может посмотреть/поправить и нажать
«Сгенерировать».

Тред создаёт и стопает сам диалог. Юзеру не нужно знать про QThread.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QScrollArea, QWidget,
)

from i18n import tr


class PromptRetryDialog(QDialog):
    """Popup ошибки генерации с AI-предложениями смягчённых промптов."""

    # Эмит при клике «↻ Повторить с этим вариантом» — содержит выбранный
    # текст. Если юзер закрыл диалог через «✕ Закрыть» — сигнал не
    # эмитится (диалог просто reject()'ится).
    retry_with = pyqtSignal(str)

    def __init__(self, actor_display: str, original_prompt: str,
                 api_error: str, project_root: Optional[Path] = None,
                 model: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.original_prompt = (original_prompt or "").strip()
        self.api_error = (api_error or "").strip()
        self.project_root = project_root
        self.model = model
        self._soften_thread = None
        self._dot_step = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._tick_dots)

        self.setWindowTitle(tr('actor_error_title'))
        self.setModal(True)
        self.resize(640, 560)
        self.setStyleSheet(
            "QDialog { background:#15101e; }"
            "QLabel#err-title { color:#ffd0d0; font-size:15px;"
            " font-weight:700; }"
            "QLabel#err-section { color:#cfcfcf; font-size:12px;"
            " font-weight:700; letter-spacing:1px; }"
            "QFrame#err-box { background:#221616; border:1px solid #5a2a2a;"
            " border-radius:8px; }"
            "QLabel#err-box-text { color:#ffe0e0; font-size:13px;"
            " background:transparent; }"
            "QFrame#orig-box { background:#1a1424; border:1px solid #2a1f3d;"
            " border-radius:8px; }"
            "QLabel#orig-box-text { color:#cfcfcf; font-size:13px;"
            " background:transparent; font-style:italic; }"
            "QFrame#variant-box { background:#1a2638;"
            " border:1px solid #4d6a8a; border-radius:8px; }"
            "QFrame#variant-box:hover { border-color:#7d9bdb; }"
            "QLabel#variant-text { color:#d8e8ff; font-size:13px;"
            " background:transparent; }"
            "QLabel#loading-text { color:#ffaa44; font-size:13px;"
            " font-family:'Menlo','Consolas',monospace;"
            " background:transparent; padding:14px 0; }"
            "QPushButton#variant-btn { background:#3a5a3a; color:#d8ffd8;"
            " border:1px solid #4d8a4d; border-radius:6px;"
            " padding:8px 14px; font-size:12px; font-weight:600; }"
            "QPushButton#variant-btn:hover { background:#4d7a4d; color:#fff;"
            " border-color:#6dba6d; }"
            "QPushButton#close-btn { background:transparent; color:#aaa;"
            " border:1px solid #3a2c52; border-radius:6px;"
            " padding:8px 16px; font-size:12px; }"
            "QPushButton#close-btn:hover { color:#fff; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # Заголовок «✗ Ошибка генерации» + «для актёра X»
        title = QLabel(tr('prompt_retry_title', actor=actor_display))
        title.setObjectName("err-title")
        title.setWordWrap(True)
        outer.addWidget(title)

        # Скролл — на случай длинного исходника + 3 варианта
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background:transparent;"
                             " border:none; }")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(10)

        # ── Секция: что отправили ─────────────────────────────────────
        sec_orig = QLabel(tr('prompt_retry_original'))
        sec_orig.setObjectName("err-section")
        ilay.addWidget(sec_orig)
        orig_frame = QFrame()
        orig_frame.setObjectName("orig-box")
        ofl = QVBoxLayout(orig_frame)
        ofl.setContentsMargins(12, 10, 12, 10)
        orig_text = QLabel(self.original_prompt or "—")
        orig_text.setObjectName("orig-box-text")
        orig_text.setWordWrap(True)
        orig_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        ofl.addWidget(orig_text)
        ilay.addWidget(orig_frame)

        # ── Секция: что ответил API ───────────────────────────────────
        sec_api = QLabel(tr('prompt_retry_api_msg'))
        sec_api.setObjectName("err-section")
        ilay.addWidget(sec_api)
        err_frame = QFrame()
        err_frame.setObjectName("err-box")
        efl = QVBoxLayout(err_frame)
        efl.setContentsMargins(12, 10, 12, 10)
        err_text = QLabel(self.api_error or "—")
        err_text.setObjectName("err-box-text")
        err_text.setWordWrap(True)
        err_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        efl.addWidget(err_text)
        ilay.addWidget(err_frame)

        # ── Секция: предложенные варианты (loading / ready / error) ────
        ilay.addSpacing(4)
        self.suggestions_section_lbl = QLabel(
            tr('prompt_retry_suggestions'))
        self.suggestions_section_lbl.setObjectName("err-section")
        ilay.addWidget(self.suggestions_section_lbl)

        # Loading-фраза (видна пока тред бежит)
        self.loading_lbl = QLabel(tr('prompt_retry_loading'))
        self.loading_lbl.setObjectName("loading-text")
        self.loading_lbl.setWordWrap(True)
        ilay.addWidget(self.loading_lbl)

        # Контейнер для вариантов (наполняется в set_variants)
        self.variants_container = QWidget()
        self.variants_container.setStyleSheet("background:transparent;")
        self.variants_layout = QVBoxLayout(self.variants_container)
        self.variants_layout.setContentsMargins(0, 0, 0, 0)
        self.variants_layout.setSpacing(10)
        self.variants_container.hide()
        ilay.addWidget(self.variants_container)

        ilay.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, stretch=1)

        # ── Низ: «Закрыть» ────────────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.close_btn = QPushButton(tr('actor_error_dismiss'))
        self.close_btn.setObjectName("close-btn")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.reject)
        bottom.addWidget(self.close_btn)
        outer.addLayout(bottom)

        # Старт спиннера для loading-фразы
        self._dot_timer.start()
        # Запускаем AI-тред если есть project_root
        if self.project_root is not None and self.original_prompt:
            self._launch_soften_thread()
        else:
            # Без AI — сразу в state «не нашли вариантов»
            self._show_no_variants()

    def _launch_soften_thread(self):
        """Старт SoftenPromptThread — тред живёт в этом диалоге, на
        reject/accept стопаем."""
        try:
            from threads.soften_prompt import SoftenPromptThread
            self._soften_thread = SoftenPromptThread(
                self.project_root,
                self.original_prompt,
                api_error=self.api_error,
                model=self.model,
                parent=self)
            self._soften_thread.results.connect(self._on_results)
            self._soften_thread.error.connect(self._on_error)
            self._soften_thread.start()
        except Exception:
            import traceback
            traceback.print_exc()
            self._show_no_variants()

    def _tick_dots(self):
        """Анимация точек после loading-фразы пока тред работает."""
        if not self.loading_lbl.isVisible():
            return
        self._dot_step = (self._dot_step + 1) % 4
        dots = ["·   ", "··  ", "··· ", "····"][self._dot_step]
        self.loading_lbl.setText(
            f"{tr('prompt_retry_loading')} {dots}")

    def _on_results(self, variants: list):
        """AI вернул варианты — показываем кнопки."""
        self._dot_timer.stop()
        self.loading_lbl.hide()
        if not variants:
            self._show_no_variants()
            return
        # Очищаем контейнер на всякий случай
        self._clear_variants_container()
        for v in variants[:3]:
            self._add_variant_card(str(v))
        self.variants_container.show()

    def _on_error(self, msg: str):
        """AI не смог — показываем подсказку без кнопок."""
        self._dot_timer.stop()
        self.loading_lbl.hide()
        self._show_no_variants()

    def _clear_variants_container(self):
        while self.variants_layout.count():
            item = self.variants_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _add_variant_card(self, text: str):
        """Карточка одного варианта: текст + кнопка «↻ Повторить»."""
        vf = QFrame()
        vf.setObjectName("variant-box")
        vfl = QVBoxLayout(vf)
        vfl.setContentsMargins(12, 10, 12, 10)
        vfl.setSpacing(8)
        vt = QLabel(text)
        vt.setObjectName("variant-text")
        vt.setWordWrap(True)
        vt.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        vfl.addWidget(vt)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn = QPushButton(tr('prompt_retry_use_variant'))
        btn.setObjectName("variant-btn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _checked=False, t=text: self._on_variant_clicked(t))
        btn_row.addWidget(btn)
        vfl.addLayout(btn_row)
        self.variants_layout.addWidget(vf)

    def _show_no_variants(self):
        """AI не предложил вариантов — общий совет."""
        self._dot_timer.stop()
        self.loading_lbl.hide()
        self._clear_variants_container()
        hint = QLabel(tr('prompt_retry_no_variants'))
        hint.setStyleSheet(
            "color:#aaa; font-size:12px; padding:8px 0;"
            " background:transparent;")
        hint.setWordWrap(True)
        self.variants_layout.addWidget(hint)
        self.variants_container.show()

    def _on_variant_clicked(self, text: str):
        """Юзер выбрал вариант — эмитим и закрываем диалог."""
        try:
            self.retry_with.emit(text or "")
        except Exception:
            pass
        self._stop_soften_sync()
        self.accept()

    def _stop_soften_sync(self):
        """Останавливает soften-тред и ЖДЁТ его реального завершения перед
        разрушением диалога. Вызывается на ВСЕХ путях закрытия (reject /
        accept / closeEvent) — иначе живой QThread-ребёнок (parent=self)
        попадает в teardown QDialog → «QThread: Destroyed while thread is
        still running» → SIGABRT. Никогда не падает."""
        try:
            self._dot_timer.stop()
        except Exception:
            pass
        th = self._soften_thread
        if th is None:
            return
        try:
            if not th.isRunning():
                return
            th.stop()                       # terminate подпроцесса + флаг
            if not th.wait(2000):           # ждём до 2с реального выхода run()
                self._diag_log_soften_timeout(
                    "soften thread did not stop within 2000ms after terminate")
        except Exception:
            pass

    def _diag_log_soften_timeout(self, msg: str):
        """Best-effort лог [SOFTEN-STOP-TIMEOUT] в
        `shows/<active>/_studio_diag.log`; при любой проблеме — stderr.
        Никогда не падает. Cross-platform: pathlib + open(encoding='utf-8'),
        без subprocess/shell. `import storyboard_app` — ленивый (внутри
        функции), чтобы не словить циклический импорт в subpackage."""
        try:
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} [SOFTEN-STOP-TIMEOUT] {msg}\n"
            wrote = False
            try:
                if self.project_root is not None:
                    import storyboard_app as _sa  # lazy — без module-level импорта
                    show = _sa.get_current_show(self.project_root)
                    if show:
                        log_path = (Path(self.project_root)
                                    / "shows" / show / "_studio_diag.log")
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(log_path, "a", encoding="utf-8") as f:
                            f.write(line)
                        wrote = True
            except Exception:
                pass
            if not wrote:
                import sys as _sys
                _sys.stderr.write(line)
        except Exception:
            pass

    def reject(self):
        """Перед закрытием стопаем И ЖДЁМ тред (см. _stop_soften_sync)."""
        self._stop_soften_sync()
        super().reject()

    def closeEvent(self, event):
        """Закрытие крестиком окна (✕) идёт через closeEvent, НЕ через
        reject(). Гасим soften-тред перед teardown QDialog — иначе живой
        QThread-ребёнок → SIGABRT (тот же путь что reject/accept)."""
        self._stop_soften_sync()
        super().closeEvent(event)
