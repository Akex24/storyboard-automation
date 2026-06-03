# -*- coding: utf-8 -*-
"""
widgets/editor_widgets.py — виджеты для вкладки «Редактор».

Содержит 4 класса:
    - OverlayActionBtn — hover-кнопка ↻/✎ для шотов и рефов
    - ShotCard         — карточка шота 9:16 в сетке блока
    - RoundedTopImage  — превью с скруглёнными верхними углами (для RefCard)
    - RefCard          — карточка референса (локация / объект / персонаж)

Зависимости от storyboard_app.py через `_AppProxy` (приоритет __main__):
    - setup_fade_overlay / fade_in / fade_out — анимации overlay
    - format_gen_duration — форматирование «1м 5с»

Прямые импорты:
    - tr из i18n
    - стандартные PyQt6 виджеты + paint API

История: вытащено из storyboard_app.py 2026-05-04 (шаг 5A рефакторинга).
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import Qt, QSize, QTimer, QRectF, QEvent, QPoint, QRect, pyqtSignal
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPainterPath, QColor, QCursor, QIcon,
)
from PyQt6.QtWidgets import (
    QFrame, QLabel, QPushButton, QWidget, QProgressBar,
    QVBoxLayout, QHBoxLayout,
)

from i18n import tr


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


def _load_lucide_icon(name: str) -> QIcon:
    """Загружает SVG-иконку из assets/icons/ как QIcon.

    Ищет в порядке:
      1. `<project_root>/assets/icons/<name>.svg` — для dev-режима.
      2. `<bundle>/assets/icons/<name>.svg` — для PyInstaller .app.
    Возвращает пустой QIcon если файл не найден (UI не падает).

    2026-05-08: используется в hover-overlay кнопках RefCard
    (edit/delete/regen). Унифицирует визуал — все кнопки одного
    стиля Lucide SVG, без emoji-разнобоя."""
    import sys
    candidates = []
    try:
        # widgets/editor_widgets.py → ../assets/icons/
        candidates.append(Path(__file__).parent.parent / "assets" / "icons" / f"{name}.svg")
    except Exception:
        pass
    try:
        if hasattr(sys, '_MEIPASS'):
            candidates.append(Path(sys._MEIPASS) / "assets" / "icons" / f"{name}.svg")
    except Exception:
        pass
    for p in candidates:
        try:
            if p.exists():
                return QIcon(str(p))
        except Exception:
            pass
    return QIcon()


# ─── Hover-кнопка для шотов и рефов ──────────────────────────────

class OverlayActionBtn(QFrame):
    """Кнопка hover-overlay (для шотов и рефов): крупная иконка сверху,
    подпись помельче снизу. Имитирует QPushButton (clicked signal,
    hover/pressed-стили), но позволяет иконке быть в 3-4 раза больше текста."""
    clicked = pyqtSignal()

    def __init__(self, icon: str, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("overlay-action")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)
        self.icon_lbl = QLabel(icon)
        self.icon_lbl.setObjectName("overlay-action-icon")
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon_lbl)
        self.text_lbl = QLabel(label)
        self.text_lbl.setObjectName("overlay-action-text")
        self.text_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_lbl.setWordWrap(True)
        lay.addWidget(self.text_lbl)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        # accept() — событие НЕ всплывает к родителю (overlay).
        # Без этого клик по большой кнопке также триггерил overlay-bg клик,
        # который открывает fullscreen — попадали два действия одновременно.
        ev.accept()

    def set_label(self, icon: str, label: str):
        self.icon_lbl.setText(icon)
        self.text_lbl.setText(label)


# ─── Карточка шота в сетке блока ─────────────────────────────────

class ShotCard(QFrame):
    regen_requested = pyqtSignal(int)
    edit_requested  = pyqtSignal(int)   # запрос на edit-попап
    # 2026-05-07: клик по картинке открывает ShotViewerDialog (попап с
    # историей версий + большим превью). Edit/regen теперь живут внутри
    # этого попапа, в hover-overlay карточки иконки убраны.
    image_clicked = pyqtSignal(int)
    # 2026-06-02: копирование активной картинки шота между шотами/блоками
    # через угловые кнопки overlay. copy → активные байты в буфер MainWindow,
    # paste → буфер добавляется новой версией в шот-назначение.
    copy_requested  = pyqtSignal(int)
    paste_requested = pyqtSignal(int)
    # Ширина подобрана так, чтобы ряд из 4 карточек занимал РОВНО ширину
    # кнопки «Сохранить стриборд как PNG» (которая стретчится на всю ширину
    # контентной области): 4×(CARD_W+20 padding QFrame) + 3×12 spacing = 944,
    # что = 1000 - 28×2 margins. Высота сохраняет строгое соотношение 9:16.
    CARD_W, CARD_H  = 207, 368

    def __init__(self, panel_idx: int, parent=None):
        super().__init__(parent)
        self.panel_idx = panel_idx
        # Запоминаем что шот пустой/blank — чтобы overlay не показывался
        self._is_blank = False
        self._is_loading = False
        # Текущее состояние overlay — для защиты от повторных fade_in
        # при многократных Enter событиях (Qt может слать их при разных
        # переходах внутри карточки)
        self._overlay_visible = False
        self.setObjectName("card")
        # Фиксированная ширина — чтобы пустые и с картинкой шоты были РОВНО
        # одной ширины (иначе sizeHint от desc_label делает их разной ширины).
        self.setFixedWidth(self.CARD_W + 20)
        self._build()
        # Hover-обработка ТОЛЬКО на области картинки (img_container) — чтобы
        # overlay не появлялся когда наводишь на текст-описание под шотом.
        # Анти-мигание: в eventFilter при Leave проверяем курсор — если он
        # на overlay (child of img_container), НЕ скрываем (это просто
        # переход parent→child, не настоящий уход с карточки).
        self.img_container.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.img_container.installEventFilter(self)
        # 2026-05-07: клик по картинке → открыть ShotViewerDialog.
        # MW.shot_cards подключает image_clicked → _on_shot_image_clicked.
        self.img_container.setCursor(Qt.CursorShape.PointingHandCursor)
        # mousePressEvent override через monkey-patch (проще чем
        # subclass'ить QWidget'ы только для этого — паттерн уже
        # используется в RefCard).
        def _img_click(ev):
            try:
                if ev.button() == Qt.MouseButton.LeftButton \
                        and not self._is_loading and not self._is_blank:
                    self.image_clicked.emit(self.panel_idx)
            except Exception:
                traceback.print_exc()
        self.img_container.mousePressEvent = _img_click  # type: ignore

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(6)
        lay.setContentsMargins(10, 10, 10, 10)

        # ── Зона изображения с hover-overlay ────────────────────────────────
        # Контейнер чтобы overlay позиционировался относительно картинки
        self.img_container = QWidget()
        self.img_container.setFixedSize(self.CARD_W, self.CARD_H)
        self.img_label = QLabel(tr('empty_shot'), self.img_container)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setGeometry(0, 0, self.CARD_W, self.CARD_H)
        self.img_label.setStyleSheet(
            "background:#1a1424; border-radius:6px; color:#333; font-size:12px;")

        # Hover-overlay — БЕЗ затемнения всей картинки. Прозрачный контейнер
        # на всю площадь img_container; внутри — нижняя полоска с лёгким
        # градиентом и двумя круглыми кнопками-иконками (Edit + Regenerate).
        # Strip позиционируется АБСОЛЮТНО через setGeometry (без QVBoxLayout)
        # чтобы избежать lazy-layout «прыжка» при первом fade_in: Qt не
        # вычисляет layout невидимого виджета, и при первом show() strip
        # сначала рендерится в (0,0), потом «прыгает» в правильное место.
        # С абсолютной геометрией strip всегда в нужной позиции с момента
        # создания.
        # 2026-05-07: вместо overlay-strip с edit/regen кнопками — теперь
        # лёгкий полупрозрачный hint поверх картинки + клик по картинке
        # открывает ShotViewerDialog (большое превью + история версий +
        # edit/regen внутри попапа). UX: «навёл — увидел подсказку,
        # кликнул — открылся подробный попап».
        STRIP_H = 36
        self.regen_overlay = QFrame(self.img_container)
        self.regen_overlay.setObjectName("shot-overlay")
        self.regen_overlay.setGeometry(0, 0, self.CARD_W, self.CARD_H)
        self.regen_overlay.setCursor(Qt.CursorShape.PointingHandCursor)

        strip = QFrame(self.regen_overlay)
        strip.setObjectName("shot-overlay-strip")
        strip.setGeometry(0, self.CARD_H - STRIP_H, self.CARD_W, STRIP_H)
        sh = QHBoxLayout(strip)
        sh.setContentsMargins(8, 6, 8, 6)
        sh.setSpacing(6)
        hint_lbl = QLabel(tr('shot_overlay_click_to_open'))
        hint_lbl.setObjectName("shot-overlay-hint")
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sh.addWidget(hint_lbl, stretch=1)

        # 2026-06-02: угловые кнопки Copy/Paste (правый верхний угол overlay).
        # Дети regen_overlay → появляются/прячутся вместе с overlay по hover,
        # т.е. ТОЛЬКО на заполненной карточке (overlay подавлен для blank и при
        # loading — eventFilter/set_loading/set_shot_info). Абсолютный
        # setGeometry (не layout) — как у strip, чтобы не было lazy-layout
        # «прыжка» при первом fade_in.
        #
        # QPushButton НАТИВНО потребляет клик и НЕ пропускает его к родителю
        # (img_container._img_click), поэтому клик по кнопке НЕ открывает попап
        # ShotViewerDialog — отдельный accept() не нужен.
        #
        # Иконки — Lucide SVG через _sa.get_icon (ленивый импорт из
        # storyboard_app, чтобы не плодить circular import в этом модуле).
        BTN = 28
        self.btn_copy = QPushButton(self.regen_overlay)
        self.btn_copy.setObjectName("shot-corner-btn")
        self.btn_copy.setGeometry(self.CARD_W - BTN - 6, 6, BTN, BTN)
        self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy.setToolTip(tr('shot_copy'))
        self.btn_paste = QPushButton(self.regen_overlay)
        self.btn_paste.setObjectName("shot-corner-btn")
        self.btn_paste.setGeometry(self.CARD_W - 2 * BTN - 12, 6, BTN, BTN)
        self.btn_paste.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_paste.setToolTip(tr('shot_paste'))
        self.btn_paste.setEnabled(False)  # активна когда буфер не пуст
        try:
            self.btn_copy.setIcon(_sa.get_icon('copy'))
            self.btn_copy.setIconSize(QSize(16, 16))
            self.btn_paste.setIcon(_sa.get_icon('clipboard-paste'))
            self.btn_paste.setIconSize(QSize(16, 16))
        except Exception:
            traceback.print_exc()
        # Инлайн-стиль (НЕ через theme.py — общий не ломаем): полупрозрачный
        # тёмный фон чтобы иконка читалась поверх светлой картинки, скругление,
        # hover — фиолетовый акцент поярче, disabled («Вставить» без буфера) —
        # приглушённый фон, видно что неактивна.
        corner_qss = (
            "QPushButton#shot-corner-btn {"
            " background:rgba(10,10,13,0.55); border:none; border-radius:6px; }"
            "QPushButton#shot-corner-btn:hover {"
            " background:rgba(110,76,196,0.85); }"
            "QPushButton#shot-corner-btn:pressed {"
            " background:rgba(90,60,170,0.95); }"
            "QPushButton#shot-corner-btn:disabled {"
            " background:rgba(10,10,13,0.20); }"
        )
        self.btn_copy.setStyleSheet(corner_qss)
        self.btn_paste.setStyleSheet(corner_qss)
        self.btn_copy.clicked.connect(
            lambda: self.copy_requested.emit(self.panel_idx))
        self.btn_paste.clicked.connect(
            lambda: self.paste_requested.emit(self.panel_idx))

        # Скрытые legacy-объекты для обратной совместимости с set_loading
        # / тестами / проверками. Сигналы edit_requested/regen_requested
        # ещё используются попапом ShotViewerDialog (клики из попапа
        # эмитятся через MW). Карточка их сама не вызывает.
        self.overlay_edit_btn = QPushButton()
        self.overlay_edit_btn.hide()
        self.overlay_regen_btn = QPushButton()
        self.overlay_regen_btn.hide()

        # Плавный fade-in/out overlay вместо мгновенного show/hide
        self._overlay_anim = _sa.setup_fade_overlay(self.regen_overlay)

        lay.addWidget(self.img_container, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        lay.addWidget(self.progress_bar)

        self.step_label = QLabel("")
        self.step_label.setObjectName("step-label")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.step_label.hide()
        lay.addWidget(self.step_label)

        row = QHBoxLayout()
        self.num_label = QLabel(f"SHOT {self.panel_idx + 1}")
        self.num_label.setObjectName("shot-num")
        # Бейдж NEW — показывается после регенерации, исчезает при переключении блока
        self.new_badge = QLabel("NEW")
        self.new_badge.setObjectName("new-badge")
        self.new_badge.hide()
        # Время генерации шота — стоит сразу после NEW, исчезает вместе с ним
        self.gen_time_label = QLabel("")
        self.gen_time_label.setObjectName("gen-time")
        self.gen_time_label.hide()
        self.dur_label = QLabel("")
        self.dur_label.setObjectName("shot-dur")
        row.addWidget(self.num_label)
        row.addSpacing(6)
        row.addWidget(self.new_badge)
        row.addSpacing(4)
        row.addWidget(self.gen_time_label)
        row.addStretch()
        row.addWidget(self.dur_label)
        lay.addLayout(row)

        self.desc_label = QLabel("")
        self.desc_label.setObjectName("shot-desc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumWidth(self.CARD_W + 20)
        lay.addWidget(self.desc_label)

        # 2026-06-03 (Этап 1): реплика шота (dialog.en из монтажной карты) —
        # ВМЕСТЕ с описанием, отдельной строкой ниже, в кавычках, своим цветом
        # (QSS #shot-dialog). Заполняется в set_shot_info из shot["dialog_en"];
        # нет реплики → hide(). wordWrap без обрезки — места под описанием много.
        self.dialog_label = QLabel("")
        self.dialog_label.setObjectName("shot-dialog")
        self.dialog_label.setWordWrap(True)
        self.dialog_label.setMaximumWidth(self.CARD_W + 20)
        self.dialog_label.hide()
        lay.addWidget(self.dialog_label)

        lay.addStretch()

        # Скрытые кнопки для обратной совместимости с set_loading логикой —
        # реальное взаимодействие через hover-overlay (overlay_regen_btn / overlay_edit_btn)
        self.regen_btn = QPushButton()
        self.regen_btn.hide()
        self.edit_btn = QPushButton()
        self.edit_btn.hide()

    def eventFilter(self, watched, event):
        """Hover ТОЛЬКО на области картинки (img_container).
        Плавный fade-in/out overlay (160мс).

        АНТИ-МИГАНИЕ: при Leave img_container проверяем глобальную позицию
        курсора. Если курсор сейчас НАД overlay (overlay — child img_container,
        перекрывает его) — это НЕ настоящий уход с картинки, а переход
        parent→child. НЕ скрываем overlay. Иначе бы был цикл: Leave →
        fade_out → overlay скрылся → курсор снова над img_container → Enter
        → fade_in → курсор сразу на overlay → Leave → cycle.

        ⚠ Тело обёрнуто в try/except: PyQt6 переводит непойманное Python
        исключение из eventFilter в qFatal()→abort() (краш всего приложения).
        """
        try:
            if watched is self.img_container:
                if event.type() == QEvent.Type.Enter:
                    # Защита от повторных Enter (Qt может слать несколько при
                    # переходах между img/strip/buttons): запускаем fade_in
                    # ТОЛЬКО если overlay сейчас не показан
                    if (not self._is_blank and not self._is_loading
                            and not self._overlay_visible):
                        self._overlay_visible = True
                        _sa.fade_in(self.regen_overlay, self._overlay_anim)
                elif event.type() == QEvent.Type.Leave:
                    # Cursor-check: не скрываем overlay если курсор сейчас НА нём
                    # (это переход parent→child, не реальный уход)
                    if self._overlay_visible:
                        gtl = self.regen_overlay.mapToGlobal(QPoint(0, 0))
                        rect = QRect(gtl, self.regen_overlay.size())
                        if rect.contains(QCursor.pos()):
                            return False  # курсор на overlay — оставляем visible
                        self._overlay_visible = False
                        _sa.fade_out(self.regen_overlay, self._overlay_anim)
        except Exception:
            traceback.print_exc()
        return super().eventFilter(watched, event)

    def leaveEvent(self, ev):
        """Страховка: курсор покинул всю карточку → overlay прячется.
        Сработает например когда мышь уходит с overlay сразу за пределы
        карточки (eventFilter Leave img_container в этом случае может
        не успеть сработать или быть проигнорирован cursor-check'ом)."""
        try:
            if self._overlay_visible:
                self._overlay_visible = False
                _sa.fade_out(self.regen_overlay, self._overlay_anim)
        except Exception:
            traceback.print_exc()
        super().leaveEvent(ev)

    def set_image(self, jpeg_bytes: Optional[bytes]):
        # Анти-моргание: если те же самые байты что мы УЖЕ применили —
        # пропускаем. Без этого `_display_block` re-paint'ил pixmap при
        # каждом вызове, даже если файл шота не менялся (например
        # `_display_block` зовётся когда другой шот в другом блоке
        # завершил регенерацию). Создание QPixmap + scaled + rounded-corner
        # painting — это тяжёлая операция, она и давала визуальное
        # моргание всех карточек на ~1 кадр.
        if jpeg_bytes == getattr(self, '_last_jpeg_bytes', None):
            return
        self._last_jpeg_bytes = jpeg_bytes
        if not jpeg_bytes:
            self.img_label.clear()
            self.img_label.setText(tr('empty_shot'))
            return
        pixmap = QPixmap.fromImage(QImage.fromData(jpeg_bytes)).scaled(
            QSize(self.CARD_W, self.CARD_H),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Скругляем углы картинки через QPainterPath-маску. Картинка
        # генерируется с прямыми углами — программно даём ей те же
        # скругления (radius 6px), что у пустых панелей и фона карточки.
        rounded = QPixmap(pixmap.size())
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, pixmap.width(), pixmap.height()), 6, 6)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        self.img_label.setPixmap(rounded)

    def set_shot_info(self, shot: Dict):
        self._is_blank = bool(shot.get("is_blank"))
        if self._is_blank:
            self.num_label.setText(tr('empty_shot'))
            self.dur_label.setText("")
            self.desc_label.setText("")
            self.dialog_label.clear()
            self.dialog_label.hide()
            self.new_badge.hide()  # для пустого шота нечего быть «новым»
            self.gen_time_label.hide()
            # Пустые шоты не дают hover-overlay — мгновенно скрываем
            # Без анимации — просто скрываем overlay
            self.regen_overlay.hide()
            self._overlay_visible = False
        else:
            self.num_label.setText(f"SHOT {shot['shot_num']}")
            self.dur_label.setText(shot["duration"])
            self.desc_label.setText(shot["description"])
            # Реплика (dialog.en) — в кавычках под описанием; нет → скрыть.
            en = (shot.get("dialog_en") or "").strip()
            if en:
                self.dialog_label.setText(f'"{en}"')
                self.dialog_label.show()
            else:
                self.dialog_label.clear()
                self.dialog_label.hide()

    def apply_lang(self):
        """Перевести тексты overlay-кнопок и плейсхолдер «ПУСТО» на текущий язык."""
        # img_label показывает «ПУСТО» когда у шота нет картинки — текст
        # устанавливается только если pixmap пустой (иначе картинка перетрёт его).
        if self.img_label.pixmap() is None or self.img_label.pixmap().isNull():
            self.img_label.setText(tr('empty_shot'))
        # Если шот пустой — обновляем и подпись номера (в set_shot_info ставится tr)
        if getattr(self, '_is_blank', False):
            self.num_label.setText(tr('empty_shot'))
        # Tooltips на круглых кнопках overlay — без текста на самой кнопке
        if hasattr(self, 'overlay_edit_btn'):
            self.overlay_edit_btn.setToolTip(tr('overlay_edit').split('\n')[-1])
        if hasattr(self, 'overlay_regen_btn'):
            self.overlay_regen_btn.setToolTip(tr('overlay_regen').split('\n')[-1])

    def set_new_badge(self, visible: bool):
        """Показ/скрытие бейджа NEW (только для НЕ-пустых шотов)."""
        if self._is_blank:
            self.new_badge.hide()
        else:
            self.new_badge.setVisible(bool(visible))

    def set_gen_time(self, seconds: int):
        """Показывает время генерации шота, например '⏱ 42с' или '⏱ 1м 5с'.
        Если 0 или это пустой шот — скрывает метку.
        """
        if self._is_blank or not seconds or seconds <= 0:
            self.gen_time_label.hide()
            self.gen_time_label.setText("")
        else:
            self.gen_time_label.setText(
                f"⏱ {_sa.format_gen_duration(seconds)}")
            self.gen_time_label.show()

    def set_progress(self, label: str, pct: int):
        self.progress_bar.setValue(pct)
        self.step_label.setText(label)
        self.progress_bar.show()
        self.step_label.show()

    def set_loading(self, loading: bool):
        self._is_loading = loading
        if loading:
            # Во время генерации overlay прячем мгновенно
            # Без анимации — просто скрываем overlay
            self.regen_overlay.hide()
            self._overlay_visible = False
        else:
            self.progress_bar.hide()
            self.step_label.hide()
            self.progress_bar.setValue(0)

    def set_paste_available(self, available: bool):
        """2026-06-02: MainWindow зовёт это у всех карточек когда в буфере
        появляется скопированная картинка — кнопка «Вставить» становится
        активной. До первого Copy буфер пуст → кнопка disabled."""
        try:
            self.btn_paste.setEnabled(bool(available))
        except Exception:
            pass


