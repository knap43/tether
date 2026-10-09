package dev.tether.core

import kotlinx.serialization.json.JsonObject
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.atomic.AtomicInteger

// -- transfers ---------------------------------------------------------------------------

data class TransferInfo(
    val id: Int,
    val name: String,
    val total: Long?,
    val done: Long,
    val incoming: Boolean,
    val peerName: String,
    val state: String, // active | done | failed | cancelled
)

class Transfer internal constructor(
    val id: Int,
    val name: String,
    val total: Long?,
    val incoming: Boolean,
    val peerName: String,
    private val emit: (TransferInfo) -> Unit,
) {
    @Volatile
    var done = 0L
        private set

    @Volatile
    var cancelled = false
        private set

    @Volatile
    var state = "active"
        private set

    @Volatile
    internal var stream: Incoming? = null
    private var lastEmit = 0L

    fun info() = TransferInfo(id, name, total, done, incoming, peerName, state)

    fun add(n: Long) {
        if (cancelled) throw RemoteError("cancelled")
        done += n
        val now = System.currentTimeMillis()
        if (now - lastEmit >= 250) {
            lastEmit = now
            emit(info())
        }
    }

    fun cancel() {
        cancelled = true
        stream?.abort()
    }

    internal fun finish(result: String) {
        if (state != "active") return
        state = if (cancelled) "cancelled" else result
        emit(info())
    }

    companion object {
        private val ids = AtomicInteger(1)
        internal fun nextId() = ids.getAndIncrement()
    }
}

/** Counts bytes read into a transfer and stops with an error once it is cancelled. */
class ProgressInputStream(input: InputStream, private val transfer: Transfer) : FilterInputStream(input) {
    override fun read(): Int {
        val b = super.read()
        if (b >= 0) transfer.add(1)
        return b
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
        if (transfer.cancelled) throw IOException("cancelled")
        val n = super.read(b, off, len)
        if (n > 0) transfer.add(n.toLong())
        return n
    }
}

// -- phone notifications -----------------------------------------------------------------

data class PhoneNotification(
    val key: String,
    val app: String,
    val appName: String,
    val title: String,
    val text: String,
    val time: Long,
    val canReply: Boolean,
    val iconPngBase64: String?,
)

// -- media on the computer ----------------------------------------------------------------

data class MediaPlayerInfo(
    val name: String,
    val identity: String,
    val status: String,
    val title: String,
    val artist: String,
    val album: String,
    val lengthMs: Long?,
    val positionMs: Long?,
    val sampledAt: Long,
) {
    val playing get() = status == "Playing"

    /** Position now, extrapolated from when it was sampled. */
    fun positionNow(now: Long = System.currentTimeMillis()): Long? {
        val p = positionMs ?: return null
        val moved = if (playing) (now - sampledAt).coerceAtLeast(0) else 0
        return lengthMs?.let { minOf(p + moved, it) } ?: (p + moved)
    }
}

data class MediaState(val players: List<MediaPlayerInfo>, val volume: Double?, val muted: Boolean, val available: Boolean) {
    val active: MediaPlayerInfo? get() = players.firstOrNull()

    companion object {
        fun parse(o: JsonObject, receivedAt: Long = System.currentTimeMillis()): MediaState {
            val players = o.arr("players")?.mapNotNull { e ->
                val p = e.asObj() ?: return@mapNotNull null
                MediaPlayerInfo(
                    p.str("name") ?: return@mapNotNull null,
                    p.str("identity") ?: "",
                    p.str("status") ?: "Stopped",
                    p.str("title") ?: "",
                    p.str("artist") ?: "",
                    p.str("album") ?: "",
                    p.long("length"),
                    p.long("position"),
                    receivedAt, // the computer's clock may differ; time from arrival
                )
            } ?: emptyList()
            val vol = o.obj("volume")
            val level = (vol?.get("level") as? kotlinx.serialization.json.JsonPrimitive)?.content?.toDoubleOrNull()
            return MediaState(players, level, vol?.bool("muted") == true, o.bool("available") != false)
        }
    }
}

/** Controls the players on a connected computer. */
class RemoteMedia(private val node: Node, val peerId: String) {
    private fun ch(): Channel = node.session(peerId)?.channel ?: throw IOException("computer not connected")

    fun state(): MediaState = MediaState.parse(ch().request(jobj("t" to "media_state")).msg)

    /** action: play_pause, play, pause, next, previous, stop, seek (value = seconds), position (ms), volume (0..1), mute */
    fun command(action: String, player: String? = null, value: Number? = null): MediaState {
        val msg = jobj("t" to "media_cmd", "action" to action, "player" to player, "value" to value)
        return MediaState.parse(ch().request(msg).msg)
    }
}
