"""Долгоживущий браузер Капри: выдаёт подтверждения reCAPTCHA v3 для Direct Chat.

Зачем отдельный модуль. Арена требует на каждый запрос Direct Chat свежее
подтверждение reCAPTCHA v3, а выдают его только в браузере. Библиотека
arena_api поднимает для этого браузер заново на каждую реплику — отсюда 20-30
секунд на ответ. Здесь браузер запускается один раз, страница живёт между
запросами, и один запуск Chrome обслуживает все реплики, пока Капри работает.

Заодно этот браузер держит вход в Arena: профиль тот же, что у Капри
(arena_state/direct_browser_profile), и cookie после каждого подтверждения
переписываются в arena_state/cookies.json — так HTTP-запрос уходит с живой
сессией и свежим cf_clearance.

Важно: Playwright живёт в своём потоке. Sync-API Playwright заводит asyncio-loop
в том потоке, где его запустили, а Капри в главном потоке потом вызывает
asyncio.run() для edge-tts — и получала «asyncio.run() cannot be called from a
running event loop», из-за чего голос переключался на Windows. Поэтому весь
браузер изолирован в отдельном потоке и общается с Капри через очередь.
"""
import json
import os
import queue
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "arenaaispofandpars")

SAME_SITE = {"lax": "Lax", "strict": "Strict", "none": "None"}
START_TIMEOUT = 120.0


class BrowserMinter:
    """Chrome в отдельном потоке, который открывает Direct Chat и даёт токены."""

    def __init__(self, profile_dir, page_url, channel=None, state_path=None,
                 show_window=False, log=None):
        self.profile_dir = profile_dir
        self.page_url = page_url
        self.channel = channel
        self.state_path = state_path
        self.show_window = show_window
        self.log = log
        self._thread = None
        self._queue = None
        self._ready = threading.Event()
        self._start_error = None
        self._lock = threading.Lock()

    # ---------- служебное ----------

    def _say(self, text):
        if self.log:
            self.log.info("arena-api: %s", text)

    # ---------- поток браузера ----------

    def _open(self):
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        from arena_api._direct_page import (  # noqa: WPS433 — ленивый импорт vendor
            _DEFAULT_ARENA_RECAPTCHA_SITE_KEY,
            _build_execute_recaptcha_v3_js,
        )
        from playwright.sync_api import sync_playwright

        self._js = _build_execute_recaptcha_v3_js(_DEFAULT_ARENA_RECAPTCHA_SITE_KEY)
        args = ["--disable-blink-features=AutomationControlled",
                "--no-first-run", "--no-default-browser-check"]
        if not self.show_window:
            # Окно нужно только для проверки — уводим его за край экрана.
            args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
        os.makedirs(self.profile_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                channel=self.channel or None,
                headless=False,             # headless Arena не пускает
                args=args,
                ignore_default_args=["--enable-automation"],
                chromium_sandbox=True,      # иначе Chrome ругается баннером --no-sandbox
                no_viewport=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._pw.stop()
            self._pw = None
            raise RuntimeError(
                "не удалось открыть Chrome с профилем Капри "
                f"({exc!s:.200}) — возможно, окно браузера Капри уже открыто, закрой его"
            ) from exc
        self._ctx.on("page", self._track_page)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.goto(self.page_url, wait_until="domcontentloaded", timeout=60000)
        self._dismiss_consent()
        self._say("браузер проверки открыт (%s)" % self.page_url)

    def _track_page(self, page):
        if page is not None:
            self._page = page

    def _dismiss_consent(self):
        """Снимает баннер cookie — без согласия Arena не догружает проверку."""
        for label in ("Accept Cookies", "Accept all", "Принять все"):
            try:
                button = self._page.get_by_role("button", name=label)
                if button.count() and button.first.is_visible(timeout=1500):
                    button.first.click(timeout=2500)
                    self._say("баннер cookie принят (%s)" % label)
                    return True
            except Exception:  # noqa: BLE001 — баннера может не быть вовсе
                continue
        return False

    def _mint(self, timeout):
        started = time.monotonic()
        last_error = ""
        while time.monotonic() - started < timeout:
            try:
                token = self._page.evaluate(self._js)
            except Exception as exc:  # noqa: BLE001 — страница могла перезагрузиться
                last_error = str(exc).splitlines()[0] if str(exc) else str(exc)
                token = None
            if isinstance(token, str) and token.strip():
                return token.strip()
            self._page.wait_for_timeout(700)
        if last_error:
            raise RuntimeError(f"не получилось подтверждение Arena: {last_error}")
        raise RuntimeError("Arena не выдала подтверждение (страница проверки не готова)")

    def _dump_cookies(self):
        """Свежие cookie для arena_state/cookies.json."""
        if not self.state_path:
            return False
        cookies = self._ctx.cookies()
        payload = []
        for item in cookies:
            domain = str(item.get("domain") or "")
            if "arena" not in domain and "google" not in domain:
                continue
            expires = item.get("expires")
            payload.append({
                "name": item.get("name"),
                "value": item.get("value"),
                "domain": domain,
                "path": item.get("path") or "/",
                "expires": -1 if not expires or expires < 0 else float(expires),
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
                "sameSite": SAME_SITE.get(str(item.get("sameSite") or "").lower(), "Lax"),
            })
        if not payload:
            return False
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cookies": payload, "origins": []}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)
        self._say("cookies Arena обновлены (%d)" % len(payload))
        return True

    def _worker(self):
        queue_ref = self._queue
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001 — ошибку отдаём в главный поток
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        while True:
            job = queue_ref.get()
            if job is None:
                break
            action, payload, result = job
            try:
                if action == "assert":
                    result["value"] = self._mint(payload)
                elif action == "cookies":
                    result["value"] = self._dump_cookies()
            except Exception as exc:  # noqa: BLE001 — ошибку отдаём в главный поток
                result["error"] = exc
            finally:
                result["done"].set()
        for closer in (self._ctx, self._pw):
            try:
                if closer is not None:
                    closer.close() if closer is self._ctx else closer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._page = self._pw = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                pass
            else:
                self._queue = queue.Queue()
                self._ready.clear()
                self._start_error = None
                self._thread = threading.Thread(
                    target=self._worker, name="capri-arena-browser", daemon=True
                )
                self._thread.start()
        if not self._ready.wait(START_TIMEOUT):
            raise RuntimeError("браузер проверки Arena не открылся за %.0f с" % START_TIMEOUT)
        if self._start_error is not None:
            error, self._start_error = self._start_error, None
            self._thread = None
            raise error

    # ---------- публичное API ----------

    def assertion(self, timeout=45.0):
        """Свежий токен reCAPTCHA v3 для POST-запроса Direct Chat."""
        return self._call("assert", timeout, wait=timeout + 30)

    def save_cookies(self):
        """Переписывает arena_state/cookies.json живыми cookie из браузера."""
        try:
            return self._call("cookies", None, wait=45)
        except Exception as exc:  # noqa: BLE001 — cookie не критичны для ответа
            self._say("не удалось обновить cookie: %s" % exc)
            return False

    def _call(self, action, payload, wait):
        self.start()
        result = {"done": threading.Event()}
        self._queue.put((action, payload, result))
        if not result["done"].wait(wait):
            raise RuntimeError("браузер проверки не ответил за %.0f с" % wait)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def close(self):
        thread, queue_ref = self._thread, self._queue
        self._thread = None
        if thread is None or queue_ref is None:
            return
        try:
            queue_ref.put(None)
            thread.join(timeout=15)
        except Exception:  # noqa: BLE001
            pass
