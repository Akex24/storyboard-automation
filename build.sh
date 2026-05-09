#!/bin/bash
# Сборка Storyboard Studio.app с предварительным запуском smoke-тестов.
#
# Используется Claude вместо прямого вызова pyinstaller — чтобы
# регрессии ловились ДО сборки, а не на этапе «открой и проверь».
#
# Запускать из корня проекта:
#     ./build.sh
#
# Если smoke-тесты упали — сборка не запускается, exit 1.
# Если тесты прошли — стандартная сборка через StoryboardStudio.spec.

set -e

cd "$(dirname "$0")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ШАГ 1/2: Smoke-тесты"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! python3 tests/smoke.py; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✗ Smoke-тесты упали — сборка отменена"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ШАГ 2/2: PyInstaller"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python3 -m PyInstaller StoryboardStudio.spec --noconfirm

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ШАГ 3/3: Пост-сборочный тест запуска"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Запускаю собранный .app на 4 секунды чтобы убедиться"
echo "  что он не падает при старте (ловит ImportError'ы которые"
echo "  не видны в smoke-тестах — например circular import в"
echo "  PyInstaller-frozen окружении)."

APP_BIN="dist/Storyboard Studio.app/Contents/MacOS/Storyboard Studio"
# Отключаем set -e на время теста — мы намеренно убиваем .app по таймеру.
# Иначе SIGTERM от kill (exit 143) валит весь build.sh.
#
# QT_QPA_PLATFORM=offscreen — Qt создаёт QApplication БЕЗ видимого окна.
# Цель: при auto-rebuild из SendUpdateThread юзер не должен видеть как
# на 4 секунды всплывает вторая копия Studio. Тест по-прежнему ловит
# ImportError / init-crash / circular import — это всё происходит ДО
# создания окон. libqoffscreen.dylib забандлен в .app/Contents/Frameworks/
# PyQt6/Qt6/plugins/platforms/ автоматически PyInstaller-хуками.
set +e
QT_QPA_PLATFORM=offscreen "$APP_BIN" > /tmp/storyboard_studio_launch.log 2>&1 &
SPID=$!
sleep 4
if kill -0 $SPID 2>/dev/null; then
    kill $SPID 2>/dev/null
    wait $SPID 2>/dev/null
    set -e
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Готово: dist/Storyboard Studio.app"
    echo "  ✓ .app стартует без падения"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    set -e
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✗ .app УПАЛ при запуске. Лог:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    cat /tmp/storyboard_studio_launch.log
    exit 1
fi
