"""hindsight_auth: the one resolver every Hindsight client uses for URL + key."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hindsight_auth as auth  # noqa: E402


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HINDSIGHT_")}
        clean["HINDSIGHT_CONFIG"] = str(self.config)
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth._VAULT.clear()
        self.addCleanup(auth._VAULT.clear)
        self.reads: list[str] = []

        def fake_read(ref):
            self.reads.append(ref)
            return ("vaulted-key", "") if ref.startswith("op://") else ("", "bad ref")

        reader = mock.patch.object(auth, "vault_read", side_effect=fake_read)
        reader.start()
        self.addCleanup(reader.stop)

    def test_env_wins_and_never_touches_the_vault(self):
        os.environ.update(HINDSIGHT_API_URL="http://env/", HINDSIGHT_API_KEY="env-key")
        self.config.write_text('api_url = "http://config"\napi_key_ref = "op://x/y/z"\n')
        found = auth.resolve()
        self.assertEqual((found.url, found.key, found.url_source, found.key_source),
                         ("http://env", "env-key", "env", "env"))
        self.assertEqual(self.reads, [])

    def test_a_wiped_config_still_resolves_through_the_default_ref(self):
        found = auth.resolve()
        self.assertEqual((found.url, found.key, found.key_source),
                         (auth.DEFAULT_URL, "vaulted-key", "vault:default-ref"))
        self.assertEqual(self.reads, [auth.DEFAULT_KEY_REF])

    def test_config_ref_beats_the_default(self):
        self.config.write_text('api_url = "http://config"\napi_key_ref = "op://DeLoSecrets/item/field"\n')
        found = auth.resolve()
        self.assertEqual((found.url, found.key_source), ("http://config", "vault:config-ref"))
        self.assertEqual(self.reads, ["op://DeLoSecrets/item/field"])

    def test_env_ref_beats_config_ref_and_empty_disables_the_vault(self):
        self.config.write_text('api_key_ref = "op://config/ref/x"\n')
        os.environ["HINDSIGHT_API_KEY_REF"] = "op://env/ref/x"
        self.assertEqual(auth.resolve().key_source, "vault:env-ref")
        os.environ["HINDSIGHT_API_KEY_REF"] = ""
        found = auth.resolve()
        self.assertEqual((found.key, found.key_source), ("", "none"))
        self.assertIn("disabled", found.error)

    def test_a_plaintext_config_key_still_works_but_is_labelled(self):
        self.config.write_text('api_key = "literal"\n')
        self.assertEqual(auth.resolve().key_source, "config-plaintext")

    def test_an_op_ref_in_api_key_is_read_from_the_vault(self):
        self.config.write_text('api_key = "op://a/b/c"\n')
        self.assertEqual(auth.resolve().key_source, "vault:config-api_key-ref")

    def test_cli_environment_carries_the_resolved_key(self):
        env = auth.environment({"PATH": "/usr/bin"})
        self.assertEqual(env["HINDSIGHT_API_KEY"], "vaulted-key")
        self.assertEqual(env["HINDSIGHT_API_URL"], auth.DEFAULT_URL)
        self.assertEqual(env["PATH"], "/usr/bin")


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config"

    def test_writer_keeps_every_other_key_comment_and_table(self):
        self.config.write_text('# note\napi_url = "old"\napi_key_ref = "op://keep/me/x"\nother = 1\n[t]\napi_url = "nested"\n')
        auth.write_config({"api_url": "https://hs-api.delo.sh"}, self.config)
        text = self.config.read_text()
        self.assertIn('api_key_ref = "op://keep/me/x"', text)
        self.assertIn("# note", text)
        self.assertIn("other = 1", text)
        self.assertIn('[t]\napi_url = "nested"', text, "a table's keys are not top-level keys")
        self.assertIn('api_url = "https://hs-api.delo.sh"', text)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_new_keys_land_before_the_first_table_and_none_removes(self):
        self.config.write_text('api_key = "x"\n[t]\ny = 2\n')
        auth.write_config({"api_key": None, "api_key_ref": "op://a/b/c"}, self.config)
        self.assertEqual(self.config.read_text(), 'api_key_ref = "op://a/b/c"\n[t]\ny = 2\n')

    def test_configure_refuses_a_literal_key(self):
        env = {**os.environ, "HINDSIGHT_CONFIG": str(self.config)}
        done = subprocess.run([sys.executable, auth.__file__, "configure", "--api-key", "secret"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertFalse(self.config.exists())

    def test_key_refuses_a_terminal_but_url_does_not_need_one(self):
        env = {**os.environ, "HINDSIGHT_CONFIG": str(self.config), "HINDSIGHT_API_URL": "http://u"}
        done = subprocess.run([sys.executable, auth.__file__, "url"], env=env, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "http://u")


if __name__ == "__main__":
    unittest.main()
