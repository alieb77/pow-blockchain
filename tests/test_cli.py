import argparse
import os
import tempfile
import unittest
from pathlib import Path

from powchain.__main__ import build_parser, mine_address_argument, resolve_miner_address
from powchain.address import to_checksummed_address
from powchain.keys import KDF_SALT_BYTES, KeyPair
from powchain.wallet import Wallet

FAST_KDF = {"name": "scrypt", "n": 1 << 10, "r": 8, "p": 1}


def _wallet_file(directory: str, label: str) -> tuple[str, str]:
    """Écrit un wallet à un chemin et retourne (chemin, adresse on-chain de la clé)."""
    path = Path(directory) / "wallet.json"
    wallet = Wallet(os.urandom(KDF_SALT_BYTES), kdf=FAST_KDF, path=path)
    key = wallet.generate_key("pw", label=label)
    wallet.save()
    return str(path), key.address


class MineAddressArgumentTests(unittest.TestCase):
    def test_accepts_raw_lowercase(self):
        address = KeyPair.generate().address
        self.assertEqual(mine_address_argument(address), address)

    def test_accepts_checksummed_and_normalises(self):
        address = KeyPair.generate().address
        checksummed = to_checksummed_address(address)
        self.assertEqual(mine_address_argument(checksummed), address)

    def test_rejects_bad_checksum(self):
        checksummed = to_checksummed_address(KeyPair.generate().address)
        for index, character in enumerate(checksummed):
            if character.isalpha():
                flipped = character.lower() if character.isupper() else character.upper()
                mangled = checksummed[:index] + flipped + checksummed[index + 1 :]
                with self.assertRaises(argparse.ArgumentTypeError):
                    mine_address_argument(mangled)
                return
        self.skipTest("adresse sans lettre")

    def test_rejects_garbage(self):
        for bad in ("xyz", "a" * 63, "zz" * 32):
            with self.subTest(value=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    mine_address_argument(bad)


class ResolveMinerAddressTests(unittest.TestCase):
    def test_none_when_not_mining(self):
        args = argparse.Namespace(mine=None, mine_label=None, wallet="wallet.json")
        self.assertIsNone(resolve_miner_address(args))

    def test_uses_mine_address_directly(self):
        address = KeyPair.generate().address
        args = argparse.Namespace(mine=address, mine_label=None, wallet="wallet.json")
        self.assertEqual(resolve_miner_address(args), address)

    def test_resolves_label_from_wallet_without_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, address = _wallet_file(tmp, "mineur")
            args = argparse.Namespace(mine=None, mine_label="mineur", wallet=path)
            self.assertEqual(resolve_miner_address(args), address)

    def test_unknown_label_reports_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = _wallet_file(tmp, "mineur")
            args = argparse.Namespace(mine=None, mine_label="absent", wallet=path)
            with self.assertRaises(Exception) as caught:
                resolve_miner_address(args)
            self.assertIn("mineur", str(caught.exception))  # liste les clés disponibles

    def test_missing_wallet_file(self):
        args = argparse.Namespace(mine=None, mine_label="mineur", wallet="pas-de-wallet-ici.json")
        with self.assertRaises(Exception):
            resolve_miner_address(args)


class NodeParserTests(unittest.TestCase):
    def test_mine_and_mine_label_are_mutually_exclusive(self):
        parser = build_parser()
        address = KeyPair.generate().address
        with self.assertRaises(SystemExit):
            parser.parse_args(["node", "--mine", address, "--mine-label", "mineur"])

    def test_mine_normalises_checksummed_at_parse_time(self):
        parser = build_parser()
        address = KeyPair.generate().address
        args = parser.parse_args(["node", "--mine", to_checksummed_address(address)])
        self.assertEqual(args.mine, address)
        self.assertIsNone(args.mine_label)

    def test_mine_label_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["node", "--mine-label", "mineur"])
        self.assertEqual(args.mine_label, "mineur")
        self.assertEqual(args.wallet, "wallet.json")


if __name__ == "__main__":
    unittest.main()
