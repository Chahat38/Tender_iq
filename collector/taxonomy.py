"""Deterministic geography and sector inference for collected tenders.

The Pakistani portals fill ``location``, ``province``, ``sector`` and
``department`` inconsistently (most adapters only know the jurisdiction,
some know a city, none classify the work). That left the browse filters
looking broken: a province dropdown built from stored values had a couple
of entries, the city list was nearly empty and the sector/department
facets had no data at all.

This module closes that gap with closed vocabularies:

* a list of Pakistani cities mapped to their province,
* province names plus the common aliases used by portals,
* keyword rules for the procurement sector and the procuring department.

Everything is derived from text the source already states (title,
organization, description, scraped location). Nothing is invented when
the text says nothing: :func:`derive_taxonomy` returns an empty mapping
in that case, and existing adapter-supplied values are never overwritten.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models import NormalizedOpportunity

# ---------------------------------------------------------------------------
# Provinces
# ---------------------------------------------------------------------------

PROVINCE_NAMES: tuple[str, ...] = (
    "Punjab",
    "Sindh",
    "Khyber Pakhtunkhwa",
    "Balochistan",
    "Gilgit-Baltistan",
    "Azad Jammu & Kashmir",
    "Islamabad Capital Territory",
)

_PROVINCE_ALIASES: dict[str, str] = {
    "punjab": "Punjab",
    "sindh": "Sindh",
    "sind": "Sindh",
    "khyber pakhtunkhwa": "Khyber Pakhtunkhwa",
    "khyber-pakhtunkhwa": "Khyber Pakhtunkhwa",
    "khyber pukhtunkhwa": "Khyber Pakhtunkhwa",
    "kpk": "Khyber Pakhtunkhwa",
    "kp": "Khyber Pakhtunkhwa",
    "nwfp": "Khyber Pakhtunkhwa",
    "balochistan": "Balochistan",
    "baluchistan": "Balochistan",
    "gilgit-baltistan": "Gilgit-Baltistan",
    "gilgit baltistan": "Gilgit-Baltistan",
    "gilgit": "Gilgit-Baltistan",
    "azad jammu & kashmir": "Azad Jammu & Kashmir",
    "azad jammu and kashmir": "Azad Jammu & Kashmir",
    "azad kashmir": "Azad Jammu & Kashmir",
    "ajk": "Azad Jammu & Kashmir",
    "aj&k": "Azad Jammu & Kashmir",
    "islamabad capital territory": "Islamabad Capital Territory",
    "islamabad": "Islamabad Capital Territory",
    "ict": "Islamabad Capital Territory",
}

# ---------------------------------------------------------------------------
# Cities (district capitals and other tendering hubs)
# ---------------------------------------------------------------------------

CITY_PROVINCE: dict[str, str] = {
    # Punjab
    "Lahore": "Punjab",
    "Faisalabad": "Punjab",
    "Rawalpindi": "Punjab",
    "Gujranwala": "Punjab",
    "Multan": "Punjab",
    "Sialkot": "Punjab",
    "Bahawalpur": "Punjab",
    "Sargodha": "Punjab",
    "Sheikhupura": "Punjab",
    "Jhelum": "Punjab",
    "Rahim Yar Khan": "Punjab",
    "Kasur": "Punjab",
    "Sahiwal": "Punjab",
    "Okara": "Punjab",
    "Gujrat": "Punjab",
    "Hafizabad": "Punjab",
    "Khanewal": "Punjab",
    "Muzaffargarh": "Punjab",
    "Mandi Bahauddin": "Punjab",
    "Jhang": "Punjab",
    "Vehari": "Punjab",
    "Chiniot": "Punjab",
    "Dera Ghazi Khan": "Punjab",
    "Layyah": "Punjab",
    "Bhakkar": "Punjab",
    "Mianwali": "Punjab",
    "Nankana Sahib": "Punjab",
    "Toba Tek Singh": "Punjab",
    "Bahawalnagar": "Punjab",
    "Rajanpur": "Punjab",
    "Pakpattan": "Punjab",
    "Attock": "Punjab",
    "Chakwal": "Punjab",
    "Khushab": "Punjab",
    "Narowal": "Punjab",
    "Lodhran": "Punjab",
    "Khanpur": "Punjab",
    "Ahmadpur East": "Punjab",
    "Kamoke": "Punjab",
    "Muridke": "Punjab",
    "Wazirabad": "Punjab",
    "Gojra": "Punjab",
    "Burewala": "Punjab",
    "Jaranwala": "Punjab",
    "Shorkot": "Punjab",
    "Kot Addu": "Punjab",
    "Taunsa": "Punjab",
    "Fort Abbas": "Punjab",
    "Yazman": "Punjab",
    "Hasilpur": "Punjab",
    "Arifwala": "Punjab",
    "Renala Khurd": "Punjab",
    "Daska": "Punjab",
    "Pasrur": "Punjab",
    "Sambrial": "Punjab",
    "Kamalia": "Punjab",
    "Pir Mahal": "Punjab",
    "Mailsi": "Punjab",
    "Bhalwal": "Punjab",
    "Kot Momin": "Punjab",
    "Bhera": "Punjab",
    "Pind Dadan Khan": "Punjab",
    "Dina": "Punjab",
    "Alipur Chatha": "Punjab",
    "Jatoi": "Punjab",
    "Jand": "Punjab",
    "Pindi Bhattian": "Punjab",
    "Sillanwali": "Punjab",
    "Kot Radha Kishan": "Punjab",
    "Chunian": "Punjab",
    "Depalpur": "Punjab",
    "Minchinabad": "Punjab",
    "Liaquatpur": "Punjab",
    "Sadiqabad": "Punjab",
    "Uch Sharif": "Punjab",
    "Mian Channu": "Punjab",
    "Abdul Hakeem": "Punjab",
    "Kabirwala": "Punjab",
    "Shujaabad": "Punjab",
    "Jalalpur Pirwala": "Punjab",
    "Chichawatni": "Punjab",
    "Bhawana": "Punjab",
    "Pattoki": "Punjab",
    "Kanganpur": "Punjab",
    "Kharian": "Punjab",
    "Sarai Alamgir": "Punjab",
    "Gujar Khan": "Punjab",
    "Kahuta": "Punjab",
    "Kallar Kahar": "Punjab",
    "Choa Saidan Shah": "Punjab",
    "Talagang": "Punjab",
    "Hasan Abdal": "Punjab",
    "Fateh Jang": "Punjab",
    "Hazro": "Punjab",
    "Nowshera Virkan": "Punjab",
    "Taxila": "Punjab",
    "Wah Cantonment": "Punjab",
    "Ferozewala": "Punjab",
    "Raiwind": "Punjab",
    "Jhawarian": "Punjab",
    "Ahmadpur Sial": "Punjab",
    "Shakargarh": "Punjab",
    "Zafarwal": "Punjab",
    "Karor Lal Esan": "Punjab",
    "Kallur Kot": "Punjab",
    "Darya Khan": "Punjab",
    "Hujra Shah Muqeem": "Punjab",
    "Kotli Loharan": "Punjab",
    # Sindh
    "Karachi": "Sindh",
    "Hyderabad": "Sindh",
    "Sukkur": "Sindh",
    "Larkana": "Sindh",
    "Nawabshah": "Sindh",
    "Shaheed Benazirabad": "Sindh",
    "Mirpur Khas": "Sindh",
    "Jacobabad": "Sindh",
    "Shikarpur": "Sindh",
    "Khairpur": "Sindh",
    "Dadu": "Sindh",
    "Thatta": "Sindh",
    "Badin": "Sindh",
    "Ghotki": "Sindh",
    "Naushahro Feroze": "Sindh",
    "Sanghar": "Sindh",
    "Umerkot": "Sindh",
    "Mithi": "Sindh",
    "Tharparkar": "Sindh",
    "Kashmore": "Sindh",
    "Kandhkot": "Sindh",
    "Tando Allahyar": "Sindh",
    "Tando Muhammad Khan": "Sindh",
    "Matiari": "Sindh",
    "Jamshoro": "Sindh",
    "Sehwan": "Sindh",
    "Kotri": "Sindh",
    "Rohri": "Sindh",
    "Pano Aqil": "Sindh",
    "Ratodero": "Sindh",
    "Kambar": "Sindh",
    "Mehar": "Sindh",
    "Moro": "Sindh",
    "Diplo": "Sindh",
    "Islamkot": "Sindh",
    "Chachro": "Sindh",
    "Daharki": "Sindh",
    "Ghauspur": "Sindh",
    "Thul": "Sindh",
    "Sijawal": "Sindh",
    "Dokri": "Sindh",
    "Warah": "Sindh",
    "Bhiria": "Sindh",
    "Kandiaro": "Sindh",
    "Halani": "Sindh",
    "Tando Jam": "Sindh",
    "Sann": "Sindh",
    "Manjhand": "Sindh",
    "Johi": "Sindh",
    "Khairpur Nathan Shah": "Sindh",
    "Bulri Shah Karim": "Sindh",
    "Golarchi": "Sindh",
    "Talhar": "Sindh",
    "Sujawal": "Sindh",
    "Mirpur Bathoro": "Sindh",
    "Jati": "Sindh",
    "Shahdadpur": "Sindh",
    "Sinjhoro": "Sindh",
    "Tando Adam": "Sindh",
    "Sakrand": "Sindh",
    "Kazi Ahmed": "Sindh",
    "Naudero": "Sindh",
    "Gambat": "Sindh",
    "Kingri": "Sindh",
    "Sobhodero": "Sindh",
    "Madeji": "Sindh",
    "Bagarji": "Sindh",
    "Karoondi": "Sindh",
    "Nara": "Sindh",
    "Aamri": "Sindh",
    "Jhol": "Sindh",
    "Bhit Shah": "Sindh",
    "Hala": "Sindh",
    "Saeedabad": "Sindh",
    "Khipro": "Sindh",
    "Jam Nawaz Ali": "Sindh",
    "Kot Ghulam Muhammad": "Sindh",
    "Chhor": "Sindh",
    "Samaro": "Sindh",
    "Kunri": "Sindh",
    # Khyber Pakhtunkhwa
    "Peshawar": "Khyber Pakhtunkhwa",
    "Mardan": "Khyber Pakhtunkhwa",
    "Mingora": "Khyber Pakhtunkhwa",
    "Swat": "Khyber Pakhtunkhwa",
    "Abbottabad": "Khyber Pakhtunkhwa",
    "Mansehra": "Khyber Pakhtunkhwa",
    "Kohat": "Khyber Pakhtunkhwa",
    "Bannu": "Khyber Pakhtunkhwa",
    "Dera Ismail Khan": "Khyber Pakhtunkhwa",
    "Charsadda": "Khyber Pakhtunkhwa",
    "Nowshera": "Khyber Pakhtunkhwa",
    "Swabi": "Khyber Pakhtunkhwa",
    "Haripur": "Khyber Pakhtunkhwa",
    "Battagram": "Khyber Pakhtunkhwa",
    "Dir": "Khyber Pakhtunkhwa",
    "Chitral": "Khyber Pakhtunkhwa",
    "Timergara": "Khyber Pakhtunkhwa",
    "Parachinar": "Khyber Pakhtunkhwa",
    "Hangu": "Khyber Pakhtunkhwa",
    "Karak": "Khyber Pakhtunkhwa",
    "Lakki Marwat": "Khyber Pakhtunkhwa",
    "Tank": "Khyber Pakhtunkhwa",
    "Buner": "Khyber Pakhtunkhwa",
    "Shangla": "Khyber Pakhtunkhwa",
    "Malakand": "Khyber Pakhtunkhwa",
    "Batkhela": "Khyber Pakhtunkhwa",
    "Jamrud": "Khyber Pakhtunkhwa",
    "Landi Kotal": "Khyber Pakhtunkhwa",
    "Wana": "Khyber Pakhtunkhwa",
    "Bara": "Khyber Pakhtunkhwa",
    "Daggar": "Khyber Pakhtunkhwa",
    "Alpuri": "Khyber Pakhtunkhwa",
    "Khar": "Khyber Pakhtunkhwa",
    "Samar Bagh": "Khyber Pakhtunkhwa",
    "Madyan": "Khyber Pakhtunkhwa",
    "Bahrain": "Khyber Pakhtunkhwa",
    "Kalam": "Khyber Pakhtunkhwa",
    "Thall": "Khyber Pakhtunkhwa",
    "Havelian": "Khyber Pakhtunkhwa",
    "Ghazi": "Khyber Pakhtunkhwa",
    "Topi": "Khyber Pakhtunkhwa",
    "Takht Bhai": "Khyber Pakhtunkhwa",
    "Zaida": "Khyber Pakhtunkhwa",
    "Razzar": "Khyber Pakhtunkhwa",
    "Shabqadar": "Khyber Pakhtunkhwa",
    "Tangi": "Khyber Pakhtunkhwa",
    "Pabbi": "Khyber Pakhtunkhwa",
    "Akora Khattak": "Khyber Pakhtunkhwa",
    "Jahangira": "Khyber Pakhtunkhwa",
    "Dargai": "Khyber Pakhtunkhwa",
    "Kulachi": "Khyber Pakhtunkhwa",
    "Paharpur": "Khyber Pakhtunkhwa",
    "Daraban": "Khyber Pakhtunkhwa",
    "Miran Shah": "Khyber Pakhtunkhwa",
    "Razmak": "Khyber Pakhtunkhwa",
    "Mir Ali": "Khyber Pakhtunkhwa",
    "Khurram": "Khyber Pakhtunkhwa",
    # Balochistan
    "Quetta": "Balochistan",
    "Gwadar": "Balochistan",
    "Turbat": "Balochistan",
    "Khuzdar": "Balochistan",
    "Sibi": "Balochistan",
    "Zhob": "Balochistan",
    "Loralai": "Balochistan",
    "Chaman": "Balochistan",
    "Dera Murad Jamali": "Balochistan",
    "Jaffarabad": "Balochistan",
    "Usta Muhammad": "Balochistan",
    "Hub": "Balochistan",
    "Lasbela": "Balochistan",
    "Bela": "Balochistan",
    "Panjgur": "Balochistan",
    "Kalat": "Balochistan",
    "Mastung": "Balochistan",
    "Nushki": "Balochistan",
    "Dalbandin": "Balochistan",
    "Chagai": "Balochistan",
    "Kharan": "Balochistan",
    "Washuk": "Balochistan",
    "Barkhan": "Balochistan",
    "Kohlu": "Balochistan",
    "Dera Bugti": "Balochistan",
    "Awaran": "Balochistan",
    "Musakhel": "Balochistan",
    "Sherani": "Balochistan",
    "Surab": "Balochistan",
    "Pishin": "Balochistan",
    "Qila Abdullah": "Balochistan",
    "Qila Saifullah": "Balochistan",
    "Muslim Bagh": "Balochistan",
    "Ziarat": "Balochistan",
    "Harnai": "Balochistan",
    "Duki": "Balochistan",
    "Gandawa": "Balochistan",
    "Jhal Magsi": "Balochistan",
    "Nasirabad": "Balochistan",
    "Tamboo": "Balochistan",
    "Buleda": "Balochistan",
    "Mand": "Balochistan",
    "Tump": "Balochistan",
    "Gichk": "Balochistan",
    "Ormara": "Balochistan",
    "Pasni": "Balochistan",
    "Sonmiani": "Balochistan",
    "Sui": "Balochistan",
    # Gilgit-Baltistan
    "Gilgit": "Gilgit-Baltistan",
    "Skardu": "Gilgit-Baltistan",
    "Hunza": "Gilgit-Baltistan",
    "Karimabad": "Gilgit-Baltistan",
    "Chilas": "Gilgit-Baltistan",
    "Khaplu": "Gilgit-Baltistan",
    "Astore": "Gilgit-Baltistan",
    "Gahkuch": "Gilgit-Baltistan",
    "Chalt": "Gilgit-Baltistan",
    "Nagar": "Gilgit-Baltistan",
    "Shigar": "Gilgit-Baltistan",
    "Kharmang": "Gilgit-Baltistan",
    "Danyore": "Gilgit-Baltistan",
    "Aliabad": "Gilgit-Baltistan",
    "Sust": "Gilgit-Baltistan",
    # Azad Jammu & Kashmir
    "Muzaffarabad": "Azad Jammu & Kashmir",
    "Mirpur": "Azad Jammu & Kashmir",
    "Bhimber": "Azad Jammu & Kashmir",
    "Kotli": "Azad Jammu & Kashmir",
    "Rawalakot": "Azad Jammu & Kashmir",
    "Bagh": "Azad Jammu & Kashmir",
    "Haveli": "Azad Jammu & Kashmir",
    "Neelum": "Azad Jammu & Kashmir",
    "Athmuqam": "Azad Jammu & Kashmir",
    "Sudhnoti": "Azad Jammu & Kashmir",
    "Pallandri": "Azad Jammu & Kashmir",
    "Poonch": "Azad Jammu & Kashmir",
    "Hajira": "Azad Jammu & Kashmir",
    "Samahni": "Azad Jammu & Kashmir",
    "Dadyal": "Azad Jammu & Kashmir",
    "Chakswari": "Azad Jammu & Kashmir",
    # Islamabad Capital Territory
    "Islamabad": "Islamabad Capital Territory",
}

#: Names too ambiguous to infer a location from free text ("water tank",
#: "shopping bag", "hub"). They stay in the dropdown (users pick them
#: explicitly) but never trigger automatic inference.
_AMBIGUOUS_CITIES = frozenset(
    {
        "Tank",
        "Bagh",
        "Hub",
        "Bela",
        "Mand",
        "Sui",
        "Khar",
        "Dir",
        "Hala",
        "Moro",
        "Nara",
        "Dina",
        "Sann",
    }
)


def _word_pattern(name: str) -> re.Pattern[str]:
    """Match ``name`` as whole words, case-insensitively."""
    return re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", re.I)


#: Longest names first so "Mirpur Khas" wins over "Mirpur" and
#: "Tando Muhammad Khan" over "Tando".
_CITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, _word_pattern(name))
    for name in sorted(
        (name for name in CITY_PROVINCE if name not in _AMBIGUOUS_CITIES),
        key=len,
        reverse=True,
    )
)

#: Longest aliases first for the same reason ("azad jammu & kashmir"
#: before "kashmir").
_PROVINCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (canonical, _word_pattern(alias))
    for alias, canonical in sorted(
        _PROVINCE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    )
    if alias not in {"kp", "ict"}  # too short/ambiguous for free text
)


def provinces_for_filter() -> list[str]:
    """Canonical province names, in display order."""
    return list(PROVINCE_NAMES)


def all_cities() -> list[str]:
    """Every known city, in alphabetical (case-insensitive) order."""
    return sorted(CITY_PROVINCE, key=str.lower)


def normalize_province(value: str | None) -> str | None:
    """Map a stored/alias province spelling onto its canonical name.

    Exact aliases win; free text such as ``"Punjab, Pakistan"`` falls
    back to whole-word province matching so facet values fold together.
    """
    key = (value or "").strip().lower()
    canonical = _PROVINCE_ALIASES.get(key)
    if canonical:
        return canonical
    return find_province(value)


def find_city(text: str | None) -> str | None:
    """Return the first known city mentioned as a whole word, if any."""
    if not text:
        return None
    for name, pattern in _CITY_PATTERNS:
        if pattern.search(text):
            return name
    return None


def find_province(text: str | None) -> str | None:
    """Return the first province/province-alias mentioned, if any."""
    if not text:
        return None
    for canonical, pattern in _PROVINCE_PATTERNS:
        if pattern.search(text):
            return canonical
    return None


# ---------------------------------------------------------------------------
# Sector / department keyword rules (first match wins)
# ---------------------------------------------------------------------------

SECTOR_LABELS: tuple[str, ...] = (
    "IT & Telecom",
    "Construction & Works",
    "Energy & Power",
    "Water & Sanitation",
    "Health & Medical",
    "Education & Training",
    "Transport & Vehicles",
    "Agriculture & Livestock",
    "Defence & Security",
    "Consulting & Professional Services",
    "Office Supplies & Furniture",
    "Machinery & Equipment",
    "Food & Catering",
    "Textiles & Uniforms",
    "Media & Advertising",
    "Real Estate & Facilities",
    "Financial Services",
    "Environment & Climate",
    "Mining & Minerals",
    "Other",
)

#: Category labels this module can derive from the tender's own text.
#: They extend the scraped category vocabulary (which is source-specific)
#: with the business areas the browse UI asks for; an existing stored
#: category is never overwritten.
CATEGORY_LABELS: tuple[str, ...] = (
    "Equipment & IT Infrastructure",
    "Software Consultancy",
    "HR & Consultancy",
    "Security & Surveillance",
)

_CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        (
            "Security & Surveillance",
            r"\b(?:securit\w*|surveillance|cctv|camera|guard\w*|watchman|"
            r"security system\w*|access control|security services)\b",
        ),
        (
            "Software Consultancy",
            r"\b(?:software|web (?:site|portal|application)|web development|"
            r"app development|source code|saas|erp|software house|"
            r"software consultancy|it consultancy|it solution\w*)\b",
        ),
        (
            "HR & Consultancy",
            r"\b(?:human resource|\bhr\b|manpower|recruitment|staffing|"
            r"personnel|secondment|headhunting|outsource\w* staff|"
            r"consultancy services|consultancy firm|consulting services)\b",
        ),
        (
            "Equipment & IT Infrastructure",
            r"\b(?:it (?:equipment|infrastructure|hardware)|"
            r"information technology infrastructure|laptops?|desktops?|"
            r"computers?|computeri[sz]ed|servers?|network (?:equipment|"
            r"switches|routers)|switches and routers|printers?|scanners?|"
            r"\bups\b|data cent(?:er|re)|hardware)\b",
        ),
    )
)

_DEPARTMENT_LABELS: tuple[str, ...] = (
    "UNDP",
    "Information Technology",
    "Health",
    "Education",
    "Water & Sanitation",
    "Works & Services",
    "Energy & Power",
    "Transport",
    "Agriculture",
    "Irrigation",
    "Finance & Revenue",
    "Defence & Security",
    "Local Government",
    "Planning & Development",
    "Law & Justice",
    "Foreign Affairs",
    "Environment",
    "Housing & Urban Development",
    "Industries & Commerce",
    "Social Welfare",
    "Information & Broadcasting",
    "Mines & Minerals",
    "Labour",
    "Tourism",
    "Religious Affairs",
    "Legislature",
)

_SECTOR_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        (
            "IT & Telecom",
            r"\b(?:information technology|ict|it services|software|hardware|"
            r"computers?|laptops?|networking|network switches?|internet|"
            r"telecom\w*|mobile phones?|data cent(?:er|re)|websites?|web portal|"
            r"digitali[sz]ation|cyber\w*|gis|mis|erp|scada|cctv)\b",
        ),
        (
            "Health & Medical",
            r"\b(?:medical|medicines?|hospitals?|health|surgical|laborator\w*|"
            r"vaccines?|ambulances?|dental|pharmaceutical\w*|drugs?)\b",
        ),
        (
            "Education & Training",
            r"\b(?:education|schools?|colleges?|universit\w*|books?|training|"
            r"scholarships?|teaching|curriculum)\b",
        ),
        (
            "Water & Sanitation",
            r"\b(?:water supply|water|sewerage|sanitation|drainage|tube ?wells?|"
            r"filtration|desalination|irrigation|canals?)\b",
        ),
        (
            "Energy & Power",
            r"\b(?:electricity|electrical|electric|power|solar|wind|turbines?|"
            r"transformers?|grid|energy|gas|lpg|petroleum|oil|diesel|"
            r"generators?|ups)\b",
        ),
        (
            "Construction & Works",
            r"\b(?:construction|civil works?|buildings?|roads?|bridges?|"
            r"culverts?|drains?|renovation|rehabilitation|repairs?|maintenance|"
            r"boundary walls?|sheds?|pavements?|plazas?|towers?|works?)\b",
        ),
        (
            "Transport & Vehicles",
            r"\b(?:vehicles?|buses?|trucks?|cars?|motorcycles?|fleet|transport|"
            r"tyres?|tires?|aviation|airports?)\b",
        ),
        (
            "Agriculture & Livestock",
            r"\b(?:agriculture|seeds?|fertilizers?|pesticides?|livestock|"
            r"veterinary|dairy|crops?|fisheries)\b",
        ),
        (
            "Defence & Security",
            r"\b(?:defence|defense|weapons?|arms|ammunition|police|army|"
            r"military|security|guards?)\b",
        ),
        (
            "Consulting & Professional Services",
            r"\b(?:consultancy|consulting|consultants?|advisory|studies|study|"
            r"feasibility|design|surveys?|research|audit|legal)\b",
        ),
        (
            "Office Supplies & Furniture",
            r"\b(?:furniture|stationery|printing|toner|paper|chairs?|tables?|"
            r"cupboards?)\b",
        ),
        (
            "Machinery & Equipment",
            r"\b(?:machinery|machines?|equipment|tools?|pumps?)\b",
        ),
        (
            "Food & Catering",
            r"\b(?:food|catering|kitchen|rations?|meals?|grocery)\b",
        ),
        (
            "Textiles & Uniforms",
            r"\b(?:textile|uniforms?|cloth|fabric|garments?)\b",
        ),
        (
            "Media & Advertising",
            r"\b(?:media|advertising|advertisements?|publicity|branding)\b",
        ),
        (
            "Real Estate & Facilities",
            r"\b(?:rent|rental|lease|property|land|accommodation|hostel)\b",
        ),
        (
            "Financial Services",
            r"\b(?:insurance|banking|financial|loans?)\b",
        ),
        (
            "Environment & Climate",
            r"\b(?:environment|climate|wildlife|forestry|conservation)\b",
        ),
        (
            "Mining & Minerals",
            r"\b(?:mining|mines?|minerals?|geological)\b",
        ),
        ("Other", r"\b(?:other|miscellaneous)\b"),
    )
)

_DEPARTMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        ("UNDP", r"\bundp\b|\bunited nations\b"),
        (
            "Information Technology",
            r"\b(?:information technology|it board|nadra|pta|e-government|"
            r"information & technology)\b",
        ),
        (
            "Health",
            r"\b(?:health|hospitals?|medical|dhq|thq|phc|bhu|doctors?)\b",
        ),
        (
            "Education",
            r"\b(?:education|schools?|colleges?|universit\w*|hec|"
            r"technical education)\b",
        ),
        (
            "Water & Sanitation",
            r"\b(?:water|sewerage|sanitation|wasa|drainage|"
            r"public health engineering|phe)\b",
        ),
        (
            "Works & Services",
            r"\b(?:works|services|c&w|communication|highways?|roads?|"
            r"buildings?|public works)\b",
        ),
        (
            "Energy & Power",
            r"\b(?:energy|power|electricity|wapda|lesco|gepco|iesco|fesco|"
            r"hesco|sepco|qesco|pesco|tesco|mepco|nuclear|solar)\b",
        ),
        (
            "Transport",
            r"\b(?:transport|railways?|motorway|nha|ports?|airports?|aviation)\b",
        ),
        (
            "Agriculture",
            r"\b(?:agriculture|livestock|veterinary|forests?|fisheries)\b",
        ),
        ("Irrigation", r"\b(?:irrigation|canals?)\b"),
        (
            "Finance & Revenue",
            r"\b(?:finance|treasury|accountant general|audit|revenue|tax|"
            r"excise)\b",
        ),
        (
            "Defence & Security",
            r"\b(?:defence|defense|army|ghq|navy|air force|rangers|police|"
            r"security)\b",
        ),
        (
            "Local Government",
            r"\b(?:municipal|local government|city district|union council|"
            r"tehsil|metropolitan|town committee)\b",
        ),
        ("Planning & Development", r"\b(?:planning|development authority|p&d)\b"),
        (
            "Law & Justice",
            r"\b(?:law|justice|courts?|judicial|legal)\b",
        ),
        ("Foreign Affairs", r"\b(?:foreign affairs|embassy|consulate)\b"),
        (
            "Environment",
            r"\b(?:environment|climate|epa|wildlife)\b",
        ),
        (
            "Housing & Urban Development",
            r"\b(?:housing|urban|estate|pha|development authority)\b",
        ),
        (
            "Industries & Commerce",
            r"\b(?:industries|commerce|industrial|investment|trade)\b",
        ),
        (
            "Social Welfare",
            r"\b(?:social welfare|bait-ul-mal|zakat|women development|"
            r"child protection)\b",
        ),
        (
            "Information & Broadcasting",
            r"\b(?:information|broadcasting|press|media)\b",
        ),
        ("Mines & Minerals", r"\b(?:mines|minerals|geological)\b"),
        ("Labour", r"\b(?:labour|labor|workers)\b"),
        ("Tourism", r"\b(?:tourism|tourist)\b"),
        ("Religious Affairs", r"\b(?:religious|auqaf)\b"),
        (
            "Legislature",
            r"\b(?:national assembly|provincial assembly|senate|parliament|"
            r"secretariat)\b",
        ),
    )
)


def _first_match(
    text: str | None, rules: Iterable[tuple[str, re.Pattern[str]]]
) -> str | None:
    if not text:
        return None
    for label, pattern in rules:
        if pattern.search(text):
            return label
    return None


def _clean(value: str | None) -> str:
    return (value or "").strip()


def derive_taxonomy(
    *,
    title: str | None = None,
    organization: str | None = None,
    description: str | None = None,
    location: str | None = None,
    province: str | None = None,
    sector: str | None = None,
    department: str | None = None,
    category: str | None = None,
) -> dict[str, str]:
    """Derive missing geography/sector/category fields from stated text.

    Returns only the fields that should be filled in; an existing value is
    never replaced, and an empty mapping means "nothing derivable".
    """
    title = title or ""
    organization = organization or ""
    description = description or ""
    text = " ".join(part for part in (title, organization, description) if part)

    changes: dict[str, str] = {}

    current_location = _clean(location)
    city = find_city(current_location) if current_location else None
    if not current_location:
        city = find_city(text)
        if city:
            changes["location"] = city

    if not _clean(province):
        derived_province = None
        if city:
            derived_province = CITY_PROVINCE.get(city)
        if derived_province is None:
            derived_province = find_province(text)
        if derived_province is None:
            derived_province = find_province(current_location)
        if derived_province:
            changes["province"] = derived_province

    if not _clean(sector):
        derived_sector = _first_match(text, _SECTOR_RULES)
        if derived_sector:
            changes["sector"] = derived_sector

    if not _clean(department):
        derived_department = _first_match(organization, _DEPARTMENT_RULES)
        if derived_department is None:
            derived_department = _first_match(text, _DEPARTMENT_RULES)
        if derived_department:
            changes["department"] = derived_department

    if not _clean(category):
        derived_category = _first_match(text, _CATEGORY_RULES)
        if derived_category:
            changes["category"] = derived_category

    return changes


def enrich_taxonomy(opportunity: "NormalizedOpportunity") -> "NormalizedOpportunity":
    """Return ``opportunity`` with derivable taxonomy fields filled in.

    Adapter-supplied values always win; the returned object is a copy when
    something changed and the same object otherwise.
    """
    changes = derive_taxonomy(
        title=opportunity.title,
        organization=opportunity.organization,
        description=opportunity.description,
        location=opportunity.location,
        province=opportunity.province,
        sector=opportunity.sector,
        department=opportunity.department,
        category=opportunity.category,
    )
    if not changes:
        return opportunity
    return opportunity.model_copy(update=changes)


def backfill(storage: Any) -> int:
    """Fill taxonomy fields on already-stored opportunities.

    Used by the ``--backfill-taxonomy`` CLI mode so an existing database
    (collected before the fields existed) gains city/province/sector/
    department values without re-fetching any source. Returns the number
    of rows that received at least one value.
    """
    updated = 0
    for row in storage.iter_taxonomy_rows():
        changes = derive_taxonomy(
            title=row.get("title"),
            organization=row.get("organization"),
            description=row.get("description"),
            location=row.get("location"),
            province=row.get("province"),
            sector=row.get("sector"),
            department=row.get("department"),
            category=row.get("category"),
        )
        if not changes:
            continue
        storage.set_taxonomy(int(row["id"]), changes)
        updated += 1
    return updated
