# ARCHITECTURE — Storyboard Studio

**Последнее обновление:** 2026-05-13 (v1.0.66 — ГЛАВНАЯ_ИНСТРУКЦИЯ.md в bundle + lazy load по разделам)

Снимок текущего устройства кода. Живой документ — обновляется в том же
коммите что и затрагиваемая правка. Лежит в `_internal/` (не уходит к
коллегам через installer; bundle через PyInstaller тоже не включает).

## Версия и статус
- Текущая: **v1.0.50** (см. `version.json`).
- Релизный канал коллег: GitHub Releases, asset `Storyboard Studio v<ver>-{mac,win}.zip`.
- Как пуляются обновления: админ → «📤 Отправить обновление» → `SendUpdateThread`
  ([threads/update.py:460](threads/update.py:460)) → bump version + git push +
  upload .app/.exe в Release.
- **История Win auto-update фиксов:**
  - **v1.0.39 → v1.0.40 апдейт упал** — chicken-and-egg in-process updater'а:
    bat генерится РАБОТАЮЩЕЙ Studio (v1.0.39 без фикса) → старый шаблон
    с `timeout` → instant exit под `stdin=DEVNULL`.
  - **v1.0.40 → v1.0.41 апдейт упал** — bat был НОВЫЙ (v1.0.40 уже с
    ping-фиксом), timestamps в bootstrap.log показали реальные секундные
    gap'ы (фикс sleep-idiom работает!), но все 15 попыток × 2 сек = 30
    сек не хватило — Defender + Yandex Protect стэком держали handle > 30с.
  - **v1.0.41 → v1.0.42 апдейт упал** — снова chicken-and-egg: v1.0.41
    в bundle ещё не имеет warmup + 30 retries, генерит свой старый bat
    (15 retries, без warmup). v1.0.42 установлен на Windows вручную через
    Installer.exe в обход in-process updater'а.
  - **v1.0.42 → v1.0.43 апдейт упал** — новый bat сработал (warmup
    лог, 30 retries × 2с = 64с реального ожидания, AV snapshot работает),
    но Defender (PID 5208, MsMpEng.exe) держал handle ВСЕ 64 секунды
    беспрерывно. Yandex Protect ни разу не появился в AV-snapshot'ах —
    единственный виновник Defender. «Просто увеличить окно — лотерея».
  - **v1.0.44 — RM API + reboot-deferred install fallback**: после
    исчерпания retry-loop bat зовёт `update_helper.ps1 -Mode Defer`,
    который через Restart Manager API логгирует точных holder'ов и через
    `MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT)` планирует подмену bundle
    на следующий рестарт Windows. Studio показывает inline-баннер «нужна
    перезагрузка» вместо popup ошибки. См. секцию «Failure mode B».

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

## Description channels for autonomous gen (v1.0.49, расширено v1.0.50)

При парсинге `✗`/`✓`-строк в чате эпизода используется приоритет каналов:

1. `[[GEN:type:name:description]]` маркер — primary, через
   `parse_gen_markers` ([views/_chat_render.py:41](views/_chat_render.py:41)).
2. Скобки `(описание)` после slug — fallback для location/object/character,
   через `_FALLBACK_LINE_RE` (✗-ветка, [views/_chat_render.py:280](views/_chat_render.py:280))
   и через `m_full`-парсер (✓-ветка, [views/_chat_render.py:355](views/_chat_render.py:355)).
3. Хвост после `— ` — последний fallback. Обычно служебная фраза
   («нужен реф» / «рефа нет» / «реф есть»), без полезной информации.

**v1.0.49** закрыл канал #2 для ✗-ветки: до фикса content скобок
выбрасывался → gen-agent получал «нужен реф» вместо реального описания →
generic-default картинка (helmet → tactical combat вместо construction
hard hat).

**v1.0.50** закрыл канал #2 для ✓-ветки (симметрично). Это safety-net
для случая когда AI ослушался all-✗ инструкции и поставил ✓ —
description всё равно подбирается корректно. Кроме того, regex
`_FALLBACK_LINE_RE` сделан с опциональной группой `desc` — поддерживает
новый формат «`- ✗ name (описание) [[GEN:...]]`» без хвоста `— `.

Defensive instructions в [threads/autonomous_gen.py:78-104](threads/autonomous_gen.py:78)
(location SOCIAL/GENRE) и [autonomous_gen.py:124-160](threads/autonomous_gen.py:124)
(object) остаются safety-net'ом поверх — переводят luxury/expensive/etc.
в English-промпт. Они не конфликтуют с fallback-каналом, а дополняют его.

## All-✗ default for references (v1.0.50)

**Архитектурный принцип:** Программа НИКОГДА не помечает референсы как
`✓` автоматически на основе наличия файла на диске. ВСЕ референсы
текущего эпизода (location + object + character) AI помечает как `✗`.
Решение «🎨 сгенерировать новый» vs «📁 выбрать существующий из
библиотеки» vs «🚫 не нужен» принимает только юзер на каждой карточке.

