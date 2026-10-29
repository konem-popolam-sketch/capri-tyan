"""Сообщения в чат Steam.

Имя друга ищем в локальных файлах Steam (сеть не нужна, пароль не нужен):
  <Steam>\\config\\loginusers.vdf                     — какой аккаунт сейчас «последний»
  <Steam>\\userdata\\<accountid>\\config\\localconfig.vdf — список друзей: id → имена (name + NameHistory)
Чат открываем протоколом steam://friends/message/<steamid64>, текст вставляем через буфер обмена
и жмём Enter. Перед печатью проверяем, что открылось окно чата именно с этим человеком.
"""
import difflib
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("capri.steam")

STEAM_ID64_BASE = 76561197960265728
CREATE_NO_WINDOW = 0x08000000

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|([^\s{}"]+)')
_NICK_OK = re.compile(r"[^0-9a-zа-яё]+")


# ---------------------------------------------------------------- VDF

def parse_vdf(text):
    """Простой разбор Valve VDF в дерево dict (строки и вложенные блоки)."""
    stack = [{}]
    key = None
    for m in _TOKEN.finditer(text):
        quoted, brace, bare = m.group(1), m.group(2), m.group(3)
        if quoted is not None:
            if key is None:
                key = quoted
            else:
                stack[-1][key] = quoted
                key = None
        elif brace == "{":
            node = {}
            stack[-1][key] = node
            stack.append(node)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif bare is not None and key is not None:
            stack[-1][key] = bare
            key = None
    return stack[0]


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _node(tree, key):
    """Найти блок по имени на любом уровне вложенности (у VDF всё завёрнуто в корневой блок)."""
    if not isinstance(tree, dict):
        return None
    node = tree.get(key)
    if isinstance(node, dict):
        return node
    for value in tree.values():
        found = _node(value, key)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------- Steam на диске

def steam_root():
    """Папка Steam: из реестра, иначе стандартные места. None — Steam не установлен."""
    for path in _registry_paths():
        if path and os.path.isdir(os.path.join(path, "userdata")):
            return path
    for path in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
                 r"D:\Steam", r"E:\Steam", r"D:\Games\Steam", r"C:\Games\Steam"):
        if os.path.isdir(os.path.join(path, "userdata")):
            return path
    return None


def _registry_paths():
    import winreg
    out = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
        for value in ("SteamPath", "InstallPath"):
            try:
                with winreg.OpenKey(hive, key) as k:
                    out.append(os.path.normpath(winreg.QueryValueEx(k, value)[0]))
            except OSError:
                pass
    return out


def recent_account_id(root):
    """accountid аккаунта, который Steam грузит последним (из loginusers.vdf)."""
    try:
        users = _node(parse_vdf(_read(os.path.join(root, "config", "loginusers.vdf"))), "users") or {}
    except OSError:
        return None
    fallback = None
    for steam_id, data in users.items():
        if not steam_id.isdigit() or not isinstance(data, dict):
            continue
        account = int(steam_id) - STEAM_ID64_BASE
        if data.get("MostRecent") == "1":
            return account
        fallback = fallback or account
    return fallback


def friends():
    """{steamid64: [все известные имена]} — из localconfig.vdf последнего аккаунта."""
    root = steam_root()
    if not root:
        return {}
    account = recent_account_id(root)
    if not account:
        return {}
    try:
        tree = parse_vdf(_read(os.path.join(root, "userdata", str(account), "config", "localconfig.vdf")))
    except OSError as e:
        log.info("localconfig.vdf не прочитан: %s", e)
        return {}
    node = _node(tree, "friends") or {}
    out = {}
    for account_id, data in node.items():
        if not account_id.isdigit() or not isinstance(data, dict):
            continue
        names = {data.get("name") or ""}
        history = data.get("NameHistory") or {}
        if isinstance(history, dict):
            names |= set(history.values())
        names = {n.strip() for n in names if isinstance(n, str) and n.strip()}
        if names:
            out[str(int(account_id) + STEAM_ID64_BASE)] = sorted(names)
    return out


# ---------------------------------------------------------------- поиск друга

def _norm(text):
    return _NICK_OK.sub(" ", (text or "").lower()).strip()


def _variants(text):
    """Нормализованное имя и его транслит: «коч» и «koch» должны находить друг друга."""
    out = {_norm(text)}
    try:
        from actions import translit
        out.add(_norm(translit(text)))
    except Exception:  # actions ещё не загрузился — хватит и одного варианта
        pass
    return {v for v in out if v}


def _score(query_variants, name):
    """Насколько имя похоже на запрос: 1.0 — точное совпадение."""
    best = 0.0
    for name_variant in _variants(name):
        for q in query_variants:
            if not q:
                continue
            if name_variant == q:
                score = 1.0
            elif name_variant.startswith(q) or q.startswith(name_variant):
                score = 0.92
            elif q in name_variant:
                score = 0.88
            elif set(q.split()) <= set(name_variant.split()):
                score = 0.84
            else:
                score = difflib.SequenceMatcher(None, q, name_variant).ratio()
            best = max(best, score)
    return best


def find(query, limit=6):
    """[(steamid64, имя, похожесть)] по имени или прошлым никам, похожие — первыми."""
    query_variants = _variants(query)
    if not query_variants:
        return []
    everything = friends()
    if not everything:
        return []
    scored = {}
    for steam_id, names in everything.items():
        best, best_name = 0.0, names[0]
        for name in names:
            score = _score(query_variants, name)
            if score > best:
                best, best_name = score, name
        if best >= 0.7:
            scored[steam_id] = (best, best_name)
    ordered = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(steam_id, name, round(score, 3)) for steam_id, (score, name) in ordered[:limit]]


