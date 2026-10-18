package com.hobbycoders.aishuffle

import android.content.Intent
import android.content.pm.ActivityInfo
import androidx.appcompat.app.AppCompatDelegate
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.espresso.Espresso.onView
import androidx.test.espresso.action.ViewActions.*
import androidx.test.espresso.assertion.ViewAssertions.doesNotExist
import androidx.test.espresso.assertion.ViewAssertions.matches
import androidx.test.espresso.matcher.ViewMatchers.*
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.account.AccountOAuth
import com.hobbycoders.aishuffle.account.HubClient
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.tools.*
import com.hobbycoders.aishuffle.ui.*
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import org.junit.*
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CopyOnWriteArrayList
import net.openid.appauth.AuthorizationRequest
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.Protocol
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONObject

/** Real native screens and actual consent collector. No test authentication route in production. */
@RunWith(AndroidJUnit4::class)
class NativeSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private var presenceFixture: PresenceFixture? = null
    @Before fun clearLocalState() {
        presenceFixture = PresenceFixture()
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolApprovals.cancelAll(); ToolsPreferences(context).setEnabled(false)
            AccountStore(context).apply { clear(); hub = AccountStore.DEFAULT_HUB }
            val store = ServerStore.get(context); store.getAll().forEach { store.remove(it.id) }; store.selectedToolsServerId = null
            AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)
        }
    }
    @After fun stopRuntime() { instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO) }; presenceFixture?.close() }

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

    private fun awaitConsent(): Long {
        var observedDeadline: Long? = null
        awaitCondition("A focused Allow once button was never presented") {
            var shown = false
            instrumentation.runOnMainSync {
                shown = android.view.inspector.WindowInspector.getGlobalWindowViews().any { root ->
                    root.hasWindowFocus() && root.findViewById<android.widget.Button>(android.R.id.button1)?.let {
                        it.isShown && it.isEnabled && it.text.toString() == "Allow once"
                    } == true
                }
                if (shown) observedDeadline = ToolApprovals.pending.value?.expiresAt
            }
            shown && observedDeadline != null
        }
        return requireNotNull(observedDeadline)
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
    private fun account(mode: String): ActivityScenario<AccountActivity> {
        instrumentation.runOnMainSync { AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO) }
        return ActivityScenario.launch<AccountActivity>(Intent(context, AccountActivity::class.java).putExtra("mode", mode)).also {
        awaitFocused(it)
        instrumentation.uiAutomation.waitForIdle(500, 5_000)
        }
    }

    @Test fun settingsOmitsApkDownloadAndKeepsAccountPhoneToolsAndReleaseNotes() {
        ActivityScenario.launch(SettingsActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            onView(withText("ANDROID APK")).check(doesNotExist())
            onView(withText("Download latest APK")).check(doesNotExist())
            onView(withText("Sign in")).check(matches(isDisplayed()))
            onView(withText("Phone tools")).perform(scrollTo()).check(matches(isDisplayed()))
            onView(withText("Version and release notes")).perform(scrollTo()).check(matches(isDisplayed()))
        }
    }

    @Test fun freshLaunchOffersAccountOrDirectWorkerConnection() {
        ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
            onView(withText("Sign in")).check(matches(isDisplayed()))
            onView(withText("Create account")).check(matches(isDisplayed()))
            onView(withText("Use another device")).perform(scrollTo()).check(matches(isDisplayed()))
            onView(withText("Connect a worker")).perform(scrollTo()).check(matches(isDisplayed()))
            onView(withText("You can connect directly without an account.")).perform(scrollTo()).check(matches(isDisplayed()))
            variants(scenario, "welcome")
        }
    }
    private fun awaitAccountStatus(scenario: ActivityScenario<AccountActivity>, expected: String) {
        awaitCondition("Account validation did not show: $expected") {
            var ready = false
            scenario.onActivity { ready = it.findViewById<android.widget.TextView>(R.id.screenStatus).text.toString() == expected }
            ready
        }
        onView(withId(R.id.screenStatus)).check(matches(withText(expected)))
    }
    @Test fun obsoletePasswordEntryUsesClerkWithoutExposingPasswordFields() = withInterceptedOAuth { starts, requests ->
        account("password").use { scenario ->
            awaitAccountStatus(scenario, "Sign-in cancelled. You can try again when you’re ready.")
            Assert.assertEquals(1, starts.get())
            Assert.assertEquals("signin", requests.single().additionalParameters["screen"])
            onView(withHint("Username")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withHint("Password")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Use an existing Shuffle password")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Use another device")).check(matches(isDisplayed()))
        }
    }
    private fun withInterceptedOAuth(block: (AtomicInteger, CopyOnWriteArrayList<AuthorizationRequest>) -> Unit) {
        // Exercise the real request builder and activity result path with no external account/network.
        val clientField = HubClient::class.java.getDeclaredField("client").apply { isAccessible = true }
        val original = clientField.get(null) as OkHttpClient
        val starts = AtomicInteger()
        val requests = CopyOnWriteArrayList<AuthorizationRequest>()
        val fixture = original.newBuilder().addInterceptor { chain ->
            val request = chain.request()
            check(request.url.toString() == "${AccountStore.DEFAULT_HUB}/v1/oauth/start" && request.method == "POST")
            val number = starts.incrementAndGet()
            val issuer = "https://identity.example"
            val body = JSONObject().put("issuer", issuer).put("authorization_endpoint", "$issuer/oauth/authorize")
                .put("token_endpoint", "$issuer/oauth/token").put("redirect_uri", AccountOAuth.REDIRECT)
                .put("client_id", "instrumentation-client").put("nonce", "nonce-$number").put("request_id", "request-$number")
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body(body.toString().toResponseBody("application/json".toMediaType())).build()
        }.build()
        val monitor = object : android.app.Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): android.app.Instrumentation.ActivityResult? {
                if (intent.component?.className != "net.openid.appauth.AuthorizationManagementActivity") return null
                requests.add(AuthorizationRequest.jsonDeserialize(requireNotNull(intent.getStringExtra("authRequest"))))
                return android.app.Instrumentation.ActivityResult(android.app.Activity.RESULT_CANCELED, null)
            }
        }
        clientField.set(null, fixture)
        instrumentation.addMonitor(monitor)
        try { block(starts, requests) }
        finally { instrumentation.removeMonitor(monitor); clientField.set(null, original) }
    }
    @Test fun accountActionsOpenBrowserImmediatelyAndRetryOnlyWhenRequested() = withInterceptedOAuth { starts, requests ->
        for ((mode, screen) in listOf("login" to "signin", "create" to "signup")) {
            val before = starts.get()
            account(mode).use { scenario ->
                awaitAccountStatus(scenario, "Sign-in cancelled. You can try again when you’re ready.")
                Assert.assertEquals(before + 1, starts.get())
                Assert.assertEquals(before + 1, requests.size)
                Assert.assertEquals(screen, requests.last().additionalParameters["screen"])
                onView(withText("Welcome back")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
                onView(withText("Continue to sign in")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
                onView(withText("Continue to create account")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
                onView(withText("Try again")).check(matches(isDisplayed()))
                // A rotation/process restoration must not replace proof belonging to the browser.
                val pending = JSONObject().put("request_id", "existing-browser-request").toString()
                AccountStore(context).pendingOAuth = pending
                scenario.recreate()
                awaitFocused(scenario)
                Assert.assertEquals(before + 1, starts.get())
                Assert.assertEquals(pending, AccountStore(context).pendingOAuth)
                onView(withText("Try again")).perform(click())
                awaitAccountStatus(scenario, "Sign-in cancelled. You can try again when you’re ready.")
                Assert.assertEquals(before + 2, starts.get())
                Assert.assertEquals(screen, requests.last().additionalParameters["screen"])
                Assert.assertNull(AccountStore(context).pendingOAuth)
                variants(scenario, "account-$mode-retry")
            }
        }
    }
    @Test fun welcomePairingOpensWithoutStartingBrowserSignIn() = withInterceptedOAuth { starts, requests ->
        ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            instrumentation.uiAutomation.waitForIdle(500, 5_000)
            onView(withText("Use another device")).perform(scrollTo(), click())
            onView(withHint("Account pairing code")).check(matches(isDisplayed()))
            onView(withText("Scan account QR")).check(matches(isDisplayed()))
            Assert.assertEquals(0, starts.get())
            Assert.assertTrue(requests.isEmpty())
            androidx.test.espresso.Espresso.pressBack()
            awaitFocused(scenario)
        }
    }
    private class TouchTrace(private val delegate: android.view.Window.Callback) : android.view.Window.Callback by delegate {
        val events = java.util.concurrent.CopyOnWriteArrayList<String>()
        override fun dispatchTouchEvent(event: android.view.MotionEvent): Boolean {
            events.add("action=${event.actionMasked}, flags=${event.flags}, x=${event.x}, y=${event.y}")
            return delegate.dispatchTouchEvent(event)
        }
    }
    @Test fun accountCodeAndQrKeepManualFallback() {
        for (mode in listOf("pair", "qr")) account(mode).use { scenario ->
            onView(withHint("Account hub HTTPS address")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withHint("Account pairing code")).check(matches(isDisplayed()))
            onView(withText("Scan account QR")).check(matches(isDisplayed()))
            lateinit var trace: TouchTrace
            scenario.onActivity { trace = TouchTrace(it.window.callback); it.window.callback = trace }
            onView(withText("Request approval")).perform(scrollTo(), click())
            try {
                awaitAccountStatus(scenario, "Enter the 12-character pairing code. Spaces and hyphens are optional.")
            } catch (error: AssertionError) {
                runCatching { TestScreenshots.capture("account-$mode-failure") }
                throw AssertionError("$mode validation failed after touches ${trace.events}", error)
            }
            variants(scenario, "account-$mode")
        }
    }
    @Test fun directServerIsClearlySeparateAndHasManualFields() {
        ActivityScenario.launch(AddServerActivity::class.java).use { scenario ->
            onView(withText("Scan QR code")).check(matches(isDisplayed()))
            onView(withHint("Worker URL")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Use a worker URL")).perform(scrollTo(), click())
            onView(withHint("Worker URL")).check(matches(isDisplayed()))
            onView(withHint("Pairing code")).perform(scrollTo()).check(matches(isDisplayed()))
            variants(scenario, "direct-server")
        }
    }
    @Test fun accountSetupDoesNotExposeOrAllowChangingTheService() = withInterceptedOAuth { _, _ ->
        account("login").use { scenario ->
            awaitAccountStatus(scenario, "Sign-in cancelled. You can try again when you’re ready.")
            onView(withHint("Account hub HTTPS address")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Account server settings")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            scenario.recreate()
            onView(withHint("Account hub HTTPS address")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
        }
    }
    @Test fun workerSetupNeverAsksForCloudflareCredentials() {
        ActivityScenario.launch(AddServerActivity::class.java).use { scenario ->
            onView(withHint("Access client secret")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withHint("Access client ID")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Advanced options")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withHint("Worker name (optional)")).check(androidx.test.espresso.assertion.ViewAssertions.doesNotExist())
            onView(withText("Use a worker URL")).perform(scrollTo(), click())
            onView(withHint("Worker URL")).perform(scrollTo(), replaceText("https://worker.example.com/pair#token=ABCD1234EFGH"), closeSoftKeyboard())
            onView(withText("Connect worker")).perform(scrollTo(), click())
            onDialogView(withText("Connect to worker.example.com?")).check(matches(isDisplayed()))
            onDialogView(withText("Cancel")).perform(click())
            // Cancelling a review never pairs a worker or persists the QR secret.
            Assert.assertTrue(ServerStore.get(context).getAll().isEmpty())
            scenario.recreate()
            onView(withHint("Pairing code")).check(matches(withText("")))
        }
    }
    @Test fun signedInWorkersUseCompactRowsAndAnOptionsMenu() {
        val store = AccountStore(context)
        val device = store.beginSignIn()
        val session = com.hobbycoders.aishuffle.account.AccountSession(AccountStore.DEFAULT_HUB, "test-only", java.time.Instant.now().plusSeconds(3600).toString(), device, "test-account", "alex")
        store.save(session)
        ServerStore.get(context).add(com.hobbycoders.aishuffle.model.Server.create("Home workstation", "https://worker.example.com", "test-only").copy(accountId = session.accountId, hubUrl = session.hub, hubServerId = "worker-one"))
        val direct = com.hobbycoders.aishuffle.model.Server.create("Direct workstation", "https://direct.example.com", "test-only")
        ServerStore.get(context).add(direct)
        ActivityScenario.launch(ServerListActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            instrumentation.uiAutomation.waitForIdle(500, 5_000)
            onView(withText("Workers")).check(matches(isDisplayed()))
            onView(withText("Home workstation")).check(matches(isDisplayed()))
            onView(withContentDescription("Options for Home workstation")).perform(click())
            onView(withText("Remove from account")).perform(click())
            onDialogView(withText("This removes Home workstation from your account on all devices and revokes its account access and managed remote connection. Direct pairings stay saved. To restore it, sign in again on that worker.")).check(matches(isDisplayed()))
            TestScreenshots.capture("remove-worker-account-confirmation")
            onDialogView(withText("Cancel")).perform(click())
            Assert.assertEquals(2, ServerStore.get(context).getAll().size)
            onView(withContentDescription("Options for Direct workstation")).perform(click())
            onView(withText("Remove from this phone")).perform(click())
            onDialogView(withText("This removes its saved connection from this phone. The worker keeps running.")).check(matches(isDisplayed()))
            TestScreenshots.capture("remove-worker-local-confirmation")
            onDialogView(withText("Continue")).perform(click())
            awaitCondition("Direct worker was not removed") { ServerStore.get(context).getById(direct.id) == null }
            onView(withText("Home workstation")).check(matches(isDisplayed()))
            variants(scenario, "workers")
        }
    }
    @Test fun toolsDefaultOffAndCannotEnableWithoutASelectedServer() {
        ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            onView(withText("Standard tools are off")).perform(scrollTo()).check(matches(isDisplayed()))
            onView(withText("Turn on standard tools")).perform(scrollTo(), click())
            Assert.assertFalse(ToolsPreferences(context).enabled)
            onView(withText("Allow while using other apps")).perform(scrollTo(), click())
            Assert.assertFalse(ToolsPreferences(context).crossAppEnabled)
            onView(withText("Open Android Accessibility settings")).perform(scrollTo()).check(matches(isDisplayed()))
            variants(scenario, "phone-tools")
        }
    }
    private data class ConsentClocks(
        val wall: Long = System.currentTimeMillis(),
        val elapsed: Long = android.os.SystemClock.elapsedRealtime(),
        val uptime: Long = android.os.SystemClock.uptimeMillis(),
    )

    private fun consent(allow: Boolean?, ttl: Long, pauseHost: Boolean = false) {
        ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
            onView(withText("Phone tools")).check(matches(isDisplayed()))
            awaitFocused(scenario)
            val result = AtomicReference<Boolean>(); val finished = CountDownLatch(1)
            val started = AtomicReference<ConsentClocks>()
            val completed = AtomicReference<ConsentClocks>()
            val completionCause = AtomicReference<Throwable>()
            val lifecycle = CopyOnWriteArrayList<String>()
            scenario.onActivity { activity ->
                activity.lifecycle.addObserver(androidx.lifecycle.LifecycleEventObserver { _, event ->
                    lifecycle.add("$event at ${ConsentClocks()}")
                })
                activity.lifecycleScope.launch {
                    started.set(ConsentClocks())
                    result.set(ToolApprovals.request("Allow test timer?", "set_timer: 30 seconds. No external action runs in this test.", started.get().wall + ttl))
                }.invokeOnCompletion { cause ->
                    completed.set(ConsentClocks())
                    completionCause.set(cause)
                    finished.countDown()
                }
            }
            val deadline = awaitConsent()
            val elapsedDeadline = requireNotNull(ToolApprovals.pending.value).expiresElapsedAt
            Assert.assertEquals("Request must remain pending until a local decision or expiry", 1L, finished.count)
            if (pauseHost) scenario.moveToState(androidx.lifecycle.Lifecycle.State.STARTED)
            else if (allow != null) {
                screenshot(scenario, "consent-${if (allow) "allow" else "deny"}")
                onView(withText(if (allow) "Allow once" else "Deny")).perform(click())
            }
            val settled = finished.await(5, TimeUnit.SECONDS)
            // Capture at coroutine completion, not after the instrumentation thread wakes.
            // A wall-clock correction, early detach, and coroutine cancellation are distinct.
            val diagnostics = "deadline=$deadline elapsedDeadline=$elapsedDeadline start=${started.get()} end=${completed.get()} " +
                "result=${result.get()} cause=${completionCause.get()} " +
                "lifecycle=$lifecycle state=${scenario.state} pending=${ToolApprovals.pending.value}"
            android.util.Log.i("ConsentTiming", diagnostics)
            Assert.assertTrue("Consent did not settle: $diagnostics", settled)
            Assert.assertNull("Coroutine cancelled instead of returning a decision: $diagnostics", completionCause.get())
            if (pauseHost) {
                Assert.assertTrue("Lifecycle cancellation waited for expiry: $diagnostics", completed.get().wall < deadline)
                Assert.assertTrue("Expected host pause: $diagnostics", lifecycle.any { it.startsWith("ON_PAUSE") })
            } else if (allow == null) {
                Assert.assertTrue("Request rejected before expiry: $diagnostics", completed.get().wall >= deadline)
                Assert.assertTrue("Request rejected before monotonic expiry: $diagnostics", completed.get().elapsed >= elapsedDeadline)
                Assert.assertFalse("Host paused before timeout: $diagnostics", lifecycle.any { it.startsWith("ON_PAUSE") })
            }
            Assert.assertEquals(diagnostics, allow == true && !pauseHost, result.get())
            Assert.assertNull(diagnostics, ToolApprovals.pending.value)
        }
    }
    @Test fun localConsentDenied() = consent(false, 20_000)
    @Test fun localConsentAcceptedOnlyByRealButton() = consent(true, 20_000)
    @Test fun localConsentExpiresWithoutAction() = consent(null, 2_000)
    @Test fun localConsentHostPauseRejectsBeforeExpiry() = consent(null, 20_000, pauseHost = true)

    @Test fun callerCancellationClearsConsentWithoutAuthorizing() {
        ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            val result = AtomicReference<Boolean>()
            val cause = AtomicReference<Throwable>()
            val finished = CountDownLatch(1)
            val request = AtomicReference<kotlinx.coroutines.Job>()
            scenario.onActivity { activity ->
                request.set(activity.lifecycleScope.launch {
                    result.set(ToolApprovals.request("Cancelled caller", "No action may be authorized"))
                }.also { job -> job.invokeOnCompletion { error -> cause.set(error); finished.countDown() } })
            }
            awaitConsent()
            request.get().cancel()
            Assert.assertTrue("Caller cancellation did not settle", finished.await(3, TimeUnit.SECONDS))
            Assert.assertTrue("Cancellation was confused with a local decision", cause.get() is kotlinx.coroutines.CancellationException)
            Assert.assertNull("Cancelled caller received an approval", result.get())
            Assert.assertNull(ToolApprovals.pending.value)
            onView(withText("Allow once")).check(doesNotExist())
        }
    }

    @Test fun expiredConsentNeverBecomesPending() {
        ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
            awaitFocused(scenario)
            val result = AtomicReference<Boolean>(); val finished = CountDownLatch(1)
            scenario.onActivity { activity -> activity.lifecycleScope.launch {
                result.set(ToolApprovals.request("Expired request", "Must never ask for approval", System.currentTimeMillis() - 1))
                finished.countDown()
            } }
            Assert.assertTrue("Expired request did not settle", finished.await(3, TimeUnit.SECONDS))
            Assert.assertFalse(result.get())
            Assert.assertNull(ToolApprovals.pending.value)
            onView(withText("Allow once")).check(doesNotExist())
        }
    }

    private fun timerApproval(allow: Boolean?, ttl: Long) {
        val monitor = instrumentation.addMonitor(android.content.IntentFilter(android.provider.AlarmClock.ACTION_SET_TIMER), android.app.Instrumentation.ActivityResult(android.app.Activity.RESULT_CANCELED, null), true)
        try {
            ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
                onView(withText("Phone tools")).check(matches(isDisplayed()))
                awaitFocused(scenario)
                val result = AtomicReference<org.json.JSONObject>(); val finished = CountDownLatch(1)
                scenario.onActivity { activity ->
                    // A local test-only paired-server fixture; no auth bypass or network success path.
                    val server = com.hobbycoders.aishuffle.model.Server.create("Instrumented server", "https://instrumentation.invalid", "test-only")
                    ServerStore.get(activity).add(server)
                    ToolsPreferences(activity).setEnabled(true)
                    DeviceToolsRuntime.attach(activity, server)
                    Assert.assertEquals(server.id, ServerStore.get(activity).selectedToolsServerId)
                    activity.lifecycleScope.launch {
                        result.set(DeviceToolExecutor(activity).execute("set_timer", org.json.JSONObject().put("seconds", 30).put("label", "Test timer"), System.currentTimeMillis() + ttl))
                        finished.countDown()
                    }
                }
                val deadline = awaitConsent()
                Assert.assertEquals("Timer must await a local decision or expiry", 1L, finished.count)
                if (allow != null) {
                    onView(withText(if (allow) "Allow once" else "Deny")).perform(click())
                }
                Assert.assertTrue("Timer consent did not settle", finished.await(6, TimeUnit.SECONDS))
                Assert.assertEquals("Timer result: ${result.get()}", if (allow == true) 1 else 0, monitor.hits)
                if (allow == true) Assert.assertEquals("launched_confirmation", result.get().getString("status"))
                else {
                    Assert.assertFalse(result.get().getBoolean("ok"))
                    Assert.assertEquals("denied", result.get().getString("status"))
                    if (allow == null) Assert.assertTrue("Timer rejected before expiry", System.currentTimeMillis() >= deadline)
                }
            }
        } finally { instrumentation.removeMonitor(monitor) }
    }
    @Test fun timerApprovalLaunchesOnlyInterceptedSystemConfirmation() = timerApproval(true, 5_000)
    @Test fun timerDenialNeverLaunchesSystemIntent() = timerApproval(false, 5_000)
    @Test fun timerExpiryNeverLaunchesSystemIntent() = timerApproval(null, 2_000)

    @Test fun consentDismissalAndStaleRequestCannotAuthorize() {
        ActivityScenario.launch(ToolSettingsActivity::class.java).use { scenario ->
            onView(withText("Phone tools")).check(matches(isDisplayed()))
            awaitFocused(scenario)
            val result = AtomicReference<Boolean>(); val finished = CountDownLatch(1)
            scenario.onActivity { activity -> activity.lifecycleScope.launch {
                result.set(ToolApprovals.request("Approve one request?", "This request has no side effects.")); finished.countDown()
            } }
            awaitConsent()
            scenario.onActivity { Assert.assertFalse(ToolApprovals.resolve(it, "stale-request-id", true)) }
            androidx.test.espresso.Espresso.pressBack()
            Assert.assertTrue(finished.await(3, TimeUnit.SECONDS)); Assert.assertFalse(result.get())
        }
    }


}
