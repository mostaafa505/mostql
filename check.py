import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

URL = "https://mostaql.com/projects?category=development&sort=latest"
BASE = "https://mostaql.com"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

FAIL_ALERT_AT = 6   # بعد 6 فشلات ورا بعض (حوالي نص ساعة) يبعتلك تنبيه
MAX_SEEN = 500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}


def clean(text):
    return " ".join((text or "").split())


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": [], "fails": 0, "alerted": False, "init": False}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        ok = r.status_code == 200
        print("Telegram:", r.status_code, "" if ok else r.text[:300])
        return ok
    except Exception as e:
        print("Telegram error:", type(e).__name__)
        return False


def parse_projects(html):
    soup = BeautifulSoup(html, "html.parser")
    projects = {}  # الترتيب محفوظ: الأحدث أولًا زي الصفحة

    for row in soup.select("tr.project-row"):
        a = row.select_one("h2 a[href*='/project/']")
        if not a:
            continue
        m = re.search(r"/project/(\d+)", a["href"])
        if not m:
            continue
        pid = m.group(1)

        title = clean(a.get_text(" "))
        if not title:
            continue

        owner_el = row.select_one("bdi")
        time_el = row.select_one("time")
        icon = row.select_one("span.hsoub-file-signature-icon")
        brief_el = row.select_one("p.project__brief a.details-url") or row.select_one("p.project__brief")

        brief = clean(brief_el.get_text(" ")) if brief_el else ""
        if len(brief) > 160:
            brief = brief[:160].rstrip() + "..."

        projects[pid] = {
            "id": pid,
            "title": title,
            "link": urljoin(BASE, a["href"]).split("#")[0].split("?")[0],
            "owner": clean(owner_el.get_text(" ")) if owner_el else "",
            "time": clean(time_el.get_text(" ")) if time_el else "",
            "bids": clean(icon.parent.get_text(" ")) if icon is not None and icon.parent else "",
            "brief": brief,
        }
    return projects


def fetch_projects():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    print("Mostaql HTTP status:", r.status_code)
    r.raise_for_status()

    projects = parse_projects(r.text)
    if not projects:
        print("DEBUG - no projects parsed. First 800 chars of page:")
        print(r.text[:800])
        raise RuntimeError("no projects found in page")

    print(f"Parsed {len(projects)} projects")
    return projects


def build_message(p):
    meta = " | ".join(x for x in [
        f"👤 {p['owner']}" if p["owner"] else "",
        f"🕒 {p['time']}" if p["time"] else "",
        f"💬 {p['bids']}" if p["bids"] else "",
    ] if x)
    parts = ["🆕 مشروع جديد في مستقل", "", p["title"]]
    if meta:
        parts.append(meta)
    if p["brief"]:
        parts += ["", p["brief"]]
    parts += ["", p["link"]]
    return "\n".join(parts)


def main():
    state = load_state()

    try:
        projects = fetch_projects()
    except Exception as e:
        print("FETCH ERROR:", e)
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= FAIL_ALERT_AT and not state.get("alerted"):
            if send_telegram("⚠️ بوت مستقل مش قادر يفتح الصفحة من فترة. راجع GitHub Actions."):
                state["alerted"] = True
        save_state(state)
        sys.exit(1 if MANUAL_RUN else 0)

    if state.get("alerted"):
        send_telegram("✅ بوت مستقل رجع يشتغل تاني.")
    state["fails"] = 0
    state["alerted"] = False

    seen = set(state.get("seen", []))

    # أول تشغيل: نسجل المشاريع الحالية من غير ما نبعتها
    if not state.get("init"):
        if send_telegram(
            f"✅ بوت مستقل اشتغل. اتسجل {len(projects)} مشروع حالي، وهيجيلك أي مشروع جديد من دلوقتي."
        ):
            state["seen"] = sorted(projects.keys(), key=int)
            state["init"] = True
            save_state(state)
            return
        print("First message failed, will retry next run")
        save_state(state)
        sys.exit(1 if MANUAL_RUN else 0)

    # الأقدم الأول عشان الرسايل توصل بالترتيب الصحيح
    new_items = [p for p in reversed(list(projects.values())) if p["id"] not in seen]
    print(f"New projects: {len(new_items)}")

    for p in new_items:
        if send_telegram(build_message(p)):
            seen.add(p["id"])
            time.sleep(2)
        else:
            print("Send failed, will retry next run:", p["id"])

    state["seen"] = sorted(seen, key=int)[-MAX_SEEN:]
    save_state(state)


if __name__ == "__main__":
    main()
