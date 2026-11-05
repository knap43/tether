package dev.tether.app

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.Display
import android.view.Surface
import dev.tether.R
import dev.tether.core.NodeEvent
import dev.tether.core.RemoteError
import dev.tether.core.streamScreen
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.roundToInt

/** Shows the phone's screen on a computer. The capture itself lives in [AudioStreamService]. */
object PhoneScreen {
    private const val ID_ASK = 4

    /** The computer we're streaming to, if any. */
    @Volatile
    var streamingTo: String? = null
        internal set

    /** From one of our activities: asks for consent, then streams to [peer]. */
    fun start(context: Context, peer: String) {
        context.startActivity(captureIntent(context, peer))
    }

    fun stop(context: Context) {
        if (streamingTo != null) {
            context.startService(Intent(context, AudioStreamService::class.java).setAction(AudioStreamService.ACTION_STOP))
        }
    }

    /** A computer asked for (or no longer wants) the phone's screen. */
    fun onControl(context: Context, e: NodeEvent.ScreenControl) {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (!e.on) {
            nm?.cancel(ID_ASK)
            if (streamingTo == e.from) stop(context)
            return
        }
        if (streamingTo == e.from) return
        val open = captureIntent(context, e.from).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        // The consent dialog needs an activity; with "display over other apps" we may start one
        // from the background, otherwise the user taps a notification.
        if (Settings.canDrawOverlays(context)) {
            runCatching { context.startActivity(open) }.onSuccess { return }
        }
        val n = Notification.Builder(context, Notifications.CH_REMOTE)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(context.md(R.color.md_primary))
            .setContentTitle("Show this phone's screen on ${e.fromName}?")
            .setContentText("Tap to start")
            .setAutoCancel(true)
            .setContentIntent(
                PendingIntent.getActivity(
                    context, e.from.hashCode() + 1, open,
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

    private fun captureIntent(context: Context, peer: String) =
        Intent(context, AudioCaptureActivity::class.java)
            .putExtra(PhoneAudio.EXTRA_PEER, peer)
            .putExtra(PhoneAudio.EXTRA_SCREEN, true)
}

/**
 * Encodes the display as H.264 and sends it to a computer. Frames that a slow network can't take
 * are dropped whole, down to the next keyframe, so the picture stays current rather than falling
 * ever further behind. Turning the phone rebuilds the encoder at the new shape; the keyframe that
 * follows carries the new size in the stream.
 */
internal class ScreenStream(
    context: Context,
    private val projection: MediaProjection,
    private val peer: String,
    private val onEnded: (String?) -> Unit,
) {
    private val main = Handler(Looper.getMainLooper())
    private val displays = context.getSystemService(DisplayManager::class.java)
    private val frames = LinkedBlockingQueue<ByteArray>()
    private val queuedBytes = AtomicInteger()
    private val source = FrameStream()
    private val log: (String) -> Unit = AndroidPlatform(context.applicationContext)::log

    private var encoder: MediaCodec? = null
    private var surface: Surface? = null
    private var display: VirtualDisplay? = null
    private var size = 0 to 0
    private var config: ByteArray? = null // SPS and PPS, sent ahead of every keyframe
    private var awaitingKey = true
    private var encoderThread: Thread? = null
    private var senderThread: Thread? = null

    @Volatile
    private var stopped = false

    @Volatile
    private var wantKey = false

    @Volatile
    private var rotated = false

    @Volatile
    private var ended = false

    private val displayListener = object : DisplayManager.DisplayListener {
        override fun onDisplayChanged(id: Int) {
            if (id == Display.DEFAULT_DISPLAY) rotated = true
        }

        override fun onDisplayAdded(id: Int) = Unit

        override fun onDisplayRemoved(id: Int) = Unit
    }

    /** Starts capturing; throws if this phone can't encode its screen. */
    fun start() {
        val metrics = metrics()
        val (enc, input, encSize) = createEncoder(metrics)
        encoder = enc
        surface = input
        size = encSize
        try {
            display = projection.createVirtualDisplay(
                "tether-screen", encSize.first, encSize.second, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, input, null, null,
            ) ?: throw IOException("no virtual display")
        } catch (e: Exception) {
            release()
            throw e
        }
        displays.registerDisplayListener(displayListener, main)
        encoderThread = Thread(::encodeLoop, "tether-screen-enc").apply { start() }
        senderThread = Thread(::sendLoop, "tether-screen").apply { start() }
    }

    /** Asks the stream to end; [onEnded] follows. */
    fun stop() {
        stopped = true
    }

    private fun metrics() = DisplayMetrics().also {
        @Suppress("DEPRECATION")
        displays.getDisplay(Display.DEFAULT_DISPLAY).getRealMetrics(it)
    }

    /** An encoder for the display's shape, at the largest size this phone's encoder supports. */
    private fun createEncoder(m: DisplayMetrics): Triple<MediaCodec, Surface, Pair<Int, Int>> {
        var last: Exception? = null
        for (edge in LONG_EDGES) {
            val enc = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            try {
                val caps = enc.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC).videoCapabilities
                val scale = minOf(1.0, edge.toDouble() / maxOf(m.widthPixels, m.heightPixels))
                val w = aligned(m.widthPixels * scale, maxOf(caps.widthAlignment, 2))
                val h = aligned(m.heightPixels * scale, maxOf(caps.heightAlignment, 2))
                if (!caps.isSizeSupported(w, h)) {
                    enc.release()
                    continue
                }
                val format = format(w, h)
                // High profile holds more detail at the same bitrate, where the encoder has it.
                enc.codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC).profileLevels
                    .filter { it.profile == MediaCodecInfo.CodecProfileLevel.AVCProfileHigh }
                    .maxByOrNull { it.level }
                    ?.let {
                        format.setInteger(MediaFormat.KEY_PROFILE, it.profile)
                        format.setInteger(MediaFormat.KEY_LEVEL, it.level)
                    }
                enc.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                val input = enc.createInputSurface()
                enc.start()
                return Triple(enc, input, w to h)
            } catch (e: Exception) { // some encoders claim sizes they then refuse; try the next one down
                last = e
                enc.release()
            }
        }
        throw IOException("no encoder for this screen", last)
    }

    private fun aligned(v: Double, alignment: Int) = (v / alignment).roundToInt() * alignment

    private fun format(w: Int, h: Int) = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h).apply {
        setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
        setInteger(MediaFormat.KEY_BIT_RATE, (w * h * BITS_PER_PIXEL).coerceIn(MIN_BITRATE, MAX_BITRATE))
        setInteger(MediaFormat.KEY_FRAME_RATE, FRAME_RATE)
        setFloat(MediaFormat.KEY_MAX_FPS_TO_ENCODER, FRAME_RATE.toFloat())
        setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, KEYFRAME_SECONDS)
        setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0) // reordering would only add delay
        setInteger(MediaFormat.KEY_PRIORITY, 0) // real time
        // A still screen produces no frames; keep some coming, so a viewer starts and stays current.
        setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, REPEAT_AFTER_US)
    }

    private fun encodeLoop() {
        val info = MediaCodec.BufferInfo()
        var error: String? = null
        try {
            while (!stopped) {
                if (rotated) follow()
                if (wantKey) {
                    wantKey = false
                    encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
                }
                val enc = encoder ?: break
                val i = enc.dequeueOutputBuffer(info, DEQUEUE_US)
                if (i < 0) continue
                val buffer = enc.getOutputBuffer(i)
                if (buffer != null && info.size > 0) {
                    buffer.position(info.offset).limit(info.offset + info.size)
                    val data = ByteArray(info.size).also { buffer.get(it) }
                    take(data, info.flags)
                }
                enc.releaseOutputBuffer(i, false)
            }
        } catch (e: Exception) { // the codec died under us
            if (!stopped) {
                log("screen: encoder failed ($e)")
                error = "The screen capture stopped"
            }
        }
        stopped = true
        finish(error)
    }

    private fun take(data: ByteArray, flags: Int) {
        if (flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
            config = data
            return
        }
        if (source.restarted.getAndSet(0) != 0) awaitingKey = true
        val behind = frames.size >= MAX_QUEUED_FRAMES || queuedBytes.get() >= MAX_QUEUED_BYTES
        if (behind) source.clear()
        if (flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0) {
            awaitingKey = false
            enqueue((config ?: ByteArray(0)) + data)
            return
        }
        if (behind) {
            awaitingKey = true
            wantKey = true
        }
        if (!awaitingKey) enqueue(data)
    }

    private fun enqueue(data: ByteArray) {
        queuedBytes.addAndGet(data.size)
        frames.put(data)
    }

    /** The display may have turned: moves the capture to an encoder of the new shape. */
    private fun follow() {
        rotated = false
        val m = metrics()
        val landscape = m.widthPixels > m.heightPixels
        if (landscape == size.first > size.second) return
        // The old encoder goes first: some phones have hardware for only one at this size.
        display?.surface = null
        encoder?.let {
            runCatching { it.stop() }
            it.release()
        }
        encoder = null
        surface?.release()
        surface = null
        config = null
        val (enc, input, encSize) = createEncoder(m)
        encoder = enc
        surface = input
        size = encSize
        display?.resize(encSize.first, encSize.second, m.densityDpi)
        display?.surface = input
        log("screen: now ${encSize.first}x${encSize.second}")
    }

    private fun sendLoop() {
        var error: String? = null
        while (!stopped) {
            val node = Hub.node ?: break
            try {
                source.restart()
                node.streamScreen(peer, source)
                break // ended on our side
            } catch (e: RemoteError) {
                error = when (e.code) {
                    "cancelled" -> null // the computer stopped it
                    "denied" -> "The computer has showing this phone's screen turned off"
                    "unsupported" -> "That computer can't show this phone's screen"
                    "not_found" -> if (resume(node)) continue else lost(node)
                    else -> "Screen stopped: ${e.message}"
                }
                break
            } catch (e: IOException) {
                if (stopped) break
                // The connection dropped: carry on over the next one rather than giving up.
                log("screen: connection lost (${node.lastCloseReason(peer) ?: e.message}); reconnecting")
                if (resume(node)) continue
                error = lost(node)
                break
            }
        }
        stopped = true
        finish(error)
    }

    private fun lost(node: dev.tether.core.Node) =
        "Lost the connection to the computer" + (node.lastCloseReason(peer)?.let { " ($it)" } ?: "")

    private fun resume(node: dev.tether.core.Node): Boolean {
        val deadline = System.currentTimeMillis() + RESUME_MS
        var asked = false
        while (!stopped && System.currentTimeMillis() < deadline) {
            if (node.isConnected(peer)) return true
            if (!asked) {
                asked = true
                node.pool.execute { runCatching { node.reconnectAll() } }
            }
            Thread.sleep(RESUME_POLL_MS)
        }
        return false
    }

    /** The first error wins; the second of the two threads to end tidies up and reports. */
    @Synchronized
    private fun finish(error: String?) {
        if (firstError == null) firstError = error
        if (ended) {
            main.post {
                release()
                onEnded(firstError)
            }
        }
        ended = true
    }

    private var firstError: String? = null

    private fun release() {
        runCatching { displays.unregisterDisplayListener(displayListener) }
        runCatching { display?.release() }
        display = null
        encoder?.let {
            runCatching { it.stop() }
            it.release()
        }
        encoder = null
        surface?.release()
        surface = null
    }

    /** Hands the encoder's frames to the channel, keeping each one whole in the stream. */
    private inner class FrameStream : InputStream() {
        val restarted = AtomicInteger()
        private var current: ByteArray? = null
        private var pos = 0

        /** A new connection needs a fresh start: its first bytes must be a keyframe. */
        fun restart() {
            current = null
            clear()
            restarted.set(1)
            wantKey = true
        }

        fun clear() {
            while (true) {
                val f = frames.poll() ?: return
                queuedBytes.addAndGet(-f.size)
            }
        }

        override fun read(): Int = throw UnsupportedOperationException()

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            var frame = current
            while (frame == null) {
                if (stopped) return -1
                frame = frames.poll(POLL_MS, TimeUnit.MILLISECONDS)
                if (frame != null) {
                    queuedBytes.addAndGet(-frame.size)
                    pos = 0
                }
            }
            if (stopped) return -1
            val n = minOf(len, frame.size - pos)
            System.arraycopy(frame, pos, b, off, n)
            pos += n
            current = if (pos < frame.size) frame else null
            return n
        }
    }

    private companion object {
        val LONG_EDGES = intArrayOf(1920, 1600, 1280, 1024, 800)
        const val MIN_BITRATE = 2_000_000
        const val MAX_BITRATE = 10_000_000
        const val BITS_PER_PIXEL = 6
        const val FRAME_RATE = 30 // films need no more, and a phone already busy decoding one is spared the rest
        const val KEYFRAME_SECONDS = 2
        const val REPEAT_AFTER_US = 100_000L
        const val DEQUEUE_US = 50_000L
        const val POLL_MS = 100L
        const val MAX_QUEUED_FRAMES = 15 // half a second
        const val MAX_QUEUED_BYTES = 1024 * 1024
        const val RESUME_MS = 45_000L
        const val RESUME_POLL_MS = 200L
    }
}
