# -*- coding: utf-8 -*-
"""UI фичи «убрать вотермарк»: фоновые треды + модальный прогресс + оркестрация
клика-по-сердечку и кнопки в шапке. Сама логика удаления — в video/watermark.py.

Инварианты:
  • remove_watermark НЕ параллелится: по сердечку — серийная очередь на страницу
    (_RemovalQueue), батч — модальный (exec блокирует UI → нет гонки за файл).
  • во время обработки карточки: спиннер + дизейбл reveal/ref (Alex не утащит в
    DaVinci версию со звездой), под alive-guard (карточку могут снести корзиной).
"""
import os
import sys
import traceback

from PyQt6.QtCore import QThread, pyqtSignal, Qt, QEvent, QTimer
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QMessageBox)

from i18n import tr


def _log(ctx, exc):
    """Лог ошибки в stderr (уходит в runtime.log) — чтобы фон не глох молча."""
    sys.stderr.write(f"[watermark_ui] {ctx}: {exc}\n")
    traceback.print_exc()


# ------------------------------------------------------------------ утилиты
def _alive(w) -> bool:
    """Жив ли C++ виджет (карточку могли удалить корзиной во время обработки)."""
    if w is None:
        return False
    try:
        from PyQt6 import sip
        if sip.isdeleted(w):
            return False
    except Exception:
        pass
    try:
        w.objectName()            # доступ к C++ → RuntimeError если удалён
        return True
    except RuntimeError:
        return False
    except Exception:
        return True


def _track(page, th):
    """Держать ссылку на воркер на СТРАНИЦЕ (переживает удаление карточек), чистить
    по завершении — анти-GC без привязки к недолговечной карточке."""
    s = getattr(page, "_wm_workers", None)
    if s is None:
        s = set()
        page._wm_workers = s
    s.add(th)
    th.finished.connect(lambda: s.discard(th))


def _favorite_video_paths(page):
    """Абсолютные пути существующих видео-избранных текущего сериала."""
    try:
        gen = page._favorites_path().parent
        return [str(gen / it["file"]) for it in page._load_favorites()
                if it.get("type") == "video" and (gen / it["file"]).is_file()]
    except Exception:
        return []


class _Worker(QThread):
    """Зовёт fn() в фоне → done(object) / failed(str)."""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:
            self.failed.emit(str(e))


