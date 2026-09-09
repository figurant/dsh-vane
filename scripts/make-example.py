"""Create small neutral operational inputs; this generates data, not a research report."""
from pathlib import Path
import pymupdf
from PIL import Image, ImageDraw

root = Path("examples/materials")
root.mkdir(parents=True, exist_ok=True)
(root / "history.csv").write_bytes(b"item,value,category\nA,10,\nB,20,\n")
(root / "supplement.csv").write_bytes(b"item,category\nA,retail\nB,wholesale\n")
(root / "follow-up.csv").write_bytes(b"item,value,category\nA,18,retail\nB,24,wholesale\nC,5,retail\n")
with pymupdf.open() as doc:
    page = doc.new_page()
    page.insert_text((72, 72), "Operational report\nA,15\nB,25\nValues are reported observations. Reporting dates and units are not provided.")
    doc.save(root / "new-report.pdf")
image = Image.new("RGB", (900, 350), "white")
ImageDraw.Draw(image).text((40, 40), "Operational report\nA,15\nB,25\nUnits and reporting dates not provided.", fill="black", font_size=36)
image.save(root / "new-report.png")
print(root)
