import hashlib
import re
from io import BytesIO

import httpx
from PIL import Image

base = "http://192.168.15.44"
# Cookie jar session for web UI
c = httpx.Client(timeout=20, follow_redirects=True)

# Get login page
r = c.get(f"{base}/fcgi/do?id=1")
html = r.content.decode("utf-8", "replace")
Path_write = "scripts/_login.html"
open(Path_write, "w", encoding="utf-8").write(html)

# Find encrypt / login related snippets
for pat in ["CreateSession", "Encrypt", "password", "Password", "md5", "MD5", "Login"]:
    idxs = [m.start() for m in re.finditer(pat, html)]
    print(pat, "count", len(idxs))

# Try CreateSession like the UI
for url in [
    f"{base}/fcgi/do?id=8&Operation=CreateSession&DivName=divLogin&UserName=admin&Password=",
    f"{base}/fcgi/do?id=8&Operation=CreateSession&User=admin&Password=",
]:
    resp = c.get(url)
    print("GET", url[len(base) :], resp.status_code, resp.headers.get("set-cookie"), resp.text[:300].replace("\n", " "))

# POST form login
resp = c.post(
    f"{base}/fcgi/do?id=8&Operation=CreateSession&DivName=divLogin",
    data={"UserName": "admin", "Password": "", "user": "admin", "pwd": ""},
)
print("POST create", resp.status_code, resp.cookies, resp.text[:400].replace("\n", " "))

# After session, try user pages with cookies
for i in [16, 5, 17, 18, 20, 21, 22, 30]:
    resp = c.get(f"{base}/fcgi/do?id={i}")
    text = resp.text
    interesting = any(k in text for k in ["Add", "Face", "Directory", "Personal", "UserID", "Register", "file"])
    print(f"authed id={i}", resp.status_code, len(text), "interesting" if interesting else "")

# Try API set with Validity dates and WebRelay like existing users
auth = httpx.Client(timeout=20, auth=("admin", ""))
payload = {
    "target": "user",
    "action": "set",
    "data": {
        "item": [
            {
                "ID": 1634,
                "UserID": "APTEST999",
                "Name": "Teste AcessoPiscinas",
                "WebRelay": "0",
                "ScheduleRelay": "",
                "LiftFloorNum": "9999",
                "Validity": 0,
            }
        ]
    },
}
resp = auth.post(base + "/api/user/set", json=payload)
print("set validity", resp.text[:300])

# Delete test users cleanup later
for uid, id_ in [("APTEST998", None), ("APTEST997", None)]:
    pass

# Try import of user data?
resp = auth.get(base + "/api/user/export")
print("export", resp.text[:400])
