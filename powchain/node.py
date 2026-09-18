"""Nœud pair-à-pair : la logique du protocole, sans aucune socket.

Un Node possède une chaîne, un mempool et la liste de ses pairs. Il ne lit
ni n'écrit sur le réseau : le transport (network.py, ou le réseau simulé de
main.py) lui signale les connexions et les messages reçus, et le Node
répond par une liste d'ACTIONS à exécuter :

    Send(peer_id, message)      envoyer un message à ce pair
    Connect(address)            ouvrir une connexion vers « hôte:port »
    Disconnect(peer_id, reason) fermer la connexion (pair fautif ou inutile)

Cette séparation rend toute la logique (gossip, synchronisation, forks,
règle du plus grand travail) testable de façon déterministe, sans sockets.

Ce qu'un nœud fait
------------------
* Poignée de main : chaque côté envoie « hello » en premier. Un hello révèle
  node_id (contre les connexions à soi-même et en double), le port d'écoute
  (pour la découverte), la hauteur et le travail cumulé (pour savoir qui doit
  se synchroniser avec qui). Tout autre message avant hello = déconnexion.
* Découverte : après hello, chaque nœud envoie les adresses qu'il connaît ;
  le destinataire se connecte aux inconnues tant qu'il a moins de max_peers.
* Gossip des transactions : une transaction reçue est admise dans le mempool
  (règles M1-M4) puis renvoyée à tous les autres pairs. Une transaction déjà
  connue n'est pas relayée : c'est ce qui fait s'éteindre la rumeur. Une
  transaction refusée vaut un « reject » motivé à l'expéditeur, sans
  déconnexion (le refus peut venir d'un simple décalage d'état).
* Gossip des blocs : un bloc qui prolonge notre pointe est validé (B1-B7,
  état S1-S3, et règle N1 ci-dessous), ajouté, purgé du mempool et relayé.
  Un bloc invalide vaut déconnexion : personne ne fabrique un bloc miné
  invalide par accident. Un bloc déjà connu est ignoré (fin du gossip).
* Synchronisation : un bloc qui ne prolonge PAS notre pointe (pair en
  avance, ou sur une autre branche), ou un hello annonçant plus de travail,
  déclenche get_blocks à partir de min(notre hauteur + 1, hauteur du pair) :
  le premier index où les deux chaînes peuvent différer. Une seule
  synchronisation à la fois. Si le premier bloc reçu ne se greffe pas sur
  notre chaîne, la divergence est plus ancienne : on redemande plus bas
  (recul 1, 2, 4, 8... blocs, jusqu'au bloc 1). Les lots s'accumulent
  (has_more) jusqu'à la pointe du pair, puis la branche candidate = nos
  blocs jusqu'au point de divergence + les blocs reçus est évaluée :
      - travail cumulé (chain_work) pas plus grand que le nôtre => ignorée ;
      - sinon prolongement de notre pointe => ajout bloc par bloc ;
      - sinon RÉORGANISATION : revalidation complète depuis le Genesis
        (Blockchain.from_blocks), remplacement, et les transactions des
        blocs abandonnés retournent au mempool (Mempool.resync).
  Règle de consensus : la chaîne au PLUS GRAND TRAVAIL CUMULÉ l'emporte, pas
  la plus longue. À égalité, on garde la nôtre (première vue).
* Règle réseau N1 (borne d'horloge) : un bloc daté de plus de
  MAX_FUTURE_DRIFT_SECONDS dans le futur est refusé, sans déconnexion (le
  pair a peut-être juste une horloge fausse ; le bloc pourra être accepté
  plus tard). Sans cette règle, un mineur daterait ses blocs dans le futur
  pour obtenir -12,5 % de difficulté à chaque bloc (proof_of_work.py).
  Ce n'est pas une règle de la chaîne (validate_block) : elle dépend de
  l'heure à laquelle on regarde, pas du bloc lui-même.
* Minage : build_candidate() assemble un bloc sur la pointe courante avec
  les transactions du mempool ; submit_block() adopte et relaie un bloc
  miné localement, sauf si la pointe a changé entre-temps (bloc périmé).

Ce qu'un nœud ne fait pas (limites connues, voir README) : pas
d'authentification des pairs, pas de score de mauvaise conduite ni de
bannissement, pas de reconnexion automatique, et la règle du plus grand
travail ne protège que si la majorité de la puissance de calcul est honnête.
"""

