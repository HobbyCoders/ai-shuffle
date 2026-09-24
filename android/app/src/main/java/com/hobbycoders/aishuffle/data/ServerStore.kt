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
    @Synchronized
    fun getAll(): List<Server> {
        val raw = prefs.getString(KEY_SERVERS, null) ?: return emptyList()
        return runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { Server.fromJson(arr.getJSONObject(it)) }
        }.getOrDefault(emptyList())
    }

    fun getById(id: String): Server? = getAll().firstOrNull { it.id == id }

    /** The worker most recently opened in chat; ServerListActivity tries it first on launch. */
    var lastUsedServerId: String?
        get() = prefs.getString(KEY_LAST_USED, null)
        set(value) { prefs.edit().putString(KEY_LAST_USED, value).apply() }
    fun markUsed(id: String) { lastUsedServerId = id }

    /** [servers] with the most recently used worker first, then insertion order. */
    fun orderedByRecentUse(servers: List<Server>): List<Server> {
        val recent = lastUsedServerId ?: return servers
        return servers.sortedBy { if (it.id == recent) 0 else 1 }
    }

    var selectedToolsServerId: String?
        get() = prefs.getString("selected_tools_server", null)
        set(value) { prefs.edit().putString("selected_tools_server", value).commit() }
    fun selectedToolsServer(): Server? = selectedToolsServerId?.let(::getById)?.takeUnless { it.needsAccountAuthorization }

    /** Adds a server (or replaces one with the same id). */
    @Synchronized
    fun add(server: Server) {
        val updated = getAll().filterNot { it.id == server.id } + server
        save(updated)
    }

    /** Replaces an existing server (matched by id). No-op if not present. */
    @Synchronized
    fun update(server: Server) {
        val current = getAll()
        if (current.none { it.id == server.id }) return
        save(current.map { if (it.id == server.id) server else it })
    }

    @Synchronized
    fun remove(id: String) {
        save(getAll().filterNot { it.id == id })
        if (lastUsedServerId == id) lastUsedServerId = null
        if (selectedToolsServerId == id) selectedToolsServerId = null
    }

    @Synchronized
    fun removeAccountServers() { save(com.hobbycoders.aishuffle.account.AccountBoundary.directOnly(getAll())) }

    @Synchronized
    fun replaceAccountServers(servers: List<Server>) {
        val saved = getAll()
        // Registry requests may have started before a renewal. Metadata from
        // that stale snapshot must not overwrite a durably rotated key.
        val current = servers.map { listed ->
            val existing = saved.firstOrNull { com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameIdentity(it, listed) }
            if (existing != null && existing.apiKey.isNotBlank()) listed.copy(apiKey = existing.apiKey, keyEpoch = existing.keyEpoch) else listed
        }
        save(com.hobbycoders.aishuffle.account.AccountBoundary.directOnly(saved) + current)
    }

    @Synchronized
    internal fun replaceKey(expected: Server, key: String, renewal: Boolean = false): Server? {
        val saved = getAll()
        val current = saved.firstOrNull { it.id == expected.id } ?: return null
        if (!com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameIdentity(current, expected) || current.apiKey != expected.apiKey)
            return null
        val epoch = if (!renewal) null else if (key == current.apiKey) current.keyEpoch else current.authorizationEpoch
        val replacement = current.copy(apiKey = key, keyEpoch = epoch)
        if (save(saved.map { if (it.id == current.id) replacement else it })) return replacement
        // SharedPreferences updates memory even when disk commit fails. Restore
        // the previous snapshot so a retry does not treat that key as durable.
        save(saved)
        return null
    }

    private fun save(servers: List<Server>): Boolean {
        val arr = JSONArray()
        servers.forEach { arr.put(it.toJson()) }
        return prefs.edit().putString(KEY_SERVERS, arr.toString()).commit()
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
        private const val KEY_LAST_USED = "last_used_server"

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
            return ServerStore(prefs).also { store ->
                com.hobbycoders.aishuffle.net.ApiClient.installCredentialStore(store::getById) { expected, key ->
                    store.replaceKey(expected, key, renewal = true)
                }
            }
        }
    }
}
