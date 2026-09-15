"""A3.2 Side Elevations - Left + Right, Prechtel real-CAD style.

Kenwood is 2-STORY, so side elevations show both plate lines and both
floor benchmarks.  Same stucco-stipple walls, shingle-textured hip roofs,
plate-height/first-floor labels on left, wavy ground line.
"""

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Circle
import numpy as np
from style_common import new_sheet, thin_line, plan_title


# =============================================================================
# Helpers (same as gen_elevations.py — kept local for clarity)
# =============================================================================

def stucco_stipple(ax, x, y, w, h, density=140, seed=1):
    rng = np.random.default_rng(seed)
    xs = rng.uniform(x, x + w, density)
    ys = rng.uniform(y, y + h, density)
    ax.scatter(xs, ys, s=0.5, c='#000', alpha=0.35, linewidths=0)


def shingle_texture(ax, poly_pts, spacing=0.5, seed=2):
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
                ax.plot([cx, cx + spacing * 0.55], [y, y], color='#000',
                        linewidth=0.2, alpha=0.8, solid_capstyle='butt')
            x += spacing
        y += spacing * 0.7
        row += 1


def roof_outline(ax, pts, lw=0.85):
    ax.add_patch(Polygon(pts, closed=True, fill=False,
                         edgecolor='#000', linewidth=lw))


def window(ax, x, y, w, h, panes_v=2):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.55))
    ax.add_patch(Rectangle((x + 0.1, y + 0.1), w - 0.2, h - 0.2, fill=False,
                           edgecolor='#000', linewidth=0.3))
    for i in range(1, panes_v):
        xm = x + i * w / panes_v
        thin_line(ax, xm, y + 0.1, xm, y + h - 0.1, lw=0.3)
    thin_line(ax, x + 0.1, y + h / 2, x + w - 0.1, y + h / 2, lw=0.3)


def garage_door(ax, x, y, w, h, panels_v=4):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.65))
    for r in range(1, panels_v):
        yr = y + r * h / panels_v
        thin_line(ax, x, yr, x + w, yr, lw=0.35)
    # panel windows on second-from-top row
    y_win = y + (panels_v - 2) * h / panels_v
    for c in range(4):
        cx = x + (c + 0.5) * w / 4
        ax.add_patch(Rectangle((cx - 0.35, y_win + h / panels_v * 0.35),
                               0.7, h / panels_v * 0.35, fill=False,
                               edgecolor='#000', linewidth=0.3))


def benchmark_line(ax, y, x_from, x_to, label):
    thin_line(ax, x_from, y, x_to, y, lw=0.35)
    ax.add_patch(Circle((x_from - 0.5, y), 0.18, fill=False,
                        edgecolor='#000', linewidth=0.35))
    ax.text(x_from - 1.0, y, label, ha='right', va='center',
            fontsize=3.5, color='#000')


def roof_slope_callout(ax, x, y, label='SHINGLE ROOF', sub='5 ON 12 ROOF SLOPE'):
    ax.add_patch(Rectangle((x, y), 12, 2.2, fill=True, facecolor='#fff',
                           edgecolor='#000', linewidth=0.35))
    ax.text(x + 6, y + 1.5, label, ha='center', va='center',
            fontsize=3.8, color='#000')
    ax.text(x + 6, y + 0.65, sub, ha='center', va='center',
            fontsize=3.4, color='#000')


def slope_arrow(ax, x, y, label='5', label2='12'):
    ax.plot([x, x + 3, x + 3], [y, y, y + 1.25], color='#000', linewidth=0.4)
    thin_line(ax, x, y, x + 3, y + 1.25, lw=0.4)
    ax.text(x + 1.5, y - 0.5, label2, ha='center', va='center',
            fontsize=3.8, color='#000')
    ax.text(x + 3.35, y + 0.6, label, ha='left', va='center',
            fontsize=3.8, color='#000')


def ground_line(ax, x_start, x_end, y_base=0):
    xs = np.linspace(x_start, x_end, 120)
    ys = y_base + 0.5 * np.sin(xs * 0.28) + 0.15 * np.cos(xs * 0.9)
    ax.plot(xs, ys, color='#000', linewidth=0.55)


# =============================================================================
# Draw sheet - 14 x 12, split top/bottom LEFT + RIGHT
# =============================================================================

fig, ax = new_sheet(figsize=(14, 12))
ax.set_ylim(0, 90)

# =========================================================================
# LEFT ELEVATION (top half, y ~ 50-85)
# =========================================================================
#
# Kenwood is 2-STORY - left side shows the depth of the house (~50' deep).
# House envelope in plan is 12..55 x 18..50 for main mass, so left side is
# the ~32' deep wall.  Second story runs the same depth.
# =========================================================================

