package com.hobbycoders.aishuffle

import android.content.Intent
import android.content.pm.ActivityInfo
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.action.ViewActions.scrollTo
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.isDisplayed
import androidx.test.espresso.matcher.ViewMatchers.withContentDescription
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.OfflineDemoActivity
import com.hobbycoders.aishuffle.ui.ServerListActivity
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class OfflineDemoSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun web(view: View): WebView? {
        if (view is WebView) return view
        if (view is ViewGroup) for (i in 0 until view.childCount) web(view.getChildAt(i))?.let { return it }
        return null
    }
    private fun evaluate(scenario: ActivityScenario<OfflineDemoActivity>, code: String): String {
        val completed = CountDownLatch(1)
        var value = ""
        scenario.onActivity { activity ->
            web(activity.window.decorView)!!.evaluateJavascript(code) { value = it; completed.countDown() }
        }
        assertTrue("JavaScript callback", completed.await(5, TimeUnit.SECONDS))
        return value
    }
    private fun await(scenario: ActivityScenario<OfflineDemoActivity>, code: String) = kotlinx.coroutines.runBlocking {
        kotlinx.coroutines.withTimeout(20_000) {
            while (evaluate(scenario, code) != "true") kotlinx.coroutines.delay(100)
        }
    }
    private fun openSettings() {
        // RESUMED/focused is not enough: launch windows can still mark a tap obscured.
        // Synchronize with platform window events before injecting the single gesture.
        instrumentation.uiAutomation.waitForIdle(500, 5_000)
        onView(withContentDescription("Settings")).perform(click())
    }
    @Test fun settingsEntryNeedsNoAccountAndReturnsToSettings() {
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false)
            AccountStore(context).clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) } }
        }
        ActivityScenario.launch<ServerListActivity>(Intent(context, ServerListActivity::class.java)).use {
            onView(withText("Explore offline demo")).check(doesNotExist())
            openSettings()
            repeat(2) {
                onView(withText("Explore offline demo")).perform(scrollTo(), click())
                onView(withText("Exit offline demo")).perform(click())
                onView(withText("Settings")).check(matches(isDisplayed()))
            }
            onView(withContentDescription("Back")).perform(click())
            onView(withText("Welcome to Shuffle")).check(matches(isDisplayed()))
            onView(withText("Explore offline demo")).check(doesNotExist())
        }
        assertNull(AccountStore(context).session())
        assertTrue(ServerStore.get(context).getAll().isEmpty())
    }
    @Test fun workerListOpensDemoOnlyThroughSettings() {
        val server = Server("demo-navigation", "Demo navigation worker", "https://worker.invalid", "test-key", "test-device")
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false)
            AccountStore(context).clear()
            ServerStore.get(context).apply {
                getAll().forEach { remove(it.id) }
                add(server)
            }
        }
        try {
            PresenceFixture().use {
                val touches = java.util.concurrent.CopyOnWriteArrayList<String>()
                val gestures = java.util.concurrent.CopyOnWriteArrayList<Pair<Int, Int>>()
                val monitor = androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry.getInstance()
                val callback = androidx.test.runner.lifecycle.ActivityLifecycleCallback { activity, stage ->
                    if (activity is ServerListActivity && stage == androidx.test.runner.lifecycle.Stage.CREATED) {
                        val original = activity.window.callback
                        activity.window.callback = object : android.view.Window.Callback by original {
                            override fun dispatchTouchEvent(event: android.view.MotionEvent): Boolean {
                                val buttons = ArrayList<View>()
                                activity.window.decorView.findViewsWithText(buttons, "Settings", View.FIND_VIEWS_WITH_CONTENT_DESCRIPTION)
                                val button = buttons.single()
                                val bounds = android.graphics.Rect().also { button.getGlobalVisibleRect(it) }
                                val detail = "action=${event.actionMasked} flags=${event.flags} x=${event.rawX} y=${event.rawY} " +
                                    "bounds=$bounds focused=${activity.hasWindowFocus()} pressed=${button.isPressed}"
                                touches += detail
                                gestures += event.actionMasked to event.flags
                                android.util.Log.i("DemoNavigation", detail)
                                return original.dispatchTouchEvent(event)
                            }
                        }
                    }
                }
                instrumentation.runOnMainSync { monitor.addLifecycleCallback(callback) }
                try {
                    // Independent launches, never a retry of an ignored tap. Stop at the first failure.
                    repeat(10) { launch ->
                        touches.clear()
                        gestures.clear()
                        ActivityScenario.launch<ServerListActivity>(Intent(context, ServerListActivity::class.java)).use {
                            onView(withText("Workers")).check(matches(isDisplayed()))
                            onView(withText("Explore offline demo")).check(doesNotExist())
                            openSettings()
                            assertEquals("Launch $launch must deliver one unobscured tap: $touches",
                                listOf(android.view.MotionEvent.ACTION_DOWN to 0, android.view.MotionEvent.ACTION_UP to 0), gestures.toList())
                            onView(withText("Settings")).check(matches(isDisplayed()))
                            if (launch == 0) TestScreenshots.capture("offline-demo-after-settings-tap")
                            onView(withText("Explore offline demo")).perform(scrollTo(), click())
                            onView(withText("Exit offline demo")).perform(click())
                            onView(withText("Settings")).check(matches(isDisplayed()))
                            onView(withContentDescription("Back")).perform(click())
                            onView(withText("Workers")).check(matches(isDisplayed()))
                            onView(withText("Explore offline demo")).check(doesNotExist())
                        }
                    }
                } finally {
                    instrumentation.runOnMainSync { monitor.removeLifecycleCallback(callback) }
                }
            }
            assertEquals(listOf(server), ServerStore.get(context).getAll())
            assertNull(AccountStore(context).session())
        } finally {
            instrumentation.runOnMainSync { ServerStore.get(context).remove(server.id) }
        }
    }
    @Test fun realBundleComposesResetsAndBlocksNetworkAcrossRotation() {
        val servers = ServerStore.get(context).getAll()
        val account = AccountStore(context).session()
        ActivityScenario.launch<OfflineDemoActivity>(Intent(context, OfflineDemoActivity::class.java)).use { scenario ->
            await(scenario, "document.body.innerText.includes('No account')")
            scenario.onActivity {
                val settings = web(it.window.decorView)!!.settings
                assertTrue(settings.blockNetworkLoads)
                assertFalse(settings.allowFileAccess)
                assertFalse(settings.allowContentAccess)
                assertTrue(settings.domStorageEnabled)
            }
            assertEquals("true", evaluate(scenario, "typeof window.__SHUFFLE_MOBILE__ === 'undefined'"))
            evaluate(scenario, "document.querySelector('textarea').value='Local test'; document.querySelector('textarea').dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('form').requestSubmit();")
            await(scenario, "document.body.innerText.includes('Preset demo reply')")
            TestScreenshots.capture("offline-demo-portrait")
            evaluate(scenario, "window.demoNetwork='pending'; fetch('https://example.com').then(()=>window.demoNetwork='escaped',()=>window.demoNetwork='blocked');")
            await(scenario, "window.demoNetwork === 'blocked'")
            evaluate(scenario, "Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='Reset sample').click()")
            await(scenario, "!document.body.innerText.includes('Preset demo reply')")
            scenario.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
            await(scenario, "document.body.innerText.includes('No account') && innerWidth > innerHeight")
            TestScreenshots.capture("offline-demo-landscape")
            scenario.recreate()
            await(scenario, "document.body.innerText.includes('No account')")
        }
        assertEquals(servers, ServerStore.get(context).getAll())
        assertEquals(account, AccountStore(context).session())
    }
}
