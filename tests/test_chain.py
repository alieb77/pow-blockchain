import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.chain import Blockchain, chain_work, is_valid_chain, validate_chain
from powchain.errors import InvalidBlockError, InvalidChainError
from powchain.mining import mine_block
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.transaction import create_transaction, sign_transaction
from tests.helpers import ALICE, BOB, CAROL, DAVE, signed_tx

T1 = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME
T2 = T1 + TARGET_BLOCK_TIME


def mined(candidate):
    return mine_block(candidate).block


def build_blocks():
    genesis = create_genesis_block()
    block1 = mined(
        create_block(
            genesis,
            [signed_tx(ALICE, BOB, 150_000_000), signed_tx(BOB, CAROL, 25_000_000, "note")],
            timestamp=T1,
        )
    )
    block2 = mined(create_block(block1, [signed_tx(CAROL, DAVE, 1_000)], timestamp=T2))
    return [genesis, block1, block2]


def forge_first_transaction(block1, signer=None):
    """Bloc 1 dont la 1re transaction est gonflée x100, hash recalculé.

    Sans signer : la transaction reste NON signée (l'attaquant n'a pas la clé
    d'alice). Avec signer : simule un attaquant qui a volé la clé.
    """
    tx1, tx2 = block1.transactions
    forged_tx = create_transaction(tx1.sender, tx1.recipient, tx1.amount * 100, tx1.data, tx1.sequence)
    if signer is not None:
        forged_tx = sign_transaction(forged_tx, signer)
    return replace(block1, transactions=(forged_tx, tx2))


def rebuild_and_remine(genesis, forged_block1, following):
    """Reconstruit et re-mine toute la suite de la chaîne par-dessus forged_block1."""
    chain = [genesis, mined(forged_block1)]
    for original in following:
        chain.append(mined(create_block(chain[-1], original.transactions, timestamp=original.timestamp)))
    return chain


class ValidateChainTests(unittest.TestCase):
    def test_valid_chain(self):
        blocks = build_blocks()
        validate_chain(blocks)
        self.assertTrue(is_valid_chain(blocks))

    def test_genesis_alone_is_valid(self):
        self.assertTrue(is_valid_chain([create_genesis_block()]))

    def test_empty_chain_is_invalid(self):
        self.assertFalse(is_valid_chain([]))
        with self.assertRaisesRegex(InvalidChainError, "vide"):
            validate_chain([])

    def test_non_sequence_is_invalid(self):
        self.assertFalse(is_valid_chain(None))
        self.assertFalse(is_valid_chain("chain"))

    def test_foreign_genesis_is_rejected_even_if_mined(self):
        blocks = build_blocks()
        fake_genesis = mined(replace(blocks[0], timestamp=GENESIS_TIMESTAMP + 1, nonce=0))
        with self.assertRaisesRegex(InvalidChainError, "Genesis"):
            validate_chain([fake_genesis])

    def test_chain_must_start_with_genesis(self):
        _, block1, block2 = build_blocks()
        self.assertFalse(is_valid_chain([block1, block2]))

    def test_tampered_amount_without_recompute(self):
        genesis, block1, block2 = build_blocks()
        tx1, tx2 = block1.transactions
        forged_block = replace(block1, transactions=(replace(tx1, amount=tx1.amount * 100), tx2))
        with self.assertRaisesRegex(InvalidChainError, "hash incohérent"):
            validate_chain([genesis, forged_block, block2])

    def test_tampered_amount_with_tx_hash_recomputed_fails_on_signature(self):
        genesis, block1, block2 = build_blocks()
        with self.assertRaisesRegex(InvalidChainError, "non signée"):
            validate_chain([genesis, forge_first_transaction(block1), block2])

    def test_full_rewrite_and_remine_without_private_key_is_rejected(self):
        genesis, block1, block2 = build_blocks()
        rewritten = rebuild_and_remine(genesis, forge_first_transaction(block1), [block2])
        with self.assertRaisesRegex(InvalidChainError, "position 1 .* non signée"):
            validate_chain(rewritten)

    def test_full_rewrite_with_stolen_key_passes(self):
        # Limite intrinsèque : la sécurité repose sur le secret de la clé privée.
        genesis, block1, block2 = build_blocks()
        rewritten = rebuild_and_remine(genesis, forge_first_transaction(block1, signer=ALICE), [block2])
        self.assertTrue(is_valid_chain(rewritten))
        self.assertEqual(chain_work(rewritten), chain_work([genesis, block1, block2]))

    def test_replayed_transaction_is_accepted_for_now(self):
        # Limite documentée de la Partie 3 : sans état des comptes, la même
        # transaction signée peut être rejouée dans un bloc ultérieur. La
        # Partie 4 la rejettera grâce au numéro de séquence.
        genesis, block1, _ = build_blocks()
        replayed = block1.transactions[0]
        block2 = mined(create_block(block1, [replayed], timestamp=T2))
        self.assertTrue(is_valid_chain([genesis, block1, block2]))

    def test_tampered_block_remined_breaks_next_link(self):
        genesis, block1, block2 = build_blocks()
        # Re-miné à partir d'un autre nonce : bloc 1 cohérent en lui-même,
        # mais son hash change, donc le prev_hash du bloc 2 ne correspond plus.
        remined_block1 = mined(replace(block1, nonce=block1.nonce + 1))
        self.assertNotEqual(remined_block1.hash, block1.hash)
        with self.assertRaisesRegex(InvalidChainError, "position 2 .* prev_hash"):
            validate_chain([genesis, remined_block1, block2])

    def test_removed_block_is_detected(self):
        genesis, _, block2 = build_blocks()
        self.assertFalse(is_valid_chain([genesis, block2]))

    def test_reordered_blocks_are_detected(self):
        genesis, block1, block2 = build_blocks()
        self.assertFalse(is_valid_chain([genesis, block2, block1]))


