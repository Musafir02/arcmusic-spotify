from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import urllib.error
import re
import time
import concurrent.futures

PARTNER_API = "https://api-partner.spotify.com/pathfinder/v1/query"
FALLBACK_HASH = "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4"

_hash_cache = {"hash": FALLBACK_HASH, "expires": 0}
_rate_limit_store = {}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 15
HASH_CACHE_TTL = 86400

# Regex patterns for cleaning titles
CLEAN_REGEX = re.compile(
    r'\s*(\(from\s+[^)]+\)|-\s*(slowed|reverb|lofi|lo-fi|remix|unplugged|reprise|acoustic|male|female|version|cover|remastered|extended|edit|feat\.?.*|ft\.?.*)$|\((slowed|reverb|lofi|lo-fi|remix|unplugged|reprise|acoustic|male|female|version|cover|remastered|extended|edit)[^)]*\)|\(feat\.?[^)]*\))',
    re.IGNORECASE
)
NON_WORD_CHAR_REGEX = re.compile(r'[^\w\s]')
WHITESPACE_REGEX = re.compile(r'\s+')

def _clean_title(title):
    if not title:
        return ""
    t = CLEAN_REGEX.sub('', title).strip()
    return t if t else title

def _string_similarity(a, b):
    if a == b:
        return 1.0
    a_clean = NON_WORD_CHAR_REGEX.sub('', a).strip()
    b_clean = NON_WORD_CHAR_REGEX.sub('', b).strip()
    if a_clean == b_clean:
        return 1.0
    if not a_clean or not b_clean:
        return 0.0

    if b_clean in a_clean or a_clean in b_clean:
        min_len = min(len(a_clean), len(b_clean))
        max_len = max(len(a_clean), len(b_clean))
        return (min_len / max_len) * 0.95

    a_words = set(w for w in WHITESPACE_REGEX.split(a_clean) if len(w) > 1)
    b_words = set(w for w in WHITESPACE_REGEX.split(b_clean) if len(w) > 1)
    if not a_words or not b_words:
        return 0.0

    intersection = len(a_words.intersection(b_words))
    union = len(a_words.union(b_words))
    jaccard = intersection / union if union > 0 else 0.0
    overlap = intersection / min(len(a_words), len(b_words))
    return jaccard * 0.5 + overlap * 0.5

def _search_jiosaavn(query):
    encoded = urllib.parse.quote(query)
    url = f"https://arcmusic-api.vercel.app/api/search/songs?query={encoded}&page=1&limit=8"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success") and isinstance(data.get("data"), dict):
                return data["data"].get("results", [])
    except Exception:
        pass
    return []

def _parse_jiosaavn_song(s):
    image_raw = s.get("image")
    images = image_raw if isinstance(image_raw, list) else []
    
    artists_raw = s.get("artists", {}).get("primary", [])
    artists = artists_raw if isinstance(artists_raw, list) else []
    artist_names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
    
    dl_raw = s.get("downloadUrl")
    download_urls = dl_raw if isinstance(dl_raw, list) else []
    best_download = ""
    for dl in reversed(download_urls):
        if isinstance(dl, dict) and dl.get("url"):
            best_download = dl["url"]
            break

    def get_image_url(index):
        if 0 <= index < len(images):
            item = images[index]
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
        return ""

    def clean_html(text):
        if not text:
            return ""
        return (text
                .replace("&quot;", '"')
                .replace("&amp;", '&')
                .replace("&lt;", '<')
                .replace("&gt;", '>')
                .replace("&#039;", "'"))

    return {
        "id": str(s.get("id", "")),
        "name": clean_html(str(s.get("name", ""))),
        "duration": int(s.get("duration", 0)),
        "album": clean_html(s.get("album", {}).get("name", "") if s.get("album") else ""),
        "artist": ", ".join(artist_names),
        "imageSmall": get_image_url(0),
        "image": get_image_url(1),
        "imageLarge": get_image_url(2),
        "downloadUrl": best_download,
    }

def _score_candidate(song, clean_name, target_secs, artists):
    song_clean = _clean_title(song.get("name", "")).lower()
    name_sim = _string_similarity(song_clean, clean_name.lower())
    if name_sim < 0.38:
        return -1.0

    duration = int(song.get("duration", 0))
    dur_diff = abs(duration - target_secs) if target_secs > 0 else 0.0
    if dur_diff > 30 and name_sim < 0.82:
        return -1.0

    artist_names = song.get("artist", "").lower()
    artist_bonus = 1.5 if any(a.lower().split(' ')[0] in artist_names for a in artists if a) else 0.0
    dur_penalty = min(dur_diff * 0.08, 3.0) if target_secs > 0 else 0.0

    return name_sim * 10 + artist_bonus - dur_penalty

def _best_match(candidates, clean_name, target_secs, artists):
    best = None
    best_score = float("-inf")
    for s in candidates:
        parsed = _parse_jiosaavn_song(s)
        score = _score_candidate(parsed, clean_name, target_secs, artists)
        if score > best_score and score >= 0:
            best_score = score
            best = parsed
    return best

def _match_single_track(track):
    target_secs = (track.get("duration_ms", 0) or 0) / 1000.0
    name = track.get("name", "")
    clean_name = _clean_title(name)
    artists = track.get("artists", [])
    primary_artist = artists[0] if artists else ""

    primary_query = f"{name} {primary_artist}".strip()
    candidates = []

    try:
        initial_results = _search_jiosaavn(primary_query)
        best_initial = _best_match(initial_results, clean_name, target_secs, artists)
        if best_initial:
            sim = _string_similarity(_clean_title(best_initial.get("name", "")).lower(), clean_name.lower())
            dur_diff = abs(best_initial.get("duration", 0) - target_secs) if target_secs > 0 else 0.0
            if sim >= 0.85 and dur_diff <= 10:
                return {"spotify": track, "matched_song": best_initial}
        candidates.extend(initial_results)
    except Exception:
        pass

    fallback_queries = {
        f"{clean_name} {primary_artist}".strip(),
        clean_name,
        name
    }
    fallback_queries = [q for q in fallback_queries if q and q != primary_query]

    if fallback_queries:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                results_lists = list(executor.map(_search_jiosaavn, fallback_queries))
            for results in results_lists:
                for s in results:
                    if not any(c.get("id") == str(s.get("id")) for c in candidates):
                        candidates.append(s)
        except Exception:
            pass

    best = _best_match(candidates, clean_name, target_secs, artists)
    return {"spotify": track, "matched_song": best}


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
        match = params.get("match", ["false"])[0].lower() == "true"

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

            if match:
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    tracks = list(executor.map(_match_single_track, tracks))

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
