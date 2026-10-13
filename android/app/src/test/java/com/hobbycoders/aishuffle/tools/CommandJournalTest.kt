package com.hobbycoders.aishuffle.tools

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class CommandJournalTest {
    private val prefs get() = RuntimeEnvironment.getApplication().getSharedPreferences("test_journal", Context.MODE_PRIVATE)
    @Before fun clear() { prefs.edit().clear().commit() }
    @Test fun expiryIsBoundedAndMalformedExpiresClosed() {
        assertFalse(CommandJournal.validExpiry(1000, 1000))
        assertFalse(CommandJournal.validExpiry(301001, 1000))
        assertTrue(CommandJournal.validExpiry(301000, 1000))
        assertEquals(0L, CommandJournal.expiry("not-a-date"))
        assertEquals(1000L, CommandJournal.expiry("1970-01-01T00:00:01Z"))
    }
    @Test fun durableClaimPreventsReexecutionAfterRestartAndNetworkRetry() {
        val key = CommandJournal.key("server", "command")
        var journal = CommandJournal(prefs)
        assertTrue(journal.claim(key, "command", "server", 5000, 1000))
        journal = CommandJournal(prefs) // process recreation: same persisted backing store
        assertFalse(journal.claim(key, "command", "server", 5000, 1000))
        assertEquals("interrupted", journal.get(key)!!.getJSONObject("result").getString("status"))
        val result = JSONObject().put("ok", true).put("status", "opened_system_ui")
        journal.finish(key, result)
        assertEquals(result.toString(), journal.pending("server", 2000).single().second.getJSONObject("result").toString())
        // A failed result POST leaves the same payload, without any new claim.
        assertFalse(CommandJournal(prefs).claim(key, "command", "server", 5000, 2000))
        journal.posted(key)
        assertTrue(journal.pending("server", 2000).isEmpty())
        assertFalse(journal.get(key)!!.has("result"))
        assertFalse(journal.claim(key, "command", "server", 5000, 2000))
    }
    @Test fun scopeIsolationAndResultRetriesExpire() {
        assertNotEquals(CommandJournal.key("one", "id"), CommandJournal.key("two", "id"))
        val journal = CommandJournal(prefs)
        journal.claim("key", "id", "one", 2000, 1000)
        assertTrue(journal.pending("two", 1000).isEmpty())
        assertTrue(journal.pending("one", 2000 + CommandJournal.MAX_LIFETIME).isEmpty())
    }
    @Test fun boundedCacheNeverEvictsUnexpiredClaimsToAllowReexecution() {
        val journal = CommandJournal(prefs)
        repeat(CommandJournal.MAX_ENTRIES) { assertTrue(journal.claim("key$it", "id$it", "s", 3000, 1000)) }
        assertFalse(journal.claim("overflow", "id", "s", 3000, 1000))
        assertFalse(journal.claim("key0", "id0", "s", 3000, 1000))
    }
}
