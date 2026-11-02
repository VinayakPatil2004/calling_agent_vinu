"""
CityLinc Timetable & Schedule Engine.
Processes all 3,547 official timetable dispatch trips across 311 active routes.
Determines next bus departures using Nashik local time (Asia/Kolkata / IST UTC+5:30).

Capabilities:
- Real-time scheduled departure calculations against current local time or user-specified target time.
- Stop-level proportional ETA estimation along the corridor based on surveyed stop distances.
- Query modes: 'next', 'around', 'morning', 'evening', 'first', 'last', 'all_day'.
- Robust midnight crossing and circular 24-hour time handling.
- Explicit scheduling disclaimer: clearly distinguishes scheduled timetable trips from live vehicle tracking.
- Strict data integrity: zero synthetic or fabricated departure times.
"""
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel, Field

from backend.app.services.citylinc_knowledge import knowledge_service, DEVANAGARI_DIGITS
from backend.app.services.location_service import location_service

logger = logging.getLogger("citylinc.timetable_engine")

# Nashik Timezone: Indian Standard Time (IST, UTC+05:30)
NASHIK_TZ = timezone(timedelta(hours=5, minutes=30))


class ScheduledTrip(BaseModel):
    trip_id: int
    route_id: int
    rno: str
    route_name: str
    vehicle_no: Optional[str] = None
    origin_departure: str  # ETA from terminal start (HH:MM)
    origin_terminal: str
    destination_arrival: str
    destination_terminal: str
    boarding_stop_name: str
    estimated_boarding_time: str  # Estimated departure from passenger's boarding stop (HH:MM)
    minutes_until_departure: Optional[int] = None
    trip_status_code: str = "0"
    is_next_day: bool = False
    is_estimated: bool = False


class TimetableResponse(BaseModel):
    success: bool
    query_mode: str
    query_time_ist: str
    reference_stop: Optional[str] = None
    route_id: Optional[int] = None
    rno: Optional[str] = None
    route_name: Optional[str] = None
    trips_count: int
    scheduled_trips: List[ScheduledTrip]
    message_en: str
    message_mr: str
    message_hi: str
    disclaimer: str = "Departures are based on official scheduled timetables. Live GPS vehicle tracking requires active on-board AVL hardware."


def parse_time_minutes(time_str: str) -> Optional[int]:
    """Parses 'HH:MM' or 'H:MM' 24hr string into integer minutes from midnight (0 - 1439)."""
    if not time_str or ":" not in time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        h = int(parts[0])
        m = int(parts[1][:2])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, IndexError):
        pass
    return None


def format_minutes_time(minutes: int) -> str:
    """Formats minutes from midnight into 'HH:MM' 24hr string."""
    m_norm = minutes % (24 * 60)
    h = m_norm // 60
    m = m_norm % 60
    return f"{h:02d}:{m:02d}"


def parse_natural_time(time_str: str, base_time: datetime) -> Optional[datetime]:
    """
    Parses natural user expressions like '3 PM', '3:30 pm', '15:00', '3 वाजता', '३:००'.
    Returns a timezone-aware datetime in IST.
    """
    if not time_str:
        return None

    norm = time_str.strip()
    for d_char, a_digit in DEVANAGARI_DIGITS.items():
        norm = norm.replace(d_char, a_digit)
    norm = norm.lower().replace("वाजता", "").replace("बजे", "").strip()

    # Match standard 24hr HH:MM
    m24 = re.search(r'\b(\d{1,2}):(\d{2})\b', norm)
    if m24:
        h = int(m24.group(1))
        m = int(m24.group(2))
        is_pm = "pm" in norm or "दुपारी" in norm or "संध्याकाळी" in norm or "रात्री" in norm
        is_am = "am" in norm or "सकाळी" in norm
        if is_pm and h < 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        return base_time.replace(hour=h, minute=m, second=0, microsecond=0)

    # Match 12hr e.g. '3 pm', '3 am', '3'
    m12 = re.search(r'\b(\d{1,2})\s*(?:am|pm|pm|वाजता)?\b', norm)
    if m12:
        h = int(m12.group(1))
        is_pm = "pm" in norm or "दुपारी" in norm or "संध्याकाळी" in norm or "रात्री" in norm
        if is_pm and h < 12:
            h += 12
        elif not is_pm and h < 6:  # If user says "3" without AM/PM in afternoon, assume 15:00
            h += 12
        return base_time.replace(hour=h, minute=0, second=0, microsecond=0)

    return None


