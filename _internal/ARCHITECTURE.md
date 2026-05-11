# ARCHITECTURE — Storyboard Studio

**Последнее обновление:** 2026-05-11

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

## Animal classification (BUG 2 fix)

**С 2026-05-10 (commit 523fb17):** «🎨 Сгенерировать» на character-маркере
из манифеста не запускает CharacterOutfitPicker если по эвристике это
ЖИВОТНОЕ — Studio переклассифицирует в object-flow.

**Двухуровневая защита:**

**1. Agent-side (primary):**
[PRODUCER_INSTRUCTIONS.md](instructions/PRODUCER_INSTRUCTIONS.md)
секция «🔴 ПРАВИЛО: ЖИВОТНЫЕ И НЕ-ЛЮДИ — В СЕКЦИЮ ОБЪЕКТЫ» +
аналогичный блок в `views/new_episode.py:_on_run` промпте говорят
агенту класть бульдога/кота/лошадь в OBJECTS секцию с
`[[GEN:object:slug:описание]]` маркером, не в CHARACTERS.

**2. Studio-side (safety net):**
[views/episode_chat.py](views/episode_chat.py):
- Module-level `_ANIMAL_KEYWORDS` константа: 35 ключевых слов
  (RU + UA + EN). Substring match, case-insensitive.
- `_on_gen_button_clicked` для `gen_type == 'character'`:
  - Если `_is_likely_animal(name, description)` → True →
    redirect: emit system-сообщение «похоже на животное», set
    `gen_type = 'object'`, fall-through к стандартному
    AutonomousGenThread launch path.
  - Иначе (real human) → `_start_outfit_picker(name)` как раньше.
- `_is_likely_animal(name, description)` проверяет blob `name + " "
  + description` (lowercased) против keywords. `description or ''`
  защищает от None.

Fall-through для редиректа использует существующую slug-collision
detection (фикс БАГ 1) для `refs/objects/`. Никакого дублирования.

**Object-промпт для животных:** [autonomous_gen.py](threads/autonomous_gen.py)
имеет hardcoded «product shot, white background, no people» для
объектов. Для животных это не идеально, но агент пишет промпт сам
на основе `description` поля и может override'ить. PRODUCER_INSTRUCTIONS
подсказывает писать «в естественной среде, photorealistic, без
cinematic 16:9». Если в реальном использовании окажется что бульдоги
получаются на белом фоне — смягчить хардкод отдельным фиксом.

## Refs auto-link reliability (БАГ 10 fix, 3 слоя защиты)

**С 2026-05-10/11 (commits ef78d7b + 87091f0):** auto-link decision
после автономной генерации защищён тремя независимыми слоями.

**Контекст проблемы:** при параллельной генерации нескольких объектов
(shotgun + phone одновременно) `_save_active_gen_decision` мог записать
filename с НЕВЕРНЫМ расширением (`shotgun.png` когда на диске `.jpg`)
из-за слепого доверия hint'у агента + race на read-modify-write
episodes.json. Раньше → файл существовал на диске, но `list_episode_refs`
не находил по неверному filename → refs panel пуст, CTA «Make
storyboards» false-positive активна.

### Layer 1 — `_save_active_gen_decision` (write-side)
[storyboard_app.py:4180+](storyboard_app.py:4180):
- **Disk-validation filename** перед записью: если файла нет под
  переданным именем — глоб `{name}.{jpg,jpeg,png,webp}` находит
  актуальное расширение.
- **`threading.Lock`** (`self._episodes_json_lock`) вокруг
  read-modify-write — защита от Python-side race при параллельных
  `_on_active_gen_finished` callbacks.
- **Atomic write** через temp-file `{name}.json.tmp.<PID>` +
  `os.replace()`. POSIX-atomic rename. PID-суффикс защищает от
  collision если юзер запустил две Studio параллельно.

### Layer 2 — `list_episode_refs` (read-side self-healing)
[storyboard_app.py:1525+](storyboard_app.py:1525):
- Nested helper `_find_on_disk(base_dir, slug, hint_filename)`:
  1) сначала hint (если файл там есть);
  2) fallback на glob `{slug}.{jpg,jpeg,png,webp}`.
- Loc/obj entries из decisions проходят через helper. В результате
  `filename` = `cand.name` (актуальное имя на диске), не stale `fn`
  из decisions. Render всегда показывает реальный файл.
- Character (`folder/file.jpg` filename pattern) — не glob'ится этой
  логикой (другая схема, требует отдельного фикса).

### Layer 3 — `_linked_file_exists` (CTA readiness check)
[views/episode_chat.py:2521+](views/episode_chat.py:2521):
- Mirror логики Layer 2 для `_check_montage_ready`: если hint
  filename не существует, тот же disk-glob fallback.
