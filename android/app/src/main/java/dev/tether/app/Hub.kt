package dev.tether.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.media.MediaScannerConnection
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import android.util.Log
import dev.tether.R
import dev.tether.core.Node
import dev.tether.core.NodeEvent
import dev.tether.core.Platform
import dev.tether.core.Session
import java.io.File
import java.util.concurrent.CopyOnWriteArraySet

/** Process-wide access to the running node and UI event fan-out. */
object Hub {
    @Volatile
    var node: Node? = null

    /** Latest media state per computer. */
    val media = java.util.concurrent.ConcurrentHashMap<String, dev.tether.core.MediaState>()

    /** The last text we put on the clipboard ourselves, so we don't echo it back. */
    @Volatile
    var lastRemoteClip: String? = null

    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArraySet<(NodeEvent) -> Unit>()

    fun addListener(l: (NodeEvent) -> Unit) {
        listeners.add(l)
    }

    fun removeListener(l: (NodeEvent) -> Unit) {
        listeners.remove(l)
    }

    fun dispatch(e: NodeEvent) {
        main.post { listeners.forEach { it(e) } }
    }

    fun runOnMain(block: () -> Unit) {
        main.post(block)
    }

    /** Connected computers (sessions with desktop peers), newest first. */
    fun computers(): List<Session> = node?.connected()?.filter { it.type != "phone" } ?: emptyList()

    fun start(context: Context) {
        context.startForegroundService(Intent(context, TetherService::class.java))
    }
}

object Notifications {
    const val CH_SERVICE = "service"
    const val CH_EVENTS = "events"
    const val CH_REMOTE = "remote"
    const val CH_MEDIA = "media"
    const val CH_TRANSFERS = "transfers"
    const val ID_SERVICE = 1
    private var nextId = 100

    fun createChannels(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(CH_SERVICE, "Connection status", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_EVENTS, "Files and pairing", NotificationManager.IMPORTANCE_DEFAULT),
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_REMOTE, "Sent from your computer", NotificationManager.IMPORTANCE_HIGH),
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_MEDIA, "Media on your computer", NotificationManager.IMPORTANCE_LOW).apply {
                setShowBadge(false)
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(CH_TRANSFERS, "Transfers", NotificationManager.IMPORTANCE_LOW).apply {
                setShowBadge(false)
            },
        )
    }

    fun openApp(context: Context, extra: String? = null): PendingIntent {
        val i = Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (extra != null) i.putExtra(MainActivity.EXTRA_PAIR, extra)
        return PendingIntent.getActivity(
            context, extra?.hashCode() ?: 0, i,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    fun post(
        context: Context,
        title: String,
        text: String,
        intent: PendingIntent? = null,
        channel: String = CH_EVENTS,
        subText: String? = null,
    ) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        val n = Notification.Builder(context, channel)
            .setSubText(subText)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(context.getColor(R.color.md_primary))
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(intent ?: openApp(context))
            .build()
        try {
            nm.notify(nextId++, n)
        } catch (e: SecurityException) {
            // notification permission not granted
        }
    }
}

class AndroidPlatform(private val context: Context) : Platform {
    override fun setClipboard(text: String) {
        Hub.lastRemoteClip = text
        Hub.runOnMain {
            val cm = context.getSystemService(ClipboardManager::class.java)
            cm?.setPrimaryClip(ClipData.newPlainText("Tether", text))
        }
    }

    override fun setClipboardImage(mime: String, data: ByteArray) {
        ClipImageProvider.setClipboard(context, mime, data)
    }

    override fun dismissNotification(key: String) {
        NotifListener.instance?.dismiss(key)
    }

    override fun replyToNotification(key: String, text: String): Boolean =
        NotifListener.instance?.reply(key, text) ?: false

    override fun downloadsDir(): File {
        val pub = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "Tether")
        if (Environment.isExternalStorageManager() || pub.canWrite()) return pub
        return File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "Tether")
    }

    override fun onEvent(event: NodeEvent) {
        Hub.dispatch(event)
        when (event) {
            is NodeEvent.StatusChanged -> {
                context.contentResolver.notifyChange(DocumentsContract.buildRootsUri(PcDocumentsProvider.AUTHORITY), null)
                TetherService.refreshNotification(context)
            }
            is NodeEvent.PairRequest -> if (!event.initiator) {
                val code = event.sas.substring(0, 3) + " " + event.sas.substring(3)
                Notifications.post(
                    context, "Pairing request from ${event.name}",
                    "Open Tether and check that both screens show $code",
                    Notifications.openApp(context, event.id),
                )
            }
            is NodeEvent.FileReceived -> {
                MediaScannerConnection.scanFile(context, arrayOf(event.file.path), null, null)
                val from = Hub.node?.peers?.get(event.from)?.name ?: "your computer"
                Notifications.post(context, "File from $from", "${event.file.name} saved to Download/Tether")
            }
            is NodeEvent.Notification ->
                Notifications.post(context, event.title, event.text, channel = Notifications.CH_REMOTE, subText = event.fromName)
            is NodeEvent.MediaUpdate -> {
                Hub.media[event.from] = event.state
                MediaNotifier.update(context, event.from, event.fromName, event.state)
            }
            is NodeEvent.TransferProgress -> TransferNotifier.update(context, event.info)
            is NodeEvent.Unpaired -> Notifications.post(context, "Unpaired", "${event.name} no longer trusts this phone")
            else -> {}
        }
    }

    override fun log(msg: String) {
        Log.i("Tether", msg)
    }
}
