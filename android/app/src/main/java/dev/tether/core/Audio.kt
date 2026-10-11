package dev.tether.core

import java.io.InputStream

/**
 * Streams raw signed 16-bit little-endian PCM from [source] to a computer, which plays it.
 * Blocks until [source] ends, the computer stops it (RemoteError "cancelled"), or the
 * connection drops.
 */
fun Node.streamAudio(peerId: String, source: InputStream, rate: Int, channels: Int) {
    val s = session(peerId) ?: throw RemoteError("not_found", "not connected")
    s.channel.request(
        jobj("t" to "audio", "rate" to rate, "channels" to channels, "format" to "s16le"),
        upload = source,
        uploadLength = -1,
        timeoutMs = 10_000,
    )
}

/** Tells a computer whether we're streaming to it (so it can stop waiting after a refusal). */
fun Node.reportAudioState(peerId: String, on: Boolean) {
    session(peerId)?.channel?.trySend(jobj("t" to "audio_state", "on" to on))
}
