import asyncio
import threading
import time
import re
import os
import logging
import subprocess
from flask import Flask, jsonify, Response, request, redirect
from playwright.async_api import async_playwright
from urllib.parse import quote, unquote, urlparse

# ── Install Chromium on startup ──────────────────────────────────────────────
print("[INIT] Installing Chromium...")
subprocess.run(["playwright", "install", "chromium"], check=True)
subprocess.run(["playwright", "install-deps", "chromium"], check=True)
print("[INIT] Chromium ready")

# ── Config ───────────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", 7860))
PROXY_HOST = "https://notgoodman-verycoolman.hf.space"

IDLE_TIMEOUT = 45  # seconds of no playlist requests before we kill the browser

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Persistent asyncio event loop ────────────────────────────────────────────
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
active_sniffers = set()

# ── CORS ─────────────────────────────────────────────────────────────────────
@app.after_request
def after_request(resp):
    resp.headers['Access-Control-Allow-Origin']  = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization,Range'
    resp.headers['Access-Control-Allow-Methods'] = 'GET,OPTIONS,HEAD'
    resp.headers['Access-Control-Expose-Headers']= 'Content-Length,Content-Range,Accept-Ranges'
    return resp

# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def home():
    return jsonify({"status": "online", "cached": len(stream_cache), "sniffing": len(active_sniffers)})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/redirect')
def redirect_to_m3u8():
    """
    302-redirect straight to the raw CDN m3u8.
    Use this when your player can fetch it directly without needing spoofed headers.
    e.g. /redirect?link=https://embedsports.top/embed/...
    """
    embed_url = request.args.get('link')
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for i in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached:
                break

    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    return redirect(cached['url'], code=302)


@app.route('/proxy')
def proxy():
    embed_url = request.args.get('link')
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    variant_url = request.args.get('_variant')  # sub-playlist (chunklist, mono.ts.m3u8, etc.)
    key_url     = request.args.get('_key')       # AES-128 key

    # ── Encryption key: proxy through browser so Origin/Referer are preserved ──
    if key_url:
        cached = _get_cached(embed_url)
        if cached and cached.get('page'):
            _touch(embed_url)
            try:
                key_bytes = run_async(_browser_fetch_binary(cached['page'], key_url), timeout=8)
                if key_bytes:
                    return Response(key_bytes, mimetype='application/octet-stream')
            except Exception as e:
                logger.warning(f"[KEY] Fetch error: {e}")
        return Response("Key fetch failed", status=503)

    # ── Variant / chunklist playlist: fetch through browser, then rewrite ──────
    if variant_url:
        cached = _get_cached(embed_url)
        if cached and cached.get('page'):
            _touch(embed_url)
            try:
                body = run_async(_browser_fetch(cached['page'], variant_url), timeout=8)
                if body and '#EXTM3U' in body:
                    rewritten = _rewrite_m3u8(body, variant_url, embed_url)
                    return Response(
                        rewritten,
                        mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache, no-store"},
                    )
                else:
                    logger.warning(f"[VARIANT] Bad/empty body for {variant_url[:80]}")
            except Exception as e:
                logger.warning(f"[VARIANT] Fetch error: {e}")
        return Response("Variant fetch failed", status=503)

    # ── Master playlist ───────────────────────────────────────────────────────
    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for i in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached:
                logger.info(f"[PROXY] Ready in {i*0.5:.1f}s")
                break

    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    body = _get_fresh_body(embed_url, cached)
    if not body:
        return Response("No playlist body", status=503)

    rewritten = _rewrite_m3u8(body, cached['url'], embed_url)
    return Response(rewritten, mimetype="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache, no-store"})


@app.route('/extract')
def extract():
    embed_url = request.args.get('url')
    if not embed_url:
        return jsonify({"success": False, "error": "missing ?url="}), 400

    cached = _get_cached(embed_url)
    if cached:
        return jsonify({"success": True, "m3u8": cached['url']})

    _ensure_sniffer(embed_url)
    for _ in range(50):
        time.sleep(0.5)
        cached = _get_cached(embed_url)
        if cached:
            return jsonify({"success": True, "m3u8": cached['url']})

    return jsonify({"success": False, "error": "timeout"}), 202

