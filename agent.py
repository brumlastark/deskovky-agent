# agent.py (v3.1) — blacklist "Deskové hry/Encyklopedie" + dynamický subject
import os
import re
import csv
import io
import html
import unicodedata
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTmHPN69oIL7Fit5EN_K6HXtYtEPOZi2v-KmFL85D-wQsljrIT3cDY_Uh0LShOiIDfOx6rGJPlfESa2/pub?output=csv"

TLAMA_URL = "https://www.tlamagames.com/novinky-v-cestine/"
TLAMA_BASE = "https://www.tlamagames.com"

PRODUCT_PATH_PREFIXES = [
    "/deskove-hry/",
]

EXPANSION_KEYWORDS = [
    "rozšíření", "rozsireni", "expanze", "expansion", "extension",
    "doplněk", "doplnok", "dodatek", "promo", "promo pack",
    "balíček", "balicek", "pack",
]

TITLE_BLACKLIST_CONTAINS = [
    "registrace", "zapomenuté heslo", "přihlásit", "prihlasit", "košík", "kosik",
    "doprava", "platba", "obchodní podmínky", "podmínky", "ochrany osobních údajů",
    "gdpr", "cookies", "věrnostní", "affiliate", "program", "kontakt", "půjčovna",
    "provozní řád", "rozcesnik", "čestina", "english", "language",
    "tel:", "facebook", "instagram",
]

# nově: generické položky, které nechceme nikdy v "Nové hry"
TITLE_BLACKLIST_EXACT = {
    "deskové hry",
    "deskove hry",
    "encyklopedie",
}

PATH_BLACKLIST_CONTAINS = [
    "/registrace", "/klient/", "/kosik", "/doprava_", "/obchodni-", "/podminky",
    "/prosenior", "/prorodinu", "/prozkusenehrace", "/strategicke-hry", "/hrypro",
    "/3-6let", "/7-12let", "/13-let", "/proholky", "/prokluky",
    "/tipy", "/sleva", "/akce", "/festival", "/deskovcon",
    "/action/language", "/affiliate_", "/deskove-hry/encyklopedie",
    "/deskove-hry/deskove-hry",
]


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def looks_like_expansion(title: str) -> bool:
    t = norm(title)
    return any(norm(k) in t for k in EXPANSION_KEYWORDS)


def load_collection_titles(csv_url: str) -> list[str]:
    r = requests.get(csv_url, timeout=30, headers={"User-Agent": "DeskovkyAgent/3.1"})
    r.raise_for_status()
    raw = r.text.lstrip("\ufeff")
    f = io.StringIO(raw)
    reader = csv.reader(f)
    rows = list(reader)
    if not rows:
        return []

    header = rows[0]
    col_idx = None
    for i, h in enumerate(header):
        if norm(h) == "titul":
            col_idx = i
            break
    if col_idx is None:
        col_idx = 0

    titles = []
    for row in rows[1:]:
        if col_idx < len(row):
            t = row[col_idx].strip()
            if t:
                titles.append(t)

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
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


def is_product_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if "tlamagames.com" not in p.netloc:
        return False

    path = p.path or ""
    if not any(path.startswith(pref) for pref in PRODUCT_PATH_PREFIXES):
        return False

    path_low = norm(path)
    if any(norm(b) in path_low for b in PATH_BLACKLIST_CONTAINS):
        return False

    # nově: vyhodíme i samotný listing "/deskove-hry/" (není produkt)
    if norm(path).rstrip("/") == "/deskove-hry":
        return False

    return True


def title_is_ok(title: str) -> bool:
    t = norm(title)
    if len(t) < 4 or len(t) > 90:
        return False

    if t in TITLE_BLACKLIST_EXACT:
        return False

    if re.fullmatch(r"[\d\+\s\-\(\)]+", title.strip()):
        return False

    if any(norm(b) in t for b in TITLE_BLACKLIST_CONTAINS):
        return False

    return True


def extract_image_url(img_tag, base: str) -> str:
    if not img_tag:
        return ""
    for attr in ["src", "data-src", "data-original", "data-lazy", "data-image"]:
        val = img_tag.get(attr)
        if val and isinstance(val, str) and val.strip():
            return absolute_url(base, val.strip())
    return ""


