import unittest

from powchain.errors import SerializationError
from powchain.serialization import (
    BLOCK_DOMAIN,
    TRANSACTION_DOMAIN,
    TRANSACTION_LIST_DOMAIN,
    encode_hash,
    encode_str,
    encode_uint32,
    encode_uint64,
    serialize_block_header,
    serialize_block_header_prefix,
    serialize_transaction_fields,
    serialize_transaction_hash_list,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


class EncodeUnsignedTests(unittest.TestCase):
    def test_uint64_is_big_endian_on_8_bytes(self):
        self.assertEqual(encode_uint64(0), b"\x00" * 8)
        self.assertEqual(encode_uint64(1), b"\x00" * 7 + b"\x01")
        self.assertEqual(encode_uint64(256), b"\x00" * 6 + b"\x01\x00")
        self.assertEqual(encode_uint64(2**64 - 1), b"\xff" * 8)

    def test_uint32_is_big_endian_on_4_bytes(self):
        self.assertEqual(encode_uint32(258), b"\x00\x00\x01\x02")

    def test_rejects_out_of_range_and_wrong_types(self):
        for bad_value in (-1, 2**64, True, False, 1.0, None, "1", b"\x01"):
            with self.subTest(value=bad_value):
                with self.assertRaises(SerializationError):
                    encode_uint64(bad_value)


class EncodeStrTests(unittest.TestCase):
    def test_empty_string_is_zero_length_prefix_only(self):
        self.assertEqual(encode_str(""), b"\x00\x00\x00\x00")

    def test_length_prefix_counts_utf8_bytes(self):
        self.assertEqual(encode_str("é"), b"\x00\x00\x00\x02\xc3\xa9")
        self.assertEqual(encode_str("abc"), b"\x00\x00\x00\x03abc")

    def test_rejects_non_strings(self):
        for bad_value in (None, b"abc", 1, 1.0, ["a"]):
            with self.subTest(value=bad_value):
                with self.assertRaises(SerializationError):
                    encode_str(bad_value)

    def test_rejects_lone_surrogates(self):
        with self.assertRaises(SerializationError):
            encode_str("\ud800")

    def test_concatenation_is_unambiguous(self):
        self.assertNotEqual(
            encode_str("ab") + encode_str("c"), encode_str("a") + encode_str("bc")
        )


class EncodeHashTests(unittest.TestCase):
    def test_hex_to_32_raw_bytes(self):
        self.assertEqual(encode_hash("ab" * 32), b"\xab" * 32)

    def test_rejects_malformed_hashes(self):
        for bad_value in ("AB" * 32, "ab" * 31, "ab" * 33, "zz" * 32, None, 42, b"\xab" * 32):
            with self.subTest(value=bad_value):
                with self.assertRaises(SerializationError):
                    encode_hash(bad_value)


class StructureSerializationTests(unittest.TestCase):
    def test_transaction_serialization_is_deterministic(self):
        first = serialize_transaction_fields("alice", "bob", 150_000_000, "note", 3)
        second = serialize_transaction_fields("alice", "bob", 150_000_000, "note", 3)
        self.assertEqual(first, second)

    def test_transaction_layout(self):
        expected = (
            TRANSACTION_DOMAIN
            + b"\x00\x00\x00\x05alice"
            + b"\x00\x00\x00\x03bob"
            + b"\x00" * 7 + b"\x07"
            + b"\x00\x00\x00\x00"
            + b"\x00" * 7 + b"\x02"
        )
        self.assertEqual(serialize_transaction_fields("alice", "bob", 7, "", 2), expected)

    def test_transaction_field_order_matters(self):
        self.assertNotEqual(
            serialize_transaction_fields("alice", "bob", 1, "", 0),
            serialize_transaction_fields("bob", "alice", 1, "", 0),
        )
        self.assertNotEqual(
            serialize_transaction_fields("alice", "bob", 1, "", 0),
            serialize_transaction_fields("alice", "bob", 1, "", 1),
        )

    def test_transaction_rejects_none_fields(self):
        for args in (
            ("alice", "bob", 1, None, 0),
            (None, "bob", 1, "", 0),
            ("alice", "bob", None, "", 0),
            ("alice", "bob", 1, "", None),
        ):
            with self.subTest(args=args):
                with self.assertRaises(SerializationError):
                    serialize_transaction_fields(*args)

    def test_empty_transaction_list(self):
        self.assertEqual(
            serialize_transaction_hash_list([]), TRANSACTION_LIST_DOMAIN + b"\x00\x00\x00\x00"
        )

    def test_transaction_list_layout_and_order(self):
        self.assertEqual(
            serialize_transaction_hash_list([HASH_A, HASH_B]),
            TRANSACTION_LIST_DOMAIN + b"\x00\x00\x00\x02" + b"\xaa" * 32 + b"\xbb" * 32,
        )
        self.assertNotEqual(
            serialize_transaction_hash_list([HASH_A, HASH_B]),
            serialize_transaction_hash_list([HASH_B, HASH_A]),
        )

    def test_transaction_list_rejects_non_sequences(self):
        for bad_value in (HASH_A, None, 3):
            with self.subTest(value=bad_value):
                with self.assertRaises(SerializationError):
                    serialize_transaction_hash_list(bad_value)

    def test_block_header_has_fixed_length(self):
        header = serialize_block_header(1, 1_767_225_600, HASH_A, HASH_B, 4096, 0)
        self.assertEqual(len(header), len(BLOCK_DOMAIN) + 8 + 8 + 32 + 32 + 8 + 8)
        self.assertTrue(header.startswith(BLOCK_DOMAIN))

    def test_block_header_is_prefix_plus_nonce(self):
        prefix = serialize_block_header_prefix(1, 1_767_225_600, HASH_A, HASH_B, 4096)
        header = serialize_block_header(1, 1_767_225_600, HASH_A, HASH_B, 4096, 77)
        self.assertEqual(header, prefix + encode_uint64(77))
        self.assertTrue(header.endswith(b"\x00" * 7 + b"\x4d"))

    def test_block_header_rejects_invalid_difficulty_and_nonce(self):
        with self.assertRaises(SerializationError):
            serialize_block_header(1, 1, HASH_A, HASH_B, -1, 0)
        with self.assertRaises(SerializationError):
            serialize_block_header(1, 1, HASH_A, HASH_B, 4096, 2**64)

    def test_domains_and_format_versions(self):
        self.assertNotEqual(TRANSACTION_DOMAIN, BLOCK_DOMAIN)
        self.assertNotEqual(TRANSACTION_DOMAIN, TRANSACTION_LIST_DOMAIN)
        self.assertEqual(TRANSACTION_DOMAIN, b"powchain/tx/v2")
        self.assertEqual(BLOCK_DOMAIN, b"powchain/block/v2")


if __name__ == "__main__":
    unittest.main()
