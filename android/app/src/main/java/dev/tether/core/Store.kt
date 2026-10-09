package dev.tether.core

import kotlinx.serialization.json.JsonObject
import java.io.File

data class SyncFolder(val id: String, val path: String)

/** Persistent settings (settings.json). */
class Settings(private val file: File, defaults: Defaults) {
    data class Defaults(
        val name: String,
        val shares: Map<String, String>,
        val downloadDir: String,
        val scanIntervalSec: Int = 30,
    )

    var name: String = defaults.name
    var port: Int = 47291
    var discoveryPort: Int = 47290
    var clipboard: Boolean = true
    var shares: Map<String, String> = defaults.shares
    var sync: List<SyncFolder> = emptyList()
    var downloadDir: String = defaults.downloadDir
    var scanIntervalSec: Int = defaults.scanIntervalSec
    var staticPeers: List<String> = emptyList()
    var forwardNotifications: Boolean = true

    init {
        if (file.exists()) runCatching {
            val o = parseObj(file.readText())
            o.str("name")?.let { name = it }
            o.int("port")?.let { port = it }
            o.int("discovery_port")?.let { discoveryPort = it }
            o.bool("clipboard")?.let { clipboard = it }
            o.obj("shares")?.let { s -> shares = s.mapNotNull { (k, v) -> v.asStr()?.let { k to it } }.toMap() }
            o.arr("sync")?.let { a ->
                sync = a.mapNotNull { e ->
                    val f = e.asObj() ?: return@mapNotNull null
                    val id = f.str("id") ?: return@mapNotNull null
                    SyncFolder(id, f.str("path") ?: return@mapNotNull null)
                }
            }
            o.str("download_dir")?.let { downloadDir = it }
            o.int("scan_interval")?.let { scanIntervalSec = it }
            o.arr("static_peers")?.let { a -> staticPeers = a.mapNotNull { it.asStr() } }
            o.bool("forward_notifications")?.let { forwardNotifications = it }
        } else save()
    }

    @Synchronized
    fun save() {
        val o = jobj(
            "name" to name,
            "port" to port,
            "discovery_port" to discoveryPort,
            "clipboard" to clipboard,
            "shares" to shares,
            "sync" to sync.map { mapOf("id" to it.id, "path" to it.path) },
            "download_dir" to downloadDir,
            "scan_interval" to scanIntervalSec,
            "static_peers" to staticPeers,
            "forward_notifications" to forwardNotifications,
        )
        atomicWrite(file, o.toString())
    }
}

/** Where to send a wake-on-LAN magic packet for one network card of a computer. */
data class WolTarget(val mac: String, val broadcast: String?, val ip: String?)

data class PeerInfo(
    val id: String,
    val name: String,
    val key: String,
    val type: String,
    val host: String?,
    val port: Int?,
    val wol: List<WolTarget> = emptyList(),
)

/** Paired devices (peers.json), same shape as the Linux daemon's. */
class PeerStore(private val file: File) {
    private val peers = LinkedHashMap<String, PeerInfo>()

    init {
        if (file.exists()) runCatching {
            parseObj(file.readText()).forEach { (id, v) ->
                val o = v as? JsonObject ?: return@forEach
                val addr = o.arr("addr")
                peers[id] = PeerInfo(
                    id, o.str("name") ?: "?", o.str("key") ?: "", o.str("type") ?: "?",
                    addr?.getOrNull(0)?.asStr(), addr?.getOrNull(1)?.asLong()?.toInt(),
                    parseWol(o.arr("wol")),
                )
            }
        }
    }

    @Synchronized
    fun all(): List<PeerInfo> = peers.values.toList()

    @Synchronized
    operator fun contains(id: String) = peers.containsKey(id)

    @Synchronized
    fun get(id: String) = peers[id]

    @Synchronized
    fun add(p: PeerInfo) {
        peers[p.id] = p
        save()
    }

    @Synchronized
    fun remove(id: String): Boolean {
        if (peers.remove(id) == null) return false
        save()
        return true
    }

    @Synchronized
    fun setAddr(id: String, host: String, port: Int, name: String? = null) {
        val p = peers[id] ?: return
        val updated = p.copy(host = host, port = port, name = name ?: p.name)
        if (updated != p) {
            peers[id] = updated
            save()
        }
    }

    @Synchronized
    fun setWol(id: String, targets: List<WolTarget>) {
        val p = peers[id] ?: return
        if (p.wol == targets) return
        peers[id] = p.copy(wol = targets)
        save()
    }

    private fun save() {
        val o = peers.mapValues { (_, p) ->
            mapOf(
                "name" to p.name, "key" to p.key, "type" to p.type,
                "addr" to if (p.host != null && p.port != null) listOf(p.host, p.port) else null,
                "wol" to p.wol.map { mapOf("mac" to it.mac, "broadcast" to it.broadcast, "ip" to it.ip) },
            )
        }
        atomicWrite(file, toJson(o).toString())
    }
}

fun parseWol(a: kotlinx.serialization.json.JsonArray?): List<WolTarget> = a?.mapNotNull { e ->
    val o = e.asObj() ?: return@mapNotNull null
    val mac = o.str("mac")?.takeIf { WakeOnLan.validMac(it) } ?: return@mapNotNull null
    WolTarget(mac, o.str("broadcast"), o.str("ip"))
} ?: emptyList()
