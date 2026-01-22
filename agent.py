# agent.py
import os
import json
import re
from datetime import date

import requests
from bs4 import BeautifulSoup


# --- ZDROJE ---
SOURCES = [
    ("TLAMA – Novinky v češtině", "https://www.tlamagames.com/novinky-v-cestine/"),
    ("Zatrolené – Připravované novinky", "https://www.zatrolene-hry.cz/katalog-her/pripravovane-novinky/"),
]

SEEN_PATH = "seen.json"


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return set()
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen_set):
    data = sorted(list(seen_set))[-2000:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_text(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_candidates(text):
    lines = [l.strip() for l in text.splitlines()]
    out = []

    for l in lines:
        if not l:
            continue
        if len(l) < 8 or len(l) > 80:
            continue

        low = l.lower()
        if any(bad in low for bad in [
            "cookies", "přihláš", "košík", "vyhled", "menu",
            "kontakt", "obchodní", "gdpr", "newsletter"
        ]):
            continue

        if "http://" in low or "https://" in low:
            continue

        if not re.search(r"[A-Za-zÁ-ž]", l):
            continue

        out.append(l)

    return out[:120]


def build_digest(new_items_by_source):
    parts = []
    parts.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}\n")
    parts.append(
        "Ahoj! Tady je čerstvý deskovkový výlov. "
        "M už plánuje škodění, Š kontroluje, jestli někde nečíhají kostky.\n"
    )

    for source, items in new_items_by_source.items():
        parts.append(f"\n## {source}\n")
        if not items:
            parts.append("- (tentokrát nic nového, nebo zdroj zlobí)")
        else:
            for it in items[:15]:
                parts.append(f"- {it}")

    parts.append("\nPS: AI Fit index + „Š šediví“ radar přijde v další verzi 😉\n")
    return "\n".join(parts)


def send_email(subject, body):
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]
    mail_to = os.environ["MAIL_TO"]

    print(f"[DEBUG] SMTP {smtp_host}:{smtp_port}")
    print(f"[DEBUG] FROM {mail_from} → TO {mail_to}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.set_debuglevel(1)
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(smtp_user, smtp_pass)
        refused = s.sendmail(mail_from, [mail_to], msg.as_string())
        print(f"[DEBUG] refused={refused}")

    print("[OK] EMAIL_SENT")


def main():
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")

    seen = load_seen()
    new_items_by_source = {}

    for source, url in SOURCES:
        print(f"[INFO] Fetching {source}: {url}")
        try:
            text = fetch_text(url)
        except Exception as e:
            print(f"[WARN] Failed to fetch {url}: {repr(e)}")
            new_items_by_source[source] = []
            continue

        candidates = extract_candidates(text)
        new_items = []

        for c in candidates:
            key = f"{source}::{c}"
            if key not in seen:
                seen.add(key)
                new_items.append(c)

        new_items_by_source[source] = new_items
        print(f"[INFO] {source}: new={len(new_items)}")

    body = build_digest(new_items_by_source)
    subject = f"Deskovkový briefing – M škodí, Š šediví ({date.today().isoformat()})"

    send_email(subject, body)
    save_seen(seen)


if __name__ == "__main__":
    main()
