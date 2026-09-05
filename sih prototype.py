
import os
import sqlite3
import itertools
import json
import math
import uuid
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "farm_to_market.db"

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()

# Lower score = better fulfillment plan.
PRODUCT_COST_WEIGHT = 1.0
DELIVERY_COST_WEIGHT = 1.0
DISTANCE_WEIGHT = 0.20
TIME_WEIGHT = 0.05
FARMER_COUNT_WEIGHT = 20.0
EXCESS_WEIGHT = 2.0

DELIVERY_COST_PER_KM = 12.0

DEMO_COORDINATES = {
    "najafgarh, delhi": (28.6127, 76.9855),
    "mehrauli, delhi": (28.5244, 77.1855),
    "alipur, delhi": (28.7983, 77.1350),
    "dwarka, delhi": (28.5921, 77.0460),
    "delhi central": (28.6448, 77.2167),
    "rohini, delhi": (28.7324, 77.0966),
    "gurugram": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
}

VALID_ORDER_TRANSITIONS = {
    "PENDING_PAYMENT": {"PAID"},
    "PAID": {"FARMERS_ASSIGNED"},
    "FARMERS_ASSIGNED": {"FARMER_CONFIRMATION"},
    "FARMER_CONFIRMATION": {"DELIVERY_ASSIGNED"},
    "DELIVERY_ASSIGNED": {"PICKUP_STARTED"},
    "PICKUP_STARTED": {"PARTIALLY_PICKED_UP", "ALL_ITEMS_PICKED_UP"},
    "PARTIALLY_PICKED_UP": {"PARTIALLY_PICKED_UP", "ALL_ITEMS_PICKED_UP"},
    "ALL_ITEMS_PICKED_UP": {"OUT_FOR_DELIVERY"},
    "OUT_FOR_DELIVERY": {"DELIVERED"},
    "DELIVERED": {"COMPLETED"},
    "COMPLETED": set(),
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value):
    return f"₹{float(value):,.2f}"


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(text):
    return " ".join(str(text).strip().lower().split())


def generate_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"


def pause():
    input("\nPress Enter to continue...")


def banner(title):
    print("\n" + "=" * 70)
    print(title.center(70))
    print("=" * 70)


# ============================================================
# DATABASE
# ============================================================

