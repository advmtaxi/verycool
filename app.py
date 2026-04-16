print("New App.py Loaded v6 - Auto FFmpeg install + /proxy = adaptive /fullres = original")
import subprocess
import sys
# ── Ensure dependencies ──────────────────────────────────────────────────────
subprocess.run([sys.executable, "-m", "pip", "install", "requests", "playwright", "curl_cffi"], check=True)
import asyncio
import threading
import time
import re
import os
import logging
import hashlib
import shutil
from curl_cffi import requests as req_lib
from flask import Flask, jsonify, Response, request, redirect, send_from_directory
from playwright.async_api import async_playwright
from urllib.parse import quote, urlparse
# ── Install Chromium + FFmpeg on startup ─────────────────────────────────────
print("[INIT] Installing Chromium...")
subprocess.run(["playwright", "install", "chromium"], check=True)
subprocess.run(["playwright", "install-deps", "chromium"], check=True)
print("[INIT] Chromium ready")

print("[INIT] Installing FFmpeg...")
subprocess.run(["apt-get", "update", "-qq"], check=True)
subprocess.run(["apt-get", "install", "-y", "ffmpeg"], check=True)
print("[INIT] FFmpeg ready ✅")
# ── Config ───────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 7860))
IDLE_TIMEOUT = 300
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)
app = Flask(__name__)
# ── Persistent asyncio event loop ────────────────────────────────────────────
_loop = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_start_loop, args=(_loop,), daemon=True).start()
# ── Storage ──────────────────────────────────────────────────────────────────
stream_cache = {}
cache_lock = threading.Lock()
active_sniffers = set()
transcoders = {}
transcode_base = "/tmp/hls_transcode"
os.makedirs(transcode_base, exist_ok=True)
transcoder_lock = threading.Lock()
# =============================================================================
# HELPERS
# =============================================================================
def get_proxy_host():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"

def _make_session(captured_headers: dict, captured_cookies: dict) -> req_lib.Session:
    s = req_lib.Session(impersonate="chrome124")
    KEEP = {"referer", "origin", "user-agent", "accept", "accept-language"}
    for k, v in captured_headers.items():
        if k.lower() in KEEP:
            s.headers[k] = v
    s.headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    s.headers.setdefault("Accept", "*/*")
    s.headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    s.cookies.update(captured_cookies)
    return s

def _fetch_text(embed_url: str, url: str) -> str | None:
    entry = _get_cached(embed_url)
    if not entry: return None
    try:
        r = entry["session"].get(url, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"[FETCH TEXT] {url[:80]} → {e}")
        return None

def _fetch_binary(embed_url: str, url: str) -> bytes | None:
    entry = _get_cached(embed_url)
    if not entry: return None
    try:
        r = entry["session"].get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.warning(f"[FETCH BINARY] {url[:80]} → {e}")
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
            stream_cache[key]["expires"] = time.time() + 10_800

def _get_fresh_master(embed_url: str, cached: dict) -> str | None:
    now = time.time()
    last = cached.get("body_ts", 0)
    if now - last < 2.0:
        return cached.get("body")
    body = _fetch_text(embed_url, cached["url"])
    if body and "#EXTM3U" in body:
        with cache_lock:
            if embed_url in stream_cache:
                stream_cache[embed_url]["body"] = body
                stream_cache[embed_url]["body_ts"] = now
        logger.info(f"[REFRESH] ✅ Master refreshed ({len(body)} bytes)")
        return body
    return cached.get("body")
