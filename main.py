import os
import requests
import time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCAN_DATE = os.getenv("SCAN_DATE", "").strip()

def send_telegram_alert(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        except Exception as e:
            print(f"Telegram send error: {e}")

def is_true_sharp_move(odds_history):
    """
    Calculates Vig-Free Implied Probability shift & filters out public/margin noise.
    Returns: (is_sharp: bool, prob_shift_pct: float, reason: str)
    """
    if len(odds_history) < 3:
        return False, 0.0, "Insufficient odds history"

    # Opening lines [Home, Draw, Away] vs Current lines
    open_h, open_d, open_a = odds_history[0][0], odds_history[0][1], odds_history[0][2]
    curr_h, curr_d, curr_a = odds_history[-1][0], odds_history[-1][1], odds_history[-1][2]

    # Guard against invalid or 0 odds
    if any(x <= 1.0 for x in [open_h, open_d, open_a, curr_h, curr_d, curr_a]):
        return False, 0.0, "Invalid odds values"

    # 1. Raw Implied Probabilities
    raw_open_prob_h = 1.0 / open_h
    raw_curr_prob_h = 1.0 / curr_h

    # 2. Calculate Bookmaker Vig (Margin)
    open_vig = (1.0 / open_h) + (1.0 / open_d) + (1.0 / open_a)
    curr_vig = (1.0 / curr_h) + (1.0 / curr_d) + (1.0 / curr_a)

    # 3. Calculate Vig-Free Fair Win Probabilities
    fair_open_prob_h = raw_open_prob_h / open_vig
    fair_curr_prob_h = raw_curr_prob_h / curr_vig

    # Percentage jump in true win probability
    prob_shift = (fair_curr_prob_h - fair_open_prob_h) * 100.0

    # --- SHARP FILTERS ---

    # Rule A: True win probability must jump by at least +8.0%
    if prob_shift < 8.0:
        return False, prob_shift, "Probability shift < +8%"

    # Rule B: Draw hedge shield (Reject if draw probability also increased significantly)
    fair_open_prob_d = (1.0 / open_d) / open_vig
    fair_curr_prob_d = (1.0 / curr_d) / curr_vig
    if (fair_curr_prob_d - fair_open_prob_d) > 0.03:
        return False, prob_shift, "Draw probability increased (mixed money signal)"

    # Rule C: Trajectory check (At least 2 consecutive downward steps)
    home_history = [entry[0] for entry in odds_history]
    drops = sum(1 for i in range(len(home_history) - 1) if home_history[i] > home_history[i + 1])
    if drops < 2:
        return False, prob_shift, "Inconsistent downward line trajectory"

    return True, prob_shift, "Genuine Sharp Move"

def check_odds():
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.aiscore.com/",
        "Origin": "https://www.aiscore.com",
    }
    
    url = "https://api.aiscore.com/api/v1/match/list"
    if SCAN_DATE:
        url = f"https://api.aiscore.com/api/v1/match/list?date={SCAN_DATE}"
        
    matches = []

    try:
        session.get("https://www.aiscore.com/", headers=headers, timeout=10)
        time.sleep(1)
        
        response = session.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            matches = data.get("data", {}).get("list", [])
    except Exception as e:
        print(f"Error fetching data: {e}")

    found = 0
    total_scanned = len(matches)

    for match in matches:
        home = match.get("homeTeam", {}).get("name", "Home")
        away = match.get("awayTeam", {}).get("name", "Away")
        league = match.get("leagueName", "League")
        odds_history = match.get("oddsHistory", {}).get("1x2", [])

        is_sharp, prob_shift, reason = is_true_sharp_move(odds_history)

        if is_sharp:
            found += 1
            open_h, open_d, open_a = odds_history[0][0], odds_history[0][1], odds_history[0][2]
            curr_h, curr_d, curr_a = odds_history[-1][0], odds_history[-1][1], odds_history[-1][2]

            msg = (
                f"🎯 SHARP MONEY MOVEMENT DETECTED!\n\n"
                f"🏆 League: {league}\n"
                f"⚽ Match: {home} vs {away}\n"
                f"📈 True Win Prob. Shift: +{prob_shift:.1f}%\n\n"
                f"📊 1X2 Odds Line:\n"
                f"• Home: {open_h} ➔ {curr_h}\n"
                f"• Draw: {open_d} ➔ {curr_d}\n"
                f"• Away: {open_a} ➔ {curr_a}"
            )
            send_telegram_alert(msg)

    target = f"Date [{SCAN_DATE}]" if SCAN_DATE else "Today"
    send_telegram_alert(
        f"✅ Sharp Scan Complete ({target})\n"
        f"Scanned {total_scanned} matches.\n"
        f"Found {found} genuine sharp movement(s)."
    )

if __name__ == "__main__":
    check_odds()
