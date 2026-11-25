"""Offline geography for the simple trip planner: airports, place lookup,
driving-distance estimates and sample average gas prices.

Distances are great-circle miles times a road factor, so they are
estimates; the traveler can always enter the odometer or map mileage
instead. Gas prices are illustrative sample data by state."""
import math
import re

ROAD_FACTOR = 1.18
DEFAULT_MPG = 25
NATIONAL_GAS = 3.25

# code, city, state, lat, lon
AIRPORTS = [
    ("ABQ", "Albuquerque", "NM", 35.04, -106.61), ("ANC", "Anchorage", "AK", 61.17, -149.99),
    ("ATL", "Atlanta", "GA", 33.64, -84.43), ("AUS", "Austin", "TX", 30.19, -97.67),
    ("BHM", "Birmingham", "AL", 33.56, -86.75), ("BNA", "Nashville", "TN", 36.12, -86.68),
    ("BOS", "Boston", "MA", 42.36, -71.01), ("BWI", "Baltimore", "MD", 39.18, -76.67),
    ("CHS", "Charleston", "SC", 32.90, -80.04), ("CLE", "Cleveland", "OH", 41.41, -81.85),
    ("CLT", "Charlotte", "NC", 35.21, -80.94), ("CMH", "Columbus", "OH", 39.998, -82.89),
    ("COS", "Colorado Springs", "CO", 38.81, -104.70), ("CVG", "Cincinnati", "KY", 39.05, -84.67),
    ("DAY", "Dayton", "OH", 39.90, -84.22), ("DCA", "Washington", "DC", 38.85, -77.04),
    ("DEN", "Denver", "CO", 39.86, -104.67), ("DFW", "Dallas-Fort Worth", "TX", 32.90, -97.04),
    ("DTW", "Detroit", "MI", 42.21, -83.35), ("ELP", "El Paso", "TX", 31.81, -106.38),
    ("EWR", "Newark", "NJ", 40.69, -74.17), ("FAI", "Fairbanks", "AK", 64.82, -147.86),
    ("FAY", "Fayetteville", "NC", 34.99, -78.88), ("HNL", "Honolulu", "HI", 21.32, -157.92),
    ("IAD", "Washington Dulles", "VA", 38.95, -77.46), ("IAH", "Houston", "TX", 29.98, -95.34),
    ("ICT", "Wichita", "KS", 37.65, -97.43), ("IND", "Indianapolis", "IN", 39.72, -86.29),
    ("JAX", "Jacksonville", "FL", 30.49, -81.69), ("JFK", "New York", "NY", 40.64, -73.78),
    ("LAS", "Las Vegas", "NV", 36.08, -115.15), ("LAX", "Los Angeles", "CA", 33.94, -118.41),
    ("MCI", "Kansas City", "MO", 39.30, -94.71), ("MCO", "Orlando", "FL", 28.43, -81.31),
    ("MEM", "Memphis", "TN", 35.04, -89.98), ("MHK", "Manhattan", "KS", 39.14, -96.67),
    ("MIA", "Miami", "FL", 25.79, -80.29), ("MSP", "Minneapolis", "MN", 44.88, -93.22),
    ("MSY", "New Orleans", "LA", 29.99, -90.26), ("OKC", "Oklahoma City", "OK", 35.39, -97.60),
    ("OMA", "Omaha", "NE", 41.30, -95.89), ("ORD", "Chicago", "IL", 41.97, -87.91),
    ("ORF", "Norfolk", "VA", 36.89, -76.20), ("PDX", "Portland", "OR", 45.59, -122.60),
    ("PHL", "Philadelphia", "PA", 39.87, -75.24), ("PHX", "Phoenix", "AZ", 33.43, -112.01),
    ("PIT", "Pittsburgh", "PA", 40.49, -80.23), ("PNS", "Pensacola", "FL", 30.47, -87.19),
    ("RDU", "Raleigh-Durham", "NC", 35.88, -78.79), ("SAN", "San Diego", "CA", 32.73, -117.19),
    ("SAT", "San Antonio", "TX", 29.53, -98.47), ("SAV", "Savannah", "GA", 32.13, -81.20),
    ("SEA", "Seattle", "WA", 47.45, -122.31), ("SFO", "San Francisco", "CA", 37.62, -122.38),
    ("SLC", "Salt Lake City", "UT", 40.79, -111.98), ("STL", "St. Louis", "MO", 38.75, -90.37),
    ("TPA", "Tampa", "FL", 27.98, -82.53), ("VPS", "Fort Walton Beach", "FL", 30.48, -86.53),
    ("FRA", "Frankfurt", "Germany", 50.04, 8.56), ("GUM", "Guam", "GU", 13.48, 144.80),
    ("ICN", "Seoul", "South Korea", 37.46, 126.44), ("NRT", "Tokyo", "Japan", 35.77, 140.39),
    ("OKA", "Okinawa", "Japan", 26.20, 127.65), ("NAP", "Naples", "Italy", 40.88, 14.29),
]
_BY_CODE = {a[0]: a for a in AIRPORTS}

