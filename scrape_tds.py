#!/usr/bin/env python3
"""Relais TDS -> JSON public (Aven de Noël).

Se connecte au site TDS.visualisation avec un compte autorisé, lit les
4 graphiques de l'Aven de Noël (CO2, températures, humidité relative,
tension batterie) et écrit un fichier JSON (docs/data.json) qui peut être
publié (GitHub Pages) puis lu par un blog.

Variables d'environnement :
  TDS_USER, TDS_PASS   identifiants (obligatoires, à stocker dans les secrets)
  TDS_BASE             URL de index.php (défaut : site tds.hydraedre.com)
  TDS_PAGES            pages à lire, séparées par des virgules
                       (défaut : custom_1029 = Aven de Noël)
  OUT_FILE             fichier de sortie (défaut : docs/data.json)
  TDS_DISPLAY          période demandée au site (optionnel, non testé) :
                       day, week, month, 91days, 182days, year, display_3years,
                       display_5years. Défaut : la page par défaut (une semaine).

Historique : à chaque exécution, les mesures lues sont FUSIONNÉES avec celles déjà
présentes dans OUT_FILE (aucune mesure n'est supprimée ni allégée). L'historique
s'allonge donc au fil du temps, au-delà de la semaine affichée par le site.

Ne jamais appeler l'adresse ...?operation=logout : c'est un lien de déconnexion.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

BASE = os.environ.get("TDS_BASE", "https://tds.hydraedre.com/visualisation/index.php")
PAGES = [p.strip() for p in os.environ.get("TDS_PAGES", "custom_1029").split(",") if p.strip()]
OUT_FILE = os.environ.get("OUT_FILE", "docs/data.json")
USER = os.environ.get("TDS_USER", "")
PASSWORD = os.environ.get("TDS_PASS", "")
DISPLAY = os.environ.get("TDS_DISPLAY", "").strip()

# Nom affiché quand la page n'a pas de titre <h1> avant les graphiques
PAGE_SITES = {"custom_1029": "Aven de Noël"}


class FormParser(HTMLParser):
    """Récupère les formulaires et leurs champs <input>."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self.cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "inputs": []}
            self.forms.append(self.cur)
        elif tag == "input" and self.cur is not None:
            self.cur["inputs"].append(a)

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None


def find_login_form(page_html):
    p = FormParser()
    p.feed(page_html)
    for f in p.forms:
        if any((i.get("type") or "").lower() == "password" for i in f["inputs"]):
            return f
    return None


def login(session):
    r = session.get(BASE, timeout=30)
    r.raise_for_status()
    form = find_login_form(r.text)
    if form is None:
        return  # déjà connecté ou pas de connexion requise
    data, user_set = {}, False
    for i in form["inputs"]:
        name = i.get("name")
        if not name:
            continue
        t = (i.get("type") or "text").lower()
        if t == "password":
            data[name] = PASSWORD
        elif t in ("text", "email") and not user_set:
            data[name] = USER
            user_set = True
        elif t in ("hidden", "submit"):
            data[name] = i.get("value", "")
        elif t in ("checkbox", "radio") and "checked" in i:
            data[name] = i.get("value", "on")
    if not user_set:
        sys.exit("Connexion : champ identifiant introuvable dans le formulaire.")
    url = urljoin(r.url, form["action"] or r.url)
    if form["method"] == "post":
        r2 = session.post(url, data=data, timeout=30)
    else:
        r2 = session.get(url, params=data, timeout=30)
    r2.raise_for_status()
    # Vérification : on ne doit plus voir de formulaire de connexion
    check = session.get(BASE, timeout=30)
    if find_login_form(check.text) is not None:
        sys.exit("Connexion refusée : identifiants incorrects ou formulaire différent de celui attendu.")


TOKEN = re.compile(
    r"<H1[^>]*>(?P<h1>.*?)</H1>|var (?P<plot>plot\d+)_parameters\s*=\s*\"(?P<raw>(?:[^\"\\]|\\.)*)\";",
    re.I | re.S,
)


def decode_params(raw):
    """Décode la chaîne JavaScript plotNNN_parameters en dictionnaire."""
    inner = json.loads('"' + raw.replace("\\'", "'") + '"')
    return json.loads(inner)


