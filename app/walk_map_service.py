"""Issue #6 read models: owner map, public dog map, walk history.

Routes are derived from the Issue #4 raw GPS points but never returned raw.
Performance: walks are paginated, and each page fetches only a sampled subset of every
route in ONE query (sequence_no is contiguous from 0, so sampling is a modulo filter that
uses the (walk_id, sequence_no) unique index).
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from sqlalchemy import and_, distinct, func, or_
from sqlalchemy.orm import contains_eager

from app.models import Walk, WalkPoint, WalkRequest
from app.gps_geometry import distance_m

OWNER_MAX_ROUTE_POINTS = 200   # route holds at most this +1 (the final point is always kept)
PUBLIC_MAX_ROUTE_POINTS = 100
# PROVISIONAL public-exposure policy (needs privacy review):
PUBLIC_TRIM_M = 150            # drop this much of the route at the start and at the end
PUBLIC_COORD_DECIMALS = 3      # ~110 m grid


class Point(NamedTuple):
    latitude: Decimal
    longitude: Decimal


def completed_filter(**keys):
    conditions = [Walk.status == "COMPLETED"]
    if "user_id" in keys:
        conditions.append(WalkRequest.user_id == keys["user_id"])
    if "dog_id" in keys:
        conditions.append(WalkRequest.dog_id == keys["dog_id"])
    return conditions


def walk_page(db, conditions, limit, offset, order=(Walk.started_at.desc(), Walk.id.desc())):
    total = (db.query(func.count(Walk.id)).select_from(Walk)
             .join(WalkRequest, Walk.walk_request_id == WalkRequest.id).filter(*conditions).scalar())
    walks = (db.query(Walk).join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
             .options(contains_eager(Walk.walk_request).joinedload(WalkRequest.dog))
             .filter(*conditions).order_by(*order).limit(limit).offset(offset).all())
    return total, walks


def sampled_routes(db, walk_ids, max_points):
    """walk_id -> (raw point count, sampled points). First and last points are kept."""
    if not walk_ids:
        return {}
    counts = dict(db.query(WalkPoint.walk_id, func.count(WalkPoint.id))
                  .filter(WalkPoint.walk_id.in_(walk_ids)).group_by(WalkPoint.walk_id).all())
    clauses = []
    for walk_id, count in counts.items():
        step = max(1, -(-count // max_points))
        clauses.append(and_(WalkPoint.walk_id == walk_id,
                            or_(WalkPoint.sequence_no % step == 0, WalkPoint.sequence_no == count - 1)))
    routes = {walk_id: (0, []) for walk_id in walk_ids}
    if not clauses:
        return routes
    rows = (db.query(WalkPoint.walk_id, WalkPoint.latitude, WalkPoint.longitude)
            .filter(or_(*clauses)).order_by(WalkPoint.walk_id, WalkPoint.sequence_no).all())
    sampled = {}
    for walk_id, latitude, longitude in rows:
        sampled.setdefault(walk_id, []).append(Point(Decimal(latitude), Decimal(longitude)))
    for walk_id, count in counts.items():
        routes[walk_id] = (count, sampled.get(walk_id, []))
    return routes


def trim_ends(points, trim_m):
    """Remove the first and last trim_m metres; too-short routes disappear completely."""
    start, travelled = 0, 0.0
    while start + 1 < len(points) and travelled < trim_m:
        travelled += distance_m(points[start], points[start + 1])
        start += 1
    end, travelled = len(points) - 1, 0.0
    while end > start and travelled < trim_m:
        travelled += distance_m(points[end - 1], points[end])
        end -= 1
    return points[start:end + 1] if end > start else []


def coarsen(points, decimals):
    grid = Decimal(1).scaleb(-decimals)
    result = []
    for point in points:
        rounded = Point(point.latitude.quantize(grid, rounding=ROUND_HALF_UP),
                        point.longitude.quantize(grid, rounding=ROUND_HALF_UP))
        if not result or result[-1] != rounded:
            result.append(rounded)
    return result


def public_route(points):
    if len(points) < 2:
        return []
    route = coarsen(trim_ends(points, PUBLIC_TRIM_M), PUBLIC_COORD_DECIMALS)
    anchors = (points[0], points[-1])
    safe = [all(distance_m(p, anchor) > PUBLIC_TRIM_M for anchor in anchors) for p in route]
    indices = [i for i, valid in enumerate(safe) if valid]
    if len(indices) < 2:
        return []
    start, end = indices[0], indices[-1]
    # Trim rounding spill at the edges. A revisit inside the remaining route
    # withholds the whole route, rather than joining disconnected fragments.
    if not all(safe[start:end + 1]):
        return []
    route = route[start:end + 1]
    for first, second in zip(route, route[1:]):
        length = distance_m(first, second)
        for anchor in anchors:
            # Triangle inequality gives a conservative lower bound for distance
            # from the anchor to ANY point on the connecting geodesic segment.
            clearance = (distance_m(anchor, first) + distance_m(anchor, second) - length) / 2
            if clearance <= PUBLIC_TRIM_M:
                return []
    return route


def motion_metrics(walk):
    return {name: getattr(walk, name) for name in (
        "moving_duration_seconds", "stopped_duration_seconds", "unknown_duration_seconds",
        "moving_distance_m", "average_pace")}


def user_map(db, user_id, limit, offset):
    total, walks = walk_page(db, completed_filter(user_id=user_id), limit, offset)
    routes = sampled_routes(db, [walk.id for walk in walks], OWNER_MAX_ROUTE_POINTS)
    return {
        "user_id": user_id, "total": total, "limit": limit, "offset": offset,
        "walks": [{
            "walk_id": walk.id, "dog": walk.walk_request.dog, "started_at": walk.started_at,
            "ended_at": walk.ended_at, "total_distance_m": walk.total_distance_m,
            "duration_seconds": walk.duration_seconds, "point_count": routes[walk.id][0],
            **motion_metrics(walk),
            "route": [{"latitude": p.latitude, "longitude": p.longitude} for p in routes[walk.id][1]],
        } for walk in walks],
    }


def dog_map(db, dog_id, limit, offset):
    conditions = completed_filter(dog_id=dog_id)
    count, distance, seconds, volunteers = (
        db.query(func.count(Walk.id), func.coalesce(func.sum(Walk.total_distance_m), 0),
                 func.coalesce(func.sum(Walk.duration_seconds), 0), func.count(distinct(WalkRequest.user_id)))
        .select_from(Walk).join(WalkRequest, Walk.walk_request_id == WalkRequest.id)
        .filter(*conditions).one())
    total, walks = walk_page(db, conditions, limit, offset)
    routes = sampled_routes(db, [walk.id for walk in walks], PUBLIC_MAX_ROUTE_POINTS)
    return {
        "dog_id": dog_id, "total": total, "limit": limit, "offset": offset,
        "stats": {"walk_count": int(count),
                  "total_distance_m": Decimal(str(distance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                  "total_duration_seconds": int(seconds), "volunteer_count": int(volunteers)},
        # Deliberately no walk/user ids, names or timestamps in the public view.
        "walks": [{
            "total_distance_m": walk.total_distance_m, "duration_seconds": walk.duration_seconds,
            "route": [{"latitude": p.latitude, "longitude": p.longitude}
                      for p in public_route(routes[walk.id][1])],
        } for walk in walks],
    }


def user_walks(db, user_id, status, limit, offset):
    conditions = [WalkRequest.user_id == user_id]
    if status is not None:
        conditions.append(Walk.status == status)
    total, walks = walk_page(db, conditions, limit, offset)
    return {
        "user_id": user_id, "total": total, "limit": limit, "offset": offset,
        "walks": [{
            "walk_id": walk.id, "walk_request_id": walk.walk_request_id, "dog": walk.walk_request.dog,
            "status": walk.status, "started_at": walk.started_at, "ended_at": walk.ended_at,
            "total_distance_m": walk.total_distance_m, "duration_seconds": walk.duration_seconds,
            **motion_metrics(walk), "counted_as_volunteer_record": walk.status == "COMPLETED",
        } for walk in walks],
    }
