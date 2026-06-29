# -*- coding: utf-8 -*-
"""
seedance_shot_slicer.py — нарезка блочного Seedance-промпта на пошотовые папки.

Шаг 2 фичи «Рефы по шотам». Вызывается из MainWindow._on_block_refs_btn
(storyboard_app.py) ПОСЛЕ сборки папки блока. Для каждого шота блока создаёт
    .cache/_block_view/<ep>_block<N>/shots/shot_<k>/
и кладёт туда:
  • нарезанный промпт <ep>_block<N>_shot<k>.txt — общая шапка (с УРЕЗАННЫМ под
    рефы шота блоком 参考说明 и внешней легендой) + ОДИН сегмент 镜头<k>
    (с предшествующим transition-маркером если был) + общий хвост (技术参数/
    限制). Текст шота слово в слово, не меняется.
  • картинки-рефы, которые использует ИМЕННО этот шот, + ПАНЕЛЬ ЭТОГО шота,
    вырезанную из склеенного листа сториборда (на ней уже face-сетки).

Источник истины «какие рефы у шота» — montage_card: shots[].scene_action с
тегами [@]imgK. Конвенция нумерации (agents/montage_rules_d.py:166,
agents/storyboard_writer_prompts.py:9): [@]img1=локация, далее объекты, далее
персонажи — ровно порядок `resolved` из _on_block_refs_btn. Маппинг
[@]imgK → китайский @imageN (для фильтрации 参考说明 + внешней легенды) — через
распарсенную легенду .txt («@imageN = name») и сопоставление name↔slug.
Номера @image НЕ перенумеровываются. @image1 (Storyboard) — всегда в каждом шоте.

Лист-модуль: только stdlib (re, shutil, pathlib). Без Qt/subprocess/app-импортов
→ нет циклических импортов; кросс-платформенно (pathlib + shutil.copy2, без shell).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ── Маркеры структуры китайского промпта ──────────────────────────────────
_SHOT_RE = re.compile(r'^\s*\[\d{2}:\d{2}-\d{2}:\d{2}\]\s*镜头\s*(\d+)')
_TRANSITION_RE = re.compile(
    r'^\s*\[(?:HARD CUT|MATCH CUT|CONTINUOUS CAMERA MOVE|CONTINUOUS HANDHELD)')
_REF_HDR = '参考说明'             # начало блока 参考说明 в шапке
_SCENE_HDR = '场景设置'           # начало 场景设置 (конец 参考说明)
_TAIL_HDR = '技术参数'            # начало хвоста
_REFBLOCK_RE = re.compile(r'^\s*\[[^\]]*?参考[:：]\s*@image(\d+)\s*\]')
_LEGEND_RE = re.compile(r'^\s*@image(\d+)\s*=\s*(.+?)\s*$')
_IMG_TAG_RE = re.compile(r'\[@\]img(\d+)')
_FENCE_RE = re.compile(r'^\s*```')


def _norm(name: str) -> str:
    """Нормализация для сопоставления name↔slug: lower + пробелы→подчёркивание."""
    return (name or '').strip().lower().replace(' ', '_')


def _used_imgk_for_shot(shot: dict) -> List[int]:
    """Уникальные K из тегов [@]imgK в scene_action шота (по возрастанию)."""
    sa = (shot.get('scene_action') or '') if isinstance(shot, dict) else ''
    return sorted({int(x) for x in _IMG_TAG_RE.findall(sa)})


def _parse_prompt(text: str) -> Optional[dict]:
    """Разбирает блочный Seedance-промпт на части. Возвращает None если
    структура нераспознаваема (нет 参考说明 / 场景设置 / шотов / 技术参数) —
    тогда caller пропускает нарезку (лучше ничего, чем кривой срез).

    Части:
      pre            — строки ДО открывающего ```fence (внешняя легенда + ───)
      fence_open     — строка ```
      head_pre_ref   — 风格/表演风格/时长 (до 参考说明) — verbatim
      ref_pre        — строки внутри 参考说明 до первого реф-блока (вкл. «参考说明：»)
      ref_blocks     — list[(num, [lines])] — каждый блок [XX参考: @imageN]…
      scene          — 场景设置 … до первого шота — verbatim (НЕ фильтруется)
      shots          — list[(num, [lines])] — сегмент шота (+ transition впереди)
      tail           — 技术参数 … 限制 — verbatim
      post           — закрывающий ``` и далее
    """
    lines = text.split('\n')
    fences = [i for i, ln in enumerate(lines) if _FENCE_RE.match(ln)]
    if len(fences) >= 2:
        open_i, close_i = fences[0], fences[-1]
        pre = lines[:open_i]
        fence_open = lines[open_i]
        body = lines[open_i + 1:close_i]
        post = lines[close_i:]            # вкл. закрывающий ```
    else:
        # fence нет — работаем со всем текстом как body (defensive)
        pre, fence_open, body, post = [], None, lines, []

    def _find(pred) -> Optional[int]:
        for i, ln in enumerate(body):
            if pred(ln):
                return i
        return None

    ref_start = _find(lambda l: l.strip().startswith(_REF_HDR))
    scene_start = _find(lambda l: l.strip().startswith(_SCENE_HDR))
    tail_start = _find(lambda l: l.strip().startswith(_TAIL_HDR))
    first_shot = _find(lambda l: _SHOT_RE.match(l))
    if None in (ref_start, scene_start, tail_start, first_shot):
        return None
    if not (ref_start < scene_start <= first_shot < tail_start):
        return None

    # Включить предшествующий transition-маркер первого шота (через пустые).
    shots_start = first_shot
    j = first_shot - 1
    while j >= scene_start and not body[j].strip():
        j -= 1
    if j >= scene_start and _TRANSITION_RE.match(body[j]):
        shots_start = j

    head_pre_ref = body[:ref_start]
    ref_region = body[ref_start:scene_start]
    scene = body[scene_start:shots_start]
    shots_region = body[shots_start:tail_start]
    tail = body[tail_start:]

    # Разбор 参考说明 на header + блоки [XX参考: @imageN].
    ref_pre: List[str] = []
    ref_blocks: List[Tuple[int, List[str]]] = []
    cur: Optional[List] = None
    for ln in ref_region:
        m = _REFBLOCK_RE.match(ln)
        if m:
            if cur is not None:
                ref_blocks.append((cur[0], cur[1]))
            cur = [int(m.group(1)), [ln]]
        elif cur is None:
            ref_pre.append(ln)
        else:
            cur[1].append(ln)
    if cur is not None:
        ref_blocks.append((cur[0], cur[1]))

    # Разбор шотов: transition-строки принадлежат СЛЕДУЮЩЕМУ шоту.
    shots: List[Tuple[int, List[str]]] = []
    pending: List[str] = []
    cur_shot: Optional[List] = None
    for ln in shots_region:
        m = _SHOT_RE.match(ln)
        if m:
            if cur_shot is not None:
                shots.append((cur_shot[0], cur_shot[1]))
            cur_shot = [int(m.group(1)), pending + [ln]]
            pending = []
            continue
        if _TRANSITION_RE.match(ln):
            pending.append(ln)            # лид-ин следующего шота
            continue
        if cur_shot is None:
            pending.append(ln)
        else:
            cur_shot[1].append(ln)
    if cur_shot is not None:
        shots.append((cur_shot[0], cur_shot[1]))

    return {
        'pre': pre, 'fence_open': fence_open,
        'head_pre_ref': head_pre_ref,
        'ref_pre': ref_pre, 'ref_blocks': ref_blocks,
        'scene': scene, 'shots': shots, 'tail': tail, 'post': post,
    }


