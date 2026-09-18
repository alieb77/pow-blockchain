import unittest

from powchain.address import (
    ADDRESS_LENGTH,
    has_valid_checksum,
    is_valid_address,
    normalize_address,
    to_checksummed_address,
)
from powchain.keys import KeyPair

# Vecteur figé : la somme de contrôle façon EIP-55 adaptée à SHA-256 de
# l'adresse "a" * 64. Elle prouve l'algorithme et que des lettres basculent
# bien en majuscule (sinon le contrôle ne détecterait rien).
A64 = "a" * 64
A64_CHECKSUMMED = "AAAaaaAAaAAaAAaAAaaAaAAAAaaAaaaAAaaAAaaAAaaAaAAaAAaaaaAAaaaaaAAA"


class AddressChecksumTests(unittest.TestCase):
    def test_pinned_vector(self):
        self.assertEqual(to_checksummed_address(A64), A64_CHECKSUMMED)

    def test_checksum_keeps_the_same_characters_case_aside(self):
        address = KeyPair.generate().address
        checksummed = to_checksummed_address(address)
        self.assertEqual(checksummed.lower(), address)
        self.assertEqual(len(checksummed), ADDRESS_LENGTH)

    def test_checksum_is_deterministic(self):
        address = KeyPair.generate().address
        self.assertEqual(to_checksummed_address(address), to_checksummed_address(address))

    def test_valid_checksum_accepts_its_own_output(self):
        for _ in range(20):
            checksummed = to_checksummed_address(KeyPair.generate().address)
            self.assertTrue(has_valid_checksum(checksummed))

    def test_lowercase_form_carries_no_checksum(self):
        # Une adresse qui a au moins une lettre a-f à mettre en majuscule ne
        # peut pas être acceptée tout en minuscules : sa casse ne prouve rien.
        self.assertNotEqual(A64_CHECKSUMMED, A64)
        self.assertFalse(has_valid_checksum(A64))

    def test_wrong_case_is_rejected(self):
        checksummed = to_checksummed_address(KeyPair.generate().address)
        # Bascule la casse du premier caractère qui est une lettre.
        for index, character in enumerate(checksummed):
            if character.isalpha():
                flipped = character.lower() if character.isupper() else character.upper()
                mangled = checksummed[:index] + flipped + checksummed[index + 1 :]
                self.assertFalse(has_valid_checksum(mangled))
                break
        else:
            self.skipTest("adresse sans lettre (cas trop rare pour ce test)")

    def test_normalize_requires_checksum_by_default(self):
        address = KeyPair.generate().address
        checksummed = to_checksummed_address(address)
        self.assertEqual(normalize_address(checksummed), address)
        if checksummed != address:
            with self.assertRaises(ValueError):
                normalize_address(address)  # tout en minuscules

    def test_normalize_unchecked_accepts_any_case(self):
        address = KeyPair.generate().address
        self.assertEqual(normalize_address(address, require_checksum=False), address)
        self.assertEqual(normalize_address(address.upper(), require_checksum=False), address)

    def test_rejects_malformed(self):
        for bad in ("xyz", "a" * 63, "a" * 65, "", None, 42):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    to_checksummed_address(bad)  # type: ignore[arg-type]
                self.assertFalse(has_valid_checksum(bad))

    def test_is_valid_address_unchanged(self):
        # La couche checksum ne change pas le format on-chain (hex minuscule).
        self.assertTrue(is_valid_address("a" * 64))
        self.assertFalse(is_valid_address(A64_CHECKSUMMED))


if __name__ == "__main__":
    unittest.main()
