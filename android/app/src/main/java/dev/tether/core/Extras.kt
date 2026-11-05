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
