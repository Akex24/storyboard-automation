# -*- coding: utf-8 -*-
"""
threads/compile_episode.py — поток сборки серии в zip.

CompileEpisodeThread проходит по всем блокам эпизода
(`episodes.json[ep_id].montage_card.blocks`) и для каждого собирает:
  • рефы (location/objects/characters из refs_decisions)
  • сторибоарды (output/storyboards/<ep>_block<n>(_<x>).jpg)
  • Seedance промпт .txt (fallback chain: active tab → original → save-copy)

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
from typing import Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal


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
                self._copy_block_refs(show_root, block, decisions, block_dir)
                self._copy_storyboards(show_root, n, block_dir)
                self._copy_seedance_txt(show_root, n, block_dir)

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
                          decisions: dict, dest_dir: Path) -> None:
        """Копирует рефы блока (location/objects/characters) в dest_dir.
        Логика повторяет _on_block_refs_btn в storyboard_app.py (этап 3+5):
        slug → refs_decisions[<cat>][<slug>].filename → реальный путь
        на диске → shutil.copy2. На пропавшие — log в stderr, продолжаем.
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
            final_name = src.name
            if final_name in seen_basenames:
                final_name = f"{cat}__{src.name}"
            seen_basenames.add(final_name)
            try:
                shutil.copy2(src, dest_dir / final_name)
            except Exception as e:
                self._log(f"copy failed for {src}: "
                          f"{type(e).__name__}: {e}")

    def _copy_storyboards(self, show_root: Path, n: int,
                           dest_dir: Path) -> None:
        """Копирует все сторибоарды блока: ep<X>_block<n>(_<X>).(jpg|jpeg|png)
        из shows/<show>/output/storyboards/."""
        sb_dir = show_root / "output" / "storyboards"
        if not sb_dir.is_dir():
            return
        pat = re.compile(
            rf'^{re.escape(self.ep_id)}_block{n}(_\d+)?\.(jpg|jpeg|png)$',
            re.IGNORECASE
        )
        try:
            for f in sb_dir.iterdir():
                if f.is_file() and pat.match(f.name):
                    try:
                        shutil.copy2(f, dest_dir / f.name)
                    except Exception as e:
                        self._log(f"storyboard copy failed {f.name}: "
                                  f"{type(e).__name__}: {e}")
        except Exception as e:
            self._log(f"storyboards listdir failed: "
                      f"{type(e).__name__}: {e}")

    def _copy_seedance_txt(self, show_root: Path, n: int,
                            dest_dir: Path) -> None:
        """Копирует Seedance промпт блока в dest_dir под именем
        <show>_<ep>_block_<n>.txt. Fallback цепь:
          1) Активная вкладка из <ep>_block_<n>_tabs.json (если active_idx
             ссылается на конкретный _tab<K>.txt → берём его).
          2) Оригинал <ep>_block_<n>.txt.
          3) Save-button копия .cache/_block_view/<ep>_block<n>/
             <show>_<ep>_block_<n>.txt.
        Если все 3 пусты — пропускаем без error (log).
        """
        seed_dir = show_root / "output" / "seedance"
        original = seed_dir / f"{self.ep_id}_block_{n}.txt"
        tabs_json = seed_dir / f"{self.ep_id}_block_{n}_tabs.json"
        save_copy = (show_root / ".cache" / "_block_view"
                     / f"{self.ep_id}_block{n}"
                     / f"{self.show_slug}_{self.ep_id}_block_{n}.txt")

        chosen_path: Optional[Path] = None
        chosen_text: Optional[str] = None

        # (1) Active tab из _tabs.json.
        if tabs_json.is_file():
            try:
                state = json.loads(tabs_json.read_text(encoding="utf-8"))
                active_idx = state.get('active_idx')
                tabs = state.get('tabs') or []
                if (isinstance(active_idx, int)
                        and 0 <= active_idx < len(tabs)):
                    tab_entry = tabs[active_idx]
                    if isinstance(tab_entry, dict):
                        tab_file = tab_entry.get('file')
                        if isinstance(tab_file, str) and tab_file:
                            tab_path = seed_dir / tab_file
                            if tab_path.is_file():
                                chosen_path = tab_path
            except Exception as e:
                self._log(f"tabs.json read failed for block {n}: "
                          f"{type(e).__name__}: {e}")

        # (2) Оригинал.
        if chosen_path is None and original.is_file():
            chosen_path = original

        # (3) Save-copy.
        if chosen_path is None and save_copy.is_file():
            chosen_path = save_copy

        if chosen_path is None:
            self._log(f"no Seedance .txt found for block {n}")
            return

        try:
            chosen_text = chosen_path.read_text(encoding="utf-8")
        except Exception as e:
            self._log(f"Seedance .txt read failed {chosen_path}: "
                      f"{type(e).__name__}: {e}")
            return

        dest_name = f"{self.show_slug}_{self.ep_id}_block_{n}.txt"
        try:
            (dest_dir / dest_name).write_text(chosen_text, encoding="utf-8")
        except Exception as e:
            self._log(f"Seedance .txt write failed: "
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
