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

# Zdroje: TLAMA držíme "top", ale bereme i ostatní.
# allowed_contains = jen URL, které "smrdí" produktovou stránkou (ne menu/košík/registrace atd.)
SOURCES = [
    {
        "name": "TLAMA – Novinky v češtině",
        "url": "https://www.tlamagames.com/novinky-v-cestine/",
        "base": "https://www.tlamagames.com",
        "priority": 100,
        "allowed_contains": ["/deskove-hry/"],
        "blocked_contains": [
            "/kosik", "/registrace", "/prihlaseni", "/zapomenute-heslo",
            "/obchodni-podminky", "/ochrany-osobnich-udaju", "/doprava",
            "/kontakt", "/vernostni", "/affiliate", "/kategorie", "tel:"
        ],
    },
    {
        "name": "TLAMA – Předprodej",
        "url": "https://www.tlamagames.com/predprodej/",
        "base": "https://www.tlamagames.com",
        "priority": 95,
        "allowed_contains": ["/deskove-hry/"],
        "blocked_contains": ["/kosik", "/registrace", "/prihlaseni", "tel:"],
    },

    {
        "name": "Rexhry – Katalog (nejnovější)",
        "url": "https://www.rexhry.cz/katalog",
        "base": "https://www.rexhry.cz",
        "priority": 70,
        "allowed_contains": ["/hra/"],
        "blocked_contains": ["/katalog", "/pripravujeme", "/kosik", "/kontakt"],
    },
    {
        "name": "Rexhry – Připravujeme",
        "url": "https://www.rexhry.cz/pripravujeme",
        "base": "https://www.rexhry.cz",
        "priority": 72,
        "allowed_contains": ["/hra/"],
        "blocked_contains": ["/katalog", "/kosik", "/kontakt"],
    },

    {
        "name": "Albi – Hry",
        "url": "https://albi.cz/hry/",
        "base": "https://albi.cz",
        "priority": 60,
        "allowed_contains": ["/hry/"],
        "blocked_contains": ["/cteni-pro-radost/", "/kosik", "/ucet", "/kontakt"],
    },
    {
        "name": "Albi – Ediční plán",
        "url": "https://albi.cz/cteni-pro-radost/edicni-plan-her-albi/",
        "base": "https://albi.cz",
        "priority": 62,
        "allowed_contains": ["/hry/", "/produkty/", "/produkt/"],
        "blocked_contains": ["/cteni-pro-radost/", "/kosik", "/ucet", "/kontakt"],
    },

    {
        "name": "Asmodee CZ – Katalog",
        "url": "https://www.asmodee.cz/katalog-her/",
        "base": "https://www.asmodee.cz",
        "priority": 55,
        "allowed_contains": ["/hra/"],
        "blocked_contains": ["/katalog-her", "/pripravujeme", "/kontakt", "/o-nas"],
    },
    {
        "name": "Asmodee CZ – Připravujeme",
        "url": "https://www.asmodee.cz/pripravujeme/",
        "base": "https://www.asmodee.cz",
        "priority": 58,
        "allowed_contains": ["/hra/"],
        "blocked_contains": ["/katalog-her", "/kontakt", "/o-nas"],
    },

    {
        "name": "MindOK – Naše hry (nejnovější)",
        "url": "https://mindok.cz/nase-hry/",
        "base": "https://mindok.cz",
        "priority": 50,
        "allowed_contains": ["/hra/"],
        "blocked_contains": ["/nase-hry", "/pripravujeme", "/rubriky", "/clanky", "/kosik"],
    },
    {
        "name": "MindOK – Připravujeme",
        "url": "https://mindok.cz/hry/pripravujeme/",
        "base": "https://mindok.cz",
        "priority": 52,
        "allowed_contains": ["/hra/", "/clanky/"],
        "blocked_contains": ["/rubriky", "/kosik"],
    },

    # Kickstarter bývá 403 – necháváme jako "best effort"
    {
        "name": "Kickstarter – Tabletop games",
        "url": "https://www.kickstarter.com/discover/categories/games/tabletop%20games",
        "base": "https://www.kickstarter.com",
        "priority": 10,
        "allowed_contains": ["/projects/"],
        "blocked_contains": ["/discover/", "/login", "/search"],
    },
]


