"""A3.1 Elevations - front + rear, Prechtel real-CAD style.

Pure white sheet, no border. Stucco walls (subtle stipple), shingle roof
texture, plate-height / first-floor benchmark lines on left with elevation
markers, thin ground line, small "FRONT ELEVATION" / "REAR ELEVATION" labels.
"""

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Circle, Arc
import numpy as np
from style_common import new_sheet, thin_line, plan_title


# =============================================================================
# Texture helpers
# =============================================================================

def stucco_stipple(ax, x, y, w, h, density=140, seed=1):
    """Very fine random dots — Prechtel's stucco walls have a light stipple."""
    rng = np.random.default_rng(seed)
    xs = rng.uniform(x, x + w, density)
    ys = rng.uniform(y, y + h, density)
    ax.scatter(xs, ys, s=0.5, c='#000', alpha=0.35, linewidths=0)


def shingle_texture(ax, poly_pts, spacing=0.55, seed=2):
    """Hatched shingle-look inside a polygon.  Draws parallel short dashes
    following the polygon's dominant axis."""
    from matplotlib.path import Path
    from matplotlib.patches import PathPatch
    p = Path(poly_pts)
    xs = [pt[0] for pt in poly_pts]
    ys = [pt[1] for pt in poly_pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    rng = np.random.default_rng(seed)
    y = ymin
    row = 0
    while y < ymax:
        x = xmin - 0.3
        offset = (row % 2) * spacing / 2
        while x < xmax:
            cx, cy = x + offset, y
            if p.contains_point((cx, cy)):
                ax.plot([cx, cx + spacing * 0.55], [cy, cy], color='#000',
                        linewidth=0.22, alpha=0.85, solid_capstyle='butt')
            x += spacing
        y += spacing * 0.65
        row += 1
    # Clip via boundary path
    boundary = PathPatch(p, fill=False, edgecolor='none')
    ax.add_patch(boundary)


def roof_outline(ax, pts, lw=0.85):
    ax.add_patch(Polygon(pts, closed=True, fill=False,
                         edgecolor='#000', linewidth=lw))


# =============================================================================
# Building block helpers
# =============================================================================

def window(ax, x, y, w, h, panes_v=2, mullion=True):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.55))
    ax.add_patch(Rectangle((x + 0.1, y + 0.1), w - 0.2, h - 0.2, fill=False,
                           edgecolor='#000', linewidth=0.3))
    if mullion:
        for i in range(1, panes_v):
            xm = x + i * w / panes_v
            thin_line(ax, xm, y + 0.1, xm, y + h - 0.1, lw=0.3)
    # Horizontal mid-rail
    thin_line(ax, x + 0.1, y + h / 2, x + w - 0.1, y + h / 2, lw=0.3)


def door(ax, x, y, w=2.2, h=6.0, panels=4):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.6))
    for i in range(panels):
        py = y + 0.4 + i * (h - 0.8) / panels
        ax.add_patch(Rectangle((x + 0.25, py), w - 0.5, (h - 0.8) / panels - 0.2,
                               fill=False, edgecolor='#000', linewidth=0.3))


def garage_door(ax, x, y, w, h, panels_v=4, panels_h=4):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.65))
    for r in range(1, panels_v):
        yr = y + r * h / panels_v
        thin_line(ax, x, yr, x + w, yr, lw=0.35)
    for c in range(1, panels_h):
        xc = x + c * w / panels_h
        for r in range(panels_v):
            y0 = y + r * h / panels_v
            y1 = y + (r + 1) * h / panels_v
            # Small horizontal panel window row on second-from-top
            if r == panels_v - 2:
                thin_line(ax, x, y0 + 0.2, x + w, y0 + 0.2, lw=0.25)


def benchmark_line(ax, y, x_from, x_to, label):
    thin_line(ax, x_from, y, x_to, y, lw=0.35)
    # Tick + small circle at left tail
    ax.add_patch(Circle((x_from - 0.5, y), 0.18, fill=False,
                        edgecolor='#000', linewidth=0.35))
    ax.text(x_from - 1.0, y, label, ha='right', va='center',
            fontsize=3.5, color='#000')


