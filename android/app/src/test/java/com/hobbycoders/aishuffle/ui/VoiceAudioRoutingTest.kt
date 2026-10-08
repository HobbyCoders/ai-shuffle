package com.hobbycoders.aishuffle.ui

import android.media.AudioDeviceInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceAudioRoutingTest {
    private fun device(id: Int, type: Int) = VoiceRoutePolicy.Device(id, type)
    private val speaker = device(1, AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)
    private val earpiece = device(2, AudioDeviceInfo.TYPE_BUILTIN_EARPIECE)
    private val wired = device(3, AudioDeviceInfo.TYPE_WIRED_HEADSET)
    private val bluetooth = device(4, AudioDeviceInfo.TYPE_BLUETOOTH_SCO)

    @Test fun plugAndUnplugFollowTheHeadsetThenDefaultMediaSpeaker() {
        assertNull(VoiceRoutePolicy.choose(listOf(speaker, earpiece)))
        assertEquals(3, VoiceRoutePolicy.choose(listOf(speaker, earpiece, wired)))
        assertNull(VoiceRoutePolicy.choose(listOf(speaker, earpiece)))
    }
    @Test fun bluetoothConnectAndDisconnectFollowTheHeadset() {
        assertEquals(4, VoiceRoutePolicy.choose(listOf(speaker, bluetooth)))
        assertNull(VoiceRoutePolicy.choose(listOf(speaker)))
    }
    @Test fun wiredWinsOverBluetoothAndBleAndUsbAreSupported() {
        assertEquals(3, VoiceRoutePolicy.choose(listOf(bluetooth, wired)))
        assertEquals(5, VoiceRoutePolicy.choose(listOf(speaker, device(5, AudioDeviceInfo.TYPE_BLE_HEADSET))))
        assertEquals(6, VoiceRoutePolicy.choose(listOf(bluetooth, device(6, AudioDeviceInfo.TYPE_USB_HEADSET))))
        assertNull(VoiceRoutePolicy.choose(listOf(device(7, AudioDeviceInfo.TYPE_BLUETOOTH_A2DP))))
    }
}
