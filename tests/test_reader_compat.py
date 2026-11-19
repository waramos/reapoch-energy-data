"""Optional contract check; no tracker source is shipped with the mirror."""
import json

import pytest

from test_build import DAY, output_path, reply, run


def test_reader_accepts_built_file(tmp_path):
    pytest.importorskip('training_tracker')
    from training_tracker.store.prices import validate_mirror

    run(tmp_path, [reply(), {'records': []}])
    document = json.loads(output_path(tmp_path).read_text())
    assert validate_mirror(document, 'energinet:DK1', DAY) == document
