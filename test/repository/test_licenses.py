"""The license check's reading of license texts and its policy."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_licenses", ROOT / "scripts/check_licenses.py")
licenses = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(licenses)

MIT = "Permission is hereby granted, free of charge, to any person obtaining\na copy of this software"
BSD = "Redistribution and use in source and binary forms, with or without modification, are permitted"
BSD3 = BSD + "\n3. Neither the name of the copyright holder nor the names of its\ncontributors may be used"
APACHE = "Apache License\n                           Version 2.0, January 2004"
# Each GNU license and the MPL name the others in their prose.
MPL = ("Mozilla Public License Version 2.0\n1.12. \"Secondary License\"\n    means either the GNU General Public License,\n"
       "Version 2.0, the GNU Lesser General Public License, Version 2.1, the GNU Affero General Public\n"
       "License, Version 3.0, or any later versions of those\n    licenses.")
NOTICE = " The files under lib/ are licensed under the GNU General Public License, version 2."
GPL2 = ("GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991\nIf this is what you want to do, use the GNU Lesser General\n"
        "Public License instead of this License.")
LGPL3 = ("GNU LESSER GENERAL PUBLIC LICENSE\nThis version of the GNU Lesser General Public License incorporates\n"
         "the terms and conditions of version 3 of the GNU General Public License")


class Licenses(unittest.TestCase):
    def test_texts_are_read_by_their_own_wording(self):
        cases = {MIT: ["MIT"], BSD: ["BSD-2-Clause"], BSD3: ["BSD-3-Clause"], APACHE: ["Apache-2.0"],
                 "Permission to use, copy, modify, and/or distribute this software for any purpose": ["ISC"],
                 "GNU GENERAL PUBLIC LICENSE Version 3": ["GPL"], GPL2: ["GPL"], LGPL3: ["LGPL"], MPL: ["MPL-2.0"],
                 "GNU AFFERO GENERAL PUBLIC LICENSE " + MIT: ["AGPL", "MIT"],
                 BSD3 + "\n\n" + APACHE: ["Apache-2.0", "BSD-3-Clause"],
                 MIT + NOTICE: ["MIT", "GNU-notice"], MPL + NOTICE: ["MPL-2.0", "GNU-notice"],
                 APACHE + " parts under the GNU Lesser General\nPublic License": ["Apache-2.0", "GNU-notice"],
                 MIT + " lib/ is GPLv2.": ["MIT", "GNU-notice"], MIT + " lib/ is GPL-2.0-only.": ["MIT", "GNU-notice"],
                 MIT + " lib/ is under the GNU GPL.": ["MIT", "GNU-notice"],
                 MIT + " under the GNU General Public Licence": ["MIT", "GNU-notice"],
                 "All rights reserved.": []}
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(licenses.from_text(text), expected)

    def test_every_license_file_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in (("LICENSE", MIT), ("COPYING.LIB", LGPL3), ("LICENSE-APACHE", APACHE),
                               ("COPYING.GPLv2", GPL2), ("license.go", GPL2), ("LICENSE_test.go", GPL2),
                               ("README", GPL2)):
                (root / name).write_text(text)
            files = licenses.license_files(root)
            self.assertEqual([file.name for file in files], ["COPYING.GPLv2", "COPYING.LIB", "LICENSE", "LICENSE-APACHE"])
            self.assertEqual(licenses.from_texts(files), "Apache-2.0 AND GPL AND LGPL AND MIT")
            (root / "NOTICE.txt").write_text("All rights reserved.")
            (root / "LICENSE.txt").write_text("All rights reserved.")
            self.assertIn("unknown", licenses.from_texts(licenses.license_files(root)))

    def test_license_file_names(self):
        for name in ("LICENSE", "LICENCE.md", "license.txt", "COPYING", "COPYING.LGPLv2.1", "COPYING-LGPL-2.1",
                     "LICENSE-GPL-3.0", "LICENSE.enterprise", "LICENSE_MIT", "COPYING3", "COPYING3.LIB",
                     "GPL-LICENSE", "MIT-LICENSE.txt", "COPYINGv2", "LICENSE2", "LICENSES"):
            with self.subTest(name=name):
                self.assertTrue(licenses.is_license_name(name))
        for name in ("license.go", "license_test.go", "LICENSE.json", "license.cpython-312.pyc", "license.pyi",
                     "README", "NOTICE"):
            with self.subTest(name=name):
                self.assertFalse(licenses.is_license_name(name))

    def test_only_permissive_licenses_and_the_named_exceptions_pass(self):
        for name, expression in (("any", "MIT"), ("any", "Apache-2.0 OR BSD-3-Clause"), ("any", "MIT-0"),
                                 ("any", "BSD-3-Clause OR GPL-2.0-only"), ("any", "(MIT OR GPL-3.0-only) AND ISC"),
                                 ("any", "Apache-2.0 WITH LLVM-exception"), ("pathspec", "MPL-2.0"),
                                 ("bubblewrap", "LGPL-2.0-or-later"), ("glibc", "LGPL-2.1-or-later")):
            with self.subTest(name=name, expression=expression):
                self.assertEqual(licenses.refusals(name, expression), [])
        for name, expression in (("any", "GPL"), ("any", "AGPL"), ("any", "LGPL"), ("any", "unknown"), ("any", ""),
                                 ("any", "MPL-2.0"), ("pathspec", "GPL"), ("bubblewrap", "GPL-3.0-only"),
                                 ("glibc", "LGPL-2.0-or-later"), ("any", "MIT AND GPL-2.0-only"),
                                 ("any", "MIT AND unknown"), ("any", "GNU-notice AND MIT"), ("any", "GPL-2.0-only WITH Classpath-exception-2.0"),
                                 ("any", "(MIT"), ("any", "MIT OR"), ("any", "MIT WITH")):
            with self.subTest(name=name, expression=expression):
                self.assertTrue(licenses.refusals(name, expression))


if __name__ == "__main__":
    unittest.main()
