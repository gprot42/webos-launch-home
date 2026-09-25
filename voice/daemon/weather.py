"""Free weather answers via Open-Meteo (no API key / no signup required).

Lookup strategy mirrors android-weather (Atmosphere):
  - one geocode search (count=8), rank locally — never sequential spellings
  - one slim forecast request (current + daily only; no hourly for voice)
  - 15-minute forecast cache; serve stale cache on network failure
  - short HTTP timeouts suitable for a TV

Detect simple weather questions such as "what is the weather tomorrow in
Southend, UK", resolve the location, fetch the forecast, and return a short
spoken-friendly answer. Returns None when the query is not a weather question
so the caller falls back to Grok.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# Same hosts as android-weather AppModule (forecast / geo Retrofit bases).
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# OpenMeteoForecastApi.CURRENT_PARAMS / DAILY_PARAMS — slim for spoken answers
# (android also requests hourly; we skip it to cut payload + parse time).
_FORECAST_CURRENT = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,"
    "precipitation,weather_code,wind_speed_10m"
)
_FORECAST_DAILY = (
    "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "weather_code"
)
_FORECAST_DAYS = 3
_FORECAST_TTL_SEC = 15 * 60  # android-weather active cache window
_HTTP_TIMEOUT_SEC = 3.0

_GEOCODE_CACHE: dict[str, dict] = {}
_FORECAST_CACHE: dict[tuple[float, float], tuple[float, dict]] = {}

# Shared opener: reuses TCP where the runtime allows (android uses OkHttp pool).
_HTTP = urllib.request.build_opener()


def _http_get_json(url: str, *, timeout: float = _HTTP_TIMEOUT_SEC) -> Any:
    """GET JSON with a short timeout and a stable User-Agent."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LaunchHome-weather/1.0 (webOS; Open-Meteo)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with _HTTP.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _place_key(loc: str) -> str:
    k = re.sub(r"[-_,]+", " ", (loc or "").lower()).strip()
    k = re.sub(r"\s+", " ", k)
    return k


def _seed_geo(
    key: str,
    *,
    name: str,
    lat: float,
    lon: float,
    admin1: str = "England",
    country: str = "United Kingdom",
    cc: str = "GB",
    pop: int = 0,
    feature: str = "PPLA",
    tz: str = "Europe/London",
) -> None:
    entry = {
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "feature_code": feature,
        "country_code": cc,
        "timezone": tz,
        "population": pop,
        "country": country,
        "admin1": admin1,
    }
    # Index under normalized keys so "Southend-on-Sea" and "south end" share a hit.
    for k in (key, _place_key(key), _place_key(name)):
        if k:
            _GEOCODE_CACHE[k] = dict(entry)


# Pre-seed common spoken places so voice turns skip geocode HTTP entirely.
_seed_geo(
    "southend-on-sea",
    name="Southend-on-Sea",
    lat=51.53782,
    lon=0.71433,
    feature="PPLA2",
    pop=295310,
)
_seed_geo("london", name="London", lat=51.50853, lon=-0.12574, feature="PPLC", pop=8961989)
_seed_geo("manchester", name="Manchester", lat=53.4808, lon=-2.2426, pop=547627)
_seed_geo("birmingham", name="Birmingham", lat=52.4862, lon=-1.8904, pop=1141816)
_seed_geo("leeds", name="Leeds", lat=53.8008, lon=-1.5491, pop=789194)
_seed_geo("glasgow", name="Glasgow", lat=55.8642, lon=-4.2518, admin1="Scotland", pop=635640)
_seed_geo("edinburgh", name="Edinburgh", lat=55.9533, lon=-3.1883, admin1="Scotland", pop=506520)
_seed_geo("bristol", name="Bristol", lat=51.4545, lon=-2.5879, pop=467099)
_seed_geo("liverpool", name="Liverpool", lat=53.4084, lon=-2.9916, pop=498042)
_seed_geo("cardiff", name="Cardiff", lat=51.4816, lon=-3.1791, admin1="Wales", pop=362756)
_seed_geo("belfast", name="Belfast", lat=54.5973, lon=-5.9301, admin1="Northern Ireland", pop=345418)
_seed_geo("cambridge", name="Cambridge", lat=52.2053, lon=0.1218, pop=145674)
_seed_geo("oxford", name="Oxford", lat=51.7520, lon=-1.2577, pop=152450)
_seed_geo("brighton", name="Brighton", lat=50.8225, lon=-0.1372, pop=229700)
_seed_geo("reading", name="Reading", lat=51.4543, lon=-0.9781, pop=174224)
_seed_geo("chelmsford", name="Chelmsford", lat=51.7356, lon=0.4685, pop=111511)
_seed_geo("basildon", name="Basildon", lat=51.5761, lon=0.4887, pop=115955)
# Capitals often asked by country name.
_seed_geo("paris", name="Paris", lat=48.8566, lon=2.3522, admin1="Île-de-France", country="France", cc="FR", pop=2161000, tz="Europe/Paris")
_seed_geo("new york", name="New York", lat=40.7128, lon=-74.0060, admin1="New York", country="United States", cc="US", pop=8336817, tz="America/New_York")
_seed_geo("tokyo", name="Tokyo", lat=35.6762, lon=139.6503, admin1="Tokyo", country="Japan", cc="JP", pop=13960000, tz="Asia/Tokyo")

