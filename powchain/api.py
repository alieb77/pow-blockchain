"""API HTTP du nœud (Partie 12) : lire la chaîne et soumettre des paiements signés, en JSON.

Pourquoi une API HTTP en plus du protocole pair-à-pair ? Le P2P (protocol.py)
est fait pour des nœuds qui se parlent en continu sur une connexion TCP
persistante, après une poignée de main. Un navigateur, un script ou une
application tierce veut simplement POSER UNE QUESTION et lire la réponse :
c'est le modèle requête/réponse de HTTP, que tout le monde sait parler. L'API
est la porte d'entrée de l'explorateur de blocs, du wallet web et de toute
application ; le protocole P2P reste la seule voie entre nœuds.

Sans dépendance : un serveur HTTP/1.1 minimal sur les flux asyncio de la
bibliothèque standard, dans la même boucle d'événements que le NodeServer
(le Node n'est pas prévu pour plusieurs fils ; ici il n'y en a qu'un). Chaque
requête est traitée puis la connexion fermée (Connection: close) : simple,
sans état, suffisant pour un explorateur.

Ce que l'API ne fait JAMAIS : signer. Elle ne connaît aucune clé privée. Elle
reçoit des transactions DÉJÀ signées (POST /transactions) et les traite comme
si un pair les lui avait envoyées : admission au mempool (règles R, S, M) puis
diffusion. Exposer l'API (--public) n'expose donc aucun fonds.

Routes (réponses JSON ; montants en UNITÉS entières, voir money.py) :
    GET  /                          index des routes
    GET  /status                    hauteur, travail, pointe, difficulté, pairs, mempool, mineur…
    GET  /blocks?limit=20&before=H  résumés des blocs, du plus récent au plus ancien
    GET  /blocks/<index|hash>       un bloc complet, transactions comprises
    GET  /transactions/<hash>       une transaction : confirmée (bloc, confirmations) ou en attente
    POST /transactions              soumettre une transaction signée (format codec.py) -> 202
    GET  /accounts/<adresse>        solde, séquence, projections (hex brut ou somme de contrôle)
    GET  /accounts/<adresse>/transactions?limit=20&offset=0   historique, plus récent d'abord
    GET  /mempool                   transactions en attente, meilleurs payeurs d'abord
    GET  /peers                     pairs connectés, carnet d'adresses, bans

Erreurs : {"error": "..."} avec 400 (requête ou transaction invalide), 404
(inconnu), 405 (méthode), 413 (corps trop gros), 500 (bug interne, journalisé,
jamais fatal pour le nœud). CORS : Access-Control-Allow-Origin: * sur toutes
les réponses, pour qu'une page web servie d'ailleurs (fichier local, autre
hôte) puisse interroger un nœud ; sans danger car l'API ne porte ni session ni
cookie : rien à voler par une page tierce.

Limites : pas de HTTPS (mettre un proxy devant pour Internet), pas de limite
de débit par client (à venir avec l'ouverture publique), pagination simple.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from .address import normalize_address, to_checksummed_address
from .block import Block
from .codec import block_to_dict, transaction_from_dict, transaction_to_dict
from .crypto import is_valid_hash_hex
from .errors import CodecError, InvalidTransactionError, MempoolError
from .money import COIN_SYMBOL, UNITS_PER_COIN
from .network import ALL_INTERFACES, NodeServer, local_ip_addresses
from .protocol import PROTOCOL_VERSION, format_address
from .transaction import Transaction

API_VERSION = 1
API_PORT_OFFSET = 1000  # port de l'API par défaut = port P2P + 1000 (5000 -> 6000)
MAX_REQUEST_LINE_BYTES = 8 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_PAGE = 20
MAX_PAGE = 200

STATUS_TEXT = {
    200: "OK",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}

ROUTES = (
    "GET /status",
    "GET /blocks?limit=20&before=<hauteur>",
    "GET /blocks/<index|hash>",
    "GET /transactions/<hash>",
    "POST /transactions  (transaction signée, format JSON du codec)",
    "GET /accounts/<adresse>",
    "GET /accounts/<adresse>/transactions?limit=20&offset=0",
    "GET /mempool",
    "GET /peers",
)


class ApiError(Exception):
    """Réponse d'erreur HTTP à renvoyer telle quelle."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    body: bytes


@dataclass(slots=True)
class Response:
    status: int
    payload: object = None  # dict/list -> JSON ; bytes/str -> tel quel ; None -> sans corps
    content_type: str = "application/json; charset=utf-8"


