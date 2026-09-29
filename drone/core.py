"""Drone courier dispatch core.

Text CMD instructions in, "OK ..." / "ERR ..." out. Pure standard library,
fixed seed, JSON archive. Every command validates fully before committing
exactly one state change.
"""

import json
import random

SEED = 20260929
BASE = (0, 0)
WEATHER_MULTIPLIER = {"clear": 1, "wind": 2, "storm": 3}


class Drone:
    def __init__(self, drone_id, x, y, battery, capacity):
        self.id = drone_id
        self.x = x
        self.y = y
        self.battery = battery
        self.capacity = capacity
        self.state = "idle"  # idle | assigned | flying | returning
        self.route = []
        self.package_id = None
        self.task_cost = 0
        self.reputation = 0

    def to_dict(self):
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "battery": self.battery,
            "capacity": self.capacity,
            "state": self.state,
            "route": [list(p) for p in self.route],
            "package_id": self.package_id,
            "task_cost": self.task_cost,
            "reputation": self.reputation,
        }

    @classmethod
    def from_dict(cls, data):
        drone = cls(data["id"], data["x"], data["y"], data["battery"], data["capacity"])
        drone.state = data["state"]
        drone.route = [tuple(p) for p in data["route"]]
        drone.package_id = data["package_id"]
        drone.task_cost = data["task_cost"]
        drone.reputation = data["reputation"]
        return drone


class Package:
    def __init__(self, package_id, x, y, weight):
        self.id = package_id
        self.x = x
        self.y = y
        self.weight = weight
        self.status = "pending"  # pending | assigned | in_transit | delivered
        self.assigned_to = None

    def to_dict(self):
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "weight": self.weight,
            "status": self.status,
            "assigned_to": self.assigned_to,
        }

    @classmethod
    def from_dict(cls, data):
        package = cls(data["id"], data["x"], data["y"], data["weight"])
        package.status = data["status"]
        package.assigned_to = data["assigned_to"]
        return package


