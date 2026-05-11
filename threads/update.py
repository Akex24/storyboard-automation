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
            script_path = self._make_bootstrap(
                new_app_src, target_path, update_dir, is_win,
                project_root=self.root)
            self._launch_bootstrap(script_path, is_win)

            self.progress.emit("Перезапуск…", 100)
            self.finished.emit(self.target_version, str(target_path))
        except Exception as e:
            self.error.emit(str(e))

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
            content = (
                "@echo off\r\n"
                "rem Storyboard Studio update bootstrap (onedir, hardened 2026-05-11)\r\n"
                "rem НЕ заменять `ping -n N+1 127.0.0.1` на `timeout` — см. комментарий в _make_bootstrap.\r\n"
                f'set "LOG={log_path}"\r\n'
                'echo [%date% %time%] bootstrap start >> "%LOG%" 2>&1\r\n'
                # 1. Wait for Studio process to die. Polling loop 1×/sec.
                ":wait_for_studio\r\n"
                "ping -n 2 127.0.0.1 > nul 2>&1\r\n"   # ≈ 1 sec (timeout broken under DEVNULL stdin)
                f'tasklist /FI "PID eq {studio_pid}" 2>nul | find /I "{studio_pid}" >nul\r\n'
                "if not errorlevel 1 goto wait_for_studio\r\n"
                'echo [%date% %time%] studio died >> "%LOG%" 2>&1\r\n'
                # Force-kill любые оставшиеся инстансы (зомби, дочерние процессы) —
                # их handle на .exe иначе блокирует move. /T = вместе с children.
                'taskkill /F /IM "Storyboard Studio.exe" /T >> "%LOG%" 2>&1\r\n'
                'echo [%date% %time%] waiting 5s for AV/Defender to release handles >> "%LOG%" 2>&1\r\n'
                "ping -n 6 127.0.0.1 > nul 2>&1\r\n"   # ≈ 5 sec (was timeout /t 2 — broken)
                # Cleanup leftover .old от прошлого апдейта.
                f'if exist "{old_dir}" rmdir /s /q "{old_dir}" >> "%LOG%" 2>&1\r\n'
                # 2026-05-11 pre-flight warmup (опция E1): открываем-закрываем
                # каждый файл в target onedir через [System.IO.File]::Open.
                # Цель — спровоцировать Defender начать post-close-scan СЕЙЧАС,
                # пока bootstrap ждёт окончания PS-цикла (~5-15 сек на 200
                # файлов PyInstaller bundle), а не во время следующего move.
                # $EAP='SilentlyContinue' глотает access-denied на закрытых
                # системой файлах. Write-Output → захватывается через >> в LOG.
                'echo [%date% %time%] pre-flight warmup: opening files to flush AV scan queue >> "%LOG%" 2>&1\r\n'
                'powershell -NoProfile -ExecutionPolicy Bypass -Command '
                "\"$EAP='SilentlyContinue'; $n=0; "
                f"Get-ChildItem -LiteralPath '{target}' -Recurse -File -ErrorAction SilentlyContinue "
                "| ForEach-Object { try { $f=[System.IO.File]::Open($_.FullName,'Open','Read','ReadWrite'); $f.Close(); $n++ } catch {} }; "
                "Write-Output ('warmup opened ' + $n + ' files')\" "
                '>> "%LOG%" 2>&1\r\n'
                # 2. Move target → .old с retry-loop. Windows Defender / антивирус
                #    могут держать handle на .exe секунд 5-60 после смерти процесса
                #    (real-time scan). 30 попыток × 2 сек = 60-секундное окно.
                #    На v1.0.41 наблюдался кейс Defender+Yandex стэком > 30с.
                #    move /y даёт надёжный errorlevel (ren тихо проваливается).
                'echo [%date% %time%] moving target to .old >> "%LOG%" 2>&1\r\n'
                "set /a move_tries=0\r\n"
                ":try_move\r\n"
                "set /a move_tries+=1\r\n"
                'echo [%date% %time%] move attempt %move_tries% >> "%LOG%" 2>&1\r\n'
                f'move /y "{target}" "{old_dir}" >> "%LOG%" 2>&1\r\n'
                "if not errorlevel 1 goto move_ok\r\n"
                'echo [%date% %time%]   attempt %move_tries% failed (target locked) >> "%LOG%" 2>&1\r\n'
                # 2026-05-11 (v1.0.44): на каждой 3-й failed-попытке вызываем
                # update_helper.ps1 в режиме Diagnose. Заменяет эвристический
                # `tasklist | findstr` на авторитетный Restart Manager API,
                # который через rstrtmgr.dll возвращает точный список holder'ов
                # (PID, AppName, ServiceName, Type=Critical/Service/MainWindow/etc).
                "set /a mod_check=%move_tries% %% 3\r\n"
                "if %mod_check% EQU 0 (\r\n"
                '  echo [%date% %time%]   RM API diagnose snapshot: >> "%LOG%" 2>&1\r\n'
                "  powershell -NoProfile -ExecutionPolicy Bypass "
                f'-File "{ps_helper_path}" -Mode Diagnose '
                f'-Target "{target}" -LogPath "{log_path}" >> "%LOG%" 2>&1\r\n'
                ")\r\n"
                "if %move_tries% LSS 30 (\r\n"
                '  echo [%date% %time%]   waiting 2s before retry >> "%LOG%" 2>&1\r\n'
                "  ping -n 3 127.0.0.1 > nul 2>&1\r\n"   # ≈ 2 sec
                "  goto try_move\r\n"
                ")\r\n"
                'echo [%date% %time%] retry exhausted, escalating to RM API + reboot-defer fallback >> "%LOG%" 2>&1\r\n'
                # 2026-05-11 (v1.0.44): escalation вместо немедленного fail.
                # update_helper.ps1 -Mode Defer:
                #   1) RM Diagnose (точный список holder'ов в лог).
                #   2) Copy new bundle в staging dir `target.new` (нет AV race).
                #   3) MoveFileEx P/Invoke: schedule delete target/* + rename
                #      staging → target ПРИ СЛЕДУЮЩЕМ рестарте Windows.
                #   4) Write pending_reboot.txt в project_root.
                #   5) Delete pending_rollback.txt (НЕ откатываем версию).
                # Exit 0 если defer сработал → Studio показывает баннер
                # «нужна перезагрузка» вместо popup'а ошибки.
                # Exit 1 если даже MoveFileEx упал → старый failed-path.
                "powershell -NoProfile -ExecutionPolicy Bypass "
                f'-File "{ps_helper_path}" -Mode Defer '
                f'-Target "{target}" -NewSrc "{new_src}" '
                f'-ProjectRoot "{project_root}" -TargetVersion "{self.target_version}" '
                f'-LogPath "{log_path}" >> "%LOG%" 2>&1\r\n'
                "if errorlevel 1 (\r\n"
                '  echo [%date% %time%] RM API + MoveFileEx defer fallback ALSO failed, writing failed marker >> "%LOG%" 2>&1\r\n'
                f'  echo move_failed > "{failed_marker}"\r\n'
                f'  start "" "{studio_exe}"\r\n'
                "  exit /b 1\r\n"
                ")\r\n"
                'echo [%date% %time%] reboot-deferred install scheduled successfully >> "%LOG%" 2>&1\r\n'
                f'start "" "{studio_exe}"\r\n'
                "exit /b 0\r\n"
                "rem ---- legacy path retained below for reference but never reached after defer ----\r\n"
                'echo [%date% %time%] (legacy) MOVE FAILED after %move_tries% tries -- target locked, aborting >> "%LOG%" 2>&1\r\n'
                f'echo move_failed > "{failed_marker}"\r\n'
                f'start "" "{studio_exe}"\r\n'
                "exit /b 1\r\n"
                ":move_ok\r\n"
                'echo [%date% %time%] move succeeded on attempt %move_tries% >> "%LOG%" 2>&1\r\n'
                # 3. Copy new bundle.
                'echo [%date% %time%] copying new bundle >> "%LOG%" 2>&1\r\n'
                'powershell -NoProfile -ExecutionPolicy Bypass -Command '
                f'"Copy-Item -LiteralPath \'{new_src}\' -Destination \'{target}\' '
                '-Recurse -Force" >> "%LOG%" 2>&1\r\n'
                "if errorlevel 1 (\r\n"
                '  echo [%date% %time%] COPY FAILED, rolling back >> "%LOG%" 2>&1\r\n'
                f'  if exist "{target}" rmdir /s /q "{target}" >> "%LOG%" 2>&1\r\n'
                f'  move /y "{old_dir}" "{target}" >> "%LOG%" 2>&1\r\n'
                f'  echo copy_failed > "{failed_marker}"\r\n'
                f'  start "" "{studio_exe}"\r\n'
                "  exit /b 1\r\n"
                ")\r\n"
                # 4. SUCCESS — delete rollback marker (Studio при старте
                #    увидит что markerа нет → НЕ откатит version.json).
                'echo [%date% %time%] success -- deleting rollback marker >> "%LOG%" 2>&1\r\n'
                f'if exist "{rollback_marker}" del /f /q "{rollback_marker}" >> "%LOG%" 2>&1\r\n'
                'echo [%date% %time%] waiting 2s before launching new Studio >> "%LOG%" 2>&1\r\n'
                "ping -n 3 127.0.0.1 > nul 2>&1\r\n"   # ≈ 2 sec (was timeout /t 2 — broken)
                # 5. Start updated Studio with retry-loop.
                "set /a tries=0\r\n"
                ":try_start\r\n"
                "set /a tries+=1\r\n"
                'echo [%date% %time%] start attempt %tries% >> "%LOG%" 2>&1\r\n'
                f'start "" "{studio_exe}"\r\n'
                'echo [%date% %time%]   waiting 5s for Studio to appear in tasklist >> "%LOG%" 2>&1\r\n'
                "ping -n 6 127.0.0.1 > nul 2>&1\r\n"   # ≈ 5 sec (was timeout /t 5 — broken)
                'tasklist /FI "IMAGENAME eq Storyboard Studio.exe" 2>nul '
                '| find /I "Storyboard Studio.exe" >nul\r\n'
                "if errorlevel 1 (\r\n"
                "  if %tries% LSS 3 goto try_start\r\n"
                ")\r\n"
                'echo [%date% %time%] waiting 5s before cleanup >> "%LOG%" 2>&1\r\n'
                "ping -n 6 127.0.0.1 > nul 2>&1\r\n"   # ≈ 5 sec
                # 6. Cleanup .old (update_dir НЕ удаляем — log нужен).
                f'if exist "{old_dir}" rmdir /s /q "{old_dir}" >> "%LOG%" 2>&1\r\n'
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
