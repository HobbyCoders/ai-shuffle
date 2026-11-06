"""Keep instrumentation input tied to its window and Android transition barrier.

These source contracts complement on-device tests; they do not emulate Android.
"""
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "android/app/src/androidTest/java/com/hobbycoders/aishuffle"


def body(source, name):
    return source.split("private fun " + name + "(", 1)[1].split("\n    private fun ", 1)[0]


class InstrumentationRaceTest(unittest.TestCase):
    def test_optional_approval_never_injects_at_stale_screen_coordinates(self):
        source = (NATIVE / "CrossAppFenceTest.kt").read_text()
        optional = body(source, "touchIfStillUp")
        self.assertIn("panelButton(text)", optional)
        self.assertIn("target.performAction(AccessibilityNodeInfo.ACTION_CLICK)", optional)
        self.assertNotRegex(optional, r"\b(?:inject|injectInputEvent|getBoundsInScreen)\s*\(")
        lookup = body(source, "panelButton")
        self.assertIn("AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY", lookup)
        self.assertIn("it.packageName?.toString() == context.packageName", lookup)

    def test_forced_close_regression_preserves_the_no_target_tap_assertion(self):
        source = (NATIVE / "CrossAppFenceTest.kt").read_text()
        signature = "@Test fun approvalClosingBetweenLookupAndActionCannotTapTheTarget()"
        self.assertTrue(signature in source, "Missing forced panel-close regression")
        case = source.split(signature, 1)[1].split("@Test", 1)[0]
        for required in ('touchIfStillUp("Allow once") {', 'fixture("swap")',
                         'assertEquals("denied", status(tapping).getString("status"))',
                         'panelButton("Allow once") == null', "assertNotTapped()",
                         "assertThrows(AssertionError::class.java)"):
            self.assertIn(required, case)
        # Lookup must happen BEFORE the forced closure, then only the captured
        # node may receive the action. Rechecking followed by raw input is racy.
        optional = body(source, "touchIfStillUp")
        self.assertLess(optional.index("panelButton(text)"), optional.index("beforeAction()"))
        self.assertLess(optional.index("beforeAction()"), optional.index("target.performAction("))

    def test_toolbar_taps_cross_the_platform_rotation_barrier_once(self):
        source = (NATIVE / "ChatToolbarSmokeTest.kt").read_text()
        self.assertTrue("import androidx.test.espresso.action.ViewActions.click" not in source,
                        "Toolbar must use the platform input barrier")
        self.assertTrue(".perform(click())" not in source, "Do not bypass the input barrier")
        for target in ('withContentDescription("More chat options")', 'withText("Refresh chat")',
                       'withContentDescription("Back")'):
            self.assertEqual(1, source.count("tapNative(onView(" + target + "))"))
        self.assertIn("scenario.state == androidx.lifecycle.Lifecycle.State.DESTROYED", source)
        helper = (NATIVE / "NativeStateIdlingResource.kt").read_text().split("internal fun tapNative(", 1)[1].split("\ninternal fun ", 1)[0]
        for event in ("down", "up"):
            self.assertIn(f"automation.injectInputEvent({event}, true)", helper)
        self.assertNotIn("ACTION_CLICK", helper)

    def test_coordinate_injection_mutation_is_rejected(self):
        path = NATIVE / "CrossAppFenceTest.kt"
        original = path.read_text()
        mutated = original.replace("target.performAction(AccessibilityNodeInfo.ACTION_CLICK)", "inject(Rect())")
        self.assertTrue(original != mutated, "Missing captured-node action to mutate")
        read_text = Path.read_text
        def read_source(p, *args, **kwargs):
            return mutated if p == path else read_text(p, *args, **kwargs)
        with patch.object(Path, "read_text", new=read_source):
            with self.assertRaises(AssertionError):
                self.test_optional_approval_never_injects_at_stale_screen_coordinates()


if __name__ == "__main__":
    unittest.main()