class ChainWorkTests(unittest.TestCase):
    def test_work_is_sum_of_difficulties(self):
        blocks = build_blocks()
        self.assertEqual(chain_work(blocks), sum(block.difficulty for block in blocks))

    def test_faster_blocks_accumulate_more_work(self):
        genesis = create_genesis_block()
        fast = mined(create_block(genesis, [], timestamp=GENESIS_TIMESTAMP + 1))
        slow = mined(create_block(genesis, [], timestamp=GENESIS_TIMESTAMP + 100))
        self.assertGreater(chain_work([genesis, fast]), chain_work([genesis, slow]))


class BlockchainClassTests(unittest.TestCase):
    def test_new_chain_starts_with_genesis(self):
        chain = Blockchain()
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain.last_block, create_genesis_block())
        self.assertTrue(chain.is_valid())
        self.assertEqual(chain.total_work, create_genesis_block().difficulty)

    def test_add_block_appends_mined_block(self):
        chain = Blockchain()
        block = mined(create_block(chain.last_block, [signed_tx(ALICE, BOB, 1)], timestamp=T1))
        chain.add_block(block)
        self.assertEqual(len(chain), 2)
        self.assertIs(chain.last_block, block)
        self.assertEqual(chain.total_work, chain_work(chain.blocks))
        chain.validate()

    def test_add_block_rejects_unsigned_transaction(self):
        chain = Blockchain()
        unsigned = create_transaction(ALICE.address, BOB.address, 1)
        block = mined(create_block(chain.last_block, [], timestamp=T1))
        block = mined(replace(block, transactions=(unsigned,)))
        with self.assertRaisesRegex(InvalidBlockError, "non signée"):
            chain.add_block(block)
        self.assertEqual(len(chain), 1)

    def test_add_block_rejects_unmined_candidate(self):
        chain = Blockchain()
        candidate = create_block(chain.last_block, [], timestamp=T1)
        with self.assertRaisesRegex(InvalidBlockError, "preuve de travail"):
            chain.add_block(candidate)
        self.assertEqual(len(chain), 1)

    def test_add_block_rejects_unlinked_block(self):
        chain = Blockchain()
        chain.add_block(mined(create_block(chain.last_block, [], timestamp=T1)))
        detached = mined(create_block(create_genesis_block(), [], timestamp=T2))
        with self.assertRaises(InvalidBlockError):
            chain.add_block(detached)
        self.assertEqual(len(chain), 2)

    def test_from_blocks_accepts_valid_and_rejects_invalid(self):
        blocks = build_blocks()
        chain = Blockchain.from_blocks(blocks)
        self.assertEqual(chain.blocks, tuple(blocks))
        with self.assertRaises(InvalidChainError):
            Blockchain.from_blocks(blocks[1:])

    def test_blocks_property_is_a_snapshot(self):
        chain = Blockchain()
        snapshot = chain.blocks
        self.assertIsInstance(snapshot, tuple)
        chain.add_block(mined(create_block(chain.last_block, [], timestamp=T1)))
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(len(chain.blocks), 2)


if __name__ == "__main__":
    unittest.main()
