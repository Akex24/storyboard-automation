# -*- coding: utf-8 -*-
"""
views/scenario_drop_zone.py — drop-зона для загрузки документа со сценариями.

Юзкейс: на стартовом экране сериала пользователь перетаскивает файл `.txt`/`.md`
с библией + 20 сериями. Виджет принимает drop, отправляет сигнал
`file_dropped(Path)` дальше — caller (storyboard_app) сам делает парсинг
через `scenario_parser` и сохранение.

Эта вьюшка — только UI и обработка drag&drop / file dialog. Никакой
файловой логики или парсинга — это всё в `scenario_parser.py`.

История: создан 2026-05-05 для долга-фичи A.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
)

from i18n import tr


# Расширения которые принимаем как сценарий-документ.
# Список синхронизирован с scenario_parser.SUPPORTED_EXTENSIONS — там же
# и логика чтения каждого формата.
from scenario_parser import SUPPORTED_EXTENSIONS as _ALLOWED_EXTS  # noqa: E402


class ScenarioDropZone(QFrame):
    """Виджет с большой пунктирной рамкой и текстом «перетащи файл сюда».

    Сигнал:
        file_dropped(Path) — испускается при успешном drag&drop ИЛИ при
        выборе файла через кнопку «Выбрать файл…». Caller отвечает за
        чтение файла, парсинг и сохранение.
    """

    file_dropped = pyqtSignal(Path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("scenario-drop-zone")
        self.setAcceptDrops(True)
        # Стиль — пунктирная рамка чтобы было визуально понятно что сюда
        # можно перетащить. На hover (drag-over) — подсветка.
        self.setStyleSheet("""
            QFrame#scenario-drop-zone {
                border: 2px dashed #4a3a68;
                border-radius: 10px;
                background: rgba(34, 26, 48, 0.4);
                padding: 16px;
            }
            QFrame#scenario-drop-zone[drag-active="true"] {
                border-color: #8a6ad8;
                background: rgba(74, 58, 104, 0.5);
            }
        """)
        self.setMinimumHeight(140)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Иконка-плейсхолдер (символьная, без emoji-зависимостей)
        icon_label = QLabel("⬇")
        icon_label.setStyleSheet("font-size: 28px; color: #8a6ad8;")
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon_label)

        # Главный текст
        self._main_label = QLabel(tr('drop_zone_main'))
        self._main_label.setStyleSheet(
            "color: #cfc6e0; font-size: 14px; font-weight: 500;"
        )
        self._main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._main_label)

        # Подсказка
        self._hint_label = QLabel(tr('drop_zone_hint'))
        self._hint_label.setStyleSheet(
            "color: #888; font-size: 11px;"
        )
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._hint_label)

        # Кнопка «Выбрать файл…» — для тех кто не хочет drag&drop
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._browse_btn = QPushButton(tr('drop_zone_browse_btn'))
        self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_btn.clicked.connect(self._on_browse)
        btn_row.addWidget(self._browse_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

    # ─── Public API ──────────────────────────────────────────────────────

    def retranslate(self) -> None:
        """Обновить тексты при смене языка интерфейса."""
        self._main_label.setText(tr('drop_zone_main'))
        self._hint_label.setText(tr('drop_zone_hint'))
        self._browse_btn.setText(tr('drop_zone_browse_btn'))

    # ─── Drag & Drop ─────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        # Проверяем что хотя бы один из файлов имеет нужное расширение
        ok = any(self._is_acceptable(Path(u.toLocalFile())) for u in event.mimeData().urls())
        if ok:
            event.acceptProposedAction()
            self.setProperty("drag-active", "true")
            self.style().unpolish(self)
            self.style().polish(self)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("drag-active", "false")
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("drag-active", "false")
        self.style().unpolish(self)
        self.style().polish(self)

        urls = event.mimeData().urls()
        files: List[Path] = []
        for u in urls:
            p = Path(u.toLocalFile())
            if self._is_acceptable(p):
                files.append(p)

        if not files:
            event.ignore()
            return

        # Берём первый подходящий — несколько файлов одновременно не
        # поддерживаем (для простоты и предсказуемости).
        event.acceptProposedAction()
        self.file_dropped.emit(files[0])

    # ─── Browse ──────────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        """Кнопка «Выбрать файл…» — открывает системный QFileDialog."""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            tr('drop_zone_browse_title'),
            "",
            "Документы (*.txt *.md *.rtf *.docx);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if self._is_acceptable(path):
            self.file_dropped.emit(path)

    # ─── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_acceptable(path: Path) -> bool:
        """True если файл существует и имеет одно из разрешённых расширений."""
        if not path or not path.is_file():
            return False
        return path.suffix.lower() in _ALLOWED_EXTS
