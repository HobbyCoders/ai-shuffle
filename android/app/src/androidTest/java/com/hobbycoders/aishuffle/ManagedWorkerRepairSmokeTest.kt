package com.hobbycoders.aishuffle

import android.app.Activity
import android.app.ActivityManager
import android.app.Instrumentation
import android.content.Intent
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.*
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.WorkerRepairActivity
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okio.Buffer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(AndroidJUnit4::class)
class ManagedWorkerRepairSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val workerId = "11111111-1111-4111-8111-111111111111"
    private val phoneId = "22222222-2222-4222-8222-222222222222"
    private val scoped = "7bff8b2f-d077-57e0-a71c-7dada5be517f"
    private val newKey = "aim_" + "b".repeat(43)
    private val session get() = AccountSession("https://hub-repair.example", "fixture-token", Instant.now().plusSeconds(3600).toString(), phoneId, "fixture-account", "tester", true)
    private val server get() = Server("account:$workerId", "Repair fixture", "https://worker-repair.example", "aim_" + "a".repeat(43), phoneId,
        accountId = "fixture-account", hubUrl = "https://hub-repair.example", hubServerId = workerId)
    private val other get() = server.copy(id = "other-worker", name = "Other fixture", hubServerId = "33333333-3333-4333-8333-333333333333", baseUrl = "https://other-repair.example", apiKey = "aim_" + "c".repeat(43))
    private fun support() = JSONObject().put("server_id", workerId).put("ready", true).put("protected_ingress", true).put("account_repair", "scoped-v1")
    private fun credential() = JSONObject().put("api_key", newKey).put("device_id", scoped).put("expires_at", "2099-01-01T00:00:00Z")
        .put("scopes", JSONArray().put("web:session").put("controller"))
    private fun views(view: View): List<View> = listOf(view) + if (view is ViewGroup) (0 until view.childCount).flatMap { views(view.getChildAt(it)) } else emptyList()
    private fun waitFor(message: String, condition: () -> Boolean) = runBlocking {
        try { kotlinx.coroutines.withTimeout(8_000) { while (!condition()) kotlinx.coroutines.delay(50) } }
        catch (e: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, e) }
    }
    private fun seed() = instrumentation.runOnMainSync {
        DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false)
        AccountStore(context).save(session)
        ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server); add(other); selectedToolsServerId = null }
    }
    private fun cleanup() = instrumentation.runOnMainSync {
        DeviceToolsRuntime.stop(); AccountStore(context).clear(); ServerStore.get(context).apply { getAll().forEach { remove(it.id) } }
    }
    private fun fixture(handler: (Request) -> Response, block: () -> Unit) {
        seed()
        val field = ManagedWorkerRepair::class.java.getDeclaredField("client").apply { isAccessible = true }
        val original = field.get(null)
        field.set(null, OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.host == "worker-repair.example")
            check(request.header("Authorization") == null && request.header("Cookie") == null)
            handler(request)
        }.build())
        PresenceFixture().use {
            try { block() } finally { field.set(null, original); cleanup() }
        }
    }
    private fun form(): ActivityScenario<WorkerRepairActivity> = ActivityScenario.launch<WorkerRepairActivity>(
        Intent(context, WorkerRepairActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).also { scenario ->
        waitFor("Verified repair form did not open") {
            var shown = false
            scenario.onActivity { activity -> shown = activity.hasWindowFocus() && views(activity.findViewById(android.R.id.content)).filterIsInstance<TextView>().any { it.hint == "One-time repair code" } }
            shown
        }
    }
    private fun submit() {
        onView(withHint("One-time repair code")).perform(scrollTo(), replaceText("ABCDEF234567"), closeSoftKeyboard())
        onView(withText("Reconnect this worker")).perform(scrollTo(), click())
    }
    private fun assertUnchanged() {
        assertEquals(server, ServerStore.get(context).getById(server.id))
        assertEquals(other, ServerStore.get(context).getById(other.id))
    }

    @Test fun managed401OffersRepairOnlyWithVerifiedCapability() {
        for (advertised in listOf(true, false)) fixture({ request ->
            check(request.method == "GET")
            PresenceFixture.response(request, 200, support().apply { if (!advertised) remove("account_repair") }.toString())
        }) {
            val field = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
            val original = field.get(null)
            field.set(null, lazyOf(OkHttpClient.Builder().addInterceptor { chain ->
                check(chain.request().url.encodedPath == "/api/v1/mobile/web-session/reservations")
                PresenceFixture.response(chain.request(), 401, "{\"detail\":\"Revoked fixture credential\"}")
            }.build()))
            try {
                ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                    waitFor("401 repair capability decision did not appear") {
                        var done = false
                        scenario.onActivity { activity ->
                            val children = views(activity.findViewById(android.R.id.content))
                            val action = children.filterIsInstance<TextView>().first { it.text == "Reconnect this worker" }
                            done = if (advertised) action.isShown else !action.isShown && activity.findViewById<TextView>(R.id.screenStatus).text.contains(ManagedWorkerRepair.UPDATE_GUIDANCE)
                        }
                        done
                    }
                    assertUnchanged()
                }
            } finally { field.set(null, original) }
        }
    }

    @Test fun newKeyReplacesOnlySelectedWorkerAndRetainsPublicPhoneIdentity() {
        val posts = AtomicInteger()
        fixture({ request ->
            if (request.method == "GET") PresenceFixture.response(request, 200, support().toString()) else {
                posts.incrementAndGet()
                val buffer = Buffer(); request.body!!.writeTo(buffer); val json = JSONObject(buffer.readUtf8())
                check(json.getString("account_repair") == "scoped-v1")
                check(json.getJSONObject("device_info").getString("device_id") == scoped)
                PresenceFixture.response(request, 200, credential().toString())
            }
        }) {
            val launches = AtomicInteger()
            val monitor = object : Instrumentation.ActivityMonitor() {
                override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                    if (intent.component?.className != ChatActivity::class.java.name) return null
                    launches.incrementAndGet(); check(intent.getStringExtra(ChatActivity.EXTRA_SERVER_ID) == server.id)
                    return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
                }
            }
            instrumentation.addMonitor(monitor)
            try {
                form().use {
                    submit()
                    waitFor("Successful repair did not replace the selected key") { ServerStore.get(context).getById(server.id)?.apiKey == newKey && launches.get() == 1 }
                    assertEquals(server.copy(apiKey = newKey), ServerStore.get(context).getById(server.id))
                    assertEquals(phoneId, ServerStore.get(context).getById(server.id)!!.deviceId)
                    assertEquals(other, ServerStore.get(context).getById(other.id))
                    assertTrue(AccountStore(context).matches(session)); assertEquals(1, posts.get())
                }
            } finally { instrumentation.removeMonitor(monitor) }
        }
    }

    @Test fun missingReturnedIdentityDoesNotOverwriteSavedKey() {
        fixture({ request -> PresenceFixture.response(request, 200,
            if (request.method == "GET") support().toString() else credential().apply { remove("device_id") }.toString()) }) {
            form().use { scenario ->
                submit()
                waitFor("Invalid response was not rejected") {
                    var rejected = false
                    scenario.onActivity { rejected = it.findViewById<TextView>(R.id.screenStatus).text.contains("invalid repair response") }
                    rejected
                }
                assertUnchanged()
            }
        }
    }

    @Test fun stoppedRepairCannotSaveAfterAccountOrWorkerChanges() {
        for (changeAccount in listOf(true, false)) {
            val started = CountDownLatch(1); val release = CountDownLatch(1); val responseBuilt = CountDownLatch(1)
            fixture({ request ->
                if (request.method == "POST") { started.countDown(); check(release.await(8, TimeUnit.SECONDS)) }
                PresenceFixture.response(request, 200, if (request.method == "GET") support().toString() else credential().toString()).also {
                    if (request.method == "POST") responseBuilt.countDown()
                }
            }) {
                val launches = AtomicInteger()
                val monitor = object : Instrumentation.ActivityMonitor() {
                    override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                        if (intent.component?.className != ChatActivity::class.java.name) return null
                        launches.incrementAndGet()
                        return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
                    }
                }
                instrumentation.addMonitor(monitor)
                try {
                    form().use { scenario ->
                        lateinit var task: ActivityManager.AppTask
                        scenario.onActivity { activity ->
                            task = activity.getSystemService(ActivityManager::class.java).appTasks.single { it.taskInfo.taskId == activity.taskId }
                        }
                        submit(); assertTrue(started.await(5, TimeUnit.SECONDS))
                        scenario.moveToState(Lifecycle.State.CREATED)
                        assertEquals(Lifecycle.State.CREATED, scenario.state)
                        val savedSession = requireNotNull(AccountStore(context).session())
                        val expectedSession = if (changeAccount) AccountSession(savedSession.hub, "changed-token", savedSession.expiresAt,
                            savedSession.deviceId, savedSession.accountId, savedSession.username, savedSession.profileComplete) else savedSession
                        val expectedServer = if (changeAccount) server else server.copy(baseUrl = "https://changed-repair.example")
                        if (changeAccount) AccountStore(context).save(expectedSession)
                        else ServerStore.get(context).update(expectedServer)
                        release.countDown()
                        assertTrue("Held repair response was not released", responseBuilt.await(5, TimeUnit.SECONDS))
                        // Resumption must close the stale form, so do not require a stable RESUMED state.
                        task.moveToFront()
                        waitFor("Repair form did not close after its identity changed") { scenario.state == Lifecycle.State.DESTROYED }
                        instrumentation.waitForIdleSync()
                        assertEquals(expectedServer, ServerStore.get(context).getById(server.id))
                        assertEquals(other, ServerStore.get(context).getById(other.id))
                        assertEquals(expectedSession.json().toString(), requireNotNull(AccountStore(context).session()).json().toString())
                        assertEquals(0, launches.get())
                    }
                } finally { release.countDown(); instrumentation.removeMonitor(monitor) }
            }
        }
    }

    @Test fun resumedRepairRejectsLateResponseForChangedAccountOrWorker() {
        for (changeAccount in listOf(true, false)) {
            val started = CountDownLatch(1); val release = CountDownLatch(1)
            fixture({ request ->
                if (request.method == "POST") { started.countDown(); check(release.await(8, TimeUnit.SECONDS)) }
                PresenceFixture.response(request, 200, if (request.method == "GET") support().toString() else credential().toString())
            }) {
                val launches = AtomicInteger()
                val monitor = object : Instrumentation.ActivityMonitor() {
                    override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                        if (intent.component?.className != ChatActivity::class.java.name) return null
                        launches.incrementAndGet()
                        return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
                    }
                }
                instrumentation.addMonitor(monitor)
                try {
                    form().use { scenario ->
                        submit(); assertTrue(started.await(5, TimeUnit.SECONDS))
                        // Keep the form resumed: cancellation cannot mask a missing ownership check.
                        scenario.onActivity { activity ->
                            assertEquals(Lifecycle.State.RESUMED, activity.lifecycle.currentState)
                            if (changeAccount) AccountStore(context).save(AccountSession(session.hub, "changed-token", session.expiresAt, phoneId, session.accountId, "tester", true))
                            else ServerStore.get(context).update(server.copy(baseUrl = "https://changed-repair.example"))
                        }
                        release.countDown()
                        waitFor("Late response was not rejected after ownership changed") {
                            var rejected = false
                            scenario.onActivity { rejected = it.findViewById<TextView>(R.id.screenStatus).text.contains("account or worker changed") }
                            rejected
                        }
                        assertEquals(server.apiKey, ServerStore.get(context).getById(server.id)!!.apiKey)
                        assertEquals(other, ServerStore.get(context).getById(other.id))
                        assertEquals(0, launches.get())
                    }
                } finally { release.countDown(); instrumentation.removeMonitor(monitor) }
            }
        }
    }
}
