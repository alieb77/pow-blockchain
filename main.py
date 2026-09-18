"""Démonstration des Parties 1 à 4 : hashes, preuve de travail, signatures, soldes et mempool.

Lancer depuis le dossier du projet :

    python main.py

Le programme génère des clés, fait miner un premier bloc (création monétaire),
fait circuler les pièces via le mempool, puis simule des attaques : rejeu,
récompense gonflée, dépense au-delà du solde, double dépense.
"""

import sys
from dataclasses import replace
from datetime import datetime, timezone

from powchain import (
    HALVING_INTERVAL,
    TARGET_BLOCK_TIME,
    Block,
    Blockchain,
    InvalidChainError,
    InvalidTransactionError,
    KeyPair,
    Mempool,
    MempoolError,
    State,
    Transaction,
    block_reward,
    create_block,
    create_coinbase_transaction,
    create_signed_transaction,
    create_transaction,
    format_units,
    is_valid_chain,
    mine_block,
    parse_coin_amount,
    validate_chain,
)

RULE = "=" * 76


def print_title(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def format_timestamp(timestamp: int) -> str:
    iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{timestamp} ({iso})"


def name_of(address: str, names: dict[str, str]) -> str:
    return names.get(address, address[:12] + "...")


def print_state(state: State, names: dict[str, str]) -> None:
    print(f"  {'compte':<10} {'solde':>20} {'prochaine séquence':>20}")
    for address, account in sorted(state.accounts.items(), key=lambda item: name_of(item[0], names)):
        print(f"  {name_of(address, names):<10} {format_units(account.balance):>20} {account.next_sequence:>20}")
    print(f"  {'TOTAL':<10} {format_units(state.total_supply):>20}   (= récompenses émises)")


def print_block(block: Block, names: dict[str, str]) -> None:
    print(f"  bloc n°{block.index}  timestamp {format_timestamp(block.timestamp)}  difficulté {block.difficulty}  nonce {block.nonce}")
    print(f"  hash {block.hash}")
    for position, tx in enumerate(block.transactions):
        kind = "coinbase " if tx.is_coinbase else "tx       "
        print(
            f"    [{position}] {kind} {name_of(tx.sender, names):<9} -> {name_of(tx.recipient, names):<9} "
            f"{format_units(tx.amount):>18}  seq {tx.sequence}"
        )


def report_chain(label: str, blocks) -> bool:
    valid = is_valid_chain(blocks)
    print(f"{label}: {'true' if valid else 'false'}")
    if not valid:
        try:
            validate_chain(blocks)
        except InvalidChainError as error:
            print(f"  raison : {error}")
    return valid


def submit(pool: Mempool, state: State, label: str, transaction: Transaction) -> None:
    try:
        pool.add(transaction, state)
        print(f"  {label:<52} : ACCEPTÉE")
    except (InvalidTransactionError, MempoolError) as error:
        print(f"  {label:<52} : REJETÉE ({error})")


def mine_next(chain: Blockchain, pool: Mempool, miner: KeyPair, names: dict[str, str], coinbase_data: str = "") -> Block:
    selected = pool.select(chain.state)
    candidate = create_block(chain.last_block, selected, miner.address, coinbase_data=coinbase_data)
    result = mine_block(candidate)
    chain.add_block(result.block)
    dropped = pool.remove_confirmed(result.block, chain.state)
    print(f"  Minage : {result.attempts} essais, {result.elapsed_seconds * 1000:.1f} ms ; "
          f"{len(selected)} transaction(s) du mempool incluse(s), {len(dropped)} purgée(s)")
    print_block(result.block, names)
    return result.block


def hand_built(prev_block: Block, transactions, miner: KeyPair) -> Block:
    """Bloc assemblé sans create_block (qui refuserait), puis miné : ce que ferait un attaquant."""
    template = create_block(prev_block, [], miner.address, timestamp=prev_block.timestamp + TARGET_BLOCK_TIME)
    return mine_block(replace(template, transactions=template.transactions + tuple(transactions))).block


def demo_keys() -> tuple[dict[str, KeyPair], dict[str, str]]:
    print_title("1. Clés")
    wallets = {name: KeyPair.generate() for name in ("miner", "alice", "bob", "carol")}
    names = {key.address: name for name, key in wallets.items()}
    names["0" * 64] = "COINBASE"
    for name, key in wallets.items():
        print(f"  {name:<8}: {key.address}")
    return wallets, names


def demo_genesis_and_first_reward(wallets: dict[str, KeyPair], names: dict[str, str]) -> tuple[Blockchain, Mempool]:
    print_title("2. Au départ, personne n'a rien : la monnaie naît avec le minage")
    chain, pool = Blockchain(), Mempool()
    print(f"  État après le Genesis : {chain.state!r}")
    submit(pool, chain.state, "alice -> bob 1 COIN (alice n'a rien)", create_signed_transaction(wallets["alice"], wallets["bob"].address, parse_coin_amount("1")))

    print("\n  Le mineur mine le bloc n°1. Il ne contient que sa coinbase : la récompense.")
    mine_next(chain, pool, wallets["miner"], names, coinbase_data="premier bloc")
    coinbase = chain.last_block.coinbase
    print(f"\n  La coinbase a pour expéditeur l'adresse réservée {coinbase.sender[:16]}..., n'est pas")
    print(f"  signée, vaut exactement block_reward(1) = {format_units(coinbase.amount)} et porte")
    print(f"  sequence = hauteur du bloc ({coinbase.sequence}) pour être unique dans toute la chaîne.\n")
    print_state(chain.state, names)
    return chain, pool


def demo_mempool(chain: Blockchain, pool: Mempool, wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("3. Le mempool : salle d'attente validée contre l'état projeté")
    miner, alice, bob, carol = (wallets[n] for n in ("miner", "alice", "bob", "carol"))
    pay = create_signed_transaction
    submit(pool, chain.state, "miner -> alice 10 COIN, seq 0", pay(miner, alice.address, parse_coin_amount("10"), sequence=0))
    submit(pool, chain.state, "miner -> bob 5 COIN, seq 1", pay(miner, bob.address, parse_coin_amount("5"), sequence=1))
    submit(pool, chain.state, "miner -> bob 5 COIN, seq 1 (doublon)", pay(miner, bob.address, parse_coin_amount("5"), sequence=1))
    submit(pool, chain.state, "miner -> carol 1 COIN, seq 3 (saut de séquence)", pay(miner, carol.address, parse_coin_amount("1"), sequence=3))
    submit(pool, chain.state, "alice -> carol 4 COIN, seq 0 (financée par l'attente)", pay(alice, carol.address, parse_coin_amount("4"), sequence=0))
    submit(pool, chain.state, "alice -> carol 4 COIN, seq 1", pay(alice, carol.address, parse_coin_amount("4"), sequence=1))
    submit(pool, chain.state, "alice -> carol 4 COIN, seq 2 (solde projeté 2)", pay(alice, carol.address, parse_coin_amount("4"), sequence=2))
    submit(pool, chain.state, "coinbase soumise par un utilisateur", create_coinbase_transaction(alice.address, 2))
    submit(pool, chain.state, "transaction non signée", create_transaction(miner.address, alice.address, 1, sequence=2))
    print(f"\n  {len(pool)} transactions en attente. Le mineur les inclut dans le bloc n°2 :")
    mine_next(chain, pool, miner, names)
    print()
    print_state(chain.state, names)
    print(f"  Mempool après le bloc : {len(pool)} transaction(s) en attente.")

    print("\n  Rejeu : alice re-soumet sa transaction « seq 0 » déjà confirmée.")
    submit(pool, chain.state, "alice -> carol 4 COIN, seq 0 (rejeu)", pay(alice, carol.address, parse_coin_amount("4"), sequence=0))
    print("  En Partie 3 le rejeu passait ; l'état des comptes le bloque désormais (règle S1).")


def demo_attacks(chain: Blockchain, wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("4. Attaques contre la chaîne (blocs assemblés et minés par l'attaquant)")
    miner, alice, bob, carol = (wallets[n] for n in ("miner", "alice", "bob", "carol"))
    honest = list(chain.blocks)
    report_chain("Chain valid", honest)

    print("\n[A] Rejeu : la transaction confirmée « alice -> carol seq 0 » recopiée dans un nouveau bloc.")
    replayed = next(tx for tx in honest[2].transactions if tx.sender == alice.address)
    report_chain("Chain valid with replayed transaction", honest + [hand_built(honest[-1], [replayed], miner)])

    print("\n[B] Le mineur s'attribue 51 COIN au lieu de 50.")
    template = create_block(honest[-1], [], miner.address, timestamp=honest[-1].timestamp + TARGET_BLOCK_TIME)
    greedy = replace(template.coinbase, amount=template.coinbase.amount + parse_coin_amount("1"))
    greedy = replace(greedy, hash=greedy.calculate_hash())
    greedy_block = mine_block(replace(template, transactions=(greedy,))).block
    report_chain("Chain valid with inflated reward", honest + [greedy_block])

    print("\n[C] bob dépense 100 COIN alors qu'il en a 5 (transaction correctement signée).")
    overspend = create_signed_transaction(bob, carol.address, parse_coin_amount("100"), sequence=0)
    report_chain("Chain valid with overspending", honest + [hand_built(honest[-1], [overspend], miner)])

    print("\n[D] Double dépense : bob envoie ses 5 COIN à carol ET à alice dans le même bloc.")
    first = create_signed_transaction(bob, carol.address, parse_coin_amount("5"), sequence=0)
    second = create_signed_transaction(bob, alice.address, parse_coin_amount("5"), sequence=1)
    report_chain("Chain valid with double spend", honest + [hand_built(honest[-1], [first, second], miner)])

    print("\n  Chaque refus vient du rejeu de l'état : la chaîne stocke l'historique, jamais les")
    print("  soldes ; un validateur recalcule tout depuis le Genesis et refuse le premier bloc")
    print("  qui ne s'y conforme pas.")


def demo_emission() -> None:
    print_title("5. Émission monétaire")
    print(f"  {'hauteur':>10} | {'récompense':>18}")
    for height in (1, HALVING_INTERVAL - 1, HALVING_INTERVAL, 2 * HALVING_INTERVAL, 3 * HALVING_INTERVAL, 10 * HALVING_INTERVAL):
        print(f"  {height:>10} | {format_units(block_reward(height)):>18}")
    print(f"\n  Divisée par deux tous les {HALVING_INTERVAL} blocs : le total tend vers 21 000 000 COIN")
    print("  sans jamais l'atteindre. À un bloc toutes les 10 s, la première division")
    print(f"  arriverait après {HALVING_INTERVAL * TARGET_BLOCK_TIME // 86400} jours.")


def main() -> None:
    wallets, names = demo_keys()
    chain, pool = demo_genesis_and_first_reward(wallets, names)
    demo_mempool(chain, pool, wallets, names)
    demo_attacks(chain, wallets, names)
    demo_emission()
    print()


if __name__ == "__main__":
    # Garantit l'affichage des accents même si la sortie est redirigée vers un fichier.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
