import json
import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block, validate_block
from powchain.codec import (
    BLOCK_FIELDS,
    TRANSACTION_FIELDS,
    block_from_dict,
    block_to_dict,
    blocks_from_list,
    blocks_to_list,
    transaction_from_dict,
    transaction_to_dict,
)
from powchain.errors import CodecError, InvalidBlockError
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.transaction import create_coinbase_transaction, is_valid_transaction, validate_transaction
from tests.helpers import ALICE, BOB, MINER, coins, mined, signed_tx

T1 = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME

# Vecteur figé : la forme JSON du Genesis. S'il casse, le format du codec a changé
# et tous les nœuds / fichiers existants doivent être mis à jour ensemble.
EXPECTED_GENESIS_JSON = (
    '{"index": 0, "timestamp": 1767225600, "transactions": [], '
    '"prev_hash": "0000000000000000000000000000000000000000000000000000000000000000", '
    '"difficulty": 4096, "nonce": 5237, '
    '"hash": "000cb9d400f03a2aa00056576b6d77cba0f781cad0c5b26fae824ae2de93facd"}'
)


class TransactionCodecTests(unittest.TestCase):
    def test_round_trip_signed_transaction(self):
        tx = signed_tx(ALICE, BOB, coins(1), "note", sequence=3)
        data = transaction_to_dict(tx)
        self.assertEqual(tuple(data), TRANSACTION_FIELDS)
        self.assertEqual(transaction_from_dict(data), tx)
        self.assertTrue(is_valid_transaction(transaction_from_dict(json.loads(json.dumps(data)))))

    def test_round_trip_coinbase(self):
        tx = create_coinbase_transaction(MINER.address, 7, "hello")
        self.assertEqual(transaction_from_dict(transaction_to_dict(tx)), tx)

    def test_decoding_does_not_validate(self):
        """Décoder n'est pas valider : une transaction falsifiée est reconstruite telle quelle."""
        data = transaction_to_dict(signed_tx(ALICE, BOB, coins(1)))
        data["amount"] = coins(2)
        forged = transaction_from_dict(data)
        self.assertEqual(forged.amount, coins(2))
        self.assertFalse(is_valid_transaction(forged))
        with self.assertRaises(Exception):
            validate_transaction(forged)

    def test_rejects_non_dict(self):
        for bad in (None, "tx", 42, [], ["sender"]):
            with self.assertRaises(CodecError):
                transaction_from_dict(bad)

    def test_rejects_missing_field(self):
        data = transaction_to_dict(signed_tx(ALICE, BOB, coins(1)))
        del data["signature"]
        with self.assertRaisesRegex(CodecError, "manquant.*signature"):
            transaction_from_dict(data)

    def test_rejects_unknown_field(self):
        data = transaction_to_dict(signed_tx(ALICE, BOB, coins(1)))
        data["fee"] = 1
        with self.assertRaisesRegex(CodecError, "inconnu.*fee"):
            transaction_from_dict(data)

    def test_rejects_wrong_types(self):
        good = transaction_to_dict(signed_tx(ALICE, BOB, coins(1)))
        for field, bad in (
            ("sender", 1),
            ("amount", "1"),
            ("amount", 1.0),
            ("amount", True),
            ("amount", -1),
            ("amount", 2**64),
            ("sequence", None),
            ("data", b"x"),
            ("hash", None),
            ("signature", 0),
        ):
            data = dict(good)
            data[field] = bad
            with self.assertRaises(CodecError, msg=f"{field}={bad!r}"):
                transaction_from_dict(data)

    def test_to_dict_requires_transaction(self):
        with self.assertRaises(CodecError):
            transaction_to_dict({"sender": "x"})


class BlockCodecTests(unittest.TestCase):
    def setUp(self):
        self.genesis = create_genesis_block()
        self.block1 = mined(create_block(self.genesis, [signed_tx(MINER, ALICE, 1)], MINER.address, timestamp=T1))

    def test_genesis_json_is_pinned(self):
        self.assertEqual(json.dumps(block_to_dict(self.genesis)), EXPECTED_GENESIS_JSON)
        self.assertEqual(block_from_dict(json.loads(EXPECTED_GENESIS_JSON)), self.genesis)

    def test_round_trip_block_with_transactions(self):
        data = block_to_dict(self.block1)
        self.assertEqual(tuple(data), BLOCK_FIELDS)
        restored = block_from_dict(json.loads(json.dumps(data)))
        self.assertEqual(restored, self.block1)
        self.assertIsInstance(restored.transactions, tuple)
        validate_block(restored, self.genesis)

    def test_decoding_does_not_validate_block(self):
        data = block_to_dict(self.block1)
        data["nonce"] = 0
        restored = block_from_dict(data)
        self.assertEqual(restored.nonce, 0)
        with self.assertRaisesRegex(InvalidBlockError, "hash incohérent"):
            validate_block(restored, self.genesis)

    def test_rejects_bad_transactions_list(self):
        data = block_to_dict(self.block1)
        data["transactions"] = "none"
        with self.assertRaisesRegex(CodecError, "liste"):
            block_from_dict(data)
        data["transactions"] = [{"sender": "x"}]
        with self.assertRaisesRegex(CodecError, "transaction n°0"):
            block_from_dict(data)

    def test_rejects_missing_unknown_and_wrong_types(self):
        good = block_to_dict(self.block1)
        for mutate, pattern in (
            (lambda d: d.pop("nonce"), "manquant"),
            (lambda d: d.__setitem__("extra", 1), "inconnu"),
            (lambda d: d.__setitem__("index", "1"), "index"),
            (lambda d: d.__setitem__("difficulty", 0.5), "difficulty"),
            (lambda d: d.__setitem__("prev_hash", None), "prev_hash"),
            (lambda d: d.__setitem__("timestamp", -1), "timestamp"),
        ):
            data = json.loads(json.dumps(good))
            mutate(data)
            with self.assertRaisesRegex(CodecError, pattern):
                block_from_dict(data)

    def test_block_list_round_trip(self):
        blocks = (self.genesis, self.block1)
        self.assertEqual(blocks_from_list(blocks_to_list(blocks)), blocks)
        with self.assertRaisesRegex(CodecError, "liste de blocs"):
            blocks_from_list({"a": 1})
        with self.assertRaisesRegex(CodecError, "bloc n°1 de la liste"):
            blocks_from_list([block_to_dict(self.genesis), {}])

    def test_to_dict_requires_block(self):
        with self.assertRaises(CodecError):
            block_to_dict(replace)
