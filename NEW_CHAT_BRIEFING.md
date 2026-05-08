# NEW CHAT BRIEFING — 2026-05-08 (вторая сессия)

**Дата:** 2026-05-08 (день/вечер). Большая сессия — редизайн UI под LUMZ-стиль.
**Юзер:** админ/мейнтейнер. Активный сериал: `finalnyy_raschet`.
**Версия Studio:** свежая сборка 2026-05-08 ~12:50 — со всеми фиксами этой сессии.

---

## Стартовое сообщение для нового чата

> Прочитай `_session_log.md` (хвост ~600 строк — за сегодняшний день
> записей много, редизайн UI этап 1-6) и `NEW_CHAT_BRIEFING.md`.
> Активные задачи — TODO 5/6/7/8 ниже. Скажи «делаем?» когда готов.

После этого Claude должен:
1. Прочитать хвост `_session_log.md` (~600 строк за 2026-05-08) для понимания
   всех правок UI-редизайна этой сессии.
2. Прочитать `CLAUDE.md` (правила сторибординга).
3. Проверить `~/.claude/projects/.../memory/MEMORY.md` — индекс правил.

---

## Что сделано во **второй сессии 2026-05-08** (свежее, после первого handoff)

### Редизайн UI под LUMZ-стиль (Этапы 1-6, частично 6)

- **Этап 1 — фундамент:** создан `views/theme.py` с `LUMZ_THEME` design tokens
  + `LumzBackground(QWidget)` с радиальным градиентом фона главного окна.
- **Этап 2 — шапка:** карточка LUMZ + pill-табы Editor/Actors/Settings БЕЗ
  иконок + lang-btn в обёртке `lang-wrapper` (transparent, для выравнивания).
- **Этап 3 — селектор сериала + эпизоды + плашка серии:**
  кнопки эпизодов «01»/«02» (только цифры, PILL_W=38, padding 4×8),
  лейбл «Эпизод:» перед пилюлями, плашка серии в LUMZ accent_gold,
  кнопка «+» сериала красная subtle, font 14 normal.
- **Этап 4 — полоса блоков:** обёртка `#blocks-bar`, активный блок accent_red,
  Референсы золотые, Чат белый, корзина SVG `trash-2.svg` + hover красный.
- **Этап 5 — карточки шотов + Seedance:** карточки лёгкие LUMZ bg_subtle,
  кнопка `▶ Промпт Seedance` красная залитая, заголовок «Подготовка в
  коридоре (Хс)» с длительностью в скобках, длительности шотов берутся
  из `output/_agent_log_<ep>.json` (надёжный JSON, не зависит от AI annotation).
- **Этап 6 (часть 1) — Refs view + overlay-кнопки:** ref-card в LUMZ,
  круглые → прямоугольные кнопки 36×32 с SVG `pencil/trash-2/sparkles`,
  без прозрачности, primary regen — красная.
- **Этап 6 (часть 2) — Chat view + save/secondary кнопки:** log_view bg_card,
  input bg_subtle, кнопка «Отправить» нейтральная серая.

### Дополнительно

- **«1 серия» вправо:** `pills_container.sizePolicy = Maximum + Fixed`,
  убраны `current_row.addStretch(1)` чтобы layout не растягивался.
- **Длительности шотов** через `get_block_shot_durations(ep_id, block_n)`
  читает `_agent_log_<ep>.json`. Fallback на парсинг promptа.
- **Файловое логирование** Studio: `~/Library/Logs/Storyboard Studio/runtime.log`
  (Mac) / `%LOCALAPPDATA%\Storyboard Studio\Logs\` (Win), кнопка
  «Открыть лог» в Settings, timestamps `[YYYY-MM-DD HH:MM:SS]`,
  Qt warnings ловятся через `qInstallMessageHandler`.
- **Спам в логе** убран: `text-shadow` удалён из QSS, шрифт-стек
  платформо-зависимый (`Helvetica Neue` Mac / `Segoe UI` Win).
- **Save-кнопка** в редакторе: SVG `download.svg`, текст «Сохранить
  сториборд» (без «как PNG»), исправлена опечатка «стриборд» → «сториборд».
- **Корзина двойная исправлена** — в `_apply_translations` убрана
  строка `setText(tr('delete_ep_btn'))` которая перерисовывала emoji
  поверх SVG.
- **Иконки на табах в шапке убраны** (юзер просил).

---

## Что готово в сессии 2026-05-07/08 (предыдущая сессия)

### TODO 1 — UX первичного аналитика
- **1а.** Описания в скобках: `slug (короткое описание)` для location/object;
  для characters: `slug (Имя — короткая роль)`. Парсер `synthesize_gen_markers`
  обновлён.
- **1б.** Авто-язык чата (RU/UK/EN) — уже работало через `Respond in user's language`
  в системном промпте + `--continue` сессия в RunEpisodeThread.
