"""Identity keys, the authenticated handshake, and the encrypted frame layer."""

import asyncio
import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

LABEL = b"tether-v1"
MAGIC = b"TTHR"
VERSION = 1
HELLO_LEN = 4 + 1 + 65 + 32
MAX_CIPHERTEXT = 4 * 1024 * 1024 + 64
HANDSHAKE_TIMEOUT = 15

KIND_JSON = 1
KIND_DATA = 2


class HandshakeError(Exception):
    pass


def device_id(spki: bytes) -> str:
    return hashlib.sha256(spki).hexdigest()


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


class Identity:
    def __init__(self, key: ec.EllipticCurvePrivateKey):
        self.key = key
        self.spki = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.id = device_id(self.spki)

    @classmethod
    def load_or_create(cls, path) -> "Identity":
        try:
            with open(path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            return cls(key)
        except FileNotFoundError:
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return cls(key)

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data, ec.ECDSA(hashes.SHA256()))


def verify(spki: bytes, sig: bytes, data: bytes) -> None:
    try:
        pub = serialization.load_der_public_key(spki)
    except ValueError as e:
        raise HandshakeError(f"bad identity key: {e}") from None
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
        raise HandshakeError("identity key is not P-256")
    try:
        pub.verify(sig, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise HandshakeError("bad signature") from None


def sas(th: bytes) -> str:
    n = int.from_bytes(hashlib.sha256(LABEL + b" sas" + th).digest()[:4], "big")
    return f"{n % 1_000_000:06d}"


def _nonce(counter: int) -> bytes:
    return b"\0\0\0\0" + counter.to_bytes(8, "big")


class Framer:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, send_key: bytes, recv_key: bytes):
        self.reader, self.writer = reader, writer
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._sc = 0
        self._rc = 0
        self._wlock = asyncio.Lock()

    async def send(self, plaintext: bytes) -> None:
        async with self._wlock:
            ct = self._send.encrypt(_nonce(self._sc), plaintext, None)
            self._sc += 1
            self.writer.write(len(ct).to_bytes(4, "big") + ct)
            await self.writer.drain()

    async def recv(self) -> bytes:
        n = int.from_bytes(await self.reader.readexactly(4), "big")
        if n < 16 or n > MAX_CIPHERTEXT:
            raise ConnectionError(f"bad frame length {n}")
        ct = await self.reader.readexactly(n)
        try:
            pt = self._recv.decrypt(_nonce(self._rc), ct, None)
        except InvalidTag:
            raise ConnectionError("frame authentication failed") from None
        self._rc += 1
        if not pt:
            raise ConnectionError("empty frame")
        return pt

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


class HandshakeResult:
    def __init__(self, framer, th, peer_spki, peer_info):
        self.framer = framer
        self.th = th
        self.peer_spki = peer_spki
        self.peer_id = device_id(peer_spki)
        self.peer_name = str(peer_info.get("name") or "unknown")[:100]
        self.peer_type = str(peer_info.get("type") or "unknown")[:20]
        self.sas = sas(th)


async def handshake(reader, writer, identity: Identity, is_client: bool, name: str, type_: str) -> HandshakeResult:
    return await asyncio.wait_for(_handshake(reader, writer, identity, is_client, name, type_), HANDSHAKE_TIMEOUT)


async def _handshake(reader, writer, identity, is_client, name, type_):
    eph = ec.generate_private_key(ec.SECP256R1())
    point = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    mine = MAGIC + bytes([VERSION]) + point + os.urandom(32)
    writer.write(mine)
    await writer.drain()
    theirs = await reader.readexactly(HELLO_LEN)
    if theirs[:4] != MAGIC:
        raise HandshakeError("not a tether peer")
    if theirs[4] != VERSION:
        raise HandshakeError(f"unsupported protocol version {theirs[4]}")
    try:
        peer_eph = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), theirs[5:70])
    except ValueError:
        raise HandshakeError("bad ephemeral key") from None

    ch, sh = (mine, theirs) if is_client else (theirs, mine)
    th = hashlib.sha256(LABEL + ch + sh).digest()
    z = eph.exchange(ec.ECDH(), peer_eph)
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=th, info=LABEL + b" keys").derive(z)
    k_c2s, k_s2c = okm[:32], okm[32:]
    framer = Framer(reader, writer, *((k_c2s, k_s2c) if is_client else (k_s2c, k_c2s)))

    role, peer_role = (b"client", b"server") if is_client else (b"server", b"client")
    auth = {
        "t": "auth",
        "id_key": b64e(identity.spki),
        "sig": b64e(identity.sign(LABEL + b" auth " + role + th)),
        "name": name,
        "type": type_,
    }
    await framer.send(bytes([KIND_JSON]) + json.dumps(auth).encode())
    pt = await framer.recv()
    if pt[0] != KIND_JSON:
        raise HandshakeError("expected auth")
    try:
        msg = json.loads(pt[1:])
        if msg.get("t") != "auth":
            raise HandshakeError("expected auth")
        peer_spki = b64d(msg["id_key"])
        sig = b64d(msg["sig"])
    except (ValueError, KeyError, TypeError):
        raise HandshakeError("malformed auth") from None
    verify(peer_spki, sig, LABEL + b" auth " + peer_role + th)
    if device_id(peer_spki) == identity.id:
        raise HandshakeError("connected to self")
    return HandshakeResult(framer, th, peer_spki, msg)
