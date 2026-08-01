import re
import time
import urllib.request
import urllib.parse
import threading
import logging
import json
from ..db import get_db

logger = logging.getLogger(__name__)

# Pattern to find any Google Maps links (shortened or full)
MAPS_URL_PATTERN = re.compile(
    r'(https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.(?:com|[a-z]{2,3}))/[^\s]+)',
    re.IGNORECASE
)

# Explicit "Lat ... Long/Lng/Lon ..." pattern already present in a message
LAT_PATTERN = re.compile(r'Lat\s*[:\-]?\s*([+-]?\d+\.\d+)', re.I)
LNG_PATTERN = re.compile(r'(?:Long|Lng|Lon)\s*[:\-]?\s*([+-]?\d+\.\d+)', re.I)

MAX_REDIRECTS = 6
REQUEST_TIMEOUT = 8
MAX_ATTEMPTS = 2

# ---- Reverse geocoding (lat/lng -> human readable address) ----
# Simple in-memory cache (rounded to ~11m precision) + a lock that enforces
# Nominatim's "max 1 request/sec" usage policy across all callers.
_reverse_geocode_cache = {}
_reverse_geocode_lock = threading.Lock()
_last_nominatim_call = [0.0]


def _throttle_nominatim():
    with _reverse_geocode_lock:
        elapsed = time.time() - _last_nominatim_call[0]
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        _last_nominatim_call[0] = time.time()


def reverse_geocode(lat, lng):
    """
    Turns a (lat, lng) pair into a human-readable address/place name using
    Nominatim's reverse geocoding endpoint. Cached in-memory so the same
    point isn't looked up repeatedly, and throttled to respect Nominatim's
    usage policy. Returns a string, or None if it couldn't be resolved.
    """
    key = (round(float(lat), 4), round(float(lng), 4))
    if key in _reverse_geocode_cache:
        return _reverse_geocode_cache[key]

    _throttle_nominatim()
    try:
        url = (
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}"
            f"&format=json&zoom=16&addressdetails=1"
        )
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'SmsGatewayApplet/1.0 (cyber.securtry.xyz@gmail.com)'
            }
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
            address = data.get('display_name') if data else None
            _reverse_geocode_cache[key] = address
            return address
    except Exception as e:
        logger.warning("Reverse geocoding failed for (%s, %s): %s", lat, lng, e)
        _reverse_geocode_cache[key] = None
        return None


def extract_explicit_coords(text):
    """
    Step 1 (highest priority): if the message text ALREADY contains explicit
    'Lat ... Long ...' coordinates, just use those directly - no need to
    touch any link at all.
    """
    if not text:
        return None
    lat_match = LAT_PATTERN.search(text)
    lng_match = LNG_PATTERN.search(text)
    if lat_match and lng_match:
        try:
            return float(lat_match.group(1)), float(lng_match.group(1))
        except ValueError:
            return None
    return None


def geocode_place_name(place_name):
    """
    Geocodes a place name (e.g. 'Agra, Uttar Pradesh') using Nominatim.
    Returns (lat, lng) if successful, else None.
    """
    try:
        query = urllib.parse.quote_plus(place_name)
        url = f"https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1"
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'SmsGatewayApplet/1.0 (cyber.securtry.xyz@gmail.com)'
            }
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data and len(data) > 0:
                lat = float(data[0]['lat'])
                lng = float(data[0]['lon'])
                logger.info("Successfully geocoded '%s' to (%f, %f)", place_name, lat, lng)
                return lat, lng
    except Exception as e:
        logger.warning("Failed geocoding place name '%s': %s", place_name, e)
    return None


def _coords_from_url_string(url):
    """Try to pull lat/lng directly out of a URL string using known Google Maps patterns."""
    # 1. @lat,lng
    m1 = re.search(r'@([+-]?\d+\.\d+),([+-]?\d+\.\d+)', url)
    if m1:
        return float(m1.group(1)), float(m1.group(2))

    # 2. ?q=lat,lng or &q=lat,lng
    m2 = re.search(r'[?&]q=([+-]?\d+\.\d+),([+-]?\d+\.\d+)', url)
    if m2:
        return float(m2.group(1)), float(m2.group(2))

    # 3. /place/lat,lng
    m3 = re.search(r'place/([+-]?\d+\.\d+),([+-]?\d+\.\d+)', url)
    if m3:
        return float(m3.group(1)), float(m3.group(2))

    # 4. !3dLAT!4dLNG (embedded in the "data=" blob Google sometimes uses)
    m4 = re.search(r'!3d([+-]?\d+\.\d+)!4d([+-]?\d+\.\d+)', url)
    if m4:
        return float(m4.group(1)), float(m4.group(2))

    return None


