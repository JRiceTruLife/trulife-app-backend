#!/usr/bin/env python3
"""A2.1 First Floor Plan - real architect CAD style (Prechtel-matching)."""

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from style_common import (
    new_sheet, plan_title, elevation_marker, side_elev_arrow,
    solid_wall_polygon, solid_wall_segment, interior_wall, thin_line,
    door_arc, tub, shower, toilet, sink_oval, vanity, kitchen_island,
    stove, fridge, stair, dim_h, dim_v,
)

fig, ax = new_sheet()

# =============================================================================
# ENVELOPE - traced clockwise, solid-black wall poche filled inward
# Main mass:  X 12..55, Y 18..50   (rectangular)
# Garage:    X 55..82, Y 20..46    (stepped-in wing)
# Overall footprint 70' x 32' at 1"=1'
# =============================================================================
env = [
    (12, 18),        # SW corner
    (12, 50),        # NW
    (55, 50),        # top of main mass
    (55, 46),        # step down to garage top
    (82, 46),        # NE of garage
    (82, 20),        # SE of garage
    (55, 20),        # step in below garage
    (55, 18),        # back to main mass floor
]
solid_wall_polygon(ax, env, thickness=0.55, inward=True)

# Wall dividing main mass from garage (Justin has one)
solid_wall_segment(ax, 55, 20, 55, 46, thickness=0.45, side='center')

# =============================================================================
# INTERIOR PARTITIONS (all solid-black)
# =============================================================================
# East wall of primary suite (bed + bath) at x=30
interior_wall(ax, 30, 18, 30, 38)
# Top wall of suite / bottom of living at y=38
interior_wall(ax, 12, 38, 30, 38)
# Bath/bedroom split at x=22
interior_wall(ax, 22, 18, 22, 30)
# WIC south wall
interior_wall(ax, 22, 30, 30, 30)
# Living east wall extends up
interior_wall(ax, 30, 38, 30, 50)
# Kitchen south wall
interior_wall(ax, 30, 42, 55, 42)
# Pantry west wall
interior_wall(ax, 48, 42, 48, 50)
# Foyer/stair row north wall
interior_wall(ax, 30, 25, 55, 25)
# Stair walls
interior_wall(ax, 38, 18, 38, 25)
interior_wall(ax, 48, 18, 48, 25)
# Powder/mud split
interior_wall(ax, 52, 18, 52, 25)

# =============================================================================
# DOOR OPENINGS with swing arcs
# =============================================================================
door_arc(ax, 28.5, 30, radius=1.9, start_deg=180, extent=90)   # WIC -> BR
door_arc(ax, 30.5, 22, radius=2.0, start_deg=90, extent=90)    # BR -> foyer
door_arc(ax, 30.5, 42, radius=2.0, start_deg=90, extent=90)    # living -> kitchen
door_arc(ax, 49.5, 44, radius=1.6, start_deg=90, extent=90)    # pantry
door_arc(ax, 48.5, 22, radius=1.4, start_deg=90, extent=90)    # powder
door_arc(ax, 52.5, 22, radius=1.4, start_deg=90, extent=90)    # mud
door_arc(ax, 54.5, 22.5, radius=1.6, start_deg=0, extent=90)   # mud -> garage
door_arc(ax, 34, 18.5, radius=2.4, start_deg=90, extent=90)    # front door
door_arc(ax, 21.5, 32, radius=1.6, start_deg=270, extent=90)   # bath door
door_arc(ax, 21.5, 24, radius=1.7, start_deg=90, extent=90)    # bed -> bath

# =============================================================================
# FIXTURES
# =============================================================================
# PRIMARY BATH (12..22 x 30..38)
tub(ax, 12.6, 30.6, w=3.5, h=2.2)                # tub at south
shower(ax, 17.8, 34.2, w=3.6, h=3.4, label='TILE\nSHOWER')
vanity(ax, 12.4, 36.4, w=5.0, h=1.4)             # double vanity
sink_oval(ax, 13.8, 37.1); sink_oval(ax, 15.9, 37.1)
toilet(ax, 16.5, 32.8)

# PRIMARY BEDROOM (12..30 x 18..30) - just note the bed area
# Small closet inside bedroom
thin_line(ax, 12.4, 20.4, 15, 20.4, lw=0.35)     # closet shelf
thin_line(ax, 15, 20.4, 15, 18.4, lw=0.35)

# WIC (22..30 x 30..38) - shelves
for cy in [30.6, 32.0, 33.4, 34.8, 36.2, 37.4]:
    thin_line(ax, 22.4, cy, 29.6, cy, lw=0.3)

# FORMAL LIVING (12..30 x 38..50) - fireplace on north wall
ax.add_patch(Rectangle((19.5, 49.2), 3.5, 0.7, fill=False,
                       edgecolor='#000', linewidth=0.4))
