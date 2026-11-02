# CityLinc AI Calling Agent — Comprehensive Data Profiling Report

**Document ID:** `DATA_PROFILING_REPORT.md`  
**Phase:** P0.2 — Source Data Ingestion & Integrity Profiling (Post-Remediation Revision 3)  
**Status:** PROPOSED SPECIFICATION — READ-ONLY AUDIT  
**Classification:** Baseline Transit Data Profiling Report  
**Execution Gate:** STRICT READ-ONLY — PHASE 2 STRICTLY LOCKED

---

## Verifiable Database Safety Statement

> **Database Safety Notice:**  
> This audit executed zero SQL statements, established zero database connections, and created no database objects. The existing PostgreSQL database state was not inspected because database connections are prohibited under strict read-only execution. All profiling metrics, schema mappings, and integrity constraints in this report are strictly derived from read-only inspection of source assets awaiting authorized Phase 2 execution.

---

## 1. Executive Summary

This report establishes the baseline quantitative and qualitative profile for the CityLinc public transit network across all eight primary source assets located in `data/raw/routes/`, `data/raw/timetables/`, and `data/raw/fare/`.

### Network Core Metrics:
- **Corridors & Routes:** 315 unique master routes (`routeid`), representing 121 unique public customer line numbers (`rno`).
- **Stop Sequences & Platforms:** 12,909 physical stop sequence instances, representing 1,187 unique physical platform points (`stationid`) and 786 logical station clusters (`displaystationid`).
- **Operational Dispatch Trips:** 3,547 scheduled trips across 311 active routes, operated by 244 distinct transit vehicles (`vehicleno`). Exactly 4 master routes have no scheduled trips (`6625`, `6664`, `6784`, `7007`).
- **Network Extent & Max Distance:** Maximum operational corridor distance is **45.4 km**, documented on Route ID `5989` (Public Line `245`, Stop Sequence `68`, *Nashik Road Depot*). Route ID `6868` represents Public Line `331` with a maximum distance of 22.1 km. Units are verified as decimal kilometres (`distance_from_origin_km`).
- **Consecutive Duplicate Stops:** Exactly 63 consecutive duplicate stop pairs (126 records) occur across 3 routes (`6710`, `6908`, `6866`), involving 66 unique repeated physical station IDs:
  - Route `6710` (Public Line `129I`): 1 consecutive pair (2 records, sequences 36 & 37, station `1049` *Bharat Nagar 1*)
  - Route `6908` (Public Line `328T`): 36 consecutive pairs (72 records)
  - Route `6866` (Public Line `331G`): 26 consecutive pairs (52 records)
  - **Preservation Policy:** All 12,909 records are preserved in `route_stops`; consecutive duplicates are flagged with `is_consecutive_duplicate = TRUE`. Zero records are deleted.
- **Stop Coordinates & Pattern Attribution:** An empirical numerical offset pattern is observed across route stops where bounding coordinate fields exhibit uniform arithmetic offsets relative to center coordinates. This pattern is tracked during ingestion via the audit flag `matches_observed_offset_pattern`. Ingestion preserves raw numeric values verbatim without claiming physical GPS provenance or asserting a confirmed synthetic origin. Physical coordinate validity and platform provenance remain provisional pending operator verification. Stored in `public.route_stop_coordinates_audit` keyed to surrogate `route_stop_id`, retaining `source_routepartid` and physical CSV `source_row_number`.
- **Timetable Dispatch Status Codes:** Column `trip_status` contains four discrete integer codes across all 3,547 records:
  - Code `0`: 2,085 rows (58.78%)
  - Code `2`: 1,237 rows (34.87%)
  - Code `6`: 218 rows (6.15%)
  - Code `9`: 7 rows (0.20%)
  - Meanings remain unconfirmed operational dispatch codes and are modeled in `trip_status_lookup`.
- **Timetable Fares & Stationfare Semantics:** Mandatory interpretation statement:
  > *"All 3,547 timetable records contain matching StopName and StartTrip values, with stationfare = 0. The operational and fare meaning of these fields remains unconfirmed and requires operator clarification."*  
  Ingestion maps `stationfare` to `raw_stationfare`, sets `fare_amount` to `NULL`, and applies `fare_verification_status = 'UNVERIFIED'`. Hardened constraint `chk_fare_verification` prevents SQL NULL bypass.
