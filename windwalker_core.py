"""
WindWalker NYC â Core Engine v2
================================
The formula:
  wind_score = wind_speed(t) Ã |cos(street_bearing â wind_direction(t))| Ã canyon_factor

Where:
  wind_speed(t)     = forecast wind for the EXACT hour you plan to walk
  wind_direction(t) = forecast direction for that same hour
  canyon_factor     = 1 + (avg_building_height / street_width) Ã presence_density
                      computed from NYC PLUTO building heights (most accurate source)

Data sources:
  1. Open-Meteo   â hourly wind forecast (speed + direction) for today & tomorrow
  2. Overpass API â walkable street network (OpenStreetMap)
  3. NYC PLUTO    â real building heights per block (via NYC Open Data Socrata API)
     Fallback:    â OSM building tags if PLUTO unavailable
"""

import math, json, heapq, time, requests
from collections import defaultdict
from datetime import datetime, timezone

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CONSTANTS
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WEATHER_URL   = "https://api.open-meteo.com/v1/forecast"
PLUTO_URL     = "https://data.cityofnewyork.us/resource/64uk-42ks.json"   # NYC PLUTO via Socrata

HEADERS          = {"User-Agent": "WindWalkerNYC/2.0 (giliicekson1@gmail.com)"}
STREET_WIDTH_M   = 18.0   # average NYC street width
WIND_NORM        = 25.0   # normalisation for route weighting
CANYON_RADIUS_M  = 35.0   # metres around edge midpoint to search for buildings
BBOX_PADDING_DEG = 0.016  # ~1.5 km padding


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 1 â WIND FORECAST  (Open-Meteo, free, no key)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def fetch_hourly_wind(lat: float, lon: float) -> dict:
    """
    Returns dict with:
      "current"  : {"speed": float mph, "direction": float deg, "time": str}
      "forecast" : [{"hour": "08:00", "speed": float, "direction": float}, ...]
                   â every hour from now through next 24h
    """
    r = requests.get(WEATHER_URL, params={
        "latitude":          lat,
        "longitude":         lon,
        "current_weather":   "true",
        "hourly":            "windspeed_10m,winddirection_10m,windgusts_10m",
        "wind_speed_unit":   "mph",
        "forecast_days":     2,
        "timezone":          "America/New_York",
    }, timeout=15)
    r.raise_for_status()
    data = r.json()

    cw = data["current_weather"]
    current = {
        "speed":     float(cw["windspeed"]),
        "direction": float(cw["winddirection"]),
        "gusts":     None,           # not in current_weather block
        "time":      cw["time"],
        "label":     "Right now",
    }

    # Build hourly forecast list
    hourly    = data["hourly"]
    times     = hourly["time"]
    speeds    = hourly["windspeed_10m"]
    dirs      = hourly["winddirection_10m"]
    gusts     = hourly["windgusts_10m"]

    # Find current hour index
    now_str   = cw["time"][:13]   # "2026-04-28T14"
    now_idx   = next((i for i, t in enumerate(times) if t.startswith(now_str)), 0)

    forecast  = []
    for i in range(now_idx, min(now_idx + 25, len(times))):
        t_str = times[i]
        dt    = datetime.fromisoformat(t_str)
        label = "Now" if i == now_idx else dt.strftime("%-I%p").lower()   # e.g. "6pm"
        forecast.append({
            "index":     i,
            "time":      t_str,
            "label":     label,
            "speed":     round(float(speeds[i]), 1),
            "direction": round(float(dirs[i]),   1),
            "gusts":     round(float(gusts[i]),  1),
        })

    return {"current": current, "forecast": forecast}


