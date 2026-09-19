"""Ligne de commande : lancer un nœud, gérer un wallet, interroger ou payer.

    python -m powchain node --port 5000 [--public | --host IP] [--peers 192.168.1.9:5000,...]
                            [--mine ADRESSE | --mine-label NOM [--wallet FICHIER]]
                            [--data-dir DOSSIER | --memory]
    python -m powchain status --node 127.0.0.1:5000 [--address ADRESSE]

    python -m powchain wallet create [--wallet FICHIER] [--label NOM]
    python -m powchain wallet generate [--label NOM]
    python -m powchain wallet import --seed-hex GRAINE [--label NOM]
    python -m powchain wallet list
    python -m powchain wallet address --label NOM
    python -m powchain wallet balance --node 127.0.0.1:5000 [--label NOM]
    python -m powchain wallet send --node 127.0.0.1:5000 --from NOM --to ADRESSE --amount 1.5
    python -m powchain wallet export --label NOM

    python -m powchain keygen                              (legacy : clé jetable en clair)
    python -m powchain send --seed-hex GRAINE ...          (legacy : pis-aller, préférez wallet)

Un nœud persiste par défaut dans data/node-<port>/ (blocs, mempool, carnet
d'adresses, voir storage.py) : relancé, il reprend sa chaîne et se reconnecte
seul aux adresses connues. --memory désactive toute écriture. Les adresses
de --peers sont des AMORCES : rappelées en priorité, jamais oubliées ; les
autres adresses du carnet sont rappelées avec un délai croissant et oubliées
après une douzaine d'échecs d'affilée (node.py, Partie 10).

Par défaut un nœud n'écoute que sur 127.0.0.1 : seule sa machine peut le
joindre. « --public » l'ouvre sur toutes les interfaces (0.0.0.0) : les autres
postes du réseau local le joignent à l'adresse affichée au démarrage, et
Internet aussi si la box redirige le port TCP vers cette machine. Un pair
distant s'amorce alors avec --peers <adresse>:<port>. Voir README (« Ouvrir
au réseau ») pour le pare-feu Windows et la redirection de port.

« status », « send » et les commandes « wallet balance/send » sont des CLIENTS
ÉPHÉMÈRES : ils ouvrent une connexion vers un nœud, se présentent (hello sans
port d'écoute), envoient leur demande, lisent la réponse et se déconnectent. Un
nœud ne fait aucune différence entre un client et un pair : mêmes messages,
mêmes règles.

Le wallet (wallet.py) garde les clés CHIFFRÉES sous un mot de passe : la graine
ne transite jamais en clair par la ligne de commande. Le mot de passe est
demandé de façon interactive (getpass) ; en contexte non interactif (scripts,
démo) il peut être lu dans la variable d'environnement POWCHAIN_WALLET_PASSWORD.
Les anciennes commandes « keygen » et « send --seed-hex » restent disponibles
comme dépannage, mais exposent la graine.
"""

import argparse
import asyncio
import getpass
import os
import signal
import sys
import time
from pathlib import Path

from .address import has_valid_checksum, is_valid_address, normalize_address, to_checksummed_address
from .api import API_PORT_OFFSET, ApiServer
from .errors import PowChainError, WalletError
from .keys import KeyPair
from .money import MIN_RELAY_FEE, format_units, parse_coin_amount
from .network import NodeServer, local_ip_addresses
from .node import Node
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
    format_address,
    message,
    parse_address,
)
from .block import create_genesis_block
from .codec import transaction_to_dict
from .storage import NodeStorage
from .transaction import create_signed_transaction
from .wallet import DEFAULT_WALLET_PATH, Wallet

STATUS_INTERVAL_SECONDS = 10
WALLET_PASSWORD_ENV = "POWCHAIN_WALLET_PASSWORD"

# Amorces publiques par défaut : nœuds toujours allumés auxquels un nouveau nœud
# se connecte automatiquement s'il ne reçoit pas de --peers. Sans elles, deux
# inconnus ne se trouveraient jamais et chacun minerait sa propre chaîne isolée ;
# c'est ce qui fait UN seul réseau partagé. `--no-default-peers` les désactive
# (réseau privé, développement local). Adresse hôte:port du bootnode.
DEFAULT_SEEDS: tuple[str, ...] = ("51.170.139.244:5000",)


