# agent.py (v4) — TLAMA + Kolekce + Skupina + AI "fit index" (Top 3)
import os
import re
import csv
import io
import html
import json
import unicodedata
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from openai import OpenAI


# === INPUTS ===
COLLECTION_CSV_URL = os.environ.get(
    "COLLECTION_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTmHPN69oIL7Fit5EN_K6HXtYtEPOZi2v-KmFL85D-wQsljrIT3cDY_Uh0LShOiIDfOx6rGJPlfESa2/pub?output=csv",
)

GROUP_CSV_URL = os.environ.get(
    "GROUP_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQsZls09kQMlBDG8kMyzb-bjIpEV9ON8zbK6a1dYS9Imp9tUcgBzQmNrFH9dtq2ySIG_afmTewJx1-1/pub?output=csv",
)

TLAMA_URL = "https://www.tlamagames.com/novinky-v-cestine/"
TLAMA_BASE = "https://www.tlamagames.com"

# === AI SETTINGS ===
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")  # levnější default
AI_SCORE_LIMIT = int(os.environ.get("AI_SCORE_LIMIT", "8"))  # kolik her týdně bodovat AI (kvůli ceně)
AI_TOP_N = int(os.environ.get("AI_TOP_N", "3"))              # kolik tipů ukázat nahoře


# === TLAMA product URL filter ===
PRODUCT_PATH_PREFIXES = ["/deskove-hry/"]

# keywords suggesting expansion
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

# exact blacklist by title (normalized)
TITLE_BLACKLIST_EXACT = {
    "deskové hry", "deskove hry",
}

# path blacklist (stable)
PATH_BLACKLIST_CONTAINS = [
    "/registrace", "/klient/", "/kosik", "/doprava_", "/obchodni-", "/podminky",
    "/prosenior", "/prorodinu", "/prozkusenehrace", "/strategicke-hry", "/hrypro",
    "/3-6let", "/7-12let", "/13-let", "/proholky", "/prokluky",
    "/tipy", "/sleva", "/akce", "/festival", "/deskovcon",
    "/action/language", "/affiliate_",
    # your “stinkers”
    "/deskove-hry/encyklopedie",
    "/deskove-hry/deskove-hry",
]


# === helpers ===
def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def looks_like_expansion(title: str) -> bool:
    t = norm(title)
    return any(norm(k) in t for k in EXPANSION_KEYWORDS)


def fetch(url: str, *, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
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

    # listing itself is not a product
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
        url = absolute_url(TLAMA_BASE, a.get("href", ""))
        if not is_product_url(url):
            continue

        text = " ".join(a.get_text(" ", strip=True).split())
        if not text:
            img_in = a.find("img")
            if img_in and img_in.get("alt"):
                text = " ".join(str(img_in.get("alt")).split())

        if not text or not title_is_ok(text):
            continue

        img_url = extract_image_url(a.find("img"), TLAMA_BASE)
        if not img_url:
            parent = a.parent
            for _ in range(4):
                if not parent:
                    break
                img_url = extract_image_url(parent.find("img"), TLAMA_BASE)
                if img_url:
                    break
                parent = parent.parent

        items_by_url[url] = {"title": text, "url": url, "image_url": img_url}

    # de-dup by normalized title
    out, seen = [], set()
    for it in items_by_url.values():
        nt = norm(it["title"])
        if nt in seen:
            continue
        seen.add(nt)
        out.append(it)
    return out


def load_csv_rows(csv_url: str) -> list[list[str]]:
    r = requests.get(csv_url, timeout=30, headers={"User-Agent": "DeskovkyAgent/4.0"})
    r.raise_for_status()
    raw = r.text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(raw))
    return list(reader)


