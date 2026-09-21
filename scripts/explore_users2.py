from playwright.sync_api import sync_playwright

BASE = "http://192.168.15.44"
USER = "admin"
PWD = os.getenv("DEVICE_PASSWORD", "")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(f"{BASE}/fcgi/do?id=1", wait_until="domcontentloaded", timeout=60000)
    page.fill("#username", USER)
    page.fill("#password", PWD)
    page.locator("input[type=submit]").first.click()
    page.wait_for_timeout(3000)
    page.screenshot(path="scripts/_ui_home.png", full_page=True)

    # Dump all clickable menu items with href/onclick
    items = page.evaluate(
        """() => Array.from(document.querySelectorAll('a, td, li, span, div'))
        .map(el => ({tag: el.tagName, id: el.id, text: (el.innerText||'').trim().slice(0,80), href: el.getAttribute('href'), onclick: el.getAttribute('onclick')}))
        .filter(x => x.text && (x.text.includes('Morador') || x.text.includes('User') || x.text.includes('Usu') || x.text.includes('Acesso') || x.text.includes('Disc') || x.href || x.onclick))
        .slice(0, 80)"""
    )
    for it in items:
        print(it)

    # Try navigate by known fcgi ids around directory
    for i in [16, 5, 17, 18, 19, 20, 21, 22, 23, 24, 25, 30, 31, 32]:
        page.goto(f"{BASE}/fcgi/do?id={i}", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        body = page.inner_text("body")[:200].replace("\n", " | ")
        if any(k in body for k in ["Usuário", "Usuario", "User", "Adicionar", "Face", "Nome", "Morador"]):
            print(f"\n=== id={i} ===")
            print(body)
            page.screenshot(path=f"scripts/_ui_id{i}.png", full_page=True)

    browser.close()
