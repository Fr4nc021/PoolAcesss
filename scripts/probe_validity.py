import json
import tarfile
from io import BytesIO

import httpx

base = "http://192.168.15.44"
c = httpx.Client(timeout=60, auth=("admin", ""))

r = c.get(base + "/api/user/export")
with tarfile.open(fileobj=BytesIO(r.content), mode="r:gz") as tar:
    xml = tar.extractfile("faceIDs/userdata.xml").read()
    open("scripts/_userdata.xml", "wb").write(xml)
    text = xml.decode("utf-8", "replace")
    print(text[:2500])
    print("---")
    # find Validity-like fields
    for key in ["Validity", "Valid", "Begin", "End", "Expire", "Date", "Schedule", "Frequency"]:
        if key.lower() in text.lower():
            print("has", key)

# cleanup face probes
r = c.get(base + "/api/user/get")
for u in (r.json().get("data") or {}).get("item") or []:
    if str(u.get("UserID", "")).startswith("APFACE"):
        resp = c.post(
            base + "/api/user/del",
            json={"target": "user", "action": "del", "data": {"item": [{"UserID": u["UserID"]}]}},
        )
        print("cleanup", u["UserID"], resp.text[:100])

# Compare a valid face user
r = c.get(base + "/api/user/get")
users = (r.json().get("data") or {}).get("item") or []
u = next(x for x in users if x.get("FaceStatus") == 1)
print("sample valid", json.dumps(u, indent=2)[:600])
