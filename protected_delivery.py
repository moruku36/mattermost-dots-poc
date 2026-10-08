"""Explicit DPAPI CurrentUser storage and public-address-pinned HTTPS delivery.

Import has no credential, filesystem or network effects.
"""
import base64
import ctypes
import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit

from notification_core import Refused


class Dpapi:
    def _crypt(self, value, decrypt):
        if os.name != 'nt' or type(value) is not bytes:
            raise Refused('DPAPI_PLATFORM_REFUSED')
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        source = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        entropy_value = b'Mattermost.NativeEvents.v1'
        entropy = (ctypes.c_ubyte * len(entropy_value)).from_buffer_copy(entropy_value)
        incoming, salt, outgoing = Blob(len(value), source), Blob(len(entropy_value), entropy), Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        try:
            if not function(ctypes.byref(incoming), None, ctypes.byref(salt), None, None, 1, ctypes.byref(outgoing)):
                raise Refused('DPAPI_REFUSED')
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            ctypes.memset(source, 0, len(value))
            if outgoing.data:
                ctypes.memset(outgoing.data, 0, outgoing.size)
                kernel.LocalFree(outgoing.data)

    def seal(self, value):
        return 'dpapi:' + base64.b64encode(self._crypt(value.encode('utf-8'), False)).decode()

    def open(self, value):
        if type(value) is not str or not value.startswith('dpapi:'):
            raise Refused('PROTECTED_VALUE_REQUIRED')
        try:
            return self._crypt(base64.b64decode(value[6:], validate=True), True).decode('utf-8')
        except (ValueError, UnicodeError):
            raise Refused('PROTECTED_VALUE_REFUSED') from None


def callback_target(url):
    if type(url) is not str or len(url) > 4096 or any(c in url for c in '\r\n\0'):
        raise Refused('CALLBACK_URL_REFUSED')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443) or '%' in parsed.hostname):
            raise ValueError()
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError()
        return parsed
    except ValueError:
        raise Refused('CALLBACK_URL_REFUSED') from None


def public_addresses(host, resolver=socket.getaddrinfo):
    addresses = []
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM)
        for family, _, _, _, target in records:
            address = target[0]
            ip = ipaddress.ip_address(address)
            if family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global or '%' in address:
                raise ValueError()
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ValueError()
        return addresses
    except (ValueError, OSError):
        raise Refused('CALLBACK_ADDRESS_REFUSED') from None


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # No second DNS resolution, proxy, redirects or hostname substitution.
        raw = socket.create_connection((self.address, 443), timeout=10)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class ProtectedHTTPS:
    def validate_url(self, url):
        callback_target(url)

    def post(self, url, headers, body):
        parsed = callback_target(url)
        if type(body) is not bytes or len(body) > 262144:
            raise Refused('DELIVERY_SIZE_REFUSED')
        addresses = public_addresses(parsed.hostname)
        connection = _PinnedHTTPS(parsed.hostname, addresses[0])
        try:
            path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            connection.request('POST', path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise Refused('CALLBACK_RESPONSE_BOUND')
            import json
            try:
                value = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                value = {}
            return response.status, value
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise ConnectionError('CALLBACK_TRANSPORT_FAILED') from None
        finally:
            connection.close()
