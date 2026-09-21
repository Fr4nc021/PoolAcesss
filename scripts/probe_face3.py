import base64
import io
import json
import tarfile
from io import BytesIO

import httpx
from PIL import Image

base = "http://192.168.15.44"
c = httpx.Client(timeout=60, auth=("admin", ""))

# Export userdata and inspect
r = c.get(base + "/api/user/export")
print("export headers", r.headers.get("content-type"), len(r.content))
open("scripts/_userdata.tgz", "wb").write(r.content)
try:
    with tarfile.open(fileobj=BytesIO(r.content), mode="r:gz") as tar:
        print("members", tar.getnames()[:50])
        for m in tar.getmembers()[:20]:
            print(" ", m.name, m.size)
            if m.isfile() and m.size < 200000:
                f = tar.extractfile(m)
                data = f.read() if f else b""
                if m.name.endswith(".csv") or m.name.endswith(".json") or m.name.endswith(".txt") or m.name.endswith(".xml"):
                    print(data[:500].decode("utf-8", "replace"))
except Exception as e:
    print("tar err", e)
    # maybe raw gzip of single file
    import gzip

    try:
        print(gzip.decompress(r.content)[:500])
    except Exception as e2:
        print("gzip err", e2)

# Try add with face base64 inline
img = Image.new("RGB", (400, 500), (190, 160, 140))
buf = BytesIO()
img.save(buf, "JPEG", quality=90)
jpeg = buf.getvalue()
b64 = base64.b64encode(jpeg).decode()

payload = {
    "target": "user",
    "action": "add",
    "data": {
        "item": [
            {
                "Name": "Face Inline",
                "UserID": "APFACE002",
                "WebRelay": "0",
                "LiftFloorNum": "9999",
                "FaceUrl": b64,
            }
        ]
    },
}
resp = c.post(base + "/api/user/add", json=payload)
print("add b64", resp.text[:300])

payload["data"]["item"][0]["UserID"] = "APFACE003"
payload["data"]["item"][0]["FaceUrl"] = "data:image/jpeg;base64," + b64
resp = c.post(base + "/api/user/add", json=payload)
print("add datauri", resp.text[:300])

r = c.get(base + "/api/user/get")
users = (r.json().get("data") or {}).get("item") or []
for u in users:
    if str(u.get("UserID", "")).startswith("APFACE"):
        print(json.dumps(u)[:400])
        c.post(
            base + "/api/user/del",
            json={"target": "user", "action": "del", "data": {"item": [{"UserID": u["UserID"]}]}},
        )
