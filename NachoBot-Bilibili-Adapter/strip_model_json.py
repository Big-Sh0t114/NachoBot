import json
import os

model_path = r"C:\Users\BigSh0t\Nacho-with-u\NachoBot-Bilibili-Adapter\Resources\hiyori_test\hiyori_pro_zh\runtime\hiyori_pro_t11.model3.json"

print(f"Reading {model_path}...")
with open(model_path, "r", encoding="utf-8") as f:
    data = json.load(f)

# Also remove DisplayInfo if present
if "DisplayInfo" in data.get("FileReferences", {}):
    print("Removing DisplayInfo...")
    del data["FileReferences"]["DisplayInfo"]

# Ensure clean slate? No, let's keep previous removals if they were saved.
# But just in case, catch Groups/HitAreas again
if "Groups" in data:
    print("Removing Groups...")
    del data["Groups"]
if "HitAreas" in data:
    print("Removing HitAreas...")
    del data["HitAreas"]

print("Writing modified file...")
with open(model_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4)

print("Done. DisplayInfo removed.")
