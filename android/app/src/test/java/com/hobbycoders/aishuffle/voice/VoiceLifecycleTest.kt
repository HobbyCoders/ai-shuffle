package com.hobbycoders.aishuffle.voice

import android.app.Application
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import android.content.Context
import android.content.SharedPreferences
import android.content.Intent
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleRegistry
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import org.robolectric.Robolectric
import org.robolectric.shadows.ShadowLooper

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class VoiceLifecycleTest {
    @Test fun deliberateStartCarriesSelectedServer() {
        val app: Application = RuntimeEnvironment.getApplication()
        VoiceCallService.start(app, "selected-server")
        val started = shadowOf(app).nextStartedService
        assertEquals("selected-server", started.getStringExtra(VoiceCallService.EXTRA_SERVER_ID))
    }
    @Test fun handoffCarriesExactExistingSession() {
        val app: Application = RuntimeEnvironment.getApplication()
        VoiceCallService.start(app, "selected-server", sessionId = "existing-session")
        val started = shadowOf(app).nextStartedService
        assertEquals("existing-session", started.getStringExtra(VoiceCallService.EXTRA_SESSION_ID))
        assertEquals("selected-server", started.getStringExtra(VoiceCallService.EXTRA_SERVER_ID))
    }
    @Test fun viewingAnActiveCallCannotRestartItAfterBackgroundFailure() {
        val app: Application = RuntimeEnvironment.getApplication()
        val instance = ServerStore::class.java.getDeclaredField("instance").apply { isAccessible = true }
        val previous = instance.get(null)
        val constructor = ServerStore::class.java.getDeclaredConstructor(SharedPreferences::class.java).apply { isAccessible = true }
        val prefs = app.getSharedPreferences("voice-lifecycle-fixture", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        val store = constructor.newInstance(prefs)
        store.add(Server("fixture-worker", "Fixture", "https://worker.example", "aim_fixture", "fixture-device"))
        instance.set(null, store)
        shadowOf(app).grantPermissions(android.Manifest.permission.RECORD_AUDIO)
        VoiceCallState.update { it.copy(status = CallStatus.LIVE, serverId = "fixture-worker", sessionId = "fixture-session") }
        val intent = Intent(app, VoiceCallActivity::class.java)
            .putExtra(VoiceCallService.EXTRA_SERVER_ID, "fixture-worker")
            .putExtra(VoiceCallService.EXTRA_SESSION_ID, "fixture-session")
        val controller = Robolectric.buildActivity(VoiceCallActivity::class.java, intent).create()
        try {
            // Drive the registered lifecycle observer without calling onResume's
            // unrelated device-tools attachment; no network or media is opened.
            val lifecycle = controller.get().lifecycle as LifecycleRegistry
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_START)
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
            ShadowLooper.idleMainLooper()
            assertNull(shadowOf(app).nextStartedService)
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_PAUSE)
            VoiceCallState.update { it.copy(status = CallStatus.ERROR, error = "Fixture network loss") }
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_RESUME)
            ShadowLooper.idleMainLooper()
            assertNull("Resuming an already-viewed failed call must not start another service", shadowOf(app).nextStartedService)
            assertEquals(CallStatus.ERROR, VoiceCallState.state.value.status)
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_PAUSE)
            lifecycle.handleLifecycleEvent(Lifecycle.Event.ON_STOP)
        } finally {
            controller.destroy()
            VoiceCallState.reset()
            instance.set(null, previous)
        }
    }
    @Test fun phoneAdapterRoutesSharedToolsAndRetainsEndSessionProtocol() = runBlocking {
        val tools = PhoneTools(RuntimeEnvironment.getApplication())
        assertTrue(tools.owns("place_call"))
        assertTrue(tools.owns("create_reminder"))
        assertFalse(tools.owns("read_messages"))
        val result = tools.execute("end_session", JSONObject())
        assertTrue(result.endSession)
        assertTrue(result.result.getBoolean("ok"))
    }
    @Test fun microphoneDenialDoesNotCreateAConnectionAndDestroyEndsState() {
        val app: Application = RuntimeEnvironment.getApplication()
        shadowOf(app).denyPermissions(android.Manifest.permission.RECORD_AUDIO)
        VoiceCallState.reset()
        val controller = org.robolectric.Robolectric.buildService(VoiceCallService::class.java).create()
        controller.get().onStartCommand(android.content.Intent().putExtra(VoiceCallService.EXTRA_SERVER_ID, "selected"), 0, 1)
        assertEquals(CallStatus.ERROR, VoiceCallState.state.value.status)
        assertFalse(VoiceCallState.isActive)
        controller.destroy()
    }
    @Test fun runtimeStateCarriesSelectedServerAndClearsOnReset() {
        VoiceCallState.update { it.copy(status = CallStatus.LIVE, serverId = "selected", muted = true) }
        assertTrue(VoiceCallState.isActive)
        assertEquals("selected", VoiceCallState.state.value.serverId)
        VoiceCallState.reset()
        assertFalse(VoiceCallState.isActive)
        assertNull(VoiceCallState.state.value.serverId)
        assertFalse(VoiceCallState.state.value.muted)
    }
}
