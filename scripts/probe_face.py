import base64
import json
from io import BytesIO

import httpx
from PIL import Image

base = "http://192.168.15.44"
c = httpx.Client(timeout=30, auth=("admin", ""))

img = Image.new("RGB", (400, 500), color=(180, 160, 140))
buf = BytesIO()
img.save(buf, format="JPEG", quality=85)
jpeg = buf.getvalue()
b64 = base64.b64encode(jpeg).decode()
data_uri = "data:image/jpeg;base64," + b64

# Fetch user 9999
r = c.get(base + "/api/user/get")
users = (r.json().get("data") or {}).get("item") or []
u = next((x for x in users if str(x.get("UserID")) == "APTEST999" or x.get("ID") == 9999), None)
print("user9999", json.dumps(u, indent=2)[:800] if u else None)

# Try set with FaceUrl variants
variants = [
    {"FaceUrl": data_uri},
    {"FaceUrl": b64},
    {"Face": b64},
    {"FaceData": b64},
    {"FaceID": data_uri},
]
for i, extra in enumerate(variants):
    payload = {
        "target": "user",
        "action": "set",
        "data": {
            "item": [
                {
                    "ID": "9999",
                    "UserID": "APTEST999",
                    "Name": "Teste AcessoPiscinas",
                    **extra,
                }
            ]
        },
    }
    resp = c.post(base + "/api/user/set", json=payload)
    print("set", i, list(extra.keys())[0], resp.text[:250])

# multipart upload guesses
files = {"file": ("face.jpg", jpeg, "image/jpeg")}
for path in [
    "/api/user/set",
    "/api/user/face",
    "/api/user/upload",
    "/api/face/upload",
    "/Image/RegisterImg/9999.jpg",
]:
    try:
        resp = c.post(path if path.startswith("http") else base + path, files=files, data={"ID": "9999", "UserID": "APTEST999"})
        print("multipart", path, resp.status_code, resp.text[:200])
    except Exception as e:
        print("multipart", path, e)

# PUT image directly
resp = c.put(base + "/Image/RegisterImg/9999.jpg", content=jpeg, headers={"Content-Type": "image/jpeg"})
print("PUT img", resp.status_code, resp.text[:200])
resp = c.post(base + "/Image/RegisterImg/9999.jpg", content=jpeg, headers={"Content-Type": "image/jpeg"})
print("POST img", resp.status_code, resp.text[:200])
