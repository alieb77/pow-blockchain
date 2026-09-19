"""Démonstration des Parties 1 à 12 : hashes, PoW, signatures, soldes, mempool, réseau P2P, disque, wallet, réseau ouvert, résilience, frais, API HTTP.

Lancer depuis le dossier du projet :

    python main.py

Le programme génère des clés, fait miner un premier bloc (création monétaire),
fait circuler les pièces via le mempool, simule des attaques (rejeu,
récompense gonflée, dépense au-delà du solde, double dépense), puis fait
vivre plusieurs nœuds : d'abord sur un réseau simulé et déterministe (forks,
règle du plus grand travail, borne d'horloge, attaque majoritaire), puis sur
de vraies sockets TCP locales, montre ce qu'un nœud écrit sur le disque et ce
qu'il refuse d'y relire, un wallet qui chiffre ses clés et met une somme de
contrôle sur les adresses, le minage vers une clé du wallet, ce qui change
quand un nœud s'ouvre au réseau (portée des adresses, adresse propre, plafond
d'entrées, délai de hello), la résilience (rappel des pairs perdus, oubli des
adresses mortes, bannissement des pairs fautifs), et enfin les frais de
transaction (politique de relais, priorité aux meilleurs payeurs, éviction,
frais reversés au mineur par la coinbase), et l'API HTTP du nœud (lecture de
la chaîne et des comptes en JSON, soumission de transactions signées).
"""

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from powchain import (
    BAN_SECONDS,
    GENESIS_TIMESTAMP,
    HALVING_INTERVAL,
    MAX_DIAL_FAILURES,
    MAX_FUTURE_DRIFT_SECONDS,
    MAX_PEERS,
    MIN_RELAY_FEE,
    RECONNECT_MAX_DELAY,
    TARGET_BLOCK_TIME,
    ApiServer,
    Block,
    Blockchain,
    Connect,
    FakeClock,
    InvalidChainError,
    InvalidTransactionError,
    KeyPair,
    Mempool,
    MempoolError,
    Message,
    Node,
    NodeServer,
    NodeStorage,
    Send,
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
    host_scope,
    is_valid_chain,
    is_valid_transaction,
    message,
    mine_block,
    normalize_address,
    parse_coin_amount,
    transaction_to_dict,
    validate_chain,
)
from powchain.protocol import NEW_BLOCK, PEERS

