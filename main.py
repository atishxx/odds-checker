import os
import re
import json
import time
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urljoin

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SCAN_DATE = os.getenv("SCAN_DATE", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "120"))
HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
DB_FILE = os.getenv("DB_FILE", "odds_tracker.db")
DEBUG_DIR = os.getenv("DEBUG_DIR", "debug")
DEBUG_CAPTURE_LIMIT = int(os.getenv("DEBUG_CAPTURE_LIMIT", "5"))
_debug_capture_count = 0

logger = logging.getLogger("OddsEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MATCH_RE = re.compile(r"/match-([^/?#]+)/([a-z0-9]+)", re.I)
ODDS_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3})?)(?![\d.])")


@dataclass
class Fixture:
    match_id: str
    slug: str
    url: str
    home: str
    away: str

    @property
    def odds_url(self) -> str:
        return f"https://www.aiscore.com/match-{self.slug}/{self.match_id}/odds"


@dataclass
class OddsRow:
    bookmaker: str
    opening: Tuple[float, float, float]
    prematch: Tuple[float, float, float]
    inplay: Optional[Tuple[float, float, float]] = None


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


def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alerted_matches'")
    exists = cur.fetchone() is not None
    if exists:
        cur.execute("PRAGMA table_info(alerted_matches)")
        columns = {row[1] for row in cur.fetchall()}
        required = {"match_id", "side", "alert_level", "alert_type", "timestamp"}
        if not required.issubset(columns):
            legacy = f"alerted_matches_legacy_{int(time.time())}"
            cur.execute(f"ALTER TABLE alerted_matches RENAME TO {legacy}")
            logger.info("Migrated legacy alert table to %s", legacy)

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


