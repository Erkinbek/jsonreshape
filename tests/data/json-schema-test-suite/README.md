# Vendored: JSON-Schema-Test-Suite (draft 2020-12)

Source: the official JSON-Schema-Test-Suite (github.com/json-schema-org/JSON-Schema-Test-Suite,
MIT license, see `LICENSE`), as shipped in the `json/` folder of the `jsonschema` 4.26.0 sdist.

* `tests/draft2020-12/` - every draft 2020-12 test file (incl. `optional/` and `optional/format/`);
* `remotes/` - the remote documents those tests reference (`http://localhost:1234/...`), served to
  the validator offline through `compile_schema(..., resources=...)`.

Used by `tests/test_jschema_suite.py` (conformance of `jsonreshape.jschema`). Do not edit the files.