- **Timetable Temporal Semantics:** Preserves `source_eta`, `source_arrival_at_destination`, `raw_eta_text`, and `raw_arrival_text VARCHAR(16) NOT NULL`. Modular next-day duration calculations are stored in `provisional_is_next_day_arrival` and `provisional_duration_mins`.
- **Summary Departure Bulletins:** `data/raw/timetables/citylinc_timetables.json` (120 route blocks containing 2,318 departure tokens). The attributes `source_departures` and `destination_departures` are preserved as **string attributes**, NOT nested arrays. Ingestion tracks `source_route_array_index` (0–119), departure/destination attribute (`departure_attribute`), and `source_departure_token_index` independently, with deterministic `occurrence_index SMALLINT NOT NULL` (no default), retaining `raw_departure_string`. Preservation of all 2,318 tokens conditionally depends on complete parsing, valid route/direction mapping, and successful unique occurrence assignment.
- **Fare Slabs & Passes:** 
  - 32 distance-based tariff slabs from `data/raw/fare/fare_matrix.pdf`. Row 32 (>62 km) base municipal adult and child fares are blank/NULL; Government Maximum ceiling rate ₹30.00 per 10 km is preserved.
  - 90-day open-ended pass price is ₹5,250 verified across `data/raw/fare/bus_passes.json` and `data/raw/fare/New Fare Passes.pdf` (FND-024).
  - Passenger concessions: 50% concession for companions of blind/handicapped pass holders (FND-028), student concessions (50% standard, 66.66% in `New Fare Passes.pdf` vs 66.60% in `bus_passes.json`), fixed-route passes (1M = 20 days single fare / 3M = 50 days single fare), and municipal residency limits.

---

## 2. Source Dataset Catalog

| # | Asset File Name | Canonical Workspace Path | File Size | Format | Encoding | Record Count | Primary Domain Content | SHA-256 Checksum |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :--- | :--- |
| **1** | `all_routes_stops.json` | `data/raw/routes/all_routes_stops.json` | 7,598,396 B | JSON Array | UTF-8 | 315 routes | Master corridors, nested stop sequences, nested trip schedules | `bdb1527c1879d73ce61e0c4060ca1d0f5a944bf9849bf2a9fb854bd2c47c9460` |
| **2** | `all_routes_stops.csv` | `data/raw/routes/all_routes_stops.csv` | 5,599,018 B | Delimited CSV | UTF-8 | 12,909 rows | Flattened stop sequences, platform coordinates, distance from origin | `f25ddc0eb32a30d6ab344bf63e8f16e3eb0f825785a6acb83621d0a39802a8c8` |
| **3** | `all_route_timetables.csv` | `data/raw/timetables/all_route_timetables.csv` | 1,658,742 B | Delimited CSV | UTF-8 | 3,547 rows | Operational CAD/AVL trip dispatches, vehicles, scheduled times | `85f9d17ecd039f1d89df988fee718503d5f6bf229e9bdcb5882f93025a9cd209` |
| **4** | `citylinc_timetables.json` | `data/raw/timetables/citylinc_timetables.json` | 54,814 B | JSON Array | UTF-8 | 120 items | Passenger bulletin summaries, directional departure time strings | `bbd3facc4cecb7e9767d5945a2e6730eb82ef3cbfe101aa98f0c87f4f663e980` |
| **5** | `bus_stop.json` | `data/raw/timetables/bus_stop.json` | 5,376 B | JSON Array | UTF-8 | 10 items | Sample directional stop sequences (auxiliary reference) | `1cb7002a0e841c8341c90bbe01ad858d6dd6f229fba2c9c8e51f652365d77cea` |
| **6** | `bus_passes.json` | `data/raw/fare/bus_passes.json` | 1,317 B | JSON Object | UTF-8 | 3 schemes | Pass categories, durations, concessions, flat pricing | `6ead1770a2b4ddfa5b83fb92926960442eecd18f85dae907c51bed28dcca9ffd` |
| **7** | `fare_matrix.pdf` | `data/raw/fare/fare_matrix.pdf` | 685,686 B | PDF Document | Binary | 32 slabs | Distance tariff table, 2022/2023 rates, government caps | `b5dd0cbfdd7297d7840465f2e7a9b05cf412f533345e315396592e4da2fc92dd` |
| **8** | `New Fare Passes.pdf` | `data/raw/fare/New Fare Passes.pdf` | 112,646 B | PDF Document | Binary | 4 tables | Revised pass pricing, route-specific passes, fare concessions | `ba5e37c3747a673c3653dc1d5185260bb2b2cc345addce33fb10bbb4c5f6b169` |

---

## 3. Deep-Dive Domain Profiling

### 3.1 Route Corridors (`all_routes_stops.json` / `all_routes_stops.csv`)
- **Total Master Corridors:** Exactly 315 unique routes.
- **Directional Split:** 158 `Up` direction routes, 157 `Down` direction routes.
- **Identifier Independence:** 
  - `rno`: Customer-facing line string (e.g. `"101"`, `"129I"`, `"245"`, `"328T"`). 121 unique line numbers.
  - `routeno`: Fleet category / depot operational code taking exclusively integer values `3`, `4`, and `6`.
