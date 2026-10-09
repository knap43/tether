package dev.tether.core

import java.io.DataInputStream
import java.io.EOFException
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.math.BigInteger
import java.net.Socket
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PublicKey
import java.security.SecureRandom
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECFieldFp
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPoint
import java.security.spec.ECPublicKeySpec
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

const val KIND_JSON: Byte = 1
const val KIND_DATA: Byte = 2

class HandshakeException(msg: String) : IOException(msg)

/** A device's long-term signing identity (ECDSA P-256). */
interface Identity {
    /** X.509 SubjectPublicKeyInfo DER of the public key. */
    val spki: ByteArray

    /** DER-encoded ECDSA-SHA256 signature. */
    fun sign(data: ByteArray): ByteArray

    val id: String get() = Crypto.deviceId(spki)
}

/** Identity kept in a file; used by tests and as a fallback. */
class FileIdentity private constructor(private val kp: KeyPair) : Identity {
    override val spki: ByteArray = kp.public.encoded

    override fun sign(data: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run {
            initSign(kp.private)
            update(data)
            sign()
        }

    companion object {
        fun loadOrCreate(dir: File): FileIdentity {
            val priv = File(dir, "identity.key")
            val pub = File(dir, "identity.pub")
            val kf = KeyFactory.getInstance("EC")
            if (priv.exists() && pub.exists()) {
                return FileIdentity(
                    KeyPair(
                        kf.generatePublic(X509EncodedKeySpec(pub.readBytes())),
                        kf.generatePrivate(PKCS8EncodedKeySpec(priv.readBytes())),
                    ),
                )
            }
            val kp = Crypto.newEcKeyPair()
            dir.mkdirs()
            priv.writeBytes(kp.private.encoded)
            pub.writeBytes(kp.public.encoded)
            return FileIdentity(kp)
        }
    }
}

object Crypto {
    val LABEL = "tether-v1".toByteArray()
    val MAGIC = "TTHR".toByteArray()
    const val VERSION = 1
    const val HELLO_LEN = 4 + 1 + 65 + 32
    const val MAX_CIPHERTEXT = 4 * 1024 * 1024 + 64
    const val HANDSHAKE_TIMEOUT_MS = 15_000

    private val random = SecureRandom()

    private val P256_P = BigInteger("ffffffff00000001000000000000000000000000ffffffffffffffffffffffff", 16)
    private val P256_N = BigInteger("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)

    fun sha256(vararg parts: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").run {
            parts.forEach { update(it) }
            digest()
        }

    fun hex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }

    fun deviceId(spki: ByteArray): String = hex(sha256(spki))

    fun b64(b: ByteArray): String = Base64.getEncoder().encodeToString(b)

    fun unb64(s: String): ByteArray = Base64.getDecoder().decode(s)

    fun randomBytes(n: Int): ByteArray = ByteArray(n).also { random.nextBytes(it) }

    fun newEcKeyPair(): KeyPair =
        KeyPairGenerator.getInstance("EC").run {
            initialize(ECGenParameterSpec("secp256r1"), random)
            generateKeyPair()
        }

    private fun hmac(key: ByteArray, vararg data: ByteArray): ByteArray =
        Mac.getInstance("HmacSHA256").run {
            init(SecretKeySpec(key, "HmacSHA256"))
            data.forEach { update(it) }
            doFinal()
        }

    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val prk = hmac(salt, ikm)
        val out = java.io.ByteArrayOutputStream()
        var t = ByteArray(0)
        var i = 1
        while (out.size() < length) {
            t = hmac(prk, t, info, byteArrayOf(i.toByte()))
            out.write(t)
            i++
        }
        return out.toByteArray().copyOf(length)
    }

    fun sas(th: ByteArray): String {
        val d = sha256(LABEL, " sas".toByteArray(), th)
        val n = ((d[0].toLong() and 0xff) shl 24) or ((d[1].toLong() and 0xff) shl 16) or
            ((d[2].toLong() and 0xff) shl 8) or (d[3].toLong() and 0xff)
        return "%06d".format(n % 1_000_000)
    }

