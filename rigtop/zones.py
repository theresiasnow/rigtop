"""CQ (DX) zone, IARU zone, and country name lookup from GPS coordinates.

Uses reverse_geocoder for offline country detection (install with: uv add reverse_geocoder).
Zone data is derived from the ARRL DXCC list and ITU zone maps.
Large countries with multiple zones use lat/lon subregion logic.
"""

from __future__ import annotations

try:
    import reverse_geocoder as _rg  # type: ignore[import-untyped]

    # Pre-initialize the KD-tree in the importing thread so that background
    # threads (e.g. Textual worker) never need to spawn a subprocess.
    # Without this, scipy's multiprocessing spawn can fail on Windows when
    # called from a non-main thread (DLL load / paging-file errors).
    _rg.search([(0.0, 0.0)], verbose=False)
    _HAS_RG = True
except Exception:
    _HAS_RG = False

# ISO 3166-1 alpha-2 → (cq_zone, iaru_zone)
# Sources: ARRL DXCC list, BigCTY, ITU zone maps.
# 0 = handled by subzone function below.
_ZONES: dict[str, tuple[int, int]] = {
    # ── Europe ──────────────────────────────────────────────────────────────
    "GB": (14, 27),
    "IE": (14, 27),  # UK, Ireland
    "IS": (40, 17),
    "FO": (14, 18),
    "GL": (40, 5),  # Iceland, Faroe, Greenland
    "NO": (14, 18),
    "SE": (14, 18),
    "DK": (14, 18),  # Norway, Sweden, Denmark
    "FI": (18, 18),  # Finland
    "EE": (15, 29),
    "LV": (15, 29),
    "LT": (15, 29),  # Baltic states
    "FR": (14, 27),
    "MC": (14, 27),
    "AD": (14, 27),  # France, Monaco, Andorra
    "ES": (14, 37),
    "PT": (14, 37),  # Spain, Portugal
    "DE": (14, 28),
    "NL": (14, 27),
    "BE": (14, 27),  # Germany, Netherlands, Belgium
    "LU": (14, 27),
    "CH": (14, 28),
    "LI": (14, 28),  # Luxembourg, Switzerland, Liechtenstein
    "AT": (15, 28),
    "IT": (15, 28),
    "SM": (15, 28),  # Austria, Italy, San Marino
    "VA": (15, 28),
    "MT": (15, 28),  # Vatican, Malta
    "PL": (15, 28),
    "CZ": (15, 28),
    "SK": (15, 28),  # Poland, Czech, Slovakia
    "HU": (15, 28),
    "SI": (15, 28),
    "HR": (15, 28),  # Hungary, Slovenia, Croatia
    "BA": (15, 28),
    "RS": (15, 28),
    "ME": (15, 28),  # Bosnia, Serbia, Montenegro
    "MK": (15, 28),
    "AL": (15, 28),
    "XK": (15, 28),  # N.Macedonia, Albania, Kosovo
    "RO": (20, 28),
    "BG": (20, 28),
    "GR": (20, 28),  # Romania, Bulgaria, Greece
    "CY": (20, 39),  # Cyprus
    "UA": (16, 29),
    "BY": (16, 29),
    "MD": (16, 29),  # Ukraine, Belarus, Moldova
    # ── Middle East ─────────────────────────────────────────────────────────
    "TR": (20, 29),
    "SY": (20, 39),
    "LB": (20, 39),  # Turkey, Syria, Lebanon
    "IL": (20, 39),
    "PS": (20, 39),
    "JO": (20, 39),  # Israel, Palestine, Jordan
    "IQ": (21, 39),
    "KW": (21, 39),
    "SA": (21, 39),  # Iraq, Kuwait, Saudi Arabia
    "AE": (21, 39),
    "QA": (21, 39),
    "BH": (21, 39),  # UAE, Qatar, Bahrain
    "OM": (21, 39),
    "YE": (21, 39),  # Oman, Yemen
    "IR": (21, 40),
    "AF": (21, 40),  # Iran, Afghanistan
    "PK": (21, 41),  # Pakistan
    # ── Central / South Asia ────────────────────────────────────────────────
    "IN": (26, 41),
    "LK": (26, 41),
    "MV": (26, 41),  # India, Sri Lanka, Maldives
    "NP": (26, 42),
    "BT": (26, 42),
    "BD": (26, 42),  # Nepal, Bhutan, Bangladesh
    "KZ": (17, 30),
    "UZ": (17, 30),
    "TM": (17, 30),  # Kazakhstan, Uzbekistan, Turkmenistan
    "KG": (17, 32),
    "TJ": (17, 32),  # Kyrgyzstan, Tajikistan
    "GE": (21, 29),
    "AM": (21, 29),
    "AZ": (21, 29),  # Georgia, Armenia, Azerbaijan
    # ── East / SE Asia ──────────────────────────────────────────────────────
    "JP": (25, 45),
    "KR": (25, 44),
    "KP": (25, 44),  # Japan, S/N Korea
    "TW": (24, 44),
    "HK": (24, 44),
    "MO": (24, 44),  # Taiwan, HK, Macau
    "MN": (23, 32),  # Mongolia
    "MM": (26, 49),
    "TH": (26, 49),
    "LA": (26, 49),  # Myanmar, Thailand, Laos
    "KH": (26, 49),
    "VN": (26, 49),  # Cambodia, Vietnam
    "MY": (28, 54),
    "SG": (28, 54),
    "BN": (28, 54),  # Malaysia, Singapore, Brunei
    "ID": (28, 54),
    "PH": (27, 50),  # Indonesia, Philippines
    # ── North America ───────────────────────────────────────────────────────
    # US=0, CA=0 → handled by subzone functions
    "MX": (6, 10),
    "GT": (7, 11),
    "BZ": (7, 11),  # Mexico, Guatemala, Belize
    "HN": (7, 11),
    "SV": (7, 11),
    "NI": (7, 11),  # Honduras, El Salvador, Nicaragua
    "CR": (7, 11),
    "PA": (7, 11),  # Costa Rica, Panama
    # ── Caribbean ───────────────────────────────────────────────────────────
    "CU": (8, 11),
    "JM": (8, 11),
    "HT": (8, 11),  # Cuba, Jamaica, Haiti
    "DO": (8, 11),
    "TT": (9, 11),
    "BB": (8, 11),  # D.Rep, Trinidad, Barbados
    "LC": (8, 11),
    "VC": (8, 11),
    "GD": (8, 11),  # St.Lucia, St.Vincent, Grenada
    "AG": (8, 11),
    "KN": (8, 11),
    "DM": (8, 11),  # Antigua, St.Kitts, Dominica
    "BS": (8, 11),  # Bahamas
    # ── South America ───────────────────────────────────────────────────────
    "CO": (9, 12),
    "VE": (9, 12),
    "GY": (9, 12),  # Colombia, Venezuela, Guyana
    "SR": (9, 12),
    "GF": (9, 12),  # Suriname, French Guiana
    "EC": (10, 12),
    "PE": (10, 12),
    "BO": (10, 14),  # Ecuador, Peru, Bolivia
    # BR=0 → handled by subzone function
    "PY": (11, 14),
    "UY": (13, 14),  # Paraguay, Uruguay
    "AR": (13, 14),
    "CL": (12, 14),  # Argentina, Chile
    "FK": (13, 16),  # Falkland Islands
    # ── Africa ──────────────────────────────────────────────────────────────
    "MA": (33, 37),
    "DZ": (33, 37),
    "TN": (33, 37),  # Morocco, Algeria, Tunisia
    "LY": (33, 38),
    "EG": (34, 38),  # Libya, Egypt
    "SD": (34, 48),
    "SS": (34, 48),
    "ET": (34, 48),  # Sudan, S.Sudan, Ethiopia
    "ER": (34, 48),
    "DJ": (34, 48),
    "SO": (34, 48),  # Eritrea, Djibouti, Somalia
    "KE": (34, 53),
    "TZ": (37, 53),
    "UG": (34, 48),  # Kenya, Tanzania, Uganda
    "RW": (36, 53),
    "BI": (36, 53),  # Rwanda, Burundi
    "MZ": (37, 53),
    "ZM": (37, 53),
    "MW": (37, 53),  # Mozambique, Zambia, Malawi
    "ZW": (38, 53),
    "BW": (38, 57),
    "NA": (38, 57),  # Zimbabwe, Botswana, Namibia
    "ZA": (38, 57),
    "SZ": (38, 57),
    "LS": (38, 57),  # S.Africa, Eswatini, Lesotho
    "MG": (39, 53),
    "MU": (39, 53),
    "RE": (39, 53),  # Madagascar, Mauritius, Réunion
    "SC": (39, 53),
    "KM": (39, 53),  # Seychelles, Comoros
    "NG": (35, 46),
    "GH": (35, 46),
    "CI": (35, 46),  # Nigeria, Ghana, Ivory Coast
    "SN": (35, 46),
    "GM": (35, 46),
    "GW": (35, 46),  # Senegal, Gambia, Guinea-Bissau
    "GN": (35, 46),
    "SL": (35, 46),
    "LR": (35, 46),  # Guinea, Sierra Leone, Liberia
    "BF": (35, 46),
    "ML": (35, 46),
    "MR": (35, 46),  # Burkina Faso, Mali, Mauritania
    "NE": (35, 46),
    "TG": (35, 46),
    "BJ": (35, 46),  # Niger, Togo, Benin
    "CM": (36, 47),
    "CF": (36, 47),
    "GQ": (36, 47),  # Cameroon, CAR, Eq.Guinea
    "GA": (36, 52),
    "CG": (36, 52),
    "CD": (36, 52),  # Gabon, Congo, DR Congo
    "AO": (37, 52),
    "TD": (35, 47),  # Angola, Chad
    # ── Oceania ─────────────────────────────────────────────────────────────
    # AU=0 → handled by subzone function
    "NZ": (32, 60),
    "PG": (28, 56),
    "SB": (28, 51),  # NZ, PNG, Solomons
    "VU": (28, 56),
    "FJ": (28, 56),
    "NC": (28, 56),  # Vanuatu, Fiji, New Caledonia
    "TO": (32, 62),
    "WS": (32, 62),
    "CK": (32, 62),  # Tonga, Samoa, Cook Is.
    "PF": (31, 63),
    "GU": (27, 64),
    "KI": (31, 61),  # Fr.Polynesia, Guam, Kiribati
    "FM": (27, 65),
    "PW": (27, 64),
    "MH": (31, 65),  # Micronesia, Palau, Marshall Is.
    "SJ": (18, 18),  # Svalbard / Jan Mayen
}

