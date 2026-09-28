# tools/crop_battle_menu_templates.py  (one-off; committed for reproducibility)
import cv2
from pathlib import Path
F = Path("tests/fixtures/battle_menu")
OUT = Path("templates/battle_menu"); OUT.mkdir(parents=True, exist_ok=True)
CROPS = {
    "hamburger": ("collapsed_badged", (990, 40, 1050, 96)),
    "close":     ("open_badged",      (990, 40, 1050, 96)),
    "exit_battle": ("open_badged",    (880, 560, 1050, 612)),
    "cart":      ("open_badged",      (884, 40, 944, 96)),
    "missions":  ("open_badged",      (884, 250, 944, 306)),
    "cards":     ("open_badged",      (884, 356, 944, 412)),
    "labs":      ("open_badged",      (990, 356, 1050, 412)),
    "event":     ("open_badged",      (994, 470, 1052, 520)),
}
for name, (frame, (x0, y0, x1, y1)) in CROPS.items():
    img = cv2.imread(str(F / f"{frame}.png"))
    cv2.imwrite(str(OUT / f"{name}.png"), img[y0:y1, x0:x1])
