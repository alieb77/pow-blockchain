"""Ligne de commande : lancer un nœud, générer une clé, interroger ou payer.

    python -m powchain node --port 5000 [--peers 127.0.0.1:5001,...] [--mine ADRESSE]
                            [--data-dir DOSSIER | --memory]
    python -m powchain keygen
    python -m powchain status --node 127.0.0.1:5000 [--address ADRESSE]
    python -m powchain send --node 127.0.0.1:5000 --seed-hex GRAINE --to ADRESSE --amount 1.5

Un nœud persiste par défaut dans data/node-<port>/ (blocs, mempool, carnet
d'adresses, voir storage.py) : relancé, il reprend sa chaîne et se reconnecte
seul aux adresses connues. --memory désactive toute écriture.

« status » et « send » sont des CLIENTS ÉPHÉMÈRES : ils ouvrent une connexion
vers un nœud, se présentent (hello sans port d'écoute), envoient leur
demande, lisent la réponse et se déconnectent. Un nœud ne fait aucune
différence entre un client et un pair : mêmes messages, mêmes règles.

La graine privée passée en argument à « send » est un pis-aller de démo : la
gestion des clés (chiffrement, suivi des séquences) sera le rôle du wallet.
"""

import argparse
import asyncio
import signal
import sys
import time

from .address import is_valid_address
from .errors import PowChainError
from .keys import KeyPair
from .money import format_units, parse_coin_amount
from .network import NodeServer
from .node import MAX_PEERS, Node
from .protocol import (
    ACCOUNT,
    GET_ACCOUNT,
    HELLO,
    MAX_MESSAGE_BYTES,
    NEW_TRANSACTION,
    PROTOCOL_VERSION,
    REJECT,
    decode_message,
    encode_message,
    message,
    parse_address,
)
from .block import create_genesis_block
from .codec import transaction_to_dict
from .storage import NodeStorage
from .transaction import create_signed_transaction

STATUS_INTERVAL_SECONDS = 10


def timestamped(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# ----------------------------------------------------------------- node


async def run_node(args: argparse.Namespace) -> None:
    if args.memory:
        node = Node(miner_address=args.mine, log=timestamped)
        timestamped("mode --memory : rien ne sera enregistré sur le disque")
    else:
        storage = NodeStorage(args.data_dir or f"data/node-{args.port}")
        existed = storage.exists()
        node = storage.open_node(miner_address=args.mine, log=timestamped)
        timestamped(
            f"dossier {storage.directory} : chaîne {'chargée' if existed else 'créée'}, hauteur {node.height}, "
            f"travail {node.work}, {len(node.mempool)} transaction(s) en attente, "
            f"{len(node.known_addresses)} adresse(s) connue(s)"
            + (f", {storage.repaired_lines} fin de fichier tronquée réparée" if storage.repaired_lines else "")
            + (f", {storage.dropped_transactions} transaction(s) périmée(s) écartée(s)" if storage.dropped_transactions else "")
        )
    server = NodeServer(node, args.host, args.port, log=timestamped)
    await server.start()
    if args.mine:
        timestamped(f"minage activé pour {args.mine[:16]}...")
    for address in list(dict.fromkeys(args.peers + list(node.known_addresses)))[:MAX_PEERS]:
        await server.connect(address)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            pass  # Windows : Ctrl+C lève KeyboardInterrupt dans asyncio.run()

    async def stop_on_failure() -> None:
        await server.failed.wait()
        stop.set()

    watcher = asyncio.create_task(stop_on_failure())
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=STATUS_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                timestamped(
                    f"hauteur {node.height}, travail {node.work}, difficulté {node.tip.difficulty}, "
                    f"{len(node.peers)} pair(s), {len(node.mempool)} transaction(s) en attente"
                    + (f", {server.blocks_mined} bloc(s) miné(s) ici" if args.mine else "")
                )
    finally:
        watcher.cancel()
        await server.stop()
        if server.fatal_error is not None:
            timestamped(f"nœud arrêté : {server.fatal_error}")
            sys.exit(1)
        timestamped("nœud arrêté")


# ---------------------------------------------------------------- client


async def request(node_address: str, requests: list, expected_types: list[str], timeout: float = 5.0) -> list:
    """Se connecte, échange les hello, envoie requests et retourne une réponse par type attendu."""
    host, port = parse_address(node_address)
    reader, writer = await asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES)
    replies = []
    try:
        genesis = create_genesis_block()
        writer.write(
            encode_message(
                message(
                    HELLO,
                    node_id="client-" + KeyPair.generate().address[:16],
                    version=PROTOCOL_VERSION,
                    listen_port=None,
                    height=0,
                    work=genesis.difficulty,
                    tip_hash=genesis.hash,
                )
            )
        )
        for msg in requests:
            writer.write(encode_message(msg))
        await writer.drain()
        deadline = asyncio.get_running_loop().time() + timeout
        wanted = list(expected_types)
        while wanted:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("le nœud n'a pas répondu à temps")
            line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            if not line:
                raise ConnectionError("le nœud a fermé la connexion")
            reply = decode_message(line)
            if reply.type == wanted[0]:
                replies.append(reply)
                wanted.pop(0)
            elif reply.type == REJECT:
                replies.append(reply)
                return replies
    finally:
        writer.close()
    return replies


