# powchain — Parties 1 à 4 : hashes, preuve de travail, signatures, soldes et mempool

Blockchain Proof of Work construite pas à pas en Python (3.10 ou plus récent).
Une seule dépendance externe, `cryptography`, pour les signatures Ed25519.

> Ce projet n'est **pas** encore une blockchain décentralisée. Les Parties 1
> à 4 fournissent une chaîne minée et vérifiable en local, avec des
> transactions signées, des soldes, une récompense de minage et une file
> d'attente. Le réseau P2P, le wallet et les packs de jeu viendront ensuite.

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
├── main.py                  démonstration : clés, coinbase, mempool, attaques, émission
├── requirements.txt         cryptography>=42
├── powchain/
│   ├── errors.py            hiérarchie d'exceptions
│   ├── crypto.py            SHA-256 (hashlib) et format des hashes
│   ├── money.py             montants entiers (1 COIN = 10^8 unités), récompense et émission
│   ├── keys.py              Ed25519 : KeyPair, verify_signature (SEUL module qui importe cryptography)
│   ├── address.py           adresse = clé publique en hexadécimal
│   ├── serialization.py     format canonique (le SEUL endroit qui définit les octets hashés)
│   ├── proof_of_work.py     cible, difficulté, règle d'ajustement, travail
│   ├── transaction.py       Transaction, hash, signature, coinbase, règles R1-R7
│   ├── block.py             Block, Genesis, create_block (coinbase en tête), règles B1-B7
│   ├── mining.py            mine_block : recherche du nonce
│   ├── state.py             State immuable : soldes, séquences, règles S1-S3
│   ├── mempool.py           Mempool : file d'attente validée contre l'état projeté (M1-M4)
│   └── chain.py             Blockchain (blocs + état), validate_chain, compute_state, chain_work
└── tests/                   201 tests unittest (~1 s) ; helpers.py = clés de test déterministes
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
| `sequence` | numéro de séquence du compte expéditeur : la n-ième transaction émise porte `n-1` ; anti-rejeu |
| Coinbase | transaction dont `sender` est l'adresse réservée `"0"*64`, non signée, `sequence` = hauteur du bloc, `amount` = `block_reward(hauteur)` |
| Récompense | 50 COIN, divisée par deux tous les 210 000 blocs ; total < 21 M COIN |
| `data` | chaîne UTF-8 opaque de 1024 octets max ; réservée aux futures métadonnées de packs |
| Timestamp | entier, secondes Unix UTC ; strictement croissant d'un bloc au suivant |
| Difficulté | entier `>= 1` stocké dans l'en-tête et hashé ; cible `= (2^256 - 1) // difficulté` |
| Genesis | `index 0`, `timestamp 1767225600`, aucune transaction (donc aucune pièce), `difficulty 4096`, `nonce 5237`, hash `000cb9d4…facd` figé par un test |

## Format canonique (ce qui est réellement hashé)

- **Chaînes** : UTF-8, préfixées de leur longueur en octets sur 4 octets big-endian.
- **Entiers** : non signés, big-endian, largeur fixe 8 octets (`uint64`).
- **Hashes** : 32 octets bruts. **Listes de hashes** : nombre d'éléments puis chaque hash.
- **Étiquette de domaine** : chaque structure commence par une constante versionnée.

```
Transaction : "powchain/tx/v2"     | sender | recipient | amount | data | sequence
Liste de tx : "powchain/txlist/v1" | n      | hash_1 | ... | hash_n
Bloc        : "powchain/block/v2"  | index | timestamp | transactions_hash | prev_hash | difficulty | nonce
```

Ni le hash ni la signature d'une transaction n'entrent dans son propre hash.
La signature est calculée **sur** le hash et couvre donc tout le contenu.

## Cycle de vie complet

