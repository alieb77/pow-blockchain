import unittest

from powchain.keys import (
    PUBLIC_KEY_HEX_LENGTH,
    SIGNATURE_HEX_LENGTH,
    KeyPair,
    is_valid_public_key_hex,
    is_valid_signature_hex,
    verify_signature,
)

# Vecteur de test n°1 de la RFC 8032 (Ed25519) : garantit que l'encapsulation
# de la bibliothèque respecte le standard, sans rien réimplémenter.
RFC8032_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC8032_PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC8032_SIGNATURE_OF_EMPTY = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


class KeyPairTests(unittest.TestCase):
    def test_generate_gives_distinct_well_formed_addresses(self):
        first, second = KeyPair.generate(), KeyPair.generate()
        self.assertTrue(is_valid_public_key_hex(first.address))
        self.assertEqual(len(first.address), PUBLIC_KEY_HEX_LENGTH)
        self.assertNotEqual(first.address, second.address)
        self.assertEqual(first.address, first.public_key_hex)

    def test_from_seed_is_deterministic(self):
        seed = bytes(range(32))
        self.assertEqual(KeyPair.from_seed(seed).address, KeyPair.from_seed(seed).address)

    def test_seed_hex_round_trip(self):
        key = KeyPair.generate()
        restored = KeyPair.from_seed_hex(key.seed_hex)
        self.assertEqual(restored.address, key.address)
        self.assertEqual(restored.sign_hex(b"x"), key.sign_hex(b"x"))

    def test_rfc8032_vector_1(self):
        key = KeyPair.from_seed_hex(RFC8032_SEED)
        self.assertEqual(key.public_key_hex, RFC8032_PUBLIC_KEY)
        self.assertEqual(key.sign_hex(b""), RFC8032_SIGNATURE_OF_EMPTY)
        self.assertTrue(verify_signature(RFC8032_PUBLIC_KEY, RFC8032_SIGNATURE_OF_EMPTY, b""))

    def test_sign_and_verify_round_trip(self):
        key = KeyPair.generate()
        message = b"powchain"
        signature = key.sign_hex(message)
        self.assertEqual(len(signature), SIGNATURE_HEX_LENGTH)
        self.assertTrue(is_valid_signature_hex(signature))
        self.assertTrue(verify_signature(key.address, signature, message))

    def test_signature_is_deterministic(self):
        key = KeyPair.generate()
        self.assertEqual(key.sign_hex(b"m"), key.sign_hex(b"m"))

    def test_verification_fails_on_any_change(self):
        key, other = KeyPair.generate(), KeyPair.generate()
        signature = key.sign_hex(b"message")
        self.assertFalse(verify_signature(key.address, signature, b"messagf"))
        self.assertFalse(verify_signature(other.address, signature, b"message"))
        self.assertFalse(verify_signature(key.address, other.sign_hex(b"message"), b"message"))
        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
        self.assertFalse(verify_signature(key.address, flipped, b"message"))

    def test_verify_never_raises_on_garbage(self):
        key = KeyPair.generate()
        signature = key.sign_hex(b"m")
        cases = [
            (None, signature, b"m"),
            ("", signature, b"m"),
            ("zz" * 32, signature, b"m"),
            (key.address.upper(), signature, b"m"),
            (key.address, None, b"m"),
            (key.address, "abc", b"m"),
            (key.address, signature.upper(), b"m"),
            (key.address, signature, "m"),
            (key.address, signature, None),
            (12, 34, 56),
        ]
        for public_key, sig, message in cases:
            with self.subTest(public_key=public_key, signature=sig, message=message):
                self.assertFalse(verify_signature(public_key, sig, message))

    def test_from_seed_rejects_bad_input(self):
        for bad in (b"short", b"x" * 33, "not bytes", None, 42):
            with self.subTest(seed=bad):
                with self.assertRaises(ValueError):
                    KeyPair.from_seed(bad)
        for bad_hex in ("zz" * 32, "ab", None):
            with self.subTest(seed_hex=bad_hex):
                with self.assertRaises(ValueError):
                    KeyPair.from_seed_hex(bad_hex)

    def test_sign_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            KeyPair.generate().sign("texte")

    def test_repr_hides_private_key(self):
        key = KeyPair.generate()
        self.assertNotIn(key.seed_hex, repr(key))
        self.assertIn(key.address[:16], repr(key))

    def test_format_checks(self):
        self.assertTrue(is_valid_public_key_hex("a" * 64))
        self.assertFalse(is_valid_public_key_hex("A" * 64))
        self.assertFalse(is_valid_public_key_hex("a" * 63))
        self.assertTrue(is_valid_signature_hex("b" * 128))
        self.assertFalse(is_valid_signature_hex("b" * 127))
        self.assertFalse(is_valid_signature_hex(None))


if __name__ == "__main__":
    unittest.main()
