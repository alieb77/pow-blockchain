"""Protocole réseau : catalogue des messages, enveloppe JSON, encodage en lignes.

Deux nœuds dialoguent sur une connexion TCP en s'envoyant des LIGNES : un
message = un objet JSON sur une seule ligne, terminé par « \\n ». C'est le
format le plus simple qui soit lisible à l'œil (utile pour comprendre ce
qui circule) et découpable sans ambiguïté (readline).

Enveloppe :  {"type": "<nom du message>", "payload": {...}}

Catalogue (PROTOCOL_VERSION = 1)
--------------------------------
    hello            premier message de CHAQUE côté d'une connexion :
                     node_id, version, listen_port (ou null si le pair
                     n'accepte pas de connexions entrantes, ex. un client
                     éphémère), height, work, tip_hash
    peers            adresses "hôte:port" d'autres nœuds connus (découverte)
    new_transaction  une transaction à relayer (gossip)
    new_block        un bloc fraîchement miné ou adopté (gossip)
    get_blocks       demande des blocs à partir de from_index
    blocks           réponse : blocs consécutifs + has_more (pagination)
    get_account      demande l'état d'une adresse (client / wallet)
    account          réponse : solde, séquence, et leurs valeurs projetées
                     (état + mempool), hauteur du nœud
    reject           refus motivé d'une transaction reçue (hash, reason)

Chaque type a un schéma de payload : clés obligatoires et type de chaque
valeur. decode_message() vérifie l'enveloppe et ce schéma ; la conversion
des dictionnaires en Transaction / Block (codec.py) et les règles métier
restent à la charge du nœud, qui ne fait jamais confiance au contenu.

Limites : MAX_MESSAGE_BYTES borne une ligne (protection mémoire) ;
MAX_BLOCKS_PER_MESSAGE borne un lot de blocs (d'où la pagination has_more).

Portée des adresses (Partie 9)
------------------------------
Une adresse « hôte:port » n'a pas le même sens partout : 127.0.0.1 désigne
« cette machine », 192.168.1.9 « cette machine du réseau local », une adresse
publique la même machine depuis Internet entier. host_scope() classe un hôte
en LOOPBACK, PRIVATE ou PUBLIC ; host_reaches() dit si une adresse a un sens
pour un pair situé à tel hôte : un nœud n'annonce jamais 127.0.0.1 à un pair
d'une autre machine, ni 192.168.x à un pair d'Internet, et ignore en
réception ce qu'un pair lui envoie de plus local que lui.
"""

import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import ProtocolError

PROTOCOL_VERSION = 1

MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16 Mio par ligne, blocs compris
MAX_BLOCKS_PER_MESSAGE = 200
MAX_PEERS_PER_MESSAGE = 64

HELLO = "hello"
PEERS = "peers"
NEW_TRANSACTION = "new_transaction"
NEW_BLOCK = "new_block"
GET_BLOCKS = "get_blocks"
BLOCKS = "blocks"
GET_ACCOUNT = "get_account"
ACCOUNT = "account"
REJECT = "reject"

# type de message -> {champ obligatoire : type(s) Python attendu(s)}
# type(None) dans un tuple signifie « null autorisé ».
_INT = (int,)
_STR = (str,)
_LIST = (list,)
_DICT = (dict,)
_BOOL = (bool,)
SCHEMAS: dict[str, dict[str, tuple]] = {
    HELLO: {
        "node_id": _STR,
        "version": _INT,
        "listen_port": (int, type(None)),
        "height": _INT,
        "work": _INT,
        "tip_hash": _STR,
    },
    PEERS: {"addresses": _LIST},
    NEW_TRANSACTION: {"transaction": _DICT},
    NEW_BLOCK: {"block": _DICT},
    GET_BLOCKS: {"from_index": _INT},
    BLOCKS: {"blocks": _LIST, "has_more": _BOOL},
    GET_ACCOUNT: {"address": _STR},
    ACCOUNT: {
        "address": _STR,
        "balance": _INT,
        "next_sequence": _INT,
        "projected_balance": _INT,
        "projected_next_sequence": _INT,
        "height": _INT,
    },
    REJECT: {"hash": _STR, "reason": _STR},
}


@dataclass(frozen=True, slots=True)
class Message:
    """Un message du protocole : son type et son contenu (dictionnaire JSON)."""

    type: str
    payload: dict = field(default_factory=dict)

    def __getitem__(self, key: str):
        return self.payload[key]


def message(type_: str, **payload) -> Message:
    """Construit un message et vérifie qu'il respecte son schéma (une erreur ici = bug local)."""
    msg = Message(type_, payload)
    validate_message(msg)
    return msg