```python
miner, alice = KeyPair.generate(), KeyPair.generate()
chain, pool = Blockchain(), Mempool()

# 1. Le mineur crée la monnaie : un bloc ne contenant que sa coinbase.
chain.add_block(mine_block(create_block(chain.last_block, [], miner.address)).block)

# 2. Il paie alice ; la transaction attend dans le mempool, validée contre l'état projeté.
pool.add(create_signed_transaction(miner, alice.address, parse_coin_amount("10"), sequence=0), chain.state)

# 3. Le prochain bloc inclut ce que le mempool sélectionne, puis le mempool est purgé.
block = mine_block(create_block(chain.last_block, pool.select(chain.state), miner.address)).block
chain.add_block(block)
pool.remove_confirmed(block, chain.state)

chain.state.balance_of(alice.address)      # 1_000_000_000 unités = 10 COIN
```

## Règles de validation

**Transaction** (`validate_transaction`, structurelles) :

- R1 `sender` et `recipient` sont des adresses valides ;
- R2 `amount` entier dans les bornes ;
- R3 `data` chaîne UTF-8 de taille bornée ;
- R4 non vide : `amount > 0` ou `data` non vide (exemption pour la coinbase) ;
- R5 `sequence` entier `uint64` ;
- R6 hash stocké bien formé et égal au hash recalculé ;
- R7 transaction normale : signature valide pour `sender` ; coinbase : aucune signature.

**État** (`State.apply_transaction`, appliquées dans l'ordre du bloc) :

- S1 `sequence` égale au prochain numéro attendu du compte expéditeur (anti-rejeu, ordre) ;
- S2 solde de l'expéditeur suffisant (interdit double dépense et découvert) ;
- S3 aucun solde ne dépasse `MAX_MONEY` ;
- coinbase : crédite le mineur sans débiter personne.

**Bloc** (`validate_block(block, prev_block)`) :

- B1 types et bornes des champs de l'en-tête ;
- B2 au plus 1000 transactions, toutes valides (R1-R7), sans doublon ;
- B3 chaînage : `index`, `prev_hash`, `timestamp > prev.timestamp` ;
- B4 `difficulty` égale à la difficulté attendue par la règle d'ajustement ;
- B5 hash stocké égal au hash recalculé ;
- B6 preuve de travail : `hash <= cible` ;
- B7 coinbase unique en première position, `sequence` = hauteur, `amount` = `block_reward(hauteur)` ; le Genesis n'en a pas.

**Mempool** (`Mempool.add(tx, state)`) : M1 valide et pas une coinbase ; M2 pas de
doublon ; M3 applicable sur l'état projeté (état + transactions en attente) ;
M4 capacité. Service dans l'ordre d'arrivée, pas encore de frais.

**Chaîne** (`validate_chain`) : Genesis canonique, puis chaque bloc validé
contre le précédent ET appliqué à l'état. `compute_state` renvoie l'état final.

## Ce qui est construit et fonctionne

- Hash déterministe, clés Ed25519, signatures, minage avec ajustement de difficulté.
- Création monétaire par coinbase, calendrier d'émission plafonné.
- État des comptes immuable rejoué depuis le Genesis : soldes et séquences.
- Mempool avec état projeté : un paiement en attente peut en financer un autre.
- Détection de toute falsification : hash, signature, preuve de travail, rejeu,
  découvert, double dépense, récompense gonflée (section 4 de `main.py`).

## Ce qui n'est pas encore implémenté, et pourquoi plus tard

| Fonctionnalité | Pourquoi elle attend |
|---|---|
| Réseau P2P, chaîne concurrente | `chain_work` est prêt ; il faut des nœuds qui échangent blocs et transactions, la règle « plus grand travail cumulé gagne » et la borne d'avance d'horloge. |
| Frais de transaction | Sans frais, le mempool sert dans l'ordre d'arrivée ; les frais donneraient au mineur une raison d'inclure une transaction plutôt qu'une autre. |
| Persistance sur disque | La chaîne vit en mémoire ; l'enregistrer permettra de redémarrer un nœud. |
| Wallet | Chiffrement des clés sur disque, suivi automatique des séquences, somme de contrôle des adresses. |
| Logique des packs de jeu | Le champ `data` et le modèle de comptes sont prêts ; un pack sera un enregistrement attaché à un compte. |
| Sécurité de la clé privée | Limite intrinsèque : une clé volée permet de signer au nom de son propriétaire, dans la limite de son solde. |
