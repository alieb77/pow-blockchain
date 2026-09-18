"""Persistance sur disque : un dossier de données par nœud.

    <data_dir>/
    ├── blocks.jsonl     la chaîne, un bloc par ligne (Genesis compris), ajout en fin de fichier
    ├── mempool.jsonl    les transactions en attente, une par ligne
    └── peers.json       le carnet d'adresses « hôte:port »

Principes
---------
* Le format est celui du codec JSON (codec.py) : lisible à l'œil, un objet
  par ligne, terminé par « \\n ». Le fichier de blocs contient TOUTE la
  chaîne, Genesis inclus : il se suffit à lui-même.
* Ajouter un bloc = ajouter une ligne (coût constant), puis flush + fsync :
  quand le nœud relaie un bloc, ce bloc est déjà sur le disque. Une
  réorganisation réécrit le fichier entier dans un fichier temporaire puis
  le renomme (os.replace, atomique) : à aucun moment le disque ne contient
  une chaîne à moitié écrite.
* Charger = ne rien croire. Chaque bloc relu est validé depuis le Genesis
  (Blockchain.from_blocks : hashes, preuve de travail, signatures, soldes).
  Un fichier modifié à la main est refusé avec la raison (StorageError). Une
  transaction du mempool devenue invalide (confirmée entre-temps, solde
  disparu) est simplement écartée.
* Réparation : si le programme a été coupé pendant l'écriture d'une ligne,
  la DERNIÈRE ligne peut être incomplète (pas de « \\n » final, ou JSON
  tronqué). Elle est ignorée et le fichier tronqué à la dernière ligne
  complète. Une ligne illisible AILLEURS qu'à la fin est une corruption :
  refus.
* Le nœud ne sait pas qu'un disque existe : NodeStorage s'inscrit comme
  auditeur (Node.add_listener) et réagit aux événements BlockAdded,
  ChainReorganized, TransactionAdded, AddressLearned et AddressForgotten
  (carnet réécrit quand une adresse entre ou en sort). Une erreur d'écriture
  remonte dans le nœud, qui s'arrête (network.py) : mieux vaut un nœud
  arrêté qu'un nœud qui croit avoir enregistré.

Ce que la persistance ne garantit pas : la disponibilité (un fichier
supprimé est perdu ; le nœud repart du Genesis et se resynchronise auprès
de ses pairs) et l'authenticité de l'historique (un fichier remplacé par
une AUTRE chaîne valide passe la validation ; seul le réseau, par la règle
du plus grand travail, remet un tel nœud d'accord avec les autres).
"""

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from .block import Block
from .chain import Blockchain
from .codec import block_from_dict, block_to_dict, transaction_from_dict, transaction_to_dict
from .errors import CodecError, InvalidChainError, InvalidTransactionError, MempoolError, StorageError
from .mempool import Mempool
from .money import MIN_RELAY_FEE
from .node import AddressForgotten, AddressLearned, BlockAdded, ChainReorganized, Event, Node, TransactionAdded
from .protocol import parse_address
from .state import State
from .transaction import Transaction

BLOCKS_FILE = "blocks.jsonl"
MEMPOOL_FILE = "mempool.jsonl"
PEERS_FILE = "peers.json"
PEERS_FORMAT_VERSION = 1


