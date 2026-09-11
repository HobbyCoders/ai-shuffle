package com.hobbycoders.aishuffle.ui

import android.app.Activity
import android.view.View
import android.widget.FrameLayout
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35, 36])
class SystemInsetsTest {
    private fun insets(ime: Int = 0): WindowInsetsCompat = WindowInsetsCompat.Builder()
        .setInsets(WindowInsetsCompat.Type.systemBars(), Insets.of(0, 24, 0, 48))
        .setInsets(WindowInsetsCompat.Type.displayCutout(), Insets.of(40, 0, 0, 0))
        .setInsets(WindowInsetsCompat.Type.ime(), Insets.of(0, 0, 0, ime))
        .build()

    @Test fun barsCutoutAndKeyboardDoNotAccumulateAndSurviveChatChildReplacement() {
        Robolectric.buildActivity(Activity::class.java).setup().use { controller ->
            val activity = controller.get()
            activity.setContentView(FrameLayout(activity))
            val root = activity.findViewById<View>(android.R.id.content)
            root.setPadding(1, 2, 3, 4)
            activity.protectSystemInsets()
            repeat(2) {
                val result = ViewCompat.dispatchApplyWindowInsets(root, insets())
                assertTrue(result.isConsumed)
                assertEquals(listOf(41, 26, 3, 52), listOf(root.paddingLeft, root.paddingTop, root.paddingRight, root.paddingBottom))
            }
            // Real ChatActivity replaces the entire content child after NativeScreen setup.
            activity.setContentView(FrameLayout(activity))
            assertSame(root, activity.findViewById<View>(android.R.id.content))
            ViewCompat.dispatchApplyWindowInsets(root, insets(300))
            assertEquals(304, root.paddingBottom)
            ViewCompat.dispatchApplyWindowInsets(root, insets())
            assertEquals(52, root.paddingBottom)
        }
    }

    @Test @Config(qualifiers = "night") fun transparentBarsUseLightIconsInNightMode() {
        Robolectric.buildActivity(Activity::class.java).setup().use { controller ->
            val activity = controller.get()
            activity.setContentView(FrameLayout(activity))
            activity.protectSystemInsets()
            val bars = androidx.core.view.WindowCompat.getInsetsController(activity.window, activity.window.decorView)
            assertFalse(bars.isAppearanceLightStatusBars)
            assertFalse(bars.isAppearanceLightNavigationBars)
        }
    }

    @Test @Config(sdk = [34]) fun olderAndroidKeepsDecorFittingAndExistingPadding() {
        Robolectric.buildActivity(Activity::class.java).setup().use { controller ->
            val activity = controller.get()
            activity.setContentView(FrameLayout(activity))
            val root = activity.findViewById<View>(android.R.id.content)
            root.setPadding(1, 2, 3, 4)
            activity.protectSystemInsets()
            ViewCompat.dispatchApplyWindowInsets(root, insets(300))
            assertEquals(listOf(1, 2, 3, 4), listOf(root.paddingLeft, root.paddingTop, root.paddingRight, root.paddingBottom))
        }
    }
}
