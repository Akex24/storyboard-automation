# -*- coding: utf-8 -*-
"""
generator/scrub_decoder.py — пред-декод кадров видео для ЖИВОГО скраба попапа.

Зачем: QMediaPlayer.setPosition на паузе перематывает по keyframe и коалесцирует
частые seek'и → при таскании playhead кадр застывает/догоняет с задержкой. Решение
(вариант A+C): при открытии видео-попапа фоновым потоком декодируем ВСЕ кадры в
УМЕНЬШЕННОМ размере (~480px) и держим их как JPEG-БАЙТЫ В ОПЕРАТИВНОЙ ПАМЯТИ
(cv2.imencode → буфер, БЕЗ записи на диск). Скраб = мгновенный lookup из RAM →
QPixmap.loadFromData. Кадр следует за playhead без задержки.

Память (JPEG q85, 480px): ~25-35КБ/кадр → 6с≈5МБ, 15с≈13МБ (в пределах 15-25МБ).
Мягкий кап (mem_cap_mb) с прореживанием — страховка для аномально длинных/HFR видео
(на 6-15с@24-30fps не срабатывает).

cv2 тянется ЛЕНИВО (паттерн detector.py / generator_video_thread — модуль не падает
без opencv). Backend форсится системный (macOS=AVFoundation, win32=MSMF, иначе
default) — тот же приём, что в generator_video_thread._extract_first_frame: FFMPEG-
бэкенд cv2 в PyInstaller-.app не собирается, системные фреймворки работают frozen.
Логика backend ДУБЛИРУЕТСЯ здесь намеренно (изоляция: рабочий видеотред не трогаем).

Cross-platform: pathlib/str-пути, cv2.imencode+bytes (НЕ imwrite — не-ASCII пути).
macOS cyrillic-путь подтверждён замером (40/40). Windows .exe (VideoCapture+не-ASCII+
MSMF) — проверить при сборке под Win.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal


def _backend_list():
    """Список cv2-backend'ов для VideoCapture по платформе (системный + фоллбэк на
    default 0). Дублирует логику generator_video_thread._extract_first_frame намеренно —
    изоляция риска (рабочий видеотред не трогаем)."""
    import sys
    import cv2
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION, 0]
    if sys.platform == "win32":
        return [cv2.CAP_MSMF, 0]
    return [0]


def open_capture(video_path: str):
    """Открыть cv2.VideoCapture, перебирая backend'ы. Возвращает открытый cap или None.
    cv2 импортируется ЛЕНИВО — caller (в потоке/на клике) ловит отсутствие opencv через
    None. Caller ОБЯЗАН вызвать cap.release()."""
    try:
        import cv2
    except Exception:
        return None
    for be in _backend_list():
        try:
            cap = cv2.VideoCapture(str(video_path), be)
        except Exception:
            cap = None
        if cap is not None:
            try:
                if cap.isOpened():
                    return cap
            except Exception:
                pass
            try:
                cap.release()
            except Exception:
                pass
    return None


def grab_full_frame_jpeg(video_path: str, pos_ms: float, jpeg_q: int = 92) -> Optional[bytes]:
    """Одноразовый ПОЛНОКАДРОВЫЙ (полное разрешение видео) захват по позиции playhead →
    JPEG-байты. Для кнопки-плюсика «взять кадр в реф»: реф должен быть резким, как само
    видео — НЕ из уменьшенного скраб-кэша. Детерминизм: seek POS_MSEC + read именно на
    playhead, не зависит от async-таймингов videoSink. ~20мс на один клик.

    Возвращает JPEG-байты (BGR→JPEG через cv2.imencode — стандартный цвет, ручной BGR→RGB
    НЕ нужен: JPEG-roundtrip сохраняет порядок каналов) или None при любой ошибке (caller
    откатится на videoSink-фоллбэк). Запись на диск — забота caller (Path.write_bytes,
    не-ASCII safe)."""
    cap = open_capture(video_path)
    if cap is None:
        return None
    try:
        import cv2
        cap.set(cv2.CAP_PROP_POS_MSEC, float(max(0.0, pos_ms)))
        ok, frame = cap.read()
        if not ok or frame is None or getattr(frame, "size", 0) <= 0:
            return None
        ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_q)])
        if not ok2:
            return None
        return buf.tobytes()
    except Exception:
        return None
    finally:
        try:
            cap.release()
        except Exception:
            pass


class ScrubPreloadThread(QThread):
    """Фоновый пред-декод ВСЕХ кадров видео в уменьшенные JPEG-байты (RAM, без диска).

    Lifecycle: создаётся parent=None, ссылка хранится на диалоге (ARCHITECTURE «QThread
    из QDialog» — паттерн A: parent=None исключает Qt-destructor SIGABRT, даже если
    какой-то путь закрытия пропустим). Диалог в teardown зовёт stop()+wait(); cap
    освобождается в run()'s finally при выходе из цикла по _abort.

    Сигналы:
      ready(object) — кэш готов: list[(ts_ms:int, jpeg_bytes:bytes)] (отсортирован по ts).
      failed()      — cv2 не открыл файл / 0 кадров → грейсфул-фоллбэк на старый скраб.
    Прогресс не шлём — спиннер indeterminate (число кадров заранее не знаем надёжно).
    """

    ready = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, video_path: str, long_side: int = 480, jpeg_q: int = 85,
                 mem_cap_mb: int = 40, parent=None):
        super().__init__(parent)
        self._video_path = str(video_path or "")
        self._long_side = max(160, int(long_side))
        self._jpeg_q = max(40, min(95, int(jpeg_q)))
        self._mem_cap_bytes = max(8, int(mem_cap_mb)) * 1024 * 1024
        self._abort = False

    def stop(self):
        """Прервать пред-декод (run выйдет из цикла, cap.release() в finally)."""
        self._abort = True

    def run(self):
        try:
            import cv2
            import numpy as _np  # noqa: F401  (cv2 возвращает numpy-кадр)
        except Exception:
            self.failed.emit()
            return
        cap = open_capture(self._video_path)
        if cap is None:
            self.failed.emit()
            return
        cache: List[Tuple[int, bytes]] = []
        try:
            # Прореживание (страховка): оценка кадров × ~30КБ/JPEG. Если проекция памяти
            # выше мягкого капа → берём каждый stride-й кадр. На 6-15с@24-30fps stride=1.
            try:
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            except Exception:
                n = 0
            stride = 1
            if n > 0:
                projected = n * 30000
                if projected > self._mem_cap_bytes:
                    stride = max(1, (projected + self._mem_cap_bytes - 1) // self._mem_cap_bytes)
            jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_q]
            idx = 0
            acc = 0
            while not self._abort:
                # grab() продвигает БЕЗ декода (дёшево пропустить прорежённые кадры);
                # retrieve() декодирует только нужный кадр.
                if not cap.grab():
                    break
                if idx % stride == 0:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None and getattr(frame, "size", 0) > 0:
                        ts_ms = 0
                        try:
                            ts_ms = int(cap.get(cv2.CAP_PROP_POS_MSEC))
                        except Exception:
                            ts_ms = 0
                        small = self._downscale(frame, cv2)
                        ok2, buf = cv2.imencode(".jpg", small, jpeg_params)
                        if ok2:
                            b = buf.tobytes()
                            cache.append((ts_ms, b))
                            acc += len(b)
                            # Жёсткая страховка по факту: если несмотря на stride копим
                            # выше капа — увеличиваем stride на лету (роняем плотность,
                            # не память). Кадры останутся монотонными по ts.
                            if acc > self._mem_cap_bytes:
                                stride += 1
                idx += 1
        except Exception:
            # Частичный кэш лучше, чем ничего — если хоть что-то набрали, отдадим.
            pass
        finally:
            try:
                cap.release()
            except Exception:
                pass
        if self._abort:
            # Закрытие во время прогрева — результат не нужен (диалог уже уходит).
            return
        if not cache:
            self.failed.emit()
            return
        # ts монотонно растёт по ходу декода → список уже отсортирован; гарантируем.
        cache.sort(key=lambda t: t[0])
        self.ready.emit(cache)

    def _downscale(self, frame, cv2):
        """Кадр BGR → уменьшенный по большей стороне до _long_side (INTER_AREA — лучшее
        качество при уменьшении). Если кадр уже меньше — оставляем как есть."""
        try:
            h, w = frame.shape[:2]
            m = max(h, w)
            if m <= self._long_side:
                return frame
            scale = self._long_side / float(m)
            nw = max(1, int(round(w * scale)))
            nh = max(1, int(round(h * scale)))
            return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        except Exception:
            return frame
