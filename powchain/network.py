"""Transport réseau : sockets TCP asyncio autour d'un Node, et boucle de minage.

NodeServer fait le lien entre le monde extérieur et la logique pure de
node.py :

* il écoute sur (host, port), accepte les connexions entrantes et en ouvre
  des sortantes (connect) ;
* pour chaque connexion, il lit les lignes (un message JSON par ligne, voir
  protocol.py), les décode et les transmet à Node.on_message() ;
* il exécute les actions renvoyées par le Node : Send => écrire sur la
  connexion visée, Connect => ouvrir une connexion, Disconnect => fermer ;
* si le Node a une adresse de minage, il fait tourner la boucle de minage.

Minage par tranches
-------------------
asyncio est une boucle à un seul fil : une fonction qui calcule sans rendre
la main bloque tout le réseau. mine_block(candidate, max_attempts=N) permet
de miner par tranches de N essais : entre deux tranches, le serveur rend la
main (await asyncio.sleep(0)) pour traiter les messages arrivés, puis
vérifie que le candidat est encore d'actualité (même pointe, même mempool,
même seconde). Si un bloc valide est arrivé d'un pair entre-temps, le
candidat est abandonné et reconstruit sur la nouvelle pointe : un mineur ne
gaspille pas plus d'une tranche d'essais sur un bloc devenu inutile.

Le timestamp du candidat est rafraîchi à chaque seconde : ainsi la
difficulté du bloc reflète le temps réellement écoulé depuis le précédent,
et la règle d'ajustement (proof_of_work.py) voit les blocs lents comme les
blocs rapides.

Ouverture au réseau (Partie 9)
------------------------------
Par défaut un nœud écoute sur 127.0.0.1 : seule sa machine peut le joindre.
Avec host="0.0.0.0" il écoute sur toutes les interfaces et devient joignable
depuis le réseau local, à l'adresse que donne local_ip_addresses(), ou depuis
Internet si la box redirige le port vers cette machine. Un port ouvert reçoit
aussi des connexions qui ne parlent jamais : sans hello au bout de
hello_timeout secondes, la connexion est fermée pour ne pas garder une entrée
occupée pour rien. Le plafond d'entrées (max_inbound) et la portée des
adresses (ce qu'on annonce à qui) sont dans node.py, logique pure.
"""

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass, replace

from .address import is_valid_address
from .errors import MiningLimitError, ProtocolError, StorageError
from .mining import mine_block
from .node import Connect, Disconnect, Node, Send
from .protocol import LOOPBACK, MAX_MESSAGE_BYTES, decode_message, encode_message, format_address, host_scope, parse_address

DEFAULT_MINING_CHUNK = 4096  # essais entre deux retours à la boucle réseau (quelques millisecondes)
HELLO_TIMEOUT_SECONDS = 10.0  # une connexion qui ne s'est pas présentée au bout de ce délai est fermée
ALL_INTERFACES = frozenset({"0.0.0.0", "", "::"})  # hôtes d'écoute « toutes les interfaces »


def local_ip_addresses() -> list[str]:
    """Adresses IP de cette machine hors boucle locale : ce qu'il faut donner aux autres pour nous joindre.

    Repose sur la résolution du nom de la machine (stdlib uniquement) ; peut
    être vide sur une machine sans réseau, ou incomplet avec plusieurs cartes.
    """
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return []
    return sorted({ip for ip in addresses if host_scope(ip) != LOOPBACK})


@dataclass(slots=True)
class Connection:
    peer_id: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    address: str | None  # « hôte:port » composé si c'est nous qui avons appelé
    lock: asyncio.Lock
    task: asyncio.Task | None = None


