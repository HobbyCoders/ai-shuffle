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
    @Test fun everyExplicitSignInGetsFreshAuthorizationIdentity() {
        val first = store.beginSignIn()
        assertEquals(first, store.installId)
        assertNotEquals(first, store.beginSignIn())
    }
    @Test fun logoutAndRevocationCannotReuseTombstonedIdentity() {
        val first = store.beginSignIn()
        store.save(session(first))
        store.clear()
        assertNull(store.session())
        assertNotEquals(first, store.beginSignIn())
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
