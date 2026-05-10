# -*- coding: utf-8 -*-
"""
widgets/character_outfit_picker.py — карточка-виджет «3 варианта одежды».

Показывается в чате эпизода вместо обычной GenButton-логики для
character'а после клика «🎨 Сгенерировать». Долг 13.

UX-состояния:
  • loading — «🤔 Подбираю варианты одежды для «{name}»…» + бегущие точки.
  • ready   — заголовок + 3 кнопки с текстом одежды + «↻ Ещё 3 варианта».
  • error   — «✗ Не получилось…» + кнопка «↻ Попробовать снова».

Сигналы:
  • variant_chosen(text) — клик по одному из 3 вариантов.
  • retry_requested()    — клик «↻ Ещё 3 варианта» / повтор после ошибки.
  • cancel_requested()   — клик по «✕» (закрыть карточку без выбора).

Виджет НЕ запускает тред сам — это делает caller (EpisodeChatView).
Тред зовёт публичные методы set_loading() → set_variants(list) /
set_error(msg).
"""

from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QSizePolicy,
)

from i18n import tr


class _ClickableLabelButton(QFrame):
    """2026-05-10: переписан с QPushButton на QFrame+QLabel.

    Корень бага «пустые окошки вариантов» (v1.0.35): QPushButton в
    Qt 6 НЕ поддерживает word-wrap нативно. Длинные варианты одежды
    (~100 символов) рендерились пустыми/обрезанными, кнопка визуально
    схлопывалась по высоте. QSS `text-align:left` + padding капризно
    работает в Qt 6.x.

    Решение: QFrame-контейнер с внутренним QLabel(setWordWrap=True).
    Click обрабатывается через mousePressEvent + custom signal.
    Высота автоматически растёт под содержимое.

    Параметр `obj_name` определяет CSS-селектор: `outfit-variant`
    (фиолетовая solid-граница) или `outfit-custom` (синяя dashed).
    """

    clicked = pyqtSignal()

    def __init__(self, text: str, obj_name: str = "outfit-variant",
                 parent=None):
        super().__init__(parent)
        self.setObjectName(obj_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(0)
        self._label = QLabel(text)
        self._label.setObjectName(f"{obj_name}-text")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Preferred)
        # Текст не интерактивен — клики идут на QFrame через bubble-up.
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction)
        # Прокидываем клик с лейбла на родителя (без него Qt может
        # съесть клик внутри label area).
        self._label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._label)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# Backwards-compat alias — старые имена ссылок.
_VariantButton = _ClickableLabelButton