FF   = 52    # first floor
PH1  = 62.5  # first-floor plate (10'-6")
FF2  = 63.5  # second floor bearing
PH2  = 72.0  # second-floor plate (~8'-6")
RIDGE = 82   # main hip ridge apex

WALL_L = 15
WALL_R = 90

# Wall envelope: 2-story flat rectangle across most of width, dropping down
# on the right for a 1-story bump (den/study wing at rear).
# Represent this as the outline with a step-down on the right last 15 units.
wall_pts = [
    (WALL_L, FF), (WALL_R - 18, FF),
    (WALL_R - 18, PH1), (WALL_R, PH1),
    (WALL_R, FF), (WALL_R, FF),  # dummy
]
# Simpler: draw full 2-story mass then overlay 1-story wing separately
# Full mass
ax.add_patch(Rectangle((WALL_L, FF), WALL_R - WALL_L - 18, PH2 - FF,
                       fill=True, facecolor='#fff', edgecolor='none'))
stucco_stipple(ax, WALL_L, FF, WALL_R - WALL_L - 18, PH2 - FF,
               density=520, seed=21)
ax.add_patch(Rectangle((WALL_L, FF), WALL_R - WALL_L - 18, PH2 - FF,
                       fill=False, edgecolor='#000', linewidth=0.75))

# 1-story rear wing (garage / rear low mass) on right
ax.add_patch(Rectangle((WALL_R - 18, FF), 18, PH1 - FF,
                       fill=True, facecolor='#fff', edgecolor='none'))
stucco_stipple(ax, WALL_R - 18, FF, 18, PH1 - FF, density=260, seed=22)
ax.add_patch(Rectangle((WALL_R - 18, FF), 18, PH1 - FF,
                       fill=False, edgecolor='#000', linewidth=0.75))

# Second-floor plate line (thin) across top of 2-story mass
thin_line(ax, WALL_L - 1, PH2, WALL_R - 17, PH2, lw=0.35)

# Main hip roof over 2-story mass
hip_main = [(WALL_L - 1, PH2), (WALL_R - 17, PH2),
            (WALL_R - 22, RIDGE), (WALL_L + 6, RIDGE)]
roof_outline(ax, hip_main)
shingle_texture(ax, hip_main, spacing=0.5, seed=23)

# Second small gable dormer breaking the front slope
dorm1 = [(WALL_L + 15, PH2), (WALL_L + 25, PH2),
         (WALL_L + 23, PH2 + 4), (WALL_L + 17, PH2 + 4)]
roof_outline(ax, dorm1)
shingle_texture(ax, dorm1, spacing=0.45, seed=24)
# dormer front face (rectangle)
ax.add_patch(Rectangle((WALL_L + 15, PH2), 10, 4, fill=True, facecolor='#fff',
                       edgecolor='#000', linewidth=0.5))
stucco_stipple(ax, WALL_L + 15, PH2, 10, 4, density=80, seed=25)
window(ax, WALL_L + 18, PH2 + 1, 4, 2.5, panes_v=2)

# Front (right side of drawing) tower dormer
tower = [(WALL_L + 42, PH2), (WALL_L + 52, PH2),
         (WALL_L + 50, PH2 + 5), (WALL_L + 44, PH2 + 5)]
roof_outline(ax, tower)
shingle_texture(ax, tower, spacing=0.45, seed=26)
ax.add_patch(Rectangle((WALL_L + 42, PH2), 10, 5, fill=True, facecolor='#fff',
                       edgecolor='#000', linewidth=0.5))
stucco_stipple(ax, WALL_L + 42, PH2, 10, 5, density=90, seed=27)
window(ax, WALL_L + 45, PH2 + 1, 4, 3, panes_v=2)

# Hip roof over 1-story rear wing
hip_rear = [(WALL_R - 19, PH1), (WALL_R + 1, PH1),
            (WALL_R - 2, PH1 + 4), (WALL_R - 16, PH1 + 4)]
roof_outline(ax, hip_rear)
shingle_texture(ax, hip_rear, spacing=0.45, seed=28)

# =========================================================================
# Windows and door on LEFT elevation
# =========================================================================

# First-floor windows across main mass
for wx in (18, 26, 38, 48, 60, 66):
    window(ax, wx, FF + 3, 3.5, 4.5, panes_v=2)

# Two-story great room glass wall (center) - taller window bank
ax.add_patch(Rectangle((44, FF + 1), 12, PH1 - FF - 1, fill=False,
                       edgecolor='#000', linewidth=0.55))
