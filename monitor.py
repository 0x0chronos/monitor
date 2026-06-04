#!/usr/bin/env python3
"""
BBRadar Monitor — GitHub Actions Edition
Roda a cada 15 minutos via cron do GitHub Actions.
Só notifica quando há programas realmente novos.

Auth flow (DevTools):
  GET  /api/frontend-token  → JWT ~5min
  POST /api/csrf-token      → X-Csrf-Token ~1h
  GET  /api/programs?scope_source=stored&sort=date_desc
"""

import json, time, base64, hashlib, sys
from pathlib import Path
from datetime import datetime, date, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════════════════════
#  CREDENCIAIS — repo DEVE ser PRIVADO
# ═══════════════════════════════════════════════════════════════
DISCORD_WEBHOOK  = "https://discord.com/api/webhooks/1486835658050764870/6khmM7CevN0CIrnI8Rfeei32n0hYXn8pkrzgXsr_23k7gKC-AT7YpDNyHbDFLJhUzRLu"
TELEGRAM_TOKEN   = "8655564407:AAFq9jaEoLdV6_b_Jdl14ZcRJ9swRRqxNng"
TELEGRAM_CHAT_ID = "1640178171"
CALLMEBOT_PHONE  = "5513996222130"          # ex: "5513999999999"
CALLMEBOT_KEY    = "2860591"

# ═══════════════════════════════════════════════════════════════
#  FILTROS (vazio = todos)
# ═══════════════════════════════════════════════════════════════
FILTER_PLATFORMS  = []
FILTER_MIN_BOUNTY = 0
FILTER_KEYWORDS   = []

# Programas lançados nos últimos N dias são sempre considerados "novos"
# mesmo que tenham sido vistos no baseline
NEW_DAYS_WINDOW = 7

# ═══════════════════════════════════════════════════════════════
STATE_FILE = Path("state.json")
BASE       = "https://bbradar.io"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         f"{BASE}/",
    "Content-Type":    "application/json",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-origin",
}

PLATFORM_COLORS = {
    "hackerone":        0x2C3E50, "bugcrowd":         0xE67E22,
    "intigriti":        0x9B59B6, "yeswehack":        0x27AE60,
    "immunefi":         0x3498DB, "code4rena":        0xE74C3C,
    "hackenproof":      0x1ABC9C, "cantina":          0xF39C12,
    "compass security": 0x2ECC71, "standoff365":      0xC0392B,
    "bugrap":           0x8E44AD, "issuehunt":        0x16A085,
    "certik":           0x2980B9, "default":          0x00B4D8,
}
PLAT_EMOJI = {
    "hackerone":"🟢","bugcrowd":"🟠","intigriti":"🟣","yeswehack":"🟡",
    "immunefi":"🔵","code4rena":"🔴","hackenproof":"🩵","cantina":"🟤",
    "compass security":"🟩","default":"⚪",
}