class CharacterOutfitPicker(QFrame):
    """Карточка с 3 вариантами одежды для character'а в чате."""

    variant_chosen = pyqtSignal(str)
    retry_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    custom_requested = pyqtSignal()  # «✎ Придумаю описание сам»

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._state = "loading"   # loading / ready / error
        self._variants: List[str] = []
        self._dot_step = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._setup_ui()
        self._apply_state()

    # ── UI ────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            "CharacterOutfitPicker { background:#1a1424;"
            " border:1px solid #3a2a55; border-radius:8px; }"
            "CharacterOutfitPicker[state=\"loading\"] {"
            " border-color:#7a4ad8; }"
            "CharacterOutfitPicker[state=\"error\"] {"
            " border-color:#8a4d4d; background:#221616; }"
            "QLabel#outfit-title { color:#cfcfcf; font-size:13px;"
            " font-weight:600; }"
            "QLabel#outfit-hint { color:#888; font-size:11px; }"
            "QLabel#outfit-loading { color:#ffaa44; font-size:12px;"
            " font-family:'Menlo','Consolas',monospace; }"
            "QLabel#outfit-error { color:#cc6666; font-size:12px; }"
            # 2026-05-10: variant и custom кнопки переписаны на
            # QFrame+QLabel. CSS-селекторы:
            #   QFrame#outfit-variant — внешний вид кнопки.
            #   QLabel#outfit-variant-text — цвет/размер текста.
            # padding переместили внутрь QFrame layout (см. _ClickableLabelButton),
            # чтобы word-wrap на QLabel работал стабильно.
            "QFrame#outfit-variant {"
            " background:#2a1f3d; border:1px solid #4a3a72;"
            " border-radius:6px; }"
            "QFrame#outfit-variant:hover {"
            " background:#3a2a52; border-color:#6e4cc4; }"
            "QLabel#outfit-variant-text {"
            " color:#e8e0ff; font-size:13px; background:transparent; }"
            "QFrame#outfit-variant:hover QLabel#outfit-variant-text {"
            " color:#fff; }"
            "QFrame#outfit-custom {"
            " background:#1a1424; border:1px dashed #4d6a8a;"
            " border-radius:6px; }"
            "QFrame#outfit-custom:hover {"
            " background:#1a2638; border-color:#7d9bdb; }"
            "QLabel#outfit-custom-text {"
            " color:#a8c8ff; font-size:13px; background:transparent; }"
            "QFrame#outfit-custom:hover QLabel#outfit-custom-text {"
            " color:#d8e8ff; }"
            "QPushButton#outfit-retry { background:transparent;"
            " color:#a8c8ff; border:1px solid #4d6a8a; border-radius:6px;"
            " padding:6px 12px; font-size:12px; }"
            "QPushButton#outfit-retry:hover { background:#1a2638;"
            " color:#d8e8ff; }"
            "QPushButton#outfit-cancel { background:transparent;"
            " color:#aaa; border:1px solid #4a4a4a; border-radius:6px;"
            " padding:6px 12px; font-size:12px; }"
            "QPushButton#outfit-cancel:hover { background:#2a2a2a;"
            " color:#ddd; }")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        # Шапка: «👤 name» (видна всегда)
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        self.title_lbl = QLabel(f"👤 {self._name}")
        self.title_lbl.setObjectName("outfit-title")
        head_row.addWidget(self.title_lbl)
        head_row.addStretch()
        outer.addLayout(head_row)

        # Loading-фраза (видна в loading)
        self.loading_lbl = QLabel("")
        self.loading_lbl.setObjectName("outfit-loading")
        self.loading_lbl.setWordWrap(True)
        outer.addWidget(self.loading_lbl)

        # Error-строка (видна в error)
        self.error_lbl = QLabel("")
        self.error_lbl.setObjectName("outfit-error")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.hide()
        outer.addWidget(self.error_lbl)

        # 3 кнопки-варианта + 4-я «Придумаю описание сам» (видны в ready).
        # Между кнопками — отдельный layout с большим spacing'ом для воздуха.
        var_layout = QVBoxLayout()
        var_layout.setSpacing(8)
        var_layout.setContentsMargins(0, 0, 0, 0)
        self.var_btns: List[_ClickableLabelButton] = []
        for _ in range(3):
            btn = _ClickableLabelButton("", obj_name="outfit-variant",
                                         parent=self)
            btn.clicked.connect(self._on_variant_clicked)
            btn.hide()
            var_layout.addWidget(btn)
            self.var_btns.append(btn)
        # 4-я кнопка «✎ Придумаю описание сам» — вместо одного из 3 вариантов
        # юзер может выбрать ручной ввод. Тоже переключает на «Актёры» с
        # баннером, но с пустым описанием — юзер заполнит в попапе сам.
        # 2026-05-10: переписана на _ClickableLabelButton (QFrame+QLabel)
        # для UX-симметрии с variant'ами + чтобы текст word-wrap'ился.
        self.custom_btn = _ClickableLabelButton(
            tr('outfit_picker_custom'),
            obj_name="outfit-custom", parent=self)
        self.custom_btn.clicked.connect(self._on_custom_clicked)
        self.custom_btn.hide()
        var_layout.addWidget(self.custom_btn)
        outer.addLayout(var_layout)
        outer.addSpacing(4)

        # Подсказка (видна в ready)
        self.hint_lbl = QLabel(tr('outfit_picker_hint'))
        self.hint_lbl.setObjectName("outfit-hint")
        self.hint_lbl.setWordWrap(True)
        self.hint_lbl.hide()
        outer.addWidget(self.hint_lbl)

        # Низ: кнопки retry / cancel
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.retry_btn = QPushButton(tr('outfit_picker_retry'))
        self.retry_btn.setObjectName("outfit-retry")
        self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retry_btn.clicked.connect(self._on_retry_clicked)
        self.retry_btn.hide()
        bottom.addWidget(self.retry_btn)

        self.cancel_btn = QPushButton(tr('actors_pending_cancel'))
        self.cancel_btn.setObjectName("outfit-cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        bottom.addWidget(self.cancel_btn)
        bottom.addStretch()
        outer.addLayout(bottom)

    # ── Состояния ─────────────────────────────────────────────────────

    def _apply_state(self):
        self.setProperty("state", self._state)
        self.style().unpolish(self)
        self.style().polish(self)
        # По умолчанию скрыто всё что зависит от состояния.
        self.loading_lbl.hide()
        self.error_lbl.hide()
        self.hint_lbl.hide()
        self.retry_btn.hide()
        self.custom_btn.hide()
        for b in self.var_btns:
            b.hide()
        if self._state == "loading":
            self.loading_lbl.show()
            self._dot_step = 0
            self.loading_lbl.setText(
                tr('outfit_picker_loading', name=self._name))
            self._dot_timer.start()
        elif self._state == "ready":
            self._dot_timer.stop()
            for i, btn in enumerate(self.var_btns):
                if i < len(self._variants):
                    btn.setText(self._variants[i])
                    btn.show()
                else:
                    btn.hide()
            self.custom_btn.show()
            self.hint_lbl.show()
            self.retry_btn.show()
        elif self._state == "error":
            self._dot_timer.stop()
            self.error_lbl.show()
            self.retry_btn.show()

    def _tick_dots(self):
        if self._state != "loading":
            return
        self._dot_step = (self._dot_step + 1) % 4
        dots = ["·   ", "··  ", "··· ", "····"][self._dot_step]
        base = tr('outfit_picker_loading', name=self._name)
        self.loading_lbl.setText(f"{base} {dots}")

    # ── Публичный API ─────────────────────────────────────────────────

    def set_loading(self):
        self._state = "loading"
        self._apply_state()

    def set_variants(self, variants: List[str]):
        self._variants = [v for v in (variants or []) if v.strip()][:3]
        if not self._variants:
            self.set_error(tr('outfit_picker_empty'))
            return
        self._state = "ready"
        self._apply_state()

    def set_error(self, msg: str):
        self._state = "error"
        self.error_lbl.setText(
            tr('outfit_picker_error', msg=(msg or "")[:200]))
        self._apply_state()

    def apply_lang(self):
        """Перевод на текущий язык (на случай смены 🇷🇺/🇺🇦/🇬🇧)."""
        if self._state == "loading":
            self.loading_lbl.setText(
                tr('outfit_picker_loading', name=self._name))
        self.hint_lbl.setText(tr('outfit_picker_hint'))
        self.retry_btn.setText(tr('outfit_picker_retry'))
        self.cancel_btn.setText(tr('actors_pending_cancel'))
        self.custom_btn.setText(tr('outfit_picker_custom'))

    # ── Внутренние слоты ─────────────────────────────────────────────

    def _on_variant_clicked(self):
        if self._state != "ready":
            return
        btn = self.sender()
        try:
            idx = self.var_btns.index(btn)  # type: ignore[arg-type]
        except ValueError:
            return
        if idx >= len(self._variants):
            return
        self.variant_chosen.emit(self._variants[idx])

    def _on_retry_clicked(self):
        if self._state not in ("ready", "error"):
            return
        self.retry_requested.emit()

    def _on_cancel_clicked(self):
        self.cancel_requested.emit()

    def _on_custom_clicked(self):
        """«✎ Придумаю описание сам» — переход на вкладку Актёров без
        предзаполненного описания. Юзер сам впишет в попап."""
        if self._state != "ready":
            return
        self.custom_requested.emit()
