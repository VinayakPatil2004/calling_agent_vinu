"""
REST API routes for CityLinc Transit Engine:
- Origin-Destination Route Pathfinding (/api/routes/search)
- Route Journey Geometry & Intermediate Stops (/api/routes/{route_id}/journey)
- Real-time Scheduled Bus Departures (/api/timetable/next)
- Comprehensive Route Dispatch Schedules (/api/timetable/departures)
- Multilingual Places, Hospitals & Landmark Discovery (/api/places/search)
- Place Connectivity & Nearby Transit Connections (/api/places/{place_id}/connections)
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, HTTPException, Path
from pydantic import BaseModel, Field

from backend.app.services.route_engine import route_engine, JourneySearchResult
from backend.app.services.timetable_engine import timetable_engine, TimetableResponse
from backend.app.services.location_service import location_service
from backend.app.services.citylinc_knowledge import knowledge_service

# Routers with standard path prefixes
routes_router = APIRouter(prefix="/api/routes", tags=["Route Engine"])
timetable_router = APIRouter(prefix="/api/timetable", tags=["Timetable Engine"])
places_router = APIRouter(prefix="/api/places", tags=["Places & POI"])


# -------------------------------------------------------------
# 1. Route Engine Endpoints
# -------------------------------------------------------------

@routes_router.get("/search", response_model=JourneySearchResult)
async def search_transit_routes(
    origin: str = Query(..., description="Origin stop or place name (e.g., 'Nashik Road', 'सीबीएस')"),
    destination: str = Query(..., description="Destination stop or place name (e.g., 'Panchavati', 'त्र्यंबक')"),
    limit_direct: int = Query(5, ge=1, le=20, description="Max direct routes to return"),
    limit_transfer: int = Query(3, ge=1, le=10, description="Max 1-transfer routes to return")
):
    """
    Computes direct and transfer transit journeys between origin and destination.
    Returns ordered intermediate stops, distances, timetable counts, and fare estimation.
    """
    res = route_engine.search_routes(
        origin_query=origin,
        destination_query=destination,
        limit_direct=limit_direct,
        limit_transfer=limit_transfer
    )
    return res


@routes_router.get("/{route_id}/journey")
async def get_route_journey(
    route_id: str = Path(..., description="Route ID (e.g. 5463) or public line number (e.g. '101')")
):
    """
    Returns ordered stop sequences, distances, coordinates, and polyline for a route.
    """
    res = location_service.get_route_trajectory(route_id)
    if "error" in res:
        raise HTTPException(status_code=404, detail=res["error"])
    return res


# -------------------------------------------------------------
# 2. Timetable Engine Endpoints
# -------------------------------------------------------------

@timetable_router.get("/next", response_model=TimetableResponse)
async def get_next_bus(
    origin: Optional[str] = Query(None, description="Origin stop/place"),
    destination: Optional[str] = Query(None, description="Destination stop/place"),
    route_id: Optional[str] = Query(None, description="Route ID or route number (e.g. '201', '5463')"),
    boarding_stop_id: Optional[int] = Query(None, description="Passenger boarding stop ID"),
    boarding_stop_name: Optional[str] = Query(None, description="Passenger boarding stop name"),
    target_time: Optional[str] = Query(None, description="Target time (e.g., '15:00', '3 PM', '3 वाजता')")
):
    """
    Determines next available bus departure in Nashik local time (Asia/Kolkata).
    Accepts route ID directly or resolves via origin-destination.
    """
    if route_id:
        return timetable_engine.query_route_departures(
            route_id_or_number=route_id,
            boarding_stop_id=boarding_stop_id,
            boarding_stop_name=boarding_stop_name,
            query_mode="next",
            target_time=target_time,
            limit=5
        )

    if origin and destination:
        return timetable_engine.get_next_bus_for_od(
            origin_query=origin,
            destination_query=destination,
            target_time=target_time
        )

    raise HTTPException(status_code=400, detail="Either 'route_id' or both 'origin' and 'destination' must be provided.")


@timetable_router.get("/departures", response_model=TimetableResponse)
async def get_route_departures(
    route_id: str = Query(..., description="Route ID or route number (e.g., '101', '201', '5463')"),
    stop_id: Optional[int] = Query(None, description="Boarding stop ID for proportional departure calculation"),
    stop_name: Optional[str] = Query(None, description="Boarding stop name"),
    mode: str = Query("next", description="Mode: 'next', 'around', 'morning', 'evening', 'first', 'last', 'all_day'"),
    time: Optional[str] = Query(None, description="Reference time for 'around' mode (e.g. '14:30', '3 PM')"),
    limit: int = Query(10, ge=1, le=50, description="Max departures to return")
):
    """
    Returns scheduled departure bulletins across multiple query modes.
    """
    valid_modes = {"next", "around", "morning", "evening", "first", "last", "all_day"}
    if mode not in valid_modes:
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'. Choose from {valid_modes}")

    res = timetable_engine.query_route_departures(
        route_id_or_number=route_id,
        boarding_stop_id=stop_id,
        boarding_stop_name=stop_name,
        query_mode=mode,
        target_time=time,
        limit=limit
    )
    return res


# -------------------------------------------------------------
# 3. Places & POI Endpoints
# -------------------------------------------------------------

@places_router.get("/search")
async def search_places(
    q: str = Query(..., min_length=1, description="Place, hospital, landmark, or stop search query"),
    category: Optional[str] = Query("all", description="Category: all, hospital, landmark, stop, route"),
    limit: int = Query(20, ge=1, le=100)
):
    """
    Multilingual place search across verified stops, hospitals, landmarks, and routes.
    """
    return location_service.search_locations(q=q, category=category, limit=limit)


@places_router.get("/{place_id}/connections")
async def get_place_connections(
    place_id: str = Path(..., description="Place or hospital ID (e.g., 'civil_hospital', 'esi_hospital', or station ID)"),
    radius_km: float = Query(1.5, ge=0.1, le=10.0, description="Nearby radius in km")
):
    """
    Returns nearby bus stops, direct passing routes, and multimodal transit accessibility for a POI.
    """
    # 1. Check if place_id matches a verified hospital entity
    if place_id in location_service.hospital_entities_by_id:
        h = location_service.hospital_entities_by_id[place_id]
        plat = h["physical_coordinates"]["lat"]
        plon = h["physical_coordinates"]["lon"]
        nearby = location_service.get_nearby_stops(plat, plon, radius_km=radius_km, limit=10)
        return {
            "place_type": "verified_hospital",
            "place_id": place_id,
            "name_en": h["name_en"],
            "name_mr": h["name_mr"],
            "address": h["address"],
            "physical_coordinates": h["physical_coordinates"],
            "source": h["source"],
            "direct_routes": h["passing_routes"],
            "direct_stops": h["linked_bus_stops"],
            "nearby_network_stops": nearby["stops"],
            "walking_distance_disclaimer": h["walking_distance_disclaimer"]
        }

    # 2. Check if place_id is an integer station ID
    try:
        sid = int(place_id)
        if sid in location_service.stations_by_id:
            st = location_service.stations_by_id[sid]
            nearby = location_service.get_nearby_stops(st["lat"], st["lon"], radius_km=radius_km, limit=10)
            return {
                "place_type": "physical_transit_station",
                "station_id": sid,
                "name": st["name"],
                "clean_name": st["clean_name"],
                "coordinates": {"lat": st["lat"], "lon": st["lon"]},
                "passing_routes": st["passing_routes"],
                "passing_route_ids": st["passing_route_ids"],
                "category": st["category"],
                "nearby_stops": nearby["stops"]
            }
    except ValueError:
        pass

    # 3. Fallback: Search hospital navigation
    nav = location_service.get_hospital_navigation(place_id)
    if nav.get("resolved"):
        return nav

    raise HTTPException(status_code=404, detail=f"Place '{place_id}' not found in CityLinc transit directory.")

