package dev.tether.app

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService
import java.io.File

/** Quick Settings tile: send the clipboard to the computer. */
class ClipTileService : TileService() {
    override fun onClick() {
        super.onClick()
        val intent = Intent(this, ClipboardActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE))
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}

/** Starts the service after boot or an app update, once a computer has been paired. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val peers = File(context.filesDir, "peers.json")
        if (peers.exists() && peers.length() > 2) Hub.start(context)
    }
}
