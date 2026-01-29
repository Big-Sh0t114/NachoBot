from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import os
import struct
import enum
import json
from typing import Tuple, Optional, Dict, Any


class FrameType(enum.IntEnum):
    HANDSHAKE_REQUEST = 1
    HANDSHAKE_RESPONSE = 2
    DATA = 3
    HEARTBEAT = 4


class CryptoManager:
    def __init__(self):
        self.private_key = X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.shared_key = None
        self.cipher = None

    def get_public_bytes(self) -> bytes:
        """获取公钥字节"""
        return self.public_key.public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )

    def compute_shared_key(self, peer_public_key_bytes: bytes):
        """计算共享密钥"""
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        peer_public_key = X25519PublicKey.from_public_bytes(peer_public_key_bytes)
        shared_key = self.private_key.exchange(peer_public_key)

        # 使用HKDF派生最终的会话密钥
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"handshake data",
        )
        self.shared_key = hkdf.derive(shared_key)
        self.cipher = ChaCha20Poly1305(self.shared_key)

    def encrypt_message(
        self, data: Dict[str, Any], sequence: int
    ) -> Tuple[bytes, bytes]:
        """加密消息"""
        if not self.cipher:
            raise ValueError("尚未建立加密会话")

        # 生成唯一的IV
        iv = os.urandom(12)

        # 序列化数据
        message = json.dumps(data).encode("utf-8")

        # 添加序列号作为关联数据
        associated_data = struct.pack("!Q", sequence)

        # 加密数据
        encrypted = self.cipher.encrypt(iv, message, associated_data)
        return iv, encrypted

    def decrypt_message(
        self, iv: bytes, encrypted_data: bytes, sequence: int
    ) -> Dict[str, Any]:
        """解密消息"""
        if not self.cipher:
            raise ValueError("尚未建立加密会话")

        # 使用序列号作为关联数据
        associated_data = struct.pack("!Q", sequence)

        # 解密数据
        decrypted = self.cipher.decrypt(iv, encrypted_data, associated_data)
        return json.loads(decrypted.decode("utf-8"))

    @staticmethod
    def create_frame(frame_type: FrameType, payload: bytes) -> bytes:
        """创建消息帧"""
        frame_length = len(payload) + 5  # 4字节长度 + 1字节类型 + 负载
        return struct.pack("!IB", frame_length, frame_type) + payload

    @staticmethod
    def parse_frame(data: bytes) -> Tuple[FrameType, bytes]:
        """解析消息帧"""
        frame_length, frame_type = struct.unpack("!IB", data[:5])
        return FrameType(frame_type), data[5:frame_length]

    @staticmethod
    def create_handshake_frame(public_key: bytes) -> bytes:
        """创建握手帧"""
        return CryptoManager.create_frame(FrameType.HANDSHAKE_REQUEST, public_key)

    @staticmethod
    def create_data_frame(iv: bytes, encrypted_data: bytes, sequence: int) -> bytes:
        """创建数据帧"""
        sequence_bytes = struct.pack("!Q", sequence)
        return CryptoManager.create_frame(
            FrameType.DATA, iv + sequence_bytes + encrypted_data
        )

    @staticmethod
    def parse_data_frame(payload: bytes) -> Tuple[bytes, bytes, int]:
        """解析数据帧"""
        iv = payload[:12]
        sequence = struct.unpack("!Q", payload[12:20])[0]
        encrypted_data = payload[20:]
        return iv, encrypted_data, sequence
