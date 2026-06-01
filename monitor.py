#!/usr/bin/env python3
"""
BBRadar Monitor — GitHub Actions Edition
Roda a cada 15 minutos via cron do GitHub Actions.
Só notifica quando há programas realmente novos.
Estado persistido em state.json (commitado automaticamente).
"""

import json, time, base64, hashlib, sys
from pathlib import Path
from datetime import datetime, date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════════════════════
#  CREDENCIAIS — repo DEVE ser privado
# ═══════════════════════════════════════════════════════════════
DISCORD_WEBHOOK  = "https://discord.com/api/webhooks/1486835658050764870/6khmM7CevN0CIrnI8Rfeei32n0hYXn8pkrzgXsr_23k7gKC-AT7YpDNyHbDFLJhUzRLu"
TELEGRAM_TOKEN   = "8655564407:AAFq9jaEoLdV6_b_Jdl14ZcRJ9swRRqxNng"
TELEGRAM_CHAT_ID = "1640178171"
CALLMEBOT_PHONE  = ""          # ex: "5513999999999" — deixe "" para desativar
CALLMEBOT_KEY    = "2860591"

# ═══════════════════════════════════════════════════════════════
#  FILTROS (lista vazia = todos)
# ═══════════════════════════════════════════════════════════════
FILTER_PLATFORMS  = []     # ex: ["HackerOne", "Intigriti", "Bugcrowd"]
FILTER_MIN_BOUNTY = 0      # ignora programas com max_bounty abaixo deste valor ($)
FILTER_KEYWORDS   = []     # busca em name + scope_tags

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
    "hackerone":        0x2C3E50,
    "bugcrowd":         0xE67E22,
    "intigriti":        0x9B59B6,
    "yeswehack":        0x27AE60,
    "immunefi":         0x3498DB,
    "code4rena":        0xE74C3C,
    "hackenproof":      0x1ABC9C,
    "cantina":          0xF39C12,
    "compass security": 0x2ECC71,
    "standoff365":      0xC0392B,
    "bugrap":           0x8E44AD,
    "issuehunt":        0x16A085,
    "certik":           0x2980B9,
    "default":          0x00B4D8,
}

PLAT_EMOJI = {
    "hackerone":        "🟢",
    "bugcrowd":         "🟠",
    "intigriti":        "🟣",
    "yeswehack":        "🟡",
    "immunefi":         "🔵",
    "code4rena":        "🔴",
    "hackenproof":      "🩵",
    "cantina":          "🟤",
    "compass security": "🟩",
    "default":          "⚪",
}

SCOPE_EMOJI = {
    "domain":        "🌐",
    "mobile":        "📱",
    "source code":   "💻",
    "smart contract":"⛓️",
    "wildcard":      "🔮",
    "api":           "🔌",
    "blockchain-dlt":"⛓️",
    "rust":          "🦀",
    "solidity":      "📜",
    "other":         "📦",
}


