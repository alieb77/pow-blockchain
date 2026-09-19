"""FLOUS — lanceur « clic-and-run » : démarre un nœud FLOUS et ouvre l'explorateur.

But : permettre à un non-développeur de PARTICIPER au réseau FLOUS sans rien
installer ni taper de commande. On double-clique l'exécutable ; il :

  * rejoint le réseau FLOUS (amorces publiques par défaut : voir powchain.__main__),
  * sert l'explorateur de blocs (page web façon Tetris) sur http://127.0.0.1:8000/,
  * ouvre cette page dans le navigateur,
  * relaie transactions et blocs, valide la chaîne, et la persiste.

Par défaut le nœud n'écoute QUE en sortie (pas de connexions entrantes) : aucune
demande de pare-feu, aucune exposition. Pour devenir un nœud public accessible
depuis l'extérieur, lancez avec l'option « --public » (voir le README). La
variable d'environnement FLOUS_NO_BROWSER=1 empêche l'ouverture du navigateur
(pratique sur un serveur). Toutes les options de « python -m powchain node »
restent acceptées ici (ex. --mine,
--mine-label, --public, --port). Fermez la fenêtre pour arrêter le nœud.

Depuis les sources :  python launcher.py [options du nœud]
En exécutable       :  FLOUS.exe [options du nœud]        (double-clic = sans option)
"""

import os
import sys
import threading
import time
import webbrowser

from powchain.api import API_PORT_OFFSET
from powchain.money import COIN_NAME, COIN_SYMBOL

DEFAULT_PORT = 5000


def _frozen() -> bool:
    """Vrai si on tourne depuis un exécutable PyInstaller (et non depuis les sources)."""
    return getattr(sys, "frozen", False)


def _open_browser_later(url: str, delay: float = 2.0) -> None:
    """Ouvre le navigateur sur l'explorateur, après un court délai (le temps que l'API démarre)."""

    def go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def _resolve_port(argv: list[str]) -> int:
    """Port P2P demandé (--port N) ou défaut, pour deviner l'URL de l'explorateur (API = port + offset)."""
    for index, token in enumerate(argv):
        if token == "--port" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                return DEFAULT_PORT
        if token.startswith("--port="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return DEFAULT_PORT
    return DEFAULT_PORT


def main() -> None:
    from powchain.__main__ import main as node_main

    user_argv = sys.argv[1:]
    port = _resolve_port(user_argv)
    api_port = port + API_PORT_OFFSET
    explorer_url = f"http://127.0.0.1:{api_port}/"

    # Données persistées dans le profil utilisateur (~/.flous) : indépendant du
    # dossier de lancement, et stable d'une session à l'autre pour un exécutable.
    data_dir = os.path.join(os.path.expanduser("~"), ".flous", f"node-{port}")

    node_argv = ["node", "--port", str(port)]
    if not any(token in ("--data-dir", "--memory") or token.startswith("--data-dir=") for token in user_argv):
        node_argv += ["--data-dir", data_dir]
    node_argv += user_argv  # les options de l'utilisateur peuvent tout compléter/écraser (--public, --mine…)

    line = "=" * 60
    print(line)
    print(f"  {COIN_NAME} ({COIN_SYMBOL}) — nœud Proof of Work")
    print(line)
    print(f"  Explorateur de blocs : {explorer_url}")
    print("  Le nœud rejoint le réseau et relaie la chaîne.")
    print("  Fermez cette fenêtre (ou Ctrl+C) pour l'arrêter.")
    print(line, flush=True)

    if os.environ.get("FLOUS_NO_BROWSER") != "1":
        _open_browser_later(explorer_url)

    exit_code = 0
    try:
        node_main(node_argv)
    except KeyboardInterrupt:
        pass
    except SystemExit as error:
        exit_code = int(error.code) if isinstance(error.code, int) else 1
    except Exception as error:  # ne jamais disparaître en silence sur un double-clic
        print(f"\nErreur : {error!r}", file=sys.stderr)
        exit_code = 1

    # Exécutable double-cliqué : garder la fenêtre ouverte pour lire les messages.
    if _frozen() and os.environ.get("FLOUS_NO_PAUSE") != "1":
        try:
            input("\nAppuyez sur Entrée pour fermer cette fenêtre…")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
