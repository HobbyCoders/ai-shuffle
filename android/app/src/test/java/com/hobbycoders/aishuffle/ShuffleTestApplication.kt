package com.hobbycoders.aishuffle

import android.app.Application
import androidx.work.Configuration
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper

/**
 * The Application for every Robolectric test (see robolectric.properties).
 *
 * On a phone, androidx.startup's InitializationProvider initialises WorkManager before any
 * activity runs. Robolectric does not run that provider, so with push on (release builds,
 * or `-PpushEnabledDebug=true`) PushRegistration.schedule in an activity's onResume threw
 * "WorkManager is not initialized properly". A test WorkManager per test keeps scheduled
 * work in memory; its constraints (network) are never met, so no worker runs.
 */
class ShuffleTestApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        WorkManagerTestInitHelper.initializeTestWorkManager(
            this, Configuration.Builder().setExecutor(SynchronousExecutor()).build())
    }
}