def timestamped(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# ----------------------------------------------------------------- node


def resolve_miner_address(args: argparse.Namespace) -> str | None:
    """Adresse vers laquelle miner : depuis --mine (adresse) ou --mine-label (clé du wallet).

    Miner n'exige que la clé PUBLIQUE : on lit donc juste l'adresse du wallet,
    sans mot de passe et sans jamais toucher à la graine chiffrée. Retourne
    None si le nœud ne mine pas.
    """
    if getattr(args, "mine_label", None):
        wallet = _load_wallet(args.wallet)
        try:
            return wallet.address_of(args.mine_label)
        except WalletError:
            available = ", ".join(wallet.labels) or "(aucune)"
            raise WalletError(
                f"aucune clé « {args.mine_label} » dans {args.wallet} ; clés disponibles : {available}"
            ) from None
    return args.mine


def listen_host(args: argparse.Namespace) -> str:
    """Interface d'écoute : toutes (0.0.0.0) avec --public, sinon --host (127.0.0.1 par défaut)."""
    return "0.0.0.0" if getattr(args, "public", False) else args.host


def seed_addresses(args: argparse.Namespace) -> list[str]:
    """Amorces au démarrage : les pairs de --peers, PLUS les amorces publiques
    par défaut (le bootnode), sauf si --no-default-peers. L'ordre est conservé
    (les --peers d'abord) et les doublons sont écartés, pour qu'un simple
    `python -m powchain node` rejoigne le réseau sans rien connaître à l'avance.
    """
    seeds = list(args.peers)
    if not getattr(args, "no_default_peers", False):
        for seed in DEFAULT_SEEDS:
            if seed not in seeds:
                seeds.append(seed)
    return seeds


async def run_node(args: argparse.Namespace) -> None:
    miner_address = resolve_miner_address(args)
    min_fee = fee_argument(args.min_fee)
    if args.memory:
        node = Node(miner_address=miner_address, log=timestamped, min_fee=min_fee)
        timestamped("mode --memory : rien ne sera enregistré sur le disque")
    else:
        storage = NodeStorage(args.data_dir or f"data/node-{args.port}")
        existed = storage.exists()
        node = storage.open_node(miner_address=miner_address, log=timestamped, min_fee=min_fee)
        timestamped(
            f"dossier {storage.directory} : chaîne {'chargée' if existed else 'créée'}, hauteur {node.height}, "
            f"travail {node.work}, {len(node.mempool)} transaction(s) en attente, "
            f"{len(node.known_addresses)} adresse(s) connue(s)"
            + (f", {storage.repaired_lines} fin de fichier tronquée réparée" if storage.repaired_lines else "")
            + (f", {storage.dropped_transactions} transaction(s) périmée(s) écartée(s)" if storage.dropped_transactions else "")
        )
    server = NodeServer(node, listen_host(args), args.port, log=timestamped)
    await server.start()
    api = None
    if not args.no_api:
        api_port = args.api_port if args.api_port is not None else args.port + API_PORT_OFFSET
        api = ApiServer(server, listen_host(args), api_port, log=timestamped)
        await api.start()
    if args.public:
        reachable = [format_address(ip, server.port) for ip in local_ip_addresses()]
        timestamped(
            "nœud PUBLIC : joignable depuis le réseau local sur "
            + (", ".join(reachable) if reachable else "(aucune adresse réseau détectée)")
            + f" ; depuis Internet si la box redirige le port TCP {server.port} vers cette machine"
        )
        timestamped("un autre poste n'arrive pas à se connecter ? autorisez Python dans le pare-feu (README, « Ouvrir au réseau »)")
    if miner_address:
        origin = f" (clé « {args.mine_label} » du wallet {args.wallet})" if args.mine_label else ""
        timestamped(f"minage vers {to_checksummed_address(miner_address)[:16]}...{origin}")
    node.remember_addresses(seed_addresses(args), seed=True)  # --peers + amorces par défaut : rappelées d'abord, jamais oubliées
    await server.tick_now()  # premiers appels tout de suite ; ensuite le nœud entretient ses pairs seul

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
                    f"{len(node.peers)} pair(s) dont {sum(1 for peer in node.peers if not peer.outbound)} entrant(s), "
                    f"{len(node.known_addresses)} adresse(s) connue(s), {len(node.mempool)} transaction(s) en attente"
                    + (f", {len(node.banned_hosts)} hôte(s) banni(s)" if node.banned_hosts else "")
                    + (
                        f", {server.blocks_mined} bloc(s) miné(s) ici, "
                        f"solde du mineur {format_units(node.chain.state.balance_of(miner_address))}"
                        if miner_address
                        else ""
                    )
                )
    finally:
        watcher.cancel()
        if api is not None:
            await api.stop()
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