def describe_wind(speed: float, direction: float) -> dict:
    """Human-readable wind description + severity."""
    dirs = ["N","NE","E","SE","S","SW","W","NW"]
    compass = dirs[round(direction / 45) % 8]
    if speed < 8:
        sev, emoji, color = "calm",   "ð¢", "#4ade80"
    elif speed < 16:
        sev, emoji, color = "light",  "ð¡", "#fbbf24"
    elif speed < 25:
        sev, emoji, color = "moderate","ð ", "#fb923c"
    elif speed < 35:
        sev, emoji, color = "strong", "ð´", "#f87171"
    else:
        sev, emoji, color = "severe", "ð¨", "#dc2626"
    return {
        "severity": sev, "emoji": emoji, "color": color,
        "compass": compass, "speed": speed, "direction": direction,
        "description": f"{speed:.1f} mph from {compass}",
        "tunnel_risk": speed >= 16,   # below 16 mph tunnel effect is minor
    }


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 2 â BUILDING HEIGHTS  (NYC PLUTO primary, OSM fallback)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def fetch_pluto_heights(bbox: tuple) -> list:
    """
    Pull real building heights from NYC PLUTO via Socrata.
    Returns list of (lat, lon, height_m).

    Root-cause notes (all three bugs fixed here):
      1. 'heightroof' does NOT exist in dataset 64uk-42ks â use 'numfloors' instead
         and estimate height as numfloors Ã 3.5 m.
      2. 'the_geom' does NOT exist in this dataset â filter on 'latitude'/'longitude'
         which are real, filterable columns.
      3. requests.get(params={'$where': ...}) URL-encodes '$' â '%24where' which
         Socrata rejects with 400. Build the URL string manually instead.
    """
    s, w, n, e = bbox
    try:
        import urllib.parse
        # latitude/longitude are real filterable columns; build URL manually
        # so '$where' stays literal (requests.params encodes '$' â '%24')
        where = (f"latitude >= {s} AND latitude <= {n} "
                 f"AND longitude >= {w} AND longitude <= {e} "
                 f"AND numfloors IS NOT NULL AND numfloors > 0")
        url = (f"{PLUTO_URL}"
               f"?$where={urllib.parse.quote(where)}"
               f"&$select=latitude,longitude,numfloors"
               f"&$limit=5000")
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        buildings = []
        for row in r.json():
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
                h   = float(row["numfloors"]) * 3.5   # ~3.5 m per floor
                buildings.append((lat, lon, h))
            except (KeyError, ValueError, TypeError):
                continue
        print(f"       PLUTO: {len(buildings)} buildings loaded (numfloors Ã 3.5 m)")
        return buildings
    except Exception as ex:
        print(f"  â  PLUTO unavailable ({ex}), falling back to OSM heights")
        return []