class NodeServer:
    """Fait vivre un Node sur de vraies sockets TCP (une instance par nœud)."""

    def __init__(
        self,
        node: Node,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        mining_chunk: int = DEFAULT_MINING_CHUNK,
        hello_timeout: float | None = HELLO_TIMEOUT_SECONDS,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.node = node
        self.host = host
        self.port = port  # 0 = port libre choisi par le système, connu après start()
        self.mining_chunk = mining_chunk
        self.hello_timeout = hello_timeout  # None = attendre le hello indéfiniment
        self._log = log if log is not None else (lambda text: None)
        self._server: asyncio.base_events.Server | None = None
        self._connections: dict[int, Connection] = {}
        self._next_peer_id = 1
        self._dialing: set[str] = set()
        self._background: set[asyncio.Task] = set()
        self._mining_task: asyncio.Task | None = None
        self.blocks_mined = 0
        self.fatal_error: StorageError | None = None
        self.failed = asyncio.Event()  # levé si le nœud ne peut plus enregistrer sur disque

    # ------------------------------------------------------------ cycle de vie

    @property
    def address(self) -> str:
        return format_address(self.host, self.port)

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """Ouvre le port d'écoute, puis démarre le minage si le nœud a une adresse de mineur."""
        self._server = await asyncio.start_server(self._accept, self.host, self.port, limit=MAX_MESSAGE_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]
        self.node.listen_port = self.port
        if self.host in ALL_INTERFACES:
            lan = ", ".join(format_address(ip, self.port) for ip in local_ip_addresses()) or "aucune adresse réseau détectée"
            self._log(f"nœud {self.node.node_id[:8]} à l'écoute sur toutes les interfaces, port {self.port} (réseau local : {lan})")
        else:
            self._log(f"nœud {self.node.node_id[:8]} à l'écoute sur {self.address}")
        if self.node.miner_address is not None:
            self.start_mining()

    def start_mining(self, miner_address: str | None = None) -> None:
        """Lance (ou relance) la boucle de minage, pour l'adresse du nœud ou celle fournie."""
        if miner_address is not None:
            if not is_valid_address(miner_address):
                raise ValueError(f"adresse de minage invalide : {miner_address!r}")
            self.node.miner_address = miner_address
        if self.node.miner_address is None:
            raise ValueError("aucune adresse de minage")
        if self._mining_task is None or self._mining_task.done():
            self._mining_task = asyncio.create_task(self._mine_forever())

    async def stop_mining(self) -> None:
        if self._mining_task is not None:
            self._mining_task.cancel()
            await asyncio.gather(self._mining_task, return_exceptions=True)
            self._mining_task = None

    @property
    def mining(self) -> bool:
        return self._mining_task is not None and not self._mining_task.done()

    async def stop(self) -> None:
        """Arrête le minage, ferme toutes les connexions et le port d'écoute."""
        await self.stop_mining()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        connections = list(self._connections.values())
        for connection in connections:
            self._close(connection)
        await asyncio.gather(*(c.task for c in connections if c.task is not None), return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def connect(self, address: str) -> bool:
        """Ouvre une connexion sortante vers « hôte:port » ; False si déjà connecté ou injoignable."""
        host, port = parse_address(address)
        address = format_address(host, port)
        if address in self._dialing or any(c.address == address for c in self._connections.values()):
            return False
        self._dialing.add(address)
        try:
            reader, writer = await asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES)
        except OSError as error:
            self._log(f"connexion à {address} impossible : {error}")
            return False
        finally:
            self._dialing.discard(address)
        self._register(reader, writer, outbound=True, address=address)
        return True

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> bool:
        """Attend (au plus timeout secondes) que predicate() devienne vrai ; utile en tests et démos."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)
        return True

    # ------------------------------------------------------------ connexions

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._register(reader, writer, outbound=False, address=None)

    def _register(self, reader, writer, *, outbound: bool, address: str | None) -> None:
        peer_id = self._next_peer_id
        self._next_peer_id += 1
        connection = Connection(peer_id, reader, writer, address, asyncio.Lock())
        self._connections[peer_id] = connection
        peername = writer.get_extra_info("peername")
        host = peername[0] if peername else "?"
        connection.task = asyncio.create_task(self._serve(connection, host, outbound))

    async def _serve(self, connection: Connection, host: str, outbound: bool) -> None:
        peer_id = connection.peer_id
        try:
            await self._execute(self.node.on_connect(peer_id, host, outbound, connection.address))
            awaiting_hello = True
            while peer_id in self._connections:
                try:
                    if awaiting_hello and self.hello_timeout is not None:
                        # Le premier message doit être hello : une connexion muette ne garde pas sa place.
                        line = await asyncio.wait_for(connection.reader.readline(), self.hello_timeout)
                    else:
                        line = await connection.reader.readline()
                except asyncio.TimeoutError:
                    self._log(f"pair {peer_id} : aucun hello en {self.hello_timeout:g} s, connexion fermée")
                    break
                except ValueError:  # ligne plus longue que MAX_MESSAGE_BYTES
                    self._log(f"pair {peer_id} : message trop volumineux, connexion fermée")
                    break
                awaiting_hello = False
                if not line:
                    break  # le pair a fermé
                try:
                    msg = decode_message(line)
                except ProtocolError as error:
                    self._log(f"pair {peer_id} : {error}, connexion fermée")
                    break
                await self._execute(self.node.on_message(peer_id, msg))
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        except StorageError as error:
            self._fail(error)
        finally:
            self._close(connection)
            self.node.on_disconnect(peer_id)

    def _fail(self, error: StorageError) -> None:
        """Le disque n'a pas pu être écrit : le nœud ne doit pas continuer, on le signale."""
        self._log(f"ERREUR DE STOCKAGE : {error}")
        self.fatal_error = error
        self.failed.set()

    def _close(self, connection: Connection) -> None:
        if self._connections.pop(connection.peer_id, None) is None:
            return
        try:
            connection.writer.close()
        except OSError:
            pass

    async def _execute(self, actions) -> None:
        for action in actions:
            if isinstance(action, Send):
                await self._send(action.peer_id, action.message)
            elif isinstance(action, Connect):
                task = asyncio.create_task(self.connect(action.address))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            elif isinstance(action, Disconnect):
                self._log(f"pair {action.peer_id} déconnecté : {action.reason}")
                connection = self._connections.get(action.peer_id)
                if connection is not None:
                    self._close(connection)
                    self.node.on_disconnect(action.peer_id)
            else:
                raise TypeError(f"action inconnue : {action!r}")

    async def _send(self, peer_id: int, msg) -> None:
        connection = self._connections.get(peer_id)
        if connection is None:
            return
        try:
            async with connection.lock:
                connection.writer.write(encode_message(msg))
                await connection.writer.drain()
        except (ConnectionError, OSError):
            self._close(connection)
            self.node.on_disconnect(peer_id)

    # ---------------------------------------------------------------- minage

    async def _mine_forever(self) -> None:
        while True:
            candidate = self.node.build_candidate()
            if candidate is None:
                return
            tip_hash, pool_size = self.node.tip.hash, len(self.node.mempool)
            nonce = candidate.nonce
            while True:
                try:
                    result = mine_block(replace(candidate, nonce=nonce), max_attempts=self.mining_chunk)
                except MiningLimitError:
                    nonce += self.mining_chunk
                    await asyncio.sleep(0)  # laisser passer les messages réseau
                    if (
                        self.node.tip.hash != tip_hash
                        or len(self.node.mempool) != pool_size
                        or self.node.now() > candidate.timestamp
                    ):
                        break  # candidat périmé : on repart sur la pointe / la seconde courante
                    continue
                self.blocks_mined += 1
                try:
                    await self._execute(self.node.submit_block(result.block))
                except StorageError as error:
                    self._fail(error)
                    return
                break
