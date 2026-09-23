# Contributing

Thanks for helping. Issues and pull requests are welcome. For anything larger than a fix, and for any change to the safety rails, open an issue first so the approach can be agreed before the work.

## Contributor guide

[CLAUDE.md](CLAUDE.md) is the single contributor guide, for people and coding agents alike. It covers setup, the architecture, the checks, the test tiers, the conventions, the safety invariants no change may weaken, and the rules for keeping account data out of this public repository.

## Pull requests

- Keep each pull request to one change, with tests.
- Run the checks listed in CLAUDE.md; CI runs them too.
- Update the docs the tests keep in step with the code, as CLAUDE.md describes.
- Add an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- Say in the description whether you ran an integration suite (`live_readonly` or `paper`) and on which IB Gateway version, without account ids or other details of your setup.
- For a change to the safety rails, explain why it is safe and link the issue where it was agreed.

## Security

Report vulnerabilities privately, as [SECURITY.md](SECURITY.md) describes, not in a public issue.

## License

Contributions are accepted under the project's [MIT License](LICENSE).
