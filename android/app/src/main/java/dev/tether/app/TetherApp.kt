package dev.tether.app

import android.app.Application

class TetherApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Appearance.install(this)
        Notifications.createChannels(this)
    }
}
