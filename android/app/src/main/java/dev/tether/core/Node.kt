package dev.tether.core

import kotlinx.serialization.json.JsonObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

const val MAX_CLIP_BYTES = 1024 * 1024
private const val PAIR_TIMEOUT_SEC = 120L
private const val RECONNECT_INTERVAL_SEC = 30L
private const val BEACON_INTERVAL_SEC = 10L
private const val MAX_UNTRUSTED = 4
private const val INDEX_PART = 5000

/** Handled on the receive thread, so they keep their order relative to later messages. */
private val INLINE = setOf("pair_req", "pair_ok", "pair_no", "unpaired", "clip", "sync_index")

const val MAX_CLIP_IMAGE = 16 * 1024 * 1024

interface Platform {
    fun setClipboard(text: String)
    fun downloadsDir(): File
    fun onEvent(event: NodeEvent)
    fun log(msg: String)

    fun setClipboardImage(mime: String, data: ByteArray) {}

    /** Dismisses a phone notification the computer's user closed. */
    fun dismissNotification(key: String) {}

    /** Answers a phone notification inline; false if it can't be replied to (any more). */
    fun replyToNotification(key: String, text: String): Boolean = false
}

sealed class NodeEvent {
    object StatusChanged : NodeEvent()
    data class PairRequest(val id: String, val name: String, val sas: String, val initiator: Boolean) : NodeEvent()
    data class PairFailed(val id: String, val reason: String) : NodeEvent()
    data class Paired(val id: String, val name: String) : NodeEvent()
    data class Unpaired(val id: String, val name: String) : NodeEvent()
    data class FileReceived(val from: String, val file: File) : NodeEvent()
    data class ClipboardReceived(val from: String) : NodeEvent()
    data class Notification(val from: String, val fromName: String, val title: String, val text: String) : NodeEvent()
    data class MediaUpdate(val from: String, val fromName: String, val state: MediaState) : NodeEvent()
    data class TransferProgress(val info: TransferInfo) : NodeEvent()
}

class PairState(val initiator: Boolean) {
    var local: Boolean? = null
    var remote: Boolean? = null
}

class Session(val node: Node, val hs: HandshakeResult, val isClient: Boolean, val addr: InetSocketAddress?) {
    val peerId = hs.peerId
    val name = hs.peerName
    val type = hs.peerType
    val clientId: String = if (isClient) node.identity.id else hs.peerId

    @Volatile
    var trusted = node.peers.contains(hs.peerId)

    @Volatile
    var pair: PairState? = null

    lateinit var channel: Channel
    val remoteIndex = ConcurrentHashMap<String, Map<String, RemoteEntry>>()
    internal val indexParts = HashMap<String, MutableMap<String, RemoteEntry>>()
    internal val bases = HashMap<String, BaseState>()
    internal val sentDigest = HashMap<String, String>()
    internal val syncSignal = Semaphore(0)
    internal val indexLock = Any()
    val sas: String get() = hs.sas
}

data class PendingPair(val id: String, val name: String, val sas: String, val initiator: Boolean, val confirmed: Boolean)

