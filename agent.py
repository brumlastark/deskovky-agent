# agent.py
import os
import json
import re
from datetime import date

import requests
from bs4 import BeautifulSoup


# --- ZDROJE (MVP: 2 zdroje) ---
SOURCES = [
    ("TLAMA – Novinky v češtině", "https://www.tlamagames.com/novinky-v-cestine/"),
    ("Zatrolené – Připravované novinky", "https://www.zatrolene-hry.cz/katalog-her/pripravovane-novinky/"),
]

SEEN_PATH = "seen.json"


def load_seen() -> set:
    if not os.path.exists(SEEN_PATH):
        return set()
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        # když je soubor rozbitý, radši začni od nuly
        return set()


def save_seen(seen_set: set) -> None:
    # udržuj soubor rozumně velký
    data = sorted(list(seen_set))[-2000:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_text(url: str) -> str:
    r = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "DeskovkyAgent/1.0 (+GitHubActions)"},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # vyhoď skripty/styly
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_candidates(text: str):
    """
    MVP heuristika: vytáhne řádky, které vypadají jako názvy.
    Později to nahradíme lepším parsováním / AI scoringem.
    """
    lines = [l.strip() for l in text.splitlines()]
    candidates = []

    for l in lines:
        if not l:
            continue

        # rozumná délka pro "název"
        if len(l) < 8 or len(l) > 80:
            continue

        low = l.lower()

        # filtruj typické UI věci / bordel
        if any(bad in low for bad in [
            "cookies", "přihláš", "přihlás", "košík", "vyhled", "menu",
            "kontakt", "obchodní", "podmínky", "privacy", "gdpr", "newsletter"
        ]):
            continue

        # odfiltruj url a podobné věci
        if "http://" in low or "https://" in low:
            continue

        # musí obsahovat nějaká písmena
        if not re.search(r"[A-Za-zÁ-ž]", l):
            continue

        candidates.append(l)

    # omez množství
    return candidates[:120]


def build_digest_without_ai(new_items_by_source):
    parts = []
    parts.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}\n")
    parts.append(
        "Ahoj! Přináším čerstvý výlov. M už si brousí zuby na škodění, "
        "Š kontroluje, jestli tu nejsou kostky.\n"
    )

    for source_name, items in new_items_by_source.items():
        parts.append(f"\n## {source_name}\n")
        if not items:
            parts.append("- (nic nového / nebo to parser teď nedal – ještě doladíme)\n")
        else:
            for it in items[:15]:
                parts.append(f"- {it}")

    parts.append("\n\nPS: Fit index + „Š šediví“ detektor kostek přidáme v další verzi.\n")
    return "\n".join(parts)


def send_email(subject: str, body: str):
    """
    SMTP DEBUG verze:
    - vypíše do logu GitHub Actions, co se děje
    - neprozradí heslo
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]
    mail_to = os.environ["MAIL_TO"]

    print(f"[DEBUG] host={smtp_host} port={smtp_port}")
    print(f"[DEBUG] from={mail_from} to={mail_to}")
    print(f"[DEBUG] subject={subject}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.set_debuglevel(1)  # SMTP komunikace půjde do logu Actions
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(smtp_user, smtp_pass)

        refused = s.sendmail(mail_from, [mail_to], msg.as_string())
        print(f"[DEBUG] refused={refused}")

    print("[OK] EMAIL_SENT (SMTP accepted recipient)")


def main():
    # rychlá kontrola: jestli máme env proměnné
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")

    seen = load_seen()
    new_items_by_source = {}

    for source_name, url in SOURCES:
        print(f"[INFO] Fetching: {source_name} ({url})")
try:
    text = fetch_text(url)
except Exception as e:
    print(f"[WARN] Failed to fetch {url}: {repr(e)}")
    new_items_by_source[source_name] = []
    continue

candidates = extract_candidates(text)

        new_items = []
        for c in candidates:
            key = f"{source_name}::{c}"
            if key not in seen:
                new_items.append(c)
                seen.add(key)

        new_items_by_source[source_name] = new_items
        print(f"[INFO] {source_name}: candidates={len(candidates)} new={len(new_items)}")

    body = build_digest_without_ai(new_items_by_source)
    subject = f"Deskovkový briefing – M škodí, Š šediví ({date.today().isoformat()})"

    send_email(subject, body)
    save_seen(seen)


if __name__ == "__main__":
    main()
