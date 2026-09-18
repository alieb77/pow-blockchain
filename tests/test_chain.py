import unittest
from dataclasses import replace

from powchain.block import GENESIS_TIMESTAMP, create_block, create_genesis_block
from powchain.chain import Blockchain, chain_work, compute_state, is_valid_chain, validate_chain
from powchain.errors import InvalidBlockError, InvalidChainError
from powchain.money import block_reward
from powchain.proof_of_work import TARGET_BLOCK_TIME
from powchain.state import State
from powchain.transaction import create_transaction, sign_transaction
from tests.helpers import ALICE, BOB, CAROL, MINER, coins, mined, signed_tx

T1 = GENESIS_TIMESTAMP + TARGET_BLOCK_TIME
T2 = T1 + TARGET_BLOCK_TIME
T3 = T2 + TARGET_BLOCK_TIME
T4 = T3 + TARGET_BLOCK_TIME
REWARD = block_reward(1)


def build_blocks():
    """Genesis, puis : bloc 1 = coinbase seule ; bloc 2 = le mineur paie alice (10) et bob (5) ;
    bloc 3 = alice paie carol (1). Chaque bloc crédite aussi 50 COIN au mineur."""
    genesis = create_genesis_block()
    block1 = mined(create_block(genesis, [], MINER.address, timestamp=T1))
    block2 = mined(
        create_block(
            block1,
            [signed_tx(MINER, ALICE, coins(10), sequence=0), signed_tx(MINER, BOB, coins(5), "note", sequence=1)],
            MINER.address,
            timestamp=T2,
        )
    )
    block3 = mined(create_block(block2, [signed_tx(ALICE, CAROL, coins(1), sequence=0)], MINER.address, timestamp=T3))
    return [genesis, block1, block2, block3]


def hand_built(prev_block, transactions, timestamp):
    """Bloc assemblé sans passer par create_block pour les transactions (qui peut refuser), puis miné."""
    template = create_block(prev_block, [], MINER.address, timestamp=timestamp)
    return mined(replace(template, transactions=template.transactions + tuple(transactions)))


def rebuild_and_remine(prefix, forged_block, following):
    """Chaîne = prefix + forged_block re-miné + chaque bloc suivant re-chaîné et re-miné."""
    chain = list(prefix) + [mined(forged_block)]
    for original in following:
        chain.append(mined(replace(original, prev_hash=chain[-1].hash)))
    return chain


def forge_payment_to_alice(block2, factor, signer=None):
    """Bloc 2 dont le paiement du mineur à alice est multiplié par factor (hash recalculé).

    Sans signer : transaction NON signée (l'attaquant n'a pas la clé du mineur).
    """
    coinbase, to_alice, to_bob = block2.transactions
    forged = create_transaction(to_alice.sender, to_alice.recipient, to_alice.amount * factor, to_alice.data, to_alice.sequence)
    if signer is not None:
        forged = sign_transaction(forged, signer)
    return replace(block2, transactions=(coinbase, forged, to_bob))


