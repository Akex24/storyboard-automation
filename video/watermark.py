#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video/watermark.py — удаление искры-вотермарк FastGen (4-лучевая полупрозрачная
звезда в правом-нижнем углу) с видео. Перенос из R&D ~/Downloads/watermark_remover.py.

Публичный API:
    has_watermark(path)    -> (bool, float)   # (есть ли искра, score детекта)
    remove_watermark(path) -> bool            # замена файла НА МЕСТЕ; см. ниже

Пайплайн (один поток кадров): high-pass presence-детект в ШИРОКОМ регионе (найденный
центр, fallback на якорь) -> affine un-blend (a*orig+b) по центру -> билатераль ядра
-> rim-safe добивание (v10c) -> гаусс-блюр поверх по мягкой маске звезды (растворяет
остаточный контур на светлом фоне) -> энкод libx264 crf20 -c:a copy. Метка removed_v2.

ffmpeg — через imageio_ffmpeg (бандлится в .app/.exe, .spec), НЕ системный.
Калибровка — watermark_calib.npz рядом с модулем (бандлится через .spec datas).

ВАЖНО (frozen .app): cv2 по умолчанию декодит mp4 через FFMPEG, чьи dylib
PyInstaller НЕ кладёт → VideoCapture молча не читает. Поэтому кадры читаем через
_open_capture() с СИСТЕМНЫМ backend (macOS CAP_AVFOUNDATION, Win CAP_MSMF) —
паттерн продублирован из generator/scrub_decoder.py намеренно (изоляция).
"""
import os
import sys
import tempfile
import subprocess
from pathlib import Path

import numpy as np
import cv2

# ------------------------------------------------------------------ constants
ANCHOR_DX, ANCHOR_DY = 128, 120     # искра = (W-ANCHOR_DX, H-ANCHOR_DY) — обычная позиция
SEARCH = 80                         # РАДИУС поиска центра искры: широкий угловой регион
                                    # (клампится к границам кадра). Звезда почти всегда на
                                    # якоре, но ищем честно и падаем на якорь при неуверенности.
PRESENCE_SCORE = 0.45               # основной порог high-pass CCOEFF_NORMED
BORDER_LO = 0.35                    # нижняя граница borderline-полосы
OFF_TOL = 3                         # ±px offset детекта от якоря для borderline
POS_TRUST_RATIO = 0.85              # позиции доверяем только если 2-й пик ≤ RATIO·score
                                    # (иначе fallback на якорь — см. _apply_center)
HP_SIGMA = 6.0                      # сигма high-pass для детекции
G0 = 24.0                           # градиентный масштаб защиты кромок (v10c)
CRF = "20"
# блюр поверх un-blend (утв. на пачке 117): растворяет остаточный контур на светлом/ровном
# фоне. Мягкая маска на холсте 2*RB, доходит до нуля ДО края (иначе квадратный срез).
RB = 80                             # полурегион блюра (холст 160×160)
BLUR_MARGIN = 8                     # дилейт маски звезды (отступ, px)
BLUR_FEATHER = 7.0                  # перо маски (мягкий край, сигма)
BLUR_SIGMA = 10.0                   # сила gaussian-блюра в зоне маски
BLUR_K = 1.0                        # прозрачность блюра (1=полный; k<1 → проступает un-blend)
DET_SAMPLES = 24                    # сколько кадров сэмплить для детект-медианы
MED_SAMPLES = 120                   # верхняя граница выборки медианы
WM_FLAG = "lumz_wm"                 # метаданный флаг «обработано» в mp4 (udta)
WM_FLAG_VALUE = "removed_v2"        # значение при удалении НОВЫМ методом (un-blend + блюр)
WM_FLAG_LEGACY = "removed"          # старый метод (без блюра) — тоже считаем «чищено»
WM_FLAG_DONE = (WM_FLAG_LEGACY, WM_FLAG_VALUE)  # любое → уже обработан, пропускать


def no_console_kwargs() -> dict:
    """kwargs для subprocess чтобы не показывать чёрное cmd-окно на Windows."""
    if sys.platform == 'win32':
        return {'creationflags': 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _backend_list():
    """cv2-backend'ы по платформе: системный + фоллбэк на default. В frozen .app
    default (FFMPEG) не читает mp4 (dylib не в бандле) — форсим системный."""
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION, 0]
    if sys.platform == "win32":
        return [cv2.CAP_MSMF, 0]
    return [0]


def _open_capture(path):
    """cv2.VideoCapture с перебором системных backend'ов. None если не открылся.
    Caller ОБЯЗАН вызвать cap.release()."""
    for be in _backend_list():
        try:
            cap = cv2.VideoCapture(str(path), be)
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


def _calib_path() -> str:
    name = "watermark_calib.npz"
    if getattr(sys, "frozen", False):
        cand = os.path.join(sys._MEIPASS, "video", name)
        if os.path.isfile(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


_CAL = None
def _load_calib():
    global _CAL
    if _CAL is None:
        d = np.load(_calib_path())
        _CAL = (d["a"].astype(np.float32), d["b"].astype(np.float32),
                d["Mfoot"].astype(np.float32), d["Mcore"].astype(np.uint8),
                d["templ"].astype(np.float32), int(d["HALF"]))
    return _CAL


def _ffmpeg_exe() -> str:
    """Путь к ffmpeg из imageio-ffmpeg (бандл), НЕ системный."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        bindir = os.path.join(sys._MEIPASS, "imageio_ffmpeg", "binaries")
        if os.path.isdir(bindir):
            for f in sorted(os.listdir(bindir)):
                if f.startswith("ffmpeg"):
                    return os.path.join(bindir, f)
    raise RuntimeError("ffmpeg не найден: imageio-ffmpeg не установлен/не забандлен.")


