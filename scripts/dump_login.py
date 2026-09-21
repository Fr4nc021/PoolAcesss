from pathlib import Path

import httpx

base = "http://192.168.15.44"
c = httpx.Client(timeout=30, follow_redirects=True)
html = c.get(f"{base}/fcgi/do?id=1").content.decode("utf-8", "replace")
Path("scripts/_login_full.html").write_text(html, encoding="utf-8")
print("len", len(html))
# print sections around Encrypt / CreateSession / password
for key in ["function Encrypt", "CreateSession", "Password", "userName", "UserName", "submit"]:
    idx = html.lower().find(key.lower())
    print("\n===", key, idx, "===")
    if idx >= 0:
        print(html[max(0, idx - 80) : idx + 400])