# ─── Превью со скруглёнными верхними углами ──────────────────────

class RoundedTopImage(QWidget):
    """Виджет для превью реф-картинки со скруглёнными ВЕРХНИМИ углами.

    Сам рисует pixmap (вместо QLabel.setPixmap), что даёт честное
    антиалиас-скругление углов независимо от размеров pixmap. Нижние углы
    оставлены прямыми — снизу к нему примыкает блок имя/тег карточки.
    Картинка масштабируется по высоте с KeepAspectRatioByExpanding и
    обрезается по центру (как в ShotCard, чтобы не было пустых полос)."""

    def __init__(self, parent=None, radius: int = 11):
        super().__init__(parent)
        self._radius = radius
        self._pixmap: Optional[QPixmap] = None
        self._bg = QColor(40, 30, 60, 102)  # тот же цвет что был в QLabel
        # Кэш отмасштабированного pixmap'а — пересчитывается ТОЛЬКО при
        # setPixmap или resizeEvent. До этого `scaled(...)` с SmoothTransformation
        # вызывался при каждом paintEvent → 60 fps × N карточек = scroll-glitch.
        self._scaled_cache: Optional[QPixmap] = None
        self._scaled_for_size: Optional[QSize] = None

    def setPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self._scaled_cache = None
        self._scaled_for_size = None
        self.update()

    def resizeEvent(self, ev):
        # Размер изменился — кэш невалиден. Сам pixmap не пересоздаём,
        # отложим до следующего paintEvent (там будет ровно один scale).
        self._scaled_cache = None
        self._scaled_for_size = None
        super().resizeEvent(ev)

    def _ensure_scaled(self) -> Optional[QPixmap]:
        if self._pixmap is None or self._pixmap.isNull():
            return None
        sz = self.size()
        if self._scaled_cache is not None and self._scaled_for_size == sz:
            return self._scaled_cache
        self._scaled_cache = self._pixmap.scaled(
            sz,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_for_size = sz
        return self._scaled_cache

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w = float(self.width()); h = float(self.height())
        r = float(self._radius)
        path = QPainterPath()
        path.moveTo(0, h)
        path.lineTo(0, r)
        path.quadTo(0, 0, r, 0)
        path.lineTo(w - r, 0)
        path.quadTo(w, 0, w, r)
        path.lineTo(w, h)
        path.closeSubpath()
        p.setClipPath(path)
        p.fillRect(QRectF(0, 0, w, h), self._bg)
        scaled = self._ensure_scaled()
        if scaled is not None:
            x = (self.width()  - scaled.width())  // 2
            y = (self.height() - scaled.height()) // 2
            p.drawPixmap(int(x), int(y), scaled)


# ─── Карточка референса (refs view) ──────────────────────────────

class RefCard(QFrame):
    """Карточка одного референса (локация / объект / персонаж).

    • Картинка вверху + имя/тег внизу
    • Клик по картинке → emit image_clicked (для fullscreen-просмотра)
    • Если kind in ('location', 'object'): hover на картинку → overlay с
      двумя кнопками «Перегенерировать» / «Изменить» → emits regen/edit_requested
    • Если kind == 'character': overlay не показывается (рефы персонажей —
      это реальные фото, их не регенерим)
    """
    image_clicked    = pyqtSignal()
    regen_requested  = pyqtSignal()
    edit_requested   = pyqtSignal()
    delete_requested = pyqtSignal()
    IMG_H = 220

    def __init__(self, r: Dict, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._is_loading = False
        # Карточка «занята»: пока ассистент CLI обновляет geometry-файл в фоне.
        # Блокирует hover/клики чтобы юзер случайно не запустил регенерацию
        # вторично пока ещё идёт обработка предыдущей.
        self._geometry_updating = False
        # 2026-05-07: фаза «генерация/редактирование картинки» (FastGen).
        # Раньше во время этой фазы юзер видел только тонкий progress-bar
        # 5px внизу — непонятно что происходит. Теперь показываем такой же
        # busy_overlay (тёмная плашка + иконка + текст + точки) что и для
        # geometry-фазы, но с другим label_key. Для location: фаза 1 =
        # image, фаза 2 = geometry. Для object: только фаза 1.
        self._image_updating = False
        self._image_updating_label_key = ""
        # Текущее состояние overlay — для защиты от повторных fade_in
        # при многократных Enter событиях
        self._overlay_visible = False
        # Запоминаем путь — нужен для поиска карточки по path при регене/edit
        self._image_path = Path(r['path'])
        self.setObjectName("ref-card")
        self._build(r)
        # eventFilter на самой RefCard (не на img_container) — Enter/Leave
        # стабильны при перемещении курсора между дочерними виджетами
        # (img_container, overlay, strip-кнопки). Иначе бы overlay перехватывал
        # мышь у img_container → Leave → мигание fade_out/fade_in.
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.installEventFilter(self)

    def _build(self, r: Dict):
        cl = QVBoxLayout(self)
        cl.setSpacing(0)
        cl.setContentsMargins(0, 0, 0, 0)

        # Контейнер картинки (для overlay позиционирования)
        self.img_container = QWidget()
        self.img_container.setFixedHeight(self.IMG_H)
        self.img_container.setCursor(Qt.CursorShape.PointingHandCursor)
        # Клик по картинке → fullscreen (для всех типов: character без
        # overlay, location/object — даже когда курсор не покрыт overlay'ем)
        def _img_click(ev):
            if ev.button() == Qt.MouseButton.LeftButton and not (
                    self._is_loading or self._geometry_updating or self._image_updating):
                self.image_clicked.emit()
        self.img_container.mousePressEvent = _img_click  # type: ignore

        # Превью картинки со скруглёнными верхними углами (radius 11px,
        # совпадает с border-radius:12 у самой карточки минус 1px бордюра).
        self.img_lbl = RoundedTopImage(self.img_container, radius=11)
        try:
            pixmap = QPixmap(str(r['path']))
            if not pixmap.isNull():
                self.img_lbl.setPixmap(pixmap)
        except Exception:
            pass

        # Hover-overlay — для локаций/объектов: ✎/🗑/↻; для персонажей:
        # только 🗑 (удалить из эпизода — рефы персонажей пересоздаются
        # через вкладку Актёры).
        # Стиль как у шотов (shot-overlay): нижняя полоска с круглыми
        # кнопками-иконками. Картинка НЕ затемняется — остаётся видна целиком.
        self.overlay = None
        if self._kind == 'character':
            self.overlay = QFrame(self.img_container)
            self.overlay.setObjectName("shot-overlay")
            self.overlay.setCursor(Qt.CursorShape.PointingHandCursor)
            def _overlay_bg_click_char(ev):
                if ev.button() == Qt.MouseButton.LeftButton:
                    self.image_clicked.emit()
            self.overlay.mousePressEvent = _overlay_bg_click_char  # type: ignore
            strip = QFrame(self.overlay)
            strip.setObjectName("shot-overlay-strip")
            self._strip = strip
            strip.setFixedHeight(72)
            sh = QHBoxLayout(strip)
            sh.setContentsMargins(10, 10, 10, 14)
            sh.setSpacing(10)
            sh.addStretch()
            # Только 🗑 — удалить персонажа из РЕФЕРЕНСОВ эпизода.
            # Файл рефа НЕ удаляется (юзер мог захотеть переиспользовать
            # этого персонажа в другом эпизоде).
            self.overlay_delete = QPushButton()
            self.overlay_delete.setObjectName("ref-overlay-btn")
            self.overlay_delete.setIcon(_load_lucide_icon("trash-2"))
            self.overlay_delete.setIconSize(QSize(16, 16))
            self.overlay_delete.setCursor(Qt.CursorShape.PointingHandCursor)
            self.overlay_delete.setToolTip(tr('overlay_remove_char_from_ep'))
            self.overlay_delete.clicked.connect(self.delete_requested)
            sh.addWidget(self.overlay_delete)
            self._overlay_anim = _sa.setup_fade_overlay(self.overlay)
        elif self._kind in ('location', 'object'):
            self.overlay = QFrame(self.img_container)
            self.overlay.setObjectName("shot-overlay")
            self.overlay.setCursor(Qt.CursorShape.PointingHandCursor)
            # Overlay сам ловит мышь (НЕ прозрачен) — чтобы strip с кнопками
            # внутри получал клики. БЕЗ QVBoxLayout: strip позиционируется
            # абсолютно через setGeometry, обновляется в resizeEvent. Это
            # избавляет от lazy-layout «прыжка» strip при первом fade_in
            # (Qt не вычисляет layout невидимого виджета).

            # Клик по фону overlay (не по кнопке) → fullscreen
            def _overlay_bg_click(ev):
                if ev.button() == Qt.MouseButton.LeftButton:
                    self.image_clicked.emit()
            self.overlay.mousePressEvent = _overlay_bg_click  # type: ignore

            strip = QFrame(self.overlay)
            strip.setObjectName("shot-overlay-strip")
            self._strip = strip  # сохраняем ссылку для resizeEvent
            strip.setFixedHeight(72)
            sh = QHBoxLayout(strip)
            sh.setContentsMargins(10, 10, 10, 14)
            sh.setSpacing(10)
            sh.addStretch()

            # RefCard использует свой стиль `ref-overlay-btn` (меньше чем
            # shot-overlay-btn у шотов) — юзер просил не такой здоровый
            # overlay чтобы не закрывал картинку. Размер 32×32, не 44×44.
            # Также убрали addStretch перед кнопками — теперь они в правом
            # нижнем углу, а не центрированы по ширине strip.
            # 2026-05-08 редизайн: SVG-иконки Lucide вместо emoji/symbols.
            # Унифицированный визуал, нет проблем с рендерингом эмодзи.
            self.overlay_edit = QPushButton()
            self.overlay_edit.setObjectName("ref-overlay-btn")
            self.overlay_edit.setIcon(_load_lucide_icon("pencil"))
            self.overlay_edit.setIconSize(QSize(16, 16))
            self.overlay_edit.setCursor(Qt.CursorShape.PointingHandCursor)
            self.overlay_edit.setToolTip(tr('overlay_edit').split('\n')[-1])
            self.overlay_edit.clicked.connect(self.edit_requested)
            sh.addWidget(self.overlay_edit)

            # Точечное удаление этого рефа с диска (с подтверждением).
            self.overlay_delete = QPushButton()
            self.overlay_delete.setObjectName("ref-overlay-btn")
            self.overlay_delete.setIcon(_load_lucide_icon("trash-2"))
            self.overlay_delete.setIconSize(QSize(16, 16))
            self.overlay_delete.setCursor(Qt.CursorShape.PointingHandCursor)
            self.overlay_delete.setToolTip(tr('overlay_delete_ref'))
            self.overlay_delete.clicked.connect(self.delete_requested)
            sh.addWidget(self.overlay_delete)

            self.overlay_regen = QPushButton()
            self.overlay_regen.setObjectName("ref-overlay-btn")
            self.overlay_regen.setProperty("primary", True)
            self.overlay_regen.setIcon(_load_lucide_icon("sparkles"))
            self.overlay_regen.setIconSize(QSize(16, 16))
            self.overlay_regen.setCursor(Qt.CursorShape.PointingHandCursor)
            self.overlay_regen.setToolTip(tr('overlay_regen').split('\n')[-1])
            self.overlay_regen.clicked.connect(self.regen_requested)
            sh.addWidget(self.overlay_regen)
            # strip позиционируется абсолютно в resizeEvent (внизу overlay)

            # Плавный fade-in/out overlay (160мс)
            self._overlay_anim = _sa.setup_fade_overlay(self.overlay)

        # Прогресс-бар во время регенерации/edit'а
        self.progress_bar = QProgressBar(self.img_container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()

        # Busy-overlay: тёмная полупрозрачная плашка с подписью «Обновляю
        # описание…» — показывается пока работает фоновый
        # ClaudeGeometryThread. Юзер сразу видит на самой карточке что
        # она занята (статус-бара недостаточно — он внизу окна, юзер
        # часто его не замечает).
        self.busy_overlay = QFrame(self.img_container)
        self.busy_overlay.setObjectName("ref-busy-overlay")
        self.busy_overlay.setStyleSheet(
            "QFrame#ref-busy-overlay {"
            " background: rgba(20, 16, 30, 0.88);"
            " border-top-left-radius: 11px;"
            " border-top-right-radius: 11px;"
            "}"
        )
        bv = QVBoxLayout(self.busy_overlay)
        bv.setContentsMargins(16, 16, 16, 16)
        bv.setSpacing(10)
        bv.addStretch()
        # Большая иконка-эмодзи
        self.busy_icon_lbl = QLabel("🤖")
        self.busy_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.busy_icon_lbl.setStyleSheet("font-size: 36px;")
        bv.addWidget(self.busy_icon_lbl)
        # Текст с анимацией точек («Обновляю описание ·  /  ··  / ···»)
        self.busy_text_lbl = QLabel("")
        self.busy_text_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.busy_text_lbl.setWordWrap(True)
        self.busy_text_lbl.setStyleSheet(
            "color: #ffcc66; font-size: 13px; font-weight: 600;"
            "letter-spacing: 0.3px;")
        bv.addWidget(self.busy_text_lbl)
        bv.addStretch()
        self.busy_overlay.hide()
        # Таймер анимации точек (запускается в set_geometry_updating(True))
        self._busy_dot_step = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(450)
        self._busy_timer.timeout.connect(self._tick_busy_dots)

        cl.addWidget(self.img_container)

        # Текстовая метка прогресса под картинкой (label из RefGenerateThread.step:
        # «Генерирую… (12с)», «Сохраняю…», «Готово!»). Тот же стиль и логика
        # что у ShotCard.step_label — пользователь видит ровно тот же UX.
        self.step_label = QLabel("")
        self.step_label.setObjectName("step-label")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.step_label.setContentsMargins(8, 4, 8, 0)
        self.step_label.hide()
        cl.addWidget(self.step_label)

        # Низ — имя + тег + NEW-бейдж (после regen/edit, до ухода с refs).
        info = QWidget()
        info.setObjectName("ref-card-info")
        il = QVBoxLayout(info)
        il.setContentsMargins(14, 10, 14, 12)
        il.setSpacing(2)
        # 2026-05-07: NEW-бейдж рядом с именем (как у ShotCard). Видим
        # только когда `_unseen_refs[ep_id]` содержит путь этой картинки.
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        name_lbl = QLabel(r['name'])
        name_lbl.setObjectName("ref-name")
        name_row.addWidget(name_lbl)
        self.new_badge = QLabel("NEW")
        self.new_badge.setObjectName("new-badge")
        self.new_badge.hide()
        name_row.addWidget(self.new_badge)
        name_row.addStretch()
        il.addLayout(name_row)
        tag_lbl = QLabel(r['tag'])
        tag_lbl.setObjectName("ref-tag")
        il.addWidget(tag_lbl)
        cl.addWidget(info)

    def set_new_badge(self, visible: bool):
        """2026-05-07: показать/скрыть NEW-бейдж рядом с именем рефа."""
        try:
            if not hasattr(self, 'new_badge'):
                return
            self.new_badge.setVisible(bool(visible))
        except Exception:
            traceback.print_exc()

    def resizeEvent(self, ev):
        # Растягиваем картинку и overlay под актуальную ширину карточки
        w = self.img_container.width()
        h = self.IMG_H
        self.img_lbl.setGeometry(0, 0, w, h)
        if self.overlay is not None:
            self.overlay.setGeometry(0, 0, w, h)
            # Strip с кнопками — внизу overlay, ширина = w (абсолютное
            # позиционирование, не QVBoxLayout — чтобы не было «прыжка»
            # strip при первом fade_in)
            strip = getattr(self, '_strip', None)
            if strip is not None:
                STRIP_H = 72
                strip.setGeometry(0, h - STRIP_H, w, STRIP_H)
        if hasattr(self, 'busy_overlay'):
            self.busy_overlay.setGeometry(0, 0, w, h)
        self.progress_bar.setGeometry(0, h - 5, w, 5)
        super().resizeEvent(ev)

    def _tick_busy_dots(self):
        """Анимация точек в busy-overlay + счётчик секунд.
        2026-05-07: 2 фазы — image_updating («Генерирую изображение» /
        «Обновляю картинку») и geometry_updating («Обновляю описание»).
        В разное время активна одна; для location они идут последовательно."""
        if self._image_updating:
            try:
                base = tr(self._image_updating_label_key
                          or 'ref_busy_image_generic')
            except Exception:
                base = "Обновляю картинку"
        elif self._geometry_updating:
            try:
                base = tr('geom_busy_card_label')
            except Exception:
                base = "Обновляю описание"
        else:
            return
        self._busy_dot_step = (self._busy_dot_step + 1) % 3
        dots = ["·    ", "··  ", "···"][self._busy_dot_step]
        # 2026-05-07: счётчик секунд — `_busy_started_at` ставится в
        # set_image_updating(True) / set_geometry_updating(True). Если
        # карточка ребилдилась — счётчик может начаться с 0 (это OK).
        secs = 0
        try:
            import time as _time
            started = getattr(self, '_busy_started_at', 0)
            if started:
                secs = max(0, int(_time.time() - started))
        except Exception:
            pass
        secs_str = f"  ({secs}с)" if secs > 0 else ""
        self.busy_text_lbl.setText(f"{base} {dots}{secs_str}")

    def eventFilter(self, watched, event):
        """⚠ Тело обёрнуто в try/except: PyQt6 переводит непойманное Python
        исключение из eventFilter в qFatal()→abort() (краш всего приложения).

        ⚠ image_clicked.emit() вызывается через QTimer.singleShot(0, ...) —
        чтобы fullscreen-диалог открывался ПОСЛЕ возврата из event-filter,
        а не вложенным `exec()` прямо изнутри MouseButtonPress. На macOS 26
        прямой `dlg.exec()` в этом контексте регулярно крашил процесс.
        """
        try:
            if watched is self:
                # Карточка ЗАБЛОКИРОВАНА: идёт регенерация картинки FastGen'ом
                # ИЛИ ассистент CLI обновляет geometry в фоне. Игнорируем
                # пользовательские события (Enter/Leave не блокируем — они
                # стабильны на самой RefCard и не вызывают мигания overlay).
                if self._is_loading or self._geometry_updating or self._image_updating:
                    if event.type() in (QEvent.Type.MouseButtonPress,
                                        QEvent.Type.MouseButtonRelease,
                                        QEvent.Type.MouseButtonDblClick):
                        return True   # съедаем событие

                if event.type() == QEvent.Type.Enter:
                    # Защита от повторных Enter: запускаем fade_in только
                    # если overlay не показан
                    if self.overlay is not None and not self._overlay_visible:
                        self._overlay_visible = True
                        _sa.fade_in(self.overlay, self._overlay_anim)
                elif event.type() == QEvent.Type.Leave:
                    # Cursor-check: не скрываем overlay если курсор на нём
                    if self.overlay is not None and self._overlay_visible:
                        gtl = self.overlay.mapToGlobal(QPoint(0, 0))
                        rect = QRect(gtl, self.overlay.size())
                        if rect.contains(QCursor.pos()):
                            return False  # курсор на overlay — оставляем
                        self._overlay_visible = False
                        _sa.fade_out(self.overlay, self._overlay_anim)
        except Exception:
            traceback.print_exc()
        return super().eventFilter(watched, event)

    def leaveEvent(self, ev):
        """Страховка: курсор покинул всю карточку → overlay прячется."""
        try:
            if self.overlay is not None and self._overlay_visible:
                self._overlay_visible = False
                _sa.fade_out(self.overlay, self._overlay_anim)
        except Exception:
            traceback.print_exc()
        super().leaveEvent(ev)

    def set_loading(self, loading: bool):
        """Показ прогресс-бара во время регенерации/edit'а. Overlay прячется мгновенно."""
        self._is_loading = loading
        if loading:
            if self.overlay is not None:
                # Без анимации — просто скрываем overlay
                self.overlay.hide()
                self._overlay_visible = False
            self.progress_bar.setValue(0)
            self.progress_bar.show()
            self.progress_bar.raise_()
            self.step_label.setText("")
            self.step_label.show()
        else:
            self.progress_bar.hide()
            self.step_label.hide()
            self.step_label.setText("")

    def set_progress(self, label: str, pct: int):
        self.progress_bar.setValue(pct)
        # 2026-05-07: пока активен busy_overlay (image_updating /
        # geometry_updating) — step_label под картинкой НЕ показываем.
        # Overlay уже показывает «Генерирую изображение …» с точками,
        # дублировать «Генерирую… (4с)» снизу избыточно.
        overlay_busy = (getattr(self, '_image_updating', False)
                        or getattr(self, '_geometry_updating', False))
        if label and not overlay_busy:
            self.step_label.setText(label)
            self.step_label.show()
        elif overlay_busy:
            self.step_label.hide()

    def set_geometry_updating(self, busy: bool):
        """Помечает карточку как «занята обновлением geometry через ассистент CLI».
        Пока флаг True:
          • eventFilter блокирует все клики и hover-события на картинке
          • поверх картинки появляется тёмный busy-overlay с надписью
            «Обновляю описание …» и анимацией точек
          • курсор меняется на стандартный (не PointingHand)
        После завершения всё откатывается обратно."""
        import time as _time
        self._geometry_updating = busy
        if busy:
            # 2026-05-07: счётчик секунд geometry-фазы.
            self._busy_started_at = _time.time()
            # step_label дублирует overlay — прячем.
            if hasattr(self, 'step_label'):
                self.step_label.hide()
            # hover-overlay (regen/edit) скрыть мгновенно — он больше не нужен
            if self.overlay is not None:
                # Без анимации — просто скрываем overlay
                self.overlay.hide()
                self._overlay_visible = False
            # busy-overlay поверх картинки + анимация
            if hasattr(self, 'busy_overlay'):
                # Растянуть на текущую геометрию контейнера (resize мог не сработать
                # если карточка только создана и не успела пройти layout-проход)
                w = self.img_container.width()
                h = self.IMG_H
                if w > 0:
                    self.busy_overlay.setGeometry(0, 0, w, h)
                self.busy_overlay.show()
                self.busy_overlay.raise_()
                self._busy_dot_step = 0
                self._tick_busy_dots()      # сразу первый кадр анимации
                self._busy_timer.start()
            try:
                self.img_container.setCursor(Qt.CursorShape.ArrowCursor)
            except Exception:
                pass
        else:
            if hasattr(self, '_busy_timer'):
                self._busy_timer.stop()
            if hasattr(self, 'busy_overlay'):
                self.busy_overlay.hide()
            try:
                self.img_container.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass

    def set_image_updating(self, busy: bool, label_key: str = '',
                             started_at: float = None):
        """2026-05-07: фаза «генерация/редактирование картинки» (FastGen).
        Аналог `set_geometry_updating`, но с другим label_key. Используется
        ВО ВРЕМЯ работы RefGenerateThread (regen/edit). После завершения
        фазы — для location включается geometry-фаза, для object на этом
        всё заканчивается.

        label_key: i18n-ключ для текста («ref_busy_image_location» —
        «Генерирую изображение», «ref_busy_image_object» — «Обновляю
        картинку»). Если пусто → используется generic.
        started_at: timestamp начала фазы (опц.) — чтобы счётчик секунд
        НЕ сбрасывался при пересоборе карточки (передаётся из
        `_active_image_paths` registry).
        """
        import time as _time
        self._image_updating = busy
        self._image_updating_label_key = label_key or ''
        if busy:
            # hover-overlay (regen/edit) скрыть мгновенно
            if self.overlay is not None:
                self.overlay.hide()
                self._overlay_visible = False
            # 2026-05-07: step_label под картинкой («Генерирую… (4с)») —
            # не нужен пока overlay активен, дублирует надпись.
            if hasattr(self, 'step_label'):
                self.step_label.hide()
            # 2026-05-07: запоминаем когда фаза стартовала — для счётчика
            # секунд в overlay-надписи. Если карточка пересоздана rebuild'ом —
            # `started_at` приходит из registry (`_active_image_paths`), и
            # счётчик показывает реальное время от начала генерации, а не
            # сбрасывается на 0.
            self._busy_started_at = (started_at if started_at
                                       else _time.time())
            if hasattr(self, 'busy_overlay'):
                w = self.img_container.width()
                h = self.IMG_H
                if w > 0:
                    self.busy_overlay.setGeometry(0, 0, w, h)
                self.busy_overlay.show()
                self.busy_overlay.raise_()
                self._busy_dot_step = 0
                self._tick_busy_dots()
                self._busy_timer.start()
            try:
                self.img_container.setCursor(Qt.CursorShape.ArrowCursor)
            except Exception:
                pass
        else:
            # Если geometry-фаза активна — НЕ скрываем overlay (она
            # продолжит крутить точки со своим label'ом). Иначе скрываем.
            if not self._geometry_updating:
                if hasattr(self, '_busy_timer'):
                    self._busy_timer.stop()
                if hasattr(self, 'busy_overlay'):
                    self.busy_overlay.hide()
                try:
                    self.img_container.setCursor(
                        Qt.CursorShape.PointingHandCursor)
                except Exception:
                    pass

    def update_image(self, path: Path):
        """Перечитывает картинку с диска (после регенерации)."""
        try:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.img_lbl.setPixmap(pixmap)
        except Exception:
            pass
