# NEW CHAT BRIEFING — 2026-05-08 (поздний вечер) · Win-релиз через GitHub Actions

**Дата:** 2026-05-08, поздний вечер. Очень большая сессия (~30 правок + Win build automation).
**Юзер:** админ/мейнтейнер. Активный сериал: `finalnyy_raschet`.
**Текущая стабильная версия Studio (на GitHub):** **app-v1.0.15** или новее (см. ниже).

---

## ⚠ Стартовое сообщение для нового чата

> Прочитай `_session_log.md` (хвост ~1500 строк за 2026-05-08 — там
> Win-релиз через GitHub Actions + множество UI-фиксов) и
> `NEW_CHAT_BRIEFING.md`. **Главное сейчас** — юзер тестирует Win-сборку
> на ноутбуке коллеги, ждёт результата теста v1.0.16 (фикс размера окна).
> Открытая большая задача — **Variant A: переработка установщика**
> (`installer_app.py`) без потери данных. Скажи «делаем?» когда готов.

После этого Claude должен:
1. Прочитать **хвост `_session_log.md`** (~последние 1500 строк за 2026-05-08).
2. Прочитать `_UI_TODO.md` (остатки LUMZ-стиля).
3. Прочитать `_WINDOWS_PREP_TODO.md` (закрытые блокеры Win).
4. Проверить состояние main: `git log --oneline -10`.
5. Проверить активные runs: https://github.com/Akex24/storyboard-automation/actions

---

## 🎯 ГДЕ МЫ СЕЙЧАС (контекст последних 30 минут)

### ✅ Что ТОЧНО работает (проверено юзером на Win)

1. **GitHub Actions для Win-сборки** — настроен и работает.
   - Триггер: `release: published` (через `SendUpdateThread.create_github_release`).
   - `.github/workflows/build-windows.yml` собирает `Storyboard Studio.exe` + `Installer.exe` за ~3 мин на windows-latest.
   - Грузит в Release как `Storyboard Studio vX.Y.Z-win.zip`.
2. **Studio.exe запускается на Win без crash** (после fix scenario_drop_zone → v1.0.15).
3. **UI рендерится корректно** на Win (PyQt6 встроен в bundle).
4. **Claude CLI** установлен и авторизован вручную через `"%USERPROFILE%\.local\bin\claude.exe" login` (PATH issue в текущем installer).

### ⏳ Что СЕЙЧАС ждём подтверждения

**v1.0.16** — fix размера окна (`setMinimumSize` 1000×900 → 900×600). Юзеру надо:
1. Жмёт «📤 Отправить обновление» в Studio Mac → bump app-v1.0.16.
2. Win-workflow собирает новый Win-zip (3 мин).
3. На Win-ноуте скачивает v1.0.16-win.zip → запускает → должно влезать на экран.

### 🔴 ОТКРЫТЫЕ КРУПНЫЕ ЗАДАЧИ

#### 1. Variant A — переработка установщика (КРИТИЧНО для команды)

