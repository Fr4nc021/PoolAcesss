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
    page.screenshot(path="scripts/_ui_home2.png", full_page=True)

    # Click menu via JS text search
    clicked = page.evaluate(
        """() => {
          const nodes = Array.from(document.querySelectorAll('a, td, li, span, div'));
          const el = nodes.find(n => (n.innerText||'').includes('Moradores'));
          if (!el) return false;
          el.click();
          return (el.innerText||'').slice(0,80);
        }"""
    )
    print("clicked menu", clicked)
    page.wait_for_timeout(2000)
    page.screenshot(path="scripts/_ui_moradores2.png", full_page=True)
    print("body", page.inner_text("body")[:1000].replace("\n", " | "))

    # list submenu texts
    texts = page.evaluate(
        """() => Array.from(document.querySelectorAll('a, td, li, span'))
          .map(n => (n.innerText||'').trim())
          .filter(t => t && t.length < 40)
          .filter((t,i,a) => a.indexOf(t)===i)
          .slice(0,100)"""
    )
    print("texts", texts)

    browser.close()
