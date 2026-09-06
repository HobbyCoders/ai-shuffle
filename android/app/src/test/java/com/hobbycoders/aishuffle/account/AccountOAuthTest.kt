package com.hobbycoders.aishuffle.account

import android.content.Context
import android.content.Intent
import android.net.Uri
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import net.openid.appauth.AuthorizationServiceConfiguration
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class AccountOAuthTest {
    private lateinit var store: AccountStore
    private val now = 1_000_000L
    @Before fun setup() {
        store = AccountStore(RuntimeEnvironment.getApplication().getSharedPreferences(UUID.randomUUID().toString(), Context.MODE_PRIVATE))
    }
    private fun arm(created: Long = now) {
        store.pendingOAuth = JSONObject().put("request_id", "pending-request").put("state", "expected-state")
            .put("nonce", "expected-nonce").put("client_id", "expected-client")
            .put("verifier", "v".repeat(43)).put("created_at", created).toString()
    }
    private fun response(state: String = "expected-state", nonce: String = "expected-nonce",
                         client: String = "expected-client", redirect: String = AccountOAuth.REDIRECT, code: String? = "returned-code"): Intent {
        val request = AuthorizationRequest.Builder(AuthorizationServiceConfiguration(
            Uri.parse("https://hub.example/connect"), Uri.parse("https://identity.example/oauth/token")),
            client, "code", Uri.parse(redirect)).setNonce(nonce).setState("expected-state").build()
        return AuthorizationResponse.Builder(request).setState(state).setAuthorizationCode(code).build().toIntent()
    }
    @Test fun verifiedResponseUsesStoredProofAndCanOnlyBeConsumedOnce() {
        arm()
        val body = AccountOAuth.consumeResponse(store, response(), now)
        assertEquals("v".repeat(43), body.getString("code_verifier"))
        assertEquals("pending-request", body.getString("request_id"))
        assertEquals("returned-code", body.getString("code"))
        assertNull(store.pendingOAuth)
        assertThrows(IllegalStateException::class.java) { AccountOAuth.consumeResponse(store, response(), now) }
    }
    @Test fun rejectsMismatchedStateClientNonceAndRedirectAndClearsPendingProof() {
        for (intent in listOf(response(state = "other"), response(nonce = "other"),
            response(client = "other"), response(redirect = "other.app://oauth/account"))) {
            arm()
            assertThrows(IllegalArgumentException::class.java) { AccountOAuth.consumeResponse(store, intent, now) }
            assertNull(store.pendingOAuth)
        }
    }
    @Test fun rejectsExpiredFutureCancelledAndIncompleteResponses() {
        for (created in listOf(now - 600001, now + 1)) {
            arm(created)
            assertThrows(IllegalArgumentException::class.java) { AccountOAuth.consumeResponse(store, response(), now) }
            assertNull(store.pendingOAuth)
        }
        for (intent in listOf(Intent(), response(code = null))) {
            arm()
            assertThrows(IllegalStateException::class.java) { AccountOAuth.consumeResponse(store, intent, now) }
            assertNull(store.pendingOAuth)
        }
    }
}
