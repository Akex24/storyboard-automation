# ARCHITECTURE — Storyboard Studio

**Последнее обновление:** 2026-06-28 (FastGen v6 — миграция ЗАВЕРШЕНА: весь стек генерации/usage/cancel на /api/v6/generations, результат читается из results[0].download_url с v5-fallback на storage_id. Плюс перебор ключей по кругу в генераторе (картинки+видео).)

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

## Правило parent для QThread, запущенного из QDialog (2026-06-19, фикс SIGABRT)

Qt-инвариант: если QThread ещё `isRunning()` в момент разрушения владельца,
Qt-destructor зовёт `qFatal` → SIGABRT («QThread: Destroyed while thread is
still running»). Для тредов, ЗАПУСКАЕМЫХ из диалога, это даёт стабильный
краш при закрытии диалога крестиком до конца работы треда. Есть ДВА
жизнеспособных паттерна — при добавлении нового диалога с фоновым QThread
ОБЯЗАТЕЛЬНО выбрать один:

**Паттерн A — `parent=None` + хранение ссылки на долгоживущем объекте (view).**
Тред создаётся `parent=None`, ссылка кладётся в список на view (переживает
диалог). Диалог закрылся — тред дописывает в фоне, его сигналы безопасно
отваливаются при удалении получателя. Эталон: `views/actors.py`
`start_ref_generation` — `GenerateActorRefThread(..., parent=None)` +
`self._ref_threads` (с явным комментарием-предупреждением, :1518). Совместим
с graceful-shutdown окна (см. ниже «closeEvent graceful thread shutdown»,
v1.0.45 — дренаж реестров stop()→wait()).

**Паттерн B — `parent=self` (тред — Qt-ребёнок диалога) + teardown на ВСЕХ
путях закрытия.** Жизнеспособен ТОЛЬКО если диалог гарантированно
останавливает И ДОЖИДАЕТСЯ (`wait()`) тред перед `super().closeEvent()`.
Путей закрытия ТРИ, и ВСЕ обязательны:
  • `reject()` — кнопка «Закрыть» + Escape;
  • `accept()` — позитивное закрытие (выбор варианта и т.п.);
  • `closeEvent()` — КРЕСТИК ОКНА (✕). Крестик идёт через `closeEvent`,
    НЕ через `reject()`; без переопределённого `closeEvent` тред не
    остановится → SIGABRT.
Эталон: `widgets/prompt_retry_dialog.py` `PromptRetryDialog` — soften-тред
`SoftenPromptThread(parent=self)` гасится единым `_stop_soften_sync()`
(`_dot_timer.stop()` + `stop()` + `wait(2000)` + diag-лог
`[SOFTEN-STOP-TIMEOUT]` при таймауте), вызываемым из reject/accept/closeEvent.
`SoftenPromptThread.stop()` делает `terminate()` + `wait(timeout=1)`
подпроцессу, чтобы `run()` вышел быстро и `wait(2000)` не блокировал GUI.

ОПАСНАЯ ТОЧКА: новый QDialog с фоновым QThread БЕЗ переопределённого
`closeEvent` — мина. Крестик окна разрушит диалог с живым тредом → SIGABRT.
До фикса 2026-06-19 на этом падал PromptRetryDialog: soften-тред `parent=self`,
но `reject()` стопал без `wait()`, `accept()` не стопал, `closeEvent`
отсутствовал. Перед добавлением нового диалога с тредом — СНАЧАЛА выбери
паттерн A или B.

## Живой скраб видео в попапе генератора — pre-decode JPEG-в-RAM + overlay (2026-06-29)

Скраб playhead в `GeneratorViewerDialog` ([generator/viewer_dialog.py](generator/viewer_dialog.py))
переведён с `QMediaPlayer.setPosition` (перематывал по keyframe, коалесцировал
частые seek → кадр застывал/догонял с задержкой — полукостыль с троттлом) на
**вариант A+C: пред-декод всех кадров в RAM + overlay-превью**.

**Новый модуль [generator/scrub_decoder.py](generator/scrub_decoder.py)** (изоляция —
рабочий `generator_video_thread` НЕ трогаем, backend-логику ДУБЛИРУЕМ):
- `open_capture(path)` — `cv2.VideoCapture` с перебором системных backend'ов
  (darwin=`CAP_AVFOUNDATION`, win32=`CAP_MSMF`, else default+фоллбэк 0). Та же
  причина, что в `_extract_first_frame`: FFMPEG-бэкенд cv2 в PyInstaller не
  собирается, системные фреймворки работают frozen. cv2 ленивый.
- `ScrubPreloadThread(QThread)` — фоновый ПОСЛЕДОВАТЕЛЬНЫЙ декод (`grab()`+
  `retrieve()`, не seek-per-frame) всех кадров в **ПОЛНОМ разрешении видео** (без
  downscale — кэш = качество видео, скраб не мылит): `cv2.imencode(".jpg", q90)` →
  **байты В ПАМЯТИ** (НИ ОДНОГО write на диск). Кэш = `list[(ts_ms, jpeg_bytes)]`.
  Сигналы `ready(cache)` / `failed()`. Мягкий кап памяти (`mem_cap_mb=128`) с
  прореживанием по `stride` — страховка для аномально длинных/HFR/4K видео (на
  обычных 6-15с@24-30fps stride=1). Замер (720p q90, ~100КБ/кадр): 6с=**14МБ**/500мс,
  10с≈23МБ, 15с≈35МБ. (Изначально кэш был 480px/q85≈2.7МБ — давал блюр при апскейле;
  Alex: кэш в полном разрешении, память 14-35МБ приемлема.)
- `grab_full_frame_jpeg(path, pos_ms, q92)` — одноразовый ПОЛНОКАДРОВЫЙ захват по
  playhead для кнопки-плюсика «взять кадр в реф»: детерминированный seek POS_MSEC,
  не зависит от async videoSink (кэш и так уже полноразмерный — но grab отдельный
  путь, q92, без участия прогрева).

**Цвет:** ручной BGR→RGB НЕ нужен — кэш и grab идут через JPEG (cv2.imencode пишет
из BGR корректный стандартный JPEG, `QPixmap.loadFromData`/Qt декодит в RGB).

**Поток-lifecycle — паттерн A** (`parent=None` + ссылка `self._preload`): исключает
Qt-destructor SIGABRT. `_teardown_scrub()` (`stop()`+`wait(1500)` → `cap.release()`
в run-finally; стоп спиннера; hide overlay; **очистка кэша из RAM**) зовётся из
ВСЕХ путей закрытия non-modal Tool-диалога: `closeEvent` (крестик) И переопределённый
`reject()` (Escape идёт через `reject()→hide()`, НЕ через `closeEvent`!).

**Прогрев (доп. UX):** на `showEvent` стартует пред-декод; до `ready` — `_BusySpinner`
(вращающаяся accent-дуга, `paintEvent`+`QPainter`+`QTimer`, accent_red из
LUMZ_THEME) поверх превью + ползунок/play `setEnabled(False)` (юзер не скрабит до
готовности → нет заикания). `_TimelineTrack` mouse-handlers получили гард
`if not self.isEnabled(): return` + dim в paintEvent + `changeEvent` прячет плюсик.

**Скраб по кэшу:** `_on_seek_moved` → `_frame_at(pos)` (`bisect_right` по `_scrub_ts`
→ ближайший слева → `QPixmap.loadFromData`) → `pm.scaled(overlay, KeepAspectRatio,
SmoothTransformation)` (кадр полноразмерный, гладко вписан, без блюра; БЕЗ
`setScaledContents` — он масштабирует грубо) → overlay.setPixmap. Плеер при скрабе НЕ
дёргаем. Если pre-decode `failed` → `_scrub_cache=None` → ГРЕЙСФУЛ-фоллбэк на старый
троттл-`setPosition` (`_SEEK_THROTTLE_MS`, попап рабочий).

**Гладкий стык overlay→видео на release (анти-мерцание «кадр назад», 2026-06-29):**
наивный порядок (overlay.hide()+vw.show() ДО того как `setPosition(final)` догнал) →
на миг виден предыдущий видео-кадр. Фикс: на `release` сначала `setPosition(final)`,
overlay ДЕРЖИМ на финальном кадре, и `_begin_overlay_handoff` ждёт `videoFrameChanged`
(vw декодировал кадр на новой позиции) ИЛИ страховочный таймаут ~120мс → только потом
`_end_scrub_overlay` (порядок: `vw.show()` ПОТОМ `overlay.hide()`). Идемпотентно
(`_handoff_active`); teardown отменяет незавершённый handoff (disconnect+stop) — иначе
singleShot/сигнал выстрелит на закрытом диалоге.

**Overlay поверх НАТИВНОГО QVideoWidget** — константа `_SCRUB_OVERLAY_MODE`:
- `"child"` — overlay дочерний vw, `raise_()` поверх; vw не прячем.
- `"hide"` (**рабочий default с 2026-06-29**) — overlay сиблинг в `_video_frame`;
  на drag `vw.hide()`, на release `setPosition(final)`→handoff→`vw.show()` (см.
  «Гладкий стык» ниже).

ПРОВЕРЕНО НА .app (2026-06-29): `"child"` НЕ работает — нативный видео-слой macOS
перекрывает QLabel-overlay (при drag ЧЁРНЫЙ экран вместо кадра, тот же класс бага,
что pillarbox: нативный слой композитится поверх Qt-сиблингов). Поэтому shipping-
default = `"hide"`: на время drag прячем `vw`, показываем overlay-сиблинг (честный
Qt z-order), на release возвращаем `vw`. `_begin/_update/_end_scrub_overlay`
ветвятся по константе; `_sync_scrub_geometry` для `"hide"` ставит overlay в
`vw.geometry()` (overlay и vw — оба дети `_video_frame`, одни координаты).

**Чёрный экран в конце видео (EndOfMedia, 2026-06-29):** доиграв до конца,
QMediaPlayer уходит в Stopped, нативный слой чернеет (последний кадр не держится).
`_on_media_status` на `mediaStatusChanged==EndOfMedia` отматывает на кадр 0 тем же
проверенным приёмом, что прайминг в showEvent (`pause()`+`setPosition(0)`). Play не
ломается — следующий ▶ играет с 0; playhead встаёт в 0 через `positionChanged`.

**Cross-platform:** macOS cyrillic-путь подтверждён замером (40/40; репо под
«…/Работа/…»). Windows .exe (VideoCapture+не-ASCII+MSMF на random/sequential) —
ПРОВЕРИТЬ при сборке под Win.

## Пайплайн монтажки (режим C) — линейный, один проход Opus 4.8 (Этап 2, 2026-06-29)

`MontageOrchestratorThread` ([threads/montage_orchestrator.py](threads/montage_orchestrator.py))
перестроен с многоагентной эскалации на ЛИНЕЙНЫЙ пайплайн. Цель — Opus 4.8
пишет всю карту (геометрия+тайминги+всё), Python проверяет, при ошибках —
ОДИН узкий проход Opus 4.8. Раньше: Scriptwriter→Validator(R1)→Geometry Editor→
Editor→Validator R2→Editor R2→Validator R3→Context Reviewer→Editor-after-reviewer.

**Новый run():**
1. `_call_scriptwriter` (Opus 4.8, `MODEL_SCRIPTWRITER`/`MODEL_EDITOR` = claude-opus-4-8).
2. `_apply_post_check_timings` — Python, поднимает `duration_sec` реплик до min.
3. `prefilter_check` (Python, `validator_prefilter_c`) — механика: блок 4 шота/15 сек +
   **наличие `geometry`** у шотов блока с `len(characters) >= 2` (новая функция
   `_check_geometry_presence`, код ошибки `block_N_shot_M_missing_geometry` —
   совместим с EDITOR_SYSTEM). БЕЗ AI.
4. Если prefilter нашёл ошибки → ОДИН `_call_editor` (Opus 4.8) по списку →
   повторный `_apply_post_check_timings` + перепроверка прейфильтром. Без эскалации.
5. `_finalize`.

**УБРАНО полностью:** AI-Validator (Haiku, все R1/R2/R3), Geometry Editor (Haiku),
Context Reviewer (Sonnet), Editor-after-reviewer, методы `_call_validator`/
`_call_geometry_editor`/`_call_context_reviewer`, константы `MODEL_VALIDATOR`/
`MODEL_CONTEXT_REVIEWER`/`MODEL_GEOMETRY_EDITOR`, импорты их промптов. `STAGE_ORDER`
ужат до `[scriptwriter, editor]` (resume-гейт только `editor_ran`).

**Честный статус при падении Editor:** в except НЕ кладём `"error"` в `_agent_log`
(иначе `_finalize` ушёл бы в `failed`/Resume) — ставим `checker_report =
{ok:False, errors, report:["Editor-проход не выполнен…"]}` → `_finalize` отдаёт
`finished_ok` с честным статусом, НЕ старый список как финал. НЕ ретраим.

**Mode A protected НЕ затронут:** правился `validator_prefilter_c.py` (режим C),
базовый `validator_prefilter.py` (md5-protected) — нет. Лимит total 60-80с в режиме
C отсутствует и НЕ добавлялся. `prefilter_check` — чистый Python, модель не зовёт.

**Долг (отложено):** `_build_agent_summary` ещё содержит inert-ветки validator/
geometry/reviewer (никогда не триггерятся, краша нет — диалог читает через `.get`);
визуальное «Validator: 0» в `widgets/montage_summary_dialog.py` — отдельный
косметический коммит.

## Нарезка Seedance-промпта по шотам — seedance_shot_slicer.py (2026-06-29)

Кнопка «Рефы» (`MainWindow._on_block_refs_btn`, [storyboard_app.py](storyboard_app.py))
кроме сборки папки блока (`.cache/_block_view/<ep>_block<N>/`) теперь нарезает
блочный Seedance-промпт на пошотовые папки `shots/shot_<k>/` — у каждого шота
свой самодостаточный промпт + только его рефы + ПАНЕЛЬ ЭТОГО шота (кроп из
склеенного листа сториборда, с face-сетками).

**Новый leaf-модуль [seedance_shot_slicer.py](seedance_shot_slicer.py)** (только
stdlib re/shutil/pathlib, без Qt/subprocess → нет циклов, кросс-платформенно):
- `_parse_prompt(text)` — режет .txt на `pre`(внешняя легенда `@imageN=name` +
  `─────`/БЛОК), шапку `风格/表演风格/时长`, блок `参考说明` (распарсен на ref-блоки
  `[XX参考: @imageN]`), `场景设置`, шоты (по `[HH:MM-HH:MM] 镜头N`; предшествующий
  transition-маркер HARD CUT/MATCH CUT/CONTINUOUS приклеен к шоту), хвост
  `技术参数/限制`, `post`. None если структура не распознана.
- `_build_shot_text` — собирает промпт шота: легенда(фильтр)+шапка+`参考说明`(фильтр
  по `used @imageN`)+`场景设置`(целиком, НЕ фильтруется)+СЕГМЕНТ ШОТА **дословным
  срезом строк**+хвост. Номера `@image` НЕ перенумеровываются.
- `slice_block_to_shots(...)` — главный вход.

**Маппинг «рефы шота» (структурный, не парсинг китайского):**
`shots[].scene_action [@]imgK` → `resolved[K-1]` (тот же порядок
`[location, *objects, *characters]`, что строит `_on_block_refs_btn`; конвенция
[@]img — [agents/montage_rules_d.py:166](agents/montage_rules_d.py:166),
[agents/storyboard_writer_prompts.py:9](agents/storyboard_writer_prompts.py:9)).
Для фильтрации китайской шапки: `slug → @imageN` через распарсенную легенду .txt
и `name.lower().replace(' ','_')==slug`. Легенда даёт `@image(K+1)=name` (storyboard
`@image1` впереди → сдвиг +1). `@image1` (Storyboard) — в каждом шоте всегда.

**Панель шота (кроп из склейки, 2026-06-29):** в `shots/shot_<k>/` кладётся НЕ весь
лист, а ПАНЕЛЬ этого шота, вырезанная из склеенного `<ep>_block<N>.jpg` (он несёт
face-сетки) по детерминированной раскладке `stitch_shots_to_landscape`
([storyboard_app.py:5607](storyboard_app.py:5607)): `PANELS=4`, для `aspect 16:9`
сетка `2×2`, для `9:16` — `4×1`. `panel_w=W//cols`, `panel_h=H//rows`; панель шота N
= `cell(N-1) = ((N-1)%cols·pw, (N-1)//cols·ph)` (без зазоров). Caller
(`_on_block_refs_btn`) берёт aspect через `show_manager.show_aspect`. Подпись «SHOT N»
уже внутри панели. PIL ленивый (как в stitch; PIL уже в бандле для face-grid). Нет
листа в dest_dir → панель не кладётся (лог).