def extract_tlama_products(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items_by_url = {}

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        url = absolute_url(TLAMA_BASE, href)
        if not is_product_url(url):
            continue

        text = " ".join(a.get_text(" ", strip=True).split())

        if not text:
            img_in = a.find("img")
            if img_in and img_in.get("alt"):
                text = " ".join(str(img_in.get("alt")).split())

        if not text or not title_is_ok(text):
            continue

        img_url = ""
        img = a.find("img")
        img_url = extract_image_url(img, TLAMA_BASE)

        if not img_url:
            parent = a.parent
            for _ in range(4):
                if not parent:
                    break
                img2 = parent.find("img")
                img_url = extract_image_url(img2, TLAMA_BASE)
                if img_url:
                    break
                parent = parent.parent

        items_by_url[url] = {
            "title": text,
            "url": url,
            "image_url": img_url,
        }

    out = []
    seen_titles = set()
    for it in items_by_url.values():
        nt = norm(it["title"])
        if nt in seen_titles:
            continue
        seen_titles.add(nt)
        out.append(it)

    return out


def match_expansion_to_owned(title: str, owned_norm_titles: list[str]) -> str | None:
    t = norm(title)
    for game_nt in owned_norm_titles:
        if len(game_nt) < 4:
            continue
        if game_nt in t:
            return game_nt
    return None


def build_email_html(owned_titles: list[str], items: list[dict]) -> tuple[str, str, int, int]:
    owned_norm = [norm(t) for t in owned_titles]

    expansions_for_owned = []
    new_games = []

    for it in items:
        title = it["title"]
        if looks_like_expansion(title):
            match = match_expansion_to_owned(title, owned_norm)
            if match:
                expansions_for_owned.append(it)
        else:
            new_games.append(it)

    expansions_for_owned = expansions_for_owned[:10]
    new_games = new_games[:12]

    exp_count = len(expansions_for_owned)
    game_count = len(new_games)

    lines = []
    lines.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}")
    lines.append("")
    lines.append("Ahoj! Tady jsou novinky z TLAMA (jen produkty /deskove-hry/).")
    lines.append("")

    lines.append("🧩 Rozšíření pro hry, které už máš:")
    if expansions_for_owned:
        for it in expansions_for_owned:
            lines.append(f"- {it['title']} — {it['url']}")
    else:
        lines.append("- (zatím nic jasného)")
    lines.append("")

    lines.append("🆕 Nové hry (TLAMA):")
    if new_games:
        for it in new_games:
            lines.append(f"- {it['title']} — {it['url']}")
    else:
        lines.append("- (zatím nic)")
    text_body = "\n".join(lines)

    def card(it):
        t = html.escape(it["title"])
        u = html.escape(it["url"])
        img = it.get("image_url") or ""
        if img:
            img_tag = f'<img src="{html.escape(img)}" alt="" style="width:64px;height:auto;border-radius:10px;display:block;">'
        else:
            img_tag = ""  # čistší: když není obrázek, neukazujeme placeholder
        left = f'<div style="flex:0 0 64px;">{img_tag}</div>' if img_tag else ""
        return f"""
        <div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid #2a2a2a;">
          {left}
          <div style="flex:1 1 auto;">
            <div style="font-size:15px;line-height:1.3;margin:0 0 4px 0;">
              <a href="{u}" style="color:#8ab4f8;text-decoration:none;">{t}</a>
            </div>
            <div style="font-size:12px;color:#9aa0a6;word-break:break-all;">{u}</div>
          </div>
        </div>
        """

    exp_html = "".join(card(it) for it in expansions_for_owned) or '<div style="color:#9aa0a6;">(zatím nic jasného)</div>'
    games_html = "".join(card(it) for it in new_games) or '<div style="color:#9aa0a6;">(zatím nic)</div>'

    html_body = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8eaed;background:#121212;padding:18px;">
      <h1 style="font-size:20px;margin:0 0 8px 0;">🎲 Deskovkový briefing – {date.today().isoformat()}</h1>
      <div style="color:#bdc1c6;margin:0 0 16px 0;">
        Ahoj! Novinky z TLAMA — jen deskovky (produkty), žádné menu/registrace. 🙂
      </div>

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🧩 Rozšíření pro hry, které už máš</h2>
      {exp_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🆕 Nové hry (TLAMA)</h2>
      {games_html}

      <div style="margin-top:16px;color:#9aa0a6;font-size:12px;">
        Zdroj: TLAMA “Novinky v češtině”. (Filtr: produktové stránky /deskove-hry/.)
      </div>
    </div>
    """.strip()

    return text_body, html_body, exp_count, game_count


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
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")

    owned = load_collection_titles(SHEET_CSV_URL)
    tlama_html = fetch_html(TLAMA_URL)
    items = extract_tlama_products(tlama_html)

    text_body, html_body, exp_count, game_count = build_email_html(owned, items)

    # dynamický subject podle obsahu
    subject = f"Deskovkový briefing – {exp_count} rozšíření + {game_count} novinek ({date.today().isoformat()})"

    send_email(subject, text_body, html_body)


if __name__ == "__main__":
    main()
