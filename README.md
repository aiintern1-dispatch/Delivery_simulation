# Delivery_simulation
## Project Plan: Interactive Web Map with Routing and Simulation

### Overview
Build a web interface using Leaflet with OpenStreetMap tiles. Users choose an origin and destination on the map; the app requests a route from a locally running OSRM server (`http://192.168.1.25:5001`) and displays the route, distance, and travel time. A marker animates along the route at an average speed of 20 km/h.

### Architecture
- **Frontend**: Leaflet map + vanilla JS (or lightweight framework-free) served via existing `server.py` (Flask) and `templates/index.html`.
- **Backend**: Only for static templating/serving. OSRM is called directly from the browser (CORS permitting). If CORS is blocked, proxy via Flask.
- **External Service**: OSRM API at `http://192.168.1.25:5001`.

### OSRM API Usage (planned per OSRM v5 API)
- **Service**: `route`
- **HTTP**: `GET`
- **URL template**:
  - `http://192.168.1.25:5001/route/v1/{profile}/{lon1},{lat1};{lon2},{lat2}`
- **Common query params**:
  - `overview=full` (full geometry for display)
  - `geometries=geojson` (Leaflet friendly)
  - `alternatives=false` (single route)
  - `steps=false` (we only need overall geometry/time)
  - `annotations=distance,duration` (optional; per-segment metrics if needed)
  - `continue_straight=default` (default behavior)
- **Profiles**: `driving`, `walking`, `cycling` (we will default to `driving`).
- **Response fields used**:
  - `routes[0].geometry` (GeoJSON LineString coordinates `[lon, lat]`)
  - `routes[0].distance` (meters)
  - `routes[0].duration` (seconds)
  - `waypoints` (echo of snapped O/D)

Example request:
```
GET http://192.168.1.25:5001/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson&alternatives=false&steps=false
```

### Features & Tasks
1) Map UI and layout
   - Initialize Leaflet map centered to a sensible default.
   - Add OSM tile layer with attribution.
   - Responsive container sizing.

2) Origin/Destination selection
   - Single-click map to set origin; second click sets destination.
   - Show markers for origin (green) and destination (red).
   - Add reset/clear controls.

3) OSRM integration
   - Build a small fetch wrapper for OSRM route endpoint.
   - Validate inputs; format coordinates as `lon,lat`.
   - Parse response (geometry, distance, duration) and handle errors.

4) Route display
   - Convert GeoJSON LineString coordinates to Leaflet polyline.
   - Fit map bounds to the route.
   - Style route (color, weight).

5) Distance and time
   - Display route distance (km, 1 decimal).
   - Compute ETA at 20 km/h: `etaSeconds = (distanceMeters / (20 * 1000)) * 3600`.
   - Display both OSRM duration (if desired) and 20 km/h ETA for comparison.

6) Animation
   - Convert route geometry to a densified polyline for smooth animation.
   - Create a marker and move it along the route at 20 km/h.
   - Use requestAnimationFrame; distance-based interpolation for consistent speed.
   - Controls: start, pause/resume, reset; disable/enable appropriately.
   - Keep camera either static or optionally follow the marker.

7) Robustness & UX
   - Loading states while fetching OSRM.
   - Error states (invalid O/D, network/OSRM error, no route found).
   - Handle CORS; if needed, add Flask proxy endpoint `/api/route`.

8) Configuration
   - Centralize `OSRM_BASE_URL` and `PROFILE`.
   - Allow override via environment variable or data-attribute in HTML.

9) Testing
   - Manual tests across Chrome/Firefox/Edge.
   - Validate with several distant O/D pairs.

10) Documentation
   - Inline code comments for key functions.
   - Usage instructions in README.

### Data Flow (happy path)
1. User clicks origin → app stores `originLatLng` and drops marker.
2. User clicks destination → app stores `destLatLng` and drops marker.
3. App calls OSRM `route` with coords and params.
4. App parses response, draws polyline, shows stats.
5. User clicks Start → marker animates along route at 20 km/h; ETA reflects this speed.

### Pseudocode for Key Parts
Route request:
```javascript
const url = `${OSRM_BASE_URL}/route/v1/${PROFILE}/${lon1},${lat1};${lon2},${lat2}?overview=full&geometries=geojson&alternatives=false&steps=false`;
const res = await fetch(url);
if (!res.ok) throw new Error(`OSRM error ${res.status}`);
const data = await res.json();
const route = data.routes?.[0];
```

Animation at 20 km/h:
```javascript
const metersPerSecond = 20000 / 3600;
// advance along polyline by metersPerSecond * deltaTimeSeconds each frame
```

### Acceptance Criteria
- User can set origin and destination by clicking on the map.
- Route renders on the map; map fits to route bounds.
- Distance (km) and ETA at 20 km/h are displayed.
- Marker animates along the route at an average of 20 km/h.
- Controls: Start, Pause/Resume, Reset work as expected.
- OSRM server at `192.168.1.25:5001` is used (configurable).