def load_collection_titles(csv_url: str) -> list[str]:
    rows = load_csv_rows(csv_url)
    if not rows:
        return []
    header = rows[0]
    col_idx = 0
    for i, h in enumerate(header):
        if norm(h) == "titul":
            col_idx = i
            break

    titles = []
    for row in rows[1:]:
        if col_idx < len(row):
            t = row[col_idx].strip()
            if t:
                titles.append(t)

    out, seen = [], set()
    for t in titles:
        nt = norm(t)
        if nt not in seen:
            seen.add(nt)
            out.append(t)
    return out


def load_group_profile(csv_url: str) -> tuple[dict, dict]:
    """
    Expects header: Kdo | Popis
    Returns: (people_profiles, meta)
      people_profiles: {"Honza": "...", "Káťa": "...", ...}
      meta: {"players": "4", "avoid_dice_heavy": "yes", ...}
    """
    rows = load_csv_rows(csv_url)
    if not rows:
        return {}, {}

    header = rows[0]
    kdo_idx = 0
    popis_idx = 1 if len(header) > 1 else 0
    for i, h in enumerate(header):
        if norm(h) == "kdo":
            kdo_idx = i
        if norm(h) == "popis":
            popis_idx = i

    people = {}
    meta = {}
    for row in rows[1:]:
        if kdo_idx >= len(row):
            continue
        kdo = row[kdo_idx].strip()
        if not kdo:
            continue
        popis = row[popis_idx].strip() if popis_idx < len(row) else ""

        # heuristic: meta keys often include underscores OR are known meta keys
        k_norm = norm(kdo)
        if "_" in k_norm or k_norm in {"players", "avoid_dice_heavy", "session_length"}:
            meta[k_norm] = popis.strip() if popis else ""
        else:
            if popis:
                people[kdo.strip()] = popis.strip()

    return people, meta


def match_expansion_to_owned(title: str, owned_norm_titles: list[str]) -> str | None:
    t = norm(title)
    for game_nt in owned_norm_titles:
        if len(game_nt) < 4:
            continue
        if game_nt in t:
            return game_nt
    return None


def extract_game_blurb_from_product_page(product_html: str) -> str:
    soup = BeautifulSoup(product_html, "html.parser")

    # try meta descriptions first
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return " ".join(og["content"].split())

    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        return " ".join(desc["content"].split())

    # fallback: try a short chunk of visible text
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    # take a sane slice
    return text[:600]


def summarize_group_for_prompt(people: dict, meta: dict) -> str:
    parts = []
    if meta:
        # keep it short & explicit for the model
        if meta.get("players"):
            parts.append(f"- Typicky hráčů: {meta.get('players')}")
        if meta.get("avoid_dice_heavy"):
            parts.append(f"- Vyhýbáme se hrám s velkým důrazem na kostky/náhodu: {meta.get('avoid_dice_heavy')}")
        if meta.get("session_length"):
            parts.append(f"- Délka sezení (realita): {meta.get('session_length')}")
    meta_block = "\n".join(parts).strip()

    people_lines = []
    for name, profile in people.items():
        people_lines.append(f"{name}: {profile}")
    people_block = "\n".join(people_lines).strip()

    out = []
    if meta_block:
        out.append("META:\n" + meta_block)
    if people_block:
        out.append("PROFILY:\n" + people_block)
    return "\n\n".join(out).strip()


