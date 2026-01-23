# agent.py (v2)
import os
import re
import csv
import io
import html
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup


# === TVŮJ GOOGLE SHEET (CSV publish) ===
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTmHPN69oIL7Fit5EN_K6HXtYtEPOZi2v-KmFL85D-wQsljrIT3cDY_Uh0LShOiIDfOx6rGJPlfESa2/pub?output=csv"

# === ZDROJ NOVINEK (zatím TLAMA) ===
TLAMA_URL = "https://www.tlamagames.com/novinky-v-cestine/"

# Slova, která typicky značí rozšíření (CZ/EN)
EXPANSION_KEYWORDS = [
    "rozšíření", "rozsireni", "expanze", "expansion", "extension",
    "doplněk", "doplnok", "dodatek", "promo", "promo pack",
    "balíček", "balicek", "pack", "packy",
]

# řádky, které nechceme brát jako hry
LINE_BLACKLIST = {
    "nastavení", "souhlasím", "přejít na obsah", "prihlasit se", "přihlásit se",
    "nová registrace", "zákaznická podpora", "kontakt", "gdpr", "cookies",
    "menu", "vyhledávání", "vyhledavani", "košík", "kosik",
}


def norm(s: str) -> str:
    """Normalize text for matching: lowercase, strip, remove diacritics, collapse spaces."""
    s = (s or "").strip().lower()
    # remove diacritics
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


def looks_like_expansion(title: str) -> bool:
    t = norm(title)
    return any(k in t for k in EXPANSION_KEYWORDS)


def load_collection_titles(csv_url: str) -> list[str]:
    """Loads user's collection from Google Sheet CSV, expects column A header 'Titul'."""
    r = requests.get(csv_url, timeout=30, headers={"User-Agent": "DeskovkyAgent/2.0"})
    r.raise_for_status()

    # CSV can contain BOM; handle robustly
    raw = r.text.lstrip("\ufeff")
    f = io.StringIO(raw)
    reader = csv.reader(f)

    rows = list(reader)
    if not rows:
        return []

    header = rows[0]
    # find column named "Titul" (case-insensitive)
    col_idx = None
    for i, h in enumerate(header):
        if norm(h) == "titul":
            col_idx = i
            break
    if col_idx is None:
        # fallback: first column
        col_idx = 0

    titles = []
    for row in rows[1:]:
        if col_idx < len(row):
            t = row[col_idx].strip()
            if t:
                titles.append(t)

    # de-dup, keep order
    seen = set()
    out = []
    for t in titles:
        nt = norm(t)
        if nt not in seen:
            seen.add(nt)
            out.append(t)
    return out


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