- **Corridor Distance Profile:**
  - Cumulative distance column `totaldistance` (`distance_from_origin_km`) in `all_routes_stops.csv` represents verified decimal kilometres (`distance_from_origin_km`).
  - Minimum route distance: 1.2 km (Route `6878`).
  - Maximum route distance: **45.4 km** on Route ID `5989` (Public Line `245`, Stop Sequence `68`, *Nashik Road Depot*). Route ID `6868` represents Public Line `331` with a maximum distance of 22.1 km.

### 3.2 Stop Sequences, Coordinates, and Duplicate Stations (`all_routes_stops.csv`)
- **Total Stop Sequences:** Exactly 12,909 rows. Every row possesses a unique natural sequence key `routepartid` (12,909 distinct values).
- **Physical vs. Logical Stations:** 1,187 unique physical platform points (`stationid`) cluster into 786 unique logical passenger stop names (`displaystationid`).
- **Consecutive Duplicate Stops:**
  - Exactly 63 consecutive duplicate stop pairs (126 physical records) occur across 3 routes:
    - Route `6710` (Public Line `129I`): 1 consecutive pair (2 records, sequences 36 & 37, station `1049` *Bharat Nagar 1*)
    - Route `6908` (Public Line `328T`): 36 consecutive pairs (72 records)
    - Route `6866` (Public Line `331G`): 26 consecutive pairs (52 records)
  - Across these three routes, exactly 66 unique physical station IDs repeat.
  - **Preservation Policy:** All 12,909 records are retained in `route_stops`; consecutive duplicates are flagged with `is_consecutive_duplicate = TRUE`. Zero records are deleted.
- **Coordinate Offset Pattern & Attribution:**
  - Standard attribution statement:
    > *"An empirical numerical offset pattern is observed across route stops where bounding coordinate fields exhibit uniform arithmetic offsets relative to center coordinates. This pattern is tracked during ingestion via the audit flag `matches_observed_offset_pattern`. Ingestion preserves raw numeric values verbatim without claiming physical GPS provenance or asserting a confirmed synthetic origin. Physical coordinate validity and platform provenance remain provisional pending operator verification."*All 3,547 timetable records contain matching StopName and StartTrip values, with stationfare = 0. The operational and fare meaning of these fields remains unconfirmed and requires operator clarification."*
- **Temporal Fidelity & Midnight Crossing:**
  - Raw strings are preserved in `raw_eta_text` and `raw_arrival_text VARCHAR(16) NOT NULL`.
  - Trips where `arrivalatdestination < eta` are flagged via `provisional_is_next_day_arrival = TRUE` and duration computed modulo 24 hours into `provisional_duration_mins`.

### 3.4 Summary Departure Bulletins (`citylinc_timetables.json`)
- **Total Route Blocks:** 120 items.
- **Total Departure Tokens:** 2,318 tokens across all route blocks.
- **String Attribute Architecture:** In `citylinc_timetables.json`, `source_departures` and `destination_departures` are **string attributes** (formatted as comma-separated departure strings), **NOT nested arrays**.
- **Independent Lineage Tracking:**
  - Outer route block index: `source_route_array_index` (0–119).
  - Directional string attribute: `departure_attribute` (`'source_departures'` or `'destination_departures'`).
  - Positional token index: `source_departure_token_index` (0-based index within the string).
  - Deterministic occurrence counter: `occurrence_index SMALLINT NOT NULL` (no default).
  - Raw string attribute: `raw_departure_string TEXT NOT NULL`.
- **Conditional Preservation Notice:** Complete preservation of all 2,318 tokens depends on complete parsing, valid route/direction mapping, and successful unique occurrence assignment.

### 3.5 Tariff Matrices, Pass Schemes, and Concession Policies
- **Distance Tariff Table (`fare_matrix.pdf`):**
  - Exactly 32 distance stages.
  - Boundary convention is **OPEN / PENDING OPERATOR CONFIRMATION** (no assumption that `[min_km, max_km)` is confirmed source behavior).
  - Exclusion constraints are **DEFERRED / INACTIVE** and must not be activated based on an unconfirmed boundary rule.
  - Row 32 (>62 km): municipal adult and child base fares are left blank/NULL; Government Maximum ceiling rate ₹30.00 per 10 km is preserved.00 per 10 km is preserved.
- **Pass Schemes & Concessions (`bus_passes.json`, `New Fare Passes.pdf`):**
  - 90-Day Open-Ended Pass: Verified at ₹5,250 in both sources (FND-024).
  - Companion Concession: 50% fare reduction for one attendant accompanying a blind or 100% disabled pass holder (FND-028).
  - Student Concession: 50% standard student discount; 66.66% in `New Fare Passes.pdf` vs 66.60% (₹2,100) calculated in `bus_passes.json`.
  - Fixed-Route Passes: 1-Month pass equals 20 single fares; 3-Month pass equals 50 single fares.
  - Residence Rules: Municipal limits vs outer suburban routes require verification.

