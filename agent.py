# agent.py (v6) — CZ vydavatelé (TLAMA priorita) + Kickstarter + Kolekce + Skupina + AI TOP 3
# - čte sources.yaml
# - deduplikuje hry napříč zdroji (preferuje TLAMA link, když existuje)
# - AI TOP 3 napříč všemi CZ + crowdfunding (oddělené sekce v mailu)
# - TOP tipy se neukazují podruhé v dalších seznamech
#
# v6 ZMĚNY:
# - Vylepšená detekce rozšíření (fuzzy matching názvů)
# - Rozšíření se matchují i bez klíčového slova "rozšíření" pokud obsahují název vlastněné hry
# - V emailu se u rozšíření zobrazuje, ke které hře patří
# - Debug logging pro diagnostiku

import os
import re
import csv
import io
import html
import json
import unicodedata
import logging
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import yaml

from openai import OpenAI

# === LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# === SHEETS ===
def _str_env(key: str, default: str) -> str:
    """Bezpečně načte string z env, prázdný string = default."""
    val = os.environ.get(key, "").strip()
    return val if val else default

COLLECTION_CSV_URL = _str_env(
    "COLLECTION_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTmHPN69oIL7Fit5EN_K6HXtYtEPOZi2v-KmFL85D-wQsljrIT3cDY_Uh0LShOiIDfOx6rGJPlfESa2/pub?output=csv",
)
GROUP_CSV_URL = _str_env(
    "GROUP_CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQsZls09kQMlBDG8kMyzb-bjIpEV9ON8zbK6a1dYS9Imp9tUcgBzQmNrFH9dtq2ySIG_afmTewJx1-1/pub?output=csv",
)

SOURCES_YAML_PATH = _str_env("SOURCES_YAML_PATH", "sources.yaml")

# === AI SETTINGS ===
OPENAI_MODEL = _str_env("OPENAI_MODEL", "gpt-4o")

def _int_env(key: str, default: int) -> int:
    """Bezpečně načte int z env, prázdný string = default."""
    val = os.environ.get(key, "").strip()
    return int(val) if val else default

AI_SCORE_LIMIT_CZ = _int_env("AI_SCORE_LIMIT_CZ", 10)  # kolik CZ kandidátů skórovat AI
AI_SCORE_LIMIT_CF = _int_env("AI_SCORE_LIMIT_CF", 6)   # kolik crowdfunding kandidátů skórovat AI
AI_TOP_N_CZ = _int_env("AI_TOP_N_CZ", 3)              # kolik TOP CZ tipů zobrazit
AI_TOP_N_CF = _int_env("AI_TOP_N_CF", 2)              # kolik TOP crowdfunding tipů zobrazit

# how many items to collect from each source page (hard cap)
PER_SOURCE_ITEM_CAP = _int_env("PER_SOURCE_ITEM_CAP", 30)

# === expansion detection ===
EXPANSION_KEYWORDS = [
    "rozšíření", "rozsireni", "expanze", "expansion", "extension",
    "doplněk", "doplnok", "dodatek", "promo", "promo pack",
    "balíček", "balicek", "pack",
]

# titles we never want to show as games
TITLE_BLACKLIST_CONTAINS = [
    "registrace", "zapomenuté heslo", "přihlásit", "prihlasit", "košík", "kosik",
    "doprava", "platba", "obchodní podmínky", "podmínky", "ochrany osobních údajů",
    "gdpr", "cookies", "věrnostní", "affiliate", "program", "kontakt", "půjčovna",
    "provozní řád", "rozcesnik", "čestina", "english", "language", "tel:",
    "facebook", "instagram",
]

TITLE_BLACKLIST_EXACT = {"deskové hry", "deskove hry"}


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def title_is_ok(title: str) -> bool:
    t = norm(title)
    if len(t) < 4 or len(t) > 110:
        return False
    if t in TITLE_BLACKLIST_EXACT:
        return False
    if any(norm(b) in t for b in TITLE_BLACKLIST_CONTAINS):
        return False
    # ignore pure price / numbers
    if re.fullmatch(r"[\d\+\s\-\(\)\.,%]+", title.strip()):
        return False
    return True


def looks_like_expansion(title: str, blurb: str = "") -> bool:
    """
    Kontroluje, zda produkt vypadá jako rozšíření.
    Hledá klíčová slova v názvu NEBO v popisu (blurb).
    """
    t = norm(title)
    b = norm(blurb)
    combined = t + " " + b
    return any(norm(k) in combined for k in EXPANSION_KEYWORDS)


