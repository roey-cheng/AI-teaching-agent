"""真实 Argon2id 哈希验证；不连接数据库或调用模型。"""

import unittest
from unittest.mock import patch

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import HashingError, VerifyMismatchError
from argon2.low_level import Type
from pydantic import SecretStr

from app.core.passwords import PasswordHashError, hash_password


class PasswordTest(unittest.TestCase):
    def test_hash_is_argon2id_fits_column_and_verifies_original_password(self):
        original = "  Case-Sensitive 密码🙂  "
        hashed = hash_password(SecretStr(original))
        self.assertTrue(hashed.startswith("$argon2id$"))
        self.assertLessEqual(len(hashed), 255)
        self.assertTrue(hashed.isascii())
        self.assertNotIn(original, hashed)
        params = extract_parameters(hashed)
        self.assertEqual((params.type, params.memory_cost, params.time_cost, params.parallelism),
                         (Type.ID, 65536, 3, 4))
        verifier = PasswordHasher()
        self.assertTrue(verifier.verify(hashed, original))
        for wrong in (original.strip(), original.lower(), "**********", "incorrect"):
            with self.subTest(wrong=wrong), self.assertRaises(VerifyMismatchError):
                verifier.verify(hashed, wrong)

    def test_same_password_has_randomized_hashes(self):
        password = SecretStr("fixture-password")
        first, second = hash_password(password), hash_password(password)
        self.assertNotEqual(first, second)
        self.assertTrue(PasswordHasher().verify(first, password.get_secret_value()))
        self.assertTrue(PasswordHasher().verify(second, password.get_secret_value()))

    def test_128_unicode_characters_are_not_truncated(self):
        password = "密" * 128
        hashed = hash_password(SecretStr(password))
        self.assertTrue(PasswordHasher().verify(hashed, password))

    def test_hash_failure_hides_underlying_error(self):
        with patch("app.core.passwords._hasher") as hasher:
            hasher.hash.side_effect = HashingError("fixture-secret")
            with self.assertRaises(PasswordHashError) as caught:
                hash_password(SecretStr("fixture-password"))
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