def send_telegram_alert(message: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        logger.warning("Telegram credentials missing; message not sent.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.ok:
                return True
            logger.warning("Telegram HTTP %s: %s", res.status_code, res.text[:250])
        except Exception as exc:
            logger.warning("Telegram attempt %s failed: %s", attempt + 1, exc)
        time.sleep(1 + attempt)
    return False


def _humanize_slug(slug: str) -> Tuple[str, str]:
    parts = re.split(r"-vs-", slug, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        parts = re.split(r"-v-", slug, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].replace("-", " ").title(), parts[1].replace("-", " ").title()
    return slug.replace("-", " ").title(), "Opponent"


def _normalise_fixture_href(href: str) -> Optional[Fixture]:
    if not href:
        return None
    m = MATCH_RE.search(href)
    if not m:
        return None
    slug, match_id = m.group(1), m.group(2)
    home, away = _humanize_slug(slug)
    base_url = f"https://www.aiscore.com/match-{slug}/{match_id}"
    return Fixture(match_id=match_id, slug=slug, url=base_url, home=home, away=away)


def _target_home_urls() -> List[str]:
    """Return discovery pages in priority order.

    AiScore's generic homepage no longer reliably renders canonical match links.
    The dedicated football day page does, so use it first for live/current scans.
    Keep the old dated homepage as a compatibility fallback for manual backfills.
    """
    if not SCAN_DATE:
        return [
            "https://www.aiscore.com/today-matches/football",
            "https://www.aiscore.com/live",
            "https://www.aiscore.com/",
        ]

    raw = SCAN_DATE.strip()
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"

    # AiScore has changed historical-date routing more than once. Try both the
    # dedicated day page with the date query and the legacy homepage query.
    return [
        f"https://www.aiscore.com/today-matches/football?date={raw}",
        f"https://www.aiscore.com/?date={raw}",
    ]


def _extract_match_urls_from_object(obj: Any) -> List[str]:
    """Recursively recover canonical match URLs embedded in JSON/Next data."""
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(k)
                walk(v)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            text = value.replace("\\/", "/")
            for m in MATCH_RE.finditer(text):
                found.append(m.group(0))

    walk(obj)
    return found


def _collect_fixture_candidates(page, captured_json: List[Any]) -> Tuple[List[str], List[str]]:
    """Collect every href plus canonical match candidates from DOM/HTML/JSON."""
    try:
        hrefs = page.locator("a[href]").evaluate_all(
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
    except Exception:
        hrefs = []

    candidates: List[str] = []
    for href in hrefs:
        if MATCH_RE.search(href or ""):
            candidates.append(href)

    try:
        html = page.content().replace("\\/", "/")
    except Exception:
        html = ""
    for m in MATCH_RE.finditer(html):
        candidates.append(m.group(0))

    for payload in captured_json:
        candidates.extend(_extract_match_urls_from_object(payload))

    return hrefs, candidates


def _save_discovery_debug(page, target: str, hrefs: List[str], response_urls: List[str]) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(DEBUG_DIR, "fixture_discovery.png"), full_page=True)
        with open(os.path.join(DEBUG_DIR, "fixture_discovery.html"), "w", encoding="utf-8") as fh:
            fh.write(page.content())
        with open(os.path.join(DEBUG_DIR, "fixture_discovery.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "target": target,
                    "final_url": page.url,
                    "title": page.title(),
                    "href_count": len(hrefs),
                    "hrefs": hrefs[:1000],
                    "response_urls": response_urls[-500:],
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )
        logger.info("Saved fixture discovery diagnostics in %s/", DEBUG_DIR)
    except Exception as exc:
        logger.warning("Could not save fixture discovery diagnostics: %s", exc)


def discover_fixtures(page) -> List[Fixture]:
    fixtures: Dict[str, Fixture] = {}
    all_hrefs: List[str] = []
    response_urls: List[str] = []
    captured_json: List[Any] = []
    last_target = ""

    def on_response(response):
        try:
            if "aiscore.com" not in response.url:
                return
            response_urls.append(response.url)
            ctype = (response.headers.get("content-type") or "").lower()
            if response.status == 200 and "json" in ctype:
                try:
                    captured_json.append(response.json())
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", on_response)
    try:
        for target in _target_home_urls():
            last_target = target
            logger.info("Opening AiScore fixture page: %s", target)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3500)
            except PlaywrightTimeoutError:
                logger.warning("Fixture page timed out: %s", target)
                continue

            # AiScore virtualises/lazy-loads parts of the fixture list.
            # Scroll in smaller steps and collect after each step so links that are
            # removed from the DOM later are still retained.
            for _ in range(10):
                hrefs, candidates = _collect_fixture_candidates(page, captured_json)
                all_hrefs.extend(hrefs)
                for href in candidates:
                    fixture = _normalise_fixture_href(href)
                    if fixture:
                        fixtures.setdefault(fixture.match_id, fixture)
                if len(fixtures) >= MAX_MATCHES:
                    break
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(350)

            # One final pass after scrolling.
            hrefs, candidates = _collect_fixture_candidates(page, captured_json)
            all_hrefs.extend(hrefs)
            for href in candidates:
                fixture = _normalise_fixture_href(href)
                if fixture:
                    fixtures.setdefault(fixture.match_id, fixture)

            logger.info(
                "Discovery pass found %d canonical match IDs from %s",
                len(fixtures),
                page.url,
            )
            if len(fixtures) >= min(MAX_MATCHES, 20):
                break

        out = list(fixtures.values())[:MAX_MATCHES]
        logger.info("Discovered %s unique AiScore match pages.", len(out))
        if not out:
            _save_discovery_debug(page, last_target, list(dict.fromkeys(all_hrefs)), response_urls)
        return out
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

def _is_decimal_odd(value: float) -> bool:
    return 1.0 < value <= 100.0


def _numbers(text: str) -> List[float]:
    vals = []
    for token in ODDS_RE.findall(text or ""):
        try:
            v = float(token)
        except ValueError:
            continue
        if _is_decimal_odd(v):
            vals.append(v)
    return vals


def _clean_bookmaker_name(text: str) -> str:
    cleaned = re.sub(r"\d{1,3}(?:\.\d+)?", " ", text or "")
    cleaned = re.sub(r"\b(?:opening|pre-match|prematch|in-play|inplay|odds|1x2|1\s*x\s*2)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|:\n\t")
    return cleaned[:50] or "AiScore"


def _extract_1x2_section(page) -> List[OddsRow]:
    """Parse the first rendered bookmaker/market row in AiScore's 1X2 section.

    The first six decimal values after the 1X2 heading are the first displayed
    row's opening H/D/A and pre-match H/D/A. A seventh-ninth triple, when
    present, is in-play. We deliberately stop after the first row so nested
    mobile markup cannot shift bookmaker boundaries and create false signals.
    """
    body = page.locator("body").inner_text(timeout=8000)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

    start = None
    for i, line in enumerate(lines):
        compact = re.sub(r"\s+", "", line).upper()
        if compact == "1X2":
            start = i + 1
            break
    if start is None:
        return []

    values: List[float] = []
    for line in lines[start:]:
        low = line.lower()
        if low.startswith("asian handicap") or low == "handicap" or low.startswith("goals") or low.startswith("total goals"):
            break

        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3})?", line):
            value = float(line)
            if _is_decimal_odd(value):
                values.append(value)
        else:
            for token in re.findall(r"(?<![\d.])\d{1,3}\.\d{1,3}(?![\d.])", line):
                value = float(token)
                if _is_decimal_odd(value):
                    values.append(value)

        if len(values) >= 9:
            break

    if len(values) < 6:
        return []

    opening = tuple(values[0:3])
    prematch = tuple(values[3:6])
    inplay = tuple(values[6:9]) if len(values) >= 9 else None
    return [OddsRow("AiScore primary row", opening, prematch, inplay)]  # type: ignore[arg-type]


def _extract_rows_from_dom(page) -> List[OddsRow]:
    # AiScore has changed class names several times. We intentionally use semantic row-like
    # containers and parse their rendered text instead of depending on a private CSS class.
    texts = page.locator("tr, [role='row']").evaluate_all(
        "els => els.map(e => (e.innerText || '').trim()).filter(Boolean)"
    )

    # Mobile odds pages often use div-based rows. Add compact elements containing enough odds.
    compact = page.locator("div").evaluate_all(
        r"""els => els.map(e => (e.innerText || '').trim())
        .filter(t => t && t.length < 220 && /\d+\.\d+/.test(t))"""
    )
    texts.extend(compact)

    seen = set()
    rows: List[OddsRow] = []
    for text in texts:
        key = " ".join(text.split())
        if key in seen:
            continue
        seen.add(key)
        nums = _numbers(text)
        # A bookmaker line in the dedicated odds section normally has
        # opening H/D/A + pre-match H/D/A (+ optional in-play H/D/A).
        if len(nums) < 6:
            continue
        opening = tuple(nums[0:3])
        prematch = tuple(nums[3:6])
        if not all(_is_decimal_odd(x) for x in opening + prematch):
            continue
        inplay = tuple(nums[6:9]) if len(nums) >= 9 and all(_is_decimal_odd(x) for x in nums[6:9]) else None
        rows.append(
            OddsRow(
                bookmaker=_clean_bookmaker_name(text),
                opening=opening,  # type: ignore[arg-type]
                prematch=prematch,  # type: ignore[arg-type]
                inplay=inplay,  # type: ignore[arg-type]
            )
        )

    # Remove duplicate numeric rows created by nested divs.
    unique: Dict[Tuple[Tuple[float, ...], Tuple[float, ...]], OddsRow] = {}
    for row in rows:
        unique.setdefault((row.opening, row.prematch), row)
    return list(unique.values())


def _extract_aggregate_from_body(page) -> List[OddsRow]:
    """Fallback for the desktop/aggregate layout visible on AiScore match pages."""
    body = page.locator("body").inner_text(timeout=8000)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

    start = None
    for i, line in enumerate(lines):
        if re.fullmatch(r"1\s*X\s*2", line, flags=re.I) or line.upper() == "1X2":
            start = i + 1
            break
    if start is None:
        return []

    stop_words = ("asian handicap", "handicap", "goals", "total goals", "corners", "total corners")
    section: List[str] = []
    for line in lines[start:]:
        if any(line.lower().startswith(word) for word in stop_words):
            break
        section.append(line)

    vals: List[float] = []
    for line in section:
        # Avoid scores/times; the odds block consists mostly of decimal values.
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3})?", line):
            v = float(line)
            if _is_decimal_odd(v):
                vals.append(v)
        if len(vals) >= 9:
            break

    if len(vals) < 6:
        return []

    opening = tuple(vals[0:3])
    prematch = tuple(vals[3:6])
    inplay = tuple(vals[6:9]) if len(vals) >= 9 else None
    return [OddsRow("AiScore aggregate", opening, prematch, inplay)]  # type: ignore[arg-type]


