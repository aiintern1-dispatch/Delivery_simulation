import random
import datetime
import os
import math
import time
import threading
import sqlite3
from collections import defaultdict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# For optional OSMnx
try:
    import osmnx as ox
    import networkx as nx
    OSMNX_AVAILABLE = True
except ImportError:
    OSMNX_AVAILABLE = False

app = FastAPI()

templates = Jinja2Templates(directory="templates")  # Your HTML files should be here

DATABASE = 'delivery_simulation.db'
auto_order_thread = None
auto_order_running = False

HEXAGON_DRIVERS = defaultdict(list)
HEXAGON_ORDERS = defaultdict(int)
HEXAGON_STATS = {}

ORDERS = []
DRIVERS = []

G = None  # Graph placeholder

# Pydantic models for order requests
class Location(BaseModel):
    lat: float
    lon: float

class OrderRequest(BaseModel):
    pickup: Location
    destination: Location

# --- Your helper functions here, unchanged ---

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    dLat = (lat2 - lat1) * math.pi / 180
    dLon = (lon2 - lon1) * math.pi / 180
    a = math.sin(dLat/2)**2 + math.cos(lat1 * math.pi / 180) * math.cos(lat2 * math.pi / 180) * math.sin(dLon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def find_nearest_driver(pickup_lat, pickup_lon):
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

# --- FastAPI Routes ---

@app.post("/api/orders")
async def create_order(order_request: OrderRequest):
    pickup = order_request.pickup
    destination = order_request.destination
    
    nearest_driver, pickup_distance = find_nearest_driver(pickup.lat, pickup.lon)
    
    if not nearest_driver:
        raise HTTPException(status_code=400, detail="No available drivers found")
    
    delivery_distance = calculate_distance(pickup.lat, pickup.lon, destination.lat, destination.lon)
    total_distance = pickup_distance + delivery_distance
    
    average_speed_kmh = 25
    average_speed_ms = average_speed_kmh * 1000 / 3600
    eta_seconds = total_distance / average_speed_ms
    eta_minutes = int(eta_seconds / 60)
    
    order_id = f'device_order_{int(time.time())}_{random.randint(1000,9999)}'
    
    try:
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
            order_id, nearest_driver['id'], pickup.lat, pickup.lon,
            destination.lat, destination.lon, pickup_distance, delivery_distance,
            total_distance, average_speed_kmh, eta_minutes, 'assigned'
        ))
        cursor.execute('UPDATE drivers SET status = "busy" WHERE id = ?', (nearest_driver['id'],))
        conn.commit()
        conn.close()
        
        return {
            'success': True,
            'order': {
                'id': order_id,
                'pickup': {'lat': pickup.lat, 'lon': pickup.lon},
                'destination': {'lat': destination.lat, 'lon': destination.lon},
                'driver_id': nearest_driver['id'],
                'driver_name': nearest_driver['name'],
                'pickup_distance': pickup_distance,
                'delivery_distance': delivery_distance,
                'total_distance': total_distance,
                'eta_minutes': eta_minutes,
                'average_speed': average_speed_kmh,
                'status': 'assigned'
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Similarly, you can convert other Flask routes like /google, /calculate_route etc.

