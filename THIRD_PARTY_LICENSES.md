# Third-Party Licenses

All production dependencies of `ledger-agent` use permissive open-source
licenses. This file lists each dependency, its version range, and its license.

| Package | License | Homepage |
|---------|---------|----------|
| pdfplumber | MIT | https://github.com/jsvine/pdfplumber |
| pdfminer.six | MIT | https://github.com/pdfminer/pdfminer.six |
| rich | MIT | https://github.com/Textualize/rich |
| questionary | MIT | https://github.com/tmbo/questionary |
| tabulate | MIT | https://github.com/astanin/python-tabulate |
| colorama | BSD-3-Clause | https://github.com/tartley/colorama |
| pandas | BSD-3-Clause | https://github.com/pandas-dev/pandas |
| openpyxl | MIT | https://foss.heptapod.net/openpyxl/openpyxl |
| python-dateutil | Apache 2.0 / BSD | https://github.com/dateutil/dateutil |
| rapidfuzz | MIT | https://github.com/maxbachmann/RapidFuzz |
| click | BSD-3-Clause | https://palletsprojects.com/p/click/ |
| typer | MIT | https://typer.tiangolo.com/ |
| python-dotenv | BSD-3-Clause | https://github.com/theskumar/python-dotenv |

> **Note on fuzzy matching**: The codebase uses `rapidfuzz` (MIT), which is
> the recommended modern replacement for both `fuzzywuzzy` (GPL-2.0) and
> `thefuzz` (MIT). Verify no GPL dependency was reintroduced with:
> `grep -r "fuzzywuzzy" ledger_agent/ --include="*.py"`.

## Dev dependencies

Dev dependencies (pytest, ruff, etc.) are MIT/BSD licensed and are not
distributed with the production package.

## Spring Boot webapp

The webapp (Form D) uses Spring Boot (Apache 2.0), Thymeleaf (Apache 2.0),
and other Maven dependencies under permissive licences. A full Maven BOM
licence report can be generated with:

```bash
cd webapp
./mvnw license:aggregate-third-party-report
```

## Disclaimer

This licence survey was conducted on 2026-05-25. Dependencies may change;
re-run the survey before each release. Use `scripts/check_licenses.py` to
automate the check.
