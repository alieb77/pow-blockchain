# powchain — Parties 1 à 11 : hashes, preuve de travail, signatures, soldes, mempool, réseau P2P, disque, wallet, minage vers wallet, ouverture au réseau, résilience, frais

[![CI](https://github.com/alieb77/pow-blockchain/actions/workflows/ci.yml/badge.svg)](https://github.com/alieb77/pow-blockchain/actions/workflows/ci.yml)

Blockchain Proof of Work construite pas à pas en Python (3.10 ou plus récent).
Une seule dépendance externe, `cryptography`, pour les signatures Ed25519 **et**
le chiffrement du wallet (scrypt + AES-256-GCM). Le réseau et le stockage
n'utilisent que la bibliothèque standard (`asyncio`, `json`, `ipaddress`).

> Depuis la Partie 5, plusieurs nœuds peuvent tourner dans plusieurs
> terminaux (ou machines d'un même réseau local), s'échanger transactions et
> blocs, se rattraper et résoudre les forks par la règle du plus grand
> travail cumulé. Depuis la Partie 6, un nœud relancé reprend sa chaîne, son
> mempool et ses pairs depuis son dossier de données, après revalidation
> complète. Depuis la Partie 7, un wallet garde les clés chiffrées sous un mot
> de passe et protège les adresses par une somme de contrôle : la graine
> privée ne transite plus en clair. Depuis la Partie 8, un nœud mine
> directement vers une clé du wallet (`node --mine-label`), sans mot de passe :
> les coins s'empilent dans le wallet en une commande. Depuis la Partie 9, un
> nœud s'ouvre au réseau local ou à Internet (`node --public`) : il n'annonce à
> chaque pair que les adresses qui ont un sens pour lui, apprend sa propre
> adresse, plafonne ses connexions entrantes et ferme les connexions muettes.
> Depuis la Partie 10, un nœud tient tout seul dans la durée : il rappelle
> ses pairs perdus avec un délai croissant, garde ses amorces `--peers` pour
> toujours, oublie les adresses mortes et bannit dix minutes l'hôte d'un pair
> fautif. Depuis la Partie 11, chaque paiement porte un **frais** signé,
> débité avec le montant et reversé au mineur par la coinbase ; le mempool
> sert les meilleurs payeurs d'abord, refuse ce qui paie moins que le minimum
> relayé et, s'il est plein, évince le moins payant : le spam a un coût. Les
> packs de jeu viendront ensuite.

## Installer et lancer

```bash
pip install -r requirements.txt
```

Démonstration complète (Parties 1 à 12 : réseau simulé, vraies sockets, disque, wallet, minage vers wallet, réseau ouvert, résilience, frais, API HTTP) :

```bash
python main.py
```

Tests (476, environ 15 s ; une trentaine utilisent de vraies sockets locales) :

```bash
python -m unittest -v
```

### Faire tourner des nœuds dans plusieurs terminaux

Terminal 1 : créer un wallet (mot de passe demandé, jamais affiché), puis
lancer un nœud qui mine directement vers la clé « mineur ». Le nœud lit
l'adresse **publique** dans le wallet : aucun mot de passe, aucune adresse à
recopier.

```bash
python -m powchain wallet create --label mineur
```

```bash
python -m powchain node --port 5000 --mine-label mineur
```

La ligne d'état du nœud affiche le solde du mineur qui monte à chaque bloc.
(On peut aussi cibler une adresse explicite avec `--mine <adresse>`, qui
accepte désormais la forme à somme de contrôle affichée par le wallet.)

Terminal 2 : un second nœud qui rejoint le premier (il rattrape la chaîne, puis reçoit chaque nouveau bloc).

```bash
python -m powchain node --port 5001 --peers 127.0.0.1:5000
```

Terminal 3 : voir le solde des clés du wallet, puis payer depuis « mineur »
vers une adresse (la transaction voyage jusqu'au mineur, qui l'inclut dans un bloc).

```bash
python -m powchain wallet balance --node 127.0.0.1:5001
```

```bash
python -m powchain wallet send --node 127.0.0.1:5001 --from mineur --to <adresse à somme de contrôle> --amount 2.5
```

`wallet send` déchiffre la clé le temps de signer (mot de passe demandé),
demande au nœud la prochaine séquence, envoie, puis affiche le solde projeté ;
un refus (solde insuffisant, rejeu, somme de contrôle d'adresse fausse) est
motivé. L'adresse `--to` doit être recopiée sous la forme à somme de contrôle
(majuscules comprises) affichée par `wallet address` / `wallet list` ; ajoutez
`--unchecked` pour forcer une adresse en minuscules. Un troisième nœud lancé
avec `--peers 127.0.0.1:5001` découvrira le premier tout seul.

> `keygen` et `send --seed-hex` existent encore (dépannage) mais exposent la
> graine privée : préférez le wallet.

Chaque nœud sert aussi une **API HTTP** (Partie 12) sur le port P2P + 1000,
en JSON : ouvrez <http://127.0.0.1:6000/status>, `/blocks`, `/blocks/1`,
`/accounts/<adresse>`, `/mempool`, `/peers` dans un navigateur ou avec `curl` ;
`POST /transactions` accepte une transaction déjà signée. `--api-port` la
déplace, `--no-api` la coupe, `--public` l'ouvre avec le nœud.

Chaque nœud écrit dans `data/node-<port>/` (changer avec `--data-dir`,
désactiver avec `--memory`). Arrêtez-le (Ctrl+C, ou même brutalement) et
relancez-le **sans** `--peers` : il recharge sa chaîne, revalide tout, reprend
ses transactions en attente et se reconnecte aux adresses qu'il connaissait.
Éteignez un pair : le nœud le rappelle tout seul (1, 2, 4… s, jusqu'à 5 min
d'attente) et le retrouve dès qu'il revient ; les adresses de `--peers` sont
rappelées en priorité et jamais oubliées.

### Ouvrir au réseau (deux machines)

Par défaut un nœud n'écoute que sur `127.0.0.1` : seule sa machine peut le
joindre. Sur la machine qui doit être joignable, ajoutez `--public` : le nœud
écoute sur toutes les interfaces et affiche au démarrage l'adresse à donner
aux autres (par exemple `192.168.1.9:5000`).

```bash
python -m powchain node --port 5000 --public --mine-label mineur
```

Sur un autre poste du même réseau local, amorcez avec cette adresse (le second
nœud n'a pas besoin d'être `--public` pour participer : il ouvre la connexion) :

```bash
python -m powchain node --port 5000 --peers 192.168.1.9:5000
```

Les clients aussi traversent le réseau : `wallet balance --node 192.168.1.9:5000`
ou `status --node 192.168.1.9:5000` interrogent le nœud public depuis n'importe
quel poste.

Si l'autre poste n'arrive pas à se connecter, c'est presque toujours le
pare-feu de Windows, qui bloque les connexions entrantes vers Python. Une
règle suffit (invite de commandes **administrateur**, port à adapter) :

```bash
netsh advfirewall firewall add rule name="powchain 5000" dir=in action=allow protocol=TCP localport=5000
```

Pour être joignable **depuis Internet**, il faut en plus rediriger le port TCP
5000 de la box vers cette machine (« NAT », « redirection de port », « virtual
server » selon les box) et donner aux autres l'adresse IP publique de la box.
Sans redirection, un nœud derrière une box peut appeler les autres mais
personne ne peut l'appeler : il participe quand même (il reçoit blocs et
transactions par les connexions qu'il a ouvertes), mais il n'aide pas les
nouveaux venus à entrer. Le nœud n'ouvre pas la box lui-même (pas d'UPnP).

### Rejoindre le réseau public (amorces par défaut)

Pour qu'un nouveau venu n'ait rien à configurer, powchain embarque une liste
d'**amorces par défaut** : des nœuds publics toujours allumés (« bootnodes »).
Un simple `python -m powchain node` s'y connecte automatiquement et se
synchronise sur la chaîne partagée — sans elles, chacun minerait sa propre
chaîne isolée. `--peers` ajoute d'autres adresses à ces amorces ;
`--no-default-peers` les ignore (réseau privé ou développement local).

## Arborescence

```
pow-blockchain/
├── main.py                  démonstration : clés, coinbase, mempool, attaques, émission, réseau P2P, disque, wallet, ouverture au réseau, résilience, frais
├── requirements.txt         cryptography>=42
├── powchain/
│   ├── errors.py            hiérarchie d'exceptions
│   ├── crypto.py            SHA-256 (hashlib) et format des hashes
│   ├── money.py             montants entiers (1 COIN = 10^8 unités), récompense et émission, frais minimal relayé
│   ├── keys.py              Ed25519 + chiffrement (scrypt, AES-256-GCM) : SEUL module qui importe cryptography
│   ├── address.py           adresse = clé publique en hexadécimal ; somme de contrôle d'affichage (EIP-55)
│   ├── serialization.py     format canonique (le SEUL endroit qui définit les octets hashés)
│   ├── proof_of_work.py     cible, difficulté, règle d'ajustement, travail
│   ├── transaction.py       Transaction, hash, signature, frais, coinbase (récompense + frais), règles R1-R7
│   ├── block.py             Block, Genesis, create_block (coinbase en tête), règles B1-B7
│   ├── mining.py            mine_block : recherche du nonce
│   ├── state.py             State immuable : soldes, séquences, règles S1-S3
│   ├── mempool.py           Mempool : file d'attente validée contre l'état projeté (M1-M5), service par frais, éviction, resync
│   ├── chain.py             Blockchain (blocs + état + index par hash, index transaction/adresse), validate_chain, chain_work
│   ├── codec.py             Transaction / Block <-> dictionnaires JSON (réseau et disque)
│   ├── protocol.py          catalogue des messages, enveloppe JSON, une ligne par message ; portée des adresses
│   ├── node.py              Node : logique P2P PURE (gossip, synchronisation, forks, règle N1, portées, plafond d'entrées, tick : rappels et bans)
│   ├── network.py           NodeServer : sockets TCP asyncio + minage par tranches ; délai de hello, appels avec délai, tick chaque seconde
│   ├── api.py               ApiServer : API HTTP JSON (serveur HTTP/1.1 minimal sur asyncio, CORS) : chaîne, comptes, mempool, pairs, POST transaction
│   ├── simulation.py        SimulatedNetwork / FakeClock : plusieurs nœuds en mémoire (hôte « sim-<id> » chacun), déterministe
│   ├── storage.py           NodeStorage : dossier de données (blocks.jsonl, mempool.jsonl, peers.json)
│   ├── wallet.py            Wallet : clés chiffrées dans wallet.json (compose keys.py, n'importe pas cryptography)
│   └── __main__.py          ligne de commande : node (--public, --mine-label, --min-fee, --api-port, --no-api), wallet, status ; keygen/send en legacy
└── tests/                   476 tests unittest ; helpers.py = clés de test déterministes
```

Chaque module ne dépend que de ceux situés au-dessus de lui dans cette liste.
`node.py` ne touche ni socket ni disque : il reçoit des messages et renvoie
des actions (`Send`, `Connect`, `Disconnect`) que `network.py` (vraies
sockets) ou `simulation.py` (en mémoire) exécutent, et il signale ses
changements durables par des événements (`BlockAdded`, `ChainReorganized`,
`TransactionAdded`, `AddressLearned`, `AddressForgotten`) auxquels `storage.py` s'abonne. C'est ce
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
| Coinbase | transaction dont `sender` est l'adresse réservée `"0"*64`, non signée, `fee = 0`, `sequence` = hauteur du bloc, `amount` = `block_reward(hauteur)` + somme des frais du bloc |
| Récompense | 50 COIN, divisée par deux tous les 210 000 blocs ; total < 21 M COIN ; les frais ne créent rien, ils changent de main |
| `fee` | frais en unités, signé avec le reste, débité de l'expéditeur **en plus** de `amount`, reversé au mineur par la coinbase ; la chaîne accepte `fee >= 0`, le mempool exige `fee >= MIN_RELAY_FEE` (0.0001 COIN, `node --min-fee`) |
| `data` | chaîne UTF-8 opaque de 1024 octets max ; réservée aux futures métadonnées de packs |
| Timestamp | entier, secondes Unix UTC ; strictement croissant d'un bloc au suivant ; au plus 120 s dans le futur pour être relayé (règle N1) |
| Difficulté | entier `>= 1` stocké dans l'en-tête et hashé ; cible `= (2^256 - 1) // difficulté` |
| Genesis | `index 0`, `timestamp 1767225600`, aucune transaction (donc aucune pièce), `difficulty 4096`, `nonce 5237`, hash `000cb9d4…facd` figé par un test |
| Message réseau | un objet JSON `{"type", "payload"}` par ligne, 16 Mio max ; `PROTOCOL_VERSION = 2` (transactions avec frais) |
| Adresse réseau | `hôte:port` ; un nœud écoute sur `--port` (`127.0.0.1` par défaut, toutes les interfaces avec `--public`), un client éphémère annonce `listen_port: null` |
| Portée d'un hôte | `loopback` (127.x, `localhost`, `::1`, `0.0.0.0`) < `private` (toute adresse non routable sur Internet : 10/8, 172.16/12, 192.168/16, lien local…) < `public` (le reste, et les noms d'hôte) ; une adresse n'est annoncée qu'à un pair au moins aussi proche que sa portée |
| Dossier de données | `data/node-<port>/` : `blocks.jsonl` (un bloc par ligne, Genesis compris), `mempool.jsonl`, `peers.json` (`{"version": 1, "addresses": [...]}`) |
| API HTTP | `http://<hôte>:<port P2P + 1000>/` (`--api-port`, `--no-api`), même interface que le nœud (`--public` l'ouvre) ; JSON, montants en unités, `Access-Control-Allow-Origin: *`, une requête par connexion |

## Format canonique (ce qui est réellement hashé)

- **Chaînes** : UTF-8, préfixées de leur longueur en octets sur 4 octets big-endian.
- **Entiers** : non signés, big-endian, largeur fixe 8 octets (`uint64`).
- **Hashes** : 32 octets bruts. **Listes de hashes** : nombre d'éléments puis chaque hash.
- **Étiquette de domaine** : chaque structure commence par une constante versionnée.

```
Transaction : "powchain/tx/v3"     | sender | recipient | amount | fee | data | sequence
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

# 2. Il paie alice (plus le frais minimal) ; la transaction attend dans le mempool, validée contre l'état projeté.
pool.add(create_signed_transaction(miner, alice.address, parse_coin_amount("10"), sequence=0, fee=MIN_RELAY_FEE), chain.state)

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
   (`peers`), en ne gardant que celles qui ont un sens pour ce pair (portée,
   Partie 9) ; le destinataire se connecte aux inconnues tant qu'il a moins de
   `MAX_PEERS` (8) connexions **sortantes** (les entrantes ont leur propre
   plafond, `MAX_INBOUND` = 32). `--peers` ne sert donc qu'à amorcer.
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

### Ouverture au réseau (Partie 9)

Tout ce qui suit est de la logique pure dans `node.py` (testée sans socket),
sauf le délai de hello qui vit dans `network.py`. Aucun changement de format :
mêmes messages, même `PROTOCOL_VERSION`, mêmes blocs.

- **Portée des adresses.** `127.0.0.1:5001` veut dire « cette machine » ;
  envoyée à un pair d'une autre machine, elle désigne *sa* machine à lui.
  `host_scope()` classe chaque hôte en `loopback` < `private` < `public`, et un
  nœud **n'annonce une adresse qu'à un pair au moins aussi proche que sa
  portée** : une adresse locale reste sur la machine, une adresse 192.168.x
  reste sur le réseau local, une adresse publique va partout. En réception,
  la règle est la même dans l'autre sens : une adresse plus locale que le pair
  qui l'envoie est ignorée (comptée dans `stats["addresses_out_of_reach"]`).
  Un pair est mémorisé sous l'adresse *vue de chez nous* (son IP source + son
  `listen_port`) : un pair du réseau local est donc connu par son IP réseau.
- **Adresse propre.** Un nœud ne connaît pas son adresse publique a priori.
  Quand un pair lui renvoie une adresse qui est en fait la sienne, il l'appelle,
  reçoit un `hello` portant son propre `node_id`, et en déduit : « cette adresse,
  c'est moi ». Il la range dans `own_addresses`, la retire du carnet
  (`AddressForgotten`, le fichier `peers.json` est réécrit), ne la rappelle
  plus jamais et peut désormais l'annoncer aux pairs pour qui elle a un sens.
- **Plafond d'entrées.** `MAX_INBOUND` (32) connexions entrantes au plus ; la
  suivante est fermée avant même le `hello`. Les connexions **sortantes**
  (`MAX_PEERS`, 8) se comptent à part : un pair qui remplit nos entrées ne nous
  empêche pas de choisir nos sorties.
- **Délai de hello.** Une connexion qui n'a rien dit au bout de
  `HELLO_TIMEOUT_SECONDS` (10 s) est fermée : elle ne garde pas une entrée
  occupée pour rien.
- **Écoute.** `--public` = `0.0.0.0` (IPv4, toutes les interfaces) ; le nœud
  affiche ses adresses réseau (`local_ip_addresses()`) au démarrage. Sans
  `--public`, rien ne change par rapport aux parties précédentes.

Ce que cette partie ne fait **pas** : ouvrir la box (pas d'UPnP ni de
traversée de NAT). Le rappel des pairs et les bans sont la Partie 10.

### Résilience (Partie 10)

Toujours de la logique pure dans `node.py`, pilotée par une seule nouveauté
côté transport : `network.py` appelle `Node.tick()` chaque seconde et lui
rapporte l'issue de chaque appel (`on_dial_failed`, `on_dial_skipped`, ou
`on_connect` en cas de succès). Aucun changement de format ni de fichier.

- **Rappel des pairs.** À chaque tick, tant qu'il a moins de `MAX_PEERS` (8)
  sorties, le nœud appelle des adresses de son carnet : les **amorces**
  (`--peers`) d'abord, puis les plus récemment vues ; jamais une adresse déjà
  connectée, en cours d'appel, bannie, ou la sienne. Un appel raté, ou une
  connexion perdue, repousse le prochain essai de 1, 2, 4… secondes
  (`RECONNECT_MAX_DELAY` = 5 min au plus) ; une poignée de main réussie remet
  le compteur à zéro. Un appel dont le transport ne dit rien pendant
  `DIAL_GRACE_SECONDS` (30 s) est tenu pour raté.
- **Oubli.** `MAX_DIAL_FAILURES` (12) échecs d'affilée, soit environ 25 min
  d'essais, font sortir l'adresse du carnet (`AddressForgotten`, fichier
  réécrit). Les amorces ne sont jamais oubliées ni évincées du carnet, même
  plein : ce sont les points d'entrée de confiance. Le compteur d'échecs vit
  en mémoire : au redémarrage, tout le carnet a de nouveau sa chance.
- **Bannissement.** Un pair **fautif** (message hors protocole ou mal formé,
  ligne illisible ou trop longue, bloc invalide, lot incohérent, Genesis
  différent) est déconnecté et son **hôte** banni `BAN_SECONDS` (10 min) :
  ses connexions sont refusées avant `hello`, son adresse n'est pas rappelée,
  et le ban se lève seul au tick. Les désagréments bénins (doublon, version
  inconnue, connexion à soi-même, plafond d'entrées, branche plus légère) ne
  bannissent pas. **La boucle locale n'est jamais bannie** : `127.0.0.1`,
  ce sont vos propres processus (autres nœuds, wallet, tests, démos).
- **Réseau simulé.** Chaque nœud simulé a désormais son propre hôte
  (`sim-<id>`) : un `Connect` vers un nœud absent ou partitionné échoue
  vraiment (`on_dial_failed`), `net.tick("A")` fait un tour d'entretien, et
  un ban frappe un seul nœud, comme avec de vraies adresses.

Ce que cette partie ne fait **pas** : un ban par hôte se contourne en
changeant d'adresse IP et frappe tous les nœuds derrière une même box ; rien
ne protège d'un réseau qui ment d'une seule voix (éclipse) si toutes les
sorties tombent chez des complices, sinon des amorces de confiance.

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

## Le wallet (Partie 7)

Jusqu'ici la clé privée était un pis-aller : `keygen` l'affichait en clair et
`send --seed-hex` la repassait en argument. Un wallet (`wallet.py`) garde une
ou plusieurs clés dans un fichier `wallet.json`, **chiffrées**, et ne les
déverrouille que le temps de signer.

**Purement côté client et additif.** Le wallet ne touche ni au format des
transactions, ni aux blocs, ni au Genesis : rien à re-figer. `wallet.py` ne
fait que composer les primitives de `keys.py` — l'invariant « seul `keys.py`
importe `cryptography` » tient toujours.

### Chiffrement

Un wallet a **un** mot de passe et **un** sel aléatoire. Le mot de passe et le
sel donnent, par **scrypt** (fonction de dérivation coûteuse en mémoire : elle
ralentit chaque essai d'attaque par dictionnaire), une clé maître de 32 octets.
Chaque graine privée est chiffrée sous cette clé par **AES-256-GCM**, un
chiffrement *authentifié* : au déchiffrement, un mot de passe erroné ou un
octet modifié fait échouer la vérification du tag au lieu de rendre une graine
fausse. L'adresse publique est liée au chiffré (donnée associée) : on ne peut
pas recoller un chiffré sous une autre adresse.

```
wallet.json = { version, kdf: {name: scrypt, n, r, p, salt}, cipher: AES-256-GCM,
                keys: [ {label, address, nonce, ciphertext}, ... ] }
```

Le fichier est écrit de façon atomique (tmp + `os.replace`), comme le stockage
du nœud. Les paramètres scrypt sont stockés avec le sel : on pourra les
durcir plus tard sans casser les anciens fichiers.

### Somme de contrôle d'adresse

L'adresse inscrite dans une transaction reste le hex brut de 64 caractères
(c'est ce qui est signé). Mais du hex brut ne détecte aucune faute de frappe.
Le wallet ajoute donc une **couche d'affichage** inspirée d'EIP-55 (adaptée à
SHA-256) : mêmes 64 caractères, mais certains chiffres-lettres passent en
**majuscule** selon un hash de l'adresse. La casse encode ainsi une somme de
contrôle. `wallet send` exige la forme à casse mixte (celle qu'affichent
`wallet address` / `wallet list`) et refuse une adresse dont la casse ne
correspond pas, sauf `--unchecked`.

### Commandes

| Commande | Rôle | Mot de passe |
|---|---|---|
| `wallet create [--label NOM]` | crée le wallet et une première clé | demandé (deux fois) |
| `wallet generate [--label NOM]` | ajoute une nouvelle clé aléatoire | demandé |
| `wallet import --seed-hex H [--label NOM]` | ajoute une clé existante (sauvegarde) | demandé |
| `wallet list` / `wallet address --label NOM` | adresses à somme de contrôle | non |
| `wallet balance --node N [--label NOM]` | interroge un nœud | non |
| `wallet send --from NOM --to ADRESSE --amount A [--fee F]` | signe et diffuse un paiement (frais : minimum relayé par défaut) | demandé |
| `wallet export --label NOM` | révèle la graine (pour la sauvegarder) | demandé |

Le mot de passe est lu par `getpass` (jamais affiché, jamais dans `argv`) ; en
contexte non interactif, la variable `POWCHAIN_WALLET_PASSWORD` sert
d'échappatoire.

**Ce que le wallet ne protège pas** (démo 9e) : un mot de passe *faible* reste
cassable hors ligne (scrypt ralentit, n'empêche pas) ; un mot de passe *perdu*
rend les fonds inaccessibles pour toujours — il n'y a aucune récupération, d'où
`wallet export` pour sauvegarder la graine à part ; et la somme de contrôle
attrape les fautes de frappe, pas l'envoi à une adresse valide mais qui
n'appartient à personne. Le wallet protège la clé **au repos**, pas un poste
déjà compromis (enregistreur de frappe).

## Miner vers son wallet (Partie 8)

`python -m powchain node --port 5000 --mine-label mineur` fait miner le nœud
directement vers la clé « mineur » du wallet (`--wallet`, défaut `wallet.json`).

Point clé : **miner vers une adresse n'exige que la clé publique** — on mine
*vers* une adresse, on ne signe rien avec. Le nœud n'a donc besoin que de
l'**adresse** de la clé, qu'il lit en clair dans le wallet : **aucun mot de
passe**, la graine chiffrée n'est jamais touchée. Le pont wallet ↔ nœud vit
entièrement dans la CLI (`resolve_miner_address`) ; `node.py` continue de
recevoir une simple adresse et sa couche reste inchangée. La ligne d'état du
nœud affiche le solde du mineur, qui monte à chaque bloc.

`--mine <adresse>` reste possible et accepte maintenant aussi la forme à somme
de contrôle affichée par le wallet (plus besoin de la re-taper en minuscules).

Limite honnête (démo 10c) : le nœud n'a lu que la partie **publique** du
wallet ; **dépenser** les coins minés demande toujours le mot de passe (pour
signer). Un nœud public qui mine pour vous ne peut donc pas toucher à votre solde.

## Frais de transaction (Partie 11)

Chaque transaction porte un champ `fee` (unités), **signé** avec le reste :
personne ne peut changer ce qu'un expéditeur a consenti à payer. L'expéditeur
est débité de `amount + fee`, le destinataire reçoit `amount`, et les frais
reviennent au mineur du bloc **par la coinbase**, dont le montant vaut
`block_reward(hauteur) + somme des frais du bloc` (règle B7). Les frais ne
créent donc aucune monnaie : sur un bloc entier, la masse monétaire ne bouge
que de la récompense (démo 13d).

Deux niveaux, volontairement distincts :

- **Règle de la chaîne** : `fee >= 0`, et la coinbase collecte *exactement* la
  somme des frais (un bloc qui en prend plus ou moins est invalide). Un mineur
  reste libre d'inclure une transaction gratuite dans **son** bloc : il en paie
  le coût en preuve de travail, et cela ne coûte rien aux autres (démo 13a).
- **Politique de relais** (`Mempool`, règle M5) : un nœud n'attend ni ne relaie
  une transaction qui paie moins que `MIN_RELAY_FEE` (0.0001 COIN ; réglable
  par `node --min-fee`). C'est ce qui protège le réseau : inonder les mempools
  de milliers de transactions coûte des coins à l'attaquant, et un mempool
  **plein** n'accepte une nouvelle transaction que si elle paie plus que la
  moins payante en attente, qui est alors évincée avec ce qui en dépend
  (règle M4, démo 13c). Comme dans Bitcoin (`minrelaytxfee`), la politique
  peut évoluer sans changer les hashes ni casser le consensus.

Le mempool sert les transactions **par frais décroissant** (à frais égal, par
ordre d'arrivée), tout en respectant pour chaque expéditeur l'ordre de ses
séquences : sa transaction n°1 ne passe jamais avant sa n°0, même si elle paie
plus (démo 13b). Une transaction qui dépend d'un crédit encore en attente peut
donc être servie un bloc plus tard que dans l'ordre d'arrivée.

Côté CLI : `wallet send --fee 0.0005` (défaut : le minimum relayé) ; la ligne
de confirmation affiche les frais payés. Ce changement de format
(`powchain/tx/v3`, `PROTOCOL_VERSION = 2`) rend les chaînes et les nœuds des
Parties 1 à 10 incompatibles : on repart du Genesis.

Limites honnêtes : le seuil est fixe (pas d'estimation de frais selon la
charge, pas de marché des frais) ; un mineur peut toujours remplir ses propres
blocs de transactions gratuites ; les frais n'empêchent pas un pair de nous
envoyer des messages invalides en boucle (une limite de débit par pair viendra
avec l'ouverture publique) ; et la priorité aux frais est locale à chaque
mempool : deux nœuds honnêtes peuvent servir dans un ordre différent.

## API HTTP (Partie 12)

Le protocole pair-à-pair est fait pour des nœuds qui se parlent en continu ;
un navigateur, un script ou une application veut juste **poser une question et
lire la réponse**. Chaque nœud sert donc une API HTTP en JSON (`api.py`), sur
le port P2P + 1000 par défaut, sans aucune dépendance : un serveur HTTP/1.1
minimal écrit sur les flux asyncio, dans la même boucle que le nœud (pas de
fil supplémentaire, donc pas de verrou à ajouter au `Node`).

| Route | Réponse |
|---|---|
| `GET /status` | hauteur, travail, pointe, difficulté, pairs (entrants/sortants), carnet, bans, mempool et `min_fee`, mineur, blocs minés ici, masse monétaire, `units_per_coin` |
| `GET /blocks?limit=20&before=H` | résumés (index, hash, horodatage, difficulté, nb de transactions, frais, mineur, récompense) du plus récent au plus ancien ; `next_before` pour la page suivante |
| `GET /blocks/<index ou hash>` | le bloc complet, transactions comprises, plus `confirmations`, `fees`, `miner` |
| `GET /transactions/<hash>` | la transaction avec `status` (`confirmed` : bloc, horodatage, confirmations ; `pending` : dans le mempool) |
| `POST /transactions` | corps = transaction **déjà signée** au format du codec ; `202` avec le solde projeté, ou `400` motivé (`frais insuffisants`, `solde insuffisant`, champ manquant…) |
| `GET /accounts/<adresse>` | solde et séquence confirmés et projetés, nombre de transactions, en attente ; l'adresse peut être en hex brut ou à somme de contrôle |
| `GET /accounts/<adresse>/transactions?limit=20&offset=0` | historique (plus récent d'abord) et transactions en attente |
| `GET /mempool` | transactions en attente, meilleurs payeurs d'abord, total des frais |
| `GET /peers` | pairs connectés (id, hôte, port, sens, hauteur), carnet, amorces, adresses propres, bans |

Pour que ça marche, `Blockchain` tient deux index en mémoire, reconstruits
avec la chaîne (donc cohérents après une réorganisation) : hash de transaction
→ (bloc, position) et adresse → transactions la concernant. Ce sont des vues,
rien de validé n'en dépend.

Ce que l'API ne fait **jamais** : signer. Elle ne connaît aucune clé ; une
transaction reçue est traitée exactement comme si un pair l'avait envoyée
(règles R, S, M puis diffusion). Exposer l'API avec `--public` n'expose aucun
fonds. `Access-Control-Allow-Origin: *` sur toutes les réponses permet à une
page web servie d'ailleurs d'interroger un nœud ; sans cookie ni session, une
page tierce n'a rien à voler.

Limites : pas de HTTPS ni d'authentification (données publiques ; pour
Internet, un proxy devant ou `--no-api`), pas de limite de débit par client,
un bug dans une route renvoie `500` et un journal, jamais un nœud arrêté
(section 14 de `main.py`, `tests/test_api.py`).

## Règles de validation

**Transaction** (`validate_transaction`, structurelles) :

- R1 `sender` et `recipient` sont des adresses valides ;
- R2 `amount` et `fee` entiers dans les bornes ;
- R3 `data` chaîne UTF-8 de taille bornée ;
- R4 non vide : `amount > 0` ou `data` non vide (exemption pour la coinbase) ;
- R5 `sequence` entier `uint64` ;
- R6 hash stocké bien formé et égal au hash recalculé ;
- R7 transaction normale : signature valide pour `sender` ; coinbase : aucune signature et `fee = 0`.

**État** (`State.apply_transaction`, appliquées dans l'ordre du bloc) :

- S1 `sequence` égale au prochain numéro attendu du compte expéditeur (anti-rejeu, ordre) ;
- S2 solde de l'expéditeur `>= amount + fee` (interdit double dépense et découvert) ; il est débité de `amount + fee`, le destinataire crédité de `amount` ;
- S3 aucun solde ne dépasse `MAX_MONEY` ;
- coinbase : crédite le mineur (récompense + frais du bloc) sans débiter personne.

**Bloc** (`validate_block(block, prev_block)`) :

- B1 types et bornes des champs de l'en-tête ;
- B2 au plus 1000 transactions, toutes valides (R1-R7), sans doublon ;
- B3 chaînage : `index`, `prev_hash`, `timestamp > prev.timestamp` ;
- B4 `difficulty` égale à la difficulté attendue par la règle d'ajustement ;
- B5 hash stocké égal au hash recalculé ;
- B6 preuve de travail : `hash <= cible` ;
- B7 coinbase unique en première position, `sequence` = hauteur, `amount` = `block_reward(hauteur)` + somme des frais du bloc ; le Genesis n'en a pas.

**Mempool** (`Mempool.add(tx, state)`) : M1 valide et pas une coinbase ; M2 pas de
doublon ; M3 applicable sur l'état projeté (état + transactions en attente) ;
M4 capacité : plein, il n'admet qu'une transaction payant plus que la moins
payante en attente, qui est évincée avec ses dépendantes ; M5 `fee >= min_fee`
(politique de relais). `select` sert par frais décroissant, séquences d'un même
expéditeur dans l'ordre. `resync(state, extra)` rejoue tout après une
réorganisation, politique de frais comprise.

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
- Wallet : clés chiffrées par mot de passe (scrypt + AES-256-GCM), somme de
  contrôle d'adresse, paiements signés sans exposer la graine ; fichier
  falsifié détecté au déverrouillage (section 9 de `main.py`, `wallet` en CLI).
- Minage vers son wallet : `node --mine-label` résout une clé du wallet en
  adresse publique (sans mot de passe) et empile les coinbases dedans ; dépenser
  demande toujours la clé privée (section 10 de `main.py`).
- Ouverture au réseau : `node --public` écoute sur toutes les interfaces ;
  portée des adresses (rien de local ne sort de la machine, rien de privé ne
  sort du réseau local), adresse propre apprise par le `hello` à soi-même,
  plafond de connexions entrantes distinct des sorties, connexions muettes
  fermées ; vérifié sur de vraies sockets via l'IP réseau de la machine
  (section 11 de `main.py`, `tests/test_open_network.py`).
- Résilience : rappel des pairs perdus avec délai croissant (`Node.tick`,
  appelé chaque seconde par le transport), amorces `--peers` prioritaires et
  jamais oubliées, adresses mortes oubliées après 12 échecs, hôte d'un pair
  fautif banni 10 min sauf la boucle locale ; réseau simulé avec un hôte par
  nœud (section 12 de `main.py`, `tests/test_resilience.py`).
- Frais : champ `fee` signé, débité avec le montant, reversé au mineur par la
  coinbase (exactement, sinon bloc invalide) ; mempool servi par frais
  décroissant, minimum relayé configurable, éviction du moins payant quand il
  est plein (section 13 de `main.py`, `tests/test_fees.py`).
- API HTTP : chaque nœud répond en JSON (statut, blocs, transactions, comptes
  avec historique, mempool, pairs) et accepte des transactions signées ; index
  transaction/adresse dans la chaîne ; CORS ouvert ; serveur HTTP minimal sans
  dépendance (section 14 de `main.py`, `tests/test_api.py`).

## Ce qui n'est pas encore implémenté, et pourquoi plus tard

| Fonctionnalité | Pourquoi elle attend |
|---|---|
| Instantané de l'état | Le chargement rejoue toute la chaîne (O(n)). Un instantané périodique des soldes rendrait le démarrage immédiat, au prix d'un second format à garder cohérent avec les blocs. |
| HTTPS et authentification de l'API | L'API est en clair et sans compte : elle ne sert que des données publiques et des transactions déjà signées. Pour l'exposer sur Internet, un proxy HTTPS devant (ou `--no-api`) ; une limite de débit par client viendra avec l'ouverture publique. |
| Estimation des frais, limite de débit par pair | Le seuil de relais est fixe : pas de marché des frais selon la charge. Et les frais ne freinent pas un pair qui envoie des messages *invalides* en boucle (ils sont rejetés sans coût pour lui) : une limite de débit par connexion viendra avec l'ouverture publique. |
| Bans contournables, éclipse | Le ban est par hôte : un attaquant change d'IP, et des nœuds honnêtes derrière la même box sont bannis avec le fautif. Rappeler ses pairs ne protège pas d'un réseau de complices qui occuperaient toutes nos sorties (attaque par éclipse) : il faudrait diversifier les sources d'adresses et vérifier plusieurs pairs indépendants. |
| Traversée de NAT | Un nœud derrière une box n'est joignable que si le port est redirigé à la main ; sinon il reste un client sortant. Pas d'UPnP, pas de relais : hors périmètre d'une blockchain pédagogique. |
| Synchronisation par en-têtes | Un fork profond se cherche par recul géométrique et la branche est revalidée entièrement ; Bitcoin échange d'abord des en-têtes (block locator). Acceptable tant que les chaînes sont courtes. |
| Logique des packs de jeu | Le champ `data` et le modèle de comptes sont prêts ; un pack sera un enregistrement attaché à un compte. |
| Attaque majoritaire | Limite intrinsèque de la preuve de travail : qui contrôle la majorité de la puissance de calcul peut réécrire l'historique (démo 6g). La parade est le nombre de confirmations, pas le code. |
| Sécurité de la clé privée | Le wallet chiffre la clé au repos, mais c'est une limite intrinsèque : une clé volée déchiffrée (ou un mot de passe capté) permet de signer au nom de son propriétaire, dans la limite de son solde. |

## Licence

MIT — voir [LICENSE](LICENSE). Projet pédagogique : à utiliser, étudier et modifier librement.
