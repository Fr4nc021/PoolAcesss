import tarfile
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from app.intelbras_client import IntelbrasClient
from app.main import create_app

# Cleanup leftovers
client_dev = IntelbrasClient()
for u in client_dev.list_users():
    name = str(u.get("Name", ""))
    uid = str(u.get("UserID", ""))
    if "E2E" in name or "Teste" in name or uid.startswith("AP"):
        try:
            client_dev.delete_user(registration=uid)
            print("cleaned", uid, name)
        except Exception as exc:
            print("clean fail", uid, exc)

# Use a real face already accepted by the device
base = "http://192.168.15.44"
raw = httpx.get(base + "/api/user/export", auth=("admin", ""), timeout=60).content
photo = Path("data/photos/_e2e_real.jpg")
photo.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tar:
    member = next(m for m in tar.getmembers() if m.name.endswith("/2.jpg"))
    data = tar.extractfile(member).read()
    photo.write_bytes(data)
print("photo bytes", photo.stat().st_size)

app = create_app()
http = app.test_client()
response = http.post(
    "/cadastrar",
    data={"name": "Teste E2E AcessoPiscinas", "photo": (BytesIO(photo.read_bytes()), "rosto.jpg")},
    content_type="multipart/form-data",
)
print("cadastrar", response.status_code, response.headers.get("Location"))
html = http.get("/membros").get_data(as_text=True)
print("listed", "Teste E2E" in html)
print("saude", http.get("/saude").get_json())

# cleanup created test user from device + leave DB as evidence or clean
for u in client_dev.list_users():
    if "E2E" in str(u.get("Name", "")):
        client_dev.delete_user(registration=str(u["UserID"]))
        print("removed device user", u["UserID"])