**Условия/edge:** источник нарезки — SAVE-файл `<show>_<ep>_block_<N>.txt` в `dest_dir`,
который пишет кнопка «💾 Save» попапа Seedance (`_do_save`, текст АКТИВНОЙ вкладки =
утверждённая версия). Файла нет (Save не нажат) → молча скип + лог
`[block_refs] … not saved yet` — нарезка появляется РОВНО когда и общий промпт в «Рефах»
(он и есть этот же Save-файл, сохранённый cleanup-keep-паттерном). Генерационный
`output/seedance/<ep>_block_<N>.txt` (есть СРАЗУ после генерации, до Save) НЕ используется.
Шот без `[@]img` → только панель сториборда (`used_imageN={1}`). tag out-of-range /
slug-miss → лог+пропуск, не падает. Режется СОХРАНЁННАЯ активная версия
(оригинал/regen/compress), `_tab<K>.txt`/`_tabs.json` не трогаются. `shots/` — dir →
текущий cleanup-rmtree-цикл в `_on_block_refs_btn` сносит её на следующем клике и
пересоздаёт (без накопления стале).

**PyInstaller:** импорт ленивый (`from seedance_shot_slicer import …` внутри
button-only handler → пост-сборочный smoke-тест старта его не дёргает), поэтому
`'seedance_shot_slicer'` добавлен в `hiddenimports` [StoryboardStudio.spec](StoryboardStudio.spec).

**Также в «Собрать эпизод» (2026-06-29):** нарезка работает И в
`CompileEpisodeThread` ([threads/compile_episode.py](threads/compile_episode.py)) —
для каждого блока тем же `slice_block_to_shots` в `block_<n>/shots/`. Источник/гейт —
тот же Save-файл: `_copy_seedance_txt` упрощён (fallback на активную вкладку `_tabs.json`
и генерационный `output/seedance/<ep>_block_<n>.txt` УБРАН) — в zip кладётся ТОЛЬКО
Save-файл; несохранённый блок → НЕТ ни промпта, ни `shots/` (строго как кнопка «Рефы»).
`_copy_block_refs` возвращает `resolved` (DRY, та же резолюция → маппинг
`[@]imgK → resolved[K-1]` байт-в-байт совпадает с одиночной кнопкой); aspect/раскладка
из `show_manager.show_aspect`. `compile_episode.py` импортит `slice_block_to_shots`
(уже в hiddenimports) и `show_manager`.

**НЕ затронуто:** `seedance_prompts.py` (SYSTEM-правила), montage_card/episodes.json/
seedance .txt — только ЧИТАЮТСЯ. Голосовая чистка (`_purge_phantom_voice`) — отдельный
шаг, не связан.

## Движение камеры Seedance — единый модуль camera_movement_rules.py (2026-06-19)

Раньше дефолтный handheld был жёстко вшит в `agents/seedance_prompts.py`
(SYSTEM, блок 风格: `手持纪录片风格`) → ВСЕ Seedance-промпты получали
дрожащую ручную камеру по умолчанию, плюс ПРАВИЛО 7 запрещало любое
движение внутри шота (шоты статичны на стыке). Теперь правила движения
вынесены в отдельный leaf-модуль, handheld перестал быть дефолтом, а
движение внутри шота разрешено (с замиранием за 0.5с до hard cut).

**`agents/camera_movement_rules.py`** — единый источник правил движения
камеры. Лист-модуль: импортирует только stdlib, сам никого из приложения
не тянет → нет циклических импортов (см. [PyInstaller circular imports]);
PyInstaller подхватывает статически через
`from agents.camera_movement_rules import …` в `seedance_prompts.py`
(в `agents/__init__.py` НЕ экспортируется — приватная зависимость
seedance). Экспортирует:
- `CAMERA_MOVEMENT_RULES` — методический RU/ZH-блок для Opus: ЧАСТЬ 1
  (движение внутри шота по содержанию кадра), ЧАСТЬ 2 (4 типа межшотового
  перехода), ЧАСТЬ 3 (10 режиссёрских приёмов). Полной статики нет
  (микро-дыхание 1-2%); `author_camera` всегда приоритетнее правил.
- `CAMERA_BLOCK_HEADER` = `运镜：` — заголовок пер-шот секции движения в
  ВЫХОДНОМ промпте (sentinel; стиль как 镜头：/动作：).
- `TRANSITION_MARKERS` = `[HARD CUT` / `[MATCH CUT` /
  `[CONTINUOUS CAMERA MOVE` / `[CONTINUOUS HANDHELD`.
- `PROTECT_MARKERS` = `CAMERA_BLOCK_HEADER` + `TRANSITION_MARKERS` —
  манифест того, что compress не режет.

**Сборка SYSTEM** (`seedance_prompts.py`): шаблон переименован в
`_SYSTEM_TEMPLATE` с плейсхолдером `〈CAMERA_MOVEMENT_RULES〉`; на module-load
`SYSTEM = _SYSTEM_TEMPLATE.replace("〈CAMERA_MOVEMENT_RULES〉",
CAMERA_MOVEMENT_RULES)`. `.replace`, а НЕ f-string — в SYSTEM полно
китайских скобок, f-string рискован. В пер-шот шаблоне добавлена строка
`运镜：` после блока `镜头`; `[HARD CUT]` заменён на
`[ТИП ПЕРЕХОДА — обоснование]` (дефолта нет, выбор по ЧАСТИ 2). ПРАВИЛО 7
переформулировано: движение ВНУТРИ шота разрешено, но перед `[HARD CUT]`
обязано замереть за 0.5с до конца (`结束前0.5秒停止`); сквозной проезд через
границу — только в CONTINUOUS-режимах.

**Защита в compress** (`SYSTEM_COMPRESS`): по той же схеме —
`_SYSTEM_COMPRESS_TEMPLATE` с токеном `〈CAMERA_HDR〉`, на module-load
`.replace("〈CAMERA_HDR〉", CAMERA_BLOCK_HEADER)`. Важен порядок: `.replace`
на module-load ДО рантайм-`.format(target/limit/current)` в
[threads/seedance_pipeline.py](threads/seedance_pipeline.py) — токен без
`{}`, format не ломает. Добавлен 12-й пункт нерушимого списка: блок `运镜`
и маркеры переходов копируются символ в символ, НЕ входят в очередь резки.
Пункт 2 обобщён с одного `[HARD CUT]` на все 4 типа перехода. Очередь
резки (1)风格 (2)свет-技术参数 (3)场景设置 движение камеры не трогает.

**Будущая правка правил движения = правка ТОЛЬКО
`camera_movement_rules.py`.** Это и есть цель выноса.

**Reference-доки** (в рантайме НЕ читаются, правлены для согласованности):
`instructions/ИНСТРУКЦИЯ_ИДЕАЛЬНЫЙ_ПРОМПТ_SEEDANCE 2.0.txt` (секции про
handheld/переходы, примеры промптов) и
`instructions/АНТИТЕАТРАЛЬНЫЙ СЛОВАРЬ — ЗАМЕНЫ EMOJI-ЭМОЦИЙ НА МИКРОМИМИКУ.txt:197`.

Mode A (`agents/montage_rules.py` / `agents/validator_prefilter.py` /
`instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ.md`) НЕ затронут — `seedance_prompts.py`
общий для всех режимов A/B/C/D (mode_loader разводит только montage_rules_*),
поэтому правка камеры работает во всех режимах из одного места.

## Выбор стиля сториборда: Рисованные/Реалистичные (2026-06-13)

Флаг `style` ('sketch'|'realistic') выбирается попапом при «Делать
сториборды» (`episode_chat._on_montage_confirm_storyboards`), хранится
по-блочно в `episodes.json[ep]['blocks'][str(n)]['style']` и доходит до
писателя через `StoryboardPipelineThread(style=...)` → `build_system`.
Канон-предложения шапки вынесены в константы `SKETCH_STYLE_SENTENCE` /
`REALISTIC_STYLE_SENTENCE` ([agents/storyboard_writer_prompts.py](agents/storyboard_writer_prompts.py))
— единый источник истины для `build_system` и текстовой подмены.

**Нюанс idempotent-skip (важно):** pipeline пропускает блок, чей `.txt`
уже на диске (`exists() && size>100`), НЕ зовя Opus. Раньше это «замораживало»
старую шапку стиля. Теперь в skip-ветке
([threads/storyboard_pipeline.py](threads/storyboard_pipeline.py)) ПЕРЕД
`block_prompt_ready.emit` вызывается `apply_style_to_prompt_text(text,
self._style)` — чистая текстовая подмена ОДНОГО предложения шапки
(sketch↔realistic) БЕЗ LLM. Так повторное «Делать сториборды» с другим
стилем перегенерирует готовый блок в выбранном стиле (тело/геометрия/рефы
не трогаются). Fallback: если в `.txt` нет ни одного канон-предложения
(перефраз писателя — на практике 0 из 418 файлов) → файл оставляется как
есть + WARN в stderr. `_clear_current_block` по-прежнему `.txt` не удаляет —
подмена этого и не требует.

## Face-grid pipeline (PNG-сетки на лица, 2026-06-02)

Фича модерации Seedance: при «Сохранить сториборд» поверх записи чистого
склеенного листа открывается попап наложения PNG-сеток на лица.

**Точка входа:** `MainWindow._save_png` ([storyboard_app.py:13890](storyboard_app.py:13890))
сначала пишет чистый `<ep>_block<N>.jpg` через `stitch_shots_to_landscape`
(JPEG quality=95), ЗАТЕМ открывает `GridDialog(candidate, ep_id, block_n,
dest_dir).exec()` (модальный, ленивый импорт — без circular import). Вариант A:
чистый лист сохраняется как раньше (нужен «Собрать серию»), попап — надстройка.

**Файлы фичи** (`widgets/face_grid/`):
- `detector.py` — YuNet (opencv), `detect_faces(path)` → `[(x,y,w,h), …]`.
  Декод изображения через PIL→numpy (cv2.imread падает на не-ASCII путях).
- `library.py` — персистентная библиотека PNG-сеток + активная сетка.
- `grid_dialog.py` — попап. Классы:
  - `StoryboardView(QGraphicsView)` — зум колесом + панорама; картинка в сцену в
    ПОЛНОМ разрешении → **координаты сцены = пиксели оригинала 1:1**.
    `mouseDoubleClickEvent` по пустому месту → `_add_grid_at` (ручная установка).
  - `GridItem(QGraphicsPixmapItem)` — наложенная сетка. `setOffset(-pw/2,-ph/2)`
    → origin = ЦЕНТР, `setScale` масштабирует вокруг центра. Хранит `_src_path`
    (PNG в библиотеке — источник альфы при композите), `_on_delete` (коллбэк).
    `shape()` = весь прямоугольник (хитбокс ловит и прозрачные клетки).
  - `_ResizeHandle` (правый-низ, ресайз от центра) / `_DeleteHandle` (лево-верх,
    крестик) — `ItemIgnoresTransformations` (постоянный экранный размер).

**Ключевой инвариант координат:** сцена = пиксели чистого jpg 1:1. `GridItem`
origin = центр → при сохранении угол для Pillow = `pos − (pw·scale/2, ph·scale/2)`.

**Сохранение (Этап 7, `GridDialog._on_save`):** композит через PIL — база
`<ep>_block<N>.jpg` (та же, что на диске) → RGBA, прозрачный layer размера базы,
каждая сетка читается из `_src_path` (полная альфа) → resize LANCZOS → `layer.paste`
(клипает off-canvas) → `alpha_composite` → RGB → `save(JPEG q=95)` поверх ТОГО ЖЕ
файла. Ноль сеток → файл не трогаем (он уже чистый). Ошибка → попап не закрываем.
Перезаписанный файл читают и «🗂 Рефы блока», и `compile_episode._copy_storyboards`
([compile_episode.py:203](threads/compile_episode.py:203)) — то есть сетки попадают
в «Собрать серию» автоматически, `compile_episode` не трогается.

**Cross-platform:** только PyQt6 + PIL через `str(Path)`. Без subprocess/shell/cv2-imread.

**Сборка:** opencv + numpy + YuNet-модель добавлены в `StoryboardStudio.spec` (Этап 0).

**Персист состояния (Этап 8, 2026-06-02):** при `_on_save` рядом со сторибордом
пишется `grids.json` (`.cache/_block_view/<ep>_block<N>/grids.json`): `schema`,
`board_w/h`, `grids[]` — для каждой сетки имя PNG (НЕ путь — резолв через
`library.get_grid_path`, переживает смену машины) + центр (пиксели оригинала) +
scale. При открытии попапа `GridDialog._restore_grids` поднимает сетки живыми
`GridItem` поверх ЧИСТОЙ базы (stitch пересобирает jpg до попапа → нет задвоения).
Ноль сеток при сохранении → `grids.json` удаляется (нет восстановления пустоты).
PNG удалён из библиотеки → сетка пропускается + счётчик. `board_w/h` —
sanity-check: при смене размера листа восстанавливаем, но предупреждаем хинтом.
Константа имени — `GRIDS_JSON_NAME` в `grid_dialog.py`.

**КРИТИЧНО — preserve от cleanup'а:** `grids.json` живёт в `.cache/`, который
выборочно чистит `MainWindow._on_block_refs_btn` («🗂 Рефы блока»,
[storyboard_app.py:12166](storyboard_app.py:12166)) — удаляет всё, кроме
сторибордов и Seedance-артефактов. В keep-клаузу добавлено `item.name ==
"grids.json"` (литерал-дубль `GRIDS_JSON_NAME` — при переименовании менять в
ОБОИХ местах). `_save_png` (legacy-regex `<base>_\d+`) и `_do_save` (Seedance)
grids.json не трогают.

## Обратимый кроп версии шота (C2, 2026-06-04)

В окне шота (`ShotViewerDialog`, `widgets/shot_viewer_dialog.py`) большое
превью — зумируемый `StoryboardView` (импорт из `grid_dialog.py`, его НЕ
трогаем). Юзер зумит/панорамит версию и при закрытии окна (Escape/крестик)
сохраняет КАДР как кроп. Кроп **обратим**: при повторном открытии отматывается
от чистого оригинала, не от уже-кропнутого.

**Файлы на версию `v{N}` (в `output/storyboards/_history/<base>/`):**
- `v{N}.jpg` — видимый/активный результат (оригинал + применённый кроп). Уходит
  в лист (`stitch` берёт активный `shot_path`), Seedance, zip, «Рефы блока».
- `orig_v{N}.jpg` — ЧИСТЫЙ оригинал, снимок `v{N}` до ПЕРВОГО кропа, неизменен.
- `crop_v{N}.json` — `{schema, scene_rect:{x,y,w,h}, img_w, img_h}`; scene_rect в
  пикселях оригинала.

**КРИТИЧНО:** префиксы `orig_`/`crop_` НЕ начинаются с `v`+цифра → их НЕ видят
`list_shot_versions`/`_has_any_versions`/лента версий/`stitch`/Seedance/zip
(фильтр `name.startswith("v") and int(stem[1:])`, [storyboard_app.py:4287](storyboard_app.py:4287)).
`threads/generate.py` (regen/realistic/edit) тоже фильтрует через
`list_shot_versions` → новые версии независимы, старые сохраняют свою пару
orig/crop. Полная чистка — `rmtree _history` при «Удалить эпизод».

**Хелперы** ([storyboard_app.py](storyboard_app.py), рядом с
`add_shot_version_from_bytes`): `shot_orig_path` / `shot_crop_json_path` /
`read_shot_crop` / `apply_shot_crop` (снимок оригинала один раз → crop(orig,
rect) → resize к размеру шота → `v{N}.jpg` q95 → json → копия в active +
`set_active_version`) / `clear_shot_crop` (восстановить из оригинала, удалить
orig+crop). Кроп всегда считается ОТ ОРИГИНАЛА → без потери качества.

**Сохранение** (`_maybe_save_crop` в `closeEvent`/`reject`, ПЕРЕД
`_activate_selected_version`): если `_preview_dirty` — видимый scene-rect
(`mapToScene(viewport)`); ≥98% по обеим осям → `clear_shot_crop` (сброс), иначе
`apply_shot_crop`. Сам делает selected активной → `_activate` становится no-op
(без двойной записи). Не dirty → файл не трогаем.

**Восстановление** (`_show_version`): есть `read_shot_crop` → грузим `orig_v{N}`
в вид + `fitInView(saved_rect)` отложенно через `QTimer.singleShot(0)` (после
show, когда вьюпорт реальный). Карточку грида обновляет MW-слот
`_on_shot_crop_committed` по сигналу `crop_committed`.

**Зеркало (M, 2026-06-04):** `crop_v{N}.json` несёт `mirror: bool`. Видимый
`v{N} = orig → [flip-horizontal если mirror] → [crop если scene_rect]`
(хелпер `_render_shot_version`). Зеркало СБРАСЫВАЕТ кроп (упрощение). Тогл —
overlay-кнопка `flip-horizontal-2` (родитель `preview_view`, НЕ в сцене → зум
её не двигает) → `set_shot_mirror`. Восстановление: при `mirror` грузим
`flip(orig)` в вид (rect хранится в координатах flip(orig)). В `_maybe_save_crop`
сброс кропа (≥98% по обеим осям) зовёт `set_shot_mirror(current_mirror)` (НЕ
`clear`) — иначе отзум на отзеркаленном снёс бы зеркало. mirror=false → пути
идентичны чистому кропу (байт-в-байт, проверено md5).

**Удаление версий (2026-06-04):** крестик `x` на `VersionThumb` (скрыт на
минимальной версии и на активной) → `delete_shot_version(history_dir, n,
active_path)`: удаляет v{N}+orig_v{N}+crop_v{N}.json, перенумеровывает хвост
m>N вниз на 1 ЛОКСТЕП (v/orig/crop вместе), активная едет по картинке (A>N →
A-1). Anti-clobber: ascending-порядок + проверка not target.exists() перед
os.replace. Видимость крестиков — `_refresh_thumb_deletability` (лёгкий, без
пересоздания ленты) после смены active без refresh (клик/зеркало).

