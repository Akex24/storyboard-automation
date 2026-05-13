# -*- coding: utf-8 -*-
"""
threads/proxy_test.py — тестирование прокси-настроек.

ProxyTestThread делает три независимых GET-запроса (GitHub / Fast Gen /
ip-api.com) с заданным таймаутом 10с, замеряет время каждого и собирает
результат в единый dict для UI Settings.

Два режима:
  • use_proxy=True  — все 3 запроса с proxies={"http": ..., "https": ...}
    Результат геолокации показывает IP/город прокси-сервера.
  • use_proxy=False — все 3 запроса БЕЗ параметра proxies (прямое подключение).
    Результат геолокации показывает реальный IP/город компьютера.

Сервис геолокации: ip-api.com (бесплатный tier, 45 req/min, HTTP-only).
Заменили ipinfo.io в v1.0.65 hotfix — он давал ложные результаты для
СНГ/Восточной Европы (украинский прокси показывался как Нидерланды).
ip-api.com отдаёт полное название страны на английском и более точные
данные для региона.

Запросы выполняются ПОСЛЕДОВАТЕЛЬНО (не parallel) — простота кода важнее
скорости. Полный прогон при таймауте 10с × 3 = максимум 30с в худшем
случае. На рабочем прокси — 1-3 секунды.

Каждый запрос обёрнут в try/except и НЕ роняет тред — частичные ошибки
(один endpoint лежит, другие работают) показываются по строкам ✓/✗.

Cross-platform: только requests + stdlib, без subprocess. Mac и Win
одинаково. Cert handling — через REQUESTS_CA_BUNDLE / SSL_CERT_FILE
которые Studio устанавливает в os.environ при старте (storyboard_app.py).

История: создано 2026-05-13 (v1.0.65, фича proxy settings).
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import requests
from PyQt6.QtCore import QThread, pyqtSignal


TEST_TIMEOUT_SEC = 10
TEST_URL_GITHUB = "https://api.github.com/zen"
TEST_URL_FASTGEN = "https://googler.fast-gen.ai/"
# v1.0.65 hotfix: ip-api.com вместо ipinfo.io. Бесплатный tier ipinfo.io
# на тесте 2026-05-13 показал ложную геолокацию украинского прокси
# 91.124.69.171 → «Lelystad, Нидерланды (Cogent Communications)».
# ip-api.com и другие сервисы корректно определяют этот IP как Киев,
# Украина. ip-api.com — точнее по СНГ/Восточной Европе, отдаёт полные
# названия стран на английском (нет нужды в country_names таблице).
#
# Ограничения ip-api.com бесплатного tier:
#   - 45 запросов/минуту (нам хватает — один запрос на клик).
#   - HTTP-only (не HTTPS). Это нормально — мы запрашиваем только
#     геолокацию СВОЕГО IP, никаких приватных данных не шлём. SSL
#     для этого вызова не критичен.
# Запрашиваем минимум полей через ?fields=... для скорости и
# защиты от изменений в их default response.
TEST_URL_GEO = "http://ip-api.com/json/?fields=status,message,country,countryCode,city,isp,org,query"


class ProxyTestThread(QThread):
    """Тестирует доступность 3 endpoint'ов с прокси или без.

    Сигнал `result_ready(dict)` отдаёт результат:
        {
          "use_proxy": bool,
          "github":  {"ok": bool, "time_ms": int, "error": str|None},
          "fastgen": {"ok": bool, "time_ms": int, "error": str|None},
          "geo":     {"ok": bool, "country": str, "country_code": str,
                      "city": str, "org": str, "ip": str, "error": str|None}
        }
    """

    result_ready = pyqtSignal(dict)

    def __init__(self, use_proxy: bool,
                 host: str = "", port: str = "",
                 username: str = "", password: str = "",
                 parent=None):
        super().__init__(parent)
        self._use_proxy = bool(use_proxy)
        self._host = (host or "").strip()
        self._port = (port or "").strip()
        self._username = (username or "").strip()
        self._password = password or ""  # пароль не trim'им (могут быть пробелы)

    # ──────────────────────────────────────────────────────────────────

    def _build_proxies(self) -> Optional[Dict[str, str]]:
        """Формирует proxies dict для requests или None для direct."""
        if not self._use_proxy:
            return None
        if not (self._host and self._port):
            return None
        if self._username and self._password:
            url = f"http://{self._username}:{self._password}@{self._host}:{self._port}"
        else:
            url = f"http://{self._host}:{self._port}"
        return {"http": url, "https": url}

    def _do_get(self, url: str, proxies: Optional[Dict[str, str]]) -> dict:
        """Один GET с замером времени. Не бросает — пишет error в результат."""
        t0 = time.perf_counter()
        try:
            r = requests.get(url, proxies=proxies, timeout=TEST_TIMEOUT_SEC)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            # 2xx/3xx/4xx — всё считается «подключение работает».
            # 5xx тоже не дисквалифицирует — это проблема целевого сервера,
            # не прокси/сети. Главное — мы дошли до сервера.
            return {
                "ok": True,
                "time_ms": elapsed_ms,
                "status_code": r.status_code,
                "body": r.text if len(r.text) < 4096 else r.text[:4096],
                "error": None,
            }
        except requests.exceptions.ProxyError as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": f"proxy error: {str(e)[:120]}"}
        except requests.exceptions.ConnectTimeout:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": "connect timeout"}
        except requests.exceptions.ReadTimeout:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": "read timeout"}
        except requests.exceptions.ConnectionError as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": f"connection error: {str(e)[:120]}"}
        except requests.exceptions.SSLError as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": f"SSL error: {str(e)[:120]}"}
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": False, "time_ms": elapsed_ms,
                    "error": f"{type(e).__name__}: {str(e)[:120]}"}

    def _parse_geo(self, raw_result: dict) -> dict:
        """Преобразует raw ответ ip-api.com в плоский dict для UI.

        Формат ответа ip-api.com:
            {
              "status": "success",
              "country": "Ukraine",          // полное название
              "countryCode": "UA",           // 2-буквенный код
              "city": "Kyiv",
              "isp": "Kyivstar PJSC",        // интернет-провайдер
              "org": "Kyivstar PJSC",        // организация
              "query": "91.124.69.171"       // проверенный IP
            }
        При ошибке: {"status": "fail", "message": "..."}.
        """
        if not raw_result.get("ok"):
            return {"ok": False, "error": raw_result.get("error") or "unknown",
                    "country": "", "country_code": "", "city": "",
                    "org": "", "ip": ""}
        try:
            import json as _json
            data = _json.loads(raw_result.get("body") or "{}")
        except Exception:
            return {"ok": False, "error": "invalid JSON from ip-api.com",
                    "country": "", "country_code": "", "city": "",
                    "org": "", "ip": ""}
        # Проверка status — ip-api.com возвращает {"status": "fail",
        # "message": "..."} при ошибке (private IP, rate limit, etc.).
        if data.get("status") != "success":
            return {
                "ok": False,
                "error": data.get("message") or "geo lookup failed",
                "country": "", "country_code": "", "city": "",
                "org": "", "ip": "",
            }
        # ip-api.com отдаёт полное название страны (Ukraine, Russia, ...)
        # — таблица country_names больше не нужна.
        # «org» совпадает с «isp» в 90% случаев, но isp более точное —
        # предпочитаем его. fallback на org если isp пуст.
        isp = (data.get("isp") or "").strip()
        org = (data.get("org") or "").strip()
        provider = isp or org or "?"
        return {
            "ok": True,
            "error": None,
            "country": (data.get("country") or "").strip() or "?",
            "country_code": (data.get("countryCode") or "").strip() or "?",
            "city": (data.get("city") or "").strip() or "?",
            "org": provider,
            "ip": (data.get("query") or "").strip() or "?",
        }

    # ──────────────────────────────────────────────────────────────────

    def run(self):
        proxies = self._build_proxies()
        result: Dict = {
            "use_proxy": self._use_proxy,
            "github": {},
            "fastgen": {},
            "geo": {},
        }
        # 1. GitHub API zen
        gh = self._do_get(TEST_URL_GITHUB, proxies)
        result["github"] = {"ok": gh["ok"], "time_ms": gh["time_ms"],
                            "error": gh.get("error")}
        # 2. Fast Gen root
        fg = self._do_get(TEST_URL_FASTGEN, proxies)
        result["fastgen"] = {"ok": fg["ok"], "time_ms": fg["time_ms"],
                             "error": fg.get("error")}
        # 3. Геолокация (ipinfo.io)
        geo_raw = self._do_get(TEST_URL_GEO, proxies)
        result["geo"] = self._parse_geo(geo_raw)
        self.result_ready.emit(result)
