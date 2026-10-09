package dev.tether.app

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.provider.Settings as AndroidSettings
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.RemoteMedia
import dev.tether.core.Settings
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Keeps the node running in the background as a foreground service, holds the
 * Wi-Fi multicast lock needed to hear discovery broadcasts, and watches the clipboard.
 */
class TetherService : Service() {
    private var multicastLock: WifiManager.MulticastLock? = null
    private var clipboard: ClipboardManager? = null
    private var logcat: Process? = null

    private val clipListener = ClipboardManager.OnPrimaryClipChangedListener { readClipboardInForeground() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startInForeground()
        multicastLock = getSystemService(WifiManager::class.java)?.createMulticastLock("tether")?.apply {
            setReferenceCounted(false)
            acquire()
        }
        Thread({ startNode() }, "tether-start").start()
        clipboard = getSystemService(ClipboardManager::class.java)
        clipboard?.addPrimaryClipChangedListener(clipListener)
        registerReceiver(batteryReceiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        startLogcatWatcher()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_MEDIA -> {
                val peer = intent.getStringExtra(EXTRA_PEER)
                val action = intent.getStringExtra(EXTRA_MEDIA_ACTION)
                val node = Hub.node
                if (peer != null && action != null && node != null) {
                    node.pool.execute {
                        runCatching {
                            val player = Hub.media[peer]?.active?.name
                            RemoteMedia(node, peer).command(action, player)
                        }
                    }
                }
            }
            ACTION_CANCEL_TRANSFER -> Hub.node?.cancelTransfer(intent.getIntExtra(EXTRA_TRANSFER, -1))
        }
        return START_STICKY
    }

    override fun onDestroy() {
        clipboard?.removePrimaryClipChangedListener(clipListener)
        runCatching { unregisterReceiver(batteryReceiver) }
        MediaNotifier.clear(this)
        logcat?.destroy()
        logcat = null
        multicastLock?.release()
        val node = Hub.node
        Hub.node = null
        Thread { node?.stop() }.start()
        super.onDestroy()
    }

    private fun startNode() {
        if (Hub.node != null) return
        val root = Environment.getExternalStorageDirectory().path
        val defaults = Settings.Defaults(
            name = deviceName(),
            shares = mapOf("Internal storage" to root),
            downloadDir = "$root/Download/Tether",
        )
        val node = Node(filesDir, KeystoreIdentity(), AndroidPlatform(applicationContext), defaults, "phone")
        try {
            node.start()
        } catch (e: Exception) {
            AndroidPlatform(applicationContext).log("failed to start: $e")
            stopSelf()
            return
        }
        Hub.node = node
        Hub.dispatch(dev.tether.core.NodeEvent.StatusChanged)
        refreshNotification(this)
    }

    private fun deviceName(): String {
        val named = AndroidSettings.Global.getString(contentResolver, AndroidSettings.Global.DEVICE_NAME)
        return if (!named.isNullOrBlank()) named else "${Build.MANUFACTURER} ${Build.MODEL}".trim()
    }

    private fun startInForeground() {
        val n = buildNotification(this)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(Notifications.ID_SERVICE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(Notifications.ID_SERVICE, n)
        }
    }

    /** Works only while one of our activities has focus (Android 10+ restriction). */
    private fun readClipboardInForeground() {
        ClipCapture.capture(this, force = false)
    }

    private val batteryReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            if (level < 0 || scale <= 0) return
            val charging = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
            val pct = level * 100 / scale
            Hub.node?.let { n -> n.pool.execute { n.reportBattery(pct, charging) } }
        }
    }

    /**
     * Optional automatic clipboard sync. When the clipboard changes while we're in the
     * background, ClipboardService logs that it denied us access. With READ_LOGS (granted
     * once over adb) we see that line and briefly bring up an invisible activity, which is
     * allowed to read the clipboard. Starting it from the background needs the
     * "display over other apps" permission.
     */
    private fun startLogcatWatcher() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || !autoClipboardAvailable(this)) return
        Thread({
            try {
                val since = SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.ROOT).format(Date())
                val p = Runtime.getRuntime().exec(arrayOf("logcat", "-T", since, "ClipboardService:E", "*:S"))
                logcat = p
                var last = 0L
                p.inputStream.bufferedReader().forEachLine { line ->
                    if (line.contains(packageName) && Hub.node?.settings?.clipboard == true) {
                        val now = System.currentTimeMillis()
                        if (now - last > 500) {
                            last = now
                            startActivity(
                                Intent(this, ClipboardActivity::class.java)
                                    .putExtra(ClipboardActivity.EXTRA_AUTO, true)
                                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION),
                            )
                        }
                    }
                }
            } catch (e: Exception) {
                AndroidPlatform(applicationContext).log("clipboard watcher stopped: $e")
            }
        }, "tether-logcat").apply {
            isDaemon = true
            start()
        }
    }

    companion object {
        const val ACTION_STOP = "dev.tether.STOP"
        const val ACTION_MEDIA = "dev.tether.MEDIA"
        const val ACTION_CANCEL_TRANSFER = "dev.tether.CANCEL_TRANSFER"
        const val EXTRA_PEER = "peer"
        const val EXTRA_MEDIA_ACTION = "media_action"
        const val EXTRA_TRANSFER = "transfer"

        fun autoClipboardAvailable(context: Context): Boolean =
            context.checkSelfPermission(Manifest.permission.READ_LOGS) == PackageManager.PERMISSION_GRANTED &&
                AndroidSettings.canDrawOverlays(context)

        fun buildNotification(context: Context): Notification {
            val connected = Hub.computers()
            val text = when {
                Hub.node == null -> "Starting…"
                connected.isNotEmpty() -> "Connected to ${connected.joinToString { it.name }}"
                Hub.node?.peers?.all()?.isNotEmpty() == true -> "Waiting for your computer"
                else -> "Not paired yet — open the app to pair"
            }
            val clipIntent = PendingIntent.getActivity(
                context, 1, Intent(context, ClipboardActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE,
            )
            val b = Notification.Builder(context, Notifications.CH_SERVICE)
                .setSmallIcon(R.drawable.ic_stat)
                .setColor(context.getColor(R.color.md_primary))
                .setContentTitle("Tether")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(Notifications.openApp(context))
            if (connected.isNotEmpty()) {
                b.addAction(Notification.Action.Builder(null, "Send clipboard", clipIntent).build())
            }
            return b.build()
        }

        fun refreshNotification(context: Context) {
            if (Hub.node == null) return
            Hub.runOnMain {
                val nm = context.getSystemService(NotificationManager::class.java)
                try {
                    nm?.notify(Notifications.ID_SERVICE, buildNotification(context))
                } catch (e: SecurityException) {
                    // notification permission not granted
                }
            }
        }
    }
}
