# Storyboard Automation

Система генерации сторибордов для вертикальных сериалов на основе Claude Code + Fast Gen AI.

## Для коллег

Скачай установщик у админа проекта и запусти его — мастер проведёт через все шаги:

1. Проверка Python
2. Скачивание проекта с GitHub
3. Ввод Fast Gen AI ключа
4. Установка Claude Code
5. Запуск приложения

После установки приложение будет автоматически проверять обновления при запуске.

## Структура

```
storyboard-automation/
├── pipeline.py              # генерация локаций (Fast Gen AI)
├── generate_storyboards.py  # генерация сторибордов с рефами
├── storyboard_app.py        # GUI приложение (PyQt6)
├── installer_app.py         # установщик-мастер для коллег
├── instructions/            # правила генерации (промпты, голоса, словарь)
├── refs/                    # референсы — локации / персонажи / объекты (gitignore)
├── output/                  # сгенерированные сториборды (gitignore)
├── scenarios/               # сценарии (gitignore)
└── version.json             # текущая версия
```

## Сборка

- **macOS**: `pyinstaller StoryboardStudio.spec` и `pyinstaller StoryboardStudioInstaller.spec`
- **Windows**: см. [BUILD_WINDOWS.md](BUILD_WINDOWS.md)

## Обновления

Админ нажимает **"📤 Отправить обновление"** в Storyboard Studio — изменения улетают на GitHub. Коллеги видят кнопку обновления при следующем запуске приложения.
