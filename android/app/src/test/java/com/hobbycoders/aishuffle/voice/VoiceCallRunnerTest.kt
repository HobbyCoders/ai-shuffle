package com.hobbycoders.aishuffle.voice

import com.hobbycoders.aishuffle.net.VoiceApi
import com.hobbycoders.aishuffle.net.VoiceSession
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class VoiceCallRunnerTest {
    private class Peer : VoicePeer {
        override val mediaState = MutableStateFlow(VoiceMediaState.CONNECTING)
        var automaticReady = true
        var closed = 0
        override suspend fun createOffer() = "test-offer"
        override suspend fun acceptAnswer(answerSdp: String) { if (automaticReady) mediaState.value = VoiceMediaState.READY }
        override fun setMicEnabled(enabled: Boolean) {}
        override fun send(event: JSONObject) {}
        override fun close() { closed++; mediaState.value = VoiceMediaState.CLOSED }
    }
    private class Backend : VoiceBackend {
        val opened = mutableListOf<String?>()
        var returnedSession = "selected-session"
        var callId: String? = "test-call"
        var attachFailure = false
        var refreshFailure = false
        var accessFailure = false
        var attachGate: CompletableDeferred<Unit>? = null
        var exchangeGate: CompletableDeferred<Unit>? = null
        var attached = 0
        override suspend fun open(sessionId: String?): VoiceSession {
            opened += sessionId
            if (refreshFailure && opened.size > 1) error("Call reached its length cap (HTTP 409)")
            return VoiceSession(returnedSession, "fake-secret", "fake-model")
        }
        override suspend fun exchange(session: VoiceSession, offer: String): Pair<String, String?> {
            exchangeGate?.await()
            return "test-answer" to callId
        }
        override suspend fun attach(sessionId: String, callId: String) {
            attached++
            attachGate?.await()
            if (attachFailure) error("Attachment refused")
        }
        override suspend fun checkAccess() { if (accessFailure) error("Worker authorization revoked") }
    }
    private class Fixture {
        val backend = Backend()
        val peers = mutableListOf<Peer>()
        val statuses = mutableListOf<CallStatus>()
        val sessions = mutableListOf<String>()
        var authorized = true
        var automaticReady = true
        fun runner(refresh: Long = 60_000, grace: Long = 1_000, access: Long = 60_000) = VoiceCallRunner(
            backend, { Peer().also { it.automaticReady = automaticReady; peers += it } },
            { authorized }, { sessions += it }, {}, { statuses += it },
            setupTimeoutMs = 2_000, disconnectGraceMs = grace, refreshAfterMs = refresh, accessCheckMs = access,
        )
    }
    private suspend fun until(check: () -> Boolean) = withTimeout(3_000) { while (!check()) delay(1) }

    @Test fun liveRequiresMediaAndSuccessfulAttachment() = runBlocking {
        val f = Fixture(); f.automaticReady = false; f.backend.attachGate = CompletableDeferred()
        val job = launch { f.runner().run("selected-session") }
        until { f.peers.isNotEmpty() }
        assertFalse(f.statuses.contains(CallStatus.LIVE)); assertEquals(0, f.backend.attached)
        f.peers.single().mediaState.value = VoiceMediaState.READY
        until { f.backend.attached == 1 }
        assertFalse(f.statuses.contains(CallStatus.LIVE))
        f.backend.attachGate!!.complete(Unit)
        until { f.statuses.last() == CallStatus.LIVE }
        job.cancelAndJoin(); assertEquals(1, f.peers.single().closed)
    }
    @Test fun missingCallIdentifierNeverShowsLiveAndClosesMedia() = runBlocking {
        val f = Fixture(); f.backend.callId = null
        assertTrue(runCatching { f.runner().run(null) }.isFailure)
        assertFalse(f.statuses.contains(CallStatus.LIVE)); assertEquals(0, f.backend.attached)
        assertEquals(1, f.peers.single().closed)
    }
    @Test fun failedAttachmentNeverShowsLive() = runBlocking {
        val f = Fixture(); f.backend.attachFailure = true
        assertTrue(runCatching { f.runner().run(null) }.isFailure)
        assertFalse(f.statuses.contains(CallStatus.LIVE)); assertEquals(1, f.peers.single().closed)
    }
    @Test fun selectedSessionCannotBeReplacedByAnotherSession() = runBlocking {
        val f = Fixture(); f.backend.returnedSession = "different"
        assertTrue(runCatching { f.runner().run("selected-session") }.isFailure)
        assertEquals(listOf("selected-session"), f.backend.opened)
        assertTrue(f.peers.isEmpty())
    }
    @Test fun revocationWhileOpeningCannotProduceALiveCall() = runBlocking {
        val f = Fixture(); f.backend.exchangeGate = CompletableDeferred()
        val result = async { runCatching { f.runner().run(null) } }
        until { f.peers.isNotEmpty() }
        f.authorized = false; f.backend.exchangeGate!!.complete(Unit)
        assertTrue(result.await().isFailure)
        assertFalse(f.statuses.contains(CallStatus.LIVE)); assertEquals(1, f.peers.single().closed)
    }
    @Test fun transientDisconnectRecoversWithoutReplacingMedia() = runBlocking {
        val f = Fixture(); val job = launch { f.runner().run(null) }
        until { f.statuses.lastOrNull() == CallStatus.LIVE }
        val peer = f.peers.single(); peer.mediaState.value = VoiceMediaState.DISCONNECTED
        until { f.statuses.last() == CallStatus.RECONNECTING }
        peer.mediaState.value = VoiceMediaState.CONNECTING
        delay(5)
        assertTrue(job.isActive); assertEquals(0, peer.closed)
        peer.mediaState.value = VoiceMediaState.READY
        until { f.statuses.last() == CallStatus.LIVE }
        assertEquals(1, f.backend.opened.size); assertEquals(0, peer.closed)
        job.cancelAndJoin(); assertEquals(1, peer.closed)
    }
    @Test fun expiredDisconnectGraceClosesMedia() = runBlocking {
        val f = Fixture(); val result = async { runCatching { f.runner(grace = 20).run(null) } }
        until { f.statuses.lastOrNull() == CallStatus.LIVE }
        f.peers.single().mediaState.value = VoiceMediaState.DISCONNECTED
        assertTrue(withTimeout(3_000) { result.await() }.isFailure)
        assertEquals(1, f.peers.single().closed)
    }
    @Test fun refreshUsesExactSessionAndDoesNotRetryCallCap() = runBlocking {
        val f = Fixture(); f.backend.refreshFailure = true
        assertTrue(runCatching { f.runner(refresh = 20).run("selected-session") }.isFailure)
        assertEquals(listOf("selected-session", "selected-session"), f.backend.opened)
        assertEquals(1, f.peers.single().closed)
        assertTrue(f.statuses.contains(CallStatus.RECONNECTING))
    }
    @Test fun cancellationDuringHandshakeClosesItsPeer() = runBlocking {
        val f = Fixture(); f.backend.exchangeGate = CompletableDeferred()
        val job = launch { f.runner().run(null) }
        until { f.peers.isNotEmpty() }
        job.cancelAndJoin()
        f.backend.exchangeGate!!.complete(Unit)
        assertEquals(1, f.peers.single().closed); assertFalse(f.statuses.contains(CallStatus.LIVE))
    }
    @Test fun periodicWorkerAuthorizationFailureClosesBackgroundCall() = runBlocking {
        val f = Fixture(); f.backend.accessFailure = true
        val result = runCatching { f.runner(access = 20).run(null) }
        assertTrue(result.isFailure); assertEquals(1, f.peers.single().closed)
    }
    @Test fun invalidHandoffIsNotConvertedIntoANewSession() = runBlocking {
        val f = Fixture()
        assertTrue(runCatching { f.runner().run("../another/session") }.isFailure)
        assertTrue(f.backend.opened.isEmpty())
    }
    @Test fun refreshRequestUsesExactSessionAndNativeCookieOnly() {
        val server = Server("worker", "Worker", "https://worker.example", "aim_test", "device")
        val request = VoiceApi.refreshRequest(server, "native-test", "selected-session")
        assertEquals("/api/v1/voice/sessions/selected-session/secret", request.url.encodedPath)
        assertEquals("POST", request.method)
        assertEquals("session=native-test", request.header("Cookie"))
        assertNull(request.header("Authorization"))
        assertEquals("https://worker.example", request.header("Origin"))
    }
}
