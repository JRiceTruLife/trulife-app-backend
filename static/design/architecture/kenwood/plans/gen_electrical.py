"""A5.1 Electrical Plan - Kenwood, Prechtel real-CAD style.

Solid-black wall footprint (same as A2.1) with faint room labels + real
electrical symbols (outlets, switches, fans, smoke detectors, quad outlets)
connected by curved DASHED wiring lines.  Standard callouts:
"220V FOR RANGE", "FAN W/LITE", "SMOKE DET.", "QUAD", etc.
"""

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Circle, FancyArrowPatch
from matplotlib.path import Path
from matplotlib.patches import PathPatch
import numpy as np
from style_common import (new_sheet, solid_wall_polygon, interior_wall,
                          thin_line, plan_title)


fig, ax = new_sheet(figsize=(14, 10))


# =============================================================================
# Wall footprint (same envelope as A2.1)
# =============================================================================

envelope = [
    (12, 18), (55, 18), (55, 20), (82, 20),
    (82, 46), (55, 46), (55, 50), (12, 50)
]
solid_wall_polygon(ax, envelope, thickness=0.55, inward=True)

# Interior partitions (same as A2.1)
interior_wall(ax, 12, 30, 30, 30)         # primary bed / bath ceiling
interior_wall(ax, 12, 38, 30, 38)         # bath / den
interior_wall(ax, 22, 30, 22, 38)         # bath/wic split
interior_wall(ax, 30, 18, 30, 50)         # spine vertical
interior_wall(ax, 30, 25, 55, 25)         # foyer row top
interior_wall(ax, 48, 25, 48, 50)         # kitchen/pantry
interior_wall(ax, 48, 42, 55, 42)         # pantry top
interior_wall(ax, 30, 42, 48, 42)         # kitchen/great split
interior_wall(ax, 42, 18, 42, 25)         # foyer/powder
interior_wall(ax, 48, 18, 48, 25)         # powder/mud


# =============================================================================
# Room labels (small, faint)
# =============================================================================

def label(x, y, name, size=4.4):
    ax.text(x, y, name, ha='center', va='center',
            fontsize=size, color='#000',
            bbox=dict(boxstyle='square,pad=0.15', fc='#fff', ec='none'))

label(21, 24, 'PRIMARY BED RM', size=4.5)
label(17, 34, 'BTH 1')
label(26, 34, 'W.I.C.')
label(21, 44, 'DEN/ STUDY')
label(39, 34, 'GREAT RM.', size=4.7)
label(39, 46, 'KITCHEN')
label(51.5, 46, 'PANTRY', size=3.6)
label(36, 21.5, 'FOYER', size=3.8)
label(45, 21.5, 'PWDR', size=3.5)
label(51.5, 21.5, 'MUD', size=3.5)
label(68, 33, '3-CAR GARAGE', size=4.5)
# Stair area
label(45, 46, 'STAIR', size=3.5)


# =============================================================================
# Electrical symbols
# =============================================================================

def outlet(ax, cx, cy):
    """Standard duplex outlet - short bar with two prongs."""
    ax.plot([cx - 0.35, cx + 0.35], [cy, cy], color='#000', linewidth=0.55)
    ax.plot([cx, cx], [cy, cy + 0.55], color='#000', linewidth=0.55)


def outlet_h(ax, cx, cy):
    """Horizontal outlet along wall."""
    ax.plot([cx, cx], [cy - 0.35, cy + 0.35], color='#000', linewidth=0.55)
    ax.plot([cx, cx + 0.55], [cy, cy], color='#000', linewidth=0.55)


def quad_outlet(ax, cx, cy):
    outlet(ax, cx, cy)
    ax.text(cx + 0.9, cy + 0.3, 'QUAD', ha='left', va='center',
            fontsize=2.8, color='#000')


def switch(ax, cx, cy):
    """S in a small circle."""
    ax.add_patch(Circle((cx, cy), 0.5, fill=False, edgecolor='#000',
                        linewidth=0.4))
    ax.text(cx, cy, 'S', ha='center', va='center', fontsize=3.3,
            color='#000')


def ceiling_fan(ax, cx, cy, label='FAN W/LITE'):
    """Fan symbol - X with a small circle in center + label."""
    r = 0.9
    for a in (0, 45, 90, 135):
        rad = np.radians(a)
        dx, dy = r * np.cos(rad), r * np.sin(rad)
        ax.plot([cx - dx, cx + dx], [cy - dy, cy + dy],
                color='#000', linewidth=0.35)
    ax.add_patch(Circle((cx, cy), 0.28, fill=True, facecolor='#fff',
                        edgecolor='#000', linewidth=0.35))
    ax.text(cx + 1.2, cy + 0.5, label, ha='left', va='center',
            fontsize=2.9, color='#000')


