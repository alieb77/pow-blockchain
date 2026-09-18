import unittest
from dataclasses import replace

from powchain.crypto import is_valid_hash_hex
from powchain.errors import InvalidTransactionError
from powchain.keys import is_valid_signature_hex
from powchain.money import MAX_MONEY
from powchain.money import block_reward
from powchain.transaction import (
    COINBASE_ADDRESS,
    MAX_DATA_BYTES,
    UNSIGNED,
    Transaction,
    calculate_transaction_hash,
    create_coinbase_transaction,
    create_signed_transaction,
    create_transaction,
    is_valid_transaction,
    sign_transaction,
    validate_transaction,
    verify_transaction_signature,
)
from tests.helpers import ALICE, BOB, CAROL, MALLORY, MINER, signed_tx

AMOUNT = 150_000_000

# Vecteur de référence du format canonique v2 : transaction ALICE -> BOB de
# 1.5 COIN, séquence 0, avec les clés de test déterministes de tests/helpers.py.
# Ed25519 étant déterministe, la signature aussi est figée. Si ce test casse,
# c'est que le format de sérialisation, la dérivation des clés de test ou le
# message signé a changé : décision explicite requise.
EXPECTED_ALICE_ADDRESS = "db72cd082d3a70ff4aad6e2ee9a9f4cdd17bca5b14b444d209a5bee6e652235d"
EXPECTED_ALICE_TO_BOB_HASH = "8f83bdc03deee1b2cb57258de3a9fe3f36862489fc8c16ef3bf70c7a295df3c8"
EXPECTED_ALICE_TO_BOB_SIGNATURE = (
    "2d34ad62f2742d7b49319941c9a0747dcc317e23ba633da923674decc24fe2ec"
    "740e89eecb6e2eafd10ec1564ba1bdc910dafd3e617c384b07d6007808b1600e"
)


