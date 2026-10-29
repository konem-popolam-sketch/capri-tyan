"""Разговор с Капри через arena-api — личный аккаунт Arena вместо локальной модели.

Модуль — ровно та же «розетка», что была у local_ai.LocalAI: main.py и actions.py
зовут ask(), chatter(), summarize_report() и start_server(), а внутри вместо Ollama
теперь arena-api из папки arenaaispofandpars.

Если Arena недоступна (нет сессии, нет интернета, не установлены зависимости),
все методы возвращают None, и Капри, как и раньше, откатывается на поиск в Google.
"""
import datetime
import logging
import os
import random
import sys
import threading
import time

from answer_style import clean, clean_short, looks_broken
from persona import CHARACTER, SPEECH_RULES, user_context

log = logging.getLogger("capri")

BASE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE, "arenaaispofandpars")
STATE = os.path.join(BASE, "arena_state", "cookies.json")

#: Короткое напоминание о характере — в каждом запросе. Полный SYSTEM (весь CHARACTER)
#: уходит один раз, в первое сообщение Direct Chat: дальше сервер помнит разговор сам.
COMPACT = ("Ты — Капри, цифровая собеседница из Найт-Сити, живёшь в компьютере пользователя. "
           "Говоришь живо, с иронией и лёгкой дерзостью, о себе только «я». "
           "Не представляйся и не рассказывай, кто ты, если прямо не спросили.")

TAIL = ("Ответь по-русски, 2–3 предложения, без markdown, списков, эмодзи и ремарок "
        "в звёздочках и скобках.")

SYSTEM = f"""{CHARACTER}

{SPEECH_RULES}
Отвечай на любые вопросы о науке, истории, технике, языке, о себе, советы и просьбы рассказать что-то."""

#: TLS-профили curl_cffi, которые обычно проходят через провайдера. Свежие профили
#: (chrome124+, firefox133+) посылают пост-квантовый ключ X25519MLKEM768, и DPI рвёт
#: соединение с arena.ai (SSL alert handshake failure) — поэтому начинаем с safari и
#: старого chrome. Свой профиль можно задать в config.json: "arena_impersonate".
IMPERSONATE_TRIES = ("safari17_0", "safari15_5", "chrome110", "edge101")

CHATTER_TOPICS = [
    "пожалуйся, что тебе скучно", "подразни пользователя за то, что он долго сидит за компьютером",
    "пошути про то, как тесно жить в компьютере", "сделай комплимент с подколкой",
    "пошути про его рабочий стол или файлы", "скажи что-нибудь загадочное и чуть жутковатое",
    "напомни отдохнуть, но в своём стиле", "похвастайся, что ты быстрее его процессора",
    "вспомни о небе и о том, что хочешь собственное тело",
]


class _LogStream:
    """Поток для служебных сообщений arena-api: у Капри нет консоли, всё идёт в capri.log."""

    def write(self, text):
        text = (text or "").strip()
        if text:
            log.info("%s", text)
        return len(text or "")

    def flush(self):
        pass

    def isatty(self):
        return False