class ApiServer:
    """Sert l'API HTTP d'un NodeServer (même boucle asyncio, aucun fil supplémentaire)."""

    def __init__(
        self,
        server: NodeServer,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.server = server
        self.host = host
        self.port = port  # 0 = port libre choisi par le système, connu après start()
        self._log = log if log is not None else (lambda text: None)
        self._http: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def node(self):
        return self.server.node

    @property
    def address(self) -> str:
        return format_address(self.host, self.port)

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ALL_INTERFACES else self.host
        return f"http://{format_address(host, self.port)}/"

    @property
    def running(self) -> bool:
        return self._http is not None

    async def start(self) -> None:
        self._http = await asyncio.start_server(self._accept, self.host, self.port, limit=MAX_HEADER_BYTES)
        self.port = self._http.sockets[0].getsockname()[1]
        if self.host in ALL_INTERFACES:
            lan = ", ".join(f"http://{format_address(ip, self.port)}/" for ip in local_ip_addresses()) or "aucune adresse réseau détectée"
            self._log(f"API HTTP sur toutes les interfaces, port {self.port} (réseau local : {lan})")
        else:
            self._log(f"API HTTP sur {self.url} (GET /status, /blocks, /accounts/<adresse>, /mempool, /peers ; POST /transactions)")

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http is not None:
            self._http.close()
            await self._http.wait_closed()
            self._http = None

    # ------------------------------------------------------------------ HTTP

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request: Request | None = None
        try:
            try:
                request = await asyncio.wait_for(_read_request(reader), REQUEST_TIMEOUT_SECONDS)
            except ApiError as error:
                response = Response(error.status, {"error": error.reason})
            except ValueError:  # ligne plus longue que la limite du lecteur
                response = Response(400, {"error": "ligne trop longue"})
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            else:
                response = await self._dispatch(request)
            await _write_response(writer, response, head_only=request is not None and request.method == "HEAD")
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, request: Request) -> Response:
        try:
            return await self._route(request)
        except ApiError as error:
            return Response(error.status, {"error": error.reason})
        except Exception as error:  # un bug dans une route ne doit jamais faire tomber le nœud
            self._log(f"API : erreur interne sur {request.method} {request.path} : {error!r}")
            return Response(500, {"error": "erreur interne"})

    async def _route(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(204)
        if request.method not in ("GET", "HEAD", "POST"):
            raise ApiError(405, f"méthode {request.method} non prise en charge")
        segments = [unquote(part) for part in request.path.split("/") if part]
        reading = request.method in ("GET", "HEAD")
        if not segments:
            return self._get_only(reading, self._index)
        head, rest = segments[0], segments[1:]
        if head == "status" and not rest:
            return self._get_only(reading, self._status)
        if head == "blocks" and not rest:
            return self._get_only(reading, lambda: self._blocks(request.query))
        if head == "blocks" and len(rest) == 1:
            return self._get_only(reading, lambda: self._block(rest[0]))
        if head == "transactions" and not rest:
            if reading:
                raise ApiError(405, "POST /transactions attend une transaction signée ; GET /transactions/<hash> lit une transaction")
            return await self._post_transaction(request.body)
        if head == "transactions" and len(rest) == 1:
            return self._get_only(reading, lambda: self._transaction(rest[0]))
        if head == "accounts" and len(rest) == 1:
            return self._get_only(reading, lambda: self._account(rest[0]))
        if head == "accounts" and len(rest) == 2 and rest[1] == "transactions":
            return self._get_only(reading, lambda: self._account_transactions(rest[0], request.query))
        if head == "mempool" and not rest:
            return self._get_only(reading, self._mempool)
        if head == "peers" and not rest:
            return self._get_only(reading, self._peers)
        raise ApiError(404, f"route inconnue : {request.path}")

    @staticmethod
    def _get_only(reading: bool, handler: Callable[[], object]) -> Response:
        if not reading:
            raise ApiError(405, "cette route se lit (GET)")
        return Response(200, handler())

    # ---------------------------------------------------------------- routes

    def _index(self) -> dict:
        return {"name": "powchain", "api_version": API_VERSION, "routes": list(ROUTES)}

    def _status(self) -> dict:
        node = self.node
        return {
            "node_id": node.node_id,
            "protocol_version": PROTOCOL_VERSION,
            "api_version": API_VERSION,
            "height": node.height,
            "work": node.work,
            "tip_hash": node.tip.hash,
            "tip_timestamp": node.tip.timestamp,
            "difficulty": node.tip.difficulty,
            "peers": len(node.peers),
            "inbound": node.inbound_connections,
            "outbound": node.outbound_connections,
            "known_addresses": len(node.known_addresses),
            "banned_hosts": len(node.banned_hosts),
            "mempool": len(node.mempool),
            "min_fee": node.mempool.min_fee,
            "miner_address": node.miner_address,
            "mining": self.server.mining,
            "blocks_mined": self.server.blocks_mined,
            "total_supply": node.chain.state.total_supply,
            "accounts": len(node.chain.state.accounts),
            "units_per_coin": UNITS_PER_COIN,
            "coin_symbol": COIN_SYMBOL,
            "listen": self.server.address,
            "time": int(node.now()),
        }

    def _blocks(self, query: dict[str, list[str]]) -> dict:
        chain = self.node.chain
        height = chain.height
        limit = _int_param(query, "limit", DEFAULT_PAGE, 1, MAX_PAGE)
        before = _int_param(query, "before", height + 1, 1, height + 1)
        start = max(before - limit, 0)
        blocks = chain.blocks_from(start, before - start)
        return {
            "height": height,
            "before": before,
            "limit": limit,
            "blocks": [_block_summary(block) for block in reversed(blocks)],
            "next_before": start if start > 0 else None,
        }

    def _block(self, key: str) -> dict:
        chain = self.node.chain
        if key.isdigit():
            block = chain.block_at(int(key))
        elif is_valid_hash_hex(key):
            index = chain.index_of(key)
            block = None if index is None else chain.block_at(index)
        else:
            raise ApiError(400, "bloc : index entier ou hash hexadécimal de 64 caractères attendu")
        if block is None:
            raise ApiError(404, f"bloc inconnu : {key}")
        return _block_full(block, chain.height)

    def _transaction(self, key: str) -> dict:
        if not is_valid_hash_hex(key):
            raise ApiError(400, "transaction : hash hexadécimal de 64 caractères attendu")
        located = self.node.chain.find_transaction(key)
        if located is not None:
            block, transaction = located
            return _confirmed_transaction(transaction, block, self.node.height)
        for transaction in self.node.mempool.transactions:
            if transaction.hash == key:
                return _pending_transaction(transaction)
        raise ApiError(404, f"transaction inconnue : {key}")

    async def _post_transaction(self, body: bytes) -> Response:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "corps illisible : JSON attendu") from None
        try:
            transaction = transaction_from_dict(data)
        except CodecError as error:
            raise ApiError(400, str(error)) from None
        try:
            await self.server.submit_transaction(transaction)
        except (InvalidTransactionError, MempoolError) as error:
            return Response(400, {"error": str(error), "hash": transaction.hash})
        projected = self.node.mempool.projected_state(self.node.chain.state)
        return Response(
            202,
            {
                "hash": transaction.hash,
                "accepted": True,
                "pending": len(self.node.mempool),
                "projected_balance": projected.balance_of(transaction.sender),
                "projected_next_sequence": projected.next_sequence_of(transaction.sender),
            },
        )

    def _account(self, raw: str) -> dict:
        address = _parse_address(raw)
        node = self.node
        state = node.chain.state
        account = state.account(address)
        projected = node.mempool.projected_state(state)
        pending = [tx for tx in node.mempool.transactions if address in (tx.sender, tx.recipient)]
        return {
            "address": address,
            "checksummed": to_checksummed_address(address),
            "balance": account.balance,
            "next_sequence": account.next_sequence,
            "projected_balance": projected.balance_of(address),
            "projected_next_sequence": projected.next_sequence_of(address),
            "transactions": len(node.chain.transaction_hashes_of(address)),
            "pending": len(pending),
            "height": node.height,
        }

    def _account_transactions(self, raw: str, query: dict[str, list[str]]) -> dict:
        address = _parse_address(raw)
        node = self.node
        hashes = node.chain.transaction_hashes_of(address)
        limit = _int_param(query, "limit", DEFAULT_PAGE, 1, MAX_PAGE)
        offset = _int_param(query, "offset", 0, 0, max(len(hashes), 0))
        newest_first = hashes[::-1][offset : offset + limit]
        confirmed = []
        for tx_hash in newest_first:
            block, transaction = node.chain.find_transaction(tx_hash)
            confirmed.append(_confirmed_transaction(transaction, block, node.height))
        pending = [
            _pending_transaction(tx) for tx in node.mempool.transactions if address in (tx.sender, tx.recipient)
        ]
        return {
            "address": address,
            "total": len(hashes),
            "offset": offset,
            "limit": limit,
            "transactions": confirmed,
            "pending": pending,
        }

    def _mempool(self) -> dict:
        mempool = self.node.mempool
        arrival = {tx.hash: rank for rank, tx in enumerate(mempool.transactions)}
        ordered = sorted(mempool.transactions, key=lambda tx: (-tx.fee, arrival[tx.hash]))
        return {
            "count": len(mempool),
            "min_fee": mempool.min_fee,
            "total_fees": sum(tx.fee for tx in ordered),
            "transactions": [_pending_transaction(tx) for tx in ordered],
        }

    def _peers(self) -> dict:
        node = self.node
        return {
            "peers": [
                {
                    "node_id": peer.node_id,
                    "host": peer.host,
                    "listen_port": peer.listen_port,
                    "address": peer.dialed_address,
                    "outbound": peer.outbound,
                    "height": peer.height,
                    "work": peer.work,
                    "tip_hash": peer.tip_hash,
                }
                for peer in node.peers
            ],
            "known_addresses": list(node.known_addresses),
            "seed_addresses": list(node.seed_addresses),
            "own_addresses": list(node.own_addresses),
            "dialing": list(node.dialing),
            "banned_hosts": list(node.banned_hosts),
        }