def save_debug_capture(page, fixture: Fixture, reason: str) -> None:
    global _debug_capture_count
    if _debug_capture_count >= DEBUG_CAPTURE_LIMIT:
        return
    _debug_capture_count += 1
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", f"{fixture.match_id}_{reason}")[:120]
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{safe}.png"), full_page=True)
        with open(os.path.join(DEBUG_DIR, f"{safe}.html"), "w", encoding="utf-8") as fh:
            fh.write(page.content())
        logger.info("Saved debug capture for %s", fixture.match_id)
    except Exception as exc:
        logger.debug("Debug capture failed: %s", exc)



def _update_fixture_names_from_page(page, fixture: Fixture) -> None:
    """Improve team names from AiScore's rendered title when the canonical slug is ambiguous."""
    try:
        title = page.title().strip()
    except Exception:
        return
    patterns = [
        r"(?:^|\d{4}/\d{2}/\d{2}\s+)(.+?)\s+vs\s+(.+?)\s+betting odds\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+live score.*?\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+-\s+AiScore",
    ]
    for pattern in patterns:
        m = re.search(pattern, title, flags=re.I)
        if m:
            home = re.sub(r"\s+", " ", m.group(1)).strip(" -")
            away = re.sub(r"\s+", " ", m.group(2)).strip(" -")
            if home and away:
                fixture.home = home
                fixture.away = away
                return


