import unittest

class TestFirstTest(unittest.TestCase):

    def test_firstTest(self):
        self.assertTrue(True)
        self.assertFalse(False)
        self.assertEqual("ABC","ABC")
        self.assertTrue(True and True)

