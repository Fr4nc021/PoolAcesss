"""Explora a UI web do XPE para mapear login e cadastro de usuário com foto."""
from playwright.sync_api import sync_playwright

BASE = "http://192.168.15.44"
USER = "admin"
PWD = os.getenv("DEVICE_PASSWORD", "")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(f"{BASE}/fcgi/do?id=1", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    page.screenshot(path="scripts/_ui_login.png", full_page=True)
    print("title", page.title())
    print("url", page.url)
    # inputs
    for sel in ["#username", "#password", "input[name=username]", "input[type=password]", "#submit", "input[type=submit]", "button"]:
        loc = page.locator(sel)
        print(sel, "count", loc.count())
    # try fill login
    if page.locator("#username").count():
        page.fill("#username", USER)
        page.fill("#password", PWD)
        # click login button - try common
        for sel in ["#loginButton", "input[type=submit]", "button:has-text('Login')", "button:has-text('Entrar')", "#submit_form input", "a:has-text('Login')"]:
            if page.locator(sel).count():
                print("clicking", sel)
                page.locator(sel).first.click()
                break
        else:
            # maybe onclick on image/button
            page.evaluate(
                """() => {
                if (typeof OnSubmit === 'function') OnSubmit();
                else if (typeof onLogin === 'function') onLogin();
                else if (typeof Login === 'function') Login();
                else {
                  const btns = document.querySelectorAll('input,button,a,img');
                  for (const b of btns) {
                    const t = (b.value||b.id||b.innerText||'').toLowerCase();
                    if (t.includes('login') || t.includes('entrar') || t.includes('ok')) { b.click(); break; }
                  }
                }
            }"""
            )
        page.wait_for_timeout(3000)
        page.screenshot(path="scripts/_ui_after_login.png", full_page=True)
        print("after url", page.url)
        print("after title", page.title())
        # dump visible text snippet
        text = page.inner_text("body")
        print("body snippet", text[:500].replace("\n", " | "))
        # links/menus
        links = page.locator("a").all_text_contents()
        print("links", [t.strip() for t in links if t.strip()][:40])
    browser.close()