- **1в.** Кнопки **«+ Добавить локацию» / «+ Добавить объект»** в REFS view —
  открывают `RefPickerDialog` с превью.
- **1г.** Confirmation popup в `RefPickerDialog` («Точно выбрать?») перед
  привязкой.

### Корзина / удаление
- Корзина на location/object теперь **НЕ удаляет файл с диска**, только отвязывает
  от эпизода через `refs_decisions[kind][slug]`. Текст popup'а kind-aware
  («Удалить локацию» / «Удалить объект»).

### Outfit picker / Actors
- Outfit picker закрывается СРАЗУ при клике «Создать референс» в Актёрах
  (через `_active_character_gens` registry в MainWindow).

### Кнопка-заголовок эпизода
- Заменено `QLabel` → `QPushButton` с фиолетовым градиентом.
- Клик → **non-modal** попап со сценарием справа от Studio (можно работать
  параллельно). Авто-позиция, повторный клик поднимает существующий.
- Подсветка `СЦЕНА N` / `ЭПИЗОД N` через `_SceneHighlighter` —
  мультиязычно (CЦЕНА|SCENE для RU/UK/EN). Цвета: тёплый янтарный pill для
  сцен, светло-фиолетовый для эпизод-заголовка.
- ВАЖНО: использован `UseUnicodePropertiesOption` иначе `\b` не работает
  с кириллицей.

### Параллельные эпизоды
- Убрана глобальная блокировка «Запустить» — `_threads: Dict[ep_id, Thread]`
  per-ep. Юзер может запускать ep5/ep6/ep7 одновременно.
- **`_on_chunk_persist` использует sender ep_id** (не shared `_current_ep_id`)
  — раньше чанки ep5/ep6 попадали в чат ep7 при быстрых параллельных запусках.
- `_check_montage_ready` тоже per-ep.
- MontageCTA баннер возвращается при перезаходе на эпизод где идёт оркестратор
  (фикс `MontageCTA.show_running()` теперь вызывает `self.show()`).

### REFS view
- Переключение эпизодов **сразу обновляет REFS** (без манёвра чат→рефы).
  Трекер `_refs_view_built_for_ep` решает когда no-op vs rebuild.
- Точки на REFS-пилюле — **per-episode** (не блинкают на чужих ep).
- **Прогресс-overlay** на refs-карточках:
  - Image gen: тёмный overlay 🤖 + «Генерирую изображение … (5с)» + счётчик.
  - Geometry (только для location): сменяется на «Обновляю описание … (12с)».
  - Object: только image gen фаза, geometry skip (раньше было — но писалось
    бесполезное в `<obj>_geometry.txt`).
  - Счётчик переживает rebuild через `_active_image_paths` registry с
    `started_at`.
- **NEW-бейдж** на refs-карточках: появляется ПОСЛЕ полного завершения
  (для location: после geometry; для object: после image-gen). Очищается
  при уходе с REFS view (`_unseen_refs[ep]`).
- Подтверждение перед regen (попап «Перегенерировать локацию/объект?
  Заменит текущее»).

### pipeline.py
- Флаг **`--kind=object|location`** — раньше всё писалось в `LOCATIONS_DIR`
  (объекты тоже), `_prompt.txt` объектов оставался orphan'ом.
- **Расширение по магическим байтам** — раньше дефолтил на `.jpg` если
  Content-Type пуст, контент мог быть PNG.

### MIME / Edit рефа
- В `_upload`: MIME определяется по магическим байтам файла (не по
  расширению). Старые `.jpg` с PNG-контентом теперь корректно отправляются
  серверу.