def _save_captured_odds_json(fixture: Fixture, captured_json: List[Dict[str, Any]]) -> None:
    if not captured_json:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", fixture.match_id)[:80]
        with open(os.path.join(DEBUG_DIR, f"{safe}_network.json"), "w", encoding="utf-8") as fh:
            json.dump(captured_json, fh, indent=2, ensure_ascii=False)
        logger.info("Saved %d captured odds/match JSON payload(s) for %s", len(captured_json), fixture.match_id)
    except Exception as exc:
        logger.debug("Could not save captured odds JSON: %s", exc)

def scrape_fixture_odds(page, fixture: Fixture) -> List[OddsRow]:
    logger.info("Odds page: %s vs %s -> %s", fixture.home, fixture.away, fixture.odds_url)

    captured_json: List[Dict[str, Any]] = []

    def on_response(response):
        if "aiscore.com" not in response.url or response.status != 200:
            return
        if not any(key in response.url.lower() for key in ("odd", "match", "market")):
            return
        try:
            data = response.json()
            if isinstance(data, dict):
                captured_json.append({"url": response.url, "data": data})
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(fixture.odds_url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2800)
        _update_fixture_names_from_page(page, fixture)

        # The dedicated page can default to Asian Handicap. Select 1X2 when the tab exists.
        for label in ("1 X 2", "1X2"):
            try:
                loc = page.get_by_text(label, exact=True)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2500)
                    page.wait_for_timeout(900)
                    break
            except Exception:
                pass

        rows = _extract_1x2_section(page)
        if not rows:
            rows = _extract_rows_from_dom(page)
        if not rows:
            rows = _extract_aggregate_from_body(page)

        # If mobile layout did not yield data, try the normal match page Odds tab; current
        # AiScore pages expose opening and pre-match 1X2 directly there as well.
        if not rows:
            page.goto(fixture.url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(2200)
            rows = _extract_aggregate_from_body(page)

        if not rows:
            logger.warning(
                "NO_ODDS_DATA for %s vs %s (captured %d odds/match JSON responses)",
                fixture.home,
                fixture.away,
                len(captured_json),
            )
            save_debug_capture(page, fixture, "no_odds")
            _save_captured_odds_json(fixture, captured_json)
        else:
            logger.info("Parsed %d 1X2 odds row(s) for %s vs %s", len(rows), fixture.home, fixture.away)
        return rows
    except PlaywrightTimeoutError:
        logger.warning("ODDS_PAGE_TIMEOUT for %s vs %s", fixture.home, fixture.away)
        save_debug_capture(page, fixture, "timeout")
        return []
    except Exception as exc:
        logger.warning("ODDS_PAGE_ERROR for %s vs %s: %s", fixture.home, fixture.away, exc)
        save_debug_capture(page, fixture, "error")
        return []
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def _fair_probability(odds: Tuple[float, float, float], index: int) -> float:
    inv = [1.0 / x for x in odds]
    vig = sum(inv)
    return inv[index] / vig if vig else 0.0


def evaluate_odds_row(row: OddsRow) -> List[Candidate]:
    """
    Evaluate Home and Away separately.

    Strict clean shape:
      - selected side opening price 1.50-2.50
      - selected side drops
      - the other team side does not drop
      - draw does not drop

    AiScore's page gives opening -> pre-match snapshots, not every historical tick, so this
    is labelled PROXY/SHARP rather than pretending it is a verified first-four sequence.
    """
    op = row.opening
    cur = row.prematch
    out: List[Candidate] = []

    for side, idx, opp_idx in (("HOME", 0, 2), ("AWAY", 2, 0)):
        selected_open = op[idx]
        selected_cur = cur[idx]
        if not (1.50 <= selected_open <= 2.50):
            continue

        selected_drop = selected_open - selected_cur
        opp_drop = op[opp_idx] - cur[opp_idx]
        draw_drop = op[1] - cur[1]
        if selected_drop <= 0:
            continue

        drop_pct = (selected_drop / selected_open) * 100.0
        fair_shift = (_fair_probability(cur, idx) - _fair_probability(op, idx)) * 100.0

        other_team_not_dropping = opp_drop <= 0.005
        draw_not_dropping = draw_drop <= 0.005
        clean_shape = other_team_not_dropping and draw_not_dropping

        # Meaningful line shortening in decimal odds. Old code required 1.20-2.00 absolute
        # points, which is unrealistic for prices in the 1.50-2.50 range.
        if clean_shape and (drop_pct >= 5.0 or fair_shift >= 2.5):
            status = "SHARP_PROXY"
            reason = "selected side shortened while draw and opponent held/rose"
        elif clean_shape and (drop_pct >= 2.0 or fair_shift >= 1.0):
            status = "NEAR_MISS"
            reason = "clean one-sided movement, but strength is below sharp threshold"
        elif drop_pct >= 3.0 and (not other_team_not_dropping or not draw_not_dropping):
            status = "NEAR_MISS"
            conflict = []
            if not draw_not_dropping:
                conflict.append("draw also shortened")
            if not other_team_not_dropping:
                conflict.append("opponent also shortened")
            reason = "; ".join(conflict)
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
            )
        )

    return out


