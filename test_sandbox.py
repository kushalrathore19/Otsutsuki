import unittest
import subprocess
import resource

class TestSandbox(unittest.TestCase):
    def test_set_limits(self):
        def set_limits():
            try:
                mem_limit = 1024 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
                if hasattr(resource, 'RLIMIT_NPROC'):
                    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
            except Exception:
                pass
        
        result = subprocess.run("echo hello", shell=True, preexec_fn=set_limits, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)

if __name__ == '__main__':
    unittest.main()
