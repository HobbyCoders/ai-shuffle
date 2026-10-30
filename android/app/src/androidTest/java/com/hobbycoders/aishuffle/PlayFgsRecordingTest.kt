package com.hobbycoders.aishuffle

import android.Manifest
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.util.Log
import android.webkit.WebView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.VoiceCallService
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Temporary evidence capture, not a replacement app, rendered animation or bridge-state injection. */
@RunWith(AndroidJUnit4::class)
class PlayFgsRecordingTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val device get() = UiDevice.getInstance(instrumentation)
    private val out get() = File(context.externalMediaDirs.first(), "play-fgs").apply { mkdirs() }
    private fun web(activity: ChatActivity): WebView =
        ChatActivity::class.java.getDeclaredField("web").apply { isAccessible = true }.get(activity) as WebView
    private fun shell(command: String): String =
        ParcelFileDescriptor.AutoCloseInputStream(instrumentation.uiAutomation.executeShellCommand(command)).bufferedReader().use { it.readText() }
    private fun pause(ms: Long) = SystemClock.sleep(ms)
    private fun waitFor(message: String, timeout: Long = 30_000, predicate: () -> Boolean) {
        val until = SystemClock.elapsedRealtime() + timeout
        while (!predicate()) {
            if (SystemClock.elapsedRealtime() >= until) throw AssertionError(message)
            pause(100)
        }
    }
    private fun js(scenario: ActivityScenario<ChatActivity>, expression: String): String {
        val done = CountDownLatch(1)
        var value = ""
        scenario.onActivity { web(it).evaluateJavascript(expression) { raw -> value = raw; done.countDown() } }
        assertTrue("WebView read timed out", done.await(5, TimeUnit.SECONDS))
        return value
    }
    private fun tap(label: String) {
        val control = device.wait(Until.findObject(By.desc(label)), 15_000)
            ?: device.wait(Until.findObject(By.text(label)), 2_000)
        assertNotNull("Actual UI control missing: $label", control)
        control!!.click()
    }
    private fun phase(name: String) {
        Log.i("PlayFgsDemo", "phase=$name")
        OkHttpClient().newCall(okhttp3.Request.Builder().url("https://localhost:8443/demo/phase?name=$name").build())
            .execute().use { assertTrue(it.isSuccessful) }
    }
    @Suppress("DEPRECATION")
    private fun assertRecordingAndForeground() {
        val running = context.getSystemService(ActivityManager::class.java).getRunningServices(100)
            .any { it.service.className == VoiceCallService::class.java.name && it.foreground }
        assertTrue("Real VoiceCallService is not in foreground", running)
        val info = context.packageManager.getServiceInfo(android.content.ComponentName(context, VoiceCallService::class.java), 0)
        assertTrue("Service is not microphone type", info.foregroundServiceType and ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE != 0)
        val recordings = context.getSystemService(AudioManager::class.java).activeRecordingConfigurations
        assertTrue("Android reports no active microphone recorder", recordings.isNotEmpty())
        Log.i("PlayFgsDemo", "Real microphone recorder active; real microphone-type FGS foreground")
    }

    @Test fun recordActualMicrophoneForegroundService() {
        val session = InstrumentationRegistry.getArguments().getString("fixture_session")
        assertFalse("Run through record-play-fgs.py with its disposable worker", session.isNullOrBlank())
        val server = Server.create("Demo worker", "https://localhost:8443", "demo-key-not-a-real-credential")
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server) }
        }
        instrumentation.uiAutomation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
        // Pairing receipt only. Every page, conversation and voice API is served by the real dev worker.
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val oldApi = apiField.get(null)
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            fun answer(code: Int, json: String) = Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
                .code(code).message("Disposable pairing fixture").header("Content-Type", "application/json")
                .body(json.toResponseBody("application/json".toMediaType())).build()
            when (request.url.encodedPath) {
                "/api/v1/mobile/key/renew" -> PresenceFixture.renewDeviceKey(request, server)
                "/api/v1/mobile/web-session/reservations" -> answer(404, "{\"detail\":\"Not Found\"}")
                "/api/v1/mobile/web-session" -> answer(200, "{\"session_token\":\"demo-session-not-a-real-credential\"}")
                else -> chain.proceed(request)
            }
        }.build()
        apiField.set(null, lazyOf(api))
        var recording: ParcelFileDescriptor? = null
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java)
                .putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                try {
                    waitFor("Real phone UI did not bootstrap", 60_000) {
                        js(scenario, "document.readyState === 'complete' && typeof window.shuffleMobileSetToken === 'function'") == "true"
                    }
                } catch (error: AssertionError) {
                    device.takeScreenshot(File(out, "bootstrap-failure.png"))
                    Log.i("PlayFgsDemo", "bootstrap diagnostic: " + js(scenario,
                        "JSON.stringify({url:location.href,ready:document.readyState,title:document.title,text:document.body.innerText.slice(0,2000),runtime:!!window.__SHUFFLE_MOBILE__,setter:typeof window.shuffleMobileSetToken})"))
                    scenario.onActivity { Log.i("PlayFgsDemo", "native status: " + it.findViewById<android.widget.TextView>(R.id.screenStatus).text) }
                    throw error
                }
                scenario.onActivity { web(it).loadUrl("https://localhost:8443/mobile/?session=$session") }
                waitFor("Seeded chat and real call button did not appear", 60_000) {
                    js(scenario, "!!document.querySelector('button[aria-label=\"Start a call in this conversation\"]')") == "true"
                }
                device.takeScreenshot(File(out, "01-chat.png"))
                phase("chat")
                recording = instrumentation.uiAutomation.executeShellCommand(
                    "screenrecord --size 720x1280 --bit-rate 6000000 --time-limit 90 ${out.absolutePath}/play-fgs-microphone.mp4")
                pause(3_000)
                phase("start-call")
                tap("Start a call in this conversation")
                val permission = device.wait(Until.findObject(By.res("com.android.permissioncontroller", "permission_allow_foreground_only_button")), 20_000)
                assertNotNull("Real Android microphone permission prompt did not appear", permission)
                device.takeScreenshot(File(out, "02-permission.png"))
                pause(1_500)
                permission!!.click()
                waitFor("Real call did not become live", 60_000) {
                    js(scenario, "document.querySelector('[aria-label=\"Active voice call\"]')?.textContent.includes('Voice call · live') === true") == "true"
                }
                phase("live")
                assertRecordingAndForeground()
                device.takeScreenshot(File(out, "03-call.png"))
                pause(4_000)
                device.openNotification()
                assertTrue("Real ongoing call notification missing", device.wait(Until.hasObject(By.textContains("Voice call")), 5_000))
                phase("notification")
                device.takeScreenshot(File(out, "04-notification.png"))
                pause(3_000)
                device.pressBack()
                pause(500)
                device.pressHome()
                phase("background")
                pause(2_000)
                assertRecordingAndForeground()
                phase("locked")
                device.sleep()
                pause(4_000)
                assertRecordingAndForeground()
                device.wakeUp()
                pause(1_500)
                device.takeScreenshot(File(out, "05-lock-screen.png"))
                pause(1_500)
                device.swipe(device.displayWidth / 2, device.displayHeight * 4 / 5, device.displayWidth / 2, device.displayHeight / 5, 30)
                pause(500)
                device.openNotification()
                val notification = device.wait(Until.findObject(By.textContains("Voice call")), 5_000)
                assertNotNull("Ongoing call notification lost across lock", notification)
                notification!!.click()
                phase("unlocked")
                waitFor("Call did not stay live after unlocking") {
                    js(scenario, "document.querySelector('[aria-label=\"Active voice call\"]')?.textContent.includes('Voice call · live') === true") == "true"
                }
                assertRecordingAndForeground()
                device.takeScreenshot(File(out, "06-unlocked.png"))
                pause(3_000)
                tap("End voice call")
                phase("ended")
                waitFor("Call controls did not disappear after hangup") {
                    js(scenario, "!document.querySelector('[aria-label=\"Active voice call\"]')") == "true"
                }
                @Suppress("DEPRECATION")
                waitFor("Microphone service stayed in foreground after hangup") {
                    context.getSystemService(ActivityManager::class.java).getRunningServices(100)
                        .none { it.service.className == VoiceCallService::class.java.name && it.foreground }
                }
                device.takeScreenshot(File(out, "07-ended.png"))
                pause(2_500)
            }
        } finally {
            if (recording != null) {
                shell("pkill -2 screenrecord")
                ParcelFileDescriptor.AutoCloseInputStream(recording!!).use { it.readBytes() }
            }
            device.takeScreenshot(File(out, "last-screen.png"))
            apiField.set(null, oldApi)
        }
    }
}