# ----------------------------
# Helpers
# ----------------------------

def env_required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val

def fetch_text(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text

def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def absolutize(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return base.rstrip("/") + href
    # relativní
    return base.rstrip("/") + "/" + href

def safe_list(items) -> List[str]:
    """
    Ošetří "bordel" typu list[dict] / None / str.
    Vrací jen list[str].
    """
    out: List[str] = []
    if not items:
        return out
    if isinstance(items, str):
        return [items]
    if isinstance(items, list):
        for x in items:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                # vem všechny hodnoty, které jsou string
                for v in x.values():
                    if isinstance(v, str):
                        out.append(v)
    return out

def url_allowed(url: str, src: dict) -> bool:
    if not url or not isinstance(url, str):
        return False
    allowed_contains = safe_list(src.get("allowed_contains"))
    blocked_contains = safe_list(src.get("blocked_contains"))

    # musí sedět allow (pokud je definované)
    if allowed_contains:
        ok = False
        for part in allowed_contains:
            if part and part in url:
                ok = True
                break
        if not ok:
            return False

    for part in blocked_contains:
        if part and part in url:
            return False

    return True

def extract_candidate_urls(listing_html: str, base: str, src: dict) -> List[str]:
    soup = BeautifulSoup(listing_html, "html.parser")
    urls: List[str] = []
    for a in soup.find_all("a", href=True):
        u = absolutize(base, a.get("href"))
        if not u:
            continue
        if url_allowed(u, src):
            urls.append(u)

    # dedupe při zachování pořadí
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:MAX_LISTING_ITEMS_PER_SOURCE]

def get_title_and_blurb(url: str, html: str) -> Tuple[str, str, Optional[str]]:
    """
    Vrátí: title, blurb (krátký popis), image_url (OG image když jde)
    """
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = normalize_ws(soup.title.string)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = normalize_ws(h1.get_text(" ", strip=True))
    title = title[:160] if title else url

    # meta description / první odstavec
    desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        desc = normalize_ws(md["content"])
    if not desc:
        ogd = soup.find("meta", attrs={"property": "og:description"})
        if ogd and ogd.get("content"):
            desc = normalize_ws(ogd["content"])
    if not desc:
        p = soup.find("p")
        if p:
            desc = normalize_ws(p.get_text(" ", strip=True))
    desc = desc[:500]

    # OG image
    img_url = None
    ogi = soup.find("meta", attrs={"property": "og:image"})
    if ogi and ogi.get("content"):
        img_url = ogi["content"].strip()
    if img_url and img_url.startswith("//"):
        img_url = "https:" + img_url

    return title, desc, img_url


# ----------------------------
# Sheets (CSV)
# ----------------------------

def load_csv_from_url(url: str) -> List[List[str]]:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    content = r.content.decode("utf-8", errors="replace")
    rows = []
    reader = csv.reader(content.splitlines())
    for row in reader:
        rows.append(row)
    return rows

def parse_group_sheet(rows: List[List[str]]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Očekává formát:
    A: Kdo, B: Popis
    + případně "key: value" řádky dole (players, avoid_dice_heavy, session_length…)
    Vrací:
      people: {name: description}
      meta: {key: value}
    """
    people: Dict[str, str] = {}
    meta: Dict[str, str] = {}

    for i, row in enumerate(rows):
        if not row or len(row) < 2:
            continue
        a = normalize_ws(row[0])
        b = normalize_ws(row[1] if len(row) > 1 else "")
        if not a or a.lower() == "kdo":
            continue
        if a in ["Honza", "Káťa", "Monča", "Šimon"]:
            if b:
                people[a] = b
        else:
            # meta řádky typu avoid_dice_heavy / players / session_length
            if b:
                meta[a] = b

    return people, meta

def parse_owned_sheet(rows: List[List[str]]) -> List[str]:
    """
    Očekává sloupec A "Titul" a hodnoty pod tím.
    """
    titles = []
    for i, row in enumerate(rows):
        if not row:
            continue
        a = normalize_ws(row[0])
        if not a or a.lower() == "titul":
            continue
        titles.append(a)
    # dedupe
    seen = set()
    out = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ----------------------------
# AI Scoring
# ----------------------------

@dataclass
class FitResult:
    score: int
    tagline: str
    notes: Dict[str, str]  # per person
    warnings: List[str]    # e.g. randomness caveat

def ai_fit_score(client: OpenAI, model: str, group_people: Dict[str, str], meta: Dict[str, str],
                 game_title: str, game_desc: str, game_url: str) -> FitResult:
    """
    Vrátí:
      score 0-100
      tagline 1 věta
      notes pro každého člověka (může být prázdné)
      warnings (max 2)
    """
    people_order = ["Honza", "Káťa", "Monča", "Šimon"]
    people_text = "\n".join([f"- {p}: {group_people.get(p,'')}" for p in people_order])
    meta_text = "\n".join([f"- {k}: {v}" for k, v in meta.items()])

    system = (
        "Jsi interní deskovkový scout pro skupinu 4 lidí. "
        "Hodnotíš fit hry pro konkrétní skupinu podle popisu lidí a metadat. "
        "Skupina nemá ráda kostkové festivaly a čistě náhodné 'roll&pray' hry; "
        "škodící/konfliktní mechaniky nevadí. "
        "Výstup musí být stručný, konkrétní a v češtině."
    )

    user = f"""
Skupina (popisy):
{people_text}

Meta:
{meta_text}

Hra:
- název: {game_title}
- url: {game_url}
- popis (může být stručný/nekvalitní): {game_desc}

Úkol:
1) Dej skóre 0–100: jak moc to sedne skupině jako celku.
2) Napiš 1 větu "tagline" pro email.
3) Napiš krátké poznámky k jednotlivým lidem (Honza, Káťa, Monča, Šimon).
   - Klidně nech některé prázdné, ale snaž se, aby u TOP kandidátů byl obvykle pokrytý alespoň 3 ze 4 lidí.
   - Nepiš slohovku: 1–2 věty na osobu.
