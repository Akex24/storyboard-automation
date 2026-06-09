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


def set_root(root) -> None:
    """Переопределить project_root (writable) и пересобрать пути файлов.

    Вызывает GUI-обёртка из frozen .app, передавая project_root из QSettings,
    потому что в бандле Path(__file__).parent указывает в read-only _MEIPASS.
    CLI это не зовёт. Идемпотентно, потокобезопасно, не кидает."""
    global ROOT, KEYS_FILE, CURSOR_FILE, ENV_FILE, ACTIVE_FILE
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
    """idx ключа, выданного последним next_key(); None если ещё не выдавался."""
    return _last_index


def next_key() -> str:
    """Следующий ключ по кругу (round-robin).

    • 0 ключей → "" (не хуже текущего поведения при пустом ключе).
    • 1 ключ  → вернуть его, КУРСОР НЕ ТРОГАТЬ (1-в-1 с текущим кодом).
    • >1      → атомарно крутануть курсор и вернуть следующий.

    ЛЮБОЕ исключение → fallback на одиночный ключ (kill-switch): генерация
    НИКОГДА не падает из-за диспетчера."""
    try:
        keys = get_keys()
        if not keys:
            return ""
        if len(keys) == 1:
            # 1 ключ: курсор НЕ трогаем (ротация без изменений), idx=0.
            key, idx = keys[0], 0
        else:
            with _lock:
                idx = _advance_cursor(len(keys))
            key = keys[idx % len(keys)]
        # Мост лампочки — ПОСЛЕ выбора ключа. _write_active имеет
        # СОБСТВЕННЫЙ try/except → его ошибка не доходит сюда, `return
        # key` ниже выполнится в любом случае. Ветка 0/fallback сюда не идёт.
        _set_last_index(idx)
        _write_active(idx)
        return key
    except Exception:
        try:
            ks = get_keys()
            if ks:
                return ks[0]
        except Exception:
            pass
        return _read_env_first_line()


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
