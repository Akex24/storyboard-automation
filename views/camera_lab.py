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
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap,
    QPolygonF, QTransform,
)
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QApplication,
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
# Стем-резолв протухших путей (Adobe/Hazel-вотчер конвертит png→jpg и
# удаляет оригинал) — тот же лекарь, что у плиток Генератора.
from generator.result_cell import resolve_existing_path


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


class HWheelScrollArea(QScrollArea):
    """Лента миниатюр: вертикальное колесо крутит ГОРИЗОНТАЛЬНЫЙ скролл
    (без зажатых модификаторов). 2026-07-03."""

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - delta)
        event.accept()


class ResultPreviewLabel(QLabel):
    clicked = pyqtSignal()
    entered = pyqtSignal()
    left = pyqtSignal()

    def enterEvent(self, event):  # noqa: N802 - Qt override
        self.entered.emit()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        self.left.emit()
        super().leaveEvent(event)

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
    """Орбитальная миникарта камеры (2026-07-03: переписана A/B/C).

    (A) МОДЕЛЬ — единственный источник правды: horizontal 0..360 (wrap),
        vertical −30..90 (clamp), zoom 0..200 (= 0.0..20.0 ×10 int).
        Наружу — int (сигнал valuesChanged, API fal); внутри drag — float-
        аккумуляторы (_h_f/_v_f), чтобы 1px-дельты не съедались округлением.
    (B) DRAG→МОДЕЛЬ — ИНКРЕМЕНТАЛЬНО от дельты курсора (dx→h полный круг с
        wrap, dy→v в пределах диапазона), БЕЗ инверсии arccos/atan2 от
        абсолютной позиции — нет ни зеркал, ни упоров, ни двузначности.
        Колесо — зум. Телепорта «камера под курсор» нет.
    (C) МОДЕЛЬ→ЭКРАН — сферическая проекция (h,v) с наклоном мировой оси
        (_AXIS_TILT), орбита радиуса _cam_dist(зум); значок ВСЕГДА рисуется
        последним слоем (на задней полусфере полупрозрачный ≥40%).
    Слайдеры и превью читают одну модель (set_values ↔ valuesChanged).
    Чистый QPainter — кроссплатформенно (Mac/Win)."""

    valuesChanged = pyqtSignal(int, int, int)   # h_deg, v_deg, zoom_x10

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._h = 0        # 0..360 (наружу/в API — int)
        self._v = 0        # -30..90
        self._z = 50       # 0..200 (= zoom 5.0)
        # 2026-07-03 (delta-drag, модель A): float-аккумуляторы углов — чтобы
        # медленный drag (dx=1px → доли градуса) не съедался int-округлением.
        self._h_f = 0.0
        self._v_f = 0.0
        self._frame_pixmap: Optional[QPixmap] = None
        self._drag_last: Optional[QPointF] = None   # последняя позиция drag
        self.setMinimumSize(260, 300)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # контракт под CameraLabView (имена прежние)
    def set_values(self, h_deg: int, v_deg: int, zoom_x10: int) -> None:
        self._h = int(h_deg) % 360
        self._v = max(-30, min(90, int(v_deg)))
        self._z = max(0, min(100, int(zoom_x10)))   # 2026-07-03: fal-предел 10.0 (422 на >10)
        # синк float-аккумуляторов drag (во время drag слайдеры blockSignals →
        # сюда не заходим, аккумулятор не сбивается)
        self._h_f = float(self._h)
        self._v_f = float(self._v)
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
            # 2026-07-03 (delta-drag, B): только захват — телепорта «камера
            # под курсор» больше нет (он был источником зеркал/прыжков).
            self._drag_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if self._drag_last is None:
            super().mouseMoveEvent(event)
            return
        # 2026-07-03 (delta-drag, B — переписано начисто): приращение углов
        # от дельты курсора, БЕЗ arccos/atan2 от абсолютной позиции — так нет
        # ни зеркал, ни упоров, ни двузначности перед/зад. dx → горизонталь
        # (полный круг, wrap 360→0 в обе стороны), dy → вертикаль (кламп
        # −30..90, мышь вверх — камера выше).
        # Чувствительность (2026-07-03, «значок за мышкой один-в-один»):
        # 1px мыши = 1px ДУГИ на текущей орбите значка (K = 57.3°/r_cam) —
        # значок держится у курсора на любом зуме (жалоба «еле ползёт по
        # пятачку» была из-за фикс-K: на малой орбите значок физически полз
        # медленнее мыши). Пол — 360°/ширина и 120°/высота превью: на большом
        # зуме размах через виджет всё равно даёт полный круг/весь диапазон.
        pos = event.position()
        dx = pos.x() - self._drag_last.x()
        dy = pos.y() - self._drag_last.y()
        self._drag_last = pos
        _, _, radius = self._sphere_geometry()
        if radius <= 0:
            return
        r_cam = max(8.0, self._cam_dist(radius))
        deg_1to1 = math.degrees(1.0 / r_cam)          # 1px дуги орбиты
        kh = max(deg_1to1, 360.0 / max(1, self.width()))
        kv = max(deg_1to1, 120.0 / max(1, self.height()))
        self._h_f = (self._h_f + dx * kh) % 360.0
        self._v_f = max(-30.0, min(90.0, self._v_f - dy * kv))
        self._h = int(round(self._h_f)) % 360
        self._v = int(round(self._v_f))
        self.update()
        self.valuesChanged.emit(self._h, self._v, self._z)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._drag_last is not None:
            self._drag_last = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        step = 5 if event.angleDelta().y() > 0 else -5   # 0.5 зума за щелчок
        self._z = max(0, min(100, self._z + step))       # 2026-07-03: fal-предел 10.0
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

    def _cam_dist(self, radius: float) -> float:
        """2026-07-03: ВИЗУАЛЬНАЯ дистанция значка камеры от центра = f(зум).
        Семантика ЗАФИКСИРОВАНА: ползунок 0 = БЛИЗКО → значок ВПЛОТНУЮ к
        карточке (0.18r); ползунок 10 = ДАЛЕКО → значок на КРАЮ сферы (1.0r).
        Линейно; дефолт ui 5.0 → 0.59r ≈ 66px (средняя «не прилипающая»
        дистанция). Drag-чувствительность 1:1 считается от этого же r_cam."""
        t_far = self._z / 100.0                  # ползунок = «дальность»
        return radius * (0.18 + 0.82 * t_far)

    # 2026-07-02 (фикс №2): ВИЗУАЛЬНАЯ широта камеры — растянутый маппинг
    # на полный видимый диапазон сферы (якоря: −30°=дно, ПОД кадром;
    # 0°=экватор; +90°=полюс, НАД кадром). В API уходит реальный v.
    @staticmethod
    def _v_to_vis(v_deg: float) -> float:
        return v_deg * 3.0 if v_deg < 0 else v_deg
    # (_vis_to_v удалён 2026-07-03 вместе с инверсией _apply_cursor —
    # delta-drag работает напрямую в градусах модели)

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

        # камера (модель→экран, C): долгота = горизонталь; широта —
        # растянутый ВИЗУАЛЬНЫЙ маппинг (−30° ПОД кадр, +90° — над). Признак
        # «за глобусом» — по долготе 90..270 (знак Z после наклона оси
        # ошибочно топил камеру у полюса). Орбита РАДИУСА _cam_dist(зум) —
        # наезд/отъезд по всей шкале (0 → вплотную к кадру, 20 → край).
        # 2026-07-03 (слои, C): значок НЕ рисуется здесь — он ПОСЛЕДНИЙ слой
        # (после карточки и передней сетки), см. конец paintEvent: раньше
        # behind-значок рисовался ПЕРВЫМ и полностью перекрывался карточкой
        # кадра («значок пропадает» на части горизонтали).
        cam_dx, cam_dy, _cam_z = self._project(
            self._v_to_vis(self._v), self._h, self._cam_dist(radius))
        cam_x, cam_y = cx + cam_dx, cy + cam_dy
        behind = 90 < (self._h % 360) < 270

        # ── миниатюра кадра: СТОИТ вертикально, наклонена КАК ОСЬ глобуса ──
        # (2026-07-03, фикс оси наклона): предыдущая версия клала кадр плашмя
        # в экватор (pitch вокруг горизонтали X) — карточка «ложилась на
        # спину» и сплющивалась в блин. Правильно: кадр — ФРОНТАЛЬНАЯ
        # вертикальная плоскость, к которой применена ТА ЖЕ матрица наклона
        # мировой оси (_AXIS_TILT), что у сетки: точка (u, v, 0) →
        # y2 = v·ct, z2 = v·st → экран (u, −v·ct) — высота сохраняется
        # (·cos18° ≈ 0.95, НЕ схлопывается), верх слегка откинут вглубь;
        # лёгкая перспектива (f = 3r) даёт мягкую трапецию — картина,
        # откинутая назад ВМЕСТЕ с наклонённым глобусом, не блин.
        # Значок камеры и линия взгляда — без изменений.
        thumb_w = radius * 0.95
        thumb_h = thumb_w * 9 / 16
        if self._frame_pixmap is not None and self._frame_pixmap.height() > 0:
            aspect = self._frame_pixmap.width() / self._frame_pixmap.height()
            if aspect < 1.0:
                thumb_h = thumb_w
                thumb_w = thumb_h * aspect
            else:
                thumb_h = thumb_w / aspect
        ct, st = math.cos(self._AXIS_TILT), math.sin(self._AXIS_TILT)
        focus = radius * 3.0

        def _tilt_point(u: float, v: float) -> QPointF:
            """Точка фронтальной плоскости кадра (u вправо, v вверх) → экран
            той же матрицей наклона оси + слабая перспектива."""
            z2 = v * st                        # depth: верх уходит вглубь
            m = focus / max(1e-3, focus - z2)  # перспективный масштаб
            return QPointF(cx + u * m, cy - (v * ct) * m)

        quad = QPolygonF([
            _tilt_point(-thumb_w / 2, +thumb_h / 2),   # верх-лево
            _tilt_point(+thumb_w / 2, +thumb_h / 2),   # верх-право
            _tilt_point(+thumb_w / 2, -thumb_h / 2),   # низ-право
            _tilt_point(-thumb_w / 2, -thumb_h / 2),   # низ-лево
        ])
        frame_pen = QPen(QColor(255, 255, 255, 70), 1)
        frame_pen.setCosmetic(True)     # рамка 1px, не сплющивается трансформом
        if self._frame_pixmap is not None:
            scaled = self._frame_pixmap.scaled(
                QSize(max(2, int(thumb_w)), max(2, int(thumb_h))),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            src_quad = QPolygonF([
                QPointF(0, 0), QPointF(scaled.width(), 0),
                QPointF(scaled.width(), scaled.height()),
                QPointF(0, scaled.height()),
            ])
            xform = QTransform()
            if QTransform.quadToQuad(src_quad, quad, xform):
                painter.save()
                painter.setTransform(xform, True)
                clip = QPainterPath()
                clip.addRoundedRect(
                    QRectF(0, 0, scaled.width(), scaled.height()), 5, 5)
                painter.setClipPath(clip)
                painter.drawPixmap(0, 0, scaled)
                painter.setClipping(False)
                painter.setPen(frame_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(
                    QRectF(0, 0, scaled.width(), scaled.height()), 5, 5)
                painter.restore()
        else:
            painter.setPen(frame_pen)
            painter.setBrush(QColor(26, 29, 31))
            painter.drawPolygon(quad)

        painter.setPen(pen_front)
        for a, b in front_segs:
            painter.drawLine(a, b)

        # 2026-07-03 (C): значок камеры + линия — ВСЕГДА, ПОСЛЕДНИМ слоем
        # (поверх карточки и сетки). На задней полусфере — полупрозрачный
        # (dim), но никогда не исчезает.
        self._draw_camera(painter, cam_x, cam_y, cx, cy, dim=behind,
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
        """Значок камеры на орбите (_cam_dist) + линия взгляда к центру.
        Зум кодируется дистанцией орбиты, толщиной линии и бейджем «N.N».
        Задняя полусфера (dim) — полупрозрачный ≥40%, но ВСЕГДА рисуется
        (последний слой paintEvent — ничем не перекрывается).
        Lucide 'video' через get_icon; fallback — точка."""
        alpha = 110 if dim else 235   # 110/255 ≈ 43% — видим всегда
        line_w = 0.8 + (zoom_x10 / 100.0) * 2.6   # ui-zoom 0→0.8px … 10→3.4px
        line_pen = QPen(QColor(232, 184, 106, 70 if dim else 150), line_w)
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
                    painter.setOpacity(0.5)   # иконка тоже ≥40% на задней стороне
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
            # 2026-07-03: высоту окна задаёт РОДИТЕЛЬ (CameraLabView.
            # _recalc_media_windows) — адаптивно от размера окна, НЕ от аспекта
            # кадра. Раньше heightForWidth(width/aspect) → 9:16-кадр раздувал
            # окно на весь экран и лейаут ехал.
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.setMinimumWidth(420)
        else:
            self.setFixedSize(128, 128)

        lay = QVBoxLayout(self)
        # 2026-07-03: large-слот — без внутренних полей, кадр касается краёв
        lay.setContentsMargins(*( (0, 0, 0, 0) if large else (10, 10, 10, 10) ))
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

    def sizeHint(self):  # noqa: N802 - Qt override
        # 2026-07-03: heightForWidth/hasHeightForWidth убраны — высота окна
        # больше НЕ зависит от аспекта кадра (её задаёт родитель через
        # maximumHeight в _recalc_media_windows). Оставлен только width-hint.
        hint = super().sizeHint()
        if self._large:
            hint.setWidth(max(hint.width(), 640))
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
        # 2026-07-03: updateGeometry() убран — высота окна фиксируется извне,
        # перелейаут под аспект кадра больше не нужен.

    def set_paste_available(self, available: bool) -> None:
        self._paste_available = bool(available)
        if self.paste_btn is not None:
            self.paste_btn.setEnabled(self._paste_available)
            self.paste_btn.setVisible(self._paste_available and self.underMouse())

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._image_path is not None
        ):
            # 2026-07-03 (регрессия «клик по Источнику молчит»): путь ЛЕЧИМ
            # стем-резолвом — Hazel конвертит source_*.png→.jpg, старый guard
            # `.exists()` на мёртвом .png тихо гасил клик (у Result пути
            # лечатся, у Source — нет было).
            healed = resolve_existing_path(self._image_path)
            print(f"[CAMLAB] source CLICK raw={self._image_path} healed={healed}")
            if healed:
                self._image_path = Path(healed)
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
            if self._large:
                self.image.setText("")   # кадр загружен — текст не нужен
            return
        if self._large:
            # 2026-07-03: ОДНА строка по центру блока (image растянут
            # stretch=1 → честный центр по вертикали и горизонтали)
            self.title.setVisible(False)
            self.hint.setVisible(False)
            self.image.setText(tr("camera_drop_single"))
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
        self.retranslate()   # large → плейсхолдер-строка по центру
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
        # 2026-07-03: картинка — прямоугольник; клип по скруглённой рамке
        # КОНТЕЙНЕРА (radius 8 у #camera-main-slot) только где дотягивается.
        self.image.setPixmap(_pixmap_clipped_to_box(
            scaled, target.width(), target.height(), 8))

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



def _pixmap_clipped_to_box(scaled: QPixmap, box_w: int, box_h: int,
                           radius: int = 8) -> QPixmap:
    """Картинка — обычный ПРЯМОУГОЛЬНИК; обрезается по скруглённой рамке
    КОНТЕЙНЕРА только там, где до неё дотягивается (2026-07-03, замена
    _rounded_pixmap: раньше скруглялась сама картинка и на вертикальном
    кадре «висела уголками в воздухе» внутри прямой рамки).
    Картинка центрирована в боксе (box_w×box_h, как AlignCenter у QLabel) —
    клип-путь = roundedRect бокса, переведённый в координаты картинки.
    Если картинка меньше бокса по обеим осям — клип её не касается (углы
    прямые); где касается краёв бокса — срез по дуге рамки.
    QImage ARGB32_Premultiplied — гарантированная альфа, Mac/Win одинаково."""
    from PyQt6.QtGui import QImage
    img = QImage(scaled.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    off_x = (box_w - scaled.width()) / 2.0    # позиция бокса отн. картинки
    off_y = (box_h - scaled.height()) / 2.0
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(-off_x, -off_y, box_w, box_h), radius, radius)
    painter.setClipPath(clip)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return QPixmap.fromImage(img)


def _camera_icon(name: str) -> QIcon:
    try:
        from storyboard_app import get_icon

        return get_icon(name)
    except Exception:
        return QIcon()


def _file_data_available(path: Path) -> bool:
    """True, если у файла есть РЕАЛЬНЫЕ данные на диске. Ловит iCloud-evicted
    (dataless) плейсхолдеры: на macOS/APFS выгруженный файл имеет st_size>0,
    но st_blocks==0 (данные в облаке), и/или рядом лежит скрытый
    '.<имя>.icloud'. Кроссплатформенно: на Win/Linux st_blocks нет (getattr →
    None) → возвращаем True (нет iCloud-eviction в этом виде — генерацию НЕ
    блокируем). При любой ошибке — True (не мешаем). 2026-07-03."""
    try:
        p = Path(path)
        # старый механизм eviction — скрытый .icloud-сайдкар рядом
        if (p.parent / f".{p.name}.icloud").exists():
            return False
        if not p.exists():
            return False
        st = p.stat()
        blocks = getattr(st, "st_blocks", None)   # None на Windows → пропуск
        if blocks is not None and st.st_size > 0 and blocks == 0:
            return False   # dataless: логический размер есть, физических блоков ноль
        return True
    except Exception:
        return True


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
        self.reveal_btn.clicked.connect(self._emit_reveal)

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
        self.delete_btn.clicked.connect(self._emit_delete)

        overlay_lay.addWidget(self.reveal_btn)
        overlay_lay.addWidget(self.copy_btn)
        overlay_lay.addStretch()
        overlay_lay.addWidget(self.delete_btn)
        overlay_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        image_lay.addWidget(self.overlay, 0, 0)
        self.overlay.raise_()   # кнопки гарантированно ПОВЕРХ картинки

        lay.addWidget(self.image_wrap)
        self._refresh_pixmap()

    def _emit_reveal(self) -> None:
        print(f"[CAMLAB] thumb reveal_btn CLICKED path={self._path}")
        self.revealRequested.emit(self._path)

    def _emit_delete(self) -> None:
        print(f"[CAMLAB] thumb delete_btn CLICKED path={self._path}")
        self.deleteRequested.emit(self._path)

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
            print(f"[CAMLAB] thumb CLICK path={self._path} exists={self._path.exists()}")
            self.clicked.emit(self._path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):  # noqa: N802 - Qt override
        self.overlay.setVisible(True)
        self.overlay.raise_()
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
        # 2026-07-03: картинка — прямоугольник; клип по скруглённой рамке
        # контейнера-миниатюры (radius 8 у #camera-result-thumb-wrap) там,
        # где картинка дотягивается до углов бокса.
        self.image.setPixmap(_pixmap_clipped_to_box(
            scaled, target.width(), target.height(), 8))



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
        self._loaded_slug: Optional[str] = None   # чей state загружен (per-show)
        self._big_path: Optional[Path] = None   # что показано в большом окне
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
        # 2026-07-03 (фикс SIGABRT «QThread destroyed while running»):
        # на выходе приложения дожидаемся живых тредов — иначе Py_FinalizeEx
        # рушит процесс, у Alex всплывало окно ошибки Python на каждый
        # teardown offscreen-смоков.
        try:
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.shutdown_threads)
        except Exception:
            pass
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
        root.addWidget(self.title_lbl)

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
        # подход B: пол высоты + большой stretch (окна делят место с лентой,
        # растут до потолка 16:9, сжимаются к полу на маленьком экране).
        self.current_slot.setMinimumHeight(self._FLOOR_MEDIA_H)
        left_lay.addWidget(self.current_slot, stretch=100)

        # 2026-07-02 (лейаут v2): под исходником — БОЛЬШОЕ окно результата
        # (последняя генерация, клик = попап-просмотрщик), под ним —
        # горизонтальная лента миниатюр всех результатов.
        self.result_title_lbl = QLabel()
        self.result_title_lbl.setObjectName("camera-section-title")
        left_lay.addWidget(self.result_title_lbl)

        # Большое окно РОВНО размера исходника: ширина общая (вся панель),
        # высота = та же адаптивная (_recalc_media_windows задаёт обоим один
        # maximumHeight, равный stretch → всегда равны). Лента ниже — остаток.
        self.big_result = ResultPreviewLabel()
        self.big_result.setObjectName("camera-result-big")
        self.big_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big_result.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
        self.big_result.setCursor(Qt.CursorShape.PointingHandCursor)
        # 2026-07-03: клик по большому окну → попап текущей показанной
        self.big_result.clicked.connect(self._open_big_popup)
        # hover-кнопки на большом окне (в папке / в буфер / удалить) —
        # действуют на ТЕКУЩУЮ показанную картинку
        self._big_btns = []
        for _icon, _tip_key, _handler in (
            ("folder-open", "camera_result_reveal", self._big_reveal),
            ("copy", "shot_copy", self._big_copy),
            ("trash-2-red", "camera_result_delete", self._big_delete),
        ):
            _b = QToolButton(self.big_result)
            _b.setObjectName("camera-thumb-overlay-btn"
                             if _icon != "trash-2-red"
                             else "camera-thumb-overlay-trash")
            _b.setIcon(_camera_icon(_icon))
            _b.setIconSize(QSize(14, 14))
            _b.setFixedSize(22, 22)
            _b.setCursor(Qt.CursorShape.PointingHandCursor)
            _b.setToolTip(tr(_tip_key))
            _b.clicked.connect(_handler)
            _b.setVisible(False)
            self._big_btns.append(_b)
        self.big_result.entered.connect(self._show_big_btns)
        self.big_result.left.connect(
            lambda: [b.setVisible(False) for b in self._big_btns])
        # подход B: пол высоты + большой stretch (равный со слотом → окна равны)
        self.big_result.setMinimumHeight(self._FLOOR_MEDIA_H)
        left_lay.addWidget(self.big_result, stretch=100)

        self.results_scroll = HWheelScrollArea()
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
        # 2026-07-03 (фикс разъехавшейся колонки): ленту НЕ прячем — она
        # единственный stretch-носитель; скрытая (setVisible(False))
        # выпадала из layout, stretch исчезал и QVBoxLayout растаскивал
        # блоки по колонке гигантскими зазорами. Пустая лента прозрачна.
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
        # 2026-07-03: верх зума ВЕРНУЛИ на 10.0 — fal-модель принимает 0..10
        # (HTTP 422 на >10, подтверждено схемой OpenAPI: min 0 / max 10 / def 5).
        # UI-семантика: 0 = далеко (общий план), 10 = вплотную (крупно) — в fal
        # уходит ИНВЕРСИЯ (10 − ui), см. _run_generation.
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

        # 2026-07-03: углы генерации выбранного результата (из манифеста)
        self.angles_info_lbl = QLabel()
        self.angles_info_lbl.setObjectName("camera-generation-status")
        self.angles_info_lbl.setVisible(False)
        controls_lay.addWidget(self.angles_info_lbl)

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
        """Крестик на исходнике: слот → «перетащи кадр», сфера без кадра,
        камера/ползунки — в дефолт (как при загрузке нового кадра). Результаты
        не трогаем. 2026-07-03: reset висел только на drop/paste — добавлен и
        сюда (иначе после удаления исходника оставались старые углы/глобус)."""
        self._current_ref = None
        self.current_slot.clear_image()
        self.orbit.set_frame_image(None)
        self._reset_camera_controls()   # h=0, v=0, zoom=5.0 + камера по центру
        QTimer.singleShot(0, self._update_big_result)   # высота слота изменилась
        self._save_state()

    def _add_references(self, slot_type: str, paths: List[Path]) -> None:
        """Оставлен только основной кадр (Current shot); референсы убраны
        вместе со старым промпт-путём (2026-07-02, fal)."""
        for path in paths:
            if slot_type != "Current shot":
                continue
            # 2026-07-03: диагностика drop-пути в runtime.log — путь/exists/
            # size/st_dev оригинала (междисковый drop) и результат копии.
            try:
                _st = path.stat()
                print(f"[CAMLAB] drop: src={path} exists={path.exists()} "
                      f"size={_st.st_size} st_dev={_st.st_dev}")
            except Exception as _e:
                print(f"[CAMLAB] drop: src={path} exists={path.exists()} stat_err={_e}")
            current_path = self._copy_current_shot_to_camera_folder(path)
            print(f"[CAMLAB] drop: copied -> {current_path} "
                  f"exists={Path(current_path).exists()}")
            self._current_ref = CameraReference(path=current_path, ref_type=slot_type)
            self.current_slot.set_image(current_path)
            self.orbit.set_frame_image(current_path)
            self._reset_camera_controls()
            QTimer.singleShot(0, self._update_big_result)   # аспект слота сменился
        self._save_state()

    def _copy_current_shot_to_camera_folder(self, path: Path) -> Path:
        show_slug = self._current_show_slug()
        if not show_slug:
            print(f"[CAMLAB] copy: нет активного шоу — источник остаётся вне проекта: {path}")
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
            shutil.copy2(path, dest)   # кроссдиск ок (shutil читает+пишет байты)
            try:
                print(f"[CAMLAB] copy: {path} -> {dest} ok size={dest.stat().st_size}")
            except Exception:
                pass
            return dest
        except Exception as exc:
            # 2026-07-03: раньше except молчал — если копия падала (права/диск),
            # _current_ref указывал на оригинал вне проекта, а причина терялась.
            print(f"[CAMLAB] copy FAILED {path} -> {out_dir}: {exc} "
                  f"(источник остаётся вне проекта)")
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
            # тот же пересинк высоты/скейла, что при drop (без него кадр
            # вставал мелким до ухода-возврата на вкладку)
            QTimer.singleShot(0, self._update_big_result)
            self._set_generation_status(tr("camera_pasted_from_clipboard"))
            self._save_state()
        except Exception as exc:
            self._set_generation_status(f"{tr('camera_generation_error')}\n{exc}")

    def _open_image_popup(self, path: Path) -> None:
        # 2026-07-03: guard тоже через стем-резолв (Hazel png→jpg) — мёртвый
        # путь раньше тихо ронял открытие попапа «Источника».
        healed = resolve_existing_path(path) if path else None
        if not healed:
            return
        path = Path(healed)
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
        каждой генерации. Один тред за раз, СВЕЖИЙ на каждый показ.
        2026-07-03: при ошибке (alpha-эндпоинт fal мигает) — ОДИН авто-ретрай
        через ~1.5с, и только если он тоже упал — «$ —». Раньше первая же
        осечка сети давала пустой баланс до следующего показа вкладки."""
        self._start_balance_thread(allow_retry=True)

    def _start_balance_thread(self, allow_retry: bool) -> None:
        if self._balance_thread is not None and self._balance_thread.isRunning():
            return
        thread = FalBalanceThread(self)
        thread.balance.connect(lambda v: self._set_balance_text(v))
        thread.error.connect(
            lambda _msg, retry=allow_retry: self._on_balance_error(retry))
        self._balance_thread = thread
        thread.start()

    def _on_balance_error(self, allow_retry: bool) -> None:
        """Ошибка баланса: один авто-ретрай (alpha fal мигает), потом «$ —».
        Лейбл при первой осечке НЕ трогаем — если ретрай успеет, пустого
        баланса юзер даже не увидит."""
        if allow_retry:
            QTimer.singleShot(
                1500, lambda: self._start_balance_thread(allow_retry=False))
            return
        self._set_balance_text(None)

    def shutdown_threads(self, wait_ms: int = 8000) -> None:
        """Остановить/дождаться FalBalanceThread и все FalAnglesThread ДО
        разрушения виджетов. Зовётся из closeEvent, MainWindow.closeEvent,
        aboutToQuit (и смоков). Не кидает. 2026-07-03: сперва .stop() (флаг
        отмены), потом wait() — GET (timeout 6с) вернётся сам, wait его
        дождётся чисто, без рискованного terminate()."""
        try:
            bt = self._balance_thread
            if bt is not None and bt.isRunning():
                if hasattr(bt, "stop"):
                    bt.stop()          # флаг: не тронет UI после отмены
                bt.wait(wait_ms)       # GET timeout=6с — дождёмся завершения
        except Exception:
            pass
        try:
            for job in list(self._generation_jobs.values()):
                th = job.thread
                if th is not None and th.isRunning():
                    th.stop()          # кооперативный — выйдет на poll-шаге
                    th.wait(wait_ms)
        except Exception:
            pass

    def closeEvent(self, event):  # noqa: N802 - Qt override
        # 2026-07-03: закрытие вкладки/окна — гасим сетевые треды ДО разрушения
        # виджетов (живой FalBalanceThread на GET иначе → SIGABRT на выходе).
        # Тройная страховка: здесь + MainWindow.closeEvent + aboutToQuit.
        self.shutdown_threads()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        self._refresh_fal_balance()
        # 2026-07-03 (п.7): state per-сериал — при смене активного сериала
        # (или первом показе после старта) перечитываем state ЭТОГО сериала.
        slug = self._current_show_slug()
        if slug != self._loaded_slug:
            print(f"[CAMLAB] showEvent: reload state "
                  f"{self._loaded_slug!r} -> {slug!r}")
            self._load_state()
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
        # 2026-07-03: блокируем ТОЛЬКО пока реально идёт генерация (живой тред).
        # Залипшие завершённые job'ы (если по какой-то причине не снялись в
        # _on_generation_finished) НЕ должны навсегда запирать новый Generate
        # при наличии прошлого результата — чистим их и продолжаем. Новый
        # Generate заменяет прошлый результат в большом окне.
        if self._generation_jobs:
            if any(j.thread is not None and j.thread.isRunning()
                   for j in self._generation_jobs.values()):
                print("[CAMLAB] run_gen: живая генерация уже идёт — пропускаю клик")
                return
            print(f"[CAMLAB] run_gen: чищу залипшие job'ы "
                  f"({len(self._generation_jobs)}), продолжаю")
            self._generation_jobs.clear()
        self._last_generation_status = ""
        if not self._current_ref:
            self._set_generation_status(tr("camera_need_current"))
            return
        # 2026-07-03: вход генерации ЛЕЧИМ стем-резолвом. Hazel-вотчер конвертит
        # копию source_*.png → .jpg и удаляет .png; _current_ref.path остаётся
        # мёртвым .png (pixmap виден из RAM), а FalAnglesThread.run падал на
        # `not image_path.exists()` → «Drop the current shot first» при ВИДИМОМ
        # кадре. resolve_existing_path догоняет png→jpg (как у ленты/big).
        healed_src = resolve_existing_path(self._current_ref.path)
        print(f"[CAMLAB] run_gen: current_ref set={self._current_ref is not None} "
              f"raw={self._current_ref.path} exists={self._current_ref.path.exists()} "
              f"healed={healed_src}")
        if not healed_src:
            self._set_generation_status(tr("camera_need_current"))
            return
        healed_src = Path(healed_src)
        if healed_src != self._current_ref.path:
            self._current_ref = CameraReference(
                path=healed_src, ref_type=self._current_ref.ref_type)
            self._save_state()   # догоняем png→jpg и в state
        # 2026-07-03: защита от iCloud-evicted (dataless) кадра — файл ЕСТЬ, но
        # данные выгружены в облако; FalAnglesThread прочитал бы пусто/упал, а
        # юзер видел невнятное «Drop the current shot first». Даём внятную ошибку.
        if not _file_data_available(healed_src):
            print(f"[CAMLAB] run_gen: DATALESS source {healed_src} "
                  f"(iCloud-evicted) — генерация заблокирована")
            self._set_generation_status(tr("camera_source_dataless"), is_error=True)
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
        # 2026-07-03 (семантика зума ЗАФИКСИРОВАНА): ползунок 0 = МАКСИМАЛЬНО
        # БЛИЗКО (крупный план), 10 = МАКСИМАЛЬНО ДАЛЕКО (общий план).
        # По ДВУМ независимым фактам генераций Alex близкий конец модели =
        # fal 0 (старые генерации: fal 0 → крупный портрет; новый скрин:
        # fal 10 → «как оригинал», не close-up) — description схемы OpenAPI
        # («10=close-up») практикой не подтверждается. Маппинг ПРЯМОЙ:
        # fal_zoom = ui — ползунок 0 шлёт fal 0 (максимальный close-up,
        # который модель реально умеет), 10 → fal 10 (максимально далеко).
        fal_zoom = max(0.0, min(10.0, zoom))
        try:
            _, _, _r = self.orbit._sphere_geometry()
            _cd = self.orbit._cam_dist(_r)
        except Exception:
            _cd = -1.0
        print(f"[CAMLAB] run_gen: ui_zoom={zoom:.1f} fal_zoom={fal_zoom:.1f} "
              f"cam_dist={_cd:.1f}px")
        self.generate_btn.setEnabled(False)
        self._generation_run_id += 1
        run_id = self._generation_run_id
        thread = FalAnglesThread(
            image_path=self._current_ref.path,
            horizontal_angle=h_deg,
            vertical_angle=v_deg,
            zoom=fal_zoom,
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
                "last_result": (
                    str(self._current_big_path())
                    if self._current_big_path() is not None else None
                ),
            }
            state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_state(self) -> None:
        self._loading_state = True
        self._loaded_slug = self._current_show_slug()
        try:
            state_path = self._state_path()
            data = {}
            if state_path is not None and state_path.exists():
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            print(f"[CAMLAB] load_state slug={self._loaded_slug!r} "
                  f"file={state_path} keys={sorted(data.keys())}")

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
            # 2026-07-03: лечим стем-резолвом (вотчер png→jpg) — иначе после
            # конвертации Hazel сохранённый .png-путь не existed и слот
            # восстанавливался ПУСТЫМ, хотя .jpg на диске лежит.
            healed_cur = resolve_existing_path(current_path) if current_path else None
            if healed_cur:
                current_path = Path(healed_cur)
                self._current_ref = CameraReference(
                    path=current_path,
                    ref_type=str(current_data.get("type") or "Current shot"),
                )
                self.current_slot.set_image(current_path)
                self.orbit.set_frame_image(current_path)

            self._remove_layout_widgets(self.results_strip_lay, CameraResultThumb)
            result_items = data.get("results") if isinstance(data.get("results"), list) else []
            # 2026-07-03 (фикс пустого big): результаты живут НЕЗАВИСИМО от
            # исходника (раньше guard current_ref ронял и ленту, и big).
            result_paths = [
                Path(str(item.get("path")))
                for item in result_items
                if isinstance(item, dict) and item.get("path")
            ]
            for path in result_paths:
                healed = resolve_existing_path(path)   # вотчер png→jpg
                if healed:
                    self._show_result_preview(Path(healed))
                    self._last_result_path = Path(healed)
            last = resolve_existing_path(data.get("last_result"))
            if last:
                self._big_path = Path(last)
                self._last_result_path = Path(last)
            print(f"[CAMLAB] load_state: results={len(result_paths)} "
                  f"restored={self._result_thumb_count()} "
                  f"current={'да' if self._current_ref else 'нет'} "
                  f"big={self._current_big_path()}")

            self.generate_btn.setEnabled(not bool(self._generation_jobs))
            self._set_generation_status("")
            self._update_big_result()
            self._update_angles_info()
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
        self._big_path = path          # свежий результат — в большое окно
        self._show_result_preview(path)
        self._update_angles_info()
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
        thumb.clicked.connect(self._set_big_from)      # клик = показать в big
        thumb.revealRequested.connect(self._reveal_result)
        thumb.copyRequested.connect(self._copy_result_to_shot_clipboard)
        thumb.deleteRequested.connect(lambda path, widget=thumb: self._delete_result(path, widget))
        self.results_strip_lay.insertWidget(self.results_strip_lay.count() - 1, thumb)
        self._update_big_result()
        self._save_state()

    def _current_big_path(self) -> Optional[Path]:
        """Показанная в большом окне картинка: явная → последняя → новейшая
        миниатюра. Каждый кандидат лечится стем-резолвом (вотчер png→jpg)."""
        for cand in (self._big_path, self._last_result_path,
                     self._newest_result_path()):
            healed = resolve_existing_path(cand)
            if healed:
                return Path(healed)
        return None

    def _set_big_from(self, path: Path) -> None:
        """Клик по миниатюре: показать её в большом окне + углы генерации."""
        healed = resolve_existing_path(path)
        self._big_path = Path(healed) if healed else Path(path)
        print(f"[CAMLAB] set_big_from raw={path} healed={healed} "
              f"exists={self._big_path.exists()}")
        self._update_big_result()
        self._update_angles_info()
        self._save_state()

    def _open_big_popup(self) -> None:
        path = self._current_big_path()
        print(f"[CAMLAB] big CLICK -> popup path={path}")
        if path is not None:
            self._open_image_popup(path)

    def _show_big_btns(self) -> None:
        if self._current_big_path() is None:
            return
        x = self.big_result.width() - 4
        for b in reversed(self._big_btns):
            x -= b.width() + 4
            b.move(x, 4)
            b.setVisible(True)
            b.raise_()

    def _big_reveal(self) -> None:
        path = self._current_big_path()
        if path is not None:
            self._reveal_result(path)

    def _big_copy(self) -> None:
        path = self._current_big_path()
        if path is not None:
            self._copy_result_to_shot_clipboard(path)

    def _big_delete(self) -> None:
        path = self._current_big_path()
        if path is not None:
            self._delete_result(path)

    def _angles_text_for(self, path: Optional[Path]) -> str:
        """Углы генерации картинки из manifest.json (запись angles)."""
        if path is None:
            return ""
        show_slug = self._current_show_slug()
        if not show_slug:
            return ""
        show_root = self._project_root / "shows" / show_slug
        manifest_path = show_root / "camera_lab" / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            try:
                rel = str(Path(path).relative_to(show_root))
            except Exception:
                rel = str(path)
            rel_stem = Path(rel).with_suffix("").as_posix()
            for item in reversed(data if isinstance(data, list) else []):
                # матч по стему: вотчер меняет расширение файла, запись
                # манифеста остаётся со старым (.png) — стем стабилен
                if Path(str(item.get("output", ""))).with_suffix("").as_posix() == rel_stem:
                    a = item.get("angles") or {}
                    if not a:
                        print(f"[CAMLAB] angles: запись без angles rel={rel}")
                        return ""
                    return (f"{int(a.get('horizontal_deg', 0))}° / "
                            f"{int(a.get('vertical_deg', 0)):+d}° / "
                            f"{float(a.get('zoom', 5.0)):.1f}")
            print(f"[CAMLAB] angles: НЕТ записи в манифесте rel={rel} "
                  f"(записей={len(data) if isinstance(data, list) else 0})")
        except Exception as exc:
            print(f"[CAMLAB] angles: EXC {exc}")
        return ""

    def _update_angles_info(self) -> None:
        lbl = getattr(self, "angles_info_lbl", None)
        if lbl is None:
            return
        text = self._angles_text_for(self._current_big_path())
        lbl.setText(f"{tr('camera_angles_info')} {text}" if text else "")
        lbl.setVisible(bool(text))

    # 2026-07-03: пол высоты окон «Источник»/«Результат» на маленьком экране —
    # окна СЖИМАЮТСЯ к нему, а не форсят гигантский минимум окна (setFixedHeight
    # форсил минимум окна ~990px → не влезало в 14"). Потолок адаптивный, ниже.
    _FLOOR_MEDIA_H = 150

    def _recalc_media_windows(self) -> None:
        """Адаптивная высота окон «Источник»/«Результат» (подход B). Высоту НЕ
        фиксируем — ставим только ПОТОЛОК maximumHeight = col_w*9/16 (форма 16:9);
        пол minimumHeight=_FLOOR, Expanding и равный stretch заданы в _build_ui.
        Высоту делит layout: окна РАСТУТ до потолка на большом окне и СЖИМАЮТСЯ
        к полу на маленьком, всегда равны (равный stretch), ширину тянут от
        колонки. НЕ зависит от аспекта кадра (потолок от ширины, не от картинки).
        Идея как у storyboard_app._recalc_shot_cards_size — размер от реального
        размера окна, а не от контента."""
        slot = getattr(self, "current_slot", None)
        big = getattr(self, "big_result", None)
        if slot is None or big is None:
            return
        col_w = slot.width()
        if col_w <= 0:
            return
        cap = int(col_w * 9 / 16)                 # потолок формы 16:9 (от ширины)
        # доступная высота колонки под ДВА окна (реальные соседи + резерв ленты),
        # всё меряем, не хардкодим. left.height() честна: max НЕ форсит минимум
        # окна (в отличие от setFixedHeight) → обратной связи нет.
        h_by_height = cap
        left = slot.parentWidget()
        lay = left.layout() if left is not None else None
        if left is not None and lay is not None:
            m = lay.contentsMargins()
            avail_v = (left.height() - m.top() - m.bottom()
                       - self.source_title_lbl.height()
                       - self.result_title_lbl.height()
                       - self.results_scroll.minimumHeight()   # резерв ленты
                       - lay.spacing() * 4)                     # 4 зазора колонки
            if avail_v > 0:
                h_by_height = avail_v // 2                       # два окна делят
        # ОДИН и тот же потолок обоим → оба упираются ровно в него → РАВНЫ
        # (остаток-парити уходит в ленту, а не одному из окон).
        cap_h = max(self._FLOOR_MEDIA_H, min(cap, h_by_height))
        for w in (slot, big):
            if w.maximumHeight() != cap_h:
                w.setMaximumHeight(cap_h)

    def _update_big_result(self) -> None:
        """Большое окно результата: последняя генерация, contain под фикс-окно;
        нет результатов → пустой placeholder. Высоту обоих окон синкает
        _recalc_media_windows (адаптив от размера окна, не от аспекта)."""
        big = getattr(self, "big_result", None)
        if big is None:
            return
        self._recalc_media_windows()   # обе окна → одна адаптивная высота
        path = self._current_big_path()
        if path is None:
            big.setPixmap(QPixmap())
            big.setText("")   # без центр-подписи — заголовок слева
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            print(f"[CAMLAB] update_big: pixmap NULL path={path}")
            big.setText("")
            return
        big.setText("")
        # contain: вписываем в фикс-окно (минус рамка 1px×2), KeepAspectRatio →
        # касается 2 краёв, по 2 другим — поля фона; форма окна не меняется.
        target_w = max(64, big.width() - 2)
        target_h = max(64, big.height() - 2)
        scaled = pixmap.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 2026-07-03: картинка — прямоугольник; клип по скруглённой рамке
        # окна «Результат» (radius 8) только где дотягивается до углов.
        big.setPixmap(_pixmap_clipped_to_box(scaled, target_w, target_h, 8))
        print(f"[CAMLAB] update_big OK path={path} scaled={scaled.width()}x{scaled.height()}")

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_big_result()   # пере-scale большого превью под новый размер

    def _copy_result_to_shot_clipboard(self, path: Path) -> None:
        if self._set_shot_clipboard is None:
            return
        healed = resolve_existing_path(path)
        print(f"[CAMLAB] copy handler raw={path} healed={healed}")
        if not healed:
            return
        path = Path(healed)
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
        healed = resolve_existing_path(path)
        print(f"[CAMLAB] reveal handler raw={path} healed={healed}")
        if not healed:
            return
        path = Path(healed)
        try:
            from storyboard_app import reveal_in_file_manager

            reveal_in_file_manager(path)
            print("[CAMLAB] reveal OK")
        except Exception as exc:
            print(f"[CAMLAB] reveal EXC {exc}")

    def _delete_result(self, path: Optional[Path] = None, widget: Optional[QWidget] = None) -> None:
        raw = path or self._last_result_path
        healed = resolve_existing_path(raw)
        print(f"[CAMLAB] delete handler raw={raw} healed={healed}")
        if not healed:
            return
        # widget ищем по СЫРОМУ пути (thumb хранит старое имя), удаляем — healed
        if widget is None and raw is not None:
            widget = self._find_result_thumb(Path(raw))
        path = Path(healed)
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
            print(f"[CAMLAB] delete OK path={path}")
            self._remove_from_manifest(path)
        except Exception as exc:
            self._set_generation_status(f"{tr('camera_generation_error')}\n{exc}")
            return
        if self._last_result_path == path:
            self._last_result_path = self._newest_result_path(excluding=path)
        if self._big_path == path:
            self._big_path = None      # fallback-цепочка возьмёт новейшую
        if widget is None:
            widget = self._find_result_thumb(path)
        if widget is not None:
            self.results_strip_lay.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        has_results = self._result_thumb_count() > 0
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
            /* 2026-07-03: рамку убрали — QSS-бордер уменьшал contentsRect
               обёртки и выпихивал фикс-лейбл на (1,1); его низ/право
               свисали за край и клипались → нижние углы теряли скругление
               (верх ~6px, низ ~2px на retina). Скругление даёт сама
               пиксмапа (radius 7, симметрична), как у #camera-ref-thumb-wrap. */
            border: none;
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
