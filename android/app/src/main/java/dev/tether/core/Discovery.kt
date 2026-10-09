package dev.tether.core

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.util.concurrent.ConcurrentHashMap

data class SeenDevice(val id: String, val name: String, val type: String, val host: String, val port: Int, val at: Long)

/** UDP beacons on the local network. */
class Discovery(private val node: Node, private val port: Int) {
    private var socket: DatagramSocket? = null
    private val seen = ConcurrentHashMap<String, SeenDevice>()
    private val replied = ConcurrentHashMap<String, Long>()

    fun start() {
        val s = DatagramSocket(null)
        s.reuseAddress = true
        s.broadcast = true
        s.bind(InetSocketAddress(port))
        socket = s
        Thread({ receiveLoop(s) }, "tether-discovery").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        socket?.close()
        socket = null
    }

    private fun beacon(): ByteArray = jobj(
        "tether" to 1,
        "id" to node.identity.id,
        "name" to node.settings.name,
        "port" to node.settings.port,
        "type" to node.deviceType,
    ).toString().toByteArray()

    fun broadcastTargets(): Set<InetAddress> {
        val out = LinkedHashSet<InetAddress>()
        runCatching {
            for (nif in NetworkInterface.getNetworkInterfaces()) {
                if (!nif.isUp || nif.isLoopback) continue
                for (a in nif.interfaceAddresses) a.broadcast?.let { out.add(it) }
            }
        }
        out.add(InetAddress.getByName("255.255.255.255"))
        return out
    }

    fun announce() {
        val s = socket ?: return
        val data = beacon()
        for (target in broadcastTargets()) {
            runCatching { s.send(DatagramPacket(data, data.size, target, port)) }
        }
    }

    private fun receiveLoop(s: DatagramSocket) {
        val buf = ByteArray(2048)
        while (!s.isClosed) {
            val p = DatagramPacket(buf, buf.size)
            try {
                s.receive(p)
            } catch (e: Exception) {
                if (s.isClosed) return
                continue
            }
            handle(String(p.data, 0, p.length, Charsets.UTF_8), p.address, s)
        }
    }

    private fun handle(text: String, from: InetAddress, s: DatagramSocket) {
        val b = runCatching { parseObj(text) }.getOrNull() ?: return
        if (b.int("tether") != 1) return
        val id = b.str("id") ?: return
        val port = b.int("port") ?: return
        if (id.length != 64 || port !in 1..65535 || id == node.identity.id) return
        val host = from.hostAddress ?: return
        seen[id] = SeenDevice(
            id, (b.str("name") ?: "?").take(100), (b.str("type") ?: "?").take(20), host, port,
            System.currentTimeMillis(),
        )
        if (node.isConnected(id)) return
        val now = System.currentTimeMillis()
        if (now - (replied[id] ?: 0L) > 5_000) {
            replied[id] = now
            val data = beacon()
            runCatching { s.send(DatagramPacket(data, data.size, from, this.port)) }
        }
        node.onBeacon(id, host, port)
    }

    fun recent(maxAgeMs: Long = 60_000): List<SeenDevice> {
        val now = System.currentTimeMillis()
        return seen.values.filter { now - it.at < maxAgeMs }
    }

    fun lookup(id: String): SeenDevice? = recent().firstOrNull { it.id == id }
}
