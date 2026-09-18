import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gemini_bot.storage import shared_control_directory


class ControlLocationTests(unittest.TestCase):
    def test_control_is_outside_appdata_and_independent_of_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / 'user'
            appdata = profile / 'AppData/Local'
            with patch.dict(os.environ, USERPROFILE=str(profile), LOCALAPPDATA=str(appdata)):
                expected = profile / 'GeminiRewardBot/control'
                self.assertEqual(shared_control_directory(), expected)
                with patch.dict(os.environ, LOCALAPPDATA=str(profile / 'AppData/Local/Packages/Code/LocalCache/Local')):
                    self.assertEqual(shared_control_directory(), expected)

    def test_legacy_database_requires_explicit_migration_not_empty_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            legacy = profile / 'AppData/Local/GeminiRewardBot/control'
            legacy.mkdir(parents=True)
            (legacy / 'production.sqlite').touch()
            with patch.dict(os.environ, USERPROFILE=str(profile), LOCALAPPDATA=str(profile / 'AppData/Local')):
                with self.assertRaisesRegex(RuntimeError, 'migration required'):
                    shared_control_directory()
                self.assertFalse((profile / 'GeminiRewardBot/control').exists())

    def test_migrated_path_never_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            control = profile / 'GeminiRewardBot/control'
            control.mkdir(parents=True)
            (control / 'production.sqlite').touch()
            with patch.dict(os.environ, USERPROFILE=str(profile), LOCALAPPDATA=str(profile / 'AppData/Local')):
                self.assertEqual(shared_control_directory(), control)
