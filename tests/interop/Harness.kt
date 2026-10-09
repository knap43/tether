// A stand-in "phone": the Android app's Kotlin core driven over stdin/stdout,
// so the Python test can exercise it against the Linux daemon.

import dev.tether.core.Crypto
import dev.tether.core.FileIdentity
import dev.tether.core.PhoneNotification
import dev.tether.core.RemoteMedia
import dev.tether.core.Node
import dev.tether.core.NodeEvent
import dev.tether.core.Platform
import dev.tether.core.RemoteCommands
import dev.tether.core.RemoteFs
import dev.tether.core.Settings
import dev.tether.core.SyncFolder
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream

fun main(args: Array<String>) {
    val home = File(args[0])
    val port = args[1].toInt()
    val share = args[2]
    val syncDir = args[3]
    val out = System.out
    fun emit(line: String) = synchronized(out) { out.println(line); out.flush() }

    var nodeRef: Node? = null
    val replyable = HashSet<String>()
    val platform = object : Platform {
        override fun setClipboardImage(mime: String, data: ByteArray) =
            emit("CLIPIMG $mime ${Crypto.hex(Crypto.sha256(data))}")
        override fun dismissNotification(key: String) = emit("DISMISS $key")
        override fun replyToNotification(key: String, text: String): Boolean {
            emit("REPLY $key|$text")
            return key in replyable
        }
        override fun setClipboard(text: String) = emit("CLIP " + text.replace("\n", "\\n"))
        override fun downloadsDir() = File(home, "downloads")
        override fun onEvent(event: NodeEvent) {
            when (event) {
                is NodeEvent.PairRequest -> emit("PAIRREQ ${event.id} ${event.sas} ${event.initiator}")
                is NodeEvent.Paired -> emit("PAIRED ${event.id}")
                is NodeEvent.PairFailed -> emit("PAIRFAILED ${event.id} ${event.reason}")
                is NodeEvent.FileReceived -> emit("FILE ${event.file.path}")
                is NodeEvent.Unpaired -> emit("UNPAIRED ${event.id}")
                is NodeEvent.Notification -> emit("NOTIFY ${event.title}|${event.text}")
                is NodeEvent.MediaUpdate -> emit("MEDIA ${event.state.active?.title}|${event.state.active?.status}")
                is NodeEvent.TransferProgress -> {
                    val i = event.info
                    if (i.state == "active" && i.name.startsWith("cancel-")) nodeRef?.cancelTransfer(i.id)
                    if (i.state != "active") emit("TRANSFER ${i.name} ${i.state} ${i.done}")
                }
                else -> {}
            }
        }
        override fun log(msg: String) = System.err.println("[kt] $msg")
    }
    File(home, "settings.json").writeText(
        """{"port": $port, "discovery_port": ${port + 2000}, "scan_interval": 1, "name": "KtPhone"}""",
    )
    val node = Node(
        home, FileIdentity.loadOrCreate(home), platform,
        Settings.Defaults("KtPhone", mapOf("Internal" to share), File(home, "downloads").path, 1),
    )
    nodeRef = node
    node.updateShares(mapOf("Internal" to share))
    node.updateSyncFolders(listOf(SyncFolder("docs", syncDir)))
    node.start(withDiscovery = false)
    emit("READY ${node.identity.id}")

    val stdin = System.`in`.bufferedReader()
    while (true) {
        val line = stdin.readLine() ?: break
        val parts = line.split(" ", limit = 3)
        try {
            when (parts[0]) {
                "pair" -> {
                    val s = node.startPairing(address = parts[1])
                    emit("SAS ${s.peerId} ${s.sas}")
                }
                "confirm" -> {
                    node.confirmPairing(parts[1], true)
                    emit("OK")
                }
                "clip" -> {
                    node.onLocalClipboard(parts[1])
                    emit("OK")
                }
                "send" -> {
                    val f = File(parts[2])
                    val saved = node.sendFile(parts[1], f.name) { FileInputStream(f) }
                    emit("SENT $saved")
                }
                "ls" -> {
                    val fs = RemoteFs(node, parts[1])
                    emit("LS " + fs.list(parts[2]).map { it.name + if (it.dir) "/" else "" }.sorted().joinToString(","))
                }
                "get" -> {
                    val (peer, rest) = parts[1] to parts[2]
                    val (remote, local, off, len) = rest.split(" ")
                    val fs = RemoteFs(node, peer)
                    FileOutputStream(local).use { fs.read(remote, off.toLong(), len.toLong(), it) }
                    emit("OK")
                }
                "put" -> {
                    val (remote, local) = parts[2].split(" ")
                    FileInputStream(local).use { RemoteFs(node, parts[1]).write(remote, it) }
                    emit("OK")
                }
                "mv" -> {
                    val (a, b) = parts[2].split(" ")
                    RemoteFs(node, parts[1]).move(a, b)
                    emit("OK")
                }
                "mkdir" -> {
                    RemoteFs(node, parts[1]).mkdir(parts[2])
                    emit("OK")
                }
                "rm" -> {
                    RemoteFs(node, parts[1]).delete(parts[2])
                    emit("OK")
                }
                "connected" -> emit("CONNECTED " + node.connected().joinToString(",") { it.peerId })
                "rescan" -> {
                    node.rescan()
                    emit("OK")
                }
                "drop" -> {
                    node.session(parts[1])?.channel?.close()
                    emit("OK")
                }
                "reconnect" -> {
                    node.reconnectAll()
                    emit("OK")
                }
                "unpair" -> {
                    node.unpair(parts[1])
                    emit("OK")
                }
                "cmds" -> {
                    val l = RemoteCommands(node, parts[1]).list()
                    emit("CMDS shell=${l.shell} " + l.commands.joinToString(",") { it.id + "=" + it.name })
                }
                "run", "sh" -> {
                    val rc = RemoteCommands(node, parts[1])
                    val r = if (parts[0] == "run") rc.run(parts[2]) else rc.shell(parts[2])
                    emit("RESULT exit=${r.exit} timedout=${r.timedOut} out=" + r.output.trim().replace("\n", "\\n"))
                }
                "battery" -> {
                    val (lvl, ch) = parts[1].split(" ").let { it[0] to (parts.getOrNull(2) ?: it.getOrNull(1)) }
                    node.reportBattery(lvl.toInt(), ch == "true")
                    emit("OK")
                }
                "notif" -> {
                    val f = (parts.drop(1).joinToString(" ")).split("|")
                    if (f[3] == "reply") replyable.add(f[0])
                    node.forwardNotification(
                        PhoneNotification(f[0], "org.signal", "Signal", f[1], f[2], 0, f[3] == "reply", null),
                    )
                    emit("OK")
                }
                "unnotif" -> {
                    node.notificationRemoved(parts[1])
                    emit("OK")
                }
                "clipimg" -> {
                    val data = File(parts[1]).readBytes()
                    node.sendClipboardImage("image/png", data)
                    emit("OK")
                }
                "mstate" -> {
                    val st = RemoteMedia(node, parts[1]).state()
                    emit("MSTATE ${st.active?.title}|${st.active?.artist}|${st.volume}")
                }
                "media" -> {
                    val args = parts[2].split(" ")
                    val st = RemoteMedia(node, parts[1]).command(args[0], value = args.getOrNull(1)?.toDouble())
                    emit("MSTATE ${st.active?.title}|${st.active?.artist}|${st.volume}")
                }
                "canwake" -> emit("CANWAKE ${node.canWake(parts[1])} ${node.peers.get(parts[1])?.wol?.joinToString { it.mac }}")
                "wake" -> emit("WOKE " + node.wake(parts[1], listOf(parts[2].toInt())))
                "quit" -> break
                else -> emit("ERR unknown command")
            }
        } catch (e: Exception) {
            emit("ERR ${e.javaClass.simpleName}: ${e.message}")
        }
    }
    node.stop()
}
