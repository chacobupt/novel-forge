import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from atomic_context import compile_pack
from read_source_segment import segment

class ContextTests(unittest.TestCase):
    def test_budget_hash_visibility_and_complete_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'facts.json'
            p.write_text(json.dumps({'fact': 'known fact', 'plan': 'candidate plan'}))
            row = {'id': 'fact', 'path': 'facts.json', 'pointer': '/fact',
                   'status': 'canon', 'visible_to': ['writer'], 'required': True,
                   'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            optional = {**row, 'id': 'extra', 'pointer': '/plan', 'required': False}
            m = {'records': [row, optional]}
            result = compile_pack(m, folder, 'writer', 10)
            self.assertEqual(result['records'][0]['text'], 'known fact')
            self.assertEqual(result['omitted_optional'], ['extra'])
            with self.assertRaises(ValueError): compile_pack(m, folder, 'writer', 9)
            with self.assertRaises(ValueError): compile_pack(m, folder, 'reader', 50)
            p.write_text('{}')
            with self.assertRaises(ValueError): compile_pack(m, folder, 'writer', 50)

    def test_whole_line_resume(self):
        rows, count = segment(['abc', 'def', 'ghi'], 1, 3, 8)
        self.assertEqual(rows, [(1, 'abc'), (2, 'def')])
        self.assertEqual(count, 8)
        self.assertEqual(segment(['abc', 'def', 'ghi'], rows[-1][0] + 1, 3)[0], [(3, 'ghi')])
        with self.assertRaises(ValueError): segment(['oversized'], 1, 1, 2)

if __name__ == '__main__':
    unittest.main()