    private fun fixed32(v: BigInteger): ByteArray {
        val b = v.toByteArray()
        return when {
            b.size == 32 -> b
            b.size > 32 -> b.copyOfRange(b.size - 32, b.size)
            else -> ByteArray(32 - b.size) + b
        }
    }

    fun encodePoint(pub: ECPublicKey): ByteArray =
        byteArrayOf(4) + fixed32(pub.w.affineX) + fixed32(pub.w.affineY)

    private fun isP256(params: ECParameterSpec): Boolean =
        (params.curve.field as? ECFieldFp)?.p == P256_P && params.order == P256_N

    fun decodePoint(bytes: ByteArray, params: ECParameterSpec): PublicKey {
        if (bytes.size != 65 || bytes[0] != 4.toByte()) throw HandshakeException("bad ephemeral key")
        val x = BigInteger(1, bytes.copyOfRange(1, 33))
        val y = BigInteger(1, bytes.copyOfRange(33, 65))
        val p = (params.curve.field as ECFieldFp).p
        if (x >= p || y >= p) throw HandshakeException("bad ephemeral key")
        val lhs = y.modPow(BigInteger.valueOf(2), p)
        val rhs = x.modPow(BigInteger.valueOf(3), p).add(params.curve.a.multiply(x)).add(params.curve.b).mod(p)
        if (lhs != rhs) throw HandshakeException("ephemeral key not on curve")
        return KeyFactory.getInstance("EC").generatePublic(ECPublicKeySpec(ECPoint(x, y), params))
    }

    fun verify(spki: ByteArray, sig: ByteArray, data: ByteArray) {
        val pub = try {
            KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki))
        } catch (e: Exception) {
            throw HandshakeException("bad identity key")
        }
        if (pub !is ECPublicKey || !isP256(pub.params)) throw HandshakeException("identity key is not P-256")
        val ok = try {
            Signature.getInstance("SHA256withECDSA").run {
                initVerify(pub)
                update(data)
                verify(sig)
            }
        } catch (e: Exception) {
            false
        }
        if (!ok) throw HandshakeException("bad signature")
    }
}

/** Length-prefixed AES-256-GCM frames with per-direction counters. */
class Framer(
    private val input: DataInputStream,
    private val output: OutputStream,
    sendKey: ByteArray,
    recvKey: ByteArray,
) {
    private val sendKey = SecretKeySpec(sendKey, "AES")
    private val recvKey = SecretKeySpec(recvKey, "AES")
    private val sendCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private val recvCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private var sendCounter = 0L
    private var recvCounter = 0L
    private val writeLock = Any()

    private fun nonce(c: Long): ByteArray {
        val n = ByteArray(12)
        for (i in 0 until 8) n[4 + i] = (c ushr (56 - 8 * i)).toByte()
        return n
    }

    fun send(plaintext: ByteArray) {
        synchronized(writeLock) {
            sendCipher.init(Cipher.ENCRYPT_MODE, sendKey, GCMParameterSpec(128, nonce(sendCounter++)))
            val ct = sendCipher.doFinal(plaintext)
            val n = ct.size
            output.write(byteArrayOf((n ushr 24).toByte(), (n ushr 16).toByte(), (n ushr 8).toByte(), n.toByte()))
            output.write(ct)
            output.flush()
        }
    }

    /** Blocking; only one thread may call this. */
    fun recv(): ByteArray {
        val n = input.readInt()
        if (n < 16 || n > Crypto.MAX_CIPHERTEXT) throw IOException("bad frame length $n")
        val ct = ByteArray(n)
        input.readFully(ct)
        val pt = try {
            recvCipher.init(Cipher.DECRYPT_MODE, recvKey, GCMParameterSpec(128, nonce(recvCounter)))
            recvCipher.doFinal(ct)
        } catch (e: java.security.GeneralSecurityException) {
            throw IOException("frame authentication failed")
        }
        recvCounter++
        if (pt.isEmpty()) throw IOException("empty frame")
        return pt
    }
}

