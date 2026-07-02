# -*- coding: utf-8 -*-
"""
camera_lab — вкладка «Камера»: смена ракурса кадра через fal.ai.

2026-07-02: переведена с FastGen-промптов на fal-ai/qwen-image-edit-2511-
multiple-angles (generator/fal_angles_thread.py). Числовые углы уходят в
API как есть (квантование в пресеты LoRA — на сервере). Убраны референсы,
промт-превью и выбор моделей; добавлены ключ fal + живой баланс и
орбитальная миникарта вместо перекоса картинки. Не трогает editor/shot
viewer/сториборд-пайплайн.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from i18n import tr
from generator.fal_angles_thread import (
    FAL_MODEL, FalAnglesThread, FalBalanceThread,
)


# 2026-07-02 (fal): остался только основной кадр — референсы вырезаны.
REF_TYPES = ("Current shot",)
REF_TYPE_LABEL_KEYS = {
    "Current shot": "camera_ref_current",
}


@dataclass
class CameraReference:
    path: Path
    ref_type: str


@dataclass
class CameraGenerationJob:
    thread: FalAnglesThread
    horizontal: float
    vertical: float
    zoom: float
    elapsed: int = 0
    elapsed_started: bool = False
    status: str = ""


class ResultPreviewLabel(QLabel):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)



class CameraResultDialog(QDialog):
    def __init__(self, image_path: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("")
        self.setModal(False)
        self.setObjectName("camera-result-dialog")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.image)
        pixmap = QPixmap(str(image_path))
        screen = self.screen() or (parent.screen() if parent else None)
        if screen is not None:
            rect = screen.availableGeometry()
            self.resize(int(rect.width() * 0.60), int(rect.height() * 0.60))
        else:
            self.resize(980, 620)
        if not pixmap.isNull():
            self.image.setPixmap(
                pixmap.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.setStyleSheet("""
            QDialog#camera-result-dialog { background: #101111; }
            QLabel { background: #101111; }
        """)


class CameraPerspectiveControl(QWidget):
    """Орбитальная миникарта камеры (2026-07-02, замена перекоса кадра).

    Миниатюра кадра — РОВНАЯ в центре (без искажений). Значок камеры ходит
    по эллиптической орбите вокруг неё:
      • горизонталь (0..360°) — позиция по кругу (0° = перед кадром, низ);
      • вертикаль (-30..90°) — «высота» точки зрения: эллипс раскрывается
        от плоского (взгляд в упор) к почти кругу (вид сверху);
      • зум (0..100 = 0.0..10.0) — дистанция камеры от кадра (радиус).
    Значения = РЕАЛЬНЫЕ API-значения (h в градусах, v в градусах,
    зум ×10 int) — без защёлкивания в пресеты. Drag мышью крутит h/v,
    колесо — зум (тот же контракт valuesChanged, что и раньше).
    Чистый QPainter — кроссплатформенно (Mac/Win)."""

    valuesChanged = pyqtSignal(int, int, int)   # h_deg, v_deg, zoom_x10

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._h = 0        # 0..360
        self._v = 0        # -30..90
        self._z = 50       # 0..100 (= zoom 5.0)
        self._frame_pixmap: Optional[QPixmap] = None
        self._drag_start: Optional[QPointF] = None
        self._drag_h = 0
        self._drag_v = 0
        self.setMinimumSize(260, 300)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # контракт под CameraLabView (имена прежние)
    def set_values(self, h_deg: int, v_deg: int, zoom_x10: int) -> None:
        self._h = int(h_deg) % 360
        self._v = max(-30, min(90, int(v_deg)))
        self._z = max(0, min(100, int(zoom_x10)))
        self.update()

    def set_frame_aspect(self, aspect: float) -> None:
        # ровной миниатюре аспект задаёт сам pixmap; метод оставлен для
        # совместимости вызовов (no-op).
        self.update()

    def set_frame_image(self, path: Optional[Path]) -> None:
        if path is None:
            self._frame_pixmap = None
            self.update()
            return
        pixmap = QPixmap(str(path))
        self._frame_pixmap = pixmap if not pixmap.isNull() else None
        self.update()

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._apply_cursor(event.position())   # камера сразу под курсор
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if self._drag_start is None:
            super().mouseMoveEvent(event)
            return
        self._apply_cursor(event.position())       # 1:1 за точкой курсора
        event.accept()

    def _apply_cursor(self, pos) -> None:
        """2026-07-02 (фикс drag 1:1): экранные координаты курсора →
        (долгота, широта) ИНВЕРСИЕЙ той же проекции — камера следует ТОЧНО
        за курсором, без дельты с множителем. Курсор вне диска сферы →
        нормируем на край. Двузначность перед/зад решается непрерывностью:
        из двух долгот (передней и зеркальной задней) берём ближайшую к
        текущей — так камера «прокручивается» через край на заднюю сторону."""
        cx, cy, radius = self._sphere_geometry()
        if radius <= 0:
            return
        sx = pos.x() - cx
        sy = pos.y() - cy
        rr = math.hypot(sx, sy)
        if rr > radius * 0.999:
            sx *= radius * 0.999 / rr
            sy *= radius * 0.999 / rr
        y2 = -sy
        z2 = math.sqrt(max(0.0, radius * radius - sx * sx - y2 * y2))
        ct, st = math.cos(self._AXIS_TILT), math.sin(self._AXIS_TILT)
        # обратный наклон (транспонированная матрица поворота вокруг X)
        y = y2 * ct + z2 * st
        z = -y2 * st + z2 * ct
        vis_lat = math.degrees(math.asin(max(-1.0, min(1.0, y / radius))))
        lon_front = math.degrees(math.atan2(sx, z)) % 360
        lon_back = (180.0 - math.degrees(math.atan2(sx, z))) % 360
        def _dist(a: float, b: float) -> float:
            d = abs(a - b) % 360
            return min(d, 360 - d)
        lon = lon_front if _dist(lon_front, self._h) <= _dist(lon_back, self._h) \
            else lon_back
        self._h = int(round(lon)) % 360
        self._v = self._vis_to_v(vis_lat)
        self.update()
        self.valuesChanged.emit(self._h, self._v, self._z)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._drag_start = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        step = 5 if event.angleDelta().y() > 0 else -5   # 0.5 зума за щелчок
        self._z = max(0, min(100, self._z + step))
        self.update()
        self.valuesChanged.emit(self._h, self._v, self._z)
        event.accept()

    # ── 3D-проекция сферы (2026-07-02, референс Higgsfield Angles) ──
    # Ортографическая проекция с фиксированным наклоном оси (объём глобуса):
    # точка сферы (lat, lon) → 3D (y вверх, z к зрителю) → наклон вокруг X
    # на _AXIS_TILT → экран (x, -y). z после наклона = depth cue (перед/зад).
    _AXIS_TILT = math.radians(-18.0)

    def _sphere_geometry(self):
        """(cx, cy, radius) сферы — единая точка правды для paint и drag."""
        rect = QRectF(self.rect()).adjusted(12, 10, -12, -10)
        return (rect.center().x(), rect.center().y() + 6,
                min(rect.width(), rect.height()) * 0.40)

    # 2026-07-02 (фикс №2): ВИЗУАЛЬНАЯ широта камеры — растянутый маппинг
    # на полный видимый диапазон сферы (якоря: −30°=дно, ПОД кадром;
    # 0°=экватор; +90°=полюс, НАД кадром). В API уходит реальный v.
    @staticmethod
    def _v_to_vis(v_deg: float) -> float:
        return v_deg * 3.0 if v_deg < 0 else v_deg

    @staticmethod
    def _vis_to_v(vis_deg: float) -> int:
        v = vis_deg / 3.0 if vis_deg < 0 else vis_deg
        return max(-30, min(90, int(round(v))))

    @classmethod
    def _project(cls, lat_deg: float, lon_deg: float, radius: float):
        """(lat°, lon°) → (dx, dy, depth). depth>0 — передняя полусфера."""
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        x = radius * math.cos(lat) * math.sin(lon)
        y = radius * math.sin(lat)
        z = radius * math.cos(lat) * math.cos(lon)
        ct, st = math.cos(cls._AXIS_TILT), math.sin(cls._AXIS_TILT)
        y2 = y * ct - z * st
        z2 = y * st + z * ct
        return x, -y2, z2

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(12, 10, -12, -10)
        painter.setPen(QPen(QColor("#1d1e20"), 1))
        painter.setBrush(QColor("#090a0a"))
        painter.drawRoundedRect(rect, 8, 8)

        cx, cy, radius = self._sphere_geometry()

        pen_front = QPen(QColor(255, 255, 255, 70), 1.0)
        pen_back = QPen(QColor(255, 255, 255, 22), 1.0)

        # 2026-07-02 (фикс №1): сегменты сетки копятся в ДВА слоя и рисуются
        # задние → миниатюра → ПЕРЕДНИЕ ПОВЕРХ — кадр читается ВНУТРИ глобуса.
        front_segs = []
        back_segs = []

        def _polyline(points):
            for (x1, y1, z1), (x2, y2, z2) in zip(points, points[1:]):
                seg = (QPointF(cx + x1, cy + y1), QPointF(cx + x2, cy + y2))
                (front_segs if (z1 + z2) * 0.5 >= 0 else back_segs).append(seg)

        # меридианы (12) и параллели (8)
        for lon in range(0, 360, 30):
            pts = [self._project(lat, lon, radius) for lat in range(-90, 91, 10)]
            _polyline(pts)
        for k in range(1, 9):
            lat = -90 + k * 20   # -70..70
            pts = [self._project(lat, lon, radius) for lon in range(0, 361, 10)]
            _polyline(pts)

        painter.setPen(pen_back)
        for a, b in back_segs:
            painter.drawLine(a, b)

        # камера: долгота = горизонталь; широта — растянутый ВИЗУАЛЬНЫЙ
        # маппинг (фикс №2: −30° уходит ПОД кадр, +90° — над). Признак
        # «за глобусом» — ЧИСТО по долготе 90..270 (фикс №4: знак Z после
        # наклона оси ошибочно топил камеру на передней стороне у полюса).
        cam_dx, cam_dy, _cam_z = self._project(
            self._v_to_vis(self._v), self._h, radius)
        cam_x, cam_y = cx + cam_dx, cy + cam_dy
        behind = 90 < (self._h % 360) < 270

        if behind:
            self._draw_camera(painter, cam_x, cam_y, cx, cy, dim=True,
                              zoom_x10=self._z)

        # ── миниатюра кадра: РОВНАЯ, небольшая, в центре сферы ──
        thumb_w = radius * 0.95
        thumb_h = thumb_w * 9 / 16
        if self._frame_pixmap is not None:
            pw, ph = self._frame_pixmap.width(), max(1, self._frame_pixmap.height())
            aspect = pw / ph
            if aspect < 1.0:
                thumb_h = thumb_w
                thumb_w = thumb_h * aspect
            else:
                thumb_h = thumb_w / aspect
        thumb_rect = QRectF(cx - thumb_w / 2, cy - thumb_h / 2, thumb_w, thumb_h)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
        if self._frame_pixmap is not None:
            scaled = self._frame_pixmap.scaled(
                QSize(int(thumb_rect.width()), int(thumb_rect.height())),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            clip = QPainterPath()
            clip.addRoundedRect(thumb_rect, 5, 5)
            painter.save()
            painter.setClipPath(clip)
            painter.drawPixmap(
                int(thumb_rect.center().x() - scaled.width() / 2),
                int(thumb_rect.center().y() - scaled.height() / 2),
                scaled,
            )
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(thumb_rect, 5, 5)
        else:
            painter.setBrush(QColor(26, 29, 31))
            painter.drawRoundedRect(thumb_rect, 5, 5)

        painter.setPen(pen_front)
        for a, b in front_segs:
            painter.drawLine(a, b)

        if not behind:
            self._draw_camera(painter, cam_x, cam_y, cx, cy, dim=False,
                              zoom_x10=self._z)

        painter.setPen(QColor(184, 184, 184))
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(12, 10, -12, -8),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            f"{tr('camera_horizontal')}: {self._h}°   "
            f"{tr('camera_vertical')}: {self._v:+d}°   "
            f"{tr('camera_zoom')}: {self._z / 10:.1f}",
        )

    @staticmethod
    def _draw_camera(painter: QPainter, x: float, y: float,
                     cx: float, cy: float, dim: bool, zoom_x10: int) -> None:
        """Значок камеры НА ПОВЕРХНОСТИ сферы + линия взгляда к центру.
        Сфера от зума НЕ меняется — зум кодируется ТОЛЩИНОЙ линии взгляда
        и бейджем «N.N» у значка. За сферой — полупрозрачный (depth cue).
        Lucide 'video' через get_icon; fallback — точка."""
        alpha = 90 if dim else 235
        line_w = 0.8 + (zoom_x10 / 100.0) * 2.6   # zoom 0→0.8px … 10→3.4px
        line_pen = QPen(QColor(232, 184, 106, 45 if dim else 150), line_w)
        line_pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(line_pen)
        painter.drawLine(QPointF(x, y), QPointF(cx, cy))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(19, 21, 22, alpha))
        painter.drawEllipse(QPointF(x, y), 13, 13)
        painter.setPen(QPen(QColor(232, 184, 106, alpha), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(x, y), 13, 13)
        icon_ok = False
        try:
            from storyboard_app import get_icon
            icon = get_icon('video')
            if icon is not None and not icon.isNull():
                painter.save()
                if dim:
                    painter.setOpacity(0.45)
                icon.paint(painter, QRect(int(x) - 8, int(y) - 8, 16, 16))
                painter.restore()
                icon_ok = True
        except Exception:
            pass
        if not icon_ok:
            painter.setBrush(QColor(232, 184, 106, alpha))
            painter.drawEllipse(QPointF(x, y), 4, 4)
        # бейдж зума у значка
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(232, 184, 106, alpha))
        painter.drawText(QRectF(x - 24, y + 13, 48, 14),
                         Qt.AlignmentFlag.AlignCenter, f"{zoom_x10 / 10:.1f}")




class ImageDropSlot(QFrame):
    filesDropped = pyqtSignal(str, list)
    imageClicked = pyqtSignal(Path)
    pasteRequested = pyqtSignal()
    clearRequested = pyqtSignal()   # 2026-07-02: крестик очистки исходника

    def __init__(
        self,
        slot_type: str,
        title_key: str,
        hint_key: str,
        large: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._slot_type = slot_type
        self._large = large
        self._title_key = title_key
        self._hint_key = hint_key
        self._image_name: Optional[str] = None
        self._image_path: Optional[Path] = None
        self._pixmap: Optional[QPixmap] = None
        self._aspect_ratio: float = 16 / 9
        self._paste_available = False
        self.setAcceptDrops(True)
        self.setObjectName("camera-main-slot" if large else "camera-ref-slot")
        if large:
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            self.setMinimumWidth(420)
        else:
            self.setFixedSize(128, 128)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self.image = QLabel()
        self.image.setObjectName("camera-slot-image")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(0 if large else 74)
        self.image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.title = QLabel()
        self.title.setObjectName("camera-slot-title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.hint = QLabel()
        self.hint.setObjectName("camera-slot-hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)

        lay.addWidget(self.image, stretch=1)
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lay.addStretch()
        if self._large:
            self.paste_btn = QToolButton(self)
            self.paste_btn.setObjectName("camera-slot-paste-btn")
            self.paste_btn.setIcon(_camera_icon("clipboard-paste"))
            self.paste_btn.setIconSize(QSize(16, 16))
            self.paste_btn.setFixedSize(28, 28)
            self.paste_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.paste_btn.setToolTip(tr("shot_paste"))
            self.paste_btn.clicked.connect(self.pasteRequested.emit)
            self.paste_btn.setVisible(False)
            # 2026-07-02: крестик очистки — по образцу paste_btn (overlay,
            # виден на hover и только когда картинка загружена).
            self.clear_btn = QToolButton(self)
            self.clear_btn.setObjectName("camera-slot-paste-btn")
            self.clear_btn.setIcon(_camera_icon("x"))
            self.clear_btn.setIconSize(QSize(16, 16))
            self.clear_btn.setFixedSize(28, 28)
            self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clear_btn.setToolTip(tr("camera_clear_source"))
            self.clear_btn.clicked.connect(self.clearRequested.emit)
            self.clear_btn.setVisible(False)
        else:
            self.paste_btn = None
            self.clear_btn = None
        self.retranslate()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return self._large

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        if not self._large:
            return super().heightForWidth(width)
        # Default is 16:9; after image load the slot follows the real image
        # aspect ratio. Clamp height so portrait refs do not crush the controls.
        return max(260, min(560, int(width / max(0.2, self._aspect_ratio))))

    def sizeHint(self):  # noqa: N802 - Qt override
        hint = super().sizeHint()
        if self._large:
            hint.setWidth(max(hint.width(), 640))
            hint.setHeight(self.heightForWidth(hint.width()))
        return hint

    def aspect_ratio(self) -> float:
        return self._aspect_ratio

    def set_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self._pixmap = pixmap
        self._image_path = path
        if self._large and pixmap.height() > 0:
            self._aspect_ratio = pixmap.width() / pixmap.height()
        self._image_name = path.name
        self.title.setVisible(not self._large)
        self.hint.setVisible(not self._large)
        self.title.setText("" if self._large else path.name)
        self.hint.setText("" if self._large else tr(REF_TYPE_LABEL_KEYS[self._slot_type]))
        self._refresh_pixmap()
        if self._large:
            self.updateGeometry()

    def set_paste_available(self, available: bool) -> None:
        self._paste_available = bool(available)
        if self.paste_btn is not None:
            self.paste_btn.setEnabled(self._paste_available)
            self.paste_btn.setVisible(self._paste_available and self.underMouse())

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._image_path is not None
            and self._image_path.exists()
        ):
            self.imageClicked.emit(self._image_path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def retranslate(self) -> None:
        if self._image_name:
            self.title.setVisible(not self._large)
            self.hint.setVisible(not self._large)
            self.title.setText("" if self._large else self._image_name)
            self.hint.setText("" if self._large else tr(REF_TYPE_LABEL_KEYS[self._slot_type]))
            return
        self.title.setVisible(True)
        self.hint.setVisible(True)
        self.title.setText(tr(self._title_key))
        self.hint.setText(tr(self._hint_key))

    def clear_image(self) -> None:
        """Сброс слота в исходное состояние «перетащи кадр»."""
        self._pixmap = None
        self._image_path = None
        self._image_name = None
        self._aspect_ratio = 16 / 9
        self.image.setPixmap(QPixmap())
        self.retranslate()
        if self._large:
            self.updateGeometry()
        self.update()

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self.paste_btn is not None:
            self.paste_btn.move(max(8, self.width() - self.paste_btn.width() - 12), 12)
        if getattr(self, "clear_btn", None) is not None:
            # левее кнопки вставки, тот же верхний отступ
            self.clear_btn.move(
                max(8, self.width() - self.paste_btn.width() * 2 - 12 - 8), 12)
        self._refresh_pixmap()

    def enterEvent(self, event):  # noqa: N802 - Qt override
        if self.paste_btn is not None:
            self.paste_btn.setVisible(self._paste_available)
        if getattr(self, "clear_btn", None) is not None:
            self.clear_btn.setVisible(self._image_path is not None)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        if self.paste_btn is not None:
            self.paste_btn.setVisible(False)
        if getattr(self, "clear_btn", None) is not None:
            self.clear_btn.setVisible(False)
        super().leaveEvent(event)

    def _refresh_pixmap(self) -> None:
        if self._pixmap is None:
            return
        target = self.image.size()
        if target.width() <= 4 or target.height() <= 4:
            return
        scaled = self._pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image.setPixmap(scaled)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
            if any(self._is_image_path(path) for path in paths):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt override
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if self._is_image_path(Path(u.toLocalFile()))
        ]
        if paths:
            self.filesDropped.emit(self._slot_type, paths)
            event.acceptProposedAction()
            return
        event.ignore()

    @staticmethod
    def _is_image_path(path: Path) -> bool:
        return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}



def _camera_icon(name: str) -> QIcon:
    try:
        from storyboard_app import get_icon

        return get_icon(name)
    except Exception:
        return QIcon()


class CameraResultThumb(QFrame):
    clicked = pyqtSignal(Path)
    revealRequested = pyqtSignal(Path)
    copyRequested = pyqtSignal(Path)
    deleteRequested = pyqtSignal(Path)

    def __init__(
        self,
        image_path: Path,
        parent: Optional[QWidget] = None,
        aspect_ratio: Optional[float] = None,
    ):
        super().__init__(parent)
        self._path = image_path
        self._pixmap = QPixmap(str(image_path))
        self.setObjectName("camera-result-thumb")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(str(image_path))
        wrap_size = self._thumb_size(self._pixmap, aspect_ratio)
        self.setFixedSize(wrap_size.width(), wrap_size.height())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.image_wrap = QFrame()
        self.image_wrap.setObjectName("camera-result-thumb-wrap")
        self.image_wrap.setFixedSize(wrap_size)
        image_lay = QGridLayout(self.image_wrap)
        image_lay.setContentsMargins(0, 0, 0, 0)
        image_lay.setSpacing(0)

        self.image = QLabel()
        self.image.setObjectName("camera-result-thumb-image")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(wrap_size)
        image_lay.addWidget(self.image, 0, 0)

        self.overlay = QWidget()
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.overlay.setVisible(False)
        overlay_lay = QHBoxLayout(self.overlay)
        overlay_lay.setContentsMargins(4, 4, 4, 4)
        overlay_lay.setSpacing(4)

        self.reveal_btn = QToolButton()
        self.reveal_btn.setObjectName("camera-thumb-overlay-btn")
        self.reveal_btn.setIcon(_camera_icon("folder-open"))
        self.reveal_btn.setIconSize(QSize(14, 14))
        self.reveal_btn.setFixedSize(22, 22)
        self.reveal_btn.setToolTip(tr("camera_result_reveal"))
        self.reveal_btn.clicked.connect(lambda: self.revealRequested.emit(self._path))

        self.copy_btn = QToolButton()
        self.copy_btn.setObjectName("camera-thumb-overlay-btn")
        self.copy_btn.setIcon(_camera_icon("copy"))
        self.copy_btn.setIconSize(QSize(14, 14))
        self.copy_btn.setFixedSize(22, 22)
        self.copy_btn.setToolTip(tr("shot_copy"))
        self.copy_btn.clicked.connect(lambda: self.copyRequested.emit(self._path))

        self.delete_btn = QToolButton()
        self.delete_btn.setObjectName("camera-thumb-overlay-trash")
        self.delete_btn.setIcon(_camera_icon("trash-2-red"))
        self.delete_btn.setIconSize(QSize(14, 14))
        self.delete_btn.setFixedSize(22, 22)
        self.delete_btn.setToolTip(tr("camera_result_delete"))
        self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self._path))

        overlay_lay.addWidget(self.reveal_btn)
        overlay_lay.addWidget(self.copy_btn)
        overlay_lay.addStretch()
        overlay_lay.addWidget(self.delete_btn)
        overlay_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        image_lay.addWidget(self.overlay, 0, 0)

        lay.addWidget(self.image_wrap)
        self._refresh_pixmap()

    def path(self) -> Path:
        return self._path

    @staticmethod
    def _thumb_size(pixmap: QPixmap, aspect_ratio: Optional[float] = None) -> QSize:
        if aspect_ratio and aspect_ratio > 0:
            aspect = aspect_ratio
        elif not pixmap.isNull() and pixmap.width() > 0 and pixmap.height() > 0:
            aspect = pixmap.width() / max(1, pixmap.height())
        else:
            aspect = 16 / 9
        max_w, max_h = 176, 150
        if aspect >= 1:
            width = max_w
            height = max(54, int(round(width / aspect)))
            if height > max_h:
                height = max_h
                width = int(round(height * aspect))
        else:
            height = max_h
            width = max(64, int(round(height * aspect)))
            if width > max_w:
                width = max_w
                height = int(round(width / aspect))
        return QSize(width, height)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):  # noqa: N802 - Qt override
        self.overlay.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        self.overlay.setVisible(False)
        super().leaveEvent(event)

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if self._pixmap.isNull():
            return
        target = self.image.size()
        if target.width() <= 0 or target.height() <= 0:
            target = self.image_wrap.size()
        scaled = self._pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        rounded = QPixmap(target)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(rounded.rect()), 8, 8)
        painter.setClipPath(clip)
        x = (target.width() - scaled.width()) // 2
        y = (target.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()
        self.image.setPixmap(rounded)



class CameraLabView(QWidget):
    """Вкладка «Камера»: ракурс кадра через fal (qwen multi-angle)."""

    def __init__(
        self,
        project_root: Path,
        parent: Optional[QWidget] = None,
        get_shot_clipboard: Optional[Callable[[], Optional[bytes]]] = None,
        set_shot_clipboard: Optional[Callable[[bytes], None]] = None,
    ):
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._get_shot_clipboard = get_shot_clipboard
        self._set_shot_clipboard = set_shot_clipboard
        self._current_ref: Optional[CameraReference] = None
        self._balance_thread: Optional[FalBalanceThread] = None
        self._loading_state = True
        self._slider_labels: List[QLabel] = []
        self._slider_value_labels: Dict[str, QLabel] = {}
        self._generation_jobs: Dict[int, CameraGenerationJob] = {}
        self._generation_run_id = 0
        self._last_generation_status = ""
        self._last_result_path: Optional[Path] = None
        self._generation_timer = QTimer(self)
        self._generation_timer.setInterval(1000)
        self._generation_timer.timeout.connect(self._tick_generation_timer)
        self._build_ui()
        self._load_state()
        self.set_shot_clipboard_available(bool(self._shot_clipboard_bytes()))

    def _build_ui(self) -> None:
        self.setObjectName("camera-lab")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(18)

        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("camera-title")
        self.subtitle_lbl = QLabel()
        self.subtitle_lbl.setObjectName("camera-subtitle")
        root.addWidget(self.title_lbl)
        root.addWidget(self.subtitle_lbl)

        body = QHBoxLayout()
        body.setSpacing(18)
        root.addLayout(body, stretch=1)

        left = QFrame()
        left.setObjectName("camera-panel")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(18, 18, 18, 18)
        left_lay.setSpacing(14)

        # 2026-07-02: заголовок исходника — блоки «Исходник/Результат»
        # больше не путаются визуально.
        self.source_title_lbl = QLabel()
        self.source_title_lbl.setObjectName("camera-section-title")
        left_lay.addWidget(self.source_title_lbl)

        self.current_slot = ImageDropSlot(
            "Current shot",
            "camera_drop_current",
            "camera_drop_current_hint",
            large=True,
        )
        self.current_slot.filesDropped.connect(self._add_references)
        self.current_slot.imageClicked.connect(self._open_image_popup)
        self.current_slot.pasteRequested.connect(self._paste_current_from_shot_clipboard)
        self.current_slot.clearRequested.connect(self._clear_current_source)
        left_lay.addWidget(self.current_slot)

        # 2026-07-02 (лейаут v2): под исходником — БОЛЬШОЕ окно результата
        # (последняя генерация, клик = попап-просмотрщик), под ним —
        # горизонтальная лента миниатюр всех результатов.
        self.result_title_lbl = QLabel()
        self.result_title_lbl.setObjectName("camera-section-title")
        left_lay.addWidget(self.result_title_lbl)

        # 2026-07-02 (лейаут v3): большое окно РОВНО размера исходника —
        # ширина общая (вся панель), высота жёстко синкается с
        # current_slot.height() в _update_big_result (у исходника высота
        # детерминирована heightForWidth). Лента ниже забирает остаток.
        self.big_result = ResultPreviewLabel()
        self.big_result.setObjectName("camera-result-big")
        self.big_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big_result.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Fixed)
        self.big_result.setCursor(Qt.CursorShape.PointingHandCursor)
        self.big_result.clicked.connect(lambda: self._open_result_viewer(None))
        left_lay.addWidget(self.big_result)

        self.results_scroll = QScrollArea()
        self.results_scroll.setObjectName("camera-results-panel")
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.results_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_scroll.setMinimumHeight(120)
        self.results_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Expanding)
        self.results_strip = QWidget()
        self.results_strip.setObjectName("camera-results-strip")
        self.results_strip_lay = QHBoxLayout(self.results_strip)
        self.results_strip_lay.setContentsMargins(0, 0, 0, 0)
        self.results_strip_lay.setSpacing(10)
        self.results_strip_lay.addStretch()
        self.results_scroll.setWidget(self.results_strip)
        self.results_scroll.setVisible(False)
        left_lay.addWidget(self.results_scroll, stretch=1)   # остаток панели

        body.addWidget(left, stretch=3)

        controls = QFrame()
        controls.setObjectName("camera-panel")
        controls.setMinimumWidth(340)
        controls_lay = QVBoxLayout(controls)
        controls_lay.setContentsMargins(18, 18, 18, 18)
        controls_lay.setSpacing(14)

        self.controls_title_lbl = QLabel()
        self.controls_title_lbl.setObjectName("camera-section-title")
        controls_lay.addWidget(self.controls_title_lbl)

        # 2026-07-02: диапазоны fal OpenAPI. Горизонталь — слайдер 0..72 с
        # множителем ×5 (честный шаг 5°); вертикаль −30..90 шаг 1°;
        # зум — слайдер 0..100 = 0.0..10.0 (шаг 0.1). В API уходят реальные
        # значения БЕЗ защёлкивания в пресеты (квантование — на сервере).
        self.rotate_slider = self._add_slider(controls_lay, "camera_horizontal", 0, 72, 0)
        self.vertical_slider = self._add_slider(controls_lay, "camera_vertical", -30, 90, 0)
        self.zoom_slider = self._add_slider(controls_lay, "camera_zoom", 0, 100, 50)

        self.orbit = CameraPerspectiveControl()
        self.orbit.valuesChanged.connect(self._on_preview_values_changed)
        controls_lay.addWidget(self.orbit)

        # ── Ключ fal + живой баланс (2026-07-02) ──
        self.fal_key_title_lbl = QLabel()
        self.fal_key_title_lbl.setObjectName("camera-section-title")
        controls_lay.addWidget(self.fal_key_title_lbl)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.fal_key_edit = QLineEdit()
        self.fal_key_edit.setObjectName("camera-fal-key")
        self.fal_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        # 2026-07-02 (фикс «ключ пропадает»): поле ПРЕДЗАПОЛНЯЕТСЯ
        # сохранённым ключом (точками) — раньше после перезапуска было
        # пустым, хотя ключ жил в QSettings и баланс тянулся.
        try:
            from storyboard_app import load_fal_key
            self.fal_key_edit.setText(load_fal_key())
        except Exception:
            pass
        # 2026-07-02: глазик (Lucide eye/eye-off) справа ВНУТРИ поля —
        # клик переключает показ ключа (echoMode Password ↔ Normal).
        self._fal_key_eye = self.fal_key_edit.addAction(
            _camera_icon("eye"), QLineEdit.ActionPosition.TrailingPosition)
        self._fal_key_eye.setToolTip(tr("camera_fal_key_show"))
        self._fal_key_eye.triggered.connect(self._toggle_fal_key_visible)
        key_row.addWidget(self.fal_key_edit, stretch=1)
        self.fal_key_btn = QPushButton()
        self.fal_key_btn.setObjectName("camera-fal-key-btn")
        self.fal_key_btn.setFixedHeight(32)
        self.fal_key_btn.clicked.connect(self._on_fal_key_confirm)
        key_row.addWidget(self.fal_key_btn)
        controls_lay.addLayout(key_row)

        self.fal_balance_lbl = QLabel()
        self.fal_balance_lbl.setObjectName("camera-fal-balance")
        self.fal_balance_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.fal_balance_lbl.setOpenExternalLinks(True)
        controls_lay.addWidget(self.fal_balance_lbl)

        self.generate_btn = QPushButton()
        self.generate_btn.setObjectName("camera-generate-btn")
        self.generate_btn.setFixedHeight(42)
        self.generate_btn.clicked.connect(self._run_generation)
        controls_lay.addWidget(self.generate_btn)

        self.generation_status_lbl = QLabel()
        self.generation_status_lbl.setObjectName("camera-generation-status")
        self.generation_status_lbl.setWordWrap(True)
        self.generation_status_lbl.setVisible(False)
        controls_lay.addWidget(self.generation_status_lbl)
        controls_lay.addStretch()
        body.addWidget(controls, stretch=2)

        self.setStyleSheet(self._qss())
        self.apply_lang()
        self._sync_control_state()

    def _add_slider(
        self,
        parent_layout: QVBoxLayout,
        label_key: str,
        minimum: int,
        maximum: int,
        value: int,
    ) -> QSlider:
        row = QWidget()
        lay = QGridLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setHorizontalSpacing(10)
        lay.setVerticalSpacing(6)

        name = QLabel()
        name.setObjectName("camera-slider-label")
        name.setProperty("_i18n_key", label_key)
        name.setText(tr(label_key))
        self._slider_labels.append(name)
        value_label = QLabel()
        value_label.setObjectName("camera-slider-value")
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._slider_value_labels[label_key] = value_label

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.valueChanged.connect(lambda _v: self._sync_control_state())
        slider.valueChanged.connect(
            lambda v, lab=value_label, key=label_key: self._update_value_label(lab, key, v)
        )
        self._update_value_label(value_label, label_key, value)

        lay.addWidget(name, 0, 0)
        lay.addWidget(value_label, 0, 1)
        lay.addWidget(slider, 1, 0, 1, 2)
        parent_layout.addWidget(row)
        return slider

    def _update_value_label(self, label: QLabel, key: str, value: int) -> None:
        # rotate-слайдер хранит value 0..72 (×5 = градусы); zoom 0..100 (=/10).
        if key == "camera_zoom":
            label.setText(f"{value / 10:.1f}")
        elif key == "camera_horizontal":
            label.setText(f"{value * 5}°")
        else:
            label.setText(f"{value:+d}°")

    # значения камеры для API (реальные единицы fal)
    def _api_values(self) -> Tuple[float, float, float]:
        return (
            float(self.rotate_slider.value() * 5),      # 0..360°
            float(self.vertical_slider.value()),        # -30..90°
            self.zoom_slider.value() / 10.0,            # 0.0..10.0
        )

    def _clear_current_source(self) -> None:
        """Крестик на исходнике: слот → «перетащи кадр», сфера без кадра.
        Результаты не трогаем."""
        self._current_ref = None
        self.current_slot.clear_image()
        self.orbit.set_frame_image(None)
        QTimer.singleShot(0, self._update_big_result)   # высота слота изменилась
        self._save_state()

    def _add_references(self, slot_type: str, paths: List[Path]) -> None:
        """Оставлен только основной кадр (Current shot); референсы убраны
        вместе со старым промпт-путём (2026-07-02, fal)."""
        for path in paths:
            if slot_type != "Current shot":
                continue
            current_path = self._copy_current_shot_to_camera_folder(path)
            self._current_ref = CameraReference(path=current_path, ref_type=slot_type)
            self.current_slot.set_image(current_path)
            self.orbit.set_frame_image(current_path)
            self._reset_camera_controls()
            QTimer.singleShot(0, self._update_big_result)   # аспект слота сменился
        self._save_state()

    def _copy_current_shot_to_camera_folder(self, path: Path) -> Path:
        show_slug = self._current_show_slug()
        if not show_slug:
            return path
        out_dir = self._project_root / "shows" / show_slug / "camera_lab" / "outputs"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            source = path.resolve()
            if source.parent == out_dir.resolve():
                return path
            suffix = path.suffix or ".jpg"
            dest = out_dir / f"source_{path.stem}{suffix}"
            index = 2
            while dest.exists():
                dest = out_dir / f"source_{path.stem}_{index}{suffix}"
                index += 1
            shutil.copy2(path, dest)
            return dest
        except Exception:
            return path

    def set_shot_clipboard_available(self, available: bool) -> None:
        self.current_slot.set_paste_available(available)

    def _shot_clipboard_bytes(self) -> Optional[bytes]:
        if self._get_shot_clipboard is None:
            return None
        try:
            return self._get_shot_clipboard()
        except Exception:
            return None

    def _paste_current_from_shot_clipboard(self) -> None:
        data = self._shot_clipboard_bytes()
        if not data:
            self._set_generation_status(tr("status_clipboard_empty"))
            self.current_slot.set_paste_available(False)
            return
        lab_dir = self._camera_lab_dir()
        if lab_dir is None:
            self._set_generation_status(tr("camera_need_show"))
            return
        try:
            out_dir = lab_dir / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = out_dir / f"source_clipboard_{ts}.jpg"
            path.write_bytes(data)
            self._current_ref = CameraReference(path=path, ref_type="Current shot")
            self.current_slot.set_image(path)
            self.orbit.set_frame_image(path)
            self.orbit.set_frame_aspect(self.current_slot.aspect_ratio())
            self._reset_camera_controls()
            self._set_generation_status(tr("camera_pasted_from_clipboard"))
            self._save_state()
        except Exception as exc:
            self._set_generation_status(f"{tr('camera_generation_error')}\n{exc}")

    def _open_image_popup(self, path: Path) -> None:
        if not path or not path.exists():
            return
        existing = getattr(self, "_image_viewer", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        try:
            dlg = CameraResultDialog(path, self)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dlg.destroyed.connect(lambda *_: setattr(self, "_image_viewer", None))
            self._image_viewer = dlg
            dlg.show()
            dlg.raise_()
        except Exception:
            pass

    def _sync_control_state(self) -> None:
        # орбита живёт в РЕАЛЬНЫХ единицах API: градусы h, градусы v, зум×10
        self.orbit.set_values(
            self.rotate_slider.value() * 5,
            self.vertical_slider.value(),
            self.zoom_slider.value(),
        )
        if self._current_ref:
            self.orbit.set_frame_image(self._current_ref.path)
        self._save_state()

    def _on_preview_values_changed(self, h_deg: int, vertical: int, zoom_x10: int) -> None:
        for slider, value in (
            (self.rotate_slider, int(round(h_deg / 5))),   # градусы → шаг ×5
            (self.vertical_slider, vertical),
            (self.zoom_slider, zoom_x10),
        ):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._refresh_slider_value_labels()
        self._save_state()

    def _refresh_slider_value_labels(self) -> None:
        for key, slider in (
            ("camera_horizontal", self.rotate_slider),
            ("camera_vertical", self.vertical_slider),
            ("camera_zoom", self.zoom_slider),
        ):
            label = self._slider_value_labels.get(key)
            if label is not None:
                self._update_value_label(label, key, slider.value())

    def _reset_camera_controls(self) -> None:
        # дефолты fal: h=0, v=0, zoom=5.0 (слайдер 50)
        for slider, value in (
            (self.rotate_slider, 0),
            (self.vertical_slider, 0),
            (self.zoom_slider, 50),
        ):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._refresh_slider_value_labels()
        self._sync_control_state()

    def apply_lang(self) -> None:
        self.title_lbl.setText(tr("camera_title"))
        self.subtitle_lbl.setText(tr("camera_subtitle"))
        self.source_title_lbl.setText(tr("camera_source_title"))
        self.controls_title_lbl.setText(tr("camera_controls_title"))
        self.fal_key_title_lbl.setText(tr("camera_fal_key_label"))
        self.fal_key_edit.setPlaceholderText(tr("camera_fal_key_placeholder"))
        self.fal_key_btn.setText(tr("camera_fal_key_confirm"))
        self._set_balance_text(None)
        self.generate_btn.setText(tr("camera_generate"))
        self.result_title_lbl.setText(tr("camera_result"))
        self._update_big_result()   # placeholder большого окна при пустых результатах
        for label in self._slider_labels:
            key = label.property("_i18n_key")
            if key:
                label.setText(tr(key))
        self.current_slot.retranslate()
        self._refresh_slider_value_labels()

    # ── Ключ fal + баланс (2026-07-02) ─────────────────────────────────
    def _toggle_fal_key_visible(self) -> None:
        """Глазик в поле ключа: показать открытым текстом / скрыть точками."""
        hidden = self.fal_key_edit.echoMode() == QLineEdit.EchoMode.Password
        self.fal_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        self._fal_key_eye.setIcon(
            _camera_icon("eye-off" if hidden else "eye"))
        self._fal_key_eye.setToolTip(
            tr("camera_fal_key_hide" if hidden else "camera_fal_key_show"))

    def _on_fal_key_confirm(self) -> None:
        """«Подтвердить»: сохранить ключ (QSettings + fal_key.txt, без
        перезапуска) и проверить его запросом баланса."""
        from storyboard_app import save_fal_key
        key = self.fal_key_edit.text().strip()
        save_fal_key(key)
        if not key:
            self._set_balance_text(None)
            return
        self._set_generation_status(tr("camera_fal_key_saved"))
        self._refresh_fal_balance()

    def _set_balance_text(self, value: Optional[float]) -> None:
        amount = f"${value:.2f}" if value is not None else "$ —"
        self.fal_balance_lbl.setText(
            f"{tr('camera_fal_balance')} <b>{amount}</b> &nbsp; "
            f"<a href=\"https://fal.ai/dashboard\" style=\"color:#8ab4f8;\">"
            f"{tr('camera_fal_dashboard')}</a>"
        )

    def _refresh_fal_balance(self) -> None:
        """Живой баланс: при подтверждении ключа, показе вкладки и после
        каждой генерации. Один тред за раз; ошибки не критичны ($ —)."""
        if self._balance_thread is not None and self._balance_thread.isRunning():
            return
        thread = FalBalanceThread(self)
        thread.balance.connect(lambda v: self._set_balance_text(v))
        thread.error.connect(lambda _msg: self._set_balance_text(None))
        self._balance_thread = thread
        thread.start()

    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        self._refresh_fal_balance()
        QTimer.singleShot(0, self._update_big_result)   # size-sync после layout

    def _set_generation_status(self, text: str = "", is_error: bool = False) -> None:
        if not hasattr(self, "generation_status_lbl"):
            return
        self.generation_status_lbl.setProperty("error", bool(is_error))
        self.generation_status_lbl.style().unpolish(self.generation_status_lbl)
        self.generation_status_lbl.style().polish(self.generation_status_lbl)
        self.generation_status_lbl.setText(text)
        self.generation_status_lbl.setVisible(bool(text))

    def _run_generation(self) -> None:
        """2026-07-02: генерация через fal (FalAnglesThread). Одна генерация
        на клик — углы детерминированы, батч не нужен."""
        if self._generation_jobs:
            return
        self._last_generation_status = ""
        if not self._current_ref:
            self._set_generation_status(tr("camera_need_current"))
            return
        show_slug = self._current_show_slug()
        if not show_slug:
            self._set_generation_status(tr("camera_need_show"))
            return
        from storyboard_app import load_fal_key
        if not load_fal_key():
            self._set_generation_status(tr("camera_fal_no_key"), is_error=True)
            return
        out_dir = self._project_root / "shows" / show_slug / "camera_lab" / "outputs"
        h_deg, v_deg, zoom = self._api_values()
        self.generate_btn.setEnabled(False)
        self._generation_run_id += 1
        run_id = self._generation_run_id
        thread = FalAnglesThread(
            image_path=self._current_ref.path,
            horizontal_angle=h_deg,
            vertical_angle=v_deg,
            zoom=zoom,
            out_dir=out_dir,
            parent=None,
        )
        job = CameraGenerationJob(
            thread=thread,
            horizontal=h_deg,
            vertical=v_deg,
            zoom=zoom,
            status=tr("camera_generation_starting"),
        )
        self._generation_jobs[run_id] = job
        thread.progress.connect(lambda message, rid=run_id: self._on_generation_progress(rid, message))
        thread.finished.connect(lambda path, rid=run_id: self._on_generation_done(rid, path))
        thread.finished.connect(lambda _path, rid=run_id: self._on_generation_finished(rid))
        thread.error.connect(lambda message, rid=run_id: self._on_generation_error(rid, message))
        thread.start()
        self._render_generation_status()
        if not self._generation_timer.isActive():
            self._generation_timer.start()

    def _current_aspect_ratio_label(self) -> str:
        aspect = self.current_slot.aspect_ratio()
        if aspect <= 0:
            return "16:9"
        if aspect < 0.85:
            return "9:16"
        if 0.92 <= aspect <= 1.08:
            return "1:1"
        return "16:9"

    def _current_show_slug(self) -> Optional[str]:
        try:
            data = json.loads(
                (self._project_root / "current_show.json").read_text(encoding="utf-8")
            )
            value = data.get("current")
            return str(value) if value else None
        except Exception:
            return None

    def _camera_lab_dir(self) -> Optional[Path]:
        show_slug = self._current_show_slug()
        if not show_slug:
            return None
        return self._project_root / "shows" / show_slug / "camera_lab"

    def _state_path(self) -> Optional[Path]:
        lab_dir = self._camera_lab_dir()
        return lab_dir / "state.json" if lab_dir is not None else None

    def _result_paths(self) -> List[Path]:
        paths: List[Path] = []
        for i in range(self.results_strip_lay.count()):
            item = self.results_strip_lay.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, CameraResultThumb):
                paths.append(widget.path())
        return paths

    @staticmethod
    def _remove_layout_widgets(layout: QLayout, widget_type: type) -> None:
        for i in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, widget_type):
                layout.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()

    def _save_state(self) -> None:
        if getattr(self, "_loading_state", False):
            return
        state_path = self._state_path()
        if state_path is None:
            return
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            h_deg, v_deg, zoom = self._api_values()
            data = {
                "version": 2,   # v2 (2026-07-02, fal): без refs/model; controls в API-единицах
                "current_ref": (
                    {
                        "path": str(self._current_ref.path),
                        "type": self._current_ref.ref_type,
                    }
                    if self._current_ref
                    else None
                ),
                "results": [
                    {"path": str(path)}
                    for path in self._result_paths()
                    if path.exists()
                ],
                "controls": {
                    "horizontal_deg": h_deg,
                    "vertical_deg": v_deg,
                    "zoom": zoom,
                },
            }
            state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_state(self) -> None:
        self._loading_state = True
        try:
            state_path = self._state_path()
            data = {}
            if state_path is not None and state_path.exists():
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded

            controls = data.get("controls") if isinstance(data.get("controls"), dict) else {}
            # v2-ключи (fal, 2026-07-02). Старый state (v1: horizontal/vertical/
            # zoom в других шкалах) не мигрируем — дефолты.
            h_deg = int(controls.get("horizontal_deg", 0))
            v_deg = int(controls.get("vertical_deg", 0))
            zoom = float(controls.get("zoom", 5.0))
            for slider, value in (
                (self.rotate_slider, max(0, min(72, int(round(h_deg / 5))))),
                (self.vertical_slider, max(-30, min(90, v_deg))),
                (self.zoom_slider, max(0, min(100, int(round(zoom * 10))))),
            ):
                slider.blockSignals(True)
                slider.setValue(value)
                slider.blockSignals(False)
            self._refresh_slider_value_labels()

            current_data = data.get("current_ref") if isinstance(data.get("current_ref"), dict) else None
            current_path = Path(str(current_data.get("path"))) if current_data and current_data.get("path") else None
            if current_path is not None and current_path.exists():
                self._current_ref = CameraReference(
                    path=current_path,
                    ref_type=str(current_data.get("type") or "Current shot"),
                )
                self.current_slot.set_image(current_path)
                self.orbit.set_frame_image(current_path)

            self._remove_layout_widgets(self.results_strip_lay, CameraResultThumb)
            result_items = data.get("results") if isinstance(data.get("results"), list) else []
            result_paths = []
            if self._current_ref is not None:
                result_paths = [
                    Path(str(item.get("path")))
                    for item in result_items
                    if isinstance(item, dict) and item.get("path")
                ]
            for path in result_paths:
                if path.exists():
                    self._show_result_preview(path)
                    self._last_result_path = path
            self.results_scroll.setVisible(self._result_thumb_count() > 0)

            self.generate_btn.setEnabled(not bool(self._generation_jobs))
            self._set_generation_status("")
            self._update_big_result()
            self._sync_control_state()
        except Exception:
            pass
        finally:
            self._loading_state = False

    def _render_generation_status(self) -> None:
        if not self._generation_jobs:
            self._set_generation_status(self._last_generation_status)
            return
        lines = self._last_generation_status.splitlines() if self._last_generation_status else []
        for index, run_id in enumerate(sorted(self._generation_jobs), start=1):
            job = self._generation_jobs[run_id]
            status = job.status or tr("camera_generation_starting")
            line = (f"{index}. fal · {int(job.horizontal)}° / {int(job.vertical):+d}° / "
                    f"{job.zoom:.1f} — {status}")
            if job.elapsed_started:
                line += f" · {tr('camera_generation_elapsed', sec=job.elapsed)}"
            lines.append(line)
        self._set_generation_status("\n".join(lines))

    def _on_generation_progress(self, run_id: int, message: str) -> None:
        job = self._generation_jobs.get(run_id)
        if job is None:
            return
        job.status = message or job.status
        if message == tr("camera_fal_generating") and not job.elapsed_started:
            job.elapsed = 0
            job.elapsed_started = True
            self._generation_timer.start()
        self._render_generation_status()

    def _tick_generation_timer(self) -> None:
        if not self._generation_jobs:
            self._generation_timer.stop()
            return
        active = False
        for job in self._generation_jobs.values():
            if job.thread.isRunning():
                active = True
            if job.elapsed_started and job.thread.isRunning():
                job.elapsed += 1
        if not active:
            self._generation_timer.stop()
        self._render_generation_status()

    def _on_generation_done(self, run_id: int, output_path: str) -> None:
        job = self._generation_jobs.get(run_id)
        if job is None:
            return
        job.elapsed_started = False
        path = Path(output_path)
        self._append_generation_manifest(path, job)
        self._last_result_path = path
        self._show_result_preview(path)
        self._refresh_fal_balance()   # генерация списала $ — обновляем

    def _on_generation_error(self, run_id: int, message: str) -> None:
        job = self._generation_jobs.get(run_id)
        if job is None:
            return
        job.elapsed_started = False
        error_text = (message or "").strip() or tr("camera_generation_error")
        job.status = f"{tr('camera_generation_error')} {error_text}"
        line = (f"fal · {int(job.horizontal)}° / {int(job.vertical):+d}° / "
                f"{job.zoom:.1f} — {job.status}")
        self._last_generation_status = (
            f"{self._last_generation_status}\n{line}"
            if self._last_generation_status
            else line
        )
        self._render_generation_status()
        self._on_generation_finished(run_id)

    def _on_generation_finished(self, run_id: Optional[int] = None) -> None:
        if run_id is not None:
            self._generation_jobs.pop(run_id, None)
        if not self._generation_jobs:
            self._generation_timer.stop()
            self._set_generation_status(self._last_generation_status)
            self.generate_btn.setEnabled(True)
            return
        self._render_generation_status()

    def _show_result_preview(self, output_path: Path) -> None:
        pixmap = QPixmap(str(output_path))
        if pixmap.isNull():
            self._set_generation_status(tr("camera_generation_error"))
            return
        thumb = CameraResultThumb(
            output_path,
            self.results_strip,
            aspect_ratio=self.current_slot.aspect_ratio() if self._current_ref else None,
        )
        thumb.clicked.connect(self._open_result_viewer)
        thumb.revealRequested.connect(self._reveal_result)
        thumb.copyRequested.connect(self._copy_result_to_shot_clipboard)
        thumb.deleteRequested.connect(lambda path, widget=thumb: self._delete_result(path, widget))
        self.results_strip_lay.insertWidget(self.results_strip_lay.count() - 1, thumb)
        self.results_scroll.setVisible(True)
        self._update_big_result()
        self._save_state()

    def _update_big_result(self) -> None:
        """Большое окно результата: последняя генерация, scaled под размер;
        нет результатов → текстовый placeholder."""
        big = getattr(self, "big_result", None)
        if big is None:
            return
        # один в один с окном исходника: ширина у обоих = ширина панели,
        # высоту жёстко копируем (у слота она из heightForWidth по аспекту)
        slot_h = self.current_slot.height() if hasattr(self, "current_slot") else 0
        if slot_h > 50 and big.height() != slot_h:
            big.setFixedHeight(slot_h)
        path = self._last_result_path
        if path is None or not Path(path).exists():
            big.setPixmap(QPixmap())
            big.setText("")   # 2026-07-02: без центр-подписи — заголовок слева
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            big.setText("")
            return
        big.setText("")
        size = big.size()
        big.setPixmap(pixmap.scaled(
            max(64, size.width()), max(64, size.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_big_result()   # пере-scale большого превью под новый размер

    def _copy_result_to_shot_clipboard(self, path: Path) -> None:
        if self._set_shot_clipboard is None:
            return
        if not path or not path.exists():
            return
        try:
            self._set_shot_clipboard(path.read_bytes())
            self._set_generation_status(tr("camera_result_copied"))
        except Exception as exc:
            self._set_generation_status(f"{tr('camera_generation_error')}\n{exc}")

    def _open_result_viewer(self, path: Optional[Path] = None) -> None:
        path = path or self._last_result_path
        self._open_image_popup(path)

    def _reveal_result(self, path: Optional[Path] = None) -> None:
        path = path or self._last_result_path
        if not path or not path.exists():
            return
        try:
            from storyboard_app import reveal_in_file_manager

            reveal_in_file_manager(path)
        except Exception:
            pass

    def _delete_result(self, path: Optional[Path] = None, widget: Optional[QWidget] = None) -> None:
        path = path or self._last_result_path
        if not path or not path.exists():
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("camera_delete_confirm_title"))
        box.setText(tr("camera_delete_confirm_body"))
        delete_btn = box.addButton(
            tr("camera_result_delete"), QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(tr("camera_delete_cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        if box.clickedButton() is not delete_btn:
            return
        try:
            path.unlink()
            self._remove_from_manifest(path)
        except Exception as exc:
            self._set_generation_status(f"{tr('camera_generation_error')}\n{exc}")
            return
        if self._last_result_path == path:
            self._last_result_path = self._newest_result_path(excluding=path)
        if widget is None:
            widget = self._find_result_thumb(path)
        if widget is not None:
            self.results_strip_lay.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        has_results = self._result_thumb_count() > 0
        self.results_scroll.setVisible(has_results)
        self._update_big_result()
        self._set_generation_status("" if has_results else tr("camera_result_deleted"))
        self._save_state()

    def _find_result_thumb(self, path: Path) -> Optional[CameraResultThumb]:
        for i in range(self.results_strip_lay.count()):
            item = self.results_strip_lay.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, CameraResultThumb) and widget.path() == path:
                return widget
        return None

    def _result_thumb_count(self) -> int:
        count = 0
        for i in range(self.results_strip_lay.count()):
            item = self.results_strip_lay.itemAt(i)
            if isinstance(item.widget() if item else None, CameraResultThumb):
                count += 1
        return count

    def _newest_result_path(self, excluding: Optional[Path] = None) -> Optional[Path]:
        for i in range(self.results_strip_lay.count() - 1, -1, -1):
            item = self.results_strip_lay.itemAt(i)
            widget = item.widget() if item else None
            if isinstance(widget, CameraResultThumb) and widget.path() != excluding:
                return widget.path()
        return None

    def _remove_from_manifest(self, output_path: Path) -> None:
        show_slug = self._current_show_slug()
        if not show_slug:
            return
        show_root = self._project_root / "shows" / show_slug
        manifest_path = show_root / "camera_lab" / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return
            try:
                rel_output = str(output_path.relative_to(show_root))
            except Exception:
                rel_output = str(output_path)
            filtered = [item for item in data if item.get("output") != rel_output]
            manifest_path.write_text(
                json.dumps(filtered, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _append_generation_manifest(self, output_path: Path, job: CameraGenerationJob) -> None:
        try:
            show_slug = self._current_show_slug()
            if not show_slug:
                return
            show_root = self._project_root / "shows" / show_slug
            manifest_path = show_root / "camera_lab" / "manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
            try:
                rel_output = str(output_path.relative_to(show_root))
            except Exception:
                rel_output = str(output_path)
            data.append({
                "output": rel_output,
                "model": FAL_MODEL,
                "provider": "fal",
                "elapsed_sec": job.elapsed,
                "angles": {
                    "horizontal_deg": job.horizontal,
                    "vertical_deg": job.vertical,
                    "zoom": job.zoom,
                },
            })
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _qss() -> str:
        return """
        QWidget#camera-lab {
            background: #121313;
        }
        QLabel#camera-title {
            color: #f7f8f4;
            font-size: 24px;
            font-weight: 700;
        }
        QLabel#camera-subtitle {
            color: rgba(255,255,255,0.48);
            font-size: 12px;
        }
        QFrame#camera-panel {
            background: #191b1d;
            border: none;
            border-radius: 8px;
        }
        QFrame#camera-main-slot {
            /* 2026-07-02: паспарту = фон страницы (#121313, как
               QWidget#camera-lab) — без серой плитки вокруг кадра */
            background: #121313;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QFrame#camera-ref-drop-area {
            background: #131516;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QFrame#camera-ref-drop-area:hover {
            border: 1px solid #1d1e20;
        }
        QLabel#camera-slot-image {
            background: transparent;   /* поля вокруг кадра = фон страницы */
            border-radius: 6px;
        }
        QLabel#camera-slot-title {
            color: #f7f8f4;
            font-size: 13px;
            font-weight: 650;
        }
        QFrame#camera-main-slot QLabel#camera-slot-title {
            font-size: 18px;
        }
        QLabel#camera-slot-hint,
        QLabel#camera-slider-label,
        QLabel#camera-ref-drop-hint {
            color: rgba(255,255,255,0.55);
            font-size: 12px;
        }
        QLabel#camera-ref-drop-title {
            color: #f7f8f4;
            font-size: 15px;
            font-weight: 650;
        }
        QLabel#camera-section-title {
            color: #b8b8b8;
            font-size: 13px;
            font-weight: 650;
        }
        QLabel#camera-generation-status {
            color: #e4e5df;
            background: transparent;
            font-size: 12px;
            line-height: 1.25;
            padding: 2px 0px 0px 0px;
        }
        QLabel#camera-generation-status[error="true"] {
            color: #ff7b86;
        }
        QScrollArea#camera-refs-scroll {
            background: transparent;
            border: none;
            min-height: 142px;
            max-height: 152px;
        }
        QWidget#camera-refs-strip {
            background: transparent;
        }
        QScrollArea#camera-results-panel {
            background: transparent;
            border: none;
            min-height: 184px;
        }
        QWidget#camera-results-strip {
            background: transparent;
        }
        QFrame#camera-ref-thumb {
            background: #131516;
            border: none;
            border-radius: 8px;
            min-width: 112px;
            max-width: 112px;
        }
        QFrame#camera-ref-thumb-wrap {
            background: rgba(255,255,255,0.04);
            border-radius: 6px;
        }
        QFrame#camera-result-thumb {
            background: transparent;
            border: none;
            border-radius: 8px;
        }
        QFrame#camera-result-thumb-wrap {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 8px;
        }
        QLabel#camera-thumb-image {
            background: rgba(255,255,255,0.04);
            border-radius: 6px;
        }
        QLabel#camera-result-thumb-image {
            background: transparent;
            border-radius: 8px;
        }
        QLabel#camera-result-big {
            /* фон страницы (#121313, как QWidget#camera-lab) */
            background: #121313;
            color: rgba(255,255,255,0.35);
            border: 1px solid #1d1e20;
            border-radius: 8px;
            font-size: 12px;
        }
        QScrollArea#camera-results-panel, QWidget#camera-results-strip {
            /* лента миниатюр — на общем фоне панели, без своих плашек */
            background: transparent;
            border: none;
        }
        QLineEdit#camera-fal-key {
            background: #131516;
            color: #e4e5df;
            border: 1px solid #1d1e20;
            border-radius: 8px;
            padding: 6px 10px;
            font-size: 12px;
        }
        QPushButton#camera-fal-key-btn {
            background: #242628;
            color: #f2f3f0;
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 8px;
            padding: 0px 14px;
            font-weight: 600;
        }
        QPushButton#camera-fal-key-btn:hover {
            background: #2c2f31;
        }
        QLabel#camera-fal-balance {
            color: #b8b8b8;
            font-size: 12px;
        }
        QPlainTextEdit#camera-prompt-preview {
            background: #131516;
            color: #b8b8b8;
            border: 1px solid #1d1e20;
            border-radius: 8px;
            padding: 8px;
            selection-background-color: #2c2f31;
            font-size: 11px;
        }
        QLabel#camera-result-preview {
            background: #131516;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QLabel#camera-ref-name,
        QLabel#camera-slider-value {
            color: #f7f8f4;
            font-size: 11px;
        }
        QToolButton#camera-thumb-overlay-btn,
        QToolButton#camera-thumb-overlay-trash,
        QToolButton#camera-slot-paste-btn {
            background: rgba(29,31,33,0.82);
            border: 1px solid rgba(255,255,255,0.20);
            border-radius: 8px;
            padding: 0px;
            min-width: 22px;
            max-width: 22px;
            min-height: 22px;
            max-height: 22px;
        }
        QToolButton#camera-slot-paste-btn {
            min-width: 28px;
            max-width: 28px;
            min-height: 28px;
            max-height: 28px;
        }
        QToolButton#camera-thumb-overlay-btn:hover,
        QToolButton#camera-slot-paste-btn:hover {
            background: rgba(48,51,53,0.94);
            border: 1px solid rgba(255,255,255,0.34);
        }
        QToolButton#camera-thumb-overlay-trash {
            border: 1px solid rgba(232,75,74,0.38);
        }
        QToolButton#camera-thumb-overlay-trash:hover {
            background: rgba(52,32,34,0.94);
            border: 1px solid rgba(255,105,105,0.72);
        }
        QSlider::groove:horizontal {
            height: 4px;
            background: #2c2f31;
            border-radius: 2px;
        }
        QSlider::handle:horizontal {
            background: #d4a256;
            width: 16px;
            height: 16px;
            margin: -6px 0;
            border-radius: 8px;
        }
        QPushButton#camera-generate-btn {
            background: #2c2f31;
            color: #f7f8f4;
            border: 1px solid #1d1e20;
            border-radius: 8px;
            font-weight: 650;
        }
        QPushButton#camera-generate-btn:hover {
            background: #36393b;
        }
        QPushButton#camera-generate-btn:pressed {
            background: #242729;
        }
        QPushButton#camera-secondary-btn,
        QPushButton#camera-danger-btn {
            background: #2c2f31;
            color: #f7f8f4;
            border: 1px solid #1d1e20;
            border-radius: 8px;
            font-weight: 650;
            padding: 8px 10px;
        }
        QPushButton#camera-secondary-btn:hover {
            background: #36393b;
        }
        QPushButton#camera-danger-btn {
            color: #ff7b8d;
        }
        QPushButton#camera-danger-btn:hover {
            background: #3a2429;
        }
        """