def _read_meta_flag(path):
    """Метаданный флаг WM_FLAG (lumz_wm) из mp4 через bundled `ffmpeg -i` (~9мс).
    ffprobe в imageio-бандл НЕ входит → читаем метаданные из stderr `ffmpeg -i`.
    Возвращает значение флага (напр. 'removed') или None. Ошибка → None (→ обычный детект)."""
    try:
        r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=15, **no_console_kwargs())
        for line in r.stderr.splitlines():
            s = line.strip()
            if s.lower().startswith(WM_FLAG):
                return s.split(":", 1)[1].strip() if ":" in s else ""
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ detection
def _sample_frames(path, k=DET_SAMPLES):
    cap = _open_capture(path)
    if cap is None:
        return [], (0, 0)
    nf = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fs = []
    if nf <= 0:
        while len(fs) < k:
            ok, f = cap.read()
            if not ok:
                break
            fs.append(f)
    else:
        for i in np.linspace(0, nf - 1, min(nf, k)).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, f = cap.read()
            if ok:
                fs.append(f)
    cap.release()
    return fs, (W, H)


def _median(frames):
    n = len(frames)
    idx = np.linspace(0, n - 1, min(n, MED_SAMPLES)).astype(int)
    return np.median(np.stack([frames[i] for i in idx]).astype(np.float32), axis=0).astype(np.uint8)


def _highpass(img, sigma=HP_SIGMA):
    f = img.astype(np.float32)
    return f - cv2.GaussianBlur(f, (0, 0), sigma)


