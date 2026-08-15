plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.hobbycoders.aishuffle"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.hobbycoders.aishuffle"
        minSdk = 26
        targetSdk = 34
        versionCode = 8
        versionName = "0.6.1-voice"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")
    implementation("org.unifiedpush.android:connector:2.5.0")

    // Voice assistant: WebRTC transport for the OpenAI Realtime call.
    implementation("io.getstream:stream-webrtc-android:1.1.3")
    // Offline wake-word spotting (Vosk small model, downloaded on first enable).
    implementation("com.alphacephei:vosk-android:0.3.47")
    // WebRTC voice-activity detector: near-free C gate that keeps Vosk starved
    // unless someone is actually speaking (fans/music/traffic don't count).
    implementation("com.github.gkonovalov.android-vad:webrtc:2.0.6")
    implementation("net.java.dev.jna:jna:5.13.0@aar")
}
