package dev.tether.core

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

object WakeOnLan {
    private val MAC = Regex("^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

    fun validMac(mac: String) = MAC.matches(mac)

    /** 6 × 0xFF followed by the MAC address 16 times. */
    fun packet(mac: String): ByteArray {
        require(validMac(mac)) { "bad MAC address $mac" }
        val m = mac.split(':', '-').map { it.toInt(16).toByte() }.toByteArray()
        return ByteArray(6) { 0xFF.toByte() } + ByteArray(16 * 6) { m[it % 6] }
    }

    /**
     * Sends the packet for every target to its own broadcast address, the global
     * broadcast, the computer's last address and any [extraHosts], on each port, three times.
     */
    fun send(targets: List<WolTarget>, extraHosts: Collection<String>, ports: List<Int> = listOf(9, 7)): Int {
        var sent = 0
        DatagramSocket().use { sock ->
            sock.broadcast = true
            repeat(3) {
                for (t in targets) {
                    val data = packet(t.mac)
                    val hosts = LinkedHashSet<String>()
                    t.broadcast?.let { hosts.add(it) }
                    hosts.add("255.255.255.255")
                    t.ip?.let { hosts.add(it) }
                    hosts.addAll(extraHosts)
                    for (h in hosts) {
                        val addr = runCatching { InetAddress.getByName(h) }.getOrNull() ?: continue
                        for (port in ports) {
                            if (runCatching { sock.send(DatagramPacket(data, data.size, addr, port)) }.isSuccess) sent++
                        }
                    }
                }
                Thread.sleep(150)
            }
        }
        return sent
    }
}
