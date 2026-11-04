"""
Render relay for DACH shop probing, personal temporary use only.

Cloudflare is NOT used here. Vercel was dropped 2026-11-04 because
hajidon's Vercel signup failed, cause unknown.

POST /api/probe
Body: {"domains": ["example.de", ...]}  (max 20 per call, .de/.at/.ch only)
Returns: {"results": [ {domain, is_shop, platform, country, confidence,
  has_cart, has_vat_notice, has_impressum, store_title, final_url, reason,
  error}, ... ]}

Logic mirrors scripts/4_deep_rescue_worker.py signatures exactly.
Fetcher is Python requests, proven on this project's pilot shops where
aiohttp timed out. A domain is fetched at https://domain, then
https://www.domain on failure or empty page. Response body is streamed
and capped at 1.5 MB to keep Render bandwidth low.
Temporary relay: delete the Render service after the September run.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SIGNATURES = {
    "Shopware": [re.compile(r"bundles/storefront", re.I), re.compile(r"content=[\"']Shopware", re.I), re.compile(r"data-add-to-cart", re.I)],
    "JTL-Shop": [re.compile(r"content=[\"']JTL-Shop", re.I), re.compile(r"jtl_token", re.I), re.compile(r"/templates/(NOVA|Evo)/", re.I), re.compile(r"jtl\.php", re.I)],
    "WooCommerce": [re.compile(r"wp-content/plugins/woocommerce", re.I), re.compile(r"woocommerce-Price-amount", re.I), re.compile(r"class=[\"'][^\"']*woocommerce", re.I)],
    "Shopify": [re.compile(r"cdn\.shopify\.com", re.I), re.compile(r"window\.Shopify", re.I), re.compile(r"Shopify\.theme", re.I)],
    "Gambio": [re.compile(r"gm_javascript", re.I), re.compile(r"templates/gambio", re.I), re.compile(r"gambio_updater", re.I)],
    "Plentymarkets": [re.compile(r"ceres-theme", re.I), re.compile(r"plentymarkets", re.I)],
    "Oxid": [re.compile(r"oxid-esales", re.I), re.compile(r"out/(flow|wave)/", re.I)],
}

CART_REGEX = re.compile(r"(in\s+den\s+warenkorb|in\s+den\s+korb|in\s+die\s+einkaufstasche|jetzt\s+kaufen|in\s+den\s+einkaufswagen|add\s+to\s+cart|warenkorb|kasse|checkout)", re.I)
LEGAL_PRICE_REGEX = re.compile(r"(inkl\.\s*mwst|inklusive\s*mehrwertsteuer|zzgl\.\s*versand|inkl\.\s*ust)", re.I)
B2B_DISQUALIFIERS = re.compile(r"(nur\s+f[üu]r\s+gewerbetreibende|h[äa]ndler-login|preis\s+auf\s+anfrage|preise\s+erst\s+nach\s+anmeldung|kein\s+verkauf\s+an\s+privat)", re.I)
PARKED_DISQUALIFIERS = re.compile(r"(steht\s+zum\s+verkauf|domain\s+kaufen|dieser\s+domainname|sedo\.com|parkingcrew|dan\.com|hugedomains)", re.I)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
}
MAX_BODY = 1_500_000


def detect_platform(text):
    for platform, patterns in SIGNATURES.items():
        for pat in patterns:
            if pat.search(text):
                return platform
    return "Custom_Ecom"


def fetch_text(url):
    with requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True, stream=True) as r:
        if r.status_code != 200:
            return None, r.status_code, str(r.url)
        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536, decode_unicode=True):
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY:
                    break
        return "".join(chunks), r.status_code, str(r.url)


def probe_domain(domain):
    domain = domain.strip().lower()
    result = {
        "domain": domain,
        "is_shop": False,
        "platform": "None",
        "country": "DE" if domain.endswith(".de") else ("AT" if domain.endswith(".at") else ("CH" if domain.endswith(".ch") else "DACH")),
        "confidence": 0.0,
        "has_cart": False,
        "has_vat_notice": False,
        "has_impressum": False,
        "store_title": None,
        "final_url": None,
        "reason": "",
        "error": "",
    }
    last_error = ""
    for url in (f"https://{domain}", f"https://www.{domain}"):
        try:
            text, status, final_url = fetch_text(url)
            if text is None or len(text) <= 400:
                last_error = f"http_{status}"
                continue
            if PARKED_DISQUALIFIERS.search(text) or B2B_DISQUALIFIERS.search(text):
                result["reason"] = "Parked / B2B disqualified"
                return result
            platform = detect_platform(text)
            has_cart = bool(CART_REGEX.search(text))
            has_vat = bool(LEGAL_PRICE_REGEX.search(text))
            has_impressum = "impressum" in text.lower()
            result["has_cart"] = has_cart
            result["has_vat_notice"] = has_vat
            result["has_impressum"] = has_impressum
            if platform != "Custom_Ecom":
                result["is_shop"] = True
                result["platform"] = platform
                result["confidence"] = 0.95
                result["final_url"] = final_url
                result["reason"] = f"Relay {platform} store"
            elif has_cart and has_vat and has_impressum:
                result["is_shop"] = True
                result["platform"] = "Custom_Ecom"
                result["confidence"] = 0.85
                result["final_url"] = final_url
                result["reason"] = "Relay Custom E-Com"
            if result["is_shop"]:
                m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
                if m:
                    result["store_title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
            return result
        except Exception as e:  # noqa: BLE001 - relay must return per-domain errors, not crash
            last_error = type(e).__name__
            continue
    result["error"] = last_error
    return result


@app.route("/", methods=["GET"])
@app.route("/api/probe", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "dach-relay-render", "max_batch": 20})


@app.route("/api/probe", methods=["POST"])
def probe():
    data = request.get_json(silent=True) or {}
    domains = data.get("domains", [])
    if not isinstance(domains, list) or not domains or len(domains) > 20:
        return jsonify({"error": "domains must be a list of 1 to 20 domains"}), 400
    domains = [d for d in domains if isinstance(d, str) and d.strip().lower().endswith((".de", ".at", ".ch"))]
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(probe_domain, domains))
    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