# ───────────────────────────────────────────────────────────────
#  HTTP SESSION
# ───────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ───────────────────────────────────────────────────────────────
#  JWT UTILS
# ───────────────────────────────────────────────────────────────
def jwt_exp(token: str) -> float:
    try:
        p = token.split(".")[1]
        p += "=" * (4 - len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0

def jwt_valid(token: str, margin: int = 60) -> bool:
    return bool(token) and time.time() < jwt_exp(token) - margin


# ───────────────────────────────────────────────────────────────
#  AUTH FLOW  (homepage → frontend-token → csrf-token)
# ───────────────────────────────────────────────────────────────
def auth_flow(session: requests.Session) -> str:
    """
    Executa o fluxo de autenticação do bbradar.io e retorna o X-Csrf-Token.
    Fluxo descoberto via DevTools:
      GET  /api/frontend-token  → JWT ~5min
      POST /api/csrf-token      → X-Csrf-Token ~1h
    """
    # 1. Homepage para pegar cookie session_id
    session.get(BASE, timeout=15)
    print("  [auth] session_id obtido")

    # 2. Frontend token
    r = session.get(f"{BASE}/api/frontend-token", timeout=10)
    r.raise_for_status()
    d = r.json()
    ft = d.get("token") or d.get("frontend_token") or d.get("data","")
    if not ft:
        raise RuntimeError(f"frontend_token vazio: {d}")
    print(f"  [auth] frontend_token OK (exp: {datetime.fromtimestamp(jwt_exp(ft)).strftime('%H:%M:%S')})")

    # 3. CSRF token
    r = session.post(f"{BASE}/api/csrf-token",
                     json={"frontend_token": ft}, timeout=10)
    r.raise_for_status()
    d = r.json()
    csrf = d.get("token") or d.get("csrf_token") or d.get("data","")
    if not csrf:
        raise RuntimeError(f"csrf_token vazio: {d}")
    print(f"  [auth] csrf_token OK (exp: {datetime.fromtimestamp(jwt_exp(csrf)).strftime('%H:%M:%S')})")
    return csrf


# ───────────────────────────────────────────────────────────────
#  FETCH PROGRAMAS
# ───────────────────────────────────────────────────────────────
def fetch_page(session: requests.Session, csrf: str, page: int = 1) -> tuple:
    url = f"{BASE}/api/programs?scope_source=stored&sort=date_desc&page={page}"
    r   = session.get(url, headers={"X-Csrf-Token": csrf}, timeout=15)
    if r.status_code in (401, 403):
        raise RuntimeError(f"Auth rejeitada: HTTP {r.status_code}")
    r.raise_for_status()
    data = r.json()
    return data.get("programs",[]), data.get("meta",{})


def fetch_all_pages(session: requests.Session, csrf: str, max_pages: int = 3) -> list:
    """Busca as primeiras N páginas (baseline ou detecção de novos)."""
    all_progs = []
    for page in range(1, max_pages + 1):
        progs, meta = fetch_page(session, csrf, page)
        all_progs.extend(progs)
        print(f"  [fetch] página {page}/{meta.get('total_pages','?')}: {len(progs)} programas")
        if page >= meta.get("total_pages", page):
            break
        time.sleep(0.4)
    return all_progs


# ───────────────────────────────────────────────────────────────
#  NORMALIZAÇÃO
# ───────────────────────────────────────────────────────────────
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
    try:
        date_display = datetime.strptime(date_l, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        pass

    if reward:
        bounty_str = reward
    elif bmax:
        bounty_str = f"até ${bmax:,}"
    else:
        bounty_str = "VDP / sem bounty"

    return {
        "id":           uid,
        "handle":       handle,
        "name":         name,
        "platform":     plat,
        "link":         link,
        "date_launched":date_l,
        "date_display": date_display,
        "scope_type":   scope_t,
        "scope_tags":   tags if isinstance(tags, list) else [],
        "bounty_min":   bmin,
        "bounty_max":   bmax,
        "bounty_str":   bounty_str,
        "up_votes":     up,
        "down_votes":   down,
        "net_score":    score,
        "profile_pic":  pic,
    }


def apply_filters(programs: list) -> list:
    out = []
    for p in programs:
        if FILTER_PLATFORMS:
            if not any(f.lower() in p["platform"].lower() for f in FILTER_PLATFORMS):
                continue
        if FILTER_KEYWORDS:
            hay = (p["name"] + " " + " ".join(p["scope_tags"])).lower()
            if not any(k.lower() in hay for k in FILTER_KEYWORDS):
                continue
        if FILTER_MIN_BOUNTY and p["bounty_max"] < FILTER_MIN_BOUNTY:
            continue
        out.append(p)
    return out


# ───────────────────────────────────────────────────────────────
#  ESTADO
# ───────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": [], "last_check": None, "total": 0, "runs": 0}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


# ───────────────────────────────────────────────────────────────
#  NOTIFICAÇÕES
# ───────────────────────────────────────────────────────────────
def notify_discord(programs: list):
    if not DISCORD_WEBHOOK or not programs:
        return
    embeds = []
    for p in programs[:10]:
        color = PLATFORM_COLORS.get(p["platform"].lower(), PLATFORM_COLORS["default"])
        scope_parts = []
        for tag in p["scope_tags"][:6]:
            em = SCOPE_EMOJI.get(tag.lower(), "🏷️")
            scope_parts.append(f"{em} {tag}")
        scope_str = "  ".join(scope_parts) or p["scope_type"] or "?"

        fields = [
            {"name":"🏢 Plataforma", "value": p["platform"],     "inline": True},
            {"name":"📅 Lançado em", "value": p["date_display"],  "inline": True},
            {"name":"💰 Bounty",     "value": p["bounty_str"],    "inline": True},
            {"name":"🏷️ Scope",      "value": scope_str,          "inline": False},
        ]
        if p["up_votes"] or p["down_votes"]:
            fields.append({
                "name":  "📊 Votos",
                "value": f"👍 {p['up_votes']}  👎 {p['down_votes']}  (score: {p['net_score']:+d})",
                "inline": False,
            })

        emb = {
            "title":     p["name"],
            "url":       p["link"] or BASE,
            "color":     color,
            "fields":    fields,
            "footer":    {"text": f"bbradar-monitor • {datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}"},
        }
        if p["profile_pic"]:
            emb["thumbnail"] = {"url": p["profile_pic"]}
        embeds.append(emb)

    payload = {
        "username":   "BBRadar Monitor",
        "avatar_url": "https://bbradar.io/favicon.ico",
        "content":    f"🎯 **{len(programs)} novo(s) programa(s)** detectado(s) em bbradar.io!",
        "embeds":     embeds,
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print(f"  [discord] ✅ {len(embeds)} embed(s) enviado(s)")


def notify_telegram(programs: list):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not programs:
        return

    def esc(t: str) -> str:
        for c in r"\_*[]()~`>#+-=|{}.!":
            t = t.replace(c, f"\\{c}")
        return t

    lines = [f"🎯 *{len(programs)} novo\\(s\\) programa\\(s\\)* em bbradar\\.io\\!\n"]
    for i, p in enumerate(programs[:10], 1):
        em   = PLAT_EMOJI.get(p["platform"].lower(), "⚪")
        tags = esc(", ".join(p["scope_tags"][:4]) or p["scope_type"] or "?")
        lines.append(
            f"*{i}\\. [{esc(p['name'])}]({p['link'] or BASE})*\n"
            f"{em} `{esc(p['platform'])}`  📅 {esc(p['date_display'])}  💰 {esc(p['bounty_str'])}\n"
            f"🏷️ {tags}\n"
        )
    if len(programs) > 10:
        lines.append(f"_\\+{len(programs)-10} mais_")

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines),
              "parse_mode": "MarkdownV2", "disable_web_page_preview": True},
        timeout=10)
    r.raise_for_status()
    print(f"  [telegram] ✅ {len(programs)} programa(s) enviado(s)")