class Database:
    def __init__(self, path=DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.create_tables()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def create_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS farmers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            contact TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS consumers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumer_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            contact TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS delivery_executives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            executive_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            contact TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            farmer_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            available_quantity REAL DEFAULT 0,
            price_per_kg REAL NOT NULL,
            expected_harvest_date TEXT,
            expected_harvest_quantity REAL DEFAULT 0,
            address TEXT,
            latitude REAL,
            longitude REAL,
            availability_status TEXT DEFAULT 'AVAILABLE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(farmer_id) REFERENCES farmers(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT UNIQUE NOT NULL,
            consumer_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            delivery_address TEXT,
            delivery_latitude REAL,
            delivery_longitude REAL,
            product_cost REAL DEFAULT 0,
            delivery_cost REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            route_json TEXT,
            distance_km REAL DEFAULT 0,
            eta_minutes REAL DEFAULT 0,
            optimization_score REAL DEFAULT 0,
            payment_status TEXT DEFAULT 'PENDING',
            order_status TEXT DEFAULT 'PENDING_PAYMENT',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(consumer_id) REFERENCES consumers(id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS farmer_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            farmer_id INTEGER NOT NULL,
            product_id INTEGER,
            allocated_quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            confirmation_status TEXT DEFAULT 'PENDING',
            picked_up INTEGER DEFAULT 0,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY(farmer_id) REFERENCES farmers(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_code TEXT UNIQUE NOT NULL,
            order_id INTEGER NOT NULL,
            executive_id INTEGER,
            current_stop INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ASSIGNED',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,
            FOREIGN KEY(executive_id) REFERENCES delivery_executives(id)
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_code TEXT UNIQUE NOT NULL,
            order_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            provider_payment_id TEXT,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_type TEXT NOT NULL,
            recipient_id INTEGER,
            order_id INTEGER,
            message TEXT NOT NULL,
            channel TEXT DEFAULT 'DEMO_TERMINAL',
            sent_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL
        );
        """)
        self.conn.commit()


# ============================================================
# LOCATION / GOOGLE MAPS
# ============================================================

def demo_coordinates(address):
    key = normalize(address)

    if key in DEMO_COORDINATES:
        return DEMO_COORDINATES[key]

    for place, coords in DEMO_COORDINATES.items():
        if place in key:
            return coords

    # Generic Delhi demonstration point.
    return DEMO_COORDINATES["delhi central"]


def get_coordinates(address):
    """
    REAL MODE:
        Google Geocoding API

    DEMO MODE:
        Known Delhi demo coordinates / Delhi Central fallback.

    The program never claims a demo coordinate is a real API result.
    """
    if GOOGLE_MAPS_API_KEY and requests:
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                "address": address,
                "key": GOOGLE_MAPS_API_KEY,
            }

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "OK" and data.get("results"):
                location = data["results"][0]["geometry"]["location"]
                print("[REAL API] Google Geocoding successful.")
                return float(location["lat"]), float(location["lng"])

            print("[REAL API] Google Geocoding returned no valid result.")
            print("[DEMO FALLBACK] Using demonstration coordinates.")

        except Exception as exc:
            print(f"[REAL API ERROR] Google Geocoding failed: {exc}")
            print("[DEMO FALLBACK] Using demonstration coordinates.")

    else:
        print("[DEMO MODE] Google Maps API key is not configured.")
        print("[DEMO MODE] Using demonstration coordinates.")

    return demo_coordinates(address)


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )

    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_route(origin, destination):
    """
    Returns:
        distance_km
        duration_minutes
        mode
    """
    if GOOGLE_MAPS_API_KEY and requests:
        try:
            url = "https://maps.googleapis.com/maps/api/directions/json"
            params = {
                "origin": f"{origin[0]},{origin[1]}",
                "destination": f"{destination[0]},{destination[1]}",
                "key": GOOGLE_MAPS_API_KEY,
            }

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "OK" and data.get("routes"):
                leg = data["routes"][0]["legs"][0]

                return {
                    "distance_km": leg["distance"]["value"] / 1000.0,
                    "duration_minutes": leg["duration"]["value"] / 60.0,
                    "mode": "REAL",
                }

            print("[REAL API] Google Directions returned no valid route.")
            print("[DEMO FALLBACK] Using demonstration route estimate.")

        except Exception as exc:
            print(f"[REAL API ERROR] Google Directions failed: {exc}")
            print("[DEMO FALLBACK] Using demonstration route estimate.")

    else:
        print("[DEMO MODE] Google Maps routing unavailable; using demo estimate.")

    straight = haversine_km(
        origin[0], origin[1],
        destination[0], destination[1]
    )

    # This is explicitly a demonstration road estimate.
    road_distance = straight * 1.25
    duration = (road_distance / 25.0) * 60.0

    return {
        "distance_km": road_distance,
        "duration_minutes": duration,
        "mode": "DEMO",
    }


def get_distance_matrix(origins, destinations):
    matrix = []

    for origin in origins:
        row = []
        for destination in destinations:
            row.append(get_route(origin, destination))
        matrix.append(row)

    return matrix


# ============================================================
# NOTIFICATIONS
# ============================================================

def send_notification(db, recipient_type, recipient_id, message, order_id=None):
    """
    Base prototype notification system.

    Notifications are stored in SQLite and printed to the terminal.
    This avoids pretending that a Firebase push was delivered when
    no device-token infrastructure has been configured.
    """
    print("\n[NOTIFICATION]")
    print(f"{recipient_type}: {message}")

    db.execute("""
        INSERT INTO notifications
        (recipient_type, recipient_id, order_id, message, channel, sent_at)
        VALUES (?, ?, ?, ?, 'DEMO_TERMINAL', ?)
    """, (
        recipient_type,
        recipient_id,
        order_id,
        message,
        now(),
    ))


# ============================================================
# PAYMENT
# ============================================================

def create_payment(db, order_id, amount):
    payment_code = generate_id("PAY")

    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        provider = "RAZORPAY_TEST"
        status = "PROCESSING"

        print("\n[RAZORPAY TEST MODE]")
        print(f"Test payment order prepared for {money(amount)}.")
        print("No real-money transaction is performed by this prototype.")

    else:
        provider = "DEMO_PAYMENT"
        status = "PROCESSING"

        print("\n[DEMO PAYMENT]")
        print(f"Order amount: {money(amount)}")
        print("Razorpay test credentials are not configured.")
        print("This is a local demonstration payment.")

    db.execute("""
        INSERT INTO payments
        (payment_code, order_id, provider, provider_payment_id,
         amount, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        payment_code,
        order_id,
        provider,
        None,
        amount,
        status,
        now(),
        now(),
    ))

    return payment_code


def verify_payment(db, order_id, success=True):
    payment = db.fetchone("""
        SELECT *
        FROM payments
        WHERE order_id=?
        ORDER BY id DESC
        LIMIT 1
    """, (order_id,))

    if not payment:
        print("Payment record not found.")
        return False

    if not success:
        db.execute("""
            UPDATE payments
            SET status='FAILED', updated_at=?
            WHERE id=?
        """, (now(), payment["id"]))

        db.execute("""
            UPDATE orders
            SET payment_status='FAILED', updated_at=?
            WHERE id=?
        """, (now(), order_id))

        print("Payment failed.")
        return False

    if payment["provider"] == "RAZORPAY_TEST":
        provider_payment_id = generate_id("RZTEST")
    else:
        provider_payment_id = generate_id("DEMO")

    db.execute("""
        UPDATE payments
        SET provider_payment_id=?, status='PAID', updated_at=?
        WHERE id=?
    """, (
        provider_payment_id,
        now(),
        payment["id"],
    ))

    db.execute("""
        UPDATE orders
        SET payment_status='PAID', updated_at=?
        WHERE id=?
    """, (now(), order_id))

    print(f"Payment successful. Payment ID: {provider_payment_id}")
    return True


# ============================================================
# ORDER STATE MACHINE
# ============================================================

def transition_order(db, order_id, new_status):
    order = db.fetchone("""
        SELECT order_status
        FROM orders
        WHERE id=?
    """, (order_id,))

    if not order:
        print("Order not found.")
        return False

    current = order["order_status"]

    if new_status not in VALID_ORDER_TRANSITIONS.get(current, set()):
        print(f"Invalid order transition: {current} -> {new_status}")
        return False

    db.execute("""
        UPDATE orders
        SET order_status=?, updated_at=?
        WHERE id=?
    """, (new_status, now(), order_id))

    return True


# ============================================================
# CORE OPTIMIZER
# ============================================================

def allocate_exact_quantity(farmers, demand):
    """
    Allocate exactly the requested quantity from the selected
    farmer combination.

    Within the already-selected combination, lower-price farmers
    are used first, then shorter distance.
    """
    remaining = demand
    allocations = []

    ordered = sorted(
        farmers,
        key=lambda f: (
            safe_float(f["price_per_kg"]),
            safe_float(f.get("distance_to_consumer", 999999)),
            f["farmer_id"],
        ),
    )

    for farmer in ordered:
        if remaining <= 1e-9:
            break

        available = safe_float(farmer["available_quantity"])
        take = min(remaining, available)

        if take > 0:
            allocations.append({
                "farmer_id": farmer["farmer_id"],
                "farmer_name": farmer["farmer_name"],
                "product_id": farmer["product_id"],
                "quantity": take,
                "unit_price": safe_float(farmer["price_per_kg"]),
            })

            remaining -= take

    if remaining <= 1e-9:
        return allocations

    return None


def route_for_permutation(permutation, consumer_coord):
    total_distance = 0.0
    total_time = 0.0

    current = consumer_coord

    for farmer in permutation:
        destination = (
            farmer["latitude"],
            farmer["longitude"],
        )

        route = get_route(current, destination)

        total_distance += route["distance_km"]
        total_time += route["duration_minutes"]

        current = destination

    final_route = get_route(current, consumer_coord)

    total_distance += final_route["distance_km"]
    total_time += final_route["duration_minutes"]

    return total_distance, total_time


def optimize_fulfillment(
    db,
    product_name,
    demand,
    consumer_coord,
    verbose=True,
    excluded_farmer_ids=None,
):
    """
    Core SIH optimization logic.

    1. Find active farmers with available stock.
    2. Generate ALL non-empty farmer combinations.
    3. Reject combinations with insufficient supply.
    4. Allocate the exact required quantity.
    5. For every feasible combination, evaluate ALL pickup permutations.
    6. Calculate product cost, delivery cost and score.
    7. Select the lowest-score candidate.

    The Google Maps code is deliberately outside this function's
    decision logic; this function only consumes route values.
    """
    excluded_farmer_ids = excluded_farmer_ids or set()

    rows = db.fetchall("""
        SELECT
            p.id AS product_id,
            p.farmer_id AS farmer_id,
            f.name AS farmer_name,
            p.product_name,
            p.available_quantity,
            p.price_per_kg,
            p.expected_harvest_date,
            p.latitude,
            p.longitude,
            f.active
        FROM products p
        JOIN farmers f ON f.id=p.farmer_id
        WHERE LOWER(p.product_name)=LOWER(?)
          AND p.available_quantity>0
          AND p.availability_status='AVAILABLE'
          AND f.active=1
        ORDER BY p.id
    """, (product_name,))

    farmers = []

    for row in rows:
        farmer = dict(row)

        if farmer["farmer_id"] in excluded_farmer_ids:
            continue

        route = get_route(
            (farmer["latitude"], farmer["longitude"]),
            consumer_coord,
        )

        farmer["distance_to_consumer"] = route["distance_km"]
        farmer["time_to_consumer"] = route["duration_minutes"]

        farmers.append(farmer)

    if not farmers:
        print("No active farmer supply is available.")
        return None

    total_combinations = (2 ** len(farmers)) - 1
    checked = 0
    feasible = 0
    candidates = []

    if verbose:
        banner("OPTIMIZATION ANALYSIS")
        print(f"Product: {product_name}")
        print(f"Demand: {demand:g} kg")
        print(f"Farmers considered: {len(farmers)}")
        print(f"All combinations to check: {total_combinations}")
        print("\nChecking combinations...")

    for size in range(1, len(farmers) + 1):
        for combo in itertools.combinations(farmers, size):
            checked += 1

            label = " + ".join(
                farmer["farmer_name"] for farmer in combo
            )

            total_supply = sum(
                safe_float(f["available_quantity"])
                for f in combo
            )

            if total_supply + 1e-9 < demand:
                if verbose:
                    print(
                        f"\n{label}\n"
                        f"  Insufficient supply: {total_supply:g} kg"
                    )
                continue

            feasible += 1

            allocations = allocate_exact_quantity(
                list(combo),
                demand,
            )

            if not allocations:
                continue

            best_route = None
            best_distance = None
            best_time = None

            # ALL pickup permutations for this combination.
            for permutation in itertools.permutations(combo):
                distance, travel_time = route_for_permutation(
                    permutation,
                    consumer_coord,
                )

                if (
                    best_distance is None
                    or distance < best_distance - 1e-9
                    or (
                        abs(distance - best_distance) < 1e-9
                        and travel_time < best_time
                    )
                ):
                    best_route = permutation
                    best_distance = distance
                    best_time = travel_time

            product_cost = sum(
                allocation["quantity"] * allocation["unit_price"]
                for allocation in allocations
            )

            delivery_cost = best_distance * DELIVERY_COST_PER_KM

            excess = max(
                0.0,
                total_supply - demand,
            )

            score = (
                PRODUCT_COST_WEIGHT * product_cost
                + DELIVERY_COST_WEIGHT * delivery_cost
                + DISTANCE_WEIGHT * best_distance
                + TIME_WEIGHT * best_time
                + FARMER_COUNT_WEIGHT * len(combo)
                + EXCESS_WEIGHT * excess
            )

            candidate = {
                "farmers": combo,
                "allocations": allocations,
                "route": list(best_route),
                "distance_km": best_distance,
                "travel_time": best_time,
                "product_cost": product_cost,
                "delivery_cost": delivery_cost,
                "total_cost": product_cost + delivery_cost,
                "excess_supply": excess,
                "score": score,
            }

            candidates.append(candidate)

            if verbose:
                print(f"\n{label}")
                print("  Feasible")
                print(
                    "  Exact allocation: "
                    f"{sum(a['quantity'] for a in allocations):g} kg"
                )
                print(f"  Product cost: {money(product_cost)}")
                print(f"  Delivery cost: {money(delivery_cost)}")
                print(f"  Distance: {best_distance:.2f} km")
                print(f"  Time: {best_time:.1f} minutes")
                print(f"  Score: {score:.2f}")

    if verbose:
        print("\n" + "-" * 70)
        print(f"Total combinations checked: {checked}")
        print(f"Feasible combinations: {feasible}")

    if not candidates:
        print("\nNo feasible combination can satisfy the requested quantity.")
        return None

    candidates.sort(
        key=lambda candidate: (
            candidate["score"],
            candidate["total_cost"],
            candidate["distance_km"],
        )
    )

    best = candidates[0]
    best["combinations_checked"] = checked
    best["feasible_combinations"] = feasible
    best["all_candidates"] = candidates

    return best


def print_fulfillment_plan(plan, product_name, demand):
    banner("OPTIMIZED FULFILLMENT PLAN")

    print(f"Product: {product_name}")
    print(f"Demand:  {demand:g} kg\n")

    print("Selected Farmers:")

    for allocation in plan["allocations"]:
        print(
            f"  {allocation['farmer_name']} -> "
            f"{allocation['quantity']:g} kg @ "
            f"{money(allocation['unit_price'])}/kg"
        )

    print(f"\nNumber of Farmers: {len(plan['allocations'])}")
    print(f"Product Cost:      {money(plan['product_cost'])}")
    print(f"Delivery Cost:     {money(plan['delivery_cost'])}")
    print(f"Total Cost:        {money(plan['total_cost'])}")

    route_names = (
        ["Consumer"]
        + [farmer["farmer_name"] for farmer in plan["route"]]
        + ["Consumer"]
    )

    print("\nOptimized Route:")
    print("  " + "\n   ↓\n  ".join(route_names))

    print(f"\nRoad Distance:      {plan['distance_km']:.2f} km")
    print(f"Estimated Time:     {plan['travel_time']:.1f} minutes")
    print(f"Optimization Score: {plan['score']:.2f}")

    print("\nWhy selected:")
    print(
        "  Best overall plan after comparing all feasible "
        "farmer combinations and pickup routes."
    )


# ============================================================
# DEMO DATA
# ============================================================

def load_demo_data(db):
    existing = db.fetchone(
        "SELECT COUNT(*) AS count FROM farmers"
    )

    if existing["count"] > 0:
        print("Demo data already exists.")
        return

    farmers = [
        ("Farmer A", "9999000001", "Najafgarh, Delhi", 10, 28, "2026-09-05"),
        ("Farmer B", "9999000002", "Mehrauli, Delhi", 100, 26, "2026-09-05"),
        ("Farmer C", "9999000003", "Alipur, Delhi", 80, 27, "2026-09-06"),
        ("Farmer D", "9999000004", "Dwarka, Delhi", 60, 25, "2026-09-05"),
        ("Farmer E", "9999000005", "Gurugram", 80, 24, "2026-09-07"),
        ("Farmer F", "9999000006", "Noida", 100, 23, "2026-09-08"),
    ]

    for name, contact, address, quantity, price, harvest_date in farmers:
        lat, lon = demo_coordinates(address)
        farmer_code = generate_id("FARM")

        db.execute("""
            INSERT INTO farmers
            (farmer_code, name, contact, address, latitude, longitude,
             active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, (
            farmer_code,
            name,
            contact,
            address,
            lat,
            lon,
            now(),
        ))

        farmer = db.fetchone("""
            SELECT id
            FROM farmers
            WHERE farmer_code=?
        """, (farmer_code,))

        db.execute("""
            INSERT INTO products
            (farmer_id, product_name, available_quantity,
             price_per_kg, expected_harvest_date,
             expected_harvest_quantity, address,
             latitude, longitude, availability_status,
             created_at, updated_at)
            VALUES (?, 'Tomatoes', ?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?, ?)
        """, (
            farmer["id"],
            quantity,
            price,
            harvest_date,
            quantity,
            address,
            lat,
            lon,
            now(),
            now(),
        ))

    consumer_code = generate_id("CON")
    lat, lon = demo_coordinates("Delhi Central")

    db.execute("""
        INSERT INTO consumers
        (consumer_code, name, contact, address,
         latitude, longitude, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        consumer_code,
        "Rahul",
        "8888000001",
        "Delhi Central",
        lat,
        lon,
        now(),
    ))

    executive_code = generate_id("DEL")

    db.execute("""
        INSERT INTO delivery_executives
        (executive_code, name, contact, active, created_at)
        VALUES (?, ?, ?, 1, ?)
    """, (
        executive_code,
        "Arjun Delivery",
        "7777000001",
        now(),
    ))

    print("Demo data loaded.")
    print("6 tomato farmers, 1 consumer and 1 delivery executive added.")


# ============================================================
# FARMER FUNCTIONS
# ============================================================

def choose_farmer(db, active_only=True):
    if active_only:
        rows = db.fetchall("""
            SELECT *
            FROM farmers
            WHERE active=1
            ORDER BY id
        """)
    else:
        rows = db.fetchall("""
            SELECT *
            FROM farmers
            ORDER BY id
        """)

    if not rows:
        print("No farmers registered.")
        return None

    for row in rows:
        print(
            f"{row['id']}. {row['name']} | "
            f"{row['address']} | "
            f"{'ACTIVE' if row['active'] else 'INACTIVE'}"
        )

    farmer_id = safe_int(input("Farmer ID: "))

    farmer = db.fetchone(
        "SELECT * FROM farmers WHERE id=?",
        (farmer_id,),
    )

    if not farmer:
        print("Invalid farmer.")
        return None

    if active_only and not farmer["active"]:
        print("Farmer is inactive.")
        return None

    return farmer


def register_farmer(db):
    banner("REGISTER FARMER")

    name = input("Farmer name: ").strip()
    contact = input("Contact: ").strip()
    address = input("Location/address: ").strip()

    if not name or not address:
        print("Name and address are required.")
        return

    lat, lon = get_coordinates(address)
    code = generate_id("FARM")

    db.execute("""
        INSERT INTO farmers
        (farmer_code, name, contact, address,
         latitude, longitude, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
    """, (
        code,
        name,
        contact,
        address,
        lat,
        lon,
        now(),
    ))

    print(f"Farmer registered: {code}")
    print(f"Coordinates: {lat:.6f}, {lon:.6f}")


def add_product(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    product = input("Product name: ").strip()
    quantity = safe_float(
        input("Available quantity (kg): ")
    )
    price = safe_float(
        input("Price per kg: ")
    )
    harvest_date = input(
        "Expected harvest date (YYYY-MM-DD): "
    ).strip()

    if not product or quantity < 0 or price <= 0:
        print("Invalid product information.")
        return

    address = (
        input(
            f"Location [{farmer['address']}]: "
        ).strip()
        or farmer["address"]
    )

    lat, lon = get_coordinates(address)

    db.execute("""
        INSERT INTO products
        (farmer_id, product_name, available_quantity,
         price_per_kg, expected_harvest_date,
         expected_harvest_quantity, address,
         latitude, longitude, availability_status,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?, ?)
    """, (
        farmer["id"],
        product,
        quantity,
        price,
        harvest_date,
        quantity,
        address,
        lat,
        lon,
        now(),
        now(),
    ))

    print("Product added successfully.")


def view_farmer_products(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    rows = db.fetchall("""
        SELECT *
        FROM products
        WHERE farmer_id=?
        ORDER BY id
    """, (farmer["id"],))

    if not rows:
        print("No products found.")
        return

    print("\n" + "-" * 90)
    print(
        f"{'ID':<5}"
        f"{'Product':<16}"
        f"{'Stock':<12}"
        f"{'Price':<12}"
        f"{'Harvest':<15}"
        f"{'Status'}"
    )
    print("-" * 90)

    for row in rows:
        print(
            f"{row['id']:<5}"
            f"{row['product_name']:<16}"
            f"{row['available_quantity']:<12.1f}"
            f"{money(row['price_per_kg']):<12}"
            f"{str(row['expected_harvest_date']):<15}"
            f"{row['availability_status']}"
        )

    print("-" * 90)


def update_product(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    rows = db.fetchall("""
        SELECT *
        FROM products
        WHERE farmer_id=?
    """, (farmer["id"],))

    if not rows:
        print("No products.")
        return

    for row in rows:
        print(
            f"{row['id']}. {row['product_name']} | "
            f"{row['available_quantity']} kg | "
            f"{money(row['price_per_kg'])}/kg"
        )

    product_id = safe_int(input("Product ID: "))

    product = db.fetchone("""
        SELECT *
        FROM products
        WHERE id=? AND farmer_id=?
    """, (product_id, farmer["id"]))

    if not product:
        print("Invalid product.")
        return

    print("1. Change name")
    print("2. Change location")
    print("3. Change availability status")

    choice = input("Choice: ").strip()

    if choice == "1":
        value = input("New product name: ").strip()

        if not value:
            print("Name cannot be empty.")
            return

        db.execute("""
            UPDATE products
            SET product_name=?, updated_at=?
            WHERE id=?
        """, (value, now(), product_id))

    elif choice == "2":
        value = input("New address: ").strip()

        if not value:
            print("Address cannot be empty.")
            return

        lat, lon = get_coordinates(value)

        db.execute("""
            UPDATE products
            SET address=?, latitude=?, longitude=?, updated_at=?
            WHERE id=?
        """, (
            value,
            lat,
            lon,
            now(),
            product_id,
        ))

    elif choice == "3":
        value = input(
            "Status (AVAILABLE/UNAVAILABLE): "
        ).strip().upper()

        if value not in {"AVAILABLE", "UNAVAILABLE"}:
            print("Invalid status.")
            return

        db.execute("""
            UPDATE products
            SET availability_status=?, updated_at=?
            WHERE id=?
        """, (
            value,
            now(),
            product_id,
        ))

    else:
        print("Invalid choice.")
        return

    print("Product updated.")


def update_quantity(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    product_id = safe_int(input("Product ID: "))
    quantity = safe_float(
        input("New available quantity (kg): ")
    )

    if quantity < 0:
        print("Quantity cannot be negative.")
        return

    cursor = db.execute("""
        UPDATE products
        SET available_quantity=?, updated_at=?
        WHERE id=? AND farmer_id=?
    """, (
        quantity,
        now(),
        product_id,
        farmer["id"],
    ))

    if cursor.rowcount == 0:
        print("Product not found.")
        return

    print("Quantity updated.")


def update_price(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    product_id = safe_int(input("Product ID: "))
    price = safe_float(input("New price/kg: "))

    if price <= 0:
        print("Price must be positive.")
        return

    cursor = db.execute("""
        UPDATE products
        SET price_per_kg=?, updated_at=?
        WHERE id=? AND farmer_id=?
    """, (
        price,
        now(),
        product_id,
        farmer["id"],
    ))

    if cursor.rowcount == 0:
        print("Product not found.")
        return

    print("Price updated.")


def update_harvest(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    product_id = safe_int(input("Product ID: "))
    harvest_date = input(
        "New expected harvest date (YYYY-MM-DD): "
    ).strip()
    harvest_quantity = safe_float(
        input("Expected harvest quantity (kg): ")
    )

    if harvest_quantity < 0:
        print("Quantity cannot be negative.")
        return

    cursor = db.execute("""
        UPDATE products
        SET expected_harvest_date=?,
            expected_harvest_quantity=?,
            updated_at=?
        WHERE id=? AND farmer_id=?
    """, (
        harvest_date,
        harvest_quantity,
        now(),
        product_id,
        farmer["id"],
    ))

    if cursor.rowcount == 0:
        print("Product not found.")
        return

    print("Harvest information updated.")


def update_farmer_location(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    address = input("New address: ").strip()

    if not address:
        print("Address cannot be empty.")
        return

    lat, lon = get_coordinates(address)

    db.execute("""
        UPDATE farmers
        SET address=?, latitude=?, longitude=?
        WHERE id=?
    """, (
        address,
        lat,
        lon,
        farmer["id"],
    ))

    db.execute("""
        UPDATE products
        SET address=?, latitude=?, longitude=?, updated_at=?
        WHERE farmer_id=?
    """, (
        address,
        lat,
        lon,
        now(),
        farmer["id"],
    ))

    print("Farmer location updated.")


def farmer_orders(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    rows = db.fetchall("""
        SELECT
            fa.*,
            o.order_code,
            o.product_name,
            o.order_status,
            o.payment_status
        FROM farmer_allocations fa
        JOIN orders o ON o.id=fa.order_id
        WHERE fa.farmer_id=?
        ORDER BY fa.id DESC
    """, (farmer["id"],))

    if not rows:
        print("No farmer orders.")
        return

    for row in rows:
        print(
            f"\nOrder {row['order_code']}"
            f"\nProduct: {row['product_name']}"
            f"\nQuantity: {row['allocated_quantity']} kg"
            f"\nPayment: {row['payment_status']}"
            f"\nAllocation: {row['confirmation_status']}"
            f"\nOrder: {row['order_status']}"
        )


def farmer_payments(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    rows = db.fetchall("""
        SELECT
            fa.allocated_quantity,
            fa.unit_price,
            o.order_code
        FROM farmer_allocations fa
        JOIN orders o ON o.id=fa.order_id
        WHERE fa.farmer_id=?
          AND o.payment_status='PAID'
    """, (farmer["id"],))

    if not rows:
        print("No paid allocations.")
        return

    total = 0.0

    for row in rows:
        value = (
            row["allocated_quantity"]
            * row["unit_price"]
        )

        total += value

        print(
            f"{row['order_code']}: "
            f"{row['allocated_quantity']} kg -> "
            f"{money(value)}"
        )

    print(f"\nTotal paid allocation value: {money(total)}")


# ============================================================
# CONSUMER FUNCTIONS
# ============================================================

def register_consumer(db):
    banner("REGISTER CONSUMER")

    name = input("Name: ").strip()
    contact = input("Contact: ").strip()
    address = input("Delivery/home address: ").strip()

    if not name or not address:
        print("Name and address are required.")
        return

    lat, lon = get_coordinates(address)
    code = generate_id("CON")

    db.execute("""
        INSERT INTO consumers
        (consumer_code, name, contact, address,
         latitude, longitude, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        code,
        name,
        contact,
        address,
        lat,
        lon,
        now(),
    ))

    print(f"Consumer registered: {code}")
    print(f"Coordinates: {lat:.6f}, {lon:.6f}")


def choose_consumer(db):
    rows = db.fetchall("""
        SELECT *
        FROM consumers
        ORDER BY id
    """)

    if not rows:
        print("No consumers registered.")
        return None

    for row in rows:
        print(
            f"{row['id']}. {row['name']} | "
            f"{row['address']}"
        )

    consumer_id = safe_int(input("Consumer ID: "))

    consumer = db.fetchone(
        "SELECT * FROM consumers WHERE id=?",
        (consumer_id,),
    )

    if not consumer:
        print("Invalid consumer.")
        return None

    return consumer


def search_products(db):
    product = input("Search product: ").strip()

    if not product:
        print("Enter a product name.")
        return

    rows = db.fetchall("""
        SELECT
            p.*,
            f.name AS farmer_name
        FROM products p
        JOIN farmers f ON f.id=p.farmer_id
        WHERE LOWER(p.product_name) LIKE LOWER(?)
          AND p.availability_status='AVAILABLE'
          AND p.available_quantity>0
          AND f.active=1
        ORDER BY p.price_per_kg
    """, (f"%{product}%",))

    if not rows:
        print("No available products found.")
        return

    print("\n" + "-" * 95)
    print(
        f"{'Farmer':<18}"
        f"{'Product':<15}"
        f"{'Available':<14}"
        f"{'Price/kg':<14}"
        f"{'Harvest':<15}"
    )
    print("-" * 95)

    for row in rows:
        print(
            f"{row['farmer_name']:<18}"
            f"{row['product_name']:<15}"
            f"{row['available_quantity']:<14.1f}"
            f"{money(row['price_per_kg']):<14}"
            f"{row['expected_harvest_date']:<15}"
        )

    print("-" * 95)


def compare_prices(db):
    product = input("Product: ").strip()

    rows = db.fetchall("""
        SELECT
            f.name AS farmer_name,
            p.available_quantity,
            p.price_per_kg,
            p.expected_harvest_date,
            p.address
        FROM products p
        JOIN farmers f ON f.id=p.farmer_id
        WHERE LOWER(p.product_name)=LOWER(?)
          AND p.available_quantity>0
          AND p.availability_status='AVAILABLE'
          AND f.active=1
        ORDER BY p.price_per_kg
    """, (product,))

    if not rows:
        print("No available sellers.")
        return

    print(f"\nPRICE COMPARISON — {product}")

    for row in rows:
        print(
            f"{row['farmer_name']:<18} "
            f"{money(row['price_per_kg'])}/kg | "
            f"{row['available_quantity']:.1f} kg | "
            f"{row['address']}"
        )


def product_details(db):
    product = input("Product: ").strip()

    rows = db.fetchall("""
        SELECT
            p.*,
            f.name AS farmer_name
        FROM products p
        JOIN farmers f ON f.id=p.farmer_id
        WHERE LOWER(p.product_name)=LOWER(?)
          AND p.availability_status='AVAILABLE'
          AND f.active=1
    """, (product,))

    if not rows:
        print("No product details found.")
        return

    for row in rows:
        print("\n" + "-" * 55)
        print(f"Farmer: {row['farmer_name']}")
        print(f"Product: {row['product_name']}")
        print(f"Available: {row['available_quantity']} kg")
        print(f"Price: {money(row['price_per_kg'])}/kg")
        print(f"Expected harvest: {row['expected_harvest_date']}")
        print(f"Location: {row['address']}")


def create_order(db):
    consumer = choose_consumer(db)

    if not consumer:
        return

    product = input("Product: ").strip()
    quantity = safe_float(
        input("Quantity required (kg): ")
    )

    if not product or quantity <= 0:
        print("Invalid order.")
        return

    print("\nDelivery location:")
    print("1. Use registered consumer address")
    print("2. Enter another address")
    print("3. Use demo current location")

    location_choice = input("Choice: ").strip()

    if location_choice == "1":
        address = consumer["address"]

    elif location_choice == "2":
        address = input("Delivery address: ").strip()

        if not address:
            print("Address is required.")
            return

    elif location_choice == "3":
        address = "Dwarka, Delhi"
        print("[DEMO LOCATION] Using Dwarka, Delhi.")

    else:
        print("Invalid choice.")
        return

    lat, lon = get_coordinates(address)

    print("\nFinding available farmer supply...")
    print("Running multi-farmer optimization...")

    plan = optimize_fulfillment(
        db,
        product,
        quantity,
        (lat, lon),
        verbose=True,
    )

    if not plan:
        return

    print_fulfillment_plan(
        plan,
        product,
        quantity,
    )

    accept = input(
        "\nAccept this fulfillment plan? (y/n): "
    ).strip().lower()

    if accept != "y":
        print("Plan rejected. No order created.")
        return

    order_code = generate_id("ORD")

    db.execute("""
        INSERT INTO orders
        (order_code, consumer_id, product_name, quantity,
         delivery_address, delivery_latitude,
         delivery_longitude, product_cost,
         delivery_cost, total_cost, route_json,
         distance_km, eta_minutes, optimization_score,
         payment_status, order_status,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'PENDING', 'PENDING_PAYMENT', ?, ?)
    """, (
        order_code,
        consumer["id"],
        product,
        quantity,
        address,
        lat,
        lon,
        plan["product_cost"],
        plan["delivery_cost"],
        plan["total_cost"],
        json.dumps([
            farmer["farmer_id"]
            for farmer in plan["route"]
        ]),
        plan["distance_km"],
        plan["travel_time"],
        plan["score"],
        now(),
        now(),
    ))

    order = db.fetchone("""
        SELECT id
        FROM orders
        WHERE order_code=?
    """, (order_code,))

    order_id = order["id"]

    db.execute("""
        INSERT INTO order_items
        (order_id, product_name, quantity, unit_price)
        VALUES (?, ?, ?, ?)
    """, (
        order_id,
        product,
        quantity,
        plan["product_cost"] / quantity,
    ))

    for allocation in plan["allocations"]:
        db.execute("""
            INSERT INTO farmer_allocations
            (order_id, farmer_id, product_id,
             allocated_quantity, unit_price,
             confirmation_status)
            VALUES (?, ?, ?, ?, ?, 'PENDING')
        """, (
            order_id,
            allocation["farmer_id"],
            allocation["product_id"],
            allocation["quantity"],
            allocation["unit_price"],
        ))

    print(f"\nOrder created: {order_code}")

    create_payment(
        db,
        order_id,
        plan["total_cost"],
    )

    if not verify_payment(db, order_id, success=True):
        return

    transition_order(
        db,
        order_id,
        "PAID",
    )

    send_notification(
        db,
        "Consumer",
        consumer["id"],
        f"Payment successful for order {order_code}.",
        order_id,
    )

    transition_order(
        db,
        order_id,
        "FARMERS_ASSIGNED",
    )

    allocations = db.fetchall("""
        SELECT
            fa.*,
            f.name
        FROM farmer_allocations fa
        JOIN farmers f ON f.id=fa.farmer_id
        WHERE fa.order_id=?
    """, (order_id,))

    for allocation in allocations:
        send_notification(
            db,
            "Farmer",
            allocation["farmer_id"],
            (
                f"New order {order_code}: "
                f"{allocation['allocated_quantity']:g} kg "
                f"{product} at "
                f"{money(allocation['unit_price'])}/kg."
            ),
            order_id,
        )

    transition_order(
        db,
        order_id,
        "FARMER_CONFIRMATION",
    )

    print("\nOrder is waiting for farmer confirmation.")
    print(f"Order code: {order_code}")


def track_order(db):
    code = input("Order code: ").strip()

    order = db.fetchone("""
        SELECT *
        FROM orders
        WHERE order_code=?
    """, (code,))

    if not order:
        print("Order not found.")
        return

    print(f"\nOrder: {order['order_code']}")
    print(
        f"Product: {order['product_name']} | "
        f"{order['quantity']} kg"
    )
    print(f"Payment: {order['payment_status']}")
    print(f"Status: {order['order_status']}")
    print(
        f"Distance: {order['distance_km']:.2f} km"
    )
    print(
        f"ETA: {order['eta_minutes']:.1f} minutes"
    )

    allocations = db.fetchall("""
        SELECT
            fa.*,
            f.name
        FROM farmer_allocations fa
        JOIN farmers f ON f.id=fa.farmer_id
        WHERE fa.order_id=?
    """, (order["id"],))

    print("\nFarmer Allocations:")

    for allocation in allocations:
        print(
            f"  {allocation['name']}: "
            f"{allocation['allocated_quantity']} kg | "
            f"{allocation['confirmation_status']}"
        )


def payment_status(db):
    code = input("Order code: ").strip()

    order = db.fetchone("""
        SELECT
            o.*,
            p.provider,
            p.provider_payment_id,
            p.status AS payment_record_status
        FROM orders o
        LEFT JOIN payments p ON p.order_id=o.id
        WHERE o.order_code=?
        ORDER BY p.id DESC
        LIMIT 1
    """, (code,))

    if not order:
        print("Order not found.")
        return

    print(f"Order: {code}")
    print(
        f"Payment status: "
        f"{order['payment_status']}"
    )
    print(f"Provider: {order['provider']}")
    print(
        f"Payment ID: "
        f"{order['provider_payment_id']}"
    )


def order_history(db):
    consumer = choose_consumer(db)

    if not consumer:
        return

    rows = db.fetchall("""
        SELECT *
        FROM orders
        WHERE consumer_id=?
        ORDER BY id DESC
    """, (consumer["id"],))

    if not rows:
        print("No order history.")
        return

    for row in rows:
        print(
            f"{row['order_code']} | "
            f"{row['product_name']} | "
            f"{row['quantity']} kg | "
            f"{row['order_status']} | "
            f"{money(row['total_cost'])}"
        )


# ============================================================
# FARMER CONFIRMATION
# ============================================================

def confirm_farmer_allocations(db):
    farmer = choose_farmer(db)

    if not farmer:
        return

    rows = db.fetchall("""
        SELECT
            fa.*,
            o.order_code,
            o.product_name
        FROM farmer_allocations fa
        JOIN orders o ON o.id=fa.order_id
        WHERE fa.farmer_id=?
          AND fa.confirmation_status='PENDING'
          AND o.payment_status='PAID'
    """, (farmer["id"],))

    if not rows:
        print("No pending paid allocations.")
        return

    for row in rows:
        print(f"\nOrder: {row['order_code']}")
        print(f"Product: {row['product_name']}")
        print(
            f"Quantity: "
            f"{row['allocated_quantity']} kg"
        )
        print(
            f"Price: "
            f"{money(row['unit_price'])}/kg"
        )

        choice = input(
            "Confirm (c) / Reject (r): "
        ).strip().lower()

        if choice == "c":
            db.execute("""
                UPDATE farmer_allocations
                SET confirmation_status='CONFIRMED'
                WHERE id=?
            """, (row["id"],))

            send_notification(
                db,
                "Consumer",
                None,
                (
                    f"Farmer {farmer['name']} "
                    f"confirmed the allocation for "
                    f"order {row['order_code']}."
                ),
                row["order_id"],
            )

            print("Allocation confirmed.")

        elif choice == "r":
            db.execute("""
                UPDATE farmer_allocations
                SET confirmation_status='REJECTED'
                WHERE id=?
            """, (row["id"],))

            print(
                "Allocation rejected. "
                "The order needs re-optimization."
            )

            reoptimize_after_rejection(
                db,
                row["order_id"],
                farmer["id"],
            )

        else:
            print("Skipped.")


def reoptimize_after_rejection(
    db,
    order_id,
    rejected_farmer_id,
):
    order = db.fetchone("""
        SELECT *
        FROM orders
        WHERE id=?
    """, (order_id,))

    if not order:
        return

    print("\n[RE-OPTIMIZATION]")
    print(
        f"Farmer ID {rejected_farmer_id} "
        "is unavailable for this order."
    )
    print("Finding another fulfillment plan...")

    plan = optimize_fulfillment(
        db,
        order["product_name"],
        order["quantity"],
        (
            order["delivery_latitude"],
            order["delivery_longitude"],
        ),
        verbose=True,
        excluded_farmer_ids={rejected_farmer_id},
    )

    if not plan:
        print(
            "No replacement plan is currently available."
        )
        return

    db.execute("""
        DELETE FROM farmer_allocations
        WHERE order_id=?
    """, (order_id,))

    for allocation in plan["allocations"]:
        db.execute("""
            INSERT INTO farmer_allocations
            (order_id, farmer_id, product_id,
             allocated_quantity, unit_price,
             confirmation_status)
            VALUES (?, ?, ?, ?, ?, 'PENDING')
        """, (
            order_id,
            allocation["farmer_id"],
            allocation["product_id"],
            allocation["quantity"],
            allocation["unit_price"],
        ))

    db.execute("""
        UPDATE orders
        SET product_cost=?,
            delivery_cost=?,
            total_cost=?,
            route_json=?,
            distance_km=?,
            eta_minutes=?,
            optimization_score=?,
            order_status='FARMER_CONFIRMATION',
            updated_at=?
        WHERE id=?
    """, (
        plan["product_cost"],
        plan["delivery_cost"],
        plan["total_cost"],
        json.dumps([
            farmer["farmer_id"]
            for farmer in plan["route"]
        ]),
        plan["distance_km"],
        plan["travel_time"],
        plan["score"],
        now(),
        order_id,
    ))

    for allocation in plan["allocations"]:
        send_notification(
            db,
            "Farmer",
            allocation["farmer_id"],
            (
                f"New replacement allocation for "
                f"order {order['order_code']}: "
                f"{allocation['quantity']:g} kg "
                f"{order['product_name']}."
            ),
            order_id,
        )

    print("\nReplacement plan created.")
    print_fulfillment_plan(
        plan,
        order["product_name"],
        order["quantity"],
    )


# ============================================================
# DELIVERY
# ============================================================

def assign_delivery_if_ready(db, order_id):
    order = db.fetchone("""
        SELECT *
        FROM orders
        WHERE id=?
    """, (order_id,))

    if not order:
        print("Order not found.")
        return False

    pending = db.fetchone("""
        SELECT COUNT(*) AS count
        FROM farmer_allocations
        WHERE order_id=?
          AND confirmation_status!='CONFIRMED'
    """, (order_id,))

    if pending["count"] != 0:
        print(
            "All selected farmers must confirm first."
        )
        return False

    existing_delivery = db.fetchone("""
        SELECT id
        FROM deliveries
        WHERE order_id=?
          AND status!='COMPLETED'
    """, (order_id,))

    if existing_delivery:
        print("Delivery is already assigned.")
        return False

    executive = db.fetchone("""
        SELECT *
        FROM delivery_executives
        WHERE active=1
        ORDER BY id
        LIMIT 1
    """)

    if not executive:
        print("No active delivery executive.")
        return False

    if order["order_status"] == "FARMER_CONFIRMATION":
        if not transition_order(
            db,
            order_id,
            "DELIVERY_ASSIGNED",
        ):
            return False

    delivery_code = generate_id("DLV")

    db.execute("""
        INSERT INTO deliveries
        (delivery_code, order_id, executive_id,
         current_stop, status, created_at, updated_at)
        VALUES (?, ?, ?, 0, 'ASSIGNED', ?, ?)
    """, (
        delivery_code,
        order_id,
        executive["id"],
        now(),
        now(),
    ))

    send_notification(
        db,
        "Consumer",
        order["consumer_id"],
        (
            f"Delivery executive "
            f"{executive['name']} assigned to "
            f"order {order['order_code']}."
        ),
        order_id,
    )

    print(
        f"Delivery assigned: {delivery_code} "
        f"to {executive['name']}"
    )

    return True


def view_deliveries(db):
    rows = db.fetchall("""
        SELECT
            d.*,
            o.order_code,
            o.product_name,
            o.quantity,
            o.distance_km,
            o.eta_minutes,
            e.name AS executive_name
        FROM deliveries d
        JOIN orders o ON o.id=d.order_id
        LEFT JOIN delivery_executives e
            ON e.id=d.executive_id
        ORDER BY d.id DESC
    """)

    if not rows:
        print("No deliveries.")
        return

    for row in rows:
        print(
            f"\n{row['delivery_code']} | "
            f"Order {row['order_code']}"
        )
        print(
            f"Product: {row['product_name']} | "
            f"{row['quantity']} kg"
        )
        print(
            f"Executive: {row['executive_name']}"
        )
        print(f"Status: {row['status']}")
        print(
            f"Distance: {row['distance_km']:.2f} km | "
            f"ETA: {row['eta_minutes']:.1f} min"
        )


def get_delivery(db):
    code = input("Delivery code: ").strip()

    return db.fetchone("""
        SELECT
            d.*,
            o.order_code,
            o.product_name,
            o.quantity,
            o.distance_km,
            o.eta_minutes,
            o.consumer_id,
            o.order_status,
            o.delivery_address,
            o.route_json
        FROM deliveries d
        JOIN orders o ON o.id=d.order_id
        WHERE d.delivery_code=?
    """, (code,))


def print_delivery_detail(db, delivery):
    allocations = db.fetchall("""
        SELECT
            fa.*,
            f.name
        FROM farmer_allocations fa
        JOIN farmers f ON f.id=fa.farmer_id
        WHERE fa.order_id=?
          AND fa.confirmation_status='CONFIRMED'
        ORDER BY fa.id
    """, (delivery["order_id"],))

    route_ids = json.loads(
        delivery["route_json"] or "[]"
    )

    route_names = []

    for farmer_id in route_ids:
        farmer = db.fetchone("""
            SELECT name
            FROM farmers
            WHERE id=?
        """, (farmer_id,))

        if farmer:
            route_names.append(farmer["name"])

    banner(
        f"DELIVERY {delivery['delivery_code']}"
    )

    print(f"Order: {delivery['order_code']}")
    print(
        f"Product: {delivery['product_name']} | "
        f"{delivery['quantity']} kg"
    )

    print("\nPickup Sequence:")

    for index, allocation in enumerate(
        allocations,
        start=1,
    ):
        print(
            f"  Pickup {index}: "
            f"{allocation['name']} -> "
            f"{allocation['allocated_quantity']} kg"
        )

    print(
        f"\nDestination: "
        f"{delivery['delivery_address']}"
    )

    print("\nOptimized Route:")

    print(
        "  "
        + "\n   ↓\n  ".join(
            ["Consumer"] + route_names + ["Consumer"]
        )
    )

    print(
        f"\nDistance: "
        f"{delivery['distance_km']:.2f} km"
    )
    print(
        f"ETA: "
        f"{delivery['eta_minutes']:.1f} minutes"
    )
    print(
        f"Current delivery status: "
        f"{delivery['status']}"
    )


def delivery_action(db, action):
    delivery = get_delivery(db)

    if not delivery:
        print("Delivery not found.")
        return

    print_delivery_detail(db, delivery)

    order_id = delivery["order_id"]
    current = delivery["status"]

    if action == "accept":
        if current != "ASSIGNED":
            print(
                "Delivery can only be accepted "
                "from ASSIGNED state."
            )
            return

        db.execute("""
            UPDATE deliveries
            SET status='ACCEPTED', updated_at=?
            WHERE id=?
        """, (now(), delivery["id"]))

        print("Delivery accepted.")

    elif action == "start":
        if current != "ACCEPTED":
            print(
                "Delivery must be accepted first."
            )
            return

        if transition_order(
            db,
            order_id,
            "PICKUP_STARTED",
        ):
            db.execute("""
                UPDATE deliveries
                SET status='PICKUP_STARTED',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            print("Pickup started.")

    elif action == "reach":
        if current not in {
            "PICKUP_STARTED",
            "PARTIAL_PICKUP",
        }:
            print(
                "You must start the delivery first."
            )
            return

        db.execute("""
            UPDATE deliveries
            SET status='AT_FARMER',
                updated_at=?
            WHERE id=?
        """, (now(), delivery["id"]))

        print("Reached current farmer.")

    elif action == "pickup":
        if current not in {
            "AT_FARMER",
            "AT_NEXT_FARMER",
            "PICKUP_STARTED",
        }:
            print(
                "Reach a farmer before pickup."
            )
            return

        allocation = db.fetchone("""
            SELECT
                fa.*,
                f.name
            FROM farmer_allocations fa
            JOIN farmers f ON f.id=fa.farmer_id
            WHERE fa.order_id=?
              AND fa.confirmation_status='CONFIRMED'
              AND fa.picked_up=0
            ORDER BY fa.id
            LIMIT 1
        """, (order_id,))

        if not allocation:
            print("No remaining confirmed pickup.")
            return

        db.execute("""
            UPDATE farmer_allocations
            SET picked_up=1
            WHERE id=?
        """, (allocation["id"],))

        remaining = db.fetchone("""
            SELECT COUNT(*) AS count
            FROM farmer_allocations
            WHERE order_id=?
              AND confirmation_status='CONFIRMED'
              AND picked_up=0
        """, (order_id,))["count"]

        if remaining == 0:
            transition_order(
                db,
                order_id,
                "ALL_ITEMS_PICKED_UP",
            )

            db.execute("""
                UPDATE deliveries
                SET status='ALL_PICKED_UP',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            print("All items picked up.")

        else:
            transition_order(
                db,
                order_id,
                "PARTIALLY_PICKED_UP",
            )

            db.execute("""
                UPDATE deliveries
                SET status='PARTIAL_PICKUP',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            print(
                f"Picked up "
                f"{allocation['allocated_quantity']} kg "
                f"from {allocation['name']}."
            )

        send_notification(
            db,
            "Consumer",
            delivery["consumer_id"],
            (
                f"Pickup completed at "
                f"{allocation['name']} for "
                f"order {delivery['order_code']}."
            ),
            order_id,
        )

    elif action == "next":
        if current not in {
            "PARTIAL_PICKUP",
            "AT_FARMER",
        }:
            print(
                "No next-farmer transition "
                "is available now."
            )
            return

        db.execute("""
            UPDATE deliveries
            SET current_stop=current_stop+1,
                status='AT_NEXT_FARMER',
                updated_at=?
            WHERE id=?
        """, (
            now(),
            delivery["id"],
        ))

        print(
            "Advanced to the next optimized "
            "farmer stop."
        )

    elif action == "out":
        if current != "ALL_PICKED_UP":
            print(
                "All items must be picked up first."
            )
            return

        if transition_order(
            db,
            order_id,
            "OUT_FOR_DELIVERY",
        ):
            db.execute("""
                UPDATE deliveries
                SET status='OUT_FOR_DELIVERY',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            send_notification(
                db,
                "Consumer",
                delivery["consumer_id"],
                (
                    f"Order {delivery['order_code']} "
                    "is now out for delivery."
                ),
                order_id,
            )

            print("Out for delivery.")

    elif action == "delivered":
        if current != "OUT_FOR_DELIVERY":
            print(
                "Delivery must be out for "
                "delivery first."
            )
            return

        if transition_order(
            db,
            order_id,
            "DELIVERED",
        ):
            db.execute("""
                UPDATE deliveries
                SET status='DELIVERED',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            send_notification(
                db,
                "Consumer",
                delivery["consumer_id"],
                (
                    f"Order {delivery['order_code']} "
                    "has been delivered."
                ),
                order_id,
            )

            transition_order(
                db,
                order_id,
                "COMPLETED",
            )

            db.execute("""
                UPDATE deliveries
                SET status='COMPLETED',
                    updated_at=?
                WHERE id=?
            """, (now(), delivery["id"]))

            print("Order completed.")


def delivery_history(db):
    rows = db.fetchall("""
        SELECT
            d.delivery_code,
            o.order_code,
            o.product_name,
            d.status,
            d.updated_at
        FROM deliveries d
        JOIN orders o ON o.id=d.order_id
        ORDER BY d.id DESC
    """)

    if not rows:
        print("No delivery history.")
        return

    for row in rows:
        print(
            f"{row['delivery_code']} | "
            f"{row['order_code']} | "
            f"{row['product_name']} | "
            f"{row['status']} | "
            f"{row['updated_at']}"
        )


# ============================================================
# SYSTEM DATA
# ============================================================

def view_system_data(db):
    banner("SYSTEM DATA")

    counts = [
        ("Farmers", "SELECT COUNT(*) AS c FROM farmers"),
        ("Consumers", "SELECT COUNT(*) AS c FROM consumers"),
        ("Products", "SELECT COUNT(*) AS c FROM products"),
        ("Orders", "SELECT COUNT(*) AS c FROM orders"),
        ("Deliveries", "SELECT COUNT(*) AS c FROM deliveries"),
        ("Payments", "SELECT COUNT(*) AS c FROM payments"),
        ("Notifications", "SELECT COUNT(*) AS c FROM notifications"),
    ]

    for label, query in counts:
        count = db.fetchone(query)["c"]
        print(f"{label:<20}: {count}")

    print("\nRecent Orders:")

    orders = db.fetchall("""
        SELECT
            order_code,
            product_name,
            quantity,
            total_cost,
            payment_status,
            order_status
        FROM orders
        ORDER BY id DESC
        LIMIT 10
    """)

    if not orders:
        print("No orders yet.")
        return

    for order in orders:
        print(
            f"{order['order_code']} | "
            f"{order['product_name']} | "
            f"{order['quantity']} kg | "
            f"{money(order['total_cost'])} | "
            f"{order['payment_status']} | "
            f"{order['order_status']}"
        )


# ============================================================
# MENUS
# ============================================================

def farmer_menu(db):
    while True:
        banner("FARMER DASHBOARD")

        print("1. Register Farmer")
        print("2. Add Product")
        print("3. Update Product")
        print("4. Update Quantity")
        print("5. Update Price")
        print("6. Update Expected Harvest")
        print("7. Update Location")
        print("8. View Products")
        print("9. View Orders")
        print("10. View Payments")
        print("11. Confirm/Reject Order Allocations")
        print("12. Back")

        choice = input("Choice: ").strip()

        if choice == "1":
            register_farmer(db)

        elif choice == "2":
            add_product(db)

        elif choice == "3":
            update_product(db)

        elif choice == "4":
            update_quantity(db)

        elif choice == "5":
            update_price(db)

        elif choice == "6":
            update_harvest(db)

        elif choice == "7":
            update_farmer_location(db)

        elif choice == "8":
            view_farmer_products(db)

        elif choice == "9":
            farmer_orders(db)

        elif choice == "10":
            farmer_payments(db)

        elif choice == "11":
            confirm_farmer_allocations(db)

        elif choice == "12":
            break

        else:
            print("Invalid choice.")

        if choice != "12":
            pause()


def consumer_menu(db):
    while True:
        banner("CONSUMER DASHBOARD")

        print("1. Register Consumer")
        print("2. Search Products")
        print("3. Compare Prices")
        print("4. View Product Details")
        print("5. Place Order")
        print("6. Track Order")
        print("7. View Payment Status")
        print("8. Order History")
        print("9. Back")

        choice = input("Choice: ").strip()

        if choice == "1":
            register_consumer(db)

        elif choice == "2":
            search_products(db)

        elif choice == "3":
            compare_prices(db)

        elif choice == "4":
            product_details(db)

        elif choice == "5":
            create_order(db)

        elif choice == "6":
            track_order(db)

        elif choice == "7":
            payment_status(db)

        elif choice == "8":
            order_history(db)

        elif choice == "9":
            break

        else:
            print("Invalid choice.")

        if choice != "9":
            pause()


def delivery_menu(db):
    while True:
        banner("DELIVERY EXECUTIVE")

        print("1. View Assigned Deliveries")
        print("2. Accept Delivery")
        print("3. Reached Farmer")
        print("4. Picked Up")
        print("5. Reached Next Farmer")
        print("6. Start Pickup")
        print("7. Out for Delivery")
        print("8. Delivered")
        print("9. Delivery History")
        print("10. Assign Ready Delivery")
        print("11. Back")

        choice = input("Choice: ").strip()

        if choice == "1":
            view_deliveries(db)

        elif choice == "2":
            delivery_action(db, "accept")

        elif choice == "3":
            delivery_action(db, "reach")

        elif choice == "4":
            delivery_action(db, "pickup")

        elif choice == "5":
            delivery_action(db, "next")

        elif choice == "6":
            delivery_action(db, "start")

        elif choice == "7":
            delivery_action(db, "out")

        elif choice == "8":
            delivery_action(db, "delivered")

        elif choice == "9":
            delivery_history(db)

        elif choice == "10":
            order_code = input(
                "Order code whose farmers confirmed: "
            ).strip()

            order = db.fetchone("""
                SELECT *
                FROM orders
                WHERE order_code=?
            """, (order_code,))

            if order:
                assign_delivery_if_ready(
                    db,
                    order["id"],
                )
            else:
                print("Order not found.")

        elif choice == "11":
            break

        else:
            print("Invalid choice.")

        if choice != "11":
            pause()


# ============================================================
# SIMPLE OPTIMIZER DEMO
# ============================================================

def run_optimizer_demo(db):
    banner("MULTI-FARMER OPTIMIZER DEMO")

    consumer = db.fetchone("""
        SELECT *
        FROM consumers
        ORDER BY id
        LIMIT 1
    """)

    if not consumer:
        print("Loading demo data first...")
        load_demo_data(db)

        consumer = db.fetchone("""
            SELECT *
            FROM consumers
            ORDER BY id
            LIMIT 1
        """)

    print("Product: Tomatoes")
    print("Demand: 150 kg")
    print(
        "\nThe optimizer will evaluate every "
        "non-empty farmer combination."
    )

    plan = optimize_fulfillment(
        db,
        "Tomatoes",
        150,
        (
            consumer["latitude"],
            consumer["longitude"],
        ),
        verbose=True,
    )

    if plan:
        print_fulfillment_plan(
            plan,
            "Tomatoes",
            150,
        )



# ============================================================
# MAIN
# ============================================================

def main():
    db = Database()

    while True:
        banner("SIH PROTOTYPE")

        print("1. Farmer")
        print("2. Consumer")
        print("3. Delivery Executive")
        print("4. Run Optimizer Demo")
        print("5. View System Data")
        print("6. Load Demo Data")
        print("7. Exit")

        choice = input("\nChoice: ").strip()

        if choice == "1":
            farmer_menu(db)

        elif choice == "2":
            consumer_menu(db)

        elif choice == "3":
            delivery_menu(db)

        elif choice == "4":
            run_optimizer_demo(db)
            pause()

        elif choice == "5":
            view_system_data(db)
            pause()

        elif choice == "6":
            load_demo_data(db)
            pause()

        elif choice == "7":
            print("Exiting. Database saved.")
            db.conn.close()
            break

        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
