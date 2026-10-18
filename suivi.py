#!/usr/bin/env python3
"""Agent de suivi hebdomadaire de la watchlist PEA.

Étapes : cours -> valeur intrinsèque / cible (méthode de la watchlist) -> signaux
-> synthèse rédigée par Claude -> envoi par mail.

Outil personnel : pas un conseil en investissement, aucun ordre n'est passé.
Variables d'environnement : voir README.md.
"""
import json
import os
import smtplib
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "watchlist.json"
STATE = ROOT / "state" / "derniers_cours.json"


# ---------------------------------------------------------------- cours
def fetch_prices(lignes):
    """Retourne {ticker: cours dans la devise de la watchlist} (les échecs sont omis)."""
    import yfinance as yf

    fx_cache = {}

    def fx(de, vers):
        if de == vers:
            return 1.0
        if (de, vers) not in fx_cache:
            h = yf.Ticker(f"{de}{vers}=X").history(period="5d")
            fx_cache[(de, vers)] = float(h["Close"].dropna().iloc[-1])
        return fx_cache[(de, vers)]

    prices, stats, errors = {}, {}, []
    for l in lignes:
        if not l["actif"] or not l["yahoo"]:
            continue
        try:
            close = yf.Ticker(l["yahoo"]).history(period="1y")["Close"].dropna()
            px = float(close.iloc[-1])
            prices[l["tk"]] = round(px * fx(l["devise_cote"], l["devise_watchlist"]), 4)
            # ratios calculés dans la devise de cotation (l'effet de change est ignoré)
            ref = lambda n: float(close.iloc[-1 - n]) if len(close) > n else None
            pct = lambda a, b: (a - b) / b * 100 if b else None
            stats[l["tk"]] = dict(
                var_hebdo_pct=pct(px, ref(5)),
                perf_1m_pct=pct(px, ref(21)),
                vs_haut_52s_pct=pct(px, float(close.max())),
            )
        except Exception as e:  # une valeur en échec ne bloque pas les autres
            errors.append(f"{l['tk']} ({type(e).__name__})")
    return prices, stats, errors


# ------------------------------------------------------------ valorisation
def evaluer(l, cours, cfg):
    bpa, jper = l["bpa"], l["jper"]
    vi = bpa * jper if bpa > 0 else None
    marge = cfg["marge_par_bucket"].get(l["bucket"], 30)
    cible = vi * (1 - marge / 100) if vi else None
    per = cours / bpa if bpa > 0 else None
    ecart = (cours - cible) / cours * 100 if cible else None  # % de baisse pour atteindre la cible
    return dict(vi=vi, cible=cible, marge=marge, per=per, ecart_cible_pct=ecart)


def analyser(cfg, prices, stats):
    lignes_out, signaux = [], []
    for l in cfg["lignes"]:
        tk = l["tk"]
        if not l["actif"]:
            continue
        cours = prices.get(tk)
        if cours is None:
            continue
        ev = evaluer(l, cours, cfg)
        st = stats.get(tk, {})
        var = st.get("var_hebdo_pct")
        d = dict(tk=tk, nom=l["nom"], bucket=l["bucket"], cours=cours,
                 var_hebdo_pct=var, perf_1m_pct=st.get("perf_1m_pct"),
                 vs_haut_52s_pct=st.get("vs_haut_52s_pct"), **ev)
        lignes_out.append(d)

        if ev["cible"] and cours <= ev["cible"]:
            signaux.append(f"{l['nom']} ({tk}) : cours {cours:g} sous ta cible {ev['cible']:.2f}")
        elif ev["ecart_cible_pct"] is not None and ev["ecart_cible_pct"] <= cfg["seuil_proche_cible_pct"]:
            signaux.append(f"{l['nom']} ({tk}) : à {ev['ecart_cible_pct']:.1f} % de ta cible ({ev['cible']:.2f})")
        if var is not None and abs(var) >= cfg["seuil_variation_hebdo_pct"]:
            signaux.append(f"{l['nom']} ({tk}) : {var:+.1f} % sur la semaine")

    positions = []
    for tk, pos in cfg.get("positions", {}).items():
        d = next((x for x in lignes_out if x["tk"] == tk), None)
        if d:
            perf = (d["cours"] - pos["pru"]) / pos["pru"] * 100
            valeur = d["cours"] * pos["quantite"]
            positions.append(dict(tk=tk, nom=d["nom"], quantite=pos["quantite"], pru=pos["pru"],
                                  cours=d["cours"], perf_pct=perf, valeur=valeur))
    return lignes_out, signaux, positions


