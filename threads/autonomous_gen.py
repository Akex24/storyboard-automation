# -*- coding: utf-8 -*-
"""
threads/autonomous_gen.py — автономная генерация одного рефа в фоне.

`AutonomousGenThread` спавнит headless `claude -p` subprocess с ФОКУСИРОВАННЫМ
промптом «Сгенерируй ОДНУ локацию/объект/персонажа по такому-то описанию.
Делай всё по CLAUDE.md (web search → промпт → pipeline.py generate →
геометрия). Никакого диалога, печатай только короткие прогресс-фразы и в
конце `✓ done` или `✗ error: <причина>`.»

Каждая кнопка «🎨 Сгенерировать» в чате запускает свой `AutonomousGenThread`.
Output subprocess'а парсится для emit'а progress (одна короткая строка
статуса) и итогового сигнала done/error.

История: создано 2026-05-04 для sub-MVP «кнопка автономной генерации
в чате эпизода». Использует тот же CLI-механизм что `RunEpisodeThread`,
но с другим промптом и без `--continue` (каждая генерация — отдельная
сессия чтобы не загрязнять основной чат).
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal


class _AppProxy:
    """Прокси к module storyboard_app — приоритет __main__.
    См. подробное объяснение в threads/update.py."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


def build_autonomous_prompt(gen_type: str, name: str, description: str,
                             ep_id: Optional[str] = None,
                             show_slug: Optional[str] = None) -> str:
    """Собирает фокусированный промпт для одной автономной генерации.

    Пайплайн зависит от типа:
      • **location** — ВСЕГДА с веб-поиском (нужно понять как реально
        выглядит такое место). Веб-поиск → промпт → pipeline.py →
        геометрия в `<name>_geometry.txt`.
      • **object** — БЕЗ веб-поиска. Описание из чата уже содержит
        нужные детали. Сразу промпт (объект на белом фоне) →
        pipeline.py. Геометрия не нужна — это объект, не локация.
      • **character** — этот тип НЕ должен приходить сюда (рефы
        персонажей создаются отдельно через GenerateActorRefThread).
        Если попал — graceful error.

    Промпт ОЧЕНЬ строгий по выводу: только prefix-строки прогресса
    (`🌐 Ищу…`, `🎨 Генерирую…`, `📝 Пишу геометрию…`) и финальная
    `✓ done <имя_файла.jpg>` или `✗ error: <короткое сообщение>`."""
    show_str = show_slug or "<активный сериал>"
    ep_str = ep_id or "<текущий эпизод>"

    if gen_type == "location":
        pipeline_steps = (
            "Пайплайн ЛОКАЦИИ — 4 шага по порядку:\n"
            f"  1. 🌐 Веб-поиск: найди как реально выглядит «{name}» "
            f"(используй WebSearch tool). 2-3 запроса, посмотри картинки,"
            f" пойми реальную геометрию места.\n"
            f"  2. ✏ Промпт: напиши промпт для генерации картинки. "
            f"Опирайся на описание и веб-поиск. Формат — см. "
            f"PIPELINE_RULES.txt раздел 1 (empty scene, no people, "
            f"cinematic lighting, 16:9, photorealistic).\n"
            f"     🔴 КРИТИЧНО — SOCIAL/GENRE CONTEXT: если в "
            f"`description` от чат-агента есть слова про social/economic "
            f"уровень (luxury / expensive / wealthy / upscale / elegant /"
            f" богатый / дорогой / элегантный) — ОБЯЗАТЕЛЬНО переведи их"
            f" в English-промпт (luxury, upscale, wealthy interior,"
            f" expensive, elegant). Без этих слов Gemini выдаёт generic"
            f" outdated / cheap-looking / generic-suburban дефолт"
            f" независимо от того что в description.\n"
            f"      • Хорошо: 'lawn of a luxury upscale country house,"
            f" expensive elegant exterior, manicured estate landscaping,"
            f" pristine white walls, premium materials'\n"
            f"      • Плохо: 'modest private house, generic suburban"
            f" fence, weathered outdated exterior, low-budget rural"
            f" aesthetic'\n"
            f"     🔴 ВАЖНО: не привязывай локацию к конкретной стране\n"
            f"     (национально-окрашенные стили) если bible не указал\n"
            f"     это явно. Дефолт по географии — generic Western/European\n"
            f"     contemporary (БЕЗ luxury по умолчанию). Уровень достатка\n"
            f"     бери из описания конкретной локации в description:\n"
            f"     «дешёвый мотель» → cheap / run-down; «элитный ресторан»\n"
            f"     → upscale fine dining; «обычная квартира» → middle-class;\n"
            f"     «стройка» → working / realistic. Применяй `luxury` ТОЛЬКО\n"
            f"     когда в description явно указано (дорогой / богатый /\n"
            f"     элитный / luxury / expensive).\n"
            f"     Для бедных/criminal/historical жанров — передавай тот\n"
            f"     контекст что в description (grungy / working-class /\n"
            f"     vintage / period-specific).\n"
            f"  3. 🎨 Генерация через pipeline.py — Bash tool: "
            f"`python3 pipeline.py generate {name} \"<промпт>\"`. "
            f"`--force` НЕ передаём: Studio гарантирует уникальность slug "
            f"перед запуском (если в refs/locations/ уже был файл с тем "
            f"же именем — Studio автоматически переименовала текущий slug "
            f"в `<name>_2`/`<name>_3`/...). pipeline.py остаётся в "
            f"safety-режиме «refuse on collision» — это страховка на случай "
            f"если уникализация всё-таки даст сбой.\n"
            f"Картинка ляжет в shows/{show_str}/refs/locations/{name}.jpg.\n"
            f"  4. 📝 Геометрия: открой картинку через Read tool и опиши "
            f"геометрию в shows/{show_str}/refs/locations/{name}_geometry.txt"
            f" (размеры, мебель, свет, лучшие ракурсы). БЕЗ Anthropic API,"
            f" пиши сам глядя на картинку.\n"
        )
        target_path_hint = (
            f"shows/{show_str}/refs/locations/{name}.jpg + "
            f"{name}_geometry.txt"
        )
    elif gen_type == "object":
        pipeline_steps = (
            "Пайплайн ОБЪЕКТА — РОВНО 2 шага (НИКАКОГО ВЕБ-ПОИСКА!):\n"
            f"  1. ✏ Промпт: напиши промпт для генерации картинки "
            f"объекта на БЕЛОМ фоне (product-shot). Используй описание "
            f"из чата выше: «{description}». Добавь стандартные слова: "
            f"«on white background, product shot, soft even lighting, "
            f"high detail, photorealistic, no people». Без cinematic, "
            f"без 16:9 — для объектов лучше квадрат или вертикаль.\n"
            f"     🔴 SOCIAL/GENRE CONTEXT: если в `description` есть\n"
            f"     указание на economic уровень (luxury / expensive /\n"
            f"     premium / cheap / used / vintage / antique) — перенеси\n"
            f"     эти слова в English-промпт. «Дорогой смартфон» →\n"
            f"     'expensive flagship smartphone, premium build'.\n"
            f"     «Старое ружьё» → 'vintage hunting rifle, worn wood\n"
            f"     stock, aged patina'. Без этого Gemini выдаёт generic\n"
            f"     usable дефолт который может не соответствовать сюжету.\n"
            f"     Для животных (бульдог, кот и т.д.) — переопредели\n"
            f"     белый фон на «in natural environment, photorealistic\n"
            f"     portrait of <animal>, soft natural lighting» — product\n"
            f"     shot на белом для живых существ выглядит абсурдно.\n"
            f"  2. 🎨 Генерация через pipeline.py — Bash tool: "
            f"`python3 pipeline.py generate {name} \"<промпт>\" --kind=object`. "
            f"🔴 Флаг `--kind=object` ОБЯЗАТЕЛЕН — pipeline сохранит .jpg "
            f"и _prompt.txt сразу в refs/objects/ (раньше всё шло в "
            f"refs/locations/, приходилось вручную перемещать). `--force` "
            f"НЕ передаём: Studio гарантирует уникальность slug перед "
            f"запуском (если slug совпал — переименовала в `<name>_2` "
            f"и т.п.). pipeline.py остаётся в safety-режиме «refuse on "
            f"collision».\n"
            f"Картинка ляжет в shows/{show_str}/refs/objects/{name}.jpg.\n"
            f"\n"
            f"🔴 КРИТИЧНО: ЗАПРЕЩЕНО использовать WebSearch / WebFetch /\n"
            f"любые web-tools для объектов. Описание из чата уже содержит\n"
            f"все нужные детали, генеративный AI прекрасно знает как\n"
            f"выглядят бытовые предметы. Веб-поиск тратит время и токены\n"
            f"впустую. Сразу пиши промпт по описанию и запускай pipeline.py.\n"
            f"Геометрия НЕ нужна — объект не локация.\n"
        )
        target_path_hint = f"shows/{show_str}/refs/objects/{name}.jpg"
    elif gen_type == "character":
        return (
            "Ты автономный фоновой агент Storyboard Studio.\n"
            f"Получил тип `character` для «{name}» — это НЕПРАВИЛЬНО.\n"
            "Рефы персонажей создаются НЕ через эту кнопку, а через\n"
            "вкладку «Актёры» (попап «Создать референс» с фото актёра).\n"
            "Заверши работу строкой:\n"
            f"`✗ error: characters не поддерживаются в автономной генерации, "
            f"используй вкладку Актёры`"
        )
    else:
        return (
            f"Ты автономный фоновой агент Storyboard Studio.\n"
            f"Неизвестный тип `{gen_type}` для «{name}».\n"
            f"Заверши работу строкой:\n"
            f"`✗ error: unknown gen type \"{gen_type}\"`"
        )

    return (
        f"Ты автономный фоновой агент Storyboard Studio. Тебе дана ОДНА "
        f"задача: сгенерировать {gen_type} «{name}» для эпизода {ep_str} "
        f"сериала {show_str}.\n\n"
        f"Описание (от ассистента в основном чате):\n{description}\n\n"
        f"{pipeline_steps}\n"
        f"ОБЩИЕ ПРАВИЛА:\n"
        f"• Без диалога с юзером. Никаких «Жду команды», «Поехали?», "
        f"«Подтверди» — просто работай.\n"
        f"• Печатай ТОЛЬКО короткие prefix-строки прогресса (одна "
        f"строка на действие, начинай с эмодзи 🌐/✏/🎨/📝).\n"
        f"• ЗАПРЕЩЕНО печатать длинные пояснения, развёрнутые анализы, "
        f"таблицы, code-блоки. Только статус.\n"
        f"• Если получилось — последняя строка ровно: "
        f"`✓ done {name}.jpg` (без кавычек).\n"
        f"• Если ошибка — последняя строка: `✗ error: <одна фраза>`.\n"
        f"• Целевой путь: {target_path_hint}\n"
    )