## Маркер на шоте — элемент сцены, НЕ translucent overlay (2026-06-07)

В окне шота (`ShotViewerDialog`) кнопка «карандаш» включает рисование красным
маркером поверх превью версии. Размеченная картинка уходит в Nano Banana как
база `[@]img0` при AI-edit (Шаг C, commit 0d711b5) — модель понимает, какой
объект тронуть. Реализация рисования — `_MarkerItem(QGraphicsItem)` в
`widgets/shot_viewer_dialog.py`, добавляется в сцену `preview_view` (тот же
`StoryboardView`, что и кроп) через `_scene.addItem`, поверх `pixmap_item`
(`setZValue(1000)`).

**КРИТИЧНО — почему ЭЛЕМЕНТ сцены, а НЕ translucent child-виджет:** первая
реализация (Шаг A) была `_MarkerCanvas(QWidget)` с `WA_TranslucentBackground`
поверх viewport'а QGraphicsView. На внешних мониторах с дробным DPR (M4 Pro +
5120×2880, macOS 26.5, Qt 6.10) включение маркера роняло Studio с SIGSEGV:
`QBackingStore::flush → QPaintDevice::devicePixelRatio()`. Translucent child
поверх QGraphicsView — известный антипаттерн Qt: его per-widget backing-store
на cocoa с дробным DPR падает при alpha-композите. Перенос на элемент сцены
(commit 3916fd3) убрал отдельный translucent-слой — item рендерится в
backing-store самого view, краш-корень исчез. **Не возвращать translucent
overlay поверх QGraphicsView.** (Промежуточный курсорный фикс d33b0d3 —
целочисленный dpr + realized-only `devicePixelRatioF` — корректен и оставлен,
но краш НЕ закрывал; корень был в оверлее, не в курсоре.)

**Инвариант координат:** `_MarkerItem` и `pixmap_item` сидят в origin сцены
без `setPos`/трансформа → item-координаты = scene = пиксели картинки 1:1.
Штрихи (`_strokes: list[list[QPointF]]`) хранятся в scene-координатах →
привязаны к картинке при любом зуме/панораме (сцена трансформирует сама).
Перо `setCosmetic(True)` — постоянная экранная толщина независимо от зума.
Рисование разрешено только внутри `pixmap_item.boundingRect()`. На время
маркера `preview_view.setDragMode(NoDrag)` (ЛКМ рисует, не панорамит),
прежний dragMode возвращается на выключении. `_bake_marked_image` запекает
штрихи в копию pixmap'а версии (НЕ рендер сцены → задвоения нет); base_image
edit-флоу не зависит от пути записи результата → штрихи в сохранённый
`v{N}.jpg` не попадают (Nano Banana перерисовывает чистый кадр).

## Кнопка «Улучшить» — зрячий Sonnet переписывает RU→EN промпт (2026-06-08)

В окне AI-edit шота (`_ask_edit_full_prompt`, [storyboard_app.py](storyboard_app.py))
под полем короткой правки — кнопка «✨ Улучшить». Юзер пишет правку по-русски
простыми словами → Sonnet 4.6 ГЛЯДЯ на картинку текущей версии шота переписывает
её в короткий командный английский промпт для Nano Banana (image-edit). Текст в
поле заменяется результатом — юзер видит и может поправить перед отправкой.

**Зрячий канал — тот же приём, что `ClaudeGeometryThread`:** `ImprovePromptThread`
([threads/improve_prompt.py](threads/improve_prompt.py)) зовёт `claude -p
--system-prompt … --dangerously-skip-permissions --model claude-sonnet-4-6`,
cwd=project_root, timeout 120, CREATE_NO_WINDOW на win32. Sonnet открывает картинку
**Read-инструментом** (абсолютный путь в user-prompt) — НЕ через Anthropic vision
SDK (Max-подписка, без per-token billing). Системный промпт `_NB_IMPROVE_SYSTEM`
лежит в том же файле (НЕ в bundled `ГЛАВНАЯ_ИНСТРУКЦИЯ.md` — та про монтаж):
короткий императивный английский, «keep everything else unchanged», сохранять
арт-стиль (sketch/photo не конвертировать), учитывать красную обводку маркера.

**Marker-aware:** картинка для Sonnet — `_bake_marked_image()` если открыт
`ShotViewerDialog` со штрихами (та же размеченная картинка, что увидит Nano
Banana → Sonnet целится в обведённый объект), иначе чистый `shot_path`. temp-PNG
маркера чистится на `th.finished` (как в Шаге C edit). Системный промпт велит НЕ
описывать сам красный след — он только указатель «какой объект».

**Модалка + async:** вызов в фоне (QThread) под guard'ом `_improve_state['alive']`
+ `_detach` виджет-апдейтеров на `dlg.finished` (поздний результат не трогает
удалённые виджеты) + ссылка в `self._improve_threads`. На время вызова — анимация
мигающих точек (QTimer parented к dlg), кнопка с зарезервированной шириной
(`fontMetrics`, паттерн `_start_animation`) и лево-выравниванием текста, чтобы
слово не ездило при смене 1/2/3 точек.

## Instant gen-overlay карточки шота (2026-06-07)

Индикатор генерации на карточке шота (`ShotCard`, [widgets/editor_widgets.py](widgets/editor_widgets.py))
показывается **моментально** при заходе на блок с активной генерацией и тикает
плавными секундами — перенос механизма с карточки актёра (`ActorCard`). До этого
был кривой: тонкая полоска под шотом, секунды рывками раз в ~4с (зашиты в текст
`step_label`, обновлялись по сигналу потока `step.emit`), индикатор появлялся с
задержкой (`set_loading(True)` сам ничего не рисовал, ждал следующий `step.emit`).

**Виджет (копия ActorCard):** `gen_overlay` — `QWidget` поверх `img_container`,
затемнение `rgba(20,14,30,0.78)`, **border-radius 6px** (КРИТИЧНО: совпадает с
радиусом картинки `img_label`/pixmap-маски — при 11px в углах проступала светлая
дуга 6–11px, артефакт). Внутри — indeterminate `QProgressBar(setRange(0,0))` +
жёлтые секунды, БЕЗ белого текст-лейбла (в отличие от ActorCard). Методы
`start_progress(started_at=None)` / `stop_progress()` / `_tick_progress()` 1:1 из
ActorCard: **свой `QTimer(1000мс)`** считает `int(time.time() - _gen_started_at)`
→ секунды плавные, независимо от сигналов потока. Guard в `set_progress`:
`if gen_overlay.isVisible(): return` — старый тонкий бар не рисуется поверх
overlay (нет двойного индикатора).

**Реестр старта:** `MainWindow._shot_gen_started_at: Dict[(block, panel_idx), float]`
([storyboard_app.py](storyboard_app.py)) — момент старта генерации шота, аналог
`_active_generations` у актёров. Block-open loop при отрисовке синхронно опрашивает
реестры и зовёт `start_progress(self._shot_gen_started_at.get((name, i)))` → overlay
сразу, секунды с верного числа (не сбрасываются при пересоборе/повторном заходе).

**Точки старта/стопа:**
- Одиночная генерация (AI-edit, text-regen, regen, realistic) + batch
  (`_start_storyboard_block`) + Mode C (`_start_storyboard_block_mode_c`): пишут
  `started_at` + зовут `start_progress`.
- Стоп — через существующий re-render блока: `_on_regen_done`/`_on_regen_error`
  (одиночная/batch, реестр `_active_regens`) и `_on_mode_c_version_finished`/`_error`
  (Mode C) попают реестр + перерисовывают блок → block-open loop видит «не активно»
  → `stop_progress`. Явный `stop_progress` в обработчиках не нужен.

**Mode C — last-version-stop:** на одну карточку `(block, panel)` идёт N версий,
реестр `_active_mode_c_version_threads` с **3-tuple ключом `(block, panel, version)`**.
Overlay держится пока в реестре есть хоть одна `(block, panel, *)` (block-open loop:
`any(b == name and p == i for (b, p, _v) in ...)`) и гаснет ТОЛЬКО когда ушла
последняя версия шота. `_shot_gen_started_at` чистится в Mode C-обработчиках по
тому же предикату «версий не осталось» (не на первой завершившейся).

## Архитектурные решения которые легко забыть

- **Anthropic API напрямую НЕ вызывается** (нет per-token billing мимо Max-
  подписки). `pipeline.py` использует только Fast Gen AI (генерация картинок).
  НО Claude РЕАЛЬНО ВИДИТ картинки — через **Read-инструмент** headless
  `claude -p`, не через Anthropic vision SDK. Два таких зрячих канала:
  `ClaudeGeometryThread` (описание геометрии локации — `claude -p` читает jpg
  рефа и переписывает `<slug>_geometry.txt`) и кнопка «Улучшить» (см. секцию
  выше). `ClaudeGeometryThread` — legacy имя (Anthropic API напрямую не зовёт),
  но это полноценный **vision-канал**: Claude смотрит на картинку через Read.
- **Caching работает прозрачно через Claude CLI** (серверный prefix-cache
  включён по умолчанию у Anthropic). **Переход на anthropic Python SDK
  делать НЕ надо** — это per-token billing мимо Max-подписки админа.
- **`PRESERVE_ON_UPDATE` ([storyboard_app.py:134](storyboard_app.py:134))
  используется только в мёртвом `DownloadUpdateThread`** — расширять без
  причины не нужно (см. ниже секцию «Архитектура обновлений»).
- **Claude CLI: system-prompt → файл, user-prompt → stdin** (2026-06-15,
  helper `threads/_claude_shared.py`). На Windows `.cmd`-shim запускается
  через cmd.exe → лимит командной строки **~8 KB на ВСЮ команду**. Любой
  system_prompt 12-22 KB (Scriptwriter / Validator / Editor /
  ContextReviewer / Seedance / Storyboard PromptWriter) ИЛИ user_prompt
  > 5 KB (Outfit picker до 27 KB, Soften prompt до 7 KB) валит запуск
  до старта claude. Симптомы: «The command line is too long.» (для
  user_prompt в argv) и **«failed to write stdin: [Errno 32] Broken pipe»**
  (для system_prompt в argv — claude.cmd упал на старте, stdin pipe
  закрылся прежде чем мы успели write).

  Helper-API:
  - `write_system_prompt_to_tmp(text) → Path` — `tempfile.mkstemp` UTF-8;
    caller обязан `path.unlink(missing_ok=True)` в finally.
  - `supports_system_prompt_file(cli) → bool` — runtime-проверка через
    `claude --help`; **кешируется ТОЛЬКО True**. False/exception НЕ
    кешируем — иначе медленный Win cold-start `--help` навсегда
    зафиксировал бы False и фикс молча отвалился.
  - `build_system_prompt_args(cli, tmp_path, raw_text)` — отдаёт
    `["--system-prompt-file", str(path)]` если CLI поддерживает,
    иначе fallback `["--system-prompt", raw_text]` (старое поведение).
  - `popen_kwargs_for_claude(**extra)` — единая точка `text=True` +
    UTF-8 + `errors='replace'` + `CREATE_NO_WINDOW` на win32.
  - `send_prompt_via_stdin(proc, user_prompt)` — write + close + молча
    глотает BrokenPipe (caller отдельно вызывает `raise_if_died_early`).
  - `raise_if_died_early(proc, settle_ms=50)` — 50мс sleep + `poll()`;
    если процесс уже exit'нул с rc≠0 → вытаскивает stderr и raise с
    понятным «claude died immediately: rc=X stderr=…». Защита от
    «нет ответа» через 600с при следующем Win-баге.

  Применено в коммите A: `MontageOrchestrator._run_claude` +
  `_run_claude_stream` (закрывает Scriptwriter/Validator/Editor/
  ContextReviewer), `SuggestOutfitsThread.run` (Outfit picker),
  `RunEpisodeThread.run` (refactor на helper без смены поведения,
  старый локальный stdin-фикс v1.0.96 заменён на helper-API).

  Коммит Б (профилактика, после подтверждения коллеги): Seedance ×3,
  Storyboard PromptWriter, Soften, Translate, Autonomous, Improve,
  camera_director.

  **НЕ затронуто:** `ClaudeGeometryThread` (короткий `lang_phrase` < 1 KB),
  `auth_switch` (команды claude --login/logout/status без промптов).

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

## Mode C — N версий шота / изолированный реестр тредов (2026-06-06)

**С 2026-06-06:** в Mode C при batch-генерации сториборда из монтажки
(«Сделать сториборд») на каждый шот спавнится **N параллельных тредов**
(N задаётся в Settings → «Версий на шот», 1-10, дефолт 1). До фикса этого
не было: один шот = одна картинка через единственный `GenerateThread`.

**Главная проблема, которую решает фикс — гонка записи в `_history`:**
если бы N тредов сохраняли версии через `next_history_index`
([storyboard_app.py:4320](storyboard_app.py:4320)), все читали бы один и тот
же `next_n` (TOCTOU между листингом и записью) и затирали бы друг другу
`v{N}.jpg`.

**Решение — заранее назначенный `version_index`:** каждый тред получает свой
`version_index` (1..N) и пишет напрямую в `v{version_index}.jpg`, **минуя**
`next_history_index`. Параметр `GenerateThread(..., version_index=None)`
([threads/generate.py:120](threads/generate.py:120)); при `None` — старое
поведение (regen/edit/realistic из попапа шота не затронуты). Ветка записи —
[threads/generate.py:796+](threads/generate.py:796).

**Изолированный реестр живых тредов:**
`self._active_mode_c_version_threads: Dict[tuple, GenerateThread]` — ключ
**3-tuple** `(block, panel_idx, version_index)`. НЕ пересекается с
`_active_regens` (2-tuple `(block, panel_idx)`). Сделано так осознанно: 13
мест читают `_active_regens` с распаковкой `(b, _)` (6 распаковок → ValueError
на 3-tuple + 7 membership/pop) и обслуживают Mode A/B. Отдельный реестр
оставляет их **байт-в-байт**.

Теперь:
- `_start_storyboard_block_mode_c` ([storyboard_app.py:12476+](storyboard_app.py:12476))
  — спавнит N тредов на каждый шот, кладёт в новый реестр.
- `_on_mode_c_version_finished` / `_on_mode_c_version_error` — pop из реестра
  + декремент общего счётчика `_storyboard_active_pending` (= `len(shots) * N`);
  при 0 → `_maybe_start_next_storyboard_block`. `_active_regens` НЕ трогают.
- `_collect_all_threads` ([storyboard_app.py:9497](storyboard_app.py:9497)) —
  дренаж нового реестра при graceful shutdown (`closeEvent` → stop+wait),
  иначе SIGABRT от живого QThread на закрытии.
- `_count_active_operations` — учёт нового реестра в UX-диалоге подтверждения
  закрытия Studio.
- `_tick_dots` / `_block_indicator_for` / `_refresh_block_indicator` — точки
  анимации блоков видят ОБА реестра (`(b, _)` для старого, `(b, _p, _v)` для
  нового).

**Гейт:** ранний диспатч в `_start_storyboard_block` при `mode == 'c'` **И**
`N > 1`; иначе старая ветка (не-C режимы, N=1) работает как раньше, без
сдвига кода.

**gen_time per-shot (не per-version):** ключ QSettings `gen_time_{block}_shot{N}`
без `version_index` — последний завершившийся тред перезапишет своим временем
(= самой долгой версии, т.к. стартовали одновременно).

## Дотяжка недостающих версий Mode C — 1 повтор/версия (2026-06-09)

При нехватке живых ключей / залипании сервера часть N версий шота падает (403/
timeout) — наблюдалось 5-7/10. Дотяжка добивает недостающие версии БЕЗ ручного
перезапуска: при падении версии сразу запускается **ровно одна** повторная
попытка параллельно остальным.

**Стратегия — Вариант B (per-shot retry в `_on_mode_c_version_error`):**
- Ракурс упавшей версии **восстанавливается из `v{N}.prompt.txt`** (`_recover_camera_override`,
  regex `^CAMERA:`). Этот файл пишется в [generate.py:815](threads/generate.py:815)
  **ДО** POST-запроса → переживает падение по 403/timeout. CameraDirector повторно
  НЕ дёргается. Нет `CAMERA:`/файла → повтор с авторским ракурсом (None) — валидная
  версия лучше дырки.
- Повтор идёт через `_spawn_one_mode_c_version(block, panel, v, camera_override)` —
  вынесенное тело цикла `_spawn_mode_c_versions` (DRY, проводка сигналов идентична).
  Рефы авто-резолвятся существующим путём GenerateThread. `generate.py` НЕ трогался.
- Цель — `mode_c_versions_per_shot()` из QSettings (НЕ хардкод 10).

**Анти-задвоение (РОВНО 1 повтор):** `self._mode_c_retried: set` хранит ключи
(block,panel,version) уже повторённых версий. `_try_retry_mode_c_version` повторяет
только если key НЕ в множестве; перед спавном `add(key)`. Повтор сам падает → key
уже в множестве → обычный путь «не вышло». Чистится в начале `_spawn_mode_c_versions`
(новый блок = свежий бюджет).