**Контекст:** при запуске юзером installer'а на Win-ноуте обнаружились серьёзные проблемы:
- Установщик распаковывает GitHub-zip целиком в папку проекта → коллега получает **всю методологию в открытом виде** (instructions/, CLAUDE.md, agents/*.py, _UI_TODO.md, и т.д.).
- Studio.exe **не скачивается** установщиком — юзер должен получить от админа отдельно (плохо для UX).
- `_open_terminal_login` использует литерал `"claude"` вместо `self._cli_path` → cmd ругается «не является командой» (PATH issue).
- Финальный экран говорит «Storyboard Studio.app» на Win (где должно быть .exe).
- `project_root` в QSettings сохраняется только при клике «Открыть папку проекта» (юзер логично жмёт «Закрыть установщик» → Studio при запуске не находит проект).

**План для Variant A** (юзер согласовал, ОТЛОЖЕНО до подтверждения работоспособности базы):
1. **Фильтр в `DownloadProjectThread`** — НЕ распаковывать `instructions/`, `agents/`, `*.md`, `tests/`, `.github/`, `.spec`, все `*.py` (они уже в .exe). Оставлять только `actors/` (демо команды) + создать пустые `shows/`, `output/`.
2. **Новый класс `DownloadAppExeThread`** — качает `Storyboard Studio.exe` из последнего Release через `fetch_release_asset_info`.
3. **Новый `StepDownloadApp`** — между `StepKey` и `StepClaudeCode`, скачивает .exe и кладёт рядом с проектом.
4. **`InstallerWindow`** — добавить новый step в stack (5/5 → 6/6).
5. **`StepDone`** — авто-сохранение `project_root` в QSettings (без зависимости от кнопок) + платформо-зависимый текст (.app/.exe) + создание ярлыка на рабочем столе через PowerShell на Win.
6. **`_open_terminal_login`** — использовать `self._cli_path` вместо литерала `"claude"`.
7. **Скрыть Python-шаг для Win** — Studio.exe это standalone bundle, Python не нужен.

После Variant A → bump до `app-v1.0.17` или похожее → Win-workflow соберёт новый `installer.exe` со всеми фиксами → раздать команде.

#### 2. UI остатки LUMZ-стиля (см. `_UI_TODO.md`)

10 пунктов виджетов которые остались в old-стиле (NewEpisodeView, Actors tab, OutfitPicker, NewShowDialog, AuthBanner, PromptRetryDialog, ShotViewerDialog, ActiveGensPanel, Settings проверка, эмодзи в монтажке).

Не критично, делается между другими задачами.

#### 3. Семантические эмодзи в `montage_status_*` (опционально)

`🔍 Чекер проверяет`, `✏ Редактор правит`, `✓ Раунд N`, `⚠ Раунд N`, `🎯 Финальный редактор` в i18n.py. Юзер пока не просил убирать — оставлены.

---

## 📋 Что было сделано в этой сессии (2026-05-08)

### UI редизайн под LUMZ (продолжение Этапа 6)

- **TODO 5/6/7/8** (анимация точек в чате, индикатор «Долго думаю», цвет диалогов в попапе сценария, кнопки «+ Добавить» в REFS).
- **RefPickerDialog** (попап выбора рефа) → LUMZ.
- **GenButton** (карточка авто-генерации в чате) → полный переход на LUMZ + убраны иконки 🎨/📁/📍/🎁/👤 + кнопка «Не нужен» скрыта (orphan).
- **MontageCTA** (баннер «Все рефы залинкованы») → LUMZ + убрана 🎬.
- **MontageSummaryDialog** (попап «Монтажная карта») → LUMZ + убрана 🎨 на «Делать сториборды».
- **Иконки убраны** в i18n: 🎬 в `montage_cta_subtitle_idle`, 🤖 в `montage_cta_title_running`, 🎬 в `montage_status_scriptwriter`, 🎬 в `montage_summary_btn_storyboards`.
- **Время** «1-3 минуты» → «может занять до 5 минут» в `montage_cta_subtitle_running`.
- **Точка после Studio** убрана — `Не закрывай Studio.` → `Не закрывай Studio` (анимация бегущих точек теперь чистая).
- **status_lbl** убран из layout `episode_chat.py` (orphan-виджет, дублировал точки в чате).

### Поведенческие фиксы

- **Точки `▶ Думаю`** теперь бегут прямо в `log_view` (а не только в `status_lbl`). Финал — без точек, без многоточия (просто `▶ Думаю`).
- **`▶ Долго думаю`** появляется сразу под `▶ Думаю` (без пустой строки) с бегущими точками.
- **Hand-off thinking** — при запуске нового эпизода через NewEpisodeView точки бегут в EpisodeChatView через `begin_external_thinking(thread)`.
- **Кнопка «Запустить» после удаления эпизода** — теперь активна (фоновый `RunEpisodeThread` корректно останавливается при удалении ep).
- **Сториборды batch-per-block** — внутри блока все шоты параллельно, между блоками последовательно. Заменил `_storyboard_shot_queue` + `_storyboard_queue_busy` на `_storyboard_blocks_queue` + `_storyboard_active_block` + `_storyboard_active_pending`.

### Win10/11 prep

- **CREATE_NO_WINDOW guard** в 4 местах:
  - `threads/autonomous_gen.py` (claude -p для авто-генерации location/object).
  - `threads/suggest_outfits.py` (claude -p для одежды character).
  - `threads/generate.py:ClaudeGeometryThread` + `RunEpisodeThread`.
- **Installer**: 3 места защищены guard'ом (Claude install, auth check, python --version).
- `_WINDOWS_PREP_TODO.md` обновлён, все P0 закрыты.

### Cross-platform install

- **`fetch_release_asset_info`** в storyboard_app.py — фильтрует по `sys.platform`: `mac` или `win` в имени файла.
- **`DownloadAppUpdateThread`** в threads/update.py — кросс-платформенно: Mac → `.app` (copytree), Win → `.exe` (copy2). PermissionError fallback в `~/Downloads`.

### GitHub Actions

- **`.github/workflows/build-windows.yml`** — собирает .exe на release: published.
- **PAT-токен** обновлён с scope `workflow` (юзер вручную через https://github.com/settings/tokens).
- **Мегакоммит `2f0071a`** — sync 108 файлов (модульная архитектура, LUMZ, всё что было локально не пушено).
- **Hotfix `fa2f794`** — `scenario_drop_zone` NoneType crash на свежей Win-установке.
- **Hotfix `67db21b`** — окно слишком большое (1000×900 → 900×600 minimum).

---

## 🔧 КРИТИЧЕСКИЕ ТЕХНИЧЕСКИЕ ДЕТАЛИ

### Цикл релиза

1. Админ собирает Mac локально: `./build.sh` (smoke + PyInstaller + smoke-launch).
2. В Studio: Settings → «📤 Отправить обновление» с галочкой «Обновить app».
3. `SendUpdateThread`:
   - Бампит `version.json` (project + опционально app_version).
   - `git add -A && commit && push origin main`.
   - `create_github_release(tag=app-v…, target=main)` через GitHub REST API.
   - Архивирует `dist/Storyboard Studio.app` → `Storyboard Studio v…-mac.zip` → `upload_release_asset`.
4. **GitHub Actions** ловит `release: published` → workflow `build-windows.yml`:
   - windows-latest runner, Python 3.11.
   - `pip install PyQt6 Pillow requests pyinstaller certifi python-docx`.
   - `pyinstaller StoryboardStudio.spec --noconfirm` → `dist/Storyboard Studio.exe`.
   - `pyinstaller StoryboardStudioInstaller.spec --noconfirm` → installer.exe.
   - PowerShell `Compress-Archive` → `Storyboard Studio v…-win.zip`.
   - `gh release upload "$tag" "$zipPath" --clobber`.
5. **Коллеги-Mac** — `CheckUpdateThread` → `DownloadAppUpdateThread` качает `…-mac.zip`.
6. **Коллеги-Win** — то же, качает `…-win.zip`. Установщик пока распаковывает GitHub-zip целиком (см. Variant A).

### Где искать важные коды

- `storyboard_app.py:fetch_release_asset_info` (~line 1584) — platform-aware filter.
- `threads/update.py:DownloadAppUpdateThread.run` — cross-platform install.
- `threads/update.py:SendUpdateThread.run` — bump + push + release.
- `installer_app.py` — установщик (5 шагов: Welcome/Python/Download/Key/ClaudeCode/Done).
- `.github/workflows/build-windows.yml` — Win build CI.

### Ключевые состояния

**Локально на маке админа:**
- `.env` — Anthropic + Fast Gen ключи (gitignored).
- QSettings `~/Library/Preferences/com.storyboardstudio.StoryboardApp.plist`:
  - `project_root` = `/Volumes/DaVinci SSD/Работа/storyboard-automation/`
  - `fastgen_api_key`, `image_provider`, `anim_speed_multiplier`, и т.д.
- git remote с PAT в URL (scope: `repo` + `workflow`).

**На GitHub:**
- Repo: `github.com/Akex24/storyboard-automation` (приватный).
- Branch: `main`.
- Releases: теги `app-v1.0.X` начиная с 1.0.2 до текущего.
- Последний хороший Win-zip: `app-v1.0.15` (или `1.0.16` после теста юзера).

**В .gitignore (расширен сегодня):**
- `.env`, `.env.local`, `*.bak`, `*.md.bak`, `*.log`, `.claude/`, `_session_log.md`, `_inbox/`, `dist/`, `build/`, `output/`, `scenarios/`, `refs/`, `shows/`, `current_show.json`.

---

## 🛡 ВАЖНЫЕ ПРАВИЛА (из памяти)

См. `~/.claude/projects/.../memory/MEMORY.md`. Кратко:

- **Кросс-платформа Mac+Win10/11:** все правки на обеих ОС. Subprocess'ы → `creationflags=0x08000000` на win32.
- **Спрашивать «делаем?»** перед любой правкой кода (если юзер просит «как ты понял?» — ответить пониманием, ждать подтверждения).
- **После правки** — короткий «что проверить» чек-лист.
- **Логи:** каждая правка — запись в `_session_log.md`.
- **Auto-rebuild:** после правок сам делать `rm -rf build/ && ./build.sh && open dist/Storyboard\ Studio.app`. Юзер не запускает билд руками.
- **AST + smoke** перед билдом.
- **«Claude»/«Клод» НЕ использовать в UI Studio** (юзер-видимые строки) — заменять на «ассистент»/«AI»/«ИИ». Исключения: `Claude CLI` в установщике, имена классов/функций, URLs `claude.com`/`claude.ai`.
- **Lucide иконки** только из `assets/icons/` через `get_icon('name')`.
- **Модульная архитектура** — новые фичи в свой файл (`views/`, `widgets/`, `threads/`).
- **«Стоп» = пауза, не откат**.
- **Никаких автодействий по убийству процессов** — перед `pkill` спросить.

---

## 📁 Активный сериал и состояние

- `current_show.json` → `{"current": "finalnyy_raschet"}`.
- Эпизоды: ep1..ep4 (точное состояние зависит от того что юзер делал).
- `shows/finalnyy_raschet/refs/locations/` может содержать orphan-промпты (см. TODO 3 в брифинге).

---

## 📞 Контактные файлы

- `_session_log.md` — полный лог за день (~27000 строк, .gitignored).
- `NEW_CHAT_BRIEFING.md` — этот файл (актуальная версия).
- `NEW_CHAT_BRIEFING_old_2026-05-08_handoff.md.bak` — предыдущая версия (.gitignored, локально).
- `~/.claude/projects/.../memory/MEMORY.md` — индекс правил.
- `_WINDOWS_PREP_TODO.md` — Win-blockers checklist (закрыт, все P0 done).
- `_UI_TODO.md` — UI LUMZ остатки (10 пунктов).
- `CLAUDE.md` — правила сторибординга для AI agents.
- `.github/workflows/build-windows.yml` — Win build CI.

---

## 🚀 Что делать в новом чате СРАЗУ

1. **Прочитать хвост `_session_log.md`** (последние 1500 строк за 2026-05-08).
2. **Проверить `git log --oneline -5`** — узнать на каком commit'е main.
3. **Проверить https://github.com/Akex24/storyboard-automation/actions** — есть ли свежие workflow runs.
4. **Спросить юзера:** «Какой статус v1.0.16 на Win? Окно влезает?» → дальше идти по его ответу:
   - Если **влезает** → переходим к Variant A (переработка installer).
   - Если **не влезает** → нужны другие фиксы (например уменьшить отступы внутри UI).
   - Если **новый crash** → срочно фикс, новый bump.

**НЕ начинай Variant A без явного «делаем» от юзера.** План записан выше, но юзер должен сначала подтвердить что Win-сборка стабильна.

---

Welcome aboard! 🎬🪟