# ------------------------------------------------- индикатор на карточке (#4)
def _center_spinner(cell, diameter=52):
    """_BusySpinner по центру карточки, переживает ресайз холста (eventFilter)."""
    from generator.viewer_dialog import _BusySpinner

    class _CenterSpinner(_BusySpinner):
        def __init__(self, parent, d):
            super().__init__(parent, d)
            self._recenter()
            try:
                parent.installEventFilter(self)
            except Exception:
                pass

        def eventFilter(self, obj, ev):
            try:
                if ev.type() == QEvent.Type.Resize:
                    self._recenter()
            except Exception:
                pass
            return False

        def _recenter(self):
            par = self.parent()
            if par is not None:
                self.move((par.width() - self._d) // 2, (par.height() - self._d) // 2)

    sp = _CenterSpinner(cell, diameter)
    sp.start()
    return sp


def _set_card_processing(cell, on):
    """on=True: спиннер + дизейбл reveal/ref/trash + тултип «идёт обработка». on=False:
    снять спиннер, вернуть кнопки/тултипы. Всё под alive-guard. trash дизейблим тоже:
    клик корзины во время удаления искры = два процесса на одном файле → рассинхрон."""
    if not _alive(cell):
        return
    btns = [getattr(cell, n, None) for n in ("btn_reveal", "btn_ref", "btn_trash")]
    if on:
        try:
            cell._wm_spinner = _center_spinner(cell)
        except Exception:
            cell._wm_spinner = None
        for b in btns:
            if b is None:
                continue
            try:
                b._wm_saved_tip = b.toolTip()
                b.setEnabled(False)
                b.setToolTip(tr('wm_processing_tip'))
            except Exception:
                pass
    else:
        sp = getattr(cell, "_wm_spinner", None)
        if sp is not None:
            try:
                sp.stop()
                sp.deleteLater()
            except Exception:
                pass
            cell._wm_spinner = None
        for b in btns:
            if b is None:
                continue
            try:
                b.setToolTip(getattr(b, "_wm_saved_tip", "") or "")
            except Exception:
                pass
        # reveal доступность зависит от наличия файла — отдать штатной логике
        try:
            if hasattr(cell, "_refresh_reveal_enabled"):
                cell._refresh_reveal_enabled()
        except Exception:
            pass
        # btn_ref / btn_trash — вернуть активность (reveal — через _refresh_reveal_enabled выше)
        for nm in ("btn_ref", "btn_trash"):
            b2 = getattr(cell, nm, None)
            if b2 is not None:
                try:
                    b2.setEnabled(True)
                except Exception:
                    pass


# ------------------------------------------ серийная очередь удаления (#5)
class _RemovalQueue:
    """remove_watermark строго серийно на страницу: обработка идёт → путь в очередь,
    следующий по завершении. Держится на page._wm_queue."""

    def __init__(self, page):
        self._page = page
        self._q = []
        self._active = None

    def enqueue(self, cell, path):
        self._q.append((cell, path))
        self._pump()

    def _pump(self):
        if self._active is not None or not self._q:
            return
        cell, path = self._q.pop(0)
        _set_card_processing(cell, True)
        from video.watermark import remove_watermark
        th = _Worker(lambda: remove_watermark(path), self._page)
        _track(self._page, th)
        self._active = th

        def _fin(_res):
            _set_card_processing(cell, False)
            self._active = None
            self._pump()

        th.done.connect(_fin)
        th.failed.connect(lambda e=None: (_log("remove_watermark", e), _fin(False)))
        th.start()


def _page_queue(page):
    q = getattr(page, "_wm_queue", None)
    if q is None:
        q = _RemovalQueue(page)
        page._wm_queue = q
    return q


def _revert_favorite(cell, page):
    """Снять отметку избранного (файл не обработали → состояние сердечка = реальность).
    Через тот же атомарный page.toggle_favorite (файл сейчас в избранном → toggle off = снять)
    + освежаем сердечко карточки."""
    try:
        fname = cell._fav_key() if (cell is not None and hasattr(cell, "_fav_key")) else None
        if fname and page.is_favorite(fname):
            page.toggle_favorite(fname, "video")     # сейчас в избранном → toggle off = снять
        if _alive(cell) and hasattr(cell, "_refresh_heart_state"):
            cell._refresh_heart_state()              # перекрасить сердечко на карточке
    except Exception as e:
        _log("revert_favorite", e)


# --------------------------------------------- путь по сердечку (тихий, #2 плана)
def on_favorite_video(cell, page, path):
    """Сердечко поставлено на видео → тихо: has_watermark (фон). Искры нет — тишина.
    Есть — busy? → QMessageBox (сердечко ОСТАЁТСЯ); свободен — в серийную очередь."""
    if not path or not os.path.isfile(path):
        return
    from video.watermark import has_watermark

    def _after_detect(res):
        if not _alive(cell):
            return
        try:
            has = bool(res[0])
        except Exception:
            has = False
        if not has:
            return                               # искры нет — тишина
        from fs_utils import is_file_busy
        if is_file_busy(path):
            _revert_favorite(cell, page)         # занят → откат сердечка (не обработали → не в избранном)
            QMessageBox.warning(cell.window(), tr('wm_busy_title'), tr('wm_busy_body'))
            return                               # файл не трогаем
        _page_queue(page).enqueue(cell, path)

    th = _Worker(lambda: has_watermark(path), page)
    _track(page, th)
    th.done.connect(_after_detect)
    th.failed.connect(lambda e=None: _log("detect", e))   # heart-путь без попапа, но НЕ молча (лог)
    th.start()


# ---------------------------------------------------- кнопка в шапке (батч)
class _BatchThread(QThread):
    progress = pyqtSignal(int, int, str)              # done, total, filename
    finished_all = pyqtSignal(int, int, list, bool)   # processed, skipped, busy, cancelled

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths = paths
        self._total = len(paths)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        from video.watermark import remove_watermark
        from fs_utils import is_file_busy
        processed = skipped = 0
        busy = []
        cancelled = False
        for i, p in enumerate(self._paths, 1):
            if self._cancel:
                cancelled = True
                break
            self.progress.emit(i, self._total, os.path.basename(p))
            try:
                if is_file_busy(p):
                    busy.append(os.path.basename(p))
                    continue
                if remove_watermark(p):
                    processed += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        self.finished_all.emit(processed, skipped, busy, cancelled)


class _ProgressDialog(QDialog):
    """Модальный прогресс: спиннер + «Обработано N из M» + имя файла + Отмена
    (прерывает ПОСЛЕ текущего файла). Результат — в self.result_data."""

    def __init__(self, thread, total, parent=None):
        super().__init__(parent)
        self._thread = thread
        self.result_data = None                       # (processed, skipped, busy, cancelled)
        self.setWindowTitle(tr('wm_progress_title'))
        self.setModal(True)
        lay = QVBoxLayout(self)
        from generator.viewer_dialog import _BusySpinner
        self._spin = _BusySpinner(self, diameter=80)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(self._spin); row.addStretch()
        lay.addLayout(row)
        self._lbl = QLabel(tr('wm_progress_text').format(done=0, total=total))
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name = QLabel("")
        self._name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._lbl)
        lay.addWidget(self._name)
        self._btn = QPushButton(tr('wm_progress_cancel'))
        self._btn.clicked.connect(self._on_cancel)
        brow = QHBoxLayout(); brow.addStretch(); brow.addWidget(self._btn); brow.addStretch()
        lay.addLayout(brow)
        thread.progress.connect(self._on_progress)
        thread.finished_all.connect(self._on_finished)
        self._spin.start()
        # Старт потока ПОСЛЕ запуска цикла событий exec() (singleShot=0 сработает на первой
        # итерации). Иначе, если поток отработает мгновенно, finished_all придёт ДО exec()
        # → accept() потеряется, диалог зависнет.
        QTimer.singleShot(0, thread.start)

    def _on_progress(self, done, total, name):
        self._lbl.setText(tr('wm_progress_text').format(done=done, total=total))
        self._name.setText(name)

    def _on_cancel(self):
        self._btn.setEnabled(False)                   # прервёт после текущего файла
        try:
            self._thread.cancel()
        except Exception:
            pass

    def _on_finished(self, processed, skipped, busy, cancelled):
        self.result_data = (processed, skipped, busy, cancelled)
        try:
            self._spin.stop()
        except Exception:
            pass
        self.accept()

    def closeEvent(self, ev):
        try:
            self._thread.cancel()                     # крестик = отмена
        except Exception:
            pass
        super().closeEvent(ev)


