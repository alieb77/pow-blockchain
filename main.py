"""Démonstration des Parties 1 à 8 : hashes, PoW, signatures, soldes, mempool, réseau P2P, disque, wallet.

Lancer depuis le dossier du projet :

    python main.py

Le programme génère des clés, fait miner un premier bloc (création monétaire),
fait circuler les pièces via le mempool, simule des attaques (rejeu,
récompense gonflée, dépense au-delà du solde, double dépense), puis fait
vivre plusieurs nœuds : d'abord sur un réseau simulé et déterministe (forks,
règle du plus grand travail, borne d'horloge, attaque majoritaire), puis sur
de vraies sockets TCP locales, montre ce qu'un nœud écrit sur le disque et ce
qu'il refuse d'y relire, un wallet qui chiffre ses clés et met une somme de
contrôle sur les adresses, et enfin le minage vers une clé du wallet.
"""

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from powchain import (
    GENESIS_TIMESTAMP,
    HALVING_INTERVAL,
    MAX_FUTURE_DRIFT_SECONDS,
    TARGET_BLOCK_TIME,
    Block,
    Blockchain,
    FakeClock,
    InvalidChainError,
    InvalidTransactionError,
    KeyPair,
    Mempool,
    MempoolError,
    Node,
    NodeServer,
    NodeStorage,
    SimulatedNetwork,
    State,
    StorageError,
    Transaction,
    Wallet,
    WalletError,
    block_reward,
    block_to_dict,
    create_block,
    create_coinbase_transaction,
    create_signed_transaction,
    create_transaction,
    format_units,
    is_valid_chain,
    is_valid_transaction,
    message,
    mine_block,
    normalize_address,
    parse_coin_amount,
    validate_chain,
)
from powchain.protocol import NEW_BLOCK

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


# ----------------------------------------------------------------------------
# Partie 5 : réseau pair-à-pair
# ----------------------------------------------------------------------------


def print_network(net: SimulatedNetwork, nodes: dict[str, Node]) -> None:
    """Une ligne par nœud : hauteur, travail, pointe, mempool, pairs."""
    for name, node in nodes.items():
        peers = ",".join(sorted(p.node_id for p in node.peers))
        print(
            f"    {name}: hauteur {node.height}, travail {node.work:>6}, pointe {node.tip.hash[:10]}..., "
            f"mempool {len(node.mempool)}, pairs [{peers}]"
        )


def print_traffic(net: SimulatedNetwork, since: int) -> None:
    """Résume les messages livrés depuis l'indice `since` : « A->B new_block x2 »."""
    counts: dict[str, int] = {}
    for delivered in net.delivered[since:]:
        key = f"{delivered.sender}->{delivered.recipient} {delivered.type}"
        counts[key] = counts.get(key, 0) + 1
    print("    trafic : " + (", ".join(f"{k}" + (f" x{n}" if n > 1 else "") for k, n in counts.items()) or "aucun"))


