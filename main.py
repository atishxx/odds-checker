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

def eval_classic_drop(odds_history):
    if len(odds_history) < 4:
        return None
    
    opening_home, opening_draw = odds_history[0][0], odds_history[0][1]
    current_home, current_draw = odds_history[-1][0], odds_history[-1][1]
    home_line = [entry[0] for entry in odds_history[:4]]

    drop_amount = opening_home - current_home

    if (current_draw >= opening_draw) and all(home_line[i] > home_line[i+1] for i in range(len(home_line)-1)):
        if drop_amount >= 2.0:
            return "MATCH"
        elif 1.2 <= drop_amount < 2.0:
            return "NEAR_MISS"
    return None

def eval_sharp_move(odds_history):
    if len(odds_history) < 3:
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

    # Draw hedge check
    fair_open_prob_d = (1.0 / open_d) / open_vig
    fair_curr_prob_d = (1.0 / curr_d) / curr_vig
    if (fair_curr_prob_d - fair_open_prob_d) > 0.03:
        return None, prob_shift

    # Trajectory check
    home_history = [entry[0] for entry in odds_history]
    drops = sum(1 for i in range(len(home_history) - 1) if home_history[i] > home_history[i + 1])
    if drops < 2:
        return None, prob_shift

    if prob_shift >= 8.0:
        return "MATCH", prob_shift
    elif 5.0 <= prob_shift < 8.0:
        return "NEAR_MISS", prob_shift

    return None, prob_shift

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

    found_sharp = 0
    found_classic = 0
    found_near_misses = 0
    total_scanned = len(matches)

    for match in matches:
        home = match.get("homeTeam", {}).get("name", "Home")
        away = match.get("awayTeam", {}).get("name", "Away")
        league = match.get("leagueName", "League")
        odds_history = match.get("oddsHistory", {}).get("1x2", [])

        classic_res = eval_classic_drop(odds_history)
        sharp_res, prob_shift = eval_sharp_move(odds_history)

        is_sharp_match = (sharp_res == "MATCH")
        is_classic_match = (classic_res == "MATCH")
        is_near_miss = (sharp_res == "NEAR_MISS" or classic_res == "NEAR_MISS") and not (is_sharp_match or is_classic_match)

        if is_sharp_match or is_classic_match or is_near_miss:
            open_h, open_d, open_a = odds_history[0][0], odds_history[0][1], odds_history[0][2]
            curr_h, curr_d, curr_a = odds_history[-1][0], odds_history[-1][1], odds_history[-1][2]

            alert_type = []
            
            if is_sharp_match:
                found_sharp += 1
                alert_type.append(f"🎯 SHARP MOVE (+{prob_shift:.1f}% Shift)")
            if is_classic_match:
                found_classic += 1
                alert_type.append(f"📉 CLASSIC DROP (Drop >= 2.0)")

            if is_near_miss:
                found_near_misses += 1
                near_details = []
                if sharp_res == "NEAR_MISS":
                    near_details.append(f"Sharp Prob Shift +{prob_shift:.1f}% [Target +8.0%]")
                if classic_res == "NEAR_MISS":
                    near_details.append(f"Home Drop {open_h - curr_h:.2f} [Target >= 2.0]")
                alert_type.append(f"👀 NEAR MISS ({', '.join(near_details)})")

            header_icon = "🚨 MATCH ALERT" if (is_sharp_match or is_classic_match) else "👀 WATCHLIST / NEAR MISS"

            msg = (
                f"{header_icon}\n"
                f"Trigger: {' | '.join(alert_type)}\n\n"
                f"🏆 League: {league}\n"
                f"⚽ Match: {home} vs {away}\n\n"
                f"📊 1X2 Odds Line:\n"
                f"• Home: {open_h} ➔ {curr_h}\n"
                f"• Draw: {open_d} ➔ {curr_d}\n"
                f"• Away: {open_a} ➔ {curr_a}"
            )
            send_telegram_alert(msg)

    target = f"Date [{SCAN_DATE}]" if SCAN_DATE else "Today"
    send_telegram_alert(
        f"✅ Daily Scan Complete ({target})\n"
        f"Scanned {total_scanned} matches.\n"
        f"🎯 Full Sharp Matches: {found_sharp}\n"
        f"📉 Full Classic Drops: {found_classic}\n"
        f"👀 Watchlist / Near Misses: {found_near_misses}"
    )

if __name__ == "__main__":
    check_odds()