class _ScanDialog(QDialog):
    """Модальный спиннер «Проверяю избранное…» на время пре-скана. Пока показан — клики
    по UI невозможны (модально) → нет параллельных сканов и очереди попапов.
    Результат — self.result_n / self.error."""

    def __init__(self, thread, parent=None):
        super().__init__(parent)
        self._thread = thread
        self.result_paths = None          # список путей С ИСКРОЙ (от воркера)
        self.error = None
        self.setWindowTitle(tr('wm_scanning'))
        self.setModal(True)
        lay = QVBoxLayout(self)
        from generator.viewer_dialog import _BusySpinner
        self._spin = _BusySpinner(self, diameter=80)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(self._spin); row.addStretch()
        lay.addLayout(row)
        lbl = QLabel(tr('wm_scanning'))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        thread.done.connect(self._on_done)
        thread.failed.connect(self._on_failed)
        self._spin.start()
        QTimer.singleShot(0, thread.start)            # старт после запуска цикла событий exec()

    def _on_done(self, val):
        self.result_paths = val           # список путей с искрой
        try:
            self._spin.stop()
        except Exception:
            pass
        self.accept()

    def _on_failed(self, msg):
        self.error = msg or "scan failed"
        try:
            self._spin.stop()
        except Exception:
            pass
        self.reject()


def run_batch(page):
    """Кнопка «Убрать вотермарк»: модальный скан-спиннер → попап с N → модальный прогресс.
    Всё последовательными модалками (клики невозможны → нет параллельных сканов/очереди попапов)."""
    paths = _favorite_video_paths(page)
    if not paths:
        QMessageBox.information(page, tr('wm_none_title'), tr('wm_none_body'))
        return
    from video.watermark import has_watermark

    # 1) пре-скан под модальным спиннером: собираем СПИСОК путей С ИСКРОЙ (не только счётчик).
    #    Поток стартует ИЗ диалога → нет гонки done<accept.
    scan = _Worker(lambda: [p for p in paths if has_watermark(p)[0]], page)
    _track(page, scan)
    dlg = _ScanDialog(scan, page)
    dlg.exec()
    if dlg.error is not None:
        QMessageBox.warning(page, tr('wm_done_title'), dlg.error)
        return
    wm_paths = dlg.result_paths or []
    n = len(wm_paths)
    if n == 0:
        QMessageBox.information(page, tr('wm_none_title'), tr('wm_none_body'))
        return

    # 2) подтверждение
    box = QMessageBox(page)
    box.setWindowTitle(tr('wm_confirm_title'))
    box.setText(tr('wm_confirm_body').format(n=n))
    ok_btn = box.addButton(tr('wm_confirm_ok'), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(tr('wm_confirm_cancel'), QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not ok_btn:
        return

    # 3) батч ТОЛЬКО по файлам с искрой → прогресс «i из n», «без вотермарка» уходит в 0
    #    (занятые среди них — отдельным списком, как и было). Поток стартует ИЗ диалога.
    th = _BatchThread(wm_paths, page)
    page._wm_batch_thread = th                        # анти-GC
    pdlg = _ProgressDialog(th, len(wm_paths), page)
    pdlg.exec()
    processed, skipped, busy, cancelled = pdlg.result_data or (0, 0, [], False)
    if cancelled:
        body = tr('wm_cancelled_body').format(done=processed + skipped + len(busy), total=len(wm_paths))
    else:
        body = tr('wm_done_body').format(processed=processed, skipped=skipped)
    if busy:
        body += "\n" + tr('wm_done_busy_line').format(n=len(busy))
    QMessageBox.information(page, tr('wm_done_title'), body)
