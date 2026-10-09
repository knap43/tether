package dev.tether.core

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/** Client side of the browsing requests, against one connected device. */
class RemoteFs(private val node: Node, val peerId: String) {
    data class Entry(val name: String, val dir: Boolean, val size: Long, val mtime: Long)

    private fun ch(): Channel = node.session(peerId)?.channel ?: throw IOException("device not connected")

    private fun entry(o: kotlinx.serialization.json.JsonObject) =
        Entry(o.str("name") ?: "", o.bool("dir") == true, o.long("size") ?: 0, o.long("mtime") ?: 0)

    fun list(path: String): List<Entry> =
        ch().request(jobj("t" to "fs_list", "path" to path)).msg.arr("entries")
            ?.mapNotNull { it.asObj()?.let(::entry) } ?: emptyList()

    fun stat(path: String): Entry =
        entry(ch().request(jobj("t" to "fs_stat", "path" to path)).msg.obj("entry") ?: throw RemoteError("io"))

    /** Copies [offset, offset+length) (length < 0: to the end) into `out`; returns bytes copied. */
    fun read(path: String, offset: Long, length: Long, out: OutputStream): Long {
        val res = ch().request(jobj("t" to "fs_read", "path" to path, "offset" to offset, "length" to length))
        val inc = res.incoming ?: throw IOException("no stream")
        try {
            return inc.copyTo(out)
        } finally {
            if (!inc.done) inc.abort()
        }
    }

    fun readBytes(path: String, offset: Long, length: Int): ByteArray =
        ByteArrayOutputStream(maxOf(length, 0)).also { read(path, offset, length.toLong(), it) }.toByteArray()

    fun write(path: String, input: InputStream, length: Long = -1) {
        ch().request(jobj("t" to "fs_write", "path" to path), upload = input, uploadLength = length, timeoutMs = 300_000)
    }

    fun mkdir(path: String) {
        ch().request(jobj("t" to "fs_mkdir", "path" to path))
    }

    fun delete(path: String) {
        ch().request(jobj("t" to "fs_delete", "path" to path))
    }

    fun move(from: String, to: String, overwrite: Boolean = false) {
        ch().request(jobj("t" to "fs_move", "from" to from, "to" to to, "overwrite" to overwrite))
    }
}