def fetch(url: str, *, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def safe_fetch(url: str, *, timeout: int = 30) -> tuple[str | None, str | None]:
    """Returns (html, error_string)"""
    try:
        return fetch(url, timeout=timeout), None
    except Exception as e:
        return None, str(e)


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


def extract_image_url(soup: BeautifulSoup, base: str) -> str:
    # meta first (most reliable)
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        return absolute_url(base, og_img["content"].strip())

    tw_img = soup.find("meta", attrs={"name": "twitter:image"})
    if tw_img and tw_img.get("content"):
        return absolute_url(base, tw_img["content"].strip())

    # fallback: first meaningful img
    img = soup.find("img")
    if img:
        for attr in ["src", "data-src", "data-original", "data-lazy", "data-image"]:
            v = img.get(attr)
            if v and isinstance(v, str) and v.strip():
                return absolute_url(base, v.strip())
    return ""


def extract_title_from_page(soup: BeautifulSoup) -> str:
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        return " ".join(og_title["content"].split()).strip()

    h1 = soup.find("h1")
    if h1:
        t = " ".join(h1.get_text(" ", strip=True).split()).strip()
        if t:
            return t

    if soup.title and soup.title.string:
        return " ".join(str(soup.title.string).split()).strip()

    return ""


def extract_blurb_from_page(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return " ".join(og["content"].split())[:700]

    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        return " ".join(desc["content"].split())[:700]

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:700]


def is_jsonld_product(soup: BeautifulSoup) -> bool:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.get_text(strip=True) or "{}")
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            if isinstance(t, list):
                t = " ".join(t)
            if isinstance(t, str) and "Product" in t:
                return True
    return False


@dataclass
class Item:
    title: str
    url: str
    image_url: str
    source_id: str
    source_label: str
    kind: str               # "cz" or "crowdfunding"
    priority: int
    blurb: str = ""         # short text from product page
    matched_base_game: str = ""  # pro rozšíření: název základní hry, ke které patří


def load_csv_rows(csv_url: str) -> list[list[str]]:
    r = requests.get(csv_url, timeout=30, headers={"User-Agent": "DeskovkyAgent/6.0"})
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

        k_norm = norm(kdo)
        if "_" in k_norm or k_norm in {"players", "avoid_dice_heavy", "session_length"}:
            meta[k_norm] = popis.strip() if popis else ""
        else:
            if popis:
                people[kdo.strip()] = popis.strip()

    return people, meta


def summarize_group_for_prompt(people: dict, meta: dict) -> str:
    parts = []
    if meta.get("players"):
        parts.append(f"- Typicky hráčů: {meta.get('players')}")
    if meta.get("avoid_dice_heavy"):
        parts.append(f"- Vyhýbáme se hrám s velkým důrazem na kostky/náhodu: {meta.get('avoid_dice_heavy')}")
    if meta.get("session_length"):
        parts.append(f"- Délka sezení (realita): {meta.get('session_length')}")
    meta_block = "\n".join(parts).strip()

    people_lines = [f"{name}: {profile}" for name, profile in people.items()]
    people_block = "\n".join(people_lines).strip()

    out = []
    if meta_block:
        out.append("META:\n" + meta_block)
    if people_block:
        out.append("PROFILY:\n" + people_block)
    return "\n\n".join(out).strip()