# Common installations, as places travelers type.
PLACES = {
    "wright-patterson afb": ("OH", 39.83, -84.05), "fort riley": ("KS", 39.06, -96.79),
    "fort liberty": ("NC", 35.14, -79.00), "fort bragg": ("NC", 35.14, -79.00),
    "joint base san antonio": ("TX", 29.38, -98.58), "lackland afb": ("TX", 29.38, -98.62),
    "peterson sfb": ("CO", 38.82, -104.70), "naval station norfolk": ("VA", 36.95, -76.31),
    "eglin afb": ("FL", 30.46, -86.55), "fort cavazos": ("TX", 31.14, -97.78),
    "joint base andrews": ("MD", 38.81, -76.87), "the pentagon": ("VA", 38.87, -77.06),
    "pentagon": ("VA", 38.87, -77.06), "ramstein ab": ("Germany", 49.44, 7.60),
    "camp humphreys": ("South Korea", 36.96, 127.03), "kadena ab": ("Japan", 26.36, 127.77),
    "manhattan": ("KS", 39.18, -96.57),
}

# Approximate geographic centers, used when a city isn't in the lists above.
STATES = {
    "AL": (32.8, -86.8), "AK": (64.2, -149.5), "AZ": (34.3, -111.7), "AR": (34.9, -92.4), "CA": (37.2, -119.4),
    "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5), "DC": (38.9, -77.0), "FL": (28.6, -82.4),
    "GA": (32.7, -83.4), "HI": (20.8, -156.3), "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3),
    "IA": (42.1, -93.5), "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.1, -92.0), "ME": (45.4, -69.2),
    "MD": (39.0, -76.8), "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3), "MS": (32.7, -89.7),
    "MO": (38.4, -92.5), "MT": (47.0, -109.6), "NE": (41.5, -99.8), "NV": (39.3, -116.6), "NH": (43.7, -71.6),
    "NJ": (40.2, -74.7), "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.6, -79.4), "ND": (47.5, -100.5),
    "OH": (40.3, -82.8), "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3), "UT": (39.3, -111.7),
    "VT": (44.1, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.5), "WV": (38.6, -80.6), "WI": (44.6, -89.9),
    "WY": (43.0, -107.6),
}

# Sample average regular gas prices, dollars per gallon.
GAS = {
    "AL": 2.95, "AK": 3.85, "AZ": 3.45, "AR": 2.90, "CA": 4.75, "CO": 3.15, "CT": 3.30, "DE": 3.15, "DC": 3.55,
    "FL": 3.20, "GA": 3.00, "HI": 4.60, "ID": 3.50, "IL": 3.55, "IN": 3.15, "IA": 3.00, "KS": 2.95, "KY": 3.00,
    "LA": 2.90, "ME": 3.30, "MD": 3.25, "MA": 3.25, "MI": 3.30, "MN": 3.10, "MS": 2.85, "MO": 2.95, "MT": 3.30,
    "NE": 3.05, "NV": 4.00, "NH": 3.20, "NJ": 3.20, "NM": 3.10, "NY": 3.40, "NC": 3.05, "ND": 3.05, "OH": 3.15,
    "OK": 2.85, "OR": 4.05, "PA": 3.45, "RI": 3.25, "SC": 2.95, "SD": 3.10, "TN": 2.95, "TX": 2.90, "UT": 3.40,
    "VT": 3.35, "VA": 3.10, "WA": 4.30, "WV": 3.20, "WI": 3.05, "WY": 3.15,
}


def airport_label(a):
    return "%s - %s, %s" % (a[0], a[1], a[2])


def airport_code(text):
    """``SAN``, ``san``, or a picked suggestion like ``SAN - San Diego, CA``."""
    m = re.match(r"\s*([A-Za-z]{3})\b", text or "")
    if m and m.group(1).upper() in _BY_CODE and (len(text.strip()) == 3 or " - " in text):
        return m.group(1).upper()
    return None


def _split(place):
    place = (place or "").strip()
    if "," in place:
        city, state = (p.strip() for p in place.rsplit(",", 1))
    else:
        city, state = place, ""
    return city, state


def _state_code(state):
    from .jtr import conus_code
    s = (state or "").strip()
    if s.upper() in STATES:
        return s.upper()
    return conus_code(s)


def locate(place):
    """Return (lat, lon, precision, state) for typed text, or None.
    precision is 'city' or 'state' (state center: rough estimate)."""
    code = airport_code(place)
    if code:
        a = _BY_CODE[code]
        return a[3], a[4], "city", a[2]
    city, state = _split(place)
    key = city.lower()
    st = _state_code(state) or state
    if key in PLACES:
        s, lat, lon = PLACES[key]
        return lat, lon, "city", s
    for a in AIRPORTS:
        if a[1].lower() == key and (not state or a[2].lower() == st.lower()):
            return a[3], a[4], "city", a[2]
    if not state and _state_code(city):
        st = _state_code(city)
    if st in STATES:
        lat, lon = STATES[st]
        return lat, lon, "state", st
    return None


def miles_between(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def driving_miles(start, destination):
    """Estimated one-way road miles, with precision, or (None, reason)."""
    a, b = locate(start), locate(destination)
    if a is None or b is None:
        return None, "We could not find %s. Enter the miles yourself." % (start if a is None else destination)
    precision = "city" if a[2] == b[2] == "city" else "state"
    return round(miles_between(a, b) * ROAD_FACTOR), precision


def gas_price(place):
    loc = locate(place)
    return GAS.get(loc[3], NATIONAL_GAS) if loc else NATIONAL_GAS


def nearest_airport(place):
    loc = locate(place)
    if loc is None:
        return None
    return min(AIRPORTS, key=lambda a: miles_between(loc, (a[3], a[4])))