def osm_building_height(tags: dict) -> float:
    """Estimate height from OSM tags when PLUTO is unavailable."""
    if "height" in tags:
        try: return float(str(tags["height"]).replace("m","").strip())
        except: pass
    if "building:levels" in tags:
        try: return float(tags["building:levels"]) * 3.5
        except: pass
    return {"apartments":25,"residential":15,"commercial":30,"office":40,
            "retail":8,"hotel":35,"yes":20}.get(tags.get("building","yes"), 20)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 3 â STREET NETWORK  (OpenStreetMap via Overpass)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def geocode(address: str) -> tuple:
    r = requests.get(NOMINATIM_URL,
                     params={"q": address, "format": "json", "limit": 1},
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    res = r.json()
    if not res: raise ValueError(f"Address not found: '{address}'")
    return float(res[0]["lat"]), float(res[0]["lon"])


def fetch_street_network(bbox: tuple) -> tuple:
    """Returns (osm_nodes, streets, osm_buildings) from Overpass."""
    s, w, n, e = bbox
    query = (f'[out:json][timeout:90];'
             f'(way[highway~"^(footway|pedestrian|path|residential|secondary|'
             f'tertiary|primary|unclassified|living_street|service)$"]({s},{w},{n},{e});'
             f'way[building]({s},{w},{n},{e}););out body;>;out skel qt;')
    r = requests.post(OVERPASS_URL, data={"data": query},
                      headers=HEADERS, timeout=120)
    r.raise_for_status()
    data = r.json()

    nodes, streets, osm_buildings = {}, [], []
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif el["type"] == "way":
            tags = el.get("tags", {})
            entry = {"nids": el["nodes"], "tags": tags}
            if "highway" in tags:   streets.append(entry)
            elif "building" in tags: osm_buildings.append(entry)
    return nodes, streets, osm_buildings


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 4 â CANYON FACTOR  (building height Ã density around each edge)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def haversine(la1, lo1, la2, lo2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2-la1), math.radians(lo2-lo1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def seg_bearing(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2-lo1)
    x = math.sin(dl)*math.cos(p2)
    y = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def build_spatial_index(building_pts: list) -> dict:
    """Grid-based spatial index: cell_key â list of (lat, lon, height)."""
    CELL = 0.001
    grid = defaultdict(list)
    for pt in building_pts:
        lat, lon = pt[0], pt[1]
        grid[(int(lat/CELL), int(lon/CELL))].append(pt)
    return grid


def nearby_building_heights(grid, mid_lat, mid_lon,
                             radius_m=CANYON_RADIUS_M) -> list:
    CELL = 0.001
    cx, cy = int(mid_lat/CELL), int(mid_lon/CELL)
    cell_r  = int(radius_m / 90) + 1
    heights = []
    for dx in range(-cell_r, cell_r+1):
        for dy in range(-cell_r, cell_r+1):
            for pt in grid[(cx+dx, cy+dy)]:
                if haversine(mid_lat, mid_lon, pt[0], pt[1]) <= radius_m:
                    heights.append(pt[2])
    return heights


def compute_canyon_index(osm_nodes: dict, graph: dict,
                          pluto_pts: list, osm_buildings: list) -> dict:
    """
    For each edge: canyon_factor = 1 + (avg_h / street_width) Ã density_factor
    Uses PLUTO heights where available, OSM fallback otherwise.
    """
    # Merge building point clouds: PLUTO first, OSM as fallback
    building_pts = list(pluto_pts)   # (lat, lon, height_m)

    if not building_pts:             # full OSM fallback
        for b in osm_buildings:
            nids = [n for n in b["nids"] if n in osm_nodes]
            if not nids: continue
            lats = [osm_nodes[n][0] for n in nids]
            lons = [osm_nodes[n][1] for n in nids]
            h    = osm_building_height(b["tags"])
            building_pts.append((sum(lats)/len(lats), sum(lons)/len(lons), h))

    grid    = build_spatial_index(building_pts)
    cidx    = {}

    for a, nbrs in graph.items():
        for b in nbrs:
            if (a, b) in cidx: continue
            la, loa = osm_nodes[a]
            lb, lob = osm_nodes[b]
            mid_lat = (la + lb) / 2
            mid_lon = (loa + lob) / 2

            heights = nearby_building_heights(grid, mid_lat, mid_lon)
            if heights:
                avg_h    = sum(heights) / len(heights)
                ratio    = min(avg_h / STREET_WIDTH_M, 4.0)   # cap at 4Ã street width
                density  = min(len(heights) / 4.0, 1.0)       # normalise presence
                factor   = 1.0 + ratio * density * 0.85
            else:
                factor   = 1.0

            cidx[(a, b)] = factor
            cidx[(b, a)] = factor

    return cidx


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 5 â WIND SCORING & ROUTING
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def wind_alignment(brg: float, wind_dir: float) -> float:
    """
    |cos(Îangle)| â 1.0 = street parallel to wind (tunnel), 0.0 = perpendicular (shelter).
    Works for both travel directions on the same street.
    """
    diff = abs(brg - wind_dir) % 180
    return abs(math.cos(math.radians(diff)))


def build_graph(osm_nodes: dict, streets: list) -> dict:
    g = defaultdict(dict)
    for way in streets:
        nids = [n for n in way["nids"] if n in osm_nodes]
        for i in range(len(nids) - 1):
            a, b    = nids[i], nids[i+1]
            la, loa = osm_nodes[a]
            lb, lob = osm_nodes[b]
            dist    = haversine(la, loa, lb, lob)
            brg     = seg_bearing(la, loa, lb, lob)
            g[a][b] = {"length": dist, "bearing": brg}
            g[b][a] = {"length": dist, "bearing": (brg+180)%360}
    return g


def score_all_edges(graph: dict, wind_speed: float, wind_dir: float,
                    canyon_idx: dict) -> dict:
    """
    Attach wind_score and wind_weight to every edge.
      wind_score  = raw exposure intensity (for visualisation)
      wind_weight = routing cost = length Ã (1 + wind_score / WIND_NORM)
    """
    wg = {}
    for a, nbrs in graph.items():
        wg[a] = {}
        for b, data in nbrs.items():
            align  = wind_alignment(data["bearing"], wind_dir)
            canyon = canyon_idx.get((a, b), 1.0)
            score  = wind_speed * align * canyon
            wg[a][b] = {
                **data,
                "wind_score":  round(score, 2),
                "wind_weight": data["length"] * (1.0 + score / WIND_NORM),
                "canyon":      round(canyon, 2),
                "alignment":   round(align,  3),
            }
    return wg


def nearest_node(osm_nodes: dict, graph: dict, lat: float, lon: float) -> int:
    return min(
        (n for n in graph if n in osm_nodes),
        key=lambda n: haversine(lat, lon, osm_nodes[n][0], osm_nodes[n][1])
    )


def dijkstra(graph: dict, start: int, end: int, weight: str = "length") -> list:
    dist  = {start: 0.0}
    prev  = {}
    pq    = [(0.0, start)]
    seen  = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen: continue
        seen.add(u)
        if u == end: break
        for v, ed in graph.get(u, {}).items():
            nd = d + ed[weight]
            if nd < dist.get(v, float("inf")):
                dist[v] = nd; prev[v] = u
                heapq.heappush(pq, (nd, v))
    if end not in prev and end != start: return []
    path, node = [], end
    while node in prev: path.append(node); node = prev[node]
    path.append(start)
    return path[::-1]


def route_stats(graph: dict, osm_nodes: dict, route: list) -> dict:
    total_len = total_score = 0.0
    n = 0
    coords = []
    for i, node in enumerate(route):
        coords.append(list(osm_nodes[node]))
        if i > 0:
            e = graph.get(route[i-1], {}).get(node, {})
            total_len   += e.get("length", 0)
            total_score += e.get("wind_score", 0)
            n += 1
    return {
        "length_m":       round(total_len),
        "length_min":     max(1, round(total_len / 83)),
        "avg_wind_score": round(total_score / n, 1) if n else 0.0,
        "coords":         coords,
    }


def edge_visualisation_data(graph: dict, osm_nodes: dict) -> list:
    """All edges with score + bearing, deduplicated, for map heat map."""
    seen, out = set(), []
    for a, nbrs in graph.items():
        for b, ed in nbrs.items():
            key = (min(a,b), max(a,b))
            if key in seen: continue
            seen.add(key)
            out.append({
                "coords":  [list(osm_nodes[a]), list(osm_nodes[b])],
                "score":   ed["wind_score"],
                "canyon":  ed["canyon"],
                "bearing": round(ed["bearing"], 1),
            })
    return out


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MODULE 6 â MAIN PIPELINE
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def run(orig_address: str, dest_address: str,
        forecast_hour_index: int = 0,
        progress_cb=None) -> dict:
    """
    Full WindWalker pipeline.

    Args:
        orig_address:        start address (any format Nominatim understands)
        dest_address:        end address
        forecast_hour_index: 0 = right now, 1 = next hour, etc. (from forecast list)
        progress_cb:         optional callable(pct: int, msg: str) for UI updates

    Returns dict with all results needed by the UI.
    """
    def progress(pct, msg):
        print(f"  [{pct:3d}%] {msg}")
        if progress_cb: progress_cb(pct, msg)

    progress(5,  "ð Geocoding addresses...")
    orig_ll = geocode(orig_address)
    time.sleep(1.1)
    dest_ll = geocode(dest_address)
    center  = ((orig_ll[0]+dest_ll[0])/2, (orig_ll[1]+dest_ll[1])/2)

    progress(18, "ð¬ Fetching hourly wind forecast...")
    wind_data = fetch_hourly_wind(*center)
    selected  = wind_data["forecast"][forecast_hour_index]
    wind_speed   = selected["speed"]
    wind_dir     = selected["direction"]
    wind_gusts   = selected["gusts"]
    wind_time    = selected["label"]
    wind_info    = describe_wind(wind_speed, wind_dir)

    print(f"       Wind at {wind_time}: {wind_speed} mph from {wind_info['compass']} "
          f"(gusts {wind_gusts} mph)")

    lats = [orig_ll[0], dest_ll[0]]
    lons = [orig_ll[1], dest_ll[1]]
    bbox = (min(lats)-BBOX_PADDING_DEG, min(lons)-BBOX_PADDING_DEG,
            max(lats)+BBOX_PADDING_DEG, max(lons)+BBOX_PADDING_DEG)

    progress(30, "ð Fetching NYC building heights (PLUTO)...")
    pluto_pts = fetch_pluto_heights(bbox)
    print(f"       PLUTO: {len(pluto_pts)} buildings with exact heights")

    progress(45, "ðº Fetching street network (OpenStreetMap)...")
    osm_nodes, streets, osm_buildings = fetch_street_network(bbox)
    print(f"       OSM: {len(osm_nodes):,} nodes Â· {len(streets):,} streets Â· {len(osm_buildings):,} buildings")

    progress(58, "ð Building street graph...")
    graph = build_graph(osm_nodes, streets)

    progress(68, "ð¢ Computing canyon factors from building heights...")
    canyon_idx = compute_canyon_index(osm_nodes, graph, pluto_pts, osm_buildings)

    progress(80, f"ð Scoring every street for wind at {wind_time}...")
    wgraph = score_all_edges(graph, wind_speed, wind_dir, canyon_idx)

    progress(90, "ð Computing shortest and wind-sheltered routes...")
    orig_node = nearest_node(osm_nodes, wgraph, orig_ll[0], orig_ll[1])
    dest_node = nearest_node(osm_nodes, wgraph, dest_ll[0], dest_ll[1])

    short_route = dijkstra(wgraph, orig_node, dest_node, "length")
    wind_route  = dijkstra(wgraph, orig_node, dest_node, "wind_weight")

    if not short_route:
        raise ValueError("No walkable route found. Try addresses on named streets.")

    short_s = route_stats(wgraph, osm_nodes, short_route)
    wind_s  = route_stats(wgraph, osm_nodes, wind_route if wind_route else short_route)

    reduction = 0.0
    if short_s["avg_wind_score"] > 0:
        reduction = (1 - wind_s["avg_wind_score"] / short_s["avg_wind_score"]) * 100

    progress(100, "â Done!")

    return {
        # Addresses
        "orig_address": orig_address,
        "dest_address": dest_address,
        "orig_ll":      orig_ll,
        "dest_ll":      dest_ll,
        # Wind
        "wind":         wind_info,
        "wind_time":    wind_time,
        "wind_gusts":   wind_gusts,
        "wind_forecast":wind_data["forecast"],
        # Routes
        "short_s":      short_s,
        "wind_s":       wind_s,
        "reduction":    round(reduction, 1),
        "same_route":   short_s["coords"] == wind_s["coords"],
        # Visualisation
        "edge_data":    edge_visualisation_data(wgraph, osm_nodes),
        # Metadata
        "n_buildings":  len(pluto_pts) or len(osm_buildings),
        "n_edges":      sum(len(v) for v in wgraph.values()),
        "used_pluto":   len(pluto_pts) > 0,
    }


# âââ Quick CLI test (only runs when executed directly, not when imported/pasted) â
if __name__ == "__main__":
    result = run(
        "Penn Station, New York, NY",
        "Grand Central Terminal, New York, NY",
        forecast_hour_index=0
    )
    w = result["wind"]
    print(f"\n{'â'*55}")
    print(f"  Wind at {result['wind_time']}: {w['description']} (gusts {result['wind_gusts']} mph)")
    print(f"  Buildings:  {result['n_buildings']} ({'PLUTO' if result['used_pluto'] else 'OSM fallback'})")
    print(f"  Edges:      {result['n_edges']:,}")
    print(f"\n  ð Shortest:  {result['short_s']['length_m']}m Â· {result['short_s']['length_min']} min Â· wind {result['short_s']['avg_wind_score']}")
    print(f"  ð¡  Sheltered: {result['wind_s']['length_m']}m Â· {result['wind_s']['length_min']} min Â· wind {result['wind_s']['avg_wind_score']}")
    print(f"  ð Wind reduction: {result['reduction']}%")
    print(f"{'â'*55}")