def _extract_place_name(url):
    m = re.search(r'/place/([^/?#]+)', url)
    if not m:
        return None
    place_name = urllib.parse.unquote_plus(m.group(1))
    if '/data=' in place_name:
        place_name = place_name.split('/data=')[0]
    if 'data=' in place_name:
        place_name = place_name.split('data=')[0]
    return place_name.strip() or None


def _resolve_redirect_chain(url):
    """
    Follows the FULL redirect chain manually (shortened Google Maps links often
    hop through more than one redirect before reaching the final URL that
    actually contains coordinates), and returns the final URL reached.
    """
    current_url = url
    seen = set()
    for _ in range(MAX_REDIRECTS):
        if current_url in seen:
            break
        seen.add(current_url)

        req = urllib.request.Request(
            current_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        )

        # Check if this URL already has coordinates - no need to go further
        direct = _coords_from_url_string(current_url)
        if direct:
            return current_url

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise urllib.request.HTTPError(req.full_url, code, msg, headers, fp)

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
                # No further redirect - this is the final page
                return response.geturl()
        except urllib.request.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get('Location')
                if not location:
                    return current_url
                # Location can be relative
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            else:
                logger.warning("HTTP error resolving URL %s: %s", current_url, e)
                return current_url
        except Exception as e:
            logger.warning("Error resolving URL %s: %s", current_url, e)
            return current_url

    return current_url


def extract_coords_from_url(url):
    """
    Step 2: resolve a (possibly shortened) Google Maps URL to real coordinates.
    Follows the full redirect chain, tries a few coordinate patterns, and
    falls back to geocoding an embedded place name. Retries once on failure
    since these are network calls that can transiently time out.
    Returns (lat, lng) if successful, else None (never a made-up coordinate).
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            final_url = _resolve_redirect_chain(url)
            if not final_url:
                continue

            logger.info("Resolved maps URL %s to final URL %s (attempt %d)", url, final_url, attempt)

            coords = _coords_from_url_string(final_url)
            if coords:
                return coords

            place_name = _extract_place_name(final_url)
            if place_name:
                logger.info("Extracted place name '%s' from URL, trying to geocode...", place_name)
                coords = geocode_place_name(place_name)
                if coords:
                    return coords

        except Exception as e:
            logger.warning("Failed resolving maps URL %s (attempt %d): %s", url, attempt, e)

        if attempt < MAX_ATTEMPTS:
            time.sleep(1)

    logger.warning("Could not resolve real coordinates for %s after %d attempts", url, MAX_ATTEMPTS)
    return None


def enrich_message_by_id(msg_id, text):
    """
    Runs in a background thread to resolve map URLs in the message text
    and updates the database.

    Priority:
      1. If the message ALREADY contains explicit Lat/Long, do nothing - those
         are used as-is by the frontend.
      2. Otherwise, resolve any Google Maps link(s) in the text to the REAL
         coordinates and append them. If resolution fails, do NOT write any
         placeholder/made-up coordinate - leave the message untouched so it
         can be retried later rather than permanently recording a wrong spot.
    """
    # Priority 1: explicit coordinates already present - nothing to do.
    if extract_explicit_coords(text):
        return

    urls = MAPS_URL_PATTERN.findall(text)
    if not urls:
        return

    updated_text = text
    any_resolved = False
    for url in urls:
        coords = extract_coords_from_url(url)
        if not coords:
            # Do not fabricate a coordinate - better to show nothing than a wrong location.
            continue

        lat, lng = coords
        coord_suffix = f" (Lat: {lat:.6f} Long: {lng:.6f})"
        if coord_suffix not in updated_text:
            updated_text += coord_suffix
            any_resolved = True

    if any_resolved and updated_text != text:
        try:
            conn = get_db()
            conn.execute("UPDATE messages SET text = ? WHERE id = ?", (updated_text, msg_id))
            conn.commit()
            conn.close()
            logger.info("Enriched message %s with real coordinates", msg_id)
        except Exception as e:
            logger.error("Failed to save enriched message %s: %s", msg_id, e)
    elif not any_resolved:
        logger.warning("Message %s: could not resolve any real coordinates, left unresolved", msg_id)


def trigger_enrichment(msg_id, text):
    """
    Safely triggers enrichment in a background thread.
    Skips entirely if the message already carries explicit coordinates.
    """
    if not text:
        return
    if extract_explicit_coords(text):
        return
    if MAPS_URL_PATTERN.search(text):
        threading.Thread(target=enrich_message_by_id, args=(msg_id, text), daemon=True).start()