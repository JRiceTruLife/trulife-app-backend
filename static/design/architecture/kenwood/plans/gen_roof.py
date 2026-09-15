"""A4 Roof Plan - Kenwood, Prechtel real-CAD style.

Hip + gable layout with shingle-texture hatching fills, "5 ON 12 ROOF SLOPE"
boxed callouts with slope-direction arrows, ridge and valley lines, small
"ROOF PLAN" title.  No border, no logo, no rotated stamps.
"""

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, FancyArrowPatch
import numpy as np
from style_common import new_sheet, thin_line, plan_title, elevation_marker, side_elev_arrow


fig, ax = new_sheet(figsize=(14, 10))


def shingle_fill(ax, poly_pts, spacing=0.35, seed=2):
    """Hatched shingle-look inside a polygon (dashes offset row-by-row).
    Finer hatching to match Prechtel's roof plan."""
    from matplotlib.path import Path
    p = Path(poly_pts)
    xs = [pt[0] for pt in poly_pts]
    ys = [pt[1] for pt in poly_pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    row = 0
    y = ymin
    while y < ymax:
        offset = (row % 2) * spacing / 2
        x = xmin - 0.3
        while x < xmax:
            cx = x + offset
            if p.contains_point((cx, y)):
                ax.plot([cx, cx + spacing * 0.55], [y, y],
                        color='#000', linewidth=0.18, alpha=0.75,
                        solid_capstyle='butt')
            x += spacing
        y += spacing * 0.75
        row += 1


def roof_face(ax, pts, seed=2):
    """One roof face: outline + shingle fill."""
    ax.add_patch(Polygon(pts, closed=True, fill=False,
                         edgecolor='#000', linewidth=0.85))
    shingle_fill(ax, pts, spacing=0.55, seed=seed)


def dashed_wall_below(ax, pts):
    """Dashed rectangle showing wall footprint below the roof."""
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        ax.plot([x1, x2], [y1, y2], color='#000', linewidth=0.3,
                linestyle=(0, (3, 3)))


def slope_callout(ax, x, y, sub='5 ON 12 ROOF SLOPE', arrow_dir='right'):
    """Boxed 2-line callout + double arrow indicating slope direction."""
    ax.add_patch(Rectangle((x, y), 12, 2.2, fill=True, facecolor='#fff',
                           edgecolor='#000', linewidth=0.4))
    ax.text(x + 6, y + 1.55, '5 ON 12', ha='center', va='center',
            fontsize=3.8, color='#000')
    ax.text(x + 6, y + 0.7, sub, ha='center', va='center',
            fontsize=3.4, color='#000')
    # Arrow beneath the box
    if arrow_dir == 'right':
        ax.annotate('', xy=(x + 10.5, y - 0.7), xytext=(x + 1.5, y - 0.7),
                    arrowprops=dict(arrowstyle='<->', lw=0.5, color='#000'))
    elif arrow_dir == 'left':
        ax.annotate('', xy=(x + 1.5, y - 0.7), xytext=(x + 10.5, y - 0.7),
                    arrowprops=dict(arrowstyle='<->', lw=0.5, color='#000'))
    elif arrow_dir == 'up':
        ax.annotate('', xy=(x + 6, y + 4), xytext=(x + 6, y - 1.5),
                    arrowprops=dict(arrowstyle='<->', lw=0.5, color='#000'))
    elif arrow_dir == 'down':
        ax.annotate('', xy=(x + 6, y - 1.5), xytext=(x + 6, y + 4),
                    arrowprops=dict(arrowstyle='<->', lw=0.5, color='#000'))


# =============================================================================
# Kenwood roof - hip-and-gable over combined main body + garage
# Footprint (from A2.1): main mass 12..55 x 18..50, garage 55..82 x 20..46
# Overhang the roof by ~2 units beyond the walls, then create hip/valley
# =============================================================================

# --- Dashed footprint of walls below (Prechtel shows the footprint underlay)
dashed_wall_below(ax, [(12, 18), (55, 18), (55, 20), (82, 20), (82, 46),
                       (55, 46), (55, 50), (12, 50)])
dashed_wall_below(ax, [(30, 15), (48, 15), (48, 18), (30, 18)])  # front porch
dashed_wall_below(ax, [(15, 50), (55, 50), (55, 56), (15, 56)])  # rear patio

# --- Main body roof: overhangs to 10..57 x 16..52
# Split into 4 hip faces + central ridge line
OL, OR = 10, 57   # main mass outer left/right
OB, OT = 16, 52   # main mass outer bottom/top
# Ridge: horizontal line across the middle
RIDGE_Y = (OB + OT) / 2  # 34

# South (front) hip face — trapezoid
face_S = [(OL, OB), (OR, OB), (52, RIDGE_Y), (15, RIDGE_Y)]
roof_face(ax, face_S, seed=3)
# North (rear) hip face
face_N = [(OL, OT), (OR, OT), (52, RIDGE_Y), (15, RIDGE_Y)]
roof_face(ax, face_N, seed=4)
# West (left) hip triangle
face_W = [(OL, OB), (OL, OT), (15, RIDGE_Y)]
roof_face(ax, face_W, seed=5)
# East (right) hip triangle
face_E = [(OR, OB), (OR, OT), (52, RIDGE_Y)]
roof_face(ax, face_E, seed=6)

# Center ridge line
thin_line(ax, 15, RIDGE_Y, 52, RIDGE_Y, lw=0.7)

# --- Garage roof: hip over 53..84 x 18..48
# The west edge (55) abuts the main body, so no roof face on the west side.
GL, GR = 53, 84
GB, GT = 18, 48
GRIDGE = 33

# Garage: three visible hip faces (south, north, east) plus the west edge
# terminates against the main body's east hip face.  The garage ridge
# runs from a valley on the west (at ~56, GRIDGE) to a hip apex on the east.
g_S = [(GL, GB), (GR, GB), (81, GRIDGE), (56, GRIDGE)]
roof_face(ax, g_S, seed=7)
g_N = [(GL, GT), (GR, GT), (81, GRIDGE), (56, GRIDGE)]
roof_face(ax, g_N, seed=8)
g_E = [(GR, GB), (GR, GT), (81, GRIDGE)]
roof_face(ax, g_E, seed=9)

# Garage ridge
thin_line(ax, 56, GRIDGE, 81, GRIDGE, lw=0.7)

# Valley where garage roof meets main body's east hip face
thin_line(ax, OR, OB, 56, GRIDGE, lw=0.55)  # south valley
thin_line(ax, OR, OT, 56, GRIDGE, lw=0.55)  # north valley

# --- Front porch small hip over 30..48 x 12..18 (extends front of main body)
p_pts_S = [(28, 12), (50, 12), (48, 15.5), (30, 15.5)]
roof_face(ax, p_pts_S, seed=11)
p_pts_L = [(28, 12), (30, 15.5), (30, 18)]
roof_face(ax, p_pts_L, seed=11)
p_pts_R = [(50, 12), (48, 15.5), (48, 18)]
roof_face(ax, p_pts_R, seed=11)
thin_line(ax, 30, 15.5, 48, 15.5, lw=0.55)  # porch ridge

# --- Rear covered patio hip roof 13..57 x 50..58
r_pts_N = [(13, 58), (57, 58), (55, 55), (15, 55)]
roof_face(ax, r_pts_N, seed=13)
r_pts_L = [(13, 58), (15, 55), (15, 52)]
roof_face(ax, r_pts_L, seed=13)
r_pts_R = [(57, 58), (55, 55), (55, 52)]
roof_face(ax, r_pts_R, seed=13)
thin_line(ax, 15, 55, 55, 55, lw=0.55)

# =============================================================================
# Slope callouts on each major face
# =============================================================================

slope_callout(ax, 18, 24, arrow_dir='down')   # S face main
slope_callout(ax, 18, 40, arrow_dir='up')     # N face main
slope_callout(ax, 58, 24, arrow_dir='down')   # S face garage
slope_callout(ax, 58, 40, arrow_dir='up')     # N face garage
slope_callout(ax, 68, 32, arrow_dir='right')  # E hip garage (right)

# =============================================================================
# Elevation markers
# =============================================================================

elevation_marker(ax, 40, 6, 'FRONT ELEV.')
elevation_marker(ax, 40, 62, 'REAR ELEV.')
side_elev_arrow(ax, 4, 34, 'LEFT SIDE ELEV.')
# Right side marker pointing outward
ax.plot([95, 94.3, 95], [33.5, 34, 34.5], color='#000', linewidth=0.5)
ax.text(95.7, 34, 'RIGHT SIDE ELEV.', ha='left', va='center',
        fontsize=3.5, color='#000')

# =============================================================================
# Plan title
# =============================================================================

plan_title(ax, 78, 8, 'ROOF PLAN')

# Export
import os
outdir = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(outdir, 'kenwood-roof.jpg')
plt.savefig(out, dpi=200, bbox_inches='tight', pad_inches=0.4,
            facecolor='#ffffff')
plt.close(fig)
print(f'Wrote {out}')
