# -*- coding: utf-8 -*-
"""
threads/update.py — потоки обновлений и стат-фетча.

Содержит 5 классов QThread:
    - CheckUpdateThread        — проверка новых версий на GitHub
    - DownloadUpdateThread     — скачивание ZIP проекта
    - DownloadAppUpdateThread  — скачивание .app из Releases
    - SendUpdateThread         — git push + загрузка .app в Release (admin)
    - FetchStatsThread         — статистика скачиваний (admin)

КРУГОВОЙ ИМПОРТ: эти треды используют helpers и константы из storyboard_app.py
(github_configured, version_gt, GITHUB_BRANCH, и т.д.). При этом storyboard_app
импортирует эти треды.

ПЕРВАЯ ПОПЫТКА (НЕ РАБОТАЕТ В PYINSTALLER): `import storyboard_app as _sa`
на module-level — работает в обычном Python через sys.modules cache, но
PyInstaller's frozen loader делает re-exec_module и падает с ImportError.

РАБОЧЕЕ РЕШЕНИЕ: lazy proxy `_sa = _AppProxy()`. При обращении к `_sa.X`
прокси делает `import storyboard_app` (на этот момент модуль уже полностью
загружен — `run()` вызывается из Qt event-loop'а, после `MainWindow.show()`),
и возвращает атрибут. Никаких импортов на module-level, никаких циклов.

История: вытащено из storyboard_app.py 2026-05-04 (был блок «Обновления — потоки»).
Lazy-proxy фикс — там же, после первого падения в PyInstaller-сборке.
"""

from __future__ import annotations

import io
import os
import sys
import json
import time
import datetime
import subprocess
import shutil
import zipfile
import tempfile
from pathlib import Path

import requests

from PyQt6.QtCore import QThread, pyqtSignal


class _AppProxy:
    """Прокси к module storyboard_app. Импорт ленивый — происходит только
    при первом обращении к атрибуту (внутри `run()` тредов).

    В PyInstaller-сборке storyboard_app.py запускается как `__main__`,
    а отдельный `storyboard_app` модуль может быть ВТОРЫМ instance с
    неинициализированными global'ами (ENV_FILE=Path(), PROMPTS_DIR=Path()).
    Чтобы треды видели актуальное состояние из MainWindow, ищем сначала
    в sys.modules['__main__'] (бандл) и только потом fallback на
    `import storyboard_app` (для dev / smoke-тестов).
    """
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        # В bundled .app __main__ — это и есть storyboard_app. В dev __main__
        # может быть pytest/python REPL, у которого нет наших атрибутов —
        # тогда fallback на 'storyboard_app'.
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app  # dev / smoke-test path
        return getattr(storyboard_app, name)


_sa = _AppProxy()


class CheckUpdateThread(QThread):
    """Проверяет наличие новых версий проекта и приложения на GitHub."""
    # curr_proj, latest_proj, curr_app, latest_app
    update_found = pyqtSignal(str, str, str, str)
    no_update    = pyqtSignal()
    error        = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            if not _sa.github_configured():
                self.no_update.emit()
                return

            curr_proj = _sa.read_local_version(self.root)
            curr_app  = _sa.read_local_app_version(self.root)

            r = requests.get(_sa.github_raw_url("version.json"), timeout=10)
            r.raise_for_status()
            latest_proj = r.json().get("version", curr_proj)

            latest_app = _sa.fetch_latest_app_release_version() or curr_app

            if _sa.version_gt(latest_proj, curr_proj) or _sa.version_gt(latest_app, curr_app):
                self.update_found.emit(curr_proj, latest_proj, curr_app, latest_app)
            else:
                self.no_update.emit()
        except Exception as e:
            self.error.emit(str(e))


