plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Deliberately NOT an androidTest variant or a dependency on :app. Package the
// complete test runtime independently of the app's R8 mapping and removals.
android {
    namespace = "com.hobbycoders.aishuffle.test"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.hobbycoders.aishuffle.test"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "smoke-only"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("junit:junit:4.13.2")
    implementation("androidx.test:runner:1.6.2")
    implementation("androidx.test:rules:1.6.1")
    implementation("androidx.test.ext:junit:1.2.1")
    implementation("androidx.test.espresso:espresso-core:3.6.1")
}