class HandshakeResult(
    val framer: Framer,
    val th: ByteArray,
    val peerSpki: ByteArray,
    val peerName: String,
    val peerType: String,
) {
    val peerId: String = Crypto.deviceId(peerSpki)
    val sas: String = Crypto.sas(th)
}

object Handshake {
    fun run(socket: Socket, identity: Identity, isClient: Boolean, name: String, type: String): HandshakeResult {
        val oldTimeout = socket.soTimeout
        socket.soTimeout = Crypto.HANDSHAKE_TIMEOUT_MS
        try {
            return runInner(socket, identity, isClient, name, type)
        } catch (e: EOFException) {
            throw HandshakeException("peer closed during handshake")
        } finally {
            if (!socket.isClosed) socket.soTimeout = oldTimeout
        }
    }

    private fun runInner(socket: Socket, identity: Identity, isClient: Boolean, name: String, type: String): HandshakeResult {
        val input = DataInputStream(socket.getInputStream().buffered(64 * 1024))
        val output = socket.getOutputStream().buffered(64 * 1024)

        val eph = Crypto.newEcKeyPair()
        val mine = Crypto.MAGIC + byteArrayOf(Crypto.VERSION.toByte()) +
            Crypto.encodePoint(eph.public as ECPublicKey) + Crypto.randomBytes(32)
        output.write(mine)
        output.flush()
        val theirs = ByteArray(Crypto.HELLO_LEN)
        input.readFully(theirs)
        if (!theirs.copyOfRange(0, 4).contentEquals(Crypto.MAGIC)) throw HandshakeException("not a tether peer")
        if (theirs[4].toInt() != Crypto.VERSION) throw HandshakeException("unsupported protocol version ${theirs[4]}")
        val peerEph = Crypto.decodePoint(theirs.copyOfRange(5, 70), (eph.public as ECPublicKey).params)

        val (ch, sh) = if (isClient) mine to theirs else theirs to mine
        val th = Crypto.sha256(Crypto.LABEL, ch, sh)
        val z = KeyAgreement.getInstance("ECDH").run {
            init(eph.private)
            doPhase(peerEph, true)
            generateSecret()
        }
        val okm = Crypto.hkdf(z, th, Crypto.LABEL + " keys".toByteArray(), 64)
        val kc2s = okm.copyOfRange(0, 32)
        val ks2c = okm.copyOfRange(32, 64)
        val framer = if (isClient) Framer(input, output, kc2s, ks2c) else Framer(input, output, ks2c, kc2s)

        val role = if (isClient) "client" else "server"
        val peerRole = if (isClient) "server" else "client"
        val auth = jobj(
            "t" to "auth",
            "id_key" to Crypto.b64(identity.spki),
            "sig" to Crypto.b64(identity.sign(Crypto.LABEL + " auth $role".toByteArray() + th)),
            "name" to name,
            "type" to type,
        )
        framer.send(byteArrayOf(KIND_JSON) + auth.toString().toByteArray())
        val pt = framer.recv()
        if (pt[0] != KIND_JSON) throw HandshakeException("expected auth")
        val msg = try {
            parseObj(String(pt, 1, pt.size - 1, Charsets.UTF_8))
        } catch (e: Exception) {
            throw HandshakeException("malformed auth")
        }
        if (msg.str("t") != "auth") throw HandshakeException("expected auth")
        val peerSpki = try {
            Crypto.unb64(msg.str("id_key") ?: "")
        } catch (e: IllegalArgumentException) {
            throw HandshakeException("malformed auth")
        }
        val sig = try {
            Crypto.unb64(msg.str("sig") ?: "")
        } catch (e: IllegalArgumentException) {
            throw HandshakeException("malformed auth")
        }
        Crypto.verify(peerSpki, sig, Crypto.LABEL + " auth $peerRole".toByteArray() + th)
        if (Crypto.deviceId(peerSpki) == identity.id) throw HandshakeException("connected to self")
        return HandshakeResult(
            framer, th, peerSpki,
            (msg.str("name") ?: "unknown").take(100),
            (msg.str("type") ?: "unknown").take(20),
        )
    }
}
