"""Camera geometry and simple vision for the bridge (Python 2.7, PIL + numpy, no NAOqi calls).

Everything assumes a flat floor and one pinhole camera per frame, placed by its pose in
FRAME_ROBOT (x forward, y left, z up; roll, pitch, yaw with positive pitch looking down), which
ALMotion.getPosition("CameraTop"/"CameraBottom", FRAME_ROBOT, True) returns. The head yaw is part
of that pose, so frames taken while looking around need no extra bookkeeping.

- ground_point(): where a pixel's ray meets the floor, in robot coordinates
- floor_model(): the floor's colour, learnt from the ground right in front of the feet
- free_space(): per image column, how far the floor runs before something stands on it
- find_color(): the largest blob of a named colour, with its bearing and ground distance
- clearance(): free-space samples from several frames binned by bearing

Nothing here knows about any particular world: the floor colour is measured every time.
"""
from __future__ import division

import math

import numpy as np

HFOV = 1.064          # rad, NAO V5 cameras (Webots Nao.proto uses the same)
MAX_RANGE = 4.0       # m; floor seen further away than this is reported as "open"
RUN = 3               # consecutive non-floor pixels that make an obstacle edge (JPEG noise, grid lines)
COLUMN_STEP = 4       # px between sampled columns (320 px -> 80 samples per frame)
BODY_RADIUS = 0.2     # m; ground points closer than this to the robot origin are its own feet

# HSV ranges: hue in degrees, min saturation, min value (0..1)
COLORS = {
    "red": ((345, 15), 0.45, 0.2),
    "orange": ((15, 40), 0.5, 0.3),
    "yellow": ((40, 70), 0.45, 0.3),
    "green": ((70, 170), 0.35, 0.2),
    "blue": ((190, 260), 0.35, 0.2),
    "purple": ((260, 320), 0.3, 0.2),
}


def focal(width):
    return (width / 2.0) / math.tan(HFOV / 2)


def rotation(roll, pitch, yaw):
    """R = Rz(yaw) Ry(pitch) Rx(roll), NAOqi's convention for 6D positions."""
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), \
        math.cos(yaw), math.sin(yaw)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def ground_point(u, v, size, pose):
    """(x, y) on the floor, in m in FRAME_ROBOT, seen at pixel column u, row v; None above the horizon."""
    width, height = size
    f = focal(width)
    ray = rotation(pose[3], pose[4], pose[5]).dot([1.0, -(u - width / 2.0) / f, -(v - height / 2.0) / f])
    if ray[2] >= -1e-6:
        return None
    t = -pose[2] / ray[2]
    return pose[0] + t * ray[0], pose[1] + t * ray[1]


def grid_row(distance, size, pose):
    """Image row of the floor `distance` m ahead of the camera, straight ahead (for the yellow lines)."""
    width, height = size
    return height / 2.0 + focal(width) * math.tan(math.atan2(pose[2], distance) - pose[4])


def to_array(image):
    return np.asarray(image.convert("RGB"), dtype=np.int16)