# Literal weather/climate questions only. Avoid metaphors ("raining insults",
# "stormy relationship") by requiring a weather framing or location/time cue.
# Include STT typos: wather, whether (as weather), temprature.
_WEATHER_HINT = re.compile(
    r"(?i)\b("
    r"weather|wather|whether|forcast|forecast|temperature|temprature|humid(?:ity)?|"
    r"how (?:hot|cold|warm)(?: is| will| does)?|"
    r"(?:is|will) it (?:rain|snow|sunny|cloudy|hot|cold|warm|windy)|"
    r"(?:rain|snow|sunny|cloudy|windy) (?:today|tomorrow|tonight|this)|"
    r"(?:today|tomorrow|tonight)'?s? (?:weather|wather|forecast|temperature)|"
    r"(?:weather|wather|whether) (?:in|for|at|near)|"
    r"forecast (?:in|for|at)|"
    r"temperature (?:in|for|at)|"
    r"(?:rain|snow|sun|cloud|wind) (?:in|for|at|near)\b"
    r")",
)

# STT often drops "weather" but keeps "in Southend today" / "South end today".
# Preposition form is safest; bare multi-word is allowed only after place alias
# (so "Then today" never matches, but "South end today" does).
_PLACE_DAY_HINT = re.compile(
    r"(?i)(?:"
    r"\b(?:in|at|for|near)\s+[A-Za-z][\w'.-]{2,40}"
    r"(?:\s+[A-Za-z][\w'.-]{1,30}){0,3}"
    r"\s*,?\s*(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening))\b"
    r"|"
    r"\b(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening))"
    r"\s+(?:in|at|for|near)\s+[A-Za-z][\w'.-]{2,}"
    r")",
)

# Bare "South end today" / "London tomorrow" (no preposition). Single English
# words like "Then today" are rejected via aliases + stopwords.
_PLACE_DAY_BARE = re.compile(
    r"(?i)^([A-Za-z][\w'.-]{2,40}(?:\s+[A-Za-z][\w'.-]{1,30}){0,3})"
    r"\s+(?:today|tonight|tomorrow)\s*[?.!]?$"
)

# STT place misspellings / splits → canonical geocode name.
# "south end" must NOT hit Nicaragua's "South End".
_PLACE_ALIASES: dict[str, str] = {
    "south end": "Southend-on-Sea",
    "southend": "Southend-on-Sea",
    "southend on sea": "Southend-on-Sea",
    "south end on sea": "Southend-on-Sea",
    "southend-on-sea": "Southend-on-Sea",
    "suthend": "Southend-on-Sea",
    "suthend on sea": "Southend-on-Sea",
    "south and": "Southend-on-Sea",
    "south an": "Southend-on-Sea",
    "southend essex": "Southend-on-Sea",
    "south end essex": "Southend-on-Sea",
    "southampton sea": "Southend-on-Sea",
    "southampton sea england": "Southend-on-Sea",
}

# Alias keys share seed coords so "south end" never hits geocode HTTP.
for _alias, _canonical in list(_PLACE_ALIASES.items()):
    _ck = _place_key(_canonical)
    _entry = _GEOCODE_CACHE.get(_ck)
    if _entry:
        _GEOCODE_CACHE[_place_key(_alias)] = dict(_entry)
        _GEOCODE_CACHE[_alias.replace(" ", "")] = dict(_entry)

