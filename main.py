import os
import re
import json
import time
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
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

MOBILE_BASE = "https://m.aiscore.com"
DESKTOP_BASE = "https://www.aiscore.com"
MATCH_RE = re.compile(r"/match-([^/?#]+)/([a-z0-9]+)", re.I)
DECIMAL_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3})?)(?![\d.])")

logger = logging.getLogger("OddsEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@dataclass
class Fixture:
    match_id: str
    slug: str
    home: str
    away: str

    @property
    def mobile_url(self) -> str:
        return f"{MOBILE_BASE}/match-{self.slug}/{self.match_id}"

    @property
    def mobile_odds_url(self) -> str:
        return f"{self.mobile_url}/odds"


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


def record_snapshots(match_id: str, rows: List[OddsRow]) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    for idx, row in enumerate(rows, start=1):
        cur.execute(
            """
            SELECT home, draw, away
            FROM odds_snapshots
            WHERE match_id=? AND market_row=?
            ORDER BY captured_at DESC, rowid DESC
            LIMIT 1
            """,
            (match_id, idx),
        )
        last = cur.fetchone()
        current = tuple(float(x) for x in row.prematch)
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
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    if PROXY_URL:
        proxy = PROXY_URL if "://" in PROXY_URL else f"http://{PROXY_URL}"
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def is_security_challenge(html: str, title: str = "") -> bool:
    hay = f"{title}\n{html[:120000]}".lower()
    signatures = (
        "just a moment",
        "performing security verification",
        "verify you are human",
        "cf-chl-",
        "challenge-platform",
        "cloudflare ray id",
    )
    return any(sig in hay for sig in signatures)


def fetch_html(session: requests.Session, url: str, label: str) -> Optional[str]:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            text = r.text or ""
            title = ""
            if text:
                m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
                if m:
                    title = re.sub(r"\s+", " ", m.group(1)).strip()
            logger.info("%s HTTP %s -> %s", label, r.status_code, r.url)
            if is_security_challenge(text, title):
                logger.warning("%s SECURITY_CHALLENGE at %s", label, r.url)
                return None
            if r.status_code == 200 and text:
                return text
            logger.warning("%s HTTP %s", label, r.status_code)
        except Exception as exc:
            logger.warning("%s attempt %d failed: %s", label, attempt + 1, exc)
        time.sleep(1.5 * (attempt + 1))
    return None


def save_text_debug(name: str, html: str = "", data: Optional[Dict[str, Any]] = None) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:100]
    if html:
        with open(os.path.join(DEBUG_DIR, f"{safe}.html"), "w", encoding="utf-8") as fh:
            fh.write(html)
    if data is not None:
        with open(os.path.join(DEBUG_DIR, f"{safe}.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)


def humanize_slug(slug: str) -> Tuple[str, str]:
    parts = re.split(r"-vs-", slug, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        parts = re.split(r"-v-", slug, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        return parts[0].replace("-", " ").title(), parts[1].replace("-", " ").title()
    return slug.replace("-", " ").title(), "Opponent"


def normalise_fixture_href(href: str) -> Optional[Fixture]:
    if not href:
        return None
    m = MATCH_RE.search(href.replace("\\/", "/"))
    if not m:
        return None
    slug, match_id = m.group(1), m.group(2)
    home, away = humanize_slug(slug)
    return Fixture(match_id=match_id, slug=slug, home=home, away=away)


def mobile_discovery_urls() -> List[str]:
    if not SCAN_DATE:
        return [
            f"{MOBILE_BASE}/today-matches/football",
            f"{MOBILE_BASE}/today-matches",
            f"{MOBILE_BASE}/",
        ]
    raw = SCAN_DATE
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return [
        f"{MOBILE_BASE}/today-matches/football?date={raw}",
        f"{MOBILE_BASE}/today-matches?date={raw}",
    ]


def discover_fixtures(session: requests.Session) -> List[Fixture]:
    fixtures: Dict[str, Fixture] = {}
    attempts: List[Dict[str, Any]] = []
    last_html = ""

    for target in mobile_discovery_urls():
        logger.info("Opening AiScore mobile fixture page: %s", target)
        html = fetch_html(session, target, "DISCOVERY")
        if not html:
            attempts.append({"url": target, "status": "blocked_or_failed"})
            continue
        last_html = html
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
        candidates = list(hrefs)
        candidates.extend(m.group(0) for m in MATCH_RE.finditer(html.replace("\\/", "/")))

        before = len(fixtures)
        for href in candidates:
            fixture = normalise_fixture_href(href)
            if fixture:
                fixtures.setdefault(fixture.match_id, fixture)
                if len(fixtures) >= MAX_MATCHES:
                    break

        attempts.append(
            {
                "url": target,
                "status": "ok",
                "href_count": len(hrefs),
                "new_matches": len(fixtures) - before,
                "total_matches": len(fixtures),
            }
        )
        logger.info("Discovery found %d unique match IDs so far", len(fixtures))
        if len(fixtures) >= min(MAX_MATCHES, 20):
            break

    if not fixtures:
        save_text_debug("fixture_discovery_mobile", last_html, {"attempts": attempts})
    else:
        save_text_debug("fixture_discovery_mobile_summary", data={"attempts": attempts, "count": len(fixtures)})

    return list(fixtures.values())[:MAX_MATCHES]


def _is_decimal_odd(value: float) -> bool:
    return 1.0 < value <= 100.0


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


def _extract_mobile_1x2_rows(html: str) -> List[OddsRow]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]

    # The mobile page is server-rendered. Its current layout is:
    # tabs -> "Opening odds" -> "1 X 2" -> repeating 3-number lines.
    # Each provider row normally exposes opening, pre-match, and current/in-play.
    start = None
    for i, line in enumerate(lines):
        if line.lower() == "opening odds":
            for j in range(i + 1, min(i + 12, len(lines))):
                if re.sub(r"\s+", "", lines[j]).upper() == "1X2":
                    start = j + 1
                    break
            if start is not None:
                break

    if start is None:
        # Fallback: use the last 1X2 marker near the odds section.
        markers = [i for i, line in enumerate(lines) if re.sub(r"\s+", "", line).upper() == "1X2"]
        if markers:
            start = markers[-1] + 1
    if start is None:
        return []

    triples: List[Tuple[float, float, float]] = []
    for line in lines[start:]:
        low = line.lower()
        if (
            low.startswith("gamble responsibly")
            or low.startswith("opening odds pre-match odds")
            or low.startswith("asian handicap")
            or low.startswith("total goals")
            or low.startswith("total corners")
        ):
            if triples:
                break
            continue
        triple = _line_triple(line)
        if triple:
            triples.append(triple)
            if len(triples) >= 30:
                break

    if len(triples) < 2:
        return []

    rows: List[OddsRow] = []

    # Current AiScore mobile pages generally expose 3 triples per source:
    # opening / pre-match / current-or-in-play. If only 2 are present, preserve that too.
    if len(triples) >= 3 and len(triples) % 3 == 0:
        group_size = 3
    elif len(triples) % 2 == 0:
        group_size = 2
    else:
        # Mixed markup: take complete triples in groups of three, then one final pair if present.
        group_size = 3 if len(triples) >= 3 else 2

    pos = 0
    row_no = 1
    while pos + 1 < len(triples):
        opening = triples[pos]
        prematch = triples[pos + 1]
        inplay = triples[pos + 2] if group_size == 3 and pos + 2 < len(triples) else None
        rows.append(
            OddsRow(
                bookmaker=f"AiScore market row {row_no}",
                opening=opening,
                prematch=prematch,
                inplay=inplay,
            )
        )
        row_no += 1
        pos += group_size
        if row_no > 10:
            break

    return rows


def update_fixture_names(html: str, fixture: Fixture) -> None:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(r"^\d{4}/\d{2}/\d{2}\s+", "", title).strip()
    patterns = [
        r"(.+?)\s+vs\s+(.+?)\s+betting odds\s+-\s+AiScore",
        r"(.+?)\s+vs\s+(.+?)\s+live score.*?\s+-\s+AiScore",
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


def scrape_fixture_odds(session: requests.Session, fixture: Fixture) -> List[OddsRow]:
    logger.info("Odds page: %s vs %s -> %s", fixture.home, fixture.away, fixture.mobile_odds_url)
    html = fetch_html(session, fixture.mobile_odds_url, f"ODDS {fixture.match_id}")
    if not html:
        return []

    update_fixture_names(html, fixture)
    rows = _extract_mobile_1x2_rows(html)
    if rows:
        logger.info("Parsed %d mobile 1X2 row(s) for %s vs %s", len(rows), fixture.home, fixture.away)
        record_snapshots(fixture.match_id, rows)
        return rows

    logger.warning("NO_1X2_ODDS for %s vs %s", fixture.home, fixture.away)
    save_text_debug(f"{fixture.match_id}_no_1x2", html, {"url": fixture.mobile_odds_url})
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
        logger.info("REJECTED: mixed Home/Away shortening across AiScore market rows")
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

    # Cross-row agreement strengthens a snapshot signal without pretending it is
    # a historical first-four tick sequence.
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
        f"📊 <b>Agreement:</b> {candidate.agreeing_rows}/{candidate.total_rows} AiScore rows\n\n"
        f"📉 <b>Opening → Pre-match 1X2</b>\n"
        f"Home: {op[0]:.2f} → {cur[0]:.2f}\n"
        f"Draw: {op[1]:.2f} → {cur[1]:.2f}\n"
        f"Away: {op[2]:.2f} → {cur[2]:.2f}\n\n"
        f"Drop: {candidate.drop:.2f} ({candidate.drop_pct:.1f}%)\n"
        f"No-vig probability shift: +{candidate.fair_prob_shift:.2f} pp\n"
        f"Reason: {candidate.reason}\n\n"
        f"ℹ️ AiScore opening → pre-match snapshot. Repeated runs are stored separately; this is not labelled as first-4 verified."
    )


def run_engine() -> None:
    init_db()
    stats = {"fixtures": 0, "with_odds": 0, "no_odds": 0, "sharp": 0, "near": 0, "sent": 0}
    session = make_session()

    fixtures = discover_fixtures(session)
    stats["fixtures"] = len(fixtures)

    if not fixtures:
        logger.error("No AiScore mobile match links discovered.")
        send_telegram_alert(
            "🚨 <b>ODDS SCANNER ERROR</b>\n"
            "AiScore mobile discovery returned no match URLs. "
            "The workflow artifact now shows whether the page was blocked or its markup changed."
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