def ai_fit_score(client: OpenAI, group_text: str, game_title: str, game_blurb: str) -> dict:
    """
    Returns dict:
      {"fit": int 0-100, "why": [..], "m_note": str, "s_note": str, "warnings": [..]}
    """
    instructions = (
        "Jsi kurátor deskovek pro jednu konkrétní skupinu. "
        "Dostaneš profil skupiny a krátký popis hry. "
        "Ohodnoť, jak moc je hra fit pro skupinu (0–100). "
        "Buď konkrétní, ale stručný. "
        "Zvlášť přidej 1 krátkou poznámku k hráčům M (Monča) a Š (Šimon), "
        "ideálně vtipně, ale ne cringe. "
        "Pokud hra výrazně stojí na kostkách/náhodě, dej varování."
    )

    input_payload = (
        f"### PROFIL SKUPINY\n{group_text}\n\n"
        f"### HRA\nNázev: {game_title}\n"
        f"Popis:\n{game_blurb}\n\n"
        "### VÝSTUP\nVrať POUZE platné JSON (bez markdownu), přesně v tomto tvaru:\n"
        "{\n"
        '  "fit": 0,\n'
        '  "why": ["důvod 1", "důvod 2"],\n'
        '  "m_note": "krátká poznámka pro M",\n'
        '  "s_note": "krátká poznámka pro Š",\n'
        '  "warnings": ["varování 1"]\n'
        "}\n"
        "fit musí být celé číslo 0–100. why max 2 položky."
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        reasoning={"effort": "low"},
        instructions=instructions,
        input=input_payload,
    )

    raw = (resp.output_text or "").strip()
    # attempt to parse JSON even if model wraps it
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return {"fit": 0, "why": ["AI odpověď nešla přečíst."], "m_note": "", "s_note": "", "warnings": ["formát AI outputu mimo JSON"]}
        data = json.loads(m.group(0))

    # sanitize
    fit = data.get("fit", 0)
    try:
        fit = int(fit)
    except Exception:
        fit = 0
    fit = max(0, min(100, fit))

    why = data.get("why", [])
    if not isinstance(why, list):
        why = []
    why = [str(x) for x in why][:2]

    m_note = str(data.get("m_note", "")).strip()
    s_note = str(data.get("s_note", "")).strip()

    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    warnings = [str(x) for x in warnings][:2]

    return {"fit": fit, "why": why, "m_note": m_note, "s_note": s_note, "warnings": warnings}


