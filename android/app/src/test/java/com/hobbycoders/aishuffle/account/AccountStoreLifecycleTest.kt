package com.hobbycoders.aishuffle.account

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.time.Instant
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class AccountStoreLifecycleTest {
    private lateinit var store: AccountStore
    @Before fun setup() {
        store = AccountStore(RuntimeEnvironment.getApplication().getSharedPreferences(UUID.randomUUID().toString(), Context.MODE_PRIVATE))
    }
    private fun session(device: String, account: String = "account-one") = AccountSession(
        "https://hub.example.com", "test-session", Instant.now().plusSeconds(3600).toString(), device, account, "tester"
    )
    @Test fun defaultHubNeedsNoSetupAndCustomHubSurvivesSignOut() {
        assertEquals(AccountStore.DEFAULT_HUB, store.hub)
        store.hub = "https://custom.example.com"
        store.clear()
        assertEquals("https://custom.example.com", store.hub)
    }
    @Test fun signInKeepsTheInstallationIdentityUntilAFreshOneIsNeeded() {
        assertFalse(store.hasInstallIdentity())
        val first = store.beginSignIn()
        assertEquals(first, store.installId)
        assertTrue(store.hasInstallIdentity())
        assertEquals(first, store.beginSignIn())
        assertNotEquals(first, store.beginSignIn(fresh = true))
    }
    @Test fun expiredSessionKeepsIdentityAndProofSoWorkersRenewTheSameEntry() {
        val first = store.beginSignIn()
        val proof = store.installProof("server-one")
        store.save(session(first))
        store.clear()
        assertNull(store.session())
        assertEquals(first, store.beginSignIn())
        assertEquals(proof, store.installProof("server-one"))
    }
    @Test fun logoutAndRevocationCannotReuseTombstonedIdentity() {
        val first = store.beginSignIn()
        val proof = store.installProof("server-one")
        store.save(session(first))
        store.clear(forgetInstall = true)
        assertNull(store.session())
        assertFalse(store.hasInstallIdentity())
        assertNotEquals(first, store.beginSignIn())
        assertNotEquals(proof, store.installProof("server-one"))
    }
    @Test fun reuseIsOffUntilTheServiceAdvertisesItAndSurvivesSignOut() {
        assertFalse(store.hubReusesDevices)
        store.hubReusesDevices = true
        store.clear(forgetInstall = true)
        assertTrue(store.hubReusesDevices)
    }
    @Test fun installProofIsPerWorkerAndNeverTheSecret() {
        store.beginSignIn()
        val one = store.installProof("server-one")
        assertTrue(one.matches(Regex("[0-9a-f]{64}")))
        assertEquals(one, store.installProof("server-one"))
        assertNotEquals(one, store.installProof("server-two"))
        val fresh = store.beginSignIn(fresh = true)
        assertNotEquals(one, store.installProof("server-one"))
        assertEquals(fresh, store.installId)
    }
    @Test fun acceptingSessionPreservesVerifiedIdentityAfterClearingOldAccount() {
        val requested = store.beginSignIn()
        val accepted = session(requested)
        store.clear()
        store.save(accepted)
        assertEquals(requested, store.installId)
        assertEquals(requested, store.session()!!.deviceId)
        assertTrue(store.matches(accepted))
    }
    @Test fun identicalTokenTextNeverMatchesAnotherHubAccountOrDevice() {
        val expected = session(store.beginSignIn())
        store.save(expected)
        assertTrue(store.matches(expected))
        assertFalse(store.matches(AccountSession("https://other.example", expected.token, expected.expiresAt, expected.deviceId, expected.accountId, "tester")))
        assertFalse(store.matches(AccountSession(expected.hub, expected.token, expected.expiresAt, "other-device", expected.accountId, "tester")))
        assertFalse(store.matches(AccountSession(expected.hub, expected.token, expected.expiresAt, expected.deviceId, "other-account", "tester")))
    }
    @Test fun ambiguousLinkResponseCanBeRetriedBySameAccountAfterRelogin() {
        val first = session(store.beginSignIn())
        store.savePendingLink("local-server", first, JSONObject().put("id", "server-one").put("server_secret", "test-only-secret"))
        store.clear()
        val next = session(store.beginSignIn())
        assertEquals("server-one", store.pendingLink("local-server", next)!!.getJSONObject("registration").getString("id"))
        assertNull(store.pendingLink("local-server", session(next.deviceId, "another-account")))
        store.clearPendingLink("local-server")
        assertNull(store.pendingLink("local-server", next))
    }
}