**Логика:** одинаковые названия не значат одинаковые объекты. «Лесная
тропинка» в ep1 и «лесная тропинка» в ep5 — две разные тропинки. Даже
одна и та же локация может потребоваться в другом ракурсе / свете /
времени суток. `refs/<type>/` — это **библиотека** существующих рефов
сериала, не «indicator нужен или не нужен».

**До v1.0.50:** analyst-агент инструктировался в
[PRODUCER_INSTRUCTIONS.md:137-141](instructions/PRODUCER_INSTRUCTIONS.md:137)
проверять `refs/<type>/` и ставить ✓ если файл есть. Это вело к
дублям (3 helmet'а в ep2 — БАГ 14) и кривым промптам (✓-ветка
парсера выбрасывала description из скобок — БАГ 13).

**После v1.0.50:** инструкция переписана в
[PRODUCER_INSTRUCTIONS.md:132+](instructions/PRODUCER_INSTRUCTIONS.md:132)
и [views/new_episode.py:902+](views/new_episode.py:902) — analyst всегда
ставит `✗` с маркером `[[GEN:type:name:description]]`, не проверяет
файлы. Character тоже `✗` (раньше character с папкой в refs/ → ✓;
теперь character всегда `✗`, при клике 🎨 откроется outfit picker
для ПЕРВОЙ сцены этого персонажа в эпизоде, [threads/suggest_outfits.py:131+](threads/suggest_outfits.py:131)).

**Collision-rename** ([views/episode_chat.py:1561+](views/episode_chat.py:1561))
переформулировано из «уже занят — переименовал» (звучит как conflict)
в «🆕 Создаю новый вариант» (нейтральная формулировка) — юзер видит
ДО старта генерации что Studio делает второй вариант рядом с
существующим, не перезаписывает.

## Diagnostic logging (v1.0.50)

Helper [`_diag_log_append`](views/episode_chat.py:760+) в `EpisodeChatView`
пишет диагностические строки в `shows/<active>/_studio_diag.log`
(append-only, переживает рестарт Studio). Fallback на stderr если
active show нет.

Зачем нужен файл: `.app` запускается кликом по иконке — `sys.stderr`
уходит в /dev/null. Существующие диагностические логи через
`sys.stderr.write` (`[init]`, `[heal]`, `[set_episode]`,
`[collision-resolve]`, `[marker-alias]`) для юзера невидимы.

В v1.0.50 helper используется в `_check_montage_ready` — при изменении
состояния CTA (`ready` / `hidden_unresolved` / `hidden_no_linked` /
`hidden_no_scenario`) пишет одну строку с количеством маркеров и
списком нерешённых. Логирование at-state-change (не каждые 2с) —
файл не разрастается.

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

## Bundled instructions (v1.0.66)

Studio загружает `instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md` из bundle в runtime
как источник правды для system prompt'ов агентов монтажной карты
(Scriptwriter / Validator / Editor / Context Reviewer). Это синхронизирует
Studio с Алексовым claude.ai-флоу — продакшен-методология LUMZ.AI лежит
в одном .md файле, никакой ручной компиляции hard-coded prompts.

**Зачем:** до v1.0.66 каждый агент получал hard-coded SCRIPTWRITER_SYSTEM
(и т.д.) в `agents/montage_prompts.py`. Эти prompts были компиляцией
правил «вручную», и со временем расходились с ГЛАВНАЯ_ИНСТРУКЦИЯ.md.
Симптом — на ep2 «Финальный расчёт» Scriptwriter выкидывал
драматические триггеры (стройка Дэвида: каска, ярость, разлетевшиеся
карандаши) при сжатии 30 сцен в 5-6 блоков, потому что в hard-coded
prompts не было: АЛГОРИТМА РАЗБИВКИ по 4 шагам, ПРАВИЛ 1-4 про реплики,
ИЕРАРХИИ СЖАТИЯ (что жертвовать первым).

**Bundle:** [StoryboardStudio.spec:24](StoryboardStudio.spec:24) добавлено
`('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md', 'instructions')` в `datas`.
На Mac .app разместится в `Contents/Resources/instructions/`, на Win
onedir — `_internal/instructions/`. `sys._MEIPASS` указывает на корень.

**Чтение:** [storyboard_app.py:1568](storyboard_app.py:1568)
`read_bundled_text(rel_path, default="") -> str` — универсальный
read-only текст-helper. Cross-platform через `sys._MEIPASS` (frozen) или
`Path(__file__).parent` (dev). Silent fallback на `default` при ошибке.
Без кэша — кэширование делает caller.

**Селективная загрузка:** [agents/instruction_loader.py](agents/instruction_loader.py).
- `load_instruction_md(filename)` — полный текст файла.
- `extract_md_sections(text, sections)` — pure parser, выбирает разделы
  верхнего уровня (`## N. ...`) по списку номеров, склеивает с
  подразделами `### ...`.
- `load_sections(sections, filename)` — combo + module-level кэш.
- Lazy import `storyboard_app.read_bundled_text` через
  `sys.modules['__main__']` (тот же паттерн что в
  [views/_chat_render.py:_to_slug](views/_chat_render.py:159) для
  `transliterate_for_filename`) — избегает circular import.
