# -*- coding: utf-8 -*-
"""
key_pool.py — диспетчер пула API-ключей Fast Gen (round-robin балансировка).

ЗАДАЧА А: до 5 ключей, выдача «следующий по кругу». БЕЗ failover (задача Б).

Чистый Python: НЕ импортирует PyQt и НЕ импортирует storyboard_app на уровне
модуля (иначе circular import — ср. _AppProxy паттерн в threads/*). Рантайм-
источник ключей — файл-мост `fastgen_keys.txt` в project_root (тот же канал,
что `image_provider.txt`), с fallback на первую строку `.env`.

РЕЗОЛВ ROOT — ВАЖНО (модуль импортируется из ДВУХ контекстов):
  • CLI (pipeline.py / generate_storyboards.py): этот файл КОПИРУЕТСЯ в
    project_root рядом с ними (sync + datas в .spec) и импортируется ОТТУДА,
    поэтому Path(__file__).parent == project_root (writable). Это НЕ cwd:
    cwd у запущенного .app обычно «/», полагаться на него нельзя.
  • GUI (frozen .app): storyboard_app импортирует key_pool из БАНДЛА
    (_MEIPASS, read-only) → Path(__file__).parent тут НЕ writable. Поэтому
    GUI-обёртка (storyboard_app) ОБЯЗАНА один раз вызвать set_root(
    <project_root из QSettings>) — это writable путь, куда GUI уже пишет
    image_provider.txt/.env. CLI set_root НЕ зовёт (его дефолт уже writable).

Дефолт ROOT = Path(__file__).resolve().parent — верен для dev и CLI;
для frozen GUI переопределяется через set_root().
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import List

MAX_KEYS = 5

# Внутрипроцессный лок: 4 GUI-потока генерации живут в ОДНОМ процессе и могут
# крутить курсор параллельно. Межпроцессный лок НЕ делаем (best-effort по
# плану: худший случай — два запроса возьмут один ключ, лёгкий перекос, не краш).
_lock = threading.Lock()

# idx ключа, выданного последним next_key() — для прямого UI-сигнала лампочки
# (GUI-поток читает через last_index() и эмитит сигнал). Обновляется в next_key.
_last_index = None

ROOT: Path = Path(__file__).resolve().parent
KEYS_FILE: Path = ROOT / "fastgen_keys.txt"
CURSOR_FILE: Path = ROOT / ".fastgen_keys_cursor"
ENV_FILE: Path = ROOT / ".env"
# Файл-мост для UI-лампочки round-robin: какой индекс ключа только что
# отдан. Пишется в next_key, слушается GUI через QFileSystemWatcher.
ACTIVE_FILE: Path = ROOT / ".fastgen_keys_active"
# 2026-06-09 (задача Б, Этап 1): failover. Файл выведенных из ротации ключей
# (cross-process: пишет GUI, читают и GUI и CLI синхронно в next_key). Строка
# на ключ: "<idx> <reason> <until_epoch>", reason ∈ temp|perm, until=0 для perm.
DISABLED_FILE: Path = ROOT / ".fastgen_keys_disabled"
# Сколько держать temp-выбитый ключ (429/лимит) вне ротации, сек (15 мин).
DISABLE_TEMP_TTL = 900


def set_root(root) -> None:
    """Переопределить project_root (writable) и пересобрать пути файлов.

    Вызывает GUI-обёртка из frozen .app, передавая project_root из QSettings,
    потому что в бандле Path(__file__).parent указывает в read-only _MEIPASS.
    CLI это не зовёт. Идемпотентно, потокобезопасно, не кидает."""
    global ROOT, KEYS_FILE, CURSOR_FILE, ENV_FILE, ACTIVE_FILE, DISABLED_FILE
    if not root:
        return
    try:
        p = Path(root)
    except Exception:
        return
    with _lock:
        ROOT = p
        KEYS_FILE = p / "fastgen_keys.txt"
        CURSOR_FILE = p / ".fastgen_keys_cursor"
        ENV_FILE = p / ".env"
        ACTIVE_FILE = p / ".fastgen_keys_active"
        DISABLED_FILE = p / ".fastgen_keys_disabled"


def _read_env_first_line() -> str:
    """Первая непустая строка .env = primary-ключ (как load_key сегодня)."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s:
                return s
    except Exception:
        pass
    return ""


def get_keys() -> List[str]:
    """Все настроенные ключи по порядку.

    Приоритет: сайдкар fastgen_keys.txt (по строке на ключ, пустые отброшены,
    максимум MAX_KEYS) → fallback на первую строку .env (= текущее одиночное
    поведение). Никогда не кидает: при любой ошибке → [] или [.env-ключ]."""
    keys: List[str] = []
    try:
        if KEYS_FILE.exists():
            for line in KEYS_FILE.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s:
                    keys.append(s)
                if len(keys) >= MAX_KEYS:
                    break
    except Exception:
        keys = []
    if keys:
        return keys
    single = _read_env_first_line()
    return [single] if single else []


