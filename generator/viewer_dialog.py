# -*- coding: utf-8 -*-
"""
generator/viewer_dialog.py — попап просмотра результата Генератора (Кусок 2/4).

Клик по готовой плитке (ShimmerCell, state image/video) → большое non-modal окно:
  • КАРТИНКА: зум колесом + панорама через StoryboardView (widgets/face_grid/grid_dialog
    — тот же зум-движок, что у попапа шота; не дублируем).
  • ВИДЕО: нативный QVideoWidget + QMediaPlayer + QAudioOutput. Ряд кнопок ПОД видео (на
    фоне окна; play/pause крупная по центру, слева возврат/папка/звук) + таймлайн-трек снизу
    (playhead с флажком и плюсиком-«взять кадр», риски по секундам). БЕЗ autoplay — первый
    кадр виден (короткий play→pause пинок под mute в showEvent: на Stopped setPosition(0)
    кадр не рендерит — проверено), играет по кнопке. Локальный mute плеера попапа (кнопка
    звука). Без лупа: доиграло → стоп. closeEvent → stop (звук не висит).

Промпт / чипы рефов / кнопки — Куски 3-4, здесь НЕТ.

Фон окна — из темы (LUMZ_THEME['bg_main']), не хардкод. Окно Qt.WindowType.Tool,
показывается через .show() (non-modal): грид/генерация продолжают работать.

Cross-platform: pathlib.Path, ленивые импорты QtMultimedia/QtMultimediaWidgets и
StoryboardView (frozen guard — если модуля нет, деградируем без падения).
"""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QSize, QTimer, QRectF, QPointF, QEvent, pyqtSignal
from PyQt6.QtGui import (QPixmap, QImage, QIcon, QPainter, QPen, QFont, QPolygonF,
                         QPalette)
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QToolButton, QWidget)

from views.theme import LUMZ_THEME, theme_qcolor
from i18n import tr   # локализация UI (i18n — лист-модуль, без circular import)

# Частота применения перемотки при таскании seek-bar (троттл): setPosition НЕ шлём на
# каждый sliderMoved — декодер захлёбывается и коалесцирует быстрые seek'и (кадр застывает
# до остановки мыши). Вместо этого копим последнюю позицию и применяем её по таймеру с
# этим интервалом, чтобы промежуточные кадры рисовались по ходу таскания.
# ИСПОЛЬЗУЕТСЯ ТОЛЬКО в грейсфул-фоллбэке (pre-decode провалился). При готовом кэше скраб
# идёт по RAM-кэшу мгновенно (см. scrub_decoder + _frame_at), троттл/таймер не участвуют.
_SEEK_THROTTLE_MS = 50

# Режим overlay скраб-превью поверх НАТИВНОГО QVideoWidget.
#  • "child" — overlay дочерний САМОГО vw, raise_() поверх; vw НЕ прячем.
#  • "hide"  — overlay дочерний _video_frame (сиблинг vw); на время drag vw.hide(),
#              на release vw.show().
# ПРОВЕРЕНО НА .app (2026-06-29): "child" НЕ работает — нативный видео-слой macOS
# перекрывает QLabel-overlay (при drag чёрный экран вместо кадра, как было с pillarbox).
# Рабочий режим — "hide": прячем vw на время drag, показываем overlay-сиблинг.
_SCRUB_OVERLAY_MODE = "hide"


