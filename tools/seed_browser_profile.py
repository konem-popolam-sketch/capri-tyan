"""Переносит вход в Arena из твоего Chrome в браузер, которым пользуется Капри.

Капри не может открыть твой Chrome напрямую (он уже запущен), поэтому она поднимает
свой браузер с отдельным профилем. Скрипт копирует в этот профиль те файлы, где
хранится вход: localStorage (сессия веб-приложения Arena) и cookies.

    python tools\\seed_browser_profile.py            # найти Chrome и перенести
    python tools\\seed_browser_profile.py --force    # перезаписать существующий профиль
    python tools\\seed_browser_profile.py --from "C:\\Users\\Me\\AppData\\Local\\Google\\Chrome\\User Data"

Chrome во время копирования лучше закрыть: пока он работает, файл Cookies занят,
и часть cookie перенести не получится (это не критично — Капри всё равно подставит
cookie из arena_state\\cookies.json).
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(BASE, "arena_state", "direct_browser_profile")

CANDIDATE_ROOTS = [
    r"%LOCALAPPDATA%\Google\Chrome\User Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
]

# Ключ шифрования cookie лежит в корне профиля браузера.
ROOT_FILES = ["Local State"]

# Что берём из папки профиля (Default). Обязательный минимум — Local Storage.
PROFILE_ITEMS = [
    "Preferences",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Local Storage",
]


def find_source():
    for pattern in CANDIDATE_ROOTS:
        root = os.path.expandvars(pattern)
        if os.path.isdir(root) and os.path.isdir(os.path.join(root, "Default")):
            return root
    return None


def chrome_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return False
    return "chrome.exe" in out.lower()


def close_chrome():
    """Просит Chrome закрыться, через 10 секунд добивает — иначе cookie занят."""
    if not chrome_running():
        print("Chrome не запущен — можно копировать.")
        return True
    print("Закрываю Chrome...")
    subprocess.run(["taskkill", "/IM", "chrome.exe"], capture_output=True, timeout=60)
    for _ in range(10):
        time.sleep(1)
        if not chrome_running():
            print("Chrome закрыт.")
            return True
    print("Chrome не закрылся сам — завершаю принудительно.")
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True, timeout=60)
    time.sleep(2)
    ok = not chrome_running()
    print("Chrome закрыт." if ok else "Не удалось закрыть Chrome — закрой его вручную.")
    return ok


def profile_session_alive(dest):
    """Есть ли в профиле Капри живой токен Arena. Возвращает дату окончания («29.10 07:15») или None.

    Значения cookie зашифрованы, но срок годности лежит открытым текстом — этого хватает.
    """
    db = os.path.join(dest, "Default", "Network", "Cookies")
    if not os.path.isfile(db):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            rows = conn.execute(
                "SELECT name, expires_utc FROM cookies WHERE name LIKE 'arena-auth-prod-v1%'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — база занята браузером или не читается
        return None
    chrome_epoch = 11644473600  # секунд между 1601-01-01 и 1970-01-01
    for name, expires_utc in rows:
        if not expires_utc:
            continue  # сессионная cookie: срок узнать нельзя, считаем что входа нет
        unix = expires_utc / 1_000_000 - chrome_epoch
        if unix > time.time() + 60:
            return time.strftime("%d.%m %H:%M", time.localtime(unix))
    return None


def copy_item(src, dst, report):
    """Копирует файл или папку; занятые файлы пропускает и пишет в отчёт."""
    if not os.path.exists(src):
        return 0
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        count = 0
        for name in os.listdir(src):
            count += copy_item(os.path.join(src, name), os.path.join(dst, name), report)
        return count
    try:
        shutil.copy2(src, dst)
        return 1
    except OSError as exc:
        report.append(f"ПРОПУСК {os.path.basename(src)}: {exc.strerror or exc}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Перенести вход в Arena из Chrome")
    parser.add_argument("--from", dest="source", help="папка 'User Data' браузера")
    parser.add_argument("--profile", default="Default", help="имя профиля (по умолчанию Default)")
    parser.add_argument("--dest", default=DEST, help="куда положить профиль")
    parser.add_argument("--force", action="store_true", help="перезаписать существующий профиль")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    parser.add_argument("--close-chrome", action="store_true",
                        help="закрыть запущенный Chrome (иначе он держит cookie и копия будет неполной)")
    parser.add_argument("--overwrite", action="store_true",
                        help="перезаписать профиль даже если в нём уже есть живой вход в Arena")
    args = parser.parse_args()

    closed = [False]

    def ensure_chrome_closed():
        if args.close_chrome and not args.dry_run and not closed[0]:
            closed[0] = True
            close_chrome()

    source = args.source or find_source()
    if not source:
        print('Не нашёл Chrome/Edge. Укажи папку вручную: --from "...\\User Data"')
        return 2
    src_profile = os.path.join(source, args.profile)
    if not os.path.isdir(src_profile):
        print(f"Нет профиля {src_profile}")
        return 2

    print(f"Источник  : {src_profile}")
    print(f"Назначение: {args.dest}")
    if os.path.isdir(args.dest) and os.listdir(args.dest):
        if not args.force:
            print("Профиль Капри уже существует. Добавь --force, чтобы перезаписать "
                  "(старый будет сохранён рядом с суффиксом .bak).")
            return 1
        alive = profile_session_alive(args.dest)
        if alive and not args.overwrite:
            # В профиле Капри вход уже есть — копия из Chrome может его сломать
            # (например, там ты вошёл только в Google, но не в Arena).
            print(f"В профиле Капри уже есть живой вход в Arena (до {alive}).")
            print("Копировать не нужно. Если всё же надо — добавь --overwrite.")
            return 3
        ensure_chrome_closed()
        backup = f"{args.dest}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        if args.dry_run:
            print(f"[dry-run] старое уедет в {backup}")
        else:
            os.rename(args.dest, backup)
            print(f"Старый профиль сохранён: {backup}")

    if args.dry_run:
        for rel in ROOT_FILES:
            print(f"[dry-run] <user data>/{rel}")
        for rel in PROFILE_ITEMS:
            print(f"[dry-run] {args.profile}/{rel}")
        return 0

    ensure_chrome_closed()
    os.makedirs(args.dest, exist_ok=True)
    report = []
    total = 0
    for rel in ROOT_FILES:
        total += copy_item(os.path.join(source, rel), os.path.join(args.dest, rel), report)
    for rel in PROFILE_ITEMS:
        src = os.path.join(src_profile, rel.replace("/", os.sep))
        dst = os.path.join(args.dest, args.profile, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        total += copy_item(src, dst, report)
    for line in report:
        print(" ", line)
    print(f"Скопировано файлов: {total}")

    entries = os.path.join(args.dest, args.profile, "Local Storage", "leveldb")
    if not os.path.isdir(entries):
        print("ВНИМАНИЕ: Local Storage не скопирован — в этом профиле нет входа в веб-интерфейс "
              "Arena, страница снова попросит логин.")
        return 1
    if any("Cookies" in line for line in report):
        print("ВАЖНО: файл Cookies был занят — значит Chrome ещё работает. Cookie с сессией "
              "Arena не перенеслись, и браузер Капри останется разлогиненным. "
              "Закрой Chrome полностью (все окна) и запусти скрипт снова: --force")
    print("Готово. Запусти Капри и задай вопрос.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
