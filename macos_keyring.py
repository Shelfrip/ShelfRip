"""OpenSubtitles passwords in macOS Keychain via Security.framework, never argv."""
from __future__ import annotations

import ctypes as C
import re
import threading
import uuid

from macos_native import CoreFoundation, bind, framework

PREFIX = "keychain:"
SERVICE = b"org.shelfrip.opensubtitles"
WARNING = ""
_lock = threading.Lock()


class KeyringError(ValueError):
    pass


def is_reference(value):
    return bool(re.fullmatch(r"keychain:[0-9a-f]{32}", value or ""))


class Keychain:
    """Generic-password API, also usable with an isolated keychain in native tests."""
    def __init__(self, path=None):
        self.path = path

    def run(self, action, reference, password=None):
        if not is_reference(reference):
            raise KeyringError("Invalid Keychain reference. Save the password again.")
        sec, cf = framework("Security"), CoreFoundation()
        get_ui = bind(sec, "SecKeychainGetUserInteractionAllowed", C.c_int32, C.c_void_p)
        set_ui = bind(sec, "SecKeychainSetUserInteractionAllowed", C.c_int32, C.c_ubyte)
        keychain, item, data, size = C.c_void_p(), C.c_void_p(), C.c_void_p(), C.c_uint32()
        account = reference[len(PREFIX):].encode("ascii")

        def check(result):
            if result:
                # Never return backend output or the password in error messages.
                raise KeyringError(f"Keychain could not complete the request (status {result}). Unlock your login keychain and try again.")

        with _lock:
            allowed = C.c_ubyte()
            check(get_ui(C.byref(allowed)))
            check(set_ui(False))  # no invisible unlock prompt blocking a background server
            try:
                if self.path:
                    check(bind(sec, "SecKeychainOpen", C.c_int32, C.c_char_p, C.c_void_p)(
                        str(self.path).encode(), C.byref(keychain)))
                if action == "store":
                    encoded = password.encode("utf-8")
                    check(bind(sec, "SecKeychainAddGenericPassword", C.c_int32, C.c_void_p,
                               C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_void_p)(
                        keychain, len(SERVICE), SERVICE, len(account), account, len(encoded), encoded, None))
                    return ""
                result = bind(sec, "SecKeychainFindGenericPassword", C.c_int32, C.c_void_p,
                              C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_void_p, C.c_void_p, C.c_void_p)(
                    keychain, len(SERVICE), SERVICE, len(account), account, C.byref(size), C.byref(data), C.byref(item))
                if action == "clear" and result == -25300:  # already removed
                    return ""
                check(result)
                if action == "clear":
                    check(bind(sec, "SecKeychainItemDelete", C.c_int32, C.c_void_p)(item))
                    return ""
                return C.string_at(data, size.value).decode("utf-8")
            finally:
                if data:
                    bind(sec, "SecKeychainItemFreeContent", C.c_int32, C.c_void_p, C.c_void_p)(None, data)
                if item:
                    cf.release(item)
                if keychain:
                    cf.release(keychain)
                set_ui(allowed.value)


def _run(action, reference, password=None):
    try:
        return Keychain().run(action, reference, password)
    except (OSError, AttributeError, UnicodeError):
        raise KeyringError("Keychain is unavailable. Unlock your login keychain and save the password again.") from None


def store(password):
    reference = PREFIX + uuid.uuid4().hex
    try:
        _run("store", reference, password)
        if lookup(reference) != password:
            raise KeyringError("Keychain did not return the saved password. Settings were not changed.")
    except KeyringError:
        cleanup(reference)
        raise
    return reference


def lookup(reference):
    return _run("lookup", reference)


def cleanup(reference):
    global WARNING
    if not is_reference(reference):
        return
    try:
        _run("clear", reference)
        WARNING = ""
    except KeyringError:
        WARNING = "An unused Shelfrip password item could not be removed. Remove it in Keychain Access."


def status():
    # Settings polling must not read secrets or attempt to unlock anything.
    return {"helper_available": True, "warning": WARNING}
