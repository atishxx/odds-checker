import os
import re
import json
import time
import sqlite3
import logging
import hashlib
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SCAN_DATE = os.getenv("SCAN_DATE", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "120"))
DB_FILE = os.getenv("DB_FILE", "odds_tracker.db")
DEBUG_DIR = os.getenv("DEBUG_DIR", "debug")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))

_default_bases = [
    "https://vnm.aiscore.com",
    "https://m.aiscore.com",
    "https://www.aiscore.com",
]
OFFICIAL_BASES = [x.strip().rstrip("/") for x in os.getenv("AISCORE_BASES", "").split(",") if x.strip()] or _default_bases

MATCH_RE = re.compile(r"/match-([^/?#]+)/([a-z0-9]+)", re.I)
LIVE_RE = re.compile(r"/live/football-[a-z0-9][a-z0-9-]*", re.I)
PREDICTION_RE = re.compile(r"/prediction/football-[a-z0-9][a-z0-9-]*-prediction", re.I)
DECIMAL_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3})?)(?![\d.])")

logger = logging.getLogger("OddsEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class FetchResult:
    requested_url: str
    final_url: str = ""
    status: Optional[int] = None
    html: str = ""
    title: str = ""
    blocked: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.status == 200 and self.html and not self.blocked)


@dataclass
class Fixture:
    match_id: str
    slug: str
    home: str
    away: str
    source_url: str = ""

    @property
    def match_path(self) -> str:
        return f"/match-{self.slug}/{self.match_id}" if self.slug and self.match_id else ""


@dataclass
class OddsRow:
    bookmaker: str
    opening: Tuple[float, float, float]
    prematch: Tuple[float, float, float]
    inplay: Optional[Tuple[float, float, float]] = None
    basis: str = "AiScore opening → pre-match"


@dataclass
class Candidate:
    side: str
    status: str
    bookmaker: str
    opening: Tuple[float, float, float]
    current: Tuple[float, float, float]
    drop: float
    drop_pct: float
    fair_prob_shift: float
    reason: str
    basis: str
    agreeing_rows: int = 1
    total_rows: int = 1