# ── HTTP ─────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ── JWT ──────────────────────────────────────────────────────────
def jwt_exp(token: str) -> float:
    try:
        p = token.split(".")[1]; p += "=" * (4 - len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception: return 0

def jwt_valid(token: str, margin: int = 60) -> bool:
    return bool(token) and time.time() < jwt_exp(token) - margin


# ── AUTH ─────────────────────────────────────────────────────────
def auth_flow(session: requests.Session) -> str:
    session.get(BASE, timeout=15)
    print("  [auth] session_id OK")

    r = session.get(f"{BASE}/api/frontend-token", timeout=10)
    r.raise_for_status()
    d = r.json()
    ft = d.get("token") or d.get("frontend_token") or d.get("data","")
    if not ft: raise RuntimeError(f"frontend_token vazio: {d}")
    print(f"  [auth] frontend_token OK (exp {datetime.fromtimestamp(jwt_exp(ft)).strftime('%H:%M')})")

    r = session.post(f"{BASE}/api/csrf-token", json={"frontend_token": ft}, timeout=10)
    r.raise_for_status()
    d = r.json()
    csrf = d.get("token") or d.get("csrf_token") or d.get("data","")
    if not csrf: raise RuntimeError(f"csrf_token vazio: {d}")
    print(f"  [auth] csrf_token OK (exp {datetime.fromtimestamp(jwt_exp(csrf)).strftime('%H:%M')})")
    return csrf


# ── FETCH ────────────────────────────────────────────────────────
def fetch_page(session, csrf, page=1):
    url = f"{BASE}/api/programs?scope_source=stored&sort=date_desc&page={page}"
    r   = session.get(url, headers={"X-Csrf-Token": csrf}, timeout=15)
    if r.status_code in (401,403):
        raise RuntimeError(f"Auth recusada: HTTP {r.status_code}")
    r.raise_for_status()
    d = r.json()
    return d.get("programs",[]), d.get("meta",{})

def fetch_pages(session, csrf, n=1):
    out = []
    for page in range(1, n+1):
        progs, meta = fetch_page(session, csrf, page)
        out.extend(progs)
        print(f"  [fetch] pág {page}/{meta.get('total_pages','?')}: {len(progs)} programas")
        if page >= meta.get("total_pages", page): break
        time.sleep(0.5)
    return out


# ── NORMALIZAÇÃO ─────────────────────────────────────────────────
def normalize(raw: dict) -> dict:
    handle  = str(raw.get("handle","")).strip()
    name    = str(raw.get("name","")).strip()
    plat    = str(raw.get("platform","")).strip()
    link    = str(raw.get("link","")).strip()
    date_l  = str(raw.get("date_launched","")).strip()
    scope_t = str(raw.get("scope_type","")).strip()
    tags    = raw.get("scope_tags") or []
    bmin    = int(raw.get("bounty_min",0) or 0)
    bmax    = int(raw.get("bounty_max",0) or 0)
    reward  = str(raw.get("reward_range","")).strip()
    up      = int(raw.get("up_votes",0)  or 0)
    down    = int(raw.get("down_votes",0)or 0)
    score   = int(raw.get("net_score",0) or 0)
    pic     = str(raw.get("profile_picture","")).strip()

    uid = hashlib.md5(f"{plat.lower()}::{handle}".encode()).hexdigest()[:12]

    date_display = date_l
    try: date_display = datetime.strptime(date_l, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception: pass

    if reward:              bounty_str = reward
    elif bmax:              bounty_str = f"até ${bmax:,}"
    else:                   bounty_str = "VDP"

    return {
        "id": uid, "handle": handle, "name": name, "platform": plat,
        "link": link, "date_launched": date_l, "date_display": date_display,
        "scope_type": scope_t, "scope_tags": tags if isinstance(tags,list) else [],
        "bounty_min": bmin, "bounty_max": bmax, "bounty_str": bounty_str,
        "up_votes": up, "down_votes": down, "net_score": score, "profile_pic": pic,
    }

def apply_filters(programs):
    out = []
    for p in programs:
        if FILTER_PLATFORMS and not any(f.lower() in p["platform"].lower() for f in FILTER_PLATFORMS): continue
        if FILTER_KEYWORDS:
            hay = (p["name"]+" "+" ".join(p["scope_tags"])).lower()
            if not any(k.lower() in hay for k in FILTER_KEYWORDS): continue
        if FILTER_MIN_BOUNTY and p["bounty_max"] < FILTER_MIN_BOUNTY: continue
        out.append(p)
    return out


# ── ESTADO ───────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"seen_ids":[], "last_check":None, "total":0, "runs":0,
            "last_new":0, "last_new_programs":[]}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


# ── DETECÇÃO ─────────────────────────────────────────────────────
def detect_new(raw_programs: list, state: dict, is_baseline: bool) -> list:
    """
    Lógica de detecção:
    - Normaliza e filtra todos os programas recebidos
    - Baseline: marca como "já vistos" apenas programas com mais de NEW_DAYS_WINDOW dias
      → programas recentes ficam de fora do seen_ids e serão detectados na próxima rodada
    - Rodadas normais: novo = id não está em seen_ids
    """
    norm     = [normalize(p) for p in raw_programs]
    filtered = apply_filters(norm)
    seen     = set(state.get("seen_ids", []))

    cutoff = (date.today() - timedelta(days=NEW_DAYS_WINDOW)).isoformat()

    if is_baseline:
        # Marca como vistos APENAS os programas antigos (> NEW_DAYS_WINDOW dias)
        old_ids = [p["id"] for p in norm if p["date_launched"] < cutoff]
        state["seen_ids"] = old_ids
        state["total"]    = len(old_ids)
        print(f"  [baseline] {len(norm)} buscados | {len(old_ids)} marcados vistos "
              f"| {len(norm)-len(old_ids)} recentes (últimos {NEW_DAYS_WINDOW}d) serão detectados na próxima rodada")
        return []

    # Rodada normal: novo = id não visto ainda
    new = [p for p in filtered if p["id"] not in seen]

    # Atualiza seen_ids com todos os programas desta rodada
    all_ids = list(seen | {p["id"] for p in norm})
    state["seen_ids"] = all_ids
    state["total"]    = len(all_ids)
    return new


# ── NOTIFICAÇÕES ─────────────────────────────────────────────────
def notify_discord(programs: list):
    if not DISCORD_WEBHOOK or not programs: return
    embeds = []
    for p in programs[:10]:
        color  = PLATFORM_COLORS.get(p["platform"].lower(), PLATFORM_COLORS["default"])
        fields = [
            {"name":"🏢 Plataforma", "value":p["platform"],    "inline":True},
            {"name":"📅 Lançado em", "value":p["date_display"], "inline":True},
            {"name":"💰 Bounty",     "value":p["bounty_str"],   "inline":True},
        ]
        tags_str = " · ".join(p["scope_tags"][:5]) or p["scope_type"] or "—"
        fields.append({"name":"🏷️ Scope","value":tags_str,"inline":False})
        if p["up_votes"] or p["down_votes"]:
            fields.append({"name":"📊 Votos",
                           "value":f"👍{p['up_votes']} 👎{p['down_votes']} (score {p['net_score']:+d})",
                           "inline":False})
        emb = {"title":p["name"],"url":p["link"] or BASE,"color":color,"fields":fields,
               "footer":{"text":f"bbradar-monitor • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}"}}
        if p["profile_pic"]: emb["thumbnail"] = {"url":p["profile_pic"]}
        embeds.append(emb)

    r = requests.post(DISCORD_WEBHOOK, json={
        "username":"BBRadar Monitor","avatar_url":"https://bbradar.io/favicon.ico",
        "content":f"🎯 **{len(programs)} novo(s) programa(s)** em bbradar.io!",
        "embeds":embeds}, timeout=10)
    r.raise_for_status()
    print(f"  [discord] ✅ {len(embeds)} embed(s)")


def notify_telegram(programs: list):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not programs: return
    def esc(t):
        for c in r"\_*[]()~`>#+-=|{}.!": t = t.replace(c,f"\\{c}")
        return t
    lines = [f"🎯 *{len(programs)} novo\\(s\\) programa\\(s\\)* em bbradar\\.io\\!\n"]
    for i,p in enumerate(programs[:10],1):
        em   = PLAT_EMOJI.get(p["platform"].lower(),"⚪")
        tags = esc(", ".join(p["scope_tags"][:4]) or p["scope_type"] or "?")
        lines.append(
            f"*{i}\\. [{esc(p['name'])}]({p['link'] or BASE})*\n"
            f"{em} `{esc(p['platform'])}`  📅 {esc(p['date_display'])}  💰 {esc(p['bounty_str'])}\n"
            f"🏷️ {tags}\n"
        )
    if len(programs)>10: lines.append(f"_\\+{len(programs)-10} mais_")
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id":TELEGRAM_CHAT_ID,"text":"\n".join(lines),
              "parse_mode":"MarkdownV2","disable_web_page_preview":True}, timeout=10)
    r.raise_for_status()
    print(f"  [telegram] ✅")


