from playwright.sync_api import sync_playwright

BASE = "http://192.168.15.44"
USER = "admin"
PWD = os.getenv("DEVICE_PASSWORD", "")


def login(page):
    page.goto(f"{BASE}/fcgi/do?id=1", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    page.fill("#username", USER)
    page.fill("#password", PWD)
    page.evaluate(
        """([user, pwd]) => new Promise((resolve) => {
            const oldAjax = $.ajax;
            let done = false;
            $.ajax = function(opts) {
              const origSuccess = opts.success;
              opts.success = function(html) {
                if (origSuccess) origSuccess(html);
                if (!done) { done = true; resolve(true); }
              };
              return oldAjax(opts);
            };
            EncryptCookie(user, pwd, 's2', true);
            setTimeout(() => resolve(false), 10000);
        })""",
        [USER, PWD],
    )
    page.wait_for_timeout(1500)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    login(page)

    # Inspect left menu structure
    menu = page.evaluate(
        """() => {
          const out = [];
          document.querySelectorAll('#left a, .left a, a').forEach(a => {
            out.push({text:(a.innerText||'').trim(), href:a.getAttribute('href'), onclick:a.getAttribute('onclick'), id:a.id});
          });
          return out.filter(x => x.text).slice(0, 40);
        }"""
    )
    print("anchors", menu)

    page.locator("a", has_text="Moradores").first.click(timeout=5000)
    page.wait_for_timeout(2500)
    page.screenshot(path="scripts/_ui_moradores3.png", full_page=True)
    print("after click body", page.inner_text("body")[:900].replace("\n", " | "))

    anchors2 = page.evaluate(
        """() => Array.from(document.querySelectorAll('a'))
          .map(a => ({text:(a.innerText||'').trim(), href:a.getAttribute('href'), onclick:a.getAttribute('onclick')}))
          .filter(x => x.text)"""
    )
    print("anchors2", anchors2[:50])
    browser.close()