class ValidateChainTests(unittest.TestCase):
    def test_valid_chain(self):
        blocks = build_blocks()
        validate_chain(blocks)
        self.assertTrue(is_valid_chain(blocks))

    def test_state_after_replay(self):
        state = compute_state(build_blocks())
        self.assertEqual(state.balance_of(MINER.address), 3 * REWARD - coins(15))
        self.assertEqual(state.balance_of(ALICE.address), coins(9))
        self.assertEqual(state.balance_of(BOB.address), coins(5))
        self.assertEqual(state.balance_of(CAROL.address), coins(1))
        self.assertEqual(state.next_sequence_of(MINER.address), 2)
        self.assertEqual(state.next_sequence_of(ALICE.address), 1)
        self.assertEqual(state.next_sequence_of(BOB.address), 0)
        self.assertEqual(state.total_supply, 3 * REWARD)

    def test_genesis_alone_is_valid_with_empty_state(self):
        self.assertTrue(is_valid_chain([create_genesis_block()]))
        self.assertEqual(compute_state([create_genesis_block()]), State())

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
        blocks = build_blocks()
        self.assertFalse(is_valid_chain(blocks[1:]))

    def test_replayed_transaction_is_rejected(self):
        blocks = build_blocks()
        replayed = blocks[3].transactions[1]  # alice -> carol, séquence 0, déjà confirmée
        block4 = hand_built(blocks[3], [replayed], T4)
        with self.assertRaisesRegex(InvalidChainError, "position 4 .* sequence 0 .* attend la sequence 1"):
            validate_chain(blocks + [block4])

    def test_sequence_gap_is_rejected(self):
        blocks = build_blocks()
        block4 = hand_built(blocks[3], [signed_tx(ALICE, CAROL, coins(1), sequence=5)], T4)
        with self.assertRaisesRegex(InvalidChainError, "sequence 5 .* attend la sequence 1"):
            validate_chain(blocks + [block4])

    def test_insufficient_balance_is_rejected(self):
        blocks = build_blocks()
        block4 = hand_built(blocks[3], [signed_tx(ALICE, CAROL, coins(50), sequence=1)], T4)
        with self.assertRaisesRegex(InvalidChainError, "solde insuffisant"):
            validate_chain(blocks + [block4])

    def test_double_spend_in_one_block_is_rejected(self):
        blocks = build_blocks()
        spend_all = signed_tx(ALICE, CAROL, coins(9), sequence=1)
        spend_again = signed_tx(ALICE, BOB, coins(9), sequence=2)
        block4 = hand_built(blocks[3], [spend_all, spend_again], T4)
        with self.assertRaisesRegex(InvalidChainError, "transaction n°2 .* solde insuffisant"):
            validate_chain(blocks + [block4])

    def test_inflated_reward_is_rejected(self):
        genesis, block1, block2, block3 = build_blocks()
        greedy = replace(block1.coinbase, amount=block1.coinbase.amount + 1)
        greedy = replace(greedy, hash=greedy.calculate_hash())
        rewritten = rebuild_and_remine([genesis], replace(block1, transactions=(greedy,)), [block2, block3])
        with self.assertRaisesRegex(InvalidChainError, "position 1 .* récompense"):
            validate_chain(rewritten)

    def test_tampered_amount_without_recompute(self):
        genesis, block1, block2, block3 = build_blocks()
        coinbase, to_alice, to_bob = block2.transactions
        forged = replace(block2, transactions=(coinbase, replace(to_alice, amount=coins(20)), to_bob))
        with self.assertRaisesRegex(InvalidChainError, "hash incohérent"):
            validate_chain([genesis, block1, forged, block3])

    def test_forged_without_private_key_is_rejected_even_if_remined(self):
        genesis, block1, block2, block3 = build_blocks()
        rewritten = rebuild_and_remine([genesis, block1], forge_payment_to_alice(block2, 2), [block3])
        with self.assertRaisesRegex(InvalidChainError, "position 2 .* non signée"):
            validate_chain(rewritten)

    def test_forged_with_stolen_key_passes_within_balance(self):
        genesis, block1, block2, block3 = build_blocks()
        rewritten = rebuild_and_remine([genesis, block1], forge_payment_to_alice(block2, 2, signer=MINER), [block3])
        self.assertTrue(is_valid_chain(rewritten))
        self.assertEqual(compute_state(rewritten).balance_of(ALICE.address), coins(19))
        self.assertEqual(chain_work(rewritten), chain_work([genesis, block1, block2, block3]))

    def test_forged_with_stolen_key_beyond_balance_is_rejected(self):
        genesis, block1, block2, block3 = build_blocks()
        rewritten = rebuild_and_remine([genesis, block1], forge_payment_to_alice(block2, 10, signer=MINER), [block3])
        with self.assertRaisesRegex(InvalidChainError, "solde insuffisant"):
            validate_chain(rewritten)

    def test_tampered_block_remined_breaks_next_link(self):
        genesis, block1, block2, block3 = build_blocks()
        remined_block1 = mined(replace(block1, nonce=block1.nonce + 1))
        self.assertNotEqual(remined_block1.hash, block1.hash)
        with self.assertRaisesRegex(InvalidChainError, "position 2 .* prev_hash"):
            validate_chain([genesis, remined_block1, block2, block3])

    def test_removed_block_is_detected(self):
        genesis, block1, block2, block3 = build_blocks()
        self.assertFalse(is_valid_chain([genesis, block1, block3]))

    def test_reordered_blocks_are_detected(self):
        genesis, block1, block2, block3 = build_blocks()
        self.assertFalse(is_valid_chain([genesis, block2, block1, block3]))