# =============================================================================
# HELPERS
# =============================================================================

def _get_cached(key):
    with cache_lock:
        c = stream_cache.get(key)
        if c and time.time() < c['expires']:
            return c
    return None


def _touch(key):
    """Update last_accessed timestamp so the idle watcher knows the stream is still live."""
    with cache_lock:
        if key in stream_cache:
            stream_cache[key]['last_accessed'] = time.time()


def _get_fresh_body(embed_url, cached):
    """Refresh master playlist via browser fetch. Rate-limited to once/2s."""
    now  = time.time()
    last = cached.get('body_ts', 0)

    if now - last < 2.0:
        return cached.get('body')

    page = cached.get('page')
    if not page:
        return cached.get('body')

    try:
        body = run_async(_browser_fetch(page, cached['url']), timeout=5)
        if body and '#EXTM3U' in body:
            with cache_lock:
                if embed_url in stream_cache:
                    stream_cache[embed_url]['body']    = body
                    stream_cache[embed_url]['body_ts'] = now
            logger.info(f"[REFRESH] ✅ {len(body)} bytes")
            return body
        logger.warning("[REFRESH] Bad body, using cached")
    except Exception as e:
        logger.warning(f"[REFRESH] Error: {e}, using cached")

    return cached.get('body')


async def _browser_fetch(page, url: str) -> str | None:
    """Fetch a URL as text inside the browser context (same IP + cookies)."""
    return await page.evaluate(f"""
        async () => {{
            try {{
                const r = await fetch({repr(url)}, {{cache: "no-store"}});
                if (!r.ok) return null;
                return await r.text();
            }} catch(e) {{
                return null;
            }}
        }}
    """)


async def _browser_fetch_binary(page, url: str) -> bytes | None:
    """
    Fetch a URL as binary (e.g. AES-128 key) inside the browser context.
    Returns raw bytes or None.
    """
    b64 = await page.evaluate(f"""
        async () => {{
            try {{
                const r = await fetch({repr(url)}, {{cache: "no-store"}});
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                // encode to base64 so we can return it as a string
                return btoa(String.fromCharCode(...new Uint8Array(buf)));
            }} catch(e) {{
                return null;
            }}
        }}
    """)
    if b64:
        import base64
        return base64.b64decode(b64)
    return None


def _is_m3u8_line(line: str) -> bool:
    """
    Detect sub-playlist lines. Handles:
      - classic .m3u8 extension
      - query-string-only variants (contains m3u8 anywhere)
    """
    lower = line.lower()
    return '.m3u8' in lower


def _rewrite_m3u8(content: str, base_url: str, key: str) -> str:
    parsed    = urlparse(base_url)
    base_host = f"{parsed.scheme}://{parsed.netloc}"
    base_path = os.path.dirname(parsed.path)

    def resolve(u: str) -> str:
        if u.startswith("http"): return u
        if u.startswith("//"):   return parsed.scheme + ":" + u
        if u.startswith("/"):    return base_host + u
        return f"{base_host}{base_path}/{u}"

    lines = []
    next_is_variant = False  # set True after #EXT-X-STREAM-INF

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#"):
            if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")):
                next_is_variant = True

            if 'URI="' in line:
                def _rewrite_uri(m):
                    uri      = m.group(1)
                    resolved = resolve(uri)
                    # AES key → key proxy, everything else with URI= → variant
                    if 'key' in uri.lower() and not uri.lower().endswith('.m3u8'):
                        return f'URI="{PROXY_HOST}/proxy?link={quote(key)}&_key={quote(resolved)}"'
                    return f'URI="{PROXY_HOST}/proxy?link={quote(key)}&_variant={quote(resolved)}"'
                line = re.sub(r'URI="(.*?)"', _rewrite_uri, line)

            lines.append(line)

        elif next_is_variant or ".m3u8" in line.lower():
            # Sub-playlist (variant / chunklist / mono.ts.m3u8)
            # Must go through browser so Origin/Referer/cookies are correct
            next_is_variant = False
            resolved = resolve(line)
            lines.append(f"{PROXY_HOST}/proxy?link={quote(key)}&_variant={quote(resolved)}")

        else:
            # Raw segment (.ts, .aac, .png, signed S3, etc.)
            # Player fetches this DIRECTLY from the CDN — no hop through us
            next_is_variant = False
            lines.append(resolve(line))

    return "\n".join(lines)

