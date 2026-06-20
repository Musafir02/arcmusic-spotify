from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import re

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
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

        try:
            from spotify_scraper import SpotifyClient

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
        match = re.search(r'playlist[/:]([a-zA-Z0-9]+)', url)
        return match.group(1) if match else None

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