def demo_simulated_network(wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("6. Réseau pair-à-pair simulé (déterministe : horloge et messages contrôlés)")
    miner, alice, bob = wallets["miner"], wallets["alice"], wallets["bob"]
    mallory = KeyPair.generate()
    names[mallory.address] = "mallory"
    clock = FakeClock(GENESIS_TIMESTAMP + TARGET_BLOCK_TIME)
    net = SimulatedNetwork(clock)
    a = net.add(Node(node_id="A", miner_address=miner.address, clock=clock))
    b = net.add(Node(node_id="B", clock=clock))
    c = net.add(Node(node_id="C", miner_address=mallory.address, clock=clock))
    nodes = {"A": a, "B": b, "C": c}

    print("\n[6a] Découverte. A mine pour « miner », C mine pour « mallory », B ne mine pas.")
    print("     B se connecte à A, puis C à B : B transmet l'adresse de A, C s'y connecte tout seul.")
    net.connect("B", "A")
    mark = len(net.delivered)
    net.connect("C", "B")
    print_traffic(net, mark)
    print_network(net, nodes)

    print("\n[6b] Gossip. A mine 2 blocs ; miner paie alice 10 COIN en soumettant la transaction à C ;")
    print("     C mine le bloc suivant. Chaque message n'est relayé qu'une fois : la rumeur s'éteint seule.")
    net.mine("A")
    clock.advance(TARGET_BLOCK_TIME)
    net.mine("A")
    mark = len(net.delivered)
    net.run("C", c.submit_transaction(create_signed_transaction(miner, alice.address, parse_coin_amount("10"), sequence=0)))
    print(f"    transaction soumise à C ; en attente sur A : {len(a.mempool)}, B : {len(b.mempool)}, C : {len(c.mempool)}")
    clock.advance(TARGET_BLOCK_TIME)
    block = net.mine("C")
    print(f"    C mine le bloc n°{block.index} avec {len(block.transactions) - 1} transaction ; solde d'alice vu par A : "
          f"{format_units(a.chain.state.balance_of(alice.address))}")
    print_traffic(net, mark)
    print_network(net, nodes)

    print("\n[6c] Fork. Le câble A-B et A-C est coupé ; A et C trouvent chacun un bloc n°4 au même instant.")
    print("     miner paie bob 7 COIN côté C seulement : confirmé dans le bloc de C.")
    clock.advance(TARGET_BLOCK_TIME)
    net.partition("A", "B")
    net.partition("A", "C")
    block_a = net.mine("A", coinbase_data="branche A")
    pay_bob = create_signed_transaction(miner, bob.address, parse_coin_amount("7"), sequence=1)
    net.run("C", c.submit_transaction(pay_bob))
    block_c = net.mine("C", coinbase_data="branche C")
    print(f"    A a le bloc {block_a.hash[:10]}..., B et C ont le bloc {block_c.hash[:10]}... (même travail : chacun garde le sien)")
    print(f"    solde de bob vu par C : {format_units(c.chain.state.balance_of(bob.address))}, vu par A : "
          f"{format_units(a.chain.state.balance_of(bob.address))}")
    print("     Le câble est réparé ; A mine le bloc n°5 : sa branche devient la plus lourde.")
    net.heal("A", "B")
    net.heal("A", "C")
    clock.advance(TARGET_BLOCK_TIME)
    mark = len(net.delivered)
    net.mine("A")
    print_traffic(net, mark)
    print_network(net, nodes)
    print(f"    réorganisations : B {b.stats['reorganizations']}, C {c.stats['reorganizations']} ; le paiement à bob est "
          f"revenu dans le mempool de C : {pay_bob in c.mempool}")
    print(f"    solde de bob vu par C : {format_units(c.chain.state.balance_of(bob.address))} (le bloc qui le payait est abandonné)")
    clock.advance(TARGET_BLOCK_TIME)
    net.mine("C")
    print(f"    C mine le bloc n°{c.height} : bob est payé sur la bonne branche, partout : "
          f"{format_units(a.chain.state.balance_of(bob.address))}")

    print("\n[6d] Plus longue n'est pas plus lourde. mallory, isolée, fabrique 3 blocs espacés de 11 s")
    print("     (la difficulté baisse de 12,5 % à chaque bloc) contre 2 blocs honnêtes rapides.")
    fork_point = a.tip
    for _ in range(2):
        clock.advance(1)
        net.mine("A")
    attacker = Blockchain.from_blocks(a.chain.blocks[: fork_point.index + 1])
    timestamp = fork_point.timestamp
    for _ in range(3):
        timestamp += TARGET_BLOCK_TIME + 1
        attacker.add_block(mine_block(create_block(attacker.last_block, [], mallory.address, timestamp=timestamp)).block)
    print(f"    honnête : hauteur {a.height}, travail {a.work} ; mallory : hauteur {attacker.height}, travail {attacker.total_work}")
    clock.advance(60)
    b.on_connect(99, "sim", False)
    b.on_message(99, Node(node_id="M", chain=attacker).hello())
    actions = b.on_message(99, message(NEW_BLOCK, block=block_to_dict(attacker.last_block)))
    while actions:
        (request,) = actions
        batch = attacker.blocks_from(request.message["from_index"], 200)
        actions = b.on_message(99, message("blocks", blocks=[block_to_dict(x) for x in batch], has_more=False))
    b.on_disconnect(99)
    print(f"    B a téléchargé la branche de mallory, l'a pesée et l'a ignorée : hauteur {b.height}, "
          f"branches plus légères refusées : {b.stats['branches_lighter']}")

    print(f"\n[6e] Borne d'horloge (règle N1). Un bloc daté de {MAX_FUTURE_DRIFT_SECONDS + 1} s dans le futur est refusé,")
    print("     sans déconnexion : le pair a peut-être une horloge fausse.")
    future = mine_block(create_block(a.tip, [], mallory.address, timestamp=clock.now + MAX_FUTURE_DRIFT_SECONDS + 1)).block
    b.on_connect(98, "sim", False)
    b.on_message(98, Node(node_id="F", clock=clock).hello())
    actions = b.on_message(98, message(NEW_BLOCK, block=block_to_dict(future)))
    print(f"    hauteur de B : {b.height}, blocs futurs refusés : {b.stats['blocks_future']}, actions : {actions} ; "
          f"pair toujours connecté : {b.peer(98) is not None}")
    clock.advance(MAX_FUTURE_DRIFT_SECONDS + 1)
    net.run("B", b.on_message(98, message(NEW_BLOCK, block=block_to_dict(future))))
    print(f"    {MAX_FUTURE_DRIFT_SECONDS + 1} s plus tard, le même bloc est accepté et relayé : hauteurs A {a.height}, B {b.height}, C {c.height}")
    b.on_disconnect(98)

    print("\n[6f] Bloc invalide (récompense gonflée, correctement miné) : le pair est déconnecté.")
    template = create_block(b.tip, [], mallory.address, timestamp=b.tip.timestamp + 1)
    greedy = replace(template.coinbase, amount=template.coinbase.amount + parse_coin_amount("1"))
    greedy = replace(greedy, hash=greedy.calculate_hash())
    greedy_block = mine_block(replace(template, transactions=(greedy,))).block
    b.on_connect(97, "sim", False)
    b.on_message(97, Node(node_id="G", clock=clock).hello())
    (action,) = b.on_message(97, message(NEW_BLOCK, block=block_to_dict(greedy_block)))
    print(f"    {type(action).__name__} : {action.reason}")

    print("\n[6g] Limite honnête : l'attaque majoritaire. mallory dispose de plus de puissance de calcul :")
    print("     pendant que le réseau produit 1 bloc, elle en produit 3 en privé, à partir d'AVANT le paiement à bob.")
    net.deliver()
    paid_in = next(block for block in c.chain.blocks if pay_bob in block.transactions)
    fork_point = c.chain.block_at(paid_in.index - 1)
    attacker = Blockchain.from_blocks(c.chain.blocks[: fork_point.index + 1])
    timestamp = fork_point.timestamp
    while attacker.total_work <= c.work:
        timestamp += 1
        attacker.add_block(mine_block(create_block(attacker.last_block, [], mallory.address, timestamp=timestamp)).block)
    print(f"    honnête : hauteur {c.height}, travail {c.work} ; mallory repart du bloc n°{fork_point.index} et mine "
          f"{attacker.height - fork_point.index} blocs rapides : hauteur {attacker.height}, travail {attacker.total_work}")
    clock.advance(60)
    net.add(Node(node_id="M", chain=attacker, clock=clock))
    net.connect("M", "B")
    print_network(net, nodes)
    print(f"    bob, qui avait 7 COIN confirmés, en a maintenant {format_units(a.chain.state.balance_of(bob.address))} partout.")
    print("    Toutes les règles ont été respectées : la chaîne de mallory est valide et plus lourde. La preuve de")
    print("    travail ne protège l'historique que tant que la majorité de la puissance de calcul est honnête ;")
    print("    plus un paiement a de blocs au-dessus de lui (confirmations), plus le réécrire coûte cher.")


async def demo_real_sockets(wallets: dict[str, KeyPair]) -> None:
    print_title("7. Le même code sur de vraies sockets TCP (127.0.0.1, ports choisis par le système)")
    miner, alice = wallets["miner"], wallets["alice"]
    servers = {
        name: NodeServer(Node(node_id=name, log=lambda t, n=name: print(f"    [{n}] {t}")))
        for name in ("A", "B", "C")
    }
    for server in servers.values():
        await server.start()
    a, b, c = servers["A"], servers["B"], servers["C"]
    print(f"    A écoute sur {a.address} ; B se connecte à A ; C se connecte à B et découvre A.")
    await b.connect(a.address)
    await c.connect(b.address)
    await c.wait_until(lambda: len(c.node.peers) == 2 and len(a.node.peers) == 2, timeout=20)
    print("    A se met à miner : chaque bloc trouvé traverse le réseau (le minage tourne par tranches dans asyncio).")
    a.start_mining(miner.address)
    await c.wait_until(lambda: c.node.height >= 3, timeout=20)
    tx = create_signed_transaction(miner, alice.address, parse_coin_amount("1"), sequence=0)
    print("    C soumet « miner -> alice 1 COIN » ; elle voyage jusqu'à A, qui la mine ; le bloc revient jusqu'à C.")
    await c._execute(c.node.submit_transaction(tx))
    ok = await c.wait_until(lambda: c.node.chain.state.balance_of(alice.address) == parse_coin_amount("1"), timeout=20)
    print(f"    alice payée, vu par C : {ok} ; hauteurs A {a.node.height}, B {b.node.height}, C {c.node.height} ; "
          f"blocs minés par A : {a.blocks_mined}")
    for server in servers.values():
        await server.stop()
    print("    Pour essayer entre terminaux : python -m powchain node --port 5000 --mine <adresse>  (voir README)")


# ----------------------------------------------------------------------------
# Partie 6 : persistance sur disque
# ----------------------------------------------------------------------------


def print_files(storage: NodeStorage) -> None:
    for path in (storage.blocks_path, storage.mempool_path, storage.peers_path):
        raw = path.read_bytes()
        print(f"    {path.name:<14} {len(raw):>6} octets, {raw.count(b'\n'):>3} ligne(s)")


def try_load(label: str, directory: Path, clock: FakeClock) -> Node | None:
    try:
        node = NodeStorage(directory).open_node(node_id=label, clock=clock)
    except StorageError as error:
        print(f"    chargement REFUSÉ : {error}")
        return None
    print(f"    chargement accepté : hauteur {node.height}, pointe {node.tip.hash[:10]}...")
    return node


def demo_persistence(wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("8. Persistance sur disque : un dossier par nœud, revalidé à chaque chargement")
    miner, alice, bob = wallets["miner"], wallets["alice"], wallets["bob"]
    mallory = KeyPair.generate()
    clock = FakeClock(GENESIS_TIMESTAMP + TARGET_BLOCK_TIME)

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "node-A"
        net = SimulatedNetwork(clock)
        storage = NodeStorage(directory)
        a = net.add(storage.open_node(node_id="A", miner_address=miner.address, clock=clock))
        b = net.add(Node(node_id="B", clock=clock))
        net.connect("A", "B")

        print("\n[8a] A écrit dans son dossier : il mine 3 blocs (le 2e paie alice), garde un paiement à bob en")
        print("     attente et connaît l'adresse de B. Chaque bloc est écrit (flush + fsync) AVANT d'être relayé.")
        clock.advance(TARGET_BLOCK_TIME)
        net.mine("A")
        net.run("A", a.submit_transaction(create_signed_transaction(miner, alice.address, parse_coin_amount("3"), sequence=0)))
        clock.advance(TARGET_BLOCK_TIME)
        net.mine("A")
        clock.advance(TARGET_BLOCK_TIME)
        net.mine("A")
        pending = create_signed_transaction(miner, bob.address, parse_coin_amount("1"), sequence=1)
        net.run("A", a.submit_transaction(pending))
        print_files(storage)
        first_line = storage.blocks_path.read_text(encoding="utf-8").splitlines()[1]
        print(f"    blocks.jsonl, ligne 2 : {first_line[:96]}...")
        good_copy = storage.blocks_path.read_bytes()

        print("\n[8b] Redémarrage : un nouveau nœud est reconstruit depuis le dossier, sans réseau.")
        restarted = NodeStorage(directory).open_node(node_id="A2", clock=clock)
        print(f"    hauteur {restarted.height}, même pointe que A : {restarted.tip == a.tip}, mempool : {len(restarted.mempool)} "
              f"(paiement à bob : {pending in restarted.mempool}), adresses connues : {restarted.known_addresses}")

        print("\n[8c] Coupure pendant l'écriture : les 40 derniers octets de blocks.jsonl manquent (ligne tronquée).")
        storage.blocks_path.write_bytes(good_copy[:-40])
        repaired = NodeStorage(directory)
        restarted = repaired.open_node(node_id="A3", clock=clock)
        print(f"    chargement : hauteur {restarted.height} (le bloc n°3 était incomplet), "
              f"{repaired.repaired_lines} fin de fichier réparée, fichier réécrit proprement")
        net2 = SimulatedNetwork(clock)
        net2.add(restarted)
        net2.add(Node(node_id="B", chain=b.chain, clock=clock))
        net2.connect("A3", "B")
        print(f"    reconnecté à B : hauteur {restarted.height}, le bloc perdu est revenu par le réseau ; "
              f"blocks.jsonl : {storage.blocks_path.read_bytes().count(b'\n')} lignes")

        print("\n[8d] Falsification du fichier : le paiement à alice (bloc n°2) passe de 3 à 30 COIN.")
        lines = good_copy.split(b"\n")
        block2 = a.chain.block_at(2)
        coinbase, payment = block2.transactions
        forged = replace(block2, transactions=(coinbase, replace(payment, amount=parse_coin_amount("30"))))
        lines[2] = json.dumps(block_to_dict(forged), separators=(",", ":")).encode("utf-8")
        storage.blocks_path.write_bytes(b"\n".join(lines))
        try_load("A4", directory, clock)
        print("     Falsification soignée : hash de la transaction recalculé, bloc re-miné... mais sans la clé du mineur.")
        unsigned = create_transaction(payment.sender, payment.recipient, parse_coin_amount("30"), payment.data, payment.sequence)
        forged = mine_block(replace(block2, transactions=(coinbase, unsigned))).block
        lines[2] = json.dumps(block_to_dict(forged), separators=(",", ":")).encode("utf-8")
        storage.blocks_path.write_bytes(b"\n".join(lines))
        try_load("A5", directory, clock)

        print("\n[8e] Limite honnête. (1) Fichier supprimé : rien à valider, le nœud repart du Genesis ;")
        print("     c'est le réseau qui lui rend sa chaîne. Le disque ne garantit pas la disponibilité.")
        storage.blocks_path.unlink()
        restarted = NodeStorage(directory).open_node(node_id="A6", clock=clock)
        print(f"    après suppression : hauteur {restarted.height}", end="")
        net3 = SimulatedNetwork(clock)
        net3.add(restarted)
        net3.add(Node(node_id="B", chain=b.chain, clock=clock))
        net3.connect("A6", "B")
        print(f" ; après reconnexion à B : hauteur {restarted.height}")
        print("     (2) Fichier remplacé par une AUTRE chaîne, valide mais étrangère (2 blocs minés par mallory) :")
        other = Blockchain()
        timestamp = GENESIS_TIMESTAMP
        for _ in range(2):
            timestamp += TARGET_BLOCK_TIME
            other.add_block(mine_block(create_block(other.last_block, [], mallory.address, timestamp=timestamp)).block)
        NodeStorage(directory).write_blocks(other.blocks)
        restarted = try_load("A7", directory, clock)
        net4 = SimulatedNetwork(clock)
        net4.add(restarted)
        net4.add(Node(node_id="B", chain=b.chain, clock=clock))
        net4.connect("A7", "B")
        print(f"    le disque l'a acceptée (elle respecte toutes les règles) ; après reconnexion à B, réorganisations : "
              f"{restarted.stats['reorganizations']}, hauteur {restarted.height}, pointe = celle de B : {restarted.tip == b.tip}")
        print("    Le disque prouve l'INTÉGRITÉ de ce qu'il contient, pas son AUTHENTICITÉ : seul le réseau,")
        print("    par la règle du plus grand travail, dit quelle chaîne valide est la bonne.")


# ----------------------------------------------------------------------------
# Partie 7 : wallet (clés chiffrées, somme de contrôle d'adresse)
# ----------------------------------------------------------------------------


def demo_wallet(wallets: dict[str, KeyPair]) -> None:
    print_title("9. Wallet : des clés chiffrées par mot de passe, une adresse à somme de contrôle")
    password = "corriger-cheval-pile-agrafe"  # une vraie phrase de passe, ici en clair pour la démo
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wallet.json"

        print("\n[9a] Création. Le wallet chiffre chaque graine (scrypt + AES-256-GCM) : rien en clair sur le disque.")
        wallet = Wallet.create(path)
        alice = wallet.generate_key(password, label="alice")
        wallet.import_seed_hex(wallets["bob"].seed_hex, password, label="bob")
        wallet.save()
        raw = path.read_bytes()
        print(f"    {path.name} : {len(raw)} octets, {len(wallet)} clés.")
        print(f"    la graine d'alice ({alice.seed_hex[:16]}...) apparaît-elle en clair dans le fichier ? "
              f"{alice.seed_hex.encode() in raw}")

        print("\n[9b] Déverrouillage. Le bon mot de passe rend la clé ; un mauvais échoue (tag AES-GCM), sans rien deviner.")
        recovered = wallet.key_pair("alice", password)
        print(f"    clé alice retrouvée à l'identique : {recovered.seed_hex == alice.seed_hex}")
        try:
            wallet.key_pair("alice", "pas le bon mot de passe")
            print("    mauvais mot de passe accepté ?!")
        except WalletError as error:
            print(f"    mauvais mot de passe : {error}")

        print("\n[9c] Une clé du wallet signe une vraie transaction, acceptée par les règles de la chaîne (R1-R7),")
        print("     sans que la graine quitte jamais le wallet en clair.")
        tx = create_signed_transaction(recovered, wallet.address_of("bob"), parse_coin_amount("1"), sequence=0)
        print(f"    is_valid_transaction : {is_valid_transaction(tx)}")

        print("\n[9d] Somme de contrôle d'adresse (façon EIP-55, adaptée à SHA-256). La CASSE encode un contrôle :")
        print("     l'adresse on-chain reste le hex brut, mais une faute de frappe est repérée avant tout envoi.")
        shared = wallet.checksummed_address_of("alice")
        print(f"    adresse à partager   : {shared}")
        print(f"    inscrite dans la tx  : {normalize_address(shared)}")
        typo = shared[:-1] + ("0" if shared[-1] != "0" else "1")
        try:
            normalize_address(typo)
            print("    faute de frappe acceptée ?!")
        except ValueError:
            print(f"    dernier caractère changé ({shared[-1]} -> {typo[-1]}) : REFUSÉE avant tout envoi")

        print("\n[9e] Limite honnête. (1) Intégrité : un octet du chiffré modifié => déverrouillage impossible,")
        print("     jamais une graine fausse rendue en silence (comme le disque du nœud en 8d).")
        data = wallet.to_dict()
        cipher = bytearray.fromhex(data["keys"][0]["ciphertext"])
        cipher[0] ^= 1
        data["keys"][0]["ciphertext"] = cipher.hex()
        try:
            Wallet.from_dict(data).key_pair("alice", password)
            print("    fichier modifié accepté ?!")
        except WalletError as error:
            print(f"    fichier modifié : {error}")
        print("     (2) Ce que le wallet NE protège PAS : un mot de passe faible reste cassable hors ligne (scrypt")
        print("     ralentit chaque essai, ne l'empêche pas) ; un mot de passe PERDU = fonds perdus, aucune")
        print("     récupération (sauvegardez la graine via « wallet export ») ; et la somme de contrôle attrape")
        print("     les fautes de frappe, pas l'envoi volontaire à une adresse valide qui n'est à personne.")


# ----------------------------------------------------------------------------
# Partie 8 : miner vers son wallet
# ----------------------------------------------------------------------------


def demo_mine_to_wallet() -> None:
    print_title("10. Miner vers son wallet : le nœud lit l'adresse publique, sans mot de passe")
    password = "corriger-cheval-pile-agrafe"
    with tempfile.TemporaryDirectory() as tmp:
        wallet = Wallet.create(Path(tmp) / "wallet.json")
        wallet.generate_key(password, label="mineur")
        wallet.save()

        print("\n[10a] La CLI résout « node --mine-label mineur » en une adresse. Miner n'utilise que la clé")
        print("     PUBLIQUE : lire l'adresse ne demande aucun mot de passe et ne touche jamais la graine chiffrée.")
        address = wallet.address_of("mineur")  # aucun mot de passe requis
        print(f"    adresse de minage : {wallet.checksummed_address_of('mineur')}")

        print("\n[10b] Le nœud mine : chaque coinbase paie cette adresse ; les coins s'empilent dans le wallet.")
        chain = Blockchain()
        for _ in range(2):
            chain.add_block(mine_block(create_block(chain.last_block, [], address)).block)
        print(f"    2 blocs minés ; solde de « mineur » : {format_units(chain.state.balance_of(address))}")

        print("\n[10c] Limite honnête. Miner vers une adresse n'expose aucun secret : le nœud n'a lu que la partie")
        print("     publique du wallet. Mais DÉPENSER ces coins demande toujours le mot de passe (pour signer) —")
        print("     la clé privée reste chiffrée. Un nœud public qui mine pour toi ne peut pas toucher à ton solde.")


def main() -> None:
    wallets, names = demo_keys()
    chain, pool = demo_genesis_and_first_reward(wallets, names)
    demo_mempool(chain, pool, wallets, names)
    demo_attacks(chain, wallets, names)
    demo_emission()
    demo_simulated_network(wallets, names)
    asyncio.run(demo_real_sockets(wallets))
    demo_persistence(wallets, names)
    demo_wallet(wallets)
    demo_mine_to_wallet()
    print()


if __name__ == "__main__":
    # Garantit l'affichage des accents même si la sortie est redirigée vers un fichier.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
