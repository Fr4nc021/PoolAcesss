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
    page.wait_for_timeout(2500)

    # Click Moradores
    page.get_by_text("Moradores/Pl. Disc.", exact=False).first.click()
    page.wait_for_timeout(2000)
    page.screenshot(path="scripts/_ui_moradores.png", full_page=True)
    text = page.inner_text("body")
    print("moradores snippet:", text[:800].replace("\n", " | "))
    links = [t.strip() for t in page.locator("a").all_text_contents() if t.strip()]
    print("links", links[:60])

    # Try Usuário / User / Adicionar
    for label in ["Usuário", "Usuario", "User", "Morador", "Adicionar", "Add", "Lista"]:
        loc = page.get_by_text(label, exact=False)
        print(label, "count", loc.count())

    # Click possible submenu
    for label in ["Usuário", "Usuario", "User"]:
        if page.get_by_text(label, exact=False).count():
            page.get_by_text(label, exact=False).first.click()
            page.wait_for_timeout(2000)
            page.screenshot(path=f"scripts/_ui_{label}.png", full_page=True)
            print(label, "url", page.url)
            print(label, "text", page.inner_text("body")[:600].replace("\n", " | "))
            break

    browser.close()
