package com.hobbycoders.aishuffle.ui

import android.content.Intent
import androidx.appcompat.app.AppCompatActivity
import com.hobbycoders.aishuffle.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * While connected, the page hides the app's bar, so the worker sheet's
 * "App settings" message is how the phone reaches its account and Sign out
 * (#951). This drives the message the page posts through the same parse and
 * routing chat uses.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ShellNavigationTest {
    private fun chat() = Robolectric.buildActivity(AppCompatActivity::class.java).also {
        it.get().setTheme(R.style.Theme_AIShuffle)
    }.setup()

    @Test fun appSettingsOpensTheAppsOwnSettingsScreenAboveChat() {
        chat().use { controller ->
            val activity = controller.get()
            val intent = ShellNavigation.intent(activity, MobileUi.shellMessage("{\"type\":\"app-settings\"}"))
            activity.startActivity(requireNotNull(intent) { "app-settings must open a screen" })
            val started = shadowOf(activity).nextStartedActivity
            assertEquals(SettingsActivity::class.java.name, started.component?.className)
            // Opened on top of chat, so Back from Settings (or after closing Account) returns to it.
            assertEquals(0, started.flags and (Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            assertNull(shadowOf(activity).nextStartedActivity)
        }
    }

    @Test fun workersStillOpensTheWorkersScreenForRecovery() {
        chat().use { controller ->
            val activity = controller.get()
            activity.startActivity(requireNotNull(ShellNavigation.intent(activity, MobileUi.shellMessage("{\"type\":\"workers\"}"))))
            val started = shadowOf(activity).nextStartedActivity
            assertEquals(ServerListActivity::class.java.name, started.component?.className)
            assertNotEquals(0, started.flags and Intent.FLAG_ACTIVITY_SINGLE_TOP)
            assertNull(started.getStringExtra("sync_message"))
            assertEquals("Authorize it first.", ShellNavigation.workers(activity, "Authorize it first.").getStringExtra("sync_message"))
        }
    }

    @Test fun otherMessagesOpenNothing() {
        chat().use { controller ->
            val activity = controller.get()
            for (raw in listOf("{\"type\":\"worker-bar\",\"shown\":true}", "{\"type\":\"voice-call\",\"active\":true}",
                    "{\"type\":\"settings\"}", "{\"type\":\"app-settings\",\"pad\":\"" + "x".repeat(300) + "\"}", null)) {
                assertNull(raw.toString(), ShellNavigation.intent(activity, MobileUi.shellMessage(raw)))
            }
        }
    }
}