def notify_whatsapp(programs: list):
    if not CALLMEBOT_PHONE or not programs:
        return
    lines = [f"🎯 BBRadar: {len(programs)} novo(s) programa(s)!\n"]
    for i, p in enumerate(programs[:6], 1):
        tags = ", ".join(p["scope_tags"][:3]) or p["scope_type"] or "?"
        lines.append(
            f"{i}. {p['name']}\n"
            f"   [{p['platform']}] {p['date_display']}\n"
            f"   💰 {p['bounty_str']}\n"
            f"   🏷️ {tags}\n"
            f"   {p['link']}"
        )
    if len(programs) > 6:
        lines.append(f"\n+{len(programs)-6} mais em bbradar.io")

    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": CALLMEBOT_PHONE, "text": "\n".join(lines), "apikey": CALLMEBOT_KEY},
        timeout=15)
    print(f"  [whatsapp] HTTP {r.status_code}")


def notify_all(programs: list):
    if not programs:
        return
    print(f"\n🔔 NOTIFICANDO: {len(programs)} programa(s) novo(s)")
    for i, p in enumerate(programs, 1):
        tags = ", ".join(p["scope_tags"][:4]) or p["scope_type"]
        print(f"  #{i:02d} [{p['platform']}] {p['name']}")
        print(f"       📅 {p['date_display']}  💰 {p['bounty_str']}")
        print(f"       🏷️  {tags}")
        print(f"       🔗 {p['link']}")

    errors = []
    for fn in [notify_discord, notify_telegram, notify_whatsapp]:
        try:
            fn(programs)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    if errors:
        print("⚠️  Erros nas notificações:", " | ".join(errors))