def _filter_legend(pre: List[str], used: set) -> List[str]:
    """Внешняя легенда: оставить @imageN-строки только для used (+@image1).
    Строки-разделители (─────, БЛОК N) сохраняются как есть."""
    out: List[str] = []
    for ln in pre:
        m = _LEGEND_RE.match(ln)
        if m and int(m.group(1)) not in used:
            continue
        out.append(ln)
    return out


def _build_shot_text(parsed: dict, shot_lines: List[str], used: set) -> str:
    """Собрать нарезанный промпт одного шота из распарсенных частей."""
    out: List[str] = []
    out += _filter_legend(parsed['pre'], used)
    if parsed['fence_open'] is not None:
        out.append(parsed['fence_open'])
    out += parsed['head_pre_ref']
    out += parsed['ref_pre']
    for num, blk in parsed['ref_blocks']:
        if num in used:
            out += blk
    out += parsed['scene']
    out += shot_lines
    out += parsed['tail']
    out += parsed['post']
    return '\n'.join(out)


def slice_block_to_shots(
    seedance_txt_path: Path,
    block: dict,
    resolved: List[Tuple[str, str, Path, str]],
    storyboard_sheet: Optional[Path],
    grid_cols: int,
    grid_rows: int,
    shots_root: Path,
    ep_id: str,
    block_n: int,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Нарезать блочный Seedance-промпт на пошотовые папки.

    Аргументы:
      seedance_txt_path — output/seedance/<ep>_block_<N>.txt (ОСНОВНОЙ, вкладка 1)
      block             — montage_card блок (shots[] со scene_action)
      resolved          — [(category, slug, src_path, basename)] в порядке
                          [location, *objects, *characters] — ровно [@]imgK
      storyboard_sheet  — склеенный лист блока <ep>_block<N>.jpg (С face-сетками,
                          из dest_dir). Из него ВЫРЕЗАЕТСЯ панель шота (@image1)
                          по детерминированной раскладке stitch_shots_to_landscape.
                          None / нет файла → панель не кладём (лог).
      grid_cols/rows    — раскладка панелей листа (stitch): 16:9 → 2×2, 9:16 → 4×1.
                          panel_w=W//cols, panel_h=H//rows; панель шота N = cell(N-1)
                          = ((N-1)%cols*pw, (N-1)//cols*ph) (без зазоров).
      shots_root        — <dest_dir>/shots (создаётся; caller уже снёс старую
                          rmtree-циклом)
      ep_id, block_n    — для имени файла <ep>_block<N>_shot<k>.txt/.jpg
      log               — callable(str) для диагностики [block_refs]; может быть None

    Возвращает количество нарезанных шотов (0 если промпт нераспознан/нет шотов).
    Все ошибки копирования — в log, не кидаем (caller не должен падать).
    """
    def _log(msg: str) -> None:
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    try:
        text = seedance_txt_path.read_text(encoding='utf-8')
    except Exception as e:
        _log(f"[block_refs] ep={ep_id} block={block_n}: cannot read seedance "
             f"prompt {seedance_txt_path}: {type(e).__name__}: {e}\n")
        return 0

    parsed = _parse_prompt(text)
    if parsed is None or not parsed['shots']:
        _log(f"[block_refs] ep={ep_id} block={block_n}: seedance prompt "
             f"structure not recognized → skip shot-slicing\n")
        return 0

    # slug → @imageN из легенды (name↔slug). @image1=Storyboard не маппим (всегда).
    legend: Dict[int, str] = {}
    for ln in parsed['pre']:
        m = _LEGEND_RE.match(ln)
        if m:
            legend[int(m.group(1))] = m.group(2)
    slug_to_imageN: Dict[str, int] = {
        _norm(name): num for num, name in legend.items() if num != 1
    }

    shots_struct = block.get('shots') or []
    by_n = {s.get('n'): s for s in shots_struct if isinstance(s, dict)}

    # Склеенный лист (с face-сетками) — открываем ОДИН раз, режем панель на шот.
    # PIL ленивый (как в stitch_shots_to_landscape); бандлится для face-grid.
    sheet_img = None
    if storyboard_sheet is not None:
        try:
            if storyboard_sheet.exists():
                from PIL import Image as _PILImage
                sheet_img = _PILImage.open(storyboard_sheet).convert("RGB")
        except Exception as e:
            _log(f"[block_refs] ep={ep_id} block={block_n}: cannot open "
                 f"storyboard sheet {storyboard_sheet}: {type(e).__name__}: {e}\n")
            sheet_img = None
    if sheet_img is None:
        _log(f"[block_refs] ep={ep_id} block={block_n}: no storyboard sheet "
             f"→ shots get no panel\n")

    sliced = 0
    for k, (shot_num, shot_lines) in enumerate(parsed['shots'], start=1):
        # сопоставление 镜头N текста ↔ структурный шот: по shot_num, фоллбэк позиции
        s = by_n.get(shot_num)
        if s is None and 0 <= k - 1 < len(shots_struct):
            s = shots_struct[k - 1]
        used_imgk = _used_imgk_for_shot(s) if isinstance(s, dict) else []

        used_imageN = {1}                      # storyboard всегда
        copy_files: List[Tuple[Path, str]] = []
        for K in used_imgk:
            if 1 <= K <= len(resolved):
                _cat, slug, src, basename = resolved[K - 1]
                copy_files.append((src, basename))
                n = slug_to_imageN.get(slug)
                if n is not None:
                    used_imageN.add(n)
                else:
                    _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                         f"slug '{slug}' ([@]img{K}) not in legend → ref-block "
                         f"not kept in 参考说明\n")
            else:
                _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                     f"[@]img{K} out of resolved range ({len(resolved)}) → skip\n")

        shot_dir = shots_root / f"shot_{shot_num}"
        try:
            shot_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                 f"mkdir failed: {type(e).__name__}: {e}\n")
            continue

        # 1) нарезанный промпт
        try:
            out_text = _build_shot_text(parsed, shot_lines, used_imageN)
            (shot_dir / f"{ep_id}_block{block_n}_shot{shot_num}.txt").write_text(
                out_text, encoding='utf-8')
        except Exception as e:
            _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                 f"write prompt failed: {type(e).__name__}: {e}\n")

        # 2) рефы шота + сториборд (collision basename → префикс категории)
        seen: set = set()
        for src, basename in copy_files:
            dest_name = basename
            if dest_name in seen:
                dest_name = f"ref__{basename}"
            seen.add(dest_name)
            try:
                shutil.copy2(src, shot_dir / dest_name)
            except Exception as e:
                _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                     f"copy ref failed {src}: {type(e).__name__}: {e}\n")
        # панель ЭТОГО шота — кроп из склеенного листа по раскладке stitch
        if sheet_img is not None:
            i = shot_num - 1
            cells = grid_cols * grid_rows
            if grid_cols >= 1 and grid_rows >= 1 and 0 <= i < cells:
                W, H = sheet_img.size
                pw, ph = W // grid_cols, H // grid_rows
                cx, cy = (i % grid_cols) * pw, (i // grid_cols) * ph
                try:
                    panel = sheet_img.crop((cx, cy, cx + pw, cy + ph))
                    panel.save(
                        shot_dir / f"{ep_id}_block{block_n}_shot{shot_num}.jpg",
                        quality=95)
                except Exception as e:
                    _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                         f"crop panel failed: {type(e).__name__}: {e}\n")
            else:
                _log(f"[block_refs] ep={ep_id} block={block_n} shot{shot_num}: "
                     f"panel index {i} out of grid {grid_cols}x{grid_rows} → no panel\n")

        sliced += 1

    if sheet_img is not None:
        try:
            sheet_img.close()
        except Exception:
            pass

    _log(f"[block_refs] ep={ep_id} block={block_n}: shots sliced={sliced} "
         f"dest={shots_root}\n")
    return sliced
