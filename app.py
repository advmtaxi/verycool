print("New App.py Loaded v3.")
import subprocess, sys

# ── Ensure dependencies are installed ───────────────────────────────────────
subprocess.run([sys.executable, "-m", "pip", "install", "requests", "playwright"], check=True)

import requests as req_lib
# ... rest of imports
import asyncio
import threading
import time
import re
import os
import logging
import subprocess
import base64
import requests as req_lib
from flask import Flask, jsonify, Response, request, redirect
from playwright.async_api import async_playwright
from urllib.parse import quote, urlparse

# ── Install Chromium on startup ──────────────────────────────────────────────
print("[INIT] Installing Chromium...")
subprocess.run(["playwright", "install", "chromium"], check=True)
subprocess.run(["playwright", "install-deps", "chromium"], check=True)
print("[INIT] Chromium ready")

# ── Config ───────────────────────────────────────────────────────────────────
PORT         = int(os.environ.get("PORT", 7860))
IDLE_TIMEOUT = 300   # 5 min idle before evicting a session (no playlist request)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Persistent asyncio event loop (for Playwright sniffs) ────────────────────
_loop = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_start_loop, args=(_loop,), daemon=True).start()

def run_async(coro, timeout=30):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)

# ── Storage ──────────────────────────────────────────────────────────────────
stream_cache    = {}   # embed_url → entry dict
cache_lock      = threading.Lock()
active_sniffers = set()

# =============================================================================
# HELPERS — dynamic host + requests session
# =============================================================================

def get_proxy_host():
    """
    Auto-detect our own public base URL from incoming request headers.
    Works on HuggingFace Spaces, Railway, Render, bare VPS — no hard-coded URL.
    """
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host   = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"


def _make_session(captured_headers: dict, captured_cookies: dict) -> req_lib.Session:
    """
    Build a requests.Session that mimics the browser that found the m3u8.
    Only pass headers the CDN cares about — don't leak internal browser internals.
    """
    s = req_lib.Session()

    # Headers we want to preserve from the original browser request
    KEEP = {"referer", "origin", "user-agent", "accept", "accept-language", "accept-encoding"}
    for k, v in captured_headers.items():
        if k.lower() in KEEP:
            s.headers[k] = v

    # Sane defaults if the browser request didn't include them
    s.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    s.headers.setdefault("Accept", "*/*")
    s.headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    s.headers.setdefault("Accept-Encoding", "gzip, deflate, br")
    s.cookies.update(captured_cookies)
    return s


def _fetch_text(embed_url: str, url: str) -> str | None:
    """Fetch URL as text using the session captured during sniff."""
    entry = _get_cached(embed_url)
    if not entry:
        return None
    try:
        r = entry["session"].get(url, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"[FETCH TEXT] {url[:80]} → {e}")
        return None


def _fetch_binary(embed_url: str, url: str) -> bytes | None:
    """Fetch URL as bytes (e.g. AES-128 key) using the captured session."""
    entry = _get_cached(embed_url)
    if not entry:
        return None
    try:
        r = entry["session"].get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.warning(f"[FETCH BINARY] {url[:80]} → {e}")
        return None


def _segments_alive(embed_url: str, playlist_body: str, base_url: str) -> bool:
    """
    HEAD-check the first .ts segment in a chunklist.
    Returns False if the segment is gone (expired CDN link), True otherwise.
    """
    entry = _get_cached(embed_url)
    if not entry:
        return False

    parsed    = urlparse(base_url)
    base_host = f"{parsed.scheme}://{parsed.netloc}"
    base_path = os.path.dirname(parsed.path)

    def resolve(u):
        if u.startswith("http"): return u
        if u.startswith("//"):   return parsed.scheme + ":" + u
        if u.startswith("/"):    return base_host + u
        return f"{base_host}{base_path}/{u}"

    for line in playlist_body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # First non-comment line is a segment
        seg_url = resolve(line)
        try:
            r = entry["session"].head(seg_url, timeout=5, allow_redirects=True)
            alive = r.status_code < 400
            logger.info(f"[SEGMENTS] HEAD {r.status_code} → {'alive' if alive else 'EXPIRED'}: {seg_url[:80]}")
            return alive
        except Exception as e:
            logger.warning(f"[SEGMENTS] HEAD error: {e}")
            return False

    return True  # no segments found → assume alive (master playlist)


