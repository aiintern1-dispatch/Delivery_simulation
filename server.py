import random
import datetime
from flask import Flask, render_template, request, jsonify
import os
import requests
import h3
import sqlite3
import threading
import time
from collections import defaultdict
import math

# Make OSMnx/NetworkX optional so the app can run with only OSRM
try:
    import osmnx as ox  # type: ignore
    import networkx as nx  # type: ignore
    OSMNX_AVAILABLE = True
except Exception:
    OSMNX_AVAILABLE = False

app = Flask(__name__)

# Global variable to store the graph.
# We load this once to avoid long loading times on every request.
G = None
DRIVERS = []  # in-memory fleet
ORDERS = []  # deployed orders

# H3 hexagon configuration
H3_RESOLUTION = 8  # Resolution 8 gives hexagons of ~0.74 km² area
HEXAGON_DRIVERS = defaultdict(list)  # hex_id -> list of drivers
HEXAGON_ORDERS = defaultdict(int)  # hex_id -> order count
HEXAGON_STATS = {}  # hex_id -> {driver_count, order_count, density_ratio}

# Database configuration
DATABASE = 'delivery_simulation.db'
AUTO_ORDER_INTERVAL = 30  # seconds between automatic order generation
auto_order_thread = None
auto_order_running = False

def init_database():
    """Initialize the database with required tables."""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Create drivers table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS drivers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            hex_id TEXT,
            status TEXT DEFAULT 'available',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create orders table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            driver_id TEXT,
            pickup_latitude REAL NOT NULL,
            pickup_longitude REAL NOT NULL,
            destination_latitude REAL NOT NULL,
            destination_longitude REAL NOT NULL,
            pickup_distance REAL,
            delivery_distance REAL,
            total_distance REAL,
            average_speed REAL,
            eta_minutes INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (driver_id) REFERENCES drivers (id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("Database initialized successfully")

def get_db_connection():
    """Get database connection."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points using Haversine formula."""
    R = 6371000  # Earth's radius in meters
    dLat = (lat2 - lat1) * math.pi / 180
    dLon = (lon2 - lon1) * math.pi / 180
    a = math.sin(dLat/2) * math.sin(dLat/2) + \
        math.cos(lat1 * math.pi / 180) * math.cos(lat2 * math.pi / 180) * \
        math.sin(dLon/2) * math.sin(dLon/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def find_nearest_driver(pickup_lat, pickup_lon):
    """Find the nearest available driver to the given location."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM drivers WHERE status = 'available'")
    available_drivers = cursor.fetchall()
    conn.close()
    
    if not available_drivers:
        return None, None
    
    nearest_driver = None
    min_distance = float('inf')
    
    for driver in available_drivers:
        distance = calculate_distance(
            driver['latitude'], driver['longitude'],
            pickup_lat, pickup_lon
        )
        if distance < min_distance:
            min_distance = distance
            nearest_driver = driver
    
    return nearest_driver, min_distance

def auto_generate_orders():
    """Automatically generate orders at regular intervals."""
    global auto_order_running
    
    while auto_order_running:
        try:
            # Generate a random order
            center_lat = 18.525
            center_lon = 73.847
            radius_m = 3000
            
            # Generate pickup location
            pickup_dlat = random.uniform(-radius_m, radius_m) / 111320.0
            pickup_dlon = random.uniform(-radius_m, radius_m) / (111320.0 * max(0.0001, math.cos(math.radians(center_lat))))
            pickup_lat = center_lat + pickup_dlat
            pickup_lon = center_lon + pickup_dlon
            
            # Generate destination location (within 2km of pickup)
            dest_radius_m = 2000
            dest_dlat = random.uniform(-dest_radius_m, dest_radius_m) / 111320.0
            dest_dlon = random.uniform(-dest_radius_m, dest_radius_m) / (111320.0 * max(0.0001, math.cos(math.radians(pickup_lat))))
            dest_lat = pickup_lat + dest_dlat
            dest_lon = pickup_lon + dest_dlon
            
            # Find nearest available driver
            nearest_driver, pickup_distance = find_nearest_driver(pickup_lat, pickup_lon)

            # Track hexagon demand for hotspot logic
            try:
                pickup_hex = get_hex_id(pickup_lat, pickup_lon)
                HEXAGON_ORDERS[pickup_hex] += 1
                update_hexagon_stats()
            except Exception:
                pass

            if nearest_driver:
                # Calculate delivery distance
                delivery_distance = calculate_distance(pickup_lat, pickup_lon, dest_lat, dest_lon)
                total_distance = pickup_distance + delivery_distance
                
                # Assume average speed of 25 km/h in city traffic
                average_speed_kmh = 25
                average_speed_ms = average_speed_kmh * 1000 / 3600  # Convert to m/s
                eta_seconds = total_distance / average_speed_ms
                eta_minutes = int(eta_seconds / 60)
                
                # Create order
                order_id = f'auto_order_{int(time.time())}_{random.randint(1000, 9999)}'
                
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO orders (
                        id, driver_id, pickup_latitude, pickup_longitude,
                        destination_latitude, destination_longitude,
                        pickup_distance, delivery_distance, total_distance,
                        average_speed, eta_minutes, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    order_id, nearest_driver['id'], pickup_lat, pickup_lon,
                    dest_lat, dest_lon, pickup_distance, delivery_distance,
                    total_distance, average_speed_kmh, eta_minutes, 'assigned'
                ))
                
                # Update driver status to busy
                cursor.execute('''
                    UPDATE drivers SET status = 'busy' WHERE id = ?
                ''', (nearest_driver['id'],))
                
                conn.commit()
                conn.close()
                
                print(f"Auto-generated order {order_id} assigned to driver {nearest_driver['name']}")
            else:
                # No available driver: create a pending order (unassigned)
                order_id = f'auto_order_{int(time.time())}_{random.randint(1000, 9999)}'
                delivery_distance = calculate_distance(pickup_lat, pickup_lon, dest_lat, dest_lon)
                total_distance = delivery_distance  # no pickup distance since none assigned yet
                average_speed_kmh = 25
                average_speed_ms = average_speed_kmh * 1000 / 3600
                eta_seconds = total_distance / average_speed_ms
                eta_minutes = int(eta_seconds / 60)

                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO orders (
                        id, driver_id, pickup_latitude, pickup_longitude,
                        destination_latitude, destination_longitude,
                        pickup_distance, delivery_distance, total_distance,
                        average_speed, eta_minutes, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''' , (
                    order_id, None, pickup_lat, pickup_lon,
                    dest_lat, dest_lon, None, delivery_distance,
                    total_distance, average_speed_kmh, eta_minutes, 'pending'
                ))
                conn.commit()
                conn.close()
                print(f"Auto-generated pending order {order_id} (no drivers available)")
            
        except Exception as e:
            print(f"Error in auto order generation: {e}")
        
        # Random interval between 10-60 seconds
        time.sleep(random.randint(10, 60))

def start_auto_order_generation():
    """Start automatic order generation in a separate thread."""
    global auto_order_thread, auto_order_running
    
    if auto_order_thread is None or not auto_order_thread.is_alive():
        auto_order_running = True
        auto_order_thread = threading.Thread(target=auto_generate_orders, daemon=True)
        auto_order_thread.start()
        print("Automatic order generation started")

def stop_auto_order_generation():
    """Stop automatic order generation."""
    global auto_order_running
    auto_order_running = False
    print("Automatic order generation stopped")

def load_graph_and_apply_weights():
    """Loads the graph, applies traffic weights, and returns it."""
    if not OSMNX_AVAILABLE:
        raise RuntimeError("OSMnx/NetworkX not available on this server.")
    print("Loading Shivajinagar (Pune) road network...")
    # Use a central point and a radius (in meters) to define the area
    place = (18.528, 73.847)  # A central point in Shivajinagar
    radius = 1500  # A 1.5 km radius

    # Use graph_from_point for a more efficient way to load a specific area
    G_loaded = ox.graph_from_point(place, dist=radius, network_type="drive")
    print("Graph loaded with", len(G_loaded.nodes), "nodes and", len(G_loaded.edges), "edges")

    print("Simulating traffic conditions...")
    for u, v, k, data in G_loaded.edges(keys=True, data=True):
        base_speed = 40  # default speed (km/h)
        
        # Robustly handle maxspeed attribute
        try:
            maxspeed_val = data.get("maxspeed")
            if isinstance(maxspeed_val, list):
                speeds = [int(str(s).split()[0]) for s in maxspeed_val]
                base_speed = sum(speeds) / len(speeds)
            elif maxspeed_val:
                base_speed = int(str(maxspeed_val).split()[0])
        except (AttributeError, ValueError):
            pass
            
        # Calculate travel time in minutes
        travel_time = (data["length"] / 1000) / base_speed * 60
        
        # Apply a random congestion factor
        congestion = random.uniform(2, 5)
        
        # Add a time-of-day traffic multiplier for peak hours
        current_hour = datetime.datetime.now().hour
        if 8 <= current_hour <= 10 or 17 <= current_hour <= 19:
            congestion *= 3.0
        
        # The final weight is travel time adjusted for congestion
        data["weight"] = travel_time * congestion
    
    return G_loaded

@app.route('/')
def index():
    """Renders the main HTML page for the web interface."""
    return render_template('index.html')

@app.route('/google')
def google_maps():
    """Renders the Google Maps-based UI and injects API key."""
    api_key = os.getenv('GOOGLE_MAPS_API_KEY', '')
    return render_template('index_google.html', google_maps_api_key=api_key)

@app.route('/calculate_route', methods=['POST'])
def calculate_route():
    """API endpoint to calculate the shortest path and return it as JSON."""
    if not OSMNX_AVAILABLE:
        return jsonify({'success': False, 'error': 'OSMnx routing not available on this server.'}), 501
    global G
    if G is None:
        G = load_graph_and_apply_weights()

    data = request.get_json()
    start_lat = data.get('start_lat')
    start_lon = data.get('start_lon')
    end_lat = data.get('end_lat')
    end_lon = data.get('end_lon')

    if not all([start_lat, start_lon, end_lat, end_lon]):
        return jsonify({'success': False, 'error': 'Invalid coordinates provided'}), 400

    print("Finding shortest path...")
    try:
        # Find the nearest nodes in the graph
        start_node = ox.nearest_nodes(G, start_lon, start_lat)
        end_node = ox.nearest_nodes(G, end_lon, end_lat)
        
        # Compute the shortest path based on the 'weight' (travel time)
        route = nx.shortest_path(G, source=start_node, target=end_node, weight="weight")
        
        # Get the coordinates for the nodes in the route
        route_coords = [[G.nodes[n]['y'], G.nodes[n]['x']] for n in route]

        # Calculate total travel time
        travel_time = sum(ox.utils_graph.get_route_edge_attributes(G, route, "weight"))

        return jsonify({
            'success': True,
            'route_coords': route_coords,
            'travel_time_minutes': round(travel_time, 2)
        })

    except nx.NetworkXNoPath:
        return jsonify({
            'success': False,
            'error': 'No path found between the selected points.'
        }), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/osrm_route', methods=['POST'])
def osrm_route():
    """Proxy to OSRM to avoid CORS and normalize response for the frontend.

    Expects JSON body with: start_lat, start_lon, end_lat, end_lon
    Returns: route_coords (list[[lat, lon]]), distance_meters, duration_seconds, eta_20kmh_seconds
    """
    data = request.get_json(silent=True) or {}
    start_lat = data.get('start_lat')
    start_lon = data.get('start_lon')
    end_lat = data.get('end_lat')
    end_lon = data.get('end_lon')

    if not all([start_lat, start_lon, end_lat, end_lon]):
        return jsonify({'success': False, 'error': 'Invalid coordinates provided'}), 400

    base_url = os.getenv('OSRM_BASE_URL', 'http://122.170.240.52:5001')
    profile = os.getenv('OSRM_PROFILE', 'driving')
    url = (
        f"{base_url}/route/v1/{profile}/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson&alternatives=false&steps=false"
    )

    try:
        resp = requests.get(url, timeout=15)
        if not resp.ok:
            return jsonify({'success': False, 'error': f'OSRM error {resp.status_code}'}), 502
        payload = resp.json()
        routes = payload.get('routes') or []
        if not routes:
            return jsonify({'success': False, 'error': 'No route found by OSRM.'}), 404

        route = routes[0]
        geometry = (route.get('geometry') or {}).get('coordinates') or []
        # Convert [lon, lat] -> [lat, lon] for Leaflet
        route_coords = [[latlon[1], latlon[0]] for latlon in geometry]
        distance_meters = float(route.get('distance', 0.0))
        duration_seconds = float(route.get('duration', 0.0))
        # ETA at 20 km/h
        meters_per_second = 20000.0 / 3600.0
        eta_20kmh_seconds = distance_meters / meters_per_second if meters_per_second > 0 else 0.0

        return jsonify({
            'success': True,
            'route_coords': route_coords,
            'distance_meters': round(distance_meters, 2),
            'duration_seconds': round(duration_seconds, 2),
            'eta_20kmh_seconds': round(eta_20kmh_seconds, 2)
        })
    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'OSRM request failed: {str(e)}'}), 502

def _haversine_meters(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(h))

def get_hex_id(lat, lon):
    """Get H3 hexagon ID for given coordinates."""
    return h3.latlng_to_cell(lat, lon, H3_RESOLUTION)

def get_hex_boundary(hex_id):
    """Get hexagon boundary coordinates for visualization."""
    boundary = h3.cell_to_boundary(hex_id)
    return [[lat, lon] for lon, lat in boundary]

def get_nearby_hexagons(hex_id, k=1):
    """Get nearby hexagons (including the center one)."""
    nearby = h3.grid_disk(hex_id, k)
    return list(nearby)

def update_hexagon_stats():
    """Update hexagon statistics for density analysis."""
    global HEXAGON_STATS
    HEXAGON_STATS = {}
    
    for hex_id in set(list(HEXAGON_DRIVERS.keys()) + list(HEXAGON_ORDERS.keys())):
        driver_count = len(HEXAGON_DRIVERS[hex_id])
        order_count = HEXAGON_ORDERS[hex_id]
        
        # Calculate density ratio (orders per driver)
        density_ratio = order_count / max(driver_count, 1)
        
        HEXAGON_STATS[hex_id] = {
            'driver_count': driver_count,
            'order_count': order_count,
            'density_ratio': density_ratio,
            'status': 'balanced'  # balanced, overloaded, underutilized
        }
        
        # Determine status based on density ratio
        if density_ratio > 3:  # More than 3 orders per driver
            HEXAGON_STATS[hex_id]['status'] = 'overloaded'
        elif density_ratio < 0.5:  # Less than 0.5 orders per driver
            HEXAGON_STATS[hex_id]['status'] = 'underutilized'

@app.post('/api/deploy_drivers')
def deploy_drivers_flask():
    """Deploy random drivers around a center point using H3 hexagons."""
    data = request.get_json(silent=True) or {}
    center_lat = float(data.get('lat', 18.525))
    center_lon = float(data.get('lon', 73.847))
    try:
        count = int(request.args.get('count', 35))
        radius_m = int(request.args.get('radius_m', 2000))
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid query params'}), 400

    global DRIVERS, HEXAGON_DRIVERS
    DRIVERS = []
    HEXAGON_DRIVERS.clear()  # Clear previous hexagon data

    # Clear existing drivers from database
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM drivers")
    conn.commit()
    conn.close()
    
    for i in range(count):
        dlat = random.uniform(-radius_m, radius_m) / 111320.0
        dlon = random.uniform(-radius_m, radius_m) / (111320.0 * max(0.0001, math.cos(math.radians(center_lat))))
        lat = center_lat + dlat
        lon = center_lon + dlon
        
        driver = {
            'id': f'drv_{i+1}',
            'name': f'Driver {i+1}',
            'location': {'lat': lat, 'lon': lon},
        }
        DRIVERS.append(driver)
        
        # Add driver to hexagon
        hex_id = get_hex_id(lat, lon)
        HEXAGON_DRIVERS[hex_id].append(driver)

        # Save driver to database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO drivers (id, name, latitude, longitude, hex_id, status)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (driver['id'], driver['name'], lat, lon, hex_id, 'available'))
        conn.commit()
        conn.close()
    
    # Update hexagon statistics
    update_hexagon_stats()

    # Start automatic order generation if not already running
    start_auto_order_generation()
    
    return jsonify({'success': True, 'drivers': DRIVERS})

@app.get('/api/drivers')
def list_drivers_flask():
    return jsonify({'drivers': DRIVERS})

@app.get('/api/drivers_db')
def get_drivers_from_db():
    """Get all drivers from database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM drivers ORDER BY created_at DESC")
        drivers = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'success': True, 'drivers': drivers})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.get('/api/orders_db')