**Гейты повтора** (`_try_retry_mode_c_version(..., msg)`): **(0) ошибка НЕ таймаут**
(2026-06-10: `msg` содержит `api timeout`/`timed out` → False); (1) key не
повторялся; (2) есть `v{N}.prompt.txt` (иначе нечего воспроизводить); (3)
`key_pool.live_key_count() > 0` (все ключи мёртвые → уведомление + skip, без
заведомо-мёртвого запроса). `live_key_count()` зеркалит фильтр `next_key`
(get_keys − `_read_disabled`).

**Гейт 0 — почему таймаут не повторяем (2026-06-10):** poll-таймаут (300с) =
сервер перегружен/медленный. Повтор в такой сервер почти обречён и **удваивает
время блока** (300+300с) — на диагностике ep25_block_6 дотяжка усугубляла
зависания, а не помогала (первая пачка v1-v10 висла 300с сама, ДО повторов; корень
— серверная медлительность, не наш код). Таймаут теперь идёт обычным путём «не
вышло» (msg логируется, бюджет `_mode_c_retried` не тратится). Повтор остаётся
только для БЫСТРЫХ ошибок (404/4xx), где он дёшев и часто помогает.

**Сохранение исходной ошибки (2026-06-10):** до фикса при ЗАПУСКЕ повтора
early-`return` в хендлере минул `_notify_storyboard_failure(... msg ...)` → в чат
шло только «повтор (дотяжка)», исходный текст ошибки терялся (диагностическая
дыра). Теперь уведомление о повторе несёт и его: `…: повтор (дотяжка, 1 попытка):
{msg[:300]}`. При НЕ-повторе (таймаут/гейты) msg логируется обычным путём как прежде.

**Учёт `_storyboard_active_pending`:** при повторе хендлер делает early `return`
ПЕРЕД декрементом → слот остаётся pending (его закроет повторный тред своим
finished/error). Блок не финиширует пока повтор в полёте.

**Коллизия реестра + keep-alive (критичный инвариант):** повтор регистрируется в
`_active_mode_c_version_threads` под ТЕМ ЖЕ 3-tuple ключом. Безопасно, потому что
**первая строка `_on_mode_c_version_error` — `_retire_thread(pop(key))`** — упавший
тред УЖЕ снят с реестра (ключ свободен) и отправлен в keep-alive
`_threads_pending_delete` (см. секцию Lifecycle). Старый тред дожимает reaper по
`isFinished()`, повтор живёт в основном реестре — **разные контейнеры, reaper
повтор не трогает**, перезаписи живой ссылки нет.

## 404-перезаливка протухшего рефа (2026-06-10)

**Симптом:** FastGen массово отдаёт `404 resource.file_not_found_or_expired`
("Referenced file not found or expired (file:<hash>)") при живых ключах.

**Причина:** реф-картинка заливается в FastGen storage один раз → серверный
`file_hash` оседает в **in-memory `_upload_cache`** ([storyboard_app.py:228](storyboard_app.py:228),
ключ `(resolved_path, mtime_ns)`). Сервер протухает/удаляет blob по storage-TTL, но
кеш продолжает отдавать мёртвый хеш (mtime файла не менялся → перезаливки нет) →
каждый шот с этим рефом ловит 404. До фикса не было ни TTL, ни проверки свежести,
ни re-upload при 404. Хеш — СЕРВЕРНЫЙ id (`data.get("file_hash")`), не md5 контента.

**Механика фикса** ([threads/generate.py](threads/generate.py), только GenerateThread):
- `_is_ref_expired_error(exc)` — детект 404 + тело `file_not_found_or_expired`/
  `resource.file_not_found`. Отдельно от `_classify_key_error` (404 — НЕ вина ключа,
  там так и None → ключ не выбивается).
- `_upload_file(..., force_reupload=False)` — при True пропускает ЧТЕНИЕ кеша (всегда
  POST) и **перезаписывает** запись свежим хешем (= инвалидация мёртвого). Дефолт →
  поведение байт-в-байт. Кеш — lock-free, перезапись идемпотентна под GIL.
- `_reupload_shot_refs(...)` — перезалив рефов шота force + сборка `ref_hashes` в том
  же порядке, что первичная (regen→shot_hashes; edit→[base]+shot; realistic→shot+[base];
  shot по `sorted_tags`).
- В `run()` submit POST обёрнут в `for _submit_attempt in range(2)`: на 404-expired
  (только attempt 0) → чат-уведомление «реф истёк — перезалит, повторяю»
  (`_sa.append_chat_message` — чистый file-I/O в jsonl, UI не трогает → безопасно из
  рабочего потока) + `_reupload_shot_refs` + пересбор `reference_images` (с OpenAI
  ≤2) + retry. **Анти-цикл:** `range(2)` = РОВНО 1 перезаливка; повторный 404 на
  attempt 1 → `raise` в общий `except` как прежняя ошибка версии.

**НЕ затронуто:** `RefGenerateThread._upload` (его cache-key БЕЗ mtime — отдельный
латентный баг, в очереди); poll-цикл; Mode A.

## Lifecycle потоков шотов — keep-alive `_threads_pending_delete` + reaper (2026-06-09)

**Проблема:** `GenerateThread` (regen + Mode C-версии) объявляет **кастомный**
сигнал `finished = pyqtSignal(int)` ([threads/generate.py:142](threads/generate.py:142))
и эмитит его из `run()` ([generate.py:930](threads/generate.py:930)) **ДО возврата
`run()`** — поток в этот момент ещё `running`. Слоты `_on_regen_done` /
`_on_regen_error` / `_on_mode_c_version_finished` / `_on_mode_c_version_error`
ловят сигнал и делают `.pop()` из реестра (`_active_regens` /
`_active_mode_c_version_threads`). Реестр держал **единственную сильную ссылку** →
`.pop()` роняет её, Python GC уничтожает QThread пока `run()` не вышел →
`~QThread()` видит `isRunning()==true` → `qFatal` → `abort()`. Под Mode C
(~30-40 параллельных потоков) гонка ловится почти гарантированно (краши ep25,
ep25 block_9).

**Почему НЕ «коннект на встроенном `QThread.finished`» (как в первом наброске
handoff):** кастомный `finished` **затеняет** встроенный `QThread.finished` —
обратиться к встроенному как `thread.finished` нельзя. Буквальный путь потребовал
бы rename сигнала во ВСЕХ emit/connect-точках двух файлов; любой пропущенный
`.finished.connect` тихо подключился бы к беспараметровому встроенному → рантайм-
`TypeError`. Поэтому выбран keep-alive через `QThread.isFinished()` — та же
гарантия («снимаем объект только ПОСЛЕ возврата `run()`»), но локально в одном
файле, без rename.

**Механизм** ([storyboard_app.py](storyboard_app.py)):
- `_threads_pending_delete: set` + `QTimer _thread_reaper` (1500 мс) заводятся в
  `__init__` рядом с `_shot_gen_started_at`.
- `_retire_thread(t)` — кладёт завершившийся поток в множество (держит ссылку
  ЖИВОЙ, чтобы GC не тронул), заводит reaper если не активен.
