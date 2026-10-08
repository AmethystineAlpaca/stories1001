"""Store Azure settings in the macOS login Keychain, never in argv or files."""
import ctypes as C
import json
import threading

SERVICE = b'stories1001.azure-speech'
ACCOUNT = b'local-workbench'
LOCK = threading.Lock()
SEC = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
CF = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
P, U = C.c_void_p, C.c_uint32
SEC.SecKeychainFindGenericPassword.argtypes = [P, U, C.c_char_p, U, C.c_char_p, C.POINTER(U), C.POINTER(P), C.POINTER(P)]
SEC.SecKeychainAddGenericPassword.argtypes = [P, U, C.c_char_p, U, C.c_char_p, U, P, C.POINTER(P)]
SEC.SecKeychainItemModifyAttributesAndData.argtypes = [P, P, U, P]
SEC.SecKeychainItemFreeContent.argtypes = [P, P]
CF.CFRelease.argtypes = [P]


def check(status):
    if status:
        raise OSError(f'钥匙串操作未完成（{status}）。请解锁登录钥匙串，并允许本机 Python 访问。')


def exists():
    with LOCK:
        status = SEC.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, None, None, None)
        if status == -25300:
            return False
        check(status)
        return True


def load():
    with LOCK:
        length, buffer = U(), P()
        status = SEC.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, C.byref(length), C.byref(buffer), None)
        if status == -25300:
            return {'region': 'eastus', 'key': ''}
        check(status)
        try:
            return json.loads(C.string_at(buffer, length.value).decode())
        finally:
            SEC.SecKeychainItemFreeContent(None, buffer)


def save(settings):
    payload = json.dumps(settings).encode()
    with LOCK:
        item = P()
        status = SEC.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, None, None, C.byref(item))
        if status == -25300:
            check(SEC.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT, len(payload), payload, None))
        else:
            check(status)
            try:
                check(SEC.SecKeychainItemModifyAttributesAndData(item, None, len(payload), payload))
            finally:
                CF.CFRelease(item)
