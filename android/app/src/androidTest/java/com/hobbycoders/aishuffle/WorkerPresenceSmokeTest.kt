package com.hobbycoders.aishuffle

import android.widget.TextView
import android.view.View
import android.view.ViewGroup
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountSession
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ServerListActivity
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

@RunWith(AndroidJUnit4::class)
class WorkerPresenceSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private fun views(view: View): List<View> = listOf(view) + if (view is ViewGroup) (0 until view.childCount).flatMap { views(view.getChildAt(it)) } else emptyList()
    private fun waitFor(message: String, condition: () -> Boolean) = runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!condition()) kotlinx.coroutines.delay(50) } }
        catch (error: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, error) }
    }
    private fun clean() = instrumentation.runOnMainSync {
        DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
        ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; selectedToolsServerId = null }
    }
    private fun description(scenario: ActivityScenario<ServerListActivity>, name: String, state: String): Boolean {
        var found = false
        scenario.onActivity { activity -> found = views(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>().any {
            it.contentDescription?.toString()?.contains("$name. $state.") == true && it.isShown
        } }
        return found
    }

    @Test fun eachAccountWorkerGetsItsOwnAccessibleStatusWithoutRebuildingRows() {
        clean()
        val account = AccountSession("https://hub-presence.example", "fixture-account-token", Instant.now().plusSeconds(3600).toString(), "viewing-phone", "account-one", "tester", true)
        AccountStore(context).save(account)
        val first = Server("account:worker-one", "Home fixture", "https://one-presence.example", "aim_fixture-one", account.deviceId,
            accountId = account.accountId, hubUrl = account.hub, hubServerId = "worker-one")
        val second = first.copy(id = "account:worker-two", name = "Desk fixture", baseUrl = "https://two-presence.example", hubServerId = "worker-two")
        ServerStore.get(context).apply { add(first); add(second) }
        val requests = AtomicInteger()
        PresenceFixture { request ->
            requests.incrementAndGet()
            check(request.header("Authorization") == null && request.header("Cookie") == null)
            check(request.url.encodedPath == "/api/v1/mobile/hub/endpoint")
            // Both return worker-one, so only the actual first worker is verified.
            PresenceFixture.response(request, 200, """{"ready":true,"protected_ingress":true,"server_id":"worker-one"}""")
        }.use {
            try {
                ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
                    var originalRow: View? = null
                    scenario.onActivity { activity -> originalRow = views(activity.findViewById(android.R.id.content)).first { it.contentDescription == "Options for Home fixture" } }
                    waitFor("Per-worker statuses did not settle") { description(scenario, first.name, "Online") && description(scenario, second.name, "Status unavailable") }
                    scenario.onActivity { activity ->
                        assertSame(originalRow, views(activity.findViewById(android.R.id.content)).first { it.contentDescription == "Options for Home fixture" })
                        assertTrue(originalRow!!.performClick())
                    }
                    androidx.test.espresso.Espresso.onView(androidx.test.espresso.matcher.ViewMatchers.withText("Remove from account"))
                        .check(androidx.test.espresso.assertion.ViewAssertions.matches(androidx.test.espresso.matcher.ViewMatchers.isDisplayed()))
                    androidx.test.espresso.Espresso.pressBack()
                    instrumentation.waitForIdleSync()
                    TestScreenshots.capture("worker-presence-two-distinct-workers")
                    assertEquals(2, requests.get())
                }
            } finally { clean() }
        }
    }

    @Test fun stoppedScreenCancelsPendingProbeAndChangedWorkerCannotInheritGreen() {
        clean()
        val original = Server("direct-worker", "Direct fixture", "https://direct-presence.example", "aim_fixture", "viewing-phone")
        ServerStore.get(context).add(original)
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val firstRequest = AtomicBoolean(true)
        val requests = AtomicInteger()
        PresenceFixture { request ->
            requests.incrementAndGet()
            if (firstRequest.getAndSet(false)) {
                started.countDown(); check(release.await(8, TimeUnit.SECONDS))
                PresenceFixture.response(request, 200, """{"status":"healthy","service":"Shuffle","version":"fixture","authenticated":false}""")
            } else PresenceFixture.response(request, 503, "{}")
        }.use {
            try {
                ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
                    assertTrue(started.await(5, TimeUnit.SECONDS))
                    scenario.moveToState(Lifecycle.State.CREATED)
                    ServerStore.get(context).update(original.copy(baseUrl = "https://changed-presence.example", apiKey = "aim_replaced"))
                    release.countDown()
                    scenario.moveToState(Lifecycle.State.RESUMED)
                    waitFor("Resumed worker was not checked independently") { description(scenario, original.name, "Status unavailable") }
                    assertFalse(description(scenario, original.name, "Online"))
                    assertEquals(2, requests.get())
                }
            } finally { release.countDown(); clean() }
        }
    }

    @Test fun directWorkerManualRefreshShowsTwoFailureOfflineAndRecovery() {
        clean()
        val server = Server("direct-worker", "Reachability fixture", "https://direct-presence.example", "aim_fixture", "viewing-phone")
        ServerStore.get(context).add(server)
        val requests = AtomicInteger()
        val resumedProbe = CountDownLatch(1)
        val releaseResume = CountDownLatch(1)
        PresenceFixture(validatedNetwork = true) { request ->
            check(request.url.encodedPath == "/api/v1/health")
            check(request.header("Authorization") == null && request.header("Cookie") == null)
            val step = requests.incrementAndGet()
            if (step == 5) { resumedProbe.countDown(); check(releaseResume.await(8, TimeUnit.SECONDS)) }
            if (step == 2 || step == 3) PresenceFixture.response(request, 502, "Gateway unavailable")
            else PresenceFixture.response(request, 200, """{"status":"healthy","service":"Shuffle","version":"fixture","authenticated":false}""")
        }.use { fixture ->
            try {
                ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
                    waitFor("Initial direct probe was not online") { description(scenario, server.name, "Online") }
                    waitFor("Worker screen did not become resumed and focused") {
                        var ready = false
                        scenario.onActivity { ready = it.lifecycle.currentState == Lifecycle.State.RESUMED && it.hasWindowFocus() }
                        ready
                    }
                    instrumentation.uiAutomation.waitForIdle(500, 5_000)
                    for (expected in listOf("Unknown", "Offline", "Online")) {
                        androidx.test.espresso.Espresso.onView(androidx.test.espresso.matcher.ViewMatchers.withContentDescription("Refresh workers"))
                            .perform(androidx.test.espresso.action.ViewActions.click())
                        waitFor("Manual refresh did not show $expected") { description(scenario, server.name, expected) }
                        if (expected == "Offline") scenario.onActivity { activity ->
                            assertTrue(views(activity.findViewById(android.R.id.content)).any { it.contentDescription?.toString()?.contains("Not reachable from this phone") == true })
                        }
                    }
                    assertEquals(4, requests.get())
                    scenario.moveToState(Lifecycle.State.CREATED)
                    fixture.network("another-synthetic-network", true)
                    scenario.moveToState(Lifecycle.State.RESUMED)
                    assertTrue(resumedProbe.await(5, TimeUnit.SECONDS))
                    assertTrue("Resume on another network must not republish cached green", description(scenario, server.name, "Unknown"))
                    releaseResume.countDown()
                    waitFor("The resumed direct probe did not recover") { description(scenario, server.name, "Online") }
                    assertEquals(5, requests.get())
                }
            } finally { releaseResume.countDown(); clean() }
        }
    }
}
