"""Construit l'exécutable « clic-and-run » FLOUS avec PyInstaller.

    pip install pyinstaller
    python build_exe.py

Produit dist/FLOUS.exe (Windows) ou dist/FLOUS (Linux, macOS). Équivalent à
« pyinstaller flous.spec --noconfirm --clean ». Voir aussi le workflow
.github/workflows/build-exe.yml qui construit et publie l'exécutable Windows.
"""

import sys


def main() -> None:
    try:
        import PyInstaller.__main__  # noqa: WPS433 (import local volontaire)
    except ImportError:
        sys.exit("PyInstaller manquant. Installez-le d'abord :  pip install pyinstaller")
    PyInstaller.__main__.run(["flous.spec", "--noconfirm", "--clean"])
    print("\nExécutable prêt dans dist/ (FLOUS.exe sous Windows).")


if __name__ == "__main__":
    main()