def _best_candidate(candidates: List[Candidate]) -> Optional[Candidate]:
    if not candidates:
        return None
    rank = {"NEAR_MISS": 1, "SHARP_PROXY": 2}
    return max(candidates, key=lambda c: (rank.get(c.status, 0), c.fair_prob_shift, c.drop_pct))


def analyse_fixture(rows: List[OddsRow]) -> Optional[Candidate]:
    all_candidates: List[Candidate] = []
    for row in rows:
        all_candidates.extend(evaluate_odds_row(row))
    return _best_candidate(all_candidates)


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
        f"🏦 <b>Source:</b> {candidate.bookmaker}\n\n"
        f"📉 <b>Opening → Pre-match 1X2</b>\n"
        f"Home: {op[0]:.2f} → {cur[0]:.2f}\n"
        f"Draw: {op[1]:.2f} → {cur[1]:.2f}\n"
        f"Away: {op[2]:.2f} → {cur[2]:.2f}\n\n"
        f"Drop: {candidate.drop:.2f} ({candidate.drop_pct:.1f}%)\n"
        f"No-vig probability shift: +{candidate.fair_prob_shift:.2f} pp\n"
        f"Reason: {candidate.reason}\n\n"
        f"ℹ️ Snapshot signal: AiScore opening → pre-match; not claimed as first-4 verified."
    )


