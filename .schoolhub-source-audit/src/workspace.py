import os
import json
import base64
import hashlib
import secrets
import shutil
import struct
from pathlib import Path

_AESGCM = None

def _aesgcm():
    """Lazy-load cryptography only when Vault crypto is actually used."""
    global _AESGCM
    if _AESGCM is None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _AESGCM = AESGCM
    return _AESGCM


class WorkspaceError(Exception):
    pass


class WorkspaceManager:
    """Encrypted SchoolHub workspace using scrypt + AES-256-GCM."""

    FORMAT_VERSION = 1
    FILE_MAGIC_V1 = b"SHENC1"
    FILE_MAGIC = b"SHENC2"
    STREAM_CHUNK_SIZE = 4 * 1024 * 1024
    STREAM_NONCE_PREFIX_LEN = 8
    VERIFY_TEXT = b"SCHOOLHUB_WORKSPACE_OK"
    VERIFY_AAD = b"schoolhub-verification-v1"
    FILE_AAD_PREFIX = b"SchoolHub-v1:"

    # 16 MiB working set: compatible with OpenSSL builds on Windows
    # that enforce a ~32 MiB EVP/scrypt memory limit.
    SCRYPT_N = 2 ** 14
    SCRYPT_R = 8
    SCRYPT_P = 1
    KEY_LEN = 32
    SALT_LEN = 16
    NONCE_LEN = 12

    def __init__(self, workspace_path, vault_path=None):
        self.workspace_path = Path(workspace_path).resolve()
        self.vault_path = (
            Path(vault_path).resolve()
            if vault_path
            else Path(str(self.workspace_path) + ".vault")
        )
        self.meta_path = self.vault_path / "vault.json"
        self.files_path = self.vault_path / "files"
        self._unlocked = False
        self._session_key = None
        self.last_warning = None

    @property
    def is_unlocked(self):
        return self._unlocked and self.workspace_path.is_dir()

    @property
    def exists(self):
        return self.vault_path.is_dir() and self.meta_path.is_file() and self.files_path.is_dir()

    @property
    def has_plaintext(self):
        """True when a readable working copy already exists on disk."""
        try:
            return self.workspace_path.is_dir() and bool(self._iter_plain_files())
        except Exception:
            return False

    def get_unlocked_path(self):
        if not self.is_unlocked:
            raise WorkspaceError("Il Workspace è bloccato.")
        self.ensure_materialized()
        return str(self.workspace_path)

    def ensure_materialized(self):
        """Ensure the unlocked plaintext workspace contains the Vault contents.

        This is deliberately conservative: existing plaintext files are never
        overwritten. If the directory is empty, the encrypted Vault is decrypted
        into it and the result is verified before returning.
        """
        if not self._unlocked:
            raise WorkspaceError("Il Workspace è bloccato.")
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        if self._iter_plain_files():
            return
        if self._session_key is None:
            raise WorkspaceError("Chiave di sessione Workspace non disponibile.")
        temp_plain = self.workspace_path.with_name(self.workspace_path.name + ".materializing-" + secrets.token_hex(8))
        temp_plain.mkdir(parents=True, exist_ok=False)
        try:
            for root, dirs, files in os.walk(self.files_path, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                for name in files:
                    p = root_path / name
                    rel = p.relative_to(self.files_path)
                    dest = self._safe_plain_destination(rel, temp_plain)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    self._decrypt_file(p, dest, self._session_key, rel)
            for item in temp_plain.iterdir():
                os.replace(item, self.workspace_path / item.name)
            shutil.rmtree(temp_plain, ignore_errors=True)
        except Exception:
            shutil.rmtree(temp_plain, ignore_errors=True)
            raise

    # ------------------------------------------------------------
    # Crypto helpers
    # ------------------------------------------------------------

    @staticmethod
    def _password_bytes(password):
        if not isinstance(password, str) or not password:
            raise WorkspaceError("Password non valida.")
        return password.encode("utf-8")

    def _derive_key(self, password, salt):
        try:
            return hashlib.scrypt(
                self._password_bytes(password),
                salt=salt,
                n=self.SCRYPT_N,
                r=self.SCRYPT_R,
                p=self.SCRYPT_P,
                dklen=self.KEY_LEN,
            )
        except (ValueError, MemoryError) as exc:
            raise WorkspaceError(f"Impossibile derivare la chiave di cifratura: {exc}") from exc

    @staticmethod
    def _b64(data):
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _unb64(value, field):
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except Exception as exc:
            raise WorkspaceError(f"Metadati Workspace non validi: {field}.") from exc

    def _make_metadata(self, key, salt):
        nonce = secrets.token_bytes(self.NONCE_LEN)
        verification = _aesgcm()(key).encrypt(nonce, self.VERIFY_TEXT, self.VERIFY_AAD)
        return {
            "version": self.FORMAT_VERSION,
            "kdf": "scrypt",
            "n": self.SCRYPT_N,
            "r": self.SCRYPT_R,
            "p": self.SCRYPT_P,
            "salt": self._b64(salt),
            "verification_nonce": self._b64(nonce),
            "verification": self._b64(verification),
        }

    def _load_metadata(self):
        if not self.meta_path.is_file():
            raise WorkspaceError("Workspace cifrato non valido: manca vault.json.")
        try:
            with self.meta_path.open("r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception as exc:
            raise WorkspaceError("Impossibile leggere i metadati del Workspace.") from exc

        if meta.get("version") != self.FORMAT_VERSION or meta.get("kdf") != "scrypt":
            raise WorkspaceError("Versione/formato Workspace non supportato.")
        for key in ("n", "r", "p", "salt", "verification_nonce", "verification"):
            if key not in meta:
                raise WorkspaceError(f"Metadati Workspace incompleti: manca {key}.")
        return meta

    def _verify_password(self, password):
        meta = self._load_metadata()
        salt = self._unb64(meta["salt"], "salt")
        if len(salt) < 16:
            raise WorkspaceError("Metadati Workspace non validi: salt troppo corto.")

        # Use stored parameters, but reject corrupted/hostile values before asking
        # OpenSSL to allocate memory.
        try:
            n = int(meta["n"]); r = int(meta["r"]); p = int(meta["p"])
            if n < 2**12 or n > 2**18 or (n & (n - 1)) != 0 or r < 1 or r > 32 or p < 1 or p > 16:
                raise ValueError("parametri scrypt fuori intervallo")
            key = hashlib.scrypt(
                self._password_bytes(password), salt=salt, n=n, r=r, p=p, dklen=self.KEY_LEN
            )
        except Exception as exc:
            raise WorkspaceError("Metadati KDF non validi o impossibili da verificare.") from exc

        nonce = self._unb64(meta["verification_nonce"], "verification_nonce")
        ciphertext = self._unb64(meta["verification"], "verification")
        try:
            plain = _aesgcm()(key).decrypt(nonce, ciphertext, self.VERIFY_AAD)
        except Exception as exc:
            raise WorkspaceError("Password errata o Workspace danneggiato.") from exc
        if plain != self.VERIFY_TEXT:
            raise WorkspaceError("Password errata o Workspace danneggiato.")
        return key

    # ------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------

    @staticmethod
    def _ensure_not_symlink(path):
        if path.is_symlink():
            raise WorkspaceError(f"Collegamento simbolico non consentito: {path.name}")

    def _iter_plain_files(self):
        if not self.workspace_path.exists():
            return []
        result = []
        for root, dirs, files in os.walk(self.workspace_path, followlinks=False):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
            for name in files:
                p = root_path / name
                self._ensure_not_symlink(p)
                result.append(p)
        return result

    def _safe_relative(self, path):
        rel = Path(path).resolve().relative_to(self.workspace_path.resolve())
        if str(rel) in ("", ".") or rel.is_absolute() or ".." in rel.parts:
            raise WorkspaceError("Percorso file non valido.")
        return rel

    def _safe_vault_destination(self, rel):
        # Never trust a stored relative path blindly.
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkspaceError(f"Percorso cifrato non valido: {rel}")
        dest = (self.files_path / rel).resolve()
        base = self.files_path.resolve()
        try:
            dest.relative_to(base)
        except ValueError as exc:
            raise WorkspaceError("Tentativo di path traversal nel Vault.") from exc
        return dest

    def _safe_plain_destination(self, rel, root):
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkspaceError(f"Percorso cifrato non valido: {rel}")
        dest = (root / rel).resolve()
        base = root.resolve()
        try:
            dest.relative_to(base)
        except ValueError as exc:
            raise WorkspaceError("Tentativo di path traversal nel Workspace.") from exc
        return dest

    @staticmethod
    def _atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
        try:
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _notify_progress(callback, fraction, phase, detail):
        if callback is None:
            return
        try:
            callback(max(0.0, min(1.0, float(fraction))), phase, str(detail))
        except Exception:
            pass

    def _encrypt_file(self, source, dest, key, rel, progress=None):
        """Encrypt any binary file using a streaming AES-GCM container (SHENC2).

        Old SHENC1 Vaults remain readable; all newly written files use SHENC2 so
        large videos/audio/images do not need to fit in RAM.
        """
        source = Path(source)
        dest = Path(dest)
        try:
            total_size = source.stat().st_size
        except OSError as exc:
            raise WorkspaceError(f"Impossibile leggere {rel}: {exc}") from exc

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp-" + secrets.token_hex(8))
        nonce_prefix = secrets.token_bytes(self.STREAM_NONCE_PREFIX_LEN)
        aad_base = self.FILE_AAD_PREFIX + str(rel).replace("\\", "/").encode("utf-8")
        done = 0
        counter = 0
        try:
            with source.open("rb") as src, tmp.open("wb") as out:
                out.write(self.FILE_MAGIC)
                out.write(struct.pack(">QI", total_size, self.STREAM_CHUNK_SIZE))
                out.write(nonce_prefix)
                if total_size == 0:
                    self._notify_progress(progress, 1.0, "Cifratura", rel)
                while True:
                    chunk = src.read(self.STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    if counter >= 2**32:
                        raise WorkspaceError(f"File troppo grande per il formato cifrato: {rel}")
                    nonce = nonce_prefix + counter.to_bytes(4, "big")
                    aad = aad_base + b":chunk:" + counter.to_bytes(4, "big") + b":" + total_size.to_bytes(8, "big")
                    ciphertext = _aesgcm()(key).encrypt(nonce, chunk, aad)
                    out.write(struct.pack(">I", len(chunk)))
                    out.write(ciphertext)
                    done += len(chunk)
                    counter += 1
                    self._notify_progress(progress, done / max(1, total_size), "Cifratura", rel)
                # Detect a file changing while it is being encrypted.
                if done != total_size or source.stat().st_size != total_size:
                    raise WorkspaceError(
                        f"{rel} è stato modificato durante la cifratura. Nessun dato viene sostituito; chiudi il programma che lo sta modificando e riprova."
                    )
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"Impossibile cifrare {rel}: {exc}") from exc
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _decrypt_file(self, source, dest, key, rel, progress=None):
        source = Path(source)
        dest = Path(dest)
        try:
            with source.open("rb") as fh:
                magic = fh.read(len(self.FILE_MAGIC))
        except OSError as exc:
            raise WorkspaceError(f"Impossibile leggere il file cifrato {rel}: {exc}") from exc

        aad_base = self.FILE_AAD_PREFIX + str(rel).replace("\\", "/").encode("utf-8")

        # Backward compatibility with all existing SHENC1 Vaults.
        if magic == self.FILE_MAGIC_V1:
            try:
                payload = source.read_bytes()
            except OSError as exc:
                raise WorkspaceError(f"Impossibile leggere il file cifrato {rel}: {exc}") from exc
            if len(payload) < len(self.FILE_MAGIC_V1) + self.NONCE_LEN + 16:
                raise WorkspaceError(f"File cifrato non valido: {rel}")
            offset = len(self.FILE_MAGIC_V1)
            nonce = payload[offset:offset + self.NONCE_LEN]
            ciphertext = payload[offset + self.NONCE_LEN:]
            try:
                plain = _aesgcm()(key).decrypt(nonce, ciphertext, aad_base)
            except Exception as exc:
                raise WorkspaceError(f"Impossibile decifrare {rel}: password errata o file danneggiato.") from exc
            try:
                self._atomic_write(dest, plain)
                self._notify_progress(progress, 1.0, "Decifratura", rel)
                return
            except OSError as exc:
                raise WorkspaceError(f"Impossibile ripristinare {rel}: {exc}") from exc

        if magic != self.FILE_MAGIC:
            raise WorkspaceError(f"Formato file non riconosciuto: {rel}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp-" + secrets.token_hex(8))
        try:
            with source.open("rb") as src, tmp.open("wb") as out:
                src.read(len(self.FILE_MAGIC))
                header = src.read(12)
                if len(header) != 12:
                    raise WorkspaceError(f"File cifrato troncato: {rel}")
                total_size, chunk_size = struct.unpack(">QI", header)
                if chunk_size < 64 * 1024 or chunk_size > 64 * 1024 * 1024:
                    raise WorkspaceError(f"Dimensione blocco cifrato non valida: {rel}")
                nonce_prefix = src.read(self.STREAM_NONCE_PREFIX_LEN)
                if len(nonce_prefix) != self.STREAM_NONCE_PREFIX_LEN:
                    raise WorkspaceError(f"File cifrato troncato: {rel}")
                done = 0
                counter = 0
                if total_size == 0:
                    self._notify_progress(progress, 1.0, "Decifratura", rel)
                while done < total_size:
                    length_raw = src.read(4)
                    if len(length_raw) != 4:
                        raise WorkspaceError(f"File cifrato troncato: {rel}")
                    plain_len = struct.unpack(">I", length_raw)[0]
                    if plain_len <= 0 or plain_len > chunk_size or done + plain_len > total_size:
                        raise WorkspaceError(f"Blocco cifrato non valido: {rel}")
                    ciphertext = src.read(plain_len + 16)
                    if len(ciphertext) != plain_len + 16:
                        raise WorkspaceError(f"File cifrato troncato: {rel}")
                    nonce = nonce_prefix + counter.to_bytes(4, "big")
                    aad = aad_base + b":chunk:" + counter.to_bytes(4, "big") + b":" + total_size.to_bytes(8, "big")
                    try:
                        plain = _aesgcm()(key).decrypt(nonce, ciphertext, aad)
                    except Exception as exc:
                        raise WorkspaceError(f"Impossibile decifrare {rel}: password errata o file danneggiato.") from exc
                    out.write(plain)
                    done += len(plain)
                    counter += 1
                    self._notify_progress(progress, done / max(1, total_size), "Decifratura", rel)
                if src.read(1):
                    raise WorkspaceError(f"Dati extra non validi nel file cifrato: {rel}")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
        except WorkspaceError:
            raise
        except OSError as exc:
            raise WorkspaceError(f"Impossibile ripristinare {rel}: {exc}") from exc
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _verify_encrypted_file(self, source, key, rel):
        """Authenticate an encrypted file without keeping plaintext on disk."""
        source = Path(source)
        with source.open("rb") as fh:
            magic = fh.read(len(self.FILE_MAGIC))
        aad_base = self.FILE_AAD_PREFIX + str(rel).replace("\\", "/").encode("utf-8")
        if magic == self.FILE_MAGIC_V1:
            payload = source.read_bytes()
            if len(payload) < len(self.FILE_MAGIC_V1) + self.NONCE_LEN + 16:
                raise WorkspaceError(f"File cifrato non valido: {rel}")
            off = len(self.FILE_MAGIC_V1)
            _aesgcm()(key).decrypt(payload[off:off+self.NONCE_LEN], payload[off+self.NONCE_LEN:], aad_base)
            return
        if magic != self.FILE_MAGIC:
            raise WorkspaceError(f"Formato file non riconosciuto: {rel}")
        with source.open("rb") as src:
            src.read(len(self.FILE_MAGIC))
            header = src.read(12)
            if len(header) != 12:
                raise WorkspaceError(f"File cifrato troncato: {rel}")
            total_size, chunk_size = struct.unpack(">QI", header)
            nonce_prefix = src.read(self.STREAM_NONCE_PREFIX_LEN)
            if len(nonce_prefix) != self.STREAM_NONCE_PREFIX_LEN:
                raise WorkspaceError(f"File cifrato troncato: {rel}")
            done = counter = 0
            while done < total_size:
                raw = src.read(4)
                if len(raw) != 4:
                    raise WorkspaceError(f"File cifrato troncato: {rel}")
                plain_len = struct.unpack(">I", raw)[0]
                if plain_len <= 0 or plain_len > chunk_size or done + plain_len > total_size:
                    raise WorkspaceError(f"Blocco cifrato non valido: {rel}")
                ciphertext = src.read(plain_len + 16)
                if len(ciphertext) != plain_len + 16:
                    raise WorkspaceError(f"File cifrato troncato: {rel}")
                nonce = nonce_prefix + counter.to_bytes(4, "big")
                aad = aad_base + b":chunk:" + counter.to_bytes(4, "big") + b":" + total_size.to_bytes(8, "big")
                plain = _aesgcm()(key).decrypt(nonce, ciphertext, aad)
                if len(plain) != plain_len:
                    raise WorkspaceError(f"Blocco cifrato non valido: {rel}")
                done += plain_len
                counter += 1
            if src.read(1):
                raise WorkspaceError(f"Dati extra non validi nel file cifrato: {rel}")

    @staticmethod
    def _tree_hash(root):
        root = Path(root)
        h = hashlib.sha256()
        files = []
        if not root.exists():
            return h.hexdigest()
        for r, ds, fs in os.walk(root, followlinks=False):
            rp = Path(r)
            ds[:] = [d for d in ds if not (rp / d).is_symlink()]
            for fn in fs:
                fp = rp / fn
                if fp.is_symlink():
                    raise WorkspaceError(f"Collegamento simbolico non consentito: {fp.name}")
                files.append(fp.relative_to(root))
        for rel in sorted(files, key=lambda x: str(x).casefold()):
            fp = root / rel
            h.update(str(rel).replace("\\", "/").encode("utf-8")); h.update(b"\0")
            with fp.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            h.update(b"\0")
        return h.hexdigest()

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------


    def export_to(self, destination, password):
        """Decrypt the vault into a temporary/plain destination without unlocking the Workspace."""
        if not self.exists:
            raise WorkspaceError("Il Workspace cifrato non esiste.")
        key = self._verify_password(password)
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise WorkspaceError("La cartella di esportazione non è vuota.")

        try:
            encrypted_files = []
            for root, dirs, files in os.walk(self.files_path, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                for name in files:
                    p = root_path / name
                    self._ensure_not_symlink(p)
                    rel = p.relative_to(self.files_path)
                    encrypted_files.append((p, rel))

            for source, rel in encrypted_files:
                dest = self._safe_plain_destination(rel, destination)
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._decrypt_file(source, dest, key, rel)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    def import_from(self, source, password):
        """Replace the encrypted vault with files from a temporary/plain source tree."""
        if not self.exists:
            raise WorkspaceError("Il Workspace cifrato non esiste.")
        source = Path(source).resolve()
        if not source.is_dir():
            raise WorkspaceError("Sorgente Workspace non valida.")
        key = self._verify_password(password)
        salt = self._unb64(self._load_metadata()["salt"], "salt")
        metadata = self._load_metadata()
        staging = self.vault_path.with_name(self.vault_path.name + ".syncing-" + secrets.token_hex(8))
        old_vault = self.vault_path.with_name(self.vault_path.name + ".old-sync-" + secrets.token_hex(8))
        try:
            (staging / "files").mkdir(parents=True, exist_ok=False)
            with (staging / "vault.json").open("w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
                fh.write("\n")

            source_files = []
            for root, dirs, files in os.walk(source, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if d != ".git" and not (root_path / d).is_symlink()]
                for name in files:
                    if ".git" in root_path.parts and root_path.parts.index(".git") >= 0:
                        continue
                    p = root_path / name
                    self._ensure_not_symlink(p)
                    rel = p.relative_to(source)
                    if rel.parts and rel.parts[0] == ".git":
                        continue
                    source_files.append((p, rel))

            for src, rel in source_files:
                self._encrypt_file(src, staging / "files" / rel, key, rel)

            for src, rel in source_files:
                encrypted = staging / "files" / rel
                self._verify_encrypted_file(encrypted, key, rel)

            os.replace(self.vault_path, old_vault)
            try:
                os.replace(staging, self.vault_path)
            except Exception:
                os.replace(old_vault, self.vault_path)
                raise
            shutil.rmtree(old_vault, ignore_errors=True)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def create(self, password, progress=None):
        """Create a verified encrypted Vault from the current plaintext Workspace.

        The Vault is committed before plaintext cleanup. Cleanup is performed by
        renaming the whole Workspace directory first, so a Windows sharing violation
        can never leave a half-deleted source tree.
        """
        if self.exists:
            raise WorkspaceError("Il Workspace cifrato esiste già.")
        if self._unlocked:
            raise WorkspaceError("Il Workspace è già sbloccato.")
        self._password_bytes(password)
        if len(password) < 8:
            raise WorkspaceError("La password deve contenere almeno 8 caratteri.")
        if self.vault_path.exists():
            raise WorkspaceError("Esiste già una cartella Vault incompleta o non valida: " + str(self.vault_path))

        self.last_warning = None
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        plain_files = self._iter_plain_files()
        source_hash = self._tree_hash(self.workspace_path)
        salt = secrets.token_bytes(self.SALT_LEN)
        key = self._derive_key(password, salt)
        metadata = self._make_metadata(key, salt)
        staging = self.vault_path.with_name(self.vault_path.name + ".creating-" + secrets.token_hex(8))

        try:
            (staging / "files").mkdir(parents=True, exist_ok=False)
            with (staging / "vault.json").open("w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
                fh.write("\n")
                fh.flush(); os.fsync(fh.fileno())

            total_files = max(1, len(plain_files))
            for idx, source in enumerate(plain_files):
                rel = self._safe_relative(source)
                if progress is None:
                    self._encrypt_file(source, staging / "files" / rel, key, rel)
                else:
                    self._encrypt_file(
                        source, staging / "files" / rel, key, rel,
                        progress=lambda frac, phase, detail, i=idx: self._notify_progress(progress, 0.82 * ((i + frac) / total_files), phase, detail)
                    )

            # Full authentication verification before the Vault becomes canonical.
            for idx, source in enumerate(plain_files):
                rel = self._safe_relative(source)
                encrypted = staging / "files" / rel
                self._verify_encrypted_file(encrypted, key, rel)
                self._notify_progress(progress, 0.82 + 0.16 * ((idx + 1) / total_files), "Verifica Vault", rel)
            self._notify_progress(progress, 0.99, "Finalizzazione", "Chiusura sicura del Workspace")

            if self._tree_hash(self.workspace_path) != source_hash:
                raise WorkspaceError("Il Workspace è stato modificato durante la cifratura. Nessun dato è stato sostituito; riprova quando i file sono fermi.")
            os.replace(staging, self.vault_path)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

        # Remove plaintext atomically at directory level. If rename fails, the
        # complete plaintext tree AND the complete Vault both remain intact.
        quarantine = self.workspace_path.with_name(self.workspace_path.name + ".creating-cleanup-" + secrets.token_hex(8))
        try:
            os.replace(self.workspace_path, quarantine)
            self.workspace_path.mkdir(parents=True, exist_ok=False)
        except Exception as exc:
            raise WorkspaceError(
                "Il Vault è stato creato e verificato, ma Windows non consente di rimuovere il Workspace in chiaro. "
                "Chiudi i programmi che stanno usando quei file, poi sblocca e blocca nuovamente il Workspace."
            ) from exc

        try:
            shutil.rmtree(quarantine, ignore_errors=False)
        except Exception as exc:
            self.last_warning = f"Copia in chiaro residua da eliminare: {quarantine}"
            raise WorkspaceError(
                "Il Vault è stato creato correttamente, ma una copia temporanea in chiaro non è stata eliminata completamente: "
                + str(quarantine) + ". Chiudi i programmi che usano quei file e rimuovi quella cartella."
            ) from exc

        self._unlocked = False
        self._session_key = None

    def recreate_from_plaintext(self, password):
        """Create a fresh encrypted Workspace from the current plaintext folder.

        Used when the old vault is unusable. The plaintext Workspace is treated as
        the source of truth; the broken vault is moved aside instead of blocking
        the user.
        """
        self._password_bytes(password)
        if len(password) < 8:
            raise WorkspaceError("La password deve contenere almeno 8 caratteri.")
        if not self.workspace_path.exists():
            self.workspace_path.mkdir(parents=True, exist_ok=True)
        plain_files = self._iter_plain_files()
        if not plain_files:
            raise WorkspaceError("Il Workspace in chiaro è vuoto: non c'è nulla da recuperare.")

        broken = None
        if self.vault_path.exists():
            broken = self.vault_path.with_name(self.vault_path.name + ".broken-" + secrets.token_hex(6))
            os.replace(self.vault_path, broken)
        try:
            self.create(password)
        except Exception:
            if broken and broken.exists() and not self.vault_path.exists():
                os.replace(broken, self.vault_path)
            raise
        # The new vault is valid and the plaintext was removed by create().
        if broken and broken.exists():
            shutil.rmtree(broken, ignore_errors=True)
        self._unlocked = False

    def unlock(self, password, progress=None):
        if self.is_unlocked:
            return
        if not self.exists:
            raise WorkspaceError("Il Workspace non è stato ancora creato.")

        self.last_warning = None
        key = self._verify_password(password)
        existing_plain = self._iter_plain_files() if self.workspace_path.exists() else []

        # Instant Unlock: if a readable working copy is already present, it is
        # the active work tree from the previous session. Never waste minutes
        # decrypting the Vault again only to compare identical/stale working files.
        # The password is still verified before access is granted.
        if existing_plain:
            self._unlocked = True
            self._session_key = key
            self._notify_progress(progress, 1.0, "Sblocco rapido", "Workspace già disponibile sul disco")
            return

        temp_plain = self.workspace_path.with_name(self.workspace_path.name + ".unlocking-" + secrets.token_hex(8))
        temp_plain.mkdir(parents=True, exist_ok=False)

        def tree_hash(root):
            h = hashlib.sha256(); files = []
            root = Path(root)
            if not root.exists(): return h.hexdigest()
            for r, ds, fs in os.walk(root, followlinks=False):
                rp = Path(r)
                ds[:] = [d for d in ds if not (rp / d).is_symlink()]
                for fn in fs:
                    fp = rp / fn
                    self._ensure_not_symlink(fp)
                    files.append(fp.relative_to(root))
            for relp in sorted(files, key=lambda x: str(x).casefold()):
                fp = root / relp
                h.update(str(relp).replace("\\", "/").encode("utf-8")); h.update(b"\0")
                with fp.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                h.update(b"\0")
            return h.hexdigest()

        try:
            encrypted_items = []
            for root, dirs, files in os.walk(self.files_path, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                for name in files:
                    p = root_path / name
                    self._ensure_not_symlink(p)
                    encrypted_items.append((p, p.relative_to(self.files_path)))
            total_items = max(1, len(encrypted_items))
            for item_index, (p, rel) in enumerate(encrypted_items):
                dest = self._safe_plain_destination(rel, temp_plain)
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._decrypt_file(
                    p, dest, key, rel,
                    progress=lambda frac, phase, detail, i=item_index: self._notify_progress(progress, 0.94 * ((i + frac) / total_items), phase, detail)
                )
            self._notify_progress(progress, 0.97, "Verifica", "Controllo integrità Workspace")

            if existing_plain:
                if tree_hash(self.workspace_path) == tree_hash(temp_plain):
                    shutil.rmtree(temp_plain, ignore_errors=True)
                    self._unlocked = True; self._session_key = key
                    return

                recovery = self.workspace_path.with_name(self.workspace_path.name + ".recovery-" + secrets.token_hex(8))
                try:
                    os.replace(self.workspace_path, recovery)
                    os.replace(temp_plain, self.workspace_path)
                except Exception as exc:
                    if not self.workspace_path.exists() and recovery.exists():
                        try: os.replace(recovery, self.workspace_path)
                        except Exception: pass
                    raise WorkspaceError(
                        "Il Workspace in chiaro era diverso dal Vault e non è stato possibile metterlo al sicuro automaticamente. "
                        "Chiudi eventuali programmi che stanno usando i file e riprova."
                    ) from exc
                self.last_warning = "Ho conservato una copia di recupero del vecchio Workspace in: " + str(recovery)
                self._unlocked = True; self._session_key = key
                return

            if self.workspace_path.exists():
                try: self.workspace_path.rmdir()
                except OSError: pass
            os.replace(temp_plain, self.workspace_path)
            self._unlocked = True; self._session_key = key
        except Exception:
            shutil.rmtree(temp_plain, ignore_errors=True)
            self._unlocked = False
            self._session_key = None
            raise

    def save_unlocked_to_vault(self, source):
        """Replace the encrypted Vault from a trusted tree using the current session key.

        This is used by Sync while the Vault is already unlocked, so Sync never
        depends on a stored password. The Vault and plaintext Workspace are updated
        from the same final tree.
        """
        if not self.is_unlocked or self._session_key is None:
            raise WorkspaceError("Il Workspace è bloccato o la chiave di sessione non è disponibile.")
        source = Path(source).resolve()
        if not source.is_dir():
            raise WorkspaceError("Sorgente Workspace non valida.")

        key = self._session_key
        metadata = self._load_metadata()
        staging = self.vault_path.with_name(self.vault_path.name + ".syncvault-" + secrets.token_hex(8))
        old_vault = self.vault_path.with_name(self.vault_path.name + ".old-syncvault-" + secrets.token_hex(8))
        try:
            (staging / "files").mkdir(parents=True, exist_ok=False)
            with (staging / "vault.json").open("w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
                fh.write("\n")

            source_files = []
            for root, dirs, files in os.walk(source, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if d != ".git" and not (root_path / d).is_symlink()]
                for name in files:
                    p = root_path / name
                    self._ensure_not_symlink(p)
                    rel = p.relative_to(source)
                    if rel.parts and rel.parts[0] == ".git":
                        continue
                    source_files.append((p, rel))

            for src_file, rel in source_files:
                self._encrypt_file(src_file, staging / "files" / rel, key, rel)

            # Verify the staged Vault before replacing the current one.
            for src_file, rel in source_files:
                enc = staging / "files" / rel
                self._verify_encrypted_file(enc, key, rel)

            os.replace(self.vault_path, old_vault)
            try:
                os.replace(staging, self.vault_path)
            except Exception:
                os.replace(old_vault, self.vault_path)
                raise
            shutil.rmtree(old_vault, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def replace_plaintext_from_tree(self, source):
        """Atomically replace the unlocked plaintext Workspace from a trusted tree."""
        if not self.is_unlocked:
            raise WorkspaceError("Il Workspace è bloccato.")
        source = Path(source).resolve()
        if not source.is_dir():
            raise WorkspaceError("Sorgente Workspace non valida.")
        self.last_warning = None

        temp = self.workspace_path.with_name(self.workspace_path.name + ".syncplain-" + secrets.token_hex(8))
        backup = self.workspace_path.with_name(self.workspace_path.name + ".sync-recovery-" + secrets.token_hex(8))
        temp.mkdir(parents=True, exist_ok=False)
        swapped = False
        try:
            for root, dirs, files in os.walk(source, followlinks=False):
                root_path = Path(root)
                dirs[:] = [d for d in dirs if d != ".git" and not (root_path / d).is_symlink()]
                rel_dir = root_path.relative_to(source)
                target_dir = temp / rel_dir
                target_dir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    src = root_path / name
                    self._ensure_not_symlink(src)
                    shutil.copy2(src, target_dir / name)

            # Swap whole directories; never delete the live tree file-by-file.
            os.replace(self.workspace_path, backup)
            try:
                os.replace(temp, self.workspace_path)
                swapped = True
            except Exception:
                os.replace(backup, self.workspace_path)
                raise

            try:
                shutil.rmtree(backup, ignore_errors=False)
            except Exception as exc:
                self.last_warning = "Copia di recupero Workspace non eliminata: " + str(backup)
                raise WorkspaceError(
                    "Il Workspace è stato aggiornato, ma la vecchia copia in chiaro non è stata eliminata completamente: "
                    + str(backup)
                ) from exc
        except Exception:
            if temp.exists(): shutil.rmtree(temp, ignore_errors=True)
            if not swapped and backup.exists() and not self.workspace_path.exists():
                try: os.replace(backup, self.workspace_path)
                except Exception: pass
            raise

    def session_lock(self):
        """Forget the in-memory key but keep the readable working copy on disk.

        This is intentionally a convenience lock, not encryption-at-rest. It
        enables near-instant unlock after password verification.
        """
        if not self.is_unlocked:
            return
        self._unlocked = False
        self._session_key = None
        self.last_warning = (
            "Blocco rapido attivo: i file restano leggibili sul disco. "
            "Usa 'Cifra e chiudi' per rimuovere la copia in chiaro."
        )

    def lock(self, password, progress=None):
        if not self.is_unlocked:
            if self.workspace_path.exists() and self._iter_plain_files():
                raise WorkspaceError("Il Workspace contiene file in chiaro ma non risulta sbloccato in questa sessione.")
            return

        self.last_warning = None
        key = self._verify_password(password)
        plain_files = self._iter_plain_files()
        source_hash = self._tree_hash(self.workspace_path)
        staging = self.vault_path.with_name(self.vault_path.name + ".locking-" + secrets.token_hex(8))
        old_vault = self.vault_path.with_name(self.vault_path.name + ".old-" + secrets.token_hex(8))

        try:
            (staging / "files").mkdir(parents=True, exist_ok=False)
            shutil.copy2(self.meta_path, staging / "vault.json")
            total_files = max(1, len(plain_files))
            for file_index, source in enumerate(plain_files):
                rel = self._safe_relative(source)
                if progress is None:
                    self._encrypt_file(source, staging / "files" / rel, key, rel)
                else:
                    self._encrypt_file(
                        source, staging / "files" / rel, key, rel,
                        progress=lambda frac, phase, detail, i=file_index: self._notify_progress(progress, 0.84 * ((i + frac) / total_files), phase, detail)
                    )
            for file_index, source in enumerate(plain_files):
                rel = self._safe_relative(source)
                self._verify_encrypted_file(staging / "files" / rel, key, rel)
                self._notify_progress(progress, 0.84 + 0.14 * ((file_index + 1) / total_files), "Verifica Vault", rel)
            self._notify_progress(progress, 0.99, "Finalizzazione", "Rimozione sicura della copia in chiaro")

            if self._tree_hash(self.workspace_path) != source_hash:
                raise WorkspaceError("Il Workspace è stato modificato durante il blocco. Il vecchio Vault resta intatto; riprova quando i file sono fermi.")

            os.replace(self.vault_path, old_vault)
            try:
                os.replace(staging, self.vault_path)
            except Exception:
                os.replace(old_vault, self.vault_path)
                raise
        except Exception:
            if staging.exists(): shutil.rmtree(staging, ignore_errors=True)
            raise

        # The newly generated Vault is canonical and complete. Keep the previous
        # encrypted Vault only until that commit succeeds, then remove it best-effort.
        shutil.rmtree(old_vault, ignore_errors=True)

        quarantine = self.workspace_path.with_name(self.workspace_path.name + ".locking-cleanup-" + secrets.token_hex(8))
        try:
            os.replace(self.workspace_path, quarantine)
            self.workspace_path.mkdir(parents=True, exist_ok=False)
        except Exception as exc:
            # No plaintext file has been removed, so retrying Lock is safe.
            raise WorkspaceError(
                "Il Vault aggiornato è sicuro, ma Windows non consente di chiudere il Workspace in chiaro. "
                "Chiudi i programmi che stanno usando i file e riprova."
            ) from exc

        self._unlocked = False
        self._session_key = None
        try:
            shutil.rmtree(quarantine, ignore_errors=False)
        except Exception as exc:
            self.last_warning = "Copia in chiaro residua da eliminare: " + str(quarantine)
            raise WorkspaceError(
                "Il Workspace è stato bloccato e il Vault è valido, ma una copia temporanea in chiaro non è stata eliminata completamente: "
                + str(quarantine)
            ) from exc

