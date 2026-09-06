package com.hobbycoders.aishuffle.account

import android.content.Context
import android.content.Intent
import android.net.Uri
import net.openid.appauth.AuthorizationException
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

    @Test fun reportsProviderRejectionWithoutExposingRedirectOrDescription() {
        arm()
        val redirect = Uri.parse(AccountOAuth.REDIRECT + "?error=invalid_request" +
            "&error_description=private-description&error_uri=https%3A%2F%2Fprivate.example%2Fsecret" +
            "&state=private-state&code=private-code")
        val intent = AuthorizationException.fromOAuthRedirect(redirect).toIntent().setData(redirect)
        val failure = assertThrows(AccountOAuth.SignInFailure::class.java) {
            AccountOAuth.consumeResponse(store, intent, now)
        }
        assertEquals(AccountOAuth.FailureCategory.PROVIDER_REJECTED, failure.category)
        assertEquals("The account service rejected sign-in (invalid_request). Start again.", failure.message)
        assertNull(failure.cause)
        assertNull(store.pendingOAuth)
        assertThrows(IllegalStateException::class.java) { AccountOAuth.consumeResponse(store, response(), now) }
    }

    @Test fun distinguishesUserCancellationFromStateMismatch() {
        val cases = listOf(
            AuthorizationException.GeneralErrors.USER_CANCELED_AUTH_FLOW to AccountOAuth.FailureCategory.CANCELLED,
            AuthorizationException.AuthorizationRequestErrors.STATE_MISMATCH to AccountOAuth.FailureCategory.STATE_MISMATCH
        )
        val messages = mutableSetOf<String?>()
        for ((template, category) in cases) {
            arm()
            val failure = assertThrows(AccountOAuth.SignInFailure::class.java) {
                AccountOAuth.consumeResponse(store, template.toIntent(), now)
            }
            assertEquals(category, failure.category)
            messages.add(failure.message)
            assertNull(failure.cause)
            assertNull(store.pendingOAuth)
        }
        assertEquals(2, messages.size)
    }

    @Test fun unknownProviderErrorsAndMalformedExceptionsCannotLeakPrivateValues() {
        val privateError = AuthorizationException(AuthorizationException.TYPE_OAUTH_AUTHORIZATION_ERROR,
            AuthorizationException.AuthorizationRequestErrors.OTHER.code, "private-code-in-error",
            "private-description", Uri.parse("https://private.example/secret"), RuntimeException("private-cause"))
        for (intent in listOf(privateError.toIntent(),
            Intent().putExtra(AuthorizationException.EXTRA_EXCEPTION, "private-malformed-json"))) {
            arm()
            val failure = assertThrows(AccountOAuth.SignInFailure::class.java) {
                AccountOAuth.consumeResponse(store, intent, now)
            }
            assertFalse(failure.toString().contains("private"))
            assertNull(failure.cause)
            assertNull(store.pendingOAuth)
        }
    }

    @Test fun exceptionTakesPrecedenceOverAnAttachedAuthorizationCode() {
        arm()
        val intent = response().putExtras(AuthorizationException.AuthorizationRequestErrors.ACCESS_DENIED.toIntent())
        val failure = assertThrows(AccountOAuth.SignInFailure::class.java) {
            AccountOAuth.consumeResponse(store, intent, now)
        }
        assertEquals(AccountOAuth.FailureCategory.PROVIDER_REJECTED, failure.category)
        assertNull(store.pendingOAuth)
    }
}
