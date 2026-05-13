print("New App.py Loaded v5 - Optimized & Master M3U8 Fix")
import subprocess
import sys
import asyncio
import threading
import time
import re
import os
import logging
from curl_cffi import requests as req_lib
from flask import Flask, jsonify, Response, request, redirect
from playwright.async_api import async_playwright
from urllib.parse import quote, urlparse, urljoin

# ── Ensure dependencies ──────────────────────────────────────────────────────
try:
    import curl_cffi
    import playwright
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "playwright", "curl_cffi"], check=True)

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

def run_async(coro, timeout=30):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)

# ── Storage ──────────────────────────────────────────────────────────────────
stream_cache    = {}
cache_lock      = threading.Lock()
active_sniffers = {} # map embed_url -> Event to wait on

# =============================================================================
# HELPERS
# =============================================================================

def get_proxy_host():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host   = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"

def _make_session(captured_headers: dict, captured_cookies: dict) -> req_lib.Session:
    """Build a session with browser impersonation and requested headers."""
    s = req_lib.Session(impersonate="chrome124")
    
    # Use requested referer/origin if not already set or as defaults
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": REFERERS[0],
        "Origin": ORIGINS[0],
    })

    # Override with captured headers if they exist
    KEEP = {"referer", "origin", "user-agent", "accept", "accept-language", "authorization", "x-playback-session-id"}
    for k, v in captured_headers.items():
        if k.lower() in KEEP:
            s.headers[k] = v
            
    s.cookies.update(captured_cookies)
    return s

def _fetch_with_retry(embed_url: str, url: str, is_binary=False):
    """Fetch content with session, trying alternative referers if it fails."""
    entry = _get_cached(embed_url)
    if not entry:
        return None
    
    session = entry["session"]
    
    def attempt(ref, orig):
        try:
            session.headers["Referer"] = ref
            session.headers["Origin"] = orig
            r = session.get(url, timeout=10)
            if r.status_code == 200:
                return r.content if is_binary else r.text
            logger.warning(f"[FETCH] {r.status_code} for {url[:50]} with {ref}")
        except Exception as e:
            logger.warning(f"[FETCH ERROR] {e} for {url[:50]}")
        return None

    # Try current headers first
    res = attempt(session.headers.get("Referer"), session.headers.get("Origin"))
    if res: return res
    
    # Try alternatives
    for ref, orig in zip(REFERERS, ORIGINS):
        if ref != session.headers.get("Referer"):
            res = attempt(ref, orig)
            if res:
                # Update session headers for future use if this worked
                session.headers["Referer"] = ref
                session.headers["Origin"] = orig
                return res
    return None

def _get_cached(key: str) -> dict | None:
    with cache_lock:
        c = stream_cache.get(key)
        if c and time.time() < c["expires"]:
            return c
    return None

def _touch(key: str):
    with cache_lock:
        if key in stream_cache:
            stream_cache[key]["last_accessed"] = time.time()
            stream_cache[key]["expires"]        = time.time() + 10800

# =============================================================================
# M3U8 REWRITER
# =============================================================================

