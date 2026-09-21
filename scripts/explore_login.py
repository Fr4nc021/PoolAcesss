from playwright.sync_api import sync_playwright

BASE = "http://192.168.15.44"
USER = "admin"
PWD = os.getenv("DEVICE_PASSWORD", "")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    # redirect to login
    if "fcgi" not in page.url:
        page.goto(f"{BASE}/fcgi/do?id=1", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    page.fill("#username", USER)
    page.fill("#password", PWD)

    # Trigger same JS the UI uses
    result = page.evaluate(
        """([user, pwd]) => new Promise((resolve) => {
            try {
              if (typeof EncryptCookie === 'function') {
                // monkeypatch success path observation
                const oldAjax = $.ajax;
                let done = false;
                $.ajax = function(opts) {
                  const origSuccess = opts.success;
                  opts.success = function(html) {
                    if (origSuccess) origSuccess(html);
                    if (!done) { done = true; resolve({via:'EncryptCookie', html: String(html).slice(0,200)}); }
                  };
                  return oldAjax(opts);
                };
                EncryptCookie(user, pwd, 's2', true);
                setTimeout(() => { if (!done) resolve({via:'timeout'}); }, 8000);
              } else {
                resolve({via:'missing EncryptCookie'});
              }
            } catch (e) {
              resolve({via:'error', err: String(e)});
            }
        })""",
        [USER, PWD],
    )
    print("encrypt result", result)
    page.wait_for_timeout(4000)
    page.screenshot(path="scripts/_ui_login2.png", full_page=True)
    print("url", page.url)
    print("body", page.inner_text("body")[:500].replace("\n", " | "))

    # If still on login, try clicking submit after encrypt already done
    if "Login" in page.inner_text("body") or page.locator("#username").count():
        page.fill("#username", USER)
        page.fill("#password", PWD)
        page.locator("input[type=submit]").first.click()
        page.wait_for_timeout(5000)
        print("after submit body", page.inner_text("body")[:500].replace("\n", " | "))
        page.screenshot(path="scripts/_ui_login3.png", full_page=True)

    browser.close()