def get_orders_from_db():
    """Get all orders from database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.*, d.name as driver_name 
            FROM orders o 
            LEFT JOIN drivers d ON o.driver_id = d.id 
            ORDER BY o.created_at DESC
        ''')
        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({'success': True, 'orders': orders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.post('/api/start_auto_orders')
def start_auto_orders():
    """Start automatic order generation."""
    try:
        start_auto_order_generation()
        return jsonify({'success': True, 'message': 'Automatic order generation started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.post('/api/stop_auto_orders')
def stop_auto_orders():
    """Stop automatic order generation."""
    try:
        stop_auto_order_generation()
        return jsonify({'success': True, 'message': 'Automatic order generation stopped'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.get('/api/auto_order_status')
def get_auto_order_status():
    """Get automatic order generation status."""
    global auto_order_running
    return jsonify({
        'success': True, 
        'running': auto_order_running,
        'interval_seconds': AUTO_ORDER_INTERVAL
    })

@app.get('/api/latest_auto_orders')
def get_latest_auto_orders():
    """Get the latest auto-generated orders for display on map."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.*, d.name as driver_name 
            FROM orders o 
            LEFT JOIN drivers d ON o.driver_id = d.id 
            WHERE o.id LIKE 'auto_order_%'
            ORDER BY o.created_at DESC
            LIMIT 50
        ''')
        orders = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        # Convert to the format expected by frontend
        formatted_orders = []
        for order in orders:
            formatted_order = {
                'id': order['id'],
                'pickup': {
                    'lat': order['pickup_latitude'],
                    'lon': order['pickup_longitude']
                },
                'destination': {
                    'lat': order['destination_latitude'],
                    'lon': order['destination_longitude']
                },
                'driver_id': order['driver_id'],
                'driver_name': order['driver_name'],
                'pickup_distance': order['pickup_distance'],
                'delivery_distance': order['delivery_distance'],
                'total_distance': order['total_distance'],
                'eta_minutes': order['eta_minutes'],
                'average_speed': order['average_speed'],
                'status': order['status'],
                'created_at': order['created_at']
            }
            formatted_orders.append(formatted_order)
        
        return jsonify({'success': True, 'orders': formatted_orders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.post('/api/complete_delivery')
def complete_delivery():
    """Mark a delivery as completed and assign driver to next order or hotspot."""
    data = request.get_json(silent=True) or {}
    order_id = data.get('order_id')
    driver_id = data.get('driver_id')
    
    if not order_id or not driver_id:
        return jsonify({'success': False, 'error': 'Missing order_id or driver_id'}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get order details
        cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
        order = cursor.fetchone()
        if not order:
            return jsonify({'success': False, 'error': 'Order not found'}), 404
        
        # Update order status to delivered
        cursor.execute('UPDATE orders SET status = "delivered" WHERE id = ?', (order_id,))
        
        # Update driver status to available
        cursor.execute('UPDATE drivers SET status = "available" WHERE id = ?', (driver_id,))
        
        # Update driver location to destination
        cursor.execute('''
            UPDATE drivers 
            SET latitude = ?, longitude = ? 
            WHERE id = ?
        ''', (order['destination_latitude'], order['destination_longitude'], driver_id))
        
        conn.commit()
        conn.close()

        # Decrement hotspot demand for the pickup hex of this order
        try:
            pickup_hex = get_hex_id(order['pickup_latitude'], order['pickup_longitude'])
            if HEXAGON_ORDERS.get(pickup_hex, 0) > 0:
                HEXAGON_ORDERS[pickup_hex] -= 1
            update_hexagon_stats()
        except Exception:
            pass
        
        # Find next order for driver or send to hotspot
        assign_driver_to_next_order_or_hotspot(driver_id)
        
        return jsonify({'success': True, 'message': 'Delivery completed successfully'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def assign_driver_to_next_order_or_hotspot(driver_id):
    """Assign driver to next available order or send to hotspot if no orders."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get driver location
        cursor.execute('SELECT latitude, longitude FROM drivers WHERE id = ?', (driver_id,))
        driver = cursor.fetchone()
        
        if not driver:
            return
        
        # Check for pending orders
        cursor.execute('''
            SELECT * FROM orders 
            WHERE status = 'pending' 
            ORDER BY created_at ASC
        ''')
        pending_orders = cursor.fetchall()
        
        if pending_orders:
            # Find nearest pending order
            nearest_order = None
            min_distance = float('inf')
            
            for order in pending_orders:
                distance = calculate_distance(
                    driver['latitude'], driver['longitude'],
                    order['pickup_latitude'], order['pickup_longitude']
                )
                if distance < min_distance:
                    min_distance = distance
                    nearest_order = order
            
            if nearest_order:
                # Assign order to driver
                cursor.execute('''
                    UPDATE orders 
                    SET driver_id = ?, status = 'assigned', pickup_distance = ? , total_distance = COALESCE(pickup_distance, 0) + COALESCE(delivery_distance, 0)
                    WHERE id = ?
                ''', (driver_id, min_distance, nearest_order['id']))
                
                # Update driver status to busy
                cursor.execute('''
                    UPDATE drivers SET status = 'busy' WHERE id = ?
                ''', (driver_id,))
                
                conn.commit()
                print(f"Driver {driver_id} assigned to order {nearest_order['id']}")
        else:
            # No pending orders, send driver to hotspot
            send_driver_to_hotspot(driver_id, driver['latitude'], driver['longitude'])
        
        conn.close()
    except Exception as e:
        print(f"Error in assign_driver_to_next_order_or_hotspot: {e}")

def send_driver_to_hotspot(driver_id, current_lat, current_lon):
    """Send driver to a high-demand area (hotspot)."""
    try:
        # Find the hexagon with the highest order density
        hotspot_hex_id = None
        max_density = 0
        
        for hex_id, stats in HEXAGON_STATS.items():
            if stats['order_count'] > 0 and stats['density_ratio'] > max_density:
                max_density = stats['density_ratio']
                hotspot_hex_id = hex_id
        
        if hotspot_hex_id:
            # Get center of hotspot hexagon
            hex_center = h3.cell_to_latlng(hotspot_hex_id)
            
            # Update driver's destination to hotspot
            # In a real implementation, we would store this destination
            print(f"Driver {driver_id} sent to hotspot at {hex_center}")
            
            # For now, we just update the driver's status
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE drivers SET status = 'moving_to_hotspot' WHERE id = ?
            ''', (driver_id,))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"Error in send_driver_to_hotspot: {e}")

