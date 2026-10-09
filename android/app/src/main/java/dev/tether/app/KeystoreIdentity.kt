package dev.tether.app

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import dev.tether.core.Identity
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/** The device identity lives in the Android Keystore; the private key never leaves it. */
class KeystoreIdentity : Identity {
    private val ks: KeyStore = KeyStore.getInstance(STORE).apply { load(null) }

    init {
        if (!ks.containsAlias(ALIAS)) {
            val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, STORE)
            gen.initialize(
                KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build(),
            )
            gen.generateKeyPair()
        }
    }

    override val spki: ByteArray = ks.getCertificate(ALIAS)!!.publicKey.encoded

    override fun sign(data: ByteArray): ByteArray {
        val key = ks.getKey(ALIAS, null) as PrivateKey
        return Signature.getInstance("SHA256withECDSA").run {
            initSign(key)
            update(data)
            sign()
        }
    }

    companion object {
        private const val STORE = "AndroidKeyStore"
        private const val ALIAS = "tether-identity"
    }
}