RULE = "=" * 76
FEE = MIN_RELAY_FEE  # frais minimal relayé : ce que paient les transactions de la démo


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
        fee = "" if tx.is_coinbase else f"  frais {tx.fee} u"
        print(
            f"    [{position}] {kind} {name_of(tx.sender, names):<9} -> {name_of(tx.recipient, names):<9} "
            f"{format_units(tx.amount):>18}  seq {tx.sequence}{fee}"
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
    submit(pool, chain.state, "alice -> bob 1 FLS (alice n'a rien)", create_signed_transaction(wallets["alice"], wallets["bob"].address, parse_coin_amount("1"), fee=FEE))

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
    print(f"  (chaque paiement porte le frais minimal relayé, {format_units(FEE)} ; voir la section 13)")

    def pay(sender: KeyPair, recipient: str, amount: int, sequence: int = 0) -> Transaction:
        return create_signed_transaction(sender, recipient, amount, sequence=sequence, fee=FEE)

    submit(pool, chain.state, "miner -> alice 10 FLS, seq 0", pay(miner, alice.address, parse_coin_amount("10"), sequence=0))
    submit(pool, chain.state, "miner -> bob 5 FLS, seq 1", pay(miner, bob.address, parse_coin_amount("5"), sequence=1))
    submit(pool, chain.state, "miner -> bob 5 FLS, seq 1 (doublon)", pay(miner, bob.address, parse_coin_amount("5"), sequence=1))
    submit(pool, chain.state, "miner -> carol 1 FLS, seq 3 (saut de séquence)", pay(miner, carol.address, parse_coin_amount("1"), sequence=3))
    submit(pool, chain.state, "alice -> carol 4 FLS, seq 0 (financée par l'attente)", pay(alice, carol.address, parse_coin_amount("4"), sequence=0))
    submit(pool, chain.state, "alice -> carol 4 FLS, seq 1", pay(alice, carol.address, parse_coin_amount("4"), sequence=1))
    submit(pool, chain.state, "alice -> carol 4 FLS, seq 2 (solde projeté 2)", pay(alice, carol.address, parse_coin_amount("4"), sequence=2))
    submit(pool, chain.state, "coinbase soumise par un utilisateur", create_coinbase_transaction(alice.address, 2))
    submit(pool, chain.state, "transaction non signée", create_transaction(miner.address, alice.address, 1, sequence=2))
    print(f"\n  {len(pool)} transactions en attente. Le mineur les inclut dans le bloc n°2 :")
    mine_next(chain, pool, miner, names)
    print()
    print_state(chain.state, names)
    print(f"  Mempool après le bloc : {len(pool)} transaction(s) en attente.")

    print("\n  Rejeu : alice re-soumet sa transaction « seq 0 » déjà confirmée.")
    submit(pool, chain.state, "alice -> carol 4 FLS, seq 0 (rejeu)", pay(alice, carol.address, parse_coin_amount("4"), sequence=0))
    print("  En Partie 3 le rejeu passait ; l'état des comptes le bloque désormais (règle S1).")


def demo_attacks(chain: Blockchain, wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("4. Attaques contre la chaîne (blocs assemblés et minés par l'attaquant)")
    miner, alice, bob, carol = (wallets[n] for n in ("miner", "alice", "bob", "carol"))
    honest = list(chain.blocks)
    report_chain("Chain valid", honest)

    print("\n[A] Rejeu : la transaction confirmée « alice -> carol seq 0 » recopiée dans un nouveau bloc.")
    replayed = next(tx for tx in honest[2].transactions if tx.sender == alice.address)
    report_chain("Chain valid with replayed transaction", honest + [hand_built(honest[-1], [replayed], miner)])

    print("\n[B] Le mineur s'attribue 51 FLS au lieu de 50.")
    template = create_block(honest[-1], [], miner.address, timestamp=honest[-1].timestamp + TARGET_BLOCK_TIME)
    greedy = replace(template.coinbase, amount=template.coinbase.amount + parse_coin_amount("1"))
    greedy = replace(greedy, hash=greedy.calculate_hash())
    greedy_block = mine_block(replace(template, transactions=(greedy,))).block
    report_chain("Chain valid with inflated reward", honest + [greedy_block])

    print("\n[C] bob dépense 100 FLS alors qu'il en a 5 (transaction correctement signée).")
    overspend = create_signed_transaction(bob, carol.address, parse_coin_amount("100"), sequence=0)
    report_chain("Chain valid with overspending", honest + [hand_built(honest[-1], [overspend], miner)])

    print("\n[D] Double dépense : bob envoie ses 5 FLS à carol ET à alice dans le même bloc.")
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
    print(f"\n  Divisée par deux tous les {HALVING_INTERVAL} blocs : le total tend vers 21 000 000 FLS")
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

    print("\n[6b] Gossip. A mine 2 blocs ; miner paie alice 10 FLS en soumettant la transaction à C ;")
    print("     C mine le bloc suivant. Chaque message n'est relayé qu'une fois : la rumeur s'éteint seule.")
    net.mine("A")
    clock.advance(TARGET_BLOCK_TIME)
    net.mine("A")
    mark = len(net.delivered)
    net.run("C", c.submit_transaction(create_signed_transaction(miner, alice.address, parse_coin_amount("10"), sequence=0, fee=FEE)))
    print(f"    transaction soumise à C ; en attente sur A : {len(a.mempool)}, B : {len(b.mempool)}, C : {len(c.mempool)}")
    clock.advance(TARGET_BLOCK_TIME)
    block = net.mine("C")
    print(f"    C mine le bloc n°{block.index} avec {len(block.transactions) - 1} transaction ; solde d'alice vu par A : "
          f"{format_units(a.chain.state.balance_of(alice.address))}")
    print_traffic(net, mark)
    print_network(net, nodes)

    print("\n[6c] Fork. Le câble A-B et A-C est coupé ; A et C trouvent chacun un bloc n°4 au même instant.")
    print("     miner paie bob 7 FLS côté C seulement : confirmé dans le bloc de C.")
    clock.advance(TARGET_BLOCK_TIME)
    net.partition("A", "B")
    net.partition("A", "C")
    block_a = net.mine("A", coinbase_data="branche A")
    pay_bob = create_signed_transaction(miner, bob.address, parse_coin_amount("7"), sequence=1, fee=FEE)
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
    b.on_connect(97, "10.66.6.6", False)
    b.on_message(97, Node(node_id="G", clock=clock).hello())
    (action,) = b.on_message(97, message(NEW_BLOCK, block=block_to_dict(greedy_block)))
    b.on_disconnect(97)
    print(f"    {type(action).__name__} : {action.reason} ; son hôte 10.66.6.6 est banni (Partie 10) : {b.is_banned('10.66.6.6')}")

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
    print(f"    bob, qui avait 7 FLS confirmés, en a maintenant {format_units(a.chain.state.balance_of(bob.address))} partout.")
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
    tx = create_signed_transaction(miner, alice.address, parse_coin_amount("1"), sequence=0, fee=FEE)
    print("    C soumet « miner -> alice 1 FLS » ; elle voyage jusqu'à A, qui la mine ; le bloc revient jusqu'à C.")
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
        net.run("A", a.submit_transaction(create_signed_transaction(miner, alice.address, parse_coin_amount("3"), sequence=0, fee=FEE)))
        clock.advance(TARGET_BLOCK_TIME)
        net.mine("A")
        clock.advance(TARGET_BLOCK_TIME)
        net.mine("A")
        pending = create_signed_transaction(miner, bob.address, parse_coin_amount("1"), sequence=1, fee=FEE)
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

        print("\n[8d] Falsification du fichier : le paiement à alice (bloc n°2) passe de 3 à 30 FLS.")
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
        tx = create_signed_transaction(recovered, wallet.address_of("bob"), parse_coin_amount("1"), sequence=0, fee=FEE)
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


# ----------------------------------------------------------------------------
# Partie 9 : ouverture au réseau
# ----------------------------------------------------------------------------


def shared_addresses(actions) -> list[str]:
    """Adresses du message « peers » contenu dans une liste d'actions ([] s'il n'y en a pas)."""
    for action in actions:
        if isinstance(action, Send) and action.message.type == PEERS:
            return action.message["addresses"]
    return []


def demo_open_network() -> None:
    print_title("11. Ouvrir au réseau : portée des adresses, adresse propre, plafond d'entrées, délai de hello")
    node = Node(node_id="ouvert", listen_port=5000, max_inbound=4, log=lambda t: print(f"    [ouvert] {t}"))

    print("\n[11a] Chaque hôte a une PORTÉE : jusqu'où son adresse a un sens.")
    for host in ("127.0.0.1", "192.168.1.9", "8.8.8.8", "mon-serveur.example"):
        print(f"    {host:<22} -> {host_scope(host)}")

    print("\n[11b] Le nœud connaît trois adresses, une par portée. À chaque pair il n'annonce que celles qui ont un")
    print("     sens depuis là où il est : 127.0.0.1 ne sort pas de la machine, 192.168.x pas du réseau local.")
    node.remember_addresses(["127.0.0.1:5001", "192.168.1.20:5000", "8.8.8.8:5000"])
    visitors = {1: ("même machine", "127.0.0.1"), 2: ("réseau local", "192.168.1.30"), 3: ("Internet", "1.1.1.1")}
    for peer_id, (where, host) in visitors.items():
        node.on_connect(peer_id, host, outbound=False)
        actions = node.on_message(peer_id, Node(node_id=f"visiteur-{peer_id}", listen_port=None).hello())
        print(f"    au pair « {where} » ({host:<13}) : {shared_addresses(actions)}")

    print("\n[11c] En réception, même règle : une adresse plus locale que le pair qui l'envoie désigne SA machine ou")
    print("     SON réseau. Le pair d'Internet annonce 127.0.0.1:6000, 10.0.0.7:5000 et 9.9.9.9:5000 :")
    actions = node.on_message(3, message(PEERS, addresses=["127.0.0.1:6000", "10.0.0.7:5000", "9.9.9.9:5000"]))
    print(f"    connexions ouvertes : {[a.address for a in actions if isinstance(a, Connect)]} ; "
          f"adresses ignorées : {node.stats['addresses_out_of_reach']}")

    print("\n[11d] Le pair du réseau local nous annonce 192.168.1.9:5000. On appelle... et on tombe sur nous-même")
    print("     (le hello porte notre node_id) : cette adresse est la nôtre, on l'apprend et on cesse de la rappeler.")
    (connect,) = node.on_message(2, message(PEERS, addresses=["192.168.1.9:5000"]))
    node.on_connect(9, "192.168.1.9", outbound=True, address=connect.address)
    (closed,) = node.on_message(9, node.hello())
    node.on_disconnect(9)
    again = node.on_message(2, message(PEERS, addresses=["192.168.1.9:5000"]))
    print(f"    résultat : « {closed.reason} » ; adresses propres : {list(node.own_addresses)} ; "
          f"encore dans le carnet : {'192.168.1.9:5000' in node.known_addresses} ; ré-annoncée -> {len(again)} appel")
    node.on_connect(10, "192.168.1.31", outbound=False)
    actions = node.on_message(10, Node(node_id="visiteur-10", listen_port=None).hello())
    print(f"    et on l'annonce désormais aux pairs du réseau local : {shared_addresses(actions)}")

    print("\n[11e] Plafond d'entrées (max_inbound = 4 ici, 32 par défaut) : une connexion entrante de trop est fermée")
    print("     avant même le hello. Les sorties (max_peers = 8) se comptent à part : remplir nos entrées ne nous")
    print("     empêche pas de choisir nos pairs.")
    (refused,) = node.on_connect(11, "1.1.1.2", outbound=False)
    print(f"    -> « {refused.reason} » ; entrées occupées : {node.inbound_connections}/4, "
          f"sorties encore possibles : {MAX_PEERS - node.outbound_connections}/{MAX_PEERS}")

    asyncio.run(demo_hello_timeout())

    print("\n[11g] Limite honnête. Un port ouvert protège désormais la mémoire du nœud (plafond, délai) et la qualité")
    print("     du carnet (portées), mais : derrière une box, personne n'entre sans redirection de port, le nœud")
    print("     reste un simple client sortant ; un pair perdu n'est pas rappelé et un pair fautif peut revenir")
    print("     (résilience : prochaine partie). Être joignable n'expose pas les fonds : tout le monde peut lire la")
    print("     chaîne, personne ne peut la falsifier sans le travail majoritaire, ni dépenser sans la clé privée.")


async def demo_hello_timeout() -> None:
    print("\n[11f] Sur une vraie socket ouverte à tous (0.0.0.0), une connexion muette est fermée après hello_timeout")
    print("     (10 s par défaut, 0,5 s ici) : elle ne garde pas une entrée occupée pour rien.")
    server = NodeServer(Node(node_id="ouvert"), host="0.0.0.0", hello_timeout=0.5, log=lambda t: print(f"    [ouvert] {t}"))
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    first = await asyncio.wait_for(reader.readline(), timeout=5)
    print(f"    le nœud se présente aussitôt ({first.decode().strip()[:52]}...) ; nous, on ne dit rien...")
    end = await asyncio.wait_for(reader.readline(), timeout=5)
    print(f"    ... et la connexion est fermée par le nœud : {end == b''} ; connexions restantes : {server.node.connections}")
    writer.close()
    await server.stop()
    print("    Pour de vrai : python -m powchain node --port 5000 --public   puis, sur un autre poste,")
    print("                   python -m powchain node --port 5000 --peers <adresse affichée>:5000")


# ----------------------------------------------------------------------------
# Partie 10 : résilience
# ----------------------------------------------------------------------------


def demo_resilience() -> None:
    print_title("12. Résilience : rappel des pairs perdus, oubli des adresses mortes, bannissement des fautifs")
    clock = FakeClock(GENESIS_TIMESTAMP + TARGET_BLOCK_TIME)
    net = SimulatedNetwork(clock)
    a = net.add(Node(node_id="A", clock=clock, log=lambda t: print(f"    [A] {t}")))
    net.add(Node(node_id="B", clock=clock))
    net.connect("A", "B")
    b_address = net.address_of("B")

    print("\n[12a] A est connecté à B. B s'éteint. À chaque tick (une fois par seconde chez le vrai nœud), A vérifie")
    print("     ses sorties et rappelle B avec un délai qui double à chaque échec (1, 2, 4... s, plafond 5 min).")
    net.disconnect("A", "B")
    net.partition("A", "B")
    print(f"    connexion perdue ; prochain rappel dans {a.retry_in(b_address)} s")
    for _ in range(3):
        clock.advance(a.retry_in(b_address))
        net.tick("A")
    print(f"    trois rappels ratés ; A patiente maintenant {a.retry_in(b_address)} s ; pairs de A : {len(a.peers)}")

    print("\n[12b] B revient. Au rappel suivant, la poignée de main réussit et le compteur d'échecs repart de zéro.")
    net.heal("A", "B")
    clock.advance(a.retry_in(b_address))
    net.tick("A")
    print(f"    A et B reconnectés : {net.connected('A', 'B')} ; délai avant rappel : {a.retry_in(b_address)} s")

    print(f"\n[12c] Une adresse morte (personne n'y répond) est oubliée après {MAX_DIAL_FAILURES} échecs d'affilée, soit")
    print("     environ 25 minutes d'essais ; une AMORCE (--peers) ne l'est jamais : c'est le point d'entrée de confiance.")
    d = net.add(Node(node_id="D", clock=clock))
    d.remember_addresses(["sim-Z:10099"])
    d.remember_addresses(["sim-S:10098"], seed=True)
    for _ in range(MAX_DIAL_FAILURES):
        net.tick("D")
        clock.advance(RECONNECT_MAX_DELAY)
    print(f"    après {d.stats['dial_failures']} échecs : adresse morte encore connue ? {'sim-Z:10099' in d.known_addresses} ; "
          f"amorce encore connue ? {'sim-S:10098' in d.known_addresses}")

    print(f"\n[12d] Pair fautif. Un pair envoie n'importe quoi : déconnecté ET son hôte banni {BAN_SECONDS // 60} min.")
    print("     Ses connexions sont refusées avant même le hello ; le ban se lève seul. La boucle locale (127.0.0.1)")
    print("     n'est jamais bannie : ce sont nos propres processus (tests, démos, wallet).")
    guard = Node(node_id="G", clock=clock, log=lambda t: print(f"    [G] {t}"))
    guard.on_connect(1, "203.0.113.9", False)
    guard.on_message(1, Node(node_id="vandale").hello())
    (action,) = guard.on_message(1, Message("dance", {}))
    guard.on_disconnect(1)
    print(f"    -> {action.reason} ; hôtes bannis : {list(guard.banned_hosts)}")
    (refused,) = guard.on_connect(2, "203.0.113.9", False)
    print(f"    il revient : « {refused.reason} »")
    clock.advance(BAN_SECONDS)
    guard.tick()
    (welcome,) = guard.on_connect(3, "203.0.113.9", False)
    print(f"    {BAN_SECONDS // 60} min plus tard : on lui envoie {welcome.message.type} (accepté de nouveau)")
    guard.on_connect(4, "127.0.0.1", False)
    (local,) = guard.on_message(4, Message("dance", {}))
    print(f"    même faute depuis 127.0.0.1 : déconnecté ({local.reason[:24]}...), banni ? {guard.is_banned('127.0.0.1')}")

    print("\n[12e] Limite honnête. (1) Le ban est par hôte : plusieurs nœuds derrière une même box partagent une IP")
    print("     et sont bannis ensemble ; à l'inverse, un attaquant change d'IP à volonté, le ban protège des bugs et")
    print("     des maladroits, pas d'un adversaire déterminé. (2) Rappeler ses pairs ne protège pas d'un réseau qui")
    print("     ment d'une seule voix (éclipse) : si toutes nos sorties tombent chez des complices, on ne voit que leur")
    print("     chaîne. La parade reste des amorces --peers de confiance et, à terme, plusieurs sources indépendantes.")


# ----------------------------------------------------------------------------
# Partie 11 : frais de transaction
# ----------------------------------------------------------------------------


def demo_fees() -> None:
    print_title("13. Frais : chaque paiement rémunère le mineur ; le mempool sert les meilleurs payeurs d'abord")
    miner, alice, bob, carol = (KeyPair.generate() for _ in range(4))
    names = {miner.address: "miner", alice.address: "alice", bob.address: "bob", carol.address: "carol", "0" * 64: "COINBASE"}
    chain, pool = Blockchain(), Mempool()
    print("  Bloc n°1 : la récompense au mineur.")
    mine_next(chain, pool, miner, names)
    for recipient, sequence in ((alice, 0), (bob, 1)):
        pool.add(create_signed_transaction(miner, recipient.address, parse_coin_amount("10"), sequence=sequence, fee=FEE), chain.state)
    print(f"\n  Bloc n°2 : le mineur finance alice et bob (10 FLS chacun, frais {format_units(FEE)} chacun).")
    block = mine_next(chain, pool, miner, names)
    print(f"  coinbase = récompense {format_units(block_reward(2))} + frais {format_units(block.total_fees)} = {format_units(block.coinbase.amount)}")
    print_state(chain.state, names)

    print(f"\n[13a] Politique de relais : un nœud n'attend ni ne relaie une transaction payant moins de {format_units(MIN_RELAY_FEE)}.")
    submit(pool, chain.state, "alice -> carol 1 FLS, frais 0", create_signed_transaction(alice, carol.address, parse_coin_amount("1"), fee=0))
    submit(pool, chain.state, f"alice -> carol 1 FLS, frais {MIN_RELAY_FEE - 1} unités", create_signed_transaction(alice, carol.address, parse_coin_amount("1"), fee=MIN_RELAY_FEE - 1))
    print("     Mais c'est une POLITIQUE, pas une règle de la chaîne : un mineur peut inclure lui-même une")
    print("     transaction gratuite dans SON bloc (il en paie le coût en preuve de travail).")
    free = create_signed_transaction(alice, carol.address, parse_coin_amount("1"), fee=0)
    report_chain("Chain valid with a fee-less transaction mined directly", list(chain.blocks) + [hand_built(chain.last_block, [free], miner)])

    print("\n[13b] Priorité aux frais. Trois paiements arrivent dans l'ordre : alice (frais x1), miner (x2), bob (x5).")
    cheap = create_signed_transaction(alice, carol.address, parse_coin_amount("1"), fee=FEE)
    mid = create_signed_transaction(miner, carol.address, parse_coin_amount("1"), sequence=2, fee=2 * FEE)
    dear = create_signed_transaction(bob, carol.address, parse_coin_amount("1"), fee=5 * FEE)
    for tx in (cheap, mid, dear):
        pool.add(tx, chain.state)
    order = [name_of(tx.sender, names) for tx in pool.select(chain.state)]
    print(f"     ordre de service pour le prochain bloc : {' > '.join(order)} (bob paie le plus : il passe en premier)")
    print("     Un même expéditeur reste servi dans l'ordre de ses séquences, quel que soit le frais de chacune.")

    print("\n[13c] Mempool plein (capacité 2 pour la démo) : entrer coûte plus que le moins payant, qui est évincé.")
    small = Mempool(max_size=2)
    small.add(cheap, chain.state)
    small.add(mid, chain.state)
    submit(small, chain.state, "miner -> carol, seq 3, frais x1 (pas mieux que le moins payant)", create_signed_transaction(miner, carol.address, parse_coin_amount("1"), sequence=3, fee=FEE))
    evicted = small.add(dear, chain.state)
    print(f"     bob (frais x5) entre ; évincée : {', '.join(name_of(tx.sender, names) for tx in evicted)} (frais x1) ; "
          f"en attente : {', '.join(name_of(tx.sender, names) for tx in small.transactions)}")

    print("\n[13d] Le bloc suivant reverse tous les frais au mineur ; la masse monétaire ne bouge que de la récompense.")
    before = chain.state.total_supply
    block = mine_next(chain, pool, miner, names)
    print(f"     coinbase = {format_units(block_reward(block.index))} + frais {format_units(block.total_fees)} = {format_units(block.coinbase.amount)}")
    print(f"     masse monétaire : {format_units(before)} -> {format_units(chain.state.total_supply)} "
          f"(+ {format_units(chain.state.total_supply - before)} : la récompense seule, les frais ne créent rien)")
    print_state(chain.state, names)
    print("\n  Limites honnêtes : le seuil est fixe (pas d'estimation de frais selon la charge) ; un mineur peut")
    print("  toujours remplir SES blocs de transactions gratuites (il en paie la preuve de travail) ; et les frais")
    print("  ne remplacent pas une limite de débit par pair : c'est désormais le seau à jetons de chaque")
    print("  connexion (node.py) qui coupe un flot de messages, même valides, avant l'ouverture publique.")


# ----------------------------------------------------------------------------
# Partie 12 : API HTTP
# ----------------------------------------------------------------------------


async def http_call(host: str, port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Client HTTP minimal (bibliothèque standard seulement) : statut et corps JSON."""
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\n\r\n".encode("latin-1") + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, content = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(content)


async def demo_api(wallets: dict[str, KeyPair], names: dict[str, str]) -> None:
    print_title("14. API HTTP : le nœud répond aussi en JSON, pour l'explorateur, le wallet web et les scripts")
    miner, alice, bob = wallets["miner"], wallets["alice"], wallets["bob"]
    server = NodeServer(Node(node_id="api-demo"), mining_chunk=512)
    await server.start()
    api = ApiServer(server, log=lambda text: print(f"    [nœud] {text}"))
    await api.start()
    try:
        node = server.node
        node.miner_address = miner.address
        for _ in range(2):
            node.submit_block(mine_block(node.build_candidate()).block)

        print(f"\n[14a] GET {api.url}status : l'état du nœud en une requête.")
        status, data = await http_call(api.host, api.port, "GET", "/status")
        print(f"    {status} -> hauteur {data['height']}, difficulté {data['difficulty']}, {data['peers']} pair(s), "
              f"mempool {data['mempool']}, masse monétaire {format_units(data['total_supply'])}")

        print("\n[14b] GET /blocks?limit=2 : les blocs, du plus récent au plus ancien.")
        status, data = await http_call(api.host, api.port, "GET", "/blocks?limit=2")
        for block in data["blocks"]:
            print(f"    bloc n°{block['index']} {block['hash'][:16]}... {block['transactions']} transaction(s), "
                  f"frais {block['fees']} u, mineur {name_of(block['miner'], names)}")

        print("\n[14c] POST /transactions : un paiement signé par le wallet. L'API ne signe jamais rien.")
        tx = create_signed_transaction(miner, alice.address, parse_coin_amount("2"), sequence=0, fee=FEE)
        status, data = await http_call(api.host, api.port, "POST", "/transactions", transaction_to_dict(tx))
        print(f"    miner -> alice 2 FLS : {status}, accepté = {data.get('accepted')}, "
              f"solde projeté du mineur {format_units(data['projected_balance'])}")
        broke = create_signed_transaction(bob, alice.address, parse_coin_amount("1"), fee=FEE)
        status, data = await http_call(api.host, api.port, "POST", "/transactions", transaction_to_dict(broke))
        print(f"    bob -> alice 1 FLS (bob n'a rien) : {status}, refus motivé : {data['error']}")

        print("\n[14d] GET /accounts/<adresse> puis /transactions/<hash> : avant et après le bloc suivant.")
        status, data = await http_call(api.host, api.port, "GET", f"/accounts/{alice.address}")
        print(f"    alice : solde confirmé {format_units(data['balance'])}, projeté {format_units(data['projected_balance'])}, "
              f"{data['pending']} transaction en attente")
        node.submit_block(mine_block(node.build_candidate()).block)
        status, data = await http_call(api.host, api.port, "GET", f"/transactions/{tx.hash}")
        print(f"    après un bloc : transaction {data['status']}, bloc n°{data['block_index']}, "
              f"{data['confirmations']} confirmation")
        status, data = await http_call(api.host, api.port, "GET", f"/accounts/{alice.address}/transactions")
        print(f"    historique d'alice : {data['total']} transaction(s) confirmée(s), {len(data['pending'])} en attente")

        print("\n  L'API ne détient aucune clé : elle lit la chaîne et relaie des transactions déjà signées ;")
        print("  l'exposer (--public) n'expose aucun fonds. Pas de HTTPS ni de limite de débit par client HTTP")
        print("  (la limite de débit vise les pairs P2P) : pour Internet, un proxy devant, ou --no-api.")
        print("  Un navigateur ouvrant http://<hôte>:<port>/ y trouve l'explorateur de blocs (façon Tetris).")
    finally:
        await api.stop()
        await server.stop()


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
    demo_open_network()
    demo_resilience()
    demo_fees()
    asyncio.run(demo_api(wallets, names))
    print()


if __name__ == "__main__":
    # Garantit l'affichage des accents même si la sortie est redirigée vers un fichier.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
