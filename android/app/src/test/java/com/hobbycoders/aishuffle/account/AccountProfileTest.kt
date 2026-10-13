package com.hobbycoders.aishuffle.account

import android.content.Context
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.IOException
import java.time.Instant
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class AccountProfileTest {
    private lateinit var store: AccountStore
    private lateinit var initial: AccountSession
    @Before fun setup() {
        store = AccountStore(RuntimeEnvironment.getApplication().getSharedPreferences(UUID.randomUUID().toString(), Context.MODE_PRIVATE))
        initial = AccountSession(AccountStore.DEFAULT_HUB, "existing-session", Instant.now().plusSeconds(3600).toString(),
            "existing-device", "existing-account", "generated-name")
        store.save(initial)
    }
    private fun response(name: String = "chosen.name", complete: Boolean = true, account: String = initial.accountId,
                         device: String = initial.deviceId) = JSONObject().put("device_id", device)
        .put("account", JSONObject().put("id", account).put("username", name).put("profile_complete", complete))

    @Test fun profileFlagsDistinguishNewExistingAndOlderStoredSessions() {
        assertNull(AccountSession.parse(initial.hub, initial.json()).profileComplete)
        for (complete in listOf(true, false)) {
            val json = initial.json().apply { getJSONObject("account").put("profile_complete", complete) }
            store.save(AccountSession.parse(initial.hub, json))
            assertEquals(complete, store.session()!!.profileComplete)
        }
        val malformed = initial.json().apply { getJSONObject("account").put("profile_complete", "false") }
        assertThrows(IllegalStateException::class.java) { AccountSession.parse(initial.hub, malformed) }
    }
    @Test fun refreshAndRenameKeepIdentityCredentialsExpiryAndPendingWorkerLinks() = runBlocking {
        store.savePendingLink("local-worker", initial, JSONObject().put("id", "worker-one"))
        val api = AccountProfile(store) { session, method, body ->
            assertEquals(initial.token, session.token)
            if (method == "GET") { assertNull(body); response("generated-name", false) }
            else { assertEquals(setOf("username"), body!!.keys().asSequence().toSet()); response(body.getString("username")) }
        }
        assertFalse(api.refresh().profileComplete!!)
        val updated = api.saveUsername("  Chosen.Name  ")
        assertEquals("chosen.name", updated.username)
        assertTrue(updated.profileComplete!!)
        assertEquals(initial.token, updated.token)
        assertEquals(initial.deviceId, updated.deviceId)
        assertEquals(initial.accountId, updated.accountId)
        assertEquals(initial.expiresAt, updated.expiresAt)
        assertEquals(initial.hub, updated.hub)
        assertEquals(initial.deviceId, store.installId)
        assertNotNull(store.pendingLink("local-worker", updated))
    }
    @Test fun invalidUsernamesNeverCallTheService() = runBlocking {
        var calls = 0
        val api = AccountProfile(store) { _, _, _ -> calls++; response() }
        for (name in listOf("", "ab", "-abc", "a b", "a@b", "éloise", "a".repeat(65))) {
            assertTrue(runCatching { api.saveUsername(name) }.exceptionOrNull() is IllegalArgumentException)
        }
        assertEquals(0, calls)
        assertEquals(initial.username, store.session()!!.username)
    }
    @Test fun unavailableNameAndRemoteErrorsKeepCurrentProfileAndAllowRetry() = runBlocking {
        var attempts = 0
        val api = AccountProfile(store) { _, _, body ->
            when (attempts++) { 0 -> throw HubFailure(409, "untrusted-provider-detail"); 1 -> throw IOException("secret-token") }
            response(body!!.getString("username"))
        }
        assertEquals("That username is already taken. Choose another.", runCatching { api.saveUsername("chosen.name") }.exceptionOrNull()!!.message)
        assertEquals(initial.username, store.session()!!.username)
        val remote = runCatching { api.saveUsername("chosen.name") }.exceptionOrNull()!!
        assertEquals("Could not save your username. Check your connection and try again.", remote.message)
        assertNull(remote.cause)
        assertEquals(initial.username, store.session()!!.username)
        assertEquals("chosen.name", api.saveUsername("chosen.name").username)
    }
    @Test fun invalidOrCrossAccountResponsesCannotOverwriteSessionMetadata() = runBlocking {
        for (answer in listOf(response(account = "someone-else"), response(device = "other-device"), response(complete = false),
            response(name = "not-the-requested-name"), JSONObject())) {
            val api = AccountProfile(store) { _, _, _ -> answer }
            assertTrue(runCatching { api.saveUsername("chosen.name") }.exceptionOrNull() is IllegalArgumentException)
            assertEquals(initial.username, store.session()!!.username)
            assertEquals(initial.token, store.session()!!.token)
        }
    }
    @Test fun staleResponseCannotRestoreASignedOutOrReplacedSession() = runBlocking {
        val api = AccountProfile(store) { _, _, _ -> store.clear(); response() }
        assertTrue(runCatching { api.refresh() }.exceptionOrNull() is IllegalStateException)
        assertNull(store.session())
        store.save(initial)
        val replacement = AccountSession(initial.hub, "new-token", initial.expiresAt, "new-device", "new-account", "new-user", true)
        val other = AccountProfile(store) { _, _, _ -> store.save(replacement); response() }
        assertTrue(runCatching { other.refresh() }.exceptionOrNull() is IllegalStateException)
        assertEquals(replacement.token, store.session()!!.token)
        assertEquals(replacement.username, store.session()!!.username)
    }
    @Test fun unauthorizedSessionsAndCancellationRemainRecognizable() = runBlocking {
        for (error in listOf(HubFailure(401, "Expired"), CancellationException("Stopped"))) {
            val api = AccountProfile(store) { _, _, _ -> throw error }
            assertSame(error, runCatching { api.refresh() }.exceptionOrNull())
            assertEquals(initial.token, store.session()!!.token)
        }
    }
}
