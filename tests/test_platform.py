from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = re.search(r'(platform_supported\(\) \{.*?\n\})\n',
                     (ROOT / 'scripts/install-body.sh').read_text(), re.S).group(1)


class PlatformTests(unittest.TestCase):
    def supported(self, distro, version):
        result = subprocess.run(['bash', '-c', FUNCTION + '\nplatform_supported "$1" "$2"',
                                 'test', distro, version], capture_output=True, text=True)
        return result.returncode == 0

    def test_minimum_and_later_debian_ubuntu_versions(self):
        for distro, version in [('debian', '12'), ('debian', '12.9'), ('debian', '13'),
                                ('debian', '14'), ('ubuntu', '22.04'), ('ubuntu', '22.10'),
                                ('ubuntu', '24.04'), ('ubuntu', '26.04'), ('ubuntu', '28.04')]:
            with self.subTest(distro=distro, version=version):
                self.assertTrue(self.supported(distro, version))

    def test_old_unidentified_and_other_distributions_are_rejected(self):
        for distro, version in [('debian', '11'), ('ubuntu', '20.04'), ('ubuntu', '21.10'),
                                ('rhel', '9'), ('rocky', '9'), ('almalinux', '9'),
                                ('centos', '9'), ('fedora', '43'), ('linuxmint', '22'),
                                ('arch', ''), ('debian', ''), ('ubuntu', 'invalid')]:
            with self.subTest(distro=distro, version=version):
                self.assertFalse(self.supported(distro, version))


if __name__ == '__main__':
    unittest.main()