def fee_argument(text: str | None) -> int:
    """Frais en FLS (écriture décimale) -> unités ; omis = minimum relayé par les nœuds."""
    return MIN_RELAY_FEE if text is None else parse_coin_amount(text)


async def run_send(args: argparse.Namespace) -> None:
    key = KeyPair.from_seed_hex(args.seed_hex)
    amount = parse_coin_amount(args.amount)
    fee = fee_argument(args.fee)
    sequence = args.sequence
    if sequence is None:
        (account,) = (await request(args.node, [message(GET_ACCOUNT, address=key.address)], [HELLO, ACCOUNT]))[1:]
        sequence = account["projected_next_sequence"]
    transaction = create_signed_transaction(key, args.to, amount, args.data, sequence, fee)
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
    print(f"  {format_units(amount)} de {key.address[:16]}... vers {args.to[:16]}..., séquence {sequence}, frais {format_units(fee)}")
    print(f"  solde projeté après envoi : {format_units(last['projected_balance'])}")


def run_keygen(args: argparse.Namespace) -> None:
    key = KeyPair.generate()
    print(f"adresse (publique) : {key.address}")
    print(f"graine (PRIVÉE)    : {key.seed_hex}")
    print("Conservez la graine : elle seule permet de signer au nom de cette adresse.")
    print("(legacy : la graine est en clair ; préférez « wallet create » qui la chiffre.)")


# ---------------------------------------------------------------- wallet


def read_password(prompt: str, *, confirm: bool = False) -> str:
    """Demande un mot de passe sans l'afficher (getpass), ou le lit dans l'environnement.

    En contexte non interactif (pas de terminal, scripts, démo), la variable
    POWCHAIN_WALLET_PASSWORD sert d'échappatoire, ce qui évite de passer le mot
    de passe en argument de commande (où il serait visible).
    """
    from_env = os.environ.get(WALLET_PASSWORD_ENV)
    if from_env is not None:
        return from_env
    password = getpass.getpass(prompt)
    if not password:
        raise ValueError("mot de passe vide")
    if confirm and getpass.getpass("Confirmez le mot de passe : ") != password:
        raise ValueError("les deux mots de passe diffèrent")
    return password


def _load_wallet(path: str) -> Wallet:
    if not Path(path).exists():
        raise WalletError(f"aucun wallet à {path} (créez-en un avec « wallet create »)")
    return Wallet.load(path)


def _announce_key(label: str, address: str) -> None:
    print(f"clé « {label} » ajoutée")
    print(f"  adresse (à partager) : {to_checksummed_address(address)}")


def wallet_create(args: argparse.Namespace) -> None:
    path = Path(args.wallet)
    if path.exists():
        raise WalletError(f"un fichier existe déjà à {args.wallet} ; choisissez un autre --wallet")
    password = read_password("Nouveau mot de passe du wallet : ", confirm=True)
    wallet = Wallet.create(path)
    key = wallet.generate_key(password, label=args.label)
    wallet.save()
    print(f"wallet créé : {path}")
    _announce_key(args.label, key.address)
    print("  Mémorisez le mot de passe : il n'y a AUCUNE récupération possible s'il est perdu.")


