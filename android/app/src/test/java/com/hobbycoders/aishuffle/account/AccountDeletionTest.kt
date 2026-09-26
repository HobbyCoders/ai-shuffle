package com.hobbycoders.aishuffle.account

import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class AccountDeletionTest {
    @Test fun publicPageIsTheHubDeletionPage() {
        assertEquals("https://hobbycoders.com/delete-account", AccountDeletion.PUBLIC_URL)
        assertEquals("https://hub.example/delete-account", AccountDeletion.pageFor("https://HUB.example/"))
        assertEquals("https://hub.example:8443/delete-account", AccountDeletion.pageFor("https://hub.example:8443"))
    }

    @Test fun onlyAnHttpsOriginCanBecomeTheDeletionPage() {
        listOf("http://hub.example", "https://hub.example/other", "https://user@hub.example", "https://hub.example?x=1", "javascript:alert(1)").forEach {
            assertThrows(IllegalArgumentException::class.java) { AccountDeletion.pageFor(it) }
        }
    }

    @Test fun revokedAccessMeansTheAccountIsGoneFromThisPhone() = runBlocking {
        assertEquals(AccountDeletion.Result.STILL_ACTIVE, AccountDeletion.check { })
        assertEquals(AccountDeletion.Result.ACCESS_ENDED, AccountDeletion.check { throw HubFailure(401, "revoked") })
        assertEquals(AccountDeletion.Result.ACCESS_ENDED, AccountDeletion.check { throw HubFailure(403, "revoked") })
    }

    @Test fun networkAndServerErrorsAreNotTakenAsDeletion() {
        for (status in listOf(0, 429, 500, 503)) {
            val error = assertThrows(HubFailure::class.java) { runBlocking { AccountDeletion.check { throw HubFailure(status, "unavailable") } } }
            assertEquals(status, error.status)
        }
    }
}
