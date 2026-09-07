package com.hobbycoders.aishuffle

import android.content.Intent
import android.webkit.CookieManager
import android.webkit.WebView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountSession
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.ContainedDocument
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/** Real ChatActivity/CookieManager/WebView; all worker/account traffic is synthetic. */
@RunWith(AndroidJUnit4::class)
class WebSessionReservationSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val server = Server.create("Reservation fixture", "https://reservation-fixture.example", "aim_fixture_key")
    private val other = Server.create("Other fixture", "https://other-reservation.example", "aim_other_fixture_key")
    private val account = AccountSession("https://reservation-hub.example", "fixture-account-token", Instant.now().plusSeconds(3600).toString(),
        "fixture-phone", "fixture-account", "fixture-user", true)
    private val reservationA = "wsm_" + "a".repeat(43)
    private val reservationB = "wsm_" + "b".repeat(43)
    private val tokenA = "fixture-session-A"
    private val tokenB = "fixture-session-B"
    private val reservePath = "/api/v1/mobile/web-session/reservations"
    private val commitPath = "/api/v1/mobile/web-session"
    private val currentToken = AtomicReference<String?>(null)
    private val acceptedDocuments = AtomicInteger()
    private val rejectedDocuments = AtomicInteger()
    private val sessionField = ChatActivity::class.java.getDeclaredField("sessionJob").apply { isAccessible = true }

    private fun waitFor(message: String, condition: () -> Boolean) = runBlocking {
        try { kotlinx.coroutines.withTimeout(12_000) { while (!condition()) kotlinx.coroutines.delay(50) } }
        catch (error: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, error) }
    }
    private fun web(activity: ChatActivity): WebView {
        val shell = activity.findViewById<TextView>(R.id.screenStatus).parent as LinearLayout
        return (0 until shell.childCount).map { shell.getChildAt(it) }.filterIsInstance<WebView>().single()
    }
    private fun cookie() = CookieManager.getInstance().getCookie(server.baseUrl).orEmpty()
    private fun response(request: Request, status: Int, body: String, mime: String = "application/json") = Response.Builder().request(request)
        .protocol(Protocol.HTTP_1_1).code(status).message("Synthetic fixture").header("Cache-Control", "no-store").header("Content-Type", mime)
        .body(body.toResponseBody(mime.toMediaType())).build()
    private fun reserved(request: Request, value: String) = response(request, 200, JSONObject().put("reservation", value)
        .put("expires_at", "2099-01-01T00:00:00Z").put("expires_in", 120).toString())
    private fun committed(request: Request, value: String): Response {
        currentToken.set(value)
        return response(request, 200, JSONObject().put("session_token", value).toString())
    }
    private fun launched() = ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id))
    private fun unchanged() {
        assertEquals(server, ServerStore.get(context).getById(server.id))
        assertEquals(other, ServerStore.get(context).getById(other.id))
        assertEquals(account.json().toString(), requireNotNull(AccountStore(context).session()).json().toString())
    }
    private fun fixture(handler: (Request) -> Response, block: () -> Unit) {
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val docField = ContainedDocument::class.java.getDeclaredField("http").apply { isAccessible = true }
        val oldApi = apiField.get(null); val oldDoc = docField.get(null)
        currentToken.set(null); acceptedDocuments.set(0); rejectedDocuments.set(0)
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false)
            AccountStore(context).save(account)
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server); add(other); selectedToolsServerId = null }
            CookieManager.getInstance().setCookie(server.baseUrl, "session=; Max-Age=0; Path=/; Secure; HttpOnly")
        }
        apiField.set(null, lazyOf(OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.host == "reservation-fixture.example" && request.method == "POST")
            check(request.header("Authorization") == "Bearer ${server.apiKey}" && request.header("Cookie") == null)
            val bytes = Buffer(); request.body!!.writeTo(bytes)
            val body = JSONObject(bytes.readUtf8())
            if (request.url.encodedPath == reservePath) check(body.length() == 0)
            else { check(request.url.encodedPath == commitPath); check(body.opt("purpose") == "web") }
            handler(request)
        }.build()))
        docField.set(null, OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.toString() == server.baseUrl + "/" && request.header("Authorization") == null)
            val authorized = request.header("Cookie").orEmpty().split(';').any { it.trim() == "session=${currentToken.get()}" }
            if (authorized) acceptedDocuments.incrementAndGet() else rejectedDocuments.incrementAndGet()
            response(request, if (authorized) 200 else 401,
                "<html><head><link rel='icon' href='data:,'></head><body>Benign reserved chat fixture</body></html>", "text/html")
        }.build())
        try { block() } finally {
            apiField.set(null, oldApi); docField.set(null, oldDoc)
            instrumentation.runOnMainSync {
                DeviceToolsRuntime.stop(); AccountStore(context).clear()
                ServerStore.get(context).apply { remove(server.id); remove(other.id) }
                CookieManager.getInstance().setCookie(server.baseUrl, "session=; Max-Age=0; Path=/; Secure; HttpOnly")
            }
        }
    }

    @Test fun lateStoppedCommitIsSupersededAndResumedCookieRemainsValid() = overlap(blockReserve = false)
    @Test fun lateCanceledReservationNeverCommitsOrInvalidatesResumedCookie() = overlap(blockReserve = true)

    private fun overlap(blockReserve: Boolean) {
        val firstStarted = CountDownLatch(1); val releaseFirst = CountDownLatch(1)
        val latestReservation = AtomicReference<String?>(null)
        val reservations = AtomicInteger(); val commits = CopyOnWriteArrayList<String>(); val rejectedCommits = AtomicInteger()
        fun hold() { firstStarted.countDown(); check(releaseFirst.await(30, TimeUnit.SECONDS)) }
        fixture({ request ->
            if (request.url.encodedPath == reservePath) {
                val number = reservations.incrementAndGet(); check(number in 1..2)
                val value = if (number == 1) reservationA else reservationB
                if (number == 1 && blockReserve) hold()
                // Reserve intentionally never touches the current WEB session token.
                latestReservation.set(value)
                reserved(request, value)
            } else {
                val value = request.header(ApiClient.WEB_RESERVATION_HEADER)
                if (value == reservationA && !blockReserve) hold()
                if (value != latestReservation.get()) {
                    rejectedCommits.incrementAndGet()
                    response(request, 409, "{\"detail\":\"Superseded reservation\"}")
                } else {
                    check(value == reservationB || !blockReserve)
                    commits.add(if (value == reservationA) "A" else "B")
                    committed(request, if (value == reservationA) tokenA else tokenB)
                }
            }
        }) {
            try {
                launched().use { scenario ->
                    assertTrue("Request A never reached the held phase", firstStarted.await(5, TimeUnit.SECONDS))
                    lateinit var firstJob: Job
                    lateinit var firstActivity: ChatActivity
                    scenario.onActivity { firstActivity = it; firstJob = sessionField.get(it) as Job }
                    scenario.moveToState(Lifecycle.State.CREATED)
                    assertTrue(firstJob.isCancelled)
                    scenario.moveToState(Lifecycle.State.RESUMED)
                    waitFor("New request B did not install a valid cookie") { cookie().contains("session=$tokenB") && acceptedDocuments.get() > 0 }
                    scenario.onActivity { assertSame(firstActivity, it) }
                    assertEquals(listOf("B"), commits.toList())
                    releaseFirst.countDown()
                    waitFor("Canceled A did not finish its HTTP response") { firstJob.isCompleted }
                    assertEquals(listOf("B"), commits.toList())
                    assertEquals(if (blockReserve) 0 else 1, rejectedCommits.get())
                    assertEquals(tokenB, currentToken.get())
                    assertTrue(cookie().contains("session=$tokenB"))
                    val before = acceptedDocuments.get()
                    scenario.onActivity { web(it).reload() }
                    waitFor("Reload did not use the still-valid B cookie") { acceptedDocuments.get() > before }
                    assertEquals(0, rejectedDocuments.get())
                    assertEquals(2, reservations.get())
                    unchanged()
                    println("RESERVED_MINT_PROOF: held=${if (blockReserve) "reserve" else "commit"}; commits=B; cookie=B; server_session=B; late_commit_status=${if (blockReserve) "not_sent" else "409"}; reload=200; credentials_unchanged=true")
                }
            } finally { releaseFirst.countDown() }
        }
    }

    @Test fun onlyExplicitLegacyAbsenceFallsBackAndTransientFailuresKeepExistingCookie() {
        for ((code, mime, body) in listOf(Triple(404, "application/json", "{\"detail\":\"Not Found\"}"),
            Triple(503, "application/json", "{\"detail\":\"Not Found\"}"), Triple(409, "application/json", "{}"),
            Triple(404, "text/html", "<html>Not Found</html>"), Triple(200, "application/json", "{}"))) {
            val commits = AtomicInteger()
            fixture({ request ->
                if (request.url.encodedPath == reservePath) response(request, code, body, mime)
                else {
                    assertNull(request.header(ApiClient.WEB_RESERVATION_HEADER))
                    commits.incrementAndGet(); committed(request, tokenB)
                }
            }) {
                instrumentation.runOnMainSync { CookieManager.getInstance().setCookie(server.baseUrl, "session=old-fixture-cookie; Path=/; Secure; HttpOnly") }
                launched().use { scenario ->
                    val legacy = code == 404 && mime == "application/json"
                    waitFor("Bootstrap did not complete") {
                        var done = false
                        scenario.onActivity { done = (sessionField.get(it) as? Job)?.isCompleted == true }
                        done
                    }
                    assertEquals(if (legacy) 1 else 0, commits.get())
                    if (legacy) waitFor("Legacy chat did not open") { acceptedDocuments.get() > 0 }
                    else { assertTrue(cookie().contains("session=old-fixture-cookie")); assertEquals(0, acceptedDocuments.get()) }
                    unchanged()
                }
            }
        }
    }

    @Test fun supersededCommitRetriesOnceAndNeverDropsTheReservationHeader() {
        for (conflicts in listOf(1, 2)) {
            val reservations = AtomicInteger(); val commits = AtomicInteger()
            fixture({ request ->
                if (request.url.encodedPath == reservePath) reserved(request, if (reservations.incrementAndGet() == 1) reservationA else reservationB)
                else {
                    val count = commits.incrementAndGet()
                    assertEquals(if (count == 1) reservationA else reservationB, request.header(ApiClient.WEB_RESERVATION_HEADER))
                    if (count <= conflicts) response(request, 409, "{\"detail\":\"Superseded reservation\"}") else committed(request, tokenB)
                }
            }) {
                launched().use { scenario ->
                    waitFor("Bounded reservation retry never finished") {
                        var done = false; scenario.onActivity { done = (sessionField.get(it) as? Job)?.isCompleted == true }; done
                    }
                    assertEquals(2, reservations.get()); assertEquals(2, commits.get())
                    if (conflicts == 1) waitFor("Successful retry did not open chat") { acceptedDocuments.get() > 0 }
                    else {
                        assertEquals(0, acceptedDocuments.get())
                        scenario.onActivity { assertTrue(it.findViewById<TextView>(R.id.screenStatus).text.contains("Another connection replaced")) }
                    }
                    unchanged()
                }
            }
        }
    }

    @Test fun changedAccountOrWorkerWhileResumedCannotCommitLateReservation() {
        for (changeAccount in listOf(true, false)) {
            val started = CountDownLatch(1); val release = CountDownLatch(1); val commits = AtomicInteger()
            fixture({ request ->
                if (request.url.encodedPath == reservePath) {
                    started.countDown(); check(release.await(30, TimeUnit.SECONDS)); reserved(request, reservationA)
                } else { commits.incrementAndGet(); committed(request, tokenA) }
            }) {
                try {
                    launched().use { scenario ->
                        assertTrue(started.await(5, TimeUnit.SECONDS))
                        lateinit var job: Job
                        scenario.onActivity { activity ->
                            assertEquals(Lifecycle.State.RESUMED, activity.lifecycle.currentState)
                            job = sessionField.get(activity) as Job
                            if (changeAccount) AccountStore(context).save(AccountSession(account.hub, "replacement-fixture-token", account.expiresAt,
                                account.deviceId, account.accountId, account.username, account.profileComplete))
                            else ServerStore.get(context).update(server.copy(apiKey = "aim_replacement_fixture_key"))
                        }
                        release.countDown()
                        waitFor("Stale reservation did not complete") { job.isCompleted }
                        assertEquals(0, commits.get()); assertEquals(0, acceptedDocuments.get())
                        assertFalse(cookie().contains("session=$tokenA"))
                        assertEquals(other, ServerStore.get(context).getById(other.id))
                        assertEquals(if (changeAccount) server.apiKey else "aim_replacement_fixture_key", ServerStore.get(context).getById(server.id)?.apiKey)
                        assertEquals(if (changeAccount) "replacement-fixture-token" else account.token, AccountStore(context).session()?.token)
                    }
                } finally { release.countDown() }
            }
        }
    }
}
