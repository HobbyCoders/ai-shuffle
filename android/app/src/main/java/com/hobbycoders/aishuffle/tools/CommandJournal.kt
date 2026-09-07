package com.hobbycoders.aishuffle.tools

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONObject
import java.security.MessageDigest
import java.time.Instant

/** Metadata and results only: never persists command arguments. A claim is durable BEFORE execution. */
class CommandJournal internal constructor(private val prefs: android.content.SharedPreferences) {
    constructor(context: Context) : this(EncryptedSharedPreferences.create(context, "tool_command_journal",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM))
    companion object {
        const val MAX_LIFETIME = 300_000L
        const val RETENTION = 86_400_000L
        const val MAX_ENTRIES = 512
        fun key(scope: String, id: String): String = MessageDigest.getInstance("SHA-256")
            .digest("$scope\u0000$id".toByteArray()).joinToString("") { "%02x".format(it) }
        fun expiry(raw: Any?): Long = when (raw) {
            is String -> runCatching { Instant.parse(raw).toEpochMilli() }.getOrDefault(0)
            is Number -> raw.toLong().let { if (it < 10_000_000_000L) it * 1000 else it }
            else -> 0
        }
        fun validExpiry(expires: Long, now: Long) = expires > now && expires - now <= MAX_LIFETIME
    }
    fun get(key: String): JSONObject? = prefs.getString(key, null)?.let { JSONObject(it) }
    fun claim(key: String, id: String, scope: String, expires: Long, now: Long): Boolean {
        if (get(key) != null) return false
        val edit = prefs.edit()
        val retained = prefs.all.filter { (_, value) ->
            val keep = runCatching { JSONObject(value as String).getLong("retain_until") > now }.getOrDefault(true)
            keep
        }
        (prefs.all.keys - retained.keys).forEach { edit.remove(it) }
        if (retained.size >= MAX_ENTRIES) { edit.commit(); return false }
        val entry = JSONObject().put("id", id).put("scope", scope).put("expires", expires)
            .put("retain_until", now + RETENTION).put("posted", false)
            .put("result", ToolRegistry.error("interrupted", "Execution was interrupted after a durable claim. It will not be repeated; verify the device before requesting another action."))
        check(edit.putString(key, entry.toString()).commit()) { "Could not persist command claim." }
        return true
    }
    fun finish(key: String, result: JSONObject) {
        val entry = get(key) ?: return
        entry.put("result", result)
        check(prefs.edit().putString(key, entry.toString()).commit()) { "Could not persist result." }
    }
    fun posted(key: String) {
        val entry = get(key) ?: return
        // Erase potentially sensitive screen/location/message output after delivery.
        entry.put("posted", true).remove("result")
        check(prefs.edit().putString(key, entry.toString()).commit())
    }
    fun pending(scope: String, now: Long): List<Pair<String, JSONObject>> {
        val edit = prefs.edit()
        val pending = prefs.all.mapNotNull { (key, raw) ->
            val entry = runCatching { JSONObject(raw as String) }.getOrNull() ?: return@mapNotNull null
            if (entry.optLong("retain_until") <= now) { edit.remove(key); return@mapNotNull null }
            if (entry.optLong("expires") + MAX_LIFETIME <= now) {
                entry.put("posted", true).remove("result")
                edit.putString(key, entry.toString())
                return@mapNotNull null
            }
            if (entry.optString("scope") == scope && !entry.optBoolean("posted")) key to entry else null
        }
        check(edit.commit())
        return pending
    }
}