---

## 4. Cross-Dataset Reconciliation Matrix

| Source Entity | Primary Source File | Target Relational Table | Record Cardinality | Integrity Constraint / Guardrail |
| :--- | :--- | :--- | :---: | :--- |
| **Corridors** | `all_routes_stops.json` | `public.routes` | 315 | `route_id` PK; `source_rno` line code; `route_code` generated alias |
| **Stops** | `all_routes_stops.csv` | `public.route_stops` | 12,909 | `route_stop_id` PK; `(route_id, direction_id, stop_sequence)` UNIQUE; 126 duplicate records flagged |
| **Coordinates** | `all_routes_stops.csv` | `public.route_stop_coordinates_audit` | 12,909 | FK `route_stop_id`; `matches_observed_offset_pattern BOOLEAN NOT NULL` |
| **Trips** | `all_route_timetables.csv` | `public.trips` | 3,547 | `trip_id` PK; FK `route_id`; `raw_arrival_text`; `trip_status_code` FK |
| **Trip Status** | `all_route_timetables.csv` | `public.trip_status_lookup` | 4 codes | Code PK; exact counts (0: 2085, 2: 1237, 6: 218, 9: 7); neutral unconfirmed labels |
| **Departures** | `citylinc_timetables.json` | `public.route_departures` | 2,318 tokens | `(route_code, direction, departure_time, occurrence_index)` UNIQUE; string attributes (not nested arrays); independent tracking of route array index, attribute, token index |
| **Tariffs** | `fare_matrix.pdf` | `public.fare_stage_rates` | 32 slabs | `stage_number` PK; FK `tariff_id`; boundary rule OPEN / PENDING OPERATOR CONFIRMATION; exclusion constraint inactive; Row 32 base fares NULL |
| **Passes** | `bus_passes.json`, `New Fare Passes.pdf` | `public.route_pass_rates` | Full matrix | FK `tariff_id`; 90D pass ₹5,250; companion 50%; student 50%/66.66% |

---

## Open Decisions & Unresolved Operator Policies

The following nine transit operational, semantic, and tariff ambiguities cannot be resolved from raw source assets alone and are explicitly retained as **OPEN / PENDING OPERATOR CONFIRMATION**:

1. **ETA Semantics:** Whether `eta` in `all_route_timetables.csv` represents scheduled depot departure time, first revenue stop time, or scheduled arrival at the first timing point, and whether `arrivalatdestination` represents scheduled route completion or vehicle turnaround.
2. **Overnight / Midnight Crossing Handling:** Definition of operational transit service dates across midnight. Specifically, whether trips with `arrivalatdestination < eta` represent next-day arrivals crossing 24:00 or multi-day dispatches, and the exact cut-off hour (e.g., 03:00 / 04:00 AM) for transit operating day rollover.
3. **Fare Slab Boundary Conventions:** Whether distance stages in `fare_matrix.pdf` follow half-open continuous intervals `[min_km, max_km)`, `(min_km, max_km]`, or discrete decimal increments, and how exact boundary distances (e.g., exactly 2.0 km or 4.0 km) are charged. Exclusion constraints must remain inactive and must not be activated based on an unconfirmed boundary rule.
4. **Current Tariff Temporal Validity:** The official municipal gazette notification numbers, legal effective dates (`effective_from`, `effective_to`), and confirmation of whether the 2022/2023 fare matrix remains currently active or has been superseded.
5. **Student Concession Rounding Rules:** Clarification on whether student pass discount is legally 66.66% per `New Fare Passes.pdf` or 66.60% (₹2,100) per `bus_passes.json`, and the mathematical rounding rules applicable to fractional rupee values.
6. **Concession Eligibility & Verification Documentation:** Precise documentation standards, age limits, and municipal residency criteria (inside NMC limits vs outer suburban routes) required for issuing concession passes.
7. **Pass Stacking & Combinability Rules:** Operating rules governing whether passes can be combined with single-journey tickets, transfer discounts, or luggage fees, or whether multiple concessions can be combined.
8. **Route-Specific Pass Calculation Formula:** The exact single-trip base fare or tariff stage applied to compute fixed-route passes (1-month = 20 single trips, 3-month = 50 single trips) on routes with variable stage fares or route variations.
9. **Route Operational Status & Frequency:** Operational status, service frequency, and scheduling policy for the 128 routes in the master registry that lack scheduled trips in `all_route_timetables.csv`.
