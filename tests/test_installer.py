import base64
import gzip
import hashlib
import importlib.util
import io
from pathlib import Path
import re
import tarfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('builder', ROOT / 'scripts/build-installer.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class InstallerTests(unittest.TestCase):
    def test_single_file_contains_all_runtime_dependencies(self):
        text = (ROOT / 'install.sh').read_text()
        encoded = text.split("<<'IKEV2_VPN_PAYLOAD'\n", 1)[1].split('\nIKEV2_VPN_PAYLOAD', 1)[0]
        packed = base64.b64decode(encoded)
        checksum = re.search(r'^PAYLOAD_SHA256=([a-f0-9]{64})$', text, re.M).group(1)
        self.assertEqual(hashlib.sha256(packed).hexdigest(), checksum)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(packed))) as archive:
            self.assertEqual(set(archive.getnames()), set(builder.FILES))
            for target, source in builder.FILES.items():
                self.assertEqual(archive.extractfile(target).read(), (ROOT / source).read_bytes())
                self.assertFalse(Path(target).is_absolute())
                self.assertNotIn('..', Path(target).parts)
        self.assertNotIn('git clone', text)

    def test_generated_entry_is_current_and_reproducible(self):
        text = (ROOT / 'install.sh').read_text()
        self.assertEqual(text, builder.build(), 'run python3 scripts/build-installer.py')
        self.assertEqual(builder.build(), builder.build())


if __name__ == '__main__':
    unittest.main()
