package com.hobbycoders.aishuffle

import android.content.Intent
import android.content.pm.ActivityInfo
import androidx.appcompat.app.AppCompatDelegate
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolApprovals
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.voice.VoiceCallActivity
import com.hobbycoders.aishuffle.voice.VoiceCallService
import com.hobbycoders.aishuffle.voice.VoiceCallState
import org.junit.*
import org.junit.runner.RunWith

/**
 * Separate instrumentation invocation with RECORD_AUDIO already denied by the host.
 * Revoking a granted runtime permission inside this process kills instrumentation
 * on Android 34, so this fixture asserts its precondition and never changes it.
 */
@RunWith(AndroidJUnit4::class)
class VoicePermissionSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    @Before fun clearLocalState() {
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolApprovals.cancelAll(); ToolsPreferences(context).setEnabled(false)
            AccountStore(context).apply { clear(); hub = AccountStore.DEFAULT_HUB }
            val store = ServerStore.get(context); store.getAll().forEach { store.remove(it.id) }; store.selectedToolsServerId = null
            AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)
        }
    }
    @After fun stopRuntime() { instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO) } }

    private fun <T : android.app.Activity> awaitFocused(scenario: ActivityScenario<T>) {
        awaitCondition("Activity never became resumed and focused") {
            var focused = false
            scenario.onActivity { focused = it.hasWindowFocus() && (it as androidx.lifecycle.LifecycleOwner).lifecycle.currentState == androidx.lifecycle.Lifecycle.State.RESUMED }
            focused
        }
    }

    private fun awaitCondition(message: String, predicate: () -> Boolean) = kotlinx.coroutines.runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!predicate()) kotlinx.coroutines.delay(50) } }
        catch (e: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, e) }
    }

    private fun <T : android.app.Activity> screenshot(scenario: ActivityScenario<T>, name: String) {
        scenario.onActivity { activity ->
            activity.findViewById<android.widget.ScrollView>(R.id.screenScroll)?.scrollTo(0, 0)
            val root = activity.findViewById<android.view.ViewGroup>(android.R.id.content)
            (root.getChildAt(0) as? android.widget.ScrollView)?.scrollTo(0, 0)
        }
        instrumentation.waitForIdleSync()
        instrumentation.uiAutomation.waitForIdle(250, 5_000)
        TestScreenshots.capture(name)
    }
    private fun <T : android.app.Activity> variants(scenario: ActivityScenario<T>, name: String) {
        awaitFocused(scenario)
        screenshot(scenario, "$name-portrait-day")
        scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
        awaitCondition("Landscape activity did not resume") {
            var ready = false
            scenario.onActivity { ready = it.hasWindowFocus() && it.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE }
            ready
        }
        screenshot(scenario, "$name-landscape-day")
        scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT; AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_YES) }
        awaitCondition("Portrait night activity did not resume") {
            var ready = false
            scenario.onActivity {
                val config = it.resources.configuration
                ready = it.hasWindowFocus() && config.orientation == android.content.res.Configuration.ORIENTATION_PORTRAIT &&
                    config.uiMode and android.content.res.Configuration.UI_MODE_NIGHT_MASK == android.content.res.Configuration.UI_MODE_NIGHT_YES
            }
            ready
        }
        screenshot(scenario, "$name-portrait-night")
    }
    @Test fun voiceWithoutMicrophoneShowsControlsAndDoesNotStartService() {
        Assert.assertNotEquals("Run this class alone after the host revokes RECORD_AUDIO before instrumentation starts", android.content.pm.PackageManager.PERMISSION_GRANTED, androidx.core.content.ContextCompat.checkSelfPermission(context, android.Manifest.permission.RECORD_AUDIO))
        val server = com.hobbycoders.aishuffle.model.Server.create("Voice test server", "https://instrumentation.invalid", "test-only")
        ServerStore.get(context).add(server)
        Assert.assertEquals(server, ServerStore.get(context).getById(server.id))
        ActivityScenario.launch<VoiceCallActivity>(Intent(context, VoiceCallActivity::class.java).putExtra(VoiceCallService.EXTRA_SERVER_ID, server.id)).use { scenario ->
            awaitFocused(scenario)
            instrumentation.runOnMainSync { VoiceCallState.update { it.copy(userCaption = "State update while microphone is off") } }
            onView(withText("Enable microphone")).check(matches(isDisplayed()))
            onView(withId(R.id.statusText)).check(matches(withText("Microphone is off. Voice stays off until you grant access.")))
            onView(withId(R.id.hangupButton)).check(matches(isDisplayed()))
            Assert.assertFalse(VoiceCallState.isActive)
            val manager = context.getSystemService(android.content.Context.ACTIVITY_SERVICE) as android.app.ActivityManager
            @Suppress("DEPRECATION")
            val voiceServiceRunning = manager.getRunningServices(Int.MAX_VALUE).any { it.service.className == VoiceCallService::class.java.name }
            Assert.assertFalse("Denied microphone must not start VoiceCallService", voiceServiceRunning)
            variants(scenario, "voice-microphone-off")
        }
        Assert.assertFalse(VoiceCallState.isActive)
    }
}
