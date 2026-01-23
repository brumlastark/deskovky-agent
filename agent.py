import os
import re
import csv
import json
import time
import hashlib
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# OpenAI (new SDK)
from openai import OpenAI


# ----------------------------
# Config
# ----------------------------

HTTP_TIMEOUT = 30
MAX_LISTING_ITEMS_PER_SOURCE = 60      # kolik kandidátů vzít z listing stránky
MAX_GAMES_TOTAL = 120                  # hard stop, ať se to nerozjede
TOP_TIPS = 3                           # TOP AI tipy
TOP_TIPS_MIN_SCORE = 65                # práh pro TOP tipy (0-100)
AI_MAX_GAMES_TO_SCORE = 18             # aby se to nezbláznilo cenově (vybereme shortlist)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# Volitelné: pokud runner nepředá GROUP_CSV_URL jako env (často chybí mapování secrets→env),
# použijeme tento fallback (public CSV export). Můžeš změnit dle potřeby.
DEFAULT_GROUP_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQsZls09kQMlBDG8kMyzb-bjIpEV9ON8zbK6a1dYS9Imp9tUcgBzQmNrFH9dtq2ySIG_afmTewJx1-1/pub?output=csv"

# Zdroje: TLAMA držíme "top", ale bereme i další (pokud TLAMA nemá danou hru)
# Pozn.: některé weby mohou blokovat scraping (403) – to řešíme "best effort".
SOURCES = [
    {
        "id": "tlama_novinky",
        "name": "TLAMA – Novinky v češtině",
        "url": "https://www.tlamagames.com/novinky-v-cestine/",
        "base": "https://www.tlamagames.com",
        # bereme jen produktové stránky s deskovkami (řeší bordel typu menu, registrace, atd.)
        "product_url_must_contain": ["/deskove-hry/"],
        "product_url_must_not_contain": [
            "/kategorie/", "/znacky/", "/akce/", "/action/", "/registrace", "/prihlasit",
            "/kosik", "/podminky", "/ochrana", "/podpora", "/pujcovna", "/about", "/kontakt",
            "/affiliate", "/doprava", "/slevy", "/typy", "/festival", "/tel:", "/detske",
            "/pro-mladez", "/prorodinu", "/prosenior", "/strategicke-hry", "/prozkusenehrace",
            "/proholky", "/prokluky", "/hryprodva", "/hryprojednoho",
            # smrduté položky, co se objevovaly:
            "encyklopedie", "Deskové hry – nejširší nabídka"
        ],
        "prefer": True,
    },
    {
        "id": "tlama_predprodej",
        "name": "TLAMA – Předprodej",
        "url": "https://www.tlamagames.com/predprodej/",
        "base": "https://www.tlamagames.com",
        "product_url_must_contain": ["/deskove-hry/"],
        "product_url_must_not_contain": [
            "/kategorie/", "/znacky/", "/akce/", "/action/", "/registrace", "/prihlasit",
            "/kosik", "/podminky", "/ochrana", "/podpora", "/pujcovna", "/about", "/kontakt",
            "/affiliate", "/doprava", "/slevy", "/typy", "/festival", "/tel:",
            "encyklopedie", "Deskové hry – nejširší nabídka"
        ],
        "prefer": True,
    },
    # Kickstarter / další zdroje si můžeš nechávat jako best-effort (často 403); teď klidně vypnuté.
]


# ----------------------------
# Helpers
# ----------------------------

def env_required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val