# =============================================================================
# START TRANSCODER
# =============================================================================
def start_transcoder(embed_url: str):
    if embed_url in transcoders:
        return
    cached = _get_cached(embed_url)
    if not cached:
        return

    stream_id = hashlib.md5(embed_url.encode("utf-8")).hexdigest()[:16]
    output_dir = os.path.join(transcode_base, stream_id)
    os.makedirs(output_dir, exist_ok=True)

    session = cached["session"]
    header_list = [f"{k}: {v}" for k, v in session.headers.items() if k.lower() not in ("content-length", "host")]
    headers_arg = "\\r\\n".join(header_list) + "\\r\\n" if header_list else ""
    cookie_str = "; ".join(f"{name}={value}" for name, value in session.cookies.items())
    input_url = cached["url"]

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-headers", headers_arg,
    ]
    if cookie_str:
        ffmpeg_cmd += ["-cookies", cookie_str]

    ffmpeg_cmd += [
        "-i", input_url,
        "-filter_complex", "[0:v]split=4[v0][v1][v2][v3];"
                          "[v0]scale=w=1280:h=720:force_original_aspect_ratio=decrease[v720];"
                          "[v1]scale=w=854:h=480:force_original_aspect_ratio=decrease[v480];"
                          "[v2]scale=w=640:h=360:force_original_aspect_ratio=decrease[v360];"
                          "[v3]scale=w=256:h=144:force_original_aspect_ratio=decrease[v144]",
        "-map", "[v720]", "-c:v:0", "libx264", "-b:v:0", "2800k", "-maxrate:v:0", "3200k", "-bufsize:v:0", "6000k", "-preset", "veryfast", "-g", "60", "-keyint_min", "60",
        "-map", "[v480]", "-c:v:1", "libx264", "-b:v:1", "1400k", "-maxrate:v:1", "1600k", "-bufsize:v:1", "3000k", "-preset", "veryfast", "-g", "60", "-keyint_min", "60",
        "-map", "[v360]", "-c:v:2", "libx264", "-b:v:2", "800k",  "-maxrate:v:2", "900k",  "-bufsize:v:2", "1800k",  "-preset", "veryfast", "-g", "60", "-keyint_min", "60",
        "-map", "[v144]", "-c:v:3", "libx264", "-b:v:3", "250k",  "-maxrate:v:3", "300k",  "-bufsize:v:3", "600k",   "-preset", "veryfast", "-g", "60", "-keyint_min", "60",
        "-map", "0:a", "-c:a", "copy",
        "-f", "hls",
        "-hls_time", "6",
        "-hls_list_size", "8",
        "-hls_flags", "delete_segments+independent_segments+discont_start",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", "q%v/seg_%05d.ts",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", "v:0,a:0,name:720p v:1,a:0,name:480p v:2,a:0,name:360p v:3,a:0,name:144p",
        "q%v/index.m3u8"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        cwd=output_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with transcoder_lock:
        transcoders[embed_url] = {
            "process": proc,
            "output_dir": output_dir,
            "stream_id": stream_id,
            "started": time.time()
        }
    logger.info(f"[TRANSCODE] ✅ Started ABR for {embed_url[:60]}... (id={stream_id})")
# =============================================================================
# M3U8 REWRITER (for /fullres only)
# =============================================================================
def _rewrite_m3u8(content: str, base_url: str, embed_key: str, proxy_host: str) -> str:
    parsed = urlparse(base_url)
    base_host = f"{parsed.scheme}://{parsed.netloc}"
    base_path = os.path.dirname(parsed.path)
    def resolve(u: str) -> str:
        if u.startswith("http"): return u
        if u.startswith("//"): return parsed.scheme + ":" + u
        if u.startswith("/"): return base_host + u
        return f"{base_host}{base_path}/{u}"
    lines = []
    header_injected = False
    next_is_variant = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line: continue
        if line == "#EXTM3U" and not header_injected:
            lines.append(line)
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
            header_injected = True
            continue
        if line.startswith("#EXT-X-PLAYLIST-TYPE"):
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
            continue
        if line.startswith("#"):
            if line.startswith(("#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF")):
                next_is_variant = True
            if 'URI="' in line:
                def _rewrite_uri(m):
                    uri = m.group(1)
                    resolved = resolve(uri)
                    if "key" in uri.lower() and not uri.lower().endswith(".m3u8"):
                        return f'URI="{proxy_host}/fullres?link={quote(embed_key)}&_key={quote(resolved)}"'
                    return f'URI="{proxy_host}/fullres?link={quote(embed_key)}&_variant={quote(resolved)}"'
                line = re.sub(r'URI="(.*?)"', _rewrite_uri, line)
            lines.append(line)
        elif next_is_variant or ".m3u8" in line.lower():
            next_is_variant = False
            resolved = resolve(line)
            lines.append(f"{proxy_host}/fullres?link={quote(embed_key)}&_variant={quote(resolved)}")
        else:
            next_is_variant = False
            lines.append(resolve(line))
    return "\n".join(lines)
# =============================================================================
# CORS
# =============================================================================
@app.after_request
def after_request(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,Range"
    resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS,HEAD"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Length,Content-Range,Accept-Ranges"
    return resp
# =============================================================================
# ROUTES
# =============================================================================
@app.route("/")
def home():
    return jsonify({"status": "online", "cached": len(stream_cache), "transcoding": len(transcoders), "sniffing": len(active_sniffers)})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ── TRANSCODED ADAPTIVE (720p/480p/360p/144p) ───────────────────────────────
@app.route("/proxy")
def proxy():
    embed_url = request.args.get("link")
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for _ in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached: break
    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    if embed_url not in transcoders:
        start_transcoder(embed_url)

    stream_id = hashlib.md5(embed_url.encode("utf-8")).hexdigest()[:16]
    master_path = os.path.join(transcode_base, stream_id, "master.m3u8")

    for _ in range(20):
        if os.path.exists(master_path):
            break
        time.sleep(1)

    if not os.path.exists(master_path):
        return Response("Transcode still starting… retry in a few seconds", status=202)

    return redirect(f"{get_proxy_host()}/trans/{stream_id}/master.m3u8", code=302)

# ── ORIGINAL FULL RESOLUTION ────────────────────────────────────────────────
@app.route("/fullres")
def fullres():
    embed_url = request.args.get("link")
    if not embed_url:
        return jsonify({"error": "missing ?link="}), 400

    proxy_host = get_proxy_host()
    variant_url = request.args.get("_variant")
    key_url = request.args.get("_key")

    if key_url:
        key_bytes = _fetch_binary(embed_url, key_url)
        if key_bytes:
            _touch(embed_url)
            return Response(key_bytes, mimetype="application/octet-stream")
        return Response("Key fetch failed", status=503)

    if variant_url:
        _touch(embed_url)
        body = _fetch_text(embed_url, variant_url)
        if not body or "#EXTM3U" not in body:
            return Response("Variant fetch failed", status=503)
        rewritten = _rewrite_m3u8(body, variant_url, embed_url, proxy_host)
        return Response(rewritten, mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache, no-store"})

    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for i in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached: break
    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)

    _touch(embed_url)
    body = _get_fresh_master(embed_url, cached)
    if not body:
        return Response("No playlist body", status=503)

    rewritten = _rewrite_m3u8(body, cached["url"], embed_url, proxy_host)
    return Response(rewritten, mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache, no-store"})

# ── Serve transcoded files ───────────────────────────────────────────────────
@app.route("/trans/<stream_id>/<path:subpath>")
def serve_transcoded(stream_id: str, subpath: str):
    directory = os.path.join(transcode_base, stream_id)
    if not os.path.exists(directory):
        return Response("Transcode not ready yet", status=404)
    full_path = os.path.join(directory, subpath)
    if not os.path.exists(full_path):
        return Response("File not found", status=404)

    if subpath.endswith(".m3u8"):
        with open(full_path, "r") as f:
            content = f.read()
        return Response(content, mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache, no-store"})
    else:
        return send_from_directory(directory, subpath, mimetype="video/MP2T")

@app.route("/redirect")
def redirect_to_m3u8():
    embed_url = request.args.get("link")
    if not embed_url: return jsonify({"error": "missing ?link="}), 400
    cached = _get_cached(embed_url)
    if not cached:
        _ensure_sniffer(embed_url)
        for _ in range(50):
            time.sleep(0.5)
            cached = _get_cached(embed_url)
            if cached: break
    if not cached:
        return Response("Sniff timed out — retry in a few seconds", status=504)
    _touch(embed_url)
    return redirect(cached["url"], code=302)

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
# SNIFFER + CLEANUP (unchanged)
# =============================================================================
def _ensure_sniffer(embed_url: str):
    if embed_url in active_sniffers: return
    active_sniffers.add(embed_url)
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)

async def _sniff(embed_url: str):
    base_url = embed_url.split("#")[0].rstrip("?")
    target = base_url + "#player=clappr#autoplay=true"
    logger.info(f"[SNIFF] Loading: {target}")
    found = {}
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled","--mute-audio","--autoplay-policy=no-user-gesture-required","--disable-dev-shm-usage","--disable-gpu","--single-process","--disable-extensions","--disable-images","--blink-settings=imagesEnabled=false","--js-flags=--max-old-space-size=256"])
    context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 720})
    async def on_response(resp):
        if found: return
        u = resp.url
        if ".m3u8" in u and "chunklist" not in u and "audio" not in u.lower():
            logger.info(f"[NETWORK] {resp.status} {u}")
            try:
                body = await resp.text()
                if "#EXTM3U" in body:
                    req_headers = await resp.request.all_headers()
                    logger.info(f"[SNIFF] ✅ Playlist found: {u}")
                    found["url"] = u
                    found["body"] = body
                    found["headers"] = req_headers
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
        except Exception: pass
        for _ in range(15):
            if found: break
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"[SNIFF] Navigation error: {e}")
    if found:
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        session = _make_session(found.get("headers", {}), cookie_dict)
        with cache_lock:
            stream_cache[embed_url] = {"url": found["url"], "body": found["body"], "body_ts": time.time(), "last_accessed": time.time(), "session": session, "expires": time.time() + 10800}
        logger.info("[SNIFF] ✅ Session cached.")
    else:
        logger.error("[SNIFF] ❌ No m3u8 found")
    try:
        await browser.close()
        await pw.stop()
    except Exception: pass
    active_sniffers.discard(embed_url)

def _cleanup_loop():
    while True:
        time.sleep(30)
        try:
            now = time.time()
            with cache_lock:
                to_evict = [k for k, v in stream_cache.items() if v["expires"] < now or (now - v.get("last_accessed", now)) > IDLE_TIMEOUT]
            for k in to_evict:
                with cache_lock:
                    entry = stream_cache.pop(k, None)
                if entry:
                    try:
                        entry["session"].close()
                    except Exception: pass
                    if k in transcoders:
                        try:
                            transcoders[k]["process"].kill()
                            shutil.rmtree(transcoders[k]["output_dir"], ignore_errors=True)
                        except: pass
                        with transcoder_lock:
                            transcoders.pop(k, None)
                    logger.info(f"[CLEANUP] Evicted: {k[:70]}")
        except Exception as e:
            logger.warning(f"[CLEANUP] Error: {e}")

# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    logger.info(f"[SERVER] Starting on :{PORT} | /proxy = adaptive (720/480/360/144p) | /fullres = original")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