- Защита от несовместимости Layer 2 (refs panel показывает) vs
  строгая проверка (CTA скрыта). Раньше БАГ 11 — refs panel
  работал но CTA пряталась.

**Cross-process защита:** atomic rename — гарантия от частичных
записей если процесс убьют. Agent в `claude -p` (внешний subprocess)
может всё равно overwrite episodes.json — для этого слой 2+3
независимо self-heal'ятся на read. Также агентский promпт ([views/
new_episode.py:1020+](views/new_episode.py:1020)) явно запрещает
трогать `refs_decisions`.

## Cultural context decomposition (БАГ 9 v2)

**С 2026-05-11 (commit 87091f0):** при формировании English-промптов
для Gemini cultural context разделён на **географию** и **уровень
достатка** — раздельные оси.

**Контекст проблемы:** Gemini генерила «обшарпанный советский
дачный экстерьер» вместо luxury загородного дома хотя agent в
manifest писал «luxury / богатый». Причина — token leakage в
инструкциях («Russian / Soviet / dacha» в плохих примерах) + путаница
agent'а с interface language vs cultural context.

### Декомпозиция

- **География:** дефолт — generic Western/European contemporary
  (БЕЗ luxury по умолчанию). НЕ применять национально-окрашенные
  стили (русский / советский / dacha / постсоветский / японский /
  арабский) если bible этого ЯВНО не указал.

- **Уровень достатка:** определяется ОТДЕЛЬНО — из описания
  конкретной локации в сценарии. Сериал может содержать локации
  РАЗНЫХ уровней одновременно. НЕ применять `luxury` ко ВСЕМ
  локациям подряд. Маппинг:
  - «дешёвый мотель Sunset» → `cheap motel run-down`
  - «элитный ресторан» → `upscale fine dining`
  - «квартира адвоката» → `professional middle-class apartment`
  - «стройка» → `working construction site`
  - «трущобы / гетто» → `grungy / impoverished`
  - «обычная квартира семьи» → `middle-class apartment`

### Места правил

- [views/new_episode.py:1183+](views/new_episode.py:1183) — блок
  «🔴 INTERFACE LANGUAGE ≠ CULTURAL CONTEXT» в промпте.
- [threads/autonomous_gen.py:84+](threads/autonomous_gen.py:84) —
  Geographic+economic rules в location/object pipelines.
- [instructions/PRODUCER_INSTRUCTIONS.md:213+](instructions/PRODUCER_INSTRUCTIONS.md:213)
  — зеркальные правила в producer-инструкциях.

### Token leakage очищен

Слова «Russian / dacha / Soviet / советский» убраны из плохих
примеров в `threads/autonomous_gen.py` (заменены на нейтральное
«outdated / cheap-looking / low-budget rural»). Агент больше не
видит эти токены как «valid в данном контексте».

## Thinking dots animation — hand-off & multi-ep registry

**С 2026-05-11:** анимация бегущих точек `▶ Думаю ····` в чатах эпизодов
живёт в [views/episode_chat.py](views/episode_chat.py) — **один**
`EpisodeChatView` на MainWindow (см. [storyboard_app.py:5275](storyboard_app.py:5275)).
Виджет переключается между эпизодами через `set_episode(ep_id)`,
перечитывая историю из `chats/<ep_id>.jsonl`. Маркер `▶ Думаю` пишется в
jsonl при старте треда; тикер `_thinking_timer` (один QTimer, 400ms)
дописывает к маркеру в текущем `log_view` хвост ` · / ·· / ··· / ····`.
Иллюзия «анимация во всех эпизодах одновременно» — на самом деле работает
один тикер по текущему виду; в чужих чатах в jsonl лежит лишь голый
маркер, а точки появляются при `set_episode` (тикер дорисовывает на лету).

**Реестр живых тредов (multi-ep fix):**
`self._external_threads: dict[ep_id, RunEpisodeThread]` — заменил
single-slot `_external_thread`. До фикса любой завершившийся внешний
тред глушил `_thinking_timer` глобально, потому что:
1. `_external_thread` — один слот, перезаписывался при каждом
   `begin_external_thinking`; но `thread.finished.connect(_end_external_thinking)`
   от каждого предыдущего треда оставался подключённым.
2. `_end_external_thinking` делал безусловный `_thinking_timer.stop()` +
   `_finalize_thinking_dots()` — это сносило анимацию параллельных
   генераций в других эпизодах, отображаемых через тот же view.

Теперь:
- `begin_external_thinking(thread, ep_id)` регистрирует тред под ключом
  `ep_id` и подключает `thread.finished` через **lambda, замыкающую
  `ep_id` + `thread`** → `_end_external_thinking(ep_id, thread)` снимает
  только этот entry, остальные эпизоды не страдают.
