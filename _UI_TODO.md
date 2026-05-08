# UI TODO — что осталось доделать по LUMZ-стилю

**Создано:** 2026-05-08 (вечер)
**Статус:** отложено, юзер занят другими делами.

Этот файл — список всего что НЕ перевели в LUMZ-стиль за сегодняшнюю сессию.
Когда вернёмся к интерфейсу — берём отсюда по порядку.

---

## ✅ Сделано сегодня (для контекста)

- TODO 5/6/7/8 (бегущие точки в чате, индикатор «долго думаю», цвет диалогов
  в попапе сценария, кнопки «+ Добавить» в REFS view)
- RefPickerDialog (попап «+ Добавить локацию/объект»)
- Hand-off thinking + анимация на «▶ Долго думаю»
- Текст «▶ Долго думаю — это нормально. Не закрывай Studio» (без точки в
  конце), без пустой строки между «▶ Думаю» и «▶ Долго думаю»
- status_lbl убран из layout episode_chat (дублировал точки)
- Кнопка «Запустить» после удаления эпизода (тред останавливается)
- GenButton (карточка авто-генерации рефа в чате) — bg_subtle + LUMZ red CTA
- MontageCTA баннер «Все рефы залинкованы» — LUMZ-стиль + убрана 🎬
- Иконки 🎬/🤖/🎬 убраны из текстов MontageCTA
- Время «1-3 минуты» → «может занять до 5 минут»
- MontageSummaryDialog (попап с таблицей блоков) — LUMZ-стиль + убрана 🎨
- Сториборды batch-per-block (внутри блока — параллельно, между блоками —
  последовательно)

---

## ⏳ Осталось — UI LUMZ (приоритет сверху вниз)

### 1. NewEpisodeView — стартовый экран «+ Новый эпизод»

**Файл:** `views/new_episode.py`

Это первое что видит юзер при создании нового эпизода. Содержит:
- Drop-зону для сценария (paste/file).
- Поле «номер серии».
- Кнопки «Запустить» / «Стоп».
- Дропдаун модели.
- Лог + чат-инпут.

Часть стилей уже подтянута в Этапе 6 (chat-input, log_view, save-кнопка).
Но сам creation_block с paste-areas, кнопками Run/Stop, ScenarioDropZone —
проверить и довести до LUMZ.

**Связанные файлы:** `views/scenario_drop_zone.py`.

### 2. Вкладка «Актёры» (Actors tab)

**Файл:** `views/actors.py` + `widgets/actor_dialogs.py`

Список карточек актёров, кнопка «+ Создать актёра», диалоги создания/
редактирования персонажа. Часто-используемая вкладка.

### 3. Outfit picker (одежда персонажа)

**Файл:** `widgets/character_outfit_picker.py`

Попап выбора одежды для актёра. Используется при первом создании рефа
персонажа в эпизоде (когда AI просит «добавь одежду для Лоры»).

### 4. NewShowDialog (создание сериала)

**Файл:** `views/new_show_dialog.py`

Попап «Создать новый сериал» — открывается по клику «+» рядом с
дропдауном сериала. Один раз увидится коллегой при первой настройке,
но всё равно лицо продукта.

### 5. AuthBanner (квота AI закончилась)

**Файл:** `widgets/auth_banner.py`

Красный баннер вверху Studio когда `claude` CLI вернул ошибку квоты или
не авторизован. Видится коллегами в момент когда у них счётчик
истекает — критичный момент UX.

### 6. PromptRetryDialog (альтернативы при softening)

**Файл:** `widgets/prompt_retry_dialog.py`

Попап с альтернативами при срабатывании NSFW-фильтра NARWHAL. Реже
используется, но в LUMZ должен быть.

### 7. ShotViewerDialog (увеличенный шот)

**Файл:** `widgets/shot_viewer_dialog.py`

Попап увеличенной картинки шота при двойном клике на карточку.
Контейнер для просмотра + история версий.

### 8. ActiveGensPanel (панель активных генераций)

**Файл:** `widgets/active_gens_panel.py`

Floating-панель внизу чата показывающая активные авто-генерации
рефов (cafe/кладовка и т.п.). Юзер может видеть список параллельных
работ.

### 9. Settings tab — детальная проверка

**Файл:** `storyboard_app.py:_build_settings_tab` (часть)

Часть настроек уже LUMZ (кнопки `settings-row-btn`, «Открыть лог»,
«Открыть папку»), но слайдер скорости анимаций, дропдаун модели,
переключатели языка — проверить.

### 10. Семантические эмодзи в монтажке (опционально)

**Файлы:** `i18n.py` (montage_status_*), `widgets/montage_summary_dialog.py:_build_agent_lines`

Сейчас остались:
- `🎬 Сценарист`, `🔍 Чекер`, `✏ Редактор`, `🎯 Финальный редактор` — в
  попапе монтажки (`MontageSummaryDialog._build_agent_lines`).
