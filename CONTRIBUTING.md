# Contributing

## Reporting issues

Open a GitHub issue with:
1. What you were trying to do
2. What happened (including any error messages)
3. Your Python version and OS
4. Whether you installed the semantic dependencies

## Submitting changes

1. Fork the repo
2. Create a branch: `git checkout -b fix/description`
3. Make your changes
4. Install dev dependencies (`python -m pip install ".[dev]"`) and run
   `python -m pytest -q`, then `python scripts/test_recall.py --no-semantic`
5. Ensure no private info in your changes (see below)
6. Open a pull request

## Privacy rule

This is a sanitized public release. Never submit:
- Real names, email addresses, or personal identifiers in examples
- API keys, tokens, passwords, or credentials
- Internal hostnames, IPs, or infrastructure details
- Anything that would identify a specific person or organization

Use neutral placeholders (Alex, Project Alpha, Jane Smith) in all
examples and test data.

## Code style

- Python 3.10+ with type hints where practical
- Keep dependencies free and open-source only
- The core (non-semantic) path must work with zero ML dependencies