class ChainWorkTests(unittest.TestCase):
    def test_work_is_sum_of_difficulties(self):
        blocks = build_blocks()
        self.assertEqual(chain_work(blocks), sum(block.difficulty for block in blocks))

    def test_faster_blocks_accumulate_more_work(self):
        genesis = create_genesis_block()
        fast = mined(create_block(genesis, [], MINER.address, timestamp=GENESIS_TIMESTAMP + 1))
        slow = mined(create_block(genesis, [], MINER.address, timestamp=GENESIS_TIMESTAMP + 100))
        self.assertGreater(chain_work([genesis, fast]), chain_work([genesis, slow]))


class BlockchainClassTests(unittest.TestCase):
    def test_new_chain_starts_with_genesis_and_empty_state(self):
        chain = Blockchain()
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain.last_block, create_genesis_block())
        self.assertTrue(chain.is_valid())
        self.assertEqual(chain.state, State())
        self.assertEqual(chain.state.total_supply, 0)
        self.assertEqual(chain.total_work, create_genesis_block().difficulty)

    def test_add_block_updates_state(self):
        chain = Blockchain()
        chain.add_block(mined(create_block(chain.last_block, [], MINER.address, timestamp=T1)))
        self.assertEqual(chain.state.balance_of(MINER.address), REWARD)
        chain.add_block(mined(create_block(chain.last_block, [signed_tx(MINER, ALICE, coins(10))], MINER.address, timestamp=T2)))
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain.state.balance_of(MINER.address), 2 * REWARD - coins(10))
        self.assertEqual(chain.state.balance_of(ALICE.address), coins(10))
        self.assertEqual(chain.state.next_sequence_of(MINER.address), 1)
        self.assertEqual(chain.state, compute_state(chain.blocks))
        chain.validate()

    def test_add_block_rejects_state_invalid_block_and_keeps_state(self):
        chain = Blockchain()
        chain.add_block(mined(create_block(chain.last_block, [], MINER.address, timestamp=T1)))
        state_before = chain.state
        unfunded = mined(create_block(chain.last_block, [signed_tx(ALICE, BOB, coins(1))], MINER.address, timestamp=T2))
        with self.assertRaisesRegex(InvalidBlockError, "solde insuffisant"):
            chain.add_block(unfunded)
        self.assertEqual(len(chain), 2)
        self.assertIs(chain.state, state_before)

    def test_add_block_rejects_unsigned_transaction(self):
        chain = Blockchain()
        block = hand_built(chain.last_block, [create_transaction(ALICE.address, BOB.address, 1)], T1)
        with self.assertRaisesRegex(InvalidBlockError, "non signée"):
            chain.add_block(block)
        self.assertEqual(len(chain), 1)

    def test_add_block_rejects_unmined_candidate(self):
        chain = Blockchain()
        candidate = create_block(chain.last_block, [], MINER.address, timestamp=T1)
        with self.assertRaisesRegex(InvalidBlockError, "preuve de travail"):
            chain.add_block(candidate)
        self.assertEqual(len(chain), 1)

    def test_add_block_rejects_unlinked_block(self):
        chain = Blockchain()
        chain.add_block(mined(create_block(chain.last_block, [], MINER.address, timestamp=T1)))
        detached = mined(create_block(create_genesis_block(), [], MINER.address, timestamp=T2))
        with self.assertRaises(InvalidBlockError):
            chain.add_block(detached)
        self.assertEqual(len(chain), 2)

    def test_from_blocks_accepts_valid_and_rejects_invalid(self):
        blocks = build_blocks()
        chain = Blockchain.from_blocks(blocks)
        self.assertEqual(chain.blocks, tuple(blocks))
        self.assertEqual(chain.state, compute_state(blocks))
        with self.assertRaises(InvalidChainError):
            Blockchain.from_blocks(blocks[1:])

    def test_blocks_property_is_a_snapshot(self):
        chain = Blockchain()
        snapshot = chain.blocks
        self.assertIsInstance(snapshot, tuple)
        chain.add_block(mined(create_block(chain.last_block, [], MINER.address, timestamp=T1)))
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(len(chain.blocks), 2)


if __name__ == "__main__":
    unittest.main()
