"""Experiment: add user+face via export/modify/import UserData.tgz"""
from __future__ import annotations

import tarfile
import time
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx

BASE = "http://192.168.15.44"
AUTH = ("admin", "")
c = httpx.Client(timeout=120, auth=AUTH)

# Export
raw = c.get(f"{BASE}/api/user/export").content
print("exported", len(raw))

# Extract to memory, find max IDs
with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tar:
    xml_bytes = tar.extractfile("faceIDs/userdata.xml").read()
    face_jpg = tar.extractfile("faceIDs/RegFaceMeun/2.jpg").read()
    names = tar.getnames()

root = ET.fromstring(xml_bytes)
keys = list(root.findall("Key"))
print("keys", len(keys))
max_id = max(int(k.get("ID")) for k in keys)
max_face = max(int(k.get("FaceID") or 0) for k in keys)
print("max id/face", max_id, max_face)

new_id = max_id + 1
new_face = max_face + 1
new_userid = "APIMP001"
new_name = "Probe Import Face"

# Append Key
attrs = {
    "ID": str(new_id),
    "FaceID": str(new_face),
    "UserID": new_userid,
    "Name": new_name,
    "Card": "",
    "Pin": "",
    "Relay": "1",
    "WebRelay": "0",
    "Floor": "9999",
    "Validity": "0",
    "Frequency": "0",
    "Mon": "0",
    "Tue": "0",
    "Wed": "0",
    "Thur": "0",
    "Fri": "0",
    "Sat": "0",
    "Sun": "0",
    "TimeStart": "",
    "TimeEnd": "",
}
ET.SubElement(root, "Key", attrs)
new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
print("new xml size", len(new_xml))

# Rebuild tgz: copy all members, replace xml, add face jpg
out = BytesIO()
with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as src, tarfile.open(
    fileobj=out, mode="w:gz"
) as dst:
    for m in src.getmembers():
        if m.name == "faceIDs/userdata.xml":
            info = tarfile.TarInfo(name=m.name)
            info.size = len(new_xml)
            info.mtime = int(time.time())
            dst.addfile(info, BytesIO(new_xml))
        else:
            f = src.extractfile(m)
            dst.addfile(m, f)
    # add face image
    face_name = f"faceIDs/RegFaceMeun/{new_face}.jpg"
    info = tarfile.TarInfo(name=face_name)
    info.size = len(face_jpg)
    info.mtime = int(time.time())
    dst.addfile(info, BytesIO(face_jpg))

payload = out.getvalue()
Path("scripts/_import_test.tgz").write_bytes(payload)
print("import package", len(payload))

# Import — try multipart form like firmware upload
files = {"file": ("UserData.tgz", payload, "application/octet-stream")}
for field in ["file", "File", "upload", "UploadFile", "userdata"]:
    files = {field: ("UserData.tgz", payload, "application/gzip")}
    r = c.post(
        f"{BASE}/api/user/import",
        files=files,
        data={"target": "user", "action": "import"},
    )
    print("import field", field, r.status_code, r.text[:200])

# Also POST raw body
r = c.post(
    f"{BASE}/api/user/import",
    content=payload,
    headers={"Content-Type": "application/octet-stream"},
)
print("import raw", r.status_code, r.text[:200])

r = c.post(f"{BASE}/api/user/import", json={"target": "user", "action": "import"})
print("import json only", r.status_code, r.text[:200])

time.sleep(2)
users = c.get(f"{BASE}/api/user/get").json()["data"]["item"]
u = next((x for x in users if x.get("UserID") == new_userid), None)
print("found", u)
if u:
    c.post(
        f"{BASE}/api/user/del",
        json={"target": "user", "action": "del", "data": {"item": [{"UserID": new_userid}]}},
    )