class Dispatch:
    def __init__(self, seed=SEED):
        self.seed = seed
        self.rng = random.Random(seed)
        self.drones = {}
        self.packages = {}
        self.zones = []
        self.weather = "clear"

    # -- geometry / cost helpers -----------------------------------------

    @staticmethod
    def _dist(ax, ay, bx, by):
        return abs(ax - bx) + abs(ay - by)

    @staticmethod
    def _point_in_zone(zone, x, y):
        x1, y1, x2, y2 = zone
        return x1 <= x <= x2 and y1 <= y <= y2

    @staticmethod
    def _segment_hits_zone(ax, ay, bx, by, zone):
        # Liang-Barsky clip against the inclusive rectangle: True when any
        # part of the segment lies inside or on the zone boundary.
        x1, y1, x2, y2 = zone
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, ax - x1), (dx, x2 - ax), (-dy, ay - y1), (dy, y2 - ay)):
            if p == 0:
                if q < 0:
                    return False
                continue
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
        return True

    def _route_blocked(self, points):
        for i in range(len(points) - 1):
            ax, ay = points[i]
            bx, by = points[i + 1]
            for zone in self.zones:
                if self._point_in_zone(zone, bx, by):
                    return True
                if self._segment_hits_zone(ax, ay, bx, by, zone):
                    return True
        return False

    def _mission_cost(self, drone, package):
        leg = self._dist(drone.x, drone.y, package.x, package.y)
        back = self._dist(package.x, package.y, BASE[0], BASE[1])
        return (leg + back) * WEATHER_MULTIPLIER[self.weather]

    def snapshot(self):
        return {
            "seed": self.seed,
            "weather": self.weather,
            "zones": [list(z) for z in self.zones],
            "drones": [self.drones[k].to_dict() for k in sorted(self.drones)],
            "packages": [self.packages[k].to_dict() for k in sorted(self.packages)],
        }

    # -- command entry point ----------------------------------------------

    def cmd(self, line):
        if not isinstance(line, str):
            return "ERR invalid input"
        tokens = line.split()
        if tokens and tokens[0].upper() == "CMD":
            tokens = tokens[1:]
        if not tokens:
            return "ERR empty command"
        name = tokens[0].upper()
        if name.startswith("_"):
            return "ERR unknown command"
        handler = getattr(self, "_do_" + name.lower(), None)
        if handler is None:
            return "ERR unknown command"
        try:
            return handler(tokens[1:])
        except (ValueError, TypeError, IndexError, KeyError):
            return "ERR invalid arguments"

    # -- commands -----------------------------------------------------------

    def _do_add_drone(self, args):
        if len(args) != 5:
            return "ERR invalid arguments"
        drone_id = args[0]
        x, y, battery, capacity = (int(v) for v in args[1:])
        if drone_id in self.drones:
            return "ERR duplicate drone"
        if battery < 0 or capacity < 0:
            return "ERR invalid arguments"
        self.drones[drone_id] = Drone(drone_id, x, y, battery, capacity)
        return "OK drone %s added" % drone_id

    def _do_add_package(self, args):
        if len(args) != 4:
            return "ERR invalid arguments"
        package_id = args[0]
        x, y, weight = (int(v) for v in args[1:])
        if package_id in self.packages:
            return "ERR duplicate package"
        if weight <= 0:
            return "ERR invalid arguments"
        self.packages[package_id] = Package(package_id, x, y, weight)
        return "OK package %s added" % package_id

    def _do_nofly(self, args):
        if len(args) != 4:
            return "ERR invalid arguments"
        x1, y1, x2, y2 = (int(v) for v in args)
        zone = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.zones.append(zone)
        return "OK nofly zone added"

    def _do_weather(self, args):
        if len(args) != 1 or args[0] not in WEATHER_MULTIPLIER:
            return "ERR invalid arguments"
        # Only future assignments are priced with the new multiplier;
        # accepted tasks keep the cost frozen at accept time.
        self.weather = args[0]
        return "OK weather %s" % self.weather

    def _do_assign(self, args):
        if len(args) != 2:
            return "ERR invalid arguments"
        drone = self.drones.get(args[0])
        package = self.packages.get(args[1])
        if drone is None or package is None:
            return "ERR not found"
        if drone.state != "idle":
            return "ERR drone busy"
        if package.status != "pending":
            return "ERR package not pending"
        if package.weight > drone.capacity:
            return "ERR overweight"
        route = [(drone.x, drone.y), (package.x, package.y), BASE]
        if self._route_blocked(route):
            return "ERR nofly zone"
        cost = self._mission_cost(drone, package)
        if drone.battery < cost:
            return "ERR low battery"
        drone.state = "assigned"
        drone.package_id = package.id
        drone.task_cost = cost
        drone.route = route
        package.status = "assigned"
        package.assigned_to = drone.id
        return "OK assigned %s -> %s cost %d" % (drone.id, package.id, cost)

    def _do_pickup(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        drone = self.drones.get(args[0])
        if drone is None:
            return "ERR not found"
        if drone.state != "assigned":
            return "ERR not assigned"
        package = self.packages[drone.package_id]
        drone.battery -= drone.task_cost  # frozen cost, weather-proof
        drone.x, drone.y = package.x, package.y
        drone.state = "flying"
        package.status = "in_transit"
        return "OK picked up %s" % package.id

    def _do_deliver(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        drone = self.drones.get(args[0])
        if drone is None:
            return "ERR not found"
        if drone.state != "flying":
            return "ERR not carrying"
        package = self.packages[drone.package_id]
        package.status = "delivered"
        drone.reputation += 1  # exactly once per delivery
        drone.package_id = None
        drone.task_cost = 0
        drone.state = "returning"
        return "OK delivered %s" % package.id

    def _do_return(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        drone = self.drones.get(args[0])
        if drone is None:
            return "ERR not found"
        if drone.state != "returning":
            return "ERR not returning"
        drone.x, drone.y = BASE
        drone.route = []  # corridor released on arrival
        drone.state = "idle"
        return "OK returned %s" % drone.id

    def _do_cancel(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        package = self.packages.get(args[0])
        if package is None:
            return "ERR not found"
        if package.status not in ("assigned", "in_transit"):
            return "ERR not cancellable"
        drone = self.drones.get(package.assigned_to)
        if drone is not None:
            drone.package_id = None
            drone.task_cost = 0
            drone.route = []
            drone.state = "returning" if drone.state == "flying" else "idle"
        package.status = "pending"  # package restored for re-dispatch
        package.assigned_to = None
        return "OK cancelled %s" % package.id

    def _do_auto(self, args):
        if args:
            return "ERR invalid arguments"
        count = 0
        for pid in sorted(self.packages):
            package = self.packages[pid]
            if package.status != "pending":
                continue
            candidates = []
            for did in sorted(self.drones):
                drone = self.drones[did]
                if drone.state != "idle" or package.weight > drone.capacity:
                    continue
                route = [(drone.x, drone.y), (package.x, package.y), BASE]
                if self._route_blocked(route):
                    continue
                cost = self._mission_cost(drone, package)
                if drone.battery >= cost:
                    dist = self._dist(drone.x, drone.y, package.x, package.y)
                    candidates.append((dist, self.rng.random(), did))
            if candidates:
                candidates.sort()
                result = self._do_assign([candidates[0][2], pid])
                if result.startswith("OK"):
                    count += 1
        return "OK auto assigned %d" % count

    def _do_save(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        with open(args[0], "w", encoding="utf-8") as fh:
            json.dump(self.snapshot(), fh, sort_keys=True)
        return "OK saved %s" % args[0]

    def _do_load(self, args):
        if len(args) != 1:
            return "ERR invalid arguments"
        try:
            with open(args[0], "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return "ERR cannot load"
        if not isinstance(data, dict) or not data:
            return "ERR invalid archive"
        try:
            drones = {d["id"]: Drone.from_dict(d) for d in data["drones"]}
            packages = {p["id"]: Package.from_dict(p) for p in data["packages"]}
            weather = data["weather"]
            if weather not in WEATHER_MULTIPLIER:
                raise ValueError("bad weather")
            zones = [tuple(z) for z in data["zones"]]
            seed = int(data["seed"])
        except (KeyError, TypeError, ValueError):
            return "ERR invalid archive"
        # Commit only after the whole archive validates.
        self.drones = drones
        self.packages = packages
        self.weather = weather
        self.zones = zones
        self.seed = seed
        self.rng = random.Random(seed)
        return "OK loaded %s" % args[0]

    def _do_status(self, args):
        if args:
            return "ERR invalid arguments"
        return "OK " + json.dumps(self.snapshot(), sort_keys=True)


DEMO_SCRIPT = [
    "ADD_DRONE d1 0 0 100 5",
    "ADD_DRONE d2 1 1 60 5",
    "ADD_PACKAGE p1 3 0 2",
    "ADD_PACKAGE p2 0 4 1",
    "NOFLY 2 2 4 4",
    "ASSIGN d1 p1",
    "ASSIGN d2 p1",
    "PICKUP d1",
    "WEATHER wind",
    "DELIVER d1",
    "DELIVER d1",
    "RETURN d1",
    "AUTO",
    "SAVE demo_save.json",
    "WEATHER storm",
    "LOAD demo_save.json",
    "STATUS",
]


def main():
    dispatch = Dispatch()
    for line in DEMO_SCRIPT:
        print("CMD %-24s -> %s" % (line, dispatch.cmd(line)))


if __name__ == "__main__":
    main()