class _TimelineTrack(QWidget):
    """Дорожка-таймлайн вместо круглого хэндла seek-bar: тёмный клип-трек со скруглением,
    рисками по секундам (1/сек, из реальной длительности) и вертикальным accent-playhead'ом.
    Клик/таскание по треку → перемотка.

    API повторяет нужные методы QSlider (value/setValue/setRange/isSliderDown) и сигналы
    sliderPressed/sliderMoved/sliderReleased — чтобы переиспользовать троттл-скраб хендлеры
    диалога без изменений. Рисование — иллюзия монтажки, БЕЗ декода кадров: playhead едет
    плавно сам, реальный кадр догоняет через троттл-setPosition.
    """

    sliderPressed = pyqtSignal()
    sliderMoved = pyqtSignal(int)
    sliderReleased = pyqtSignal()
    grabRequested = pyqtSignal()   # клик по плюсику у playhead → взять кадр (заглушка диалога)

    _PAD = 8          # гориз. отступ внутри трека: playhead у краёв не обрезается
    _HEAD = 26        # верх клип-трека; место под флажок+плюсик добавлено СВЕРХУ (клип прежний)
    _FLAG_TOP = 17    # верх (база) флажка-треугольника; остриё вниз в _HEAD (на линии)
    _PLUS_SZ = 22     # размер кнопки-плюсика
    _PLUS_DX = 10     # сдвиг плюсика ВПРАВО от линии playhead (плюсик сбоку, не над флажком)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dur = 0          # длительность, мс
        self._pos = 0          # позиция playhead, мс
        self._down = False     # идёт ли таскание
        self.setObjectName("viewer-track")
        self.setFixedHeight(59)     # место под флажок+плюсик СВЕРХУ (_HEAD) + клип прежней высоты
        self.setMinimumWidth(220)
        self.setMaximumWidth(600)     # дорожка компактна: не растягивается на весь экран
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Кнопка-плюсик СПРАВА от флажка playhead: едет вместе с полоской (см. _reposition_plus);
        # клик → grabRequested. Отдельный дочерний QToolButton → его клик НЕ доходит до
        # mousePressEvent трека (перемотки) — плюсик кликается отдельно от скраба.
        self._plus_btn = QToolButton(self)
        self._plus_btn.setObjectName("viewer-plus")
        self._plus_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._plus_btn.setFixedSize(self._PLUS_SZ, self._PLUS_SZ)
        self._plus_btn.setIconSize(QSize(13, 13))
        self._plus_btn.setToolTip(tr('gen_tt_grab_frame'))
        try:
            from generator.result_cell import _tinted_icon
            # ТЁМНЫЙ знак «+» (bg_main) на accent-фоне кнопки — как флажок, читаемо
            self._plus_btn.setIcon(_tinted_icon("plus", LUMZ_THEME["bg_main"]))
        except Exception:
            pass
        self._plus_btn.setStyleSheet(
            "QToolButton#viewer-plus {"
            f" background:{LUMZ_THEME['accent_red']}; border:none; border-radius:6px; }}"
        )
        self._plus_btn.clicked.connect(self._emit_grab)
        self._plus_btn.setVisible(False)   # 2026-07-11: плюсик перенесён в ряд кнопок управления
        self._reposition_plus()

    # --- QSlider-совместимый интерфейс (переиспользуем хендлеры скраба диалога) ---
    def setRange(self, lo, hi):
        self._dur = max(0, int(hi))
        self._pos = max(0, min(self._pos, self._dur))
        self._reposition_plus()
        self.update()

    def setValue(self, ms):
        self._pos = max(0, min(int(ms), self._dur))
        self._reposition_plus()
        self.update()

    def value(self):
        return int(self._pos)

    def isSliderDown(self):
        return self._down

    # --- маппинг x<->мс ---
    def _x_to_ms(self, x):
        w = self.width() - 2 * self._PAD
        if w <= 0 or self._dur <= 0:
            return 0
        frac = max(0.0, min(1.0, (x - self._PAD) / w))
        return int(frac * self._dur)

    def _ms_to_x(self, ms):
        w = self.width() - 2 * self._PAD
        if self._dur <= 0:
            return float(self._PAD)
        return self._PAD + (ms / self._dur) * w

    # --- мышь: клик/таскание по треку = перемотка ---
    #     Гард `if not self.isEnabled()` — пока кэш кадров греется (спиннер), трек
    #     ЗАБЛОКИРОВАН: клик/таскание не должны скрабить до готовности (иначе заикание).
    def mousePressEvent(self, ev):
        if not self.isEnabled():
            return super().mousePressEvent(ev)
        if ev.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self._pos = self._x_to_ms(ev.position().x())
            self._reposition_plus()
            self.update()
            self.sliderPressed.emit()
            self.sliderMoved.emit(self._pos)
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if not self.isEnabled():
            return super().mouseMoveEvent(ev)
        if self._down:
            self._pos = self._x_to_ms(ev.position().x())
            self._reposition_plus()
            self.update()
            self.sliderMoved.emit(self._pos)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if not self.isEnabled():
            return super().mouseReleaseEvent(ev)
        if ev.button() == Qt.MouseButton.LeftButton and self._down:
            self._down = False
            self._pos = self._x_to_ms(ev.position().x())
            self.update()
            self.sliderReleased.emit()
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)

    def changeEvent(self, ev):
        # EnabledChange: при блокировке (прогрев кэша) прячем плюсик-«взять кадр» и
        # перерисовываем приглушённо; при разблокировке возвращаем.
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.EnabledChange:
            if self._plus_btn is not None:
                self._plus_btn.setVisible(False)   # плюсик перенесён в ряд кнопок — на дорожке скрыт
            self.update()

    # --- плюсик у playhead: позиционирование за полоской + сигнал «взять кадр» ---
    def _reposition_plus(self):
        """Поставить плюсик строго по X линии playhead + _PLUS_DX, БЕЗ зажима у краёв —
        он едет за линией всю дорожку и спокойно уезжает ЗА правую границу трека вместе с
        ней (не упирается раньше, не расходится). Плюсик — дочерний контейнера дорожки
        (нижней панели), а НЕ трека: иначе клиппинг по краю трека обрезал бы его в конце.
        Зовётся при смене позиции/диапазона/таскании/resize/move."""
        # 2026-07-11: плюсик «взять кадр» перенесён в ряд кнопок управления → на дорожке
        # больше НЕ показываем (позиционирование за playhead отключено, кнопка спрятана).
        btn = getattr(self, "_plus_btn", None)
        if btn is not None:
            btn.setVisible(False)

    def moveEvent(self, ev):
        # трек центрируется в панели → при ресайзе окна он СДВИГАЕТСЯ (не всегда ресайзится),
        # плюсик (дочерний панели) надо переставить вслед.
        super().moveEvent(ev)
        self._reposition_plus()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._reposition_plus()

    def _emit_grab(self):
        self.grabRequested.emit()

    # --- рисование клип-трека (всё из токенов LUMZ_THEME через theme_qcolor) ---
    def paintEvent(self, ev):
        t = LUMZ_THEME
        head = self._HEAD
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            p.setOpacity(0.4)   # прогрев кэша: трек приглушён (скраб недоступен)
        # клип-трек: тёмный фон + рамка, скруглённые углы (ниже зоны плюсика+флажка _HEAD)
        rect = QRectF(0.5, head + 0.5, self.width() - 1, self.height() - head - 1)
        p.setPen(QPen(theme_qcolor(t["border_strong"]), 1))
        p.setBrush(theme_qcolor(t["bg_main"]))
        p.drawRoundedRect(rect, 6, 6)
        if self._dur <= 0:
            p.end()
            return
        # деления по секундам: 1 риска/сек (4с→4, 8с→8), цифры если не сливаются
        n = int(round(self._dur / 1000.0))
        w = self.width() - 2 * self._PAD
        show_nums = (n > 0 and (w / n) >= 16)
        p.setFont(QFont(self.font().family(), 7))
        for k in range(0, n + 1):
            x = self._PAD + (k / n) * w if n > 0 else self._PAD
            p.setPen(QPen(theme_qcolor(t["border_strong"]), 1))
            p.drawLine(int(x), head + 5, int(x), self.height() - 11)
            if show_nums:
                p.setPen(QPen(theme_qcolor(t["text_secondary"]), 1))
                p.drawText(QRectF(x - 9, self.height() - 12, 18, 11),
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                           str(k))
        # playhead: линия 3px (НЕЧЁТНАЯ → чёткий центральный пиксель на cx) + флажок-головка,
        # центрированный по ТОЙ ЖЕ оси cx. Раньше линия 2px давала пол-пикселя → флажок съезжал.
        cx = round(self._ms_to_x(self._pos))
        accent = theme_qcolor(t["accent_red"])
        # линию рисуем БЕЗ сглаживания → чёткие пиксели cx-1,cx,cx+1 (нечётная ширина 3px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(accent, 3))
        p.drawLine(cx, head, cx, self.height() - 4)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)   # вернули AA для гладких скосов
        # флажок: ▽ по центру линии (_FLAG_TOP..верх клипа _HEAD), остриём вниз к линии
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        hw = 6
        flag = QPolygonF([QPointF(cx - hw, float(self._FLAG_TOP)),
                          QPointF(cx + hw, float(self._FLAG_TOP)),
                          QPointF(float(cx), float(head))])
        p.drawPolygon(flag)
        p.end()