def notify_whatsapp(programs: list):
    if not CALLMEBOT_PHONE or not programs: return
    lines = [f"🎯 BBRadar: {len(programs)} novo(s) programa(s)!\n"]
    for i,p in enumerate(programs[:6],1):
        tags = ", ".join(p["scope_tags"][:3]) or p["scope_type"] or "?"
        lines.append(f"{i}. {p['name']}\n   [{p['platform']}] {p['date_display']}\n   💰 {p['bounty_str']}\n   {p['link']}")
    r = requests.get("https://api.callmebot.com/whatsapp.php",
        params={"phone":CALLMEBOT_PHONE,"text":"\n".join(lines),"apikey":CALLMEBOT_KEY}, timeout=15)
    print(f"  [whatsapp] HTTP {r.status_code}")


def notify_all(programs: list):
    if not programs: return
    print(f"\n🔔 NOTIFICANDO: {len(programs)} programa(s) novo(s)")
    for i,p in enumerate(programs,1):
        print(f"  #{i:02d} [{p['platform']}] {p['name']} | {p['date_display']} | {p['bounty_str']}")
    for fn in [notify_discord, notify_telegram, notify_whatsapp]:
        try: fn(programs)
        except Exception as e: print(f"  ⚠️ {fn.__name__}: {e}")