# =============================================================================
# SNIFFER
# =============================================================================

def _ensure_sniffer(embed_url):
    if embed_url in active_sniffers:
        return
    active_sniffers.add(embed_url)
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)


async def _sniff(embed_url):
    base_url = embed_url.split("#")[0].rstrip("?")
    target   = base_url + "#player=clappr#autoplay=true"
    logger.info(f"[SNIFF] Loading: {target}")

    found = {}

    pw      = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
        ]
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
    )

    async def on_response(resp):
        if found:
            return
        u = resp.url
        # Catch master playlists only — skip chunkists/audio variants here
        # (we'll fetch those on-demand through the browser later)
        if ".m3u8" in u and "chunklist" not in u and "audio" not in u.lower():
            logger.info(f"[NETWORK] {resp.status} {u}")
            try:
                body = await resp.text()
                if "#EXTM3U" in body:
                    logger.info(f"[SNIFF] ✅ Got playlist: {u}")
                    found['url']  = u
                    found['body'] = body
            except Exception as e:
                logger.warning(f"[SNIFF] Body read error: {e}")

    page = await context.new_page()
    page.on("response", on_response)

    try:
        await page.goto(target, timeout=20000, wait_until="domcontentloaded")
        try:
            btn = page.locator(".clappr-big-play-button")
            if await btn.is_visible():
                await btn.click(timeout=1000)
        except:
            pass
        for _ in range(15):
            if found:
                break
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"[SNIFF] Navigation error: {e}")

    if found:
        with cache_lock:
            stream_cache[embed_url] = {
                "url":           found['url'],
                "body":          found['body'],
                "body_ts":       time.time(),
                "last_accessed": time.time(),
                "page":          page,       # keep alive for browser-fetch refreshes
                "browser":       browser,
                "pw":            pw,
                "expires":       time.time() + 3600,
            }
        logger.info("[SNIFF] ✅ Cached with live page")
    else:
        logger.error("[SNIFF] ❌ No m3u8 found")
        await browser.close()
        await pw.stop()

    active_sniffers.discard(embed_url)

# =============================================================================
# CLEANUP
# =============================================================================

def _cleanup_loop():
    """
    Runs every 10s.
    Closes browsers for streams that are either:
      - expired (> 1hr old), OR
      - idle (no playlist request for IDLE_TIMEOUT seconds = user closed the stream)
    """
    while True:
        time.sleep(10)
        try:
            now = time.time()
            with cache_lock:
                to_evict = [
                    k for k, v in stream_cache.items()
                    if v['expires'] < now
                    or (now - v.get('last_accessed', now)) > IDLE_TIMEOUT
                ]
            for k in to_evict:
                with cache_lock:
                    entry = stream_cache.pop(k, None)
                if entry:
                    idle_secs = now - entry.get('last_accessed', now)
                    reason    = "expired" if entry['expires'] < now else f"idle {idle_secs:.0f}s"
                    async def _close(e=entry):
                        try:
                            if e.get('browser'): await e['browser'].close()
                            if e.get('pw'):      await e['pw'].stop()
                        except:
                            pass
                    asyncio.run_coroutine_threadsafe(_close(), _loop)
                    logger.info(f"[CLEANUP] Closed ({reason}): {k[:60]}")
        except Exception as e:
            logger.warning(f"[CLEANUP] Error: {e}")


if __name__ == '__main__':
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    logger.info(f"[SERVER] Starting on :{PORT}")
    app.run(host='0.0.0.0', port=PORT, threaded=True)