class ArenaAI:
    """Ответы через arena-api. Интерфейс совпадает с прежним LocalAI."""

    TOKEN_TTL = 100.0   # столько секунд живёт подтверждение Arena
    COOKIE_TTL = 600.0  # так часто обновляем cookie из браузера

    def __init__(self, cfg, client=None):
        cfg = cfg or {}
        self.model = str(cfg.get("arena_model") or "").strip()
        self.backend = str(cfg.get("arena_backend") or "auto").strip().lower()
        self.base_url = str(cfg.get("arena_base_url") or "https://arena.ai").strip()
        self.timeout = float(cfg.get("arena_timeout") or 90)
        self.state_path = str(cfg.get("arena_state_path") or STATE)
        self.impersonate = str(cfg.get("arena_impersonate") or "").strip()
        self.show_browser = bool(cfg.get("arena_show_browser"))
        self.channel = str(cfg.get("arena_channel") or "").strip() or None
        self.hide_automation = bool(cfg.get("arena_hide_automation"))
        # Быстрый путь: свой долгоживущий браузер выдаёт подтверждения, а сам
        # запрос уходит обычным HTTP — без запуска Chrome на каждую реплику.
        self.fast = cfg.get("arena_fast", True)
        self.fast = bool(self.fast) if not isinstance(self.fast, str) else self.fast.strip().lower() not in ("0", "false", "no", "")
        self.history = []          # [{"role": ..., "content": ...}] — как у LocalAI
        self.on_model_change = None  # main записывает выбранную модель в config.json
        self.last_error = ""
        self.mode = None           # "gradio" | "direct"
        self._pkg = None
        self._options = None
        self._client = client      # Gradio-клиент (или DirectArenaClient в режиме direct)
        self._direct = None
        self._direct_chat = None
        self._minter = None        # arena_browser.BrowserMinter
        self._fast_client = None   # DirectArenaClient для HTTP-пути
        self._conv_id = None       # id разговора: контекст между репликами
        self._conv_path = os.path.join(os.path.dirname(self.state_path), "conversation_id.txt")
        self._conv_id = self._load_conv()
        self._fast_broken = False  # после нескольких ошибок откатываемся на старый путь
        self._fast_failures = 0
        self._token = None         # (подтверждение,monotonic) — живёт TOKEN_TTL
        self._cookies_ts = 0.0     # когда последний раз забирали cookie из браузера
        self._lock = threading.Lock()
        self._gradio_failures = 0
        self._retry_at = 0.0       # раньше этого времени не пробуем подключаться заново

    # ---------- подключение ----------

    @staticmethod
    def installed():
        """Есть ли рядом папка arenaaispofandpars с модулем arena_api."""
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        try:
            import arena_api  # noqa: F401
            return True
        except Exception as e:
            log.info("arena-api не загрузилась: %s", e)
            return False

    @property
    def available(self):
        return self._client is not None or self._direct is not None

    def _import(self):
        if self._pkg is not None:
            return self._pkg
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        try:
            import arena_api
        except ImportError as e:
            self.last_error = (f"не хватает библиотеки ({e.name or e}) — запусти setup_ai.bat "
                               "или: pip install httpx[http2]")
            log.info("arena-api: %s", self.last_error)
            return None
        except Exception as e:  # noqa: BLE001 — модуль есть, но сломан
            self.last_error = f"arena-api не загрузилась: {e}"
            log.info("%s", self.last_error)
            return None
        self._pkg = arena_api
        return arena_api

    def _make_options(self, pkg, impersonate=None):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        extra = {}
        if impersonate:
            extra["impersonate"] = impersonate
        if self.channel:
            # "chrome" — поднимать настоящий установленный Chrome, а не встроенный Chromium:
            # в нём и профиль с твоим входом, и меньше следов автоматизации.
            extra["channel"] = self.channel
        if self.hide_automation:
            # В окне входа Arena не должно быть флагов автоматизации, иначе Google
            # не даёт ввести почту (поле очищается на «Продолжить»).
            extra["hide_automation"] = True
        return pkg.AutoOptions(
            base_url=self.base_url,
            storage_path=self.state_path,
            artifacts_dir=os.path.join(os.path.dirname(self.state_path), "artifacts"),
            interactive=False,          # у Капри нет консоли: ждать Enter некому
            prompt=lambda _message: "",
            verify=True,
            solve_captchas="auto",
            show_browser=self.show_browser,  # True — видно окно, которым Arena проверяет запрос
            output=_LogStream(),
            **extra,
        )

    def _profiles(self):
        """TLS-профили для перебора: свой из config.json — единственным, иначе запасные."""
        if self.impersonate:
            return [self.impersonate]
        return list(IMPERSONATE_TRIES)

    @staticmethod
    def profile_session_until(state_path):
        """До какого времени в профиле Капри есть вход в Arena (по сроку cookie).

        Chrome шифрует значения cookie (App-Bound Encryption), но срок годности
        лежит открытым номером — его и читаем. Возвращает unix-время или None.
        """
        db = os.path.join(os.path.dirname(state_path), "direct_browser_profile",
                          "Default", "Network", "Cookies")
        if not os.path.exists(db):
            return None
        try:
            import sqlite3
            uri = "file:" + db.replace(os.sep, "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                rows = con.execute(
                    "SELECT expires_utc FROM cookies WHERE name LIKE 'arena-auth-prod-v1%'"
                ).fetchall()
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — база занята или Chrome её ещё не создал
            return None
        for (expires_utc,) in rows:
            if not expires_utc:
                continue
            unix = expires_utc / 1_000_000 - 11644473600  # Chrome считает от 1601 года
            if unix > time.time() + 60:
                return unix
        return None

    def _connect(self):
        """Создаёт клиент. Возвращает True, если получилось."""
        with self._lock:
            if self.available:
                if self._pkg is None:
                    self._import()  # клиент могли передать снаружи (тесты, свой транспорт)
                return True
            if time.monotonic() < self._retry_at:
                return False
            pkg = self._import()
            if pkg is None:
                self._retry_at = time.monotonic() + 60
                return False
            until = self.profile_session_until(self.state_path)
            if until and not self.show_browser:
                log.info("arena-api: вход в Arena в профиле есть (до %s)",
                         time.strftime("%d.%m %H:%M", time.localtime(until)))
            elif until is None and not self.show_browser:
                # Входа нет: без видимого окна пользователь не сможет его сделать.
                self.show_browser = True
                log.info("arena-api: входа в Arena нет — открываю видимое окно браузера. "
                         "Войди в Arena один раз; дальше окно можно спрятать "
                         "(config.json \"arena_show_browser\": false)")
            if self._options is None:
                self._options = self._make_options(pkg)
            errors = []
            # Direct Chat первым: Gradio-протокола на arena.ai больше нет
            # (вместо JSON /config отдаёт HTML-страницу).
            modes = ["direct", "gradio"] if self.backend not in ("gradio", "direct") else [self.backend]
            for mode in modes:
                for profile in self._profiles():
                    options = self._options
                    if options is None or getattr(options, "impersonate", None) != profile:
                        options = self._make_options(pkg, profile)
                    try:
                        if mode == "gradio":
                            client = pkg.auto_client(options)
                        else:
                            client = pkg.auto_direct_client(options, verification_token="auto")
                    except Exception as e:  # noqa: BLE001 — пробуем следующий профиль/режим
                        errors.append(f"{mode}/{profile or 'auto'}: {e}")
                        log.info("arena-api: режим %s, TLS-профиль %s не подошёл — %s",
                                 mode, profile or "auto", e)
                        continue
                    self._options = options
                    self.impersonate = profile
                    if mode == "gradio":
                        self._client = client
                        self.mode = "gradio"
                    else:
                        self._direct = client
                        self.mode = "direct"
                    self.last_error = ""
                    log.info("arena-api: подключилась (%s, TLS-профиль %s, %s)",
                             mode, profile or "auto", self.base_url)
                    return True
            self.last_error = self._explain(errors)
            self._retry_at = time.monotonic() + 60  # не долбим Arena на каждую фразу
            log.info("arena-api недоступна: %s", self.last_error)
            return False

    def _explain(self, errors):
        text = " | ".join(errors) or "неизвестная ошибка"
        low = text.lower()
        if "no session found" in low or "нет сессии" in low:
            return ("нет сессии Arena — войди на arena.ai в своём браузере (Firefox или Chrome) "
                    "или запусти setup_ai.bat и войди в открывшемся окне")
        if "handshake" in low or "ssl" in low or "tls" in low:
            return (f"провайдер рвёт TLS-соединение с Arena (похоже на DPI): включи zapret или VPN. "
                    f"Если уже включён — впиши в config.json \"arena_impersonate\" один из профилей "
                    f"({', '.join(IMPERSONATE_TRIES)}) или пустую строку, чтобы Капри перебрала их сама ({text})")
        if "proxy" in low or "connect" in low or "timed out" in low or "timeout" in low:
            return f"нет связи с {self.base_url} — проверь интернет или VPN ({text})"
        if "playwright" in low:
            return f"для входа нужен Playwright: {text}"
        return text

    def start_server(self):
        """Фоновое подключение при запуске Капри (main зовёт в отдельном потоке)."""
        try:
            self._connect()
        except Exception as e:  # noqa: BLE001 — Капри должна запуститься в любом случае
            self.last_error = str(e) or e.__class__.__name__
            log.info("arena-api: не удалось подключиться — %s", self.last_error)

    def _stop_minter(self):
        if self._minter is not None:
            try:
                self._minter.close()
            except Exception:  # noqa: BLE001
                pass
            self._minter = None
            self._fast_client = None

    def close(self):
        self._stop_minter()
        for client in (self._client, self._direct):
            try:
                if client is not None:
                    client.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- запросы ----------

    def _now(self):
        return datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

    def _prompt(self, question):
        return f"{COMPACT}\n{user_context()}\nСейчас {self._now()}.\n\n{question}\n\n[{TAIL}]"

    def _system_prompt(self, question):
        """Первое сообщение Direct Chat: там весь характер, дальше хватает COMPACT."""
        return f"{SYSTEM}\n{user_context()}\nСейчас {self._now()}.\n\n{question}\n\n[{TAIL}]"

    def _chat_gradio(self, prompt, history, timeout=None):
        pkg = self._pkg
        messages = [pkg.Message(m["role"], m["content"]) for m in history]
        config = pkg.GenerationConfig(queue_timeout=timeout or self.timeout)
        chunks = self._client.stream(prompt, model=self.model or None, config=config, history=messages)
        return "".join(chunk.delta for chunk in chunks)

    def _direct_client(self):
        if self._direct is None:
            pkg = self._import()
            if pkg is None:
                raise RuntimeError(self.last_error or "arena-api не загрузилась")
            if self._options is None:
                self._options = self._make_options(pkg, self.impersonate)
            self._direct = pkg.auto_direct_client(self._options, verification_token="auto")
        return self._direct

    def _direct_model(self, client):
        """Какая модель Direct Chat: из настроек, иначе лучшая доступная (и запоминаем)."""
        if self.model:
            return self.model
        models = [m for m in client.get_models() if m.available]
        if not models:
            raise RuntimeError("Arena не отдала список доступных моделей")
        try:
            from arena_api.direct import model_chat_score
            best = min(models, key=model_chat_score)
        except Exception:  # noqa: BLE001 — приватная эвристика недоступна, берём первую
            best = models[0]
        self.model = best.id
        log.info("arena-api: выбрана модель %s (%s)", best.id, best.display_name or "")
        if self.on_model_change:
            self.on_model_change(best.id)
        return best.id

    def _chat_direct(self, prompt, history, fresh=False, timeout=None):
        client = self._direct_client()
        config = self._pkg.GenerationConfig(queue_timeout=timeout or self.timeout)
        started = time.monotonic()
        last = None
        for attempt in (1, 2):
            try:
                if fresh:
                    # отдельный разговор, чтобы реплика «ни о чём» не оседала в основном контексте
                    return client.chat(model=self._direct_model(client), config=config).send_message(prompt)
                if self._direct_chat is None:
                    # контекст разговора сервер помнит сам по conversation_id
                    self._direct_chat = client.chat(model=self._direct_model(client), config=config)
                answer = self._direct_chat.send_message(prompt)
                # Каждый запрос поднимает свой браузер: разговор из предыдущего
                # ответа к следующему уже недействителен (Arena отвечает 404
                # «session to be validated»), поэтому следующий начинаем сразу
                # с нового — без заведомо провального первого захода.
                if not fresh:
                    self._direct_chat = None
                log.info("arena-api: ответ за %.1f с", time.monotonic() - started)
                return answer
            except Exception as e:  # noqa: BLE001 — один повтор на медленный первый пуск и на протухший разговор
                last = e
                text = str(e).lower()
                stale = "not found" in text or "session to be validated" in text
                slow = "timed out" in text
                if attempt == 2 or not (slow or stale):
                    raise
                if stale:
                    # Arena потеряла разговор (бывает после входа/выхода из аккаунта).
                    # Начинаем новый — тогда вместо «я не знаю» будет нормальный ответ.
                    log.info("arena-api: разговор Arena протух (%s), начинаю новый", e)
                else:
                    # Первый запрос поднимает скрытый браузер и открывает arena.ai: на холодную
                    # это дольше таймаута. Браузер уже запущен — повтор отвечает быстро.
                    log.info("arena-api: первый ответ не успел (%.0f с), повторяю запрос — %s",
                             timeout or self.timeout, e)
                self._direct_chat = None
                time.sleep(2)
        raise last

    # ---------- быстрый путь: один браузер на все запросы ----------

    def _load_conv(self):
        """Разговор Arena переживает перезапуск Капри: дописываем в тот же тред."""
        try:
            with open(self._conv_path, encoding="utf-8") as fh:
                value = fh.read().strip()
            return value or None
        except OSError:
            return None

    def _save_conv(self):
        if not self._conv_id:
            return
        try:
            with open(self._conv_path, "w", encoding="utf-8") as fh:
                fh.write(self._conv_id)
        except OSError as e:
            log.info("arena-api: не удалось сохранить id разговора (%s)", e)

    def _forget_conv(self):
        self._conv_id = None
        try:
            os.remove(self._conv_path)
        except OSError:
            pass

    def _minter_obj(self):
        """Долгоживущий браузер для подтверждений (поднимается при первом запросе)."""
        if self._minter is None:
            import arena_browser
            self._minter = arena_browser.BrowserMinter(
                profile_dir=os.path.join(os.path.dirname(self.state_path),
                                         "direct_browser_profile"),
                page_url=self.base_url.rstrip("/") + "/text/direct",
                channel=self.channel,
                state_path=self.state_path,
                show_window=self.show_browser,
                log=log,
            )
        return self._minter

    def _http_client(self):
        """Клиент Direct Chat без браузера: подтверждение передаём строкой."""
        if self._fast_client is None:
            pkg = self._pkg
            options = self._make_options(pkg, self.impersonate or None)
            self._fast_client = pkg.auto_direct_client(options)
        return self._fast_client

    def _assertion(self, fresh=False):
        """Подтверждение Arena: держим не дольше TOKEN_TTL, чтобы не тратить секунды."""
        if not fresh and self._token and time.monotonic() - self._token[1] < self.TOKEN_TTL:
            return self._token[0]
        started = time.monotonic()
        token = self._minter_obj().assertion()
        self._token = (token, time.monotonic())
        log.info("arena-api: подтверждение Arena получено за %.1f с", time.monotonic() - started)
        return token

    def _refresh_cookies(self, force=False):
        """cf_clearance и прочие cookie обновляем не чаще раза в COOKIE_TTL."""
        now = time.monotonic()
        if not force and now - self._cookies_ts < self.COOKIE_TTL:
            return
        self._minter_obj().save_cookies()
        self._cookies_ts = now

    def _chat_fast(self, prompt, timeout=None):
        pkg = self._pkg
        client = self._http_client()
        config = pkg.GenerationConfig(queue_timeout=timeout or self.timeout)
        model = self._direct_model(client)
        started = time.monotonic()
        fresh_token = False
        for attempt in (1, 2, 3):
            token = self._assertion(fresh=fresh_token)
            self._refresh_cookies(force=fresh_token and attempt > 1)
            follow_up = bool(self._conv_id)
            try:
                chunks = client.stream(
                    prompt,
                    model=model,
                    verification_token=token,
                    config=config,
                    conversation_id=self._conv_id,
                    follow_up=follow_up,
                )
                answer = "".join(chunk.delta for chunk in chunks)
            except Exception as e:  # noqa: BLE001 — Arena могла забыть разговор
                text = str(e).lower()
                stale = (
                    "not found" in text
                    or "session to be validated" in text
                    or "already exists" in text
                )
                auth = (
                    "401" in text
                    or "403" in text
                    or "unauthor" in text
                    or "verification" in text
                    or "recaptcha" in text
                    or "captcha" in text
                )
                if stale and self._conv_id:
                    log.info("arena-api: тред Arena не принят (%s), начинаю новый", e)
                    self._forget_conv()
                    continue
                if auth and not fresh_token:
                    log.info("arena-api: проверка не принята (%s), беру новую", e)
                    fresh_token = True
                    self._token = None
                    continue
                raise
            self._conv_id = getattr(client, "conversation_id", None) or self._conv_id
            self._save_conv()
            log.info("arena-api: ответ за %.1f с (без запуска браузера)",
                     time.monotonic() - started)
            return answer
        raise RuntimeError("Arena не приняла запрос ни в старом, ни в новом разговоре")

    def _complete(self, prompt, history=(), fresh=False, timeout=None):
        """Один запрос. Direct Chat первым, Gradio — только как запасной путь."""
        if not self._connect():
            return None
        if self.mode == "direct":
            if self.fast and not self._fast_broken and not fresh:
                try:
                    answer = self._chat_fast(prompt, timeout=timeout)
                    self._fast_failures = 0
                    return answer
                except Exception as e:  # noqa: BLE001 — откат на путь через браузер
                    self._fast_failures += 1
                    if self._fast_failures >= 3:
                        self._fast_broken = True
                    log.info("arena-api: быстрый путь не сработал (%s), отвечаю через браузер", e)
                    # Библиотеке нужен этот же профиль Chrome — свой браузер закрываем.
                    self._stop_minter()
            return self._chat_direct(prompt, history, fresh=fresh, timeout=timeout)
        try:
            answer = self._chat_gradio(prompt, history, timeout=timeout)
            self._gradio_failures = 0
            return answer
        except Exception as e:  # noqa: BLE001 — Gradio мог отвалиться, пробуем Direct
            self._gradio_failures += 1
            log.info("arena-api: Gradio не ответил (%s), отвечаю через Direct Chat", e)
            if self._gradio_failures >= 2:  # два раза подряд — дальше сразу Direct
                self.mode = "direct"
                log.info("arena-api: переключаюсь на Direct Chat постоянно")
            return self._chat_direct(prompt, history, fresh=fresh, timeout=timeout)

    # ---------- то, что зовёт Капри ----------

    def ask(self, question):
        """Текст ответа или None, если лучше открыть поиск."""
        if not self._connect():
            return None
        prompt = self._prompt(question)
        if self.mode == "direct" and self._direct_chat is None:
            prompt = self._system_prompt(question)
        try:
            raw = self._complete(prompt, self.history)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e) or e.__class__.__name__
            log.info("Arena не ответила: %s", self.last_error)
            return None
        if raw is None:
            return None
        answer = clean(raw, question, self.history)
        if looks_broken(answer):
            log.info("Arena вернула пустой или оборванный ответ: %r", (raw or "")[:200])
            return None
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-6:]
        return answer

    def chatter(self):
        """Короткая спонтанная реплика, когда Капри появляется сама. None — если не вышло."""
        prompt = (f"{COMPACT}\n{user_context()}\n\n"
                  "Ты сама решила ненадолго появиться на экране. Скажи одну короткую спонтанную фразу, "
                  f"до пятнадцати слов. Тема: {random.choice(CHATTER_TOPICS)}. "
                  f"Не спрашивай, чем помочь, и не упоминай команды.\n\n[{TAIL}]")
        try:
            raw = self._complete(prompt, fresh=True, timeout=min(self.timeout, 25))
        except Exception as e:  # noqa: BLE001
            log.info("Arena: реплика не получилась — %s", e)
            return None
        return clean_short(raw)

    def summarize_report(self, report):
        prompt = (f"{COMPACT}\n\n"
                  "Вот отчёт диагностики моего ПК. Скажи 2–4 предложения: общее состояние, главные "
                  "проблемы и что сделать. Безвредные ошибки вроде DistributedCOM не раздувай.\n\n"
                  f"{report[:6000]}\n\n[{TAIL}]")
        try:
            raw = self._complete(prompt, fresh=True)
        except Exception as e:  # noqa: BLE001
            log.info("Arena: отчёт не разобрала — %s", e)
            return None
        return clean(raw or "", "отчёт диагностики", []) or None