def build_email(owned_titles: list[str], items: list[dict], people: dict, meta: dict) -> tuple[str, str]:
    owned_norm = [norm(t) for t in owned_titles]

    expansions_for_owned = []
    new_games = []
    for it in items:
        title = it["title"]
        if looks_like_expansion(title):
            if match_expansion_to_owned(title, owned_norm):
                expansions_for_owned.append(it)
        else:
            new_games.append(it)

    expansions_for_owned = expansions_for_owned[:10]
    new_games = new_games[:12]

    # === AI scoring (Top N) ===
    top_block = []
    scored = []
    if os.environ.get("OPENAI_API_KEY"):
        client = OpenAI()
        group_text = summarize_group_for_prompt(people, meta)

        # score only first AI_SCORE_LIMIT games (cost control)
        candidates = new_games[:AI_SCORE_LIMIT]
        for it in candidates:
            try:
                product_html = fetch(it["url"])
                blurb = extract_game_blurb_from_product_page(product_html)
            except Exception:
                blurb = it["title"]

            score = ai_fit_score(client, group_text, it["title"], blurb)
            scored.append((it, score))

        scored.sort(key=lambda x: x[1].get("fit", 0), reverse=True)
        top = scored[:AI_TOP_N]

        for it, score in top:
            top_block.append({"item": it, "score": score})

    # === Plain text ===
    lines = []
    lines.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}")
    lines.append("")
    if top_block:
        lines.append("🏆 TOP tipy týdne (AI fit pro skupinu):")
        for t in top_block:
            it = t["item"]; sc = t["score"]
            lines.append(f"- {sc['fit']}/100 — {it['title']}")
            for w in sc.get("why", []):
                lines.append(f"  • {w}")
            if sc.get("m_note"):
                lines.append(f"  • M: {sc['m_note']}")
            if sc.get("s_note"):
                lines.append(f"  • Š: {sc['s_note']}")
            for warn in sc.get("warnings", []):
                lines.append(f"  ⚠️ {warn}")
            lines.append(f"  {it['url']}")
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

    # === HTML ===
    def card(it, extra_html=""):
        t = html.escape(it["title"])
        u = html.escape(it["url"])
        img = it.get("image_url") or ""
        img_tag = f'<img src="{html.escape(img)}" alt="" style="width:64px;height:auto;border-radius:10px;display:block;">' if img else ""
        left = f'<div style="flex:0 0 64px;">{img_tag}</div>' if img_tag else ""
        return f"""
        <div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid #2a2a2a;">
          {left}
          <div style="flex:1 1 auto;">
            <div style="font-size:15px;line-height:1.3;margin:0 0 4px 0;">
              <a href="{u}" style="color:#8ab4f8;text-decoration:none;">{t}</a>
            </div>
            {extra_html}
            <div style="font-size:12px;color:#9aa0a6;word-break:break-all;">{u}</div>
          </div>
        </div>
        """

    top_html = ""
    if top_block:
        blocks = []
        for t in top_block:
            it = t["item"]; sc = t["score"]
            why = sc.get("why", [])
            m_note = sc.get("m_note", "")
            s_note = sc.get("s_note", "")
            warnings = sc.get("warnings", [])

            parts = [f'<div style="margin:6px 0 0 0;color:#e8eaed;font-size:13px;">'
                     f'<b>{sc["fit"]}/100</b> — {html.escape(" • ".join(why))}</div>']

            notes = []
            if m_note:
                notes.append(f'M: {html.escape(m_note)}')
            if s_note:
                notes.append(f'Š: {html.escape(s_note)}')
            if notes:
                parts.append(f'<div style="margin:4px 0 0 0;color:#bdc1c6;font-size:12px;">{" | ".join(notes)}</div>')

            if warnings:
                parts.append(f'<div style="margin:4px 0 0 0;color:#f28b82;font-size:12px;">⚠️ {html.escape(" • ".join(warnings))}</div>')

            extra = "\n".join(parts)
            blocks.append(card(it, extra_html=extra))

        top_html = """
        <h2 style="font-size:16px;margin:18px 0 8px 0;">🏆 TOP tipy týdne (AI fit)</h2>
        """ + "".join(blocks)

    exp_html = "".join(card(it) for it in expansions_for_owned) or '<div style="color:#9aa0a6;">(zatím nic jasného)</div>'
    games_html = "".join(card(it) for it in new_games) or '<div style="color:#9aa0a6;">(zatím nic)</div>'

    html_body = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8eaed;background:#121212;padding:18px;">
      <h1 style="font-size:20px;margin:0 0 8px 0;">🎲 Deskovkový briefing – {date.today().isoformat()}</h1>
      <div style="color:#bdc1c6;margin:0 0 16px 0;">
        Ahoj! Novinky z TLAMA — jen deskovky (produkty), žádné menu/registrace. 🙂
      </div>

      {top_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🧩 Rozšíření pro hry, které už máš</h2>
      {exp_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🆕 Nové hry (TLAMA)</h2>
      {games_html}

      <div style="margin-top:16px;color:#9aa0a6;font-size:12px;">
        Zdroj: TLAMA “Novinky v češtině”. (Filtr: produktové stránky /deskove-hry/.)
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
    # minimal required env
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars: {missing}")

    # load inputs
    owned = load_collection_titles(COLLECTION_CSV_URL)
    people, meta = load_group_profile(GROUP_CSV_URL)

    tlama_html = fetch(TLAMA_URL)
    items = extract_tlama_products(tlama_html)

    # build mail
    text_body, html_body = build_email(owned, items, people, meta)

    # dynamic subject
    # (we don't parse counts from HTML, just do quick heuristics here)
    subject = f"Deskovkový briefing – {date.today().isoformat()}"
    if os.environ.get("OPENAI_API_KEY"):
        subject = f"Deskovkový briefing – TOP {AI_TOP_N} tipy (AI) ({date.today().isoformat()})"

    send_email(subject, text_body, html_body)


if __name__ == "__main__":
    main()