- В `RefGenerateThread`: переключено с хардкода OpenAI на **выбор провайдера
  через `_sa.image_provider()`** (default NARWHAL = `/api/v4/flow/image/generate`).
  OpenAI имел pydantic-баг на `reference_images` — теперь NARWHAL.

### Прочее
- Правило «`Дэвид — главный герой`» — убрано. Аналитик пишет роль ТОЛЬКО
  из текста сценария.
- Fallback prompt-файла для объектов с цифровым суффиксом (`shotgun1.jpg`
  → `shotgun_prompt.txt`).

### Кросс-платформа
- ВСЕ правки сессии — чистый Path/json/Qt/regex/requests. Без subprocess
  (кроме существующих claude CLI вызовов которые ужe были).
- Память сохранила правило в `feedback_windows_crossplatform.md`.

---

## TODO в новом чате

### TODO 1 — Авто-перевод сценария на язык чата

**Запрос юзера:**
> Можно сделать чтобы сценарий в попапе автоматически переводился на язык
> чата? Я начал чат на украинском → клик на кнопку «5 серія» → попап
> показывает сценарий на украинском.

**Текущее состояние:**
- Сценарий в `shows/<slug>/scenarios/epNN.txt` — оригинальный язык
  (тот, на котором юзер закинул).
- `storyboard_app.py:_on_ep_title_clicked` (~line 5760) читает файл и
  показывает в `QPlainTextEdit` с подсветкой через `_SceneHighlighter`.
- `_SceneHighlighter` понимает CЦЕНА/SCENE/ЭПИЗОД/ЕПІЗОД/EPISODE.

**План реализации (на согласование с юзером):**
1. **Кэш переводов:** `shows/<slug>/scenarios/epNN_<lang>.txt`
   (например `ep05_uk.txt`). Создаётся лениво при первом открытии в
   этом языке.
2. **Определение языка чата** — helper `_detect_chat_language(ep_id)`:
   читает последние user-messages из `chats/<ep>.jsonl`, эвристика на
   характерные буквы/слова (RU: ё, ы, ъ; UK: і, ї, є, ґ; EN: только
   латиница).
3. **Background-перевод** — новый `ScenarioTranslateThread` (по образцу
   `ClaudeGeometryThread`). Запускает `claude -p` с инструкцией:
   ```
   Translate this episode scenario to {target_lang}.
   CRITICAL RULES:
   - Keep ALL dialogue lines in quotes UNCHANGED (they're original English).
   - Translate scene labels: СЦЕНА N → СЦЕНА N (UK same as RU) / SCENE N (EN).
   - Translate ЭПИЗОД N → ЕПІЗОД N (UK) / EPISODE N (EN).
   - Keep structure (line numbers, blank lines) identical.
   ```
4. **UI flow в `_on_ep_title_clicked`:**
   - Определить язык чата текущего ep.
   - Если `epNN_<chat_lang>.txt` существует → показать.
   - Если нет → показать оригинал + индикатор «Перевожу на UK…» в заголовке.
   - При завершении треда → если попап ещё открыт, обновить текст.
5. **i18n** — ключи `scenario_translating`, `scenario_translation_failed`.

**Сложность:** средняя. Будет работать на 100% корректно для нарратива.
Реплики персонажей в кавычках сохраняем как есть (они в pipeline должны
оставаться оригинальные).

**Подвох:** перевод через `claude -p` — отдельный subprocess. Должен иметь
`CREATE_NO_WINDOW` на win32 (см. правило кросс-платформы).

### TODO 2 — Win10/11 prep блокеры (отложено)

3 файла без `CREATE_NO_WINDOW` для `subprocess.Popen` на win32:
- `threads/autonomous_gen.py:216`
- `threads/suggest_outfits.py:256`
- `threads/generate.py:587`

См. `_WINDOWS_PREP_TODO.md`. ~10 минут перед Win-релизом.

### TODO 3 — Чистка orphan'ов в refs/locations/

Из-за старого pipeline.py баги, в `refs/locations/` остались
`<object>_prompt.txt` файлы для объектов:
- `black_frame_mirror_prompt.txt`
- `phone_prompt.txt`
- `spy_camera_prompt.txt`
- `table_lamp_prompt.txt`
- `double_barrel_shotgun_prompt.txt`

