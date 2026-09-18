# powchain — Parties 1 à 6 : hashes, preuve de travail, signatures, soldes, mempool, réseau P2P, disque

Blockchain Proof of Work construite pas à pas en Python (3.10 ou plus récent).
Une seule dépendance externe, `cryptography`, pour les signatures Ed25519.
Le réseau et le stockage n'utilisent que la bibliothèque standard (`asyncio`, `json`).

> Depuis la Partie 5, plusieurs nœuds peuvent tourner dans plusieurs
> terminaux (ou machines d'un même réseau local), s'échanger transactions et
> blocs, se rattraper et résoudre les forks par la règle du plus grand
> travail cumulé. Depuis la Partie 6, un nœud relancé reprend sa chaîne, son
> mempool et ses pairs depuis son dossier de données, après revalidation
> complète. Le wallet, les frais et les packs de jeu viendront ensuite.

## Installer et lancer

```bash
pip install -r requirements.txt
```

Démonstration complète (Parties 1 à 6 : réseau simulé, vraies sockets, disque) :

```bash
python main.py
```

Tests (336, environ 7 s ; une quinzaine utilisent de vraies sockets locales) :

```bash
python -m unittest -v
```

### Faire tourner des nœuds dans plusieurs terminaux

Terminal 1 : générer une clé, puis lancer un nœud qui mine pour elle.

```bash
python -m powchain keygen
```

```bash
python -m powchain node --port 5000 --mine <adresse affichée par keygen>
```

Terminal 2 : un second nœud qui rejoint le premier (il rattrape la chaîne, puis reçoit chaque nouveau bloc).

```bash
python -m powchain node --port 5001 --peers 127.0.0.1:5000
```

Terminal 3 : interroger un nœud, puis payer depuis la clé du mineur (la transaction voyage jusqu'au mineur, qui l'inclut dans un bloc).

```bash
python -m powchain status --node 127.0.0.1:5001 --address <adresse>
```

```bash
python -m powchain send --node 127.0.0.1:5001 --seed-hex <graine de keygen> --to <adresse destinataire> --amount 2.5
```

`send` demande au nœud la prochaine séquence du compte, signe, envoie, puis
affiche le solde projeté ; un refus (solde insuffisant, rejeu) est motivé.
Un troisième nœud lancé avec `--peers 127.0.0.1:5001` découvrira le premier
tout seul (échange d'adresses).

Chaque nœud écrit dans `data/node-<port>/` (changer avec `--data-dir`,
désactiver avec `--memory`). Arrêtez-le (Ctrl+C, ou même brutalement) et
relancez-le **sans** `--peers` : il recharge sa chaîne, revalide tout, reprend
ses transactions en attente et se reconnecte aux adresses qu'il connaissait.

## Arborescence

```
pow-blockchain/
├── main.py                  démonstration : clés, coinbase, mempool, attaques, émission, réseau P2P, disque
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
│   ├── mempool.py           Mempool : file d'attente validée contre l'état projeté (M1-M4), resync
│   ├── chain.py             Blockchain (blocs + état + index par hash), validate_chain, chain_work
│   ├── codec.py             Transaction / Block <-> dictionnaires JSON (réseau et disque)
│   ├── protocol.py          catalogue des messages, enveloppe JSON, une ligne par message
│   ├── node.py              Node : logique P2P PURE (gossip, synchronisation, forks, règle N1)
│   ├── network.py           NodeServer : sockets TCP asyncio + minage par tranches
│   ├── simulation.py        SimulatedNetwork / FakeClock : plusieurs nœuds en mémoire, déterministe
│   ├── storage.py           NodeStorage : dossier de données (blocks.jsonl, mempool.jsonl, peers.json)
│   └── __main__.py          ligne de commande : node, keygen, status, send
└── tests/                   336 tests unittest ; helpers.py = clés de test déterministes
```

Chaque module ne dépend que de ceux situés au-dessus de lui dans cette liste.
`node.py` ne touche ni socket ni disque : il reçoit des messages et renvoie
des actions (`Send`, `Connect`, `Disconnect`) que `network.py` (vraies
sockets) ou `simulation.py` (en mémoire) exécutent, et il signale ses
changements durables par des événements (`BlockAdded`, `ChainReorganized`,
`TransactionAdded`, `AddressLearned`) auxquels `storage.py` s'abonne. C'est ce
qui permet de tester forks, réorganisations et persistance de façon déterministe.

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
| Timestamp | entier, secondes Unix UTC ; strictement croissant d'un bloc au suivant ; au plus 120 s dans le futur pour être relayé (règle N1) |
| Difficulté | entier `>= 1` stocké dans l'en-tête et hashé ; cible `= (2^256 - 1) // difficulté` |
| Genesis | `index 0`, `timestamp 1767225600`, aucune transaction (donc aucune pièce), `difficulty 4096`, `nonce 5237`, hash `000cb9d4…facd` figé par un test |
| Message réseau | un objet JSON `{"type", "payload"}` par ligne, 16 Mio max ; `PROTOCOL_VERSION = 1` |
| Adresse réseau | `hôte:port` ; un nœud écoute sur `--port`, un client éphémère annonce `listen_port: null` |
| Dossier de données | `data/node-<port>/` : `blocks.jsonl` (un bloc par ligne, Genesis compris), `mempool.jsonl`, `peers.json` (`{"version": 1, "addresses": [...]}`) |

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

Le format JSON de `codec.py` est distinct : il transporte l'objet **complet**
(signature comprise, transactions d'un bloc incluses) et sert au réseau comme
au disque. Décoder n'est pas valider : un objet décodé passe ensuite par
`validate_transaction` / `validate_block` comme n'importe quel autre.

## Cycle de vie complet (en local)

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

## Le réseau pair-à-pair (Partie 5)

### Ce qu'un nœud fait

1. **Poignée de main.** Chaque côté d'une connexion envoie `hello` en premier :
   `node_id`, version, port d'écoute, hauteur, travail cumulé, hash de la pointe.
   Un `node_id` égal au nôtre (connexion à soi-même) ou déjà connecté (doublon),
   une version inconnue, ou tout message avant `hello` : connexion fermée.
2. **Découverte.** Après `hello`, chaque nœud envoie les adresses qu'il connaît
   (`peers`) ; le destinataire se connecte aux inconnues tant qu'il a moins de
   `MAX_PEERS` (8) connexions. `--peers` ne sert donc qu'à amorcer.
3. **Gossip des transactions.** `new_transaction` : admise dans le mempool
   (M1-M4) puis renvoyée à tous les autres pairs. Une transaction déjà connue
   n'est pas relayée : la rumeur s'éteint d'elle-même. Un refus vaut un
   `reject` motivé à l'expéditeur, sans déconnexion.
4. **Gossip des blocs.** `new_block` qui prolonge notre pointe : validé
   (B1-B7, S1-S3, N1), ajouté, mempool purgé, relayé aux autres. Déjà connu :
   ignoré. Invalide : pair déconnecté (un bloc miné invalide n'est jamais un
   accident).
5. **Synchronisation.** Un bloc qui ne prolonge pas notre pointe, ou un
   `hello` annonçant plus de travail, déclenche `get_blocks` à partir de
   `min(notre hauteur + 1, hauteur du pair)`. Une seule synchronisation à la
   fois. Si le premier bloc reçu ne se greffe pas sur notre chaîne, on
   redemande plus bas (recul 1, 2, 4, 8… jusqu'au bloc 1). Les lots de 200
   blocs s'enchaînent (`has_more`) jusqu'à la pointe du pair.
6. **Évaluation de la branche.** Candidate = nos blocs jusqu'au point de
   divergence + blocs reçus. Travail cumulé (`chain_work`) pas plus grand que le
   nôtre : ignorée. Prolongement de notre pointe : ajout bloc par bloc. Sinon
   **réorganisation** : revalidation complète depuis le Genesis
   (`Blockchain.from_blocks`), remplacement, et les transactions des blocs
   abandonnés retournent au mempool (`Mempool.resync`).
7. **Minage.** Le nœud assemble un candidat sur sa pointe avec le mempool et
   mine par tranches de 4096 essais dans la boucle `asyncio`, en rendant la
   main au réseau entre deux tranches. Un bloc arrivé d'un pair rend le
   candidat périmé : il est reconstruit sur la nouvelle pointe. Le timestamp
   est rafraîchi à chaque seconde pour que la difficulté reflète le temps réel.

### Règles

- **Consensus : la chaîne au plus grand travail cumulé l'emporte**, pas la
  plus longue. À travail égal, on garde la sienne (première vue). Comme la
  difficulté suit les timestamps, une chaîne plus longue de blocs lents peut
  peser moins qu'une chaîne plus courte de blocs rapides (démo 6d).
- **N1, borne d'horloge :** un bloc daté de plus de `MAX_FUTURE_DRIFT_SECONDS`
  (120 s, soit 12 fois le temps de bloc, même ratio que Bitcoin) dans le futur
  est refusé, sans déconnexion, et pourra être accepté plus tard. Sans elle,
  un mineur daterait ses blocs dans le futur pour obtenir -12,5 % de difficulté
  à chaque bloc. C'est une règle **réseau**, pas une règle de la chaîne : elle
  dépend de l'heure à laquelle on regarde le bloc.
- Un pair qui envoie un message hors protocole, un bloc invalide, un lot de
  blocs incohérent ou une chaîne sans ancêtre commun (Genesis différent) est
  déconnecté. Un pair dont la branche est simplement plus légère ne l'est pas.

### Messages (`protocol.py`)

| Type | Payload | Rôle |
|---|---|---|
| `hello` | `node_id, version, listen_port, height, work, tip_hash` | premier message de chaque côté |
| `peers` | `addresses: ["hôte:port", …]` | découverte |
| `new_transaction` | `transaction` | gossip |
| `new_block` | `block` | gossip |
| `get_blocks` | `from_index` | synchronisation |
| `blocks` | `blocks: [...], has_more` | réponse, paginée par 200 |
| `get_account` | `address` | client / wallet |
| `account` | `balance, next_sequence, projected_balance, projected_next_sequence, height` | réponse |
| `reject` | `hash, reason` | transaction refusée |

Un nœud ne fait aucune différence entre un pair et un client éphémère
(`status`, `send`) : mêmes messages, mêmes règles, aucune confiance accordée
au contenu reçu.

## La persistance sur disque (Partie 6)

Un nœud possède un dossier de données ; `NodeStorage` le lit au démarrage et
l'écrit ensuite au fil des événements du nœud.

| Fichier | Contenu | Quand il est écrit |
|---|---|---|
| `blocks.jsonl` | toute la chaîne, un bloc par ligne, Genesis compris | une ligne ajoutée (flush + `fsync`) à chaque bloc adopté, **avant** qu'il soit relayé ; réécrit entièrement lors d'une réorganisation |
| `mempool.jsonl` | les transactions en attente | une ligne ajoutée à chaque admission ; réécrit après chaque bloc (purge) |
| `peers.json` | le carnet d'adresses `hôte:port` | réécrit à chaque adresse apprise |

Toute réécriture passe par un fichier temporaire puis `os.replace` (atomique) :
le disque ne contient jamais une chaîne à moitié écrite.

**Au chargement, on ne croit rien.** Chaque bloc relu est validé depuis le
Genesis par `Blockchain.from_blocks` (hashes, preuve de travail, signatures,
soldes) : un fichier modifié à la main est refusé avec la raison
(`StorageError`), même si la falsification recalcule les hashes et re-mine le
bloc (la signature manque toujours). Une transaction du mempool devenue
invalide (confirmée entre-temps) est écartée. Une adresse mal formée dans
`peers.json` est ignorée.

**Réparation.** Si le programme a été coupé pendant une écriture, la dernière
ligne peut être tronquée (pas de `\n` final ou JSON incomplet) : elle est
ignorée, le fichier est réécrit proprement, et le bloc manquant reviendra par
le réseau. Une ligne illisible **ailleurs** qu'à la fin est une corruption :
refus.

**Erreur d'écriture.** Une `StorageError` levée pendant l'enregistrement
(disque plein, dossier disparu) remonte dans le nœud et arrête le serveur
(`NodeServer.failed`, code de sortie 1) : mieux vaut un nœud arrêté qu'un
nœud qui croit avoir enregistré.

**Ce que le disque ne garantit pas** (démo 8e) : la *disponibilité* (un
fichier supprimé est perdu ; le nœud repart du Genesis et se resynchronise
auprès de ses pairs) et l'*authenticité* (un fichier remplacé par une autre
chaîne valide passe la validation ; seul le réseau, par la règle du plus
grand travail, remet ce nœud d'accord avec les autres). Le disque prouve
l'intégrité de ce qu'il contient, pas que c'est la bonne chaîne.

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
`resync(state, extra)` rejoue tout après une réorganisation.

**Chaîne** (`validate_chain`) : Genesis canonique, puis chaque bloc validé
contre le précédent ET appliqué à l'état. `compute_state` renvoie l'état final.

**Réseau** (`Node`) : N1 borne d'horloge ; consensus par travail cumulé.

**Disque** (`NodeStorage`) : revalidation complète au chargement ; fin de
fichier tronquée réparée ; corruption ailleurs refusée ; écritures atomiques.

## Ce qui est construit et fonctionne

- Hash déterministe, clés Ed25519, signatures, minage avec ajustement de difficulté.
- Création monétaire par coinbase, calendrier d'émission plafonné.
- État des comptes immuable rejoué depuis le Genesis : soldes et séquences.
- Mempool avec état projeté : un paiement en attente peut en financer un autre.
- Détection de toute falsification : hash, signature, preuve de travail, rejeu,
  découvert, double dépense, récompense gonflée (section 4 de `main.py`).
- Nœuds en réseau : découverte, gossip, rattrapage paginé, forks résolus par le
  travail cumulé avec réorganisation et retour des transactions au mempool,
  borne d'horloge, déconnexion des pairs fautifs (sections 6 et 7 de `main.py`,
  `python -m powchain`).
- Persistance : chaîne, mempool et carnet d'adresses survivent à un arrêt,
  même brutal ; fichier falsifié refusé, fin tronquée réparée, reconnexion
  automatique aux pairs connus (section 8 de `main.py`).

## Ce qui n'est pas encore implémenté, et pourquoi plus tard

| Fonctionnalité | Pourquoi elle attend |
|---|---|
| Wallet | Chiffrement des clés sur disque, suivi automatique des séquences, somme de contrôle des adresses. `send --seed-hex` est un pis-aller. |
| Instantané de l'état | Le chargement rejoue toute la chaîne (O(n)). Un instantané périodique des soldes rendrait le démarrage immédiat, au prix d'un second format à garder cohérent avec les blocs. |
| Frais de transaction | Sans frais, le mempool sert dans l'ordre d'arrivée ; les frais donneraient au mineur une raison d'inclure une transaction plutôt qu'une autre et protégeraient le réseau du spam. |
| Reconnexion, bannissement | Un pair perdu n'est pas rappelé ; un pair fautif est déconnecté mais peut revenir. Il manque un score de mauvaise conduite et une liste noire temporaire. |
| Synchronisation par en-têtes | Un fork profond se cherche par recul géométrique et la branche est revalidée entièrement ; Bitcoin échange d'abord des en-têtes (block locator). Acceptable tant que les chaînes sont courtes. |
| Logique des packs de jeu | Le champ `data` et le modèle de comptes sont prêts ; un pack sera un enregistrement attaché à un compte. |
| Attaque majoritaire | Limite intrinsèque de la preuve de travail : qui contrôle la majorité de la puissance de calcul peut réécrire l'historique (démo 6g). La parade est le nombre de confirmations, pas le code. |
| Sécurité de la clé privée | Limite intrinsèque : une clé volée permet de signer au nom de son propriétaire, dans la limite de son solde. |