async def run_status(args: argparse.Namespace) -> None:
    requests, expected = [], [HELLO]
    if args.address:
        requests.append(message(GET_ACCOUNT, address=args.address))
        expected.append(ACCOUNT)
    replies = await request(args.node, requests, expected)
    hello = replies[0]
    print(f"nœud {hello['node_id']} sur {args.node}")
    print(f"  hauteur {hello['height']}, travail cumulé {hello['work']}, pointe {hello['tip_hash'][:16]}...")
    if args.address:
        account = replies[1]
        print(f"  compte {args.address[:16]}... :")
        print(f"    solde confirmé      {format_units(account['balance'])} (séquence suivante {account['next_sequence']})")
        print(f"    solde projeté       {format_units(account['projected_balance'])} (séquence suivante {account['projected_next_sequence']})")


async def run_send(args: argparse.Namespace) -> None:
    key = KeyPair.from_seed_hex(args.seed_hex)
    amount = parse_coin_amount(args.amount)
    sequence = args.sequence
    if sequence is None:
        (account,) = (await request(args.node, [message(GET_ACCOUNT, address=key.address)], [HELLO, ACCOUNT]))[1:]
        sequence = account["projected_next_sequence"]
    transaction = create_signed_transaction(key, args.to, amount, args.data, sequence)
    replies = await request(
        args.node,
        [message(NEW_TRANSACTION, transaction=transaction_to_dict(transaction)), message(GET_ACCOUNT, address=key.address)],
        [HELLO, ACCOUNT],
    )
    last = replies[-1]
    if last.type == REJECT:
        print(f"REFUSÉE : {last['reason']}")
        sys.exit(1)
    print(f"transaction {transaction.hash} envoyée à {args.node}")
    print(f"  {format_units(amount)} de {key.address[:16]}... vers {args.to[:16]}..., séquence {sequence}")
    print(f"  solde projeté après envoi : {format_units(last['projected_balance'])}")


def run_keygen(args: argparse.Namespace) -> None:
    key = KeyPair.generate()
    print(f"adresse (publique) : {key.address}")
    print(f"graine (PRIVÉE)    : {key.seed_hex}")
    print("Conservez la graine : elle seule permet de signer au nom de cette adresse.")


# ------------------------------------------------------------------ main


def address_argument(value: str) -> str:
    if not is_valid_address(value):
        raise argparse.ArgumentTypeError("adresse attendue : 64 caractères hexadécimaux minuscules")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m powchain", description="powchain : nœud, clés, paiements.")
    commands = parser.add_subparsers(dest="command", required=True)

    node = commands.add_parser("node", help="lancer un nœud (et miner si --mine)")
    node.add_argument("--host", default="127.0.0.1")
    node.add_argument("--port", type=int, default=5000)
    node.add_argument("--peers", default="", help="adresses hôte:port séparées par des virgules")
    node.add_argument("--mine", type=address_argument, default=None, metavar="ADRESSE", help="miner pour cette adresse")
    node.add_argument("--data-dir", default=None, metavar="DOSSIER", help="dossier de données (défaut : data/node-<port>)")
    node.add_argument("--memory", action="store_true", help="ne rien enregistrer sur le disque")

    commands.add_parser("keygen", help="générer une paire de clés")

    status = commands.add_parser("status", help="interroger un nœud")
    status.add_argument("--node", default="127.0.0.1:5000")
    status.add_argument("--address", type=address_argument, default=None, help="afficher aussi ce compte")

    send = commands.add_parser("send", help="signer et envoyer un paiement à un nœud")
    send.add_argument("--node", default="127.0.0.1:5000")
    send.add_argument("--seed-hex", required=True, help="graine privée de l'expéditeur (64 hex)")
    send.add_argument("--to", type=address_argument, required=True)
    send.add_argument("--amount", required=True, help="montant en COIN, ex. 1.5")
    send.add_argument("--data", default="")
    send.add_argument("--sequence", type=int, default=None, help="sinon demandée au nœud")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    args_peers = [address.strip() for address in getattr(args, "peers", "").split(",") if address.strip()]
    args.peers = args_peers
    try:
        if args.command == "node":
            asyncio.run(run_node(args))
        elif args.command == "keygen":
            run_keygen(args)
        elif args.command == "status":
            asyncio.run(run_status(args))
        elif args.command == "send":
            asyncio.run(run_send(args))
    except KeyboardInterrupt:
        print()
    except (PowChainError, OSError, TimeoutError, ValueError) as error:
        print(f"erreur : {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
