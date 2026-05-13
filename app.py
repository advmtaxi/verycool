import subprocess
import sys
import os

# ── Ensure dependencies are installed BEFORE any other imports ────────────────
def install_deps():
    print("[INIT] Checking dependencies...")
    try:
        import flask
        import curl_cffi
        import playwright
    except ImportError:
        print("[INIT] Installing missing dependencies...")
        subprocess.run([sys.executable, "-m", "pip", "install", "flask", "requests", "playwright", "curl_cffi"], check=True)
    
    # Ensure playwright browsers are installed
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception as e:
        print(f"[INIT] Playwright install warning: {e}")

if __name__ == "__main__" or __name__ == "app":
    install_deps()

# ── Now safe to import everything else ───────────────────────────────────────
import asyncio
import threading
import time
import re
import logging
from curl_cffi import requests as req_lib
from flask import Flask, jsonify, Response, request, redirect
from playwright.async_api import async_playwright
from urllib.parse import quote, urlparse, urljoin

print("New App.py Loaded v6 - Dependency Fix & Master M3U8")

# ── Config ───────────────────────────────────────────────────────────────────
PORT         = int(os.environ.get("PORT", 7860))
IDLE_TIMEOUT = 600   # 10 min idle before evicting a session
REFERERS     = ["https://embedsports.top/", "https://streamed.pk/"]
ORIGINS      = ["https://embedsports.top", "https://streamed.pk"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Persistent asyncio event loop ───────────────────────────────────────────
_loop = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_start_loop, args=(_loop,), daemon=True).start()

# ── Storage ──────────────────────────────────────────────────────────────────
stream_cache    = {}
cache_lock      = threading.Lock()
active_sniffers = {}

# =============================================================================
# HELPERS
# =============================================================================

def get_proxy_host():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host   = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"

def _make_session(captured_headers: dict, captured_cookies: dict) -> req_lib.Session:
    s = req_lib.Session(impersonate="chrome124")
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": REFERERS[0],
        "Origin": ORIGINS[0],
    })
    KEEP = {"referer", "origin", "user-agent", "accept", "accept-language", "authorization", "x-playback-session-id"}
    for k, v in captured_headers.items():
        if k.lower() in KEEP:
            s.headers[k] = v
    s.cookies.update(captured_cookies)
    return s

def _fetch_with_retry(embed_url: str, url: str, is_binary=False):
    entry = _get_cached(embed_url)
    if not entry: return None
    session = entry["session"]
    
    def attempt(ref, orig):
        try:
            session.headers["Referer"] = ref
            session.headers["Origin"] = orig
            r = session.get(url, timeout=10)
            if r.status_code == 200:
                return r.content if is_binary else r.text
        except: pass
        return None

    res = attempt(session.headers.get("Referer"), session.headers.get("Origin"))
    if res: return res
    
    for ref, orig in zip(REFERERS, ORIGINS):
        res = attempt(ref, orig)
        if res:
            session.headers["Referer"], session.headers["Origin"] = ref, orig
            return res
    return None

def _get_cached(key: str) -> dict | None:
    with cache_lock:
        c = stream_cache.get(key)
        if c and time.time() < c["expires"]: return c
    return None

def _touch(key: str):
    with cache_lock:
        if key in stream_cache:
            stream_cache[key]["last_accessed"] = time.time()
            stream_cache[key]["expires"] = time.time() + 10800

# =============================================================================
# M3U8 REWRITER
# =============================================================================

def _rewrite_m3u8(content: str, playlist_url: str, embed_key: str, proxy_host: str) -> str:
    lines = []
    base_url = playlist_url.rsplit('/', 1)[0] + '/'
    def resolve(u: str) -> str:
        return urljoin(base_url, u) if not u.startswith("http") else u

    for raw in content.splitlines():
        line = raw.strip()
        if not line: continue
        if line.startswith("#"):
            if 'URI="' in line:
                line = re.sub(r'URI="(.*?)"', lambda m: f'URI="{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolve(m.group(1)))}"', line)
            lines.append(line)
        else:
            lines.append(f"{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolve(line))}")
    return "\n".join(lines)

# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def home():
    return jsonify({"status": "online", "cached": len(stream_cache)})

@app.route("/proxy")
def proxy():
    embed_url = request.args.get("link")
    target_url = request.args.get("_url") or request.args.get("_variant") or request.args.get("_key")
    if not embed_url: return "Missing link", 400

    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for _ in range(60):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached: break
    
    if not cached: return "Sniffing failed", 504
    _touch(embed_url)

    if not target_url:
        target_url = cached["url"]
        body = cached["body"]
        if time.time() - cached.get("body_ts", 0) > 5:
            new_body = _fetch_with_retry(embed_url, target_url)
            if new_body:
                body = new_body
                with cache_lock:
                    stream_cache[embed_url]["body"], stream_cache[embed_url]["body_ts"] = body, time.time()
    else:
        is_binary = any(ext in target_url.lower() for ext in [".ts", ".mp4", ".m4s", ".key"])
        body = _fetch_with_retry(embed_url, target_url, is_binary=is_binary)

    if body is None: return "Fetch failed", 503

    if isinstance(body, str) and "#EXTM3U" in body:
        return Response(_rewrite_m3u8(body, target_url, embed_url, get_proxy_host()), mimetype="application/vnd.apple.mpegurl")
    return Response(body, mimetype="video/MP2T" if ".ts" in target_url.lower() else "application/octet-stream")

# =============================================================================
# SNIFFER
# =============================================================================

def _ensure_sniffer(embed_url: str):
    with cache_lock:
        if embed_url in active_sniffers: return
        active_sniffers[embed_url] = True
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)

async def _sniff(embed_url: str):
    found = {}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2}", lambda route: route.abort())

            async def on_response(resp):
                if not found and ".m3u8" in resp.url and resp.status == 200:
                    try:
                        text = await resp.text()
                        if "#EXTM3U" in text:
                            found.update({"url": resp.url, "body": text, "headers": await resp.request.all_headers(), "cookies": await context.cookies()})
                    except: pass
            page.on("response", on_response)
            await page.goto(embed_url, timeout=30000, wait_until="commit")
            for _ in range(10):
                if found: break
                await asyncio.sleep(1)
                try:
                    btn = page.locator(".clappr-big-play-button, .vjs-big-play-button, button[class*='play']").first
                    if await btn.is_visible(): await btn.click()
                except: pass
            await browser.close()
        except Exception as e: logger.error(f"Sniff error: {e}")

    if found:
        with cache_lock:
            stream_cache[embed_url] = {"url": found["url"], "body": found["body"], "body_ts": time.time(), "session": _make_session(found["headers"], {c["name"]: c["value"] for c in found["cookies"]}), "expires": time.time() + 10800, "last_accessed": time.time()}
    with cache_lock: active_sniffers.pop(embed_url, None)

# =============================================================================
# CLEANUP
# =============================================================================

def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with cache_lock:
            to_remove = [k for k, v in stream_cache.items() if v["expires"] < now or (now - v["last_accessed"]) > IDLE_TIMEOUT]
            for k in to_remove:
                entry = stream_cache.pop(k)
                try: entry["session"].close()
                except: pass

if __name__ == "__main__":
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
