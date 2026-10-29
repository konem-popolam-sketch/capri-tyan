"""Забирает свежую сессию Arena из профиля Капри и обновляет arena_state\\cookies.json.

Зачем: Капри поднимает Chrome на профиле arena_state\\direct_browser_profile. Если ты
вошёл в Arena в этом окне, сессия там свежая, а в cookies.json — старая, и она
перетирает свежую при следующем запуске. Скрипт вытаскивает cookie из браузера
(расшифровывает их сам Chrome) и сохраняет в cookies.json.

    python tools\\export_browser_session.py

Если окно открылось на странице входа Google — войди там руками, скрипт подождёт
и сохранит сессию сам (по умолчанию ждёт 5 минут).
"""
import argparse
import base64
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(BASE, "arena_state", "direct_browser_profile")
OUT = os.path.join(BASE, "arena_state", "cookies.json")
KEEP = ("arena.ai",)
LOGIN_URL = "https://arena.ai/"


def session_expiry(cookies):
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


def keep(cookie):
    domain = str(cookie.get("domain", "")).lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in KEEP)


def same_site(value):
    kind = str(value or "").lower().replace("_", "").replace("-", "")
    return {"strict": "Strict", "none": "None"}.get(kind, "Lax")


def main():
    parser = argparse.ArgumentParser(description="Обновить сессию Arena для Капри из её браузера")
    parser.add_argument("--profile-dir", default=PROFILE)
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--channel", default="chrome", help="chrome / msedge / пусто для Chromium")
    parser.add_argument("--wait", type=int, default=300, help="сколько секунд ждать входа")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Нет playwright — поставь его через setup_ai.bat (python -m pip install playwright).")
        return 2

    if not os.path.isdir(args.profile_dir):
        print(f"Нет профиля {args.profile_dir}. Сначала: python tools\\seed_browser_profile.py --force")
        return 1

    with sync_playwright() as pw:
        options = {
            "user_data_dir": args.profile_dir,
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": ["--disable-blink-features=AutomationControlled", "--start-maximized"],
        }
        if args.channel:
            options["channel"] = args.channel
        context = pw.chromium.launch_persistent_context(**options)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

        deadline = time.monotonic() + max(10, args.wait)
        logged_in = False
        while time.monotonic() < deadline:
            url = page.url
            if "/text/" in url or (url.rstrip("/") == LOGIN_URL.rstrip("/")):
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(4)
                url = page.url
            if "accounts.google.com" not in url and "arena.ai" in url:
                cookies = [c for c in context.cookies() if keep(c)]
                if session_expiry(cookies) and session_expiry(cookies) > time.time():
                    logged_in = True
                    break
            time.sleep(2)

        cookies = [c for c in context.cookies() if keep(c)]
        origins = []
        if logged_in:
            state = context.storage_state()
            origins = [o for o in state.get("origins", []) if "arena.ai" in str(o.get("origin", ""))]
        context.close()

    if not logged_in:
        print("Сессия Arena в этом профиле не обнаружена (браузер открыл вход Google).")
        print("Войди в Arena в открывшемся окне и запусти скрипт снова — он сохранит сессию.")
        print("Подсказка: адрес можно вставить в поле правой кнопкой → Вставить (или Shift+Insert).")
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out):
        backup = f"{args.out}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(args.out, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        print(f"Старый cookies.json сохранён: {backup}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": origins}, f, ensure_ascii=False, indent=2)

    exp = session_expiry(cookies)
    print(f"Сохранено cookie Arena: {len(cookies)}")
    if exp:
        print(f"Сессия действует до {time.strftime('%d.%m %H:%M', time.localtime(exp))}")
    print("Теперь запускай Капри — она подхватит эту сессию.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
