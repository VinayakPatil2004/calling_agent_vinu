"""
CityLinc Transit Fare Service (Phase 4.7)
Implements official NMC CityLinc distance-based tariff slabs (effective 1st January 2023),
multi-leg boarding fare summation, pass comparisons, and tariff validity governance.
"""

import math
from typing import Dict, Any, List, Optional, Tuple


class CityLincFareService:
    """
    Centralized, versioned tariff engine for CityLinc Nashik.
    Based on the official 32-stage distance tariff from `data/raw/fare/fare_matrix.pdf`
    (NMC revised tariff effective 1st January 2023).
    """

    TARIFF_VERSION = "2023-01-01"
    TARIFF_STATUS = "INDICATIVE_TARIFF_2023"  # Awaiting 2024-2026 gazette verification
    DAILY_PASS_INR = 110.0  # Open-Ended 1-Day Pass (unlimited travel across network)

    # 32 Official Distance Stages: (max_km_inclusive, adult_fare_inr, kids_fare_inr)
    # Extracted verbatim from data/raw/fare/fare_matrix.pdf
    TARIFF_SLABS: List[Tuple[float, int, int]] = [
        (2.0, 10, 5),
        (4.0, 17, 10),
        (6.0, 23, 12),
        (8.0, 30, 15),
        (10.0, 30, 15),
        (12.0, 35, 18),
        (14.0, 35, 18),
        (16.0, 35, 18),
        (18.0, 40, 18),
        (20.0, 40, 21),
        (22.0, 40, 21),
        (24.0, 45, 23),
        (26.0, 45, 23),
        (28.0, 50, 25),
        (30.0, 50, 25),
        (32.0, 55, 28),
        (34.0, 55, 28),
        (36.0, 55, 28),
        (38.0, 55, 28),
        (40.0, 60, 30),
        (42.0, 60, 30),
        (44.0, 70, 35),
        (46.0, 70, 35),
        (48.0, 75, 38),
        (50.0, 75, 38),
        (52.0, 80, 40),
        (54.0, 80, 40),
        (56.0, 85, 43),
        (58.0, 85, 43),
        (60.0, 90, 45),
        (62.0, 90, 45),
    ]

    def compute_stage_fare(self, distance_km: float, is_child: bool = False) -> int:
        """
        Computes the single-ticket adult or child fare for a single bus boarding based on distance.
        Boundary condition: Half-open interval (lower_km, upper_km].
        Example: 2.0 km -> Stage 1 (Rs 10). 2.1 km -> Stage 2 (Rs 17).
        """
        d = max(0.1, float(distance_km))

        # Check standard 31 slabs (up to 62.0 km)
        for max_km, adult_fare, kids_fare in self.TARIFF_SLABS:
            if d <= max_km:
                return kids_fare if is_child else adult_fare

        # Stage 32 (> 62.0 km): Base 90 INR + Rs 30 per additional 10 km (Gov Ceiling)
        extra_km = d - 62.0
        extra_units = math.ceil(extra_km / 10.0)
        adult_fare = 90 + (extra_units * 30)
        kids_fare = 45 + (extra_units * 15)
        return kids_fare if is_child else adult_fare

    def compute_journey_fare(
        self,
        leg_distances_km: List[float],
        is_child: bool = False
    ) -> Dict[str, Any]:
        """
        Computes multi-leg journey fare enforcing CityLinc's boarding-based ticketing policy:
        total_fare = sum(individual_leg_fares).
        Also provides comparison against the verified Rs 110 Open-Ended Daily Pass.
        """
        if not leg_distances_km:
            leg_distances_km = [1.0]

        leg_fares = [self.compute_stage_fare(d, is_child=is_child) for d in leg_distances_km]
        total_fare = sum(leg_fares)
        total_distance = sum(leg_distances_km)

        # Daily pass recommendation if journey has multiple transfers or total fare >= 110
        daily_pass_recommended = (total_fare >= self.DAILY_PASS_INR) or (len(leg_distances_km) >= 3)

        return {
            "total_fare_inr": total_fare,
            "leg_fares_inr": leg_fares,
            "total_distance_km": round(total_distance, 2),
            "tariff_version": self.TARIFF_VERSION,
            "tariff_status": self.TARIFF_STATUS,
            "is_indicative": True,  # Awaiting 2024-2026 gazette verification
            "daily_pass_inr": self.DAILY_PASS_INR,
            "daily_pass_recommended": daily_pass_recommended,
            "fare_rule": "Individual boarding ticket required per bus leg (no free transfers)",
            "disclaimer_mr": "हे भाडे १ जानेवारी २०२३ च्या तक्त्यानुसार अंदाजित असून अधिकृत वाहकाकडे (conductor) खात्री करावी.",
            "disclaimer_hi": "यह किराया १ जनवरी २०२३ की तालिका के अनुसार अनुमानित है, कृपया बस कंडक्टर से पुष्टि करें।",
            "disclaimer_en": "This fare is estimated based on the 1-Jan-2023 indicative tariff; please confirm with the conductor."
        }

    def format_fare_message(
        self,
        fare_data: Dict[str, Any],
        lang: str = "mr-IN"
    ) -> str:
        """Generates localized, transparent passenger-facing fare text."""
        total = fare_data["total_fare_inr"]
        legs_count = len(fare_data.get("leg_fares_inr", []))

        if lang == "mr-IN":
            if legs_count <= 1:
                base = f"अंदाजे तिकीट ₹{total} (१ जानेवारी २०२३ तक्त्यानुसार अंदाजित; वाहकाकडे खात्री करावी)"
            else:
                base = f"एकूण अंदाजे तिकीट खर्च ₹{total} (प्रत्येक बससाठी स्वतंत्र तिकीट)"
                if fare_data.get("daily_pass_recommended"):
                    base += f". दिवसभरात अधिक प्रवास असल्यास ₹{int(self.DAILY_PASS_INR)} चा दैनिक पास फायदेशीर ठरू शकतो"
            return base
        elif lang == "hi-IN":
            if legs_count <= 1:
                base = f"अनुमानित टिकट किराया ₹{total} (१ जनवरी २०२३ तालिका के अनुसार अनुमानित; कंडक्टर से पुष्टि करें)"
            else:
                base = f"कुल अनुमानित किराया ₹{total} (प्रत्येक बस का अलग टिकट लगेगा)"
                if fare_data.get("daily_pass_recommended"):
                    base += f"। अधिक यात्रा के लिए ₹{int(self.DAILY_PASS_INR)} का दैनिक पास अधिक किफायती हो सकता है"
            return base
        else:
            if legs_count <= 1:
                base = f"Estimated ticket fare ₹{total} (based on 1-Jan-2023 indicative tariff; confirm with conductor)"
            else:
                base = f"Total estimated ticket fare ₹{total} (individual ticket required per bus)"
                if fare_data.get("daily_pass_recommended"):
                    base += f". For multiple rides, the ₹{int(self.DAILY_PASS_INR)} Daily Pass may be economical"
            return base


# Global singleton
fare_service = CityLincFareService()

