package com.hobbycoders.aishuffle.data

import android.content.Context
import android.content.SharedPreferences
import com.hobbycoders.aishuffle.model.Server
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ServerStoreRecentUseTest {
    private lateinit var prefs: SharedPreferences
    private lateinit var store: ServerStore
    private val workers = listOf("pc", "nas", "laptop").map { Server(it, it, "https://$it.example", "aim_$it", "phone") }
    private fun fromPreferences(): ServerStore = ServerStore::class.java.getDeclaredConstructor(SharedPreferences::class.java).let {
        it.isAccessible = true
        it.newInstance(prefs)
    }
    @Before fun setup() {
        prefs = RuntimeEnvironment.getApplication().getSharedPreferences("recent-workers", Context.MODE_PRIVATE)
        prefs.edit().clear().commit()
        store = fromPreferences()
        workers.forEach(store::add)
    }
    @Test fun recentUsePersistsMoreThanOnlyTheLastWorker() {
        store.markUsed("laptop")
        store.markUsed("nas")
        store.markUsed("pc")
        store = fromPreferences()
        assertEquals(listOf("pc", "nas", "laptop"), store.orderedByRecentUse(workers.reversed()).map { it.id })
        store.markUsed("laptop")
        assertEquals(listOf("laptop", "pc", "nas"), store.orderedByRecentUse(workers).map { it.id })
        assertEquals("laptop", store.lastUsedServerId)
    }
    @Test fun oldInstallsAndCorruptHistoryKeepTheSavedLastUsedPreference() {
        prefs.edit().putString("last_used_server", "nas").commit()
        assertEquals(listOf("nas", "pc", "laptop"), store.orderedByRecentUse(workers).map { it.id })
        prefs.edit().putString("recently_used_servers", "invalid JSON").commit()
        assertEquals(listOf("nas", "pc", "laptop"), store.orderedByRecentUse(workers).map { it.id })
    }
    @Test fun removedWorkersAndAccountChangesDoNotBringThemBack() {
        store.markUsed("laptop")
        store.markUsed("nas")
        store.remove("nas")
        assertEquals(listOf("laptop", "pc"), store.orderedByRecentUse(store.getAll()).map { it.id })
        assertEquals(listOf("pc"), store.orderedByRecentUse(listOf(workers[0])).map { it.id })
    }
}