- `_reap_finished_threads()` — на каждом тике сметает те, у кого
  `t.isFinished()==True` (а это **только после возврата `run()`**) →
  `t.deleteLater()` (фактическое разрушение отдаётся Qt event-loop'у); когда
  множество пусто — таймер останавливается (не тикает вхолостую).
- 4 слота: `.pop(...)` обёрнуты в `_retire_thread(.pop(...))`. **Логика реестра,
  redraw-предиката и `_shot_gen_started_at` не изменилась** — поток так же
  мгновенно исчезает из реестра (overlay гаснет как прежде); меняется лишь то,
  что объект живёт в keep-alive до `isFinished()`, а не дропается сразу.
- `_collect_all_threads` ([storyboard_app.py](storyboard_app.py)) дренажит и
  `_threads_pending_delete` при закрытии — редкий ещё-бегущий при shutdown поток
  дождётся `.wait()`, не словит SIGABRT.

**НЕ затронуто:** `threads/generate.py` (сигналы/emit без изменений, rename НЕ
делался); `_ref_threads` — это `list`, из которого ссылку НИКОГДА не снимают
(`_on_ref_done`/`_on_ref_error` без `.pop`) → преждевременного GC и краша он не
даёт (лёгкая утечка списка, не lifecycle-баг; при желании чистить тем же reaper'ом
отдельной правкой).

## Троттлинг ключей — ≤25 одновременных запросов на ключ (2026-06-11)

FastGen PRO держит **25 concurrent потоков на ключ** (картинка = 4 кредита,
4000 кредитов/час). Mode C блок 3 шота × 10 версий = **30 параллельных
GenerateThread'ов** на одном ключе пробивал лимит → сервер ронял/тормозил.
Фикс: in-process счётчик живых запросов + acquire/release слотов.

**`key_pool.py` (новое, всё под существующим `_lock`):**
- `MAX_INFLIGHT_PER_KEY = 25` — hardcode-лимит (если FastGen поменяет тариф —
  менять здесь).
- `_inflight: dict {idx: count}` — живые запросы на ключ. idx — в пространстве
  `get_keys()`. **In-process**: живёт в памяти GUI-процесса; CLI-процессы
  (`generate_storyboards.py`/`pipeline.py`) последовательны (1 запрос/процесс)
  и сюда НЕ входят — межпроцессного лимита НЕТ (дорого, не окупается; худший
  случай GUI 25 + штучный CLI-реф = небольшой перелим, безвреден).
- `acquire_slot(max_per_key=25) -> (key, idx, ok)` — берёт слот на **наименее
  загруженном** живом ключе со свободным местом (`_inflight[idx] < max`),
  `_inflight[idx]+=1`. Фильтр живых — ТОТ ЖЕ, что у `next_key_with_idx`
  (manual-off жёстко исключены; среди оставшихся отсев `_read_disabled`, при
  пустоте фолбэк на pool). Tie-break при равной загрузке — round-robin
  `_advance_cursor` (вызывается ВНУТРИ `with _lock`, сам лок не берёт → нет
  реентранси). `_write_active` для лампочки. Возвраты: все на потолке →
  `("", None, False)` (caller ждёт и повторяет); нет ключей/все manual-off →
  `("", None, True)` (ждать бессмысленно); **любая ошибка → fallback
  `(key, None, True)` БЕЗ резерва слота** (kill-switch — троттл отключается,
  генерация не падает).
- `release_slot(idx)` — декремент под `_lock`, не ниже 0, пустой счётчик ключа
  удаляется (карта не растёт). `idx=None` → no-op (kill-switch/фолбэк-ключ).

**`storyboard_app.py`:** обёртки `acquire_api_slot()` / `release_api_slot(idx)`
рядом с `next_api_key()` (ленивый import key_pool, kill-switch на `load_api_key`).
Достижимы из потоков через `_sa.` (см. `_AppProxy`).

**`threads/generate.py` — 4 потока генерации** (GenerateThread, RefGenerateThread,
GenerateActorRefThread, EditActorRefThread): вместо одиночного `next_api_key()` —
**acquire-петля** + `try/finally release_api_slot(_slot_idx)`. Инвариант: один
acquire на `run()`, release в `finally` → счётчик НЕ залипает ни на одном пути
падения (return/except/raise). `_slot_idx=None` объявлен ДО `try` → finally
никогда не `NameError` (стоп до захвата слота → release(None) no-op).
- **GenerateThread** (есть `self._stop`): петля прерывается `self._stop` (как в
  его poll-цикле). Mode C-дотяжка/добор идут через тот же `run()` → троттлятся
  автоматически.
- **3 штучных реф-потока** (нет `_stop`): петля `self.isInterruptionRequested()`
  + cap `_wait >= 150` (≈300с); на исчерпании берут ключ через `next_api_key()`
  **без резерва** (перелимит на 1 для редкого штучного реф-гена безопаснее
  зависшего навсегда потока). На практике слот свободен с первого прохода.
- `GenerateActorRefThread` release вложен в ЕГО существующий `finally`
  (детектив-лог), у остальных трёх — новый `finally`.

**НЕ затронуто:** `next_key()`/`next_key_with_idx()` (контракт CLI сохранён);
failover (`disable_key`/`_read_disabled`); ручной тумблер; CLI-пути;
`ApplyTextureThread` (чистый PIL, ключ не берёт); тело `run()` (submit-retry/
poll/404-перезаливка/сохранение) — byte-for-byte. Коммит `128c0fc`.

## Статусы ключей в Settings + ручной тумблер — Этап 2 UI (2026-06-10)

Визуал состояния каждого из 5 полей ключей в Settings + ручной тумблер
«использовать». **4 состояния, приоритет manual-off > perm > temp > live:**
- **серая точка «отключён вручную»** — ключ выключен юзером (manual-off);
- **красная «не работает — замени»** — perm (401/403/license);
- **жёлтая «лимит, вернётся через N мин»** (countdown) — temp (429);
- **зелёная** — живой. Пустое поле → серая, тумблер disabled.

**Хранение ручного off — отдельный файл `.fastgen_keys_manual_off`, по ТЕКСТУ
ключа** (не idx). Почему так (а не `reason=manual` в `.fastgen_keys_disabled`):
disabled-файл авто-управляемый (TTL-prune temp; `save_keys` его УДАЛЯЕТ) — ручной
off там стёрся бы при «Сохранить». По тексту (не idx) → переживает reorder/
save_keys; stale-записи (ключ удалён) безвредны. `set_manual_off`/`manual_off_keys`
в `key_pool.py`; снимается ТОЛЬКО тумблером (не TTL, не save_keys, не disable_key).

**Фильтр в `next_key_with_idx`:** manual-off — **ЖЁСТКОЕ исключение**, ключ не
выдаётся НИ в каком случае (даже когда все вручную-включённые выбиты failover'ом —
fallback берёт включённые, manual-off вне игры). Все выключены вручную → `("",None)`.
`live_key_count` вычитает и disabled, и manual-off. `disabled_status()` — публичная
read-only обёртка над `_read_disabled` для UI.

**UI (`storyboard_app.py`):** per-row `QCheckBox` + статус-`QLabel`
(`_apikey_toggles`/`_apikey_status_labels`, индекс синхронен с `_apikey_fields`).
`_refresh_key_status_indicators` маппит **поле → текст ключа → idx в `get_keys()`**
(не по позиции поля — устойчиво к пустым/несохранённым полям, без исключений).
`_on_key_toggle` → `set_manual_off`. Обновление: первичный refresh при build +
при открытии вкладки Settings (`_on_main_tab_changed`, idx==2) + `_key_status_timer`
(10с, countdown) — **тикает ТОЛЬКО пока Settings активна** (stop при уходе).

**Фикс конфликта с лампочкой round-robin:** `_blink_key_indicator` через 400мс
сбрасывал индикатор в жёсткий серый — затирало бы статус-цвет. Теперь сброс в
`_key_status_colors[idx]` (текущий статус-цвет). i18n: 4 ключа × 3 языка.

## Failover пула ключей FastGen — Этап 1, ядро (2026-06-09)

Round-robin пул (`key_pool.py`, задача А) до v2026-06-09 крутил ВСЕ ключи без
фильтра — битый/лимитный ключ оставался в ротации, следующий запрос мог снова
его взять. Этап 1 задачи Б добавляет **вывод виновного ключа из ротации**.

**Disable-файл `.fastgen_keys_disabled`** (рядом с `.fastgen_keys_cursor` в
writable project_root; путь в `set_root`). Cross-process: пишет GUI, читают и
GUI, и CLI (`pipeline.py`/`generate_storyboards.py`) синхронно в `next_key()` —
**без watcher** (FSEvents на внешнем томе ненадёжен). Строка на выбитый ключ:
```
<idx> <reason> <until_epoch>
```
- `reason ∈ temp|perm`. **`temp`** (429/лимит): `until_epoch` = АБСОЛЮТНЫЙ
  timestamp возврата (`now + DISABLE_TEMP_TTL`, дефолт 900с=15мин) — несёт точное
  время авто-возврата (и для countdown в UI Этапа 2). **`perm`** (401/403/
  license_expired): `until=0`, возврата нет — до ручного `save_keys`.
- Идентификация ключа по **`idx`** (как лампочка/`last_index`). Безопасно: единств.
  мутатор `fastgen_keys.txt` — `save_keys`, и он чистит disabled-файл → idx
  стабилен между сохранениями.

**Функции `key_pool.py`:**
- `disable_key(idx, reason, ttl_seconds=900)` — merge в файл под `_lock`; perm
  перекрывает temp, повторный temp продлевает `until`; `idx=None`/неверный
  reason → no-op; не кидает (kill-switch — failover НЕ роняет генерацию).
- `_read_disabled() -> {idx:(reason,until)}` — парс + **ленивый TTL-prune**:
  temp с `until<=now` отбрасываются И файл переписывается без них (так ключ
  возвращается в ротацию **без таймер-треда**, cross-process). Малформ-строки
  пропускаются, на ошибке → `{}`.
- `next_key()` фильтр (только ветка >1 ключа): `live = idx не в disabled`; если
  выбиты ВСЕ → `live = все` (graceful fallback, не пустота); курсор крутится по
  `live`. Ветки **0 и 1 ключа — без изменений** (1 ключ отдаётся даже выбитым).
- `save_keys()` — удаляет disabled-файл (ручное обновление ключей снимает ВСЕ
  выбивания, в т.ч. perm).

**Детект на стороне потоков** (`threads/generate.py`): `_classify_key_error(exc)`
рядом с `_http_error_detail` — `429→'temp'`, `401/403→'perm'`, иначе (5xx/таймаут/
сеть/без response) `None` (это сервер, ключ не виноват). Тело на `license_expired`
НЕ парсится — по политике любой 403 = perm. Вызов `disable_key(self._used_key_idx,
kind)` дописан **ПОСЛЕ** существующего `self.error.emit(...)` в 4 потоках
(GenerateThread, RefGenerateThread, GenerateActorRefThread, EditActorRefThread),
каждый под своим `try/except`. `emit`-логика и `_http_error_detail` НЕ изменены.
`self._used_key_idx` уже сохранялся на старте (round-robin лампочка) — переиспользован.

**Границы Этапа 1 (осознанно отложено):**
- **CLI write-side** — `pipeline.py`/`generate_storyboards.py` пока только ЧИТАЮТ
  выбивания (фильтр в `next_key` общий), но сами disable НЕ пишут → **Этап 1b**.
- **Визуал** — цвет индикатора ключа, крестик «недоступен» в Settings, countdown
  «вернётся через ~N мин», i18n → **Этап 2**. Данные (`until`/`reason`) уже есть.
- **Публичный `disabled_status()`** (read-only, без prune для UI-поллинга) → Этап 2.

**Корректная атрибуция idx — `next_key_with_idx()` (2026-06-09, фикс racy-idx):**
До фикса поток узнавал idx своего ключа из ГЛОБАЛЬНОГО `key_pool._last_index` —
`next_key()` писал туда idx, поток сразу читал через `last_index()`
(`self._used_key_idx = _kp.last_index()`). Под Mode C (~30 потоков) глобал
перезаписывался параллельными потоками **между** выдачей ключа и чтением idx →
поток ловил ЧУЖОЙ idx. Это било и по лампочке (мигала не на тот ключ / не мигала:
idx4 отработал 12×, но `.fastgen_keys_active=4` при cursor=60 — лампа молчала), и —
важнее — по **failover**: `disable_key(self._used_key_idx)` мог выбить ЗДОРОВЫЙ
ключ из-за чужой 403. **Фикс (Вариант B):** новая `next_key_with_idx() -> (key, idx)`
отдаёт idx НАПРЯМУЮ — каждый поток получает свой idx в одни руки, без глобала.
`next_key() -> str` стал тонкой обёрткой `next_key_with_idx()[0]` (CLI-контракт
`pipeline.py`/`generate_storyboards.py` цел). GUI-обёртка `next_api_key()`
([storyboard_app.py](storyboard_app.py)) тоже отдаёт `(key, idx)`; 4 потока в
`generate.py` делают `key, self._used_key_idx = _sa.next_api_key()`. Фолбэк
(пустой пул / ошибка диспетчера) → `idx=None`: лампочка молчит (guard),
`disable_key(None)`=no-op (фолбэк-ключ вне idx-пространства пула — выбивать нечего).
`_last_index`/`last_index()` оставлены LEGACY для CLI-лампочки-watcher
(`.fastgen_keys_active` мост), GUI оттуда idx больше не читает.

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

### Default model = `claude-opus-4-8` — [views/new_episode.py:1790](views/new_episode.py:1790)
Сохранённое в `QSettings` по ключу `"new_ep/model_v2"`. Если ключа нет
у юзера — загружается Opus 4.8 как fallback (2026-07-02, был 4.7; старая
ссылка на episode_chat.py:297 была неверной — ключ читается ТОЛЬКО в
`new_episode.py:_current_model`; покрывает манифест ШАГ 1 + свободный
чат эпизода). Ключ никем не пишется с 2026-05-09 (дропдаун убран) —
фактически у всех работает дефолт из кода.

## Camera Lab — вкладка «Камера» (2026-07-02: fal)

`views/camera_lab.py` + `generator/fal_angles_thread.py`. Смена ракурса
кадра через fal-ai/qwen-image-edit-2511-multiple-angles (queue.fal.run:
submit POST → poll status_url → result URL; вход `image_urls` принимает
data:URI base64 — upload-шага нет). Числовые углы уходят КАК ЕСТЬ:
horizontal 0..360° (слайдер шаг 5°), vertical −30..90° (шаг 1°),
zoom 0.0..10.0 (шаг 0.1); квантование в пресеты LoRA — на сервере.

С 2026-07-04 вкладка видна ВСЕМ пользователям (админ-гейт `if self._is_admin`
снят с создания CameraLabView и pill в шапке); H1-заголовок «Камера» в теле
вкладки убран — панели «Исходник»/«Результат» начинаются от верхнего поля.

Ключ fal: `load_fal_key()`/`save_fal_key()` в storyboard_app.py (~:4240,
образец load_api_key): QSettings `fal_api_key` → сайдкар
`<project_root>/fal_key.txt` (в .gitignore). Поле ввода — на самой
вкладке, работает без перезапуска. Баланс живой:
`GET https://rest.alpha.fal.ai/billing/user_balance` (голое число $;
alpha-эндпоинт, при недоступности UI кажет «$ —», генерацию не блокирует).

Орбитальная миникарта (CameraPerspectiveControl): миниатюра кадра ровная
в центре, значок камеры (Lucide 'video') на эллиптической орбите,
пунктир взгляда; drag = углы, колесо = зум. Чистый QPainter, Mac/Win.

Окна «Источник»/«Результат» (2026-07-03): ФИКС-форма, высота АДАПТИВНА от
размера окна (не от аспекта кадра — раньше `heightForWidth(width/aspect)`
раздувал окно на 9:16-кадре, лейаут ехал). `CameraLabView._recalc_media_
windows` (зеркало `_recalc_shot_cards_size` Редактора) на resize/show:
`H = max(_FLOOR_MEDIA_H=150, min(col_w*9/16, доступная_высота_колонки//2))`
— одна H на ОБА окна (всегда равны), ширина обе тянут от колонки (Expanding).
Картинка внутри — contain (KeepAspectRatio, касается 2 краёв, по 2 — поля
фона). `heightForWidth`/`hasHeightForWidth` в `ImageDropSlot` удалены.

Убрано в 2026-07-02: референсы (ReferenceDropArea/CameraRefThumb),
JSON-промпт (~340 строк _build_camera_prompt), выбор моделей
(CameraModelToggle, Nano Banana 2/Flower/OpenAI), путь GeneratorImageThread,
батч ×2 (углы детерминированы — одна генерация на клик). state v2
(camera_lab/state.json): controls в API-единицах, без refs/model; старый
v1-state не мигрируется (дефолты). manifest.json: поле angles{h,v,zoom},
model=fal-модель, provider="fal".

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

**Карта разделов на агента (v1.0.70):**

| Агент | Разделы | Что | Почему |
|---|---|---|---|
| Scriptwriter | [1, 3, 4, 6, 8, 9] | роль + ДНК + тайминг + карта + теги + визуальный приоритет | пишет карту с нуля, всё нужно |
| Validator | [4, 6, 8, 9] | формула + лимиты + теги + визуальный приоритет | проверяет по чек-листу |
| Editor | [3, 4, 6, 8, 9] | ДНК + формула + лимиты + теги + визуальный приоритет | правит карту с учётом ИЕРАРХИИ СЖАТИЯ |
| Context Reviewer | [1, 3, 5, 7, 8, 9, 10, 11, 12] | роль + ДНК + рефы + камера + теги + визуальный приоритет + структура выдачи + чеклист + передаточный пакет | выдаёт финальные Seedance/Storyboard промпты — нужны все продакшен-разделы |

**v1.0.70 (2026-05-14) — расширение карты:**
- Раздел 9 «ПРИНЦИП ВИЗУАЛЬНОГО ПРИОРИТЕТА» добавлен ко ВСЕМ агентам
  монтажа (был не подключён нигде). Закрывает корневой gap «Scriptwriter
  пишет 'белая футболка' → Validator ловит как forbidden_phrase →
  Editor правит» (стоил ~3 мин на эпизод). Раздел крошечный (351 ch),
  эффект большой.
- Context Reviewer расширен с [1, 3] до полного продакшен-набора. До
  v1.0.70 он сверял Bible + характеры, но финальные промпты Seedance
  выдавал «по интуиции Opus» — без знания референсов (5), режиссуры
  камеры (7), синтаксиса тегов (8), структуры выдачи (10), чеклиста
  (11), передаточного пакета (12). Теперь видит весь продакшен-блок
  кроме разделов 2 (правила общения), 4 (тайминг — не его задача), 6
  (монтажная карта — уже готова до него).

**Что НЕ передаётся ни одному агенту:** раздел 2 (правила общения —
для claude.ai чата, не для агентов).

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

## Mode C Scriptwriter — ПОЛНОЕ ПОКРЫТИЕ вместо ПРИОРИТЕТА 3 (2026-06-18, коммит 040cb46)

**Контекст:** баг коллеги (Studio 1.0.98, ASHES WEST ep2) — Scriptwriter в
Mode C выкинул финал гаражной сцены (рюкзак → посадка → выезд Импалы из
гаража). Причина: бездиалоговые «связки-перемещения» подпадали под разрешение
ПРИОРИТЕТ 3 «можно объединять» → схлопывались в монтажный стык. (Mode C
убрал таймкап 60-80с и лимит блоков ещё на уровне Mode B, но лазейка
ПРИОРИТЕТ 3 для шотов без реплик оставалась.)

**Изменено в `agents/montage_rules_c.py` — в ОБЕИХ копиях правил Scriptwriter
(`_FALLBACK_SCRIPTWRITER_SYSTEM` и `_SCRIPTWRITER_JSON_TAIL`):**
- Блок **ПРИОРИТЕТ 3 (МОЖНО ОБЪЕДИНЯТЬ — для шотов без реплик)** УДАЛЁН.
  Вместо него — **ПОЛНОЕ ПОКРЫТИЕ СЦЕНАРИЯ (СЦЕНЫ И Б-РОЛЛЫ)**: каждая сцена /
  действие / б-ролл обязательны в карте; РАЗНЫЕ события сценария = РАЗНЫЕ
  шоты; нарезка ОДНОГО события на планы (крупный/средний/реакция) разрешена;
  объединять РАЗНЫЕ события в один шот — запрещено. ПРИОРИТЕТ 1 и 2 остались
  (они только про то, что НЕ выкидывать).
- Удалена строка «Сценарий обычно содержит 25-35 сцен» (неявный потолок,
  подталкивал Opus к сжатию).
- Висячая ссылка на «ПРИОРИТЕТ 3» в блоке про реплики переформулирована
  (секции ПРИОРИТЕТ 3 больше нет).

**FALLBACK синхронизирован с JSON_TAIL:** до этого `_FALLBACK_SCRIPTWRITER_SYSTEM`
отставал от `_SCRIPTWRITER_JSON_TAIL` на 2 блока правил (коммиты d706352
2026-06-06 «ТОРМОЗ НА ОТСЕБЯТИНУ» и a81ec162 2026-06-08 «АВТОРСКИЙ РАКУРС /
author_camera» легли только в JSON_TAIL). Теперь оба блока + док поля
`author_camera` есть и в FALLBACK. Активный путь — JSON_TAIL (см. `_build_lazy`
выше); FALLBACK работает только если bundled `.md` не загрузился.

**Согласованность:** новое «не объединять РАЗНЫЕ события» НЕ конфликтует с
исключением ТОРМОЗА «объединение и нарезка существующих сценарных сцен на
шоты» — нарезка ОДНОГО события на планы разрешена обоими, запрет только на
склейку РАЗНЫХ событий.

**Затронут ТОЛЬКО Mode C.** Mode A (`montage_rules.py`, md5-эталон 450fb204)
сохраняет старую ИЕРАРХИЮ СЖАТИЯ с ПРИОРИТЕТОМ 3 без изменений. Mode B (`_b`)
и Mode D (= клон A) не трогались. Проверка: `montage_rules_c.py` md5 63c93ac6,
ast.parse/py_compile OK.

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
работает ЛИНЕЙНО. С **v1.0.75 (2026-05-14)** добавлена стадия 2.5 —
Geometry Editor. С **v1.0.76** — стадия 3.5 Validator R2.
С **v1.0.77 (2026-05-14)** — стадии 3.6 Editor R2 + 3.7 Validator R3
для второго раунда правок:

```
1. Scriptwriter (Opus 4.7)         — пишет монтажную карту с нуля
2. Validator R1 (Haiku 4.5)        — проверяет один раз  (v1.0.72)
2.5. Geometry Editor (Haiku 4.5)   — ТОЛЬКО если есть missing_geometry-ошибки;
                                     добавляет shot.geometry. Failed → fallback в Editor.  (v1.0.75)
3. Editor R1   (Opus 4.7)          — если остались ошибки кроме geometry,
                                     применяет правки  (v1.0.76: переход с Sonnet)
3.5. Validator R2 (Haiku 4.5)      — ТОЛЬКО если Editor R1 реально отработал;
                                     проверяет результат, считает реальное
                                     остаточное количество ошибок.  (v1.0.76)
3.6. Editor R2 (Opus 4.7)          — ТОЛЬКО если Validator R2 успешен И
                                     остались errors > 0. Без geometry-split.  (v1.0.77)
3.7. Validator R3 (Haiku 4.5)      — ТОЛЬКО если Editor R2 реально отработал;
                                     финальная цифра остатка.  (v1.0.77)
                                     Editor R3 НЕ запускаем (стоп после R3).
4. Context Reviewer (Sonnet 4.6)   — ОПЦИОНАЛЬНО (toggle в Settings, default OFF)
   └─ если concerns > 0 → Editor ещё раз (без Validator R4)
5. ФИНАЛ — checker_report = R3/R2/R1 (последний успешный по убыванию)
```

**v1.0.77 split logic для Editor R2 + Validator R3 в `run()`:**
```python
validator_r2_ok = False
if editor_ran:
    try:
        r2_report = _call_validator(montage_card, round_num=2)
        checker_report = r2_report
        validator_r2_ok = True
    except Exception:
        log({'stage':'validator_r2','error':...})

editor_r2_ran = False
if validator_r2_ok and not checker_report.ok and checker_report.errors > 0:
    try:
        montage_card = _call_editor(montage_card, r2_errors, round_num=2)
        editor_r2_ran = True
    except Exception:
        log({'stage':'editor_r2','error':...})

if editor_r2_ran:
    try:
        r3_report = _call_validator(montage_card, round_num=3)
        checker_report = r3_report
    except Exception:
        log({'stage':'validator_r3','error':...})
```

**v1.0.77 UI: симметричное set-сравнение R2 vs R3** (по той же логике
что R1 vs R2 в v1.0.76):
- `resolved = R2 \ R3` — реально исправлено Editor R2
- `unresolved = R2 ∩ R3` — Editor R2 не справился
- `new = R3 \ R2` — Editor R2 создал при правке

Дополнительные UI строки (после Editor R1 строк):
- `✏ Редактор R2 — исправил все {Y2} оставшихся ошибок ✓`
- `✏ Редактор R2 — исправил {res2} из {Y2} оставшихся ошибок`
- `⚠ Editor R2 создал {new2} новых ошибок при правке`
- `⚠ Редактор R2 УПАЛ — ...` (editor_r2.failed)
- `⚠ Не удалось проверить результат Editor R2 — Чекер R3 ...` (validator_r3.failed)

Editor R2 запускается **только** при:
- `validator_r2.ran == True AND not validator_r2.failed`
- `checker_report.errors > 0`
- (Editor R1 успешно — следует из validator_r2_ok)

**v1.0.78 (2026-05-14) — UI косметика после v1.0.75-v1.0.77 (5 багов):**

1. **`{errors_word}` плейсхолдер** не подставлялся в progress-bar: [widgets/montage_cta.py:126](widgets/montage_cta.py:126) `show_running` вызывал `tr(status_key)` без kwargs, потом manual replace только `{errors_count}`. Auto-inject `errors_word` через `plural_errors()` в [i18n.py:1571-1573](i18n.py:1571) не срабатывал. Fix: `text = tr(status_key, **fmt)`.

2. **Устаревший текст «Финальная проверка пропущена для скорости»** в `montage_status_round_done_errors` (i18n.py × 3 языка) — текст из v1.0.62, после v1.0.76+v1.0.77 Validator R2/R3 всегда запускаются. Укорочено до «⚠ Чекер: {errors_count} {errors_word}».

3. **`_STAGE_DISPLAY` + `_STAGE_ORDER`** в [widgets/montage_summary_dialog.py:437](widgets/montage_summary_dialog.py:437) расширены с 4 до 8 стадий (scriptwriter, validator R1, geometry_editor, editor R1, validator_r2, editor_r2, validator_r3, context_reviewer). До v1.0.78 таблица «ТАЙМИНГ ГЕНЕРАЦИИ» пропускала Geometry Editor / Validator R2/R3 / Editor R2 — расхождение ИТОГО vs сумма видимых строк.

4. **Пересчёт `total_seconds`** в [widgets/montage_summary_dialog.py:87](widgets/montage_summary_dialog.py:87) — раньше брали `montage_card.get('total_seconds')` от Scriptwriter, Editor правил длительности шотов но поле не пересчитывал. Теперь `total_seconds = sum(sum(s.duration_sec for s in shots) for b in blocks)` — игнорируем поле, считаем по факту. Расхождение «63с заголовок vs 66с блоки» закрыто.

5. **Прогресс-handler для 4 новых stages** добавлен в `_on_montage_progress` ([views/episode_chat.py:599 + 3256](views/episode_chat.py:599)) для обоих call-sites + 4 i18n ключа × 3 языка:
- `montage_status_geometry_editor` (для v1.0.75)
- `montage_status_validator_r2` (для v1.0.76)
- `montage_status_editor_r2` (для v1.0.77)
- `montage_status_validator_r3` (для v1.0.77)
До v1.0.78 эти stages фолбэчили на «Сценарист пишет монтажную карту» (лживый UI).

**v1.0.79 (2026-05-14) — усиление инварианта «пересчитай duration после правки реплики» в EDITOR_SYSTEM:**

На ep2 v1.0.78 Editor R1 переписал реплику Mark с 12 на 20 слов
не пересчитав duration_sec — Validator R2 поймал новую ошибку
`dialog_too_short_for_words`. Корень: правило про пересчёт duration
в `_EDITOR_JSON_TAIL` было зарыто в подсекции «КАК ДОБИРАТЬ ДО 60-80с»
и сформулировано реактивно («если коротко — увеличь»), без статуса
инварианта.

Усиление в [agents/montage_prompts.py:889 + 1191](agents/montage_prompts.py:889):
- **`_EDITOR_ROLE`** (350 → 727 ch): добавлено напоминание «при ЛЮБОМ изменении текста реплики ОБЯЗАТЕЛЬНО пересчитай duration_sec» + указание «самая частая ошибка Editor'а».
- **`_EDITOR_JSON_TAIL`** (4096 → 5595 ch): в самое начало (перед «ГЛАВНЫЙ ПРИНЦИП ПРАВКИ») добавлен блок «КРИТИЧЕСКИЙ ИНВАРИАНТ — ТАЙМИНГ РЕПЛИК» с двойными `═` рамками. Содержит формулу, таблицы скорости/запаса, жёсткое правило `duration_sec ≥ минимума`, конкретный анти-пример с репликой Mark «Don't worry. I've taken care of everything.» (формула 20÷2.75 + 1.5 = 8.77), два варианта правильной правки, и итоговое требование самопроверки таймингов ВСЕХ реплик в карте после правки.

EDITOR_SYSTEM 12 861 → **14 737 ch** (+14.6%). Сравнимо с SCRIPTWRITER_SYSTEM (14 738 ch).

Метрика успеха: на следующих прогонах ep2 показатель `new_count` (Editor R1 создал новых ошибок при правке, виден в UI Editor-строке после v1.0.76 set-сравнения) должен упасть с 4 (на ep2 v1.0.78) до 0-1. Если не упадёт — переход к Python post-check автоматическому подъёму duration_sec до минимума.

**v1.0.80 (2026-05-14) — два ещё КРИТИЧЕСКИХ ИНВАРИАНТА в Editor + отключение rule_7a в Validator:**

На ep2 v1.0.79 инвариант про duration (v1.0.79) сработал — 0 новых `dialog_too_short_for_words`. Но Editor R2 создал 4 ошибки **других** категорий: 2× missing_geometry (удалил поле geometry), 1× invalid_location_mixing (объединил шоты разных локаций), 1× missing_voice_profiles (косяк Validator R2 — AI-галлюцинация). Подход с КРИТИЧЕСКИМ ИНВАРИАНТОМ работает — расширяется.

Добавлено в `_EDITOR_JSON_TAIL` ([agents/montage_prompts.py:1191](agents/montage_prompts.py:1191)):
- **КРИТИЧЕСКИЙ ИНВАРИАНТ — ГЕОМЕТРИЯ ПЕРСОНАЖЕЙ** (~700 ch): запрет удалять/искажать `geometry` при правке других ошибок. Анти-пример с missing_geometry на ep2.
- **КРИТИЧЕСКИЙ ИНВАРИАНТ — ЛОКАЦИИ БЛОКОВ** (~750 ch): правило «один блок = одна локация». Запрет объединять шоты разных локаций при сжатии. Анти-пример с Блоком 4 на ep2 (улица + дорожка + лестница + коридор).

Обновлено `_EDITOR_ROLE` напоминание: теперь упоминает все 3 инварианта (тайминг + геометрия + локации).

EDITOR_SYSTEM 14 737 → **16 381 ch** (+1 644, +11.2%).

**rule_7a в VALIDATOR_SYSTEM ОТКЛЮЧЕНО** (обе константы `_VALIDATOR_JSON_TAIL` + `_FALLBACK_VALIDATOR_SYSTEM` симметрично): содержимое между маркерами `<!-- BEGIN rule_7a -->` / `<!-- END rule_7a -->` заменено на 4-строчный placeholder. Причина: voice profiles файл (`shows/<slug>/voices.txt`, per-show — мигрировано с глобального `instructions/`-файла в 2026-05-20) **не передаётся в Validator в текущем оркестраторе** — он используется только в Seedance pipeline ([threads/seedance_pipeline.py:86](threads/seedance_pipeline.py:86)). Без профилей AI Validator стабильно галлюцинировал собственный код `missing_voice_profiles`, который не описан ни в rule_7a, ни в Python pre-filter, ни в Editor. Маркеры BEGIN/END сохранены — skip-механика v1.0.69 продолжает работать, правило вернём когда профили подключим к montage_orchestrator.

VALIDATOR_SYSTEM full: 15 484 → **14 618 ch** (-866). Stripped (после Python pre-filter): 13 484 → **12 618 ch** (-866).

Метрика успеха v1.0.80: на ep2 показатели `missing_geometry`, `invalid_location_mixing`, `missing_voice_profiles` в Validator R2/R3 должны быть = 0. Общий `new_count` (Editor создал новых) — упасть с 4 до 0-1.

**v1.0.81 (2026-05-14) — Python post-check таймингов после Editor (вторая линия защиты):**

Несмотря на «КРИТИЧЕСКИЙ ИНВАРИАНТ — ТАЙМИНГ РЕПЛИК» из v1.0.79, Editor R1/R2 продолжали создавать `dialog_too_short_for_words` ошибки на ep2. Промпт-фикс работал частично — Opus иногда забывал правило. Решение: гарантированный Python post-check в коде, не уговариваем AI.

Новый модуль [agents/timing_post_check.py](agents/timing_post_check.py):
- `SPEED_MAP`: fast=3.0, normal=2.75, emotional=2.25, slow=1.75 (слова/сек)
- `min_duration_sec(words, speech_type)` → `math.ceil(words/speed + reserve)`
- `apply_timing_post_check(card)` — in-place поправка `duration_sec` шотов с репликой если меньше минимума + пересчёт `card['total_seconds']`. Возвращает (card, summary).

Врезка в [threads/montage_orchestrator.py:251 + 316](threads/montage_orchestrator.py:251): новый метод `_apply_post_check_timings(card, round_num)` вызывается сразу ПОСЛЕ успешного `_call_editor` R1 и R2, ДО Validator R2/R3 соответственно. Логируется в `_agent_log` как stage `post_check_timings_r{round_num}` со всеми метаданными (shots_checked, shots_fixed, fixes[], delta_total_seconds).

Pipeline (полный после v1.0.81):
```
1. Scriptwriter (Opus 4.7)
2. Validator R1 (Haiku 4.5)
2.5. Geometry Editor (Haiku 4.5)
3. Editor R1 (Opus 4.7)
3.1. Post-check таймингов R1 (Python, ~10мс)     ← v1.0.81
3.5. Validator R2 (Haiku 4.5)
3.6. Editor R2 (Opus 4.7)
3.6.1. Post-check таймингов R2 (Python, ~10мс)   ← v1.0.81
3.7. Validator R3 (Haiku 4.5)
4. Context Reviewer (Sonnet 4.6, опц.)
5. ФИНАЛ
```

Две линии защиты:
1. EDITOR_SYSTEM КРИТИЧЕСКИЙ ИНВАРИАНТ (v1.0.79) — Opus сам пересчитывает duration при правке реплик
2. Python post-check (v1.0.81) — гарантированно поднимает duration до min если AI пропустил

UI ([widgets/montage_summary_dialog.py](widgets/montage_summary_dialog.py)):
- `_STAGE_DISPLAY` + `_STAGE_ORDER` расширены 2 стадиями (R1, R2). Таблица таймингов показывает «Post-check timings R1/R2: < 1 сек».
- `_build_agent_lines` — 2 новые строки в попапе после Editor R1/R2: «🔧 Post-check таймингов R1 — поправил X шотов (+Y сек)». Показывается только если shots_fixed > 0.

Post-check НЕ обрабатывает Editor-after-Reviewer (CR concerns > 0 → ещё Editor) — Bug 6 в очереди. Post-check не fix'ит блок-overflow (>15с) или total-overflow (>80с) — Validator R2/R3 поймает через Python pre-filter.

Cross-platform: чистый Python + math.ceil + dict-логика. Никаких subprocess/Path/open.

**v1.0.82 (2026-05-14) — персистентность монтажной карты + CTA «📂 Открыть монтажную карту»:**

Раньше попап монтажки выскакивал автоматически после `_on_montage_finished_ok`, при крестике карта терялась из памяти (через `_pending_montage_results.pop()` + удаление локального dlg-объекта). На диске оставался диагностический `_agent_log_epN.json`, но Studio его не читала. При параллельной работе с 4-5 эпизодами попапы лезли отовсюду — путаница.

**Новое поведение:**
- Карта сохраняется на диск автоматически в `episodes.json[ep]['montage_card']` (полная карта со всеми полями: blocks с duration_sec, dialog, scene_action, geometry, total_seconds).
- Также сохраняются `montage_checker_report`, `montage_agent_summary`, `montage_rounds_used` — для восстановления попапа в точности как был.
- Попап **НЕ выскакивает** автоматически. Вместо этого в чате эпизода CTA переключается в state `KIND_OPEN_MAP` — кнопка «📂 Открыть монтажную карту» (зелёный tint).
- Юзер кликает по «Открыть» → читает карту с диска → попап открывается. Крестик просто закрывает окно без warnings.
- Карта переживает перезапуск Studio: при возврате на эпизод `_check_montage_ready` детектит карту на диске → CTA сразу show_open_map.

**Хранение (episodes.json[ep_id]):**
- `montage_card` — НОВОЕ поле, полная карта. Source of truth для повторного открытия.
- `montage_checker_report`, `montage_agent_summary`, `montage_rounds_used` — НОВЫЕ поля, восстанавливают full popup state.
- `blocks` — старое поле (урезанный формат `{n, name, shots[n]=description_ru}`), пишется ТОЛЬКО при клике «🎨 Делать сториборды» — для StoryboardPipeline.

**Fallback на `_agent_log_epN.json`:** для эпизодов сгенерированных до v1.0.82 (когда `montage_card` ещё не записывался) — `_load_full_montage_card` делает reverse-search последней stage с `result.blocks` в логе. Без миграции, прозрачно.

**Удаление карты:** кнопка «🗑 Удалить монтажную карту» в попапе (новый сигнал `delete_card` из MontageSummaryDialog). При клике — QMessageBox подтверждение + проверка `_is_storyboard_or_seedance_running()` (защита от race). После подтверждения — `_delete_full_montage_card` снимает 5 полей (`montage_card`, `montage_checker_report`, `montage_agent_summary`, `montage_rounds_used`, `blocks`) из `episodes.json[ep]`. НЕ трогает `_agent_log_epN.json`, `output/seedance/*`, `output/storyboards/*`. CTA возвращается к «Сделать монтажную карту».

**Блокировка «🗑 Удалить» при активном пайплайне:** `dlg.set_delete_enabled(False, tr('montage_delete_blocked_pipeline'))` если `_storyboard_pipeline_thread.isRunning()` или `_seedance_pipeline_thread.isRunning()`. Tooltip объясняет почему.

**Файлы:** `views/episode_chat.py` (~200 строк новых методов + правки 3 handler'ов), `widgets/montage_summary_dialog.py` (новая кнопка + сигнал, удалена reject-warning логика), `widgets/montage_cta.py` (KIND_OPEN_MAP + show_open_map), `i18n.py` (9 новых ключей × 3 языка).

**Cross-platform:** все file-IO через `pathlib.Path.read_text() / write_text() / exists()`. Никаких `subprocess` / `shell`.

**v1.0.76 split logic в `run()` для Validator R2:**
```python
editor_ran = False
if editor try-block succeeded:
    editor_ran = True

if editor_ran:
    try:
        r2_report = _call_validator(montage_card, round_num=2)
        checker_report = r2_report      # ← Studio видит реальное состояние
    except Exception:
        log({'stage':'validator_r2','error':...})
        # checker_report остаётся от R1, UI пометит validator_r2.failed=True
```

**v1.0.76 UI: set-сравнение R1 vs R2** по ключу `(code, where)`:
- `resolved = R1 \ R2` — реально исправлено Editor'ом
- `unresolved = R1 ∩ R2` — Editor не справился
- `new = R2 \ R1` — Editor создал при правке

UI строки (в `_build_agent_lines`):
- `✏ Редактор — исправил все {Y} ошибок ✓` (resolved == Y, new == 0)
- `✏ Редактор — исправил {resolved_count} из {Y} ошибок` (partial)
- `⚠ Editor создал {new_count} новых ошибок при правке` (вторая строка если new > 0)
- `⚠ Не удалось проверить результат Editor — Чекер R2 ...` (validator_r2.failed)

**Почему Editor → Opus 4.7 (v1.0.76):** на ep2 v1.0.75 Sonnet 4.6
«отрапортовал» что исправил 5 ошибок (`errors_in=5`), но Validator R2
(добавленный в v1.0.76 для диагностики) показал что все 5 фактически
остались в карте. Симптом: Sonnet пропускает при многозадачной правке
(timing math + forbidden_phrase одновременно). Opus умнее → реже
промахивается. Цена +1 мин (~3-4 мин Editor вместо 2:49).

**v1.0.75 split logic в `run()`:**
```python
geometry_errors = [e for e in errors if e['code'].endswith('_missing_geometry')]
other_errors    = [e for e in errors if not ...]

if geometry_errors:
    try:
        montage_card = _call_geometry_editor(montage_card, geometry_errors)
        editor_input_errors = other_errors           # успех — Editor легче
    except Exception:
        log({'stage':'geometry_editor','error':...})
        editor_input_errors = all_errors             # fallback Q1=B
        # UI покажет «⚠ Geometry Editor УПАЛ»

if other_errors / fallback_all:
    _call_editor(montage_card, editor_input_errors)
```

**Обоснование (анализ ep2 v1.0.74):** Editor получил 8 ошибок (3×
missing_geometry + 5 других), упёрся в timeout 600s. Каждая
missing_geometry требует от Sonnet генерации ~300 ch строки ГЕОМЕТРИЯ
из ничего — это не правка, а создание. Haiku 4.5 на такой структурной
задаче в 4× быстрее Sonnet + достаточно качественен (composition не
требует Bible / характеров — только координаты от scene_action).

**Размер `get_geometry_editor_system()`:** ~3.4 KB (vs EDITOR_SYSTEM 12.9 KB,
в 3.8× меньше). Состав:
- `_GEOMETRY_EDITOR_ROLE` (~250 ch)
- `load_subsection(6, "ПРОСТРАНСТВЕННАЯ ГЕОМЕТРИЯ СЦЕНЫ")` — текст
  подсекции из ГИ напрямую (~1800 ch). НОВЫЙ механизм извлечения
  подзаголовков `### ` внутри раздела `## N` — `extract_md_subsection`
  + `load_subsection` в `instruction_loader.py`.
- `_GEOMETRY_EDITOR_JSON_TAIL` (~1300 ch) — пошаговая инструкция
  «найди шот по where → возьми characters + scene_action → сгенерируй
  geometry → запиши в shot.geometry. НЕ меняй другие поля.»

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

**v1.0.73 (2026-05-14) — fix rule_9 ложных срабатываний на микромимику:**

После v1.0.72 (Haiku Validator) на ep2 нашлось 16 forbidden_phrase
ошибок, из которых **13 — ложные** на корректную микромимику:
«Eyes flick quickly left and right», «Pupils slightly constricted»,
«Brows pull inward and up at the center», «Tears well at the lower
lash line», «Muscles around her eyes contract». Это всё антитеатральная
микромимика, требуемая разделом 3 ГИ.

Корень: rule_9 в `_VALIDATOR_JSON_TAIL` содержал противоречие — в
списке запретов «описания глаз/бровей/тела/лица», а в категориальном
правиле в конце «Эмоция должна передаваться через описание физиологии
(мышцы лица, глаза, дыхание, челюсть, кадык, поза)». Модель ловила
первый параграф и ставила forbidden_phrase на любое упоминание частей
лица.

Решение: rule_9 переписан с **явным разделением макро vs микро мимики**
+ **whitelist микромимики** + явные категории нарушений. 4 категории
нарушений:
- А) ВЗГЛЯД В КАМЕРУ (4th wall)
- Б) МАКРО-МИМИКА (eyes wide / face contorted / jaw drops — ярлык эмоции на всё лицо)
- В) ЭМОЦИОНАЛЬНЫЕ ЯРЛЫКИ (panic/fear/rage/... и синонимы)
- Г) ОДЕЖДА / ИНТЕРЬЕР / МАТЕРИАЛЫ СЛОВАМИ (white t-shirt / wooden table / silk)