Можно почистить вручную или скриптом — НЕ критично, fallback в
`_on_ref_regen` подхватывает их корректно.

### TODO 5 — Анимация бегущих точек «Думаю...» в чате

**Запрос юзера 2026-05-08 вечер:** «когда написано в чате „Передаю секцию“,
потом „Думаю“ — хочу чтобы точки бежали тик-тик-тик прямо в чате».

**Текущее состояние:** анимация `_thinking_step` УЖЕ есть в
`views/new_episode.py` — но обновляет только `status_lbl` (отдельная
полоска статуса), а не последнюю строку в `log_view` (само поле чата).
Юзер хочет чтобы точки бежали В САМОМ ЧАТЕ.

**Реализация (~30 строк):**
- В `_tick_thinking` (или новый метод) — найти последнюю строку
  `log_view`, начинающуюся с `▶ Думаю` (через QTextCursor +
  movePosition(End) + select(LineUnderCursor)).
- Заменить её на `▶ Думаю {dots}` где `dots` = `["·   ", "··  ", "··· ", "····"][step]`.
- Триггер каждые 400мс пока `_thread.isRunning()`.
- Альтернатива проще: после получения первого chunk от AI — точки убирать.

**Файлы:** `views/new_episode.py:_tick_thinking` + аналогично
`views/episode_chat.py` (там тоже `_thinking_timer` для followup).


### TODO 6 — Индикатор «Долго думаю, это нормально»

**Запрос юзера 2026-05-08 вечер:** иногда AI thinks 2-3 минуты на
тяжёлый первый запрос (большой prompt: сценарий + bible + voice profiles).
Юзер не понимает что происходит, думает что зависло.

**Реализация:**
- Добавить timer в `RunEpisodeThread` start: через **120 секунд** без
  первого chunk → `progress.emit("⏳ Долго думаю — это нормально для первого запроса. Не закрывай Studio.")`.
- Текст в i18n: `thinking_long_hint` (RU/UK/EN).
- Когда первый chunk пришёл — таймер отменяется, обычная работа.

**Файлы:** `threads/generate.py:RunEpisodeThread.run` + `i18n.py`.


### TODO 7 — Цвет диалогов в попапе сценария эпизода

**Запрос юзера 2026-05-08 вечер:** «в попапе сценария эпизода (клик
на золотую плашку «Сумасшедший») есть подсветка `СЦЕНА N` /
`ЭПИЗОД N`. Хочу чтобы ДИАЛОГИ персонажей тоже подсвечивались своим
цветом».

**Реализация:**
- В `_SceneHighlighter` (поиск в `storyboard_app.py` или views/) —
  добавить третий regex-format для строк вида `Имя: "..."` или
  `Имя — "..."` (формат диалогов в сценарии).
- Цвет — нейтральный голубой/зелёный (не красный — он у эпизода,
  не золотой — у сцен).

**Файлы:** `views/new_episode.py` или `storyboard_app.py` (где `_SceneHighlighter` живёт).


### TODO 8 — Кнопки «+ Добавить» в REFS view

**Запрос юзера 2026-05-08 вечер:** «во вкладке REFS не доделаны кнопки
„Добавить локацию“, „Добавить объект“, „Добавить персонажа“ —
надо в LUMZ-стиле сделать».

**Реализация:**
- Найти где они создаются (вероятно `_build_refs_view` в storyboard_app.py).
- Перекрасить под LUMZ accent_red_subtle (как кнопка `new_show_btn`)
  или с radius_md и border_strong.

**Файлы:** `storyboard_app.py:_build_refs_view`.


### TODO 4 — Вернуть кнопку «🎨 Сгенерировать» после ошибки AutonomousGenThread

**Запись 2026-05-08.**

**Симптом:** юзер кликнул «🎨 Сгенерировать buldog» → subprocess `claude -p`
упал → popup с ошибкой → юзер дисмиснул → **кнопки больше нет в чате**,
перегенерить нельзя без перезапуска Studio.

**Корневая причина:**
- `EpisodeChatView._gen_seen_names` хранит имена для которых карточка уже
  была создана (защита от дублей при стримминге).
- При успехе name остаётся в seen → нормально.
- При **ошибке** name остаётся в seen, карточка из layout удаляется →
  при перезаходе в эпизод `synthesize_gen_markers` находит маркер заново,
  но `_maybe_show_gen_button` блокирует фильтром `name in _gen_seen_names`.

