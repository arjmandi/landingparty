"""Build site/ from the Claude Design export in design/.

To pick up a design change, save the canvas's project/Main.dc.html over design/Main.dc.html and
put any new or changed /_blob assets, named <blob id>.<ext>, in a folder. Then run:

    python scripts/build_site.py --blobs FOLDER --version CANVAS_VERSION

Without --blobs it only regenerates site/index.html from assets already in site/.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "design"
SITE = ROOT / "site"
SITE_URL = "https://landingparty.ai/"
REPO_URL = "https://github.com/arjmandi/landingparty"
# Built from mkdocs.yml into site/docs/ by the pages workflow.
DOCS_URL = "/docs/"
DESIGN_DOC_URL = f"{REPO_URL}/blob/main/00-landing-party.md"
DESCRIPTION = (
    "Landing Party lands an agent on a territory it did not make and cannot reset — "
    "with a small kit, a dollar budget, a line back to the ship, and no instructions beyond the "
    "mission."
)

# Edits the site makes on top of the design. Each applies only while the design still has it.
REMOVED = [
    # The "design, not yet code" card: the first implementation is now in the repository.
    re.compile(
        r'\n<div class="lp-card lp-px" style="padding: 36px; display: flex; flex-wrap: wrap;[^\n]*'
        r"\n<div[^\n]*\n<p[^\n]*The ship is still on the drawing board\.</p>"
        r"\n<p[^\n]*Landing Party is a design, not yet code\.[^\n]*</p>\n</div>"
        r"\n<a [^\n]*>Read the design doc</a>\n</div>"
    ),
]
REPLACED = [
    (">V0 · DESIGN PHASE</span>", ">V0</span>"),
    ('<a href="#" title="Coming next">Docs</a>', f'<a href="{DOCS_URL}">Docs</a>'),
    (
        (
            '<a href="#" class="lp-btn lp-btn-ghost lp-px" style="min-height: 44px; '
            'padding: 0 18px; font-size: 16px; border: 0">Read the design doc</a>'
        ),
        (
            f'<a href="{REPO_URL}" style="display: inline-flex; align-items: center; gap: 8px">'
            'GitHub <span aria-hidden="true">↗</span></a>\n'
            f'<a href="{DESIGN_DOC_URL}" class="lp-btn lp-btn-ghost lp-px" style="min-height: 44px; '
            'padding: 0 18px; font-size: 16px; border: 0">Read the design doc</a>'
        ),
    ),
    (
        '<a href="#" class="lp-btn lp-btn-primary lp-px">Read the docs</a>',
        f'<a href="{DOCS_URL}" class="lp-btn lp-btn-primary lp-px">Read the docs</a>',
    ),
    (
        "<span>contact: [CONTACT EMAIL]</span>",
        f'<span>source: <a href="{REPO_URL}">github.com/arjmandi/landingparty</a></span>',
    ),
]

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landing Party — a world for agents whose territory is the live internet</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{site}">
<meta name="theme-color" content="#070C22">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<meta property="og:type" content="website">
<meta property="og:url" content="{site}">
<meta property="og:site_name" content="Landing Party">
<meta property="og:title" content="Landing Party — give an agent the internet as a world">
<meta property="og:description" content="{description}">
<meta property="og:image" content="{site}{image}">
<meta property="og:image:width" content="1280">
<meta property="og:image:height" content="720">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{helmet}
</head>
<body>
{body}
</body>
</html>
"""


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_blob(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix != ".mp4":
        shutil.copyfile(source, dest)
        return
    # The canvas keeps ~8 Mbps masters; CRF 25 is visually close (SSIM ~0.98) at ~40% the size.
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", source, "-an", "-c:v", "libx264"]
        + ["-preset", "slow", "-crf", "25", "-pix_fmt", "yuv420p", "-profile:v", "high"]
        + ["-movflags", "+faststart", dest],
        check=True,
    )


def sync_assets(body, manifest, blobs):
    """Point each /_blob URL at its file under site/, importing new or changed blobs."""
    assets = manifest["assets"]
    used = list(dict.fromkeys(re.findall(r"/_blob/([0-9a-f]{32})", body)))
    for blob in used:
        source = next(blobs.glob(f"{blob}.*"), None) if blobs else None
        entry = assets.get(blob)
        if source and (not entry or entry["sha256"] != sha256(source)):
            kind = "video" if source.suffix == ".mp4" else "img"
            path = entry["path"] if entry else f"assets/{kind}/{blob}{source.suffix}"
            import_blob(source, SITE / path)
            entry = assets[blob] = {"path": path, "sha256": sha256(source)}
            print(f"imported {blob} -> site/{path}")
        if not entry or not (SITE / entry["path"]).exists():
            raise SystemExit(f"/_blob/{blob} is new: download it into the --blobs folder")
        body = body.replace(f"/_blob/{blob}", entry["path"])
    for blob in [blob for blob in assets if blob not in used]:
        (SITE / assets.pop(blob)["path"]).unlink(missing_ok=True)
        print(f"removed unused {blob}")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blobs", type=Path, help="folder of downloaded <blob id>.<ext> files")
    parser.add_argument("--version", help="canvas version the design was exported from")
    args = parser.parse_args()

    source = (DESIGN / "Main.dc.html").read_text()
    helmet = re.search(r"<helmet>\n(.*?)\n</helmet>", source, re.DOTALL).group(1)
    body = re.search(r"</helmet>\n(.*?)\n</x-dc>", source, re.DOTALL).group(1)
    if re.search(r"<(sc-for|sc-if|dc-import)\b", body):
        raise SystemExit("design uses canvas components; this static export cannot render them")

    # Template holes take their declared defaults: the page has no editor to change them.
    props = json.loads(re.search(r"data-props='([^']*)'", source).group(1))
    for name, prop in props.items():
        if "default" in prop:
            body = body.replace(f"{{{{{name}}}}}", prop["default"])
    for pattern in REMOVED:
        body = pattern.sub("", body)
    for old, new in REPLACED:
        body = body.replace(old, new)
    for leftover in ("{{", 'href="#"'):
        if leftover in body:
            line = next(line for line in body.splitlines() if leftover in line)
            raise SystemExit(f"unresolved {leftover!r} in design: {line.strip()[:160]}")

    manifest_path = DESIGN / "assets.json"
    manifest = json.loads(manifest_path.read_text())
    body = sync_assets(body, manifest, args.blobs)
    if args.version:
        manifest["version"] = args.version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    image = re.search(r'poster="([^"]+)"', body).group(1)
    page = PAGE.format(
        description=DESCRIPTION, site=SITE_URL, image=image, helmet=helmet, body=body
    )
    (SITE / "index.html").write_text(page)
    print(f"wrote site/index.html from canvas version {manifest['version']}")


if __name__ == "__main__":
    main()
