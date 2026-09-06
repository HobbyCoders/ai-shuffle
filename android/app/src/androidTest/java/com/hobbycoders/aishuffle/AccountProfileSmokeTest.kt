package com.hobbycoders.aishuffle

import android.app.Activity
import android.app.Instrumentation
import android.content.Intent
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.*
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.*
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.AccountActivity
import com.hobbycoders.aishuffle.ui.AddServerActivity
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class AccountProfileSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val store get() = AccountStore(context)
    @Before fun clearLocalState() {
        instrumentation.runOnMainSync {
            com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.stop()
            store.clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) } }
        }
    }
    private fun awaitText(text: String) = kotlinx.coroutines.runBlocking {
        kotlinx.coroutines.withTimeout(8_000) {
            while (runCatching { onView(withText(text)).check(matches(isDisplayed())) }.isFailure) kotlinx.coroutines.delay(50)
        }
    }
    private fun profile(username: String, complete: Boolean) = JSONObject().put("device_id", store.installId)
        .put("account", JSONObject().put("id", "test-account").put("username", username).put("profile_complete", complete))
    private fun withApi(handler: (Request) -> Pair<Int, JSONObject>, block: () -> Unit) {
        val field = HubClient::class.java.getDeclaredField("client").apply { isAccessible = true }
        val original = field.get(null) as OkHttpClient
        val client = original.newBuilder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.toString().startsWith("${AccountStore.DEFAULT_HUB}/v1/"))
            val (code, body) = handler(request)
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(code).message("Fixture")
                .body(body.toString().toResponseBody("application/json".toMediaType())).build()
        }.build()
        val monitor = object : Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                if (intent.component?.className != "net.openid.appauth.AuthorizationManagementActivity") return null
                val request = AuthorizationRequest.jsonDeserialize(requireNotNull(intent.getStringExtra("authRequest")))
                return Instrumentation.ActivityResult(Activity.RESULT_OK,
                    AuthorizationResponse.Builder(request).setState(request.state).setAuthorizationCode("test-only-code").build().toIntent())
            }
        }
        field.set(null, client)
        instrumentation.addMonitor(monitor)
        try { block() } finally { instrumentation.removeMonitor(monitor); field.set(null, original) }
    }
    private fun startResponse() = JSONObject().put("issuer", "https://identity.example")
        .put("authorization_endpoint", "https://identity.example/oauth/authorize").put("token_endpoint", "https://identity.example/oauth/token")
        .put("redirect_uri", AccountOAuth.REDIRECT).put("client_id", "test-client").put("nonce", "test-nonce").put("request_id", "test-request")
    private fun sessionResponse(complete: Boolean?): JSONObject = profile("generated-name", complete ?: true).apply {
        if (complete == null) getJSONObject("account").remove("profile_complete")
        put("access_token", "test-account-session").put("expires_at", Instant.now().plusSeconds(3600).toString())
    }
    private fun body(request: Request): JSONObject {
        val buffer = okio.Buffer()
        request.body!!.writeTo(buffer)
        return JSONObject(buffer.readUtf8())
    }

    @Test fun newOAuthAccountChoosesUsernameBeforeWorkersAndKeepsDraftOnRotation() {
        val patches = AtomicInteger()
        val syncs = AtomicInteger()
        val firstSync = CountDownLatch(1)
        val releaseFirstSync = CountDownLatch(1)
        withApi({ request -> 200 to when (request.url.encodedPath) {
            "/v1/oauth/start" -> startResponse()
            "/v1/oauth/exchange" -> sessionResponse(false)
            "/v1/me" -> {
                assertEquals("PATCH", request.method)
                patches.incrementAndGet()
                profile(body(request).getString("username"), true)
            }
            "/v1/servers" -> {
                syncs.incrementAndGet()
                firstSync.countDown()
                if (!releaseFirstSync.await(15, TimeUnit.SECONDS)) throw java.io.IOException("Fixture sync gate was not released")
                JSONObject().put("servers", JSONArray())
            }
            else -> error("Unexpected fixture route")
        } }) {
            ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", "create")).use { scenario ->
                awaitText("Choose your username")
                onView(withHint("Username")).check(matches(withText(""))).perform(replaceText("my.name"), closeSoftKeyboard())
                val before = store.session()!!
                scenario.recreate()
                onView(withHint("Username")).check(matches(withText("my.name")))
                onView(withText("Save and continue")).perform(scrollTo(), click())
                assertTrue("Worker sync did not begin after saving", firstSync.await(5, TimeUnit.SECONDS))
                // Recreate after the profile is saved but before worker sync finishes.
                scenario.recreate()
                releaseFirstSync.countDown()
                awaitText("No workers yet")
                assertEquals(1, patches.get())
                assertEquals("my.name", store.session()!!.username)
                assertEquals(before.token, store.session()!!.token)
                assertEquals(before.deviceId, store.session()!!.deviceId)
                assertEquals(before.accountId, store.session()!!.accountId)
                assertEquals(true, store.session()!!.profileComplete)
            }
        }
    }
    @Test fun completedAndOlderOAuthResponsesContinueWithoutAnotherUsernamePrompt() {
        for (complete in listOf(true, null)) {
            val profileGets = AtomicInteger()
            withApi({ request -> 200 to when (request.url.encodedPath) {
                "/v1/oauth/start" -> startResponse()
                "/v1/oauth/exchange" -> sessionResponse(complete)
                "/v1/me" -> { assertEquals("GET", request.method); profileGets.incrementAndGet(); profile("chosen-name", true) }
                "/v1/servers" -> JSONObject().put("servers", JSONArray())
                else -> error("Unexpected fixture route")
            } }) {
                ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", "login")).use {
                    awaitText("No workers yet")
                    onView(withText("Save and continue")).check(doesNotExist())
                    assertEquals(if (complete == null) 1 else 0, profileGets.get())
                    assertEquals(true, store.session()!!.profileComplete)
                }
            }
        }
    }
    @Test fun settingsRenameHandlesDuplicateAndRemoteFailureWithoutLosingWorkerAccess() {
        val session = AccountSession(AccountStore.DEFAULT_HUB, "preserved-session", Instant.now().plusSeconds(3600).toString(),
            "preserved-device", "test-account", "generated-name")
        store.save(session)
        val worker = Server.create("My worker", "https://worker.example", "aim_preserved").copy(
            accountId = session.accountId, deviceId = session.deviceId, hubUrl = session.hub, hubServerId = "worker-one")
        ServerStore.get(context).add(worker)
        val patches = AtomicInteger()
        withApi({ request ->
            check(request.url.encodedPath == "/v1/me")
            if (request.method == "GET") 200 to profile("generated-name", false)
            else when (patches.incrementAndGet()) {
                1 -> 409 to JSONObject()
                2 -> 503 to JSONObject()
                else -> 200 to profile(body(request).getString("username"), true)
            }
        }) {
            ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", "manage")).use {
                onView(withText("Edit username")).perform(scrollTo(), click())
                awaitText("Save username")
                onView(withHint("Username")).check(matches(withText(""))).perform(replaceText("my.name"), closeSoftKeyboard())
                onView(withText("Save username")).perform(scrollTo(), click())
                awaitText("That username is already taken. Choose another.")
                assertEquals(session.username, store.session()!!.username)
                onView(withText("Save username")).perform(scrollTo(), click())
                awaitText("Could not save your username. Check your connection and try again.")
                assertEquals(session.token, store.session()!!.token)
                onView(withText("Save username")).perform(scrollTo(), click())
                awaitText("Username updated.")
                assertEquals("my.name", store.session()!!.username)
                assertEquals(session.token, store.session()!!.token)
                assertEquals(session.deviceId, store.session()!!.deviceId)
                assertEquals(session.accountId, store.session()!!.accountId)
                assertEquals(worker, ServerStore.get(context).getAll().single())
            }
        }
    }
    @Test fun approvedWorkerCanResumeAfterRecreationWithoutReusingItsCode() {
        val session = AccountSession(AccountStore.DEFAULT_HUB, "preserved-session", Instant.now().plusSeconds(3600).toString(),
            "preserved-device", "test-account", "chosen-name", true)
        store.save(session)
        val lookups = AtomicInteger()
        val approvals = AtomicInteger()
        val workerLists = AtomicInteger()
        val firstPoll = CountDownLatch(1)
        val resumedPoll = CountDownLatch(1)
        withApi({ request -> 200 to when (request.url.encodedPath) {
            "/v1/worker-links/lookup" -> { lookups.incrementAndGet(); JSONObject().put("name", "My worker") }
            "/v1/worker-links/approve" -> { approvals.incrementAndGet(); JSONObject().put("id", "worker-one") }
            "/v1/servers" -> {
                if (workerLists.incrementAndGet() == 1) firstPoll.countDown() else resumedPoll.countDown()
                JSONObject().put("servers", JSONArray())
            }
            else -> error("Unexpected fixture route")
        } }) {
            ActivityScenario.launch(AddServerActivity::class.java).use { scenario ->
                onView(withHint("Pairing code")).perform(replaceText("ABCDEF234567"), closeSoftKeyboard())
                onView(withText("Continue")).perform(scrollTo(), click())
                awaitText("Add My worker?")
                onView(withId(android.R.id.button1)).perform(click())
                assertTrue(firstPoll.await(5, TimeUnit.SECONDS))
                scenario.recreate()
                awaitText("Worker approved. Tap Continue to reconnect, or return to Workers and refresh. No new code is needed.")
                onView(withHint("Pairing code")).check(matches(withText("")))
                onView(withText("Continue")).perform(scrollTo(), click())
                assertTrue(resumedPoll.await(5, TimeUnit.SECONDS))
                assertEquals(1, lookups.get())
                assertEquals(1, approvals.get())
                assertEquals(session.token, store.session()!!.token)
            }
        }
    }
}
