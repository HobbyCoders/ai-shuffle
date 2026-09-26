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
import com.hobbycoders.aishuffle.ui.MobileUi
import com.hobbycoders.aishuffle.ui.WorkerAssetLoader
import com.hobbycoders.aishuffle.ui.WorkerDownloadClient
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CopyOnWriteArrayList

/** Real WebView loading the bundled UI + native activity callbacks, local intercepted HTTP for the worker API and a test-only SAF destination. */
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
        fun response(request: okhttp3.Request, text: String, mime: String) = Response.Builder().request(request)
            .protocol(Protocol.HTTP_1_1).code(200).message("Fixture").body(text.toResponseBody(mime.toMediaType())).build()
        val sessions = java.util.concurrent.atomic.AtomicInteger()
        val api = OkHttpClient.Builder().addInterceptor { chain ->
            if (chain.request().url.encodedPath == "/api/v1/mobile/key/renew")
                return@addInterceptor PresenceFixture.renewDeviceKey(chain.request(), server)
            if (chain.request().url.encodedPath == "/api/v1/mobile/web-session/reservations")
                return@addInterceptor Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(404).message("Legacy fixture")
                    .header("Content-Type", "application/json").body("{\"detail\":\"Not Found\"}".toResponseBody("application/json".toMediaType())).build()
            check(chain.request().url.encodedPath == "/api/v1/mobile/web-session")
            beforeSession(sessions.incrementAndGet())
            response(chain.request(), "{\"session_token\":\"fixture-cookie\"}", "application/json")
        }.build()
        val monitor = object : Instrumentation.ActivityMonitor() {
            override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? =
                if (intent.action == Intent.ACTION_CREATE_DOCUMENT || intent.action == Intent.ACTION_GET_CONTENT) picker(intent, server) else null
        }
        apiField.set(null, lazyOf(api)); instrumentation.addMonitor(monitor)
        try {
            ActivityScenario.launch<ChatActivity>(Intent(context, ChatActivity::class.java).putExtra(ChatActivity.EXTRA_SERVER_ID, server.id)).use { scenario ->
                beforeReady(scenario)
                // The document is the app's own bundle; the worker only answered the session mint.
                var runtimeReady = false
                waitFor("Bundled UI JavaScript never initialized") {
                    scenario.onActivity {
                        if (it.hasWindowFocus() && web(it).url == MobileUi.INDEX_URL && it.findViewById<TextView>(R.id.screenStatus).text.isEmpty()) {
                            web(it).evaluateJavascript("document.readyState === 'complete' && typeof window.shuffleMobileSetToken === 'function'") { raw ->
                                runtimeReady = raw == "true"
                            }
                        }
                    }
                    runtimeReady
                }
                block(scenario, server)
            }
        } finally {
            instrumentation.removeMonitor(monitor); apiField.set(null, oldApi)
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
    // Run this method alone in a fresh instrumentation process to cover Chromium
    // cold start (not just a WebView warmed by another test). WebView 151 used to
    // throw in ChatDownloads.install before the activity could show its first page.
    @Test fun coldChatStartsWithBootstrapAndNativeBridges() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, _ ->
            var ready = false
            waitFor("First chat document is missing its bootstrap or native bridges") {
                scenario.onActivity { activity ->
                    web(activity).evaluateJavascript("""
                        location.origin === '${MobileUi.ORIGIN}' &&
                        window.__SHUFFLE_MOBILE__?.token === 'fixture-cookie' &&
                        typeof window.shuffleFileSave?.postMessage === 'function'
                    """.trimIndent()) { ready = it == "true" }
                }
                ready
            }
            // Exercise the injected download script, not just the bridge object.
            saveBlob(scenario, "cold-start download")
            status(scenario, "File saved.")
            assertEquals("cold-start download", context.contentResolver.openInputStream(destination)!!.bufferedReader().use { it.readText() })
        }
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
    @Test fun workerMediaElementsAndOrdinaryAttachmentLinksUseAuthenticatedNativeTransport() {
        val seen = CopyOnWriteArrayList<okhttp3.Request>()
        val png = java.util.Base64.getDecoder().decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        // A valid, silent PCM WAV: lets the real WebView decode audio metadata, not just fetch bytes.
        val wav = java.nio.ByteBuffer.allocate(8044).order(java.nio.ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray()); putInt(8036); put("WAVEfmt ".toByteArray()); putInt(16)
            putShort(1); putShort(1); putInt(8000); putInt(8000); putShort(1); putShort(8)
            put("data".toByteArray()); putInt(8000); repeat(8000) { put(128.toByte()) }
        }.array()
        // Synthetic 64x64 red H.264 baseline, 5 frames/1s; generated with FFmpeg
        // color=c=red:s=64x64:r=5 -t 1 -c:v libx264 -pix_fmt yuv420p -profile:v baseline -movflags +faststart -an.
        val videoBytes = InstrumentationRegistry.getInstrumentation().context.assets.open("native-media.mp4").use { it.readBytes() }
        val http = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request(); seen.add(request)
            val path = request.url.encodedPath
            val ranged = request.header("Range") == "bytes=2-4"
            val (bytes, mime) = when {
                ranged -> "abc".toByteArray() to "video/mp4"
                path.contains("generated-audio") -> wav to "audio/wav"
                path.contains("generated-videos") -> videoBytes to "video/mp4"
                path.contains("generated-images") || path.contains("files/by-path") && request.url.queryParameter("path")!!.endsWith(".png") -> png to "image/png"
                else -> "benign attachment".toByteArray() to "application/pdf"
            }
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(if (ranged) 206 else 200).message("Fixture")
                .header("Content-Type", mime).header("Content-Length", bytes.size.toString()).header("Accept-Ranges", "bytes")
                .apply { if (ranged) header("Content-Range", "bytes 2-4/12") }.body(bytes.toResponseBody(mime.toMediaType())).build()
        }.build()
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, server ->
            scenario.onActivity { activity ->
                val transport = WorkerDownloadClient(http)
                ChatActivity::class.java.getDeclaredField("workerAssets").apply { isAccessible = true }
                    .set(activity, WorkerAssetLoader(transport))
                val downloads = ChatActivity::class.java.getDeclaredField("downloads").apply { isAccessible = true }.get(activity)
                downloads.javaClass.getDeclaredField("client").apply { isAccessible = true }.set(downloads, transport)
                web(activity).evaluateJavascript("""
                    (() => {
                      window.assetSmoke = {images:0, audio:false, video:false, range:false, errors:[]};
                      for (const src of [
                        '${server.baseUrl}/api/generated-images/by-path?path=/work/image.png',
                        '${server.baseUrl}/api/files/by-path?path=/work/chip.png',
                        '/api/files/by-path?path=/work/legacy.png']) {
                        const image = new Image(); image.onload = () => { if (image.naturalWidth === 1) assetSmoke.images++; };
                        image.onerror = () => assetSmoke.errors.push('image:' + src);
                        image.src = src; document.body.append(image);
                      }
                      const audio = document.createElement('audio'); audio.preload = 'auto';
                      audio.onloadedmetadata = () => { assetSmoke.audioDuration = audio.duration; assetSmoke.audio = audio.duration === 1; };
                      audio.onerror = () => assetSmoke.errors.push('audio:' + audio.error?.code);
                      audio.src = '${server.baseUrl}/api/generated-audio/by-path?path=/work/audio.wav'; document.body.append(audio); audio.load();
                      const video = document.createElement('video'); video.preload = 'auto';
                      video.onloadeddata = () => { assetSmoke.video = video.videoWidth === 64 && video.videoHeight === 64 && video.duration === 1; };
                      video.onerror = () => assetSmoke.errors.push('video:' + video.error?.code);
                      video.src = '${server.baseUrl}/api/generated-videos/by-path?path=/work/video.mp4'; document.body.append(video); video.load();
                      fetch('${server.baseUrl}/api/generated-videos/by-path?path=/work/range.mp4', {headers:{Range:'bytes=2-4'}})
                        .then(async r => {
                          assetSmoke.rangeResponse = {status:r.status, contentRange:r.headers.get('Content-Range'), length:r.headers.get('Content-Length')};
                          const body = await r.text(); assetSmoke.rangeResponse.body = body;
                          assetSmoke.range = r.status === 206 && r.headers.get('Content-Range') === 'bytes 2-4/12' && r.headers.get('Content-Length') === '3' && body === 'abc';
                        })
                        .catch(error => assetSmoke.errors.push('range:' + error.message));
                    })()
                """.trimIndent(), null)
            }
            var ready = false
            var lastState = "No JavaScript callback"
            try {
                waitFor("WebView did not decode images/audio and preserve the ranged response") {
                    scenario.onActivity { web(it).evaluateJavascript("JSON.stringify(assetSmoke)") { raw ->
                        lastState = org.json.JSONTokener(raw).nextValue().toString()
                        val state = org.json.JSONObject(lastState)
                        ready = state.optInt("images") == 3 && state.optBoolean("audio") && state.optBoolean("video") && state.optBoolean("range")
                    } }
                    ready
                }
            } catch (error: AssertionError) {
                throw AssertionError("${error.message}; state=$lastState; requested=${seen.map { it.url.encodedPath + "?" + it.url.encodedQuery + " Range=" + it.header("Range") }}", error)
            }
            waitFor("Video element did not request its source") { seen.any { it.url.queryParameter("path") == "/work/video.mp4" } }
            for (url in listOf("${server.baseUrl}/api/v1/notes/n/attachments/a/download", "/api/files/by-path?path=/work/report.pdf")) {
                val before = seen.size
                val expected = if (url.startsWith("/")) server.baseUrl + url else url
                scenario.onActivity { web(it).evaluateJavascript("""
                    (() => { const a=document.createElement('a'); a.href=${org.json.JSONObject.quote(url)};
                    a.target='_blank'; a.textContent='report.pdf'; document.body.append(a);
                    a.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); })()
                """.trimIndent(), null) }
                waitFor("Native attachment download was not requested") { seen.drop(before).any { it.url.toString() == expected } }
                status(scenario, "File saved.")
                assertEquals("benign attachment", context.contentResolver.openInputStream(destination)!!.bufferedReader().use { it.readText() })
                scenario.onActivity { assertEquals(MobileUi.INDEX_URL, web(it).url) }
            }
            assertTrue(seen.any { it.url.encodedPath.contains("notes/") })
            seen.forEach {
                assertEquals("chat-fixture.example", it.url.host)
                assertEquals("Bearer fixture-cookie", it.header("Authorization"))
                assertFalse(it.url.toString().contains("fixture-cookie"))
            }
        }
    }
    @Test fun cancellationAndRemovedWorkerNeverPersistDownloadedBytes() {
        fixture({ _, _ -> Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null) }) { scenario, _ ->
            saveBlob(scenario); status(scenario, "Download cancelled.")
            assertThrows(java.io.FileNotFoundException::class.java) { context.contentResolver.openInputStream(destination) }
        }
        fixture({ _, server -> ServerStore.get(context).remove(server.id); Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(destination)) }) { scenario, _ ->
            lateinit var downloadStatus: TextView
            scenario.onActivity { downloadStatus = it.findViewById(R.id.screenStatus) }
            // SAF creates the selected document before returning it. Seed it so an
            // absent destination proves removePartial finished, not that it never ran.
            context.contentResolver.openOutputStream(destination, "wt")!!.use { it.write("picker-created document".toByteArray()) }
            saveBlob(scenario)
            waitFor("Removed-worker download was neither rejected nor its chat closed") {
                // ActivityScenario.getState synchronizes with the main thread and
                // must itself run on the instrumentation thread, not inside runOnMainSync.
                if (scenario.state == androidx.lifecycle.Lifecycle.State.DESTROYED) {
                    true
                } else {
                    var rejected = false
                    instrumentation.runOnMainSync {
                        // The captured view can be read even if authorization loss
                        // finished ChatActivity; never call onActivity after removal.
                        rejected = downloadStatus.text.toString() == "Chat changed. Download the file again."
                    }
                    rejected
                }
            }
            // removePartial uses IO + NonCancellable: neither the status nor activity
            // destruction is a barrier for the asynchronous document deletion.
            waitFor("Removed-worker download left its picker document behind") {
                try {
                    context.contentResolver.openInputStream(destination)!!.use { }
                    false
                } catch (_: java.io.FileNotFoundException) { true }
            }
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
                val audio = Permission(Uri.parse(MobileUi.ORIGIN), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                chrome.onPermissionRequest(audio); assertArrayEquals(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE), audio.granted)
                val outside = Permission(Uri.parse("https://outside.example"), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                chrome.onPermissionRequest(outside); assertTrue(outside.denied)
                // The worker is an API origin, never the document: it cannot hold the microphone either.
                val worker = Permission(Uri.parse(server.baseUrl), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                chrome.onPermissionRequest(worker); assertTrue(worker.denied)
                val mixed = Permission(Uri.parse(MobileUi.ORIGIN), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE, PermissionRequest.RESOURCE_VIDEO_CAPTURE))
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
            val audio = Permission(Uri.parse(MobileUi.ORIGIN), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            scenario.onActivity { pending(it, audio); assertNull(audio.granted); assertFalse(audio.denied) }
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED)
            waitFor("Pending audio permission was not completed on resume") { audio.granted != null }
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.STARTED)
            val stale = Permission(Uri.parse(MobileUi.ORIGIN), arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
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
}