# mullions
for i in range(1, 4):
    xm = 44 + i * 3
    thin_line(ax, xm, FF + 1.2, xm, PH1 - 0.2, lw=0.3)
thin_line(ax, 44.2, (FF + PH1) / 2, 55.8, (FF + PH1) / 2, lw=0.3)

# Second-floor windows
for wx in (18, 26, 34, 60, 66):
    window(ax, wx, FF2 + 2, 3.5, 4, panes_v=2)

# Rear wing single door + window
ax.add_patch(Rectangle((WALL_R - 15, FF + 0.5), 2.5, 6.5, fill=False,
                       edgecolor='#000', linewidth=0.55))
window(ax, WALL_R - 8, FF + 3, 3.5, 4.5, panes_v=2)

# =========================================================================
# Benchmarks and labels
# =========================================================================

benchmark_line(ax, PH2, 6.5, WALL_L - 0.5, 'PLATE HEIGHT')
benchmark_line(ax, FF2, 6.5, WALL_L - 0.5, '2ND FLOOR')
benchmark_line(ax, PH1, 6.5, WALL_L - 0.5, 'PLATE HEIGHT')
benchmark_line(ax, FF, 6.5, WALL_L - 0.5, 'FIRST FLOOR')

# Height dimensions
thin_line(ax, 9, FF, 9, PH1, lw=0.35)
for yb in (FF, PH1):
    thin_line(ax, 8.8, yb, 9.2, yb, lw=0.4)
