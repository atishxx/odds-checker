import os
import json
import time
import sqlite3
import logging
import requests
from typing import List, Dict, Any, Optional
from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

# System Configurations
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SCAN_DATE = os.getenv("SCAN_DATE", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()  # Format: "http://user:pass@ip:port"
DB_FILE = "odds_tracker.db"

# Configure Logging Stream
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("OddsEngine")

# --- DATABASE STATE ENGINE ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerted_matches (
            match_id TEXT PRIMARY KEY,
            alert_type TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_already_alerted(match_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM alerted_matches WHERE match_id = ?", (match_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def record_alert(match_id: str, alert_type: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO alerted_matches (match_id, alert_type) VALUES (?, ?)",
        (match_id, alert_type)
    )
    conn.commit()
    conn.close()

# --- TELEGRAM NOTIFICATION SYSTEM ---
def send_telegram_alert(message: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        logger.warning("Telegram credentials missing. Skipping dispatch.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                return True
            logger.warning(f"Telegram returned HTTP {res.status_code}")
        except Exception as e:
            logger.error(f"Telegram alert error (Attempt {attempt+1}): {e}")
            time.sleep(1)
    return False

# --- DATA PARSER & QUANT ENGINE ---
def extract_odds_history(match: Dict[str, Any]) -> List[List[float]]:
    odds_hist = match.get("oddsHistory", {}).get("1x2", [])
    if len(odds_hist) >= 2:
        return odds_hist

    odds_dict = match.get("odds", {})
    if isinstance(odds_dict, dict):
        hist = odds_dict.get("1x2", {}).get("history", [])
        if len(hist) >= 2:
            return hist

    rates = match.get("rates", {}) or match.get("odds", {})
    if isinstance(rates, dict) and "1x2" in rates:
        line_data = rates["1x2"]
        if "open" in line_data and "current" in line_data:
            return [line_data["open"], line_data["current"]]

    return []

def eval_classic_drop(odds_history: List[List[float]]) -> Optional[str]:
    if len(odds_history) < 2:
        return None
    
    opening_home, opening_draw = odds_history[0][0], odds_history[0][1]
    current_home, current_draw = odds_history[-1][0], odds_history[-1][1]
    
    drop_amount = opening_home - current_home

    trajectory_ok = True
    if len(odds_history) >= 4:
        home_line = [entry[0] for entry in odds_history[:4]]
        trajectory_ok = all(home_line[i] > home_line[i+1] for i in range(len(home_line)-1))

    if (current_draw >= opening_draw) and trajectory_ok:
        if drop_amount >= 2.0:
            return "MATCH"
        elif 1.2 <= drop_amount < 2.0:
            return "NEAR_MISS"
    return None

def eval_sharp_move(odds_history: List[List[float]]):
    if len(odds_history) < 2:
        return None, 0.0

    open_h, open_d, open_a = odds_history[0][0], odds_history[0][1], odds_history[0][2]
    curr_h, curr_d, curr_a = odds_history[-1][0], odds_history[-1][1], odds_history[-1][2]

    if any(x <= 1.0 for x in [open_h, open_d, open_a, curr_h, curr_d, curr_a]):
        return None, 0.0

    raw_open_prob_h = 1.0 / open_h
    raw_curr_prob_h = 1.0 / curr_h

    open_vig = (1.0 / open_h) + (1.0 / open_d) + (1.0 / open_a)
    curr_vig = (1.0 / curr_h) + (1.0 / curr_d) + (1.0 / curr_a)

    fair_open_prob_h = raw_open_prob_h / open_vig
    fair_curr_prob_h = raw_curr_prob_h / curr_vig

    prob_shift = (fair_curr_prob_h - fair_open_prob_h) * 100.0

    fair_open_prob_d = (1.0 / open_d) / open_vig
    fair_curr_prob_d = (1.0 / curr_d) / curr_vig
    if (fair_curr_prob_d - fair_open_prob_d) > 0.03:
        return None, prob_shift

    if len(odds_history) >= 3:
        home_history = [entry[0] for entry in odds_history]
        drops = sum(1 for i in range(len(home_history) - 1) if home_history[i] > home_history[i + 1])
        if drops < 1:
            return None, prob_shift

    if prob_shift >= 8.0:
        return "MATCH", prob_shift
    elif 5.0 <= prob_shift < 8.0:
        return "NEAR_MISS", prob_shift

    return None, prob_shift

# --- DUAL-ENGINE SCRAPING ARCHITECTURE ---
def fetch_via_cffi() -> List[Dict[str, Any]]:
    """Primary Execution Tier: C-Based TLS Fingerprint Spoofing"""
    url = "https://api.aiscore.com/api/v1/match/list"
    if SCAN_DATE:
        url = f"https://api.aiscore.com/api/v1/match/list?date={SCAN_DATE}"

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.aiscore.com/",
        "Origin": "https://www.aiscore.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    try:
        logger.info("Executing Tier 1 Scraping: TLS Impersonation (curl_cffi)...")
        response = cffi_requests.get(url, headers=headers, proxies=proxies, impersonate="chrome120", timeout=15)
        if response.status_code == 200:
            data = response.json()
            matches = data.get("data", {}).get("list", [])
            if matches:
                logger.info(f"Tier 1 Success: Acquired {len(matches)} match records.")
                return matches
    except Exception as e:
        logger.error(f"Tier 1 Scraper failed: {e}")
    return []

def fetch_via_playwright() -> List[Dict[str, Any]]:
    """Secondary Execution Tier: Headless Browser Network Interception"""
    logger.info("Executing Tier 2 Scraping: Playwright DOM Interceptor...")
    captured_matches = []

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage"
    ]

    with sync_playwright() as p:
        browser_options = {"headless": True, "args": launch_args}
        if PROXY_URL:
            browser_options["proxy"] = {"server": PROXY_URL}

        browser = p.chromium.launch(**browser_options)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()

        def intercept_response(response):
            nonlocal captured_matches
            if "match" in response.url and response.status == 200:
                try:
                    data = response.json()
                    match_list = data.get("data", {}).get("list", []) or data.get("list", [])
                    if match_list and isinstance(match_list, list):
                        captured_matches = match_list
                except Exception:
                    pass

        page.on("response", intercept_response)

        target_url = "https://www.aiscore.com/"
        if SCAN_DATE:
            target_url = f"https://www.aiscore.com/?date={SCAN_DATE}"

        try:
            page.goto(target_url, wait_until="networkidle", timeout=35000)
            page.wait_for_timeout(4000)
        except Exception as e:
            logger.error(f"Tier 2 Playwright Execution error: {e}")
        finally:
            browser.close()

    logger.info(f"Tier 2 Scraping Complete: Captured {len(captured_matches)} match records.")
    return captured_matches

# --- PIPELINE CONTROLLER ---
def run_engine():
    init_db()

    # Automatic Failover Routing
    matches = fetch_via_cffi()
    if not matches:
        matches = fetch_via_playwright()

    if not matches:
        logger.error("Critical Failure: All scraping tiers exhausted with zero records.")
        send_telegram_alert("🚨 <b>CRITICAL SYSTEM ALERT</b>\nScraper execution failed across all tiers.")
        return

    found_sharp = 0
    found_classic = 0
    found_near_misses = 0
    total_scanned = len(matches)

    for match in matches:
        match_id = str(match.get("id") or match.get("matchId") or f"{match.get('homeTeam', {}).get('name')}_{match.get('awayTeam', {}).get('name')}")
        home = match.get("homeTeam", {}).get("name", "Home")
        away = match.get("awayTeam", {}).get("name", "Away")
        league = match.get("leagueName", "League")

        odds_history = extract_odds_history(match)
        if not odds_history:
            continue

        classic_res = eval_classic_drop(odds_history)
        sharp_res, prob_shift = eval_sharp_move(odds_history)

        is_sharp_match = (sharp_res == "MATCH")
        is_classic_match = (classic_res == "MATCH")
        is_near_miss = (sharp_res == "NEAR_MISS" or classic_res == "NEAR_MISS") and not (is_sharp_match or is_classic_match)

        if is_sharp_match or is_classic_match or is_near_miss:
            # Deduplication Barrier
            if is_already_alerted(match_id):
                logger.info(f"Deduplicated match alert: {home} vs {away}")
                continue

            open_h, open_d, open_a = odds_history[0][0], odds_history[0][1], odds_history[0][2]
            curr_h, curr_d, curr_a = odds_history[-1][0], odds_history[-1][1], odds_history[-1][2]

            alert_type = []
            if is_sharp_match:
                found_sharp += 1
                alert_type.append(f"⚡ <b>SHARP STEAM</b> (+{prob_shift:.1f}% Shift)")
            if is_classic_match:
                found_classic += 1
                alert_type.append(f"💥 <b>CLASSIC CRUSH</b> (Drop >= 2.0)")

            if is_near_miss:
                found_near_misses += 1
                near_details = []
                if sharp_res == "NEAR_MISS":
                    near_details.append(f"Sharp Prob Shift +{prob_shift:.1f}% [Target +8.0%]")
                if classic_res == "NEAR_MISS":
                    near_details.append(f"Home Drop {open_h - curr_h:.2f} [Target >= 2.0]")
                alert_type.append(f"🎯 <b>WATCHLIST</b> ({', '.join(near_details)})")

            header_icon = "🚨 <b>APEX MATCH TRIGGER</b>" if (is_sharp_match or is_classic_match) else "👀 <b>HIGH PRIORITY WATCHLIST</b>"

            msg = (
                f"{header_icon}\n"
                f"<b>Status:</b> {' | '.join(alert_type)}\n\n"
                f"🏆 <b>League:</b> {league}\n"
                f"⚽ <b>Match:</b> {home} vs {away}\n\n"
                f"📊 <b>1X2 Odds Movement:</b>\n"
                f"• Home: {open_h} ➔ {curr_h}\n"
                f"• Draw: {open_d} ➔ {curr_d}\n"
                f"• Away: {open_a} ➔ {curr_a}"
            )
            
            if send_telegram_alert(msg):
                record_alert(match_id, "|".join(alert_type))
            time.sleep(0.5)

    target = f"Date [{SCAN_DATE}]" if SCAN_DATE else "Today"
    send_telegram_alert(
        f"⚡ <b>SCAN COMPLETE ({target})</b>\n"
        f"• Matches Scanned: {total_scanned}\n"
        f"• 🔥 Sharp Steam Hits: {found_sharp}\n"
        f"• 💥 Classic Line Crushes: {found_classic}\n"
        f"• 🎯 Watchlist Candidates: {found_near_misses}"
    )

if __name__ == "__main__":
    run_engine()
