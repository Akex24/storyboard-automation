# -*- coding: utf-8 -*-
"""
generator/viewer_dialog.py — попап просмотра результата Генератора (Кусок 2/4).

Клик по готовой плитке (ShimmerCell, state image/video) → большое non-modal окно:
  • КАРТИНКА: зум колесом + панорама через StoryboardView (widgets/face_grid/grid_dialog
    — тот же зум-движок, что у попапа шота; не дублируем).
  • ВИДЕО: нативный QVideoWidget + QMediaPlayer + QAudioOutput. Play на HOVER окна
    (enterEvent → play, leaveEvent → pause). Глобальный mute берётся из parent._video_muted
    (фичер mute генератора). НЕ autoplay. closeEvent → stop (звук не висит).

Промпт / чипы рефов / кнопки — Куски 3-4, здесь НЕТ.

Фон окна — из темы (LUMZ_THEME['bg_main']), не хардкод. Окно Qt.WindowType.Tool,
показывается через .show() (non-modal): грид/генерация продолжают работать.

Cross-platform: pathlib.Path, ленивые импорты QtMultimedia/QtMultimediaWidgets и
StoryboardView (frozen guard — если модуля нет, деградируем без падения).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel

from views.theme import LUMZ_THEME


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

    # ── видео: нативный QVideoWidget + плеер (play на hover) ────────────
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
        vw = QVideoWidget(self)
        # Мышь проходит сквозь видеовиджет → enter/leave диалога срабатывают по всей
        # площади окна (иначе дочерний виджет перехватывал бы hover-события).
        vw.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._video_widget = vw
        self._audio = QAudioOutput(self)
        # Глобальный mute генератора (если parent его несёт).
        self._audio.setMuted(bool(getattr(self.parent(), "_video_muted", False)))
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(vw)
        self._player.setSource(QUrl.fromLocalFile(self._result_path))
        lay.addWidget(vw)

    def _target_size(self, aspect: str):
        """Крупный размер окна под формат, вписанный в доступную область экрана."""
        max_w, max_h = 1280, 800
        try:
            from PyQt6.QtWidgets import QApplication
            avail = QApplication.primaryScreen().availableGeometry()
            max_w = int(avail.width() * 0.8)
            max_h = int(avail.height() * 0.85)
        except Exception:
            pass
        rw, rh = (9, 16) if aspect == "9:16" else (16, 9)
        w = max_w
        h = int(w * rh / rw)
        if h > max_h:
            h = max_h
            w = int(h * rw / rh)
        return max(480, w), max(360, h)

    # ── hover-плей видео (зум видео не нужен — просто большой плеер) ────
    def enterEvent(self, ev):
        super().enterEvent(ev)
        if self._player is not None:
            try:
                # Применить актуальный глобальный mute перед стартом.
                if self._audio is not None:
                    self._audio.setMuted(bool(getattr(self.parent(), "_video_muted", False)))
                self._player.play()
            except Exception:
                pass

    def leaveEvent(self, ev):
        super().leaveEvent(ev)
        if self._player is not None:
            try:
                self._player.pause()
            except Exception:
                pass

    def closeEvent(self, ev):
        # Остановить воспроизведение/звук при закрытии (звук не висит).
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        super().closeEvent(ev)
