# ARCHITECTURE — Storyboard Studio

**Последнее обновление:** 2026-05-10

Снимок текущего устройства кода. Живой документ — обновляется в том же
коммите что и затрагиваемая правка. Лежит в `_internal/` (не уходит к
коллегам через installer; bundle через PyInstaller тоже не включает).

## Версия и статус
- Текущая: **v1.0.30** (см. `version.json`).
- Релизный канал коллег: GitHub Releases, asset `Storyboard Studio v<ver>-{mac,win}.zip`.
- Как пуляются обновления: админ → «📤 Отправить обновление» → `SendUpdateThread`
  ([threads/update.py:460](threads/update.py:460)) → bump version + git push +
  upload .app/.exe в Release.

## Архитектурные решения которые легко забыть

- **Anthropic API напрямую НЕ вызывается.** `pipeline.py` использует только
  Fast Gen AI (генерация картинок). Описание геометрии локаций делает
  Claude Code сам, глядя на сгенерированную картинку. Класс
  `ClaudeGeometryThread` — legacy имя, не вызывает Anthropic API.
- **Caching работает прозрачно через Claude CLI** (серверный prefix-cache
  включён по умолчанию у Anthropic). **Переход на anthropic Python SDK
  делать НЕ надо** — это per-token billing мимо Max-подписки админа.
- **`PRESERVE_ON_UPDATE` ([storyboard_app.py:134](storyboard_app.py:134))
  используется только в мёртвом `DownloadUpdateThread`** — расширять без
  причины не нужно (см. ниже секцию «Архитектура обновлений»).

## Hardcoded values

### `seedance_model = "claude-opus-4-7"` — [views/episode_chat.py:2836](views/episode_chat.py:2836)
Seedance pipeline всегда вызывает Opus 4.7 независимо от дропдауна модели в шапке.
Причина (см. комментарий выше hardcode): Seedance-промпты большие и сложные,
Sonnet даёт заметно хуже качество. Карту и PromptWriter можно гонять на Sonnet
(быстрее), а Seedance — только Opus.
**Когда снимать:** только если будет внутренний бенч-тест на Sonnet/Opus
по идентичной выборке Seedance-промптов и Sonnet выходит на паритет.

### Default model = `claude-opus-4-7` — [views/episode_chat.py:297](views/episode_chat.py:297)
Сохранённое в `QSettings` по ключу `"new_ep/model_v2"`. Если ключа нет
у юзера — загружается Opus 4.7 как fallback.

## Per-agent model routing

| Агент | Слушает дропдаун? | Где |
|-------|--------------------|-----|
| Montage (монтажная карта) | ДА | [views/episode_chat.py:2492](views/episode_chat.py:2492) `_current_model()` |
| StoryboardWriter | ДА | [views/episode_chat.py:2749](views/episode_chat.py:2749) `_current_model()` |
| Seedance pipeline | НЕТ — hardcode Opus 4.7 | [views/episode_chat.py:2836](views/episode_chat.py:2836) |

Дропдаун в шапке: Sonnet 4.6 / Opus 4.7 / Haiku 4.5
([views/episode_chat.py:289-292](views/episode_chat.py:289)).

## Module boundaries

```
storyboard_app.py    — главный модуль: главное окно, вкладки, баннеры,
                       пути (SHOW_ROOT/PROMPTS_DIR/...), глобальные
                       утилиты (block_wheel_event, cross_fade_swap,
                       no_console_kwargs, find_claude_cli...)
threads/             — QThread worker'ы (10 файлов)
                       update.py: CheckUpdateThread, DownloadAppUpdateThread,
                                  SendUpdateThread, FetchStatsThread.
                                  DownloadUpdateThread — МЁРТВЫЙ КОД (см. ниже).
views/               — главные view'ы (episode_chat, new_episode, actors,
                       theme, _chat_render — общий хелпер рендера чата)
widgets/             — кастомные QWidget (active_gens_panel, gen_button,
                       montage_cta, ref_picker_dialog, shot_viewer_dialog,
                       editor_widgets и т.д.)
agents/              — system prompts как Python-модули (НЕ читают
                       instructions/*.txt в runtime — всё инлайн).
                       montage_prompts, seedance_prompts, storyboard_writer_prompts.
i18n.py              — TRANSLATIONS dict для RU/UK/EN.
scenario_parser.py   — парсинг сценариев.
show_manager.py      — current_show.json, shows/<slug>/ переключатель.
installer_app.py     — отдельная программа Storyboard Installer.app
                       (DownloadProjectThread + DownloadAppExeThread).
```

