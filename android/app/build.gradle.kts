plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.hobbycoders.aishuffle"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.hobbycoders.aishuffle"
        minSdk = 26
        targetSdk = 36
        versionCode = System.getenv("ANDROID_VERSION_CODE")?.toInt() ?: 12
        versionName = System.getenv("ANDROID_VERSION_NAME") ?: "0.8.19"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        manifestPlaceholders["appAuthRedirectScheme"] = "com.hobbycoders.aishuffle"
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
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (System.getenv("KEYSTORE_PATH") != null) {
                signingConfig = signingConfigs.getByName("release")
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

// The APK ships its own build of the mobile UI (see android/scripts/sync-mobile-ui.sh).
// Never assemble without it: the WebView has nothing to load and a worker must
// never be asked for the UI instead.
val mobileUiIndex = layout.projectDirectory.file("src/main/assets/mobile-ui/index.html")
val checkMobileUi by tasks.registering {
    description = "Fails when the bundled mobile UI (src/main/assets/mobile-ui) is missing."
    group = "verification"
    doLast {
        if (!mobileUiIndex.asFile.exists()) {
            throw GradleException(
                "The bundled mobile UI is missing: ${mobileUiIndex.asFile}\n" +
                    "Run `bash android/scripts/sync-mobile-ui.sh` (or `npm run sync:mobile` in frontend/) " +
                    "before building the APK. The app ships its own UI; workers never serve it."
            )
        }
    }
}
tasks.matching { it.name == "preBuild" }.configureEach { dependsOn(checkMobileUi) }

dependencies {
    implementation("net.openid:appauth:0.11.1")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
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
