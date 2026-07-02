# -*- coding: utf-8 -*-
"""
threads/compile_episode.py — поток сборки серии в zip.

CompileEpisodeThread проходит по всем блокам эпизода
(`episodes.json[ep_id].montage_card.blocks`) и для каждого собирает:
  • рефы (location/objects/characters из refs_decisions)
  • сторибоарды (output/storyboards/<ep>_block<n>(_<x>).jpg)
  • Seedance промпт .txt — ТОЛЬКО Save-файл <show>_<ep>_block_<n>.txt (нажата
    «💾 Save» в попапе). Блок без Save → нет промпта и нет shots/ в zip.
  • shots/shot_<k>/ — пошотовая нарезка (только для сохранённых блоков),
    через seedance_shot_slicer (тот же путь, что у кнопки «🗂 Рефы блока»).

Всё кладётся в staging-папку `shows/<show>/.cache/_episode_compile/<ep>/`
с подпапками `block_<n>/`. Затем zipуется в одном архиве:
  shows/<show>/output/<ep>/<show>_<ep>.zip

Staging-папка удаляется ВСЕГДА (try/finally) — даже при ошибке.

Сигналы:
  • progress(str)         — текстовое сообщение для логов (не используется в UI).
  • step(str, int)        — (label, percent) для UI.
  • finished(str)         — путь к финальному zip.
  • error(str)            — текст ошибки. Спец-значение 'empty_episode'
                            для случая когда в montage_card нет блоков
                            (UI показывает локализованную подсказку).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

import show_manager
from seedance_shot_slicer import slice_block_to_shots


class CompileEpisodeThread(QThread):
    progress = pyqtSignal(str)
    step     = pyqtSignal(str, int)
    finished = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, project_root: Path, show_slug: str, ep_id: str,
                 parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.show_slug = show_slug
        self.ep_id = ep_id

    def run(self) -> None:
        show_root = self.project_root / "shows" / self.show_slug
        temp_dir = (show_root / ".cache" / "_episode_compile" / self.ep_id)
        try:
            # 1. Прочитать episodes.json → montage_card.blocks.
            ep_meta = self._read_episode_meta(show_root)
            blocks = ((ep_meta.get('montage_card') or {}).get('blocks')
                      or [])
            if not blocks:
                self.error.emit("empty_episode")
                return
            decisions = ep_meta.get('refs_decisions') or {}

            # 2. Подготовить staging.
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)

            # 3. Пройти все блоки.
            total = len(blocks)
            for idx, block in enumerate(blocks):
                n = block.get('n')
                if not isinstance(n, int):
                    continue
                pct = 5 + int((idx / total) * 80)
                self.step.emit(f"Блок {n}/{total}", pct)
                block_dir = temp_dir / f"block_{n}"
                block_dir.mkdir(parents=True, exist_ok=True)
                resolved = self._copy_block_refs(
                    show_root, block, decisions, block_dir)
                self._copy_storyboards(show_root, n, block_dir)
                self._copy_seedance_txt(show_root, n, block_dir)
                # Пошотовая нарезка — только для сохранённых блоков (Save-файл).
                self._slice_shots(show_root, block, n, resolved, block_dir)

            # 4. Зипуем всё.
            self.step.emit("Архивирую…", 88)
            zip_dir = show_root / "output" / self.ep_id
            zip_dir.mkdir(parents=True, exist_ok=True)
            zip_path = zip_dir / f"{self.show_slug}_{self.ep_id}.zip"
            # 2026-05-20: чистим zip_dir от всего кроме нашего zip-файла.
            # macOS Finder при двойном клике на zip распаковывает его рядом
            # в папку `<show>_<ep>/` (или `<...> 2/`, `<...> 3/`...). После
            # нескольких «Собрать серию» в output/<ep>/ накапливаются
            # распакованные копии. Юзер хочет видеть только актуальный zip.
            try:
                for item in zip_dir.iterdir():
                    if item.is_file() and item.name == zip_path.name:
                        continue  # наш zip — оставляем (всё равно перепишется)
                    try:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink()
                    except Exception as e:
                        self._log(f"cleanup failed for {item.name}: "
                                  f"{type(e).__name__}: {e}")
            except Exception as e:
                self._log(f"zip_dir cleanup iterdir failed: "
                          f"{type(e).__name__}: {e}")
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(zip_path, 'w',
                                 zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as zf:
                for f in sorted(temp_dir.rglob("*")):
                    if f.is_file() and f.name not in (
                            '.DS_Store', 'Thumbs.db'):
                        zf.write(f, arcname=f.relative_to(temp_dir))

            self.step.emit("Готово", 100)
            self.finished.emit(str(zip_path))
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            # ВСЕГДА чистим temp — даже на ошибке (план 5).
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ─── Хелперы ────────────────────────────────────────────────────

    def _read_episode_meta(self, show_root: Path) -> dict:
        """Читает episodes.json и возвращает запись для self.ep_id."""
        ep_path = show_root / "episodes.json"
        if not ep_path.is_file():
            return {}
        try:
            data = json.loads(ep_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        ep = data.get(self.ep_id)
        return ep if isinstance(ep, dict) else {}

    def _copy_block_refs(self, show_root: Path, block: dict,
                          decisions: dict, dest_dir: Path
                          ) -> List[Tuple[str, str, Path, str]]:
        """Копирует рефы блока (location/objects/characters) в dest_dir и
        ВОЗВРАЩАЕТ resolved-список [(cat, slug, src, basename)] в порядке
        [location, *objects, *characters] (плоский basename=src.name) — для
        нарезки по шотам (`[@]imgK → resolved[K-1]`, байт-в-байт как
        _on_block_refs_btn). Логика повторяет _on_block_refs_btn (этап 3+5):
        slug → refs_decisions[<cat>][<slug>].filename → путь на диске →
        shutil.copy2. Пропавшие — log в stderr + skip (в resolved НЕ входят,
        как в одиночной кнопке — паритет маппинга).
        """
        cat_to_plural = {'location': 'locations',
                         'object': 'objects',
                         'character': 'characters'}
        refs_root = show_root / "refs"
        targets = []  # list of (cat_singular, slug)
        loc = block.get('location')
        if isinstance(loc, str) and loc:
            targets.append(('location', loc))
        for s in (block.get('objects') or []):
            if isinstance(s, str) and s:
                targets.append(('object', s))
        for s in (block.get('characters') or []):
            if isinstance(s, str) and s:
                targets.append(('character', s))

        seen_basenames: set = set()
        resolved: List[Tuple[str, str, Path, str]] = []
        for cat, slug in targets:
            bucket = (decisions.get(cat)
                      if isinstance(decisions, dict) else None)
            entry = (bucket or {}).get(slug) if isinstance(bucket, dict) else None
            filename = ((entry or {}).get('filename')
                        if isinstance(entry, dict) else None)
            if not filename:
                self._log(f"no filename for {cat}/{slug} in block {block.get('n')}")
                continue
            src = refs_root / cat_to_plural[cat] / filename
            if not src.is_file():
                self._log(f"file not on disk: {src}")
                continue
            # resolved для нарезки: плоский basename (slicer делает свою
            # коллизию ref__). Порядок/skip — как в _on_block_refs_btn.
            resolved.append((cat, slug, src, src.name))
            final_name = src.name
            if final_name in seen_basenames:
                final_name = f"{cat}__{src.name}"
            seen_basenames.add(final_name)
            try:
                shutil.copy2(src, dest_dir / final_name)
            except Exception as e:
                self._log(f"copy failed for {src}: "
                          f"{type(e).__name__}: {e}")

            # 2026-06-04: для персонажа — дополнительно версия-с-сеткой из
            # characters_grid/<slug>/. Имя детерминированное: <orig_stem>_grid.jpg
            # (views/actors.py пишет ровно его). Матчим ТОЧНЫЙ stem, не startswith
            # — иначе реф «laura» поймал бы сетку рефа «laura_2». В zip уезжает
            # grid__<slug>__<name> (префикс не конфликтует с оригиналом). Текстуру
            # в zip НЕ кладём. Зеркало шага 5b-2 в _on_block_refs_btn (Коммит 4).
            if cat == 'character':
                grid_dir = refs_root / "characters_grid" / slug
                target_stem = src.stem + "_grid"
                try:
                    grid_cands = [
                        p for p in grid_dir.iterdir()
                        if p.is_file()
                        and p.suffix.lower() in ('.jpg', '.jpeg', '.png')
                        and p.stem == target_stem
                    ] if grid_dir.is_dir() else []
                    if grid_cands:
                        latest = max(grid_cands,
                                     key=lambda p: p.stat().st_mtime)
                        shutil.copy2(
                            latest, dest_dir / f"grid__{slug}__{latest.name}")
                except Exception as e:
                    self._log(f"grid copy failed for {slug}/{src.stem}: "
                              f"{type(e).__name__}: {e}")

        return resolved

    def _copy_storyboards(self, show_root: Path, n: int,
                           dest_dir: Path) -> None:
        """Копирует ОДИН landscape-лист блока из
        shows/<show>/.cache/_block_view/<ep>_block<n>/<ep>_block<n>.jpg
        (создаётся кнопкой «Сохранить сториборд» в редакторе блока).

        2026-05-20: один блок = один файл. Раньше искали в
        output/storyboards/ с regex по версиям — это была неправильная
        папка (там лежат отдельные шоты `_shot<M>`, не landscape-листы).
        Если файла нет (юзер не нажимал «Сохранить сториборд» для этого
        блока) — пропускаем без error, log в stderr. Confirm-dialog
        перед сборкой с чекбоксом «Все сториборды сохранены» защищает
        юзера от случайных пропусков.
        """
        src = (show_root / ".cache" / "_block_view"
               / f"{self.ep_id}_block{n}"
               / f"{self.ep_id}_block{n}.jpg")
        if not src.is_file():
            self._log(f"no saved storyboard for block {n} "
                      f"(user did not press «Сохранить сториборд»)")
            return
        try:
            shutil.copy2(src, dest_dir / src.name)
        except Exception as e:
            self._log(f"storyboard copy failed {src.name}: "
                      f"{type(e).__name__}: {e}")

    def _copy_seedance_txt(self, show_root: Path, n: int,
                            dest_dir: Path) -> None:
        """Копирует Seedance промпт блока в dest_dir под именем
        <show>_<ep>_block_<n>.txt — ТОЛЬКО из Save-файла (нажата «💾 Save» в
        попапе Seedance): .cache/_block_view/<ep>_block<n>/
        <show>_<ep>_block_<n>.txt (утверждённая активная версия).

        Save-файла нет (Save не нажат) → промпт в zip НЕ кладём (skip + log).
        Так сборка строгая как кнопка «🗂 Рефы блока». Fallback на активную
        вкладку tabs.json / генерационный output/seedance/<ep>_block_<n>.txt
        УБРАН (2026-06-29) — раньше промпт уезжал в zip даже без Save.
        """
        save_copy = (show_root / ".cache" / "_block_view"
                     / f"{self.ep_id}_block{n}"
                     / f"{self.show_slug}_{self.ep_id}_block_{n}.txt")
        if not save_copy.is_file():
            self._log(f"block {n} not saved (no «💾 Save») → no Seedance "
                      f"prompt in zip")
            return
        try:
            shutil.copy2(save_copy, dest_dir / save_copy.name)
        except Exception as e:
            self._log(f"Seedance .txt copy failed {save_copy}: "
                      f"{type(e).__name__}: {e}")

    def _slice_shots(self, show_root: Path, block: dict, n: int,
                      resolved: List[Tuple[str, str, Path, str]],
                      block_dir: Path) -> None:
        """Пошотовая нарезка блока в block_dir/shots/ — ТОЛЬКО если блок
        сохранён (Save-файл есть). Тот же `slice_block_to_shots`, тот же
        источник (Save-файл) / лист с face-сетками / раскладка панелей, что у
        кнопки «🗂 Рефы блока». resolved (порядок [location,*objects,
        *characters]) обеспечивает идентичный маппинг [@]imgK → resolved[K-1].
        """
        save_copy = (show_root / ".cache" / "_block_view"
                     / f"{self.ep_id}_block{n}"
                     / f"{self.show_slug}_{self.ep_id}_block_{n}.txt")
        if not save_copy.is_file():
            return  # не сохранён → нет shots (промпт тоже не уехал в zip)
        sheet = (show_root / ".cache" / "_block_view"
                 / f"{self.ep_id}_block{n}" / f"{self.ep_id}_block{n}.jpg")
        try:
            aspect = show_manager.show_aspect(
                self.project_root, self.show_slug)
        except Exception:
            aspect = "9:16"
        # Раскладка как stitch_shots_to_landscape: 16:9 → 2×2, иначе PANELS(=4)×1.
        grid_cols, grid_rows = (2, 2) if aspect == "16:9" else (4, 1)
        try:
            slice_block_to_shots(
                save_copy, block, resolved,
                sheet if sheet.is_file() else None,
                grid_cols, grid_rows,
                block_dir / "shots", self.ep_id, n, log=self._log,
                grid_root=show_root / "refs" / "characters_grid")
        except Exception as e:
            self._log(f"shot-slicing failed for block {n}: "
                      f"{type(e).__name__}: {e}")

    def _log(self, msg: str) -> None:
        """Stderr-лог с префиксом [compile_episode]. Не валит pipeline."""
        try:
            sys.stderr.write(
                f"[compile_episode] ep={self.ep_id} show={self.show_slug}: "
                f"{msg}\n")
            sys.stderr.flush()
        except Exception:
            pass
