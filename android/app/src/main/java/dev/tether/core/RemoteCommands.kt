package dev.tether.core

import java.io.IOException

/** Runs commands on a connected computer: its saved commands, or free-form shell if it allows that. */
class RemoteCommands(private val node: Node, val peerId: String) {
    data class Command(val id: String, val name: String)

    data class Listing(val commands: List<Command>, val shell: Boolean)

    data class Result(val exit: Int?, val output: String, val truncated: Boolean, val timedOut: Boolean)

    private fun ch(): Channel = node.session(peerId)?.channel ?: throw IOException("computer not connected")

    fun list(): Listing {
        val m = ch().request(jobj("t" to "cmd_list")).msg
        val cmds = m.arr("commands")?.mapNotNull { e ->
            val o = e.asObj() ?: return@mapNotNull null
            Command(o.str("id") ?: return@mapNotNull null, o.str("name") ?: return@mapNotNull null)
        } ?: emptyList()
        return Listing(cmds, m.bool("shell") == true)
    }

    fun run(id: String): Result = exec(jobj("t" to "cmd_run", "id" to id))

    fun shell(line: String): Result = exec(jobj("t" to "cmd_run", "shell" to line))

    private fun exec(msg: kotlinx.serialization.json.JsonObject): Result {
        // The computer enforces its own time limit; this only guards against a vanished peer.
        val m = ch().request(msg, timeoutMs = 15 * 60_000).msg
        return Result(m.int("exit"), m.str("output") ?: "", m.bool("truncated") == true, m.bool("timed_out") == true)
    }
}