# When several Open-Meteo hits exist, prefer UK for these spoken names.
_PREFER_UK: frozenset[str] = frozenset(
    {
        "southend",
        "southend-on-sea",
        "south end",
        "suthend",
        "london",
        "manchester",
        "birmingham",
        "leeds",
        "glasgow",
        "edinburgh",
        "bristol",
        "liverpool",
        "cardiff",
        "belfast",
        "cambridge",
        "oxford",
        "brighton",
        "reading",
        "chelmsford",
        "basildon",
    }
)

# English function words / fillers that Open-Meteo still geocodes as place names
# ("Then" → Then, India; "The" → Teresina; "And" → Anderson).
_PLACE_DAY_SKIP = frozenset(
    """
    a an the and or but if then than that this these those there here
    when what where who why how which whom whose
    i me my we us our you your he she him her it they them their
    is am are was were be been being do does did have has had
    will would could should may might must can shall
    not no yes so too very just only all any some more most other such
    to of in on at for from by with as into over after before under
    up out about now today tonight tomorrow please
    see look get go come take make know think say tell ask
    call meet back later coming going doing
    good great fine ok okay hello hi hey thanks thank right
    home work bed school day night morning afternoon evening
    weather forecast temperature rain snow sun wind
    """.split()
)

# Metaphor / non-weather disqualifiers when only a loose rain/storm word matches.
_WEATHER_METAPHOR = re.compile(
    r"(?i)\b("
    r"raining (?:insults|money|men|cats|dogs)?|"
    r"stormy (?:relationship|romance|night of love)|"
    r"snowed under|under the weather|"
    r"cold shoulder|hot take|rain on my parade|"
    r"brainstorm|brainstorming"
    r")\b",
)

# WMO weather interpretation codes -> plain-language description.
_WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with light hail",
    99: "thunderstorms with heavy hail",
}


