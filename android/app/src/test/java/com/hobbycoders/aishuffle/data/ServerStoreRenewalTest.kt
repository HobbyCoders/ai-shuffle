package com.hobbycoders.aishuffle.data

import android.content.Context
import android.content.SharedPreferences
import com.hobbycoders.aishuffle.model.Server
import java.lang.reflect.Proxy
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ServerStoreRenewalTest {
    private val original = Server("worker", "Worker", "https://worker.example", "aim_original", "phone",
        accountId = "account", hubUrl = "https://hub.example", hubServerId = "worker-identity")
    private lateinit var prefs: SharedPreferences
    private lateinit var store: ServerStore

    private fun fromPreferences(preferences: SharedPreferences): ServerStore =
        ServerStore::class.java.getDeclaredConstructor(SharedPreferences::class.java).let {
            it.isAccessible = true
            it.newInstance(preferences)
        }

    @Test fun pendingFeedbackUsesTheSamePhoneIdAsTheEnrollmentPayloadNotTheErrorBody() {
        val ownId = "30000000-0000-4000-8000-000000000001"
        val unrelatedId = "90000000-0000-4000-8000-000000000009"
        val account = com.hobbycoders.aishuffle.account.AccountSession("https://hub.example", "fixture-token", "2099-01-01T00:00:00Z", ownId, "account", "Owner")
        // These are the actual registry-record and enrollment-payload producers
        // used together by AccountRepository.sync(), not an ID parsed from an error.
        val server = com.hobbycoders.aishuffle.account.AccountWorkerCache.listed(emptyList(), account,
            "worker-identity", "Untrusted display name", "https://worker.example")
        val deviceInfo = com.hobbycoders.aishuffle.account.HubClient.device(account.deviceId)
        assertEquals(deviceInfo.getString("device_id"), server.deviceId)
        val body = org.json.JSONObject().put("detail", "Confirm this device on the worker before connecting")
            .put("device_id", unrelatedId).put("device_name", "Injected label").toString()
        val renewal = com.hobbycoders.aishuffle.net.ApiClient.parseKeyRenewal(server, 403, "application/json", body)
            as com.hobbycoders.aishuffle.net.ApiResult.Failure
        val reservation = com.hobbycoders.aishuffle.net.ApiClient.parseWebReservation(403, "application/json", body, deviceId = server.deviceId)
            as com.hobbycoders.aishuffle.net.ApiResult.Failure
        for (message in listOf(renewal.message, reservation.message)) {
            assertTrue(message.contains("Phone device ID: $ownId"))
            assertTrue(message.contains("matching name is not proof"))
            assertFalse(message.contains(unrelatedId))
            assertFalse(message.contains("Injected label"))
            assertFalse(message.contains(server.name))
            assertFalse(message.contains(account.token))
        }
        val rawError = org.json.JSONObject().put("detail", "Approve $unrelatedId instead").toString()
        assertFalse(com.hobbycoders.aishuffle.net.ApiClient.errorMessage(403, rawError, ownId).contains(unrelatedId))
        assertFalse(com.hobbycoders.aishuffle.net.ApiClient.errorMessage(403, body, "injected\nID").contains("injected"))
    }

    @Before fun setup() {
        prefs = RuntimeEnvironment.getApplication().getSharedPreferences("renewal-test", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        store = fromPreferences(prefs)
        store.add(original)
    }

    @Test fun committedReplacementSurvivesReloadAndStaleRegistrySnapshots() {
        val rotated = store.replaceKey(original, "aim_replacement", renewal = true)!!
        assertEquals(rotated, fromPreferences(prefs).getById(original.id))
        assertTrue(com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameAuthorization(original, rotated))
        assertNull(store.replaceKey(original, "aim_stale-response"))
        store.replaceAccountServers(listOf(original.copy(name = "Registry rename")))
        assertEquals("Registry rename", store.getById(original.id)!!.name)
        assertEquals("aim_replacement", store.getById(original.id)!!.apiKey)
        assertTrue(com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameAuthorization(original, store.getById(original.id)))
        val repaired = store.replaceKey(store.getById(original.id)!!, "aim_explicit-repair")!!
        assertFalse(com.hobbycoders.aishuffle.net.DeviceKeyRenewal.sameAuthorization(original, repaired))
    }

    @Test fun registryCannotMoveOrResurrectStoredCredentialsAfterRepairOrRemoval() {
        val repaired = store.replaceKey(original, "aim_explicit-repair")!!
        val stale = original.copy(name = "Registry rename", baseUrl = "https://untrusted.example:8443")
        val current = store.replaceAccountServers(listOf(stale)).single()
        assertEquals(repaired.copy(name = stale.name), current)
        assertEquals(current, fromPreferences(prefs).getById(original.id))
        store.remove(original.id)
        val waiting = store.replaceAccountServers(listOf(stale)).single()
        assertTrue(waiting.needsAccountAuthorization)
        assertEquals("", waiting.apiKey)
        assertNull(waiting.keyEpoch)
    }

    @Test fun failedCommitDoesNotAuthorizeUsingTheReplacement() {
        var allow = false
        val broken = Proxy.newProxyInstance(SharedPreferences::class.java.classLoader,
            arrayOf(SharedPreferences::class.java)) { _, method, args ->
            if (method.name != "edit") method.invoke(prefs, *(args ?: emptyArray())) else {
                val editor = prefs.edit()
                Proxy.newProxyInstance(SharedPreferences.Editor::class.java.classLoader,
                    arrayOf(SharedPreferences.Editor::class.java)) { proxy, call, arguments ->
                    if (call.name == "commit" && !allow) false else {
                        val result = call.invoke(editor, *(arguments ?: emptyArray()))
                        if (result is SharedPreferences.Editor) proxy else result
                    }
                }
            }
        } as SharedPreferences
        val failing = fromPreferences(broken)
        assertNull(failing.replaceKey(original, "aim_replacement"))
        assertEquals(original, fromPreferences(prefs).getById(original.id))
        assertThrows(IllegalStateException::class.java) { failing.replaceAccountServers(emptyList()) }
        assertEquals(original, fromPreferences(prefs).getById(original.id))
        allow = true
        assertEquals("aim_replacement", failing.replaceKey(original, "aim_replacement")!!.apiKey)
    }

    @Test fun removalAccountChangeAndEndpointChangeFenceTheWrite() {
        store.remove(original.id)
        assertNull(store.replaceKey(original, "aim_late-response"))
        assertTrue(store.getAll().isEmpty())
        for (changed in listOf(original.copy(accountId = "other"),
            original.copy(baseUrl = "https://other.example"), original.copy(deviceId = "other"))) {
            store.add(changed)
            assertNull(store.replaceKey(original, "aim_late-response"))
            assertEquals(changed, store.getById(original.id))
        }
    }
}
