package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class DeviceKeyRenewalTest {
    private val original = Server("fixture", "Worker", "https://worker.example", "aim_" + "a".repeat(43), "device")
    private val replacement = "aim_" + "b".repeat(43)
    private fun response(key: String = replacement, rotated: Boolean = true) = JSONObject()
        .put("api_key", key).put("device_id", original.deviceId).put("rotated", rotated)
        .put("expires_at", "2030-04-01T00:00:00Z").toString()

    @Test fun onlyRenewalPreservesTheAuthorizationEpochAcrossSerialization() {
        val rotated = original.copy(apiKey = replacement, keyEpoch = original.authorizationEpoch)
        val reloaded = Server.fromJson(JSONObject(rotated.toJson().toString()))
        assertTrue(DeviceKeyRenewal.sameAuthorization(original, reloaded))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, original.copy(apiKey = replacement)))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, rotated.copy(keyEpoch = null)))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, rotated.copy(accountId = "other")))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, rotated.copy(cfAccessClientSecret = "other")))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, rotated.copy(baseUrl = "https://other.example")))
        assertFalse(DeviceKeyRenewal.sameAuthorization(original, null))
    }

    @Test fun requestCarriesOnlyTheSavedWorkerKeyAndAccessHeaders() {
        val request = ApiClient.keyRenewalRequest(original.copy(cfAccessClientId = "fixture-cf", cfAccessClientSecret = "fixture-secret"))
        assertEquals("https://worker.example/api/v1/mobile/key/renew", request.url.toString())
        assertEquals("POST", request.method)
        assertEquals("Bearer ${original.apiKey}", request.header("Authorization"))
        assertEquals("no-store", request.header("Cache-Control"))
        assertNull(request.header("Cookie"))
        assertEquals("fixture-cf", request.header("CF-Access-Client-Id"))
        assertThrows(IllegalArgumentException::class.java) { ApiClient.keyRenewalRequest(original.copy(baseUrl = "https://worker.example/elsewhere")) }
    }

    @Test fun validatesIdentityTypesAndCompatibilityWithoutDowngradingDenials() {
        val parse = { code: Int, type: String, raw: String -> ApiClient.parseKeyRenewal(original, code, type, raw) }
        val key = (parse(200, "application/json", response()) as ApiResult.Success).data
        assertEquals(replacement, key.value)
        assertFalse(key.toString().contains(replacement))
        assertTrue(parse(200, "application/json", response(original.apiKey, false)) is ApiResult.Success)
        for (bad in listOf(JSONObject(response()).put("device_id", "other"),
            JSONObject(response()).put("api_key", "shc_wrong"), JSONObject(response()).put("rotated", "true"),
            JSONObject(response()).put("rotated", false), JSONObject(response()).put("expires_at", "tomorrow")))
            assertTrue(parse(200, "application/json", bad.toString()) is ApiResult.Failure)
        assertTrue(parse(200, "text/html", response()) is ApiResult.Failure)
        assertTrue(parse(200, "application/json", " ".repeat(8193)) is ApiResult.Failure)
        for ((code, detail) in listOf(404 to "Not Found", 405 to "Method Not Allowed"))
            assertEquals(original.apiKey, (parse(code, "application/json", JSONObject().put("detail", detail).toString()) as ApiResult.Success).data.value)
        for (code in listOf(301, 302, 401, 403, 409, 410, 500, 503))
            assertTrue(parse(code, "application/json", "{\"detail\":\"Not Found\"}") is ApiResult.Failure)
        assertTrue(parse(404, "text/html", "<html>Not Found</html>") is ApiResult.Failure)
    }

    @Test fun lostResponseRecoversSameReplacementAndUsesDurableKeyForBothProducers() = runBlocking {
        var saved: Server? = original
        var calls = 0
        val gate = CompletableDeferred<Unit>()
        val started = CompletableDeferred<Unit>()
        val manager = DeviceKeyRenewal({ saved }, { expected, key ->
            assertEquals(saved, expected)
            expected.copy(apiKey = key).also { saved = it }
        }, { request ->
            assertEquals(original.apiKey, request.apiKey)
            calls++
            if (calls == 1) { started.complete(Unit); gate.await(); ApiResult.Failure("Response lost") }
            else ApiResult.Success(RenewedDeviceKey(replacement))
        }, { 0L })
        val web = async { manager.prepare(original) }
        started.await()
        val native = async { manager.prepare(original) }
        gate.complete(Unit)
        val webServer = (web.await() as ApiResult.Success).data
        val nativeServer = (native.await() as ApiResult.Success).data
        assertEquals(2, calls)
        assertEquals(saved, webServer)
        assertEquals(saved, nativeServer)
        assertEquals("Bearer $replacement", ApiClient.webSessionRequest(webServer).header("Authorization"))
        assertEquals("Bearer $replacement", ApiClient.webSessionRequest(nativeServer, ApiClient.SessionPurpose.NATIVE).header("Authorization"))
    }

    @Test fun failedPersistenceNeverUsesReplacementAndRetryStartsFromSavedKey() = runBlocking {
        var saved = original
        var persist = false
        var calls = 0
        val manager = DeviceKeyRenewal({ saved }, { expected, key ->
            if (persist) expected.copy(apiKey = key).also { saved = it } else null
        }, { request ->
            assertEquals(original.apiKey, request.apiKey)
            calls++
            ApiResult.Success(RenewedDeviceKey(replacement))
        })
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        assertEquals(original, saved)
        persist = true
        assertEquals(replacement, (manager.prepare(original) as ApiResult.Success).data.apiKey)
        assertEquals(2, calls)
    }

    @Test fun checksAgainWhileActiveAndNeverResurrectsRemovedOrChangedAccount() = runBlocking {
        var saved: Server? = original
        var clock = 0L
        var calls = 0
        val manager = DeviceKeyRenewal({ saved }, { expected, key ->
            if (saved == expected) expected.copy(apiKey = key).also { saved = it } else null
        }, { current -> calls++; ApiResult.Success(RenewedDeviceKey(current.apiKey)) }, { clock })
        assertTrue(manager.prepare(original) is ApiResult.Success)
        clock++
        assertTrue(manager.prepare(original) is ApiResult.Success)
        assertEquals(1, calls)
        clock += DeviceKeyRenewal.CHECK_INTERVAL_MS
        assertTrue(manager.prepare(original) is ApiResult.Success)
        assertEquals(2, calls)
        saved = null
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        saved = original.copy(accountId = "another-account")
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        saved = original.copy(baseUrl = "https://other.example")
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        assertEquals(2, calls)
    }

    @Test fun accountChangeDuringExchangeDiscardsReplacement() = runBlocking {
        var saved: Server? = original
        val manager = DeviceKeyRenewal({ saved }, { expected, key ->
            if (saved == expected) expected.copy(apiKey = key).also { saved = it } else null
        }, { saved = null; ApiResult.Success(RenewedDeviceKey(replacement)) })
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        assertNull(saved)
    }

    @Test fun authorizationFailuresKeepCredentialAndDoNotRetryOrCache() = runBlocking {
        for (code in listOf(401, 403, 409)) {
            var calls = 0
            val manager = DeviceKeyRenewal({ original }, { _, _ -> fail("Must not persist a rejected credential"); null },
                { calls++; ApiResult.Failure("Rejected", code) })
            assertTrue(manager.prepare(original) is ApiResult.Failure)
            assertEquals(1, calls)
            assertTrue(manager.prepare(original) is ApiResult.Failure)
            assertEquals(2, calls)
        }
    }

    @Test fun outagesHaveOneRetryAndFeedbackDistinguishesPendingFromDenied() = runBlocking {
        var calls = 0
        val manager = DeviceKeyRenewal({ original }, { _, _ -> fail("Must not persist an outage"); null },
            { calls++; ApiResult.Failure("Unavailable", 503) })
        assertTrue(manager.prepare(original) is ApiResult.Failure)
        assertEquals(2, calls)
        val pending = ApiClient.errorMessage(403, JSONObject().put("detail", "Confirm this device on the worker before connecting").toString())
        val denied = ApiClient.errorMessage(403, JSONObject().put("detail", "This device was denied by the worker owner").toString())
        assertTrue(pending.contains("Waiting for worker confirmation"))
        assertTrue(denied.contains("denied"))
        assertNotEquals(pending, denied)
        assertFalse(ApiClient.errorMessage(403, "<secret-from-proxy>").contains("secret-from-proxy"))
    }
}