# ───────────────────────────────────────────────────────────────
#  STATUS PAGE (index.html — servido pelo GitHub Pages)
# ───────────────────────────────────────────────────────────────
def update_status_page(state: dict, new_programs: list, total_fetched: int):
    now_utc = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    runs    = state.get("runs", 0)
    total   = state.get("total", 0)

    new_rows = ""
    if new_programs:
        for p in new_programs[:20]:
            tags = ", ".join(p["scope_tags"][:4]) or p["scope_type"] or "—"
            new_rows += f"""
            <tr class="new-row">
              <td><a href="{p['link']}" target="_blank">{p['name']}</a></td>
              <td>{p['platform']}</td>
              <td>{p['date_display']}</td>
              <td>{p['bounty_str']}</td>
              <td>{tags}</td>
            </tr>"""

    new_section = f"""
    <div class="card alert">
      <h2>🎯 {len(new_programs)} Novo(s) Programa(s) Detectado(s)</h2>
      <table>
        <thead><tr>
          <th>Nome</th><th>Plataforma</th><th>Data</th><th>Bounty</th><th>Scope</th>
        </tr></thead>
        <tbody>{new_rows}</tbody>
      </table>
    </div>""" if new_programs else ""

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="900">
  <title>BBRadar Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 4px; }}
    h1 span {{ color: #58a6ff; }}
    .sub {{ color: #8b949e; font-size: 0.9rem; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 16px; margin-bottom: 24px; }}
    .stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; text-align: center; }}
    .stat .num {{ font-size: 2rem; font-weight: 700; color: #58a6ff; }}
    .stat .label {{ font-size: 0.8rem; color: #8b949e; margin-top: 4px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
    .card h2 {{ margin-bottom: 16px; font-size: 1.1rem; }}
    .alert {{ border-color: #f78166; }}
    .alert h2 {{ color: #f78166; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    th {{ color: #8b949e; text-align: left; padding: 8px 10px; border-bottom: 1px solid #30363d; font-weight: 600; }}
    td {{ padding: 8px 10px; border-bottom: 1px solid #21262d; vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    .new-row td:first-child {{ font-weight: 600; }}
    a {{ color: #58a6ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem;
              background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb; }}
    .ok {{ color: #3fb950; }}
    .footer {{ text-align: center; color: #8b949e; font-size: 0.8rem; margin-top: 32px; }}
  </style>
</head>
<body>
  <h1>🎯 BBRadar <span>Monitor</span></h1>
  <p class="sub">Atualiza automaticamente a cada 15 minutos via GitHub Actions</p>

  <div class="grid">
    <div class="stat">
      <div class="num">{total}</div>
      <div class="label">Programas Catalogados</div>
    </div>
    <div class="stat">
      <div class="num ok">{len(new_programs)}</div>
      <div class="label">Novos nesta Rodada</div>
    </div>
    <div class="stat">
      <div class="num">{runs}</div>
      <div class="label">Checagens Realizadas</div>
    </div>
    <div class="stat">
      <div class="num" style="font-size:1.1rem">{now_utc}</div>
      <div class="label">Última Checagem</div>
    </div>
  </div>

  {new_section}

  <div class="card">
    <h2>📡 Status das Notificações</h2>
    <table>
      <thead><tr><th>Canal</th><th>Status</th></tr></thead>
      <tbody>
        <tr><td>Discord</td><td class="ok">✅ Configurado</td></tr>
        <tr><td>Telegram</td><td class="ok">✅ Configurado</td></tr>
        <tr><td>WhatsApp</td><td>{"✅ Configurado" if CALLMEBOT_PHONE else "⚠️ CALLMEBOT_PHONE não definido"}</td></tr>
      </tbody>
    </table>
  </div>

  <div class="footer">
    BBRadar Monitor • Cronos 0x0 • Powered by GitHub Actions
  </div>
</body>
</html>"""
    Path("index.html").write_text(html, encoding="utf-8")
    print(f"  [status] index.html atualizado")


# ───────────────────────────────────────────────────────────────
#  MAIN
# ───────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*55}")
    print(f"  BBRadar Monitor — {datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}")
    print(f"{'='*55}")

    state = load_state()
    is_baseline = len(state.get("seen_ids",[])) == 0

    if is_baseline:
        print("\n[INFO] Primeira execução — carregando baseline (sem notificações)...")

    session = make_session()

    # Auth
    print("\n[1/3] Autenticando...")
    csrf = auth_flow(session)

    # Fetch
    pages = 4 if is_baseline else 1
    print(f"\n[2/3] Buscando programas ({pages} página(s))...")
    raw_programs = fetch_all_pages(session, csrf, max_pages=pages)

    if not raw_programs:
        print("❌ Nenhum dado obtido — verifique a conexão")
        # Atualiza estado mesmo sem dados
        state["runs"] = state.get("runs",0) + 1
        state["last_check"] = datetime.utcnow().isoformat()
        save_state(state)
        update_status_page(state, [], 0)
        sys.exit(1)

    # Detectar novos
    print(f"\n[3/3] Comparando com estado...")
    seen      = set(state.get("seen_ids",[]))
    norm      = [normalize(p) for p in raw_programs]
    filtered  = apply_filters(norm)
    new_progs = [] if is_baseline else [p for p in filtered if p["id"] not in seen]

    # Atualiza estado
    all_ids = list(seen | {p["id"] for p in norm})
    state["seen_ids"]   = all_ids
    state["total"]      = len(all_ids)
    state["last_check"] = datetime.utcnow().isoformat()
    state["runs"]       = state.get("runs",0) + 1
    save_state(state)

    print(f"  Total fetched  : {len(raw_programs)}")
    print(f"  Após filtros   : {len(filtered)}")
    print(f"  IDs conhecidos : {len(seen)}")
    print(f"  🆕 Novos        : {len(new_progs)}")

    if is_baseline:
        print(f"\n✅ Baseline: {len(all_ids)} programas catalogados.")
        print("   Próximas execuções vão detectar e notificar novos programas.")
    elif new_progs:
        notify_all(new_progs)
    else:
        print("\n✅ Nenhum programa novo nesta checagem.")

    update_status_page(state, new_progs, len(raw_programs))
    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    main()
