import os
import requests
import time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        except Exception as e:
            print(f"Telegram send error: {e}")

def check_odds():
    session = requests.Session()
    
    # Browser spoofing headers to bypass Cloudflare blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.aiscore.com/",
        "Origin": "https://www.aiscore.com",
        "Sec-Ch-Ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site"
    }
    
    url = "https://api.aiscore.com/api/v1/match/list"
    matches = []

    try:
        # Establish session cookies from home page first
        session.get("https://www.aiscore.com/", headers=headers, timeout=10)
        time.sleep(1)
        
        # Fetch the live odds payload
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

        if len(odds_history) >= 4:
            opening_home, opening_draw = odds_history[0][0], odds_history[0][1]
            current_home, current_draw = odds_history[-1][0], odds_history[-1][1]

            # Rule 1: Home drop >= 2.0
            # Rule 2: Draw did not drop (current >= opening)
            # Rule 3: 3 consecutive drops
            home_line = [entry[0] for entry in odds_history[:4]]
            
            if (opening_home - current_home >= 2.0) and (current_draw >= opening_draw):
                if all(home_line[i] > home_line[i+1] for i in range(len(home_line)-1)):
                    found += 1
                    msg = (
                        f"🚨 ODDS DROP ALERT!\n\n"
                        f"League: {league}\n"
                        f"Match: {home} vs {away}\n"
                        f"Home Drop: {opening_home} ➔ {current_home}\n"
                        f"Draw Line: {opening_draw} ➔ {current_draw}"
                    )
                    send_telegram_alert(msg)

    # Confirmation report
    send_telegram_alert(f"✅ Daily Scan Complete!\nScanned {total_scanned} matches today.\n{found} matches met your criteria.")

if __name__ == "__main__":
    check_odds()
    
