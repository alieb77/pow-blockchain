import json
import unittest

from powchain.errors import ProtocolError
from powchain.protocol import (
    ACCOUNT,
    BLOCKS,
    GET_ACCOUNT,
    GET_BLOCKS,
    HELLO,
    MAX_BLOCKS_PER_MESSAGE,
    MAX_MESSAGE_BYTES,
    MAX_PEERS_PER_MESSAGE,
    NEW_BLOCK,
    NEW_TRANSACTION,
    PEERS,
    PROTOCOL_VERSION,
    REJECT,
    SCHEMAS,
    Message,
    decode_message,
    encode_message,
    format_address,
    message,
    parse_address,
    validate_message,
)


def hello(**overrides):
    payload = dict(node_id="abc", version=PROTOCOL_VERSION, listen_port=5000, height=3, work=12288, tip_hash="f" * 64)
    payload.update(overrides)
    return Message(HELLO, payload)


class MessageSchemaTests(unittest.TestCase):
    def test_every_type_has_a_schema(self):
        for type_ in (HELLO, PEERS, NEW_TRANSACTION, NEW_BLOCK, GET_BLOCKS, BLOCKS, GET_ACCOUNT, ACCOUNT, REJECT):
            self.assertIn(type_, SCHEMAS)

    def test_valid_messages(self):
        validate_message(hello())
        validate_message(hello(listen_port=None))
        validate_message(message(PEERS, addresses=["127.0.0.1:5000"]))
        validate_message(message(GET_BLOCKS, from_index=0))
        validate_message(message(BLOCKS, blocks=[], has_more=False))
        validate_message(message(REJECT, hash="x", reason="y"))

    def test_unknown_type(self):
        with self.assertRaisesRegex(ProtocolError, "inconnu"):
            validate_message(Message("dance", {}))

    def test_missing_field(self):
        with self.assertRaisesRegex(ProtocolError, "manquant.*from_index"):
            validate_message(Message(GET_BLOCKS, {}))

    def test_unknown_field(self):
        with self.assertRaisesRegex(ProtocolError, "inconnu.*extra"):
            validate_message(Message(GET_BLOCKS, {"from_index": 1, "extra": 2}))

    def test_wrong_types(self):
        with self.assertRaisesRegex(ProtocolError, "height"):
            validate_message(hello(height="3"))
        with self.assertRaisesRegex(ProtocolError, "booléen"):
            validate_message(hello(height=True))
        with self.assertRaisesRegex(ProtocolError, "listen_port"):
            validate_message(hello(listen_port="5000"))
        with self.assertRaisesRegex(ProtocolError, "has_more"):
            validate_message(Message(BLOCKS, {"blocks": [], "has_more": 1}))
        with self.assertRaisesRegex(ProtocolError, "payload"):
            validate_message(Message(GET_BLOCKS, "x"))
        with self.assertRaises(ProtocolError):
            validate_message("hello")

    def test_message_factory_validates(self):
        with self.assertRaises(ProtocolError):
            message(GET_BLOCKS)
        msg = message(GET_BLOCKS, from_index=4)
        self.assertEqual(msg["from_index"], 4)

    def test_list_bounds(self):
        with self.assertRaisesRegex(ProtocolError, "adresses"):
            validate_message(Message(PEERS, {"addresses": ["a:1"] * (MAX_PEERS_PER_MESSAGE + 1)}))
        with self.assertRaisesRegex(ProtocolError, "blocs"):
            validate_message(Message(BLOCKS, {"blocks": [{}] * (MAX_BLOCKS_PER_MESSAGE + 1), "has_more": False}))


class EncodingTests(unittest.TestCase):
    def test_encode_is_one_json_line(self):
        raw = encode_message(hello())
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw), {"type": HELLO, "payload": hello().payload})

    def test_round_trip(self):
        for msg in (hello(), hello(listen_port=None), message(PEERS, addresses=["h:1"]), message(REJECT, hash="é", reason="ü")):
            self.assertEqual(decode_message(encode_message(msg)), msg)

    def test_decode_rejects_bad_input(self):
        for raw, pattern in (
            (b"not json\n", "JSON"),
            (b"[1, 2]\n", "enveloppe"),
            (b'{"type": "hello"}\n', "enveloppe"),
            (b'{"type": 1, "payload": {}}\n', "type"),
            (b'{"type": "hello", "payload": []}\n', "payload"),
            (b'{"type": "hello", "payload": {}, "x": 1}\n', "enveloppe"),
            (b'{"type": "nope", "payload": {}}\n', "inconnu"),
            (b"\xff\xfe\n", "JSON"),
        ):
            with self.assertRaisesRegex(ProtocolError, pattern, msg=raw):
                decode_message(raw)
        with self.assertRaises(ProtocolError):
            decode_message("texte")

    def test_size_limit(self):
        huge = b"{" + b" " * MAX_MESSAGE_BYTES + b"}\n"
        with self.assertRaisesRegex(ProtocolError, "volumineux"):
            decode_message(huge)
        with self.assertRaisesRegex(ProtocolError, "volumineux"):
            encode_message(message(REJECT, hash="x", reason="y" * MAX_MESSAGE_BYTES))


class AddressTests(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_address("127.0.0.1:5000"), ("127.0.0.1", 5000))
        self.assertEqual(parse_address("[::1]:80"), ("[::1]", 80))
        self.assertEqual(format_address("h", 1), "h:1")

    def test_parse_rejects(self):
        for bad in ("", "host", ":5000", "host:", "host:abc", "host:0", "host:70000", 5000, None):
            with self.assertRaises(ProtocolError, msg=repr(bad)):
                parse_address(bad)
