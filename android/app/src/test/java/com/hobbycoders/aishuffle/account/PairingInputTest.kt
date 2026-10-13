package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PairingInputTest {
    @Test fun workerApprovalCodesCannotRedirectOrBecomeAccountPairing() {
        assertEquals("ABCDEFGHIJKL", PairingInput.workerCode("aishuffle://worker/link#code=ABCDEFGHIJKL"))
        assertEquals("ABCDEFGHIJKL", PairingInput.workerCode("abcd efgh ijkl"))
        listOf("aishuffle://account/pair#code=ABCDEFGHIJKL",
            "aishuffle://worker/link?hub=attacker#code=ABCDEFGHIJKL",
            "aishuffle://worker/link#code=ABCDEFGHIJKL&hub=attacker",
            "https://attacker/pair#code=ABCDEFGHIJKL", "ABCD1234EFGH").forEach {
            assertThrows(IllegalArgumentException::class.java) { PairingInput.workerCode(it) }
        }
    }
    @Test fun accountQrIsBoundToTheConfiguredService() {
        val hub = "https://hub.example"
        val qr = PairingInput.accountQr("ABCD EFGH IJKL")
        assertFalse(qr.contains(hub))
        assertEquals(PairingInput.Pair(hub, "ABCDEFGHIJKL"), PairingInput.parseAccount(qr, hub))
        assertEquals(hub, PairingInput.parseAccount("$hub/pair#code=ABCDEFGHIJKL", hub).origin)
        listOf("https://other.example/pair#code=ABCDEFGHIJKL", "$qr&hub=https://other.example", "aishuffle://account/pair?hub=other#code=ABCDEFGHIJKL").forEach {
            assertThrows(IllegalArgumentException::class.java) { PairingInput.parseAccount(it, hub) }
        }
    }
    @Test fun normalizesOnlyOriginAndDefaultPort() {
        assertEquals("https://hub.example", PairingInput.origin(" HTTPS://HUB.example:443/ "))
        assertEquals("https://hub.example:8443", PairingInput.origin("https://hub.example:8443"))
    }
    @Test fun rejectsUnsafeOrigins() {
        listOf("http://hub.example", "https://user:password@hub.example", "https://hub.example/path", "https://hub.example?token=secret", "https://hub.example#secret", "https://", "javascript:alert(1)", "https://hub.example:0").forEach {
            assertThrows(IllegalArgumentException::class.java) { PairingInput.origin(it) }
        }
    }
    @Test fun readsShortLivedCodeNotCredentials() {
        val pair = PairingInput.parse("https://hub.example/pair#code=ABCD-EFGH-IJKL")
        assertEquals("ABCDEFGHIJKL", pair.code)
        assertEquals("https://hub.example", pair.origin)
        assertEquals("ABCDEFGHIJKL", PairingInput.code("abcd efgh ijkl"))
    }
    @Test fun directServerAndAccountQrAreNotInterchangeable() {
        assertEquals("ABCDEFGHIJKL", PairingInput.parseDirect("https://server.example/pair#token=ABCDEFGHIJKL").code)
        assertThrows(IllegalArgumentException::class.java) { PairingInput.parse("https://server.example/pair#token=ABCDEFGHIJKL") }
        assertThrows(IllegalArgumentException::class.java) { PairingInput.parseDirect("https://hub.example/pair#code=ABCDEFGHIJKL") }
    }
    @Test fun rejectsAmbiguousAndCredentialQr() {
        listOf("https://hub.example/pair?code=ABCDEFGHIJKL", "https://hub.example/pair#code=ABCDEFGHIJKL&token=secret", "https://hub.example/pair#token=aim_secret", "https://user@hub.example/pair#code=ABCDEFGHIJKL", "http://hub.example/pair#code=ABCDEFGHIJKL").forEach {
            assertThrows(IllegalArgumentException::class.java) { PairingInput.parse(it) }
        }
    }
    @Test fun originBoundaryIncludesSchemeHostAndPort() {
        assertTrue(PairingInput.sameOrigin("https://hub.example", "https://hub.example:443/chat"))
        listOf("https://hub.example.evil/chat", "https://other.example/chat", "https://hub.example:8443/chat", "http://hub.example/chat", "https://user@hub.example/chat", "data:text/html,test").forEach {
            assertFalse(PairingInput.sameOrigin("https://hub.example", it))
        }
    }
    @Test fun legacyServersNeverAcquireAccountIdentity() {
        val legacy = Server.create("Home", "https://shuffle.example", "aim_local")
        val parsed = Server.fromJson(legacy.toJson())
        assertNull(parsed.accountId); assertNull(parsed.hubUrl); assertNull(parsed.hubServerId)
        assertEquals("aim_local", parsed.apiKey)
        val shared = parsed.copy(accountId = "account-a", hubUrl = "https://hub.example", hubServerId = "server-a")
        assertEquals("account-a", Server.fromJson(shared.toJson()).accountId)
        val anotherAccount = shared.copy(accountId = "account-b", hubUrl = "https://other-hub.example")
        val remaining = AccountBoundary.directOnly(listOf(parsed, shared, anotherAccount))
        assertEquals(listOf(parsed), remaining)
    }
    @Test fun expiredAuthDoesNotRestore() {
        val auth = JSONObject().put("access_token", "test-token").put("expires_at", "2020-01-01T00:00:00Z")
        assertThrows(IllegalArgumentException::class.java) { AccountSession.parse("https://hub.example", auth) }
    }
}
