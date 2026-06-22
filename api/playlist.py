from http.server import BaseHTTPRequestHandler
from spotify_scraper import SpotifyClient
import json
import urllib.parse
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
            clean_url = f"https://open.spotify.com/playlist/{playlist_id}"

            with SpotifyClient() as client:
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

                owner_name = ""
                if playlist.owner:
                    owner_name = playlist.owner.get("name", "") if isinstance(playlist.owner, dict) else str(playlist.owner)

                self._json_response(200, {
                    "name": playlist.name,
                    "owner": owner_name,
                    "track_count": playlist.total_tracks or len(tracks),
                    "tracks": tracks,
                })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

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
