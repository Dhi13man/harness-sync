## Summary

Describe the problem and the smallest change that solves it.

## Verification

- [ ] `python3 -m py_compile skills/harness-sync/scripts/harness_sync.py`
- [ ] `python3 -m unittest discover -s tests -p 'test_*.py'`
- [ ] `./tests/smoke_test.sh`
- [ ] A second unchanged sync reports only `skip`
- [ ] No secrets or machine-local configuration are included

## Scope

List any intentionally unsupported harnesses, platforms, or follow-up work.