- `🔍 Чекер проверяет`, `✏ Редактор правит`, `✓ Раунд N`, `⚠ Раунд N`,
  `🎯 Финальный редактор`, `✓ Финальный редактор`, `⚠ Финальный
  редактор` — в `montage_status_*` (статус оркестратора в чате).

Юзер на них прямо не указал, но если делаем «полный LUMZ без эмодзи»
— надо убрать. На сегодня оставлены как семантические маркеры.

---

## ⏳ Осталось — функциональные TODO (не UI)

### TODO 4 — Вернуть кнопку «🎨 Сгенерировать» после ошибки AutonomousGenThread

См. `NEW_CHAT_BRIEFING.md` секция TODO 4 (~10 строк).

**Симптом:** юзер кликнул «Сгенерировать buldog» → subprocess `claude -p`
упал → popup с ошибкой → юзер дисмиснул → кнопки больше нет в чате,
перегенерить нельзя без перезапуска Studio.

**Файлы:** `storyboard_app.py:_on_active_gen_error` (~line 3643),
`views/episode_chat.py:_gen_seen_names`.

### TODO 1 — Авто-перевод сценария на язык чата

См. `NEW_CHAT_BRIEFING.md` секция TODO 1 (детальный план реализации).

При клике на золотую плашку «X серия» сценарий должен переводиться
на язык текущего чата (RU/UK/EN определяется по последним user-messages
в jsonl).

### TODO 2 — Win10/11 prep блокеры

См. `_WINDOWS_PREP_TODO.md`.

3 файла без `CREATE_NO_WINDOW` для `subprocess.Popen` на win32:
- `threads/autonomous_gen.py:216`
- `threads/suggest_outfits.py:256`
- `threads/generate.py:587`

~10 минут перед Win-релизом.

### TODO 3 — Чистка orphan'ов в refs/locations/

См. `NEW_CHAT_BRIEFING.md` секция TODO 3.

`<object>_prompt.txt` для объектов в `refs/locations/` (исторический баг
старого `pipeline.py` без `--kind`).

---

## Где брать LUMZ-токены при переписывании

`views/theme.py:LUMZ_THEME` — словарь design tokens. Цвета:
- `bg_main`, `bg_panel`, `bg_card`, `bg_subtle`, `bg_hover`
- `border_default`, `border_strong`, `border_subtle`
- `text_primary`, `text_secondary`, `text_muted`
- `accent_red`, `accent_red_bg`, `accent_red_border`, `accent_red_subtle`,
  `accent_red_subtle_border`
- `accent_gold`, `accent_gold_bg`, `accent_gold_border`
- `radius_sm` (6), `radius_md` (8), `radius_lg` (14)

**Унифицированные паттерны кнопок** (см. `widgets/gen_button.py`,
`widgets/montage_cta.py`, `widgets/montage_summary_dialog.py`):

```python
# Primary CTA — solid LUMZ red
"QPushButton { background: #e4344a; color: #ffffff;"
" border: none; border-radius: 6px;"
" padding: 6px 14px; font-size: 12px; font-weight: 500; }"
"QPushButton:hover { background: #d92d44; }"
"QPushButton:pressed { background: #c52539; }"

# Secondary — нейтральный save-style
"QPushButton { background: rgba(255,255,255,0.06);"
" color: #ffffff;"
" border: 1px solid rgba(255,255,255,0.12);"
" border-radius: 6px; padding: 6px 14px;"
" font-size: 12px; font-weight: 500; }"
"QPushButton:hover { background: rgba(255,255,255,0.10);"
" border-color: rgba(255,255,255,0.20); }"

# Subtle accent — accent_red_subtle (для retry / опасных действий)
"QPushButton { background: rgba(228,52,74,0.10);"
" color: #e4344a;"
" border: 1px solid rgba(228,52,74,0.25);"
" border-radius: 6px; padding: 6px 14px; }"
"QPushButton:hover { background: rgba(228,52,74,0.18);"
" border-color: rgba(228,52,74,0.40); }"
```

**Карточка-контейнер** (как ref-card, gen-card):
```python
"QFrame { background: rgba(255,255,255,0.04);"
" border: 1px solid rgba(255,255,255,0.06);"
" border-radius: 8px; }"
"QFrame:hover { border-color: rgba(228,52,74,0.40); }"
```

---

## Workflow когда вернёмся

1. Прочитать `_session_log.md` (хвост) и этот файл.
2. Запустить Studio в текущей сборке, посмотреть где старый стиль виден.
3. Брать пункты из этого файла по порядку (1 → 9).
4. Каждый виджет: мини-разведка (`grep "background:" widgets/<file>.py`),
   замена old-цветов на LUMZ-токены, ast.parse + smoke + build + relaunch.
5. После каждой правки — запись в `_session_log.md`, обновление этого файла
   (вычеркнуть пункт).