ax.text(21.25, 49.55, 'FP', ha='center', va='center', fontsize=3.5,
        color='#000')

# KITCHEN (30..48 x 42..50)
kitchen_island(ax, 33.5, 44.6, w=11, h=2.2)
sink_oval(ax, 39, 45.7, w=1.4, h=0.8)             # island prep sink
# Counter along north wall + main sink
thin_line(ax, 30.5, 49.4, 47.5, 49.4, lw=0.45)
sink_oval(ax, 34.5, 49.0, w=1.5, h=0.9)          # main sink
sink_oval(ax, 34.5, 49.0, w=0.75, h=0.45)        # inner bowl
fridge(ax, 45, 47.2, w=2.4, h=2.2)
stove(ax, 41, 48.5)
# Range hood note
ax.text(41, 49.9, "36\" HOOD", ha='center', va='center',
        fontsize=3, color='#000')

# PANTRY (48..55 x 42..50) - shelves
for cy in [43, 44, 45, 46, 47, 48, 49]:
    thin_line(ax, 48.5, cy, 54.6, cy, lw=0.28)

# GREAT ROOM (30..48 x 25..42) - keep empty (2-story vaulted)
# Small fireplace + hearth on east wall of great room? Skip.
# (skipped extra dimension note to avoid collision)

# FOYER (30..38 x 18..25)
# STAIR (38..48 x 18..25) - vertical stair, 12 treads
stair(ax, 38.3, 18.3, 47.7, 24.7, treads=12, direction='up')
# UP arrow + label
ax.annotate('', xy=(43, 24.5), xytext=(43, 19.5),
            arrowprops=dict(arrowstyle='->', color='#000', lw=0.5))
ax.text(43.5, 22, 'UP', fontsize=4, color='#000', ha='left', va='center')

# POWDER (48..52 x 18..25)
toilet(ax, 50, 21)
sink_oval(ax, 50, 23, w=1.2, h=0.7)

# MUD (52..55 x 18..25) - bench
thin_line(ax, 52.3, 24.3, 54.7, 24.3, lw=0.4)   # bench
thin_line(ax, 52.3, 24.3, 52.3, 23.5, lw=0.3)
thin_line(ax, 54.7, 24.3, 54.7, 23.5, lw=0.3)

# GARAGE (55..82 x 20..46) - 3 garage door openings on south face
for gx in [58, 66, 74]:
    thin_line(ax, gx, 20, gx + 6, 20, lw=0.9)
    # Door section swing (dashed rectangle above opening)
    thin_line(ax, gx, 20, gx, 22.5, lw=0.3, dashed=True)
    thin_line(ax, gx + 6, 20, gx + 6, 22.5, lw=0.3, dashed=True)
    thin_line(ax, gx, 22.5, gx + 6, 22.5, lw=0.3, dashed=True)

# =============================================================================
# EXTERIOR ELEMENTS (dashed - outside envelope)
# =============================================================================
# PORCH below foyer (30..48 x 15..18)
for x1, y1, x2, y2 in [(30, 15, 48, 18)]:
    thin_line(ax, x1, y1, x1, y2, lw=0.5, dashed=True)
    thin_line(ax, x1, y1, x2, y1, lw=0.5, dashed=True)
    thin_line(ax, x2, y1, x2, y2, lw=0.5, dashed=True)
ax.text(39, 16.3, 'PORCH', ha='center', va='center',
        fontsize=4.4, color='#000')
# Porch columns
for cx in [32.5, 39, 45.5]:
    ax.add_patch(Rectangle((cx - 0.5, 14.6), 1.0, 1.0, fill=False,
                           edgecolor='#000', linewidth=0.35))

# COVERED PATIO behind main mass (15..55 x 50..56)
for x1, y1, x2, y2 in [(15, 50, 55, 56)]:
    thin_line(ax, x1, y1, x1, y2, lw=0.5, dashed=True)
    thin_line(ax, x1, y2, x2, y2, lw=0.5, dashed=True)
    thin_line(ax, x2, y1, x2, y2, lw=0.5, dashed=True)
ax.text(35, 53, 'COVERED PATIO   12\'-3 1/2" CLG. HT.',
        ha='center', va='center', fontsize=4, color='#000')

# =============================================================================
# ROOM LABELS - tiny, with ceiling heights (Prechtel style)
# =============================================================================
def rm(x, y, name, ceil='10\'-0" CLG. HT.', size=5.5):
    ax.text(x, y + 0.5, name, ha='center', va='center',
            fontsize=size, color='#000')
    if ceil:
        ax.text(x, y - 0.35, ceil, ha='center', va='center',
                fontsize=3.4, color='#000')