def _detect(path):
    """Ищет искру в ШИРОКОМ угловом регионе (радиус SEARCH, клампится к границам
    кадра). Временна́я медиана глушит движущийся фон. Звезда почти всегда на якоре,
    но детект честный + мера уверенности (2-й пик). -> dict(score, peak2, cx, cy,
    dx, dy, W, H) или None если видео не читается."""
    frames, (W, H) = _sample_frames(path)
    if not frames:
        return None
    _, _, _, _, templ, HALF = _load_calib()
    ax, ay = W - ANCHOR_DX, H - ANCHOR_DY
    g = cv2.cvtColor(_median(frames), cv2.COLOR_BGR2GRAY).astype(np.float32)
    y0, y1 = max(0, ay - HALF - SEARCH), min(H, ay + HALF + SEARCH)
    x0, x1 = max(0, ax - HALF - SEARCH), min(W, ax + HALF + SEARCH)
    if (y1 - y0) < 2 * HALF or (x1 - x0) < 2 * HALF:      # кадр меньше окна
        return dict(score=0.0, peak2=0.0, cx=ax, cy=ay, dx=0, dy=0, W=W, H=H)
    reg = _highpass(g[y0:y1, x0:x1])
    T = _highpass((templ * 255.0).astype(np.float32))
    r = cv2.matchTemplate(reg, T, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(r)
    cx, cy = x0 + loc[0] + HALF, y0 + loc[1] + HALF
    rr = r.copy()                                         # 2-й пик вне окрестности лучшего
    yy, xx = loc[1], loc[0]
    rr[max(0, yy - 12):yy + 13, max(0, xx - 12):xx + 13] = -1.0
    _, peak2, _, _ = cv2.minMaxLoc(rr)
    return dict(score=float(score), peak2=float(peak2), cx=int(cx), cy=int(cy),
                dx=int(cx - ax), dy=int(cy - ay), W=W, H=H)


def _is_spark(info) -> bool:
    """presence-гейт: основной порог ИЛИ borderline (слабый score, но матч
    залип на якоре — offset ±OFF_TOL). Оверлей на фикс. угловом отступе:
    реальная искра всегда на якоре; ложный матч без искры уводит offset."""
    s, dx, dy = info["score"], info["dx"], info["dy"]
    primary = s >= PRESENCE_SCORE
    borderline = (BORDER_LO <= s < PRESENCE_SCORE and abs(dx) <= OFF_TOL and abs(dy) <= OFF_TOL)
    return bool(primary or borderline)


def _apply_center(info, W, H, HALF):
    """Центр применения un-blend: НАЙДЕННЫЙ (cx,cy), если детект уверен (сильный score
    И чёткое разделение пиков); иначе fallback на ЯКОРЬ. Клампится, чтобы окно 2*HALF
    целиком влезло в кадр. -> (cx, cy, confident)."""
    ax, ay = W - ANCHOR_DX, H - ANCHOR_DY
    confident = (info["score"] >= PRESENCE_SCORE and
                 info["peak2"] <= POS_TRUST_RATIO * info["score"])
    cx = info["cx"] if confident else ax
    cy = info["cy"] if confident else ay
    cx = min(max(cx, HALF), W - HALF)
    cy = min(max(cy, HALF), H - HALF)
    return cx, cy, confident


_BLUR_MASK = None
def _blur_mask():
    """Мягкая маска зоны блюра (кэш): звезда (Mfoot>0.3) + отступ + перо, на холсте
    2*RB. Ключ: доходит до НУЛЯ до края холста → блюр нигде не срезается ступенькой
    (в тесном окне 96×96 длинные лучи упирались в край → квадратный контур)."""
    global _BLUR_MASK
    if _BLUR_MASK is None:
        _, _, Mfoot, _, _, HALF = _load_calib()
        star = (Mfoot > 0.30).astype(np.uint8)
        canvas = np.zeros((2 * RB, 2 * RB), np.uint8)
        off = RB - HALF                                   # вклейка звезды по центру
        canvas[off:off + 2 * HALF, off:off + 2 * HALF] = star
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * BLUR_MARGIN + 1, 2 * BLUR_MARGIN + 1))
        soft = cv2.GaussianBlur(cv2.dilate(canvas, k).astype(np.float32), (0, 0), BLUR_FEATHER)
        _BLUR_MASK = np.clip(soft, 0, 1)
    return _BLUR_MASK


def _blur_over(out, cx, cy):
    """Гаусс-блюр поверх результата un-blend по мягкой маске в регионе 2*RB (маска
    целиком внутри, до нуля → без квадратного края). Растворяет остаточный контур на
    светлом/ровном фоне. Регион клампится к границам кадра. out меняется на месте."""
    if BLUR_SIGMA <= 0 or BLUR_K <= 0:
        return out
    mask = _blur_mask()
    H, W = out.shape[:2]
    y0, y1, x0, x1 = cy - RB, cy + RB, cx - RB, cx + RB
    my0, mx0 = max(0, -y0), max(0, -x0)                   # смещение среза маски
    y0c, y1c, x0c, x1c = max(0, y0), min(H, y1), max(0, x0), min(W, x1)
    reg = out[y0c:y1c, x0c:x1c].astype(np.float32)
    m = (mask[my0:my0 + (y1c - y0c), mx0:mx0 + (x1c - x0c)] * BLUR_K)[..., None]
    blur = cv2.GaussianBlur(reg, (0, 0), BLUR_SIGMA)
    out[y0c:y1c, x0c:x1c] = np.clip(reg * (1 - m) + blur * m, 0, 255).astype(np.uint8)
    return out


