package com.hobbycoders.aishuffle.tools

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.NativeScreen
import com.hobbycoders.aishuffle.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.IOException
import java.lang.ref.WeakReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class DeviceToolsRuntimeTest {
    class ApprovalHost : NativeScreen()
    class HandoffActivity : Activity() {
        override fun startActivity(intent: Intent) {
            super.startActivity(intent)
            // Android pauses the caller after an external intent. Re-enter immediately to expose the race.
            DeviceToolsRuntime.detach(this)
        }
    }
    private val app get() = RuntimeEnvironment.getApplication()
    private val target = Server("worker", "Worker", "https://worker.example", "aim_test", "phone")
    private val intent get() = Intent(Intent.ACTION_VIEW, android.net.Uri.parse("https://example.com"))
    private fun field(name: String) = DeviceToolsRuntime::class.java.getDeclaredField(name).apply { isAccessible = true }
    @Before fun setup() {
        val prefs = app.getSharedPreferences("runtime-workers", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        val store = ServerStore::class.java.getDeclaredConstructor(SharedPreferences::class.java).apply { isAccessible = true }.newInstance(prefs)
        store.add(target)
        ServerStore::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, store)
        app.getSharedPreferences("device_tools", Context.MODE_PRIVATE).edit().putBoolean("enabled", true).commit()
    }
    @After fun cleanup() {
        field("server").set(null, null)
        DeviceToolsRuntime.stop()
        ServerStore::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
    }
    private fun host(activity: Activity): Job = Job().also {
        field("activity").set(null, WeakReference(activity))
        field("context").set(null, app)
        field("server").set(null, target)
        field("polling").set(null, it)
    }
    @Test fun nativeScreenClearsTheExecutionHostBeforeResolvingPausedConsent() = runBlocking {
        Robolectric.buildActivity(ApprovalHost::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().visible().use { controller ->
            controller.windowFocusChanged(true)
            val command = host(controller.get())
            val decision = CoroutineScope(Dispatchers.Main.immediate).async {
                ToolApprovals.request("Pause test", "No action may run") to DeviceToolsRuntime.hasAttachedActivity()
            }
            org.robolectric.shadows.ShadowLooper.idleMainLooper()
            assertNotNull(ToolApprovals.pending.value)
            controller.pause()
            org.robolectric.shadows.ShadowLooper.idleMainLooper()
            val (approved, stillAttached) = decision.await()
            assertFalse(approved)
            assertFalse("NativeScreen must detach the execution host before resolving consent", stillAttached)
            assertTrue("The factual no-action result must survive pause shutdown", command.isActive)
        }
    }
    @Test fun dialogFocusLossDoesNotMeanTheApprovalHostWasDetached() {
        Robolectric.buildActivity(HandoffActivity::class.java).setup().visible().use { controller ->
            host(controller.get())
            controller.windowFocusChanged(false)
            assertTrue(DeviceToolsRuntime.hasAttachedActivity())
            assertFalse(DeviceToolsRuntime.hasForegroundActivity())
            DeviceToolsRuntime.detach(controller.get())
            assertFalse(DeviceToolsRuntime.hasAttachedActivity())
        }
    }
    @Test fun ownHandoffDoesNotCancelTheCommandBeforeItsResultCanBePosted() = runBlocking {
        Robolectric.buildActivity(HandoffActivity::class.java).setup().visible().use { controller ->
            controller.windowFocusChanged(true)
            val command = host(controller.get())
            val result = DeviceToolsRuntime.launch(intent, System.currentTimeMillis() + 20_000)
            assertEquals("launched_confirmation", result.getString("status"))
            assertTrue("The handoff must leave the result-delivery coroutine alive", command.isActive)
            assertEquals(target, field("server").get(null))
            command.cancel()
        }
    }
    @Test fun handoffIsJournaledAndPostedBeforeShutdownWithoutExecutingAnotherCommandOffScreen() = runBlocking {
        for (offline in listOf(false, true)) {
            Robolectric.buildActivity(HandoffActivity::class.java).setup().visible().use { controller ->
                controller.windowFocusChanged(true)
                val command = host(controller.get())
                val epoch = field("generation").getLong(null)
                val prefs = app.getSharedPreferences("handoff-journal", Context.MODE_PRIVATE)
                prefs.edit().clear().commit()
                val journal = CommandJournal(prefs)
                val expires = System.currentTimeMillis() + 20_000
                val commands = JSONArray()
                for (id in listOf("first", "second")) commands.put(JSONObject().put("id", id).put("name", "open_url")
                    .put("arguments", JSONObject()).put("expires_at", expires))
                var launches = 0
                val poller = DeviceCommandPoller(journal, "scope", { path, body ->
                    if (path == "pending") JSONObject().put("commands", commands)
                    else {
                        assertTrue("Shutdown must follow the result POST", command.isActive)
                        assertEquals(target, field("server").get(null))
                        val expected = if (path == "first/result") "launched_confirmation" else "foreground_required"
                        assertEquals(expected, body!!.getJSONObject("result").getString("status"))
                        assertEquals(expected, journal.get(CommandJournal.key("scope", path.substringBefore('/')))!!.getJSONObject("result").getString("status"))
                        if (offline) throw IOException("offline")
                        JSONObject().put("ok", true)
                    }
                }, { DeviceToolsRuntime.hasForegroundActivity() }, { _, _, deadline ->
                    launches++
                    DeviceToolsRuntime.launch(intent, deadline)
                })
                try {
                    try { poller.pollOnce(true); assertFalse(offline) }
                    catch (_: IOException) { assertTrue(offline) }
                } finally { DeviceToolsRuntime.finishHandoff(epoch) }
                assertFalse(command.isActive)
                assertNull(field("server").get(null))
                assertEquals(1, launches)
                assertEquals(offline, journal.get(CommandJournal.key("scope", "first"))!!.has("result"))
                assertEquals(offline, journal.pending("scope", System.currentTimeMillis()).isNotEmpty())
                assertFalse(journal.claim(CommandJournal.key("scope", "first"), "first", "scope", expires, System.currentTimeMillis()))
            }
        }
    }
    @Test fun ordinaryPauseAndExplicitStopStillCancelImmediately() = runBlocking {
        Robolectric.buildActivity(HandoffActivity::class.java).setup().visible().use { controller ->
            controller.windowFocusChanged(true)
            var command = host(controller.get())
            DeviceToolsRuntime.detach(controller.get())
            assertFalse(command.isActive)
            command = host(controller.get())
            DeviceToolsRuntime.launch(intent, System.currentTimeMillis() + 20_000)
            assertTrue(command.isActive)
            DeviceToolsRuntime.stop()
            assertFalse(command.isActive)
            assertNull(field("server").get(null))
        }
    }
}
