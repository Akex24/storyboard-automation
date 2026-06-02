# -*- coding: utf-8 -*-
"""widgets/face_grid/detector.py — детекция лиц через YuNet (cv2.FaceDetectorYN).

Этап 1 (2026-06-02). Чистая логика, без UI. Используется попапом наложения
PNG-сеток: находит лица на склеенном сториборде блока, чтобы автоматически
наложить на каждое выбранную сетку.

Контракт:
    detect_faces(image, ...) -> [(x, y, w, h), ...]
    Координаты — в пикселях ИСХОДНОГО (полноразмерного) изображения, НЕ превью.
    Это критично для дальнейшего наложения/сохранения в полном разрешении.

Зависимости: opencv-python-headless (cv2) + numpy + Pillow. Модель —
assets/models/face_detection_yunet_2023mar.onnx, путь резолвится через
storyboard_app.get_model_path (_MEIPASS-aware, работает и в .app/.exe-бандле).

Cross-platform: только cv2 / numpy / PIL / pathlib. Без subprocess / shell /
raw open() — гейт не требуется. Картинку декодит Pillow (кроссплатформенно),
не cv2.imread (тот спотыкается на не-ASCII путях в Windows).

Импорты cv2/numpy/PIL — ЛЕНИВЫЕ (внутри функций), чтобы импорт модуля не падал
в окружении без opencv (например на этапе ast/smoke до установки зависимостей).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Union

# Имя бандлованной модели (см. StoryboardStudio.spec datas + get_model_path).
MODEL_NAME = "face_detection_yunet_2023mar.onnx"

# Тип входа: путь к файлу / PIL.Image / numpy-массив (RGB).
ImageInput = Union[str, Path, "object"]


def _resolve_model_path() -> Path:
    """Путь к YuNet .onnx через storyboard_app.get_model_path (_MEIPASS-aware).
    Ленивый импорт storyboard_app — избегаем circular import на уровне модуля."""
    try:
        import storyboard_app as _sa
        p = _sa.get_model_path(MODEL_NAME)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Не удалось получить путь к модели: {e}") from e
    if not p or not Path(p).exists():
        raise FileNotFoundError(
            f"YuNet модель не найдена ({MODEL_NAME}). Ожидалась в assets/models/ "
            f"(бандл или project_root). get_model_path вернул: {p}")
    return Path(p)


def _to_bgr(image: ImageInput):
    """Приводит вход к BGR numpy-массиву (uint8, HxWx3) для cv2.

    Декод — через Pillow (кроссплатформенно, без проблем cv2.imread с
    не-ASCII путями на Windows). PIL даёт RGB → переворачиваем в BGR.
    """
    import numpy as np
    from PIL import Image as PILImage

    if isinstance(image, (str, Path)):
        with PILImage.open(image) as im:
            arr = np.array(im.convert("RGB"))
    elif isinstance(image, PILImage.Image):
        arr = np.array(image.convert("RGB"))
    else:
        # Предполагаем numpy-массив RGB (HxWx3) или grayscale (HxW).
        arr = np.asarray(image)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        elif arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]  # отбросить альфу
    # RGB -> BGR, contiguous (cv2 требует C-contiguous uint8)
    bgr = np.ascontiguousarray(arr[:, :, ::-1])
    if bgr.dtype != np.uint8:
        bgr = bgr.astype("uint8")
    return bgr


def detect_faces(
    image: ImageInput,
    score_threshold: float = 0.6,
    nms_threshold: float = 0.3,
) -> List[Tuple[int, int, int, int]]:
    """Находит лица на изображении, возвращает боксы в координатах ИСХОДНОГО
    изображения.

    Args:
        image: путь (str/Path) к картинке, либо PIL.Image, либо numpy RGB.
        score_threshold: порог уверенности YuNet (0..1). 0.6 — компромисс:
            ловит фотореалистичные лица, меньше ложных на текстурах. Эскизы
            ловит хуже — это ОК, недостающие лица юзер добавит вручную.
        nms_threshold: порог non-max-suppression (склейка пересекающихся боксов).

    Returns:
        Список (x, y, w, h) — левый верхний угол + ширина/высота в пикселях
        полноразмерного изображения. Пустой список если лиц нет.
    """
    import cv2
    import numpy as np  # noqa: F401

    bgr = _to_bgr(image)
    h, w = bgr.shape[:2]
    if w <= 0 or h <= 0:
        return []

    model = _resolve_model_path()
    # input_size задаём реальным размером картинки → координаты лиц вернутся
    # в координатах ИСХОДНОГО изображения (не нормированные, не превью).
    detector = cv2.FaceDetectorYN_create(
        str(model), "", (w, h), score_threshold, nms_threshold, 5000)
    detector.setInputSize((w, h))

    _, faces = detector.detect(bgr)
    boxes: List[Tuple[int, int, int, int]] = []
    if faces is None:
        return boxes
    for f in faces:
        # f[0..3] = x, y, w, h (далее 5 landmark'ов и score — нам не нужны)
        x = int(round(float(f[0])))
        y = int(round(float(f[1])))
        fw = int(round(float(f[2])))
        fh = int(round(float(f[3])))
        # Клампим в границы картинки (YuNet иногда даёт чуть за край).
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        fw = max(1, min(fw, w - x))
        fh = max(1, min(fh, h - y))
        boxes.append((x, y, fw, fh))
    return boxes