def _encode_line(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _parse_object(line: bytes) -> dict | None:
    """Objet JSON de la ligne, ou None si elle est illisible ou n'est pas un objet."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class NodeStorage:
    """Lit et écrit le dossier de données d'un nœud, et suit un Node pour le tenir à jour."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)
        self.blocks_path = self.directory / BLOCKS_FILE
        self.mempool_path = self.directory / MEMPOOL_FILE
        self.peers_path = self.directory / PEERS_FILE
        self.repaired_lines = 0  # fins de fichier tronquées réparées au dernier chargement
        self.dropped_transactions = 0  # transactions du mempool écartées au dernier chargement
        self._node: Node | None = None
        self._blocks_on_disk = 0  # nombre de blocs lus au dernier load_chain()

    # ----------------------------------------------------------- chargement

    def exists(self) -> bool:
        return self.blocks_path.exists()

    def load_chain(self) -> Blockchain:
        """La chaîne du fichier, entièrement revalidée ; le Genesis seul si le fichier n'existe pas."""
        self._blocks_on_disk = 0
        if not self.blocks_path.exists():
            return Blockchain()
        blocks: list[Block] = []
        for position, data in enumerate(self._read_lines(self.blocks_path)):
            try:
                blocks.append(block_from_dict(data))
            except CodecError as error:
                raise StorageError(f"{self.blocks_path.name}, ligne {position + 1} : {error}") from None
        if not blocks:
            return Blockchain()
        try:
            chain = Blockchain.from_blocks(blocks)
        except InvalidChainError as error:
            raise StorageError(f"{self.blocks_path.name} : chaîne invalide ({error})") from None
        self._blocks_on_disk = len(blocks)
        return chain

    def load_mempool(
        self, state: State, max_size: int | None = None, min_fee: int = MIN_RELAY_FEE
    ) -> Mempool:
        """Le mempool du fichier, chaque transaction re-validée sur state ; les autres sont écartées."""
        mempool = Mempool(min_fee=min_fee) if max_size is None else Mempool(max_size, min_fee=min_fee)
        self.dropped_transactions = 0
        if not self.mempool_path.exists():
            return mempool
        for position, data in enumerate(self._read_lines(self.mempool_path)):
            try:
                transaction = transaction_from_dict(data)
            except CodecError as error:
                raise StorageError(f"{self.mempool_path.name}, ligne {position + 1} : {error}") from None
            try:
                mempool.add(transaction, state)
            except (InvalidTransactionError, MempoolError):
                self.dropped_transactions += 1
        return mempool

    def load_addresses(self) -> tuple[str, ...]:
        """Le carnet d'adresses, ou () s'il n'existe pas."""
        if not self.peers_path.exists():
            return ()
        try:
            data = json.loads(self.peers_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StorageError(f"{self.peers_path.name} : illisible ({error})") from None
        if not isinstance(data, dict) or data.get("version") != PEERS_FORMAT_VERSION or not isinstance(data.get("addresses"), list):
            raise StorageError(f"{self.peers_path.name} : format inattendu")
        addresses = []
        for raw in data["addresses"]:
            try:
                host, port = parse_address(raw)
            except Exception:  # ProtocolError ou type inattendu : une adresse fausse n'invalide pas le carnet
                continue
            addresses.append(f"{host}:{port}")
        return tuple(addresses)

    def open_node(self, *, min_fee: int = MIN_RELAY_FEE, **node_kwargs) -> Node:
        """Construit un Node à partir du dossier (ou d'un dossier neuf) et le suit désormais.

        Les arguments sont ceux de Node (node_id, miner_address, clock, log...) ;
        min_fee est la politique de frais du mempool rechargé (règle M5).
        Le dossier est créé s'il n'existe pas ; le Genesis est écrit aussitôt.
        """
        self.repaired_lines = 0
        chain = self.load_chain()
        mempool = self.load_mempool(chain.state, min_fee=min_fee)
        node = Node(chain=chain, mempool=mempool, **node_kwargs)
        node.remember_addresses(self.load_addresses())
        self.follow(node)
        return node

    def follow(self, node: Node) -> None:
        """Écrit l'état courant du nœud (si le disque diffère) puis enregistre chaque événement à venir."""
        self._node = node
        self._ensure_directory()
        if self._blocks_on_disk != len(node.chain):
            self.write_blocks(node.chain.blocks)
            self._blocks_on_disk = len(node.chain)
        self.write_mempool(node.mempool.transactions)
        self.write_addresses(node.known_addresses)
        node.add_listener(self._on_event)

    # -------------------------------------------------------------- écriture

    def _on_event(self, event: Event) -> None:
        node = self._node
        if node is None:
            return
        if isinstance(event, BlockAdded):
            self.append_block(event.block)
            self.write_mempool(node.mempool.transactions)
        elif isinstance(event, ChainReorganized):
            self.write_blocks(node.chain.blocks)
            self.write_mempool(node.mempool.transactions)
        elif isinstance(event, TransactionAdded):
            self._append_line(self.mempool_path, transaction_to_dict(event.transaction))
        elif isinstance(event, (AddressLearned, AddressForgotten)):
            self.write_addresses(node.known_addresses)

    def append_block(self, block: Block) -> None:
        """Ajoute une ligne (flush + fsync) : le bloc est sur le disque au retour."""
        self._append_line(self.blocks_path, block_to_dict(block))

    def write_blocks(self, blocks: Sequence[Block]) -> None:
        """Réécrit tout le fichier de blocs de façon atomique (fichier temporaire + renommage)."""
        self._write_lines(self.blocks_path, (block_to_dict(block) for block in blocks))

    def write_mempool(self, transactions: Iterable[Transaction]) -> None:
        self._write_lines(self.mempool_path, (transaction_to_dict(tx) for tx in transactions))

    def write_addresses(self, addresses: Iterable[str]) -> None:
        payload = {"version": PEERS_FORMAT_VERSION, "addresses": list(addresses)}
        self._write_atomic(self.peers_path, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")

    # --------------------------------------------------------------- interne

    def _read_lines(self, path: Path) -> list[dict]:
        """Objets JSON du fichier, une ligne par objet ; répare une fin de fichier tronquée."""
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise StorageError(f"{path.name} : lecture impossible ({error})") from None
        lines = raw.split(b"\n")
        tail = lines.pop()  # ce qui suit le dernier « \n » : vide si le fichier se termine proprement
        objects: list[dict] = []
        repaired = False
        for position, line in enumerate(lines):
            if line.strip() == b"":
                continue
            data = _parse_object(line)
            if data is None:
                if position == len(lines) - 1 and tail == b"":
                    repaired = True  # dernière ligne illisible : écriture interrompue avant le flush
                    break
                raise StorageError(f"{path.name}, ligne {position + 1} : JSON illisible")
            objects.append(data)
        if tail.strip() != b"":
            data = _parse_object(tail)  # ligne sans « \n » final : tronquée, ou juste pas terminée
            if data is not None:
                objects.append(data)
            repaired = True
        if repaired:
            self.repaired_lines += 1
            self._write_lines(path, objects)
        return objects

    def _ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError(f"dossier {self.directory} : création impossible ({error})") from None

    def _append_line(self, path: Path, data: dict) -> None:
        self._ensure_directory()
        try:
            with open(path, "ab") as handle:
                handle.write(_encode_line(data))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise StorageError(f"{path.name} : écriture impossible ({error})") from None

    def _write_lines(self, path: Path, objects: Iterable[dict]) -> None:
        self._write_atomic(path, b"".join(_encode_line(data) for data in objects))

    def _write_atomic(self, path: Path, content: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp")
        self._ensure_directory()
        try:
            with open(temporary, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            raise StorageError(f"{path.name} : écriture impossible ({error})") from None
