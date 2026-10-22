# Site design source

`Main.dc.html` is the landingparty.ai page exactly as exported from its Claude Design canvas.
`assets.json` records the canvas version it came from and maps each `/_blob/` image and video to
its file under `site/`, with the SHA-256 of the canvas original.

[`scripts/build_site.py`](../scripts/build_site.py) turns it into `site/index.html`. It resolves
links to this repository, removes the "design, not yet code" card, and compresses videos. Edit
the design in Claude Design, not `site/index.html`, and re-export:

1. Save the canvas's `project/Main.dc.html` over `design/Main.dc.html`; `git diff design/` is
   the design change.
2. Download any `/_blob/` asset that is new or changed into a folder as `<blob id>.<ext>`.
3. `python scripts/build_site.py --blobs FOLDER --version CANVAS_VERSION`, then commit. The
   `pages` workflow deploys `site/` on merge to `main`.