## Архитектура обновлений (КРИТИЧЕСКИ ВАЖНО)

Два разных пути доставки файлов до коллеги:

### A. Первая установка через Storyboard Installer.app
[installer_app.py:280](installer_app.py:280) `DownloadProjectThread`:
- Качает GitHub zip → жёсткий whitelist:
  - `ALLOW_DIRS = {"actors"}`
  - `ALLOW_FILES = {"version.json", "pipeline.py"}`
  - Создаются пустые `shows/`, `output/`.
- Всё остальное **игнорируется**: `instructions/`, `agents/`, прочие `*.py`, `*.md`, `.spec`, `.github/`, `tests/`.

`pipeline.py` нужен потому что AutonomousGenThread запускает `claude -p`
с `cwd=project_root`, и агент через Bash tool вызывает
`python3 pipeline.py generate <name> "<prompt>"` — файл должен быть
в cwd. Дополнительно Studio при каждом старте делает self-healing
sync через `sync_pipeline_py_to_project` ([storyboard_app.py](storyboard_app.py)) —
overwrite'ит project pipeline.py из bundle если содержимое отличается
(закрывает skew после auto-update'а .exe).

### B. Обновление приложения из работающей Studio
[threads/update.py:200](threads/update.py:200) `DownloadAppUpdateThread`:
- Качает GitHub Release asset (собранный bundle, **не исходники**).
- Bootstrap-скрипт (.bat / .sh) ждёт смерти Studio, **подменяет ВЕСЬ
  bundle**.
- Файлы проекта (`*.md`, `instructions/`, `agents/`, `.py`) при этом
  потоке **не трогаются**.

**Hardened flow (2026-05-09 после Failure mode A на Win,
+ 2026-05-10 retry-loop против Windows Defender lock):**
- Studio пишет ДВА маркера в `project_root` ДО запуска bootstrap'а:
  - `pending_version.txt` = NEW версия → Studio при старте обновит
    `version.json[app_version]` на это.
  - `pending_rollback.txt` = OLD версия → bat при success УДАЛЯЕТ файл.
- Bat использует `move /y` (не `ren` — он тихо проваливается!) +
  errorlevel-check. На fail: запись `update_failed.txt`, старт СТАРОЙ
  Studio, exit 1. На copy fail: rollback `mv .old → target`.
- **2026-05-10**: после `studio died` bat делает
  `taskkill /F /IM "Storyboard Studio.exe" /T` (force-kill всех
  инстансов + children) + `timeout 2`, затем move обёрнут в
  retry-loop: 6 попыток × 2 сек = до 12 сек ожидания. Причина —
  Windows Defender держит handle на убитом .exe секунд 5-10
  для сканирования; одной попытки мало. Каждая попытка пишется в
  bootstrap.log с номером (`move attempt N`, `attempt N failed`,
  `move succeeded on attempt N`). См. `_session_log.md` запись
  «Win auto-update: taskkill + retry-loop».
- Bat пишет полный лог в `update_dir/bootstrap.log` (не stdout/stderr).
  `update_dir` НЕ удаляется bootstrap'ом — Studio при следующем старте
  чистит через `finalize_pending_update`, но защищает папку с активным
  bootstrap.log если найден failed update.
- При старте `finalize_pending_update` ([storyboard_app.py:778](storyboard_app.py:778)):
  - Если `pending_rollback.txt` есть → bat упал → откат `version.json`
    на OLD + popup юзеру (`_show_update_failed_dialog`) с кликабельной
    ссылкой на Installer и bootstrap.log.
  - Если только `pending_version.txt` есть → success → bump version.json.

### МЁРТВЫЙ КОД (важно знать чтобы не путаться)

`DownloadUpdateThread` ([threads/update.py:107](threads/update.py:107)) — старый
механизм «синий баннер Обновление проекта» который копировал ВЕСЬ репо
коллеге. **Не вызывается с 2026-05-08.** Кнопка удалена из layout, вызовы
выпилены ([storyboard_app.py:8865](storyboard_app.py:8865),
[storyboard_app.py:4698](storyboard_app.py:4698)). Класс остался импортом и
в `tests/smoke.py`. Если когда-то нужно восстановить — git history.

`PRESERVE_ON_UPDATE` ([storyboard_app.py:134](storyboard_app.py:134)) —
используется только в `DownloadUpdateThread`, то есть в мёртвом коде.
Расширять его без причины не надо.

### Что зашито в Studio bundle (PyInstaller)
[StoryboardStudio.spec:13](StoryboardStudio.spec:13):
```python
datas=[
    (certifi.where(), 'certifi'),
    ('assets/icons', 'assets/icons'),
    ('pipeline.py', '.'),       # 2026-05-09: для self-healing sync в project_root
],
```
Плюс автоматически — все импортируемые .py модули (включая `agents/*.py`).
**НЕ зашиваются:** `instructions/*.txt`, `*.md`, `_session_log.md`. Если
будущий feature потребует читать `instructions/*` в runtime — нужно
явно добавить в `datas` spec.

`pipeline.py` после распаковки находится через `sys._MEIPASS` (на Mac
.app `Contents/Resources/pipeline.py`, на Win onedir `_internal/pipeline.py`).
Studio при старте копирует его в `project_root` через
`sync_pipeline_py_to_project` ([storyboard_app.py](storyboard_app.py)).

## Cross-platform (Mac / Win 10-11)

Коллеги на Win получают `.exe` (onedir mode с 2026-05-08). Каждое касание
кода должно работать на обеих ОС:
- `subprocess.Popen/run` — обязательно `**_sa.no_console_kwargs()` или
  явно `creationflags=CREATE_NO_WINDOW` на win32 (иначе мигают cmd-окна).
- Никаких shell-only команд (`bash -c`, `&&` через shell=True).
- Пути — только `pathlib.Path`, не f-string'и со слешами.
- Claude CLI: `find_claude_cli()` ищет на обоих ОС, кешируется в
  `_claude_cli_cache`.

Win-onedir, не onefile: PyInstaller onefile + Windows Defender = крэш
(`_MEI…\base_l…` карантин). См. [StoryboardStudio.spec:60-...](StoryboardStudio.spec:60).

## Не-очевидные инварианты в коде

Если видишь незнакомый паттерн — НЕ удаляй, найди запись в `_session_log.md`.

- `block_wheel_event(widget)` ([storyboard_app.py](storyboard_app.py)) —
  блокирует прокрутку колёсиком над QSlider/QComboBox/QSpinBox в Settings,
  чтобы при скролле страницы значения не съезжали. Применяется к КАЖДОМУ
  новому виджету настройки. См. CRITICAL_RULES.md.
- `cross_fade_swap` — анимация смены превью.
- `_active_regens` — словарь активных регенераций шотов.
- `_unseen_shots` — set новых шотов для NEW-бейджей.
- `_dot_step` — счётчик «…» в индикаторах загрузки.
- `extract_shot_prompt`, `shot_path` — вытаскивание промпта/пути по индексу.
- `ClaudeGeometryThread` — описание геометрии локации (этот класс остаётся
  по имени но не вызывает Anthropic API; имя legacy).
- `find_claude_cli`, `_claude_cli_cache` — ресолв пути к Claude CLI.
- `no_console_kwargs()` — кросс-платформенные subprocess kwargs (Win:
  `creationflags=CREATE_NO_WINDOW`; Mac: `{}`).

## Distribution

User — admin, единственный кто пушит. Коллеги получают через:
1. Установка → `Storyboard Installer.app/.exe` → `DownloadProjectThread`
   качает actors/ + version.json, `DownloadAppExeThread` качает Studio.
2. Обновления → внутри Studio → баннер «Обновить приложение» →
   `DownloadAppUpdateThread` подменяет bundle через bootstrap.

`Send Update` (admin-only кнопка) при `upload_app=True`:
1. Очистка `build/` + `bash build.sh` — авто-пересборка .app (с 2026-05-09).
   Если упало → error до bump'а версии (история Releases без дыр).
2. Bump app_version → git commit → git push.
3. Архивация .app → создание Release → upload asset.

При `upload_app=False` шаг 1 пропускается. Build.sh — Mac-only (bash);
Win .exe собирает GitHub Actions из push'а отдельно.

Не-админский UI (Send Update, FetchStats) гейтится в коде — у коллег
этих кнопок нет. См. memory: project_distribution.

## Tech debt / known issues

### Долг 1 — цвета строк планирования в чате
[storyboard_app.py:829](storyboard_app.py:829) `detect_line_kind`. Сейчас
матчит только голые префиксы `✓`/`✗`, не `- ✓` / `- ✗` (с дефисом списка).
Нужно расширить regex на `^\s*[-*]\s*[✓✗▶!]`. Вынести общий рендер чата
в `views/_chat_render.py` (helper уже есть). См. `_session_log.md` Долг 1
для деталей.

### Долг 2 — UI LUMZ-стиль остатки
См. [_UI_TODO.md](_UI_TODO.md) (создан 2026-05-08, отложен). Список
секций которые ещё не доведены до LUMZ-стиля: NewEpisodeView, вкладка
«Актёры», и далее. Берём по порядку когда юзер вернётся к интерфейсу.

### Долг 3 — семантические эмодзи в `montage_status_*`
[i18n.py:412-417](i18n.py:412). Сейчас содержат `🔍`, `✏`, `✓`, `⚠`, `🎯`
прямо в строках — формально это нарушение memory `feedback_icons_lucide`
(весь UI должен быть на Lucide SVG). Эмодзи статичны и контекстны
(статус прогресса), не критично, но при редизайне статус-бара заменить
на текстовые маркеры или вынести как отдельные иконки.

### Долг 4 — параллелизация Storyboard PromptWriter
Обсуждалось: сейчас PromptWriter гоняется блок за блоком последовательно.
Параллелизация по блокам внутри одного эпизода даст ~3× ускорение
(каждый блок — независимый контекст). Отложено до того момента когда
система правил и оптимизации Seedance стабилизируются.

### Долг 5 — призрачные source_btn после повторных Cancel'ов в Pick existing
В `EpisodeChatView._on_outfit_pick_existing` ([views/episode_chat.py](views/episode_chat.py)) — при Rejected (юзер открыл RefPickerDialog
из picker'а bottom row, потом Cancel) cleanup не делается, picker
остаётся живой. **Но** если юзер потом сделает любую navigation +
вернётся → `_restore_gen_buttons_from_history` создаст НОВУЮ
GenButton для david. А старая (скрытая, в `_outfit_source_btns`)
остаётся в `_gen_layout` как призрак. При повторных Cancel-циклах
накапливается несколько hidden GenButton'ов.

**Симптом для юзера:** не виден — призраки скрыты. Технически —
утечка widget'ов (минорная). Чинить когда будет общий рефакторинг
управления `_gen_layout` (шире чем outfit picker).

**Возможный фикс:** при `_restore_gen_buttons_from_history` для
character маркера, у которого уже есть source_btn в
`_outfit_source_btns[ep_id]` — переиспользовать его (re-show + reset)
вместо создания новой. Или явный cleanup призраков на ep switch.
