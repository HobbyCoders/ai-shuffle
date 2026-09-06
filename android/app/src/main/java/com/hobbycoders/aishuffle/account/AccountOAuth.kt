package com.hobbycoders.aishuffle.account

import android.content.Intent
import android.net.Uri
import net.openid.appauth.AuthorizationException
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import net.openid.appauth.AuthorizationServiceConfiguration
import net.openid.appauth.CodeVerifierUtil
import net.openid.appauth.ResponseTypeValues
import org.json.JSONObject

/** Browser OAuth with AppAuth's state/PKCE handling and encrypted pending state. */
object AccountOAuth {
    const val REDIRECT = "com.hobbycoders.aishuffle://oauth/account"
    internal enum class FailureCategory { CANCELLED, PROVIDER_REJECTED, STATE_MISMATCH, INVALID_RESPONSE }
    internal class SignInFailure(val category: FailureCategory, message: String) : IllegalStateException(message)
    private val providerErrors = setOf("invalid_request", "unauthorized_client", "access_denied",
        "unsupported_response_type", "invalid_scope", "server_error", "temporarily_unavailable",
        "invalid_client", "login_required", "interaction_required", "consent_required", "account_selection_required")

    private fun invalidResponse() = SignInFailure(FailureCategory.INVALID_RESPONSE,
        "The browser did not return a valid sign-in response. Start again.")

    private fun failure(error: AuthorizationException): SignInFailure = when {
        error.type == AuthorizationException.TYPE_GENERAL_ERROR &&
            error.code == AuthorizationException.GeneralErrors.USER_CANCELED_AUTH_FLOW.code ->
            SignInFailure(FailureCategory.CANCELLED, "Sign-in cancelled. You can try again when you’re ready.")
        error.type == AuthorizationException.TYPE_GENERAL_ERROR &&
            error.code == AuthorizationException.AuthorizationRequestErrors.STATE_MISMATCH.code ->
            SignInFailure(FailureCategory.STATE_MISMATCH, "The sign-in response did not match this request. Start again.")
        error.type == AuthorizationException.TYPE_OAUTH_AUTHORIZATION_ERROR -> {
            // Provider error strings, descriptions, URIs and causes are untrusted.
            // Only fixed protocol categories may reach UI or exception output.
            val category = error.error?.takeIf { it in providerErrors } ?: "provider_error"
            SignInFailure(FailureCategory.PROVIDER_REJECTED,
                "The account service rejected sign-in ($category). Start again.")
        }
        else -> invalidResponse()
    }

    suspend fun begin(store: AccountStore, createAccount: Boolean = false): AuthorizationRequest {
        store.pendingOAuth = null
        val verifier = CodeVerifierUtil.generateRandomCodeVerifier()
        val start = HubClient.call(AccountStore.DEFAULT_HUB, "/v1/oauth/start", "POST",
            HubClient.device(store.beginSignIn()).put("code_challenge", CodeVerifierUtil.deriveCodeVerifierChallenge(verifier)))
        val issuer = PairingInput.origin(start.getString("issuer"))
        require(start.getString("authorization_endpoint") == "$issuer/oauth/authorize" &&
            start.getString("token_endpoint") == "$issuer/oauth/token" && start.getString("redirect_uri") == REDIRECT)
        val request = AuthorizationRequest.Builder(
            AuthorizationServiceConfiguration(Uri.parse("${AccountStore.DEFAULT_HUB}/connect"), Uri.parse("$issuer/oauth/token")),
            start.getString("client_id"), ResponseTypeValues.CODE, Uri.parse(REDIRECT)
        ).setScopes("openid", "profile", "email")
            .setNonce(start.getString("nonce")).setCodeVerifier(verifier)
            .setAdditionalParameters(mapOf("request_id" to start.getString("request_id"), "screen" to if (createAccount) "signup" else "signin")).build()
        store.pendingOAuth = JSONObject().put("request_id", start.getString("request_id"))
            .put("state", request.state).put("verifier", verifier).put("nonce", request.nonce)
            .put("client_id", request.clientId).put("created_at", System.currentTimeMillis()).toString()
        return request
    }
    suspend fun finish(store: AccountStore, data: Intent): JSONObject =
        HubClient.call(AccountStore.DEFAULT_HUB, "/v1/oauth/exchange", "POST", consumeResponse(store, data))

    internal fun consumeResponse(store: AccountStore, data: Intent, now: Long = System.currentTimeMillis()): JSONObject {
        val pending = store.pendingOAuth?.let(::JSONObject) ?: error("Sign-in expired. Start again.")
        store.pendingOAuth = null
        // AppAuth returns RESULT_OK for both successful and rejected redirects.
        // Read the exception first; never exchange a code from a rejected result.
        val authError = try { AuthorizationException.fromIntent(data) }
            catch (_: RuntimeException) { throw invalidResponse() }
        if (authError != null) throw failure(authError)
        val response = try { AuthorizationResponse.fromIntent(data) }
            catch (_: RuntimeException) { throw invalidResponse() }
            ?: throw invalidResponse()
        val age = now - pending.getLong("created_at")
        require(age in 0..600000 && response.state == pending.getString("state") &&
            response.request.clientId == pending.getString("client_id") &&
            response.request.nonce == pending.getString("nonce") && response.request.redirectUri.toString() == REDIRECT) {
            "Sign-in could not be verified. Start again."
        }
        return JSONObject()
            .put("request_id", pending.getString("request_id"))
            .put("code_verifier", pending.getString("verifier"))
            .put("code", response.authorizationCode ?: error("Sign-in did not return an authorization code."))
    }
}
