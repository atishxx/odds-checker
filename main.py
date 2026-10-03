import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})

def check_odds():
    url = "https://api.aiscore.com/api/v1/match/list" 
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        matches = data.get("data", {}).get("list", [])
    except Exception:
        print("Could not fetch data.")
        return

    found = 0
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
    
    print(f"Done. Found {found} matches.")

if __name__ == "__main__":
    check_odds()
  
