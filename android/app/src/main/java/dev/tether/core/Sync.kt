package dev.tether.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.io.File
import java.io.FileInputStream
import java.nio.file.Files
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private const val TOMBSTONE_TTL_MS = 30L * 24 * 3600 * 1000

fun ignoredName(name: String) = name.startsWith(".tether")

fun safeRel(rel: String?): String {
    val parts = splitPath(rel)
    if (parts.isEmpty() || parts.any { ignoredName(it) }) throw RemoteError("bad_request")
    return parts.joinToString("/")
}

fun sha256File(f: File): String {
    val md = MessageDigest.getInstance("SHA-256")
    FileInputStream(f).use { input ->
        val buf = ByteArray(1 shl 20)
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            md.update(buf, 0, n)
        }
    }
    return Crypto.hex(md.digest())
}

fun conflictName(rel: String, now: Long = System.currentTimeMillis()): String {
    val slash = rel.lastIndexOf('/')
    val dir = if (slash >= 0) rel.substring(0, slash + 1) else ""
    val base = rel.substring(slash + 1)
    val dot = base.lastIndexOf('.')
    val (stem, ext) = if (dot > 0) base.substring(0, dot) to base.substring(dot) else base to ""
    val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(Date(now))
    return "$dir$stem.conflict-$stamp$ext"
}

/** h = sha256 hex or null for a tombstone; s = size; m = mtime ms (as announced); n = local mtime (change detection). */
data class IndexEntry(val h: String?, val s: Long, val m: Long, val n: Long)

/** A remote index entry: [hash|null, size, mtime_ms]. */
data class RemoteEntry(val h: String?, val s: Long, val m: Long)

class FolderIndex(val id: String, val root: File, stateDir: File) {
    private val file = File(stateDir, "index-$id.json")
    val files: MutableMap<String, IndexEntry> = load()

    private fun load(): MutableMap<String, IndexEntry> {
        val out = HashMap<String, IndexEntry>()
        if (!file.exists()) return out
        runCatching {
            parseObj(file.readText()).forEach { (rel, v) ->
                val o = v as? JsonObject ?: return@forEach
                out[rel] = IndexEntry(o.str("h"), o.long("s") ?: 0, o.long("m") ?: 0, o.long("n") ?: 0)
            }
        }
        return out
    }

    fun save() {
        val obj = files.mapValues { (_, e) -> mapOf("h" to e.h, "s" to e.s, "m" to e.m, "n" to e.n) }
        atomicWrite(file, toJson(obj).toString())
    }

    /** Rescan the folder (blocking). Returns true if the index changed. */
    fun scan(): Boolean {
        if (!root.isDirectory) return false
        val seen = HashMap<String, File>()
        walk(root, "", seen)
        var changed = false
        for ((rel, f) in seen) {
            val old = files[rel]
            val size = f.length()
            val mtime = f.lastModified()
            if (old != null && old.h != null && old.s == size && old.n == mtime) continue
            val h = try {
                sha256File(f)
            } catch (e: Exception) {
                continue
            }
            val size2 = f.length()
            val mtime2 = f.lastModified()
            files[rel] = if (old != null && old.h == h && old.s == size2) {
                old.copy(m = mtime2, n = mtime2)
            } else {
                IndexEntry(h, size2, mtime2, mtime2)
            }
            changed = true
        }
        val now = System.currentTimeMillis()
        for ((rel, e) in files.entries.toList()) {
            if (seen.containsKey(rel)) continue
            if (e.h != null) {
                files[rel] = IndexEntry(null, 0, now, 0)
                changed = true
            } else if (now - e.m > TOMBSTONE_TTL_MS) {
                files.remove(rel)
                changed = true
            }
        }
        if (changed) save()
        return changed
    }

    private fun walk(dir: File, relDir: String, out: MutableMap<String, File>) {
        val kids = dir.listFiles() ?: return
        for (f in kids) {
            if (ignoredName(f.name)) continue
            if (Files.isSymbolicLink(f.toPath())) continue
            val rel = if (relDir.isEmpty()) f.name else "$relDir/${f.name}"
            when {
                f.isDirectory -> walk(f, rel, out)
                f.isFile -> out[rel] = f
            }
        }
    }

