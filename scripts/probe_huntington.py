import sys
import time

sys.path.insert(0, "scripts")
from playwright.sync_api import sync_playwright
import apartments_com_scraper as S

pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False)
context = browser.new_context()
page = context.new_page()
page.goto("https://enjoyhuntington.com/", timeout=30000, wait_until="domcontentloaded")
time.sleep(15)
txt = S._page_main_text(page)
print("main text:", len(txt), repr(txt[:300]))
if len(txt) < 200:
    time.sleep(8)
    txt2 = page.evaluate("() => document.body.innerText || ''")
    print("body.innerText:", len(txt2), repr(txt2[:300]))
res = S._scrape_property_site(context, "https://enjoyhuntington.com/")
print("pages:", [(p["page"], len(p["text"])) for p in res["pages"]])
print("TOTAL:", len(res["text"]))
page.close()
browser.close()
pw.stop()

if res.get("text"):
    conn = S._get_azure_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE dbo.apartment_deals SET property_site_url=%s, property_site_text=%s WHERE deal_name=%s",
        (res["url"], res["text"][:15000], "Huntington"),
    )
    conn.commit()
    print("azure rows updated:", cur.rowcount)
    conn.close()