**Реализация (~10 строк):**
- В `MainWindow._on_active_gen_error` (storyboard_app.py ~line 3643) после
  `_active_gens.pop(key)` найти `EpisodeChatView` для `ep_id`:
  ```python
  ev = self.episode_chat_view  # один view, переключается между ep_id
  if ev is not None and ev._ep_id == ep_id:
      ev._gen_seen_names.discard(name)
      # пере-синтезировать → карточка появится
      msgs = read_chat_messages(...)
      ev._restore_gen_buttons_from_history(msgs)
  ```
- Альтернатива проще: при error `AutonomousGenThread` **всегда** делать
  `ev._gen_seen_names.discard(name)` независимо от текущего ep'а.

**Файлы для правки:**
- `storyboard_app.py:_on_active_gen_error` (~line 3643)
- `views/episode_chat.py` — может потребоваться public-метод
  `forget_gen_button(name)` если приватный `_gen_seen_names` лучше не
  трогать снаружи.

**Не блокирует:** workaround — перезапустить Studio. На следующем старте
`_gen_seen_names` пусто, при заходе в эпизод
`_restore_gen_buttons_from_history` находит маркер и создаёт карточку.

---

## Правила работы (кратко, полный список — в memory `MEMORY.md`)

- **Кросс-платформа Mac+Win10/11:** все правки на обеих ОС. Subprocess'ы —
  `CREATE_NO_WINDOW` на win32. `pathlib.Path` всегда. Никаких shell-only
  команд (см. `feedback_windows_crossplatform.md`).
- **Спрашивать «делаем?»** перед любой правкой кода.
- **После правки** — короткий «что проверить» чек-лист.
- **Логи:** каждая правка — запись в `_session_log.md` (дата, что трогал,
  что НЕ трогал, верификация, маркеры).
- **Auto-rebuild:** после правок сам делать `rm -rf build/ && ./build.sh
  && open dist/Storyboard\ Studio.app`. Юзер не запускает билд руками.
- **AST + smoke** перед билдом.
- **Слово «Claude»/«Клод» НЕ использовать** в UI Storyboard Studio
  (юзер-видимые строки). Заменять на «ассистент»/«AI»/«ИИ».
- **Lucide иконки** только из `assets/icons/`, через `get_icon('name')`.
- **Модульная архитектура** — новые фичи в свой файл (`views/`,
  `widgets/`, `threads/`), не дамп в `storyboard_app.py`.
- **Anti-context-loss:** незнакомый паттерн (`_unseen_shots`,
  `_active_regens`, `_active_image_paths`, `_unseen_refs`) — ищи в
  `_session_log.md` зачем оно. Не удалять.
- **«Стоп» = пауза, не откат** — при `стоп` от юзера просто остановиться.
- **Никаких автодействий по убийству процессов** — перед `pkill` спросить
  есть ли активные генерации.

---

## Активный сериал и состояние

- `current_show.json` → `{"current": "finalnyy_raschet"}`.
- Эпизоды: ep1..ep7 (но юзер мог удалить-пересоздать ep5/6/7 во время
  тестирования параллельных запусков).
- `shows/finalnyy_raschet/refs/locations/` — несколько orphan-промптов
  (см. TODO 3).

---

## Контактные файлы

- `_session_log.md` — полный лог за день (~50 записей с маркерами).
- `NEW_CHAT_BRIEFING.md` — этот файл (актуальная версия).
- `NEW_CHAT_BRIEFING_old_2026-05-07_19-00.md.bak` — предыдущая версия (для
  справки если что-то непонятно).
- `~/.claude/projects/.../memory/MEMORY.md` — индекс правил.
- `_WINDOWS_PREP_TODO.md` — Win-blockers checklist.
- `_UI_TODO.md` — UI LUMZ-стиль: что осталось доделать (NewEpisodeView,
  Actors, Outfit picker, NewShowDialog, AuthBanner, PromptRetryDialog,
  ShotViewerDialog, ActiveGensPanel, Settings tab, эмодзи в монтажке).
  Создано 2026-05-08 после большой UI-сессии.
- `CLAUDE.md` — правила сторибординга (для AI agents).

Welcome aboard! 🎬
