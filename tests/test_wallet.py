import json
import os
import tempfile
import unittest
from pathlib import Path

from powchain.errors import WalletError
from powchain.keys import (
    AEAD_KEY_BYTES,
    KDF_SALT_BYTES,
    KeyPair,
    aead_decrypt,
    aead_encrypt,
    derive_symmetric_key,
)
from powchain.wallet import Wallet, WalletEntry

# Paramètre scrypt volontairement faible pour que les tests restent rapides.
# Le code de production garde SCRYPT_N = 2^15 ; ici on ne teste que la logique.
FAST_KDF = {"name": "scrypt", "n": 1 << 10, "r": 8, "p": 1}


def fast_wallet(path=None) -> Wallet:
    return Wallet(os.urandom(KDF_SALT_BYTES), kdf=FAST_KDF, path=path)


class EncryptionPrimitiveTests(unittest.TestCase):
    def test_derive_is_deterministic_and_salted(self):
        salt = os.urandom(KDF_SALT_BYTES)
        first = derive_symmetric_key("mot de passe", salt, n=1024)
        again = derive_symmetric_key("mot de passe", salt, n=1024)
        self.assertEqual(first, again)
        self.assertEqual(len(first), AEAD_KEY_BYTES)
        other_salt = derive_symmetric_key("mot de passe", os.urandom(KDF_SALT_BYTES), n=1024)
        self.assertNotEqual(first, other_salt)
        other_password = derive_symmetric_key("autre", salt, n=1024)
        self.assertNotEqual(first, other_password)

    def test_derive_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            derive_symmetric_key("x", b"trop court", n=1024)
        with self.assertRaises(TypeError):
            derive_symmetric_key(1234, os.urandom(KDF_SALT_BYTES), n=1024)  # type: ignore[arg-type]

    def test_encrypt_decrypt_round_trip(self):
        key = os.urandom(AEAD_KEY_BYTES)
        nonce, ciphertext = aead_encrypt(key, b"graine secrete", b"adresse")
        self.assertEqual(aead_decrypt(key, nonce, ciphertext, b"adresse"), b"graine secrete")

    def test_nonce_is_random_each_call(self):
        key = os.urandom(AEAD_KEY_BYTES)
        nonce_a, _ = aead_encrypt(key, b"x")
        nonce_b, _ = aead_encrypt(key, b"x")
        self.assertNotEqual(nonce_a, nonce_b)

    def test_wrong_key_fails(self):
        nonce, ciphertext = aead_encrypt(os.urandom(AEAD_KEY_BYTES), b"secret")
        with self.assertRaises(ValueError):
            aead_decrypt(os.urandom(AEAD_KEY_BYTES), nonce, ciphertext)

    def test_tampered_ciphertext_or_aad_fails(self):
        key = os.urandom(AEAD_KEY_BYTES)
        nonce, ciphertext = aead_encrypt(key, b"secret", b"adresse")
        flipped = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
        with self.assertRaises(ValueError):
            aead_decrypt(key, nonce, flipped, b"adresse")
        with self.assertRaises(ValueError):
            aead_decrypt(key, nonce, ciphertext, b"AUTRE-adresse")

    def test_encrypt_rejects_bad_key_length(self):
        with self.assertRaises(ValueError):
            aead_encrypt(b"court", b"x")


