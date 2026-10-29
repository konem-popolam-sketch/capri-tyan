"""Проверка связи Капри с Arena вручную: что нашлось, сколько ждём, что ответила.

    python tools\\check_arena.py

Пишет всё в tools\\check_arena.log — этот файл можно показывать, там нет cookie.
"""
import json
import logging
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
LOG = os.path.join(BASE, "tools", "check_arena.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("capri")


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8-sig") as f:
        cfg = json.load(f)
    log.info("arena_ai=%s backend=%s model=%s timeout=%s impersonate=%r show_browser=%s",
             cfg.get("arena_ai"), cfg.get("arena_backend"), cfg.get("arena_model"),
             cfg.get("arena_timeout"), cfg.get("arena_impersonate"), cfg.get("arena_show_browser"))

    from arena_ai import ArenaAI

    ai = ArenaAI(cfg)
    started = time.monotonic()
    ok = ai._connect()
    log.info("connect: %s mode=%s профиль=%s за %.1f с", ok, ai.mode, ai.impersonate or "-",
             time.monotonic() - started)
    if not ok:
        log.info("ошибка: %s", ai.last_error)
        return 1

    started = time.monotonic()
    answer = ai.ask("Скажи одним предложением, кто ты.")
    log.info("ответ за %.1f с: %r", time.monotonic() - started, answer)
    if answer is None:
        log.info("Капри получила None — причина в сроке выше (Arena не ответила)")
        return 1
    ai.close()
    log.info("ГОТОВО: Arena отвечает")
    return 0


if __name__ == "__main__":
    sys.exit(main())
