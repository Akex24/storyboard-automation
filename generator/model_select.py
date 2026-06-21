# -*- coding: utf-8 -*-
"""
generator/model_select.py — кастомная выпадашка моделей (2026-06-21).

Замена системного QComboBox#model-combo: своя кнопка-триггер + тёмный поповер
со списком моделей, всплывающий ВВЕРХ над кнопкой. Механика поповера — по
образцу widgets/editor_widgets.py `_tr_popup`: дочерний QFrame окна (НЕ
top-level/Frameless), ручное позиционирование move()+raise_()+show(), закрытие
по клику ВНЕ через eventFilter на QApplication.

Контракт под generator/generator_page.py (минимум правок в _on_run):
  • set_models([(label, model_id), ...]) — список под режим; держит текущий
    выбор если он есть в новом списке, иначе берёт первый
  • current_model_id() -> object | None  — id выбранной (= прежний currentData())
  • current_label() -> str
  • сигнал changed                       — при смене выбора (пока никто не слушает)

Высоту виджета (34) задаёт generator_page СНАРУЖИ — внутри setFixedHeight НЕ ставим.
Без сторонних зависимостей — только PyQt6.
"""

from __future__ import annotations

from typing import List, Tuple, Optional

from PyQt6.QtCore import Qt, QPoint, QEvent, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QApplication,
    QSizePolicy,
)


