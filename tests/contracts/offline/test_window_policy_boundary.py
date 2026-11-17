"""Public per-request Native window-policy contract; no native software or GUI.

Independently verifiable input/confirmation boundaries for the new optional
``allow_window_creation`` selector, without a real PVsystCLI, GUI, or any mock of
``_winlaunch`` (window counts are a real-native acceptance item, not asserted here):

1. The two high-level request schemas keep an explicit ``False`` through
   ``model_dump(exclude_none=True)`` (so a confirmation ticket never drops it) and
   drop only ``None`` (so the historical request/checkpoint shape is preserved).
2. A strict ``allow_window_creation=False`` that is paired with the default GUI
   import method is rejected before any project/scene write or GUI side effect, in
   both the house task and the sourced prepare-then-house task.
"""
import threading
import unittest

from pvsyst_auto.api.schemas import HouseTaskRequest, SourcedHouseTaskRequest
from pvsyst_auto.workflows.house import run_house_task
from pvsyst_auto.workflows.sourced_house import run_sourced_house_task


class _LockedStore:
    """Minimal store double: the strict preflight path only needs the workspace lock.

    No other store attribute is provided on purpose; touching one would raise and
    fail the test, proving the rejection happens before any store/filesystem action.
    """

    def __init__(self):
        self.lock = threading.RLock()


class WindowPolicyBoundary(unittest.TestCase):
    def test_schema_confirmation_roundtrip(self):
        for model in (HouseTaskRequest, SourcedHouseTaskRequest):
            self.assertIn("allow_window_creation", model.model_fields)
            self.assertIsNone(model.model_fields["allow_window_creation"].default)
            for value in (False, True):
                dumped = model.model_construct(allow_window_creation=value).model_dump(exclude_none=True)
                self.assertIn("allow_window_creation", dumped, model.__name__)
                self.assertIs(dumped["allow_window_creation"], value)
            self.assertNotIn(
                "allow_window_creation",
                model.model_construct(allow_window_creation=None).model_dump(exclude_none=True),
                "%s: omission must add no key to preserve the historical shape" % model.__name__)

    def test_strict_false_gui_rejected_before_any_action(self):
        for request in ({"scene_import_method": "gui", "allow_window_creation": False},
                        {"allow_window_creation": False}):
            result = run_house_task(_LockedStore(), None, None, request=request)
            self.assertEqual(result["status"], "blocked", result)
            self.assertFalse(result["native_consumption_verified"])
            self.assertTrue(any("allow_window_creation=false requires" in message
                                for message in result["issues"]), result)
        sourced = run_sourced_house_task(
            _LockedStore(), None, None,
            request={"scene_import_method": "gui", "allow_window_creation": False}, binding={})
        self.assertEqual(sourced["status"], "blocked", sourced)
        self.assertTrue(any("allow_window_creation=false requires" in message
                            for message in sourced["issues"]), sourced)


if __name__ == "__main__":
    unittest.main()
