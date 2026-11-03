import time
from playwright.sync_api import sync_playwright

HTML = "<html><body><div id=app>loading</div><script>setTimeout(()=>{document.getElementById('app').innerHTML='<ul><li>JS Card One</li><li>JS Card Two</li></ul>'},300)</script></body></html>"
t = time.time()
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.set_content(HTML)
    pg.wait_for_selector("li")
    print("chromium", b.version, "| rendered:", pg.inner_text("#app").replace("\n", " | "))
    b.close()
print("seconds", round(time.time() - t, 1))
