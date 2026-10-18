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

    prices, errors = {}, []
    for l in lignes:
        if not l["actif"] or not l["yahoo"]:
            continue
        try:
            h = yf.Ticker(l["yahoo"]).history(period="5d")
            px = float(h["Close"].dropna().iloc[-1])
            prices[l["tk"]] = round(px * fx(l["devise_cote"], l["devise_watchlist"]), 4)
        except Exception as e:  # une valeur en échec ne bloque pas les autres
            errors.append(f"{l['tk']} ({type(e).__name__})")
    return prices, errors


# ------------------------------------------------------------ valorisation
def evaluer(l, cours, cfg):
    bpa, jper = l["bpa"], l["jper"]
    vi = bpa * jper if bpa > 0 else None
    marge = cfg["marge_par_bucket"].get(l["bucket"], 30)
    cible = vi * (1 - marge / 100) if vi else None
    per = cours / bpa if bpa > 0 else None
    ecart = (cours - cible) / cours * 100 if cible else None  # % de baisse pour atteindre la cible
    return dict(vi=vi, cible=cible, marge=marge, per=per, ecart_cible_pct=ecart)


def analyser(cfg, prices, previous):
    lignes_out, signaux = [], []
    for l in cfg["lignes"]:
        tk = l["tk"]
        if not l["actif"]:
            continue
        cours = prices.get(tk)
        if cours is None:
            continue
        ev = evaluer(l, cours, cfg)
        prev = previous.get(tk)
        var = (cours - prev) / prev * 100 if prev else None
        d = dict(tk=tk, nom=l["nom"], bucket=l["bucket"], cours=cours,
                 var_hebdo_pct=var, **ev)
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
    if positions:
        out += ["", "Tes positions :"]
        for p in positions:
            out.append(f"- {p['nom']} : {p['quantite']} titres, PRU {p['pru']:.3f}, "
                       f"cours {p['cours']:.3f} ({p['perf_pct']:+.1f} %), valeur ≈ {p['valeur']:.0f}")
    out += ["", "Watchlist (cours / cible / écart pour atteindre la cible) :"]
    for d in sorted(lignes, key=lambda x: (x["ecart_cible_pct"] is None, x["ecart_cible_pct"] or 0)):
        cible = f"{d['cible']:.2f}" if d["cible"] else "—"
        ecart = f"{d['ecart_cible_pct']:+.1f} %" if d["ecart_cible_pct"] is not None else "—"
        out.append(f"- [{d['bucket']}] {d['nom']} : {d['cours']:g} / {cible} / {ecart}")
    if errors:
        out += ["", "Cours non récupérés : " + ", ".join(errors)]
    out += ["", "Outil personnel, pas un conseil en investissement."]
    return "\n".join(out)


def synthese_claude(donnees_texte):
    """Synthèse rédigée par Claude ; retourne None si pas de clé ou en cas d'erreur."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
            max_tokens=900,
            system=(
                "Tu rédiges le mail hebdomadaire de suivi du PEA d'Aubin, en français, ton direct. "
                "Appuie-toi UNIQUEMENT sur les données fournies (cours, cibles issues de SA méthode, "
                "variations, positions). N'invente aucun chiffre ni aucune actualité. "
                "Tu ne donnes aucun conseil d'achat ou de vente : tu constates l'écart avec ses propres "
                "cibles et mets en avant ce qui a bougé. 200 mots maximum, puis rien d'autre."
            ),
            messages=[{"role": "user", "content": donnees_texte}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as e:
        print(f"Synthèse Claude indisponible : {e}", file=sys.stderr)
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

    prices, errors = fetch_prices(cfg["lignes"])
    if not prices:
        sys.exit("Aucun cours récupéré : pas de mail envoyé.")

    lignes, signaux, positions = analyser(cfg, prices, previous)
    date_str = date.today().strftime("%d/%m/%Y")
    detail = texte_brut(date_str, lignes, signaux, positions, errors)
    synth = synthese_claude(detail)
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
