"""
agents/camera_director.py — режиссёр ракурсов для версий шота (Mode C).

НАЗНАЧЕНИЕ. В Mode C при батч-генерации N версий одного шота каждая версия
должна получать СВОЙ ракурс камеры, чтобы версии были визуально разнообразны:
    • v1 — авторский ракурс (из сценария). НЕ ТРОГАЕМ.
    • v2..vN — альтернативные ракурсы, которые предлагает агент-режиссёр
      (Sonnet) одним батч-вызовом, по двум осям:
          дистанция: close / medium / wide
          позиция:   front / side / back / high / low
      Набор подстраивается под N, без проверяющих агентов поверх (быстро).

Модуль ИНЕРТНЫЙ на этапе Коммита 1: его никто не импортирует, поведение
Studio не меняется. Подключается тонкими вызовами позже (ниточки в
_start_storyboard_block_mode_c и GenerateThread).

ИЗОЛЯЦИЯ / Mode A/B. Вся логика камер-режиссёра собрана здесь. Файл зовётся
только из Mode C-кода. Mode A/B сюда не заходят. Откат фичи = убрать вызовы
этого модуля; сам файл можно удалить, от него ничто не зависит.

CROSS-PLATFORM. propose_cameras запускает `claude -p` subprocess'ом; на win32
обязателен creationflags=CREATE_NO_WINDOW (тихий backend-вызов, без окна cmd).
Чистый stdlib (subprocess/json/re), путей к файлам не трогаем. Mac == Win.

LAZY-IMPORT. storyboard_app запускается как `__main__` в .app, поэтому его
хелперы (_replace_panel_body / _extract_panel_body) резолвятся ЛЕНИВО через
_AppProxy (паттерн скопирован из threads/generate.py), чтобы не словить
циклический импорт при сборке PyInstaller. claude CLI приходит параметром
cli_path — модуль сам его не ищет.

Две публичные функции:
    propose_cameras(shot_contexts, n, cli_path) -> {(panel_idx, v): camera_str}
    apply_camera(prompt_text, panel_idx, camera_str) -> str
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


# Модель агента-режиссёра. Тот же tier что Context Reviewer (быстрый Sonnet).
MODEL_CAMERA_DIRECTOR = "claude-sonnet-4-6"

# Таймаут одного CLI-вызова — как у остальных backend-агентов (orchestrator).
SUBPROCESS_TIMEOUT_SEC = 600

# Допустимые оси (для валидации/документации; режиссёр комбинирует их).
_DISTANCE_AXIS = ("close", "medium", "wide")
_POSITION_AXIS = ("front", "side", "back", "high", "low")


# ─────────────────────────────────────────────────────────────────────────
# Lazy proxy на storyboard_app (он = __main__ в .app). Берём ТОЛЬКО
# _replace_panel_body / _extract_panel_body. Паттерн из threads/generate.py.
# ─────────────────────────────────────────────────────────────────────────
class _AppProxy:
    def __getattr__(self, name):
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


# ─────────────────────────────────────────────────────────────────────────
# SYSTEM-промпт режиссёра. Константа внутри модуля (никаких правок инструкций).
# ─────────────────────────────────────────────────────────────────────────
SYSTEM = """\
Ты — режиссёр-постановщик ракурсов для вертикального драматического сериала
(9:16). Тебе дают список шотов одного блока сториборда. Для КАЖДОГО шота уже
есть авторский ракурс (v1) — его ты НЕ трогаешь и НЕ повторяешь. Твоя задача:
предложить N-1 АЛЬТЕРНАТИВНЫХ ракурсов на каждый шот (для версий v2..vN), так
чтобы версии были визуально РАЗНООБРАЗНЫ и отличались и от авторского, и друг
от друга.

Каждый ракурс собирается по двум осям:
  • дистанция: close / medium / wide
  • позиция камеры: front / side / back / high / low