def run_engine() -> None:
    init_db()
    stats = {
        "fixtures": 0,
        "with_odds": 0,
        "no_odds": 0,
        "sharp": 0,
        "near": 0,
        "sent": 0,
    }

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]

    with sync_playwright() as p:
        browser_options: Dict[str, Any] = {"headless": HEADLESS, "args": launch_args}
        if PROXY_URL:
            parsed = urlparse(PROXY_URL if "://" in PROXY_URL else f"http://{PROXY_URL}")
            proxy: Dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy["username"] = parsed.username
            if parsed.password:
                proxy["password"] = parsed.password
            browser_options["proxy"] = proxy

        browser = p.chromium.launch(**browser_options)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="Indian/Mauritius",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "DNT": "1",
            },
        )
        page = context.new_page()
        page.set_default_timeout(10000)

        try:
            fixtures = discover_fixtures(page)
        except Exception as exc:
            logger.exception("Fixture discovery failed: %s", exc)
            fixtures = []

        stats["fixtures"] = len(fixtures)
        if not fixtures:
            logger.error("No AiScore match links discovered.")
            send_telegram_alert(
                "🚨 <b>ODDS SCANNER ERROR</b>\nAiScore loaded, but no match URLs were discovered. Check the workflow log."
            )
            browser.close()
            return

        for index, fixture in enumerate(fixtures, start=1):
            logger.info("[%d/%d] Processing %s vs %s", index, len(fixtures), fixture.home, fixture.away)
            rows = scrape_fixture_odds(page, fixture)
            if not rows:
                stats["no_odds"] += 1
                continue
            stats["with_odds"] += 1

            candidate = analyse_fixture(rows)
            if not candidate:
                logger.info("REJECTED: %s vs %s - no clean/near movement", fixture.home, fixture.away)
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
                # Even with no Telegram secrets, print the candidate in Actions logs.
                logger.info("CANDIDATE %s", re.sub(r"<[^>]+>", "", message).replace("\n", " | "))

            time.sleep(0.25)

        browser.close()

    target = SCAN_DATE or "today"
    summary = (
        f"✅ <b>SCAN COMPLETE ({target})</b>\n"
        f"Matches discovered: {stats['fixtures']}\n"
        f"Odds pages parsed: {stats['with_odds']}\n"
        f"No odds available: {stats['no_odds']}\n"
        f"Sharp proxies: {stats['sharp']}\n"
        f"Near misses: {stats['near']}\n"
        f"Alerts sent: {stats['sent']}"
    )
    logger.info(re.sub(r"<[^>]+>", "", summary).replace("\n", " | "))
    send_telegram_alert(summary)


if __name__ == "__main__":
    run_engine()