ax.text(8.7, (FF + PH1) / 2, '10\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

thin_line(ax, 9, FF2, 9, PH2, lw=0.35)
for yb in (FF2, PH2):
    thin_line(ax, 8.8, yb, 9.2, yb, lw=0.4)
ax.text(8.7, (FF2 + PH2) / 2, '8\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

ax.text(WALL_L + 3, FF + 10, 'STUCCO AS', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(WALL_L + 3, FF + 9.2, 'SPECIFIED', ha='left', va='center',
        fontsize=3.2, color='#000')

ax.text(WALL_L + 28, PH2 + 1.8, '2" x 8" WD. FASCIA', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(WALL_L + 28, PH2 + 1.1, 'W/ 1" x WD. TRIM', ha='left', va='center',
        fontsize=3.2, color='#000')

roof_slope_callout(ax, WALL_R - 25, RIDGE + 0.5)
slope_arrow(ax, WALL_L + 4, RIDGE - 4)
slope_arrow(ax, WALL_L + 34, RIDGE - 5)

# Ground line
ground_line(ax, WALL_L - 8, WALL_R + 8, y_base=FF - 0.3)

# Title
plan_title(ax, 35, 46, 'LEFT ELEVATION')

# =========================================================================
# RIGHT ELEVATION (bottom half) - the garage side
# =========================================================================

FF_r   = 10
PH1_r  = 20.5
FF2_r  = 21.5
PH2_r  = 30
RIDGE_r = 40

# Right elevation - garage on left half (1-story lower profile),
# main 2-story mass on right half
G_L = WALL_L + 3    # garage left edge
G_R = WALL_L + 35   # garage right edge (~32' garage)
M_R = WALL_R        # main body right edge

# Garage 1-story mass
ax.add_patch(Rectangle((G_L, FF_r), G_R - G_L, PH1_r - FF_r,
                       fill=True, facecolor='#fff', edgecolor='none'))
stucco_stipple(ax, G_L, FF_r, G_R - G_L, PH1_r - FF_r,
               density=340, seed=31)
ax.add_patch(Rectangle((G_L, FF_r), G_R - G_L, PH1_r - FF_r,
                       fill=False, edgecolor='#000', linewidth=0.75))

# Main 2-story mass
ax.add_patch(Rectangle((G_R, FF_r), M_R - G_R, PH2_r - FF_r,
                       fill=True, facecolor='#fff', edgecolor='none'))
stucco_stipple(ax, G_R, FF_r, M_R - G_R, PH2_r - FF_r,
               density=520, seed=32)
ax.add_patch(Rectangle((G_R, FF_r), M_R - G_R, PH2_r - FF_r,
                       fill=False, edgecolor='#000', linewidth=0.75))

# Garage hip roof
hip_g = [(G_L - 1, PH1_r), (G_R + 1, PH1_r),
         (G_R - 2, PH1_r + 4), (G_L + 3, PH1_r + 4)]
roof_outline(ax, hip_g)
shingle_texture(ax, hip_g, spacing=0.45, seed=33)

# Small entry gable between garage and main mass
gable_e = [(G_R - 5, PH1_r), (G_R + 5, PH1_r),
           (G_R + 3, PH1_r + 5.5), (G_R - 3, PH1_r + 5.5)]
roof_outline(ax, gable_e)
shingle_texture(ax, gable_e, spacing=0.45, seed=34)
# gable-front triangle
gable_e_front = [(G_R - 5, PH1_r + 5.5), (G_R, PH1_r + 8), (G_R + 5, PH1_r + 5.5)]
roof_outline(ax, gable_e_front)
shingle_texture(ax, gable_e_front, spacing=0.4, seed=35)

# Main hip roof over 2-story
hip_m = [(G_R - 1, PH2_r), (M_R + 1, PH2_r),
         (M_R - 4, RIDGE_r), (G_R + 4, RIDGE_r)]
roof_outline(ax, hip_m)
shingle_texture(ax, hip_m, spacing=0.5, seed=36)

# Windows and doors - RIGHT elevation
# Two garage doors on garage face
garage_door(ax, G_L + 3, FF_r + 0.5, 10, 7.5)
garage_door(ax, G_L + 16, FF_r + 0.5, 10, 7.5)

# Small entry porch box between garage and main mass
ax.add_patch(Rectangle((G_R - 3, FF_r), 6, 8, fill=False,
                       edgecolor='#000', linewidth=0.55))
ax.text(G_R, FF_r + 4, 'PORCH', ha='center', va='center',
        fontsize=3.6, color='#000',
        bbox=dict(boxstyle='square,pad=0.2', fc='#fff', ec='#000', lw=0.35))

# Main 2-story windows: first floor + second floor
for wx in (G_R + 8, G_R + 15, G_R + 22, G_R + 28, G_R + 36, G_R + 43):
    window(ax, wx, FF_r + 3, 3.5, 4.5, panes_v=2)
for wx in (G_R + 8, G_R + 15, G_R + 22, G_R + 28, G_R + 36, G_R + 43):
    window(ax, wx, FF2_r + 2, 3.5, 4, panes_v=2)

# Small "COVERED PATIO" opening on far right
ax.add_patch(Rectangle((M_R - 6, FF_r), 6, PH1_r - FF_r, fill=False,
                       edgecolor='#000', linewidth=0.5))
ax.text(M_R - 3, FF_r + 4, 'COVERED\nPATIO', ha='center', va='center',
        fontsize=3.2, color='#000',
        bbox=dict(boxstyle='square,pad=0.2', fc='#fff', ec='#000', lw=0.35))

# Benchmarks on right elevation (mirrored to left side of drawing)
benchmark_line(ax, PH2_r, 6.5, G_L - 0.5, 'PLATE HEIGHT')
benchmark_line(ax, FF2_r, 6.5, G_L - 0.5, '2ND FLOOR')
benchmark_line(ax, PH1_r, 6.5, G_L - 0.5, 'PLATE HEIGHT')
benchmark_line(ax, FF_r, 6.5, G_L - 0.5, 'FIRST FLOOR')

thin_line(ax, 9, FF_r, 9, PH1_r, lw=0.35)
for yb in (FF_r, PH1_r):
    thin_line(ax, 8.8, yb, 9.2, yb, lw=0.4)
ax.text(8.7, (FF_r + PH1_r) / 2, '10\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

thin_line(ax, 9, FF2_r, 9, PH2_r, lw=0.35)
for yb in (FF2_r, PH2_r):
    thin_line(ax, 8.8, yb, 9.2, yb, lw=0.4)
ax.text(8.7, (FF2_r + PH2_r) / 2, '8\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

ax.text(G_L + 3, FF_r + 10, 'STUCCO AS', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(G_L + 3, FF_r + 9.2, 'SPECIFIED', ha='left', va='center',
        fontsize=3.2, color='#000')

ax.text(G_R + 12, PH2_r + 1.5, '2" x 8" WD. FASCIA', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(G_R + 12, PH2_r + 0.8, 'W/ 1" x WD. TRIM', ha='left', va='center',
        fontsize=3.2, color='#000')

roof_slope_callout(ax, M_R - 25, RIDGE_r - 1.5)
slope_arrow(ax, G_L + 4, PH1_r + 4)
slope_arrow(ax, G_R - 5, PH1_r + 6)
slope_arrow(ax, G_R + 6, RIDGE_r - 4)

# Ground line
ground_line(ax, WALL_L - 8, WALL_R + 8, y_base=FF_r - 0.3)

# Title
plan_title(ax, 35, 3.5, 'RIGHT ELEVATION')

# =========================================================================
# Export
# =========================================================================

import os
outdir = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(outdir, 'kenwood-side-elevations.jpg')
plt.savefig(out, dpi=200, bbox_inches='tight', pad_inches=0.4,
            facecolor='#ffffff')
plt.close(fig)
print(f'Wrote {out}')
