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

def debug_scan():
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

    try:
        session.get("https://www.aiscore.com/", headers=headers, timeout=10)
        time.sleep(1)
        response = session.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            send_telegram_alert(f"⚠️ API Error: Server returned status code {response.status_code}")
            return

        data = response.json()
        matches = data.get("data", {}).get("list", [])
        
    except Exception as e:
        send_telegram_alert(f"⚠️ Diagnostic Failed: {e}")
        return

    total_matches = len(matches)
    matches_with_odds_key = 0
    matches_with_odds_history = 0
    sample_keys = []

    if total_matches > 0:
        sample_keys = list(matches[0].keys())

    for match in matches:
        # Check all possible locations where odds might be stored
        odds_hist = match.get("oddsHistory", {}).get("1x2", [])
        odds_raw = match.get("odds", {}) or match.get("rates", {})
        
        if odds_hist or odds_raw:
            matches_with_odds_key += 1
        if len(odds_hist) >= 3:
            matches_with_odds_history += 1

    # Send diagnostic summary to Telegram
    debug_msg = (
        f"🔍 DIAGNOSTIC REPORT\n\n"
        f"• Total Matches Fetched: {total_matches}\n"
        f"• Matches with ANY Odds Data: {matches_with_odds_key}\n"
        f"• Matches with History >= 3 lines: {matches_with_odds_history}\n\n"
        f"📋 Sample Match Keys:\n{', '.join(sample_keys[:10])}"
    )
    send_telegram_alert(debug_msg)

if __name__ == "__main__":
    debug_scan()
    
