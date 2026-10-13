package com.hobbycoders.aishuffle.account

import kotlinx.coroutines.CancellationException
import org.json.JSONObject
import java.util.Locale

/** Profile updates preserve the current session/device and never change worker credentials. */
class AccountProfile(
    private val store: AccountStore,
    private val request: suspend (AccountSession, String, JSONObject?) -> JSONObject = { session, method, body ->
        HubClient.call(session.hub, "/v1/me", method, body, session.token)
    }
) {
    companion object {
        const val USERNAME_HINT = "Use 3–64 letters, numbers, periods, underscores or hyphens. Start with a letter or number."
        fun username(value: String): String = value.trim().lowercase(Locale.ROOT).also {
            require(it.matches(Regex("[a-z0-9][a-z0-9_.-]{2,63}"))) { USERNAME_HINT }
        }
    }

    suspend fun refresh(): AccountSession = update("GET", null)
    suspend fun saveUsername(value: String): AccountSession = update("PATCH", JSONObject().put("username", username(value)))

    private suspend fun update(method: String, body: JSONObject?): AccountSession {
        val session = store.session() ?: error("Sign in to update your account.")
        val response = try { request(session, method, body) }
        catch (e: CancellationException) { throw e }
        catch (e: Exception) {
            if (e is HubFailure && e.status == 401) throw e
            throw IllegalStateException(when {
                e is HubFailure && e.status == 409 && method == "PATCH" -> "That username is already taken. Choose another."
                e is HubFailure && e.status == 400 && method == "PATCH" -> USERNAME_HINT
                e is HubFailure && e.status == 429 -> "Too many attempts. Wait a few minutes, then try again."
                method == "PATCH" -> "Could not save your username. Check your connection and try again."
                else -> "Could not load your account. Check your connection and try again."
            })
        }
        val account = response.optJSONObject("account")
        val name = account?.opt("username") as? String
        val complete = account?.opt("profile_complete") as? Boolean
        require(account?.optString("id") == session.accountId && response.optString("device_id") == session.deviceId &&
            !name.isNullOrBlank() && complete != null &&
            (method != "PATCH" || (complete && name == body?.getString("username")))) { "The service returned an invalid account profile. Try again." }
        return store.updateProfile(session, name!!, complete!!)
    }
}
