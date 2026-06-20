from http.server import BaseHTTPRequestHandler
import json
import urllib.parse

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        url = params.get("url", [None])[0]

        if not url:
            self._json_response(400, {"error": "Missing 'url' parameter"})
            return

        try:
            from spotify_scraper import SpotifyClient

            with SpotifyClient() as client:
                playlist = client.get_playlist(url)

                tracks = []
                for entry in playlist.tracks:
                    track = entry.track
                    artists = [a.name for a in track.artists] if track.artists else []
                    tracks.append({
                        "name": track.name,
                        "artists": artists,
                        "duration_ms": track.duration_ms,
                        "album": track.album.name if track.album else None,
                    })

                self._json_response(200, {
                    "name": playlist.name,
                    "owner": playlist.subtitle or "",
                    "track_count": playlist.track_count or len(tracks),
                    "tracks": tracks,
                })

        except Exception as e:
            self._json_response(500, {"error": str(e)})

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