4) Pokud je ve hře významná náhoda/kostky, dej varování (max 2) – stručně.

Vrať STRICTNĚ JSON v tomto tvaru:
{{
  "score": 0,
  "tagline": "...",
  "notes": {{
    "Honza": "... nebo prázdný řetězec",
    "Káťa": "...",
    "Monča": "...",
    "Šimon": "..."
  }},
  "warnings": ["...", "..."]
}}
""".strip()

    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
    )

    text = resp.output_text.strip()
    # někdy model omylem obalí ```json ... ```
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    data = json.loads(text)

    score = int(data.get("score", 0))
    score = max(0, min(100, score))
    tagline = normalize_ws(data.get("tagline", ""))[:220]
    notes = data.get("notes", {}) or {}
    warnings = data.get("warnings", []) or []

    # normalizace klíčů
    out_notes: Dict[str, str] = {}
    for p in ["Honza", "Káťa", "Monča", "Šimon"]:
        v = notes.get(p, "")
        if not isinstance(v, str):
            v = ""
        out_notes[p] = normalize_ws(v)

    out_warn = []
    for w in warnings[:2]:
        if isinstance(w, str):
            w = normalize_ws(w)
            if w:
                out_warn.append(w)

    return FitResult(score=score, tagline=tagline, notes=out_notes, warnings=out_warn)


# ----------------------------
# Email rendering
# ----------------------------

def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def render_game_html(item: dict, fit: Optional[FitResult], show_source: bool = True) -> str:
    title = html_escape(item["title"])
    url = html_escape(item["url"])
    src = html_escape(item["source"])
    img = item.get("image")

    fit_line = ""
    notes_html = ""
    warn_html = ""

    if fit:
        fit_line = f'<div style="margin:4px 0 6px 0;"><b>{fit.score}/100</b> — {html_escape(fit.tagline)}</div>'

        # poznámky pro lidi: vytáhneme jen ty neprázdné (typicky 2–4)
        parts = []
        for who in ["Honza", "Káťa", "Monča", "Šimon"]:
            note = fit.notes.get(who, "")
            if note:
                parts.append(f"<div><b>{html_escape(who)}:</b> {html_escape(note)}</div>")
        if parts:
            notes_html = "<div style='margin:6px 0 0 0;'>" + "".join(parts) + "</div>"

        if fit.warnings:
            warn_html = "<div style='margin:8px 0 0 0; color:#b45309;'><b>⚠</b> " + html_escape(" • ".join(fit.warnings)) + "</div>"

    img_html = ""
    if img:
        img_html = f"""
        <td style="width:72px; vertical-align:top; padding-right:10px;">
          <img src="{html_escape(img)}" style="width:64px; height:64px; object-fit:cover; border-radius:10px; border:1px solid rgba(255,255,255,0.12);" />
        </td>
        """

    src_line = f"<div style='opacity:0.75; font-size:12px;'>{src}</div>" if show_source else ""
    return f"""
    <table role="presentation" style="width:100%; border-collapse:collapse; margin:10px 0 12px 0;">
      <tr>
        {img_html}
        <td style="vertical-align:top;">
          <div style="font-size:16px; font-weight:700; margin-bottom:4px;">{title}</div>
          <div style="margin-bottom:6px;"><a href="{url}" style="color:#a78bfa;">{url}</a></div>
          {fit_line}
          {notes_html}
          {warn_html}
          {src_line}
        </td>
      </tr>
    </table>
    <hr style="border:none; border-top:1px solid rgba(255,255,255,0.10); margin:12px 0;">
    """.strip()

def build_subject(top: List[Tuple[dict, FitResult]], date_str: str) -> str:
    if not top:
        return f"Deskovkový briefing – {date_str}"
    # vezmeme 1–2 názvy pro dynamiku
    names = [t[0]["title"] for t in top[:2]]
    short = " / ".join(names)
    short = re.sub(r"\s+", " ", short).strip()
    if len(short) > 55:
        short = short[:52] + "…"
    return f"Deskovkový briefing – TOP tipy: {short} ({date_str})"

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

    # důležité hlavičky (Gmail je na to citlivý)
    msg["Message-ID"] = make_msgid()
    msg["X-Entity-Ref-ID"] = hashlib.sha1((subject + str(time.time())).encode("utf-8")).hexdigest()

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        if port == 587:
            server.starttls(context=context)
            server.ehlo()
        server.login(user, password)
        server.send_message(msg)


# ----------------------------
# Main pipeline
# ----------------------------

def scrape_source(src: dict) -> Tuple[List[dict], Optional[str]]:
    """
    Vrací:
      items: [{title,url,source,image}]
      warn: string když zdroj padl
    """
    try:
        html = fetch_text(src["url"])
        candidates = extract_candidate_urls(html, src["base"], src)

        items: List[dict] = []
        for u in candidates:
            try:
                page_html = fetch_text(u)
                title, desc, img = get_title_and_blurb(u, page_html)

                # hrubý filtr: title co vypadá jako "kategorie" / "všechny hry"
                bad_titles = ["Deskové hry", "Hry", "Katalog her", "Všechny hry"]
                if title in bad_titles:
                    continue

                items.append({
                    "title": title,
                    "url": u,
                    "desc": desc,
                    "image": img,
                    "source": src["name"],
                    "priority": int(src.get("priority", 0)),
                })
                if len(items) >= MAX_LISTING_ITEMS_PER_SOURCE:
                    break
            except requests.HTTPError as e:
                # některé produktové stránky mohou být bloknuté – přeskočíme
                continue
            except Exception:
                continue

        return items, None
    except requests.HTTPError as e:
        return [], f"{src['name']}: nepodařilo se stáhnout ({str(e)})"
    except Exception as e:
        return [], f"{src['name']}: chyba ({str(e)})"

def dedupe_items(items: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for it in items:
        key = it["url"]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out

def shortlist_for_ai(items: List[dict]) -> List[dict]:
    """
    Vybereme kandidáty pro AI skórování:
    - preferuj vyšší priority (TLAMA)
    - trochu diverzity
    """
    items_sorted = sorted(items, key=lambda x: (-x["priority"], x["title"]))
    return items_sorted[:AI_MAX_GAMES_TO_SCORE]

def main():
    # env
    openai_key = env_required("OPENAI_API_KEY")
    openai_model = env_required("OPENAI_MODEL")
    group_csv_url = env_required("GROUP_CSV_URL")
    owned_csv_url = env_required("OWNED_CSV_URL")

    # group profile
    group_rows = load_csv_from_url(group_csv_url)
    people, meta = parse_group_sheet(group_rows)

    # owned collection
    owned_rows = load_csv_from_url(owned_csv_url)
    owned_titles = parse_owned_sheet(owned_rows)
    owned_set = set(t.lower() for t in owned_titles)

    # scrape all sources
    all_items: List[dict] = []
    warnings: List[str] = []

    for src in SOURCES:
        items, warn = scrape_source(src)
        if warn:
            warnings.append(warn)
        all_items.extend(items)
        if len(all_items) >= MAX_GAMES_TOTAL:
            break

    all_items = dedupe_items(all_items)

    # rozdělíme na "owned-related expansions" vs "new"
    expansions = []
    new_games = []

    # heuristika: když název obsahuje název hry z kolekce (část), je to kandidát na rozšíření
    owned_tokens = sorted([t for t in owned_titles if len(t) >= 4], key=len, reverse=True)

    for it in all_items:
        t_low = it["title"].lower()
        # když je to přímo hra kterou už máš, nebudeme ji tlačit jako novinku
        if t_low in owned_set:
            continue

        is_exp = False
        if any(x in t_low for x in ["rozšíření", "expanze", "expansion", "extension", "vodní světy"]):
            # zkus přiřadit k existující hře podle substringu
            for ot in owned_tokens[:40]:
                if ot.lower() in t_low:
                    is_exp = True
                    break
        if is_exp:
            expansions.append(it)
        else:
            new_games.append(it)

    # AI scoring pro shortlist
    client = OpenAI(api_key=openai_key)

    scored: List[Tuple[dict, FitResult]] = []
    shortlist = shortlist_for_ai(new_games)

    for it in shortlist:
        try:
            fit = ai_fit_score(
                client=client,
                model=openai_model,
                group_people=people,
                meta=meta,
                game_title=it["title"],
                game_desc=it.get("desc", ""),
                game_url=it["url"],
            )
            scored.append((it, fit))
        except Exception as e:
            # když AI failne, jen přeskočíme
            continue

    scored_sorted = sorted(scored, key=lambda x: x[1].score, reverse=True)
    top = [(it, fit) for (it, fit) in scored_sorted if fit.score >= TOP_TIPS_MIN_SCORE][:TOP_TIPS]

    # aby se TOP tipy neopakovaly níž
    top_urls = set([it["url"] for it, _ in top])
    new_games_rest = [it for it in new_games if it["url"] not in top_urls]

    # Zredukujeme "rest" – ať to není nekonečný
    new_games_rest = sorted(new_games_rest, key=lambda x: (-x["priority"], x["title"]))[:35]
    expansions = sorted(expansions, key=lambda x: (-x["priority"], x["title"]))[:12]

    # build email (HTML)
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%Y-%m-%d")

    intro = "Ahoj! TLAMA držíme jako hlavní zdroj. Když je hra i jinde, bereme TLAMA link. Když TLAMA nemá, bereme ostatní. 🙂"
    subject = build_subject(top, date_str)

    html = f"""
    <html>
    <body style="background:#0b0b0f; color:#e5e7eb; font-family: ui-sans-serif, system-ui, -apple-system; line-height:1.45; padding:18px;">
      <div style="max-width:820px; margin:0 auto;">
        <div style="font-size:22px; font-weight:800; margin-bottom:6px;">🎲 Deskovkový briefing – {date_str}</div>
        <div style="opacity:0.9; margin-bottom:18px;">{html_escape(intro)}</div>

        <div style="margin:18px 0 8px 0; font-size:18px; font-weight:800;">🏆 TOP tipy týdne (AI fit)</div>
    """.strip()

    text_lines = [f"Deskovkový briefing – {date_str}", "", intro, "", "TOP tipy týdne (AI fit):"]

    if top:
        for it, fit in top:
            html += render_game_html(it, fit, show_source=True)
            # text varianta
            text_lines.append(f"- {it['title']} ({fit.score}/100) – {fit.tagline}")
            text_lines.append(f"  {it['url']}")
            for who in ["Honza", "Káťa", "Monča", "Šimon"]:
                note = fit.notes.get(who, "")
                if note:
                    text_lines.append(f"  {who}: {note}")
            if fit.warnings:
                text_lines.append(f"  ! { ' | '.join(fit.warnings)}")
            text_lines.append("")
    else:
        html += "<div style='opacity:0.8; margin:10px 0 18px 0;'>(zatím nic, co by AI chtěla vytáhnout jako TOP)</div>"
        text_lines.append("(zatím nic)")

    # expansions
    html += "<div style='margin:22px 0 8px 0; font-size:18px; font-weight:800;'>🧩 Rozšíření pro hry, které už máš</div>"
    if expansions:
        for it in expansions:
            html += render_game_html(it, None, show_source=True)
            text_lines.append(f"Rozšíření: {it['title']} – {it['url']}")
    else:
        html += "<div style='opacity:0.8; margin:10px 0 18px 0;'>(zatím nic jasného)</div>"
        text_lines.append("Rozšíření: (zatím nic jasného)")
    text_lines.append("")

    # new games rest
    html += "<div style='margin:22px 0 8px 0; font-size:18px; font-weight:800;'>🇨🇿 Novinky v ČR (TLAMA + ostatní)</div>"
    if new_games_rest:
        for it in new_games_rest:
            html += render_game_html(it, None, show_source=True)
    else:
        html += "<div style='opacity:0.8; margin:10px 0 18px 0;'>(nic dalšího)</div>"

    # crowdfunding section (best effort, může být prázdné)
    html += "<div style='margin:22px 0 8px 0; font-size:18px; font-weight:800;'>🚀 Crowdfunding (Kickstarter)</div>"
    html += "<div style='opacity:0.8; margin:10px 0 18px 0;'>(zatím nic / nebo zdroj zlobí)</div>"

    # warnings
    if warnings:
        html += "<div style='margin:22px 0 8px 0; font-size:16px; font-weight:800;'>⚠ Poznámky ke zdrojům</div>"
        html += "<ul style='opacity:0.9;'>"
        for w in warnings[:10]:
            html += f"<li>{html_escape(w)}</li>"
        html += "</ul>"

        text_lines.append("Poznámky ke zdrojům:")
        for w in warnings[:10]:
            text_lines.append(f"- {w}")

    html += """
      </div>
    </body>
    </html>
    """

    text_body = "\n".join(text_lines).strip()

    send_email(subject=subject, text_body=text_body, html_body=html)


if __name__ == "__main__":
    main()
