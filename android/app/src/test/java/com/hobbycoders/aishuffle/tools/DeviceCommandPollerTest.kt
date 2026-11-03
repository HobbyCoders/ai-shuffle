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
        for (status in (400..499).filter { it !in setOf(408, 425, 429) }) {
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

    @Test fun authorizationRejectionRetiresTheResultBeforeStoppingTheWorker() = runBlocking {
        seed("old")
        val poller = DeviceCommandPoller(journal, "scope", { _, _ -> throw ToolsAuthorizationRevoked() },
            { true }, { _, _, _ -> error("Must not execute") })
        try { poller.pollOnce(true); fail("Must stop the unauthorized worker") }
        catch (_: ToolsAuthorizationRevoked) { }
        assertFalse(journal.get("old")!!.has("result"))
        assertFalse(journal.claim("old", "old", "scope", System.currentTimeMillis() + 20_000, System.currentTimeMillis()))
    }

    @Test fun retryableFailuresKeepTheResultAndNeverRepeatExecution() = runBlocking {
        for (failure in listOf(IOException("offline"), ToolsHttpFailure(500), ToolsHttpFailure(408), ToolsHttpFailure(425), ToolsHttpFailure(429))) {
            clear(); seed("old")
            val poller = DeviceCommandPoller(journal, "scope", { _, _ -> throw failure }, { true }, { _, _, _ -> error("Repeated execution") })
            try { poller.pollOnce(true); fail("Must retry") } catch (_: IOException) { }
            assertTrue(journal.get("old")!!.has("result"))
            assertFalse(journal.get("old")!!.getBoolean("posted"))
        }
    }

    @Test fun claimedCommandsAfterForegroundLossReceiveFactualResults() = runBlocking {
        val now = System.currentTimeMillis()
        val commands = JSONArray()
        for (id in listOf("first", "second", "third")) commands.put(JSONObject().put("id", id).put("name", "open_url")
            .put("arguments", JSONObject()).put("expires_at", now + 20_000))
        var foreground = true
        var executions = 0
        val posted = mutableListOf<JSONObject>()
        val poller = DeviceCommandPoller(journal, "scope", { path, body ->
            if (path == "pending") JSONObject().put("commands", commands)
            else { posted += body!!.getJSONObject("result"); JSONObject().put("ok", true) }
        }, { foreground }, { _, _, _ ->
            executions++; foreground = false
            JSONObject().put("ok", true).put("status", "launched_confirmation")
        })
        poller.pollOnce(true)
        assertEquals(1, executions)
        assertEquals(3, posted.size)
        for (result in posted.drop(1)) {
            assertEquals("foreground_required", result.getString("status"))
            assertTrue(result.toString().contains("no action was performed"))
        }
        assertTrue(journal.pending("scope", now).isEmpty())
    }

    @Test fun largeUnicodeScreenResultFitsTheServersAsciiJsonLimit() = runBlocking {
        val nodes = JSONArray()
        repeat(80) { nodes.put(JSONObject().put("ref", "node-$it").put("text", "中".repeat(160)).put("description", "Ж".repeat(160))) }
        val screen = JSONObject().put("ok", true).put("snapshot", "snapshot").put("nodes", nodes)
        val command = JSONObject().put("id", "screen").put("name", "read_screen").put("arguments", JSONObject())
            .put("expires_at", System.currentTimeMillis() + 20_000)
        var delivered = false
        DeviceCommandPoller(journal, "scope", { path, body ->
            if (path == "pending") JSONObject().put("commands", JSONArray().put(command)) else {
                val result = body!!.getJSONObject("result")
                // Python bounded_json uses ensure_ascii=True, including both UTF-16 surrogates.
                val escapedBytes = result.toString().fold(0) { size, ch -> size + if (ch.code > 127) 6 else 1 }
                assertTrue("Server would reject $escapedBytes bytes", escapedBytes <= 64 * 1024)
                assertTrue(result.getBoolean("ok"))
                assertTrue(result.getBoolean("truncated"))
                assertTrue(result.getJSONArray("nodes").length() > 0)
                delivered = true
                JSONObject().put("ok", true)
            }
        }, { true }, { _, _, _ -> screen }).pollOnce(true)
        assertTrue(delivered)
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