def _process_frame(frame, a, b, Mfoot, Mcore, Msm, HALF, cx, cy):
    out = frame.astype(np.float32)
    y0, y1, x0, x1 = cy - HALF, cy + HALF, cx - HALF, cx + HALF
    win = frame[y0:y1, x0:x1].astype(np.float32)
    lin = np.clip(a * win + b, 0, 255)                       # affine un-blend
    fp = Mfoot[..., None]
    res = win * (1 - fp) + lin * fp
    bil = cv2.bilateralFilter(res.astype(np.float32), 7, 45, 7)   # сгладить контур
    res = res * (1 - Msm[..., None]) + bil * Msm[..., None]
    plate = cv2.inpaint(np.clip(res, 0, 255).astype(np.uint8), Mcore, 9,
                        cv2.INPAINT_TELEA).astype(np.float32)      # rim-safe добивание
    pg = cv2.cvtColor(plate.astype(np.uint8), cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(pg, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(pg, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.GaussianBlur(np.sqrt(gx * gx + gy * gy), (0, 0), 2.0)
    edge = np.clip(grad / G0, 0, 1) ** 2
    ww = (Mfoot * (1 - edge))[..., None]
    fin = res * (1 - ww) + plate * ww
    out[y0:y1, x0:x1] = fin
    return np.clip(out, 0, 255).astype(np.uint8)


def _audio_md5(path):
    r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                        "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True, **no_console_kwargs())
    return r.stdout.strip()


# ------------------------------------------------------------------ public API
def has_watermark(path):
    """(есть ли искра FastGen, score детекта). Сперва — метаданный флаг lumz_wm=removed
    (bundled ffmpeg -i, ~9мс): есть → (False, 0.0) БЕЗ покадрового детекта; нет → детект
    по сэмплам (~24 кадра)."""
    if _read_meta_flag(path) in WM_FLAG_DONE:     # removed ИЛИ removed_v2 → уже чищен
        return (False, 0.0)
    info = _detect(path)
    if info is None:
        return (False, 0.0)
    return (_is_spark(info), round(info["score"], 3))


def remove_watermark(path) -> bool:
    """Убрать искру, заменив файл НА МЕСТЕ (то же имя/путь), атомарно (temp -> os.replace);
    оригинал уходит в системную Корзину через fs_utils.move_to_trash (откатываемо).

    Возвращает:
      • False — искры нет / ниже гейта (файл НЕ тронут);
      • False — не удалось положить оригинал в Корзину (файл НЕ заменён, temp удалён);
      • False — ошибка энкода / аудио-MD5 разошёлся (temp удалён);
      • True  — искра убрана, файл заменён.

    Кадры читаются ПОТОКОВО (open→read→process→write→next) — память O(1) кадров.
    Примечание: у клипа БЕЗ аудиодорожки _audio_md5 вернёт пустую строку с обеих
    сторон, поэтому проверка «аудио бит-в-бит» пройдёт формально (сохранять нечего) —
    это ожидаемо, поведение не меняем.
    """
    path = str(Path(path).expanduser())
    if _read_meta_flag(path) in WM_FLAG_DONE:
        return False                          # уже очищено (removed/removed_v2) — не трогаем
    info = _detect(path)
    if info is None or not _is_spark(info):
        return False

    a, b, Mfoot, Mcore, templ, HALF = _load_calib()
    Msm = np.clip(cv2.GaussianBlur(Mcore.astype(np.float32) / 255.0, (0, 0), 5.0) * 1.3, 0, 1)

    cap = _open_capture(path)
    if cap is None:
        return False
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    ok, first = cap.read()
    if not ok:
        cap.release()
        return False
    H, W = first.shape[:2]
    cx, cy, _conf = _apply_center(info, W, H, HALF)   # un-blend по НАЙДЕННОМУ центру (info из _detect)
    if cx - HALF < 0 or cy - HALF < 0 or cx + HALF > W or cy + HALF > H:
        cap.release()
        return False

    a_before = _audio_md5(path)
    tmp = path + ".wmtmp.mp4"
    errf = tempfile.TemporaryFile()          # stderr -> файл (НЕ PIPE) → без дедлока трубы
    cmd = [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps}",
           "-i", "-", "-i", path, "-map", "0:v:0", "-map", "1:a:0?",
           "-c:v", "libx264", "-crf", CRF, "-preset", "slow", "-pix_fmt", "yuv420p",
           "-c:a", "copy",
           # метка «обработано» в контейнер mp4 (udta); use_metadata_tags ОБЯЗАТЕЛЕН —
           # без него mp4-муксер дропает произвольные ключи (проверено).
           "-metadata", f"{WM_FLAG}={WM_FLAG_VALUE}", "-movflags", "use_metadata_tags",
           tmp]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=errf, **no_console_kwargs())
    try:
        p.stdin.write(_blur_over(_process_frame(first, a, b, Mfoot, Mcore, Msm, HALF, cx, cy), cx, cy).tobytes())
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            p.stdin.write(_blur_over(_process_frame(frame, a, b, Mfoot, Mcore, Msm, HALF, cx, cy), cx, cy).tobytes())
        p.stdin.close()
    except BrokenPipeError:
        pass
    finally:
        cap.release()
    rc = p.wait()

    if rc != 0 or not os.path.exists(tmp):
        try:
            errf.seek(0)
            msg = errf.read().decode(errors="ignore")[-500:]
            if msg.strip():
                sys.stderr.write(f"[watermark] ffmpeg rc={rc}: {msg}\n")
        except Exception:
            pass
        errf.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    errf.close()

    if _audio_md5(tmp) != a_before:          # аудио бит-в-бит (см. примечание в докстринге)
        os.remove(tmp)
        return False

    try:
        from fs_utils import move_to_trash
    except Exception:
        os.remove(tmp)
        return False
    if not move_to_trash(path):              # оригинал -> Корзина; не смог -> НЕ заменяем
        os.remove(tmp)
        return False
    os.replace(tmp, path)                    # атомарная замена в то же имя
    return True


def backfill_flag(path) -> bool:
    """Пометить УЖЕ очищенный файл флагом lumz_wm=removed БЕЗ пересжатия (remux `-c copy`).
    Для файлов, очищенных ДО появления метки (чтобы has_watermark по ним был мгновенным).
    Содержимое видео/аудио НЕ меняется (только контейнерный тег) → оригинал в Корзину НЕ
    отправляем, temp -> os.replace. Уже помечен → True (no-op). НЕ вызывать автоматически.

    Возвращает True при успехе (или уже помечен), False при ошибке/недоступности файла."""
    path = str(Path(path).expanduser())
    if not os.path.isfile(path):
        return False
    if _read_meta_flag(path) in WM_FLAG_DONE:
        return True                           # уже помечен (removed/removed_v2)
    tmp = path + ".wmflag.mp4"
    # backfill помечает СТАРО-очищенные файлы (до появления метки) → legacy-значение
    cmd = [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", path,
           "-map", "0", "-c", "copy",
           "-metadata", f"{WM_FLAG}={WM_FLAG_LEGACY}", "-movflags", "use_metadata_tags", tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, **no_console_kwargs())
        if r.returncode != 0 or not os.path.exists(tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
            return False
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, path)                      # то же содержимое → без Корзины
    return True


def _selftest_cli(path, outpath=None) -> int:
    """Хедлес self-test для build-verify: копирует клип в temp, прогоняет
    has_watermark + remove_watermark, печатает score/размеры/аудио/метку. Оригинал
    не трогает. Если задан outpath — обработанный файл копируется туда (для проверки
    блюра из собранного .app)."""
    import shutil
    src = Path(path).expanduser()
    if not src.is_file():
        print(f"[wm-selftest] нет файла: {src}")
        return 2
    td = tempfile.mkdtemp(prefix="wm_selftest_")
    work = os.path.join(td, src.name)
    shutil.copy2(str(src), work)
    try:
        print(f"[wm-selftest] ffmpeg={_ffmpeg_exe()}")
        has, score = has_watermark(work)
        before = os.path.getsize(work)
        a_before = _audio_md5(work)
        print(f"[wm-selftest] has_watermark={has} score={score}")
        ok = remove_watermark(work)
        after = os.path.getsize(work) if os.path.exists(work) else 0
        a_after = _audio_md5(work) if os.path.exists(work) else ""
        print(f"[wm-selftest] remove_watermark={ok} size {before}->{after} "
              f"audio {'OK' if a_after == a_before else 'MISMATCH'} flag={_read_meta_flag(work)}")
        if outpath and os.path.exists(work):
            shutil.copy2(work, str(Path(outpath).expanduser()))
            print(f"[wm-selftest] saved -> {outpath}")
        return 0 if (has and ok) else 1
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        raise SystemExit(_selftest_cli(sys.argv[1]))
    print("usage: python3 -m video.watermark <video.mp4>   (self-test)")
