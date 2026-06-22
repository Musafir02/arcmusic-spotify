from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import re
import time

_rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 10

EMBED_URL = "https://open.spotify.com/embed/playlist/{}"
PLAYLIST_API = "https://api-partner.spotify.com/pathfinder/v1/query"

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
            tracks = []
            playlist_name = ""
            owner_name = ""

            embed_result = self._fetch_via_embed(playlist_id)
            if embed_result and embed_result.get("tracks"):
                playlist_name = embed_result.get("name", "")
                tracks = embed_result.get("tracks", [])
            else:
                try:
                    from spotify_scraper import SpotifyClient
                    with SpotifyClient() as client:
                        clean_url = f"https://open.spotify.com/playlist/{playlist_id}"
                        playlist = client.get_playlist(clean_url)

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
                        if playlist.owner:
                            owner_name = playlist.owner.get("name", "") if isinstance(playlist.owner, dict) else str(playlist.owner)

                except Exception:
                    pass

            if not tracks:
                self._json_response(404, {"error": "Could not fetch playlist tracks"})
                return

            self._json_response(200, {
                "name": playlist_name,
                "owner": owner_name,
                "track_count": len(tracks),
                "tracks": tracks,
            })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _fetch_via_embed(self, playlist_id):
        try:
            embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
            req = urllib.request.Request(embed_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")

            token_match = re.search(r'"accessToken":"([^"]+)"', html)
            if not token_match:
                return None

            access_token = token_match.group(1)
            return self._fetch_all_tracks_api(playlist_id, access_token)
        except Exception:
            return None

    def _fetch_remaining_via_embed(self, playlist_id, already_have):
        try:
            embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
            req = urllib.request.Request(embed_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8")

            token_match = re.search(r'"accessToken":"([^"]+)"', html)
            if not token_match:
                return []

            access_token = token_match.group(1)
            result = self._fetch_all_tracks_api(playlist_id, access_token)
            if result and len(result.get("tracks", [])) > already_have:
                return result["tracks"][already_have:]
            return []
        except Exception:
            return []

    def _fetch_all_tracks_api(self, playlist_id, access_token):
        tracks = []
        playlist_name = ""
        offset = 0
        limit = 100

        try:
            meta_url = f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name,owner(display_name)"
            req = urllib.request.Request(meta_url, headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Mozilla/5.0"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
                playlist_name = meta.get("name", "")
        except Exception:
            pass

        while True:
            api_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?offset={offset}&limit={limit}&fields=items(track(name,artists(name),duration_ms,album(name))),total,next"
            req = urllib.request.Request(api_url, headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "Mozilla/5.0"
            })

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception:
                break

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

        return {"name": playlist_name, "tracks": tracks}

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
