import json
from io import BytesIO

import httpx
from PIL import Image

base = "http://192.168.15.44"
c = httpx.Client(timeout=30, auth=("admin", ""))

r = c.get(base + "/api/user/get")
users = (r.json().get("data") or {}).get("item") or []
for u in users:
    if str(u.get("UserID", "")).startswith("APTEST") or str(u.get("UserID", "")).startswith("APFACE"):
        payload = {
            "target": "user",
            "action": "del",
            "data": {"item": [{"ID": str(u["ID"])}]},
        }
        resp = c.post(base + "/api/user/del", json=payload)
        print("del", u["UserID"], u["ID"], resp.text[:200])

img = Image.new("RGB", (400, 500), (200, 170, 150))
buf = BytesIO()
img.save(buf, "JPEG", quality=90)
jpeg = buf.getvalue()

payload = {
    "target": "user",
    "action": "add",
    "data": {
        "item": [
            {
                "Name": "Face Probe",
                "UserID": "APFACE001",
                "WebRelay": "0",
                "LiftFloorNum": "9999",
            }
        ]
    },
}
resp = c.post(base + "/api/user/add", json=payload)
print("add", resp.text)
item = ((resp.json().get("data") or {}).get("item") or [{}])[0]
uid = str(item.get("ID"))
print("new id", uid)

field_names = ["face", "Face", "file", "File", "upload", "image", "Image", "FaceUrl", "picture", "pic"]
for field in field_names:
    files = {field: ("face.jpg", jpeg, "image/jpeg")}
    data_form = {"ID": uid, "UserID": "APFACE001", "Name": "Face Probe"}
    for path in ["/api/user/set", "/api/user/edit", "/api/user/add"]:
        resp = c.post(
            base + path,
            data={**data_form, "target": "user", "action": "set"},
            files=files,
        )
        text = resp.text
        interesting = (
            "unsupport" not in text
            and "error param" not in text
            and "No hanlders" not in text
        ) or ("retcode\": 0" in text) or ('"retcode":0' in text) or ("OK" in text)
        if interesting:
            print("HIT", path, field, text[:250])

# Also try FaceUrl pointing to existing face image on device
payload = {
    "target": "user",
    "action": "set",
    "data": {
        "item": [
            {
                "ID": uid,
                "UserID": "APFACE001",
                "Name": "Face Probe",
                "FaceUrl": "https://192.168.15.44/Image/RegisterImg/2.jpg",
                "WebRelay": "0",
            }
        ]
    },
}
resp = c.post(base + "/api/user/set", json=payload)
print("set FaceUrl copy", resp.text[:250])

r = c.get(base + "/api/user/get")
users = (r.json().get("data") or {}).get("item") or []
u = next((x for x in users if x.get("UserID") == "APFACE001"), None)
print("after", json.dumps(u, indent=2)[:500] if u else None)
