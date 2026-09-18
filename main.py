"""Démonstration des Parties 1 à 3 : hashes, preuve de travail, clés et signatures.

Lancer depuis le dossier du projet :

    python main.py

Le programme génère des clés, construit des transactions signées, mine une
chaîne, montre l'ajustement de la difficulté, puis simule plusieurs
attaquants : sans clé privée, avec une clé volée, et par rejeu.
"""

import sys
from dataclasses import replace
from datetime import datetime, timezone

from powchain import (
    TARGET_BLOCK_TIME,
    Block,
    Blockchain,
    InvalidBlockError,
    InvalidChainError,
    InvalidTransactionError,
    KeyPair,
    Transaction,
    create_block,
    create_signed_transaction,
    create_transaction,
    format_target,
    format_units,
    hash_meets_target,
    is_valid_chain,
    mine_block,
    parse_coin_amount,
    sign_transaction,
    validate_block,
    validate_chain,
    validate_transaction,
    verify_transaction_signature,
)

RULE = "=" * 76


def print_title(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def format_timestamp(timestamp: int) -> str:
    iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{timestamp} ({iso})"


def short(value: str) -> str:
    return f"{value[:12]}...{value[-6:]}"


def print_transaction(label: str, transaction: Transaction, names: dict[str, str]) -> None:
    print(f"  {label}")
    print(f"    sender    : {short(transaction.sender)} ({names.get(transaction.sender, '?')})")
    print(f"    recipient : {short(transaction.recipient)} ({names.get(transaction.recipient, '?')})")
    print(f"    amount    : {transaction.amount} unités = {format_units(transaction.amount)}")
    print(f"    data      : {transaction.data!r}")
    print(f"    sequence  : {transaction.sequence}")
    print(f"    hash      : {transaction.hash}")
    print(f"    signature : {transaction.signature[:64]}")
    print(f"                {transaction.signature[64:]}")


def print_block(block: Block, names: dict[str, str]) -> None:
    print(f"  index        : {block.index}")
    print(f"  timestamp    : {format_timestamp(block.timestamp)}")
    print(f"  prev_hash    : {block.prev_hash}")
    print(f"  difficulty   : {block.difficulty}")
    print(f"  nonce        : {block.nonce}")
    print(f"  transactions : {len(block.transactions)}")
    for position, transaction in enumerate(block.transactions):
        print(
            f"    [{position}] {names.get(transaction.sender, '?')} -> "
            f"{names.get(transaction.recipient, '?')} : {format_units(transaction.amount)}"
            f"   (hash {transaction.hash[:16]}...)"
        )
    print(f"  cible        : {format_target(block.target)}")
    print(f"  hash         : {block.hash}")
    print(f"  hash <= cible: {hash_meets_target(block.hash, block.difficulty)}")


def report_transaction(label: str, transaction: Transaction) -> None:
    try:
        validate_transaction(transaction)
        print(f"  {label} : valide")
    except InvalidTransactionError as error:
        print(f"  {label} : REJETÉE ({error})")


def report_block(label: str, block: Block, prev_block: Block) -> None:
    try:
        validate_block(block, prev_block)
        print(f"  {label} : valide")
    except InvalidBlockError as error:
        print(f"  {label} : REJETÉ ({error})")


def report_chain(label: str, blocks) -> bool:
    valid = is_valid_chain(blocks)
    print(f"{label}: {'true' if valid else 'false'}")
    if not valid:
        try:
            validate_chain(blocks)
        except InvalidChainError as error:
            print(f"  raison : {error}")
    return valid


def mined(candidate: Block) -> Block:
    return mine_block(candidate).block


def demo_keys() -> tuple[dict[str, KeyPair], dict[str, str]]:
    print_title("1. Clés et adresses (Ed25519)")
    wallets = {name: KeyPair.generate() for name in ("alice", "bob", "carol", "mallory")}
    names = {key.address: name for name, key in wallets.items()}
    print("  Chaque participant tire une clé privée aléatoire de 32 octets ; sa clé publique,")
    print("  en hexadécimal, lui sert d'adresse. La clé privée ne quitte jamais son wallet.\n")
    for name, key in wallets.items():
        print(f"  {name:<8}: {key.address}")
    print(f"\n  repr() d'une clé ne révèle rien : {wallets['alice']!r}")
    print("  Relancez le programme : les adresses changent, donc les hashes des transactions aussi.")
    return wallets, names


def demo_signed_transactions(wallets: dict[str, KeyPair], names: dict[str, str]) -> tuple[Transaction, Transaction]:
    print_title("2. Transactions signées")
    alice, bob, carol, mallory = (wallets[n] for n in ("alice", "bob", "carol", "mallory"))
    tx_alice = create_signed_transaction(alice, bob.address, parse_coin_amount("1.5"))
    tx_bob = create_signed_transaction(
        bob, carol.address, parse_coin_amount("0.25"), data='{"type":"demo","pack_id":"starter-001"}', sequence=0
    )
    print_transaction("tx_alice", tx_alice, names)
    print()
    print_transaction("tx_bob", tx_bob, names)
    print("\n  La signature couvre le hash, donc tout le contenu : la clé privée d'alice a")
    print("  signé exactement « alice paie 1.5 COIN à bob, séquence 0 ».")
    print(f"  Vérification avec la clé publique (= adresse) d'alice : {verify_transaction_signature(tx_alice)}")

    print("\n  Ce que la validation rejette :")
    report_transaction("non signée (create_transaction seul)", create_transaction(alice.address, bob.address, 1))
    forged = replace(tx_alice, amount=parse_coin_amount("100"))
    report_transaction("montant modifié, hash conservé", forged)
    forged = replace(forged, hash=forged.calculate_hash())
    report_transaction("montant modifié, hash recalculé", forged)
    unsigned = create_transaction(alice.address, bob.address, parse_coin_amount("100"))
    report_transaction(
        "signée par mallory au nom d'alice", replace(unsigned, signature=mallory.sign_hex(unsigned.signing_message()))
    )
    report_transaction("signature copiée depuis tx_bob", replace(tx_alice, signature=tx_bob.signature))
    try:
        sign_transaction(unsigned, mallory)
    except InvalidTransactionError as error:
        print(f"  sign_transaction(tx d'alice, clé de mallory) : REFUSÉ ({error})")
    return tx_alice, tx_bob


def demo_mining(chain: Blockchain, transactions: tuple[Transaction, ...], names: dict[str, str]) -> None:
    print_title("3. Bloc n°1 miné avec ces transactions")
    candidate = create_block(chain.last_block, transactions)
    report_block("Candidat (nonce 0)", candidate, chain.last_block)
    result = mine_block(candidate)
    print(
        f"  Minage : nonce {result.block.nonce} trouvé en {result.attempts} essais, "
        f"{result.elapsed_seconds * 1000:.1f} ms ({result.hash_rate:,.0f} essais/s)"
    )
    chain.add_block(result.block)
    print_block(result.block, names)
    report_chain("Chain valid", chain.blocks)

    print(f"\n  Ajustement de la difficulté (cible : un bloc toutes les {TARGET_BLOCK_TIME} s) :")
    print(f"  {'bloc':>4} | {'écart (s)':>10} | {'difficulté':>10} | {'essais':>8}")
    for delta in (1, 1, 60):
        prev_block = chain.last_block
        result = mine_block(create_block(prev_block, [], timestamp=prev_block.timestamp + delta))
        chain.add_block(result.block)
        print(f"  {result.block.index:>4} | {delta:>10} | {result.block.difficulty:>10} | {result.attempts:>8}")


def demo_attacks(chain: Blockchain, wallets: dict[str, KeyPair]) -> None:
    print_title("4. Attaques : sans clé, avec clé volée, par rejeu")
    genesis, block1, *following = chain.blocks
    honest_tx = block1.transactions[0]
    other_txs = block1.transactions[1:]
    forged_amount = parse_coin_amount("100")

    def rebuild_and_remine(forged_tx: Transaction) -> list[Block]:
        # L'attaquant assemble ses blocs à la main (create_block refuserait une
        # transaction invalide) puis re-mine chacun d'eux dans l'ordre.
        rewritten = [genesis, mined(replace(block1, transactions=(forged_tx,) + other_txs))]
        for original in following:
            rewritten.append(mined(replace(original, prev_hash=rewritten[-1].hash)))
        return rewritten

    print("\n[A] Montant de la 1re transaction du bloc n°1 modifié, aucun hash recalculé.")
    forged_tx = replace(honest_tx, amount=forged_amount)
    report_chain("Chain valid after tampering", [genesis, replace(block1, transactions=(forged_tx,) + other_txs), *following])

    print("\n[B] Mallory dispose d'une puissance de minage illimitée : elle réécrit ET re-mine")
    print("    toute la chaîne avec le montant falsifié. Mais elle n'a pas la clé d'alice.")
    forged_tx = create_transaction(honest_tx.sender, honest_tx.recipient, forged_amount, honest_tx.data, honest_tx.sequence)
    report_chain("Chain valid after forged rewrite (no private key)", rebuild_and_remine(forged_tx))
    print("  Toute la puissance de calcul du monde ne remplace pas la clé privée d'alice :")
    print("  un mineur peut retarder ou omettre une transaction, jamais en inventer une.")

    print("\n[C] Mallory a volé la clé privée d'alice.")
    forged_tx = sign_transaction(forged_tx, wallets["alice"])
    report_chain("Chain valid with stolen key", rebuild_and_remine(forged_tx))
    print("  Limite intrinsèque : toute la sécurité repose sur le secret de la clé privée.")
    print("  C'est pourquoi le wallet (étape ultérieure) chiffrera les clés sur disque.")

    print("\n[D] Rejeu : la transaction signée d'alice est recopiée telle quelle dans un nouveau bloc.")
    replay_block = mined(create_block(chain.last_block, [honest_tx], timestamp=chain.last_block.timestamp + TARGET_BLOCK_TIME))
    report_chain("Chain valid with replayed transaction", [*chain.blocks, replay_block])
    print("  Accepté aujourd'hui : rien ne mémorise encore que la séquence 0 d'alice a été")
    print("  utilisée. La Partie 4 (soldes et état des comptes) exigera sequence = numéro")
    print("  attendu, et refusera aussi de dépenser plus que le solde disponible.")


def main() -> None:
    wallets, names = demo_keys()
    transactions = demo_signed_transactions(wallets, names)
    chain = Blockchain()
    demo_mining(chain, transactions, names)
    demo_attacks(chain, wallets)
    print()


if __name__ == "__main__":
    # Garantit l'affichage des accents même si la sortie est redirigée vers un fichier.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
