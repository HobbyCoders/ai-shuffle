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
    class ApprovalHost : NativeScreen() {
        var launches = 0
        override fun startActivity(intent: Intent) { super.startActivity(intent); launches++ }
    }
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
    private fun pumpUntil(condition: () -> Boolean) {
        val until = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5)
        while (!condition() && System.nanoTime() < until) {
            org.robolectric.shadows.ShadowLooper.idleMainLooper()
            Thread.sleep(5)
        }
        assertTrue("Main-thread operation did not settle", condition())
    }
    @Test fun immediatePostBeforePauseBlocksTheBatchedCalendarEditor() = exerciseUnpausedHandoff(false)
    @Test fun laterCommandRunsAfterAHandoffThatNeverPausedTheHost() = exerciseUnpausedHandoff(true)
    private fun exerciseUnpausedHandoff(runLaterCommand: Boolean) = runBlocking {
        Robolectric.buildActivity(ApprovalHost::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().visible().use { controller ->
            controller.windowFocusChanged(true)
            host(controller.get())
            val epoch = field("generation").getLong(null)
            val expires = System.currentTimeMillis() + 20_000
            var commands = JSONArray()
                .put(JSONObject().put("id", "alarm").put("name", "set_alarm")
                    .put("arguments", JSONObject().put("hour", 7).put("minute", 0)).put("expires_at", expires))
                .put(JSONObject().put("id", "calendar").put("name", "create_calendar_event")
                    .put("arguments", JSONObject().put("title", "Test").put("start_time", "2030-01-01T10:00:00Z"))
                    .put("expires_at", expires))
            val journal = CommandJournal(app.getSharedPreferences("post-before-pause", Context.MODE_PRIVATE))
            val posted = mutableListOf<JSONObject>()
            val request: suspend (String, JSONObject?) -> JSONObject = { path, body ->
                if (path == "pending") JSONObject().put("commands", commands) else {
                    assertTrue("This reproduction must POST before any lifecycle pause", DeviceToolsRuntime.hasAttachedActivity())
                    posted.add(body!!.getJSONObject("result"))
                    JSONObject().put("ok", true) // Immediate POST, no held response and no synchronous detach.
                }
            }
            val executor = DeviceToolExecutor(app)
            // Do not copy poller construction, its canExecute lambda or its finally boundary here.
            val work = CoroutineScope(Dispatchers.Main.immediate).async {
                DeviceToolsRuntime.pollCommands(journal, "scope", epoch, true, executor, request)
            }
            try {
                pumpUntil { ToolApprovals.pending.value != null }
                val dialog = org.robolectric.shadows.ShadowDialog.getLatestDialog() as androidx.appcompat.app.AlertDialog
                val info = org.robolectric.util.ReflectionHelpers.getField<Any>(dialog.window!!.decorView, "mAttachInfo")
                org.robolectric.util.ReflectionHelpers.setField(info, "mHasWindowFocus", true)
                dialog.getButton(androidx.appcompat.app.AlertDialog.BUTTON_POSITIVE).performClick()
                controller.windowFocusChanged(true)
                pumpUntil { work.isCompleted }
                work.await()
                assertEquals("Only the alarm UI may launch before onPause", 1, controller.get().launches)
                assertEquals(listOf("launched_confirmation", "foreground_required"), posted.map { it.getString("status") })
                if (runLaterCommand) {
                    assertTrue(DeviceToolsRuntime.hasAttachedActivity())
                    commands = JSONArray().put(JSONObject().put("id", "later-calendar").put("name", "create_calendar_event")
                        .put("arguments", JSONObject().put("title", "Later").put("start_time", "2030-01-01T11:00:00Z"))
                        .put("expires_at", expires))
                    val later = CoroutineScope(Dispatchers.Main.immediate).async {
                        DeviceToolsRuntime.pollCommands(journal, "scope", epoch, true, executor, request)
                    }
                    try {
                        pumpUntil { later.isCompleted }
                        later.await()
                        assertEquals("An unpaused handoff must not block a later poll", 2, controller.get().launches)
                        assertEquals("launched_confirmation", posted.last().getString("status"))
                    } finally { later.cancel() }
                }
            } finally { work.cancel() }
        }
    }
    @Test fun backgroundingLocationConsentIsNotAUserDenial() = runBlocking {
        org.robolectric.Shadows.shadowOf(app).grantPermissions(android.Manifest.permission.ACCESS_COARSE_LOCATION)
        Robolectric.buildActivity(ApprovalHost::class.java).also {
            it.get().setTheme(R.style.Theme_AIShuffle)
        }.setup().visible().use { controller ->
            controller.windowFocusChanged(true)
            host(controller.get())
            val result = CoroutineScope(Dispatchers.Main.immediate).async {
                DeviceToolExecutor(app).execute("get_location", JSONObject())
            }
            try {
                pumpUntil { ToolApprovals.pending.value != null }
                controller.pause()
                pumpUntil { result.isCompleted }
                assertEquals("foreground_required", result.await().getString("status"))
            } finally { result.cancel() }
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
