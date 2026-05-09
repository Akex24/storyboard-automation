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

    def __init__(self, target_version: str, root: Path):
        super().__init__()
        self.target_version = target_version
        self.root = root

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
        try:
            self.progress.emit("Ищу релиз на GitHub…", 5)
            asset = _sa.fetch_release_asset_info(self.target_version)
            if not asset:
                self.error.emit(
                    f"Не найден .zip в релизе app-v{self.target_version}.\n"
                    "Попробуй обновить вручную — скачай с GitHub Releases.")
                return

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
            update_dir = (Path(tempfile.gettempdir())
                          / f"storyboard_update_{self.target_version}_{os.getpid()}")
            if update_dir.exists():
                shutil.rmtree(update_dir, ignore_errors=True)
            update_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(buf) as z:
                z.extractall(update_dir)

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

            self.progress.emit("Готовлю установку…", 85)

            # Маркер новой версии для Studio (читается при следующем
            # старте → обновляет version.json[app_version] → удаляется).
            try:
                marker = self.root / "pending_version.txt"
                marker.write_text(self.target_version, encoding='utf-8')
            except Exception:
                pass  # некритично — баннер просто появится один лишний раз

            # Bootstrap-скрипт + запуск detached
            script_path = self._make_bootstrap(
                new_app_src, target_path, update_dir, is_win)
            self._launch_bootstrap(script_path, is_win)

            self.progress.emit("Перезапуск…", 100)
            self.finished.emit(self.target_version, str(target_path))
        except Exception as e:
            self.error.emit(str(e))

    def _make_bootstrap(self, new_src: Path, target: Path,
                         update_dir: Path, is_win: bool) -> Path:
        """Пишет bootstrap-скрипт в update_dir и возвращает путь.

        Скрипт:
          1. Ждёт пока процесс Studio (PID известен) умрёт. Иначе на Win
             move .exe фейлится из-за file lock.
          2. Подменяет target на new_src.
          3. Запускает обновлённый Studio.
          4. Удаляет update_dir (сам себя в том числе).
        """
        studio_pid = os.getpid()
        if is_win:
            script = update_dir / "update.bat"
            # 2026-05-08: onedir mode. target — это ПАПКА «Storyboard Studio»
            # содержащая .exe + _internal/. new_src — также папка из
            # распакованного zip. Подменяем папку целиком.
            target_parent = target.parent
            target_name = target.name           # «Storyboard Studio» (папка)
            studio_exe = target / "Storyboard Studio.exe"
            old_dir = target_parent / f"{target_name}.old"
            # Логика:
            # 1. Wait Studio.exe died (PID).
            # 2. Rename target папка → target.old (чтобы не пытаться
            #    удалить пока Defender может что-то держать).
            # 3. PS Copy-Item -Recurse new_src → target (новая папка).
            # 4. Start обновлённый .exe с retry-loop.
            # 5. Через 5 сек удалить target.old + update_dir.
            # PS используется для надёжного recursive copy с flush.
            content = (
                "@echo off\r\n"
                "rem Storyboard Studio update bootstrap (onedir)\r\n"
                ":wait_for_studio\r\n"
                "timeout /t 1 /nobreak > nul 2>&1\r\n"
                f'tasklist /FI "PID eq {studio_pid}" 2>nul | find /I "{studio_pid}" >nul\r\n'
                "if not errorlevel 1 goto wait_for_studio\r\n"
                "timeout /t 1 /nobreak > nul 2>&1\r\n"
                # Удаляем .old папку если осталась от предыдущего апдейта.
                f'if exist "{old_dir}" rmdir /s /q "{old_dir}"\r\n'
                # Переименовываем текущую папку Studio → .old.
                f'if exist "{target}" ren "{target}" "{target_name}.old"\r\n'
                "timeout /t 1 /nobreak > nul 2>&1\r\n"
                # Копируем новую onedir папку из update_dir в target.
                f'powershell -NoProfile -ExecutionPolicy Bypass -Command '
                f'"Copy-Item -LiteralPath \'{new_src}\' '
                f'-Destination \'{target}\' -Recurse -Force"\r\n'
                "timeout /t 3 /nobreak > nul 2>&1\r\n"
                # Запускаем обновлённую Studio с retry.
                "set /a tries=0\r\n"
                ":try_start\r\n"
                "set /a tries+=1\r\n"
                f'start "" "{studio_exe}"\r\n'
                "timeout /t 5 /nobreak > nul 2>&1\r\n"
                'tasklist /FI "IMAGENAME eq Storyboard Studio.exe" 2>nul '
                '| find /I "Storyboard Studio.exe" >nul\r\n'
                "if errorlevel 1 (\r\n"
                "  if %tries% LSS 3 goto try_start\r\n"
                ")\r\n"
                "timeout /t 5 /nobreak > nul 2>&1\r\n"
                # Cleanup: удалить старую .old папку и саму update_dir.
                f'if exist "{old_dir}" rmdir /s /q "{old_dir}"\r\n'
                f'rmdir /s /q "{update_dir}"\r\n'
            )
            script.write_bytes(content.encode('utf-8'))
        else:
            script = update_dir / "update.sh"
            # На Mac .app — это папка, удаляем целиком и копируем новую.
            # `kill -0 PID` возвращает 0 если процесс жив, иначе ошибка.
            # `open` запускает .app как стандартный Mac launcher.
            content = (
                "#!/bin/bash\n"
                "# Storyboard Studio update bootstrap\n"
                f"while kill -0 {studio_pid} 2>/dev/null; do sleep 1; done\n"
                "sleep 1\n"
                f'rm -rf "{target}"\n'
                f'cp -R "{new_src}" "{target}"\n'
                f'open "{target}"\n'
                "sleep 2\n"
                f'rm -rf "{update_dir}"\n'
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
