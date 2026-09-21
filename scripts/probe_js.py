import httpx
import re

base = "http://192.168.15.44"
c = httpx.Client(timeout=15, follow_redirects=True)

# Save login page HTML for inspection
r = c.get(f"{base}/fcgi/do?id=1")
open("scripts/_ui_id1.html", "wb").write(r.content)
text = r.content.decode("utf-8", "replace")

# Extract script srcs and inline action= strings
srcs = re.findall(r'src=["\']([^"\']+)["\']', text, flags=re.I)
print("srcs", srcs)
actions = sorted(set(re.findall(r"action=([A-Za-z0-9_]+)", text)))
print("actions in html", actions[:50], "count", len(actions))

# Download referenced JS files and search actions
js_hits = {}
for src in srcs:
    url = src if src.startswith("http") else base + "/" + src.lstrip("./")
    # UI refs like ../js/... resolve from /fcgi/ so go to /js/
    if src.startswith("../"):
        url = base + "/" + src[3:]
    try:
        jr = c.get(url)
        print("js", url, jr.status_code, len(jr.content))
        if jr.status_code == 200:
            open("scripts/_" + url.split("/")[-1], "wb").write(jr.content)
            found = sorted(set(re.findall(r"action=([A-Za-z0-9_]+)", jr.text)))
            if found:
                js_hits[url] = found
            # also look for fcgi/do
            if "fcgi/do" in jr.text or "addUser" in jr.text or "Face" in jr.text:
                print("  interesting keywords present")
                for kw in ["addUser", "AddUser", "face", "Face", "Personal", "user", "User"]:
                    if kw.lower() in jr.text.lower():
                        print("   has", kw)
    except Exception as e:
        print("js fail", url, e)

for url, found in js_hits.items():
    print(url, found[:80])
