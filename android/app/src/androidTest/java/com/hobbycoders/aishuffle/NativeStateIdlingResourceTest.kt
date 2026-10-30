package com.hobbycoders.aishuffle

import android.content.Intent
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.ui.AccountActivity
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class NativeStateIdlingResourceTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun launch() = ActivityScenario.launch<AccountActivity>(
        Intent(ApplicationProvider.getApplicationContext<android.content.Context>(), AccountActivity::class.java).putExtra("mode", "pair"))

    @Test fun reportsTheActualLayoutTransitionWithoutPollingOrRepeatingAnAction() {
        launch().use { scenario ->
            lateinit var resource: NativeStateIdlingResource
            lateinit var status: TextView
            val changed = CountDownLatch(1)
            scenario.onActivity { activity ->
                status = activity.findViewById(R.id.screenStatus)
                status.text = "Pending test state"
                resource = NativeStateIdlingResource("Rendered test state", activity.window.decorView,
                    { status.text == "Completed test state" })
                resource.registerIdleTransitionCallback { changed.countDown() }
                assertFalse(resource.isIdleNow())
            }
            try {
                assertEquals("A pending state must not signal idle", 1L, changed.count)
                scenario.onActivity { status.text = "Completed test state" }
                assertTrue("The layout callback did not publish idle", changed.await(8, TimeUnit.SECONDS))
                scenario.onActivity { assertTrue(resource.isIdleNow()) }
            } finally { instrumentation.runOnMainSync { resource.close() } }
        }
    }

    @Test fun resumedScreenWaitsForAFocusedCommittedFrame() {
        launch().use { scenario ->
            lateinit var resource: NativeStateIdlingResource
            val committed = CountDownLatch(1)
            scenario.onActivity { activity ->
                resource = NativeStateIdlingResource("Focused test frame", activity.window.decorView,
                    { true }, requireFocusedFrame = true)
                resource.registerIdleTransitionCallback { committed.countDown() }
                assertFalse("RESUMED alone cannot satisfy a frame barrier", resource.isIdleNow())
            }
            try {
                assertTrue("No focused frame was committed", committed.await(8, TimeUnit.SECONDS))
                scenario.onActivity { activity ->
                    assertTrue(activity.hasWindowFocus())
                    assertTrue(resource.isIdleNow())
                }
            } finally { instrumentation.runOnMainSync { resource.close() } }
        }
    }
}