- Empty results НЕ кэшируются (фикс v1.0.66 на циклический import при
  ранней загрузке storyboard_app — read_bundled_text ещё недоступна,
  reader падает в lambda fallback; не кэшируя "" даём следующей
  попытке шанс прочитать корректно).

**Карта разделов на агента:**

| Агент | Разделы | Что | Почему |
|---|---|---|---|
| Scriptwriter | [1, 3, 4, 6, 8] | роль + ДНК + тайминг + карта + теги | пишет карту с нуля, всё нужно |
| Validator | [4, 6, 8] | формула + лимиты + теги | проверяет по чек-листу |
| Editor | [3, 4, 6, 8] | ДНК + формула + лимиты + теги | правит карту с учётом ИЕРАРХИИ СЖАТИЯ |
| Context Reviewer | [1, 3] | роль + ДНК | сверяет с Bible + характерами |

**Что НЕ передаётся:** разделы 2 (правила общения — для claude.ai чата,
не для агентов), 5 (рефы среды — отдельный pipeline), 7 (режиссура
камеры — для PromptWriter), 9-12 (выдача промптов, чеклист, передаточный
пакет — не для Scriptwriter).

**Lazy build через PEP 562 `__getattr__`:** [agents/montage_prompts.py](agents/montage_prompts.py)
определяет `__getattr__(name)` который при первом обращении к
`SCRIPTWRITER_SYSTEM` / `VALIDATOR_SYSTEM` / `EDITOR_SYSTEM` /
`CONTEXT_REVIEWER_SYSTEM` строит финальный prompt:

```
SCRIPTWRITER_SYSTEM = _SCRIPTWRITER_ROLE
                    + load_sections([1,3,4,6,8])  ← из .md
                    + _SCRIPTWRITER_JSON_TAIL     ← Studio-specific
                                                    (JSON schema, geometry,
                                                    override про игнор
                                                    plain-text формата)
```

Lazy нужен потому что `threads/montage_orchestrator.py:32` импортирует
`SCRIPTWRITER_SYSTEM` напрямую — это force-resolve через `__getattr__`.
Если бы build был module-level (eager) — он бы выполнялся во время
загрузки `storyboard_app`, когда `read_bundled_text` ещё не определена
(она на line 1568, а `from threads import ...` на line ~30). С lazy —
build откладывается до первого ОБРАЩЕНИЯ к публичному имени из
`MontageOrchestratorThread.run()`, что происходит уже после старта
`app.exec()` — все модули загружены, `read_bundled_text` доступна.

**JSON_TAIL override:** разделы из ГЛАВНАЯ_ИНСТРУКЦИЯ.md описывают
plain-text формат карты (`===МОНТАЖНАЯ_КАРТА_НАЧАЛО===` + сцены).
Studio использует JSON. `_*_JSON_TAIL` константы содержат явный override:
«формат вывода — JSON по схеме ниже, игнорируй plain-text формат из
раздела 6 ГЛАВНАЯ_ИНСТРУКЦИЯ». Это сохраняет ГЛАВНАЯ_ИНСТРУКЦИЯ.md
валидной и для claude.ai-флоу Алекса, и для Studio.

**Fallback:** при ошибке чтения / отсутствии файла в bundle (старый
Installer без `instructions/`) → используется `_FALLBACK_*` константа
(прежний hard-coded текст). Это переходный страховочный механизм для
v1.0.66, удалится в v1.0.67+ после полного rollout.

**Размеры финальных prompts (v1.0.66 real):**

| Агент | Final size | Содержит |
|---|---|---|
| Scriptwriter | ~13 400 ch | role + 5 разделов .md + JSON_TAIL с ПРИОРИТЕТАМИ |
| Validator | ~11 800 ch | role + 3 раздела + 14 пунктов проверки |
| Editor | ~11 500 ch | role + 4 раздела + правила правок |
| Context Reviewer | ~6 500 ch | role + 2 раздела + 4 проверки |

Эти размеры в пределах текущих hard-coded (для Scriptwriter было ~14.7K).
Никаких токен-всплесков — селективная загрузка отрезает лишнее.

## Proxy settings (v1.0.65)

Studio поддерживает HTTP/HTTPS-прокси для всех исходящих запросов
через UI Settings → секция «🌐 ПРОКСИ-СЕРВЕР» (видна всем юзерам, не
админ-only).

**QSettings ключи** (хранятся вне репо — Mac plist / Win registry):
- `proxy/enabled` (bool, default False) — мастер-флаг.
- `proxy/host` (str) — IP или hostname.
- `proxy/port` (str) — номер порта.
- `proxy/username` (str) — auth login, опционально.
- `proxy/password` (str) — auth password, **plain-text** (как `fastgen_api_key`).

