package dev.tether.core

import kotlinx.serialization.json.JsonObject
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicInteger

const val CHUNK = 64 * 1024
private const val QUEUE_DEPTH = 64
private const val PING_INTERVAL_MS = 30_000L
private const val IDLE_TIMEOUT_MS = 90_000
private val EMPTY = ByteArray(0)
private val CLOSED = Any()
private val ABORTED = Any()

/** Messages whose "stream" field announces data flowing to us. */
private val INBOUND_STREAM_TYPES = setOf("send", "fs_write", "res", "clip_image")

/** An error that travels over the wire as a code. */
open class RemoteError(val code: String, detail: String = "") :
    IOException(if (detail.isEmpty()) code else "$code: $detail")

class StreamAborted : IOException("stream aborted by sender")

class Response(val msg: JsonObject, val incoming: Incoming?)

/** Receiving end of a stream. */
class Incoming internal constructor(private val ch: Channel, val sid: Int) {
    internal val queue = ArrayBlockingQueue<Any>(QUEUE_DEPTH)

    @Volatile
    var done = false
        private set

    /** Next chunk; an empty array marks the end of the stream. */
    fun read(): ByteArray {
        if (done) return EMPTY
        val item = queue.take()
        if (item === CLOSED) {
            done = true
            throw IOException("connection closed")
        }
        if (item === ABORTED) {
            done = true
            throw StreamAborted()
        }
        val b = item as ByteArray
        if (b.isEmpty()) done = true
        return b
    }

    fun copyTo(out: OutputStream, onChunk: ((ByteArray) -> Unit)? = null): Long {
        var total = 0L
        while (true) {
            val b = read()
            if (b.isEmpty()) return total
            out.write(b)
            onChunk?.invoke(b)
            total += b.size
        }
    }

    /** Stop receiving; the rest of the stream is discarded. */
    fun abort() {
        if (done) return
        done = true
        if (ch.streams.remove(sid) != null) {
            ch.discard.add(sid)
            if (!ch.closed) ch.trySend(jobj("t" to "cancel", "stream" to sid))
        }
        queue.clear() // unblocks the reader thread if it is waiting to enqueue
    }
}

typealias Handler = (Channel, JsonObject, Incoming?) -> Unit

