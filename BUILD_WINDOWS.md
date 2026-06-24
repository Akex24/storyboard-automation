# Сборка Windows .exe — инструкция

Эту инструкцию нужно один раз пройти на любом Windows-компьютере, чтобы получить:
- `Storyboard Studio.exe` — само приложение
- `Storyboard Studio Installer.exe` — установщик для коллег

## Что нужно установить (один раз)

1. **Python 3.10+** — скачать с [python.org](https://www.python.org/downloads/windows/)
   - При установке поставить галочку **"Add Python to PATH"**

2. **Git for Windows** — скачать с [git-scm.com](https://git-scm.com/download/win)

## Сборка

Открой PowerShell или CMD в папке проекта и выполни:

```cmd
pip install PyQt6 Pillow pillow-heif requests pyinstaller
git clone https://github.com/Akex24/storyboard-automation.git
cd storyboard-automation
pyinstaller StoryboardStudio.spec
pyinstaller StoryboardStudioInstaller.spec
```

После сборки в папке `dist/` появятся два файла:
- `Storyboard Studio.exe`
- `Storyboard Studio Installer.exe`

Эти `.exe` можно раздавать коллегам с Windows.