**Применение прокси:**
`apply_proxy_from_settings()` ([storyboard_app.py](storyboard_app.py)) читает
QSettings и выставляет env vars `HTTP_PROXY`/`HTTPS_PROXY` (+ lowercase
для совместимости) в `os.environ`. Вызывается в `def main()` ПОСЛЕ
`_init_studio_file_logging()` и `_install_qt_message_handler()`, ДО
`app = QApplication(sys.argv)`. QSettings работает без QApplication когда
передан явный `(APP_ORG, APP_NAME)`.

**Какие потоки наследуют прокси автоматически:**
| Поток | Библиотека | Прокси работает? |
|---|---|---|
| Claude CLI subprocess (montage, autogen, outfits, soften, run_episode, seedance, storyboard_pipeline) | `subprocess` без явного `env=` | ✓ наследует os.environ |
| Fast Gen API (pipeline.py, threads/generate.py) | `requests` (Session + одиночные) | ✓ автоматически из env |
| GitHub Releases (`threads/update.py`) | `requests` | ✓ автоматически |
| GitHub API (`storyboard_app.py` FetchStats / SendUpdate) | `requests` | ✓ автоматически |

**Исключение — `installer_app.py`** ([installer_app.py:294, 396, 421](installer_app.py:294))
использует `urllib.request.urlopen` — **НЕ читает env vars автоматически**.
Это отдельная программа (первая установка), не часть Studio. Юзер с
прокси-сетью должен либо выставить системный прокси перед запуском
Installer.exe, либо отдельная задача — добавить `ProxyHandler` в
`installer_app.py` (см. Долг 7).

**UI и handlers:**
- Секция «🌐 ПРОКСИ-СЕРВЕР» в `_build_settings_tab` между `apikey_frame`
  и v1.0.61 Montage-блоком.
- 4 поля QLineEdit (host/port/username/password) + чекбокс +
  кнопка-глазик 👁/🙈 для пароля + кнопка «Проверить подключение» +
  результат-label (RichText, цветные ✓/✗) + кнопка «Сохранить и
  применить» + хинт про перезапуск.
- При выключенном чекбоксе 4 поля и глазик disabled, кнопки
  «Проверить» и «Сохранить» остаются активны (тест direct connection
  доступен всегда).

**ProxyTestThread** ([threads/proxy_test.py](threads/proxy_test.py)):
делает 3 независимых GET-запроса (`https://api.github.com/zen`,
`https://googler.fast-gen.ai/`, `https://ipinfo.io/json`) с `timeout=10`,
с параметром `proxies` или без него (по `use_proxy: bool`). Отдаёт
результат через сигнал `result_ready(dict)`. Каждый endpoint в своём
`try/except` — частичные ошибки не аборт всего теста. ipinfo.io
используется для отображения геолокации прокси-IP (или реального IP в
direct режиме). Маппинг 2-буквенных кодов стран → русские названия в
`_parse_geo`.

**Изменения требуют рестарта.** `os.environ` читается `requests` при
создании первой сессии в процессе. Изменение env во время работы
Studio НЕ перехватывается уже-созданными `requests.Session()`'ами. UI
явно сообщает юзеру: после Save диалог «Перезапустить сейчас / позже».
При «позже» — дополнительный info-диалог что настройки сохранены, но
не активны до рестарта.

**Безопасность пароля:**
- Хранение plain-text в QSettings (как `fastgen_api_key`). Шифрование
  через Keychain/Credential Manager — см. Долг 8.
- В runtime.log пишется только `host=X port=Y user=Z` (БЕЗ пароля).
- UI маскирует звёздочками по default (`EchoMode.Password`). Глазик
  переключает на `EchoMode.Normal` только при явном клике юзера.

## Монтажная карта — пайплайн агентов (v1.0.62+v1.0.63)

С **v1.0.62 (2026-05-13)** оркестратор [threads/montage_orchestrator.py](threads/montage_orchestrator.py)
работает ЛИНЕЙНО без раундов:

```
1. Scriptwriter (Opus 4.7)         — пишет монтажную карту с нуля
2. Validator   (Sonnet 4.6)        — проверяет один раз
3. Editor      (Sonnet 4.6)        — если Validator.errors > 0, применяет правки
4. Context Reviewer (Sonnet 4.6)   — ОПЦИОНАЛЬНО (toggle в Settings, default OFF)
   └─ если concerns > 0 → Editor ещё раз
5. ФИНАЛ (без повторной проверки)
```