def extract_tlama_items(html_text: str) -> list[dict]:
    """
    Best-effort extraction of items (title, url, image_url) from TLAMA page.
    Works even if structure changes slightly.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    base = "https://www.tlamagames.com"
    items_by_url = {}

    # Heuristic: product-like links are usually <a> with meaningful text and href to TLAMA
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = " ".join(a.get_text(" ", strip=True).split())

        if not text:
            continue

        # quick blacklist
        if norm(text) in LINE_BLACKLIST:
            continue

        # skip very short/very long
        if len(text) < 4 or len(text) > 120:
            continue

        url = absolute_url(base, href)

        # keep only TLAMA internal links
        if "tlamagames.com" not in url:
            continue

        # drop obvious non-product pages
        bad_parts = ["/customer/", "/checkout", "/account", "/login", "/search", "/kontakt", "/gdpr", "#"]
        if any(bp in url for bp in bad_parts):
            continue

        # find an image near this link
        img_url = ""
        img = a.find("img")
        if img and img.get("src"):
            img_url = absolute_url(base, img.get("src").strip())
        else:
            # try nearby images (parent container)
            parent = a.parent
            for _ in range(3):
                if not parent:
                    break
                img2 = parent.find("img")
                if img2 and img2.get("src"):
                    img_url = absolute_url(base, img2.get("src").strip())
                    break
                parent = parent.parent

        # store
        if url not in items_by_url:
            items_by_url[url] = {
                "title": text,
                "url": url,
                "image_url": img_url,
            }

    # lightly clean: remove duplicates by normalized title where URL differs
    seen_titles = set()
    out = []
    for it in items_by_url.values():
        nt = norm(it["title"])
        # filter out obvious UI leftovers
        if any(bad in nt for bad in ["tlamagames", "novinky v cestine", "lednovy vyprodej", "nejprodavanejsi"]):
            continue
        if nt in seen_titles:
            continue
        seen_titles.add(nt)
        out.append(it)

    return out


def match_expansion_to_owned(title: str, owned_norm_titles: list[str]) -> str | None:
    """
    Returns matched owned game title (normalized string) if expansion seems to reference it.
    Simple substring match.
    """
    t = norm(title)
    for game_nt in owned_norm_titles:
        # require a minimum length to avoid silly matches (e.g., "go")
        if len(game_nt) < 4:
            continue
        if game_nt in t:
            return game_nt
    return None


def build_email_html(owned_titles: list[str], items: list[dict]) -> tuple[str, str]:
    owned_norm = [norm(t) for t in owned_titles]

    expansions_for_owned = []
    other_items = []

    for it in items:
        title = it["title"]
        if looks_like_expansion(title):
            match = match_expansion_to_owned(title, owned_norm)
            if match:
                expansions_for_owned.append(it)
            else:
                other_items.append(it)
        else:
            other_items.append(it)

    # Plain-text fallback
    lines = []
    lines.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}")
    lines.append("")
    lines.append("Ahoj! Tady je výlov z TLAMA (novinky v češtině).")
    lines.append("")

    if expansions_for_owned:
        lines.append("🧩 Rozšíření pro hry, které už máš:")
        for it in expansions_for_owned[:20]:
            lines.append(f"- {it['title']} — {it['url']}")
        lines.append("")
    else:
        lines.append("🧩 Rozšíření pro tvoje hry: (zatím nic jasného)")
        lines.append("")

    lines.append("🆕 Ostatní novinky / hry:")
    for it in other_items[:30]:
        lines.append(f"- {it['title']} — {it['url']}")

    text_body = "\n".join(lines)

    # HTML body
    def card(it):
        t = html.escape(it["title"])
        u = html.escape(it["url"])
        img = it.get("image_url") or ""
        if img:
            img_tag = f'<img src="{html.escape(img)}" alt="" style="width:64px;height:auto;border-radius:8px;display:block;">'
        else:
            img_tag = '<div style="width:64px;height:64px;border-radius:8px;background:#2a2a2a;"></div>'
        return f"""
        <div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid #2a2a2a;">
          <div style="flex:0 0 64px;">{img_tag}</div>
          <div style="flex:1 1 auto;">
            <div style="font-size:15px;line-height:1.3;margin:0 0 4px 0;"><a href="{u}" style="color:#8ab4f8;text-decoration:none;">{t}</a></div>
            <div style="font-size:12px;color:#9aa0a6;word-break:break-all;">{u}</div>
          </div>
        </div>
        """

    exp_html = "".join(card(it) for it in expansions_for_owned[:20]) or '<div style="color:#9aa0a6;">(zatím nic jasného)</div>'
    other_html = "".join(card(it) for it in other_items[:30]) or '<div style="color:#9aa0a6;">(nic)</div>'

    html_body = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8eaed;background:#121212;padding:18px;">
      <h1 style="font-size:20px;margin:0 0 8px 0;">🎲 Deskovkový briefing – {date.today().isoformat()}</h1>
      <div style="color:#bdc1c6;margin:0 0 16px 0;">
        Ahoj! M už plánuje škodění, Š kontroluje, jestli někde nečíhají kostky.
      </div>

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🧩 Rozšíření pro hry, které už máš</h2>
      {exp_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🆕 Ostatní novinky / hry (TLAMA)</h2>
      {other_html}

      <div style="margin-top:16px;color:#9aa0a6;font-size:12px;">
        Zdroj: TLAMA “Novinky v češtině”. Zatrolené zatím blokují automatické stahování (403).
      </div>
    </div>
    """.strip()

    return text_body, html_body


def send_email(subject: str, text_body: str, html_body: str):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import make_msgid

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    mail_from = os.environ["MAIL_FROM"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Message-ID"] = make_msgid()

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(smtp_user, smtp_pass)
        s.sendmail(mail_from, [mail_to], msg.as_string())


def main():
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")

    owned = load_collection_titles(SHEET_CSV_URL)
    print(f"[INFO] Loaded collection titles: {len(owned)}")

    tlama_html = fetch_html(TLAMA_URL)
    items = extract_tlama_items(tlama_html)
    print(f"[INFO] Extracted TLAMA items: {len(items)}")

    text_body, html_body = build_email_html(owned, items)
    subject = f"Deskovkový briefing – M škodí, Š šediví ({date.today().isoformat()})"
    send_email(subject, text_body, html_body)


if __name__ == "__main__":
    main()
