from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import urllib.error
import re
import time

_rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        client_ip = self.headers.get("X-Forwarded-For", self.client_address[0])
        if isinstance(client_ip, str) and "," in client_ip:
            client_ip = client_ip.split(",")[0].strip()

        if not self._check_rate_limit(client_ip):
            self._json_response(429, {"error": "Rate limit exceeded. Try again later."})
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        raw_url = params.get("url", [None])[0]

        if not raw_url:
            self._json_response(400, {"error": "Missing 'url' parameter"})
            return

        playlist_id = self._extract_id(raw_url)
        if not playlist_id:
            self._json_response(400, {"error": "Could not extract playlist ID from URL"})
            return

        if not re.match(r'^[a-zA-Z0-9_-]+$', playlist_id):
            self._json_response(400, {"error": "Invalid playlist ID format"})
            return

        try:
            result = self._fetch_playlist(playlist_id)
            if not result or not result.get("tracks"):
                self._json_response(404, {"error": "Could not fetch playlist tracks"})
                return

            self._json_response(200, {
                "name": result["name"],
                "owner": result["owner"],
                "track_count": len(result["tracks"]),
                "tracks": result["tracks"],
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _fetch_playlist(self, playlist_id):
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        req = urllib.request.Request(embed_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        })

        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")

        token_match = re.search(r'"accessToken":"([^"]+)"', html)
        if not token_match:
            return None

        access_token = token_match.group(1)

        tracks = []
        playlist_name = ""
        owner = ""

        nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd_match:
            try:
                nd = json.loads(nd_match.group(1))
                entity = nd.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                playlist_name = entity.get("name", "")
                owner = entity.get("subtitle", "")

                for t in entity.get("trackList", []):
                    sub = t.get("subtitle", "")
                    artists = [a.strip() for a in sub.replace("\u00b7", ",").split(",")] if sub else []
                    tracks.append({
                        "name": t.get("title", ""),
                        "artists": artists,
                        "duration_ms": t.get("duration"),
                        "album": None,
                    })
            except Exception:
                pass

        if not tracks:
            return None

        offset = len(tracks)
        limit = 100
        max_retries = 2

        while True:
            page_data = self._api_get_with_retry(
                f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?offset={offset}&limit={limit}",
                access_token,
                max_retries,
            )
            if not page_data:
                break

            items = page_data.get("items", [])
            if not items:
                break

            for item in items:
                t = item.get("track")
                if not t or not t.get("name"):
                    continue
                artists = [a["name"] for a in t.get("artists", []) if a.get("name")]
                tracks.append({
                    "name": t["name"],
                    "artists": artists,
                    "duration_ms": t.get("duration_ms"),
                    "album": t.get("album", {}).get("name") if t.get("album") else None,
                })

            offset += limit
            if not page_data.get("next"):
                break

        return {"name": playlist_name, "owner": owner, "tracks": tracks}

    def _api_get_with_retry(self, url, token, max_retries=2):
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    retry_after = int(e.headers.get("Retry-After", "2"))
                    time.sleep(min(retry_after, 4))
                    continue
                return None
            except Exception:
                return None
        return None

    def _extract_id(self, url):
        match = re.search(r'playlist[/:]([a-zA-Z0-9_-]+)', url)
        return match.group(1) if match else None

    def _check_rate_limit(self, client_ip):
        now = time.time()
        if client_ip in _rate_limit_store:
            window_start, count = _rate_limit_store[client_ip]
            if now - window_start < RATE_LIMIT_WINDOW:
                if count >= RATE_LIMIT_MAX:
                    return False
                _rate_limit_store[client_ip] = (window_start, count + 1)
            else:
                _rate_limit_store[client_ip] = (now, 1)
        else:
            _rate_limit_store[client_ip] = (now, 1)
        return True

    def _json_response(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