def roof_slope_callout(ax, x, y, label='SHINGLE ROOF', sub='5 ON 12 ROOF SLOPE'):
    """Small boxed callout with two-line text (like Prechtel)."""
    ax.add_patch(Rectangle((x, y), 12, 2.2, fill=True, facecolor='#fff',
                           edgecolor='#000', linewidth=0.35))
    ax.text(x + 6, y + 1.5, label, ha='center', va='center',
            fontsize=3.8, color='#000')
    ax.text(x + 6, y + 0.65, sub, ha='center', va='center',
            fontsize=3.4, color='#000')


def slope_arrow(ax, x, y, label='5', label2='12'):
    """Small right-triangle slope indicator with 12 base, 5 rise."""
    ax.plot([x, x + 3, x + 3], [y, y, y + 1.25], color='#000', linewidth=0.4)
    thin_line(ax, x, y, x + 3, y + 1.25, lw=0.4)
    ax.text(x + 1.5, y - 0.5, label2, ha='center', va='center',
            fontsize=3.8, color='#000')
    ax.text(x + 3.35, y + 0.6, label, ha='left', va='center',
            fontsize=3.8, color='#000')


def ground_line(ax, x_start, x_end, y_base=0):
    """Soft undulating ground line."""
    xs = np.linspace(x_start, x_end, 120)
    ys = y_base + 0.35 * np.sin(xs * 0.35) + 0.1 * np.cos(xs * 0.9)
    ax.plot(xs, ys, color='#000', linewidth=0.55)


# =============================================================================
# Draw sheet
# =============================================================================

fig, ax = new_sheet(figsize=(14, 12))
ax.set_ylim(0, 90)  # taller sheet for two stacked elevations

# =========================================================================
# FRONT ELEVATION (top half, y ~ 50-85)
# =========================================================================

FF = 52   # first-floor line
PH = 62.5  # plate-height first floor (roughly 10'-6")
RIDGE = 72  # main ridge height (front-facing gable / hip)

# Main body outline - left main mass 12..55, garage wing 55..82, right small wing 82..90
# Wall outline (bottom = FF, top = PH, right wing steps up)
wall_pts_front = [
    (12, FF), (55, FF), (55, FF), (82, FF), (90, FF),  # baseline
    (90, PH), (82, PH),
    (82, PH), (55, PH),
    (55, PH), (12, PH), (12, FF)
]
# Draw walls as stucco stipple field first
ax.add_patch(Rectangle((12, FF), 78, PH - FF, fill=True, facecolor='#ffffff',
                       edgecolor='none'))
stucco_stipple(ax, 12, FF, 78, PH - FF, density=520, seed=3)

# Wall outline
ax.add_patch(Rectangle((12, FF), 78, PH - FF, fill=False,
                       edgecolor='#000', linewidth=0.75))

# Main hip roof - big pyramid over 12..55 with ridge line
# Left main hip roof
hip1 = [(11, PH), (55, PH), (48, RIDGE - 1.5), (18, RIDGE - 1.5)]
roof_outline(ax, hip1, lw=0.85)
shingle_texture(ax, hip1, spacing=0.5, seed=5)

# Center gable dormer over garage entry (~55..70)
gable = [(55, PH), (70, PH), (70, PH + 4), (62.5, PH + 7), (55, PH + 4)]
roof_outline(ax, gable, lw=0.85)
shingle_texture(ax, gable, spacing=0.5, seed=6)

# Right hip roof over 70..90
hip2 = [(70, PH), (90, PH), (87, RIDGE - 3.5), (73, RIDGE - 3.5)]
roof_outline(ax, hip2, lw=0.85)
shingle_texture(ax, hip2, spacing=0.5, seed=7)

# Fascia lines at plate height
thin_line(ax, 11, PH, 91, PH, lw=0.35)
thin_line(ax, 11.5, PH - 0.35, 90.5, PH - 0.35, lw=0.28)

# Left side small gable dormer bump over Den (18..24) at PH..PH+3
dorm = [(18, PH), (24, PH), (24, PH + 2.5), (21, PH + 3.8), (18, PH + 2.5)]
roof_outline(ax, dorm, lw=0.7)
shingle_texture(ax, dorm, spacing=0.45, seed=8)

# =========================================================================
# Windows and doors (front, y = FF..PH  =  height 10.5)
# =========================================================================