def smoke(ax, cx, cy):
    ax.add_patch(Circle((cx, cy), 0.5, fill=False, edgecolor='#000',
                        linewidth=0.4))
    ax.text(cx, cy, 'SD', ha='center', va='center', fontsize=2.9,
            color='#000')
    ax.text(cx + 1.0, cy + 0.5, 'SMOKE DET.', ha='left', va='center',
            fontsize=2.7, color='#000')


def can_light(ax, cx, cy):
    """Recessed can light - circle with cross."""
    ax.add_patch(Circle((cx, cy), 0.35, fill=False, edgecolor='#000',
                        linewidth=0.4))
    thin_line(ax, cx - 0.35, cy, cx + 0.35, cy, lw=0.3)
    thin_line(ax, cx, cy - 0.35, cx, cy + 0.35, lw=0.3)


def hanging_light(ax, cx, cy, label='HANGING FIXTURE'):
    ax.add_patch(Circle((cx, cy), 0.45, fill=True, facecolor='#000',
                        edgecolor='#000', linewidth=0.4))
    ax.add_patch(Circle((cx, cy), 0.25, fill=True, facecolor='#fff',
                        edgecolor='none'))
    ax.text(cx + 1.0, cy + 0.4, label, ha='left', va='center',
            fontsize=2.7, color='#000')


def wire(ax, pts, lw=0.3):
    """Curved dashed wiring line through a list of points.  Uses
    Path with CURVE3 for smooth arcs."""
    if len(pts) < 2:
        return
    verts = [pts[0]]
    codes = [Path.MOVETO]
    for i in range(1, len(pts) - 1):
        verts.append(pts[i])
        verts.append(pts[i + 1])
        codes.append(Path.CURVE3)
        codes.append(Path.CURVE3)
    if len(pts) == 2:
        verts.append(pts[1])
        codes.append(Path.LINETO)
    path = Path(verts, codes)
    ax.add_patch(PathPatch(path, fill=False, edgecolor='#000',
                           linewidth=lw, linestyle=(0, (3, 3))))


def callout(ax, x, y, text, target=None):
    ax.text(x, y, text, ha='left', va='center', fontsize=2.8, color='#000')
    if target:
        thin_line(ax, x - 0.3, y, target[0], target[1], lw=0.25)


# =============================================================================
# Primary Bedroom (12..30 x 18..30)
# =============================================================================

ceiling_fan(ax, 20, 24)
switch(ax, 29, 27)  # switch by door
# outlets along walls
for wx in (14, 17, 22, 27):
    outlet(ax, wx, 18.9)
outlet(ax, 12.9, 22)
outlet(ax, 12.9, 27)
outlet(ax, 29.1, 22)
smoke(ax, 22, 27.5)

# Wiring
wire(ax, [(20, 24), (26, 26.5), (29, 27)])
wire(ax, [(20, 24), (14, 22), (13, 20)])

# =============================================================================
# Bath 1 (12..22 x 30..38)
# =============================================================================

can_light(ax, 15, 34)
can_light(ax, 19, 34)
ceiling_fan(ax, 17, 36, label='VENT')
outlet(ax, 13, 32.5)   # by tub
outlet(ax, 20, 32.5)   # by vanity
switch(ax, 22, 37)
wire(ax, [(15, 34), (19, 34), (17, 36), (22, 37)])

# =============================================================================
# W.I.C. (22..30 x 30..38)
# =============================================================================

can_light(ax, 26, 34)
switch(ax, 29.4, 30.7)
wire(ax, [(26, 34), (29.4, 30.7)])

# =============================================================================
# Den / Study (12..30 x 38..50)
# =============================================================================

ceiling_fan(ax, 20, 44)
quad_outlet(ax, 20, 39)  # for TV
outlet(ax, 14, 39)
outlet(ax, 26, 39)
outlet(ax, 12.9, 44)
outlet(ax, 29.1, 44)
outlet(ax, 14, 49.5)
outlet(ax, 26, 49.5)
switch(ax, 29.4, 41)
smoke(ax, 22, 47)
wire(ax, [(20, 44), (22, 47), (29.4, 41)])
wire(ax, [(20, 44), (20, 39)])

# =============================================================================
# Great Room (30..48 x 25..42)
# =============================================================================

