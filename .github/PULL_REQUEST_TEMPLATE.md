**What changes and why**


**Tests**
- [ ] new / changed behaviour has a test; a bug fix has a regression test that failed before the fix
- [ ] `pytest -q -m "not gui and not bench"` passes locally (and the GUI tests of the touched modules)

**Documentation**
- [ ] `README.md` and `README.uz.md` updated together (same sections), `docs/ARCHITECTURE.md` when the design changed
- [ ] `CHANGELOG.md` has a line under "Unreleased"
- [ ] new user-visible texts have both `uz` and `en` entries in `jsonreshape/i18n.py`
