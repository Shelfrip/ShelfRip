"""Small CoreFoundation bindings shared by macOS integration (stdlib only)."""
from __future__ import annotations

import ctypes as C
from functools import lru_cache


@lru_cache(maxsize=None)
def framework(name):
    return C.CDLL(f"/System/Library/Frameworks/{name}.framework/{name}")


def bind(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


class CoreFoundation:
    def __init__(self):
        self.lib = framework("CoreFoundation")
        self.release = bind(self.lib, "CFRelease", None, C.c_void_p)
        self.type_id = bind(self.lib, "CFGetTypeID", C.c_ulong, C.c_void_p)
        self.types = {name: bind(self.lib, f"CF{name}GetTypeID", C.c_ulong)()
                      for name in ("String", "Data", "Array", "Dictionary", "Boolean", "Number")}
        self.count = bind(self.lib, "CFArrayGetCount", C.c_long, C.c_void_p)
        self.at = bind(self.lib, "CFArrayGetValueAtIndex", C.c_void_p, C.c_void_p, C.c_long)

    def string(self, text):
        return bind(self.lib, "CFStringCreateWithCString", C.c_void_p, C.c_void_p, C.c_char_p, C.c_uint32)(
            None, text.encode("utf-8"), 0x08000100)

    def value(self, ref):
        if not ref:
            return None
        kind = self.type_id(ref)
        if kind == self.types["String"]:
            length = bind(self.lib, "CFStringGetLength", C.c_long, C.c_void_p)(ref)
            buf = C.create_string_buffer(length * 4 + 1)
            ok = bind(self.lib, "CFStringGetCString", C.c_bool, C.c_void_p, C.c_void_p, C.c_long, C.c_uint32)(
                ref, buf, len(buf), 0x08000100)
            if not ok:
                raise ValueError("Cannot decode a macOS string.")
            return buf.value.decode("utf-8")
        if kind == self.types["Data"]:
            size = bind(self.lib, "CFDataGetLength", C.c_long, C.c_void_p)(ref)
            ptr = bind(self.lib, "CFDataGetBytePtr", C.c_void_p, C.c_void_p)(ref)
            return C.string_at(ptr, size)
        if kind == self.types["Array"]:
            return [self.value(self.at(ref, i)) for i in range(self.count(ref))]
        if kind == self.types["Dictionary"]:
            count = bind(self.lib, "CFDictionaryGetCount", C.c_long, C.c_void_p)(ref)
            keys, values = (C.c_void_p * count)(), (C.c_void_p * count)()
            bind(self.lib, "CFDictionaryGetKeysAndValues", None, C.c_void_p, C.c_void_p, C.c_void_p)(ref, keys, values)
            # Some framework dictionaries contain opaque object keys, not plist keys.
            return {key: self.value(v) for k, v in zip(keys, values)
                    if isinstance(key := self.value(k), str)}
        if kind == self.types["Boolean"]:
            return bool(bind(self.lib, "CFBooleanGetValue", C.c_bool, C.c_void_p)(ref))
        if kind == self.types["Number"]:
            number = C.c_int64()
            bind(self.lib, "CFNumberGetValue", C.c_bool, C.c_void_p, C.c_int, C.c_void_p)(ref, 4, C.byref(number))
            return number.value
        return None  # e.g. DiscRecording track references / MSF objects

    def take(self, ref):
        try:
            return self.value(ref)
        finally:
            if ref:
                self.release(ref)