**Что изменилось:**
- Константа `MAX_ROUNDS` УБРАНА (была =2 в v1.0.61, =3 ранее). Цикла больше нет.
- `_agent_log` теперь содержит ровно 1 запись validator (не 2-3).
- `rounds_used` в сигнале `finished_ok` всегда = 1 (поле оставлено для
  совместимости с caller'ом `views/episode_chat.py:_on_montage_finished_ok`).
- Прогресс-стадии: `validator_running` / `validator_done` / `editor_running` /
  `context_reviewer_running` / `context_reviewer_done` — БЕЗ полей `round` /
  `max_rounds` (i18n строки `montage_status_*` соответственно обновлены).
- `montage_status_round_done_errors` теперь честно говорит юзеру:
  «⚠ Чекер: N ошибок, Editor применил правки. Финальная проверка пропущена
  для скорости.» — чтобы юзер знал что R2 убрали и при необходимости
  может прогнать `montage-checker.jsx` вручную.

**Обоснование (анализ ep2 v1.0.61):** Validator R2 длился ~7 мин и нашёл
2 ошибки которые уже не правились (MAX_ROUNDS исчерпан) — пустая трата
времени. ep4 v1.0.61 принят с одного раунда — R2 там не запускался.
Делаем это поведение по умолчанию.

**Blacklist макро-мимики (v1.0.62)** — расширен в трёх местах
[agents/montage_prompts.py](agents/montage_prompts.py):
- `COMMON_RULES` секция «МИМИКА — ТОЛЬКО МИКРОМИМИКА» (для Scriptwriter/Editor).
- `STRUCTURAL_RULES` секция «ЗАПРЕЩЁННЫЕ ФОРМУЛИРОВКИ» (для Validator/Reviewer).
- `VALIDATOR_SYSTEM` пункт 9 — добавлено КАТЕГОРИЯЛЬНОЕ ПРАВИЛО про любые
  синонимы и производные эмоциональных ярлыков.
- `SCRIPTWRITER_SYSTEM` — отдельный абзац «СТРОГОЕ ПРАВИЛО — НИКАКИХ
  ЭМОЦИОНАЛЬНЫХ ЯРЛЫКОВ» сразу после `{COMMON_RULES}`.

Причина: на ep2 v1.0.61 Scriptwriter написал «Face contorted as if in
panic» и «Eyes wide and darting» — формально слов 'panic' и 'eyes wide'
не было в списке из 5 запретов, поэтому Scriptwriter их не считал
нарушением. Расширенный список + категориальное правило закрывают
лексический gap.

**Per-stage timings (v1.0.63)** — каждая стадия теперь замеряется отдельно.
В [threads/montage_orchestrator.py](threads/montage_orchestrator.py) методы
`_call_*` оборачивают `_run_claude` через `t0 = time.time()` →
`duration_sec = time.time() - t0` (замер ТОЛЬКО subprocess CLI, без
парсинга/билда промпта). Поля `started_at` + `duration_sec` пишутся в
`_agent_log[*]`, попадают в `_agent_log_ep*.json` автоматически.

`_build_agent_summary` собирает агрегат `timing = {per_stage: [...],
total_sec: ...}` и кладёт его в `agent_summary['timing']` — БЕЗ
изменения сигнатуры `finished_ok = pyqtSignal(dict, dict, int, str, dict)`.
Старые сборки/caller'ы не ломаются (forward-compat через
`agent_summary.get('timing', {})`).

UI: [widgets/montage_summary_dialog.py](widgets/montage_summary_dialog.py)
рисует моноширинную таблицу таймингов сразу после head_lines (между
отчётом по агентам и таблицей блоков). Стадии не запускавшиеся —
не показываются (нет строк «0 сек»). Display-имена стадий
(`Scriptwriter`/`Validator`/`Editor`/`Context Reviewer`) — на английском,
не локализуются (технические имена агентов). Локализуются только
`timing_section_title` и `timing_total`. Формат времени:
`<60 сек` → `'X сек'`, `≥60 сек` → `'X мин Y сек'`.

**v1.0.64 (2026-05-13) — eyebrow rule conflict + script adaptation:**

1. **Разрешён конфликт COMMON_RULES vs STRUCTURAL_RULES** (нашли на
   замерах v1.0.63 ep2): COMMON_RULES в позитивных примерах содержал
   `"one eyebrow slightly raised"`, а Validator c расширенным blacklist
   v1.0.62 банил эту фразу как семантический эквивалент запрещённого
   `"raised eyebrows"`. Цикл «Scriptwriter пишет по позитивному примеру
   → Validator кидает forbidden_phrase → Editor правит» стоил ~3 мин
   на эпизод. Также `"whites visible above the lower lid"` — анатомическая
   дескрипция wide eyes, потенциальный аналогичный конфликт.

   Правка [agents/montage_prompts.py:187-190](agents/montage_prompts.py:187):
   - `"one eyebrow slightly raised"` → `"slight twitch at the corner of the eye"`.
   - `"whites visible above the lower lid"` → `"left eyelid slightly
     heavier than right"` (асимметрия — принцип из АНТИТЕАТРАЛЬНОГО_СЛОВАРЯ,
     чистая физиология без эмоциональной привязки).