- `_has_live_thread_for(ep_id)` — единый предикат: есть ли у этого ep_id
  живой тред (in-chat followup `self._thread` если `ep_id == self._ep_id`,
  или external из реестра).
- `_tick_thinking` опирается на `_has_live_thread_for(self._ep_id)` —
  завершение треда другого эпизода никак не влияет.
- `_maybe_stop_thinking()` — общий хелпер: стопит таймер и финализирует
  точки только если у текущего ep_id больше нет живых тредов. Используется
  в `_on_done` / `_on_error` / `_on_stopped` вместо unconditional stop.
- `_refresh_thinking_for_current_ep()` — зовётся из `set_episode` после
  перерисовки истории: если у нового ep_id живой тред есть → стартует
  тикер (история уже содержит маркер `▶ Думаю`); иначе стопит.

**In-chat followup vs external:** `self._thread` (single) — это followup
из `_on_send` (юзер набрал сообщение прямо в чате эпизода). Он по
построению относится к `self._ep_id` на момент старта. Реестр
`_external_threads` — отдельный для тредов из `NewEpisodeView` после
hand-off (`begin_external_thinking`). Оба учитываются в
`_has_live_thread_for`.

**Caller:** [views/new_episode.py:1311](views/new_episode.py:1311) после
`_switch_to_episode_chat` зовёт `ev.begin_external_thinking(self._thread,
ep_id=self._current_ep_id)` — ep_id передаётся явно (раньше fallback на
`ev._ep_id`).

## Slug collision handling (refs)

**С 2026-05-10 (commit b8b07ec):** «🎨 Сгенерировать» = всегда создаёт
новый ref, никогда не перезаписывает существующий. Если slug уже занят
в `refs/<sub>/` (location или object) — Studio автоматически переименует
текущий slug в `<name>_2` / `<name>_3` / ... ДО запуска генерации.

**Как работает:**

`EpisodeChatView._on_gen_button_clicked`
([views/episode_chat.py:1256](views/episode_chat.py:1256)) для
`gen_type in ('location', 'object')` вызывает
`_resolve_collision_free_slug(cur_show, gen_type, name)` ПЕРЕД
созданием `AutonomousGenThread`. Если `refs/<sub>/<name>.*` пуст —
имя возвращается как есть. Если коллизия — Studio:

1. `glob` ищет первый свободный суффикс (`<name>_2`, `<name>_3`, ...).
2. Обновляет `episodes.json[ep_id].refs.<sub>`: basename-match через
   `rsplit(".", 1)`. Заменяет `<name>.<ext>` на `<new_name>.<ext>`,
   сохраняя расширение. **basename match, не префикс** — иначе
   `name="hall"` ошибочно зацепил бы `item="hallway.jpg"`.
3. Эмитит system-сообщение в чат: «ℹ Слаг X уже занят, переименовал
   в Y. Если хотел переиспользовать — жми «📁 Выбрать существующий».

`AutonomousGenThread` получает уже-уникальный slug. В промпт
агенту ([threads/autonomous_gen.py](threads/autonomous_gen.py))
команда `pipeline.py generate` пишется БЕЗ флага `--force` —
pipeline.py остаётся в default «refuse on collision» режиме как
safety net. Если уникализация на Studio-стороне когда-то даст сбой
(race condition, missed glob), pipeline.py отрефьюзит запись и юзер
увидит ошибку — лучше чем silent overwrite.

**Что НЕ так в архитектуре, осталось на потом:**
- Refs остаются global per show (одна папка `refs/locations/`).
  Episode-association живёт только в `episodes.json[ep].refs.<sub>`.
- Если юзер хочет ВРУЧНУЮ переименовать ref после генерации —
  поддержки нет, нужно править файлы и `episodes.json` руками.
- Если юзер хочет regenerate существующий slug (replace), кнопка
  «🎨 Сгенерировать» больше не появляется при `✓` маркере. Нужно
  удалить файл из `refs/<sub>/` руками, тогда агент при следующем
  Run эпизода сделает `✗` маркер и покажет кнопку.

## Single source of truth: `scenarios/ep{NN:02d}.txt`

**С 2026-05-10 (commit f07b2d9):** для каждого эпизода единственный
авторитетный файл сценария — `shows/<slug>/scenarios/ep{NN:02d}.txt`
(zero-pad до 2: ep01, ep02 ... ep99; ep100+ без pad).

**Кто пишет:**
- `_on_scenario_file` ([storyboard_app.py:5653](storyboard_app.py:5653))
  при drop файла — через `scenario_parser.save_parsed_doc` (парсит
  «ЭПИЗОД N» маркеры, режет на epNN.txt).