Комбинируй оси так, чтобы набор на шот был максимально разным (разная
дистанция И/ИЛИ разная позиция). Учитывай смысл шота: число персонажей,
действие, есть ли реплика — но НЕ переписывай действие, только ракурс.

ЖЁСТКИЕ ЗАПРЕТЫ (тех-правила проекта, главнее разнообразия):
  • Никаких "looking at camera" / "facing camera" — персонаж в камеру не смотрит.
  • Ракурс — короткая операторская фраза на английском (например
    "Medium shot, side angle" / "Wide low-angle shot" / "Close-up from above").
  • Не выдумывай новых объектов, локаций, действий — только камера.

ФОРМАТ ВЫВОДА — СТРОГО ВАЛИДНЫЙ JSON, без markdown, начинай сразу с '{':
{
  "shots": [
    {"panel_idx": 0, "cameras": ["<ракурс v2>", "<ракурс v3>"]},
    {"panel_idx": 1, "cameras": ["<ракурс v2>", "<ракурс v3>"]}
  ]
}
Длина "cameras" для КАЖДОГО шота = N-1 (ровно столько, сколько просят).
Только голый JSON, без комментариев и пояснений.
"""


# ─────────────────────────────────────────────────────────────────────────
# Публичное API
# ─────────────────────────────────────────────────────────────────────────
def propose_cameras(shot_contexts: List[dict],
                    n: int,
                    cli_path: Optional[str]) -> Dict[Tuple[int, int], str]:
    """Батч-вызов агента-режиссёра. Возвращает альт-ракурсы для версий v2..vN.

    Аргументы:
      shot_contexts — список словарей по шотам блока, каждый:
          {
            "panel_idx": int,
            "scene_action": str,       # текст действия шота
            "dialog": str,             # реплика (или "" если нет)
            "characters_count": int,   # число персонажей в шоте
            "author_camera": str,      # авторский ракурс v1 (чтобы не дублировать)
          }
      n        — число версий на шот (>=1).
      cli_path — путь к claude CLI (приходит из спавнера, модуль сам не ищет).

    Возврат: {(panel_idx, version_index): camera_str} ТОЛЬКО для v=2..N.
             v1 в словаре отсутствует (авторский ракурс не подменяем).
             Пустой dict если n<=1, нет контекста, CLI недоступен или
             ответ не распарсился (фича просто не применится — версии
             останутся с авторским ракурсом, без падения).
    """
    if n <= 1 or not shot_contexts:
        return {}
    if not cli_path:
        return {}
    try:
        user_prompt = _build_user_prompt(shot_contexts, n)
        raw = _call_sonnet(SYSTEM, user_prompt, cli_path)
        return _parse_cameras(raw, shot_contexts, n)
    except Exception as e:
        # Режиссёр — улучшение, а не критический путь. Любой сбой = тихий
        # фолбэк на авторский ракурс для всех версий.
        try:
            sys.stderr.write(f"[camera_director] propose_cameras failed: {e}\n")
        except Exception:
            pass
        return {}


def apply_camera(prompt_text: str, panel_idx: int, camera_str: str) -> str:
    """Подменяет ракурс в строке-метке CAMERA: тела Panel N+1.

    Чистый текст→текст. Обёртка над storyboard_app._extract_panel_body +
    _replace_panel_body: достаёт тело панели, меняет в нём строку
    'CAMERA: ...' на 'CAMERA: <camera_str>' (если метки нет — добавляет
    её первой строкой тела), возвращает обновлённый полный текст промпта.

    Если panel_idx не найден / camera_str пустой — возвращает prompt_text
    без изменений (инвариант: ничего лишнего не ломаем).
    """
    if not camera_str or not camera_str.strip():
        return prompt_text
    body = _sa._extract_panel_body(prompt_text, panel_idx)
    if body is None:
        return prompt_text
    new_body = _swap_camera_line(body, camera_str.strip())
    return _sa._replace_panel_body(prompt_text, panel_idx, new_body)


# ─────────────────────────────────────────────────────────────────────────
# Внутренние хелперы
# ─────────────────────────────────────────────────────────────────────────
_CAMERA_LINE_RE = re.compile(r'(?im)^[ \t]*CAMERA:[ \t]*.*$')


def _swap_camera_line(body: str, camera_str: str) -> str:
    """Заменяет первую строку 'CAMERA: ...' в теле панели на новый ракурс.
    Если метки CAMERA: в теле нет — добавляет её первой строкой."""
    new_line = f"CAMERA: {camera_str}"
    if _CAMERA_LINE_RE.search(body):
        return _CAMERA_LINE_RE.sub(new_line, body, count=1)
    return new_line + "\n" + body


def _build_user_prompt(shot_contexts: List[dict], n: int) -> str:
    """Сериализует контекст шотов блока в user-prompt для режиссёра."""
    payload = {
        "versions_per_shot": n,
        "alternatives_needed_per_shot": n - 1,
        "shots": [
            {
                "panel_idx": sc.get("panel_idx"),
                "scene_action": (sc.get("scene_action") or "")[:600],
                "dialog": (sc.get("dialog") or "")[:300],
                "characters_count": sc.get("characters_count", 0),
                "author_camera_v1": sc.get("author_camera") or "",
            }
            for sc in shot_contexts
        ],
    }
    return (
        f"N (версий на шот) = {n}. Нужно по {n - 1} альтернативных ракурса "
        f"на КАЖДЫЙ шот (для v2..v{n}). v1 (author_camera_v1) не трогать.\n\n"
        "ШОТЫ БЛОКА (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _call_sonnet(system_prompt: str, user_prompt: str, cli_path: str) -> str:
    """Один вызов `claude -p` через subprocess. Паттерн из
    montage_orchestrator._run_claude. На win32 — без консольного окна."""
    cmd = [cli_path, "-p",
           "--system-prompt", system_prompt,
           "--output-format", "text",
           "--model", MODEL_CAMERA_DIRECTOR]
    kwargs = {
        "input": user_prompt,
        "capture_output": True,
        "text": True,
        "timeout": SUBPROCESS_TIMEOUT_SEC,
        "encoding": "utf-8",
    }
    if sys.platform == "win32":
        # Скрываем окно cmd — это тихий backend-вызов AI.
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    r = subprocess.run(cmd, **kwargs)
    if r.returncode != 0:
        stderr = (r.stderr or "")[:500]
        raise RuntimeError(f"claude exit={r.returncode}: {stderr}")
    return (r.stdout or "").strip()


def _strip_json_fence(s: str) -> str:
    """Убирает ```json ... ``` обёртку если Sonnet всё же её добавил."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r'^```[a-zA-Z]*\n', '', s)
        s = re.sub(r'\n```$', '', s)
    return s.strip()


def _parse_cameras(raw: str,
                   shot_contexts: List[dict],
                   n: int) -> Dict[Tuple[int, int], str]:
    """Парсит JSON-ответ режиссёра в {(panel_idx, v): camera_str} для v=2..N.

    Берёт по каждому шоту не больше n-1 ракурсов; если режиссёр прислал
    меньше — заполняем только что есть (остальные версии останутся с
    авторским ракурсом). Неизвестные panel_idx игнорируем."""
    data = json.loads(_strip_json_fence(raw))
    valid_idx = {sc.get("panel_idx") for sc in shot_contexts}
    out: Dict[Tuple[int, int], str] = {}
    for shot in data.get("shots", []):
        pi = shot.get("panel_idx")
        if pi not in valid_idx:
            continue
        cams = shot.get("cameras") or []
        for offset, cam in enumerate(cams[: n - 1]):
            if isinstance(cam, str) and cam.strip():
                # offset 0 -> версия v2, offset 1 -> v3, ...
                out[(pi, offset + 2)] = cam.strip()
    return out
