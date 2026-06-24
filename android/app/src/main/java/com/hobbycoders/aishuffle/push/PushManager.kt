package com.hobbycoders.aishuffle.push

import android.content.Context
import android.util.Log
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.net.ApiClient
import com.hobbycoders.aishuffle.net.ApiResult
import org.unifiedpush.android.connector.UnifiedPush

/**
 * Coordinates UnifiedPush distributor selection and endpoint propagation.
 *
 * - [ensureRegistered] is called on startup once at least one server exists. It
 *   picks a distributor (first one if several) and asks it to register; if no
 *   distributor is installed it reports that gracefully and the app keeps working.
 * - [onEndpointReceived] persists a freshly minted endpoint and registers it with
 *   every saved server.
 */
object PushManager {

    private const val TAG = "PushManager"

    /** Outcome of [ensureRegistered] so the UI can show a hint when needed. */
    sealed class RegisterOutcome {
        /** A distributor was found and registration was requested. */
        object Requested : RegisterOutcome()

        /** No UnifiedPush distributor is installed. */
        object NoDistributor : RegisterOutcome()
    }

    /**
     * Ensure the app is registered with a UnifiedPush distributor.
     *
     * Safe to call repeatedly. Never throws; returns an outcome describing what
     * happened so the caller can surface a one-time "install a distributor" hint.
     */
    fun ensureRegistered(context: Context): RegisterOutcome {
        return try {
            val distributors = UnifiedPush.getDistributors(context)
            if (distributors.isEmpty()) {
                Log.i(TAG, "No UnifiedPush distributor installed")
                return RegisterOutcome.NoDistributor
            }

            // Honour an existing user choice; otherwise pick the first available.
            val saved = UnifiedPush.getSavedDistributor(context)
            val chosen = saved?.takeIf { distributors.contains(it) } ?: distributors.first()
            if (saved != chosen) {
                UnifiedPush.saveDistributor(context, chosen)
            }
            UnifiedPush.registerApp(context)
            Log.i(TAG, "Requested UnifiedPush registration via $chosen")
            RegisterOutcome.Requested
        } catch (e: Exception) {
            Log.w(TAG, "UnifiedPush registration failed", e)
            RegisterOutcome.NoDistributor
        }
    }

    /**
     * Persist [endpoint] and register it with every saved server. Called from the
     * messaging receiver's onNewEndpoint.
     */
    suspend fun onEndpointReceived(context: Context, endpoint: String) {
        val store = ServerStore.get(context)
        store.unifiedPushEndpoint = endpoint
        registerEndpointWithAllServers(context, endpoint)
    }

    /**
     * Register the currently known endpoint with a single server (used right after
     * adding a new server). No-op if no endpoint is known yet.
     */
    suspend fun registerKnownEndpointWith(context: Context, serverId: String) {
        val store = ServerStore.get(context)
        val endpoint = store.unifiedPushEndpoint ?: return
        val server = store.getById(serverId) ?: return
        when (val result = ApiClient.registerUnifiedPush(server, endpoint)) {
            is ApiResult.Success ->
                store.update(server.copy(unifiedPushEndpoint = endpoint))
            is ApiResult.Failure ->
                Log.w(TAG, "Endpoint registration failed for ${server.name}: ${result.message}")
        }
    }

    private suspend fun registerEndpointWithAllServers(context: Context, endpoint: String) {
        val store = ServerStore.get(context)
        for (server in store.getAll()) {
            when (val result = ApiClient.registerUnifiedPush(server, endpoint)) {
                is ApiResult.Success ->
                    store.update(server.copy(unifiedPushEndpoint = endpoint))
                is ApiResult.Failure ->
                    Log.w(TAG, "Endpoint registration failed for ${server.name}: ${result.message}")
            }
        }
    }
}
