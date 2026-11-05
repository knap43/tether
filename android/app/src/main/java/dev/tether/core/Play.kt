package dev.tether.core

import kotlinx.serialization.json.JsonObject
import java.io.InputStream
import java.util.concurrent.ConcurrentHashMap

/** A file this phone offers a computer's player. Reads are by position, so seeks may overlap. */
interface PlayableFile {
    val id: String
    val name: String
    val size: Long
    val mime: String?

    /** Reads at [position]; the number of bytes read, or -1 at the end. */
    fun read(position: Long, into: ByteArray, off: Int, len: Int): Int

    fun close()
}

private val playable = ConcurrentHashMap<String, PlayableFile>()

/** Asks a computer to play what's at a web link itself. RemoteError "not_playable" if it can't fetch it. */
fun Node.playUrl(peerId: String, url: String) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    s.channel.request(jobj("t" to "play_url", "url" to url), timeoutMs = 45_000)
}

/** Offers [files] to a computer's player in place of any offered before. */
fun Node.playFiles(peerId: String, files: List<PlayableFile>) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    releasePlayable()
    files.forEach { playable[it.id] = it }
    val listed = files.map { jobj("id" to it.id, "name" to it.name, "size" to it.size, "mime" to it.mime) }
    s.channel.request(jobj("t" to "play_files", "files" to listed), timeoutMs = 15_000)
}

/** The player has finished with the files, so they can be let go. */
fun releasePlayable() {
    val gone = playable.values.toList()
    playable.clear()
    gone.forEach { runCatching { it.close() } }
}

internal fun onPlayRead(ch: Channel, msg: JsonObject) {
    val f = playable[msg.str("id")] ?: throw RemoteError("not_found")
    val offset = (msg.long("offset") ?: 0L).coerceIn(0L, f.size)
    val want = msg.long("length") ?: -1L
    val length = if (want < 0) f.size - offset else minOf(want, f.size - offset)
    val input = object : InputStream() {
        private var position = offset

        override fun read(): Int = throw UnsupportedOperationException()

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            val n = f.read(position, b, off, len)
            if (n > 0) position += n
            return n
        }
    }
    ch.replyStream(msg, input, length, "size" to f.size, "length" to length)
}
