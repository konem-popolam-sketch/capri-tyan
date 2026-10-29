"""Забирает свежие cookie Arena из твоего Chrome и кладёт их Капри.

Зачем: у Капри в arena_state\\cookies.json сессия Arena истекает (access_token живёт
час), и без обновления она разлогинена — страница Arena просит вход в Google.

    python tools\\import_chrome_cookies.py

Chrome на время импорта нужно закрыть: он держит файл cookie открытым.
Скрипт ничего не пишет в твой браузер — только читает и сохраняет копию для Капри.
Старый cookies.json сохраняется рядом как cookies.json.bak.
"""
import base64
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE, "arena_state", "cookies.json")
KEEP = ("arena.ai", "auth.arena.ai", "accounts.google.com", "google.com")


def session_expiry(cookies):
    """Когда истекает access_token из cookie arena-auth-prod-v1 (или None)."""
    raw = next((c["value"] for c in cookies if c["name"] == "arena-auth-prod-v1"), "")
    if not raw.startswith("base64-"):
        return None
    encoded = raw[len("base64-"):]
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None
    exp = payload.get("expires_at")
    return float(exp) if isinstance(exp, (int, float)) else None


def norm(cookie):
    """Приводит cookie rookiepy к формату arena_state/cookies.json."""
    def pick(*names):
        for name in names:
            value = cookie.get(name)
            if value not in (None, ""):
                return value
        return None

    same_site_raw = pick("same_site", "sameSite")
    same_site = "Lax"
    if isinstance(same_site_raw, str):
        kind = same_site_raw.lower().replace("_", "").replace("-", "").replace(" ", "")
        same_site = {"strict": "Strict", "none": "None", "lax": "Lax"}.get(kind, "Lax")
    return {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": pick("path") or "/",
        "expires": float(pick("expires", "expirationDate") or -1),
        "httpOnly": bool(pick("http_only", "httpOnly")),
        "secure": bool(pick("secure")),
        "sameSite": same_site if same_site != "NoRestriction" else "Lax",
    }


def main():
    try:
        import rookiepy
    except ImportError:
        print("Нет модуля rookiepy — он ставится вместе с setup_ai.bat: "
              "python -m pip install rookiepy")
        return 2

    try:
        raw = rookiepy.chrome()
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "appbound" in message.lower() or "admin" in message.lower():
            print("Chrome 130+ шифрует cookie привязкой к приложению (App-Bound Encryption): "
                  "посторонний процесс прочитает их только с правами администратора.")
            print("Проще не читать, а скопировать профиль целиком: закрой Chrome и запусти\n"
                  "    python tools\\seed_browser_profile.py --force")
            return 1
        print(f"Не смог прочитать cookie Chrome: {message}")
        print("Скорее всего Chrome запущен. Закрой его полностью (все окна) и повтори.")
        return 1

    cookies = [
        norm(c) for c in raw
        if any(str(c.get("domain", "")).lstrip(".").endswith(domain) for domain in KEEP)
    ]
    if not cookies:
        print("В Chrome не нашлось cookie Arena — ты точно залогинен на arena.ai в этом браузере?")
        return 1

    exp = session_expiry(cookies)
    if exp is not None and exp <= time.time():
        print(f"Внимание: access_token истёк {time.strftime('%H:%M:%S', time.localtime(exp))} — "
              "открой arena.ai в Chrome (он обновит сессию), закрой Chrome и повтори.")
        return 1

    if os.path.exists(STATE):
        backup = f"{STATE}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(STATE, backup)
        print(f"Старый cookies.json сохранён: {backup}")

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": []}, f, ensure_ascii=False, indent=2)

    print(f"Перенесено cookie: {len(cookies)}")
    for c in cookies:
        when = time.strftime("%d.%m %H:%M", time.localtime(c["expires"])) if c["expires"] > 0 else "сессия"
        print(f"  {c['domain']:20} {c['name'][:34]:36} до {when}")
    if exp is not None:
        print(f"Сессия Arena действует до {time.strftime('%d.%m %H:%M', time.localtime(exp))}")
    print("Готово. Запусти Капри и задай вопрос.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
