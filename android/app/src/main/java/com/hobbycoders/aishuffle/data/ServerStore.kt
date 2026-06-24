package com.hobbycoders.aishuffle.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.hobbycoders.aishuffle.model.Server
import org.json.JSONArray

/**
 * Persists the list of connected [Server]s in [EncryptedSharedPreferences],
 * backed by a Keystore master key. The list is serialized to a JSON array under
 * a single key.
 *
 * Also stores the globally-known UnifiedPush endpoint so that newly-added
 * servers can be registered with the same endpoint.
 */
class ServerStore private constructor(private val prefs: SharedPreferences) {

    /** Returns all saved servers, in insertion order. */
    fun getAll(): List<Server> {
        val raw = prefs.getString(KEY_SERVERS, null) ?: return emptyList()
        return runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { Server.fromJson(arr.getJSONObject(it)) }
        }.getOrDefault(emptyList())
    }

    fun getById(id: String): Server? = getAll().firstOrNull { it.id == id }

    /** Adds a server (or replaces one with the same id). */
    fun add(server: Server) {
        val updated = getAll().filterNot { it.id == server.id } + server
        save(updated)
    }

    /** Replaces an existing server (matched by id). No-op if not present. */
    fun update(server: Server) {
        val current = getAll()
        if (current.none { it.id == server.id }) return
        save(current.map { if (it.id == server.id) server else it })
    }

    fun remove(id: String) {
        save(getAll().filterNot { it.id == id })
    }

    private fun save(servers: List<Server>) {
        val arr = JSONArray()
        servers.forEach { arr.put(it.toJson()) }
        prefs.edit().putString(KEY_SERVERS, arr.toString()).apply()
    }

    /** The last UnifiedPush endpoint the distributor handed us, if any. */
    var unifiedPushEndpoint: String?
        get() = prefs.getString(KEY_UP_ENDPOINT, null)
        set(value) {
            prefs.edit().apply {
                if (value == null) remove(KEY_UP_ENDPOINT) else putString(KEY_UP_ENDPOINT, value)
            }.apply()
        }

    companion object {
        private const val PREFS_NAME = "ai_shuffle_secure_prefs"
        private const val KEY_SERVERS = "servers"
        private const val KEY_UP_ENDPOINT = "unifiedpush_endpoint"

        @Volatile
        private var instance: ServerStore? = null

        fun get(context: Context): ServerStore {
            return instance ?: synchronized(this) {
                instance ?: build(context.applicationContext).also { instance = it }
            }
        }

        private fun build(appContext: Context): ServerStore {
            val masterKey = MasterKey.Builder(appContext)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            val prefs = EncryptedSharedPreferences.create(
                appContext,
                PREFS_NAME,
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
            return ServerStore(prefs)
        }
    }
}
