# powchain — Parties 1 à 3 : hashes, preuve de travail, clés et signatures

Blockchain Proof of Work construite pas à pas en Python (3.10 ou plus récent).
Une seule dépendance externe, `cryptography`, pour les signatures Ed25519.

> Ce projet n'est **pas** encore une blockchain décentralisée. Les Parties 1
> à 3 fournissent une chaîne minée, vérifiable en local, dont chaque
> transaction est signée par son expéditeur. Les soldes, la récompense du
> mineur, le mempool et le réseau P2P viendront dans les parties suivantes.

## Installer et lancer

```bash
pip install -r requirements.txt
```

```bash
python main.py
```

```bash
python -m unittest -v
```

## Arborescence

```
pow-blockchain/
├── main.py                  démonstration : clés, transactions signées, minage, attaques
├── requirements.txt         cryptography>=42
├── powchain/
│   ├── errors.py            hiérarchie d'exceptions
│   ├── crypto.py            SHA-256 (hashlib) et format des hashes
│   ├── money.py             montants entiers, 1 COIN = 10^8 unités
│   ├── keys.py              Ed25519 : KeyPair, verify_signature (SEUL module qui importe cryptography)
│   ├── address.py           adresse = clé publique en hexadécimal
│   ├── serialization.py     format canonique (le SEUL endroit qui définit les octets hashés)
│   ├── proof_of_work.py     cible, difficulté, règle d'ajustement, travail
│   ├── transaction.py       Transaction, hash, signature, create/sign/validate (R1-R7)
│   ├── block.py             Block, calculate_hash, Genesis, create_block, validate_block (B1-B6)
│   ├── mining.py            mine_block : recherche du nonce
│   └── chain.py             Blockchain, validate_chain / is_valid_chain, chain_work
└── tests/                   139 tests unittest (~1 s) ; helpers.py = clés de test déterministes
```

Chaque module ne dépend que de ceux situés au-dessus de lui dans cette liste.

## Conventions (à connaître avant d'écrire du code)

| Sujet | Convention |
|---|---|
| Hash | SHA-256, représenté **partout** en hexadécimal minuscule de 64 caractères |
| Montant | entier en **unités** ; `1 COIN = 100_000_000 unités` ; `0 <= amount <= MAX_MONEY` (21 M COIN) ; jamais de float |
| Clé privée | 32 octets Ed25519 ; ne quitte jamais `KeyPair`, `repr()` ne l'affiche pas |
| Adresse | la clé publique Ed25519 en hexadécimal minuscule (64 caractères) |
| Signature | Ed25519 des 32 octets du hash de la transaction, hexadécimal (128 caractères) |
| `sequence` | numéro de séquence anti-rejeu du compte expéditeur, entier `uint64`, signé ; vérifié en Partie 4 |
| `data` | chaîne UTF-8 opaque de 1024 octets max, vide autorisée ; réservée aux futures métadonnées de packs |
| Timestamp | entier, secondes Unix UTC ; strictement croissant d'un bloc au suivant |
| Difficulté | entier `>= 1` stocké dans l'en-tête et hashé ; cible `= (2^256 - 1) // difficulté` |
| Genesis | `index 0`, `timestamp 1767225600`, `prev_hash "0"*64`, `difficulty 4096`, `nonce 5237`, aucune transaction ; hash `000cb9d4…facd` figé par un test |

## Format canonique (ce qui est réellement hashé)

Le hash ne porte jamais sur la représentation mémoire d'un objet, mais sur une
suite d'octets construite par `serialization.py` :

- **Chaînes** : UTF-8, préfixées de leur longueur en octets sur 4 octets big-endian.
- **Entiers** : non signés, big-endian, largeur fixe 8 octets (`uint64`).
- **Hashes** : 32 octets bruts.
- **Listes de hashes** : nombre d'éléments sur 4 octets puis chaque hash dans l'ordre.
- **Étiquette de domaine** : chaque structure commence par une constante versionnée.

```
Transaction : "powchain/tx/v2"     | sender | recipient | amount | data | sequence
Liste de tx : "powchain/txlist/v1" | n      | hash_1 | ... | hash_n
Bloc        : "powchain/block/v2"  | index | timestamp | transactions_hash | prev_hash | difficulty | nonce
```

Ni le hash ni la signature d'une transaction n'entrent dans son propre hash.
La signature est calculée **sur** le hash : elle couvre donc tout le contenu,
y compris l'étiquette de domaine, ce qui la rend inutilisable sur une autre
chaîne ou un autre format.

## Clés et signatures

- `KeyPair.generate()` tire une clé privée du générateur aléatoire du système.
  `key.address` est la clé publique en hexadécimal.
