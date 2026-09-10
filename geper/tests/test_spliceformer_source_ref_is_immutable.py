"""
SpliceFormer's pinned source ref must be an IMMUTABLE commit, not a tag.

`DEFAULT_SOURCE_REF` is interpolated straight into a raw.githubusercontent
URL, so it decides which bytes a deployment downloads. It was `"v1.0.0"`,
and the comment beside it claimed that pinning the tag meant "the exact
source/weights GEPER downloads never silently change underneath a running
deployment".

*** THAT CLAIM IS FALSE FOR A TAG. *** A git tag is a movable label: the
upstream owner can repoint `v1.0.0` at different content at any time, and
`v1.0.0` is a LIGHTWEIGHT tag here (verified against the real repository --
`git ls-remote` returns no peeled `^{}` line), so it points directly at a
commit and can simply be reassigned. Two deployments that both recorded
"spliceformer v1.0.0" could therefore have run different weights, and
nothing in the provenance record would show it. Every other model in this
codebase pins something immutable -- ESM-2, HyenaDNA, Enformer and Borzoi
all pin 40-hex commit shas, and SpliceBERT pins a fixed Zenodo record.

This is the same defect as recording a config pin and calling it an
observation, one level up: a mutable pointer is under-specified, and the
label is the one thing that cannot change when the content does.

The tag is kept as a HUMAN-READABLE LABEL, because it is genuinely useful
(it names the Zenodo-archived release) -- it just must not be the thing
the download resolves through.
"""

import re
import unittest

from pipeline.models.spliceformer import loader as spliceformer_loader

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class SpliceFormerSourceRefTest(unittest.TestCase):
    def test_source_ref_is_an_immutable_commit_sha(self):
        self.assertRegex(
            spliceformer_loader.DEFAULT_SOURCE_REF,
            _COMMIT_SHA,
            "DEFAULT_SOURCE_REF decides which bytes are downloaded; a movable tag "
            "means two runs recording the same ref may have used different weights",
        )

    def test_the_release_tag_survives_as_a_human_label(self):
        """Losing the tag would lose the link to the Zenodo-archived release."""
        self.assertEqual(spliceformer_loader.DEFAULT_SOURCE_TAG, "v1.0.0")

    def test_checkpoint_url_resolves_through_the_immutable_ref(self):
        url = spliceformer_loader.checkpoint_url()
        self.assertIn(spliceformer_loader.DEFAULT_SOURCE_REF, url)
        self.assertNotIn(
            "/v1.0.0/",
            url,
            "the download must not resolve through the movable tag",
        )


if __name__ == "__main__":
    unittest.main()