class _VideoFrame(QWidget):
    """Контейнер с фоном LUMZ_THEME['bg_main']: вписывает дочерний QVideoWidget в свой
    прямоугольник ПО АСПЕКТУ видео и центрирует. QVideoWidget при этом ровно по размеру
    кадра и сам НЕ леттербоксит чёрным — поля вокруг (pillarbox/letterbox) заполняет фон
    контейнера (bg_main).

    Почему так, а не палитра/setStyleSheet на самом QVideoWidget: нативный видео-слой
    рисуется ПОВЕРХ фона виджета и закрашивает pillarbox чёрным независимо от палитры
    (проверено: палитра показывала bg_main в grab пустого виджета, но на реальном .app
    поля оставались чёрными). Контейнер же — обычный QWidget, его фон рисуется реально.
    """

    def __init__(self, video_widget, ratio_w, ratio_h, parent=None, on_fit=None):
        super().__init__(parent)
        self._vw = video_widget
        self._vw.setParent(self)
        self._rw = max(1, int(ratio_w))
        self._rh = max(1, int(ratio_h))
        self._on_fit = on_fit     # коллбэк после раскладки vw: центрировать спиннер/overlay
        self.setObjectName("viewer-videoframe")
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, theme_qcolor(LUMZ_THEME["bg_main"]))
        self.setPalette(pal)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _fit(self):
        """Наибольший прямоугольник аспекта _rw:_rh, вписанный в контейнер, по центру."""
        cw, ch = self.width(), self.height()
        if cw <= 0 or ch <= 0:
            return
        w = cw
        h = int(w * self._rh / self._rw)
        if h > ch:
            h = ch
            w = int(h * self._rw / self._rh)
        self._vw.setGeometry((cw - w) // 2, (ch - h) // 2, w, h)
        if self._on_fit is not None:
            try:
                self._on_fit()
            except Exception:
                pass


class _BusySpinner(QWidget):
    """Круглый indeterminate-спиннер: вращающаяся accent-дуга (LUMZ_THEME['accent_red'])
    поверх превью, пока греется кэш кадров. paintEvent+QPainter+QTimer — принятый в проекте
    путь (QSS-анимаций на macOS-нативных виджетах нет). Прозрачный фон → виден кадр под ним."""

    def __init__(self, parent=None, diameter: int = 48):
        super().__init__(parent)
        self._d = int(diameter)
        self._angle = 0
        self.setFixedSize(self._d, self._d)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(28)   # ~36 кадров/сек вращения
        self._timer.timeout.connect(self._tick)

    def start(self):
        self.show()
        self.raise_()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        if self._timer.isActive():
            self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        m = 5
        rect = QRectF(m, m, self._d - 2 * m, self._d - 2 * m)
        # фоновое кольцо (приглушённое) + яркая дуга-«голова» (accent), крутится по _angle
        p.setPen(QPen(theme_qcolor(LUMZ_THEME["bg_subtle"]), 4))
        p.drawArc(rect, 0, 360 * 16)
        pen = QPen(theme_qcolor(LUMZ_THEME["accent_red"]), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        # старт-угол в 1/16°, отсчёт против часовой; минус → крутим по часовой
        p.drawArc(rect, -self._angle * 16, 100 * 16)
        p.end()


class GeneratorViewerDialog(QDialog):
    """Большой просмотр одной готовой плитки генератора (картинка с зумом / видео-плеер).

    Минимальный (Кусок 2): только просмотр. Промпт/рефы/кнопки добавятся в Кусках 3-4.
    """

    def __init__(self, result_path: str, meta: dict, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self._result_path = str(result_path or "")
        self._meta = meta if isinstance(meta, dict) else {}
        self._player = None
        self._audio = None
        self._primed = False         # первый кадр показан (один раз в showEvent)
        self._btn_play = None        # главная кнопка play/pause (ряд под видео)
        self._btn_return = None      # ← вернуть в генератор (заглушка, Куски 3-4)
        self._btn_folder = None      # ← показать в Finder (рабочая)
        self._btn_mute = None        # ← локальный mute плеера попапа (рабочая)
        self._seek = None            # таймлайн-трек (_TimelineTrack, QSlider-совместимый)
        self._icon_play = None
        self._icon_pause = None
        self._icon_vol = None
        self._icon_volx = None
        self._muted = False           # локальный mute плеера попапа
        self._was_playing = False     # скраб: играло ли видео до захвата ползунка
        self._seek_timer = None       # троттл-таймер перемотки (ТОЛЬКО фоллбэк, кэш провален)
        self._pending_seek = None     # последняя позиция, ждущая применения троттлом (фоллбэк)
        self._last_seek_apply = 0.0   # monotonic-время последнего setPosition (фоллбэк-троттл)
        # ── живой скраб (A+C): pre-decode кэш кадров в RAM + overlay-превью ──
        self._scrub_cache = None      # list[(ts_ms:int, jpeg_bytes)] | None (None=нет/греется)
        self._scrub_ts = []           # параллельный список ts (для bisect-lookup по позиции)
        self._preload = None          # ScrubPreloadThread (parent=None, ссылка тут — паттерн A)
        self._scrub_overlay = None    # QLabel-превью кадра во время drag
        self._spinner = None          # _BusySpinner поверх превью на время прогрева кэша
        self._scrub_warming = False   # идёт ли прогрев (контролы disabled, спиннер крутится)
        # Гладкий стык overlay→видео на release (анти-мерцание «кадр назад»): держим overlay
        # пока vw не декодирует кадр на финальной позиции (videoFrameChanged) или таймаут.
        self._handoff_timer = None
        self._handoff_sink = None
        self._handoff_active = False
        self._is_video = (self._meta.get("type") == "video")
        aspect = self._meta.get("aspect", "16:9")

        self.setWindowTitle(tr('gen_viewer_title'))
        # Фон окна — из темы (bg_main), не сырой hex.
        self.setStyleSheet(f"QDialog {{ background:{LUMZ_THEME['bg_main']}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        if self._is_video:
            self._build_video(lay)
        else:
            self._build_image(lay)

        w, h = self._target_size(aspect)
        self.resize(w, h)
        # Центрируем окно по активному экрану (кроссплатформенно: Mac/Win).
        # До .show() self.screen() может быть None → фолбэк на primaryScreen().
        try:
            from PyQt6.QtWidgets import QApplication
            scr = self.screen() or QApplication.primaryScreen()
            avail = scr.availableGeometry()
            self.move(avail.x() + (avail.width() - w) // 2,
                      avail.y() + (avail.height() - h) // 2)
        except Exception:
            pass

    # ── картинка: StoryboardView (зум колесом + панорама) + панель снизу ──
    def _build_image(self, lay: QVBoxLayout):
        pix = QPixmap(self._result_path)
        try:
            # Ленивый импорт (frozen/circular guard — как в shot_viewer_dialog).
            from widgets.face_grid.grid_dialog import StoryboardView
            self._view = StoryboardView(pix)
            lay.addWidget(self._view, 1)   # картинка занимает всё место над панелью
        except Exception:
            # Фолбэк: StoryboardView недоступен → статичная картинка без зума.
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background:transparent;")
            if not pix.isNull():
                lbl.setPixmap(pix)
            lbl.setScaledContents(False)
            lay.addWidget(lbl, 1)
        # Нижняя панель: ВОЗВРАТ + ПАПКА (как у видео-ряда, но без плеера/таймлайна/звука).
        lay.addWidget(self._build_image_button_row())

    # ── ряд кнопок ПОД картинкой: возврат + папка (переиспользует видео-логику) ──
    def _build_image_button_row(self) -> QWidget:
        """Нижняя панель картиночного попапа: ДВЕ кнопки — возврат (corner-up-left) и
        папка (folder-open). Переиспользует _mk_row_btn (28×28, Lucide-иконка), стиль
        _controls_qss и СУЩЕСТВУЮЩИЕ обработчики _on_return_clicked / _on_reveal_clicked
        (логику не дублируем). Без play/pause, таймлайна, звука — их у картинки нет."""
        bar = QWidget(self)
        bar.setObjectName("viewer-btnrow")
        bar.setFixedHeight(44)
        bar.setStyleSheet(self._controls_qss())
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 0, 12, 0)
        hb.setSpacing(6)
        self._btn_return = self._mk_row_btn(
            "corner-up-left", 28, 18, self._on_return_clicked, tr('gen_tt_return'), bar)
        self._btn_folder = self._mk_row_btn(
            "folder-open", 28, 18, self._on_reveal_clicked, tr('gen_tt_show_finder'), bar)
        hb.addStretch(1)
        hb.addWidget(self._btn_return)
        hb.addWidget(self._btn_folder)
        hb.addStretch(1)
        return bar

    # ── видео: QVideoWidget + плеер + ряд кнопок + таймлайн-трек ────────────
    def _build_video(self, lay: QVBoxLayout):
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PyQt6.QtMultimediaWidgets import QVideoWidget
        except Exception:
            # QtMultimedia недоступен (frozen без модуля) → показать кадр-превью или текст.
            jpg = str(Path(self._result_path).with_suffix(".jpg"))
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = QPixmap(jpg)
            if not pm.isNull():
                lbl.setPixmap(pm)
            else:
                lbl.setText(tr('gen_viewer_no_player'))
                lbl.setStyleSheet("color:rgba(255,255,255,0.55); font-size:13px;")
            lay.addWidget(lbl)
            return
        # WA_TransparentForMouseEvents НЕ ставим: он влияет только на доставку mouse-событий,
        # не на рендер кадров. Клик мышью по видео = play/pause (как ютуб); кнопка play в ряду
        # тоже работает. Одиночный ЛКМ по QVideoWidget → _toggle_play_pause.
        class _ClickableVideo(QVideoWidget):
            clicked = pyqtSignal()
            def mouseReleaseEvent(self, ev):
                if ev.button() == Qt.MouseButton.LeftButton:
                    self.clicked.emit()
                super().mouseReleaseEvent(ev)
        vw = _ClickableVideo()
        self._video_widget = vw
        try:
            vw.clicked.connect(self._toggle_play_pause)
        except Exception:
            pass
        self._audio = QAudioOutput(self)
        # Стартовый mute берём из глобального флага генератора, дальше — локальная кнопка звука.
        self._muted = bool(getattr(self.parent(), "_video_muted", False))
        self._audio.setMuted(self._muted)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(vw)
        self._player.setSource(QUrl.fromLocalFile(self._result_path))
        # Видео завёрнуто в контейнер-фон bg_main (_VideoFrame): вписывает кадр по аспекту и
        # центрирует → поля вокруг = bg_main, а не чёрный (палитра на самом QVideoWidget
        # pillarbox не красит — нативный видео-слой поверх; проверено эмпирически).
        rw, rh = (9, 16) if self._meta.get("aspect") == "9:16" else (16, 9)
        self._video_frame = _VideoFrame(vw, rw, rh, self, on_fit=self._sync_scrub_geometry)
        # Скраб-overlay (кадр-превью при drag) + спиннер прогрева. Родитель overlay зависит
        # от режима (_SCRUB_OVERLAY_MODE — точка проверки на .app): "child" → поверх нативного
        # vw; "hide" → сиблинг в _video_frame (vw прячем на время drag). Спиннер всегда в
        # _video_frame (по центру превью).
        ov_parent = vw if _SCRUB_OVERLAY_MODE == "child" else self._video_frame
        self._scrub_overlay = QLabel(ov_parent)
        self._scrub_overlay.setObjectName("viewer-scrub-overlay")
        # БЕЗ setScaledContents: кадр полноразмерный (= качество видео), вписываем сами в
        # _update_scrub_overlay через pm.scaled(..., SmoothTransformation) — гладко, без блюра
        # (setScaledContents масштабирует грубо/без сглаживания). Центрируем по overlay.
        self._scrub_overlay.setScaledContents(False)
        self._scrub_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scrub_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._scrub_overlay.hide()
        self._spinner = _BusySpinner(self._video_frame)
        self._spinner.hide()
        # Порядок сверху вниз: видео (stretch=1) → РЯД КНОПОК (на фоне окна) → дорожка.
        # Кнопки в своей строке НИЖЕ видео — не перекрывают изображение.
        lay.addWidget(self._video_frame, 1)
        lay.addWidget(self._build_button_row())
        lay.addWidget(self._build_controls())
        # Иконка кнопки следует за состоянием плеера; трек — за длительностью/позицией.
        try:
            self._player.playbackStateChanged.connect(lambda *_: self._update_play_icon())
            self._player.durationChanged.connect(self._on_duration)
            self._player.positionChanged.connect(self._on_position)
            self._player.mediaStatusChanged.connect(self._on_media_status)
        except Exception:
            pass
        self._update_play_icon()

    # ── ряд кнопок ПОД видео (на фоне окна): слева мелкие, play/pause крупная по центру ──
    def _build_button_row(self) -> QWidget:
        # Иконки play/pause (24px) + звук (vol/volx) — Lucide через _tinted_icon.
        self._icon_play = self._tint("play")
        self._icon_pause = self._tint("pause")
        self._icon_vol = self._tint("volume-2")
        self._icon_volx = self._tint("volume-x")

        bar = QWidget(self)
        bar.setObjectName("viewer-btnrow")
        bar.setFixedHeight(44)
        bar.setStyleSheet(self._controls_qss())
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(12, 0, 12, 0)
        outer.setSpacing(0)

        # Внутренний контейнер ТОЙ ЖЕ раскладки, что дорожка снизу (stretch 6, ≤600) → его
        # левый край совпадает с левым краем трека, а центр — с центром окна.
        inner = QWidget(bar)
        inner.setObjectName("viewer-btnrow-inner")
        inner.setMinimumWidth(240)
        inner.setMaximumWidth(600)
        ih = QHBoxLayout(inner)
        ih.setContentsMargins(0, 0, 0, 0)
        ih.setSpacing(0)

        # левая группа мелких кнопок (28×28), 3 шт — начинается на левом крае дорожки
        LG_GAP = 6
        self._btn_return = self._mk_row_btn("corner-up-left", 28, 18, self._on_return_clicked,
                                            tr('gen_tt_return'), inner)
        self._btn_folder = self._mk_row_btn("folder-open", 28, 18, self._on_reveal_clicked,
                                            tr('gen_tt_show_finder'), inner)
        self._btn_mute = self._mk_row_btn("volume-2", 28, 18, self._on_toggle_mute,
                                          tr('gen_tt_sound'), inner)
        left = QHBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(LG_GAP)
        for b in (self._btn_return, self._btn_folder, self._btn_mute):
            left.addWidget(b)
        lg_w = 3 * 28 + 2 * LG_GAP   # ширина левой группы (3 кнопки) → симметричный spacer справа

        # play/pause — крупная (40×40), главная, ровно по центру контейнера
        self._btn_play = self._mk_row_btn("play", 40, 24, self._toggle_play_pause, tr('gen_tt_playpause'), inner)
        self._btn_play.setObjectName("viewer-playpause")

        # плюсик «взять кадр» — перенесён с ползунка таймлайна в ПРАВУЮ часть ряда.
        self._btn_grab = self._mk_row_btn("plus", 28, 16, self._on_grab_frame,
                                          tr('gen_tt_grab_frame'), inner)

        ih.addLayout(left)
        ih.addStretch(1)
        ih.addWidget(self._btn_play)
        ih.addStretch(1)
        ih.addSpacing(lg_w - 28)     # зеркало левой группы минус плюсик → play/pause по центру
        ih.addWidget(self._btn_grab) # плюсик у правого края ряда

        outer.addStretch(1)
        outer.addWidget(inner, 6)
        outer.addStretch(1)
        self._update_mute_icon()
        return bar

    def _tint(self, name: str) -> QIcon:
        """Lucide SVG в text_primary через result_cell._tinted_icon (frozen/circular guard)."""
        try:
            from generator.result_cell import _tinted_icon
            return _tinted_icon(name, LUMZ_THEME["text_primary"])
        except Exception:
            return QIcon()

    def _mk_row_btn(self, icon_name, size, icon_px, on_click, tooltip, parent) -> QToolButton:
        b = QToolButton(parent)
        b.setObjectName("viewer-rowbtn")
        b.setFixedSize(size, size)
        b.setIconSize(QSize(icon_px, icon_px))
        ic = self._tint(icon_name)
        if ic is not None:
            b.setIcon(ic)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            b.setToolTip(tooltip)
        if on_click is not None:
            b.clicked.connect(on_click)
        return b

    # ── нижняя панель под видео: таймлайн-трек по центру (playhead + риски) ──
    def _build_controls(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("viewer-controls")
        bar.setFixedHeight(68)
        bar.setStyleSheet(self._controls_qss())
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 0, 12, 0)
        hb.setSpacing(0)

        # Дорожка-таймлайн центрирована и НЕ растягивается: stretch по бокам + maxWidth у
        # трека (overflow при широком окне уходит в боковые spacer'ы → трек ≤600 по центру).
        hb.addStretch(1)
        self._seek = _TimelineTrack(bar)
        self._seek.setRange(0, 0)
        # Live-скраб: захват → пауза (отзывчиво, без частящего аудио); таскание → копим
        # позицию и применяем её ТРОТТЛОМ (таймер, _SEEK_THROTTLE_MS) — прямой setPosition
        # на каждое движение декодер коалесцирует, кадр застывает до остановки мыши;
        # отпускание → финальная позиция + продолжить play, если играло. positionChanged
        # двигает playhead сам, но только когда юзер его не держит (см. _on_position).
        self._seek.sliderPressed.connect(self._on_seek_pressed)
        self._seek.sliderMoved.connect(self._on_seek_moved)
        self._seek.sliderReleased.connect(self._on_seek_released)
        self._seek.grabRequested.connect(self._on_grab_frame)   # плюсик у playhead → взять кадр
        # Троттл-таймер: применяет накопленную позицию раз в _SEEK_THROTTLE_MS.
        self._seek_timer = QTimer(self)
        self._seek_timer.setInterval(_SEEK_THROTTLE_MS)
        self._seek_timer.timeout.connect(self._flush_pending_seek)
        hb.addWidget(self._seek, 6)
        hb.addStretch(1)
        return bar

    def _controls_qss(self) -> str:
        """QSS панели/кнопки из токенов темы (сам трек рисуется в _TimelineTrack.paintEvent)."""
        t = LUMZ_THEME
        return (
            # ряд кнопок — на фоне окна (transparent → виден bg_main диалога)
            "QWidget#viewer-btnrow { background:transparent; }"
            "QWidget#viewer-btnrow-inner { background:transparent; }"
            "QToolButton#viewer-rowbtn {"
            " background:transparent; border:none; border-radius:6px; }"
            f"QToolButton#viewer-rowbtn:hover {{ background:{t['bg_hover']}; }}"
            "QToolButton#viewer-playpause {"
            " background:transparent; border:none; border-radius:8px; }"
            f"QToolButton#viewer-playpause:hover {{ background:{t['bg_hover']}; }}"
            # нижняя панель дорожки — слегка приподнятая поверхность
            "QWidget#viewer-controls {"
            f" background:{t['bg_subtle']}; border-top:1px solid {t['border_default']}; }}"
            "QWidget#viewer-track { background:transparent; }"
        )

    def _target_size(self, aspect: str):
        """Крупный размер окна под формат, вписанный в доступную область экрана."""
        max_w, max_h = 1280, 800
        try:
            from PyQt6.QtWidgets import QApplication
            avail = QApplication.primaryScreen().availableGeometry()
            max_w = int(avail.width() * 0.6)
            max_h = int(avail.height() * 0.6)
        except Exception:
            pass
        rw, rh = (9, 16) if aspect == "9:16" else (16, 9)
        w = max_w
        h = int(w * rh / rw)
        if h > max_h:
            h = max_h
            w = int(h * rw / rh)
        return max(480, w), max(360, h)

    # ── AUTOPLAY: старт в showEvent. Виджет к этому моменту показан → есть нативная
    #    поверхность (нет чёрного кадра); не зависим от hover/enterEvent, который НЕ
    #    приходит, когда окно открывается прямо под курсором (корень бага). ──────────
    def showEvent(self, ev):
        super().showEvent(ev)
        # АВТОЗАПУСК с начала при открытии попапа: setPosition(0) + play() (по требованию —
        # клик по карточке сразу играет видео в попапе, ручной play не нужен). Иконку
        # play/pause догоняет playbackStateChanged → _update_play_icon.
        if self._player is not None and not self._primed:
            self._primed = True
            try:
                if self._audio is not None:
                    self._audio.setMuted(self._muted)
                self._player.setPosition(0)
                self._player.play()
            except Exception:
                pass
            self._update_play_icon()
            # Прогрев кэша кадров для живого скраба (фоном). До готовности — спиннер и
            # disabled-контролы (юзер не скрабит до готовности → нет заикания).
            self._start_scrub_preload()

    # ── кнопка play/pause панели → пауза/плей (иконка обновится по сигналу) ──
    def _toggle_play_pause(self):
        """Играет → пауза; на паузе/остановлено → play. Иконку меняет
        playbackStateChanged → _update_play_icon (вручную тут не трогаем)."""
        if self._player is None:
            return
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
            if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._player.pause()
            else:
                self._player.play()
        except Exception:
            pass

    def _update_play_icon(self):
        """Иконка кнопки = pause во время проигрывания, play в паузе/стопе."""
        if self._btn_play is None:
            return
        playing = False
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
            playing = (self._player is not None and self._player.playbackState()
                       == QMediaPlayer.PlaybackState.PlayingState)
        except Exception:
            playing = False
        ic = self._icon_pause if playing else self._icon_play
        if ic is not None:
            self._btn_play.setIcon(ic)

    def _on_duration(self, dur: int):
        """durationChanged → диапазон seek-bar (0..длительность в мс)."""
        if self._seek is not None:
            self._seek.setRange(0, max(0, int(dur)))

    def _on_position(self, pos: int):
        """positionChanged → двигать ползунок сам, но НЕ когда юзер его держит."""
        if self._seek is not None and not self._seek.isSliderDown():
            self._seek.setValue(int(pos))

    def _on_media_status(self, status):
        """Видео доиграло (EndOfMedia): QMediaPlayer уходит в Stopped, нативный слой macOS
        чернеет (последний кадр не держится). Отматываем на первый кадр — тем же проверенным
        приёмом, что прайминг в showEvent (pause()+setPosition(0) рендерит кадр 0). Playhead
        синхронно встанет в 0 через positionChanged. Play не ломаем: следующий ▶ играет с 0."""
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
        except Exception:
            return
        if self._player is None:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            try:
                self._player.pause()
                self._player.setPosition(0)
            except Exception:
                pass
            self._update_play_icon()

    def _on_seek_moved(self, val: int):
        """Таскание ползунка. ОСНОВНОЙ путь (кэш готов): рисуем кадр из RAM-кэша в overlay
        МГНОВЕННО (bisect-lookup + QPixmap.loadFromData ~1-2мс) — кадр следует за playhead
        без задержки, плеер НЕ дёргаем. ФОЛЛБЭК (кэш провален, self._scrub_cache=None):
        старый троттл-setPosition (декодер коалесцирует — кадр догоняет, но попап рабочий)."""
        if self._scrub_cache is not None:
            self._update_scrub_overlay(int(val))
            return
        # ── фоллбэк: pre-decode провалился → старый QMediaPlayer-скраб ──
        self._pending_seek = int(val)
        import time
        now = time.monotonic()
        if self._player is not None and (now - self._last_seek_apply) * 1000.0 >= _SEEK_THROTTLE_MS:
            self._last_seek_apply = now
            try:
                self._player.setPosition(int(val))
            except Exception:
                pass
        if self._seek_timer is not None and not self._seek_timer.isActive():
            self._seek_timer.start()

    def _on_seek_pressed(self):
        """Захват ползунка: запомнить, играло ли, поставить на паузу (аудио не частит при
        таскании). ОСНОВНОЙ путь: показать overlay с кадром текущей позиции (далее _moved
        обновляет из кэша). ФОЛЛБЭК: запустить троттл-таймер перемотки."""
        if self._player is None:
            return
        try:
            from PyQt6.QtMultimedia import QMediaPlayer
            self._was_playing = (self._player.playbackState()
                                 == QMediaPlayer.PlaybackState.PlayingState)
            if self._was_playing:
                self._player.pause()
        except Exception:
            self._was_playing = False
        if self._scrub_cache is not None:
            self._begin_scrub_overlay(int(self._seek.value()) if self._seek is not None else 0)
            return
        # ── фоллбэк ──
        self._pending_seek = None
        self._last_seek_apply = 0.0   # сброс → первое же движение применится сразу
        if self._seek_timer is not None and not self._seek_timer.isActive():
            self._seek_timer.start()

    def _flush_pending_seek(self):
        """Тик троттл-таймера (ТОЛЬКО фоллбэк): ДОБИВКА накопленной позиции, если последний
        mouseMove не успел её применить (движение замерло внутри окна троттла)."""
        if self._player is None or self._pending_seek is None:
            return
        try:
            self._player.setPosition(int(self._pending_seek))
            import time
            self._last_seek_apply = time.monotonic()
        except Exception:
            pass
        self._pending_seek = None

    def _on_seek_released(self):
        """Отпустили ползунок: синхронизировать плеер с playhead (ОДИН setPosition).
        ОСНОВНОЙ путь (overlay активен): НЕ прячем overlay сразу — ждём, пока vw декодирует
        кадр на финальной позиции (_begin_overlay_handoff), и только потом overlay→vw. Так
        видео-кадр и overlay-кадр совпадают в момент стыка — нет мерцания «кадр назад».
        ФОЛЛБЭК (overlay не использовался): сразу _end + play."""
        if self._seek_timer is not None:
            self._seek_timer.stop()
        if self._player is None:
            self._end_scrub_overlay()
            return
        final = int(self._seek.value()) if self._seek is not None else 0
        overlay_active = (self._scrub_cache is not None
                          and self._scrub_overlay is not None
                          and self._scrub_overlay.isVisible())
        try:
            self._player.setPosition(final)
        except Exception:
            pass
        if overlay_active:
            # overlay держим на финальном кадре; стык — после готовности видео-кадра.
            self._update_scrub_overlay(final)
            self._begin_overlay_handoff()
            return
        # ── фоллбэк (overlay не использовался: pre-decode провалился) ──
        self._end_scrub_overlay()
        if self._was_playing:
            try:
                self._player.play()
            except Exception:
                pass
        self._pending_seek = None
        self._was_playing = False

    def _begin_overlay_handoff(self):
        """Гладкий стык overlay→видео: ждём videoFrameChanged (vw декодировал кадр на новой
        позиции) ИЛИ страховочный таймаут ~120мс, затем _finish. Так vw показывается уже с
        правильным кадром — без вспышки предыдущего."""
        self._handoff_active = True
        sink = None
        try:
            if self._video_widget is not None:
                sink = self._video_widget.videoSink()
        except Exception:
            sink = None
        self._handoff_sink = sink
        if sink is not None:
            try:
                sink.videoFrameChanged.connect(self._on_handoff_frame)
            except Exception:
                self._handoff_sink = None
        if self._handoff_timer is None:
            self._handoff_timer = QTimer(self)
            self._handoff_timer.setSingleShot(True)
            self._handoff_timer.timeout.connect(self._finish_overlay_handoff)
        self._handoff_timer.start(120)

    def _on_handoff_frame(self, *args):
        # Первый же кадр после setPosition → видео готово, завершаем стык.
        self._finish_overlay_handoff()

    def _finish_overlay_handoff(self):
        """Завершить стык: отписаться от videoFrameChanged, overlay→vw (кадр уже на месте),
        продолжить play если играло. Идемпотентно (сигнал+таймаут не сделают дважды)."""
        if not self._handoff_active:
            return
        self._handoff_active = False
        if self._handoff_timer is not None:
            try:
                self._handoff_timer.stop()
            except Exception:
                pass
        if self._handoff_sink is not None:
            try:
                self._handoff_sink.videoFrameChanged.disconnect(self._on_handoff_frame)
            except Exception:
                pass
            self._handoff_sink = None
        self._end_scrub_overlay()
        if self._was_playing and self._player is not None:
            try:
                self._player.play()
            except Exception:
                pass
        self._was_playing = False
        self._pending_seek = None

    # ── живой скраб: кэш кадров (pre-decode в RAM) + overlay-превью ──────────
    def _start_scrub_preload(self):
        """Запустить фоновый пред-декод кадров (ScrubPreloadThread, parent=None — паттерн A).
        До готовности: спиннер + disabled-контролы. Провал импорта/cv2 → грейсфул:
        контролы включаем, скраб идёт по старому QMediaPlayer-пути."""
        if self._preload is not None:
            return
        try:
            from generator.scrub_decoder import ScrubPreloadThread
        except Exception:
            # opencv/модуль недоступен → без кэша, фоллбэк-скраб (контролы активны).
            self._scrub_cache = None
            return
        self._scrub_warming = True
        self._set_controls_enabled(False)
        self._sync_scrub_geometry()
        if self._spinner is not None:
            self._spinner.start()
        th = ScrubPreloadThread(self._result_path, jpeg_q=90,
                                mem_cap_mb=128, parent=None)
        self._preload = th
        th.ready.connect(self._on_preload_ready)
        th.failed.connect(self._on_preload_failed)
        th.start()

    def _on_preload_ready(self, cache):
        """Кэш готов: сохранить (+ параллельный список ts для bisect), убрать спиннер,
        включить контролы. Скраб теперь мгновенный из RAM."""
        try:
            self._scrub_cache = list(cache) if cache else None
            self._scrub_ts = [int(ts) for ts, _b in (self._scrub_cache or [])]
        except Exception:
            self._scrub_cache = None
            self._scrub_ts = []
        self._scrub_warming = False
        if self._spinner is not None:
            self._spinner.stop()
        self._set_controls_enabled(True)

    def _on_preload_failed(self):
        """Pre-decode провалился (cv2 не открыл файл / 0 кадров): убрать спиннер, ВКЛЮЧИТЬ
        контролы — скраб пойдёт по старому QMediaPlayer-пути (попап рабочий)."""
        self._scrub_cache = None
        self._scrub_ts = []
        self._scrub_warming = False
        if self._spinner is not None:
            self._spinner.stop()
        self._set_controls_enabled(True)
        try:
            import sys
            sys.stderr.write("[scrub] pre-decode failed → fallback to QMediaPlayer scrub\n")
        except Exception:
            pass

    def _set_controls_enabled(self, on: bool):
        """Блокировка/разблокировка ползунка и play на время прогрева кэша."""
        if self._seek is not None:
            self._seek.setEnabled(bool(on))
        if self._btn_play is not None:
            self._btn_play.setEnabled(bool(on))

    def _frame_at(self, pos_ms: int) -> QPixmap:
        """Кадр из RAM-кэша по позиции: bisect ближайший слева → QPixmap.loadFromData.
        Пустой QPixmap если кэша нет."""
        if not self._scrub_cache or not self._scrub_ts:
            return QPixmap()
        idx = bisect_right(self._scrub_ts, int(pos_ms)) - 1
        if idx < 0:
            idx = 0
        elif idx >= len(self._scrub_cache):
            idx = len(self._scrub_cache) - 1
        pm = QPixmap()
        try:
            pm.loadFromData(self._scrub_cache[idx][1], "JPEG")
        except Exception:
            return QPixmap()
        return pm

    def _begin_scrub_overlay(self, pos_ms: int):
        """Показать overlay-превью на старте drag. Режим "hide" → спрятать нативный vw на
        время drag (его слой иначе перекрывает overlay); "child" → overlay поверх vw."""
        if self._scrub_overlay is None:
            return
        self._sync_scrub_geometry()
        if _SCRUB_OVERLAY_MODE == "hide" and self._video_widget is not None:
            self._video_widget.hide()
        self._update_scrub_overlay(pos_ms)
        self._scrub_overlay.show()
        self._scrub_overlay.raise_()

    def _update_scrub_overlay(self, pos_ms: int):
        """Обновить кадр overlay из кэша (мгновенно). Кадр полноразмерный → вписываем в размер
        overlay с Qt.SmoothTransformation (сглаженно, без блюра). Без setPixmap если кэша нет."""
        if self._scrub_overlay is None:
            return
        pm = self._frame_at(pos_ms)
        if pm.isNull():
            return
        sz = self._scrub_overlay.size()
        if sz.width() > 0 and sz.height() > 0:
            pm = pm.scaled(sz, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._scrub_overlay.setPixmap(pm)

    def _end_scrub_overlay(self):
        """Завершить overlay. Порядок для гладкого стыка: СНАЧАЛА вернуть vw (он уже под
        overlay'ем, видео-кадр на финальной позиции уже декодирован — см. handoff), ПОТОМ
        скрыть overlay → нет вспышки неотрисованного/предыдущего кадра."""
        if _SCRUB_OVERLAY_MODE == "hide" and self._video_widget is not None:
            self._video_widget.show()
        if self._scrub_overlay is not None:
            self._scrub_overlay.hide()

    def _sync_scrub_geometry(self):
        """Подогнать overlay под прямоугольник кадра vw и центрировать спиннер. Зовётся из
        _VideoFrame._fit (ресайз окна) и перед показом overlay."""
        vw = getattr(self, "_video_widget", None)
        vf = getattr(self, "_video_frame", None)
        if self._scrub_overlay is not None and vw is not None:
            if _SCRUB_OVERLAY_MODE == "child":
                # overlay — дочерний vw → заполняет его целиком (0,0,w,h).
                self._scrub_overlay.setGeometry(0, 0, vw.width(), vw.height())
            else:
                # overlay — сиблинг в _video_frame → совпадает с geometry vw в нём.
                self._scrub_overlay.setGeometry(vw.geometry())
        if self._spinner is not None and vf is not None:
            self._spinner.move((vf.width() - self._spinner.width()) // 2,
                               (vf.height() - self._spinner.height()) // 2)

    # ── action-кнопки ряда ─────────────────────────────────────────────
    def _on_return_clicked(self):
        """Стрелка возврата → «повторить генерацию»: промпт + ВСЕ рефы + настройки (модель,
        формат, длительность, режим) этой карточки в поле генератора, попап закрыть.
        Делегирует page.restore_from_meta (контролы генератора живут там; родитель = page)."""
        page = self.parent()
        if page is not None and hasattr(page, "restore_from_meta"):
            try:
                page.restore_from_meta(self._meta)
            except Exception:
                pass
        self.close()

    def _on_grab_frame(self):
        """Клик по плюсику у playhead → захватить кадр видео в ПОЛНОМ разрешении
        (кадр под playhead) → сохранить JPEG в папку холста (рядом с видео) →
        (1) добавить рефом к следующей генерации (page.add_ref) И (2) положить новой
        карточкой на холст тем же путём, что drag-drop из папки (page._import_dropped_files)
        → закрыть попап. Кадр — обычная картинка-реф: VEO 3.1 / OmniFlash умеют
        image-to-video, дальше юзер сам пишет промпт."""
        vw = getattr(self, "_video_widget", None)
        if vw is None:
            return
        # Позиция playhead (где остановил юзер) — приоритет у трека; фоллбэк на плеер.
        if self._seek is not None:
            pos = int(self._seek.value())
        else:
            pos = int(self._player.position()) if self._player is not None else 0
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(self._result_path).parent / f"frame_{stamp}_{pos}ms.jpg"
        # ПОЛНОЕ КАЧЕСТВО (требование): кадр в ПОЛНОМ разрешении видео, НЕ из уменьшенного
        # скраб-кэша (480px). Первично — детерминированный cv2-seek по playhead (полный кадр,
        # imencode→write_bytes, не-ASCII safe). Фоллбэк — videoSink().videoFrame() (Qt,
        # полное разрешение, но зависит от async-тайминга позиции плеера).
        saved = False
        try:
            from generator.scrub_decoder import grab_full_frame_jpeg
            jpg = grab_full_frame_jpeg(self._result_path, float(pos), jpeg_q=92)
            if jpg:
                out.write_bytes(jpg)
                saved = True
        except Exception:
            saved = False
        if not saved:
            # ── фоллбэк: текущий кадр из QVideoSink → QImage → JPEG ──
            try:
                frame = vw.videoSink().videoFrame()
                img = frame.toImage() if frame is not None else None
            except Exception:
                img = None
            if img is None or img.isNull():
                return
            try:
                from PyQt6.QtGui import QImage as _QImage
                if img.format() != _QImage.Format.Format_RGB32:
                    img = img.convertToFormat(_QImage.Format.Format_RGB32)
                if not img.save(str(out), "JPEG", 92):
                    return
            except Exception:
                return
        # КЛЮЧЕВОЕ от ▶-бага: добавляем реф ТОЛЬКО если сохранённый файл реально читается
        # QPixmap (тем же загрузчиком, что превью рефа). Так путь в add_ref ГАРАНТИРОВАННО
        # указывает на существующий читаемый кадр — нечитаемый/несуществующий не добавляем
        # (иначе превью рефа рисует ▶-видео-заглушку на null-картинку).
        if QPixmap(str(out)).isNull():
            return
        # Кадр валиден (файл на диске в generator/ читается). page = generator_page
        # (родитель попапа). Кладём кадр в ДВА места, независимыми guard'ами:
        #   (1) в поле рефов (как раньше) — реф к следующей генерации;
        #   (2) ДОПОЛНИТЕЛЬНО новой карточкой на холст — тем же путём, что drag-drop
        #       из папки (_import_dropped_files: копия gen_<ts>.jpg + карточка +
        #       canvas.json). Порядок: сначала реф, потом холст, потом close.
        # Отсутствие метода / ошибка одного вызова не роняет попап и не мешает второму.
        page = self.parent()
        if page is not None and hasattr(page, "add_ref"):
            try:
                page.add_ref(str(out))
            except Exception:
                pass
        if page is not None and hasattr(page, "_import_dropped_files"):
            try:
                page._import_dropped_files([out])   # кадр → новой карточкой на холст
            except Exception:
                pass
        self.close()

    def _on_reveal_clicked(self):
        """Показать файл результата в Finder/Explorer (reveal-and-select). Делегирует
        кросс-платформенному storyboard_app.reveal_in_file_manager (ленивый импорт, как
        в result_cell). Любая ошибка — тихий выход (UI-удобство)."""
        if not self._result_path:
            return
        try:
            from storyboard_app import reveal_in_file_manager
            reveal_in_file_manager(self._result_path)
        except Exception:
            pass

    def _on_toggle_mute(self):
        """Локальный mute плеера попапа (НЕ трогает глобальный mute генератора)."""
        self._muted = not self._muted
        if self._audio is not None:
            try:
                self._audio.setMuted(self._muted)
            except Exception:
                pass
        self._update_mute_icon()

    def _update_mute_icon(self):
        """Иконка кнопки звука: volume-x при mute, volume-2 при звуке."""
        if self._btn_mute is None:
            return
        ic = self._icon_volx if self._muted else self._icon_vol
        if ic is not None:
            self._btn_mute.setIcon(ic)

    def _teardown_scrub(self):
        """Остановить пред-декод-поток (stop+wait → cap.release в run-finally), спиннер,
        overlay и ОЧИСТИТЬ кэш кадров из RAM. Идемпотентно — зовётся из всех путей закрытия
        (closeEvent/reject). Паттерн A (поток parent=None): даже пропусти мы путь — нет
        SIGABRT, но явный wait освобождает cv2-cap сразу и не оставляет кэш в памяти."""
        # Отменить незавершённый handoff (release во время ожидания стыка) — иначе
        # singleShot/videoFrameChanged выстрелит на уже закрытом диалоге.
        self._handoff_active = False
        if self._handoff_timer is not None:
            try:
                self._handoff_timer.stop()
            except Exception:
                pass
        if self._handoff_sink is not None:
            try:
                self._handoff_sink.videoFrameChanged.disconnect(self._on_handoff_frame)
            except Exception:
                pass
            self._handoff_sink = None
        th = self._preload
        if th is not None:
            try:
                th.stop()
                th.wait(1500)
            except Exception:
                pass
            self._preload = None
        if self._spinner is not None:
            try:
                self._spinner.stop()
            except Exception:
                pass
        if self._scrub_overlay is not None:
            try:
                self._scrub_overlay.hide()
            except Exception:
                pass
        # Очистка RAM: список JPEG-байтов и ts уходят из памяти.
        self._scrub_cache = None
        self._scrub_ts = []
        self._scrub_warming = False

    def _stop_playback(self):
        """Общая очистка при ЛЮБОМ закрытии (крестик=closeEvent И Escape=reject): teardown
        скраб-потока/cap/кэша + троттл-таймер + СТОП плеера/звука. Иначе после Escape
        (reject→hide, БЕЗ closeEvent) звук/видео продолжали играть."""
        self._teardown_scrub()
        if self._seek_timer is not None:
            try:
                self._seek_timer.stop()
            except Exception:
                pass
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    def reject(self):
        # Escape у non-modal QDialog идёт через reject()→hide() (НЕ через closeEvent!) →
        # та же очистка что при закрытии крестиком (иначе звук висит после Escape).
        self._stop_playback()
        super().reject()

    def closeEvent(self, ev):
        # Крестик: та же очистка (звук не висит, cv2-cap освобождён, поток остановлен).
        self._stop_playback()
        super().closeEvent(ev)