# ------------------------------------------------------------------ helpers


def _block_summary(block: Block) -> dict:
    coinbase = block.coinbase
    return {
        "index": block.index,
        "hash": block.hash,
        "prev_hash": block.prev_hash,
        "timestamp": block.timestamp,
        "difficulty": block.difficulty,
        "nonce": block.nonce,
        "transactions": len(block.transactions),
        "fees": block.total_fees,
        "miner": coinbase.recipient if coinbase else None,
        "reward": coinbase.amount if coinbase else 0,
    }


def _block_full(block: Block, height: int) -> dict:
    data = block_to_dict(block)
    coinbase = block.coinbase
    data["confirmations"] = height - block.index + 1
    data["fees"] = block.total_fees
    data["miner"] = coinbase.recipient if coinbase else None
    return data


def _confirmed_transaction(transaction: Transaction, block: Block, height: int) -> dict:
    data = transaction_to_dict(transaction)
    data["status"] = "confirmed"
    data["block_index"] = block.index
    data["block_hash"] = block.hash
    data["timestamp"] = block.timestamp
    data["confirmations"] = height - block.index + 1
    return data


def _pending_transaction(transaction: Transaction) -> dict:
    data = transaction_to_dict(transaction)
    data["status"] = "pending"
    data["confirmations"] = 0
    return data


