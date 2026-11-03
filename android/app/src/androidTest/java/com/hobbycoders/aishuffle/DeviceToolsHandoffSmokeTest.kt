package com.hobbycoders.aishuffle

import android.content.Intent
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.click
import androidx.test.espresso.matcher.ViewMatchers.withText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.*
import com.hobbycoders.aishuffle.ui.SettingsActivity
import kotlinx.coroutines.Job
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.IOException
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/** Real startPolling/executor/consent/lifecycle/journal path; only the HTTP peer is simulated. */
@RunWith(AndroidJUnit4::class)
class DeviceToolsHandoffSmokeTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private fun runtimeField(name: String) = DeviceToolsRuntime::class.java.getDeclaredField(name).apply { isAccessible = true }.get(null)
    private fun awaitMain(message: String, condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 10_000
        do {
            var satisfied = false
            instrumentation.runOnMainSync { satisfied = condition() }
            if (satisfied) return
            SystemClock.sleep(25)
        } while (SystemClock.elapsedRealtime() < deadline)
        fail("$message (runtime availability: ${DeviceToolsRuntime.availability.value})")
    }
    @Test fun successfulHandoffPostsBeforeRealPollingShutdown() = exercise(false, false)
    @Test fun offlineHandoffKeepsResultThroughRealPollingShutdown() = exercise(true, false)
    @Test fun ordinaryPauseDuringApprovalReportsNoActionWithoutWaitingForExpiry() = exercise(false, true)

    private fun exercise(offline: Boolean, pauseDuringApproval: Boolean) {
        val target = Server("handoff-smoke-${System.nanoTime()}", "Handoff fixture", "https://handoff.invalid", "fixture-key", "fixture-device")
        val scope = "${target.id}|${target.baseUrl}|${target.deviceId}"
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop()
            ToolsPreferences(context).setEnabled(false)
            val store = ServerStore.get(context)
            store.getAll().forEach { store.remove(it.id) }
            // setEnabled calls preferencesChanged: enable BEFORE selecting the target so attach
            // cannot retire a phantom prior target and cancel its in-flight key renewal.
            ToolsPreferences(context).setEnabled(true)
            store.add(target)
            store.selectedToolsServerId = target.id
        }
        // A unique id permits assertions across repeated test runs without clearing secure storage.
        val id = "handoff-${System.nanoTime()}"
        val journal = CommandJournal(context)
        val pending = AtomicBoolean(false)
        val enteredPost = CountDownLatch(1)
        val releasePost = CountDownLatch(1)
        val withdrawn = CountDownLatch(1)
        val outputs = CopyOnWriteArrayList<JSONObject>()
        val launches = AtomicInteger()
        val monitor = object : android.app.Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): android.app.Instrumentation.ActivityResult? {
                if (intent.component?.className == ControlTargetActivity::class.java.name) launches.incrementAndGet()
                return null // Do NOT intercept: Android must really pause the original activity.
            }
        }
        // Tools first renew through ApiClient's separate transport. Model that HTTP peer too;
        // do not seed the renewal cache or bypass its validation/persistence producing path.
        val renewals = AtomicInteger()
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val previousApi = apiField.get(null)
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            if (request.url.encodedPath == "/api/v1/mobile/key/renew") {
                renewals.incrementAndGet()
                PresenceFixture.renewDeviceKey(request, target)
            } else PresenceFixture.response(request, 404, "{}")
        }.build()
        val clientField = DeviceToolsApi::class.java.getDeclaredField("client").apply { isAccessible = true }
        val original = clientField.get(null) as OkHttpClient
        val fixture = original.newBuilder().addInterceptor { chain ->
            val request = chain.request()
            val body = request.body?.let { Buffer().also(it::writeTo).readUtf8() }?.let(::JSONObject)
            val result = when {
                request.url.encodedPath.endsWith("/capabilities") -> {
                    if (body?.optBoolean("enabled") == false) withdrawn.countDown()
                    JSONObject().put("ok", true)
                }
                request.url.encodedPath.endsWith("/pending") -> {
                    val commands = JSONArray()
                    if (pending.compareAndSet(false, true)) {
                        for (commandId in listOf(id, "$id-next")) commands.put(JSONObject()
                            .put("id", commandId).put("name", "open_app")
                            .put("arguments", JSONObject().put("package_name", instrumentation.context.packageName))
                            .put("expires_at", System.currentTimeMillis() + 60_000))
                    }
                    JSONObject().put("commands", commands)
                }
                request.url.encodedPath.endsWith("/result") -> {
                    outputs += requireNotNull(body).getJSONObject("result")
                    enteredPost.countDown()
                    check(releasePost.await(10, TimeUnit.SECONDS)) { "Test failed to release result POST" }
                    if (offline) throw IOException("Simulated offline delivery")
                    JSONObject().put("ok", true)
                }
                else -> error("Unexpected device-tools request")
            }
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body(result.toString().toResponseBody("application/json".toMediaType())).build()
        }.build()
        instrumentation.addMonitor(monitor)
        clientField.set(null, fixture)
        apiField.set(null, lazyOf(api))
        try {
            ActivityScenario.launch<SettingsActivity>(Intent(context, SettingsActivity::class.java)).use { scenario ->
                instrumentation.waitForIdleSync()
                var host: SettingsActivity? = null
                scenario.onActivity { host = it }
                awaitMain("Settings never gained focus") { host?.hasWindowFocus() == true }
                scenario.onActivity { DeviceToolsRuntime.attach(it, target) }
                awaitMain("Real executor never requested approval") { ToolApprovals.pending.value != null }
                assertEquals("The actual renewal producer must validate the HTTP receipt first", 1, renewals.get())
                if (pauseDuringApproval) instrumentation.uiAutomation.executeShellCommand("input keyevent KEYCODE_HOME").close()
                else onView(withText("Allow once")).perform(click())
                assertTrue("Result did not reach the HTTP peer promptly", enteredPost.await(10, TimeUnit.SECONDS))
                if (!pauseDuringApproval) awaitMain("External app did not actually open") {
                    val root = instrumentation.uiAutomation.rootInActiveWindow
                    try { root?.packageName == instrumentation.context.packageName } finally { root?.recycle() }
                }
                awaitMain("Real pause did not preserve delivery") { DeviceToolsRuntime.availability.value == "handoff_result_pending" }
                instrumentation.runOnMainSync {
                    assertTrue((runtimeField("polling") as Job).isActive)
                    assertEquals(target, runtimeField("server"))
                }
                assertEquals(if (pauseDuringApproval) "foreground_required" else "launched_confirmation", outputs.first().getString("status"))
                assertTrue(journal.get(CommandJournal.key(scope, id))!!.has("result"))
                releasePost.countDown()
                assertTrue("Real startPolling finally did not withdraw capabilities", withdrawn.await(10, TimeUnit.SECONDS))
                awaitMain("Real startPolling finally did not stop its own loop") {
                    runtimeField("polling") == null && runtimeField("server") == null
                }
                assertEquals(if (pauseDuringApproval) 0 else 1, launches.get())
                assertEquals(offline, journal.get(CommandJournal.key(scope, id))!!.has("result"))
                if (!offline) {
                    assertEquals(2, outputs.size)
                    assertEquals("foreground_required", outputs.last().getString("status"))
                }
                assertFalse(journal.claim(CommandJournal.key(scope, id), id, scope, System.currentTimeMillis() + 20_000, System.currentTimeMillis()))
            }
        } finally {
            releasePost.countDown()
            instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false) }
            instrumentation.removeMonitor(monitor)
            clientField.set(null, original)
            apiField.set(null, previousApi)
        }
    }
}