class Channel(
    private val framer: Framer,
    isClient: Boolean,
    private val socket: Socket,
    val label: String,
    private val pool: ExecutorService,
    private val inlineTypes: Set<String>,
    private val log: (String) -> Unit,
    private val handler: Handler,
) {
    private var nextStream = if (isClient) 1 else 2
    private val nextReq = AtomicInteger(1)
    private val pending = ConcurrentHashMap<Int, CompletableFuture<Response>>()
    internal val streams = ConcurrentHashMap<Int, Incoming>()
    internal val discard: MutableSet<Int> = ConcurrentHashMap.newKeySet()
    private val cancelled: MutableSet<Int> = ConcurrentHashMap.newKeySet()
    private val closeListeners = CopyOnWriteArrayList<() -> Unit>()

    @Volatile
    var closed = false
        private set

    fun onClose(fn: () -> Unit) {
        closeListeners.add(fn)
        if (closed) fn()
    }

    fun newStream(): Int = synchronized(this) {
        val s = nextStream
        nextStream += 2
        s
    }

    fun send(msg: JsonObject) {
        if (closed) throw IOException("connection closed")
        framer.send(byteArrayOf(KIND_JSON) + msg.toString().toByteArray(Charsets.UTF_8))
    }

    fun trySend(msg: JsonObject): Boolean = try {
        send(msg)
        true
    } catch (e: IOException) {
        false
    }

    private fun sendChunk(sid: Int, data: ByteArray, off: Int, len: Int) {
        val buf = ByteArray(5 + len)
        buf[0] = KIND_DATA
        buf[1] = (sid ushr 24).toByte()
        buf[2] = (sid ushr 16).toByte()
        buf[3] = (sid ushr 8).toByte()
        buf[4] = sid.toByte()
        System.arraycopy(data, off, buf, 5, len)
        framer.send(buf)
    }

    /** Sends `length` bytes (or everything until EOF if negative). False if the receiver cancelled. */
    fun sendStream(sid: Int, input: InputStream, length: Long): Boolean {
        val buf = ByteArray(CHUNK)
        var left = length
        try {
            while (length < 0 || left > 0) {
                if (cancelled.remove(sid)) return false
                val want = if (length < 0) CHUNK else minOf(CHUNK.toLong(), left).toInt()
                val n = input.read(buf, 0, want)
                if (n < 0) {
                    if (length >= 0) throw IOException("source ended early")
                    break
                }
                if (n == 0) continue
                sendChunk(sid, buf, 0, n)
                if (length >= 0) left -= n
            }
        } catch (e: IOException) {
            if (!closed) trySend(jobj("t" to "abort", "stream" to sid))
            throw e
        }
        sendChunk(sid, EMPTY, 0, 0)
        return true
    }

    fun request(
        msg: JsonObject,
        upload: InputStream? = null,
        uploadLength: Long = -1,
        timeoutMs: Long = 60_000,
    ): Response {
        val req = nextReq.getAndIncrement()
        val fut = CompletableFuture<Response>()
        pending[req] = fut
        try {
            if (upload != null) {
                val sid = newStream()
                send(msg.with("req" to req, "stream" to sid))
                if (!sendStream(sid, upload, uploadLength)) {
                    val early = fut.getNow(null)
                    throw RemoteError(early?.msg?.str("error") ?: "cancelled")
                }
            } else {
                send(msg.with("req" to req))
            }
            val res = try {
                fut.get(timeoutMs, TimeUnit.MILLISECONDS)
            } catch (e: TimeoutException) {
                throw IOException("request timed out")
            } catch (e: ExecutionException) {
                throw (e.cause as? IOException) ?: IOException(e.cause)
            } catch (e: InterruptedException) {
                throw IOException("interrupted")
            }
            if (res.msg.bool("ok") != true) {
                res.incoming?.abort()
                throw RemoteError(res.msg.str("error") ?: "io")
            }
            return res
        } finally {
            pending.remove(req)
        }
    }

    fun reply(req: JsonObject, vararg fields: Pair<String, Any?>) {
        send(jobj("t" to "res", "req" to req["req"], "ok" to true).with(*fields))
    }

    fun replyError(req: JsonObject, code: String) {
        if (req.containsKey("req")) trySend(jobj("t" to "res", "req" to req["req"], "ok" to false, "error" to code))
    }

    fun replyStream(req: JsonObject, input: InputStream, length: Long, vararg fields: Pair<String, Any?>) {
        val sid = newStream()
        reply(req, "stream" to sid, *fields)
        sendStream(sid, input, length)
    }

    /** Runs the receive loop on the calling thread until the connection ends. */
    fun run() {
        val pinger = Thread {
            try {
                while (!closed) {
                    Thread.sleep(PING_INTERVAL_MS)
                    trySend(jobj("t" to "ping"))
                }
            } catch (e: InterruptedException) {
                // shutting down
            }
        }.apply {
            isDaemon = true
            name = "tether-ping"
            start()
        }
        try {
            socket.soTimeout = IDLE_TIMEOUT_MS
            while (true) {
                val pt = framer.recv()
                when (pt[0]) {
                    KIND_JSON -> onMessage(parseObj(String(pt, 1, pt.size - 1, Charsets.UTF_8)))
                    KIND_DATA -> {
                        if (pt.size < 5) throw IOException("short data frame")
                        val sid = ((pt[1].toInt() and 0xff) shl 24) or ((pt[2].toInt() and 0xff) shl 16) or
                            ((pt[3].toInt() and 0xff) shl 8) or (pt[4].toInt() and 0xff)
                        onChunk(sid, pt.copyOfRange(5, pt.size))
                    }
                    else -> throw IOException("unknown frame kind ${pt[0]}")
                }
            }
        } catch (e: Exception) {
            log("$label: closed (${e.message ?: e.javaClass.simpleName})")
        } finally {
            pinger.interrupt()
            shutdown()
        }
    }

    private fun onMessage(msg: JsonObject) {
        val t = msg.str("t")
        val sid = msg.int("stream")
        var inc: Incoming? = null
        if (t in INBOUND_STREAM_TYPES && sid != null) {
            inc = Incoming(this, sid)
            streams[sid] = inc
        }
        when (t) {
            "res" -> {
                val fut = msg.int("req")?.let { pending[it] }
                if (fut != null && !fut.isDone) fut.complete(Response(msg, inc)) else inc?.abort()
            }
            "ping" -> trySend(jobj("t" to "pong"))
            "pong" -> {}
            "cancel" -> if (sid != null) cancelled.add(sid)
            "abort" -> if (sid != null) streams.remove(sid)?.queue?.put(ABORTED)
            else -> if (t in inlineTypes) dispatch(msg, inc) else pool.execute { dispatch(msg, inc) }
        }
    }

    private fun dispatch(msg: JsonObject, inc: Incoming?) {
        try {
            handler(this, msg, inc)
        } catch (e: RemoteError) {
            inc?.abort()
            replyError(msg, e.code)
        } catch (e: Exception) {
            inc?.abort()
            if (!closed) log("$label: handler for ${msg.str("t")} failed: $e")
            replyError(msg, "io")
        }
    }

    private fun onChunk(sid: Int, data: ByteArray) {
        if (discard.contains(sid)) {
            if (data.isEmpty()) discard.remove(sid)
            return
        }
        val inc = streams[sid] ?: return
        if (data.isEmpty()) streams.remove(sid)
        inc.queue.put(data)
    }

    private var finished = false

    fun close() {
        closed = true
        try {
            socket.close()
        } catch (e: IOException) {
            // ignore
        }
    }

    private fun shutdown() {
        synchronized(this) {
            if (finished) return
            finished = true
        }
        close()
        pending.values.forEach { it.completeExceptionally(IOException("connection closed")) }
        streams.values.forEach {
            it.queue.clear()
            it.queue.offer(CLOSED)
        }
        streams.clear()
        closeListeners.forEach { runCatching { it() } }
    }
}
