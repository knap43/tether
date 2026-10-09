package dev.tether.app

import android.app.Application

class TetherApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Notifications.createChannels(this)
    }
}