# Left main mass (Den 15..17, Primary Bedroom windows 19..30)
window(ax, 14, FF + 3.5, 3.0, 4.5, panes_v=2)   # Den window
window(ax, 20, FF + 3.5, 3.5, 4.5, panes_v=2)   # Primary
window(ax, 25, FF + 3.5, 3.5, 4.5, panes_v=2)   # Primary
window(ax, 30, FF + 3.5, 3.5, 4.5, panes_v=2)   # Great room

# Garage: 2 garage doors 34..44 and 45..54
garage_door(ax, 34, FF + 0.5, 10, 7.5)
garage_door(ax, 45, FF + 0.5, 9, 7.5)

# Front entry door + sidelight + arched detail 56..62
door(ax, 58, FF + 0.5, 3.2, 7.2, panels=4)
# Small arch top over entry
arch_pts = np.linspace(0, np.pi, 24)
arch_x = 58 + 1.6 + 1.6 * np.cos(arch_pts + np.pi)
arch_y = FF + 7.7 + 0.9 * np.sin(arch_pts)
ax.plot(arch_x + 1.6, arch_y, color='#000', linewidth=0.4)

# Right wing: two arched windows
for wx in (72, 78, 84):
    ax.add_patch(Rectangle((wx, FF + 2.5), 3.5, 5.5, fill=False,
                           edgecolor='#000', linewidth=0.5))
    # arch top
    xs = np.linspace(0, np.pi, 20)
    ax.plot(wx + 1.75 + 1.75 * np.cos(xs + np.pi),
            FF + 8.0 + 0.7 * np.sin(xs), color='#000', linewidth=0.4)
    # Mullion
    thin_line(ax, wx + 1.75, FF + 2.6, wx + 1.75, FF + 7.9, lw=0.3)

# =========================================================================
# Benchmark lines on left
# =========================================================================

benchmark_line(ax, PH, 4, 11.5, 'PLATE HEIGHT')
benchmark_line(ax, FF, 4, 11.5, 'FIRST FLOOR')
# Dimension between plate and floor
thin_line(ax, 6, FF, 6, PH, lw=0.35)
for yb in (FF, PH):
    thin_line(ax, 5.8, yb, 6.2, yb, lw=0.4)