def ai_fit_score(client: OpenAI, group_text: str, game_title: str, game_blurb: str) -> dict:
    instructions = (
        "Jsi kurátor deskovek pro jednu konkrétní skupinu. "
        "Dostaneš profil skupiny a krátký popis hry. "
        "Ohodnoť, jak moc je hra fit pro skupinu (0–100). "
        "Buď konkrétní a stručný. "
        "Cíl: A) fit pro skupinu + 2–4 krátké poznámky cílené na konkrétní členy (Honza, Káťa, Monča, Šimon). "
        "Poznámky vybírej podle relevance (nemusí být pro všechny, ale ať to není pořád jen pro stejné dva). "
        "Pokud hra výrazně stojí na náhodě/kostkách, uveď varování."
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
        '    {"who": "Honza", "note": "krátká poznámka"},\n'
        '    {"who": "Káťa", "note": "krátká poznámka"}\n'
        "  ],\n"
        '  "warnings": ["varování 1"]\n'
        "}\n"
        "- fit musí být celé číslo 0–100.\n"
        "- why max 2 položky.\n"
        "- notes: dej 2 až 4 položky, who musí být JEN z: Honza, Káťa, Monča, Šimon.\n"
        "- warnings max 2 položky."
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

    # notes validation/sanitization
    allowed_people = {"Honza", "Káťa", "Monča", "Šimon"}
    notes = data.get("notes", [])
    out_notes = []
    if isinstance(notes, list):
        for n in notes:
            if not isinstance(n, dict):
                continue
            who = str(n.get("who", "")).strip()
            note = str(n.get("note", "")).strip()
            if who in allowed_people and note:
                out_notes.append({"who": who, "note": note})
    # cap + ensure unique who order
    seen_who = set()
    dedup_notes = []
    for n in out_notes:
        if n["who"] in seen_who:
            continue
        seen_who.add(n["who"])
        dedup_notes.append(n)
    notes = dedup_notes[:4]

    warnings = data.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    warnings = [str(x) for x in warnings][:2]

    return {"fit": fit, "why": why, "notes": notes, "warnings": warnings}


def load_sources_config(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("sources", [])


def url_allowed(url: str, src: dict) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False

    allow_domains = set(src.get("allow_domains", []))
    if allow_domains and p.netloc not in allow_domains:
        return False

    must_contain = src.get("product_url_must_contain", [])
    for part in must_contain:
        if part and part not in url:
            return False

    must_contain_any = src.get("product_url_must_contain_any", [])
    if must_contain_any:
        if not any(part in url for part in must_contain_any if part):
            return False

    must_not = src.get("product_url_must_not_contain", [])
    for part in must_not:
        if part and part in url:
            return False

    return True


def extract_candidate_urls(listing_html: str, base_url: str, src: dict) -> list[str]:
    soup = BeautifulSoup(listing_html, "html.parser")
    out = []
    seen = set()

    for a in soup.find_all("a", href=True):
        u = absolute_url(base_url, a.get("href", ""))
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        if url_allowed(u, src):
            out.append(u)

    return out[:PER_SOURCE_ITEM_CAP * 3]  # loose cap pre-filter


def build_item_from_product_page(url: str, src: dict) -> Item | None:
    html_text, err = safe_fetch(url, timeout=30)
    if not html_text:
        return None

    soup = BeautifulSoup(html_text, "html.parser")

    # Heuristic: product pages usually have og:title/og:image or Product JSON-LD
    title = extract_title_from_page(soup)
    if not title or not title_is_ok(title):
        return None

    # additional heuristics to reduce garbage
    # - if no og:image AND no Product jsonld and title looks like navigation => drop
    if not soup.find("meta", attrs={"property": "og:image"}) and not is_jsonld_product(soup):
        # keep Kickstarter anyway (project pages)
        if src.get("kind") != "crowdfunding":
            # still allow if URL clearly looks like product and title ok
            pass

    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    image_url = extract_image_url(soup, base)
    blurb = extract_blurb_from_page(soup)

    return Item(
        title=title.strip(),
        url=url,
        image_url=image_url.strip(),
        source_id=src["id"],
        source_label=src["label"],
        kind=src.get("kind", "cz"),
        priority=int(src.get("priority", 999)),
        blurb=blurb.strip(),
    )


def scrape_source(src: dict) -> tuple[list[Item], list[str]]:
    """Returns (items, warnings)"""
    warnings = []
    items: list[Item] = []

    for page_url in src.get("urls", []):
        listing_html, err = safe_fetch(page_url, timeout=30)
        if not listing_html:
            warnings.append(f"{src['label']}: nepodařilo se stáhnout ({err})")
            continue

        base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
        candidates = extract_candidate_urls(listing_html, base, src)

        # For Kickstarter discover pages, many links are duplicates; keep a smaller set
        candidates = candidates[:PER_SOURCE_ITEM_CAP]

        for u in candidates:
            if len(items) >= PER_SOURCE_ITEM_CAP:
                break
            it = build_item_from_product_page(u, src)
            if it:
                items.append(it)

    # dedupe inside source by title norm
    out, seen = [], set()
    for it in items:
        nt = norm(it.title)
        if nt in seen:
            continue
        seen.add(nt)
        out.append(it)

    return out[:PER_SOURCE_ITEM_CAP], warnings


def merge_dedupe_items(items: list[Item]) -> list[Item]:
    """
    Dedupe by normalized title.
    If multiple sources have same title, keep the one with lowest priority number
    (TLAMA should have lower numbers in config).
    """
    best: dict[str, Item] = {}
    for it in items:
        key = norm(it.title)
        cur = best.get(key)
        if not cur:
            best[key] = it
            continue
        if it.priority < cur.priority:
            best[key] = it
    return list(best.values())


# =============================================================================
# VYLEPŠENÁ LOGIKA PRO ROZŠÍŘENÍ (v6)
# =============================================================================

def match_expansion_to_owned(title: str, owned_titles: list[str], owned_norm: list[str]) -> tuple[str, str] | None:
    """
    Vrací (název_vlastněné_hry, typ_matche) nebo None.
    
    typ_matche: "prefix_colon" | "full" | "prefix3" | "prefix2" | "main_word"
    
    PRIORITA MATCHŮ (nižší = lepší):
    1. PREFIX_COLON - produkt ZAČÍNÁ názvem hry + dvojtečka/pomlčka (nejspolehlivější!)
       Např. "Na křídlech draků: Ohnivá akademie" → "Na křídlech draků"
    2. FULL match - celý název hry je někde v názvu produktu
    3. PREFIX3/PREFIX2 - první 2-3 slova (vyžaduje klíčové slovo)
    4. MAIN_WORD - hlavní slovo (vyžaduje klíčové slovo)
    
    Při stejné prioritě se preferuje delší název vlastněné hry.
    """
    t = norm(title)
    
    # Sbíráme všechny matche: (priorita, délka_názvu, orig_title, match_type_str)
    matches: list[tuple[int, int, str, str]] = []
    
    for orig_title, game_nt in zip(owned_titles, owned_norm):
        if len(game_nt) < 4:
            continue
        
        words = game_nt.split()
        
        # 1. PREFIX_COLON - produkt ZAČÍNÁ názvem hry + ":" nebo " -" nebo " –"
        #    Tohle je nejspolehlivější pattern pro rozšíření!
        #    "Na křídlech draků: Ohnivá akademie" začíná "na kridlech draku:"
        for separator in [":", " -", " –", " —"]:
            if t.startswith(game_nt + separator.lower()) or t.startswith(game_nt + separator):
                matches.append((0, len(game_nt), orig_title, "prefix_colon"))
                break
        else:
            # 2. FULL match - celý název hry je v názvu rozšíření (priorita 1)
            if game_nt in t:
                matches.append((1, len(game_nt), orig_title, "full"))
                continue
            
            # 3. PREFIX3 match - první 3 slova (priorita 2)
            if len(words) >= 3:
                prefix3 = " ".join(words[:3])
                if len(prefix3) >= 10 and prefix3 in t:
                    matches.append((2, len(prefix3), orig_title, "prefix3"))
                    continue
            
            # 4. PREFIX2 match - první 2 slova (priorita 3)
            if len(words) >= 2:
                prefix2 = " ".join(words[:2])
                if len(prefix2) >= 8 and prefix2 in t:
                    matches.append((3, len(prefix2), orig_title, "prefix2"))
                    continue
            
            # 5. MAIN_WORD match - nejdelší slovo (priorita 4)
            if len(words) >= 1:
                main_word = max(words, key=len)
                if len(main_word) >= 6 and main_word in t:
                    generic_words = {"hry", "hra", "game", "games", "edition", "edice", "deluxe", "board", "card", "dice", "the"}
                    if main_word not in generic_words:
                        matches.append((4, len(main_word), orig_title, "main_word"))
    
    if not matches:
        return None
    
    # Seřadíme: nejdřív podle priority (nižší = lepší), pak podle délky (delší = lepší)
    matches.sort(key=lambda x: (x[0], -x[1]))
    
    best = matches[0]
    priority, length, orig_title, match_type = best
    
    log.debug(f"  Match [{match_type}]: '{orig_title}' pro '{title}' (priorita {priority}, délka {length})")
    
    return (orig_title, match_type)


def categorize_item(
    item: Item, 
    owned_titles: list[str], 
    owned_norm: list[str]
) -> tuple[str, str | None]:
    """
    Vrací (kategorie, matched_game).
    
    kategorie: 
      - "expansion_for_owned" = rozšíření pro hru, kterou vlastníme
      - "expansion_other" = rozšíření, ale ne pro naši hru
      - "new_game" = nová hra (ne rozšíření)
    
    matched_game: název základní hry (jen pro expansion_for_owned)
    
    DŮLEŽITÉ: Aby se produkt označil jako rozšíření pro vlastněnou hru, musí:
    - FULL match: celý název hry je v názvu produktu → vždy OK
    - PARTIAL match: jen část názvu → vyžaduje klíčové slovo (rozšíření, expansion, ...)
    
    Klíčová slova se hledají v názvu I v popisu (blurb).
    """
    is_expansion_by_keyword = looks_like_expansion(item.title, item.blurb)
    match_result = match_expansion_to_owned(item.title, owned_titles, owned_norm)
    
    if match_result:
        matched_game, match_type = match_result
        matched_game_len = len(norm(matched_game))
        
        # SPOLEHLIVÉ MATCHE (nevyžadují klíčové slovo):
        # - prefix_colon: "Na křídlech draků: Ohnivá akademie" začíná "Na křídlech draků:"
        # - full: celý název hry je v produktu
        #
        # ALE: krátké názvy (< 8 znaků) jako "Duna", "Catan" atd. 
        # můžou mít podtituly i u samostatných her ("Duna: Tajemství rodu")
        # → u krátkých názvů VŽDY vyžadujeme klíčové slovo
        #
        # SLABÉ MATCHE (vždy vyžadují klíčové slovo):
        # - prefix2, prefix3, main_word
        
        is_short_name = matched_game_len < 10  # "Na křídlech" = 10, "Duna" = 4
        
        if match_type in ("prefix_colon", "full") and not is_short_name:
            # Dlouhý název + spolehlivý match = OK
            return ("expansion_for_owned", matched_game)
        elif is_expansion_by_keyword:
            # Krátký název nebo slabý match, ale máme klíčové slovo = OK
            return ("expansion_for_owned", matched_game)
        else:
            # Krátký název bez klíčového slova = ignorujeme (možná samostatná hra)
            if is_short_name:
                log.debug(f"   Krátký název bez klíčového slova: '{item.title}' ~> '{matched_game}'")
            else:
                log.debug(f"   Partial match bez klíčového slova: '{item.title}' ~> '{matched_game}'")
    
    if is_expansion_by_keyword:
        # Je to rozšíření (podle klíčového slova), ale ne pro naši hru
        return ("expansion_other", None)
    
    return ("new_game", None)


# =============================================================================
# BUILD EMAIL
# =============================================================================

def build_email(
    owned_titles: list[str], 
    all_items: list[Item], 
    people: dict, 
    meta: dict, 
    warnings: list[str]
) -> tuple[str, str]:
    
    owned_norm = [norm(t) for t in owned_titles]
    
    # Debug: logujeme vlastněné hry
    log.info(f"📚 Načteno {len(owned_titles)} vlastněných her z tabulky")
    if owned_titles:
        log.info(f"   Prvních 10 her: {owned_titles[:10]}")
    
    cz_items = [it for it in all_items if it.kind == "cz"]
    cf_items = [it for it in all_items if it.kind == "crowdfunding"]
    
    log.info(f"🔍 Analyzuji {len(cz_items)} CZ položek pro rozšíření...")
    
    # Debug: vypíšeme všechny CZ položky
    log.info(f"   Všechny CZ položky:")
    for it in cz_items[:15]:  # max 15 pro přehlednost
        log.info(f"     - {it.title}")
    
    expansions_for_owned: list[Item] = []
    new_games_cz: list[Item] = []
    skipped_other_expansions = 0
    
    for it in cz_items:
        category, matched_game = categorize_item(it, owned_titles, owned_norm)
        
        # Debug log pro každou položku
        if category == "expansion_for_owned":
            log.info(f"   ✅ '{it.title}' → rozšíření pro '{matched_game}'")
        elif category == "expansion_other":
            log.info(f"   ⏭️ '{it.title}' → rozšíření, ale ne pro tvou hru")
        else:
            log.debug(f"   ➡️ '{it.title}' → nová hra")
        
        if category == "expansion_for_owned":
            it.matched_base_game = matched_game or ""
            expansions_for_owned.append(it)
        elif category == "expansion_other":
            skipped_other_expansions += 1
        else:
            new_games_cz.append(it)
    
    log.info(f"📊 Výsledek: {len(expansions_for_owned)} rozšíření pro vlastněné hry, "
             f"{len(new_games_cz)} nových her, {skipped_other_expansions} rozšíření pro jiné hry")
    
    # separate crowdfunding: no expansion logic, just list
    crowdfunding = cf_items

    # sort CZ: TLAMA-ish first by priority then alpha
    new_games_cz.sort(key=lambda x: (x.priority, norm(x.title)))
    expansions_for_owned.sort(key=lambda x: (x.priority, norm(x.title)))
    crowdfunding.sort(key=lambda x: (x.priority, norm(x.title)))

    # caps
    expansions_for_owned = expansions_for_owned[:12]
    new_games_cz = new_games_cz[:20]
    crowdfunding = crowdfunding[:12]

    # === AI scoring - SEPARÁTNĚ pro CZ a crowdfunding ===
    top_cz_block = []
    top_cf_block = []
    
    if os.environ.get("OPENAI_API_KEY"):
        client = OpenAI()
        group_text = summarize_group_for_prompt(people, meta)

        # --- CZ TOP tipy ---
        log.info(f"🤖 AI hodnotí TOP {AI_TOP_N_CZ} z {min(len(new_games_cz), AI_SCORE_LIMIT_CZ)} CZ her...")
        cz_candidates = new_games_cz[:AI_SCORE_LIMIT_CZ]
        cz_scored = []
        for it in cz_candidates:
            blurb = it.blurb or it.title
            score = ai_fit_score(client, group_text, it.title, blurb)
            cz_scored.append((it, score))
            log.debug(f"   {it.title}: {score.get('fit', 0)}/100")

        cz_scored.sort(key=lambda x: x[1].get("fit", 0), reverse=True)
        for it, score in cz_scored[:AI_TOP_N_CZ]:
            top_cz_block.append({"item": it, "score": score})
        
        # --- Crowdfunding TOP tipy ---
        if crowdfunding:
            log.info(f"🤖 AI hodnotí TOP {AI_TOP_N_CF} z {min(len(crowdfunding), AI_SCORE_LIMIT_CF)} crowdfunding her...")
            cf_candidates = crowdfunding[:AI_SCORE_LIMIT_CF]
            cf_scored = []
            for it in cf_candidates:
                blurb = it.blurb or it.title
                score = ai_fit_score(client, group_text, it.title, blurb)
                cf_scored.append((it, score))
                log.debug(f"   {it.title}: {score.get('fit', 0)}/100")

            cf_scored.sort(key=lambda x: x[1].get("fit", 0), reverse=True)
            # Bereme jen ty s fitem >= 50 (má smysl doporučit)
            for it, score in cf_scored[:AI_TOP_N_CF]:
                if score.get("fit", 0) >= 40:  # minimální práh pro doporučení
                    top_cf_block.append({"item": it, "score": score})

    # remove TOP items from lists (no duplicates)
    top_cz_urls = {t["item"].url for t in top_cz_block} if top_cz_block else set()
    top_cf_urls = {t["item"].url for t in top_cf_block} if top_cf_block else set()
    new_games_cz_rest = [it for it in new_games_cz if it.url not in top_cz_urls]
    crowdfunding_rest = [it for it in crowdfunding if it.url not in top_cf_urls]

    # === Plain text ===
    lines = []
    lines.append(f"🎲 Deskovkový briefing – {date.today().isoformat()}")
    lines.append("")

    # TOP CZ tipy
    if top_cz_block:
        lines.append("🏆 TOP tipy týdne – CZ novinky (AI fit pro skupinu):")
        for t in top_cz_block:
            it = t["item"]; sc = t["score"]
            src = f"{it.source_label}"
            lines.append(f"- {sc['fit']}/100 — {it.title}  ({src})")
            for w in sc.get("why", []):
                lines.append(f"  • {w}")

            notes = sc.get("notes", [])
            if isinstance(notes, list) and notes:
                for n in notes:
                    who = n.get("who", "")
                    note = n.get("note", "")
                    if who and note:
                        lines.append(f"  • {who}: {note}")

            for warn in sc.get("warnings", []):
                lines.append(f"  ⚠️ {warn}")
            lines.append(f"  {it.url}")
        lines.append("")

    # TOP Crowdfunding tipy
    if top_cf_block:
        lines.append("🚀 TOP tipy z crowdfundingu (AI fit pro skupinu):")
        for t in top_cf_block:
            it = t["item"]; sc = t["score"]
            lines.append(f"- {sc['fit']}/100 — {it.title} ({it.source_label})")
            for w in sc.get("why", []):
                lines.append(f"  • {w}")

            notes = sc.get("notes", [])
            if isinstance(notes, list) and notes:
                for n in notes:
                    who = n.get("who", "")
                    note = n.get("note", "")
                    if who and note:
                        lines.append(f"  • {who}: {note}")

            for warn in sc.get("warnings", []):
                lines.append(f"  ⚠️ {warn}")
            lines.append(f"  {it.url}")
        lines.append("")

    lines.append("🧩 Rozšíření pro hry, které už máš:")
    if expansions_for_owned:
        for it in expansions_for_owned:
            base_info = f" → {it.matched_base_game}" if it.matched_base_game else ""
            lines.append(f"- {it.title}{base_info} — {it.url} ({it.source_label})")
    else:
        lines.append("- (žádná rozšíření pro tvé hry momentálně v nabídce)")
    lines.append("")

    lines.append("🇨🇿 Novinky v ČR (TLAMA + ostatní):")
    if new_games_cz_rest:
        for it in new_games_cz_rest:
            lines.append(f"- {it.title} — {it.url} ({it.source_label})")
    else:
        lines.append("- (zbytek tento týden pokryl TOP výběr 🙂)")
    lines.append("")

    lines.append("🚀 Další na crowdfundingu:")
    if crowdfunding_rest:
        for it in crowdfunding_rest:
            lines.append(f"- {it.title} — {it.url} ({it.source_label})")
    else:
        lines.append("- (zatím nic / nebo zdroje zrovna zlobí)")
    lines.append("")

    if warnings:
        lines.append("⚠️ Poznámky ke zdrojům:")
        for w in warnings[:6]:
            lines.append(f"- {w}")

    text_body = "\n".join(lines)

    # === HTML ===
    def card(it: Item, extra_html: str = "", show_base_game: bool = False):
        t = html.escape(it.title)
        u = html.escape(it.url)
        img = it.image_url or ""
        img_tag = f'<img src="{html.escape(img)}" alt="" style="width:64px;height:auto;border-radius:10px;display:block;">' if img else ""
        left = f'<div style="flex:0 0 64px;">{img_tag}</div>' if img_tag else ""
        # Zobrazujeme vždy source_label (TLAMA, Kickstarter, Gamefound, ...)
        badge = f'<div style="font-size:12px;color:#9aa0a6;margin-top:2px;">{html.escape(it.source_label)}</div>'
        
        # Pro rozšíření zobrazíme, ke které hře patří
        base_game_html = ""
        if show_base_game and it.matched_base_game:
            base_game_html = f'<div style="font-size:12px;color:#81c995;margin-top:2px;">→ pro hru: {html.escape(it.matched_base_game)}</div>'
        
        return f"""
        <div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid #2a2a2a;">
          {left}
          <div style="flex:1 1 auto;">
            <div style="font-size:15px;line-height:1.3;margin:0 0 4px 0;">
              <a href="{u}" style="color:#8ab4f8;text-decoration:none;">{t}</a>
            </div>
            {extra_html}
            {base_game_html}
            {badge}
            <div style="font-size:12px;color:#9aa0a6;word-break:break-all;">{u}</div>
          </div>
        </div>
        """

    # Helper pro generování TOP bloků
    def build_top_html_block(top_block: list, title: str) -> str:
        if not top_block:
            return ""
        blocks = []
        for t in top_block:
            it = t["item"]; sc = t["score"]
            why = sc.get("why", [])
            warns = sc.get("warnings", [])

            parts = []
            if why:
                parts.append(
                    f'<div style="margin:6px 0 0 0;color:#e8eaed;font-size:13px;">'
                    f'<b>{sc["fit"]}/100</b> — {html.escape(" • ".join(why))}</div>'
                )
            else:
                parts.append(f'<div style="margin:6px 0 0 0;color:#e8eaed;font-size:13px;"><b>{sc["fit"]}/100</b></div>')

            # notes (2–4 people)
            notes = sc.get("notes", [])
            if isinstance(notes, list) and notes:
                nice = []
                for n in notes:
                    who = (n.get("who") or "").strip()
                    note = (n.get("note") or "").strip()
                    if who and note:
                        nice.append(f'{html.escape(who)}: {html.escape(note)}')
                if nice:
                    parts.append(f'<div style="margin:4px 0 0 0;color:#bdc1c6;font-size:12px;">{" | ".join(nice)}</div>')

            if warns:
                parts.append(f'<div style="margin:4px 0 0 0;color:#f28b82;font-size:12px;">⚠️ {html.escape(" • ".join(warns))}</div>')

            blocks.append(card(it, extra_html="\n".join(parts)))

        return f'<h2 style="font-size:16px;margin:18px 0 8px 0;">{title}</h2>' + "".join(blocks)

    # TOP CZ
    top_cz_html = build_top_html_block(top_cz_block, "🏆 TOP tipy týdne – CZ novinky (AI fit)")
    
    # TOP Crowdfunding  
    top_cf_html = build_top_html_block(top_cf_block, "🚀 TOP tipy z crowdfundingu (AI fit)")

    # Rozšíření - s informací o základní hře
    exp_html = "".join(card(it, show_base_game=True) for it in expansions_for_owned) or '<div style="color:#9aa0a6;">(žádná rozšíření pro tvé hry momentálně v nabídce)</div>'
    cz_html = "".join(card(it) for it in new_games_cz_rest) or '<div style="color:#9aa0a6;">(zbytek tento týden pokryl TOP výběr 🙂)</div>'
    cf_html = "".join(card(it) for it in crowdfunding_rest) or '<div style="color:#9aa0a6;">(zatím nic / nebo zdroj zrovna zlobí)</div>'

    warn_html = ""
    if warnings:
        warn_lines = "".join(f"<li>{html.escape(w)}</li>" for w in warnings[:6])
        warn_html = f"""
        <h2 style="font-size:16px;margin:18px 0 8px 0;">⚠️ Poznámky ke zdrojům</h2>
        <ul style="color:#bdc1c6;margin:0 0 6px 18px;padding:0;">{warn_lines}</ul>
        """

    html_body = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#e8eaed;background:#121212;padding:18px;">
      <h1 style="font-size:20px;margin:0 0 8px 0;">🎲 Deskovkový briefing – {date.today().isoformat()}</h1>
      <div style="color:#bdc1c6;margin:0 0 16px 0;">
        TLAMA držíme jako hlavní zdroj. Když je hra i jinde, bereme TLAMA link. Když TLAMA nemá, bereme ostatní. 🙂
      </div>

      {top_cz_html}

      {top_cf_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🧩 Rozšíření pro hry, které už máš</h2>
      {exp_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🇨🇿 Novinky v ČR (TLAMA + ostatní)</h2>
      {cz_html}

      <h2 style="font-size:16px;margin:18px 0 8px 0;">🚀 Další na crowdfundingu</h2>
      {cf_html}

      {warn_html}

      <div style="margin-top:16px;color:#9aa0a6;font-size:12px;">
        Pozn.: některé weby (hlavně crowdfunding) občas mění strukturu / blokují scrapování. Když zlobí, uvidíš to v poznámkách.
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
    smtp_port = _int_env("SMTP_PORT", 587)
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

    log.info("🚀 Spouštím Deskovkový briefing v6...")
    
    owned = load_collection_titles(COLLECTION_CSV_URL)
    log.info(f"📚 Načteno {len(owned)} vlastněných her")
    
    people, meta = load_group_profile(GROUP_CSV_URL)
    log.info(f"👥 Načteno {len(people)} profilů hráčů")

    sources = load_sources_config(SOURCES_YAML_PATH)
    log.info(f"🌐 Načteno {len(sources)} zdrojů")

    all_items = []
    warnings = []

    for src in sources:
        log.info(f"   Scrapuji: {src['label']}...")
        items, warns = scrape_source(src)
        log.info(f"   → {len(items)} položek")
        all_items.extend(items)
        warnings.extend(warns)

    # global dedupe + TLAMA preference by priority
    all_items = merge_dedupe_items(all_items)
    log.info(f"📦 Celkem {len(all_items)} unikátních položek po deduplikaci")

    text_body, html_body = build_email(owned, all_items, people, meta, warnings)

    subject = f"Deskovkový briefing – {date.today().isoformat()}"
    if os.environ.get("OPENAI_API_KEY"):
        subject = f"Deskovkový briefing – AI tipy ({date.today().isoformat()})"

    send_email(subject, text_body, html_body)
    log.info("✅ Email odeslán!")


if __name__ == "__main__":
    main()
