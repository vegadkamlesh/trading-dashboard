import unittest
from unittest.mock import patch

from app import dhan_client
from app.config import Settings


class SourceIpBindingTests(unittest.TestCase):
    def test_each_account_uses_its_own_source_ip(self):
        settings = Settings(
            _env_file=None, dhan_master_source_ip="10.0.0.1", dhan_child_source_ip=" 10.0.0.2 ",
        )
        with patch.object(dhan_client, "get_settings", return_value=settings):
            self.assertEqual(dhan_client.source_ip_for("master"), "10.0.0.1")
            self.assertEqual(dhan_client.source_ip_for("master-data"), "10.0.0.1")
            self.assertEqual(dhan_client.source_ip_for("child"), "10.0.0.2")
            self.assertEqual(dhan_client.source_ip_for("other"), "")

    def test_unset_source_ip_keeps_default_routing(self):
        with patch.object(dhan_client, "get_settings", return_value=Settings(_env_file=None)):
            self.assertEqual(dhan_client.source_ip_for("child"), "")
        self.assertIsNone(dhan_client._transport(""))
        self.assertIsNotNone(dhan_client._transport("10.0.0.2"))


if __name__ == "__main__":
    unittest.main()
