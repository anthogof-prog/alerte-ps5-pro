#!/usr/bin/env python3
"""
Alerte stock PS5 Pro.
Vérifie les fiches produit listées dans sites.json et envoie une
notification ntfy si une offre est en stock sous le prix maximum.
"""
import json
import os
import re
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PARIS = ZoneInfo("Europe/Paris")
DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DIR, "sites.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()   # optionnel
FREE_USER = os.environ.get("FREE_USER", "").strip()       # optionnel (SMS Free Mobile)
FREE_PASS = os.environ.get("FREE_PASS", "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

DISPO_OK = ("instock", "limitedavailability", "onlineonly", "preorder")


def prix_vers_float(valeur):
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        return float(valeur)
    texte = str(valeur).replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    texte = texte.replace("€", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", texte)
    return float(m.group()) if m else None


def objets_jsonld(soup):
    """Renvoie tous les objets JSON-LD de la page (y compris dans @graph)."""
    for balise in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(balise.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        pile = [data]
        while pile:
            obj = pile.pop()
            if isinstance(obj, list):
                pile.extend(obj)
            elif isinstance(obj, dict):
                yield obj
                if "@graph" in obj:
                    pile.append(obj["@graph"])


def offres_jsonld(soup):
    """Liste de (prix, disponible) trouvées dans les données structurées."""
    offres = []
    for obj in objets_jsonld(soup):
        types = obj.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        brutes = obj.get("offers", [])
        brutes = brutes if isinstance(brutes, list) else [brutes]
        for o in brutes:
            if not isinstance(o, dict):
                continue
            sous = o.get("offers")
            liste = (sous if isinstance(sous, list) else [sous]) if sous else [o]
            for off in liste:
                if not isinstance(off, dict):
                    continue
                prix = prix_vers_float(off.get("price") or off.get("lowPrice"))
                dispo = str(off.get("availability", "")).lower().rsplit("/", 1)[-1]
                offres.append((prix, dispo in DISPO_OK))
    return offres


def offres_secours(html, soup):
    """Méthode approximative si la page n'a pas de données structurées."""
    prix = None
    for sel in ('meta[property="product:price:amount"]', '[itemprop="price"]'):
        el = soup.select_one(sel)
        if el:
            prix = prix_vers_float(el.get("content") or el.get_text())
            if prix:
                break
    bas = html.lower()
    rupture = any(m in bas for m in ("rupture de stock", "indisponible", "épuisé",
                                     "plus disponible", "out of stock"))
    panier = "ajouter au panier" in bas
    return [(prix, panier and not rupture)] if prix else []


def verifier(site, prix_max):
    nom, url = site["nom"], site.get("url", "").strip()
    if not url:
        return {"nom": nom, "etat": "ignoré (pas de lien)"}
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
    except requests.RequestException as e:
        return {"nom": nom, "etat": f"erreur réseau ({type(e).__name__})"}
    if r.status_code != 200:
        return {"nom": nom, "etat": f"bloqué ou erreur (HTTP {r.status_code})"}

    soup = BeautifulSoup(r.text, "html.parser")
    offres = offres_jsonld(soup)
    methode = "données structurées"
    if not offres:
        offres = offres_secours(r.text, soup)
        methode = "lecture approximative"
    if not offres:
        return {"nom": nom, "etat": "page illisible (protection anti-robot ?)"}

    bonnes = [p for p, dispo in offres if dispo and p and p < prix_max]
    if bonnes:
        return {"nom": nom, "etat": "EN STOCK", "prix": min(bonnes),
                "url": url, "methode": methode}
    dispo_cheres = [p for p, dispo in offres if dispo and p]
    if dispo_cheres:
        return {"nom": nom, "etat": f"en stock mais trop cher ({min(dispo_cheres):.2f} €)"}
    return {"nom": nom, "etat": "rupture"}


def envoyer_sms(texte):
    """SMS gratuit vers son propre numéro, réservé aux abonnés Free Mobile."""
    if not (FREE_USER and FREE_PASS):
        return
    try:
        r = requests.get("https://smsapi.free-mobile.fr/sendmsg",
                         params={"user": FREE_USER, "pass": FREE_PASS, "msg": texte},
                         timeout=15)
        print(f"  SMS Free Mobile : HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"  Échec du SMS : {e}")


def notifier(titre, message, url=None, priorite="urgent", tags="video_game",
             email=False):
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC absent : pas de notification envoyée)")
        return
    entetes = {
        # les en-têtes HTTP doivent rester en ASCII
        "Title": titre.encode("ascii", "ignore").decode(),
        "Priority": priorite,
        "Tags": tags,
    }
    if url:
        entetes["Click"] = url

    # 1) notification sur le téléphone (appli ntfy)
    envoyer_ntfy(entetes, message, "notification")
    # 2) e-mail, envoyé à part pour qu'un refus n'empêche pas la notification
    if email and ALERT_EMAIL:
        envoyer_ntfy(dict(entetes, Email=ALERT_EMAIL), message, "e-mail")


def envoyer_ntfy(entetes, message, quoi):
    try:
        r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                          data=message.encode("utf-8"), headers=entetes, timeout=15)
        if r.ok:
            print(f"  ntfy ({quoi}) : OK")
        else:
            print(f"  ntfy ({quoi}) : ÉCHEC HTTP {r.status_code} -> {r.text[:300]}")
    except requests.RequestException as e:
        print(f"  ntfy ({quoi}) : erreur réseau {e}")


def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)

    maintenant = datetime.now(PARIS)
    print(f"Vérification du {maintenant:%d/%m/%Y à %H:%M} (heure de Paris)")

    if "--test" in sys.argv:
        notifier("Test alerte PS5 Pro",
                 "Ça marche ! Vous recevrez une alerte comme celle-ci dès qu'une "
                 "PS5 Pro sera en stock sous " f"{cfg['prix_max']} €.",
                 priorite="default", tags="white_check_mark", email=True)
        envoyer_sms("Test alerte PS5 Pro : les SMS fonctionnent !")
        print(f"Test envoyé vers le sujet ntfy « {NTFY_TOPIC} ».")

    if maintenant.date() > date.fromisoformat(cfg["date_fin"]):
        print("Période de surveillance terminée. Rien à faire.")
        return

    resultats = [verifier(s, cfg["prix_max"]) for s in cfg["sites"]]
    for res in resultats:
        print(f"- {res['nom']}: {res['etat']}")
        if res["etat"] == "EN STOCK":
            notifier(f"PS5 Pro en stock - {res['nom']}",
                     f"{res['nom']} : {res['prix']:.2f} € — touchez pour ouvrir la page.\n"
                     f"(détecté par {res['methode']}, vérifiez le vendeur)",
                     url=res["url"], email=True)
            envoyer_sms(f"PS5 Pro en stock chez {res['nom']} a {res['prix']:.2f} EUR : {res['url']}")

    # Petit résumé chaque matin pour savoir que tout tourne
    if maintenant.hour == 6 and maintenant.minute < 10:
        lignes = "\n".join(f"{r['nom']} : {r['etat']}" for r in resultats)
        notifier("Surveillance PS5 Pro active", "Point du matin :\n" + lignes,
                 priorite="low", tags="eyes")


if __name__ == "__main__":
    main()