Whitelist (НЕ forbidden_phrase): глаза/зрачки/веки/ресницы, брови/лоб/
щёки/скулы, губы/рот/челюсть/подбородок/кадык, дыхание, любые мышцы
лица, шея, плечи, поза, руки без указания одежды.

Маркеры `<!-- BEGIN rule_9 -->` / `<!-- END rule_9 -->` не тронуты —
skip-механика v1.0.69 продолжает работать симметрично. Правка
применена синхронно в `_VALIDATOR_JSON_TAIL` и `_FALLBACK_VALIDATOR_SYSTEM`.

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

**v1.0.69 (2026-05-14) — Python pre-filter Validator'а:**

10 из 14 правил Validator'а вынесены из AI в Python — детерминированные
проверки, на которых Sonnet 4.6 тратил chain-of-thought reasoning впустую
(ep2 v1.0.68: Validator 7:40 при output всего 4 KB = 9 ch/sec на той же
модели, где Editor выдаёт 137 ch/sec).

Python проверяет (`agents/validator_prefilter.py`):
- #1 too_many_shots (≤4)
- #2 over_15s (сумма duration блока)
- #3 total_out_of_range (60–80с — пересчитывается через
  `sum(shots.duration_sec)`; поле `card['total_seconds']` не доверяем)