2. **Добавлены ПРАВИЛА АДАПТАЦИИ СЦЕНАРИЯ** в SCRIPTWRITER_SYSTEM
   (нашли на ep2 v1.0.63: Scriptwriter выкинул сцены 6-9 — ярость
   Дэвида на стройке, кульминационный триггер эпизода).

   Иерархия важности из 3 приоритетов (моменты узнавания, эмоциональные
   пики, точка перед клиффхэнгером — НЕ ВЫКИДЫВАТЬ; POV/параллельный
   монтаж/визуальные якоря — СОХРАНЯЙ ПО ВОЗМОЖНОСТИ; связки/повторы/
   описания — МОЖНО ОБЪЕДИНЯТЬ). Размещено в [agents/montage_prompts.py](agents/montage_prompts.py)
   между «СТРОГОЕ ПРАВИЛО» и «ФОРМАТ ВЫВОДА».

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
  инстансов + children) + пауза, затем move обёрнут в retry-loop.
  Причина — Windows Defender держит handle на убитом .exe секунд
  5-30 для real-time scan; одной попытки мало. Каждая попытка
  пишется в bootstrap.log с номером (`move attempt N`,
  `attempt N failed`, `move succeeded on attempt N`).
- **2026-05-11 — КРИТИЧНЫЙ ФИКС sleep idiom (v1.0.40+):**
  Все `timeout /t N /nobreak > nul 2>&1` в bat'е заменены на
  `ping -n N+1 127.0.0.1 > nul 2>&1`. Причина: bat запускается
  из Studio через `subprocess.Popen` со `stdin=DEVNULL`
  ([threads/update.py:526](threads/update.py:526)), а `timeout.exe`
  в этих условиях **мгновенно выходит** с ошибкой «Input redirection
  is not supported» (даже с `/nobreak` — он подавляет только
  keypress). Stderr→NUL → визуально не видно. Эффект: на v1.0.37/38
  **никакой** sleep в bat'е не работал, «6 retries × 2 сек = 12с»
  выполнялись за 300мс, Defender не успевал отпустить handle →
  «MOVE FAILED — target locked» практически гарантированно.
  Формула: `ping -n {sec+1} 127.0.0.1` ≈ `sec` секунд реального
  ожидания, не читает stdin. Идиома стабильна с DOS-времён.
  **НЕ ПРАВИТЬ обратно на `timeout` без понимания причины** —
  есть предупреждающий комментарий в `_make_bootstrap`.
- **2026-05-11 (v1.0.42) — handle release on slow AV stacks:**
  v1.0.41 на машинах с Defender+Yandex Protect стэком показал
  release > 30 сек → 15 retries не хватало. Три правки в Win-ветке
  `_make_bootstrap`:
  (1) Retry: 15 → **30 попыток × 2 сек = 60-секундное окно**.
  Post-taskkill пауза 2с → **5с** (`ping -n 6`).
  (2) **Pre-flight warmup** ДО первой move: PowerShell проходит
  target onedir, open/close каждый файл через `[System.IO.File]::Open`.
  Провоцирует Defender начать post-close-scan СЕЙЧАС пока bootstrap
  занят ~10 сек warmup'ом — продуктивная задержка.
  (3) **AV-snapshot logging** на каждой 3-й failed-попытке:
  `tasklist | findstr /I "MsMpEng Yandex AntimalwareSvc MBAMService
  ekrn avp avast avg"` пишет в bootstrap.log активные AV-процессы.
  Будущая диагностика без Sysinternals.
  Если 60 сек не хватит — следующий шаг **Restart Manager API**
  через PowerShell P/Invoke (v1.0.43+).
- **2026-05-11 (v1.0.44) — Failure mode B (reboot-deferred install):**
  На v1.0.43 наблюдался кейс где Defender держит handle ВСЕ 64 сек
  retry-loop'а беспрерывно — увеличение окна = лотерея. Решение —
  канонический Windows installer pattern: рядом с `update.bat` теперь
  лежит `update_helper.ps1` (генерится в `_make_bootstrap` Win-ветке
  из константы `_PS_HELPER_TEMPLATE`). Два режима:
  - **`-Mode Diagnose`** заменяет старый `tasklist | findstr` AV-snapshot
    на авторитетный Restart Manager API (rstrtmgr.dll) через C# P/Invoke
    stubs. Возвращает точный список holder'ов: PID, AppName, ServiceName,
    Type (Critical / Service / MainWindow / Explorer / etc).
  - **`-Mode Defer`** — escalation после исчерпания 30-сек retry-loop.
    Шаги: (1) RM Diagnose (для лога); (2) Copy new bundle в staging dir
    `<target>.new` (нет AV race — это NEW путь); (3) `MoveFileEx(
    MOVEFILE_DELAY_UNTIL_REBOOT)` через kernel32 P/Invoke: schedule
    deletion для всех файлов и папок target + schedule rename
    staging → target; (4) Write `pending_reboot.txt` в project_root с
    `target_version` + `scheduled_at` (ISO UTC); (5) Delete
    `pending_rollback.txt` (НЕ откатываем версию — install запланирован).
  - **На рестарте Windows** session manager применяет MoveFileEx-очередь
    из реестра (`HKLM\System\CurrentControlSet\Control\Session Manager\
    PendingFileRenameOperations`) ДО загрузки user-сервисов — Defender
    ещё не запущен в эту фазу boot, нет race.
  - **Studio при следующем старте** (`finalize_pending_update` →
    `_finalize_pending_reboot`): получает last boot time через
    `ctypes.windll.kernel32.GetTickCount64()`. Если boot был ПОСЛЕ
    `scheduled_at` → reboot произошёл → MoveFileEx применился → bump
    `version.json[app_version]` на target + удалить все markers →
    показать toast «обновлено». Иначе → reboot ещё не было → показать
    inline-баннер «нужна перезагрузка» (через 7 дней — более настойчивый
    текст с указанием версии).
  - **КРИТИЧНО:** НЕ пытаемся RmShutdown терминировать Defender.
    `MsMpEng.exe` — Protected Process Light (PPL), даже SYSTEM с admin
    не убьёт его. RM API используется ТОЛЬКО для диагностики.
  - **i18n keys:** `update_pending_reboot_short`,
    `update_pending_reboot_urgent`, `update_pending_reboot_dismiss`,
    `update_install_success_toast` — ru/uk/en.
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
- **Character filename invariant** (2026-05-11, v1.0.46): в
  `refs_decisions[<ep>].character[<slug>].filename` ВСЕГДА хранится
  формат `<character_slug>/<file>.<ext>`. Контролируется на двух
  уровнях: (1) caller `_on_outfit_accepted` ([views/episode_chat.py:1866](views/episode_chat.py:1866))
  передаёт `f"{name}/{picked_name}"`, (2) defense-in-depth в
  `_save_ref_decision` ([views/episode_chat.py:2081](views/episode_chat.py:2081))
  auto-prepend'ит folder если caller забыл, с warning в stderr.