# -------------------------------------------------------------- rédaction
def texte_brut(date_str, lignes, signaux, positions, errors):
    out = [f"Suivi PEA — {date_str}", ""]
    out.append("Signaux de la semaine :")
    out += [f"- {s}" for s in signaux] or ["- Rien à signaler."]
    a = sorted((d for d in lignes if d["bucket"] == "A"),
               key=lambda x: (x["ecart_cible_pct"] is None, x["ecart_cible_pct"] or 0))
    if a:
        f = lambda v: f"{v:+.1f} %" if v is not None else "—"
        out += ["", "Bucket A — tes priorités (de la plus proche à la plus éloignée de la cible) :",
                "(cours / cible / baisse nécessaire pour l'atteindre | semaine, 1 mois, vs plus haut 52 sem.)"]
        for d in a:
            out.append(f"- {d['nom']} : {d['cours']:g} / {d['cible']:.2f} / {f(d['ecart_cible_pct'])}"
                       f" | {f(d['var_hebdo_pct'])}, {f(d['perf_1m_pct'])}, {f(d['vs_haut_52s_pct'])}")
    if positions:
        out += ["", "Tes positions :"]
        for p in positions:
            out.append(f"- {p['nom']} : {p['quantite']} titres, PRU {p['pru']:.3f}, "
                       f"cours {p['cours']:.3f} ({p['perf_pct']:+.1f} %), valeur ≈ {p['valeur']:.0f}")
    out += ["", "Autres lignes B et C (cours / cible / écart pour atteindre la cible) :"]
    for d in sorted((d for d in lignes if d["bucket"] != "A"),
                    key=lambda x: (x["ecart_cible_pct"] is None, x["ecart_cible_pct"] or 0)):
        cible = f"{d['cible']:.2f}" if d["cible"] else "—"
        ecart = f"{d['ecart_cible_pct']:+.1f} %" if d["ecart_cible_pct"] is not None else "—"
        out.append(f"- [{d['bucket']}] {d['nom']} : {d['cours']:g} / {cible} / {ecart}")
    if errors:
        out += ["", "Cours non récupérés : " + ", ".join(errors)]
    out += ["", "Outil personnel, pas un conseil en investissement."]
    return "\n".join(out)


SYSTEM_PROMPT = (
    "Tu rédiges le mail hebdomadaire de suivi du PEA d'Aubin, en français, ton direct et "
    "précis, comme un analyste qui s'adresse à un investisseur particulier.\n"
    "Données : cours, cibles issues de SA méthode (BPA × PER juste, marge de sécurité par "
    "bucket), variations, positions. Ces chiffres sont la référence : ne les recalcule pas, "
    "n'en invente aucun.\n"
    "Utilise la recherche web UNIQUEMENT pour les valeurs listées dans la rubrique « Recherche "
    "web autorisée » des données (une recherche par valeur, pas plus) : explique POURQUOI elles "
    "ont bougé (résultats, objectif d'analyste, actualité) et donne les nouvelles de NIBE, sa "
    "seule position. Pour toutes les autres valeurs, appuie-toi sur les seules données fournies, "
    "sans recherche. N'affirme que ce que tes recherches confirment, indique la date des "
    "informations, et dis explicitement quand tu n'as rien trouvé.\n"
    "Les lignes du bucket A sont ses achats prioritaires : consacre-leur la plus grande partie du "
    "mail, une à deux phrases par valeur (écart à la cible, tendance sur 1 mois, distance au plus "
    "haut 52 semaines, et la raison du mouvement ou l'actualité marquante, trouvée par recherche). "
    "Dis clairement quelles valeurs A s'éloignent ou se rapprochent de leur cible, sans les "
    "présenter comme à acheter.\n"
    "Structure : 1) bucket A, de la valeur la plus proche de sa cible à la plus éloignée ; "
    "2) signaux sur les buckets B et C ; 3) NIBE : position, PRU, nouvelles ; 4) une ligne « à "
    "surveiller cette semaine » (publications de résultats ou événements datés connus). "
    "Termine par la liste des sources (titre + URL).\n"
    "Tu ne donnes aucun conseil d'achat ou de vente et ne dis jamais « acheter », « vendre » "
    "ou « renforcer » : tu constates les faits et l'écart avec ses propres cibles. "
    "500 mots maximum hors sources."
)


