## Summary

The token counts the gateway reports in `usage` are an estimate, made without a model file and without a network, so that they are the same in every deployment. The rule was: a word is a token, a number is a token, a mark of punctuation is a token.

That rule was badly wrong for the Chinese, Japanese and Korean writing. A run of those characters is one word to the rule and so one token, while a tokeniser of a model provider gives one token to each of those characters. A Chinese answer was counted at a quarter of its real size and a Japanese answer at a fifth, so a client that shows the tokens of a conversation was shown a number that had nothing to do with the answer.

The characters of those writings are now counted one by one. A run of digits is now counted in groups of three, which is how a tokeniser of a model provider splits them.

## Changes

- `server.py`: the characters of the Chinese, Japanese and Korean writing are a token of their own. The ranges are written as explicit escapes, so the rule does not depend on how the file is read.
- `server.py`: a run of digits is counted in groups of three, and the digits of every script are counted: a date written with the fullwidth digits counts like the same date written with the ASCII ones.
- `server.py`: the docstring of `token_count()` says what the count is.
- `tests/test_server.py`: nine regression tests (a character of the Chinese, the Japanese and the Korean writing is a token of its own, the digits are counted in groups of three, the digits of every script are counted, a text that mixes the writings counts each of them its own way, a radical of the Chinese writing next to a word is counted apart from the word, and the count of a foreign text does not depend on where it was split).
- `README.md`: a section that says what the counts are and what they are not.
- `CHANGELOG.md`: one `[FIX]` line under `## Unreleased`, and the line of the streamed usage now describes the rule of today.

## Acceptance criteria mirror

- [x] 1. A character of the Chinese, Japanese or Korean writing is a token of its own.
- [x] 2. A run of digits is counted in groups of three.
- [x] 3. The count of a text that arrives in pieces does not depend on where it was split, for those characters and for the digits as well.
- [x] 4. The count still needs no model file and no network.
- [x] 5. `ruff check .`, `ruff format --check .`, `mypy .` and `pytest` pass.

## Test plan

The repository's declared validation, in the pinned virtualenv (ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, Python 3.12.3):

- `ruff format --check .` — 9 files already formatted
- `ruff check .` — All checks passed!
- `mypy .` — Success: no issues found in 1 source file
- `pytest` — 163 passed

The four new tests were also run against the `server.py` of `main` before this change: three of them fail there, so they pin the change rather than the implementation. The fourth, which checks that the count does not depend on where the text was split, fails when the text that is carried from one piece to the next is dropped.

## How the count was measured

The count was measured against the `cl100k_base` tokeniser, built offline from the vocabulary of the `tiktoken-offline` wheel. It is a measuring instrument only: the gateway keeps no model file and no dependency.

| sample | `cl100k_base` | before | after | error before | error after |
|---|---|---|---|---|---|
| Chinese | 33 | 8 | 30 | −75.8% | −9.1% |
| Japanese | 31 | 6 | 32 | −80.6% | +3.2% |
| Korean | 34 | 11 | 25 | −67.6% | −26.5% |
| `README.md` | 1301 | 1264 | 1297 | −2.8% | −0.3% |
| `CHANGELOG.md` | 1397 | 1357 | 1375 | −2.9% | −1.6% |
| `server.py` | 18674 | 17368 | 17488 | −7.0% | −6.4% |
| `tests/test_server.py` | 23235 | 22865 | 23126 | −1.6% | −0.5% |

The count is closer on every sample, and the change does not touch the counting of the Latin writing beyond the digits.

## State of this pull request

The head under review is `587fe8e8b9462e174ea01998a18075d6ba0da53b`. Six rounds of independent fresh-context review drove the change; each round found something real and each fix has a test that fails without it. The last completed review returned REQUEST CHANGES on a completeness claim about the class of characters, which the last commit fixes: the class now holds the blocks of those writings whole, and a test holds one character of every block.

The review of that last head has not been run. A reviewer should check, above all, that the class holds every word character of those writings and that every block of it is held by the test.

## What is still an estimate

A word that no tokeniser holds in one piece is counted as one token, while a real tokeniser splits it into several; a word of thirty-four letters is counted as one token and is really eleven. A rule of the kind "a word longer than N letters is counted as a quarter of its letters" was measured against the same tokeniser and made the estimate worse on the files of this repository -- on `README.md` the error went from −0.3% to +4.8%, and on the test file from −0.5% to +10%, while it only helped `server.py`. No rule of that kind can be had without the vocabulary of the model, so the count stays an estimate and the README says so.

The digits of every script are counted the same way: a run of them is counted in groups of three, and the fullwidth digits of a Japanese date count like the ASCII digits of the same date.
