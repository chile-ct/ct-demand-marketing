#!/usr/bin/env python3
"""
URL Tracking classifier — PTY/JOB landing-page focus-segment classification.

CANONICAL SOURCE: this file is a manual port of the relevant functions/constants from
`chotot-digital`'s `tools/tracking/shared/landing_page_focus_audit.py` (a *different* repo on
the machine that built this dashboard — not tracked here, no automated sync exists between the
two repos). If that repo's classification rules change again (location aliases, segment
patterns, focus sets, price-range patterns, the 3-way cluster rule), THIS FILE NEEDS MANUAL
RE-SYNC. Ported 2026-08-13 under decisions D-014/D-015/D-017 in that repo's context/decisions.md.

What was ported verbatim (copied, not re-derived):
  - classify_location, LOCATION_ALIASES, normalize_path, dashed, has_phrase — location classification.
  - classify_pty_segment, PTY_FOCUS_LABELS, PTY_GENERIC_SEGMENT_LABELS — PTY property-type classification.
  - classify_job_segment, JOB_FOCUS_LABELS, JOB_CODE_LABELS, JOB_GENERIC_SEGMENT_LABELS — JOB job-type classification.
  - has_pty_price_range, PTY_PRICE_PATTERNS, PTY_PRICE_CODE_PATTERN — PTY-only price-range bonus signal.

What was intentionally NOT ported:
  - decision_type()/SCOPE_SIGNAL_TOKENS — the "Scope decision vs Optimization" concept was
    explicitly dropped from this dashboard's URL Tracking tab UI per user feedback (no "Decision
    type" column). Not needed here.
  - The CSV/UI-report/search-terms file parsers (load_rows/sniff_format/audit/campaign_breakdown)
    — this dashboard reads rows from the PTY-URL/JOB-URL Google Sheet tabs directly (see
    `fetch_url_tracking_rows()` in scripts/update_marketplace.py), not from downloaded CSVs.

What is NEW in this file (not in the source repo, built for this dashboard specifically):
  - classify_cluster() — a single entry point wrapping the location/segment classifiers into
    the 3-way Focus/Generic/Non-focus cluster (D-017), plus the PTY price-signal overlay.
  - pty_campaign_group() / job_campaign_group() — campaign-name → group mapping for the
    grouped campaign tables (Let/Sell for PTY; the 8 job types + Generic/multiple for JOB).
    These groupings are a URL-Tracking-tab-specific requirement, not part of the source
    classifier's scope.

Focus location set (D-015): Ho Chi Minh + Binh Duong, for BOTH verticals. Ha Noi is NOT focus.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit


# ─── Ported verbatim from tools/tracking/shared/landing_page_focus_audit.py ─────────────────

# Explicit province/city strings are preferred. City/district aliases are used only where the
# province is routinely omitted from a valid landing-page slug (for example Thu Duc or TPHCM tags).
#
# District-level tokens for the three focus-relevant cities (Ho Chi Minh, Ha Noi, Binh Duong) are
# listed BOTH with their administrative prefix (huyen-/quan-/thanh-pho-/thi-xa-) AND bare (no
# prefix), because live URLs frequently drop the prefix (e.g. "nha-dat-hoc-mon-..." not
# "nha-dat-huyen-hoc-mon-..."). Bare forms are added only for these three cities to limit
# false-positive collision risk with other provinces' place names (see D-014).
LOCATION_ALIASES = {
    "Ho Chi Minh": [
        "tp-ho-chi-minh", "ho-chi-minh", "tp-hcm", "tphcm", "thanh-pho-thu-duc", "thu-duc",
        "quan-1", "quan-2", "quan-3", "quan-4", "quan-5", "quan-6", "quan-7", "quan-8",
        "quan-9", "quan-10", "quan-11", "quan-12", "go-vap", "tan-binh", "tan-phu",
        "binh-thanh", "phu-nhuan", "binh-tan", "huyen-binh-chanh", "binh-chanh",
        "huyen-hoc-mon", "hoc-mon", "huyen-cu-chi", "cu-chi", "huyen-nha-be", "nha-be",
        "huyen-can-gio", "can-gio",
    ],
    "Ha Noi": [
        "ha-noi", "hanoi", "nam-tu-liem", "bac-tu-liem", "huyen-gia-lam", "gia-lam",
        "quan-ha-dong", "ha-dong", "quan-hoang-mai", "hoang-mai", "quan-cau-giay", "cau-giay",
        "quan-thanh-xuan", "thanh-xuan", "quan-long-bien", "long-bien", "quan-dong-da", "dong-da",
        "quan-ba-dinh", "ba-dinh", "quan-hoan-kiem", "hoan-kiem", "quan-tay-ho", "tay-ho",
        "quan-hai-ba-trung", "hai-ba-trung", "huyen-thanh-tri", "thanh-tri", "huyen-hoai-duc",
        "hoai-duc", "huyen-dong-anh", "dong-anh", "huyen-soc-son", "soc-son", "huyen-chuong-my",
        "chuong-my", "huyen-dan-phuong", "dan-phuong", "huyen-quoc-oai", "quoc-oai",
        "huyen-thach-that", "thach-that", "huyen-me-linh", "me-linh", "huyen-thuong-tin",
        "thuong-tin", "huyen-phu-xuyen", "phu-xuyen", "huyen-ung-hoa", "ung-hoa", "huyen-my-duc",
        "my-duc", "huyen-ba-vi", "ba-vi", "thi-xa-son-tay", "son-tay",
    ],
    "Binh Duong": [
        "binh-duong", "thanh-pho-di-an", "di-an", "thanh-pho-thuan-an", "thuan-an",
        "thanh-pho-thu-dau-mot", "thu-dau-mot", "thi-xa-ben-cat", "ben-cat", "thi-xa-tan-uyen",
        "tan-uyen", "huyen-bau-bang", "bau-bang", "huyen-dau-tieng", "dau-tieng",
        "huyen-phu-giao", "phu-giao", "huyen-bac-tan-uyen", "bac-tan-uyen",
    ],
    "Da Nang": [
        "da-nang", "quan-ngu-hanh-son", "quan-lien-chieu", "quan-son-tra", "quan-hai-chau",
        "quan-cam-le", "huyen-hoa-vang",
    ],
    "Dong Nai": ["dong-nai", "thanh-pho-bien-hoa", "bien-hoa", "thanh-pho-long-khanh"],
    "Long An": ["long-an"],
    "Ba Ria - Vung Tau": ["ba-ria-vung-tau", "vung-tau"],
    "Tay Ninh": ["tay-ninh"],
    "Can Tho": ["can-tho"],
    "Lam Dong": ["lam-dong", "da-lat"],
    "Kien Giang": ["kien-giang", "phu-quoc"],
    "Khanh Hoa": ["khanh-hoa", "nha-trang"],
    "Hai Phong": ["hai-phong"],
    "Dak Lak": ["dak-lak", "buon-ma-thuot"],
    "Hue": ["thua-thien-hue", "thanh-pho-hue", "hue"],
    "Quang Nam": ["quang-nam"],
    "Binh Phuoc": ["binh-phuoc"],
    "Binh Dinh": ["binh-dinh"],
    "Vinh Long": ["vinh-long"],
    "Ben Tre": ["ben-tre"],
    "An Giang": ["an-giang"],
    "Quang Ninh": ["quang-ninh"],
    "Gia Lai": ["gia-lai"],
    "Thanh Hoa": ["thanh-hoa"],
    "Bac Ninh": ["bac-ninh"],
    "Hung Yen": ["hung-yen"],
    "Ha Nam": ["ha-nam"],
    "Phu Yen": ["phu-yen"],
    "Nghe An": ["nghe-an"],
    "Tien Giang": ["tien-giang"],
    "Ninh Thuan": ["ninh-thuan"],
    "Binh Thuan": ["binh-thuan", "thanh-pho-phan-thiet"],
    "Soc Trang": ["soc-trang"],
    "Tra Vinh": ["tra-vinh"],
    "Hau Giang": ["hau-giang"],
    "Bac Lieu": ["bac-lieu"],
    "Ca Mau": ["ca-mau"],
    "Dong Thap": ["dong-thap"],
    "Lao Cai": ["lao-cai"],
    "Yen Bai": ["yen-bai"],
    "Phu Tho": ["phu-tho"],
    "Vinh Phuc": ["vinh-phuc"],
    "Thai Nguyen": ["thai-nguyen"],
    "Bac Giang": ["bac-giang"],
    "Lang Son": ["lang-son"],
    "Cao Bang": ["cao-bang"],
    "Bac Kan": ["bac-kan"],
    "Tuyen Quang": ["tuyen-quang"],
    "Ha Giang": ["ha-giang"],
    "Hoa Binh": ["hoa-binh"],
    "Son La": ["son-la"],
    "Dien Bien": ["dien-bien"],
    "Lai Chau": ["lai-chau"],
    "Ninh Binh": ["ninh-binh"],
    "Nam Dinh": ["nam-dinh"],
    "Thai Binh": ["thai-binh"],
    "Ha Tinh": ["ha-tinh"],
    "Quang Binh": ["quang-binh"],
    "Quang Tri": ["quang-tri"],
    "Quang Ngai": ["quang-ngai"],
    "Kon Tum": ["kon-tum"],
    "Dak Nong": ["dak-nong"],
}


JOB_CODE_LABELS = {
    "2": "Sales / Ban hang",
    "3": "Driver / Tai xe",
    "6": "Customer service",
    "7": "Security guard / Bao ve",
    "15": "Sales staff / Nhan vien kinh doanh",
    "17": "Factory worker / Cong nhan",
    "18": "Other jobs",
    "23": "Carpenter",
    "24": "Delivery / Giao hang",
    "25": "Garment worker",
    "26": "Home tailor",
    "27": "Service staff / Nhan vien phuc vu",
    "28": "Telesales",
    "29": "Warehouse staff / Nhan vien kho",
    "30": "Cleaner / Tap vu",
    "31": "Office staff",
    "32": "Cashier",
    "33": "Kitchen assistant",
    "34": "Hairdresser",
    "35": "Domestic helper",
    "36": "Barista",
    "37": "Spa technician",
    "38": "Mechanic",
    "39": "Nail technician",
    "40": "Healthcare",
    "41": "Receptionist",
    "42": "Electrician",
    "43": "Secretary",
    "44": "Construction",
    "45": "PG / Promoter",
    "46": "Cook / Chef",
    "47": "Accountant",
    "48": "Mechanical worker",
    "49": "Welder",
    "50": "Turner / Machinist",
    "51": "Mason",
    "52": "Nanny",
    "53": "Computer technician",
    "54": "Teacher",
    "55": "Tutor",
    "56": "Designer",
}


JOB_FOCUS_LABELS = {
    "Sales / Ban hang",
    "Driver / Tai xe",
    "Security guard / Bao ve",
    "Sales staff / Nhan vien kinh doanh",
    "Factory worker / Cong nhan",
    "Delivery / Giao hang",
    "Service staff / Nhan vien phuc vu",
    "Warehouse staff / Nhan vien kho",
}


# Narrowed (D-014, Kiet): Land and Office are still classified by classify_pty_segment (so they
# keep their own label) but no longer count as focus.
PTY_FOCUS_LABELS = {"Room", "Apartment", "House"}

# D-017: PTY carries an optional price-range BONUS signal (informational only, never gates the
# Focus/Generic/Non-focus cluster). Patterns verified against real evidence URLs: "gia-tu-5-trieu-
# den-10-trieu", "gia-duoi-500-trieu", "gia-tren-30-ty", bare "gia-re", and the internal "sdprN"
# price-bucket code. False-positive traps ruled out: "gia-lam" (Gia Lam district), "gia-lai"
# (Gia Lai province), "khang-gia-go-vap" (a building/project name) — none match because "gia-"
# must be immediately followed by a digit, tu-/duoi-/tren-, or the bare "re" token.
PTY_PRICE_PATTERNS = (
    re.compile(r"(?:^|-)gia-tu-\d+-(?:trieu|ty)-den-\d+-(?:trieu|ty)(?:-|$)"),
    re.compile(r"(?:^|-)gia-duoi-\d+-(?:trieu|ty)(?:-|$)"),
    re.compile(r"(?:^|-)gia-tren-\d+-(?:trieu|ty)(?:-|$)"),
    re.compile(r"(?:^|-)gia-\d+-(?:trieu|ty)(?:-|$)"),
    re.compile(r"(?:^|-)duoi-\d+-(?:trieu|ty)(?:-|$)"),  # e.g. "duoi-500-trieu" without a "gia-" prefix
    re.compile(r"(?:^|-)gia-re(?:-|$)"),
)
PTY_PRICE_CODE_PATTERN = re.compile(r"(?:^|-)sdpr\d+(?:-|$)")

# Segment labels that mean "no identifiable focus-relevant type at all" — used to decide the
# "Generic" cluster (zero signal on any applicable dimension), as distinct from "Non-focus"
# (has signal on at least one dimension, but fails at least one required dimension).
PTY_GENERIC_SEGMENT_LABELS = {
    "Generic property page", "Generic / homepage",
    "Project page without focus type", "Other / no focus property token",
}
JOB_GENERIC_SEGMENT_LABELS = {
    "Other tag / keyword page", "Generic location / homepage", "Other / no canonical job type",
}


def has_pty_price_range(path: str) -> bool:
    normalized = dashed(path)
    if PTY_PRICE_CODE_PATTERN.search(normalized):
        return True
    return any(pattern.search(normalized) for pattern in PTY_PRICE_PATTERNS)


def normalize_path(url: str) -> str:
    cleaned = (url or "").replace("{ignore}", "")
    path = unquote(urlsplit(cleaned).path)
    path = unicodedata.normalize("NFKD", path).encode("ascii", "ignore").decode("ascii")
    path = re.sub(r"/+", "/", path.lower()).strip("/")
    return path


def dashed(path: str) -> str:
    return re.sub(r"-+", "-", path.replace("/", "-")).strip("-")


def has_phrase(path: str, phrase: str) -> bool:
    return re.search(rf"(?:^|-){re.escape(phrase)}(?:-|$)", dashed(path)) is not None


def classify_location(path: str) -> tuple[str, list[str]]:
    matches: list[tuple[int, int, str, str]] = []
    normalized = dashed(path)
    for label, aliases in LOCATION_ALIASES.items():
        for alias in aliases:
            found = list(re.finditer(rf"(?:^|-){re.escape(alias)}(?:-|$)", normalized))
            for match in found:
                matches.append((match.start(), len(alias), label, alias))
    if not matches:
        return "Unclassified / no city in URL", []
    # Prefer the last and then longest explicit marker; URL taxonomies normally put location after type.
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    labels = list(dict.fromkeys(item[2] for item in matches))
    return matches[0][2], labels


def classify_pty_segment(path: str) -> tuple[str, list[str]]:
    matched: list[str] = []
    if any(has_phrase(path, value) for value in ("phong-tro", "thue-tro", "nha-tro")):
        matched.append("Room")
    if any(has_phrase(path, value) for value in ("chung-cu", "can-ho")):
        matched.append("Apartment")
    if has_phrase(path, "nha-dat") or has_phrase(path, "nha-nguyen-can"):
        matched.append("House")
    if has_phrase(path, "dat-nen") or any(
        has_phrase(path, value) for value in ("mua-ban-dat", "thue-dat")
    ):
        matched.append("Land")
    if has_phrase(path, "van-phong"):
        matched.append("Office")

    priority = ["Room", "Apartment", "House", "Land", "Office"]
    if matched:
        return next(label for label in priority if label in matched), matched
    if has_phrase(path, "bat-dong-san"):
        return "Generic property page", matched
    if not path or path in {"mua-ban", "thue", "mua-ban-nha-dat", "mua-ban-dat"}:
        # `mua-ban-nha-dat` and `mua-ban-dat` are caught above; the set documents generic fallbacks.
        return "Generic / homepage", matched
    if has_phrase(path, "du-an"):
        return "Project page without focus type", matched
    return "Other / no focus property token", matched


def classify_job_segment(path: str) -> tuple[str, list[str], str | None]:
    matched: list[str] = []
    phrase_map = [
        ("Sales staff / Nhan vien kinh doanh", ("nhan-vien-kinh-doanh",)),
        ("Service staff / Nhan vien phuc vu", ("nhan-vien-phuc-vu",)),
        ("Warehouse staff / Nhan vien kho", ("nhan-vien-kho-van", "nhan-vien-kho", "kho-van")),
        ("Delivery / Giao hang", ("tai-xe-giao-hang", "giao-hang")),
        ("Driver / Tai xe", ("tai-xe-lai-xe", "tai-xe")),
        ("Factory worker / Cong nhan", ("cong-nhan",)),
        ("Security guard / Bao ve", ("bao-ve",)),
        ("Sales / Ban hang", ("ban-hang",)),
    ]
    for label, phrases in phrase_map:
        if any(has_phrase(path, phrase) for phrase in phrases):
            matched.append(label)

    code_match = re.search(r"(?:^|-)sdjt(\d+)(?:-|$)", dashed(path))
    code = code_match.group(1) if code_match else None
    if matched:
        return matched[0], matched, code
    if code:
        return JOB_CODE_LABELS.get(code, f"Job type sdjt{code}"), matched, code
    if path.startswith("tags/"):
        return "Other tag / keyword page", matched, code
    if not path or path == "viec-lam" or path.startswith("viec-lam-"):
        return "Generic location / homepage", matched, code
    return "Other / no canonical job type", matched, code


# ─── New for the URL Tracking tab (not part of the source classifier) ──────────────────────────

# D-015: focus location is HCM + Binh Duong for BOTH verticals.
FOCUS_LOCATIONS = {"Ho Chi Minh", "Binh Duong"}


def classify_cluster(vertical: str, landing_page_url: str) -> dict:
    """Classify one landing-page URL into the 3-way Focus/Generic/Non-focus cluster (D-017).

    Focus    = location match AND segment match (2-dimension rule, same for both verticals —
               price never gates this, it is a PTY-only bonus/informational signal).
    Generic  = no signal at all on location AND no signal at all on segment.
    Non-focus= has signal on at least one dimension but fails at least one.
    """
    path = normalize_path(landing_page_url)
    location, _ = classify_location(path)
    location_focus = location in FOCUS_LOCATIONS
    location_present = location != "Unclassified / no city in URL"

    if vertical == "PTY":
        segment, _ = classify_pty_segment(path)
        focus_segments = PTY_FOCUS_LABELS
        generic_segment_labels = PTY_GENERIC_SEGMENT_LABELS
        price_signal = has_pty_price_range(path)
    else:
        segment, _, _ = classify_job_segment(path)
        focus_segments = JOB_FOCUS_LABELS
        generic_segment_labels = JOB_GENERIC_SEGMENT_LABELS
        price_signal = None

    segment_focus = segment in focus_segments
    segment_present = segment not in generic_segment_labels

    eligible = location_focus and segment_focus
    no_signal = not location_present and not segment_present
    cluster = "Focus" if eligible else ("Generic" if no_signal else "Non-focus")

    return {
        "cluster": cluster,
        "location": location,
        "segment": segment,
        "price_signal": price_signal,
        "combined_label": f"{segment} x {location}",
    }


# Campaign-name → group mapping for the grouped campaign tables. Distinct from the source
# repo's decision_type()/SCOPE_SIGNAL_TOKENS concept — this is purely for the URL Tracking tab's
# "group campaigns" requirement.
def pty_campaign_group(campaign_name: str | None) -> str:
    name = (campaign_name or "").lower()
    if "let_" in name:
        return "Let"
    if "sell_" in name:
        return "Sell"
    return "Other / Unclear"


_JOB_GROUP_TOKENS = [
    ("Sales / Ban hang", ("banhang",)),
    ("Security guard / Bao ve", ("baove",)),
    ("Factory worker / Cong nhan", ("congnhan",)),
    ("Service staff / Nhan vien phuc vu", ("nvpv",)),
    ("Sales staff / Nhan vien kinh doanh", ("nvkd",)),
    ("Warehouse staff / Nhan vien kho", ("khovan", "nvkho")),
    ("Driver / Tai xe", ("taixe",)),
    ("Delivery / Giao hang", ("giaohang",)),
]


def job_campaign_group(campaign_name: str | None) -> str:
    name = (campaign_name or "").lower()
    for label, tokens in _JOB_GROUP_TOKENS:
        if any(token in name for token in tokens):
            return label
    return "Generic / multiple types"