class TransactionHashTests(unittest.TestCase):
    def test_same_data_gives_same_hash_and_signature(self):
        first = signed_tx(ALICE, BOB, AMOUNT, "note")
        second = signed_tx(ALICE, BOB, AMOUNT, "note")
        self.assertEqual(first.hash, second.hash)
        self.assertEqual(first.signature, second.signature)  # Ed25519 est déterministe
        self.assertEqual(first, second)

    def test_hash_and_signature_are_canonical_hex(self):
        tx = signed_tx(ALICE, BOB, AMOUNT)
        self.assertTrue(is_valid_hash_hex(tx.hash))
        self.assertTrue(is_valid_signature_hex(tx.signature))

    def test_every_field_changes_hash(self):
        base = signed_tx(ALICE, BOB, AMOUNT, "note", sequence=3)
        variants = [
            signed_tx(CAROL, BOB, AMOUNT, "note", sequence=3),
            signed_tx(ALICE, CAROL, AMOUNT, "note", sequence=3),
            signed_tx(ALICE, BOB, AMOUNT + 1, "note", sequence=3),
            signed_tx(ALICE, BOB, AMOUNT, "notes", sequence=3),
            signed_tx(ALICE, BOB, AMOUNT, "note", sequence=4),
            signed_tx(BOB, ALICE, AMOUNT, "note", sequence=3),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(variant.hash, base.hash)
                self.assertNotEqual(variant.signature, base.signature)

    def test_stored_hash_and_signature_are_not_part_of_the_computation(self):
        tx = signed_tx(ALICE, BOB, AMOUNT)
        forged = replace(tx, hash="f" * 64, signature="a" * 128)
        self.assertEqual(forged.calculate_hash(), tx.hash)

    def test_pinned_reference_vector(self):
        tx = signed_tx(ALICE, BOB, AMOUNT)
        self.assertEqual(ALICE.address, EXPECTED_ALICE_ADDRESS)
        self.assertEqual(tx.hash, EXPECTED_ALICE_TO_BOB_HASH)
        self.assertEqual(tx.signature, EXPECTED_ALICE_TO_BOB_SIGNATURE)

    def test_function_and_method_agree(self):
        tx = signed_tx(ALICE, BOB, AMOUNT, "x", sequence=9)
        self.assertEqual(tx.hash, calculate_transaction_hash(ALICE.address, BOB.address, AMOUNT, 0, "x", 9))
        self.assertEqual(tx.hash, tx.calculate_hash())
        self.assertEqual(tx.signing_message(), bytes.fromhex(tx.hash))


class CreateAndSignTests(unittest.TestCase):
    def test_create_transaction_is_unsigned_and_not_yet_valid(self):
        tx = create_transaction(ALICE.address, BOB.address, 1)
        self.assertFalse(tx.is_signed)
        self.assertEqual(tx.signature, UNSIGNED)
        self.assertFalse(is_valid_transaction(tx))
        with self.assertRaisesRegex(InvalidTransactionError, "non signée"):
            validate_transaction(tx)

    def test_sign_transaction_makes_it_valid(self):
        unsigned = create_transaction(ALICE.address, BOB.address, 1)
        signed = sign_transaction(unsigned, ALICE)
        self.assertTrue(signed.is_signed)
        self.assertTrue(is_valid_transaction(signed))
        self.assertEqual(replace(signed, signature=UNSIGNED), unsigned)

    def test_sign_requires_the_senders_key(self):
        unsigned = create_transaction(ALICE.address, BOB.address, 1)
        with self.assertRaisesRegex(InvalidTransactionError, "expéditeur"):
            sign_transaction(unsigned, BOB)

    def test_sign_rejects_bad_arguments(self):
        with self.assertRaises(InvalidTransactionError):
            sign_transaction("tx", ALICE)
        with self.assertRaises(InvalidTransactionError):
            sign_transaction(create_transaction(ALICE.address, BOB.address, 1), "clé")
        with self.assertRaises(InvalidTransactionError):
            create_signed_transaction(ALICE.address, BOB.address, 1)

    def test_create_signed_transaction_defaults(self):
        tx = create_signed_transaction(ALICE, BOB.address, 1)
        self.assertEqual(tx.sender, ALICE.address)
        self.assertEqual(tx.data, "")
        self.assertEqual(tx.sequence, 0)
        self.assertTrue(is_valid_transaction(tx))

    def test_zero_amount_allowed_when_data_present(self):
        self.assertTrue(is_valid_transaction(signed_tx(ALICE, BOB, 0, "pack:starter")))

    def test_max_money_allowed(self):
        self.assertTrue(is_valid_transaction(signed_tx(ALICE, BOB, MAX_MONEY)))

    def test_rejects_invalid_fields(self):
        alice, bob = ALICE.address, BOB.address
        cases = {
            "sender vide": ("", bob, 1, "", 0),
            "sender ancien format": ("alice", bob, 1, "", 0),
            "sender majuscules": (alice.upper(), bob, 1, "", 0),
            "sender None": (None, bob, 1, "", 0),
            "recipient trop court": (alice, bob[:-1], 1, "", 0),
            "amount negatif": (alice, bob, -1, "", 0),
            "amount float": (alice, bob, 1.5, "", 0),
            "amount bool": (alice, bob, True, "", 0),
            "amount chaine": (alice, bob, "5", "", 0),
            "amount > MAX_MONEY": (alice, bob, MAX_MONEY + 1, "", 0),
            "data None": (alice, bob, 1, None, 0),
            "data bytes": (alice, bob, 1, b"x", 0),
            "data trop longue": (alice, bob, 1, "x" * (MAX_DATA_BYTES + 1), 0),
            "data accents trop longue": (alice, bob, 1, "é" * (MAX_DATA_BYTES // 2 + 1), 0),
            "transaction vide": (alice, bob, 0, "", 0),
            "sequence negative": (alice, bob, 1, "", -1),
            "sequence float": (alice, bob, 1, "", 1.0),
            "sequence bool": (alice, bob, 1, "", False),
            "sequence trop grande": (alice, bob, 1, "", 2**64),
            "sequence chaine": (alice, bob, 1, "", "0"),
        }
        for label, args in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(InvalidTransactionError):
                    create_transaction(*args)


class ValidateTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tx = signed_tx(ALICE, BOB, AMOUNT, "note", sequence=1)

    def test_valid_transaction(self):
        validate_transaction(self.tx)
        self.assertTrue(is_valid_transaction(self.tx))
        self.assertTrue(verify_transaction_signature(self.tx))

    def test_tampered_amount_is_detected_by_hash(self):
        forged = replace(self.tx, amount=AMOUNT * 10)
        with self.assertRaisesRegex(InvalidTransactionError, "hash incohérent"):
            validate_transaction(forged)

    def test_tampered_amount_with_recomputed_hash_is_detected_by_signature(self):
        forged = replace(self.tx, amount=AMOUNT * 10)
        forged = replace(forged, hash=forged.calculate_hash())
        self.assertFalse(verify_transaction_signature(forged))
        with self.assertRaisesRegex(InvalidTransactionError, "signature invalide"):
            validate_transaction(forged)

    def test_signature_by_another_key_is_rejected(self):
        unsigned = create_transaction(ALICE.address, BOB.address, AMOUNT)
        forged = replace(unsigned, signature=MALLORY.sign_hex(unsigned.signing_message()))
        with self.assertRaisesRegex(InvalidTransactionError, "signature invalide"):
            validate_transaction(forged)

    def test_signature_copied_from_another_transaction_is_rejected(self):
        other = signed_tx(ALICE, CAROL, AMOUNT, "note", sequence=1)
        self.assertFalse(is_valid_transaction(replace(self.tx, signature=other.signature)))

    def test_malformed_signature(self):
        for bad in ("abc", self.tx.signature.upper(), self.tx.signature[:-2], None, 12):
            with self.subTest(signature=bad):
                self.assertFalse(is_valid_transaction(replace(self.tx, signature=bad)))
        with self.assertRaisesRegex(InvalidTransactionError, "mal formée"):
            validate_transaction(replace(self.tx, signature="abc"))

    def test_malformed_stored_hash(self):
        for bad_hash in ("", "abc", self.tx.hash.upper(), None):
            with self.subTest(hash=bad_hash):
                self.assertFalse(is_valid_transaction(replace(self.tx, hash=bad_hash)))

    def test_non_transaction_objects(self):
        for bad in (None, "tx", {"sender": ALICE.address}):
            with self.subTest(value=bad):
                self.assertFalse(is_valid_transaction(bad))
                self.assertFalse(verify_transaction_signature(bad))

    def test_direct_construction_with_bad_types(self):
        self.assertFalse(is_valid_transaction(Transaction(1, 2, 3, 4, 5, 6, 7, 8)))
        self.assertFalse(verify_transaction_signature(Transaction(1, 2, 3, 4, 5, 6, 7, 8)))
        bad = Transaction(ALICE.address, BOB.address, 1.0, 0, "", 0, "0" * 64, "0" * 128)
        self.assertFalse(is_valid_transaction(bad))


class CoinbaseTests(unittest.TestCase):
    def test_create_coinbase_transaction(self):
        coinbase = create_coinbase_transaction(MINER.address, 7, "message du mineur")
        self.assertTrue(coinbase.is_coinbase)
        self.assertFalse(coinbase.is_signed)
        self.assertEqual(coinbase.sender, COINBASE_ADDRESS)
        self.assertEqual(coinbase.recipient, MINER.address)
        self.assertEqual(coinbase.amount, block_reward(7))
        self.assertEqual(coinbase.sequence, 7)
        self.assertEqual(coinbase.data, "message du mineur")
        self.assertEqual(coinbase.hash, coinbase.calculate_hash())
        self.assertTrue(is_valid_transaction(coinbase))

    def test_coinbase_is_unique_per_height(self):
        self.assertNotEqual(
            create_coinbase_transaction(MINER.address, 1).hash,
            create_coinbase_transaction(MINER.address, 2).hash,
        )

    def test_coinbase_address_is_reserved_and_well_formed(self):
        self.assertEqual(len(COINBASE_ADDRESS), 64)
        self.assertFalse(signed_tx(ALICE, BOB, 1).is_coinbase)
        self.assertFalse(verify_transaction_signature(create_coinbase_transaction(MINER.address, 1)))

    def test_signed_coinbase_is_rejected(self):
        coinbase = create_coinbase_transaction(MINER.address, 1)
        with self.assertRaisesRegex(InvalidTransactionError, "coinbase .* signature"):
            validate_transaction(replace(coinbase, signature="a" * 128))
        with self.assertRaisesRegex(InvalidTransactionError, "coinbase ne se signe pas"):
            sign_transaction(coinbase, MINER)

    def test_coinbase_rejects_bad_inputs(self):
        for height in (0, -1, 1.0, True, None):
            with self.subTest(height=height):
                with self.assertRaises(InvalidTransactionError):
                    create_coinbase_transaction(MINER.address, height)
        for miner in ("alice", "", None):
            with self.subTest(miner=miner):
                with self.assertRaises(InvalidTransactionError):
                    create_coinbase_transaction(miner, 1)

    def test_zero_amount_coinbase_without_data_is_allowed(self):
        # Quand la récompense s'éteint (après ~64 divisions), la coinbase vaut 0.
        extinct = create_coinbase_transaction(MINER.address, 100 * 210_000)
        self.assertEqual(extinct.amount, 0)
        self.assertTrue(is_valid_transaction(extinct))


if __name__ == "__main__":
    unittest.main()
