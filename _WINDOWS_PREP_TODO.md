# 🪟 WINDOWS PREP TODO — что починить ДО релиза для коллег на Win10/11

Этот файл — список **обязательных правок** которые надо сделать перед
тем как собирать `.exe` для коллег. Каждый пункт — потенциально видимый
для юзера баг на Windows. После закрытия пункта — отметить ✅ и записать
дату/коммит.

Ссылки на инструкции:
- `BUILD_WINDOWS.md` — сборка `.exe` (PyInstaller spec).
- Memory: `project_distribution.md` — кросс-платформенность (правило).

---

## 🔴 P0 — критично, без этого коллеги увидят чёрные cmd-окна

### 1. `subprocess.Popen(claude -p)` без `CREATE_NO_WINDOW` на Win

На Windows запуск `subprocess.Popen` с list-args (без `shell=True`)
открывает консольное окно `cmd` для каждого дочернего процесса.
Каждый клик «Сгенерировать» / «🎨 для character» / regen шота → у
коллеги выскакивает чёрный cmd поверх Studio. Образец как делать
правильно — `threads/montage_orchestrator.py:319-323`:

```python
if sys.platform == 'win32':
    CREATE_NO_WINDOW = 0x08000000
    kwargs['creationflags'] = CREATE_NO_WINDOW
```

**Места без CREATE_NO_WINDOW (нашёл 2026-05-07):**

- [x] ✅ 2026-05-08 — `threads/autonomous_gen.py` — `subprocess.Popen` защищён CREATE_NO_WINDOW guard на win32.
- [x] ✅ 2026-05-08 — `threads/suggest_outfits.py` — `subprocess.Popen` защищён.
- [x] ✅ 2026-05-08 — `threads/generate.py` — два места защищены: `ClaudeGeometryThread.run` (subprocess.run) и `RunEpisodeThread.run` (subprocess.Popen).
- [x] ✅ 2026-05-08 — `installer_app.py` — 3 места защищены: Claude install (line 143), Claude auth check (line 198), python --version (line 435).

**Как править (одинаково для всех трёх):**
```python
import sys
import subprocess

popen_kwargs = dict(
    args,
    cwd=str(self.project_root),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)
if sys.platform == 'win32':
    popen_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
self._proc = subprocess.Popen(**popen_kwargs)
```

**Время:** ~10 минут на 3 файла + сборка/smoke + git commit + push.

---

## 🟡 P1 — желательно перед релизом, но не блокер

(пока пусто — добавлять по мере обнаружения)

---

## 🟢 P2 — приятные мелочи

(пока пусто)

---

## История

- **2026-05-07** — файл создан. Юзер напомнил про кросс-платформенность
  (Mac+Win10/11). При проверке сегодняшних правок (Rule 6б, попап
  правки SHOT с двойным полем, попап параллельных гене, per-episode
  outfit/montage) обнаружено что 3 существующих subprocess-вызова не
  имеют CREATE_NO_WINDOW guard. Это исторический пробел — не сегодняшняя
  поломка, но видимая на Win. Юзер сказал «пока оставь, добавь в
  важные правки перед Win-релизом». Записано в P0.