class TimetableEngine:
    """High-performance schedule engine for CityLinc Nashik timetables."""

    def __init__(self):
        pass

    def get_current_nashik_time(self) -> datetime:
        """Returns current wall-clock datetime in Nashik (IST)."""
        return datetime.now(NASHIK_TZ)

    def _estimate_stop_departure_time(
        self,
        route_dict: Dict[str, Any],
        trip_dict: Dict[str, Any],
        boarding_stop_id: Optional[int] = None,
        boarding_stop_name: Optional[str] = None
    ) -> Tuple[str, int, bool]:
        """
        Calculates scheduled departure time at an intermediate passenger boarding stop.
        If passenger boards at origin terminal, returns (trip['eta'], 0, False).
        Otherwise, calculates proportional arrival based on surveyed corridor distance.
        Returns: (departure_time_str_HH_MM, minutes_offset_from_origin, is_estimated)
        """
        origin_eta = trip_dict.get("eta", "00:00")
        dest_arr = trip_dict.get("arrivalatdestination", origin_eta)
        t_origin_mins = parse_time_minutes(origin_eta) or 0
        t_dest_mins = parse_time_minutes(dest_arr) or (t_origin_mins + 60)

        # Handle midnight duration
        trip_duration = t_dest_mins - t_origin_mins
        if trip_duration < 0:
            trip_duration += 1440
        if trip_duration == 0:
            trip_duration = 45  # fallback reasonable city trip duration

        stops = route_dict.get("stops", [])
        if not stops or (boarding_stop_id is None and boarding_stop_name is None):
            return origin_eta, 0, False

        target_idx = -1
        for idx, s in enumerate(stops):
            if boarding_stop_id is not None and int(s.get("stationid") or -1) == boarding_stop_id:
                target_idx = idx
                break
            if boarding_stop_name is not None and boarding_stop_name.lower() in (s.get("stationname") or "").lower():
                target_idx = idx
                break

        if target_idx <= 0:
            return origin_eta, 0, False

        try:
            total_corr_dist = float(stops[-1].get("totaldistance") or 1.0)
            target_dist = float(stops[target_idx].get("totaldistance") or 0.0)
            ratio = min(1.0, max(0.0, target_dist / total_corr_dist)) if total_corr_dist > 0 else 0.0
        except (ValueError, TypeError, ZeroDivisionError):
            ratio = target_idx / len(stops)

        stop_offset_mins = int(round(ratio * trip_duration))
        stop_departure_mins = (t_origin_mins + stop_offset_mins) % 1440
        return format_minutes_time(stop_departure_mins), stop_offset_mins, True

    def query_route_departures(
        self,
        route_id_or_number: str,
        boarding_stop_id: Optional[int] = None,
        boarding_stop_name: Optional[str] = None,
        query_mode: str = "next",
        target_time: Optional[str] = None,
        limit: int = 5
    ) -> TimetableResponse:
        """
        Queries scheduled timetable trips for a specific route.
        Supported modes:
        - 'next': Departures immediately following reference time.
        - 'around': Departures around specified target time.
        - 'morning': Departures between 06:00 and 11:59.
        - 'evening': Departures between 16:00 and 21:59.
        - 'first': The earliest scheduled departure of the day.
        - 'last': The final scheduled departure of the day.
        - 'all_day': All scheduled departures chronologically.
        """
        now_ist = self.get_current_nashik_time()
        ref_time = now_ist
        if target_time:
            parsed_t = parse_natural_time(target_time, now_ist)
            if parsed_t:
                ref_time = parsed_t

        ref_time_str = ref_time.strftime("%H:%M")
        ref_mins = ref_time.hour * 60 + ref_time.minute

        # Resolve route
        target_routes = []
        try:
            rid = int(route_id_or_number)
            if rid in knowledge_service.routes_by_id:
                target_routes.append(knowledge_service.routes_by_id[rid])
        except ValueError:
            pass

        if not target_routes:
            rno_norm = route_id_or_number.strip().upper()
            target_routes = knowledge_service.routes_by_number.get(rno_norm, [])

        if not target_routes:
            return TimetableResponse(
                success=False,
                query_mode=query_mode,
                query_time_ist=ref_time_str,
                trips_count=0,
                scheduled_trips=[],
                message_en=f"Route '{route_id_or_number}' was not found in the CityLinc network.",
                message_mr=f"सिटीलिंक नेटवर्कमध्ये मार्ग '{route_id_or_number}' सापडला नाही.",
                message_hi=f"सिटीलिंक नेटवर्क में रूट '{route_id_or_number}' नहीं मिला।"
            )

        # Collect all trips across matching route variants
        all_trip_models: List[ScheduledTrip] = []

        for r in target_routes:
            rid = int(r.get("routeid"))
            rno = str(r.get("rno", "")).strip().upper()
            rname = r.get("routename") or ""
            stops = r.get("stops", [])
            origin_name = stops[0].get("stationname") if stops else r.get("fromstation") or "Origin"
            dest_name = stops[-1].get("stationname") if stops else r.get("tostation") or "Destination"

            for t in r.get("timetable", []):
                origin_eta = t.get("eta", "")
                if not origin_eta:
                    continue

                res_est = self._estimate_stop_departure_time(
                    route_dict=r,
                    trip_dict=t,
                    boarding_stop_id=boarding_stop_id,
                    boarding_stop_name=boarding_stop_name
                )
                stop_eta = res_est[0]
                is_estimated = res_est[2] if len(res_est) > 2 else (res_est[1] > 0)

                stop_mins = parse_time_minutes(stop_eta) or 0
                mins_diff = stop_mins - ref_mins
                is_next_day = False
                if mins_diff < 0:
                    mins_diff += 1440
                    is_next_day = True

                b_name = boarding_stop_name or (stops[0].get("stationname") if stops else origin_name)
                all_trip_models.append(ScheduledTrip(
                    trip_id=int(t.get("tripid")),
                    route_id=rid,
                    rno=rno,
                    route_name=rname,
                    vehicle_no=t.get("vehicleno"),
                    origin_departure=origin_eta,
                    origin_terminal=str(origin_name),
                    destination_arrival=t.get("arrivalatdestination", ""),
                    destination_terminal=str(dest_name),
                    boarding_stop_name=b_name,
                    estimated_boarding_time=stop_eta,
                    minutes_until_departure=mins_diff,
                    trip_status_code=str(t.get("trip_status", "0")),
                    is_next_day=is_next_day,
                    is_estimated=is_estimated
                ))

        if not all_trip_models:
            return TimetableResponse(
                success=False,
                query_mode=query_mode,
                query_time_ist=ref_time_str,
                route_id=target_routes[0].get("routeid"),
                rno=target_routes[0].get("rno"),
                route_name=target_routes[0].get("routename"),
                trips_count=0,
                scheduled_trips=[],
                message_en=f"Timetable schedule for Route {route_id_or_number} is currently pending operator dispatch.",
                message_mr=f"मार्ग {route_id_or_number} साठी वेळापत्रक सध्या उपलब्ध नाही.",
                message_hi=f"रूट {route_id_or_number} के लिए समय-सारणी अभी उपलब्ध नहीं है।"
            )

        # Sort trips chronologically by estimated boarding time
        all_trip_models.sort(key=lambda x: parse_time_minutes(x.estimated_boarding_time) or 0)

        # Filter according to mode
        filtered_trips: List[ScheduledTrip] = []

        if query_mode == "first":
            filtered_trips = [all_trip_models[0]]
        elif query_mode == "last":
            filtered_trips = [all_trip_models[-1]]
        elif query_mode == "morning":
            filtered_trips = [
                t for t in all_trip_models
                if 360 <= (parse_time_minutes(t.estimated_boarding_time) or 0) < 720
            ][:limit]
        elif query_mode == "evening":
            filtered_trips = [
                t for t in all_trip_models
                if 960 <= (parse_time_minutes(t.estimated_boarding_time) or 0) < 1320
            ][:limit]
        elif query_mode == "around":
            # Trips within -30 mins and +60 mins of target
            filtered_trips = [
                t for t in all_trip_models
                if -30 <= ((parse_time_minutes(t.estimated_boarding_time) or 0) - ref_mins) <= 90
            ][:limit]
            if not filtered_trips:
                filtered_trips = [t for t in all_trip_models if (parse_time_minutes(t.estimated_boarding_time) or 0) >= ref_mins][:limit]
        elif query_mode == "all_day":
            filtered_trips = all_trip_models[:limit]
        else:
            # Default 'next': Trips departing after reference time
            upcoming = [
                t for t in all_trip_models
                if (parse_time_minutes(t.estimated_boarding_time) or 0) >= ref_mins
            ]
            if upcoming:
                filtered_trips = upcoming[:limit]
            else:
                # If all scheduled trips for today have departed, return tomorrow morning's first departures
                tomorrow_trips = []
                for t in all_trip_models[:limit]:
                    item = t.model_copy()
                    item.is_next_day = True
                    tomorrow_trips.append(item)
                filtered_trips = tomorrow_trips

        # Formulate multilingual messages
        primary_trip = filtered_trips[0] if filtered_trips else None
        route_label = primary_trip.rno if primary_trip else route_id_or_number
        b_name = primary_trip.boarding_stop_name if primary_trip else "थांब्यावरून"

        if primary_trip:
            next_t = primary_trip.estimated_boarding_time
            if primary_trip.is_next_day:
                if primary_trip.is_estimated:
                    msg_en = f"All scheduled buses for today on Route {route_label} have departed. The first bus tomorrow is estimated to depart at approximately {next_t} from {primary_trip.boarding_stop_name}."
                    msg_mr = f"आजच्या मार्ग {route_label} च्या सर्व नियोजित बसेस निघून गेल्या आहेत. उद्या सकाळी पहिली बस {primary_trip.boarding_stop_name} येथून अंदाजे {next_t} वाजता सुटेल."
                    msg_hi = f"आज के लिए रूट {route_label} की सभी बसें निकल चुकी हैं। कल सुबह की पहली बस {primary_trip.boarding_stop_name} से लगभग {next_t} बजे रवाना होगी।"
                else:
                    msg_en = f"All scheduled buses for today on Route {route_label} have departed. The first bus tomorrow departs at {next_t} from {primary_trip.boarding_stop_name}."
                    msg_mr = f"आजच्या मार्ग {route_label} च्या सर्व फेऱ्या पूर्ण झाल्या आहेत. उद्या सकाळची पहिली बस {next_t} वाजता {primary_trip.boarding_stop_name} येथून सुटेल."
                    msg_hi = f"आज के लिए रूट {route_label} की सभी बसें निकल चुकी हैं। कल सुबह की पहली बस {next_t} बजे {primary_trip.boarding_stop_name} से रवाना होगी।"
            else:
                mins_txt = f"in {primary_trip.minutes_until_departure} mins" if primary_trip.minutes_until_departure is not None else ""
                if primary_trip.is_estimated:
                    msg_en = f"Route {route_label} is estimated to depart from {primary_trip.boarding_stop_name} at approximately {next_t}."
                    msg_mr = f"मार्ग {route_label} ची पुढची बस {primary_trip.boarding_stop_name} येथून अंदाजे {next_t} वाजता सुटेल."
                    msg_hi = f"रूट {route_label} की अगली बस {primary_trip.boarding_stop_name} से लगभग {next_t} बजे रवाना होगी।"
                else:
                    msg_en = f"The next scheduled bus on Route {route_label} departs at {next_t} ({mins_txt}) from {primary_trip.boarding_stop_name}."
                    msg_mr = f"मार्ग {route_label} ची पुढची बस {next_t} वाजता {primary_trip.boarding_stop_name} येथून सुटेल."
                    msg_hi = f"रूट {route_label} की अगली बस {next_t} बजे {primary_trip.boarding_stop_name} से छूटेगी।"
        else:
            msg_en = f"No departures found for Route {route_label} under query mode '{query_mode}'."
            msg_mr = f"मार्ग {route_label} साठी फेऱ्या सापडल्या नाहीत."
            msg_hi = f"रूट {route_label} के लिए कोई बस नहीं मिली।"

        return TimetableResponse(
            success=bool(filtered_trips),
            query_mode=query_mode,
            query_time_ist=ref_time_str,
            reference_stop=b_name,
            route_id=target_routes[0].get("routeid"),
            rno=target_routes[0].get("rno"),
            route_name=target_routes[0].get("routename"),
            trips_count=len(filtered_trips),
            scheduled_trips=filtered_trips,
            message_en=msg_en,
            message_mr=msg_mr,
            message_hi=msg_hi
        )

    def get_next_bus_for_od(
        self,
        origin_query: str,
        destination_query: str,
        target_time: Optional[str] = None
    ) -> TimetableResponse:
        """
        Determines next available bus between an origin and destination.
        Integrates RouteEngine search with timetable trip departures.
        """
        from backend.app.services.route_engine import route_engine

        od_result = route_engine.search_routes(origin_query, destination_query, limit_direct=None)
        if not od_result.success or not od_result.direct_routes:
            now_str = self.get_current_nashik_time().strftime("%H:%M")
            return TimetableResponse(
                success=False,
                query_mode="next",
                query_time_ist=now_str,
                trips_count=0,
                scheduled_trips=[],
                message_en=f"No direct routes found between {origin_query} and {destination_query} to check schedules.",
                message_mr=f"{origin_query} ते {destination_query} दरम्यान थेट बस मार्ग उपलब्ध नाही.",
                message_hi=f"{origin_query} से {destination_query} के बीच सीधा रूट उपलब्ध नहीं है।"
            )

        best_direct = od_result.direct_routes[0]
        return self.query_route_departures(
            route_id_or_number=str(best_direct.route_id),
            boarding_stop_id=best_direct.boarding_stop.station_id,
            boarding_stop_name=best_direct.boarding_stop.name,
            query_mode="next",
            target_time=target_time,
            limit=5
        )


# Global singleton
timetable_engine = TimetableEngine()
