import base64
import os
import subprocess
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SCRIPT = Path(__file__).with_name('validate_ledger_push.py')


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class TestPushValidation(unittest.TestCase):
    def setUp(self):
        key = ec.generate_private_key(ec.SECP256R1())
        self.env = {
            'VAPID_PRIVATE_KEY': encoded(key.private_numbers().private_value.to_bytes(32, 'big')),
            'VAPID_PUBLIC_KEY': encoded(key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
            'PUSH_SUBSCRIPTION_KEY': encoded(os.urandom(32)),
            'VAPID_SUBJECT': 'https://ledger.example.com/',
        }

    def check_config(self, valid):
        result = subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ, **self.env}, capture_output=True)
        self.assertEqual(result.returncode == 0, valid)
        self.assertFalse(any(value.encode() in result.stdout + result.stderr for value in self.env.values()))

    def test_valid_configuration(self):
        self.check_config(True)

    def test_mismatched_pair(self):
        other = ec.generate_private_key(ec.SECP256R1())
        self.env['VAPID_PUBLIC_KEY'] = encoded(other.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
        self.check_config(False)

    def test_invalid_encryption_key(self):
        self.env['PUSH_SUBSCRIPTION_KEY'] = encoded(os.urandom(31))
        self.check_config(False)

    def test_invalid_subject(self):
        self.env['VAPID_SUBJECT'] = 'http://example.com'
        self.check_config(False)


if __name__ == '__main__':
    unittest.main()
