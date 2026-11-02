package com.hobbycoders.aishuffle.tools

import android.content.Context
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.IOException

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class DeviceCommandPollerTest {
    private val prefs get() = RuntimeEnvironment.getApplication().getSharedPreferences("poller", Context.MODE_PRIVATE)
    private val journal get() = CommandJournal(prefs)
    private fun seed(id: String) {
        val now = System.currentTimeMillis()
        assertTrue(journal.claim(id, id, "scope", now + 20_000, now))
        journal.finish(id, JSONObject().put("ok", true).put("status", "launched_confirmation"))
    }
    @Before fun clear() { prefs.edit().clear().commit() }

    @Test fun finalRejectionsClearSensitiveResultsAndFetchNewWorkInTheSameIteration() = runBlocking {
        for (status in listOf(404, 409, 410)) {
            clear(); seed("old"); seed("other")
            val calls = mutableListOf<String>()
            val poller = DeviceCommandPoller(journal, "scope", { path, _ ->
                calls += path
                if (path == "old/result") throw ToolsHttpFailure(status)
                if (path == "pending") JSONObject().put("commands", JSONArray()) else JSONObject().put("ok", true)
            }, { true }, { _, _, _ -> error("Stored commands must not execute again") })
            poller.pollOnce(true)
            assertEquals("pending", calls.last())
            assertTrue(calls.contains("other/result"))
            assertTrue(journal.pending("scope", System.currentTimeMillis()).isEmpty())
            assertFalse(journal.get("old")!!.has("result"))
            assertFalse(journal.claim("old", "old", "scope", System.currentTimeMillis() + 20_000, System.currentTimeMillis()))
        }
    }

    @Test fun retryableFailuresKeepTheResultAndNeverRepeatExecution() = runBlocking {
        for (failure in listOf(IOException("offline"), ToolsHttpFailure(500), ToolsHttpFailure(429))) {
            clear(); seed("old")
            val poller = DeviceCommandPoller(journal, "scope", { _, _ -> throw failure }, { true }, { _, _, _ -> error("Repeated execution") })
            try { poller.pollOnce(true); fail("Must retry") } catch (_: IOException) { }
            assertTrue(journal.get("old")!!.has("result"))
            assertFalse(journal.get("old")!!.getBoolean("posted"))
        }
    }

    @Test fun aFreshRejectedResultAlsoDoesNotBlockTheNextCommand() = runBlocking {
        val now = System.currentTimeMillis()
        val commands = JSONArray()
        for (id in listOf("first", "second")) commands.put(JSONObject().put("id", id).put("name", "read_screen")
            .put("arguments", JSONObject()).put("expires_at", now + 20_000))
        val executed = mutableListOf<String>()
        var reads = 0
        val poller = DeviceCommandPoller(journal, "scope", { path, _ ->
            when (path) {
                "pending" -> { reads++; JSONObject().put("commands", commands) }
                "first/result" -> throw ToolsHttpFailure(409)
                else -> JSONObject().put("ok", true)
            }
        }, { true }, { name, _, _ -> executed += name; JSONObject().put("ok", true) })
        poller.pollOnce(true)
        poller.pollOnce(true)
        assertEquals(2, executed.size)
        assertEquals(2, reads)
        assertTrue(journal.pending("scope", now).isEmpty())
    }
}