def _texte(msg):
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def synthese_claude(donnees_texte):
    """Synthèse rédigée par Claude avec recherche web ; None si pas de clé ou erreur."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY absente ou vide : synthèse rédigée non générée.", file=sys.stderr)
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    outils = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]

    def appel(tools):
        messages = [{"role": "user", "content": donnees_texte}]
        for _ in range(4):  # la recherche web peut interrompre le tour (pause_turn)
            kwargs = dict(model=model, max_tokens=3000, system=SYSTEM_PROMPT, messages=messages)
            if tools:
                kwargs["tools"] = tools
            msg = client.messages.create(**kwargs)
            blocs = [getattr(b, "type", "?") for b in msg.content]
            u = getattr(msg, "usage", None)
            print(f"API {model} (web={bool(tools)}) : stop_reason={msg.stop_reason}, "
                  f"blocs={blocs}, tokens in/out={getattr(u, 'input_tokens', '?')}/"
                  f"{getattr(u, 'output_tokens', '?')}", file=sys.stderr)
            if msg.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": msg.content}]
        texte = _texte(msg)
        if not texte:
            raise RuntimeError("réponse de l'API sans texte")
        return texte

    try:
        return appel(outils)
    except Exception as e:
        print(f"Synthèse avec recherche web indisponible : {type(e).__name__}: {e}", file=sys.stderr)
    try:  # repli : synthèse sans recherche web
        return appel(None)
    except Exception as e:
        print(f"Synthèse Claude indisponible : {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ mail
def envoyer_mail(sujet, corps):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    dest = os.environ.get("MAIL_TO", user)
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = user, dest, sujet
    m.set_content(corps)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(m)


# ------------------------------------------------------------------ main
def main():
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}

    prices, stats, errors = fetch_prices(cfg["lignes"])
    if not prices:
        sys.exit("Aucun cours récupéré : pas de mail envoyé.")

    lignes, signaux, positions = analyser(cfg, prices, stats)
    date_str = date.today().strftime("%d/%m/%Y")
    detail = texte_brut(date_str, lignes, signaux, positions, errors)
    proches_a = [d["nom"] for d in sorted(
        (d for d in lignes if d["bucket"] == "A" and d["ecart_cible_pct"] is not None),
        key=lambda x: x["ecart_cible_pct"])[:3]]
    cibles_web = ["NIBE Industrier"] + proches_a
    synth = synthese_claude(detail + "\n\nRecherche web autorisée : " + " ; ".join(cibles_web))
    corps = (synth + "\n\n---\n" + detail) if synth else detail
    sujet = f"Suivi PEA {date_str} — " + (f"{len(signaux)} signal(s)" if signaux else "RAS")

    if dry:
        print(sujet + "\n\n" + corps)
    else:
        envoyer_mail(sujet, corps)
        print("Mail envoyé :", sujet)

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({**previous, **prices}, indent=1))


if __name__ == "__main__":
    main()
