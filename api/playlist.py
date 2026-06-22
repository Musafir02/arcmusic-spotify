from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import urllib.error
import re
import time

PARTNER_API = "https://api-partner.spotify.com/pathfinder/v1/query"
FALLBACK_HASH = "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4"

_hash_cache = {"hash": FALLBACK_HASH, "expires": 0}
_rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10
HASH_CACHE_TTL = 86400

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
            embed_data = self._get_embed_data(playlist_id)
            if not embed_data or not embed_data.get("token"):
                self._json_response(500, {"error": "Could not obtain access token"})
                return

            graphql_hash = self._get_graphql_hash()
            if not graphql_hash:
                self._json_response(500, {"error": "Could not resolve GraphQL hash"})
                return

            access_token = embed_data["token"]
            playlist_name = embed_data.get("name", "")
            owner = embed_data.get("owner", "")

            tracks = []
            offset = 0
            limit = 400
            total = None

            while True:
                result = self._graphql_fetch(playlist_id, access_token, graphql_hash, offset, limit)

                if result is None:
                    _hash_cache["hash"] = None
                    _hash_cache["expires"] = 0
                    graphql_hash = self._get_graphql_hash()
                    if graphql_hash:
                        result = self._graphql_fetch(playlist_id, access_token, graphql_hash, offset, limit)

                if not result:
                    break

                if total is None:
                    total = result.get("totalCount", 0)

                items = result.get("items", [])
                if not items:
                    break

                for item in items:
                    track_data = item.get("itemV2", {}).get("data", {})
                    if not track_data or not track_data.get("name"):
                        continue

                    artists_items = track_data.get("artists", {}).get("items", [])
                    artists = [a.get("profile", {}).get("name", "") for a in artists_items if a.get("profile", {}).get("name")]

                    album_data = track_data.get("albumOfTrack", {})
                    album_name = album_data.get("name") if album_data else None

                    duration = track_data.get("trackDuration", {}).get("totalMilliseconds")

                    tracks.append({
                        "name": track_data["name"],
                        "artists": artists,
                        "duration_ms": duration,
                        "album": album_name,
                    })

                offset += len(items)
                if total and offset >= total:
                    break

            if not tracks:
                self._json_response(404, {"error": "Could not fetch playlist tracks"})
                return

            self._json_response(200, {
                "name": playlist_name,
                "owner": owner,
                "track_count": len(tracks),
                "tracks": tracks,
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _get_graphql_hash(self):
        now = time.time()
        if _hash_cache["hash"] and _hash_cache["expires"] > now:
            return _hash_cache["hash"]

        try:
            page_req = urllib.request.Request("https://open.spotify.com", headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            })
            with urllib.request.urlopen(page_req, timeout=10) as resp:
                page_html = resp.read().decode("utf-8")

            js_match = re.search(r'(https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[a-f0-9]+\.js)', page_html)
            if not js_match:
                return _hash_cache["hash"] or FALLBACK_HASH

            js_url = js_match.group(1)
            js_req = urllib.request.Request(js_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(js_req, timeout=45) as resp:
                js_content = resp.read().decode("utf-8")

            hash_match = re.search(r'"fetchPlaylistContents","query","([a-f0-9]{64})"', js_content)
            if not hash_match:
                hash_match = re.search(r'fetchPlaylist\w*","query","([a-f0-9]{64})"', js_content)

            if hash_match:
                _hash_cache["hash"] = hash_match.group(1)
                _hash_cache["expires"] = now + HASH_CACHE_TTL
                return _hash_cache["hash"]

        except Exception:
            pass

        return _hash_cache["hash"] or FALLBACK_HASH

    def _get_embed_data(self, playlist_id):
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        req = urllib.request.Request(embed_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")

            token_match = re.search(r'"accessToken":"([^"]+)"', html)
            if not token_match:
                return None

            result = {"token": token_match.group(1)}

            nd_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if nd_match:
                try:
                    nd = json.loads(nd_match.group(1))
                    entity = nd.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                    result["name"] = entity.get("name", "")
                    result["owner"] = entity.get("subtitle", "")
                except Exception:
                    pass

            return result
        except Exception:
            return None

    def _graphql_fetch(self, playlist_id, token, graphql_hash, offset=0, limit=400):
        variables = json.dumps({
            "uri": f"spotify:playlist:{playlist_id}",
            "offset": offset,
            "limit": limit,
        })
        extensions = json.dumps({
            "persistedQuery": {
                "version": 1,
                "sha256Hash": graphql_hash,
            }
        })

        params = urllib.parse.urlencode({
            "operationName": "fetchPlaylistContents",
            "variables": variables,
            "extensions": extensions,
        })

        url = f"{PARTNER_API}?{params}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "app-platform": "WebPlayer",
        })

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if "errors" in data:
                for err in data["errors"]:
                    if "PersistedQueryNotFound" in err.get("message", ""):
                        return None
                return None

            return data.get("data", {}).get("playlistV2", {}).get("content", {})
        except Exception:
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
