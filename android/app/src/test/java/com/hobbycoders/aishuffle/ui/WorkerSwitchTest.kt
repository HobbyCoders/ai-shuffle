package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.URLEncoder

class WorkerSwitchTest {
    private val base = "https://pc.example"
    private fun worker(id: String, url: String, key: String = "aim_$id", accountId: String? = null) =
        Server(id = id, name = "Worker $id", baseUrl = url, apiKey = key, deviceId = "device-$id", accountId = accountId)
    private val laptop = worker("laptop", "https://laptop.example")
    private val nas = worker("nas", "https://nas.example:8443")
    private val paired = listOf(worker("pc", base), laptop, nas)

    /** What the page builds: `${origin}/mobile/?connect=${encodeURIComponent(target)}`. */
    private fun request(target: String, from: String = base) = "$from/mobile/?connect=" + URLEncoder.encode(target, "UTF-8")

    @Test fun theAppAdvertisesTheCapabilityThePageChecks() {
        assertEquals("worker-switch", WorkerSwitch.CAPABILITY)
        assertTrue(WorkerSwitch.CAPABILITY in MobileUi.CAPABILITIES)
    }

    @Test fun ordinaryPhoneUiNavigationIsNotASwitch() {
        for (url in listOf("$base/mobile/", "$base/mobile/chat/abc?x=1", "$base/mobile/?connected=1", "$base/mobile/?q=connect",
                "https://laptop.example/mobile/?connect=x", "$base/?connect=https%3A%2F%2Flaptop.example", "about:blank", null))
            assertEquals(url.toString(), WorkerSwitch.Decision.NotASwitch, WorkerSwitch.decide(base, url, paired))
    }

    @Test fun aPairedWorkerIsOpenedByExactOrigin() {
        assertEquals(WorkerSwitch.Decision.Open(laptop), WorkerSwitch.decide(base, request("https://laptop.example"), paired))
        assertEquals(WorkerSwitch.Decision.Open(nas), WorkerSwitch.decide(base, request("https://nas.example:8443"), paired))
        // The page sends URL.origin; equivalent spellings normalise to the same origin.
        assertEquals(WorkerSwitch.Decision.Open(laptop), WorkerSwitch.decide(base, request("https://LAPTOP.example:443/"), paired))
        assertEquals(WorkerSwitch.Decision.Open(laptop), WorkerSwitch.decide(base, "$base/mobile?connect=https%3A%2F%2Flaptop.example", paired))
    }

    @Test fun anUnpairedOrLookalikeWorkerIsRefused() {
        for (target in listOf("https://evil.example", "https://laptop.example.evil", "https://nas.example", "https://nas.example:8444",
                "https://sub.laptop.example"))
            assertEquals(target, WorkerSwitch.Decision.Reject(WorkerSwitch.UNKNOWN), WorkerSwitch.decide(base, request(target), paired))
        assertEquals(WorkerSwitch.Decision.Reject(WorkerSwitch.UNKNOWN), WorkerSwitch.decide(base, request("https://laptop.example"), emptyList()))
    }

    @Test fun malformedRequestsAreRefusedWithoutNavigating() {
        val malformed = listOf(
            request("http://laptop.example"),                        // not HTTPS
            request("https://user:pass@laptop.example"),             // credentials
            request("https://laptop.example/mobile/"),               // a path, not an origin
            request("https://laptop.example?token=shc_x"),           // a query
            request("https://laptop.example#frag"),                  // a fragment
            request("laptop.example"),                               // no scheme
            request("https://laptop.example "),                      // whitespace
            "$base/mobile/?connect=",                                // empty
            "$base/mobile/?connect=%E0%A4",                          // not UTF-8
            "$base/mobile/?connect=https%3A%2F%2Flaptop.example&token=shc_x", // extra parameter
            "$base/mobile/?connect=https%3A%2F%2Flaptop.example&connect=https%3A%2F%2Fnas.example%3A8443",
            "$base/mobile/?connect=https%3A%2F%2Flaptop.example#x",  // fragment on the request
            "$base/mobile/chat/abc?connect=https%3A%2F%2Flaptop.example", // not the index
            "$base/mobile/?connect=https%3A%2F%2F" + "a".repeat(600) + ".example",
        )
        for (url in malformed) {
            assertTrue(url, WorkerSwitch.isRequest(base, url))
            assertEquals(url, WorkerSwitch.Decision.Reject(WorkerSwitch.MALFORMED), WorkerSwitch.decide(base, url, paired))
        }
    }

    @Test fun theCurrentWorkerIsAlreadyConnected() {
        assertEquals(WorkerSwitch.Decision.AlreadyConnected, WorkerSwitch.decide(base, request("https://pc.example"), paired))
    }

    @Test fun anAccountWorkerWithoutAKeyIsAuthorizedInWorkersFirst() {
        val registryOnly = worker("phone-less", "https://desk.example", key = "", accountId = "acct")
        assertEquals(WorkerSwitch.Decision.NeedsAuthorization(registryOnly),
            WorkerSwitch.decide(base, request("https://desk.example"), paired + registryOnly))
        // When the same worker is also held with a key, that entry wins.
        val keyed = worker("desk", "https://desk.example")
        assertEquals(WorkerSwitch.Decision.Open(keyed), WorkerSwitch.decide(base, request("https://desk.example"), listOf(registryOnly, keyed)))
    }

    @Test fun mostRecentlyUsedEntryWinsForTheSameOrigin() {
        val again = worker("laptop-2", "https://laptop.example")
        assertEquals(WorkerSwitch.Decision.Open(again), WorkerSwitch.decide(base, request("https://laptop.example"), listOf(again, laptop)))
    }

    @Test fun requestedOriginIsNormalised() {
        assertEquals("https://laptop.example", WorkerSwitch.requestedOrigin(base, request("https://Laptop.Example")))
        assertNull(WorkerSwitch.requestedOrigin(base, request("https://laptop.example", from = "https://laptop.example")))
        assertFalse(WorkerSwitch.isRequest(base, "https://laptop.example/mobile/?connect=https%3A%2F%2Fpc.example"))
    }
}