def _rewrite_m3u8(content: str, playlist_url: str, embed_key: str, proxy_host: str) -> str:
    """
    Improved rewriter for Master and Media playlists.
    Proxies everything (variants, keys, segments) to ensure headers are always sent.
    """
    lines = []
    header_injected = False
    
    # Determine base URL for relative paths
    base_url = playlist_url.rsplit('/', 1)[0] + '/'

    def resolve(u: str) -> str:
        if u.startswith("http"): return u
        return urljoin(base_url, u)

    for raw in content.splitlines():
        line = raw.strip()
        if not line: continue

        # Inject EVENT type for better buffering if not present
        if line == "#EXTM3U" and not header_injected:
            lines.append(line)
            # lines.append("#EXT-X-PLAYLIST-TYPE:EVENT") # Optional: can cause issues with some live streams
            header_injected = True
            continue

        if line.startswith("#"):
            # Handle URI in tags (Keys, Map, etc.)
            if 'URI="' in line:
                def _rewrite_uri(m):
                    uri = m.group(1)
                    resolved = resolve(uri)
                    # Proxy everything to be safe with headers
                    return f'URI="{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolved)}"'
                line = re.sub(r'URI="(.*?)"', _rewrite_uri, line)
            lines.append(line)
        else:
            # It's a URL (Variant or Segment)
            resolved = resolve(line)
            # Proxy everything
            lines.append(f"{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolved)}")

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
    
    if not embed_url:
        return "Missing link", 400

    proxy_host = get_proxy_host()
    
    # 1. Ensure we have a session
    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        # Wait for sniffer (max 30s)
        for _ in range(60):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached: break
    
    if not cached:
        return "Sniffing failed", 504

    _touch(embed_url)

    # 2. If no target_url, we are requesting the master playlist
    if not target_url:
        target_url = cached["url"]
        body = cached["body"]
        # Refresh master if older than 5s
        if time.time() - cached.get("body_ts", 0) > 5:
            new_body = _fetch_with_retry(embed_url, target_url)
            if new_body:
                body = new_body
                with cache_lock:
                    stream_cache[embed_url]["body"] = body
                    stream_cache[embed_url]["body_ts"] = time.time()
    else:
        # Fetch the requested variant/segment/key
        is_binary = any(ext in target_url.lower() for ext in [".ts", ".mp4", ".m4s", ".key"])
        body = _fetch_with_retry(embed_url, target_url, is_binary=is_binary)

    if body is None:
        return "Fetch failed", 503

    # 3. Handle response
    if isinstance(body, str) and "#EXTM3U" in body:
        # It's a playlist, rewrite it
        rewritten = _rewrite_m3u8(body, target_url, embed_url, proxy_host)
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl")
    else:
        # It's a segment or key
        mimetype = "video/MP2T" if ".ts" in target_url.lower() else "application/octet-stream"
        return Response(body, mimetype=mimetype)

# =============================================================================
# SNIFFER
# =============================================================================

def _ensure_sniffer(embed_url: str):
    with cache_lock:
        if embed_url in active_sniffers:
            return
        active_sniffers[embed_url] = True
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)

async def _sniff(embed_url: str):
    logger.info(f"[SNIFF] Starting for: {embed_url}")
    found = {}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        
        # Optimization: Block unnecessary resources
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,svg,css,woff,woff2}", lambda route: route.abort())

        async def on_response(resp):
            if found: return
            u = resp.url
            if ".m3u8" in u and resp.status == 200:
                try:
                    text = await resp.text()
                    if "#EXTM3U" in text:
                        found["url"] = u
                        found["body"] = text
                        found["headers"] = await resp.request.all_headers()
                        found["cookies"] = await context.cookies()
                except: pass

        page.on("response", on_response)
        
        try:
            # Try to load the page
            await page.goto(embed_url, timeout=30000, wait_until="commit")
            # Wait a bit for the player to trigger
            for _ in range(10):
                if found: break
                await asyncio.sleep(1)
                # Try clicking play button if visible
                try:
                    play_btn = page.locator(".clappr-big-play-button, .vjs-big-play-button, button[class*='play']").first
                    if await play_btn.is_visible():
                        await play_btn.click()
                except: pass
        except Exception as e:
            logger.error(f"[SNIFF ERROR] {e}")
        finally:
            await browser.close()

    if found:
        cookie_dict = {c["name"]: c["value"] for c in found["cookies"]}
        session = _make_session(found["headers"], cookie_dict)
        with cache_lock:
            stream_cache[embed_url] = {
                "url": found["url"],
                "body": found["body"],
                "body_ts": time.time(),
                "session": session,
                "expires": time.time() + 10800,
                "last_accessed": time.time()
            }
        logger.info(f"[SNIFF SUCCESS] Found m3u8 for {embed_url}")
    else:
        logger.error(f"[SNIFF FAILED] No m3u8 for {embed_url}")
    
    with cache_lock:
        active_sniffers.pop(embed_url, None)

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
                logger.info(f"[CLEANUP] Removed {k[:50]}")

if __name__ == "__main__":
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    # Install playwright browsers if needed
    subprocess.run(["playwright", "install", "chromium"], check=False)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
