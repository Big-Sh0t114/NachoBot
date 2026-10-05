from __future__ import annotations

import struct
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import Mock, patch

import nacl.secret
from nacl.exceptions import CryptoError

from discord.errors import ClientException
from discord.sinks.core import Sink
from discord.sinks.errors import RecordingException
from discord.utils import MISSING
from discord.voice.packets.rtp import RTPPacket, decode
from discord.voice.receive.reader import PacketDecryptor, is_rtcp

from discord_client import DemuxingAudioReader, RetryAwareVoiceClient


MODE = "aead_xchacha20_poly1305_rtpsize"
KEY = bytes(range(32))
SSRC = 0x1E51
CAPTURED_RTCP_FEEDBACK = bytes.fromhex(
    "81cd000300001e511d152815545972d75fd810663a08f07f171e17648d260f2a91000000"
)


class _Connection:
    def __init__(self, *, ssrc_user_map=None):
        self.ssrc_user_map = ssrc_user_map or {}
        self.dave_session = None
        self.listeners = []
        self.removed_listeners = []

    def add_socket_listener(self, listener):
        self.listeners.append(listener)

    def remove_socket_listener(self, listener):
        self.removed_listeners.append(listener)
        self.listeners.remove(listener)


def _voice_client(*, ssrc_user_map=None):
    client = SimpleNamespace(
        _connection=_Connection(ssrc_user_map=ssrc_user_map),
        mode=MODE,
        secret_key=KEY,
    )
    return client


def _reader(client, key=KEY):
    reader = object.__new__(DemuxingAudioReader)
    reader.client = client
    reader.decryptor = PacketDecryptor(MODE, key, client)
    reader.error = None
    reader.packet_router = Mock()
    reader.speaking_timer = Mock()
    return reader


def _encrypted_rtp(key, *, payload_type=120, marker=False, sequence=1):
    second_octet = payload_type | (0x80 if marker else 0)
    header = struct.pack(">BBHII", 0x80, second_octet, sequence, sequence * 960, SSRC)
    nonce = sequence.to_bytes(4, "big")
    plaintext = b"\xf8\xff\xfe"
    ciphertext = nacl.secret.Aead(key).encrypt(
        plaintext, header, nonce + (b"\x00" * 20)
    ).ciphertext
    return header + ciphertext + nonce, plaintext


def _encrypted_rtcp_feedback(key):
    # RFC 4585 RTPFB/FMT=1, with the clear RTCP header authenticated as AAD.
    header = bytes.fromhex("81cd000300001e51")
    feedback = bytes.fromhex("0102030405060708")
    nonce = bytes.fromhex("12345678")
    ciphertext = nacl.secret.Aead(key).encrypt(
        feedback, header, nonce + (b"\x00" * 20)
    ).ciphertext
    return header + ciphertext + nonce, header + feedback