- **Twin decisions cleanup** (2026-05-11, v1.0.48): после collision-resolve
  в `episodes.json[ep].refs_decisions[<kind>]` могут оказаться ДВЕ записи
  под разными ключами, указывающие на ОДИН файл:
  K1 = chat-marker name (например `house_corridor`),
  K2 = stem от collision-renamed file (например `house_corridor_2`,
  stem от `house_corridor_2.jpg`). Оба `linked`, оба
  filename=`house_corridor_2.jpg`. `list_episode_refs` итерирует
  decisions и отрисовывает обе как отдельные карточки в UI References
  (юзер видит «House corridor 2» два раза, img7+img8).
  **Защита на двух уровнях:**
  (1) **Prevention** — `_on_gen_use_existing` ([views/episode_chat.py:2303](views/episode_chat.py:2303))
    при Pick existing для location/object: если `stem(picked_name)` !=
    `name` и в decisions есть entry под `picked_stem` с `linked` —
    удалить её (это устаревший autogen twin) перед сохранением новой
    записи под marker name.
  (2) **Healing** — `heal_stale_decisions` ([storyboard_app.py:880](storyboard_app.py:880))
    при старте Studio для каждого bucket'а location/object: найти пары
    `(K1, K2)` где `K2 = K1 + _[2-9]`, оба `linked`, одинаковый filename,
    stem(filename) == K2. Удалить K2 (technical artifact), оставить K1
    (chat marker). Лог `[heal-twin-cleanup]`. Character handler не
    задействован — у character key = имя персонажа, structurally нет
    twin.
- **Marker-alias for collision-renamed decisions** (2026-05-11, v1.0.47):
  AI в чате называет маркер исходным именем (например `house_corridor`),
  но при collision-resolve файл переименован в `house_corridor_2.jpg`
  и decisions ключ — `house_corridor_2`. Это исходное архитектурное
  поведение `_resolve_collision_free_slug` + `_save_active_gen_decision`
  с new_name (существовало с момента создания фичи в БАГ 1 fix).
  `_check_montage_ready` парсит chat-text через `synthesize_gen_markers` →
  marker.name = исходный `house_corridor` → direct lookup в decisions не
  находит → marker считался unresolved → CTA блокировалась. Quick-fix
  v1.0.47: после direct lookup miss пробуем alias ключи `<m.name>_2..._9`.
  Найдено РОВНО ОДНО с decision='linked' → используем (с лог-тегом
  `[marker-alias]`). Несколько → ambiguity treated as unresolved
  (safety). Это **только read-side fallback** — никаких миграций или
  bucket-key renames. Архитектурный фикс (decisions key = chat marker
  name) отложен на будущее (Tech debt).
- **Decisions key rename invariant** (2026-05-11, v1.0.46): при
  collision-resolve через `_resolve_collision_free_slug`
  ([views/episode_chat.py:1470](views/episode_chat.py:1470)) если slug
  переименовывается в `<slug>_N`, то УСТАРЕВШИЙ entry в
  `refs_decisions[ep][<sub_singular>]` под старым slug **удаляется**.
  Новая запись будет добавлена `_on_active_gen_finished` под new_name.
  Без этого decisions продолжал указывать на чужой файл от другого
  эпизода (тот же первоначальный slug, другая картинка). heal на старте
  Studio тоже умеет это лечить через `[heal-manifest-driven]` ветку.
