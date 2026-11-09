"""Personal space: where a penguin stands relative to everyone else in its room.

Pure functions over stage coordinates; nothing here sends packets or sleeps.
Callers pass only rectangles they already trust (measured floors, or the
labelled approximation when it is enabled), so spacing never widens where a
bot may walk. It only chooses *which* trusted point to use:

- a random free spot (no other penguin closer than ``personal`` px), so a
  crowd spreads out without pushing everyone to the walls;
- the roomiest candidate when nothing is free (a small floor, a busy room);
- a spot at talking distance around someone, nearest to where the bot is now,
  so a conversation looks like a loose ring rather than a stack.
"""
import math

# Two penguin sprites visibly overlap below roughly this distance on the 760x480 stage.
OVERLAP_PX = 45
# Talking distance (px) from the person a bot is chatting with.
RING = (60.0, 90.0)
# How far (px) from its arrival point a bot looks for a free spot.
ARRIVAL_RADIUS = 120
CANDIDATES = 16


def nearest(point, others):
    """Distance from ``point`` to the closest of ``others`` (inf when alone)."""
    return min((math.dist(point, other) for other in others), default=math.inf)


def is_clear(point, others, personal):
    return personal <= 0 or nearest(point, others) >= personal


def can_stay(point, others, personal):
    """A body already standing here keeps its spot unless it overlaps someone.

    New spots aim for ``personal``; staying only needs no overlap. The gap between
    the two keeps a full small floor from reshuffling at every step.
    """
    return is_clear(point, others, min(personal, OVERLAP_PX))


def _weighted(rects, rng):
    """One rectangle, chosen in proportion to its area (like Room.walk_point)."""
    pick = rng.randrange(sum(rect.size() for rect in rects))
    for rect in rects:
        if pick < rect.size():
            return rect
        pick -= rect.size()
    return rects[-1]  # pragma: no cover (unreachable)


def _near_point(rect, near, radius, rng):
    """Uniform point in ``rect`` within the square of ``radius`` around ``near``, or None."""
    min_x, max_x = max(rect.min_x, int(near[0] - radius)), min(rect.max_x, int(near[0] + radius))
    min_y, max_y = max(rect.min_y, int(near[1] - radius)), min(rect.max_y, int(near[1] + radius))
    if min_x > max_x or min_y > max_y:
        return None
    return rng.randint(min_x, max_x), rng.randint(min_y, max_y)


def pick_spot(rects, others, rng, *, personal, near=None, radius=None, candidates=CANDIDATES):
    """A random free point of ``rects``, optionally within ``radius`` of ``near``.

    Returns the roomiest candidate when none is free, and None when ``rects``
    offers no candidate at all (empty, or nothing within ``radius``).
    """
    rects = list(rects)
    if not rects:
        return None

    def sample(count):
        points = []
        for _ in range(count):
            rect = _weighted(rects, rng)
            point = rect.point(rng) if near is None or not radius else _near_point(rect, near, radius, rng)
            if point is not None:
                points.append(point)
        return points

    pool = sample(candidates)
    free = [point for point in pool if is_clear(point, others, personal)]
    if free:
        return free[0]  # the pool is already random
    pool += sample(2 * candidates)  # crowded: look harder for the roomiest point
    if not pool:
        return None
    return max(pool, key=lambda point: nearest(point, others))


def ring_spot(center, rects, others, rng, *, personal, origin=None, radius=RING, steps=12):
    """A free point at talking distance from ``center``, inside ``rects``.

    Of the free points, the one closest to ``origin`` (the bot's own position),
    so it walks to the near side of the group. None when no point fits.
    """
    rects = list(rects)
    if not rects:
        return None
    offset = rng.uniform(0, 2 * math.pi)
    found = []
    for step in range(steps):
        angle = offset + 2 * math.pi * step / steps
        distance = rng.uniform(*radius)
        point = (int(round(center[0] + distance * math.cos(angle))),
                 int(round(center[1] + distance * math.sin(angle))))
        if any(rect.contains(*point) for rect in rects) and is_clear(point, others, personal):
            found.append(point)
    if not found:
        return None
    if origin is None:
        return found[0]
    return min(found, key=lambda point: math.dist(point, origin))


def overlap_pairs(points, fixed=(), threshold=OVERLAP_PX):
    """Pairs closer than ``threshold``: among ``points``, and between ``points`` and ``fixed``."""
    points, fixed = list(points), list(fixed)
    count = 0
    for index, point in enumerate(points):
        count += sum(1 for other in points[index + 1:] if math.dist(point, other) < threshold)
        count += sum(1 for other in fixed if math.dist(point, other) < threshold)
    return count
