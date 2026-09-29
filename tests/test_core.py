"""Regression tests for the drone courier dispatch core.

Fixed seed, JSON archive, pure standard library. Run: python -m unittest -v
"""

import json
import os
import tempfile
import unittest

from drone.core import BASE, Dispatch, SEED


def make_dispatch():
    return Dispatch(seed=SEED)


class RegressionTests(unittest.TestCase):
    def test_package_claimed_by_single_drone(self):
        d = make_dispatch()
        self.assertEqual(d.cmd("ADD_DRONE d1 0 0 100 5"), "OK drone d1 added")
        self.assertEqual(d.cmd("ADD_DRONE d2 1 1 100 5"), "OK drone d2 added")
        d.cmd("ADD_PACKAGE p1 3 0 1")
        self.assertTrue(d.cmd("ASSIGN d1 p1").startswith("OK"))
        second = d.cmd("ASSIGN d2 p1")
        self.assertTrue(second.startswith("ERR"), second)
        self.assertEqual(d.packages["p1"].assigned_to, "d1")
        self.assertEqual(d.drones["d2"].state, "idle")
        self.assertIsNone(d.drones["d2"].package_id)

    def test_insufficient_battery_rejected(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 10 5")
        d.cmd("ADD_PACKAGE p1 3 4 1")  # cost = (7 + 7) * 1 = 14 > 10
        result = d.cmd("ASSIGN d1 p1")
        self.assertEqual(result, "ERR low battery")
        self.assertEqual(d.drones["d1"].state, "idle")
        self.assertEqual(d.drones["d1"].battery, 10)
        self.assertEqual(d.packages["p1"].status, "pending")
        d.cmd("ADD_PACKAGE p2 2 1 1")  # cost = (3 + 3) * 1 = 6 <= 10
        self.assertTrue(d.cmd("ASSIGN d1 p2").startswith("OK"))

    def test_nofly_zone_boundary_detected(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 500 5")
        d.cmd("NOFLY 2 2 4 4")
        d.cmd("ADD_PACKAGE p_edge 4 4 1")  # on the boundary counts as inside
        self.assertEqual(d.cmd("ASSIGN d1 p_edge"), "ERR nofly zone")
        d.cmd("ADD_PACKAGE p_corner 2 4 1")  # corner boundary also inside
        self.assertEqual(d.cmd("ASSIGN d1 p_corner"), "ERR nofly zone")
        d.cmd("ADD_PACKAGE p_free 5 0 1")  # clearly outside
        self.assertTrue(d.cmd("ASSIGN d1 p_free").startswith("OK"))
        d.cmd("ADD_DRONE d2 0 5 500 5")
        d.cmd("ADD_PACKAGE p_cross 5 5 1")  # straight leg crosses the zone
        self.assertEqual(d.cmd("ASSIGN d2 p_cross"), "ERR nofly zone")

    def test_route_released_on_return(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        d.cmd("ADD_PACKAGE p1 3 0 1")
        d.cmd("ASSIGN d1 p1")
        self.assertTrue(d.drones["d1"].route, "route reserved while assigned")
        d.cmd("PICKUP d1")
        d.cmd("DELIVER d1")
        self.assertTrue(d.cmd("RETURN d1").startswith("OK"))
        drone = d.drones["d1"]
        self.assertEqual(drone.route, [])
        self.assertEqual(drone.state, "idle")
        self.assertEqual((drone.x, drone.y), BASE)

    def test_weather_change_does_not_recompute_airborne(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        d.cmd("ADD_PACKAGE p1 3 0 1")  # clear cost = (3 + 3) * 1 = 6
        d.cmd("ASSIGN d1 p1")
        d.cmd("PICKUP d1")
        self.assertEqual(d.drones["d1"].battery, 94)
        self.assertTrue(d.cmd("WEATHER storm").startswith("OK"))
        # Airborne task must not be recomputed under storm pricing.
        self.assertEqual(d.drones["d1"].battery, 94)
        self.assertEqual(d.packages["p1"].status, "in_transit")
        self.assertTrue(d.cmd("DELIVER d1").startswith("OK"))
        self.assertEqual(d.drones["d1"].battery, 94)
        # New assignments do use the storm multiplier.
        d.cmd("RETURN d1")
        d.cmd("ADD_PACKAGE p2 3 0 1")  # storm cost = 6 * 3 = 18
        self.assertEqual(d.cmd("ASSIGN d1 p2"), "OK assigned d1 -> p2 cost 18")

    def test_cancel_restores_package(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        d.cmd("ADD_DRONE d2 0 0 100 5")
        d.cmd("ADD_PACKAGE p1 3 0 1")
        d.cmd("ASSIGN d1 p1")
        self.assertTrue(d.cmd("CANCEL p1").startswith("OK"))
        package = d.packages["p1"]
        self.assertEqual(package.status, "pending")
        self.assertIsNone(package.assigned_to)
        self.assertEqual(d.drones["d1"].state, "idle")
        self.assertEqual(d.drones["d1"].route, [])
        self.assertTrue(d.cmd("ASSIGN d2 p1").startswith("OK"))
        self.assertEqual(d.cmd("CANCEL p1"), "OK cancelled p1")
        self.assertTrue(d.cmd("ASSIGN d1 p1").startswith("OK"))
        d.cmd("PICKUP d1")
        d.cmd("DELIVER d1")
        self.assertEqual(d.cmd("CANCEL p1"), "ERR not cancellable")

    def test_duplicate_delivery_counts_reputation_once(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        d.cmd("ADD_PACKAGE p1 3 0 1")
        d.cmd("ASSIGN d1 p1")
        d.cmd("PICKUP d1")
        self.assertTrue(d.cmd("DELIVER d1").startswith("OK"))
        self.assertEqual(d.drones["d1"].reputation, 1)
        again = d.cmd("DELIVER d1")
        self.assertTrue(again.startswith("ERR"), again)
        self.assertEqual(d.drones["d1"].reputation, 1)
        self.assertEqual(d.packages["p1"].status, "delivered")

    def test_load_restores_position_and_battery(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 2 3 100 5")
        d.cmd("ADD_PACKAGE p1 4 3 1")
        d.cmd("ASSIGN d1 p1")
        d.cmd("PICKUP d1")  # drone now at (4, 3) with reduced battery
        saved = (d.drones["d1"].x, d.drones["d1"].y, d.drones["d1"].battery)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            self.assertTrue(d.cmd("SAVE " + path).startswith("OK"))
            # Mutate live state so a stale load would be visible.
            d.cmd("WEATHER storm")
            d.cmd("ADD_DRONE d2 9 9 50 5")
            d.drones["d1"].x = 99
            d.drones["d1"].battery = 1
            self.assertTrue(d.cmd("LOAD " + path).startswith("OK"))
        drone = d.drones["d1"]
        self.assertEqual((drone.x, drone.y, drone.battery), saved)
        self.assertEqual((drone.x, drone.y), (4, 3))
        self.assertEqual(d.weather, "clear")
        self.assertNotIn("d2", d.drones)
        self.assertEqual(d.packages["p1"].status, "in_transit")


class SupplementaryTests(unittest.TestCase):
    def test_illegal_commands_change_nothing(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        before = d.cmd("STATUS")
        self.assertEqual(d.cmd("FLY d1"), "ERR unknown command")
        self.assertEqual(d.cmd(""), "ERR empty command")
        self.assertEqual(d.cmd("CMD"), "ERR empty command")
        self.assertEqual(d.cmd(None), "ERR invalid input")
        self.assertEqual(d.cmd("ASSIGN d1"), "ERR invalid arguments")
        self.assertEqual(d.cmd("ADD_DRONE d2 a b c d"), "ERR invalid arguments")
        self.assertEqual(d.cmd("WEATHER hail"), "ERR invalid arguments")
        self.assertEqual(d.cmd("STATUS"), before)
        self.assertTrue(d.cmd("CMD STATUS").startswith("OK"))

    def test_empty_data_handled(self):
        d = make_dispatch()
        self.assertTrue(d.cmd("STATUS").startswith("OK"))
        self.assertEqual(d.cmd("ASSIGN ghost ghost"), "ERR not found")
        self.assertEqual(d.cmd("PICKUP ghost"), "ERR not found")
        self.assertEqual(d.cmd("CANCEL ghost"), "ERR not found")
        self.assertEqual(d.cmd("DELIVER ghost"), "ERR not found")
        self.assertEqual(d.cmd("AUTO"), "OK auto assigned 0")
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "empty.json")
            with open(empty, "w", encoding="utf-8") as fh:
                fh.write("{}")
            self.assertEqual(d.cmd("LOAD " + empty), "ERR invalid archive")
            broken = os.path.join(tmp, "broken.json")
            with open(broken, "w", encoding="utf-8") as fh:
                fh.write("not json")
            self.assertEqual(d.cmd("LOAD " + broken), "ERR cannot load")
            self.assertEqual(
                d.cmd("LOAD " + os.path.join(tmp, "missing.json")),
                "ERR cannot load",
            )
        self.assertEqual(d.drones, {})

    def test_capacity_boundary(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 500 5")
        d.cmd("ADD_PACKAGE p_fit 1 0 5")  # weight == capacity is allowed
        self.assertTrue(d.cmd("ASSIGN d1 p_fit").startswith("OK"))
        d.cmd("ADD_DRONE d2 0 0 500 5")
        d.cmd("ADD_PACKAGE p_over 1 0 6")  # capacity + 1 is rejected
        self.assertEqual(d.cmd("ASSIGN d2 p_over"), "ERR overweight")
        self.assertEqual(d.drones["d2"].state, "idle")
        self.assertEqual(d.packages["p_over"].status, "pending")

    def test_duplicate_input_rejected(self):
        d = make_dispatch()
        d.cmd("ADD_DRONE d1 0 0 100 5")
        self.assertEqual(d.cmd("ADD_DRONE d1 9 9 1 1"), "ERR duplicate drone")
        self.assertEqual((d.drones["d1"].x, d.drones["d1"].battery), (0, 100))
        d.cmd("ADD_PACKAGE p1 3 0 1")
        self.assertEqual(d.cmd("ADD_PACKAGE p1 5 5 2"), "ERR duplicate package")
        self.assertEqual((d.packages["p1"].x, d.packages["p1"].weight), (3, 1))
        self.assertTrue(d.cmd("ASSIGN d1 p1").startswith("OK"))
        self.assertEqual(d.cmd("ASSIGN d1 p1"), "ERR drone busy")
        self.assertEqual(d.packages["p1"].assigned_to, "d1")


if __name__ == "__main__":
    unittest.main()