class WalletTests(unittest.TestCase):
    def test_generate_and_unlock_round_trip(self):
        wallet = fast_wallet()
        key = wallet.generate_key("motdepasse", label="principal")
        restored = wallet.key_pair("principal", "motdepasse")
        self.assertEqual(restored.address, key.address)
        self.assertEqual(restored.seed_hex, key.seed_hex)
        self.assertEqual(wallet.address_of("principal"), key.address)

    def test_verify_password(self):
        wallet = fast_wallet()
        self.assertFalse(wallet.verify_password("x"))  # wallet vide
        wallet.generate_key("bon", label="k")
        self.assertTrue(wallet.verify_password("bon"))
        self.assertFalse(wallet.verify_password("mauvais"))

    def test_wrong_password_raises(self):
        wallet = fast_wallet()
        wallet.generate_key("bon", label="k")
        with self.assertRaises(WalletError):
            wallet.key_pair("k", "mauvais")

    def test_second_key_must_use_same_password(self):
        wallet = fast_wallet()
        wallet.generate_key("motdepasse", label="a")
        with self.assertRaises(WalletError):
            wallet.generate_key("un-autre", label="b")
        # Le bon mot de passe passe, et les deux clés se déverrouillent.
        wallet.generate_key("motdepasse", label="b")
        self.assertEqual(len(wallet), 2)
        self.assertNotEqual(
            wallet.key_pair("a", "motdepasse").address, wallet.key_pair("b", "motdepasse").address
        )

    def test_duplicate_label_and_address_rejected(self):
        wallet = fast_wallet()
        key = wallet.generate_key("pw", label="a")
        with self.assertRaises(WalletError):
            wallet.generate_key("pw", label="a")  # même label
        with self.assertRaises(WalletError):
            wallet.add_key_pair(key, "pw", label="b")  # même clé

    def test_import_seed_hex(self):
        source = KeyPair.generate()
        wallet = fast_wallet()
        imported = wallet.import_seed_hex(source.seed_hex, "pw", label="restauree")
        self.assertEqual(imported.address, source.address)
        self.assertEqual(wallet.export_seed_hex("restauree", "pw"), source.seed_hex)
        with self.assertRaises(WalletError):
            wallet.import_seed_hex("pas hex", "pw", label="x")

    def test_suggest_label_is_unique(self):
        wallet = fast_wallet()
        first = wallet.suggest_label()
        wallet.generate_key("pw", label=first)
        self.assertNotEqual(wallet.suggest_label(), first)

    def test_entry_and_address_lookups(self):
        wallet = fast_wallet()
        key = wallet.generate_key("pw", label="a")
        self.assertEqual(wallet.label_of_address(key.address), "a")
        self.assertIsNone(wallet.label_of_address("0" * 64))
        self.assertEqual(wallet.checksummed_address_of("a").lower(), key.address)
        with self.assertRaises(WalletError):
            wallet.entry("inconnue")

    def test_remove(self):
        wallet = fast_wallet()
        wallet.generate_key("pw", label="a")
        wallet.remove("a")
        self.assertEqual(len(wallet), 0)
        with self.assertRaises(WalletError):
            wallet.remove("a")

    def test_to_dict_from_dict_round_trip(self):
        wallet = fast_wallet()
        wallet.generate_key("pw", label="a")
        wallet.generate_key("pw", label="b")
        clone = Wallet.from_dict(wallet.to_dict())
        self.assertEqual(clone.labels, wallet.labels)
        self.assertEqual(clone.key_pair("a", "pw").seed_hex, wallet.key_pair("a", "pw").seed_hex)

    def test_from_dict_rejects_corruption(self):
        wallet = fast_wallet()
        wallet.generate_key("pw", label="a")
        good = wallet.to_dict()

        def broken(**changes):
            data = json.loads(json.dumps(good))
            data.update(changes)
            return data

        with self.assertRaises(WalletError):
            Wallet.from_dict(broken(version=999))
        with self.assertRaises(WalletError):
            Wallet.from_dict(broken(cipher="ROT13"))
        with self.assertRaises(WalletError):
            Wallet.from_dict(broken(kdf={"name": "inconnu"}))
        with self.assertRaises(WalletError):
            Wallet.from_dict("pas un objet")
        # entrée mal formée : adresse invalide
        data = json.loads(json.dumps(good))
        data["keys"][0]["address"] = "zz"
        with self.assertRaises(WalletError):
            Wallet.from_dict(data)
        # labels en double
        data = json.loads(json.dumps(good))
        data["keys"].append(dict(data["keys"][0]))
        with self.assertRaises(WalletError):
            Wallet.from_dict(data)

    def test_save_and_load_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "wallet.json"  # le dossier parent est créé
            wallet = Wallet.create(path)  # paramètres scrypt de production, une fois
            key = wallet.generate_key("pw", label="a")
            wallet.save()
            self.assertTrue(path.exists())
            reloaded = Wallet.load(path)
            self.assertEqual(reloaded.key_pair("a", "pw").seed_hex, key.seed_hex)

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WalletError):
                Wallet.load(Path(tmp) / "absent.json")

    def test_tampered_file_is_detected_at_unlock(self):
        # Limite honnête : le disque garantit l'intégrité (AES-GCM), pas l'accès.
        # Un octet du chiffré modifié => le déchiffrement échoue au lieu de
        # rendre une graine fausse.
        wallet = fast_wallet()
        wallet.generate_key("pw", label="a")
        data = wallet.to_dict()
        cipher = bytearray.fromhex(data["keys"][0]["ciphertext"])
        cipher[0] ^= 1
        data["keys"][0]["ciphertext"] = cipher.hex()
        tampered = Wallet.from_dict(data)  # se charge sans erreur (structure valide)
        with self.assertRaises(WalletError):
            tampered.key_pair("a", "pw")  # mais ne se déverrouille pas

    def test_swapping_stored_address_is_detected(self):
        # L'adresse est liée au chiffré (donnée associée) : la remplacer par une
        # autre adresse valide casse le tag d'authentification.
        wallet = fast_wallet()
        wallet.generate_key("pw", label="a")
        data = wallet.to_dict()
        data["keys"][0]["address"] = KeyPair.generate().address
        tampered = Wallet.from_dict(data)
        with self.assertRaises(WalletError):
            tampered.key_pair("a", "pw")


if __name__ == "__main__":
    unittest.main()
