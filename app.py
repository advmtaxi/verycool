import subprocess
import sys
import os
import asyncio
import threading
import time
import re
import logging
from urllib.parse import quote, urlparse, urljoin
from flask import Flask, jsonify, Response, request, redirect

# Ensure dependencies are installed
try:
    import requests
    from curl_cffi import requests as req_lib
    from playwright.async_api import async_playwright
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "requests", "playwright", "curl_cffi"], check=True)
    from curl_cffi import requests as req_lib
    from playwright.async_api import async_playwright

# Configuration
PORT = int(os.environ.get("PORT", 7860))
IDLE_TIMEOUT = 300
CACHE_EXPIRY = 10800

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global state
stream_cache = {}
cache_lock = threading.Lock()
active_sniffers = set()

# Async loop for Playwright
_loop = asyncio.new_event_loop()
def _start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
threading.Thread(target=_start_loop, args=(_loop,), daemon=True).start()

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
    s.cookies.update(captured_cookies)
    return s

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
            stream_cache[key]["expires"] = time.time() + CACHE_EXPIRY

def _rewrite_playlist(content: str, base_url: str, embed_key: str, proxy_host: str) -> str:
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line: continue
        
        if line.startswith("#"):
            # Handle URI in tags like #EXT-X-KEY or #EXT-X-MAP
            if 'URI="' in line:
                def _repl(m):
                    uri = m.group(1)
                    resolved = urljoin(base_url, uri)
                    if ".m3u8" in resolved.lower():
                        return f'URI="{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolved)}"'
                    return f'URI="{resolved}"'
                line = re.sub(r'URI="(.*?)"', _repl, line)
            lines.append(line)
        else:
            resolved = urljoin(base_url, line)
            if ".m3u8" in resolved.lower():
                lines.append(f"{proxy_host}/proxy?link={quote(embed_key)}&_url={quote(resolved)}")
            else:
                lines.append(resolved)
    return "\n".join(lines)

@app.route("/")
def home():
    return jsonify({"status": "online", "cached": len(stream_cache)})

@app.route("/proxy")
def proxy():
    embed_url = request.args.get("link")
    target_url = request.args.get("_url")
    
    if not embed_url:
        return "Missing link", 400
    
    # If no _url, it means we are starting from the master M3U8
    entry = _get_cached(embed_url)
    if not entry:
        _ensure_sniffer(embed_url)
        # Increase wait loop to 120 * 0.5 = 60 seconds
        for i in range(120):
            time.sleep(0.5)
            entry = _get_cached(embed_url)
            if entry: 
                logger.info(f"[PROXY] Sniff finished after {i*0.5}s")
                break
            
    if not entry:
        logger.error(f"[PROXY] Sniff TIMEOUT for {embed_url}")
        return "Sniff timeout", 504
    
    _touch(embed_url)
    
    url_to_fetch = target_url if target_url else entry["url"]
    
    try:
        # Use the captured session to fetch the M3U8
        parsed_embed = urlparse(embed_url)
        origin = f"{parsed_embed.scheme}://{parsed_embed.netloc}"
        headers = {"Referer": embed_url, "Origin": origin}
        
        r = entry["session"].get(url_to_fetch, headers=headers, timeout=15)
        r.raise_for_status()
        
        content = r.text
        # Check if it's a playlist
        if "#EXTM3U" in content:
            rewritten = _rewrite_playlist(content, url_to_fetch, embed_url, get_proxy_host())
            return Response(rewritten, mimetype="application/vnd.apple.mpegurl")
        else:
            # If it's not a playlist (maybe a segment somehow reached here), just return it
            return Response(r.content, mimetype=r.headers.get("Content-Type"))
            
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return f"Error: {e}", 500

def _ensure_sniffer(embed_url: str):
    if embed_url in active_sniffers: return
    active_sniffers.add(embed_url)
    asyncio.run_coroutine_threadsafe(_sniff(embed_url), _loop)

async def _sniff(embed_url: str):
    # Keep the hash fragments as requested
    target = embed_url
    if "#" not in target:
        target += "#player=clappr#autoplay=true"
    elif "player=clappr" not in target:
        target += "#player=clappr#autoplay=true"
        
    logger.info(f"[SNIFF] Loading: {target}")
    found = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        
        async def on_response(resp):
            if found: return
            u = resp.url
            if ".m3u8" in u and "chunklist" not in u and "audio" not in u.lower():
                try:
                    text = await resp.text()
                    if "#EXTM3U" in text:
                        found["url"] = u
                        found["headers"] = await resp.request.all_headers()
                        logger.info(f"[SNIFF] Found M3U8: {u}")
                except: pass

        page = await context.new_page()
        page.on("response", on_response)
        
        try:
            # Using 'domcontentloaded' is much faster than 'networkidle'
            await page.goto(target, wait_until="domcontentloaded", timeout=20000)
            
            # Try to click play button immediately if it appears
            try:
                play_btn = page.locator(".clappr-big-play-button")
                if await play_btn.is_visible():
                    await play_btn.click(timeout=1000)
            except: pass
            
            # Rapidly check for the M3U8 for up to 15 seconds
            for _ in range(60):
                if found: break
                await asyncio.sleep(0.25)
        except Exception as e:
            logger.error(f"[SNIFF] Page error: {e}")

        if found:
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            session = _make_session(found["headers"], cookie_dict)
            with cache_lock:
                stream_cache[embed_url] = {
                    "url": found["url"],
                    "session": session,
                    "expires": time.time() + CACHE_EXPIRY,
                    "last_accessed": time.time()
                }
        
        await browser.close()
    active_sniffers.discard(embed_url)

if __name__ == "__main__":
    logger.info(f"Starting server on {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