- **Decisions filename self-heal** (2026-05-11, v1.0.45, расширено v1.0.46):
  `heal_stale_decisions(project_root)` вызывается ОДИН РАЗ в
  `MainWindow.__init__` после `finalize_pending_update`. Проходит по
  всем `shows/<slug>/episodes.json`, для каждой записи в
  `refs_decisions[<kind>][<slug>]` с `decision == 'linked'` проверяет
  существование filename на диске.
  - **location/object**: если filename не найден → disk-glob по
    `{stem,slug}.{jpg,jpeg,png,webp}` в той же подпапке refs/.
    Найден → обновляет filename в JSON атомарно (temp + os.replace).
  - **character**: filename = `<folder>/<file>`. Если прямой путь не
    найден → **outfit-safety: НЕ подменяем другим outfit'ом**, только
    лог в stderr. Тихая подмена outfit'а опаснее чем сломанная CTA —
    юзер не заметит что Дэвид сменил куртку.
  Реактивная защита от тех же mismatch'ов — `_linked_file_exists`
  (views/episode_chat.py) теперь имеет disk-glob fallback и для
  character (только смена расширения в пределах одной outfit-folder).
  Также `_sync_decision_filenames_after_regen` (storyboard_app.py)
  обновляет decisions после успешной регенерации в РЕФЕРЕНСАХ — чтобы
  не плодить новые битые decisions.
- **closeEvent graceful thread shutdown** (2026-05-11, v1.0.45):
  до v1.0.45 `closeEvent` полагался на «потоки умрут вместе с процессом»,
  но Qt destructor живого QThread вызывает `qFatal` → SIGABRT. Особенно
  стабильно крашилось при закрытии во время `SeedancePipelineThread`
  + дочерних `GenerateThread`'ов. Теперь:
  1. `_count_active_tasks` считает все типы threads (storyboard, montage,
     outfit, autogen, auth — раньше учитывались только shot/ref/geometry/
     episode).
  2. `_collect_all_threads` возвращает плоский dedup'нутый список из всех
     реестров (MainWindow + EpisodeChatView + NewEpisodeView).
  3. `_graceful_shutdown_all_threads` (вызывается перед `event.accept()`):
     stop() → wait(2s) → terminate() + wait(500ms). После — Qt destructor
     видит мёртвые QThread'ы, никаких SIGABRT.
- **Sender-aware JSONL routing в NewEpisodeView** (2026-05-11):
  `_on_thread_finished` / `_on_thread_error` / `_on_thread_stopped`
  пишут в чат через `target_ep = sender_ep or self._current_ep_id` и
  `_sa.append_chat_message(target_ep, ...)` + `ev.on_external_append`,
  **мимо** `_append_log_persist`. `_append_log_persist` роутит по
  `self._current_ep_id` (id формы) — годится только для актуального
  треда формы. Для параллельных фоновых тредов это вело к misrouting'у
  сообщений `⏹ Остановлено` / `✗ Ошибка` в чужой чат. `_append_log`
  (без persist) можно вызывать только если `is_current_form_thread`.

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

### Долг 7 — installer_app.py не уважает HTTP_PROXY env vars (v1.0.65)

`installer_app.py` использует `urllib.request.urlopen` ([installer_app.py:294, 396, 421](installer_app.py:294)) для скачивания project zip + GitHub release asset. urllib **НЕ читает** `HTTP_PROXY`/`HTTPS_PROXY` env vars автоматически (в отличие от `requests`). Поэтому юзер с прокси-сетью, кому нужна первая установка Studio через `Storyboard Installer.app/.exe`, **не сможет** скачать ассеты — соединение пойдёт прямое и упрётся в network policy.

**Решение:** добавить `urllib.request.ProxyHandler` + `build_opener` явно. Читать прокси-настройки из системы (через `urllib.request.getproxies()`) или передавать через env. Объём — ~30 строк в `installer_app.py:_download_*` методах.

**Срочность:** низкая. Installer запускается ОДНОКРАТНО при первой установке. У коллег корпоративная сеть с прокси — да. Альтернатива на сейчас: поставить системный прокси (System Preferences → Network → Advanced → Proxies) перед запуском Installer'а.

### Долг 8 — пароль прокси хранится plain-text в QSettings (v1.0.65)

`proxy/password` (как и `fastgen_api_key`) хранится в QSettings в plain-text формате — Mac `.plist` / Win Registry / Linux `.conf`. Любой кто получит доступ к юзерскому профилю прочитает пароль.

**Решение:** интеграция с Keychain (Mac) / Credential Manager (Win) / Secret Service (Linux). Самый чистый Python-пакет — `keyring` (PyPI). Изменения:
1. Добавить `keyring` в зависимости + PyInstaller bundle.
2. В `apply_proxy_from_settings` и `_on_proxy_save_clicked` — пароль хранить через `keyring.set_password(service="StoryboardStudio/proxy", username=APP_NAME, password=pwd)` / `get_password(...)`.
3. Из QSettings удалить ключ `proxy/password` (оставить только мастер-флаг enabled + host/port/username).
4. То же сделать для `fastgen_api_key`.

**Срочность:** низкая — `fastgen_api_key` живёт plain-text давно, никаких инцидентов не было. Юзер должен сам понимать что админский профиль = доверенная среда.