class ModelSelect(QWidget):
    """Кнопка-триггер + тёмный поповер списка моделей (всплывает вверх)."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("model-select")
        self._items: List[Tuple[str, object]] = []   # [(label, model_id), ...]
        self._index = -1                              # индекс выбранной строки
        self._popup: Optional[QFrame] = None          # ленивый дочерний QFrame окна
        self._rows_box: Optional[QVBoxLayout] = None  # контейнер строк поповера

        # Кнопка-триггер: objectName "model-combo" — тот же, что был у QComboBox.
        # QSS #model-combo в generator_page рассчитан на тип QComboBox и кнопку НЕ
        # покрывает → стилизуем триггер ЛОКАЛЬНО (та же тёмная палитра).
        self.trigger = QPushButton(self)
        self.trigger.setObjectName("model-combo")
        self.trigger.setCursor(Qt.CursorShape.PointingHandCursor)
        # Триггер тянется на всю высоту виджета (34px задаёт generator_page снаружи) —
        # иначе кнопка ниже соседних блоков ряда. Vertical=Expanding → layout растягивает.
        self.trigger.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        # Текст слева (название) + стрелка справа — через внутренний layout.
        trow = QHBoxLayout(self.trigger)
        trow.setContentsMargins(12, 0, 12, 0)
        trow.setSpacing(8)
        self._label_lbl = QLabel("", self.trigger)
        self._label_lbl.setObjectName("model-combo-label")
        self._label_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._arrow_lbl = QLabel("▾", self.trigger)
        self._arrow_lbl.setObjectName("model-combo-arrow")
        self._arrow_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        trow.addWidget(self._label_lbl)
        trow.addStretch(1)
        trow.addWidget(self._arrow_lbl)
        self.trigger.clicked.connect(self._toggle_popup)
        # Ховер текста триггера ловим на самой кнопке (label transparent-for-mouse →
        # Enter/Leave идут кнопке). Это ОТДЕЛЬНЫЙ фильтр от app-фильтра dismiss —
        # eventFilter различает по obj (см. eventFilter()).
        self.trigger.installEventFilter(self)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.trigger)

        # Локальный стиль триггера. БЕЗ бордера (как соседние QFrame#seg-group в
        # generator_page) — иначе кнопка выбивается из ряда. Ховер как у #seg:
        # фон НЕ трогаем, текст приглушён → светлеет. Перекраску делаем по
        # Enter/Leave кнопки в eventFilter (надёжнее хрупкого parent:hover child).
        self.setStyleSheet(
            "QPushButton#model-combo { background:#100b18; border:none;"
            " border-radius:10px; min-width:150px; }"
            "QLabel#model-combo-label { color:rgba(255,255,255,0.55);"
            " font-size:13px; background:transparent; }"
            "QLabel#model-combo-arrow { color:#9a8fb0; font-size:11px;"
            " background:transparent; }")

    # ── публичный API (контракт под generator_page) ─────────────────────
    def set_models(self, items):
        """Задать список моделей под режим. items = [(label, model_id), ...].
        Сохраняет текущий выбор если он есть в новом списке, иначе берёт первый.
        Обновляет текст кнопки и (если поповер открыт) перестраивает строки."""
        prev = self._items[self._index] if 0 <= self._index < len(self._items) else None
        self._items = list(items or [])
        idx = 0 if self._items else -1
        if prev is not None:
            for i, it in enumerate(self._items):
                if it == prev:
                    idx = i
                    break
        self._index = idx
        self._sync_trigger()
        if self._popup is not None and self._popup.isVisible():
            self._build_rows()

    def current_model_id(self):
        """id выбранной модели (= прежний currentData()); None для video-заглушек."""
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None

    def current_label(self) -> str:
        if 0 <= self._index < len(self._items):
            return self._items[self._index][0]
        return ""

    # ── триггер ─────────────────────────────────────────────────────────
    def _sync_trigger(self):
        """Текст кнопки = название выбранной модели (без эмодзи)."""
        self._label_lbl.setText(self.current_label())

    def _toggle_popup(self):
        if self._popup is not None and self._popup.isVisible():
            self._hide_popup()
        else:
            self._show_popup()

    # ── поповер ─────────────────────────────────────────────────────────
    def _ensure_popup(self):
        """Лениво создать дочерний QFrame окна (как _tr_popup) + контейнер строк."""
        if self._popup is not None:
            return
        win = self.window()
        self._popup = QFrame(win)
        self._popup.setObjectName("model-popup")
        self._popup.setStyleSheet(
            "QFrame#model-popup { background:#221b2e; border:1px solid #322a40;"
            " border-radius:12px; }"
            "QPushButton#model-popup-item { background:transparent; color:#e8e3f0;"
            " border:none; border-radius:8px; font-size:13px; text-align:left; }"
            "QPushButton#model-popup-item:hover { background:#2c2438; }"
            "QPushButton#model-popup-item[selected=\"true\"] { color:#ffffff; }"
            "QLabel#model-popup-text { background:transparent; }"
            "QLabel#model-popup-check { color:#d4a256; background:transparent; }")
        self._rows_box = QVBoxLayout(self._popup)
        # Симметричные отступы вокруг строк: ховер-пилюля первой и последней строки
        # одинаково отстоит от скруглённой рамки поповера (низ не липнет к краю).
        self._rows_box.setContentsMargins(8, 10, 8, 10)
        self._rows_box.setSpacing(8)   # воздух между строками: ховер не лижет соседа

    def _build_rows(self):
        """Пересобрать строки поповера из self._items (галочка у выбранной)."""
        if self._rows_box is None:
            return
        while self._rows_box.count():
            it = self._rows_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for i, (label, mid) in enumerate(self._items):
            row = QPushButton(self._popup)
            row.setObjectName("model-popup-item")
            row.setProperty("selected", i == self._index)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            # Все строки фикс. высоты 40px и НЕ растягиваются (Fixed) → spacing
            # между ними гарантированно не схлопывается, ховер-пилюля не лижет соседа.
            row.setFixedHeight(40)
            row.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            # padding строки задаём margins layout'а (надёжнее QSS-padding на
            # кнопке с дочерним layout): текст слева, ✓ справа.
            rrow = QHBoxLayout(row)
            rrow.setContentsMargins(12, 8, 12, 8)
            rrow.setSpacing(8)
            txt = QLabel(label, row)
            txt.setObjectName("model-popup-text")
            txt.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            check = QLabel("✓", row)
            check.setObjectName("model-popup-check")
            check.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            check.setVisible(i == self._index)   # галочка только у выбранной
            rrow.addWidget(txt)
            rrow.addStretch(1)
            rrow.addWidget(check)
            row.clicked.connect(lambda _checked=False, idx=i: self._choose(idx))
            self._rows_box.addWidget(row)

    def _show_popup(self):
        if not self._items:
            return
        self._ensure_popup()
        self._build_rows()
        popup = self._popup
        popup.adjustSize()
        # Ширина = max(ширина кнопки, естественная ширина строк).
        w = max(self.width(), popup.sizeHint().width())
        popup.resize(w, popup.sizeHint().height())
        # Позиция: ВВЕРХ над кнопкой, левый край по кнопке; кламп в окно (как _tr_popup).
        win = self.window()
        tl = self.mapTo(win, QPoint(0, 0))
        x = tl.x()
        y = tl.y() - popup.height() - 6
        x = max(8, min(x, win.width() - popup.width() - 8))
        y = max(8, min(y, win.height() - popup.height() - 8))
        popup.move(x, y)
        popup.show()
        popup.raise_()
        self._arrow_lbl.setText("▴")
        # Клик ВНЕ поповера/кнопки → закрыть. Фильтр на QApplication ловит клики
        # где угодно. Снимаем перед установкой → ровно один экземпляр фильтра.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
            app.installEventFilter(self)

    def _hide_popup(self):
        if self._popup is not None and self._popup.isVisible():
            self._popup.hide()
        self._arrow_lbl.setText("▾")
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass

    def _choose(self, idx: int):
        """Клик по строке: выбрать, обновить кнопку, закрыть, эмитнуть changed."""
        if not (0 <= idx < len(self._items)):
            self._hide_popup()
            return
        changed = idx != self._index
        self._index = idx
        self._sync_trigger()
        self._hide_popup()
        if changed:
            self.changed.emit()

    # ── dismiss / жизненный цикл ────────────────────────────────────────
    def eventFilter(self, obj, ev):
        """Два независимых фильтра в одном методе (различаем по obj):
        1) obj is self.trigger → Enter/Leave кнопки: перекраска текста (ховер).
        2) app-фильтр (ставится при открытом поповере) → клик мимо = dismiss.
        Тело в try/except: непойманное исключение в eventFilter PyQt6 переводит
        в qFatal()→abort() (краш всего приложения)."""
        try:
            # (1) Ховер триггера: текст 0.55 → 0.85 (фон не трогаем, как #seg).
            if obj is self.trigger:
                if ev.type() == QEvent.Type.Enter:
                    self._label_lbl.setStyleSheet(
                        "color:rgba(255,255,255,0.85); background:transparent;")
                elif ev.type() == QEvent.Type.Leave:
                    self._label_lbl.setStyleSheet(
                        "color:rgba(255,255,255,0.55); background:transparent;")
            # (2) Клик ВНЕ поповера/кнопки → закрыть (как _tr_popup).
            if (self._popup is not None and self._popup.isVisible()
                    and ev.type() == QEvent.Type.MouseButtonPress):
                gp = ev.globalPosition().toPoint()
                in_popup = self._popup.rect().contains(
                    self._popup.mapFromGlobal(gp))
                in_trigger = self.rect().contains(self.mapFromGlobal(gp))
                if not in_popup and not in_trigger:
                    self._hide_popup()
        except Exception:
            pass
        return super().eventFilter(obj, ev)

    def hideEvent(self, ev):
        """Скрытие виджета/страницы → закрыть поповер (не висит над другими вкладками)."""
        self._hide_popup()
        super().hideEvent(ev)
