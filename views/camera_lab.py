# -*- coding: utf-8 -*-
"""
camera_lab — isolated admin-only prototype for changing camera angle.

This view deliberately does not touch the editor, shot viewer, or existing
storyboard generation flow. The first version builds the UI, reference intake,
camera controls, and prompt mapping; the generation hook is kept as a stub.
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
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from i18n import tr
from generator.generator_thread import GeneratorImageThread


REF_TYPES = ("Current shot", "Character", "Location", "Object")
PROVIDER_NARWHAL = "narwhal"
PROVIDER_OPENAI = "openai"
MODEL_NANO_BANANA_2 = "nano-banana-2"
MODEL_NANO_BANANA_2_FLOWER = "flower-image"
MODEL_OPENAI_IMAGE = "openai-image"
CAMERA_GENERATION_BATCH_SIZE = 2
CAMERA_MODEL_OPTIONS = (
    (MODEL_NANO_BANANA_2, "Nano Banana 2"),
    (MODEL_NANO_BANANA_2_FLOWER, "Nano Banana 2 Flower"),
    (MODEL_OPENAI_IMAGE, "OpenAI"),
)
REF_TYPE_LABEL_KEYS = {
    "Current shot": "camera_ref_current",
    "Character": "camera_ref_character",
    "Location": "camera_ref_location",
    "Object": "camera_ref_object",
}


@dataclass
class CameraReference:
    path: Path
    ref_type: str


@dataclass
class CameraGenerationJob:
    thread: GeneratorImageThread
    prompt: str
    refs: List[Path]
    ref_types: List[str]
    provider: str
    model_id: str
    model_name: str
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


class CameraModelToggle(QWidget):
    valueChanged = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._options = list(CAMERA_MODEL_OPTIONS)
        self._value = MODEL_NANO_BANANA_2
        self.setMinimumHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if value not in {item[0] for item in self._options}:
            value = MODEL_NANO_BANANA_2
        if self._value == value:
            self.update()
            return
        self._value = value
        self.update()
        self.valueChanged.emit(value)

    def set_labels(self, labels: List[str]) -> None:
        if len(labels) != len(self._options):
            return
        self._options = [(value, labels[i]) for i, (value, _label) in enumerate(self._options)]
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self._options:
            super().mouseReleaseEvent(event)
            return
        rect = self.rect().adjusted(1, 1, -1, -1)
        index = int((event.position().x() - rect.left()) / max(1, rect.width()) * len(self._options))
        index = max(0, min(len(self._options) - 1, index))
        self.set_value(self._options[index][0])
        event.accept()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 8

        painter.setPen(QPen(QColor("#1d1e20"), 1))
        painter.setBrush(QColor("#131516"))
        painter.drawRoundedRect(rect, radius, radius)

        count = max(1, len(self._options))
        segment_w = rect.width() / count
        selected_index = next(
            (i for i, (value, _label) in enumerate(self._options) if value == self._value),
            0,
        )
        active_rect = QRectF(
            rect.left() + selected_index * segment_w + 4,
            rect.top() + 4,
            segment_w - 8,
            rect.height() - 8,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#303335"))
        painter.drawRoundedRect(active_rect, 6, 6)

        font = QFont()
        font.setPointSize(10)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        for i, (_value, label) in enumerate(self._options):
            text_rect = QRectF(
                rect.left() + i * segment_w + 6,
                rect.top(),
                segment_w - 12,
                rect.height(),
            )
            painter.setPen(QColor("#f1e0ac") if i == selected_index else QColor("#b8b8b8"))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, label)


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
    """Interactive perspective preview: drag the frame to set camera angles."""

    valuesChanged = pyqtSignal(int, int, int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rotate = 0
        self._vertical = 0
        self._zoom = 100
        self._frame_aspect = 16 / 9
        self._frame_pixmap: Optional[QPixmap] = None
        self._drag_start: Optional[QPointF] = None
        self._drag_rotate = 0
        self._drag_vertical = 0
        self.setMinimumSize(260, 300)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, rotate: int, vertical: int, zoom_percent: int) -> None:
        self._rotate = max(-90, min(90, int(rotate)))
        self._vertical = max(-90, min(90, int(vertical)))
        self._zoom = max(50, min(200, int(zoom_percent)))
        self.update()

    def set_frame_aspect(self, aspect: float) -> None:
        if aspect > 0:
            self._frame_aspect = aspect
            self.update()

    def set_frame_image(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        self._frame_pixmap = pixmap if not pixmap.isNull() else None
        if self._frame_pixmap is not None:
            self._frame_aspect = max(
                0.1,
                self._frame_pixmap.width() / max(1, self._frame_pixmap.height()),
            )
        self.update()

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            self._drag_rotate = self._rotate
            self._drag_vertical = self._vertical
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if self._drag_start is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._drag_start
        self._rotate = max(-90, min(90, int(round(self._drag_rotate + delta.x() * 0.55))))
        self._vertical = max(-90, min(90, int(round(self._drag_vertical - delta.y() * 0.48))))
        self.update()
        self.valuesChanged.emit(self._rotate, self._vertical, self._zoom)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._drag_start = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - Qt override
        step = 5 if event.angleDelta().y() > 0 else -5
        self._zoom = max(50, min(200, self._zoom + step))
        self.update()
        self.valuesChanged.emit(self._rotate, self._vertical, self._zoom)
        event.accept()

    @staticmethod
    def _project_card(
        rect: QRectF,
        rotate: int,
        vertical: int,
        zoom_percent: int,
        aspect: float,
    ) -> QPolygonF:
        max_w = min(rect.width() * 0.72, 260.0)
        max_h = min(rect.height() * 0.58, 168.0)
        aspect = max(0.1, min(6.0, aspect))
        if max_w / max(1.0, max_h) > aspect:
            card_h = max_h
            card_w = card_h * aspect
        else:
            card_w = max_w
            card_h = card_w / aspect

        zoom_scale = 0.55 + (max(50, min(200, zoom_percent)) - 50) / 150 * 0.90
        card_w *= zoom_scale
        card_h *= zoom_scale

        yaw = math.radians(max(-90, min(90, rotate)) / 90 * 88)
        pitch = math.radians(max(-90, min(90, vertical)) / 90 * 82)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        distance = 620.0
        center = QPointF(rect.center().x(), rect.center().y() + 8)

        points = []
        for x, y in (
            (-card_w / 2, -card_h / 2),
            (card_w / 2, -card_h / 2),
            (card_w / 2, card_h / 2),
            (-card_w / 2, card_h / 2),
        ):
            x1 = x * cy
            z1 = -x * sy
            y2 = y * cp - z1 * sp
            z2 = y * sp + z1 * cp
            factor = distance / max(120.0, distance - z2)
            points.append(QPointF(center.x() + x1 * factor, center.y() + y2 * factor))
        return QPolygonF(points)

    def paintEvent(self, event):  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(12, 10, -12, -10)

        painter.setPen(QPen(QColor("#1d1e20"), 1))
        painter.setBrush(QColor("#090a0a"))
        painter.drawRoundedRect(rect, 8, 8)

        painter.save()
        scene_clip = QPainterPath()
        scene_clip.addRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 7, 7)
        painter.setClipPath(scene_clip)

        card_poly = self._project_card(
            rect,
            self._rotate,
            self._vertical,
            self._zoom,
            self._frame_aspect,
        )
        shadow = QPolygonF([QPointF(point.x(), point.y() + 8) for point in card_poly])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 96))
        painter.drawPolygon(shadow)

        if self._frame_pixmap is not None:
            source_rect = QRectF(0, 0, self._frame_pixmap.width(), self._frame_pixmap.height())
            source_poly = QPolygonF([
                source_rect.topLeft(),
                source_rect.topRight(),
                source_rect.bottomRight(),
                source_rect.bottomLeft(),
            ])
            transform = QTransform()
            if QTransform.quadToQuad(source_poly, card_poly, transform):
                painter.save()
                card_clip = QPainterPath()
                card_clip.addPolygon(card_poly)
                painter.setClipPath(card_clip)
                painter.setTransform(transform, True)
                painter.drawPixmap(0, 0, self._frame_pixmap)
                painter.restore()
        else:
            painter.setBrush(QColor(26, 29, 31))
            painter.drawPolygon(card_poly)

        painter.setPen(QPen(QColor(255, 255, 255, 86), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(card_poly)
        painter.restore()

        painter.setPen(QColor(184, 184, 184))
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(12, 10, -12, -8),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            f"{tr('camera_horizontal')}: {self._rotate:+d}   "
            f"{tr('camera_vertical')}: {self._vertical:+d}   "
            f"{tr('camera_zoom')}: {self._zoom / 100:.2f}",
        )


class ImageDropSlot(QFrame):
    filesDropped = pyqtSignal(str, list)
    imageClicked = pyqtSignal(Path)
    pasteRequested = pyqtSignal()

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
        else:
            self.paste_btn = None
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

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self.paste_btn is not None:
            self.paste_btn.move(max(8, self.width() - self.paste_btn.width() - 12), 12)
        self._refresh_pixmap()

    def enterEvent(self, event):  # noqa: N802 - Qt override
        if self.paste_btn is not None:
            self.paste_btn.setVisible(self._paste_available)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        if self.paste_btn is not None:
            self.paste_btn.setVisible(False)
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


class ReferenceDropArea(QFrame):
    filesDropped = pyqtSignal(str, list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("camera-ref-drop-area")
        self.setMinimumHeight(300)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(6)

        self.title = QLabel()
        self.title.setObjectName("camera-ref-drop-title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)

        self.hint = QLabel()
        self.hint.setObjectName("camera-ref-drop-hint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)

        lay.addStretch()
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lay.addStretch()
        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(tr("camera_refs_drop"))
        self.hint.setText(tr("camera_refs_drop_hint"))

    def dragEnterEvent(self, event):  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
            if any(ImageDropSlot._is_image_path(path) for path in paths):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):  # noqa: N802 - Qt override
        paths = [
            Path(u.toLocalFile())
            for u in event.mimeData().urls()
            if ImageDropSlot._is_image_path(Path(u.toLocalFile()))
        ]
        if paths:
            self.filesDropped.emit("Reference", paths)
            event.acceptProposedAction()
            return
        event.ignore()


class CameraResultArea(QFrame):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("camera-result-area")
        self.setMinimumHeight(260)


class FlowLayout(QLayout):
    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0, spacing: int = 10):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt override
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802 - Qt override
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802 - Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):  # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):  # noqa: N802 - Qt override
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802 - Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if line_height > 0 and next_x - spacing > effective.right() + 1:
                x = effective.x()
                y += line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


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


class CameraRefThumb(QFrame):
    clicked = pyqtSignal(Path)
    deleteRequested = pyqtSignal(Path)

    def __init__(self, ref: CameraReference, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._ref = ref
        self._pixmap = QPixmap(str(ref.path))
        self.setObjectName("camera-ref-thumb")
        self.setToolTip(str(ref.path))
        self.setFixedWidth(112)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        self.image_wrap = QFrame()
        self.image_wrap.setObjectName("camera-ref-thumb-wrap")
        self.image_wrap.setFixedSize(104, 82)
        image_lay = QGridLayout(self.image_wrap)
        image_lay.setContentsMargins(0, 0, 0, 0)
        image_lay.setSpacing(0)

        self.image = QLabel()
        self.image.setObjectName("camera-thumb-image")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(104, 82)
        self.image.setCursor(Qt.CursorShape.PointingHandCursor)
        image_lay.addWidget(self.image, 0, 0)

        self.overlay = QWidget()
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.overlay.setVisible(False)
        overlay_lay = QHBoxLayout(self.overlay)
        overlay_lay.setContentsMargins(4, 4, 4, 4)
        overlay_lay.setSpacing(0)
        overlay_lay.addStretch()

        self.delete_btn = QToolButton()
        self.delete_btn.setObjectName("camera-thumb-overlay-trash")
        self.delete_btn.setIcon(_camera_icon("trash-2-red"))
        self.delete_btn.setIconSize(QSize(14, 14))
        self.delete_btn.setFixedSize(22, 22)
        self.delete_btn.setToolTip(tr("camera_ref_remove"))
        self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self._ref.path))
        overlay_lay.addWidget(self.delete_btn)
        overlay_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        image_lay.addWidget(self.overlay, 0, 0)

        self.name = QLabel(ref.path.name)
        self.name.setObjectName("camera-ref-name")
        self.name.setToolTip(str(ref.path))
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setWordWrap(True)

        lay.addWidget(self.image_wrap)
        lay.addWidget(self.name)
        self._refresh_pixmap()

    def path(self) -> Path:
        return self._ref.path

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._ref.path)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):  # noqa: N802 - Qt override
        self.overlay.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        self.overlay.setVisible(False)
        super().leaveEvent(event)

    def _refresh_pixmap(self) -> None:
        if self._pixmap.isNull():
            return
        target = self.image.size()
        scaled = self._pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image.setPixmap(scaled)


class CameraLabView(QWidget):
    """Admin-only prototype tab for Krea-like camera angle experiments."""

    def __init__(
        self,
        project_root: Path,
        parent: Optional[QWidget] = None,
        default_provider: str = PROVIDER_NARWHAL,
        provider_changed: Optional[Callable[[str], None]] = None,
        get_shot_clipboard: Optional[Callable[[], Optional[bytes]]] = None,
        set_shot_clipboard: Optional[Callable[[bytes], None]] = None,
    ):
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._provider_changed = provider_changed
        self._get_shot_clipboard = get_shot_clipboard
        self._set_shot_clipboard = set_shot_clipboard
        self._provider = (
            default_provider
            if default_provider in (PROVIDER_NARWHAL, PROVIDER_OPENAI)
            else PROVIDER_NARWHAL
        )
        self._selected_model_id = (
            MODEL_OPENAI_IMAGE if self._provider == PROVIDER_OPENAI else MODEL_NANO_BANANA_2
        )
        self._current_ref: Optional[CameraReference] = None
        self._refs: List[CameraReference] = []
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

        self.current_slot = ImageDropSlot(
            "Current shot",
            "camera_drop_current",
            "camera_drop_current_hint",
            large=True,
        )
        self.current_slot.filesDropped.connect(self._add_references)
        self.current_slot.imageClicked.connect(self._open_image_popup)
        self.current_slot.pasteRequested.connect(self._paste_current_from_shot_clipboard)
        left_lay.addWidget(self.current_slot, stretch=1)

        self.refs_title_lbl = QLabel()
        self.refs_title_lbl.setObjectName("camera-section-title")
        left_lay.addWidget(self.refs_title_lbl)

        self.refs_drop_area = ReferenceDropArea()
        self.refs_drop_area.filesDropped.connect(self._add_references)

        self.refs_scroll = QScrollArea()
        self.refs_scroll.setObjectName("camera-refs-scroll")
        self.refs_scroll.setWidgetResizable(True)
        self.refs_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.refs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.refs_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.refs_strip = QWidget()
        self.refs_strip.setObjectName("camera-refs-strip")
        self.refs_strip_lay = QHBoxLayout(self.refs_strip)
        self.refs_strip_lay.setContentsMargins(0, 0, 0, 0)
        self.refs_strip_lay.setSpacing(10)
        self.refs_strip_lay.addStretch()
        self.refs_scroll.setWidget(self.refs_strip)
        self.refs_scroll.setVisible(False)
        self.refs_drop_area.layout().insertWidget(0, self.refs_scroll)

        refs_results_split = QFrame()
        refs_results_split.setObjectName("camera-refs-results-split")
        refs_results_lay = QVBoxLayout(refs_results_split)
        refs_results_lay.setContentsMargins(0, 0, 0, 0)
        refs_results_lay.setSpacing(12)
        refs_results_lay.addWidget(self.refs_drop_area, stretch=1)

        self.result_title_lbl = QLabel()
        self.result_title_lbl.setObjectName("camera-section-title")
        refs_results_lay.addWidget(self.result_title_lbl)

        self.result_area = CameraResultArea()
        self.result_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.result_area.setFixedHeight(258)
        result_area_lay = QVBoxLayout(self.result_area)
        result_area_lay.setContentsMargins(16, 16, 16, 16)
        result_area_lay.setSpacing(10)

        self.result_box = QPlainTextEdit()
        self.result_box.setObjectName("camera-result")
        self.result_box.setReadOnly(True)
        self.result_box.setMinimumHeight(62)
        self.result_box.setVisible(False)
        result_area_lay.addWidget(self.result_box, stretch=1)

        self.results_scroll = QScrollArea()
        self.results_scroll.setObjectName("camera-results-panel")
        self.results_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.results_strip = QWidget()
        self.results_strip.setObjectName("camera-results-strip")
        self.results_strip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.results_strip_lay = FlowLayout(self.results_strip, margin=0, spacing=10)
        self.results_strip_lay.setContentsMargins(0, 0, 0, 0)
        self.results_scroll.setWidget(self.results_strip)
        self.results_scroll.setVisible(False)
        result_area_lay.addWidget(self.results_scroll, stretch=1)

        refs_results_lay.addWidget(self.result_area, stretch=1)
        left_lay.addWidget(refs_results_split, stretch=1)
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

        self.rotate_slider = self._add_slider(controls_lay, "camera_horizontal", -90, 90, 0)
        self.vertical_slider = self._add_slider(controls_lay, "camera_vertical", -90, 90, 0)
        self.zoom_slider = self._add_slider(controls_lay, "camera_zoom", 50, 200, 100)

        self.orbit = CameraPerspectiveControl()
        self.orbit.valuesChanged.connect(self._on_preview_values_changed)
        controls_lay.addWidget(self.orbit)

        self.prompt_title_lbl = QLabel()
        self.prompt_title_lbl.setObjectName("camera-section-title")
        controls_lay.addWidget(self.prompt_title_lbl)

        self.prompt_box = QPlainTextEdit()
        self.prompt_box.setObjectName("camera-prompt-preview")
        self.prompt_box.setReadOnly(True)
        self.prompt_box.setMinimumHeight(150)
        controls_lay.addWidget(self.prompt_box)

        self.provider_label = QLabel()
        self.provider_label.setObjectName("camera-section-title")
        controls_lay.addWidget(self.provider_label)

        self.provider_toggle = CameraModelToggle(self)
        self.provider_toggle.set_value(self._selected_model_id)
        self.provider_toggle.valueChanged.connect(self._on_model_changed)
        controls_lay.addWidget(self.provider_toggle)

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
        if key == "camera_zoom":
            label.setText(f"{value / 100:.2f}")
        else:
            label.setText(f"{value:+d}")

    def _add_references(self, slot_type: str, paths: List[Path]) -> None:
        for path in paths:
            if slot_type == "Current shot":
                current_path = self._copy_current_shot_to_camera_folder(path)
                self._current_ref = CameraReference(path=current_path, ref_type=slot_type)
                self.current_slot.set_image(current_path)
                self.orbit.set_frame_image(current_path)
                self.orbit.set_frame_aspect(self.current_slot.aspect_ratio())
                self._reset_camera_controls()
                self._refresh_prompt_preview()
                continue
            ref_type = self._infer_reference_type(path) if slot_type == "Reference" else slot_type
            ref = CameraReference(path=path, ref_type=ref_type)
            self._refs.append(ref)
            self._append_ref_thumb(ref)
        self._refresh_prompt_preview()
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

    @staticmethod
    def _infer_reference_type(path: Path) -> str:
        name = path.stem.lower()
        object_markers = (
            "door", "gate", "key", "phone", "ring", "weapon", "gun", "knife",
            "book", "cup", "glass", "table", "chair", "object", "prop",
            "flesh_door",
        )
        location_markers = (
            "room", "hall", "corridor", "street", "kitchen", "bedroom",
            "office", "lobby", "garage", "location", "environment", "interior",
            "exterior",
        )
        if any(marker in name for marker in object_markers):
            return "Object"
        if any(marker in name for marker in location_markers):
            return "Location"
        return "Character"

    def _append_ref_thumb(self, ref: CameraReference) -> None:
        self.refs_scroll.setVisible(True)
        card = CameraRefThumb(ref)
        card.clicked.connect(self._open_image_popup)
        card.deleteRequested.connect(lambda path, widget=card: self._remove_reference(path, widget))
        self.refs_strip_lay.insertWidget(self.refs_strip_lay.count() - 1, card)

    def _remove_reference(self, path: Path, widget: Optional[QWidget] = None) -> None:
        self._refs = [ref for ref in self._refs if ref.path != path]
        if widget is None:
            for i in range(self.refs_strip_lay.count() - 1, -1, -1):
                item = self.refs_strip_lay.itemAt(i)
                candidate = item.widget() if item else None
                if isinstance(candidate, CameraRefThumb) and candidate.path() == path:
                    widget = candidate
                    break
        if widget is not None:
            self.refs_strip_lay.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self.refs_scroll.setVisible(bool(self._refs))
        if (
            self.generation_status_lbl.property("error")
            and self.generation_status_lbl.text() == tr("camera_flower_refs_error")
            and not self._refs
        ):
            self._set_generation_status("")
        self._refresh_prompt_preview()
        self._save_state()

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
        self.orbit.set_values(
            self.rotate_slider.value(),
            self.vertical_slider.value(),
            self.zoom_slider.value(),
        )
        if self._current_ref:
            self.orbit.set_frame_image(self._current_ref.path)
            self.orbit.set_frame_aspect(self.current_slot.aspect_ratio())
        self._refresh_prompt_preview()
        self._save_state()

    def _on_preview_values_changed(self, rotate: int, vertical: int, zoom: int) -> None:
        for slider, value in (
            (self.rotate_slider, rotate),
            (self.vertical_slider, vertical),
            (self.zoom_slider, zoom),
        ):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._refresh_slider_value_labels()
        self._refresh_prompt_preview()
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
        for slider, value in (
            (self.rotate_slider, 0),
            (self.vertical_slider, 0),
            (self.zoom_slider, 100),
        ):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._sync_control_state()

    def apply_lang(self) -> None:
        self.title_lbl.setText(tr("camera_title"))
        self.subtitle_lbl.setText(tr("camera_subtitle"))
        self.refs_title_lbl.setText(tr("camera_refs_title"))
        self.controls_title_lbl.setText(tr("camera_controls_title"))
        self.prompt_title_lbl.setText(tr("camera_prompt_preview"))
        self.prompt_box.setPlaceholderText(tr("camera_prompt_placeholder"))
        self.provider_label.setText(tr("camera_model_label"))
        self.provider_toggle.set_labels([
            "Nano Banana 2",
            "Nano Banana 2 Flower",
            "OpenAI",
        ])
        self.generate_btn.setText(f"{tr('camera_generate')} ×{CAMERA_GENERATION_BATCH_SIZE}")
        self.result_title_lbl.setText(tr("camera_result"))
        self.result_box.setPlaceholderText(tr("camera_result_placeholder"))
        for label in self._slider_labels:
            key = label.property("_i18n_key")
            if key:
                label.setText(tr(key))
        self.current_slot.retranslate()
        self.refs_drop_area.retranslate()
        self._refresh_prompt_preview()

    def _on_model_changed(self, value: str) -> None:
        valid_models = {item[0] for item in CAMERA_MODEL_OPTIONS}
        if value not in valid_models:
            value = MODEL_NANO_BANANA_2
        self._selected_model_id = value
        self._provider = PROVIDER_OPENAI if value == MODEL_OPENAI_IMAGE else PROVIDER_NARWHAL
        if self._provider_changed is not None:
            self._provider_changed(self._provider)
        self._refresh_prompt_preview()
        self._save_state()

    def selected_provider(self) -> str:
        return self._provider

    def selected_model_name(self) -> str:
        return next(
            (label for value, label in CAMERA_MODEL_OPTIONS if value == self._selected_model_id),
            "Nano Banana 2",
        )

    def selected_model_id(self) -> str:
        return self._selected_model_id

    def _refresh_prompt_preview(self) -> None:
        prompt_box = getattr(self, "prompt_box", None)
        if prompt_box is None:
            return
        prompt_box.setPlainText(self._build_camera_prompt())

    def _supports_support_references(self, model_id: Optional[str] = None) -> bool:
        return (model_id or self.selected_model_id()) != MODEL_NANO_BANANA_2_FLOWER

    def _generation_references_for_model(self, model_id: str) -> Tuple[List[Path], List[str]]:
        if self._current_ref is None:
            return [], []
        refs = [self._current_ref.path]
        ref_types = ["Current shot"]
        if self._supports_support_references(model_id):
            refs.extend(ref.path for ref in self._refs)
            ref_types.extend(ref.ref_type for ref in self._refs)
        return refs, ref_types

    def _build_camera_prompt(self) -> str:
        rotate = self.rotate_slider.value()
        vertical = self.vertical_slider.value()
        zoom = self.zoom_slider.value() / 100.0
        support_refs = self._refs if self._supports_support_references() else []

        references = []
        if self._current_ref:
            references.append({
                "tag": "[@]img1",
                "filename": self._current_ref.path.name,
                "role": "current_shot",
                "priority": "primary_source_frame",
                "instructions": [
                    "Use this image as the authoritative source frame.",
                    "Rotate only the virtual camera around the anchored center of interest.",
                    "Preserve the frozen performance exactly.",
                ],
            })
        if support_refs:
            counts = {"Character": 0, "Location": 0, "Object": 0}
            start_index = 2 if self._current_ref else 1
            for offset, ref in enumerate(support_refs):
                counts[ref.ref_type] = counts.get(ref.ref_type, 0) + 1
                role = ref.ref_type.lower()
                references.append({
                    "tag": f"[@]img{start_index + offset}",
                    "filename": ref.path.name,
                    "role": f"{role}_reference_{counts[ref.ref_type]}",
                    "priority": "support_reference_only",
                    "instructions": [
                        "Keep this reference separate from all other references.",
                        "Do not merge identities, outfits, props, or environments from different reference cards.",
                        "Do not replace the current shot subject or rebuild the whole scene from this support reference.",
                    ],
                })

        if rotate > 0:
            horizontal_direction = "left"
            horizontal_degrees = rotate
        elif rotate < 0:
            horizontal_direction = "right"
            horizontal_degrees = abs(rotate)
        else:
            horizontal_direction = "unchanged"
            horizontal_degrees = 0

        if vertical > 0:
            vertical_direction = "upward"
            vertical_degrees = vertical
        elif vertical < 0:
            vertical_direction = "downward"
            vertical_degrees = abs(vertical)
        else:
            vertical_direction = "unchanged"
            vertical_degrees = 0

        if zoom < 1.0:
            distance_factor = 1.0 / max(0.01, zoom)
            zoom_action = "move_camera_farther"
            zoom_distance_factor = f"{distance_factor:.2f}x farther"
        elif zoom > 1.0:
            zoom_action = "move_camera_closer"
            zoom_distance_factor = f"{zoom:.2f}x closer"
        else:
            zoom_action = "unchanged"
            zoom_distance_factor = "original distance"

        prompt_spec = {
            "prompt_format": "camera_lab_json_prompt_v1",
            "task": "full_scene_camera_viewpoint_change",
            "output": {
                "type": "single_image",
                "must_match_source_aspect_ratio": True,
                "primary_goal": (
                    "Create a novel-view render of the entire photographed scene from the new camera position. "
                    "The subject, foreground, background, and environment must all be reobserved from that new position."
                ),
                "reject_if": [
                    "the background is the original image background reused as a 2D plate",
                    "only the person changes while the environment keeps the same composition",
                    "the subject is rotated in place instead of the camera moving through the scene",
                ],
            },
            "references": references,
            "source_frame_contract": {
                "authoritative_source": "[@]img1",
                "preserve_from_source": "subject identity, clothing, moment, lighting style, location identity, and lens feel",
                "do_not_preserve_as_2d_backplate": (
                    "The source frame is not a background plate. Do not paste, freeze, trace, or keep the original "
                    "background layout behind a changed subject."
                ),
                "scene": "same physical location, seen from a different camera position",
                "new_content_policy": (
                    "If the source image does not show enough information for the new side/top view, hallucinate "
                    "plausible unseen background, foreground, occlusions, side surfaces, and depth continuations "
                    "that match the same real location."
                ),
            },
            "novel_view_synthesis_contract": {
                "mode": "3d_scene_rephotography_not_2d_image_edit",
                "required_behavior": (
                    "Infer the 3D layout of the whole scene from the source frame, then render a new photograph "
                    "from the requested camera position."
                ),
                "background_rule": (
                    "The environment must be regenerated from the new viewpoint. It may preserve the same place, "
                    "materials, lighting, and style, but not the same 2D arrangement of background objects."
                ),
                "if_background_reference_is_missing": (
                    "Invent plausible unseen parts of the same environment. Do not use missing information as a "
                    "reason to keep the old background unchanged."
                ),
                "minimum_visible_changes": [
                    "background object positions shift relative to the subject",
                    "foreground/background occlusion boundaries change",
                    "side surfaces or newly visible areas appear",
                    "near elements move more than far elements",
                    "the horizon/vertical posts/roof/floor lines obey the new camera angle",
                ],
                "self_check_before_final": [
                    "Would this image still look plausible if the subject were removed? The remaining environment must still show a new camera viewpoint.",
                    "If the old and new backgrounds could be aligned by a simple crop/warp, the result is wrong.",
                    "If only the person appears to rotate, the result is wrong.",
                ],
            },
            "full_scene_geometry_contract": {
                "rule": "This is a novel camera view of a whole 3D scene, not a character turntable or face/body edit.",
                "camera_motion": "the whole scene is reobserved and re-rendered from the new camera position",
                "subject": {
                    "must_not_rotate_in_place": True,
                    "must_not_turn_head_or_body_to_create_side_view": True,
                    "stays_fixed_in_world_space": True,
                    "new_view_reveals_subject_side_due_to_camera_position_only": True,
                    "head_and_gaze_remain_fixed_in_world_space": True,
                },
                "environment": {
                    "must_change_with_parallax": True,
                    "near_background_shifts_more_than_far_background": True,
                    "side_background_becomes_visible": True,
                    "old_background_layout_must_not_remain_locked": True,
                    "rebuild_occluded_areas_from_new_viewpoint": True,
                    "must_not_keep_same_pixel_or_compositional_background": True,
                    "must_invent_plausible_unseen_environment_when_needed": True,
                },
                "depth_cues_required": [
                    "parallax_between_subject_and_background",
                    "changed relative positions of roof, fence, posts, plants, and background openings",
                    "newly visible side surfaces around the subject",
                    "occlusion changes caused by the new camera angle",
                ],
                "forbidden_failure_modes": [
                    "rotating only the man while leaving the hut/fence/background unchanged",
                    "using the old background as a pasted static backdrop",
                    "cutting out the subject and placing him on the original frame",
                    "face/head/body turntable",
                    "2d warp of the person only",
                    "same background composition with a different face angle",
                    "unchanged background with a rotated subject",
                    "static wallpaper/background behind a newly posed person",
                    "simple cutout of the person pasted over the source frame",
                ],
            },
            "camera_change": {
                "operation": "orbit_virtual_camera_around_anchored_subject_in_full_3d_scene",
                "anchor": "current shot center of interest",
                "strength": "apply the full requested orbit, do not soften or reduce the angle",
                "max_range_note": "Horizontal and vertical orbit controls may request up to 90 degrees.",
                "ui_values": {
                    "horizontal_slider": rotate,
                    "vertical_slider": vertical,
                    "zoom_slider": self.zoom_slider.value(),
                },
                "horizontal_orbit": {
                    "direction": horizontal_direction,
                    "degrees": horizontal_degrees,
                    "interpretation": "move the camera around the subject by exactly this many degrees",
                    "screen_mapping": (
                        "positive horizontal slider means the requested final camera is on the left side of the subject; "
                        "negative horizontal slider means the requested final camera is on the right side of the subject"
                    ),
                    "anti_mirror_instruction": (
                        "Do not mirror this direction. A negative horizontal value must reveal the opposite side "
                        "from a positive horizontal value."
                    ),
                    "keep_subject_frame_position": True,
                },
                "vertical_orbit": {
                    "direction": vertical_direction,
                    "degrees": vertical_degrees,
                    "interpretation": "raise or lower the camera around the subject by exactly this many degrees",
                    "screen_mapping": (
                        "positive vertical slider means higher camera viewpoint; "
                        "negative vertical slider means lower camera viewpoint"
                    ),
                    "arc_center": "same anchored subject",
                },
                "zoom": {
                    "value": f"{zoom:.2f}",
                    "action": zoom_action,
                    "distance_factor": zoom_distance_factor,
                    "preserve_center_of_interest": True,
                },
                "forbidden_camera_moves": [
                    "pan",
                    "slide",
                    "truck_sideways",
                    "dolly_sideways",
                    "flat_image_shift",
                    "crop_change_unrelated_to_viewpoint",
                    "reframe_to_new_composition",
                    "subject_only_rotation",
                    "background_locked_subject_turntable",
                    "same_background_new_person_angle",
                    "2d_backplate_reuse",
                ],
            },
            "identity_and_performance_lock": {
                "rule": (
                    "The subject is frozen in the exact same moment as the source frame. "
                    "Only the camera moves; the subject does not rotate, re-pose, turn the head, or redirect the eyes."
                ),
                "head_and_gaze_lock": {
                    "rule": (
                        "Keep the head, neck, face orientation, nose direction, chin direction, and eye gaze aimed "
                        "at the same real-world point as in the source frame."
                    ),
                    "world_space_gaze_target": "unchanged from source frame",
                    "world_space_head_orientation": "unchanged from source frame",
                    "if_new_camera_is_on_the_side": (
                        "the subject must still look toward the original off-camera point, not toward the new camera"
                    ),
                    "preserve_head_yaw_pitch_roll": True,
                    "preserve_neck_rotation": True,
                    "preserve_nose_direction": True,
                    "preserve_chin_direction": True,
                    "preserve_eye_aim_vector": True,
                    "forbidden": [
                        "turning the head to face the new camera",
                        "rotating the eyes toward the new camera",
                        "making eye contact with the new camera",
                        "changing the gaze target",
                        "changing the neck angle",
                        "changing the head pose to make a prettier side profile",
                        "re-aiming the face after the camera move",
                    ],
                },
                "preserve_exactly": [
                    "character_identity",
                    "face",
                    "body_pose",
                    "gesture",
                    "head_angle",
                    "head_yaw",
                    "head_pitch",
                    "head_roll",
                    "neck_angle",
                    "nose_direction",
                    "chin_direction",
                    "eye_gaze_direction",
                    "eye_aim_vector",
                    "gaze_target_in_scene",
                    "eyelid_state",
                    "mouth_shape",
                    "eyebrows",
                    "cheeks",
                    "wrinkles",
                    "face_tension",
                    "micro_expression",
                    "emotional_state",
                    "clothing",
                    "proportions",
                ],
                "eyes": {
                    "preserve_source_eyelid_state": True,
                    "preserve_gaze_as_world_space_direction": True,
                    "do_not_rotate_eyes_or_head_to_follow_the_new_camera": True,
                    "if_camera_orbits_to_side_subject_should_not_make_new_eye_contact": True,
                    "keep_original_off_camera_gaze_target_even_after_side_orbit": True,
                    "if_source_eyes_are_closed": "must_remain_closed",
                    "if_source_eyes_are_half_closed": "must_remain_same_half_closed_shape",
                    "never_redirect_gaze_to_camera": True,
                },
                "face_forbidden_changes": [
                    "do_not_open_closed_eyes",
                    "do_not_change_expression",
                    "do_not_change_head_yaw",
                    "do_not_change_head_pitch",
                    "do_not_change_head_roll",
                    "do_not_change_neck_rotation",
                    "do_not_change_nose_direction",
                    "do_not_change_chin_direction",
                    "do_not_change_eye_aim_vector",
                    "do_not_change_gaze_target",
                    "do_not_make_eye_contact_with_new_camera",
                    "do_not_turn_head_to_side",
                    "do_not_rotate_body_to_side",
                    "do_not_beautify",
                    "do_not_normalize_face",
                    "do_not_age_shift",
                    "do_not_face_swap",
                    "do_not_reinterpret_emotion",
                ],
            },
            "reference_policy": {
                "keep_references_separate": True,
                "support_references_do_not_override_current_shot": True,
                "object_and_location_references_are_support_only": True,
                "do_not_introduce_extra_people_or_props_unless_already_visible": True,
            },
            "critical_plain_english_override": (
                "Create a new photograph from a different camera position by re-rendering the whole scene. Do not rotate only the man. "
                f"The requested horizontal camera side is {horizontal_direction} by {horizontal_degrees} degrees; do not mirror it to the opposite side. "
                "The hut, fence, roof, posts, plants, foreground, and background must all change with real parallax. "
                "If the new camera view reveals unseen areas, invent plausible unseen parts of the same location. "
                "Do not keep the old background locked behind a side-turned subject; do not use the source image as a 2D backplate. "
                "Keep the man's head orientation, neck angle, nose direction, chin direction, and eye gaze aimed at the same real-world point as in the original. "
                "If the camera moves to the side, he must not turn his head or eyes to look into the new camera. "
                "Do not open closed eyes. Do not change the face, expression, gaze, pose, clothing, identity, or performance. "
                "Do not make the subject look into the new camera; preserve the original gaze vector in the scene."
            ),
        }

        json_block = json.dumps(prompt_spec, ensure_ascii=False, indent=2)
        return (
            "Use the following JSON-style prompt as plain-text instructions for image editing. "
            "Do not parse it as an API request. The attached images are already provided in "
            "the request inputs and are referenced by their tags.\n\n"
            f"{json_block}\n\n"
            "Critical override: create a new photograph from a different camera position by re-rendering the whole scene. Do not rotate only the man. "
            f"The requested horizontal camera side is {horizontal_direction} by {horizontal_degrees} degrees; do not mirror it to the opposite side. "
            "The hut, fence, roof, posts, plants, foreground, and background must all change with real parallax. "
            "If the new camera view reveals unseen areas, invent plausible unseen parts of the same location. "
            "Do not keep the old background locked behind a side-turned subject; do not use the source image as a 2D backplate. "
            "Keep the man's head orientation, neck angle, nose direction, chin direction, and eye gaze aimed at the same real-world point as in the original. "
            "If the camera moves to the side, he must not turn his head or eyes to look into the new camera. "
            "Do not open closed eyes. Do not change the face, expression, gaze, pose, "
            "clothing, identity, or performance. Do not make the subject look into the new camera; "
            "preserve the original gaze vector in the scene."
        )

    def _set_generation_status(self, text: str = "", is_error: bool = False) -> None:
        if not hasattr(self, "generation_status_lbl"):
            return
        self.generation_status_lbl.setProperty("error", bool(is_error))
        self.generation_status_lbl.style().unpolish(self.generation_status_lbl)
        self.generation_status_lbl.style().polish(self.generation_status_lbl)
        self.generation_status_lbl.setText(text)
        self.generation_status_lbl.setVisible(bool(text))

    def _run_generation(self) -> None:
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
        model_id = self.selected_model_id()
        model_name = self.selected_model_name()
        if model_id == MODEL_NANO_BANANA_2_FLOWER and self._refs:
            self._set_generation_status(tr("camera_flower_refs_error"), is_error=True)
            return
        prompt = self._build_camera_prompt()
        out_dir = self._project_root / "shows" / show_slug / "camera_lab" / "outputs"
        refs, ref_types = self._generation_references_for_model(model_id)
        ref_names = [f"img{i + 1}" for i in range(len(refs))]
        batch_size = CAMERA_GENERATION_BATCH_SIZE
        self.generate_btn.setEnabled(False)
        for _ in range(batch_size):
            self._generation_run_id += 1
            run_id = self._generation_run_id
            thread = GeneratorImageThread(
                prompt,
                self._current_aspect_ratio_label(),
                model_id,
                out_dir,
                refs=refs,
                ref_names=ref_names,
                parent=None,
            )
            job = CameraGenerationJob(
                thread=thread,
                prompt=prompt,
                refs=list(refs),
                ref_types=list(ref_types),
                provider=self._provider,
                model_id=model_id,
                model_name=model_name,
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
        if self._current_ref is None:
            return
        state_path = self._state_path()
        if state_path is None:
            return
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "current_ref": (
                    {
                        "path": str(self._current_ref.path),
                        "type": self._current_ref.ref_type,
                    }
                    if self._current_ref
                    else None
                ),
                "refs": [
                    {"path": str(ref.path), "type": ref.ref_type}
                    for ref in self._refs
                    if ref.path.exists()
                ],
                "results": [
                    {"path": str(path)}
                    for path in self._result_paths()
                    if path.exists()
                ],
                "controls": {
                    "horizontal": self.rotate_slider.value(),
                    "vertical": self.vertical_slider.value(),
                    "zoom": self.zoom_slider.value(),
                },
                "selected_model_id": self._selected_model_id,
                "provider": self._provider,
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

            model_id = str(data.get("selected_model_id") or self._selected_model_id)
            if model_id in {item[0] for item in CAMERA_MODEL_OPTIONS}:
                self.provider_toggle.set_value(model_id)
                self._selected_model_id = model_id
                self._provider = PROVIDER_OPENAI if model_id == MODEL_OPENAI_IMAGE else PROVIDER_NARWHAL

            controls = data.get("controls") if isinstance(data.get("controls"), dict) else {}
            for slider, key, default in (
                (self.rotate_slider, "horizontal", 0),
                (self.vertical_slider, "vertical", 0),
                (self.zoom_slider, "zoom", 100),
            ):
                slider.blockSignals(True)
                slider.setValue(int(controls.get(key, default)))
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
                self.orbit.set_frame_aspect(self.current_slot.aspect_ratio())

            self._remove_layout_widgets(self.refs_strip_lay, QFrame)
            self._refs.clear()
            refs_data = data.get("refs") if isinstance(data.get("refs"), list) else []
            for item in refs_data:
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                path = Path(str(item.get("path")))
                if not path.exists():
                    continue
                ref = CameraReference(path=path, ref_type=str(item.get("type") or "Character"))
                self._refs.append(ref)
                self._append_ref_thumb(ref)
            self.refs_scroll.setVisible(bool(self._refs))

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
            self._sync_control_state()
        except Exception:
            pass
        finally:
            self._loading_state = False
            self._refresh_prompt_preview()

    def _render_generation_status(self) -> None:
        if not self._generation_jobs:
            self._set_generation_status(self._last_generation_status)
            return
        lines = self._last_generation_status.splitlines() if self._last_generation_status else []
        for index, run_id in enumerate(sorted(self._generation_jobs), start=1):
            job = self._generation_jobs[run_id]
            status = job.status or tr("camera_generation_starting")
            line = f"{index}. {job.model_name} ({job.model_id}) — {status}"
            if job.elapsed_started:
                line += f" · {tr('camera_generation_elapsed', sec=job.elapsed)}"
            lines.append(line)
        self._set_generation_status("\n".join(lines))

    def _on_generation_progress(self, run_id: int, message: str) -> None:
        job = self._generation_jobs.get(run_id)
        if job is None:
            return
        job.status = message or job.status
        if message == tr("gen_prog_generating") and not job.elapsed_started:
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

    def _on_generation_error(self, run_id: int, message: str) -> None:
        job = self._generation_jobs.get(run_id)
        if job is None:
            return
        job.elapsed_started = False
        error_text = (message or "").strip() or tr("camera_generation_error")
        job.status = f"{tr('camera_generation_error')} {error_text}"
        line = f"{job.model_name} ({job.model_id}) — {job.status}"
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
        self.results_strip_lay.addWidget(thumb)
        self.results_scroll.setVisible(True)
        self._save_state()

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
                "model": job.model_id,
                "provider": job.provider,
                "elapsed_sec": job.elapsed,
                "prompt": job.prompt,
                "references": [
                    {
                        "path": str(path),
                        "tag": f"[@]img{i + 1}",
                        "type": job.ref_types[i] if i < len(job.ref_types) else "Reference",
                    }
                    for i, path in enumerate(job.refs)
                ],
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
            background: #131516;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QFrame#camera-ref-drop-area {
            background: #131516;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QFrame#camera-result-area {
            background: #131516;
            border: 1px solid #1d1e20;
            border-radius: 8px;
        }
        QFrame#camera-ref-drop-area:hover {
            border: 1px solid #1d1e20;
        }
        QLabel#camera-slot-image {
            background: rgba(255,255,255,0.02);
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
        QPlainTextEdit#camera-result {
            background: transparent;
            color: #e4e5df;
            border: none;
            border-radius: 0px;
            padding: 8px;
            selection-background-color: #2c2f31;
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
