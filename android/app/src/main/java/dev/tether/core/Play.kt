package dev.tether.core

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
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

/**
 * Asks a computer to play what's at a web link itself. True if it had to open the link in its
 * browser instead; RemoteError "not_playable" if it couldn't do that either.
 */
fun Node.playUrl(peerId: String, url: String): Boolean {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    return s.channel.request(jobj("t" to "play_url", "url" to url), timeoutMs = 45_000).msg.bool("browser") == true
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

data class MusicEntry(val name: String, val dir: Boolean, val size: Long)

/** What the computer's music player is doing. Positions and lengths are in seconds. */
data class MusicState(
    val playing: Boolean,
    val title: String?,
    val artist: String?,
    val paused: Boolean,
    val position: Double,
    val length: Double,
    val volume: Double,
    val count: Int,
)

/** A folder of the computer's music; folders first, then tracks. */
fun Node.musicList(peerId: String, path: String): List<MusicEntry> {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    val res = s.channel.request(jobj("t" to "music_list", "path" to path))
    return res.msg.arr("entries").orEmpty().mapNotNull { it as? JsonObject }.map {
        MusicEntry(it.str("name").orEmpty(), it.bool("dir") == true, it.long("size") ?: 0L)
    }
}

/** Plays a track, followed by the rest of its folder, or everything inside a folder. */
fun Node.musicPlay(peerId: String, path: String) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    s.channel.request(jobj("t" to "music_play", "path" to path))
}

fun Node.musicState(peerId: String): MusicState {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    return parseMusicState(s.channel.request(jobj("t" to "music_state")).msg)
}

/** Actions: toggle, next, previous, stop, position (seconds), volume (0-100). */
fun Node.musicCommand(peerId: String, action: String, value: Double? = null): MusicState {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    return parseMusicState(s.channel.request(jobj("t" to "music_cmd", "action" to action, "value" to value)).msg)
}

private fun parseMusicState(m: JsonObject): MusicState {
    fun number(k: String) = (m[k] as? JsonPrimitive)?.doubleOrNull
    return MusicState(
        playing = m.bool("playing") == true,
        title = m.str("title"),
        artist = m.str("artist"),
        paused = m.bool("paused") == true,
        position = number("position") ?: 0.0,
        length = number("length") ?: 0.0,
        volume = number("volume") ?: 100.0,
        count = number("count")?.toInt() ?: 1,
    )
}

/** Asks to use the computer's mouse and keyboard. RemoteError "denied" while it has that turned off. */
fun Node.remoteOpen(peerId: String) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    s.channel.request(jobj("t" to "remote_open"))
}

/** The computer's output volume (0–1). */
fun Node.remoteVolume(peerId: String): Double? {
    val s = session(peerId) ?: return null
    return (s.channel.request(jobj("t" to "remote_volume")).msg["volume"] as? JsonPrimitive)?.doubleOrNull
}

fun Node.remoteClose(peerId: String) {
    session(peerId)?.channel?.trySend(jobj("t" to "remote_close"))
}

fun Node.remoteMove(peerId: String, dx: Float, dy: Float) {
    session(peerId)?.channel?.trySend(jobj("t" to "remote", "kind" to "move", "dx" to dx.toDouble(), "dy" to dy.toDouble()))
}

fun Node.remoteClick(peerId: String, button: String) {
    session(peerId)?.channel?.trySend(jobj("t" to "remote", "kind" to "click", "button" to button))
}

fun Node.remoteScroll(peerId: String, steps: Int) {
    session(peerId)?.channel?.trySend(jobj("t" to "remote", "kind" to "scroll", "steps" to steps))
}

fun Node.remoteKey(peerId: String, key: String) {
    session(peerId)?.channel?.trySend(jobj("t" to "remote", "kind" to "key", "key" to key))
}

/** Puts the computer to sleep. */
fun Node.suspendComputer(peerId: String) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    s.channel.request(jobj("t" to "suspend"))
}
