package com.hobbycoders.aishuffle.tools

import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject

/** One queue iteration, shared by foreground and cross-app hosts. Claims survive retries. */
internal class DeviceCommandPoller(
    private val journal: CommandJournal,
    private val scopeId: String,
    private val request: suspend (String, JSONObject?) -> JSONObject,
    private val canExecute: () -> Boolean,
    private val execute: suspend (String, JSONObject, Long) -> JSONObject,
    private val queueFull: () -> Unit = {},
) {
    private suspend fun postResult(key: String, id: String, result: JSONObject) {
        try {
            val response = request("$id/result", JSONObject().put("result", ToolResultBounds.fit(result)))
            check(response.optBoolean("ok"))
        } catch (failure: ToolsAuthorizationRevoked) {
            // This result is final too, but authorization loss must still stop this worker.
            withContext(NonCancellable + Dispatchers.IO) { journal.posted(key) }
            throw failure
        } catch (failure: ToolsHttpFailure) {
            // Definitive per-command rejection: erase the output, keep the claim
            // tombstone (never execute again), and continue this poll iteration.
            if (failure.statusCode !in 400..499 || failure.statusCode in setOf(408, 425, 429)) throw failure
        }
        withContext(NonCancellable + Dispatchers.IO) { journal.posted(key) }
    }

    suspend fun pollOnce(enabled: Boolean) {
        // Deliver durable results first; a network failure never repeats execution.
        for ((key, entry) in withContext(Dispatchers.IO) { journal.pending(scopeId, System.currentTimeMillis()) }) {
            postResult(key, entry.getString("id"), entry.getJSONObject("result"))
        }
        val commands = request("pending", null).optJSONArray("commands") ?: JSONArray()
        for (i in 0 until minOf(commands.length(), 32)) {
            currentCoroutineContext().ensureActive()
            val command = commands.optJSONObject(i) ?: continue
            val id = command.optString("id")
            if (!id.matches(Regex("[A-Za-z0-9_-]{1,128}"))) continue
            val key = CommandJournal.key(scopeId, id)
            if (withContext(Dispatchers.IO) { journal.get(key) } != null) continue
            val now = System.currentTimeMillis()
            val expires = CommandJournal.expiry(command.opt("expires_at"))
            // Claim even expired/unsupported/disabled requests so they receive one factual result.
            val claimed = withContext(Dispatchers.IO) { journal.claim(key, id, scopeId, expires.coerceAtLeast(now), now) }
            if (!claimed) { queueFull(); continue }
            val result = ToolResultBounds.fit(when {
                !canExecute() -> ToolRegistry.error("foreground_required", "Open AI Shuffle and retry; no action was performed.")
                !CommandJournal.validExpiry(expires, now) -> ToolRegistry.error("expired", "Command expired or has an invalid expiry (maximum lifetime: five minutes).")
                !enabled -> ToolRegistry.error("disabled", "Android tools are disabled.")
                command.optJSONObject("arguments") == null -> ToolRegistry.error("invalid_arguments", "arguments must be an object.")
                else -> withTimeoutOrNull((expires - now).coerceAtMost(30_000)) {
                    execute(command.optString("name"), command.getJSONObject("arguments"), expires)
                } ?: ToolRegistry.error("expired", "Execution deadline elapsed. Verify device state; this command will not be repeated.")
            })
            withContext(NonCancellable + Dispatchers.IO) { journal.finish(key, result) }
            currentCoroutineContext().ensureActive()
            postResult(key, id, result)
        }
    }
}