- #4 shot_numbering (1..N подряд)
- #5 dialog_missing_lang (ru + en оба непустые)
- #7 invalid_speech_type (enum)
- #8 speaker_not_in_characters (slug ∈ block.characters)
- #10 unknown_location (slug ∈ refs.locations)
- #11 unknown_character (slug ∈ refs.characters)
- #13 too_many_blocks (>7)

AI остаётся:
- #6 timing math (слова × скорость + запас)
- #7а speech_type vs voice profile
- #9 forbidden_phrase (семантика + синонимы)
- #12 artificially_long (растяжка >5с при простом действии)
- #14 visually_duplicate_shots (попарная семантика)

**Защита от рассинхрона:** каждая `_check_*` функция в
`validator_prefilter.py` содержит цитату соответствующего пункта из
`_VALIDATOR_JSON_TAIL` в docstring. При правке текста правила в
.md/промпте — синхронно править docstring.

**Skip-механика для AI Validator'а:** все 14 правил в
`_VALIDATOR_JSON_TAIL` и `_FALLBACK_VALIDATOR_SYSTEM` обёрнуты парными
маркерами `<!-- BEGIN rule_N --> ... <!-- END rule_N -->`. Новая
функция `get_validator_system(skip_rules: Set[str])` режет блоки между
маркерами по regex с backreference. После prefilter передаём
`rules_done` в `get_validator_system` → AI получает system на 14%
меньше (14025 → 12025 ch) и не видит уже проверенных правил.

