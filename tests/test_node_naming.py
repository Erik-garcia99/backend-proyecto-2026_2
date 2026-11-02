import unittest

from app.node_registration import default_node_name


class NodeNamingTests(unittest.TestCase):
    def test_default_name_is_readable_and_distinct_per_mac(self):
        first = default_node_name('00:4B:12:8E:B9:88', 'guide')
        second = default_node_name('00:4B:12:90:1B:98', 'guide')
        self.assertEqual(first, 'Guía de luces B9:88')
        self.assertEqual(second, 'Guía de luces 1B:98')
        self.assertNotEqual(first, second)

    def test_unknown_type_still_gets_a_name(self):
        self.assertEqual(default_node_name('AA:BB:CC:DD:EE:FF', 'otro'), 'Dispositivo EE:FF')