    fun wire(): Map<String, RemoteEntry> = files.mapValues { (_, e) -> RemoteEntry(e.h, e.s, e.m) }

    fun wireJson(entries: Map<String, RemoteEntry>): JsonObject =
        JsonObject(entries.mapValues { (_, e) -> JsonArray(listOf(toJson(e.h), JsonPrimitive(e.s), JsonPrimitive(e.m))) })

    /** True if the file on disk is still what the index says. */
    fun matchesDisk(rel: String): Boolean {
        val e = files[rel]
        val f = File(root, rel)
        if (!f.exists()) return e == null || e.h == null
        return e != null && e.h != null && e.s == f.length() && e.n == f.lastModified()
    }
}

/** Per (peer, folder): relpath → hash both sides last agreed on. */
class BaseState(stateDir: File, peerId: String, folderId: String) {
    private val file = File(stateDir, "base-${peerId.take(16)}-$folderId.json")
    private val map: MutableMap<String, String> = HashMap()
    private var dirty = false

    init {
        if (file.exists()) runCatching {
            parseObj(file.readText()).forEach { (k, v) -> v.asStr()?.let { map[k] = it } }
        }
    }

    fun get(rel: String): String? = map[rel]

    fun set(rel: String, h: String?) {
        if (h == null) {
            if (map.remove(rel) != null) dirty = true
        } else if (map[rel] != h) {
            map[rel] = h
            dirty = true
        }
    }

    fun save() {
        if (dirty) {
            atomicWrite(file, toJson(map).toString())
            dirty = false
        }
    }
}

enum class SyncAction { PULL, DELETE, CONFLICT }

data class PlannedAction(val action: SyncAction, val rel: String, val remote: RemoteEntry)

fun parseRemoteIndex(files: JsonObject): Map<String, RemoteEntry> {
    val out = HashMap<String, RemoteEntry>()
    for ((rel, v) in files) {
        val a = v as? JsonArray ?: continue
        if (a.size != 3) continue
        val h = if (a[0] is JsonNull) null else a[0].asStr() ?: continue
        out[rel] = RemoteEntry(h, a[1].asLong() ?: 0, a[2].asLong() ?: 0)
    }
    return out
}

/** Three-way decision for every path in the remote index; records agreements in `base`. */
fun plan(
    local: Map<String, RemoteEntry>,
    remote: Map<String, RemoteEntry>,
    base: BaseState,
    myId: String,
    peerId: String,
): List<PlannedAction> {
    val actions = ArrayList<PlannedAction>()
    for ((rel, r) in remote) {
        val ok = try {
            safeRel(rel) == rel
        } catch (e: RemoteError) {
            false
        }
        if (!ok) continue
        val l = local[rel]
        val lh = l?.h
        val rh = r.h
        val b = base.get(rel)
        when {
            lh == rh -> base.set(rel, lh)
            rh == b -> {}
            lh == b -> actions.add(PlannedAction(if (rh == null) SyncAction.DELETE else SyncAction.PULL, rel, r))
            rh == null -> {}
            lh == null -> actions.add(PlannedAction(SyncAction.PULL, rel, r))
            l!!.m < r.m || (l.m == r.m && myId < peerId) -> actions.add(PlannedAction(SyncAction.CONFLICT, rel, r))
        }
    }
    return actions
}

fun removeEmptyParents(root: File, rel: String) {
    var dir = File(root, rel).parentFile
    while (dir != null && dir.path.length > root.path.length) {
        val kids = dir.list()
        if (kids == null || kids.isNotEmpty() || !dir.delete()) return
        dir = dir.parentFile
    }
}

fun atomicWrite(file: File, text: String) {
    file.parentFile?.mkdirs()
    val tmp = File(file.parentFile, ".tmp-${file.name}-${System.nanoTime()}")
    tmp.writeText(text)
    Files.move(tmp.toPath(), file.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING, java.nio.file.StandardCopyOption.ATOMIC_MOVE)
}
