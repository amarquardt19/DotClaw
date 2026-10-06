#!/usr/bin/env python3
"""
Keep Uber page alive by periodically checking and reopening if needed
"""
import time
import requests
import subprocess
import os

CDP_PORT = os.getenv("EMMYCAR_BROWSER_CDP_PORT", "18801")
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_PROFILE = "/Users/adammarq/chrome-emmycar"

def check_uber_page():
    """Check if Uber page is open in Chrome"""
    try:
        response = requests.get(f"{CDP_URL}/json", timeout=5)
        pages = response.json()
        
        # Check if any page has uber.com
        for page in pages:
            url = page.get("url", "")
            if "uber.com" in url:
                print(f"✅ Uber page found: {url}")
                return True
        
        print("❌ No Uber page found")
        return False
    except Exception as e:
        print(f"❌ Error checking pages: {e}")
        return False

def open_uber_page():
    """Open Uber in Chrome with CDP"""
    print("🚗 Opening Uber page...")
    cmd = [
        "open", "-a", "Google Chrome",
        "--args",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CHROME_PROFILE}",
        "https://www.uber.com"
    ]
    subprocess.run(cmd)
    time.sleep(5)  # Wait for Chrome to start

def main():
    print("🔄 Starting Uber page keeper...")
    print(f"📍 CDP URL: {CDP_URL}")
    print(f"📂 Chrome profile: {CHROME_PROFILE}")
    print("⏰ Checking every 2 minutes...\n")
    
    while True:
        if not check_uber_page():
            print("🔧 Reopening Uber page...")
            open_uber_page()
        
        # Check every 2 minutes
        time.sleep(120)

if __name__ == "__main__":
    main()