ceiling_fan(ax, 39, 33)
can_light(ax, 34, 30)
can_light(ax, 44, 30)
can_light(ax, 34, 38)
can_light(ax, 44, 38)
quad_outlet(ax, 39, 25.6)   # TV wall
outlet(ax, 32, 25.6)
outlet(ax, 46, 25.6)
outlet(ax, 30.9, 30)
outlet(ax, 30.9, 38)
outlet(ax, 47.1, 30)
outlet(ax, 47.1, 38)
switch(ax, 30.9, 41)
switch(ax, 47.1, 41)
smoke(ax, 39, 40)
wire(ax, [(39, 33), (44, 30), (47.1, 30)])
wire(ax, [(39, 33), (34, 30), (30.9, 30)])
wire(ax, [(39, 33), (39, 40), (39, 41)])
wire(ax, [(39, 33), (39, 25.6)])

# =============================================================================
# Kitchen (30..48 x 42..50)
# =============================================================================

hanging_light(ax, 39, 44.5, label='HANGING\nFIXTURE')
can_light(ax, 34, 48)
can_light(ax, 44, 48)
can_light(ax, 34, 43)
can_light(ax, 44, 43)
# Island outlets
outlet(ax, 35, 45.5)
outlet(ax, 43, 45.5)
# Counter outlets
outlet(ax, 33, 49.4)
outlet(ax, 38, 49.4)
outlet(ax, 42, 49.4)
outlet(ax, 46, 49.4)
# Stove 220V
outlet(ax, 40, 49.4)
callout(ax, 41, 52, '220V FOR RANGE',
        target=(40.5, 49.3))
callout(ax, 41, 53, '110V FOR CFM',
        target=(38.5, 49.3))
# Dishwasher outlet
outlet(ax, 34, 42.4)
callout(ax, 27.5, 44, '220V FOR DBL OVEN',
        target=(33.7, 42.6))
switch(ax, 30.9, 43)
wire(ax, [(39, 44.5), (34, 43), (30.9, 43)])
wire(ax, [(39, 44.5), (44, 43), (46, 42.5)])
wire(ax, [(39, 44.5), (39, 49.4)])

# =============================================================================
# Pantry (48..55 x 42..50)
# =============================================================================

can_light(ax, 51.5, 46)
switch(ax, 48.9, 43)
outlet(ax, 51.5, 49.4)
wire(ax, [(51.5, 46), (48.9, 43)])

# =============================================================================
# Foyer / Powder / Mud row (30..55 x 18..25)
# =============================================================================

hanging_light(ax, 36, 22, label='HANGING\nFIXTURE')
can_light(ax, 45, 22)
outlet(ax, 33, 18.9)
outlet(ax, 40.5, 18.9)
outlet(ax, 51.5, 18.9)
switch(ax, 41.4, 22)
switch(ax, 49.4, 22)
smoke(ax, 36, 24)
wire(ax, [(36, 22), (36, 24), (41.4, 22), (45, 22)])

# =============================================================================
# 3-Car Garage (55..82 x 20..46)
# =============================================================================

can_light(ax, 62, 26)
can_light(ax, 74, 26)
can_light(ax, 62, 40)
can_light(ax, 74, 40)
outlet(ax, 56, 25)
outlet(ax, 56, 32)
outlet(ax, 56, 41)
outlet(ax, 81, 25)
outlet(ax, 81, 33)
outlet(ax, 81, 41)
quad_outlet(ax, 68, 20.9)
switch(ax, 56, 45)
callout(ax, 56.5, 47, '110V OUTLET FOR G/M',
        target=(56.2, 45.5))
callout(ax, 56.5, 48, 'GARAGE DR. OPENER',
        target=(56.2, 45.5))
callout(ax, 82.5, 33, 'A/C CONDENSER UNIT',
        target=(81.5, 33))
callout(ax, 82.5, 32, '220V FOR A/C',
        target=(81.5, 33))
callout(ax, 82.5, 31, '110V FOR A/C W/GFI',
        target=(81.5, 33))
smoke(ax, 68, 40)
wire(ax, [(56, 45), (62, 40), (74, 40), (81, 41)])
wire(ax, [(56, 45), (62, 26), (68, 20.9)])
wire(ax, [(68, 40), (74, 26)])

# =============================================================================
# Exterior floods + hose bibs
# =============================================================================

callout(ax, 32, 15.5, 'HOSE BIBB',
        target=(33, 17.8))
callout(ax, 14, 51.5, 'FLOOD LITE',
        target=(13, 50.2))
callout(ax, 83, 51, 'FLOOD LITE',
        target=(82.2, 46))
callout(ax, 83, 18, 'HOSE BIBB',
        target=(82.2, 20.3))

# =============================================================================
# Plan title
# =============================================================================

plan_title(ax, 72, 12, 'ELECTRICAL PLAN')

# Export
import os
outdir = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(outdir, 'kenwood-electrical.jpg')
plt.savefig(out, dpi=200, bbox_inches='tight', pad_inches=0.4,
            facecolor='#ffffff')
plt.close(fig)
print(f'Wrote {out}')
