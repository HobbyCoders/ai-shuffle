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

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class VoiceLifecycleTest {
    @Test fun deliberateStartDefaultsToChatAndCarriesSelectedServer() {
        val app: Application = RuntimeEnvironment.getApplication()
        VoiceCallService.start(app, "selected-server")
        val started = shadowOf(app).nextStartedService
        assertEquals(VoiceCallService.MODE_CHAT, started.getStringExtra(VoiceCallService.EXTRA_MODE))
        assertEquals("selected-server", started.getStringExtra(VoiceCallService.EXTRA_SERVER_ID))
    }
    @Test fun handoffCarriesExactExistingSession() {
        val app: Application = RuntimeEnvironment.getApplication()
        VoiceCallService.start(app, "selected-server", SessionMode.CHAT, "existing-session")
        val started = shadowOf(app).nextStartedService
        assertEquals("existing-session", started.getStringExtra(VoiceCallService.EXTRA_SESSION_ID))
        assertEquals("selected-server", started.getStringExtra(VoiceCallService.EXTRA_SERVER_ID))
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