class AutonomousGenThread(QThread):
    """Headless subprocess `claude -p` для одной автономной генерации.

    Сигналы:
      • progress(str) — короткая строка статуса (то что AI печатает)
      • finished_ok(str) — путь к сгенерированной картинке (если AI сообщил)
      • error(str) — сообщение об ошибке от AI или CLI
      • stopped() — после явной остановки через stop()

    Параметры:
      • project_root — рабочая директория (где CLAUDE.md)
      • gen_type — 'location' / 'object' / 'character'
      • name — slug (prison_phone_hallway)
      • description — текст для контекста промпта (что это за место/объект)
      • ep_id — `epXX` для трекинга в логе AI (опционально)
      • show_slug — slug сериала (опционально, AI берёт из current_show.json)
      • model — model-id для `--model` (None → default CLI)
    """
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    error = pyqtSignal(str)
    stopped = pyqtSignal()
    # Phase 2 hotfix #18: эмитится для location-генерации в момент когда
    # AI начал шаг «📝 геометрия» — это значит pipeline.py уже завершил
    # шаг 3 и файл картинки лежит в refs/locations/. Между image_ready
    # и finished_ok ~15-30с (Read tool + написание geometry.txt). UI
    # использует это чтобы показать «✓ картинка готова, описываю
    # геометрию...» вместо «🎨 генерируется...» — синхронно с появлением
    # картинки в РЕФЕРЕНСАХ.
    image_ready = pyqtSignal()

    def __init__(self, project_root: Path, gen_type: str, name: str,
                 description: str, ep_id: Optional[str] = None,
                 show_slug: Optional[str] = None,
                 model: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.gen_type = gen_type
        self.name = name
        self.description = description
        self.ep_id = ep_id
        self.show_slug = show_slug
        self.model = model
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self):
        cli = _sa.find_claude_cli()
        if not cli:
            self.error.emit("claude_cli_not_found")
            return
        try:
            prompt = build_autonomous_prompt(
                self.gen_type, self.name, self.description,
                ep_id=self.ep_id, show_slug=self.show_slug)
            args = [cli]
            if self.model:
                args += ["--model", self.model]
            args += ["-p", prompt, "--dangerously-skip-permissions"]
            # 2026-05-08: CREATE_NO_WINDOW guard для Win10/11 — без него
            # на каждый клик «Сгенерировать» у коллег открывается чёрное
            # cmd-окно поверх Studio (см. _WINDOWS_PREP_TODO.md P0).
            popen_kwargs = dict(
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",      # 2026-05-09 Win-fix: иначе на Win
                errors="replace",      # cp1252 default → crash на 0x98.
                bufsize=1,
            )
            if sys.platform == 'win32':
                popen_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(args, **popen_kwargs)
            assert self._proc.stdout is not None
            last_line: str = ""
            done_path: Optional[str] = None
            err_msg: Optional[str] = None
            image_ready_emitted = False
            for raw in self._proc.stdout:
                if self._stop_requested:
                    break
                line = (raw or "").rstrip()
                if not line:
                    continue
                # Игнорируем JSON/code-блоки которые AI мог нечаянно
                # выплюнуть — оставляем только понятные статус-строки.
                if line.startswith(('🌐', '✏', '🎨', '📝', '✓', '✗', '⚠', '▶')):
                    self.progress.emit(line)
                    last_line = line
                    # Phase 2 hotfix #18: «📝 …» = шаг геометрии для
                    # location → значит файл картинки уже на диске.
                    # Сигналим один раз, чтобы UI обновился.
                    if (not image_ready_emitted
                            and self.gen_type == 'location'
                            and line.startswith('📝')):
                        image_ready_emitted = True
                        self.image_ready.emit()
                    if line.startswith('✓ done'):
                        # Финальная фраза — извлечём имя файла если есть.
                        rest = line[len('✓ done'):].strip()
                        done_path = rest or self.name
                    elif line.startswith('✗ error'):
                        err_msg = line[len('✗ error:'):].strip() \
                            if ':' in line else line
                else:
                    # Прочая строка — тоже шлём, но не считаем за done/error
                    self.progress.emit(line)
                    last_line = line
            rc = self._proc.wait(timeout=10)
            if self._stop_requested:
                self.stopped.emit()
                return
            if err_msg:
                self.error.emit(err_msg or last_line or "unknown error")
                return
            if rc != 0 and not done_path:
                self.error.emit(f"exit {rc}: {last_line[:200]}")
                return
            self.finished_ok.emit(done_path or self.name)
        except Exception as e:
            if self._stop_requested:
                self.stopped.emit()
                return
            self.error.emit(str(e)[:500])