**Объединение ошибок:** в `_call_validator`
[threads/montage_orchestrator.py](threads/montage_orchestrator.py):
```python
py_errors, rules_done = prefilter_check(card, refs)
validator_system = get_validator_system(skip_rules=rules_done)
ai_report = self._run_claude(validator_system, user, ...)
merged_errors = py_errors + ai_report.get('errors', [])
```
Editor читает merged_errors без отличий (формат `{code, where, details}`
идентичен AI Validator'у). В `_agent_log[validator]` добавлены поля
`prefilter_errors`, `prefilter_rules_done`, `validator_system_chars`.

**v1.0.71 (2026-05-14) — honest UI при exception в Validator (Bug 1 fix):**

Обнаружено на ep2: Validator упал по `subprocess.TimeoutExpired` через
600s, но попап показал «🔍 Чекер: 0 ошибок» — silent failure.

Корень: в `run()` при exception в `_call_validator` orchestrator пишет
`{stage: 'validator', error: ...}` в `_agent_log` (без `result`/
`duration_sec`), делает `_finalize` с инициализированным пустым
`checker_report = {"ok": False, "errors": [], "report": []}` и `return`.
`_build_agent_summary` на error-stage делал `res = {}` → `errors_count
= 0`, `runs = 1` → UI попадал в else-ветку «нашёл 0 ошибок».

**Минимальный фикс (поведение оркестратора НЕ менялось):**
- `_build_agent_summary` ([threads/montage_orchestrator.py](threads/montage_orchestrator.py)): при `s.get('error') and not s.get('result')` в `rounds_passed[-1]` пишет дополнительные поля `failed: True`, `error: str(error_msg)`.
- `_build_agent_lines` ([widgets/montage_summary_dialog.py](widgets/montage_summary_dialog.py)): в ветке Чекера ДО проверки `last.get('ok')` стоит `if last.get('failed'):` — выводит «⚠ Чекер УПАЛ — <причина>. Карта НЕ ПРОВЕРЕНА — возможны нарушения правил, которые остались незамеченными.». TimeoutExpired распознаётся подстрокой `timed out / timeout` → «превысил лимит времени (10 минут)». Для прочих exception — первая строка `str(error)` до 120 ch.
- Локальный флаг `validator_failed` в `_build_agent_lines` — при `ed_runs=0` И `validator_failed` Editor-строка пишет «не запускался (Чекер упал, входа нет)» вместо ложного «нечего было править».

**Поведение оркестратора при exception в Validator (СОХРАНЕНО из v1.0.62):**
Карта Scriptwriter всё равно отдаётся в `_finalize` (не fatal). Это
было сделано, чтобы не блокировать юзера при сбое AI — но без честного
UI юзер не знал, что валидация не прошла. Теперь знает.

**v1.0.74 (2026-05-14) — honest UI распространён на Editor и Context Reviewer (Bug 4 fix):**

На прогоне ep2 v1.0.73 Validator (Haiku) отработал 1:50, но Editor
(Sonnet 4.6) упёрся в timeout 600s. UI показал «✏ Редактор — поправил
0 ошибок за 1 раунд(ов)» — клон Bug 1 для Editor. v1.0.71 fix
покрывал только validator-ветку, editor + context_reviewer ветки
остались с прежним багом.

**Симметричный fix по тому же паттерну ([threads/montage_orchestrator.py](threads/montage_orchestrator.py) `_build_agent_summary`):**
- editor branch: при `s.get('error') and not s.get('result')` в
  `rounds[-1]` пишет `failed: True` + `error: str(error_msg)`.
- context_reviewer branch: при exception ставит `ran: True`, `ok:
  False` (а не `True` по default'у), `failed: True`, `error`.
  Без этого UI показывал «🎯 Финальный редактор — прошёл 0 проверок,
  противоречий нет» при реальном таймауте.

**UI ([widgets/montage_summary_dialog.py](widgets/montage_summary_dialog.py)):**
- Editor: новый `ed_failed_round` ловит failed-round ПЕРЕД позитивной
  веткой. Сообщение «⚠ Редактор УПАЛ — <причина>. Часть ошибок осталась
  без правок — карта отдана в состоянии до Editor'а».
- Context Reviewer: новая ветка `if cr.get('failed'):` ПЕРЕД старой
  `if cr.get('ok') and not concerns:`. Сообщение «⚠ Финальный редактор
  УПАЛ — <причина>. Bible-сверка не выполнена».
- TimeoutExpired распознаётся подстрокой `timed out / timeout` → текст
  «превысил лимит времени (10 минут)». Для прочих exception — первая
  строка `str(error)` до 120 ch.

**Поведение оркестратора при exception в Editor/Reviewer (СОХРАНЕНО):**
- Editor exception → `_finalize(montage_card, checker_report)` (карта
  до Editor'а, не fatal).
- Reviewer exception → то же.
- Editor-after-Reviewer exception → то же.

**Открытые баги (отдельные задачи):**
- Bug 2: при exception в `_call_validator` `prefilter_check` уже отработал, но его `py_errors` теряются — exception летит из `_run_claude` до merge. Если Python pre-filter найдёт реальные ошибки — они не дойдут до Editor.
- Bug 5: root cause скорости Editor (клон Bug 3 для Editor). На ep2 v1.0.73 Editor получил 8 ошибок (vs прошлые 5) и не успел уложиться в 600s. Варианты: увеличить timeout / переключить Editor на Haiku 4.5 (риск creative качества) / разбить Editor на 2-3 раунда по 3-4 ошибки / специализированный сабагент на missing_geometry.
- Bug 6: `editor_after_reviewer` stage не имеет ветки в `_build_agent_summary` — его статистика теряется при успешном выполнении, при exception не показывается в UI.

## Per-agent model routing

| Агент | Слушает дропдаун? | Где |
|-------|--------------------|-----|
| Montage (монтажная карта) | ДА | [views/episode_chat.py:2492](views/episode_chat.py:2492) `_current_model()` |
| StoryboardWriter | ДА | [views/episode_chat.py:2749](views/episode_chat.py:2749) `_current_model()` |
| Seedance pipeline | НЕТ — hardcode Opus 4.7 | [views/episode_chat.py:2836](views/episode_chat.py:2836) |

Дропдаун в шапке: Sonnet 4.6 / Opus 4.7 / Haiku 4.5
([views/episode_chat.py:289-292](views/episode_chat.py:289)).

### Per-stage модели монтажного пайплайна (hardcoded в MontageOrchestratorThread)

В [threads/montage_orchestrator.py](threads/montage_orchestrator.py) каждая стадия монтажа имеет жёстко зашитую модель (юзерский дропдаун эти hardcode НЕ переопределяет — модель выбирается под задачу):

| Стадия | Модель (v1.0.76) | Где | Почему |
|---|---|---|---|
| Scriptwriter | claude-opus-4-7 | [montage_orchestrator.py:92](threads/montage_orchestrator.py:92) | творческая генерация карты с нуля, нужен Opus |
| Validator R1 / R2 | claude-haiku-4-5 | [montage_orchestrator.py:93](threads/montage_orchestrator.py:93) | проверка по чек-листу после Python pre-filter — механики хватит Haiku, в 4× быстрее Sonnet (v1.0.72). R2 (v1.0.76) запускается после Editor если editor_ran=True для проверки реального результата |
| Geometry Editor | claude-haiku-4-5 | [montage_orchestrator.py:107](threads/montage_orchestrator.py:107) | узкая структурная правка — добавление `shot.geometry` к missing_geometry-ошибкам, Haiku справляется (v1.0.75) |
| **Editor** | **claude-opus-4-7** | [montage_orchestrator.py:94](threads/montage_orchestrator.py:94) | **v1.0.76: переход с Sonnet 4.6 на Opus 4.7** — Sonnet «отрапортовал» что исправил 5 ошибок на ep2 v1.0.75, но Validator R2 показал что фактически остались все 5. Opus умнее при многозадачной правке |
| Context Reviewer | claude-sonnet-4-6 | [montage_orchestrator.py:95](threads/montage_orchestrator.py:95) | сверка с Bible + продакшен-разделами 5/7/8/9/10/11/12 (v1.0.70) |

**v1.0.72 (2026-05-14) — диагностика Sonnet vs Haiku на Validator:**

| Тест | Время | Output | Speed | Ошибок |
|---|---:|---:|---:|---:|
| Studio (последний прогон ep2) | timeout 600s | — | — | — |
| Ручной Sonnet 4.6 (тот же prompt) | 4:39.7 (279.7s) | 3 393 ch | 12.1 ch/sec | 2 |
| **Ручной Haiku 4.5 (тот же prompt)** | **2:04.5** (124.5s) | 6 404 ch | **51.4 ch/sec** | 1 |

Оба нашли главную математическую ошибку (`block_1_shot_2_dialog_too_short_for_words` — slow speech_type, 16 слов EN, реальный минимум 9.5–10.6с против 8с в карте). Haiku в 2.25× быстрее Sonnet на ручном тесте + 5× запас до 600s потолка в Studio.

**Не объяснённое расхождение:** Sonnet ручной 4:40 vs Sonnet в Studio >8:00. Возможные источники — прокси/network jitter (юзер выключал прокси), cold-start subprocess, retry внутри claude CLI. Не критично — переключение на Haiku убирает обе нестабильности (медленность + расхождение).

**Откат при регрессии качества:** `git revert <commit-v1.0.72>` — вернёт `MODEL_VALIDATOR = "claude-sonnet-4-6"`.

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

### B.1 Тихая синхронизация actors/ (v1.0.70+)

Между Send Update и DownloadApp есть второй release-asset:
`actors-snapshot-v<N>.zip` — платформо-независимый.

**Админская сторона** ([threads/update.py:SendUpdateThread](threads/update.py)
после upload .app zip):
- Пакует `actors/actors.json` + `actors/<slug>/*` (фото актёров).
- **Исключает** `actors/_textures/`, `.DS_Store`, `Thumbs.db`, любые `_*`
  служебные папки.
- Заливает как второй asset в тот же `app-vX.Y.Z` Release через тот же
  `upload_release_asset(rel["upload_url"], actors_zip_path)`.
- Любой failure pack/upload — **не валит** Send Update (best-effort,
  `.app` уже выехал).

**Коллежья сторона** ([threads/update.py:DownloadAppUpdateThread._sync_actors_snapshot](threads/update.py)):
- Вызывается ПОСЛЕ успешной распаковки .app/.exe zip, ДО создания
  bootstrap-скрипта. Не идёт через bootstrap — `actors/` не залочены
  процессом Studio, можно подменять live.
- Через `fetch_release_asset_by_name(version, "actors-snapshot")`
  ищет asset в Release.
- Качает zip → валидирует `is_zipfile` + наличие `actors/` префикса →
  распаковывает в отдельный tempdir (`<tmp>/actors_snapshot_<v>_<pid>/`).
- Дельта: `to_replace = zip_slugs`, `to_delete = local_slugs - zip_slugs`.
- Apply slug-by-slug (`rmtree + copytree`), retry 3× × 200ms на каждом
  shutdown — против Windows Defender locks.
- `actors.json` копируется целиком (поле `roles` затирается — продуктовое
  решение, у коллег `roles` не отображается в UI).
- **Защита:**
  - Локальная `actors/_textures/` НЕ трогается (фильтр `_is_protected`).
  - Любые `_*` папки и `.DS_Store/Thumbs.db` НЕ трогаются.
  - Если у пользователя есть `.git/` — это админ, skip целиком.
- Любая ошибка → `_early_log` (в `%LOCALAPPDATA%\StoryboardStudio\logs\`
  или `~/Library/Logs/StoryboardStudio/`) + continue (актёры остаются
  как были, основной .app update не страдает).

**Backward compat:**
- Старый Send Update (без actors-snapshot.zip) + новый Download:
  `fetch_release_asset_by_name` вернёт None → log + return, .app
  обновится без actors-sync.
- Новый Send Update + старый Download: старый не знает про asset,
  игнорирует. Следующий апдейт подхватит.

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
    ('instructions/ГЛАВНАЯ_ИНСТРУКЦИЯ*.md', 'instructions'),  # v1.0.66
    ('assets/models/face_detection_yunet_2023mar.onnx', 'assets/models'),  # 2026-06-02
],
```
Плюс автоматически — все импортируемые .py модули (включая `agents/*.py`).
**НЕ зашиваются:** `instructions/*.txt`, `_session_log.md`. Если
будущий feature потребует читать файлы в runtime — нужно
явно добавить в `datas` spec.

**2026-06-02 — opencv + YuNet (фича наложения PNG-сеток на лица сториборда):**
- Зависимости сборки: **`opencv-python-headless` + `numpy`** (numpy УБРАН из
  `excludes` в spec; `cv2` добавлен в `hiddenimports`). Только `-headless` —
  полный `opencv-python` тянет свой Qt и конфликтует с PyQt6 в бандле.
- `numpy` тянется как транзитивная зависимость cv2.
- Win-сборка ([.github/workflows/build-windows.yml](.github/workflows/build-windows.yml)):
  `pip install … opencv-python-headless numpy` добавлено.
- Модель YuNet (`assets/models/face_detection_yunet_2023mar.onnx`, ~232КБ,
  из OpenCV Zoo, git-LFS-media) бандлится через `datas`.
- Резолвер пути к модели — **`get_model_path(name)`**
  ([storyboard_app.py](storyboard_app.py), рядом с `get_icon`), `_MEIPASS`-aware
  (Mac `Contents/Resources/assets/models/`, Win onedir `_internal/assets/models/`),
  фолбэк на project_root из QSettings.
- ⚠️ **Win-сборка с opencv+numpy на момент Этапа 0 НЕ проверена** — обязательно
  обкатать на Windows до раздачи (cross-platform gate). Mac-фундамент рабочий.
- ⚠️ Бандл вырос (+opencv+numpy ~60-80МБ) → больше вес авто-апдейта коллегам.

**2026-06-24 — pillow-heif (кроссплатформенный heic→jpg для рефов):**
- Зависимость сборки: **`pillow-heif`** (wheels Win/Mac/Linux, тянет нативный
  `libheif`+`libde265`). В spec через **`collect_all('pillow_heif')`** → влито в
  `binaries`/`datas`/`hiddenimports` (+ явный `'pillow_heif'`). ⚠️ Пакет ДОЛЖЕН
  стоять в окружении сборки — иначе `collect_all` валит билд (намеренно: не
  шипим без heic-конвертера).
- Win-сборка ([.github/workflows/build-windows.yml](.github/workflows/build-windows.yml))
  + [BUILD_WINDOWS.md](BUILD_WINDOWS.md): `pip install … pillow-heif` добавлено.
- `register_heif_opener()` — ОДИН раз в `main()` ([storyboard_app.py](storyboard_app.py),
  после `_install_qt_message_handler`), lazy `try/except` (нет пакета → не падаем).
  В runtime.log пишет `[pillow-heif] registered`.
- Конвертер — **`convert_heic_to_jpg(src) -> bytes|None`** (модульная,
  [storyboard_app.py](storyboard_app.py) рядом с `add_photo_to_actor`): гибрид C —
  primary `pillow_heif`/PIL → fallback `sips` (только darwin) → None (caller
  отклоняет). Общий для location-DnD (этап 3) и будущей миграции `add_photo_to_actor`.
- ⚠️ Бандл arm64-only → heic-нативка едет только Apple-Silicon-коллегам (как и
  весь .app). `add_photo_to_actor` (sips) пока НЕ мигрирован на хелпер — follow-up.

`pipeline.py` после распаковки находится через `sys._MEIPASS` (на Mac
.app `Contents/Resources/pipeline.py`, на Win onedir `_internal/pipeline.py`).
Studio при старте копирует его в `project_root` через
`sync_pipeline_py_to_project` ([storyboard_app.py](storyboard_app.py)).

### Провайдеры картинок — три (2026-07-02: + Nano Banana 2 Lite)

Константы в `storyboard_app.py` (~:4280): `IMAGE_PROVIDER_NARWHAL`
("narwhal"), `IMAGE_PROVIDER_NARWHAL_LITE` ("narwhal_lite"),
`IMAGE_PROVIDER_OPENAI` ("openai"); кортеж валидных `IMAGE_PROVIDERS_ALL`;
мапа `IMAGE_PROVIDER_MODEL` (provider → model-строка payload). Обрезка
рефов до `OPENAI_MAX_REFS`=10 — по кортежу `IMAGE_PROVIDERS_REF_CAPPED`
(openai + narwhal_lite; narwhal исторически без обрезки). UI Настроек:
обе карточки провайдеров на `ModeSegment` (N сегментов; ProviderToggle
был жёстко 2-позиционным, в карточках больше не используется). Вкладка
Генератора: `MODELS_BY_MODE["image"]` в generator/generator_page.py.
server_check живость меряет по nano-banana-2 (Lite не тестирует).

### Bridge `image_provider.txt` (2026-05-15)

`pipeline.py` запускается AI-агентом в subprocess'е `claude -p` через
Bash tool — у него нет доступа к QSettings. Чтобы GUI-переключатель
«Nano Banana 2 / Nano Banana 2 Lite / OpenAI» в Настройках влиял и на
рефы локаций/объектов (а не только на шоты `GenerateThread`), Studio
пишет выбранного провайдера в `<project_root>/image_provider.txt`
(содержимое — одна строка `narwhal`, `narwhal_lite` или `openai`).
Старые CLI-сборки при незнакомом `narwhal_lite` падают на default
`openai` (безопасная деградация до Send Update).

Пишут оба места в `storyboard_app.py`:
1. `set_image_provider(value)` — при изменении в Настройках.
2. Старт MW рядом с `sync_pipeline_py_to_project` — гарантирует что
   файл существует даже если юзер ни разу не открывал Settings.

Читает `pipeline.load_provider()` — default `openai` (обратная
совместимость со старыми сборками без файла, которые ставились до
2026-05-15 — `Phase 2 hotfix #20` хардкодил OpenAI flow).

При `narwhal` endpoint = `/api/v4/flow/image/generate` (cost=4, мягче
content-фильтр, отдельный pool от OpenAI). При `openai` =
`/api/v4/openai/image/generate` (cost=1). Поле `model` НЕ передаётся
ни в одном случае (иначе NARWHAL flow маршрутизирует обратно в
OpenAI — см. коммент в [threads/generate.py:242](threads/generate.py:242)).

**FastGen v5 — миграция ЗАВЕРШЕНА (2026-06-20). Весь стек на v5.** Описание
v4-эндпоинтов ВЫШЕ — ИСТОРИЧЕСКОЕ (прежняя схема `/api/v4/flow|openai/image/generate`).

КОНТРАКТ v5 (единый по всем путям): submit
`POST /api/v5/generations?result_format=ref` — провайдер НЕ в пути, задаётся полем
`payload["model"]` (`nano-banana-2`=NARWHAL/flow, `nano-banana-2-lite`=NB2 Lite
(2026-07-02, 4 кред., до 10 рефов — обрезка как у OpenAI), `openai-image`=OpenAI;
мапа — `IMAGE_PROVIDER_MODEL` в storyboard_app.py, у CLI своя копия); рефы —
`payload["inputs"]=[{"name":f"img{i+1}","input":h}]` (биндинг ПОЗИЦИОННЫЙ, name
произвольный; старое `reference_images` убрано); хэш рефа — ГОЛЫЙ 32-hex (префикс
`file:` срезается в 5 upload-методах — v5 даёт `422 string_pattern_mismatch` на
префиксе, v4 срезал сам); `op_id = data["id"]` (не `operation_id`); poll
`GET /api/v5/generations/{id}?result_format=ref`, статусы-множества
(succeeded/success/completed/done = готово; failed/error/cancelled = ошибка;
queued/running = поллим); storage_id из `results[0].metadata.storage_id` (+fallback
на старый url/ref/file_hash-разбор). Граница `image_bytes`/сохранение и
`{STORAGE}/upload`+`/file/{id}/raw` НЕ менялись.

МИГРИРОВАННЫЕ ПУТИ (все): `RefGenerateThread` (рефы локаций/объектов 🔄/✏️),
`GenerateThread` (шоты), `GenerateActorRefThread`/`EditActorRefThread` (актёры) в
`threads/generate.py`; CLI `pipeline.py` (локации/объекты из чата) и
`generate_storyboards.py` (batch-шоты, ручной/агентский); служебные
`threads/cancel.py` (серверная отмена → `DELETE /api/v5/generations/{id}`, читает
поле `cancelled` СТРОКОЙ "True"/"False") и `threads/server_check.py` («Проверить
сервер» — ДВА теста подряд model=`nano-banana-2`+`openai-image`, у каждого свой
потолок CEILING_SEC и свой замер времени; результат — две строки `results`-сигнала).

ДИАГНОСТИКА/прочее: каждый путь пишет `[FASTGEN] path=… api=v5 … op_id=… status=…
result=ok|error [error=<причина>]` (GUI → runtime.log через studio tee; CLI → stdout;
ключ НЕ логируется — только `auth=X-API-Key`). Поллинг ускорен 4с→1.5с (GenerateThread
`for _ in range(3): sleep(0.5)` со stop-проверкой). `_is_ref_expired_error` — v5
(404 + JSON `code/error` `resource.file_not_found_or_expired`). `_http_error_detail`
дописывает поле-уровневый pydantic `detail`.

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
- **Видео-превью в .app: cv2 backend ОБЯЗАН быть системным** (2026-06-29).
  `GeneratorVideoThread._extract_first_frame` ([generator/generator_video_thread.py](generator/generator_video_thread.py))
  извлекает первый кадр .mp4 → `gen_<ts>.jpg` (превью плитки). cv2 по умолчанию декодит
  .mp4 через FFMPEG, чьи dylib (`cv2/.dylibs/libav*`) PyInstaller в бандл НЕ кладёт → в
  frozen .app `VideoCapture` молча не читает кадр (превью пропадает, остаётся ▶). Поэтому
  форсим СИСТЕМНЫЙ backend: macOS `cv2.CAP_AVFOUNDATION`, Win `cv2.CAP_MSMF` (оба
  скомпилированы в cv2, внешних dylib/DLL не требуют), с фоллбэком на default. НЕ убирать
  ветку по `sys.platform` — иначе в .app снова пропадут превью видео.
- **Генератор: дроп-картинка кладётся как JPEG + стем-резолв файла плитки**
  (2026-06-28). `_import_dropped_files` ([generator/generator_page.py](generator/generator_page.py))
  конвертирует дропнутую картинку в `gen_<ts>.jpg` (q=95, без даунскейла) для
  ВСЕХ форматов кроме уже-`.jpg/.jpeg`. Причина: на машинах с Adobe watch-folder
  внешний процесс сам пере-кодирует любой `.png` в папке холста в `.jpg` за
  секунды и удаляет оригинал → путь плитки в meta/`_result_path` (с `.png`)
  протухал, клик/реф/папка ломались. Видео не трогаем. Defensive-страховка:
  `resolve_existing_path(path)` + `ShimmerCell._heal_path()`
  ([generator/result_cell.py](generator/result_cell.py)) — если точный путь файла
  плитки исчез, ищем файл с тем же СТЕМОМ `<stem>.*` в той же папке и обновляем
  `_result_path`+`meta['file']` на реальное расширение. НЕ удаляй — это защита от
  внешней подмены расширения (клик/reveal/ref + `add_ref_from_meta` зовут резолв).
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

## Пул API-ключей FastGen (round-robin + индикатор) — 2026-06-09

До 5 ключей FastGen с балансировкой по кругу. При 1 ключе поведение
идентично прежнему (полная обратная совместимость).

### Модуль `key_pool.py` (корень репо)
Чистый Python, **без Qt** и **без импорта storyboard_app** (импортируется
CLI-скриптами в отдельных процессах — Qt там недоступен; circular import
исключён). Экспорт:
- `get_keys() -> list[str]` — все настроенные ключи по порядку. Источник:
  сайдкар `fastgen_keys.txt` → fallback на первую строку `.env` (= старое
  одиночное поведение).
- `next_key() -> str` — следующий ключ по кругу. 1 ключ → возврат без
  изменения курсора; >1 → атомарный сдвиг курсора. ЛЮБОЕ исключение →
  fallback на одиночный ключ (kill-switch: генерация не падает).
- `save_keys(list)` — атомарная запись сайдкара (tmp + os.replace).
- `set_root(path)` — переопределяет project_root (writable) для frozen GUI.

### Хранение (всё в project_root)
- **QSettings** `fastgen_api_key` (primary) + `fastgen_api_key_2..5` — для
  репопуляции 5 полей в Настройках.
- **`fastgen_keys.txt`** — сайдкар, рантайм-источник ротации (по строке на
  ключ). СОДЕРЖИТ КЛЮЧИ → в `.gitignore`.
- **`.env` строка 0** — primary-ключ (legacy `load_key()`/`load_api_key()`
  fallback; sync через существующий `save_api_key`).
- **`.fastgen_keys_cursor`** — монотонный курсор round-robin.

### GUI vs CLI (резолв ROOT)
- CLI (`pipeline.py`/`generate_storyboards.py`) импортируют `key_pool` из
  project_root (куда он скопирован) → `Path(__file__).parent` = writable.
- Frozen GUI импортирует `key_pool` из бандла (_MEIPASS, read-only) →
  `storyboard_app` зовёт `key_pool.set_root(project_root)` при старте
  (рядом с `sync_*` в `MainWindow.__init__`).

### Читатели ключа (6 шт., все через пул)
- 4 GUI-потока в `threads/generate.py` (GenerateThread / RefGenerateThread /
  GenerateActorRefThread / EditActorRefThread) → `_sa.next_api_key()` →
  обёртка `storyboard_app.next_api_key()` → `key_pool.next_key()`.
- 2 CLI: `pipeline.py` / `generate_storyboards.py` → `next_key() or load_key()`
  (ленивый импорт + двойной fallback; без `key_pool.py` рядом — тихо на
  одиночном ключе).

### Доставка в frozen .app
`StoryboardStudio.spec` datas бандлит `pipeline.py`, `generate_storyboards.py`,
`key_pool.py`. `sync_pipeline_py_to_project` (MainWindow.__init__) копирует
все три в project_root при старте (цикл с `continue`, skip-identical).
⚠️ Старый .app (до этих правок) при запуске откатывает `pipeline.py` в
project_root старым бандлом — до пересборки .app не запускать против рабочего
репо.

### Индикатор-лампочка (косметика, реалтайм)
Файл-мост `.fastgen_keys_active` (`"{idx} {nonce}"`, nonce=time.time())
пишется в `key_pool.next_key()` ПОСЛЕ выбора ключа, в СОБСТВЕННОМ try/except
— его поломка НЕ влияет на выдачу ключа. GUI (`storyboard_app`) слушает файл
через `QFileSystemWatcher` (создан в `__init__`, не привязан к вкладке) и
мигает лампочкой выданного ключа (`_on_keypool_active_changed` →
`_blink_key_indicator`, вспышка ~400мс с авто-гашением). Пишется при выдаче
из ЛЮБОГО источника (GUI + CLI) → «бегущий огонёк» по реально занятым ключам.
Дедуп по nonce (watcher шумит по каталогу). Всё в try/except — на генерацию
не влияет.

**2026-06-09 (апдейт: прямой сигнал + по успеху + poll-таймаут):**
- **Лампочка теперь через прямой Qt-сигнал, а НЕ watcher.** `QFileSystemWatcher`
  на файл-мост оказался ненадёжен: проект на внешнем томе (`/Volumes/…`), FSEvents
  там часто не доставляет `directoryChanged`/`fileChanged` → лампочка молчала.
  Файл-мост `.fastgen_keys_active` + watcher ОСТАВЛЕНЫ как fallback и единственный
  путь для CLI, но основной путь для GUI — сигнал `key_used = pyqtSignal(int)` в
  4 потоках (`threads/generate.py`), подключён к `_blink_key_indicator` в 9 точках
  (7 в `storyboard_app.py`, 2 в `views/actors.py` через `self.window()`).
- **Мигает по УСПЕХУ, а не по выдаче.** idx выданного ключа сохраняется локально
  в потоке (`self._used_key_idx = key_pool.last_index()` сразу после выдачи), а
  `key_used.emit` — ПЕРЕД `finished.emit` (на успешном скачивании картинки). Так
  мёртвый ключ (403/404/лимит) уходит в `except` ДО успеха и НЕ мигает. Новый
  аксессор `key_pool.last_index()` отдаёт idx последней выдачи без смены сигнатуры
  `next_key()`.
- **Диагностика ошибок:** `_http_error_detail` (`threads/generate.py`) дописывает
  причину сервера в текст ошибки (`… | server: <текст> [<code>]`) для 4 GUI-потоков
  — раньше был только generic «403 Client Error» без тела ответа.

### Poll-таймаут потоков генерации (2026-06-09)
Все 4 потока (`GenerateThread`, `RefGenerateThread`, `GenerateActorRefThread`,
`EditActorRefThread`) имеют `POLL_TIMEOUT_SEC = 300` (5 мин, по `time.monotonic`).
Если FastGen держит операцию в `pending` дольше — `error.emit("API timeout…")` +
`return`, поток завершается, карточка освобождается. Раньше `GenerateThread`/
`RefGenerateThread` крутили `while True` без потолка → под нагрузкой Mode C
(~40 потоков) зависшая операция висела вечно (600+с), карточка не закрывалась.

### Авто-ретрай транзиентных сбоев генерации (video-путь, 2026-06-28 + 2026-07-04)
`GeneratorVideoThread.run` (`generator/generator_video_thread.py`) оборачивает ВСЮ
генерацию (submit+poll) в `for retry_attempt in range(4)` — потолок 3 повтора,
пауза 10с (`_retry_pause()`, дроблёная по `_stop`), единый механизм
`retry_pending`/`break` (submit-стадия → гвард `if retry_pending: continue` перед
poll; poll-стадия → существующий гвард в конце цикла).
- 2026-06-28: ретрай серверного `status=failed` с транзиентным текстом
  (`gen_errors.is_transient`: try again / captcha / concurrency / 502/503/504 / …).
- 2026-07-04: + транзиентные HTTP `_RETRYABLE_HTTP = (500,502,503,504)` на submit
  И poll; + обрыв транспорта (`requests.RequestException`: connection aborted /
  read-write timeout) на submit И poll. Каждый повтор пишет `error=net_retry` в
  `runtime.log` (видно в live-мониторе `tail -f`).
НЕ ретраятся (deny-wins): 401/403 (мёртвый ключ) → `dead`; 429 → перебор ключей по
кругу (свой механизм, НЕ `range(4)`); `key_search_timeout` 180с; `POLL_TIMEOUT` 600с;
`invalid_request`/контент/лицензия → `human_message`. Картиночный путь
(`generator/generator_thread.py`) те же 500/502/503/504 + обрыв пока НЕ ретраит —
зеркальная правка отложена (poll-GET там вообще без try → сырое исключение в общий
`except`).

### Задача Б (НЕ сделана, отложена)
Failover при лимите/ошибке ключа: если ключ упёрся в лимит или отвалился —
временно вывести из ротации, нагрузка на живые без перезагрузки; крестик
«ключ недоступен» у поля; уведомления о лимите. Сейчас обработка ошибок
FastGen НЕ различает «лимит ключа» от «сервер недоступен» (всё через
`raise_for_status` → generic), retry-логики нет. Это следующий этап.
