"""Minage : recherche du nonce qui satisfait la preuve de travail.

mine_block() prend un bloc candidat (produit par create_block : nonce = 0,
difficulté attendue, hash calculé mais presque sûrement au-dessus de la
cible) et essaie les nonces 0, 1, 2, ... jusqu'à ce que le hash de l'en-tête
soit inférieur ou égal à la cible.

Optimisation fidèle au format canonique : le nonce est le DERNIER champ de
l'en-tête. On hashe donc une seule fois le préfixe (tout sauf le nonce) puis,
à chaque essai, on copie cet état SHA-256 et on n'y ajoute que les 8 octets
du nonce. Le résultat est strictement identique à Block.calculate_hash()
(les tests le vérifient), sans re-sérialiser l'en-tête à chaque tentative.

La recherche est déterministe : le même candidat donne toujours le même
nonce. Le nombre d'essais, lui, suit une loi géométrique de moyenne
« difficulté » : parfois quelques essais, parfois plusieurs fois la moyenne,
comme une loterie.
"""

import hashlib
import time
from dataclasses import dataclass, replace

from .block import Block
from .errors import MiningError, MiningLimitError, SerializationError
from .proof_of_work import is_valid_difficulty, target_from_difficulty
from .serialization import UINT64_BYTES, UINT64_MAX, is_uint64, serialize_block_header_prefix


@dataclass(frozen=True, slots=True)
class MiningResult:
    """Bloc miné accompagné du coût de la recherche."""

    block: Block
    attempts: int
    elapsed_seconds: float

    @property
    def hash_rate(self) -> float:
        """Essais par seconde (0.0 si la durée mesurée est nulle)."""
        return self.attempts / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


def mine_block(candidate: Block, max_attempts: int | None = None) -> MiningResult:
    """Cherche, à partir de candidate.nonce, le premier nonce dont le hash respecte la cible.

    Lève MiningLimitError si max_attempts essais ne suffisent pas, MiningError
    si le candidat est inexploitable. Ne vérifie PAS les autres règles du bloc
    (chaînage, difficulté attendue...) : c'est le rôle de validate_block().
    """
    if not isinstance(candidate, Block):
        raise MiningError(f"objet Block attendu, reçu {type(candidate).__name__}")
    if not is_valid_difficulty(candidate.difficulty):
        raise MiningError(f"difficulté invalide : {candidate.difficulty!r}")
    if not is_uint64(candidate.nonce):
        raise MiningError(f"nonce de départ invalide : {candidate.nonce!r}")
    if max_attempts is not None and (not is_uint64(max_attempts) or max_attempts < 1):
        raise MiningError(f"max_attempts invalide : {max_attempts!r}")
    try:
        prefix = serialize_block_header_prefix(
            candidate.index,
            candidate.timestamp,
            candidate.calculate_transactions_hash(),
            candidate.prev_hash,
            candidate.difficulty,
        )
    except SerializationError as error:
        raise MiningError(f"candidat non sérialisable : {error}") from None

    target = target_from_difficulty(candidate.difficulty)
    prefix_state = hashlib.sha256(prefix)
    nonce = candidate.nonce
    attempts = 0
    started = time.perf_counter()
    while True:
        attempts += 1
        hasher = prefix_state.copy()
        # Identique à encode_uint64(nonce) : 8 octets big-endian non signés.
        hasher.update(nonce.to_bytes(UINT64_BYTES, byteorder="big", signed=False))
        digest = hasher.digest()
        if int.from_bytes(digest, byteorder="big") <= target:
            elapsed = time.perf_counter() - started
            mined = replace(candidate, nonce=nonce, hash=digest.hex())
            return MiningResult(block=mined, attempts=attempts, elapsed_seconds=elapsed)
        if max_attempts is not None and attempts >= max_attempts:
            raise MiningLimitError(
                f"aucun nonce trouvé en {attempts} essais (difficulté {candidate.difficulty})"
            )
        nonce += 1
        if nonce > UINT64_MAX:
            raise MiningError("espace des nonces épuisé : changer le timestamp ou les transactions")