- `create_signed_transaction(key, recipient, amount, data, sequence)` construit
  et signe. `create_transaction(...)` seule produit une transaction non signée,
  invalide tant que `sign_transaction(tx, key)` n'a pas été appelée.
- `validate_transaction` recalcule le hash puis vérifie la signature avec la
  clé publique `sender`. Sans la clé privée de l'expéditeur, aucune
  modification n'est possible, quelle que soit la puissance de minage.
- Le wrapper est vérifié contre le vecteur n°1 de la RFC 8032 dans les tests.

## Preuve de travail

Un bloc est accepté si son hash, lu comme un entier de 256 bits, est
inférieur ou égal à `cible = MAX_TARGET // difficulté`. La difficulté est le
nombre moyen d'essais. Objectif : un bloc toutes les `TARGET_BLOCK_TIME = 10`
secondes ; à chaque bloc, plus rapide que la cible → +12,5 %, plus lent →
−12,5 %, borné dans `[1, 2^64 - 1]`. `chain_work` (somme des difficultés)
sera le critère de choix entre chaînes concurrentes au moment du P2P.

```python
alice = KeyPair.generate()
tx = create_signed_transaction(alice, bob_address, parse_coin_amount("1.5"))
candidate = create_block(chain.last_block, [tx])   # nonce 0, difficulté attendue
mined = mine_block(candidate).block                # nonce trouvé, hash <= cible
chain.add_block(mined)                             # accepté seulement si toutes les règles passent
```

## Règles de validation

**Transaction** (`validate_transaction`) :

- R1 `sender` et `recipient` sont des adresses valides (clé publique hex 64 car.) ;
- R2 `amount` entier dans les bornes ;
- R3 `data` chaîne UTF-8 de taille bornée ;
- R4 non vide : `amount > 0` ou `data` non vide ;
- R5 `sequence` entier `uint64` ;
- R6 hash stocké bien formé et égal au hash recalculé ;
- R7 signature bien formée et valide pour `sender` sur le hash.

**Bloc** (`validate_block(block, prev_block)`) :

- B1 types et bornes des champs de l'en-tête ;
- B2 transactions en tuple, toutes valides (R1-R7), sans doublon ;
- B3 chaînage : `index == prev.index + 1`, `prev_hash == prev.hash`, `timestamp > prev.timestamp` ;
- B4 `difficulty` égale à la difficulté attendue par la règle d'ajustement ;
- B5 hash stocké égal au hash recalculé ;
- B6 preuve de travail : `hash <= cible`.

**Chaîne** (`validate_chain`) : non vide, premier bloc strictement égal au
Genesis canonique, puis chaque bloc validé contre le précédent.

Chaque règle existe en deux versions : `validate_*()` lève une exception qui
explique la raison, `is_valid_*()` renvoie un booléen.

## Ce qui est construit et fonctionne

- Hash déterministe des transactions et des blocs, vecteurs figés dans les tests.
- Clés Ed25519, adresses, signatures déterministes, vérification robuste
  (jamais d'exception sur une entrée mal formée).
- Genesis miné, minage déterministe avec hachage incrémental, ajustement
  automatique de la difficulté.
- Détection de falsification à tous les niveaux : hash de transaction,
  signature, hash de bloc, lien `prev_hash`, preuve de travail. Un attaquant
  sans clé privée ne peut rien forger, même avec une puissance de minage
  illimitée (section 4 de `main.py`).

## Ce qui n'est pas encore implémenté, et pourquoi plus tard

| Fonctionnalité | Pourquoi elle attend |
|---|---|
| Rejeu d'une transaction | Aujourd'hui accepté : rien ne mémorise qu'une séquence a été utilisée. La Partie 4 tiendra un état des comptes et exigera `sequence` = numéro attendu. |
| Soldes, récompense du mineur (coinbase) | Nécessitent l'état des comptes dérivé de la chaîne ; la coinbase sera une transaction spéciale exemptée de R7, unique et en première position. |
| Mempool | File des transactions en attente, filtrée par signature et solde. |
| Réseau P2P, choix de la chaîne la plus lourde | `chain_work` est prêt ; il faudra des nœuds qui échangent blocs et transactions. La borne d'avance d'horloge viendra à cette étape. |
| Wallet | Chiffrement des clés sur disque, somme de contrôle des adresses, suivi des séquences. |
| Logique des packs de jeu | Le champ `data` est déjà là ; son interprétation viendra une fois soldes en place. |
| Sécurité de la clé privée | Limite intrinsèque de toute blockchain : une clé volée permet de signer au nom de son propriétaire. |