class VoiceReceiveDemuxTests(unittest.TestCase):
    def test_captured_feedback_and_unsupported_rtcp_never_reach_rtp_decryptor(self):
        self.assertEqual(len(CAPTURED_RTCP_FEEDBACK), 36)
        self.assertFalse(is_rtcp(CAPTURED_RTCP_FEEDBACK))
        self.assertIsInstance(decode(CAPTURED_RTCP_FEEDBACK), RTPPacket)

        client = _voice_client()
        reader = _reader(client)
        reader.decryptor.decrypt_rtp = Mock(side_effect=AssertionError("RTP decrypt"))

        candidates = [CAPTURED_RTCP_FEEDBACK]
        candidates.extend(
            bytes((0x80, packet_type, 0, 1)) + (b"\x00" * 32)
            for packet_type in range(192, 224)
            if packet_type not in (200, 201)
        )

        for packet in candidates:
            with self.subTest(packet_type=packet[1]):
                reader.callback(packet)

        reader.decryptor.decrypt_rtp.assert_not_called()
        reader.packet_router.feed_rtp.assert_not_called()
        reader.packet_router.feed_rtcp.assert_not_called()

    def test_rtcp_aead_primitive_works_while_old_rtp_path_fails(self):
        packet, plaintext = _encrypted_rtcp_feedback(KEY)
        self.assertFalse(is_rtcp(packet))
        self.assertIsInstance(decode(packet), RTPPacket)

        decryptor = PacketDecryptor(MODE, KEY, _voice_client())
        self.assertEqual(decryptor._decryptor_rtcp(packet), plaintext)
        with self.assertRaises(CryptoError):
            decryptor.decrypt_rtp(decode(packet))

        reader = _reader(_voice_client())
        reader.callback(packet)
        reader.packet_router.feed_rtp.assert_not_called()
        reader.packet_router.feed_rtcp.assert_not_called()

    def test_real_encrypted_opus_pt120_and_marker_pt120_still_route(self):
        client = _voice_client(ssrc_user_map={SSRC: 42})
        reader = _reader(client)

        for sequence, marker in ((1, False), (2, True)):
            packet, plaintext = _encrypted_rtp(
                KEY, marker=marker, sequence=sequence
            )
            with self.subTest(marker=marker):
                reader.callback(packet)

        routed = [call.args[0] for call in reader.packet_router.feed_rtp.call_args_list]
        self.assertEqual(len(routed), 2)
        self.assertEqual([packet.payload for packet in routed], [120, 120])
        self.assertEqual([packet.marker for packet in routed], [False, True])
        self.assertEqual(
            [packet.decrypted_data for packet in routed],
            [b"\xf8\xff\xfe", b"\xf8\xff\xfe"],
        )

    def test_supported_sender_and_receiver_reports_keep_sdk_control_path(self):
        sender_report = bytes.fromhex("80c80006") + (b"\x00" * 24)
        receiver_report = bytes.fromhex("80c90001") + bytes.fromhex("00001e51")
        reader = _reader(_voice_client())
        reader.decryptor.decrypt_rtp = Mock(side_effect=AssertionError("RTP decrypt"))

        reader.callback(sender_report)
        reader.callback(receiver_report)

        packets = [call.args[0] for call in reader.packet_router.feed_rtcp.call_args_list]
        self.assertEqual([packet.type for packet in packets], [200, 201])
        reader.decryptor.decrypt_rtp.assert_not_called()
        reader.packet_router.feed_rtp.assert_not_called()

    def test_short_and_invalid_version_datagrams_are_dropped(self):
        client = _voice_client()
        reader = _reader(client)
        reader.decryptor.decrypt_rtp = Mock(side_effect=AssertionError("RTP decrypt"))
        malformed = (
            b"",
            b"\x80",
            b"\x40\x78" + (b"\x00" * 16),
            b"\x80\x78" + (b"\x00" * 8),
            b"\x90\x78" + (b"\x00" * 12),
            bytes.fromhex("81c80000") + (b"\x00" * 23),
            bytes.fromhex("81c90000") + (b"\x00" * 27),
        )

        for packet in malformed:
            with self.subTest(packet_length=len(packet)):
                reader.callback(packet)

        reader.decryptor.decrypt_rtp.assert_not_called()
        reader.packet_router.feed_rtp.assert_not_called()
        reader.packet_router.feed_rtcp.assert_not_called()

    def test_real_bad_rtp_key_still_reports_crypto_failure(self):
        packet, _plaintext = _encrypted_rtp(KEY)
        client = _voice_client(ssrc_user_map={SSRC: 42})
        reader = _reader(client, key=b"\xff" * 32)

        with self.assertLogs("discord.voice.receive.reader", level="ERROR") as logs:
            reader.callback(packet)

        self.assertTrue(
            any("CryptoError while decoding a voice packet" in line for line in logs.output)
        )
        reader.packet_router.feed_rtp.assert_not_called()


class RetryAwareReaderLifecycleTests(unittest.TestCase):
    def _client(self):
        return SimpleNamespace(
            _reader=MISSING,
            mode=MODE,
            secret_key=KEY,
            _connection=_Connection(),
            is_connected=Mock(return_value=True),
            is_recording=Mock(return_value=False),
        )

    def test_start_listening_constructs_local_reader_and_stop_removes_listener(self):
        client = self._client()
        sink = Sink()
        callback = Mock()

        self.assertIs(
            RetryAwareVoiceClient.start_listening,
            RetryAwareVoiceClient.start_recording,
        )
        with (
            patch.object(DemuxingAudioReader, "start", autospec=True) as reader_start,
            warnings.catch_warnings(record=True) as warning_records,
        ):
            warnings.simplefilter("always")
            RetryAwareVoiceClient.start_listening(
                client, sink, callback, "capture-tag", sync_start=True
            )

        reader = client._reader
        self.assertIsInstance(reader, DemuxingAudioReader)
        self.assertIs(reader.after, callback)
        self.assertEqual(reader.args, ("capture-tag",))
        reader_start.assert_called_once_with(reader)
        self.assertEqual(len(warning_records), 2)

        # Exercise the inherited reader start/stop bookkeeping with inert
        # component starts, so no worker or network thread is created.
        for component in (
            reader.packet_router,
            reader.event_router,
            reader.speaking_timer,
            reader.keep_alive,
        ):
            component.start = Mock()
        reader._stop = Mock()
        reader.start()
        self.assertEqual(len(client._connection.listeners), 1)
        registered_listener = client._connection.listeners[0]

        RetryAwareVoiceClient.stop_recording(client)

        self.assertEqual(client._connection.listeners, [])
        self.assertEqual(len(client._connection.removed_listeners), 1)
        removed_listener = client._connection.removed_listeners[0]
        self.assertEqual(removed_listener, registered_listener)
        self.assertIs(removed_listener.__self__, reader)
        self.assertIs(removed_listener.__func__, DemuxingAudioReader.callback)
        self.assertIs(client._reader, MISSING)

    def test_sdk_recording_guards_are_preserved(self):
        client = self._client()
        sink = Sink()

        client.is_connected.return_value = False
        with self.assertRaises(RecordingException):
            RetryAwareVoiceClient.start_recording(client, sink)

        client.is_connected.return_value = True
        with self.assertRaises(TypeError):
            RetryAwareVoiceClient.start_recording(client, object())

        client.is_recording.return_value = True
        with self.assertRaises(ClientException):
            RetryAwareVoiceClient.start_recording(client, sink)


if __name__ == "__main__":
    unittest.main()