# Layout (bottom-to-top left column):
#   Primary Bedroom (bottom-left)   -> 18..30 y
#   Primary Bath  (mid-left)        -> 30..38 y
#   WIC          (mid, next to bath)-> 30..38 y
#   Den/Study    (top-left)         -> 38..50 y
rm(20, 23, 'PRIMARY BED RM', size=4.8)
rm(17, 34, 'BATH 1', size=4.4)
rm(26, 34, 'W.I.C.', size=4.2, ceil='10\'-0" CLG. HT.')
rm(20, 44, 'DEN/ STUDY', size=4.8)
rm(39, 46.5, 'KITCHEN', size=4.8)
rm(51.5, 46, 'PANTRY', size=3.8, ceil='')
rm(39, 33, 'GREAT RM.', size=6, ceil='12\'-0" CLG. HT.')
rm(34, 21.5, 'FOYER', size=4.2, ceil='')
rm(43, 22.2, 'STAIR', size=3.8, ceil='SEE STAIR PLAN\nSHEET A2.2')
rm(50, 21.5, 'PWDR', size=3.6, ceil='')
rm(53.5, 21.5, 'MUD', size=3.6, ceil='')
rm(68.5, 33, '3-CAR GARAGE', size=5.6, ceil='10\'-3 1/2" CLG. HT.')

# =============================================================================
# DIMENSION CHAINS - dense, all 4 sides
# =============================================================================
# TOP chain (segmented + overall)
Y_SEG = 51.5
Y_OVER = 54
E_TOP = 50.4
# Segments
dim_h(ax, 12, 24, Y_SEG, "12'-0\"", ext_from_y=E_TOP)
dim_h(ax, 24, 36, Y_SEG, "12'-0\"", ext_from_y=E_TOP)
dim_h(ax, 36, 48, Y_SEG, "12'-0\"", ext_from_y=E_TOP)
dim_h(ax, 48, 55, Y_SEG, "7'-0\"", ext_from_y=E_TOP)
dim_h(ax, 55, 82, Y_SEG, "27'-0\"", ext_from_y=46.4)
# Overall
dim_h(ax, 12, 82, Y_OVER, "70'-0\"", ext_from_y=Y_SEG + 0.4,
      tick_len=0.7)

# BOTTOM chain (below porch)
Y_BOT = 12
E_BOT = 14.4
dim_h(ax, 30, 48, Y_BOT, "18'-0\"", ext_from_y=E_BOT)
dim_h(ax, 55, 82, Y_BOT, "27'-0\"", ext_from_y=20)

# LEFT chain
X_LEFT = 8.5
E_LEFT = 11.6
dim_v(ax, 18, 30, X_LEFT, "12'-0\"", ext_from_x=E_LEFT)
dim_v(ax, 30, 38, X_LEFT, "8'-0\"",  ext_from_x=E_LEFT)
dim_v(ax, 38, 50, X_LEFT, "12'-0\"", ext_from_x=E_LEFT)
dim_v(ax, 18, 50, X_LEFT - 2.5, "32'-0\"", ext_from_x=X_LEFT + 0.3,
      tick_len=0.7)

# RIGHT chain
X_RIGHT = 85.5
E_RIGHT = 82.4
dim_v(ax, 20, 33, X_RIGHT, "13'-0\"", ext_from_x=E_RIGHT)
dim_v(ax, 33, 46, X_RIGHT, "13'-0\"", ext_from_x=E_RIGHT)
dim_v(ax, 20, 46, X_RIGHT + 2.5, "26'-0\"", ext_from_x=X_RIGHT + 0.3,
      tick_len=0.7)

# =============================================================================
# ELEVATION MARKERS (small chevrons on each side, like Prechtel)
# =============================================================================
elevation_marker(ax, 47, 13.5, label='FRONT ELEV.')
elevation_marker(ax, 32, 57.5, label='REAR ELEV.')
ax.plot([5.5, 4.8, 5.5], [33.5, 34, 34.5], color='#000', linewidth=0.5)
ax.text(6.3, 34, 'LEFT\nSIDE ELEV.', ha='left', va='center',
        fontsize=3.5, color='#000')
ax.plot([94, 94.7, 94], [33.5, 34, 34.5], color='#000', linewidth=0.5)
ax.text(93.2, 34, 'RIGHT\nSIDE ELEV.', ha='right', va='center',
        fontsize=3.5, color='#000')

# =============================================================================
# PLAN TITLE at bottom (Prechtel style)
# =============================================================================
plan_title(ax, 65, 8, 'FLOOR  PLAN')

# --- Save --------------------------------------------------------------------
out = '/home/user/workspace/trulife-app/design/architecture/kenwood/plans/kenwood-1st-level.jpg'
plt.savefig(out, dpi=200, bbox_inches='tight', facecolor='#ffffff',
            pad_inches=0.15)
print(f'Wrote {out}')
plt.close()