class DownloadUpdateThread(QThread):
    """Скачивает и применяет обновление с GitHub."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str)   # new version
    error    = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            self.progress.emit("Скачиваю обновление…", 5)
            r = requests.get(_sa.github_zip_url(), timeout=120, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            buf = io.BytesIO()
            done = 0
            for chunk in r.iter_content(chunk_size=16384):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 5 + int(done / total * 60)
                    self.progress.emit(f"Скачиваю… {done // 1024} КБ", pct)

            self.progress.emit("Распаковка…", 70)
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(buf) as z:
                    z.extractall(tmp)
                extracted = next(Path(tmp).iterdir())  # первая (единственная) папка

                self.progress.emit("Применяю изменения…", 85)
                copied = 0
                # Собираем relative paths для actors/ из zip — нужно
                # для зеркалирования (см. ниже).
                zip_actors_relpaths = set()
                for src in extracted.rglob("*"):
                    if not src.is_file():
                        continue
                    rel = src.relative_to(extracted)
                    if rel.parts and rel.parts[0] in _sa.PRESERVE_ON_UPDATE:
                        continue
                    if rel.parts and rel.parts[0] == "actors":
                        zip_actors_relpaths.add(rel)
                    dst = self.root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1

                # Зеркалирование `actors/`: админ — единственный кто
                # управляет актёрами. Если он удалил кого-то локально и
                # отправил обновление, у коллег этот актёр должен исчезнуть.
                # Логика: для каждого файла в локальной actors/ — если
                # его НЕТ в zip → удаляем. Пустые папки тоже чистим.
                #
                # Защита: делаем зеркалирование ТОЛЬКО если в zip есть
                # хоть один файл в actors/. Иначе (пустой zip / старая
                # версия без actors) — не трогаем локальные данные, чтобы
                # не выкосить всё на ровном месте.
                deleted = 0
                local_actors_root = self.root / "actors"
                if zip_actors_relpaths and local_actors_root.is_dir():
                    self.progress.emit("Синхронизация актёров…", 92)
                    # Удаляем файлы которых нет в zip
                    for local_file in list(local_actors_root.rglob("*")):
                        if not local_file.is_file():
                            continue
                        rel = local_file.relative_to(self.root)
                        if rel not in zip_actors_relpaths:
                            try:
                                local_file.unlink()
                                deleted += 1
                            except Exception:
                                pass
                    # Чистим пустые папки актёров (actors/<slug>/)
                    for slug_dir in list(local_actors_root.iterdir()):
                        if slug_dir.is_dir():
                            try:
                                if not any(slug_dir.iterdir()):
                                    slug_dir.rmdir()
                            except Exception:
                                pass

            new_version = _sa.read_local_version(self.root)
            msg = f"Обновлено! ({copied} файлов)"
            if deleted:
                msg += f", удалено {deleted} файлов актёров"
            self.progress.emit(msg, 100)
            self.finished.emit(new_version)
        except Exception as e:
            self.error.emit(str(e))


class DownloadAppUpdateThread(QThread):
    """Скачивает и устанавливает новую версию Storyboard Studio.app из GitHub Releases."""
    progress = pyqtSignal(str, int)
    finished = pyqtSignal(str, str)   # (new_app_version, install_path)
    error    = pyqtSignal(str)

    # 2026-05-12 (v1.0.53): системные пути на Windows куда update_dir
    # никогда не должен попасть. Если tempfile.gettempdir() вернул что-то
    # из этого списка (как было с C:\Windows\System32 в v1.0.39 у админа
    # запущенного из admin PowerShell) — fallback на LOCALAPPDATA.
    _WIN_SYSTEM_PATH_PREFIXES = (
        r'c:\windows',
        r'c:\program files',
        r'c:\program files (x86)',
        r'c:\programdata',
    )

    def __init__(self, target_version: str, root: Path):
        super().__init__()
        self.target_version = target_version
        self.root = root
        self._early_log_path = None  # установится в run() как можно раньше

    # ─────────────────────────────────────────────────────────────────
    # 2026-05-12 (v1.0.53): раннее логирование в гарантированно
    # безопасное место. Пишется в %LOCALAPPDATA%\StoryboardStudio\logs\
    # на Win, ~/Library/Logs/StoryboardStudio/ на Mac. Используется
    # для диагностики когда update_dir не создалась / network упал /
    # любой exception ДО создания bootstrap.log.
    # ─────────────────────────────────────────────────────────────────

    def _open_early_log(self) -> Path:
        """Открывает (создаёт) файл раннего лога. Возвращает Path.
        Не валит pipeline если не получилось — возвращает None."""
        try:
            if sys.platform == 'win32':
                base = Path(os.environ.get('LOCALAPPDATA', '')
                            or (Path.home() / 'AppData' / 'Local'))
                log_dir = base / 'StoryboardStudio' / 'logs'
            else:
                log_dir = Path.home() / 'Library' / 'Logs' / 'StoryboardStudio'
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir / f"update_{self.target_version}.log"
        except Exception:
            return None

    def _early_log(self, msg: str) -> None:
        """Append строку в файл раннего лога. Безопасно — не валит pipeline."""
        try:
            if self._early_log_path is None:
                return
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self._early_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{ts}  {msg}\n")
        except Exception:
            pass

    def _choose_update_dir(self) -> Path:
        """Возвращает безопасный путь для рабочей папки обновления.

        2026-05-12 (v1.0.53): фикс кейса когда update_dir попадал в
        C:\\Windows\\System32 (см. _WIN_SYSTEM_PATH_PREFIXES). Раньше
        использовалось tempfile.gettempdir() напрямую — на Win её
        последний fallback это os.getcwd(), что может быть System32
        если Studio запущена из admin PowerShell без явного -WorkingDirectory.

        Логика:
          • На Win: пытаемся %LOCALAPPDATA%\\Temp\\StoryboardStudio.
            Если LOCALAPPDATA сломан / не задан → fallback на
            %USERPROFILE%\\AppData\\Local\\Temp\\StoryboardStudio.
            Если и этот путь оказался в системной папке → exception.
          • На Mac: gettempdir() возвращает /var/folders/... — безопасно,
            используем как раньше.
        """
        if sys.platform == 'win32':
            localapp = os.environ.get('LOCALAPPDATA', '')
            if localapp:
                base = Path(localapp) / 'Temp' / 'StoryboardStudio'
            else:
                base = (Path.home() / 'AppData' / 'Local' / 'Temp'
                        / 'StoryboardStudio')
            # Защита: если получился путь внутри системной папки —
            # тоже отказываемся и берём %USERPROFILE%\AppData fallback.
            base_lower = str(base).lower().replace('/', '\\')
            for prefix in self._WIN_SYSTEM_PATH_PREFIXES:
                if base_lower.startswith(prefix):
                    base = (Path.home() / 'AppData' / 'Local' / 'Temp'
                            / 'StoryboardStudio')
                    break
            # Финальная проверка: всё ещё в системной папке — raise.
            final_lower = str(base).lower().replace('/', '\\')
            for prefix in self._WIN_SYSTEM_PATH_PREFIXES:
                if final_lower.startswith(prefix):
                    raise RuntimeError(
                        f"Не удалось выбрать безопасный путь для обновления — "
                        f"все варианты ведут в системную папку. "
                        f"Проверь env-переменные LOCALAPPDATA / USERPROFILE.")
        else:
            # Mac: gettempdir() возвращает /var/folders/... — пользовательский путь.
            base = Path(tempfile.gettempdir()) / 'StoryboardStudio'
        return base / f"storyboard_update_{self.target_version}_{os.getpid()}"

    def _sync_actors_snapshot(self) -> None:
        """Тихая синхронизация папки `actors/` на стороне коллеги.

        Качает release-asset `actors-snapshot-v<target>.zip` (если есть),
        распаковывает в отдельный tempdir, и зеркалит slug-папки + actors.json
        в локальный `self.root / "actors"`.

        Что ЗЕРКАЛИТСЯ:
          - actors/actors.json (целиком — поле `roles` затирается, это
            продуктовое решение)
          - actors/<slug>/ (фото актёров)
          - Slug удалённый у админа — удаляется и у коллеги

        Что НЕ ТРОГАЕТСЯ:
          - actors/_textures/ — глобальные текстуры коллеги
          - actors/_* — любые служебные папки начинающиеся с _
          - actors/.DS_Store / Thumbs.db
          - Файлы и папки которые в локальной actors/ есть но НЕ
            упоминаются в zip (если admin их явно не удалил — оставляем)

        Failure modes:
          - Asset не найден в Release: log + return (старый Send Update
            без этой фичи — нормально, актёры останутся как есть).
          - Скачивание упало / zip битый: log + return, local actors/
            не тронут.
          - Apply одного slug упал (Defender lock и т.п.): retry 3×
            с 200ms задержкой, потом skip slug, продолжаем остальные.
          - Любая uncaught exception ловится в caller (run()), не валит
            основной .app update.

        Защита от self-sync для админа:
          Если у юзера есть `.git/` директория — это админ с source
          репозитория, его actors/ — source of truth, не трогаем.
        """
        # ── 0. Guard для админа ──
        if (self.root / '.git').is_dir():
            self._early_log(
                "[actors_sync] admin detected (.git exists), skipping")
            return

        # ── 1. Найти actors-snapshot asset ──
        self._early_log(
            f"[actors_sync] start, target=v{self.target_version}")
        asset = _sa.fetch_release_asset_by_name(
            self.target_version, "actors-snapshot")
        if not asset:
            self._early_log(
                "[actors_sync] asset 'actors-snapshot' not found in release, "
                "skipping (старый Send Update без этой фичи — норма)")
            return
        a_name = asset.get('name', '?')
        a_size = asset.get('size', 0)
        a_url = asset.get('browser_download_url')
        if not a_url:
            self._early_log(
                f"[actors_sync] asset has no download_url, skipping: {a_name}")
            return
        self._early_log(
            f"[actors_sync] asset found: {a_name}, size={a_size} bytes")

        # ── 2. Скачать zip в память ──
        try:
            r = requests.get(a_url, timeout=300, stream=True)
            r.raise_for_status()
            zip_buf = io.BytesIO()
            done = 0
            for chunk in r.iter_content(chunk_size=65536):
                zip_buf.write(chunk)
                done += len(chunk)
            self._early_log(
                f"[actors_sync] downloaded {done} bytes")
        except Exception as e:
            self._early_log(
                f"[actors_sync] download failed: {type(e).__name__}: {e}")
            return

        # ── 3. Валидировать и распаковать в отдельный tempdir ──
        actors_tmp = (Path(tempfile.gettempdir())
                      / "StoryboardStudio"
                      / f"actors_snapshot_{self.target_version}_{os.getpid()}")
        try:
            if actors_tmp.exists():
                shutil.rmtree(actors_tmp, ignore_errors=True)
            actors_tmp.mkdir(parents=True, exist_ok=True)
            zip_buf.seek(0)
            if not zipfile.is_zipfile(zip_buf):
                self._early_log(
                    "[actors_sync] downloaded file is not a valid zip, skipping")
                return
            zip_buf.seek(0)
            with zipfile.ZipFile(zip_buf) as z:
                # Sanity: проверим что внутри есть верхняя папка `actors/`.
                names = z.namelist()
                if not any(n.startswith('actors/') for n in names):
                    self._early_log(
                        "[actors_sync] zip has no 'actors/' prefix, skipping")
                    return
                z.extractall(actors_tmp)
            self._early_log(
                f"[actors_sync] zip extracted to {actors_tmp}")
        except Exception as e:
            self._early_log(
                f"[actors_sync] extract failed: {type(e).__name__}: {e}")
            try:
                if actors_tmp.exists():
                    shutil.rmtree(actors_tmp, ignore_errors=True)
            except Exception:
                pass
            return

        # ── 4. Подсчитать дельту: что заменять, что удалять ──
        local_actors = self.root / "actors"
        new_actors = actors_tmp / "actors"
        if not new_actors.is_dir():
            self._early_log(
                "[actors_sync] zip extracted but no 'actors/' folder inside, "
                "skipping")
            try:
                shutil.rmtree(actors_tmp, ignore_errors=True)
            except Exception:
                pass
            return
        local_actors.mkdir(parents=True, exist_ok=True)

        def _is_protected(name: str) -> bool:
            """Имена которые НЕ синкаются (защищены)."""
            if not name:
                return True
            if name.startswith('_'):
                return True
            if name in ('.DS_Store', 'Thumbs.db'):
                return True
            return False

        zip_slugs = {p.name for p in new_actors.iterdir()
                     if p.is_dir() and not _is_protected(p.name)}
        # Страховка: если в zip 0 slug-папок — что-то сломалось при упаковке.
        # НЕ удаляем локальных актёров, abort sync.
        if not zip_slugs:
            self._early_log(
                "[actors_sync] zip contains 0 slug folders — abort to prevent "
                "data loss on coworker's side")
            try:
                shutil.rmtree(actors_tmp, ignore_errors=True)
            except Exception:
                pass
            return
        local_slugs = {p.name for p in local_actors.iterdir()
                       if p.is_dir() and not _is_protected(p.name)}
        to_replace = zip_slugs
        to_delete = local_slugs - zip_slugs
        self._early_log(
            f"[actors_sync] zip_slugs={sorted(zip_slugs)}, "
            f"local_slugs={sorted(local_slugs)}, "
            f"to_replace={len(to_replace)}, to_delete={len(to_delete)}")

        # ── 5. Apply (retry 3× с 200ms на каждом slug) ──
        def _retry(fn, what: str, slug: str) -> bool:
            for attempt in range(1, 4):
                try:
                    fn()
                    if attempt > 1:
                        self._early_log(
                            f"[actors_sync] {what} {slug}: OK on attempt {attempt}")
                    return True
                except Exception as e:
                    self._early_log(
                        f"[actors_sync] {what} {slug}: attempt {attempt}/3 "
                        f"failed: {type(e).__name__}: {e}")
                    time.sleep(0.2)
            return False

        replaced = 0
        deleted = 0
        skipped = 0
        # Заменяем каждый slug
        for slug in sorted(to_replace):
            src = new_actors / slug
            dst = local_actors / slug

            def _do_replace(_src=src, _dst=dst):
                if _dst.exists():
                    shutil.rmtree(_dst)
                shutil.copytree(_src, _dst)
            if _retry(_do_replace, "replace", slug):
                replaced += 1
            else:
                skipped += 1
        # Удаляем slug-папки которых нет в zip
        for slug in sorted(to_delete):
            dst = local_actors / slug

            def _do_delete(_dst=dst):
                shutil.rmtree(_dst)
            if _retry(_do_delete, "delete", slug):
                deleted += 1
            else:
                skipped += 1
        # actors.json — целиком копируем (поле `roles` затирается по дизайну)
        zip_json = new_actors / "actors.json"
        if zip_json.is_file():
            dst_json = local_actors / "actors.json"

            def _do_copy_json(_src=zip_json, _dst=dst_json):
                shutil.copy2(_src, _dst)
            if not _retry(_do_copy_json, "copy_json", "actors.json"):
                skipped += 1

        # ── 6. Cleanup tempdir ──
        try:
            shutil.rmtree(actors_tmp, ignore_errors=True)
        except Exception:
            pass

        self._early_log(
            f"[actors_sync] DONE: replaced={replaced}, "
            f"deleted={deleted}, skipped={skipped}")

    def run(self):
        """Скачивает новую версию + создаёт bootstrap-скрипт + запускает его.

        2026-05-08 (Шаг 2): переход на bootstrap-логику. Раньше Thread
        пытался подменить .exe пока Studio запущена — на Win это падало
        с PermissionError (Windows блокирует удаление запущенного .exe)
        и был fallback в Downloads, юзер должен был руками копировать.

        Теперь:
          1. Качаем zip → распаковываем в постоянную temp папку
             (не TempDirectory — она удалится при выходе Thread'а).
          2. Создаём bootstrap-скрипт (.bat на Win, .sh на Mac) который:
             - Ждёт пока процесс Studio (с известным PID) умрёт.
             - Подменяет .exe/.app на новый.
             - Запускает обновлённый.
             - Удаляет себя и временные файлы.
          3. Пишем «pending_version.txt» рядом с version.json — Studio
             при следующем старте обновит version.json[app_version].
          4. Запускаем bootstrap detached (он переживёт смерть Studio).
          5. emit finished → caller вызывает QApplication.quit().
          6. Bootstrap делает свою работу → юзер видит новую Studio.
        """
        # 2026-05-12 (v1.0.53): максимально раннее логирование диагностики.
        # Файл всегда в %LOCALAPPDATA%\StoryboardStudio\logs\ (Win) или
        # ~/Library/Logs/StoryboardStudio/ (Mac) — независимо от того
        # куда tempfile.gettempdir() уйдёт. Юзер при failure обновления
        # увидит точный путь к этому логу в popup'е и сможет переслать.
        self._early_log_path = self._open_early_log()
        try:
            self._early_log(
                f"=== DownloadAppUpdateThread.run() start ===")
            self._early_log(
                f"target_version={self.target_version}, "
                f"sys.platform={sys.platform}")
            self._early_log(
                f"sys.executable={sys.executable}")
            self._early_log(
                f"os.getcwd()={os.getcwd()}")
            self._early_log(
                f"tempfile.gettempdir()={tempfile.gettempdir()}")
            self._early_log(
                f"env LOCALAPPDATA={os.environ.get('LOCALAPPDATA', '<unset>')}")
            self._early_log(
                f"env TEMP={os.environ.get('TEMP', '<unset>')}, "
                f"TMP={os.environ.get('TMP', '<unset>')}")
        except Exception:
            pass

        try:
            self.progress.emit("Ищу релиз на GitHub…", 5)
            self._early_log("fetching release asset info...")
            asset = _sa.fetch_release_asset_info(self.target_version)
            if not asset:
                msg = (f"Не найден .zip в релизе app-v{self.target_version}.\n"
                       "Попробуй обновить вручную — скачай с GitHub Releases.")
                self._early_log(f"FAIL: {msg}")
                self.error.emit(msg)
                return
            self._early_log(
                f"asset found: name={asset.get('name','?')}, "
                f"size={asset.get('size', 0)}")

            download_url = asset["browser_download_url"]
            size_bytes   = asset.get("size", 0)
            size_mb      = max(1, size_bytes // (1024 * 1024))

            self.progress.emit(f"Скачиваю приложение ({size_mb} МБ)…", 8)
            r = requests.get(download_url, timeout=600, stream=True)
            r.raise_for_status()
            total = int(r.headers.get("content-length", size_bytes))
            buf   = io.BytesIO()
            done  = 0
            for chunk in r.iter_content(chunk_size=65536):
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = 8 + int(done / total * 60)
                    self.progress.emit(
                        f"Скачиваю… {done // (1024*1024)} / {total // (1024*1024)} МБ", pct)

            self.progress.emit("Распаковка…", 70)

            is_win = (sys.platform == 'win32')

            # Куда подменять (target_path = папка onedir на Win или .app
            # bundle на Mac). 2026-05-08: Studio на Win переключена на
            # onedir (папка с .exe + _internal/). Bootstrap подменяет
            # ВСЮ папку, не один файл.
            if is_win:
                if not getattr(sys, 'frozen', False):
                    self.error.emit(
                        "Авто-обновление работает только из собранного .exe.\n"
                        "В dev-режиме обновись через GitHub Releases вручную.")
                    return
                # sys.executable = «…\Storyboard Studio\Storyboard Studio.exe»
                # → target_path = «…\Storyboard Studio\» (папка onedir).
                target_path = Path(sys.executable).parent
            else:
                app_bundle = _sa.find_current_app_bundle()
                if not app_bundle:
                    self.error.emit(
                        "Не найден установленный Storyboard Studio.app.\n"
                        "Перенеси .app в /Applications или ~/Applications.")
                    return
                target_path = app_bundle

            # Постоянная temp-папка для распаковки + bootstrap скрипта.
            # ВАЖНО: НЕ TemporaryDirectory — она удалится когда Thread
            # умрёт, а bootstrap должен прочитать new_app_src ПОСЛЕ
            # выхода из Studio. Bootstrap сам удалит эту папку в конце.
            # 2026-05-12 (v1.0.53): _choose_update_dir() гарантирует
            # путь в пользовательской папке (%LOCALAPPDATA%\Temp\StoryboardStudio
            # на Win, /var/folders/.../StoryboardStudio на Mac) и
            # отказывается возвращать путь внутри C:\Windows\System32
            # или C:\Program Files (бывшая проблема — см. v1.0.39 след).
            update_dir = self._choose_update_dir()
            self._early_log(f"chosen update_dir={update_dir}")
            if update_dir.exists():
                shutil.rmtree(update_dir, ignore_errors=True)
            update_dir.mkdir(parents=True, exist_ok=True)
            if not update_dir.exists():
                msg = f"Не удалось создать рабочую папку обновления: {update_dir}"
                self._early_log(f"FAIL: {msg}")
                self.error.emit(msg)
                return
            self._early_log(f"update_dir created OK")

            with zipfile.ZipFile(buf) as z:
                z.extractall(update_dir)
            self._early_log(f"zip extracted to update_dir")

            # На Win ищем папку «Storyboard Studio» (onedir),
            # внутри которой .exe + _internal/.
            # На Mac ищем .app bundle (папку).
            if is_win:
                candidates = [p for p in update_dir.iterdir()
                              if p.is_dir()
                              and 'installer' not in p.name.lower()
                              and (p / 'Storyboard Studio.exe').exists()]
            else:
                candidates = list(update_dir.rglob('*.app'))
            if not candidates:
                self.error.emit(
                    "В архиве не найдено приложение Storyboard Studio.")
                return
            new_app_src = candidates[0]

            # ── Тихий sync actors/ (best-effort, без отдельного UI) ──
            # Качаем actors-snapshot-vX.Y.Z.zip из того же релиза и
            # зеркалим в self.root / "actors". Локальные _textures и
            # любые _* служебные папки НЕ ТРОГАЕМ. Любая ошибка тут —
            # log + continue, не валим основной .app update.
            try:
                self._sync_actors_snapshot()
            except Exception as _e:
                self._early_log(
                    f"[actors_sync] uncaught error (skipped): "
                    f"{type(_e).__name__}: {_e}")

            self.progress.emit("Готовлю установку…", 85)

            # 2026-05-09: ДВА маркера для безопасного апдейта.
            # - pending_version.txt = NEW: при success bat'а Studio при
            #   старте обновит version.json[app_version] на это значение.
            # - pending_rollback.txt = OLD: bat при success УДАЛЯЕТ этот
            #   файл. Если файл остался → bat упал на середине → Studio
            #   при старте откатывает version.json и показывает popup
            #   с прямой ссылкой на ручный Installer.
            old_app_version = ""
            try:
                vfile_now = self.root / "version.json"
                if vfile_now.exists():
                    old_app_version = json.loads(
                        vfile_now.read_text(encoding='utf-8')).get(
                            'app_version', '')
            except Exception:
                pass
            try:
                (self.root / "pending_version.txt").write_text(
                    self.target_version, encoding='utf-8')
                (self.root / "pending_rollback.txt").write_text(
                    old_app_version, encoding='utf-8')
            except Exception:
                pass  # некритично — popup при следующем старте появится без old_version

            # Bootstrap-скрипт + запуск detached
            self._early_log(f"creating bootstrap script in {update_dir}")
            script_path = self._make_bootstrap(
                new_app_src, target_path, update_dir, is_win,
                project_root=self.root)
            self._early_log(f"bootstrap created at {script_path}")
            self._launch_bootstrap(script_path, is_win)
            self._early_log("bootstrap launched detached, Studio will quit")

            self.progress.emit("Перезапуск…", 100)
            self.finished.emit(self.target_version, str(target_path))
        except Exception as e:
            # 2026-05-12 (v1.0.53): полный traceback в early-лог чтобы
            # юзер мог переслать причину failure (старый код терял
            # traceback в stderr который на Win .exe идёт в /dev/null).
            try:
                import traceback as _tb
                self._early_log(
                    f"FATAL EXCEPTION:\n{_tb.format_exc()}")
            except Exception:
                pass
            # В сообщение для popup кладём ссылку на лог если он есть.
            err_msg = str(e)
            if self._early_log_path is not None:
                err_msg += f"\n\nЛог обновления: {self._early_log_path}"
            self.error.emit(err_msg)

    # 2026-05-11 (v1.0.44): PowerShell helper для Win bootstrap'а.
    # Содержит два режима:
    #   -Mode Diagnose — авторитетно через Restart Manager API (rstrtmgr.dll)
    #     определяет процессы держащие файлы target onedir. Заменяет
    #     эвристический `tasklist | findstr` snapshot на точные PID/AppName/
    #     ServiceName/Type (Critical/Service/MainWindow/etc).
    #   -Mode Defer — escalation path после исчерпания retry-loop:
    #     1) Diagnose holders (для лога).
    #     2) Copy new bundle в staging dir `target.new` (NEW путь, нет AV race).
    #     3) MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT) через P/Invoke kernel32:
    #        - schedule delete всех файлов и папок target/
    #        - schedule rename staging → target
    #     4) Write `pending_reboot.txt` в project_root.
    #     5) Delete `pending_rollback.txt` (НЕ откатываем — install запланирован
    #        на следующий рестарт Windows).
    #     6) Exit 0. Studio запускается старая (current bundle), показывает
    #        non-blocking баннер «нужна перезагрузка». На рестарте Windows
    #        ДО загрузки user-сервисов применяет MoveFileEx из реестра
    #        HKLM\System\CurrentControlSet\Control\Session Manager\
    #        PendingFileRenameOperations — bundle подменяется без Defender'а
    #        (он ещё не запущен в эту фазу boot).
    #
    # КРИТИЧНО: НЕ пытаемся RmShutdown терминировать Defender. MsMpEng.exe —
    # Protected Process Light (PPL), даже SYSTEM с admin не убьёт его.
    # RM API используется ТОЛЬКО для диагностики (логгирование holders).
    #
    # Файл генерируется как UTF-8 с BOM (utf-8-sig) чтобы PowerShell корректно
    # парсил unicode (русские пути / app names в RM API могут содержать non-ASCII).
    _PS_HELPER_TEMPLATE = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Diagnose','Defer')]
    [string]$Mode,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$false)][string]$NewSrc,
    [Parameter(Mandatory=$false)][string]$ProjectRoot,
    [Parameter(Mandatory=$false)][string]$TargetVersion,
    [Parameter(Mandatory=$true)][string]$LogPath
)
$ErrorActionPreference = 'Continue'

function Log {
    param([string]$Msg)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try { Add-Content -Path $LogPath -Value "[$ts] [helper-$Mode] $Msg" -ErrorAction SilentlyContinue } catch {}
}

# C# stubs: Restart Manager API + MoveFileEx P/Invoke.
# -ErrorAction SilentlyContinue: если тип уже зарегистрирован (повторный вызов в
#   одной PS-сессии — теоретически не наш кейс, но defensive).
Add-Type @"
using System;
using System.Runtime.InteropServices;

public class RestartManager {
    [StructLayout(LayoutKind.Sequential)]
    public struct RM_UNIQUE_PROCESS {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }
    public const int CCH_RM_MAX_APP_NAME = 255;
    public const int CCH_RM_MAX_SVC_NAME = 63;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_APP_NAME + 1)]
        public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = CCH_RM_MAX_SVC_NAME + 1)]
        public string strServiceShortName;
        public uint ApplicationType;
        public uint AppStatus;
        public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
    }
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Auto)]
    public static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, string strSessionKey);
    [DllImport("rstrtmgr.dll")]
    public static extern int RmEndSession(uint pSessionHandle);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Auto)]
    public static extern int RmRegisterResources(uint pSessionHandle, uint nFiles, string[] rgsFilenames,
        uint nApplications, RM_UNIQUE_PROCESS[] rgApplications, uint nServices, string[] rgsServiceNames);
    [DllImport("rstrtmgr.dll")]
    public static extern int RmGetList(uint dwSessionHandle, out uint pnProcInfoNeeded, ref uint pnProcInfo,
        [In, Out] RM_PROCESS_INFO[] rgAffectedApps, ref uint lpdwRebootReasons);
}
public class Win32File {
    public const uint MOVEFILE_REPLACE_EXISTING = 0x1;
    public const uint MOVEFILE_DELAY_UNTIL_REBOOT = 0x4;
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool MoveFileEx(string lpExistingFileName, string lpNewFileName, uint dwFlags);
}
"@ -ErrorAction SilentlyContinue

function Get-FileHolders {
    param([string[]]$Files)
    [uint32]$sessionHandle = 0
    $sessionKey = [System.Guid]::NewGuid().ToString()
    $rc = [RestartManager]::RmStartSession([ref]$sessionHandle, 0, $sessionKey)
    if ($rc -ne 0) { Log "RmStartSession failed: rc=$rc"; return @() }
    try {
        $rc = [RestartManager]::RmRegisterResources($sessionHandle, $Files.Count, $Files, 0, $null, 0, $null)
        if ($rc -ne 0) { Log "RmRegisterResources failed: rc=$rc"; return @() }
        [uint32]$needed = 0; [uint32]$got = 64; [uint32]$reasons = 0
        $procs = New-Object 'RestartManager+RM_PROCESS_INFO[]' 64
        $rc = [RestartManager]::RmGetList($sessionHandle, [ref]$needed, [ref]$got, $procs, [ref]$reasons)
        if ($rc -ne 0 -and $rc -ne 234) { Log "RmGetList failed: rc=$rc"; return @() }
        $results = @()
        for ($i = 0; $i -lt $got; $i++) {
            $results += [PSCustomObject]@{
                PID         = $procs[$i].Process.dwProcessId
                AppName     = $procs[$i].strAppName
                ServiceName = $procs[$i].strServiceShortName
                Type        = $procs[$i].ApplicationType
                Restartable = $procs[$i].bRestartable
            }
        }
        return $results
    } finally {
        [void][RestartManager]::RmEndSession($sessionHandle)
    }
}

# === MAIN ===
Log "starting (Mode=$Mode, Target=$Target)"

# Gather sample files to register: .exe + up to 20 .dll/.pyd из _internal/
$exe = Join-Path $Target 'Storyboard Studio.exe'
$files = @()
if (Test-Path -LiteralPath $exe) { $files += $exe }
$internal = Join-Path $Target '_internal'
if (Test-Path -LiteralPath $internal) {
    $extra = Get-ChildItem -LiteralPath $internal -Recurse -File -Include '*.dll','*.pyd' -ErrorAction SilentlyContinue | Select-Object -First 20
    $files += ($extra | ForEach-Object { $_.FullName })
}
Log "registered $($files.Count) files for RM Diagnose"

$holders = @(Get-FileHolders -Files $files)
if ($holders.Count -eq 0) {
    Log "RM API: no holders detected (target appears free)"
} else {
    Log "RM API: $($holders.Count) holder(s) found:"
    foreach ($h in $holders) {
        $typeName = switch ([int]$h.Type) {
            1 { 'MainWindow' }
            2 { 'OtherWindow' }
            3 { 'Service' }
            4 { 'Explorer' }
            5 { 'Console' }
            1000 { 'Critical' }
            default { "Type$($h.Type)" }
        }
        Log "  PID=$($h.PID) Type=$typeName AppName='$($h.AppName)' Service='$($h.ServiceName)' Restartable=$($h.Restartable)"
    }
}

if ($Mode -eq 'Diagnose') { exit 0 }

# ============ Defer mode ============
Log "entering reboot-defer fallback"

$staging = "$Target.new"
try {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Log "copying new bundle to staging: $staging"
    Copy-Item -LiteralPath $NewSrc -Destination $staging -Recurse -Force
    Log "staging copy completed"
} catch {
    Log "FATAL: staging copy failed: $_"
    exit 1
}

$failed = 0; $scheduled = 0

# Schedule deletion of every file in target (DELAY_UNTIL_REBOOT, dst=$null)
Get-ChildItem -LiteralPath $Target -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    $ok = [Win32File]::MoveFileEx($_.FullName, $null, [Win32File]::MOVEFILE_DELAY_UNTIL_REBOOT)
    if ($ok) { $scheduled++ } else { $failed++; Log "MoveFileEx delete-file failed: $($_.FullName) err=$([System.Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
}
# Schedule deletion of every subdir (depth-first → leaves first)
Get-ChildItem -LiteralPath $Target -Recurse -Directory -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object {
    $ok = [Win32File]::MoveFileEx($_.FullName, $null, [Win32File]::MOVEFILE_DELAY_UNTIL_REBOOT)
    if ($ok) { $scheduled++ } else { $failed++; Log "MoveFileEx delete-dir failed: $($_.FullName)" }
}
# Top-level target dir itself
$ok = [Win32File]::MoveFileEx($Target, $null, [Win32File]::MOVEFILE_DELAY_UNTIL_REBOOT)
if ($ok) { $scheduled++ } else { $failed++; Log "MoveFileEx delete target-root failed" }

# Schedule rename staging → target (DELAY_UNTIL_REBOOT applies after deletes)
$flags = [Win32File]::MOVEFILE_REPLACE_EXISTING -bor [Win32File]::MOVEFILE_DELAY_UNTIL_REBOOT
$ok = [Win32File]::MoveFileEx($staging, $Target, $flags)
if ($ok) { $scheduled++ } else { $failed++; Log "MoveFileEx rename staging→target failed" }

Log "MoveFileEx: scheduled=$scheduled failed=$failed"

if ($failed -gt 0) {
    Log "FATAL: $failed MoveFileEx calls failed — rolling back staging, exit 1"
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

# Write pending_reboot.txt
$rebootMarker = Join-Path $ProjectRoot 'pending_reboot.txt'
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$content = "target_version=$TargetVersion`r`nscheduled_at=$now`r`n"
try {
    [System.IO.File]::WriteAllText($rebootMarker, $content)
    Log "wrote pending_reboot.txt (target=$TargetVersion scheduled_at=$now)"
} catch {
    Log "FATAL: failed to write pending_reboot.txt: $_"
    exit 1
}

# Delete pending_rollback.txt (НЕ откатываем версию — install запланирован)
$rollback = Join-Path $ProjectRoot 'pending_rollback.txt'
if (Test-Path -LiteralPath $rollback) {
    try { Remove-Item -LiteralPath $rollback -Force -ErrorAction SilentlyContinue; Log "removed pending_rollback.txt" } catch {}
}

Log "Defer mode: reboot-deferred install scheduled successfully (exit 0)"
exit 0
'''

    # ────────────────────────────────────────────────────────────────────────
    # 2026-05-12 (v1.0.55): WinForms splash window для UX'а во время update.
    # Раньше юзер видел пустой экран 60-90 сек после Studio.quit() пока bat
    # обновлял bundle. Главные последствия:
    #   1. Юзер думал «зависло» → запускал Studio через ярлык → старая .exe
    #      открывалась → залочивала bundle для bat → move_failed.
    #   2. Юзер думал «не сработало» → шёл искать Installer руками.
    # Splash решает обе проблемы:
    #   • TopMost окно — юзер видит что обновление идёт.
    #   • ControlBox=$false — окно нельзя закрыть до завершения (доп. защита
    #     от любопытных кликов).
    #   • Lock-file с PID этого PS-процесса — Studio при старте через ярлык
    #     видит lock + alive PID → показывает «идёт обновление» + closes.
    # PS-скрипт запускается через `start "" powershell ... -File splash.ps1`
    # — это создаёт ОТДЕЛЬНЫЙ процесс, detached от bat. При смерти bat
    # splash продолжает жить, читает status-файл, ждёт `done` или watchdog.
    # ────────────────────────────────────────────────────────────────────────
    _SPLASH_PS_TEMPLATE = r'''[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$StatusFile,
    [Parameter(Mandatory=$true)][string]$LockFile,
    [Parameter(Mandatory=$true)][string]$LogDir,
    [Parameter(Mandatory=$true)][string]$Version,
    [Parameter(Mandatory=$true)][string]$LabelPreparing,
    [Parameter(Mandatory=$true)][string]$LabelUpdating,
    [Parameter(Mandatory=$true)][string]$LabelLaunching,
    [Parameter(Mandatory=$true)][string]$LabelStalled,
    [Parameter(Mandatory=$true)][string]$LabelFailed,
    [Parameter(Mandatory=$true)][string]$LabelRebootNeeded,
    [Parameter(Mandatory=$true)][string]$LabelClose,
    [Parameter(Mandatory=$true)][string]$WindowTitle
)

# Lock-file: пишем свой PID — Studio при старте через ярлык увидит
# lock + проверит что наш PID жив → покажет popup и закроется.
try {
    [System.IO.File]::WriteAllText($LockFile, $PID.ToString())
} catch {
    # Не валим pipeline — lock-file опциональная защита.
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object Windows.Forms.Form
$form.Text = $WindowTitle
$form.Size = New-Object Drawing.Size(500, 170)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.ControlBox = $false  # юзер не может закрыть окно (нет [×])

$label = New-Object Windows.Forms.Label
$label.Location = New-Object Drawing.Point(20, 30)
$label.Size = New-Object Drawing.Size(460, 50)
$label.Text = $LabelPreparing
$label.Font = New-Object Drawing.Font('Segoe UI', 11)
$label.TextAlign = 'MiddleLeft'
$form.Controls.Add($label)

# Кнопка Закрыть — скрыта по умолчанию, появляется только при stall/failed.
$btnClose = New-Object Windows.Forms.Button
$btnClose.Text = $LabelClose
$btnClose.Location = New-Object Drawing.Point(370, 95)
$btnClose.Size = New-Object Drawing.Size(100, 30)
$btnClose.Visible = $false
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

$script:lastStatus = ''
$script:lastUpdate = [DateTime]::Now

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 500
$timer.Add_Tick({
    try {
        # 2026-05-12 (v1.0.55, fix): watchdog активен ТОЛЬКО пока status пуст
        # или 'preparing'. Долгий robocopy ('updating' status) законен — он
        # может работать 2-3 минуты на медленных машинах с Defender, и в это
        # время bat ничего не пишет в status. Watchdog ловит реальное
        # зависание: bat не стартанул вообще, или умер до первой status-записи.
        # После любого следующего status — выключаем watchdog, доверяем bat'у.
        if (($script:lastStatus -eq '' -or $script:lastStatus -eq 'preparing') -and
            [DateTime]::Now.Subtract($script:lastUpdate).TotalSeconds -gt 180) {
            $label.Text = ($LabelStalled -replace '\{log_dir\}', $LogDir)
            $btnClose.Visible = $true
            $timer.Stop()
            return
        }
        if (Test-Path -LiteralPath $StatusFile) {
            $status = (Get-Content -LiteralPath $StatusFile -Raw -ErrorAction SilentlyContinue)
            if ($status) { $status = $status.Trim() }
            if ($status -and $status -ne $script:lastStatus) {
                $script:lastStatus = $status
                $script:lastUpdate = [DateTime]::Now
                switch ($status) {
                    'preparing'     { $label.Text = $LabelPreparing }
                    'updating'      { $label.Text = ($LabelUpdating -replace '\{version\}', $Version) }
                    'launching'     { $label.Text = $LabelLaunching }
                    'done'          { $form.Close() }
                    'reboot_needed' {
                        $label.Text = $LabelRebootNeeded
                        $btnClose.Visible = $true
                        $timer.Stop()
                    }
                    'failed_final'  {
                        $label.Text = ($LabelFailed -replace '\{log_dir\}', $LogDir)
                        $btnClose.Visible = $true
                        $timer.Stop()
                    }
                }
            }
        }
    } catch {}
})
$timer.Start()

# При закрытии формы (через Close() или кнопку) — снимаем lock-file.
$form.Add_FormClosing({
    try {
        if (Test-Path -LiteralPath $LockFile) {
            Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
        }
        $timer.Stop()
        $timer.Dispose()
    } catch {}
})

[void][Windows.Forms.Application]::Run($form)
'''

    def _make_bootstrap(self, new_src: Path, target: Path,
                         update_dir: Path, is_win: bool,
                         project_root: Path) -> Path:
        """Пишет bootstrap-скрипт в update_dir и возвращает путь.

        2026-05-09 hardened после Failure mode A на Win (ren упал silently
        → Copy-Item -Force создал nested mess в target\\target\\, .exe
        остался старый, юзер думал что обновился).

        Скрипт:
          1. Ждёт пока процесс Studio (PID известен) умрёт.
          2. Move /y target → target.old (errorlevel показывает успех).
             На fail — записать update_failed.txt + старт СТАРОЙ Studio
             (она ещё на месте) + exit.
          3. Copy-Item new_src → target. На fail — rollback (mv .old
             → target обратно) + старт старой + exit.
          4. На success — delete pending_rollback.txt в project_root
             (Studio при старте видит → НЕ откатывает version.json).
          5. Start обновлённой Studio с retry-loop.
          6. Cleanup target.old. update_dir НЕ удаляем — bootstrap.log
             остаётся для диагностики (auto-cleanup на следующем старте
             Studio через finalize_pending_update).
        """
        studio_pid = os.getpid()
        log_path = update_dir / "bootstrap.log"
        rollback_marker = project_root / "pending_rollback.txt"
        failed_marker = update_dir / "update_failed.txt"
        if is_win:
            script = update_dir / "update.bat"
            target_parent = target.parent
            target_name = target.name           # «Storyboard Studio» (папка)
            studio_exe = target / "Storyboard Studio.exe"
            old_dir = target_parent / f"{target_name}.old"
            # 2026-05-11 (v1.0.44): рядом с update.bat пишем PS-helper для
            # RM API + MoveFileEx escalation. UTF-8 с BOM (utf-8-sig) чтобы
            # PowerShell корректно парсил unicode в путях / AppName'ах.
            ps_helper_path = update_dir / "update_helper.ps1"
            ps_helper_path.write_bytes(
                self._PS_HELPER_TEMPLATE.encode('utf-8-sig'))
            # ────────────────────────────────────────────────────────────────
            # 2026-05-11 КРИТИЧНО: НЕ ПРАВИТЬ `ping -n N+1 127.0.0.1` обратно
            # на `timeout /t N`. Это известный Windows gotcha.
            #
            # Bat запускается из Studio через subprocess.Popen со
            # stdin=subprocess.DEVNULL (см. _launch_bootstrap). Внутри такого
            # bat'а команда `timeout.exe` детектит redirected stdin и
            # МГНОВЕННО выходит с ошибкой «ERROR: Input redirection is not
            # supported, exiting the process immediately.» (даже с флагом
            # /nobreak — он подавляет только keypress, не stdin-check).
            # Stderr перенаправлен в NUL → визуально незаметно, но НИ ОДИН
            # `timeout` не спит реально 0 секунд.
            #
            # На v1.0.37/v1.0.38 это вылилось в баг «MOVE FAILED — target
            # locked»: задумывалось «6 retries × 2 sec = 12 сек окно» для
            # отпускания handle Defender'ом, по факту 6 попыток выполнялись
            # за 300мс, AV не успевал scan завершить → handle на .exe лочил
            # папку → апдейт фейлился.
            #
            # `ping -n N 127.0.0.1` отправляет N пакетов loopback с 1-сек
            # интервалом (первый мгновенно), даёт ≈ N-1 секунд реального
            # ожидания. НЕ читает stdin, надёжный sleep idiom с DOS-времён.
            # Формула: `ping -n {sec+1}` ≈ {sec} секунд ожидания.
            #
            # Также увеличен move retry: 6 → 30 (×2с = 60-секундное окно).
            # Defender release window обычно 5-15 сек, нужен запас на медленные
            # машины с агрессивным AV. На v1.0.41 наблюдался кейс где
            # Defender + Yandex Protect стэком держали handle > 30 сек —
            # 30 попыток × 2 сек дают 60 сек что покрывает стэк двух AV.
            #
            # 2026-05-11 + pre-flight warmup (опция E1): ДО первой попытки
            # move PowerShell проходит target onedir и open/close каждый
            # файл через [System.IO.File]::Open. Это форсит Defender'у
            # начать post-close-scan СЕЙЧАС пока bootstrap ещё ждёт ~10
            # сек, вместо того чтобы AV догонял уже во время move retry.
            # Эффективность переменная (если scan-cache валиден, open
            # быстрый), но «продуктивная задержка» = безусловный плюс.
            #
            # 2026-05-11 + диагностический логгинг AV (опция диагностики
            # без Sysinternals): на каждой 3-й failed-попытке move bat
            # пишет в bootstrap.log снимок активных AV-процессов через
            # `tasklist | findstr` — для будущей диагностики если 60 сек
            # тоже не хватит. Покрывает Defender, Yandex, MBAM, ESET,
            # Kaspersky, AVG, Avast.
            # ────────────────────────────────────────────────────────────────
            # 2026-05-12 (v1.0.55): кардинальная переработка bat-flow.
            # Удалены: move/rename target папки (хрупкое — нужно free-all-files
            # одновременно, AV блокировал), 30-retry loop, pre-flight warmup,
            # Copy-Item после move. Заменены на: robocopy /MIR (per-file replace
            # с retry per-file), splash window (TopMost WinForms через PS),
            # lock-file (защита от запуска Studio через ярлык во время апдейта),
            # status-файл (splash читает и обновляет label).
            #
            # Структура файлов:
            #   $LOG          — bootstrap.log в update_dir (как раньше)
            #   $STATUS       — update_status.txt в %LOCALAPPDATA%\StoryboardStudio
            #                   (splash polls это раз в 500мс)
            #   $LOCK         — updating.lock в %LOCALAPPDATA%\StoryboardStudio
            #                   (содержит PID splash-процесса; Studio при старте
            #                   видит → отказывается стартовать)
            #   splash.ps1    — WinForms окно в update_dir (рядом с update.bat)
            # shared_dir — публичная директория для UX-state файлов,
            # видимая Studio при следующем запуске (НЕ внутри Temp\).
            shared_dir = (Path(os.environ.get(
                'LOCALAPPDATA',
                str(Path.home() / 'AppData' / 'Local')))
                / 'StoryboardStudio')
            status_file = shared_dir / 'update_status.txt'
            lock_file = shared_dir / 'updating.lock'
            log_dir = shared_dir / 'logs'

            # Splash PowerShell-скрипт + локализованные labels из i18n.
            # 2026-05-12 (v1.0.55): UTF-8 без BOM по требованию юзера.
            # ВНИМАНИЕ: PowerShell 5.1 без BOM может интерпретировать .ps1
            # как Windows-1251 → кириллица в labels сломается. Если на
            # каком-то юзере проявится garbled splash text — добавь
            # 'chcp 65001 >> "%LOG%" 2>&1\r\n' в bat-content ДО splash
            # invocation, либо верни `encode('utf-8-sig')` (BOM решает
            # проблему PS 5.1 кодировки автоматически).
            splash_ps = update_dir / 'splash.ps1'
            splash_ps.write_bytes(
                self._SPLASH_PS_TEMPLATE.encode('utf-8'))
            try:
                from i18n import tr as _tr
                label_preparing = _tr('splash_preparing')
                label_updating = _tr('splash_updating')
                label_launching = _tr('splash_launching')
                label_stalled = _tr('splash_stalled')
                label_failed = _tr('splash_failed')
                label_reboot_needed = _tr('splash_reboot_needed')
                label_close = _tr('splash_close_btn')
                window_title = _tr('update_quit_msg_title')
            except Exception:
                # Fallback на RU если i18n недоступен.
                label_preparing = 'Подготовка...'
                label_updating = 'Обновление до v{version}...'
                label_launching = 'Запуск новой версии...'
                label_stalled = 'Обновление зависло, см. логи: {log_dir}'
                label_failed = 'Обновление не удалось. Логи: {log_dir}'
                label_reboot_needed = 'Установка отложена до перезагрузки Windows.'
                label_close = 'Закрыть'
                window_title = 'Обновление Storyboard Studio'

            content = (
                "@echo off\r\n"
                "rem Storyboard Studio update bootstrap v1.0.55 — robocopy + splash + lock.\r\n"
                "rem НЕ заменять `ping -n N+1 127.0.0.1` на `timeout` — см. комментарий в _make_bootstrap.\r\n"
                f'set "LOG={log_path}"\r\n'
                f'set "STATUS={status_file}"\r\n'
                f'set "LOCK={lock_file}"\r\n'
                f'set "SHARED_DIR={shared_dir}"\r\n'
                'echo [%date% %time%] bootstrap start (v1.0.55 robocopy flow) >> "%LOG%" 2>&1\r\n'
                # Создаём shared dir если её нет.
                'if not exist "%SHARED_DIR%" mkdir "%SHARED_DIR%" >> "%LOG%" 2>&1\r\n'
                # Cleanup стейл status/lock от прошлой неудачной попытки.
                'if exist "%STATUS%" del /F /Q "%STATUS%" >> "%LOG%" 2>&1\r\n'
                'if exist "%LOCK%" del /F /Q "%LOCK%" >> "%LOG%" 2>&1\r\n'
                # 1. Wait for Studio process to die. Polling loop 1×/sec.
                ":wait_for_studio\r\n"
                "ping -n 2 127.0.0.1 > nul 2>&1\r\n"
                f'tasklist /FI "PID eq {studio_pid}" 2>nul | find /I "{studio_pid}" >nul\r\n'
                "if not errorlevel 1 goto wait_for_studio\r\n"
                'echo [%date% %time%] studio died >> "%LOG%" 2>&1\r\n'
                'taskkill /F /IM "Storyboard Studio.exe" /T >> "%LOG%" 2>&1\r\n'
                # 2. СРАЗУ запускаем splash detached — юзер видит окно
                # «Подготовка...» пока bat ждёт AV release. start "" создаёт
                # отдельный процесс не-child bat'а — переживёт смерть bat.
                # -WindowStyle Hidden скрывает console окно powershell.exe;
                # WinForms Form показывается отдельно через Application::Run.
                'echo preparing > "%STATUS%"\r\n'
                'echo [%date% %time%] launching splash detached >> "%LOG%" 2>&1\r\n'
                'start "" powershell -NoProfile -ExecutionPolicy Bypass '
                '-WindowStyle Hidden '
                f'-File "{splash_ps}" '
                '-StatusFile "%STATUS%" '
                '-LockFile "%LOCK%" '
                f'-LogDir "{log_dir}" '
                f'-Version "{self.target_version}" '
                f'-LabelPreparing "{label_preparing}" '
                f'-LabelUpdating "{label_updating}" '
                f'-LabelLaunching "{label_launching}" '
                f'-LabelStalled "{label_stalled}" '
                f'-LabelFailed "{label_failed}" '
                f'-LabelRebootNeeded "{label_reboot_needed}" '
                f'-LabelClose "{label_close}" '
                f'-WindowTitle "{window_title}"\r\n'
                # 3. AV release window. 5 сек на старт scan'а после taskkill.
                # Splash уже показан — юзер видит «Подготовка...» эти 5 сек.
                'echo [%date% %time%] waiting 5s for AV/Defender to release handles >> "%LOG%" 2>&1\r\n'
                "ping -n 6 127.0.0.1 > nul 2>&1\r\n"
                # 4. ROBOCOPY /MIR — per-file replace in-place.
                # /MIR mirrors source to target (extra files in target deleted).
                # /W:2 /R:30 — 2 sec wait, 30 retries per locked file
                #   (per-file, не whole-bundle, как было с move).
                # /NP — no progress percent (loud в логе иначе).
                # /NJH /NJS — no job header/summary (compact log).
                # /NDL — no directory list.
                # /LOG+ append to bootstrap.log.
                # Exit codes: 0-7 = success (in different shades),
                #             8+ = real failure → escalation to defer.
                'echo updating > "%STATUS%"\r\n'
                'echo [%date% %time%] starting robocopy /MIR new -> target >> "%LOG%" 2>&1\r\n'
                f'robocopy "{new_src}" "{target}" '
                '/MIR /W:2 /R:30 /NP /NJH /NJS /NDL '
                '/LOG+:"%LOG%"\r\n'
                # if errorlevel 8 = "is >= 8" — это failure.
                "if errorlevel 8 goto robocopy_failed\r\n"
                'echo [%date% %time%] robocopy succeeded (exit code 0-7) >> "%LOG%" 2>&1\r\n'
                "goto robocopy_ok\r\n"
                # ─── robocopy_failed: escalation на RM API + MoveFileEx defer ───
                ":robocopy_failed\r\n"
                'echo [%date% %time%] robocopy failed (exit >= 8), escalating to defer fallback >> "%LOG%" 2>&1\r\n'
                # RM API + MoveFileEx defer (как было в v1.0.44):
                #   1) RM Diagnose — список holder'ов в лог.
                #   2) Copy new bundle в staging dir `target.new`.
                #   3) MoveFileEx schedule delete target/* + rename staging→target
                #      ПРИ СЛЕДУЮЩЕМ ребуте Windows.
                #   4) Write pending_reboot.txt в project_root.
                #   5) Delete pending_rollback.txt (НЕ откатываем).
                "powershell -NoProfile -ExecutionPolicy Bypass "
                f'-File "{ps_helper_path}" -Mode Defer '
                f'-Target "{target}" -NewSrc "{new_src}" '
                f'-ProjectRoot "{project_root}" -TargetVersion "{self.target_version}" '
                f'-LogPath "{log_path}" >> "%LOG%" 2>&1\r\n'
                "if errorlevel 1 (\r\n"
                '  echo [%date% %time%] defer fallback ALSO failed >> "%LOG%" 2>&1\r\n'
                '  echo failed_final > "%STATUS%"\r\n'
                f'  echo move_failed > "{failed_marker}"\r\n'
                f'  start "" "{studio_exe}"\r\n'
                "  exit /b 1\r\n"
                ")\r\n"
                'echo [%date% %time%] reboot-deferred install scheduled successfully >> "%LOG%" 2>&1\r\n'
                'echo reboot_needed > "%STATUS%"\r\n'
                f'start "" "{studio_exe}"\r\n'
                "exit /b 0\r\n"
                # ─── robocopy_ok: успех, запускаем новую Studio ───
                ":robocopy_ok\r\n"
                # Delete rollback marker — Studio при старте видит, что
                # rollback не нужен, и применяет pending_version → version.json.
                'echo [%date% %time%] success -- deleting rollback marker >> "%LOG%" 2>&1\r\n'
                f'if exist "{rollback_marker}" del /f /q "{rollback_marker}" >> "%LOG%" 2>&1\r\n'
                'echo launching > "%STATUS%"\r\n'
                'echo [%date% %time%] signaling splash done >> "%LOG%" 2>&1\r\n'
                # 2026-05-12 (v1.0.55, RACE FIX): двухфазное закрытие
                # splash → unlock → start Studio. Раньше bat сам удалял
                # lock-file перед start'ом — это давало race-окно: lock
                # удалён, Studio ещё не запустилась, юзер тыкает ярлык →
                # запускается вторая копия без защиты. Теперь:
                #   1. echo done > STATUS — splash начинает закрываться.
                #   2. Splash в FormClosing удаляет lock-file (атомарно
                #      внутри splash-процесса).
                #   3. Bat poll'ит `if not exist LOCK` каждые 200мс,
                #      timeout 5 сек.
                #   4. Только когда lock реально исчез — start Studio.
                # Lock остаётся пока окно splash visible → если юзер
                # за это время тыкнет ярлык, Studio при __init__
                # увидит alive lock → откажется стартовать.
                'echo done > "%STATUS%"\r\n'
                "set /a wait_tries=0\r\n"
                ":wait_lock_release\r\n"
                'if not exist "%LOCK%" goto lock_released\r\n'
                "set /a wait_tries+=1\r\n"
                "if %wait_tries% GEQ 25 goto lock_timeout\r\n"
                "ping -n 1 127.0.0.1 > nul 2>&1\r\n"   # ≈ 200ms
                "goto wait_lock_release\r\n"
                ":lock_timeout\r\n"
                'echo [%date% %time%] WARN: splash did not release lock in 5s, forcing unlock >> "%LOG%" 2>&1\r\n'
                'if exist "%LOCK%" del /F /Q "%LOCK%" >> "%LOG%" 2>&1\r\n'
                ":lock_released\r\n"
                'echo [%date% %time%] lock released, starting new Studio >> "%LOG%" 2>&1\r\n'
                # 5. Start updated Studio with retry-loop.
                "set /a tries=0\r\n"
                ":try_start\r\n"
                "set /a tries+=1\r\n"
                'echo [%date% %time%] start attempt %tries% >> "%LOG%" 2>&1\r\n'
                f'start "" "{studio_exe}"\r\n'
                'echo [%date% %time%]   waiting 5s for Studio to appear in tasklist >> "%LOG%" 2>&1\r\n'
                "ping -n 6 127.0.0.1 > nul 2>&1\r\n"
                'tasklist /FI "IMAGENAME eq Storyboard Studio.exe" 2>nul '
                '| find /I "Storyboard Studio.exe" >nul\r\n'
                "if errorlevel 1 (\r\n"
                "  if %tries% LSS 3 goto try_start\r\n"
                ")\r\n"
                # update_dir НЕ удаляем намеренно — bootstrap.log нужен
                # для диагностики. Cleanup старых storyboard_update_*
                # папок делает Studio при старте через
                # finalize_pending_update (см. storyboard_app.py:1249+).
                'echo [%date% %time%] bootstrap complete >> "%LOG%" 2>&1\r\n'
            )
            script.write_bytes(content.encode('utf-8'))
        else:
            script = update_dir / "update.sh"
            old_dir = Path(f"{target}.old")
            content = (
                "#!/bin/bash\n"
                "# Storyboard Studio update bootstrap (Mac, hardened 2026-05-09)\n"
                f'LOG="{log_path}"\n'
                'echo "[$(date)] bootstrap start" >> "$LOG"\n'
                f"while kill -0 {studio_pid} 2>/dev/null; do sleep 1; done\n"
                'echo "[$(date)] studio died" >> "$LOG"\n'
                "sleep 1\n"
                # Cleanup leftover .old.
                f'rm -rf "{old_dir}" 2>/dev/null\n'
                # Move target → .old. На Mac mv почти всегда работает,
                # но проверим на всякий случай.
                'echo "[$(date)] moving target to .old" >> "$LOG"\n'
                f'if [ -d "{target}" ]; then\n'
                f'  if ! mv "{target}" "{old_dir}" 2>>"$LOG"; then\n'
                '    echo "[$(date)] MOVE FAILED, aborting" >> "$LOG"\n'
                f'    echo move_failed > "{failed_marker}"\n'
                f'    open "{target}" 2>/dev/null\n'
                "    exit 1\n"
                "  fi\n"
                "fi\n"
                # Copy new.
                'echo "[$(date)] copying new bundle" >> "$LOG"\n'
                f'if ! cp -R "{new_src}" "{target}" 2>>"$LOG"; then\n'
                '  echo "[$(date)] COPY FAILED, rolling back" >> "$LOG"\n'
                f'  rm -rf "{target}" 2>/dev/null\n'
                f'  mv "{old_dir}" "{target}" 2>>"$LOG"\n'
                f'  echo copy_failed > "{failed_marker}"\n'
                f'  open "{target}" 2>/dev/null\n'
                "  exit 1\n"
                "fi\n"
                # SUCCESS — delete rollback marker.
                'echo "[$(date)] success -- deleting rollback marker" >> "$LOG"\n'
                f'rm -f "{rollback_marker}" 2>>"$LOG"\n'
                f'open "{target}"\n'
                "sleep 2\n"
                f'rm -rf "{old_dir}" 2>>"$LOG"\n'
                'echo "[$(date)] bootstrap complete" >> "$LOG"\n'
            )
            script.write_text(content, encoding='utf-8')
            os.chmod(script, 0o755)
        return script

    def _launch_bootstrap(self, script: Path, is_win: bool):
        """Запускает bootstrap-скрипт detached — он переживёт смерть Studio.

        Win (исправлено 2026-05-08): окно cmd скрываем через STARTUPINFO
        с SW_HIDE — это работает надёжно. CREATE_NO_WINDOW + cmd.exe в
        связке с DETACHED_PROCESS даёт undefined behavior: Windows
        игнорирует флаг скрытия и показывает окно. STARTUPINFO работает
        независимо от других флагов.

        CREATE_NEW_PROCESS_GROUP оставляем — даёт child собственную
        process group, чтобы Ctrl+C в parent (Studio) не убил bootstrap.
        DETACHED_PROCESS убран как лишний и конфликтующий.

        Mac: start_new_session=True (POSIX setsid) — bash без терминала.
        """
        if is_win:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            NEW_GROUP = 0x00000200
            NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd", "/c", str(script)],
                creationflags=NEW_GROUP | NO_WINDOW,
                startupinfo=si,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["/bin/bash", str(script)],
                start_new_session=True,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )


class SendUpdateThread(QThread):
    """Админ-режим: бампит версию + git commit + git push.
    Опционально — загружает Storyboard Studio.app в GitHub Releases.
    """
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, str, bool)   # (project_version, app_version, app_uploaded)
    error    = pyqtSignal(str)

    def __init__(self, root: Path, upload_app: bool = False):
        super().__init__()
        self.root       = root
        self.upload_app = upload_app

    def run(self):
        try:
            # 1. Если upload_app — сначала пересобираем .app. Если сборка
            #    упала, выходим с error ДО bump'а версии и git push —
            #    чтобы не плодить дыры в истории Releases (v1.0.32 в git
            #    без соответствующего Release-asset).
            #
            # Очистка build/ обязательна: PyInstaller держит .pyc от
            # прошлой сборки → правки могут не попасть в bundle.
            #
            # Mac-only: build.sh — bash; админ работает на Mac, GitHub
            # Actions собирает Win .exe отдельно из push'а.
            if self.upload_app:
                self.progress.emit("Очистка build/…")
                build_dir = self.root / "build"
                if build_dir.exists():
                    shutil.rmtree(build_dir, ignore_errors=True)

                build_script = self.root / "build.sh"
                if not build_script.exists():
                    self.error.emit(
                        f"Не найден {build_script}. Авто-пересборка невозможна.")
                    return

                self.progress.emit(
                    "Пересборка .app (≈2-3 мин, smoke + PyInstaller + launch-тест)…")

                # 2026-05-09: Qt env fix. SendUpdateThread работает внутри
                # bundled Storyboard Studio.app — PyInstaller bootloader
                # выставляет DYLD_*/QT_*/PYTHONHOME/PYTHONPATH/_PYI_*.
                # Дочерний bash → python3 в build.sh унаследует их и при
                # `import PyQt6` загрузит Qt из bundle поверх системного
                # PyQt6 → дважды зарегистрированный QMetalLayer → SIGABRT
                # в smoke.py. Чистим env. В dev (`sys.frozen=False`) env
                # уже чистый — гейтим чтобы не сломать dev-режим. На Win
                # этих vars нет — `.pop(k, None)` тихо пропустит.
                clean_env = os.environ.copy()
                if getattr(sys, 'frozen', False):
                    for k in list(clean_env.keys()):
                        if (k.startswith(("DYLD_", "QT_", "_PYI_"))
                                or k in ("PYTHONHOME", "PYTHONPATH")):
                            clean_env.pop(k, None)

                try:
                    rb = subprocess.run(
                        ["bash", str(build_script)],
                        cwd=str(self.root),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=600,
                        env=clean_env,
                        **_sa.no_console_kwargs(),
                    )
                except subprocess.TimeoutExpired:
                    self.error.emit(
                        "Сборка .app превысила 10 минут. Запусти ./build.sh "
                        "вручную чтобы посмотреть лог.")
                    return
                if rb.returncode != 0:
                    tail = ((rb.stderr or "") + (rb.stdout or ""))[-1500:]
                    self.error.emit(f"Сборка .app упала:\n{tail}")
                    return

                app_path = self.root / "dist" / "Storyboard Studio.app"
                if not app_path.exists():
                    self.error.emit(
                        f"Сборка прошла, но {app_path} не найден.")
                    return

            vfile = self.root / "version.json"
            data = json.loads(vfile.read_text(encoding="utf-8")) if vfile.exists() \
                   else {"version": "1.0.0", "app_version": "1.0.0"}

            # 2026-05-08 (Шаг B): убрана концепция «версии проекта». Раньше
            # было два поля: `version` (project) и `app_version` (Studio).
            # Теперь у Studio одна версия — она бампается в `app_version`.
            # Поле `version` синхронизируем с app_version чтобы у коллег с
            # legacy-version.json синий баннер «Обновление проекта» не
            # вылезал (он сравнивает GitHub.version vs local.version).
            cur_app_v = data.get("app_version", data.get("version", "1.0.0"))
            amaj, amin, apat = cur_app_v.split(".")
            new_app_version = f"{amaj}.{amin}.{int(apat) + 1}"
            data["app_version"] = new_app_version
            # Синхронизируем legacy-поле для backward-compat. Если Studio
            # на старом коде сравнит GitHub.version vs local.version —
            # они теперь оба = новой версии.
            data["version"] = new_app_version
            new_version = new_app_version

            data["released"] = datetime.date.today().isoformat()
            vfile.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            self.progress.emit("Готовлю коммит…")
            subprocess.run(["git", "-C", str(self.root), "add", "-A"],
                           check=True, capture_output=True, timeout=30,
                           **_sa.no_console_kwargs())
            r = subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", f"Update {new_version}"],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                **_sa.no_console_kwargs(),
            )
            if r.returncode != 0 and "nothing to commit" not in r.stdout:
                self.error.emit(f"Git commit error: {r.stderr or r.stdout}")
                return

            self.progress.emit("Отправляю на GitHub…")
            r = subprocess.run(
                ["git", "-C", str(self.root), "push", "origin", _sa.GITHUB_BRANCH],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",  # 2026-05-09 Win-fix.
                **_sa.no_console_kwargs(),
            )
            if r.returncode != 0:
                self.error.emit(f"Git push error: {r.stderr}")
                return

            uploaded = False
            if self.upload_app:
                # .app уже собран и проверен в шаге 1 (build-then-bump).
                app_path = self.root / "dist" / "Storyboard Studio.app"

                token = _sa.get_github_token_from_remote(self.root)
                if not token:
                    self.error.emit(
                        "Не нашёл GitHub token в URL origin.\n"
                        "Чтобы загрузить .app в Releases — настрой git remote с токеном:\n"
                        "git remote set-url origin https://TOKEN@github.com/USER/REPO.git")
                    return

                self.progress.emit("Архивирую Storyboard Studio.app…")
                # Имя ZIP в человекочитаемом формате — точно так же как
                # пишется версия в шапке приложения и в системе обновлений.
                # GitHub корректно URL-кодирует пробелы в asset_url.
                zip_name = f"Storyboard Studio v{new_app_version}-mac.zip"
                zip_path = self.root / "dist" / zip_name
                if zip_path.exists():
                    zip_path.unlink()
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    for f in app_path.rglob("*"):
                        if f.is_file() or f.is_symlink():
                            zf.write(f, f.relative_to(app_path.parent))

                self.progress.emit("Создаю GitHub Release…")
                tag = f"app-v{new_app_version}"
                rel = _sa.create_github_release(
                    token, tag,
                    name=f"Storyboard Studio v{new_app_version}",
                    body=f"Storyboard Studio v{new_app_version}",
                )
                if not rel:
                    self.error.emit(
                        "Не удалось создать GitHub Release. Проверь токен и права (нужен scope 'repo').")
                    return

                size_mb = zip_path.stat().st_size // (1024 * 1024)
                self.progress.emit(f"Загружаю .app ({size_mb} МБ) в Release…")
                if not _sa.upload_release_asset(token, rel["upload_url"], zip_path):
                    self.error.emit("Не удалось загрузить .app в GitHub Release.")
                    return

                # Удаляем zip после успешной загрузки — он больше не нужен,
                # коллеги скачивают его прямо с GitHub Releases.
                try:
                    zip_path.unlink()
                except Exception:
                    pass

                uploaded = True

                # ── actors-snapshot.zip — отдельный release-asset ──
                # Платформо-независимый снапшот папки actors/ для тихой
                # синхронизации на стороне коллеги (см.
                # DownloadAppUpdateThread._sync_actors_snapshot). Содержит:
                #   - actors/actors.json
                #   - actors/<slug>/*.jpg (фото актёров)
                # ИСКЛЮЧАЕТСЯ:
                #   - actors/_textures/ — локальные текстуры коллег
                #   - actors/.DS_Store / Thumbs.db — мусор ОС
                #   - actors/_*/ — любые служебные папки начинающиеся с _
                # Если упаковка/upload упали — Send Update НЕ валится
                # (актёры синкаются best-effort, основной .app уже выехал).
                try:
                    self.progress.emit("Архивирую actors-snapshot…")
                    actors_zip_name = (
                        f"actors-snapshot-v{new_app_version}.zip")
                    actors_zip_path = self.root / "dist" / actors_zip_name
                    if actors_zip_path.exists():
                        actors_zip_path.unlink()
                    actors_root = self.root / "actors"
                    packed_files = 0
                    if actors_root.is_dir():
                        with zipfile.ZipFile(
                                actors_zip_path, 'w',
                                zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                            # actors.json — корневой файл папки.
                            json_p = actors_root / "actors.json"
                            if json_p.is_file():
                                zf.write(
                                    json_p,
                                    arcname=f"actors/actors.json")
                                packed_files += 1
                            # Slug-папки актёров (исключая _*).
                            for slug_dir in actors_root.iterdir():
                                if not slug_dir.is_dir():
                                    continue
                                if slug_dir.name.startswith('_'):
                                    continue
                                if slug_dir.name in ('.DS_Store',):
                                    continue
                                for f in slug_dir.rglob('*'):
                                    if not f.is_file():
                                        continue
                                    if f.name in ('.DS_Store', 'Thumbs.db'):
                                        continue
                                    # ВАЖНО: НЕ называть переменную `rel` —
                                    # она затрёт `rel` из outer scope (dict от
                                    # _sa.create_github_release c upload_url),
                                    # и `rel["upload_url"]` ниже бросит
                                    # TypeError: PosixPath is not subscriptable.
                                    # Bug пойман в v1.0.68/1.0.69 (см. runtime.log).
                                    arc_rel = f.relative_to(actors_root)
                                    zf.write(f, arcname=f"actors/{arc_rel}")
                                    packed_files += 1
                    if packed_files > 0 and actors_zip_path.exists():
                        a_size_mb = max(
                            1, actors_zip_path.stat().st_size // (1024 * 1024))
                        self.progress.emit(
                            f"Загружаю actors-snapshot ({a_size_mb} МБ)…")
                        a_ok = _sa.upload_release_asset(
                            token, rel["upload_url"], actors_zip_path)
                        try:
                            actors_zip_path.unlink()
                        except Exception:
                            pass
                        if not a_ok:
                            sys.stderr.write(
                                "[send_update] actors-snapshot upload failed "
                                "(non-fatal, .app уже выехал)\n")
                except Exception as _e:
                    # Не валим Send Update если actors-snapshot не собрался.
                    sys.stderr.write(
                        f"[send_update] actors-snapshot pack/upload error: "
                        f"{type(_e).__name__}: {_e}\n")
                    try:
                        if actors_zip_path.exists():
                            actors_zip_path.unlink()
                    except Exception:
                        pass

            self.finished.emit(new_version, new_app_version, uploaded)
        except subprocess.CalledProcessError as e:
            self.error.emit(f"Ошибка git: {e.stderr.decode() if e.stderr else str(e)}")
        except Exception as e:
            self.error.emit(str(e))


class FetchStatsThread(QThread):
    """Загружает статистику скачиваний из GitHub Releases (только для admin)."""
    finished = pyqtSignal(list)   # list of {tag, version, downloads}

    def run(self):
        self.finished.emit(_sa.fetch_all_release_stats())