def key_count() -> int:
    """Сколько ключей реально настроено (для UI/диагностики)."""
    return len(get_keys())


def _read_cursor() -> int:
    try:
        return int((CURSOR_FILE.read_text(encoding="utf-8").strip() or "0"))
    except Exception:
        return 0


def _write_atomic(path: Path, text: str) -> None:
    """Atomic write: temp {name}.tmp.<PID> + os.replace (POSIX/Win-atomic
    rename — convention проекта, ARCHITECTURE 'Atomic write'). PID-суффикс
    защищает от параллельных писателей."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _advance_cursor(n: int) -> int:
    """Прочитать курсор, вернуть текущий индекс (cur % n), записать cur+1.

    Вызывается ТОЛЬКО под _lock. Курсор — монотонный счётчик (модуль на чтении
    через % n), файл растёт медленно. Если запись курсора не удалась — не
    падаем: в следующий раз вернётся тот же idx (лёгкий перекос, не краш)."""
    cur = _read_cursor()
    idx = cur % n
    try:
        _write_atomic(CURSOR_FILE, str(cur + 1))
    except Exception:
        pass
    return idx


def _write_active(idx: int) -> None:
    """Файл-мост для UI-лампочки round-robin (КОСМЕТИКА).

    Пишет "{idx} {nonce}" в ACTIVE_FILE (разделитель — пробел). nonce
    (time.time()) гарантирует смену содержимого даже при том же idx
    (например 1 ключ) → watcher в GUI срабатывает на каждой выдаче.
    СОБСТВЕННЫЙ try/except: любая ошибка записи проглатывается и НЕ
    влияет на выдачу ключа."""
    try:
        tmp = ACTIVE_FILE.with_name(f"{ACTIVE_FILE.name}.tmp.{os.getpid()}")
        tmp.write_text(f"{idx} {time.time()}", encoding="utf-8")
        os.replace(tmp, ACTIVE_FILE)
    except Exception:
        pass


def _set_last_index(idx):
    """Запомнить idx последней выдачи ключа (для прямого UI-сигнала лампочки).
    СОБСТВЕННЫЙ try/except — поломка НЕ влияет на выдачу ключа."""
    global _last_index
    try:
        _last_index = idx
    except Exception:
        pass


def last_index():
    """LEGACY (2026-06-09): idx ключа, выданного последним next_key(), через
    ГЛОБАЛ `_last_index`. Оставлено для CLI-лампочки-watcher (`.fastgen_keys_active`
    мост). GUI/потоки idx больше отсюда НЕ читают — он racy под Mode C (глобал
    перезаписывается параллельными потоками между выдачей и чтением); вместо
    этого берут idx напрямую из `next_key_with_idx()`. Не удалять."""
    return _last_index


def _read_disabled() -> dict:
    """Прочитать выведенные из ротации ключи → {idx: (reason, until)}.

    Строка файла: "<idx> <reason> <until_epoch>". reason ∈ temp|perm.
    Ленивый TTL: temp-записи с until<=now считаются истёкшими (ключ вернулся
    в ротацию) — отбрасываются И файл переписывается без них. perm живут до
    save_keys. Малформ-строки молча пропускаются. На любой ошибке → {}.
    Cross-process: читается синхронно и GUI, и CLI (без watcher)."""
    out: dict = {}
    try:
        if not DISABLED_FILE.exists():
            return {}
        now = time.time()
        changed = False
        for line in DISABLED_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 3:
                changed = changed or bool(line.strip())
                continue
            try:
                idx = int(parts[0])
                reason = parts[1]
                until = float(parts[2])
            except Exception:
                changed = True
                continue
            if reason == "temp" and until <= now:
                changed = True  # истёк → не возвращаем в out, перепишем файл
                continue
            out[idx] = (reason, until)
        if changed:
            try:
                _write_disabled(out)
            except Exception:
                pass
    except Exception:
        return {}
    return out


def _write_disabled(disabled: dict) -> None:
    """Атомарно записать карту выбитых ключей. Пустая карта → удалить файл
    (возврат к «всё в ротации»). Под _lock у вызывающего НЕ требуется —
    сам не трогает курсор. Не кидает."""
    try:
        if not disabled:
            try:
                if DISABLED_FILE.exists():
                    DISABLED_FILE.unlink()
            except Exception:
                pass
            return
        lines = [f"{idx} {reason} {until}"
                 for idx, (reason, until) in sorted(disabled.items())]
        _write_atomic(DISABLED_FILE, "\n".join(lines) + "\n")
    except Exception:
        pass


def disable_key(idx, reason, ttl_seconds=DISABLE_TEMP_TTL) -> None:
    """Вывести ключ idx из ротации. reason: 'temp' (429/лимит — вернётся через
    ttl_seconds) или 'perm' (401/403/license — до ручного save_keys).

    perm перекрывает temp (если ключ уже temp, а пришёл perm — станет perm).
    Повторный temp продлевает until. idx=None / некорректный reason → no-op.
    Не кидает (kill-switch: failover НИКОГДА не роняет генерацию)."""
    try:
        if idx is None or reason not in ("temp", "perm"):
            return
        idx = int(idx)
        with _lock:
            disabled = _read_disabled()
            prev = disabled.get(idx)
            if prev and prev[0] == "perm" and reason == "temp":
                return  # perm уже стоит — temp не понижает
            until = 0.0 if reason == "perm" else (time.time() + float(ttl_seconds))
            disabled[idx] = (reason, until)
            _write_disabled(disabled)
    except Exception:
        pass


def next_key_with_idx() -> tuple:
    """Следующий ключ по кругу (round-robin) ВМЕСТЕ с его idx → (key, idx).

    2026-06-09 (фикс racy-idx): idx возвращается НАПРЯМУЮ вызывающему — каждый
    поток получает СВОЙ idx в одни руки, без чтения общего `_last_index`
    (который перезаписывался параллельными Mode C-потоками → чужой idx в
    лампочке И в failover-выбивании). `next_key()` ниже — тонкая обёртка.

    • 0 ключей → ("", None).
    • 1 ключ  → (key, 0), КУРСОР НЕ ТРОГАТЬ (1-в-1 с прежним кодом).
    • >1      → атомарно крутануть курсор, (key, real_idx).

    ЛЮБОЕ исключение → fallback (kill-switch): (key, None) — idx не
    атрибутируем (фолбэк-ключ вне idx-пространства пула). None у downstream:
    лампочка не мигает (guard), disable_key(None)=no-op (НЕ выбьет чужой ключ).
    Генерация НИКОГДА не падает из-за диспетчера."""
    try:
        keys = get_keys()
        if not keys:
            return "", None
        if len(keys) == 1:
            # 1 ключ: курсор НЕ трогаем (ротация без изменений), idx=0.
            # Даже если он выбит — отдаём (graceful: лучше дёрнуть, чем "").
            key, idx = keys[0], 0
        else:
            # 2026-06-09 (задача Б): отсев выведенных из ротации ключей.
            # Если выбиты ВСЕ — фолбэк на полный список (пробуем всё равно,
            # не возвращаем пустоту). Курсор крутится по живому подмножеству.
            disabled = _read_disabled()
            live = [i for i in range(len(keys)) if i not in disabled]
            if not live:
                live = list(range(len(keys)))
            with _lock:
                pos = _advance_cursor(len(live))
            idx = live[pos % len(live)]
            key = keys[idx]
        # Мост лампочки — ПОСЛЕ выбора ключа. _write_active имеет
        # СОБСТВЕННЫЙ try/except → его ошибка не доходит сюда, return
        # ниже выполнится в любом случае. Ветка 0/fallback сюда не идёт.
        # _set_last_index — LEGACY для CLI-watcher (GUI берёт idx из return).
        _set_last_index(idx)
        _write_active(idx)
        return key, idx
    except Exception:
        try:
            ks = get_keys()
            if ks:
                return ks[0], None
        except Exception:
            pass
        return _read_env_first_line(), None


def next_key() -> str:
    """Тонкая обёртка над next_key_with_idx() — возвращает ТОЛЬКО ключ (строку).

    Контракт сохранён для CLI (`pipeline.py`/`generate_storyboards.py`:
    `key = next_key() or load_key()`), которому idx не нужен."""
    return next_key_with_idx()[0]


def save_keys(keys) -> None:
    """Атомарно записать сайдкар fastgen_keys.txt (по строке на ключ).

    Пустые отбрасывает, максимум MAX_KEYS. При пустом списке — удаляет сайдкар
    (возврат к .env-поведению). Сбрасывает курсор в 0, чтобы новый набор
    начинался с первого ключа. Не кидает."""
    try:
        cleaned: List[str] = []
        for k in (keys or []):
            s = (k or "").strip()
            if s:
                cleaned.append(s)
            if len(cleaned) >= MAX_KEYS:
                break
        with _lock:
            # 2026-06-09 (задача Б): ручное обновление ключей снимает ВСЕ
            # выбивания (в т.ч. perm/license) — новый набор пробуется с нуля.
            try:
                if DISABLED_FILE.exists():
                    DISABLED_FILE.unlink()
            except Exception:
                pass
            if not cleaned:
                try:
                    if KEYS_FILE.exists():
                        KEYS_FILE.unlink()
                except Exception:
                    pass
                return
            _write_atomic(KEYS_FILE, "\n".join(cleaned) + "\n")
            try:
                _write_atomic(CURSOR_FILE, "0")
            except Exception:
                pass
    except Exception:
        pass
