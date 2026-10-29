"""Показывает, что видит браузер Капри на arena.ai: cookies и локальная сессия.

    python tools\\diagnose_arena_browser.py

Запускает Chrome на профиле Капри (arena_state\\direct_browser_profile), открывает
arena.ai и печатает: какие cookie там есть, жив ли токен сессии, лежит ли токен
в localStorage и просит ли страница войти. Вход не требуется.
"""
import base64
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(BASE, "arena_state", "direct_browser_profile")


def token_expiry(value):
    if not str(value).startswith("base64-"):
        return None
    encoded = str(value)[len("base64-"):]
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return None
    exp = payload.get("expires_at")
    return float(exp) if isinstance(exp, (int, float)) else None


def main():
    if not os.path.isdir(PROFILE):
        print(f"Нет профиля {PROFILE}")
        return 1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Нет playwright — запусти setup_ai.bat")
        return 2

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            PROFILE,
            channel="chrome",
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://arena.ai/", wait_until="domcontentloaded", timeout=60_000)
        time.sleep(6)

        cookies = [c for c in context.cookies() if "arena.ai" in c.get("domain", "")]
        print(f"\n== Cookie профиля (arena.ai): {len(cookies)} ==")
        for c in sorted(cookies, key=lambda x: x["name"]):
            print(f"  {c['name']:34} {str(c['value'])[:16]:18}")

        token = next((c["value"] for c in cookies if c["name"] == "arena-auth-prod-v1"), None)
        if token:
            exp = token_expiry(token)
            if exp:
                alive = exp > time.time()
                print(f"\nТокен сессии: {'ЖИВ' if alive else 'ИСТЁК'} до "
                      f"{time.strftime('%d.%m %H:%M', time.localtime(exp))}")
            else:
                print(f"\nТокен сессии найден, но не base64-JSON (len={len(token)})")
        else:
            print("\nТокена arena-auth-prod-v1 в профиле НЕТ")

        try:
            local = page.evaluate(
                "() => Object.keys(localStorage).map(k => [k, String(localStorage.getItem(k)).slice(0, 24)])"
            )
        except Exception as exc:  # noqa: BLE001
            local = []
            print(f"localStorage прочитать не удалось: {exc}")
        print(f"\n== localStorage arena.ai: {len(local)} ключей ==")
        for name, value in local:
            print(f"  {name[:40]:42} {value}")

        page_text = ""
        try:
            page_text = page.inner_text("body")[:4000].lower()
        except Exception:  # noqa: BLE001
            pass
        asks_login = ("log in or create account" in page_text
                      or "continue with google" in page_text
                      or "continue with email" in page_text)
        print(f"\nСтраница: {page.url}")
        print(f"Просит войти: {'ДА' if asks_login else 'НЕТ'}")
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