def floor_model(bottom_image, rows=24):
    """(colour, tolerance) of the floor from the lowest rows of the bottom camera, i.e. the ground
    about 0.25 m in front of the feet. Returns None if that patch isn't one uniform surface
    (the robot is right at an obstacle, or the floor is patterned): free_space then can't work."""
    pixels = to_array(bottom_image)[-rows:, bottom_image.size[0] // 5:-bottom_image.size[0] // 5]
    pixels = pixels.reshape(-1, 3)
    colour = np.median(pixels, axis=0)
    spread = np.percentile(np.abs(pixels - colour).max(axis=1), 90)
    if spread > 40:
        return None
    return colour, max(20.0, 3.0 * spread)


def above_horizon(image, pose, floor, gap=5):
    """Fraction of the image above the horizon that has the floor's colour, or None when the camera
    looks too far down to see the horizon. Real floor never shows up there; a wall right in front of
    the robot, whose colour the bottom camera took for the floor, fills it."""
    width, height = image.size
    row = int(height / 2.0 - focal(width) * math.tan(pose[4])) - gap
    if row < 10:
        return None
    return float(floor_mask(image, floor)[:min(row, height)].mean())


def floor_mask(image, floor):
    colour, tolerance = floor
    return np.abs(to_array(image) - colour).max(axis=2) <= tolerance


def free_space(image, pose, floor, step=COLUMN_STEP, edge=0.0):
    """For every `step`-th column: (x, y, kind), the floor point in m in FRAME_ROBOT, where kind is
      "obstacle"  something stands on the floor at that distance,
      "near"      the column starts off-floor: an obstacle closer than the nearest visible ground
                  (distance = that ground's distance, an upper bound),
      "open"      floor up to the horizon or MAX_RANGE (distance = how far it was seen).
    edge: fraction of the width to skip on each side."""
    mask = floor_mask(image, floor)
    height, width = mask.shape
    skip = int(edge * width)
    samples = []
    for u in range(skip + step // 2, width - skip, step):
        column = mask[:, u]
        v = height - 1
        misses = 0
        while v >= 0:
            misses = 0 if column[v] else misses + 1
            if misses >= RUN:
                break
            v -= 1
        edge = v + RUN  # the last floor pixel below the obstacle's run of RUN pixels
        if v < 0:
            point, kind = ground_point(u, 0, image.size, pose), "open"
        elif edge >= height:
            point, kind = ground_point(u, height - 1, image.size, pose), "near"
        else:
            point, kind = ground_point(u, edge, image.size, pose), "obstacle"
        if point is None:  # the edge is above the horizon: floor as far as we can tell
            point, kind = _horizon_point(u, image.size, pose), "open"
            if point is None:
                continue
        distance = math.hypot(*point)
        if distance < BODY_RADIUS:
            continue
        if distance > MAX_RANGE:
            point = (point[0] * MAX_RANGE / distance, point[1] * MAX_RANGE / distance)
            distance, kind = MAX_RANGE, ("open" if kind == "obstacle" else kind)
        samples.append((point[0], point[1], kind))
    return samples


def _horizon_point(u, size, pose):
    """The farthest floor point below the horizon in column u (for columns that are floor to the top)."""
    for v in range(size[1] - 1, -1, -1):
        point = ground_point(u, v, size, pose)
        if point is None or math.hypot(*point) > MAX_RANGE:
            return ground_point(u, min(v + 1, size[1] - 1), size, pose)
    return ground_point(u, 0, size, pose)


def _free(entries):
    """(free m, blocked, upper_bound) for (camera, distance, kind) samples of one direction: the nearest
    obstacle, else the farthest floor seen; None when there are no samples. A "near" from the top
    camera only counts when the bottom camera didn't look there, and then only says "closer than
    this" (upper_bound True)."""
    if not entries:
        return None
    has_bottom = any(camera == "bottom" for camera, _, _ in entries)
    blocking = [(d, kind == "near" and camera == "top") for camera, d, kind in entries
                if kind == "obstacle" or (kind == "near" and (camera == "bottom" or not has_bottom))]
    if blocking:
        free, bound = min(blocking)
        return free, True, bound
    return max(d for _, d, _ in entries), False, False


def clearance(samples, bin_deg=10):
    """Bin (camera, x, y, kind) samples from any number of frames by bearing. Returns a list, left
    to right, of {"bearing_deg", "free_m", "blocked"} (blocked False: floor seen that far, maybe
    more), plus "upper_bound": True when something is somewhere closer than free_m."""
    bins = {}
    for camera, x, y, kind in samples:
        key = int(round(math.degrees(math.atan2(y, x)) / bin_deg))
        bins.setdefault(key, []).append((camera, math.hypot(x, y), kind))
    table = []
    for key in sorted(bins, reverse=True):
        free, blocked, bound = _free(bins[key])
        entry = {"bearing_deg": key * bin_deg, "free_m": round(free, 2), "blocked": blocked}
        if bound:
            entry["upper_bound"] = True
        table.append(entry)
    return table


def lane(samples, half_width):
    """(free m, blocked, upper_bound) straight ahead in a lane 2 * half_width wide (x forward).
    None if unseen."""
    return _free([(camera, x, kind) for camera, x, y, kind in samples if x > 0 and abs(y) <= half_width])


def best_heading(samples, half_width, want, margin, remaining, spread=0.6, step=0.087, cost=0.8):
    """Local planner: of the headings within `spread` rad of straight ahead, the one to steer to now.
    Each heading phi scores the metres it could walk down a lane 2 * half_width wide
    (min(lane free - margin, remaining)) minus `cost` m per rad it is off `want` (the heading asked
    for, relative to the body). Returns (phi, room m along phi, room m straight ahead); room None
    for a direction nobody saw."""
    best = None
    ahead = None
    n = int(round(spread / step))
    for i in range(-n, n + 1):
        phi = i * step
        c, s = math.cos(-phi), math.sin(-phi)
        rotated = [(camera, c * x - s * y, s * x + c * y, kind) for camera, x, y, kind in samples]
        free = lane(rotated, half_width)
        room = None if free is None else free[0] - margin
        if i == 0:
            ahead = room
        if room is None:
            continue
        score = min(room, remaining) - cost * abs(phi - want)
        if best is None or score > best[0]:
            best = (score, phi, room)
    return (None, None, ahead) if best is None else (best[1], best[2], ahead)


def side_gaps(samples, half_width, reach=(0.3, 1.5)):
    """Distance (m) to the nearest obstacle beside the lane on the left and on the right, looking
    reach[0]..reach[1] m ahead; None on a side with no obstacle seen there."""
    left = [y for _, x, y, kind in samples if kind != "open" and reach[0] <= x <= reach[1] and y > half_width]
    right = [-y for _, x, y, kind in samples if kind != "open" and reach[0] <= x <= reach[1] and y < -half_width]
    return (min(left) if left else None), (min(right) if right else None)


def _hsv(array):
    rgb = array.astype(np.float32) / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    top, low = rgb.max(axis=2), rgb.min(axis=2)
    delta = top - low + 1e-6
    hue = np.where(top == r, ((g - b) / delta) % 6, np.where(top == g, (b - r) / delta + 2, (r - g) / delta + 4))
    return hue * 60.0, np.where(top > 0, delta / (top + 1e-6), 0), top


def color_mask(image, name):
    (h0, h1), s_min, v_min = COLORS[name]
    hue, sat, val = _hsv(to_array(image))
    in_hue = (hue >= h0) | (hue <= h1) if h0 > h1 else (hue >= h0) & (hue <= h1)
    return in_hue & (sat >= s_min) & (val >= v_min)


def find_color(image, pose, name, min_pixels=40):
    """The pixels of colour `name` in one frame: None, or {"bearing_deg", "distance_m" (where its
    lowest pixel meets the floor; None if that's above the horizon), "pixels", "fraction", "columns",
    and "upper_bound": True when it runs off the bottom of the frame (then it's closer than that)}."""
    mask = color_mask(image, name)
    count = int(mask.sum())
    if count < min_pixels:
        return None
    columns = np.nonzero(mask.any(axis=0))[0]
    u = float(np.median(np.nonzero(mask)[1]))
    base = int(np.nonzero(mask[:, int(u)])[0].max()) if mask[:, int(u)].any() else int(np.nonzero(mask)[0].max())
    point = ground_point(u, base, image.size, pose)
    if point is not None and math.hypot(*point) < BODY_RADIUS:
        return None  # the robot's own body
    width, height = image.size
    f = focal(width)
    bearing = pose[5] + math.atan2(-(u - width / 2.0), f)
    if point is not None:
        bearing = math.atan2(point[1], point[0])
    hit = {"bearing_deg": round(math.degrees(bearing)),
           "distance_m": None if point is None else round(math.hypot(*point), 2),
           "pixels": count, "fraction": round(count / float(width * height), 3),
           "columns": [int(columns[0]), int(columns[-1])]}
    if base >= height - 1:  # runs off the bottom of the frame: its base is closer than that
        hit["upper_bound"] = True
    return hit