def env_optional(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val


def http_get(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def normalize_title(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "").strip())
    return t


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def load_csv_from_url(url: str) -> List[List[str]]:
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    # Google CSV bývá utf-8
    content = r.content.decode("utf-8", errors="replace")
    reader = csv.reader(content.splitlines())
    return [row for row in reader if row]


def parse_owned_sheet(rows: List[List[str]]) -> List[str]:
    # očekáváme A1 = Titul, sloupec A vyplněný
    titles = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        if not row:
            continue
        title = row[0].strip()
        if title:
            titles.append(title)
    return titles


def parse_group_sheet(rows: List[List[str]]) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    """
    Očekávání (tvůj sheet):
    - sloupec A = "Kdo"
    - sloupec B = "Popis"
    - nahoře lidi: Honza, Káťa, Monča, Šimon + volné popisy
    - dole "meta" řádky: např. avoid_dice_heavy / players / session_length...
      (u meta je klíč ve sloupci A a hodnota ve sloupci B nebo C)
    """
    people = []
    meta: Dict[str, str] = {}

    # najdeme indexy hlaviček
    header = [c.strip().lower() for c in rows[0]]
    col_a = 0
    col_b = 1 if len(header) > 1 else 0

    for i, row in enumerate(rows[1:], start=2):
        if not row or len(row) < 1:
            continue
        a = (row[col_a] if len(row) > col_a else "").strip()
        b = (row[col_b] if len(row) > col_b else "").strip()

        if not a:
            continue

        # heuristika: jména vs meta klíče
        # pokud a vypadá jako jméno (bez podtržítek) a b je delší text -> člověk
        if "_" not in a and (len(b) > 10 or a.lower() in {"honza", "káťa", "monča", "šimon"}):
            people.append({"who": a, "desc": b})
        else:
            # meta: value preferujeme ze sloupce B, případně C
            val = b
            if not val and len(row) > 2:
                val = row[2].strip()
            if val:
                meta[a] = val

    return people, meta


def build_group_text(people: List[Dict[str, str]], meta: Dict[str, str]) -> str:
    lines = []
    lines.append("Skupina hraje nejčastěji ve 4 (Honza, Káťa, Monča, Šimon).")
    if meta:
        for k, v in meta.items():
            lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("Profily lidí:")
    for p in people:
        lines.append(f"- {p['who']}: {p['desc']}")
    return "\n".join(lines)


def url_allowed(url: str, src: dict) -> bool:
    must = src.get("product_url_must_contain") or []
    must_not = src.get("product_url_must_not_contain") or []

    for part in must:
        if part and part not in url:
            return False
    for part in must_not:
        if part and part in url:
            return False
    return True


def extract_candidate_urls(listing_html: str, base: str, src: dict) -> List[str]:
    soup = BeautifulSoup(listing_html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if href.startswith("/"):
            u = base.rstrip("/") + href
        elif href.startswith("http"):
            u = href
        else:
            continue

        # rychlá normalizace (odstranit #…)
        u = u.split("#")[0]
        if url_allowed(u, src):
            urls.append(u)

    # unikátní + zachovat pořadí
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:MAX_LISTING_ITEMS_PER_SOURCE]


def scrape_game_detail(url: str, src: dict) -> Dict[str, str]:
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = normalize_title(h1.get_text(" ", strip=True))

    if not title:
        title = normalize_title(soup.title.get_text(" ", strip=True) if soup.title else url)

    # zkusíme najít obrázek produktu
    img = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        img = og["content"].strip()

    # krátký text: meta description + první rozumné odstavce
    desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        desc = md["content"].strip()

    # fallback: pár odstavců
    if len(desc) < 80:
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        ps = [p for p in ps if len(p) > 60]
        desc = (desc + "\n" + "\n".join(ps[:3])).strip()

    return {
        "title": title,
        "url": url,
        "img": img,
        "desc": desc[:1200],  # hard cap
        "source": src["name"],
        "source_id": src["id"],
        "prefer": bool(src.get("prefer")),
    }


def scrape_source(src: dict) -> Tuple[List[Dict[str, str]], List[str]]:
    warns = []
    try:
        listing = http_get(src["url"])
    except Exception as e:
        warns.append(f"{src['name']}: nepodařilo se stáhnout ({e})")
        return [], warns

    candidates = extract_candidate_urls(listing, src["base"], src)
    items = []
    for u in candidates:
        try:
            it = scrape_game_detail(u, src)
            if it.get("title"):
                items.append(it)
            if len(items) >= MAX_LISTING_ITEMS_PER_SOURCE:
                break
        except Exception:
            # detail může občas failnout; ignorujeme
            continue

    return items, warns


def dedupe_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for it in items:
        key = sha1((it.get("title", "") + "|" + it.get("url", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def classify_owned_vs_new(items: List[Dict[str, str]], owned_titles: List[str]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    owned_norm = {normalize_title(t).lower() for t in owned_titles}
    expansions = []
    new_games = []
    for it in items:
        t = normalize_title(it["title"]).lower()
        # hrubá heuristika: pokud název obsahuje název vlastněné hry -> rozšíření
        is_exp = False
        for ot in owned_norm:
            if ot and ot in t and len(ot) >= 5 and ot != t:
                is_exp = True
                break
        if is_exp:
            expansions.append(it)
        else:
            new_games.append(it)
    return expansions, new_games


def ai_fit_score(client: OpenAI, group_text: str, game_title: str, game_blurb: str) -> dict:
    instructions = (
        "Jsi kurátor deskovek pro jednu konkrétní skupinu. "
        "Dostaneš profil skupiny a krátký popis hry. "
        "Ohodnoť, jak moc je hra fit pro skupinu (0–100). "
        "Buď konkrétní a stručný. "
        "Přidej poznámky pro konkrétní členy skupiny (Honza, Káťa, Monča, Šimon) – "
        "jen tam, kde je to relevantní, typicky 2–4 poznámky. "
        "Pokud hra výrazně stojí na kostkách/náhodě, uveď varování."
    )

    input_payload = (
        f"### PROFIL SKUPINY\n{group_text}\n\n"
        f"### HRA\nNázev: {game_title}\n"
        f"Popis:\n{game_blurb}\n\n"
        "### VÝSTUP\nVrať POUZE platné JSON (bez markdownu), přesně v tomto tvaru:\n"
        "{\n"
        '  "fit": 0,\n'
        '  "why": ["důvod 1", "důvod 2"],\n'
        '  "notes": [\n'
        '    {"who":"Honza","text":"..."}\n'
        "  ],\n"
        '  "warnings": ["varování 1"]\n'
        "}\n"
        "- fit musí být celé číslo 0–100.\n"
        "- why max 2 položky.\n"
        "- notes: 2–4 položky typicky, ale klidně 1–4 podle relevance. 'who' musí být přesně jedno z: Honza, Káťa, Monča, Šimon.\n"
        "- warnings max 2."
    )

    resp = client.responses.create(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=input_payload,
    )

    raw = (resp.output_text or "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return {"fit": 0, "why": ["AI odpověď nešla přečíst."], "notes": [], "warnings": ["AI output mimo JSON"]}
        data = json.loads(m.group(0))

    try:
        fit = int(data.get("fit", 0))
    except Exception:
        fit = 0
    fit = max(0, min(100, fit))

    why = data.get("why", [])
    if not isinstance(why, list):
        why = []
    why = [str(x) for x in why][:2]

    notes = data.get("notes", [])
    if not isinstance(notes, list):
        notes = []
    cleaned_notes = []
    allowed = {"Honza", "Káťa", "Monča", "Šimon"}
    for n in notes[:6]:
        if not isinstance(n, dict):
            continue
        who = str(n.get("who", "")).strip()
        text = str(n.get("text", "")).strip()
        if who in allowed and text:
            cleaned_notes.append({"who": who, "text": text})

    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    warnings = [str(x) for x in warnings][:2]

    return {"fit": fit, "why": why, "notes": cleaned_notes, "warnings": warnings}


def build_email(owned_titles: List[str], expansions: List[Dict[str, str]], new_games: List[Dict[str, str]], scored: List[Dict[str, str]], warns: List[str]) -> Tuple[str, str, str]:
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    subject = f"Deskovkový briefing – TOP {TOP_TIPS} tipy (AI) ({today})"

    # TOP tipy
    top = [x for x in scored if x.get("fit", 0) >= TOP_TIPS_MIN_SCORE]
    top = sorted(top, key=lambda x: x.get("fit", 0), reverse=True)[:TOP_TIPS]

    # TEXT
    lines = []
    lines.append(f"🎲 Deskovkový briefing — {today}")
    lines.append("Ahoj! Novinky z TLAMA — jen deskovky (produkty), žádné menu/registrace. 🙂")
    lines.append("")
    if top:
        lines.append("🏆 TOP tipy týdne (AI fit)")
        for t in top:
            lines.append(f"- {t['title']} — {t.get('fit',0)}/100")
            for w in t.get("why", []):
                lines.append(f"  • {w}")
            for n in t.get("notes", []):
                lines.append(f"  • {n['who']}: {n['text']}")
            for w in t.get("warnings", []):
                lines.append(f"  ⚠ {w}")
            lines.append(f"  {t['url']}")
            lines.append("")
    else:
        lines.append("🏆 TOP tipy týdne (AI fit)")
        lines.append("(tentokrát nic nad práh)")
        lines.append("")

    lines.append("🧩 Rozšíření pro hry, které už máš")
    if expansions:
        for it in expansions[:20]:
            lines.append(f"- {it['title']} — {it['url']}")
    else:
        lines.append("(zatím nic jasného)")
    lines.append("")

    lines.append("🆕 Nové hry (TLAMA)")
    for it in new_games[:60]:
        lines.append(f"- {it['title']} — {it['url']}")
    lines.append("")

    if warns:
        lines.append("⚠ Poznámky ke zdrojům")
        for w in warns:
            lines.append(f"- {w}")

    text_body = "\n".join(lines)

    # HTML
    def esc(s: str) -> str:
        import html
        return html.escape(s or "")

    parts = []
    parts.append(f'<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;max-width:720px;margin:0 auto;color:#e8eaed;">')
    parts.append(f'<h2 style="margin:0 0 6px 0;">🎲 Deskovkový briefing — {esc(today)}</h2>')
    parts.append(f'<div style="margin:0 0 18px 0;color:#bdc1c6;">Ahoj! Novinky z TLAMA — jen deskovky (produkty), žádné menu/registrace. 🙂</div>')

    # TOP
    parts.append('<h3 style="margin:22px 0 8px 0;">🏆 TOP tipy týdne (AI fit)</h3>')
    if top:
        for t in top:
            img = t.get("img") or ""
            parts.append('<div style="border:1px solid #303134;border-radius:12px;padding:12px;margin:10px 0;background:#171717;">')
            parts.append('<div style="display:flex;gap:12px;align-items:flex-start;">')
            if img:
                parts.append(f'<img src="{esc(img)}" alt="" width="56" height="56" style="border-radius:10px;object-fit:cover;background:#202124;border:1px solid #303134;">')
            parts.append('<div style="flex:1;">')
            parts.append(f'<div style="font-weight:700;font-size:15px;margin-bottom:3px;">{esc(t["title"])}</div>')
            parts.append(f'<div style="color:#8ab4f8;font-size:13px;margin-bottom:6px;">{esc(str(t.get("fit",0)))} / 100</div>')
            if t.get("why"):
                parts.append('<ul style="margin:6px 0 8px 18px;color:#bdc1c6;">')
                for w in t["why"]:
                    parts.append(f"<li>{esc(w)}</li>")
                parts.append("</ul>")
            notes = []
            for n in t.get("notes", []):
                notes.append(f"{esc(n['who'])}: {esc(n['text'])}")
            if notes:
                parts.append(f'<div style="margin:4px 0 0 0;color:#bdc1c6;font-size:12px;">{" | ".join(notes)}</div>')
            if t.get("warnings"):
                parts.append('<div style="margin-top:6px;color:#f28b82;font-size:12px;">')
                for w in t["warnings"]:
                    parts.append(f"⚠ {esc(w)}<br>")
                parts.append("</div>")
            parts.append(f'<div style="margin-top:8px;"><a href="{esc(t["url"])}" style="color:#c58af9;text-decoration:none;">{esc(t["url"])}</a></div>')
            parts.append('</div></div></div>')
    else:
        parts.append('<div style="color:#bdc1c6;">(tentokrát nic nad práh)</div>')

    # Expansions
    parts.append('<h3 style="margin:22px 0 8px 0;">🧩 Rozšíření pro hry, které už máš</h3>')
    if expansions:
        parts.append('<ul style="margin:6px 0 8px 18px;">')
        for it in expansions[:20]:
            parts.append(f'<li><a href="{esc(it["url"])}" style="color:#c58af9;text-decoration:none;">{esc(it["title"])}</a></li>')
        parts.append('</ul>')
    else:
        parts.append('<div style="color:#bdc1c6;">(zatím nic jasného)</div>')

    # New games
    parts.append('<h3 style="margin:22px 0 8px 0;">🆕 Nové hry (TLAMA)</h3>')
    for it in new_games[:60]:
        img = it.get("img") or ""
        parts.append('<div style="display:flex;gap:10px;align-items:flex-start;border-top:1px solid #303134;padding:10px 0;">')
        if img:
            parts.append(f'<img src="{esc(img)}" alt="" width="44" height="44" style="border-radius:10px;object-fit:cover;background:#202124;border:1px solid #303134;">')
        parts.append('<div style="flex:1;">')
        parts.append(f'<div style="font-weight:650;">{esc(it["title"])}</div>')
        parts.append(f'<div><a href="{esc(it["url"])}" style="color:#c58af9;text-decoration:none;">{esc(it["url"])}</a></div>')
        parts.append('</div></div>')

    # Warnings
    if warns:
        parts.append('<h3 style="margin:22px 0 8px 0;">⚠ Poznámky ke zdrojům</h3>')
        parts.append('<ul style="margin:6px 0 8px 18px;color:#bdc1c6;">')
        for w in warns:
            parts.append(f"<li>{esc(w)}</li>")
        parts.append("</ul>")

    parts.append("</div>")
    html_body = "\n".join(parts)

    return subject, text_body, html_body


def send_email(subject: str, text_body: str, html_body: str) -> None:
    host = env_required("SMTP_HOST")
    port = int(env_required("SMTP_PORT"))
    user = env_required("SMTP_USER")
    password = env_required("SMTP_PASS")
    mail_from = env_required("MAIL_FROM")
    mail_to = env_required("MAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=mail_from.split("@")[-1])

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context) as server:
        server.login(user, password)
        server.send_message(msg)


def select_shortlist_for_ai(new_games: List[Dict[str, str]]) -> List[Dict[str, str]]:
    # priorita: TLAMA prefer + hry s delším popisem
    ranked = sorted(
        new_games,
        key=lambda x: (
            0 if x.get("prefer") else 1,
            -len(x.get("desc", "")),
        )
    )
    return ranked[:AI_MAX_GAMES_TO_SCORE]


def main() -> None:
    global OPENAI_MODEL

    # env
    openai_key = env_required("OPENAI_API_KEY")
    openai_model = env_required("OPENAI_MODEL")
    group_csv_url = env_optional("GROUP_CSV_URL", DEFAULT_GROUP_CSV_URL)
    owned_csv_url = env_required("OWNED_CSV_URL")

    # group profile
    if not group_csv_url:
        raise RuntimeError("Missing env var: GROUP_CSV_URL (and DEFAULT_GROUP_CSV_URL is empty)")
    group_rows = load_csv_from_url(group_csv_url)
    people, meta = parse_group_sheet(group_rows)
    group_text = build_group_text(people, meta)

    # owned collection
    owned_rows = load_csv_from_url(owned_csv_url)
    owned_titles = parse_owned_sheet(owned_rows)

    # scrape sources
    all_items: List[Dict[str, str]] = []
    warns: List[str] = []

    for src in SOURCES:
        print(f"[INFO] Fetching: {src['name']} ({src['url']})")
        items, w = scrape_source(src)
        warns.extend(w)
        all_items.extend(items)
        if len(all_items) >= MAX_GAMES_TOTAL:
            break

    all_items = dedupe_items(all_items)

    expansions, new_games = classify_owned_vs_new(all_items, owned_titles)

    # AI scoring for shortlist
    OPENAI_MODEL = openai_model
    client = OpenAI(api_key=openai_key)

    shortlist = select_shortlist_for_ai(new_games)
    scored = []
    for it in shortlist:
        blurb = it.get("desc", "")
        try:
            sc = ai_fit_score(client, group_text, it["title"], blurb)
        except Exception as e:
            sc = {"fit": 0, "why": ["AI score fail"], "notes": [], "warnings": [str(e)]}
        it2 = dict(it)
        it2.update(sc)
        scored.append(it2)
        time.sleep(0.25)

    subject, text_body, html_body = build_email(
        owned_titles=owned_titles,
        expansions=expansions,
        new_games=new_games,
        scored=scored,
        warns=warns
    )

    send_email(subject, text_body, html_body)
    print("[OK] Email sent.")


if __name__ == "__main__":
    main()