ax.text(5.7, (FF + PH) / 2, '10\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

# Fascia + trim callouts on right of front elevation
ax.text(46, PH + 1.8, '2" x 8" WD. FASCIA', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(46, PH + 1.1, 'W/ 1" x WD. TRIM', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.plot([45.8, 47], [PH + 1.5, PH + 0.4], color='#000', linewidth=0.3)

ax.text(14, FF + 10.5, 'STUCCO AS', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(14, FF + 9.8, 'SPECIFIED', ha='left', va='center',
        fontsize=3.2, color='#000')

# Roof callouts
roof_slope_callout(ax, 76, RIDGE + 0.5, 'SHINGLE ROOF', '5 ON 12 ROOF SLOPE')
slope_arrow(ax, 33, RIDGE - 3.5)

# Ground line
ground_line(ax, 8, 94, y_base=FF - 0.3)

# Front elevation title
plan_title(ax, 30, 46, 'FRONT ELEVATION')

# =========================================================================
# REAR ELEVATION (bottom half, y ~ 10-40)
# =========================================================================

FF2 = 10
PH2 = 20.5
RIDGE2 = 30.5

# Rear body 12..90 - two-story portion in center (covered patio)
ax.add_patch(Rectangle((12, FF2), 78, PH2 - FF2, fill=True, facecolor='#ffffff',
                       edgecolor='none'))
stucco_stipple(ax, 12, FF2, 78, PH2 - FF2, density=520, seed=13)
ax.add_patch(Rectangle((12, FF2), 78, PH2 - FF2, fill=False,
                       edgecolor='#000', linewidth=0.75))

# 2nd-story portion above the center (Kenwood is 2-story) - 30..70
ax.add_patch(Rectangle((30, PH2), 40, 8, fill=True, facecolor='#ffffff',
                       edgecolor='none'))
stucco_stipple(ax, 30, PH2, 40, 8, density=280, seed=14)
ax.add_patch(Rectangle((30, PH2), 40, 8, fill=False,
                       edgecolor='#000', linewidth=0.75))

# 2nd-floor plate height
PH2b = PH2 + 8
thin_line(ax, 29, PH2b, 71, PH2b, lw=0.35)

# Main hip roof over the 2-story portion
hip_r = [(29, PH2b), (71, PH2b), (65, RIDGE2), (35, RIDGE2)]
roof_outline(ax, hip_r, lw=0.85)
shingle_texture(ax, hip_r, spacing=0.5, seed=15)

# Left side hip lower roof
hip_rL = [(11, PH2), (30, PH2), (30, PH2 + 3), (20, PH2 + 5), (12, PH2 + 3)]
roof_outline(ax, hip_rL, lw=0.8)
shingle_texture(ax, hip_rL, spacing=0.5, seed=16)

# Right side hip lower roof
hip_rR = [(70, PH2), (91, PH2), (89, PH2 + 3), (80, PH2 + 5), (70, PH2 + 3)]
roof_outline(ax, hip_rR, lw=0.8)
shingle_texture(ax, hip_rR, spacing=0.5, seed=17)

# Windows - rear
# First floor windows (many, spread out)
for wx in (15, 20, 25, 65, 72, 78, 85):
    window(ax, wx, FF2 + 3.5, 3.0, 4.5, panes_v=2)

# Center rear = covered patio doors 40..60
ax.add_patch(Rectangle((40, FF2), 20, PH2 - FF2, fill=False,
                       edgecolor='#000', linewidth=0.55))
# Two patio doors
door(ax, 42, FF2 + 0.5, 3.5, 8.5, panels=2)
door(ax, 54, FF2 + 0.5, 3.5, 8.5, panels=2)
window(ax, 47, FF2 + 0.5, 5.5, 8.5, panes_v=3)

ax.text(50, FF2 + 4, 'COVERED PATIO', ha='center', va='center',
        fontsize=4, color='#000',
        bbox=dict(boxstyle='square,pad=0.3', fc='#fff', ec='#000', lw=0.35))

# Second-floor windows
for wx in (33, 38, 46, 54, 60, 65):
    window(ax, wx, PH2 + 2.5, 3.2, 5.0, panes_v=2)

# 2nd-floor center covered balcony
ax.add_patch(Rectangle((45, PH2), 10, 5.5, fill=False,
                       edgecolor='#000', linewidth=0.5))
ax.text(50, PH2 + 4, 'COVERED PATIO', ha='center', va='center',
        fontsize=3.6, color='#000',
        bbox=dict(boxstyle='square,pad=0.3', fc='#fff', ec='#000', lw=0.35))

# Benchmarks on left rear - 4 lines cleanly separated
benchmark_line(ax, PH2b, 4, 11.5, '2ND PLATE HT.')
benchmark_line(ax, PH2 + 0.3, 4, 11.5, '2ND FL. / 1ST PLATE')
benchmark_line(ax, FF2, 4, 11.5, '1ST FLOOR')

thin_line(ax, 6, FF2, 6, PH2, lw=0.35)
for yb in (FF2, PH2):
    thin_line(ax, 5.8, yb, 6.2, yb, lw=0.4)
ax.text(5.7, (FF2 + PH2) / 2, '10\'-6"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

thin_line(ax, 6, PH2, 6, PH2b, lw=0.35)
for yb in (PH2, PH2b):
    thin_line(ax, 5.8, yb, 6.2, yb, lw=0.4)
ax.text(5.7, (PH2 + PH2b) / 2, '9\'-0"', ha='right', va='center',
        fontsize=3.5, color='#000', rotation=90)

# Roof callouts rear
roof_slope_callout(ax, 76, RIDGE2 - 1.5, 'SHINGLE ROOF', '5 ON 12 ROOF SLOPE')
slope_arrow(ax, 25, PH2 + 5)
slope_arrow(ax, 78, PH2 + 5)

ax.text(35, PH2b + 1.5, '2" x 8" WD. FASCIA', ha='left', va='center',
        fontsize=3.2, color='#000')
ax.text(35, PH2b + 0.8, 'W/ 1" x WD. TRIM', ha='left', va='center',
        fontsize=3.2, color='#000')

# Ground line rear
ground_line(ax, 8, 94, y_base=FF2 - 0.3)

# Rear elevation title
plan_title(ax, 30, 3.5, 'REAR ELEVATION')

# =========================================================================
# Export
# =========================================================================

import os
outdir = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(outdir, 'kenwood-elevations.jpg')
plt.savefig(out, dpi=200, bbox_inches='tight', pad_inches=0.4,
            facecolor='#ffffff')
plt.close(fig)
print(f'Wrote {out}')
