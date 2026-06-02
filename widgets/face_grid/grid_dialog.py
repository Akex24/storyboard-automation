# -*- coding: utf-8 -*-
"""widgets/face_grid/grid_dialog.py — попап наложения PNG-сеток на лица
склеенного сториборда блока.

Этап 3 (2026-06-02) — СКЕЛЕТ: показывает склеенный сториборд (read-only превью
в QScrollArea) + кнопки-заглушки. Логики наложения/сохранения пока НЕТ:
  • Этап 4 — UI библиотеки сеток (список/добавить/удалить, выбор активной);
  • Этап 5 — «Наложить»: YuNet находит лица → активная сетка на каждое;
  • Этап 6 — ручной drag/resize + ручная установка сетки;
  • Этап 7 — «Сохранить в рефы блока» (композит в .cache/_block_view/...).

Открывается из MainWindow._save_png ПОСЛЕ записи чистого <base>.jpg (вариант A):
чистый сториборд сохраняется как раньше (нужен «Собрать серию»), попап —
надстройка сверху.

Модальный (вызывается через .exec()) — самодостаточная задача, не теряется
за главным окном, нет рассинхрона с переключением блока (работает с файлом,
захваченным при открытии).

Контекст, который попап держит для будущих этапов:
  • stitched_path — путь к чистому <base>.jpg (Этап 5 детекция, Этап 7 база);
  • ep_id, block_n — для имени файла-результата и заголовка;
  • dest_dir — .cache/_block_view/<ep>_block<N>/ (Этап 7 — куда сохранять).

Cross-platform: только PyQt6 + QPixmap(str(path)). Без subprocess/shell/open.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QWidget, QApplication,
)

from i18n import tr
from views.theme import lumz_button_qss


class GridDialog(QDialog):
    """Попап наложения сеток на лица (скелет — Этап 3)."""

    def __init__(self, stitched_path, ep_id: str, block_n: int,
                 dest_dir, parent=None):
        super().__init__(parent)
        # ── Контекст для будущих этапов (5/7) ──
        self.stitched_path = Path(stitched_path)
        self.ep_id = str(ep_id)
        self.block_n = int(block_n)
        self.dest_dir = Path(dest_dir)

        self.setWindowTitle(
            tr('grid_dialog_title', block=f"{self.ep_id}_block{self.block_n}"))
        self.setModal(True)

        # Адаптивный размер под родительское окно (как ShotViewerDialog).
        parent_win = self.parent().window() if self.parent() else None
        if parent_win:
            pw, ph = parent_win.width(), parent_win.height()
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            pw, ph = geo.width(), geo.height()
        self.setMinimumSize(700, 500)
        self.setMaximumSize(int(pw * 0.95), int(ph * 0.95))
        self.resize(min(1100, int(pw * 0.95)), min(760, int(ph * 0.95)))

        self._build()

    def _build(self):
        self.setStyleSheet(
            "QDialog { background:#0a0a0d; }"
            "QLabel#hint { color:rgba(255,255,255,0.55); font-size:11px; }"
            "QLabel#empty { color:rgba(255,255,255,0.40);"
            " font-style:italic; font-size:13px; }"
            + lumz_button_qss('subtle', 'grid_btn_pick')
            + lumz_button_qss('primary', 'grid_btn_apply')
            + lumz_button_qss('secondary', 'grid_btn_save')
            + lumz_button_qss('subtle', 'grid_btn_close')
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # ── Превью склеенного сториборда (read-only, скроллится) ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:1px solid #25193a;"
                             " border-radius:6px; background:#0a0612; }")
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(8, 8, 8, 8)
        self.preview_lbl = QLabel()
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(str(self.stitched_path))
        if not pix.isNull():
            # Вписываем по ширине ~1024 (сохраняя пропорции). Полное
            # разрешение оригинала понадобится позже (Этап 5/7) — берётся
            # заново из self.stitched_path, не из этого превью.
            if pix.width() > 1024:
                pix = pix.scaledToWidth(
                    1024, Qt.TransformationMode.SmoothTransformation)
            self.preview_lbl.setPixmap(pix)
        else:
            self.preview_lbl.setObjectName("empty")
            self.preview_lbl.setText(tr('grid_no_image'))
        hl.addWidget(self.preview_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(holder)
        lay.addWidget(scroll, stretch=1)

        # ── Хинт-строка (фидбэк заглушек) ──
        self.hint_lbl = QLabel("")
        self.hint_lbl.setObjectName("hint")
        lay.addWidget(self.hint_lbl)

        # ── Кнопки ──
        actions = QHBoxLayout()
        actions.setSpacing(10)

        # Заглушки (логика — Этапы 4/5/7). Клик → хинт, без действия.
        self.btn_pick = QPushButton(tr('grid_btn_pick'))
        self.btn_pick.setObjectName("grid_btn_pick")
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.clicked.connect(self._stub)
        actions.addWidget(self.btn_pick)

        self.btn_apply = QPushButton(tr('grid_btn_apply'))
        self.btn_apply.setObjectName("grid_btn_apply")
        self.btn_apply.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_apply.clicked.connect(self._stub)
        actions.addWidget(self.btn_apply)

        self.btn_save = QPushButton(tr('grid_btn_save'))
        self.btn_save.setObjectName("grid_btn_save")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self._stub)
        actions.addWidget(self.btn_save)

        actions.addStretch()

        self.btn_close = QPushButton(tr('grid_btn_close'))
        self.btn_close.setObjectName("grid_btn_close")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        actions.addWidget(self.btn_close)

        lay.addLayout(actions)

    def _stub(self):
        """Заглушка кнопок Этапа 3 — логика появится на Этапах 4/5/7."""
        self.hint_lbl.setText(tr('grid_stub_hint'))