@app.get('/api/orders')
def get_orders_for_heatmap():
    """Return recent orders in a lightweight format for heatmap/UI."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.*, d.name as driver_name 
            FROM orders o 
            LEFT JOIN drivers d ON o.driver_id = d.id 
            ORDER BY o.created_at DESC
            LIMIT 200
        ''')
        rows = cursor.fetchall()
        conn.close()

        orders = []
        for row in rows:
            orders.append({
                'id': row['id'],
                'pickup': {'lat': row['pickup_latitude'], 'lon': row['pickup_longitude']},
                'destination': {'lat': row['destination_latitude'], 'lon': row['destination_longitude']},
                'driver_id': row['driver_id'],
                'driver_name': row['driver_name'],
                'pickup_distance': row['pickup_distance'] or 0.0,
                'delivery_distance': row['delivery_distance'] or 0.0,
                'total_distance': row['total_distance'] or 0.0,
                'status': row['status'],
                'created_at': row['created_at']
            })
        return jsonify({'success': True, 'orders': orders})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.get('/api/osrm_route')
def get_osrm_route():
    """Get OSRM route between two points."""
    start_lat = request.args.get('start_lat')
    start_lon = request.args.get('start_lon')
    end_lat = request.args.get('end_lat')
    end_lon = request.args.get('end_lon')
    
    if not all([start_lat, start_lon, end_lat, end_lon]):
        return jsonify({'success': False, 'error': 'Missing coordinates'}), 400
    
    try:
        start_lat = float(start_lat)
        start_lon = float(start_lon)
        end_lat = float(end_lat)
        end_lon = float(end_lon)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid coordinates'}), 400
    
    # Call OSRM directly
    base_url = os.getenv('OSRM_BASE_URL', 'http://122.170.240.52:5001')
    profile = os.getenv('OSRM_PROFILE', 'driving')
    url = (
        f"{base_url}/route/v1/{profile}/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson&alternatives=true&steps=false"
    )
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('code') == 'Ok' and data.get('routes'):
            route = data['routes'][0]
            return jsonify({
                'success': True,
                'route': {
                    'geometry': route['geometry'],
                    'distance': route['distance'],
                    'duration': route['duration']
                }
            })
        else:
            return jsonify({'success': False, 'error': 'No route found'}), 404
            
    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'OSRM request failed: {str(e)}'}), 500

@app.get('/api/order_details/<order_id>')
def get_order_details(order_id):
    """Get detailed information about a specific order."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.*, d.name as driver_name 
            FROM orders o 
            LEFT JOIN drivers d ON o.driver_id = d.id 
            WHERE o.id = ?
        ''', (order_id,))
        order = cursor.fetchone()
        conn.close()
        
        if not order:
            return jsonify({'success': False, 'error': 'Order not found'}), 404
        
        return jsonify({
            'success': True,
            'order': {
                'id': order['id'],
                'driver_id': order['driver_id'],
                'driver_name': order['driver_name'],
                'pickup': {
                    'lat': order['pickup_latitude'],
                    'lon': order['pickup_longitude']
                },
                'destination': {
                    'lat': order['destination_latitude'],
                    'lon': order['destination_longitude']
                },
                'pickup_distance': order['pickup_distance'],
                'delivery_distance': order['delivery_distance'],
                'total_distance': order['total_distance'],
                'average_speed': order['average_speed'],
                'eta_minutes': order['eta_minutes'],
                'status': order['status'],
                'created_at': order['created_at']
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    # Initialize database
    init_database()
    
    # Do not preload OSMnx graph to keep startup light if OSMnx is not installed
    app.run(debug=True, host="0.0.0.0", port=8000)
