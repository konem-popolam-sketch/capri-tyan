# Правки Капри в этой копии arena_api

Библиотека `arena_api` лежит здесь как есть (Apache-2.0) — не `pip install`. При
обновлении из апстрима правки ниже нужно вернуть.

## 1. `arena_api/_direct_page.py` — не затирать живую сессию Arena

`_PRESERVED_TRUST_COOKIE_PREFIXES` (около строки 50) дополнен `"arena-auth-prod-v1"`.

Зачем: перед загрузкой страницы `_seed_direct_context_cookies` (строка 160) вливает
cookies из `arena_state/cookies.json` через `context.add_cookies(...)`, сохраняя лишь
несколько служебных имён. Токен сессии Arena в этом файле стареет за час, а в профиле
браузера остаётся живой — без правки просроченный токен перетирал рабочий, и Arena
открывалась разлогиненной («Log In or Create Account»).

## 2. `arena_api/auto.py` — засев cookie только через общий сидер

В `_mint_token_on_page` (около строки 1758) вызов заменён на
`_seed_direct_context_cookies(context, seed_cookies, self.options.base_url)`.

Зачем: этот путь раньше вливал cookie из `arena_state/cookies.json` напрямую, без
списка сохраняемых имён, и просроченный `arena-auth-prod-v1` снова ломал вход в
Arena — модалка «Log In or Create Account» появлялась даже при живом токене в профиле.
Теперь обе точки засева ведут себя одинаково (см. пункт 1).

## 3. Настройки, которые использует Капри

* `config.json → arena_channel: "chrome"` — поднимать установленный Chrome (в нём
  профиль с входом), а не встроенный Chromium.
* `config.json → arena_hide_automation: true` — в окне входа убрать флаги
  автоматизации (`--enable-automation`, `navigator.webdriver`), иначе Google очищает
  поле почты при нажатии «Продолжить».
* `config.json → arena_impersonate` — TLS-профиль `curl_cffi`; в России дефолтный
  `chrome` режется DPI на handshake, рабочие: `safari17_0`, `chrome110`, `safari15_5`,
  `edge101`.

## 3. Инструменты в `tools/`

* `seed_browser_profile.py` — копирует вход из Chrome в профиль Капри
  (`arena_state/direct_browser_profile`): `Local State`, `Default/Network/Cookies`,
  `Default/Local Storage`. Chrome должен быть закрыт (`--close-chrome` закроет его).
* `export_browser_session.py` — забирает свежие cookie самим Chrome и пишет
  `arena_state/cookies.json` (Chrome 130+ шифрует cookie App-Bound Encryption, посторонним
  процессам они недоступны без прав администратора).
* `diagnose_arena_browser.py` — печатает, что видит браузер Капри: cookies, срок
  токена, ключи `localStorage`, просит ли страница войти.
* `check_arena.py` — проверка связи с Arena и таймингов (пишет `tools/check_arena.log`).

Всё это запускается одним файлом `fix_arena_login.bat` в корне проекта.