def validate_message(msg: object) -> None:
    """Lève ProtocolError si le type est inconnu ou si le payload ne suit pas le schéma."""
    if not isinstance(msg, Message):
        raise ProtocolError(f"objet Message attendu, reçu {type(msg).__name__}")
    schema = SCHEMAS.get(msg.type)
    if schema is None:
        raise ProtocolError(f"type de message inconnu : {msg.type!r}")
    if not isinstance(msg.payload, Mapping):
        raise ProtocolError(f"{msg.type} : payload dictionnaire attendu")
    missing = [key for key in schema if key not in msg.payload]
    if missing:
        raise ProtocolError(f"{msg.type} : champ(s) manquant(s) : {', '.join(missing)}")
    unknown = [key for key in msg.payload if key not in schema]
    if unknown:
        raise ProtocolError(f"{msg.type} : champ(s) inconnu(s) : {', '.join(map(str, unknown))}")
    for key, expected_types in schema.items():
        value = msg.payload[key]
        if isinstance(value, bool) and bool not in expected_types:
            raise ProtocolError(f"{msg.type} : « {key} » ne doit pas être un booléen")
        if not isinstance(value, expected_types):
            names = " ou ".join(t.__name__ for t in expected_types)
            raise ProtocolError(f"{msg.type} : « {key} » : {names} attendu, reçu {type(value).__name__}")
    if msg.type == PEERS and len(msg.payload["addresses"]) > MAX_PEERS_PER_MESSAGE:
        raise ProtocolError(f"peers : plus de {MAX_PEERS_PER_MESSAGE} adresses")
    if msg.type == BLOCKS and len(msg.payload["blocks"]) > MAX_BLOCKS_PER_MESSAGE:
        raise ProtocolError(f"blocks : plus de {MAX_BLOCKS_PER_MESSAGE} blocs dans un seul message")


def encode_message(msg: Message) -> bytes:
    """Ligne JSON (UTF-8, terminée par \\n) prête à être écrite sur la connexion."""
    validate_message(msg)
    line = json.dumps({"type": msg.type, "payload": msg.payload}, separators=(",", ":"), ensure_ascii=False)
    raw = line.encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message {msg.type} trop volumineux : {len(raw)} octets > {MAX_MESSAGE_BYTES}")
    return raw


def decode_message(line: bytes) -> Message:
    """Message reconstruit depuis une ligne reçue, ou ProtocolError.

    Vérifie l'enveloppe et le schéma du payload ; ne décode pas les
    transactions / blocs (voir codec.py) et ne vérifie aucune règle métier.
    """
    if not isinstance(line, (bytes, bytearray)):
        raise ProtocolError(f"octets attendus, reçu {type(line).__name__}")
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message trop volumineux : {len(line)} octets > {MAX_MESSAGE_BYTES}")
    try:
        envelope = json.loads(bytes(line).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"JSON illisible : {error}") from None
    if not isinstance(envelope, dict) or set(envelope) != {"type", "payload"}:
        raise ProtocolError('enveloppe attendue : {"type": ..., "payload": {...}}')
    if not isinstance(envelope["type"], str):
        raise ProtocolError("« type » doit être une chaîne")
    if not isinstance(envelope["payload"], dict):
        raise ProtocolError("« payload » doit être un objet JSON")
    msg = Message(envelope["type"], envelope["payload"])
    validate_message(msg)
    return msg


def format_address(host: str, port: int) -> str:
    return f"{host}:{port}"


def parse_address(text: object) -> tuple[str, int]:
    """« hôte:port » -> (hôte, port), ou ProtocolError."""
    if not isinstance(text, str) or ":" not in text:
        raise ProtocolError(f"adresse « hôte:port » attendue, reçu {text!r}")
    host, _, port_text = text.rpartition(":")
    if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ProtocolError(f"adresse invalide : {text!r}")
    return host, int(port_text)


# Portée d'un hôte : jusqu'où son adresse a un sens.
LOOPBACK = "loopback"  # 127.x, ::1, localhost, 0.0.0.0 : cette machine seulement
PRIVATE = "private"  # 10/8, 172.16/12, 192.168/16, lien local, et toute plage non routable sur Internet
PUBLIC = "public"  # adresse routable sur Internet, ou nom d'hôte (supposé résoluble partout)
_SCOPE_RANK = {LOOPBACK: 0, PRIVATE: 1, PUBLIC: 2}


def host_scope(host: str) -> str:
    """LOOPBACK, PRIVATE ou PUBLIC pour un hôte « hôte » (adresse IP ou nom)."""
    if host == "localhost":
        return LOOPBACK
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return PUBLIC  # un nom d'hôte : on suppose que tout le monde peut le résoudre
    if ip.is_loopback or ip.is_unspecified:
        return LOOPBACK
    if not ip.is_global:
        return PRIVATE  # privé, lien local, réservé, documentation... : ne sort pas sur Internet
    return PUBLIC


def host_reaches(host: str, peer_host: str) -> bool:
    """Vrai si une adresse située à `host` a un sens pour un pair situé à `peer_host`.

    Règle : une adresse ne se partage qu'avec un pair au moins aussi proche
    que sa portée. 127.0.0.1 n'a de sens que pour un pair de la même machine ;
    192.168.x que pour un pair de la même machine ou du même réseau local ;
    une adresse publique en a pour tout le monde.
    """
    return _SCOPE_RANK[host_scope(host)] >= _SCOPE_RANK[host_scope(peer_host)]
