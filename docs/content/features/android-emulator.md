<!--
title: Android emulator
description: Run, install and test Android apps in an emulator on your worker, live in the Phone panel
order: 13
-->

# Android emulator

AI Shuffle can run an Android phone emulator on your worker. You and your agents can use it to try a phone app you are building:

- install the app;
- open it;
- tap, swipe and type;
- read its log;
- watch the screen live in the **Phone** panel.

The emulator runs a Pixel-sized phone (1080 × 2400) with Android 15 (API 35, Google APIs). It is shared by every chat on that worker. AI Shuffle stops it when nobody has used it for 30 minutes.

## When the Phone panel opens

The phone is the agent's tool, so the composer has no Phone button. The **Phone** panel opens beside the chat when an agent in that chat uses the emulator.

- If you close the panel, it stays closed for that chat while the agent keeps working.
- To open it again, choose **Show phone** next to one of the agent's Emulator steps in the chat. This also works after you reopen an older chat.
- The panel shows the phone of the worker you are connected to. It does not open for a chat that runs on another worker.

## What you need

- **A Linux or Windows worker on an x86_64 (Intel or AMD) processor.** macOS and Windows ARM64 workers are not supported yet.
- **Hardware virtualization.** Linux needs KVM (`/dev/kvm`); Windows needs a working Windows Hypervisor Platform (WHPX) or Android Emulator Hypervisor Driver (AEHD).
- **About 9 GB of free disk space** in the data folder. The emulator, the phone system and the phone's own storage take about 6.5 GB.
- **About 3 GB of free memory** while the phone runs.

### Docker

Pass the KVM device to the container.

- **docker run:** add `--device /dev/kvm`.
- **Compose:** add this under the service:

  ```yaml
  devices:
    - /dev/kvm:/dev/kvm
  ```

- **Unraid:** add `--device /dev/kvm` to **Extra Parameters**.

Then recreate the container. When the container starts, it gives AI Shuffle access to the device's group. If the Phone panel still says it cannot open `/dev/kvm`, do one of these:

- Add `--group-add <the kvm group's number>`. Find the number with `stat -c %g /dev/kvm` on the host.
- Give the device read/write access for everyone on the host (`chmod 666 /dev/kvm`). Most Linux distributions do this already.

### Linux desktop or server without Docker

1. Turn on virtualization (Intel VT-x or AMD-V) in your computer's firmware settings.
2. Make sure the `kvm` module is loaded: `ls -l /dev/kvm` should list the device.
3. If you cannot read and write the device, add your account to the `kvm` group: `sudo usermod -aG kvm $USER`. Then sign out and back in, and restart AI Shuffle.

### Windows desktop

1. Turn on Intel VT-x or AMD-V in BIOS/UEFI.
2. Enable **Windows Hypervisor Platform** in Windows Features, then reboot. Alternatively, use Android Studio's SDK Manager to install **Android Emulator Hypervisor Driver (AEHD)** on a system with Hyper-V disabled. AI Shuffle does not install drivers or change Windows features for you.
3. Set up the SDK from the Phone panel. Before starting a phone, AI Shuffle runs `emulator.exe -accel-check` with a deadline. If acceleration is unavailable, it reports how to fix it instead of attempting a slow software boot.

The Windows desktop installer does not bundle the Android SDK. By default, setup installs and manages a private SDK under `android-sdk` in AI Shuffle's data folder. Windows SDK discovery checks `ANDROID_HOME`, `ANDROID_SDK_ROOT` and `%LOCALAPPDATA%\Android\Sdk` for guidance only: it never adopts, writes to or stops processes from an Android Studio SDK.

`ANDROID_SDK_DIR` is an explicit opt-in to managing another SDK folder. Setup may replace that folder's emulator, platform-tools and selected system-image packages after license acceptance; choose a dedicated folder rather than your Android Studio SDK. Path-based startup cleanup is limited to the private data-folder SDK. In an explicit external SDK, only processes recorded and identity-verified by AI Shuffle's process ledger may be reaped. AI Shuffle uses adb server port **5038**, separate from Android Studio's default **5037**.

Passing acceleration checks are cached until the emulator binary changes; status does not probe again while a phone is booting/running or SDK setup is in progress.

Live Windows verification of this backend is still pending; its Windows paths and process lifecycle are covered by simulated tests.

## Install the emulator

The emulator is not part of AI Shuffle's download. Install it once per worker:

1. Ask an agent to check the emulator, for example *"Check the Android emulator's status."* The **Phone** panel opens.
2. Read the Android SDK License Agreement. The panel links to it.
3. Tick **I accept the Android SDK License Agreement** and choose **Set up Android emulator**.

AI Shuffle downloads about 2 GB from Google: the emulator, the Android platform tools and one phone system. It checks every file against the checksum Google publishes and stores them in the data folder (`android-sdk`). The panel shows the progress, which usually takes a few minutes.

An agent can start the install too, but only after you have told it in the chat that you accept the license.

### What the license means

Google's tools come under the [Android Software Development Kit License Agreement](https://developer.android.com/studio/terms), not under AI Shuffle's license. When you tick the box and choose **Set up Android emulator**, you accept that agreement with Google for your own use of those tools. AI Shuffle records your acceptance in the SDK folder in the same place Android's own tools do. A build that uses this SDK (`ANDROID_HOME`) then sees that the license was accepted.

## Use the Phone panel

- **Start phone** boots the phone. A first start takes about 30 to 60 seconds.
- Click or tap the screen to tap the phone. Drag to swipe. Type on your keyboard to send text.
- The buttons send **Back**, **Home**, **Recent apps**, **Rotate**, **Power** and the volume keys.
- **Install APK** installs an app file (`.apk`, up to 512 MB) from your computer.
- **Stop** shuts the phone down. Apps you installed stay on the phone until you install the emulator again.

## Let an agent test your app

Agents have two tools:

- **Emulator** checks the status, starts and stops the phone, installs an APK, opens an app, taps, swipes, types, presses keys, lists what is on the screen, and reads the log.
- **EmulatorScreenshot** shows the agent the screen.

An agent can build your app, install the APK, open it, tap through it and check the log in one task. Ask, for example: *"Build the debug APK, install it on the emulator, open it and check that the sign-in screen works."*

To build Android apps, the agent also needs Java and the Android build tools. They are not part of AI Shuffle. The agent can install them in the chat. The emulator's status tells it where the SDK is (`ANDROID_HOME`) and where `adb` is, so a Gradle build can install straight onto the phone.

## Who can use it

Only the owner can open the Phone panel and its controls. API keys and other people's devices cannot. Agents use the emulator through their tools and follow the chat's permission mode. Installing the emulator always asks first in the assistant.

## Troubleshooting

| The panel says | What to do |
| --- | --- |
| No /dev/kvm | Pass the device to the container or turn on virtualization. See [What you need](#what-you-need). |
| Cannot open /dev/kvm | Give AI Shuffle's account access to the device. See [Docker](#docker). |
| Not supported on this worker | Use a Linux or Windows x86_64 worker. |
| Hardware acceleration is unavailable | On Windows, enable virtualization and WHPX, reboot, and run `emulator.exe -accel-check` in the SDK's emulator folder. AEHD is an alternative when Hyper-V is disabled. |
| Not enough free disk space | Free up the space the message names, then install again. |
| The emulator exited unexpectedly | Start it again. The message names the emulator's own error, and the log is in `android-sdk/logs` in the data folder. |
