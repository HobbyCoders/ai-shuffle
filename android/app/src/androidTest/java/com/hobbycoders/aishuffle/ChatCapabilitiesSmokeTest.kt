package com.hobbycoders.aishuffle

import android.app.Activity
import android.app.Instrumentation
import android.content.Intent
import android.net.Uri
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.tools.DeviceToolsRuntime
import com.hobbycoders.aishuffle.tools.ToolsPreferences
import com.hobbycoders.aishuffle.ui.ChatActivity
import com.hobbycoders.aishuffle.ui.ContainedDocument
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CopyOnWriteArrayList

/** Real WebView + native activity callbacks, local intercepted HTTP and a test-only SAF destination. */
@RunWith(AndroidJUnit4::class)
class ChatCapabilitiesSmokeTest {
    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val destination = Uri.parse("content://com.hobbycoders.aishuffle.test.downloads/document/fixture")
    private fun waitFor(message: String, predicate: () -> Boolean) = kotlinx.coroutines.runBlocking {
        try { kotlinx.coroutines.withTimeout(12_000) { while (!predicate()) kotlinx.coroutines.delay(50) } }
        catch (error: kotlinx.coroutines.TimeoutCancellationException) { throw AssertionError(message, error) }
    }
    private fun web(activity: ChatActivity): WebView {
        val shell = activity.findViewById<TextView>(R.id.screenStatus).parent as LinearLayout
        return shell.getChildAt(shell.childCount - 1) as WebView
    }
    private fun fixture(
        picker: (Intent, Server) -> Instrumentation.ActivityResult?,
        beforeSession: (Int) -> Unit = {},
        beforeReady: (ActivityScenario<ChatActivity>) -> Unit = {},
        block: (ActivityScenario<ChatActivity>, Server) -> Unit,
    ) {
        val server = Server.create("Benign capability fixture", "https://chat-fixture.example", "fixture-key")
        instrumentation.runOnMainSync {
            DeviceToolsRuntime.stop(); ToolsPreferences(context).setEnabled(false); AccountStore(context).clear()
            ServerStore.get(context).apply { getAll().forEach { remove(it.id) }; add(server) }
        }
        context.contentResolver.delete(destination, null, null)
        val apiField = ApiClient::class.java.getDeclaredField("client\$delegate").apply { isAccessible = true }
        val oldApi = apiField.get(null)
        val docField = ContainedDocument::class.java.getDeclaredField("http").apply { isAccessible = true }
        val oldDoc = docField.get(null)
        fun response(request: okhttp3.Request, text: String, mime: String) = Response.Builder().request(request)
            .protocol(Protocol.HTTP_1_1).code(200).message("Fixture").body(text.toResponseBody(mime.toMediaType())).build()
        val sessions = java.util.concurrent.atomic.AtomicInteger()
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            if (chain.request().url.encodedPath == "/api/v1/mobile/web-session/reservations")
                return@addInterceptor Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(404).message("Legacy fixture")
                    .header("Content-Type", "application/json").body("{\"detail\":\"Not Found\"}".toResponseBody("application/json".toMediaType())).build()
            check(chain.request().url.encodedPath == "/api/v1/mobile/web-session")
            beforeSession(sessions.incrementAndGet())
            response(chain.request(), "{\"session_token\":\"fixture-cookie\"}", "application/json")
        }.build()
        val doc = OkHttpClient.Builder().addInterceptor { chain ->
            check(chain.request().url.host == "chat-fixture.example")
            response(chain.request(), "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>Benign file and permission fixture</body></html>", "text/html")
        }.build()
        val monitor = object : Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? =
                if (intent.action == Intent.ACTION_CREATE_DOCUMENT || intent.action == Intent.ACTION_GET_CONTENT) picker(intent, server) else null
        }
        apiField.set(null, lazyOf(api)); docField.set(null, doc); instrumentation.addMonitor(monitor)
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                beforeReady(scenario)
                waitFor("Fixture document never loaded") {
                    var ready = false
                    scenario.onActivity { ready = it.hasWindowFocus() && web(it).url == server.baseUrl + "/" && it.findViewById<TextView>(R.id.screenStatus).text.isEmpty() }
                    ready
                }
                block(scenario, server)
            }
        } finally {
            instrumentation.removeMonitor(monitor); apiField.set(null, oldApi); docField.set(null, oldDoc)
            instrumentation.runOnMainSync { DeviceToolsRuntime.stop(); ServerStore.get(context).remove(server.id) }
            context.contentResolver.delete(destination, null, null)
        }
    }
    private fun status(scenario: ActivityScenario<ChatActivity>, expected: String) = waitFor("Expected status: $expected") {
        var matched = false
        scenario.onActivity { matched = it.findViewById<TextView>(R.id.screenStatus).text.toString() == expected }
        matched
    }
    private fun saveBlob(scenario: ActivityScenario<ChatActivity>, contents: String = "small benign fixture") {
        scenario.onActivity { web(it).evaluateJavascript("""
            (() => { const blob = new Blob([${org.json.JSONObject.quote(contents)}], {type:'text/plain'});
            const url = URL.createObjectURL(blob); const a = document.createElement('a');
            a.href = url; a.download = 'fixture.txt'; a.click(); URL.revokeObjectURL(url); })()
        """.trimIndent(), null) }
    }
    @Test fun blobAndInlineImageSaveThroughRealWebViewWithoutBrowserHandoff() {
        val chosen = CopyOnWriteArrayList<String>()
        fixture({ intent, _ -> chosen.add(intent.getStringExtra(Intent.EXTRA_TITLE).orEmpty()); Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, _ ->
            val contents = "small benign fixture\n".repeat(4000)
            saveBlob(scenario, contents)
            status(scenario, "File saved.")
            assertEquals(contents, context.contentResolver.openInputStream(destination)!!.bufferedReader().use { it.readText() })
            val png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            scenario.onActivity { web(it).evaluateJavascript("""
                (() => { const a = document.createElement('a'); a.href='data:image/png;base64,$png'; a.download='image.png'; a.click(); })()
            """.trimIndent(), null) }
            waitFor("Image save did not open its own picker") { chosen.size == 2 }
            status(scenario, "File saved.")
            val image = context.contentResolver.openInputStream(destination)!!.use { it.readBytes() }
            assertArrayEquals(java.util.Base64.getDecoder().decode(png), image)
            val decoded = android.graphics.BitmapFactory.decodeByteArray(image, 0, image.size)
            assertNotNull(decoded); assertEquals(1, decoded.width); assertEquals(1, decoded.height); decoded.recycle()
            assertEquals(listOf("fixture.txt", "image.png"), chosen.toList())
        }
    }
    @Test fun cancellationAndRemovedWorkerNeverPersistDownloadedBytes() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, _ ->
            saveBlob(scenario); status(scenario, "Download cancelled.")
            assertThrows(java.io.FileNotFoundException::class.java) { context.contentResolver.openInputStream(destination) }
        }
        fixture({ _, server -> ServerStore.get(context).remove(server.id); Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, _ ->
            saveBlob(scenario); status(scenario, "Chat changed. Download the file again.")
            assertThrows(java.io.FileNotFoundException::class.java) { context.contentResolver.openInputStream(destination) }
        }
    }
    private class Permission(private val source: Uri, private val requested: Array<String>) : PermissionRequest() {
        var granted: Array<String>? = null; var denied = false
        override fun getOrigin() = source
        override fun getResources() = requested
        override fun grant(resources: Array<String>) { granted = resources }
        override fun deny() { denied = true }
    }
    @Test fun microphoneGrantIsAudioOnlyAndFileSelectionRechecksWorkerOwnership() {
        instrumentation.uiAutomation.grantRuntimePermission(context.packageName, android.Manifest.permission.RECORD_AUDIO)
        fixture({ _, server -> ServerStore.get(context).remove(server.id); Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, server ->
            scenario.onActivity { activity ->
                val view = web(activity); val chrome = requireNotNull(view.webChromeClient)
                val audio = Permission(Uri.parse(server.baseUrl), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                chrome.onPermissionRequest(audio); assertArrayEquals(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE), audio.granted)
                val outside = Permission(Uri.parse("https://outside.example"), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                chrome.onPermissionRequest(outside); assertTrue(outside.denied)
                val mixed = Permission(Uri.parse(server.baseUrl), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE, PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                chrome.onPermissionRequest(mixed); assertTrue(mixed.denied)
            }
            var delivered = false; var selected: Array<Uri>? = null
            scenario.onActivity { activity ->
                val params = object : WebChromeClient.FileChooserParams() {
                    override fun getMode() = MODE_OPEN
                    override fun getAcceptTypes() = arrayOf("text/plain")
                    override fun isCaptureEnabled() = false
                    override fun getTitle(): CharSequence? = null
                    override fun getFilenameHint(): String? = null
                    override fun createIntent() = Intent(Intent.ACTION_GET_CONTENT).setType("text/plain")
                }
                val view = web(activity)
                view.webChromeClient!!.onShowFileChooser(view, ValueCallback { selected = it; delivered = true }, params)
            }
            waitFor("File picker did not return") { delivered }
            assertNull(selected)
        }
    }
    @Test fun microphonePermissionResultWaitsForResumedChatAndRejectsRemovedWorker() {
        instrumentation.uiAutomation.grantRuntimePermission(context.packageName, android.Manifest.permission.RECORD_AUDIO)
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, server ->
            val complete = ChatActivity::class.java.getDeclaredMethod("completeMicrophonePermission").apply { isAccessible = true }
            fun pending(activity: ChatActivity, request: Permission) {
                fun field(name: String) = ChatActivity::class.java.getDeclaredField(name).apply { isAccessible = true }
                field("microphone").set(activity, request)
                field("microphoneDocument").setInt(activity, field("document").getInt(activity))
                field("microphoneReady").setBoolean(activity, true)
                complete.invoke(activity)
            }
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.STARTED)
            val audio = Permission(Uri.parse(server.baseUrl), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            scenario.onActivity { pending(it, audio); assertNull(audio.granted); assertFalse(audio.denied) }
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED)
            waitFor("Pending audio permission was not completed on resume") { audio.granted != null }
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.STARTED)
            val stale = Permission(Uri.parse(server.baseUrl), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            scenario.onActivity {
                pending(it, stale); assertNull(stale.granted)
                ServerStore.get(context).remove(server.id)
                complete.invoke(it); assertTrue(stale.denied); assertNull(stale.granted)
            }
        }
    }
    /** Opt-in interactive fixture: the device executor chooses a destination in the real system picker. */
    @Test fun realSystemSaveDialogReturnsToChatAndCompletesTransfer() {
        org.junit.Assume.assumeTrue(InstrumentationRegistry.getArguments().getString("realSaveDialog") == "true")
        fixture({ _, _ -> null }) { scenario, _ ->
            scenario.onActivity { web(it).evaluateJavascript("""
                (() => { const url=URL.createObjectURL(new Blob(['Benign Shuffle system Save dialog fixture\\n'], {type:'text/plain'}));
                const a=document.createElement('a'); a.href=url; a.download='shuffle-download-smoke.txt'; a.click(); URL.revokeObjectURL(url); })()
            """.trimIndent(), null) }
            kotlinx.coroutines.runBlocking {
                kotlinx.coroutines.withTimeout(180_000) {
                    while (true) {
                        var done = false
                        scenario.onActivity { done = it.findViewById<TextView>(R.id.screenStatus).text.toString() == "File saved." }
                        if (done) break
                        kotlinx.coroutines.delay(100)
                    }
                }
            }
        }
    }
    @Test fun nativeVoiceHandoffPreservesSelectedChatAndRejectsReplay() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, server ->
            val launches = CopyOnWriteArrayList<Intent>()
            val monitor = object : Instrumentation.ActivityMonitor() {
                override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                    if (intent.component?.className != "com.hobbycoders.aishuffle.voice.VoiceCallActivity") return null
                    launches.add(intent)
                    return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
                }
            }
            instrumentation.addMonitor(monitor)
            try {
                val request = """{"type":"start","id":"4165b4fc-c414-4e76-a39a-e173904831ca","sessionId":"chosen_chat-123"}"""
                scenario.onActivity { web(it).evaluateJavascript("window.voiceReplies=[]; shuffleNativeVoice.addEventListener('message',e=>voiceReplies.push(JSON.parse(e.data))); shuffleNativeVoice.postMessage(${org.json.JSONObject.quote(request)});", null) }
                waitFor("Native voice did not open") { launches.size == 1 }
                assertEquals(server.id, launches.single().getStringExtra("server_id"))
                assertEquals("chosen_chat-123", launches.single().getStringExtra("session_id"))
                scenario.onActivity { web(it).evaluateJavascript("shuffleNativeVoice.postMessage(${org.json.JSONObject.quote(request)});", null) }
                var replies = ""
                waitFor("The handoff did not acknowledge then refuse the replay") {
                    scenario.onActivity { web(it).evaluateJavascript("JSON.stringify(voiceReplies)") { raw -> replies = raw } }
                    replies.contains("true") && replies.contains("false")
                }
                assertEquals(1, launches.size)
            } finally { instrumentation.removeMonitor(monitor) }
        }
    }
    @Test fun failedNativeVoiceLaunchHasOneFailureAndNeverAcknowledgesSuccess() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, server ->
            var originalPage = 0
            val pageField = ChatActivity::class.java.getDeclaredField("document").apply { isAccessible = true }
            scenario.onActivity { activity ->
                originalPage = pageField.getInt(activity)
                val view = web(activity)
                val bridgeField = ChatActivity::class.java.getDeclaredField("nativeVoice").apply { isAccessible = true }
                (bridgeField.get(activity) as com.hobbycoders.aishuffle.ui.NativeVoiceBridge).close()
                val fixtureBridge = com.hobbycoders.aishuffle.ui.NativeVoiceBridge(activity, view, server.baseUrl, { true }) {
                    throw IllegalStateException("Fixture activity launch refused")
                }
                bridgeField.set(activity, fixtureBridge)
                fixtureBridge.install()
                view.reload()
            }
            waitFor("Replacement voice bridge document did not load") {
                var ready = false
                scenario.onActivity { ready = pageField.getInt(it) > originalPage && web(it).progress == 100 && it.hasWindowFocus() }
                ready
            }
            val request = """{"type":"start","id":"4165b4fc-c414-4e76-a39a-e173904831ca","sessionId":"chosen_chat-123"}"""
            scenario.onActivity { web(it).evaluateJavascript("window.voiceReplies=[]; shuffleNativeVoice.addEventListener('message',e=>voiceReplies.push(JSON.parse(e.data))); shuffleNativeVoice.postMessage(${org.json.JSONObject.quote(request)});", null) }
            var replies = ""
            waitFor("Failed voice launch never replied") {
                scenario.onActivity { web(it).evaluateJavascript("JSON.stringify(voiceReplies)") { raw -> replies = raw } }
                replies.contains("false")
            }
            assertFalse(replies.contains("true"))
            val decoded = org.json.JSONArray(org.json.JSONTokener(replies).nextValue() as String)
            assertEquals(1, decoded.length())
            assertEquals("Voice could not open. Try again.", decoded.getJSONObject(0).getString("error"))
        }
    }
    @Test fun interruptedInitialSessionLoadsAfterResumeWithoutRotatingReadyChat() {
        val firstStarted = java.util.concurrent.CountDownLatch(1)
        val releaseFirst = java.util.concurrent.CountDownLatch(1)
        val requests = java.util.concurrent.atomic.AtomicInteger()
        fixture(
            picker = { _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) },
            beforeSession = { number ->
                requests.incrementAndGet()
                if (number == 1) { firstStarted.countDown(); check(releaseFirst.await(10, java.util.concurrent.TimeUnit.SECONDS)) }
            },
            beforeReady = { scenario ->
                assertTrue(firstStarted.await(5, java.util.concurrent.TimeUnit.SECONDS))
                scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED)
                releaseFirst.countDown()
                scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED)
            },
        ) { scenario, _ ->
            assertEquals(2, requests.get())
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED)
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED)
            instrumentation.waitForIdleSync()
            assertEquals("Ready chat must keep its existing cookie on ordinary resume", 2, requests.get())
        }
    }
    @Test fun nativeVoiceHandoffCannotReplaceAnActiveCallForAnotherChat() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, server ->
            val launches = CopyOnWriteArrayList<Intent>()
            val monitor = object : Instrumentation.ActivityMonitor() {
                override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
                    if (intent.component?.className != "com.hobbycoders.aishuffle.voice.VoiceCallActivity") return null
                    launches.add(intent); return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
                }
            }
            instrumentation.addMonitor(monitor)
            try {
                scenario.onActivity {
                    com.hobbycoders.aishuffle.voice.VoiceCallState.update { state -> state.copy(status = com.hobbycoders.aishuffle.voice.CallStatus.LIVE, serverId = server.id, sessionId = "another-chat") }
                    web(it).evaluateJavascript("window.voiceReplies=[]; shuffleNativeVoice.addEventListener('message',e=>voiceReplies.push(JSON.parse(e.data))); shuffleNativeVoice.postMessage('{\"type\":\"start\",\"id\":\"4165b4fc-c414-4e76-a39a-e173904831ca\",\"sessionId\":\"chosen-chat\"}');", null)
                }
                var replies = ""
                waitFor("Active-call handoff was not refused") {
                    scenario.onActivity { web(it).evaluateJavascript("JSON.stringify(voiceReplies)") { raw -> replies = raw } }
                    replies.contains("false")
                }
                assertTrue(replies.contains("End the current voice call")); assertTrue(launches.isEmpty())
            } finally {
                instrumentation.removeMonitor(monitor)
                scenario.onActivity { com.hobbycoders.aishuffle.voice.VoiceCallState.reset() }
            }
        }
    }
}
