import re
from pathlib import Path

import httpx

base = "http://192.168.15.44"
c = httpx.Client(timeout=20, auth=("admin", ""), follow_redirects=True)

# Crawl likely static dirs for face-related scripts
candidates = []
for folder in ["js", "css", "html", "page", "pages", "cgi", "fcgi"]:
    for name in [
        "user.js",
        "users.js",
        "personal.js",
        "directory.js",
        "face.js",
        "common.js",
        "main.js",
        "web.js",
        "config.js",
        "func.js",
        "ajax.js",
        "upload.js",
        "phone.js",
        "index.js",
    ]:
        candidates.append(f"/{folder}/{name}")

# Also from UI pages with different ids
for i in range(0, 40):
    r = c.get(f"{base}/fcgi/do?id={i}")
    text = r.content.decode("utf-8", "replace")
    if "Face" in text or "face" in text or "Register" in text or "User" in text:
        print(f"id={i} len={len(text)} title=", re.search(r"<title>(.*?)</title>", text, re.I))
        for m in re.findall(r"src=['\"]([^'\"]+)['\"]", text, re.I):
            print("  src", m)
        for m in set(re.findall(r"fcgi/do\?[^'\"\s]+|/api/[A-Za-z0-9_./?-]+|RegisterImg|Upload|multipart|FormData|action=[A-Za-z0-9_]+", text)):
            print("  hit", m[:120])

print("--- probing js list via common names ---")
for path in candidates:
    r = c.get(base + path)
    if r.status_code == 200 and len(r.content) > 50 and b"404" not in r.content[:50]:
        print("FOUND", path, len(r.content))
        text = r.text
        if any(k.lower() in text.lower() for k in ["face", "register", "upload", "userid", "FormData"]):
            print("  relevant")
            # extract upload URLs
            for m in set(re.findall(r"['\"]([^'\"]*(?:face|upload|Register|user)[^'\"]*)['\"]", text, re.I)):
                if len(m) < 120:
                    print("   ", m)

# firmware upload pattern might mirror face upload
r = c.get(base + "/api/firmware/upload")
print("firmware upload get", r.status_code, r.text[:200])