class Node(
    val dir: File,
    val identity: Identity,
    val platform: Platform,
    defaults: Settings.Defaults,
    val deviceType: String = "phone",
) {
    val settings = Settings(File(dir, "settings.json"), defaults)
    val peers = PeerStore(File(dir, "peers.json"))
    private val syncDir = File(dir, "sync").apply { mkdirs() }
    private val sessions = ConcurrentHashMap<String, Session>()
    private val untrusted: MutableSet<Session> = ConcurrentHashMap.newKeySet()
    private val connecting: MutableSet<String> = ConcurrentHashMap.newKeySet()

    @Volatile
    var shares = Shares(settings.shares)
        private set

    @Volatile
    var indexes: Map<String, FolderIndex> = emptyMap()
        private set
    val scanLock = ReentrantLock()

    val pool: ExecutorService = Executors.newCachedThreadPool { r -> Thread(r, "tether-worker").apply { isDaemon = true } }
    private val scheduler: ScheduledExecutorService =
        Executors.newScheduledThreadPool(2) { r -> Thread(r, "tether-timer").apply { isDaemon = true } }
    val discovery = Discovery(this, settings.discoveryPort)
    private var server: ServerSocket? = null

    @Volatile
    var running = false
        private set

    @Volatile
    private var lastClip: String? = null

    @Volatile
    private var lastClipImage: String? = null

    @Volatile
    var battery: Pair<Int, Boolean>? = null
        private set

    private val transfers = ConcurrentHashMap<Int, Transfer>()

    init {
        loadSyncFolders()
    }

    // -- lifecycle -------------------------------------------------------------

    fun start(withDiscovery: Boolean = true) {
        if (running) return
        running = true
        val ss = ServerSocket()
        ss.reuseAddress = true
        ss.bind(InetSocketAddress(settings.port))
        server = ss
        Thread({ acceptLoop(ss) }, "tether-accept").apply {
            isDaemon = true
            start()
        }
        if (withDiscovery) {
            runCatching { discovery.start() }.onFailure { platform.log("discovery unavailable: $it") }
            scheduler.scheduleWithFixedDelay({ runCatching { discovery.announce() } }, 0, BEACON_INTERVAL_SEC, TimeUnit.SECONDS)
        }
        scheduler.scheduleWithFixedDelay({ safely { rescan() } }, 1, settings.scanIntervalSec.toLong(), TimeUnit.SECONDS)
        scheduler.scheduleWithFixedDelay({ safely { reconnectAll() } }, 2, RECONNECT_INTERVAL_SEC, TimeUnit.SECONDS)
        platform.log("device ${settings.name} (${identity.id.take(12)}) listening on ${settings.port}")
    }

    fun stop() {
        running = false
        runCatching { server?.close() }
        discovery.stop()
        (sessions.values + untrusted).forEach { it.channel.close() }
        scheduler.shutdownNow()
        pool.shutdownNow()
    }

    private inline fun safely(block: () -> Unit) {
        try {
            block()
        } catch (e: Exception) {
            platform.log("background task failed: $e")
        }
    }

    // -- connections -----------------------------------------------------------

    fun isConnected(peerId: String): Boolean = sessions[peerId]?.channel?.closed == false

    fun connected(): List<Session> = sessions.values.filter { !it.channel.closed }

    fun session(peerId: String): Session? = sessions[peerId]?.takeIf { !it.channel.closed }

    private fun acceptLoop(ss: ServerSocket) {
        while (running) {
            val sock = try {
                ss.accept()
            } catch (e: IOException) {
                if (!running) return
                continue
            }
            pool.execute {
                val s = establish(sock, false, sock.remoteSocketAddress as? InetSocketAddress)
                if (s != null) serve(s)
            }
        }
    }

    /** Opens a session (blocking: connects and handshakes) and serves it in the background. */
    fun connect(host: String, port: Int): Session {
        val sock = Socket()
        try {
            sock.connect(InetSocketAddress(host, port), 5_000)
        } catch (e: IOException) {
            sock.close()
            throw e
        }
        val s = establish(sock, true, InetSocketAddress(host, port)) ?: throw IOException("handshake failed")
        pool.execute { serve(s) }
        return s
    }

    private fun establish(sock: Socket, isClient: Boolean, addr: InetSocketAddress?): Session? {
        sock.tcpNoDelay = true
        val hs = try {
            Handshake.run(sock, identity, isClient, settings.name, deviceType)
        } catch (e: Exception) {
            platform.log("handshake with $addr failed: ${e.message}")
            runCatching { sock.close() }
            return null
        }
        val s = Session(this, hs, isClient, addr)
        s.channel = Channel(hs.framer, isClient, sock, s.name, pool, INLINE, platform::log) { ch, msg, inc ->
            handle(s, ch, msg, inc)
        }
        if (s.trusted) {
            if (!register(s)) {
                runCatching { sock.close() }
                return null
            }
            if (isClient && addr != null) peers.setAddr(s.peerId, addr.hostString, addr.port, s.name)
            pool.execute { onTrusted(s) }
        } else {
            if (untrusted.size >= MAX_UNTRUSTED) {
                runCatching { sock.close() }
                return null
            }
            untrusted.add(s)
            scheduler.schedule({ if (!s.trusted && s.pair == null) s.channel.close() }, PAIR_TIMEOUT_SEC, TimeUnit.SECONDS)
        }
        platform.log("session with ${s.name} (${s.peerId.take(12)}, ${if (s.trusted) "trusted" else "unpaired"})")
        return s
    }

    @Synchronized
    private fun register(s: Session): Boolean {
        val old = sessions[s.peerId]
        if (old != null && old !== s && !old.channel.closed) {
            val smaller = minOf(identity.id, s.peerId)
            if (!(s.clientId == smaller || old.clientId != smaller)) return false
            old.channel.close()
        }
        sessions[s.peerId] = s
        return true
    }

    private fun serve(s: Session) {
        try {
            s.channel.run()
        } finally {
            untrusted.remove(s)
            if (sessions.remove(s.peerId, s)) platform.onEvent(NodeEvent.StatusChanged)
        }
    }

    private fun onTrusted(s: Session) {
        platform.onEvent(NodeEvent.StatusChanged)
        pool.execute { syncWorker(s) }
        battery?.let { (level, charging) ->
            s.channel.trySend(jobj("t" to "battery", "level" to level, "charging" to charging))
        }
        safely { sendIndexes(s) }
    }

    fun onBeacon(peerId: String, host: String, port: Int) {
        if (!peers.contains(peerId)) return
        peers.setAddr(peerId, host, port)
        if (identity.id < peerId) pool.execute { tryConnect(peerId, host, port) }
    }

    /** True if any live session (trusted or still pairing) exists with this peer. */
    private fun hasLiveSession(peerId: String): Boolean =
        isConnected(peerId) || untrusted.any { it.peerId == peerId && !it.channel.closed }

    private fun tryConnect(key: String, host: String, port: Int) {
        if (hasLiveSession(key) || !connecting.add(key)) return
        try {
            connect(host, port)
        } catch (e: IOException) {
            platform.log("connect to $host:$port failed: ${e.message}")
        } finally {
            connecting.remove(key)
        }
    }

    /** True if we know how to wake this computer (it told us its network cards). */
    fun canWake(peerId: String): Boolean = peers.get(peerId)?.wol?.isNotEmpty() == true

    /**
     * Sends wake-on-LAN magic packets to a paired computer, then keeps trying to
     * reconnect for a couple of minutes while it boots. Blocking; returns packets sent.
     */
    fun wake(peerId: String, ports: List<Int> = listOf(9, 7)): Int {
        val p = peers.get(peerId) ?: throw RemoteError("not_found")
        if (p.wol.isEmpty()) throw RemoteError("not_found", "connect once so the computer can share its network address")
        val extra = discovery.broadcastTargets().mapNotNull { it.hostAddress } + listOfNotNull(p.host)
        val sent = WakeOnLan.send(p.wol, extra, ports)
        for (delay in listOf(5L, 10, 15, 20, 30, 45, 60, 90, 120)) {
            scheduler.schedule({ if (!isConnected(peerId)) safely { reconnectAll() } }, delay, TimeUnit.SECONDS)
        }
        platform.log("sent $sent wake-on-LAN packets to ${p.name}")
        return sent
    }

    fun reconnectAll() {
        for (p in peers.all()) {
            if (p.host != null && p.port != null && !isConnected(p.id)) pool.execute { tryConnect(p.id, p.host, p.port) }
        }
        for (hp in settings.staticPeers) {
            val host = hp.substringBeforeLast(':')
            val port = hp.substringAfterLast(':').toIntOrNull() ?: settings.port
            if (connected().none { it.addr?.hostString == host }) pool.execute { tryConnect("static:$hp", host, port) }
        }
    }

    // -- dispatch ----------------------------------------------------------------

    private fun handle(s: Session, ch: Channel, msg: JsonObject, inc: Incoming?) {
        when (val t = msg.str("t")) {
            "pair_req", "pair_ok", "pair_no" -> onPair(s, t)
            "unpaired" -> {
                if (peers.remove(s.peerId)) platform.onEvent(NodeEvent.Unpaired(s.peerId, s.name))
                ch.close()
            }
            else -> {
                if (!s.trusted) {
                    // Mid-pairing the peer may already trust us; don't make it forget us.
                    if (findPairing(s.peerId) == null) ch.trySend(jobj("t" to "unpaired"))
                    ch.close()
                    return
                }
                when (t) {
                    "clip" -> onClip(s, msg)
                    "send" -> onSend(s, msg, inc!!)
                    "fs_list" -> ch.reply(msg, "entries" to shares.list(msg.str("path")))
                    "fs_stat" -> ch.reply(msg, "entry" to shares.stat(msg.str("path")))
                    "fs_read" -> onFsRead(ch, msg)
                    "fs_write" -> onFsWrite(ch, msg, inc!!)
                    "fs_mkdir" -> {
                        shares.mkdir(msg.str("path"))
                        ch.reply(msg)
                    }
                    "fs_delete" -> {
                        shares.delete(msg.str("path"))
                        ch.reply(msg)
                    }
                    "fs_move" -> {
                        shares.move(msg.str("from"), msg.str("to"), msg.bool("overwrite") == true)
                        ch.reply(msg)
                    }
                    "sync_index" -> onSyncIndex(s, msg)
                    "sync_get" -> onSyncGet(ch, msg)
                    "clip_image" -> onClipImage(ch, msg, inc!!)
                    "notif_dismiss" -> platform.dismissNotification(msg.str("key") ?: throw RemoteError("bad_request"))
                    "notif_reply" -> {
                        val key = msg.str("key") ?: throw RemoteError("bad_request")
                        val text = msg.str("text") ?: throw RemoteError("bad_request")
                        if (!platform.replyToNotification(key, text)) throw RemoteError("not_found", "can't reply to that")
                        ch.reply(msg)
                    }
                    "host_info" -> {
                        peers.setWol(s.peerId, parseWol(msg.arr("wol")))
                        platform.onEvent(NodeEvent.StatusChanged)
                    }
                    "media_update" -> platform.onEvent(NodeEvent.MediaUpdate(s.peerId, s.name, MediaState.parse(msg)))
                    "notify" -> {
                        val title = msg.str("title") ?: throw RemoteError("bad_request")
                        platform.onEvent(NodeEvent.Notification(s.peerId, s.name, title.take(200), (msg.str("text") ?: "").take(4000)))
                        ch.reply(msg)
                    }
                    else -> throw RemoteError("unsupported")
                }
            }
        }
    }

    // -- pairing -----------------------------------------------------------------

    private fun findPairing(peerId: String): Session? =
        (untrusted + sessions.values).firstOrNull { it.peerId == peerId && it.pair != null && !it.channel.closed }

    /** Starts pairing with a discovered device or an address ("host" or "host:port"). Blocking. */
    fun startPairing(peerId: String? = null, address: String? = null): Session {
        val (host, port) = when {
            address != null -> {
                val hasPort = address.contains(':')
                val h = if (hasPort) address.substringBeforeLast(':') else address
                val p = if (hasPort) {
                    address.substringAfterLast(':').toIntOrNull() ?: throw RemoteError("bad_request", "bad port")
                } else {
                    settings.port
                }
                h to p
            }
            peerId != null -> discovery.lookup(peerId)?.let { it.host to it.port }
                ?: throw RemoteError("not_found", "device not seen on the network")
            else -> throw RemoteError("bad_request")
        }
        val s = connect(host, port)
        synchronized(s) { s.pair = PairState(initiator = true) }
        s.channel.send(jobj("t" to "pair_req"))
        pairPrompt(s)
        return s
    }

    private fun pairPrompt(s: Session) {
        platform.onEvent(NodeEvent.PairRequest(s.peerId, s.name, s.sas, s.pair?.initiator ?: false))
        platform.onEvent(NodeEvent.StatusChanged)
        val p = s.pair
        scheduler.schedule({
            synchronized(s) {
                if (s.pair === p && p != null) {
                    s.pair = null
                    platform.onEvent(NodeEvent.PairFailed(s.peerId, "timeout"))
                    if (!s.trusted) s.channel.close()
                }
            }
        }, PAIR_TIMEOUT_SEC, TimeUnit.SECONDS)
    }

    private fun onPair(s: Session, t: String) {
        var prompt = false
        synchronized(s) {
            when (t) {
                "pair_req" -> if (s.pair == null) {
                    s.pair = PairState(initiator = false)
                    prompt = true
                }
                "pair_ok" -> s.pair?.let {
                    it.remote = true
                    maybePaired(s)
                }
                "pair_no" -> if (s.pair != null) {
                    s.pair = null
                    platform.onEvent(NodeEvent.PairFailed(s.peerId, "rejected"))
                    if (!s.trusted) s.channel.close()
                }
            }
        }
        if (prompt) pairPrompt(s)
    }

    fun confirmPairing(peerId: String, accept: Boolean) {
        val s = findPairing(peerId) ?: throw RemoteError("not_found", "no pairing in progress with that device")
        synchronized(s) {
            if (!accept) {
                s.pair = null
                s.channel.trySend(jobj("t" to "pair_no"))
                platform.onEvent(NodeEvent.PairFailed(peerId, "rejected"))
                if (!s.trusted) s.channel.close()
                return
            }
            s.pair?.local = true
            s.channel.send(jobj("t" to "pair_ok"))
            maybePaired(s)
        }
    }

    private fun maybePaired(s: Session) {
        val p = s.pair ?: return
        if (p.local != true || p.remote != true) return
        s.pair = null
        s.trusted = true
        untrusted.remove(s)
        synchronized(this) {
            val old = sessions[s.peerId]
            if (old != null && old !== s) old.channel.close()
            sessions[s.peerId] = s
        }
        peers.add(
            PeerInfo(
                s.peerId, s.name, Crypto.b64(s.hs.peerSpki), s.type,
                if (s.isClient) s.addr?.hostString else null, if (s.isClient) s.addr?.port else null,
            ),
        )
        platform.log("paired with ${s.name}")
        platform.onEvent(NodeEvent.Paired(s.peerId, s.name))
        pool.execute { onTrusted(s) }
    }

    fun unpair(peerId: String) {
        sessions[peerId]?.let {
            it.channel.trySend(jobj("t" to "unpaired"))
            it.channel.close()
        }
        val name = peers.get(peerId)?.name ?: "?"
        if (!peers.remove(peerId)) throw RemoteError("not_found")
        syncDir.listFiles { f -> f.name.startsWith("base-${peerId.take(16)}-") }?.forEach { it.delete() }
        platform.onEvent(NodeEvent.Unpaired(peerId, name))
        platform.onEvent(NodeEvent.StatusChanged)
    }

    fun pendingPairs(): List<PendingPair> = (untrusted + sessions.values).mapNotNull { s ->
        s.pair?.let { PendingPair(s.peerId, s.name, s.sas, it.initiator, it.local == true) }
    }

    // -- clipboard ---------------------------------------------------------------

    /** Called when the local clipboard changes; forwards to every connected device. */
    fun onLocalClipboard(text: String?, force: Boolean = false) {
        if (!settings.clipboard && !force) return
        if (text.isNullOrEmpty() || text.toByteArray().size > MAX_CLIP_BYTES) return
        if (text == lastClip && !force) return
        lastClip = text
        val msg = jobj("t" to "clip", "text" to text)
        for (s in connected()) pool.execute { s.channel.trySend(msg) }
    }

    private fun onClip(s: Session, msg: JsonObject) {
        val text = msg.str("text") ?: throw RemoteError("bad_request")
        if (!settings.clipboard) return
        lastClip = text
        platform.setClipboard(text)
        platform.onEvent(NodeEvent.ClipboardReceived(s.peerId))
    }

    // -- quick send ----------------------------------------------------------------

    /** Sends a file; `open` is called twice (hash pass, then upload). Returns the name it was saved under. */
    fun sendFile(peerId: String, name: String, open: () -> InputStream): String {
        val s = session(peerId) ?: throw RemoteError("not_found", "device not connected")
        val md = MessageDigest.getInstance("SHA-256")
        var size = 0L
        open().use { input ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
                size += n
            }
        }
        val t = startTransfer(name, size, false, s.name)
        val res = try {
            open().use { input ->
                s.channel.request(
                    jobj("t" to "send", "name" to name, "size" to size, "sha256" to Crypto.hex(md.digest())),
                    upload = ProgressInputStream(input, t), uploadLength = size, timeoutMs = 120_000,
                )
            }
        } catch (e: Exception) {
            finishTransfer(t, "failed")
            if (t.cancelled) throw RemoteError("cancelled")
            throw e
        }
        finishTransfer(t, "done")
        return res.msg.str("name") ?: name
    }

    // -- browsing transfers ----------------------------------------------------------

    /**
     * Copies a file from a device's shared folders into the download folder, as a
     * tracked (cancellable) transfer. Returns the file it was saved as.
     */
    fun downloadFile(peerId: String, path: String): File {
        val s = session(peerId) ?: throw RemoteError("not_found", "device not connected")
        val name = cleanName(path.substringAfterLast('/'))
        val dir = platform.downloadsDir().apply { mkdirs() }
        val res = s.channel.request(jobj("t" to "fs_read", "path" to path, "offset" to 0L, "length" to -1L))
        val inc = res.incoming ?: throw IOException("no stream")
        val t = startTransfer(name, res.msg.long("length") ?: res.msg.long("size"), true, s.name)
        t.stream = inc
        val tmp = File(dir, TMP_PREFIX + Crypto.hex(Crypto.randomBytes(6)))
        val final: File
        try {
            FileOutputStream(tmp).use { out -> inc.copyTo(out) { t.add(it.size.toLong()) } }
            if (t.cancelled) throw RemoteError("cancelled")
            final = uniqueFile(dir, name)
            Files.move(tmp.toPath(), final.toPath())
        } catch (e: Exception) {
            if (!inc.done) inc.abort()
            finishTransfer(t, "failed")
            if (t.cancelled) throw RemoteError("cancelled")
            throw e
        } finally {
            tmp.delete()
        }
        finishTransfer(t, "done")
        platform.onEvent(NodeEvent.FileReceived(s.peerId, final))
        return final
    }

    /** Writes a file into a device's shared folders (replacing `path`), as a tracked transfer. */
    fun uploadFile(peerId: String, path: String, size: Long?, open: () -> InputStream) {
        val s = session(peerId) ?: throw RemoteError("not_found", "device not connected")
        val t = startTransfer(path.substringAfterLast('/'), size, false, s.name)
        try {
            open().use { input ->
                s.channel.request(
                    jobj("t" to "fs_write", "path" to path),
                    upload = ProgressInputStream(input, t), uploadLength = size ?: -1, timeoutMs = 300_000,
                )
            }
        } catch (e: Exception) {
            finishTransfer(t, "failed")
            if (t.cancelled) throw RemoteError("cancelled")
            throw e
        }
        finishTransfer(t, "done")
    }

    private fun onSend(s: Session, msg: JsonObject, inc: Incoming) {
        val name = cleanName(msg.str("name"))
        val dir = platform.downloadsDir().apply { mkdirs() }
        val tmp = File(dir, TMP_PREFIX + Crypto.hex(Crypto.randomBytes(6)))
        val md = MessageDigest.getInstance("SHA-256")
        val final: File
        val size = msg.long("size")
        val t = startTransfer(name, size, true, s.name)
        t.stream = inc
        try {
            val n = FileOutputStream(tmp).use { out ->
                inc.copyTo(out) {
                    md.update(it)
                    t.add(it.size.toLong())
                }
            }
            if (t.cancelled) throw RemoteError("cancelled")
            val sha = msg.str("sha256")
            if ((size != null && n != size) || (sha != null && Crypto.hex(md.digest()) != sha)) {
                throw RemoteError("io", "integrity check failed")
            }
            final = uniqueFile(dir, name)
            Files.move(tmp.toPath(), final.toPath())
        } catch (e: Exception) {
            finishTransfer(t, "failed")
            throw e
        } finally {
            tmp.delete()
        }
        finishTransfer(t, "done")
        s.channel.reply(msg, "name" to final.name)
        platform.onEvent(NodeEvent.FileReceived(s.peerId, final))
    }

    // -- transfers -------------------------------------------------------------------

    private fun startTransfer(name: String, total: Long?, incoming: Boolean, peerName: String): Transfer {
        val t = Transfer(Transfer.nextId(), name, total, incoming, peerName) {
            platform.onEvent(NodeEvent.TransferProgress(it))
        }
        transfers[t.id] = t
        platform.onEvent(NodeEvent.TransferProgress(t.info()))
        return t
    }

    private fun finishTransfer(t: Transfer, result: String) {
        transfers.remove(t.id)
        t.finish(result)
    }

    fun cancelTransfer(id: Int): Boolean = transfers[id]?.let { it.cancel(); true } ?: false

    fun activeTransfers(): List<TransferInfo> = transfers.values.map { it.info() }

    // -- clipboard images ------------------------------------------------------------

    /** Sends an image from the phone's clipboard to every connected computer. */
    fun sendClipboardImage(mime: String, data: ByteArray, force: Boolean = false) {
        if ((!settings.clipboard && !force) || data.isEmpty() || data.size > MAX_CLIP_IMAGE) return
        val h = Crypto.hex(Crypto.sha256(data))
        if (h == lastClipImage && !force) return
        lastClipImage = h
        for (s in connected()) {
            pool.execute {
                safely {
                    s.channel.request(
                        jobj("t" to "clip_image", "mime" to mime, "size" to data.size),
                        upload = data.inputStream(), uploadLength = data.size.toLong(),
                    )
                }
            }
        }
    }

    private fun onClipImage(ch: Channel, msg: JsonObject, inc: Incoming) {
        val mime = msg.str("mime") ?: ""
        val buf = java.io.ByteArrayOutputStream()
        while (true) {
            val b = inc.read()
            if (b.isEmpty()) break
            buf.write(b)
            if (buf.size() > MAX_CLIP_IMAGE) {
                inc.abort()
                throw RemoteError("bad_request", "too large")
            }
        }
        if (!mime.startsWith("image/")) throw RemoteError("bad_request")
        ch.reply(msg)
        if (settings.clipboard) {
            val data = buf.toByteArray()
            lastClipImage = Crypto.hex(Crypto.sha256(data))
            platform.setClipboardImage(mime, data)
        }
    }

    // -- battery and notifications from the phone ------------------------------------

    fun reportBattery(level: Int, charging: Boolean) {
        val b = level.coerceIn(0, 100) to charging
        if (b == battery) return
        battery = b
        val msg = jobj("t" to "battery", "level" to b.first, "charging" to b.second)
        for (s in connected()) pool.execute { s.channel.trySend(msg) }
    }

    fun forwardNotification(n: PhoneNotification) {
        val msg = jobj(
            "t" to "notif_posted", "key" to n.key, "app" to n.app, "app_name" to n.appName, "title" to n.title,
            "text" to n.text, "time" to n.time, "reply" to n.canReply, "icon" to n.iconPngBase64,
        )
        for (s in connected()) if (s.type != "phone") pool.execute { s.channel.trySend(msg) }
    }

    fun notificationRemoved(key: String) {
        val msg = jobj("t" to "notif_removed", "key" to key)
        for (s in connected()) if (s.type != "phone") pool.execute { s.channel.trySend(msg) }
    }

    // -- browsing (we serve) ---------------------------------------------------------

    private fun onFsRead(ch: Channel, msg: JsonObject) {
        val f = shares.resolve(msg.str("path")) ?: throw RemoteError("is_dir")
        if (f.isDirectory) throw RemoteError("is_dir")
        val input = try {
            FileInputStream(f)
        } catch (e: IOException) {
            throw ioError(e)
        }
        input.use {
            val size = it.channel.size()
            val offset = (msg.long("offset") ?: 0L).coerceIn(0L, size)
            val req = msg.long("length") ?: -1L
            val length = if (req < 0) size - offset else minOf(req, size - offset)
            it.channel.position(offset)
            ch.replyStream(msg, it, length, "size" to size, "length" to length)
        }
    }

    private fun onFsWrite(ch: Channel, msg: JsonObject, inc: Incoming) {
        val (tmp, final) = shares.beginWrite(msg.str("path"))
        try {
            FileOutputStream(tmp).use { inc.copyTo(it) }
            Files.move(tmp.toPath(), final.toPath(), StandardCopyOption.REPLACE_EXISTING)
        } catch (e: Exception) {
            throw ioError(e)
        } finally {
            tmp.delete()
        }
        ch.reply(msg)
    }

    // -- sync ------------------------------------------------------------------------

    fun loadSyncFolders() {
        indexes = settings.sync.associate { it.id to FolderIndex(it.id, File(it.path), syncDir) }
    }

    fun updateShares(newShares: Map<String, String>) {
        settings.shares = newShares
        settings.save()
        shares = Shares(newShares)
    }

    fun updateSyncFolders(folders: List<SyncFolder>) {
        settings.sync = folders
        settings.save()
        scanLock.withLock { loadSyncFolders() }
        pool.execute { safely { rescan() } }
    }

    fun rescan() {
        var changed = false
        scanLock.withLock {
            for (idx in indexes.values) if (idx.scan()) changed = true
        }
        if (changed) broadcastIndexes()
    }

    private fun broadcastIndexes() {
        for (s in connected()) {
            pool.execute { safely { sendIndexes(s) } }
            s.syncSignal.release()
        }
    }

    private fun sendIndexes(s: Session) = synchronized(s.indexLock) {
        for ((fid, idx) in indexes) {
            val wire = scanLock.withLock { idx.wire() }
            val digest = Crypto.hex(Crypto.sha256(wire.toSortedMap().toString().toByteArray()))
            if (s.sentDigest[fid] == digest) continue
            val items = wire.entries.toList()
            var i = 0
            do {
                val part = items.subList(i, minOf(i + INDEX_PART, items.size)).associate { it.key to it.value }
                val more = i + INDEX_PART < items.size
                s.channel.send(jobj("t" to "sync_index", "folder" to fid, "files" to idx.wireJson(part), "more" to more))
                i += INDEX_PART
            } while (more)
            s.sentDigest[fid] = digest
        }
    }

    private fun onSyncIndex(s: Session, msg: JsonObject) {
        val fid = msg.str("folder") ?: throw RemoteError("bad_request")
        val files = msg.obj("files") ?: throw RemoteError("bad_request")
        val acc = s.indexParts.getOrPut(fid) { HashMap() }
        acc.putAll(parseRemoteIndex(files))
        if (msg.bool("more") != true) {
            s.remoteIndex[fid] = s.indexParts.remove(fid)!!
            s.syncSignal.release()
        }
    }

    private fun onSyncGet(ch: Channel, msg: JsonObject) {
        val idx = indexes[msg.str("folder")] ?: throw RemoteError("not_found")
        val rel = safeRel(msg.str("path"))
        val e = scanLock.withLock { idx.files[rel] }
        if (e == null || e.h != msg.str("hash") || !idx.matchesDisk(rel)) throw RemoteError("changed")
        val input = try {
            FileInputStream(File(idx.root, rel))
        } catch (ex: IOException) {
            throw ioError(ex)
        }
        input.use { ch.replyStream(msg, it, it.channel.size(), "size" to it.channel.size(), "mtime" to e.m) }
    }

    private fun syncWorker(s: Session) {
        while (!s.channel.closed && running) {
            try {
                s.syncSignal.acquire()
            } catch (e: InterruptedException) {
                return
            }
            s.syncSignal.drainPermits()
            var acted = false
            for ((fid, remote) in s.remoteIndex.entries.toList()) {
                val idx = indexes[fid] ?: continue
                try {
                    if (syncFolder(s, idx, remote)) acted = true
                } catch (e: Exception) {
                    platform.log("sync $fid: $e")
                }
            }
            if (acted) broadcastIndexes()
        }
    }

    private fun syncFolder(s: Session, idx: FolderIndex, remote: Map<String, RemoteEntry>): Boolean {
        if (!idx.root.isDirectory) return false
        val base = s.bases.getOrPut(idx.id) { BaseState(syncDir, s.peerId, idx.id) }
        val actions = scanLock.withLock { plan(idx.wire(), remote, base, identity.id, s.peerId) }
        base.save()
        var done = false
        for (a in actions) {
            if (s.channel.closed) break
            try {
                if (apply(s, idx, base, a)) done = true
            } catch (e: IOException) {
                platform.log("sync ${idx.id}/${a.rel}: ${e.message}")
            }
            base.save()
        }
        if (done) scanLock.withLock { idx.save() }
        return done
    }

    private fun diskState(f: File): Pair<Long, Long>? = if (f.exists()) f.length() to f.lastModified() else null

    private fun apply(s: Session, idx: FolderIndex, base: BaseState, a: PlannedAction): Boolean {
        val target = File(idx.root, a.rel)
        if (a.action == SyncAction.DELETE) {
            scanLock.withLock {
                if (!idx.matchesDisk(a.rel)) return false
                if (target.exists() && !target.delete()) throw IOException("cannot delete ${a.rel}")
                removeEmptyParents(idx.root, a.rel)
                if (idx.files.containsKey(a.rel)) idx.files[a.rel] = IndexEntry(null, 0, System.currentTimeMillis(), 0)
                base.set(a.rel, null)
            }
            platform.log("sync ${idx.id}: deleted ${a.rel}")
            return true
        }
        val before = scanLock.withLock {
            if (!idx.matchesDisk(a.rel)) return false
            if (a.action == SyncAction.CONFLICT) {
                var name = conflictName(a.rel)
                var i = 1
                while (File(idx.root, name).exists()) {
                    val c = conflictName(a.rel)
                    val dot = c.lastIndexOf('.').takeIf { it > c.lastIndexOf('/') + 1 } ?: c.length
                    name = c.substring(0, dot) + "-$i" + c.substring(dot)
                    i++
                }
                if (!target.renameTo(File(idx.root, name))) throw IOException("cannot rename ${a.rel}")
                idx.files.remove(a.rel)
                platform.log("sync ${idx.id}: conflict, kept local copy as $name")
            }
            diskState(target)
        }
        val res = s.channel.request(jobj("t" to "sync_get", "folder" to idx.id, "path" to a.rel, "hash" to a.remote.h))
        val inc = res.incoming ?: throw IOException("no stream")
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, TMP_PREFIX + Crypto.hex(Crypto.randomBytes(6)))
        val md = MessageDigest.getInstance("SHA-256")
        try {
            FileOutputStream(tmp).use { out -> inc.copyTo(out) { md.update(it) } }
            if (Crypto.hex(md.digest()) != a.remote.h) throw RemoteError("changed", "hash mismatch")
            val mtime = res.msg.long("mtime") ?: a.remote.m
            scanLock.withLock {
                if (diskState(target) != before) throw RemoteError("changed", "local file changed during download")
                tmp.setLastModified(mtime)
                Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
                idx.files[a.rel] = IndexEntry(a.remote.h, target.length(), mtime, target.lastModified())
                base.set(a.rel, a.remote.h)
            }
        } finally {
            if (!inc.done) inc.abort()
            tmp.delete()
        }
        platform.log("sync ${idx.id}: pulled ${a.rel}")
        return true
    }

    // -- helpers ---------------------------------------------------------------------

    companion object {
        fun cleanName(name: String?): String {
            var n = (name ?: "").substringAfterLast('/').replace(Regex("[\\u0000-\\u001f]"), "").trim()
            if (n.isEmpty() || n == "." || n == ".." || n.startsWith(".tether")) n = "file"
            return n.take(200)
        }

        fun uniqueFile(dir: File, name: String): File {
            var f = File(dir, name)
            val dot = name.lastIndexOf('.').takeIf { it > 0 } ?: name.length
            var i = 1
            while (f.exists()) {
                f = File(dir, "${name.substring(0, dot)} ($i)${name.substring(dot)}")
                i++
            }
            return f
        }
    }
}
