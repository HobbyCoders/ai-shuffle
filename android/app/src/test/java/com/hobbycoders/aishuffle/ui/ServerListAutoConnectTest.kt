package com.hobbycoders.aishuffle.ui

import android.Manifest
import android.app.Activity
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Looper
import android.security.keystore.KeyGenParameterSpec
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.app.ActivityCompat
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.presence.WorkerPresenceProbe
import com.hobbycoders.aishuffle.push.PushPermission
import kotlinx.coroutines.CompletableDeferred
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.android.controller.ActivityController
import org.robolectric.annotation.Config
import java.io.InputStream
import java.io.OutputStream
import java.security.Key
import java.security.KeyStoreSpi
import java.security.Provider
import java.security.SecureRandom
import java.security.Security
import java.security.cert.Certificate
import java.security.spec.AlgorithmParameterSpec
import java.util.Collections
import java.util.Date
import java.util.Enumeration
import java.util.concurrent.ConcurrentHashMap
import javax.crypto.KeyGeneratorSpi
import javax.crypto.SecretKey
import javax.crypto.spec.SecretKeySpec

/**
 * Issue 1067: on launch the list asks for notification permission, and its automatic
 * connect could open a chat and finish the list while the system prompt was coming up.
 * This drives the real activity, permission launcher and encrypted stores; only the
 * final choice of worker and Android's key store are replaced.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class ServerListAutoConnectTest {
    private val context: android.app.Application get() = RuntimeEnvironment.getApplication()
    // The real probe runs on OkHttp's threads, as on a phone. Nothing listens on this local
    // port, so it fails at once and never leaves the machine.
    private val worker = Server("worker", "Home", "https://127.0.0.1:1", "aim_key", "phone")
    private val probes = (WorkerPresenceProbe::class.java.getDeclaredField("client").apply { isAccessible = true }.get(null) as OkHttpClient).dispatcher
    private val opened = mutableListOf<String?>()
    private var searches = 0

    @Before fun setUp() {
        Security.addProvider(FakeAndroidKeyStore())
        ServerStore::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
        ServerStore.get(context).add(worker)
        PushPermission.enabled = true
        ServerListActivity.reachableFixture = { searches++; worker }
        notifications(enabled = false)
    }
    @After fun tearDown() {
        PushPermission.enabled = com.hobbycoders.aishuffle.BuildConfig.PUSH_ENABLED
        ServerListActivity.reachableFixture = null
        ActivityCompat.setPermissionCompatDelegate(null)
        Security.removeProvider(FakeAndroidKeyStore.NAME)
        FakeAndroidKeyStore.keys.clear()
    }

    private fun notifications(enabled: Boolean) =
        shadowOf(context.getSystemService(NotificationManager::class.java)).setNotificationsEnabled(enabled)
    private fun launcher(): ActivityController<ServerListActivity> = Robolectric.buildActivity(ServerListActivity::class.java,
        Intent(context, ServerListActivity::class.java).setAction(Intent.ACTION_MAIN))
    /** Runs the main thread until no probe is running. OkHttp counts a call until after its callback resumed the sweep. */
    private fun settle() {
        val deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(20)
        do {
            shadowOf(Looper.getMainLooper()).idle()
            if (probes.runningCallsCount() + probes.queuedCallsCount() == 0) break
            if (System.nanoTime() > deadline) throw AssertionError("The worker probe did not finish")
            Thread.sleep(2)
        } while (true)
        shadowOf(Looper.getMainLooper()).idle()
    }
    private fun openedChats(): List<String?> {
        settle()
        generateSequence { shadowOf(context).nextStartedActivity }
            .filter { it.component?.className == ChatActivity::class.java.name }
            .forEach { opened += it.getStringExtra(ChatActivity.EXTRA_SERVER_ID) }
        return opened
    }
    private fun requestCode(list: ActivityController<ServerListActivity>): Int {
        val request = shadowOf(list.get()).lastRequestedPermission
        assertNotNull("the launcher must make the real request", request)
        assertEquals(listOf(Manifest.permission.POST_NOTIFICATIONS), request.requestedPermissions.toList())
        return request.requestCode
    }
    private fun answer(list: ActivityController<ServerListActivity>, code: Int, granted: Boolean) = list.get().onRequestPermissionsResult(
        code, arrayOf(Manifest.permission.POST_NOTIFICATIONS),
        intArrayOf(if (granted) PackageManager.PERMISSION_GRANTED else PackageManager.PERMISSION_DENIED))
    private fun assertWaitingForTheAnswer(list: ActivityController<ServerListActivity>) {
        assertEquals("a chat opened over the permission prompt", emptyList<String?>(), openedChats())
        assertFalse("the list finished under the permission prompt", list.get().isFinishing)
    }
    private fun assertOpenedOnce(list: ActivityController<ServerListActivity>) {
        assertEquals(listOf<String?>(worker.id), openedChats())
        assertTrue(list.get().isFinishing)
    }
    private fun views(view: View): List<View> =
        listOf(view) + if (view is ViewGroup) (0 until view.childCount).flatMap { views(view.getChildAt(it)) } else emptyList()

    @Test fun withNoRequestALauncherStartStillConnectsAtOnce() {
        notifications(enabled = true)
        val list = launcher().setup()
        assertNull(shadowOf(list.get()).lastRequestedPermission)
        assertOpenedOnce(list)
    }

    /**
     * A probe that cannot build its request never suspends, so the whole sweep ends inside
     * onResume, before the lifecycle reports RESUMED. A probe that fails at once can do the same.
     */
    private fun sweepEndsInsideTheResume() = ServerStore.get(context).add(worker.copy(baseUrl = "not a url"))

    @Test fun aSweepThatEndsInsideTheResumeStillConnects() {
        sweepEndsInsideTheResume()
        notifications(enabled = true)
        assertOpenedOnce(launcher().setup())
    }

    @Test fun aSweepThatEndsInsideTheResumeAlreadySeesTheRequest() {
        sweepEndsInsideTheResume()
        val list = launcher().setup()
        val code = requestCode(list)
        assertWaitingForTheAnswer(list)
        assertEquals("the request starts before the sweep", 0, searches)
        answer(list, code, granted = true)
        assertOpenedOnce(list)
    }

    @Test fun waitsForAllowThenConnectsWithoutATap() = answeredWhilePaused(granted = true)
    @Test fun waitsForDenyThenConnectsWithoutATap() = answeredWhilePaused(granted = false)
    private fun answeredWhilePaused(granted: Boolean) {
        val list = launcher().setup()
        val code = requestCode(list)
        assertWaitingForTheAnswer(list)
        list.pause()
        answer(list, code, granted)
        list.resume()
        assertOpenedOnce(list)
    }

    /** The logcat race: Android reports RESUMED after the request, before it pauses the list. */
    @Test fun theResumedStateAfterTheRequestIsNotPermissionToConnect() {
        val list = launcher().setup()
        requestCode(list)
        assertTrue(list.get().lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED))
        assertWaitingForTheAnswer(list)
        assertEquals("a sweep held back by the request starts no search", 0, searches)
    }

    /** "Don't ask again" and a granted permission can answer while the list never left RESUMED. */
    @Test fun anImmediateAnswerContinuesWithNoResumeToFollow() {
        val list = launcher().setup()
        val code = requestCode(list)
        assertWaitingForTheAnswer(list)
        answer(list, code, granted = false)
        assertOpenedOnce(list)
    }

    @Test fun aRequestThatCannotStartDoesNotBlockConnecting() {
        ActivityCompat.setPermissionCompatDelegate(object : ActivityCompat.PermissionCompatDelegate {
            override fun requestPermissions(activity: Activity, permissions: Array<String>, requestCode: Int): Boolean =
                throw IllegalStateException("no permission controller")
            override fun onActivityResult(activity: Activity, requestCode: Int, resultCode: Int, data: Intent?) = false
        })
        val list = launcher().setup()
        assertOpenedOnce(list)
    }

    @Test fun aSearchAlreadyRunningWhenTheRequestStartsCannotConnectOverIt() {
        val running = CompletableDeferred<Server?>()
        ServerListActivity.reachableFixture = { if (searches++ == 0) running.await() else worker }
        notifications(enabled = true)
        val list = launcher().setup()
        settle()
        assertEquals("the first search is in flight", 1, searches)
        notifications(enabled = false)
        list.pause().resume()
        val code = requestCode(list)
        running.complete(worker)
        assertWaitingForTheAnswer(list)
        list.pause()
        answer(list, code, granted = true)
        list.resume()
        assertOpenedOnce(list)
    }

    /** onStop cancels the search; nothing else remembers that one is still owed. */
    @Test fun aSearchCancelledBehindThePromptStartsAgainAfterTheAnswer() {
        val never = CompletableDeferred<Server?>()
        ServerListActivity.reachableFixture = { if (searches++ == 0) never.await() else worker }
        notifications(enabled = true)
        val list = launcher().setup()
        settle()
        notifications(enabled = false)
        list.pause().resume()
        val code = requestCode(list)
        list.pause().stop()
        answer(list, code, granted = false)
        list.start().resume()
        assertOpenedOnce(list)
    }

    /** A relaunch under the prompt passes through onResume, then waits for the answer. */
    @Test fun rotatingUnderThePromptKeepsWaitingAndThenConnects() {
        val first = launcher().setup()
        val code = requestCode(first)
        assertWaitingForTheAnswer(first)
        val state = Bundle()
        first.pause().saveInstanceState(state).stop().destroy()
        // Even if the "asked" flag did not reach disk: a second request would cancel the open one.
        context.getSharedPreferences("needs_you_permission", 0).edit().clear().commit()
        val second = launcher().create(state).start().postCreate(state).resume()
        assertWaitingForTheAnswer(second)
        assertNull("the request is still open: never ask twice", shadowOf(second.get()).lastRequestedPermission)
        second.pause()
        answer(second, code, granted = true)
        second.resume()
        assertOpenedOnce(second)
    }

    /** The answer reaches the recreated process before its first resume. */
    @Test fun anAnswerDeliveredToARecreatedListStillConnects() {
        val first = launcher().setup()
        val code = requestCode(first)
        val state = Bundle()
        first.pause().saveInstanceState(state).stop().destroy()
        val second = launcher().create(state).start().postCreate(state)
        answer(second, code, granted = false)
        second.resume()
        assertOpenedOnce(second)
    }

    private fun tap(list: ActivityController<ServerListActivity>, server: Server) {
        val name = views(list.get().window.decorView).filterIsInstance<TextView>().first { it.text.toString() == server.name }
        assertTrue(generateSequence<View>(name) { it.parent as? View }.first { it.isClickable }.performClick())
    }

    @Test fun aManualWorkerTapStillOpensWhileARequestIsOpen() {
        val list = launcher().setup()
        requestCode(list)
        settle()
        tap(list, worker)
        assertEquals(listOf<String?>(worker.id), openedChats())
        assertFalse("a tap keeps the list underneath, as it always has", list.get().isFinishing)
    }

    /** Answer the prompt, pick B while the restarted search runs, press Back before B's page loads. */
    @Test fun comingBackFromAChosenWorkerDoesNotOpenAnother() {
        val chosen = Server("chosen", "Chosen", "https://127.0.0.1:2", "aim_key", "phone")
        ServerStore.get(context).add(chosen)
        val running = CompletableDeferred<Server?>()
        var cancelled = false
        ServerListActivity.reachableFixture = {
            searches++
            try { running.await() } catch (stopped: kotlinx.coroutines.CancellationException) { cancelled = true; throw stopped }
        }
        val list = launcher().setup()
        val code = requestCode(list)
        answer(list, code, granted = true)
        settle()
        assertEquals("the answer restarted the search", 1, searches)
        tap(list, chosen)
        assertEquals(listOf<String?>(chosen.id), openedChats())
        assertTrue("choosing a worker ends the search", cancelled)
        running.complete(worker)
        list.pause().stop().start().resume()
        assertEquals("only the chosen worker opens", listOf<String?>(chosen.id), openedChats())
        assertEquals("nothing searches on the way back", 1, searches)
        assertFalse(list.get().isFinishing)
    }

    /** Going to Settings and back chooses nothing, so the owed connect still happens. */
    @Test fun comingBackWithNoChoiceStillConnects() {
        val never = CompletableDeferred<Server?>()
        ServerListActivity.reachableFixture = { if (searches++ == 0) never.await() else worker }
        notifications(enabled = true)
        val list = launcher().setup()
        settle()
        assertEquals("the first search is in flight", 1, searches)
        list.pause().stop().start().resume()
        assertOpenedOnce(list)
    }

    /** A search that outlives the wish to connect must check it again before opening. */
    @Test fun aSearchThatEndsAfterTheConnectWasCalledOffOpensNothing() {
        val running = CompletableDeferred<Server?>()
        ServerListActivity.reachableFixture = { searches++; running.await() }
        notifications(enabled = true)
        val list = launcher().setup()
        settle()
        assertEquals("the search is in flight", 1, searches)
        list.newIntent(Intent(context, ServerListActivity::class.java))
        running.complete(worker)
        assertEquals(emptyList<String?>(), openedChats())
        assertFalse(list.get().isFinishing)
    }

    /** Robolectric has no Android key store; the real encrypted stores need only these two services. */
    class FakeAndroidKeyStore : Provider(NAME, 1.0, "In-memory stand-in for tests") {
        init {
            put("KeyStore.$NAME", Store::class.java.name)
            put("KeyGenerator.AES", Generator::class.java.name)
        }
        companion object {
            const val NAME = "AndroidKeyStore"
            val keys = ConcurrentHashMap<String, SecretKey>()
        }
        class Generator : KeyGeneratorSpi() {
            private var alias = ""
            override fun engineInit(random: SecureRandom?) = throw UnsupportedOperationException()
            override fun engineInit(params: AlgorithmParameterSpec?, random: SecureRandom?) { alias = (params as KeyGenParameterSpec).keystoreAlias }
            override fun engineInit(keysize: Int, random: SecureRandom?) = throw UnsupportedOperationException()
            override fun engineGenerateKey(): SecretKey =
                SecretKeySpec(ByteArray(32).also { SecureRandom().nextBytes(it) }, "AES").also { keys[alias] = it }
        }
        class Store : KeyStoreSpi() {
            override fun engineLoad(stream: InputStream?, password: CharArray?) = Unit
            override fun engineStore(stream: OutputStream?, password: CharArray?) = Unit
            override fun engineGetKey(alias: String, password: CharArray?): Key? = keys[alias]
            override fun engineContainsAlias(alias: String) = keys.containsKey(alias)
            override fun engineDeleteEntry(alias: String) { keys.remove(alias) }
            override fun engineAliases(): Enumeration<String> = Collections.enumeration(keys.keys)
            override fun engineSize() = keys.size
            override fun engineIsKeyEntry(alias: String) = keys.containsKey(alias)
            override fun engineIsCertificateEntry(alias: String) = false
            override fun engineGetCertificate(alias: String): Certificate? = null
            override fun engineGetCertificateChain(alias: String): Array<Certificate>? = null
            override fun engineGetCertificateAlias(cert: Certificate?): String? = null
            override fun engineGetCreationDate(alias: String): Date? = null
            override fun engineSetKeyEntry(alias: String, key: Key?, password: CharArray?, chain: Array<out Certificate>?) = throw UnsupportedOperationException()
            override fun engineSetKeyEntry(alias: String, key: ByteArray?, chain: Array<out Certificate>?) = throw UnsupportedOperationException()
            override fun engineSetCertificateEntry(alias: String, cert: Certificate?) = throw UnsupportedOperationException()
        }
    }
}
