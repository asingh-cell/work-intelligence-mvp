"""Run this ONCE, on your own computer, to get a Google Calendar refresh
token. It does NOT run on Render — it's a one-time setup helper.

Usage:
    python get_google_token.py

You'll need GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from Google Cloud
Console first (see README.md for the full walkthrough). This script will
print a URL and a short code — open the URL in any browser, enter the code,
approve access, and this script will print the refresh token to paste into
Render's environment variables.
"""
import time
import requests

CLIENT_ID = input("Paste your Google OAuth Client ID: ").strip()
CLIENT_SECRET = input("Paste your Google OAuth Client Secret: ").strip()

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

resp = requests.post(DEVICE_CODE_URL, data={"client_id": CLIENT_ID, "scope": SCOPE})
resp.raise_for_status()
data = resp.json()

print()
print(f"1. Open this URL in any browser: {data['verification_url']}")
print(f"2. Enter this code when asked: {data['user_code']}")
print("3. Approve access, then come back here — this will finish automatically.")
print()

interval = data.get("interval", 5)
device_code = data["device_code"]

while True:
    time.sleep(interval)
    poll = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    result = poll.json()
    if "refresh_token" in result:
        print("Success! Add these to Render's environment variables:")
        print(f"GOOGLE_CLIENT_ID={CLIENT_ID}")
        print(f"GOOGLE_CLIENT_SECRET={CLIENT_SECRET}")
        print(f"GOOGLE_REFRESH_TOKEN={result['refresh_token']}")
        break
    elif result.get("error") == "authorization_pending":
        continue
    else:
        print("Error:", result)
        break
