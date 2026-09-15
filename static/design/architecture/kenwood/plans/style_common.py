"""Real-architect CAD style helpers.

Target: match the Prechtel Estate architect drawings exactly.
  * Pure white sheet, NO outer border
  * Solid black wall poche (filled walls) - the key visual
  * Small plan-title + scale label at bottom, with underline
  * Dense dimension chains with tick marks + extension lines
  * All fixtures drawn in outline (tubs, showers with X, sinks, toilets)
  * NO logo, NO revisions box, NO rotated stamps, NO title block
"""

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Arc, Circle, FancyArrowPatch
from matplotlib import rcParams
import numpy as np

rcParams['font.family'] = 'DejaVu Sans'


# =============================================================================
# Sheet setup - pure white, no border
# =============================================================================

def new_sheet(figsize=(14, 10)):
    fig, ax = plt.subplots(figsize=figsize, dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 70)
    ax.set_aspect('equal')
    ax.axis('off')
    fig.patch.set_facecolor('#ffffff')
    ax.set_facecolor('#ffffff')
    return fig, ax


# =============================================================================
# Solid black wall poche - the defining visual
# =============================================================================

def _perp_offset(p1, p2, dist):
    """Return unit perpendicular vector times dist for segment p1->p2."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = (dx * dx + dy * dy) ** 0.5 or 1.0
    return (-dy / L * dist, dx / L * dist)


def solid_wall_polygon(ax, pts, thickness=0.55, close=True, inward=True):
    """Draw exterior wall as a solid-black filled band (poche) following
    the outer perimeter `pts`.  Wall thickness is filled solid black inward
    (or outward if inward=False).
    """
    pts = list(pts)
    if close and pts[0] != pts[-1]:
        pts.append(pts[0])

    # Signed area determines winding
    n = len(pts) - 1
    area = 0.0
    for i in range(n):
        area += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    ccw = area > 0
    sign = 1.0 if (ccw ^ (not inward)) else -1.0

    inner = []
    for i in range(n):
        p_prev = pts[(i - 1) % n]
        p_curr = pts[i]
        p_next = pts[(i + 1) % n]
        d1 = (p_curr[0] - p_prev[0], p_curr[1] - p_prev[1])
        d2 = (p_next[0] - p_curr[0], p_next[1] - p_curr[1])
        L1 = (d1[0] ** 2 + d1[1] ** 2) ** 0.5 or 1.0
        L2 = (d2[0] ** 2 + d2[1] ** 2) ** 0.5 or 1.0
        n1 = (-d1[1] / L1 * sign, d1[0] / L1 * sign)
        n2 = (-d2[1] / L2 * sign, d2[0] / L2 * sign)
        bis = (n1[0] + n2[0], n1[1] + n2[1])
        LB = (bis[0] ** 2 + bis[1] ** 2) ** 0.5 or 1.0
        bis = (bis[0] / LB, bis[1] / LB)
        cos_a = max(0.25, bis[0] * n1[0] + bis[1] * n1[1])
        off = thickness / cos_a
        inner.append((p_curr[0] + bis[0] * off, p_curr[1] + bis[1] * off))
    inner.append(inner[0])

    # Fill the wall poche as one solid polygon: outer + reversed inner
    outer_pts = [(p[0], p[1]) for p in pts]
    inner_pts = list(reversed(inner))
    poly_pts = outer_pts + inner_pts
    ax.add_patch(Polygon(poly_pts, closed=True, facecolor='#000',
                         edgecolor='none', linewidth=0))


def solid_wall_segment(ax, x1, y1, x2, y2, thickness=0.35, side='right'):
    """Solid-black-filled interior partition (short segment).
    `side` controls which side the wall thickness offsets to.
    """
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5 or 1.0
    if side == 'right':
        nx, ny = -dy / L * thickness, dx / L * thickness
    elif side == 'left':
        nx, ny = dy / L * thickness, -dx / L * thickness
    else:  # both sides (centered)
        nx, ny = -dy / L * thickness / 2, dx / L * thickness / 2
        # Build 4-corner rectangle around the centerline
        p1 = (x1 + nx, y1 + ny)
        p2 = (x2 + nx, y2 + ny)
        p3 = (x2 - nx, y2 - ny)
        p4 = (x1 - nx, y1 - ny)
        ax.add_patch(Polygon([p1, p2, p3, p4], closed=True,
                             facecolor='#000', edgecolor='none'))
        return
    p1 = (x1, y1)
    p2 = (x2, y2)
    p3 = (x2 + nx, y2 + ny)
    p4 = (x1 + nx, y1 + ny)
    ax.add_patch(Polygon([p1, p2, p3, p4], closed=True,
                         facecolor='#000', edgecolor='none'))


def interior_wall(ax, x1, y1, x2, y2, thickness=0.28):
    """Interior partition - centered solid-black rectangle."""
    solid_wall_segment(ax, x1, y1, x2, y2, thickness=thickness, side='center')


def thin_line(ax, x1, y1, x2, y2, lw=0.45, dashed=False):
    ls = (0, (3, 3)) if dashed else '-'
    ax.plot([x1, x2], [y1, y2], color='#000', linewidth=lw,
            linestyle=ls, solid_capstyle='butt')


# =============================================================================
# Doors and openings
# =============================================================================

def door_arc(ax, hinge_x, hinge_y, radius=2.0, start_deg=0, extent=90, lw=0.4):
    arc = Arc((hinge_x, hinge_y), 2 * radius, 2 * radius,
              angle=0, theta1=start_deg, theta2=start_deg + extent,
              color='#000', linewidth=lw)
    ax.add_patch(arc)
    a = np.radians(start_deg)
    ax.plot([hinge_x, hinge_x + radius * np.cos(a)],
            [hinge_y, hinge_y + radius * np.sin(a)],
            color='#000', linewidth=lw)


# =============================================================================
# Fixtures - drawn in thin outline
# =============================================================================

def tub(ax, x, y, w=3.5, h=2.2):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.45))
    # Inner tub oval
    inner = Rectangle((x + 0.25, y + 0.25), w - 0.5, h - 0.5, fill=False,
                      edgecolor='#000', linewidth=0.35)
    ax.add_patch(inner)


def shower(ax, x, y, w=3.5, h=3.5, label='TILE\nSHOWER'):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.45))
    # Diagonal X
    thin_line(ax, x, y, x + w, y + h, lw=0.35)
    thin_line(ax, x, y + h, x + w, y, lw=0.35)
    if label:
        ax.text(x + w / 2, y + h / 2, label,
                ha='center', va='center', fontsize=3.6, color='#000')


def toilet(ax, cx, cy, facing='south'):
    """Small oval + tank rectangle."""
    ax.add_patch(Rectangle((cx - 0.55, cy - 0.75), 1.1, 0.5, fill=False,
                           edgecolor='#000', linewidth=0.35))  # tank
    # bowl
    from matplotlib.patches import Ellipse
    ax.add_patch(Ellipse((cx, cy + 0.15), 0.9, 1.1, fill=False,
                         edgecolor='#000', linewidth=0.35))


def sink_oval(ax, cx, cy, w=1.0, h=0.7):
    from matplotlib.patches import Ellipse
    ax.add_patch(Ellipse((cx, cy), w, h, fill=False,
                         edgecolor='#000', linewidth=0.35))


def vanity(ax, x, y, w, h=1.4):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.4))


def kitchen_island(ax, x, y, w=10, h=2.2):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.5))


def stove(ax, cx, cy):
    ax.add_patch(Rectangle((cx - 1.5, cy - 0.9), 3.0, 1.8, fill=False,
                           edgecolor='#000', linewidth=0.4))
    for dx, dy in [(-0.7, 0.35), (0.7, 0.35), (-0.7, -0.35), (0.7, -0.35)]:
        ax.add_patch(Circle((cx + dx, cy + dy), 0.22, fill=False,
                            edgecolor='#000', linewidth=0.3))


def fridge(ax, x, y, w=2.5, h=2.5):
    ax.add_patch(Rectangle((x, y), w, h, fill=False,
                           edgecolor='#000', linewidth=0.4))
    thin_line(ax, x, y + h - 0.15, x + w, y + h - 0.15, lw=0.3)


def stair(ax, x1, y1, x2, y2, treads=12, direction='up'):
    """Draw stair treads (thin lines) inside the rectangle."""
    for i in range(1, treads):
        t = i / treads
        if x2 - x1 > y2 - y1:  # horizontal stair
            xt = x1 + t * (x2 - x1)
            thin_line(ax, xt, y1 + 0.2, xt, y2 - 0.2, lw=0.3)
        else:  # vertical stair (Justin's Kenwood)
            yt = y1 + t * (y2 - y1)
            thin_line(ax, x1 + 0.2, yt, x2 - 0.2, yt, lw=0.3)


# =============================================================================
# Dimension chains
# =============================================================================

def dim_h(ax, x1, x2, y, label, ext_from_y=None, tick_len=0.5, lw=0.35,
          font=4.2):
    """Horizontal dimension with tick marks."""
    if ext_from_y is not None:
        ax.plot([x1, x1], [ext_from_y, y + 0.25], color='#000', linewidth=0.25)
        ax.plot([x2, x2], [ext_from_y, y + 0.25], color='#000', linewidth=0.25)
    ax.plot([x1, x2], [y, y], color='#000', linewidth=lw)
    for x in (x1, x2):
        ax.plot([x - 0.25, x + 0.25], [y - tick_len / 2, y + tick_len / 2],
                color='#000', linewidth=0.5)
    ax.text((x1 + x2) / 2, y + 0.35, label,
            ha='center', va='bottom', fontsize=font, color='#000')


def dim_v(ax, y1, y2, x, label, ext_from_x=None, tick_len=0.5, lw=0.35,
          font=4.2):
    if ext_from_x is not None:
        ax.plot([ext_from_x, x - 0.25], [y1, y1], color='#000', linewidth=0.25)
        ax.plot([ext_from_x, x - 0.25], [y2, y2], color='#000', linewidth=0.25)
    ax.plot([x, x], [y1, y2], color='#000', linewidth=lw)
    for y in (y1, y2):
        ax.plot([x - tick_len / 2, x + tick_len / 2], [y - 0.25, y + 0.25],
                color='#000', linewidth=0.5)
    ax.text(x - 0.35, (y1 + y2) / 2, label,
            ha='right', va='center', fontsize=font, color='#000',
            rotation=90)


# =============================================================================
# Bottom plan-title label - the ONLY margin element
# =============================================================================

def plan_title(ax, x, y, title, scale='1/4" = 1\'-0"'):
    """Small title + scale, with a rectangular underline underneath.
    Matches the Prechtel style exactly."""
    ax.text(x, y + 0.9, title, ha='center', va='center',
            fontsize=10, color='#000')
    # Rectangular underline
    lw_line = len(title) * 0.32
    ax.plot([x - lw_line / 2, x + lw_line / 2], [y + 0.35, y + 0.35],
            color='#000', linewidth=0.8)
    ax.text(x, y - 0.3, f'SCALE: {scale}',
            ha='center', va='center', fontsize=5.2, color='#000')


def elevation_marker(ax, x, y, label='FRONT ELEV.'):
    """Small chevron marker (like Justin's) pointing at the plan side."""
    ax.plot([x - 0.6, x, x + 0.6], [y + 0.5, y, y + 0.5],
            color='#000', linewidth=0.5)
    ax.text(x, y - 0.6, label, ha='center', va='center',
            fontsize=3.5, color='#000')


def side_elev_arrow(ax, x, y, label='RIGHT SIDE ELEV.'):
    """Right/left side elevation call-out with chevron pointing outward."""
    ax.plot([x, x + 0.7, x], [y - 0.5, y, y + 0.5],
            color='#000', linewidth=0.5)
    ax.text(x + 1.4, y, label, ha='left', va='center',
            fontsize=3.5, color='#000')
