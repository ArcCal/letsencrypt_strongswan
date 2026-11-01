"""Repository file validation."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '__pycache__', '.pytest_cache'}
IMAGE_TYPES = {'.png', '.jpg', '.jpeg'}


class RepositoryTests(unittest.TestCase):
    def test_filenames_and_text_are_ascii_only(self):
        checked = 0
        for path in ROOT.rglob('*'):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED for part in relative.parts):
                continue
            self.assertTrue(str(relative).isascii(), f'Non-ASCII path: {relative!r}')
            if not path.is_file() or path.suffix.lower() in IMAGE_TYPES:
                continue
            with self.subTest(path=str(relative)):
                text = path.read_text(encoding='utf-8')
                self.assertTrue(text.isascii(), f'Non-ASCII text in {relative}')
                checked += 1
        self.assertGreater(checked, 10)


if __name__ == '__main__':
    unittest.main()
