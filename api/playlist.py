from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
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
                "name": result.get("name", ""),
                "owner": result.get("owner", ""),
                "track_count": len(result["tracks"]),
                "tracks": result["tracks"],
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _fetch_playlist(self, playlist_id):
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        req = urllib.request.Request(embed_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")
        except Exception:
            return self._fallback_scraper(playlist_id)

        token_match = re.search(r'"accessToken":"([^"]+)"', html)
        next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)

        tracks = []
        playlist_name = ""
        owner = ""
        total_tracks = 0

        if next_data_match:
            try:
                nd = json.loads(next_data_match.group(1))
                entity = nd.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                playlist_name = entity.get("name", "")
                owner = entity.get("subtitle", "")

                for t in entity.get("trackList", []):
                    artists_raw = t.get("subtitle", "")
                    artists = [a.strip() for a in artists_raw.replace("\u00b7", ",").split(",")] if artists_raw else []
                    tracks.append({
                        "name": t.get("title", ""),
                        "artists": artists,
                        "duration_ms": t.get("duration"),
                        "album": None,
                    })
            except Exception:
                pass

        if token_match:
            access_token = token_match.group(1)

            try:
                meta_url = f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name,owner(display_name),tracks(total)"
                meta_req = urllib.request.Request(meta_url, headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "Mozilla/5.0"
                })
                with urllib.request.urlopen(meta_req, timeout=10) as resp:
                    meta = json.loads(resp.read().decode("utf-8"))
                    if not playlist_name:
                        playlist_name = meta.get("name", "")
                    total_tracks = meta.get("tracks", {}).get("total", 0)
                    o = meta.get("owner", {})
                    if o and not owner:
                        owner = o.get("display_name", "")
            except Exception:
                total_tracks = len(tracks)

            if total_tracks > len(tracks):
                offset = len(tracks)
                limit = 100
                while offset < total_tracks:
                    try:
                        api_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?offset={offset}&limit={limit}"
                        api_req = urllib.request.Request(api_url, headers={
                            "Authorization": f"Bearer {access_token}",
                            "User-Agent": "Mozilla/5.0"
                        })
                        with urllib.request.urlopen(api_req, timeout=15) as resp:
                            data = json.loads(resp.read().decode("utf-8"))

                        items = data.get("items", [])
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
                        if not data.get("next"):
                            break
                    except Exception:
                        break

        if not tracks:
            return self._fallback_scraper(playlist_id)

        return {"name": playlist_name, "owner": owner, "tracks": tracks}

    def _fallback_scraper(self, playlist_id):
        try:
            from spotify_scraper import SpotifyClient
            with SpotifyClient() as client:
                clean_url = f"https://open.spotify.com/playlist/{playlist_id}"
                playlist = client.get_playlist(clean_url)

                tracks = []
                for entry in playlist.tracks:
                    t = entry.track
                    artists = [a.name for a in t.artists] if t.artists else []
                    tracks.append({
                        "name": t.name,
                        "artists": artists,
                        "duration_ms": t.duration_ms,
                        "album": t.album.name if t.album else None,
                    })

                playlist_name = playlist.name or ""
                owner_name = ""
                if playlist.owner:
                    owner_name = playlist.owner.get("name", "") if isinstance(playlist.owner, dict) else str(playlist.owner)

                return {"name": playlist_name, "owner": owner_name, "tracks": tracks}
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
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