import secrets
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .address import is_valid_address
from .block import Block, create_block
from .chain import Blockchain, chain_work
from .codec import block_from_dict, block_to_dict, blocks_from_list, blocks_to_list, transaction_from_dict, transaction_to_dict
from .errors import CodecError, InvalidBlockError, InvalidChainError, InvalidTransactionError, MempoolError, ProtocolError
from .mempool import Mempool
from .protocol import (
    ACCOUNT,
    BLOCKS,
    GET_ACCOUNT,
    GET_BLOCKS,
    HELLO,
    MAX_BLOCKS_PER_MESSAGE,
    MAX_PEERS_PER_MESSAGE,
    NEW_BLOCK,
    NEW_TRANSACTION,
    PEERS,
    PROTOCOL_VERSION,
    REJECT,
    Message,
    format_address,
    message,
    parse_address,
    validate_message,
)
from .transaction import Transaction

MAX_FUTURE_DRIFT_SECONDS = 120  # règle N1 : 12 x TARGET_BLOCK_TIME, même ratio que Bitcoin (2 h / 10 min)
MAX_PEERS = 8  # connexions simultanées (entrantes + sortantes) qu'un nœud accepte d'ouvrir lui-même
MAX_KNOWN_ADDRESSES = 64  # carnet d'adresses pour la découverte
MAX_SYNC_BLOCKS = 100_000  # blocs accumulés au plus pendant une synchronisation (garde-fou mémoire)
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class Send:
    peer_id: object
    message: Message


@dataclass(frozen=True, slots=True)
class Connect:
    address: str


@dataclass(frozen=True, slots=True)
class Disconnect:
    peer_id: object
    reason: str


Action = Send | Connect | Disconnect


@dataclass(slots=True)
class Peer:
    """Ce que le nœud sait d'une connexion. node_id vaut None tant que hello n'est pas reçu."""

    peer_id: object
    host: str
    outbound: bool
    dialed_address: str | None = None
    node_id: str | None = None
    listen_port: int | None = None
    height: int = 0
    work: int = 0
    tip_hash: str = ""

    @property
    def ready(self) -> bool:
        return self.node_id is not None

    @property
    def address(self) -> str | None:
        """Adresse à laquelle ce pair accepte des connexions, si on la connaît."""
        if self.dialed_address is not None:
            return self.dialed_address
        if self.listen_port is not None:
            return format_address(self.host, self.listen_port)
        return None


@dataclass(slots=True)
class SyncState:
    """Synchronisation en cours avec un pair : d'où on demande, et ce qu'on a reçu."""

    from_index: int
    step: int = 1
    received: list[Block] = field(default_factory=list)


