package com.hobbycoders.aishuffle

import android.content.Intent
import android.os.SystemClock
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
    private fun waitFor(message: String, timeoutMs: Long = 8_000, condition: () -> Boolean) = runBlocking {
        try { kotlinx.coroutines.withTimeout(timeoutMs) { while (!condition()) kotlinx.coroutines.delay(50) } }
        catch (error: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, error) }
    }
    private fun clean() = instrumentation.runOnMainSync {
        DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
        ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; selectedToolsServerId = null }
    }
    // Class-based ActivityScenario.launch sends ACTION_MAIN, which intentionally
    // auto-connects since #838. Management/status tests must open the list instead.
    private fun launchWorkers(launcher: Boolean = false): ActivityScenario<ServerListActivity> =
        ActivityScenario.launch(Intent(context, ServerListActivity::class.java).apply {
            if (launcher) action = Intent.ACTION_MAIN
        })
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
        PresenceFixture(validatedNetwork = true) { request ->
            requests.incrementAndGet()
            check(request.header("Authorization") == null && request.header("Cookie") == null)
            check(request.url.encodedPath == "/api/v1/mobile/hub/endpoint")
            // Both return worker-one, so only the actual first worker is verified.
            PresenceFixture.response(request, 200, """{"ready":true,"protected_ingress":true,"server_id":"worker-one"}""")
        }.use {
            try {
                launchWorkers().use { scenario ->
                    val activity = scenario.nativeActivity()
                    awaitNativeWindow(activity)
                    var originalRow: View? = null
                    scenario.onActivity { activity -> originalRow = views(activity.findViewById(android.R.id.content)).first { it.contentDescription == "Options for Home fixture" } }
                    awaitNativeState(activity, "Independent account-worker observations") {
                        listOf(first.name to "Online", second.name to "Status unavailable").all { (name, state) ->
                            views(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>().any {
                                it.contentDescription?.toString()?.contains("$name. $state.") == true && it.isShown
                            }
                        }
                    }
                    assertTrue(description(scenario, first.name, "Online"))
                    assertTrue(description(scenario, second.name, "Status unavailable"))
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

    @Test fun launcherOpensAnOnlineWorkerWithoutASecondProbeSweep() = launcherOnlineWorker(stalledProbe = false)
    @Test fun launcherSelectsOnlineWorkerByDeadlineEvenWhenAnotherProbeNeverCompletes() = launcherOnlineWorker(stalledProbe = true)

    private fun launcherOnlineWorker(stalledProbe: Boolean) {
        clean()
        val first = Server("startup-offline", "Unavailable startup fixture", "https://startup-offline-presence.example", "aim_fixture", "viewing-phone")
        val second = first.copy(id = "startup-online", name = "Online startup fixture", baseUrl = "https://startup-online-presence.example")
        ServerStore.get(context).apply { add(first); add(second) }
        val requests = AtomicInteger()
        val pending = CountDownLatch(1)
        val release = CountDownLatch(1)
        val opened = java.util.concurrent.CopyOnWriteArrayList<Intent>()
        // Exercise the actual launcher selection and hand-off, but keep this
        // presence test out of Chat's separate authenticated bootstrap flow.
        val monitor = object : android.app.Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): android.app.Instrumentation.ActivityResult? {
                if (intent.component?.className != "com.hobbycoders.aishuffle.ui.ChatActivity") return null
                opened.add(intent)
                return android.app.Instrumentation.ActivityResult(android.app.Activity.RESULT_CANCELED, null)
            }
        }
        instrumentation.addMonitor(monitor)
        PresenceFixture { request ->
            requests.incrementAndGet()
            if (request.url.host == "startup-online-presence.example")
                PresenceFixture.response(request, 200, """{"status":"healthy","service":"Shuffle","version":"fixture","authenticated":false}""")
            else {
                if (stalledProbe) {
                    pending.countDown()
                    // Deliberately never complete while selection is under test,
                    // like a system DNS lookup that ignores OkHttp's timeout.
                    release.await()
                }
                PresenceFixture.response(request, 503, "{}")
            }
        }.use {
            try {
                val startedAt = SystemClock.elapsedRealtime()
                launchWorkers(launcher = true).use { scenario ->
                    if (stalledProbe) assertTrue(pending.await(5, TimeUnit.SECONDS))
                    waitFor("Launcher did not hand off to the checked online worker", if (stalledProbe) 12_000 else 8_000) { opened.isNotEmpty() }
                    if (stalledProbe) assertTrue("Startup selection exceeded its deadline", SystemClock.elapsedRealtime() - startedAt < 12_000)
                    assertEquals(listOf(second.id), opened.map { it.getStringExtra("server_id") })
                    assertEquals("Startup selection must reuse row probes", 2, requests.get())
                    waitFor("Launcher did not finish after hand-off") { scenario.state == Lifecycle.State.DESTROYED }
                }
            } finally { release.countDown(); instrumentation.removeMonitor(monitor); clean() }
        }
    }

    @Test fun launcherChecksEachWorkerOnceAndKeepsTheUnavailableList() = launcherUnavailableList(stalledProbe = false)
    @Test fun launcherFallsBackByDeadlineAndLateProbeOnlyUpdatesItsRow() = launcherUnavailableList(stalledProbe = true)

    private fun launcherUnavailableList(stalledProbe: Boolean) {
        clean()
        val first = Server("launcher-one", "First startup fixture", "https://startup-one-presence.example", "aim_fixture", "viewing-phone")
        val second = first.copy(id = "launcher-two", name = "Second startup fixture", baseUrl = "https://startup-two-presence.example")
        ServerStore.get(context).apply { add(first); add(second) }
        val requests = AtomicInteger()
        val pending = CountDownLatch(1)
        val release = CountDownLatch(1)
        PresenceFixture { request ->
            requests.incrementAndGet()
            if (stalledProbe && request.url.host == "startup-two-presence.example") {
                pending.countDown()
                release.await()
                PresenceFixture.response(request, 200, """{"status":"healthy","service":"Shuffle","version":"fixture","authenticated":false}""")
            } else PresenceFixture.response(request, 503, "{}")
        }.use {
            try {
                val startedAt = SystemClock.elapsedRealtime()
                launchWorkers(launcher = true).use { scenario ->
                    if (stalledProbe) assertTrue(pending.await(5, TimeUnit.SECONDS))
                    waitFor("Launcher fallback did not keep the checked worker rows", if (stalledProbe) 12_000 else 8_000) {
                        var fallback = false
                        scenario.onActivity { activity -> fallback = views(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>()
                            .any { it.text.startsWith("No authorized worker is reachable.") && it.isShown } }
                        fallback && description(scenario, first.name, "Status unavailable") &&
                            description(scenario, second.name, if (stalledProbe) "Unknown" else "Status unavailable")
                    }
                    if (stalledProbe) assertTrue("Startup fallback exceeded its deadline", SystemClock.elapsedRealtime() - startedAt < 12_000)
                    assertEquals("Startup selection must reuse row probes", 2, requests.get())
                    if (stalledProbe) {
                        // Selection must not cancel a still-visible row's probe or
                        // run again when it eventually completes.
                        release.countDown()
                        waitFor("The late probe did not update its row") { description(scenario, second.name, "Online") }
                        assertEquals(Lifecycle.State.RESUMED, scenario.state)
                        assertEquals("Late results must not start another sweep", 2, requests.get())
                    }
                }
            } finally { release.countDown(); clean() }
        }
    }

    @Test fun stoppedScreenCancelsPendingProbeAndChangedWorkerCannotInheritGreen() = stoppedProbe(launcher = false)
    @Test fun stoppedLauncherCancelsPendingProbeAndChangedWorkerCannotInheritGreen() = stoppedProbe(launcher = true)

    private fun stoppedProbe(launcher: Boolean) {
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
                launchWorkers(launcher).use { scenario ->
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
                launchWorkers().use { scenario ->
                    val activity = scenario.nativeActivity()
                    awaitNativeWindow(activity)
                    fun awaitStatus(expected: String) {
                        awaitNativeState(activity, "Direct worker observation: $expected") {
                            views(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>().any {
                                it.contentDescription?.toString()?.contains("${server.name}. $expected.") == true && it.isShown
                            }
                        }
                        assertTrue("Direct worker did not show $expected", description(scenario, server.name, expected))
                    }
                    awaitStatus("Online")
                    for (expected in listOf("Unknown", "Offline", "Online")) {
                        tapNative(androidx.test.espresso.Espresso.onView(
                            androidx.test.espresso.matcher.ViewMatchers.withContentDescription("Refresh workers")))
                        awaitStatus(expected)
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
                    awaitStatus("Online")
                    assertEquals(5, requests.get())
                }
            } finally { releaseResume.countDown(); clean() }
        }
    }
}
