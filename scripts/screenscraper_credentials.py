#!/usr/bin/env python3
"""Private, per-user ScreenScraper credential storage using only the stdlib.

Windows encrypts with current-user DPAPI (never CRYPTPROTECT_LOCAL_MACHINE).
Other systems use an owner-only file, explicitly named ``protected_file``.
The workbench access token is a separate credential and is never accepted here.
Importing this module neither reads nor creates the credential store.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

FIELDS = (
    "SCREENSCRAPER_DEVID", "SCREENSCRAPER_DEVPASSWORD",
    "SCREENSCRAPER_SSID", "SCREENSCRAPER_SSPASSWORD",
)
DEVELOPER_FIELDS = FIELDS[:2]
USER_FIELDS = FIELDS[2:]
PATH_ENV = "ESDE_SCREENSCRAPER_CREDENTIALS_FILE"
MAX_STORE_BYTES = 65536
SERVICE = "es-de-resource-workbench"


class CredentialError(RuntimeError):
    """Safe error text; ``missing`` contains field names, never their values."""

    def __init__(self, message, *, code="credential_error", missing=()):
        super().__init__(message)
        self.code = code
        self.missing = list(missing)


def default_path(env=None):
    """Return a local user configuration path, outside any run or skill folder."""
    values = os.environ if env is None else env
    explicit = values.get(PATH_ENV)
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = values.get("LOCALAPPDATA")
        if not base:
            base = Path.home() / "AppData" / "Local"
    else:
        base = values.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / SERVICE / "credentials" / "screenscraper.json"


def _path(path=None, env=None):
    candidate = (default_path(env) if path is None else Path(path)).expanduser()
    result = Path(os.path.abspath(candidate))
    # Do not resolve first: that would hide links and Windows directory junctions.
    for component in (*reversed(result.parents), result):
        try:
            details = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise CredentialError("Cannot inspect the private credential path", code="unsafe_path") from None
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise CredentialError("Credential paths must not contain links or junctions", code="unsafe_path")
        if component != result and not stat.S_ISDIR(details.st_mode):
            raise CredentialError("Credential parent must be a directory", code="unsafe_path")
        if component == result and not stat.S_ISREG(details.st_mode):
            raise CredentialError("Credential store must be a regular file", code="unsafe_path")
        if component == result and details.st_nlink != 1:
            raise CredentialError("Credential store must not have additional hard links", code="unsafe_path")
    return result


def _normalize(values, *, required, reject_unknown=False):
    if not isinstance(values, Mapping):
        raise CredentialError("Credentials must be an object with named fields", code="invalid_values")
    if reject_unknown and any(key not in FIELDS for key in values):
        raise CredentialError("Only ScreenScraper credential fields are accepted", code="invalid_values")
    result = {}
    for field in FIELDS:
        if field in values:
            value = values[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise CredentialError("Credential fields must contain non-empty text", code="invalid_values", missing=[field])
            result[field] = value
    for pair in (DEVELOPER_FIELDS, USER_FIELDS):
        present = [field in result for field in pair]
        if any(present) and not all(present):
            absent = [field for field in pair if field not in result]
            raise CredentialError("Provide both fields of each credential pair", code="incomplete_pair", missing=absent)
    if required and not result:
        raise CredentialError("Provide a complete developer pair or user account pair", code="incomplete_pair", missing=FIELDS)
    return result


def _windows_api():
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    return advapi, kernel, crypt, wintypes


def _windows_identity(api):
    advapi, kernel, _, wintypes = api
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise CredentialError("Cannot determine the current Windows user", code="unsafe_permissions")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 65536:
            raise CredentialError("Cannot determine the current Windows user", code="unsafe_permissions")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise CredentialError("Cannot determine the current Windows user", code="unsafe_permissions")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise CredentialError("Cannot determine the current Windows user", code="unsafe_permissions")
        try:
            return buffer, sid, text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _private_windows(path, *, set_permissions=False, directory=False):
    api = _windows_api()
    advapi, kernel, _, wintypes = api
    identity_buffer, sid, sid_text = _windows_identity(api)
    descriptor = ctypes.c_void_p()
    try:
        if set_permissions:
            flags = "OICI" if directory else ""
            sddl = "O:" + sid_text + "D:P(A;" + flags + ";FA;;;" + sid_text + ")"
            if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                raise CredentialError("Cannot protect the credential store permissions", code="unsafe_permissions")
            if not advapi.SetFileSecurityW(str(path), 0x80000005, descriptor):
                raise CredentialError("Cannot protect the credential store permissions", code="unsafe_permissions")
            kernel.LocalFree(descriptor)
            descriptor = ctypes.c_void_p()
        owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        if advapi.GetNamedSecurityInfoW(str(path), 1, 0x00000005, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
            raise CredentialError("Cannot verify the credential store permissions", code="unsafe_permissions")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not owner or not dacl or not advapi.EqualSid(owner, sid) or not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)) or not control.value & 0x1000:
            raise CredentialError("Credential store must belong only to the current user", code="unsafe_permissions")
        # ACL header is BYTE revision/reserved, WORD size/count/reserved.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        if count != 1:
            raise CredentialError("Credential store must belong only to the current user", code="unsafe_permissions")
        ace = ctypes.c_void_p()
        if not advapi.GetAce(dacl, 0, ctypes.byref(ace)):
            raise CredentialError("Cannot verify the credential store permissions", code="unsafe_permissions")
        ace_type = ctypes.c_ubyte.from_address(ace.value).value
        ace_flags = ctypes.c_ubyte.from_address(ace.value + 1).value
        mask = ctypes.c_uint32.from_address(ace.value + 4).value
        if ace_type != 0 or ace_flags & 0x10 or mask != 0x001F01FF or not advapi.EqualSid(ace.value + 8, sid):
            raise CredentialError("Credential store must belong only to the current user", code="unsafe_permissions")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        # Keep the token buffer alive while its SID is used above.
        del identity_buffer


def _private_posix(path, *, set_permissions=False, directory=False):
    expected = 0o700 if directory else 0o600
    try:
        details = path.lstat()
        if details.st_uid != os.geteuid():
            raise CredentialError("Credential store must belong to the current user", code="unsafe_permissions")
        if set_permissions:
            path.chmod(expected)
            details = path.lstat()
        if stat.S_IMODE(details.st_mode) != expected:
            raise CredentialError("Credential store permissions are not private", code="unsafe_permissions")
    except OSError:
        raise CredentialError("Cannot verify the credential store permissions", code="unsafe_permissions") from None


def _private(path, *, set_permissions=False, directory=False):
    if os.name == "nt":
        _private_windows(path, set_permissions=set_permissions, directory=directory)
    else:
        _private_posix(path, set_permissions=set_permissions, directory=directory)


def _dpapi(data, *, decrypt=False):
    """Encrypt/decrypt for the current Windows user without UI prompts."""
    _, kernel, crypt, wintypes = _windows_api()
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN = 1. Deliberately omit LOCAL_MACHINE = 4.
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise CredentialError("Cannot decrypt credentials for the current Windows user" if decrypt else "Cannot encrypt credentials for the current Windows user", code="decrypt_failed" if decrypt else "encrypt_failed")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def _summary(values, *, storage, protection):
    developer = all(field in values for field in DEVELOPER_FIELDS)
    user = all(field in values for field in USER_FIELDS)
    return {"configured": developer or user, "ready": developer, "storage": storage, "protection": protection,
            "developer_configured": developer, "user_configured": user,
            "missing": [] if developer else list(DEVELOPER_FIELDS)}


def _protection():
    return "current_user_dpapi" if os.name == "nt" else "protected_file"


def save_credentials(values, path=None):
    """Atomically update whole pairs, retaining any previously saved other pair."""
    selected = _normalize(values, required=True, reject_unknown=True)
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _path(target)
        _private(target.parent, set_permissions=True, directory=True)
        if target.exists():
            _private(target)
            if not all(field in selected for field in FIELDS):
                # Every partial update preserves the other saved pair. An
                # unreadable store can only be replaced with all four fields,
                # or explicitly cleared before starting a new configuration.
                selected = {**load_credentials(target), **selected}
        raw = json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        envelope = {"version": 1, "provider": "screenscraper", "protection": _protection()}
        if os.name == "nt":
            envelope["encrypted"] = base64.b64encode(_dpapi(raw)).decode("ascii")
        else:
            envelope["credentials"] = selected
        encoded = (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_STORE_BYTES:
            raise CredentialError("Credential text exceeds the private store size limit", code="invalid_values")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".screenscraper-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                _private(temporary, set_permissions=True)
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            _path(target)
            os.replace(temporary, target)
            _private(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return _summary(selected, storage="saved", protection=_protection())
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError):
        raise CredentialError("Cannot save credentials to the private user store", code="save_failed") from None


def load_credentials(path=None):
    """Read saved secrets; return an empty dict when no store exists."""
    target = _path(path)
    if not target.exists():
        return {}
    _private(target.parent, directory=True)
    _private(target)
    try:
        if target.stat().st_size > MAX_STORE_BYTES:
            raise ValueError()
        with target.open("rb") as source:
            raw = source.read(MAX_STORE_BYTES + 1)
        if len(raw) > MAX_STORE_BYTES:
            raise ValueError()
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or envelope.get("version") != 1 or envelope.get("provider") != "screenscraper" or envelope.get("protection") != _protection():
            raise ValueError()
        if os.name == "nt":
            encrypted = envelope.get("encrypted")
            if not isinstance(encrypted, str) or "credentials" in envelope:
                raise ValueError()
            selected = json.loads(_dpapi(base64.b64decode(encrypted, validate=True), decrypt=True).decode("utf-8"))
        else:
            if "encrypted" in envelope:
                raise ValueError()
            selected = envelope.get("credentials")
        return _normalize(selected, required=True, reject_unknown=True)
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise CredentialError("Saved ScreenScraper credentials are unreadable or invalid", code="invalid_store") from None


def forget_credentials(path=None):
    """Remove only this credential file; do not alter any environment variables."""
    target = _path(path)
    try:
        existed = target.exists()
        if existed:
            _private(target.parent, directory=True)
            _private(target)
            target.unlink()
        return {**_summary({}, storage="none", protection="none"), "forgotten": existed}
    except CredentialError:
        raise
    except OSError:
        raise CredentialError("Cannot remove the private credential store", code="forget_failed") from None


def _resolve(env=None, path=None):
    explicit_env = env is not None
    values = os.environ if env is None else env
    override = _normalize(values, required=False)
    # A supplied environment is isolated unless its caller explicitly supplies a
    # file. This prevents env={} unit tests from reading a real user's secrets.
    complete_override = all(field in override for field in DEVELOPER_FIELDS)
    all_pairs_override = all(field in override for field in FIELDS)
    saved = {}
    if not all_pairs_override and (path is not None or not explicit_env):
        try:
            selected_path = _path(path, values)
            saved = load_credentials(selected_path)
        except CredentialError:
            # A complete temporary developer pair must remain usable even if
            # old saved credentials are corrupt or inaccessible. Without an
            # override, the same failure remains explicit and never ready.
            if not complete_override:
                raise
    result = dict(saved)
    for pair in (DEVELOPER_FIELDS, USER_FIELDS):
        if all(field in override for field in pair):
            result.update({field: override[field] for field in pair})
    source = "environment" if override else "saved" if saved else "none"
    protection = _protection() if saved else "none"
    return result, source, protection


def resolve_credentials(env=None, path=None):
    """Use whole environment pairs first, otherwise saved whole pairs."""
    return _resolve(env, path)[0]


def credential_status(env=None, path=None):
    """Public flags only. ``ready`` means locally complete, not API-validated."""
    try:
        values, source, protection = _resolve(env, path)
        return _summary(values, storage=source, protection=protection)
    except CredentialError as exc:
        return {**_summary({}, storage="error", protection="none"), "error": exc.code, "message": str(exc), "missing": exc.missing or list(DEVELOPER_FIELDS)}
