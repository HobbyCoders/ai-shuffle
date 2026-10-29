package com.hobbycoders.aishuffle.push

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.hobbycoders.aishuffle.BuildConfig

class NeedsYouMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) { if (BuildConfig.PUSH_ENABLED) PushRegistration.schedule(this) }
    override fun onMessageReceived(message: RemoteMessage) {
        if (BuildConfig.PUSH_ENABLED) NeedsYouNotifications.receive(this, message.data)
    }
    override fun onDeletedMessages() { if (BuildConfig.PUSH_ENABLED) PushRegistration.schedule(this) }
}