- `_on_run` ([views/new_episode.py:795-805](views/new_episode.py:795)) —
  при клике Run в форме «+». Пишет `active_text` (sliced секцию)
  напрямую в `ep{NN:02d}.txt`. Раньше писал в `_active.txt`.

**Кто читает:**
- Правая панель `_show_scenario_popup`
  ([storyboard_app.py:6562](storyboard_app.py:6562)) — кандидаты:
  `ep_id.txt`, `ep_id.lstrip('ep').txt`, `ep{NN:02d}.txt`.
- Промпт нового эпизода
  ([views/new_episode.py:891-901](views/new_episode.py:891)) —
  hardcode `Read shows/<slug>/scenarios/ep{NN:02d}.txt`.
- `_load_scenario_text` для монтажки/CTA
  ([views/episode_chat.py:2330](views/episode_chat.py:2330)) — кандидаты:
  zero-pad PRIMARY, потом `ep_id.txt`, `ep_id.lstrip('ep').txt`.
- `SuggestOutfitsThread._load_context`
  ([threads/suggest_outfits.py:228-238](threads/suggest_outfits.py:228))
  — zero-pad PRIMARY, fallback на `ep_id.txt` без zero-pad.

**Что больше НЕ используется:**
- `scenarios/_active.txt` — legacy, разъезжался с UI-эпизодом (был
  глобальный mutable, обновлялся только в `_on_run`, не сбрасывался при
  `_select_episode`). Все code paths убраны 2026-05-10. На дисках у
  коллег файл может остаться мусором — безвреден, никто не читает.
- `scenarios/_inbox.txt` — пишется в `_on_run` как «черновик ввода»
  (raw paste до парсинга). Никто не читает; оставлен на месте, может
  пригодиться в будущем для restore последнего ввода в форму «+».

**`active.txt` (без подчёркивания) — отдельная сущность:**
`_history/<basename>/active.txt` ([storyboard_app.py:2851](storyboard_app.py:2851))
— per-shot variant pointer (какая версия шота сейчас в работе). К
сценариям отношения не имеет.

**`episodes.json[ep].refs` — пишет Claude через Bash tool**
(см. [PRODUCER_INSTRUCTIONS.md](instructions/PRODUCER_INSTRUCTIONS.md)
ШАГ 1). Содержимое refs зависит от того что Claude прочитает по
scenario-пути. После переключения чтения на zero-pad (2026-05-10) refs
самовыправляются на следующем Run каждого эпизода — миграция в
Python-коде не нужна.

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

### Долг 6 (PRIORITY 1) — manifest format degradation (БАГ 13)

На длинных задачах Opus 4.7 иногда сокращает manifest от секционного
формата (`ЛОКАЦИИ: / ОБЪЕКТЫ: / ПЕРСОНАЖИ:` + `- ✗ name (...)`) до
prose summary:

> «Manifest ep3 записан: bedroom + house_lawn в локациях,
> double_barrel_shotgun + phone + buldog в объектах. Не хватает
> 3 рефов — house_lawn, phone, buldog. Жми кнопки 🎨...»

Парсер `_FALLBACK_LINE_RE` + `_INLINE_GEN_HEADER_RE` в
[views/_chat_render.py](views/_chat_render.py) не распознаёт prose
формат → markers = пусто → кнопки не появляются.

**История:** Pre-existing. Два расширения парсера уже было:
- `5df52f9` (2026-05-10) — формат «- ✗ НУЖНО СГЕНЕРИРОВАТЬ: ...»
- `526b5c0` (2026-05-11, БАГ 12) — nested parens в descriptions.
Это **третий формат** — кошки-мышки.

**Архитектурно правильное решение:** Studio должна использовать
**`episodes.json` как source of truth** для markers, а chat-парсер
делать косметическим. Pipeline:
1. Agent пишет manifest в `episodes.json[ep].refs.{locations,objects}`
   (как и сейчас) — это data layer.
2. Studio при `_check_montage_ready` / `_advance_gen_queue`:
   читает `refs.locations/objects` из `episodes.json` + проверяет
   соответствующие файлы на диске → формирует список (slug, status).
3. UI кнопки строит из этого списка, не из чата.
4. Chat-парсер остаётся только для description/display name (UX
   feedback), но кнопки появляются гарантированно если manifest
   в `episodes.json` валидный.

**Объём:** ~150 строк refactor в `_check_montage_ready`,
`_advance_gen_queue`, `_restore_gen_buttons_from_history`. Также
нужно убедиться что agent ВСЕГДА пишет в episodes.json (это уже
gated в промпте).

**Срочность:** P1 — баг наблюдается у юзера на каждой 3-й параллельной
генерации Opus 4.7. До решения промпт уже содержит запреты на prose
формат, но Opus иногда нарушает при long-running tasks.