def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alerted_matches (
            match_id TEXT NOT NULL,
            side TEXT NOT NULL,
            alert_level INTEGER NOT NULL,
            alert_type TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (match_id, side)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            match_id TEXT NOT NULL,
            market_row INTEGER NOT NULL,
            home REAL NOT NULL,
            draw REAL NOT NULL,
            away REAL NOT NULL,
            captured_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def get_alert_level(match_id: str, side: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT alert_level FROM alerted_matches WHERE match_id=? AND side=?", (match_id, side))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def record_alert(match_id: str, side: str, alert_level: int, alert_type: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alerted_matches(match_id, side, alert_level, alert_type)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(match_id, side) DO UPDATE SET
            alert_level=excluded.alert_level,
            alert_type=excluded.alert_type,
            timestamp=CURRENT_TIMESTAMP
        """,
        (match_id, side, alert_level, alert_type),
    )
    conn.commit()
    conn.close()


def get_first_snapshot(match_id: str, market_row: int) -> Optional[Tuple[float, float, float]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT home, draw, away FROM odds_snapshots
        WHERE match_id=? AND market_row=?
        ORDER BY captured_at ASC, rowid ASC LIMIT 1
        """,
        (match_id, market_row),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return tuple(float(x) for x in row)


def record_snapshots(match_id: str, rows: List[OddsRow]) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    for idx, row in enumerate(rows, start=1):
        current = tuple(float(x) for x in row.prematch)
        cur.execute(
            """
            SELECT home, draw, away FROM odds_snapshots
            WHERE match_id=? AND market_row=?
            ORDER BY captured_at DESC, rowid DESC LIMIT 1
            """,
            (match_id, idx),
        )
        last = cur.fetchone()
        if last and all(abs(float(last[i]) - current[i]) < 1e-9 for i in range(3)):
            continue
        cur.execute(
            "INSERT INTO odds_snapshots(match_id, market_row, home, draw, away) VALUES (?, ?, ?, ?, ?)",
            (match_id, idx, current[0], current[1], current[2]),
        )
    conn.commit()
    conn.close()


def send_telegram_alert(message: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        logger.warning("Telegram credentials missing; message not sent.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.ok:
                return True
            logger.warning("Telegram HTTP %s: %s", res.status_code, res.text[:250])
        except Exception as exc:
            logger.warning("Telegram attempt %d failed: %s", attempt + 1, exc)
        time.sleep(1 + attempt)
    return False


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.google.com/",
        }
    )
    if PROXY_URL:
        proxy = PROXY_URL if "://" in PROXY_URL else f"http://{PROXY_URL}"
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def is_security_challenge(html: str, title: str = "") -> bool:
    hay = f"{title}\n{html[:160000]}".lower()
    signatures = (
        "just a moment",
        "performing security verification",
        "verify you are human",
        "cf-chl-",
        "challenge-platform",
        "cloudflare ray id",
        "attention required",
    )
    return any(sig in hay for sig in signatures)


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)[:120]


def save_text_debug(name: str, html: str = "", data: Optional[Dict[str, Any]] = None) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe = _safe_name(name)
    if html:
        with open(os.path.join(DEBUG_DIR, f"{safe}.html"), "w", encoding="utf-8") as fh:
            fh.write(html)
    if data is not None:
        with open(os.path.join(DEBUG_DIR, f"{safe}.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)


def fetch_html(session: requests.Session, url: str, label: str, save_failure: bool = True) -> FetchResult:
    last = FetchResult(requested_url=url)
    attempts = []
    for attempt in range(3):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            text = r.text or ""
            title = ""
            if text:
                m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
                if m:
                    title = re.sub(r"\s+", " ", m.group(1)).strip()
            blocked = is_security_challenge(text, title)
            last = FetchResult(
                requested_url=url,
                final_url=str(r.url),
                status=r.status_code,
                html=text,
                title=title,
                blocked=blocked,
            )
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "status": r.status_code,
                    "final_url": str(r.url),
                    "title": title,
                    "blocked": blocked,
                    "content_length": len(text),
                    "server": r.headers.get("server", ""),
                    "content_type": r.headers.get("content-type", ""),
                }
            )
            logger.info("%s HTTP %s -> %s", label, r.status_code, r.url)
            if blocked:
                logger.warning("%s SECURITY_CHALLENGE at %s", label, r.url)
                break
            if r.status_code == 200 and text:
                return last
            logger.warning("%s HTTP %s", label, r.status_code)
        except Exception as exc:
            last = FetchResult(requested_url=url, error=f"{type(exc).__name__}: {exc}")
            attempts.append({"attempt": attempt + 1, "error": last.error})
            logger.warning("%s attempt %d failed: %s", label, attempt + 1, exc)
        time.sleep(1.5 * (attempt + 1))

    if save_failure:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        save_text_debug(
            f"fetch_failure_{digest}",
            last.html,
            {
                "label": label,
                "url": url,
                "attempts": attempts,
                "last": {
                    "final_url": last.final_url,
                    "status": last.status,
                    "title": last.title,
                    "blocked": last.blocked,
                    "error": last.error,
                },
            },
        )
    return last


def humanize_slug(slug: str) -> Tuple[str, str]:
    parts = re.split(r"-vs-", slug, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        parts = re.split(r"-v-", slug, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].replace("-", " ").title(), parts[1].replace("-", " ").title()
    return slug.replace("-", " ").title(), "Opponent"


def normalise_fixture_href(href: str, source_url: str = "") -> Optional[Fixture]:
    if not href:
        return None
    cleaned = href.replace("\\/", "/")
    m = MATCH_RE.search(cleaned)
    if not m:
        return None
    slug, match_id = m.group(1), m.group(2)
    home, away = humanize_slug(slug)
    return Fixture(match_id=match_id, slug=slug, home=home, away=away, source_url=source_url)


def _route_urls(html: str, final_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if LIVE_RE.search(href) or PREDICTION_RE.search(href):
            found.append(urljoin(final_url, href))
    cleaned = html.replace("\\/", "/")
    for rx in (LIVE_RE, PREDICTION_RE):
        for m in rx.finditer(cleaned):
            found.append(urljoin(final_url, m.group(0)))
    return list(dict.fromkeys(found))


def _extract_canonical_fixtures(html: str, source_url: str) -> List[Fixture]:
    fixtures: Dict[str, Fixture] = {}
    soup = BeautifulSoup(html, "html.parser")
    candidates = [a.get("href", "") for a in soup.find_all("a", href=True)]
    cleaned = html.replace("\\/", "/")
    candidates.extend(m.group(0) for m in MATCH_RE.finditer(cleaned))
    canonical = soup.find("link", rel=lambda value: value and "canonical" in str(value).lower())
    if canonical and canonical.get("href"):
        candidates.append(canonical.get("href"))
    for href in candidates:
        fx = normalise_fixture_href(href, source_url=source_url)
        if fx:
            fixtures.setdefault(fx.match_id, fx)
    return list(fixtures.values())


def _fallback_fixture_from_route(route_url: str) -> Optional[Fixture]:
    path = urlparse(route_url).path.strip("/")
    slug = ""
    if path.startswith("live/football-"):
        slug = path[len("live/football-"):]
    elif path.startswith("prediction/football-") and path.endswith("-prediction"):
        slug = path[len("prediction/football-"):-len("-prediction")]
    if "-vs-" not in slug:
        return None
    home, away = humanize_slug(slug)
    synthetic = "route_" + hashlib.sha1(route_url.encode("utf-8")).hexdigest()[:16]
    return Fixture(match_id=synthetic, slug=slug, home=home, away=away, source_url=route_url)


def discovery_targets(base: str) -> List[str]:
    host = urlparse(base).netloc.lower()
    if "vnm.aiscore.com" in host:
        paths = ["/prediction", "/live", "/"]
    elif "m.aiscore.com" in host:
        paths = ["/today-matches/football", "/today-matches", "/live", "/"]
    else:
        paths = ["/live", "/football", "/"]
    if SCAN_DATE:
        raw = SCAN_DATE
        if re.fullmatch(r"\d{8}", raw):
            raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        dated = []
        for p in paths:
            joiner = "&" if "?" in p else "?"
            dated.append(f"{p}{joiner}date={raw}")
        paths = dated + paths
    return [urljoin(base + "/", p.lstrip("/")) for p in paths]


def discover_fixtures(session: requests.Session) -> List[Fixture]:
    fixtures: Dict[str, Fixture] = {}
    route_queue: List[str] = []
    attempts: List[Dict[str, Any]] = []

    for base in OFFICIAL_BASES:
        for target in discovery_targets(base):
            if len(fixtures) >= MAX_MATCHES:
                break
            result = fetch_html(session, target, "DISCOVERY", save_failure=True)
            attempts.append(
                {
                    "url": target,
                    "status": result.status,
                    "ok": result.ok,
                    "blocked": result.blocked,
                    "title": result.title,
                    "final_url": result.final_url,
                    "error": result.error,
                }
            )
            if not result.ok:
                continue
            direct = _extract_canonical_fixtures(result.html, result.final_url or target)
            for fx in direct:
                fixtures.setdefault(fx.match_id, fx)
                if len(fixtures) >= MAX_MATCHES:
                    break
            route_queue.extend(_route_urls(result.html, result.final_url or target))
            route_queue = list(dict.fromkeys(route_queue))
            logger.info("Discovery direct=%d route_candidates=%d", len(fixtures), len(route_queue))
            if len(fixtures) >= min(MAX_MATCHES, 20):
                break
        if len(fixtures) >= min(MAX_MATCHES, 20):
            break

    resolution_budget = min(max(MAX_MATCHES * 2, 30), 180)
    resolved = 0
    for route_url in route_queue:
        if len(fixtures) >= MAX_MATCHES or resolved >= resolution_budget:
            break
        resolved += 1
        result = fetch_html(session, route_url, "RESOLVE", save_failure=False)
        if not result.ok:
            continue
        canonical = _extract_canonical_fixtures(result.html, route_url)
        if canonical:
            for fx in canonical:
                fx.source_url = route_url
                fixtures.setdefault(fx.match_id, fx)
        else:
            fallback = _fallback_fixture_from_route(route_url)
            if fallback:
                fixtures.setdefault(fallback.match_id, fallback)
        logger.info("Route resolution %d/%d -> fixtures=%d", resolved, len(route_queue), len(fixtures))

    save_text_debug(
        "fixture_discovery_summary",
        data={
            "official_bases": OFFICIAL_BASES,
            "attempts": attempts,
            "route_candidates": len(route_queue),
            "routes_resolved": resolved,
            "fixtures": len(fixtures),
            "sample": [
                {
                    "match_id": f.match_id,
                    "slug": f.slug,
                    "home": f.home,
                    "away": f.away,
                    "source_url": f.source_url,
                }
                for f in list(fixtures.values())[:20]
            ],
        },
    )
    return list(fixtures.values())[:MAX_MATCHES]


def _is_decimal_odd(value: float) -> bool:
    return 1.01 <= value <= 100.0


def _line_triple(line: str) -> Optional[Tuple[float, float, float]]:
    vals = []
    for token in DECIMAL_RE.findall(line):
        try:
            v = float(token)
        except ValueError:
            continue
        if _is_decimal_odd(v):
            vals.append(v)
    if len(vals) == 3:
        return (vals[0], vals[1], vals[2])
    return None


def _extract_1x2_rows(html: str) -> List[OddsRow]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    low_lines = [x.lower() for x in lines]

    start = 0
    opening_markers = [i for i, x in enumerate(low_lines) if "opening odds" in x]
    if opening_markers:
        start = opening_markers[0]

    for i in range(start, min(start + 50, len(lines))):
        compact = re.sub(r"\s+", "", lines[i]).upper()
        if compact in {"1X2", "1×2"}:
            start = i + 1
            break

    stop = min(len(lines), start + 180)
    stop_terms = ("asian handicap", "total goals", "total corners", "double chance", "correct score", "gamble responsibly")
    for i in range(start + 1, min(len(lines), start + 180)):
        if any(low_lines[i].startswith(term) for term in stop_terms):
            stop = i
            break

    triples: List[Tuple[float, float, float]] = []
    for line in lines[start:stop]:
        t = _line_triple(line)
        if t and t not in triples[-2:]:
            triples.append(t)

    # Table/div fallback: many regional pages keep the 1X2 values in one row/container.
    if len(triples) < 2:
        for tag in soup.find_all(["tr", "li", "div"]):
            raw = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
            if len(raw) > 240:
                continue
            nums = []
            for token in DECIMAL_RE.findall(raw):
                try:
                    value = float(token)
                except ValueError:
                    continue
                if _is_decimal_odd(value):
                    nums.append(value)
            if len(nums) in (3, 6, 9):
                for pos in range(0, len(nums), 3):
                    tri = tuple(nums[pos:pos + 3])
                    if len(tri) == 3 and tri not in triples:
                        triples.append(tri)
            if len(triples) >= 12:
                break

    if not triples:
        return []

    # Single visible triple: keep it as an observed AiScore 1X2 snapshot.
    if len(triples) == 1:
        return [
            OddsRow(
                bookmaker="AiScore observed 1X2",
                opening=triples[0],
                prematch=triples[0],
                basis="Observed AiScore snapshot history",
            )
        ]

    section_text = " ".join(low_lines[max(0, start - 10):stop])
    has_inplay = "in-play odds" in section_text or "in play odds" in section_text
    group_size = 3 if has_inplay and len(triples) >= 3 else 2

    rows: List[OddsRow] = []
    pos = 0
    row_no = 1
    while pos + 1 < len(triples) and row_no <= 12:
        opening = triples[pos]
        prematch = triples[pos + 1]
        inplay = triples[pos + 2] if group_size == 3 and pos + 2 < len(triples) else None
        rows.append(
            OddsRow(
                bookmaker=f"AiScore market row {row_no}",
                opening=opening,
                prematch=prematch,
                inplay=inplay,
                basis="AiScore opening → pre-match",
            )
        )
        pos += group_size
        row_no += 1
    return rows


def update_fixture_names(html: str, fixture: Fixture) -> None:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(r"^\d{4}/\d{2}/\d{2}\s+", "", title).strip()
    patterns = [
        r"(.+?)\s+vs\s+(.+?)\s+betting odds\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+live score.*?\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+Prediction\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+-\s+AiScore",
    ]
    for pattern in patterns:
        m = re.search(pattern, title, flags=re.I)
        if not m:
            continue
        home = re.sub(r"\s+", " ", m.group(1)).strip(" -")
        away = re.sub(r"\s+", " ", m.group(2)).strip(" -")
        if home and away:
            fixture.home = home
            fixture.away = away
            return


def _candidate_page_urls(fixture: Fixture) -> List[str]:
    urls = []
    if fixture.match_id and not fixture.match_id.startswith("route_"):
        for base in OFFICIAL_BASES:
            urls.append(f"{base}{fixture.match_path}/odds")
            urls.append(f"{base}{fixture.match_path}")
    if fixture.source_url:
        urls.append(fixture.source_url)
    return list(dict.fromkeys(urls))


def _apply_observed_baseline(fixture: Fixture, rows: List[OddsRow]) -> List[OddsRow]:
    adjusted = []
    for idx, row in enumerate(rows, start=1):
        if row.basis != "Observed AiScore snapshot history":
            adjusted.append(row)
            continue
        baseline = get_first_snapshot(fixture.match_id, idx)
        if baseline:
            adjusted.append(
                OddsRow(
                    bookmaker=row.bookmaker,
                    opening=baseline,
                    prematch=row.prematch,
                    basis="Observed AiScore first snapshot → current",
                )
            )
        else:
            adjusted.append(row)
    return adjusted


def scrape_fixture_odds(session: requests.Session, fixture: Fixture) -> List[OddsRow]:
    attempts = []
    for url in _candidate_page_urls(fixture):
        logger.info("Odds source: %s vs %s -> %s", fixture.home, fixture.away, url)
        result = fetch_html(session, url, f"ODDS {fixture.match_id}", save_failure=False)
        attempts.append(
            {
                "url": url,
                "status": result.status,
                "ok": result.ok,
                "blocked": result.blocked,
                "title": result.title,
                "final_url": result.final_url,
                "error": result.error,
            }
        )
        if not result.ok:
            continue
        update_fixture_names(result.html, fixture)
        rows = _extract_1x2_rows(result.html)
        if not rows:
            continue
        rows = _apply_observed_baseline(fixture, rows)
        record_snapshots(fixture.match_id, rows)
        logger.info("Parsed %d 1X2 row(s) for %s vs %s", len(rows), fixture.home, fixture.away)
        return rows

    logger.warning("NO_1X2_ODDS for %s vs %s", fixture.home, fixture.away)
    save_text_debug(f"{fixture.match_id}_odds_attempts", data={"fixture": fixture.__dict__, "attempts": attempts})
    return []


def _fair_probability(odds: Tuple[float, float, float], index: int) -> float:
    inv = [1.0 / x for x in odds]
    vig = sum(inv)
    return inv[index] / vig if vig else 0.0


def evaluate_odds_row(row: OddsRow) -> List[Candidate]:
    op = row.opening
    cur = row.prematch
    out: List[Candidate] = []

    for side, idx, opp_idx in (("HOME", 0, 2), ("AWAY", 2, 0)):
        selected_open = op[idx]
        selected_cur = cur[idx]
        if not (1.50 <= selected_open <= 2.50):
            continue

        selected_drop = selected_open - selected_cur
        draw_drop = op[1] - cur[1]
        opp_drop = op[opp_idx] - cur[opp_idx]
        if selected_drop <= 0.005:
            continue

        drop_pct = selected_drop / selected_open * 100.0
        fair_shift = (_fair_probability(cur, idx) - _fair_probability(op, idx)) * 100.0

        draw_ok = draw_drop <= 0.005
        opponent_ok = opp_drop <= 0.005
        clean_shape = draw_ok and opponent_ok

        if clean_shape and (drop_pct >= 5.0 or fair_shift >= 2.5):
            status = "SHARP_PROXY"
            reason = "selected side shortened; draw and opponent held or rose"
        elif clean_shape and (drop_pct >= 2.0 or fair_shift >= 1.0):
            status = "NEAR_MISS"
            reason = "clean one-sided shortening, below sharp threshold"
        elif drop_pct >= 3.0:
            conflicts = []
            if not draw_ok:
                conflicts.append("draw also shortened")
            if not opponent_ok:
                conflicts.append("opponent also shortened")
            status = "NEAR_MISS"
            reason = "; ".join(conflicts) or "movement conflict"
        else:
            continue

        out.append(
            Candidate(
                side=side,
                status=status,
                bookmaker=row.bookmaker,
                opening=op,
                current=cur,
                drop=selected_drop,
                drop_pct=drop_pct,
                fair_prob_shift=fair_shift,
                reason=reason,
                basis=row.basis,
            )
        )
    return out


def analyse_fixture(rows: List[OddsRow]) -> Optional[Candidate]:
    by_side: Dict[str, List[Candidate]] = {"HOME": [], "AWAY": []}
    for row in rows:
        for candidate in evaluate_odds_row(row):
            by_side[candidate.side].append(candidate)

    home = by_side["HOME"]
    away = by_side["AWAY"]
    if home and away:
        logger.info("REJECTED: mixed Home/Away shortening across AiScore rows")
        return None

    side_candidates = home or away
    if not side_candidates:
        return None

    sharp = [c for c in side_candidates if c.status == "SHARP_PROXY"]
    near = [c for c in side_candidates if c.status == "NEAR_MISS"]
    pool = sharp or near
    best = max(pool, key=lambda c: (c.fair_prob_shift, c.drop_pct))
    best.agreeing_rows = len(pool)
    best.total_rows = len(rows)
    if sharp and len(sharp) >= 2:
        best.reason += f"; {len(sharp)}/{len(rows)} AiScore rows agree"
    elif near and len(near) >= 2:
        best.reason += f"; {len(near)}/{len(rows)} AiScore rows show the same side"
    return best


def _alert_level(status: str) -> int:
    return 2 if status == "SHARP_PROXY" else 1


def format_candidate_message(fixture: Fixture, candidate: Candidate) -> str:
    op = candidate.opening
    cur = candidate.current
    side_name = fixture.home if candidate.side == "HOME" else fixture.away
    header = "⚡ <b>SHARP MOVEMENT PROXY</b>" if candidate.status == "SHARP_PROXY" else "👀 <b>NEAR MISS</b>"
    return (
        f"{header}\n"
        f"⚽ <b>{fixture.home} vs {fixture.away}</b>\n"
        f"🎯 <b>Selected:</b> {side_name} ({candidate.side})\n"
        f"🏦 <b>Source:</b> {candidate.bookmaker}\n"
        f"📊 <b>Agreement:</b> {candidate.agreeing_rows}/{candidate.total_rows} AiScore rows\n"
        f"🧭 <b>Basis:</b> {candidate.basis}\n\n"
        f"📉 <b>1X2</b>\n"
        f"Home: {op[0]:.2f} → {cur[0]:.2f}\n"
        f"Draw: {op[1]:.2f} → {cur[1]:.2f}\n"
        f"Away: {op[2]:.2f} → {cur[2]:.2f}\n\n"
        f"Drop: {candidate.drop:.2f} ({candidate.drop_pct:.1f}%)\n"
        f"No-vig probability shift: +{candidate.fair_prob_shift:.2f} pp\n"
        f"Reason: {candidate.reason}\n\n"
        f"ℹ️ Proxy/observed movement only unless a full AiScore opening→pre-match row was parsed."
    )


def run_engine() -> None:
    init_db()
    stats = {"fixtures": 0, "with_odds": 0, "no_odds": 0, "sharp": 0, "near": 0, "sent": 0}
    session = make_session()

    logger.info("AiScore official hosts: %s", ", ".join(OFFICIAL_BASES))
    fixtures = discover_fixtures(session)
    stats["fixtures"] = len(fixtures)

    if not fixtures:
        logger.error("No AiScore match links discovered on any official host.")
        send_telegram_alert(
            "🚨 <b>ODDS SCANNER ERROR</b>\n"
            "No AiScore fixtures were reachable from this runner. "
            "Check fixture_discovery_summary.json and fetch_failure_*.json in the workflow artifact. "
            "If all official hosts are blocked on a GitHub-hosted runner, use the included self-hosted workflow."
        )
        return

    logger.info("Discovered %d AiScore fixtures", len(fixtures))
    for index, fixture in enumerate(fixtures, start=1):
        logger.info("[%d/%d] Processing %s vs %s", index, len(fixtures), fixture.home, fixture.away)
        rows = scrape_fixture_odds(session, fixture)
        if not rows:
            stats["no_odds"] += 1
            continue
        stats["with_odds"] += 1

        candidate = analyse_fixture(rows)
        if not candidate:
            logger.info("REJECTED: %s vs %s - no valid one-sided movement", fixture.home, fixture.away)
            continue

        level = _alert_level(candidate.status)
        previous = get_alert_level(fixture.match_id, candidate.side)
        if previous >= level:
            logger.info("DEDUP: %s %s already alerted at level %d", fixture.match_id, candidate.side, previous)
            continue

        if candidate.status == "SHARP_PROXY":
            stats["sharp"] += 1
        else:
            stats["near"] += 1

        message = format_candidate_message(fixture, candidate)
        if send_telegram_alert(message):
            stats["sent"] += 1
            record_alert(fixture.match_id, candidate.side, level, candidate.status)
        else:
            logger.info("CANDIDATE %s", re.sub(r"<[^>]+>", "", message).replace("\n", " | "))
        time.sleep(0.15)

    target = SCAN_DATE or "today"
    summary = (
        f"✅ <b>SCAN COMPLETE ({target})</b>\n"
        f"Matches discovered: {stats['fixtures']}\n"
        f"Odds pages parsed: {stats['with_odds']}\n"
        f"No 1X2 odds: {stats['no_odds']}\n"
        f"Sharp proxies: {stats['sharp']}\n"
        f"Near misses: {stats['near']}\n"
        f"Alerts sent: {stats['sent']}"
    )
    logger.info(re.sub(r"<[^>]+>", "", summary).replace("\n", " | "))
    send_telegram_alert(summary)


if __name__ == "__main__":
    run_engine()