def _get_cached(key: str) -> dict | None:
    with cache_lock:
        c = stream_cache.get(key)
        if c and time.time() < c["expires"]:
            return c
    return None


def _touch(key: str):
    """
    Mark the stream as recently accessed.
    Extends expiry by 3 h from now so long matches never auto-evict while active.
    """
    with cache_lock:
        if key in stream_cache:
            stream_cache[key]["last_accessed"] = time.time()
            stream_cache[key]["expires"]        = time.time() + 10_800  # 3 h rolling


def _get_fresh_master(embed_url: str, cached: dict) -> str | None:
    """
    Re-fetch the master playlist via requests. Rate-limited to once per 2 s.
    Falls back to cached body if fetch fails.
    """
    now  = time.time()
    last = cached.get("body_ts", 0)

    if now - last < 2.0:
        return cached.get("body")

    body = _fetch_text(embed_url, cached["url"])
    if body and "#EXTM3U" in body:
        with cache_lock:
            if embed_url in stream_cache:
                stream_cache[embed_url]["body"]    = body
                stream_cache[embed_url]["body_ts"] = now
        logger.info(f"[REFRESH] ✅ Master refreshed ({len(body)} bytes)")
        return body

    logger.warning("[REFRESH] Using cached master (fresh fetch failed)")
    return cached.get("body")

# =============================================================================
# M3U8 REWRITER
# =============================================================================

def _rewrite_m3u8(content: str, base_url: str, embed_key: str, proxy_host: str) -> str:
    """
    Rewrite a playlist so that:
      - Sub-playlists (variant / chunklist) are routed through our /proxy
        so the CDN gets the right Origin/Referer/cookies.
      - AES-128 keys are routed through /proxy?_key=... for the same reason.
      - Raw .ts segments are left as absolute CDN URLs — player fetches them directly.
      - #EXT-X-PLAYLIST-TYPE:EVENT is injected so players keep all segments
        in their buffer (enables rewind) but still start at the live edge.
    """
    parsed    = urlparse(base_url)
    base_host = f"{parsed.scheme}://{parsed.netloc}"
    base_path = os.path.dirname(parsed.path)

    def resolve(u: str) -> str:
        if u.startswith("http"): return u
        if u.startswith("//"):   return parsed.scheme + ":" + u
        if u.startswith("/"):    return base_host + u
        return f"{base_host}{base_path}/{u}"

    lines               = []
    header_injected     = False
    next_is_variant     = False

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue

        # ── After #EXTM3U inject EVENT type so players buffer all segments ──
        if line == "#EXTM3U" and not header_injected:
            lines.append(line)
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
            header_injected = True
            continue

        # Override any LIVE/VOD type the source sends — we always want EVENT
        if line.startswith("#EXT-X-PLAYLIST-TYPE"):
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
            continue

        if line.startswith("#"):
            # Flag that the next URI line is a variant stream
            if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")):
                next_is_variant = True

            # Rewrite URI="..." inside tags (keys, alternate renditions, etc.)
            if 'URI="' in line:
                def _rewrite_uri(m):
                    uri      = m.group(1)
                    resolved = resolve(uri)
                    # AES key (contains "key" but not a sub-playlist)
                    if "key" in uri.lower() and not uri.lower().endswith(".m3u8"):
                        return f'URI="{proxy_host}/proxy?link={quote(embed_key)}&_key={quote(resolved)}"'
                    # Sub-playlist referenced inline
                    return f'URI="{proxy_host}/proxy?link={quote(embed_key)}&_variant={quote(resolved)}"'
                line = re.sub(r'URI="(.*?)"', _rewrite_uri, line)

            lines.append(line)

        elif next_is_variant or ".m3u8" in line.lower():
            # Variant / chunklist → must go through our proxy
            next_is_variant = False
            resolved = resolve(line)
            lines.append(f"{proxy_host}/proxy?link={quote(embed_key)}&_variant={quote(resolved)}")

        else:
            # Raw segment (.ts, .aac, signed S3, etc.) → absolute CDN URL, player fetches directly
            next_is_variant = False
            lines.append(resolve(line))

    return "\n".join(lines)

