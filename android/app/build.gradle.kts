// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 HobbyCoders.

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Local/CI builds without Firebase config still build and run, with push off.
val pushConfigured = file("google-services.json").isFile
if (pushConfigured) apply(plugin = "com.google.gms.google-services")

extra["license"] = "AGPL-3.0-only"

// The Google Play bundle leaves out app control (the AccessibilityService):
// Play's Accessibility API policy does not cover AI-planned actions. Builds
// for the website APK and local development keep it. android-play.yml sets
// ANDROID_PLAY_STORE_BUILD=true; `-PplayStoreBuild=true` does the same locally.
val playStoreBuild = System.getenv("ANDROID_PLAY_STORE_BUILD") == "true" ||
    project.findProperty("playStoreBuild")?.toString() == "true"
val minifiedSmoke = project.findProperty("minifiedSmoke")?.toString() == "true"

android {
    namespace = "com.hobbycoders.aishuffle"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.hobbycoders.aishuffle"
        minSdk = 26
        targetSdk = 36
        versionCode = System.getenv("ANDROID_VERSION_CODE")?.toInt() ?: 13
        versionName = System.getenv("ANDROID_VERSION_NAME") ?: "1.2.1"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        manifestPlaceholders["appAuthRedirectScheme"] = "com.hobbycoders.aishuffle"
        buildConfigField("boolean", "APP_CONTROL", (!playStoreBuild).toString())
        buildConfigField("boolean", "PUSH_ENABLED", pushConfigured.toString())
    }

    if (playStoreBuild) {
        // Overlays the release manifest and removes the AccessibilityService.
        sourceSets.getByName("release").manifest.srcFile("src/playStore/AndroidManifest.xml")
    }

    signingConfigs {
        create("release") {
            // Populated from env vars (CI secrets) so every build is signed with
            // the SAME key — required for in-place updates. When absent (local
            // builds without the secrets), the release type is left unsigned.
            val keystorePath = System.getenv("KEYSTORE_PATH")
            if (keystorePath != null) {
                storeFile = file(keystorePath)
                storePassword = System.getenv("KEYSTORE_PASSWORD")
                keyAlias = System.getenv("KEY_ALIAS")
                keyPassword = System.getenv("KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (System.getenv("KEYSTORE_PATH") != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
        if (minifiedSmoke) {
            // Never shipped: same R8/resource rules and non-debuggable code as
            // release, plus a fixture launcher and disposable debug signing.
            create("releaseSmoke") {
                initWith(getByName("release"))
                signingConfig = signingConfigs.getByName("debug")
                matchingFallbacks += "release"
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    testOptions {
        unitTests.isIncludeAndroidResources = true
    }

    androidResources {
        // Keep Svelte's _app directory. AAPT's default <dir>_* filter otherwise
        // strips every JS/CSS/font file while retaining a misleading HTML shell.
        ignoreAssetsPattern = "!.svn:!.git:!.ds_store:!*.scc:.*:!CVS:!thumbs.db:!picasa.ini:!*~"
    }
}

// Chat loads the phone UI from the paired worker's /mobile/; the APK carries one
// copy only for the offline demo (see android/scripts/sync-offline-demo.sh).
// Never assemble without it: the demo would open to a blank page.
val offlineDemoIndex = layout.projectDirectory.file("src/main/assets/offline-demo/index.html")
val checkOfflineDemo by tasks.registering {
    description = "Fails when the offline demo UI (src/main/assets/offline-demo) is missing."
    group = "verification"
    doLast {
        if (!offlineDemoIndex.asFile.exists()) {
            throw GradleException(
                "The offline demo UI is missing: ${offlineDemoIndex.asFile}\n" +
                    "Run `bash android/scripts/sync-offline-demo.sh` before building the APK. " +
                    "Chat itself loads the phone UI from the worker; only the demo is bundled."
            )
        }
    }
}
tasks.matching { it.name == "preBuild" }.configureEach { dependsOn(checkOfflineDemo) }

dependencies {
    implementation(platform("com.google.firebase:firebase-bom:33.7.0"))
    implementation("com.google.firebase:firebase-messaging:24.1.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("net.openid:appauth:0.11.1")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("androidx.webkit:webkit:1.10.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("org.robolectric:robolectric:4.16.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    testImplementation("org.json:json:20240303")
}