def _normalize_place(loc: str) -> str:
    """Map STT place crumbs to a canonical name (South end → Southend-on-Sea)."""
    if not loc:
        return loc
    key = _place_key(loc)
    key = re.sub(r"\s*,\s*", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if key in _PLACE_ALIASES:
        return _PLACE_ALIASES[key]
    nospace = key.replace(" ", "")
    if nospace in _PLACE_ALIASES:
        return _PLACE_ALIASES[nospace]
    # "south end essex" style: try first two tokens
    parts = key.split()
    if len(parts) >= 2:
        two = " ".join(parts[:2])
        if two in _PLACE_ALIASES:
            return _PLACE_ALIASES[two]
    return loc.strip()


def normalize_weather_transcript(text: str) -> str:
    """Correct deterministic weather-place STT regressions before display."""
    raw = text or ""
    corrected = re.sub(
        r"(?i)\bSouthampton\s*,?\s+Sea(?:\s*,?\s+England)?\b",
        "Southend-on-Sea, England",
        raw,
    )
    if corrected != raw:
        print(
            "[voice] weather STT correction: %r -> %r"
            % (raw[:100], corrected[:100]),
            flush=True,
        )
    return corrected


def _location_tokens_ok(loc: str) -> bool:
    """Reject STT crumbs that are English words, not place names."""
    if not loc:
        return False
    # Known aliases always OK (even "south end" which contains stopword "end").
    if _place_key(loc) in _PLACE_ALIASES or _place_key(loc).replace(" ", "") in _PLACE_ALIASES:
        return True
    if _normalize_place(loc) != loc.strip():
        return True
    words = [
        w.lower().strip(".,?!'\"")
        for w in re.split(r"\s+", loc.strip())
        if w.strip(".,?!'\"")
    ]
    if not words:
        return False
    # Drop leading stopwords ("the weather in …" already stripped; keep safe).
    while words and words[0] in _PLACE_DAY_SKIP:
        words.pop(0)
    if not words:
        return False
    content = [w for w in words if w not in _PLACE_DAY_SKIP]
    if not content:
        return False
    # Need at least one real-looking place token (len>=3, not pure digits).
    if not any(len(w) >= 3 and not w.isdigit() for w in content):
        return False
    # Whole phrase still a stopword phrase?
    if " ".join(words).lower() in _PLACE_DAY_SKIP:
        return False
    return True


def is_weather_query(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _WEATHER_METAPHOR.search(t):
        return False
    if _WEATHER_HINT.search(t):
        return True
    # Truncated STT with preposition: "In Southend today"
    if _PLACE_DAY_HINT.search(t):
        loc = _extract_location(t)
        if _location_tokens_ok(loc or ""):
            return True
    # Bare "South end today" only when place aliases / looks real.
    # Never "Then today." (single stopword).
    m = _PLACE_DAY_BARE.match(t.rstrip("?.!").strip())
    if m:
        loc = m.group(1).strip()
        if _location_tokens_ok(loc):
            return True
    return False


def _day_offset(text: str) -> int:
    t = (text or "").lower()
    if "day after tomorrow" in t:
        return 2
    if "tomorrow" in t:
        return 1
    return 0


# Country / alias -> representative capital city. A bare country name geocodes
# to the country centroid (an arbitrary, often uninhabited point), which gives a
# meaningless forecast, so we resolve it to the capital instead. Worldwide
# coverage with no API key -- Open-Meteo geocodes each capital fine.
_CAPITALS = {
    "afghanistan": "Kabul", "albania": "Tirana", "algeria": "Algiers",
    "andorra": "Andorra la Vella", "angola": "Luanda", "argentina": "Buenos Aires",
    "armenia": "Yerevan", "australia": "Canberra", "austria": "Vienna",
    "azerbaijan": "Baku", "bahamas": "Nassau", "bahrain": "Manama",
    "bangladesh": "Dhaka", "barbados": "Bridgetown", "belarus": "Minsk",
    "belgium": "Brussels", "belize": "Belmopan", "benin": "Porto-Novo",
    "bhutan": "Thimphu", "bolivia": "La Paz", "bosnia": "Sarajevo",
    "bosnia and herzegovina": "Sarajevo", "botswana": "Gaborone",
    "brazil": "Brasilia", "brunei": "Bandar Seri Begawan", "bulgaria": "Sofia",
    "burkina faso": "Ouagadougou", "burundi": "Gitega", "cambodia": "Phnom Penh",
    "cameroon": "Yaounde", "canada": "Ottawa", "chad": "N'Djamena",
    "chile": "Santiago", "china": "Beijing", "colombia": "Bogota",
    "congo": "Kinshasa", "costa rica": "San Jose", "croatia": "Zagreb",
    "cuba": "Havana", "cyprus": "Nicosia", "czech republic": "Prague",
    "czechia": "Prague", "denmark": "Copenhagen", "dominican republic": "Santo Domingo",
    "ecuador": "Quito", "egypt": "Cairo", "el salvador": "San Salvador",
    "england": "London", "estonia": "Tallinn", "ethiopia": "Addis Ababa",
    "fiji": "Suva", "finland": "Helsinki", "france": "Paris",
    "georgia": "Tbilisi", "germany": "Berlin", "ghana": "Accra",
    "greece": "Athens", "greenland": "Nuuk", "guatemala": "Guatemala City",
    "guyana": "Georgetown", "haiti": "Port-au-Prince", "honduras": "Tegucigalpa",
    "hungary": "Budapest", "iceland": "Reykjavik", "india": "New Delhi",
    "indonesia": "Jakarta", "iran": "Tehran", "iraq": "Baghdad",
    "ireland": "Dublin", "israel": "Jerusalem", "italy": "Rome",
    "ivory coast": "Yamoussoukro", "jamaica": "Kingston", "japan": "Tokyo",
    "jordan": "Amman", "kazakhstan": "Astana", "kenya": "Nairobi",
    "kuwait": "Kuwait City", "kyrgyzstan": "Bishkek", "laos": "Vientiane",
    "latvia": "Riga", "lebanon": "Beirut", "libya": "Tripoli",
    "liechtenstein": "Vaduz", "lithuania": "Vilnius", "luxembourg": "Luxembourg",
    "madagascar": "Antananarivo", "malawi": "Lilongwe", "malaysia": "Kuala Lumpur",
    "maldives": "Male", "mali": "Bamako", "malta": "Valletta",
    "mauritania": "Nouakchott", "mauritius": "Port Louis", "mexico": "Mexico City",
    "moldova": "Chisinau", "monaco": "Monaco", "mongolia": "Ulaanbaatar",
    "montenegro": "Podgorica", "morocco": "Rabat", "mozambique": "Maputo",
    "myanmar": "Naypyidaw", "namibia": "Windhoek", "nepal": "Kathmandu",
    "netherlands": "Amsterdam", "new zealand": "Wellington", "nicaragua": "Managua",
    "niger": "Niamey", "nigeria": "Abuja", "north korea": "Pyongyang",
    "north macedonia": "Skopje", "macedonia": "Skopje", "norway": "Oslo",
    "oman": "Muscat", "pakistan": "Islamabad", "panama": "Panama City",
    "papua new guinea": "Port Moresby", "paraguay": "Asuncion", "peru": "Lima",
    "philippines": "Manila", "poland": "Warsaw", "portugal": "Lisbon",
    "qatar": "Doha", "romania": "Bucharest", "russia": "Moscow",
    "rwanda": "Kigali", "san marino": "San Marino", "saudi arabia": "Riyadh",
    "scotland": "Edinburgh", "senegal": "Dakar", "serbia": "Belgrade",
    "sierra leone": "Freetown", "singapore": "Singapore", "slovakia": "Bratislava",
    "slovenia": "Ljubljana", "somalia": "Mogadishu", "south africa": "Pretoria",
    "south korea": "Seoul", "korea": "Seoul", "south sudan": "Juba",
    "spain": "Madrid", "sri lanka": "Colombo", "sudan": "Khartoum",
    "sweden": "Stockholm", "switzerland": "Bern", "syria": "Damascus",
    "taiwan": "Taipei", "tajikistan": "Dushanbe", "tanzania": "Dodoma",
    "thailand": "Bangkok", "togo": "Lome", "trinidad and tobago": "Port of Spain",
    "tunisia": "Tunis", "turkey": "Ankara", "turkmenistan": "Ashgabat",
    "uganda": "Kampala", "ukraine": "Kyiv", "united arab emirates": "Abu Dhabi",
    "uae": "Abu Dhabi", "united kingdom": "London", "uk": "London",
    "great britain": "London", "britain": "London", "united states": "Washington",
    "united states of america": "Washington", "usa": "Washington",
    "us": "Washington", "america": "Washington", "uruguay": "Montevideo",
    "uzbekistan": "Tashkent", "venezuela": "Caracas", "vietnam": "Hanoi",
    "wales": "Cardiff", "yemen": "Sanaa", "zambia": "Lusaka",
    "zimbabwe": "Harare",
}

# GeoNames feature codes for country / territory level results (Open-Meteo
# returns these when a bare country name is searched).
_COUNTRY_FEATURE_CODES = {"PCLI", "PCLD", "PCLIX", "PCLS", "PCLF", "PCL", "TERR"}


def _search_places(name: str) -> list:
    """One Open-Meteo geocode call (android OpenMeteoGeocodingApi.search)."""
    name = (name or "").strip()
    if not name:
        return []
    q = urllib.parse.urlencode(
        {
            "name": name,
            "count": 8,  # android default
            "language": "en",
            "format": "json",
        }
    )
    try:
        data = _http_get_json(GEOCODE_URL + "?" + q)
    except Exception as exc:  # noqa: BLE001
        print("[voice] weather geocode http failed: %s" % exc, flush=True)
        return []
    if not isinstance(data, dict):
        return []
    return data.get("results") or []


def _extract_location(text: str) -> Optional[str]:
    """Pull the place name out of the question.

    Handles "... in Southend, UK", "... for Paris", "weather in New York",
    truncated STT "In Southend today", bare "Southend today", "Southend weather".
    Strips trailing time words so "in Southend tomorrow" -> "Southend".
    """
    t = (text or "").strip().rstrip("?.!")

    def _strip_time_tail(loc: str) -> str:
        loc = re.sub(
            r"\b(today|tonight|tomorrow|day after tomorrow|this|"
            r"morning|afternoon|evening|right now|now|please|"
            r"like|going to be|be)\b.*$",
            "",
            loc,
            flags=re.IGNORECASE,
        ).strip()
        return loc.strip(" ,")

    # "... in Southend today" / "weather for Paris"
    m = re.search(r"\b(?:in|at|for|near)\s+(.+)$", t, re.IGNORECASE)
    if m:
        loc = _strip_time_tail(m.group(1).strip())
        if loc:
            return loc

    # "today in Southend" / "tonight near Paris"
    m = re.search(
        r"\b(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening))"
        r"\s+(?:in|at|for|near)\s+(.+)$",
        t,
        re.IGNORECASE,
    )
    if m:
        loc = _strip_time_tail(m.group(1).strip())
        if loc:
            return loc

    # "Southend weather" / "London forecast"
    m = re.search(
        r"^(.+?)\s+(?:weather|forecast|temperature)\s*$", t, re.IGNORECASE
    )
    if m:
        loc = _strip_time_tail(m.group(1).strip())
        # Drop leading fillers: "the weather" already handled; "what's the X"
        loc = re.sub(
            r"^(?:what(?:'s| is|s)?|the|a|an)\s+", "", loc, flags=re.IGNORECASE
        ).strip()
        if loc and loc.lower() not in ("the", "a", "an", "what"):
            return loc

    # Bare "Southend today" only when first token is a plausible place
    # (never "Then today" / "And today" from truncated STT).
    m = re.match(
        r"^([A-Za-z][\w'.-]{2,40}(?:\s+[A-Za-z][\w'.-]{1,30}){0,2})"
        r"\s+(?:today|tonight|tomorrow)\s*$",
        t,
        re.IGNORECASE,
    )
    if m:
        loc = m.group(1).strip()
        if _location_tokens_ok(loc):
            return loc

    return None


def _result_score(r: dict, *, query: str, prefer_uk: bool) -> tuple:
    """Higher is better. Prefer large UK cities over tiny same-name towns."""
    name = (r.get("name") or "").lower()
    q = re.sub(r"[-']", " ", query.lower()).strip()
    q0 = q.replace(" ", "")
    n0 = name.replace("-", " ").replace(" ", "")
    pop = 0
    try:
        pop = int(r.get("population") or 0)
    except (TypeError, ValueError):
        pop = 0
    cc = (r.get("country_code") or "").lower()
    # Exact / strong name match
    name_score = 0
    if n0 == q0 or name.replace("-", " ") == q:
        name_score = 100
    elif q0 and (q0 in n0 or n0.startswith(q0[: min(6, len(q0))])):
        name_score = 60
    elif q.split() and name.startswith(q.split()[0][:4]):
        name_score = 30
    uk_bonus = 40 if prefer_uk and cc == "gb" else 0
    if prefer_uk and cc != "gb":
        uk_bonus = -50
    # Population (log-ish): 300k city beats 8k village
    pop_score = min(pop, 2_000_000) // 1000
    return (name_score + uk_bonus, pop_score, pop)


def _geocode(location: str) -> Optional[dict]:
    """Resolve place → lat/lon with at most one HTTP search.

    Mirrors android-weather: OpenMeteoGeocodingApi.search(name, count=8) then
    local ranking — not a loop of alternate spellings over the network.
    """
    location = _normalize_place(location)
    cache_key = _place_key(location)
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached:
        return dict(cached)
    # Alias-normalized name may already be seeded (south end → southend-on-sea).
    seed = _GEOCODE_CACHE.get(_place_key(location))
    if seed:
        return dict(seed)

    parts = [p.strip() for p in location.split(",") if p.strip()]
    name = parts[0] if parts else location
    country_hint = parts[1].lower() if len(parts) > 1 else ""

    # Bare country/region → capital (centroid forecasts are useless).
    key = name.lower().strip()
    prefer_uk = _place_key(name) in _PREFER_UK or key.replace(" ", "") in {
        k.replace(" ", "") for k in _PREFER_UK
    }
    if key in _CAPITALS:
        country_hint = country_hint or key
        name = _CAPITALS[key]
        prefer_uk = prefer_uk or key in (
            "uk",
            "united kingdom",
            "england",
            "britain",
            "scotland",
            "wales",
        )
        # Capitals we pre-seed (paris/london/tokyo/…) skip HTTP entirely.
        cap_seed = _GEOCODE_CACHE.get(_place_key(name))
        if cap_seed:
            _GEOCODE_CACHE[cache_key] = dict(cap_seed)
            return dict(cap_seed)

    if prefer_uk and not country_hint:
        country_hint = "uk"

    # Prefer hyphenated form when spaces look like "Southend on Sea".
    query_name = name.strip()
    if " " in query_name and "-" not in query_name:
        # Still one HTTP call — Open-Meteo matches both; hyphen helps a few towns.
        query_name = name

    t0 = time.time()
    results = _search_places(query_name)
    if not results and " " in name:
        # Single cheap fallback: first token only (e.g. "New York City" → "New").
        # Avoids the old 4-candidate sequential waterfall.
        results = _search_places(name.split()[0])
    print(
        "[voice] weather geocode search name=%r hits=%d +%.0fms"
        % (query_name, len(results), (time.time() - t0) * 1000.0),
        flush=True,
    )
    if not results:
        return None

    scored = sorted(
        results,
        key=lambda r: _result_score(r, query=name, prefer_uk=prefer_uk),
        reverse=True,
    )
    chosen = scored[0]
    if country_hint:
        for r in scored:
            cc = (r.get("country_code") or "").lower()
            cn = (r.get("country") or "").lower()
            if country_hint in (
                "uk",
                "u.k.",
                "england",
                "britain",
                "scotland",
                "wales",
                "gb",
            ) and cc == "gb":
                chosen = r
                break
            if country_hint in (cc, cn) or country_hint in cn:
                chosen = r
                break

    # Country-level hit → one capital search (android uses location enrichment).
    if (chosen.get("feature_code") or "") in _COUNTRY_FEATURE_CODES:
        cap = (
            _CAPITALS.get((chosen.get("name") or "").lower())
            or _CAPITALS.get((chosen.get("country") or "").lower())
        )
        if cap:
            cap_seed = _GEOCODE_CACHE.get(_place_key(cap))
            if cap_seed:
                chosen = dict(cap_seed)
            else:
                cap_results = _search_places(cap)
                if cap_results:
                    chosen = cap_results[0]

    if chosen:
        _GEOCODE_CACHE[cache_key] = dict(chosen)
        # Also cache under the resolved city name for next turn.
        nk = _place_key(str(chosen.get("name") or ""))
        if nk and nk not in _GEOCODE_CACHE:
            _GEOCODE_CACHE[nk] = dict(chosen)
    return chosen


def _fetch_forecast(lat: float, lon: float) -> dict:
    """One Open-Meteo forecast call (android OpenMeteoForecastApi.forecast).

    current + daily only; 15-minute TTL; stale-on-error like WeatherRepository.
    """
    key = (round(lat, 3), round(lon, 3))
    cached = _FORECAST_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _FORECAST_TTL_SEC:
        return dict(cached[1])

    q = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "timezone": "auto",
            "current": _FORECAST_CURRENT,
            "daily": _FORECAST_DAILY,
            "forecast_days": _FORECAST_DAYS,
        }
    )
    url = FORECAST_URL + "?" + q
    last_error: Optional[Exception] = None
    t0 = time.time()
    for attempt in range(2):
        try:
            result = _http_get_json(url, timeout=_HTTP_TIMEOUT_SEC)
            if not isinstance(result, dict):
                raise RuntimeError("forecast non-object")
            _FORECAST_CACHE[key] = (time.time(), dict(result))
            print(
                "[voice] weather forecast ok +%.0fms attempt=%d"
                % ((time.time() - t0) * 1000.0, attempt + 1),
                flush=True,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 0:
                time.sleep(0.1)
    if cached:
        print(
            "[voice] weather forecast refresh failed (%s); using stale cache"
            % last_error,
            flush=True,
        )
        return dict(cached[1])
    raise RuntimeError("weather forecast unavailable") from last_error


def _place_label(geo: dict) -> str:
    name = geo.get("name") or ""
    admin = geo.get("admin1") or ""
    country = geo.get("country") or ""
    bits = [name]
    if admin and admin.lower() != name.lower():
        bits.append(admin)
    if country:
        bits.append(country)
    return ", ".join([b for b in bits if b])


def _geo_matches_query(location: str, geo: dict, *, strict: bool) -> bool:
    """Drop bad geocodes (e.g. English word 'Then' → village in India)."""
    if not geo or not location:
        return False
    if not _location_tokens_ok(location):
        return False
    # Compare against alias-normalized name (south end → southend-on-sea).
    norm = _normalize_place(location)
    q = re.sub(r"[-']", " ", norm.lower()).strip()
    name = re.sub(r"[-']", " ", (geo.get("name") or "").lower()).strip()
    if not name:
        return False
    q0 = q.split()[0]
    n0 = name.split()[0]
    q_ns = q.replace(" ", "")
    n_ns = name.replace(" ", "")
    # Name must share a prefix with the query (southend ≈ southend-on-sea).
    if len(q0) >= 4:
        if not (
            name.startswith(q0[:4])
            or q0.startswith(n0[:4])
            or q0 in name
            or n0 in q
            or q_ns[:5] in n_ns
            or n_ns[:5] in q_ns
        ):
            return False
    elif q0 != n0 and q0 not in name:
        return False
    pop = 0
    try:
        pop = int(geo.get("population") or 0)
    except (TypeError, ValueError):
        pop = 0
    # Strict path = no explicit "weather" word (place+day only). Demand a
    # real town so garbage STT cannot invent a micro-place match.
    if strict and pop and pop < 15000 and len(q0) <= 5:
        return False
    if strict and not pop and len(q0) <= 4:
        return False
    return True


def weather_answer(text: str) -> Optional[str]:
    """Return a spoken-friendly weather answer, or None if not applicable."""
    if not is_weather_query(text):
        return None
    location = _extract_location(text)
    # Bare "South end today" — extract_location may miss; use bare matcher.
    if not location:
        m = _PLACE_DAY_BARE.match((text or "").rstrip("?.!").strip())
        if m:
            location = m.group(1).strip()
    if not location or not _location_tokens_ok(location):
        # Weather wording but no place — ask rather than invent.
        if _WEATHER_HINT.search(text or ""):
            return (
                "Weather for which city or area? "
                "Just say the place and I'll get the latest conditions for you."
            )
        return None
    location = _normalize_place(location)
    strict = not bool(_WEATHER_HINT.search(text or ""))
    try:
        geo = _geocode(location)
        if not geo or not _geo_matches_query(location, geo, strict=strict):
            if strict:
                # Place+day STT crumb that geocoded nonsense — fall through to Grok.
                return None
            return "I couldn't find a place called %s for a weather report." % location
        lat = float(geo["latitude"])
        lon = float(geo["longitude"])
        print(
            "[voice] weather geocode loc=%r -> %s (%.3f,%.3f) pop=%s"
            % (
                location,
                _place_label(geo),
                lat,
                lon,
                geo.get("population"),
            ),
            flush=True,
        )
        fc = _fetch_forecast(lat, lon)
    except Exception:
        return None

    place = _place_label(geo)
    unit = (fc.get("daily_units", {}) or {}).get("temperature_2m_max", "\u00b0C")
    if unit in ("°C", "\u00b0C", "C"):
        unit = " degrees"  # clearer for TTS than "°C"
    else:
        unit = " " + str(unit)
    offset = _day_offset(text)
    daily = fc.get("daily") or {}
    codes = daily.get("weather_code") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    pprob = daily.get("precipitation_probability_max") or []

    def _code_desc(code: Any) -> str:
        try:
            return _WMO.get(int(code), "unclear conditions")
        except (TypeError, ValueError):
            return "unclear conditions"

    if offset == 0 and fc.get("current"):
        cur = fc["current"]
        daily_code = codes[0] if codes else cur.get("weather_code", -1)
        desc = _code_desc(daily_code)
        ctemp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        if tmax:
            summary = (
                "Today in %s: %s, with a maximum temperature of %d%s"
                % (place, desc, round(float(tmax[0])), unit)
            )
            if tmin:
                summary += " and a minimum of %d%s" % (round(float(tmin[0])), unit)
            parts = [summary]
        else:
            parts = ["Today in %s: %s" % (place, desc)]
        if ctemp is not None:
            line = "It is currently around %d%s" % (round(float(ctemp)), unit)
            try:
                if feels is not None and abs(float(feels) - float(ctemp)) >= 2.5:
                    line += ", feeling like %d%s" % (round(float(feels)), unit)
            except (TypeError, ValueError):
                pass
            parts.append(line)
        if pprob and pprob[0] is not None and round(float(pprob[0])) >= 20:
            parts.append("%d percent chance of rain" % round(float(pprob[0])))
        return ". ".join(parts) + "."

    if offset < len(codes):
        when = {1: "Tomorrow", 2: "The day after tomorrow"}.get(offset, "That day")
        desc = _code_desc(codes[offset])
        hi = round(float(tmax[offset])) if offset < len(tmax) and tmax[offset] is not None else None
        lo = round(float(tmin[offset])) if offset < len(tmin) and tmin[offset] is not None else None
        answer = "%s in %s: %s" % (when, place, desc)
        if hi is not None and lo is not None:
            answer += ", with a high of %d%s and a low of %d%s" % (hi, unit, lo, unit)
        if offset < len(pprob) and pprob[offset] is not None:
            answer += ", and a %d percent chance of precipitation" % round(
                float(pprob[offset])
            )
        return answer + "."

    return "I don't have a forecast that far ahead for %s." % place