# =============================================================================
# CORS
# =============================================================================

@app.after_request
def after_request(resp):
    resp.headers["Access-Control-Allow-Origin"]   = "*"
    resp.headers["Access-Control-Allow-Headers"]  = "Content-Type,Authorization,Range"
    resp.headers["Access-Control-Allow-Methods"]  = "GET,OPTIONS,HEAD"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Length,Content-Range,Accept-Ranges"
    return resp

# =============================================================================
# ROUTES
# =============================================================================

@app.route("/")
def home():
    return jsonify({
        "status":   "online",
        "cached":   len(stream_cache),
        "sniffing": len(active_sniffers),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/redirect")
def redirect_to_m3u8():
    """302-redirect straight to the raw CDN m3u8 (no rewriting)."""
    embed_url = request.args.get("link")
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for _ in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached:
                break

    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    return redirect(cached["url"], code=302)


@app.route("/proxy")
def proxy():
    embed_url = request.args.get("link")
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    proxy_host  = get_proxy_host()
    variant_url = request.args.get("_variant")
    key_url     = request.args.get("_key")

    # ── AES-128 encryption key ────────────────────────────────────────────────
    if key_url:
        key_bytes = _fetch_binary(embed_url, key_url)
        if key_bytes:
            _touch(embed_url)
            return Response(key_bytes, mimetype="application/octet-stream")
        return Response("Key fetch failed", status=503)

    # ── Variant / chunklist playlist ─────────────────────────────────────────
    if variant_url:
        _touch(embed_url)
        body = _fetch_text(embed_url, variant_url)

        if not body or "#EXTM3U" not in body:
            logger.warning(f"[VARIANT] Bad/empty body for {variant_url[:80]}")
            return Response("Variant fetch failed", status=503)

        # Check if segments are still alive on the CDN
        if not _segments_alive(embed_url, body, variant_url):
            logger.warning(f"[VARIANT] Segments expired — triggering re-sniff")
            # Evict the stale session and kick off a fresh sniff
            with cache_lock:
                stream_cache.pop(embed_url, None)
            _ensure_sniffer(embed_url)
            return Response(
                "Stream segments have expired. The source URL has rotated — "
                "please reload the player in a few seconds.",
                status=410,
            )

        rewritten = _rewrite_m3u8(body, variant_url, embed_url, proxy_host)
        return Response(
            rewritten,
            mimetype="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache, no-store"},
        )

    # ── Master playlist ───────────────────────────────────────────────────────
    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for i in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached:
                logger.info(f"[PROXY] Ready after {i * 0.5:.1f}s")
                break

    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    body = _get_fresh_master(embed_url, cached)
    if not body:
        return Response("No playlist body", status=503)

    rewritten = _rewrite_m3u8(body, cached["url"], embed_url, proxy_host)
    return Response(
        rewritten,
        mimetype="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.route("/extract")
def extract():
    embed_url = request.args.get("url")
    if not embed_url:
        return jsonify({"success": False, "error": "missing ?url="}), 400

    cached = _get_cached(embed_url)
    if cached:
        return jsonify({"success": True, "m3u8": cached["url"]})

    _ensure_sniffer(embed_url)
    for _ in range(50):
        time.sleep(0.5)
        cached = _get_cached(embed_url)
        if cached:
            return jsonify({"success": True, "m3u8": cached["url"]})

    return jsonify({"success": False, "error": "timeout"}), 202

# =============================================================================
# SNIFFER  — Chromium is used ONLY to find the m3u8 URL + capture headers/cookies.
#            The browser is closed immediately after. All subsequent fetches
#            go through a plain requests.Session — no browser stays alive.
# =============================================================================

def _ensure_sniffer(embed_url: str):
    if embed_url in active_sniffers:
        return
    active_sniffers.add(embed_url)
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)


async def _sniff(embed_url: str):
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
            "--disable-extensions",
            "--disable-images",
            "--blink-settings=imagesEnabled=false",
            "--js-flags=--max-old-space-size=256",
        ],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
    )

    async def on_response(resp):
        if found:
            return
        u = resp.url
        # Grab master playlists only — skip chunklists / audio variants
        if ".m3u8" in u and "chunklist" not in u and "audio" not in u.lower():
            logger.info(f"[NETWORK] {resp.status} {u}")
            try:
                body = await resp.text()
                if "#EXTM3U" in body:
                    # Capture the exact request headers the browser sent
                    req_headers = await resp.request.all_headers()
                    logger.info(f"[SNIFF] ✅ Playlist found: {u}")
                    found["url"]     = u
                    found["body"]    = body
                    found["headers"] = req_headers
            except Exception as e:
                logger.warning(f"[SNIFF] Body read error: {e}")

    page = await context.new_page()
    page.on("response", on_response)

    try:
        await page.goto(target, timeout=20_000, wait_until="domcontentloaded")
        # Try to click play button if it appears
        try:
            btn = page.locator(".clappr-big-play-button")
            if await btn.is_visible():
                await btn.click(timeout=1_000)
        except Exception:
            pass
        # Wait up to 15 s for the network intercept to fire
        for _ in range(15):
            if found:
                break
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"[SNIFF] Navigation error: {e}")

    if found:
        # Grab browser cookies before we close everything
        cookies     = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        session = _make_session(found.get("headers", {}), cookie_dict)

        with cache_lock:
            stream_cache[embed_url] = {
                "url":           found["url"],
                "body":          found["body"],
                "body_ts":       time.time(),
                "last_accessed": time.time(),
                "session":       session,
                # Rolling 3-hour expiry — extended by _touch() on each playlist request
                "expires":       time.time() + 10_800,
            }
        logger.info("[SNIFF] ✅ Session cached. Closing browser now.")
    else:
        logger.error("[SNIFF] ❌ No m3u8 found within timeout")

    # ── Always close Chromium — we don't need it anymore ──────────────────────
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass

    active_sniffers.discard(embed_url)

# =============================================================================
# CLEANUP — evict idle/expired sessions (no browsers to close, just dicts)
# =============================================================================

def _cleanup_loop():
    """Runs every 30 s. Evicts sessions that are expired or have gone idle."""
    while True:
        time.sleep(30)
        try:
            now = time.time()
            with cache_lock:
                to_evict = [
                    k for k, v in stream_cache.items()
                    if v["expires"] < now
                    or (now - v.get("last_accessed", now)) > IDLE_TIMEOUT
                ]
            for k in to_evict:
                with cache_lock:
                    entry = stream_cache.pop(k, None)
                if entry:
                    idle_s = now - entry.get("last_accessed", now)
                    reason = (
                        "expired"
                        if entry["expires"] < now
                        else f"idle {idle_s:.0f}s"
                    )
                    # Close the requests session cleanly
                    try:
                        entry["session"].close()
                    except Exception:
                        pass
                    logger.info(f"[CLEANUP] Evicted ({reason}): {k[:70]}")
        except Exception as e:
            logger.warning(f"[CLEANUP] Error: {e}")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    logger.info(f"[SERVER] Starting on :{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
