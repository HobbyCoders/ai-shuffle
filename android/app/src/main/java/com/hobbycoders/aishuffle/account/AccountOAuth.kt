package com.hobbycoders.aishuffle.account

import android.content.Intent
import android.net.Uri
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import net.openid.appauth.AuthorizationServiceConfiguration
import net.openid.appauth.CodeVerifierUtil
import net.openid.appauth.ResponseTypeValues
import org.json.JSONObject

/** Browser OAuth with AppAuth's state/PKCE handling and encrypted pending state. */
object AccountOAuth {
    const val REDIRECT = "com.hobbycoders.aishuffle://oauth/account"
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
        val response = AuthorizationResponse.fromIntent(data) ?: error("Sign-in was cancelled or could not be completed.")
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