def split_message(rest):
    """«Пете привет как дела» → (имя, текст). Имя берём самое длинное из известных ников."""
    rest = (rest or "").strip(" ,.:;-—")
    if not rest:
        return "", ""
    known = []
    for names in friends().values():
        known.extend(names)
    known = [n for n in known if n]
    low = _norm(rest)
    best = ""
    for name in sorted(known, key=len, reverse=True):
        n = _norm(name)
        if n and (low == n or low.startswith(n + " ")):
            best = name
            break
    if best:
        # отрезаем ровно столько символов, сколько заняло имя (по нормализованному тексту)
        idx = _cut_by_norm(rest, best)
        return best, rest[idx:].strip(" ,.:;-—")
    parts = rest.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _cut_by_norm(rest, name):
    """Позиция в rest, где заканчивается name (по нормализованному тексту)."""
    target = len(_norm(name))
    seen = 0
    for i, ch in enumerate(rest):
        if _NICK_OK.match(ch):
            continue
        seen += 1
        if seen >= target:
            return i + 1
    return len(rest)


# ---------------------------------------------------------------- окна Steam

def steam_windows():
    """[(hwnd, title)] — видимые окна Steam (клиент и его webhelper)."""
    import automation
    return automation.windows_of(("steam.exe", "steamwebhelper.exe"))


def running():
    return bool(steam_windows())


_IGNORE_BUTTONS = {"игнорировать", "ignore", "не показывать снова", "don't show again"}


def dismiss_dialogs():
    """Закрыть служебные окна Steam («Путь к разделу Steam в реестре недоступен…»): они блокируют
    клиент и не дают открыться чату. Жмём «Игнорировать» — это ничего не меняет в настройках."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    clicks = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def on_button(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.strip().lower() in _IGNORE_BUTTONS:
                clicks.append((hwnd, buf.value))
        return True

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def on_window(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value == "#32770":  # стандартный диалог Windows
            u.EnumChildWindows(hwnd, on_button, 0)
        return True

    u.EnumWindows(on_window, 0)
    for hwnd, text in clicks:
        log.info("Закрываю окно Steam: %s", text)
        u.SendMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
    return bool(clicks)


def _open_uri(uri):
    try:
        os.startfile(uri)  # noqa: S606 — зарегистрированный протокол steam://
        return True
    except OSError:
        log.info("os.startfile не сработал для %s", uri)
    try:
        subprocess.Popen(["cmd", "/c", "start", "", uri],
                         creationflags=CREATE_NO_WINDOW, close_fds=True)
        return True
    except OSError:
        log.exception("Не смогла открыть %s", uri)
        return False


def open_chat(steam_id):
    return _open_uri(f"steam://friends/message/{steam_id}")


def _wait_steam_foreground(timeout=8.0):
    """Дождаться, пока на переднем плане окажется клиент Steam (после открытия чата он выходит вперёд).
    Возвращает заголовок окна или None."""
    import automation
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if automation.foreground_process() in ("steam.exe", "steamwebhelper.exe"):
            return automation.foreground_title()
        time.sleep(0.4)
    return None


def _focus_steam():
    """Вывести клиент Steam вперёд: сначала окно «Список друзей» (там живой чат), потом главное."""
    import automation
    windows = steam_windows()
    for want in ("друз", "friend"):
        for hwnd, title in windows:
            if want in title.lower() and automation.focus(hwnd):
                return hwnd
    for hwnd, title in windows:
        if automation.focus(hwnd):
            return hwnd
    return None


# ---------------------------------------------------------------- отправка

def send(name, text):
    """Отправить text человеку name.
    Возвращает (status, детали): sent / not_found / no_steam / no_window / ambiguous."""
    import automation
    if not text:
        return "not_found", name
    if not steam_root():
        return "no_steam", None
    matches = find(name, limit=3)
    if not matches:
        return "not_found", name
    steam_id, nick, score = matches[0]
    # переспрашиваем, только если второй человек подходит почти так же и точного совпадения нет
    rivals = [(sid, n) for sid, n, s in matches[1:] if s >= score - 0.05]
    if score < 1.0 and rivals:
        return "ambiguous", [nick] + [n for _, n in rivals]

    if not running():
        _open_uri("steam://open/friends")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not running():
            time.sleep(1.0)
    if not running():
        return "no_steam", None

    dismiss_dialogs()
    if not open_chat(steam_id):
        return "no_window", nick
    # печатаем только когда на переднем плане именно Steam — иначе текст уйдёт в чужое окно
    if not _wait_steam_foreground(timeout=6.0) and not _focus_steam():
        return "no_window", nick
    if not _wait_steam_foreground(timeout=4.0):
        return "no_window", nick
    dismiss_dialogs()

    time.sleep(0.8)
    automation.input_text(text)
    time.sleep(0.35)
    from actions import send_keys
    send_keys(["enter"])
    log.info("Отправила в Steam для %s: %s", nick, text)
    return "sent", nick
