#!/usr/bin/env python3
"""Structural checks for the hand-authored SVG diagrams in docs/images.

Catches three failure modes that are invisible without rendering:

  1. content that overflows the viewBox
  2. arrows that terminate in empty space instead of at a box
  3. text that spills past the bottom of its own panel

Written after a dashed arrow in setup-flow.svg was found pointing 28px short
of its target, which XML validation could not detect.

LIMITATION: this checks geometry, not meaning. An arrow that lands squarely
on the *wrong* box passes every test here - setup-flow.svg shipped with its
step 3 arrow pointing at step 5, skipping step 4, and this script called it
clean. Diagrams still need looking at. Serve them and open in a browser:

    python -m http.server 8765 --directory docs/images

Run: python tests/check_diagrams.py
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2000/svg}"
IMAGES = os.path.join(os.path.dirname(__file__), "..", "docs", "images")
# An arrow tip should land within this many px of the box it points at.
TOLERANCE = 14


def endpoint(d):
    """Final (x, y) of a path using absolute M/H/V/L commands."""
    x = y = 0.0
    for cmd, args in re.findall(r"([MHVL])\s*([-\d.,\s]*)", d):
        nums = [float(v) for v in re.findall(r"-?\d*\.?\d+", args)]
        if not nums:
            continue
        if cmd == "M":
            x, y = nums[0], nums[1]
        elif cmd == "L":
            x, y = nums[-2], nums[-1]
        elif cmd == "H":
            x = nums[-1]
        elif cmd == "V":
            y = nums[-1]
    return x, y


def dist_to_rect(px, py, rect):
    x, y = float(rect.get("x", 0)), float(rect.get("y", 0))
    w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
    dx = max(x - px, 0, px - (x + w))
    dy = max(y - py, 0, py - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def check(path):
    problems = []
    root = ET.parse(path).getroot()
    vb = [float(v) for v in root.get("viewBox").split()]
    width, height = vb[2], vb[3]

    # Marker/arrowhead definitions are not diagram arrows.
    in_defs = {id(p) for d in root.iter(NS + "defs") for p in d.iter(NS + "path")}
    # The full-canvas background rect is not a target box.
    boxes = [r for r in root.iter(NS + "rect")
             if float(r.get("width", 0)) < width * 0.9]

    for rect in root.iter(NS + "rect"):
        x, y = float(rect.get("x", 0)), float(rect.get("y", 0))
        w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
        if x < -0.5 or y < -0.5 or x + w > width + 0.5 or y + h > height + 0.5:
            problems.append("rect outside viewBox at {:g},{:g}".format(x, y))

    for text in root.iter(NS + "text"):
        x, y = float(text.get("x", 0)), float(text.get("y", 0))
        if not (0 <= x <= width and 0 <= y <= height):
            problems.append("text outside viewBox at {:g},{:g}".format(x, y))

    for p in root.iter(NS + "path"):
        if id(p) in in_defs:
            continue
        d = p.get("d", "")
        if not d.startswith("M"):
            continue
        ex, ey = endpoint(d)
        near = min((dist_to_rect(ex, ey, b) for b in boxes), default=1e9)
        if near > TOLERANCE:
            problems.append(
                "arrow ends {:.0f}px from any box (d={})".format(near, d))

    # Text sitting inside a panel must not spill past that panel's bottom
    # edge. A baseline 2px below its box still renders "inside the viewBox",
    # so the viewBox check above cannot see this.
    for text in root.iter(NS + "text"):
        tx, ty = float(text.get("x", 0)), float(text.get("y", 0))
        size = float(text.get("font-size", 12))
        descender = ty + size * 0.25
        # The panel this text belongs to is the innermost box that contains
        # the baseline vertically, allowing one line-height of slack so that
        # text which has just spilled out still resolves to its own panel
        # rather than being silently reassigned to the one outside it.
        panel = None
        for b in boxes:
            bx, by = float(b.get("x", 0)), float(b.get("y", 0))
            bw, bh = float(b.get("width", 0)), float(b.get("height", 0))
            if bx <= tx <= bx + bw and by <= ty <= by + bh + size:
                if panel is None or by > float(panel.get("y", 0)):
                    panel = b
        if panel is None:
            continue
        bottom = float(panel.get("y", 0)) + float(panel.get("height", 0))
        if descender > bottom:
            problems.append(
                "text at y={:g} spills past its panel bottom {:g}: {!r}".format(
                    ty, bottom, (text.text or "")[:34]))
    return problems


def main():
    files = sorted(glob.glob(os.path.join(IMAGES, "*.svg")))
    if not files:
        print("no diagrams found in docs/images")
        return 0
    failed = 0
    for f in files:
        problems = check(f)
        name = os.path.basename(f)
        if problems:
            failed += 1
            print("  FAIL  {}".format(name))
            for p in problems:
                print("          {}".format(p))
        else:
            print("  OK    {}".format(name))
    print("\n{}/{} diagrams clean".format(len(files) - failed, len(files)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