def y_axis_fit(layers):
    """Retourne (pente, origine, min, max) reliant y normalisé -> valeur, d'après les graduations."""
    for layer in layers:
        labels = (layer.get("y_labels") or {}).get("values") or []
        pts = []
        for lab in labels:
            try:
                pts.append((float(lab["y"]), float(lab["txt"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(pts) >= 2:
            (y0, v0), (y1, v1) = pts[0], pts[-1]
            if y1 == y0:
                continue
            slope = (v1 - v0) / (y1 - y0)
            return slope, v0 - slope * y0, min(v for _, v in pts), max(v for _, v in pts)
    return None


def thresholds_from_shapes(layers, fit):
    """Seuils (zones colorées) du graphique, convertis dans l'unité de l'axe."""
    if fit is None:
        return []
    slope, origin = fit[0], fit[1]
    ys = set()
    for layer in layers:
        for shape in layer.get("shapes", []) or []:
            for v in shape.get("values", []):
                if 0 < v["y"] < 1:
                    ys.add(round(v["y"], 6))
    return sorted((round(origin + slope * y, 4) for y in ys), reverse=True)


def parse_page(page_html, page_name):
    charts = {}
    site = PAGE_SITES.get(page_name, "")
    # Période affichée par la page (début / fin, en secondes)
    m0 = re.search(r"plot\d+\.t0\s*=\s*(\d+)", page_html)
    m1 = re.search(r"plot\d+\.t1\s*=\s*(\d+)", page_html)
    page_t0 = int(m0.group(1)) if m0 else None
    page_t1 = int(m1.group(1)) if m1 else None
    for m in TOKEN.finditer(page_html):
        if m.group("h1") is not None:
            site = html.unescape(re.sub(r"<[^>]+>", "", m.group("h1"))).strip()
            continue
        try:
            params = decode_params(m.group("raw"))
        except (ValueError, KeyError):
            print(f"Avertissement : {m.group('plot')} illisible, ignoré.", file=sys.stderr)
            continue
        layers = params.get("layers", [])
        fit = y_axis_fit(layers)
        series = []
        for layer in layers:
            if layer.get("type") != "line":
                continue
            values = layer.get("values", [])
            if "x_span" in layer:
                # Courbe datée : x et y sont normalisés par x_span/x_offset et y_span/y_offset
                xs, xo = layer["x_span"], layer["x_offset"]
                ys, yo = layer["y_span"], layer.get("y_offset", 0)
                pts = [
                    [int(round((xo + v["x"] * xs) * 1000)), round(v["y"] * ys + yo, 5)]
                    for v in values
                ]
            elif fit is not None and page_t0 is not None and page_t1 is not None:
                # Courbe sans échelle propre (ex. tension batterie) : on utilise la période
                # de la page pour x et les graduations de l'axe pour y
                slope, origin = fit[0], fit[1]
                span = page_t1 - page_t0
                pts = [
                    [int(round((page_t0 + v["x"] * span) * 1000)), round(origin + slope * v["y"], 5)]
                    for v in values
                ]
            else:
                continue
            pts.sort()
            series.append({"name": layer.get("title", ""), "color": layer.get("color", "#00A000"), "points": pts})
        if not series:
            continue
        opts = params.get("options", {})
        chart = {
            "page": page_name,
            "site": site,
            "title": opts.get("title", ""),
            "unit": opts.get("title_left", ""),
            "series": series,
        }
        if fit is not None:
            chart["ymin"], chart["ymax"] = fit[2], fit[3]
        th = thresholds_from_shapes(layers, fit)
        if th:
            chart["thresholds"] = th
        charts[str(params.get("id"))] = chart
    return charts


def merge_charts(old, new):
    """Fusionne les nouvelles mesures dans l'historique : union par horodatage,
    la valeur la plus récente l'emporte, aucune mesure n'est supprimée."""
    merged = dict(old)
    for cid, chart in new.items():
        prev = merged.get(cid)
        if prev is None:
            merged[cid] = chart
            continue
        series = []
        for idx, ns in enumerate(chart["series"]):
            # Série correspondante dans l'ancien fichier : même nom, sinon même position
            os_ = next((s for s in prev.get("series", []) if s.get("name") == ns["name"]), None)
            if os_ is None and idx < len(prev.get("series", [])):
                os_ = prev["series"][idx]
            points = {p[0]: p[1] for p in (os_["points"] if os_ else [])}
            points.update({p[0]: p[1] for p in ns["points"]})
            series.append({**ns, "points": [[t, points[t]] for t in sorted(points)]})
        merged[cid] = {**chart, "series": series}
    return merged


def load_existing(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("charts", {})
    except (OSError, ValueError):
        return {}


def fetch_page(session, page):
    """Page par défaut (GET), ou période choisie (POST du formulaire du site)."""
    if not DISPLAY:
        return session.get(BASE, params={"show": page}, timeout=60)
    form = {
        "show": page,
        "display": DISPLAY,
        "t0_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ttimezone": "1",
        "group_code": "",
        "date_compare": "2013-11-12",
        "serial_number": "",
        "equipment_id": "",
        "update": "Mettre-à-jour",
    }
    return session.post(BASE, data=form, timeout=120)


def main():
    if not USER or not PASSWORD:
        sys.exit("TDS_USER et TDS_PASS doivent être définis.")
    session = requests.Session()
    session.headers["User-Agent"] = "tds-relay (publication de mesures avec autorisation du titulaire)"
    login(session)
    fresh = {}
    for page in PAGES:
        r = fetch_page(session, page)
        r.raise_for_status()
        found = parse_page(r.text, page)
        print(f"{page} : {len(found)} graphique(s)")
        fresh.update(found)
        time.sleep(1)
    if not fresh:
        sys.exit("Aucun graphique trouvé : le fichier de sortie n'est pas modifié.")
    all_charts = merge_charts(load_existing(OUT_FILE), fresh)
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "charts": all_charts}
    os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Écrit : {OUT_FILE} ({len(all_charts)} graphiques)")


if __name__ == "__main__":
    main()