# ISO 3166-1 alpha-2 → display country name
_NAMES: dict[str, str] = {
    "AD": "Andorra",
    "AE": "UAE",
    "AF": "Afghanistan",
    "AG": "Antigua",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "AZ": "Azerbaijan",
    "BA": "Bosnia",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BN": "Brunei",
    "BO": "Bolivia",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CD": "DR Congo",
    "CF": "C.Afr. Rep.",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Ivory Coast",
    "CK": "Cook Islands",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cape Verde",
    "CY": "Cyprus",
    "CZ": "Czech Rep.",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Rep.",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FK": "Falkland Is.",
    "FM": "Micronesia",
    "FO": "Faroe Islands",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "Fr. Guiana",
    "GH": "Ghana",
    "GL": "Greenland",
    "GM": "Gambia",
    "GN": "Guinea",
    "GQ": "Eq. Guinea",
    "GR": "Greece",
    "GT": "Guatemala",
    "GU": "Guam",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KI": "Kiribati",
    "KM": "Comoros",
    "KN": "St. Kitts",
    "KP": "N. Korea",
    "KR": "S. Korea",
    "KW": "Kuwait",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LC": "St. Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MG": "Madagascar",
    "MH": "Marshall Is.",
    "MK": "N. Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macau",
    "MR": "Mauritania",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NC": "New Caledonia",
    "NE": "Niger",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PF": "Fr. Polynesia",
    "PG": "Papua NG",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PS": "Palestine",
    "PT": "Portugal",
    "PW": "Palau",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RE": "Réunion",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Is.",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SJ": "Svalbard",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SM": "San Marino",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "S. Sudan",
    "SV": "El Salvador",
    "SY": "Syria",
    "SZ": "Eswatini",
    "TD": "Chad",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TL": "East Timor",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkey",
    "TT": "Trinidad",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Vatican",
    "VC": "St. Vincent",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "VU": "Vanuatu",
    "WS": "Samoa",
    "XK": "Kosovo",
    "YE": "Yemen",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


# ---------------------------------------------------------------------------
# Subzone functions for large multi-zone countries
# ---------------------------------------------------------------------------


def _us_zones(lat: float, lon: float) -> tuple[int, int]:
    if lat > 54 and lon < -130:  # Alaska
        return (1, 1)
    if lat < 25 and lon < -150:  # Hawaii
        return (31, 61)
    # Contiguous US — approximate by longitude
    if lon <= -115:
        return (3, 6)  # Pacific (WA, OR, CA, NV, ID, MT, WY)
    if lon <= -100:
        return (4, 7)  # Mountain / SW (CO, UT, AZ, NM, ND, SD, NE, KS, TX west)
    if lon <= -85:
        return (5, 8)  # Central (TX, OK, MO, AR, LA, MS, IA, MN, WI, IL, IN, MI, OH)
    return (5, 8)  # East (KY, TN, AL, GA, FL, SC, NC, VA, WV and NE states)


def _ca_zones(lat: float, lon: float) -> tuple[int, int]:
    if lat > 70 or lon < -120:  # Arctic / Yukon / NWT west
        return (1, 2)
    if lat > 60:  # NWT south, NU south, northern QC
        return (2, 3)
    if lon <= -110:  # BC, AB, SK west
        return (3, 4)
    if lon <= -85:  # SK east, MB, ON west
        return (4, 4)
    return (5, 9)  # ON south, QC, Maritimes, NL


def _ru_zones(lat: float, lon: float) -> tuple[int, int]:
    if lon < 40:
        return (16, 29)  # European Russia (Kaliningrad, etc.)
    if lon < 60:
        return (16, 30)  # European Russia east
    if lon < 75:
        return (17, 30)  # West Siberia
    if lon < 90:
        return (17, 31)  # Central Siberia west
    if lon < 110:
        return (18, 32)  # Central Siberia east / Baikal
    if lon < 135:
        return (19, 32)  # East Siberia
    if lon < 150:
        return (19, 33)  # Far East (Magadan)
    return (25, 34)  # Chukotka / Kamchatka


def _au_zones(lat: float, lon: float) -> tuple[int, int]:
    if lon < 129:
        return (29, 58)  # WA
    if lon < 138:
        return (29, 55)  # NT / SA west
    if lat < -30 and lon > 149:
        return (30, 60)  # TAS / VIC / NSW south
    return (29, 59)  # QLD / NSW north / SA east


def _br_zones(lat: float, lon: float) -> tuple[int, int]:
    if lat > 0:
        return (9, 12)  # Northern Brazil (Amapá, Roraima, Pará north)
    if lon < -60:
        return (10, 12)  # Western Brazil (Amazonas, Acre, Rondônia, Mato Grosso)
    return (11, 15)  # Eastern / Southern Brazil


def _cn_zones(lat: float, lon: float) -> tuple[int, int]:
    if lon < 100:
        return (24, 41)  # Xinjiang / Tibet west
    if lon < 115:
        return (24, 42)  # Central China (Sichuan, Yunnan, Gansu)
    if lat > 45:
        return (24, 33)  # Manchuria / Inner Mongolia north
    return (24, 44)  # East China


_SUBZONE: dict[str, object] = {
    "US": _us_zones,
    "CA": _ca_zones,
    "RU": _ru_zones,
    "AU": _au_zones,
    "BR": _br_zones,
    "CN": _cn_zones,
}


# ---------------------------------------------------------------------------
# Cache (coarse 0.5° grid to avoid redundant reverse-geocoder calls)
# ---------------------------------------------------------------------------

_cache: dict[tuple[int, int], dict] = {}


def _grid_key(lat: float, lon: float) -> tuple[int, int]:
    return (int(lat * 2), int(lon * 2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lookup(lat: float, lon: float) -> dict:
    """Return location info for the given coordinates.

    Returns a dict with keys: ``cc``, ``country``, ``cq``, ``iaru``.
    Values are strings; zones are formatted as e.g. ``"14"`` or ``"?"`` if unknown.
    """
    key = _grid_key(lat, lon)
    if key in _cache:
        return _cache[key]

    cc, country = _get_country(lat, lon)

    # Zone lookup — subzone function takes priority for multi-zone countries
    fn = _SUBZONE.get(cc)
    if fn is not None:
        cq, iaru = fn(lat, lon)  # type: ignore[call-arg]
    else:
        cq, iaru = _ZONES.get(cc, (0, 0))

    result = {
        "cc": cc,
        "country": country,
        "cq": str(cq) if cq else "?",
        "iaru": str(iaru) if iaru else "?",
    }
    _cache[key] = result
    return result


def _get_country(lat: float, lon: float) -> tuple[str, str]:
    """Return (ISO-2 code, display name). Falls back to empty strings."""
    if _HAS_RG:
        try:
            results = _rg.search([(lat, lon)], verbose=False)
            if results:
                cc = results[0].get("cc", "")
                name = _NAMES.get(cc, cc)
                return cc, name
        except Exception:
            pass
    return "", ""
