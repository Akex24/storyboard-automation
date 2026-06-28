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

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, QSize, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QPixmap, QIcon, QPainter, QPen, QFont, QPolygonF, QPalette
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QToolButton, QWidget)

from views.theme import LUMZ_THEME, theme_qcolor

# Частота применения перемотки при таскании seek-bar (троттл): setPosition НЕ шлём на
# каждый sliderMoved — декодер захлёбывается и коалесцирует быстрые seek'и (кадр застывает
# до остановки мыши). Вместо этого копим последнюю позицию и применяем её по таймеру с
# этим интервалом, чтобы промежуточные кадры рисовались по ходу таскания.
_SEEK_THROTTLE_MS = 50


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
        self._plus_btn.setToolTip("Взять этот кадр в референс")
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
    def mousePressEvent(self, ev):
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
        if self._down:
            self._pos = self._x_to_ms(ev.position().x())
            self._reposition_plus()
            self.update()
            self.sliderMoved.emit(self._pos)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._down:
            self._down = False
            self._pos = self._x_to_ms(ev.position().x())
            self.update()
            self.sliderReleased.emit()
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)

    # --- плюсик у playhead: позиционирование за полоской + сигнал «взять кадр» ---
    def _reposition_plus(self):
        """Поставить плюсик строго по X линии playhead + _PLUS_DX, БЕЗ зажима у краёв —
        он едет за линией всю дорожку и спокойно уезжает ЗА правую границу трека вместе с
        ней (не упирается раньше, не расходится). Плюсик — дочерний контейнера дорожки
        (нижней панели), а НЕ трека: иначе клиппинг по краю трека обрезал бы его в конце.
        Зовётся при смене позиции/диапазона/таскании/resize/move."""
        btn = getattr(self, "_plus_btn", None)
        if btn is None:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        if btn.parent() is not parent:
            btn.setParent(parent)
            btn.show()
        cx = round(self._ms_to_x(self._pos))
        # координаты панели = позиция трека внутри неё + X внутри трека
        btn.move(self.x() + cx + self._PLUS_DX, self.y() + 2)
        btn.raise_()

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

    def __init__(self, video_widget, ratio_w, ratio_h, parent=None):
        super().__init__(parent)
        self._vw = video_widget
        self._vw.setParent(self)
        self._rw = max(1, int(ratio_w))
        self._rh = max(1, int(ratio_h))
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
        self._seek_timer = None       # троттл-таймер перемотки при таскании
        self._pending_seek = None     # последняя позиция, ждущая применения троттлом
        self._is_video = (self._meta.get("type") == "video")
        aspect = self._meta.get("aspect", "16:9")

        self.setWindowTitle("Просмотр")
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

    # ── картинка: StoryboardView (зум колесом + панорама) ──────────────
    def _build_image(self, lay: QVBoxLayout):
        pix = QPixmap(self._result_path)
        try:
            # Ленивый импорт (frozen/circular guard — как в shot_viewer_dialog).
            from widgets.face_grid.grid_dialog import StoryboardView
            self._view = StoryboardView(pix)
            lay.addWidget(self._view)
        except Exception:
            # Фолбэк: StoryboardView недоступен → статичная картинка без зума.
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("background:transparent;")
            if not pix.isNull():
                lbl.setPixmap(pix)
            lbl.setScaledContents(False)
            lay.addWidget(lbl)

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
                lbl.setText("Видео-плеер недоступен")
                lbl.setStyleSheet("color:rgba(255,255,255,0.55); font-size:13px;")
            lay.addWidget(lbl)
            return
        # WA_TransparentForMouseEvents НЕ ставим: он влияет только на доставку mouse-событий,
        # не на рендер кадров. Управление на панели, а не по клику в видео.
        vw = QVideoWidget()
        self._video_widget = vw
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
        self._video_frame = _VideoFrame(vw, rw, rh, self)
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
                                            "Вернуть в генератор", inner)
        self._btn_folder = self._mk_row_btn("folder-open", 28, 18, self._on_reveal_clicked,
                                            "Показать в Finder", inner)
        self._btn_mute = self._mk_row_btn("volume-2", 28, 18, self._on_toggle_mute,
                                          "Звук", inner)
        left = QHBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(LG_GAP)
        for b in (self._btn_return, self._btn_folder, self._btn_mute):
            left.addWidget(b)
        lg_w = 3 * 28 + 2 * LG_GAP   # ширина левой группы (3 кнопки) → симметричный spacer справа

        # play/pause — крупная (40×40), главная, ровно по центру контейнера
        self._btn_play = self._mk_row_btn("play", 40, 24, self._toggle_play_pause, "Плей/пауза", inner)
        self._btn_play.setObjectName("viewer-playpause")

        ih.addLayout(left)
        ih.addStretch(1)
        ih.addWidget(self._btn_play)
        ih.addStretch(1)
        ih.addSpacing(lg_w)          # зеркало левой группы → play/pause строго по центру

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
        # БЕЗ autoplay и БЕЗ видимой отмотки: pause() на свежезагруженном плеере декодирует и
        # показывает ПЕРВЫЙ кадр (pos 0) без проигрывания вперёд (проверено: pause() сам даёт
        # кадр 0; прежний play→pause-пинок отматывался видимо — убран). setPosition(0) фиксирует.
        if self._player is not None and not self._primed:
            self._primed = True
            try:
                if self._audio is not None:
                    self._audio.setMuted(self._muted)
                self._player.pause()
                self._player.setPosition(0)
            except Exception:
                pass
            self._update_play_icon()

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

    def _on_seek_moved(self, val: int):
        """Таскание ползунка → копим последнюю позицию; применяет её троттл-таймер
        (_flush_pending_seek) раз в _SEEK_THROTTLE_MS — декодер успевает рисовать
        промежуточные кадры по ходу, а не только на остановке мыши."""
        self._pending_seek = int(val)
        if self._seek_timer is not None and not self._seek_timer.isActive():
            self._seek_timer.start()

    def _on_seek_pressed(self):
        """Захват ползунка: запомнить, играло ли, и поставить на паузу — чтобы скраб был
        отзывчивым (на этой сборке Qt setPosition перерисовывает кадр и на паузе), а аудио
        не частило при таскании. Запускаем троттл-таймер перемотки."""
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
        self._pending_seek = None
        if self._seek_timer is not None and not self._seek_timer.isActive():
            self._seek_timer.start()

    def _flush_pending_seek(self):
        """Тик троттл-таймера: применить накопленную позицию (если пришла новая)."""
        if self._player is None or self._pending_seek is None:
            return
        try:
            self._player.setPosition(int(self._pending_seek))
        except Exception:
            pass
        self._pending_seek = None

    def _on_seek_released(self):
        """Отпустили ползунок: стоп троттла + зафиксировать кадр финальной позиции; если
        до скраба играло — продолжить с этого места, иначе остаться на выбранном кадре."""
        if self._seek_timer is not None:
            self._seek_timer.stop()
        if self._player is None:
            return
        try:
            if self._seek is not None:
                self._player.setPosition(int(self._seek.value()))
            if self._was_playing:
                self._player.play()
        except Exception:
            pass
        finally:
            self._pending_seek = None
            self._was_playing = False

    # ── action-кнопки ряда ─────────────────────────────────────────────
    def _on_return_clicked(self):
        """Вернуть результат в генератор. Заглушка — логика в Кусках 3-4."""
        pass

    def _on_grab_frame(self):
        """Клик по плюсику у playhead → взять этот кадр в референс. ЗАГЛУШКА.
        Кусок 3: захват кадра playhead → реф в генератор."""
        pass

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

    def closeEvent(self, ev):
        # Остановить троттл-таймер и воспроизведение/звук при закрытии (звук не висит).
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
        super().closeEvent(ev)
