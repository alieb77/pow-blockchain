"""Réseau simulé en mémoire : plusieurs Node dans un seul processus, sans socket.

Deuxième « transport » de node.py, à côté de network.py. Ici, les actions
d'un nœud sont mises dans une file et livrées une par une, dans l'ordre,
jusqu'à ce qu'il ne se passe plus rien (deliver). Tout est synchrone et
déterministe : idéal pour tester les forks et les réorganisations, ou pour
une démo lisible. Chaque message traverse tout de même l'encodage JSON
(encode_message / decode_message), exactement comme sur une socket.

    clock = FakeClock(GENESIS_TIMESTAMP)
    net = SimulatedNetwork(clock)
    a = net.add(Node(node_id="A", miner_address=..., clock=clock))
    b = net.add(Node(node_id="B", clock=clock))
    net.connect("A", "B")          # poignée de main, échange d'adresses
    net.mine("A")                  # A mine sur sa pointe, le bloc se propage
    net.partition("A", "B")        # les messages entre A et B sont perdus
    net.heal("A", "B")             # ... puis passent de nouveau

Les nœuds sont désignés par leur node_id ; leur « adresse » réseau est
« sim:<node_id> », avec un port fictif unique pour la découverte.
"""

from collections import deque
from dataclasses import dataclass

from .block import Block
from .errors import ProtocolError
from .mining import mine_block
from .node import Action, Connect, Disconnect, Node, Send
from .protocol import decode_message, encode_message, format_address, parse_address

SIM_HOST = "sim"


class FakeClock:
    """Horloge contrôlée à la main ; utilisable comme paramètre clock= d'un Node."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


@dataclass(frozen=True, slots=True)
class Delivered:
    """Trace d'un message livré : de qui, à qui, quel type."""

    sender: str
    recipient: str
    type: str


class SimulatedNetwork:
    """Relie des Node en mémoire et livre leurs actions de façon déterministe."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock
        self._nodes: dict[str, Node] = {}
        self._ports: dict[str, int] = {}
        self._next_port = 10_001
        self._next_peer_id: dict[str, int] = {}
        self._links: dict[tuple[str, int], tuple[str, int]] = {}
        self._partitions: set[frozenset[str]] = set()
        self._queue: deque[tuple[str, Action]] = deque()
        self.delivered: list[Delivered] = []
        self.disconnections: list[tuple[str, str, str]] = []  # (nœud, pair, raison)

    # --------------------------------------------------------------- nœuds

    def add(self, node: Node) -> Node:
        if node.node_id in self._nodes:
            raise ValueError(f"nœud déjà présent : {node.node_id}")
        self._nodes[node.node_id] = node
        self._ports[node.node_id] = self._next_port
        node.listen_port = self._next_port
        self._next_port += 1
        self._next_peer_id[node.node_id] = 1
        return node

    def node(self, name: str) -> Node:
        return self._nodes[name]

    def address_of(self, name: str) -> str:
        return format_address(SIM_HOST, self._ports[name])

    def _name_of(self, address: str) -> str | None:
        try:
            host, port = parse_address(address)
        except ProtocolError:
            return None
        if host != SIM_HOST:
            return None
        return next((name for name, p in self._ports.items() if p == port), None)

    def peer_id(self, name: str, other: str) -> int | None:
        """Identifiant sous lequel `name` connaît `other`, ou None s'ils ne sont pas connectés."""
        for (from_name, from_id), (to_name, _) in self._links.items():
            if from_name == name and to_name == other:
                return from_id
        return None

    def connected(self, a: str, b: str) -> bool:
        return self.peer_id(a, b) is not None

    # ---------------------------------------------------------- connexions

    def connect(self, a: str, b: str, deliver: bool = True) -> None:
        """a appelle b : les deux nœuds voient une nouvelle connexion et échangent leurs hello."""
        if a == b or self.connected(a, b):
            return
        a_id, b_id = self._new_peer_id(a), self._new_peer_id(b)
        self._links[(a, a_id)] = (b, b_id)
        self._links[(b, b_id)] = (a, a_id)
        self._enqueue(a, self._nodes[a].on_connect(a_id, SIM_HOST, True, self.address_of(b)))
        self._enqueue(b, self._nodes[b].on_connect(b_id, SIM_HOST, False))
        if deliver:
            self.deliver()

    def disconnect(self, a: str, b: str, reason: str = "fermeture locale") -> None:
        a_id = self.peer_id(a, b)
        if a_id is None:
            return
        b_id = self._links[(a, a_id)][1]
        del self._links[(a, a_id)]
        del self._links[(b, b_id)]
        self._nodes[a].on_disconnect(a_id)
        self._nodes[b].on_disconnect(b_id)
        self.disconnections.append((a, b, reason))

    def partition(self, a: str, b: str) -> None:
        """Coupe le câble entre a et b : leurs messages sont perdus (connexions conservées)."""
        self._partitions.add(frozenset((a, b)))

    def heal(self, a: str, b: str) -> None:
        self._partitions.discard(frozenset((a, b)))

    # ---------------------------------------------------------- activité

    def run(self, name: str, actions: list[Action]) -> None:
        """Exécute des actions produites hors message (submit_transaction, submit_block...)."""
        self._enqueue(name, actions)
        self.deliver()

    def mine(self, name: str, coinbase_data: str = "") -> Block:
        """Le nœud mine un bloc sur sa pointe, l'adopte et le diffuse ; retourne le bloc."""
        node = self._nodes[name]
        candidate = node.build_candidate(coinbase_data)
        if candidate is None:
            raise ValueError(f"le nœud {name} n'a pas d'adresse de minage")
        block = mine_block(candidate).block
        self.run(name, node.submit_block(block))
        return block

    def deliver(self, limit: int = 100_000) -> int:
        """Livre tout ce qui est en attente jusqu'au calme plat ; retourne le nombre de messages livrés."""
        count = 0
        while self._queue:
            if count >= limit:
                raise RuntimeError(f"plus de {limit} messages : le réseau ne converge pas")
            name, action = self._queue.popleft()
            count += self._execute(name, action)
        return count

    # ---------------------------------------------------------- interne

    def _new_peer_id(self, name: str) -> int:
        peer_id = self._next_peer_id[name]
        self._next_peer_id[name] = peer_id + 1
        return peer_id

    def _enqueue(self, name: str, actions: list[Action]) -> None:
        for action in actions:
            self._queue.append((name, action))

    def _execute(self, name: str, action: Action) -> int:
        if isinstance(action, Send):
            link = self._links.get((name, action.peer_id))
            if link is None:
                return 0  # connexion déjà fermée
            to_name, to_id = link
            if frozenset((name, to_name)) in self._partitions:
                return 0  # câble coupé : message perdu
            msg = decode_message(encode_message(action.message))  # même chemin que sur une socket
            self.delivered.append(Delivered(name, to_name, msg.type))
            self._enqueue(to_name, self._nodes[to_name].on_message(to_id, msg))
            return 1
        if isinstance(action, Connect):
            target = self._name_of(action.address)
            if target is not None and target != name and not self.connected(name, target):
                if frozenset((name, target)) not in self._partitions:
                    self.connect(name, target, deliver=False)
            return 0
        if isinstance(action, Disconnect):
            link = self._links.get((name, action.peer_id))
            if link is not None:
                self.disconnect(name, link[0], action.reason)
            return 0
        raise TypeError(f"action inconnue : {action!r}")
