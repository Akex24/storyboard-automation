# -*- coding: utf-8 -*-
"""
threads/suggest_outfits.py — короткий вспомогательный тред для Долг 13.

`SuggestOutfitsThread` спрашивает у локального CLI ровно ТРИ варианта
одежды для персонажа в контексте текущего эпизода (опционально + bible).
Возвращает list[str] длиной 3 (или error).

Промпт строжайший:
  • Только одежда: верх, низ, обувь.
  • С деталями цвета и материала.
  • Каждый вариант ПОЛНЫЙ (верх+низ+обувь).
  • ЗАПРЕЩЕНО: эмоции, состояние, взгляд, поза, прическа, лицо, борода,
    фон, действие, освещение, ракурс, что персонаж делает.
  • Формат ответа: ровно 3 строки, разделитель `|||`. Без префиксов,
    без нумерации, без markdown.

Используется кнопкой «🎨 Сгенерировать» на character-карточке в чате
эпизода (вместо AutonomousGenThread, который для character'ов не
работает). Юзер выбирает один из вариантов → текст идёт префиллом в
CreateActorRefDialog на вкладке «Актёры».
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal


class _AppProxy:
    """Прокси к module storyboard_app — приоритет __main__."""
    def __getattr__(self, name):
        import sys
        main_mod = sys.modules.get('__main__')
        if main_mod is not None and hasattr(main_mod, name):
            return getattr(main_mod, name)
        import storyboard_app
        return getattr(storyboard_app, name)


_sa = _AppProxy()


# Жёсткий лимит чтобы не отправлять весь сценарий целиком — берём
# первые ~6000 символов (этого хватает чтобы AI понял жанр и контекст,
# а не упирался в стоимость токенов).
_SCENARIO_MAX_CHARS = 6000
_BIBLE_MAX_CHARS = 4000


def _read_truncated(path: Path, limit: int) -> str:
    """Читает первые `limit` символов файла. Если файла нет — пустая строка."""
    try:
        if not path.exists() or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= limit:
            return text
        return text[:limit] + "\n... [текст обрезан]\n"
    except Exception:
        return ""


def build_outfit_prompt(name: str, scenario_text: str = "",
                        bible_text: str = "",
                        chat_description: str = "",
                        previous_variants: Optional[List[str]] = None) -> str:
    """Собирает промпт для AI: контекст + железные правила формата.

    2026-05-07: `previous_variants` — список ранее показанных вариантов.
    На retry («Ещё 3 варианта») передаём накопленный список — AI обязан
    предложить кардинально другие, иначе детерминированно повторяет
    одно и то же при одинаковом промпте.

    2026-05-10 (БАГ 4 fix): `chat_description` — текст из манифеста
    агента в чате эпизода для этого character'а (то что в скобках +
    хвост строки `- ✗ mark (Марк — любовник Лоры) — ... Одежда сцен
    7-8 — обнажённый торс / простыня по пояс`). Содержит per-scene
    outfit notes которых в сценарии может не быть. Передаётся как
    отдельный context-блок с приоритетом над эвристиками."""
    ctx_blocks = []
    if bible_text.strip():
        ctx_blocks.append("=== БИБЛИЯ СЕРИАЛА (фрагмент) ===\n"
                          + bible_text.strip())
    if scenario_text.strip():
        ctx_blocks.append("=== СЦЕНАРИЙ ЭПИЗОДА (фрагмент) ===\n"
                          + scenario_text.strip())
    if chat_description.strip():
        # 2026-05-11 (БАГ 4 retry): override-сообщение поднято в НАЧАЛО
        # context'а — чтобы AI увидел его ДО правил «верх+низ+обувь»
        # ниже. Раньше override был внизу, AI слушал более жёсткое
        # правило про обязательные верх+низ+обувь и игнорировал
        # bed-scene заметки.
        ctx_blocks.insert(0,
            "=== 🔴 ПРИОРИТЕТ #1 — КОНТЕКСТ ОТ АГЕНТА ===\n"
            "Этот блок ЯВНО указывает что персонаж носит (или НЕ "
            "носит) в конкретных сценах. Это OVERRIDE любых эвристик "
            "из bible/scenario и любых дефолтных правил «верх+низ+обувь» "
            "ниже:\n\n"
            + chat_description.strip()
            + "\n\nЕсли в КОНТЕКСТЕ выше явно указана одежда (или её "
            "отсутствие — «обнажённый торс», «голый», «в простыне», "
            "«в кровати») — БУКВАЛЬНО используй эту информацию для "
            "вариантов. НЕ выдумывай «верх+низ+обувь» если в сцене "
            "персонаж без рубашки или босиком.")
    context = ("\n\n".join(ctx_blocks)).strip() or \
        "(контекст эпизода недоступен — придумай универсальные варианты)"

    # 2026-05-07: блок «уже предложено» — добавляется только если есть
    # список. Заставляет AI генерировать новые варианты вместо повтора.
    avoid_block = ""
    if previous_variants:
        bullets = "\n".join(
            f"  • {v}" for v in previous_variants if v and v.strip())
        if bullets:
            avoid_block = (
                f"\n⛔ ЭТИ ВАРИАНТЫ УЖЕ БЫЛИ ПРЕДЛОЖЕНЫ РАНЕЕ — "
                f"НЕ ПОВТОРЯЙ ИХ И НЕ ПРЕДЛАГАЙ ПОХОЖИЕ:\n"
                f"{bullets}\n\n"
                f"Дай КАРДИНАЛЬНО ДРУГИЕ 3 варианта. Используй другие "
                f"стили (офис / casual / спорт / вечер / домашнее / "
                f"рабочая униформа и т.д.), другие цвета, другие "
                f"силуэты, другие материалы. Не повторяй ни одну из "
                f"вышеперечисленных комбинаций.\n"
            )

    return (
        f"Ты подбираешь варианты одежды персонажа «{name}» для съёмки.\n\n"
        f"{context}\n"
        f"{avoid_block}"
        f"ЗАДАЧА: Дай ровно 3 варианта одежды персонажа «{name}» подходящих "
        f"под этот эпизод/контекст. Каждый вариант — одна короткая фраза "
        f"(на русском).\n\n"
        f"СОДЕРЖАНИЕ ВАРИАНТА — ТОЛЬКО ОДЕЖДА (СТАНДАРТНЫЙ ФОРМАТ):\n"
        f"  • верх (рубашка/футболка/свитер/куртка/пальто и т.д.)\n"
        f"  • низ (брюки/джинсы/юбка/шорты и т.д.)\n"
        f"  • обувь (ботинки/кроссовки/туфли и т.д.)\n"
        f"С деталями цвета и материала — НЕ «джинсы», а «синие джинсы» или "
        f"«черные потертые джинсы». НЕ «рубашка», а «белая льняная рубашка».\n"
        f"Каждый из 3 вариантов ОБЯЗАТЕЛЬНО содержит верх + низ + обувь. "
        f"Ничего не пропускай.\n\n"
        f"🔴 ИСКЛЮЧЕНИЕ — bed scenes / голый торс / в простыне:\n"
        f"  Если в КОНТЕКСТ ОТ АГЕНТА (см. блок выше) явно указано\n"
        f"  отсутствие части одежды (обнажённый торс, голый, в простыне,\n"
        f"  в кровати, без рубашки) — НЕ ВЫДУМЫВАЙ верх/обувь.\n"
        f"  Структура варианта МЕНЯЕТСЯ — выдавай описание визуальной\n"
        f"  картины. Каждый из 3 вариантов всё равно ОБЯЗАН быть\n"
        f"  РАЗНООБРАЗНЫМ (не три одинаковых «голый торс + простыня»):\n"
        f"    • Вариант 1 — буквально по описанию (например «обнажённый\n"
        f"      торс, простыня по пояс, босиком»).\n"
        f"    • Вариант 2 — альтернативное прочтение в рамках того же\n"
        f"      состояния («в чёрных боксерах, простыня смята рядом,\n"
        f"      босиком»).\n"
        f"    • Вариант 3 — ещё одна интерпретация той же сцены\n"
        f"      («белая майка-сетка тонкая, простыня обмотана вокруг\n"
        f"      бёдер, босиком»).\n"
        f"  Все три остаются «в кровати полураздет» но различаются\n"
        f"  деталями.\n"
        f"  Если в контексте указана КОНКРЕТНАЯ одежда («в белом халате»,\n"
        f"  «в кожаной куртке») — буквально перенеси её в один из\n"
        f"  вариантов, остальные два варьируй (другой цвет, материал,\n"
        f"  крой) в РАМКАХ той же одежды.\n\n"
        f"🔴 СТРОГО ЗАПРЕЩЕНО упоминать:\n"
        f"  • эмоции и состояние («усталый», «отстранённый», «собранный»)\n"
        f"  • взгляд и куда смотрит\n"
        f"  • позу (стоит/сидит/лежит/руки скрещены и т.п.)\n"
        f"  • прическу, бороду, лицо, черты внешности\n"
        f"  • фон, локацию, освещение, ракурс\n"
        f"  • действие — что персонаж делает\n"
        f"  • аксессуары если они привязаны к действию (телефон в руке,\n"
        f"    сигарета во рту). Аксессуары как часть образа можно (часы,\n"
        f"    цепочка, очки) но без позы.\n\n"
        f"ФОРМАТ ОТВЕТА (СТРОГО): ровно 3 варианта, разделитель `|||`, "
        f"без нумерации, без префиксов, без markdown, без кавычек, без "
        f"пояснений. Никаких других строк в ответе.\n\n"
        f"ПРИМЕР ХОРОШЕГО ОТВЕТА:\n"
        f"темно-серая водолазка, синие джинсы, черные кожаные ботинки ||| "
        f"белая льняная рубашка с расстегнутым воротом, черные классические "
        f"брюки, коричневые броги ||| черная толстовка с капюшоном, серые "
        f"потертые джинсы, белые кеды\n\n"
        f"Теперь дай ответ в этом формате."
    )


def parse_outfit_response(raw: str) -> List[str]:
    """Парсит вывод AI в список вариантов. Возвращает [] если не получилось."""
    if not raw:
        return []
    # AI может обернуть в code-block — снимем.
    text = raw.strip()
    # Удаляем возможные ```...``` обёртки
    if text.startswith("```"):
        # отбрасываем первую и последнюю строки если это ```
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Если AI всё-таки выдал несколько строк без `|||` — попробуем
    # разделить по новой строке (на случай отклонения от формата).
    if "|||" in text:
        parts = [p.strip() for p in text.split("|||")]
    else:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
    # Чистим маркеры списка («1.», «-», «•») в начале строк
    cleaned: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Снять возможные обрамляющие кавычки
        if (p.startswith('«') and p.endswith('»')) or \
           (p.startswith('"') and p.endswith('"')) or \
           (p.startswith('"') and p.endswith('"')):
            p = p[1:-1].strip()
        # Снять возможный префикс «1. », «1) », «- », «• »
        for pref in ("1.", "2.", "3.", "1)", "2)", "3)", "-", "•", "*"):
            if p.startswith(pref):
                p = p[len(pref):].lstrip(" \t")
                break
        if p:
            cleaned.append(p)
    return cleaned[:3]


class SuggestOutfitsThread(QThread):
    """Headless `claude -p` для получения 3 вариантов одежды.

    Сигналы:
      • results(list) — список из 3 строк с вариантами
      • error(str) — короткое сообщение об ошибке
      • stopped() — после явной остановки
    """
    results = pyqtSignal(list)
    error = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, project_root: Path, character_name: str,
                 ep_id: Optional[str] = None,
                 show_slug: Optional[str] = None,
                 model: Optional[str] = None,
                 previous_variants: Optional[List[str]] = None,
                 chat_description: str = "",
                 parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.character_name = character_name
        self.ep_id = ep_id
        self.show_slug = show_slug
        self.model = model
        # 2026-05-07: список ранее показанных вариантов для текущего
        # персонажа. На retry («Ещё 3 варианта») передаётся в промпт.
        self.previous_variants = list(previous_variants or [])
        # 2026-05-10 (БАГ 4 fix): rich description от агента из
        # манифеста чата для этого character'а. Содержит per-scene
        # outfit notes (пример: «Одежда сцен 7-8 — обнажённый торс /
        # простыня по пояс в кровати»). См. build_outfit_prompt.
        self.chat_description = chat_description or ""
        self._proc: Optional[subprocess.Popen] = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _load_context(self) -> tuple[str, str]:
        """Возвращает (scenario_text, bible_text). Любой может быть пустым."""
        scenario = ""
        bible = ""
        if not self.show_slug:
            return ("", "")
        show_root = self.project_root / "shows" / self.show_slug
        # 2026-05-10: zero-pad ep{NN:02d}.txt — single source of truth.
        # _active.txt fallback УБРАН (stale, разъезжался с UI-эпизодом).
        if self.ep_id:
            num_str = self.ep_id.lstrip('ep')
            if num_str.isdigit():
                ep_path = (show_root / "scenarios"
                           / f"ep{int(num_str):02d}.txt")
            else:
                ep_path = show_root / "scenarios" / f"{self.ep_id}.txt"
            scenario = _read_truncated(ep_path, _SCENARIO_MAX_CHARS)
        # Библия
        bible = _read_truncated(show_root / "bible.txt", _BIBLE_MAX_CHARS)
        return (scenario, bible)

    def run(self):
        cli = _sa.find_claude_cli()
        if not cli:
            self.error.emit("claude_cli_not_found")
            return
        try:
            scenario_text, bible_text = self._load_context()
            prompt = build_outfit_prompt(
                self.character_name, scenario_text, bible_text,
                chat_description=self.chat_description,
                previous_variants=self.previous_variants)
            args = [cli]
            if self.model:
                args += ["--model", self.model]
            args += ["-p", prompt, "--dangerously-skip-permissions"]
            # 2026-05-08: CREATE_NO_WINDOW guard для Win10/11 (см.
            # _WINDOWS_PREP_TODO.md P0).
            popen_kwargs = dict(
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",      # 2026-05-09 Win-fix.
                errors="replace",
                bufsize=1,
            )
            if sys.platform == 'win32':
                popen_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(args, **popen_kwargs)
            assert self._proc.stdout is not None
            buf = self._proc.stdout.read()
            rc = self._proc.wait(timeout=10)
            if self._stop_requested:
                self.stopped.emit()
                return
            variants = parse_outfit_response(buf or "")
            if rc != 0 and not variants:
                self.error.emit(
                    f"exit {rc}: {(buf or '').strip()[:200]}")
                return
            if len(variants) < 3:
                # AI не дал три варианта — отдадим что есть (минимум 1),
                # либо ошибку если совсем пусто.
                if not variants:
                    self.error.emit("empty")
                    return
            self.results.emit(variants)
        except Exception as e:
            if self._stop_requested:
                self.stopped.emit()
                return
            self.error.emit(str(e)[:500])
