from pathlib import Path

from PIL import Image

from app.intelbras_client import IntelbrasClient

# Generate a larger synthetic face-like image (oval + eyes) — better than flat color
path = Path("data/photos/_synth.jpg")
path.parent.mkdir(parents=True, exist_ok=True)
img = Image.new("RGB", (480, 640), (210, 190, 170))
px = img.load()
# crude head oval
for y in range(640):
    for x in range(480):
        cx, cy = 240, 280
        if ((x - cx) ** 2) / (140**2) + ((y - cy) ** 2) / (180**2) <= 1:
            px[x, y] = (190, 150, 120)
        if ((x - 190) ** 2) / (18**2) + ((y - 250) ** 2) / (12**2) <= 1:
            px[x, y] = (40, 30, 20)
        if ((x - 290) ** 2) / (18**2) + ((y - 250) ** 2) / (12**2) <= 1:
            px[x, y] = (40, 30, 20)
img.save(path, format="JPEG", quality=92)

client = IntelbrasClient()
# cleanup AP*
for u in client.list_users():
    if str(u.get("UserID", "")).startswith("AP") or "E2E" in str(u.get("Name", "")):
        client.delete_user(registration=str(u["UserID"]))
        print("cleaned", u["UserID"])

created = client.register_member("Teste E2E AcessoPiscinas", path)
print("OK", created)
client.delete_user(registration=created.registration)
print("removed", created.registration)
