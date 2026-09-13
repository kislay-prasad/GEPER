"""
clinical/report_revision_shape.py
──────────────────────────────────

THE ONE WRITTEN-DOWN CONTRACT between
`clinical.data_access.DataAccess.get_report_revision` (which reads the
amendment / re-analysis records out of the database) and
`geper/report/clinical_report_builder.py::normalize_report_revision` (which
turns those facts into the signed-off banner wording).

WHY THIS FILE EXISTS. The two halves live in different deployment units, and
they cannot import each other:

  clinical -> geper   is impossible IN CI. `clinical_report_builder` imports
      `pipeline.acmg_rules`, which imports `pipeline.gnomad.models`, which
      runs `pipeline/gnomad/__init__.py`, which imports the gnomAD HTTP
      provider and through it `requests`. The clinical job installs
      clinical/requirements.txt and has no `requests`, and it must not get
      one: the clinical layer running its own tests must not depend on the
      variant pipeline. (MEASURED: CI run 34755731014, merge d389012 --
      "ModuleNotFoundError: No module named 'requests'",
      ../geper/pipeline/gnomad/provider.py:34.)

  geper -> clinical   is the wrong direction: the renderer must keep working
      for callers that have no clinical database at all.

So the contract is THIS MODULE, and it imports NOTHING. Both test suites load
it (geper's by path, from the repository root) and each pins its own half
against it. Change a key name in the reader and `clinical/tests/
test_w118_report_revision_reader.py` fails; change a key name here and
`geper/tests/test_w118_banner_rulings.py` fails because its banner fixtures
no longer match. There is no way to move one half and leave the other
silently stale, which is the failure this card is entirely about.

Names only. No types, no validation, no logic -- validation belongs to
`normalize_report_revision`, which is the module that refuses a bad block.
"""

# The four states a document may be in. Any other top-level key is a caller
# inventing a state the renderer has never heard of.
REVISION_KEYS = frozenset({"amends", "superseded_by", "reanalysis_of", "reanalysed_since"})

# Banner 1: this report IS an amendment of another one.
AMENDS_REQUIRED = frozenset(
    {
        "original_report_id",
        "original_issued_at",
        # R2: which date `original_issued_at` actually is. Travels WITH the
        # timestamp, never inferred from which column was populated.
        "original_issued_basis",
        "reason",
        "amended_by",
        "amended_at",
    }
)

# Banner 2: this report HAS BEEN superseded by an amendment.
SUPERSEDED_BY_REQUIRED = frozenset(
    {
        "amendment_report_id",
        "amended_at",
        # R4: the same basis vocabulary as AMENDS, so the two banners cannot
        # disagree about what "issued" means for one pair of documents.
        "amended_at_basis",
        # R8: False once retention has deleted the amended report.
        "retained",
    }
)

# R5: EXACTLY ONE of these accompanies SUPERSEDED_BY_REQUIRED. `reason` when
# the named amendment is the one that superseded THIS document; otherwise
# `amendment_count`, because the latest reason explains a change from a
# document the reader has never seen.
SUPERSEDED_BY_EXACTLY_ONE_OF = frozenset({"reason", "amendment_count"})

# Banner 3: this report IS a re-analysis of an earlier interpretation.
REANALYSIS_OF_REQUIRED = frozenset({"parent_interpretation_id", "parent_interpreted_at"})

# Banner 4: one entry per direct re-analysis created after this report.
REANALYSED_SINCE_ITEM_REQUIRED = frozenset({"interpretation_id", "created_at"})

# R2/R4: the only two things an `*_basis` value may say.
DATE_BASES = frozenset({"released", "approved"})