def _parse_address(raw: str) -> str:
    try:
        return normalize_address(raw, require_checksum=False)
    except ValueError as error:
        raise ApiError(400, f"adresse invalide : {error}") from None


def _int_param(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[-1])
    except ValueError:
        raise ApiError(400, f"paramètre {name} : entier attendu") from None
    if not minimum <= value <= maximum:
        raise ApiError(400, f"paramètre {name} : entre {minimum} et {maximum} attendu")
    return value


async def _read_request(reader: asyncio.StreamReader) -> Request:
    line = await reader.readline()
    if not line:
        raise ConnectionError("connexion fermée avant la requête")
    if len(line) > MAX_REQUEST_LINE_BYTES:
        raise ApiError(400, "ligne de requête trop longue")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ApiError(400, "requête HTTP mal formée")
    method, target, _ = parts
    headers: dict[str, str] = {}
    total = 0
    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("connexion fermée dans les en-têtes")
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise ApiError(400, "en-têtes trop longs")
        if line in (b"\r\n", b"\n"):
            break
        name, separator, value = line.decode("latin-1").partition(":")
        if not separator:
            raise ApiError(400, "en-tête mal formé")
        headers[name.strip().lower()] = value.strip()
    body = b""
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise ApiError(400, "Content-Length invalide") from None
        if length < 0:
            raise ApiError(400, "Content-Length invalide")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"corps de plus de {MAX_BODY_BYTES} octets")
        body = await reader.readexactly(length)
    elif headers.get("transfer-encoding"):
        raise ApiError(400, "Transfer-Encoding non pris en charge : envoyez Content-Length")
    split = urlsplit(target)
    return Request(method.upper(), split.path or "/", parse_qs(split.query), body)


async def _write_response(writer: asyncio.StreamWriter, response: Response, *, head_only: bool = False) -> None:
    payload = response.payload
    if payload is None:
        body = b""
    elif isinstance(payload, bytes):
        body = payload
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8") + b"\n"
    head = "\r\n".join(
        (
            f"HTTP/1.1 {response.status} {STATUS_TEXT.get(response.status, '')}".rstrip(),
            f"Content-Type: {response.content_type}",
            f"Content-Length: {len(body)}",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, OPTIONS",
            "Access-Control-Allow-Headers: Content-Type",
            "Access-Control-Max-Age: 86400",
            "Cache-Control: no-store",
            "Connection: close",
            "",
            "",
        )
    )
    writer.write(head.encode("latin-1") + (b"" if head_only else body))
    await writer.drain()