# ── MAIN ─────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*55}")
    print(f"  BBRadar Monitor — {now.strftime('%d/%m/%Y %H:%M UTC')}")
    print(f"{'='*55}")

    state      = load_state()
    is_baseline = len(state.get("seen_ids",[])) == 0

    if is_baseline:
        print(f"\n[MODO] Baseline — programas com >{NEW_DAYS_WINDOW}d serão marcados vistos")
        print(f"       Programas recentes serão notificados na próxima rodada ✅")
    else:
        print(f"\n[MODO] Detecção — {len(state['seen_ids'])} IDs conhecidos")

    session = make_session()

    print("\n[1/3] Autenticando...")
    try:
        csrf = auth_flow(session)
    except Exception as e:
        print(f"❌ Falha na autenticação: {e}")
        sys.exit(1)

    pages = 3 if is_baseline else 1
    print(f"\n[2/3] Buscando programas ({pages} página(s))...")
    try:
        raw_programs = fetch_pages(session, csrf, n=pages)
    except Exception as e:
        print(f"❌ Falha no fetch: {e}")
        sys.exit(1)

    if not raw_programs:
        print("❌ Nenhum dado retornado")
        sys.exit(1)

    print(f"\n[3/3] Processando {len(raw_programs)} programas...")
    new_progs = detect_new(raw_programs, state, is_baseline)

    # Atualiza metadados
    state["last_check"] = now.isoformat()
    state["runs"]       = state.get("runs",0) + 1
    state["last_new"]   = len(new_progs)
    state["last_new_programs"] = [
        {k:v for k,v in p.items() if k not in ("id","handle","profile_pic")}
        for p in new_progs[:20]
    ]
    save_state(state)

    print(f"\n  Total fetched : {len(raw_programs)}")
    print(f"  IDs conhecidos: {len(state['seen_ids'])}")
    print(f"  🆕 Novos       : {len(new_progs)}")

    if is_baseline:
        print(f"\n✅ Baseline concluído. Próxima rodada detectará programas novos.")
    elif new_progs:
        notify_all(new_progs)
    else:
        print("\n✅ Nenhum programa novo.")

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    main()
