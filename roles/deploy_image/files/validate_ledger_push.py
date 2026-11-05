"""Fail closed on invalid Ledger Web Push configuration; never print values."""
import base64
import os
import sys
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def decode(name):
    value = os.environ[name]
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def validate():
    public = decode('VAPID_PUBLIC_KEY')
    private = decode('VAPID_PRIVATE_KEY')
    key = decode('PUSH_SUBSCRIPTION_KEY')
    if len(private) != 32 or len(key) != 32:
        raise ValueError('Invalid key length')
    derived = ec.derive_private_key(int.from_bytes(private, 'big'), ec.SECP256R1())
    expected = derived.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    if public != expected:
        raise ValueError('VAPID key mismatch')
    subject = urlsplit(os.environ['VAPID_SUBJECT'])
    if not ((subject.scheme == 'mailto' and '@' in subject.path and not subject.netloc)
            or (subject.scheme == 'https' and subject.hostname and not subject.username and not subject.password)):
        raise ValueError('Invalid VAPID subject')


if __name__ == '__main__':
    try:
        validate()
    except Exception:
        print('Invalid Web Push configuration', file=sys.stderr)
        sys.exit(1)
