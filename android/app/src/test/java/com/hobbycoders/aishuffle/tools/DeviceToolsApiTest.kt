package com.hobbycoders.aishuffle.tools

import org.junit.Assert.*
import org.junit.Test

class DeviceToolsApiTest {
    @Test fun resultRejectionsRetainTheirHttpStatus() {
        for (status in listOf(404, 409, 410, 429, 500)) {
            try {
                DeviceToolsApi.checkStatus("command/result", status)
                fail("HTTP $status must not be acknowledged")
            } catch (failure: ToolsHttpFailure) {
                assertEquals(status, failure.statusCode)
            }
        }
    }
    @Test fun missingEndpointsAndRevocationKeepTheirExistingMeaning() {
        for (path in listOf("pending", "capabilities")) {
            try { DeviceToolsApi.checkStatus(path, 404); fail("Unsupported worker") }
            catch (_: ToolsUnsupported) { }
        }
        for (status in listOf(401, 403)) {
            try { DeviceToolsApi.checkStatus("command/result", status); fail("Authorization revoked") }
            catch (_: ToolsAuthorizationRevoked) { }
        }
        DeviceToolsApi.checkStatus("command/result", 200)
    }
}
