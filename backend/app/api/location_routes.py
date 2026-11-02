"""
REST API routes for Nashik Location Intelligence & Interactive Map.
Endpoints:
- GET /api/locations/stops
- GET /api/locations/search
- GET /api/locations/nearby
- GET /api/locations/hospitals
- GET /api/locations/landmarks
- GET /api/locations/routes/{route_id}
- GET /api/locations/routes (all route summaries)
"""
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, HTTPException, Path
from pydantic import BaseModel, Field

from backend.app.services.location_service import location_service

router = APIRouter(prefix="/api/locations", tags=["Location Intelligence"])


class StationResponseModel(BaseModel):
    station_id: int
    name: str
    clean_name: str
    display_name: str
    lat: float
    lon: float
    direction: str
    category: str
    is_hospital: bool
    is_landmark: bool
    landmark_category: Optional[str] = None
    passing_routes: List[str]
    passing_route_ids: List[int]
    stop_count_in_network: int


class StopsListResponse(BaseModel):
    total: int
    count: int
    offset: int
    limit: int
    stops: List[Dict[str, Any]]


class NearbyStopsResponse(BaseModel):
    center: Dict[str, float]
    radius_km: float
    total_found: int
    count: int
    disclaimer: str
    stops: List[Dict[str, Any]]


class HospitalListResponse(BaseModel):
    total: int
    count: int
    category: str
    hospitals: List[Dict[str, Any]]


class LandmarkListResponse(BaseModel):
    total: int
    count: int
    filter_category: str
    landmarks: List[Dict[str, Any]]


class SearchResponse(BaseModel):
    query: str
    category: str
    total_matches: int
    stops: List[Dict[str, Any]]
    routes: List[Dict[str, Any]]
    hospitals: List[Dict[str, Any]]
    landmarks: List[Dict[str, Any]]


class RouteTrajectoryResponse(BaseModel):
    route_id: Optional[int] = None
    rno: Optional[str] = None
    route_name: Optional[str] = None
    from_station: Optional[str] = None
    to_station: Optional[str] = None
    stop_count: int
    total_distance_km: float
    origin: Optional[Dict[str, Any]] = None
    destination: Optional[Dict[str, Any]] = None
    stops: List[Dict[str, Any]]
    polyline: List[List[float]]
    deduplicated_points_count: int
    raw_stops_count: int


@router.get("/stops", response_model=StopsListResponse)
async def get_stops(
    query: Optional[str] = Query(None, description="Filter by stop name or passing route"),
    route_id: Optional[int] = Query(None, description="Filter by route ID"),
    category: Optional[str] = Query(None, description="Filter by category (hospital, education, religious, transit_hub, junction)"),
    limit: int = Query(100, ge=0, le=2000, description="Page limit (0 returns all)"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """
    Returns physical bus stations in Nashik with GPS coordinates and passing routes.
    Includes all 1,187 verified stations with Up/Down platform distinction.
    """
    res = location_service.get_all_stops(
        query=query,
        route_id=route_id,
        category=category,
        limit=limit,
        offset=offset
    )
    return res


@router.get("/search", response_model=SearchResponse)
async def search_locations(
    q: str = Query(..., min_length=1, description="Search term in English, Devanagari Marathi, or Hindi"),
    category: Optional[str] = Query("all", description="Category filter: all, stop, route, hospital, landmark"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results per category")
):
    """
    Multilingual fuzzy search across transit stops, hospital landmarks, and bus routes.
    """
    return location_service.search_locations(q=q, category=category, limit=limit)


@router.get("/nearby", response_model=NearbyStopsResponse)
async def get_nearby_stops(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Center latitude in decimal degrees"),
    lon: float = Query(..., ge=-180.0, le=180.0, description="Center longitude in decimal degrees"),
    radius_km: float = Query(2.0, ge=0.1, le=50.0, description="Search radius in kilometers (straight-line Haversine)"),
    limit: int = Query(20, ge=1, le=100, description="Maximum stops to return")
):
    """
    Discovers nearest transit stops within a Haversine radius of specified coordinates.
    Returns passing routes and geodesic distance.
    """
    return location_service.get_nearby_stops(lat=lat, lon=lon, radius_km=radius_km, limit=limit)


@router.get("/hospitals", response_model=HospitalListResponse)
async def get_hospitals(
    query: Optional[str] = Query(None, description="Optional hospital name filter"),
    grouped: Optional[bool] = Query(False, description="If true, returns grouped physical hospital POIs with Up/Down platforms"),
    limit: int = Query(50, ge=1, le=100, description="Maximum results")
):
    """
    Returns verified hospital bus stops or grouped physical hospital entities with passing routes.
    """
    return location_service.get_hospitals(query=query, limit=limit, grouped=grouped)


@router.get("/hospitals/navigation")
async def get_hospital_navigation_endpoint(
    query: str = Query(..., description="Hospital name or alias (e.g., 'Civil Hospital', 'ESIC', 'NAMCO')")
):
    """
    Phase 4E: Returns POI navigation guidance, nearest verified bus stops,
    direct passing routes, and directional platform recommendations.
    """
    res = location_service.get_hospital_navigation(query=query)
    if not res.get("resolved"):
        raise HTTPException(status_code=404, detail=res.get("message", "Hospital not found"))
    return res


@router.get("/landmarks", response_model=LandmarkListResponse)
async def get_landmarks(
    category: Optional[str] = Query(None, description="Category filter: education, religious, transit_hub, junction"),
    query: Optional[str] = Query(None, description="Optional landmark name filter"),
    limit: int = Query(100, ge=1, le=500, description="Maximum results")
):
    """
    Returns categorized major landmarks (educational institutes, cultural/religious sites, transit hubs).
    """
    return location_service.get_landmarks(category=category, query=query, limit=limit)


@router.get("/routes")
async def get_all_routes_summary():
    """
    Returns summary list of all 315 routes with public line numbers (rno),
    origins, destinations, and stop counts for map route selector.
    """
    return {
        "total": len(location_service.routes_summary),
        "routes": location_service.routes_summary
    }


@router.get("/routes/{route_id}", response_model=RouteTrajectoryResponse)
async def get_route_trajectory(
    route_id: str = Path(..., description="Route ID (e.g. 6601) or Route Number (e.g. '101')")
):
    """
    Returns full trajectory, ordered sequence of stops, and clean polyline for a route.
    Consecutive duplicate station coordinates (DQI-004) are deduplicated in polyline.
    """
    res = location_service.get_route_trajectory(route_id)
    if not res or res.get("error"):
        raise HTTPException(
            status_code=404,
            detail=res.get("error", f"Route '{route_id}' not found.")
        )
    return res

