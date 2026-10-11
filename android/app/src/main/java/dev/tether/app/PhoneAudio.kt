package dev.tether.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.widget.Toast
import dev.tether.R
import dev.tether.core.NodeEvent
import dev.tether.core.RemoteError
import dev.tether.core.reportAudioState
import dev.tether.core.streamAudio
import java.io.IOException
import java.io.InputStream

/**
 * Streams what the phone plays to a computer: Android's playback capture (which needs the
 * user's consent each time) feeds raw 48 kHz stereo PCM into a Tether stream.
 */
object PhoneAudio {
    const val RATE = 48_000
    const val CHANNELS = 2
    const val EXTRA_PEER = "peer"
    private const val PREFS = "audio"
    private const val KEY_SILENCE = "silence_phone"
    private const val ID_ASK = 3

    /** The computer we're streaming to, if any. */
    @Volatile
    var streamingTo: String? = null
        internal set

    fun silencePhone(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_SILENCE, true)

    fun setSilencePhone(context: Context, on: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_SILENCE, on).apply()
    }

    /** From one of our activities: asks for consent, then streams to [peer]. */
    fun start(context: Context, peer: String) {
        context.startActivity(Intent(context, AudioCaptureActivity::class.java).putExtra(EXTRA_PEER, peer))
    }

    fun stop(context: Context) {
        if (streamingTo != null) {
            context.startService(Intent(context, AudioStreamService::class.java).setAction(AudioStreamService.ACTION_STOP))
        }
    }

    /** A computer asked for (or no longer wants) the phone's audio. */
    fun onControl(context: Context, e: NodeEvent.AudioControl) {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (!e.on) {
            nm?.cancel(ID_ASK)
            if (streamingTo == e.from) stop(context)
            return
        }
        if (streamingTo == e.from) return
        val open = Intent(context, AudioCaptureActivity::class.java)
            .putExtra(EXTRA_PEER, e.from)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        // The consent dialog needs an activity; with "display over other apps" we may start one
        // from the background, otherwise the user taps a notification.
        if (Settings.canDrawOverlays(context)) {
            runCatching { context.startActivity(open) }.onSuccess { return }
        }
        val n = Notification.Builder(context, Notifications.CH_REMOTE)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(context.md(R.color.md_primary))
            .setContentTitle("Play this phone's audio on ${e.fromName}?")
            .setContentText("Tap to start")
            .setAutoCancel(true)
            .setContentIntent(
                PendingIntent.getActivity(
                    context, e.from.hashCode(), open,
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
            )
            .build()
        try {
            nm?.notify(ID_ASK, n)
        } catch (ex: SecurityException) {
            // notification permission not granted
        }
    }

    internal fun cancelAsk(context: Context) {
        context.getSystemService(NotificationManager::class.java)?.cancel(ID_ASK)
    }
}

/** Invisible: gets the audio permission and the capture consent, then hands over to the service. */
class AudioCaptureActivity : Activity() {
    private var peer: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        peer = intent?.getStringExtra(PhoneAudio.EXTRA_PEER)
        PhoneAudio.cancelAsk(this)
        if (savedInstanceState != null) return // a result is on its way
        if (peer == null || Hub.node?.isConnected(peer!!) != true) return fail("That computer isn't connected")
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_PERMISSION)
        } else {
            askConsent()
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            askConsent()
        } else {
            fail("Tether needs the audio permission to capture what the phone plays")
        }
    }

    private fun askConsent() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val intent = if (Build.VERSION.SDK_INT >= 34) {
            // Whole device, not one app: playback capture works on the device's mix.
            mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
        } else {
            mpm.createScreenCaptureIntent()
        }
        startActivityForResult(intent, REQ_CONSENT)
    }

    @Deprecated("Activity result API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CONSENT) return
        if (resultCode != RESULT_OK || data == null) return fail(null)
        startForegroundService(
            Intent(this, AudioStreamService::class.java)
                .setAction(AudioStreamService.ACTION_START)
                .putExtra(PhoneAudio.EXTRA_PEER, peer)
                .putExtra(AudioStreamService.EXTRA_CODE, resultCode)
                .putExtra(AudioStreamService.EXTRA_DATA, data),
        )
        finish()
        overridePendingTransition(0, 0)
    }

    private fun fail(msg: String?) {
        if (msg != null) Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
        val node = Hub.node
        val p = peer
        if (node != null && p != null) node.pool.execute { node.reportAudioState(p, false) }
        finish()
        overridePendingTransition(0, 0)
    }

    companion object {
        private const val REQ_PERMISSION = 1
        private const val REQ_CONSENT = 2
    }
}

/** Captures the device's playback and streams it while a foreground notification is up. */
class AudioStreamService : Service() {
    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var record: AudioRecord? = null
    private var worker: Thread? = null
    private var mutedByUs = false
    private var next: Intent? = null // a newer consent, started once the current capture ends

