# CLAUDE CODE INSTRUCTIONS — Storyboard Automation

**Последнее обновление:** 2026-05-09

Точка входа для Claude Code в этом репозитории. Короткая, императивная.
Подробности — в файлах по ссылкам.

---

## ДИСЦИПЛИНА РАБОТЫ

1. **Не отвечай по памяти.** Если запрос про код / hardcode / архитектуру —
   сначала Read / grep, потом ответ. Если про producer-методологию —
   сначала Read `instructions/PRODUCER_INSTRUCTIONS.md`, потом ответ.
2. **Спроси «делаем?» перед первым Edit / Write.** Юзер подтверждает план
   до правки. Если юзер спрашивает «как ты понял задачу?» — сначала
   покажи своё понимание текстом, дождись подтверждения, и только потом
   начинай работу.
3. **После каждой правки кода** — verify (`ast.parse`, smoke), rebuild
   (`./build.sh`) если нужно, авто-launch `.app`, короткий «что проверить»
   юзеру.
4. **Anthropic API напрямую НЕ вызывается.** Описание геометрии и промпты
   ты делаешь сам глядя на картинку в чате. См. `_internal/ARCHITECTURE.md`
   секцию «Архитектурные решения которые легко забыть».
5. **Cross-platform.** При правке `subprocess.Popen` / `run` — обязательно
   `**no_console_kwargs()` или явно `creationflags=CREATE_NO_WINDOW` на
   win32. Никаких `shell=True` со встроенными командами. Пути — `pathlib.Path`,
   не f-string'и со слешами. Подробности в `_internal/ARCHITECTURE.md`
   секция «Cross-platform».
6. **Worktree-изоляция.** Если сессия запущена в worktree
   (`.claude/worktrees/<name>/`) — все Write / Edit / git-команды только
   через путь worktree. Никаких абсолютных путей в main repo. Перед каждой
   правкой — мысленная проверка: путь содержит `worktrees/`? Да → ок.
   Нет → стоп, поправить путь. Финальный merge в main делается явной
   командой `git merge --ff-only` из main repo, а не правкой main напрямую.

---

## ДВА РЕЖИМА — ОПРЕДЕЛИ ПЕРЕД ОТВЕТОМ

### Режим CODE — правишь .py / GUI / сборку / обновления / settings / hooks

Перед правкой:
- Read `_internal/ARCHITECTURE.md` — текущее устройство кода, hardcode, долги.
- Глянь последние записи в `_session_log.md` — что трогали недавно.
- Hard rules: `CRITICAL_RULES.md` (wheelEvent блокировка, слово «Claude» в UI).

После правки:
- Запись в `_session_log.md` (формат: дата, файлы/функции, что НЕ трогал, верификация).
- Если меняется hardcode/архитектура — апдейт `_internal/ARCHITECTURE.md`
  в том же коммите.

### Режим PRODUCER — генерация эпизода / сценарий / локация / сториборд

Source of truth: `instructions/PRODUCER_INSTRUCTIONS.md`. Читать целиком
перед любым шагом producer-pipeline.

Hard rules для сториборда — там же (взгляд персонажей, микромимика, теги,
шапка промпта, нумерация шотов и т.д.).

Методология: `instructions/*.txt` (PIPELINE_RULES, голосовые профили,
антитеатральный словарь, библия сериала).

---

## ТРИГГЕРЫ РЕЖИМОВ

CODE триггеры:
- Правка `.py`, баг, сборка `.app`, `installer_app.py`, hooks,
  `settings.json`, GUI поведение, обновления (CheckUpdate / SendUpdate /
  DownloadAppUpdate).

PRODUCER триггеры:
- «работаем над эпизодом», «новый сериал», сценарий, реф, локация,
  объект, сториборд, монтажная карта, обновить geometry.

При ЛЮБОМ пересечении — **CODE побеждает**. Если задача затрагивает
.py код — это всегда CODE-режим, даже если рядом упомянут эпизод.

---

## ССЫЛКИ

- `_internal/ARCHITECTURE.md` — карта кода, hardcode, долги, архитектура обновлений.
- `_internal/BOOTSTRAP.md` — короткие императивы (читается через хук на каждый prompt).
- `CRITICAL_RULES.md` — нерушимые правила разработки кода.
- `instructions/PRODUCER_INSTRUCTIONS.md` — producer-методология.
- `_session_log.md` — хронологический лог Claude-правок (последние 30 дней).