class Node:
    """Logique d'un nœud : chaîne + mempool + pairs, pilotée par messages, sans réseau."""

    def __init__(
        self,
        *,
        node_id: str | None = None,
        miner_address: str | None = None,
        listen_port: int | None = None,
        clock: Callable[[], float] = time.time,
        chain: Blockchain | None = None,
        mempool: Mempool | None = None,
        max_peers: int = MAX_PEERS,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if miner_address is not None and not is_valid_address(miner_address):
            raise ValueError(f"adresse de minage invalide : {miner_address!r}")
        self.node_id = node_id if node_id is not None else secrets.token_hex(8)
        self.miner_address = miner_address
        self.listen_port = listen_port
        self.chain = chain if chain is not None else Blockchain()
        self.mempool = mempool if mempool is not None else Mempool()
        self.stats: Counter[str] = Counter()
        self._clock = clock
        self._max_peers = max_peers
        self._log = log if log is not None else (lambda text: None)
        self._peers: dict[object, Peer] = {}
        self._syncs: dict[object, SyncState] = {}
        self._known_addresses: dict[str, None] = {}  # dict = ensemble ordonné
        self._handlers = {
            HELLO: self._on_hello,
            PEERS: self._on_peers,
            NEW_TRANSACTION: self._on_new_transaction,
            NEW_BLOCK: self._on_new_block,
            GET_BLOCKS: self._on_get_blocks,
            BLOCKS: self._on_blocks,
            GET_ACCOUNT: self._on_get_account,
            ACCOUNT: self._ignore,
            REJECT: self._ignore,
        }

    # ------------------------------------------------------------------ vues

    @property
    def tip(self) -> Block:
        return self.chain.last_block

    @property
    def height(self) -> int:
        return self.chain.height

    @property
    def work(self) -> int:
        return self.chain.total_work

    @property
    def peers(self) -> tuple[Peer, ...]:
        """Pairs dont la poignée de main est terminée."""
        return tuple(peer for peer in self._peers.values() if peer.ready)

    @property
    def connections(self) -> int:
        """Connexions ouvertes, poignée de main terminée ou non."""
        return len(self._peers)

    @property
    def known_addresses(self) -> tuple[str, ...]:
        return tuple(self._known_addresses)

    @property
    def syncing(self) -> bool:
        return bool(self._syncs)

    def peer(self, peer_id: object) -> Peer | None:
        return self._peers.get(peer_id)

    def now(self) -> int:
        """Heure locale du nœud en secondes Unix (injectable pour les tests)."""
        return int(self._clock())

    def __repr__(self) -> str:
        return f"Node({self.node_id[:8]}, hauteur {self.height}, travail {self.work}, {len(self.peers)} pair(s))"

    # ------------------------------------------------- appels du transport

    def on_connect(self, peer_id: object, host: str, outbound: bool, address: str | None = None) -> list[Action]:
        """Nouvelle connexion (entrante ou sortante) : on se présente."""
        if peer_id in self._peers:
            raise ValueError(f"peer_id déjà utilisé : {peer_id!r}")
        self._peers[peer_id] = Peer(peer_id, host, outbound, dialed_address=address)
        if address is not None:
            self._remember_address(address)
        return [Send(peer_id, self.hello())]

    def on_disconnect(self, peer_id: object) -> list[Action]:
        self._peers.pop(peer_id, None)
        self._syncs.pop(peer_id, None)
        return []

    def on_message(self, peer_id: object, msg: Message) -> list[Action]:
        """Traite un message reçu de peer_id et retourne les actions à exécuter.

        Ne lève jamais pour un message hostile : un message hors protocole
        vaut Disconnect. Un message d'une connexion inconnue est ignoré.
        """
        peer = self._peers.get(peer_id)
        if peer is None:
            return []
        try:
            validate_message(msg)
        except ProtocolError as error:
            return [Disconnect(peer_id, f"message hors protocole : {error}")]
        if not peer.ready and msg.type != HELLO:
            return [Disconnect(peer_id, f"« {msg.type} » reçu avant hello")]
        if peer.ready and msg.type == HELLO:
            return [Disconnect(peer_id, "hello reçu deux fois")]
        try:
            return self._handlers[msg.type](peer, msg)
        except (CodecError, ProtocolError) as error:
            return [Disconnect(peer_id, f"message « {msg.type} » mal formé : {error}")]

    # ------------------------------------------------------- API locale

    def hello(self) -> Message:
        return message(
            HELLO,
            node_id=self.node_id,
            version=PROTOCOL_VERSION,
            listen_port=self.listen_port,
            height=self.height,
            work=self.work,
            tip_hash=self.tip.hash,
        )

    def submit_transaction(self, transaction: Transaction) -> list[Action]:
        """Transaction émise localement (wallet, CLI) : admise dans le mempool puis diffusée.

        Lève InvalidTransactionError / MempoolError si elle est refusée.
        """
        if transaction in self.mempool:
            return []
        self.mempool.add(transaction, self.chain.state)
        self.stats["transactions_accepted"] += 1
        return self._broadcast(message(NEW_TRANSACTION, transaction=transaction_to_dict(transaction)))

    def build_candidate(self, coinbase_data: str = "") -> Block | None:
        """Bloc candidat sur la pointe courante avec les transactions du mempool (None sans mineur)."""
        if self.miner_address is None:
            return None
        timestamp = max(self.now(), self.tip.timestamp + 1)
        selected = self.mempool.select(self.chain.state)
        return create_block(self.tip, selected, self.miner_address, timestamp=timestamp, coinbase_data=coinbase_data)

    def submit_block(self, block: Block) -> list[Action]:
        """Bloc miné localement : adopté et diffusé, ou ignoré s'il est périmé (pointe changée).

        Lève InvalidBlockError si le bloc est invalide (ce serait un bug local).
        """
        if block.prev_hash != self.tip.hash:
            self.stats["blocks_stale"] += 1
            self._log(f"bloc n°{block.index} miné trop tard : la pointe a changé, bloc abandonné")
            return []
        return self._adopt_block(block, origin=None)

    # ------------------------------------------------------- gestionnaires

    def _ignore(self, peer: Peer, msg: Message) -> list[Action]:
        return []

    def _on_hello(self, peer: Peer, msg: Message) -> list[Action]:
        if msg["version"] != PROTOCOL_VERSION:
            return [Disconnect(peer.peer_id, f"version de protocole {msg['version']} non prise en charge")]
        if msg["node_id"] == self.node_id:
            return [Disconnect(peer.peer_id, "connexion à soi-même")]
        if any(other.node_id == msg["node_id"] for other in self._peers.values() if other is not peer):
            return [Disconnect(peer.peer_id, "déjà connecté à ce nœud")]
        listen_port = msg["listen_port"]
        if listen_port is not None and not 1 <= listen_port <= 65535:
            return [Disconnect(peer.peer_id, f"port d'écoute invalide : {listen_port}")]
        peer.node_id = msg["node_id"]
        peer.listen_port = listen_port
        peer.height, peer.work, peer.tip_hash = msg["height"], msg["work"], msg["tip_hash"]
        if peer.address is not None:
            self._remember_address(peer.address)
        self._log(f"pair {peer.node_id[:8]} connecté ({'sortant' if peer.outbound else 'entrant'}) : hauteur {peer.height}, travail {peer.work}")
        actions: list[Action] = []
        shareable = [address for address in self._known_addresses if address != peer.address]
        if shareable:
            actions.append(Send(peer.peer_id, message(PEERS, addresses=shareable[:MAX_PEERS_PER_MESSAGE])))
        if peer.work > self.work:
            actions.extend(self._start_sync(peer, min(self.height + 1, peer.height)))
        return actions

    def _on_peers(self, peer: Peer, msg: Message) -> list[Action]:
        actions: list[Action] = []
        for raw in msg["addresses"]:
            host, port = parse_address(raw)  # ProtocolError => Disconnect via on_message
            address = format_address(host, port)
            if address in self._known_addresses or self._is_own_address(host, port):
                continue
            self._remember_address(address)
            if self.connections < self._max_peers:
                actions.append(Connect(address))
        return actions

    def _on_new_transaction(self, peer: Peer, msg: Message) -> list[Action]:
        transaction = transaction_from_dict(msg["transaction"])
        if transaction in self.mempool:
            return []  # déjà connue : la rumeur s'arrête ici
        try:
            self.mempool.add(transaction, self.chain.state)
        except (InvalidTransactionError, MempoolError) as error:
            self.stats["transactions_rejected"] += 1
            return [Send(peer.peer_id, message(REJECT, hash=transaction.hash, reason=str(error)))]
        self.stats["transactions_accepted"] += 1
        self._log(f"transaction {transaction.hash[:12]}... reçue de {peer.node_id[:8]}, relayée")
        return self._broadcast(message(NEW_TRANSACTION, transaction=transaction_to_dict(transaction)), exclude=peer.peer_id)

    def _on_new_block(self, peer: Peer, msg: Message) -> list[Action]:
        block = block_from_dict(msg["block"])
        if block.hash in self.chain:
            return []  # déjà dans notre chaîne : la rumeur s'arrête ici
        if block.index == self.height + 1 and block.prev_hash == self.tip.hash:
            return self._adopt_block(block, origin=peer.peer_id)
        # Le bloc ne prolonge pas notre pointe : le pair est en avance ou sur une autre branche.
        return self._start_sync(peer, min(self.height + 1, block.index))

    def _on_get_blocks(self, peer: Peer, msg: Message) -> list[Action]:
        batch = self.chain.blocks_from(msg["from_index"], MAX_BLOCKS_PER_MESSAGE)
        has_more = bool(batch) and batch[-1].index < self.height
        return [Send(peer.peer_id, message(BLOCKS, blocks=blocks_to_list(batch), has_more=has_more))]

    def _on_blocks(self, peer: Peer, msg: Message) -> list[Action]:
        sync = self._syncs.get(peer.peer_id)
        if sync is None:
            return []  # réponse non sollicitée
        blocks = blocks_from_list(msg["blocks"])
        if not blocks:
            del self._syncs[peer.peer_id]
            return []  # le pair n'a rien au-delà de ce qu'on a demandé
        # Continuité du lot avec ce qu'on a déjà reçu (index consécutifs, chaînage des hashes).
        expected_index = sync.received[-1].index + 1 if sync.received else sync.from_index
        expected_prev = sync.received[-1].hash if sync.received else None
        for block in blocks:
            if block.index != expected_index or (expected_prev is not None and block.prev_hash != expected_prev):
                del self._syncs[peer.peer_id]
                return [Disconnect(peer.peer_id, f"lot de blocs incohérent (attendu bloc n°{expected_index})")]
            expected_index, expected_prev = block.index + 1, block.hash
        if not sync.received and not self._grafts_on_our_chain(blocks[0]):
            # Le point de divergence est plus ancien : on redemande plus bas, en reculant de plus en plus.
            if sync.from_index <= 1:
                del self._syncs[peer.peer_id]
                return [Disconnect(peer.peer_id, "aucun ancêtre commun : Genesis différent")]
            sync.from_index = max(1, sync.from_index - sync.step)
            sync.step *= 2
            return [Send(peer.peer_id, message(GET_BLOCKS, from_index=sync.from_index))]
        sync.received.extend(blocks)
        if len(sync.received) > MAX_SYNC_BLOCKS:
            del self._syncs[peer.peer_id]
            return [Disconnect(peer.peer_id, f"synchronisation de plus de {MAX_SYNC_BLOCKS} blocs")]
        if msg["has_more"]:
            return [Send(peer.peer_id, message(GET_BLOCKS, from_index=sync.received[-1].index + 1))]
        del self._syncs[peer.peer_id]
        return self._evaluate_branch(peer, sync.received)

    def _on_get_account(self, peer: Peer, msg: Message) -> list[Action]:
        address = msg["address"]
        if not is_valid_address(address):
            raise ProtocolError(f"adresse invalide : {address!r}")
        state = self.chain.state
        current = state.account(address)
        projected = self.mempool.projected_state(state).account(address)
        return [
            Send(
                peer.peer_id,
                message(
                    ACCOUNT,
                    address=address,
                    balance=current.balance,
                    next_sequence=current.next_sequence,
                    projected_balance=projected.balance,
                    projected_next_sequence=projected.next_sequence,
                    height=self.height,
                ),
            )
        ]

    # ------------------------------------------------- blocs et branches

    def _adopt_block(self, block: Block, origin: object | None) -> list[Action]:
        """Ajoute un bloc qui prolonge notre pointe (règles B, S et N1), purge le mempool, relaie."""
        drift = self._future_drift(block)
        if drift is not None:
            self.stats["blocks_future"] += 1
            self._log(f"bloc n°{block.index} refusé : daté de {drift} s dans le futur (règle N1, max {MAX_FUTURE_DRIFT_SECONDS} s)")
            return []
        try:
            self.chain.add_block(block)
        except InvalidBlockError as error:
            self.stats["blocks_rejected"] += 1
            if origin is None:
                raise
            return [Disconnect(origin, f"bloc invalide : {error}")]
        self.mempool.remove_confirmed(block, self.chain.state)
        self.stats["blocks_accepted"] += 1
        source = "miné ici" if origin is None else f"reçu de {self._name(origin)}"
        self._log(f"bloc n°{block.index} {block.hash[:12]}... {source} : {len(block.transactions) - 1} transaction(s), difficulté {block.difficulty}")
        return self._broadcast(message(NEW_BLOCK, block=block_to_dict(block)), exclude=origin)

    def _start_sync(self, peer: Peer, from_index: int) -> list[Action]:
        """Demande au pair ses blocs à partir de from_index (premier index où on peut différer)."""
        if self._syncs:
            return []  # une synchronisation à la fois : les autres pairs attendront le prochain bloc
        sync = SyncState(from_index=max(1, from_index))
        self._syncs[peer.peer_id] = sync
        self.stats["syncs"] += 1
        return [Send(peer.peer_id, message(GET_BLOCKS, from_index=sync.from_index))]

    def _grafts_on_our_chain(self, first: Block) -> bool:
        """Vrai si le bloc précédent de first est un bloc de NOTRE chaîne (point de greffe)."""
        anchor = self.chain.block_at(first.index - 1)
        return anchor is not None and anchor.hash == first.prev_hash

    def _evaluate_branch(self, peer: Peer, received: list[Block]) -> list[Action]:
        """Branche reçue complète : l'adopter si, et seulement si, elle pèse plus de travail."""
        first = received[0]
        if not self._grafts_on_our_chain(first):
            self._log("synchronisation abandonnée : notre chaîne a changé entre-temps")
            return []
        ours = self.chain.blocks
        candidate = list(ours[: first.index]) + received
        if chain_work(candidate) <= self.chain.total_work:
            self.stats["branches_lighter"] += 1
            self._log(
                f"branche de {self._name(peer.peer_id)} ignorée : travail {chain_work(candidate)} "
                f"<= {self.chain.total_work} (la nôtre), longueur {len(candidate) - 1} contre {self.height}"
            )
            return []
        for block in received:
            drift = self._future_drift(block)
            if drift is not None:
                self.stats["blocks_future"] += 1
                self._log(f"branche ignorée : bloc n°{block.index} daté de {drift} s dans le futur (règle N1)")
                return []
        if first.index == self.height + 1:
            # Simple retard : la branche prolonge notre pointe, ajout bloc par bloc.
            for block in received:
                try:
                    self.chain.add_block(block)
                except InvalidBlockError as error:
                    self.stats["blocks_rejected"] += 1
                    return [Disconnect(peer.peer_id, f"bloc n°{block.index} invalide : {error}")]
                self.mempool.remove_confirmed(block, self.chain.state)
                self.stats["blocks_accepted"] += 1
            self._log(f"rattrapage : {len(received)} bloc(s) de {self._name(peer.peer_id)}, hauteur {self.height}")
        else:
            # Fork : revalidation complète, puis remplacement de la chaîne.
            try:
                new_chain = Blockchain.from_blocks(candidate)
            except InvalidChainError as error:
                self.stats["blocks_rejected"] += 1
                return [Disconnect(peer.peer_id, f"branche invalide : {error}")]
            abandoned = ours[first.index :]
            self.chain = new_chain
            returned = tuple(tx for block in abandoned for tx in block.transactions if not tx.is_coinbase)
            dropped = self.mempool.resync(new_chain.state, returned)
            self.stats["reorganizations"] += 1
            self.stats["blocks_accepted"] += len(received)
            self._log(
                f"RÉORGANISATION : {len(abandoned)} bloc(s) abandonné(s) depuis le bloc n°{first.index}, "
                f"{len(received)} adopté(s) de {self._name(peer.peer_id)} ; {len(returned)} transaction(s) "
                f"rendue(s) au mempool, {len(dropped)} purgée(s) ; hauteur {self.height}, travail {self.work}"
            )
        return self._broadcast(message(NEW_BLOCK, block=block_to_dict(self.tip)), exclude=peer.peer_id)

    def _future_drift(self, block: Block) -> int | None:
        """Règle N1 : nombre de secondes d'avance sur l'horloge locale au-delà de la borne, sinon None."""
        drift = block.timestamp - self.now()
        return drift if drift > MAX_FUTURE_DRIFT_SECONDS else None

    # ------------------------------------------------------------ utilitaires

    def _broadcast(self, msg: Message, exclude: object | None = None) -> list[Action]:
        return [Send(peer.peer_id, msg) for peer in self.peers if peer.peer_id != exclude]

    def _remember_address(self, address: str) -> None:
        self._known_addresses.pop(address, None)
        self._known_addresses[address] = None
        while len(self._known_addresses) > MAX_KNOWN_ADDRESSES:
            del self._known_addresses[next(iter(self._known_addresses))]

    def _is_own_address(self, host: str, port: int) -> bool:
        return self.listen_port == port and host in LOCAL_HOSTS

    def _name(self, peer_id: object) -> str:
        peer = self._peers.get(peer_id)
        return peer.node_id[:8] if peer is not None and peer.node_id else str(peer_id)