    @Volatile
    private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> begin(intent)
            ACTION_STOP -> if (worker == null) stopSelf() else halt()
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        halt()
        super.onDestroy()
    }

    private fun begin(intent: Intent) {
        val peer = intent.getStringExtra(PhoneAudio.EXTRA_PEER)
        val code = intent.getIntExtra(EXTRA_CODE, 0)
        @Suppress("DEPRECATION")
        val data = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        } else {
            intent.getParcelableExtra(EXTRA_DATA)
        }
        val node = Hub.node
        val name = peer?.let { node?.peers?.get(it)?.name } ?: "your computer"
        // Android requires the foreground service (of type mediaProjection) before the projection.
        startForeground(
            Notifications.ID_AUDIO, buildNotification(name),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
        )
        if (worker != null) {
            // Already streaming (perhaps elsewhere): a fresh consent replaces the old capture.
            next = intent
            halt()
            return
        }
        if (peer == null || data == null || node == null) return finishWith(null, null)
        val mp = try {
            getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data)
        } catch (e: Exception) {
            null
        } ?: return finishWith(peer, "Couldn't start capturing audio")
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            // The user revoked it (e.g. from the status bar). Ignore a stale projection's late stop.
            override fun onStop() {
                if (projection === mp) halt()
            }
        }, main)
        val rec = try {
            createRecord(mp)
        } catch (e: Exception) {
            null
        } ?: return finishWith(peer, "Couldn't capture this phone's audio")
        record = rec
        rec.startRecording()
        silence()
        stopping = false
        PhoneAudio.streamingTo = peer
        Hub.dispatch(NodeEvent.StatusChanged)
        worker = Thread({ stream(peer, rec) }, "tether-audio").apply { start() }
    }

    @SuppressLint("MissingPermission") // AudioCaptureActivity obtained RECORD_AUDIO
    private fun createRecord(mp: MediaProjection): AudioRecord {
        val config = AudioPlaybackCaptureConfiguration.Builder(mp)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .build()
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(PhoneAudio.RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
            .build()
        val min = AudioRecord.getMinBufferSize(PhoneAudio.RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT)
        return AudioRecord.Builder()
            .setAudioFormat(format)
            .setBufferSizeInBytes(maxOf(min * 2, FRAME_BYTES * 10))
            .setAudioPlaybackCaptureConfig(config)
            .build()
            .also { if (it.state != AudioRecord.STATE_INITIALIZED) throw IOException("AudioRecord not initialized") }
    }

    private fun stream(peer: String, rec: AudioRecord) {
        var error: String? = null
        try {
            val node = Hub.node ?: throw IOException("Tether isn't running")
            node.streamAudio(peer, PcmStream(rec), PhoneAudio.RATE, PhoneAudio.CHANNELS)
        } catch (e: RemoteError) {
            error = when (e.code) {
                "cancelled" -> null // the computer stopped it
                "denied" -> "The computer has phone audio turned off"
                "unsupported" -> "That computer can't play phone audio"
                else -> "Audio stopped: ${e.message}"
            }
        } catch (e: IOException) {
            if (!stopping) error = "Audio stopped: ${e.message}"
        }
        main.post { finishWith(peer, error) }
    }

    /** Asks the capture to end; the worker notices and cleans up. */
    private fun halt() {
        stopping = true
        runCatching { record?.stop() } // unblocks a pending read
    }

    private fun finishWith(peer: String?, error: String?) {
        stopping = true
        worker = null
        record?.let { runCatching { it.stop() }; it.release() }
        record = null
        projection?.let { runCatching { it.stop() } }
        projection = null
        unsilence()
        PhoneAudio.streamingTo = null
        Hub.dispatch(NodeEvent.StatusChanged)
        val node = Hub.node
        if (peer != null && node != null) node.pool.execute { node.reportAudioState(peer, false) }
        if (error != null) Toast.makeText(this, error, Toast.LENGTH_LONG).show()
        next?.let {
            next = null
            begin(it)
            return
        }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun silence() {
        if (!PhoneAudio.silencePhone(this)) return
        val am = getSystemService(AudioManager::class.java) ?: return
        if (!am.isStreamMute(AudioManager.STREAM_MUSIC)) {
            am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_MUTE, 0)
            mutedByUs = true
        }
    }

    private fun unsilence() {
        if (!mutedByUs) return
        mutedByUs = false
        val am = getSystemService(AudioManager::class.java) ?: return
        am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_UNMUTE, 0)
    }

    private fun buildNotification(computer: String): Notification {
        val stop = PendingIntent.getService(
            this, 0, Intent(this, AudioStreamService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, Notifications.CH_AUDIO)
            .setSmallIcon(R.drawable.ic_volume)
            .setColor(md(R.color.md_primary))
            .setContentTitle("Playing on $computer")
            .setContentText("This phone's audio is streaming to your computer")
            .setOngoing(true)
            .setContentIntent(Notifications.openApp(this))
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    /** Reads the capture in 20 ms pieces, so each one goes out as soon as it's recorded. */
    private inner class PcmStream(private val rec: AudioRecord) : InputStream() {
        override fun read(): Int = throw UnsupportedOperationException()

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (stopping) return -1
            val n = rec.read(b, off, minOf(len, FRAME_BYTES), AudioRecord.READ_BLOCKING)
            if (stopping) return -1
            if (n < 0) throw IOException("capture failed ($n)")
            return n
        }
    }

    companion object {
        const val ACTION_START = "dev.tether.AUDIO_START"
        const val ACTION_STOP = "dev.tether.AUDIO_STOP"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val FRAME_BYTES = PhoneAudio.RATE / 50 * PhoneAudio.CHANNELS * 2 // 20 ms
    }
}