def wallet_generate(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    label = args.label or wallet.suggest_label()
    password = read_password("Mot de passe du wallet : ")
    key = wallet.generate_key(password, label=label)
    wallet.save()
    _announce_key(label, key.address)


def wallet_import(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    label = args.label or wallet.suggest_label()
    password = read_password("Mot de passe du wallet : ")
    key = wallet.import_seed_hex(args.seed_hex, password, label=label)
    wallet.save()
    _announce_key(label, key.address)


def wallet_list(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    if not len(wallet):
        print(f"wallet vide : {args.wallet}")
        return
    print(f"{len(wallet)} clé(s) dans {args.wallet} :")
    for entry in wallet.entries:
        print(f"  {entry.label:<16} {to_checksummed_address(entry.address)}")


def wallet_address(args: argparse.Namespace) -> None:
    print(_load_wallet(args.wallet).checksummed_address_of(args.label))


def wallet_export(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    password = read_password(f"Mot de passe du wallet (révéler « {args.label} ») : ")
    seed = wallet.export_seed_hex(args.label, password)
    print(f"graine (PRIVÉE) de « {args.label} » : {seed}")
    print("Quiconque détient cette graine peut dépenser. Conservez-la hors ligne.")


async def wallet_balance(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    entries = [wallet.entry(args.label)] if args.label else list(wallet.entries)
    if not entries:
        print(f"wallet vide : {args.wallet}")
        return
    for entry in entries:
        replies = await request(args.node, [message(GET_ACCOUNT, address=entry.address)], [HELLO, ACCOUNT])
        account = replies[1]
        print(f"« {entry.label} »  {to_checksummed_address(entry.address)}")
        print(f"    solde confirmé  {format_units(account['balance'])} (séquence suivante {account['next_sequence']})")
        print(f"    solde projeté   {format_units(account['projected_balance'])} (séquence suivante {account['projected_next_sequence']})")


async def wallet_send(args: argparse.Namespace) -> None:
    wallet = _load_wallet(args.wallet)
    try:
        recipient = normalize_address(args.to, require_checksum=not args.unchecked)
    except ValueError as error:
        raise WalletError(str(error)) from None
    amount = parse_coin_amount(args.amount)
    fee = fee_argument(args.fee)
    password = read_password(f"Mot de passe du wallet (signer depuis « {args.from_label} ») : ")
    key = wallet.key_pair(args.from_label, password)
    sequence = args.sequence
    if sequence is None:
        (account,) = (await request(args.node, [message(GET_ACCOUNT, address=key.address)], [HELLO, ACCOUNT]))[1:]
        sequence = account["projected_next_sequence"]
    transaction = create_signed_transaction(key, recipient, amount, args.data, sequence, fee)
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
    print(f"  {format_units(amount)} de « {args.from_label} » vers {to_checksummed_address(recipient)[:20]}..., séquence {sequence}, frais {format_units(fee)}")
    print(f"  solde projeté après envoi : {format_units(last['projected_balance'])}")


def run_wallet(args: argparse.Namespace) -> None:
    """Aiguille vers la bonne sous-commande wallet (synchrones, ou réseau via asyncio)."""
    if args.wallet_command == "create":
        wallet_create(args)
    elif args.wallet_command == "generate":
        wallet_generate(args)
    elif args.wallet_command == "import":
        wallet_import(args)
    elif args.wallet_command == "list":
        wallet_list(args)
    elif args.wallet_command == "address":
        wallet_address(args)
    elif args.wallet_command == "export":
        wallet_export(args)
    elif args.wallet_command == "balance":
        asyncio.run(wallet_balance(args))
    elif args.wallet_command == "send":
        asyncio.run(wallet_send(args))


# ------------------------------------------------------------------ main


def address_argument(value: str) -> str:
    if not is_valid_address(value):
        raise argparse.ArgumentTypeError("adresse attendue : 64 caractères hexadécimaux minuscules")
    return value


def mine_address_argument(value: str) -> str:
    """Adresse de minage : accepte le hex brut minuscule OU la forme à somme de contrôle du wallet."""
    if is_valid_address(value):
        return value
    if has_valid_checksum(value):
        return value.lower()
    raise argparse.ArgumentTypeError(
        "adresse invalide : 64 caractères hexadécimaux, ou la forme à somme de contrôle affichée par le wallet"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m powchain", description="FLOUS (FLS) — nœud, clés, paiements (moteur powchain).")
    commands = parser.add_subparsers(dest="command", required=True)

    node = commands.add_parser("node", help="lancer un nœud (et miner si --mine / --mine-label)")
    listen = node.add_mutually_exclusive_group()
    listen.add_argument("--host", default="127.0.0.1", metavar="IP",
                        help="interface d'écoute (défaut : 127.0.0.1, cette machine seulement)")
    listen.add_argument("--public", action="store_true",
                        help="écouter sur toutes les interfaces (0.0.0.0) : joignable depuis le réseau local ou Internet")
    node.add_argument("--port", type=int, default=5000)
    node.add_argument("--peers", default="", help=f"adresses hôte:port séparées par des virgules ; s'ajoutent aux amorces par défaut ({', '.join(DEFAULT_SEEDS)})")
    node.add_argument("--no-default-peers", action="store_true",
                      help="ne pas se connecter aux amorces publiques par défaut (réseau isolé ou développement local)")
    mine = node.add_mutually_exclusive_group()
    mine.add_argument("--mine", type=mine_address_argument, default=None, metavar="ADRESSE",
                      help="miner vers cette adresse (hex brut ou forme à somme de contrôle)")
    mine.add_argument("--mine-label", default=None, metavar="NOM",
                      help="miner vers la clé NOM du wallet (aucun mot de passe : seule l'adresse publique est lue)")
    node.add_argument("--wallet", default=DEFAULT_WALLET_PATH, metavar="FICHIER",
                      help="wallet où lire --mine-label (défaut : wallet.json)")
    node.add_argument("--data-dir", default=None, metavar="DOSSIER", help="dossier de données (défaut : data/node-<port>)")
    node.add_argument("--memory", action="store_true", help="ne rien enregistrer sur le disque")
    node.add_argument("--min-fee", default=None, metavar="FLS",
                      help=f"frais minimal pour garder et relayer une transaction (défaut : {format_units(MIN_RELAY_FEE)})")
    node.add_argument("--api-port", type=int, default=None, metavar="PORT",
                      help=f"port de l'API HTTP (défaut : port P2P + {API_PORT_OFFSET}) ; même interface que le nœud (--public l'ouvre aussi)")
    node.add_argument("--no-api", action="store_true", help="ne pas servir l'API HTTP")

    commands.add_parser("keygen", help="générer une paire de clés")

    status = commands.add_parser("status", help="interroger un nœud")
    status.add_argument("--node", default="127.0.0.1:5000")
    status.add_argument("--address", type=address_argument, default=None, help="afficher aussi ce compte")

    send = commands.add_parser("send", help="legacy : payer via une graine passée en argument (préférez « wallet send »)")
    send.add_argument("--node", default="127.0.0.1:5000")
    send.add_argument("--seed-hex", required=True, help="graine privée de l'expéditeur (64 hex)")
    send.add_argument("--to", type=address_argument, required=True)
    send.add_argument("--amount", required=True, help="montant en FLS, ex. 1.5")
    send.add_argument("--fee", default=None, help=f"frais pour le mineur en FLS (défaut : {format_units(MIN_RELAY_FEE)})")
    send.add_argument("--data", default="")
    send.add_argument("--sequence", type=int, default=None, help="sinon demandée au nœud")

    _build_wallet_parser(commands)
    return parser


def _build_wallet_parser(commands: "argparse._SubParsersAction") -> None:
    """Sous-commandes du wallet : create, generate, import, list, address, balance, send, export."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--wallet", default=DEFAULT_WALLET_PATH, metavar="FICHIER", help="fichier du wallet (défaut : wallet.json)")

    wallet = commands.add_parser("wallet", help="gérer un wallet de clés chiffrées par mot de passe")
    sub = wallet.add_subparsers(dest="wallet_command", required=True)

    create = sub.add_parser("create", parents=[common], help="créer un wallet et sa première clé")
    create.add_argument("--label", default="principal", help="nom de la première clé (défaut : principal)")

    generate = sub.add_parser("generate", parents=[common], help="ajouter une nouvelle clé aléatoire")
    generate.add_argument("--label", default=None, help="nom de la clé (auto si omis)")

    imp = sub.add_parser("import", parents=[common], help="ajouter une clé depuis sa graine hexadécimale")
    imp.add_argument("--seed-hex", required=True, help="graine privée de 64 hex à importer")
    imp.add_argument("--label", default=None, help="nom de la clé (auto si omis)")

    sub.add_parser("list", parents=[common], help="lister les clés et leurs adresses")

    address = sub.add_parser("address", parents=[common], help="afficher l'adresse (à somme de contrôle) d'une clé")
    address.add_argument("--label", required=True)

    balance = sub.add_parser("balance", parents=[common], help="interroger un nœud pour le solde des clés")
    balance.add_argument("--node", default="127.0.0.1:5000")
    balance.add_argument("--label", default=None, help="une seule clé (sinon toutes)")

    wsend = sub.add_parser("send", parents=[common], help="signer et envoyer un paiement depuis une clé du wallet")
    wsend.add_argument("--node", default="127.0.0.1:5000")
    wsend.add_argument("--from", dest="from_label", required=True, metavar="NOM", help="clé expéditrice (label du wallet)")
    wsend.add_argument("--to", required=True, metavar="ADRESSE", help="adresse destinataire (forme à somme de contrôle)")
    wsend.add_argument("--amount", required=True, help="montant en FLS, ex. 1.5")
    wsend.add_argument("--fee", default=None, help=f"frais pour le mineur en FLS (défaut : {format_units(MIN_RELAY_FEE)})")
    wsend.add_argument("--data", default="")
    wsend.add_argument("--sequence", type=int, default=None, help="sinon demandée au nœud")
    wsend.add_argument("--unchecked", action="store_true", help="accepter une adresse --to sans somme de contrôle vérifiée")

    export = sub.add_parser("export", parents=[common], help="révéler la graine d'une clé (pour une sauvegarde)")
    export.add_argument("--label", required=True)


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
        elif args.command == "wallet":
            run_wallet(args)
    except KeyboardInterrupt:
        print()
    except (PowChainError, OSError, TimeoutError, ValueError) as error:
        print(f"erreur : {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
